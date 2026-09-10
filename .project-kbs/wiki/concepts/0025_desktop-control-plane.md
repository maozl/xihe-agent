---
type: concept
title: 桌面端控制面设计（xihe desktop）
slug: 0025_desktop-control-plane
aliases:
  - xihe desktop
  - 控制面
  - control plane
  - 桌面端设计
  - 三层模型
tags:
  - architecture
  - desktop
  - electron
  - control-plane
status: active
created: 2026-08-10
updated: 2026-08-11
related_pages:
  - wiki/concepts/0024_desktop-serve-protocol.md
  - wiki/insights/0026_desktop-agent-model-built-in-xihe.md
  - wiki/concepts/0011_gateway-architecture.md
  - wiki/concepts/0006_session-design.md
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/entities/0001_xihe-agent.md
---

# 桌面端控制面设计（xihe desktop）

> **跨仓说明**：xihe desktop 是独立仓库（`E:\xihe-desktop`），但它是 xihe 的桌面前端 / 控制面，与 [[0024_desktop-serve-protocol]] 的协议是一对共生体，故收录在本 wiki。

> ⚠️ **Agent 层建模已被订正（2026-08-11）**：本文「三层模型」里的 **Agent 层**（定义为「一个 provider 下的 persona / 实例」）、**3 个种子 agent**（`xihe-ops`/`xihe-research`/`claude-dev`）以及「**serve 由用户显式启动**」的设定，已被 [[0026_desktop-agent-model-built-in-xihe]] **推翻**。新定调：Agent = 几种**类型**（xihe = 桌面**内置**、main 进程托管 `xihe serve` 生命周期；claude = **可添加**的 connector 占位），**非多实例 / 非 persona**。本页其余部分（控制面定位、能力驱动 UI、electron-vite 三段式、store 机制、组件表、连接生命周期、协议引用）仍然有效。下文涉及 Agent 层处均就地标注「⚠️ 已被 [[0026]] 订正」。

## 摘要

**xihe desktop** 是 xihe 的桌面**控制面（control plane）**：一个 Electron app，本身**不内嵌任何引擎**，而是通过 [[0024_desktop-serve-protocol]] 的 HTTP+WS 协议驱动 `xihe serve`（以及未来的 Claude / CodeBuddy 等 provider）。设计核心是**三层模型**（Provider → Agent → Session）+ **能力驱动 UI**（按 capability flag 分支，永不 sniff 引擎名）。当前 **v0.0.1 骨架**：UI 全部搭好，P0（serve 接入）已落地——`xihe-ops` 这一槽位在 serve 可达时升级为真实流式对话；persona 层 / Claude connector / 管理 UI 仍是 demo 占位（见 Roadmap）。

技术栈：**electron-vite + Electron 29 + React 18 + Tailwind 3 + Zustand 4**（图标 lucide-react，`cn = twMerge(clsx)`）。气网隔离：npm 走内部 registry `<内部npm镜像>:8001`，Electron 二进制走 `electron_mirror`。

## 定位：控制面，不是引擎

桌面是**薄控制面**，引擎（xihe / Claude / CodeBuddy）**留在进程外**。桌面只做三件事：**选择**（在 provider/agent 间切换）、**驱动**（把用户输入经协议喂给引擎、渲染回流）、**留存引用**（对话真理按 provider 形态分别归属——见下）。这样一套 UI 能同时容纳自托管 xihe 和云上 Claude，无需为每种引擎写一套前端。

- **不内嵌引擎**：renderer 不 import 任何 Node / 引擎 API；所有交互经 `xihe serve`（[[0024_desktop-serve-protocol]]）。
- **对话真理归属分形态**：process 型 xihe 的真理在 serve 的 `sessions.db`；connector 型 Claude 的真理在 provider 侧，桌面只存连接引用 + 注释（不复制）。
- **v0.0.1 = 骨架**：README 自称「v0.0.1 骨架」；数据全 mock，仅 `xihe-ops` 槽位接 serve 后转 live。

## 进程结构（electron-vite 三段式）

electron-vite 把构建拆成三段，职责清晰隔离：

| 段 | 文件 | 职责 |
|----|------|------|
| **main** | `src/main/index.ts` | 单个 `BrowserWindow`，生命周期，**无任何 `ipcMain` handler** |
| **preload** | `src/preload/index.ts` | `contextBridge` 暴露 `window.desktop`（**当前是 stub**） |
| **renderer** | `src/renderer/src/*` | 自包含 React app，不碰 Node API |

