# Causal IR Subproject A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce Causal IR 1.0 as the canonical in-memory graph and append-only journal behind existing CaseTrace APIs while preserving the current provenance records, viewer, artifacts, lifecycle, and agent behavior.

**Architecture:** A focused `CausalIRStore` owns nodes, edges, artifacts, journal sequence, canonical snapshots, and compatibility projections. `CaseTrace` continues producing the same semantic data through its public APIs, but all graph mutations pass through the store; `trace.json` becomes a strict Causal IR superset, while `provenance-trace.json` remains the existing records/dataflow projection.

**Tech Stack:** TypeScript, Bun test, Node filesystem APIs already used by CaseTrace, existing static HTML renderer, existing trace attribution Python loader through compatibility records.

## Global Constraints

- Trace collection remains `passive_sidecar` with `behavior_impact: "none"`.
- No trace or attribution data may enter agent prompts, tool inputs, model requests, or task-loop decisions.
- `trace.json` uses `trace_version: "6.0"` and `causal_ir_version: "1.0"`.
- Existing formal record IDs, payloads, artifact hashes, edge endpoints, normalized relations, manifest semantics, and static HTML features must remain available.
- Original edge relations must be preserved; an unknown relation may project to `derived_from` only with an explicit diagnostic and must not be attribution-eligible.
- `records.jsonl` remains append-only and retains the legacy `record_type` field during migration.
- Large text remains in content-addressed artifacts.
- Tests run from `packages/opencode`, never the repository root.
- Follow red-green-refactor: no production implementation before the corresponding test fails for the expected reason.

---

## File Map

- Create `packages/opencode/src/observability/causal-ir.ts`: Causal IR types, canonical hashing, store, journal entries, snapshot/replay, and provenance projection.
- Create `packages/opencode/test/observability/causal-ir.test.ts`: focused store, replay, relation, and projection tests.
- Modify `packages/opencode/src/observability/trace-semantic-contract.ts`: Trace 6.0 version and lossless relation normalization details.
- Modify `packages/opencode/src/observability/case-trace.ts`: make `CausalIRStore` own graph/artifact state and emit canonical snapshots plus compatibility projections.
- Modify `packages/opencode/src/observability/causal-trace-viewer.ts`: accept the Causal IR superset through the unchanged compatibility view.
- Modify `packages/opencode/test/observability/case-trace.test.ts`: assert Causal IR output, compatibility projection, journal operations, lifecycle recovery, and viewer parity.
- Modify `tools/trace_attribution/tests/test_backward_taint.py`: add one compatibility assertion that Trace 6.0 records remain loadable without changing attribution semantics.

### Task 1: Causal IR Types, Store, And Replay

**Files:**
- Create: `packages/opencode/src/observability/causal-ir.ts`
- Create: `packages/opencode/test/observability/causal-ir.test.ts`

**Interfaces:**
- Produces: `CAUSAL_IR_VERSION`, `CausalIRNodeInput`, `CausalIREdgeInput`, `CausalIRJournalEntry`, `CausalIRStore`, `replayCausalIRJournal`.
- Consumes later: `CaseTrace` passes already-redacted node, edge, and artifact objects into the store.

- [ ] **Step 1: Write failing create/update/replay tests**

```ts
import { describe, expect, test } from "bun:test"
import { CausalIRStore, replayCausalIRJournal } from "@/observability/causal-ir"

describe("causal IR store", () => {
  test("replays node creation and update into the same snapshot", () => {
    const journal: unknown[] = []
    const store = new CausalIRStore({ runID: "run_1", caseID: "case_1", append: (entry) => journal.push(entry) })
    const node = store.createNode({
      node_id: "node_1",
      kind: "decision",
      component: "task",
      timestamp: "2026-07-14T00:00:00.000Z",
      time_ms: 1,
      data: { chosen_action: "read" },
    })
    node.data = { chosen_action: "edit" }
    store.updateNode(node)

    expect(replayCausalIRJournal(journal).nodes).toEqual(store.snapshot().nodes)
    expect(journal.map((entry: any) => entry.operation)).toEqual(["node.created", "node.updated"])
  })
})
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Expected: FAIL because `@/observability/causal-ir` does not exist.

- [ ] **Step 3: Implement the minimal store and canonical journal**

Implement these exported shapes in `causal-ir.ts`:

```ts
export const CAUSAL_IR_VERSION = "1.0" as const

