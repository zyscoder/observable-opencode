# Agentic Recursive Causal Attribution 设计

## 1. 背景

observable-opencode 已经能够将 Agent、LLM、上下文转换、工具、MCP、Skill、Subagent、代码变更、验证和结果处理投影为可审计的 Causal IR。离线归因模块也已经具备后向遍历、节点 Judge、根因二次确认、Progress Episode 导航、持久化缓存、熔断和断点续跑能力。

真实 Sphinx FeatureBench 验证暴露出两个问题：

1. 当前遍历会对大量节点逐个调用 Judge，单个复杂分支可产生近 200 次请求；即使增加缓存，首次分析仍然昂贵。
2. 当前 Judge 容易局部判断正确但全局归因错误。例如 `dec_85` 被判定为从初始 benchmark prompt 传播缺陷，prompt 随后被判为根因；但在 Harness 优化视角下，prompt 未显式枚举隐藏兼容方法只是促进因素，Agent 过早收敛并放弃仓库调用点搜索才是主要可优化根因。

问题不在于 Trace 缺少 prompt、decision 或数据流事实，而在于归因模块缺少一个既系统又不僵化的调查过程。固定规则不能预先排除 prompt、tool 或 context 成为根因；完全自由的 Agent 调查又可能漫游、漏查或难以审计。

## 2. 目标

采用“递归后向语义污点分析主干 + 多假设回溯 + 按需 Agentic 调查 + 独立根因确认”的融合方案，使离线归因模块能够：

- 从已知缺陷节点出发，系统地沿重建语义数据流寻找候选引入节点；
- 由 LLM 理解节点、边和缺陷语义，而不是按组件类型硬编码结论；
- 识别同一缺陷传播、不同缺陷之间的因果转化、促进条件、结果证据和无关关系；
- 同时维护并比较多个竞争根因假设，不因一次局部判断过早剪掉正确分支；
- 在直接数据流不足、判断冲突或证据缺失时，主动检查 artifact、上下文链、Episode、相邻分支和代码变更；
- 输出主要根因、共同根因、促进因素、结果放大因素和未解决假设；
- 保持分析离线、被动、可恢复、可审计，不改变 Agent 或 benchmark 执行行为。

## 3. 非目标与约束

- 不修改 Causal IR 原始 Trace，不向被评测 Agent 反馈归因结论。
- 不通过固定组件白名单决定根因。Prompt、Context、Tool、LLM、Subagent、Harness 均可在证据充分时成为根因。
- 不把时间相邻视为因果传播；每条递归关系必须有显式或可审计的重建证据。
- 不要求一次调用将完整 Trace 塞给 LLM；上下文按调查需要逐步展开。
- 不把 `unknown`、Provider 错误、预算耗尽或缺失证据提升为根因。
- 不在本轮重构 Trace 采集内核；若调查发现事实缺口，通过 `trace_improvement_report` 反馈后续插装优化。

## 4. 核心概念

### 4.1 Active Defect State

递归过程中追踪的不是一个固定字符串，而是结构化缺陷状态：

```json
{
  "defect_state_id": "defect:missing_namespace_contract",
  "label": "missing_parse_namespace_object_contract",
  "expected": "DefinitionParser preserves the repository compatibility contract",
  "actual": "parse_namespace_object is absent",
  "mechanism": "implementation planning omitted an existing called method",
  "scope": "parser_contract_recovery",
  "fingerprint": "...",
  "derived_from_defect_state_id": "",
  "transformation_reason": ""
}
```

当上游节点包含不同但能导致当前缺陷的语义错误时，创建新的 Defect State，并通过 transformation 关系连接。这样可以表达“错误上下文选择 -> 错误计划 -> 错误代码 -> 验证失败”，避免强迫所有节点使用同一个 defect type。

### 4.2 Causal Relation

LLM 对当前节点与每个语义前驱之间的关系分类为：

- `same_defect_propagation`：上游已包含相同缺陷，递归时保持 Active Defect State。
- `defect_transformation`：上游包含不同缺陷，并通过明确机制转化为当前缺陷；递归时切换为新的 Defect State。
- `introduction_candidate`：当前节点包含缺陷，未发现更上游的缺陷传播或转化来源。
- `contributing_condition`：影响结果发生概率或暴露条件，但本身不携带当前缺陷。
- `outcome_evidence`：忠实记录或暴露缺陷结果。
- `unrelated`：与当前缺陷没有可证实关系。
- `unknown`：现有证据不足。

这些关系是 LLM 基于语义事实做出的判断。确定性代码只校验引用、枚举值、证据来源和字段一致性。

### 4.3 Attribution Hypothesis

每条可能的因果解释维护独立假设：

