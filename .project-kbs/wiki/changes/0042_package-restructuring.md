---
type: change
title: 包结构重构——serve 业务模块化、app 启动器、registry 契约下沉、core 子包化与基础设施对流
slug: 0042_package-restructuring
change_type: refactor
risk_level: high
status: completed
created: 2026-09-01
updated: 2026-09-01
affected_modules:
  - src/app/main.py
  - src/core/registry.py
  - src/core/agent/
  - src/core/services/
  - src/core/support/
  - src/gateway/serve/
  - src/gateway/platforms/
  - src/tools/__init__.py
related_insights:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
---

# 包结构重构——serve 业务模块化、app 启动器、registry 契约下沉、core 子包化与基础设施对流

## 摘要

按用户定调的**三原则**（① 高内聚低耦合 ② 依赖单向流动成 DAG、严禁循环导入 ③ 按变化原因分组）对 `src/` 做四步重构：serve 按业务分模块、启动器独立成 `app/`、工具注册表契约下沉 `core/registry.py`、core 子包化（`agent/` + `services/` + `support/`）并让 tools 的历史基础设施对流上移。全程零功能变化，pytest 466 绿 + serve 冒烟 200。

## 设计依据：三原则

1. **高内聚低耦合**：同一包围绕同一职责；包间依赖少而清晰（契约放在双方都要依赖的层）。
2. **单向 DAG**：接口层 → 业务层 → 数据层 → 公共层，严禁循环导入。重构后实测依赖方向：`core/support/` 仅 import 标准库（DAG 底）；core 根层契约（`logging_config`/`session` → `config`，`registry` → `support`）；`core/agent/` → 根层；`core/services/` → 根层 + `support`；**core 不得在模块级 import tools**（import tool 模块 = 注册副作用），需要时函数级延迟 import，组合根（`core.context`）调 `load_all_tools()`。
3. **按变化原因分组**：引擎随循环逻辑变（`agent/`）、特性域各自演进（`services/`）、零依赖机器随横切关注点变（`support/`）。

## 变更内容

### 1. serve 按业务分模块（此前已落地）

`serve_resources/` 并入 `gateway/serve/`，一个业务一个模块、各自持有 `add_routes`：`app.py`（ServeApp 状态 + 系统探针）/ `chat.py`（/stream 回合引擎 + 会话历史）/ `admin.py`（管理面板 + 专家 + 商店）/ `browser.py`（含 `browser_window.py` Win32 吸附）/ `terminal.py`（ssh 面板）/ `knowledge.py`（记忆 + KBS）；`_common.py` + `emitter.py`（worker→WS 桥）是管道；`server.py` 组装。

### 2. `app/` 启动器 + platforms 归位

`cli/app.py` + `cli/__main__.py` 承载的不只是 CLI——还有 gateway/serve 分发——抽出为 **`src/app/main.py`**（argparse 分发三模式：`cmd_chat`/`cmd_gateway`/`cmd_serve`/`cmd_cron`/`cmd_doctor`）；`platforms/` 包移入 **`gateway/platforms/`**（平台适配只服务 gateway）；`cli/` 回归纯 CLI 面（`chat.py` + `doctor.py`）。

### 3. registry 契约下沉 `core/registry.py`

`ToolRegistry` / `registry` 单例 / `tool_result` / `tool_error` / `_run_async` 从 `tools/__init__.py` 移到 **`core/registry.py`**——契约要放在引擎（core）与插件（tools）两侧都依赖的层。`tools/__init__.py` 只留 re-export + `load_all_tools()` / MCP 后台发现生命周期，40 个 tool 模块的 `from tools import registry, tool_result, tool_error` 短式**零改动**。dispatch 内的审批门（`evaluate`/`remember_rule`）与路径重写（`resolve_path`）随之提为模块级 import（来自 `core.support`，零依赖例外）。

### 4. core 子包化（21 文件 → 根 8 + 三个子包）+ 基础设施对流

- **`core/agent/`** 引擎（变化原因 = agent 循环逻辑）：`agent.py`（1452 行）、`prompts.py`、`prompt_context.py`、`compressor.py`、`auxiliary_client.py`、`model_catalog.py`、`title_generator.py`。包 `__init__` re-export `XiheAgent`，7 处 `from core.agent import XiheAgent` 零改动。
- **`core/services/`** 特性域（变化原因 = 各特性自身演进）：`external_agent.py`、`store.py`、`agent_defs.py`、`diagnostics.py`。
- **`core/support/`** 零依赖公共机器（DAG 底，仅标准库）：`approvals.py`、`paths.py`、`interrupt.py`、`redact.py`、`tool_result_storage.py`、`proc_utils.py`、`safe_env.py`。
- core 根层留**契约**：`config.py`、`session.py`、`registry.py`、`toolsets.py`、`context.py`（组合根）、`logging_config.py`、`version.py`。
- **对流（tools → core.support）**：`tools/_approvals.py` → `core/support/approvals.py`（去下划线）、`tools/_paths.py` → `core/support/paths.py`、`tools/tool_result_storage.py` → `core/support/`——它们的消费方是 core 引擎 + 三模式，不是 tools 自己，留在 tools 造成 core 反向够进 tools。tools 40 → 35 文件（仅 `_ssh_tap.py` 保留为下划线基础设施）。

## 兼容性策略

- `git mv` 保 rename 历史；`pyproject.toml` 的 `core*`/`gateway*` glob 天然覆盖新子包，零打包改动。
- import 迁移：一轮 18 模式 sed + 2 处定点（`from core import X` 点式形态会逃过点路径 sed）；`from core.agent import XiheAgent` 靠包 re-export 零改动。

## 验证

- pytest **466 passed / 20 skipped / exit 0**（重构前后各跑一轮全绿）。
- 全量 `py_compile` 通过；分层 import 冒烟（load_all_tools 后 91 工具注册）。
- `xihe --version` 正常；serve 起动冒烟：`/health` `/readiness` `/agents` `/sessions` 全 200（临时端口，进程已清理）。
- 桌面端无 Python 路径硬引用（`XIHE_BIN` 走 PATH），无需改动。

## 影响

- gateway / serve 长驻进程需重启生效；桌面端点「重启 xihe」。
- 无 API / 协议 / 配置变化；`config.example.yaml`、CLAUDE.md、AGENTS.md、README 双语的结构树已同步。
