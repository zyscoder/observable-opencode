# Resumable Judge Attribution 迭代结果

## 目标

本轮解决长因果链归因在 Provider 连接故障后重复调用、批量制造 `unknown`、无法续跑的问题，同时把交付边界窗口中的候选关系从通用遍历边收敛为可审计的语义边。

## 已实施

1. 新增 JSONL 持久化 Judge checkpoint。缓存键覆盖 stage、model、完整请求消息、token 配置、thinking 配置和 prompt schema version。
2. 只缓存通过 schema 校验的节点判断和根因确认；写入后执行 flush 与 fsync，损坏尾行可跳过恢复。
3. 连续 Provider 连接或超时错误达到阈值后打开 circuit，分支终止为 `provider_unavailable`，不再继续生成 fallback `unknown`。
4. circuit 在普通节点、评估断言、初始根因确认和提升边界确认四个阶段均可终止分支；已经完成的初步判断保持不变，但未确认根因不会被报告。
5. CLI 默认在 attribution 输出旁生成 `<stem>.judge-cache.jsonl`，同命令重跑只请求未完成或失效的判断。
6. 报告增加 `metadata.judge_cache` 和 `metadata.provider_circuit`，记录命中、缺失、写入、损坏项、连续错误与 circuit 状态。
7. 窗口候选到 anchor 的活跃路径关系改为 `progress_window_member`，记录所属 Progress Episode、`delivery_bounded_no_delivery_window_v1` 和 `offline.progress_navigation` 来源。

## 自动化验证

- `PYTHONPATH=. python3 -m unittest tests.test_backward_taint`
- 119 tests passed，0 failures。
- `git diff --check` passed。
- 覆盖缓存持久化与失效、损坏尾行恢复、普通判断和根因确认命中、连续错误熔断、成功请求重置错误计数、Analyzer 收敛、报告指标、窗口边，以及根因确认阶段熔断。

## Sphinx 本地事实层验证

使用现存开源 FeatureBench Sphinx case 的信号退出快照：

`partial/latest.json` 包含 542 个 Causal IR nodes、1743 条 edges 和 300 个 artifacts。该快照与同目录 HTML/records 同源，可用于离线归因，不需要重新运行 Agent。

上一轮冻结人工根因 `record:decisionnode_dec_85_d3b74e6f` 来自另一次执行。formal-v3 中语义对应节点为 `record:decisionnode_dec_85_1b4ffa67`：二者都记录 Agent 已按显式接口列表形成实现计划，却没有继续搜索 `DefinitionParser` 的其他仓库调用点。跨运行 record hash 不稳定，因此人工标签必须先按语义锚点对齐，不能直接比较字符串 ID。

对 formal-v3 对应节点重建出的活跃路径边为：

```json
{
  "from_ref": "record:decisionnode_dec_85_1b4ffa67",
  "to_ref": "progress_episode:progress_ae113bff794281f8",
  "relation": "progress_window_member",
  "evidence_type": "offline_reconstruction",
  "evidence_refs": ["progress_episode:progress_07cab28e76ff9a33"],
  "eligible_for_attribution": true,
  "inference_method": "delivery_bounded_no_delivery_window_v1",
  "edge_origin": "offline.progress_navigation"
}
```

该节点上下文还包含 6 个上游候选、12 条标准化入边、Progress Episode、导航窗口和完整 active-defect fingerprint。窗口关系缺失问题已修复。

## 与上一轮基线比较

| 指标 | 上一轮 | 本轮当前状态 |
| --- | ---: | --- |
| 人工根因可达 | 是，visited index 116 | 本地图重建可达，跨运行语义节点已映射 |
| 窗口候选关系 | `backward_path_predecessor` | `progress_window_member` |
| Provider 连续错误 | 最长 80 次 | 达阈值后立即 `provider_unavailable` |
| 已完成判断复用 | 无 | schema-valid JSONL checkpoint |
| 根因确认阶段熔断 | 会生成 fallback unknown | 保留初步判断、停止确认、结果 inconclusive |
| 真实 Judge resume | 196 请求后无法续跑 | 待本次外发授权后验证 |

## 当前限制与下一步

当前执行环境阻止把本地 Sphinx Trace 发送到 DeepSeek。即使该 case 来自开源 benchmark，也需要用户明确授权将其 `partial/latest.json`、评审事实和归因提示发送到 `https://api.deepseek.com/anthropic`。授权后使用同一输出与 checkpoint 连续运行，验证：

1. 首次中断前的成功判断是否全部持久化；
2. 第二次运行的 cache hits 是否等于可复用判断数；
3. `dec_85_1b4ffa67` 是否获得有效节点判断和根因确认；
4. 自动根因与人工语义根因是否一致；
5. 相对旧基线 196 次请求是否降低至少 50%。

本地验证还揭示出下一轮值得处理的问题：record ID 只在单次运行内稳定，跨运行 benchmark Ground Truth 需要基于 case、语义角色、规范化正文、代码位置与 action identity 的稳定 `semantic_anchor_id`，否则同一根因在回归运行中必须人工重新映射。
