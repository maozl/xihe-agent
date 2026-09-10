---
type: concept
title: 角色化子 agent（Roles）
slug: 0017_role-based-subagents
aliases:
  - roles
  - 角色
  - 角色化
  - role-based agent
  - subagent
tags:
  - architecture
  - agent
  - delegation
  - tools
  - skills
status: deprecated
created: 2026-07-21
updated: 2026-08-16
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0007_skills-system.md
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/concepts/0016_interrupt-stop-steer.md
---

# 角色化子 agent（Roles）— ⚠️ 已废弃

> **已废弃（2026-07-22）**：角色化方案回退。原因：主 agent 路由负担（glm 路由弱，何时委派哪个角色判断易错）、隔离副作用（子 agent 不知全局，需主 agent 传 context，易漏）、绑定 skill 全量注入浪费 token、角色边界模糊。回归**单 agent + request_tools + skill_view**（主 agent 全局上下文 + 按需工具/skill），ad-hoc delegate（无 role）保留作可选隔离大任务。`subagent_blocked` 标签 + `is_subagent` 属性保留（子 agent 边界，与角色无关）。本文留作历史记录。
>
> **后继（2026-08-16）**：[[0032_specialist-agents]] 专家 agent 落地——同为「预定义专家」但机制不同：配置声明的独立派生工具 `run_<slug>_agent`（非 delegate role 参数）、完整分层 prompt（非 wholesale 覆盖）、主 agent 技能索引不动（白名单在专家侧收口，默认空=不注入）。对本文四个回退理由的逐条对策见 0032「与角色化的差异」节。

## 摘要

预定义角色（专属 system prompt + 专属 toolset + 绑定 skill + 可选 MCP/model），主 agent 通过 `delegate_task(role=..., goal=...)` 委派。角色定义是单一 source of truth（`agents/*.md` 文件）。解决单 agent 的 tool/skill/prompt 膨胀与领域工具冲突——重型工具/领域知识下沉到角色，主 agent 只保留协调能力。

与 [[0013_toolset-scope-and-dynamic-expansion]] 互补：toolset scope 是「单 agent 内按需裁剪工具」，角色化是「按领域拆成多个 agent，每个带专属工具」。

## 角色定义（agents/*.md）

文件位置：bundled `agents/`（repo）+ user `~/.xihe-agent/agents/`（覆盖同名）。flat 结构，`<role>.md`。

frontmatter 字段职责独立：

| 字段 | 必需 | 说明 |
|---|---|---|
| `name` | 是 | 角色名（缺省用文件名 stem） |
| `description` | 是 | 触发条件（主 agent 据此路由） |
| `toolsets` | 否 | 内置 toolset 列表（core/web/ssh/...） |
| `mcp` | 否 | MCP server 名（对应 config `mcp_servers` 的 key）—— **独立字段，不混进 toolsets**（避免泄漏 `mcp-{server}` 内部命名） |
| `skills` | 否 | 绑定的 skill 名（内容注入角色 prompt） |
| `model` | 否 | 角色专属模型 |

body 是角色 system prompt（子 agent 的完整 system prompt，不加载主 agent 的 CLAUDE.md/skill 索引）。

## 加载机制（照搬 skills 模式）

`core/roles.py` 完全对称 `core/prompts.py` 的 skill 机制：双目录扫描（user 覆盖 bundled）+ mtime 缓存 + 双检锁。导出：

- `build_roles_prompt()`：角色清单（注入主 agent system prompt，仿 `<available_skills>`）
- `get_role_definition(name)`：完整角色定义（含 prompt_body），给 delegate 用
- `get_role_bound_skill_names()`：所有角色绑定 skill 的并集（给 skill 索引精简用）
- `get_role_bound_mcp_toolsets()`：角色绑定的 `mcp-{server}` 并集（给 MCP 分组用）
- `_load_skill_body(name)`：复用 `skills_tool._parse_frontmatter` 读 SKILL.md body（给角色 prompt 注入用）

**修正了 skill 的优先级 bug**：`prompts._scan_skills_index` 注释说"user 覆盖 bundled"，但 bundled 先入 `seen_names` 实际导致 bundled 覆盖 user。roles 扫描让 user 先入，正确实现 user 覆盖 bundled。

## 主 agent vs 子 agent：能力统一、定位区分

**能力维度（已由 role 驱动，统一）**：toolset/mcp/skills/model/prompt 都由角色定义决定。子 agent 完全 role 配置，没有"主子能力歧视"。

**定位维度（保留区分，非任意歧视）**——去掉这些会打回膨胀痛点：

