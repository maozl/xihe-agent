# Insight Page Schema

Use an insight page for a durable conclusion, comparison, synthesis, decision record, or reusable takeaway produced through research or discussion.

## When To Use

- a result should not disappear into chat history
- a comparison or synthesis will likely be reused later
- a discussion materially changes the current understanding of a topic or business line

## Frontmatter

```yaml
---
type: insight
title: DPM密码使用方法指南
slug: dpm-password-usage-guide
domain:
  - security
tags:
  - password-management
  - best-practice
status: active
created: 2026-07-28
updated: 2026-07-28
confidence: high
sources:
  - path: raw/sources/dpm-platform-analysis.md
    date: 2026-07-28
derived_from:
  - wiki/entities/security/DPM密码管理平台.md
related_domains:
  - dev-platform
supersedes: []
superseded_by: []
---
```

## Field Notes

- `domain`: all business lines this insight touches (multi-valued). Insights stay flat under `wiki/insights/`; the field is the only placement signal. Every value must exist in `wiki/domains/index.md`.
- `confidence`: use `low`, `medium`, or `high` for how settled the conclusion is right now
- `derived_from`: direct upstream pages or source captures that produced this insight
- `related_domains`: domains that should surface this insight during review or retrieval
- `supersedes` and `superseded_by`: only use these when one insight clearly replaces or corrects another

## Suggested Body

```markdown
# DPM密码使用方法指南

## 摘要 (Summary)

Short explanation of the durable takeaway.

## 为什么重要 (Why It Matters)

Explain the practical or strategic significance.

## 证据 (Evidence)

- evidence point with citation
- evidence point with citation

## 注意事项 (Caveats)

- what is still uncertain

## 相关页面 (Related Pages)

- [[dpm-password-management]] - entity this guide depends on
```
