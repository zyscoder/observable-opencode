# Root Confirmation Task 2 Fix30 Report

## Result

DONE

Fix30 closes both Task 2 persistence gaps from the brief while preserving the
offline, passive, read-only Agent boundary. It does not implement Task 3, add a
dependency, or modify unrelated untracked files.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix30-brief.md`
- Root stability plan Global Constraints and Task 2
- Baseline: `46d5021acb638381f6830e523ae3ed86fb2c400c`

## TDD Evidence

Initial focused RED:

```text
python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix30.py -v
Ran 10 tests
FAILED (failures=9, errors=9)
```

The failures demonstrated that marker-stripped global terminal records passed
direct, live, checkpoint, report/evaluator, and stale-quarantine boundaries;
artifact owner/hash/path/content/missing drift was revalidated as ordinary
evidence or reached the confirmation Provider; and the required disposition
and persistence versions did not exist.

A second RED was added during self-review for the crash window where a durable
`confirmation_failed` action exists after the last state snapshot:

```text
Ran 11 tests
FAILED (failures=1)
```

It proved restore rebuilt an ordinary confirmation request before replaying the
durable rejected snapshot. The replay path was then moved ahead of artifact
preflight and request construction.

Final focused GREEN:

```text
Ran 11 tests in 0.530s
OK
```

## Implementation

### Ledger-aware residual global terminals

- Added one shared residual signature for pass and failure-episode records.
- The signature uses an explicit seed authority map derived from the seed
  ledger/report seeds.
- Explicit markers, canonical or partial global owners, and ledger-bound
  residual completed/failed shapes are recognized before filtering.
- Ordinary investigation and unresolved records remain non-global controls.
- The classifier now runs before live terminal derivation, checkpoint restore
  filtering, stale-seed quarantine, report restore/validation, and evaluator
  acceptance.

### Rejected artifact snapshots

- Added `validated|rejected_snapshot` terminal evidence disposition.
- The exact enqueue-time artifact envelope array remains the audit snapshot and
  receives a stable snapshot identity.
- Fresh graph comparison facts and rejection reasons are stored separately.
- A rejected snapshot can terminate only as one seed-local
  `confirmation_failed` action containing an `unknown`, no-evidence
  confirmation.
- Rejected snapshots cannot produce confirmed/rejected substantive verdicts,
  decisive evidence, supporting evidence, or Provider calls.
- Restore round-trips the durable action and snapshot without rebuilding a
  normal request or rerunning ordinary envelope validation.
- A durable rejected action after the last state snapshot is replayed before
  preflight, preventing duplicate terminal actions.
- Other queued seeds continue independently.

### Versioned contracts

- Action state: `recursive-analysis-actions/v10`
- Report/evaluator: `recursive-attribution-report/v13`
- Checkpoint: `recursive-attribution-checkpoint/v12`
- Global terminal records: `terminal-record-schema/v3`
- Root confirmation: `recursive-root-confirmation/v14`
- Terminal evidence: `terminal-evidence/v2`
- Confirmation action projection: `action-projection/v4`

## Verification

All commands used isolated `PYTHONPYCACHEPREFIX` locations and no network.

```text
Focused Fix30:                         11 tests, OK
Root confirmation Fix17-Fix30:       196 tests, OK
Directly affected suite:             385 tests, OK
Full unittest discovery:             929 tests, OK
python3 -m compileall -q:                 OK
git diff --check 46d5021ac:               OK
```

## Self-review

- Confirmed residual classification receives ledger authority at every required
  pre-filter boundary.
- Confirmed evaluator audit-only handling occurs only after strict report and
  action reconciliation; any support or decisive use removes the exemption.
- Confirmed validated terminal evidence retains current graph/envelope checks.
- Confirmed rejected snapshots preserve the original envelope bytes, have no
  evidence refs, and produce exactly one failed action per affected seed.
- Confirmed crash recovery performs no normal request rebuild and no Provider
  replay for a durable rejected snapshot.
- Confirmed the worktree remains on the requested baseline and unrelated
  untracked files are untouched.

## Concerns

None. The explicit schema and policy version bumps intentionally reject older
current-contract snapshots instead of silently accepting them under Fix30
semantics.
