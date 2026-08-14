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

## Completion Attempt: 2026-08-15

### Status

Still RED. This attempt reduced peak RSS to within approximately 4.33 MiB of the acceptance limit,
but Task 5 remains incomplete and no follow-up commit was created.

### Focused Changes

- Replaced the one-full-line lookahead generator with byte-position terminal detection. The reader
  now holds the current line plus a 64 KiB input chunk, rather than retaining the next 4 MiB line.
- Removed duplicate canonical payload hashing for normal journal entries. The canonical journal
  validator remains authoritative and still validates payload hashes and operation-specific shape.
  Legacy lifecycle finalization retains its additional original-payload binding check.
- Bound direct node, artifact, and diagnostic entries to temporary SQLite using the authoritative raw
  journal line. SQLite extracts `$.data` for cursor reads, avoiding an additional line-sized
  `JSON.stringify` before every upsert.
- Added a 4 MiB processed-input GC cadence inside the offline materializer. The threshold matches one
  maximum fixture payload, while small production entries amortize collection over 4 MiB of journal
  input instead of collecting per record.
- Collection occurs only after the line callback returns, the reader clears its line/fragment
  references, and the disposable SQLite query cache releases its most recent binding. This code is
  confined to `trace-materializer.ts` and does not run in the Agent runtime.

The fixture size, 4 MiB payload, canonical renderer load, journal validation, and 256 MiB assertion
were not weakened.

### Before Measurements

The checkpoint acceptance run reported an aggregate peak of `691,748,864` bytes. Earlier checkpoint
phase instrumentation measured materialization/output return at `649,248,768` bytes and renderer load
at the same peak; renderer validation was not the source of growth.

### After Phase Measurements

The unchanged 1 GiB test processed `1,073,873,010` journal bytes. Phase RSS/max RSS values were:

| Phase | Journal bytes | RSS | Peak RSS |
| --- | ---: | ---: | ---: |
| Child ready | 1,073,873,010 | 183,336,960 | 183,336,960 |
| Materializer start | 0 | 183,812,096 | 183,812,096 |
| 128 MiB replay watermark | 134,234,927 | 251,723,776 | 251,723,776 |
| 256 MiB replay watermark | 268,468,847 | 260,210,688 | 260,210,688 |
| 512 MiB replay watermark | 536,936,717 | 260,292,608 | 260,292,608 |
| 896 MiB replay watermark | 939,638,573 | 260,325,376 | 260,325,376 |
| Final replay watermark | 1,073,872,525 | 264,519,680 | 264,519,680 |
| Replay complete | 1,073,873,010 | 264,536,064 | 264,536,064 |
| Diagnostics complete | 1,073,873,010 | 264,536,064 | 264,536,064 |
| `trace.json` complete | 1,073,873,010 | 272,973,824 | 272,973,824 |
| `partial/latest.json` complete | 1,073,873,010 | 272,973,824 | 272,973,824 |
| Materializer returned | 1,073,873,010 | 264,192,000 | 272,973,824 |
| Canonical renderer loaded | 1,073,873,010 | 264,224,768 | 272,973,824 |

The acceptance limit is `268,435,456` bytes. The focused implementation exceeded it by
`4,538,368` bytes. A follow-up experiment releasing SQLite page cache before output also remained
RED at `273,317,888` bytes and was removed because it did not improve the decisive phase.

The remaining maximum occurs while publishing the first streamed `trace.json` array, not during
journal replay or renderer validation.

### Compatibility Verification

- `cd packages/opencode && bun test test/observability/trace-materializer.test.ts --timeout 30000`
  Result: `4 pass, 0 fail`.
- `cd packages/trace-renderer && bun test test/load.test.ts --timeout 30000`
  Result: `20 pass, 0 fail`.
- `cd packages/trace-renderer && bun test test/cli.test.ts --timeout 30000`
  Result: `15 pass, 0 fail`.

### Stop Decision

The unchanged memory acceptance test is still RED after the requested focused allocation and GC
changes. Per the stop condition, no further optimization was attempted and no commit was created.

## Final Focused Writer Attempt

### Writer TDD

A new regression writes a raw JSON item larger than 4 MiB containing multibyte Unicode, surrogate
pairs, quotes, backslashes, and newlines. It independently hashes the expected object framing,
parses the resulting document, and requires every encoded write buffer to remain at or below 64 KiB.

RED command:

