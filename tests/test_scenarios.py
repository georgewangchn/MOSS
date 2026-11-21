"""场景级模拟测试 —— 12 个治理场景 × 每场景 5-8 轮会议/邮件交互。

模拟方式（与产品架构一致：LLM 只抽取，引擎/推理机判定）：
  - 每轮交互 = 一份会议纪要/周报/邮件文档（以注释呈现）+ 其**确定性抽取结果**
    （ParsedResult / add_update 调用，代表 LLM 抽取输出的规范形态）；
  - 判定全部走真实管线：GovernanceEngine 摄入 → reason_graph 分类 →
    OwlCompiler.compile 完备性检查 → OwlRuntime.checkpoint 告警 → auto_escalate 升级。
  - 推理路径强制 Python fallback（确定性、不依赖 Java）；其与 HermiT 的等价
    由 tests/test_ontology_repo.py::test_hermit_matches_fallback 守护。

时间处理：所有时间用真实 now 的日级偏移（±3/±30 天），跨午夜安全；
需要"停滞/超期"历史状态时直接回填模型时间戳后 save()。
"""
from datetime import datetime, timedelta

import pytest

import moss.owl_ontology as oo
from moss.engine import GovernanceEngine
from moss.models import ParsedResult, compute_priority
from moss.owl_compiler import OwlCompiler, OwlRuntime
from moss.storage import Storage


@pytest.fixture(autouse=True)
def _force_fallback(monkeypatch):
    """全模块强制 fallback 推理，保证确定性。"""
    monkeypatch.setattr(oo, "_java_checked", True)
    monkeypatch.setattr(oo, "_java_available", False)
    monkeypatch.setattr(oo, "_onto_instance", None)


# ── 测试基座 ──────────────────────────────────────────────────

def make_engine(tmp_path):
    return GovernanceEngine(Storage(str(tmp_path)))


def classify(engine):
    return oo.reason_graph(engine.graph)


def alerts_of(engine, decision_id):
    return [a for a in OwlRuntime().checkpoint(engine.graph) if a.decision_id == decision_id]


def compile_of(engine, decision_id):
    return [c for c in OwlCompiler().compile(engine.graph) if c.decision_id == decision_id]


def escalate(engine):
    rt = OwlRuntime()
    return rt.auto_escalate(rt.checkpoint(engine.graph), engine.graph)


def priority_of(engine, did):
    return compute_priority(engine.graph.decisions[did], engine.graph)


def backdate(engine, did=None, uid=None, days=30):
    """回填时间戳模拟时间流逝（决策 last_update_at / 更新 created_at）。"""
    if did:
        engine.graph.decisions[did].last_update_at = datetime.now() - timedelta(days=days)
    if uid:
        for u in engine.graph.updates:
            if u.id == uid:
                u.created_at = datetime.now() - timedelta(days=days)
    engine.save()


# ═══════════════════════════════════════════════════════════════
# S1 · 常规交付全程：决议 → 指派 → 周报推进 → 按期完成
# ═══════════════════════════════════════════════════════════════
def test_s01_normal_delivery_lifecycle(tmp_path):
    eng = make_engine(tmp_path)

    # R1 会议纪要：「讨论通过：完成 ERP 系统升级，目标年底前完成」
    r1 = eng.ingest_parsed_result(ParsedResult(decisions=[
        {"title": "完成ERP系统升级", "raw_statement": "完成 ERP 系统升级，目标年底前完成"},
    ]))
    did = r1["created"][0].id
    assert r1["created"] and priority_of(eng, did) == "P4"          # 无主 → P4

    # R2 邮件：「ERP 升级由张工负责，技术部，12-30 前完成」
    eng.ingest_parsed_result(ParsedResult(assignments=[{
        "decision_title": "完成ERP系统升级", "owner": "张工",
        "department": "技术部",
        "deadline": (datetime.now() + timedelta(days=30)).isoformat(),
    }]))
    d = eng.graph.decisions[did]
    assert d.owner == "张工" and priority_of(eng, did) == "P2"      # owner+deadline 无更新

    # R3 周报 W1：「方案设计完成，进度 10%」
    eng.add_update(did, "方案设计完成", progress=10.0)
    assert classify(eng)["governed"] == [did]
    assert priority_of(eng, did) == "P1"

    # R4 周报 W2：进度 30%
    eng.add_update(did, "开发环境就绪", progress=30.0)
    # R5 周报 W3：进度 60%
    eng.add_update(did, "核心模块迁移完成", progress=60.0)
    # R6 会议：「出现性能隐患，风险上调 MEDIUM」
    eng.add_update(did, "压测发现瓶颈，上调风险", risk="MEDIUM")
    assert classify(eng)["high_risk"] == []                          # MEDIUM ≠ HIGH

    # R7 周报 W4：进度 90%
    eng.add_update(did, "UAT 通过", progress=90.0)
    assert d.progress == 90.0 and d.status == "RUNNING"             # 首次更新自动 OPEN→RUNNING

    # R8 会议纪要：「ERP 升级验收通过，项目关闭」
    eng.transition_status(did, "DONE")
    r = classify(eng)
    assert r["done"] == [did]
    assert priority_of(eng, did) == "P4"                            # DONE → P4
    assert d.progress == 100.0
    assert alerts_of(eng, did) == []                                # 完成后无任何告警


