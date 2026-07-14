# Causal IR Unified Kernel Migration Design

## Background

The current trace already contains the main ingredients of a causal
intermediate representation:

- internal `CausalNode` and `CausalEdge` collections;
- formal `ProvenanceRecord` and `DataflowEdge` projections;
- an append-only `records.jsonl` stream;
- raw runtime events and content-addressed artifacts;
- offline message-lineage reconstruction and backward attribution.

The same semantic fact is nevertheless represented through several parallel
collections, including causal nodes, semantic decisions, context snapshots,
verification records, change records, response segments, and design records.
The finalized formal trace is projected from causal nodes, while the offline
analyzer reconstructs another graph from records, source refs, dataflow edges,
and artifacts. This creates multiple sources of truth, lossy relation
normalization, duplicated identity logic, and avoidable attribution gaps.

This design makes a versioned Causal IR the single semantic kernel. Existing
formal trace behavior remains available through compatibility projections until
all consumers have migrated and equivalence gates pass.

## Goals

1. Replace parallel semantic stores with one canonical Causal IR graph and
   append-only lifecycle journal.
2. Preserve all information currently available in formal records, edges,
   artifacts, partial snapshots, and trace lifecycle output.
3. Preserve original relation semantics while also exposing normalized
   relations for stable consumers.
4. Distinguish observed facts, deterministic derivations, advisory inferences,
   and model-generated analysis.
5. Make `trace.html` and offline attribution consume the same canonical graph.
6. Keep existing trace readers working through explicit compatibility
   projections during migration.
7. Preserve signal handling, interrupted-trace recovery, redaction, artifact
   de-duplication, and static HTML viewing.
8. Keep collection passive and prevent trace or attribution data from changing
   the observed agent's behavior.

## Non-Goals

- No runtime root-cause diagnosis.
- No attribution result is written back into the fact graph or agent session.
- No raw token, text, or tool-input delta becomes a semantic node.
- No immediate removal of compatibility outputs.
- No rewrite of model, tool, MCP, skill, subagent, or task-loop behavior.
- No causal edge is made attribution-eligible from temporal proximity alone.

## Compatibility Definition

"Functionally lossless" means that, for every trace produced before migration,
the Causal IR can project an equivalent formal trace with:

- the same case lifecycle and manifest meaning;
- the same formal record identities and semantic payloads;
- the same artifact hashes and readable payloads;
- the same dataflow endpoints and normalized relations;
- no reduction in graph reachability used by the viewer or attribution module;
- the same partial-finalization behavior after supported process signals;
- the same redaction guarantees;
- the same or better attribution input, without changing the agent run.

The Causal IR is not required to reconstruct raw information that the current
trace never records. It must preserve every recorded raw event and artifact by
reference, but semantic aggregation remains intentionally non-reversible to
unobserved provider state.

## Canonical Bundle

The case directory becomes:

```text
<trace_root>/<case_id>/
  manifest.json
  trace.json
  records.jsonl
  trace.html
  provenance-trace.json
  legacy-trace.json
  raw-events.jsonl
  partial/latest.json
  artifacts/sha256/<hash>.json|txt
  analysis/message-lineage.json
  analysis/attribution.json
  analysis/attribution-checkpoints.jsonl
```

The files have these authorities:

- `trace.json`: canonical finalized Causal IR.
- `records.jsonl`: canonical append-only Causal IR lifecycle journal.
- `partial/latest.json`: latest recoverable Causal IR snapshot.
- `trace.html`: static projection of `trace.json` and artifacts.
- `provenance-trace.json`: migration-period projection using the existing
  `records[]` and `dataflow_edges[]` contract.
- `legacy-trace.json`: existing span/event compatibility output.
- `raw-events.jsonl`: low-level observed event stream for trace debugging.
- `analysis/*`: optional derived overlays that reference Causal IR IDs but never
  mutate the fact graph.

`trace.json`, `records.jsonl`, and artifacts are the only semantic source of
truth. Compatibility and analysis files are reproducible projections.

## Causal IR Schema

### Top-Level Document

```ts
type CausalIR = {
  trace_version: "6.0"
  causal_ir_version: "1.0"
  manifest: TraceManifest
  nodes: CausalIRNode[]
  edges: CausalIREdge[]
  artifacts: TraceArtifact[]
  journal: CausalIRJournalSummary
  metrics: TraceMetrics
  diagnostics: TraceDiagnostic[]
  compatibility: CompatibilitySummary
}
```

