# Bounded Segmented Trace Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent long observable-opencode sessions from being terminated by trace-owned memory growth while preserving one recoverable logical Trace across resumed process runs.

**Architecture:** The Agent worker writes immutable Causal IR journal segments and keeps only bounded hot state. Complete `trace.json` construction runs in an isolated materializer process backed by segment-local SQLite, and a root `session.json` links all process segments into one logical trace.

**Tech Stack:** TypeScript, Bun, `bun:sqlite`, append-only JSONL, existing Causal IR schemas, Bun test.

## Global Constraints

- Trace collection must not change Agent messages, context, decisions, tools, skills, MCP calls, file changes, session data, or exit semantics.
- `records.jsonl` and artifacts are durable evidence; `trace.json`, manifest, partial, provenance, legacy, and HTML files are replaceable derived outputs.
- Never truncate or delete a prior segment when opening or resuming a session.
- Every process lifetime has a unique `run_id`; users and attribution consume one logical case trace with explicit continuation edges.
- Runtime hot nodes default to 512, hot edges to 1,024, and recent refs per category to 256.
- Observer-owned RSS growth after warm-up must remain below 128 MB for the standard 10,000-record stress payload.
- A 1 GB journal materialization must remain at or below 256 MB RSS.
- Storage and materialization failures may degrade observability only and must never feed information back to the Agent.

---

### Task 1: Lightweight Runtime Terminal Record

**Files:**
- Modify: `packages/opencode/src/observability/causal-ir.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/cli/cmd/tui/worker-trace.ts`
- Test: `packages/opencode/test/observability/causal-ir.test.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`
- Test: `packages/opencode/test/cli/tui/worker-trace.test.ts`

**Interfaces:**
- Produces: `CausalIRStore.closeRuntime(data: CausalIRRuntimeCloseData): CausalIRCommitResult`.
- Produces: `CaseTrace.closeAll(input?: FinishTraceInput): TraceMaterializationRequest[]`.
- Produces: `TraceMaterializationRequest = { caseDir: string; caseID: string; runID: string; sessionID?: string; recordsFile: string }`.
- Produces: `finalizeWorkerTraces(...): Promise<TraceMaterializationRequest[]>`.

- [ ] **Step 1: Add failing journal-contract tests**

Add a test that calls `closeRuntime` and asserts the final line has operation
`case.runtime_closed`, contains status/result/manifest identity, preserves the hash chain,
and is accepted as terminal by `validateCausalIRJournal`. Add a test that rejects any
entry after `case.runtime_closed`.

- [ ] **Step 2: Run the focused Causal IR test and verify RED**

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Expected: FAIL because `closeRuntime` and `case.runtime_closed` do not exist.

- [ ] **Step 3: Implement the minimal runtime-close operation**

Extend the operation union and validator with:

```ts
export type CausalIRRuntimeCloseData = {
  format: "runtime_close"
  status: "success" | "error" | "cancelled"
  closed_at: string
  result?: unknown
  error?: unknown
  manifest: {
    case_id: string
    run_id: string
    session_id?: string
  }
}
```

`closeRuntime` appends only this compact operation. It must not call `snapshot()`,
`synchronize()`, provenance projection, or JSON document serialization.

- [ ] **Step 4: Add failing CaseTrace and worker tests**

Assert that `CaseTrace.closeAll()` returns materialization requests, leaves
`records.jsonl` with a valid runtime-close terminal entry, and does not create
`trace.json`. Assert the TUI worker helper returns the requests without propagating trace
errors.

- [ ] **Step 5: Run the focused tests and verify RED**

Run: `bun test test/observability/case-trace.test.ts test/cli/tui/worker-trace.test.ts --timeout 30000`

Expected: FAIL because runtime close still performs full terminal projection.

- [ ] **Step 6: Implement journal-only worker shutdown**

Add `ActiveCaseTrace.closeRuntime()` that records open-lifecycle cancellation summaries
without global graph scans, appends `case.runtime_closed`, marks the trace closed, and
returns immutable paths. Keep the existing `finish()` compatibility path until Task 3.
Change `finalizeWorkerTraces` to use `closeAll`.

- [ ] **Step 7: Run focused tests and commit**

Run the two commands from Steps 2 and 5. Both must pass.

Commit: `feat(observability): add lightweight runtime trace close`

---

### Task 2: Recoverable Trace Materializer Command

