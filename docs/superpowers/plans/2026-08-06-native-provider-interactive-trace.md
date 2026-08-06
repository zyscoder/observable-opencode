# Native Provider and Interactive Session Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve OpenCode's native model configuration while making passive semantic Trace collection work for both HTTP and TUI execution, with one end-to-end Trace per independent root session.

**Architecture:** OpenCode Config remains the only authority for provider and model resolution; `MODEL`, `APIKEY`, and `URL` are ordinary `{env:...}` substitution inputs. A small session router replaces the process-wide Trace singleton: root sessions receive isolated `ActiveCaseTrace` instances, child/subagent sessions alias their parent root Trace, and process finalizers finish every active root Trace without changing Agent execution.

**Tech Stack:** TypeScript, Bun test, Effect, OpenCode Config/Provider APIs, Causal IR, JSON/HTML Trace artifacts.

## Global Constraints

- Do not add core-code special cases for `DEEPSEEK_API_KEY`, `MODEL`, `APIKEY`, or `URL`.
- Do not change Config loading order, Provider schema, model resolution, authentication, retries, prompts, tools, or Agent task-loop behavior.
- Trace remains passive, read-only, and unavailable to the running Agent.
- A user-created root session owns one Trace; child/subagent sessions remain inside their parent root Trace.
- Independent root sessions in the same TUI or server process must not share events, IDs, terminal state, or output files.
- Normal exit, `SIGINT`, `SIGTERM`, and `SIGHUP` finish all capturable root traces; `SIGKILL` can preserve only already-written partial artifacts.
- Existing one-process/one-case benchmark directory naming remains compatible.

---

## File Structure

- Create `packages/opencode/src/observability/case-trace-session.ts`: pure session routing, root/child aliasing, reference ownership, and bulk finalization.
- Create `packages/opencode/test/observability/case-trace-session.test.ts`: focused router unit tests with fake Trace handles.
- Modify `packages/opencode/src/observability/case-trace.ts`: use the router, expose routed `startSpan`, `aliasSession`, `finishSession`, and `finishAll` APIs, and make process finalizers bulk-safe.
- Modify the twelve production Trace span call sites under `session/`, `tool/`, `mcp/`, and `cli/cmd/run*`: call routed `CaseTrace.startSpan()` instead of dereferencing a global Trace.
- Modify `packages/opencode/src/tool/task.ts`: alias a child/subagent session to the parent root Trace before the child prompt starts.
- Modify `packages/opencode/src/session/session.ts`: finish an explicitly deleted root session without finalizing its parent when a child is removed.
- Modify `packages/opencode/src/cli/cmd/tui/worker.ts`: finish all root session traces during worker shutdown.
- Modify `packages/opencode/test/observability/case-trace.test.ts`: add multi-root isolation, child alias, naming, receipt, and signal-finalization integration tests.
- Modify `packages/opencode/test/config/config.test.ts`: prove generic provider values resolve through existing `{env:...}` substitution with no production Config change.
- Modify `README.md`: replace DeepSeek-specific setup with native Config guidance and document TUI plus HTTP Trace operation.

---

### Task 1: Lock Native Provider Configuration Behavior

**Files:**
- Modify: `packages/opencode/test/config/config.test.ts`

**Interfaces:**
- Consumes: existing `Config.load()` and `{env:VARIABLE}` substitution.
- Produces: a regression contract showing that generic OpenAI-compatible provider configuration needs no observable-specific runtime code.

- [ ] **Step 1: Add a failing generic provider substitution test**

Add a test beside the existing environment-substitution tests. It must save and restore all three environment variables and load this standard config:

```ts
test("resolves generic model credentials through native provider config", async () => {
  const previous = {
    MODEL: process.env.MODEL,
    APIKEY: process.env.APIKEY,
    URL: process.env.URL,
  }
  process.env.MODEL = "compatible/glm-4.5"
  process.env.APIKEY = "test-compatible-key"
  process.env.URL = "https://compatible.example.test/v1"

  try {
    await using tmp = await tmpdir({
      init: async (dir) => {
        await writeConfig(dir, {
          $schema: "https://opencode.ai/config.json",
          model: "{env:MODEL}",
          provider: {
            compatible: {
              npm: "@ai-sdk/openai-compatible",
              name: "OpenAI Compatible",
              options: {
                apiKey: "{env:APIKEY}",
                baseURL: "{env:URL}",
              },
              models: {
                "glm-4.5": { name: "GLM 4.5" },
              },
            },
          },
        })
      },
    })
    await WithInstance.provide({
      directory: tmp.path,
      fn: async () => {
        const config = await load()
        expect(config.model).toBe("compatible/glm-4.5")
        expect(config.provider?.compatible?.options).toEqual({
          apiKey: "test-compatible-key",
          baseURL: "https://compatible.example.test/v1",
        })
        expect(config.provider?.compatible?.models?.["glm-4.5"]?.name).toBe("GLM 4.5")
      },
    })
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
  }
})
```

- [ ] **Step 2: Run the focused test and inspect schema compatibility**

Run:

```bash
bun --cwd packages/opencode test test/config/config.test.ts
```

Expected: PASS without any production Config edit. If the schema normalizes an option without changing its value, assert the normalized structure rather than weakening the value assertions.

- [ ] **Step 3: Commit the regression contract**

```bash
git add packages/opencode/test/config/config.test.ts
git commit -m "test: cover native compatible provider env config"
```

---

### Task 2: Add a Pure Root-Session Trace Router

**Files:**
- Create: `packages/opencode/src/observability/case-trace-session.ts`
- Create: `packages/opencode/test/observability/case-trace-session.test.ts`

**Interfaces:**
- Consumes: a factory supplied by `case-trace.ts` for creating an `ActiveCaseTrace`.
- Produces: `SessionTraceRegistry<T>`, `TraceRouteHint`, `traceRouteHint()`, root/child aliasing, reference ownership, and bulk finalization.

- [ ] **Step 1: Write failing router tests**

Use a fake handle and cover these exact behaviors:

```ts
type FakeTrace = {
  id: string
  finished: Array<{ status: string }>
  signals: NodeJS.Signals[]
}

const create = (id: string): FakeTrace => ({ id, finished: [], signals: [] })

test("isolates independent root sessions", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  expect(registry.resolve({ sessionID: "ses_a" })).not.toBe(registry.resolve({ sessionID: "ses_b" }))
  expect(registry.resolve({ sessionID: "ses_a" })).toBe(registry.resolve({ sessionID: "ses_a" }))
})

test("routes child sessions to their parent root", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const parent = registry.resolve({ sessionID: "ses_parent" })
  registry.alias("ses_child", "ses_parent")
  expect(registry.resolve({ sessionID: "ses_child" })).toBe(parent)
})

test("routes reference-only records to their owner", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  const owner = registry.resolve({ sessionID: "ses_owner" })
  registry.remember(owner, ["span:span_1", "decision:dec_1"])
  expect(registry.resolve({ refs: ["span:span_1"] })).toBe(owner)
})

test("finishes every root exactly once", () => {
  const registry = new SessionTraceRegistry<FakeTrace>((sessionID) => create(sessionID ?? "process"))
  registry.resolve({ sessionID: "ses_a" })
  registry.resolve({ sessionID: "ses_b" })
  registry.finishAll((trace) => trace.finished.push({ status: "success" }))
  registry.finishAll((trace) => trace.finished.push({ status: "success" }))
  expect(registry.values().map((trace) => trace.finished.length)).toEqual([1, 1])
})
```

Also test `traceRouteHint()` for `sessionID`, `session_id`, nested `data.sessionID`, nested `metadata.sessionID`, `span_id`, `source_refs`, and edge endpoint IDs. It must not treat `child_session_id` as an independent root before an explicit alias is registered.

- [ ] **Step 2: Run the tests to verify the module is missing**

Run:

```bash
bun --cwd packages/opencode test test/observability/case-trace-session.test.ts
```

Expected: FAIL because `case-trace-session.ts` does not exist.

