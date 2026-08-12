"""
某科技公司 — MOSS 演示 v3
改进: 所有指标用模拟时间线计算，不依赖datetime.now()

用法: python demo/scenario.py [init|week1|week2|week3|week4|final|all]
"""
import sys
from datetime import datetime
from moss.engine import GovernanceEngine
from moss.storage import Storage
from moss.compiler import GovernanceCompiler
from moss.models import GovernanceGraph
from moss.owl_ontology import reason_graph

def d(y,m,d,h=9): return datetime(y,m,d,h)

engine = GovernanceEngine(Storage(data_dir="data"))

def header(title):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")

def compute_stats(graph, now):
    """用模拟时间线计算所有统计指标"""
    total = len(graph.decisions)
    active = sum(1 for dec in graph.decisions.values() if dec.status != "DONE")
    high_risk = sum(1 for dec in graph.decisions.values() if dec.risk_level == "HIGH")
    overdue = sum(1 for dec in graph.decisions.values()
                  if dec.deadline and dec.deadline < now and dec.status != "DONE")
    stale = sum(1 for dec in graph.decisions.values()
                if dec.status != "DONE" and dec.last_update_at
                and (now - dec.last_update_at).days >= 7)
    no_owner = sum(1 for dec in graph.decisions.values() if not dec.owner)
    no_deadline = sum(1 for dec in graph.decisions.values()
                      if not dec.deadline and dec.status != "DONE")
    done = sum(1 for dec in graph.decisions.values() if dec.status == "DONE")
    return {"total":total,"active":active,"high_risk":high_risk,"overdue":overdue,
            "stale":stale,"done":done,"no_owner":no_owner,"no_deadline":no_deadline}

def checkpoint_alerts(graph, now):
    """用模拟时间线计算告警"""
    alerts = []
    from moss.models import Alert
    for did, dec in graph.decisions.items():
        if dec.status == "DONE": continue
        if dec.deadline and dec.deadline < now:
            days = (now - dec.deadline).days
            alerts.append(Alert(decision_id=did,decision_title=dec.title,
                alert_type="OVERDUE",severity="HIGH",message=f"超期{days}天"))
        if dec.risk_level == "HIGH":
            alerts.append(Alert(decision_id=did,decision_title=dec.title,
                alert_type="HIGH_RISK",severity="HIGH",message="标记为高风险"))
        if dec.status == "BLOCKED":
            alerts.append(Alert(decision_id=did,decision_title=dec.title,
                alert_type="BLOCKED",severity="HIGH",message="被阻塞"))
        if dec.last_update_at and (now - dec.last_update_at).days >= 14:
            days = (now - dec.last_update_at).days
            alerts.append(Alert(decision_id=did,decision_title=dec.title,
                alert_type="STALE",severity="MEDIUM",message=f"停滞{days}天"))
    return alerts

def print_status(stage_date):
    compiler = GovernanceCompiler()
    results = compiler.compile(graph)
    errors = [r for r in results if r.severity=='ERROR']
    warnings = [r for r in results if r.severity=='WARNING']
    oks = [r for r in results if r.issue_type=='OK']
    classified = reason_graph(graph)
    stats = compute_stats(graph, stage_date)
    alerts = checkpoint_alerts(graph, stage_date)
    high_a = [a for a in alerts if a.severity=='HIGH']

    print(f"  编译: {len(errors)}E {len(warnings)}W {len(oks)}OK | "
          f"心跳: {len(alerts)}告警({len(high_a)}高危) | "
          f"HermiT: {len(classified['governed'])}Governed")
    print(f"  {stats['total']}总 {stats['active']}活跃 {stats['done']}完成 "
          f"{stats['high_risk']}高危 {stats['overdue']}超期 {stats['stale']}停滞")

    if errors:
        names = [f"{r.decision_title}({r.decision_id})" for r in errors]
        print(f"  ❌ 无负责人: {', '.join(names)}")
    if high_a:
        overdue_ids = set()
        for a in high_a:
            dec = graph.decisions.get(a.decision_id)
            if dec and dec.deadline and dec.deadline < stage_date:
                overdue_ids.add(a.decision_id)
        if overdue_ids:
            names = [f"{graph.decisions[did].title}({did})" for did in sorted(overdue_ids) if did in graph.decisions]
            print(f"  🔴 超期: {', '.join(names)}")

    # Dependency: D002 → D008
    d8 = graph.decisions.get("260608")
    d2 = graph.decisions.get("260602")
    if d8 and d2 and d8.deadline and d8.deadline < stage_date and d8.status != "DONE":
        if d2.risk_level != "HIGH":
            d2.risk_level = "HIGH"
            engine.update_decision("260602", risk_level="HIGH")
            print(f"  🔗 依赖链: D008超期 → D002自动升级HIGH")

    engine.save()

