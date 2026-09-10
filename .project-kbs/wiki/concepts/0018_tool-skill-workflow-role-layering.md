---
type: concept
title: 工具/技能/角色分层架构（workflow 是 skill 的编排用法）
slug: 0018_tool-skill-workflow-role-layering
aliases:
  - 分层架构
  - 能力分层
  - tool/skill/role
tags:
  - architecture
  - tools
  - skills
  - agent
status: active
created: 2026-07-22
updated: 2026-07-22
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0007_skills-system.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/concepts/0017_role-based-subagents.md
---

# 工具/技能/角色分层架构（workflow 是 skill 的编排用法）— ⚠️ 角色层已废弃

> **角色层已废弃（2026-07-22）**：角色化方案回退（见 [[0017]]）。当前架构回归 **tool / skill 两层 + ad-hoc delegate**（主 agent 按需 `request_tools` + `skill_view`，真需隔离才 `delegate_task` 无 role）。本文角色层内容（角色配置/主子差别）作历史记录；`subagent_blocked` 标签 + `is_subagent` 属性（子 agent 边界）仍保留。

## 摘要

xihe 能力体系**三层**，正交组合：

| 层 | 是什么 | 例子 |
|---|---|---|
| **tool** | 原子能力 | browser_navigate、ssh_exec、terminal、read_file |
| **skill** | 知识/流程积木（组合 tool；**编排型 skill = workflow**） | ssh-access、cmdb-query-variable、security-ticket-monitor |
| **角色** | 执行体（绑定 toolset + skill + mcp + prompt，被委派） | web-ops、server-ops |

**workflow 不是单独机制** —— 它是 **skill 的一种用法**（编排型 skill，body 写完整业务流程，放 `workflows/` 分类）。skill 已能编排 tool + 引用别的 skill（agent view），不需单独 workflow 层。

角色是执行体（被委派，隔离上下文）。skill（含编排型/workflow）是知识/流程（agent view 按它跑）。两者正交。

## 分层关系图

```
╔══════════════════════════════════════════════════════════════╗
║  ① 知识 / 流程层（Skill）                                     ║
║                                                              ║
║   编排型 skill (= workflow)       原子 skill                 ║
║   security-ticket-monitor         ssh-access / cmdb-query /  ║
║     body 写完整流程(8步)          get-stories / plan / ...   ║
║     引用别的 skill ──view──►      (progressive disclosure)   ║
║         │ 描述用法                                           ║
╠══════════════════════════════════════════════════════════════╣
║  ② 执行体层（Agent / 角色）                                   ║
║                                                              ║
║   ┌─────────┐ delegate ┌─────────┐ delegate ┌─────────┐      ║
║   │ 主Agent │──role───►│ web-ops │◄────────│server-ops│     ║
║   │DEFAULT_ │          │core,web,│         │core,ssh  │     ║
║   │TOOLSETS │          │media    │         │          │     ║
║   │+mcp差集 │          │cmdb,    │         │ssh-access│     ║
║   │skill索引│          │intranet │         │          │     ║
║   └────┬────┘          └────┬────┘         └────┬────┘      ║
║        │ view skill         │ 调tool             │ 调tool    ║
║   cron Agent（全工具，view skill 直接跑）                     ║
╠══════════════════════════════════════════════════════════════╣
║  ③ 能力层（Toolset / Tool / MCP）                             ║
║                                                              ║
║   Toolset ──含──► Tool                                       ║
║   core → terminal/read_file/write_file/patch/search_files.. ║
║   web → browser_*(30+)  ssh → ssh_*  media → vision/ocr..   ║
║   MCP server → mcp-{server}（动态注册，未绑定角色的挂主Agent）║
╚══════════════════════════════════════════════════════════════╝
```

## 关系类型