`trace_version` describes the complete trace bundle contract.
`causal_ir_version` versions the graph and journal schema independently.

### Node Envelope

```ts
type CausalIRNode = {
  node_id: string
  kind: FormalRecordType
  schema_version: string
  origin: "observed" | "deterministic_derived" | "offline_derived"
  component: TraceComponent
  status?: string
  title?: string
  order: {
    sequence: number
    timestamp: string
    time_ms: number
  }
  scope: {
    run_id: string
    case_id: string
    session_id?: string
    message_id?: string
    part_id?: string
    call_id?: string
    span_id?: string
    parent_span_id?: string
    agent_id?: string
    parent_agent_id?: string
  }
  payload: Record<string, unknown>
  input_refs: CausalIRRef[]
  output_refs: CausalIRRef[]
  source_refs: CausalIRRef[]
  source_locations: TraceSourceLocation[]
  artifact_refs: string[]
  aliases: string[]
  derivation?: DerivationProvenance
  integrity: {
    payload_hash: string
    source_hash?: string
  }
  metadata?: Record<string, unknown>
}
```

The envelope contains stable identity, ordering, execution scope, typed
references, payload, source location, artifact links, and integrity hashes.
Event-specific payloads remain extensible and are validated by `kind` and
`schema_version`.

### Edge Envelope

```ts
type CausalIREdge = {
  edge_id: string
  from: CausalIRRef
  to: CausalIRRef
  original_relation: string
  normalized_relation: FormalDataflowRelation
  evidence_tier: "confirmed" | "content_matched" | "temporal_advisory"
  eligible_for_attribution: boolean
  derivation_method: string
  evidence_refs: CausalIRRef[]
  confidence?: number
  label?: string
  metadata?: Record<string, unknown>
}
```

Unknown relations are never silently rewritten as `derived_from`. The original
relation is retained, and normalization either succeeds explicitly or marks a
compatibility diagnostic. `temporal_advisory` edges are always ineligible for
automatic backward attribution.

### Reference Envelope

```ts
type CausalIRRef = {
  ref_type: "node" | "artifact" | "raw_event" | "external"
  ref_id: string
  legacy_ref?: string
  label?: string
}
```

Legacy aliases remain resolvable during migration. New producers write stable
node IDs and typed references directly.

### Derivation Provenance

Deterministic and offline-derived facts declare:

```ts
type DerivationProvenance = {
  algorithm: string
  algorithm_version: string
  derived_at: string
  input_refs: CausalIRRef[]
  rule_id?: string
  reproducible: boolean
}
```

An observed node cannot claim a derivation algorithm. A derived node or edge
without its input refs is invalid.

## Fact And Analysis Separation

The canonical graph contains three factual origins:

- `observed`: directly captured from agent/runtime execution;
- `deterministic_derived`: reproducible parsing, aggregation, or identity
  reconstruction from observed data;
- `offline_derived`: a separately generated semantic relation whose method,
  inputs, and confidence are recorded.

Model judgments such as defect presence, causal role, root-cause status, and
optimization advice are not Causal IR facts. They live in an analysis overlay:

```ts
type AnalysisOverlay = {
  overlay_version: string
  source_trace_hash: string
  analyzer: Record<string, unknown>
  annotations: AnalysisAnnotation[]
  roots: RootCauseCandidate[]
  checkpoints?: string
}
```

Every annotation references immutable Causal IR node/edge IDs. The overlay is
replaceable and cannot be consumed by the observed agent during the recorded
run.

## Lifecycle Journal

`records.jsonl` becomes the append-only source journal. Entries are strictly
ordered and use these operations:

- `node.created`;
- `node.updated`;
- `edge.created`;
- `artifact.created`;
- `artifact.reused`;
- `diagnostic.created`;
- `case.checkpointed`;
- `case.finalized`.

Each entry contains sequence, timestamp, entity ID, previous payload hash when
updated, new payload hash, and redacted payload or artifact reference. Replaying
the journal must reproduce `trace.json` byte-equivalent semantic content after
canonical JSON ordering.

Signal handling flushes pending entries, writes `partial/latest.json`, projects
`trace.html`, and records finalization disposition. SIGKILL remains
unhandleable, but the latest flushed journal and partial snapshot remain usable.

