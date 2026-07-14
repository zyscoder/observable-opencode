# Loop 5 Sphinx 语义 Trace 与离线归因对照报告

## 评估对象

- Benchmark：FeatureBench Lite
- Case：`sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1`
- 输入 Trace：沿用 Loop 4 的同一份 1,768-record Trace，未重新执行 Agent
- 归因模型：`deepseek-v4-pro`，Anthropic-compatible endpoint
- 单请求超时：3,600 秒
- 搜索预算：每条缺陷分支 `max_depth=24`、`max_nodes=128`
- 目标：在不把真实执行证据、环境失败、正确计划或代码复杂度当作根因的前提下，为每个观察缺陷找到可审计的引入 episode

## 基线问题

Loop 5 修复前的归因结果虽然报告 `root_found`，但没有按缺陷分支核算覆盖率，并存在以下错误：

| 问题 | 基线表现 |
| --- | --- |
| Parser 根因重复 | 同一实现 episode 同时报出 `dec_160`、`dec_162`、`chg_2` |
| Parser 假早根因 | 把描述正确接口契约的 `dec_160` 误判为缺陷引入 |
| Verification 缺失 | 未找到错误 self-test 的 authored action 根因 |
| Scope 缺失 | 未找到越界兼容性修改的决策根因 |
| 无关根因 | 仅因逻辑复杂而把 `chg_5` 推测为 symbol-resolution 根因 |
| 因果边污染 | 把环境/验证失败误当作 out-of-scope 缺陷传播源 |
| 质量状态失真 | 2 个 blocking gap，但顶层仍显示高覆盖的 `root_found` |

## Ground Truth 复核修正

旧人工报告把 `decisionnode_dec_160_d3011460` 标为 parser 根因。重新读取完整 reasoning artifact 后发现，该节点明确计划让 `struct/union/enum` 仅解析 `_parse_nested_name()`，并不要求输入重复 directive keyword。它描述的是正确契约。

真正的语义偏离发生在 `decisionnode_dec_162_d4c07ef3`：该 authored edit 改为调用 `_parse_struct_keyword`、`_parse_union_keyword` 和 `_parse_enum_keyword`，把 keyword 要求写入了可执行变更。因此本轮将 parser 人工根因修正为 `dec_162` 所在 causal episode。

这说明人工复核结果应作为可审计标签，而非不可质疑的真值。根因评估需要同时检查 Trace 原文、人工标签和离线模块结论的一致性。

## 最终归因结果

| 缺陷分支 | 最终根因 episode | 结论 |
| --- | --- | --- |
| `incorrect_declaration_parser_contract` | `decisionnode_dec_162_d4c07ef3` | edit action 首次把错误 parser contract 写入实现；正确计划 `dec_160` 不再是根因 |
| `self_test_does_not_match_interface_contract` | `decisionnode_dec_362_abbf250d` | authored bash script 使用 `struct MyStruct` 等错误契约输入；tool result 仅是真实执行证据 |
| `host_compatibility_changes_outside_feature_scope` | `decisionnode_dec_306_45a8106a`、`decisionnode_dec_346_dac72e2f` | 分别对应首轮 logging 兼容性修改决策和后续继续修复 host Python 的独立越界决策 |

最终报告指标：

- 顶层结果：`root_found`
- 缺陷分支：3/3 `root_found`
- Judge 执行错误：0
- 未解析引用：0
- 搜索截断：0
- blocking gap：0
- advisory gap：3
- 最终分析置信度：`high`

3 个 advisory 均位于已找到根因的分支，只表示个别旁路节点的模型判定仍不稳定，不再错误地宣告整条归因链被阻塞。

## 本轮归因模块改进

1. 对每个观察缺陷建立独立 branch，禁止不同缺陷共享全局判断状态。
2. 用 call ID、span ID 和 confirmed message lineage 构造 causal episode。
3. 从 change/tool result 等表面节点展开同 episode 的早期 reasoning 和 authored action。
4. 当 episode 已有 authored action decision 时，不再重复送审等价的 `tool.call` 包装记录。
5. 强制 `defect_propagation` 引用真实、已提供且包含同一缺陷的上游节点。
6. 将 `motivated_by_evidence` 与 `defect_propagated_from` 分离，环境失败不再携带决策缺陷。
7. 对根因执行独立 node-local confirmation，要求语义 grounding 和“精确执行当前节点是否会导致缺陷”的反事实检查。
8. 根因确认否决唯一 defect predecessor 后，重新计算 downstream propagation 的 introduction 边界。
9. 将 schema 修复耗尽与网络/执行失败分开：前者安全降级为可审计 `unknown`，不伪造根因。
10. 将已闭合分支中的旁路 unknown 移入 `advisory_gaps`，仅 unresolved branch 保留 blocking gap。

## 本轮 Trace 事实层改进

新生成的 Trace 升级为 5.6。Repository change 不再只引用“最近失败验证”，而是同时记录：

- `tool_call:*`：`materialized_by_action`
- `span:*`：`executed_in_span`
- `verification:*`：`motivated_by_evidence`

每条关系包含 inference 来源和置信度。`failure_to_change` 的正式语义也由笼统的 `failed_before` 收敛为 `motivated_by_evidence`，并明确标注 `motivation_not_defect_propagation`。这些字段仅由 Trace 被动记录，不参与 Agent 输入、工具选择或任务循环。

## 剩余风险与下一轮方向

1. Root confirmation 增加了模型调用，但当前报告尚未统计 confirmation 调用数、延迟和 token；下一轮应将离线归因自身的成本与稳定性结构化输出。
2. 个别长 reasoning 即使已有 hydrated artifact，模型仍可能误认为关键位置被截断；可在确认 prompt 中显式区分 preview truncation 与 artifact truncation。
3. Scope 分支存在多个独立越界 episode。下一轮可增加 `primary_root`、`contributing_root` 与 `repeated_introduction` 层级，但不应强行合并没有确认关系的跨-turn 决策。
4. Trace 5.6 的 change provenance 需要在新执行的开源 benchmark case 上回归，验证新关系能直接减少 Judge 对 verification/change 边的误判。
5. 当前环境缺少 Bun 可执行文件，新增 TypeScript 行为测试已编写且类型检查通过，但仍需在 GitHub workflow 或带 Bun 的 release 构建环境中执行完整测试。
