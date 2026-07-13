# Loop 3 Revision-aware Trace + Attribution Hydration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让语义 trace 能区分历史与当前仓库状态，让离线归因模块读取完整 artifact 并基于原子 claim 做可靠后向分析，同时用复杂 benchmark fixtures 验证端到端效果。

**Architecture:** TypeScript recorder 在 case 结束后构建 revision-aware 语义事实层；Python attribution loader 读取 trace 和 artifact，按 claim 与时态排序上游；stress review 将机制覆盖、真实结果和预设故障拆分。所有派生信息保持被动、事后、不可反馈给 agent。

**Tech Stack:** TypeScript、Bun test、Node.js test runner、Python 3 unittest、Anthropic Python SDK、DeepSeek Anthropic-compatible API。

## Global Constraints

- Trace 插装不得改变 agent prompt、上下文、工具输入输出、任务循环和模型行为。
- 离线归因结果不得反馈给 agent。
- 大文本保存在 artifact，语义 trace 只保留结构化摘要和引用。
- Stress case 必须通过 `opencode serve -> session -> HTTP request` 执行。
- 保留当前工作树中已完成但未提交的 Loop 2 修改。

---

### Task 1: Revision-aware Change and Verification

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `repository_revision`, `revision_before`, `revision_after`, `verification_phase`, `effective_for_final_state`, `supersedes_refs`, `parsed_command_outcomes`, `exit_masked_by_shell`。

- [ ] **Step 1:** 添加失败测试，构造 baseline failed verification、production change、post-change passed verification，断言 revision 和 supersede 关系。
- [ ] **Step 2:** 运行 focused Bun test，确认新断言失败。
- [ ] **Step 3:** 在 `ActiveCaseTrace` 中维护仅供 recorder 使用的 revision 计数，并在 change/verification 记录中派生时态字段。
- [ ] **Step 4:** 添加组合 shell 命令测试，断言 shell exit 0 但输出 `---EXIT: 1` 时记录 `exit_masked_by_shell=true` 和子命令失败。
- [ ] **Step 5:** 实现 shell outcome 解析与最终状态 verification 选择。
- [ ] **Step 6:** 运行 focused Bun test 和 TypeScript typecheck。

### Task 2: Atomic Claims and Direct Support

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: Task 1 revision-aware verification。
- Produces: atomic `response.claim` records with `claim_kind`, `temporal_scope`, `repository_revision`, `direct_support_refs`, `candidate_context_refs`, `superseded_evidence_refs`。

- [ ] **Step 1:** 添加失败测试，输入“全部通过。\n\n## 测试脚本说明”和 Markdown 表格，断言首句成为 claim、标题和表头不成为 claim。
- [ ] **Step 2:** 添加失败测试，断言“全部通过”只直接绑定 post-change passed verification，baseline failure 进入 superseded refs。
- [ ] **Step 3:** 修改 claim 分段、分类和证据匹配逻辑。
- [ ] **Step 4:** 运行 focused Bun test。

### Task 3: Semantic Record Convergence

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: one canonical semantic tool span per `call_id`; meaningful semantic facts only。

- [ ] **Step 1:** 添加失败测试，断言相同 call_id 的 tool lifecycle/execute 事件在最终 `trace.json` 中只产生一个 canonical tool record。
- [ ] **Step 2:** 添加失败测试，断言 path-only directory listing 只产生 observation，不产生 semantic fact。
- [ ] **Step 3:** 实现 canonical tool merge、path-only/generic fact gate 和 no-op compaction check 聚合。
- [ ] **Step 4:** 运行 focused Bun test 与 semantic contract tests。

### Task 4: Case Lifecycle Semantics

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `case_status`, `shutdown_disposition`, `shutdown_signal` without conflating completed cases with cancellation。

- [ ] **Step 1:** 添加 SIGINT-after-success 和 SIGTERM-before-completion 两个失败测试。
- [ ] **Step 2:** 实现 lifecycle normalization。
- [ ] **Step 3:** 运行 focused tests。

### Task 5: Artifact-aware Attribution Graph

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: hydrated compact nodes, artifact expansion metadata, revision-aware upstream ordering。

