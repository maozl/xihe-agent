---
type: concept
title: 工作空间 cwd 绑定与入口差异（桌面 vs CLI）
slug: 0031_workspace-cwd-binding
aliases:
  - 工作空间绑定
  - workspace cwd
  - 入口差异
tags:
  - desktop
  - workspace
  - serve
  - cli
status: active
created: 2026-08-16
updated: 2026-08-16
related_pages:
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/concepts/0025_desktop-control-plane.md
  - wiki/changes/0021_cli-hybrid-tui.md
  - wiki/concepts/0029_desktop-dual-engine-architecture.md
---

# 工作空间 cwd 绑定与入口差异（桌面 vs CLI）

## 摘要

**一句话：工作空间的 cwd 绑定在「会话」上（桌面私有 map）、随每轮消息传给 serve、不落库；入口（外层对话列表 vs 工作空间内）不影响它；CLI 则完全独立——既看不到这些会话，也没有这套绑定。**

| 入口 | 看得到工作空间会话？ | agent 的 cwd |
|------|---------------------|--------------|
| 桌面 · 工作空间内 | 是 | 该空间 workdir（每轮传） |
| 桌面 · 外层对话列表 | 是（同一个 convWorkspace map） | 同上，**完全一致** |
| CLI（`xihe chat`） | **否**（platform="cli" 过滤，serve 会话不可见） | 进程启动目录（[[0021]] cwd 注入） |

## 绑定机制（三层链路）

绑定本体：`~/.xihe-desktop/workspaces.json` 的 `convWorkspace: {convId → workspaceId}`（renderer `lib/persist.ts` 校验，main 侧 IPC `workspace:load/save` 原子写）。

1. **UI 层**：`App.tsx:55-61` 按 activeConvId 查 map → activeWs → 自动 `setShowTree(true)`（文件树随会话出现）。绑定是派生值、刻意不存 ConvMeta（注释：survives syncConversations rebuilds——绑定只活在 map 里）。
2. **发送层**：`store.ts:471-479` —— 每次 `sendTurn` 都重新解析 `convWorkspace[convId] → workdir`，`stream.sendTurn(convId, text, workdir)`（`serveClient.ts:219`）。**每轮一传，中途换绑定即时生效**，不是建会话时一次性带。
3. **serve 层**：`serve.py:538-542` —— 从 turn 取 `cmd.cwd`，`is_dir()` 校验（不存在则丢弃 + warning），`create_agent(cwd=cwd)`。工具侧（terminal 的 `resolve_path`/`agent_base_dir`，`tools/_paths.py`）相对路径与子进程 cwd 落到该目录；未绑定会话 `cwd=None` → 回退 serve 进程 cwd。

## 为什么入口无关

绑定查的是 **convId**，不是导航路径。外层列表和工作空间视图只是同一会话集合的两个过滤器（`Sidebar.tsx:46` 空间视图就是用同一个 convWorkspace 过滤），底层会话对象同一个；App.tsx 的派生与 store 的 send 解析都只看 convId。**从外层点进工作空间下建的对话 = 从空间内进入，行为完全一致。**

## 失效边界（cwd 丢失的三种情况）

1. **删工作空间**：`store.ts:720-724` 清掉该空间所有绑定 → 之后这些对话不再带 cwd。
2. **workdir 在磁盘不存在/被移走**：`serve.py:539-541` `is_dir()` 不过 → 丢弃 cwd + warning → agent 回退 serve 进程 cwd。
3. **外层新建的对话**（未经工作空间）：本来就没绑定。

## CLI：完全独立的另一套

- **会话不可见**：`chat.py:305/335` 会话列表（`-r` 选号、`/sessions`）只列 `platform="cli"`；serve 会话 key 是 `agent:main:serve:dm:{conv_id}`（[[0024]]），另一个命名空间。
- **map 私有**：CLI 不读 `~/.xihe-desktop/workspaces.json`，也不认识 conv_id 这套 ID。
- **cwd 不落库**：sessions.db 存消息/标题/缓存提示词，**无 cwd 字段**——cwd 只活在轮次协议字段和当轮 agent 实例里，serve 侧不持久化。
- **CLI 原生语义**：`xihe chat` 从哪个目录启动，agent 基目录就是哪（[[0021]]）。想在某个项目里干活 → cd 过去再启动。

## 历史消息 ≠ 运行时 cwd

消息内容（工具调用记录、改过哪些文件）持久在库里、随会话走；工作目录只随轮次/进程走。即使未来跨入口恢复会话，模型「记得干过什么」也不会自动恢复「当时在哪个目录」——两者生命周期不同。

## 打通方向（未实现，仅备忘）

若要 CLI/其他前端也能恢复工作空间：把 cwd 持久化到 session meta（serve 每轮写、resume 时读），而不是只靠桌面端 map。现状设计 = 桌面会话的 workspace 是桌面私有上下文。

## 相关页面

- [[0024_desktop-serve-protocol]] —— cwd 是 serve 协议的轮次字段；conv_id → session key 映射。
- [[0026_desktop-agent-model-built-in-xihe]] —— Workspace = 项目文件夹/用户资产，与 agent 正交的一等公民。
- [[0025_desktop-control-plane]] —— 桌面控制面（workspace 概念的来处）。
- [[0021_cli-hybrid-tui]] —— CLI 侧的 cwd 注入语义（进程启动目录）。
- [[0029_desktop-dual-engine-architecture]] —— 桌面整体架构 hub。
