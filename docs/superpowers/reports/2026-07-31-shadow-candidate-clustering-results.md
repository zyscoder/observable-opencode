# CandidateClusterManifest Shadow Clustering 迭代结果

## 1. 本轮结论

本轮已经建立完整、确定性、只读的 `CandidateClusterManifest` 事实层，并以
`candidate-cluster-shadow-event/v1` 事件接入 checkpoint 和最终 report。

该事实层不会改变：

- Candidate Budget 的 discovered/offered/evidence-context/dropped 结果；
- Candidate Page Plan；
- Global Judge 请求；
- Agent、Harness、工具调用和原始 Trace；
- 最终根因发布逻辑。

真实开源 Benchmark 回放达到进入下一阶段 shadow `ClusterTriage` 的覆盖门槛：

- 候选成员覆盖率：100%；
- 重复成员：0；
- 人工根因组覆盖率：100%；
- 模拟展开根因召回率：100%；
- 未落地图引用：0；
- Pydantic 确定性复跑：完整输出字节级一致；
- Provider 请求：0。

## 2. 实现内容

### 2.1 四级事实分组

候选按确定性优先级进入唯一叶组：

1. L0：真实 `action_group_id/call_id`；
2. L1：confirmed causal episode；
3. L2：candidate-to-seed 物化锚点；
4. L3：组件、事件、root eligibility、工具、文件、符号和路径形状的结构化
   签名。

每个候选同时保存完整 `CandidateFact`，每个组保存完整成员、原始发现顺序、
成员身份、代表节点、root-eligible 成员、角色分布、证据引用和现有漏斗
disposition。

### 2.2 身份与回放

manifest 建立三层绑定：

- `candidate_identity`：与 Candidate Budget 共享的候选语义身份；
- `candidate_set_identity`：绑定候选身份与发现顺序；
- `source_selection_identity`：绑定来源 Candidate Budget Selection；
- `manifest_identity`：绑定完整 manifest 内容。

事件使用 `seed_binding_identity + source_selection_identity` 幂等。相同
selection 若产生不同 manifest，直接报告确定性冲突。

### 2.3 严格校验

checkpoint、report 和 evaluator 均检查：

- 精确事件 schema；
- manifest、selection、seed 和 defect 绑定；
- 每个候选恰好属于一个叶组；
- cluster 与 CandidateFact 双向成员一致；
- case、position、component、event type、episode、action identity、path
  shape 和 root eligibility 与 active graph 一致；
- 身份篡改和外来/过期图引用被拒绝。

## 3. Benchmark 结果

### 3.1 Sphinx

两个事实分支结果一致：

| discovered | offered | offered groups | 当前页 | shadow 目录页 | 根因覆盖 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | 5 | 4 | 1 | 1 | 1/1 |

`record:decision` 与对应物化节点位于 confirmed causal episode 中，人工根因
是组代表。

### 3.2 Pydantic FeatureBench

三个显式 observed-defect seed：

| discovered | offered | 全部组 | offered groups | 当前页 | shadow 目录页 | 降幅 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 537 | 256 | 174 | 134 | 32 | 17 | 47.66% |
| 537 | 256 | 174 | 134 | 32 | 17 | 47.66% |
| 538 | 256 | 174 | 134 | 32 | 17 | 47.66% |

人工根因 `record:decisionnode_dec_327_a0b88b89` 在三个 seed 中均：

- discovered；
- offered；
- 进入 L0 exact action group；
- 保持为组代表；
- 模拟展开后可恢复原始候选。

### 3.3 Seaborn FeatureBench

三个显式 observed-defect seed：

| discovered | offered | 全部组 | offered groups | 当前页 | shadow 目录页 | 降幅 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 545 | 256 | 176 | 141 | 32 | 18 | 44.92% |
| 545 | 256 | 176 | 142 | 32 | 18 | 44.53% |
| 545 | 256 | 176 | 142 | 32 | 18 | 44.53% |

四个人工根因在三个 seed 中全部 discovered、offered，并全部为所在组的代表。
其中三个进入 L0 exact action group，一个进入 confirmed causal episode。

## 4. 存储影响

Pydantic 单个完整 manifest 的紧凑 JSON 约 1.07 MB。旧 Pydantic checkpoint
包含 17 个 state snapshot，平均每个约 4.49 MB；若在同规模单 seed 运行中
内嵌 manifest，预计 action journal 增加约 18.2 MB，相对现有 107 MB 约
17%。

本阶段优先保证事实完整性，该放大率尚可接受。启用真实 ClusterTriage 前应
设计内容寻址 sidecar：journal 只保存 manifest identity、selection binding
和 sidecar locator，完整 manifest 只持久化一次。

## 5. 验证产物

- Sphinx：
  `.benchmark-runs/shadow-candidate-clustering-20260731/sphinx.json`
- Pydantic：
  `.benchmark-runs/shadow-candidate-clustering-20260731/pydantic.json`
- Seaborn：
  `.benchmark-runs/shadow-candidate-clustering-20260731/seaborn.json`

Pydantic 使用相同输入再次生成 `/tmp/pydantic-shadow-replay.json`，与正式
结果执行 `cmp` 无差异。

## 6. 下一阶段

可以进入 shadow `ClusterTriage`，但仍不直接改变真实调度：

1. 用完整 manifest 生成组级摘要；
2. 让独立 triage 只选择需要展开的 cluster；
3. 被选 cluster 必须还原为原始候选，再交给现有严格 Global Judge；
4. 对照当前 256 候选路径，验证根因 recall、候选节省率和错误组排除率；
5. 任一人工根因未进入展开集时自动回退现有完整分页；
6. 同时实现内容寻址 sidecar，避免多次 snapshot 重复内嵌大 manifest。

当前结果证明“完整候选可以被安全组织成可回放目录”，尚未证明“组级 triage
可以安全替代现有候选分页”。后者是下一轮的核心验收问题。
