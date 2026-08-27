---
type: concept
title: 系统提示词装载与三层 Agent prompt 差异
slug: 0036_system-prompt-assembly
aliases:
  - prompt 装载
  - LAYERS 表
  - PromptCtx
  - 分层 prompt
  - 三层 agent prompt 差异
tags:
  - architecture
  - agent
  - prompt
status: active
created: 2026-08-19
updated: 2026-08-19
related_pages:
  - wiki/concepts/0034_three-layer-agent-roster.md
  - wiki/concepts/0032_specialist-agents.md
  - wiki/concepts/0014_project-context-loading.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/changes/0019_kbs-feature.md
---

# 系统提示词装载与三层 Agent prompt 差异

## 摘要

系统提示词由 `core/prompts.py` 的**声明式层表**装配：`build_system_prompt()` 拿一个 `PromptCtx` 顺序走 `LAYERS: list[Layer]`，每层返回该节文本或 `None` 跳过，非 None 按**表序**拼接，最后 `expand_agent_vars` 展开 `${AGENT_HOME}`。所有工具相关层 key off `ctx.tools` —— **指导层永不宣传实际不可调用的工具**。

三层 agent 走**三条不同装载路径**：

| 层 | 装载路径 | prompt 形态 |
|---|---|---|
| 主 agent | `_build_system_prompt` → 完整 LAYERS 表 | 全部四层组（identity + platform + kbs preamble + roster + 纪律 + 按工具指导 + 运行时上下文） |
| 专家 agent | **同一条** `_build_system_prompt` → LAYERS 表 | 完整层组，但 identity 换 yaml persona、无 platform/kbs preamble/roster 层 |
| delegate 子 agent | `chat()` 里 `system_prompt_override` **整段替换，短路分层装配** | 纯任务卡（一句话身份 + YOUR TASK/CONTEXT/WORKSPACE + 汇报要求），无任何指导层 |

名单模型（[[0034]]）决定"agent 手里有什么工具"，本页决定"agent 的 prompt 里看到什么文本"；两者的接口是 `available_tools` 集合。

## 装配管线

```
XiheAgent.chat()  (每轮)
  └─ _build_system_prompt(platform, session_key)
       ├─ available_tools = registry.get_schemas(toolsets=enabled ∪ expansion, subagent=is_subagent) 的工具名集合
       ├─ 主 agent 专属：kbs_preamble（kbs.enabled）+ agent_roster（specialists.enabled，按 available_tools 过滤）
       └─ build_system_prompt(...)          ← core/prompts.py
            ├─ PromptCtx(tools, platform, skills_allowed, cwd, identity_override,
            │            agent_roster, kbs_preamble, kbs_read_note, load_claude_md,
            │            load_cursorrules, project_context)
            ├─ for layer in LAYERS: text = layer(ctx)   # None → 跳过
            ├─ "\n\n".join(非 None 各节)
            └─ expand_agent_vars(...)                    # ${AGENT_HOME} 最后统一展开
```

- **Layer = `Callable[[PromptCtx], Optional[str]]`**。两个工厂覆盖大多数行：`_tool_guard(names, text)`（工具交集非空才注入）、`_passthrough(field)`（ctx 字段直通）。
- **表序 = 节序**。想调整系统提示词里章节顺序，改 `LAYERS` 列表顺序即可，不动任何函数。
- prompt 文本**每轮重建**（含压缩后重建，agent.py 两处 `if not system_prompt_override` 分支）；被持久化到 session 的是重建结果 + 字面 user message，**不是**运行时注入的记忆快照。
- 首次构建会往 agent.log 一次性 dump 完整 prompt（`SYSTEM PROMPT (first build dump)`）——排查"prompt 里到底有什么"先看它。

## 层组与注入条件

`LAYERS` 18 层，分四组。条件列中"在面"指工具名出现在 `ctx.tools`：