export type CausalNodeLike = {
  node_id: string
  kind: string
  component?: string
  span_id?: string
  timestamp: string
  time_ms: number
  title?: string
  status?: string
  data?: Record<string, unknown>
  source_refs?: string[]
  source_locations?: Record<string, unknown>[]
  typed_resources?: Record<string, unknown>[]
  artifact_refs?: string[]
  metadata?: Record<string, unknown>
}

export type CausalEdgeLike = {
  edge_id: string
  from: { type: string; id: string; label?: string }
  to: { type: string; id: string; label?: string }
  relation: string
  label?: string
  metadata?: Record<string, unknown>
}

export type ArtifactLike = {
  artifact_id: string
  hash: string
  path: string
  [key: string]: unknown
}

export type CausalIRJournalEntry = {
  sequence: number
  time: string
  operation: "node.created" | "node.updated" | "edge.created" | "artifact.created" | "artifact.reused" | "diagnostic.created" | "case.checkpointed" | "case.finalized"
  record_type: string
  entity_id?: string
  previous_payload_hash?: string
  payload_hash?: string
  data: unknown
}

export class CausalIRStore {
  constructor(input: { runID: string; caseID: string; append?: (entry: CausalIRJournalEntry) => void })
  readonly nodes: CausalNodeLike[]
  readonly edges: CausalEdgeLike[]
  readonly artifacts: ArtifactLike[]
  createNode<T extends CausalNodeLike>(node: T): T
  updateNode<T extends CausalNodeLike>(node: T): T
  replaceNodes(nodes: CausalNodeLike[]): void
  createEdge<T extends CausalEdgeLike>(edge: T): T
  replaceEdges(edges: CausalEdgeLike[]): void
  createArtifact<T extends ArtifactLike>(artifact: T): T
  reuseArtifact<T extends ArtifactLike>(artifact: T): T
  checkpoint(data: unknown): void
  finalize(data: unknown): void
  snapshot(): CausalIRStoreSnapshot
}
```

Use canonical recursively sorted JSON for payload hashes. Preserve object references so existing CaseTrace enrichment can mutate a node and then call `updateNode()`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Expected: PASS.

- [ ] **Step 5: Commit the store foundation**

```bash
git add packages/opencode/src/observability/causal-ir.ts packages/opencode/test/observability/causal-ir.test.ts
git commit -m "feat: add canonical causal IR store"
```

### Task 2: Lossless Relations And Compatibility Projection

**Files:**
- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/src/observability/causal-ir.ts`
- Modify: `packages/opencode/test/observability/causal-ir.test.ts`

**Interfaces:**
- Produces: `normalizeRelationDetails(relation)` and `projectProvenanceTrace(snapshot, input)`.
- Consumes: formal type/relation allowlists from `trace-semantic-contract.ts`.

- [ ] **Step 1: Add failing relation and projection tests**