## Unified Store

A `CausalIRStore` owns nodes, edges, artifacts, diagnostics, indexes, journal
sequence, and snapshot generation. It provides:

- create/update/query node operations;
- typed edge creation with relation validation;
- alias and identity indexes;
- artifact write and de-duplication;
- append-only journal persistence;
- deterministic finalized snapshot generation;
- compatibility projection hooks.

Existing trace APIs such as decision, context, verification, change, response,
MCP, skill, and subagent recording become typed producers over this store.
During migration, compatibility views may expose legacy collections, but those
views are derived from the store and are never independently mutated.

## Compatibility Projections

### Formal Provenance Projection

`projectProvenanceTrace(ir)` produces the existing:

- `manifest`;
- `records[]`;
- `dataflow_edges[]`;
- `artifacts[]`;
- `metrics`.

Record IDs, event types, payload fields, source refs, artifact refs, timestamps,
and status values remain stable. Edge projection uses `normalized_relation` and
retains `original_relation`, evidence tier, and derivation fields in metadata.

### Legacy Projection

Existing span/event output remains available from observed scope and raw event
references. Data not represented in a semantic node remains in
`raw-events.jsonl` and is not fabricated.

### Viewer Projection

`trace.html` reads the Causal IR model directly. Existing overview, timeline,
component flow, input/output inspector, artifact viewer, context lineage,
subagent hierarchy, and claim evidence views continue to work. Analysis overlay
panels are optional and visually separated from recorded facts.

### Attribution Adapter

The Python graph loader accepts both schemas:

1. Causal IR `nodes[]` and `edges[]` are preferred;
2. legacy `records[]` and `dataflow_edges[]` are adapted;
3. message-lineage reconstruction emits an overlay against canonical IDs;
4. no second alias or edge graph is maintained after loading.

## Migration Phases

### Phase 1: Schema And Store

- Add Causal IR schema, validation, canonical hashing, and journal replay.
- Wrap current `causalNodes`, `causalEdges`, and artifacts in `CausalIRStore`.
- Preserve current output through compatibility projections.

### Phase 2: Typed Producer Migration

- Migrate lifecycle, prompt/context, LLM, decision, tool, MCP, skill, subagent,
  verification, change, evidence, response, and claim producers.
- Replace independently mutated semantic collections with store queries and
  typed projections.
- Keep raw event collection unchanged.

### Phase 3: Consumer Migration

- Render `trace.html` from Causal IR.
- Update the attribution loader and message-lineage reconstruction.
- Add explicit analysis overlay outputs and source trace hashes.

### Phase 4: Equivalence Gate

- Rebuild prior synthetic, Sphinx, Pydantic, and Seaborn traces.
- Compare record identity, payload, artifacts, edge reachability, viewer
  sections, interruption behavior, and attribution baselines.
- Keep compatibility projection enabled until every gate passes.

### Phase 5: Single-Kernel Cleanup

- Remove parallel mutable semantic arrays.
- Keep compatibility projections for at least one release line.
- Mark `provenance-trace.json` deprecated only after downstream consumers use
  `trace.json` Causal IR.

## Error Handling

- Invalid node payloads are rejected before journal append and create a trace
  diagnostic without affecting agent execution.
- An unknown relation preserves its original value, is ineligible for
  attribution, and creates a normalization diagnostic.
- An unresolved ref is retained as an external ref and reported; it is never
  redirected by timing alone.
- Journal append failure disables further trace writes but cannot fail the
  observed agent task.
- Snapshot or HTML projection failure leaves the journal intact for later
  recovery.
- Missing artifacts remain explicit gaps with expected hash and path.
- Overlay source hash mismatch prevents attribution results from being shown as
  belonging to the current trace.
- Interrupted replay stops at the last valid complete JSONL entry.

## Passive-Behavior Invariants

1. Trace producers receive copies or summaries of already available data.
2. No trace function changes prompt content, context selection, tool arguments,
   tool results, model settings, permission behavior, or loop decisions.
3. Trace failures are isolated from agent execution.
4. Derived facts are generated after the underlying event and cannot be read by
   the running agent.
5. Analysis overlays are generated offline and are not placed in session
   context or tool-visible paths.
6. Existing environment variables and trace directory selection remain
   compatible.

