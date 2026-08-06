# Responsibility-aware Omission 与 Episode-diverse Reserve 实施计划

## Task 1：恢复义务事实模型

**文件**

- 新建 `tools/trace_attribution/trace_attribution/restoration_obligation.py`
- 新建 `tools/trace_attribution/tests/test_restoration_obligation.py`

**TDD**

1. 测试严格 schema、稳定 identity、round-trip 和未知字段拒绝。
2. 测试恢复义务只能引用当前 Trace 中可解析的事实。
3. 测试 `offline_judge_only` 可见性不可被改写。
4. 实现不可变 `RestorationObligation` 及集合规范化。

## Task 2：Evidence Capsule v8

**文件**

- 修改 `evidence_capsule.py`
- 修改 `test_evidence_capsule.py`

**TDD**

1. 候选 capsule 携带适用义务、episode 和 phase 事实。
2. capsule identity 覆盖新增事实。
3. 义务引用断链时拒绝构造或验证。
4. v7 保持只读识别，但不得冒充 v8。

## Task 3：Global Judge v11

**文件**

- 修改 `global_judge.py`
- 修改 `causal_state.py`
- 修改 `test_global_judge.py`
- 修改 `test_causal_state.py`

**TDD**

1. `positive_introduction` 保持既有输入/输出和反事实约束。
2. 完整责任事实下允许 present-to-present 的 `responsible_omission`。
3. 缺少责任、义务、关闭边界或 preventing counterfactual 时拒绝遗漏根候选。
4. 修复窗口仍开放时只能判为 `ordinary_non_repair`。
5. present-to-present contributor 缺少 contribution mechanism 时拒绝。
6. comparison contract v2 逐项声明两类根因的必要条件。
7. v10 checkpoint 不得静默迁移为 v11。

## Task 4：递归分析贯通

**文件**

- 修改 `recursive_analyzer.py`
- 修改 `causal_judge.py`
- 修改 `checkpoint.py`
- 修改对应测试

**TDD**

1. active defect、恢复义务和 capsule v8 一起进入请求身份。
2. repair prompt 只能修复 v11 结构，不得弱化责任或机制约束。
3. 页面 checkpoint 持久化义务和 comparison contract v2。
4. replay 时事实或协议变化必须拒绝旧页面。

## Task 5：Episode-diverse Reserve

**文件**

- 新建 `trace_attribution/candidate_episode.py`
- 修改 `candidate_budget.py`
- 修改 `recursive_analyzer.py`
- 新建 `tests/test_candidate_episode.py`
- 修改 `test_candidate_budget.py`
- 修改 `test_recursive_analyzer.py`

**TDD**

1. 每个 materialization episode 保留最早计划和最晚执行代表。
2. 多 episode 在 256 上限内 round-robin，不被单个大 episode 挤占。
3. 无 episode 节点进入稳定 fallback bucket。
4. 审计输出包含 episode key、role、reserve rank 和 disposition。
5. Pydantic 的早期计划 `dec_155` 和晚期节点 `dec_327` 同时进入评审池。
6. Seaborn 的四个人工根因候选全部被覆盖；标签只用于事后检查。

## Task 6：严格评估器规范化

**文件**

- 修改 `scripts/evaluate_recursive_attribution.py`
- 修改对应 evaluator 测试

**TDD**

1. raw open-source `partial/latest.json` 先经过 `TraceGraph` 规范化。
2. CLI 和 evaluator 对同一输入产生相同 canonical refs。
3. 非规范 artifact hash 不再造成 source ref 误报。

## Task 7：离线行为隔离

**文件**

- 修改或新增 attribution integration tests

**TDD**

1. 开启/关闭归因时比较 Agent message、provider request、tool I/O 和 final output hash。
2. 恢复义务与归因结论不得出现在 Agent 可见上下文。
3. 所有新增 journal 事件声明无运行时行为影响。

## Task 8：完整验证与真实轨迹

1. 运行新增聚焦测试。
2. 运行 `tools/trace_attribution/tests` 全量测试。
3. 重放 Pydantic、Seaborn 和 Sphinx 公开 benchmark Trace。
4. 对比人工分析与离线模块：
   - root candidate recall；
   - omission root confirmation；
   - contributor precision；
   - candidate reduction ratio；
   - page completion 与请求预算；
   - inconclusive 的证据缺口是否准确。
5. 更新本轮结果报告，记录仍缺失和冗余的语义事实。
