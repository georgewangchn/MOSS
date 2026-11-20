import json
import operator
import re
import sys
from datetime import datetime
from typing import Annotated, List, TypedDict

# LangChain/LangGraph imports are deferred to enable graceful degradation.
# When langchain/langgraph are not installed, AgentExtractor falls back to
# the simpler Extractor (httpx-based) for document processing.

from moss.config import get_config
from moss.models import (
    Assignment,
    CompileIssue,
    Decision,
    GovernanceGraph,
    Issue,
    ParsedResult,
    Update,
)
from moss.owl_compiler import OwlCompiler
from moss.owl_ontology import reason_graph
from moss.skills import Skill, get_active_skills, get_skill, match_skill


def _check_langchain_available() -> bool:
    """Check if langchain/langgraph are installed."""
    try:
        import langchain_core  # noqa: F401
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False


# Lazy import helpers — only called when langchain is available
def _import_langchain():
    """Import langchain components lazily."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.runnables import RunnableConfig
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.constants import Send
    from langgraph.graph import END, StateGraph
    from pydantic import SecretStr
    return (HumanMessage, SystemMessage, RunnableConfig, ChatOpenAI,
            InMemorySaver, Send, END, StateGraph, SecretStr)


class Section(TypedDict):
    index: int
    text: str


class Gap(TypedDict):
    decision_title: str
    gap_type: str
    message: str


class ExtractionState(TypedDict):
    document: str
    source: str
    sections: Annotated[List[Section], operator.add]
    section_results: Annotated[List[dict], operator.add]
    merged: dict
    gaps: Annotated[List[Gap], operator.add]
    result: dict
    trace: Annotated[List[str], operator.add]


def _build_governance_schema(skill: Skill) -> str:
    """Generate the governance contract from ontology definitions + skill rules."""
    schema = """GOVERNANCE ONTOLOGY CONTRACT — defined by owlready2 OWL classes:

ONTOLOGY DEFINED CLASSES (HermiT classification targets):
  GovernedDecision ≡ Decision ⊓ (∃hasOwner) ⊓ (∃hasDeadline) ⊓ (∃hasUpdate)
  HasOwnerDecision  ≡ Decision ⊓ (∃hasOwner)
  HasDeadlineDecision ≡ Decision ⊓ (∃hasDeadline)

CONSTRAINTS (validated by HermiT reasoner):
  ERROR if: Decision is active AND does NOT satisfy HasOwnerDecision
  WARNING if: Decision is active AND does NOT satisfy HasDeadlineDecision
  WARNING if: Decision is active AND has no Update objects

RULES:
  - ONLY extract what is EXPLICITLY stated in text
  - owner=null UNLESS a specific person/department is explicitly named
  - deadline=null UNLESS a specific date/time is explicitly mentioned
  - dependency=null UNLESS a collaborating department/unit is explicitly named
  - DO NOT hallucinate or infer missing data"""
    if skill.extraction_rules:
        schema += f"\n\nDOMAIN-SPECIFIC RULES ({skill.name}):\n{skill.extraction_rules}"
    return schema


SECTION_PROMPT = """Extract governance objects from this document section.

{governance_schema}

EXTRACTION vs VALIDATION — CRITICAL SEPARATION:
  The ontology contract below defines the TARGET state for FULLY-GOVERNED decisions.
  Your job is EXTRACTION ONLY: capture EVERYTHING the text says.
  Missing owners/deadlines ARE EXPECTED — the system will flag gaps (ERROR/WARNING)
  and the user will resolve them via follow-up "moss ingest" commands.
  DO NOT skip items just because they lack owners or deadlines.
  If the text says "推进XX" → EXTRACT IT as a decision, even if no owner is named.

Output JSON with keys: issues, decisions, assignments, updates, risks.
For each decision, include a "source_quote" field with the exact text that supports the extraction.
Each assignment object has: decision_title, owner, department, dependency, deadline, raw_statement.

EXAMPLE — vague meeting minutes without specific owners:
  会议结论：
  1. 尽快推进供应商切换。
  2. 依法依规处理争议问题。

Expected output (note: NO owners mentioned → all owner=null, STILL extract all decisions):
{{
  "decisions": [
    {{"title": "推进供应商切换", "raw_statement": "尽快推进供应商切换", "source_quote": "1. 尽快推进供应商切换。"}},
    {{"title": "依法依规处理争议问题", "raw_statement": "依法依规处理争议问题", "source_quote": "2. 依法依规处理争议问题。"}}
  ],
  "assignments": [],
  "issues": [],
  "updates": [],
  "risks": []
}}

EXAMPLE — meetings with explicit owners:
  会议结论：
  1. 尽快推进供应商切换。
  2. 3月底前完成Q1审计，张部长负责。

