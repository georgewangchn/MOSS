from typing import List

from governanceops.models import CompileIssue, GovernanceGraph
from governanceops.owl_compiler import OwlCompiler


class GovernanceCompiler:
    def __init__(self):
        self._compiler = OwlCompiler()

    def compile(self, graph: GovernanceGraph) -> List[CompileIssue]:
        return self._compiler.compile(graph)
