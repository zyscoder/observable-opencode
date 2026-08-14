# Bounded Segmented Trace Runtime Design

## 1. Background

Observable OpenCode currently appends semantic operations to `records.jsonl`, but the
runtime also retains the complete Causal IR graph and several compatibility projections
in JavaScript memory. Appending a durable copy does not evict those objects.

A controlled probe with 3,000 observations measured:

| Phase | RSS |
| --- | ---: |
| 500 observations | 259 MB |
| 3,000 observations | 421 MB |
| terminal `finish()` | 1,596 MB |

The terminal peak is caused by constructing synchronized Causal IR, provenance, and
legacy projections together. In a memory-constrained environment, the worker can be
terminated by the operating system before it writes `trace.json`.

The current stable case-directory behavior creates a second reliability failure. Opening
the same case or resumed session removes terminal outputs and truncates `records.jsonl`.
An interrupted session therefore cannot continue its trace history.

## 2. Goals

1. Bound observability-owned runtime memory independently of trace duration.
2. Preserve every successfully appended semantic fact across `SIGKILL` and OOM.
3. Never let trace collection or reconstruction change Agent input, decisions, tool
   execution, session state, or exit semantics.
4. Present one logical trace for a resumed session while storing each process lifetime as
   an immutable run segment.
5. Preserve the current Causal IR information content and attribution interface.
6. Produce compatibility artifacts such as `trace.json` outside the Agent worker's heap.

## 3. Non-goals

- Changing OpenCode context-window, compaction, model, tool, skill, or MCP behavior.
- Feeding trace-derived facts or diagnostics back into the Agent.
- Performing root-cause attribution in the trace runtime.
- Making `SIGKILL` catchable. Recovery starts from the last durable journal entry.
- Sharing trace materialization state with OpenCode's session database.

## 4. Selected Architecture

### 4.1 Durable facts are authoritative

`records.jsonl` is the source of truth. Runtime objects are caches and active-lifecycle
state only. A fact is considered captured after its complete JSONL line has been appended.

Each OpenCode process writes a new immutable segment:

```text
<trace-root>/<logical-case-id>/
├── session.json
├── segments/
│   ├── <run-id-1>/
│   │   ├── records.jsonl
│   │   ├── segment.json
│   │   ├── index.sqlite
│   │   └── artifacts/
│   └── <run-id-2>/
│       ├── records.jsonl
│       ├── segment.json
│       ├── index.sqlite
│       └── artifacts/
├── trace.json
├── manifest.json
└── partial/latest.json
```

`session.json` is updated atomically and records segment order, session identity, run
identity, continuation relationships, status, and file locations. A resumed session never
opens an existing segment for truncation.

### 4.2 Bounded runtime state

The worker retains only:

- active spans, turns, tool calls, agents, and lifecycle records;
- a bounded hot-node LRU;
- bounded recent-reference windows used by online instrumentation;
- counters, journal hash state, and append status;
- artifact metadata needed by active records.

Node, edge, artifact, diagnostic, alias, and reference indexes are materialized in the
segment-local SQLite database. SQLite is private observer state and must not be added to
OpenCode's session database.

Default limits:

| State | Default bound |
| --- | ---: |
| Hot Causal IR nodes | 512 |
| Hot edges | 1,024 |
| Recent refs per category | 256 |
| Completed active lifecycle records | immediately evicted |
| Inline semantic preview | 2,048 characters |

Bounds may be configurable for diagnostics, but increasing them must never be required for
semantic completeness. Cold lookup uses SQLite rather than silently dropping provenance.

### 4.3 Runtime recorder and offline materializer

The runtime recorder performs local sanitization, artifact externalization, append-only
journal writes, and incremental SQLite index updates. It does not construct complete
provenance or legacy projections during shutdown.

The standalone `observable-trace finalize <case-dir>` materializer:

1. validates every segment independently;
2. recovers valid JSONL lines up to the first torn or invalid tail line;
3. reconstructs current node and edge state in a temporary SQLite materialization store;
4. inserts explicit `run.continuation` nodes and edges between segments;
5. streams the unified Causal IR document to an atomic `trace.json` write;
6. writes `manifest.json` and `partial/latest.json` from the same committed snapshot;
7. reports incomplete segments without presenting them as successful runs.

The TUI may invoke the standalone materializer after worker shutdown. Materialization
failure cannot fail or modify the completed Agent session. The same command can be retried
manually or by the attribution CLI.

