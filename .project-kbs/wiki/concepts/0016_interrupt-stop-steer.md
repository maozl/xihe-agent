---
type: concept
title: 中断 / 停止 / Steer——任务运行时的控制通道设计
slug: 0016_interrupt-stop-steer
aliases:
  - interrupt
  - stop
  - /stop
  - steer
  - 任务中断
  - 可中断工具
  - interruptible_iter
tags:
  - gateway
  - agent-loop
  - interrupt
  - concurrency
  - tools
status: active
created: 2026-07-17
updated: 2026-07-17
related_pages:
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/insights/0009_agent-security-master-identity.md
---

# 中断 / 停止 / Steer——任务运行时的控制通道设计

## 摘要

gateway 模式下，agent 一轮可能跑很久（搜索、SSH、子进程）。需要让用户能 (a) **停掉**当前轮、(b) **中途补充**指令而不打断。核心设计：**停止走独立的"带外控制通道"**（绕过消息队列，直接中断），与普通消息流分离——这样停止信号永远不会被卡在正在跑的任务后面（这是最初的致命 bug）。中断是 **per-agent** 的（contextvar，按会话隔离），工具靠**协作式轮询**（循环类用 `interruptible_iter`，子进程类靠注册表 kill）响应。Steer 是**不打断的中途注入**，在迭代边界塞进当前轮；被停止时丢弃，自然结束时若有未消费的 steer 则转成新轮。

## 三种消息路由（适配器入口）

`platforms/wecom.py` `_on_callback` 按顺序分流：

| 消息 | 条件 | 去向 |
|---|---|---|
| **停止意图** | `is_stop_intent(text)` 且注册了中断 hook | **带外**：`await self._on_interrupt(event)`，**不进队列** |
| 普通消息 + 有活跃轮 | steer hook 返回 True | `agent.steer(text)`（注入当前轮）+ ack |
| 普通消息 + 无活跃轮 | — | 进 per-session 队列当下一轮 |

`is_stop_intent`（`gateway/commands.py`）：识别 `/stop`、`/cancel` + 自然语言整句匹配（停/停止/中断/结束/取消/算了/done/finish…，见 `_STOP_PHRASES`）。**整条消息精确匹配**，避免"停止监控"误触发；`/stop` 是 100% 保底。

## 中断机制（agent 层 + gateway 层）

### 单一入口 `interrupt_session`
`gateway/bot.py` 模块级 `interrupt_session(session_key) -> bool`：查 `_active_agents[sk]`（模块级 dict + lock）→ `agent.interrupt()`。所有中断路径都走它（adapter hook、`/stop` fallback、handle_message 安全网）。

### per-agent 中断标志（contextvar）
`tools/interrupt.py` 用 `contextvars.ContextVar` 存"当前线程跑的 agent"。agent 分发循环在每个工具调用前后 `bind_current_agent(self)`/`reset`（顺序+并行两条路径都绑）。`is_interrupted()` 读**当前 agent** 的标志——**按会话隔离**：A 的 `/stop` 不会让 B 的工具误退（旧的全局 `threading.Event` 会串）。

### `XiheAgent.interrupt()`
设 `_interrupt_requested` → **`kill_subprocesses()`**（杀该 agent 注册的子进程）→ 传播给子 agent（各自 kill 自己的子进程）。agent 循环在每个迭代边界 `_check_interrupt()` → 返回 `"[interrupted]"`。

## 可中断工具（两套 primitive）

**没有"通用 kill 线程"**——Python 杀线程不安全且对阻塞在 C/subprocess 的线程无效。按工具类型分两套：

| 工具类型 | primitive | 机制 | 已接入 |
|---|---|---|---|
| 纯 Python 循环 | `interruptible_iter(iter, every=32)` | 包迭代器，每 N 项查 `is_interrupted()`，中断即停 yield；调用方查标志返回部分结果 | `search_files`（4 处循环）|
| 子进程 | `register_subprocess(proc)` / `run_interruptible(...)` | Popen 后注册到 per-agent 表，`interrupt()` 时 `proc.kill()` → `wait/communicate` 立即返回 | `terminal`、`execute_code`、`maven`（`subprocess.run`→`run_interruptible`）|
| shell recv 循环 | 直接轮询 `is_interrupted()` | — | `ssh_tool._read_until_prompt` |
| 单次阻塞调用（http/browser/mcp/vision）| 无 | 靠各自 timeout；中途打断需 per-library（低 ROI）| — |

