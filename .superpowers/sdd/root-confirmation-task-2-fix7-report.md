# Root Confirmation Stability Task 2 Fix Wave 7 Report

Base commit: `e85bd1c43`

## Scope

Closed all five Important findings from the seventh whole-Task-2 review. The
analysis remains offline, passive, read-only, dependency-free, and isolated
from agent behavior. No Task 3 evidence expansion behavior was added.

## Root Causes

1. Restored capsules froze their payloads but did not bind `candidate_ref` to
   the candidate fact, embedded node, downstream-path start, or resolved path
   reference.
2. Final and restored-report audits resolved path node identities but did not
   verify adjacent hops against the active graph or compare confirmed paths to
   the selected global assessment path.
3. Global authored-candidate discovery and root selection coerced capsule
   eligibility with `bool()`, admitting truthy strings and numbers.
4. Structured counterfactuals were internally consistent, but their causal
   effect was checked against role/status semantics only for selected roots.
5. Global selection and the two analyzer enqueue sites had no shared three-
   candidate ceiling or deterministic selected-reference normalization.

## RED

Added the eight targeted regression tests before production changes and ran
them as one exact reproduction set.

Result: `Ran 8 tests` / `FAILED (failures=20, errors=1)`.

The failures proved:

- all five candidate-identity substitutions restored successfully;
- malformed string, numeric, and null eligibility survived live and restored
  capsule construction;
- four candidates could be selected and selected order was payload-dependent;
- contradictory unselected-root and non-root counterfactuals were accepted;
- a real alternate path could replace the selected global assessment path at
  direct publication;
- completed-checkpoint restore accepted a disconnected recursive path; and
- `RecursiveAnalysisState` had no shared bounded enqueue defense.

## Changes

- `CandidateEvidenceCapsule.validate()` now binds the canonical candidate ref
  to the candidate fact, embedded node, downstream path start, and resolved
  candidate path reference during construction, envelope restore, and request
  validation. Eligibility must be an actual boolean.
- Global open-candidate discovery and selected-root validation use exact
  `is True` checks. Every assessment now validates root/non-root
  counterfactual semantics before selection or persistence.
- Global judgments reject more than three unique selected refs and normalize
  accepted selections lexicographically, making confirmation queue order
  independent of payload order.
- Final and completed-checkpoint publication audits verify confirmed
  confirmation/root paths against active graph endpoints and the shared
  non-temporal causal-edge policy, exact candidate/seed ownership, and selected
  global assessment paths.
- Both global and recursive enqueue paths now use one state-level method that
  rejects ineligible, duplicate, and over-limit candidates. Restored state also
  rejects queues containing more than three unique candidates for a seed.

## Verification

1. Exact targeted regression set:
   - `Ran 8 tests in 0.047s` / `OK`.
2. Task 2-focused attribution set:
   - `Ran 318 tests in 1.649s` / `OK`.
3. Full Python attribution suite:
   - `Ran 674 tests in 3.557s` / `OK`.
4. Compile check:
   - `python3 -m compileall -q` over attribution source, scripts, and tests;
     exit status `0`.
5. Working-tree diff check:
   - `git diff --check`; exit status `0`.
6. Required committed base-range diff check:
   - `git diff --check e85bd1c43..HEAD`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_evidence_capsule.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `.superpowers/sdd/root-confirmation-task-2-fix7-report.md`

## Residual Risk

- Conservative unknown/rejected confirmation records may retain an invalid
  attempted path for auditability, but they cannot publish a confirmed root.
- Lexicographic ordering stabilizes the selected confirmation set without
  importing retrieval rank or score into confirmation facts.
