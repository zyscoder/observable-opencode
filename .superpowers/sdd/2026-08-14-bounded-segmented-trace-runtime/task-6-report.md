# Task 6 Report: Immutable Session Segments and Unified Resume

Date: 2026-08-15

Status: **partial, reproducible checkpoint**. The focused segment, materializer, and renderer-load paths pass. The broader `case-trace` compatibility suite is not green and is listed under blockers rather than being expanded in this checkpoint.

## Implemented

- Added `openTraceSegment({ rootDir, logicalCaseID, sessionID, runID })` and an atomically replaced root `session.json`.
- New process runs write runtime evidence to a new immutable `segments/<segment-id>/` directory. Reopening never reuses or truncates a prior segment.
- A still-running predecessor is changed to `interrupted_unfinalized` in `session.json` only. Its `segment.json`, journal, artifacts, and SQLite index are not changed.
- A known session ID is discovered from disk across separate processes; no in-memory trace registry is required.
- Existing root `records.jsonl` is discovered as immutable `legacy-root` segment zero and is neither moved nor rewritten.
- Duplicate `run_id` values are rejected. A pre-existing orphan physical directory is preserved and the new segment path is disambiguated (`run_collision-2`, and so on).
- Root manifest writes use create/write/fsync/rename and best-effort directory fsync. A failed publication removes the unpublished segment and leaves the previous manifest bytes unchanged.
- Runtime close returns a `TraceMaterializationRequest` whose `caseDir` is the logical root while `recordsFile` identifies the physical segment journal.
- Root materialization replays every ordered segment through a disk-backed SQLite index, scopes colliding entities by run, rewrites segment artifact paths, and emits an explicit `continued_from` edge with `metadata.provenance_type = "run.continuation"`.
- The renderer accepts a logical root or direct `session.json`, rematerializes the unified root trace, and retains flat `trace.json` / `records.jsonl` loading.
- `opencode run -s <sessionID>` passes the known session ID into trace configuration early enough for cross-process root discovery.
- Segment allocation failures degrade tracing to the existing passive in-memory observer path instead of breaking the observed command.

## Exact Directory Example

```text
$OPENCODE_CASE_TRACE_DIR/
  session--ses_123--a1b2c3d4/
    session.json                         # ordered mutable root metadata
    trace.json                           # unified materialized compatibility output
    manifest.json
    partial/latest.json
    segments/
      run_01HAAA/
        segment.json                     # immutable descriptor snapshot
        records.jsonl                    # authoritative segment journal
        events.jsonl
        raw-events.jsonl
        index.sqlite
        artifacts/
          sha256/<digest>
        trace.json                       # segment-local terminal snapshot, when finalized
      run_01HBBB/
        segment.json
        records.jsonl
        events.jsonl
        raw-events.jsonl
        index.sqlite
        artifacts/
          sha256/<digest>
```

A legacy trace may additionally keep these files at the logical root:

```text
session--ses_123--a1b2c3d4/
  records.jsonl                          # immutable legacy-root segment zero
  index.sqlite
  artifacts/
```

Those legacy bytes remain in place. `session.json.segments[0].path` is `"."` and its `segment_id` is `"legacy-root"`.

## RED

The first immutable-resume test was run against the old stable-directory implementation. Reopening the same case changed the original journal SHA-256 from the expected `2b8692e75240d351132da52595e1f5bff71d796492f734b2ac3ce17f2b22d430` to `c5ac9d014a730aed4dd6ac05a265fe12284a87a598fa0fd29a1439ed8dc2ed35`, proving old evidence was truncated/replaced.

Additional failing stages were observed before implementation:

- orphan segment collision raised `EEXIST`; failed manifest publication left an unpublished segment; root-only finalization returned false;
- runtime close exposed the physical directory instead of the logical root;
- unified materialization attempted root `records.jsonl` and failed with `ENOENT`;
- legacy root discovery omitted `legacy-root`;
- renderer source selection only recognized flat `trace.json` / `records.jsonl`;
- direct terminal publication exposed the physical segment;
- separate processes without a shared case registry chose different logical roots for the same session ID.