```ts
test("preserves an unknown original relation without making it attribution eligible", () => {
  const edge = store.createEdge({
    edge_id: "edge_1",
    from: { type: "node", id: "a" },
    to: { type: "node", id: "b" },
    relation: "custom_future_relation",
  })
  const ir = store.snapshot()
  expect(ir.edges[0].original_relation).toBe("custom_future_relation")
  expect(ir.edges[0].normalized_relation).toBe("derived_from")
  expect(ir.edges[0].eligible_for_attribution).toBe(false)
  expect(ir.diagnostics[0].kind).toBe("unknown_relation")
})

test("projects canonical nodes into the existing provenance record contract", () => {
  const projection = projectProvenanceTrace(store.snapshot(), {
    traceVersion: "6.0",
    manifest: { case_id: "case_1", run_id: "run_1" },
    metrics: { token_usage: {}, trace_health: { issues: [] } },
  })
  expect(projection.records[0].record_id).toBe("node_1")
  expect(projection.dataflow_edges[0].relation).toBe("derived_from")
})
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Expected: FAIL because relation details and projection do not exist.

- [ ] **Step 3: Implement normalization details and projection**

Add:

```ts
export function normalizeRelationDetails(relation: string) {
  const migrated = RELATION_MIGRATIONS[relation]
  if (migrated) return { original: relation, normalized: migrated, known: true }
  if (isFormalDataflowRelation(relation)) return { original: relation, normalized: relation, known: true }
  return { original: relation, normalized: "derived_from" as const, known: false }
}
```

`projectProvenanceTrace` must map canonical nodes back to existing
`ProvenanceRecord` fields and canonical edges to existing `DataflowEdge` fields.
Put `original_relation`, `evidence_tier`, `eligible_for_attribution`, and
`derivation_method` into compatibility edge metadata.

- [ ] **Step 4: Run focused tests and typecheck**

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Expected: PASS.

Run: `bun typecheck`

Expected: exit code 0.

- [ ] **Step 5: Commit lossless projection**

```bash
git add packages/opencode/src/observability/causal-ir.ts packages/opencode/src/observability/trace-semantic-contract.ts packages/opencode/test/observability/causal-ir.test.ts
git commit -m "feat: preserve causal relations in trace projections"
```

### Task 3: Route CaseTrace Graph And Artifacts Through The Store

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: `CausalIRStore` create/update/replace node, edge, and artifact APIs.
- Produces: unchanged public `CaseTrace` methods backed by one canonical store.

- [ ] **Step 1: Add a failing canonical-ownership test**

Extend the first bundle test to assert:

```ts
expect(trace.causal_ir_version).toBe("1.0")
expect(trace.nodes.map((node: any) => node.node_id)).toEqual(
  trace.records.map((record: any) => record.record_id),
)
expect(trace.artifacts).toEqual(provenance.artifacts)
expect(trace.compatibility.provenance_projection).toBe("provenance-trace.json")
```

- [ ] **Step 2: Run the bundle test and verify RED**

Run: `bun test test/observability/case-trace.test.ts -t "writes trace semantic contract" --timeout 30000`

Expected: FAIL because `trace.json` has no Causal IR fields.

- [ ] **Step 3: Replace mutable graph collections with store-backed access**

In `CaseTraceWriter`:

```ts
private readonly causalIR: CausalIRStore
private get causalNodes() {
  return this.causalIR.nodes as CausalNode[]
}
private get causalEdges() {
  return this.causalIR.edges as CausalEdge[]
}
private get artifacts() {
  return this.causalIR.artifacts as TraceArtifact[]
}
```

Initialize the store in the constructor before `open()`. Route `node()`,
`causalEdge()`, every existing `node.update`, stale design filtering, artifact
creation, and artifact reuse through store APIs. Keep the current artifact
de-duplication index as an index over store-owned artifacts.

- [ ] **Step 4: Make canonical journal entries retain legacy record type**

The store append callback writes entries with both:

```json
{
  "operation": "node.created",
  "record_type": "node",
  "entity_id": "node_...",
  "payload_hash": "...",
  "data": {}
}
```

Replace direct node/edge/artifact `writeRecord()` calls with store operations.
Keep non-graph diagnostics as explicit journal diagnostics.

- [ ] **Step 5: Run focused and full CaseTrace tests**

Run: `bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts --timeout 30000`

Expected: PASS.

- [ ] **Step 6: Commit store integration**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "refactor: make causal IR the trace graph owner"
```

### Task 4: Emit Trace 6.0 Causal IR Superset

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: canonical `trace.json`, compatibility `provenance-trace.json`, Causal IR `partial/latest.json`, and unchanged `trace.html` functionality.

- [ ] **Step 1: Add failing bundle separation assertions**

```ts
expect(trace.trace_version).toBe("6.0")
expect(trace.causal_ir_version).toBe("1.0")
expect(trace.nodes.length).toBe(trace.metrics.records)
expect(trace.edges.length).toBe(trace.metrics.dataflow_edges)
expect(provenance.trace_version).toBe("6.0")
expect(provenance.causal_ir_version).toBeUndefined()
expect(partial.causal_ir_version).toBe("1.0")
expect(traceHtml).toContain("Trace v6.0")
```

- [ ] **Step 2: Run the bundle test and verify RED**

Run: `bun test test/observability/case-trace.test.ts -t "writes trace semantic contract" --timeout 30000`

Expected: FAIL on version and canonical fields.

- [ ] **Step 3: Add Causal IR summary construction**

