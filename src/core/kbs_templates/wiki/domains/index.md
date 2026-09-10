---
type: domain-registry
title: 业务域注册表 (Controlled Vocabulary)
---

# 业务域注册表 (Controlled Vocabulary)

> This is the **only source of truth** for valid business-domain slugs. Every page's `domain` frontmatter value must appear in the table below.

> **Ownership**: the user owns this registry. The agent maintains the pages within each domain. Creating / merging / splitting / retiring a domain is a structural change and requires explicit confirmation.

## Registered Domains

| slug | 中文名 | 别名 (aliases) | 定位 (positioning) | 实体目录 |
|------|--------|----------------|--------------------|----------|
| (none yet — add business domains as structural changes, with user confirmation) | - | - | - | - |

## Conventions

- Slugs are lowercase, hyphenated, English. One canonical Chinese name and optional short aliases per domain.
- The first value in a page's `domain` list is its **primary** domain. For entities, the primary domain also decides the physical subfolder.
- A page may carry **multiple** domains. It physically lives under its primary domain but must be referenced from every domain it touches.
- `entities/_shared/` maps to the `cross-cutting` slug.
- Insights and concepts stay flat under their own folders; their `domain` field is the only placement signal.