The SIGKILL regression test launches a real Bun child, waits until its journal/artifact/index exist, sends `SIGKILL`, records SHA-256 values for `records.jsonl`, every content-addressed artifact, and `index.sqlite`, then launches the continuation and compares every prior hash. Hashes are calculated at runtime because run IDs and SQLite bytes are intentionally not fixed fixtures.

## GREEN

Fresh checkpoint verification on 2026-08-15:

```text
cd packages/opencode
bun test test/observability/trace-segment.test.ts --timeout 30000
# 9 pass, 0 fail, 70 expect() calls

bun test test/observability/trace-materializer.test.ts --timeout 30000
# 7 pass, 0 fail, 21 expect() calls

cd packages/trace-renderer
bun test test/load.test.ts --timeout 30000
# 21 pass, 0 fail, 74 expect() calls
```

The focused segment suite covers real SIGKILL byte hashes, cross-process resume, segment/run collisions, atomic manifest failure, immutable `segment.json`, logical-root materialization requests, two-segment unified output, legacy root discovery, artifact reachability, and direct root publication.

Previously run focused checks also passed but were not rerun for this stop-now checkpoint:

- 1 GiB sparse journal materialization: 1 pass, measured peak max RSS 255,524,864 bytes, below the 256 MiB limit of 268,435,456 bytes.
- TUI worker/publication tests: 7 pass.
- Renderer CLI tests: 15 pass.
- Renderer typecheck: pass.
- `git diff --check`: pass both before and after focused verification.

## Blockers And Compatibility Risks

1. The full `packages/opencode/test/observability/case-trace.test.ts` suite was previously run and is not green. It was not rerun after the user's stop-now instruction. Failures include old tests expecting `records.jsonl`, `raw-events.jsonl`, `legacy-trace.json`, and `provenance-trace.json` at the logical root, while new runtime evidence intentionally lives in the physical segment. Root compatibility policy for the two derived legacy projection files still needs a deliberate decision.
2. Unified materialization currently namespaces IDs for any segmented root, including a single new segment. Several existing semantic tests expect the historical unscoped IDs, and embedded reference strings inside arbitrary payload data are not all rewritten. Multi-run collision handling is covered; flat/single-segment ID compatibility is incomplete.
3. The unified materializer emits a reduced aggregate manifest/metrics document. Rich terminal fields produced by `ActiveCaseTrace` (for example detailed trace-health, subject-revision, and shutdown metadata) are not all retained at the logical root.
4. `session.json` publication is atomic against torn replacement, but there is no inter-process lock or compare-and-swap. Two processes opening the same session concurrently can race between reading and replacing the ordered manifest.
5. Session discovery scans immediate child directories of the configured trace root. Duplicate matching roots are rejected, but simultaneous first-time creators can still allocate separate roots before either manifest becomes visible.
6. Legacy terminal detection reads a bounded 4 MiB tail. This is bounded and passive, but a legacy journal with more than 4 MiB of trailing nonterminal/corrupt data could be classified as interrupted.
7. Terminal publication intentionally catches materialization errors to preserve passive-observer behavior. In that case the immutable segment remains authoritative, but an older root `trace.json` can remain stale until a later explicit materialization succeeds.
8. The broader opencode typecheck has pre-existing unrelated errors in sidebar/plugin/dependency/semver areas. Task 6-specific type errors were removed during development, but the full typecheck is not a green completion gate for this checkpoint.

The unrelated `artifact[0]` baseline assertion was not changed.

## Checkpoint Scope

This commit is suitable for reproducing and reviewing the immutable-segment architecture and focused behavior. It should not be treated as fully complete Task 6 until the root compatibility outputs, single-segment ID compatibility, rich aggregate metadata, and concurrent manifest update policy are resolved and the broad `case-trace` suite is green apart from the explicitly excluded baseline.

## Completion

Status on 2026-08-15: **Task 6 complete within the explicitly excluded `artifact[0]` baseline**. This section supersedes the checkpoint blockers above.

### Completed Behavior

