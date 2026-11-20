"""符合性校验：判定本体是否符合 OntologyOps 'Ontology Repo' 协议。

协议契约见 ontology/PROTOCOL.md（语义级、格式无关），条款见 ontology/schema/conformance.yaml。
本模块是注册表式 check 分发器：每条 clause 声明一个 check 名 + args，run() 逐条执行，
产出机器可读的 ConformanceReport（`moss onto conformance --json` 即合规证据文件）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel

from .loader import (
    OntologyValidationError,
    default_dsl_path,
    load_raw,
    validate_schema,
    _ONTOLOGY_DIR,
)
from .model import Expr, OntologyModel

CONFORMANCE_PATH = _ONTOLOGY_DIR / "schema" / "conformance.yaml"

CHECKS: dict[str, Callable] = {}


def register(name: str):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


class ClauseResult(BaseModel):
    id: str
    profile: str = "core"
    desc: str
    passed: bool
    message: str = ""


class ConformanceReport(BaseModel):
    protocol: str
    version: str = ""
    passed: bool
    clauses: list[ClauseResult]


# ── expression walkers ────────────────────────────────────────

def _props_in_expr(e: Optional[Expr]) -> set[str]:
    if e is None:
        return set()
    out: set[str] = set()
    if e.property:
        out.add(e.property)
    for o in (e.operands or []):
        out |= _props_in_expr(o)
    if e.operand:
        out |= _props_in_expr(e.operand)
    return out


def _value_restrictions(e: Optional[Expr]) -> list[tuple[str, Any]]:
    if e is None:
        return []
    out: list[tuple[str, Any]] = []
    if e.type == "value" and e.property is not None:
        out.append((e.property, e.value))
    for o in (e.operands or []):
        out += _value_restrictions(o)
    if e.operand:
        out += _value_restrictions(e.operand)
    return out


# ── checks ─────────────────────────────────────────────────────

@register("schema_valid")
def _schema_valid(model, raw, args):
    try:
        validate_schema(raw)
        return True, "通过 JSON Schema 校验"
    except OntologyValidationError as e:
        return False, str(e).splitlines()[0]


@register("declares_result_keys")
def _declares_result_keys(model, raw, args):
    keys = model.result_keys()
    return (len(keys) > 0), (f"声明了 {len(keys)} 个 result_key" if keys else "未声明任何 result_key")


@register("has_concepts")
def _has_concepts(model, raw, args):
    required = set(args.get("required", []))
    have = {c.name for c in model.concepts}
    missing = required - have
    return (not missing), (f"缺少概念: {sorted(missing)}" if missing else f"含 {sorted(required)}")


@register("has_defined_class_equiv")
def _has_defined_class_equiv(model, raw, args):
    rk = args.get("result_key")
    props = set(args.get("properties", []))
    dc = model.defined_by_key().get(rk)
    if dc is None:
        return False, f"无 result_key={rk} 的定义类"
    used = _props_in_expr(dc.equivalent_to)
    missing = props - used
    return (not missing), (f"{dc.name} 缺属性 {sorted(missing)}" if missing else f"{dc.name} 含 {sorted(props)}")


@register("has_value_restriction")
def _has_value_restriction(model, raw, args):
    prop = args.get("property")
    values = {str(v) for v in args.get("values", [])}
    found = {str(v) for dc in model.defined_classes for (p, v) in _value_restrictions(dc.equivalent_to) if p == prop}
    missing = values - found
    return (not missing), (f"{prop} 缺值约束 {sorted(missing)}" if missing else f"{prop} 含值约束 {sorted(values)}")


@register("has_object_property")
def _has_object_property(model, raw, args):
    name = args.get("name")
    domain = args.get("domain")
    rng = args.get("range")
    for p in model.properties.object:
        if p.name != name:
            continue
        problems = []
        if domain and p.domain != domain:
            problems.append(f"domain={p.domain}")
        if rng and p.range != rng:
            problems.append(f"range={p.range}")
        return (not problems), (f"{name} 不匹配: {' '.join(problems)}" if problems else f"{name}: {p.domain}→{p.range}")
    return False, f"缺少对象属性 {name}"


@register("reasoner_result_keys")
def _reasoner_result_keys(model, raw, args):
    required = set(args.get("keys", []))
    have = set(model.result_keys())
    missing = required - have
    return (not missing), (f"缺 result_key {sorted(missing)}" if missing else f"声明了 {sorted(required)}")


@register("ttl_parses")
def _ttl_parses(model, raw, args):
    from moss.owl_ontology import _get_ontology
    from .export import export_ttl
    try:
        import rdflib
    except ImportError:
        return False, "缺少 rdflib，无法产出/校验 Turtle"
    onto = _get_ontology()
    if onto._model is None:
        onto._model = model
    if not onto._built:
        onto.build()
    with tempfile.TemporaryDirectory() as td:
        p = export_ttl(onto.onto, Path(td) / "o.ttl")
        fmt = "turtle" if p.suffix == ".ttl" else "nt"
        try:
            rdflib.Graph().parse(str(p), format=fmt)
            return True, f"产出并成功解析 {p.suffix}"
        except Exception as e:  # pragma: no cover
            return False, f"解析失败: {e}"


# ── runner ─────────────────────────────────────────────────────

def run(path: Path | None = None) -> ConformanceReport:
    spec = yaml.safe_load(CONFORMANCE_PATH.read_text(encoding="utf-8"))
    raw = load_raw(path)
    try:
        model = OntologyModel.model_validate(raw)
    except Exception:
        model = None  # 让依赖 model 的条款各自报错，schema_valid 会精确指出问题

    clauses: list[ClauseResult] = []
    all_pass = True
    for c in spec["clauses"]:
        fn = CHECKS.get(c["check"])
        if fn is None:
            passed, msg = False, f"未知 check: {c['check']}"
        elif model is None and c["check"] != "schema_valid":
            passed, msg = False, "本体模型无法解析（见 C1）"
        else:
            try:
                passed, msg = fn(model, raw, c.get("args", {}))
            except Exception as e:
                passed, msg = False, f"检查异常: {e}"
        all_pass = all_pass and passed
        clauses.append(ClauseResult(
            id=c["id"], profile=c.get("profile", "core"),
            desc=c["desc"], passed=passed, message=msg,
        ))

    return ConformanceReport(
        protocol=spec.get("protocol", ""),
        version=str(spec.get("version", "")),
        passed=all_pass,
        clauses=clauses,
    )
