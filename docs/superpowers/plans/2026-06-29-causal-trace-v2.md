# Causal Trace v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the benchmark trace surface with a causal trace bundle that supports evidence-chain attribution across context, LLM, tools, MCP, skills, subagents, compaction, changes, verification, and final claims.

**Architecture:** Extend the existing `CaseTrace` writer into a Causal Trace v2 writer that emits manifest, causal graph, semantic records, raw events, partial snapshots, artifact-deduped content, and a static viewer. Existing instrumentation call sites are mapped into causal nodes and edges, with targeted additions for MCP returned content, skill content, subagent result evidence, and compaction semantics.

**Tech Stack:** TypeScript, Bun test, existing observable-opencode trace utilities, static HTML/CSS/JS viewer, filesystem artifacts.

## Global Constraints

- Work on branch `codex/causal-trace-v2`.
- Treat Causal Trace v2 outputs as the primary contract.
- Do not preserve old trace directory or schema as a hard requirement.
- Do not perform automatic root-cause diagnosis.
- Do not write API keys, bearer tokens, cookies, or credential-like strings to trace outputs.

---

### Task 1: Causal Bundle Writer

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `manifest.json`, `causal-trace.json`, `records.jsonl`, `raw-events.jsonl`, `partial/latest.json`, `viewer.html`
- Produces: `CaseTrace.node(input)`, `CaseTrace.causalEdge(input)`, `CaseTrace.observation(input)`, `CaseTrace.compaction(input)`

- [ ] **Step 1: Write failing tests**
  - Add a test that starts a trace, records one event and one final claim, finalizes, and expects Causal Trace v2 bundle files.
  - Add a test that repeated large payloads produce one artifact by content hash.

- [ ] **Step 2: Verify RED**
  - Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`
  - Expected: FAIL because new bundle files and APIs do not exist.

- [ ] **Step 3: Implement writer**
  - Add v2 node/edge/artifact/index types.
  - Write semantic records to `records.jsonl`.
  - Write low-level events to `raw-events.jsonl`.
  - Write manifest and causal trace on finish.
  - Add artifact dedupe under `artifacts/sha256`.

- [ ] **Step 4: Verify GREEN**
  - Run the same test file.
  - Expected: new tests pass.

### Task 2: Finalizer and Partial Snapshots

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: signal-safe finalization for `SIGINT`, `SIGTERM`, `SIGHUP`, `uncaughtException`, and `unhandledRejection`
- Produces: `partial/latest.json`

- [ ] **Step 1: Write failing test**
  - Spawn a traced child process, send `SIGINT`, and expect `manifest.json`, `causal-trace.json`, `viewer.html`, and `partial/latest.json`.

- [ ] **Step 2: Verify RED**
  - Run observability test file.
  - Expected: FAIL because SIGINT does not finalize v2 outputs.

- [ ] **Step 3: Implement finalizer**
  - Install signal and error handlers once.
  - Finish active trace as `cancelled` for signals and `error` for thrown/rejected errors.
  - Refresh partial snapshot during writes and finalization.

- [ ] **Step 4: Verify GREEN**
  - Run observability test file.
  - Expected: signal finalization test passes.

### Task 3: Component Semantic Nodes

**Files:**
- Modify: `packages/opencode/src/tool/tool.ts`
- Modify: `packages/opencode/src/mcp/index.ts`
- Modify: `packages/opencode/src/tool/task.ts`
- Modify: skill load instrumentation file identified by `rg "component: \"skill\""`
- Modify: `packages/opencode/src/session/compaction.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `observation` nodes for tool/MCP/skill/subagent outputs
- Produces: `context.compaction` nodes for compaction records

- [ ] **Step 1: Write failing tests**
  - Add tests that call `CaseTrace.observation` and `CaseTrace.compaction` directly and verify graph nodes, edges, and artifacts.
  - Add a small MCP-like record test that ensures returned content is preserved in artifact-backed observations.

- [ ] **Step 2: Verify RED**
  - Run observability test file.
  - Expected: FAIL because new semantic records are not implemented.

- [ ] **Step 3: Implement call-site mapping**
  - Tool: convert outputs into observation/change/verification nodes.
  - MCP: store `result.content` summary/artifact and observation node.
  - Task: store child session id, result observation, and parent-child edge.
  - Skill: store skill instructions as artifact-backed observation.
  - Compaction: store trigger, budget, selection, previous summary, serialized tail, summary, and auto-continue metadata.

- [ ] **Step 4: Verify GREEN**
  - Run observability test file.
  - Expected: component semantic tests pass.

### Task 4: Static Causal Viewer

**Files:**
- Create: `packages/opencode/src/observability/causal-trace-viewer.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `viewer.html` with Causal Graph, Timeline, Evidence Inspector, and Context Analyzer sections.

- [ ] **Step 1: Write failing test**
  - Render a Causal Trace v2 fixture and assert the viewer contains the four required sections and linked artifact metadata.

- [ ] **Step 2: Verify RED**
  - Run observability test file.
  - Expected: FAIL because new viewer module/sections do not exist.

- [ ] **Step 3: Implement viewer**
  - Build static HTML renderer.
  - Collapse low-value records by default.
  - Show nodes, edges, evidence refs, artifact previews, and context/compaction details.

- [ ] **Step 4: Verify GREEN**
  - Run observability test file.
  - Expected: viewer tests pass.

### Task 5: Docs and End-to-End Verification

**Files:**
- Modify: `docs/observable-benchmark-trace.md`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: updated usage documentation for Causal Trace v2 bundle.

- [ ] **Step 1: Update docs**
  - Document the new bundle layout, env vars, and viewer workflow.

- [ ] **Step 2: Run focused tests**
  - Run: `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`
  - Expected: all observability tests pass.

- [ ] **Step 3: Run typecheck**
  - Run: `bun run --cwd packages/opencode typecheck`
  - Expected: TypeScript check exits 0.

- [ ] **Step 4: Inspect git diff**
  - Run: `git diff --stat`
  - Expected: changes are limited to observability, targeted instrumentation, tests, and docs.

