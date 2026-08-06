# Causal Judgment Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为离线节点缺陷判断提供保留边语义、缺陷身份、下游判断和 Episode 状态的结构化因果上下文。

**Architecture:** `TraceGraph` 保存标准化边索引；独立 `judgment_context.py` 从图、当前后向路径和既有判断构造纯离线上下文；Analyzer 对支持新接口的 Judge 注入上下文，Claude prompt 消费它，旧 Judge 保持兼容。

**Tech Stack:** Python 3、dataclasses、unittest、Anthropic SDK 兼容接口。

## Global Constraints

- 不改变 Agent、Trace 采集和 benchmark case 执行行为。
- 不把离线归因结果反馈给 Agent。
- 根因确认保持 node-local。
- 不引入新的第三方依赖。

---

### Task 1: Preserve Causal Edge Semantics

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `TraceGraph.incoming_edge_context(ref, allowed_refs=None)` 和 `TraceGraph.edge_context(from_ref, to_ref)`。

- [ ] 写失败测试，断言 dataflow/message-lineage/source-ref 边保留 relation、evidence_type、confidence、inference_method 和 edge_origin。
- [ ] 运行定向测试并确认因缺少边上下文接口失败。
- [ ] 实现标准化边索引和只读查询接口，对重复边稳定去重。
- [ ] 运行定向测试并确认通过。

### Task 2: Build Structured Judgment Context

**Files:**
- Create: `tools/trace_attribution/trace_attribution/judgment_context.py`
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `build_causal_judgment_context(graph, node_ref, path, judgments, episode_index, objective) -> JsonDict`。

- [ ] 写失败测试，覆盖 active defect 指纹、active-path 出边、下游 judgment、Causal Episode 和 Progress Episode。
- [ ] 运行定向测试并确认模块或字段缺失。
- [ ] 实现确定性上下文构造和紧凑节点/判断摘要。
- [ ] 运行定向测试并确认通过。

### Task 3: Inject Context Without Breaking Existing Judges

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: `build_causal_judgment_context(...)`。
- Produces: 可选 `judge_node_with_context(..., judgment_context)`；原 `judge_node(...)` 不变。

- [ ] 写失败测试，断言 context-aware Judge 收到结构化上下文，legacy Judge 仍被原接口调用。
- [ ] 写失败 prompt 测试，断言 `causal_judgment_context` 和边关系进入 JSON。
- [ ] 实现 Analyzer 兼容分派和 Claude 上下文接口。
- [ ] 运行定向测试并确认通过。

### Task 4: Regression and Benchmark Review

**Files:**
- Modify: `tools/trace_attribution/README.md`
- Create: `docs/superpowers/reports/2026-07-17-causal-judgment-context-results.md`

**Interfaces:**
- Consumes: 完成后的归因模块和已有 Trace。
- Produces: 单测结果、旧 Trace 复跑结果、错误根因/unknown/请求量对比。

- [ ] 运行完整 `test_backward_taint.py` 测试集。
- [ ] 对已有 Terminal、Pydantic、Sphinx Trace 运行离线归因。
- [ ] 比较人工根因与模型根因，记录 exact-root、false-root、unknown 和 Judge request count。
- [ ] 更新 README 和结果报告，提炼下一轮是否需要拆分节点 Judge 与边 Judge。
