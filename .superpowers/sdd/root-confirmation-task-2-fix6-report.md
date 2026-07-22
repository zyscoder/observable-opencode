# Root Confirmation Stability Task 2 Fix Wave 6 Report

Base commit: `1dc86c847`

## Scope

Closed all four findings from the sixth whole-Task-2 review. The analysis remains
offline, passive, read-only, dependency-free, and isolated from agent behavior.
No Task 3 evidence expansion behavior was added.

## Root Causes

1. Raw `dataflow_edges` selected the first truthy top-level or metadata
   provenance value. A recorded-looking value could therefore erase a temporal
   marker from the other container before confirmation policy ran.
2. Persisted v2 judgments checked schema keys and active-focus identity but did
   not rerun live matrix semantics because their candidate capsules were not
   persisted with the seed result.
3. The final/restored report audit resolved evidence citations but omitted the
   node identities that publish confirmations and roots.
4. `is_confirmation_causal_edge()` coerced explicit eligibility values with
   `bool()`, admitting truthy strings and numbers.

## RED

Added targeted tests before production changes and ran the exact 11-test
reproduction set.

Result: `Ran 11 tests` / `FAILED (failures=28)`.

The failures proved:

- all eight top-level/metadata provenance conflicts lost their two-source facts;
- temporal metadata still reached downstream search, queued confirmation,
  global validation, and final root publication;
- `"false"`, `"true"`, and `1` passed shared/global eligibility checks;
- missing assessments, incomplete competitor coverage, invalid status values,
  and contradictory counterfactuals restored as v2 judgments;
- partial and completed checkpoints restored semantically incomplete matrices;
- structurally bidirectional `record:ghost` root identities passed both new-report
  and completed-checkpoint audits when evidence cited a real node.

## Decisions

- Normalized recorded edges now retain `recorded_provenance.top_level` and
  `recorded_provenance.metadata` for relation, evidence type, origin, and
  inference method. Temporal classification evaluates their union.
- Eligibility is accepted only when the explicit/defaulted value `is True`.
- Every persisted global judgment now carries a canonical, seed-bound validation
  envelope containing the exact request and candidate capsules. Restore rebuilds
  the request and passes the saved payload through the live v2 validator, then
  cross-checks offered candidates, selected roots, and decisive refs against the
  seed result.
- Checkpoint compatibility uses
  `global-candidate-judgment/v2+validation-envelope/v1`; older v2-shaped state is
  rejected for rejudgment instead of being upgraded heuristically.
- Publication auditing separately resolves revision-eligible graph identities
  for confirmed seed owners, selected candidates, assessments and causal paths,
  all confirmations and competitor paths, and primary/co-root fields. Artifact
  resolution remains valid only for evidence citations, never root identities.

## Verification

1. Exact targeted reproduction set:
   - `Ran 11 tests in 0.130s` / `OK`.
2. Task 2-focused attribution set:
   - `Ran 342 tests in 1.610s` / `OK`.
3. Full Python attribution suite:
   - `Ran 665 tests in 3.518s` / `OK`.
4. Compile check:
   - `python3 -m compileall -q` over attribution source, scripts, and tests;
     exit status `0`.
5. Working-tree diff check:
   - `git diff --check`; exit status `0`.
6. Required base-range diff check is rerun after commit and recorded in the final
   response.

## Files

- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/confirmation_path.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_confirmation_path.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `.superpowers/sdd/root-confirmation-task-2-fix6-report.md`

## Risks

- Canonical validation envelopes increase checkpoint/report size because the
  bounded candidate capsules are retained per seed. This is intentional: exact
  restore-time semantics take precedence over compact but unverifiable state.
- Older checkpoints with prompt-v2 judgments but no validation envelope are
  intentionally incompatible and must be rejudged.
- Future recorded provenance fields outside the four confirmation signals still
  require explicit policy review before they affect temporal classification.