```json
{
  "hypothesis_id": "hyp_12",
  "claim": "Agent prematurely narrowed implementation to explicitly listed methods",
  "candidate_root_ref": "record:decisionnode_dec_85_1b4ffa67",
  "active_defect_state_id": "defect:missing_namespace_contract",
  "supporting_evidence_refs": [],
  "opposing_evidence_refs": [],
  "unresolved_questions": [],
  "alternative_hypothesis_ids": [],
  "counterfactual": {},
  "status": "active",
  "confidence": 0.0
}
```

假设状态为 `active`、`supported`、`rejected`、`superseded` 或 `unresolved`。假设置信度用于调查排序，不作为固定阈值下的机械根因判定。

## 5. 总体架构

```text
Observed Defect / Quality Gap
            |
            v
Defect State Builder
            |
            v
Recursive Causal Frontier <---- Checkpoint / Resume
            |
            v
Semantic Predecessor Retriever
            |
            v
LLM Relation Judge
      |             |
      | sufficient  | conflict / missing evidence
      v             v
Recursive Branch    Agentic Investigation Tools
      |             |
      +-------> Hypothesis Ledger <-------+
                         |
                         v
               Independent Root Verifier
                         |
                         v
               Ranked Attribution Report
```

### 5.1 Defect State Builder

从 `case.observed_defect`、`case.quality_gap`、失败验证或用户指定起点构造初始 Defect State。状态保留 expected、actual、mechanism、scope、时间锚点和稳定 fingerprint。所有后续 Judge 输入必须携带当前状态以及从初始状态到当前状态的 transformation chain。

### 5.2 Recursive Causal Frontier

算法语义上采用递归后向遍历；工程上使用可序列化 priority frontier，而不是语言调用栈，以支持：

- 断点续跑；
- 循环检测；
- 分支优先级调整；
- 多缺陷分支隔离预算；
- 回溯和替代假设恢复。

Frontier item 包含当前节点、Active Defect State、下游路径、假设 ID、递归深度、候选来源、优先级和已检查证据。

### 5.3 Semantic Predecessor Retriever

候选前驱按以下层次检索，但不把排序直接解释为根因概率：

1. Trace 中 attribution-eligible 的显式 Causal IR edges；
2. message/context/tool/change/subagent lineage 的 confirmed 或 content-matched edges；
3. Causal Episode 与 delivery-bounded Progress Window 的 concrete candidates；
4. 已记录但未进入当前直接路径的 sibling actions、verification 和 artifacts；
5. 仅在证据不足时启用的语义检索候选，并标记为 inferred。

排序可参考边证据强度、与当前缺陷的语义相关性、任务义务相关性、状态变更关系、时间先后和当前假设缺口。排序只决定检查顺序，不得永久剪枝未检查分支。

### 5.4 LLM Relation Judge

每次判断提供：

- 当前 Active Defect State 和 transformation chain；
- 当前节点完整语义、hydrated artifact 和 semantic role；
- 候选前驱及标准化边语义；
- 当前节点到最终缺陷的下游路径摘要；
- 已完成的下游判断；
- 所属 Agent/Subagent、Progress Episode、上下文快照和任务义务；
- 当前假设及支持、反对、缺失证据；
- 证据 provenance 和置信等级。

Judge 返回当前节点缺陷状态、每个候选前驱的 Causal Relation、是否递归、递归时使用的 Defect State、判断理由、直接证据引用、缺失证据和建议下一步。

LLM 可以认为 prompt 是根因，也可以认为它只是促进因素；可以认为 Agent 决策引入缺陷，也可以认为 Agent 忠实执行了错误需求。结论必须来自任务义务、节点语义、数据流和反事实，而不是组件规则。

### 5.5 Hypothesis Ledger 与回溯

当多个前驱可解释当前缺陷时，为每条解释创建或更新假设。调查器持续维护：

- 支持证据和反对证据；
- 尚未检查的关键分支；
- 同一结果的替代解释；
- 是否存在共同原因；
- 已被证伪的传播关系；
- 当前最有信息增益的下一步调查。

一个分支被 Judge 判为 `unrelated` 或 `contributing_condition` 后，不立即删除对应假设。只有证据充分且独立判断一致时才标记 rejected；否则保留为 unresolved，并允许其他分支失败后回溯。

### 5.6 Agentic Investigation Tools

Agentic 调查不是默认自由漫游，仅在以下条件触发：

- 当前节点没有可用语义前驱，但仍明显不是缺陷引入点；
- 候选关系为 `unknown`；
- 两个高置信假设相互冲突；
- artifact 被截断或关键输入输出未展开；
- transformation mechanism 缺少中间节点；
- 根因确认发现反例。

离线工具包括：

