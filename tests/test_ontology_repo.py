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
        "domain_quality", "domain_compliance", "pharma_gmp_production",
        "hospital_medical_affairs", "battery_safety", "internet_med_content_compliance",
        "role_aligned", "role_misaligned", "role_unregistered",
    } <= keys


def test_org_vocabulary_layered():
    """org 段：通用岗位层 + 4 原型分层；别名/归一化/跨域覆盖解析正确。"""
    model = load_model(default_dsl_path())
    org = model.org
    assert org is not None
    assert {p.id for p in org.universal_positions} >= {"ceo", "cfo", "cho", "cto", "coo"}
    assert {a.id for a in org.archetypes} == {"pharma", "hospital", "battery", "internet-med"}
    # 岗位可按 id/label/别名 跨层查找：原型岗位优先于通用岗位
    assert org.find_position("首席财务官").domain == "财务"
    assert org.find_position("财务总监").id == "cfo"          # 别名命中
    assert org.find_position("首席财务官（分管财务）").id == "cfo"  # 归一化剥括号注释
    assert org.find_position("质量负责人", "pharma").duties    # 药厂质量负责人
    assert org.find_position("质量受权人", "pharma").id == "qa-head"
    assert org.find_position("质量负责人") is None            # 通用层无此岗位
    # 业务域别名解析归一
    assert org.resolve_domain("预算") == "财务"
    assert org.resolve_domain("GMP", "pharma") == "GMP生产"
    assert org.resolve_domain("不存在的域", "pharma") is None
    # 高管跨域：董事长/CEO 对全域负最终责任
    ceo = next(p for p in org.universal_positions if p.id == "ceo")
    chairman = next(p for p in org.universal_positions if p.id == "chairman")
    assert ceo.covers_all_domains and chairman.covers_all_domains
    # 数据守卫：别名不得在同一解析作用域内指向不同岗位（否则静默劫持匹配优先级）
    def _scope_names(positions):
        seen = {}
        for p in positions:
            for n in (p.id, p.label, *p.aliases):
                key = n.strip().lower()
                assert seen.get(key, p.id) == p.id, f"岗位别名撞名: {n} → {p.id} 与 {seen.get(key)}"
                seen[key] = p.id
    _scope_names(org.universal_positions)
    for a in org.archetypes:
        _scope_names(a.positions)

    def _check_domain_aliases(amap, scope):
        seen = {}
        for std, aliases in amap.items():
            for alias in aliases:
                key = alias.strip().lower()
                assert seen.get(key, std) == std, f"域别名撞名({scope}): {alias} → {std} 与 {seen.get(key)}"
                seen[key] = std
    _check_domain_aliases(org.domain_aliases, "universal")
    for a in org.archetypes:
        _check_domain_aliases(a.domain_aliases, a.id)
    # 每个原型都有行业与岗位
    for a in org.archetypes:
        assert a.industry and a.positions


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
    """治理样本：g 完全治理（CFO 管财务域 → 对齐）；u 只有 owner；od 超期+高风险+停滞
    （CFO 却管生产域 → 错配）；blk 状态阻塞；bd 依赖 blk；fin 已完成；
    ic 为 CRITICAL 议题；U-2 为 30 天前的停滞更新。"""
    now = datetime.now()
    g = Decision(id="g", title="governed", owner="Alice",
                 deadline=now + timedelta(days=30), risk_level="LOW",
                 last_update_at=now,
                 owner_role="首席财务官", domain="财务")
    u = Decision(id="u", title="ungoverned", owner="Bob")
    od = Decision(id="od", title="overdue high risk", owner="Carol",
                  deadline=now - timedelta(days=3), risk_level="HIGH",
                  last_update_at=now - timedelta(days=30),
                  owner_role="首席财务官", domain="生产")
    gh = Decision(id="gh", title="unregistered role", owner="Grace",
                 owner_role="外部顾问", domain="财务",
                 deadline=now + timedelta(days=10))
    blk = Decision(id="blk", title="blocked source", owner="Dan",
                   deadline=now + timedelta(days=10), status="BLOCKED")
    bd = Decision(id="bd", title="depends on blocked", owner="Eve",
                  deadline=now + timedelta(days=10), depends_on=["blk"])
    fin = Decision(id="fin", title="finished", owner="Fay",
                   deadline=now - timedelta(days=1), status="DONE")
    graph = GovernanceGraph()
    graph.decisions = {"g": g, "u": u, "od": od, "gh": gh, "blk": blk, "bd": bd, "fin": fin}
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
    assert sorted(r["has_owner"]) == ["bd", "blk", "fin", "g", "gh", "od", "u"]
    assert sorted(r["has_deadline"]) == ["bd", "blk", "fin", "g", "gh", "od"]
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
    # 组织对齐三分类（无 MOSS_ARCHETYPE → 通用层词汇）：
    # CFO↔财务 对齐；CFO↔生产 错配（职责越权）；外部顾问 未注册（数据治理）
    assert r["role_aligned"] == ["g"]
    assert r["role_misaligned"] == ["od"]
    assert r["role_unregistered"] == ["gh"]
    # 未声明原型 → 行业特化类静默（双路径一致）
    assert r["pharma_gmp_production"] == []
    assert r["battery_safety"] == []


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
def test_hermit_parity_across_archetype_switch(monkeypatch):
    """同进程切换 MOSS_ARCHETYPE 后重置推理单例：world 残留旧断言不得污染 HermiT。

    回归：通用模式下断言过的 isRoleUnregistered=True 在 pharma 下（岗位已注册、
    计算为 False、不再断言）不会回撤 → HermiT 读脏断言与 fallback 分漂移。
    clear_individuals 的 world 级清扫修复此问题。
    """
    import moss.config as cfg
    import moss.owl_ontology as oo
    graph = GovernanceGraph()
    graph.decisions["m"] = Decision(
        id="m", title="生产", owner="x", owner_role="生产总监", domain="GMP生产",
        deadline=datetime.now() + timedelta(days=5),
    )

    def run_pair(archetype):
        monkeypatch.setenv("MOSS_ARCHETYPE", archetype)
        monkeypatch.setattr(cfg, "_config_cache", None)
        oo._onto_instance = None
        oo._java_checked, oo._java_available = True, True
        h = {k: sorted(v) for k, v in oo.reason_graph(graph).items() if isinstance(v, list)}
        oo._onto_instance = None
        oo._java_available = False
        f = {k: sorted(v) for k, v in oo.reason_graph(graph).items() if isinstance(v, list)}
        return h, f

    h1, f1 = run_pair("")           # 通用模式：生产总监 未注册
    h2, f2 = run_pair("pharma")     # 药厂模式：在册且域对齐
    assert h1 == f1 and "m" in h1["role_unregistered"]
    assert h2 == f2 and "m" in h2["role_aligned"] and "m" not in h2["role_unregistered"]


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