1. **上下文隔离**：子 agent `is_subagent` → `skip_context_files=True`，不加载 CLAUDE.md/skill 索引/项目上下文/角色清单。这是 delegate 的核心价值——子 agent prompt 小、专注，领域知识靠 role body + 绑定 skill **精确注入**（`_load_skill_body` 现读）。
2. **tool 边界（`subagent_blocked` 标签）**：危险 tool 注册时标 `subagent_blocked=True`，`registry.get_schemas(subagent=True)` 按 tag 过滤（不再 toolset/tool 两套 block 列表）。子 agent 不能用：`delegate_task`（递归）、`skill_manage`（改全局 skill）、`send_message`/`send_image`/`clarify`（无对话通道）、`cronjob`（不该动调度）。子 agent **能用**：`skill_view`/`skills_list`/`todo`（只读/无害，未标 blocked）——绑定 skill 构造注入仍是主路径，但子 agent 也能 skill_view 按需加载。
3. **递归深度**：`MAX_DEPTH=2` 硬限（`delegate_depth` 数字），防无限委派。
4. **主/子判断**：`XiheAgent.is_subagent`（构造属性，`_build_child_agent` 传 `True`，主 agent 默认 `False`）。与 `delegate_depth`（递归深度，MAX_DEPTH 用）独立——depth 管 recursion，is_subagent 管身份（主/子），不互相派生。

### 完整差别对照

| 维度 | 主 agent | 角色 agent（web-ops/server-ops） |
|---|---|---|
| 身份 | `is_subagent=False` | `True`（构造传） |
| 触发 | 入口（wecom/feishu/cron 全工具） | `delegate_task(role=...)` 委派 |
| toolset | `DEFAULT_TOOLSETS` + mcp 差集（含 communication/agent/scheduler） | 角色 toolsets（`role_mode` 不与父交集）+ mcp 字段 |
| tool 边界 | 全 tool（不过滤） | `subagent_blocked` 标签过滤（双保险，角色 toolset 本就不含 communication/agent/scheduler） |
| system prompt | `build_system_prompt`（identity + CLAUDE.md + skill 索引 + 角色清单 + 项目上下文） | `system_prompt_override`（角色 body + 绑定 skill 内容） |
| 上下文加载 | CLAUDE.md / skill 索引 / 角色清单 / 项目上下文 | 全部 skip（隔离） |
| skill 获取 | 索引（全部 − 角色绑定）+ `skill_view` 按需 | 绑定 skill 构造注入（主路径） |
| MCP | 差集（未绑定角色的挂主） | role_mode 直接拿（mcp 字段） |
| memory | 注入 | 不注入 |
| 递归 | `depth=0` | `depth=parent+1`（MAX_DEPTH 防） |
| 对话用户 | 能（clarify/send_message） | 不能（toolset 无 communication + subagent_blocked） |
| auto-title | 有 | 无 |

三个本质区别归为一句话：**隔离**（角色不加载全局上下文，专注）+ **能力边界**（角色 toolset 专注单领域，不对话/不再委派/不动 cron）+ **入口**（主是对话入口+协调者，角色是被委派执行体）。

### 三个设计取舍

1. **为什么角色不加载全局上下文（CLAUDE.md/项目上下文/memory）**：角色领域知识自带（role body + 绑定 skill 构造注入），任务上下文由主 agent 在 `goal`/`context` 传。隔离省 token + 避免主 agent 全局上下文干扰角色专注。**取舍**：角色不知项目指令/用户偏好，需主 agent 传或写进 body。

2. **为什么角色不加载 skill 索引**：绑定 skill 已构造注入（角色有它的 skill 知识，不需索引发现）+ toolset 通常无 `skill_view`（不能 view）。ad-hoc 子 agent 有 `skills_list`（动态列）+ `skill_view`，主动 list 替代被动 system prompt 索引——功能等价，不需加载索引。

3. **角色能否再委派其他角色**：当前**不能**（toolset 无 `agent` → 无 `delegate_task` + `subagent_blocked` 标签挡 + `MAX_DEPTH` 防）。设计：**主 agent 是唯一协调者**，角色是执行体（不递归委派）。角色协作走主 agent（角色返回需求 → 主 agent 再委派）。如需角色直接协作，可放开（toolset 加 `agent` + 不标 delegate_task blocked，靠 MAX_DEPTH 防递归），但控制流复杂化，目前不开。

## role_mode：角色 toolset 放宽父交集

`_resolve_allowed_toolsets` 加 `role_mode` 参数：

- 默认（ad-hoc delegate）：`requested & parent_ts`（子不超过父）
- **role_mode**：直接授权角色声明的 toolset（不交集）；危险 tool 由 `subagent_blocked` 标签在 get_schemas 过滤（不再 toolset 级剥）

这让角色能拥有主 agent 故意没挂的 MCP server（MCP 分组后主 agent 不挂被绑定的 MCP，但角色 role_mode 直接拿到）。

## skill 索引精简 + MCP 分组（瘦身落点）

经核实 `DEFAULT_TOOLSETS` 已是精简集（web/media/scheduler 靠 `request_tools`，不在默认集），toolset 维度无合适下沉对象。真正瘦身落点：

