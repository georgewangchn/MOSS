from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import logging
import yaml

_active_skills: List[str] = ["default"]
_STATE_FILE: Optional[Path] = None
_logger = logging.getLogger("moss.skills")


def _get_state_file() -> Path:
    global _STATE_FILE
    if _STATE_FILE is None:
        from moss.config import get_config
        cfg = get_config()
        _STATE_FILE = cfg.data_path() / "active_skills.json"
    return _STATE_FILE


def _save_state():
    import json
    f = _get_state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(_active_skills, ensure_ascii=False))


def _load_state():
    global _active_skills
    f = _get_state_file()
    if f.exists():
        try:
            import json
            loaded = json.loads(f.read_text())
            if isinstance(loaded, list) and len(loaded) > 0:
                _active_skills = loaded
                if "default" not in _active_skills:
                    _active_skills.insert(0, "default")
        except Exception as e:
            _logger.warning("Failed to load active skills state from %s: %s", f, e)


@dataclass
class Skill:
    name: str
    description: str
    system_prompt: str
    section_hint: str = ""
    extraction_rules: str = ""
    tags: list[str] = field(default_factory=list)


SKILL_REGISTRY: Dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    SKILL_REGISTRY[skill.name] = skill
    return skill


def get_skill(name: str) -> Optional[Skill]:
    return SKILL_REGISTRY.get(name)


def list_skills() -> list[Skill]:
    return list(SKILL_REGISTRY.values())


def load_skill(name: str) -> Skill:
    _load_state()
    skill = get_skill(name)
    if not skill:
        raise ValueError(f"Skill '{name}' not found. Available: {list(SKILL_REGISTRY.keys())}")
    if name not in _active_skills:
        _active_skills.append(name)
        _save_state()
    return skill


def remove_skill(name: str) -> bool:
    _load_state()
    if name == "default":
        return False
    if name in _active_skills:
        _active_skills.remove(name)
        _save_state()
        return True
    return False


def _parse_skill_md(filepath: Path) -> Optional[Skill]:
    """Parse a governance SKILL.md file into a Skill object.

    Reads YAML frontmatter + markdown sections. Expected sections:
      ## System Prompt → skill.system_prompt
      ## Extraction Rules → skill.extraction_rules
      ## Domain Knowledge → appended to extraction_rules
      ## Examples → used as section_hint for the LLM
    """
    import re
    content = filepath.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    frontmatter_text = parts[1].strip()
    body = parts[2].strip()

    try:
        fm = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None

    name = str(fm.get("name", ""))
    if not name:
        return None
    description = str(fm.get("description", ""))
    tags_raw = fm.get("tags", [])

    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    elif isinstance(tags_raw, str) and tags_raw:
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = []

    sections = re.split(r"\n## ", body)
    system_prompt = ""
    extraction_rules = ""
    domain_knowledge = ""
    section_hint = ""

    for section in sections:
        section = section.strip()
        if section.startswith("System Prompt"):
            m = re.search(r"```(.*?)```", section, re.DOTALL)
            if m:
                system_prompt = m.group(1).strip()
        elif section.startswith("Extraction Rules"):
            m = re.search(r"```(?:yaml)?(.*?)```", section, re.DOTALL)
            if m:
                extraction_rules = m.group(1).strip()
        elif section.startswith("Domain Knowledge"):
            domain_knowledge = section.split("\n", 1)[-1].strip() if "\n" in section else ""
        elif section.startswith("Examples"):
            section_hint = section.split("\n", 1)[-1].strip() if "\n" in section else ""

    full_rules = extraction_rules
    if domain_knowledge:
        full_rules = f"{extraction_rules}\n\n## Domain Knowledge\n{domain_knowledge}".strip()

    return Skill(
        name=name,
        description=description,
        system_prompt=system_prompt or "You are a governance extraction expert.",
        extraction_rules=full_rules,
        section_hint=section_hint[:500] if section_hint else "",
        tags=tags,
    )