Expected output:
{{
  "decisions": [
    {{"title": "推进供应商切换", "raw_statement": "尽快推进供应商切换", "source_quote": "1. 尽快推进供应商切换。"}},
    {{"title": "完成Q1审计", "raw_statement": "3月底前完成Q1审计，张部长负责", "source_quote": "2. 3月底前完成Q1审计，张部长负责。"}}
  ],
  "assignments": [
    {{"decision_title": "完成Q1审计", "owner": "张部长", "deadline": "2026-03-31", "raw_statement": "3月底前完成Q1审计，张部长负责"}}
  ],
  "issues": [],
  "updates": [],
  "risks": []
}}

KEY RULES:
- EVERY meeting conclusion item (1. 2. 3. ...) → EXTRACT as a SEPARATE decision, ALWAYS
- SUBORDINATE CLAUSE MERGE: When text uses "——" or "需/包括/如" to list subordinate details of ONE main decision, extract ONE decision only.
  "推进XX——需加强YY，依法ZZ，及时汇报" → ONE decision "推进XX" (NOT 4 separate ones).
- Items without owners → STILL extract as decision, owner=null. This is NORMAL.
- Items without deadlines → STILL extract as decision, deadline=null. This is NORMAL.
- "XX负责" → extract as assignment with owner
- "X月X日前"/"X月底前" → deadline in ISO format YYYY-MM-DD
- DO NOT hallucinate owners or deadlines that are not stated

Section text:
{text}

JSON:"""


MERGE_PROMPT = """Merge {count} extraction results into one deduplicated result.
Same decision in multiple sections → merge into one, keep most detailed version.
Conflicting fields → keep last mentioned.
Output ONLY the merged JSON.

{governance_schema}

Results:
{results}