**Files:**
- Create: `packages/opencode/src/observability/trace-materializer.ts`
- Create: `packages/opencode/src/cli/cmd/trace-finalize.ts`
- Modify: `packages/opencode/src/index.ts`
- Modify: `packages/trace-renderer/src/cli.ts`
- Test: `packages/opencode/test/observability/trace-materializer.test.ts`
- Test: `packages/opencode/test/cli/trace-finalize.test.ts`
- Test: `packages/trace-renderer/test/cli.test.ts`

**Interfaces:**
- Consumes: `case.runtime_closed` from Task 1.
- Produces: `materializeTrace(input: { caseDir: string }): TraceMaterializationResult`.
- Produces: `TraceMaterializationResult = { caseDir: string; traceFile: string; manifestFile: string; partialFile: string; completeness: "complete" | "incomplete"; recoveredLines: number }`.
- Produces: hidden OpenCode command `trace-finalize <case-dir>`.
- Extends: `observable-trace finalize <case-dir>`.

- [ ] **Step 1: Write failing materializer tests**

Create journals for complete runtime close, missing close after simulated `SIGKILL`, and a
torn final JSONL line. Assert complete journals produce a successful canonical
`trace.json`; interrupted journals produce `recovery_status: "incomplete_journal_replay"`;
and torn tails preserve all prior valid entries while reporting the dropped line.

- [ ] **Step 2: Run the focused materializer test and verify RED**

Run: `bun test test/observability/trace-materializer.test.ts --timeout 30000`

Expected: FAIL because `trace-materializer.ts` does not exist.

- [ ] **Step 3: Implement journal recovery and atomic outputs**

Read JSONL line by line, stop only at an invalid trailing line, validate the valid prefix,
replay current graph state, construct a canonical Causal IR document, and atomically write
`trace.json`, `manifest.json`, and `partial/latest.json`. Do not modify journal or artifact
bytes. Non-tail corruption must fail materialization.

- [ ] **Step 4: Add failing CLI tests**

Assert both commands accept a case directory, print completeness and output paths, reject
unknown arguments, and never accept `trace.html` as semantic input.

- [ ] **Step 5: Run CLI tests and verify RED**

Run: `bun test test/cli/trace-finalize.test.ts --timeout 30000`

Run: `bun test test/cli.test.ts --timeout 30000` from `packages/trace-renderer`.

Expected: FAIL because the finalize commands are not registered.

- [ ] **Step 6: Implement both CLI surfaces**

Register the hidden OpenCode command and add `finalize` parsing to `observable-trace`. Both
must call the same `materializeTrace` implementation.

- [ ] **Step 7: Run focused tests and commit**

Run all commands from Steps 2 and 5. They must pass.

Commit: `feat(observability): materialize trace from runtime journal`

---

### Task 3: Isolated Automatic Materialization

**Files:**
- Create: `packages/opencode/src/cli/cmd/tui/trace-materializer-process.ts`
- Modify: `packages/opencode/src/cli/cmd/tui/thread.ts`
- Modify: `packages/opencode/src/cli/cmd/tui/worker.ts`
- Modify: `packages/opencode/src/observability/trace-publication.ts`
- Test: `packages/opencode/test/cli/tui/trace-materializer-process.test.ts`
- Test: `packages/opencode/test/cli/tui/thread.test.ts`

**Interfaces:**
- Consumes: `TraceMaterializationRequest[]` returned by worker shutdown.
- Produces: `materializeWorkerTraces(requests, options?): Promise<TracePublication[]>`.
- Spawns: current executable with `trace-finalize <case-dir>` in a separate process.

- [ ] **Step 1: Write failing process-isolation tests**

Use a fixture executable to assert one child per unique case directory, bounded concurrency
of one, inherited trace-only environment, captured exit status, and continuation after one
child failure. Assert no request changes the worker's original shutdown result.

- [ ] **Step 2: Run the process helper test and verify RED**

Run: `bun test test/cli/tui/trace-materializer-process.test.ts --timeout 30000`

Expected: FAIL because the helper does not exist.

- [ ] **Step 3: Implement isolated child execution**

Build the development command from `process.execPath` plus `process.argv[1]`; build the
compiled command from `process.execPath` alone. Wait for each child, collect publications,
and convert spawn/exit failures into trace-only warnings.

- [ ] **Step 4: Add failing TUI lifecycle tests**

Assert shutdown order is: worker runtime close, worker termination, child materialization,
publication. Assert Ctrl-C and normal TUI exit both produce `trace.json`. Assert a failed
materializer still permits process exit with the original code.

- [ ] **Step 5: Run TUI tests and verify RED**

Run: `bun test test/cli/tui/thread.test.ts test/cli/tui/worker-trace.test.ts --timeout 30000`