| 组 | 层 | 内容 | 注入条件 |
|---|---|---|---|
| Identity | `_identity` | 身份 | `identity_override` > SOUL.md > `AGENT_IDENTITY` |
| | kbs_preamble | kbs 协议前导（`kbs_protocol.md`，`<root>` 实时替换） | 仅主 agent 且 `kbs.enabled`（[[0019]]） |
| | `_platform_prompt` | 平台适配（wecom 4000 字限/禁 markdown 等） | platform 命中 `PLATFORM_PROMPTS`（cli/wecom/feishu/telegram/discord；专家="agent"、delegate 不适用 → 无） |
| Discipline | `_tool_use` | Tool-Use Enforcement + mandatory-tool-use 清单 | `ctx.tools` 非空；清单行各自按工具（terminal→时间/系统状态/git 等） |
| | `_behavior_rules` | Behavior Rules 1-4 | 恒注入（空工具骨架也有）；第 5/6 条分别条件于 model_info/request_tools 在面 |
| Tool guidance | `_memory_guidance` | Memory 节（读/写两行合并单头） | memory 或 memory_manage 在面 |
| | session_search | 会话续接提示 | session_search 在面 |
| | delegate_task | 委派指导 | delegate_task 在面 |
| | agent_roster | 专家花名册 + 路由阶梯 | 仅主 agent 且 specialists.enabled 且 run_*_agent 实际在面 |
| | external_agent | 外部引擎(claude)指导 | external_agent 在面 |
| | browser_login | 浏览器登录流 | browser_login 在面 |
| | cronjob | 定时任务指导 | cronjob 在面 |
| | `_kbs_subagent_note` | kbs 读纪律（候选≠结论） | `kbs_read_note` 标志 **且** kbs_search 在面 → 实际只有专家 agent 命中 |
| | `_mcp_guidance` | MCP 服务器管理提示 | 有 `mcp_*` 工具，**或**有 write_file/patch/terminal（能改 config.yaml 的面） |
| | `_coding_guidance` | 编码纪律 1-6 + 第 7 条 | `CODING_TOOLS={write_file,patch,terminal}` 交集非空；第 7 条的 delegate 半句另条件于 delegate_task 在面 |
| Runtime context | `_skills_index` | 技能索引 + 保存指导 | skill_view/skills_list/skill_manage 任一在面（base 必有）；索引按 `skills_allowed` 过滤；SKILLS_GUIDANCE 仅 skill_manage 在面 |
| | `_cwd_hint` | 工作目录 + OS/shell 提示 | `ctx.cwd` 非空 |
| | `_project_context` | 项目上下文（.xihe.md/AGENTS.md/CLAUDE.md/.cursorrules） | `project_context` 开关且文件存在（[[0014]]） |

## 三层 agent 的 prompt 差异

### 主 agent

唯一拿到**全部**层组的层。独有四样：

- **platform 层**：入口平台决定（cli/wecom/feishu/…）。
- **kbs preamble**：`kbs.enabled` 时整篇精简协议进 prompt（`load_kbs_preamble` 每次现读 `kbs_protocol.md`——**改 .md 无需重启**）。
- **roster 层**：specialists.enabled 时列出各 `run_<slug>_agent` + 路由阶梯，且按 `available_tools` 过滤——花名册永不宣传该 agent 调不到的专家。
- **记忆快照**：每轮在 `chat()` 的 **API 边界**把召回记忆 append 到 system message（`<memory-context>` 块），**不进 prompt 文本、不落库**——落库的 user message 必须是用户字面输入，且 prompt 文本保持稳定利于前缀缓存。

名单来自 config.yaml 顶层 `toolsets`/`skills`（[[0034]]）；project_context/cwd/`session.load_claude_md`/`load_cursorrules` 均按主配置。

### 专家 agent（specialists）

**与主 agent 同一条装配代码**（`_build_system_prompt` → LAYERS），差别全在入参：

