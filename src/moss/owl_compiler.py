from datetime import datetime
from typing import List

from moss.models import Alert, CompileIssue, Escalation, GovernanceGraph
from moss.owl_ontology import reason_graph


class OwlCompiler:
    def compile(self, graph: GovernanceGraph) -> List[CompileIssue]:
        classified = reason_graph(graph)
        results: List[CompileIssue] = []

        for did, decision in graph.decisions.items():
            if decision.status == "DONE":
                results.append(CompileIssue(
                    decision_id=did, decision_title=decision.title,
                    issue_type="OK", severity="OK",
                    message="已完成",
                ))
                continue

            if did not in classified["has_owner"]:
                results.append(CompileIssue(
                    decision_id=did, decision_title=decision.title,
                    issue_type="MISSING_OWNER", severity="ERROR",
                    message="缺失负责人 — 无法追踪执行",
                ))

            if did not in classified["has_deadline"]:
                results.append(CompileIssue(
                    decision_id=did, decision_title=decision.title,
                    issue_type="MISSING_DEADLINE", severity="WARNING",
                    message="缺失截止日期 — 无时间约束",
                ))

            if not decision.dependency:
                results.append(CompileIssue(
                    decision_id=did, decision_title=decision.title,
                    issue_type="MISSING_DEPENDENCY", severity="WARNING",
                    message="缺失协同部门 — 跨部门协作不可见",
                ))

            updates = [u for u in graph.updates if u.decision_id == did]
            if not updates:
                results.append(CompileIssue(
                    decision_id=did, decision_title=decision.title,
                    issue_type="MISSING_UPDATE", severity="WARNING",
                    message="无进展更新 — 可能已停滞",
                ))

            if updates and decision.last_update_at:
                days_since = (datetime.now() - decision.last_update_at).days
                if days_since >= 7:
                    results.append(CompileIssue(
                        decision_id=did, decision_title=decision.title,
                        issue_type="STALE", severity="WARNING",
                        message=f"已停滞 {days_since} 天",
                    ))

            if did in classified["governed"]:
                if not any(r.decision_id == did and r.severity != "OK" for r in results):
                    results.append(CompileIssue(
                        decision_id=did, decision_title=decision.title,
                    issue_type="OK", severity="OK",
                    message="所有治理检查通过 [HermiT: GovernedDecision]",
                ))
            else:
                if not any(r.decision_id == did for r in results):
                    results.append(CompileIssue(
                        decision_id=did, decision_title=decision.title,
                        issue_type="OK", severity="OK",
                        message="所有治理检查通过",
                    ))

        return results


class OwlRuntime:
    def checkpoint(self, graph: GovernanceGraph) -> List[Alert]:
        alerts: List[Alert] = []
        now = datetime.now()

        for did, decision in graph.decisions.items():
            if decision.status == "DONE":
                continue

            if decision.deadline and decision.deadline < now:
                days_overdue = (now - decision.deadline).days
                alerts.append(Alert(
                    decision_id=did, decision_title=decision.title,
                    alert_type="OVERDUE", severity="HIGH",
                    message=f"截止日期已超期 {days_overdue} 天",
                ))

            if decision.risk_level == "HIGH":
                alerts.append(Alert(
                    decision_id=did, decision_title=decision.title,
                    alert_type="HIGH_RISK", severity="HIGH",
                    message="标记为高风险",
                ))

            if decision.last_update_at:
                days_since_update = (now - decision.last_update_at).days
                if days_since_update >= 7:
                    alerts.append(Alert(
                        decision_id=did, decision_title=decision.title,
                        alert_type="STALE", severity="MEDIUM",
                        message=f"已停滞 {days_since_update} 天无更新",
                    ))
            elif (now - decision.created_at).days >= 3:
                alerts.append(Alert(
                    decision_id=did, decision_title=decision.title,
                    alert_type="NO_PROGRESS", severity="MEDIUM",
                    message="创建后从未更新",
                ))

            if decision.status == "BLOCKED":
                alerts.append(Alert(
                    decision_id=did, decision_title=decision.title,
                    alert_type="BLOCKED", severity="HIGH",
                    message="已被阻塞，需要处理",
                ))

        return alerts

    def auto_escalate(self, alerts: List[Alert], graph: GovernanceGraph) -> List[Escalation]:
        escalations: List[Escalation] = []
        for alert in alerts:
            if alert.severity == "HIGH":
                decision = graph.decisions.get(alert.decision_id)
                escalations.append(Escalation(
                    decision_id=alert.decision_id,
                    decision_title=alert.decision_title,
                    message=alert.message,
                    owner=decision.owner if decision else None,
                ))
        return escalations

    def generate_meeting_brief(self, graph: GovernanceGraph) -> str:
        compiler = OwlCompiler()
        compile_results = compiler.compile(graph)
        lines = []

        decision_status: dict[str, tuple[str, str]] = {}
        for r in compile_results:
            if r.decision_id not in decision_status:
                decision_status[r.decision_id] = (r.decision_title, "OK")
            current = decision_status[r.decision_id][1]
            if r.issue_type == "OK" and current != "ERROR" and current != "WARNING":
                new_status = "OK"
            elif r.severity == "ERROR":
                new_status = "ERROR"
            elif r.severity == "WARNING" and current != "ERROR":
                new_status = "WARNING"
            else:
                continue
            decision_status[r.decision_id] = (r.decision_title, new_status)

        for did in sorted(decision_status.keys()):
            title, status = decision_status[did]
            if status == "OK":
                lines.append(f"{did} {title} - ON TRACK ✓")
            elif status == "ERROR":
                lines.append(f"{did} {title} - NO PROGRESS ⚠️")
            else:
                lines.append(f"{did} {title} - AT RISK ⚠️")

        total = len(decision_status)
        at_risk = sum(1 for _, s in decision_status.values() if s != "OK")

        if at_risk > 0:
            lines.append(f"\n{at_risk}/{total} decisions are at risk or stalled.")
        else:
            lines.append(f"\nAll {total} decisions are on track.")

        return "\n".join(lines)
