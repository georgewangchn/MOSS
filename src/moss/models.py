import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

DecisionStatus = Literal["OPEN", "BLOCKED", "RUNNING", "DONE"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
PriorityLevel = Literal["P0", "P1", "P2", "P3", "P4"]
IssueSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
CompileSeverity = Literal["ERROR", "WARNING", "OK"]
AlertSeverity = Literal["HIGH", "MEDIUM", "LOW"]
CompileIssueType = Literal["MISSING_OWNER", "MISSING_DEADLINE", "MISSING_DEPENDENCY", "MISSING_UPDATE", "STALE", "ROLE_MISALIGNED", "ROLE_UNREGISTERED", "OK"]
AlertType = Literal["OVERDUE", "HIGH_RISK", "STALE", "NO_PROGRESS", "BLOCKED"]


class Issue(BaseModel):
    id: str
    title: str
    description: str = ""
    severity: IssueSeverity = "MEDIUM"
    created_at: datetime = Field(default_factory=datetime.now)


class Update(BaseModel):
    id: str
    decision_id: str
    content: str
    progress: Optional[float] = None
    risk: Optional[RiskLevel] = None
    created_at: datetime = Field(default_factory=datetime.now)
    source: str = "cli"


class Decision(BaseModel):
    id: str
    title: str
    source: str = "ingest"
    issue_id: Optional[str] = None
    owner: Optional[str] = None
    department: Optional[str] = None
    dependency: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)  # 依赖的决策 ID（区别于 dependency 协作部门）
    domain: Optional[str] = None       # 业务域（org 词汇表约束，如 财务/GMP生产/电池安全）
    owner_role: Optional[str] = None   # 岗位（org 词汇表约束，如 首席财务官/质量负责人）
    created_at: datetime = Field(default_factory=datetime.now)
    deadline: Optional[datetime] = None
    status: DecisionStatus = "OPEN"
    progress: float = 0.0
    risk_level: RiskLevel = "LOW"
    last_update_at: Optional[datetime] = None
    raw_statement: str = ""

    @computed_field
    @property
    def priority(self) -> PriorityLevel:
        return compute_priority(self)


class Assignment(BaseModel):
    decision_id: str
    owner: str
    department: Optional[str] = None
    assigned_at: datetime = Field(default_factory=datetime.now)


class GovernanceGraph(BaseModel):
    decisions: Dict[str, Decision] = Field(default_factory=dict)
    issues: Dict[str, Issue] = Field(default_factory=dict)
    updates: List[Update] = Field(default_factory=list)
    assignments: List[Assignment] = Field(default_factory=list)
    edges: Dict[str, Dict[str, List[str]]] = Field(default_factory=lambda: {
        "issue_to_decision": {},
        "decision_to_updates": {},
        "decision_to_assignment": {},
    })

    def get_next_decision_id(self) -> str:
        """Generate next decision ID: 260601, 260602, ... (YYWW + sequence)"""
        from datetime import datetime
        now = datetime.now()
        prefix = now.strftime("%y%W")  # e.g. "2606" for 2026 week 6
        existing = [k for k in self.decisions.keys() if k.startswith(prefix)]
        if not existing:
            return f"{prefix}01"
        max_num = max(int(k[4:]) for k in existing)
        return f"{prefix}{max_num + 1:02d}"

    def get_next_issue_id(self) -> str:
        """Generate next issue ID: I-260601, I-260602, ..."""
        count = len(self.issues) + 1
        return f"I-{count:03d}"

    def get_next_update_id(self) -> str:
        """Generate next update ID: U-001, U-002, ..."""
        count = len(self.updates) + 1
        return f"U-{count:03d}"

    def get_decision_updates(self, decision_id: str) -> List[Update]:
        updates = [u for u in self.updates if u.decision_id == decision_id]
        updates.sort(key=lambda u: u.created_at, reverse=True)
        return updates

    def get_decision_assignment(self, decision_id: str) -> Optional[Assignment]:
        matches = [a for a in self.assignments if a.decision_id == decision_id]
        if not matches:
            return None
        matches.sort(key=lambda a: a.assigned_at, reverse=True)
        return matches[0]


class ParsedResult(BaseModel):
    issues: List[dict] = Field(default_factory=list)
    decisions: List[dict] = Field(default_factory=list)
    assignments: List[dict] = Field(default_factory=list)
    updates: List[dict] = Field(default_factory=list)
    risks: List[dict] = Field(default_factory=list)
    # Agent alignment: LLM tells engine which existing entities each extracted item relates to
    candidate_matches: List[dict] = Field(default_factory=list)
    governance_gaps: List[dict] = Field(default_factory=list)