Expected: FAIL because the parent does not materialize returned requests.

- [ ] **Step 6: Wire parent/worker lifecycle and publication**

Return requests over RPC, terminate the worker before launching materialization, print the
logical directory plus final JSON path, and retain the one-hour configurable shutdown wait
for journal close only.

- [ ] **Step 7: Re-run the 3,000-observation probe and commit**

The worker-side close must not exceed the pre-close RSS by more than 128 MB. Run the focused
tests from Steps 2 and 5.

Commit: `fix(opencode): isolate trace materialization from TUI worker`

---

### Task 4: Disk-backed Causal IR Runtime Store

**Files:**
- Create: `packages/opencode/src/observability/causal-ir-runtime-store.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/causal-ir-runtime-store.test.ts`
- Test: `packages/opencode/test/observability/case-trace-memory.test.ts`

**Interfaces:**
- Produces: `CausalIRRuntimeStore` with `createNode`, `updateNode`, `replaceNodes`,
  `createEdge`, `replaceEdges`, `createArtifact`, `reuseArtifact`, `createDiagnostic`,
  `resolveReference`, targeted query methods, journal summary, and runtime close.
- Produces: `CausalIRNodeQuery = { ids?: string[]; kinds?: string[]; component?: string; status?: string; sessionID?: string; messageID?: string; callID?: string; limit?: number; reverse?: boolean }`.
- Uses: segment-local `index.sqlite`; `records.jsonl` remains commit authority.

- [ ] **Step 1: Write failing equivalence and eviction tests**

Feed the same create/update/edge/artifact sequence to in-memory `CausalIRStore` and the new
runtime store. Assert equivalent replayed snapshots, alias resolution, hash-chain entries,
and query results. Insert 2,000 nodes and assert the hot object count never exceeds 512 and
hot edge count never exceeds 1,024.

- [ ] **Step 2: Run the store test and verify RED**

Run: `bun test test/observability/causal-ir-runtime-store.test.ts --timeout 30000`

Expected: FAIL because the runtime store does not exist.

- [ ] **Step 3: Implement SQLite materialization and bounded LRUs**

Create WAL-mode tables for entities, aliases, references, and metadata. Append JSONL before
committing its index transaction. Store complete canonical JSON by identity and keep only
bounded parsed objects in LRUs. Query cold entities from SQLite; do not return trace data to
Agent code.

- [ ] **Step 4: Refactor ActiveCaseTrace to targeted queries**

Replace full-array `find`, `filter`, `findIndex`, and iteration in runtime paths with query
methods. Keep whole-graph scans only in the external materializer. Replace node-count-based
semantic IDs with monotonic counters so eviction cannot reuse IDs.

- [ ] **Step 5: Write and run the failing memory acceptance test**

Spawn a child that writes 10,000 standard observations, forces GC every 500 records, and
reports warm-up and final RSS. Before the runtime store is selected, verify the test fails
the 128 MB growth bound.

Run: `bun test test/observability/case-trace-memory.test.ts --timeout 120000`

- [ ] **Step 6: Select the runtime store and verify GREEN**

Use `CausalIRRuntimeStore` for `ActiveCaseTrace`; retain `CausalIRStore` for offline replay
and schema tests. Re-run the store and memory tests and confirm both pass.

- [ ] **Step 7: Run semantic regression tests and commit**

Run: `bun test test/observability/case-trace.test.ts --timeout 120000`

Run: `bun test test/observability/causal-ir.test.ts --timeout 30000`

Commit: `refactor(observability): bound runtime causal ir memory`

---

### Task 5: Streaming SQLite Materializer

**Files:**
- Modify: `packages/opencode/src/observability/trace-materializer.ts`
- Create: `packages/opencode/src/observability/streaming-json-writer.ts`
- Test: `packages/opencode/test/observability/trace-materializer-memory.test.ts`

**Interfaces:**
- Preserves: `materializeTrace` from Task 2.
- Produces: streaming JSON object/array writer with atomic rename.

- [ ] **Step 1: Write failing large-journal memory test**

Generate a sparse 1 GB-equivalent journal fixture without retaining its payload in test
memory. Materialize it in a child process and assert RSS remains at or below 256 MB and the
output passes the canonical Causal IR validator.

- [ ] **Step 2: Run the memory test and verify RED**

