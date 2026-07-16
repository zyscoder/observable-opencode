# 最终 Claim 证据闭环与归因边界设计

## 背景

当前 Causal IR 已能完整记录工具结果进入模型上下文的链路，但 HTTP 实际运行时，
`response.output` 往往没有显式 `source_refs`。最终 Claim 因而只能连接到 LLM、消息转换和
上下文节点，无法进一步确定回答中的每条事实由哪些已进入模型上下文的证据支持。

离线归因模块能够遍历这些链路，但当模型把当前节点判为缺陷传播、其声明的所有缺陷上游又
被判为无缺陷时，当前实现仅记录矛盾，不会把当前节点保留为可复核的缺陷引入边界。

## 目标

1. 从最终一次模型生成的确认上下文中，确定性重建 Claim 的候选证据集合。
2. 对每条最终 Claim 记录候选证据、匹配结果、选中或拒绝原因以及证据角色。
3. 只有确定性匹配成功的证据才成为 Claim 的归因边；未匹配候选仅保存在数据字段中。
4. 当缺陷传播判断与上游判断形成闭合矛盾时，保留当前节点为待复核的首个缺陷边界。
5. 全程保持被动旁路，新增事实不得进入 Agent 上下文或改变 Agent 行为。

## 非目标

- 不推断模型内部注意力或思维过程。
- 不把“证据进入上下文”直接等同于“Agent 使用了证据”。
- 不新增在线模型调用，也不把离线归因结果回写 Trace。
- 不把全部上下文候选升级为直接证据。
- 本轮不删除 legacy/provenance/HTML 兼容投影。

## 方案选择

### 方案 A：把最终生成上下文中的全部证据直接挂到 Claim

实现简单，但会制造大量假阳性归因，无法区分“出现过”和“实际支持回答”。不采用。

### 方案 B：确认上下文候选 + 离线确定性 Claim 匹配

从同一生成生命周期的 `context.transform` 和 `context.pack` 读取确认工具结果，再追溯到
语义事实、变更和验证记录。候选集合只说明“证据确认进入模型上下文”；基于 Claim 文本、
结构化事实、路径和值进行确定性匹配后，才建立直接支持边。采用此方案。

### 方案 C：增加第二次 LLM 调用判断每条 Claim 的证据

语义能力更强，但成本高、不可复现，并可能把诊断模型输出误当运行事实。本轮不采用。

## Trace 数据流

```text
tool.result
  -> confirmed context.pack
  -> context.transform
  -> llm.call
  -> response.output
  -> response.claim

tool.result
  -> evidence.semantic_fact / verification / change
  -> deterministic claim grounding
  -> response.claim
```

`generationEvidenceCandidates()` 仅从最终生成生命周期的确认 context set 中读取成员：

1. 收集 `context.transform.inferred_tool_context_refs` 和其确认 context set 成员。
2. 建立工具结果的规范身份集合，包括 node ref 和 call-id ref。
3. 选择来源链可回溯到这些工具结果的语义事实、有效验证和当前变更。
4. 保留工具结果自身作为没有结构化事实时的后备候选。

候选对象记录：

- `candidate_ref`
- `candidate_origin=confirmed_generation_context`
- `decision=selected_direct_support|rejected_no_match`
- `score` 和 `reasons`
- `rejection_reason`
- `agent_attention_observed=false`
- `behavior_impact=none`

## Claim 归因语义

- `direct_evidence_refs`：匹配成功的 evidence/tool outcome。
- `direct_support_refs`：匹配成功的 evidence、verification 或 change。
- `grounding_candidate_refs`：确认进入生成上下文的全部候选。
- `grounding_decisions`：逐候选的确定性选择结果。
- `grounding_method=confirmed_context_semantic_match_v1`。
- `grounding_behavior_impact=none`。

选中的证据沿现有 `evidence_to_claim` 等归因边连接 Claim。被拒绝候选不写入
`source_refs`，也不创建可归因边，避免污染后向污点分析。

## 归因边界规则

若某节点满足以下全部条件：

1. 当前判断 `defect_status=present`；
2. 当前角色为 `defect_propagation`；
3. 至少声明一个 `defect_propagated_from` 上游；
4. 所有这些上游均已访问且判为 `absent`；
5. 不存在未知、未解析或未访问的缺陷上游；

则当前节点成为“首个观测到的缺陷边界候选”。分析器将其临时重分类为
`defect_introduction`，并交给现有 root confirmer 反证。确认失败时必须移除，不能作为根因输出。

## 错误与退化处理

- 找不到确认生成上下文时保持当前行为，不猜测证据。
- 候选存在但没有匹配时，Claim 保持 unsupported，并记录逐候选拒绝原因。
- context set 或工具结果 ref 无法解析时写入已有诊断，不建立直接证据边。
- 归因边界存在未知上游、judge error 或搜索预算截断时保持 inconclusive。

## 验收标准

自动化验收：

- 新增 Trace 测试必须先失败，再通过。
- 新增归因边界测试必须覆盖 absent、unknown 和 confirmer 拒绝三种情况。
- 现有 Causal IR、CaseTrace、runner、stress、归因模块和 typecheck 全部通过。

真实语义 case 验收：

- `semantic-requirement-priority` 的 `claim_direct_evidence_refs` 不再缺失。
- 三个语义 case 的 Claim 直接证据覆盖率目标不低于 90%。
- 归因图 unresolved ref 和 temporal attribution edge 均为 0。
- 人工后向检查能从最终 Claim 到达对应语义事实、工具结果和生成节点。
- Trace 边数和文件体积相对本轮基线增长均不超过 10%。
- 不要求强行输出根因；证据不足时仍应诚实返回 inconclusive。