- New segmented runs expose only unified root `session.json`, `trace.json`, `manifest.json`, `partial/latest.json`, `legacy-trace.json`, and `provenance-trace.json`. They do not create root `records.jsonl`, `events.jsonl`, or `raw-events.jsonl`; runtime authority remains in the physical segment.
- Root legacy compatibility artifact paths remain reachable through immutable content-addressed hardlinks/copies under root `artifacts/`. Unified canonical and provenance documents use unambiguous `segments/<segment-id>/artifacts/...` paths.
- A single non-legacy segment preserves historical node, edge, and artifact IDs. Two or more segments use deterministic `<segment-id>::<entity-type>::<original-id>` identities, rewrite typed and legacy references, retain original IDs plus run/segment metadata, and add an explicit `run.continuation` edge.
- Unified manifest construction starts with the newest valid terminal segment manifest and retains result/error, subject revision, shutdown, recovery, trace-health, and status fields before overlaying logical session and segment summaries.
- Metrics aggregate numeric leaves by sum and arrays by ordered concatenation. Graph counts are recalculated from unique materialized entities. Multi-segment output documents this as `session_segments_v1` in `metrics.aggregation`.
- Cross-process lock directories serialize root discovery, first creation, segment allocation, session binding, and finalization. The lock key is session ID when known and logical case otherwise; waits are bounded, dead/stale locks are recoverable, nonce ownership prevents deleting a successor lock, and observer failures remain passive.
- `run.start` is now guaranteed to be the first causal journal entity even with an 8-byte summary limit: the store creates a minimal unsummarized node, then records artifact-backed startup detail as an update.
- Root canonical and partial projections remain hardlinked on supported filesystems, while every replacement is atomic. A failed `session.json` replacement removes the unpublished segment and preserves the previous manifest bytes.

### Final Directory Example

```text
$OPENCODE_CASE_TRACE_DIR/reused-stable-case/
  session.json
  trace.json
  manifest.json
  legacy-trace.json
  provenance-trace.json
  partial/latest.json
  artifacts/sha256/<compatibility-hardlink>
  segments/
    7bf0.../
      segment.json
      records.jsonl
      events.jsonl
      raw-events.jsonl
      index.sqlite
      artifacts/sha256/<digest>
    a219.../
      segment.json
      records.jsonl
      events.jsonl
      raw-events.jsonl
      index.sqlite
      artifacts/sha256/<digest>
```

There is no mutable or symlinked root journal alias. A pre-existing legacy root is the sole exception: its original root `records.jsonl`, `index.sqlite`, and artifacts remain immutable segment zero and are never moved or rewritten.

### Completion RED/GREEN

The original RED remains the byte-level reopen proof recorded above. Additional RED stages in this completion pass covered single-segment ID regression, colliding two-segment references, reduced aggregate metadata, concurrent lost updates, live/stale lock behavior, ultra-low-limit startup artifact ordering, root/physical TUI request confusion, and stale root projections after a resumed SIGKILL.

Fresh GREEN evidence:

```text
cd packages/opencode
bun test test/observability/trace-segment.test.ts
# 14 pass, 0 fail, 223 expect() calls

bun test test/observability/trace-materializer.test.ts
# 7 pass, 0 fail, 21 expect() calls

bun test test/cli/tui/worker-trace.test.ts test/cli/tui/trace-materializer-process.test.ts
# 4 pass, 0 fail, 26 expect() calls

cd packages/trace-renderer
bun test test/load.test.ts test/cli.test.ts
# 36 pass, 0 fail, 188 expect() calls

bun run typecheck
# pass
```

The full `case-trace` run completed with 161 pass and 2 reported failures. One was an unrelated timing outlier; its exact test immediately passed alone in 330 ms. The only reproducible failure is the explicitly excluded, unchanged `trace.artifacts[0]` baseline assertion in `persists semantic trace records with artifacts and redaction`: artifact zero is `run.start.environment`, while the assertion assumes it is the later semantic model-message artifact.

