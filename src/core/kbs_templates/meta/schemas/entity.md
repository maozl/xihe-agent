# Entity Page Schema

Use an entity page for a named thing: person, company, product, tool, dataset, institution, or named system.

## When To Use

- the same named thing appears across multiple sources or business lines
- the entity needs its own facts, role, or comparison context
- future work will likely refer to it again by name

## Frontmatter

```yaml
---
type: entity
title: DPM密码管理平台
slug: dpm-password-management
aliases:
  - DPM
  - 数据密码管理平台
domain:
  - security
tags:
  - password-management
  - devops
status: active
created: 2026-07-28
updated: 2026-07-28
related_pages:
  - wiki/entities/_shared/企业平台目录.md
sources:
  - path: https://portal.example.internal
    date: 2026-07-28
---
```

## Field Notes

- `aliases`: alternate spellings, brand names, or short names worth preserving
- `domain`: **all** business lines this entity touches (multi-valued). The first value is the *primary* domain and decides the physical subfolder. Every value must exist in `wiki/domains/index.md`.
- `related_pages`: only the pages that matter most for reuse or comparison
- `status`: usually `active` or `archived`; keep it simple unless the entity truly stops mattering

## Physical Placement

Entities live in `wiki/entities/<primary-domain>/`. If an entity genuinely spans domains with no clear primary, place it in `wiki/entities/_shared/` and set `domain: [cross-cutting]` plus the touched domains.

## Suggested Body

```markdown
# DPM密码管理平台

## 摘要 (Summary)

Short description of what it is and why it matters.

## 关键事实 (Key Facts)

- what it is
- role in its business line

## 相关页面 (Related Pages)

- [[企业平台目录]] - cross-cutting catalog
- [[数据地图元数据]] - downstream consumer in another domain
```
