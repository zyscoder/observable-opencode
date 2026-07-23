# Root Confirmation Stability Task 2 Critical Fix Wave 25

Base commit: `54625368a`

Close all findings from independent review nine.

## 1. Apply formal provenance binding to every relevant record

Extract an event-independent subject/provenance binding validator. Any record
with an explicit `revision_provenance_status` other than `valid` is
active-revision ineligible. Under a formal manifest, every revision-bearing or
default-start record, including `response.output`, must carry matching subject
revision and valid provenance. Legacy omission remains allowed only without a
formal manifest.

## 2. Support verified artifact evidence in confirmation

Separate path-node validation from evidence-reference validation.
`checked_evidence_refs` may resolve to either:

- an active-revision eligible trace node; or
- an available, verified manifest artifact.

Build complete artifact evidence envelopes for confirmation requests. Queue,
live confirmation, partial/pending/completed restore, and evaluator validation
must accept valid artifacts and reject missing/truncated/unresolved artifacts.
An enqueue failure must create an explicit per-seed blocker rather than
silently dropping the candidate.

## 3. Enforce unique local occurrences and relation bijection

Reject duplicate `LocalStateOwner.occurrence_identity` across step judgments
where uniqueness is required. Canonically reconcile causal relations with all
judgment predecessor projections bidirectionally, rejecting duplicate,
missing, extra, substituted, or contradictory occurrences in live state,
partial/pending/completed restore, and evaluator validation.

## 4. Share nested investigation owner quarantine

Use one owner/seed extraction helper for partial, pending, and completed
investigation journals. Inspect top-level owner fields and nested
`active_visit`, directive arguments, hypothesis, seed binding, and referenced
state. Quarantine stale-owned entries before report validation and rebuild
derived investigation counts/metadata from retained entries.

## Constraints And Verification

- Task 2 only; offline, passive, read-only; no Agent feedback.
- No dependency or Task 3 behavior.
- Strict TDD.
- Preserve identities unless persisted shape/meaning changes.
- Run focused tests, graph/artifact/recursive/checkpoint/evaluator suites, full
  attribution suite, compileall, and `git diff --check 54625368a..HEAD`.
- Commit only this wave and write
  `.superpowers/sdd/root-confirmation-task-2-fix25-report.md`.