Run: `bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

Expected: FAIL because the Task 2 materializer loads the complete journal and graph.

- [ ] **Step 3: Implement line-streamed replay into temporary SQLite**

Validate hash chains incrementally, upsert current entities by ID, retain lifecycle and
journal summaries as scalar state, and discard each parsed line after its transaction.

- [ ] **Step 4: Stream canonical JSON arrays from SQLite cursors**

Write the object prefix, stream nodes/edges/artifacts/diagnostics/records/dataflow edges with
comma control, write the suffix, `fsync`, and atomically rename. Never call `JSON.stringify`
on the full document.

- [ ] **Step 5: Verify memory and compatibility and commit**

Run the Task 2 materializer tests, trace-renderer CLI tests, and Step 2 command. All must pass.

Commit: `refactor(observability): stream trace materialization`

---

### Task 6: Immutable Session Segments and Resume

**Files:**
- Create: `packages/opencode/src/observability/trace-segment.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/trace-materializer.ts`
- Modify: `packages/trace-renderer/src/load.ts`
- Test: `packages/opencode/test/observability/trace-segment.test.ts`
- Test: `packages/trace-renderer/test/load.test.ts`

**Interfaces:**
- Produces: `openTraceSegment({ rootDir, logicalCaseID, sessionID, runID }): TraceSegment`.
- Produces: atomic root `session.json` with ordered segment descriptors.
- Extends: `materializeTrace` to merge all descriptors and add continuation edges.

- [ ] **Step 1: Write failing immutable-resume tests**

Create a first run, record exact journal/artifact hashes, simulate `SIGKILL`, and open the
same session again. Assert a new segment path is allocated and every prior byte is unchanged.

- [ ] **Step 2: Run the segment test and verify RED**

Run: `bun test test/observability/trace-segment.test.ts --timeout 30000`

Expected: FAIL because the current `open()` truncates the stable case directory.

- [ ] **Step 3: Implement atomic session manifest and segment allocation**

Use root `session.json` as the ordered index. Write each process under
`segments/<run-id>/`; set `continuation_of` to the preceding run; mark an unclosed preceding
segment `interrupted_unfinalized`; and atomically replace the manifest without modifying
segment files.

- [ ] **Step 4: Add failing unified-materialization tests**

Assert two segments produce one root `trace.json`, all node and edge scopes retain physical
run IDs, and an explicit `run.continuation` edge links segments. Assert attribution-facing
records include facts from both runs.

- [ ] **Step 5: Run tests and verify RED**

Run the segment test and `bun test test/load.test.ts --timeout 30000` from
`packages/trace-renderer`.

- [ ] **Step 6: Implement unified merge and legacy discovery**

Teach materializer and renderer to discover `session.json`. Treat an existing root
`records.jsonl` as immutable legacy segment zero. Preserve root `trace.json` as the unified
compatibility output.

- [ ] **Step 7: Run focused tests and commit**

Run all commands from Steps 2 and 5. They must pass.

Commit: `feat(observability): resume unified traces with immutable segments`

---

### Task 7: End-to-End Regression, Documentation, and Release Readiness

**Files:**
- Modify: `packages/opencode/test/observability/stress-cases/run-stress-cases.mjs`
- Modify: `packages/opencode/test/observability/benchmark-cases/run-featurebench-cases.mjs`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-14-bounded-segmented-trace-runtime-design.md` only if verified behavior requires clarification

**Interfaces:**
- Consumes all prior task interfaces.
- Produces user-facing TUI, serve, renderer, and attribution usage documentation.

- [ ] **Step 1: Add end-to-end regression cases**

Cover normal TUI exit, `SIGINT`, `SIGTERM`, `SIGKILL`, long-context compaction, tool/skill/MCP,
subagent interaction, resumed session, one failed materialization followed by retry, and
legacy flat-directory recovery.

- [ ] **Step 2: Run stress and benchmark regressions**

Run the focused observability suite from `packages/opencode`, then execute representative
stress and FeatureBench cases. Confirm existing semantic categories remain present.

- [ ] **Step 3: Verify passive-observer equivalence**

Run the same deterministic fixture with tracing enabled and disabled. Compare Agent-visible
messages, tool calls, file hashes, session rows, and exit code; permit differences only in
timing and trace-only stderr publication.

- [ ] **Step 4: Update README**

Document the segment layout, automatic and manual finalization, crash recovery, session
resume, trace location output, memory defaults, and the rule that attribution consumes root
`trace.json` or the logical case directory.

- [ ] **Step 5: Run final verification and commit**

Run `bun typecheck` from `packages/opencode` and `packages/trace-renderer`; run their focused
test suites; run `git diff --check`; and record measured runtime/materializer RSS.

Commit: `docs: document bounded resumable trace runtime`
