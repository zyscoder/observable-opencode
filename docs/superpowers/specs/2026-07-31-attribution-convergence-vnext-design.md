# Attribution Convergence vNext 设计规格

## 1. 背景

2026-07-31 对 Pydantic deprecated-fields 和 Seaborn regression 两个
FeatureBench 开源任务进行了真实的 observable-opencode + DeepSeek 执行、
官方测试验证、人工后向语义污点分析和离线归因模块分析。

评测结果表明：

- Pydantic 官方测试为 0 passed / 12 failed。人工分层验证识别出
  descriptor 生命周期、model rebuild 和 JSON Schema 三类独立缺陷。
- Seaborn 官方测试为 30 passed / 26 failed / 5 skipped。人工分析识别出
  pandas 数据契约、集成范围、验证选择和错误关闭等多类问题。
- 离线归因分别将 4757 和 1873 条记录压缩到 293 和 263 个唯一候选，
  候选范围缩减 93.8% 和 86.0%。
- 候选集包含部分正确实现节点，但遗漏了 `dec_293`、`dec_147` 这类
  “决定不做某件事”的规划遗漏节点。
- 首轮 Global Judge 能识别 Pydantic 的 `dec_861`，但 Seaborn 将错误关闭
  `dec_343` 当成功能根因。
- DeepSeek 返回 402 后，模块继续产生大量无效请求，并在最终报告中丢失
  已成功完成的部分 Global Judge 判断。

因此，当前主矛盾不是缺少更多原始 Trace 字段，而是：

1. Provider 失败生命周期不可靠；
2. 多个独立失败被合并为一个宽泛 defect seed；
3. 规划遗漏节点没有稳定进入候选集；
4. 功能缺陷引入点、验证遗漏和错误关闭缺少正交角色；
5. 大 Trace 的内存和收尾成本仍不可接受。

## 2. 目标

本轮建设 Attribution Convergence vNext，目标是：

- 在 Provider 异常下保留全部已验证事实，并可确定性恢复；
- 将外部测试失败拆分成可证伪的独立缺陷种子；
- 召回显式实现错误和规划遗漏两类根因候选；
- 使用 LLM 驱动的递归后向分析确认多根因，同时区分非根因角色；
- 保持 Trace 和归因对 Agent 完全不可见、不可反馈；
- 将 Pydantic 规模 Trace 的收尾内存和耗时控制在可部署范围。

## 3. 全局约束

- Trace 捕获保持被动记录，不修改 Agent prompt、上下文、工具、skill/MCP、
  任务循环或最终答案。
- 离线归因不得读取人工根因标签作为候选、Judge 或修复输入。
- 人工标签只允许用于运行结束后的独立指标计算。
- 外部官方测试事实可作为 observed defect，但必须标记来源和发生时间。
- 所有新增离线推断边必须与 Trace confirmed edge 分层，不得伪装成已记录事实。
- Provider timeout 保持 3600 秒；质量优先于请求成本。
- 不通过放宽 grounding、root confirmation 或报告校验来提升成功率。
- 在当前 `codex/message-context-lineage` 共享脏分支上工作，不回退既有改动。

## 4. 总体架构

```text
External evaluation facts
        |
        v
Failure Signature Clustering
        |
        +---- defect seed A
        +---- defect seed B
        +---- defect seed C
                    |
                    v
Candidate Reconstruction
  confirmed dataflow + omission obligations + progress episodes
                    |
                    v
Candidate Cluster / Page Planning
                    |
                    v
Global Comparative Judge
                    |
                    v
Recursive Backward Taint + Independent Confirmation
                    |
                    v
Canonical roots + factor roles + unresolved facts
```

Provider 请求生命周期与因果分析生命周期分离。Provider 失败不能删除已经
持久化的 Judge 事实，也不能将 incomplete page 解释为“没有根因”。

## 5. Provider 失败语义

### 5.1 错误分类

不可重试错误：

- HTTP 400 中确定性的 schema/model 参数错误；
- HTTP 401、402、403、404；
- 不支持的模型或 endpoint；
- 明确的账户、权限或余额错误。

可重试错误：

- connection reset、DNS、connect timeout；
- HTTP 408、409、425、429；
- HTTP 500、502、503、504；
- Provider 临时 unavailable。

### 5.2 熔断规则

- 不可重试错误第一次出现即打开 circuit；
- 可重试错误达到 `provider_error_threshold` 后打开 circuit；
- circuit 状态包含分类、HTTP status、稳定 error code、首次发生请求和时间；
- circuit 打开后不得继续分配物理请求；
- 余额或权限恢复后，新的 CLI 运行可从 checkpoint 恢复。

