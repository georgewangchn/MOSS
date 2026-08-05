"""编译产物导出：把 owlready2 TBox 序列化为标准 W3C 格式。

- .owl  : owlready2 原生 RDF/XML
- .ttl  : 标准 Turtle（经 rdflib）；rdflib 缺失则降级 .nt (N-Triples)
这些是"编译产物"（build artifact），非事实源；供 Protégé/GraphDB 等外部工具互操作与展示。
"""
from __future__ import annotations

from pathlib import Path


def export_owl(onto, path: Path) -> Path:
    """导出 RDF/XML (.owl)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    onto.save(file=str(path), format="rdfxml")
    return path


def export_ttl(onto, path: Path) -> Path:
    """导出标准 Turtle (.ttl)；无 rdflib 时降级为 N-Triples (.nt)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        graph = onto.world.as_rdflib_graph()
        graph.serialize(destination=str(path), format="turtle")
        return path
    except ImportError:
        nt = path.with_suffix(".nt")
        onto.save(file=str(nt), format="ntriples")
        return nt
