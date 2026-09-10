# Lint Report Schema

Use a lint report for a human-readable maintenance pass over the knowledge base or one focused part of it.

This is a markdown-first review document, not a JSON-first machine report. Keep the metadata light and put the actual maintenance judgment in the body.

## When To Use

- a lint pass reviews freshness, structure, cross-links, duplicates, candidate backlog, or domain health
- the user needs a durable maintenance report that explains what was checked and what should change next
- the output should guide promotion, merge, drop, cleanup, domain changes, or follow-up work

## Frontmatter

```yaml
---
type: lint-report
title: Domain Health and Candidate Review
slug: domain-health-review-2026-07-28
date: 2026-07-28
scope: domain-health-and-candidates
reviewed_paths:
  - wiki/domains/
  - wiki/entities/
  - meta/candidates/
---
```

## Field Notes

- `date`: the date of the review pass
- `scope`: a short label for what this report mainly reviewed
- `reviewed_paths`: only the paths actually checked during this pass
- keep findings, actions, and recommendations in markdown sections rather than encoding them as nested frontmatter data

## Suggested Body

```markdown
# Domain Health and Candidate Review

## Summary

Short paragraph or 2-4 bullets describing the overall health of the reviewed area.

## Findings

- [high] Two domain hubs have scope sections that no longer match the pages claiming that domain.
- [medium] `wiki/entities/_shared/系统负责人信息.md` claims `domain: [cross-cutting]` but is not listed in `wiki/domains/cross-cutting.md`.
- [low] One entity uses the slug `data-gov` instead of the registered `data-governance` — auto-corrected.
- [low] The `big-data` domain has only one entity; consider merging into `dev-platform` at the next pass.

## Candidate Actions

- `candidate-sqlscan-ci-integration-boundary.md` -> promote to `wiki/insights/sqlscan-ci-boundary.md`
- `candidate-keep-every-chat-fragment.md` -> drop because it does not meet the work-memory boundary

## Domain Actions

- reconcile `wiki/domains/security.md` scope with entities tagged `security`
- propose merging `big-data` into `dev-platform` (awaiting user confirmation)

## Recommended Updates

- refresh `wiki/recent.md` after the promotion pass
- update each touched hub's scope section
- remove stale candidate references from `meta/candidates/index.md`

## Follow-Up

Optional. Use this section for deferred checks, sequencing notes, or a suggested next lint pass.
```