`packages/opencode` typecheck still exits 2 only for the pre-existing sidebar implicit-any errors, duplicate worktree SDK private types, plugin overload/dependency resolution, and semver declaration issues listed in the command output. No Task 6 file appears in that error list. `git diff --check` passes.

### Compatibility Risks

- Root `legacy-trace.json` remains a latest-terminal compatibility projection rather than a merged legacy-schema history; the unified canonical/provenance projections are the complete multi-segment history.
- Root compatibility artifacts are immutable content-addressed links/copies and may accumulate across resumes. Segment artifacts remain authoritative and are never rewritten.
- Lock timing defaults are a 5-second bounded wait and 30-second stale threshold, configurable through `OPENCODE_TRACE_SEGMENT_LOCK_WAIT_MS` and `OPENCODE_TRACE_SEGMENT_LOCK_STALE_MS`.
- Session discovery scans immediate logical roots and rejects duplicate matches. The observer degrades passively if discovery or lock acquisition cannot establish a unique owner.
- A reviewer subagent was unavailable in this environment; completion used a direct requirement/diff audit plus the focused and broad verification above.

### Final Completion Checkpoint

Per the stop-and-checkpoint request, no further implementation scope was added. The last fully completed broad run was:

```text
bun test test/observability/case-trace.test.ts
# 161 pass, 2 fail, 2128 expect() calls
```

The two reported failures were:

1. `keeps decision generation provenance within the decision session` timed out once after an abnormal 941,770 ms measurement; its immediate isolated rerun passed in 330 ms.
2. `persists semantic trace records with artifacts and redaction` is the explicitly excluded unchanged `artifact[0]` baseline. It expects artifact zero to contain `semantic model message`, but artifact zero is the earlier `run.start.environment` JSON artifact.

A later broad rerun was interrupted by the user after the formerly timed-out test had passed in-suite at 260 ms, so it is not claimed as a completed full-suite result. The final minimal reproducible command was:

```text
bun test test/observability/trace-segment.test.ts test/observability/case-trace.test.ts \
  -t '(direct finish materializes and publishes|persists semantic trace records with artifacts and redaction)'
# 1 pass, 1 fail, 40 expect() calls
```

The passing test verifies logical-root publication with physical segment runtime storage. The sole minimal failure is the exact excluded assertion above. Fresh focused completion totals remain segment 14/0, materializer 7/0, renderer load/CLI 36/0, and TUI worker/materializer 4/0.

## Fix Round 1/5 Completion

Status on 2026-08-15: **review round 1 complete**, with the explicitly excluded `artifact[0]` assertion still unchanged.

### RED

Fresh failing regressions were added before each production fix:

- A live lock over an existing legacy root allowed terminal emergency paths to run after allocation failure. The child wrote a persistence-failure diagnostic instead of remaining fully memory-only. Byte hashes covered root records, index, artifacts, canonical/legacy/provenance projections, manifest, and partial output.
- An aged lock owned by the current live PID was eligible for stale recovery; late session binding changed lock identity; and release did not provide a nonce-owned rename boundary against successor deletion.
- Recursive colon rewriting changed `https://...`, ordinary text, and paths while failing to rewrite bare `artifact_id`, `node_id`, typed `ref_id`, diagnostic refs, and terminal/metric refs.
- Renderer session discovery created root derived outputs and preferred `session.json` over an existing `trace.json`.
- Incomplete history forced a valid successful terminal to `error`, legacy terminal lines over 4 MiB were classified as interrupted, process traces inherited the configured session, and same-path compatibility artifacts silently retained older bytes.
- A staged publication race test held the stable root lock, advanced `session.json.generation` after staging, and proved the old commit marker remained until the changed snapshot was discarded and regenerated.

### GREEN Behavior

