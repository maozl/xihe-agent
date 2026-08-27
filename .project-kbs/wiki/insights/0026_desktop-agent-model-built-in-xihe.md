---
type: insight
title: 桌面端 Agent 模型定调——内置 xihe + 可添加 claude（非多实例 / 非 persona）
slug: 0026_desktop-agent-model-built-in-xihe
aliases:
  - desktop agent model
  - 内置 xihe
  - agent 类型
  - main 托管 serve
tags:
  - architecture
  - desktop
  - control-plane
  - decision
status: active
created: 2026-08-11
updated: 2026-08-11
confidence: high
derived_from:
  - wiki/concepts/0025_desktop-control-plane.md
  - wiki/concepts/0017_role-based-subagents.md
  - wiki/concepts/0018_tool-skill-workflow-role-layering.md
related_pages:
  - wiki/concepts/0025_desktop-control-plane.md
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/concepts/0017_role-based-subagents.md
  - wiki/concepts/0018_tool-skill-workflow-role-layering.md
---

# 桌面端 Agent 模型定调——内置 xihe + 可添加 claude（非多实例 / 非 persona）

## 摘要

2026-08-11 与用户定调 xihe-desktop 的 **agent / workspace / manage** 三者关系。核心结论：**Agent = 几种「类型」，不是几个 xihe 进程，也不是几种人格。**

- **xihe** = 桌面**内置**默认 agent，始终在。实现锚点：桌面 **main 进程托管 `xihe serve` 子进程生命周期**（启动 / 健康检查 / 崩溃重启 / 退出清理）——用户永不手敲 `xihe serve`、永不在 UI 见到 "serve" 字样。
- **claude** = **可添加**的外部 agent 类型（connector，凭据接入）。**当前只留 IA 槽位，不实现 connector**（见 F1 范围）。
- **不是**多实例（多个并行 `xihe serve`）；**不是**多 persona（persona 层已在 xihe-agent 侧回退，见 [[0017_role-based-subagents]] / [[0018_tool-skill-workflow-role-layering]]）。

这条定调**实质性地推翻了 [[0025_desktop-control-plane]] 的 Agent 层建模**（原三层模型把 Agent 层定义为「一个 provider 下的 persona / 实例」，并种了 3 个 demo agent `xihe-ops`/`xihe-research`/`claude-dev`）。本决策落地后 [[0025]] 的控制面 / 能力驱动 / 三段式 / store / 协议引用等仍然有效，**唯独 Agent 层语义需按本文订正**。

## 决策内容

### Agent 模型：类型，而非进程数 / 人格

| 维度 | 结论 |
|------|------|
| **xihe** | 内置默认 agent。桌面 main 托管 `xihe serve` 子进程。始终可用。 |
| **claude** | 可添加 agent 类型（connector）。IA 预留，**本阶段不实现**。 |
| 多实例（多 xihe 进程） | ❌ 不做。xihe 是唯一的内置引擎，没有「再起一个 xihe」的概念。 |
| 多 persona | ❌ 不做。persona 层在 xihe-agent 侧已回退（[[0017]]/[[0018]]），桌面侧不再为其预留 `xihe-research` 这类假槽。 |

> 用户原话：「xihe 应该是桌面端内置的一个 agent，claude 则是另一个 agent 类型的可选项。」
> （我最初猜的「xihe 多实例」方向被用户当场否决——见备选方案。）

### 内置范围：main 托管 xihe 进程

桌面 **main 进程完全拥有 `xihe serve` 的生命周期**，而不是把 serve 当成一个用户需要自己启动的外部后端：

- spawn `xihe serve` 子进程；健康检查（`/health`）；崩溃重启；应用退出时清理子进程。
- **renderer↔serve 通道不变**：渲染层仍直连 `127.0.0.1:7788`（[[0024_desktop-serve-protocol]] 的 REST/WS 契约不动）。
- main→renderer 通过 IPC（如 `xihe:status`）汇报 serve 健康，渲染层据此切 demo↔live。
- **凭据归属不变**：xihe 读自己的 `~/.xihe-agent/.env`；桌面 main 永不触碰、永不代理 `api_key`/`base_url`（与 [[0025]] 「对话真理归属分形态」一致）。