| 维度 | 值 |
|---|---|
| identity | `identity_override` = yaml `persona`；若其名单全量或含 memory_manage，再追加一行 `agent:<slug>:` 记忆命名空间前缀要求 |
| platform | SessionSource platform=`"agent"` → 不在 `PLATFORM_PROMPTS` → **无平台层**（专家不直接面向用户） |
| kbs | 无 preamble（is_subagent gate）；改拿 `kbs_read_note=True` → 若 kbs_search 在面注入 3 行读纪律（候选≠结论） |
| roster 层 | 无（专家不能再派专家） |
| 工具指导层 | **照常注入，按专家自己的工具面裁剪**——这是与 delegate 的本质区别：itsm 专家有 terminal 就有编码纪律，claude 专家只有 external_agent 指导 |
| skills / project_context | 各自 yaml 键；`project_context: false` 可隔离项目文件 |
| cwd | 继承父 workspace（`_resolve_workspace_hint`，agent_base_dir 单源） |
| 连接键 | model/base_url/api_key/max_iterations 可覆盖，留空继承主配置（dispatch 时 overlay） |
| 记忆快照 | 无（is_subagent） |
| 工具面 | `(base ∪ yaml 名单) − subagent_blocked`（get_schemas(subagent=True)） |

### delegate 子 agent

**完全不走分层装配**：`chat()` 里 `if self.system_prompt_override: system_prompt = self.system_prompt_override` 直接短路，压缩后重建也被同一条件跳过。prompt 是 `_build_child_system_prompt` 生成的任务卡：

```
You are a focused subagent working on a specific delegated task.
YOUR TASK: <goal>
CONTEXT: <context>            # 可选
WORKSPACE PATH: <path>        # 可选——workspace 以文本嵌入，不是 cwd 层
Complete this task using the tools available to you. …
- What you did / found or accomplished / files created / issues encountered
（回复以摘要形式返回父 agent）
```

由此 delegate **没有**：platform 层、kbs 任何层、roster、Tool-Use Enforcement、Behavior Rules、全部工具指导、技能索引、项目上下文、cwd 层、记忆快照。工具面 = `(base ∪ 参数 toolsets 或 DELEGATE_DEFAULT_TOOLSETS) − subagent_blocked`；默认名单 `files/terminal/dev_tool/http/web/media` **故意不含 memory_manage**（durable facts 走父 agent；读仍经 base 到达）。

**设计动机**：delegate 是一次性窄任务执行器，父 agent 已持有全部指导与上下文，任务卡 + 工具 schema 足够；专家是常驻领域专家，需要与自身工具面匹配的完整纪律（[[0032]] 的对照）。

## 关键不变式与陷阱

1. **条件层必须 key off `ctx.tools`，且 `available_tools` 与 chat loop 的 `get_schemas` 用同一过滤器**（`subagent=self.is_subagent` + `enabled_toolsets ∪ _expansion_state`）。过滤器漂移 → 指导层宣传不可调的工具（request_tools 动态展开后尤其要同步）。
2. **编码纪律按"写/执行面"判定**（`CODING_TOOLS = {write_file, patch, terminal}`）：读工具在 base 人人有，若按读工具判定会把编辑纪律灌进只读 agent。
3. **`system_prompt_override` 三处短路**（首轮取用 + 两处压缩后重建跳过）——给 delegate 加任何 prompt 层时先想清楚该改任务卡还是该走分层装配。
4. **记忆快照不进 prompt 文本**：在 API 边界 append、下一轮丢弃重建。若把它焊进 build_system_prompt，前缀缓存失效且污染落库内容。
5. **`kbs_read_note=is_subagent` 只对专家生效**——delegate 不 build，拿了标志也没用；主 agent 走 preamble 分支。
6. 空工具 agent 的骨架 = identity + platform + Behavior Rules（Tool-Use Discipline 也无）——`test_prompt_layers.py` 的 skeleton 测试锁定此形状。
7. 改 `LAYERS` 表/层函数需**重启 serve/gateway**（Python 模块缓存）；改 `kbs_protocol.md`、技能文件、SOUL.md、项目上下文文件**即时生效**（每次现读磁盘）。

## 与名单模型的关系

[[0034]] 管"有什么"（roster 三态、base 并集、subagent_blocked），本页管"说什么"（层表、条件、三路径）。`available_tools` 是两页的接口：名单解析产出它，层表消费它。新增工具时两页各有一条义务——名单侧进 toolset（[[0002]]），prompt 侧考虑要不要配一层指导。