| 关系 | 含义 | 例子 |
|---|---|---|
| view | agent 加载 skill（编排型 skill body 引用别的 skill → agent 再 view） | 主Agent ⟶ security-ticket-monitor ⟶ get-stories |
| 委派 | 主 agent 起角色子 agent（隔离上下文） | 主Agent ⟶ web-ops |
| 绑定 | 角色构造时注入 toolset+skill+mcp | web-ops 绑 cmdb/intranet-sites |
| 调 tool | agent 执行时调原子工具 | web-ops ⟶ browser_navigate |
| 含 | toolset 分组 tool | web toolset ⟶ browser_* |
| 注册 | MCP server 启动注册成 toolset | er-llm ⟶ mcp-er-llm_* |

## 角色配置速查

| | 主 Agent | web-ops | server-ops |
|---|---|---|---|
| toolset | core,memory,comm,agent + mcp差集 | core,web,media | core,ssh |
| skill | 索引（全 − 角色绑定） | cmdb-query-variable, intranet-sites（注入） | ssh-access（注入） |
| mcp | 未被角色绑的 | 无 | 无 |
| 触发 | 入口（wecom/feishu/cron 全工具） | delegate_task(role=) | delegate_task(role=) |

## 子 agent 的 tool 边界（`subagent_blocked` 标签 + `is_subagent`）

tool 注册时声明 `subagent_blocked`（默认 False，少数危险 tool 标 True）：`delegate_task`（递归）、`skill_manage`（改全局 skill）、`send_message`/`send_image`/`clarify`（无对话通道）、`cronjob`（不该动调度）。`registry.get_schemas(subagent=True)` 按标签过滤——**一套机制**，不再 toolset+tool 两套 block 列表。加新 blocked tool 只需注册时标 `subagent_blocked=True`。

主/子判断用 `XiheAgent.is_subagent`（构造属性，子 agent 构造时传 `True`，主 agent 默认 `False`）。与 `delegate_depth`（递归深度）独立——depth 管 `MAX_DEPTH` 防递归，is_subagent 是身份属性管主/子行为（skip/过滤）。

子 agent 能用 `skill_view`/`skills_list`/`todo`（未标 blocked），skill 获取与主 agent 对齐（progressive disclosure）；角色绑定 skill 仍构造注入为主路径（角色 toolset 通常不含 `agent`）。详见 [[0017_role-based-subagents]]。

## workflow = 编排型 skill（不是单独机制）

- skill body 能写**完整编排流程**（步骤 + tool 用法 + 引用别的 skill）
- security-ticket-monitor 就是编排型 skill（`workflows/`），body 写 8 步完整流程
- 引用别的 skill 时，agent 按 body 指导自己 `skill_view` 加载（progressive disclosure）
- **不需单独 workflow 机制**：曾考虑给 workflow 加 frontmatter `skills` 字段 + view 时自动注入引用 skill，但调研发现 **skill 已是 workflow 的功能替代品**（编排 body + 引用 agent view），自动注入只是优化、非必需，故不实现

## skill vs 角色（正交两维度）

- **角色 = 执行体**（who：带 toolset，被委派，隔离上下文，单领域专注）
- **skill（含编排型/workflow）= 知识/流程**（how：agent view 按它跑，当前上下文，跨领域编排）

角色可 view skill 按它跑；编排型 skill 步骤可委派角色（toolset 外的能力）。两者协同：skill（流程）指导，遇到 toolset 外的能力就委派对应角色。

## 一条完整链路（举例）

- 用户「查 CMDB 变量 X」→ 主Agent 看角色清单 → `delegate_task(role="web-ops", goal="查 CMDB X")` → web-ops 子Agent（browser + cmdb-query-variable 注入）跑 → 返 summary
- 用户「处理安全工单」→ 主Agent `skill_view("security-ticket-monitor")` → 编排型 skill（完整流程）→ 按步骤（browser 委派 web-ops，代码自己 search_files，内部 CLI skill 自己 terminal），需要时 view 引用的 get-stories

## 相关页面

- [[0002_tool-registry-and-dispatch]] — tool 注册 + toolset（能力层基础）
- [[0007_skills-system]] — skill 机制（progressive disclosure，编排型 skill 复用）
- [[0013_toolset-scope-and-dynamic-expansion]] — toolset 分组 + 按需展开
- [[0017_role-based-subagents]] — 角色（执行体层）
