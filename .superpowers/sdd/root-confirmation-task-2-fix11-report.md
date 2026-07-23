# Root Confirmation Stability Task 2 Fix Wave 11 Report

Base commit: `da0826d47`

## Scope

Closed the final Important finding from the eleventh whole-Task-2 review. The
change remains offline, passive, read-only, dependency-free, and isolated from
agent behavior. No Task 3 feature work was added.

## Root Cause

Capsule v3 construction derived retrieval-edge, action-group, evidence-reference,
artifact-hydration, and missing-evidence facts from active sources, but active
validation only reconciled candidate/node identity, path references, and three
edge collections. Restored prompt-bearing collections therefore remained trusted
persisted data. A substituted action-group member or forged resolved reference
could enter `grounded_refs`, reach a Judge request, and become an accepted
expansion anchor.

## RED

All finding-level regressions were added before production changes.

1. Direct capsule, Judge request, and expansion-anchor selectors ran 3 tests and
   reported 8 failures. Every named collection mutation passed validation, and a
   forged action-group member was accepted as a grounded expansion anchor.
2. Partial checkpoint restore ran 1 test and failed because the stale
   action-group member was accepted.
3. The bounded-source selector ran 1 test and failed because an oversized source
   envelope restored successfully.
4. The paired synthetic-route selector ran 1 test and failed because a jointly
   substituted unresolved route source and Judge field passed active validation.

The direct collection test covers a changed retrieval edge, substituted
action-group member, forged resolved evidence reference, stale artifact hydration,
and both removed and extra missing-evidence refs. It recomputes the persisted
collection hash after mutation so the active graph reconstruction, rather than an
unchanged checksum, must reject every substitution.

## Changes

- Added capsule v4 `validation_source`, a strict and byte-bounded envelope of the
  canonical candidate route, sanitized evidence refs, path, seed refs, and the
  prompt-collection digest.
- Centralized live construction of all five named prompt-bearing collections.
  Active validation reconstructs them from the active graph, artifact manifest
  and hydration state, action-group lineage, normalized references, and bounded
  route inputs, then compares every collection exactly.
- Reconciled recorded `confirmed_edge` and `attribution_edge` routes against the
  active graph. Removed, changed, temporalized, provenance-drifted, and
  revision-drifted recorded routes fail closed. Synthetic retrieval routes remain
  explicit bounded construction inputs and cannot alter causal selection rules.
- Sanitized validation-source refs with the same evidence-eligibility policy as
  the Judge capsule, preventing audit-only refs from reappearing in persistence.
- Bound capsule construction to a 16 KiB validation-source ceiling. Oversized
  envelopes fail before persistence or Judge use.
- Added current-version valid roundtrip and bounded-size coverage, active
  envelope/Judge restoration coverage, partial and completed checkpoint restore
  coverage, and expansion-anchor noninterference coverage.
- Bumped compatibility identities because persisted serialization changed:
  capsule v4, validation envelope v4, global judgment/prompt v4, report v5, and
  checkpoint v5. The global persistence contract is now
  `global-candidate-judgment/v4+validation-envelope/v4+capsule/v4`; root
  confirmation v7 remains unchanged because its serialization and behavior did
  not change.

## Verification

1. Task 2-focused attribution set:
   - `Ran 403 tests in 2.306s` / `OK`.
2. Full Python attribution suite:
   - `Ran 702 tests in 4.219s` / `OK`.
3. Compile check:
   - `PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix11-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`; exit status `0`.
4. Diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check da0826d47 --`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `.superpowers/sdd/root-confirmation-task-2-fix11-report.md`

## Residual Risk

Synthetic retrieval edges are intentionally navigation-only and are preserved as
bounded construction inputs because they are not recorded graph edges. Their
candidate endpoints must resolve against the active graph; unresolved evidence
refs remain explicit unresolved/missing facts. Their complete Judge-facing
collection is source-bound, and they cannot establish a causal path or root
verdict without the existing independent causal validation.