- [ ] **Step 3: Implement the router**

Create the focused module with this public contract:

```ts
export type TraceRouteHint = {
  sessionID?: string
  refs: string[]
}

export function traceRouteHint(input: unknown): TraceRouteHint

export class SessionTraceRegistry<T extends object> {
  constructor(private readonly create: (sessionID: string | undefined, ordinal: number) => T)
  resolve(hint?: Partial<TraceRouteHint>): T
  alias(childSessionID: string, parentSessionID: string): T
  remember(trace: T, refs: string[]): void
  finishSession(sessionID: string, finish: (trace: T) => void): void
  finishAll(finish: (trace: T) => void): void
  values(): T[]
  reset(finish?: (trace: T) => void): void
}
```

Implementation rules:

```ts
const directSessionKeys = new Set(["sessionID", "session_id", "parentSessionID", "parent_session_id"])
const referenceKeys = new Set([
  "span_id",
  "turn_id",
  "decision_id",
  "snapshot_id",
  "record_id",
  "node_id",
  "fact_id",
  "verification_id",
  "change_id",
])
```

`resolve()` applies this order: explicit/aliased session, owned reference, sole existing root, then process-level trace. It must deduplicate `values()` by object identity so aliases never cause duplicate finalization. `finishAll()` tracks finalized object identities and is idempotent.

- [ ] **Step 4: Run router tests**

```bash
bun --cwd packages/opencode test test/observability/case-trace-session.test.ts
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the router**

```bash
git add packages/opencode/src/observability/case-trace-session.ts packages/opencode/test/observability/case-trace-session.test.ts
git commit -m "feat: add root session trace router"
```

---

### Task 3: Route CaseTrace APIs and Preserve Subagent Causality

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/session/processor.ts`
- Modify: `packages/opencode/src/session/llm.ts`
- Modify: `packages/opencode/src/session/prompt.ts`
- Modify: `packages/opencode/src/tool/skill.ts`
- Modify: `packages/opencode/src/tool/task.ts`
- Modify: `packages/opencode/src/tool/tool.ts`
- Modify: `packages/opencode/src/tool/shell.ts`
- Modify: `packages/opencode/src/mcp/index.ts`
- Modify: `packages/opencode/src/cli/cmd/run.ts`
- Modify: `packages/opencode/src/cli/cmd/run/runtime.queue.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: `SessionTraceRegistry`, `traceRouteHint`, existing `ActiveCaseTrace`, and all current semantic Trace methods.
- Produces: routed `CaseTrace.startSpan`, `CaseTrace.aliasSession`, `CaseTrace.finishSession`, and `CaseTrace.finishAll` while retaining backward-compatible `configure`, `get`, `setSessionID`, and `finish`.

- [ ] **Step 1: Add failing multi-session integration tests**

Spawn a child Bun process with tracing enabled and execute:

```ts
CaseTrace.promptAssembly({
  stage: "initial_user_request",
  session_id: "ses_root_a",
  message_id: "msg_a",
  input: { parts: [{ type: "text", text: "root A" }] },
})
const spanA = CaseTrace.startSpan({
  component: "llm",
  operation: "stream",
  name: "compatible/model-a",
  input: { sessionID: "ses_root_a" },
})
CaseTrace.aliasSession("ses_child_a", "ses_root_a")
CaseTrace.llmTurn({
  turn_id: "turn_child_a",
  session_id: "ses_child_a",
  parent_session_id: "ses_root_a",
  agent: "general",
  agent_role: "subagent",
  provider_id: "compatible",
  model_id: "model-a",
  status: "success",
  source_refs: spanA ? [`span:${spanA.id}`] : [],
})
CaseTrace.promptAssembly({
  stage: "initial_user_request",
  session_id: "ses_root_b",
  message_id: "msg_b",
  input: { parts: [{ type: "text", text: "root B" }] },
})
CaseTrace.finishAll({ status: "success", result: { reason: "test.shutdown" } })
```

Assert that two root directories exist, root A contains `ses_root_a` plus `ses_child_a`, root B contains only `ses_root_b`, each manifest has a different `run_id`, and neither Trace contains the other root's prompt text.

- [ ] **Step 2: Run the integration test to verify current singleton contamination**

```bash
bun --cwd packages/opencode test test/observability/case-trace.test.ts --test-name-pattern "isolates root sessions"
```

Expected: FAIL because only one process-wide active Trace exists.

- [ ] **Step 3: Replace singleton state with routed state**

In `case-trace.ts`, retain `ActiveCaseTrace` but replace `let active` with a registry plus base configuration. The factory applies these case-ID rules:

```ts
function routedCaseID(baseCaseID: string | undefined, sessionID: string | undefined, ordinal: number) {
  if (!sessionID) return safeCaseID(baseCaseID ?? "")
  if (baseCaseID && ordinal === 0) return safeCaseID(baseCaseID)
  if (baseCaseID) return safeCaseID(`${baseCaseID}--${sessionID}`)
  return safeCaseID(`session-${sessionID}`)
}
```

Add route helpers that remember every returned semantic identity:

```ts
function routed(input?: unknown) {
  if (!enabledFromEnv()) return undefined
  return registry.resolve(traceRouteHint(input))
}

