# Fusion C P0 Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct misleading derived Trace facts and make the recursive attribution engine traverse and judge the strongest trace-visible defect-introduction candidates with a smaller, auditable protocol.

**Architecture:** Preserve passive collection and Causal IR compatibility. Fix semantic derivation at its source in `case-trace.ts`, then repair offline progress-window reconstruction and use deterministic candidate/root-eligibility policy around the LLM Judge. The LLM remains responsible for semantic defect and propagation judgments; deterministic code controls only fact consistency, navigation, and protocol validity.

**Tech Stack:** TypeScript/Bun tests for Trace generation; Python 3 `unittest` for offline attribution; existing Causal IR and Claude-compatible Judge transport.

## Global Constraints

- Trace instrumentation remains passive, post-hoc, and invisible to Agent behavior.
- Existing raw artifacts and recorded facts are preserved; only incorrect derived semantics are changed.
- Navigation aggregates such as `progress.episode` are never reportable roots.
- Every production-code change must be preceded by a failing regression test.
- Unrelated untracked workspace files must not be staged or modified.

---

### Task 1: Verification Fact Consistency

**Files:**
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**
- Consumes: recorded command, stdout, stderr, explicit status, and exit code.
- Produces: `verification` records whose status cannot be changed to failed by a location-only output line.

- [ ] Add a failing test where pytest output contains a source location, `11 passed`, and exit code `0`; assert passed status and no parsed failure.
- [ ] Run the focused Bun test and verify the new assertion fails because a location-only record is currently emitted.
- [ ] Require an explicit failure marker before attaching a location to a parsed failure.
- [ ] Add a consistency guard so explicit pass summaries plus exit code `0` cannot become failed without an assertion/error/failure marker.
- [ ] Run the focused and full `case-trace.test.ts` suites.

### Task 2: Verification Attempt Classification And Claim Integrity

**Files:**
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**
- Consumes: shell command and final response text.
- Produces: verification-attempt semantics for package-runner test commands and parenthesis-complete response claims.

- [ ] Add a failing test for `npx mocha ... | head` that expects a verification attempt with unknown result reliability, not missing verification.
- [ ] Add a failing test for a sentence containing `(i.e., ...)` and assert it remains one response claim.
- [ ] Extend passive command classification and mark exit status as pipeline-masked when the final pipeline consumer can hide the test runner's exit code.
- [ ] Make sentence splitting track balanced parentheses and merge punctuation-leading continuations.
- [ ] Run focused and full Trace tests.

### Task 3: Delivery-Boundary Candidate Reconstruction

**Files:**
- Modify: `tools/trace_attribution/tests/test_backward_taint.py`
- Modify: `tools/trace_attribution/trace_attribution/progress.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_retrieval.py`

**Interfaces:**
- Consumes: linked offline progress episodes.
- Produces: a delivery-bounded window that includes the immediately preceding delivery episode's concrete candidate members while retaining its boundary metadata.

- [ ] Add a failing test proving the previous delivery episode's reasoning and authored mutation action are returned as candidates.
- [ ] Run the focused Python test and verify those refs are absent.
- [ ] Include previous-delivery candidate members, tag their boundary role, and preserve deterministic chronological ordering.
- [ ] Run focused progress and backward-taint tests.

### Task 4: Sparse Judge Protocol And Root Eligibility

**Files:**
- Modify: `tools/trace_attribution/tests/test_causal_judge.py`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

**Interfaces:**
- Consumes: deterministic candidate shortlist and recursive semantic context.
- Produces: sparse ranked predecessor assessments, internal retry diagnostics that do not become tool investigations, and root confirmation comparisons restricted to independently root-eligible hypotheses.

- [ ] Add failing tests for a sparse predecessor response, a `judge_retry` no-op, a navigation aggregate root rejection, and root-eligible competitor filtering.
- [ ] Verify each test fails for the current behavior.
- [ ] Limit each causal step to the strongest 8 structurally ranked candidates while retaining deterministic pagination metadata.
- [ ] Permit sparse predecessor assessments and record unselected offered refs deterministically.
- [ ] Reject contradictory status/reason payloads before they enter recursive state.
- [ ] Treat `judge_retry` as an internal diagnostic, not an investigation directive.
- [ ] Filter root confirmation competitors to supported, root-eligible introduction hypotheses.
- [ ] Run focused and full Python attribution suites.

### Task 5: Benchmark Regression

**Files:**
- Modify: `docs/superpowers/reports/2026-07-21-open-source-benchmark-fusion-c-evaluation.md` only if new measured results are available.

**Interfaces:**
- Consumes: the five existing open-source benchmark Trace bundles and the current authorized Provider configuration.
- Produces: comparable attribution JSON and manual-versus-automatic findings.

- [ ] Run TypeScript and Python full suites.
- [ ] Re-run Gin, Axios, Astropy, TerminalBench, and Pydantic attribution against the same inputs.
- [ ] Compare decisiveness, root safety/recall, visited refs, unresolved refs, protocol failures, and repair traffic against the previous baseline.
- [ ] Record measured improvements and remaining gaps without claiming a collector behavior that was not freshly exercised.
