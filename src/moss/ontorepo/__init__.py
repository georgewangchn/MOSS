"""OntologyOps 'Ontology Repo' 支柱在 MOSS 中的实现。

把本体（TBox：类/属性/定义类）从硬编码 Python 外置为声明式 DSL 的唯一事实源，
并提供版本化治理（validate / build / diff / tag / rollback / conformance）。

模块：
  model        — DSL 的 Pydantic 数据模型
  loader       — 加载 YAML + JSON Schema 校验 → OntologyModel
  export       — 编译产物导出（.owl / .ttl）              [Phase B]
  cwa          — 闭世界表达式解释器（消除 fallback 漂移）   [Phase C]
  store        — 本体版本仓库（SQLite onto_versions/tags）  [Phase D]
  diff         — 语义级本体 Diff                            [Phase D]
  conformance  — 符合性条款校验                             [Phase E]
"""
