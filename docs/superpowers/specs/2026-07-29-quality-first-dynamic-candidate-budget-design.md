# Quality-First 动态候选预算设计

## 1. 目标

本轮将离线归因模块的 Global 候选选择从固定的宽度优先截断，改造成
“质量优先、结构保障、可审计扩容”的候选预算控制。核心目标不是降低
Provider 成本，而是确保具有真实因果路径的缺陷引入节点能够进入后续 LLM
判断范围。

本轮只改造候选召回和候选预算事实，不改 Agent 行为、不改 Trace 生成、不改
根因确认语义。候选池可以扩大，但本轮不把扩大后的全量候选一次性发送给
Provider；Global Judge 分页与候选淘汰属于下一轮。

## 2. 已知问题

当前 Global grounded upstream closure 使用宽度优先遍历，最多选择 24 个节点。
在候选密集的 Trace 中，大量一跳来源会先占满预算，使二跳或三跳的 authored
decision 无法进入候选池。

Seaborn 的 `dec_230` 和 Pydantic 的 `dec_327` 均存在活动的
`decision -> tool call -> change -> verification` 因果链，但当前闭包在到达
decision 前耗尽 24 项预算。因此这是候选选择缺陷，不是 Trace 缺边，也不是
LLM 判断能力不足。

## 3. 方案选择

采用 Quality-First 动态预算方案，先完成召回事实层：

1. 将 Global grounded closure 的“发现节点”和“占用候选名额”解耦；
2. 从当前 frontier 与所有相关 outcome 分支构造多锚点闭包，避免只沿最新
   progress episode 回溯；
3. 为具有真实上游路径的二跳及以上 authored decision 保留结构保障槽位；
4. 候选去重后按互斥语义类别执行确定性配额；
5. 按发现规模动态扩展候选预算；
6. 把发现、保留和裁剪过程写入候选漏斗审计；
7. 将漏斗审计随 `candidate_compression` 持久化到归因报告。

后续轮次实施 Judge 分页、Global 分组锦标赛、阶段 Provider 预算隔离和动态
root confirmation。

## 4. 候选分类

每个候选只分配一个主类别，分类顺序固定：

1. `authored_decision`：活动修订中、可作为 authored root 的 decision；
2. `tool_change_envelope`：tool call、tool result、change 及执行结果；
3. `verification_outcome`：verification、case result、evaluation fact；
4. `factor_or_lifecycle`：context、compaction、subagent、signal、lifecycle；
5. `other`：无法归入以上类别的活动语义节点。

分类只用于离线候选调度，不能改变 `CausalCandidate`、TraceNode 或因果边事实。

## 5. 本轮预算

以高质量归因为第一优先级，按发现候选数选择确定性预算：

| 发现数 | offered 上限 | grounded decision 保障槽 |
| ---: | ---: | ---: |
| `0..24` | 24 | 4 |
| `25..96` | 96 | 16 |
| `97..192` | 192 | 32 |
| `193+` | 256 | 64 |

配额采用“保底而非硬分区”：

- 为 grounded 二跳及以上 authored decision 保留当前分档对应的槽位；
- 其余槽位仍按现有确定性顺序填充；
- authored decision 不足时，空槽自动回流给其他类别；
- 同一节点的多条 retrieval route 只占一个 offered slot；
- 所有候选必须属于活动修订，并具有可归因的真实语义边；
- provenance-only sibling 不得冒充 grounded decision 保障。

多锚点闭包扫描与 discovery 输出分离：

- 扫描最多 4,096 条 predecessor edge，最大深度为 8；
- 多锚点按 edge round-robin 推进，避免后续 anchor 被先到锚点独占；
- 扫描结束后先保留最多 64 个 deep grounded authored decisions；
- 再按确定发现顺序把 closure discovery 输出补到最多 256 个节点；
- 只有活动修订且满足
  `is_confirmation_causal_edge(..., default_eligible=False)` 的边可传播。

## 6. 数据流

```text
frontier + related outcome anchors
  -> multi-anchor grounded predecessor closure
  -> candidate classification
  -> quality-first dynamic budget
  -> grounded authored-decision reservation
  -> deterministic spillover selection
  -> candidate evidence capsules
  -> candidate funnel metrics
  -> Global Judge 或 oversized gate
```

## 7. 候选漏斗

每次 Global pass 记录一个完整可重放的 `candidate-budget-funnel/v2`：

- `discovered_count`：遍历发现的唯一活动候选数；
- `offered_count`：最终交给 Capsule 构造的候选数；
- `dropped_count`：因 offered limit 被裁掉的候选数；
- `counts_by_category`：各类别 discovered/offered/dropped 数；
- `policy`：本轮总上限与 decision 保障数；
- `grounded_decision_refs`：所有被真实因果路径支持的 decision；
- `reserved_grounded_decision_refs`：通过结构保障保留的 decision；
- `drop_reasons`：按原因聚合的裁剪数量；
- `selection_identity`：由输入、预算和输出计算的稳定 SHA-256 身份。

审计内容只记录候选选择事实，不生成根因结论，不反馈给 Agent。

## 8. 兼容性

- `SemanticPredecessorRetriever.retrieve()` 签名保持不变；
- `CausalCandidate` schema 保持不变；
- root confirmation Top-3 和 non-root Top-3 本轮保持不变；
- Provider 请求上限本轮保持不变；
- checkpoint replay 不增加 Provider 调用；
- 旧报告没有 `candidate_funnel` 时继续可读取；
- 早期 `candidate-budget-funnel/v1` 只读兼容，不伪装成可完整重放的 v2；
- 新报告通过 v2 漏斗 schema 保留完整审计；
- 只校验系统拥有的 `candidate_compression.candidate_funnel`，业务 payload 中
  的同名字段不属于系统审计对象。

## 9. 验收标准

1. 24 个以上一跳候选存在时，真实二跳 authored decision 仍进入 Global pool；
2. 保留 decision 的 downstream path 是图中可验证的真实因果路径；
3. 同一输入重复运行的候选顺序和 `selection_identity` 完全一致；
4. 漏斗分类计数满足 discovered = offered + dropped；
5. 漏斗可在 completed、bypassed 和 failed Global pass 中读取；
6. 现有 1,234 项测试保持通过；
7. Seaborn 四个人工根因在对应 seed 至少被一个候选池召回；
8. Pydantic `dec_327` 在两个 seed 中均被召回并保留；
9. Sphinx 小图仍使用最低预算档并稳定召回 `record:decision`；
10. 大图的全量候选不直接作为一次 Provider 请求发送；
11. fabricated refs 保持为 0。
