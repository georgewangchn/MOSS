from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

DecisionStatus = Literal["OPEN", "BLOCKED", "RUNNING", "DONE"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
PriorityLevel = Literal["P0", "P1", "P2", "P3", "P4"]
IssueSeverity = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
CompileSeverity = Literal["ERROR", "WARNING", "OK"]
AlertSeverity = Literal["HIGH", "MEDIUM", "LOW"]
CompileIssueType = Literal["MISSING_OWNER", "MISSING_DEADLINE", "MISSING_DEPENDENCY", "MISSING_UPDATE", "STALE", "OK"]
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

    P0: Governed (hasOwner+hasDeadline+hasUpdate) ∧ deadline < 7d ∧ risk=HIGH
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


# abox_binding `computed:` 名称 → 取值函数 的唯一注册点。
# owl_ontology（HermiT ABox 断言）与 cwa（fallback 求值）共用此表，
# 消除两条推理路径对 computed 绑定的字符串特判。
COMPUTED_BINDINGS: Dict[str, Callable[..., Any]] = {
    "compute_priority": compute_priority,
    "decision_is_overdue": decision_is_overdue,
    "decision_is_stale": decision_is_stale,
    "update_is_stale": update_is_stale,
}