- Segment allocation failure now permanently sets `persistenceEnabled=false` for that trace. Every file open/truncate/append/remove, artifact, segment finalize, materialize, and publication path is gated; in-memory causal recording remains available.
- `session.json` stores one immutable `lock_key` plus monotonically increasing `generation`. Allocation, late binding, finalization, and publication use that same key. Confirmed live PIDs are never age-evicted; dead owners and sufficiently old malformed locks recover. Release verifies the nonce and atomically renames the owned lock directory to a nonce-specific tombstone before deletion.
- Segment identity maps drive schema-aware rewriting. Canonical typed refs/endpoints, declared `*_ref`/`*_refs` fields, semantic node/edge/artifact/diagnostic ID fields, diagnostics, terminal result, and metrics are rewritten; URLs, paths, and ordinary strings are untouched. The regression includes a unified graph-reference validator.
- Segmented publication materializes to an OS staging directory from one session snapshot, acquires the stable root lock, compares generation, retries changed snapshots, publishes compatibility files first, and renames `trace.json` last as the commit marker. Root `trace.json` and `partial/latest.json` retain one inode on supported filesystems.
- Renderer loading reads an existing root trace directly. If only `session.json` exists it materializes into an OS temporary directory, reads it, and removes it; recursive root file hashes are identical before and after loading.
- One newest valid terminal supplies run ID, status, result/error/recovery, source files, and legacy projection consistently. Other interrupted segments set `historical_interruptions` and `session_recovery` without changing a successful terminal status.
- Compatibility artifact publication hashes source and destination bytes. Different content at the same path is stored immutably at `artifacts/sha256/<full-sha256>` and the latest legacy projection is rewritten; the old path and bytes remain unchanged.
- Legacy terminal discovery scans backward with a reusable 64 KiB buffer and parses the actual final complete line, including a tested terminal over 5 MiB. Physical segment creation is followed by `fsync(segmentsDir)` before publishing the session manifest.

### Exact Layout

```text
$OPENCODE_CASE_TRACE_DIR/session-case/
  session.json                 # lock_key + generation + ordered descriptors
  trace.json                   # last-published commit marker
  manifest.json
  legacy-trace.json
  provenance-trace.json
  partial/latest.json          # hardlink of trace.json where supported
  artifacts/
    fact.txt                   # older immutable compatibility bytes
    sha256/<full-sha256>       # collided latest compatibility bytes
  segments/
    run-first/
      segment.json
      records.jsonl
      index.sqlite
      artifacts/...
    run-second/
      segment.json
      records.jsonl
      index.sqlite
      artifacts/...
```

No new segmented run creates root `records.jsonl`, `events.jsonl`, or `raw-events.jsonl`. A pre-existing root journal remains immutable legacy segment zero.

### Final Verification

```text
packages/opencode:
  trace-segment.test.ts                                  23 pass, 0 fail, 307 expects
  trace-materializer.test.ts + diagnostics              8 pass, 0 fail, 24 expects
  trace-materializer-process.test.ts                    1 pass, 0 fail, 6 expects
  case-trace.test.ts                                    162 pass, 1 fail, 2133 expects

packages/trace-renderer:
  load.test.ts + cli.test.ts                            36 pass, 0 fail, 190 expects
  bun run typecheck                                     pass

repository:
  git diff --check                                      pass
```

The sole full case-trace failure is the unchanged, explicitly excluded `persists semantic trace records with artifacts and redaction` baseline. It assumes `trace.artifacts[0]` is the semantic-message artifact; artifact zero is the earlier `run.start.environment` artifact.

`packages/opencode` typecheck exits 2 only for pre-existing sidebar implicit-any errors, worktree/main SDK private-type duplication, plugin overload/dependency resolution, and the semver declaration. No observability or Task 6 file remains in the error list.

### Compatibility Risks

- An existing root `trace.json` is a committed snapshot and renderer loading does not replace it. Explicit materialization/finalization is required to publish a newer session generation.
- Root legacy output remains the newest valid terminal's compatibility projection, not a merged legacy-schema history. Canonical and provenance outputs contain the complete segmented graph.
- Content-addressed compatibility artifacts intentionally accumulate across resumes. Segment-local artifacts remain authoritative and are never changed.
- Malformed locks recover only after the configured stale threshold; a syntactically valid lock owned by a live PID can require operator/process resolution rather than age-based eviction.

## Fix Round 2/5 Completion

Status on 2026-08-15: **review round 2 complete**, with only the explicitly excluded and unchanged `artifact[0]` baseline failing in the full case-trace suite.

