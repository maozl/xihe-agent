---
type: concept
title: 专家 Agent（Specialist Agents）
slug: 0032_specialist-agents
aliases:
  - specialist agents
  - 专家 agent
  - 常驻专家
  - run_<slug>_agent
tags:
  - architecture
  - agent
  - delegation
  - toolset
  - skills
status: active
created: 2026-08-16
updated: 2026-08-17
related_pages:
  - wiki/concepts/0034_three-layer-agent-roster.md
  - wiki/changes/0033_specialist-toolset-overhaul.md
  - wiki/changes/0035_three-layer-roster-unification.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0007_skills-system.md
  - wiki/concepts/0017_role-based-subagents.md
  - wiki/concepts/0024_desktop-serve-protocol.md
---

# 专家 Agent（Specialist Agents）

## 摘要

> ⚠️ **2026-08-17 名单语义订正**（见 [[0034_three-layer-agent-roster]] / [[0035]]）：① `toolsets` 不再缺省 `[files, memory]`——统一语义下**不写/`[]` = 不加载任何工具**（告警），`["*"]` = 全量；桌面表单预填 files+memory 只是 UI 缺省。② 新增 **`specialists.enabled` 总闸（config.yaml，默认 false）**：关则 `run_*_agent` 不注册、花名册层自动消失，yaml 仍可编辑；`GET /specialists` 返回 `specialists_enabled` 供客户端区分「配置关」vs「待重启」，桌面能力开关卡有对应 Toggle。

xihe 支持由**配置声明的常驻专家**：每个专家一个独立文件 `<agent_home>/agents/<slug>.yaml`（文件名即 slug），进程启动时（`load_all_tools()`）自动注册一个派生工具 `run_<slug>_agent(goal, context)` 到 `agent` toolset，主 agent 的 system prompt 增加一层**专家花名册**（roster）供路由。专家与 `delegate_task` 的临时子 agent 是两类东西：临时工是运行时发明、wholesale prompt 覆盖、无编制；专家是**谁拥有配置谁声明**、走完整分层 prompt、toolset/skills/连接参数固定编制。落地变更见 [[0033_specialist-toolset-overhaul]]。

与已废弃的角色化（[[0017_role-based-subagents]]，2026-07-22 回退）的关系：机制上吸取教训——不再是 `delegate_task(role=...)` 参数 + 主 agent skill 索引精简 + MCP 差集分组那套运行时路由，而是**独立派生工具 + 主 agent 索引不动 + skills 白名单在专家侧收口**。0017 回退的四个理由（路由负担/隔离副作用/绑定注入 token/边界模糊）对应的设计对策见下文「与角色化的差异」。

## 定义文件（agents/*.yaml）

一个文件一个专家，slug 必须匹配 `^[a-z][a-z0-9_]{1,30}$`（小写字母开头、≥2 位）——这个正则同时兼任 serve API 的**路径穿越防线**。文件在进程启动时读取，编辑后需重启 serve/gateway 才生效。

| 字段 | 必需 | 说明 |
|---|---|---|
| `description` | ✅ | 职责描述，主 agent 花名册路由依据 |
| `persona` | ✅ | 身份层全文（替换 SOUL.md 身份，其余分层保留） |
| `name` | 否 | 显示名，缺省 = slug |
| `toolsets` | 否 | 静态组 + `mcp`（全部服务器）/ `mcp-<server>`（按需单个）；**不写/`[]` = 不加载**（告警）、`["*"]` = 全量（统一语义见 [[0034]]） |
| `skills` | 否 | 技能白名单；**不写/`[]` = 不注入任何技能**（≠ 全量索引）、`["*"]` = 全量 |
| `model` / `base_url` / `api_key` | 否 | 连接覆盖；留空 = 继承主配置 |
| `max_iterations` | 否 | 留空/不写 = 继承主配置 |
| `project_context` | 否 | true 才读 cwd 的 CLAUDE.md/.xihe.md 等（编码类专家开） |
| `enabled` | 否 | false 则不注册派生工具（保留文件） |

**校验容错**：`core/agent_defs.py` 的 `_parse_def` 对坏文件 warn-and-skip，绝不打断主 agent 启动。未知静态 toolset 名 → 告警剔除；`mcp-<server>` 未注册（服务器未配置/未连上）→ 告警但**保留**（resolve 到空集，等服务器注册后自然生效）。

## 派生工具机制

`load_all_tools()` 末尾为每个通过校验的 AgentDef 调 `registry.register(name="run_<slug>_agent", toolset="agent", subagent_blocked=True, ...)`。调度时 `specialist_agent_tool._build_agent_instance` 构造子 `XiheAgent`：

- `enabled_toolsets = agent_def.toolsets`（专家声明即授权，不与父交集——同 0017 的 role_mode 思路）
- `skills_allowed = set(agent_def.skills)`——注意是 `set(...)` 不是 `set(...) or None`，空列表转空集 = 无技能
- persona 作 identity_override，**其余分层照常构建**（行为准则/上下文/花名册不含自身）
- `config = {**parent.config, **agent_def.config_overrides()}`——只覆盖非空连接键，父 config dict 不被就地改

### 完整分层 prompt（与 delegate 临时工的本质差异）

