# Semantic Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a semantic evidence layer to observable opencode trace so each case can support root-cause attribution without automatic diagnosis.

**Architecture:** Extend `CaseTrace` with first-class semantic arrays and APIs, then instrument LLM input, processor events, tool execution, edit/bash results, and compaction. Render the new semantic data in static `trace.html`.

**Tech Stack:** TypeScript, Bun test, existing opencode `CaseTrace`, static HTML rendering.

## Global Constraints

- Keep existing `events.jsonl`, `trace.json`, `trace.html` outputs.
- Keep artifact-backed storage for large text.
- Do not store secrets in trace or artifacts.
- Do not implement automatic root-cause diagnosis.
- Preserve compatibility with existing span/event consumers.

---

### Task 1: Extend Trace Schema and Core APIs

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Produces: `CaseTrace.contextSnapshot`, `decision`, `edge`, `verification`, `change`, `constraint`, `finalEvidence`
- Produces: `TraceSummary.trace_version` value `"1.1"`

- [ ] Add failing tests for semantic arrays, artifact-backed context, and redaction.
- [ ] Implement semantic types and storage arrays.
- [ ] Implement public `CaseTrace` semantic API methods.
- [ ] Implement redaction before summary/artifact writing.
- [ ] Verify unit tests pass.

### Task 2: Instrument LLM Context Snapshots

**Files:**

- Modify: `packages/opencode/src/session/llm.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: `CaseTrace.contextSnapshot`
- Produces: context snapshot with model messages, system prompts, tool schema and metadata.

- [ ] Add failing test that renders a context snapshot with artifact content.
- [ ] Record context snapshot before provider stream starts.
- [ ] Add semantic edge from context snapshot to LLM span.
- [ ] Verify tests pass.

### Task 3: Instrument Tools, Verification, and Changes

**Files:**

- Modify: `packages/opencode/src/tool/tool.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: `CaseTrace.decision`, `verification`, `change`, `edge`
- Produces: semantic tool decisions, verification records for bash-like commands, change records for edit-like outputs.

- [ ] Add failing tests for bash verification record and edit change record.
- [ ] Classify tool calls by id and arguments.
- [ ] Parse command outputs for simple expected/actual failures.
- [ ] Extract edit diffs and file metadata.
- [ ] Verify tests pass.

### Task 4: Instrument Processor and Final Response Evidence

**Files:**

- Modify: `packages/opencode/src/session/processor.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: `CaseTrace.decision`, `edge`, `finalEvidence`
- Produces: final response artifact and evidence links to recent tool/change/verification records.

- [ ] Add failing test for final response evidence in trace and HTML.
- [ ] Record tool call decision semantics.
- [ ] Record final text artifact at `text-end`.
- [ ] Link final answer to recent evidence records.
- [ ] Verify tests pass.

### Task 5: Instrument Compaction Semantics

**Files:**

- Modify: `packages/opencode/src/session/compaction.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: `CaseTrace.contextSnapshot`, `decision`, `edge`
- Produces: compaction context snapshot and selection metadata.

- [ ] Add failing test for compaction semantic events via direct `CaseTrace` API.
- [ ] Record compaction selection metadata and prompt artifacts.
- [ ] Record compaction output summary and edges.
- [ ] Verify tests pass.

### Task 6: Render Semantic HTML Views

**Files:**

- Modify: `packages/opencode/src/observability/case-trace-html.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: semantic arrays from `TraceSummary`
- Produces: static HTML sections `Evidence Chain`, `LLM Context`, `Changes & Verification`, `Constraints`.

- [ ] Add failing tests for all new HTML section labels and expandable artifacts.
- [ ] Implement semantic render helpers.
- [ ] Keep existing process and artifact views intact.
- [ ] Verify tests pass.

### Task 7: Documentation and Verification

**Files:**

- Modify: `docs/observable-benchmark-trace.md`

**Interfaces:**

- Produces: updated user-facing documentation for semantic trace.

- [ ] Document new trace fields and HTML views.
- [ ] Run `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`.
- [ ] Run `bun run --cwd packages/opencode typecheck`.
- [ ] Build release binary if needed and run a synthetic case.
- [ ] Commit and push changes.
