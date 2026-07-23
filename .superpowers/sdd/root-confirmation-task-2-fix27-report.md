# Root Confirmation Task 2 Fix Wave 27 Report

Base commit: `b035e652c`

Status: `DONE`

## Scope And Boundaries

Fix27 closes the four Important findings in the brief. The implementation
remains offline, passive, read-only, and attribution-only. It adds no
dependency, does not implement Task 3, and does not feed any conclusion back
to the Agent.

## Root-Cause Validation

### 1. Real Global Judge failures became successful results

`ClaudeCausalJudge.judge_candidates_bounded()` converted every validated
request outcome without a payload into a synthetic successful
`GlobalCandidateJudgment(outcome="inconclusive")`. That bypassed the
analyzer's typed `BoundedJudgeCallError` failure path and could record a
completed global pass.

The adapter now raises `BoundedJudgeCallError` with the exact physical request
count and the original error kind/detail. The analyzer's existing `fail_seed()`
path remains the only producer of the failed action, unresolved episode,
blocker, and missing evidence.

### 2. Global artifact hydration used a weaker owner contract

`TraceGraph.artifact_hydration_manifest()` previously assembled a second,
weaker content envelope. It did not require the candidate to be the strict
active owner and did not include the canonical owner binding identity.
Rejected content could also disappear from the Judge payload without a gap.

Hydration now calls the existing
`artifact_evidence_envelope(..., expected_owner_ref=...)` function. Unbound,
stale, mismatched, and otherwise ineligible owners expose no content and emit
an explicit ineligible evidence fact. Global capsules convert all missing,
truncated, integrity-failed, and owner-ineligible artifacts into
Judge-visible gaps. Root confirmation attaches the complete canonical
manifest after generic payload sanitization, avoiding a second envelope
construction or a partially stripped manifest.

### 3. Failed global derivations were not bijective

Restore validation previously checked only that a failed action's blocker
appeared in the seed and that some unresolved episode named the pass. It did
not reconcile exact diagnostics, physical request count, owner, seed gaps, or
episode cardinality.

Fix27 defines `global-candidate-failure-projection/v2` and persists the same
projection in the failed action and its one unresolved episode. Restore and
report/evaluator validation require an exact canonical bijection, exact
canonical seed owner/ref/fingerprint, exact blocker and missing-evidence
sets, and exact physical request counts. Typed seed parsing rejects duplicate
set-backed persisted fields before normalization can collapse cardinality.

### 4. Pending confirmation identity was trusted

Queue restore and enqueue accepted any non-empty `semantic_identity`, while
producers separately reimplemented the hash tuple. Confirmation execution
could also synthesize a missing identity.

All producers now call `_confirmation_request_identity()`. Pending entries
have an exact allowed schema and must store the recomputed identity. Enqueue,
live validation, queue-key projection, action lookup, restore, evaluator, and
replay all use the same canonical value. Execution no longer repairs a
missing identity.

## RED To GREEN Evidence

The initial focused RED command was:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix27.py -v
```

Before production changes it ran 19 tests and failed with 20 failing
assertions/subtests. The failures covered the synthetic Judge fallback,
strict owner hydration, failure projection reconciliation, canonical pending
identity, and required version identities.

Each finding then went GREEN independently:

- Real adapter typed failure: 5 tests passed.
- Strict Global artifact hydration: 5 tests passed initially; the final suite
  has 6 after adding checkpoint/report/evaluator round-trip coverage.
- Global failure projection: 3 tests passed.
- Canonical pending confirmation identity: 4 tests passed.

Self-review added a focused duplicate seed-gap mutation. It first failed
because typed seed construction silently deduplicated the persisted list, then
passed after duplicate rejection moved into
`SeedAttributionResult.from_dict()`.

## Version Decisions

- Candidate evidence capsule: `candidate-evidence-capsule/v7`
- Graph evidence policy: `graph-external-evidence-eligibility/v5`
- Global judgment/prompt: `global-candidate-judgment/v6`
- Global validation envelope: `global-candidate-validation-envelope/v6`
- Global failure projection: `global-candidate-failure-projection/v2`
- Root confirmation persistence: includes
  `confirmation-request-identity/v1`
- Action state: `recursive-analysis-actions/v7`
- Report: `recursive-attribution-report/v10`
- Checkpoint: `recursive-attribution-checkpoint/v9`

Immediate Fix26 capsule/report identities are rejected rather than silently
reinterpreted.

## Final Verification

```text
PYTHONPYCACHEPREFIX=/tmp/fix27-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix27.py -v
Ran 20 tests - OK

PYTHONPYCACHEPREFIX=/tmp/fix27-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_root_confirmation_fix22.py tools/trace_attribution/tests/test_root_confirmation_fix23.py tools/trace_attribution/tests/test_root_confirmation_fix24.py tools/trace_attribution/tests/test_root_confirmation_fix25.py tools/trace_attribution/tests/test_root_confirmation_fix26.py tools/trace_attribution/tests/test_root_confirmation_fix27.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_causal_checkpoint.py
Ran 386 tests - OK

PYTHONPYCACHEPREFIX=/tmp/fix27-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_investigation.py
Ran 250 tests - OK

PYTHONPYCACHEPREFIX=/tmp/fix27-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
Ran 884 tests - OK

PYTHONPYCACHEPREFIX=/tmp/fix27-pycache python3 -m compileall -q tools/trace_attribution
PASS

git diff --check b035e652c..HEAD
PASS
```

## Changed Files

Production:

- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`

Focused and affected tests:

- `tools/trace_attribution/tests/test_root_confirmation_fix27.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_causal_judge.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix18.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix19.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix20.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix23.py`
- `tools/trace_attribution/tests/test_root_confirmation_fix26.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`

Control documents:

- `.superpowers/sdd/root-confirmation-task-2-fix27-brief.md`
- `.superpowers/sdd/root-confirmation-task-2-fix27-report.md`

## Residual Risks

No known correctness concern remains in Fix27 scope. Provider behavior is
covered with the real `ClaudeCausalJudge` adapter and scripted offline
transports; no live provider/network call was made, as required.
