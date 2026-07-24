# Root Confirmation Task 2 Fix Wave 31 Brief

Base commit: `ca2ffd027`

## Scope

Close both Important findings from the final cumulative Task 2 review. Keep the
analysis offline, passive, read-only, and unable to feed results back to the
Agent. Do not implement Task 3, add dependencies, or touch unrelated untracked
files.

Use strict TDD. Record focused RED and GREEN for each finding, then run affected
and full attribution suites, `compileall`, and
`git diff --check ca2ffd027..HEAD`.

## Required Fixes

### 1. Detect one-authority-field residual global terminals

The ledger-aware residual signature must reject a formerly global pass or
failure episode even when:

- all explicit global markers are removed;
- the canonical owner is replaced by an ordinary owner that retains only the
  correct seed binding; and
- one additional non-marker authority field is removed:
  `defect_fingerprint` from a completed/failed pass, or `defect_state_id` from
  a failed episode.

Root cause: the current classifier requires the complete
`seed_ref + defect_fingerprint` or `node_ref + defect_state_id` pair before it
considers the otherwise distinctive terminal shape.

Use the authoritative seed ledger plus structural terminal fields to classify
partial records. A pass-like record must have a completed/failed action shape
and at least one unambiguous ledger authority match. An episode-like record must
have the failure-episode structural fields that remain and at least one
unambiguous ledger authority match. Ambiguous or contradictory authority must
be rejected, never guessed or silently filtered.

The same classifier must protect live terminal derivation, checkpoint restore,
report/evaluator validation, and stale-seed quarantine. Add controls showing
ordinary investigation and unresolved records, including records that happen
to reference a known seed, are not misclassified without the distinctive
terminal shape.

### 2. Bind rejected factual request projections to semantic identity

The exact persisted `factual_request_projection` must be cryptographically
bound to `semantic_identity` for both validated and `rejected_snapshot`
terminal confirmations.

Add one canonical projection validator/identity helper that:

- requires the exact versioned projection schema and the exact set of factual
  request fields;
- computes the same identity as a live `RootConfirmationRequest`;
- validates a persisted projection directly without rebuilding the request
  from the current graph.

For a rejected snapshot, restore must compare the persisted projection-derived
identity with `semantic_identity` before replay or acceptance. Preserve the
original factual projection as audit data; do not revalidate rejected artifact
content as evidence and do not invoke the confirmation Provider.

Include `factual_request_projection` in the canonical queue, journal, terminal
action projection, checkpoint, report, evaluator, and replay bijections. A
change to any factual request field in only one representation, or a coordinated
change that does not update the semantic identity correctly, must fail closed.
Version the affected persistence contracts explicitly.

## Tests

Create `tools/trace_attribution/tests/test_root_confirmation_fix31.py` with at
least:

1. completed and failed passes missing `defect_fingerprint` after marker/owner
   stripping;
2. failed episode missing `defect_state_id` after marker/owner stripping;
3. live, checkpoint, report/evaluator, and stale-quarantine rejection;
4. ordinary investigation/unresolved controls;
5. rejected queue projection tampering rejected by state restore;
6. rejected report/action/checkpoint projection tampering rejected;
7. valid rejected snapshot round-trip remains provider-free and seed-local;
8. valid ordinary terminal confirmation round-trip remains accepted.

Reuse existing Fix28-Fix30 fixtures where practical. Tests must exercise real
serialization and validation paths, not only isolated helpers.

## Verification

At minimum:

1. New Fix31 focused tests.
2. Fix17-Fix31 plus global terminal, confirmation, artifact, seed, checkpoint,
   report/evaluator/replay/acceptance regressions.
3. Directly affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check ca2ffd027..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix31-report.md` with root-cause
  validation, RED/GREEN evidence, version changes, exact commands/counts,
  changed files, and residual risks.
- One commit containing only Fix31 files, this brief, and report.
