---
type: concept
title: 项目上下文加载规则
slug: 0014_project-context-loading
aliases:
  - project context
  - CLAUDE.md
  - .xihe.md
  - context files
tags:
  - architecture
  - prompts
  - config
status: active
created: 2026-07-13
updated: 2026-07-13
related_pages:
  - wiki/concepts/0002_tool-registry-and-dispatch.md
  - wiki/concepts/0013_toolset-scope-and-dynamic-expansion.md
  - wiki/entities/0001_xihe-agent.md
---

# 项目上下文加载规则

## 摘要

xihe 在构建 system prompt 时会加载项目目录下的上下文文件（`.xihe.md`、`AGENTS.md`、`CLAUDE.md`、`.cursorrules`）。这些文件提供项目特定的指令、约定和约束。加载行为可通过 config 控制——`.xihe.md` 和 `AGENTS.md` 始终加载，`CLAUDE.md` 和 `.cursorrules` 可配置开关。

## 加载机制

`core/prompt_context.py` 的 `load_project_context()` 在 `build_system_prompt()` 第 7 层调用（`core/prompts.py`）。

### 加载规则（ALL 匹配，不是 first-match-wins）

所有存在的文件**全部加载并合并**（不同工具的配置可以共存）：

| 文件 | 始终加载? | 说明 |
|---|---|---|
| `.xihe.md` | ✅ 是 | xihe 专属上下文（xihe 自己的项目指令） |
| `AGENTS.md` | ✅ 是 | agent 通用指令（跨工具共享） |
| `CLAUDE.md` | ❌ 可配置 | Claude Code 专属（`session.load_claude_md`） |
| `.cursorrules` | ❌ 可配置 | Cursor IDE 专属（`session.load_cursorrules`） |

### 设计原因

**旧逻辑是 `or`（first-match-wins）**——`.xihe.md` 存在就不加载 CLAUDE.md。但不同工具的配置文件服务于不同 agent，应该**共存**而非互斥。例如：`.xihe.md` 给 xihe 用，`CLAUDE.md` 给 Claude Code 用，两者内容不同、不冲突。

改成了加载**所有存在的文件**，每段加 `## 文件名` 标题分隔。

## 配置

`config.yaml`：
```yaml
session:
  load_claude_md: false       # 不加载 CLAUDE.md（省 ~8K token/call）
  load_cursorrules: false     # 不加载 .cursorrules
```

- `.xihe.md` 和 `AGENTS.md` **始终加载**，不受配置控制（xihe 的核心上下文）
- 默认值 `true`（向后兼容）
- 子 agent（delegate_depth > 0）跳过所有上下文文件（`skip_context_files=True`）

## 各文件职责

### `.xihe.md`（xihe 专属）

包含 xihe agent 需要知道的项目信息：
- 环境约束（内网、非多模态模型）
- 运行态位置（`~/.xihe-agent/`）
- 工具约定（scratch/scripts 目录、read-before-edit）
- 浏览器/MCP/cron/模型/gateway 行为要点

**不放开发者信息**（安装命令、架构实现细节、API 用法）——那些放 CLAUDE.md 或代码注释。

### `CLAUDE.md`（Claude Code 专属）

包含 Claude Code 工作时需要的开发者信息：
- 项目是什么、怎么安装运行
- 架构设计（tool registry、agent loop、session、config）
- 编辑代码时的 gotchas（加工具 = register + toolset、browser check_fn 门控）
- Playwright sync-API 约束

**默认不加载**（`load_claude_md: false`），因为 xihe agent 不需要开发者视角的信息。需要时可改回 `true` 或 agent 自行 `read_file` 查阅。

## Token 影响

| 配置 | 加载内容 | system prompt 增量 |
|---|---|---|
| 全开（旧默认） | CLAUDE.md ~9K chars | +~9K chars ≈ +~6-8K token/call |
| 全关（当前） | .xihe.md ~2.5K chars | +~2.5K chars ≈ +~2K token/call |
| 差异 | | **省 ~6-8K token/call** |

在 toolset scope（[[0013_toolset-scope-and-dynamic-expansion]]）的基础上进一步精简 system prompt。

## 调用链

```
XiheAgent._build_system_prompt (core/agent.py)
  → 读 config: session.load_claude_md, session.load_cursorrules
  → build_system_prompt (core/prompts.py)
    → load_project_context (core/prompt_context.py)
      → _load_xihe_md(cwd)          # 始终
      → _load_agents_md(cwd)        # 始终
      → _load_claude_md(cwd)        # if include_claude_md
      → _load_cursorrules(cwd)      # if include_cursorrules
      → 合并所有，加标题
```

`_load_xihe_md` 从 cwd 向上遍历到 git root，搜索 `.xihe.md` / `XIHE.md` / `xihe.md`。其他文件在 cwd 查找。

## 相关页面

- [[0002_tool-registry-and-dispatch]] — system prompt 各层组装
- [[0013_toolset-scope-and-dynamic-expansion]] — 另一个 system prompt 精简手段
- [[0001_xihe-agent]] — 项目总览
