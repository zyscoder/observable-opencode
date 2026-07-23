# Root Confirmation Stability Task 2 Fix Wave 13 Report

Base commit: `b30c55f4a`

## Scope

Closed both Important findings from the thirteenth whole-Task-2 review. The
change remains offline, passive, read-only, dependency-free, and isolated from
agent behavior. No Task 3 evidence-expansion work was added.

## Root Causes

1. Recorded-route restore constructed a `CausalCandidate` from the capsule's
   persisted `candidate_evidence_refs` and canonicalized that candidate against
   itself. Authoritative candidate output was consulted only for synthetic or
   empty-edge routes. Persisted additions, removals, duplicates, reordered
   membership, and source-family drift could therefore authorize or influence
   reconstruction before prompt-collection validation.
2. Action-group construction scanned every raw graph node with a matching
   action/call identity. It did not require canonical active resolution,
   `graph.evidence_eligible()`, or active-revision compatibility, so audit-only
   external facts and stale same-call nodes could enter capsule facts, Judge
   grounding, decisive evidence, and expansion anchors.

## RED

Both finding-level regressions were run before their production changes.

1. The recorded-route test exercised an unrelated active ref, a removed ref,
   reordered and duplicated membership, a source-family change, and changed
   edge metadata with recomputed prompt collections/hash. The base
   implementation accepted the unrelated, removed, duplicated, and
   source-family mutations. Reordered/metadata mutations reached only later
   prompt-collection mismatch checks instead of failing source binding.
2. A shared-call audit-only `external.evaluation_fact` was present in the
   direct capsule action group, producing the expected failing membership
   assertion.

## Changes

- Recorded capsule construction normalizes recorded routes to Judge-visible
  edge form, then reconciles them against current authoritative routes.
- Reconciliation selects the authoritative route by active candidate identity,
  target, relation, and evidence type; this preserves distinct routes for the
  same candidate across multiple seeds.
- The selected edge must exactly equal an active normalized graph edge. Source
  family, normalized edge metadata, and ordered/deduplicated evidence
  membership must exactly equal the authoritative current candidate output.
- Persisted `candidate_evidence_refs` are now comparison-only. They are never
  used to construct the authoritative recorded route.
- Missing authoritative recorded output fails closed before prompt
  reconstruction, Judge use, restore, or publication.
- Action-group members now require exact canonical graph resolution, active
  graph presence, `graph.evidence_eligible()`, and active-revision
  compatibility before hydration or serialization.
- Valid group members retain deterministic trace order and the existing
  truncation/count contract.
- Added direct capsule mutations, valid recorded roundtrips, active validation
  envelope restore, Judge grounded-ref/decisive-evidence/anchor coverage,
  ineligible external and stale same-call members, and partial/completed
  checkpoint mutations.

## Persistence

No serialized field, schema shape, or semantic identity changed. Capsule v4,
validation envelope v4, global judgment v4, report v5, and checkpoint v5
identities remain unchanged.

## Verification

1. Finding-level RED/GREEN regressions: expected failures observed before the
   implementation; all five focused cross-surface tests pass after it.
2. Task 2-focused attribution set:
   - `Ran 391 tests in 2.873s` / `OK`.
3. Full Python attribution suite:
   - `Ran 715 tests in 4.974s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix13-pycache-final python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`; exit status `0`.
5. Diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check b30c55f4a --`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `.superpowers/sdd/root-confirmation-task-2-fix13-report.md`
