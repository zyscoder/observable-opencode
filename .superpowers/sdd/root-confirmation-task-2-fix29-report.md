# Root Confirmation Task 2 Fix Wave 29 Report

Base commit: `2b05451f7`

Status: `DONE`

## Scope And Boundaries

Fix29 closes the three Important findings in the fourth cumulative Task 2
review. The attribution path remains offline, passive, read-only, and unable to
feed analysis results back to the Agent. The change adds no dependency and
does not implement Task 3 evidence expansion.

## Root-Cause Validation And Fixes

### 1. `no_defect` did not close every open authored candidate

The previous validator required only a decisive absent exculpatory or outcome
assessment. Other open authored root-eligible candidates could retain unknown
input/output status, an unknown role, or no resolved candidate-to-seed path,
while the seed was still marked no-defect.

The v7 Global Judge contract now requires exact assessment coverage for every
open authored root-eligible candidate. Each must have absent defect/output
status, known input and role, a complete grounded path, and a decidable
counterfactual. A present open candidate cannot be hidden as outcome evidence.
Insufficient responses fail schema validation, so the existing adapter repair
path must return `needs_expansion` or `inconclusive`; analyzer-level invalid
output becomes a seed evidence gap.

### 2. Global terminal records had split marker and owner checks

Investigation records and unresolved episodes previously used different marker
tests. Owner validation checked only the persisted seed binding in important
paths, so coordinated seed/pass/owner substitutions and kind-only episodes
could escape validation before stale-state filtering.

Fix29 adds one shared global terminal marker predicate and applies the exact
record classifiers before live terminal-set derivation, checkpoint restore,
report validation, evaluator reconciliation, and stale-seed quarantine.
Completed and failed passes now reconstruct the seed binding from `seed_ref`
and `defect_fingerprint`, reconstruct the pass identity, and compare the full
canonical owner including hypothesis, visit, occurrence, and seed fields.
Exact schemas reject kind-only, incomplete, unknown-status, extra-field, and
substituted-owner records before any stale record is removed.

### 3. Terminal confirmation discarded explicit artifact ownership

The previous terminal path checkpointed a completed action and then called a
generic ownerless artifact eligibility probe. A valid artifact with multiple
active owners was rejected despite the queue already holding an exact
candidate-owned envelope, and the completed action could remain durable.

Terminal validation now reuses the queue/action artifact envelopes. Every
artifact evidence reference must map to one persisted canonical envelope, and
the graph rebuilds that envelope with its persisted `expected_owner_ref` and
requires exact hash, range, path, provenance, and owner equality. Node evidence
keeps normal active-revision validation. Candidate, path, owner, request,
projection, and artifact validation all finish before a completed or failed
checkpoint action is written.

If provider-returned artifact evidence fails this validation, the analyzer
restores the queued envelope snapshot, records a canonical unknown
`terminal_artifact_evidence_invalid` result and only a failed action for that
seed, then continues other seeds. A valid multi-active-owner artifact completes
with its explicit candidate owner and survives checkpoint, report, and
evaluator round trips.

## TDD Evidence

### Fix 1 RED and GREEN

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-red1-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix29.py -v

Ran 10 tests - FAILED (failures=8)
```

The failures covered unknown output/input/role, missing open assessment,
present output hidden as outcome evidence, unresolved path, real adapter
repair, and analyzer seed outcome. Missing-counterfactual and the already valid
closed case passed through existing lower-level validation.

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-green1-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix29.py -v

Ran 10 tests - OK
```

### Fix 2 RED and GREEN

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-red2b-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.CanonicalGlobalTerminalClassifierTest \
  -v

Ran 6 tests - FAILED (failures=7 subtests)
```

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-green2-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.CanonicalGlobalTerminalClassifierTest \
  -v

Ran 6 tests - OK
```

### Fix 3 RED and GREEN

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-red3-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.ExplicitArtifactOwnerTerminalValidationTest \
  -v

Ran 3 tests - FAILED (errors=7 subtests)
```

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-green3d-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.ExplicitArtifactOwnerTerminalValidationTest \
  -v

Ran 3 tests - OK
```

### Version Boundary RED and GREEN

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-red4-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.Fix29PersistenceVersionTest \
  -v

Ran 2 tests - FAILED (failures=2)
```

```text
PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix29-green4-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools.trace_attribution.tests.test_root_confirmation_fix29.Fix29PersistenceVersionTest \
  -v

Ran 2 tests - OK
```

## Schema And Policy Decisions

- Global Judge prompt and judgment: `global-candidate-judgment/v7`
- Global validation envelope: `global-candidate-validation-envelope/v7`
- Global failure projection: `global-candidate-failure-projection/v4`
- Global persistence: failure action v3 and terminal-record schema v2
- Root confirmation persistence: v13 with terminal evidence v1 and action
  projection v3
- Action state: `recursive-analysis-actions/v9`
- Report and evaluator: `recursive-attribution-report/v12`
- Checkpoint: `recursive-attribution-checkpoint/v11`
- Evidence policy and canonical artifact-owner envelope remain unchanged

Immediate old v6 Global envelopes/judgments, v11 reports, v8 actions, and v10
checkpoints are rejected rather than silently reinterpreted. The existing
conservative v1/v2 report migration remains non-authoritative.

## Verification

Focused Fix29:

```text
PYTHONPYCACHEPREFIX=/tmp/fix29-focused-final-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest \
  tools/trace_attribution/tests/test_root_confirmation_fix29.py

Ran 21 tests - OK
```

Fix17-Fix29 plus seed, Global Judge, causal Judge, checkpoint, and recursive
core regressions:

```text
PYTHONPYCACHEPREFIX=/tmp/fix29-core-final-pycache \
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
  tools.trace_attribution.tests.test_root_confirmation_fix29 \
  tools.trace_attribution.tests.test_seed_attribution \
  tools.trace_attribution.tests.test_global_judge \
  tools.trace_attribution.tests.test_causal_judge \
  tools.trace_attribution.tests.test_causal_checkpoint \
  tools.trace_attribution.tests.test_recursive_analyzer

Ran 522 tests - OK
```

Report, evaluator, replay, artifact, and acceptance affected suites:

```text
PYTHONPYCACHEPREFIX=/tmp/fix29-affected-final-pycache \
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
PYTHONPYCACHEPREFIX=/tmp/fix29-full-final-pycache \
PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'

Ran 918 tests - OK
```

Compilation and diff checks:

```text
PYTHONPYCACHEPREFIX=/tmp/fix29-compile-final-pycache \
python3 -m compileall -q tools/trace_attribution
PASS

git diff --cached --check
PASS

git diff --check 2b05451f7..HEAD
PASS
```

## Changed Files

Production and evaluator:

- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`

Focused and migrated regression tests:

- `tools/trace_attribution/tests/test_root_confirmation_fix29.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix18.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix19.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix23.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix26.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix27.py`

Control documents:

- `.superpowers/sdd/root-confirmation-task-2-fix29-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix29-report.md`

## Self-Review And Residual Risks

The production and test diff was reviewed for validation order, canonical owner
reconstruction, exact schema equality, per-seed failure isolation, stale-state
filter order, and accidental Task 3 or dependency changes. No unresolved
correctness finding remains.

The intentional compatibility cost is that immediately previous persisted
Global Judge, action, report, and checkpoint states require rejudgment or
regeneration. Artifact validation continues to depend on the existing graph
envelope as the single owner contract; Fix29 deliberately adds no parallel
artifact-owner representation.
