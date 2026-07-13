# Loop 2 Trace and Attribution Joint Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate false defect roots on successful stress cases while enriching compaction and response-quality trace semantics needed by offline attribution.

**Architecture:** TypeScript trace/review code remains passive and records or detects facts only after normal agent behavior. The Python attribution module validates whether an evaluation assertion is real, represents uncertainty explicitly, and reports why traversal stopped. Existing fields remain readable, but Ground Truth is no longer treated as proof of an observed defect.

**Tech Stack:** TypeScript, Node test runner, Python 3 dataclasses, unittest, observable-opencode trace schema 5.5.

## Global Constraints

- Trace collection must remain passive and must never feed derived semantics back to the agent.
- Each judge or JSON-repair request defaults to a 3600-second timeout.
- Successful cases must not produce fabricated root causes.
- Missing or malformed judge fields must become `unknown`, not implicit `False` or a root cause.
- Implement every behavior change with a failing regression test first.

---

### Task 1: Response Quality and Compaction Trace Facts

**Files:**
- Modify: `packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`
- Modify: `packages/opencode/test/observability/stress-cases/lib/stress-review.mjs`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/session/compaction.ts`

**Interfaces:**
- Consumes: existing `response.output`, `TraceContextLedger`, and `CompactionRecordInput` fields.
- Produces: quality evidence that includes full response outputs, plus `algorithm_version`, `input_message_count`, `compaction_request_message_count`, and `output_message_count` compaction facts.

- [ ] **Step 1: Add failing response-output quality test**

Add a semantic-quality trace whose only risk statement is in `response.output` and assert `risk_assessment` is found and the quality score is complete.

- [ ] **Step 2: Run the stress review test and verify RED**

Run: `node --test packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`

Expected: the new assertion fails because `risk_assessment` excludes `response.output`.

- [ ] **Step 3: Include `response.output` in answer-semantic detectors**

Use one helper predicate for `response.output`, `response.claim`, `decision`, and `design.record`; keep evidence-fact-only detectors unchanged.

- [ ] **Step 4: Add failing compaction ledger test**

Extend an existing compaction test to require:

```ts
expect(compaction.data.algorithm_version).toBe("head-tail-summary/v1")
expect(compaction.data.input_message_count).toBeGreaterThan(0)
expect(compaction.data.compaction_request_message_count).toBeGreaterThan(0)
expect(compaction.data.output_message_count).toBeGreaterThan(0)
```

- [ ] **Step 5: Run the focused trace test and verify RED**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts -t "compaction"`

Expected: new fields are absent.

- [ ] **Step 6: Record compaction counts without changing compaction behavior**

Extend `TraceContextLedger` and `CompactionRecordInput`, pass counts already available in `session/compaction.ts`, and emit the fields at the top level and in `context_ledger`.

- [ ] **Step 7: Run TypeScript tests and verify GREEN**

Run the focused trace and stress review tests. Expected: PASS.

### Task 2: Separate Designed Ground Truth from Observed Defects

**Files:**
- Modify: `tools/trace_attribution/tests/test_backward_taint.py`
- Modify: `tools/trace_attribution/trace_attribution/quality_review.py`

**Interfaces:**
- Consumes: stress review `ground_truth_root_cause`, `quality_review.quality_gaps`, and `missing_semantics`.
- Produces: offline evaluation records only for actually observed quality gaps or missing semantics; Ground Truth remains metadata.

- [ ] **Step 1: Replace the old Ground-Truth injection expectation**

Assert that a review containing only `ground_truth_root_cause` does not create `case.observed_defect`, and that default starts fall back to final response claims.

- [ ] **Step 2: Run the focused Python test and verify RED**

Run the renamed test with unittest. Expected: the old injected observed-defect node is still present.

- [ ] **Step 3: Remove unconditional observed-defect injection**

Delete the `if not gaps and not missing_semantics` injection path. Preserve quality-gap and missing-semantic injection.

- [ ] **Step 4: Run focused tests and verify GREEN**

Expected: Ground Truth alone never becomes an observed defect.

### Task 3: Tri-State Judgments and Evaluation Assertion Rejection

**Files:**
- Modify: `tools/trace_attribution/tests/test_backward_taint.py`
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/trace_improvement.py`

**Interfaces:**
- Produces: `NodeJudgment.defect_status`, report `analysis_outcome`, and `termination_reason`.
- Status values: `present`, `absent`, `unknown`.
- Outcome values: `root_found`, `no_defect`, `inconclusive`.
- Termination values: `queue_exhausted`, `depth_limit`, `node_limit`.

- [ ] **Step 1: Add failing tri-state parsing tests**

Assert missing `has_defect`/`defect_status` parses as `unknown`; explicit true/false maps to `present`/`absent` for compatibility.

- [ ] **Step 2: Add failing fallback and report-outcome tests**

Assert judge failure produces `unknown` and no root, successful starts produce `no_defect`, and unknown starts produce `inconclusive`.

- [ ] **Step 3: Add failing evaluation-rejection test**

When an evaluation start is judged present but all cited upstream nodes are absent, assert the evaluator is changed to absent and is not retained as a root.

- [ ] **Step 4: Run focused tests and verify RED**

Expected: current Boolean fallback and boundary-preservation behavior fails all new assertions.

- [ ] **Step 5: Implement tri-state compatibility**

Add `defect_status` while retaining `has_defect` in serialized reports. Normalize explicit status first, then legacy Boolean, otherwise `unknown`.

- [ ] **Step 6: Validate judge payloads and mark irreparable output unknown**

Require a defect status/Boolean, `influenced_by`, `is_root_cause`, and `confidence`. Retry malformed or incomplete output once; if still invalid, analyzer fallback is unknown rather than defective.

- [ ] **Step 7: Reject unsupported evaluation assertions**

Replace the existing evaluator-boundary root preservation with an absent judgment when every evaluated upstream node is absent. Preserve `inconclusive` when any upstream is unknown.

- [ ] **Step 8: Emit analysis outcome and termination reason**

Derive outcome from roots and statuses and record whether the queue ended normally or hit a configured bound.

- [ ] **Step 9: Run all attribution tests and verify GREEN**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -v`

Expected: PASS.

### Task 4: Loop 2 Regression Gate

**Files:**
- Modify: `docs/superpowers/reports/2026-07-13-pre-loop2-attribution-timeout-review.md` only if verified results differ from the planned acceptance gates.

**Interfaces:**
- Consumes: the four Loop 1 traces and their review files.
- Produces: comparable attribution JSON under `/tmp/observable-opencode-loop2/`.

- [ ] **Step 1: Re-run deterministic review on the four traces**

Expected: architecture risk assessment is found and no false quality gap is generated.

- [ ] **Step 2: Re-run offline attribution with 3600-second timeout**

Use `max_depth=8`, `max_nodes=48`, and DeepSeek-V4-Pro.

- [ ] **Step 3: Compare against manual analysis**

Expected: all four successful cases report `analysis_outcome=no_defect` and zero root causes.

- [ ] **Step 4: Run full local verification**

Run Python tests, stress review tests, focused trace tests, syntax checks, and `git diff --check`.

- [ ] **Step 5: Present Loop 2 result and next-loop proposal**

Do not push or trigger release until requested by the user or until the loop reaches its explicit publish step.
