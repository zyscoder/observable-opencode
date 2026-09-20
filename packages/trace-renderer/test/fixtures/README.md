# OpenCode Provenance Fixtures

`opencode-pre-migration-canonical-trace.json` preserves a canonical historical
manifest that still advertises the renderer-owned `trace.html` output.

`sphinx-projection-only/trace.json` preserves the historical projection-only
compatibility shape without depending on ignored local benchmark output.

These renderer-owned fixtures were derived from `provenance-trace.json` files
captured on 2026-08-11 from the OpenCode runtime scenarios in
`packages/opencode/test/observability/case-trace-runtime.test.ts`:

- `task6-large-runtime`: 5,001 `CaseTrace.node` observations carrying one
  4,125-byte authoritative artifact payload.
- `task6-idempotent-sigterm`: `flushForSignal("SIGTERM")` after a runtime
  observation.
- `task6-sigterm-emergency-recovery`: a SIGTERM finalization where the
  canonical trace write is forced to fail and the partial recovery persists.

The fixtures retain the persisted provenance projection shape, record IDs,
statuses, artifact references, and terminal data. Volatile run IDs,
timestamps, process IDs, and local paths were normalized. The large runtime
fixture retains its first and last runtime records; the renderer test expands
the intervening records deterministically to keep the checked-in fixture
compact.
