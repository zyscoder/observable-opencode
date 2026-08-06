# Resumable Judge Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为离线语义归因增加内容寻址判断缓存、Provider 熔断和可恢复重跑，并补齐 Progress 导航窗口的显式成员边。

**Architecture:** `cache.py` 独立负责 JSONL checkpoint 和请求语义哈希；`ClaudeJudgeClient` 在 schema 校验后读写缓存并维护连续 Provider 错误状态；`BackwardTaintAnalyzer` 在熔断时立即结束当前分支；CLI 默认把 checkpoint 放在归因输出旁边。Progress 窗口边继续由纯离线 context builder 投影，不修改原始 Trace。

**Tech Stack:** Python 3、dataclasses、JSONL、SHA-256、unittest、Anthropic-compatible SDK。

## Global Constraints

- 不改变 Agent、LLM、工具、MCP、Skill、Trace 采集或 benchmark case 执行行为。
- 不缓存 API key、完整 prompt 或新增的私域内容副本。
- 只缓存通过结构校验的模型判断；Provider 失败和 schema repair exhausted 不缓存。
- 缓存 key 必须覆盖完整请求语义和 prompt schema version。
- 不引入新的第三方依赖。

---

### Task 1: Durable Content-Addressed Judgment Cache

**Files:**
- Create: `tools/trace_attribution/trace_attribution/cache.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `JudgmentCache(path)`, `build_judge_cache_key(...)`, `get(...)`, `put(...)`, `stats()`。
- Consumes: 已验证的 `NodeJudgment` 和完整请求语义。

- [ ] 写失败测试：相同请求第二次命中缓存且 Provider 只调用一次。
- [ ] 写失败测试：objective/context/model/prompt version 改变时 cache miss。
- [ ] 写失败测试：损坏 JSONL 尾行被跳过并计入 corrupt count。
- [ ] 实现 JSONL 加载、SHA-256 key、append/flush/fsync 和 judgment 反序列化。
- [ ] 在 node judgment 与 root confirmation 的成功路径接入缓存，schema exhausted 不写缓存。
- [ ] 运行定向测试并确认通过。

### Task 2: Provider Circuit Breaker and Branch Termination

**Files:**
- Create: `tools/trace_attribution/trace_attribution/errors.py`
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `JudgeProviderError`, `JudgeProviderUnavailable`；Judge 暴露 circuit 状态和连续错误计数。
- Consumes: worker 返回的 `error_type` 和错误文本。

- [ ] 写失败测试：成功请求清零连续错误计数。
- [ ] 写失败测试：连续 3 次连接错误打开熔断器，第 4 个节点不再调用 Provider。
- [ ] 写失败测试：Analyzer termination reason 为 `provider_unavailable`，未访问节点不生成 fallback unknown。
- [ ] 保留 worker 错误类型，并只把连接/超时类错误计入熔断。
- [ ] Analyzer 捕获熔断异常后保存 unresolved ref 并停止当前分支。
- [ ] 运行定向测试并确认通过。

### Task 3: CLI Resume and Report Metrics

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/README.md`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: `--judge-cache`、`--provider-error-threshold` 和默认 `<out-stem>.judge-cache.jsonl`。
- Produces: 报告 metadata 中的 `judge_cache` 与 `provider_circuit`。

- [ ] 写失败 CLI 测试，验证默认和显式 cache 路径。
- [ ] 写失败报告测试，验证 hits/misses/writes/corrupt entries 和 circuit 状态。
- [ ] 接入 CLI 参数和默认路径，不改变原有必填参数。
- [ ] 在 AttributionReport metadata 投影 Judge cache/circuit 指标。
- [ ] 更新 README 的运行与 resume 示例。
- [ ] 运行定向测试并确认通过。

### Task 4: Explicit Progress Window Membership Edge

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/judgment_context.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Produces: active path 上的 `progress_window_member` 离线边。

- [ ] 写失败测试：非直属微 Episode 的 candidate 连接到窗口 anchor 时关系为 `progress_window_member`。
- [ ] 实现基于 `progress_navigation_window` 的显式边投影。
- [ ] 验证直属微 Episode 仍保留 `progress_episode_member`。
- [ ] 运行定向测试并确认通过。

### Task 5: Regression and Resume Benchmark

**Files:**
- Create: `docs/superpowers/reports/2026-07-20-resumable-judge-attribution-results.md`

**Interfaces:**
- Consumes: Sphinx、Pydantic 和成功负样本现有 Trace。
- Produces: 根因命中、cache 命中、请求量、Provider 失败、unknown 和假阳性对比。

- [ ] 运行完整 `python3 -m unittest tests.test_backward_taint`。
- [ ] 使用已有 Sphinx checkpoint 或新 checkpoint 跑一次受控中断，再 resume 完成相同分支。
- [ ] 验证 `dec_85` 的实际 Judge 结果及 `progress_window_member` 上下文。
- [ ] 回归 Pydantic 人工根因和一个成功负样本。
- [ ] 记录与上一轮 196 requests / 81 errors / no root 的对照数据。
- [ ] 判断下一轮是否需要窗口级 LLM 候选筛选或拆分 node/edge Judge。