**main**（`createWindow`）：窗口 `1280×820`（min `960×600`），`show:false`+`ready-to-show` 显窗，`autoHideMenuBar`，`backgroundColor:#0a0a0a`，`title:'xihe desktop'`。`webPreferences`：`preload: ../preload/index.js`、**`contextIsolation:true`**、`sandbox:false`、`nodeIntegration` 未设（默认 false）。外链 `setWindowOpenHandler` 强制 `shell.openExternal` + deny。加载目标：dev 走 `process.env.ELECTRON_RENDERER_URL`（electron-vite 默认 renderer 端口 **5173**），prod 走 `loadFile`。**安全姿态正确**（contextIsolation 开、nodeIntegration 关），但代价是 main 几乎不做事——所有逻辑在 renderer。

**preload**（stub）：`contextBridge.exposeInMainWorld('desktop', { version:'0.0.1', mode:'demo', ping() })`，`ping` 调 `ipcRenderer.invoke('desktop:ping')`——但 main **没有** `ipcMain.handle('desktop:ping')`，调用会挂起。头注释明说：renderer 现在自包含（mock），接 serve 在 P0，这里「先把契约摆出来，留最小可见」。**renderer 当前完全不消费 `window.desktop`**。这是有意为之的占位，不是 bug。

## 三层模型（设计哲学）

桌面按三层建模，这是整套设计的主梁：

```
Provider  ──(引擎 / 连接)──  Agent  ──(人格 / 实例)──  Session  ──(对话线程)
```

- **Provider**：一个引擎或一条连接。两种**形态（shape）**：
  - **process**：你拥有一个 xihe 实例（`xihe --config X` 起的进程），自带独立数据根（`dataRoot`），桌面 spawn/监督它。真理归你。
  - **connector**：连到一个托管 provider（Claude / OpenAI 兼容），凭据走注册表，一个 connector 服务多账号、按会话绑凭据。真理在 provider 侧，桌面只存引用 + 注释。
- **Agent**：一个 provider 下的**人格 / 实例**（persona）。同一个 process 型 xihe 可挂多个 persona（共享内核，独立 system_prompt + memory）——这是 P1 的核心，让多 agent 不必各起一个进程。
  - ⚠️ **已被 [[0026_desktop-agent-model-built-in-xihe]] 订正**：Agent 层**不再是 persona / 实例**，而是**类型**——xihe（桌面内置，main 托管 `xihe serve`）+ claude（可添加 connector）。persona 路线已废弃（[[0017]]/[[0018]]）。Provider→Agent→Session 作为会话归属的分层名仍可保留，但 Agent 层语义以 [[0026]] 为准。
- **Session**：一个对话线程。serve 侧一个 `conv_id` → 一个持久 xihe session（映射见 [[0024_desktop-serve-protocol]] 会话映射节、[[0006_session-design]]）。

种子数据（`SEED_AGENTS`）就是这三层的演示：`xihe-ops`（process，唯一 live 槽）、`xihe-research`（process，persona 占位 → P1）、`claude-dev`（connector，占位 → P2）。

> ⚠️ **已被 [[0026_desktop-agent-model-built-in-xihe]] 订正**：F1 将把 `SEED_AGENTS` 收敛为**单一内置 xihe**（live）+ **claude 可添加占位**（IA 槽，disabled），删除 `xihe-research` persona 假槽。下文 Roadmap 的 P1（persona 层）相应废弃。

## 能力驱动 UI（capability-driven）

`Agent.capabilities: string[]`。**UI 按这些 flag 分支，永不按引擎名分支。** 这是「一套 UI 容纳异质引擎」的前提——新引擎只要声明能力即可，前端零改动。

- 能力描述符的**来源**在 serve 侧实时推导自工具注册表（`browser`/`vision`/`mcp` 等反映真正过 `check_fn` 门控的工具，见 [[0002_tool-registry-and-dispatch]]），经 `/health`、`/agents`、WS `hello` 三处下发（见 [[0024_desktop-serve-protocol]] 能力描述符节）。桌面连上即拿到，覆写种子的 `capabilities`。
- **`EngineBadge` 是唯一例外**：它按引擎名配色（`xihe→brand`、`claude→orange`、`codebuddy→emerald`）。但这是**徽标配色**，不是功能分支——能力判断仍走 `capabilities`。
- `CapChip`：capability → 图标映射（`shell→Terminal`、`browser/images→MonitorUp`、`mcp→Boxes`、`interrupt/escalation→Shield`、`fork→GitFork`，默认 `Cpu`），渲染 `ManagePanel` 的人格能力清单。

