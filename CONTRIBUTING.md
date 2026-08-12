# Contributing to MOSS

感谢你对 MOSS 的兴趣！本文档描述开发流程和规范。

## 许可

本仓库采用**分层许可**：

| 资产类型 | 许可协议 | 说明 |
|---------|---------|------|
| **代码** | [MIT](LICENSE) | 自由使用、修改、商用，无需 CLA |
| **协议规范** | [CC BY 4.0](src/moss/resources/ontology/LICENSE) | 署名 + 允许商业实现 |

`src/moss/resources/ontology/` 下的协议规范（PROTOCOL.md、schema、conformance）的权威源在 [OntologyOps 仓库](https://github.com/georgewangchn/OntologyOps)，无论从哪个仓库获取，协议许可始终为 CC BY 4.0。

- **代码贡献**（MIT）：无需 CLA，MIT 已授权商用
- **协议贡献**（CC BY 4.0）：提交即表示您授权您的贡献以 CC BY 4.0 许可发布，与协议规范保持一致

项目名称使用指引见 [TRADEMARK.md](TRADEMARK.md)。

---

## 开发环境

```bash
# 克隆仓库
git clone https://github.com/georgewangchn/MOSS.git
cd MOSS

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装完整开发依赖（含 langchain/langgraph + 测试工具）
pip install -e ".[agent,dev]"
```

### 可选依赖

| Extras | 说明 | 安装命令 |
|--------|------|----------|
| `[agent]` | LangGraph Agent 提取（并行分块 + refine 回环） | `pip install -e ".[agent]"` |
| `[dev]` | 开发工具（pytest, ruff, mypy） | `pip install -e ".[dev]"` |
| `[java]` | HermiT 完整推理（需系统 Java 11+） | 安装 JDK 后自动检测 |

---

## 项目结构

```
src/moss/         # 唯一代码包（src 布局）
  config.py                # 配置管理（工作区：cwd 有 .env 则 cwd，否则 ~/.moss/）
  storage.py               # SQLite 存储后端
  models.py                # 数据模型（Decision, Issue, Update, GovernanceGraph）
  compiler.py              # Governance Compiler — 完备性检查
  runtime.py               # Governance Runtime — 心跳/提醒/状态机
  engine.py                # GovernanceEngine — CRUD + 依赖图 + EventBus
  extractor.py             # 基础提取器（httpx 直调 LLM）
  agent_extractor.py       # LangGraph Agent 提取（可选，延迟导入）
  intent_agent.py  llm_client.py  prompts.py  skills.py
  owl_ontology.py          # OWL 2 DL 本体（从 DSL 编译）+ HermiT 推理
  owl_compiler.py          # 本体编译器
  cli/                     # CLI 入口（main.py + onto_cmd.py）
  web/                     # Web 仪表盘（server.py + event_bus.py + static/）
  ontorepo/                # Ontology Repo 支柱（OntologyOps 参考实现）
    model.py loader.py export.py cwa.py store.py diff.py conformance.py
  sources/                 # 数据源接入层（base / file / email）
  commands/                # CLI 子命令（init_cmd：init 向导 + demo 种子）
  resources/ontology/      # 本体 DSL + schema + PROTOCOL（随包分发）
tests/                     # pytest
demo/                      # 演示脚本 + 数据
```

---

## 开发规范

### 1. 引擎层冻结原则

`src/moss/` 下的核心模块（compiler, runtime, engine, owl_ontology, owl_compiler, models）是产品壁垒，**不为开源化而改动**。新增功能通过扩展点实现：

- 新增数据源 → 在 `sources/` 下新增 `Source` 子类
- 新增提取场景 → 在 `skills/` 下新增 `SKILL.md`
- 新增 LLM 后端 → 适配 `llm_client.py` 的 `chat()` 接口

### 2. 降级优先

所有可选依赖必须支持降级：
- 无 langchain → 降级到 Extractor
- 无 Java → 降级到纯 Python 推理
- 无 Ollama → demo 模式使用预置数据
- 无 LLM → 不提取，只编译已有数据

### 3. 跨平台

- 不使用 `fcntl`（仅 Unix）— SQLite WAL 已处理并发
- 不使用 `Path(__file__).parent.parent` — 使用 `get_workspace()`
- 文件路径用 `pathlib.Path`，不用字符串拼接

### 4. 提交规范

```
<type>: <description>

type: feat | fix | refactor | docs | test | chore
```

示例：
- `feat: add Gmail OAuth source`
- `fix: skills.py path resolution after pip install`
- `docs: update README with Docker instructions`
- `chore: remove untested Dockerfile and docker-compose.yml`

---

## 测试

```bash
# 运行全部测试
pytest

# 运行特定模块
pytest tests/test_ontology_repo.py

# 覆盖率
pytest --cov=moss
```

### 降级模式测试

确保在无 langchain、无 Java 环境下测试通过：

```bash
# 模拟无 langchain
pip uninstall langchain langgraph -y
pytest tests/test_extractor.py
pip install -e ".[agent]"
```

---

## 添加新的数据源

1. 在 `src/moss/sources/` 下创建新文件
2. 继承 `BaseSource`，实现 `fetch()` 方法
3. 返回 `list[RawDoc]`
4. 在 CLI 中注册 `connect` 子命令

```python
from moss.sources.base import BaseSource, RawDoc

class WebhookSource(BaseSource):
    def fetch(self) -> list[RawDoc]:
        # 从 webhook 接收数据
        return [RawDoc(content=text, source_type="webhook", meta={})]
```

---

## 添加新的提取技能

1. 在 `src/moss/skills/<skill-name>/` 下创建 `SKILL.md`
2. 包含 YAML frontmatter + System Prompt + Extraction Rules
3. 重启后自动加载

```markdown
---
name: contract-review
description: 合同审查 — 提取合同条款中的治理对象
tags: [contract, legal]
---

## System Prompt
\```
You are MOSS Parser for contract documents...
\```

## Extraction Rules
\```yaml
- Each clause → 1 decision
- "甲方负责" → owner
- "签署后X日内" → deadline
\```
```

---

## 发布流程

1. 更新 `pyproject.toml` 版本号
2. 更新 CHANGELOG
3. 创建 git tag `vX.Y.Z`
4. `python -m build && twine upload dist/*`

---

## 联系

- Issues: https://github.com/georgewangchn/MOSS/issues
- Discussions: https://github.com/georgewangchn/MOSS/discussions
- 方法论 / 协议：[OntologyOps](https://github.com/georgewangchn/OntologyOps)
