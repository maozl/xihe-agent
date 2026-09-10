# Skills 系统

## 概述

Skills 是 Xihe Agent 的**过程记忆**——把反复验证过的任务方法论沉淀为可复用的指令模板。和 Memory（声明式事实）不同，Skills 是**窄域且可执行**的：它告诉你"怎么做某一类任务"。

核心设计采用 progressive disclosure 架构：

- **Tier 1 — 索引层**：system prompt 自动注入所有 skill 的 name + description，LLM 每轮都能看到
- **Tier 2 — 指令层**：LLM 调用 `skill_view(name)` 按需加载完整 SKILL.md
- **Tier 3 — 关联文件层**：调用 `skill_view(name, file_path="references/xxx.md")` 加载补充文档

## 工具列表

| 工具 | 类型 | 说明 |
|------|------|------|
| `skills_list` | 只读 | 列出所有 skill 的名称、描述、分类 |
| `skill_view` | 只读 | 加载 skill 完整内容或关联文件 |
| `skill_manage` | 读写 | 创建/编辑/删除 skill（6 个 action） |

### skill_manage 详解

| Action | 说明 | 必填参数 |
|--------|------|----------|
| `create` | 创建新 skill（SKILL.md + 目录） | name, content |
| `edit` | 全量替换 SKILL.md（大改） | name, content |
| `patch` | 精准 find-and-replace（小改） | name, old_string, new_string |
| `delete` | 删除整个 skill | name |
| `write_file` | 添加/替换 supporting file | name, file_path, file_content |
| `remove_file` | 删除 supporting file | name, file_path |

## 目录结构

```
skills/                              # Bundled skills（随代码发布，只读）
├── diagramming/
│   ├── DESCRIPTION.md               # 分类描述（可选）
│   └── excalidraw/
│       └── SKILL.md
├── domain/
│   └── domain-intel/
│       ├── SKILL.md
│       └── references/
│           └── osint-sources.md
├── email/
│   ├── DESCRIPTION.md
│   └── himalaya/
│       ├── SKILL.md                 # 主指令文件（必须）
│       └── references/              # 补充文档（可选）
│           ├── configuration.md
│           └── message-composition.md
└── software-development/
    ├── DESCRIPTION.md
    ├── systematic-debugging/
    │   └── SKILL.md
    └── test-driven-development/
        └── SKILL.md

~/.xihe-agent/skills/                # User skills（用户创建，可读写）
├── my-workflow/
│   ├── SKILL.md
│   ├── references/
│   ├── templates/
│   └── scripts/
└── devops/
    └── deploy-checklist/
        ├── SKILL.md
        └── templates/
            └── deploy-config.yaml
```

### 目录层级规则

- **无分类**：`skills/my-skill/SKILL.md` → category 为 "general"
- **有分类**：`skills/category-name/my-skill/SKILL.md` → category 为 "category-name"
- 分类目录下可放 `DESCRIPTION.md` 描述该分类
- Skill 下允许的子目录：`references/`、`templates/`、`scripts/`、`assets/`

### 优先级

User skills 目录（`~/.xihe-agent/skills/`）优先于 bundled skills 目录。同名 skill 以 user 版本为准。

## SKILL.md 格式

SKILL.md 由 YAML frontmatter + Markdown body 组成：

```markdown
---
name: my-skill
description: 一句话描述这个 skill 做什么，解决什么问题
version: 1.0.0
prerequisites:
  commands: [git, docker]            # 依赖的外部命令
platforms: [linux, macos]            # 平台限制（可选，缺省=全平台）
metadata:
  tags: [tag1, tag2]
---

# Skill 标题

## Prerequisites

1. 前置条件说明
2. 安装命令

## Instructions

1. 具体步骤，包含精确命令
2. ...

## Pitfalls

- 已知坑点
- 边界情况

## Verification

验证步骤，确认任务完成

## References

- `references/extra-docs.md`（补充文档）
- `templates/config.yaml`（配置模板）
```

### Frontmatter 必填字段

| 字段 | 必须 | 说明 |
|------|------|------|
| `name` | 是 | Skill 名称，小写+连字符，最长 64 字符 |
| `description` | 是 | 一句话描述，最长 1024 字符 |

### Frontmatter 可选字段

| 字段 | 说明 |
|------|------|
| `version` | 版本号 |
| `prerequisites.commands` | 依赖的外部命令列表 |
| `platforms` | 支持的平台（linux/macos/windows） |
| `metadata.tags` | 标签列表 |

### 命名规则

- 只允许小写字母、数字、连字符、下划线、点
- 必须以字母或数字开头
- 最长 64 字符
- 正则：`^[a-z0-9][a-z0-9._-]*$`

## Skill 索引注入机制

### 自动注入

System prompt 构建时，`build_skills_prompt()` 自动扫描所有 skill 目录，生成紧凑的索引注入到 system prompt 中：