function remember<T>(trace: ActiveCaseTrace | undefined, value: T): T {
  if (!trace) return value
  registry.remember(trace, traceRouteHint(value).refs)
  return value
}
```

Expose these APIs:

```ts
export function startSpan(input: StartSpanInput) {
  const trace = routed(input)
  if (!trace) return undefined
  const span = trace.startSpan(input)
  registry.remember(trace, [`span:${span.id}`, span.id])
  return span
}

export function aliasSession(childSessionID: string, parentSessionID: string) {
  if (!enabledFromEnv()) return
  registry.alias(childSessionID, parentSessionID)
}

export function finishSession(sessionID: string, input?: FinishTraceInput) {
  registry.finishSession(sessionID, (trace) => trace.finish(input))
}

export function finishAll(input?: FinishTraceInput) {
  registry.finishAll((trace) => trace.finish(input))
}
```

Every existing namespace method routes using its own input and registers returned refs. `get()` remains a compatibility fallback for tests and single-case callers, but production span creation must no longer use it.

Process finalizers call `finishAll`/bulk `flushForSignal`; one failing trace is caught so remaining roots still finalize. Bulk completion must preserve the original signal exit code.

- [ ] **Step 4: Route all production span/event call sites**

Replace all twelve forms of:

```ts
CaseTrace.get()?.startSpan(input)
```

with:

```ts
CaseTrace.startSpan(input)
```

Replace `Boolean(CaseTrace.get())` in `tool/shell.ts` with `CaseTrace.isEnabled()`, and replace `CaseTrace.get()?.event(...)` with `CaseTrace.event(...)`. This prevents a no-session lookup from selecting the wrong root.

- [ ] **Step 5: Alias subagent sessions before child execution**

Immediately after `nextSession` is known in `tool/task.ts`, add:

```ts
CaseTrace.aliasSession(nextSession.id, ctx.sessionID)
```

This must execute before `subagent_prompt` and `ops.prompt(...)`, ensuring child LLM, tool, MCP, skill, compaction, and response records remain in the parent end-to-end Trace. Do not expose the alias to the Agent or change the child prompt.

- [ ] **Step 6: Run focused routing and semantic regressions**

```bash
bun --cwd packages/opencode test test/observability/case-trace-session.test.ts test/observability/case-trace.test.ts
```

Expected: PASS, including all pre-existing semantic Trace assertions.

- [ ] **Step 7: Commit routed CaseTrace**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/src/session/processor.ts packages/opencode/src/session/llm.ts packages/opencode/src/session/prompt.ts packages/opencode/src/tool/skill.ts packages/opencode/src/tool/task.ts packages/opencode/src/tool/tool.ts packages/opencode/src/tool/shell.ts packages/opencode/src/mcp/index.ts packages/opencode/src/cli/cmd/run.ts packages/opencode/src/cli/cmd/run/runtime.queue.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "feat: isolate traces by root session"
```