### 5.3 部分结果

每个成功 page 立即以事务方式写入 checkpoint。后续 page 失败时：

- 已成功 judgment 继续进入最终报告；
- 未完成 page 标记为 unresolved；
- `analysis_outcome` 可以是 `partial` 或 `inconclusive`，但不能丢弃事实；
- 最终报告必须区分“没有候选”和“候选判断未完成”。

## 6. 多缺陷种子

外部测试结果先规范化为 `EvaluationFailureFact`：

- test id；
- exception/assertion 类型；
- normalized message；
- 首个业务栈帧；
- symbol、file、line；
- expected/actual；
- evaluation run identity。

使用确定性 `FailureSignature` 聚类：

```text
exception family
+ first business symbol
+ normalized assertion contract
+ affected subsystem
```

每个 cluster 产生一个 `ObservedDefectSeed`。聚类不得使用人工根因节点。
同一测试可在 layered evaluation 中进入后续 seed，但必须记录前置阻断已被
人工中和，防止把第二层失败误当作原始直接观测。

## 7. 规划遗漏候选

新增离线 `ObligationGapCandidate`，来源包括：

- 用户题面和接口说明中的任务义务；
- Agent 明确列出的计划项；
- Agent 已发现但明确排除的依赖；
- edit/tool/test 对义务的实际覆盖；
- 失败后被判断为“既有问题”或“不在范围”的节点。

候选只能由 Trace 中已记录文本、结构化工具事实和外部评测事实派生。
每个候选必须保存：

- obligation id；
- decision ref；
- recognized evidence refs；
- planned coverage；
- actual action coverage；
- exclusion rationale；
- downstream failure signature refs；
- `evidence_type=offline_reconstruction`。

“缺少某个函数”本身不是根因。只有存在记录在案的规划、排除或错误假设节点时，
才生成对应 omission candidate。

## 8. 根因与因素角色

根因资格与结果必要性、因果链角色正交：

- `defect_introduction_root`
- `verification_omission`
- `false_closure`
- `downstream_materialization`
- `amplifying_condition`
- `external_interruption`

功能缺陷 seed 的错误关闭节点不能替代更早的实现或规划引入点。
错误关闭可作为独立 acceptance/verification seed 的根因。

Global Judge 和递归 Judge 均获得：

- 当前 failure signature；
- 当前候选及完整语义 payload；
- 上游任务义务和计划；
- 对应 edit/diff；
- 下游 verification/outcome；
- 同组竞争候选；
- confirmed path 与 offline reconstruction path 的分层信息。

多根因确认要求每个 root 对至少一个独立 failure signature 有阻止缺陷的
反事实作用。共享 contributing condition 不重复发布为多个 root。

## 9. Trace 运行时

保持 Causal IR schema 为现有 Trace 的超集。优化仅作用于存储和投影：

- 大文本 artifact 边写边落盘；
- partial snapshot 只保存增量索引和小型预览；
- HTML 生成按记录流读取，不重新常驻全部 payload；
- SIGINT/SIGTERM 基于已有记录生成结构完整的 Trace/HTML；
- SIGKILL 无法执行用户态逻辑，只保证此前 partial snapshot 可读；
- finalizer 超时后写出明确 incomplete 状态，不阻塞进程无限退出。

## 10. 验收指标

- pre-Judge 人工根因候选召回率：100%；
- 唯一候选相对原始记录缩减：不低于 85%；
- 根因 Precision、Recall：均不低于 0.85；
- 多根因覆盖率：不低于 0.90；
- 因果角色 F1：不低于 0.75；
- Provider 健康时 `inconclusive` 比例：不高于 10%；
- 不可重试 Provider 错误的物理请求数：不高于 1；
- 已成功部分判断保留率：100%；
- Agent-visible message/tool/provider/final-answer hash：完全不变；
- Pydantic 规模 Trace 峰值 RSS：不高于 2 GB；
- SIGINT/SIGTERM HTML 收尾：不高于 60 秒。

## 11. 验证案例

第一组复用已有 Trace，隔离归因算法变化：

- Pydantic deprecated fields；
- Seaborn regression；
- Sphinx C domain parser。

第二组重新执行 Agent，验证 Trace 运行时：

- 跨模块数据契约；
- 架构依赖遗漏；
- 错误测试选择；
- 多个独立隐藏失败；
- 长上下文和信号退出。

每个案例分别保存 Agent 结果、官方测试、人工分析、模块分析和差异报告。
