# Responsibility-aware Omission 与 Episode-diverse Reserve 设计

## 1. 背景

当前 Global Judge 用 `input_defect_status` 和 `output_defect_status` 描述缺陷在节点
前后的物理状态，并要求 `root_candidate` 的输入缺陷不能为 `present`。该约束只适合
“节点正向引入缺陷”的场景，不适合需求开发任务中的“责任性遗漏”：

- 代码仓基线或被遮蔽的 baseline 已经缺少某项能力；
- Agent 在本次任务中承担恢复、补齐或迁移该能力的责任；
- Agent 已经获得需求、代码和验证证据，也拥有可执行的修复窗口；
- Agent 最终关闭任务时仍未履行义务，导致缺陷保留。

这种情况下，缺陷在节点输入和输出都为 `present`，但责任性遗漏仍可能是本次执行
效果不佳的根因。若继续把物理状态与责任判断绑定，离线归因会结构性漏掉遗漏根因。

候选预算还有另一类风险：发现层虽可找到 512 个节点，评审层仍只接收 256 个。
按 Trace 时间倒序保留候选会偏向晚期执行节点，可能丢掉早期计划、范围界定和架构
决策。提高总上限会显著增加分页和模型调用，不能替代语义多样性保留。

## 2. 目标

1. 将物理缺陷传播、规范性修复责任、修复窗口和因果角色建模为正交事实。
2. 支持 `positive_introduction` 与 `responsible_omission` 两类根候选。
3. 严格区分责任性遗漏、普通未修复和有机制的贡献条件。
4. 将恢复义务作为离线事实提供给归因模型，不反馈给 Agent。
5. 使用 episode-diverse reserve，在 256 个评审预算内同时保留早期计划和晚期执行。
6. 保持旧 v10 产物只读可审计，但禁止其静默推断 v11 的遗漏语义。
7. 使严格评估器与运行时使用同一 TraceGraph 规范化路径。

## 3. 非目标

- 不让 Trace 插装改变 Agent 的 prompt、上下文、工具结果或控制流。
- 不在运行时向 Agent 注入恢复义务、人工标签或归因结果。
- 不把“缺陷经过某节点仍存在”自动解释成该节点有责任。
- 不使用人工根因标签参与候选发现、排序或 Global Judge prompt 构造。
- 不在本轮把评审上限从 256 直接提高到 512。

## 4. 双层因果语义

### 4.1 物理缺陷状态

继续保留：

- `input_defect_status`: `present | absent | unknown`
- `output_defect_status`: `present | absent | unknown`

它们只回答缺陷是否存在，不回答谁应当修复、谁对结果负责。

### 4.2 责任与义务状态

新增恢复义务 `RestorationObligation`：

```json
{
  "obligation_id": "obl_...",
  "kind": "observed_defect_remediation",
  "baseline_state": "preexisting_missing",
  "required_end_state": "defect_absent",
  "required_capabilities": ["model dependency recovery"],
  "scope_refs": ["record:..."],
  "acceptance_evidence_refs": ["record:..."],
  "provenance": {
    "source": "quality_review",
    "source_refs": ["record:..."]
  },
  "visibility": "offline_judge_only"
}
```

Global Candidate Assessment v11 增加：

- `responsibility`: `primary | shared | none | unknown`
- `candidate_phase`: `diagnostic | planning | intermediate | implementation | closure | final`
- `obligation_status_before`: `pending | satisfied | violated | unknown`
- `obligation_status_after`: `pending | satisfied | violated | unknown`
- `repair_window_effect`: `remained_open | closed | unknown`
- `failure_mode`
- `obligation_refs`
- `contribution_mechanism`

`failure_mode` 取值：

- `positive_introduction`
- `responsible_omission`
- `ordinary_non_repair`
- `omission_enabling_condition`
- `none`
- `unknown`

## 5. 判定规则

### 5.1 正向引入

`root_candidate + positive_introduction` 必须满足：

- 输入缺陷为 `absent | unknown`；
- 输出缺陷为 `present`；
- 候选到 seed 的事实路径完整；
- 反事实干预为 `replace_with_semantically_correct_behavior`；
- 反事实预测缺陷为 `absent`，作用为 `prevents`。

### 5.2 责任性遗漏

`root_candidate + responsible_omission` 允许输入和输出缺陷都为 `present`，但必须
同时满足：

