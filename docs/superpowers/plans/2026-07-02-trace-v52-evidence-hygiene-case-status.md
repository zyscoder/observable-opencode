# Trace v5.2 Evidence Hygiene + Case Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收敛 observable-opencode trace 的事实层和展示层，让离线归因优先消费高价值语义事实，而不是被任务进度、宽泛上下文引用和过长子任务引用干扰。

**Architecture:** 在 `case-trace.ts` 中保持原始事件完整保存，但将可归因事实拆成更精确的正式 record：`evidence.semantic_fact` 承载可证明事实，`task.plan_state` 承载 todo/计划进度，`execution.observation` 承载普通工具观察。`trace.html` 默认突出 claim -> evidence -> tool/context/subagent 的因果链，routine details 进入折叠区或 artifact refs。

**Tech Stack:** TypeScript, Bun test, opencode observability trace bundle, static HTML viewer.

## Global Constraints

- 版本升级到 `TRACE_VERSION = "5.2"`。
- 不恢复 `viewer.html`，单一 HTML 入口仍为 `trace.html`。
- 原始事件仍进入 `events.jsonl` / `raw-events.jsonl`，但 provenance `records` 只保留有语义的正式 record。
- 不做自动根因诊断，只提供离线归因可消费的事实、引用、摘要和质量指标。
- 大文本继续写 artifacts，HTML 可以查看 artifact refs。

---

### Task 1: Contract And Tests

**Files:**

- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes: existing `CaseTrace.evidenceFact`, `CaseTrace.observation`, `CaseTrace.subagent`, `CaseTrace.compaction`, `CaseTrace.finish`.
- Produces: expected event types `evidence.semantic_fact`, `execution.observation`, `task.plan_state`, `case.completed`.

- [ ] **Step 1: Add failing tests for trace version and case status**

Add a test that finishes with `status: "cancelled"` but includes a completed final response. Expect `manifest.status === "cancelled"`, `manifest.case_status === "success"`, and a `case.completed` record.

- [ ] **Step 2: Add failing tests for evidence hygiene**

Create a todowrite-like evidence input and a read/MCP evidence input. Expect todowrite to emit `task.plan_state`, while read/MCP emits `evidence.semantic_fact`; `evidence.fact` should not be emitted for todowrite.

- [ ] **Step 3: Add failing tests for claim refs**

Expect `response.claim.source_refs` to be narrow direct refs and broad refs to live in `data.legacy_context_refs`; health should not count every claim as polluted by legacy refs.

- [ ] **Step 4: Add failing tests for subagent and compaction summaries**

Expect subagent output to include `child_timeline_summary`, `child_metric_summary`, `child_key_evidence_refs`, `child_trace_artifact_ref`, and limited inline refs. Expect compaction to include `retention_ratio`, retained/dropped fact counts, and `compression_loss_risks`.

### Task 2: Case Status And Event Contract

**Files:**

- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**

- Produces: `TraceManifest.case_status`, `TraceManifest.server_status`, `TraceManifest.case_completed_at`, and formal `case.completed` / `case.failed` records.

- [ ] **Step 1: Extend trace types**

Add optional manifest fields for server/process status versus case status.

- [ ] **Step 2: Infer case status**

When final user-visible response exists and no error is recorded, set `case_status` to `success` even if server shutdown status is `cancelled`.

- [ ] **Step 3: Emit case lifecycle record**

Append `case.completed` or `case.failed` before writing final trace.

### Task 3: Evidence Hygiene

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`

**Interfaces:**

- Produces: `evidence.semantic_fact`, `execution.observation`, `task.plan_state`.
- Keeps: backward-compatible source refs and artifacts in raw events.

- [ ] **Step 1: Classify evidence records**

Route todowrite/todo progress to `task.plan_state`, generic tool output to `execution.observation`, and direct factual MCP/read/verification facts to `evidence.semantic_fact`.

- [ ] **Step 2: Update metrics**

Rename health counters around semantic evidence and keep `duplicate_semantic_facts` separate from low-value observations.

- [ ] **Step 3: Update viewer sections**

Rename Evidence Facts to Semantic Evidence and add separate Plan State / Execution Observations summaries where useful.

### Task 4: Claim Attribution Cleanup

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`

**Interfaces:**

- Produces: `data.direct_evidence_refs`, `data.legacy_context_refs`, `data.context_refs`, `data.execution_refs`, `data.attribution_summary`.

- [ ] **Step 1: Narrow record source refs**

Set `response.claim.source_refs` to direct evidence first, then bounded execution/context refs only when no direct evidence exists.

- [ ] **Step 2: Preserve legacy refs only in data**

Keep broad refs in `data.legacy_context_refs` and show them folded in HTML.

- [ ] **Step 3: Add attribution summary**

Add counts and support level summary for each claim.

### Task 5: Subagent And Compaction Summaries

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`

**Interfaces:**

- Produces: `child_timeline_summary`, `child_metric_summary`, `child_key_evidence_refs`, `child_trace_artifact_ref`, `retention_ratio`, `retained_fact_count`, `dropped_fact_count`, `compression_loss_risks`.

- [ ] **Step 1: Summarize child trace refs**

Keep only a small inline sample and write full refs to artifact.

- [ ] **Step 2: Add compaction quality fields**

Calculate retention ratio and fact counts; derive risk flags from dropped facts, missing estimates, empty summary, and auto-continue.

### Task 6: Documentation And Verification

**Files:**

- Modify: `docs/observable-benchmark-trace.md`

**Interfaces:**

- Produces: updated v5.2 usage guidance for offline attribution.

- [ ] **Step 1: Update spec**

Document case/server status split, semantic evidence classes, claim attribution fields, subagent summaries, and compaction risk fields.

- [ ] **Step 2: Verify**

Run formatting, type checking, and available tests. If Bun is not installed locally, record that limitation and run static checks that are available.
