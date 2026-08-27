---
type: change
title: 三层名单统一 + specialists.enabled 闸门 + 能力描述符收敛
slug: 0035_three-layer-roster-unification
change_type: refactor
risk_level: medium
status: completed
created: 2026-08-17
updated: 2026-08-17
affected_services:
  - xihe-agent
affected_modules:
  - core/toolsets.py
  - core/agent.py
  - core/agent_defs.py
  - core/config.py
  - cli/app.py
  - gateway/server.py
  - gateway/serve.py
  - tools/specialist_agent_tool.py
  - tools/delegate_tool.py
  - desktop/src/main/xiheConfig.ts
  - desktop/src/renderer/src/components/SettingsPanel.tsx
---

# 三层名单统一 + specialists.enabled 闸门 + 能力描述符收敛

## 摘要

主/专家/delegate 三层 agent 的 tool/skill/MCP 名单收口为一套语义：主 agent 从 config.yaml 顶层 `toolsets`/`skills` 键实例化（与专家共用 `resolve_roster`，删除全部 main 专属逻辑）；新增 `specialists.enabled` 总闸（默认关）；delegate 名单独立于父 + `subagent_blocked` 安全收口；serve 能力描述符按主名单收敛；桌面暴露 specialists 开关。架构沉淀见 [[0034_three-layer-agent-roster]]。

## 变更内容

### 统一解析（core/toolsets.py）

- 新 `resolve_roster(spec, where, warnings)`：三态语义（不写/`[]`→不加载+告警；`["*"]`→None 全量；名单→白名单，`mcp`/`mcp-<server>` 永远保留）。
- 新 `normalize_toolset_names`：校验/去未知名/非字符串剔除/`*` 归一。
- **删除** `resolve_main_toolsets` / `resolve_main_skills` / `select_toolsets` / 旧 `DEFAULT_TOOLSETS`（main 专属逻辑清零）。

### 关键修复（core/agent.py）

- `enabled_toolsets` 构造从 `if enabled_toolsets`（truthiness，`[]`→None = 全量，语义反转）改为 **`is not None`**。统一语义的「不写=不加载」在此 bug 下实际是「全量」。

### 专家闸门（tools/specialist_agent_tool.py + core/config.py）

- 新 `specialists_enabled(config)`（读 `specialists.enabled`，**默认 false**）；`register_specialist_agent_tools()` 顶部闸口：关 = 不注册 `run_*_agent` + 花名册层自动消失（按可调用工具过滤）。
- config.py 白名单扩展：顶层键加 `toolsets`/`skills`；section 加 `specialists`（**拷贝循环 + setdefault 循环两处**，漏一处静默消失）。
- `GET /specialists` 返回 `specialists_enabled`（区分「配置关」vs「待重启」）。

### 主 agent 接线（cli/app.py + gateway/server.py + gateway/serve.py）

- `SharedContext.__init__` 解析 `main_toolsets`/`main_skills` 存一次；`create_agent(enabled_toolsets, cwd, skills_allowed)`；三入口（CLI/gateway/serve）全走它。`ServeApp` 改收 `shared_ctx`。
- **`_capabilities(toolsets)` 收敛**：从裸读 `registry._tools` 改为 `get_schemas(toolsets=主名单)`（roster + check_fn 同视图）——slim 主 agent 不虚报 browser/vision/mcp。

### delegate（tools/delegate_tool.py）

- `_resolve_allowed_toolsets(requested)`：删父名单交集参数；缺省→DEFAULT_TOOLSETS 七组；`["*"]`→None；全非法→回退默认。
- schema 描述的 blocked 清单修正：5 个 → 实际 12 类（补 external_agent/kbs_init/run_*_agent/web_record/browser_record/send_image）——模型看不到完整清单会在委派 prompt 里给子 agent 布置它用不了的工具。

### 桌面（desktop/）

- `xiheConfig.ts`：VALUE_SPECS 加 `specialists_enabled → [specialists, enabled]`（default false）；`desktop.ts` XiheConfig/XiheConfigPatch 两接口同步。
- `SettingsPanel.tsx`：能力开关卡新增「专家 agent 委派」Toggle（buildPatch 行级 YAML patch）；SpecialistsCard 加琥珀横幅（`specialists_enabled=false` 时提示去能力开关打开，区分「待重启」角标）。
- `serveClient.ts`：`SpecialistsInfo.specialists_enabled`。

### 文档

- CLAUDE.md：roster 模型 bullet 重写（主=顶层键/一个 resolver/delegate 独立）、specialists 闸门段落、Gotchas 加 `[]` vs `None`、config sections 列表订正（删虚构的 `agents`、补 `specialists`）。
- `tests/test_roster.py`：normalize/resolve/AgentDef 解析/load_config 透传/闸门三态/XiheAgent 三态/delegate 三态。

## 变更分析

### 设计迭代（三次用户纠正，终态=最小概念量）

内置 DEFAULT_MAIN_TOOLSETS 兜底 → 「不需要 main」；`main:` section / agents/main.yaml 保留 slug → 「主 agent 不需要单独搞一个 agent，读根目录 config.yaml 实例化就行」；终态：顶层键 + 同一个 resolve_roster。论据：主/专家配置项基本一致，main 专属逻辑是无谓分叉。

### 破坏性影响（存量部署）

- config.yaml **不写 `toolsets`** → 主 agent 无工具（原为全量）；要全量须显式 `["*"]`。
- **不写 `specialists.enabled`** → `run_*_agent` 消失（原为有 yaml 即注册）。日志有 "Specialist dispatch off (specialists.enabled not set)" 可查。
- 旧专家 yaml 若依赖「缺省 `[files, memory]`」→ 现在缺省=不加载（告警）。
- 运行中的 serve/gateway 需重启（名单在 SharedContext 启动时解析、schema 按进程缓存）。

## 验证

- pytest 41 通过（含新 test_roster.py 全组）。
- 三层端到端 walkthrough（临时脚本）：L1 主 agent slim 名单 13 工具含 run_sec_agent/delegate_task；L2 专家 [dev_tool, terminal]→5 工具无泄漏；L3 delegate 默认 49 工具、`["*"]`→None、blocked 12 类核对。
- `_capabilities` 三态实测：slim→无 browser/vision；None→有；[]→基础六项。
- 桌面 `npm run build` 三 bundle 绿。
- 用户实机：7 组 → 49 工具（32 MCP + slim 核心 + run_itsm_agent）。

## 相关页面

- [[0034_three-layer-agent-roster]] — 三层名单模型（本变更沉淀的稳定概念）
- [[0032_specialist-agents]] — 专家层架构（本文档化前的专家知识，toolsets 缺省语义已被本文订正）
- [[0033_specialist-toolset-overhaul]] — 前置变更（14 平铺组、serve CRUD、桌面编辑器）