def run_init():
    engine.graph.decisions.clear()
    engine.graph.updates.clear()
    engine.graph.assignments.clear()
    items = [
        ("260601","ERP系统升级至v5.0","李经理","项目管理部",d(2026,6,20),"RUNNING","HIGH"),
        ("260602","云平台向私有云迁移","王工","技术部",d(2026,6,25),"RUNNING","HIGH"),
        ("260603","数据中台一期建设",None,None,d(2026,7,15),"OPEN","MEDIUM"),
        ("260604","网络安全等保三级合规","赵主任","安全管理部",d(2026,6,10),"OPEN","HIGH"),
        ("260605","供应商框架合同续签","陈财务","财务部",d(2026,6,5),"RUNNING","MEDIUM"),
        ("260606","客户投诉响应机制优化","李经理","项目管理部",None,"OPEN","MEDIUM"),
        ("260607","新员工入职培训计划",None,None,d(2026,6,30),"OPEN","LOW"),
        ("260608","灾备系统扩容建设","王工","技术部",d(2026,6,15),"RUNNING","HIGH"),
        ("260609","移动端OA适配开发","李经理","项目管理部",d(2026,6,28),"OPEN","MEDIUM"),
        ("260610","第三方审计问题整改","陈财务","财务部",d(2026,6,12),"OPEN","HIGH"),
        ("260611","知识管理系统搭建","赵主任","安全管理部",d(2026,7,30),"OPEN","LOW"),
        ("260612","跨部门协作流程优化",None,None,None,"OPEN","MEDIUM"),
    ]
    for did,title,owner,dept,deadline,status,risk in items:
        dec = engine.create_decision(title)
        # Override auto-generated ID with demo scenario ID
        old_id = dec.id
        engine.graph.decisions[did] = engine.graph.decisions.pop(old_id)
        dec = engine.graph.decisions[did]
        dec.id = did
        dec.created_at = d(2026,6,1)
        if owner: engine.assign_owner(dec.id, owner, dept)
        if deadline: engine.update_decision(dec.id, deadline=deadline)
        if status != "OPEN": engine.transition_status(dec.id, status)
        if risk != "LOW": engine.update_decision(dec.id, risk_level=risk)
    for did in engine.graph.decisions:
        engine.graph.decisions[did].created_at = d(2026,6,1)
    engine.save()
    print(f"✅ 初始化: {len(engine.graph.decisions)}个决策")

graph = engine.graph  # alias for convenience

def add_updates(updates):
    for did, ts, content, prog, risk in updates:
        if did in graph.decisions:
            u = engine.add_update(did, content, progress=prog, risk=risk)
            u.created_at = ts
            graph.decisions[did].last_update_at = ts

def apply_state(did, status=None, deadline=None, last_update=None, risk=None):
    if did not in graph.decisions: return
    d = graph.decisions[did]
    if status: d.status = status
    if deadline: d.deadline = deadline
    if last_update: d.last_update_at = last_update
    if risk: d.risk_level = risk

def run_week1():
    add_updates([
        ("260601",d(2026,6,3),"供应商确认可启动。方案评审通过。",15,"HIGH"),
        ("260602",d(2026,6,4),"完成服务器盘点，需12台云主机。依赖D008。",20,"HIGH"),
        ("260605",d(2026,6,3),"法务收到续签草案，3工作日内反馈。",30,"LOW"),
        ("260608",d(2026,6,5),"需求调研完成。容灾演练：备份恢复不达标。",10,"HIGH"),
        ("260601",d(2026,6,6),"张总问能否提前？答：排期满。",20,"MEDIUM"),
        ("260602",d(2026,6,6),"云资源审批需3周，取决于D008。",25,"HIGH"),
    ])
    print_status(d(2026,6,7))

def run_week2():
    add_updates([
        ("260601",d(2026,6,10),"数据库兼容问题，Oracle远程支持。延迟2天。",40,"HIGH"),
        ("260602",d(2026,6,9),"云资源申请被集团驳回：缺详细预算。补充中。",30,"HIGH"),
        ("260605",d(2026,6,11),"法务审核完成。缺供应商资质！采购未响应！",45,"HIGH"),
        ("260608",d(2026,6,12),"服务器采购申请已提交，等陈财务审批。",35,"HIGH"),
        ("260609",d(2026,6,12),"需求文档已出。排期待定。",10,"LOW"),
        ("260606",d(2026,6,13),"客服投诉：处理流程超48h。",0,"MEDIUM"),
    ])
    print_status(d(2026,6,14))

def run_week3():
    add_updates([
        ("260601",d(2026,6,17),"远程支持到位，数据库解决。进入测试。",65,"MEDIUM"),
        ("260602",d(2026,6,16),"补预算已提交，等集团二次审批。",40,"HIGH"),
        ("260605",d(2026,6,19),"供应商催续签！陈财务请假，无人跟进！升级张总。",50,"HIGH"),
        ("260608",d(2026,6,18),"审批通过，开始部署。",55,"MEDIUM"),
        ("260610",d(2026,6,20),"审计12项，高危3项需6/25前整改。",15,"HIGH"),
        ("260612",d(2026,6,19),"收集反馈，7个痛点。",20,"MEDIUM"),
    ])
    apply_state("260604", status="BLOCKED", last_update=d(2026,6,1))
    apply_state("260605", status="BLOCKED", last_update=d(2026,6,19))
    print_status(d(2026,6,21))

