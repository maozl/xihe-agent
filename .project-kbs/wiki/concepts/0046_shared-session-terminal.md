---
type: concept
title: 共享会话终端——桌面终端面板的三源通道架构
slug: 0046_shared-session-terminal
aliases:
  - 终端面板
  - terminal panel
  - _local_tap
tags:
  - desktop
  - serve
  - terminal
status: active
created: 2026-09-02
updated: 2026-09-02
related_pages:
  - wiki/changes/0043_desktop-workbench-phase1.md
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/concepts/0037_approval-permission-system.md
---

# 共享会话终端——桌面终端面板的三源通道架构

## 摘要

桌面终端面板（`TerminalPanel.tsx`）不是桌面私有的终端，而是**共享会话终端的观察面 + 第二键盘**：每个通道只有一个真相源，agent 的工具和桌面用户都是它的生产者/观察者。三类通道、三个宿主：**本地 shell**（桌面 main 进程的一个持久 ConPTY）、**agent 本地命令**（serve 进程的通道注册表，conv/proc 两种）、**SSH 会话**（serve 进程 ssh_tool 的注册表 + tap）。变更来源见 [[0043_desktop-workbench-phase1]]。

## 核心要点

### 1. Ring——所有通道的统一底座（`tools/_ssh_tap.py`）

有界字符串环，按**单调递增的字符 offset** 寻址（`base` = 保留区起点流偏移、`w` = 写指针）：append 进 chunk 列表（无整环拷贝），碎片超 4096 chunk 合并，单次 append 超环上限保尾部；`read(start)` 双向 clamp 后返回 `(text, end_offset)`。offset 寻址是断线续传与多 viewer 一致性的根基——viewer 各持 cursor，从环里按 cursor 增量读。

### 2. agent 本地命令通道（`tools/_local_tap.py`，与 _ssh_tap 同构的注册表）

- key 两种：`conv:{session_key}`（`terminal` 工具的一次性命令）与 `proc:{session_key}:{name}`（`process` 工具的常驻进程）。**session_key 是诚实的作用域**——core 不知道工作空间，桌面侧自己按对话打标签。
- conv 通道的 `active` 计数让同一对话并发回合的输出**交错如共享控制台**且 `running` 真实；proc 通道由进程独占（`attach_proc`/`proc_exit`/`live_proc`）。
- `begin`/`end` 写 `—— agent $ 命令 (cwd)` 头与 exit 尾行（绿/红 ANSI），桌面 viewer 里 agent 命令因此可辨识。
- 容量策略：每环 200K 字符；**CHANNEL_CAP 16 + 30 分钟空闲逐出，活进程的通道永不逐出**（环是该进程日志的唯一副本）；`stop_all` 在 serve 关停与解释器退出（atexit）杀光常驻进程——否则 supervisor 死掉会孤儿化用户以为被管理的 web 后端。

### 3. serve 流协议（`gateway/serve/terminal.py`，无状态模块函数）

- WS `/{ssh|local}/live/stream?key=&from=`：先 `meta`（含 `offset`/`base`），然后 `d` 数据帧。**`from` 缺省 = 尾 8K 背靠背；显式 = 断线续传 cursor（clamp 到 [base, w]，高于 w 意味着 serve 重启环重开 → 从现在开始）**。
- 数据帧**自带结束 offset，客户端 cursor 跟随服务端，永不反向**；帧上限 8K（环一次读出可达 ~1MB，单帧会卡死 renderer 的 JSON parse），50ms tick 合并同拍输出（与 chat Emitter 同哲学）。
- ssh 订阅队列只贡献**输入归属与关闭事件**，数据事件丢弃——同样的字节也在环里，cursor 寻址使环为唯一权威，消除"读 backlog 与订阅之间"的双发窗口。
- 输入不对称：**ssh 通道可输入**（`{t:"input"}` → `tap.send_user_input`，归属事件驱动"agent 输入中/手动输入"角标）；**本地通道只读**（进程归 tool 管，viewer 不能打字）。`conv:` key 不存在时按需建空通道等首条命令。
- `POST /ssh/live/connect` 走 agent 同一个 `_ssh_connect`（`origin="desktop"`），会话落共享注册表、agent 之后能接着驱动；token 只进该进程内存，不回显不留存。