---

### Task 4: Finalize Every Interactive TUI Session

**Files:**
- Modify: `packages/opencode/src/cli/cmd/tui/worker.ts`
- Modify: `packages/opencode/src/session/session.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: `CaseTrace.finishAll()` and existing worker shutdown RPC.
- Produces: normal TUI exit finalizes every active root Trace and prints one terminal receipt per root session.

- [ ] **Step 1: Add a failing worker-shutdown-equivalent test**

Spawn a process that creates two root sessions, then invokes:

```ts
CaseTrace.finishAll({
  status: "success",
  result: { reason: "worker.shutdown" },
})
```

Run without `OPENCODE_CASE_TRACE_QUIET`. Capture `stderr` and assert exactly two occurrences of
`[observable-opencode] Session trace saved`, each with a distinct `session:` and existing `html:` path.

- [ ] **Step 2: Verify the focused receipt test**

```bash
bun --cwd packages/opencode test test/observability/case-trace.test.ts --test-name-pattern "publishes every interactive session"
```

Expected before the worker edit: the direct API test passes after Task 3, while source inspection still finds `CaseTrace.finish(` in `worker.ts`.

- [ ] **Step 3: Change TUI worker shutdown to bulk finalization**

Replace the worker's final call with:

```ts
CaseTrace.finishAll({
  status: failure ? "error" : "success",
  error: failure,
  result: {
    reason: "worker.shutdown",
  },
})
```

Keep shutdown order unchanged: dispose instances, stop the server, finalize Trace in `finally`, return through existing RPC, then let `thread.ts` terminate the worker. Do not move Trace output to stdout.

- [ ] **Step 4: Finalize explicitly deleted root sessions**

After a root session has been successfully removed in `session/session.ts`, finalize only that root:

```ts
if (!session.parentID) {
  CaseTrace.finishSession(sessionID, {
    status: "success",
    result: { reason: "session.deleted" },
  })
}
```

Place this after the existing deletion work succeeds. Child deletion must not call `finishSession`, because the child is an alias of the parent end-to-end Trace.

- [ ] **Step 5: Exercise normal, deletion, and signal completion**

Run the focused tests that spawn child processes for normal exit, `SIGINT`, `SIGTERM`, and `SIGHUP`. Assert every pre-existing root has `trace.json`, `trace.html`, and `partial/latest.json`, and signal traces retain `cancelled` status.

```bash
bun --cwd packages/opencode test test/observability/case-trace.test.ts test/observability/trace-publication.test.ts
```

Expected: PASS with no duplicate receipts and no changed signal exit codes.

- [ ] **Step 6: Commit TUI finalization**

```bash
git add packages/opencode/src/cli/cmd/tui/worker.ts packages/opencode/src/session/session.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "feat: finalize all interactive session traces"
```

---

### Task 5: Rewrite the Observable README Around Native Configuration

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: actual Config paths, `{env:...}` syntax, release asset names, TUI command, HTTP endpoints, Trace variables, and signal semantics.
- Produces: provider-neutral installation and operation instructions for both interactive users and benchmark harnesses.

- [ ] **Step 1: Replace the DeepSeek-only configuration section**

Use the heading `## 配置模型与 Provider` and state that model information comes from:

```text
~/.config/opencode/opencode.json[c]
<project>/opencode.json[c]
OPENCODE_CONFIG
OPENCODE_CONFIG_DIR
OPENCODE_CONFIG_CONTENT
```

Include a complete OpenAI-compatible config using `model: "{env:MODEL}"`, `apiKey: "{env:APIKEY}"`, and `baseURL: "{env:URL}"`. Explain that users may reference any environment-variable names, and that built-in Providers keep their native authentication flow.

- [ ] **Step 2: Add TUI as the first Trace usage mode**

Document:

```bash
export MODEL="compatible/glm-4.5"
export APIKEY="<provider-api-key>"
export URL="https://provider.example.com/v1"
export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/traces"

opencode /data/repos/target-project
```

Explain that each independent root session gets one directory, multi-turn conversation stays in that directory, and subagent sessions remain linked in the parent Trace. Normal exit prints all completed paths; capturable signals produce cancelled/partial paths; `SIGKILL` cannot run finalization.

- [ ] **Step 3: Make HTTP server an equal second mode**

Keep `opencode serve --hostname 127.0.0.1 --port 4096`, but remove DeepSeek-specific exports and remove the hardcoded model from the default message request. The example request relies on the resolved OpenCode config. Add a short note that a benchmark may use the native explicit request override only when the case intentionally pins a model.

- [ ] **Step 4: Verify commands and secret hygiene**

Run:

```bash
rg -n "DEEPSEEK_API_KEY|只支持.*DeepSeek|配置 DeepSeek" README.md
rg -n "MODEL|APIKEY|URL|opencode.json|opencode serve|opencode /data/repos/target-project" README.md
git diff --check -- README.md
```

Expected: the first command has no provider-runtime matches in the OpenCode setup section; attribution may still show DeepSeek as one optional Anthropic-compatible judging example. The second command finds both native configuration and both run modes. `git diff --check` prints nothing.

- [ ] **Step 5: Commit README changes**

```bash
git add README.md
git commit -m "docs: document native providers and interactive traces"
```

---

### Task 6: Full Verification and Release Publication

**Files:**
- Modify only if verification finds a defect in files already listed above.

**Interfaces:**
- Consumes: all implementation tasks.
- Produces: verified branch, pushed commit history, observable release tag, and triggered release workflow.

- [ ] **Step 1: Run focused tests**

```bash
bun --cwd packages/opencode test test/config/config.test.ts test/observability/case-trace-session.test.ts test/observability/case-trace.test.ts test/observability/trace-publication.test.ts
```

Expected: all tests PASS.

- [ ] **Step 2: Run type and formatting checks**

```bash
bun run --cwd packages/opencode typecheck
git diff --check HEAD~5..HEAD
```

Expected: typecheck exits 0 and `git diff --check` prints nothing.

- [ ] **Step 3: Verify passive behavior and source invariants**

```bash
rg -n "process\.env\.(MODEL|APIKEY|URL|DEEPSEEK_API_KEY)" packages/opencode/src
rg -n "CaseTrace\.get\(\)\?\.startSpan|CaseTrace\.get\(\)\?\.event" packages/opencode/src
rg -n "CaseTrace\.finishAll" packages/opencode/src/cli/cmd/tui/worker.ts packages/opencode/src/observability/case-trace.ts
```

Expected: no generic model-variable reads in production source, no direct global span/event dereferences, and `finishAll` appears in both the worker and public Trace API.

- [ ] **Step 4: Inspect repository state and staged scope**

```bash
git status --short
git log --oneline -8
```

Expected: only the pre-existing untracked benchmark report files remain; no credential, runtime Trace, cache, or generated benchmark artifact is committed.

- [ ] **Step 5: Push and trigger the release workflow**

Push `codex/message-context-lineage`, fetch remote tags, compute the next unused observable serial from the highest existing tag, and monitor the existing `release observable` workflow until completion.

```bash
git push origin codex/message-context-lineage
git fetch --tags origin
LATEST="$(git tag --list 'v1.14.48-observable.*' --sort=-v:refname | head -1)"
SERIAL="${LATEST##*.}"
TAG="v1.14.48-observable.$((SERIAL + 1))"
git tag "$TAG"
git push origin "$TAG"
gh run list --workflow "release observable" --limit 3
```

Expected: the branch and computed tag push successfully; the workflow publishes Linux and macOS binaries plus `SHA256SUMS`. Abort instead of overwriting if `git rev-parse "$TAG"` unexpectedly resolves before tag creation.

- [ ] **Step 6: Verify release assets**

```bash
gh release view "$TAG" --json url,assets
```

Expected assets include `opencode-observable-linux-x64`, supported macOS binaries, and `SHA256SUMS`; report the release and workflow URLs.
