---
type: concept
title: 桌面端双引擎架构总览（xihe serve + claude，统一 ServeEvent）
slug: 0029_desktop-dual-engine-architecture
aliases:
  - 桌面端整体架构
  - xihe-desktop 双引擎
  - desktop architecture overview
tags:
  - architecture
  - desktop
  - xihe
  - claude
  - transport
status: active
created: 2026-08-12
updated: 2026-08-12
related_pages:
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/concepts/0025_desktop-control-plane.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/changes/0027_desktop-claude-longlived-rewrite.md
  - wiki/concepts/0028_desktop-claude-transport-architecture.md
---

# 桌面端双引擎架构总览（xihe serve + claude，统一 ServeEvent）

## 摘要

xihe-desktop（独立仓 `E:\xihe-desktop`，Electron 控制面）用**同一套 renderer/store** 同时驱动**两种 agent 引擎**：

- **内置 xihe**——main 进程托管一个 `xihe serve` 子进程（内嵌 aiohttp HTTP+WebSocket server，每轮新建薄 `XiheAgent`）。
- **可添加 claude**——main 持有「每会话一个长驻 `claude` CLI 子进程」的 map，stdin 跨轮喂 NDJSON。

两路传输介质不同（**WS** vs **STDIO**）、事件抵达通道不同（WS 客户端 vs `claude:event` IPC 推送），但都被映射成**同一个 `ServeEvent` 帧联合**，renderer 的 `handleEvent` 按 `conv_id` **引擎无关**地归约。本页是桌面端整体架构的**总览与索引**——把分散在 [[0024]]（serve 协议）、[[0025]]（控制面/UI/store）、[[0028]]（claude 传输）的拼图合为一张地图，并给出两路传输的正面对照。各引擎深水区不在此重复，一律 `[[slug]]` 链出。

> 读这页的前提结论：**main 是编排者不是引擎；renderer 是引擎无关的归约层；两路传输共享同一个事件协议与 store reducer。**

## 核心要点

- **双引擎并列**：xihe（内置、process provider、main 托管 serve 子进程）+ claude（可添加、connector、main 持有长驻 CLI 子进程 map）。Agent 模型定调见 [[0026_desktop-agent-model-built-in-xihe]]。
- **统一事件协议**：两路都产 `ServeEvent` 形帧；renderer `handleEvent` / `patchPending` 引擎无关，按 `conv_id` 路由。claude 的 `ClaudeEvent` 是 `ServeEvent` 的结构兼容子集（见 [[0028]] 映射表）。
- **main 无状态于「会话内容」，有状态于「进程」**：roster / llm-config / 历史 / resume-map 全在 renderer；main 只持有 serve 子进程句柄 + claude 进程 map + IPC 桥。
- **控制流按引擎分流**：store.sendMessage 依 `agent.engine` 分发——xihe 走 WS `stream.sendTurn`，claude 走 IPC `desktop.claudeSend`。
- **会话真理归属分形态**：xihe 归 serve 的 SQLite `sessions.db`（跨重启/跨模式存活）；claude 归 claude 自家落盘会话文件（冷 `--resume` 读）+ renderer 的 `claude-sessions.json`（历史 + resume map）。

## 双引擎总览图

```
┌─ renderer (store.ts) ──────────────────────────────────────────────┐
│  sendMessage(agent, convId, text)                                  │
│    └─ 依 agent.engine 分流 ─┬─ xihe   → stream.sendTurn (WS)       │
│                             └─ claude → desktop.claudeSend (IPC)    │
│                                                                     │
│  handleEvent(frame)   ←──── 引擎无关，按 conv_id 归约 ────┐         │
│    text_delta / thought_delta / tool_call /               │         │
│    tool_result / complete / error                         │         │
└──────────┬──────────────────────────────▲──────────────────┼─────────┘
    WS ↑ 事件(stream)            IPC ↑ claude:event         │
    (serveClient.ts)             (onClaudeEvent)            │
┌──────────┴──────────────────────────────┴─────────────────┼─────────┐
│  main (index.ts) — 总编排                                  │         │
│  ┌─ ServeSupervisor (serve.ts) ──┐  ┌─ ClaudeRunner (claude.ts) ──┐ │
│  │ · adopt-or-spawn xihe serve    │  │ · Map<convId, 长驻 claude   │ │
│  │ · readiness/liveness/restart   │  │   子进程>                    │ │
│  │ · xihe:status push (进程态)    │  │ · stdin NDJSON turns         │ │
│  │                                │  │ · stdout → ServeEvent 帧     │ │
│  │                                │  │ · claude:event push          │ │
│  └────────────────────────────────┘  └──────────────────────────────┘ │
│  IPC 桥: workspace/fs/agentStore/claudeSessions + dialog             │
│  before-quit: claude.stop() + supervisor.stop()                      │
└──────────┬──────────────────────────────────▲────────────────────────┘
   spawn ↓ (HTTP+WS server)              spawn ↓ stdin ↓ / stdout ↑ (NDJSON)
┌─ xihe serve 子进程（全局 1 个）──────┐  ┌─ claude 子进程（每会话 1 个）────┐
│  aiohttp 127.0.0.1:7788              │  │  -p --input-format stream-json  │
│  REST + WS /stream                   │  │  env: ANTHROPIC_* (内部网关)     │
│  每轮薄 XiheAgent (run_in_executor)  │  │  会话文件落 cwd（冷 resume 读）  │
│  SQLite sessions.db (跨重启)         │  │  长驻跨轮，prompt-cache 复用     │
└──────────────────────────────────────┘  └─────────────────────────────────┘
```