# ═══════════════════════════════════════════════════════════════
# S2 · 无主决策问责：ERROR → 升级 → 指派 → 补期限 → 补更新
# ═══════════════════════════════════════════════════════════════
def test_s02_unowned_decision_accountability(tmp_path):
    eng = make_engine(tmp_path)

    # R1 邮件：「建议启动客户数据合规整改」（无负责人、无期限）
    r1 = eng.ingest_parsed_result(ParsedResult(decisions=[
        {"title": "客户数据合规整改", "raw_statement": "监管新规要求整改客户数据处理流程"},
    ]))
    did = r1["created"][0].id
    assert any(c.issue_type == "MISSING_OWNER" and c.severity == "ERROR"
               for c in compile_of(eng, did))
    assert classify(eng)["has_owner"] == []

    # R2 周报：无人认领，风险被标记 HIGH
    eng.update_decision(did, risk_level="HIGH")
    high = [a for a in alerts_of(eng, did) if a.alert_type == "HIGH_RISK"]
    assert high and high[0].severity == "HIGH"

    # R3 升级会议：HIGH 告警自动升级（无主 → escalation.owner 为空）
    esc = [e for e in escalate(eng) if e.decision_id == did]
    assert esc and esc[0].owner is None

    # R4 邮件：「责成李部长（财务）牵头」
    eng.assign_owner(did, "李部长", "财务部")
    assert classify(eng)["has_owner"] == [did]
    assert priority_of(eng, did) == "P3"                            # 仅 owner

    # R5 邮件：「期限 30 天」
    eng.update_decision(did, deadline=datetime.now() + timedelta(days=30))
    assert classify(eng)["has_deadline"] == [did]
    assert priority_of(eng, did) == "P2"

    # R6 周报：首次进展
    eng.add_update(did, "整改方案初稿完成", progress=15.0)
    assert classify(eng)["governed"] == [did] and priority_of(eng, did) == "P1"


# ═══════════════════════════════════════════════════════════════
# S3 · 超期升级链：超期 3 天 → 14 天 → 升级通知 → 完成止损
# ═══════════════════════════════════════════════════════════════
def test_s03_overdue_escalation_chain(tmp_path):
    eng = make_engine(tmp_path)

    # R1 会议纪要：「旧机房下线，截止日已定」（创建时即已过期 3 天）
    d = eng.create_decision(
        title="旧机房下线迁移", owner="王工",
        deadline=datetime.now() - timedelta(days=3),
    )
    did = d.id

    # R2 周报：有一次进展（governed 成立）
    eng.add_update(did, "设备清单已盘点", progress=20.0)
    assert classify(eng)["governed"] == [did]

    # R3 checkpoint：超期 3 天告警
    assert classify(eng)["overdue"] == [did]
    od = [a for a in alerts_of(eng, did) if a.alert_type == "OVERDUE"]
    assert od and od[0].severity == "HIGH" and "3" in od[0].message

    # R4 模拟又过 11 天（deadline 实际再往前推 → 相当于超期 14 天）
    eng.update_decision(did, deadline=datetime.now() - timedelta(days=14))
    od = [a for a in alerts_of(eng, did) if a.alert_type == "OVERDUE"]
    assert "14" in od[0].message

    # R5 升级：通知到 owner
    esc = [e for e in escalate(eng) if e.decision_id == did]
    assert esc and esc[0].owner == "王工"

    # R6 持续无进展 → 同时进入停滞
    backdate(eng, did=did, days=30)
    assert classify(eng)["stale"] == [did]

    # R7 会议纪要：「迁移完成，机房已下线」→ 止损
    eng.transition_status(did, "DONE")
    r = classify(eng)
    assert did not in r["overdue"] and did not in r["stale"]
    assert alerts_of(eng, did) == []


