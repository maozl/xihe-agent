---
type: concept
title: 桌面端 claude 接入架构（长驻 stream-json STDIO 传输层）
slug: 0028_desktop-claude-transport-architecture
aliases:
  - ClaudeRunner 架构
  - claude 长驻进程模型
tags:
  - architecture
  - desktop
  - claude
  - transport
status: active
created: 2026-08-12
updated: 2026-08-12
related_pages:
  - wiki/changes/0027_desktop-claude-longlived-rewrite.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/concepts/0025_desktop-control-plane.md
  - wiki/concepts/0029_desktop-dual-engine-architecture.md
sources:
  - path: raw/sources/Claude Code CLI & SDK WebSocket Protocol 整合手册.md
    date: 2026-08-12
---

# 桌面端 claude 接入架构（长驻 stream-json STDIO 传输层）

## 摘要

xihe-desktop 把本地 `claude` CLI 当作**第二种 agent 引擎**接入，与 `xihe serve`（WS）并列。传输层 = **每个会话（convId）一个长驻 claude 子进程**，main 用保持打开的 stdin 跨轮喂 NDJSON turn 消息，stdout 回 NDJSON 事件帧 → 映射成与 xihe 同形的 `ServeEvent` → 复用 renderer 的 `handleEvent` 归约。进程死了（崩溃/中断/退出 app）下次 `send()` 用 `--resume <sid>` 冷续同会话。**热/冷双路径实测通过**（2026-08-12）。本页是稳定的架构参考；具体某次重构的 before/after 见 [[0027_desktop-claude-longlived-rewrite]]。

## 核心要点

- **接入点 = 本地 CLI 子进程**，不是 HTTP 端点、不直连 Anthropic API。claude 进程经内部网关（`ANTHROPIC_BASE_URL`）打模型，桌面向子进程注入 env + `--model`。
- **一个会话 = 一个长驻进程**（`Map<convId, LongLivedSession>`）。stdin 跨轮不关，每轮写一行 NDJSON `{"type":"user","message":{"role":"user","content":"..."}}`。
- **事件协议复用 xihe 的 `ServeEvent`**：claude NDJSON → 同形帧 → renderer 按引擎无关的 `conv_id` 路由。两路传输（xihe WS / claude STDIO）共享 `handleEvent`/`patchPending`。
- **main 无状态于「会话内容」，有状态于「进程」**：roster/llm-config/历史/resume-map 全在 renderer；main 只持有进程 map + NDJSON 解析。
- **冷续无损**：killTree 杀进程后下一轮 `--resume` 续同会话，模型记忆不丢（claude 自带会话持久化，文件落钉死 cwd 下）。

## 整体数据流

```
┌─ renderer (store.ts) ─────────────────────────────────────────┐
│  sendClaudeTurn(agent, convId, text)                          │
│    · llm = agent.llmConfig; permissionMode = agent.permissionMode
│    · cwd / sessionId 从 claudeResume[convId] 取（首轮钉 cwd）  │
│    · desktop.claudeSend({convId, prompt, cwd, sessionId, llm, │
│                          permissionMode})                     │
│  handleEvent(frame)  ←── onClaudeEvent 订阅（按 conv_id 归约） │
│    text_delta/thought_delta/tool_call/tool_result/complete/error
└────────┬───────────────────────────────────────▲──────────────┘
    IPC ↓ claude:send                        IPC ↑ claude:event
┌─ main (index.ts + claude.ts) ─────────────────────────────────┐
│  claude:send handler → 校验 payload → ClaudeRunner.send(req)  │
│  pushClaudeEvent(frame) → webContents.send('claude:event')    │
│                  (!isDestroyed() 守卫)                         │
│                                                                │
│  ClaudeRunner                                                 │
│    sessions: Map<convId, LongLivedSession>                    │
│    send → ensureSession(懒 spawn) → writeTurn(stdin NDJSON)   │
│    attachStdout: stdout NDJSON 行 → handleLine → onStreamEvent/
│                  onUserMessage/onResult → finalize(发帧)       │
│    interrupt / dispose / stop  (killTree)                     │
└────────┬───────────────────────────────────────▲──────────────┘
     spawn ↓ stdin (NDJSON turn)         stdout ↑ (NDJSON events)
┌─ claude 子进程（长驻，一会话一个）─────────────────────────────┐
│  -p --input-format stream-json --output-format stream-json    │
│  --verbose --include-partial-messages --permission-mode <m>   │
│  --model <m> [--resume <sid>]                                 │
│  env: ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL (内部网关)        │
│  会话持久化 ON（文件落 cwd）→ 供冷 resume                       │
└────────────────────────────────────────────────────────────────┘
```