两条虚线箭头是关键：**xihe 的事件经 WS 客户端进 renderer；claude 的事件经 IPC 推送进 renderer**——抵达通道不同，但都喂给同一个 `handleEvent`。

## 两路传输对照

两路并列、互补，不存在谁取代谁。xihe 是「内置默认 + 多会话共享一进程 + 服务端有状态」；claude 是「可添加 + 一会话一进程 + 客户端持有会话真理」。

| 维度 | xihe serve（[[0024]]） | claude（[[0028]]） |
|------|------------------------|--------------------|
| **传输介质** | HTTP（REST）+ WebSocket `/stream` | STDIO（stdin NDJSON in / stdout NDJSON out） |
| **接入点** | `xihe serve` 子进程内嵌 aiohttp server（`127.0.0.1:7788`） | 本地 `claude` CLI 二进制（spawn） |
| **进程模型** | main 托管**全局 1 个** serve 子进程；serve 内部每轮新建**薄 XiheAgent** | main 持有 `Map<convId, 长驻 claude 子进程>`，**一会话一个** |
| **并发模型** | serve 侧 `run_in_executor`（工作线程）+ `_turn_locks` 同会话串行 + Emitter（stdlib `queue.Queue` → WS 协程） | 单进程 stdin 序列（claude 自身串行）；同 convId 第二个 `send` 被 error 帧拒 |
| **事件抵达通道** | WS 客户端（`serveClient.ts`）→ `handleEvent` | IPC `claude:event` 推送（`onClaudeEvent`）→ `handleEvent` |
| **会话真理归属** | serve 的 SQLite `sessions.db`（`agent:main:serve:dm:{conv_id}`），跨重启/跨模式 | claude 自家落盘会话文件（相对 cwd）+ renderer `claude-sessions.json` |
| **冷启动开销** | 无（serve 长驻；薄 agent 每轮建很便宜） | 进程死后下次 `send` 冷 spawn + `--resume`（~5s），无损续同会话 |
| **上下文/cache 复用** | 每轮薄 agent 重建，但 SQLite 历史续接；serve 持 `ContextCompressor` 等重对象 | 长驻进程内 prompt-cache 跨轮复用（实测 `cache_read` 命中，见 [[0027]]） |
| **中断语义** | 协作式：WS `interrupt` → serve 的 `_active` + 锁 → `agent.interrupt()`（循环轮询） | 非协作式：`killTree`（杀进程树）→ 下次冷 `--resume` 续上 |
| **自动重启** | 有：owned 子进程意外退出按 backoff `[1,2,5]s` 自动拉起 | 无：进程退出即退出，下次 `send` 懒 respawn（带 resume） |
| **cwd 语义** | 无 per-conv cwd（serve 在自家 `agent_home` 跑） | per-conv 钉死（首轮定，后续不变，`--resume` 承重依赖） |
| **凭据注入路径** | `xiheEnv(xiheLlm)` → serve 子进程 env（serve 读 env last → 覆盖 xihe 自家 `.env`） | `claudeEnv(llm)` → spawn env（`ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`）+ `--model` argv |
| **能力描述符** | serve 从 registry 实时推导（`check_fn` 门控），`/agents` 下发 | 固定（claude 工具集由 CLI 自带，桌面不裁剪） |

> 「为什么 xihe 协作式中断、claude 杀进程」：xihe 的 agent 循环有内建中断点（[[0016_interrupt-stop-steer]]），能干净停在轮次边界；claude 是黑盒 CLI 子进程，桌面拿不到内部循环，只能杀进程树、靠 `--resume` 续。代价是热进程 + 其进程内 cache 在中断时丢，下一轮付 ~5s 重启。

## 统一归约层（ServeEvent + handleEvent）