# ═══════════════════════════════════════════════════════════════
# S4 · 高风险 P0 触发：governed ∧ 截止≤7天 ∧ HIGH 才升 P0
# ═══════════════════════════════════════════════════════════════
def test_s04_p0_critical_trigger(tmp_path):
    eng = make_engine(tmp_path)
    did = eng.create_decision(
        title="支付通道切换", owner="赵工",
        deadline=datetime.now() + timedelta(days=3),
    ).id

    # R1 周报：进度 20（governed 成立；截止 3 天但风险 LOW → 仅 P1）
    eng.add_update(did, "通道联调启动", progress=20.0)
    r = classify(eng)
    assert r["governed"] == [did] and priority_of(eng, did) == "P1"
    assert r["critical"] == []

    # R2 会议：「支付牌照审查趋严，风险 HIGH」
    eng.add_update(did, "监管审查趋严", risk="HIGH")
    assert classify(eng)["critical"] == [did]
    assert priority_of(eng, did) == "P0"
    assert any(a.alert_type == "HIGH_RISK" for a in alerts_of(eng, did))

    # R3 周报：风险维持 HIGH，critical 稳定
    eng.add_update(did, "补充材料已提交", progress=45.0)
    assert classify(eng)["critical"] == [did]

    # R4 会议：「审查通过，风险回落 MEDIUM」→ P0 条件破坏
    eng.add_update(did, "审查通过", risk="MEDIUM")
    assert classify(eng)["critical"] == [] and priority_of(eng, did) == "P1"

    # R5 周报：HIGH 窗口期整体断言
    assert classify(eng)["high_risk"] == []


# ═══════════════════════════════════════════════════════════════
# S5 · 阻塞依赖传播：B 阻塞 → 依赖方 A 被标记 → 解除恢复
# ═══════════════════════════════════════════════════════════════
def test_s05_blocked_dependency_propagation(tmp_path):
    eng = make_engine(tmp_path)
    b = eng.create_decision(title="采购新服务器", owner="钱主管",
                            deadline=datetime.now() + timedelta(days=20)).id
    a = eng.create_decision(title="数据迁移执行", owner="孙工",
                            deadline=datetime.now() + timedelta(days=25)).id
    c = eng.create_decision(title="独立验收报告", owner="周工",
                            deadline=datetime.now() + timedelta(days=15)).id

    # R1 会议纪要：「数据迁移依赖服务器到位」
    eng.update_decision(a, depends_on=[b])
    assert classify(eng)["has_dependency"] == [a]

    # R2 邮件：「供应商断供，采购阻塞」
    eng.transition_status(b, "BLOCKED")
    r = classify(eng)
    assert r["blocked"] == [b]
    assert r["blocked_dependency"] == [a]
    assert any(al.alert_type == "BLOCKED" for al in alerts_of(eng, b))

    # R3 周报：A 自身仍 RUNNING，但因依赖受阻
    eng.add_update(a, "等待服务器到货", progress=10.0)
    assert classify(eng)["blocked_dependency"] == [a]

    # R4 悬空依赖：C 声称依赖不存在的 "ghost"（容忍，不分类）
    eng.update_decision(c, depends_on=["ghost"])
    r = classify(eng)
    assert c not in r["has_dependency"]
    assert c not in r["blocked_dependency"]

    # R5 邮件：「备货渠道打通，采购恢复」
    eng.transition_status(b, "RUNNING")
    assert classify(eng)["blocked_dependency"] == []

    # R6 周报：依赖完成
    eng.add_update(b, "服务器上架", progress=80.0)

    # R7 会议纪要：「采购完结」→ B DONE 后 A 不再受 blocked_dependency 影响
    eng.transition_status(b, "DONE")
    r = classify(eng)
    assert r["done"] == [b] and r["blocked_dependency"] == []
    assert classify(eng)["has_dependency"] == [a]                   # 依赖关系本身保留


# ═══════════════════════════════════════════════════════════════
# S6 · 停滞与恢复：STALE/NO_PROGRESS → 新更新止损（旧更新仍算陈旧）
# ═══════════════════════════════════════════════════════════════
def test_s06_stale_and_revival(tmp_path):
    eng = make_engine(tmp_path)
    d1 = eng.create_decision(title="官网改版", owner="吴设计",
                             deadline=datetime.now() + timedelta(days=40)).id
    d2 = eng.create_decision(title="品牌手册修订", owner="郑编辑",
                             deadline=datetime.now() + timedelta(days=60)).id

    # R1 周报：d1 有一次进展
    u1 = eng.add_update(d1, "首页视觉稿完成", progress=25.0)
    assert classify(eng)["stale"] == []

    # R2-R3 模拟五周静默：d1 的更新与 last_update_at 回溯 30 天
    backdate(eng, did=d1, uid=u1.id, days=30)
    r = classify(eng)
    assert r["stale"] == [d1]
    assert r["stale_update"] == [u1.id]
    assert any(c.issue_type == "STALE" for c in compile_of(eng, d1))
    assert any(a.alert_type == "STALE" for a in alerts_of(eng, d1))

    # R4 d2 从未更新且创建已 5 天 → NO_PROGRESS
    eng.graph.decisions[d2].created_at = datetime.now() - timedelta(days=5)
    eng.save()
    assert any(a.alert_type == "NO_PROGRESS" for a in alerts_of(eng, d2))

    # R5 会议纪要：「改版重启，交互稿完成」
    u2 = eng.add_update(d1, "交互稿完成", progress=50.0)
    assert classify(eng)["stale"] == []                             # 决策级停滞止损

    # R6 语义记录：旧更新 U-001 本身仍是陈旧文档（stale_update 不因新更新消失）
    r = classify(eng)
    assert u1.id in r["stale_update"] and u2.id not in r["stale_update"]

    # R7 d2 补一次进展 → NO_PROGRESS 消失
    eng.add_update(d2, "目录结构确定", progress=10.0)
    assert not any(a.alert_type == "NO_PROGRESS" for a in alerts_of(eng, d2))

    # R8 收尾：DONE 后即使历史陈旧也不再告警
    eng.transition_status(d2, "DONE")
    backdate(eng, did=d2, days=30)
    assert not any(a.alert_type == "STALE" for a in alerts_of(eng, d2))


