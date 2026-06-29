# Trace Semantic Contract v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Trace Semantic Contract v4 so benchmark traces expose one formal semantic entrypoint, remove low-value nodes from formal records, and enrich records for offline attribution.

**Architecture:** Add a small semantic contract module that owns record types, relation enums, relation migration, and raw/formal event classification. Keep `CaseTrace` as the trace writer, but make it call the contract before writing provenance output. Merge the provenance viewer into the formal `trace.html` path and demote `viewer.html` to a compatibility alias.

**Tech Stack:** TypeScript, Bun test, existing `CaseTrace` observability code, static HTML renderer.

## Global Constraints

- Official structured data entrypoint is `provenance-trace.json`.
- Official visual entrypoint is `trace.html`.
- `viewer.html` must not be a separate formal viewer; it can only be an alias for transition.
- Formal `records[]` must not contain `runtime.event`, `tool-input-delta`, token/text delta, empty `tool_overrides`, or pure `part_count` nodes.
- Every formal record type and dataflow relation must be validated against a strict contract.
- Trace generation must not perform root-cause attribution or diagnosis.
- Large texts remain artifact-backed.
- TDD is required for behavior changes.

---

### Task 1: Contract Module And Failing Schema Tests

**Files:**
- Create: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `TRACE_VERSION`, `FORMAL_RECORD_TYPES`, `FORMAL_DATAFLOW_RELATIONS`, `normalizeRelation(relation: string): FormalDataflowRelation`, `isFormalRecordType(type: string): boolean`, `shouldPromoteRuntimeEvent(component: TraceComponent, eventType: string, data?: unknown): boolean`.
- Consumes: existing `TraceComponent` type from `case-trace.ts`.

- [ ] **Step 1: Write failing tests for v4 version, record whitelist, relation whitelist, and noise exclusion**

Add tests that create runtime/prompt events and old relation names, then assert:

```ts
expect(provenance.trace_version).toBe("4.0")
expect(provenance.records.map((record: any) => record.event_type)).not.toContain("runtime.event")
expect(provenance.records.map((record: any) => record.event_type)).not.toContain("tool-input-delta")
expect(provenance.dataflow_edges.every((edge: any) => allowedRelations.has(edge.relation))).toBe(true)
expect(provenance.dataflow_edges.map((edge: any) => edge.relation)).toContain("modified_by")
expect(provenance.dataflow_edges.map((edge: any) => edge.relation)).not.toContain("tool_to_change")
```

- [ ] **Step 2: Run the failing test**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: FAIL because trace version is still `3.0`, `runtime.event` is still emitted, and old relations are not normalized.

- [ ] **Step 3: Add the contract module**

Implement:

```ts
export const TRACE_VERSION = "4.0" as const
export const FORMAL_RECORD_TYPES = [
  "run.start",
  "task.loop",
  "context.pack",
  "context.compaction",
  "llm.call",
  "tool.call",
  "mcp.call",
  "skill.load",
  "subagent.call",
  "observation",
  "change",
  "verification",
  "response.output",
] as const

export const FORMAL_DATAFLOW_RELATIONS = [
  "selected_into_context",
  "prompted",
  "produced",
  "consumed",
  "compressed_from",
  "compressed_to",
  "spawned",
  "continued_from",
  "derived_from",
  "verified_by",
  "modified_by",
  "failed_before",
  "read_from",
  "returned_by",
] as const
```

Also implement old relation migration exactly as specified in `docs/superpowers/specs/2026-06-29-trace-semantic-contract-v4-design.md`.

- [ ] **Step 4: Run contract tests until they pass**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: the new tests pass after later tasks wire the contract.

---

### Task 2: Formal Record Filtering And Relation Normalization

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: `TRACE_VERSION`, `isFormalRecordType`, `normalizeRelation`, `shouldPromoteRuntimeEvent`.
- Produces: v4 `provenance-trace.json`, manifest, partial snapshot, and formal records with no low-value runtime nodes.

- [ ] **Step 1: Write failing tests for prompt/runtime aggregation**

Add a test that calls:

```ts
CaseTrace.event({ component: "prompt", event_type: "prompt.parts.resolved", data: { part_count: 1, part_types: ["text"], tool_overrides: {} } })
CaseTrace.event({ component: "processor", event_type: "tool-input-delta", data: { id: "call_1", delta: " owns" } })
```

Assert these do not become formal provenance records.

- [ ] **Step 2: Run the failing test**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: FAIL because `CaseTrace.event()` currently promotes runtime/prompt events to `runtime.event`.

- [ ] **Step 3: Wire contract into `CaseTrace`**

Update imports:

```ts
import { TRACE_VERSION, isFormalRecordType, normalizeRelation, shouldPromoteRuntimeEvent } from "./trace-semantic-contract"
```

Update `TraceManifest` and `ProvenanceTraceSummary` trace versions to use `typeof TRACE_VERSION`.

Change `event()` so only action-affecting runtime events can be promoted. Prompt part events and processor deltas stay in `events[]`/raw only.

Change `provenanceRecords()` to filter non-formal record types.

Change `provenanceDataflowEdges()` to call `normalizeRelation()` and emit only formal relations.

- [ ] **Step 4: Run tests**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: PASS for v4 filtering and relation normalization.

