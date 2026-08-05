#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  GovernanceOps — 演示脚本
#
#  用法: bash demo/run.sh
#  时长: ~10 分钟 (按回车推进每一步)
# ═══════════════════════════════════════════════════════════════

set -e
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
BOLD='\033[1m'

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV_PYTHON="$ROOT/venv/bin/python"
OPS="$ROOT/venv/bin/govops"

pause() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    read -p "$(echo -e ${YELLOW}'[按 Enter 继续]'${NC}) "
    echo ""
}

section() {
    echo ""
    echo -e "${BOLD}${BLUE}════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}  $1${NC}"
    echo -e "${BOLD}${BLUE}════════════════════════════════════════════════════════${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
section "第0步: 清理环境 + 项目结构展示"
# ═══════════════════════════════════════════════════════════════

rm -rf data
echo -e "${BOLD}项目结构:${NC}"
echo "  src/governanceops/          ← 唯一代码包"
echo "    ├─ cli/                   ← CLI 模块"
echo "    ├─ web/                   ← Web 模块 (FastAPI + SSE + SPA)"
echo "    ├─ ontorepo/              ← Ontology Repo (DSL/版本化/符合性)"
echo "    ├─ sources/  commands/    ← 数据源接入 / 命令"
echo "    └─ resources/ontology/    ← 本体 DSL + schema + PROTOCOL (随包分发)"
echo "  demo/  tests/               ← 演示 / 测试"
echo ""
echo -e "${BOLD}可用命令:${NC}"
$OPS --help 2>&1 | grep "  " | head -10
echo "  ..."
pause

# ═══════════════════════════════════════════════════════════════
section "第1步: 场景初始化 — 国信数科 12个决策"
# ═══════════════════════════════════════════════════════════════

echo "国信数科是国信集团下属数字科技公司。"
echo "2026年6月，12个核心决策，5个部门，4次周会。"
echo ""
echo -e "${YELLOW}执行: python demo/scenario.py init${NC}"
echo ""
$VENV_PYTHON demo/scenario.py init
pause

# ═══════════════════════════════════════════════════════════════
section "第2步: 第1周 — 看似正常，但暗藏问题"
# ═══════════════════════════════════════════════════════════════

echo -e "${YELLOW}执行: python demo/scenario.py week1${NC}"
echo ""
$VENV_PYTHON demo/scenario.py week1
pause

echo -e "${RED}Ops 发现了什么?${NC}"
echo "  ❌ 3个决策无负责人 (260603 数据中台/260607 培训/260612 协作流程)"
echo "  🔴 260605 供应商合同已超期 (deadline 6/5)"
echo ""
echo -e "${YELLOW}执行: ops attention${NC}"
echo ""
$OPS attention 2>&1 | head -25
pause

# ═══════════════════════════════════════════════════════════════
section "第3步: HermiT 本体推理"
# ═══════════════════════════════════════════════════════════════

echo "HermiT 是 OWL2 DL Tableau 推理机。"
echo "它会自动将决策分类到 OWL defined classes 中。"
echo ""
echo -e "${YELLOW}执行: ops hermit${NC}"
echo ""
$OPS hermit 2>&1
pause

# ═══════════════════════════════════════════════════════════════
section "第4步: 第2周 — 问题浮现"
# ═══════════════════════════════════════════════════════════════

echo -e "${YELLOW}执行: python demo/scenario.py week2${NC}"
echo ""
$VENV_PYTHON demo/scenario.py week2 2>&1 | tail -8
pause

echo -e "${RED}Ops 发现了什么?${NC}"
echo "  🔴 超期翻3倍: 260604等保(超4天) 260605合同(超9天) 260610审计(超2天)"
echo "  ⏸ 6个决策停滞>7天"
echo "  ❌ 260603/260607/260612 仍然无负责人"
echo ""
echo -e "${YELLOW}执行: ops compile${NC}"
echo ""
$OPS compile 2>&1 | head -20
pause

# ═══════════════════════════════════════════════════════════════
section "第5步: 第3周 — 危机爆发"
# ═══════════════════════════════════════════════════════════════

echo "第3周: 陈财务请假，260605供应商合同无人跟进。"
echo "260604等保合规deadline已超11天，仍未处理。"
echo ""
echo -e "${YELLOW}执行: python demo/scenario.py week3${NC}"
echo ""
$VENV_PYTHON demo/scenario.py week3 2>&1 | tail -8
pause

echo -e "${RED}关键拦截:${NC}"
echo "  6/19 陈财务请假 → 260605超期14天 → 自动升级通知张总"
echo "  6/19 张总指定李经理协调采购部 → 人治断裂处，系统自动补位"
echo "  🔗 260608超期 → 260602云迁移自动联动升级"
pause

# ═══════════════════════════════════════════════════════════════
section "第6步: 第4周 + 月底复盘"
# ═══════════════════════════════════════════════════════════════

echo -e "${YELLOW}执行: python demo/scenario.py week4${NC}"
echo ""
$VENV_PYTHON demo/scenario.py week4 2>&1 | tail -8
echo ""
echo -e "${YELLOW}执行: python demo/scenario.py final${NC}"
echo ""
$VENV_PYTHON demo/scenario.py final 2>&1
pause

# ═══════════════════════════════════════════════════════════════
section "第7步: 周会简报"
# ═══════════════════════════════════════════════════════════════

echo "一键生成周会简报，看板式呈现。"
echo ""
echo -e "${YELLOW}执行: ops brief${NC}"
echo ""
$OPS brief 2>&1
pause

# ═══════════════════════════════════════════════════════════════
section "第8步: 管理优先级 (规则驱动)"
# ═══════════════════════════════════════════════════════════════

echo "P0-P4 完全由规则推断，不由人定:"
echo "  P0 = Governed(hasOwner+hasDeadline+hasUpdate) ∧ deadline≤7天 ∧ risk=HIGH"
echo "  P1 = Governed"
echo "  P2 = hasOwner + hasDeadline"
echo "  P3 = hasOwner"
echo "  P4 = 无owner"
echo ""
echo -e "${YELLOW}执行: ops attention${NC}"
echo ""
$OPS attention 2>&1 | head -25
pause

# ═══════════════════════════════════════════════════════════════
section "第9步: Web 多端演示 (可选)"
# ═══════════════════════════════════════════════════════════════

echo "Web 模式 — 5个Tab模拟多交互端:"
echo "  Tab1 📥 输入 — 粘贴周报/邮件"
echo "  Tab2 🎤 会议 — 聊天式交互"
echo "  Tab3 📊 看板 — 自动刷新"
echo "  Tab4 🔔 通知 — SSE实时推送"
echo "  Tab5 📋 日志 — 全事件时间线"
echo ""
echo -e "${YELLOW}启动: govops serve${NC}"
echo "  然后打开浏览器 → http://localhost:8000"
pause

# ═══════════════════════════════════════════════════════════════
section "演示完成"
# ═══════════════════════════════════════════════════════════════

echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  GovernanceOps 演示完成${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo "核心亮点:"
echo "  1. 本体推理 (owlready2 + HermiT Tableau)"
echo "  2. Agent 提取 (LangGraph + LLM)"
echo "  3. 事件驱动 (SSE 实时推送)"
echo "  4. 规则驱动优先级 (不由人定)"
echo "  5. 闭环治理 (extract → validate → refine)"
echo ""
echo "技术栈: Python • LangGraph • owlready2 • HermiT • FastAPI • SSE"
