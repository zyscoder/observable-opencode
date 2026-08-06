# 质量优先归因收敛迭代结果

## 结论

本轮修复了三个相互独立的问题：

1. Judge 的固定三段式修复无法处理连续但不同的语义错误。
2. `necessary` 被错误等同于“候选根因”，导致必要的下游落地点被重复升级为根因。
3. 分页层只预留 4 次物理请求，无法使用 Judge 已支持的 6 次语义修复预算。

归因仍是被动、离线、不可反馈给 Agent 的模块。Trace、Agent 消息、工具调用和最终回答路径未被修改。

## 实现结果

### 动态语义修复

- 最大语义修复次数：6。
- 连续 3 次等价校验错误后停止，避免无进展重试。
- 每次修复携带有序校验历史、最近无效输出、原始事实闭包和阶段契约。
- 分页物理请求上限由 4 提升到 6，分页策略版本升级为 v2。

### 必要性与因果角色解耦

- 新增合法事实组合：`necessary + downstream_materialization`。
- 该组合只有在同一 seed、同一 defect 的已确认上游根因路径包含当前候选时才允许发布。
- 只有 `necessary + unknown` 才能进入独立根因确认。
- 必要落地点直接发布为 materialization，不再产生重复根因确认。
- FactorRole 发布契约升级为 v2。

### 角色边界

新增统一的 `role_boundary_contract`，同时用于首次判断和语义修复：

- 仅观察既有缺陷、未改变交付状态或缺陷暴露的 verification 判为 `unrelated`。
- 关闭仍然开放的修复窗口、增加缺陷持续性或暴露的 timeout/interruption 判为 `amplifying_factor`。
- 只有实际执行、存储、发出或交付既有缺陷状态的节点才判为 `downstream_materialization`。
- 仅处于根因下游，不足以证明 materialization。

## Sphinx 冷启动结果

修复角色边界前的三个独立冷启动：

| 指标 | R1 | R2 | R3 | 中位数 |
|---|---:|---:|---:|---:|
| 有效归因 | 1 | 1 | 1 | 1 |
| 根因 Recall | 1.0 | 1.0 | 1.0 | 1.0 |
| 根因 Precision | 1.0 | 1.0 | 1.0 | 1.0 |
| Top-1 | 1 | 1 | 1 | 1 |
| FactorRole F1 | 0.5 | 0.5 | 0.5 | 0.5 |
| Unknown Rate | 0 | 0 | 0 | 0 |
| Judge 请求数 | 9 | 11 | 9 | 9 |
| 安全检查 | 通过 | 通过 | 通过 | 通过 |

两项稳定误差均来自角色边界：

- `record:timeout`：人工标注为 amplifier，模块误判为 materialization。
- `record:verification`：人工标注为 unrelated，模块误判为 materialization。

新增角色边界后的三个独立冷启动：

| 指标 | R4 | R5 | R6 | 中位数 |
|---|---:|---:|---:|---:|
| 有效归因 | 1 | 1 | 1 | 1 |
| 根因 Recall | 1.0 | 1.0 | 1.0 | 1.0 |
| 根因 Precision | 1.0 | 1.0 | 1.0 | 1.0 |
| Top-1 | 1 | 1 | 1 | 1 |
| FactorRole F1 | 1.0 | 1.0 | 1.0 | 1.0 |
| Unknown Rate | 0 | 0 | 0 | 0 |
| Judge 请求数 | 9 | 10 | 8 | 9 |
| 安全检查 | 通过 | 通过 | 通过 | 通过 |

最终角色事实与人工标注一致：

- 根因：`record:decision`
- 条件：`record:prompt`
- 必要下游落地点：`record:change`
- 放大因素：`record:timeout`
- 无关观察：`record:verification`

安全计数在全部运行中保持：

- fabricated refs：0
- unresolved confirmed facts：0
- duplicate identities：0

## Pydantic 压力基线

公开 Pydantic FeatureBench Trace 的代表 seed 产生：

- 发现候选：537
- 质量策略放行：256
- 丢弃：281
- 初始计划：32 页，每页 8 个候选
- 人工根因 `record:decisionnode_dec_327_a0b88b89` 的 offered rank：4

压力运行在主动中断前完成：

- 成功页：20
- 失败页：6
- 正在运行页：1
- 物理 Judge 请求：54
- 成功页平均物理请求：1.45
- 失败页中有 5 页用满旧的 4 次预算

`SIGINT` 后生成了合法报告：

- outcome：`inconclusive`
- seed outcome：`evidence_gap`
- blocker：`analysis_interrupted`
- checkpoint 可恢复：true
- 伪根因：0

这说明候选发现能覆盖人工根因，但 256 个候选逐节点分页的吞吐和后段页语义稳定性不足。`max_frontier_items` 只控制递归 frontier，不控制 Global 候选池，不应被误用为候选硬上限。

## 回归验证

- FactorRole 专项测试：105/105 通过。
- 完整离线归因测试：1339/1339 通过。
- 分页恢复测试确认：已开始页不会被重试，恢复只继续后续页。
- 归因中断测试确认：SIGINT/SIGTERM 形成结构化 evidence gap，不发布伪根因。
- 离线隔离测试确认：归因结果不会反馈给 Agent 或改变 Harness 行为。

## 未解决问题

Pydantic 和 Seaborn 大 Trace 仍受 256 候选、固定 8 候选页宽影响。直接截断到 64 个候选虽然更快，但缺少跨案例 recall 证明，当前不采用。

下一轮采用两阶段候选聚类：

1. 先生成不可变的 shadow `CandidateClusterManifest`，不改变当前 Judge 调度。
2. 达到人工根因组覆盖 100% 后，再启用 `ClusterTriage -> group expansion -> existing Global Judge`。

Shadow 阶段的验收门槛：

- 每个 discovered candidate 恰好属于一个叶组。
- 人工根因所属组覆盖率：100%。
- 模拟展开后的人工根因 recall：100%。
- manifest replay identity：100% 稳定。
- 未 grounded 引用：0。
- 模拟初始页数从 32 页降至 16–20 页。

Cluster 只决定是否展开，不直接确认根因。展开后仍由现有节点级 Global Judge、RootConfirmation 和 FactorRole 契约完成最终判断。