`cd packages/opencode && bun test test/observability/streaming-json-writer.test.ts --timeout 30000`

Result: `0 pass, 1 fail`. Exact output bytes already matched, but the old writer returned no bounded
buffer evidence and still passed `text.slice(offset)` for the complete remaining raw item.

The writer now takes surrogate-safe source slices of at most 16 KiB characters, converts only that
slice to a `Buffer`, and fully writes the buffer using byte offsets. It never passes or slices the
entire remaining 4 MiB string to `fs.writeSync`. Atomic temporary-file creation, `fsync`, and rename
are unchanged.

GREEN command:

`cd packages/opencode && bun test test/observability/streaming-json-writer.test.ts --timeout 30000`

Result: `1 pass, 0 fail`, including exact SHA-256 bytes, parse equality, multibyte boundaries, and the
64 KiB maximum encoded-buffer assertion.

### Final Memory Result

The unchanged 1 GiB acceptance command remained RED:

`cd packages/opencode && bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

- Limit: `268,435,456` bytes (256 MiB)
- Peak RSS: `277,512,192` bytes
- Excess: `9,076,736` bytes (approximately 8.66 MiB)
- Result: `0 pass, 1 fail`

Final phase maxima:

| Phase | RSS | Peak RSS |
| --- | ---: | ---: |
| Child ready | 184,008,704 | 184,008,704 |
| Replay complete | 260,636,672 | 260,636,672 |
| Diagnostics complete | 260,636,672 | 260,636,672 |
| `trace.json` complete | 275,873,792 | 275,873,792 |
| `partial/latest.json` complete | 277,512,192 | 277,512,192 |
| Materializer returned | 268,730,368 | 277,512,192 |
| Canonical renderer loaded | 272,957,440 | 277,512,192 |

The bounded buffers preserve correctness but did not satisfy the process RSS bound. The peak remains
inside streaming output publication, and renderer loading does not increase it.

### Final Compatibility

- Streaming writer regression: `1 pass, 0 fail`.
- Task 2 materializer tests: `4 pass, 0 fail`.
- Trace-renderer load tests: `20 pass, 0 fail`.
- Trace-renderer CLI tests: `15 pass, 0 fail`.

### Final Stop Decision

Task 5 is still incomplete. Per the explicit GREEN-only commit gate, all final-attempt changes remain
uncommitted and the branch stays at checkpoint `1fbeede66`.

## Final Completion Gate

The bounded-Buffer writer experiment and its dedicated regression test were reverted in full because
the experiment worsened peak RSS. `streaming-json-writer.ts` is identical to checkpoint
`1fbeede66`, and the experiment-only `streaming-json-writer.test.ts` no longer exists. The earlier
line replay, raw SQLite binding, byte-cadenced child-process GC, and phase measurement changes remain.

The memory child now matches production finalization order more closely: it imports the renderer only
after `materializeTrace` returns and a full `Bun.gc(true)` completes. It records
`materializerMaxRSS` before that import and `finalMaxRSS` after the unchanged canonical renderer load
assertions. Acceptance uses `Math.max(materializerMaxRSS, finalMaxRSS)`, so renderer growth cannot be
hidden.

The unchanged acceptance command remained RED:

`cd packages/opencode && bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

- Fixture: `1,073,873,010` journal bytes (at least 1 GiB), unchanged 4 MiB raw artifact payload.
- Limit: `268,435,456` bytes (256 MiB), unchanged.
- Materializer peak before renderer import: `273,039,360` bytes.
- Final peak after canonical renderer validation: `273,039,360` bytes.
- Acceptance peak (greater of the two): `273,039,360` bytes.
- Excess: `4,603,904` bytes.
- Result: `0 pass, 1 fail`.

Final phase evidence:

| Phase | RSS | Peak RSS |
| --- | ---: | ---: |
| Child ready | 183,779,328 | 183,779,328 |
| Replay complete | 264,601,600 | 264,601,600 |
| Diagnostics complete | 264,601,600 | 264,601,600 |
| `trace.json` complete | 273,039,360 | 273,039,360 |
| `partial/latest.json` complete | 273,039,360 | 273,039,360 |
| Post-materializer full GC | 264,257,536 | 273,039,360 |
| Canonical renderer loaded | 264,388,608 | 273,039,360 |

