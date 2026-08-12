from typing import List

from moss.models import CompileIssue, GovernanceGraph
from moss.owl_compiler import OwlCompiler


class GovernanceCompiler:
    def __init__(self):
        self._compiler = OwlCompiler()

    def compile(self, graph: GovernanceGraph) -> List[CompileIssue]:
        return self._compiler.compile(graph)
