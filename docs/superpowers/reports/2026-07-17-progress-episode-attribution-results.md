# Progress Episode 归因迭代结果

## 1. 目标与约束

本轮针对 2026-07-16 多 Benchmark 对照评测中“人工可归因 3/3、自动根因命中 0/3”的问题进行离线改造。

约束保持不变：

- 不修改 Agent 提示词、任务编排、工具调用或仓库行为；
- 不把派生事实写回原始 `trace.json`；
- 不把归因结果反馈给 Agent；
- 所有进展事实标记为 `offline_passive_reconstruction`、`offline_only=true`、`behavior_impact=none`。

## 2. 实施内容

1. 新增 `progress.episode`，按 Agent turn 聚合 reasoning、action、tool、change 和 verification 语义。
2. 以任务决策时间排序 Episode，避免晚期消息携带早期 context 管理记录导致时间链错乱。
3. 建立 `previous episode -> current episode -> evaluation/final response` 的离线导航链。
4. Episode 不可成为根因；分析器必须下钻到具体决策、动作、变更或验证节点。
5. 区分完整 `member_refs` 与用于逐节点 LLM 判断的 `candidate_member_refs`。
6. 决策节点压缩时优先保留 `rationale`、`decision_type`、`chosen_action` 和 `intent`。
7. `case.observed_defect`、`case.quality_gap` 由生产 LLM Judge 校验，不再无条件判为缺陷。
8. 报告新增 `judge_model` 与 `judge_request_count`，证明模型调用及其成本，不保存凭据和提示词。

## 3. 图可达性

使用冻结人工标签对 TerminalBench、FeatureBench Pydantic 和 FeatureBench Sphinx 做后向图遍历：

| Case | Episode 数 | 人工根因图可达 | 最短图深度 |
| --- | ---: | ---: | ---: |
| TerminalBench cancel-async-tasks | 9 | 4/4 | 1-2 |
| FeatureBench Pydantic | 16 | 2/2 | 7 |
| FeatureBench Sphinx | 15 | 1/1 | 8 |

图层召回从上一版自动分析未访问人工根因的 0/3，提升为三个 Case 全部可达。

## 4. 全成员展开基线

同一 DeepSeek Judge、同一原始 Trace 下，全量展开 Episode 成员得到：

| Case | 访问节点 | Judge 请求 | 人工根因被访问 | 自动结果 |
| --- | ---: | ---: | ---: | --- |
| TerminalBench | 42 | 76 | 2 个核心决策 | `root_found`，但根因错误 |
| Pydantic | 128 | 158 | 3/3 | 节点上限，`inconclusive` |
| Sphinx | 128 | 194 | 3/3 | 节点上限，`inconclusive` |

TerminalBench 的错误根因为 `dec_17`：该节点讨论“尚在 semaphore 等待的任务”，Judge 却把它解释为“已启动任务 cleanup 被 SIGINT 中断”，发生 active-defect 语义漂移。人工根因 `dec_2/dec_3` 已被访问，但 `dec_2` 被错误判断为正确模式。

## 5. 输入投影缺陷

Pydantic 和 Sphinx 的 Judge 输出将人工根因描述为泛化的 `continue_processing_stream`。检查发现：

- 原节点包含 300-500 字符的关键 `rationale`；
- `TraceNode.compact()` 在节点较大时只保留宽泛 `selected_context_refs` 和 `message_transforms`；
- `rationale`、`decision_type` 和 `chosen_action` 不在压缩语义白名单中。

修复后，`dec_97/102` 的 1600 字符 Judge 输入稳定保留决策类型、动作、意图和 rationale 摘要。Pydantic `dec_102` 的新判断已能识别“已观察到空实现，却继续寻找参考实现而不是写补丁”的真实语义。

## 6. 候选投影效果

完整 Episode 成员不删除，只减少需要逐节点调用 LLM 的候选：

| Case | 完整成员 | 裁决候选 | 减少比例 | 人工根因仍为候选 |
| --- | ---: | ---: | ---: | ---: |
| TerminalBench | 29 | 27 | 6.9% | 2/2 |
| Pydantic | 108 | 32 | 70.4% | 2/2 |
| Sphinx | 111 | 30 | 73.0% | 1/1 |

候选规则保留 reasoning、第一条 authored action、写入/编辑、验证、委派、MCP/skill 和错误节点。普通 read/grep 动作仍在 Episode 中可查看，但默认不单独消耗 Judge 请求。

## 7. 第三次回放与外部阻塞

第三次回放在 Pydantic 中途出现连续 `APIConnectionError`：

- Pydantic 已访问 75 个节点、发起 98 次请求，人工根因 3/3 仍被访问；
- 连接故障后产生 17 个 Judge error，结果必须视为 `inconclusive`；
- 后续 Sphinx 和 TerminalBench 的首个请求立即连接失败，不能用于效果比较。

因此，本轮可以确认图召回、时间链、决策输入投影和候选压缩均生效，但不能宣称最终 LLM 根因准确率达到目标。

## 8. 当前结论与下一缺口

API 恢复后，对冻结人工标签中的四个关键节点做了不计入端到端命中率的定点 Judge 诊断。新增“任务前提不等于 Agent 行为缺陷”以及“重复 LLM generation envelope 只能是 `derived_from`”规则后：

| 节点 | 定点判断 | 与人工分析一致性 |
| --- | --- | --- |
| Pydantic `dec_102` | `defect_introduction`, root=true | 一致 |
| Sphinx `dec_85` | `defect_introduction`, root=true | 一致 |
| Pydantic `dec_97` | schema repair 后仍为 unknown | 未决 |
| Terminal `dec_2` | 本次为 schema unknown；前一次定点判断精确命中 | 不稳定 |

`dec_102` 的判断明确区分了“上游 LLM 提供分析”与“当前 decision 选择继续处理而不写补丁”；`dec_85` 的判断明确引用“已经 comprehensive understanding 却继续 look at”，不再把 LLM 包装节点当作同一缺陷的传播源。

该结果证明当前 Trace 和 Judge 输入已经足以支持至少两个失败 Case 的人工等价根因判断，但定点诊断不能替代完整后向搜索评测。

当前能力从“根因节点不可达”推进到“根因节点稳定可达且关键决策语义可交给 Judge”。下一瓶颈是：

1. 完整搜索需要在确认根因后停止无关的更早 Episode 分支，否则正确候选已出现仍会消耗大量请求；
2. active-defect 机制一致性仍需结构化校验，避免把相邻但不同的异步语义误当作同一缺陷；
3. Schema repair 仍有随机性，同一节点可能在不同调用中准确命中或落为 unknown；
4. 连续 Provider 连接失败缺少分支级熔断和可恢复重试策略；
5. 仍需在 API 恢复后重跑三条失败样本和 Gin/Axios/Astropy 成功负样本，确认准确根因与假阳性。

本轮不通过启发式直接提升任何节点为根因。所有最终根因仍必须由 LLM 节点判断、独立根因确认和现有 Causal Episode 折叠共同产生。
