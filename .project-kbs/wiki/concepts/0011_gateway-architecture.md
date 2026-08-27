---
type: concept
title: Gateway 架构（消息处理与并发模型）
slug: 0011_gateway-architecture
aliases:
  - gateway 架构
  - SharedContext
  - fresh agent per message
  - 消息并发
tags:
  - architecture
  - gateway
  - concurrency
status: active
created: 2026-07-06
updated: 2026-07-07
related_pages:
  - wiki/entities/0001_xihe-agent.md
  - wiki/concepts/0006_session-design.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0005_mcp-dynamic-registration.md
  - wiki/changes/0010_cdp-default-and-cron-job-forms.md
---

# Gateway 架构（消息处理与并发模型）

## 摘要

xihe 一个 agent 内核、两种运行模式：CLI 模式跑一个长生命周期 `XiheAgent`；**Gateway 模式每来一条消息就新建一个 `XiheAgent`**，但这是廉价操作——贵的状态都在 `SharedContext`（和一批模块全局）里跨消息共享，对话连续性靠 SQLite，不靠 agent 对象。消息处理在专用线程里跑 agent 回合，主事件循环同步阻塞等待——这带来一个重要的并发特性（与隐患）。

## SharedContext：gateway 启动时建一次

`cli/app.py:SharedContext.__init__` 拥有跨消息复用的重对象：
- `db` — `SessionDB`（SQLite，对话历史）
- `aux` — `AuxiliaryClient`（视觉/压缩等辅助 LLM 调用）
- `compressor` — `ContextCompressor`

并接线工具依赖 + 启动常驻服务：
- `session_search_tool.set_session_db`、`cronjob_tools.start_scheduler()`、`vision/image_generation/tts_tools.set_auxiliary`
- `discover_mcp_tools()`（连接所有 MCP server，注册工具）

gateway 进程启动建一个 `SharedContext`，存活整个进程生命周期。

## create_agent：每条消息新建，薄壳

`SharedContext.create_agent()` 只 `return XiheAgent(config, shared_db, shared_aux, shared_compressor)`——**薄壳**，持有 config + 三个共享引用，构造极廉价。`gateway/bot.py` 每条入站消息调一次（slash 命令上下文一次 `:175`、正式回合一次 `:188`）。

> 「廉价」是这套设计成立的前提：如果是贵对象就不敢每条消息建。

## 连续对话靠 SQLite，不靠 agent 对象

每条消息的新 agent 拿 `session_key` 去 `db` 里 load 历史消息 → 接着跑。agent 用完即弃。session_key 由 `SessionSource`（platform + chat_id + user）确定性生成（见 [[0006_session-design]]）。

## 模块级全局共享状态

除 SharedContext 外，这些状态跨消息持久（在各自 `tools/*.py` 模块顶层）：
- **浏览器**：`tools/browser_tool.py` 的 `_page`/`_context`/`_browser_instance`（CDP 托管真实 Chrome，登录态跨消息/跨重启保留，见 [[0003_browser-tools]]）
- **MCP 连接**：`tools/mcp_tool.py` 的 `_servers`（每个 server 一个长生命周期 asyncio Task，见 [[0005_mcp-dynamic-registration]]）
- **cron 调度器**：`tools/cronjob_tools.py` 的 daemon 线程 + `_jobs`（见 [[0009_cron-jobs]]）

### cron 跑 job 需要 agent，由 gateway 启动时注入 factory

cron 的 daemon 线程每 60s tick，但执行 job 要调 `agent.chat(...)`。agent 来源有两种：
- **`_agent`（旧）**：聊天里调用 `cronjob` 工具时注入（`_inject_agent(parent_agent)`，handler 收到的 `kw["parent_agent"]`）。
- **`_agent_factory`（新，2026-07-07）**：`set_agent_factory(shared_ctx.create_agent)` 由 `run_gateway` 启动时注入。`_get_agent()` 优先用 factory **每个 job 取一个新 agent**（并发安全，不共享 `_agent`），`_has_agent()` 守卫 `_agent_factory or _agent`。

