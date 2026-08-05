"""Intent Agent — 自然语言输入 → 结构化意图 + 参数提取"""
import json
import re
import sys

from governanceops.llm_client import LLMClient

INTENT_PROMPT = """You are a GovernanceOps command parser.

Classify user input into one intent and extract parameters.

Intents:
- AMEND: modify a decision (assign owner, set deadline, change status/risk/progress)
  Extract: decision_title (which decision), field (owner/deadline/status/risk/progress), value
- INGEST: extract governance objects from free text
- CHECKPOINT: run governance heartbeat
- QUERY: ask about decision status

Output STRICT JSON only:
{{"intent":"AMEND","decision_title":"跨部门协作流程优化","field":"owner","value":"王工"}}
{{"intent":"INGEST"}}
{{"intent":"CHECKPOINT"}}
{{"intent":"QUERY","decision_title":"ERP升级"}}

Input: {text}
JSON:"""


class IntentAgent:
    def __init__(self, llm_client: LLMClient | None = None):
        self.llm = llm_client or LLMClient()

    def parse(self, text: str) -> dict:
        return self._llm_parse(text)

    def _llm_parse(self, text: str) -> dict:
        messages = [{"role": "user", "content": INTENT_PROMPT.format(text=text)}]
        raw = self.llm.chat(messages, temperature=0.0, max_tokens=512)
        raw = re.sub(r"```(?:json)?\n?", "", raw).replace("```", "")
        return json.loads(raw)

    def _regex_parse(self, text: str) -> dict:
        text = text.strip()
        # Direct amend: "260601 owner 王工"
        m = re.match(r"^(\d{6})\s+(owner|deadline|status|risk|progress|title)\s+(.+)$", text, re.I)
        if m:
            return {"intent": "AMEND", "decision_id": m.group(1), "field": m.group(2).lower(), "value": m.group(3)}

        # NL amend: "跨部门协作... 分配给 王工"
        m = re.match(r"^(.+?)\s*(?:分配给|负责人是|改成|设置为|owner[:\s]*|deadline[:\s]*|截止[:\s]*|期限[:\s]*)\s*(.+)$", text, re.I)
        if m:
            title_part = m.group(1).strip().rstrip("的。，,.")
            value = m.group(2).strip().rstrip("。")
            is_deadline = bool(re.search(r"(\d{4}-\d{2}-\d{2})|(\d+月\d+[日号])", value))
            return {"intent": "AMEND", "decision_title": title_part,
                    "field": "deadline" if is_deadline else "owner", "value": value}

        # Status change: "XX已经完成了" / "XX成功了" / "XX做完了" / "XX done"
        m = re.match(r"^(.+?)\s*(?:已经完成了|已经完成|已完成|成功了|做完了|完成了|已上线|已交付|done|已解决)\s*[了。！!]*$", text, re.I)
        if m:
            return {"intent": "AMEND", "decision_title": m.group(1).strip().rstrip("的。，,。已经已"),
                    "field": "status", "value": "DONE"}

        # Risk change: "XX是高风险" / "XX risk high"
        m = re.match(r"^(.+?)\s*(?:是高风险|风险高|risk[:\s]*high)\s*$", text, re.I)
        if m:
            return {"intent": "AMEND", "decision_title": m.group(1).strip().rstrip("的。，,。"),
                    "field": "risk_level", "value": "HIGH"}

        # Checkpoint triggers
        if any(w in text for w in ["心跳", "checkpoint", "检查", "巡查"]):
            return {"intent": "CHECKPOINT"}

        # Default: ingest
        return {"intent": "INGEST"}