### RED

The new regressions were run before the production changes. `trace-segment.test.ts` initially reported 17 pass and 9 fail; renderer load reported 20 pass and 2 fail. The failures proved:

- malformed `session.json` and malformed root legacy journals gained a new `segments/` directory before validation, changing a full byte/tree SHA-256;
- session/case lock keys bypassed a held global lock, and two independently allocated unknown traces could both bind the same session ID;
- suffix matching rewrote `customer_node_id`, an unresolved artifact ID, an unresolved typed reference, and an HTTPS value in `documentation_ref`;
- root derived files were independent regular files with no `.derived/current` generation boundary;
- a stale root trace was loaded as complete after a resumed process was SIGKILLed instead of replaying all current immutable segments into temporary output.

The publication regression injects failure at `generation_ready`, `root_links_ready`, `current_swap_ready`, and `current_swapped`. After each failure it compares both the stable compatibility-file hashes and a recursive tree hash, including `.derived`, against the old generation.

### GREEN Behavior

- `openTraceSegment` acquires one constant `trace-root-manifest-v1` lock for the configured trace root. Discovery, target-root validation, first creation, allocation, late bind, finalize, and publication all use this domain; no operation switches keys after a session becomes known.
- Existing target roots are parsed before creating `segments/` or any child. Invalid session descriptors or invalid legacy identity lines throw without changing the pre-existing tree. Allocation cleanup removes newly created empty directories if manifest publication fails.
- Late bind scans every manifest while holding the global lock. A duplicate session root is rejected, different session IDs bind independently, and `ActiveCaseTrace.sessionID` changes only after `TraceSegment.bindSessionID()` succeeds. Lock timeout leaves the active observer unbound and passive.
- Entity declarations are pre-registered with a bounded JSONL line scan. Reference rewriting uses explicit node, edge, artifact, diagnostic, and reference-key sets; it rewrites only resolved IDs or explicit `node:`, `record:`, `edge:`, `artifact:`, and `diagnostic:` schemes. `documentation_ref`, URLs, paths, ordinary text, `customer_node_id`, and unknown typed fields remain unchanged. The unified graph validator checks every rewritten endpoint and artifact/diagnostic reference.
- A published session generation is built under `.derived/generations/<session-generation>/`. Root compatibility paths are stable symlinks through `.derived/current`, and one atomic symlink rename switches the whole set. `trace.json` and `partial/latest.json` remain hardlinked inside the generation. Mid-install errors and injected post-swap errors restore the old links/current target and remove the uncommitted generation.
- Legacy root journals, indexes, and artifact directories remain byte-identical segment zero. Their compatibility projection can address generated artifacts through `.derived/current/artifacts/...` without replacing the authoritative root `artifacts/` directory.
- Renderer load compares `trace.manifest.session_generation` with `session.json.generation`. Equal generations read the committed root. Missing or stale output is materialized under the OS temporary directory, includes running/SIGKILL-interrupted segments, reports `incomplete`, and leaves every root hash unchanged.

### Exact Derived Layout

```text
$OPENCODE_CASE_TRACE_DIR/session-case/
  session.json
  trace.json                 -> .derived/current/trace.json
  manifest.json              -> .derived/current/manifest.json
  legacy-trace.json          -> .derived/current/legacy-trace.json
  provenance-trace.json      -> .derived/current/provenance-trace.json
  partial/latest.json        -> ../.derived/current/partial/latest.json
  artifacts                  -> .derived/current/artifacts
  .derived/
    current                  -> generations/6
    generations/
      4/
        trace.json
        manifest.json
        legacy-trace.json
        provenance-trace.json
        partial/latest.json  # hardlink of this generation's trace.json
        artifacts/...
      6/
        trace.json
        manifest.json
        legacy-trace.json
        provenance-trace.json
        partial/latest.json
        artifacts/...
  segments/
    run-first/records.jsonl
    run-second/records.jsonl
```

For a legacy segment-zero root, the original root `records.jsonl`, `index.sqlite`, and `artifacts/` remain regular authoritative paths and are not replaced by derived symlinks.

