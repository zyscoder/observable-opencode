# Offline Progress Episode Reconstruction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make observed defects and final responses reach meaningful Agent decision episodes while preserving passive offline attribution.

**Architecture:** Add a deterministic offline progress reconstruction module, inject synthetic `progress.episode` nodes and selective edges into `TraceGraph`, then teach backward analysis and the LLM Judge to treat episodes as non-root semantic aggregates. Validate evaluation assertions with the production LLM Judge and expose minimal Judge telemetry.

**Tech Stack:** Python 3.9+, `unittest`, existing `trace_attribution` package, Anthropic-compatible Judge API.

## Global Constraints

- Do not change Agent prompts, tool behavior, task scheduling, or runtime execution.
- Do not write reconstructed records back to `trace.json`.
- Mark every reconstructed node `offline_only=true` and `behavior_impact=none`.
- Do not promote temporal adjacency into a root cause.
- Use TDD for every behavioral change.

---

### Task 1: Reconstruct progress episodes

**Files:**
- Create: `tools/trace_attribution/trace_attribution/progress.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: `Dict[str, TraceNode]` and `message_lineage["turns"]`.
- Produces: `reconstruct_progress_episodes(nodes, turns) -> ProgressReconstruction` containing synthetic nodes, target projections, and stats.

- [x] Write a failing test whose evaluation node cannot reach a prior no-progress decision episode.
- [x] Run the focused test and verify failure because `progress.episode` does not exist.
- [x] Implement turn filtering, deterministic summaries, phase/progress counters, and previous-episode refs.
- [x] Integrate synthetic nodes and edges into `TraceGraph.from_trace()` without mutating source Trace data.
- [x] Run focused graph tests and verify episode membership, chaining, and target projection.

### Task 2: Prioritize semantic progress during traversal

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: `progress.episode` nodes from Task 1.
- Produces: ordered upstream candidates and concrete member expansion for active defect branches.

- [x] Write failing tests for evaluation/response upstream priority and episode member expansion.
- [x] Run the focused tests and verify lifecycle/context records currently outrank semantic progress.
- [x] Add progress-aware upstream ordering and episode predecessor expansion.
- [x] Add the `offline_progress_aggregate` Judge role and prohibit aggregate roots.
- [x] Add progress fields to compact semantic payloads.
- [x] Run the focused tests and verify decisions are visited without making the aggregate a root.

### Task 3: Validate evaluation assertions with the production LLM Judge

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: optional `judge_evaluation_assertion(...) -> NodeJudgment`; `ClaudeJudgeClient` implements it by invoking `judge_node()`.

- [x] Write a failing test where an evaluation-aware Judge rejects a false observed defect.
- [x] Verify the current analyzer bypasses the Judge and reports the assertion present.
- [x] Route production evaluation assertions through the optional validation method while preserving deterministic fallback for existing test doubles.
- [x] Verify an absent evaluation assertion terminates as `no_defect` and a present assertion continues backward.

### Task 4: Add Judge invocation telemetry

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: report metadata fields `judge_model` and `judge_request_count`.

- [x] Write a failing metadata test.
- [x] Add a request counter around every Anthropic-compatible request and expose the configured model.
- [x] Run the focused test and verify credentials and prompt bodies are not persisted.

### Task 5: Full regression and real-trace comparison

**Files:**
- Modify: `docs/superpowers/reports/2026-07-17-progress-episode-attribution-results.md`

**Interfaces:**
- Consumes: existing TerminalBench, Pydantic, Sphinx, Gin, Axios, and Astropy Trace files.
- Produces: graph reachability and LLM attribution comparison against frozen manual labels.

- [x] Run `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint`.
- [x] Rebuild graphs for the three failure traces and assert manually labeled decision refs are reachable within configured limits.
- [ ] Re-run DeepSeek-backed offline attribution for all six controls with the existing reviews and frozen manual labels.
- [x] Compare the three failure traces and targeted root judgments; the full six-case comparison remains pending after API connection failures.
- [x] Save the partial result report without changing Ground Truth labels after observing the new output.
