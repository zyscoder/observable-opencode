# Quality-first Global Judge 分页迭代结果

## 本轮目标

本轮验证 Global Judge 在大 Trace 中能否做到：

1. 完整发现具有事实因果路径的候选；
2. 在有限 LLM 评审预算中保留高价值 authored decisions；
3. 分页判断时区分“本页无根因”和“观察到的缺陷不存在”；
4. 通过真实 benchmark 暴露候选召回之后的下一层归因瓶颈。

所有处理均为离线、被动分析，`behavior_impact` 为
`none_offline_analysis_only`，不会改变或反馈到 Agent 执行。

## 已完成修复

### 1. 失效评审引用重绑定

历史 review 的 `record_refs` 在当前 Trace revision 中全部失效时，离线
评审节点会绑定到当前最终结果事实，并记录
`review-observed-defect-source-binding/v1` 审计。重绑定不读取人工根因标签。

### 2. 页面级无根因语义

新增 `no_root_candidates`，表达“active defect 仍成立，但本页没有支持的
根因”。`no_defect` 仅用于决定性事实推翻 active defect。Global Candidate
Judgment 持久化协议为 `global-candidate-judgment/v10`。

### 3. 发现与评审预算解耦

- grounded closure 节点上限：`256 -> 512`
- factual edge 扫描上限：`4096 -> 8192`
- LLM assessment 上限：仍为 `256`
- grounded decision reserve：仍为 `64`

预算器开始保留调用方提供的 grounded decision 质量顺序，当前顺序按 Trace
位置从最终结果边界向前排列。持久化校验器验证该顺序对应的集合、资格、
类别、reserve 和 offered 投影，但不再错误地强制恢复为发现顺序。

## 自动化验证

新增或收紧的测试覆盖：

- 显式 grounded decision 质量顺序不能被集合化；
- 超过 300 个浅层节点和 64 个决策时，晚期事实决策仍被发现和保留；
- 校验器可重放非发现顺序的 grounded reserve；
- `no_root_candidates` 不会推翻 active defect；
- 新旧 checkpoint、分页、候选 capsule 与全局判断协议兼容。

全量结果：

```text
Ran 1302 tests in 60.459s
OK
```

## 真实 Trace 预检

### Pydantic FeatureBench

Trace：

`pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1`

单 seed `missing_model_rebuild_dependencies`：

- discovered：537
- offered：256
- dropped：281
- 人工标签 `record:decisionnode_dec_327_a0b88b89`
  - disposition：offered
  - offered rank：4
  - grounded path：7 个节点

修复前该节点因 256 节点 closure 上限未进入候选审计；修复后进入第二个
Judge 页面。

### Seaborn FeatureBench

三个 observed-defect seed 均发现 545 个候选、提供 256 个 assessment
候选。四个人工根因在三个 seed 中全部 offered，且均具有 7 至 8 节点事实
路径。当前质量顺序偏向晚期决策：

- 两个晚期根因 rank 为 3、8；
- 两个较早实现根因 rank 为 242、251。

召回完整，但较早根因要到第 61、63 页才被判断，说明全局“最新优先”仍
需要被 episode-diverse 排序替代。

## Pydantic 真实模型运行

产物：

`.benchmark-runs/global-pagination-20260730/pydantic-v10/recursive.attribution.json`

模型：`deepseek-v4-pro`，Anthropic-compatible DeepSeek endpoint。

运行到第 10 个完成页面后主动发送 SIGINT，CLI 正常生成 attribution
report 和 checkpoint：

- completed pages：10
- failed pages：0
- physical Judge requests：18
- page outcomes：10 个 `no_root_candidates`
- selected roots：0
- report termination：`signal_interrupted`

停止原因不是超时或协议错误，而是发现了结构上不可满足的根因条件。

严格离线 evaluator 未对该产物生成 acceptance metrics。原因是 evaluator
直接验证开源归档的 `partial/latest.json`，拒绝其中的短 artifact hash 和
未 canonicalize 的 source refs；归因 CLI 则先通过 `TraceGraph` 重建和
规范化后再分析。后续应让 evaluator 复用同一规范化入口。当前报告中的
候选覆盖、页面和请求数据均来自已签名 funnel 与 checkpoint，不将 evaluator
失败计为通过。

## 新暴露的归因瓶颈

### 1. 现行协议无法表达“应修未修”的根因

该 seed 明确记录 `rebuild_model_fields` 和 `set_model_mocks` 已在 masked
baseline 中缺失。因此所有 Agent 节点的 `input_defect_status` 都是
`present`。

当前 `_validate_assessment_counterfactual_consistency()` 强制：

```text
root_candidate => input_defect_status != present
```

所以无论 Agent 的计划、范围判断或实现决策承担了多强的恢复责任，都无法
成为 root。现行协议只表达“节点引入了一个此前不存在的缺陷”，不能表达
“节点有明确修复责任和有效机会，却遗漏了必要恢复”。

第二页中模型找到人工标签 `dec_327`，但将其判断为
`contributing_condition`：该节点只编辑 `computed_field`，未处理本 seed
的 model-construction dependencies。这个判断在现有协议内一致，但也说明
人工单根标签比 seed-specific 根因更粗。

### 2. 256 评审预算仍可能丢计划级语义

`record:decisionnode_dec_155_e25dd391` 是完整实施计划节点，明确把任务范围
收缩为几个局部函数。它具有 active-seed 事实路径且 assessment eligible，
但：

- discovered rank：366
- disposition：dropped
- reason：`total_limit`

直接把 assessment cap 扩到 512 会产生 128 个页面；默认 128 次请求预算
无法同时覆盖页面、重试、evidence expansion 和最终比较。因此更合理的
第一步是按 materialization episode 同时保留早期计划和晚期执行决策，再
根据显式运行预算决定是否扩容。

## 下一阶段建议

推荐同时推进两个相互独立的改造：

1. **Responsibility-aware omission root**
   - 区分 `introduced_defect` 与 `failed_required_repair`；
   - 后者允许 input/output defect 都为 present；
   - 必须提供任务要求、计划承诺、作用域或最后有效修复机会等事实责任依据；
   - 正确替换该决策后必须能让当前 seed 的 defect 变为 absent。

2. **Episode-diverse grounded reserve**
   - 从 candidate-to-seed 事实路径推导 materialization episode；
   - 每个 episode 同时保留最早计划决策与最接近执行的决策；
   - 64 个 reserve 按 episode round-robin，而不是全局最新 64 个；
   - funnel 追加 grounded hops、episode key、reserve rank 和裁剪原因审计。

两项改造都只作用于离线 Trace 与归因模块，不改变 Agent 行为。
