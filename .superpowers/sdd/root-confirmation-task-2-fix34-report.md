# Root Confirmation Task 2 Fix34 Report

## Result

DONE

Fix34 makes completed confirmation replay atomic across action, binding,
terminal, physical accounting, Provider state, circuit state, and terminal
recording. It prevalidates and caches the canonical Provider payload before
any replay mutation, then applies physical accounting, Provider/circuit state,
and the terminal confirmation exactly once. The change preserves Fix33
ledger/frontier authority, rejected Provider-free replay, and the offline,
passive, read-only boundary. It does not implement Task 3 or add a dependency.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix34-brief.md`
- `docs/superpowers/plans/2026-07-21-root-confirmation-stability-implementation.md`
  Global Constraints and Task 2
- `.superpowers/sdd/root-confirmation-task-2-fix33-report.md`
- Baseline: `676391224a93c4e522fcf17a7afe5ff0f80c0864`

## Root Cause Validation

Fix33 moved completed replay's action projection and terminal binding
validation ahead of `_apply_provider_result_state()`, but the Provider payload
itself was still validated inside that apply method. The replay path first
updated `judge_requests` or `judge_request_uncertainty_count`, then called the
Provider validator. An invalid threshold, cache identity, accounting shape,
or Provider identity therefore raised after analysis accounting had already
changed.

The same path called `_record_confirmation()` after accounting and Provider
mutation. That method repeated the complete terminal validator, leaving
another validator capable of raising after mutation despite Fix33's earlier
preflight.

The focused RED reproduced the first issue with a nonzero physical delta:
threshold, cache, and accounting failures each changed `judge_requests` from
5 to 8 before raising. The test compares complete frontier, hypothesis, and
action checkpoint payloads plus all transport circuit fields before and after
the failed replay.

## TDD Evidence

Focused RED after test-fixture invariants were corrected and before production
changes:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-red3 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix34.py -v

Ran 5 tests in 0.172s
FAILED (failures=3)
```

The three failures were the invalid Provider threshold, cache identity, and
accounting subcases. The invalid terminal atomicity, nonzero success,
checkpoint/report JSON round-trip, and rejected Provider-free controls already
passed.

Focused GREEN:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-green1 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix34.py -v

Ran 5 tests in 0.155s
OK
```

## Implementation

- Reuses the canonical confirmation action projection as the physical
  accounting authority instead of coercing persisted values with `int()` or
  `bool()`.
- Projects exact physical request debit or uncertainty accounting on a shallow
  state copy before touching the live state.
- Rejects a projected negative physical request count.
- Validates the full persisted Provider schema, exact keys, circuit types and
  threshold, cache identity, cache stats shape, accounting shape and types,
  and payload identity during preflight.
- Binds Provider `judge_requests` and
  `judge_request_uncertainty_count` to the projected replay result. Logical
  counters remain non-replayed, preserving Fix33 behavior.
- Caches the canonical deep-copied Provider payload returned by preflight.
- Splits Provider mutation from Provider validation so completed replay applies
  only the already validated canonical payload.
- Splits terminal recording from terminal validation so completed replay
  records only the already validated canonical action projection.
- Preserves the required successful order: physical/uncertainty accounting,
  Provider/circuit application, then terminal confirmation recording.

## Controls

The Fix34 module covers:

1. invalid Provider threshold, cache identity, and accounting with nonzero
   physical delta, with complete state and circuit equality after failure;
2. invalid terminal binding with nonzero physical delta, with the same
   atomicity assertion;
3. valid nonzero completed replay with exact physical, logical, uncertainty,
   Provider, and circuit assertions, followed by a second no-op replay;
4. a real durable checkpoint JSON journal restore and report JSON file
   serialization/restore for a queued pending confirmation;
5. rejected snapshot replay without Provider payload, request rebuild, or
   Provider call.

Fix17-Fix34 retain all Fix33 ledger/frontier authority controls.

## Version Decision

No persistence version changed. Fix34 changes validation/application order and
strengthens replay consistency without changing serialized fields,
canonicalization, identities, or migration behavior:

- Request projection: `root-confirmation-request-projection/v2`
- Request identity: `confirmation_request:v3`
- Action state: `recursive-analysis-actions/v11`
- Report/evaluator: `recursive-attribution-report/v14`
- Checkpoint: `recursive-attribution-checkpoint/v13`
- Root confirmation: `recursive-root-confirmation/v15`
- Confirmation action projection: `action-projection/v5`

## Verification

```text
Focused Fix33/Fix34 plus Provider-accounting/checkpoint neighbors:
  88 tests, OK

Fix17-Fix34:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-fix17-34 \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..34}.py
  239 tests, OK

Affected confirmation/provider-accounting/checkpoint/report/evaluator/
replay/acceptance suites:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-affected \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..34}.py \
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
  678 tests, OK

Full discovery:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-full \
  python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
  972 tests, OK

Compile:
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix34-compile \
  python3 -m compileall -q tools/trace_attribution
  OK

Working-tree diff:
  git diff --check
  OK

Committed-range diff:
  git diff --check 676391224..HEAD
  OK
```

## Changed Files

- `.superpowers/sdd/root-confirmation-task-2-fix34-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix34-report.md`
- `tools/trace_attribution/tests/test_root_confirmation_fix34.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

## Self-review

- Confirmed every completed replay validator runs before live analysis or
  transport mutation.
- Confirmed the post-accounting Provider path consumes only the cached
  canonical Provider payload.
- Confirmed the post-Provider terminal path consumes only the cached canonical
  action projection.
- Confirmed failed Provider and terminal preflight preserve physical, logical,
  uncertainty, Provider, circuit, queue, journal, frontier, and ledger state.
- Confirmed valid nonzero physical delta and persisted circuit state apply
  once, while logical counters are not replayed.
- Confirmed the pending control traverses durable checkpoint JSON journals and
  a report JSON file rather than only round-tripping one queue mapping.
- Confirmed rejected replay remains graph-request-rebuild-free and
  Provider-free.
- Confirmed no graph mutation, Agent feedback path, dependency, Task 3
  behavior, schema bump, signature, secret, or external trust root was added.
- Confirmed unrelated untracked files remain untouched and are excluded from
  staging.

## Residual Risks

Provider payload identity remains an internal consistency hash, not a
cryptographic trust boundary. As in Fix33, a coherent rewrite of every
persisted authority is outside this contract.
