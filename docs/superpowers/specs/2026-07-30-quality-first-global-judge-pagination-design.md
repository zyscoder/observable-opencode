# Quality-first Global Judge 分页与跨页收敛设计

## 1. 背景

动态候选预算已经允许单个 seed 最多保留 256 个有事实依据的候选，但当前
Global Judge 仍把全部候选放入一次请求。候选规模增大后会出现三类问题：

1. 单次 prompt 过大，模型容易漏评候选、混淆因果路径或输出不完整矩阵。
2. seed 级 checkpoint 只能恢复整次 Global Judge，无法精确恢复已完成的局部判断。
3. 最终选择缺少显式的跨组比较过程，无法区分页内候选、跨页 finalist 和最终待确认根候选。

本轮只改造离线归因路径，不修改 Agent 的 prompt、上下文、工具调用或执行结果。

## 2. 目标

- 每次 Judge 只接收最多 4 个完整 Candidate Evidence Capsule。
- 将“待判根因候选”与“只提供语义事实的证据上下文”分离，后者可被引用但
  不进入 assessment 矩阵。
- 对 25 至 256 个候选进行确定性分页，并记录候选为何进入某一页。
- 页内独立判断缺陷状态、传播角色、反事实和证据缺口。
- 保留多个根因假设和所有尚未被证据排除的 unresolved 候选。
- 将页内结果汇总为不超过 48 个可比较 finalist；超过 4 个时继续分轮比较。
- 最终仍由现有 Global Judge 选出最多 3 个根候选，再进入独立根因确认。
- 每个页面调用都有独立、可校验、可恢复的 checkpoint 生命周期。
- 单页失败不污染其他页；只要有未完成页，seed 不得被错误标记为完整收敛。

## 3. 非目标

- 不由分页器推断根因。
- 不用检索排名、启发式分数或页面顺序替代 Judge 语义判断。
- 不改变 Agent 行为，也不将离线归因结果反馈给 Agent。
- 不压缩、摘要或裁切 Candidate Evidence Capsule 的事实字段。
- 不为了满足 finalist 数量上限而静默丢弃 unresolved 候选。

## 4. 核心模型

### 4.1 Candidate Funnel v3

`candidate-budget-funnel/v3` 将发现节点划分为三类：

1. `offered`
   - 存在至少一条通往当前 active seed 的合格因果路径。
   - 进入 Global Judge assessment 和分页。
2. `evidence_context`
   - 没有合格因果路径，不能被确认成 root、condition 或 amplifier。
   - 仍以完整 Evidence Capsule 进入请求，可作为 supporting/counterevidence
     被引用，但不要求模型输出候选角色、路径和反事实。
3. `dropped`
   - 仅因已签名的总候选预算上限而未进入本轮请求。

漏斗为每个节点记录 `assessment_eligible`、`disposition`、offered/context rank
及原因。调度顺序先保留全部合格 assessment candidates，再用剩余预算保留
evidence context，避免无路径证据挤掉真正根候选。

Global Judge validation envelope 升级为 v10，分别持久化：

- `candidate_evidence_capsules`
- `evidence_context_capsules`

两类 capsule 都进入 grounded refs、请求身份、capsule identity、checkpoint
一致性校验和最终 pass 审计，但只有第一类进入 comparison contract。

离线评审注入还必须校验 `observed_defect.record_refs` 是否能解析到当前 Trace
revision。若声明引用全部失效，注入器不得静默保留断链节点，而应把外部评审
事实绑定到当前最终响应 claim/output，并在 `source_binding` 中记录声明引用、
失效引用、fallback refs 和绑定依据。该 fallback 只表达“外部评审评价了本次
最终结果”，不包含人工根因标签，也不改变 Agent 运行事实。

### 4.2 Candidate Page Plan

`CandidatePagePlan` 是输入候选到页面的确定性投影：