Merged JSON:"""


def _parse_json(raw: str) -> dict:
    cleaned = raw.strip().removeprefix("\ufeff")
    lines = cleaned.split("\n")
    if lines and lines[0].strip().startswith(("```", "~~~")):
        lines = lines[1:]
    if lines and lines[-1].strip() in ("```", "~~~"):
        lines = lines[:-1]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {}


def _combine_raw(results: List[dict]) -> dict:
    combined = {"issues": [], "decisions": [], "assignments": [], "updates": [], "risks": []}
    seen_issues = set()
    seen_decisions = set()
    for r in results:
        for issue in r.get("issues", []):
            k = issue.get("title", "").strip().lower()
            if k and k not in seen_issues:
                seen_issues.add(k)
                combined["issues"].append(issue)
        for dec in r.get("decisions", []):
            k = dec.get("title", "").strip().lower()
            if k and k not in seen_decisions:
                seen_decisions.add(k)
                combined["decisions"].append(dec)
        for key in ("assignments", "updates", "risks"):
            combined[key].extend(r.get(key, []))
    return combined


def _split_document(text: str) -> List[str]:
    parts = re.split(r"\n(?=#{1,3}\s)", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return parts
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [text]
    chunks, buf = [], ""
    for p in paragraphs:
        if len(buf) + len(p) < 4000:
            buf = (buf + "\n\n" + p) if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks or [text]


def _log(msg: str, error: bool = False, trace: bool = False):
    prefix = "[Agent]" if not error else "[Agent] ERROR:"
    if trace:
        prefix = "  ╰─"
    print(f"{prefix} {msg}", file=sys.stderr if error else sys.stdout)


class AgentExtractor:
    def __init__(self, skill: Skill | str | None = None):
        if isinstance(skill, str):
            skill = get_skill(skill) or get_skill("default")
        self.skill = skill if skill is not None else get_skill("default")  # type: ignore[assignment]
        assert self.skill is not None, "default skill must be registered"
        self._governance_schema = _build_governance_schema(self.skill)

        self._langchain_available = _check_langchain_available()
        if self._langchain_available:
            (HumanMessage, SystemMessage, RunnableConfig, ChatOpenAI,
             InMemorySaver, Send, END, StateGraph, SecretStr) = _import_langchain()

            cfg = get_config()
            api_base = cfg.llm.api_url
            if "/v1/chat/completions" in api_base:
                api_base = api_base.rsplit("/v1/chat/completions", 1)[0] + "/v1"
            elif not api_base.endswith("/v1"):
                api_base = api_base.rstrip("/") + "/v1"
            self.llm = ChatOpenAI(
                model=cfg.llm.model,
                api_key=SecretStr(cfg.llm.api_key),
                base_url=api_base,
                temperature=0.1,
                timeout=60,
                max_retries=2,
            )
            self.checkpointer = InMemorySaver()
            self._Send = Send
            self._END = END
            self._StateGraph = StateGraph
            self._HumanMessage = HumanMessage
            self._SystemMessage = SystemMessage
            self._RunnableConfig = RunnableConfig
            self.graph = self._build_graph()
            self._thread_id = f"ingest-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        else:
            # Degraded mode: use Extractor (httpx-based, no langchain needed)
            print("[Agent] langchain/langgraph not installed — using degraded mode (Extractor fallback)", file=sys.stderr)
            from moss.extractor import Extractor
            self._fallback_extractor = Extractor()
            self.graph = None

    def _build_graph(self):
        builder = self._StateGraph(ExtractionState)

        builder.add_node("chunker", self._chunker_node)
        builder.add_node("section_extractor", self._section_extractor_node)
        builder.add_node("merge_results", self._merge_node)
        builder.add_node("validate", self._validate_node)
        builder.add_node("finalize", self._finalize_node)

        builder.set_entry_point("chunker")
        builder.add_conditional_edges("chunker", self._route_sections, {"section_extractor": "section_extractor"})
        builder.add_edge("section_extractor", "merge_results")
        builder.add_edge("merge_results", "validate")
        builder.add_edge("validate", "finalize")
        builder.add_edge("finalize", self._END)

        return builder.compile(checkpointer=self.checkpointer)

    def ingest(self, text: str, source: str = "ingest") -> ParsedResult:
        if self.skill.name == "default" and len(get_active_skills()) > 1:  # type: ignore[union-attr]
            self.skill = match_skill(text)  # type: ignore[assignment]
            self._governance_schema = _build_governance_schema(self.skill)  # type: ignore[arg-type]

        # Degraded mode: use Extractor fallback
        if not self._langchain_available or self.graph is None:
            return self._fallback_extract(text, source)

        config = self._RunnableConfig(configurable={"thread_id": self._thread_id})
        initial: ExtractionState = {
            "document": text, "source": source,
            "sections": [], "section_results": [], "merged": {},
            "gaps": [], "result": {}, "trace": [],
        }
        try:
            final = self.graph.invoke(initial, config)
            result = final.get("result", {})
            self._print_trace(final.get("trace", []))
            return ParsedResult(**result) if result else ParsedResult()
        except Exception as e:
            _log(f"Ingest failed: {e}", error=True)
            return ParsedResult()

    def _print_trace(self, trace: List[str]):
        if not trace:
            return
        _log("Extraction trace:")
        for t in trace:
            _log(t, trace=True)

    def _fallback_extract(self, text: str, source: str) -> ParsedResult:
        """Degraded extraction without langchain — use Extractor (httpx-based).

        Splits document into sections, extracts each with Extractor,
        then combines results. No refine/validate loop but still functional.
        """
        sections = _split_document(text)
        all_results = []
        for i, section_text in enumerate(sections):
            try:
                result = self._fallback_extractor.extract(
                    section_text,
                    existing_context="",
                    skill=self.skill if isinstance(self.skill, Skill) else None,
                )
                if result and (result.decisions or result.issues):
                    all_results.append(result)
                    _log(f"  Section {i+1}: {len(result.decisions)} decisions")
            except Exception as e:
                _log(f"  Section {i+1} error: {e}", error=True)

        if not all_results:
            return ParsedResult()

        # Merge all ParsedResult objects
        merged = ParsedResult()
        for r in all_results:
            merged.decisions.extend(r.decisions)
            merged.issues.extend(r.issues)
            merged.assignments.extend(r.assignments)
            merged.updates.extend(r.updates)
            merged.risks.extend(r.risks)
        _log(f"Fallback extraction: {len(merged.decisions)} decisions, {len(merged.issues)} issues")
        return merged

    def _chunker_node(self, state: ExtractionState) -> dict:
        sections = _split_document(state["document"])
        return {"sections": [Section(index=i, text=s) for i, s in enumerate(sections)]}

    def _route_sections(self, state: ExtractionState) -> List:
        secs = state["sections"]
        if not secs:
            return []
        if len(secs) == 1:
            return [self._Send("section_extractor", {"sections": secs})]
        return [self._Send("section_extractor", {"sections": [s]}) for s in secs]

    def _section_extractor_node(self, state: ExtractionState) -> dict:
        results = []
        for section in state["sections"]:
            try:
                r = self._extract_one_section(section["text"], section["index"])
                if r:
                    results.append(r)
                    _log(f"  Section {section['index']+1}: {len(r.get('decisions',[]))} decisions, {len(r.get('issues',[]))} issues")
            except Exception as e:
                _log(f"  Section {section['index']+1} error: {e}", error=True)
        return {"section_results": results}

    def _extract_one_section(self, text: str, index: int) -> dict:
        system = self.skill.system_prompt  # type: ignore[union-attr]
        try:
            response = self.llm.invoke([
                self._SystemMessage(content=system),
                self._HumanMessage(content=SECTION_PROMPT.format(
                    governance_schema=self._governance_schema, text=text
                )),
            ])
            content = response.content
            if isinstance(content, list):
                content = str(content[0]) if content else ""
            return _parse_json(str(content))
        except Exception:
            return {}

    def _merge_node(self, state: ExtractionState) -> dict:
        results = state.get("section_results", [])
        if not results:
            return {"merged": {}}
        if len(results) == 1:
            return {"merged": results[0]}
        try:
            results_json = json.dumps(results, ensure_ascii=False, indent=2)
            response = self.llm.invoke([
                self._SystemMessage(content=self.skill.system_prompt),  # type: ignore[union-attr]
                self._HumanMessage(content=MERGE_PROMPT.format(
                    count=len(results), results=results_json,
                    governance_schema=self._governance_schema,
                )),
            ])
            content = response.content
            if isinstance(content, list):
                content = str(content[0]) if content else ""
            parsed = _parse_json(str(content))
            return {"merged": parsed if parsed else _combine_raw(results)}
        except Exception as e:
            _log(f"Merge failed, using raw combine: {e}", error=True)
            return {"merged": _combine_raw(results)}

    def _validate_node(self, state: ExtractionState) -> dict:
        merged = state.get("merged", {})
        if not merged or not merged.get("decisions"):
            return {"gaps": [], "trace": ["No decisions to validate"]}

        graph = self._merged_to_graph(merged)
        compiler = OwlCompiler()
        compile_issues = compiler.compile(graph)
        classified = reason_graph(graph)

        gaps: List[Gap] = []
        trace: List[str] = []

        for ci in compile_issues:
            if ci.severity == "ERROR":
                gaps.append(Gap(decision_title=ci.decision_title, gap_type=ci.issue_type, message=ci.message))
                trace.append(f"  ❌ [HermiT] {ci.decision_id} {ci.decision_title}: {ci.issue_type}")
            elif ci.severity == "WARNING":
                gaps.append(Gap(decision_title=ci.decision_title, gap_type=ci.issue_type, message=ci.message))
                trace.append(f"  ⚠ [HermiT] {ci.decision_id} {ci.decision_title}: {ci.issue_type}")
            elif ci.issue_type == "OK":
                if ci.decision_id in classified.get("governed", []):
                    trace.append(f"  ✓ [HermiT→GovernedDecision] {ci.decision_id} {ci.decision_title}")
                else:
                    trace.append(f"  ✓ [HermiT] {ci.decision_id} {ci.decision_title}: OK")

        if gaps:
            errors = sum(1 for ci in compile_issues if ci.severity == "ERROR")
            warnings = sum(1 for ci in compile_issues if ci.severity == "WARNING")
            _log(f"HermiT validation: {len(gaps)} gaps ({errors} errors, {warnings} warnings)")
        else:
            _log("HermiT validation: all governance checks passed ✓")

        return {"gaps": gaps, "trace": trace}

    def _merged_to_graph(self, merged: dict) -> GovernanceGraph:
        graph = GovernanceGraph()
        for i, dec in enumerate(merged.get("decisions", [])):
            did = f"AGENT-{i+1:03d}"
            title = dec.get("title", f"Decision-{i+1}")
            graph.decisions[did] = Decision(
                id=did, title=title,
                source="agent-extract",
                raw_statement=dec.get("raw_statement", ""),
            )

        for i, issue_data in enumerate(merged.get("issues", [])):
            iid = f"AGENT-ISSUE-{i+1:03d}"
            graph.issues[iid] = Issue(
                id=iid,
                title=issue_data.get("title", f"Issue-{i+1}"),
                description=issue_data.get("description", ""),
            )

        for a in merged.get("assignments", []):
            decision_title = a.get("decision_title", "")
            owner = a.get("owner")
            if not owner:
                continue
            for did, d in graph.decisions.items():
                if d.title.lower() == decision_title.lower():
                    d.owner = owner
                    if a.get("department"):
                        d.department = a["department"]
                    if a.get("dependency"):
                        d.dependency = a["dependency"]
                    if a.get("deadline"):
                        try:
                            d.deadline = datetime.fromisoformat(str(a["deadline"]))
                        except (ValueError, TypeError):
                            pass
                    graph.assignments.append(Assignment(
                        decision_id=did, owner=owner, department=a.get("department"),
                    ))
                    break

        for u in merged.get("updates", []):
            decision_title = u.get("decision_title", "")
            content = u.get("content", "")
            if not content:
                continue
            for did, d in graph.decisions.items():
                if d.title.lower() == decision_title.lower():
                    graph.updates.append(Update(
                        id=graph.get_next_update_id(),
                        decision_id=did,
                        content=content,
                        progress=u.get("progress"),
                    ))
                    break

        return graph

    def _finalize_node(self, state: ExtractionState) -> dict:
        merged = state.get("merged", {})
        if not merged:
            merged = _combine_raw(state.get("section_results", []))
        return {"result": merged}
