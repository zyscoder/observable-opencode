# Root Confirmation Task 2 Fix Wave 28 Report

Base commit: `b831bcd42`

Status: `DONE`

## Scope And Boundaries

Fix28 closes the three Important findings from the third cumulative Task 2
review. The implementation remains offline, passive, read-only, and unable to
feed attribution output back to the Agent. It adds no dependency and does not
implement Task 3 evidence expansion.

The original implementation subagent reached its usage limit after producing
the code and focused tests but before writing this report or committing. The
controller inspected the resulting diff and completed all focused, affected,
full-suite, compilation, and whitespace verification.

## Root-Cause Validation And Fixes

### 1. Confirmation replay used an incomplete request identity

The previous replay identity hashed only hypothesis, candidate, defect, and
seed identifiers. The actual Judge-visible request additionally contained the
defect payload, recursive path and envelopes, supporting/opposing evidence,
artifact facts, competitors, obligations, hypothesis semantic hash, and
analysis perspective. A restored queue could therefore mutate those facts and
reuse an old completed confirmation.

Fix28 moves the identity source to
`RootConfirmationRequest.factual_dict()`. The new
`root-confirmation-request-projection/v1` contains every Judge-visible fact,
and `confirmation_request:v2:<sha256>` hashes that canonical projection.
Pending queue entries persist both the projection and identity. Enqueue,
queue validation, started/completed/failed actions, replay, report/checkpoint
restore, and evaluator validation rebuild the request and require exact
projection and identity equality. A bit-identical request replays without
another provider call; any evidence, path, competitor, obligation, artifact,
semantic, or perspective change fails closed.

### 2. Malformed global terminal records were filtered before bijection

The previous validator selected only records already shaped like valid
completed/failed passes or projected failure episodes. An extra episode with
the same pass marker but no projection, or a pass with an unknown status, was
ignored before the cardinality check.

Fix28 classifies every journal or unresolved-branch record carrying a global
pass/failure marker before reconciliation. Completed pass, failed pass, and
failed episode each have an exact allowed schema. Unknown status, missing
projection, extra semantic fields, malformed owner, or contradictory identity
is rejected. Only after classification does validation enforce the strict
failed action/event/episode/seed-gap multiset bijection. The same classifier is
used by live state, checkpoint/report restoration, stale-seed quarantine, and
evaluator validation.

### 3. Typed global failures were not checkpointed immediately

The previous global pass wrote a pre-provider checkpoint. On provider or
validation failure it created the failed action, unresolved episode, request
accounting, and seed gap only in memory, then continued. A crash before a later
generic checkpoint could restore the pre-call state and invoke the provider
again.

Fix28 makes the canonical `fail_seed()` path finalize accounting, failed event,
failure projection, unresolved episode, seed gap, and rejected frontier state,
then immediately write
`global:failed:<canonical-pass-identity>`. Missing capability, capsule/request
failure, typed or generic provider failure, and invalid output all use this
path. Restore treats the pass as terminal. A crash injected immediately after
the failure checkpoint preserves the exact request count and failure facts,
does not call the failed seed again, and allows another seed to continue.

## TDD Evidence

The focused Fix28 test module covers:

- mutation of every Judge-visible confirmation fact changes the versioned
  identity;
- queue, started action, and terminal action use one request identity;
- report/evaluator reject obligation and competitor mutations;
- parallel or legacy identity fields are rejected;
- bit-identical checkpoint replay performs no confirmation provider call;
- unknown, incomplete, or extra global terminal records are rejected before
  bijection;
- stale-seed quarantine validates malformed terminal records before filtering;
- all enabled global failure branches write terminal checkpoints;
- crash-after-failure restore preserves accounting, seed gaps, and independent
  seed progress.

The usage-limit interruption left no durable console transcript from the
initial RED run. Inspection against base `b831bcd42` confirms the three tested
contracts were absent there: the four-field identity function, projection-
filtered episode reconciliation, and success-only `global:after` checkpoint
were the reviewed defects. The final focused suite is GREEN.