### Final Verification

```text
packages/opencode:
  trace-segment.test.ts                                  27 pass, 0 fail, 362 expects
  trace-materializer.test.ts                             7 pass, 0 fail, 21 expects
  trace-materializer-memory.test.ts                      1 pass, 0 fail, 5 expects
  worker-trace.test.ts + thread.test.ts                 10 pass, 0 fail, 31 expects
  case-trace.test.ts                                   162 pass, 1 fail, 2134 expects

packages/trace-renderer:
  load.test.ts + cli.test.ts                            37 pass, 0 fail, 196 expects
  bun run typecheck                                     pass

repository:
  git diff --check                                      pass
```

The 1 GiB sparse-journal run measured peak RSS at 261,292,032 bytes, below the 268,435,456-byte limit. One combined materializer command emitted a spurious unnamed Bun hook timeout with an impossible 935,746 ms duration despite a roughly four-second wall clock; immediate independent reruns of the normal and 1 GiB files passed 7/7 and 1/1 and are the reported results.

The full case-trace failure is exactly `persists semantic trace records with artifacts and redaction`. Its unchanged assertion assumes `trace.artifacts[0]` contains `semantic model message`; artifact zero is the earlier `run.start.environment` JSON artifact. No Task 6 code or test changes that baseline assertion.

`packages/opencode` typecheck still exits 2 only for the pre-existing TUI sidebar implicit-any diagnostics, worktree/main SDK private-type duplication, plugin overload/dependency resolution, and missing semver declarations. Renderer typecheck imports the changed materializer and passes.

### Compatibility Risks

- Stable root compatibility paths are symlinks on platforms that support atomic symlink replacement. If symlink creation is unavailable, publication rolls back and leaves the prior generation visible rather than exposing a partial new set.
- Derived generations are immutable once installed. Session finalization increments the manifest generation; read-only rendering of a still-running generation stays temporary and never publishes into the logical root.
- Root `legacy-trace.json` remains the newest valid terminal's legacy projection. The canonical and provenance files are the complete multi-segment history.
- A malformed unrelated session manifest encountered while scanning the configured trace root prevents session discovery and causes passive fallback. This favors uniqueness and evidence preservation over partial discovery.

## Fix Round 3/5 Completion

Status on 2026-08-16: **review round 3 complete**. The full case-trace suite has only the explicitly excluded, unchanged `artifact[0]` assertion failing.

### RED

The initial focused run established four segment/materializer failures (`26 pass, 4 fail`) and one renderer failure (`21 pass, 1 fail`):

- a well-typed but invalid `lock_key` was silently normalized and the root was mutated;
- legacy-flat and explicit-output materialization bypassed the global manifest lock;
- semantic aliases such as `evidence:`, `verification:`, `change:`, context, response, tool, skill, and MCP identities were not resolved to scoped canonical nodes;
- injected first-generation copy failure did not exist, so publication succeeded instead of proving cleanup;
- renderer temporary reconstruction hardlinked a segment artifact, changing its source `ctime`.

A second RED check made the manifest test physically valid and then proved that a reversed `created_at`/`updated_at` ordering was accepted. The table also covers every descriptor field, optional session IDs, status, traversal/absolute paths, exact records/artifacts/index contracts, generation, lock metadata, and continuation targets.

### GREEN Behavior

