---
type: concept
title: 桌面端通信协议与 xihe serve 模式
slug: 0024_desktop-serve-protocol
aliases:
  - xihe serve
  - serve 模式
  - Workspace Protocol
  - 桌面端通信协议
  - 桌面↔agent 协议
tags:
  - architecture
  - serve
  - desktop
  - protocol
  - websocket
status: active
created: 2026-08-10
updated: 2026-08-10
related_pages:
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/concepts/0006_session-design.md
  - wiki/concepts/0023_multi-instance-config.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/entities/0001_xihe-agent.md
---

# 桌面端通信协议与 xihe serve 模式

## 摘要

`xihe serve` 是 xihe 的**第三种运行模式**（继 `xihe chat` 交互式 CLI、`xihe gateway` 消息平台之后）。它把**同一个 agent 内核**用 aiohttp 包成本地 HTTP+WebSocket 服务，让外部前端（桌面 app、脚本、web UI）能在不内嵌引擎的前提下驱动 xihe——桌面连 `/stream`，就像任意客户端连后端。

架构复刻 gateway：一个 `SharedContext`（SQLite / 辅助 LLM / 压缩器，启动建一次）+ **每轮对话新建一个薄 `XiheAgent`**（见 [[0011_gateway-architecture]]）。关键差异在**并发模型**：serve 用 `loop.run_in_executor` 把 agent 回合丢进工作线程，事件循环**不被阻塞**——从而避开了 gateway「`thread.join()` 卡死单线程循环」的已知顽疾。agent 工作线程的同步流式回调通过 stdlib `queue.Queue` 桥接到 WebSocket，与 `gateway.stream_consumer.StreamConsumer` 同思路。

桌面端 ↔ serve 的这套 REST + WS 契约，本文档记为 **Workspace Protocol**。它是桌面端（控制面，见 [[0025_desktop-control-plane]]）与 xihe（执行内核）之间的中立接口——桌面按能力描述符分支，**永不 sniff 引擎名**。

## 第三种运行模式

| 模式 | agent 生命周期 | 入口 | 前端 | 事件循环阻塞？ |
|------|----------------|------|------|----------------|
| `chat` | 一个长生命周期 `XiheAgent` | `cli/chat.py` | 终端 REPL | — |
| `gateway` | 每条消息新建薄 agent | `gateway/bot.py` | 平台 adapter（WeCom/Feishu） | **是**（`thread.join`，见 [[0011_gateway-architecture]]） |
| **`serve`** | **每轮对话新建薄 agent** | `gateway/serve.py` | 任意 HTTP/WS 客户端（桌面/脚本/web） | **否**（`run_in_executor`） |

serve 本质上是「把 gateway 模式跑在一个中立协议上」，把平台 adapter 换成了 aiohttp。`SharedContext`（`cli/app.py`）跨轮复用重对象；`SharedContext.create_agent()` 每轮调一次——**薄壳**，持有 config + 三个共享引用，构造廉价（同 [[0011_gateway-architecture]] 的前提）。toolset 解析也照搬 gateway：`DEFAULT_TOOLSETS` + `mcp`（有 `mcp_*` 工具时）+ `kbs`（`kbs.enabled` 时）。

入口注册：`cli/app.py:cmd_serve` → `load_config(args.config)` → `setup_logging(INFO, also_file=True)`（serve 起初漏了 logging，回调日志被吞，已修）→ `run_serve(config, host=args.host, port=args.port, version=VERSION)`。CLI 子命令：`xihe serve [--host 127.0.0.1] [--port 7788] [--config X]`。端口/主机**不在 config.yaml 里**，是 CLI flag（默认 7788 / 127.0.0.1）；桌面端硬编码 7788（`setServeBase` 可覆盖）。

## REST 接口（无状态）