## 进程与会话生命周期（LongLivedSession）

数据模型（`src/main/claude.ts`）：

```ts
interface LongLivedSession {
  convId: string
  child: ChildProcess
  stdin: NodeJS.WritableStream     // 跨轮保持开；每轮写一行 NDJSON，teardown 外不 .end()
  cwd: string                      // spawn 时钉死；--resume 依赖（会话文件相对 cwd）
  llm: LlmConfig                   // spawn 时烙入（env+--model）；改配置不热生效
  permissionMode: ClaudePermissionMode
  sessionId?: string               // 从 system/init 捕获，每个 complete 帧回传
  lineBuf: string                  // stdout NDJSON 行缓冲（进程级）
  ready: boolean                   // 首个 system/init 到达后 true
  pendingSend: ClaudeTurnRequest | null  // ready 前到达的轮次（单槽缓冲，claude 串行）
  currentTurn: TurnScratch | null  // 非 null = 有轮在飞；NDJSON 行路由进它
  turnCounter: number              // turnId = `${convId}-${n}`
  stopping: boolean                // teardown 中（dispose/stop/interrupt），抑制意外退出 error 帧
}
```

**生命周期事件：**
| 事件 | 触发 | 动作 |
|------|------|------|
| **懒 spawn** | 首次 `send()`（map 无项）| `ensureSession`：spawn + `pipeLogs` + `attachStdout` + `ready:false` |
| **就绪** | 首个 `system/init` 帧 | `ready=true` + 清就绪超时 timer（`onReady`）。**不再 flush**——`send()` 冷启直接 `writeTurn`（stream-json 须先收到 stdin 首行才 boot，buffer 会死锁，见「关键不变式」） |
| **就绪超时**（2026-08-12）| spawn 后 `READY_TIMEOUT_MS`（45s）仍 `!ready` | 发 error 帧（带 stderr 尾）+ `teardown`（下次 send 冷 resume） |
| **空闲回收**（2026-08-12）| `finalize` 后 `IDLE_TTL_MS`（10min）仍空闲 | `teardown`（下次 send 冷 `--resume` 无损）；`writeTurn`/`teardown` 清此 timer |
| **轮次开始** | `writeTurn` | 新建 `TurnScratch` + 往 stdin 写一行 user NDJSON |
| **轮次结束** | `result` 帧 / 进程退出 | `finalize` 发 complete/error 帧 + 清 `currentTurn`（**会话保活**） |
| **中断**（stop 按钮）| `interrupt(convId)` | finalize `interrupted` + `killTree` + 删 map 项（下次 send 冷 resume） |
| **销毁**（删会话）| `dispose(convId)` | 空闲→`killTree`+删项；有轮在飞→回退 interrupt |
| **意外退出** | child exit 且 `!stopping` | 在飞轮次→error 帧；删项（下次 send 懒重启，**不自动重启**） |
| **app 退出** | `before-quit` → `stop()` | 幂等；遍历所有 session `killTree`；`clear()`；关 logStream |

**关键不变式：**
- `currentTurn !== null` ⇒ 有轮在飞 ⇒ 同 convId 第二个 `send()` 被 error 帧拒绝（防堆叠，renderer 也守一道）。
- NDJSON 解析器**每个会话只 attach 一次**，永远路由进 `session.currentTurn`；`currentTurn === null` 时的行（轮间杂帧）尽力丢弃。
- **stdin-boot（2026-08-12 坐实）**：`--input-format stream-json` 模式下 claude **必须先收到 stdin 首行才 boot + 吐 `system/init`**。故 `send()` 对冷启会话**直接 `writeTurn`**（写入即 boot 触发），`onReady` 只清就绪 timer、不 flush 任何 buffer——「等 init 再 write」会双向死等。这是长驻重写后实测暴露的死锁真根因（probe 不 write → 20s 无输出；write → 9s 拿 init）。
- **cwd 会话生命周期内钉死、永不改**——renderer 经 `claudeResume[convId].cwd` 保证首轮钉死后续复用；这对冷 `--resume` 是承重的（claude 会话文件相对 cwd）。
- 与 [[0011_gateway-architecture]]/ServeSupervisor 区别：**无 liveness 轮询、无 auto-restart/backoff**——claude 进程退出就退出，下次 send 懒重启（带 resume）。理由：中途静默重启会吞掉在飞轮次的流式状态且不告诉用户。有的是**一次性 45s 就绪超时**（spawn 后没 init 就报错 teardown，非持续轮询）+ **10min 空闲回收**（2026-08-12 加）。

## 冷/热双路径（实测 2026-08-12）

