# Retrieval + Global Judge Fusion Design

## Goal

将当前递归归因模块中已经有效的语义数据流重建和候选检索能力，与一次全局
LLM 候选比较结合。归因模块先将完整 Trace 压缩为高召回候选证据闭包，Global
Judge 再判断候选之间的相对因果角色；证据不足时才调用现有递归分析继续扩展，
所有根因仍需经过独立确认。

## Constraints

- 归因过程保持离线、被动、只读，不改变 Agent、Harness、Trace 或 Case 结果。
- 检索得分只表示导航相关性，不能直接成为根因结论。
- 不可将候选裁剪设为不可恢复边界；Global Judge 可以请求回查 Trace。
- `no_defect` 必须由明确反证或成功验证支持，不能由候选为空推导。
- 历史 Trace 缺少关键事实时返回 `needs_expansion` 或 `inconclusive`。
- 已有递归 Judge 和独立根因确认继续可用，保证向后兼容。

## Architecture

### 1. Candidate Retrieval

从每个起始缺陷的初始 frontier 节点检索高召回候选。导航节点使用 progress
window，普通节点使用已确认数据流、episode、sibling 和语义回退。候选按 ref
去重，但不在此阶段判断缺陷。

### 2. Evidence Closure

为每个候选生成 `CandidateEvidenceCapsule`：

- 候选节点的 compact 语义；
- 检索来源、分数和数据流边；
- 起始缺陷与候选到起点的下游路径；
- 候选引用的证据、artifact hydration 状态；
- 与候选共享 `action_group_id` 的 reasoning/tool/change/verification 节点；
- 紧邻的已确认入边和出边；
- 缺失、未解析或截断证据。

证据闭包是面向 Judge 的信息压缩单位，不修改原始 Trace。

### 3. Global Candidate Judge

一次请求同时比较所有候选，并返回：

- `candidate_roots`：一个或多个需要独立确认的根因候选；
- `no_defect`：起始缺陷被反证，且存在决定性证据；
- `needs_expansion`：指定候选或证据边界需要递归扩展；
- `inconclusive`：现有事实无法形成判断。

每个候选输出 `defect_status`、`causal_role`、理由、证据引用和置信度。模型必须
显式比较竞争假设，也允许保留 `no_defect`，不得被强制选择根因。

### 4. Controlled Recursive Expansion

只有 `needs_expansion`、低置信度或证据闭包存在关键缺口时，才把指定 anchor
送入现有递归后向污点分析。递归引擎作为证据扩展器和因果路径证明器，不再对
所有候选逐个盲目遍历。

### 5. Independent Confirmation

Global Judge 选择的根因候选进入现有独立确认流程。确认请求不携带检索排名，
避免排序锚定。只有确认通过的候选才能进入 `confirmed_roots`。

## Failure Handling

- Global Judge 不可用、协议校验失败或预算不足时，回退现有递归流程。
- 候选为空但存在未解析事实时，返回 `needs_expansion`，不得输出 `no_defect`。
- 候选引用无法解析时，将缺口写入证据闭包和最终报告。
- Global Judge 引用未提供 ref、输出自相矛盾或把 outcome evidence 当成 root 时，
  拒绝结果并进入修复或回退。

## Reporting

最终 attribution JSON 在 metadata 中追加：

- `fusion_mode`；
- `global_candidate_judgment`；
- `candidate_evidence_capsule_summary`；
- `candidate_compression`；
- `recursive_expansion_reason`；
- Global Judge 的逻辑/物理调用计数。

原有字段继续保留，便于和历史评测结果对比。

## Acceptance Criteria

1. 单元测试验证证据闭包包含候选、路径、动作组、反证和缺失证据。
2. Global Judge schema 不允许引用候选集合外的根因。
3. `no_defect` 必须包含决定性证据且不能同时选择根因。
4. `needs_expansion` 必须给出合法 anchor 和具体缺口。
5. Global Judge 不可用时，现有递归测试行为不变。
6. Axios、Astropy、TerminalBench 至少完成一轮融合模式回归，并报告候选覆盖、
   压缩率、Global Judge 结果和独立确认结果。
