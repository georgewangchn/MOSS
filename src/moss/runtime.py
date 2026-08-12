from typing import List

from moss.models import Alert, Escalation, GovernanceGraph
from moss.owl_compiler import OwlRuntime


class GovernanceRuntime:
    def __init__(self, graph: GovernanceGraph):
        self.graph = graph
        self._runtime = OwlRuntime()

    def checkpoint(self) -> List[Alert]:
        return self._runtime.checkpoint(self.graph)

    def auto_escalate(self, alerts: List[Alert]) -> List[Escalation]:
        return self._runtime.auto_escalate(alerts, self.graph)

    def generate_meeting_brief(self) -> str:
        return self._runtime.generate_meeting_brief(self.graph)
