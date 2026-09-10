---
type: change
title: 编辑器 IDEA 式 git 行标记——HEAD 基线、行级 diff 与 Monaco gutter 装饰
slug: 0045_desktop-git-gutter-marks
change_type: feature
risk_level: low
status: completed
created: 2026-09-02
updated: 2026-09-02
affected_modules:
  - desktop/src/main/git.ts
  - desktop/src/renderer/src/lib/lineDiff.ts
  - desktop/src/renderer/src/components/monaco/MonacoPane.tsx
  - desktop/src/renderer/src/components/EditorArea.tsx
  - desktop/src/renderer/src/index.css
related_insights:
  - wiki/changes/0043_desktop-workbench-phase1.md
---

# 编辑器 IDEA 式 git 行标记——HEAD 基线、行级 diff 与 Monaco gutter 装饰

## 摘要

工作台编辑器打开文件时在行号旁显示变更色条（IDEA/VS Code 同款）：🟩 新增 / 🟦 修改 / 灰短杠 删除（锚定在删除位置的下一条行号顶部），minimap/overview ruler 同步投影；编辑时 400ms 防抖实时刷新，保存后保留（对比基线是 HEAD，commit 后消失）。

## 变更内容

- **`git:headFile` IPC**（`main/git.ts`）：`git show HEAD:<rel>` 取已提交基线。语义三分：untracked / unborn HEAD → `content: null` = **空基线**（整文件视为新增，全绿）；非 repo / 无 git / 超 1 MiB（对齐编辑器读上限）→ `ok: false` = 无标记；正常 → HEAD 文本。async spawn（同 `gitStatus`，同步 spawn 会阻塞 main IPC 循环）。
- **`lib/lineDiff.ts`** 行级 diff：先裁公共前缀/后缀（常规编辑只剩极小中段），中段 LCS DP（Uint32Array）；超 4M cell 降级为整块 modified（仅整文件重写触发）。分类：连续非等价簇 = 一次编辑，del+add 相邻**配对为 modified**、富余 add = added、富余 del 取一灰标锚定下一条行。两个语义决策：`''` 按 **0 行**处理（新文件全 added，否则幻影空行会与首行配对成 modified）；行尾 `\n` 产生的幻影行在 Monaco 侧用 `getLineCount()` 夹掉。
- **`MonacoPane` `headText` 属性**：`marginClassName` 打 3px 色条（CSS 走 `--c-success`/`--c-accent` 变量、明暗主题自适应）；`overviewRuler` 是 **canvas 绘制必须硬编码 hex**（CSS 变量在 canvas 不解析）。防抖 400ms 重算（value/headText/path 变化触发；编辑器异步创建完成后补一次应用）。
- **`EditorArea`**：tab 打开时取一次 HEAD（HEAD 只在 commit 变、不随保存刷新），`heads: Record<path, string|null>`（'' = 新文件基线，null = 无标记）。

## 已知边界

- 终端里 commit 后**已打开 tab 的标记不消**（基线是打开时取的）——重开 tab 即刷新；commit 不 bump fsVersion，渲染层无感知信号。
- chat 模式右侧树的抽屉查看器（MonacoPane 另一使用点）未接 `headText`。
- 删除标记不可点击查看被删内容（IDEA 的三角点击弹层），仅位置标记。

## 验证

lineDiff 用 esbuild 转译后跑 8 组用例（identical / 新文件 / 改中行 / 删中行 / 追加 / 删尾行 / 2→3 替换 / 多处编辑）全部符合预期；tsc + build 绿。

## 相关页面

- [[0043_desktop-workbench-phase1]] - 工作台编辑器本体