- `readTraceSessionManifest` is now the single parser used by allocation, binding/finalization, materialization, and renderer generation checks. It validates full field types and values, timestamp ordering, global lock metadata, nonnegative generation, ordered unique run/segment identities, session consistency, continuation targets, and canonical non-escaping descriptor paths. Resume additionally validates each immutable physical `segment.json` identity before creating a directory.
- Malformed roots are rejected before output or segment creation. Both allocation and explicit-output materialization tests compare recursive tree hashes and assert that no new segment, `.derived`, or output directory appears.
- Flat materialization, explicit-output/read-only reconstruction, segmented publication, and renderer reads all use `trace-root-manifest-v1`. Renderer holds the lock across session/trace generation comparison and the complete read or temporary replay, including legacy-flat input.
- Multi-segment replay pre-registers canonical node aliases and rejects ambiguous alias tails. Exact semantic ID fields and resolved safe-suffix `*_ref`/`*_refs` values scope through canonical entity maps. Recognized causal schemes resolve to scoped canonical node IDs; URLs, paths, customer fields, ordinary text, and unresolved values remain byte-for-byte unchanged. The graph validator walks typed and legacy scoped references across nodes, edges, diagnostics, terminal data, and metrics.
- Generation creation, population, compatibility-link installation, and current-symlink swap are one rollback boundary. Injected failures during first-generation copying and after the first root-link install recursively remove temporary/new generations, restore prior paths, and remove newly created empty `.derived` parents.
- Read-only materialization recursively copies compatibility artifact bytes. Renderer tests preserve source content, `ctime`, link count, and the full logical-root tree while reconstructing a stale generation containing an interrupted segment.

### Exact Layout

```text
$OPENCODE_CASE_TRACE_DIR/session-case/
  session.json
  trace.json                   -> .derived/current/trace.json
  manifest.json                -> .derived/current/manifest.json
  legacy-trace.json            -> .derived/current/legacy-trace.json
  provenance-trace.json        -> .derived/current/provenance-trace.json
  partial/latest.json          -> ../.derived/current/partial/latest.json
  .derived/
    current                    -> generations/<generation>
    generations/<generation>/
      trace.json
      manifest.json
      legacy-trace.json
      provenance-trace.json
      partial/latest.json
      artifacts/...
  segments/
    <segment-id>/
      segment.json
      records.jsonl
      index.sqlite
      artifacts/...
```

New segmented roots still do not expose mutable root `records.jsonl`, `events.jsonl`, or `raw-events.jsonl`. A legacy root journal remains immutable segment zero.

### Final Verification

```text
packages/opencode:
  trace-segment.test.ts                                  30 pass, 0 fail, 623 expects
  trace-materializer.test.ts + diagnostics               8 pass, 0 fail, 24 expects
  trace-materializer-memory.test.ts                       1 pass, 0 fail, 5 expects
    peak RSS                                             255,197,184 bytes
  worker-trace + materializer-process + thread           11 pass, 0 fail, 37 expects
  case-trace.test.ts (120 s test ceiling)                162 pass, 1 fail, 2134 expects

packages/trace-renderer:
  load.test.ts + cli.test.ts                             38 pass, 0 fail, 201 expects
  bun run typecheck                                      pass

repository:
  git diff --check                                       pass
```

The first 30-second full case-trace run reported `161 pass, 2 fail, 1 hook error`: the known artifact assertion plus a spurious `stores large semantic payloads` timeout with an impossible `917,529 ms` duration. That exact test immediately passed alone in 326 ms. A complete rerun with a 120-second per-test ceiling passed it in 260 ms and finished at `162 pass, 1 fail`; the sole failure is the excluded `trace.artifacts[0]` baseline, which remains unchanged.

`packages/opencode` typecheck still exits 2 only for the pre-existing sidebar implicit-any diagnostics, worktree/main SDK private-type duplication, plugin overload/dependency resolution, missing plugin dependencies, and semver declarations. No Task 6 or observability file appears in its diagnostics.

### Compatibility Risks

- Strict segmented-manifest validation rejects old or hand-authored segmented manifests that omit `lock_key`, `generation`, `artifacts`, or `index`, use noncanonical descriptor paths, or contain inconsistent session/continuation metadata. Legacy flat roots without `session.json` remain supported.
- Alias tails shared by multiple nodes are intentionally left unresolved rather than assigned nondeterministically. Explicit canonical IDs and fully qualified aliases remain deterministic.
- Standalone case directories directly under a protected parent use the case directory as the lock-root anchor because a sibling lock for the inferred parent cannot be created. Normal configured trace roots use the same parent-root global domain as allocation and publication.
- Atomic root generation switching still depends on symlink support. A link/swap failure removes the candidate generation and leaves the previous visible set intact.
