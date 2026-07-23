# Root Confirmation Stability Task 2 Critical Fix Wave 14 Report

Base commit: `c7f7c3e8e`

## Scope

Closed the Critical stale-revision candidate publication defect. The change is
limited to Task 2 attribution analysis and remains offline, passive, read-only,
dependency-free, and isolated from agent behavior. No Task 3 evidence expansion
or unrelated behavior was added.

## Root Cause

Wave 13 filtered action-group siblings by revision, but candidate eligibility
still depended on generic graph evidence eligibility and authored-node type.
An authored candidate at `git:stale` therefore remained eligible when the trace
manifest named `git:active`.

When that stale candidate had no eligible action-group siblings, the empty-group
fallback added the candidate itself. The resulting capsule then authorized the
same ref for global grounding and selection. Confirmation queueing, independent
confirmation, factor/root publication, and checkpoint restore repeated only the
generic evidence/authored checks, so the stale candidate could become a
published root.

## RED

All regression failures were observed before the production changes.

1. The end-to-end regression used a manifest at `git:active` and a unique
   decision at `git:stale`. Its single assertion reported all leaked surfaces:
   `['action_group', 'grounded_refs', 'confirmation_queue', 'published_roots']`.
2. Recorded `confirmed_edge` and synthetic `semantic_fallback` routes for the
   stale candidate both produced capsules instead of the expected empty result.
3. A two-seed queue accepted both the stale and active candidates:
   `[True, True]` instead of `[False, True]`.
4. Partial checkpoint restore accepted a previously confirmed root after its
   active graph node drifted from `git:active` to `git:stale`; no `ValueError`
   was raised. The same test also covers completed report restore.

## Changes

- Added `active_revision_candidate_eligible(graph, ref)` as the single
  authoritative candidate gate. It canonicalizes aliases, requires an active
  graph node and graph evidence eligibility, rejects explicit non-`matched`
  revision status, and rejects declared subject/repository revisions that do
  not equal the manifest subject revision.
- Preserved legacy revision-missing nodes when they contain no contradictory
  revision metadata. An explicit `revision_status="missing"` fails closed.
- Applied the predicate to recorded and synthetic capsule construction,
  capsule active-graph restore, action-group members, and the empty-group
  fallback. The fallback additionally requires authored-root eligibility.
- Applied the same predicate to recursive and global candidate retrieval,
  memoization, navigation routing, global request grounding validation, global
  authored sibling/evidence selection, and selected-root application.
- Applied defense-in-depth checks to authored-root introduction, confirmation
  queue insertion and restore, confirmation request construction, competitor
  construction, confirmation recording, non-root factor publication, root
  publication, current report audit, and partial/completed restore.
- Candidate aliases are accepted only through canonical resolution and are
  published as the canonical ref. Valid active-revision candidates remain
  eligible, and rejecting one seed's stale candidate does not suppress another
  seed's active candidate.

## Persistence

No serialized field, schema shape, or semantic identity changed. Capsule v4,
validation envelope v4, global judgment v4, report v5, and checkpoint v5
identities remain unchanged. The change rejects invalid active-state content
using existing persisted revision facts.

## Verification

1. End-to-end and finding-level RED/GREEN regressions:
   - `Ran 4 tests in 0.029s` / `OK`.
2. Expanded Task 2 focused attribution set:
   - `Ran 382 tests in 2.068s` / `OK`.
3. Full Python attribution suite:
   - `Ran 719 tests in 4.570s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix14-pycache-final python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`; exit status `0`.
5. Diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check c7f7c3e8e --`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `.superpowers/sdd/root-confirmation-task-2-fix14-report.md`
