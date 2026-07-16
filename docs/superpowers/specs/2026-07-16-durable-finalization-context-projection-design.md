# Durable Finalization and Context Projection Design

## Background

Long-running benchmark cases can produce thousands of Causal IR nodes, tens of
thousands of edges, and large artifacts. The current finalization path writes a
full partial snapshot, appends another full snapshot and trace document to the
journal, builds compatibility projections, and only then publishes the
canonical `trace.json`. A supported process signal can therefore interrupt the
expensive projection work after useful data has been checkpointed but before a
formal terminal trace is available.

The same long traces also repeat historical context membership across message
transformations and materialize the canonical graph into several complete
files. That increases storage and traversal cost and makes "available in
context" look too similar to "used by this decision" during attribution.

## Goals

1. Make `SIGINT`, `SIGTERM`, and `SIGHUP` publish a terminal canonical trace
   and manifest before optional projections.
2. Keep final journal lifecycle entries compact while preserving replay of the
   canonical graph from incremental entries and checkpoints.
3. Make benchmark runners forward supported signals and wait for the child
   server's flush contract before exiting.
4. Preserve passive observation: tracing must not change model messages,
   context selection, tool execution, agent decisions, or agent-visible output.
5. After finalization correctness is stable, normalize context membership and
   make Causal IR the only eagerly persisted semantic source of truth.

## Non-Goals

- `SIGKILL` cannot execute a handler. It is covered by periodic checkpoints and
  offline recovery, not synchronous finalization.
- No online attribution, diagnosis, or feedback into the agent.
- No Context Edge representation change in phase one.
- No removal of compatibility outputs before phase-two equivalence gates pass.

## Phase One: Durable Finalization

### Terminal Core

`CaseTrace.finish()` first closes semantic records and constructs one terminal
Causal IR summary. It then publishes, in this order:

1. compact `case.finalized` journal entry;
2. atomic canonical `trace.json`;
3. atomic terminal `manifest.json`;
4. terminal `partial/latest.json` as the recovery mirror;
5. compatibility JSON and `trace.html` projections.

The canonical trace records whether the compact final journal append succeeded.
An append failure marks journal health as poisoned but does not prevent the
canonical trace from being published.

### Compact Lifecycle Journal

Incremental node, edge, artifact, and diagnostic entries remain authoritative.
Periodic checkpoints may contain a full store snapshot because they provide
bounded recovery after `SIGKILL`. The terminal lifecycle entry contains only:

- terminal manifest data;
- graph counts and integrity hashes;
- the latest durable checkpoint sequence/hash;
- canonical trace path and terminal status.

It never embeds another complete store snapshot or complete trace document.
Replay applies all incremental entries after the latest checkpoint, so final
graph recovery remains lossless without terminal duplication.

### Signal Ownership

The opencode process handles `SIGINT`, `SIGTERM`, and `SIGHUP` synchronously,
finishes the terminal core, and exits with the conventional signal code. The
FeatureBench runner installs temporary process-level handlers while a child is
active. A handler forwards the original signal exactly once, waits for the
child exit and canonical trace publication up to a bounded grace period, closes
logs, then restores normal signal exit semantics.

### Phase-One Acceptance

- Supported signals always produce terminal `manifest.json`, `trace.json`,
  `partial/latest.json`, and `trace.html` in the controlled large-trace test.
- Manifest and lifecycle records agree on terminal signal and cancellation.
- Final journal entry size is bounded by metadata and does not scale with node
  or edge count.
- Journal replay reconstructs the same nodes, edges, artifacts, and diagnostics
  as `trace.json`.
- Runner tests prove signal forwarding and waiting without invoking a model.
- Existing passive-behavior and compatibility tests remain green.

## Phase Two: Context and Projection Convergence

Phase two starts only after phase-one signal tests and at least two benchmark
regression loops are stable.

### Context Representation

- Persist each message, tool result, MCP result, skill result, and large payload
  once through a stable node or content-addressed artifact.
- Represent a context snapshot as an ordered manifest of references plus
  transformation provenance, rather than repeating full historical payloads.
- Represent broad membership as a context-set relation that can be lazily
  expanded for a specific query.
- Keep attribution-bearing relations such as `consumed_by_decision` separate
  from advisory `available_in_context` relations.

### Projection Representation

- `trace.json`, `records.jsonl`, checkpoints, and artifacts remain canonical.
- Provenance, legacy, and HTML outputs become deterministic read-time or final
  projections from canonical Causal IR.
- Large artifact text is loaded lazily by the viewer instead of embedded into
  every projection.
- Compatibility files remain available until projection-equivalence tests show
  no loss for existing consumers.

### Phase-Two Acceptance

- Existing attribution paths are preserved or become more precise.
- Direct consumption and context availability are distinguishable by schema.
- Context membership storage approaches growth by unique semantic units and
  snapshots, rather than snapshots multiplied by accumulated history.
- Canonical and compatibility projections pass identity, reachability,
  redaction, lifecycle, and artifact-equivalence tests.
- Benchmark comparison reports graph size, bytes, finalization time, viewer
  load time, attribution candidate count, and root-boundary agreement.

## Rollout and Rollback

Phase one changes file publication order and terminal journal payload only. It
does not change agent execution. Phase two is guarded by equivalence tests and
can retain the previous projector behind an internal compatibility path until
the benchmark gates pass. Any regression in canonical replay, signal output,
or attribution reachability blocks the next phase.
