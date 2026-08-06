# Causal Judgment Context 迭代结果

## 目标

本轮增强离线归因 Judge 的节点判断上下文，同时保持 Agent、Trace 采集和 benchmark case 行为不变。新增信息只用于事后归因，不反馈给 Agent。

## 实施结果

1. `TraceGraph` 保存标准化边语义，包括 relation、evidence type、confidence、inference method 和 edge origin。
2. 每个节点获得稳定的 active-defect fingerprint、入边、活跃路径出边、下游既有判断、Causal Episode 和 Progress Episode。
3. 上下文保存到归因报告的 `defect_branches[].metadata.judgment_contexts`，可以审计模型实际收到的因果切片。
4. Claude/DeepSeek Judge 通过可选 context-aware 接口消费新上下文；旧 Judge 签名保持兼容。
5. 根因二次确认继续保持 node-local，不使用上游节点连坐确认根因。
6. 微 Progress Episode 全部保留；新增交付边界导航窗口，跨越连续无交付 turn 时不再逐个消耗搜索深度。

## 自动化验证

- `python3 -m unittest tests.test_backward_taint`
- 109 tests passed，0 failures。
- 新增测试覆盖边语义、缺陷指纹、下游判断、Episode 投影、Judge 兼容接口、提示词投影、上下文审计和交付边界窗口。

## Sphinx 真实 Benchmark

Case：`sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1`

观察缺陷：`missing_parse_namespace_object_contract`

冻结人工根因：`record:decisionnode_dec_85_d3b74e6f`

### 导航修复前

| 配置 | 访问节点 | Judge 请求 | Judge 错误 | 人工根因是否访问 | 结果 |
| --- | ---: | ---: | ---: | --- | --- |
| depth 12 / nodes 48 | 36 | 58 | 0 | 否 | depth limit |
| depth 14 / nodes 64 | 43 | 73 | 0 | 否 | depth limit |

当前 Trace 被重建为 68 个微 Progress Episode。人工根因位于 chronology 12，最新 Episode 位于 chronology 70；逐 turn 导航使根因在预算内不可达。

### 交付边界窗口修复后

| 指标 | 结果 |
| --- | --- |
| 访问节点 | 141 |
| Judge 请求 | 196 |
| active-defect fingerprint | 141/141 一致 |
| 有下游判断上下文 | 140/141 |
| 有 Progress 导航窗口 | 140/141 |
| 人工根因是否访问 | 是，visited index 116 |
| Judge 错误 | 81 |
| 最长连续连接错误 | 80 个节点 |
| 最终结果 | inconclusive，无自动根因 |

导航窗口把人工根因从 58 个微 Episode 的距离压缩为 14 个交付边界窗口，并且没有删除根因候选。图可达性和缺陷身份一致性已验证生效。

本次不能评价根因 Judge 是否正确：Provider 从 visited index 61 开始出现连续连接故障，人工根因位于 index 116，其判断因 `APIConnectionError` 降级为 `unknown`。这不是该节点的语义判断结果。

## 当前判断

### 语义 Trace / Causal IR

当前已能提供节点正文、artifact、消息链、工具/变更/验证、Episode 和边事实。对显式错误和中等长度因果链，信息基本充分。剩余事实缺口是部分 concrete decision 没有直接入边，以及导航窗口候选到窗口 anchor 仍使用通用 analyzer-path 关系，需要投影为明确的 `progress_window_member` 离线边。

### 离线归因模块

当前已经从“人工根因不可达、缺陷语义易漂移”推进到“人工根因可达、缺陷指纹稳定、Judge 输入可审计”。主要瓶颈转为搜索成本和 Provider 可恢复性。一次长分支发起近 200 个请求，连接故障后仍继续产生 80 个无价值 unknown，浪费时间和额度，也使已完成判断无法在下次复用。

## 下一轮优先级

1. 实现持久化 Judge cache/checkpoint。缓存键包含 trace hash、objective、active-defect fingerprint、node semantic hash、judgment-context hash、model 和 prompt version。
2. 连续 3 次 Provider 连接错误后停止当前分支，标记 `provider_unavailable`，不得继续制造大量 fallback unknown。
3. 支持 resume，只补跑失败/缺失节点，复用本轮已成功的节点判断和根因确认。
4. 将窗口候选到 anchor 的关系结构化为 `progress_window_member`，去掉通用 `backward_path_predecessor`。
5. 用 resume 后的同一 Sphinx case 完成 `dec_85` 判断，再回归 Pydantic 与一个成功负样本。
6. Provider 稳定后比较窗口级候选筛选与全候选搜索，目标是在保持人工根因召回的前提下把 Judge 请求降低至少 50%。
