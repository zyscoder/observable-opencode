# Global Judge Convergence Implementation Plan

## Task 1: Root eligibility contract

1. Add failing tests for lifecycle exclusion.
2. Add failing tests for graph-aware `change` fallback behavior.
3. Add failing capsule tests proving persisted Global Judge eligibility follows
   the graph.
4. Implement the smallest taxonomy and graph-aware eligibility changes.
5. Run the focused retrieval and capsule suites.

## Task 2: Per-candidate Judge constraints

1. Add failing tests for a deterministic candidate comparison contract.
2. Add failing tests proving the contract appears in the initial prompt.
3. Add failing tests proving the same contract appears in repair constraints.
4. Implement one shared contract builder used by both call paths.
5. Run the focused Global Judge and causal Judge suites.

## Task 3: Negative-compression gate

1. Add failing metric tests for eligible and oversized payloads.
2. Add a failing analyzer test proving an oversized payload makes no Global
   Judge call and leaves recursive analysis active.
3. Implement deterministic gate metrics and non-terminal fallback routing.
4. Persist a compact offline gate event without classifying it as a failed
   Global pass.
5. Run focused recursive analyzer and checkpoint tests.

## Task 4: Regression and live validation

1. Run the complete trace-attribution test suite.
2. Run Python compilation checks.
3. Review the diff for online behavior changes and secret persistence.
4. Rerun the sanitized Sphinx fixture with the configured DeepSeek endpoint.
5. Compare open-root sets, request counts, payload sizes, terminal outcomes, and
   manual backward-taint conclusions with the prior run.
6. Write a concise iteration report and commit only the intended files.