## Test Strategy

Implementation follows red-green-refactor.

### Schema And Store Tests

1. Create, update, edge, artifact, checkpoint, and finalize entries replay to an
   identical Causal IR snapshot.
2. Original and normalized relations round-trip without information loss.
3. Derived nodes require algorithm, version, and input refs.
4. Temporal advisory edges are never attribution-eligible.
5. Alias resolution is deterministic and reports collisions.
6. Artifact hashes and de-duplication remain stable.

### Projection Tests

1. Current formal records project with identical IDs and semantic payloads.
2. Current dataflow edges project with identical endpoints and normalized
   relations.
3. Existing manifest fields and file paths remain available.
4. Viewer sections contain the same semantic records and artifact access.
5. Legacy loader and Causal IR loader produce equivalent attribution graphs.

### Lifecycle Tests

1. Normal completion finalizes journal, snapshot, manifest, and HTML.
2. SIGINT, SIGTERM, and SIGHUP preserve a usable journal and HTML.
3. An interrupted final snapshot can be rebuilt from the journal.
4. Projection failure does not corrupt the journal or alter agent status.

### Passive-Behavior Tests

1. Trace disabled, current trace, and Causal IR modes produce identical agent
   prompts, model requests, tool calls, tool outputs, repository changes, and
   final response hashes for deterministic fixtures.
2. Trace-derived and attribution-derived data never appears in model context.
3. Trace write failures do not alter task completion status.

## Acceptance Gates

Migration is complete only when:

1. `trace.json` validates as Causal IR 1.0 and Trace 6.0;
2. every formal semantic event is represented by exactly one canonical node;
3. all compatibility records are generated from Causal IR, not parallel arrays;
4. 100% of prior formal record IDs and artifact hashes round-trip;
5. all prior edge endpoints round-trip and original relation text is preserved;
6. attribution graph reachability is equal or greater for every prior start ref;
7. the Seaborn manual roots remain reachable after schema migration;
8. trace and attribution regression suites pass;
9. HTTP session benchmark execution and interactive serve behavior are
   unchanged;
10. SIGINT, SIGTERM, and SIGHUP produce viewable trace output;
11. static `trace.html` exposes all existing semantic content and artifacts;
12. no trace or overlay data is fed back into the agent;
13. provenance compatibility output is reproducible from `trace.json` alone;
14. journal replay reproduces the finalized semantic graph.

## Implementation Decomposition

The migration is delivered as four independently reviewable subprojects. Each
subproject has its own implementation plan, tests, and commit boundary.

### Subproject A: Canonical Store And Lossless Projection

Introduce the schema, `CausalIRStore`, lifecycle journal, replay, and
compatibility projection behind existing trace APIs. Existing semantic
collections may temporarily remain as read-only comparison inputs. This
subproject is accepted when current fixtures produce equivalent provenance
records, edges, artifacts, lifecycle state, and HTML from the new store.

### Subproject B: Typed Producer Migration

Move producer families into the canonical store in bounded batches:

1. lifecycle, prompt, context, and LLM;
2. decision, tool, verification, change, and evidence;
3. MCP, skill, subagent, response, claim, and design records.

Each batch removes the corresponding mutable parallel store only after its
projection-equivalence tests pass.

### Subproject C: Consumer And Analysis Migration

Move `trace.html`, Python graph loading, message-lineage reconstruction, and
attribution overlays to canonical IDs and Causal IR edges. Compatibility schema
loading remains covered by regression tests.

### Subproject D: Cleanup, Replay, And Release

Remove remaining duplicate semantic state, rebuild historical traces, rerun
offline attribution, execute HTTP stress cases, verify signal handling and
passive behavior, then publish the release.

Subproject A is implemented first. No later subproject starts until the current
subproject's acceptance gate passes.

## Delivery Order

1. Add schema, store, replay, and projection tests.
2. Implement Causal IR types and `CausalIRStore` behind existing trace APIs.
3. Generate canonical `trace.json` and compatibility provenance output.
4. Migrate typed producers one component family at a time.
5. Migrate the viewer and attribution loader.
6. Remove parallel mutable semantic stores.
7. Run unit, type, lifecycle, equivalence, and passive-behavior tests.
8. Rebuild and reanalyze existing traces before running a new stress case.
9. Push and release only after all acceptance gates pass.
