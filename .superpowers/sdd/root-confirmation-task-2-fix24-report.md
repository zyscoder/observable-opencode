# Root Confirmation Task 2 Fix Wave 24 Report

Base commit: `0509fea3c`

## Status

DONE

Fix24 remains limited to Root Confirmation Task 2. The implementation is
offline, passive, read-only, does not feed results back to the Agent, adds no
dependency, and does not implement Task 3 behavior.

## Findings Closed

1. Terminal confirmation action reconciliation now rejects a canonical action
   whose exact seed binding is absent from the current seed ledger. A missing
   builder can no longer be silently treated as stale.
2. A terminal action is excluded only when its exact ledger builder explicitly
   carries `start_ref_active_revision_ineligible`. Active builders continue
   into the bidirectional canonical multiset comparison against confirmation
   journal projections.
3. The same reconciliation runs for partial checkpoint state, pending and
   completed report restore, and evaluator report validation. Existing
   canonical validation still rejects malformed or missing owners before seed
   classification.
4. Every remaining `default_start_refs()` branch now filters candidates through
   `active_revision_evidence_eligible(ref)` before preferred-output selection,
   ordering, or return: response outputs, quality-flag fallbacks, case outcome
   records, and final generic fallback records. Final response claims and the
   earlier evaluation/defect branches retain their existing active filters.
5. A stale preferred response output no longer suppresses an active lower
   response-output or fallback branch. When no active candidate remains,
   `default_start_refs()` returns an empty list and the analyzer preserves its
   existing conservative no-start result.

## Persisted Identities

No persisted identity changed. Fix24 tightens validation and consistently
enforces the existing active-revision evidence policy without changing a
serialized field, shape, identity calculation, or policy meaning.

## TDD Record

- RED: structurally canonical cross-seed terminal actions with no current
  ledger builder were accepted by partial, pending, and completed restore.
- RED: stale response outputs won preferred selection; stale quality, case,
  and generic fallback records leaked into default starts; all-stale traces
  produced a stale start instead of the conservative empty start set.
- GREEN: 8 focused Fix24 tests, with branch and restore subtest matrices, cover
  unknown canonical actions, evaluator rejection, duplicates, active
  multi-seed controls, every affected default-start branch, fallback backfill,
  no-active behavior, aliases, formal/legacy manifests, and live/restore
  consistency.

## Verification

- Focused Fix24:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix24-green-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix24.py`
  - 8 tests, PASS
- Core affected Fix17-Fix24, seed, recursive, checkpoint, and evaluator:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix24-affected-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_root_confirmation_fix17.py tools/trace_attribution/tests/test_root_confirmation_fix18.py tools/trace_attribution/tests/test_root_confirmation_fix19.py tools/trace_attribution/tests/test_root_confirmation_fix20.py tools/trace_attribution/tests/test_root_confirmation_fix21.py tools/trace_attribution/tests/test_root_confirmation_fix22.py tools/trace_attribution/tests/test_root_confirmation_fix23.py tools/trace_attribution/tests/test_root_confirmation_fix24.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_causal_checkpoint.py`
  - 335 tests, PASS
- Graph/recursive/checkpoint/evaluator affected:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix24-graph-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_backward_taint.py tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_investigation.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_recursive_cli.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_benchmarks.py tools/trace_attribution/tests/test_recursive_acceptance_review.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_root_confirmation_fix24.py`
  - 512 tests, PASS
- Full attribution suite:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix24-full-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
  - 820 tests, PASS
- Compile:
  `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix24-compileall python3 -m compileall -q tools/trace_attribution`
  - PASS
- Exact baseline whitespace check:
  `git diff --check 0509fea3c..HEAD`
  - PASS

## Self-Review

- Unknown, quarantined, and active seed actions are mutually exclusive after
  canonical parsing. Unknown actions fail, exact quarantined builders filter,
  and active actions participate in the existing bidirectional bijection.
- Existing Fix18 stale-seed restore controls pass, including mixed stale and
  active publication checkpoints. Duplicate active actions still fail.
- Active-revision filtering happens before response-output preference and
  before all affected fallback ordering. A stale higher branch therefore
  cannot suppress an active lower branch.
- Formal and legacy revision authority, canonical and bare-ref aliases, empty
  no-start analysis, and report round-trip behavior are covered.
- Existing unrelated untracked files were not modified or staged.

## Concerns

None.
