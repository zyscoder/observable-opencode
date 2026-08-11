# Offline Trace Renderer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove HTML and periodic full-snapshot generation from observable OpenCode and provide a standalone `observable-trace render` tool that renders finalized or journal-only traces on demand.

**Architecture:** OpenCode remains the passive semantic recorder and writes append-only JSONL plus one canonical terminal `trace.json`. A new workspace package owns Causal IR recovery, Provenance projection, artifact-safe HTML rendering, and its CLI. The Python attribution module continues to consume semantic Trace data and does not duplicate renderer semantics.

**Tech Stack:** TypeScript, Bun, Causal IR JSONL, Bun test, GitHub Actions, existing Python attribution CLI.

## Global Constraints

- OpenCode must never import or invoke an HTML renderer.
- Runtime collection must not periodically materialize a complete Causal IR JSON or HTML snapshot.
- Agent prompts, model requests, tools, context management, and task-loop behavior must remain unchanged.
- `trace.json` remains the canonical finalized Causal IR document.
- `records.jsonl` remains the append-only recovery source.
- Journal-only rendering must be explicitly marked incomplete.
- Rendering must not mutate semantic Trace inputs.
- Existing unrelated untracked benchmark reports must remain untouched.

---

## File Structure

- Create `packages/trace-renderer/` for the standalone package, loader, CLI, renderer, and tests.
- Move `causal-trace-viewer.ts`, `case-trace-html.ts`, and their renderer tests out of `packages/opencode/`.
- Modify `case-trace.ts` and `trace-publication.ts` so OpenCode publishes semantic files only.
- Modify the release workflow to publish `observable-trace-<platform>` binaries.
- Update the root and attribution READMEs with capture, render, and analyze commands.

---

### Task 1: Lock the OpenCode Runtime Boundary