| 方法·路径 | 响应 |
|-----------|------|
| `GET /health` | `{ok, version, mode:"serve", model, capabilities:[...]}` |
| `GET /agents` | `{agents:[{id:"self", name, engine:"xihe", shape:"process", model, status:"online", capabilities, dataRoot:AGENT_HOME, description}]}` —— P0 自描述单 agent，persona 多 agent 留待 [[0025_desktop-control-plane]] 的 P1 |
| `GET /sessions` | `{sessions:[{conv_id, session_key, title, updated_at, msg_count}]}` —— serve 平台会话，按更新时间倒序 |
| `GET /convs/{conv_id}/messages` | `{conv_id, messages:[{role, content}]}` —— 历史转录 |
| `POST /convs/{conv_id}/reset` | `{conv_id, session_key, reset:bool}` —— 该会话重开一轮 |

`get_messages` 有两处过滤，**不能去掉**：丢掉 `role=="system"`（xihe 内部 system prompt，不是聊天气泡）和「无 content 的 assistant 帧」（纯工具调用框架、[[0011_gateway-architecture]] 提到的 dangling 修复/恢复提示等内部脚手架）。桌面只渲染 `role+content`。

## WebSocket `/stream` 事件契约

**客户端 → 服务端：**
| type | 字段 | 语义 |
|------|------|------|
| `send` | `conv_id`, `text` | 发起一轮对话 |
| `interrupt` | `conv_id` | 中断该会话当前回合 |

**服务端 → 客户端：**
| type | 字段 | 何时发 |
|------|------|--------|
| `hello` | `version`, `mode`, `model`, `capabilities` | 连接建立即发（无 `turn_id`/`conv_id`） |
| `turn_start` | `turn_id`, `conv_id`, `session_key` | 每轮回合开始 |
| `text_delta` | `turn_id`, `conv_id`, `text` | 正文增量 |
| `thought_delta` | `turn_id`, `conv_id`, `text` | 推理增量（`kind=="reasoning"`） |
| `tool_call` | `turn_id`, `conv_id`, `name`, `args` | 工具开始 |
| `tool_result` | `turn_id`, `conv_id`, `name`, `args`, `elapsed` | 工具结束（`elapsed` 秒，3 位小数） |
| `complete` | `turn_id`, `conv_id`, `text` | 回合正常结束，`text`=最终回复 |
| `error` | `turn_id?`, `conv_id?`, `message` | 回合失败或入参非法；`hello` 后的通用错误可无 `turn_id`/`conv_id` |

心跳：`WebSocketResponse(heartbeat=30)` —— aiohttp 自动 ping/pong 保活。

> **`args` 是摘要不是全文**：`tool_call` / `tool_result` 的 `args` 是 agent 把原始 `arguments` 截到 **120 字符**后传出的（`core/agent.py` 调 `tc["arguments"][:120]`），用于 UI 展示，**不是**完整工具入参。需要完整入参得在桌面端侧自己留（P0 不需要）。

## 会话映射

桌面一个对话 id（`conv_id`）→ `SessionSource(platform="serve", chat_id=conv_id, user_id="desktop", chat_type="dm")` → 确定性 `session_key` = **`agent:main:serve:dm:{conv_id}`** → 一个持久 xihe 会话。

- 历史落 `sessions.db`，**跨 serve 重启存活**。
- 与 CLI / gateway 会话**完全隔离**（platform 段不同：`serve` vs `cli`/`wecom`/`feishu`），除非你手动复用 key。
- `_PLATFORM = "serve"`、`_DEFAULT_USER = "desktop"` 是 serve.py 顶部两个常量——刻意匹配 `xihe serve` 命令名以便按进程 grep；若偏好 "server" 改这一处即可（cosmetic）。

会话 key 推导见 [[0006_session-design]]。

## 能力描述符（capability descriptor）

`_capabilities()` 向桌面声明能力 flag：

```
["text","streaming","tools","interrupt","sessions","thoughts"]   # 基线常驻
+ "browser"            # 有任一 browser_* 工具
+ "vision"             # 有 vision_analyze 或 image_ocr
+ "image_generation"   # 有 image_generation 工具
+ "mcp"                # 有任一 mcp_* 工具
```