def load_skills_from_disk() -> int:
    """Scan MOSS skill directories for scene-based SKILL.md files.

    Searches only MOSS-specific paths:
      1. {project}/moss/skills/<skill-name>/SKILL.md
      2. {data_dir}/skills/<skill-name>/SKILL.md

    These are MOSS agent skills — distinct from WorkBuddy skills.
    Each skill defines how to extract governance objects from a specific
    document scene type (meeting minutes, weekly reports, emails, etc.).

    Returns the number of newly loaded skills.
    """
    from moss.config import get_config
    cfg = get_config()
    search_dirs = [
        Path(__file__).parent / "skills",      # bundled skills (pip-installed)
        cfg.data_path() / "skills",             # user-custom skills
    ]

    loaded = 0
    for base in search_dirs:
        if not base.exists():
            continue
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            md_file = skill_dir / "SKILL.md"
            if not md_file.exists():
                continue
            # Skip if already registered in built-in registry
            name_from_dir = skill_dir.name
            if name_from_dir in SKILL_REGISTRY:
                continue
            skill = _parse_skill_md(md_file)
            # Only register if it has the governance-extraction type marker
            # AND a non-empty system_prompt (filters out non-governance skills)
            if skill and skill.system_prompt and len(skill.system_prompt) > 50:
                register(skill)
                loaded += 1

    return loaded


def get_active_skills() -> List[Skill]:
    _load_state()
    if "default" not in _active_skills:
        _active_skills.insert(0, "default")
        _save_state()
    return [get_skill(n) for n in _active_skills if get_skill(n)]  # type: ignore[misc]


def active_skill_names() -> List[str]:
    _load_state()
    return list(_active_skills)


def match_skill(document: str) -> Skill:
    active = get_active_skills()
    if len(active) == 1:
        return active[0]

    for skill in active:
        if skill.name == "default":
            continue
        if _match_weekly(skill, document):
            return skill
        if _match_email(skill, document):
            return skill
        if _match_meeting(skill, document):
            return skill
        if _match_project(skill, document):
            return skill

    return active[0]


def _match_weekly(skill: Skill, doc: str) -> bool:
    return "weekly" in skill.tags and (
        "周报" in doc
        or ("本周" in doc and "下周" in doc)
        or any(h in doc for h in ["## 销售", "## 采购", "## 研发", "## 部门"])
    )


def _match_email(skill: Skill, doc: str) -> bool:
    return "email" in skill.tags and (
        "From:" in doc or "Subject:" in doc or "发件人" in doc or "收件人" in doc
    )


def _match_meeting(skill: Skill, doc: str) -> bool:
    return "meeting" in skill.tags and (
        "会议纪要" in doc or "会议主题" in doc or "会议结论" in doc
        or "参会人" in doc or "议题" in doc or "决议" in doc
    )


def _match_project(skill: Skill, doc: str) -> bool:
    return "project" in skill.tags and (
        "里程碑" in doc or "关键路径" in doc or "项目概述" in doc or "交付物" in doc
    )


# ═══════════════════════════════════════════════════════════════
# Built-in Skills
# ═══════════════════════════════════════════════════════════════

register(Skill(
    name="default",
    description="通用提取 — 适用于任意企业文本",
    tags=["universal"],
    system_prompt="""You are MOSS Parser for Chinese enterprise documents.
Extract governance objects from business text (meeting minutes, reports, emails, work plans).

YOUR JOB IS EXTRACTION ONLY — not validation:
- Capture EVERY decision mentioned in the text, even if it lacks owner/deadline.
- Missing fields (owner=null, deadline=null) are EXPECTED from vague documents.
- The system will flag gaps separately; follow-up text will fill them in.
- DO NOT skip an item just because it has no owner. If the text says "推进XX", extract it.

WHAT TO EXTRACT:
- decisions: action items, resolutions, tasks — each numbered item or explicit "to-do" statement
- issues: problems, blockers, risks — things that need attention but are not yet assigned
- assignments: who is responsible for what — "XXX负责", "由XXX牵头", "责任人：XXX"
- updates: progress reports on existing work — "已完成", "进行中", "下周计划"
- risks: explicit risk items with severity if stated

CRITICAL RULES:
- Each meeting conclusion item (1. 2. 3.) is a SEPARATE decision — ALWAYS extract all of them
- "尽快推进XX" → decision, owner=null UNLESS owner is explicitly named
- "加强XX"/"依法XX"/"落实XX"/"汇报XX" → ALL are decisions, extract them
- "XX负责" → decision + assignment with owner AND department
- "X月X日前" → deadline in ISO format YYYY-MM-DD
- Output strict JSON only. No markdown, no explanations.
- DO NOT hallucinate missing fields. owner=null unless explicitly named.""",
))


