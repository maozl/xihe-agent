---
type: change
title: 桌面端开发工作台 Phase 1——Monaco 编辑器、双模式布局、diff 卡、git 装饰、运行面板与终端
slug: 0043_desktop-workbench-phase1
change_type: feature
risk_level: medium
status: completed
created: 2026-09-02
updated: 2026-09-02
affected_modules:
  - desktop/src/renderer/src/components/monaco/
  - desktop/src/renderer/src/components/EditorArea.tsx
  - desktop/src/renderer/src/components/FileTreePanel.tsx
  - desktop/src/renderer/src/components/DiffBlock.tsx
  - desktop/src/renderer/src/components/RunPanel.tsx
  - desktop/src/renderer/src/components/TerminalPanel.tsx
  - desktop/src/main/git.ts
  - desktop/src/main/run.ts
  - desktop/src/main/localPty.ts
  - src/tools/file_tools.py
  - src/core/registry.py
  - src/gateway/serve/emitter.py
  - src/tools/_local_tap.py
related_insights:
  - wiki/concepts/0024_desktop-serve-protocol.md
---

# 桌面端开发工作台 Phase 1——Monaco 编辑器、双模式布局、diff 卡、git 装饰、运行面板与终端

## 摘要

面向 IT 开发者（IDEA/VS 迁移用户）把桌面端从"会话为中心"补全为可日常居住的工作台：Monaco 编辑器 + tab、chat/workbench 双模式布局、agent 改动的 diff 可视化（trace diff 卡 + 审批卡预览）、文件树 git 装饰、一次性运行面板、共享终端（本地 shell / agent 本地命令 / SSH）。桌面端纯增量 + serve 三处加字段（write_file diff、审批 args、终端 tap），老新版本互通只少显示不破坏协议。

## 变更内容

### Monaco 基建（气隙约束下的引入方式）

- `monaco-editor@^0.56` 从内部 npm mirror 装；**不用** `@monaco-editor/react`（其默认 CDN loader 在气隙不可用），直接 ESM + Vite `?worker`（editor/ts/json worker；CSS/HTML worker 不接——Monarch 高亮不依赖 worker）。
- `components/monaco/`：`env.ts`（`ensureMonaco()` 懒加载 + `getOrCreateModel` 按 Uri 复用 model 保 undo 历史 + defineTheme `xihe-dark`/`xihe-light` 硬编码镜像 index.css 色值——Monaco 不吃 CSS 变量）、`MonacoPane.tsx`（薄封装：挂载/dispose/ResizeObserver 精确 layout，**不用 automaticLayout 轮询**；Ctrl+S 以 monaco command 绑定避免焦点在编辑器外误触）、`MonacoDiffPane.tsx`（只读 DiffEditor）。
- 所有 monaco 模块 **React.lazy 动态导入**（mermaid 先例），聊天首屏不背数 MB chunk；dev 模式启动 1.5s 后预热（首次开文件不付数百模块流式加载的成本），prod 保留懒加载。
- `lib/lang.ts` `detectLanguage(filename)`：扩展名→language id 小映射。

### 双模式布局（chat ≈ 现状 / workbench = 编辑器中心）

- `~/.xihe-desktop/settings.json` 加 `layoutMode: 'chat' | 'workbench'`（`app:setLayoutMode` IPC 只持久化，纯 renderer 关切）。
- chat：原布局 + 右侧 `FileTreePanel`（树 + 底部单文件抽屉查看器，textarea 换 MonacoPane）。
- workbench：左 `FileTree`（240 默认宽）→ 中 `EditorArea`（flex-1）→ 右 `ChatPanel`（460 默认宽、左缘可拖）→ `BrowserPanel` 最右；终端/运行抽屉两模式都全宽。
- `FileTreePanel.tsx` 拆出 `FileTree`（导航 + 内联 CRUD + git 装饰位，`side` 左/右决定默认宽与折叠箭头方向），chat 面板与 workbench 左栏共用。

### 编辑器 tab 与外部变更协调

- store 新 slice：`EditorTab { kind: 'file'|'diff'; path?; diff? }` + `editorTabs`/`activeTabId` + open/close/rename（重命名/删除联动 tab）。
- `EditorArea`：tab 栏（dirty 点/关闭）+ 每 path 一个 Monaco model（tab 切换 undo 不丢）；Ctrl+S 走写沙箱；1 MiB 截断只读横幅、binary/error 态。
- **fsVersion 计数器**：agent 的 write_file/patch tool_result、桌面侧 CRUD、编辑器保存后 bump；EditorArea 订阅——非 dirty tab 直接重读，dirty tab 出"文件已在磁盘修改"横幅（用磁盘内容/保留我的，**不覆盖未保存草稿**）；FileTree 500ms 防抖刷树 + git。

