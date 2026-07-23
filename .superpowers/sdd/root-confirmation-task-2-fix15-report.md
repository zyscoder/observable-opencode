# Root Confirmation Stability Task 2 Fix Wave 15 Report

Base commit: `b4c87bf81`

## Scope

Closed both Important findings from the fifteenth whole-Task-2 review. The
change is limited to offline attribution retrieval, candidate evidence
validation, restore, and publication. It remains passive, read-only,
dependency-free, and isolated from agent behavior. No Task 3 expansion or
unrelated behavior was added.

## Root Cause

Candidate retrieval checked generic graph evidence eligibility but did not
apply active revision eligibility until later capsule, queue, or publication
boundaries. Direct edges, the `process.signal` early return, semantic fallback,
sibling context, progress windows, and merged routes could therefore rank and
truncate a stale high-scoring candidate before the active lower-scoring
candidate was considered.

The shared active-revision predicate also collapsed two independent namespaces
into strings. It compared numeric CaseTrace `repository_revision` generations
to the manifest's Git-text `subject_revision`, and truthy coercion erased
generation zero. Action-group comparison and capsule/context serialization
repeated the same conflation.

## RED

All finding-level regressions failed before production changes.

1. With `limit=1`, stale candidates outranked active candidates through direct
   edges, `process.signal`, semantic fallback, sibling, and progress retrieval.
   Every route returned `record:stale` instead of `record:active`.
2. Multi-seed/tie/duplicate coverage returned stale candidates for both seeds
   and returned a stale candidate for the no-eligible case.
3. Numeric `1` and `N` generations were rejected against `git:active`; stale
   generation `0` was accepted against active generation `1`; boolean false
   was erased as missing.
4. Capsule facts serialized repository generation zero as an empty string.
5. Partial checkpoint restore accepted a root from generation zero after the
   authoritative current claim advanced to generation one.
6. Publication rejected an otherwise valid authored root whose subject was
   `git:active` and whose numeric generation matched the current claim.
7. Capsule and global persistence identities remained at v4 after the
   zero-preserving persisted meaning changed.

## Changes

- Added one graph-level active revision predicate used by retrieval, evidence
  sanitization, capsules, action groups, global selection, restore, confirmation,
  and publication.
- `subject_revision` is now validated only as a string identity against the
  active manifest subject revision.
- `repository_revision` is now validated only as a nonnegative integer against
  an authoritative CaseTrace generation derived from current response claims,
  effective verifications, or recorded change generations. When no such
  authority exists, valid numeric fields follow the legacy compatibility policy
  and remain eligible.
- Explicit `revision_status` values retain their own contract: only `matched`
  is eligible; missing/null legacy fields remain valid, while explicit missing,
  mismatch, or malformed values fail closed.
- Preserved integer zero in candidate graph facts and active defect context.
- Applied revision eligibility before source deduplication, semantic/edge
  ranking, provenance-envelope selection, route merging, canonicalization, and
  every `limit` truncation. Eligible-candidate ranking and score-only navigation
  semantics are unchanged.
- Filtered stale resolved evidence refs and action-group members while
  preserving unresolved refs and valid legacy nodes with absent revision
  fields.
- Added regressions for every retrieval path, multi-seed isolation, ties,
  duplicates, no-eligible results, numeric generations `0/1/N`, combined
  subject/generation fields, null/missing/malformed values, current claims,
  capsules, restore, and publication.

## Persistence

Candidate capsule identity advanced from
`candidate-evidence-capsule/v4` to `candidate-evidence-capsule/v5` because
generation zero is now persisted as `"0"` instead of an empty value. The global
persistence contract now names `capsule/v5`. Global judgment v4, validation
envelope v4, report v5, checkpoint v5, and root confirmation v7 shapes remain
unchanged. Older v4 capsules are rejected for conservative reconstruction.

## Verification

1. Finding-level RED/GREEN regression set:
   - `Ran 6 tests in 0.038s` / `OK`.
2. Persistence identity RED/GREEN set:
   - `Ran 3 tests in 0.003s` / `OK`.
3. Expanded Task 2 focused attribution set:
   - `Ran 260 tests in 1.848s` / `OK`.
4. Full Python attribution suite:
   - `Ran 724 tests in 4.543s` / `OK`.
5. Compile check:
   - `PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix15-pycache-final python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`; exit status `0`.
6. Diff check:
   - `git diff --check b4c87bf81..HEAD`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/judgment_context.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/tests/test_causal_retrieval.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `.superpowers/sdd/root-confirmation-task-2-fix15-report.md`
