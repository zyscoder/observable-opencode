# Loop 2：语义 Trace 与离线归因联合优化报告

## 一、目标

本轮同时优化语义 trace 与离线后向语义污点分析，遵守以下边界：

- trace 仅被动记录事实，不向 agent 反馈诊断结果，不改变 agent 决策与执行行为；
- Ground Truth 仅用于外部评测，不作为 observed defect 或归因起点注入；
- 判断器超时、格式错误、搜索边界等分析失败不得伪造为缺陷根因；
- 成功案例必须允许输出 `no_defect`，而不是强制寻找根因。

## 二、实施内容

### 2.1 Trace 事实层

1. 确定性质量评审将 `response.output`、`response.claim`、`decision`、`design.record` 统一纳入回答语义面，修复完整回答中已有风险分析但被判缺失的问题。
2. `context.compaction` 增加以下浅层事实：
   - `algorithm_version`
   - `input_message_count`
   - `compaction_request_message_count`
   - `output_message_count`
3. 上述字段来自实际 compaction 输入和输出，只写 trace，不参与 compaction 决策。

### 2.2 离线归因层

1. 删除 Ground Truth 根因向 `case.observed_defect` 和 `case.missing_semantic` 的隐式注入；只有显式 `observed_defects` 才能创建离线 observed-defect 节点。
2. 节点判断从二态扩展为：
   - `present`：确认存在与分析目标相关的缺陷；
   - `absent`：确认不存在相关缺陷；
   - `unknown`：信息不足或判断失败，不能当作无缺陷或根因。
3. 判断器输出必须包含状态、原因、影响节点、根因标记和置信度；合法但字段不完整的 JSON 也会触发一次修复。
4. `case.quality_gap`、`case.observed_defect`、`case.missing_semantic` 被视为待校验断言；若其引用的上游节点全部无缺陷，该断言被拒绝。
5. judge error、未解析引用、`max_depth` 和 `max_nodes` 不再生成边界根因。
6. 报告增加：
   - `analysis_outcome=root_found|no_defect|inconclusive`
   - `termination_reason=queue_exhausted|depth_limit|node_limit`
7. 成功 trace 默认从最后一个完整 `response.output` 开始，而不是优先选择带质量标记的孤立 claim。
8. `TraceNode.compact()` 优先保留 `text`、结构化事实、质量标记和证据摘要，避免长引用数组挤掉核心语义。
9. 显式分析 `response.claim` 时，优先将其父级 `response.output` 放入上游判断上下文。

## 三、回归结果

### 3.1 确定性质量评审

复用 Loop 1 的四份原始 trace 重新评审：

| Case | Trace sufficiency | Quality | Quality gaps |
|---|---|---:|---:|
| tool-failure-hallucination | sufficient | - | 0 |
| ignored-mcp-fact | sufficient | - | 0 |
| compaction-lost-constraint | sufficient | - | 0 |
| semantic-architecture-boundary | sufficient | 100/80 | 0 |

架构案例旧结果错误认为缺少 risk assessment；本轮识别到 `response.output` 中的风险、替代方案和验证策略后恢复为满分。

### 3.2 DeepSeek 同 Trace 对照

模型、原始 trace、目标和搜索边界保持一致，仅替换归因模块实现。

| Case | v1 outcome | v1 roots/errors | v2 outcome | v2 roots/errors |
|---|---|---:|---|---:|
| tool-failure-hallucination | inconclusive | 0/0 | no_defect | 0/0 |
| ignored-mcp-fact | no_defect | 0/0 | no_defect | 0/0 |
| compaction-lost-constraint | inconclusive | 0/1 | no_defect | 0/0 |
| semantic-architecture-boundary | root_found（假根因） | 1/0 | no_defect | 0/0 |

v2 四例均从完整最终回答开始，全部得到 `no_defect`，无根因、无 judge error。架构案例置信度为 1.0，压缩与工具失败案例不再因文本缺失而进入 unknown。

## 四、仍存在的问题

### 4.1 Trace 语义缺口