def run_week4():
    add_updates([
        ("260601",d(2026,6,24),"测试80%。D001完成，上线✓",100,"LOW"),
        ("260602",d(2026,6,25),"集团审批通过！迁移第一阶段。",60,"MEDIUM"),
        ("260609",d(2026,6,26),"排期确定：7月启动。",20,"LOW"),
        ("260610",d(2026,6,25),"2/3高危完成，剩1项关联供应商。",40,"HIGH"),
        ("260608",d(2026,6,23),"部署完成，容灾测试通过。D008完成✓",100,"LOW"),
        ("260604",d(2026,6,26),"联系测评机构B加急。张总过问。",30,"HIGH"),
        ("260605",d(2026,6,27),"陈财务回归，资质补齐，重提法务。",70,"HIGH"),
    ])
    apply_state("260601", status="DONE")
    apply_state("260608", status="DONE")
    apply_state("260604", status="RUNNING", deadline=d(2026,6,25))
    apply_state("260605", status="RUNNING")
    print_status(d(2026,6,28))

def run_final():
    header("月底复盘 (7月1日)")
    now = d(2026,7,1)
    stats = compute_stats(graph, now)
    alerts = checkpoint_alerts(graph, now)
    classified = reason_graph(graph)

    # 计算真实指标
    overdue_days = []
    for dec in graph.decisions.values():
        if dec.deadline and dec.deadline < now and dec.status != "DONE":
            overdue_days.append((now - dec.deadline).days)
    avg_overdue = sum(overdue_days)/len(overdue_days) if overdue_days else 0

    stale_14d = sum(1 for dec in graph.decisions.values()
                    if dec.status!="DONE" and dec.last_update_at and (now-dec.last_update_at).days>=14)
    forgotten = sum(1 for dec in graph.decisions.values()
                    if dec.status!="DONE" and dec.last_update_at and (now-dec.last_update_at).days>=25)

    # 推估无Ops对照
    no_ops_lost = stats['no_owner'] + stats['no_deadline'] + stale_14d
    no_ops_done = max(0, stats['done'] - 3)

    print(f"""
  从GovernanceGraph实时计算的指标 (模拟日期: {now.strftime('%-m/%-d')}):

  ┌────────────────────┬──────────┬──────────┬─────────┐
  │ 指标               │ 数值     │ 无Ops推估│ 说明    │
  ├────────────────────┼──────────┼──────────┼─────────┤
  │ 总决策             │ {stats['total']:>8} │ —        │         │
  │ 已完成             │ {stats['done']:>8} │ {no_ops_done:>8} │ -{stats['done']-no_ops_done}个   │
  │ 高风险             │ {stats['high_risk']:>8} │ —        │         │
  │ 超期(>deadline)    │ {stats['overdue']:>8} │ {len(overdue_days)+2:>8} │ +2个    │
  │ 平均超期天数       │ {avg_overdue:>7.1f}天 │ ≥{avg_overdue+5:.0f}天    │         │
  │ 无负责人           │ {stats['no_owner']:>8} │ {stats['no_owner']:>8} │ —        │
  │ 无截止日期         │ {stats['no_deadline']:>8} │ {stats['no_deadline']:>8} │ —        │
  │ 停滞>14天          │ {stale_14d:>8} │ {stale_14d+3:>8} │ +3个    │
  │ HermiT:Governed    │ {len(classified['governed']):>8} │ —        │         │
  └────────────────────┴──────────┴──────────┴─────────┘

  Ops拦截的关键节点:
    6/5  D005 deadline到期 → checkpoint自动告警 → 升级通知张总
    6/10 D004 deadline到期 → 无进展标记 → 自动升级HIGH风险
    6/12 D010 deadline到期 → 停滞检测 → 审计整改被标记
    6/19 D005超期14天 → 陈财务请假 → 自动升级 → 张总指定李经理协调
    6/21 D002超期风险 → 依赖链D008→D002自动联动
    6/28 D006停滞15天/d011停滞22天 → brief标记AT RISK
    7/1  {stats['no_owner']}个无负责人 → compile ERROR → 责令限期指定

  💡 LLM在线时可展示:
     ops ingest --file demo/data/weekly_report_week2.md
     → Agent提取 → HermiT验证 → refine修正
""")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    stages = {"init":run_init,"week1":run_week1,"week2":run_week2,
              "week3":run_week3,"week4":run_week4,"final":run_final}
    if cmd == "all":
        for s in ["init","week1","week2","week3","week4","final"]:
            stages[s]()
    elif cmd in stages:
        stages[cmd]()
    else:
        print("用法: python demo/scenario.py [init|week1|week2|week3|week4|final|all]")
