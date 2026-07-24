# Root Confirmation Task 2 Fix32 Report

## Result

DONE

Fix32 binds canonical persisted request projections to the authoritative outer
queue, analysis state, terminal confirmation, owner, journal, and action
semantics. The change remains offline, passive, read-only, seed-local, and
unable to feed results back to the Agent. It does not implement Task 3, add a
dependency, introduce a trust root, or touch unrelated untracked files.

## Requirement Source

- `.superpowers/sdd/root-confirmation-task-2-fix32-brief.md`
- `docs/superpowers/plans/2026-07-21-root-confirmation-stability-implementation.md`
  Global Constraints and Task 2
- `.superpowers/sdd/root-confirmation-task-2-fix31-report.md`
- Baseline: `51cc63d7260325ff7ecb3c62396ffb30e4c4d2dc`

## Root Cause

Fix31 made `factual_request_projection` canonical and bound its SHA-256 digest
to `semantic_identity`. On rejected snapshots, restore stopped there: a
coordinated projection plus identity rewrite remained internally
self-consistent even when it contradicted the outer queue request.

Ordinary terminal paths rebuilt the current request, while action validation
checked the terminal confirmation, owner, journal, and action copies against
one another. Neither path shared a graph-free comparison of projection facts
against the authoritative outer candidate, complete analysis-state
`DefectState`, recursive path, hypothesis id/hash, seed binding, and analysis
perspective. Existing multiset bijections rejected one-copy drift but could not
close this coordinated projection/identity versus outer-state gap.

## TDD Evidence

Focused RED, before production changes:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-red2 \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix32.py -v

Ran 16 tests in 1.054s
FAILED (failures=9)
```

The nine failures were the expected missing rejections for coordinated
perspective drift, six coordinated request-fact drifts, new terminal action
outer-path drift, and rejected replay outer-path drift. A passing
non-behavioral scope-constant assertion was removed during self-review, so the
final focused module contains 15 behavioral tests.

Final focused GREEN:

```text
PYTHONPATH=tools/trace_attribution \
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-green-final \
python3 -m unittest \
tools/trace_attribution/tests/test_root_confirmation_fix32.py -v

Ran 15 tests in 1.179s
OK
```

## Implementation

- Added one shared graph-free binding validator in `recursive_analyzer.py`.
- Reused the Fix31 canonical projection parser and projection identity helper.
- Compared projection facts to outer candidate, complete canonical
  `DefectState`, path, hypothesis id/hash, seed binding, and run perspective.
- Bound owner, terminal `RootConfirmation`, request identity, and action
  projection to those same facts.
- Invoked the validator for pending, validated, and `rejected_snapshot` queue
  validation, and before accepting a newly persisted or replayed terminal
  action.
- Kept rejected replay ahead of ordinary request reconstruction. The validator
  has no `TraceGraph` parameter and performs no graph request rebuild, artifact
  hydration or authorization, or Provider call.
- Preserved existing queue/journal/action/action-record bijections, which carry
  the binding check into checkpoint restore, report and evaluator validation,
  and replay.

## Version Decision

No persistence version changed. Fix32 adds validation to the existing exact
schemas without changing serialized fields, canonicalization, identity input,
or migration behavior:

- Request projection: `root-confirmation-request-projection/v2`
- Request identity: `confirmation_request:v3`
- Action state: `recursive-analysis-actions/v11`
- Report/evaluator: `recursive-attribution-report/v14`
- Checkpoint: `recursive-attribution-checkpoint/v13`
- Root confirmation: `recursive-root-confirmation/v15`
- Confirmation action projection: `action-projection/v5`

Compatible Fix31 payloads that satisfy the newly explicit cross-binding remain
valid; contradictory payloads now fail closed.

## Verification

```text
Fix17-Fix32:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-fix17-32-final \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..32}.py
  227 tests, OK

Affected confirmation/artifact/seed/checkpoint/report/evaluator/
replay/acceptance suites:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-affected-final \
  python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix{17..32}.py \
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
  666 tests, OK

Full discovery:
  PYTHONPATH=tools/trace_attribution \
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-full-final \
  python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
  960 tests, OK

Compile:
  PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix32-compile-final \
  python3 -m compileall -q tools/trace_attribution
  OK

Diff:
  git diff --check 51cc63d72..HEAD
  OK
```

## Changed Files

- `.superpowers/sdd/root-confirmation-task-2-fix32-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix32-report.md`
- `tools/trace_attribution/tests/test_root_confirmation_fix32.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

## Self-review

- Confirmed the validator consumes only persisted mappings, canonical
  `DefectState`, `RootConfirmation`, and `LocalStateOwner` values.
- Confirmed complete defect-state serialization, including fingerprint and
  `defect_state_id`, is compared to analysis-state authority.
- Confirmed pending, validated, and rejected terminal queues use the same
  binding check.
- Confirmed terminal persistence and both failed/completed replay acceptance
  flow through the same action binding gate.
- Confirmed rejected replay remains request-rebuild-free, Provider-free, and
  artifact-audit-preserving; no rejected artifact is hydrated or reauthorized.
- Confirmed valid rejected, ordinary terminal, and pending round trips remain
  accepted, and one failed seed does not affect the independent seed control.
- Confirmed no dependency, signature, secret, external state, Task 3 behavior,
  or Agent feedback path was added.
- Confirmed unrelated untracked files remain untouched and unstaged.

## Residual Risks

This is an internal consistency check, not cryptographic authenticity. It does
not promise detection if an attacker coherently rewrites every authoritative
projection, queue, analysis-state, owner, confirmation, journal, action, and
identity representation. Rejected artifact snapshots remain immutable audit
facts rather than newly authorized evidence.