> **坑（已修）**：曾长期表现为「gateway 重启后 cron job 静默不执行」。根因是 `_agent` 只在聊天侧 cronjob 工具被调用时注入，重启后归零；调度器 `if not _agent: continue` 把每个 tick 全跳过，直到有人在聊天里碰巧用了 cronjob 工具——所以表现为「时灵时不灵、重启就废」。改为 gateway 启动注入 factory 后，cron 自主执行、重启不丢。
- **platform adapter**：`send_message_tool._adapter` / `cronjob_tools._platform_adapter`

## 消息处理线程模型与并发

每条消息：新 agent → 专用 `threading.Thread` 跑 `agent.chat()` → 主 asyncio 事件循环用**同步阻塞** `agent_thread.join(timeout=5.0)` 等它结束（`gateway/bot.py:243`），轮询间隙发 ACK。

**关键并发特性**：`thread.join()` 是阻塞调用，会**卡死单线程事件循环**。后果：
- **同一 chat**：新消息进不来（躺 WebSocket 缓冲区），等当前回合结束才被处理 → **被动串行**（实际效果像排队，但不是显式队列）。
- **不同 chat**：也互相拖累（A 聊天跑长任务，B 聊天干等）——本应并发却串行了。
- `active.interrupt()` 代码写在那但**几乎触发不了**（事件循环没空处理新消息去调它）。
- 回合期间发不了心跳/进度（循环被占用）。

> 这是当前架构的已知特征。改成 `await asyncio.to_thread(agent_thread.join, 5.0)` 即可解放事件循环（曾随 HITL 改动引入、又随其回退而撤）。真并发化后，因共享状态（SQLite/浏览器/MCP）非并发安全，**必须显式加 per-session 串行 guard**（如 asyncio.Event + sentinel + generation 号三重锁）。

## 消息处理流（server.py:handle_message）

1. 抽取媒体（图片走 vision/OCR 预描述）→ 拼成 text
2. `session_key = db.build_key(source)`
3. （同一 session 有活跃 agent → 调 `active.interrupt()`——见上，实际很少触发）
4. **slash 命令优先**：`text.startswith("/")` → `handle_command`（见下）
5. `create_agent()` → 登记进 `_active_agents[session_key]`
6. 专用线程跑 `agent.chat()`，主循环 `join` 等，超 `ACK_THRESHOLD_SECONDS` 发「正在处理」
7. 回合结束 → 发最终回复（流式或分片）+ 排空 pending media

## Slash 命令路由（gateway/commands.py）

`handle_command` 在 agent 回合**之前**处理 `/` 开头的消息，返回约定：
- 非空字符串（且非 `__CLEAR__`/`__QUIT__`）→ 直接回复、**不跑 agent**
- `None` → 流入 agent 回合（如 `/login`）
- `__CLEAR__`/`__QUIT__` → CLI 特殊语义

命令：`/new /reset /title /model /status /history /tools /compress /clear /quit /ping /login /reload-mcp /help`。

## ACK 与流式

- **ACK**：长任务超阈值主动发「⏳ 正在处理」（`server.py` join 循环里）。
- **流式**：平台支持 `send_stream`/`edit_message` 时，`StreamConsumer` 跑成独立 asyncio task，agent 线程通过回调把 delta 推给它。但因事件循环阻塞，流式期间也收不了新消息。

## 「新 agent + 活 registry」带来的特性

工具注册表（`tools/__init__:registry`）是进程级 live 对象。每条消息的新 agent 调 `registry.get_schemas()` 实时读 → **运行时注册的工具下条消息自动可见，不用刷缓存**：
- MCP `/reload-mcp` 后，下条消息的 agent 自动拿到新工具集（见 [[0005_mcp-dynamic-registration]]）
- 浏览器 `check_fn` 门控变化同理

## 相关页面

- [[0001_xihe-agent]] — 项目总览，CLI/Gateway 双模式
- [[0006_session-design]] — session_key/session_id 两层，对话连续性的基础
- [[0002_tool-registry-and-dispatch]] — 工具注册表 + check_fn 门控（新 agent 实时读）
- [[0005_mcp-dynamic-registration]] — MCP 工具的动态注册与 `/reload-mcp`
- [[0010_cdp-default-and-cron-job-forms]] — 含 gateway `run_gateway` 改动记录
