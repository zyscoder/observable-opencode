# Root Confirmation Stability Task 2 Critical Fix Wave 17

Base commit: `63b1f6056`

Close every Critical and Important finding from the independent whole-Task-2
review. Keep this work inside Task 2.

## 1. Validate the event-specific active repository revision

`active_repository_revision()` derives the authoritative CaseTrace generation
from current response claims, effective verifications, and `change.revision_after`.
`active_revision_evidence_eligible()` must validate the corresponding
event-specific field rather than looking only at `repository_revision`.

- `response.claim` and effective `verification`: validate `repository_revision`.
- `change`: validate `revision_after`.
- Preserve numeric zero and reject booleans, negative values, malformed values,
  and mismatches.
- When an authoritative active generation exists, an event type that requires a
  generation field but omits or malforms it must fail closed.
- Legacy event types without a defined generation field retain the documented
  compatibility behavior.

Strict RED: an active decision -> stale `change(revision_after=0)` -> active
defect under active generation 1 must be rejected by graph eligibility, capsule
construction, direct global validation, confirmation, factor/root publication,
and partial/completed restore. Add active-generation, zero, malformed, missing,
alias, and no-authority counterparts.

## 2. Reject stale analysis starts before any Judge request

Default and explicit start refs must pass active-revision evidence eligibility
before becoming current nodes, active focus, recursive frontier entries, global
Judge inputs, or `no_defect` outcomes. Retain the seed as an auditable
`evidence_gap`/`inconclusive` result with a stable blocker; do not invoke any
Judge or publish a root.

Cover ordinary stale claims/defects, explicit starts, default starts,
multi-seed isolation, checkpoint resume, and a valid active start.

## 3. Rebuild Judge-facing episode context from active members

Filter causal episode and progress episode member refs with the active-revision
evidence policy before slicing or summary construction. Recompute summaries,
counts, windows, support/evidence collections, delivery signals, and any
Judge-facing episode payload from active members only. Stale members may appear
only in an explicit audit diagnostic that cannot become grounded evidence or
influence Judge reasoning.

Cover mixed active/stale episodes, all-stale episodes, limit=1 backfill,
summaries/counts, aliases, and persisted/resumed contexts.

## 4. Filter every bounded scan before consuming its budget

All adjacency and semantic-search/investigation paths must filter ineligible
endpoints before `islice`, scan counters, ranking, limit slicing, and truncation
metadata. Stale nodes cannot consume `scan_limit` or suppress active backfill.
Truncation metadata must describe eligible candidates, not raw stale entries.

Cover limit/scan_limit 1, active backfill, ties, no eligible entries, and normal
active inputs.

## 5. Separate Judge evidence from unresolved diagnostics

Unresolved refs must never appear in any Judge-visible evidence-bearing field,
including `validation_source.candidate_evidence_refs`, hypothesis evidence,
candidate evidence collections, capsule prompts, global prompts, or
confirmation requests. Keep them only in explicit missing/unresolved diagnostic
collections that are excluded from prompts and grounded refs.

Tests must inspect the exact serialized Judge payload, not only output
validation. Cover candidate evidence, supporting/opposing hypothesis evidence,
artifacts, aliases, capsule restore, and checkpoint resume.

## 6. Clarify evidence and authored-root eligibility naming

Use `active_revision_evidence_eligible()` for ordinary facts and path members.
Reserve candidate/root terminology for the additional authored-root eligibility
gate. Remove or tightly constrain the compatibility alias so call sites cannot
mistake generic active evidence for an authored root candidate.

## Constraints And Verification

- Offline, passive, read-only; no Agent-visible behavior or feedback changes.
- Retrieval score/rank remains navigation-only.
- Missing evidence produces `evidence_gap` or `inconclusive`, never a forced root.
- Preserve per-seed isolation.
- No new dependency and no Task 3 evidence-expansion behavior.
- Strict TDD. Record each reviewer reproduction as RED before implementation.
- Bump serialized identities only if persisted shape or meaning changes.
- Run focused finding tests, all affected attribution suites, the complete
  attribution suite, compileall, and `git diff --check 63b1f6056..HEAD`.
- Commit only this wave and write
  `.superpowers/sdd/root-confirmation-task-2-fix17-report.md`.
