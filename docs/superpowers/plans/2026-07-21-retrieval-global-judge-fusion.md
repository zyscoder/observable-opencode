# Retrieval + Global Judge Fusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a high-recall candidate evidence closure and one global LLM comparison pass, while retaining recursive expansion and independent root confirmation as bounded fallbacks.

**Architecture:** A deterministic evidence-capsule builder projects candidate-local causal context from Causal IR. An optional bounded Global Judge compares all capsules, returns roots/no-defect/expansion, and feeds selected hypotheses into the existing recursive and confirmation lifecycle. Legacy recursive behavior remains the fallback.

**Tech Stack:** Python 3, dataclasses, existing TraceGraph/Causal IR, unittest, Anthropic-compatible ClaudeJudgeClient.

## Global Constraints

- Offline analysis only; no attribution output may alter Agent behavior.
- Retrieval rank is navigation evidence, never a causal verdict.
- Candidate loss must be recoverable through explicit recursive expansion.
- `no_defect` requires grounded decisive evidence.
- Existing recursive analysis remains the compatibility fallback.

---

### Task 1: Candidate Evidence Capsules

**Files:**
- Create: `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- Create: `tools/trace_attribution/tests/test_evidence_capsule.py`

**Interfaces:**
- Consumes: `TraceGraph`, `CausalCandidate`, `DefectState`, `FrontierItem`.
- Produces: `CandidateEvidenceCapsule`, `build_candidate_evidence_capsules(...)`.

- [ ] Write tests for candidate semantics, downstream path, action-group members, grounded evidence, and missing artifact reporting.
- [ ] Run `python3 -m unittest tools.trace_attribution.tests.test_evidence_capsule -v` and verify the new import/behavior fails.
- [ ] Implement immutable JSON-serializable evidence capsules and deterministic compression metrics.
- [ ] Run the focused test and existing retrieval tests.

### Task 2: Global Judge Contract

**Files:**
- Create: `tools/trace_attribution/trace_attribution/global_judge.py`
- Create: `tools/trace_attribution/tests/test_global_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`

**Interfaces:**
- Consumes: `GlobalCandidateJudgeRequest` containing defect facts and evidence capsules.
- Produces: `GlobalCandidateJudgment` with outcome, candidate assessments, selected refs, expansion requests, decisive evidence, and confidence.

- [ ] Write failing tests for valid roots, valid no-defect, valid expansion, unknown refs, contradictory status, and missing decisive evidence.
- [ ] Run the focused tests and confirm schema failures are caused by missing implementation.
- [ ] Implement dataclasses, prompt generation, strict factual validation, and bounded Provider execution using the existing repair/cache path.
- [ ] Run global-Judge and causal-Judge tests.

### Task 3: Fusion Orchestration

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`
- Modify: `tools/trace_attribution/tests/test_recursive_analyzer.py`

**Interfaces:**
- Consumes: initial frontier, candidate capsules, optional `GlobalJudgeCapability`.
- Produces: global candidate pre-pass, selected recursive anchors, no-defect termination, fallback recursion, and report metadata.

- [ ] Write failing tests proving global roots are prioritized, no-defect terminates without recursive Judge calls, expansion routes only requested anchors, and unsupported Judges preserve legacy behavior.
- [ ] Run focused tests and verify all four behaviors fail for the intended reason.
- [ ] Add bounded fusion pre-pass, checkpoint-safe state fields, request accounting, fallback semantics, and CLI switch `--fusion-mode` with default `retrieval-global` for recursive-agentic analysis.
- [ ] Run recursive analyzer, CLI, checkpoint, and report tests.

### Task 4: Open-Source Benchmark Regression

**Files:**
- Create: `docs/superpowers/reports/2026-07-21-retrieval-global-judge-fusion-results.md`

**Interfaces:**
- Consumes: Axios, Astropy, TerminalBench benchmark traces and current provider configuration.
- Produces: human/global/recursive comparison and next-iteration findings.

- [ ] Run the full Python test suite.
- [ ] Re-run Axios, Astropy, and TerminalBench with fusion mode and fresh output/cache paths.
- [ ] Measure candidate/action-group recall, node and byte compression, physical/logical calls, no-defect accuracy, root confirmation, and expansion behavior.
- [ ] Compare results with the prior recursive-only baseline and document semantic Trace gaps separately from attribution gaps.
