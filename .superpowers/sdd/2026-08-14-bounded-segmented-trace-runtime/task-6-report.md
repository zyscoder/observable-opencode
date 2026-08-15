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
