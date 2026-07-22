# Root Confirmation Stability Task 2 Fix Report

Base commit: `d5fc6ca09`

## Scope

Closed the three Task 2 review findings only. The changes remain offline,
passive, read-only analysis and add no dependencies or Task 3 behavior.

## RED

The added global-judge regressions failed against the base implementation:

- A neighboring `active_focus_text` with its own valid normalized SHA-256 was
  accepted by request construction and could proceed through the capability and
  replay boundaries.
- A `causal_path_refs` sequence with individually grounded refs but a
  disconnected intermediate hop was accepted.
- An unselected `contributing_condition` with output defect `present` and an
  empty path was accepted.
- The fallback built `GlobalCandidateJudgment` directly, so the shared payload
  validator was not called for the fallback result.

The first strict path implementation also exposed a real capsule closure gap:
multi-hop paths were grounded from recorded graph edges, but capsules retained
only the candidate's immediate outgoing edges. The analyzer fusion regression
then fell back to recursive processing instead of confirming the valid path.

## Implementation

- `GlobalCandidateJudgeRequest.validate()` now binds normalized
  `active_focus_text` to normalized `active_defect.actual` and verifies its
  SHA-256. Construction, prompt/cache entry, payload validation, and replay all
  call this boundary.
- Global payload validation now checks every adjacent causal-path pair against
  an exact, forward, `eligible_for_attribution=True` edge in the supplied
  capsule. Any present `root_candidate`, `contributing_condition`, or
  `amplifying_factor` must carry a valid candidate-to-seed path, whether or not
  it is selected.
- Candidate capsules now project already-recorded path edges into
  `causal_path_edges`. This preserves proof for real multi-hop paths without
  synthesizing paths or changing the retrieval-only `outgoing_edges` contract.
- Global fallback now creates a complete inconclusive payload and obtains its
  result exclusively through `validate_global_candidate_payload`.

## Tests

Added global-judge coverage for:

- self-consistent neighboring focus rejection at request, capability/cache, and
  replay boundaries;
- disconnected but individually grounded path hops;
- unselected present causal candidates without a path;
- direct fallback invocation of the global payload validator.

Final verification:

1. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`
   - `Ran 27 tests` / `OK`.
2. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py -v`
   - `Ran 86 tests` / `OK`.
3. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_analyzer.py -v`
   - `Ran 80 tests` / `OK`.
4. `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`
   - `Ran 630 tests` / `OK`.
5. `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`
   - exit status `0`.
6. `git diff --check`
   - exit status `0`.