后半段从 `registry._tools` 实时推导——也就是说能力 flag 反映**当前进程真正过 `check_fn` 门控的工具**（如 Playwright 没装则 `browser` 自动消失，见 [[0002_tool-registry-and-dispatch]] 的 check_fn 门控）。`/health`、`/agents`、WS `hello` 三处都带这份描述符。

**桌面 UI 按这些 flag 分支，永不 sniff 引擎名。** 这是「中立协议」的核心承诺，也是桌面能同时容纳 xihe / Claude / CodeBuddy 等异质 provider 的前提。能力描述符的**设计哲学**（三层模型、capability-driven UI）见 [[0025_desktop-control-plane]]，本文只记 wire 层。

## Emitter：跨线程回调 → WS 的桥

这是 serve 最关键的技术点。`agent.chat()` 的回调（`stream_delta_callback` / `tool_call_start_callback` / `tool_call_callback`）是**从工作线程同步触发**的（agent 跑在 `run_in_executor` 的线程里），不能直接调 `ws.send_json`（asyncio 对象非线程安全，且该线程没有 running loop）。

`Emitter`（`gateway/serve.py`）解法：
- 回调把 JSON 事件 `put` 进一个 **stdlib `queue.Queue`**（线程安全，无需 loop）。
- WS handler 协程**自己**排空这个队列：`get_nowait()` 取不到就 `await asyncio.sleep(0.02)` 再试，循环往复直到工作线程结束。
- `_DONE` 哨兵：工作线程 `finally` 里 `emitter.finish()` 推一个 `_DONE`，排空循环见到它即收尾。

**为什么不用 `asyncio.Queue`？** 工作线程是 ThreadPoolExecutor 线程，**没有 running loop**，`asyncio.Queue.put` 既非线程安全、也无 loop 可用。stdlib `queue.Queue` 是唯一不需要 loop 的线程安全原语——这是 load-bearing 选择，不是风格偏好。

**与 gateway 的对比**：[[0011_gateway-architecture]] 的 `StreamConsumer` 是一个**独立 asyncio task**，agent 线程把 delta 推给它。serve **没有**单独的 consumer task——排空发生在 **WS handler 协程内**（`_handle_send` 里的 while 循环）。两者都是「同步工作线程回调 → 事件循环」的桥，但 serve 把桥并进了 handler。

**`on_delta` 的 `None` 哨兵**：agent 在工具分派前会用 `text is None` 标记段落边界（`core/agent.py`）。Emitter 见 `None` 直接 skip——不产生 WS 事件，因为紧随其后的 `tool_call` 事件本身已承载了阶段切换。

## 并发与中断

serve 用 `loop.run_in_executor(None, _worker)` 跑 agent 回合，事件循环在 `await asyncio.sleep(0.02)` 的间隙**完全空闲**——可以收新 WS 帧、发心跳、处理别的会话。**这正是它对 gateway 的核心改进**：[[0011_gateway-architecture]] 里 `thread.join()` 把单线程循环卡死，导致同会话被动串行、跨会话互相拖累、中断触发不了、期间发不了进度。serve 把 join 换成「executor + 排空循环」，循环不再阻塞。

两条锁：

- **`_turn_locks`**（`conv_id → asyncio.Lock`）：同一会话的回合**串行**，防止历史被并发写坏 / 回复交错。仅在单事件循环线程访问，dict 变更无需额外锁；`asyncio.Lock` 守的是 await 区间。
- **`_active`**（`conv_id → 当前 XiheAgent`）+ `threading.Lock`：给 `/interrupt` 用。`_handle_send` 开回合时登记 agent、结束时按身份比对 `pop`（防把后一轮的 agent 误删）。

中断路径（两条）：
1. **客户端主动**：WS 收 `{type:"interrupt","conv_id":...}` → `_interrupt()` 从 `_active` 取 agent → `agent.interrupt()`。
2. **客户端掉线**：`stream()` 的 `finally` 遍历该 socket 启动过的所有 `conv_id` 逐个 `_interrupt`——**关键**：否则 agent 会对着死连接把一整轮跑完、回调对着无人排空的队列空推。