`run_interruptible` 是 `subprocess.run` 的直接替代（同签名），自动注册/注销，工具不用传 `parent_agent`（走 contextvar）。`interruptible_iter` 也是 contextvar 自动绑定——新循环工具 `for x in interruptible_iter(...)` 一行接入。

**delegate** 不用自己处理：中断从父传播到子 agent，子的工具各自停。

## Steer（不打断的中途补充）

- 用户在轮跑期间发普通消息 → `_handle_steer` → `agent.steer(text)`（进 `_steer_messages` 缓冲）→ 回 `📝 收到，会结合这条继续处理。`
- agent 循环在每个迭代边界（`_check_interrupt` 之后、调模型之前）`_drain_steer()`，以 `[用户中途补充] …` 注入为 user message → 模型据此调整下一步。
- **只在迭代边界生效**——模型正在生成中途插不进去（同 Claude Code 的 steer）。
- **轮结束时的处理**（`handle_message` 末尾，`_drain_steer()`）：
  - **被用户停止**（`was_interrupted`）→ **丢弃**（否则停止后会立刻把 steer 开成新轮，看起来"没停"）。
  - **自然结束** + 有未消费 steer（最后生成窗口到的）→ **转成新轮**（`asyncio.create_task(handle_message(合成事件))`），不丢真实消息。

## 收尾 UX

- `/stop` 立刻回 `⏹ 已发送停止信号，正在中断当前任务…`（`_handle_stop_intent`）。
- 被中断轮真正结束后回 `✅ 任务已停止。`（handle_message 检测 `response=="[interrupted]"`）。
- 无活跃轮时回 `没有正在运行的任务。`

## 关键坑（演进过程中踩的）

1. **队列阻塞**（最初的致命 bug）：`/stop` 和任务挤同一 per-session 队列 → 排在运行轮后面 → 永远停不掉。解：停止意图走带外 hook 绕过队列。
2. **全局中断跨会话串**：旧 `threading.Event` 全局 → A 的 `/stop` 误杀 B 的工具。解：改 per-agent contextvar。
3. **工具不轮询 → 卡死**：search_files 跑 5 分钟不看中断。解：`interruptible_iter`（循环）+ 子进程注册表（子进程）。
4. **60s 并行总超时**（已删）：`as_completed(timeout=60)` + `shutdown(wait=True)` → 不省时（照样等到完成）还丢真实结果（113s 的 search 结果被假"超时"错误覆盖）。解：删总超时，等全部完成，各工具自己的 timeout + 中断兜底。
5. **流式 errcode 40008**：重排的 steer 用合成 msg_id，无 `_reply_req_ids` 上下文 → 企微拒绝流式。解：`use_streaming` 必须有有效 `reply_req_id`，否则走普通发送。
6. **停止后 steer 被重排 → 看起来没停**：option B 对任何轮结束都重排 steer，停止时也会立刻开新轮。解：停止时丢弃、自然结束时才重排。
7. **停止词不全**：`结束` 没收录 → 被 steer 而非停止。解：扩 `_STOP_PHRASES`。局限：整句精确匹配，`结束当前任务吧` 认不出——`/stop` 保底，LLM 分类器是升级路径。

## 与 Claude Code 的对比

Claude Code 有专用按键（Esc）作中断通道，**不用做语义判断**——按键即停。xihe-agent 跑在 WeCom（只有文本消息，无带外按键），**被迫做语义识别**（`is_stop_intent`）来区分停止 vs 普通消息。这是文本-only 通道的必然代价；`interruptible_iter` / 子进程注册表 / per-agent 中断这些机制与 Claude Code 的协作式中断约束一致。

## 相关页面

- [[0011_gateway-architecture]] — 每消息新建薄 agent、模块级共享状态、per-session 队列（本页的中断/steer 在此之上）
- [[0002_tool-registry-and-dispatch]] — 工具分发循环绑定 current agent（中断 contextvar 的注入点）
- [[0009_agent-security-master-identity]] — 中断是"谁能让 agent 停"的控制权问题，与主人身份/权限相关