class CompileIssue(BaseModel):
    decision_id: str
    decision_title: str
    issue_type: CompileIssueType
    severity: CompileSeverity
    message: str


class Alert(BaseModel):
    decision_id: str
    decision_title: str
    alert_type: AlertType
    severity: AlertSeverity
    message: str
    created_at: datetime = Field(default_factory=datetime.now)


class Escalation(BaseModel):
    decision_id: str
    decision_title: str
    message: str
    owner: Optional[str] = None


class SystemStatus(BaseModel):
    total_decisions: int = 0
    active_decisions: int = 0
    high_risk_count: int = 0
    overdue_count: int = 0
    stale_count: int = 0
    by_status: Dict[str, int] = Field(default_factory=dict)
    by_department: Dict[str, int] = Field(default_factory=dict)


def compute_priority(
    decision: "Decision",
    graph: "GovernanceGraph | None" = None,
    now: datetime | None = None,
) -> PriorityLevel:
    """Compute priority from governance state — completely rule-driven, no human input.

    P0: Governed (hasOwner+hasDeadline+hasUpdate) ∧ floor(deadline-now) ≤ 7d（即不足 8 整天） ∧ risk=HIGH
    P1: Governed (all governance checks passed)
    P2: hasOwner + hasDeadline (missing update)
    P3: hasOwner (missing deadline)
    P4: no owner (lowest priority — unowned work)
    """
    if now is None:
        now = datetime.now()

    if graph is None:
        has_update = False
    else:
        has_update = any(u.decision_id == decision.id for u in graph.updates)

    has_owner = bool(decision.owner)
    has_deadline = bool(decision.deadline)
    governed = has_owner and has_deadline and has_update

    if decision.status == "DONE":
        return "P4"

    if governed:
        if decision.deadline and (decision.deadline - now).days <= 7 and decision.risk_level == "HIGH":
            return "P0"
        return "P1"

    if has_owner and has_deadline:
        return "P2"

    if has_owner:
        return "P3"

    return "P4"


# 停滞阈值（天）——与 owl_compiler 的 STALE 告警语义保持一致
STALE_DAYS = 7


def decision_is_overdue(decision: "Decision", graph=None, now: datetime | None = None) -> bool:
    """截止日期已过且未完成（镜像 OwlRuntime.checkpoint 的 OVERDUE 语义）。"""
    if now is None:
        now = datetime.now()
    return bool(
        decision.deadline
        and decision.deadline < now
        and decision.status != "DONE"
    )


def decision_is_stale(decision: "Decision", graph=None, now: datetime | None = None) -> bool:
    """距最近一次进展更新已停滞 ≥ STALE_DAYS 天且未完成。"""
    if now is None:
        now = datetime.now()
    if decision.status == "DONE" or not decision.last_update_at:
        return False
    return (now - decision.last_update_at).days >= STALE_DAYS


def update_is_stale(update: "Update", graph=None, now: datetime | None = None) -> bool:
    """单条进展更新距报告时间已达到或超过 STALE_DAYS 天。"""
    if now is None:
        now = datetime.now()
    return (now - update.created_at).days >= STALE_DAYS


# ── 组织治理（org 词汇表驱动） ─────────────────────────────────

_ORG_CACHE: Any = None
_ARCHETYPE_WARNED = False


def _org_vocab():
    """进程级缓存加载 DSL 的 org 词汇表（DSL 在进程内不变，与 TBox 单例一致）。"""
    global _ORG_CACHE
    if _ORG_CACHE is None:
        from moss.ontorepo.loader import default_dsl_path, load_model
        _ORG_CACHE = load_model(default_dsl_path()).org
    return _ORG_CACHE


def _active_archetype_id() -> str | None:
    """工作区声明的原型 id；已声明但词汇表中不存在时警告一次（防 typo 静默降级）。"""
    global _ARCHETYPE_WARNED
    from moss.config import get_config
    archetype = get_config().archetype
    if archetype and not _ARCHETYPE_WARNED:
        org = _org_vocab()
        if org is not None and org.archetype_by_id(archetype) is None:
            valid = ", ".join(a.id for a in org.archetypes)
            print(
                f"[Org] MOSS_ARCHETYPE='{archetype}' 不在词汇表中（可用: {valid}）。"
                f"行业语义将静默失效，仅通用层生效 —— 请检查拼写（moss org list）。",
                file=sys.stderr,
            )
            _ARCHETYPE_WARNED = True
    return archetype


