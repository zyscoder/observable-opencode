# CandidateClusterManifest Shadow Clustering 设计规格

## 1. 背景与目标

当前 Global Candidate Judge 在大型 Trace 上先发现完整候选集，再通过固定质量优先预算将候选收敛到最多 256 个，并以每页 8 个候选交给 LLM 判断。该方案已经能够保住人工根因，但 Pydantic 压力样本仍出现以下问题：

- 完整发现 537 个候选，实际送审 256 个，仍需 32 个初始页面；
- 同一 action、同一 causal episode 或同一物化链上的候选被拆散，LLM 重复读取近似事实；
- 当前 episode reserve 只负责排序和保留少量代表，不能证明被省略成员仍可回放；
- 如果直接启用 LLM 聚类或组级裁剪，可能在没有覆盖证明的情况下丢失根因。

本轮目标是先建立一个**只读、可回放、零丢失的候选分组事实层**。它只生成并持久化 `CandidateClusterManifest`，不改变候选筛选、分页、提示词、LLM 调用和最终归因结论。

## 2. 非目标

本轮不实现以下行为：

- 不让 cluster 直接成为根因候选；
- 不根据人工标签、评审结果或 LLM 输出生成 cluster；
- 不用 cluster 调整 `select_global_candidates()` 的输入或输出；
- 不减少 Global Judge 页面；
- 不修改 Agent、Harness、工具调用或原始 Trace；
- 不把 shadow 统计反馈给在线 Agent。

## 3. 分层分组模型

每个已发现候选必须且只能进入一个叶组，按以下优先级选择分组依据：

1. **L0 exact action group**
   - 使用 Trace 已记录的 `action_group_id` 或 `call_id`。
   - 用于绑定计划、调用、结果、变更等属于同一真实动作的数据。
2. **L1 confirmed causal episode**
   - 使用 `CausalEpisodeIndex` 基于 confirmed lineage、call/span identity 和显式执行边构造的 episode。
   - 只消费 Trace 中已有的确认关系，不做语义猜测。
3. **L2 materialization episode**
   - 使用候选到 seed 的已落盘路径中的显式 episode/materialization ID 或物化节点锚点。
   - 用于关联同一实现、验证或结果落盘链。
4. **L3 structural fallback**
   - 只使用结构化事实：组件、事件类型、episode role、根因资格、工具名、文件、符号、路径尾部和候选来源。
   - 不读取自由文本相似度，不调用 LLM，不使用 embedding。

分层优先级用于选择叶组，不表示较低层信息会丢失。每个 `CandidateFact` 同时保存可获得的 L0-L3 事实，便于后续构建真正的父子层级。

## 4. CandidateFact

每个完整发现候选生成一条不可变事实记录，至少包含：

- `ref`、`candidate_identity`、`discovered_rank`、Trace position；
- component、event type、candidate source、edge relation；
- `root_candidate_eligible`；
- episode role；
- action identity；
- confirmed causal episode identity 及成员数；
- materialization episode identity；
- candidate-to-seed 完整路径和稳定路径尾部；
- 结构化工具名、文件、符号；
- 适用的 restoration obligation ID；
- 被选择的叶组层级、分组依据和 cluster ID。

`candidate_identity` 复用 Candidate Budget 的事实身份算法，忽略检索分数等非语义排序噪声。

## 5. CandidateCluster

每个叶组必须保存：

- 稳定 `cluster_id`、层级、分组键和分组依据；
- 完整 `member_refs`、成员身份和原始发现顺序；
- root-eligible 成员；
- role、event type、component、source、action identity、obligation 的分布；
- 代表节点：
  - earliest authored plan；
  - latest execution；
  - latest verification；
  - latest closure；
  - latest root-eligible candidate；
- 已落盘支持证据和反对证据引用；
- `expansion_status=shadow_only` 与原因；
- 每个成员最终 disposition，来自现有 Candidate Budget，不能由 cluster 改写。

代表节点用于未来组级预筛，不代表其他成员可以删除。

## 6. Manifest 完整性契约

`CandidateClusterManifest` 必须满足：

- 发现候选 ref 无重复；
- 每个发现候选恰好属于一个叶组；
- cluster 成员并集严格等于发现候选全集；
- cluster 成员之间无交集；
- CandidateFact 的 cluster ID 与 cluster 成员表双向一致；
- 所有 ref 均可在当前 active revision 图中解析；
- 不包含 manifest 生成输入之外的候选；
- `candidate_set_identity` 绑定候选身份与原始顺序；
- `source_selection_identity` 绑定产生 manifest 的 Candidate Budget Selection；
- `manifest_identity` 绑定全部无签名内容；
- `from_dict(to_dict(manifest))` 身份完全一致；
- 篡改成员、顺序、依据或 disposition 后必须校验失败。

## 7. 接入方式

接入点位于 `_global_candidate_pool()`：

1. 构造完整 `canonical_candidates` 和 candidate-to-seed path；
2. 执行现有 `select_global_candidates()`；
3. 使用 `selection.discovered`、完整路径、现有 funnel disposition 和
   `selection_identity` 构造 manifest；
4. 以精确 schema `candidate-cluster-shadow-event/v1` 的
   `candidate_cluster_manifest_shadow` 事件追加到
   `investigation_journal`；
5. 继续返回原有 `selected, paths, funnel`，不得改变顺序和内容。

事件随现有 action-state checkpoint 与最终 report 的 `investigation_journal` 持久化。恢复和报告验证时重新校验 manifest 自身身份及图引用。

事件以 `seed_binding_identity + source_selection_identity` 幂等。同一来源
selection 若重建出不同 `manifest_identity`，必须作为确定性冲突失败，不得
追加第二份或静默覆盖。

## 8. 离线验收指标

人工标签只允许在独立评估阶段使用：

- candidate membership recall = 100%；
- candidate membership precision = 100%；
- duplicate membership = 0；
- human root group coverage = 100%；
- simulated expansion root recall = 100%；
- manifest replay identity = 100%；
- ungrounded refs = 0；
- 原始候选选择、分页计划及 Agent-visible Trace hash 与未启用 shadow 时一致。

同时统计组级模拟压缩潜力：

- 叶组数量；
- singleton 比例；
- root-eligible 组数量；
- 每组多代表策略下的模拟初始页数；
- 完整 256 候选分页相较模拟分页的信息量下降比例。

目标值是把 Pydantic 的模拟初始页数降到 16-20 页，但本轮不据此真实减少页面。

## 9. 后续启用门槛

只有上述完整性指标在 Sphinx、Pydantic、Seaborn 均通过，才进入下一阶段：

1. 新增独立 `ClusterTriage`，只选择需要展开的组；
2. 被选组必须展开为原始候选后交给现有严格 Judge；
3. cluster 永远不能直接确认为根因；
4. 未展开组仍保留完整 manifest 与可恢复状态；
5. 一旦根因覆盖证明不足，自动回退到当前完整候选分页。