### 4. 桌面三源 tab（`TerminalPanel.tsx`）

- tab key 即通道 key：`__local__`（main 的 ConPTY——node-pty，PowerShell 优先（PSReadLine 历史/编辑），cmd 兜底；懒创建、**detach 只停流不杀 shell**、app 退出或重连按钮才 kill；16K backlog）/ `conv:` / `proc:` / ssh 会话 key。
- **跟随规则：conv tab 跟随当前对话自动切换；LOCAL / ssh / proc tab 钉住**（用户可能正在打字或看构建）；非当前对话的通道以会话尾串小徽标区分，当前对话标"本对话"。
- 断线续传：tab key → 渲染到的最后帧 offset；重挂带 `cursor − 8K` 回放尾巴（serve clamp 到 base）。**同 key 复活**（agent 重连同名 alias / 重启同名进程）= 新环，旧 cursor 无意义 → 删 cursor + `attachEpoch` 重建终端；列表轮询 3s、tab 死后加速到 1s 等复活。
- 连接失败二分：列表接口返回 null = serve 不可达 → 重试；key 不在列表 = 会话/通道消失 → 置死不重试。
- pty 尺寸**跟随 viewer**（fit → `{t:"resize"}` → 服务端 resize_pty，换行按面板真实宽度计算）；别的 viewer 改了尺寸自己也跟随（resize 事件回环）。
- 本地 tab 的手敲走 main IPC（不经 serve）；**桌面手敲不经 agent 审批管线**——那是用户自己的手，agent 的 terminal/process 命令照常过 dispatch 审批门（[[0037]]）。

### 5. 自动聚焦状态链（store `agentTermTarget`）

`tool_call` 事件（name 为 terminal/process）且 `conv_id` === **活动对话**且未被静音 → `showTerminal: true` + `agentTermTarget`：
- conv 目标：立即选中该通道（服务端按需建空通道，先于首条命令打开也能等到）；
- proc 目标：等下一次列表刷新中出现**首个未见过的**新 proc key（倒序取最新——同一拍两个 start 时取后者；seen 标记在解析**之后**，否则会把目标自己刚引发的通道藏掉）。
- `terminalAutoMuted`：用户在 agent 跑命令时手动关面板 → 本会话不再自动弹（通道仍列 tab，手动可看）；重新打开面板即解除。

## 关键不变式

- 一个通道一个环一个真相源；viewer 永远从环增量读，客户端 cursor 只被服务端数据帧推进。
- 进程所有权：本地通道的进程只被 owning tool 控制（viewer 只读）；ssh 会话 agent 与桌面**共写**（第二键盘语义）。
- 活进程的通道不可逐出（日志唯一副本）；serve 退出必须树杀全部常驻进程。

## 排查指引（"agent 的命令没出现在面板"）

1. 面板是否被静音（`terminalAutoMuted`——之前手动关过）？重新打开面板解除。
2. conv tab 是否当前对话？跟随与自动聚焦只对**活动对话**生效，后台对话的通道只列 tab。
3. serve 是否可达：`GET /local/live` 列通道；key 是否被逐出（30 分钟空闲 / 16 通道上限）或 serve 重启过（环重开，旧 offset 失效——面板同 key 复活路径会自动重建）。

## 相关页面

- [[0043_desktop-workbench-phase1]] - 变更来源（工作台批次）
- [[0024_desktop-serve-protocol]] - serve 协议总览（本页是 terminal 业务的专门页）
- [[0037_approval-permission-system]] - agent 侧 terminal 命令的审批门
- [[0016_interrupt-stop-steer]] - 命令可中断性（read_until_prompt 轮询中断）