```
## Available Skills
Before replying, scan the skills below. If one clearly matches your task,
load it with skill_view(name) and follow its instructions. If a skill has
issues, fix it with skill_manage(action='patch').
After difficult/iterative tasks, offer to save as a skill. If a skill you
loaded was missing steps or had wrong commands, update it before finishing.

<available_skills>
  email:
    - himalaya: CLI to manage emails via IMAP/SMTP...
  software-development:
    - systematic-debugging: Use when encountering any bug, test failure...
    - test-driven-development: Use when implementing any feature or bugfix...
</available_skills>

If none match, proceed normally without loading a skill.
```

### 缓存机制

- **内存缓存**：`build_skills_prompt()` 结果缓存在进程内，以 `(bundled_dir_mtime, user_dir_mtime)` 为 key
- **自动失效**：skill 文件的 mtime 变化时，下次构建自动重新扫描
- **主动失效**：`skill_manage` 执行成功后调用 `clear_skills_cache()` 立即清缓存
- 子 agent（`delegate_depth > 0`）跳过 skills 注入

### 注入条件

只有当 `skill_manage` / `skill_view` / `skills_list` 工具可用时，才注入 skills 索引。

## Skill 生命周期

```
               ┌─────────────────────────────────────────┐
               │           用户请求任务                    │
               └──────────────┬──────────────────────────┘
                              │
               ┌──────────────▼──────────────────────────┐
               │  LLM 扫描 system prompt 中的 skill 索引  │
               └──────────────┬──────────────────────────┘
                              │
                  ┌───────────┴───────────┐
                  │                       │
           有匹配的 skill           没有匹配的 skill
                  │                       │
     ┌────────────▼──────────┐            │
     │ skill_view(name)      │            │
     │ 加载完整指令          │     正常执行任务
     └────────────┬──────────┘            │
                  │                       │
     ┌────────────▼──────────┐            │
     │ 按指令执行任务        │            │
     └────────────┬──────────┘            │
                  │                       │
         ┌────────┴────────┐              │
         │                 │              │
    指令有效           指令有问题          │
         │                 │              │
     执行完成      skill_manage           │
         │          (action='patch')      │
         │                 │              │
         └────────┬────────┘              │
                  │                       │
         ┌────────▼───────────────────────▼┐
         │  任务复杂/迭代/发现新模式？       │
         └────────┬────────────────────────┘
                  │ 是
         ┌────────▼────────┐
         │ skill_manage    │
         │ (action='create')│
         │ 保存为新 skill  │
         └─────────────────┘
```

### 什么时候创建 skill

- 完成复杂任务（5+ 次工具调用）
- 克服了棘手的错误
- 用户纠正过的方法确实有效
- 发现了非显而易见的工作流
- 用户要求记住某个流程

### 什么时候更新 skill

- 使用 skill 时发现指令过时/有错
- 缺少步骤或发现新坑点
- 操作系统特定的失败
- 优先用 `patch`（小改），大改才用 `edit`

### 什么时候不用创建 skill

- 简单的一次性任务
- 通用的 LLM 能力（写邮件、翻译、总结）
- 已经被其他 skill 覆盖的场景

## 原子写入

所有 skill 文件写入使用 `_atomic_write_text()`：

1. 在目标目录创建临时文件 `.filename.tmp.xxxx`
2. 写入完整内容
3. `os.replace()` 原子替换目标文件

保证目标文件永远不会处于部分写入状态，即使进程崩溃。

## 内容限制

| 限制 | 值 | 说明 |
|------|-----|------|
| SKILL.md 最大字符数 | 100,000 | ~36k tokens |
| 单个 supporting file 最大字节 | 1 MiB | 1,048,576 bytes |
| Skill 名称最大长度 | 64 字符 | |
| Description 最大长度 | 1,024 字符 | |
| Frontmatter 必须有 | name, description | 缺少则创建/编辑失败 |

## 代码结构

```
tools/
├── skills_tool.py          # skills_list + skill_view（只读工具）
└── skill_manager_tool.py   # skill_manage（读写工具，6 个 action）

core/
└── prompts.py              # build_skills_prompt() — 索引构建 + 缓存
```

### 关键函数

| 函数 | 文件 | 说明 |
|------|------|------|
| `build_skills_prompt()` | `core/prompts.py` | 构建 skill 索引字符串，带缓存 |
| `clear_skills_cache()` | `core/prompts.py` | 清除索引缓存 |
| `_scan_skills_index()` | `core/prompts.py` | 扫描所有 SKILL.md，返回分类索引 |
| `_skill_manage()` | `tools/skill_manager_tool.py` | skill_manage 入口，分发到各 action |
| `_create_skill()` | `tools/skill_manager_tool.py` | 创建新 skill |
| `_edit_skill()` | `tools/skill_manager_tool.py` | 全量替换 SKILL.md |
| `_patch_skill()` | `tools/skill_manager_tool.py` | 精准 find-and-replace |
| `_delete_skill()` | `tools/skill_manager_tool.py` | 删除 skill |
| `_write_file()` | `tools/skill_manager_tool.py` | 写入 supporting file |
| `_remove_file()` | `tools/skill_manager_tool.py` | 删除 supporting file |
| `_find_skill()` | `tools/skill_manager_tool.py` | 跨目录查找 skill |
| `_atomic_write_text()` | `tools/skill_manager_tool.py` | 原子写入文件 |