### 4.4 Unified user view

The root directory is the user-visible trace. Segment boundaries remain queryable through:

- `run_id` on every node and edge;
- segment entries in `session.json` and the unified manifest;
- `continuation_of` links;
- explicit interrupted, resumed, completed, and recovered statuses.

The attribution module receives the unified `trace.json` or the logical case directory.
When given the directory, it finalizes stale derived outputs before analysis.

### 4.5 Crash and resume behavior

On normal shutdown, the worker appends a compact `segment.finalized` lifecycle record and
returns. It prints the logical trace directory and whether unified materialization completed.

On `SIGINT`, `SIGTERM`, or `SIGHUP`, the worker appends a cancelled terminal record when the
signal handler runs. On `SIGKILL` or OOM, no terminal record is expected. The next process or
offline materializer marks the previous segment as `interrupted_unfinalized` based on its
valid journal prefix.

Resuming with `-s <session-id>` resolves the same logical case directory, creates a new run
segment, and links it to the previous segment. It never deletes prior JSONL, artifacts, or
derived files before the new segment is durable.

### 4.6 Passive-observer contract

All observer operations are one-way side effects. Trace-derived state is inaccessible to
prompt assembly, context management, Agent decisions, tool selection, skills, MCP, and
result processing.

If trace storage is unavailable:

- record a local warning when possible;
- disable or degrade only observability;
- do not retry Agent operations;
- do not alter the Agent response or process status.

## 5. Delivery Phases

### Phase 1: Memory-safe single-run capture

1. Add reproducible memory-growth and terminal-peak tests.
2. Introduce the segment journal and segment-local disk index.
3. Replace full-graph runtime ownership with bounded hot state plus disk lookup.
4. Move unified projection construction to `observable-trace finalize`.
5. Make TUI shutdown invoke materialization outside the worker heap.
6. Verify existing single-run Causal IR and attribution fixtures remain semantically
   equivalent.

### Phase 2: Session-level continuation

1. Add atomic `session.json` and immutable segment allocation.
2. Resolve resumed session IDs to existing logical trace roots.
3. Add continuation semantics and interrupted-segment recovery.
4. Materialize all segments into one user-visible trace.
5. Add migration/read compatibility for existing flat trace directories.

## 6. Verification

### 6.1 Memory acceptance

- A 10,000-record stress trace must complete without observability RSS growing linearly.
- After warm-up, observer-owned RSS growth must remain below 128 MB under the standard
  stress payload.
- Materializing a 1 GB journal must use no more than 256 MB RSS.
- Terminal worker shutdown must not construct the full unified trace in its own heap.

### 6.2 Durability acceptance

- Every fully appended line before `SIGKILL` remains replayable.
- A torn final line is ignored and reported; earlier lines remain usable.
- Restarting the same session does not modify prior segment bytes.
- Repeated finalization produces an equivalent unified trace.

### 6.3 Semantic acceptance

- Existing Causal IR contract tests pass.
- Existing stress and open-source benchmark traces retain node, edge, artifact, lifecycle,
  compaction, tool, skill, MCP, subagent, and response semantics.
- Manual and offline-attribution comparisons do not lose a previously available root-cause
  candidate because of hot-state eviction.

### 6.4 Behavior acceptance

- Running the same case with tracing enabled and disabled produces the same Agent-visible
  messages, tool calls, file changes, exit status, and session data, excluding timing and
  trace-only output.
- Disk/index/materializer failures do not enter model context or Agent control flow.

## 7. Migration and Compatibility

Existing flat directories remain readable. The materializer treats their root
`records.jsonl` as a legacy segment. New runs always use `segments/<run-id>`.

`trace.json` remains the canonical compatibility output for the attribution module, but it
is derived and replaceable. Segment journals and artifacts are the durable evidence.

The initial rollout keeps current environment variables. New limits and materialization
controls receive conservative defaults and are documented only when users need to override
them.

## 8. Risks and Controls

- **Online derivation depends on cold history:** use SQLite-backed reference lookup and add
  semantic-equivalence fixtures before eviction is enabled.
- **SQLite write amplification:** batch index writes in bounded transactions while JSONL
  remains the commit authority.
- **Materializer interruption:** write all derived outputs atomically and leave journals
  untouched.
- **Cross-run identity collision:** scope physical identities by `run_id` and expose stable
  logical aliases in the unified projection.
- **Legacy consumer assumptions:** retain flat `trace.json` and manifest outputs at the
  logical case root.
