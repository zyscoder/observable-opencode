# Trace Provenance v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework observable-opencode case trace output into a provenance facts layer for offline attribution analysis.

**Architecture:** Keep the existing trace writer and component instrumentation, but change the primary bundle from Causal Trace v2 to Trace Provenance v3. The writer emits fact records and dataflow edges, while large payloads remain artifact-backed and the viewer presents source/dataflow inspection without diagnosis language.

**Tech Stack:** TypeScript, Bun test, existing opencode observability modules.

## Global Constraints

- Trace must not perform root-cause attribution or diagnosis.
- Primary bundle is `provenance-trace.json` with `trace_version: "3.0"`.
- Use `source_refs`, `records`, `dataflow_edges`, and `response_segments`.
- Do not output `diagnostics_hints`, `final_response_evidence`, `final.claim`, or `evidence_refs` in the primary v3 bundle.
- Preserve secret redaction and artifact-backed large text behavior.

---

### Task 1: Add v3 Schema Tests

**Files:**
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: Existing `CaseTrace` test harness.
- Produces: Failing tests for `provenance-trace.json`, `responseOutput`, `source_refs`, `records`, and `dataflow_edges`.

- [x] Write failing tests for v3 bundle creation.
- [x] Verify tests fail before implementation.

### Task 2: Implement Provenance Writer

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**
- Produces: `CaseTrace.responseOutput(input)`, v3 manifest, v3 provenance summary, source ref normalization.

- [x] Add v3 types and `TraceResponseSegment`.
- [x] Write `provenance-trace.json` and `partial/latest.json` as v3 summaries.
- [x] Map internal edges to dataflow relations.
- [x] Keep `finalEvidence` as a compatibility wrapper around `responseOutput`.

### Task 3: Migrate Component Call Sites

**Files:**
- Modify: `packages/opencode/src/session/processor.ts`
- Modify: `packages/opencode/src/session/compaction.ts`
- Modify: `packages/opencode/src/session/prompt.ts`
- Modify: `packages/opencode/src/mcp/index.ts`
- Modify: `packages/opencode/src/tool/tool.ts`
- Modify: `packages/opencode/src/tool/skill.ts`
- Modify: `packages/opencode/src/tool/task.ts`

**Interfaces:**
- Consumes: `source_refs` and `responseOutput`.
- Produces: Component records that avoid diagnosis/evidence terminology.

- [x] Use `responseOutput` for assistant text.
- [x] Keep compaction summaries under context/observation records.
- [x] Rename component references from `evidence_refs` to `source_refs`.

### Task 4: Update Viewers And Docs

**Files:**
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`
- Modify: `packages/opencode/src/observability/case-trace-html.ts`
- Modify: `docs/observable-benchmark-trace.md`
- Create: `docs/superpowers/specs/2026-06-29-trace-provenance-v3-design.md`

**Interfaces:**
- Produces: HTML sections named `Component Dataflow`, `Execution Timeline`, `IO Inspector`, `Context Ledger`, and `Artifacts`.

- [x] Rename viewer copy away from causal/evidence language.
- [x] Render v3 records and dataflow edges.
- [x] Document v3 files, fields, and non-goals.

### Task 5: Verify

**Files:**
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: Passing targeted tests and typecheck.

- [x] Run `bun test test/observability/case-trace.test.ts --timeout 30000`.
- [x] Run package typecheck.
- [x] Scan for stale output terminology in generated v3 bundle paths.
