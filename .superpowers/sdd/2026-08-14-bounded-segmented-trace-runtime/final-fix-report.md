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

## Checkpoint 3: First-entry allocation crash recovery

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Finding covered:

- Critical: a newly published segment can no longer point at a journal that has never existed, and a zero-line or pre-fix missing segmented journal materializes as an interrupted prefix instead of poisoning the logical session.

RED evidence:

- `bun test test/observability/trace-segment.test.ts --timeout 30000 -t "durably initializes and recovers"`: 0 pass, 1 fail. Immediately after the allocation boundary, reading `segments/run_first_entry_crash/records.jsonl` failed with `ENOENT`; `session.json` had already published that path.

GREEN evidence:

- The same targeted command: 1 pass, 0 fail, 7 expectations in 17.79 ms.
- `bun test test/observability/trace-segment.test.ts test/observability/trace-materializer.test.ts --timeout 120000`: 42 pass, 0 fail, 737 expectations in 5.83 s.
- `bun run typecheck` in `packages/opencode`: exit 2 with the exact pre-change 22-diagnostic baseline and no new diagnostics.

Durability and recovery evidence:

- Segment allocation creates and fsyncs an empty `records.jsonl`, then fsyncs its directory before atomically publishing `segment.json` and `session.json`.
- The allocation-only test fixture stops at the exact pre-first-entry crash boundary. Materialization returns `completeness: "incomplete"`, `recoveredLines: 0`, top-level error/interrupted status, descriptor-derived identities and start time, empty legacy projections, and an explicit interrupted recovery record.
- Materialization leaves both the zero-byte journal and the published session manifest byte-identical. Segmented journals missing from older crash windows receive the same zero-line interrupted recovery; flat missing/empty journals remain invalid.

## Checkpoint 4: Disk-backed multi-segment namespace lookup

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Finding covered:

- Critical: multi-segment entity-ID and node-alias predeclaration no longer retains graph-cardinality JavaScript maps. Namespaced existence, alias ownership, and ambiguity are indexed in the temporary materializer SQLite database with 256-entry entity and alias lookup caches.

RED evidence:

- `bun test test/observability/trace-materializer-multisegment-memory.test.ts --timeout 600000`: the final 1 GiB fixture contains 1,088,692,372 journal bytes across two segments and repeated local identities. The original map-backed materializer peaked at 578,387,968 bytes versus the 268,435,456-byte limit.
- The first disk-table implementation reduced peak RSS to 315,179,008 bytes. Bounded transaction batches removed the commit spike but remained RED at 290,095,104 bytes, showing that JavaScript pre-scan parsing still retained too much allocator memory.
- After the namespace schema landed, the original sparse acceptance exposed a narrow regression: 268,632,064 bytes, 196,608 bytes over the hard limit.

GREEN evidence:

- `bun test test/observability/trace-materializer-multisegment-memory.test.ts --timeout 600000`: 1 pass, 0 fail, 7 expectations in 24.63 s. A standalone production Bun child materialized 1,088,692,372 bytes and 62,008 valid entries at a 169,017,344-byte peak, with both colliding alias references resolving to their segment-scoped target IDs.
- `bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`: 1 pass, 0 fail, 5 expectations in 3.99 s. The pre-existing sparse 1,073,873,543-byte journal plus renderer load peaked at 252,297,216 bytes after reducing the temporary SQLite page cache from 8 MiB to 4 MiB.
- `bun test test/observability/trace-materializer.test.ts test/observability/trace-materializer-diagnostics.test.ts test/observability/trace-segment.test.ts --timeout 120000`: 43 pass, 0 fail, 740 expectations in 8.79 s.
- `bun run typecheck` in `packages/opencode`: exit 2 with the exact pre-change 22-diagnostic baseline and no new diagnostics.
- `git diff --check`: pass.

Memory and semantic evidence:

- Predeclaration uses SQLite JSON queries over each bounded physical line, avoiding a graph-retaining JavaScript parse pass. Entity existence and typed alias owners are stored in `WITHOUT ROWID` tables; aliases preserve unique-owner and ambiguity behavior.
- The high-cardinality fixture repeats all local node IDs across two immutable segments, includes three typed aliases per node, compacts each segment to a small terminal snapshot, and verifies exact namespace resolution after replay. This exercises high cardinality without weakening the 1 GiB journal or loading a synthetic 1 GiB derived JSON document into the renderer.
- The measured child is a plain production Bun process; the parent `bun test` process builds the fixture, verifies bytes/RSS/exit status, and validates the child-reported semantic assertions without adding test-runner memory to the materializer envelope.

