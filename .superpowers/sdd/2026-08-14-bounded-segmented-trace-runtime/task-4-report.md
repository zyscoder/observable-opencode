# Task 4 Report: Disk-backed Causal IR Runtime Store

## Checkpoint Status

Implementation is substantially complete but this checkpoint is **not declared complete** and is
**not committed**. The Task 4 focused tests and memory acceptance are GREEN. The package-wide
typecheck is RED in unchanged TUI/plugin/SDK dependency files, and the final full
`case-trace.test.ts` rerun was interrupted before Bun printed its final result.

Base HEAD remains `e8c9d86c1b1820ea51e2e04c4a10ef3ac462ad92`.

## Completed Implementation

- Added a WAL-mode `bun:sqlite` Causal IR runtime materialization with tables for nodes,
  edges, artifacts, diagnostics, aliases, references, payload hashes, and journal metadata.
- Kept `records.jsonl` authoritative: JSONL append succeeds before the SQLite transaction
  commits; append failure rolls back the index mutation and poisons the runtime store.
- Added 512-node and 1,024-edge parsed-object LRUs. Cold rows remain queryable from SQLite.
- Stored complete runtime and canonical JSON by entity identity and retained monotonic entity
  insertion order independently of cache residency.
- Added exact identity, kind, component, status, session, message, call, alias/reference,
  endpoint/relation, artifact, and diagnostic queries.
- Replaced `ActiveCaseTrace`'s long-lived complete Causal IR arrays with the runtime store and
  monotonic generated node/edge counters.
- Kept normal `closeRuntime()` bounded: it appends a compact runtime-close record and closes
  SQLite without constructing a complete graph.
- Preserved explicit legacy `CaseTrace.finish()` projection compatibility. That compatibility
  path still materializes a whole graph, as required by the existing projection tests.
- Added passive fallback to an in-memory poisoned store if the trace directory or disk SQLite
  cannot be opened, so observer storage failure does not alter Agent-visible process output.
- Corrected reverse-query ordering so failed cases retain the latest 16 finalized-open refs.

No runtime-store or observer data is returned to Agent decision code.

## RED Evidence

### Store did not exist

```text
bun test test/observability/causal-ir-runtime-store.test.ts --timeout 30000
Cannot find module '@/observability/causal-ir-runtime-store'
0 pass, 1 fail, 1 error
```

### Pre-migration memory

The child wrote 10,000 standard observations, forced GC after 1,000 warm-up records and
every 500 records thereafter, and compared warm/final RSS.

```text
Expected growth: <= 134,217,728 bytes
Observed growth: 579,158,016 bytes
Result: FAIL
```

### Self-review regressions caught by new tests

- Unavailable SQLite path: child exited `1` instead of preserving Agent-visible output.
- Latest-open-ref query: reverse query returned old records (`open_0` through `open_14`)
  instead of the latest records.

Both tests are GREEN after the scoped fixes.

## GREEN Evidence

### Runtime store equivalence, eviction, transaction, and passive isolation

```text
bun test test/observability/causal-ir-runtime-store.test.ts --timeout 30000
6 pass, 0 fail, 25 expect() calls
```

This covers replay/snapshot equivalence with `CausalIRStore`, journal hash chains, alias
resolution, generated diagnostics, replacement snapshots, directed queries, 2,000 nodes,
1,500 edges, cache bounds, cold lookup, append rollback, passive initialization failure,
and latest-16 ordering.

### Post-migration memory

```text
bun test test/observability/case-trace-memory.test.ts --timeout 120000
1 pass, 0 fail, 3 expect() calls
```

Direct child measurement:

```text
warm RSS:  442,892,288 bytes
final RSS: 466,190,336 bytes
growth:     23,298,048 bytes (22.22 MiB)
bound:     134,217,728 bytes (128 MiB)
```

### Causal IR semantic suite

```text
bun test test/observability/causal-ir.test.ts --timeout 30000
58 pass, 0 fail, 220 expect() calls
```

Focused regressions for tool convergence, subagent matching, and failed signal handling also
passed after fixing runtime scope indexing and runtime-store close timing.

## Failed or Incomplete Checks

### Package typecheck

```text
bun typecheck
exit 2
```

No error names a Task 4 changed file. Failures are in unchanged files and dependency state:

- implicit `any` parameters in `src/cli/cmd/tui/feature-plugins/sidebar/*.tsx`;
- duplicate worktree/root SDK client types in TUI/plugin/test fixture files;
- missing `zod`, `effect`, `@opentui/*`, and `semver` declarations in sibling packages.

Because the requested reproducible package compile check is RED, this checkpoint is not
committed.

### Full CaseTrace semantic regression

The latest command was interrupted before its final summary:

