---
type: concept
title: 三层 Agent 名单模型（主 / 专家 / delegate）
slug: 0034_three-layer-agent-roster
aliases:
  - 三层 agent
  - roster 模型
  - resolve_roster
  - config-driven roster
tags:
  - architecture
  - agent
  - toolset
  - config
status: active
created: 2026-08-17
updated: 2026-08-17
related_pages:
  - wiki/changes/0035_three-layer-roster-unification.md
  - wiki/concepts/0032_specialist-agents.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/entities/0001_xihe-agent.md
---

# 三层 Agent 名单模型（主 / 专家 / delegate）

## 摘要

xihe 有三层 agent，每层的 tool/skill/MCP 名单来源不同，但**主 agent 与专家 agent 共用同一个解析函数** `core.toolsets.resolve_roster(spec)`——主 agent 的 spec 就是 config.yaml 本身（顶层 `toolsets`/`skills` 键，与 `model` 并列），专家的 spec 是 `agents/<slug>.yaml`。**不存在任何 main 专属解析逻辑**（无 `resolve_main_toolsets` 之类分叉；这是用户明确裁决：两者配置项基本一致，语义必须一条路）。delegate 临时子 agent 不走配置，名单是运行时 `toolsets` 参数，与父名单**完全独立**。

| 层 | 名单来源 | 解析路径 |
|---|---|---|
| 主 agent | config.yaml 顶层 `toolsets` / `skills` | `resolve_roster(config)`，SharedContext 启动时解析一次 |
| 专家 agent | `agents/<slug>.yaml` 同名键 | `resolve_roster(spec)`，`_parse_def` 内；受 `specialists.enabled` 总闸 |
| delegate 子 agent | 运行时 `toolsets` 参数三态 | `delegate_tool._resolve_allowed_toolsets`，不经 resolve_roster |

## 统一名单语义（resolve_roster）

一个函数，三态语义，**主/专家完全一致**：

| 写法 | 效果 |
|---|---|
| 不写 / `[]` | **不加载任何工具**（记 warning）；skills 同理不注入 |
| `["*"]` | 返回 `None` = 不限制（全量） |
| 名单 | 白名单；未知静态组名剔除并告警；**`mcp` / `mcp-<server>` 永远保留**（服务器可能稍后注册，先占名） |

skills 侧：`"*"` 在列表里 → `None`（全量索引）；否则字符串列表为白名单；非字符串项静默丢弃。

要点：

- 「不写 = 不加载」是**故意的破坏性语义**（配错了立刻发现 agent 没工具，而不是默默全量）。主 agent 想全量须显式 `toolsets: ["*"]`。
- `mcp-<server>` 未注册也保留名字 → resolve 到空集，服务器连上后自然生效（不告警丢失）。
- 未知静态名剔除只告警不失败——坏配置不打断启动（同 `_parse_def` 的 warn-and-skip 哲学）。

## [] vs None 不变式（最锋利的陷阱）

`enabled_toolsets` 是三态的：`None` = 全量、`[]` = 空、非空 set = 白名单。**任何 truthiness 判断都会把 `[]` 翻成 `None`**（`[]` 为 falsy）——统一语义落地时 `agent.py` 构造函数就因此出过反向 bug：「没配置=不加载」实际变成「全量」。全链路必须保持：

```python
set(enabled_toolsets) if enabled_toolsets is not None else None
```

- `XiheAgent.__init__`（agent.py）+ 两处 `get_schemas` 调用点都依赖 `is not None`
- `registry.get_schemas` 契约：`toolsets=None` → 全部工具；`toolsets=set()` → 空
- `skills_allowed` 同构三态：`None` 全量索引 / `set()` 不注入 / 非空白名单（见 [[0032]] 的 falsy 翻转陷阱）
- 已记入 CLAUDE.md Gotchas；`tests/test_roster.py` 的 TestXiheAgentRoster 守护

## 主 agent：config.yaml 顶层键

