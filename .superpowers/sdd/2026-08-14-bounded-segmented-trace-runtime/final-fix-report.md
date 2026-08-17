# Bounded Segmented Trace Runtime Final Fix Report

## Checkpoint 1: Journal-only terminal close and disk materialization

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Findings covered:

- Critical: all production close paths use journal-only closure; deterministic terminal enrichment and legacy compatibility projection occur during disk-backed materialization.
- Critical: the latest interrupted segment controls top-level partial/error status.
- Important: TUI/process signals propagate cancellation metadata.
- Important: terminal journal commit and descriptor durability precede segment completion; materialization reconciles committed journal state after manifest-write failure.
- Important: journal-only traces receive the required legacy compatibility projection.
- Passive-observer invariant: close/materialization failures remain diagnostic-only and do not change command exit behavior.

RED evidence:

- `bun test test/cli/tui/worker-trace.test.ts --timeout 30000`: 3 pass, 1 fail; signal closure incorrectly produced `success`.
- `bun test test/observability/trace-materializer.test.ts --timeout 30000`: 6 pass, 1 fail; journal-only materialization omitted `legacy-trace.json`.
- `bun test test/observability/trace-segment.test.ts --timeout 30000`: 31 pass, 2 fail; an uncommitted close was advertised complete and a latest interrupted segment inherited an older success.
- `bun test test/cli/run/runtime.trace.test.ts --timeout 30000`: 7 pass, 5 fail; production run paths still invoked full in-memory finish.
- `bun test test/cli/tui/thread.test.ts --timeout 30000`: 6 pass, 1 fail; the terminal signal was dropped at the worker boundary.
- `bun test test/observability/trace-terminal-equivalence.test.ts --timeout 30000`: 0 pass, 1 fail; journal materialization lacked constraints, span fidelity, terminal claims/lifecycle, and manifest semantics.
- Injected record-fsync failure left the segment manifest `running`, demonstrating the durability/reconciliation gap before the fix.

GREEN evidence:

- `bun test test/cli/tui/worker-trace.test.ts --timeout 30000`: 4 pass, 0 fail.
- `bun test test/cli/tui/thread.test.ts --timeout 30000`: 7 pass, 0 fail.
- `bun test test/cli/run/runtime.trace.test.ts --timeout 30000`: 12 pass, 0 fail.
- `bun test test/observability/trace-materializer.test.ts --timeout 30000`: 7 pass, 0 fail.
- `bun test test/observability/trace-terminal-equivalence.test.ts --timeout 30000`: 1 pass, 0 fail.
- `bun test test/observability/case-trace-runtime.test.ts --timeout 30000`: 28 pass, 0 fail, 7,336 expectations.
- `bun test test/observability/trace-segment.test.ts --timeout 30000`: 34 pass, 0 fail, 705 expectations.
- `bun test test/cli/tui/process-trace-e2e.test.ts --timeout 30000`: 1 pass, 0 fail, 31 expectations across normal exit, SIGINT, SIGTERM, and SIGHUP. The sandbox rejected loopback port binding with `EADDRINUSE`; the same command passed outside that network sandbox.
- `bun test test/observability/case-trace-terminal-memory.test.ts --timeout 30000`: 1 pass, 0 fail. RSS before close 617,414,656 bytes; after close 608,616,448 bytes; growth -8,798,208 bytes (-8.39 MiB).
- `bun run typecheck` in `packages/opencode`: exit 2 with the exact pre-change 22-diagnostic baseline and no new diagnostics.
- `git diff --check`: pass.

Semantic and durability evidence:

- The equivalence test compares deterministic full finish with journal-only close plus materialization, including manifest terminal fields, lifecycle/response/claim/support/case nodes, constraints, and legacy spans.
- Segment tests cover pre-commit failure, descriptor-fsync failure, manifest reconciliation, and latest-segment interruption precedence.
- The terminal-memory test closes a production-shaped trace without replaying its journal into runtime memory.