**热路径**（进程活着）：`writeTurn` 往 stdin 写一行 NDJSON → claude 当新轮处理 → 流式回 → `result/success` 后进程继续等下一条。同进程同 session_id 跨轮，`cache_read_input_tokens` 命中（实测 25856）证明进程内 prompt-cache 复用。

**冷路径**（进程死了）：下次 `send()` 发现 map 无项 → `ensureSession` 用 `--resume <sid>` 重 spawn（`sid` 来自 renderer 的 `claudeResume[convId].sessionId`，由前一轮 complete 帧钉死）→ claude 读自家持久化会话文件续同会话。实测：kill 进程 A（turn1 记暗号 sid=8f907cec）→ 进程 B `--resume 8f907cec` turn2 问暗号 → 答 `BLUEFIRE`，同 sid 跨进程复活、模型记忆不丢。

→ **跨 app 重启续接免费入范围**（app 退出杀进程，但 claude 会话文件在盘上；重开 send 即 resume 续上）；**消除「中断丢上下文」权衡**（kill 进程后下一轮 resume 续上）。

## NDJSON 事件映射（claude → ServeEvent）

renderer 把 claude 帧当 `ServeEvent` 消费（按 `conv_id` 路由，引擎无关）。main 的映射：

| claude stdout NDJSON | → ServeEvent 形帧 | 备注 |
|---|---|---|
| `system`·`subtype:init` | （不发帧）| 标记 ready + flush pendingSend + 捕 session_id |
| `stream_event`·`content_block_delta`·`text_delta` | `text_delta` | 真流式分片 |
| `stream_event`·`content_block_delta`·`thinking_delta` | `thought_delta` | glm-5.2-zp 默认不发（见 [[0025]] 待办） |
| `stream_event`·`content_block_start`·`tool_use` + 累积 `input_json_delta` → `content_block_stop` | `tool_call`（name + 拼整 args）| args 分片缓冲，block stop 时拼整发一帧 |
| `user`·`content[].type==='tool_result'` | `tool_result`（FIFO 配对 tool_use_id→name）| claude 自家工具往返 |
| `result`·subtype `success` | `complete`（text + session_id）| 轮次结束标志 |
| `result`·subtype error / 非零退出 | `error` | |

兜底：若一轮没流式出文字（partial 解析失败 / 只回 tool_call），`onResult` 用 `result.result` 文本兜底补一帧 text_delta。

## 跨边界协议（IPC）

**控制通道**（renderer → main，`ipcRenderer.invoke`）：
| 通道 | 载荷 | main 动作 |
|------|------|-----------|
| `claude:send` | `{convId, prompt, cwd, sessionId?, llm, permissionMode}` | 校验 → `claude.send(req)` |
| `claude:interrupt` | `convId: string` | `claude.interrupt(convId)`（停在飞轮次；空闲 no-op） |
| `claude:dispose` | `convId: string` | `claude.dispose(convId)`（拆空闲会话；删会话用） |

**事件通道**（main → renderer，`webContents.send`）：
- `claude:event`：推 `ClaudeEvent` 帧（= `ServeEvent` 子集）。`pushClaudeEvent` 经 `BrowserWindow.getAllWindows()[0]` + `!isDestroyed()` 守卫。

**preload 桥**（`contextBridge.exposeInMainWorld('desktop', …)`）：`claudeSend` / `claudeInterrupt` / `claudeDispose` / `onClaudeEvent`（same-ref removeListener 模式，React StrictMode 安全）。

> `claude:send` 不查 agents.json——payload 由 renderer 全量组装（main 无状态于 roster/llm-config）。agents.json 只被 main 读 `xiheLlm`（注入 xihe serve 子进程，与 claude 无关）。

## 凭据注入（spawn 时）

`src/main/llmConfig.ts`：`claudeEnv(llm)` → `{ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL}`；`claudeModelArg(llm)` → `['--model', llm.model]`。spawn 时 `env: { ...process.env, ...claudeEnv(llm) }`。

- **api_key 永不 log、永不经 serve 端点暴露、UI 掩码（`sk-…****`）**，只在内存/盘（`~/.xihe-desktop/agents.json`）。
- airgap：`base_url` 指内部网关（如 `http://<内部网关IP>/public/`），模型多为 `glm-5.2-zp`（**非多模态**，见自动记忆）。

## Spawn 矩阵

与 xihe serve（ServeSupervisor）完全一致：
- `bin = process.env.CLAUDE_BIN ?? 'claude'`
- `shell: process.platform === 'win32' && !isAbsolute(bin)`（Windows `.cmd` npm shim 需 PATHEXT；绝对 `CLAUDE_BIN` 直 spawn）
- `windowsHide: true`
- `detached: process.platform !== 'win32'`（POSIX 自成进程组，`kill(-pid)` 才杀得全）
- `cwd: req.cwd ?? homedir()`
- **Windows stdin 空格 bug**：prompt 不走 argv（`claude.cmd` 在 shell 层被 cmd.exe 按空格切词），改走 stdin。长驻模式下 stdin 永远是 NDJSON 文本，天然规避。

