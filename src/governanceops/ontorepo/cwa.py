"""闭世界表达式解释器 (Closed-World Assumption evaluator)。

当 Java/HermiT 不可用时，用它对同一份 DSL 定义类做闭世界求值，复刻 HermiT 的分类结果。
关键：逻辑结构（and/or/not/some/value）从 DSL 派生，代码里只保留"叶子取值器"
(_has_value / _accessor)，取值规则由 DSL 的 abox_binding 外置。

因此 HermiT 路径（owl_ontology._define_from_model）与 fallback 路径（本模块）
共享同一份定义类逻辑 —— 改 DSL 的 defined_classes，两条路径同步改变，消除历史上
手写 _fallback_reason 与 TBox 之间的漂移。唯一残留的代码/DSL 双写点是 abox_binding
的叶子字段名（对应稳定的 Pydantic 字段），不涉及逻辑结构。
"""
from __future__ import annotations

from typing import Any


def evaluate(expr, decision, graph, binding) -> bool:
    """对一个 DSL Expr 节点做闭世界求值（针对单个 decision）。"""
    t = expr.type
    if t == "class":
        # 我们只在 graph.decisions 上迭代 → 个体恒为 Decision；
        # 交集里的类型断言节点 (Decision / Thing) 视为 True。
        return True
    if t == "and":
        return all(evaluate(o, decision, graph, binding) for o in expr.operands)
    if t == "or":
        return any(evaluate(o, decision, graph, binding) for o in expr.operands)
    if t == "not":
        return not evaluate(expr.operand, decision, graph, binding)
    if t == "some":
        # ∃P.filler 在闭世界下 = "P 至少有一个取值"
        return _has_value(expr.property, decision, graph, binding)
    if t == "value":
        # P value v = "P 的取值等于 v"
        return _accessor(expr.property, decision, graph, binding) == expr.value
    raise ValueError(f"unknown expression type: {t}")


def _binding_for(prop, binding):
    b = binding.get(prop)
    if b is None:
        raise KeyError(f"abox_binding 缺少 property '{prop}' 的绑定")
    return b


def _has_value(prop, decision, graph, binding) -> bool:
    b = _binding_for(prop, binding)
    if b.field:
        return bool(getattr(decision, b.field, None))
    if b.relation == "updates_of_decision":
        updates = graph.updates if graph is not None else []
        return any(u.decision_id == decision.id for u in updates)
    if b.computed:
        return _accessor(prop, decision, graph, binding) is not None
    return False


def _accessor(prop, decision, graph, binding) -> Any:
    b = _binding_for(prop, binding)
    if b.computed == "compute_priority":
        from governanceops.models import compute_priority
        return compute_priority(decision, graph)
    if b.field:
        val = getattr(decision, b.field, None)
        if b.transform == "isoformat" and val is not None and hasattr(val, "isoformat"):
            return val.isoformat()
        return val
    return None
