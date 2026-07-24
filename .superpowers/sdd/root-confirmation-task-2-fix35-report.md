# Root Confirmation Task 2 Fix35 Report

## Result

DONE

Fix35 binds all six canonical Provider accounting fields to the projected
post-replay state and canonicalizes both cache-stat forms emitted by this
codebase. Checkpoint restore and replay now reject re-signed cache or accounting
drift before live accounting, Provider/circuit state, queue, journal, or
terminal state changes. Fix34 replay ordering, Fix33 authority binding, and
Provider-free rejected replay remain unchanged. No dependency or Task 3
behavior was added.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix35-brief.md`
- `docs/superpowers/plans/2026-07-21-root-confirmation-stability-implementation.md`
  Global Constraints and Task 2
- `.superpowers/sdd/root-confirmation-task-2-fix34-report.md`
- Baseline: `b59431810d8b860053e0c6cc09a468b06ec379c4`

## Root Cause Validation

Fix34 projected completed replay's physical accounting before Provider
validation, but called `_validate_provider_state()` with accounting equality
disabled and then compared only `judge_requests` and
`judge_request_uncertainty_count`. A type-valid, nonnegative,
identity-re-signed drift in `logical_judge_calls`,
`logical_confirmation_calls`, `investigation_rounds`, or `artifact_bytes`
therefore passed preflight and was applied as Provider state.

The shared Provider validator only required `cache_stats` to be a mapping.
Missing or extra keys, wrong scalar/container types, negative counts,
inconsistent write-error counts, and contradictory enabled/path values all
survived identity recomputation. Because checkpoint restore, ordinary replay,
and completed replay already converge on `_validate_provider_state()`, the
missing canonical cache validator at that boundary was the common root cause.

The focused RED reproduced both defects. All four nonphysical accounting
drifts completed instead of raising, while the physical and uncertainty
controls reached only the old two-field check. Every malformed cache-stat
shape was accepted, including checkpoint restore and completed replay.

## TDD Evidence

Focused RED before production changes:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-red \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix35.py -v

Ran 6 tests in 0.177s
FAILED (failures=19)
```

Focused GREEN after the minimal implementation:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-green \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix35.py -v

Ran 6 tests in 0.200s
OK
```

## Implementation

- Defines the exact full `JudgmentCache.stats()` key set and its seven
  nonnegative exact-int counters.
- Accepts the minimal disabled form only as exactly `{"enabled": false}`.
- Requires full-form `enabled` to be an exact bool, `path` an exact string,
  every counter an exact nonnegative int, and `write_errors` an exact list of
  strings.
- Requires `write_error_count == len(write_errors)`.
- Requires an enabled full cache to have a nonempty path and a disabled full
  cache to have an empty path.
- Rejects non-string keys, missing/extra keys, wrong types, negative counts,
  inconsistent errors, and noncanonical enabled/path combinations.
- Returns a canonical deep copy and installs it into the canonical Provider
  payload before Provider identity comparison.
- Uses normal `_validate_provider_state()` accounting equality against the
  projected replay state, replacing the partial physical-field comparison.
- Preserves successful replay order: physical/uncertainty accounting,
  prevalidated Provider/circuit application, then prevalidated terminal
  recording.
- Updates the Fix33 valid replay fixture to model the already-debited
  nonphysical snapshot state instead of replaying a completed action from an
  inconsistent fresh state.

## Controls

The Fix35 module covers:

1. identity-re-signed drift in each of all six accounting fields, with complete
   state/circuit equality after rejection;
2. minimal disabled, full enabled, and full disabled canonical cache stats;
3. missing/extra keys; wrong bool/string/int/list and list-item types; negative
   counts; inconsistent error count; and invalid enabled/path combinations;
4. re-signed malformed cache stats through checkpoint restore and completed
   replay, with replay atomicity;
5. valid full enabled cache stats through nonzero checkpoint restore and
   nonzero completed replay.

Fix17-Fix35 retain Fix33 authority binding, Fix34 replay ordering, and rejected
Provider-free replay controls.

## Version Decision

No persistence version changed. Fix35 strengthens validation of existing
Provider state fields without changing serialized fields, identities,
migration behavior, or accepted output from `JudgmentCache.stats()`:

- Request projection: `root-confirmation-request-projection/v2`
- Request identity: `confirmation_request:v3`
- Action state: `recursive-analysis-actions/v11`
- Report/evaluator: `recursive-attribution-report/v14`
- Checkpoint: `recursive-attribution-checkpoint/v13`
- Root confirmation: `recursive-root-confirmation/v15`
- Confirmation action projection: `action-projection/v5`
- Provider state: `recursive-provider-state/v1`

## Verification

```text
Focused Fix35:
  6 tests, OK

Fix17-Fix35:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-fix17-35-rerun \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..35}.py
  245 tests, OK

Affected Provider/accounting/cache/checkpoint/confirmation/report/evaluator/
replay/acceptance suites:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-affected \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..35}.py \
  tools/trace_attribution/tests/test_backward_taint.py \
  tools/trace_attribution/tests/test_global_judge.py \
  tools/trace_attribution/tests/test_causal_judge.py \
  tools/trace_attribution/tests/test_confirmation_path.py \
  tools/trace_attribution/tests/test_artifact_hydration.py \
  tools/trace_attribution/tests/test_seed_attribution.py \
  tools/trace_attribution/tests/test_causal_checkpoint.py \
  tools/trace_attribution/tests/test_causal_state.py \
  tools/trace_attribution/tests/test_recursive_analyzer.py \
  tools/trace_attribution/tests/test_recursive_benchmarks.py \
  tools/trace_attribution/tests/test_recursive_acceptance_review.py \
  tools/trace_attribution/tests/test_recursive_cli.py \
  tools/trace_attribution/tests/test_evaluation_facts.py
  843 tests, OK

Full discovery:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-full \
  python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
  978 tests, OK

Compile:
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix35-compile \
  python3 -m compileall -q tools/trace_attribution
  OK

Working-tree diff:
  git diff --check
  OK

Committed-range diff:
  git diff --check b59431810..HEAD
  OK
```

## Changed Files

- `.superpowers/sdd/root-confirmation-task-2-fix35-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix35-report.md`
- `tools/trace_attribution/tests/test_root_confirmation_fix33.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix35.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

## Self-review

- Confirmed completed replay compares all six Provider accounting fields to the
  projected state before any replay mutation.
- Confirmed cache validation is centralized in `_validate_provider_state()` and
  therefore shared by checkpoint restore, ordinary replay, and completed replay.
- Confirmed Provider identity is checked against the canonical cache-stat copy.
- Confirmed post-preflight replay consumes only the canonical Provider payload.
- Confirmed logical, investigation, and artifact counters are not replayed.
- Confirmed physical/uncertainty accounting, Provider/circuit application, and
  terminal recording retain Fix34 order.
- Confirmed rejected replay remains request-rebuild-free and Provider-free.
- Confirmed no graph mutation, Agent feedback path, dependency, Task 3
  behavior, schema bump, signature, secret, or external trust root was added.
- Confirmed unrelated untracked files remain untouched and will not be staged.

## Residual Risks

Provider payload identity remains an internal consistency hash rather than a
cryptographic trust boundary. A coherent rewrite of every persisted authority
remains outside this contract.
