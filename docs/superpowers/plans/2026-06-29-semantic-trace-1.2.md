# Semantic Trace 1.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve semantic trace precision so traces are useful as evidence for human root-cause attribution.

**Architecture:** Extend `CaseTrace` with precision redaction, verification parsing, finish-time constraint evaluation, concrete evidence references, and design records. Render design records in static HTML and let the processor emit them from final design-like responses.

**Tech Stack:** TypeScript, Bun test, existing opencode `CaseTrace`, static HTML rendering.

## Global Constraints

- Keep existing `events.jsonl`, `trace.json`, `trace.html` outputs.
- Keep artifact-backed storage for large text.
- Do not store secrets in trace or artifacts.
- Do not implement automatic root-cause diagnosis.
- Preserve compatibility with existing 1.1 semantic arrays.

---

### Task 1: Regression Tests

**Files:**

- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: existing `CaseTrace` public API.
- Produces: failing tests for the five semantic precision gaps.

- [ ] Add a test that verifies token metric keys are retained while auth-like fields and secret-looking string values are redacted.
- [ ] Add a test that verifies parser skips source template lines and extracts `Error: expected 170, got 30`.
- [ ] Add a test that verifies read-only constraints are evaluated at finish.
- [ ] Add a test that verifies final evidence references concrete context, verification and change IDs.
- [ ] Add a test that verifies design records render in HTML.

### Task 2: Core Trace Semantics

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**

- Produces: `TraceDesignRecord`, `CaseTrace.designRecord`.
- Produces: exact redaction and finish-time constraint evaluation.

- [ ] Implement precise `isSensitiveKey`.
- [ ] Fix `parseVerificationFailures` candidate selection.
- [ ] Track recent evidence refs.
- [ ] Replace generic final evidence refs with concrete refs.
- [ ] Evaluate unknown constraints before summary write.
- [ ] Add `design_records` storage and API.

### Task 3: Processor and HTML

**Files:**

- Modify: `packages/opencode/src/session/processor.ts`
- Modify: `packages/opencode/src/observability/case-trace-html.ts`

**Interfaces:**

- Consumes: `CaseTrace.finalEvidence`, `CaseTrace.designRecord`.
- Produces: design-like final responses recorded and rendered.

- [ ] Stop passing `recent_*` placeholders from `processor.ts`.
- [ ] Emit design records for architecture/design responses using final text and concrete evidence refs.
- [ ] Render `Design Records` in `trace.html`.

### Task 4: Documentation and Verification

**Files:**

- Modify: `docs/observable-benchmark-trace.md`

**Interfaces:**

- Produces: user-facing description of 1.2 semantic precision fields.

- [ ] Document 1.2 additions.
- [ ] Run targeted observability tests if Bun is available.
- [ ] Run `./node_modules/.bin/tsgo --noEmit --project packages/opencode/tsconfig.json`.
- [ ] Commit, push, tag the next observable release, and confirm workflow result.