```text
bun test test/observability/case-trace.test.ts --timeout 120000
```

The captured prefix contained only passing tests, including Task 1-3 external runtime-close
coverage and the previously failing tool/subagent cases, but this is not sufficient to claim
the full suite GREEN. An earlier full run had 156 passes and 7 failures; six Task 4 query-scope
regressions were subsequently fixed and passed focused reruns. The remaining observed failure
was `persists semantic trace records with artifacts and redaction`: in this deep worktree the
long `run.start.environment` payload becomes artifact-backed before the semantic message, so
the test's positional `trace.artifacts[0]` assumption fails. No production behavior was changed
to accommodate that environment-sensitive assertion.

## Files Changed

- `packages/opencode/src/observability/causal-ir-runtime-store.ts` (new)
- `packages/opencode/src/observability/causal-ir.ts`
- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/test/observability/causal-ir-runtime-store.test.ts` (new)
- `packages/opencode/test/observability/case-trace-memory.test.ts` (new)
- `.superpowers/sdd/2026-08-14-bounded-segmented-trace-runtime/task-4-report.md` (new)

## Semantic-equivalence Risks

- Normal segmented runtime shutdown is bounded, but explicit legacy `CaseTrace.finish()` still
  performs whole-document projection for compatibility. Removing that scan belongs to the
  external-materializer migration, not this checkpoint.
- SQLite is a disposable observer index; journal replay remains authoritative. Crash recovery
  must rebuild from `records.jsonl`, not trust a possibly newer/partial SQLite file.
- Alias recanonicalization and replacement operations are covered by focused equivalence tests,
  but the interrupted full `case-trace` suite still needs a final uninterrupted run.
- Package-wide typecheck cannot currently establish a green repository baseline because of the
  unrelated errors listed above.

## Commit

No commit created at this checkpoint. Current base SHA:
`e8c9d86c1b1820ea51e2e04c4a10ef3ac462ad92`.

## Fix Round 1

### Status

All four load-bearing review findings are addressed on top of Task 4 commit
`18338240d8a46b41a20d7981a078bffaa4e2d2a4`. The fixes preserve `records.jsonl` as commit
authority, keep disabled/closed observer state passive, retire finalized traces from CaseTrace
routing, and cap recent reference indexes at 256 entries per category.

### RED Evidence

The tests were written and observed failing before production fixes:

1. **Second append durable prefix:** an unresolved-reference node successfully appended
   `node.created`, then failed the following diagnostic append. Raw SQLite returned no node,
   proving the outer transaction had rolled back an already durable JSONL prefix.
2. **Post-initialization SQLite faults:** after a warm node, injected mutation/query faults were
   ignored by the old runtime store; queries returned both warm and post-fault nodes instead of
   a passive empty result.
3. **Transaction/snapshot fault fallback:** injected transaction failure returned a committed
   finalization, and an injected snapshot-query failure made `finalize()` return `undefined`
   instead of a passive failed commit result.
4. **Finished trace routing:** a child that called `finishSession()`, emitted a late observation,
   then received SIGTERM exited `1` instead of the expected signal exit `143`; the late event
   reached a closed SQLite store.
5. **256-entry routing bound:** after remembering 257 `span:` refs, `span:span_0` still routed
   to its original trace. The session suite result was `23 pass, 1 fail`.

### Fixes

- Added central `passiveMutation`/`passiveQuery` guards and mutation/query/transaction fault
  injection points to `CausalIRRuntimeStore`.
- SQL faults disable and close the private SQLite observer store without throwing through public
  APIs. Queries after disable/close return empty snapshots/results; mutations return inert input
  copies or `{ committed: false, poisoned: true }`.
- Each SQLite transaction tracks only journal entries whose append callback succeeded. If a later
  append fails, the outer transaction rolls back, the successful durable prefix is rematerialized
  in SQLite, and the runtime store is then passively disabled. No uncommitted later diagnostic is
  indexed.
- Added active-only session resolution. `routed()` and `CaseTrace.get()` discard finalized traces,
  so late observer events and later process signals do not access their closed stores.
- Added per-prefix routing-reference LRU ownership with a 256-entry cap and category isolation.
  Runtime recent arrays remain below that global maximum, while recent/dedupe maps and sets now
  use bounded 256-entry insertion helpers.

### GREEN Verification

```text
bun test test/observability/causal-ir-runtime-store.test.ts --timeout 30000
8 pass, 0 fail, 47 expect() calls

bun test test/observability/causal-ir.test.ts --timeout 30000
58 pass, 0 fail, 220 expect() calls

bun test test/observability/case-trace-session.test.ts --timeout 30000
24 pass, 0 fail, 75 expect() calls

bun test test/observability/case-trace-runtime.test.ts --timeout 120000
28 pass, 0 fail, 7332 expect() calls