## Store（Zustand）

单 store（`src/renderer/src/store.ts`），闭包内持 WS 句柄与重连状态：

- 类型：`EngineKind = 'xihe'|'claude'|'codebuddy'`、`AgentShape = 'process'|'connector'`、`AgentStatus = 'online'|'offline'|'demo'`。`Agent` 字段含 `capabilities`、`dataRoot?`、`serveBacked?`、`serveConvId?`。
- **`LIVE_SLOT_ID = 'xihe-ops'`**：唯一在 serve 可达时升级的种子。升级时 `serveConvId = desktop-${remote.id ?? 'self'}`、`status:'online'`、`capabilities` 用 serve 下发的覆写、`dataRoot` 取 serve 的 `AGENT_HOME`。
- **闭包状态**：`stream`（WS 句柄）、`reconnectTimer`（3s 重连）、`deliberateClose`（优雅关闭旗标——**目前从不置 true**，重连逻辑没有 graceful-shutdown 路径）。
- **`handleEvent`**：只渲染 `text_delta`（追加到 pending 气泡）/ `complete`（定稿）/ `error`（⚠️ 标红）。明注释：`// hello / turn_start / thought_delta / tool_call / tool_result: not rendered in P0`（`store.ts:135`）——思考流、工具调用、回合生命周期的事件暂不渲染。
- **`select(id)`**：serve-backed agent 首次打开时**懒加载**持久历史（`getHistory(serveConvId)` → 过滤 user/assistant → 填 session）。
- **`sendMessage`**：`serveBacked && serveConvId && serveConnected && stream` 全满足 → `stream.sendTurn`；否则 `mockStream`（4 字符/16ms 打字机跑 `demoReply`）。
- **`patchPending`**：按 `conv_id` 反查 agent，找到该会话最后一条 `pending` 的 assistant 消息就地更新。

## 组件

| 组件 | 读 | 渲染 |
|------|----|------|
| `Sidebar.tsx` | agents / selectedId / serveConnected / serveVersion | 品牌标（`xihe desktop` / `control plane · v0.0.1`）、agent 列表、**禁用**的「添加 Agent」、底部连接状态（绿 `已连接 xihe serve · v{version}` / 琥珀 `演示模式 · 未连接 serve`） |
| `ChatPanel.tsx` | `sessions[agent.id]` / `sendMessage` | 消息流（user `bg-brand` / assistant `bg-neutral-800`）；pending 空 →「正在思考…」、非空 → 闪烁光标；`textarea`+`Send`，Enter 提交 / Shift+Enter 换行。**无附件/图片/中断按钮**（`interrupt` 仅在 `ServeStream` 上，未接线 UI） |
| `ManagePanel.tsx` | 仅 `agent` prop（纯展示，无 `useStore`） | 5 节：Persona（引擎/形态/模型/`dataRoot` + `CapChip` 能力清单 + 描述伪装成 `system_prompt`）、MCP「未配置」、Skills「无 skill」、调度「无定时任务」、凭据「凭据入加密配置」——**后 4 节是空占位**，标题即未来职责划分 |
| `common.tsx` | — | `EngineBadge`（引擎配色，见上）、`StatusDot`（`online→emerald`/`demo→amber`/`offline→neutral`）、`CapChip` |

## 连接生命周期

`App.tsx` 挂载即 `connectServe()`（dev 下 React StrictMode 会 double-invoke → 两次 `/health`+`/agents`+`/stream`，无害；生产无）：

1. `getHealth()` —— 不通 → `serveConnected:false`，停在 demo 态。
2. `getAgents()` —— 拿 serve 自描述 agent。
3. 把 `LIVE_SLOT` 升级为 live（见 Store 节）。
4. `openStream()` 连 `/stream`：`onopen` resolve、`onclose`/`onerror` 触发 `onStatus(false)` → `scheduleReconnect`（3s 重试）。
5. `select(LIVE_SLOT_ID)` 触发历史懒加载。

底栏状态指示 + 重连让「serve 没起 → demo，起了 → 自动 live」成为零配置体验。

## 落地文件（`E:\xihe-desktop`）

