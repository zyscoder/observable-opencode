# Root Confirmation Task 2 Fix33 Report

## Result

DONE

Fix33 binds every persisted terminal hypothesis hash to the hypothesis ledger
and, when present, the exact frontier item. Completed confirmation replay now
validates its action projection and request binding before applying Provider
state. The change remains offline, passive, read-only, seed-local, and unable
to feed results back to the Agent. It does not implement Task 3 or add a
dependency.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix33-brief.md`
- `docs/superpowers/plans/2026-07-21-root-confirmation-stability-implementation.md`
  Global Constraints and Task 2
- `.superpowers/sdd/root-confirmation-task-2-fix32-report.md`
- Baseline: `30abe17c8652c813c88d68c50d15ca3e5a8e4e61`

## Root Cause

Fix32's graph-independent request projection validator compared queue,
projection, confirmation, journal, and action copies, but accepted their
`hypothesis_semantic_hash` as self-authoritative. A coordinated rewrite of all
those copies and their derived request/response identities therefore remained
valid while the hypothesis ledger and frontier retained the original hash.

The completed replay path parsed the terminal action and checked the rebuilt
request identity, but deferred the complete terminal action/request binding
check to `_record_confirmation()`. `_apply_provider_result_state()` ran before
that check, so an invalid action could update persisted Provider accounting and
circuit state before raising.

## TDD Evidence

Focused RED, after correcting test-only fixture construction and before any
production change:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-red2 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix33.py -v

Ran 7 tests in 0.237s
FAILED (failures=6)
```

The six expected failures were coordinated rejected hash drift through
report/evaluator, checkpoint, and replay; validated terminal drift with and
without a live frontier item; and Provider state mutation before invalid
completed replay failed. The valid round-trip control passed during RED.

Focused GREEN:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-green1 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix33.py -v

Ran 7 tests in 0.224s
OK
```

## Implementation

- Extended the shared graph-independent confirmation binding validator with
  the current `HypothesisLedger` and `RecursiveFrontier`.
- Required hypothesis id, semantic hash, candidate root, active defect
  fingerprint, and seed binding to match the ledger.
- When the owner's frontier visit exists, required those same facts to match
  the exact frontier item and required the canonical `confirmation_queue`
  owner occurrence.
- When the frontier visit no longer exists, reconstructed the canonical owner
  visit from ledger authority and required an exact occurrence match.
- Kept existing projection/outer request, recursive path, perspective,
  confirmation, and action checks in the same shared path.
- Invoked the authority check for pending, validated, and rejected snapshot
  queue state and terminal action validation, which carries it through
  checkpoint, report/evaluator, and replay validation.
- Moved completed replay's full terminal validation immediately after action
  parsing and before replay accounting or `_apply_provider_result_state()`.
- Corrected two pre-existing queue-bound test fixtures to use canonical owner
  visits. Their arbitrary owner strings had never represented valid restored
  confirmation ownership.

Rejected snapshot validation still consumes only persisted mappings, ledger,
frontier, canonical defect state, owner, and confirmation values. It does not
rebuild a graph request, hydrate or authorize artifacts, or call a Provider.

## Version Decision

No persistence version changed. Fix33 adds validation and reorders replay
application without changing serialized fields, canonicalization, identity
inputs, or migration behavior:

- Request projection: `root-confirmation-request-projection/v2`
- Request identity: `confirmation_request:v3`
- Action state: `recursive-analysis-actions/v11`
- Report/evaluator: `recursive-attribution-report/v14`
- Checkpoint: `recursive-attribution-checkpoint/v13`
- Root confirmation: `recursive-root-confirmation/v15`
- Confirmation action projection: `action-projection/v5`

Valid Fix32 payloads remain valid. Payloads whose terminal hypothesis facts do
not match ledger/frontier authority now fail closed.

## Verification

```text
Focused Fix32/Fix33 plus confirmation/artifact/checkpoint:
  98 tests, OK

Fix17-Fix33:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-fix17-33 \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..33}.py
  234 tests, OK

Affected confirmation/artifact/seed/checkpoint/report/evaluator/
replay/acceptance suites:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-affected2 \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..33}.py \
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
  673 tests, OK

Full discovery:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-full \
  python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
  967 tests, OK

Compile:
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix33-compile \
  python3 -m compileall -q tools/trace_attribution
  OK

Working-tree diff:
  git diff --check
  OK

Committed-range diff:
  git diff --check 30abe17c8..HEAD
  OK
```

## Changed Files

- `.superpowers/sdd/root-confirmation-task-2-fix33-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix33-report.md`
- `tools/trace_attribution/tests/test_root_confirmation_fix33.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

## Self-review

- Confirmed both frontier-present and frontier-absent authority paths are
  covered.
- Confirmed the ledger and frontier hashes remain unchanged in coordinated
  rejected tampering tests; all derived copies and identities are rewritten.
- Confirmed pending and validated terminal hash drift fail closed.
- Confirmed completed replay validates before Provider state or accounting
  changes, while valid replay restores the persisted Provider state.
- Confirmed valid pending, validated/completed, rejected, and completed replay
  round trips remain accepted.
- Confirmed rejected replay remains request-rebuild-free and Provider-free;
  existing artifact preflight regressions retain hydration/authorization
  coverage.
- Confirmed no graph mutation, Agent feedback path, dependency, Task 3
  behavior, schema bump, signature, secret, or external trust root was added.
- Confirmed unrelated untracked files remain untouched and will not be staged.

## Residual Risks

This remains an internal consistency check, not cryptographic authenticity. A
coherent rewrite of every persisted authority, including the hypothesis ledger
and frontier, is outside this contract. Rejected artifact snapshots remain
immutable audit facts rather than newly authorized evidence.