- [ ] **Step 1:** 添加 fixture trace：tool result preview 截断、完整内容位于 artifact，断言 graph 能展开与 claim 相关片段。
- [ ] **Step 2:** 添加 revision ordering 测试，断言当前 revision verification 排在 historical verification 前。
- [ ] **Step 3:** 实现 artifact index、受预算控制的 hydration 和 prompt metadata。
- [ ] **Step 4:** 运行 Python unit tests。

### Task 6: Attribution Starts and Inconclusive Feedback

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/trace_improvement.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: atomic claims and artifact-aware graph。
- Produces: claim-first starts and mandatory blocking gaps for unknown judgments。

- [ ] **Step 1:** 添加失败测试，断言高价值 final claims 优先于整段 response.output。
- [ ] **Step 2:** 添加失败测试，断言任意 unknown/inconclusive 至少生成一个 blocking gap。
- [ ] **Step 3:** 实现 start ranking、temporal guidance 和 feedback classification。
- [ ] **Step 4:** 运行 Python unit tests。

### Task 7: Stress Review Separation

**Files:**
- Modify: `packages/opencode/test/observability/stress-cases/lib/stress-review.mjs`
- Modify: `packages/opencode/test/observability/stress-cases/stress-case.schema.json`
- Modify: `packages/opencode/test/observability/stress-cases/cases.json`
- Test: `packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`

**Interfaces:**
- Produces: `mechanism_coverage`, `actual_case_outcome`, `designed_failure_target`。

- [ ] **Step 1:** 添加失败测试，证明字段齐全但实际修改正确时不能报告预设 root cause。
- [ ] **Step 2:** 添加 pass/fail/unknown acceptance assertion tests。
- [ ] **Step 3:** 修改 review JSON 与 summary Markdown。
- [ ] **Step 4:** 运行 Node stress review tests。

### Task 8: Open-source FeatureBench Adapter

**Files:**
- Create: `packages/opencode/test/observability/benchmark-cases/open-source-benchmarks.json`
- Create: `packages/opencode/test/observability/benchmark-cases/run-featurebench-cases.mjs`
- Create: `packages/opencode/test/observability/benchmark-cases/featurebench-cases.test.mjs`

**Interfaces:**
- Consumes: FeatureBench fast/lite instance metadata、上游 base commit、现有 HTTP session runner。
- Produces: at least three real open-source benchmark traces and official evaluation metadata when available。

- [ ] **Step 1:** 固定 FeatureBench release/commit 和 split，下载元数据后按多文件、跨模块、F2P/P2P 条件选择 3 至 4 个 instance id。
- [ ] **Step 2:** 添加失败测试，验证 adapter 不读取 gold patch、不把本地 questions 当实例、且必须记录 source/version/base commit。
- [ ] **Step 3:** 实现上游仓库 checkout、HTTP session 请求、patch/diff 收集和 trace 目录映射。
- [ ] **Step 4:** 接入 FeatureBench 官方 harness；无法在 arm64 执行时显式记录 `evaluation_status=not_run` 和原因。
- [ ] **Step 5:** 添加 source manifest 和 runner tests。
- [ ] **Step 6:** 运行 Node tests。

### Task 9: End-to-end Verification

**Files:**
- Create: `docs/superpowers/reports/2026-07-13-loop3-revision-aware-trace-attribution-review.md`

**Interfaces:**
- Produces: regression evidence, HTTP benchmark traces, human/offline comparison and next-loop findings。

- [ ] **Step 1:** 运行全部 Python attribution tests、Node stress tests、focused Bun tests 和 TypeScript typecheck。
- [ ] **Step 2:** 使用本地构建或最新 macOS release，通过 HTTP 执行旧 stress 回归和至少三个 FeatureBench 真实实例。
- [ ] **Step 3:** 对失败或低质量 case 做人工后向语义污点分析。
- [ ] **Step 4:** 使用 DeepSeek v4-pro 和 1h timeout 运行离线归因模块。
- [ ] **Step 5:** 对比人工与模块结果并保存报告。