## Checkpoint 5: Short renderer locks, contained paths, generation retention, and timeout documentation

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Findings covered:

- Important: renderer reads and read-only materialization capture a manifest generation under the allocation lock, perform parse/replay/materialization outside it, and recheck before return; a changed generation retries from immutable evidence.
- Important: protected session, descriptor, journal, raw-event, artifact, compatibility, renderer-source, and publication paths reject symlink traversal. Existing paths are realpath-contained and protected files use no-follow opens where available.
- Minor: successful publication retains only the current and immediately previous derived generation. An already-open reader keeps valid immutable bytes when the oldest directory is reclaimed.
- Minor: README timeout guidance now distinguishes fixed 5,000 ms worker cleanup, configurable journal close, and the independently awaited materializer process.

RED evidence:

- `bun test test/load.test.ts` in `packages/trace-renderer`: the new allocation-concurrency case failed after the parent hit the configured 200 ms trace-session-lock timeout while a child renderer was paused in `trace.json` reading.
- `bun test test/observability/trace-segment.test.ts --test-name-pattern "symlinked protected|symlinked derived|retains only"`: 0 pass, 3 fail. A symlinked `session.json` and `.derived` root were followed, and generations `2`, `4`, and `6` all remained instead of only rollback/current `4` and `6`.
- `bun test test/load.test.ts --test-name-pattern "source symlinks"`: 0 pass, 1 fail. Renderer loading accepted an out-of-root `trace.json` symlink and projected its bytes.
- `bun test test/observability/trace-segment.test.ts --test-name-pattern "symlinked publication"`: 0 pass, 1 fail. A symlinked `partial/` directory redirected compatibility publication outside the case root.

GREEN evidence:

- `bun test test/observability/trace-segment.test.ts test/observability/trace-materializer.test.ts test/observability/trace-materializer-diagnostics.test.ts --timeout 30000`: 46 pass, 0 fail, 792 expectations in 8.88 s.
- `bun test test/load.test.ts --timeout 30000` in `packages/trace-renderer`: 25 pass, 0 fail, 93 expectations in 198 ms. The paused renderer allowed allocation, observed generation `3`, retried, and returned the latest interrupted run in 69.50 ms.
- `bun test test/cli/tui/thread.test.ts test/cli/tui/trace-materializer-process.test.ts --timeout 30000`: 8 pass, 0 fail, 17 expectations in 6.56 s.
- `bun run typecheck` in `packages/trace-renderer`: exit 0.
- `bun run typecheck` in `packages/opencode`: exit 2 with exactly the established 22 unrelated diagnostics and no diagnostics in touched observability or test files.
- `git diff --check`: pass.

Concurrency, path, and retention evidence:

- The renderer concurrency child pauses after opening the exact immutable generation file. A competing `openTraceSegment()` succeeds under a 200 ms deadline; the renderer's generation recheck discards the stale result and retries against generation `3`.
- The malicious-tree matrix covers symlinked `session.json`, segment directory, `segment.json`, `records.jsonl`, `artifacts`, `index.sqlite`, `legacy-trace.json`, `.derived`, `partial/`, and renderer `trace.json`/`records.jsonl`. Outside-file/tree hashes remain unchanged.
- Manifest-relative paths remain lexical after realpath validation, preserving stable `/var`-relative recovery metadata on macOS while containment checks use canonical `/private/var` paths.
- The retention test publishes three generations, observes only the latest two directories, confirms the oldest directory is removed, and reads the oldest generation successfully through a file descriptor opened before reclamation.

## Checkpoint 6: Materialized terminal and legacy compatibility integration

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Integration gaps covered:

- Journal-only signal traces now prove that journal entities are the exact durable source subset, open-node closure is limited to audited deterministic finalization fields, and additional terminal nodes/edges carry reproducible materializer derivation metadata.
- A process signal after an explicit successful final answer preserves `case_status: success` and `shutdown_disposition: graceful_after_case_completion` instead of being overwritten as interrupted.
- Completed runtime spans remain evicted from JavaScript memory, but `span.start`/`span.end` evidence is upserted into temporary SQLite and streamed into `legacy-trace.json`, preserving token usage, redaction, and concrete tool-span references.
- Modern segmented traces regenerate legacy compatibility from durable journal/raw-event evidence. Existing flat legacy roots remain copy-compatible for migration.

RED evidence:

- `bun test test/observability/case-trace.test.ts --test-name-pattern "keeps process-signal shutdown separate|preserves explicit token metrics|uses concrete source refs|finalizes canonical partial and trace when" --timeout 120000`: 0 pass, 5 fail in 2.04 s. Two signal cases exposed deterministic materializer nodes absent from raw replay, the successful signal manifest was overwritten to `interrupted_before_case_completion`, and both legacy span consumers found an empty `spans` array.
- Root-cause inspection showed complete sanitized `span.start` and `span.end` envelopes in `raw-events.jsonl`, while `ingestLegacyRuntimeFile()` imported only generic events and errors. Modern direct-finish segments also contained a bounded in-memory legacy file with completed spans evicted, which the root materializer copied instead of rebuilding.
- After adding the journal-only oracle, its first RED showed that open observed nodes are intentionally finalized during materialization. The strengthened assertion now compares all stable fields exactly and permits only terminal status, the four explicit finalization fields, compatibility refs, and the recomputed payload hash to differ.

GREEN evidence:

- `bun test test/observability/case-trace.test.ts --test-name-pattern "keeps process-signal shutdown separate|preserves explicit token metrics|uses concrete source refs|finalizes canonical partial and trace when" --timeout 120000`: 5 pass, 0 fail, 152 expectations in 2.03 s.
- `bun test test/observability/trace-materializer.test.ts test/observability/trace-terminal-equivalence.test.ts --timeout 120000`: 8 pass, 0 fail, 28 expectations in 1.26 s.
- `git diff --check`: pass before checkpointing.

Durability and compatibility evidence:

- Runtime span reconstruction is disk-backed and ordered by first lifecycle occurrence; `span.end` atomically replaces the start payload without changing its ordinal. Formal-node projection remains a fallback only when no raw lifecycle exists, preventing duplicates.
- Namespaced multi-segment raw span/event/error references use the same disk-backed schema-aware resolver as canonical entities.
- Legacy reconstruction retains already-sanitized token metadata and credentials remain redacted in both raw evidence and the generated compatibility document.

## Checkpoint 7: Complete disk-materialized legacy parity

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Integration gaps covered:

- Regenerated modern legacy output links immutable segment artifacts into the compatibility artifact tree, including the 5,000-record authoritative-payload workload.
- Legacy semantic edges are projected only from the canonical opt-in marker and preserve original relation, optional-field presence, evidence refs, confidence, label, and metadata exactly.
- `context_snapshots` excludes internal `context.pack` set nodes; verification records restore their canonical top-level `passed`/`failed` status.
- Constraint compatibility records are upserted from durable `semantic.constraint` and `semantic.constraint_evaluated` raw evidence. Journal-only unknown constraints receive the same deterministic read-only/test/minimal-change evaluation from disk-backed change and verification facts.

RED evidence:

- Full CaseTrace family command: `bun test test/observability/case-trace.test.ts test/observability/case-trace-runtime.test.ts test/observability/case-trace-session.test.ts test/observability/case-trace-memory.test.ts test/observability/case-trace-terminal-memory.test.ts --timeout 120000`: 215 pass, 6 fail, 9,607 expectations in 99.88 s. Failures were the missing compatibility artifact directory, normalized legacy edge shape, duplicate context snapshot, two missing verification statuses, and stale unknown constraint status.
- Strengthening `trace-terminal-equivalence.test.ts` from an explicitly satisfied constraint to an unknown read-only constraint initially passed symmetrically with both projections wrong. Adding the required `observed_satisfied` oracle produced the intended RED: 0 pass, 1 fail in 1.17 s.

GREEN evidence:

- `bun test test/observability/case-trace-runtime.test.ts test/observability/case-trace.test.ts test/observability/trace-terminal-equivalence.test.ts --test-name-pattern "uses the bounded writer|projects legacy semantic edges|persists semantic trace records|marks verification as failed|does not infer verification failure|evaluates read-only constraints|journal-only terminal materialization" --timeout 120000`: 7 pass, 0 fail, 68 expectations in 13.69 s.
- The targeted 5,000-record child reported RSS 282,247,168 before direct compatibility finalization and 653,328,384 after, with 5,891.27 ms finalization. This legacy `finish()` compatibility path is intentionally retained for callers; production TUI/run/serve use the separately measured journal-only close path.
- `git diff --check`: pass before checkpointing.

Disk and semantic evidence:

- Constraint updates and completed spans use keyed SQLite upserts that preserve first-observation order while replacing only the latest durable state.
- Canonical node/edge and raw-event scans remain iterator-based. No unbounded runtime collection or full legacy document is reintroduced.
- Artifact publication merges every immutable segment's content-addressed tree and retains no symlink-following path shortcut.

