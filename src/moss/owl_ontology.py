"""
OWL Ontology for MOSS — owlready2 + HermiT reasoning engine.

Architecture:
  - Singleton GovernanceOntology: class hierarchy built once,
    individuals cleared and reloaded per reason_graph() call.
  - Retry-with-rebuild: if HermiT reasoning fails (e.g., from
    inferred quad store pollution), the ontology is rebuilt from
    scratch and retried once before falling back to pure-Python checks.

This is the core differentiator of MOSS:
  HermiT does OWL2 DL classification; Python does closed-world checks.
"""

import operator
import shutil
import subprocess
import sys
from datetime import datetime
import types as _types
from functools import reduce
from typing import Any, Dict, Optional

from owlready2 import (
    DataProperty,
    FunctionalProperty,
    Not,
    ObjectProperty,
    Thing,
    destroy_entity,
    get_ontology,
    sync_reasoner_hermit,
)

MOSS_IRI = "http://moss.org/ontology"

# DSL datatype 名 → Python 类型（编译 DataProperty range 用）
_DATATYPE = {"string": str, "float": float, "integer": int, "boolean": bool}

_onto_instance: Optional["GovernanceOntology"] = None
_hermit_failures: int = 0
_hermit_successes: int = 0
_java_checked: bool = False
_java_available: bool = False


def _check_java() -> bool:
    """Check if Java runtime is available (required for HermiT reasoner).

    Cached after first check.
    """
    global _java_checked, _java_available
    if _java_checked:
        return _java_available
    _java_checked = True
    java_path = shutil.which("java")
    if java_path:
        try:
            result = subprocess.run(
                [java_path, "-version"],
                capture_output=True, timeout=10,
            )
            _java_available = result.returncode == 0
        except Exception:
            _java_available = False
    return _java_available


def get_reasoner_mode() -> str:
    """Return current reasoner mode for display."""
    if _check_java():
        return "HermiT (OWL 2 DL)"
    return "Python-Fallback"