# ═══════════════════════════════════════════════════════════════
# S7 · 议题→决策晋升：EDR 触发 · 同批去重 · 候选匹配关联 · CRITICAL 议题
# ═══════════════════════════════════════════════════════════════
def test_s07_issue_to_decision_promotion(tmp_path):
    eng = make_engine(tmp_path)

    # R1 会议纪要：「有同事提出供应商合同快到期，需要评估」→ 无匹配决策 → EDR
    r1 = eng.ingest_parsed_result(ParsedResult(issues=[
        {"title": "供应商合同到期风险", "description": "合同将于三个月内到期", "severity": "MEDIUM"},
    ]))
    assert len(r1["edr_needs"]) == 1
    assert "no_matching_decision" in r1["edr_needs"][0]["reason"]

    # R2 会议纪要：同批同时抽出议题与近似决策 → 决策被本体守卫抑制
    r2 = eng.ingest_parsed_result(ParsedResult(
        issues=[{"title": "供应商合同到期风险", "severity": "HIGH"}],
        decisions=[{"title": "供应商合同到期风险处理"}],   # bigram 重叠 ≥3 → 抑制
    ))
    assert r2["created"] == [] and len(eng.graph.issues) == 2       # 只有议题入库

    # R3 邮件：EDR 结论 → 成立「供应商切换专项」并指定负责人
    r3 = eng.ingest_parsed_result(ParsedResult(assignments=[{
        "decision_title": "供应商切换专项", "owner": "冯经理", "department": "采购部",
        "deadline": (datetime.now() + timedelta(days=45)).isoformat(),
    }]))
    did = r3["created"][0].id

    # R4 会议纪要：与既有决策高度相关的议题 + Agent 候选匹配 → 直接关联（不触发 EDR）
    r4 = eng.ingest_parsed_result(ParsedResult(
        issues=[{"title": "供应商切换专项推进风险", "severity": "MEDIUM"}],
        candidate_matches=[{"extracted_title": "供应商切换专项推进风险",
                            "existing_id": did, "relation": "related"}],
    ))
    assert r4["edr_needs"] == []
    linked_iid = [i.id for i in eng.graph.issues.values()
                  if i.title == "供应商切换专项推进风险"][0]
    assert did in eng.graph.edges["issue_to_decision"].get(linked_iid, [])

    # R5 邮件：「合规部通报 CRITICAL 级数据出境隐患」
    eng.ingest_parsed_result(ParsedResult(issues=[
        {"title": "数据出境合规隐患", "severity": "CRITICAL"},
    ]))
    assert "数据出境合规隐患" in [i.title for i in eng.graph.issues.values()]

    # R6 推理断言：CRITICAL 议题进入 critical_issue 分类；HIGH 议题不进
    r = classify(eng)
    crit_issues = [eng.graph.issues[i].title for i in r["critical_issue"]]
    assert "数据出境合规隐患" in crit_issues
    assert "供应商合同到期风险" not in crit_issues

    # R7 重复关联幂等：边不重复，决策反向指回议题
    eng.link_issue_to_decision(linked_iid, did)
    assert eng.graph.edges["issue_to_decision"][linked_iid].count(did) == 1
    assert eng.graph.decisions[did].issue_id == linked_iid