- schema：`global-candidate-page-plan/v1`
- page size：4
- 输入身份：seed、defect fingerprint、候选 capsule identity 列表
- 页面身份：输入身份、round index、page index、页面候选身份列表的哈希
- 完整性：每个输入候选恰好出现一次，不重复、不遗漏
- 稳定性：相同输入得到完全相同的页面和页面身份

初始候选顺序沿用已经签名的 candidate funnel 输出顺序。跨页轮次沿用前一轮
survivor 的稳定顺序，不重新使用检索分数排序。

### 4.3 Page Judgment

页内继续使用现有 `GlobalCandidateJudgeRequest` 和
`GlobalCandidateJudgment`。每个 assessment candidate 仍必须拥有完整 assessment：

- input/output defect status
- causal role
- candidate-to-seed path
- counterfactual
- compared candidates
- grounded evidence
- confidence

Evidence-context capsule 只提供引用事实，不出现在 assessment 列表。页内
`selected_candidate_refs` 只表示该页中已经满足根候选条件的候选，不直接
发布为最终根因。

页级 judgment 增加 `no_root_candidates` outcome，用于表达：

- 当前 active defect 没有被推翻；
- 本页所有 assessment 均已完整判断；
- 本页没有支持为 root 的候选，但可以保留 condition、amplifier 或 outcome
  evidence。

`no_root_candidates` 与 `no_defect` 不可互换。后者只在决定性反证推翻
active defect 本身时使用。Global Candidate Judgment schema 升级为 v10，
避免旧 checkpoint 将页级排除误解释为整案无缺陷。

### 4.4 Survivor Classification

页内结果经过纯事实规则分为：

1. `selected_root_hypothesis`
   - 被页内 judgment 选中。
2. `supported_root_hypothesis`
   - assessment 是 `root_candidate`，输出缺陷存在，反事实可阻止缺陷。
3. `unresolved_root_hypothesis`
   - root-eligible，且 assessment 的状态、角色、路径或反事实仍不完整。
4. `non_root_factor`
   - contributing condition、amplifying factor 或 outcome evidence。
5. `excluded`
   - 有完整事实支持其与根因无关或具有排除性。

分页器只做上述结构化投影，不重新解释自然语言 reason。

### 4.5 Finalist 与 unresolved

- `selected_root_hypothesis` 和 `supported_root_hypothesis` 进入 finalist pool。
- `unresolved_root_hypothesis` 单独进入 unresolved pool，并保留证据缺口。
- `non_root_factor` 进入现有非根因独立确认通道。
- `excluded` 不再进入跨页根候选比较，但完整 assessment 仍保存在审计记录中。

finalist 软上限为 48。若 supported finalists 超过 48，则对它们开始下一比较轮，
而不是按分数截断。若 unresolved 数量使候选无法安全收敛，则结果保持
`inconclusive` 或 `needs_expansion`。

### 4.6 跨页收敛

收敛过程分为三层：

1. **初始分页**
   - 对全部候选逐页判断。
2. **比较轮**
   - 将上一轮 supported finalists 重新按 4 个一页组成完整请求。
   - 每轮都重新比较候选，不复用页内“冠军”作为既定事实。
3. **最终轮**
   - 当 supported finalists 不超过 4 且不存在阻塞性的 unresolved 页面时，
     执行一次最终 Global Judge。
   - 最终 judgment 最多选择 3 个根候选，交给现有独立根因确认。

为防止无限循环，若连续两轮 survivor 集合不变，停止比较并发布
`global_candidate_pagination_stalled`，保留全部 survivors 为 unresolved。

## 5. Checkpoint 与恢复

每页使用独立语义键：

```text
global_judge_page:{pass_identity}:{round_index}:{page_index}:{page_identity}
```

生命周期：

```text
global_judge_page_started
global_judge_page_completed | global_judge_page_failed
```

页面生命周期使用独立 action schema。既有 `global_judge_started/completed/failed`
只用于旧版单次 pass 的读取与兼容，不与页面动作混写。

started 记录：