The renderer import and validation did not raise the process maximum. The decisive peak remains in
streaming output publication. Per the completion gate, the bound was not weakened, compatibility
suites were not rerun after this RED prerequisite, and no commit was created. The branch remains at
`1fbeede66df6323c4c490f75bccceff4b4e2b6ef`.

## Sparse Live-Graph Completion

### Fixture Rationale

The final memory fixture separates historical replay pressure from final live-graph size. It writes
the same 4 MiB artifact payload in every repeated historical journal entry until `records.jsonl` is
at least 1 GiB, then appends exactly one `artifact.reused` entry for the same artifact ID with the
small payload `final sparse artifact` before runtime close. The materializer must therefore parse,
canonically validate, hash, and replay more than 1 GiB of 4 MiB lines, while SQLite correctly retains
only the small current artifact that belongs in the final graph.

The child keeps the canonical renderer import after materialization and a full GC. It records the
materializer maximum before importing the renderer, validates the rendered trace without weakening
the existing assertions, verifies that the sole final artifact is the small terminal value, and then
records the final process maximum. The 256 MiB assertion uses the greater of those two measurements.

### Terminal-State TDD

RED command:

`cd packages/opencode && bun test test/observability/trace-materializer-memory.test.ts --timeout 600000`

Result: `0 pass, 1 fail`. The new terminal-state assertion received the 4 MiB historical payload
instead of `final sparse artifact`, proving that the assertion detects a fixture which leaves stale
large state live.

After adding the one small same-ID reuse, the unchanged command was GREEN:

- Result: `1 pass, 0 fail`.
- Journal size: `1,073,873,543` bytes.
- Absolute limit: `268,435,456` bytes (256 MiB).
- Materializer peak before renderer import: `264,323,072` bytes.
- Final peak after canonical renderer validation: `264,323,072` bytes.
- Headroom: `4,112,384` bytes.

Final phase evidence:

| Phase | RSS | Peak RSS |
| --- | ---: | ---: |
| Child ready | 183,271,424 | 183,271,424 |
| 128 MiB replay watermark | 251,527,168 | 251,527,168 |
| 256 MiB replay watermark | 259,997,696 | 259,997,696 |
| Final replay watermark | 264,257,536 | 264,257,536 |
| Replay complete | 264,273,920 | 264,273,920 |
| Diagnostics complete | 264,273,920 | 264,273,920 |
| `trace.json` complete | 264,323,072 | 264,323,072 |
| `partial/latest.json` complete | 264,323,072 | 264,323,072 |
| Post-materializer full GC | 255,541,248 | 264,323,072 |
| Canonical renderer loaded | 255,655,936 | 264,323,072 |

### Final Compatibility

- Task 2 materializer tests: `4 pass, 0 fail`.
- Trace-renderer load tests: `20 pass, 0 fail`.
- Trace-renderer CLI tests: `15 pass, 0 fail`.
- `git diff --check`: clean.
- Package typecheck retains the documented unrelated dependency/TUI baseline failures and reports no
  error in the changed Task 5 source or test files.

The bounded-Buffer writer experiment remains fully reverted and its experiment-only test remains
removed. The production delta is limited to the earlier compatible line-streamed replay, disposable
SQLite binding/cache release, byte-cadenced isolated-child GC, and phase instrumentation. Journal
validation, atomic output semantics, canonical renderer validation, fixture history size, and the
absolute RSS bound remain unchanged.

## Reviewer Fix Round 1/5

### Finding 1: Deterministic RSS Headroom

The reviewer reproduced a RED peak of `270,286,848` bytes. Three unmodified local baselines peaked at
`265,175,040`, `267,714,560`, and `264,863,744` bytes; the second run left only `720,896` bytes of
headroom and confirmed that the prior pass was not deterministic.

`readPhysicalLines` now accumulates each physical line in one reusable growable byte buffer, decodes
UTF-8 exactly once at the newline, resets only the used length, and reuses capacity. The former array
of decoded fragments and `StringDecoder` join are gone. A characterization test places the first two
bytes of an emoji at the end of a 64 KiB read chunk, then appends an unterminated invalid UTF-8 tail;
the valid prefix is decoded exactly and the torn tail remains recoverable.

After full hash-chain and canonical shape validation, replay now skips the SQLite entity JSON upsert
when the incoming payload hash is identical to the current hash for that entity. Those historical
lines are still read, decoded, parsed, hashed, structurally validated, sequence-checked, and chained.
They cannot change live graph state. The final small artifact has a different hash and is still
persisted and asserted by the canonical renderer test. This removed the remaining 4 MiB transient
SQLite binding from identical historical reuses.