# ═══════════════════════════════════════════════════════════════
# S8 · 周报批量摄入：一轮多决议 + 模糊指派匹配 + 部门聚合
# ═══════════════════════════════════════════════════════════════
def test_s08_weekly_batch_ingest(tmp_path):
    eng = make_engine(tmp_path)

    # R1 会议纪要（季度经营会）：一次形成 4 项决议与指派
    r1 = eng.ingest_parsed_result(ParsedResult(assignments=[
        {"decision_title": "华东区渠道下沉", "owner": "陈总监", "department": "销售部",
         "deadline": (datetime.now() + timedelta(days=30)).isoformat()},
        {"decision_title": "客服知识库建设", "owner": "刘经理", "department": "客服部",
         "deadline": (datetime.now() + timedelta(days=60)).isoformat()},
        {"decision_title": "年报审计准备", "owner": "何会计师", "department": "财务部",
         "deadline": (datetime.now() + timedelta(days=45)).isoformat()},
        {"decision_title": "等保测评整改", "owner": "郭安全", "department": "技术部",
         "dependency": "行政部",
         "deadline": (datetime.now() + timedelta(days=20)).isoformat()},
    ]))
    assert len(r1["created"]) == 4
    ids = [d.id for d in r1["created"]]
    assert len(set(ids)) == 4                                       # 决策 ID 递增不重复

    # R2 周报：三部门各自进展
    eng.add_update(ids[0], "江苏试点签约", progress=30.0)
    eng.add_update(ids[1], "FAQ 结构完成", progress=25.0)
    eng.add_update(ids[3], "差距分析完成", progress=15.0)

    # R3 周报（模糊标题指派）：指派标题是既有决策的子串 → 模糊匹配而非新建
    r3 = eng.ingest_parsed_result(ParsedResult(assignments=[{
        "decision_title": "渠道下沉", "owner": "陈总监", "department": "销售部",
    }]))
    assert r3["created"] == [] and r3["updated"]                    # ≥0.4 匹配到既有决策

    # R4 摄入重复决议（标题近重复）→ 去重不新建
    r4 = eng.ingest_parsed_result(ParsedResult(decisions=[
        {"title": "华东区渠道下沉"}, {"title": "客服知识库建设"},
    ]))
    assert r4["created"] == [] and len(eng.graph.decisions) == 4

    # R5 部门聚合
    stats = eng.get_stats()
    assert stats["by_department"] == {"销售部": 1, "客服部": 1, "财务部": 1, "技术部": 1}
    assert stats["overdue_count"] == 0 and stats["stale_count"] == 0

    # R6 优先级分布：3 个 governed(P1) + 1 个 P2（年报审计未报进展）
    r = classify(eng)
    assert sorted(r["governed"]) == sorted(ids[:2] + [ids[3]])
    assert priority_of(eng, ids[2]) == "P2"


# ═══════════════════════════════════════════════════════════════
# S9 · 风险与窗口期联动：HIGH 风险 × 截止 ≤7 天 的 AND 语义
# ═══════════════════════════════════════════════════════════════
def test_s09_risk_deadline_window_conjunction(tmp_path):
    eng = make_engine(tmp_path)
    did = eng.create_decision(title="新车发布会筹备", owner="许总监",
                              deadline=datetime.now() + timedelta(days=20)).id

    # R1 周报：进度 10，LOW，截止尚远 → P1
    eng.add_update(did, "场地初筛", progress=10.0)
    assert priority_of(eng, did) == "P1"

    # R2 会议：「舆情风险 HIGH」但截止还有 ~20 天 → 仍 P1（AND 未满足）
    eng.add_update(did, "舆情预警", risk="HIGH")
    assert classify(eng)["high_risk"] == [did]
    assert classify(eng)["critical"] == []
    assert priority_of(eng, did) == "P1"

    # R3 模拟临近：截止改到 5 天后 → HIGH ∧ ≤7d → P0
    eng.update_decision(did, deadline=datetime.now() + timedelta(days=5))
    assert classify(eng)["critical"] == [did]
    assert priority_of(eng, did) == "P0"

    # R4 周报：保持 HIGH + 窗口内 → P0 稳定
    eng.add_update(did, "媒体邀约发出", progress=55.0)
    assert priority_of(eng, did) == "P0"

    # R5 会议：「舆情降温 MEDIUM」→ 破坏 AND → P1
    eng.add_update(did, "舆情降温", risk="MEDIUM")
    assert classify(eng)["critical"] == [] and priority_of(eng, did) == "P1"

    # R6 窗口断言：critical 与 high_risk 是独立分类
    r = classify(eng)
    assert r["high_risk"] == [] and r["critical"] == []


# ═══════════════════════════════════════════════════════════════
# S10 · 状态机全路径：合法迁移 + 非法迁移拒绝 + 自动迁移
# ═══════════════════════════════════════════════════════════════
def test_s10_status_machine_full_path(tmp_path):
    eng = make_engine(tmp_path)
    did = eng.create_decision(title="双活数据中心建设", owner="杨架构",
                              deadline=datetime.now() + timedelta(days=90)).id
    d = eng.graph.decisions[did]
    assert d.status == "OPEN"

    # R1 周报：首次进展 → 自动 OPEN→RUNNING
    eng.add_update(did, "架构评审通过", progress=10.0)
    assert d.status == "RUNNING"

    # R2 邮件：「机房电力审批受阻」RUNNING→BLOCKED 合法
    eng.transition_status(did, "BLOCKED")
    assert classify(eng)["blocked"] == [did]

    # R3 非法：BLOCKED→OPEN 拒绝
    with pytest.raises(ValueError, match="Cannot transition"):
        eng.transition_status(did, "OPEN")

    # R4 邮件：「审批通过」BLOCKED→RUNNING 合法
    eng.transition_status(did, "RUNNING")
    assert classify(eng)["blocked"] == []

    # R5 会议纪要：「里程碑验收提前完成」RUNNING→DONE 合法；进度自动 100
    eng.transition_status(did, "DONE")
    assert classify(eng)["done"] == [did] and d.progress == 100.0

    # R6 终态封闭：DONE→RUNNING / DONE→BLOCKED 均拒绝
    with pytest.raises(ValueError, match="Cannot transition"):
        eng.transition_status(did, "RUNNING")
    with pytest.raises(ValueError, match="Cannot transition"):
        eng.transition_status(did, "BLOCKED")

    # R7 全量告警：DONE 后 checkpoint 完全静默
    assert alerts_of(eng, did) == []

    # R8 终态保护：DONE 后补充更新/改进度不得回退 progress（终态即验收）
    eng.add_update(did, "验收后补充归档说明", progress=40.0)
    assert d.progress == 100.0
    eng.update_decision(did, progress=10.0)
    assert d.progress == 100.0


