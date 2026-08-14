# Task 5 Report: Streaming SQLite Materializer

## Status

Reproducible checkpoint, not complete. The Task 2 compatibility tests and trace-renderer CLI
tests pass, and canonical renderer loading succeeds for the 1 GiB fixture. The required
256 MiB peak RSS acceptance bound still fails, so Task 5 must not be treated as GREEN.

## Changed Files

- `packages/opencode/src/observability/trace-materializer.ts`
- `packages/opencode/src/observability/streaming-json-writer.ts`
- `packages/opencode/test/observability/trace-materializer-memory.test.ts`
- `.superpowers/sdd/2026-08-14-bounded-segmented-trace-runtime/task-5-report.md`

## RED Evidence

Command:

`cd packages/opencode && bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

The test streams a valid journal to disk until it is at least 1 GiB. It repeatedly reuses one
4 MiB artifact, so the fixture generator retains only one bounded payload and the replayed live
graph contains one artifact. Materialization runs in a child process and then loads the output
through `loadRenderableTrace`, the canonical renderer compatibility path.

Before the implementation change, the Task 2 materializer completed but failed the RSS assertion:

- Limit: `268,435,456` bytes (256 MiB)
- Peak RSS: `5,061,623,808` bytes (approximately 4.71 GiB)
- Result: `0 pass, 1 fail`

This was a measured memory failure, not a timeout or child crash.

## Implemented Checkpoint

- Reads `records.jsonl` in 64 KiB chunks with one-line lookahead for torn-tail recovery.
- Validates sequence, identity, operation, payload hashes, per-entity hash chains, terminal close,
  and canonical entry shape one line at a time.
- Replays current nodes, edges, artifacts, diagnostics, payload-hash state, and lifecycle snapshots
  into a new disposable temporary SQLite database.
- Does not read or trust the runtime `index.sqlite`; `records.jsonl` remains authoritative.
- Preserves complete/incomplete manifest status, cancelled/error close data, torn-tail dropped-line
  counts, and the public `materializeTrace` result shape.
- Streams canonical arrays and per-item provenance compatibility projections from SQLite cursors.
- Writes each JSON output through a temporary file, calls `fsync`, and atomically renames it.
- Does not construct or call `JSON.stringify` on a full trace document.

## Verification

### Compatibility GREEN

`cd packages/opencode && bun test test/observability/trace-materializer.test.ts --timeout 30000`

Result: `4 pass, 0 fail`.

`cd packages/trace-renderer && bun test test/cli.test.ts --timeout 30000`

Result: `15 pass, 0 fail`.

These cover complete close, interrupted close, torn tail, non-tail corruption, materialize/render
integration, preservation of incomplete recovery status, output collision handling, and CLI input
validation.

### Memory Still RED

Fresh checkpoint command:

`cd packages/opencode && bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

Result:

- Canonical `loadRenderableTrace` validation completed and emitted its measurement marker.
- Limit: `268,435,456` bytes (256 MiB)
- Peak RSS: `691,748,864` bytes (approximately 659.7 MiB)
- Result: `0 pass, 1 fail`

The implementation removes journal-wide retention but does not yet satisfy the acceptance bound.
Temporary diagnosis showed the SQLite replay database stayed near 4 MiB; remaining peak memory is
associated with transient JSON parse, canonical payload hashing, validation, and SQLite JSON binding
allocations under Bun.

### Typecheck And Diff

`cd packages/opencode && bun run typecheck`

Result: failed only on the known package baseline: TUI implicit-any/plugin typing, duplicate SDK
worktree type identities, missing plugin dependencies, and missing `semver` declarations. No changed
Task 5 file appears in the final error output.

`git diff --check`

Result: passed.

## Compatibility Risks And Remaining Work

- The 1 GiB memory acceptance criterion is not met; this is the blocking concern.
- Canonical payload validation currently creates line-sized temporary strings more than once in
  some validation paths. That allocation path needs bounded redesign or verified collection cadence.
- SQLite primary keys enforce one current entity per ID. This matches the Task 5 upsert requirement,
  but malformed legacy lifecycle snapshots containing duplicate IDs could differ from the old array
  replay's duplicate behavior.
- Generated diagnostic reconciliation is cursor-based, but one alias-collision or unresolved-reference
  diagnostic can still require an array proportional to that single diagnostic's owner list.
- Atomicity is per output file, matching the prior sequential publication behavior; the three derived
  outputs are not committed as one cross-file transaction.

## Checkpoint Conclusion

This commit is a bounded-replay implementation checkpoint with passing compatibility coverage and a
large measured RSS reduction. It is intentionally reported as incomplete because peak RSS remains
approximately 403.3 MiB above the required limit.
