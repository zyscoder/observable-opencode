# Root Confirmation Stability Task 2 Fix Wave 10 Report

Base commit: `469b1b161`

## Scope

Closed all four Important findings from the tenth whole-Task-2 review. The
changes remain offline, passive, read-only, dependency-free, and isolated from
agent behavior. No Task 3 behavior was added.

## Root Causes

1. Confirmation ownership treated every `rejected` status as definitive even
   when its validated counterfactual remained `unknown`; live construction,
   competitor reconciliation, report roles, and restore consequently disagreed
   about whether the candidate was resolved.
2. Authored-root exclusions enumerated known evidence/outcome records instead
   of classifying repository families, leaving `subagent.result` and
   `case.observed_success` root-eligible.
3. Selection enforced the Task 2 `input_defect_status != present` rule, but the
   assessment/counterfactual validator independently required `absent`.
4. Wave 9 strengthened serialized capsule edge facts without changing the
   capsule, envelope, judgment, report, cache/prompt, model/config, or checkpoint
   compatibility identities.

## RED

Added the finding-level regressions before production changes.

- The first exact batch ran 11 selectors and reported 8 failures and 4 errors.
  Two errors were incorrect unittest selectors; rerunning the corrected two
  selectors produced 2 expected failures. Across the 12 intended regressions,
  every finding reproduced.
- A separate uncertainty-contract test ran once and failed because an
  unknown-input root assessment could claim confidence `1.0`.
- The first full-suite run after the taxonomy fix ran 695 tests and failed two
  legacy fixture expectations that still labeled `subagent.result` as a causal
  factor.

The failures demonstrated unresolved rejection finality at live construction,
direct report validation, partial restore, and completed restore; both taxonomy
omissions; unknown-input matrix rejection; and unchanged persistence identities.

## Changes

- Added `is_definitive_confirmation()` as the shared status-resolution
  predicate. `confirmed` requires a causality-supporting counterfactual;
  `rejected` is definitive only with `rejects_causality`. Builders, backtracking,
  competitor reconciliation, ownership validation, rejected-factor validation,
  rejected-candidate validation, restore, and publication now use this rule.
- Unresolved rejections retain their confirmation audit identity but force the
  owner seed to `evidence_gap`/`inconclusive` with concrete
  `root_confirmation_unresolved` facts. They are not published as rejected
  candidates or factors and do not trigger terminal backtracking.
- Replaced one-off authored-root exclusions with structural case,
  observation, result, error, fact, support-assessment, and verification family
  rules. An exhaustive test derives event types from the canonical TypeScript
  contract and every recursive fixture. Authored prompt, decision, context
  transformation, and change records remain eligible; excluded records remain
  available as evidence.
- Aligned matrix role consistency with selection: a root candidate is allowed
  when input is `absent` or `unknown` and output is `present`. Input `present`
  remains contradictory. Unknown input must preserve uncertainty and cannot
  claim confidence `1.0`.
- Bumped the changed identities to capsule v3, validation envelope v3, global
  judgment v3, report v4, root-confirmation prompt v7, and checkpoint v4.
  Checkpoint config and CLI model identity now include global-judgment and
  root-confirmation behavior contracts; prompt versions invalidate stale judge
  cache keys.
- Current capsule, envelope, seed/report, partial checkpoint, and completed
  checkpoint roundtrips remain valid. Capsule/envelope v2, judgment v2, report
  v3, and checkpoint v3-shaped state fail at their compatibility boundaries.
  Existing report v2 migration remains conservative and publishes no roots.

## Verification

1. Finding-level regression set after implementation:
   - `Ran 12 tests in 0.048s` / `OK`.
2. Task 2-focused attribution set:
   - `Ran 396 tests in 2.309s` / `OK`.
3. Full Python attribution suite:
   - `Ran 695 tests in 4.502s` / `OK`.
4. Compile check:
   - `PYTHONPYCACHEPREFIX=/tmp/root-confirmation-fix10-pycache python3 -m compileall -q trace_attribution scripts tests`; exit status `0` from `tools/trace_attribution`.
5. Diff checks:
   - `git diff --check`; exit status `0`.
   - `git diff --check 469b1b161 --`; exit status `0`.

## Files

- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/causal_judge.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/cli.py`
- `tools/trace_attribution/trace_attribution/__init__.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- `tools/trace_attribution/tests/fixtures/recursive_cases/subagent_warning_ignored.json`
- Task 2-focused tests under `tools/trace_attribution/tests/`
- `.superpowers/sdd/root-confirmation-task-2-fix10-report.md`

## Residual Risk

- Structural evidence/outcome families intentionally fail closed. A future
  authored event whose canonical name ends in `.result`, `.error`, or a fact
  suffix requires an explicit taxonomy design change rather than silently
  becoming root-eligible.
- Report v3 and checkpoint v3 are explicitly incompatible with these semantics;
  callers must re-run attribution or use the existing conservative v2 report
  migration path.
