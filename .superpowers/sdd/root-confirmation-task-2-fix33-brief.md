# Root Confirmation Task 2 Fix Wave 33 Brief

Base commit: `30abe17c8`

## Scope

Close the remaining Important authority-binding gap and the replay-order Minor
finding from the Fix32 task review. Keep the analysis offline, passive,
read-only, and unable to feed results back to the Agent. Do not implement
Task 3, add dependencies, or touch unrelated untracked files.

Use strict TDD. Record focused RED and GREEN, then run affected and full
attribution suites, `compileall`, and `git diff --check 30abe17c8..HEAD`.

## Required Fixes

### 1. Bind terminal hypothesis hash to ledger/frontier authority

Fix32 binds the factual request projection to its persisted queue, journal,
confirmation, and action copies. Those copies can still be coordinated around
a changed `hypothesis_semantic_hash` while the authoritative hypothesis ledger
and frontier item remain unchanged.

Extend the shared confirmation binding path so the persisted outer request and
projection must also match the authoritative hypothesis:

- when the owner has a frontier item, compare the entry and projection
  `hypothesis_semantic_hash` with both the frontier item's hash and the
  hypothesis ledger's `semantic_hash`;
- when the owner no longer has a frontier item, compare against the ledger
  hypothesis directly;
- retain exact hypothesis id, seed binding, candidate, active defect, owner
  occurrence, path, and perspective checks;
- reject missing, contradictory, ambiguous, or stale authority.

The check must run for pending, validated, and `rejected_snapshot` state and
must flow through checkpoint, report/evaluator, terminal action, and replay
validation. It must remain graph-request-rebuild-free and Provider-free for a
rejected snapshot.

### 2. Validate completed replay before applying Provider state

On completed confirmation replay, validate the action projection and terminal
request binding before calling `_apply_provider_result_state()`. If replay
validation fails, no provider circuit/accounting state may be modified.

Preserve the existing successful replay accounting behavior after validation.

## Tests

Create `tools/trace_attribution/tests/test_root_confirmation_fix33.py` with at
least:

1. coordinated rejected queue/projection/confirmation/journal/action hash and
   identity rewrite while ledger/frontier authority remains unchanged; report
   and evaluator validation must reject;
2. the same authority mismatch through checkpoint restore and replay;
3. pending and validated terminal hypothesis-hash drift controls;
4. owner without a live frontier item still binds to the ledger hypothesis;
5. completed replay with an invalid terminal binding raises before provider
   state changes;
6. valid rejected, pending, validated, and completed replay round trips remain
   accepted and preserve provider accounting.

Use real serialization and replay paths. Do not claim detection if every
authoritative representation is coherently rewritten.

## Verification

At minimum:

1. New Fix33 focused tests.
2. Fix17-Fix33 plus confirmation, artifact, seed, checkpoint,
   report/evaluator/replay/acceptance regressions.
3. Directly affected suites.
4. Full `tools/trace_attribution/tests` discovery.
5. `python3 -m compileall -q tools/trace_attribution`.
6. `git diff --check 30abe17c8..HEAD`.

## Deliverables

- Production fixes and focused tests.
- `.superpowers/sdd/root-confirmation-task-2-fix33-report.md` with root-cause
  validation, RED/GREEN evidence, exact commands/counts, changed files,
  version decisions, self-review, and residual risks.
- One commit containing only Fix33 files, this brief, and report.
