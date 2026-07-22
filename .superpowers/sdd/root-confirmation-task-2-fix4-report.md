# Root Confirmation Stability Task 2 Fix Wave 4 Report

Base commit: `8f06aa7ba`

## Scope

Closed the incomplete temporal and unknown relation policy in Task 2 only. The
changes remain offline, passive, read-only, and dependency-free; no Task 3
behavior was added.

## Root Cause

The shared confirmation predicate checked attribution eligibility and a small
relation deny-set. It did not reuse `graph.is_temporal_only_edge()`, so temporal
aliases and temporal `evidence_type`, `edge_origin`, or `inference_method`
metadata could pass. It also accepted every relation absent from the deny-set,
including empty, missing, and unknown values.

Both downstream path search and queued confirmation already called the shared
predicate, as did the global payload validator. The incomplete shared policy
therefore affected all three boundaries.

## RED

Tests were added before production changes.

- `test_confirmation_path.py` produced 12 failures for temporal aliases,
  temporal metadata, and empty/missing/unknown relations.
- `test_global_judge.py` produced 11 failures through the global capsule hop
  validator for the same gaps.
- The targeted recursive tests produced nine downstream-search failures, and
  queued confirmation incorrectly invoked the confirmation judge for a
  `temporal_advisory` path.

## Implementation

- `is_confirmation_causal_edge()` now directly reuses
  `graph.is_temporal_only_edge()`. The dependency is one-way from the small
  confirmation policy into `graph`; `graph` does not import confirmation code,
  so no circular import was introduced.
- Added an explicit `CONFIRMATION_CAUSAL_RELATIONS` allow-list derived from the
  repository's `FORMAL_DATAFLOW_RELATIONS`, graph reconstruction relations, and
  established attribution fixtures. `claim_group_precedes` remains excluded as
  ordering-only.
- Retained explicit navigation, fallback, progress, and temporal relation
  exclusions on top of the graph temporal classifier.
- Empty, missing, graph-default `dataflow`, and unknown relations now fail
  closed. Matching uses the graph's existing `str(...).strip().lower()` behavior
  and introduces no aliases.
- Updated one provenance-envelope fixture to provide a separate
  `used_as_context` causal hop. Its ranking routes remain navigation evidence
  and no longer serve as confirmation proof.

## Coverage

Added or expanded coverage for:

- `temporal_availability`, `available_to_next_request`, and
  `temporal_adjacency`;
- `temporal_inferred`, `temporal_only`, and `temporal_advisory` evidence types;
- temporal edge origin and inference method metadata;
- navigation, fallback, progress, empty, missing, and unknown relations;
- mixed causal/temporal paths;
- downstream search, queued confirmation, and global validation;
- representative formal, reconstructed, and fixture causal relations;
- canonical case and surrounding-whitespace matching.

## Final Verification

1. Focused policy/global/causal/recursive suites:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py`

   Result: `Ran 209 tests in 0.590s` / `OK`.

2. Full Python attribution suite:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`

   Result: `Ran 646 tests in 3.155s` / `OK`.

3. Compile check:

   `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-fix4-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`

   Result: exit status `0`.

4. Diff check:

   `git diff --check`

   Result: exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/confirmation_path.py`
- `tools/trace_attribution/tests/test_confirmation_path.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `.superpowers/sdd/root-confirmation-task-2-fix4-report.md`

## Risks

No blocking risks. The fail-closed allow-list intentionally requires future
trace relation additions to be explicitly reviewed before they can ground a
root-confirmation path.
