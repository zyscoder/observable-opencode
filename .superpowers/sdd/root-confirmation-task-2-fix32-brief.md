# Root Confirmation Task 2 Fix Wave 32 Brief

Base commit: `51cc63d72`

## Scope

Close the remaining Important finding from the cumulative Task 2 review. Keep
the analysis offline, passive, read-only, and unable to feed results back to
the Agent. Do not implement Task 3, add dependencies, or touch unrelated
untracked files.

Use strict TDD. Record focused RED and GREEN, then run affected and full
attribution suites, `compileall`, and `git diff --check 51cc63d72..HEAD`.

## Required Fix

### Bind persisted request projection to terminal request semantics

Fix31 proves that a persisted factual request projection is canonical and that
its SHA-256 identity matches `semantic_identity`. It does not yet prove that the
projection still describes the outer queue/action/confirmation request.

Add a graph-free canonical binding validator shared by pending, validated, and
`rejected_snapshot` paths. It must compare the persisted projection facts with
the authoritative outer request state:

- `candidate_ref`;
- the complete canonical `DefectState` available in analysis state, including
  its fingerprint and defect-state identity;
- `recursive_path`;
- `hypothesis_id`;
- `hypothesis_semantic_hash`;
- `seed_binding_identity`;
- `analysis_perspective`.

The validator must also ensure the terminal `RootConfirmation`, owner, queue,
journal, and action projection agree on candidate, defect fingerprint, path,
hypothesis id/hash, seed identity, and request identity. Reuse the existing
canonical projection parser and identity helper. Do not rebuild a rejected
request from the current graph, hydrate or reauthorize rejected artifacts, or
call the Provider.

Invoke the same binding check before accepting checkpoint restore, report and
evaluator validation, action replay, and a newly persisted terminal action.
One-representation drift and coordinated projection+identity drift that still
contradicts the outer request state must fail closed.

This is an internal consistency contract, not a cryptographic authenticity
claim against an attacker who rewrites every authoritative representation.
Do not add signing, secrets, external state, or a new trust root.

## Tests

Create `tools/trace_attribution/tests/test_root_confirmation_fix32.py` using
real Fix31/Fix30 serialization fixtures. At minimum:

1. Change rejected `analysis_perspective`, recompute the valid request identity,
   and synchronize projection/identity copies while leaving the outer queue
   perspective unchanged; checkpoint restore must reject.
2. Repeat coordinated drift for candidate, defect state/fingerprint, path,
   hypothesis id/hash, and seed binding; each must fail at the appropriate
   restore/report/action/replay boundary.
3. Tamper a terminal confirmation or action outer field while projection and
   identity remain self-consistent; reject.
4. Valid rejected snapshot round-trip remains provider-free, graph-request-
   rebuild-free, audit-preserving, and seed-local.
5. Valid ordinary terminal and pending confirmation round-trips remain
   accepted.

Tests must distinguish internal consistency from total-store authenticity and
must not assert impossible detection after every authoritative copy is
coherently rewritten.

## Verification

At minimum:

1. New Fix32 focused tests.
2. Fix17-Fix32 plus confirmation, artifact, seed, checkpoint,
   report/evaluator/replay/acceptance regressions.
3. Directly affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check 51cc63d72..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix32-report.md` with root-cause
  validation, RED/GREEN evidence, exact commands/counts, changed files,
  version decisions, self-review, and residual risks.
- One commit containing only Fix32 files, this brief, and report.
