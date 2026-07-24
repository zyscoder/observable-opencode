# Root Confirmation Task 2 Fix31 Report

## Result

DONE

Fix31 closes the two Important findings in the brief while preserving the
offline, passive, read-only Agent boundary. It does not implement Task 3, add
dependencies, or touch unrelated untracked files.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix31-brief.md`
- `docs/superpowers/plans/2026-07-21-root-confirmation-stability-implementation.md`
  Global Constraints and Task 2
- `.superpowers/sdd/root-confirmation-task-2-fix30-report.md`
- Baseline: `ca2ffd027bfd1d7456b5b144e599a906fc0d23d6`

## Root Causes

### Partial ledger authority

`_global_terminal_residual_signature()` required either a top-level seed
binding or the complete `seed_ref + defect_fingerprint` pair for pass records,
and the complete `node_ref + defect_state_id` pair for failure episodes.
Marker-stripped terminal records therefore became invisible when one authority
field was removed, even though the surviving terminal structure and seed
ledger identified one unique seed.

### Unbound rejected factual projection

Rejected snapshots checked only that `semantic_identity` had the expected
prefix. Their persisted `factual_request_projection` was not directly
canonicalized and hashed, and the projection was absent from confirmation
journal and terminal action projections. Queue, journal, action, checkpoint,
report, evaluator, and replay bijections therefore could not detect factual
projection drift without rebuilding a normal request from the current graph.

## TDD Evidence

Finding 1 RED, after correcting a test-fixture `KeyError`:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-red1b \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix31.py -v

Ran 5 tests in 0.153s
FAILED (failures=8)
```

Finding 1 GREEN:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-green1 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix31.py -v

Ran 5 tests in 0.079s
OK
```

Finding 2 RED, with no test errors:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-red2b \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix31.py -v

Ran 20 tests in 1.121s
FAILED (failures=10)
```

The RED discovery included four inherited Fix30 controls. The final Fix31
module references the Fix30 class through its module, so those controls are
exercised only through the Fix31 subclass and final focused discovery is 16.

Finding 2 final GREEN:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-green2d \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix31.py -v

Ran 16 tests in 0.956s
OK
```

## Implementation

### Ledger-aware residual terminals

- The existing classifier now establishes a distinctive pass or failure
  episode structure before consulting authority.
- Every surviving authority claim is resolved against the seed ledger and the
  claim sets must intersect at exactly one seed.
- Completed and failed pass records remain recognizable without
  `defect_fingerprint`; failure episodes remain recognizable without
  `defect_state_id`.
- Unknown, contradictory, and ambiguous authority fails closed.
- Ordinary investigation and unresolved controls, including controls that
  reference a known seed, remain non-global without the terminal structure.
- The shared classifier continues to protect live derivation, checkpoint
  restore, report/evaluator validation, and stale-seed quarantine.

### Projection identity binding

- Added a canonical persisted projection validator that requires the exact
  version and exact factual request field set, validates all field types and
  canonical defect identity, and returns canonical facts without graph access.
- Live `RootConfirmationRequest` and persisted projections now use the same
  projection identity function.
- Rejected restore derives identity directly from the persisted projection and
  compares it with `semantic_identity` before replay.
- `factual_request_projection` is required in the canonical terminal action
  projection and copied into the confirmation journal.
- Existing queue/journal/action/checkpoint/report/evaluator/replay multiset
  bijections now include the exact projection.
- Rejected artifact content remains audit-only: no artifact reauthorization,
  ordinary request rebuild, substantive verdict, decisive evidence, or
  confirmation Provider call occurs.

## Version Changes

- Request projection: `root-confirmation-request-projection/v2`
- Request identity: `confirmation_request:v3`
- Action state: `recursive-analysis-actions/v11`
- Report/evaluator: `recursive-attribution-report/v14`
- Checkpoint: `recursive-attribution-checkpoint/v13`
- Root confirmation: `recursive-root-confirmation/v15`
- Confirmation action projection: `action-projection/v5`

## Verification

```text
Focused Fix31:
  16 tests, OK

Fix17-Fix31:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-fix17-31c \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..31}.py
  212 tests, OK

Affected global terminal/confirmation/artifact/seed/checkpoint/
report/evaluator/replay/acceptance suites:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-affected \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..31}.py \
  tools/trace_attribution/tests/test_global_judge.py \
  tools/trace_attribution/tests/test_causal_judge.py \
  tools/trace_attribution/tests/test_confirmation_path.py \
  tools/trace_attribution/tests/test_artifact_hydration.py \
  tools/trace_attribution/tests/test_seed_attribution.py \
  tools/trace_attribution/tests/test_causal_checkpoint.py \
  tools/trace_attribution/tests/test_recursive_analyzer.py \
  tools/trace_attribution/tests/test_recursive_benchmarks.py \
  tools/trace_attribution/tests/test_recursive_acceptance_review.py \
  tools/trace_attribution/tests/test_recursive_cli.py \
  tools/trace_attribution/tests/test_evaluation_facts.py
  651 tests, OK

Full discovery:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-full \
  python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
  945 tests, OK

Compile:
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix31-compile \
  python3 -m compileall -q tools/trace_attribution
  OK

Diff:
  git diff --check ca2ffd027..HEAD
  OK
```

## Changed Files

- `.superpowers/sdd/root-confirmation-task-2-fix31-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix31-report.md`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix19.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix23.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix24.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix26.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix27.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix28.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix29.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix30.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix31.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

## Self-review

- Confirmed the residual classifier receives seed-ledger authority at every
  required pre-filter boundary and checks structure before authority.
- Confirmed contradictory or ambiguous authority raises instead of choosing or
  filtering a seed.
- Confirmed persisted projection validation has no `TraceGraph` input and does
  not call `_build_confirmation_request()`.
- Confirmed rejected replay validates projection identity before replay and
  remains Provider-free and seed-local.
- Confirmed validated ordinary terminal confirmations still round-trip and
  retain active-revision and non-temporal evidence checks.
- Confirmed no dependency, Task 3 behavior, or Agent feedback path was added.
- Confirmed unrelated untracked files remain untouched and unstaged.

## Residual Risks

Older report/checkpoint/action contracts are intentionally rejected under the
new explicit versions instead of being silently reinterpreted. No other known
residual risk remains in Fix31 scope.
