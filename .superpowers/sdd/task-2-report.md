# Task 2 Report: ActiveCaseTrace Terminal Trace Publication

## Status

Completed. No files were staged or committed.

## Changes

- `packages/opencode/src/observability/case-trace.ts`
  - Imports Task 1 `collectTracePublication()`, `reportTracePublication()`, and
    `TracePublicationStatus`.
  - Adds `locationReported` and `publishTerminalLocation(status)` to publish
    one stderr receipt per active trace, only after terminal persistence has
    completed. `OPENCODE_CASE_TRACE_QUIET === "1"` suppresses the receipt.
  - Maps `success`, `error`, and `cancelled` to `completed`, `failed`, and
    `cancelled`; Task 1 degrades incomplete terminal artifacts to `partial`.
  - Calls publication after normal `finish()`, `persistSignalSnapshot()`, and
    `persistSignalSnapshotBestEffort()` persistence paths. Existing
    before-exit, exit, SIGINT, SIGTERM, SIGHUP, uncaught-exception, and
    unhandled-rejection routes already converge on these paths. SIGKILL remains
    intentionally uncapturable.
- `packages/opencode/test/observability/case-trace.test.ts`
  - Defaults inherited fixtures to quiet mode, preserving existing empty-stderr
    assertions.
  - Adds RED/GREEN integration fixtures for explicit repeated `finish()` and
    a real `SIGTERM`; both explicitly enable publication and check the receipt,
    persisted `trace.json` and `trace.html` locations, unchanged stdout, and
    one receipt only.

## RED

The root command is deliberately blocked by `bunfig` in this checkout:

```bash
bun test packages/opencode/test/observability/case-trace.test.ts -t "trace publication"
```

Result: expected local runner block (`do-not-run-tests-from-root`). Equivalent
package command used:

```bash
cd packages/opencode
bun test test/observability/case-trace.test.ts -t "trace publication"
```

Result before implementation: exit `1`; `0 pass`, `2 fail`. Both failures were
the expected missing `Session trace saved` stderr receipt.

## GREEN

Focused publication regression:

```bash
cd packages/opencode
bun test test/observability/case-trace.test.ts -t "trace publication"
```

Result: exit `0`; `2 pass`, `0 fail`.

Terminal-path regression:

```bash
cd packages/opencode
bun test test/observability/case-trace.test.ts -t "trace publication|receives SIGINT|receives SIGTERM|signal listener"
```

Result: exit `0`; `6 pass`, `0 fail`.

Task 1 publication unit regression:

```bash
cd packages/opencode
bun test test/observability/trace-publication.test.ts
```

Result: exit `0`; `4 pass`, `0 fail`.

Full requested trace regression, run concurrently only to remain within the
execution channel time window:

```bash
cd packages/opencode
bun test --concurrent --max-concurrency=20 --reporter=dot \
  test/observability/trace-publication.test.ts \
  test/observability/case-trace.test.ts
```

Result: exit `0`; `150 pass`, `0 fail`, `2004 expect()` calls.

Static diff check:

```bash
git diff --check
```

Result: exit `0`, no output.

## Signal And One-Time Verification

- The real SIGTERM fixture exits with code `143` and emits exactly one receipt.
- The repeated explicit `CaseTrace.finish()` fixture exits `0` and emits
  exactly one receipt.
- Both fixtures confirm the receipt carries `session: ses_publication`,
  `trace.html`, and `trace.json`; `trace.json` exists by the time stderr is
  read. The explicit-finish fixture also retains its exact stdout
  (`agent output\n`), while the SIGTERM fixture keeps stdout empty.
- `locationReported` is set only after Task 1 returns a publication, so no
  receipt is consumed before a file-backed terminal or partial snapshot exists.
  Task 1 contains the stderr writer failure isolation, so reporting cannot
  change process exit behavior.

## Self-Review

- Compared both shared files to
  `/tmp/observable-opencode-task2-before.nJWk6H`; only the Task 2 additions
  listed above appear in the snapshot diff. Existing uncommitted work remains.
- Publication occurs after direct and emergency terminal persistence attempts;
  it does not write stdout.
- Existing unified process finalizers cover beforeExit/exit, all three
  catchable signals, uncaught exceptions, and unhandled rejections without
  changing their exit-control logic.
- No `git add` or `git commit` was run, per task instruction.

## Concerns

No functional concerns. The plain serial full-suite command exceeds the
execution channel's 30-second foreground window; the same two requested files
were therefore verified successfully with Bun's concurrent runner, while the
focused signal tests retained their serial execution and exact exit-code checks.
