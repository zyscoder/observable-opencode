# Offline Trace Renderer Design

## Context

Observable OpenCode currently uses `trace.html` as both a live snapshot and a final human-readable report. Each runtime partial write rebuilds the Causal IR projection and the complete HTML document. This adds CPU, memory, and disk amplification to long-running sessions even though attribution consumes semantic Trace data rather than HTML.

## Goals

- Remove all HTML rendering from the OpenCode runtime.
- Keep semantic collection passive and preserve Agent behavior.
- Keep `trace.json` as the canonical finalized Causal IR document.
- Keep `records.jsonl` as the append-only Causal IR journal and recovery source.
- Generate `trace.html` only on explicit user request through a standalone tool.
- Render interrupted traces even when no finalized `trace.json` exists.
- Reuse one Causal IR replay and projection implementation across rendering paths.

## Non-goals

- Do not move causal attribution into the renderer.
- Do not use HTML as an attribution input.
- Do not change model prompts, tool execution, context management, or Agent decisions.
- Do not maintain a second Python implementation of Causal IR replay or projection.

## Architecture

### OpenCode runtime

OpenCode owns semantic collection and canonical finalization:

- Append runtime events to `events.jsonl` and `raw-events.jsonl`.
- Append typed Causal IR operations to `records.jsonl`.
- Persist referenced payloads under `artifacts/`.
- On normal termination or a catchable termination signal, write `trace.json`, `manifest.json`, and compatibility outputs once.
- Do not create or update `trace.html`.
- Do not periodically materialize a complete JSON or HTML snapshot while the session runs.
- Print the case directory and canonical `trace.json` path at terminal publication.

`SIGKILL` cannot execute a finalizer. In that case, the append-only journal remains the recovery source.

### Standalone renderer

A standalone TypeScript tool named `observable-trace` owns presentation:

```text
observable-trace render <case-directory-or-trace-file> [--output <path>]
```

Input resolution order:

1. Read a finalized `trace.json` when available.
2. Otherwise replay `records.jsonl` into a Causal IR snapshot.
3. Mark journal-only output as incomplete instead of presenting it as a completed case.

The renderer projects the canonical graph into the existing Provenance view and writes `trace.html` atomically. It may read artifact payloads from the case directory, but it must not modify semantic Trace inputs.

### Attribution module

The Python attribution module continues to consume `trace.json`. A convenience command may invoke `observable-trace`, but the Python module does not own or duplicate renderer semantics. A later migration may move attribution from compatibility `records` and `dataflow_edges` to canonical Causal IR `nodes` and `edges`; that migration is outside this change.

## File contract

During execution, a case directory may contain:

```text
<case>/
  events.jsonl
  raw-events.jsonl
  records.jsonl
  artifacts/
```

After graceful finalization it additionally contains:

```text
  trace.json
  manifest.json
  legacy-trace.json
  provenance-trace.json
  partial/latest.json
```

`trace.html` is absent until `observable-trace render` is run. The default output remains `<case>/trace.html` for compatibility with existing bookmarks and workflows.

## Performance policy

- Runtime writes remain append-only except for terminal files.
- No timer or event path invokes the HTML renderer.
- No periodic call materializes a full Causal IR snapshot.
- Renderer cost is paid only by an explicit offline command.

## Failure handling

- Invalid JSONL lines report the line number and fail without overwriting an existing HTML file.
- An empty or unrecoverable journal produces a non-zero exit status.
- Journal-only recovery includes an explicit incomplete-run banner and recovery provenance.
- Output uses atomic replacement so an interrupted render cannot leave a partially written HTML file.

## Compatibility

- Existing finalized `trace.json` files remain renderable.
- Existing `trace.html` files are not deleted.
- New OpenCode runs no longer advertise `trace.html` as a runtime-produced file.
- Release workflows publish `observable-trace` alongside observable OpenCode for supported platforms.

## Verification

- Runtime tests prove that no HTML or periodic full snapshot is created before or after OpenCode finalization.
- Signal tests prove that `SIGINT` and `SIGTERM` still persist canonical semantic Trace files.
- Renderer tests cover finalized `trace.json`, journal-only recovery, invalid journals, output override, and artifact access.
- A regression test verifies that rendering does not alter any semantic Trace input.
- Type checks and the existing observability and attribution suites must remain green.