- `SharedContext.__init__`（`cli/app.py`）在 MCP discovery 之后解析一次，存 `main_toolsets` / `main_skills`；三个入口（CLI `init_agent`、gateway `server.py` 每消息 agent、serve `_handle_send` 每轮 agent）都把它传给 `create_agent()`——**一个 xihe 进程内主 agent 名单全局一致**。
- `create_agent()` 无参调用（cron 任务、斜杠命令的上下文 agent）**保持全量**，不受主名单影响——定时任务和命令需要完整能力，这是设计而非遗漏。
- 推荐形态：主 agent 只当协调者（slim 名单 + `request_tools` 按需扩展，见 [[0013]]），专业工具交给专家 / delegate。用户实机：7 组 → 49 工具（32 个来自两个 MCP 服务器）。
- serve 的 `_capabilities(toolsets)` 能力描述符**按主名单收敛**（走 `get_schemas`，含 check_fn 门控）——slim 主 agent 不虚报 browser/mcp 角标，桌面 capability-driven UI 不误导。

**配置管道陷阱**：`load_config`（config.py）从**白名单**拷贝顶层键和 section，且有两处循环（拷贝循环 + setdefault 循环）。新增键必须两处都加，否则**静默消失**——`toolsets`/`skills`（顶层键）和 `specialists`（section）都踩过这条路。已记入 CLAUDE.md。

## 专家 agent：yaml + 总闸

- `agents/<slug>.yaml` 的 `toolsets`/`skills` 与主 agent **同名同语义**（上表）。与 [[0032]] 记录时的差异：**不再有「默认 `[files, memory]`」**——统一语义下不写就是不加载（告警）；桌面表单预填 files+memory 只是 UI 缺省，非语义缺省。
- `specialists.enabled`（config.yaml section，**默认 false**）是派发总闸：关 = `register_specialist_agent_tools()` 直接返回，`run_*_agent` 不注册、提示词花名册层自动消失（花名册按实际可调用工具过滤）；yaml 文件仍可编辑（`GET /specialists` 返回 `specialists_enabled` 供客户端区分「配置关」和「待重启」）。普通用户不需要专家委派，agents/ 目录里有文件不等于暴露派发工具。
- persona 走 `identity_override`，其余分层照常（区别于 delegate 的 wholesale 覆盖，详见 [[0032]]）。

## delegate 子 agent：运行时三态

- `toolsets` 参数：**缺省/`[]` → `DEFAULT_TOOLSETS`**（files/terminal/dev_tool/http/web/memory/media）；`["*"]` → 全量；指定名单 → 原样尊重（**不与父名单交集**）。全部非法名 → 回退默认。
- 独立性是设计决策：主 agent slim 化后，若子 agent 继承父名单会被饿死（父没有的组子也拿不到）；安全性不靠名单继承，靠 `subagent_blocked` 标签。
- `subagent_blocked=True` 全集（12 类）：delegate_task（递归）、clarify / send_message / send_image（用户交互）、cronjob（副作用调度）、skill_manage（技能变更）、external_agent、`run_*_agent`（跨 agent 派发）、kbs_init（写脚手架；kbs_status/kbs_search 仍可用）、web_record / browser_record（录制）、browser_state_delete。经 `registry.get_schemas(subagent=True)` 过滤。
- 硬边界：`MAX_DEPTH=2`、max_iterations 硬帽 60。
- 子 agent **没有技能索引**：`system_prompt_override` 整体替换提示词，短路了 `_build_system_prompt`；技能只能经 `skills` toolset 的 skills_list/skill_view 工具访问，而默认 7 组不含 `skills`。

## 设计裁决记录（为什么主 agent 不单独建模）

落地过程三次被用户纠正，最终形态是**最小概念量**：

1. ❌ 内置 `DEFAULT_MAIN_TOOLSETS` 缺省兜底 → 「config 里面不需要加 main 这个」
2. ❌ `main:` section / `agents/main.yaml` 保留 slug → 「主 agent 不需要单独搞一个 agent，它就用现在的主 agent 读根目录下的 config.yaml 来实例化」
3. ✅ config.yaml 顶层键，与专家同一个 `resolve_roster`

核心论据：主/专家配置项基本一致，任何 main 专属逻辑（`resolve_main_toolsets` 等）都是无谓分叉；主 agent 的「定义文件」就是它本来就在读的根配置。

## 相关页面

- [[0035_three-layer-roster-unification]] — 本次统一的变更记录（代码点、验证、破坏性影响）
- [[0032_specialist-agents]] — 专家层细节（分层 prompt、连接覆盖、api_key 安全、CRUD）
- [[0002_tool-registry-and-dispatch]] — registry 与 get_schemas 双路匹配（mcp-\<server\> 的基础设施）
- [[0013_toolset-scope-and-dynamic-expansion]] — request_tools 按需扩展（slim 主 agent 的配套）
- [[0001_xihe-agent]] — 项目总览