# ═══════════════════════════════════════════════════════════════
# S11 · 多决策大会：跨部门决议 + 协作缺失告警 + 会议简报
# ═══════════════════════════════════════════════════════════════
def test_s11_multi_decision_board_meeting(tmp_path):
    eng = make_engine(tmp_path)

    # R1 董事会纪要：三项跨部门决议（其中年报审计声明依赖财务部）
    r1 = eng.ingest_parsed_result(ParsedResult(assignments=[
        {"decision_title": "智能客服上线", "owner": "崔产品", "department": "产品部",
         "deadline": (datetime.now() + timedelta(days=25)).isoformat()},
        {"decision_title": "年报审计整改", "owner": "沈审计", "department": "财务部",
         "dependency": "技术部",
         "deadline": (datetime.now() + timedelta(days=40)).isoformat()},
        {"decision_title": "海外节点扩容", "owner": "韩运维", "department": "技术部",
         "deadline": (datetime.now() + timedelta(days=35)).isoformat()},
    ]))
    ids = {d.title: d.id for d in r1["created"]}
    assert len(ids) == 3

    # R2 完备性：未声明协作部门的决议收到 MISSING_DEPENDENCY 警告，声明的豁免
    ci_smart = compile_of(eng, ids["智能客服上线"])
    ci_audit = compile_of(eng, ids["年报审计整改"])
    assert any(c.issue_type == "MISSING_DEPENDENCY" for c in ci_smart)
    assert not any(c.issue_type == "MISSING_DEPENDENCY" for c in ci_audit)

    # R3 周报：全部启动进展
    for title, prog in [("智能客服上线", 15.0), ("年报审计整改", 10.0), ("海外节点扩容", 20.0)]:
        eng.add_update(ids[title], "启动", progress=prog)
    r = classify(eng)
    assert sorted(r["governed"]) == sorted(ids.values())

    # R4 邮件：「海外节点被监管问询，风险 HIGH」
    eng.add_update(ids["海外节点扩容"], "监管问询", risk="HIGH")
    assert classify(eng)["high_risk"] == [ids["海外节点扩容"]]

    # R5 会议简报：智能客服/海外节点因缺协作部门 → 2/3 AT RISK
    brief = OwlRuntime().generate_meeting_brief(eng.graph)
    assert "2/3" in brief and "AT RISK" in brief

    # R6 部门视图：产品/财务/技术各 1 项
    assert eng.get_stats()["by_department"] == {"产品部": 1, "财务部": 1, "技术部": 1}


# ═══════════════════════════════════════════════════════════════
# S12 · 关键人员断层：负责人请假 → 超期升级 → 交接改派 → 闭环
# ═══════════════════════════════════════════════════════════════
def test_s12_key_person_absence_handover(tmp_path):
    eng = make_engine(tmp_path)
    did = eng.create_decision(title="等保三级认证", owner="罗安全",
                              deadline=datetime.now() + timedelta(days=10)).id

    # R1 任命邮件 + 周报：正常推进
    eng.add_update(did, "差距评估完成", progress=20.0)
    assert eng.graph.decisions[did].owner == "罗安全"

    # R2 邮件：「罗安全休假两周，期间无法响应」→ 进展静默且跨过截止日
    eng.update_decision(did, deadline=datetime.now() - timedelta(days=2))
    backdate(eng, did=did, days=15)
    assert classify(eng)["overdue"] == [did]

    # R3 checkpoint + 升级：通知到罗安全本人
    esc = [e for e in escalate(eng) if e.decision_id == did]
    assert esc and esc[0].owner == "罗安全"

    # R4 周报：仍无进展 → 停滞叠加
    assert classify(eng)["stale"] == [did]

    # R5 邮件：「项目经理 高岗（安全技术部）接管」
    eng.assign_owner(did, "高岗", "安全技术部")
    assert eng.get_assignment(did).owner == "高岗"                  # 最新指派生效
    assert classify(eng)["has_owner"] == [did]

    # R6 接手人推动：新进展止损停滞（超期仍持续到完成）
    eng.add_update(did, "整改项过半", progress=60.0)
    assert classify(eng)["stale"] == []
    assert classify(eng)["overdue"] == [did]

    # R7 会议纪要：「认证通过」→ 升级链闭环
    eng.transition_status(did, "DONE")
    assert alerts_of(eng, did) == [] and classify(eng)["done"] == [did]


