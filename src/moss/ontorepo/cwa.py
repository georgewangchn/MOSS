"""闭世界表达式解释器 (Closed-World Assumption evaluator)。

当 Java/HermiT 不可用时，用它对同一份 DSL 定义类做闭世界求值，复刻 HermiT 的分类结果。
关键：逻辑结构（and/or/not/some/value）从 DSL 派生，代码里只保留"叶子取值器"
(_values / _relation_values)，取值规则由 DSL 的 abox_binding 外置。

因此 HermiT 路径（owl_ontology._define_from_model）与 fallback 路径（本模块）
共享同一份定义类逻辑 —— 改 DSL 的 defined_classes，两条路径同步改变，消除历史上
手写 _fallback_reason 与 TBox 之间的漂移。唯一残留的代码/DSL 双写点是 abox_binding
的叶子字段名（对应稳定的 Pydantic 字段）与 computed 名称（集中于
moss.models.COMPUTED_BINDINGS 注册表），不涉及逻辑结构。

与 HermiT 侧的取值对齐规则：owl_ontology._load_abox_fields 只对真值做 ABox 断言，
故本模块 _values 同样只返回真值 —— 否则 computed 的 False 布尔会被 some 误判为真。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple


def evaluate(
    expr,
    subject: Any,
    graph,
    binding,
    defined: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
    _seen: Optional[Set[Tuple[int, str]]] = None,
) -> bool:
    """对一个 DSL Expr 节点做闭世界求值。

    subject 可以是 Decision / Issue / Update 任一 Pydantic 对象；class 节点按
    subject 实际类型判定（Thing 恒真）。defined 提供 定义类名 → DefinedClass 映射，
    使 some(P, {class: X}) 的 filler 可引用定义类（递归求值其 equivalent_to，
    访问集防环）。
    """
    if now is None:
        now = datetime.now()
    if _seen is None:
        _seen = set()

    t = expr.type
    if t == "class":
        if expr.name in ("Thing", type(subject).__name__):
            return True
        # class 节点引用定义类（与 HermiT _resolve_class 对齐）：递归求值其 equivalent_to。
        # 下钻用副本集合：共享集合会被 not/or 兄弟分支污染（先 not(class X) 再 class X 时
        # 第二次查 seen 误判 False，与 HermiT 分歧）；副本同样保证 A≡B≡A 环确定性终止。
        if defined and expr.name in defined:
            key = (id(subject), expr.name)
            if key in _seen:
                return False
            return evaluate(defined[expr.name].equivalent_to, subject, graph, binding, defined, now, _seen | {key})
        return False
    if t == "and":
        return all(evaluate(o, subject, graph, binding, defined, now, _seen) for o in expr.operands)
    if t == "or":
        return any(evaluate(o, subject, graph, binding, defined, now, _seen) for o in expr.operands)
    if t == "not":
        return not evaluate(expr.operand, subject, graph, binding, defined, now, _seen)
    if t == "some":
        values = _values(expr.property, subject, graph, binding, now)
        if not values:
            return False
        f = expr.filler
        if f.class_ is None or f.class_ == "Thing":
            # 数据类型 filler（含布尔 computed）= "P 至少有一个真值"
            return True
        return any(
            _member_of_class(v, f.class_, graph, binding, defined, now, _seen)
            for v in values
        )
    if t == "value":
        vals = _values(expr.property, subject, graph, binding, now)
        return bool(vals) and vals[0] == expr.value
    raise ValueError(f"unknown expression type: {t}")


def _binding_for(prop, binding):
    b = binding.get(prop)
    if b is None:
        raise KeyError(f"abox_binding 缺少 property '{prop}' 的绑定")
    return b


def _member_of_class(target, name, graph, binding, defined, now, _seen) -> bool:
    """判定关系目标个体是否属于 filler 指名的类（概念或定义类）。"""
    key = (id(target), name)
    if key in _seen:
        return False
    _seen.add(key)
    if name == "Thing" or name == type(target).__name__:
        return True
    if defined and name in defined:
        return evaluate(defined[name].equivalent_to, target, graph, binding, defined, now, _seen)
    return False


def _values(prop, subject, graph, binding, now) -> list:
    """解析 property 在 subject 上的闭世界取值列表（只含真值）。"""
    b = _binding_for(prop, binding)
    if b.relation:
        return _relation_values(b.relation, subject, graph)
    if b.computed:
        from moss.models import COMPUTED_BINDINGS
        fn = COMPUTED_BINDINGS.get(b.computed)
        if fn is None:
            return []
        value = fn(subject, graph, now)
        return [value] if value else []
    if b.field:
        val = getattr(subject, b.field, None)
        if val is None:
            return []
        if b.transform == "isoformat" and hasattr(val, "isoformat"):
            val = val.isoformat()
        return [val] if val else []
    return []


def _relation_values(relation, subject, graph) -> list:
    """解析关系绑定在 subject 上的目标对象列表。"""
    if graph is None:
        return []
    if relation == "updates_of_decision":
        return [u for u in graph.updates if u.decision_id == subject.id]
    if relation == "depends_on":
        deps = getattr(subject, "depends_on", [])
        return [graph.decisions[dep] for dep in deps if dep in graph.decisions]
    return []
