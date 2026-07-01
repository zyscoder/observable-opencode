# Trace v5 Claim, Skill, Compaction, And HTTP Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make trace v5 cleaner for offline attribution and validate release binaries through the same HTTP session path used by benchmark cases.

**Architecture:** Keep `case-trace.ts` as the semantic producer and `causal-trace-viewer.ts` as the single HTML renderer. Add narrowly scoped helper logic for claim filtering, skill-request text discovery, compaction artifact refs, and subagent trace availability. Release validation uses `opencode serve` plus HTTP `/session` and `/session/:id/message`.

**Tech Stack:** TypeScript, Bun test, opencode observability trace writer, static HTML renderer, headless opencode HTTP server, `tsgo --noEmit`.

## Global Constraints

- Do not implement root-cause diagnosis.
- Do not split `trace.html` into a separate viewer file.
- Do not remove legacy trace files.
- Keep large payloads in artifacts and expose links from trace fields.
- Release validation must use `opencode serve` and HTTP session/message requests, not `opencode run`.

---

### Task 1: Version And Claim Noise Tests

**Files:**

- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Produces `TRACE_VERSION = "5.0"`.
- Produces response claims without Markdown scaffolding.

- [ ] Add failing tests for final answers containing `**总结：**`, numbered list ordinals, Markdown table headers, and table separators.
- [ ] Run focused test and verify it fails because noisy claims still appear.
- [ ] Update version expectations to `5.0`.

### Task 2: Claim Extractor Filtering

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**

- Consumes final answer text.
- Produces only factual claim strings.

- [ ] Update `isNonFactualResponseClaim` to strip bold markers before bullet markers.
- [ ] Filter `isBrokenClaimFragment` before creating `response.claim`.
- [ ] Add Markdown table/header/section-scaffold detection.
- [ ] Run focused claim tests and verify green.

### Task 3: Skill Request Provenance

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes prompt payloads with nested `parts`.
- Produces `skill.load` records for explicit skill requests.

- [ ] Add failing test with `prompt.assembly({ input: { parts: [{ type: "text", text: "请使用 repo-audit skill" }] } })`.
- [ ] Run focused test and verify no `skill.load` record exists.
- [ ] Add `parts`, `input`, `messages`, `prompt`, and `body` to `collectTextCandidates`.
- [ ] Verify `skill_request_unresolved` is counted when the skill is unavailable.

### Task 4: Compaction And Subagent Provenance

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes compaction output summary and auto-continue metadata.
- Produces `summary_artifact_ref`, non-empty `after_context_refs`, and subagent unavailable reason.

- [ ] Add failing test for small compaction `output_summary` that still expects `summary_artifact_ref`.
- [ ] Add failing test for `auto_continue_prompt_ref` being promoted into `after_context_refs` when no after refs are provided.
- [ ] Add failing test for subagent `child_trace_unavailable_reason`.
- [ ] Implement minimal helpers and verify tests pass.

### Task 5: Viewer And Docs

**Files:**

- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`
- Modify: `docs/observable-benchmark-trace.md`

**Interfaces:**

- Consumes v5 fields.
- Shows cleaner claim matrix and subagent trace availability reason.

- [ ] Update viewer version display assertions.
- [ ] Add child trace unavailable reason to subagent section if present.
- [ ] Document HTTP validation path.
- [ ] Run typecheck and diff checks.

### Task 6: Release Validation Through HTTP Server

**Files:**

- No production source changes expected.

**Interfaces:**

- Starts release binary with `serve`.
- Sends HTTP requests to create sessions and messages.

- [ ] Push branch and release tag.
- [ ] Download macOS release binary and verify SHA256.
- [ ] Start `opencode serve --hostname 127.0.0.1 --port 0` with isolated env.
- [ ] `POST /session?directory=<repo>` and `POST /session/:id/message?directory=<repo>` for tool/MCP/skill, large-context/subagent, and forced-compaction scenarios.
- [ ] Analyze generated traces for missing semantics and redundant noise.

## Verification

- `./node_modules/.bin/prettier --write` on touched files.
- `./node_modules/.bin/tsgo --noEmit` from `packages/opencode`.
- `git diff --check`.
- If Bun is unavailable locally, document that Bun tests could not be run and rely on release-binary HTTP E2E validation.

## Local Verification Result

- `./node_modules/.bin/prettier --write ...`: passed.
- `./node_modules/.bin/tsgo --noEmit` from `packages/opencode`: passed.
- `git diff --check`: passed.
- `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`: not run locally because `bun` is not available on PATH in this environment.