# ═══════════════════════════════════════════════════════════════
# S13 · 岗位-职责对齐（医药原型）：专业岗位/通用岗位 × 对齐/错配/未注册
# ═══════════════════════════════════════════════════════════════
def test_s13_role_duty_alignment(tmp_path, monkeypatch):
    import moss.config as cfg
    monkeypatch.setenv("MOSS_ARCHETYPE", "pharma")
    monkeypatch.setattr(cfg, "_config_cache", None)
    eng = make_engine(tmp_path)

    # R1 会议纪要（生产例会）：「注射剂生产线扩产，属 GMP生产 事务，生产负责人牵头」
    d1 = eng.create_decision(title="注射剂生产线扩产", owner="马厂长",
                             owner_role="生产负责人", domain="GMP生产",
                             deadline=datetime.now() + timedelta(days=30)).id
    # R2 会议纪要（经营会）：「年度预算编制，属财务事务，CFO 负责」
    d2 = eng.create_decision(title="年度预算编制", owner="钱会计",
                             owner_role="首席财务官", domain="财务",
                             deadline=datetime.now() + timedelta(days=45)).id
    # R3 会议纪要（质量例会）：「偏差调查整改，属质量事务，质量负责人牵头」
    d3 = eng.create_decision(title="偏差调查整改", owner="孙质量",
                             owner_role="质量负责人", domain="质量",
                             deadline=datetime.now() + timedelta(days=14)).id
    r = classify(eng)
    assert sorted(r["role_aligned"]) == sorted([d1, d2, d3])     # 岗位域精确匹配
    assert r["role_misaligned"] == []

    # R4 邮件（越权指派）：「GMP 验证主计划由 CFO 分管」→ CFO 职责不含 GMP生产
    d4 = eng.create_decision(title="工艺验证主计划", owner="钱会计",
                             owner_role="首席财务官", domain="GMP生产",
                             deadline=datetime.now() + timedelta(days=20)).id
    assert classify(eng)["role_misaligned"] == [d4]

    # R5 邮件（角色不在词汇表）：「数据治理由分管领导负责」→ 未注册 = 数据治理信号（区别于错配）
    d5 = eng.create_decision(title="数据治理专项", owner="周某",
                             owner_role="分管领导", domain="信息技术",
                             deadline=datetime.now() + timedelta(days=60)).id
    r = classify(eng)
    assert r["role_misaligned"] == [d4]
    assert r["role_unregistered"] == [d5]

    # R6 会议纪要（只声明单侧）：有岗位无域 / 有域无岗位 → 两个对齐类都不进
    d6 = eng.create_decision(title="培训体系升级", owner="吴某",
                             owner_role="首席人才官",
                             deadline=datetime.now() + timedelta(days=50)).id
    d7 = eng.create_decision(title="安全库存策略", owner="郑某", domain="供应链",
                             deadline=datetime.now() + timedelta(days=25)).id
    r = classify(eng)
    for did in (d6, d7):
        assert did not in r["role_aligned"] and did not in r["role_misaligned"]

    # R7 会议纪要（改派纠偏）：d4 改由生产负责人分管 → 离开错配、进入对齐
    eng.update_decision(d4, owner_role="生产负责人")
    r = classify(eng)
    assert d4 in r["role_aligned"] and d4 not in r["role_misaligned"]
    assert r["role_misaligned"] == [] and r["role_unregistered"] == [d5]

    # R8 真实表述鲁棒性：别名命中 + 高管跨域覆盖 + 域别名归一
    d8 = eng.create_decision(title="年度资金计划", owner="冯某",
                             owner_role="财务总监", domain="预算",
                             deadline=datetime.now() + timedelta(days=30)).id
    d9 = eng.create_decision(title="集团质量月度评审", owner="总裁办",
                             owner_role="总经理", domain="质量",
                             deadline=datetime.now() + timedelta(days=15)).id
    r = classify(eng)
    assert d8 in r["role_aligned"]      # 财务总监→CFO 别名；预算→财务 域别名
    assert d9 in r["role_aligned"]      # 总经理→CEO 别名；covers_all 跨域覆盖质量域

    # 行业上下文经 workspace 常量桥接：医药 ∧ GMP生产 触发行业特化类
    assert sorted(r["pharma_gmp_production"]) == sorted([d1, d4])
    assert sorted(r["domain_quality"]) == sorted([d3, d9])
    assert r["battery_safety"] == []                            # 跨行业类隔离

    # R9 治理人员工作流：对齐信号进入 compile 报告；role-only 也判未注册
    d10 = eng.create_decision(title="供应商大会筹备", owner="孙某",
                              owner_role="销售老大",
                              deadline=datetime.now() + timedelta(days=8)).id
    d11 = eng.create_decision(title="洁净区改造预算", owner="钱会计",
                              owner_role="首席财务官", domain="GMP生产",
                              deadline=datetime.now() + timedelta(days=12)).id
    r = classify(eng)
    assert d10 in r["role_unregistered"]                        # 未声明业务域不影响未注册判定
    assert d11 in r["role_misaligned"]
    ci5 = compile_of(eng, d5)
    assert any(c.issue_type == "ROLE_UNREGISTERED" for c in ci5)
    ci11 = compile_of(eng, d11)
    assert any(c.issue_type == "ROLE_MISALIGNED" and "首席财务官" in c.message for c in ci11)
    ci10 = compile_of(eng, d10)
    assert any(c.issue_type == "ROLE_UNREGISTERED" and "销售老大" in c.message for c in ci10)