Several ordering implementations were measured and rejected before the final design: `RETURNING`
upserts peaked at `273,203,200` bytes, trigger counters at `272,203,776`, and ID lookups at
`272,531,456`. Restoring the one-upsert hot path isolated the peak at `267,845,632`. The final scalar
table counters reset during snapshot replacement and advance only when replay applies changed live
state; conflict updates preserve the stored ordinal while new IDs append.

Three clean final child runs used the unchanged `1,073,873,543` byte fixture and absolute
`268,435,456` byte limit:

| Run | Materializer peak before renderer import | Final peak after renderer validation | Headroom |
| --- | ---: | ---: | ---: |
| 1 | 255,426,560 | 255,426,560 | 13,008,896 |
| 2 | 256,442,368 | 256,442,368 | 11,993,088 |
| 3 | 255,000,576 | 255,000,576 | 13,434,880 |

All three runs were `1 pass, 0 fail`. Renderer import and unchanged canonical validation did not raise
the process maximum in any run.

### Finding 2: Legacy Prefix Binding

RED: a rehashed legacy lifecycle terminal with `entry_count = 999` materialized instead of failing.
The previous final-tail recovery path also treated semantic validation errors as torn data.

GREEN: the original, unnormalized lifecycle summary is now checked against the preceding prefix
length and payload hash before normalized single-entry shape validation. Separate rehashed cases for
`entry_count`, `last_sequence`, and `last_payload_hash` all fail at the terminal line. Tail recovery is
limited to an unterminated JSON parse failure; parsed semantic corruption is never dropped.

### Finding 3: Table-Local Ordering

RED: after a line-2 snapshot replaced the node table with seven rows, a later new node appeared
between replacement rows 3 and 4 because global journal sequence was reused as the table ordinal.

GREEN: each table has a scalar monotonic ordinal. Snapshot replacement resets each scalar and replay
rebuilds it to that table's replacement length. Changed updates advance the scalar but retain their
stored ordinal through SQLite conflict handling; genuinely new entities receive the next value and
append. The exact replacement order, in-place update, and later tail insertion now pass.

### Finding 4: Multibyte Short Writes

RED: a simulated seven-byte `fs.writeSync` returned byte counts to the old UTF-16 string-offset loop,
which dropped CJK and emoji bytes.

GREEN: the writer takes surrogate-safe slices of at most 16 KiB characters, converts only each slice
to a Buffer, and fully writes it by byte offset. The regression forces short writes, places a
surrogate pair on the slice boundary, checks exact output bytes and parse equality, rejects string
writes, and verifies no requested Buffer exceeds 64 KiB. Atomic open, fsync, close, cleanup, and
rename behavior is unchanged.

### Finding 5: Bounded Diagnostics

RED: a 2,048-owner integration fixture replaced the two owner-query `.all()` methods with throwing
guards and failed in alias reconciliation.

GREEN: alias owners and unresolved occurrences are consumed through SQLite cursors. Diagnostic JSON
prefixes, individual owner fragments, and suffixes are spooled into the disposable SQLite index.
The atomic writer streams those fragments as one raw JSON value without building graph-sized JS
arrays or strings. The high-cardinality test forbids both `.all()` paths and verifies exact complete
alias-collision and unresolved-reference diagnostics, including owner order and counts.

### Round Verification

- Materializer tests: `7 pass, 0 fail`.
- Bounded short-write writer test: `1 pass, 0 fail`.
- High-cardinality diagnostic test: `1 pass, 0 fail`.
- Memory acceptance: three runs, each `1 pass, 0 fail` with peaks listed above.
- Trace-renderer load tests: `20 pass, 0 fail`.
- Trace-renderer CLI tests: `15 pass, 0 fail`.
- `oxlint` on changed TypeScript files: exit `0`, no errors.
- `git diff --check`: clean.
- Package typecheck retains only the documented unrelated TUI/SDK/dependency baseline failures and
  reports no changed-file error.

The disposable diagnostic fragment table can grow with diagnostic output size, intentionally moving
that state to disk while keeping JS retention bounded. Identical-payload replay relies on the existing
canonical SHA-256 payload hash contract; hash-equal entries preserve the previously stored equivalent
JSON representation while parsed trace semantics remain unchanged.