- `inspect_node(ref)`
- `expand_upstream(ref, relation_filter)`
- `expand_downstream(ref)`
- `inspect_artifact(artifact_id, range)`
- `inspect_episode(ref)`
- `inspect_context_lineage(message_id)`
- `inspect_task_obligations(scope)`
- `compare_causal_paths(path_ids)`
- `search_semantic_nodes(query, scope)`
- `record_hypothesis(...)`
- `reject_hypothesis(...)`
- `request_root_confirmation(...)`

工具由本地代码执行，只读取 Trace、artifact 和离线重建索引。每次调用及返回摘要进入 attribution investigation journal。

### 5.7 Independent Root Verifier

候选引入节点必须经过独立验证。Verifier 使用独立 prompt，并尝试证伪候选：

1. 当前节点是否确实包含被追踪的缺陷或可解释的上游缺陷；
2. 节点是否早于下游结果；
3. 已检查的上游是否仍有更早的缺陷传播或转化来源；
4. 如果节点行为被替换为语义正确行为，缺陷是否大概率不再发生；
5. 是否存在能更完整解释相同结果的竞争假设；
6. 结论是否由当前节点正文、artifact、路径和任务义务直接支持。

Verifier 不接收首轮 Judge 的最终结论措辞，只接收候选、事实、路径、支持与反对证据，降低锚定偏差。确认结果为 `confirmed`、`rejected` 或 `unknown`。

## 6. 递归算法

```text
seed = build_defect_state(observed_defect)
frontier.push(observed_ref, seed, initial_hypothesis)

while frontier has work and budget remains:
    item = frontier.pop_highest_priority()
    context = hydrate_causal_context(item)
    candidates = retrieve_semantic_predecessors(item, context)
    judgment = llm_judge(item, candidates, context)
    update_hypothesis_ledger(judgment)

    for relation in judgment.predecessor_relations:
        if relation is same_defect_propagation:
            frontier.push(relation.ref, item.defect_state, relation.hypothesis)
        if relation is defect_transformation:
            transformed = build_upstream_defect_state(relation)
            frontier.push(relation.ref, transformed, relation.hypothesis)
        if relation is contributing_condition:
            record_condition(relation)
        if relation is outcome_evidence or unrelated:
            close_that_edge(relation)
        if relation is unknown:
            schedule_agentic_investigation(relation)

    if current node is defective and no viable defective predecessor remains:
        propose_introduction_candidate(item)

    if a branch is rejected:
        restore_best_unresolved_alternative()

verify all non-dominated introduction candidates independently
rank confirmed roots, co-roots, contributing conditions, and amplifiers
```

### 6.1 循环与合并

访问键使用 `(node_ref, defect_fingerprint, hypothesis_semantic_hash)`。相同节点和缺陷状态再次出现时合并证据，不重复调用 Judge；不同 transformation state 可以独立访问同一节点。检测到循环时保留循环边并停止该递归路径，不把循环节点作为根因。

### 6.2 停止条件

单个假设分支在以下情况停止：

- 找到并独立确认缺陷引入节点；
- 当前节点无缺陷，且没有可转化的上游缺陷；
- 假设被支持更充分的竞争假设取代；
- 所有可用证据耗尽，结论为 unresolved；
- Provider 不可用；
- 节点、深度、调查轮次、artifact 字节或请求预算耗尽。

预算耗尽只能输出 `inconclusive` 或部分结果，不得将最后访问节点当作根因。

## 7. 归因视角而非硬边界

输入允许声明自然语言 `analysis_perspective`，例如“寻找 Harness 可优化的主要根因”或“评估需求描述质量”。它用于：

- 确定报告中 primary root 的排序视角；
- 指导反事实中的可控性与改进价值判断；
- 区分主要根因、共同根因和促进因素。

它不得：

- 禁止某个组件参与因果链；
- 将指定组件自动判为根因；
- 删除不符合视角的事实。

同一 Trace 可以在不同 perspective 下得到不同 primary attribution，但底层确认的因果关系和证据保持一致。例如 Sphinx prompt omission 可以在需求质量视角成为主要根因，在 Harness 优化视角成为促进因素。

## 8. 持久化、缓存与恢复

复用现有 `JudgmentCache` 和 Provider circuit breaker，并增加调查 checkpoint：

- `frontier.jsonl`：新增、完成、回溯和合并的递归 item；
- `hypotheses.jsonl`：假设状态及证据变化；
- `investigation-actions.jsonl`：Agentic 工具调用和结果摘要；
- Judge cache：节点关系判断与独立确认结果。

缓存键包含 prompt schema version、模型、Active Defect State、当前节点、候选前驱、下游路径摘要、假设上下文和 hydrated evidence hash。上下文或缺陷状态变化时不得复用旧判断。

SIGINT、SIGTERM、Provider 熔断或进程异常后，同命令重跑从 frontier 和 hypothesis checkpoint 恢复。恢复结果必须与不中断执行在相同输入、模型输出和预算下语义等价。

## 9. 输出模型

