---
type: concept
title: Skills 系统（progressive disclosure）
slug: 0007_skills-system
aliases:
  - Skills
  - 技能
tags:
  - architecture
  - skills
status: active
created: 2026-07-01
updated: 2026-07-01
related_pages:
  - wiki/entities/0001_xihe-agent.md
sources:
  - path: raw/sources/skills.md
    date: 2026-07-01
---

# Skills 系统（progressive disclosure）

## 摘要

Skills 是 xihe-agent 的**过程记忆**——把反复验证的任务方法论沉淀为可复用指令模板（区别于声明式事实的 Memory）。设计采用 **progressive disclosure 三层**: Tier1 索引层（system prompt 自动注入所有 skill 的 name+description）/ Tier2 指令层（`skill_view(name)` 按需加载完整 SKILL.md）/ Tier3 关联文件层（`skill_view(name, file_path=...)` 加载补充文档）。Bundled skills 随代码发布只读，user skills 在 `~/.xihe-agent/skills/` 可读写且优先级更高。

## 核心要点

- **三个工具**: `skills_list`（只读，列出名称/描述/分类）/ `skill_view`（只读，加载 SKILL.md 或关联文件）/ `skill_manage`（读写，6 action）。
- **`skill_manage` 6 action**: `create`（建 SKILL.md+目录）/ `edit`（全量替换，大改）/ `patch`（find-and-replace，小改）/ `delete` / `write_file`（supporting file）/ `remove_file`。
- **目录结构**:
  - bundled: `skills/[category/]<skill>/SKILL.md`，分类目录可放 `DESCRIPTION.md`。
  - user: `~/.xihe-agent/skills/...`，**优先于** bundled（同名以 user 版本为准）。
  - skill 下允许的子目录: `references/` / `templates/` / `scripts/` / `assets/`。
  - 无分类 → category="general"；有分类 → category=目录名。
- **SKILL.md 格式**: YAML frontmatter + Markdown body。
  - 必填: `name`（小写+连字符，正则 `^[a-z0-9][a-z0-9._-]*$`，≤64 字符）、`description`（≤1024 字符）。
  - 可选: `version` / `prerequisites.commands`（依赖命令）/ `platforms`（linux/macos/windows）/ `metadata.tags`。
  - body 建议: Prerequisites / Instructions / Pitfalls / Verification / References。
- **索引注入** (`build_skills_prompt`): 扫描所有 SKILL.md 生成紧凑索引注入 system prompt，附带「匹配则 `skill_view` 加载、有问题 `skill_manage patch`、复杂任务后 offer 保存为 skill」的引导。**仅当** skills 工具可用时注入；子 agent（`delegate_depth>0`）跳过。
- **缓存**: 进程内缓存，key 为 `(bundled_dir_mtime, user_dir_mtime)`；mtime 变自动失效；`skill_manage` 成功后 `clear_skills_cache()` 立即清。
- **原子写入** `_atomic_write_text`: 写临时文件 `.filename.tmp.xxxx` → `os.replace()` 原子替换，崩溃也不会部分写入。
- **内容限制**: SKILL.md ≤100k 字符（~36k tokens）；supporting file ≤1 MiB。
- **何时建 skill**: 完成 5+ 工具调用的复杂任务 / 克服棘手错误 / 用户纠正过的方法确实有效 / 发现非显而易见的工作流 / 用户要求记住。**不建**: 简单一次性任务 / 通用 LLM 能力 / 已被覆盖的场景。

## 适用场景

- 新增技能: 用 `skill_manage create` 或直接在 `skills/` 建目录 + SKILL.md。
- 排查「skill 没被注入」: 确认 SKILL.md frontmatter 有 `name`+`description`、工具可用、缓存已清。
- 维护: 指令过时优先 `patch`，大改才 `edit`。

## 涉及文件

`tools/skills_tool.py`（`skills_list`/`skill_view`）/ `tools/skill_manager_tool.py`（`skill_manage` 6 action + `_atomic_write_text`）/ `core/prompts.py`（`build_skills_prompt` / `clear_skills_cache` / `_scan_skills_index`）。

## 相关页面

- [[0001_xihe-agent]] — Skills 是核心子系统之一
- 原文快照: [raw/sources/skills.md](../../raw/sources/skills.md)
