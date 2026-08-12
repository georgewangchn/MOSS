from datetime import datetime
from typing import List, Optional, cast

from moss.models import (
    Assignment,
    Decision,
    DecisionStatus,
    GovernanceGraph,
    Issue,
    IssueSeverity,
    ParsedResult,
    RiskLevel,
    Update,
)
from moss.storage import Storage


# Shared stop words for Chinese bigram matching
_STOP_WORDS = {
    "的", "了", "是", "在", "有", "和", "与", "对", "中", "为",
    "被", "不", "到", "也", "就", "要", "会", "能", "可以", "是否",
    "应该", "该", "此", "其", "需", "已", "将",
}


class GovernanceEngine:
    VALID_STATUSES = {"OPEN", "BLOCKED", "RUNNING", "DONE"}
    VALID_TRANSITIONS = {
        "OPEN": {"BLOCKED", "RUNNING", "DONE"},
        "BLOCKED": {"RUNNING", "DONE"},
        "RUNNING": {"BLOCKED", "DONE"},
        "DONE": set(),
    }

    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage()
        self.graph = self.storage.load()

    def save(self) -> None:
        self.storage.save(self.graph)

    def reload(self) -> None:
        self.graph = self.storage.load()

    def create_decision(
        self,
        title: str,
        source: str = "ingest",
        raw_statement: str = "",
        owner: str | None = None,
        department: str | None = None,
        dependency: str | None = None,
        deadline: datetime | None = None,
    ) -> Decision:
        did = self.graph.get_next_decision_id()
        decision = Decision(
            id=did,
            title=title,
            source=source,
            raw_statement=raw_statement,
            owner=owner,
            department=department,
            dependency=dependency,
            deadline=deadline,
        )
        self.graph.decisions[did] = decision
        self.save()
        return decision

    def get_decision(self, decision_id: str) -> Optional[Decision]:
        return self.graph.decisions.get(decision_id)

    def list_decisions(
        self,
        status: str | None = None,
        department: str | None = None,
        risk_level: str | None = None,
    ) -> List[Decision]:
        results = list(self.graph.decisions.values())
        if status:
            results = [d for d in results if d.status == status]
        if department:
            results = [d for d in results if d.department == department]
        if risk_level:
            results = [d for d in results if d.risk_level == risk_level]
        results.sort(key=lambda d: d.created_at, reverse=True)
        return results

    def update_decision(self, decision_id: str, **kwargs) -> Decision:
        decision = self.graph.decisions.get(decision_id)
        if decision is None:
            raise ValueError(f"Decision {decision_id} not found")
        allowed_fields = {
            "title", "owner", "department", "dependency", "deadline",
            "status", "progress", "risk_level", "issue_id",
        }
        for key, value in kwargs.items():
            if key in allowed_fields:
                setattr(decision, key, value)
        decision.last_update_at = datetime.now()
        self.save()
        return decision

    def amend_decision(self, decision_id: str, **kwargs) -> Decision:
        return self.update_decision(decision_id, **kwargs)

    def create_issue(
        self, title: str, description: str = "", severity: IssueSeverity = "MEDIUM"
    ) -> Issue:
        iid = self.graph.get_next_issue_id()
        issue = Issue(id=iid, title=title, description=description, severity=severity)
        self.graph.issues[iid] = issue
        self.save()
        return issue

    def get_issue(self, issue_id: str) -> Optional[Issue]:
        return self.graph.issues.get(issue_id)

    def link_issue_to_decision(self, issue_id: str, decision_id: str) -> None:
        if issue_id not in self.graph.issues:
            raise ValueError(f"Issue {issue_id} not found")
        if decision_id not in self.graph.decisions:
            raise ValueError(f"Decision {decision_id} not found")
        self.graph.decisions[decision_id].issue_id = issue_id
        edge_map = self.graph.edges.setdefault("issue_to_decision", {})
        edge_list = edge_map.setdefault(issue_id, [])
        if decision_id not in edge_list:
            edge_list.append(decision_id)
        self.save()

    def add_update(
        self,
        decision_id: str,
        content: str,
        progress: float | None = None,
        risk: RiskLevel | None = None,
        source: str = "cli",
    ) -> Update:
        if decision_id not in self.graph.decisions:
            raise ValueError(f"Decision {decision_id} not found")
        uid = self.graph.get_next_update_id()
        update = Update(
            id=uid,
            decision_id=decision_id,
            content=content,
            progress=progress,
            risk=risk,
            source=source,
        )
        self.graph.updates.append(update)

        decision = self.graph.decisions[decision_id]
        if progress is not None:
            decision.progress = progress
        if risk:
            decision.risk_level = risk
        decision.last_update_at = datetime.now()
        
        # Auto-transition: OPEN → RUNNING on first update
        if decision.status == "OPEN":
            decision.status = "RUNNING"
        
        edge_map = self.graph.edges.setdefault("decision_to_updates", {})
        edge_list = edge_map.setdefault(decision_id, [])
        edge_list.append(uid)

        self.save()
        return update

    def get_updates(self, decision_id: str) -> List[Update]:
        updates = [u for u in self.graph.updates if u.decision_id == decision_id]
        updates.sort(key=lambda u: u.created_at, reverse=True)
        return updates

    def assign_owner(
        self, decision_id: str, owner: str, department: str | None = None
    ) -> Assignment:
        if decision_id not in self.graph.decisions:
            raise ValueError(f"Decision {decision_id} not found")
        if department is None:
            department = self._infer_department(owner)
        assignment = Assignment(
            decision_id=decision_id, owner=owner, department=department
        )
        self.graph.assignments.append(assignment)

        decision = self.graph.decisions[decision_id]
        decision.owner = owner
        if department:
            decision.department = department
        decision.last_update_at = datetime.now()

        edge_map = self.graph.edges.setdefault("decision_to_assignment", {})
        edge_list = edge_map.setdefault(decision_id, [])
        edge_list.append(assignment.assigned_at.isoformat())

        self.save()
        return assignment

    def get_assignment(self, decision_id: str) -> Optional[Assignment]:
        matches = [a for a in self.graph.assignments if a.decision_id == decision_id]
        if not matches:
            return None
        matches.sort(key=lambda a: a.assigned_at, reverse=True)
        return matches[0]

    @staticmethod
    def _title_match_score(needle: str, haystack: str) -> float:
        """Fuzzy title matching for assignment-to-decision linking.
        Returns 0.0–1.0 where >0.4 means confident match."""
        a, b = needle.lower().strip(), haystack.lower().strip()
        if not a or not b:
            return 0.0
        # Exact match
        if a == b:
            return 1.0
        # One contains the other
        if a in b or b in a:
            return 0.85
        # Character overlap (Jaccard on character sets)
        set_a, set_b = set(a), set(b)
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        if union == 0:
            return 0.0
        return intersection / union

    @staticmethod
    def _infer_department(owner: str) -> str | None:
        dept_keywords = {
            "采购": "采购中心",
            "销售": "销售中心",
            "研发": "研发中心",
            "客服": "客服中心",
            "财务": "财务中心",
            "人力": "人力资源中心",
            "行政": "行政中心",
            "市场": "市场中心",
            "运营": "运营中心",
            "技术": "技术中心",
            "产品": "产品中心",
        }
        for keyword, dept in dept_keywords.items():
            if keyword in owner:
                return dept
        return None

    def transition_status(self, decision_id: str, new_status: str) -> Decision:
        if new_status not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status: {new_status}")
        decision = self.graph.decisions.get(decision_id)
        if decision is None:
            raise ValueError(f"Decision {decision_id} not found")
        if decision.status == new_status:
            return decision
        valid_transitions = self.VALID_TRANSITIONS.get(decision.status, set())
        if new_status not in valid_transitions:
            raise ValueError(
                f"Cannot transition from {decision.status} to {new_status}. "
                f"Valid transitions: {valid_transitions or 'none'}"
            )
        decision.status = cast(DecisionStatus, new_status)
        decision.last_update_at = datetime.now()
        if new_status == "DONE":
            decision.progress = 100.0
        self.save()
        return decision

    def should_trigger_edr(self, issue: Issue) -> dict:
        """Evaluate whether an Issue needs EDR (Enterprise Decision Runtime) deep research.

        EDR triggers when an Issue lacks sufficient evidence to form/resolve a Decision.
        Returns dict with:
          - trigger: bool
          - reason: str (why triggered or empty)
          - matched_decision_id: Optional[str] (weak match found)
        """
        reasons = []
        matched_decision_id = None

        # HIGH/CRITICAL severity → always EDR
        if issue.severity in ("HIGH", "CRITICAL"):
            reasons.append(f"severity={issue.severity}")

        # Check if any existing Decision covers this issue (strong match)
        best_score = 0.0
        for did, d in self.graph.decisions.items():
            score = self._title_match_score(issue.title, d.title)
            if score > best_score:
                best_score = score
                matched_decision_id = did

        if best_score >= 0.5:
            return {"trigger": False, "reason": "", "matched_decision_id": matched_decision_id}

        # No strong match → EDR needed
        if not reasons:
            reasons.append("no_matching_decision")

        return {
            "trigger": True,
            "reason": ", ".join(reasons),
            "matched_decision_id": matched_decision_id,
        }

    def find_decision_for_issue(self, issue_title: str, exclude_ids: Optional[set] = None) -> Optional[str]:
        """Find existing Decision related to an Issue by keyword overlap.

        Uses bigram matching: extract 2-char keywords from the issue title
        and check which Decision title contains the most keywords.
        Requires at least 2 keyword matches to consider it related.

        Args:
            issue_title: The Issue title to match against Decisions.
            exclude_ids: Optional set of Decision IDs to skip (e.g. newly
                         created in the same ingest call).
        """
        keywords = set()
        for i in range(len(issue_title) - 1):
            bigram = issue_title[i : i + 2]
            if bigram not in _STOP_WORDS:
                keywords.add(bigram)

        best_id: Optional[str] = None
        best_count = 0
        for did, d in self.graph.decisions.items():
            if exclude_ids and did in exclude_ids:
                continue
            count = sum(1 for kw in keywords if kw in d.title)
            if count > best_count:
                best_count = count
                best_id = did

        return best_id if best_count >= 2 else None

    def _dedup_issue_decision_overlap(self, issues: list[dict], decisions: list[dict]) -> list[dict]:
        """Remove Decisions that overlap with Issues from the same extract batch.

        When the agent (LLM) extracts both an Issue and a Decision for the same
        topic, the Decision is likely an error — per ontology rules, unresolved
        proposals should be Issues, not Decisions. The correct flow is:
        Issue → EDR research → (link to existing Decision OR create new Decision).

        Uses bigram overlap to detect similarity between Issue and Decision titles.
        """
        if not issues or not decisions:
            return decisions

        def bigrams(text: str) -> set[str]:
            return {
                text[i:i+2] for i in range(len(text) - 1)
                if text[i:i+2] not in _STOP_WORDS
            }

        issue_bigram_sets = [(ig, bigrams(ig.get("title", ""))) for ig in issues]

        filtered = []
        for dec in decisions:
            dec_title = dec.get("title", "")
            dec_bg = bigrams(dec_title)
            suppressed = False
            for ig, ibg in issue_bigram_sets:
                overlap = len(dec_bg & ibg)
                # ≥3 overlapping bigrams → same topic as Issue → suppress Decision
                if overlap >= 3:
                    suppressed = True
                    break
            if not suppressed:
                filtered.append(dec)

        return filtered

    def ingest_parsed_result(self, result: ParsedResult) -> dict:
        """Returns {"created": [...], "updated": [...], "edr_needs": [...]}"""
        created_decisions = []
        updated_decisions = []
        edr_needs: list[dict] = []  # Issues that need EDR

        # ── Ontology-Agent guard: suppress Decisions that overlap with Issues ──
        # If LLM extracts both an Issue and a Decision for the same topic,
        # the Decision is suppressed — per ontology, unresolved proposals are
        # Issues, and Decisions should only be created after EDR provides evidence.
        result.decisions = self._dedup_issue_decision_overlap(result.issues, result.decisions)

        issue_id_map: dict[str, str] = {}
        # ── Pre-process LLM candidate_matches for Issue→Decision linking ──
        # Agent (LLM) tells us which extracted items relate to which existing entities.
        # This is the ontology-agent collaboration: Agent determines alignment,
        # Engine applies the link with guard checks.
        issue_match_map: dict[str, str] = {}  # issue_title → existing_decision_id
        for cm in result.candidate_matches:
            extracted_title = cm.get("extracted_title", "")
            existing_id = cm.get("existing_id", "")
            relation = cm.get("relation", "")
            if existing_id and relation in ("related", "update"):
                # Verify the target decision actually exists
                if existing_id in self.graph.decisions:
                    issue_match_map[extracted_title] = existing_id

        for issue_data in result.issues:
            issue = self.create_issue(
                title=issue_data.get("title") or "Untitled Issue",
                description=issue_data.get("description") or "",
                severity=issue_data.get("severity") or "MEDIUM",
            )
            issue_id_map[issue.title] = issue.id
            # Evaluate EDR trigger for each newly created Issue
            edr_eval = self.should_trigger_edr(issue)
            # Prefer LLM candidate_match over engine's post-hoc matching
            llm_matched = issue_match_map.get(issue.title)
            if edr_eval["trigger"]:
                edr_needs.append({
                    "issue_id": issue.id,
                    "issue_title": issue.title,
                    "issue_severity": issue.severity,
                    "reason": edr_eval["reason"],
                    "weak_match_id": llm_matched or edr_eval.get("matched_decision_id"),
                })
            elif llm_matched:
                # LLM says it relates to existing decision, link now
                self.link_issue_to_decision(issue.id, llm_matched)

        for dec_data in result.decisions:
            title = dec_data.get("title") or "Untitled Decision"
            # Skip if a decision with similar title already exists
            existing = False
            for d in self.graph.decisions.values():
                if self._title_match_score(title, d.title) >= 0.7:
                    existing = True
                    break
            if existing:
                continue
            d = self.create_decision(
                title=title,
                source="ingest",
                raw_statement=dec_data.get("raw_statement") or "",
            )
            created_decisions.append(d)

        for issue_title, issue_id in issue_id_map.items():
            for d in created_decisions:
                if issue_title.lower() in d.title.lower() or d.title.lower() in issue_title.lower():
                    d.issue_id = issue_id
                    edge_map = self.graph.edges.setdefault("issue_to_decision", {})
                    edge_list = edge_map.setdefault(issue_id, [])
                    if d.id not in edge_list:
                        edge_list.append(d.id)

        for a in result.assignments:
            decision_title = a.get("decision_title") or ""
            owner = a.get("owner")
            deadline_str = a.get("deadline")
            if not owner:
                continue

            # Match to newly created decisions OR existing decisions by title
            # Use fuzzy matching: exact match → substring → character overlap
            matched = False
            all_decisions = {**self.graph.decisions, **{d.id: d for d in created_decisions}}

            # Sort by match quality: prefer exact, then fuzzy
            candidates: list[tuple[float, Decision]] = []
            for d in all_decisions.values():
                score = self._title_match_score(decision_title, d.title)
                if score > 0:
                    candidates.append((score, d))
            candidates.sort(key=lambda x: x[0], reverse=True)

            if candidates and candidates[0][0] >= 0.4:
                d = candidates[0][1]
                was_new = d.id in {nd.id for nd in created_decisions}
                self.assign_owner(d.id, owner, a.get("department"))
                if a.get("dependency"):
                    d.dependency = a["dependency"]
                if deadline_str:
                    try:
                        parsed = datetime.fromisoformat(deadline_str)
                    except (ValueError, TypeError):
                        parsed = None
                    if parsed:
                        d.deadline = parsed
                matched = True
                if not was_new:
                    updated_decisions.append(d)

            if not matched and decision_title:
                # Create a new tracked decision from the assignment
                d = self.create_decision(
                    title=decision_title,
                    source="ingest",
                    raw_statement=a.get("raw_statement") or "",
                    owner=owner,
                    department=a.get("department") or None,
                    dependency=a.get("dependency") or None,
                )
                if deadline_str:
                    try:
                        d.deadline = datetime.fromisoformat(deadline_str)
                    except (ValueError, TypeError):
                        pass
                created_decisions.append(d)

        self.save()
        return {"created": created_decisions, "updated": updated_decisions, "edr_needs": edr_needs}

    def get_stats(self) -> dict:
        now = datetime.now()
        total = len(self.graph.decisions)
        active = sum(1 for d in self.graph.decisions.values() if d.status != "DONE")
        high_risk = sum(
            1 for d in self.graph.decisions.values() if d.risk_level == "HIGH"
        )
        overdue = sum(
            1
            for d in self.graph.decisions.values()
            if d.deadline and d.deadline < now and d.status != "DONE"
        )
        stale = sum(
            1
            for d in self.graph.decisions.values()
            if d.status != "DONE"
            and d.last_update_at
            and (now - d.last_update_at).days >= 7
        )

        by_status: dict[str, int] = {}
        for d in self.graph.decisions.values():
            by_status[d.status] = by_status.get(d.status, 0) + 1

        by_department: dict[str, int] = {}
        for d in self.graph.decisions.values():
            dept = d.department or "Unassigned"
            by_department[dept] = by_department.get(dept, 0) + 1

        by_priority: dict[str, int] = {}
        from moss.models import compute_priority
        for d in self.graph.decisions.values():
            p = compute_priority(d, self.graph, now)
            by_priority[p] = by_priority.get(p, 0) + 1

        return {
            "total_decisions": total,
            "active_decisions": active,
            "high_risk_count": high_risk,
            "overdue_count": overdue,
            "stale_count": stale,
            "by_status": by_status,
            "by_department": by_department,
            "by_priority": by_priority,
        }