---

### Task 3: Semantic Enrichment Fields

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/tool/tool.ts`
- Modify: `packages/opencode/src/tool/skill.ts`
- Modify: `packages/opencode/src/tool/task.ts`
- Modify: `packages/opencode/src/mcp/index.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `source_locations`, `context_ledger`, `response_segments.visibility`, `response_segments.turn_index`, `response_segments.is_final_for_case`, and `subagent.trace_ref`.

- [ ] **Step 1: Write failing tests for enriched records**

Add tests that assert:

```ts
expect(response.data.metadata.visibility).toBe("user_visible")
expect(response.data.metadata.is_final_for_case).toBe(true)
expect(compaction.data.context_ledger.algorithm).toBeDefined()
expect(subagent.data.trace_ref.child_session_id).toBeDefined()
expect(observation.data.source_locations?.[0]?.path).toBe("src/pricing.mjs")
```

- [ ] **Step 2: Run failing tests**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: FAIL because these fields are absent or only implicit.

- [ ] **Step 3: Extend trace types and record builders**

Add lightweight types inside `case-trace.ts`:

```ts
export type TraceSourceLocation = {
  uri?: string
  path?: string
  line_start?: number
  line_end?: number
  content_hash?: string
  snippet_preview?: string
}

export type TraceContextLedger = {
  token_estimate_before?: number
  token_estimate_after?: number
  retained_message_ids?: string[]
  dropped_message_ids?: string[]
  retained_fact_refs?: string[]
  dropped_fact_refs?: string[]
  summary_artifact_id?: string
  algorithm?: string
}
```

Thread optional `source_locations` through observations, tool records, MCP records, and response source metadata where available.

- [ ] **Step 4: Add best-effort semantic extraction**

For this implementation round, do deterministic best-effort extraction:

- file paths from tool args such as `filePath`, `path`, `file`, and result metadata.
- line spans from explicit `start`/`end` fields or line-like metadata.
- compaction ledger from existing selected head/tail counts and summary artifact IDs.
- response visibility defaults to `user_visible`; compaction/auto-continue outputs can be marked `compaction_followup` when metadata indicates compaction.
- subagent trace ref uses existing subagent task/session identifiers.

- [ ] **Step 5: Run tests**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: PASS for semantic enrichment fields.

---

### Task 4: Merge HTML Entry Points

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`
- Modify: `packages/opencode/src/observability/case-trace-html.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: formal `trace.html` from `provenance-trace.json`; optional `viewer.html` alias only.

- [ ] **Step 1: Write failing test for single formal HTML entrypoint**

Assert:

```ts
expect(await exists(path.join(caseDir, "trace.html"))).toBe(true)
expect(traceHtml).toContain("Trace Provenance")
expect(traceHtml).toContain("Component Dataflow")
expect(traceHtml).toContain("Context Ledger")
expect(traceHtml).not.toContain("Evidence Inspector")
```

If `viewer.html` exists, assert it contains an alias message pointing to `trace.html`.

- [ ] **Step 2: Run failing test**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: FAIL because current formal v3 viewer is written to `viewer.html`.

- [ ] **Step 3: Write provenance HTML to `trace.html`**

Change finalization so:

```ts
const provenance = this.provenanceSummary(status)
fs.writeFileSync(this.provenanceTraceFile, json(provenance))
fs.writeFileSync(this.htmlFile, renderProvenanceTraceHtml(provenance))
fs.writeFileSync(this.viewerFile, renderTraceAliasHtml("trace.html"))
```

Keep old debug HTML only behind the full/debug profile if implemented in this round.

- [ ] **Step 4: Run tests**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: PASS for formal `trace.html`.

---

### Task 5: Docs, Full Verification, And Commit

**Files:**
- Modify: `docs/observable-benchmark-trace.md`
- Modify: `docs/superpowers/specs/2026-06-29-trace-semantic-contract-v4-design.md` if implementation decisions require clarification.
- Modify: `docs/superpowers/plans/2026-06-29-trace-semantic-contract-v4.md` checkbox statuses as tasks complete.

**Interfaces:**
- Consumes: all code changes from Tasks 1-4.
- Produces: verified implementation commit.

- [ ] **Step 1: Update docs**

Replace v3 formal guidance with v4:

```md
- `trace.html`: formal semantic viewer.
- `provenance-trace.json`: Trace Semantic Contract v4 formal data.
- `viewer.html`: compatibility alias only when present.
```

- [ ] **Step 2: Run focused tests**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`

Expected: all tests pass.

- [ ] **Step 3: Run typecheck**

Run: `./node_modules/.bin/tsgo --noEmit -p packages/opencode/tsconfig.json`

Expected: exit 0.

- [ ] **Step 4: Run whitespace check**

Run: `git diff --check`

Expected: exit 0.

- [ ] **Step 5: Commit**

Run:

```bash
git add packages/opencode/src/observability packages/opencode/src/tool packages/opencode/src/mcp docs/observable-benchmark-trace.md docs/superpowers/specs/2026-06-29-trace-semantic-contract-v4-design.md docs/superpowers/plans/2026-06-29-trace-semantic-contract-v4.md
git commit -m "feat: implement trace semantic contract v4"
```
