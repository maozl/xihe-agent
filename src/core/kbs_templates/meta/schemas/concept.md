# Concept Page Schema

Use a concept page for a stable idea, framework, definition, method, or recurring abstraction.

## When To Use

- the same idea shows up across multiple sources, business lines, or insights
- the user will likely need the concept again as a reusable lens
- the page is about the idea itself, not a named person, company, product, or system

## Frontmatter

```yaml
---
type: concept
title: 数据血缘
slug: data-lineage
aliases:
  - lineage
  - 血缘
domain:
  - data-governance
tags:
  - data-governance
  - governance
status: active
created: 2026-07-28
updated: 2026-07-28
related_pages:
  - wiki/domains/data-governance.md
sources:
  - path: raw/sources/datamap-overview.md
    date: 2026-07-28
---
```

## Field Notes

- `aliases`: alternate names worth searching or linking, not every casual synonym
- `domain`: all business lines where this concept applies (multi-valued). Concepts stay flat under `wiki/concepts/`; the field is the only placement signal. Every value must exist in `wiki/domains/index.md`.
- `related_pages`: a short list of the most relevant connected pages, not a full graph
- `updated`: change this when the concept meaning, framing, or key links change

## Suggested Body

```markdown
# 数据血缘

## 摘要 (Summary)

1-2 paragraphs defining the concept in reusable terms.

## 核心要点 (Core Points)

- core idea
- practical boundary or caveat

## 相关页面 (Related Pages)

- [[data-governance]] - domain where this concept is central
```