## 中断与销毁（2026-08-12 后两者收敛为 halt）

硬化后 `interrupt()` 与 `dispose()` 都走 `halt()`——**无论是否有轮在飞、无论是否还在 boot 窗口，都 finalize + killTree + 删项**。二者只余调用点区分（stop 按钮 vs 删会话），行为已无差异。硬化前的「interrupt 仅在有轮在飞时生效」会让 boot 窗口里的停止按钮 no-op，已修。

| | interrupt(convId) | dispose(convId) |
|---|---|---|
| 触发 | stop 按钮（renderer `interrupt`） | 删会话（renderer `deleteConversation`） |
| 前提 | **无条件**（含 ready 前 boot 窗口） | 同 interrupt（收敛到 halt） |
| 行为 | finalize `interrupted`（若有在飞轮）+ killTree + 删项 | killTree + 删项 |
| 目的 | 停/杀会话 | 拆会话进程 |

`deleteConversation` 对 claude agent **无条件** `claudeDispose`（旧逻辑仅 pending 时 interrupt，空闲进程会泄漏）。

## 持久化（renderer 持有，main 透明 bridge）

| 文件 | 内容 | 读写 |
|------|------|------|
| `~/.xihe-desktop/agents.json` | userAgents（含 claude agent + llmConfig）+ xiheLlm | renderer 持有；main 只读 `xiheLlm` |
| `~/.xihe-desktop/claude-sessions.json` | `{sessions: Record<convId, Message[]>, resume: Record<convId, {sessionId?, cwd}>}` | renderer 持有；main 是 dumb atomic fs bridge |
| `~/.xihe-desktop/claude.log` | claude 子进程 stdout+stderr（best-effort 追加）| main `pipeLogs` 追加 |
| claude 自家会话文件 | 落 cwd 下 | claude 进程写（持久化 ON）；冷 resume 读 |

`claudeResume[convId]`：首轮钉 `{cwd}`；complete 帧带 `session_id` 时并入 `{cwd, sessionId}`；删会话时清。这是冷 resume 的数据源。

## 适用边界 / 何时参考本页

- 改 claude 传输层（spawn args、NDJSON 映射、生命周期）。
- 加新 claude 控制操作（如协议级 interrupt、权限审批 UI）——本页是现状基线。
- 排查「claude 会话不续接 / 进程泄漏 / 中断后失忆」——查生命周期表 + 冷热路径。
- 对比 xihe serve 传输（[[0024_desktop-serve-protocol]]）：两路并列、共享 renderer 归约、IPC 形状不同（WS vs STDIO）。

## 已知限制（v1）

1. **无协议级 interrupt**：用 killTree + 冷 resume（`control_request`/`interrupt` 字段未实测，见整合手册第二部分，标注「部分 AI 生成」）。热进程 + 其进程内 cache 在 interrupt 时丢，下一轮付 ~5s 重启，模型记忆靠 resume 活。
2. **无权限审批 UI**：`permission_request`/`control_response` 未实现；bypassPermissions / acceptEdits 够 v1。
3. **改 agent 配置不热生效**：spawn 时烙入 llm/permissionMode/cwd；workaround = dispose 后下次 resume。
4. **cwd 会话生命周期内钉死**：目录被删 → `--resume` 失败 → error 帧。
5. **resume 依赖 claude 落盘会话文件**：损坏/禁用 → error 帧（v1 不自动回退新会话）。

> 空闲回收（旧限制「无空闲回收」）已于 2026-08-12 落地（10min `IDLE_TTL_MS`）；就绪超时（45s）+ PID 文件孤儿清扫同期加入。完整硬化清单见 [[0029_desktop-dual-engine-architecture]] 的「ClaudeRunner 生命周期健壮性」节。

## 相关页面

- [[0029_desktop-dual-engine-architecture]] — 桌面端双引擎架构总览（本页的上一层地图：两路并列、统一归约、main 总编排）。
- [[0027_desktop-claude-longlived-rewrite]] — 本架构的落地变更记录（before/after、验证）。
- [[0026_desktop-agent-model-built-in-xihe]] — 桌面 Agent 模型定调（claude = 可添加 agent 类型）。
- [[0024_desktop-serve-protocol]] — xihe serve 传输层（与本页并列的第二路）。
- [[0025_desktop-control-plane]] — 桌面控制面整体设计（三层模型、能力驱动 UI、renderer store）。