## Checkpoint 8: Align legacy compatibility tests with immutable journal authority

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

Root-cause evidence:

- `bun test test/observability/causal-ir.test.ts test/observability/causal-ir-runtime-store.test.ts test/observability/claim-atomization.test.ts test/observability/streaming-json-writer.test.ts test/observability/trace-publication.test.ts test/observability/trace-materializer.test.ts test/observability/trace-materializer-diagnostics.test.ts test/observability/trace-segment.test.ts test/observability/trace-terminal-equivalence.test.ts test/tool/semantic-observability.test.ts --timeout 1200000`: 159 pass, 4 fail, 1,232 expectations in 11.78 s.
- Three failures were stale pre-materializer contracts: they expected the latest physical legacy file to replace the unified disk-backed projection, expected a fixture-only top-level `marker` absent from the legacy 1.3 schema, and expected the replaceable logical-root projection to remain byte-identical to a segment-local projection. The fourth failure was the sandbox's synthetic loopback `EADDRINUSE` at `server.listen(0, "127.0.0.1")`.
- Durable `records.jsonl` and immutable segment artifacts already contained both colliding artifacts and the latest terminal status. No production path or schema was changed to satisfy obsolete copy-through assumptions.

GREEN evidence:

- `bun test test/observability/trace-segment.test.ts --test-name-pattern "keeps compatibility artifacts distinct|aggregates rich terminal manifests|direct finish materializes" --timeout 120000`: 3 pass, 0 fail, 41 expectations in 1.15 s.
- `bun test test/observability/trace-segment.test.ts test/observability/trace-materializer.test.ts test/observability/trace-materializer-diagnostics.test.ts test/observability/trace-terminal-equivalence.test.ts --timeout 120000`: 47 pass, 0 fail, 799 expectations in 9.53 s.
- Full CaseTrace family command: `bun test test/observability/case-trace.test.ts test/observability/case-trace-runtime.test.ts test/observability/case-trace-session.test.ts test/observability/case-trace-memory.test.ts test/observability/case-trace-terminal-memory.test.ts --timeout 120000`: 221 pass, 0 fail, 9,652 expectations in 100.96 s.

Compatibility and immutability evidence:

- Reused artifact-relative paths remain separately readable at their scoped immutable segment paths; the root compatibility alias retains its original bytes.
- The unified legacy projection now has explicit assertions for latest terminal status, result, and error instead of relying on a non-schema fixture marker.
- Repeated materialization asserts that the segment-local legacy projection remains byte-identical, while the logical-root projection remains replaceable and journal-derived.

## Checkpoint 9: Restore production-shaped interactive process ownership

Status: complete and ready for its coherent checkpoint commit. The exact SHA is recorded in the final commit mapping after Git creates this commit.

RED and root-cause evidence:

- `bun test test/cli/tui/worker-trace.test.ts test/cli/tui/thread.test.ts test/cli/tui/trace-materializer-process.test.ts test/cli/run/runtime.trace.test.ts test/cli/serve-command.test.ts test/cli/trace-finalize.test.ts --timeout 120000`: 26 pass, 1 fail, 89 expectations in 11.64 s. The synthetic real-trace replacement fixture produced two roots instead of its expected three.
- Production `run` creates a process-scoped outer `run.execute` span before binding a session and closes it through `beforeTraceFinalize`. The synthetic fixture omitted both operations, so its compatibility root was correctly claimed by `ses_a`; this was a fixture defect rather than product trace loss.

GREEN evidence:

- `bun test test/cli/run/runtime.trace.test.ts --test-name-pattern "writes independent real traces" --timeout 120000`: 1 pass, 0 fail, 6 expectations in 1.52 s.
- `bun test test/cli/tui/worker-trace.test.ts test/cli/tui/thread.test.ts test/cli/tui/trace-materializer-process.test.ts test/cli/run/runtime.trace.test.ts test/cli/serve-command.test.ts test/cli/trace-finalize.test.ts --timeout 120000`: 27 pass, 0 fail, 93 expectations in 11.57 s.

Production-path evidence:

- Worker signal mapping, trace-only shutdown failures, normal/Ctrl-C worker termination ordering, the separate five-second cleanup and trace-close deadlines, serial external materialization, run replacement and startup failures, serve startup, and standalone finalize all pass.
- The fixture now owns and closes the process trace exactly where production does; it does not replace or stand in for the separately executed production E2E command.