1. **skill 索引精简**：`prompts._scan_skills_index` 排除 `get_role_bound_skill_names()` 的 skill。被角色绑定的 skill 从主 agent `<available_skills>` 移除，随角色出现。
2. **MCP 按角色分组**：gateway 主 agent MCP 加载改**差集**——`get_registered_mcp_toolsets() − get_role_bound_mcp_toolsets()`，被角色绑定的 `mcp-{server}` 不挂主 agent。兜底：主 agent `request_tools(["mcp-<server>"])` 临时拿回。

主 agent（广度：全部 skill 索引 − 角色绑定，按需 skill_view）vs 子 agent（深度：只绑定 skill 的完整 body 注入）。

## 缓存一致性

skills 索引缓存 key 含 roles 目录 mtime（`roles_dirs_mtime`），改角色 .md 的 skill 绑定会自动失效 skills 索引（否则只改 role 不动 skills 目录时主索引不刷新）。

system prompt 整体缓存在 SessionDB（`db.set_system_prompt`），角色清单/skill 索引变化后**旧会话不刷新**，需新 session（沿用 skill 旧行为）。

## 对 cron / skill_manage 的影响

**cron**：cron agent 走 `create_agent()` 无参 → **全工具**（不受 MCP 分组影响，含所有 MCP）。skill 索引仍排除角色绑定。cron 能 `delegate_task(role=...)`。⚠️ 不一致：cron agent 全工具，聊天主 agent（gateway `server.py:305`）走差集瘦身——可接受（定时任务无人值守该有完整能力）。

**skill_manage**：功能不受影响（操作 skills/ 文件，调 `clear_skills_cache`）。被绑定 skill 修改→角色委派时 `_load_skill_body` 现读最新 ✓；删除→角色 .md 脏引用，`_load_skill_body` 返回 None 被 `if body:` 跳过（graceful 降级，不崩）。skill_manage 不感知角色绑定（创建/删 skill 不自动同步角色 `skills:` 字段）。

## 改动文件

- `core/roles.py`（新）：角色加载核心
- `agents/_TEMPLATE.md`、`agents/README.md`（新）：模板/说明
- `agents/web-ops.md`（新）：首个角色，行内网站操作，toolsets `[core,web,media]`，绑 `cmdb-query-variable` + `intranet-sites`
- `core/prompts.py`：注入角色清单段 + `_scan_skills_index` 排除角色绑定 skill + skills 缓存 key 含 roles mtime
- `tools/delegate_tool.py`：`role` 参数 + `role_mode` + `_build_child_system_prompt` 注入角色正文/绑定 skill + 修 `shared_db/aux/compressor` 先建后覆盖的异味
- `tools/mcp_tool.py`：`get_registered_mcp_toolsets()`
- `gateway/bot.py`：主 agent MCP 差集加载（+ fallback）

## 设计决策与取舍

- **角色暴露方式 = 单工具 + 清单**：`delegate_task` 加 `role` 参数 + 角色清单注入主 prompt（不每角色生成 `ask_{role}` 工具，避免 schema 膨胀）。
- **mcp 独立字段**：不把 `mcp-{server}` 混进 `toolsets`，抽象掉内部命名约定，未来可扩展工具级粒度 `[{server, tools}]`。
- **角色绑定 skill 构造注入（主路径）**：角色 toolset 通常不含 `agent`，绑定 skill 内容构造时直接进 prompt 最省事。子 agent 现在也能 `skill_view`（未标 `subagent_blocked`），但角色用构造注入（toolset 不含 agent + 省 view 往返）。
- **主 agent 不激进瘦身**：DEFAULT_TOOLSETS 已精简，瘦身靠 skill 索引精简 + MCP 分组（配置驱动，可逆）。

## 后续

- **主 agent 角色化**：让主 agent 也读一个 `main` role 定义（toolset/prompt 配置驱动，取代硬编码 `DEFAULT_TOOLSETS`）——Phase 1 之后可选演化，但 main role 仍需保留全局上下文加载 + 对话入口定位。
- **A2A 对外暴露**：角色 = A2A agent，复用官方 `a2a-sdk`（JSON-RPC over HTTP+SSE，与 MCP 传输同构）。Phase 2（对外 server）/ Phase 3（主动调外部 agent）。
- **skill_manage 感知角色绑定**：删 skill 时检查 `get_role_bound_skill_names()` 命中则提示。边缘改进。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — toolset/dispatch 基础设施（role_mode 复用）
- [[0007_skills-system]] — skill 机制（角色加载完全照搬，绑定 skill 内容注入）
- [[0011_gateway-architecture]] — `create_agent` / SharedContext（主 agent 构造点）
- [[0013_toolset-scope-and-dynamic-expansion]] — 单 agent 工具裁剪（与角色化互补）
- [[0016_interrupt-stop-steer]] — 中断传播到子 agent（角色委派复用 `_active_children`）