def workspace_archetype(decision=None, graph=None, now: datetime | None = None) -> str | None:
    """工作区声明的公司原型 id（全局常量；未声明返回 None → 不做 ABox 断言）。"""
    return _active_archetype_id()


def workspace_industry(decision=None, graph=None, now: datetime | None = None) -> str | None:
    """工作区原型所属行业（如 医药/医疗/汽车/互联网）；未声明原型返回 None。"""
    org = _org_vocab()
    arch = org.archetype_by_id(_active_archetype_id()) if org else None
    return arch.industry if arch else None


def decision_domain(decision: "Decision", graph=None, now: datetime | None = None) -> str | None:
    """决策业务域归一到词汇表标准名（含别名解析），供 hasDomain 断言。

    value(hasDomain, 标准名) 分类与对齐判定共用同一解析，避免"抽取侧用别名、
    分类侧吃原始串"的不一致。无法解析 → None（不断言，域类不触发）。
    """
    if not decision.domain:
        return None
    org = _org_vocab()
    if org is None:
        return decision.domain
    return org.resolve_domain(decision.domain, _active_archetype_id()) or None


def decision_role_unregistered(decision: "Decision", graph=None, now: datetime | None = None) -> bool:
    """决策岗位不在词汇表中（当前原型专业岗位 + 通用岗位，含别名）。

    只要求 owner_role 存在 —— 未声明业务域不影响"岗位是否注册"的判定。
    这是数据治理信号（提示补词汇表或改用标准岗位名），区别于
    decision_role_misaligned 的职责越权信号。
    """
    if not decision.owner_role:
        return False
    org = _org_vocab()
    if org is None:
        return False
    return org.find_position(decision.owner_role, _active_archetype_id()) is None


def decision_role_aligned(decision: "Decision", graph=None, now: datetime | None = None) -> bool:
    """岗位-业务域对齐判定（org 词汇表，归一化 + 别名匹配）。

    对齐 = owner_role 命中词汇表岗位（id/label/alias，归一化比较），且满足其一：
    (a) 岗位 covers_all_domains（董事长/CEO 对全域负最终责任）；
    (b) 岗位主业务域 == 决策业务域（经 resolve_domain 归一到标准域名）；
    (c) 决策业务域 ∈ 岗位职责清单。
    """
    if not decision.owner_role or not decision.domain:
        return False
    org = _org_vocab()
    if org is None:
        return False
    arch_id = _active_archetype_id()
    pos = org.find_position(decision.owner_role, arch_id)
    if pos is None:
        return False
    if pos.covers_all_domains:
        return True
    domain = org.resolve_domain(decision.domain, arch_id) or decision.domain
    pos_domain = org.resolve_domain(pos.domain, arch_id) or pos.domain
    if domain == pos_domain:
        return True
    from moss.ontorepo.model import _norm
    return any(_norm(d) == _norm(domain) for d in pos.duties)


def decision_role_misaligned(decision: "Decision", graph=None, now: datetime | None = None) -> bool:
    """岗位在词汇表中、但其职责不覆盖决策业务域 —— 职责越权信号。

    与 decision_role_unregistered（岗位未注册）互斥拆分：数据问题与职责问题
    是两种不同的治理动作。仍须独立成 computed 正向断言：misalignment 不能用
    not some(isRoleAligned) 表达——OWL 开放世界下"未断言"推不出否定，会使
    HermiT 与闭世界 fallback 漂移。
    """
    if not decision.owner_role or not decision.domain:
        return False
    if decision_role_unregistered(decision, graph, now):
        return False
    return not decision_role_aligned(decision, graph, now)


# abox_binding `computed:` 名称 → 取值函数 的唯一注册点。
# owl_ontology（HermiT ABox 断言）与 cwa（fallback 求值）共用此表，
# 消除两条推理路径对 computed 绑定的字符串特判。
COMPUTED_BINDINGS: Dict[str, Callable[..., Any]] = {
    "compute_priority": compute_priority,
    "decision_is_overdue": decision_is_overdue,
    "decision_is_stale": decision_is_stale,
    "update_is_stale": update_is_stale,
    "workspace_archetype": workspace_archetype,
    "workspace_industry": workspace_industry,
    "decision_domain": decision_domain,
    "decision_role_aligned": decision_role_aligned,
    "decision_role_misaligned": decision_role_misaligned,
    "decision_role_unregistered": decision_role_unregistered,
}