`causalIRSummary(status, caseStatus)` must return the canonical graph plus the
compatibility `records` and `dataflow_edges` fields. The compatibility file is
created by `projectProvenanceTrace` and does not include canonical `nodes` or
`edges`.

Write:

- canonical summary to `trace.json`;
- compatibility projection to `provenance-trace.json`;
- canonical summary to `partial/latest.json`;
- viewer HTML from the canonical summary's compatibility view.

- [ ] **Step 4: Update viewer typing without changing rendered features**

Accept a structural provenance view obtained from Causal IR. Keep all existing
section IDs and artifact links unchanged.

- [ ] **Step 5: Run CaseTrace and viewer tests**

Run: `bun test test/observability/case-trace.test.ts --timeout 30000`

Expected: PASS with all existing HTML assertions.

- [ ] **Step 6: Commit Trace 6.0 output**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/src/observability/causal-trace-viewer.ts packages/opencode/src/observability/trace-semantic-contract.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "feat: emit causal IR trace 6 bundle"
```

### Task 5: Journal Recovery And Passive-Behavior Gates

**Files:**
- Modify: `packages/opencode/test/observability/causal-ir.test.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**
- Consumes: Causal IR replay and CaseTrace signal finalization.
- Produces: replayable normal and interrupted journals.

- [ ] **Step 1: Add failing lifecycle tests**

Add tests that:

- replay `records.jsonl` and compare node IDs, edge IDs, and artifact hashes to
  finalized `trace.json`;
- verify `case.finalized` is the final journal operation on normal finish;
- verify SIGINT and SIGTERM produce canonical `partial/latest.json` and
  `trace.html`;
- inject a trace write failure and verify the fixture's agent-visible result
  hash remains unchanged.

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run: `bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts -t "journal|SIGINT|SIGTERM|passive" --timeout 30000`

Expected: FAIL until checkpoint/finalize journal operations and replay loading
are integrated.

- [ ] **Step 3: Implement checkpoint and finalization journal operations**

Call `causalIR.checkpoint()` after a forced partial snapshot and
`causalIR.finalize()` exactly once during normal or signal finalization. Keep
write failures inside the trace subsystem's existing failure isolation.

- [ ] **Step 4: Run lifecycle tests and verify GREEN**

Run: `bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts -t "journal|SIGINT|SIGTERM|passive" --timeout 30000`

Expected: PASS.

- [ ] **Step 5: Commit lifecycle recovery**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/test/observability/causal-ir.test.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "feat: replay causal IR journals after interruption"
```

### Task 6: Attribution Compatibility And Full Regression

**Files:**
- Modify: `tools/trace_attribution/tests/test_backward_taint.py`
- Create: `docs/superpowers/reports/2026-07-14-causal-ir-subproject-a-verification.md`

**Interfaces:**
- Consumes: Trace 6.0 Causal IR compatibility records.
- Produces: regression evidence and Subproject A acceptance report.

- [ ] **Step 1: Add a failing Python compatibility test**

Construct a minimal Trace 6.0 fixture containing both `nodes/edges` and
`records/dataflow_edges`, load it through `TraceGraph.from_trace`, and assert
that the existing node refs and upstream reachability are unchanged.

- [ ] **Step 2: Run the Python test and verify RED or existing compatibility**

Run from repository root:

`python3 -m unittest tools.trace_attribution.tests.test_backward_taint -v`

If it passes immediately, record that the existing loader already provides
compatibility and keep the test as a regression characterization. No Python
production change is needed in Subproject A.

- [ ] **Step 3: Run full TypeScript verification**

Run from `packages/opencode`:

```bash
bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000
bun typecheck
```

Expected: all tests pass and typecheck exits 0.

- [ ] **Step 4: Verify formatting and repository state**

Run from repository root:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; unrelated existing untracked reports remain
untouched.

- [ ] **Step 5: Write the acceptance report**

Record:

- test counts and commands;
- canonical/projection field comparison;
- journal replay equality;
- artifact hash equality;
- lifecycle signal results;
- passive-behavior result;
- remaining work assigned to Subprojects B-D.

- [ ] **Step 6: Commit Subproject A verification**

```bash
git add tools/trace_attribution/tests/test_backward_taint.py docs/superpowers/reports/2026-07-14-causal-ir-subproject-a-verification.md
git commit -m "test: verify causal IR trace compatibility"
```
