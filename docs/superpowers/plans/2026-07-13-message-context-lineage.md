# Message Context Lineage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconstruct offline agent-turn and message-context lineage and make causal roles authoritative for backward root-cause attribution.

**Architecture:** A new reconstruction module builds deterministic turn, snapshot, and provenance-edge records from the existing trace and artifacts. `TraceGraph` merges only confirmed/content-matched reconstructed edges into attribution traversal. The judgment schema separates evidence, propagation, and introduction so faithful failure observations cannot become roots.

**Tech Stack:** Python 3.9 standard library, existing Anthropic-compatible judge client, `unittest`, observable-opencode semantic trace JSON.

## Global Constraints

- Reconstruction is passive and offline; it must not modify or feed data into agent execution.
- Existing trace files remain unchanged.
- Temporal-only inferred edges cannot establish a root cause.
- Large message bodies remain artifact-backed.
- The old Pydantic trace is the required regression gate before any new benchmark case.

---

### Task 1: Causal Role Contract

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `NodeJudgment.causal_role: str`
- Produces: `CAUSAL_ROLES`
- Consumes: legacy `defect_status`, `influenced_by`, and `is_root_cause`

- [x] Add failing tests proving evidence nodes and present non-root dead ends are never promoted to roots.
- [x] Add failing validation tests for contradictory causal roles.
- [x] Implement causal-role parsing and conservative legacy mapping.
- [x] Require root candidates to be explicit `defect_introduction` nodes.
- [x] Run the complete Python unit suite.

### Task 2: Missing-Semantic Isolation

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/trace_improvement.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: `case.missing_semantic` start records
- Produces: inconclusive trace-gap judgments with no defect propagation

- [x] Add a failing test where a missing-semantic record cites code changes.
- [x] Verify the current analyzer incorrectly visits or roots the changes.
- [x] Treat missing semantics as an unknown observability boundary.
- [x] Preserve the corresponding trace-improvement recommendation.
- [x] Run focused and complete Python tests.

### Task 3: Turn And Message Reconstruction

**Files:**
- Create: `tools/trace_attribution/trace_attribution/reconstruction.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `reconstruct_message_lineage(trace, nodes, aliases, artifact_root)`
- Produces: `MessageLineage` JSON with `turns`, `snapshots`, `edges`, and `stats`
- Produces: attribution-eligible reconstructed edges

- [x] Add a failing test for same-message reasoning-to-action reconstruction.
- [x] Add a failing test for session/message/call identifier extraction.
- [x] Implement deterministic indexes and turn grouping.
- [x] Emit normalized prompt/context/compaction/LLM snapshots.
- [x] Merge confirmed reconstructed edges into `TraceGraph`.
- [x] Run focused and complete Python tests.

### Task 4: Artifact-Backed Context Retention

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/reconstruction.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: LLM request message artifacts and earlier decision rationale text
- Produces: `retained_in_context` content-matched edges

- [x] Add a failing artifact test containing a prior reasoning decision in a later LLM request.
- [x] Implement safe artifact loading under the trace root.
- [x] Match normalized exact decision content and reject short/ambiguous snippets.
- [x] Prioritize recent matched decisions in LLM upstream context.
- [x] Verify truncated or missing artifacts produce gaps instead of edges.

### Task 5: Lineage Output And CLI

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/README.md`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `--lineage-out`
- Produces: default `<attribution-output-stem>.message-lineage.json`
- Produces: attribution metadata `message_lineage`

- [x] Add CLI parsing and report-metadata tests.
- [x] Write the reconstructed lineage without altering the source trace.
- [x] Document edge evidence tiers and passive behavior.
- [x] Run the complete Python unit suite.

### Task 6: Prior Trace Regression

**Files:**
- Create: `docs/superpowers/reports/2026-07-13-loop4-message-lineage-review.md`
- Create: `docs/superpowers/reports/2026-07-13-loop4-prior-trace-after-c.json`

**Interfaces:**
- Consumes: prior Pydantic `trace.json` and review JSON
- Produces: old/new attribution comparison and acceptance result

- [x] Build lineage for the prior trace and verify `dec_1093` is reachable.
- [x] Run DeepSeek V4 Pro attribution with one-hour per-request timeout.
- [x] Confirm pytest evidence and missing-semantic code changes are not roots.
- [x] Confirm the analyzer visits `dec_1093` or report the precise remaining blocker.
- [x] Run `git diff --check`, all Python tests, TypeScript typecheck, and focused trace tests.
