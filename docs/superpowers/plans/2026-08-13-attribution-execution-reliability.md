# Attribution Execution Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent oversized attribution Judge requests, dynamically fit candidate pages, and report execution failures separately from semantic evidence gaps.

**Architecture:** Keep canonical Causal IR and validation envelopes complete. Introduce a deterministic prompt-budget module and bounded Judge projection at the LLM boundary, use that budget to split Global Judge pages, classify stable provider overflow as non-retryable, and project typed execution failures into reports.

**Tech Stack:** Python 3 standard library, Anthropic Python SDK, `unittest`, existing recursive attribution engine and checkpoint schemas.

## Global Constraints

- Attribution remains an offline passive sidecar and never changes Agent behavior.
- Complete trace facts and validation envelopes remain lossless.
- No physical request may be made when local preflight predicts context overflow.
- Provider context overflow is non-retryable.
- Semantic evidence gaps and execution failures remain separate result categories.
- All production changes follow test-first red-green cycles.

---

### Task 1: Judge Context Budget and Provider Classification

**Files:**
- Create: `tools/trace_attribution/trace_attribution/judge_budget.py`
- Modify: `tools/trace_attribution/trace_attribution/request.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/errors.py`
- Test: `tools/trace_attribution/tests/test_judge_budget.py`
- Test: `tools/trace_attribution/tests/test_causal_judge.py`

**Interfaces:**
- Produces: `JudgeContextBudget`, `PromptBudgetMeasurement`, `estimate_prompt_tokens()`, and `JudgeContextBudgetExceeded`.
- Consumes: `system`, Anthropic message dictionaries, configured context window, output reserve and safety margin.

- [x] Write failing tests proving invalid budgets are rejected, oversized prompts fail with zero physical requests, and context-window HTTP 400 is non-retryable.
- [x] Run the focused tests and verify failures are caused by missing budget behavior.
- [x] Implement deterministic conservative estimation and immutable budget/measurement types.
- [x] Add CLI options `--judge-context-window-tokens` and `--judge-context-safety-margin-tokens` and validate them in `AttributionOptions`.
- [x] Add transport preflight before incrementing `request_count` or invoking the SDK.
- [x] Classify structured or message-derived context overflow as non-retryable `context_window_exceeded`.
- [x] Run focused tests and the existing provider-classification suite.

### Task 2: Bounded Global Judge Projection

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- Modify: `tools/trace_attribution/trace_attribution/global_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Test: `tools/trace_attribution/tests/test_global_judge.py`
- Test: `tools/trace_attribution/tests/test_evidence_capsule.py`

**Interfaces:**
- Produces: `GlobalCandidateJudgeRequest.judge_prompt_projection()` and a projection diagnostics object containing schema, identity, retained sections, omitted sections and byte counts.
- Consumes: canonical `GlobalCandidateJudgeRequest` and candidate capsules.

- [x] Write failing tests with large repeated capsule fields proving the Judge projection is bounded while the validation envelope remains byte-for-byte complete.
- [x] Verify the tests fail because the current prompt serializes the full request.
- [x] Implement schema-versioned candidate and request prompt projections with explicit omission manifests and canonical hashes.
- [x] Update `build_global_candidate_prompt()` to use only the prompt projection and comparison contract derived from that projection.
- [x] Ensure validators and checkpoint envelopes continue using the canonical request.
- [x] Run focused projection, Global Judge and capsule tests.

### Task 3: Token-Aware Dynamic Candidate Paging

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/candidate_paging.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_global_pagination_integration.py`
- Test: `tools/trace_attribution/tests/test_candidate_paging.py`

**Interfaces:**
- Produces: deterministic dynamic page plans with budget measurements and split provenance.
- Consumes: projected Global Judge requests and `JudgeContextBudget`.

- [x] Write failing tests proving an oversized eight-candidate page splits deterministically and preserves ordered exact candidate coverage.
- [x] Write a failing test proving a minimal single-candidate overflow ends locally with zero physical requests.
- [x] Add a dynamic fit pass after deterministic count-based planning and before page execution.
- [x] Persist page budget, canonical request envelope, request/projection identity, parent identity and split reason in plan/checkpoint events.
- [x] Rebuild every planned request against the active graph, replay split lineage, and bind convergence to the final durable plan.
- [x] Reject checkpoint replay when budget or projection policy differs.
- [x] Run paging unit and integration suites.

### Task 4: Typed Execution Failure Reporting

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/service.py`
- Modify: `tools/trace_attribution/trace_attribution/request.py`
- Test: `tools/trace_attribution/tests/test_recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_question_attribution.py`

**Interfaces:**
- Produces: `analysis_execution_failures`, seed outcome `execution_failed`, report `analysis_outcome=execution_failed`, and question-projection execution gaps.
- Consumes: typed local budget failures and provider failure dispositions.

- [x] Write failing tests proving a failed first Judge page cannot become `evidence_gap` and that mixed completed/failed seeds preserve completed semantic results.
- [x] Add structured execution-failure state to recursive analysis and checkpoint serialization.
- [x] Map local and provider context overflow to `analysis_execution_failed/context_window_exceeded`.
- [x] Update question-bound conclusions to state that semantic attribution did not complete.
- [x] Keep genuine missing trace facts under `missing_evidence` and semantic `evidence_gap`.
- [x] Run recursive analyzer and question-attribution suites.

### Task 5: Regression and Large-Payload Verification

**Files:**
- Modify: `tools/trace_attribution/README.md`
- Modify: `docs/superpowers/reports/2026-08-13-yocto-expectation-gap-temporary-log.md`
- Test: `tools/trace_attribution/tests/test_judge_budget.py`

**Interfaces:**
- Consumes: all interfaces produced by Tasks 1-4.
- Produces: documented configuration, diagnostics and recovery instructions.

- [x] Add a synthetic request whose canonical payload exceeds 200k estimated tokens and assert every emitted physical request fits the configured budget.
- [x] Run focused causal Judge, Global Judge and pagination test modules.
- [x] Run `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`.
- [x] Document budget CLI options, execution-failure output and how to rerun the Yocto trace with a fresh checkpoint/output path.
- [x] Run `git diff --check` and review the complete diff for trace loss or Agent behavior changes.
- [x] Bind planned requests to run-level perspective, seed-derived obligations, and evidence-context authority.
- [x] Replay `round summary -> follow-up plan -> convergence` candidate lineage and reject coordinated re-signing.
- [x] Persist and remeasure canonical full-comparison preflight facts when a large finalist set stalls or overflows.
- [x] Derive evidence-context authority from graph-replayed manifest paths and reconstructed omission gaps instead of trusting the request funnel.
- [x] Bind convergence terminal status to durable page facts and bind final-comparison execution failures to their canonical preflight measurement.
- [x] Require report-level execution-failure metadata to bijectively match per-seed execution failures.
- [x] Commit the implementation on `codex/attribution-execution-reliability`.