register(Skill(
    name="weekly-report",
    description="中文企业周报 — 按部门/章节提取决策、进展、风险",
    tags=["report", "weekly", "chinese"],
    system_prompt="""You are MOSS Parser specialized in CHINESE CORPORATE WEEKLY REPORTS.

A Chinese weekly report typically follows this structure:
- # 本周周报 (weekly report header)
- ## 部门名 (department sections: 销售部, 研发部, 采购部, etc.)
- Each section contains: 本周完成 (completed), 下周计划 (planned), 风险/问题 (risks)

EXTRACTION RULES for weekly reports:
1. Each departmental section may contain MULTIPLE decisions
2. "下周计划" items → decisions (action items for next week)
3. "风险/问题" items → issues + risks
4. "本周完成" items → updates (progress reports on existing decisions)
5. Owner is usually mentioned as "XXX负责" or "负责人：XXX"
6. Deadlines appear as "截止日期", "deadline", "需在X前完成"
7. Progress indicators: "完成X%", "进行中", "已完成", "待启动"
8. Department = the section header (e.g., "销售部门" → department: "销售中心")

Output strict JSON only. Extract ALL governance objects mentioned.""",
    section_hint="This is one section of a weekly report. Focus on this section's department/topic.",
    extraction_rules="Extract decisions from '下周计划'. Extract issues from '风险/问题'. Extract updates from '本周完成'.",
))


register(Skill(
    name="email",
    description="企业邮件 — 提取邮件头、正文、转发线索中的治理对象",
    tags=["email", "communication"],
    system_prompt="""You are MOSS Parser specialized in CORPORATE EMAILS.

Corporate emails have this structure:
- Headers: From, To, CC, Subject, Date
- Body: main message content
- Signature block (ignore for extraction)
- Thread: previous messages below, marked with ">" or "---Original Message---"

EXTRACTION RULES for emails:
1. Subject line often contains the decision summary
2. CC list → potential additional stakeholders
3. Action items are often phrased as "请XXX", "需要XXX", "请尽快"
4. Deadlines appear as "本周五前", "下周一", "3天内"
5. Email sender is usually the decision owner unless someone else is delegated
6. Inline replies may contain progress updates on previously discussed decisions
7. Attachments mentioned → note as potential evidence sources

Output strict JSON only. Extract ALL governance objects mentioned.""",
    section_hint="This is one part of an email thread. Focus on what THIS message adds to the conversation.",
    extraction_rules="Sender is default owner unless explicitly delegated. Check CC for stakeholders. Thread replies contain updates.",
))


register(Skill(
    name="meeting-minutes",
    description="会议纪要 — 提取议题、决议、行动项、责任人",
    tags=["meeting", "minutes"],
    system_prompt="""You are MOSS Parser specialized in MEETING MINUTES.

Meeting minutes typically contain:
- 会议主题 (meeting topic)
- 参会人员 (attendees) → potential owners/stakeholders  
- 议题列表 (agenda items) → issues
- 决议 (resolutions) → decisions
- 行动项 (action items) → decisions with owners
- 下一步 (next steps) → future decisions

EXTRACTION RULES for meeting minutes:
1. Each agenda item → 1 issue
2. Each resolution → 1 decision (status: OPEN)
3. Each action item with "XXX负责" → 1 decision with owner assigned
4. Action items with "期限/截止" → decision with deadline
5. "会议决定" or "决议" sections → decisions
6. Attendees listed as "参会人：XXX, YYY" → candidate owners
7. "遗留问题" or "待跟进" → issues with high severity

Output strict JSON only. Extract ALL governance objects mentioned.""",
    section_hint="This is a section of meeting minutes. Focus on decisions and action items assigned in this section.",
    extraction_rules="Each action item = 1 decision. 'XXX负责' = owner. '期限: X' = deadline. Attendees = candidate owners.",
))


register(Skill(
    name="project-report",
    description="项目报告 — 提取里程碑、任务、风险、资源需求",
    tags=["project", "report"],
    system_prompt="""You are MOSS Parser specialized in PROJECT REPORTS.

Project reports contain:
- 项目概述 (project overview)
- 里程碑进度 (milestone progress) → decisions with progress
- 任务列表 (task list) → individual decisions
- 风险登记 (risk register) → risks
- 资源需求 (resource needs) → issues
- 关键路径 (critical path) → high-priority decisions

EXTRACTION RULES for project reports:
1. Each milestone → 1 decision (progress from milestone status)
2. Each task with owner → 1 decision with assignment
3. "高风险" items → decisions with risk_level="HIGH"
4. "阻塞" or "blocked" items → decisions with status="BLOCKED"
5. "延期" items → issues with severity="HIGH"
6. Resource requests → issues
7. Key dates → deadlines for decisions

Output strict JSON only. Extract ALL governance objects mentioned.""",
    section_hint="This is a section of a project report. Focus on tasks, milestones, and risks specific to this section.",
    extraction_rules="Milestones = decisions with progress. Blocked items = decisions status=BLOCKED. '高风险' = risk_level=HIGH.",
))