两路传输能在同一个 UI 里无差别显示，靠的是**事件协议的公约数**：

- **`ServeEvent` 帧联合**（[[0024]] 的 WS 契约定义）：`hello` / `turn_start` / `text_delta` / `thought_delta` / `tool_call` / `tool_result` / `complete` / `error`。
- **claude 映射到其子集**：`ClaudeEvent`（[[0028]]）只产 `text_delta` / `thought_delta` / `tool_call` / `tool_result` / `complete` / `error`——结构兼容 `ServeEvent`，renderer 把 IPC 载荷 cast 成 `ServeEvent` 消费，按 `conv_id` 路由，**从不读 `turn_id` 之外引擎专属字段**。
- **`handleEvent(frame)`**：renderer store 的引擎无关 reducer。无论帧来自 WS 还是 IPC，都走同一段「按 conv_id 找会话 → 追加消息流 → patchPending」逻辑。
- **出站分流、入站合流**：`sendMessage` 按 `agent.engine` 分到 WS 或 IPC（出站分两路）；两路回来的帧都进 `handleEvent`（入站合一路）。这是「能力驱动 UI」在传输层的体现——UI 不 sniff 引擎名（`EngineBadge` 配色是唯一例外，见 [[0025]]）。

## main 进程总编排（F1/F2 现状）

main（`src/main/index.ts`）是**唯一的进程编排者**，持有三个角色：

1. **ServeSupervisor（`serve.ts`）——托管内置 xihe**。adopt-or-spawn 策略：启动先探 `/health`，已在跑（dev 手起的 serve）就 adopt（`owned=false`，退出不杀），否则 spawn 自家子进程（`owned=true`，退出 tree-kill）。就绪轮询 400ms 一次、15s 截止；稳态 liveness 6s 一次、fetch 2s 超时（AbortController 兜半开 TCP）；owned 子进程意外退出按 `[1,2,5]s` backoff 自动重启。进程态经 `xihe:status` 推 renderer（+ mount 时 pull 一次覆盖 pre-`did-finish-load` 丢帧窗口）。详见 `serve.ts`。
2. **ClaudeRunner（`claude.ts`）——驱动可添加 claude**。`Map<convId, LongLivedSession>`；懒 spawn + stdin 跨轮喂 NDJSON；进程死后下次 `send` 冷 `--resume` 续同会话。**生命周期健壮性已硬化**（2026-08-12，见下节）。不经 IPC 端点暴露 roster/llm-config——renderer 每轮全量组装 payload。详见 [[0028]]。
3. **IPC 桥（`index.ts` `registerIpc`）**——renderer 在沙箱里碰不到 fs/child_process，main 是它的 fs + 进程代理。通道分四组：
   - 控制通道：`claude:send` / `claude:interrupt` / `claude:dispose`、`xihe:status`（pull）、`desktop:ping`。
   - 事件推送：`xihe:status`（supervisor onStatus）、`claude:event`（`pushClaudeEvent`，`!isDestroyed()` 守卫）。
   - 存储 bridge（renderer 持有、main 原子读写）：`workspace:load/save`、`agentStore:load/save`、`claudeSessions:load/save`。
   - 文件树（纯桌面侧，不涉 serve）：`dialog:openDirectory`、`fs:listDir/readFile/writeFile/createFile/createDir/delete/rename`（破坏性写 sandbox 到 workspace 根，realpath + 前缀校验防符号链接逃逸）。
   - `before-quit`：`claude.stop()`（幂等，遍历 session `killTree` + 摘 PID）+ `supervisor.stop()`（仅杀 owned 子进程）。

### ClaudeRunner 生命周期健壮性（2026-08-12 增量）

长驻重写（[[0027]]）后实测暴露一个死锁 + 用户提两个增强，一并解决（main 侧 4 项，renderer API 不变；详见 [[0028]] 生命周期表）：

- **① stdin-boot 死锁（真根因）**：`--input-format stream-json` 模式下 claude **收到 stdin 首行才 boot + 吐 `system/init`**；旧 `send()` 把首轮 buffer 到 `pendingSend` 等 init 才 write → 双向死等 → 45s 超时。修：`send()` 冷启直接 `writeTurn`，去掉 buffer。
- **② interrupt 无条件 teardown**：旧 `interrupt` 头一行 `!currentTurn → return`，而 ready 前 `currentTurn` 恒 null → 点停止 no-op。修：找到 session 即 `halt`（finalize interrupted + killTree），覆盖 boot 窗口。
- **③ 45s 就绪超时**：spawn 后 `READY_TIMEOUT_MS` 仍无 `system/init` → 发 error 帧（带 stderr 尾）+ teardown，把无限 pending 变可重试错误。
- **④ 10min 空闲回收**：`finalize` 后排 `IDLE_TTL_MS` 定时器，到时仍空闲 → teardown（下次 `send` 冷 `--resume` 无损）。
- **⑤ PID 文件孤儿清扫**：spawn 登记 `~/.xihe-desktop/claude-pids.json`；app `whenReady` 调 `sweepClaudeOrphans()`，双重校验（进程在 + 命令行含 `--input-format stream-json` 指纹，防 pid 复用误杀）后 `killTree`，清异常关闭残留。`proc.ts` 加 `listProcessCommandLine(pid)`（wmic 封装）。