`_safe_send(ws, obj) -> bool`：发帧前先判 `ws.closed`，发送异常一律返回 `False`。排空循环一旦拿到 `False` 立刻 `_interrupt` + 跳出——保证「客户端没了，回合不再烧」。

## CORS 与桌面端接法

Electron renderer（`file://`/`app://` origin）跨域 fetch `http://127.0.0.1:<port>` 的 REST。WS 不受同源约束，故 CORS 只需给 REST 加：

- `on_response_prepare` 钩子给所有响应加 `Access-Control-Allow-Origin: *`、`Allow-Headers: Content-Type`、`Allow-Methods: GET, POST, OPTIONS`；`WebSocketResponse` 跳过。
- `OPTIONS /{tail:.*}` 返回 204，满足预检。

桌面端（renderer 原生 `WebSocket` + `fetch`，无 axios）：
- `baseUrl = http://127.0.0.1:7788`、`wsUrl = ws://127.0.0.1:7788/stream` 硬编码；`setServeBase(url)` 可改（给设置项留口）。
- `connectStream` 在 `onopen` resolve、`onclose`/`onerror` reject / 触发 `onStatus(false)`；store 据此做 3s 指数重连兜底。
- React StrictMode 在 dev 下会 double-invoke `useEffect` → 两次 `/health` + `/agents` + `/stream`，无害（生产无）。

## 落地文件

- **serve 内核**：`gateway/serve.py`（`run_serve` / `ServeApp` / `Emitter` / `_capabilities` / `_resolve_toolsets` / CORS）
- **CLI 接线**：`cli/app.py`（`cmd_serve` + `serve` 子解析器：`--host` / `--port` / `--config`）
- **桌面端客户端**：`xihe-desktop/src/renderer/src/lib/serveClient.ts`（事件联合类型 + REST/WS 客户端）

## 设计权衡与坑

- **漏 logging（已修）**：`cmd_serve` 起初没调 `setup_logging`，回调里的 `logger.info` 全被吞（stdout 还是块缓冲）。现已在 `cmd_serve` 加 `setup_logging(level=INFO, also_file=True)`，`agent.log` 能看到 `[gateway.serve] xihe serve listening...`。排查 serve 问题先看 `agent.log`。
- **平台名 `serve` vs `server`**：刻意选 `serve` 匹配命令名，便于 grep；单常量，纯 cosmetic，要改改一处。
- **端口在 CLI 不在 config**：`--port`/`--host` 是 flag，不进 `config.yaml`。要开多实例用 `xihe --config X serve --port N`（实例隔离见 [[0023_multi-instance-config]]：`agent_home` 决定数据根，各实例 sessions/log/browser/cron 独立）。
- **历史过滤不可去**：system prompt + 空 assistant 帧是 xihe 内部脚手架，泄漏到桌面会暴露 recovery hint / dangling 修复（见 [[0011_gateway-architecture]] 的 `_repair_dangling_tool_calls` / `_inject_recovery_hint`）。
- **排空用 stdlib queue 而非 asyncio.Queue**：见 Emitter 节，工作线程无 loop。
- **客户端掉线必中断**：否则烧一整轮 agent。

## 相关页面

- [[0011_gateway-architecture]] —— SharedContext + 每消息薄 agent（serve 同构）+ serve 解决的 `thread.join` 阻塞顽疾
- [[0025_desktop-control-plane]] —— 桌面端完整设计：三层模型、两种 provider 形态、capability-driven UI、demo/live 兜底、roadmap
- [[0006_session-design]] —— session_key 推导，会话映射的基础
- [[0023_multi-instance-config]] —— `xihe --config X serve --port N` 多实例隔离
- [[0002_tool-registry-and-dispatch]] —— 能力描述符的推导来源（registry + check_fn 门控）
- [[0001_xihe-agent]] —— 项目总览，三种运行模式
