# OpenCode 最新基线语义 Trace 迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Observable OpenCode 的语义 Trace 和 Causal IR 能力迁移到上游最新 OpenCode `dev` 基线，并保持 Agent 行为无损。

**Architecture:** 以新版 `packages/core` 的 session/runner/event 体系作为运行时主体，在其旁边新增 Trace runtime adapter。运行时只追加 segment journal 和 artifact，退出后由独立 CLI materializer 生成 root `trace.json`、兼容投影和 HTML。Trace adapter 通过明确的 passive hook 接入，不修改消息、工具选择或执行结果。

**Tech Stack:** Bun 1.3.14、TypeScript、Effect 4 beta、SQLite/Drizzle、OpenTUI、Causal IR、append-only JSONL、GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-09-20-opencode-latest-trace-migration-design.md`

## Global Constraints

- 上游基线固定为 `ebb7b76eca82342642c78645109e865614533827`，不得回退到旧版 `1.14.48`。
- Trace 只做被动记录，不修改 Agent、LLM、Tool、Skill、MCP 的输入输出和执行顺序。
- 运行时只写 segment journal 和外置 artifact，不生成完整 `trace.json` 或 HTML。
- Trace 写入或 materializer 失败只能标记 observability degraded，不能让 Agent 主流程失败。
- root `trace.json` 必须继续作为 Causal IR 和离线归因的标准输入。
- 大文本必须外置到 artifact，结构化记录只保存引用、hash、大小和受限 preview。

---

### Task 1: 建立新版 Trace 运行时边界

**Files:**
- Create: `packages/core/src/observability/trace-semantic-contract.ts`
- Create: `packages/core/src/observability/trace-runtime.ts`
- Create: `packages/core/src/observability/trace-segment.ts`
- Create: `packages/core/src/observability/trace-artifact.ts`
- Create: `packages/core/test/observability/trace-segment.test.ts`
- Create: `packages/core/test/observability/trace-runtime.test.ts`
- Modify: `packages/core/src/index.ts`

**Interfaces:**
- `TraceRuntime.open(input): TraceHandle`
- `TraceHandle.record(record): void`
- `TraceHandle.recordArtifact(input): ArtifactRef`
- `TraceHandle.close(status): void`
- `TraceRuntime.resume(caseDir, sessionID): TraceHandle`
- `TraceRuntime` never throws into the caller's Agent path.

- [ ] **Step 1: Write the failing segment lifecycle tests**

Create tests that open a case, append `run.start` and `observation`, close it, reopen the session manifest, and assert the journal is append-only. Add a second test that opens a continuation and asserts the new segment has `continuation_of` set to the prior run.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `bun test packages/core/test/observability/trace-segment.test.ts packages/core/test/observability/trace-runtime.test.ts`

Expected: FAIL because the new Trace runtime modules do not exist.

- [ ] **Step 3: Implement bounded runtime storage**

Port the old segment identity, lock, manifest generation and valid-journal-prefix logic into the new core package. Use the new core global path service for the default trace root and `OPENCODE_CASE_TRACE_DIR` when provided. Keep record payloads bounded and write large content through `trace-artifact.ts`.

- [ ] **Step 4: Add the formal contract and passive error boundary**

Port the record types and dataflow relations from the old contract. `record` and artifact operations must catch serialization, permission and disk errors, increment an internal degraded counter, and return without affecting the caller.

- [ ] **Step 5: Run the focused tests and verify they pass**

Run: `bun test packages/core/test/observability/trace-segment.test.ts packages/core/test/observability/trace-runtime.test.ts`

Expected: PASS with append-only records, continuation metadata and bounded artifact references.

- [ ] **Step 6: Commit the runtime boundary**

```bash
git add packages/core/src/observability packages/core/test/observability packages/core/src/index.ts
git commit -m "feat(trace): add latest core segment runtime"
```

### Task 2: Port Causal IR and offline materialization

**Files:**
- Create: `packages/core/src/observability/causal-ir.ts`
- Create: `packages/core/src/observability/trace-materializer.ts`
- Create: `packages/core/src/observability/trace-publication.ts`
- Create: `packages/core/src/observability/causal-ir-runtime-store.ts`
- Modify: `packages/core/src/index.ts`
- Create: `packages/opencode/src/cli/cmd/trace-finalize.ts`
- Create: `packages/opencode/src/cli/cmd/trace-render.ts`
- Create: `packages/core/test/observability/trace-materializer.test.ts`
- Create: `packages/opencode/test/cli/trace-finalize.test.ts`

**Interfaces:**
- `materializeCase(caseDir, options): MaterializedTrace`
- `publishTrace(caseDir, materialized): PublicationReceipt`
- `finalize <logical-case-dir>` publishes `trace.json`, `manifest.json`, `partial/latest.json`, legacy projections and provenance projection.
- `render <case-dir-or-trace> [--output path]` only reads or performs temporary materialization and writes HTML.

- [ ] **Step 1: Create a fixture journal and failing materializer test**

Use two segments containing `run.start`, `decision`, `tool.call`, `tool.result`, `change`, and `verification` records. Assert that materialization produces ordered records, dataflow edges, segment metadata, completeness and generation fields.

- [ ] **Step 2: Run the materializer test and verify it fails**

Run: `bun test packages/core/test/observability/trace-materializer.test.ts`

Expected: FAIL because the new Causal IR materializer is absent.

- [ ] **Step 3: Port Causal IR normalization and relation validation**

Move the old relation migration, formal record validation, artifact resolution and human-readable summaries into the new core package. Unknown records remain observations; never infer a causal edge only because two records are adjacent.

- [ ] **Step 4: Implement atomic publication**

Read `session.json`, snapshot its generation, materialize all segments in manifest order, verify generation is unchanged, then atomically publish derived files. If generation changes, return a retryable error and leave the prior published generation untouched.

- [ ] **Step 5: Port CLI wiring and render integration**

Register `trace-finalize` and `trace-render` in the current CLI command registry. Keep renderer offline and ensure `render` never calls an LLM or modifies the source case directory during temporary materialization.

- [ ] **Step 6: Run focused tests and commit**

Run: `bun test packages/core/test/observability/trace-materializer.test.ts packages/opencode/test/cli/trace-finalize.test.ts`

Expected: PASS, with `trace.json` as the canonical Causal IR and HTML as a human-only projection.

```bash
git add packages/core/src/observability packages/core/test/observability packages/opencode/src/cli/cmd packages/opencode/test/cli/trace-finalize.test.ts
git commit -m "feat(trace): port causal ir materializer to latest core"
```

### Task 3: Integrate session, prompt and context semantics

**Files:**
- Modify: `packages/core/src/session/prompt.ts`
- Modify: `packages/core/src/session/compaction.ts`
- Modify: `packages/core/src/session/message.ts`
- Modify: `packages/core/src/session/message-v2.ts`
- Modify: `packages/core/src/session/event.ts`
- Modify: `packages/core/src/session/runner/index.ts`
- Modify: `packages/core/src/session/runner/llm.ts`
- Modify: `packages/core/src/session/runner/publish-llm-event.ts`
- Create: `packages/core/test/observability/session-trace-hooks.test.ts`

**Interfaces:**
- `TraceRuntime.forSession(sessionID, context)` returns an optional passive handle.
- Hook helpers accept already-built values and return `void`; they cannot alter the value.
- Context records include source refs, transform names, before/after summaries, token counts and final message refs.

- [ ] **Step 1: Add hook contract tests**

Test that recording a user prompt, context transform, compaction and LLM request does not alter deep equality of the original prompt/messages and does not change runner output.

- [ ] **Step 2: Instrument prompt and task-loop boundaries**

Record user request, task identity, loop iteration, active agent, obligations, plan state and final response claims without changing prompt assembly.

- [ ] **Step 3: Instrument context transformations and compaction**

Record source references, selection, truncation, compaction algorithm, input/output summaries and context epoch. Store complete large context in artifacts rather than inline fields.

- [ ] **Step 4: Instrument LLM turn boundaries**

Record provider/model, request/response refs, token usage, duration, finish reason, retry and error facts. Record each conversion layer from session messages to provider payload using immutable snapshots.

- [ ] **Step 5: Run focused tests and commit**

Run: `bun test packages/core/test/observability/session-trace-hooks.test.ts packages/core/test/session/compaction.test.ts packages/core/test/session/runner/*.test.ts`

Expected: PASS and unchanged message/runner behavior.

```bash
git add packages/core/src/session packages/core/test/observability
git commit -m "feat(trace): observe latest session and context pipeline"
```

### Task 4: Integrate decision, Tool, Skill, MCP and Subagent semantics

**Files:**
- Modify: `packages/core/src/session/runner/index.ts`
- Modify: `packages/core/src/session/runner/llm.ts`
- Modify: `packages/core/src/tool.ts`
- Modify: `packages/core/src/plugin/skill.ts`
- Modify: `packages/core/src/mcp.ts`
- Modify: `packages/core/src/session/instruction.ts`
- Modify: `packages/core/src/session/runner/max-steps.ts`
- Create: `packages/core/test/observability/action-trace-hooks.test.ts`
- Create: `packages/core/test/observability/skill-mcp-trace-hooks.test.ts`

**Interfaces:**
- Decision records contain decision ref, candidate actions, selected action, stated reason and parent context refs.
- Tool/Skill/MCP records contain call ref, input/output artifact refs, status, duration, error and parent decision ref.
- Subagent records contain parent session, child session, delegated task, return summary and consumption edge.

- [ ] **Step 1: Add failing action and nested-call tests**

Create a deterministic fake Tool, nested Skill chain and MCP fixture. Assert the trace records the attempted and completed calls, including omitted nested calls as observable facts when the Skill contract declares them required.

- [ ] **Step 2: Instrument decision/action creation**

Capture the model decision before dispatch, the actual dispatch result, retry and fallback behavior. Preserve the original action object and never rewrite the selected action.

- [ ] **Step 3: Instrument Tool, Skill and MCP lifecycle**

Record discovery, exposure, load, invocation, result, failure and parent-child dataflow. Large inputs/outputs go to artifacts. Keep existing permission and execution behavior unchanged.

- [ ] **Step 4: Instrument Subagent delegation and return**

Create explicit `subagent.call`, `spawned`, `delegated_to` and `reported_to` facts around child session creation and result consumption.

- [ ] **Step 5: Run focused tests and commit**

Run: `bun test packages/core/test/observability/action-trace-hooks.test.ts packages/core/test/observability/skill-mcp-trace-hooks.test.ts`

Expected: PASS with complete parent-child and invocation provenance.

```bash
git add packages/core/src/session packages/core/src/tool.ts packages/core/src/plugin/skill.ts packages/core/src/mcp.ts packages/core/test/observability
git commit -m "feat(trace): observe actions tools skills mcp and subagents"
```

### Task 5: Integrate TUI, HTTP, signal and continuation lifecycle

**Files:**
- Modify: `packages/opencode/src/cli/cmd/tui/worker.ts`
- Modify: `packages/opencode/src/cli/cmd/tui/worker-trace.ts`
- Modify: `packages/opencode/src/cli/cmd/tui/trace-materializer-process.ts`
- Modify: `packages/opencode/src/cli/cmd/run/trace.ts`
- Modify: `packages/opencode/src/server/routes/instance/httpapi/groups/session.ts`
- Modify: `packages/opencode/src/server/routes/instance/httpapi/groups/v2/session.ts`
- Create: `packages/opencode/test/cli/tui/latest-trace-lifecycle.test.ts`
- Create: `packages/opencode/test/cli/http-trace-lifecycle.test.ts`

**Interfaces:**
- TUI and HTTP code call lifecycle methods only; they do not know Causal IR internals.
- Exit receipt prints `directory`, `trace`, `manifest` and `partial` when publication succeeds.
- Signal handling closes journal where possible and never promises userland work on `SIGKILL`.

- [ ] **Step 1: Add lifecycle fixture tests**

Test normal close, `SIGINT`, `SIGTERM`, incomplete segment recovery and same-session continuation. Assert the Agent result path is unchanged when materialization is forced to fail.

- [ ] **Step 2: Connect TUI worker lifecycle**

Open/resume one segment per process lifetime, close it at worker shutdown, then launch the independent materializer after all worker resources are released.

- [ ] **Step 3: Connect HTTP root session lifecycle**

Bind the trace session to the HTTP root session, finalize on deletion/server shutdown, and preserve the case directory in the response or shutdown receipt.

- [ ] **Step 4: Add signal and continuation handling**

Map incomplete old segments to `interrupted_unfinalized`, create a new continuation segment, and preserve `continuation_of` in the manifest.

- [ ] **Step 5: Run focused tests and commit**

Run: `bun test packages/opencode/test/cli/tui/latest-trace-lifecycle.test.ts packages/opencode/test/cli/http-trace-lifecycle.test.ts`

Expected: PASS for normal shutdown, recoverable interruptions and repeat finalize.

```bash
git add packages/opencode/src/cli packages/opencode/src/server packages/opencode/test/cli
git commit -m "feat(trace): integrate latest lifecycle and continuation"
```

### Task 6: Migrate compatibility tooling and documentation

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/release-observable-linux.yml`
- Create: `.github/workflows/release-observable-latest.yml`
- Modify: `.claude/skills/rootcause-analysis/references/trace-structure.md`
- Modify: `.claude/skills/rootcause-analysis/scripts/trace_query.py`
- Create: `packages/core/test/observability/legacy-trace-compat.test.ts`

**Interfaces:**
- Existing environment variables remain supported: `OPENCODE_CASE_TRACE`, `OPENCODE_CASE_ID`, `OPENCODE_CASE_TRACE_DIR`, `OPENCODE_TRACE_SEGMENT_*`.
- Existing `observable-trace finalize` and `render` command syntax remains valid.
- Rootcause analysis consumes finalized root `trace.json`, never a segment-local journal.

- [ ] **Step 1: Add legacy fixture compatibility tests**

Load a representative old Trace fixture and assert current `trace_query.py validate`, summary, node and neighbors queries continue to work on the new output.

- [ ] **Step 2: Update CLI and Skill contract documentation**

Document the latest binaries, root case selection, finalize after continuation, render-only behavior and standard attribution input.

- [ ] **Step 3: Update release workflow**

Build latest OpenCode and the standalone Trace binary for Linux x64, Linux arm64, macOS arm64/x64 and Windows targets supported by the current release matrix. Publish checksums and version metadata.

- [ ] **Step 4: Run compatibility tests and commit**

Run: `bun test packages/core/test/observability/legacy-trace-compat.test.ts`

Expected: PASS with old and new Trace inputs accepted by the analysis tooling.

```bash
git add README.md .github/workflows .claude/skills/rootcause-analysis packages/core/test/observability/legacy-trace-compat.test.ts
git commit -m "docs(release): publish latest observable opencode workflow"
```

### Task 7: Full verification and migration report

**Files:**
- Create: `docs/superpowers/reports/2026-09-20-opencode-latest-trace-migration.md`
- Modify: `docs/superpowers/plans/2026-09-20-opencode-latest-trace-migration.md`

- [ ] **Step 1: Run core typecheck and focused tests**

Run: `bun turbo typecheck` and all new Trace tests. Record exact pass/fail counts and any unrelated upstream failures.

- [ ] **Step 2: Run behavior-equivalence tests with Trace disabled**

Run the same deterministic session fixtures with `OPENCODE_CASE_TRACE=0` and `OPENCODE_CASE_TRACE=1`; compare LLM request payloads, Tool inputs, Tool outputs and action sequence hashes.

- [ ] **Step 3: Run end-to-end interactive and HTTP smoke tests**

Run one TUI session, one `opencode serve` request, one Tool/Skill/MCP/Subagent case and one forced-compaction case. Finalize and render every case.

- [ ] **Step 4: Run continuation and interruption recovery tests**

Create a session, finalize it, continue it, finalize again, then test a process terminated before terminal record and recover with `finalize`.

- [ ] **Step 5: Validate published artifacts**

For each case run:

```bash
jq -e . "$CASE_DIR/trace.json" >/dev/null
python3 .claude/skills/rootcause-analysis/scripts/trace_query.py validate --trace "$CASE_DIR/trace.json"
test -s "$CASE_DIR/trace.html"
```

- [ ] **Step 6: Write the migration report and mark the plan**

Record the migrated source map, behavior-equivalence result, Trace coverage, known gaps and release artifact checksums. Mark only completed tasks as checked.

- [ ] **Step 7: Commit the final verification report**

```bash
git add docs/superpowers/reports/2026-09-20-opencode-latest-trace-migration.md docs/superpowers/plans/2026-09-20-opencode-latest-trace-migration.md
git commit -m "test: verify latest opencode trace migration"
```
