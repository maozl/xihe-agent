# Candidate Note Schema

Use a candidate note for provisional but valuable work memory that is not yet stable enough for the formal wiki.

A candidate is transitional work memory. It should stay lightweight and eventually end in `promoted`, `merged`, or `dropped`.

## When To Use

- a conversation produces a promising hypothesis, reframing, or open question with future reuse value
- the material clearly matters to an existing topic or business line, but it still needs validation, synthesis, or cleanup
- writing it directly into `concept`, `entity`, or `insight` would be premature today

Do not use a candidate note for persona notes, raw transcript storage, or one-off task tracking.

## Lifecycle

`open -> promoted | merged | dropped`

Keep the state simple. The goal is to record whether the note is still provisional and, if it is no longer open, what resolved it.

## Frontmatter

```yaml
---
type: candidate
title: Candidate: SQLScan CI Integration Boundary
slug: candidate-sqlscan-ci-integration-boundary
status: open
created: 2026-07-28
updated: 2026-07-28
domain:
  - data-governance
  - security
related_topic: data-governance
derived_from:
  - wiki/entities/data-governance/SQLScan系统.md
why_it_matters: This would change how SQLScan gates the dev-platform release pipeline.
next_action: Validate against the PACE build template, then promote into an insight.
---
```

The `domain` field is **optional** for candidates: add it when the business line is already clear, omit it when the domain is still uncertain.

When the candidate is resolved, add the smallest useful resolution metadata:

```yaml
resolved_at: 2026-07-30
resolution_target: wiki/insights/sqlscan-ci-boundary.md
resolution_note: Promoted after review confirmed the judgment held across related entities.
```

## Field Notes

- `status`: use `open`, `promoted`, `merged`, or `dropped`
- `domain`: optional; all business lines this note likely touches (multi-valued, must be registered slugs)
- `resolved_at`: add this when the candidate is no longer `open`
- `resolution_target`: the page path that absorbed or replaced the candidate; omit this when the outcome is simply `dropped`
- `resolution_note`: one short sentence explaining why the candidate was promoted, merged, or dropped
- `related_topic`: one primary topic, thread, or page path; keep secondary links in the body
- `derived_from`: direct source or page paths that triggered the note, not a raw chat dump
- `why_it_matters`: one sentence on why this note has reuse value
- `next_action`: the smallest next step that could resolve or advance the note; this is mainly for `open` candidates
- `updated`: refresh this when the note changes or when resolution metadata is added so stale candidates are easier to spot

## Suggested Body

```markdown
# Candidate: SQLScan CI Integration Boundary

## Summary

Short description of the provisional idea, judgment, or open question.

## Provisional Notes

- supporting point
- supporting point

## Open Questions

- what still needs to be checked

## Related Pages

- [[SQLScan系统]] - entity this could update
- [[dpm-password-usage-guide]] - possible destination if validated

## Resolution

Use this only when the candidate is no longer open.

- outcome and why
- destination page if promoted or merged
```