- `responsibility` 为 `primary | shared`；
- 至少引用一个当前请求中的恢复义务；
- 义务在节点前为 `pending`；
- 义务在节点后为 `violated`；
- 节点位于 `closure | final` 边界；
- 修复窗口被关闭；
- 反事实干预为 `replace_with_obligation_satisfying_behavior`；
- 反事实预测缺陷为 `absent`，作用为 `prevents`；
- 理由和证据能够证明责任范围、修复机会和关闭边界。

若修复窗口仍开放，节点只能是 `ordinary_non_repair`，不能成为遗漏根因。责任、
义务或关闭边界证据不足时必须返回 `needs_expansion` 或 `inconclusive`。

### 5.3 贡献条件

输入和输出缺陷均为 `present` 不足以证明贡献。`contributing_condition` 必须提供：

```json
{
  "type": "scope_narrowing",
  "target_ref": "record:...",
  "effect": "将后续修复范围限制为 computed_field",
  "evidence_refs": ["record:..."]
}
```

机制类型：

- `scope_narrowing`
- `misleading_success_signal`
- `faulty_assumption`
- `repair_opportunity_consumption`

`ordinary_non_repair` 不得携带贡献角色；没有机制和目标的 present-to-present 节点
应判为 `unrelated` 或 `outcome_evidence`，而不是贡献条件。

## 6. Evidence Capsule v8

每个候选 capsule 增加离线责任事实：

- 可适用的恢复义务及其 provenance；
- 候选所在 progress/materialization episode；
- 候选 phase；
- 候选到任务关闭边界和 active defect 的事实路径；
- 义务 scope 与候选输入、输出、验证证据之间的引用关系。

这些字段只来自已记录 Trace、外部质量评审和 benchmark 评审事实，不从人工根因
标签生成。Capsule identity 必须覆盖义务和 episode 字段，防止旧 checkpoint
在事实变化后被错误复放。

## 7. Episode-diverse Reserve

发现层维持最多 512 个 grounded candidates，评审层维持最多 256 个。保留流程：

1. 从 candidate-to-seed path 提取最近的 materialization/progress episode。
2. 对每个 episode 分别识别：
   - 最早 authored planning/decision 节点；
   - 最晚 implementation/verification/closure 节点。
3. 按 episode 稳定顺序 round-robin 保留上述代表。
4. 剩余预算再按现有 grounded quality order 填充。
5. 无法解析 episode 的节点进入独立 fallback bucket，不得静默丢失。

审计字段：

- `grounded_hops`
- `episode_key`
- `episode_role`: `early_plan | late_execution | fallback`
- `reserve_rank`
- `reserve_disposition`
- `reserve_reason`

该策略不判断根因，只保证有限评审预算中的阶段覆盖。人工标签不得参与 episode
提取、代表选择或排序。

## 8. 协议与兼容

- Global Candidate Judgment：`global-candidate-judgment/v11`
- Validation Envelope：`global-candidate-validation-envelope/v11`
- Evidence Capsule：`candidate-evidence-capsule/v8`
- Comparison Contract：`global-candidate-comparison-contract/v2`

v10/v7 产物可只读展示，但不能自动补出 responsibility、obligation 或
responsible omission。检查点版本不匹配时必须重新判断。

## 9. 行为隔离

新增事实的生产、存储和消费全部位于离线归因路径。验证至少包括：

- 开启/关闭离线归因时，Agent 输入消息 hash 一致；
- 工具调用参数和工具结果 hash 一致；
- LLM provider 请求 hash 一致；
- 最终 Agent 输出 hash 一致；
- 新增字段均声明 `visibility=offline_judge_only` 或
  `behavior_impact=none_offline_analysis_only`。

## 10. 验收

1. 合成遗漏案例可把关闭边界判为责任性遗漏根候选。
2. 同一案例中的诊断和中间编辑节点不得因 present-to-present 被误判为 contributor。
3. 正向引入案例保持原有判定能力。
4. Pydantic 案例不再出现全页 `no_root_candidates` 的结构性死局。
5. Seaborn 的早期计划和晚期执行人工根候选都进入 256 评审池。
6. 默认 128 次请求预算不足以完成全部页面时，不得发布伪全局根因。
7. 严格评估器可直接读取 raw `partial/latest.json` 并走与 CLI 相同的规范化。
8. 全量测试通过，且 Agent 行为隔离 hash 不变。