class GovernanceOntology:
    """Singleton OWL ontology for governance decisions.

    Uses in-memory SQLite backend. Class hierarchy is defined once;
    individuals are cleared and reloaded per graph.
    """

    def __init__(self):
        self.onto = get_ontology(MOSS_IRI)
        self._built = False
        self._individuals: Dict[str, Any] = {}
        self._model = None                       # cached OntologyModel (from DSL)
        # populated by _define_from_model:
        self.classes: Dict[str, Any] = {}
        self.properties: Dict[str, Any] = {}
        self.prop_domains: Dict[str, str] = {}   # property name → DSL domain (ABox 装载按概念过滤)
        self.defined: Dict[str, Any] = {}        # class name → owlready2 class
        self.result_keys: Dict[str, str] = {}    # result_key → defined class name

    # ── Lifecycle ─────────────────────────────────────────────

    def build(self) -> None:
        """Build OWL class/property hierarchy from the DSL. Idempotent.

        The TBox (concepts / properties / defined classes) is compiled from
        the declarative ontology DSL (ontology/moss.onto.yaml) — the single
        source of truth — rather than hardcoded Python.
        """
        if self._built:
            return
        if self._model is None:
            from moss.ontorepo.loader import default_dsl_path, load_model
            self._model = load_model(default_dsl_path())
        self._define_from_model(self._model)
        self._built = True

    def rebuild(self) -> None:
        """Force full rebuild — used when HermiT corrupts the world."""
        self._individuals.clear()
        try:
            self.onto.destroy()
        except Exception:
            pass
        self.onto = get_ontology(MOSS_IRI)
        self._built = False
        self.build()

    def clear_individuals(self) -> None:
        """Destroy all individuals while preserving class hierarchy."""
        for ind in list(self._individuals.values()):
            try:
                destroy_entity(ind)
            except Exception:
                pass
        self._individuals.clear()

    # ── OWL Definition (compiled from DSL) ────────────────────

    def _define_from_model(self, model) -> None:
        """Compile the DSL OntologyModel into an owlready2 TBox.

        Dynamically creates concepts / properties / defined classes so the
        ontology content lives in the DSL, not in Python. Populates
        self.classes / self.properties / self.defined / self.result_keys and
        back-compat attributes (self.Decision, self.GovernedDecision, …) so
        load_graph()/reason() keep working.
        """
        onto = self.onto
        self.classes = {"Thing": Thing}
        self.properties = {}
        self.prop_domains = {}
        self.defined = {}
        self.result_keys = {}

        with onto:
            # concepts
            for c in model.concepts:
                base = self.classes.get(c.parent, Thing)
                self.classes[c.name] = _types.new_class(c.name, (base,))

            # data properties
            for p in model.properties.data:
                bases = (DataProperty, FunctionalProperty) if p.functional else (DataProperty,)
                prop = _types.new_class(p.name, bases)
                prop.domain = [Thing] if p.domain == "Thing" else [self.classes[p.domain]]
                prop.range = [_DATATYPE[p.range]]
                self.properties[p.name] = prop
                self.prop_domains[p.name] = p.domain

            # object properties
            for p in model.properties.object:
                bases = (ObjectProperty, FunctionalProperty) if p.functional else (ObjectProperty,)
                prop = _types.new_class(p.name, bases)
                prop.domain = [Thing] if p.domain == "Thing" else [self.classes[p.domain]]
                prop.range = [Thing] if p.range == "Thing" else [self.classes[p.range]]
                self.properties[p.name] = prop
                self.prop_domains[p.name] = p.domain

            # defined classes (HermiT classification targets)
            for dc in model.defined_classes:
                base = self.classes.get(dc.parent, Thing)
                cls = _types.new_class(dc.name, (base,))
                cls.equivalent_to.append(self._build_expr(dc.equivalent_to))
                self.defined[dc.name] = cls
                if dc.result_key:
                    self.result_keys[dc.result_key] = dc.name

        # back-compat attribute access
        self.Decision = self.classes["Decision"]
        self.Issue = self.classes["Issue"]
        self.Update = self.classes["Update"]
        for name, cls in self.defined.items():
            setattr(self, name, cls)

    def _resolve_class(self, name: str):
        """Resolve a class name in an expression — concept or defined class.

        Defined classes are legal fillers (e.g. some(dependsOn, BlockedDecision)),
        so both maps are searched.
        """
        if name == "Thing":
            return Thing
        if name in self.classes:
            return self.classes[name]
        if name in self.defined:
            return self.defined[name]
        raise KeyError(f"unknown class referenced in expression: {name}")

    def _build_expr(self, e):
        """Recursively build an owlready2 class expression from a DSL Expr node."""
        t = e.type
        if t == "class":
            return self._resolve_class(e.name)
        if t == "and":
            return reduce(operator.and_, [self._build_expr(o) for o in e.operands])
        if t == "or":
            return reduce(operator.or_, [self._build_expr(o) for o in e.operands])
        if t == "not":
            return Not(self._build_expr(e.operand))
        prop = self.properties[e.property]
        if t == "some":
            f = e.filler
            if f.datatype:
                filler = _DATATYPE[f.datatype]
            else:
                filler = self._resolve_class(f.class_)
            return prop.some(filler)
        if t == "value":
            return prop.value(e.value)
        raise ValueError(f"unknown expression type: {t}")

    # ── Graph Loading ─────────────────────────────────────────

    def load_graph(self, graph) -> None:
        """Load governance graph into ontology as OWL individuals.

        Clears previous individuals first, then creates fresh ones.
        ABox mapping (which Python field → which OWL property) is derived
        from the DSL's abox_binding, not hardcoded — so changes to the DSL
        binding automatically flow through here. This eliminates the last
        drift source between the HermiT path and the fallback path.
        """
        self.build()
        self.clear_individuals()

        D = self.Decision
        I = self.Issue
        U = self.Update

        binding = self._model.abox_binding
        now = datetime.now()

        with self.onto:
            # Pass 1: create ALL individuals and assert data-property values,
            # so pass 2 never hits a forward reference.
            for did, decision in graph.decisions.items():
                ind = D(did)
                self._individuals[did] = ind
                self._load_abox_fields(ind, decision, "Decision", graph, binding, now)

            for update in graph.updates:
                ind = U(update.id)
                self._individuals[update.id] = ind
                self._load_abox_fields(ind, update, "Update", graph, binding, now)

            for iid, issue in graph.issues.items():
                ind = I(iid)
                self._individuals[iid] = ind
                self._load_abox_fields(ind, issue, "Issue", graph, binding, now)

            # Pass 2: wire object-property links (relation bindings).
            for update in graph.updates:
                if update.decision_id in self._individuals:
                    self._individuals[update.decision_id].hasUpdate.append(
                        self._individuals[update.id]
                    )
            for did, decision in graph.decisions.items():
                ind = self._individuals[did]
                for dep_id in decision.depends_on:
                    target = self._individuals.get(dep_id)
                    if target is not None:
                        ind.dependsOn.append(target)

    def _load_abox_fields(self, ind, obj, kind: str, graph, binding, now) -> None:
        """Set DataProperty values on an individual from abox_binding.

        Mirrors cwa.py's accessor logic so HermiT and fallback paths share
        one ABox mapping source. Bindings whose property domain does not
        cover the individual's concept (or Thing) are skipped. Only truthy
        values are asserted — cwa mirrors this with bool() so a False
        computed boolean classifies identically on both paths.
        """
        from moss.models import COMPUTED_BINDINGS

        for prop_name, b in binding.items():
            if b.relation:
                continue

            prop = self.properties.get(prop_name)
            if prop is None:
                continue

            domain = self.prop_domains.get(prop_name, "Thing")
            if domain not in ("Thing", kind):
                continue

            if b.computed:
                fn = COMPUTED_BINDINGS.get(b.computed)
                if fn is None:
                    continue
                value = fn(obj, graph, now)
                if value:
                    setattr(ind, prop_name, value)
                continue

            if b.field:
                if kind == "Update" and prop_name == "hasTitle":
                    # Update 无 title 字段：hasTitle 取 content 前 80 字（既有 ABox 行为）
                    value = obj.content[:80]
                else:
                    value = getattr(obj, b.field, None)
                if value is None:
                    continue
                if b.transform == "isoformat" and hasattr(value, "isoformat"):
                    value = value.isoformat()
                if value:
                    setattr(ind, prop_name, value)

    # ── Reasoning ─────────────────────────────────────────────

    def reason(self) -> Dict[str, Any]:
        """Run HermiT reasoner and collect classification results.

        Result keys are derived from the DSL (each defined class' result_key),
        so adding/removing a defined class in the DSL automatically flows through
        here — no hardcoded key list. `all_decisions` is always present.
        Individuals of every concept (Decision / Issue / Update) are
        classified; each key's list stays homogeneous because the defined
        class's parent fixes which concept it can match.
        """
        with self.onto:
            sync_reasoner_hermit(infer_property_values=True, debug=0)

        results: Dict[str, Any] = {rk: [] for rk in self.result_keys}
        results["all_decisions"] = []

        for iid, ind in self._individuals.items():
            if isinstance(ind, self.Decision):
                results["all_decisions"].append(iid)
            for rk, cls_name in self.result_keys.items():
                if isinstance(ind, self.defined[cls_name]):
                    results[rk].append(iid)

        return results


