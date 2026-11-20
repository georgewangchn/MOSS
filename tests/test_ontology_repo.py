"""Ontology Repo 测试 —— 锁定 DSL 校验、协议符合性、以及 HermiT/fallback 语义一致。

这些测试是 OntologyOps 'Ontology Repo' 支柱的回归护栏：
  - DSL 必须合法、必须声明协议规定的 result_key
  - 参考实现必须自证符合协议（全绿）
  - 破坏本体应被符合性条款捕获
  - 无 Java 的闭世界 fallback 必须复刻定义类语义（不漂移）
"""
from datetime import datetime, timedelta

import pytest

from moss.models import Decision, GovernanceGraph, Issue, Update
from moss.ontorepo.conformance import run
from moss.ontorepo.loader import default_dsl_path, load_model


def test_dsl_validates():
    model = load_model(default_dsl_path())
    assert model.ontology.iri == "http://moss.org/ontology"
    assert {c.name for c in model.concepts} >= {"Decision", "Issue", "Update"}


def test_declares_required_result_keys():
    model = load_model(default_dsl_path())
    keys = set(model.result_keys())
    assert {
        "governed", "has_owner", "has_deadline", "critical", "high_priority",
        "high_risk", "blocked", "done", "overdue", "stale",
        "critical_issue", "stale_update", "has_dependency", "blocked_dependency",
    } <= keys


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
    """治理样本：g 完全治理；u 只有 owner；od 超期+高风险+停滞；
    blk 状态阻塞；bd 依赖 blk；fin 已完成（截止日已过但 DONE 不算超期）；
    ic 为 CRITICAL 议题；U-2 为 30 天前的停滞更新。"""
    now = datetime.now()
    g = Decision(id="g", title="governed", owner="Alice",
                 deadline=now + timedelta(days=30), risk_level="LOW",
                 last_update_at=now)
    u = Decision(id="u", title="ungoverned", owner="Bob")
    od = Decision(id="od", title="overdue high risk", owner="Carol",
                  deadline=now - timedelta(days=3), risk_level="HIGH",
                  last_update_at=now - timedelta(days=30))
    blk = Decision(id="blk", title="blocked source", owner="Dan",
                   deadline=now + timedelta(days=10), status="BLOCKED")
    bd = Decision(id="bd", title="depends on blocked", owner="Eve",
                  deadline=now + timedelta(days=10), depends_on=["blk"])
    fin = Decision(id="fin", title="finished", owner="Fay",
                   deadline=now - timedelta(days=1), status="DONE")
    graph = GovernanceGraph()
    graph.decisions = {"g": g, "u": u, "od": od, "blk": blk, "bd": bd, "fin": fin}
    graph.issues = {"ic": Issue(id="ic", title="critical issue", severity="CRITICAL")}
    graph.updates = [
        Update(id="U-1", decision_id="g", content="progress", created_at=now),
        Update(id="U-2", decision_id="od", content="old progress",
               created_at=now - timedelta(days=30)),
    ]
    return graph


def test_fallback_matches_defined_semantics(monkeypatch):
    """无 Java 时，DSL 驱动的闭世界 fallback 必须复刻定义类语义。"""
    import moss.owl_ontology as oo
    monkeypatch.setattr(oo, "_java_checked", True)
    monkeypatch.setattr(oo, "_java_available", False)
    monkeypatch.setattr(oo, "_onto_instance", None)

    r = oo.reason_graph(_graph())
    assert sorted(r["governed"]) == ["g", "od"]
    assert sorted(r["has_owner"]) == ["bd", "blk", "fin", "g", "od", "u"]
    assert sorted(r["has_deadline"]) == ["bd", "blk", "fin", "g", "od"]
    # 优先级关键类：od 为 governed ∧ 截止 ≤7d ∧ HIGH → P0；g → P1
    assert r["critical"] == ["od"]
    assert r["high_priority"] == ["g"]
    # 值约束类
    assert r["high_risk"] == ["od"]
    assert r["blocked"] == ["blk"]
    assert r["done"] == ["fin"]
    # 时间相对类（computed 布尔桥接）：fin 截止日已过但 DONE → isOverdue=False 必须不分类
    assert r["overdue"] == ["od"]
    assert "fin" not in r["overdue"]
    assert r["stale"] == ["od"]
    # Issue / Update 级分类
    assert r["critical_issue"] == ["ic"]
    assert r["stale_update"] == ["U-2"]
    # 依赖语义
    assert r["has_dependency"] == ["bd"]
    assert r["blocked_dependency"] == ["bd"]


def test_dependency_classification(monkeypatch):
    """dependsOn 对象属性：直接依赖 BLOCKED 决策 → blocked_dependency；
    悬空依赖 ID 不产生分类；依赖正常决策只进 has_dependency。"""
    import moss.owl_ontology as oo
    monkeypatch.setattr(oo, "_java_checked", True)
    monkeypatch.setattr(oo, "_java_available", False)
    monkeypatch.setattr(oo, "_onto_instance", None)

    now = datetime.now()
    blocked = Decision(id="x1", title="blocked", owner="A",
                       deadline=now + timedelta(days=5), status="BLOCKED")
    normal = Decision(id="x2", title="normal", owner="B", deadline=now + timedelta(days=5))
    dep_bad = Decision(id="x3", title="depends on blocked", owner="C",
                       deadline=now + timedelta(days=5), depends_on=["x1"])
    dep_ok = Decision(id="x4", title="depends on normal", owner="D",
                      deadline=now + timedelta(days=5), depends_on=["x2"])
    dep_ghost = Decision(id="x5", title="dangling dep", owner="E",
                         deadline=now + timedelta(days=5), depends_on=["ghost"])

    graph = GovernanceGraph()
    graph.decisions = {"x1": blocked, "x2": normal, "x3": dep_bad,
                       "x4": dep_ok, "x5": dep_ghost}

    r = oo.reason_graph(graph)
    assert sorted(r["has_dependency"]) == ["x3", "x4"]
    assert r["blocked_dependency"] == ["x3"]


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