### diff 可视化（trace diff 卡 + 审批卡预览）

- **serve 加字段**：`_write_file` 覆盖已有文件时写前读旧内容出 unified diff（截 5000 字符，与 `_patch` 既有形态对齐）+ `created` 标记；result JSON 带 diff 后 live 流与历史 trace 通吃，零协议改动（新测试 `tests/test_file_tools_write.py`）。
- `DiffBlock.tsx`：折叠态路径 + `+N −M` 统计，展开态 highlight.js `diff` 语法高亮（绿增红删，零 Monaco 依赖）；`TurnTrace` 的 write_file/patch done 结果含 diff 时渲染。
- 卡片动作：「打开文件」（用 result JSON 的 `path`——**解析后的绝对路径**，args 里的 path 是 rewrite 前原样可能相对）、「打开对比」（patch 且历史 trace args 全量时开 Monaco DiffEditor tab）。
- **审批卡**：dispatch 审批门把原始 args（64K 截断）传入 request_approval → emitter 透传 → `PendingApproval.args` → 「查看变更」——write_file = 磁盘当前内容（审批挂起=写未落盘，读到的就是旧侧）vs args.content；patch = args.old/new hunk 级；>256KB 回落文本统计。

### 文件树 git 装饰

- `main/git.ts` `gitStatus(workdir)`：向上找 `.git` → `git status --porcelain=v1 -z --untracked-files=all -b`（`--no-optional-locks` 不碰用户 index.lock；10s 超时；5000 条 cap；**事件驱动不轮询**）。`-z` 记录解析含 rename 双段，`-b` 行取分支名。
- 树条目装饰：M/R/C→warning、A/U→success、D→danger（复用语义 token 不加 CSS 变量）+ 父目录沿路径聚合淡色角标；`not-repo`/`no-git` 静默。

### IDEA 紧凑树（单链目录合并）

- 扁平模式重做为 IDEA「Compact Empty Middle Packages」语义：单链目录合并为一节点（`src.main.java`）。`chainOf` 沿单目录链下探，遇未加载目录 `ensureChildren` 触发加载下一轮继续合并（`pendingLoads` 集合去重并发请求）；**50 层迭代上限防 symlink 环**。链尾节点承 rename、链首承 delete。偏好存 localStorage `fileTreeMode`（旧值 `'flat'` 视同 compact）。

### 运行面板 + 共享终端

- `main/run.ts`：一次性命令执行，**每 workdir 一槽**（新命令 supersede 旧的、树杀）、历史按 workdir 持久（`~/.xihe-desktop/`）；`run:event` 推 start/out/exit。`RunPanel`：↑ 召回历史 + 下拉重填、文件树/编辑器 Play 入口经 `runCmdDraft` 预填、`guessRunCommand` 按扩展名猜启动命令（python/node/npx tsx）。
- `main/localPty.ts`：本地 pty attach/write/resize/kill + 事件推送；**终端面板三源合一**（本地 shell / agent 的本地命令 / SSH 会话），agent 与桌面手敲共写同一通道（pump + 环形缓冲；`prompt` 字段修状态链盲点）。serve 侧 `_local_tap.py`（与 `_ssh_tap` 同构的本地命令 tap）+ terminal.py/emitter.py 通道。

## 已知边界

- 终端会话是 serve 进程级共享（跨对话），切工作空间不随之切换（conv tab 跟随、proc/ssh/local 钉住）。
- live 流 `tool_call.args` 是 120 字符截断切片，全量 args 只在历史 trace 接口——「打开对比」仅历史可用。
- 编辑器 tab 不持久化（会话内状态）；无 LSP/调试器/全局搜索（Phase 2 候选）。

## 验证

- serve 侧：`test_file_tools_write.py`（已有文件出 diff/新文件 created/超大跳过）+ process/terminal/ssh_tap/local_live 相关测试更新，pytest 全绿。
- 桌面：tsc + electron-vite build 绿，monaco 独立 lazy chunk + worker 产物确认。
- 手动 e2e：模式切换持久化、编辑保存、diff 卡、审批预览、git 装饰、agent 写后树自动刷新。

## 相关页面

- [[0024_desktop-serve-protocol]] - 桌面↔serve 协议（本变更为其加字段而非改协议）
- [[0029_desktop-dual-engine-architecture]] - 桌面整体架构
- [[0045_desktop-git-gutter-marks]] - 编辑器 git 行标记（工作台编辑器的后续增强）
- [[0046_shared-session-terminal]] - 终端面板的专门架构页（本节的小节展开）
