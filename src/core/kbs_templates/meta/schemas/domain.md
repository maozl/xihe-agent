# Domain Hub Page Schema

Use a domain page for a **business line / capability area** that groups entities, insights, and concepts. A domain is a permanent navigation axis, not an ongoing workstream.

`wiki/domains/index.md` is the registry (controlled vocabulary). One hub page lives at `wiki/domains/<slug>.md` for each registered domain.

## When To Use

- a business line has, or will grow to have, several entities and insights worth grouping
- the user navigates by business line ("the data-governance stuff", "the security platforms")
- a stable area needs a single entry point with positioning, scope, and current status

## When NOT To Use

- a single named system or tool → use an `entity`
- an evolving research thread with a thesis and next steps → currently no `project` type; use `active.md` plus an `insight`
- a one-off comparison or conclusion → use an `insight`

Do not create a domain hub without registering the slug in `wiki/domains/index.md` first, and do not add a domain to the registry without explicit user confirmation.

## Frontmatter

```yaml
---
type: domain
title: 数据治理 / 数据地图
slug: data-governance
aliases:
  - 数据地图
  - datamap
status: active
created: 2026-07-28
updated: 2026-07-28
owner: 信息数据部
thesis: 企业数据资产的目录、血缘、质量、申请审批与元数据治理中枢
related_domains:
  - security
---
```

## Field Notes

- `slug`: must match the folder name and the registry entry exactly (lowercase, hyphenated)
- `aliases`: names the user might say in conversation; used for domain resolution
- `owner`: optional business owner / department
- `thesis`: one line on what this business line is and why it matters; refresh as the line's role evolves
- `related_domains`: other domains that share pages or boundaries (cross-links the hub)

## Suggested Body

```markdown
# 数据治理 / 数据地图

## 定位 (Positioning)

One or two sentences on what this business line covers and why it exists.

## 范围 (Scope)

Live inventory of pages that claim this domain. Reconcile during lint.

- Entities:
  - [[数据地图元数据]] - 元数据模块功能说明
  - [[数据地图-子系统架构详解]] - 6个核心子系统架构
- Insights:
  - [[企业平台体系洞察]] - 平台体系总览
- Concepts:
  - (none yet)

## 当前判断 (Current Thesis)

The current best working view of this business line. Light-weight; this absorbs the status role a project page would play.

## 开放问题 (Open Questions)

- question
- question

## 相关域 (Related Domains)

- [[security]] - SQLScan 同时落在数据治理与安全两个域
```

## Maintenance Notes

- The scope section is a live inventory, not a one-time list. During lint, reconcile it against every page whose `domain` field includes this slug.
- Cross-domain pages (e.g. SQLScan) must appear here even though their physical home is elsewhere.
- If this domain stays at one or two pages for a long time, propose merging into an adjacent domain during lint; do not merge without confirmation.
