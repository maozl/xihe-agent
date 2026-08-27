---
type: change
title: xihe-desktop ClaudeRunner 长驻 stream-json STDIO 重写
slug: 0027_desktop-claude-longlived-rewrite
change_type: refactor
risk_level: high
status: completed
created: 2026-08-12
updated: 2026-08-12
affected_services:
  - xihe-desktop
affected_modules:
  - src/main/claude.ts
  - src/main/index.ts
  - src/preload/index.ts
  - src/renderer/src/lib/desktop.ts
  - src/renderer/src/store.ts
related_insights:
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
rollback_plan: ""
---

# xihe-desktop ClaudeRunner 长驻 stream-json STDIO 重写

## 摘要

把桌面端 `ClaudeRunner`（`E:\xihe-desktop\src\main\claude.ts`）从「每轮 spawn 一个新 claude 子进程 + `--resume`」重写为「一个会话（convId）一个长驻子进程，stdin 跨轮喂 NDJSON」。每轮省 ~5s CLI 启动 + 复用进程内 prompt-cache。热路径（stdin 多轮）+ 冷路径（进程死后 `--resume` 续同会话）**两条都实测验证通过**。

## 变更内容

### 代码变更

- **`src/main/claude.ts` — 全量重写**：`Map<convId, ActiveTurn>` → `Map<convId, LongLivedSession>`。
  - `LongLivedSession`：child + kept-open stdin + 钉死 cwd + sessionId + lineBuf + ready/pendingSend/currentTurn + stopping。
  - `TurnScratch`：per-turn 草稿（toolBlocks/toolUseNames/streamedText/done），从旧 ActiveTurn 原样搬，轮次边界重置。
  - `send()`：有会话且 currentTurn 在飞 → 拒绝；无会话 → `ensureSession`（冷路径带 `--resume`）；未 ready → 缓冲 `pendingSend`；ready → `writeTurn`。
  - `ensureSession()`：spawn 矩阵同 ServeSupervisor；args 新增 `--input-format stream-json`、**不**传 `--no-session-persistence`（持久化是冷 resume 前提）、已知 sid 时加 `--resume <sid>`。
  - `writeTurn()`：往 stdin 写 `{"type":"user","message":{"role":"user","content":prompt}}\n`，**保持 stdin 开着**（旧代码 `stdin.end(prompt)` 裸文本+关流）。
  - `attachStdout()`/`onSessionExit()`：进程级 NDJSON 解析；意外退出→error 帧 + 删 map 项（下次懒重启），**不**自动重启。
  - `interrupt()`：无损——`killTree` + finalize `interrupted` + 删 map 项；下次 `send()` 带 `--resume` 续同会话。
  - `dispose()`（新）：拆空闲会话（补 `deleteConversation` 缺口）；有轮在飞则回退 interrupt。
  - `stop()`：幂等，镜像 ServeSupervisor。
  - NDJSON 三处理器（`onStreamEvent`/`onUserMessage`/`onResult`）逻辑**原样搬**进 TurnScratch，签名加 `s`。
- **`src/main/index.ts`**：加 `claude:dispose` IPC handler（`claude?.dispose(convId)`）。
- **`src/preload/index.ts`**：desktop 面加 `claudeDispose`。
- **`src/renderer/src/lib/desktop.ts`**：DesktopAPI 加 `claudeDispose` 类型。
- **`src/renderer/src/store.ts`**：`deleteConversation` 对 claude 无条件 `claudeDispose`（旧逻辑仅 pending 时 interrupt，空闲进程会泄漏）；xihe 路径保持原 pending 门 + `stream.interrupt`。

### 配置/数据变更

无。claude 自带会话持久化保持开启（会话文件落在钉死 cwd 下，相对路径）。

## 变更分析

### 变更原因

1. 每轮新 spawn 付 ~5s CLI 启动开销（实测 `system/init` 要到 spawn 后 +5035ms）。
2. 每轮重读完整 system prompt → 丢 prompt-cache 命中（实测 turn2 `cache_read_input_tokens: 25856` 命中是本重写的核心收益）。

### 变更影响

**正面**：
- 同会话多轮复用进程 + 进程内 cache，省启动 + 省 token。
- `--resume` 冷路径让跨 app 重启 / 中断后续接**无损**（验证通过）。

**潜在影响**：
- 每个 claude 会话占一个子进程直到会话删/app 退出（无空闲回收，多会话占内存）。
- 改 agent 配置不热生效（spawn 时烙入）；workaround = dispose 后下次 resume。

## 验证方案

### 实证探测（两条路径，零依赖脚本，已删）

**热路径**（同进程多轮 stdin 复用）：探测 turn1 "reply PONG" → result → turn2 "上一条让你回复什么" → 答对 + `cache_read 25856` 命中。同进程同 session_id。✅

**冷路径**（本变更唯一未验项，恢复 claude 后补验 2026-08-12）：
- 进程 A turn1 记暗号 `BLUEFIRE`（捕 sid `8f907cec`，答 `GOT IT.`，cache_read 320）。
- kill 进程 A。
- 进程 B 带 `--resume 8f907cec` turn2 问暗号 → 答 **`BLUEFIRE`**（同 sid，cache_read 256）。✅
- **结论**：`--resume <sid> --input-format stream-json` 组合工作正常，会话跨进程复活、模型记忆不丢。

### 类型/构建

```
node E:/xihe-desktop/node_modules/typescript/bin/tsc --noEmit -p E:/xihe-desktop/tsconfig.json   # exit 0
cd E:/xihe-desktop && npm run build                                                              # exit 0
```

### 待人工 happy-path（app 内）

代码 + 协议双路径已验，但 app 内端到端（流式渲染、interrupt 按钮、删空闲会话、跨重启续接）建议用户 `npm run dev` 走一遍（计划验证清单第 3 项）。

## 风险评估

**风险等级**：高（重写核心传输层）→ 实际**已通过双路径实证**，残余风险降到中。

**残余风险点**：
1. 协议级 interrupt / permission_response 字段未实测（v1 用 killTree 兜底 + 冷 resume，bypassPermissions 下用不到权限）。
2. `--resume` 依赖 claude 落盘会话文件 + 同 cwd；钉死 cwd 目录被删 → resume 失败 → error 帧。
3. 无空闲回收定时器（多 claude 会话 = 多子进程）。

## 已知限制

1. 无协议级 interrupt（killTree + 冷 resume，热 cache 在 interrupt 时丢、模型记忆靠 resume 活）。
2. 改 agent 配置不热生效（dispose 后下次 resume 生效）。
3. cwd 会话生命周期内钉死，且对 `--resume` 承重。
4. 无空闲回收。
5. stdin 写与死亡的竞态（EPIPE → error 帧，exit handler 也触发删项，下次懒重启）。
6. resume 依赖 claude 持久化会话文件；损坏/禁用 → error 帧（v1 不自动回退新会话）。

## 相关页面

- [[0028_desktop-claude-transport-architecture]] — 本架构的**稳定参考页**（数据流、生命周期、IPC、协议映射、冷热路径）。
- [[0026_desktop-agent-model-built-in-xihe]] — 桌面 Agent 模型定调（claude = 可添加 agent 类型）；本变更是其 F2 的性能/复用升级。
- 计划全文：`~\.claude\plans\breezy-brewing-thompson.md`（已批准）。
- 候选源：`meta/candidates/desktop-claude-longlived-rewrite.md`（已 promoted）。