## Checkpoint 2: Bounded runtime compatibility state and registry ownership

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Findings covered:

- Critical: completed spans, runtime events/errors, context snapshots, decisions, verifications, changes, constraints, response segments, design records, and finalized registry ownership are bounded or retired.
- Critical: generated compatibility IDs use independent monotonic counters and remain stable after hot-window eviction.
- Critical: verification supersession and change invalidation query evicted facts through the segment SQLite index.
- Important compatibility constraint: disk materialization reconstructs all legacy compatibility families, including raw runtime events and span errors, after the 256-record hot boundary.
- Passive-observer invariant: an unavailable trace root disables persistence without changing the 10,000-record workload's Agent-visible operation.

RED evidence:

- `bun test test/observability/case-trace-mixed-memory.test.ts --timeout 120000`: the initial 10,000-record probe grew from 494,665,728 to 989,085,696 bytes, a 494,419,968-byte increase, exceeding the 128 MiB cap.
- The strengthened mixed-family probe initially grew 340,262,912 bytes after a 1,000-record warm-up; moving the measurement boundary to 3,000 records, after every bounded cache was populated, isolates post-warm-up ownership growth.
- `bun test test/observability/case-trace-session.test.ts --timeout 30000`: finalized registry ownership retained all 10,000 roots instead of returning an empty active set.
- `bun test test/observability/case-trace-runtime.test.ts --timeout 30000 -t "queries evicted verification facts"`: the first verification remained effective and had no `superseded_by` reference after eviction.
- The original combined 257-per-family disk fixture timed out after 240,003 ms because changes repeatedly invalidated the colocated verification population. This was a fixture interaction, not the 10,000-record memory path. Isolating each family retained the 257th-record eviction proof.
- The isolated legacy-projection RED completed in 0.90 s and found `legacy.errors.length === 0`; after exposing that gap, the event scenario found 0 of 257 generic runtime events. The journal-only materializer had never replayed `raw-events.jsonl`.

GREEN evidence:

- `bun test test/observability/case-trace-mixed-memory.test.ts --timeout 240000`: 2 pass, 0 fail, 40 expectations in 86.63 s. Fresh RSS was 893,370,368 bytes after warm-up and 1,015,365,632 bytes after 10,000 records: 121,995,264-byte growth (116.34 MiB), below 128 MiB. All seven generated compatibility families reached ordinal 1000; storage-degraded closure returned zero materialization requests.
- An earlier strengthened GREEN run measured 906,002,432 to 952,975,360 bytes: 46,972,928-byte growth (44.80 MiB). The higher fresh run is retained as the conservative acceptance result.
- The isolated disk-backed compatibility replay passed all nine 257-record scenarios with 29 expectations in 5.59 s; spans/errors, runtime events, snapshots, decisions, changes, verifications, constraints, responses, and designs survive hot eviction.
- `bun test test/observability/causal-ir-runtime-store.test.ts test/observability/case-trace-session.test.ts test/observability/case-trace-runtime.test.ts test/observability/trace-materializer.test.ts --timeout 120000`: 71 pass, 0 fail, 7,500 expectations in 20.15 s. Registry ownership is 27/27 green; the cold supersession/invalidation case passes in 1.08 s.
- `bun run typecheck` in `packages/opencode`: exit 2 with the exact pre-change 22-diagnostic baseline and no new diagnostics. A newly exposed local narrowing diagnostic was fixed before this checkpoint.

Implementation evidence:

- Runtime compatibility arrays retain at most 256 recent values while aggregate counters preserve complete metrics and response/change state.
- Completed span and span-node ownership is evicted immediately; finalized registry roots, aliases, and owners are retired while bounded tombstones prevent accidental rerouting and root ordinals remain monotonic.
- SQLite node queries support bounded sequence pagination and indexed JSON equality filters used for cold verification/change semantics.
- The materializer streams raw runtime events and span errors through its temporary SQLite index instead of retaining the raw event log in JavaScript memory.