bun test test/observability/case-trace.test.ts \
  -t "keeps agent-visible result bytes and hash unchanged when passive trace writes fail" \
  --timeout 30000
1 pass, 0 fail, 34 expect() calls

bun test test/observability/case-trace-memory.test.ts --timeout 120000
1 pass, 0 fail, 3 expect() calls
```

Direct 10,000-observation child measurement after Fix Round 1:

```text
warm RSS:  446,496,768 bytes
final RSS: 456,802,304 bytes
growth:     10,305,536 bytes (9.83 MiB)
bound:     134,217,728 bytes (128 MiB)
```

`bun typecheck` still exits `2` only for the pre-existing TUI/plugin/SDK/dependency errors listed
earlier in this report. After correcting Fix Round 1's optional journal hash and typed edge-ref
issues, no changed Task 4 source or test file appears in the typecheck output.

### Files Changed in Fix Round 1

- `packages/opencode/src/observability/causal-ir-runtime-store.ts`
- `packages/opencode/src/observability/case-trace-session.ts`
- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/test/observability/causal-ir-runtime-store.test.ts`
- `packages/opencode/test/observability/case-trace-session.test.ts`
- `packages/opencode/test/observability/case-trace-runtime.test.ts`
- `.superpowers/sdd/2026-08-14-bounded-segmented-trace-runtime/task-4-report.md`

### Remaining Concerns

- SQLite durable-prefix rematerialization runs only after an append/transaction failure and then
  disables the store; `records.jsonl` remains the source for any later full rebuild.
- The package-wide typecheck baseline remains RED outside Task 4 scope.
- The unrelated environment-sensitive `trace.artifacts[0]` test was intentionally not changed.

## Fix Round 2

### Status

The remaining routing-ownership bound finding is addressed on top of Fix Round 1 commit
`1d94c55984527c21c79fa9c2431acbbdaec41925`. Routing state now has fixed category cardinality,
at most 256 retained reference keys per normalized category, and at most 256 recent trace owners
per retained key. This state remains observer-only and cannot feed Agent decisions or output.

### RED Evidence

Two tests were added before the production change and failed for the expected reasons:

```text
bun test test/observability/case-trace-session.test.ts \
  -t "retains at most 256 recent owners|normalizes arbitrary reference prefixes" \
  --timeout 30000

0 pass, 2 fail, 4 expect() calls
```

- After 257 traces claimed `span:shared`, the oldest trace still routed through that key.
- After 257 distinct arbitrary prefixes were remembered, the oldest unknown-prefix key still
  routed to its owner, proving that arbitrary prefixes could grow the category map without bound.

### Fix

- Derived a fixed known-category set from the typed reference keys emitted by `traceRouteHint`.
  Untyped refs use `untyped`; every unknown prefix shares the single `other` category.
- Kept the existing 256-key per-category insertion-order bound. Evicting a key from its category
  also deletes its `owners` entry, so the category index and reverse ownership map cannot diverge.
- Made each retained key's owner set a 256-entry recent-owner LRU using deterministic JavaScript
  `Set` insertion order. Re-remembering an owner refreshes it; adding owner 257 evicts the oldest.

### GREEN Verification

```text
bun test test/observability/case-trace-session.test.ts \
  -t "retains at most 256 recent owners|normalizes arbitrary reference prefixes" \
  --timeout 30000
2 pass, 0 fail, 6 expect() calls

bun test test/observability/case-trace-session.test.ts --timeout 30000
26 pass, 0 fail, 81 expect() calls

bun test test/observability/case-trace-runtime.test.ts --timeout 120000
28 pass, 0 fail, 7332 expect() calls

bun test test/observability/case-trace-memory.test.ts --timeout 120000
1 pass, 0 fail, 3 expect() calls
```

The memory acceptance test again kept 10,000 observations within its 128 MiB RSS-growth bound.
`bun typecheck` still exits `2` only for the pre-existing TUI/plugin/SDK/dependency errors recorded
above; neither Fix Round 2 file appears in its output.

### Files Changed in Fix Round 2

- `packages/opencode/src/observability/case-trace-session.ts`
- `packages/opencode/test/observability/case-trace-session.test.ts`
- `.superpowers/sdd/2026-08-14-bounded-segmented-trace-runtime/task-4-report.md`

### Semantic-equivalence Risk

- Unknown typed prefixes now compete inside one 256-key observer-routing category. Evicted unknown
  refs use the existing process/isolated trace fallback; this can reduce observer attribution
  precision for stale refs but cannot alter Agent-visible behavior.
- Root/alias lifecycle ownership is outside this finding. This round bounds only the recent
  reference-routing indexes identified by review and does not broaden session lifecycle behavior.
