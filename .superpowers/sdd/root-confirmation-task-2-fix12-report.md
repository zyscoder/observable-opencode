# Root Confirmation Stability Task 2 Fix Wave 12 Report

Base commit: `8454a9782`

## Scope

Closed both Important findings from the twelfth whole-Task-2 review. The
changes remain offline, passive, read-only, dependency-free, and isolated from
agent behavior. No Task 3 evidence-expansion feature work was added.

## Root Causes

1. Capsule v4 rebuilt prompt collections from active graph facts, but every
   non-recorded retrieval family was reconstructed from its own persisted
   validation source. A jointly changed source family, navigation route, or
   active but unrelated evidence ref was therefore self-consistent and could
   enter Judge grounding or expansion anchors. The navigation-only boolean was
   emitted but was not required as exact boolean `True` on capsule restore.
2. Report construction checked that non-root factors embedded a top-level
   confirmation, but did not require their reason, confidence, citations, and
   mechanism to equal that confirmation. Active graph path validation covered
   confirmed roots only, so contributing conditions, amplifiers, and rejected
   candidates could publish disconnected, reversed, or temporal paths.

## RED

All finding-level regressions were observed failing before their production
changes were retained.

1. Exact navigation-only fact mutations (`False`, string `"true"`, integer `1`,
   and `None`) produced four failures because capsule restore accepted each
   value.
2. A self-consistent synthetic route changed its source family, navigation
   evidence, validation source, collection facts, and collection hash to include
   an unrelated active ref. Direct active validation accepted it. A separate
   source-family laundering mutation changed a synthetic empty route into a
   recorded family and was also accepted.
3. Eleven factor mutations changed reason, confidence, evidence citations, or
   mechanism across contributing, amplifying, and rejected-candidate roles;
   direct report construction accepted every mutation.
4. Contributing, amplifying, and rejected-candidate reports all survived an
   active path changed to temporal-only. The expanded GREEN regressions also
   cover disconnected and reversed hops.
5. With both non-root publication hooks removed, the new partial/completed
   checkpoint regression failed because partial restore accepted a disconnected
   factor path. Restoring the shared validator made both restore paths pass.

## Changes

- `CandidateEvidenceCapsule` now requires
  `retrieval_is_not_causal_verdict is True` exactly.
- Active capsule validation reconstructs every non-recorded source from the
  authoritative candidate outputs retained by the active analysis state. It
  matches canonical candidate identity, source family, sanitized navigation
  metadata, and evidence membership before rebuilding all prompt collections.
- Empty routes, including routes claiming a recorded family, require the same
  authoritative source match. Synthetic-to-recorded source laundering therefore
  fails closed.
- Global validation-envelope restore receives active candidate outputs from
  partial state or completed report reconstruction. Missing or mismatched active
  routes reject the request before Judge use or anchor selection.
- Direct report validation now requires every factor projection to equal its
  embedded/top-level confirmation for candidate/path identity, role, reason,
  confidence, evidence citations, and structured mechanism. Rejected candidates
  require a non-empty path and evidence, not only a definitive status.
- Added one shared active publication validator for contributing conditions,
  amplifying factors, and rejected candidates. It validates exact confirmation
  ownership, seed/defect binding, active candidate identity, evidence resolution
  and eligibility, every adjacent path hop through
  `is_confirmation_causal_edge()`, and matching global assessment facts when a
  global assessment exists.
- The shared validator runs for direct final publication, partial checkpoint
  restore, pending/completed report restore, and final report auditing.
- Added valid synthetic envelope/capsule roundtrips, valid factor publication
  roundtrips, bounded existing capsule coverage, source/route/evidence/metadata/
  hash/boolean mutations, all non-root roles, all invalid path orientations, and
  partial/completed checkpoint mutations.

## Persistence

No serialized field or schema shape changed. Capsule v4, validation envelope v4,
global judgment v4, report v5, and checkpoint v5 identities remain unchanged.

## Verification

1. Finding-level RED/GREEN regressions: observed expected failures before each
   validator was retained; all pass after implementation.
2. Task 2-focused attribution set:
   - `Ran 387 tests in 2.766s` / `OK`.
3. Full Python attribution suite:
   - `Ran 711 tests in 4.912s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix12-pycache-final python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`; exit status `0`.
5. Diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check 8454a9782 --`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_causal_state.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `.superpowers/sdd/root-confirmation-task-2-fix12-report.md`