- page plan identity
- round/page index
- request/capsule identity
- 完整 validation envelope
- assessment/context capsule 双集合身份
- 本页物理请求预留量

terminal 记录：

- 精确物理请求消耗
- canonical judgment 或结构化失败
- evidence expansion history
- provider state

恢复规则：

- completed 页面直接复放，不再请求 provider。
- started 但无 terminal 的页面按现有保守规则记为 interrupted，不重复请求。
- failed 页面不阻止其他页面执行，但阻止 seed 被标记为完整收敛。
- checkpoint 校验必须证明页面身份、候选全集和 provider accounting 一致。

## 6. 预算

质量优先，成本次要：

- page size：4
- initial finalist soft limit：48
- max comparison rounds：4
- deterministic page physical request cap：4
- 每页沿用现有 evidence expansion 上限
- 总物理请求仍受 `max_judge_requests` 硬限制

调度时只给当前页面预留其可消费的上限，不能像当前 seed 级调用一样占用全部剩余
预算。页面预算由 Judge 的单次调用与 expansion 上限推导，默认最多 4 次物理请求。

page size 最初设计为 24，但 FeatureBench Seaborn 实测首个 24 候选页面的
validation envelope 达到约 737 KB，24 个 capsule 合计约 722 KB；降为 8 后
validation envelope 约 259 KB，但模型的原始响应、聚焦修复和完整重试仍都在
约 14.7K 字符处截断，无法形成完整 assessment 矩阵。因此页面上限进一步收敛
为 4，以同时约束输入语义载荷和默认 4096 output token 下的结构化矩阵规模。
该值是质量上限，不是成本优化参数。

page size、page physical request cap、max comparison rounds 和分页策略版本必须进入
checkpoint config fingerprint。分页策略发生变化时，新版本必须拒绝恢复旧
checkpoint；旧产物仍可由生成它的版本只读审计，但不自动迁移或混写新页面动作。

预算不足时：

- 已完成页保持有效。
- 未执行页记为 `budget_exhausted`。
- seed 结果为 partial/inconclusive，不能把已有页 winner 当作全局 winner。

## 7. 审计输出

在 investigation journal 中追加：

- `global_candidate_page_plan`
- `global_candidate_page`
- `global_candidate_round_summary`
- `global_candidate_convergence`

每条记录都包含 `behavior_impact=none_offline_analysis_only`。

最终 `global_candidate_pass` 保留兼容入口，但增加分页摘要：

- page count / completed / failed
- round count
- supported finalist refs
- unresolved refs
- excluded count
- convergence status
- final judgment identity
- assessment candidate 与 evidence-context capsule 的完整事实列表

## 8. 失败语义

- 单页 schema 无效：该页 failed，其他页继续。
- 单页 provider 错误：记录精确请求数，其他页继续。
- evidence expansion 未满足：候选进入 unresolved，不作负面淘汰。
- 跨页集合不收敛：stalled，保留 survivors。
- 预算耗尽：partial，保留已完成页事实和未执行页清单。
- checkpoint 身份不一致：拒绝恢复，不能容错为新调用。

## 9. 验收标准

1. 256 个候选被完整、无重复地分成 64 页。
2. 任一 Judge 请求最多包含 4 个需要 assessment 的 capsule；evidence-context
   capsule 不进入 assessment 矩阵，但仍被完整签名和审计。
3. 页内失败不阻止其他页执行。
4. 恢复时 completed 页面产生 0 次 provider 调用。
5. unresolved 候选不因 48 个 finalist 软上限而丢失。
6. 最终根候选必须来自最终轮 judgment，不能由分页器直接发布。
7. Sphinx、Seaborn、Pydantic 已知根候选仍进入最终比较或 unresolved 集合。
8. 全量测试通过，离线归因不改变 Agent 行为。
9. 无合格因果路径的节点不得被要求输出 causal role，但其事实仍可作为
   grounded evidence 被最终判断引用。
