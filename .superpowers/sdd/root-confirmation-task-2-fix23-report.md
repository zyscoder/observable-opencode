# Root Confirmation Task 2 Fix Wave 23 Report

Base commit: `601741980`

## Status

DONE

Fix23 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, adds no
dependency, and does not implement Task 3 behavior.

## Findings Closed

1. Confirmation journal outcomes now have a canonical, owner-bound action
   projection. The projection retains operation and action status, semantic
   action key, request and response identities, exact local owner occurrence,
   candidate/hypothesis/defect/seed/path identities, evidence, physical request
   accounting, and the complete confirmation payload.
2. Live state, action snapshots, current reports, pending/completed restore,
   and evaluator validation use the same projection constructor and validator.
   Missing, duplicate, extra, substituted, cross-seed, rewritten occurrence,
   and request/response identity mutations fail.
3. Checkpoint restore compares confirmation journal projections with all
   completed/failed confirmation action records visible at the restored
   snapshot transaction. The comparison is unconditional when either side is
   empty, so deleting every terminal action while retaining confirmations
   fails partial, pending, and completed restore.
4. Terminal confirmation action records persist their canonical projection.
   Crash replay reuses the exact completed/failed projection without producing
   a duplicate terminal action, and interrupted requests retain the started
   action's reservation and conservative inexact accounting.
5. Numeric repository revision authority now parses the formal manifest
   binding before considering producers. Under a declared formal revision,
   response claims, effective final-state verifications, and changes require
   the matching `subject_revision` and
   `revision_provenance_status=valid`. Missing, mismatched, malformed, stale,
   audit/offline, and ineligible producers cannot establish authority and are
   themselves active-revision ineligible.
6. Legacy subject-binding omission remains accepted only when the manifest has
   no formal subject revision. Valid zero, ties, event-specific numeric fields,
   and strict non-boolean nonnegative integer validation remain intact.

## Persisted Identities

- Report: `recursive-attribution-report/v8`
- Checkpoint: `recursive-attribution-checkpoint/v7`
- Action state: `recursive-analysis-actions/v5`
- Published output: `recursive-attribution-output/v2`
- Evaluator comparison: `recursive-attribution-comparison/v5`
- Judgment cache: `3.0`
- Evidence eligibility: `graph-external-evidence-eligibility/v3`
- Root confirmation persistence:
  `recursive-root-confirmation/v9+resolution/v2+evidence-policy/v3+local-state-owner/v1+action-projection/v1`
- Global candidate persistence now binds `evidence-policy/v3`

Current v8 reports require an explicit
`metadata.confirmation_action_projection` list, including an explicit empty
list. Report v7 and checkpoint/action states from the prior identities are
rejected. The existing v2 report migration remains conservative and publishes
no migrated confirmations or roots.

## TDD Record

- RED: deleting all `confirmation_completed`/`confirmation_failed` actions
  allowed partial, pending, and completed restore.
- RED: reports and action snapshots had no durable confirmation action
  projection, and projection mutations could not be validated.
- RED: formal manifests accepted missing subject/provenance producers and a
  stale unbound revision 99 displaced a valid bound revision 2.
- RED: missing-bound numeric producers remained active-revision evidence
  eligible even after they were excluded from authority selection.
- RED: omitting the projection field was indistinguishable from an explicit
  empty projection.
- GREEN: 12 focused Fix23 tests cover empty action sides, projection shape and
  mutation classes, formal and legacy manifest controls, all authority event
  types, zero/ties/numeric validation, and every bumped identity.

## Verification

- Focused Fix23:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix23-focused-final3-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix23.py`
  - 12 tests, PASS
- Core affected Fix17-Fix23, seed, recursive, checkpoint, and evaluator:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix23-affected-final3-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_root_confirmation_fix22.py tools/trace_attribution/tests/test_root_confirmation_fix23.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_causal_checkpoint.py`
  - 327 tests, PASS
- Graph/recursive/checkpoint/evaluator affected:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix23-graph-final2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_investigation.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_recursive_cli.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_root_confirmation_fix23.py`
  - 516 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix23-full-final2-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 812 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix23-compileall-final2 python3 -m compileall -q tools/trace_attribution`
  - PASS
- Worktree whitespace check before commit:
  `git diff --check 601741980`
  - PASS
- Exact baseline whitespace check after commit:
  `git diff --check 601741980..HEAD`
  - PASS

## Self-Review

- Projection construction is centralized; state, report, evaluator, action
  record, and replay paths do not maintain parallel approximations.
- Journal-to-projection and projection-to-terminal-action comparisons are
  bidirectional multisets and are invoked with an explicit empty action list
  during checkpoint/report restore.
- Snapshot reconciliation remains transaction-bounded; an action committed
  after a snapshot cannot authorize facts in that earlier snapshot.
- Stale-seed quarantine filters report/state projections and filters terminal
  actions through the same active seed ledger before comparison.
- Formal manifest validation is evaluated before revision maximization, so a
  stale high producer cannot hide a valid lower authority.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
