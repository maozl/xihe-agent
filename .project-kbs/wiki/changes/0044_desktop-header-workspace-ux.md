---
type: change
title: 桌面端头部交互重构——按钮与工作空间关联规则、生效布局推导、状态合并与品牌打磨
slug: 0044_desktop-header-workspace-ux
change_type: improvement
risk_level: low
status: completed
created: 2026-09-02
updated: 2026-09-02
affected_modules:
  - desktop/src/renderer/src/App.tsx
  - desktop/src/renderer/src/components/Sidebar.tsx
  - desktop/src/renderer/src/store.ts
---

# 桌面端头部交互重构——按钮与工作空间关联规则、生效布局推导、状态合并与品牌打磨

## 摘要

桌面端头部右侧按钮组定稿为一排视图开关（布局/文件树/浏览器/运行/终端/商店/知识库/设置），并确立**按钮与工作空间绑定的关联规则**；删除冗余的工作空间 chip 下拉；派生 `inWorkbench` 修掉"无绑定困在工作台"的死角；合并树开关的双重重叠状态。同批完成品牌资产接入（banner→logo 图标管线）。

## 变更内容

### 按钮与工作空间绑定的关联规则（后续加面板按钮的规范）

判断依据一句话：**操作对象是"这个目录里的东西"的按钮跟绑定走，全局能力不关联**。

| 按钮 | 无绑定工作空间时 | 依据 |
|---|---|---|
| 工作台布局 | **隐藏** | 工作台的树/编辑器以工作空间为对象 |
| 文件树 | 置灰（title 提示先绑定） | 树内容 = 工作空间目录 |
| 运行 | 置灰，但面板开着时可点（保证能关） | 执行 cwd/历史/运行槽全按 workdir 分桶 |
| 浏览器 | 不关联 | 吸附 agent 的 Chrome（serve CDP），跟会话走 |
| 终端 | 不关联 | 本地 shell/agent 命令/SSH，进程级共享 |
| 商店/知识库/设置 | 不关联 | 应用级页面 |

### 工作空间 chip 删除

头部带下拉列表的工作空间 chip（绑定列表 + 通用 + 工作台布局项）整体移除——绑定/切换统一走侧栏（rail 工作空间入口、`openWorkspace`/`openConversationInWorkspace`/`exitWorkspace` 已完整覆盖），树面板头部本就展示工作空间名。store 的 `bindWorkspaceToConv` 随之失去调用方删除。

### 生效布局推导 `inWorkbench`（防死角不变式）

`inWorkbench = !!activeWs && layoutMode === 'workbench'`——渲染分支按**生效布局**而非偏好：未绑定的会话即使偏好是 workbench 也按 chat 渲染。若只藏按钮不改渲染，用户会在工作台里切到通用会话：按钮没了、左侧只剩绑定引导占位，**退不出工作台**。派生而非切换的好处：切回绑定会话时工作台自动恢复，不动持久化偏好。

### 树开关状态合并

原 `showTree`（header 按钮操作）与 `rightTreeCollapsed`（面板内折叠钮 + EdgeStrip 恢复条操作）是**同一面板的两个独立开关**，会出现状态错位（EdgeStrip 恢复了、header 按钮还显示相反状态）。合并为单一 `rightTreeCollapsed`，三个入口（header 按钮/面板折叠/边缘恢复条）操作同一状态；按钮改为模式感知——chat 切右侧树、workbench 切左侧树（修掉了"workbench 模式下按钮无效"的问题）。

### 品牌资产接入

- `docs/banner.jpg` 清 AI 水印后作为品牌图；**再生成管线** `docs/_make_icons.py`：中心裁 560×560 → `desktop/src/renderer/src/assets/logo.png`（128px 渲染层）+ `desktop/resources/icon.png`（256px 窗口图标），调整 banner 后重跑一次即可。
- 展示点：侧栏品牌位（原蓝底 "x" 字母块）、聊天空态两处 + 首启欢迎卡、`BrowserWindow` icon（`existsSync` 兜底，缺图标不影响开窗；dev 下 `app.getAppPath()` = desktop/ 根）。
- 头部文案精简（同批）：去 agent 名前缀（模型名移到 composer 下方）、去 `control plane · v0.0.1` 副标题、`xihe desktop` → `xihe`。

## 踩坑

- **Vite Fast Refresh 停在旧模块**：连续多次编辑 App.tsx（中间含一次短暂类型错误）后渲染进程可能停在旧代码，表现为"按钮消失"等与代码不符的现象——Ctrl+R 或重启 dev 恢复，排查 UI"回归"先刷新再查代码。

## 验证

tsc + electron-vite build 绿；按钮三态（绑定/未绑定/面板开合）人工核对。

## 相关页面

- [[0043_desktop-workbench-phase1]] - 工作台布局与面板的来源
- [[0031_workspace-cwd-binding]] - 工作空间绑定模型（convWorkspace map）