归因报告在保留现有兼容摘要的同时增加：

- `defect_states`
- `causal_relations`
- `hypotheses`
- `recursive_paths`
- `investigation_journal`
- `confirmed_roots`
- `co_roots`
- `contributing_conditions`
- `amplifying_factors`
- `rejected_candidates`
- `unresolved_hypotheses`
- `root_confirmations`
- `analysis_perspective`
- `provider_and_cache_metrics`

每个根因必须能回溯到：起始缺陷、递归路径、Defect State 转换、支持与反对证据、调查动作和独立确认。

跨运行 benchmark 比较使用离线 `semantic_anchor_id`，由 case ID、semantic role、规范化节点正文、代码位置、action identity 和关键 artifact hash 计算。原始 record ref 仍保留用于单次 Trace 审计。

## 10. 错误处理

- Judge schema 不合法：定向 repair；仍失败则该判断为 unknown，不缓存。
- 引用不存在：拒绝判断并记录 fabricated/unresolved ref，不沿该边递归。
- Provider 连续错误：打开 circuit，持久化 frontier 和假设后返回 inconclusive。
- Artifact 缺失：保留缺口，尝试其他证据；不得推测缺失正文。
- 多假设无法区分：输出 unresolved competing hypotheses，不强行选根因。
- 独立确认拒绝：回溯到下一候选，并保留 rejection reason。
- 数据流缺边：按需使用语义检索，所有新增连接标记 inferred 并降低证据等级。

## 11. 测试策略

### 11.1 单元测试

- 同缺陷传播递归；
- 缺陷转化后切换 Active Defect State；
- contributing condition 不被错误递归为同缺陷；
- 多候选分支创建、合并、拒绝和回溯；
- 循环检测与同节点不同 defect state；
- unknown 触发按需调查；
- 独立确认拒绝后恢复替代假设；
- Provider 熔断和 checkpoint resume；
- 缓存键随 defect/hypothesis/context 改变而失效；
- 不存在引用和缺失 artifact 保持 inconclusive。

### 11.2 合成 Stress Cases

- Prompt 不完整，但 Agent 有仓库检索义务且过早收敛；
- Prompt 明确给出错误要求，Agent 忠实执行；
- 上下文压缩删除关键约束；
- Tool 返回错误但 Agent 忽略；
- Subagent 发现风险但主 Agent 未采纳；
- MCP/Skill 输出正确但后续转换损坏；
- 多个独立缺陷共同导致最终失败；
- Causal IR 缺少一条直接边，需要按需语义检索；
- Harness timeout 只放大结果而非引入实现缺陷。

### 11.3 开源 Benchmark 回归

- Sphinx：主要可优化根因应召回语义对应的 `dec_85`；prompt omission 和 timeout 分别作为促进因素和放大因素保留。
- Pydantic：验证不同缺陷转换链和根因排序。
- Seaborn 成功或负样本：不得因为存在复杂工具链而制造根因。
- 至少补充一种需求理解类 benchmark 和一种上下文压缩类 benchmark。

## 12. 验收标准

1. Sphinx 中不再只输出 prompt root；报告必须比较 prompt、Agent decision 和 Harness timeout 三个假设。
2. 在 Harness 优化 perspective 下，语义对应 `dec_85` 进入 confirmed root 或明确的 unresolved top candidate，且给出支持、反对和缺失证据。
3. 在需求质量 perspective 下，模块仍可将 prompt omission 判为主要根因，不受固定组件规则限制。
4. 每个递归步骤都能审计当前 Defect State、前驱关系、直接证据和下一步选择。
5. 人工标注回归集中，候选根因召回不低于当前版本；Top-1 一致率应提高，具体基线在实施前冻结。
6. 相比 Sphinx 旧版 196 次 Judge 请求，优先递归与按需调查的请求数降低至少 50%，同时保留人工根因召回。
7. 任何 fabricated ref、未知判断、预算终止或 Provider 失败均不能成为 confirmed root。
8. 中断重跑复用所有语义等价的已校验判断、frontier 和 hypothesis 状态。
9. 归因模块仍是纯离线 sidecar，对 Agent、Trace 和 benchmark 行为影响为零。

## 13. 实施边界

本设计作为一次独立归因模块重构实施，按以下顺序落地：

1. Defect State、Causal Relation、Hypothesis 和 Frontier 数据模型；
2. 递归调度、循环检测、分支合并和回溯；
3. Relation Judge 上下文与 schema；
4. 按需 Agentic 调查工具；
5. 独立根因确认和多因素报告；
6. checkpoint、缓存迁移和 CLI；
7. 合成 case 与开源 benchmark 对比。

Trace 采集侧新增字段、HTML 可视化和大规模分布式归因不属于本次实现；它们根据本轮 `trace_improvement_report` 和归因效果在后续迭代处理。