# ═══════════════════════════════════════════════════════════════
# S14 · 原型切换：同一份治理数据，换 MOSS_ARCHETYPE 后行业语义随之切换
# ═══════════════════════════════════════════════════════════════
def test_s14_archetype_switching(tmp_path, monkeypatch):
    import moss.config as cfg
    eng = make_engine(tmp_path)

    def switch(archetype):
        monkeypatch.setenv("MOSS_ARCHETYPE", archetype)
        monkeypatch.setattr(cfg, "_config_cache", None)          # 重建配置单例

    # R1 药厂模式：GMP 生产 + 电池安全两类决策同时入库
    switch("pharma")
    gmp = eng.create_decision(title="洁净区环境监测", owner="马厂长",
                              owner_role="生产负责人", domain="GMP生产").id
    cell = eng.create_decision(title="新型电芯选型", owner="李某",
                               owner_role="电芯工艺负责人", domain="电芯工艺").id
    r = classify(eng)
    assert r["pharma_gmp_production"] == [gmp]
    assert r["battery_safety"] == []
    assert r["role_unregistered"] == [cell]                      # 电芯岗位不在药厂词汇表

    # R2 切换到电池厂：同一份 graph 重新推理 → 行业语义翻转
    switch("battery")
    r = classify(eng)
    assert r["pharma_gmp_production"] == []                      # 医药类静默
    assert cell in r["role_aligned"]                             # 电芯工艺负责人回到词汇表
    assert gmp in r["role_unregistered"]                         # 药厂生产负责人不在电池厂词汇表

    # R3 电池厂新增安全决策：行业特化类触发
    safety = eng.create_decision(title="热失控防护升级", owner="王某",
                                 owner_role="电池安全负责人", domain="电池安全").id
    r = classify(eng)
    assert r["battery_safety"] == [safety]
    assert safety in r["role_aligned"]

    # R4 通用层跨原型恒定：CFO/财务 对齐在两种原型下都成立
    fin = eng.create_decision(title="季度资金计划", owner="钱会计",
                              owner_role="首席财务官", domain="财务").id
    assert fin in classify(eng)["role_aligned"]

    # R5 取消原型声明：行业类全部静默，通用层对齐仍工作（降级路径）
    monkeypatch.setenv("MOSS_ARCHETYPE", "")
    monkeypatch.setattr(cfg, "_config_cache", None)
    r = classify(eng)
    assert r["battery_safety"] == [] and r["pharma_gmp_production"] == []
    assert fin in r["role_aligned"] and cell in r["role_unregistered"]

    # R6 落库往返：org 上下文字段（owner_role/domain）持久化不丢失
    eng.save()
    eng.reload()
    d = eng.graph.decisions[fin]
    assert d.owner_role == "首席财务官" and d.domain == "财务"


# ═══════════════════════════════════════════════════════════════
# 回归：moss demo 种子可装载且组织三态可演示
# ═══════════════════════════════════════════════════════════════
def test_demo_seed_loads_with_org_context(tmp_path):
    """种子装载回归（U-001 created_at 移除曾致 loader 传 None 崩溃）+ 组织三态。"""
    from moss.commands.init_cmd import load_demo_seed
    from moss.storage import Storage as _Storage
    graph = load_demo_seed(_Storage(str(tmp_path)))
    assert len(graph.decisions) == 4 and len(graph.issues) == 2 and len(graph.updates) == 2
    r = oo.reason_graph(graph)
    assert sorted(r["role_aligned"]) == ["demo-001", "demo-003"]
    assert r["role_misaligned"] == ["demo-002"]      # CFO 牵头供应商切换（供应链域）
    assert r["role_unregistered"] == ["demo-004"]    # "分管副总"非标准岗位


# ── 套件完整性（防场景漂移的元断言） ───────────────────────────

def test_scenario_matrix_complete():
    """确保 14 个场景测试函数始终在位（改名/误删会被发现）。"""
    names = {n for n in globals() if n.startswith("test_s")}
    for i in range(1, 15):
        assert any(n.startswith(f"test_s{i:02d}_") for n in names), f"缺少场景 S{i}"