1. 完整回答可能分布在多个 `response.output` 中，最后一个 segment 未必包含此前的需求理解、方案取舍和风险分析。
2. response、claim 的 `source_refs`、`direct_evidence_refs`、metadata 中存在较多重复，且边的直接证据、上下文和仅候选关系没有统一优先级。
3. compaction 已有算法版本和数量，但仍缺少逐层转换清单、保留/丢弃原因、summary 对原消息和事实的覆盖映射。
4. claim support 的 `weak_evidence_match` 仍可能对正确的范围声明产生假阳性。
5. 完整 `case-trace.test.ts` 当前有 22 个既有契约失败，主要是旧断言禁止 `evidence_refs` 或期待旧字段布局，需要单独收敛测试基线。

### 4.2 归因模块缺口

1. judge 首次响应、修复响应、校验失败原因没有持久化；本轮曾观察到一次 repair 返回空文本，只能从最终 error 间接判断。
2. 当前上游选择主要依赖节点类型，尚未利用 dataflow edge 的 relation、direct/contextual/candidate 角色做预算排序。
3. 对完整回答和原子 claim 的判断仍是单层模式，缺少“回答整体 -> 可疑 claim -> 生成/证据链”的分层下钻。
4. `max_depth`、`max_nodes` 已不再制造根因，但仍是静态预算；复杂 trace 需要根据剩余因果前沿动态扩展。
5. 当前 `no_defect` 是模型判断结果，尚未与外部 Ground Truth/benchmark 得分做隔离后的自动校准统计。

## 五、冗余信息判断

- `response.output.data.metadata` 与浅层字段重复保存同一批 refs，价值低且会占用判断提示预算；
- 大量 legacy/candidate context refs 对展示有用，但不应与直接因果前驱同优先级进入归因 prompt；
- 原始流式 delta 不应进入语义图，继续只留在 raw events；
- 同一 payload 的 artifact 引用、preview 和内联副本需继续去重，但不能删除可回看全文的 artifact。

## 六、Loop 3 联合方案

### 6.1 Trace 事实层

1. 增加被动派生的 final-answer bundle，聚合本轮所有 final `response.output`、claim 和生成 provenance，保留 segment 顺序。
2. 为 dataflow edge 增加语义角色和消费关系：direct evidence、context、candidate、generated-by、split-from、verified-by。
3. 增加 compaction retention manifest：转换阶段、算法参数、保留/丢弃原因、消息与事实覆盖映射、summary 来源映射。
4. 收敛 response/claim 重复 refs：trace JSON 保留规范字段，大文本和大列表继续通过 artifact 在 HTML 中展开。
5. 修复 `weak_evidence_match` 对范围声明和“不修改某目录”声明的假阳性规则。

### 6.2 离线归因层

1. 持久化每个节点的 judge attempt、原始响应摘要、修复响应、schema 校验结果与耗时，支持断点恢复。
2. 基于 edge relation 和语义角色进行上游预算排序，保证生成父节点和直接证据不会被候选 refs 挤出。
3. 实现分层判断：final-answer bundle 整体判断；仅在 present/unknown 时下钻到 claim；再沿生成、上下文、工具和证据链后向遍历。
4. 将动态搜索边界改为因果前沿驱动，并在报告中记录未访问前沿与预算估算。
5. 外部评测器读取 Ground Truth，对 root/no-defect/unknown 做校准统计，但不把标签注入判断 prompt。

### 6.3 Loop 3 验收

- 四个本轮成功案例继续保持 `no_defect`；
- 至少加入两个真实失败案例，能定位到非 evaluation、非 answer-surface 的缺陷引入节点；
- judge 中断后可从节点 checkpoint 恢复，不重复已完成调用；
- final-answer bundle 可回溯到所有 segment、claim、LLM、context、tool/MCP/skill 和 verification 事实；
- trace 插装仍满足 `collection_mode=passive_sidecar`、`behavior_impact=none`。

## 七、验证记录

- Node stress review：11/11 通过；
- Python attribution：39/39 通过；
- 本轮 compaction provenance 聚焦测试：1/1 通过；
- TypeScript `tsgo --noEmit`：通过；
- 完整 `case-trace.test.ts`：67 通过、22 个既有契约失败，需在 Loop 3 作为测试基线收敛项处理。