## Schema And Policy Decisions

- Root confirmation prompt: `recursive-root-confirmation-v9`
- Request projection: `root-confirmation-request-projection/v1`
- Request identity: `confirmation_request:v2`
- Root confirmation persistence:
  `recursive-root-confirmation/v12` with action projection v2 and request
  identity v2
- Global failure projection:
  `global-candidate-failure-projection/v3`
- Global persistence adds failure action v2 and terminal-record schema v1
- Action state: `recursive-analysis-actions/v8`
- Report: `recursive-attribution-report/v11`
- Checkpoint: `recursive-attribution-checkpoint/v10`

Older immediate schemas are rejected rather than silently reinterpreted. The
existing conservative v1/v2 report migration remains non-authoritative and
does not publish migrated roots.

## Verification

Focused Fix28:

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix28-status-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix28.py

Ran 12 tests - OK
```

Fix17-Fix28 plus seed, global Judge, causal Judge, checkpoint, and recursive
core regressions:

```text
PYTHONPYCACHEPREFIX=/tmp/fix28-core-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix17 \
  tools.trace_attribution.tests.test_root_confirmation_fix18 \
  tools.trace_attribution.tests.test_root_confirmation_fix19 \
  tools.trace_attribution.tests.test_root_confirmation_fix20 \
  tools.trace_attribution.tests.test_root_confirmation_fix21 \
  tools.trace_attribution.tests.test_root_confirmation_fix22 \
  tools.trace_attribution.tests.test_root_confirmation_fix23 \
  tools.trace_attribution.tests.test_root_confirmation_fix24 \
  tools.trace_attribution.tests.test_root_confirmation_fix25 \
  tools.trace_attribution.tests.test_root_confirmation_fix26 \
  tools.trace_attribution.tests.test_root_confirmation_fix27 \
  tools.trace_attribution.tests.test_root_confirmation_fix28 \
  tools.trace_attribution.tests.test_seed_attribution \
  tools.trace_attribution.tests.test_global_judge \
  tools.trace_attribution.tests.test_causal_judge \
  tools.trace_attribution.tests.test_causal_checkpoint \
  tools.trace_attribution.tests.test_recursive_analyzer

Ran 501 tests - OK
```

Report, evaluator, replay, artifact, and acceptance affected suites:

```text
PYTHONPYCACHEPREFIX=/tmp/fix28-affected-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_recursive_acceptance_review \
  tools.trace_attribution.tests.test_causal_state \
  tools.trace_attribution.tests.test_investigation \
  tools.trace_attribution.tests.test_recursive_benchmarks \
  tools.trace_attribution.tests.test_recursive_cli \
  tools.trace_attribution.tests.test_evidence_capsule \
  tools.trace_attribution.tests.test_causal_retrieval \
  tools.trace_attribution.tests.test_artifact_hydration \
  tools.trace_attribution.tests.test_evaluation_facts

Ran 235 tests - OK
```

Full attribution discovery:

```text
PYTHONPYCACHEPREFIX=/tmp/fix28-full-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'

Ran 897 tests - OK
```

Compilation and exact diff check:

```text
PYTHONPYCACHEPREFIX=/tmp/fix28-compile-pycache \
python3 -m compileall -q tools/trace_attribution
PASS

git diff --check b831bcd42
PASS
```

## Changed Files

Production and evaluator:

- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`

Focused and migrated regression tests:

- `tools/trace_attribution/tests/test_root_confirmation_fix28.py`
- affected Root Confirmation, causal Judge, checkpoint, seed, recursive
  analyzer, and acceptance test fixtures listed by `git diff --name-only`

Control documents:

- `.superpowers/sdd/root-confirmation-task-2-fix28-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix28-report.md`

## Residual Risks

No known Fix28 correctness issue remains after focused, affected, and full
regression. A fresh cumulative independent Task 2 review is still required
before Task 2 can be marked complete. Existing unrelated untracked benchmark
and design files were not modified, removed, or staged.
