# CandidateClusterManifest Shadow Clustering 实施计划

## 范围

实现确定性、只读的候选分组事实层，并把 manifest 作为 checkpoint/report 审计事件持久化。本轮不启用 ClusterTriage，不改变候选放行和 LLM 判断。

## 任务

### 1. 先建立失败测试

- 新增 `test_candidate_clustering.py`；
- 覆盖 L0-L3 分组优先级；
- 覆盖候选零丢失、零重复、稳定身份和篡改拒绝；
- 覆盖代表节点、分布、证据引用与 disposition；
- 覆盖人工标签不会影响 manifest；
- 覆盖 restoration obligation 只通过 scope/path 事实绑定。

### 2. 实现领域模型

- 新增 `candidate_clustering.py`；
- 实现 `CandidateFact`、`CandidateCluster`、`CandidateClusterManifest`；
- 实现 `build_candidate_cluster_manifest()`；
- 复用 `CausalEpisodeIndex`、episode role、action identity、root eligibility 和 Candidate Budget identity；
- 所有对象支持严格 `to_dict()/from_dict()` 与 SHA-256 身份校验。

### 3. 接入 shadow audit

- 在 `_global_candidate_pool()` 完整发现集之后生成 manifest；
- 以 `candidate_cluster_manifest_shadow` 写入 `investigation_journal`；
- 绑定 Candidate Budget `selection_identity`；
- 同一 seed binding/selection identity 幂等去重；
- 同一 selection 产生不同 manifest 时拒绝确定性冲突；
- checkpoint snapshot 和 report validation 重新校验 manifest；
- 保持 `_global_candidate_pool()` 原返回值及 Candidate Budget funnel 不变。

### 4. 行为隔离回归

- 对比接入前后的 offered refs、evidence-context refs 和 candidate funnel identity；
- 对比 CandidatePagePlan identity；
- 对比原始 Trace 与 Agent-visible provider/tool/message 投影 hash；
- 验证 attribution 模块不产生在线模型或工具调用。

### 5. 多 Benchmark 离线评估

- Sphinx：小规模回放与根因组覆盖；
- Pydantic：537 候选压力样本的完整性与模拟页数；
- Seaborn：不同代码结构上的分组稳定性；
- 输出人工根因所在组、组成员、代表覆盖与压缩潜力。

### 6. 完整验证与报告

- 运行 candidate clustering 定向测试；
- 运行 checkpoint/report/offline isolation 回归；
- 运行 trace attribution 全量测试；
- 执行 `git diff --check`；
- 更新本轮结果报告并给出是否满足 ClusterTriage 启用门槛的结论。
