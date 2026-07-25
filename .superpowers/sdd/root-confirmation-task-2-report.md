# Root Confirmation Stability Task 2 Report

## Status

Implemented on base `bd8f1ab27` using test-driven development.

Global candidate judgment now uses the fail-closed
`global-candidate-judgment/v8` contract. Every request is bound to exactly one
seed and every accepted judgment carries a complete, immutable candidate
comparison matrix for that seed.

## Behavior Delivered

- Added explicit request fields for `seed_ref`, `active_defect`,
  `active_focus_text`, and the SHA-256 of normalized focus text. Capsule defect
  and seed bindings must match the explicit request; no field is inferred from
  `capsules[0]`.
- Added required input/output defect status, causal path, structured
  counterfactual, and compared-candidate fields to every assessment. Top-level,
  assessment, binding, and counterfactual objects reject missing and extra keys.
- Reused the root-confirmation counterfactual vocabulary:
  `intervention_ref`, `intervention_kind`, `predicted_defect_status`, and
  `causal_effect`. Predictions are limited to decidable present/absent results.
- Added `active_focus_binding` and the public
  `validate_active_focus_binding()` helper. Seed ref, defect fingerprint, and
  normalized focus text hash must exactly match the request.
- Restricted selected roots to offered root-eligible candidates whose input does
  not already contain the defect and whose output does. A selected root also
  needs a supplied candidate-to-seed path and a counterfactual that predicts the
  defect becomes absent.
- Required every assessment to cover all open root-eligible candidates. The set
  is sorted independently of retrieval order and includes the assessed candidate
  itself when eligible.
- Stopped synthesizing candidate-to-seed paths for disconnected retrieval
  evidence. Such candidates remain available as evidence but cannot present a
  fabricated causal path.
- Made comparison-before-selection an explicit ordered prompt phase and emitted
  `retrieval_is_not_causal_verdict=true`. Rank and score remain navigation-only.
- Updated Claude success, repair, cache, and fallback paths. Fallback produces a
  valid bound v8 `inconclusive` judgment instead of bypassing the schema.
- Bound cache audit identities and analyzer journal events to the active seed.
  Analyzer consumption revalidates the complete v8 judgment, including offline
  and scripted judge implementations, before applying it.
- Preserved offline, passive, read-only analysis behavior and added no dependency.

`compared_candidate_refs` explicitly includes self for an eligible candidate.
Every assessment uses the same complete open eligible set; retrieval rank only
orders navigation inputs and cannot change comparison coverage.

## TDD Evidence

### RED

1. Focus drift and five missing matrix fields:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py -v`

   Result: 6 failures. The v1 validator rejected `active_focus_binding` as an
   unknown top-level field and did not identify the five required matrix fields.

2. Fallback and ordered prompt phases:

   Same command after the first v2 model increment.

   Result: fallback raised a constructor error for the five new assessment fields,
   and the prompt lacked a machine-verifiable compare-then-select sequence.

3. Analyzer integration:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py -v`

   Result: 12 retrieval-global tests errored because analyzer still constructed a
   v1 request without explicit seed identity.

4. Structured counterfactual alignment:

   The global suite failed against the earlier free-text counterfactual shape after
   tests switched to the existing exact intervention/prediction/effect vocabulary.

### GREEN

- Global Judge suite: 23 tests passed.
- Causal Judge suite: 86 tests passed.
- Recursive Analyzer suite: 80 tests passed.
- Full Python attribution suite: 1,052 tests passed.

## Final Commands

1. Required focused suites:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py`

   Result: `Ran 23 tests` / `OK`.

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_judge.py`

   Result: `Ran 86 tests` / `OK`.

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_analyzer.py`

   Result: `Ran 80 tests` / `OK`.

2. Full Python attribution suite:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`

   Result: `Ran 1052 tests in 29.266s` / `OK`.

3. Compile and diff checks:

   `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`

   Result: exit status 0.

   `git diff --check`

   Result: exit status 0.

## Files

- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/__init__.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `.superpowers/sdd/root-confirmation-task-2-report.md`

## Cumulative Review

The complete Task 2 range `bd8f1ab27..59822ecdf` received two independent
read-only reviews:

- Global Judge, candidate matrix, capsule, retrieval, and graph boundaries:
  CLEAN.
- Persistence, resume, publication, ownership, and report boundaries: CLEAN.

## Risks

No blocking risks.

The strict v8 schema intentionally invalidates pre-v8 cached global judgments and
requires external/offline judge implementations to populate the complete matrix.
Provider responses may therefore use the existing bounded repair path more often
until prompts settle; exhausted repairs remain a valid seed-bound inconclusive
fallback and cannot publish a root.