| 维度 | delegate_task 临时子 agent | 专家 agent |
|---|---|---|
| system prompt | `system_prompt_override` 全量替换（goal+context 拼一段） | persona 只替换身份层，行为准则/项目上下文分层保留 |
| 编制 | 调用时发明（toolsets 参数） | 配置声明，重启不变 |
| skills | 白名单与否看父 agent | 自带白名单 |
| 连接参数 | 仅 model 可覆盖 | model/base_url/api_key/max_iterations 四项 |
| 发现方式 | 主 agent 记得用它 | 花名册层常驻主 prompt |
| 深度限制 | `subagent_blocked` 挡递归 | 同样 `subagent_blocked=True`，专家不能再委派 |

## skills 白名单语义（None vs 空集）

`XiheAgent.skills_allowed` 三态：

- `None` = 主 agent 默认，注入全量技能索引
- `set()`（空集）= **不注入任何技能**（`build_skills_prompt(set())` 过滤后返回 ""）
- 非空集合 = 只注入白名单内的技能

**陷阱**：实现里任何 `or None` / falsy 翻转都会把空集悄悄变成全量索引（曾因此出过 bug：专家「不选技能」实际拿到全部技能索引）。两处必须保持 `set(skills) if skills is not None else None` 形态：`core/agent.py` 构造参数 + `specialist_agent_tool` 传参。e2e 断言 `skills_allowed == set()` 守护此语义。

## toolsets 与 mcp-\<server\> 按需授权

专家的 `toolsets` 接受三类名字：

1. **静态组**：`core/toolsets.py` 的 14 个平铺组（files/terminal/dev_tool/http/web/memory/…，见 [[0033]] 的目录表）
2. **`mcp`**：全部 MCP 服务器工具
3. **`mcp-<server>`**：只授权单个服务器

`mcp-<server>` 零调度侧改动即可工作的原因在 `registry.get_schemas` 的**双路匹配**：一个工具被选中当且仅当 `resolve_toolset(ts)`（TOOLSETS 里的静态名单）∪ `entry.toolset == ts`（registry 注册时的 toolset 字符串）——MCP 每个服务器的工具注册时 `toolset=f"mcp-{name}"`，天然命中第二条路。推论：**各工具模块里 register 的 toolset 字符串必须与组名镜像**，改名要两头改。

## 连接覆盖与 api_key 安全

`AgentDef.config_overrides()` 只输出非空的 model/base_url/api_key/max_iterations，dispatch 时覆盖到主 config 副本上（可让专家走独立网关/模型/凭证）。api_key 约束：

- 永不跨 serve API 回显——`GET /specialists` 只返回 `api_key_set` 布尔；spec 里剥掉 api_key
- `PUT /specialists/{slug}` 三态：body 带 api_key 且非空 = 写入；**空串 = 清除**；**键缺省 = 保持文件里的现有值**（桌面表单「留空保持不变」靠这条）
- 实现顺序敏感：先处理「空串 pop」，再「缺省则从 `_load_raw` 回填」——反过来会吞掉清除语义

## 存储、CRUD 与「待重启」徽标

- 存储：一专家一文件，`save_raw` 走 tmp + `os.replace` 原子写；`list_raw_specs()` 给编辑器**未校验**视图（校验会丢的坏文件也要能显示出来修）
- serve API：`GET /specialists`（列表+toolset 目录+mcp 服务器清单+已注册名单）、`PUT /specialists/{slug}`（创建/更新，返回校验告警）、`DELETE /specialists/{slug}`（幂等，bad slug 400）
- **待重启徽标**：`registered` 字段读**活 registry**（`run_*_agent` 过滤），文件编辑后 registry 不变，列表里该专家标「待重启」直到 serve 重启——这是特性不是 bug，桌面用它提示用户点重启
- 桌面编辑器（SettingsPanel `SpecialistsCard`）：工具集/MCP/技能全 chips 多选；不在当前目录里的旧名（已删服务器/已删技能）渲染虚线但**保留不静默丢**；api_key 只写不回读

## 与角色化（0017）的差异——逐条对策

| 0017 回退理由 | 专家 agent 的对策 |
|---|---|
| 主 agent 路由负担（glm 路由弱） | 花名册只在**用户声明**的专家上存在，规模受控；派生工具 schema 即路由界面，比 role 参数直白 |
| 隔离副作用（子 agent 不知全局） | 保留（隔离是委派的本意）；`project_context: true` 可让编码类专家读项目上下文 |
| 绑定 skill 全量注入浪费 token | skills 是**白名单**且默认空（不注入任何技能），不是绑定注入 |
| 角色边界模糊 | description 必填 + 由配置所有者定义边界，不靠运行时推断 |

## 相关页面

- [[0033_specialist-toolset-overhaul]] — 落地变更记录（存储迁移、serve CRUD、桌面编辑器、toolset 目录重构）
- [[0002_tool-registry-and-dispatch]] — 注册表与 get_schemas 双路匹配（mcp-\<server\> 的基础设施）
- [[0007_skills-system]] — 技能系统（白名单注入的底层）
- [[0017_role-based-subagents]] — 已废弃的角色化前作（差异对照见上）
- [[0024_desktop-serve-protocol]] — serve 模式（/specialists API 宿主）