**Files:**
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/test/observability/trace-publication.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/trace-publication.ts`

**Interfaces:**
- Consumes: existing `CaseTrace.configure`, `CaseTrace.finish`, signal finalizers, and Causal IR journal append callbacks.
- Produces: append-only runtime JSONL and terminal semantic JSON, but no `trace.html`.

- [ ] Write tests asserting that a running case has `records.jsonl` but neither `trace.html` nor `partial/latest.json`.
- [ ] Add terminal assertions for `trace.json`, `manifest.json`, and `partial/latest.json`, with no HTML.
- [ ] Run `bun test test/observability/case-trace.test.ts test/observability/trace-publication.test.ts` from `packages/opencode`; verify failure because the current runtime creates HTML and partial snapshots.
- [ ] Remove the HTML import, HTML streaming branch, `writePartial()` method, and every event-path call to it from `case-trace.ts`.
- [ ] Keep terminal writes for canonical and compatibility JSON. Change terminal completeness and publication to depend on semantic files, not HTML.
- [ ] Re-run the focused tests and commit as `refactor(observability): keep HTML outside the runtime`.

---

### Task 2: Extract the Existing Renderer

**Files:**
- Create: `packages/trace-renderer/package.json`
- Create: `packages/trace-renderer/tsconfig.json`
- Move: `packages/opencode/src/observability/causal-trace-viewer.ts` to `packages/trace-renderer/src/viewer.ts`
- Move: `packages/opencode/src/observability/case-trace-html.ts` to `packages/trace-renderer/src/html.ts`
- Move: `packages/opencode/test/observability/case-trace-html.test.ts` to `packages/trace-renderer/test/html.test.ts`
- Modify: `bun.lock`

**Interfaces:**
- Consumes: Causal IR types and semantic contract exports from the `opencode` workspace package.
- Produces: `writeProvenanceTraceHtmlFile(target, trace, options)` in a package OpenCode does not depend on.

- [ ] Move renderer sources and tests without changing behavior.
- [ ] Declare `opencode: workspace:*` as a renderer dependency; do not add a reverse dependency.
- [ ] Update imports to `opencode/observability/...` and regenerate the lockfile with `bun install --lockfile-only`.
- [ ] Run `bun test packages/trace-renderer/test/html.test.ts` and preserve the existing HTML safety, streaming, artifact, and visual-contract coverage.
- [ ] Run `rg "trace-renderer|case-trace-html|causal-trace-viewer" packages/opencode/src`; require no renderer import from OpenCode runtime source.
- [ ] Commit as `refactor: extract standalone trace renderer`.

---

### Task 3: Load Finalized and Interrupted Traces

**Files:**
- Create: `packages/trace-renderer/src/load.ts`
- Create: `packages/trace-renderer/test/load.test.ts`

**Interfaces:**
- Produces: `loadRenderableTrace(input: string): RenderableTraceLoadResult`.
- `RenderableTraceLoadResult` contains `trace`, `caseDir`, `source: "trace.json" | "records.jsonl"`, and `incomplete`.
- Consumes: `replayCausalIRJournal`, `replayCausalIRTrace`, and `projectProvenanceTrace`.

- [ ] Write a finalized-Trace test that expects compatibility projection loading and `incomplete: false`.
- [ ] Run `bun test packages/trace-renderer/test/load.test.ts`; verify failure because the loader is absent.
- [ ] Implement strict `trace.json` validation and projection loading.
- [ ] Add a journal-only test with node and edge operations but no `case.finalized`.
- [ ] Implement JSONL replay. Prefer `replayCausalIRTrace`; otherwise replay the graph and synthesize a manifest with `recovery_status: "incomplete_journal_replay"` and a non-success status.
- [ ] Add malformed-line and empty-journal tests. Errors must include `records.jsonl:<one-based-line>`.
- [ ] Run the loading suite and commit as `feat(trace-renderer): replay causal IR journals`.

---

### Task 4: Add the Offline CLI

**Files:**
- Create: `packages/trace-renderer/src/cli.ts`
- Create: `packages/trace-renderer/test/cli.test.ts`
- Modify: `packages/trace-renderer/package.json`

**Interfaces:**
- Consumes: `loadRenderableTrace(input)` and `writeProvenanceTraceHtmlFile(target, trace)`.
- Produces: `observable-trace render <input> [--output <path>]`.

- [ ] Write a failing CLI test for a finalized fixture, default `<case>/trace.html`, exit code 0, and a summary containing source, completeness, and output path.
- [ ] Implement strict command parsing, absolute path resolution, and atomic rendering.
- [ ] Add journal-only rendering assertions for an explicit incomplete-run banner.
- [ ] Add `--output` and input-immutability tests. Hash `trace.json`, JSONL, and artifacts before and after rendering.
- [ ] Run all renderer tests and commit as `feat(trace-renderer): add offline render CLI`.

---

### Task 5: Package and Document the Renderer

**Files:**
- Modify: `.github/workflows/release-observable-linux.yml`
- Modify: `README.md`
- Modify: `tools/trace_attribution/README.md`

**Interfaces:**
- Produces: `observable-trace-<platform>` release assets for every supported OpenCode target.

- [ ] Add renderer typecheck and test steps to the release job.
- [ ] Compile one renderer binary per existing platform target and fail if an expected asset is absent.
- [ ] Include both binary families in `SHA256SUMS` and release notes.
- [ ] Document `observable-trace render <case-dir>` and the separate `trace-attribution --trace <case>/trace.json --question ...` workflow.
- [ ] Explain finalized versus journal-only rendering, `SIGKILL`, and that HTML is never an attribution input.
- [ ] Validate local help and render commands, then commit as `build: publish offline trace renderer`.

---

### Task 6: Full Regression and Real Trace Verification

**Files:**
- Modify only files required by failures directly caused by Tasks 1-5.

**Interfaces:**
- Verifies passive collection, terminal finalization, signal handling, journal recovery, HTML rendering, attribution compatibility, and release compilation.

- [ ] Run root `bun run typecheck`.
- [ ] Run OpenCode observability tests and every renderer test.
- [ ] Run `python -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`.
- [ ] Run a local traced session and verify JSONL exists while running, terminal semantic JSON exists after exit, and HTML is absent until explicitly rendered.
- [ ] Render the real Trace and inspect components, dataflow edges, artifacts, and incomplete-state labeling.
- [ ] Verify SHA-256 hashes of `trace.json`, JSONL, and artifacts are byte-identical before and after rendering.
- [ ] Commit only regression fixes directly required by this migration.