# ── Singleton Access ──────────────────────────────────────────

def _get_ontology() -> GovernanceOntology:
    """Get or create the singleton ontology instance."""
    global _onto_instance
    if _onto_instance is None:
        _onto_instance = GovernanceOntology()
    return _onto_instance


# ── Public API ────────────────────────────────────────────────

def reason_graph(graph) -> Dict[str, Any]:
    """Reason over a governance graph using HermiT Tableau reasoner.

    Strategy:
      0. Pre-check: if Java is not available, skip HermiT entirely → pure-Python fallback
      1. First attempt with cached ontology
      2. On failure: rebuild ontology from scratch, retry once
      3. Both fail: fall back to pure-Python field checks

    Uses in-memory SQLite backend — no file corruption, fast rebuilds.
    """
    global _hermit_failures, _hermit_successes

    # Pre-check: skip HermiT entirely if Java is not available
    if not _check_java():
        if _hermit_failures == 0:
            print(
                "[Reasoner] Java not found — using Python-Fallback mode. "
                "Install Java (JDK 11+) for full HermiT OWL 2 DL reasoning.",
                file=sys.stderr,
            )
        _hermit_failures += 1
        return _fallback_reason(graph)

    onto = _get_ontology()

    # Attempt 1
    result = _try_reason(onto, graph)
    if result is not None:
        _hermit_successes += 1
        return result

    # Attempt 2: rebuild and retry
    print("[HermiT] Rebuilding ontology for retry...", file=sys.stderr)
    _hermit_failures += 1
    onto.rebuild()

    result = _try_reason(onto, graph)
    if result is not None:
        _hermit_successes += 1
        return result

    # Both attempts failed — fallback
    _hermit_failures += 1
    print(
        f"[HermiT] Reasoner unavailable after 2 attempts "
        f"(failures: {_hermit_failures}, successes: {_hermit_successes}). "
        f"Using fallback field checks.",
        file=sys.stderr,
    )
    return _fallback_reason(graph)


