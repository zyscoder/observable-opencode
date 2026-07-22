# Root Confirmation Stability Task 2 Fix Wave 3 Report

Base commit: `d8d23e1a2`

## Scope

Closed the remaining Task 2 causal-path relation gap only. The analysis remains
offline, passive, read-only, and dependency-free.

## Root Cause

The recursive analyzer excluded confirmation-stage non-causal relations while
searching downstream paths, but the global payload validator checked only
direction and `eligible_for_attribution`. An eligible navigation, temporal, or
fallback relation could therefore be accepted in a globally judged causal path.

## RED

Added the following tests before changing production code:

- The global payload validator must reject eligible
  `semantic_navigation_route`, `temporal_sequence`, `fallback_sequence`, and
  `previous_progress_episode` hops.
- A mixed path with a causal `produced` hop followed by an eligible
  `temporal_sequence` hop must be rejected.
- The downstream analyzer path search must reject the same eligible
  navigation, temporal, fallback, and mixed paths.
- A fully eligible, correctly directed `produced` hop remains valid through
  the global payload validator.

The first RED run produced four global-validator failures. The explicit
`fallback_sequence` additions then failed in both the global validator and
downstream analyzer search, confirming that both boundaries needed the shared
policy.

## Implementation

- Added `confirmation_path.py` with the centralized
  `NON_CAUSAL_CONFIRMATION_RELATIONS` set and
  `is_confirmation_causal_edge()` predicate.
- Replaced the recursive downstream search and queued confirmation-path
  checks with that predicate.
- Applied the same predicate to global capsule hop validation, preserving the
  validator's requirement for explicitly true attribution eligibility.
- Kept positive causal relations accepted while rejecting non-causal hops even
  when their direction and eligibility match.

## Verification

1. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`
   - `Ran 33 tests` / `OK`.
2. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py -v`
   - `Ran 86 tests` / `OK`.
3. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_analyzer.py -v`
   - `Ran 81 tests` / `OK`.
4. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
   - `Ran 637 tests in 3.143s` / `OK`.
5. `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-fix3-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`
   - exit status `0`.
6. `git diff --check`
   - exit status `0`.
