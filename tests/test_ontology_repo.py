"""Ontology Repo 测试 —— 锁定 DSL 校验、协议符合性、以及 HermiT/fallback 语义一致。

这些测试是 OntologyOps 'Ontology Repo' 支柱的回归护栏：
  - DSL 必须合法、必须声明协议规定的 result_key
  - 参考实现必须自证符合协议（全绿）
  - 破坏本体应被符合性条款捕获
  - 无 Java 的闭世界 fallback 必须复刻定义类语义（不漂移）
"""
from datetime import datetime, timedelta

import pytest

from moss.models import Decision, GovernanceGraph, Update
from moss.ontorepo.conformance import run
from moss.ontorepo.loader import default_dsl_path, load_model


def test_dsl_validates():
    model = load_model(default_dsl_path())
    assert model.ontology.iri == "http://moss.org/ontology"
    assert {c.name for c in model.concepts} >= {"Decision", "Issue", "Update"}


def test_declares_required_result_keys():
    model = load_model(default_dsl_path())
    keys = set(model.result_keys())
    assert {"governed", "has_owner", "has_deadline", "critical", "high_priority"} <= keys


def test_conformance_all_pass():
    report = run()
    failed = [c.id for c in report.clauses if not c.passed]
    assert report.passed, f"符合性未全绿: {failed}"


def test_conformance_detects_missing_concept(tmp_path):
    import yaml
    data = yaml.safe_load(default_dsl_path().read_text(encoding="utf-8"))
    data["concepts"] = [c for c in data["concepts"] if c["name"] != "Decision"]
    bad = tmp_path / "bad.onto.yaml"
    bad.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    report = run(bad)
    assert not report.passed
    assert any(c.id == "gov-core-concept" and not c.passed for c in report.clauses)


def _graph():
    """两个决策：g 完全治理 (owner+deadline+update)，u 只有 owner。"""
    now = datetime.now()
    g = Decision(id="g", title="governed", owner="Alice",
                 deadline=now + timedelta(days=30), risk_level="LOW")
    u = Decision(id="u", title="ungoverned", owner="Bob")
    graph = GovernanceGraph()
    graph.decisions = {"g": g, "u": u}
    graph.updates = [Update(id="U-1", decision_id="g", content="progress")]
    return graph


def test_fallback_matches_defined_semantics(monkeypatch):
    """无 Java 时，DSL 驱动的闭世界 fallback 必须复刻定义类语义。"""
    import moss.owl_ontology as oo
    monkeypatch.setattr(oo, "_java_checked", True)
    monkeypatch.setattr(oo, "_java_available", False)
    monkeypatch.setattr(oo, "_onto_instance", None)

    r = oo.reason_graph(_graph())
    assert sorted(r["governed"]) == ["g"]
    assert sorted(r["has_owner"]) == ["g", "u"]
    assert sorted(r["has_deadline"]) == ["g"]


@pytest.mark.skipif(__import__("shutil").which("java") is None, reason="HermiT 需 Java")
def test_hermit_matches_fallback(monkeypatch):
    """有 Java 时，HermiT 分类必须与闭世界 fallback 一致（两条路径不漂移）。"""
    import moss.owl_ontology as oo
    graph = _graph()

    monkeypatch.setattr(oo, "_onto_instance", None)
    monkeypatch.setattr(oo, "_java_checked", True)
    monkeypatch.setattr(oo, "_java_available", True)
    hermit = {k: sorted(v) for k, v in oo.reason_graph(graph).items() if isinstance(v, list)}

    monkeypatch.setattr(oo, "_onto_instance", None)
    monkeypatch.setattr(oo, "_java_available", False)
    fallback = {k: sorted(v) for k, v in oo.reason_graph(graph).items() if isinstance(v, list)}

    assert hermit == fallback