- 入口/外壳：`src/main/index.ts`、`src/preload/index.ts`、`src/renderer/src/{main.tsx,App.tsx,index.css}`
- 状态：`src/renderer/src/store.ts`
- 协议客户端：`src/renderer/src/lib/serveClient.ts`、`src/renderer/src/lib/cn.ts`
- 组件：`src/renderer/src/components/{Sidebar,ChatPanel,ManagePanel,common}.tsx`
- 配置：`electron.vite.config.ts`、`tailwind.config.cjs`（brand 色 `#6d8cff`，`brand.soft #3a4a7a` **声明未用**）、`postcss.config.cjs`、`tsconfig.json`、`.npmrc`（内部 registry + electron mirror）

## Roadmap（README §路线 + 代码标记）

| 阶段 | 内容 | 现状 |
|------|------|------|
| **P0** | serve 接入（`xihe-ops` 槽真实流式对话） | ✅ **已落地** |
| **F1** | main 托管 `xihe serve` 生命周期（spawn/health/restart/cleanup）+ 花名册收敛为内置 xihe + 去 "serve" 措辞 | 下一步（[[0026]] 定调） |
| ~~**P1**~~ | ~~persona 层：一个 xihe 进程挂多 persona（`xihe-research` 占位）~~ | ❌ **废弃**（persona 在 xihe-agent 侧已回退 [[0017]]/[[0018]]；[[0026]] 不再留假槽） |
| **P2** | claude = **可添加 agent 类型**（connector + 凭据金库）；UI 占位（`claude-dev` → disabled IA 槽） | demo 占位（[[0026]]：现在只锁语义，不实现 connector） |
| **P3** | 调度 / skill / MCP 管理 UI（`ManagePanel`） | MCP/skills/cron 已**只读接入**（serve `/mcp`/`/skills`/`/cron` + 桌面渲染）；凭据节静态；写入路径未做 |
| **P4** | CodeBuddy / 远程 provider | 仅 `EngineKind` 枚举值 |

## 设计权衡与未完成项

> ⚠️ 本节若干条目（「全 demo 数据」「`ManagePanel` 非功能」「『添加 Agent』禁用」）是 v0.0.1 骨架的历史快照，部分已被后续工作或 [[0026_desktop-agent-model-built-in-xihe]] 改变：ManagePanel 的 MCP/skills/cron 已只读接入（见 Roadmap P3）；花名册重构与「添加 claude」流归 F1（[[0026]]）。读下面条目时请对照上文 Roadmap 的最新现状。

- **全 demo 数据**：种子 3 agent 全 `status:'demo'`；`xihe-research`/`claude-dev` 即便 serve 起了也不接线，仅 `xihe-ops` 升级——这让 UI 能在任何后端存在前就完整演示。（⚠️ F1 将收敛花名册，见 [[0026]]。）
- **preload IPC 是死通道**：`desktop.ping` 调无 handler 的 `desktop:ping`，`window.desktop` 整个未被消费。是有意占位（契约先行），但**接 serve 后该清理或落实**。
- **`ManagePanel` 非功能**：除 Persona 只读元数据外全是空占位。
- **`brand.soft` 死 token**：Tailwind 声明了没用。
- **「添加 Agent」禁用**：无创建流。
- **无中断 UI**：`ServeStream.interrupt` 已实现但没按钮。
- **无打包**：README 明说 `electron-builder` 配置待加；scripts 只有 dev/build/preview。
- **事件渲染部分**：P0 只渲染 text/complete/error；thought_delta / tool_call / tool_result 待 P1+（store.ts:135 注释）。
- **`deliberateClose` 从不置 true**：重连逻辑缺优雅关闭路径——目前靠 socket 掉线触发，应用退出时可能留一次无谓重连。
- **StrictMode 双触发**（dev）：两次握手无害，但 P1 接多人格/多会话时需清理（避免重复 WS）。

## 相关页面

- [[0026_desktop-agent-model-built-in-xihe]] —— **Agent 层建模订正**（推翻本文 Agent 层 / 种子 agent / serve 显式暴露设定）：内置 xihe + 可添加 claude，非多实例 / 非 persona
- [[0024_desktop-serve-protocol]] —— 桌面↔agent 的通信协议（serve 模式 + REST/WS 契约 + Emitter 桥 + 中断），本文档的共生页
- [[0011_gateway-architecture]] —— serve 暴露的 SharedContext + 每消息薄 agent 内核
- [[0006_session-design]] —— 桌面 `conv_id` → session_key 的映射基础
- [[0002_tool-registry-and-dispatch]] —— 能力描述符的推导来源（registry + check_fn 门控）
- [[0001_xihe-agent]] —— xihe 项目总览，桌面是其前端形态之一
