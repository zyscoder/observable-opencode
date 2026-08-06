# Process-Defect Navigation Implementation Plan

1. Add failing regressions for navigation candidates beyond the old top-two
   and top-eight limits.
2. Add prompt and transformed-defect contract tests for candidate-local process
   defects and responsible non-repair.
3. Route the full bounded progress candidate page while preserving all existing
   root-confirmation gates.
4. Run focused recursive-analyzer and causal-Judge tests, then the full Python
   suite.
5. Re-run the same neutral Sphinx FeatureBench Bundle with
   `deepseek-v4-flash`; compare candidate recall, node judgments, confirmed
   roots, validation failures, request count, and manual agreement.