> 对称缺口（后续）：`xihe serve` 孤儿（ServeSupervisor 域）同构，本轮只做 claude。无原生模块（不能 Job Object），Windows 无 `PR_SET_PDEATHSIG`，故用 PID 文件 + `taskkill /T` + 指纹校验兜。

## 凭据与会话真理归属

**凭据**（`~/.xihe-desktop/agents.json`，renderer 持有）：
- **xihe 注入**：main boot 时 `readXiheLlm()` 读 `xiheLlm` → `xiheEnv()` 展成 `LLM_API_KEY`/`LLM_BASE_URL`/`MODEL` → 注入 serve 子进程 env（serve 读 env last → 覆盖 xihe 自家 `~/.xihe-agent/.env`）。无 managed config → 不注入 → xihe 回退自家 env。
- **claude 注入**：每轮 renderer 把 agent 的 `llmConfig` 塞进 `claude:send` payload → spawn 时 `claudeEnv(llm)` → env（`ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`）+ `claudeModelArg(llm)` → `--model` argv。
- **api_key 永不 log、永不经 serve 端点暴露、UI 掩码（`sk-…****`）**，只在内存/盘（`agents.json`）。

**会话真理归属**：

| | xihe | claude |
|---|------|--------|
| 历史 | serve `sessions.db`（SQLite，跨重启/跨模式） | renderer `claude-sessions.json`（消息流） |
| 续接键 | `session_key = agent:main:serve:dm:{conv_id}`（自动） | `claudeResume[convId] = {cwd, sessionId?}`（首轮钉 cwd，complete 帧回填 sessionId） |
| 冷续机制 | 无需——serve 长驻 + SQLite | 进程死后下次 `send` 带 `--resume <sid>`（claude 读自家落盘会话文件） |
| cwd 角色 | 无 per-conv | 承重（claude 会话文件相对 cwd，冷 resume 须同 cwd） |

## 阅读路径（按角色分流）

- **想理解桌面整体** → 本页。
- **改 xihe 那路传输**（REST/WS wire、Emitter 桥、会话映射、能力描述符）→ [[0024_desktop-serve-protocol]]。
- **改 claude 那路传输**（spawn args、NDJSON 映射、生命周期、冷热路径）→ [[0028_desktop-claude-transport-architecture]]；重写历史 before/after → [[0027_desktop-claude-longlived-rewrite]]。
- **改桌面 UI / store / 控制面**（三层模型、能力驱动 UI、组件、连接生命周期）→ [[0025_desktop-control-plane]]。
- **Agent 模型产品决策**（内置 xihe + 可添加 claude，否决多实例/persona）→ [[0026_desktop-agent-model-built-in-xihe]]。
- **gateway 内核参照**（serve 复刻的对象）→ [[0011_gateway-architecture]]；**中断控制通道** → [[0016_interrupt-stop-steer]]。

## 适用边界 / 何时参考本页

- 第一次接触桌面端，需要一张整体地图。
- 评估「改一处会影响两路哪一路」——查对照表 + 总编排节。
- 对比 xihe 与 claude 的传输/生命周期/中断/真理归属差异。
- 排查「事件不显示 / 路由错会话 / 凭据没生效」——先定位是出站分流、入站合流、还是注入路径的问题。

## 相关页面

- [[0024_desktop-serve-protocol]] —— xihe serve 传输层（REST+WS wire、Emitter 桥、会话映射）。
- [[0028_desktop-claude-transport-architecture]] —— claude 传输层（长驻 STDIO、生命周期、冷热路径、硬化细节）。
- [[0025_desktop-control-plane]] —— 桌面控制面整体设计（三层模型、能力驱动 UI、store）。
- [[0026_desktop-agent-model-built-in-xihe]] —— Agent 模型定调（内置 xihe + 可添加 claude）。
- [[0027_desktop-claude-longlived-rewrite]] —— ClaudeRunner 长驻重写的变更记录。
- [[0011_gateway-architecture]] —— gateway 内核（serve 复刻的 SharedContext + 薄 agent 模型）。