### Workspace：项目文件夹 / 用户资产（与 agent 正交）

- Workspace = 一个**可复用的工作目录**，用户可把它绑定到会话（设 cwd）。已按 P3 现状落地（`convWorkspace` 绑定 + `sendTurn(cwd)` 经 [[0024]] 透传到 agent）。
- 定位为**一等公民**：去掉 [[0025]] 里「高级用户 / 隐藏」措辞。Workspace 与 agent **正交**——任意会话都可绑任意 workspace，不依赖 agent 类型。

### Manage：范围待定（deferred）

- 用户明确「这个晚点确认」，本决策**不锁定** Manage 的边界。
- 现状：ManagePanel 已**只读**接了 MCP / skills / cron（进程级，serve 端点 `/mcp`/`/skills`/`/cron`）。凭据节保持静态信息（加密存储、桌面不回显）。

## 备选方案对比

| 方案 | 含义 | 结论 |
|------|------|------|
| **Agent = 类型（内置 xihe + 可添加 claude）** ✅ | 用户不感知进程边界，xihe 永远在；claude 是可选的外接 | **采纳** |
| Agent = xihe 多实例 | 用户可起多个并行 `xihe serve`（不同 config / 数据根） | ❌ 否决（用户）。过度复杂，违背「内置默认 agent」心智；多实例需求由 xihe-agent 侧 `--config`（[[0023_multi-instance-config]]）覆盖，不必上桌面 UI |
| Agent = persona | 一个 xihe 进程挂多 persona（`xihe-research` 等） | ❌ 否决。persona 层已在 xihe-agent 回退（[[0017]]/[[0018]]：路由负担 / 隔离副作用 / 边界模糊），桌面不为已回退的机制留假槽 |

## 适用边界

- 本结论约束 **xihe-desktop**（`E:\xihe-desktop`）的产品建模与 UI 花名册。
- 不约束 xihe-agent 侧：xihe-agent 仍是单进程多模式（CLI / gateway / serve），`--config` 多实例能力（[[0023]]）独立保留。
- claude connector 的具体接入协议（凭据金库、按会话绑凭据、对话真理归属 provider）等真正实现时再定；现在只锁「它是可添加的 agent 类型」这一占位语义。

## F1 工作流（实施方向，非本文落地）

本决策的直接下游是 **F1**（下一步实现，单独出计划）：

1. **main 托管 xihe serve 生命周期**：spawn / health / restart / cleanup 子进程；main→renderer `xihe:status` IPC。
2. **花名册重构**：`SEED_AGENTS`（3 个 demo）→ 收敛为**单一内置 xihe**（live）+ **claude 可添加占位**（IA 槽，disabled）。删 `xihe-research` persona 假槽。
3. **去 "serve" 措辞**：UI / 文案不再暴露 "serve" 字样（连接状态、demoReply、空态提示等）——对用户而言只有「xihe」。

## 风险与缓解

- **风险**：main 托管子进程增加生命周期复杂度（进程泄漏、端口占用、僵尸进程）。
  **缓解**：app 退出（`before-quit`）必杀子进程；启动前探针端口；崩溃带退避重启。
- **风险**：claude 槽位长期是占位，可能误导成「即将支持」。
  **缓解**：UI 明确标注「未接入 / 占位」，不在文案里承诺时间。

## 相关页面

- [[0025_desktop-control-plane]] —— 桌面控制面设计；本文推翻其 Agent 层建模（persona / 3 种子 agent / serve 显式暴露），其余仍有效
- [[0024_desktop-serve-protocol]] —— renderer↔serve 通道契约（F1 不动）
- [[0017_role-based-subagents]] —— persona/角色化在 xihe-agent 侧回退的依据（本文据此不在桌面留 persona 假槽）
- [[0018_tool-skill-workflow-role-layering]] —— 角色层废弃记录（同上）
- [[0023_multi-instance-config]] —— xihe-agent 多实例能力（独立保留，桌面不做多实例 UI 的边界依据）