def _try_reason(onto: GovernanceOntology, graph) -> Optional[Dict[str, Any]]:
    """Attempt one reasoning cycle. Returns None on any failure."""
    try:
        onto.load_graph(graph)
        return onto.reason()
    except Exception as e:
        print(f"[HermiT] Reasoning failed: {e}", file=sys.stderr)
        return None


def _fallback_reason(graph) -> Dict[str, Any]:
    """Pure-Python governance checks when HermiT is unavailable.

    Not a degradation — these are the same closed-world checks that complement
    HermiT's open-world classification. The classification LOGIC is derived from
    the SAME DSL defined-classes that HermiT uses (via the cwa evaluator), so this
    path can no longer drift from the TBox: change a defined class in the DSL and
    both HermiT and this fallback change together.

    Each defined class is only evaluated against subjects of its parent concept
    (mirroring OWL subsumption), so Decision classes never touch Issue/Update
    objects and vice versa.
    """
    from datetime import datetime as _dt

    from moss.ontorepo.cwa import evaluate
    from moss.ontorepo.loader import default_dsl_path, load_model

    onto = _get_ontology()
    if onto._model is None:
        onto._model = load_model(default_dsl_path())
    model = onto._model
    binding = model.abox_binding
    now = _dt.now()
    defined = {dc.name: dc for dc in model.defined_classes}

    results: Dict[str, Any] = {dc.result_key: [] for dc in model.defined_classes if dc.result_key}
    results["all_decisions"] = list(graph.decisions.keys())

    pools = {
        "Decision": [(did, d) for did, d in graph.decisions.items()],
        "Issue": [(iid, i) for iid, i in graph.issues.items()],
        "Update": [(u.id, u) for u in graph.updates],
    }

    for dc in model.defined_classes:
        if not dc.result_key:
            continue
        for oid, obj in pools.get(dc.parent, []):
            if evaluate(dc.equivalent_to, obj, graph, binding, defined=defined, now=now):
                results[dc.result_key].append(oid)

    return results


def reasoner_stats() -> Dict[str, Any]:
    """Return HermiT reasoner health statistics."""
    return {
        "successes": _hermit_successes,
        "failures": _hermit_failures,
        "java_available": _check_java(),
        "reasoner_mode": get_reasoner_mode(),
    }
