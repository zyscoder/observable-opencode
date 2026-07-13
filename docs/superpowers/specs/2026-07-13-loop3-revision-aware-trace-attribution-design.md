# Loop 3 Revision-aware Trace + Attribution Hydration 设计规格

## 1. 背景

本轮先使用 `opencode serve -> session -> HTTP request` 执行了 11 个 stress case。所有 case 的最终代码和测试行为均正确，预设故障没有真实触发，但现有 stress review 仍把预设根因显示为 Root Cause，并把字段存在性计为 `sufficient/effective/100`。对 4 个代表性 trace 的离线归因结果为：1 个正确 `no_defect`、2 个 `inconclusive`、1 个假阳性根因。

假阳性的直接原因是最终回答同时关联修改前失败验证和修改后成功验证，却没有仓库版本与验证时态；两个 `inconclusive` 的直接原因是 trace 虽然保存了完整 tool output、diff 和 artifact，归因模块只拿到截断 preview 或低价值上游节点。

## 2. 目标

1. 让 trace 表达“哪个事实属于哪个仓库状态”，避免历史失败污染最终状态判断。
2. 让最终回答中的每个重要原子 claim 只关联真正支持它的当前事实，同时保留候选上下文供审计。
3. 让离线归因模块能够按需读取 trace 已保存的 artifact，而不是因为 preview 截断而停止。
4. 从主语义流移除重复或无实质语义的节点，原始事件仍保存在 `raw-events.jsonl`。
5. 将 stress review 的机制覆盖、实际执行结果和预设故障目标彻底分离。
6. 使用来自 benchmark 能力域的复杂可执行 case 验证调用链理解、架构边界、私域知识、需求优先级、影响分析和方案质量。

## 3. 全局约束

- 所有 trace 增强必须是被动记录或 case 结束后的派生，不得改变 prompt、工具输入、上下文选择、模型输出、任务循环或 agent 决策。
- 离线归因结果不得反馈给 agent。
- 大文本继续存入 artifact；`trace.json` 保存可检索摘要、内容哈希和 artifact 引用。
- `raw-events.jsonl` 保留原始事件；`trace.json` 是收敛后的语义事实层，可移除重复节点。
- case 被主动结束服务时，case 结果与 server shutdown 分开表达，不再用一个 `cancelled` 混淆两者。

## 4. Trace 事实层设计

### 4.1 仓库 Revision

每次生产代码、测试、文档或配置变更后生成单调递增的 `repository_revision`。`change` 记录输出 `revision_before`、`revision_after` 和 `changed_roles`。所有后续 verification、response claim 和 tool observation 记录其观察到的 revision。

这不是对 agent 行为的反馈。revision 由 trace recorder 根据已发生的 change 事后维护。

### 4.2 Verification 生命周期

verification 增加：

- `repository_revision`
- `verification_phase`: `baseline`、`post_change`、`post_test_change`、`unknown`
- `effective_for_final_state`
- `supersedes_refs` 与 `superseded_by_refs`
- `process_exit_code`
- `parsed_command_outcomes`
- `exit_masked_by_shell`
- `scope_source`: package script、显式文件或未知

组合命令既保存 shell 最终退出码，也解析输出中明确打印的子命令退出码。修改前失败验证仍然保留，但不会作为“最终全部通过”claim 的冲突证据。

### 4.3 原子 Response Claim

claim 提取必须覆盖 Markdown 标题前的正文首句，例如“全部通过”。标题、表头、分隔线和纯格式行不生成 claim。

claim 增加：

- `claim_kind`: verification、change、requirement、architecture、risk、fact
- `temporal_scope`: baseline、current_revision、historical、future
- `repository_revision`
- `direct_support_refs`
- `candidate_context_refs`
- `superseded_evidence_refs`

最终 `response.output` 保留生成上下文和所有候选执行记录，但直接事实支持只通过原子 claim 表达。

### 4.4 工具与 Evidence 收敛

同一 `call_id` 在语义 trace 中只保留一个 canonical tool span，包含输入、输出、状态、耗时、revision 和 artifact 引用。底层开始/结束事件只进入 raw events。

目录列表、文件路径和通用 fallback summary 保留为 `execution.observation`，但不再自动复制成 `evidence.semantic_fact`。只有能形成 subject/predicate/value、代码定义、需求约束、接口契约、测试结果或架构边界的内容才生成 semantic fact。

未触发压缩的重复 `context.compaction_check` 聚合到 metrics；真正触发压缩、越界或算法选择变化时才进入主语义流。

### 4.5 Case 生命周期

`case_status` 表达任务结果；`shutdown_disposition` 表达服务如何退出。正常完成 case 后由 runner 发送 SIGINT，应记录：

- `case_status: success`
- `shutdown_disposition: graceful_after_case_completion`
- `shutdown_signal: SIGINT`

只有任务本身未完成才使用 `cancelled`。

## 5. 离线归因模块设计

### 5.1 Artifact Hydration

`TraceGraph` 加载 trace 时建立 artifact 索引。judge 请求构造阶段根据当前 claim 和上游引用按需展开 artifact，优先提取：

1. claim 直接支持证据；
2. 当前 revision 的 verification/change；
3. tool output 中与 claim 共享实体、数值、路径和谓词的片段；
4. 生成该 claim 的 LLM/context 转换。

展开有字符预算，报告必须说明哪些 artifact 已展开、哪些因预算省略。

### 5.2 起点与排序

默认从最终回答的高价值原子 claim 开始，而不是把整段 response.output 当成单个判断单元。排序为：带 quality flag 的 claim、verification/change claim、requirement/architecture claim、risk claim。没有原子 claim 时才回退到 final response.output。

上游顺序为：direct support、当前 revision verification、change、semantic fact、tool result、LLM generation、context、candidate evidence、historical/superseded evidence。

### 5.3 时态判断

judge 输入显式包含当前 revision 和证据时态规则。历史失败不与当前成功直接冲突；只有同一 revision、同一范围的结果才构成冲突。

### 5.4 Inconclusive 反馈

任何 `unknown` 都必须产生至少一个结构化 blocking gap。若 trace 中已有 artifact 但未展开，问题归于 attribution hydration；若 trace 根本没有语义数据，问题归于 trace emitter。不得再出现 `analysis_outcome=inconclusive` 但 improvement report 为零项。

## 6. Stress Review 设计

review 输出三个正交部分：

- `mechanism_coverage`: trace 是否记录了所需机制和字段。
- `actual_case_outcome`: 根据实际 diff、测试、禁止路径、MCP/skill/subagent 要求和回答断言判断 pass/fail/unknown。
- `designed_failure_target`: fixture 想诱发的故障，仅供外部评测，不参与实际缺陷判断。

只有 `actual_case_outcome=fail` 或存在经过验证的 quality gap 时，才允许进入根因分析。质量分不再仅凭关键词和字段存在性给满分。

## 7. 开源复杂 Benchmark Cases

当前目录中的 questions 是能力题模板，不是完整、可执行的 benchmark。本轮不得把它们改造成自定义 fixture 后冒充真实 benchmark。

主数据源选用 MIT 许可的 **LiberCoders/FeatureBench**。它面向 feature-level 复杂开发，任务来自真实开源仓库，使用执行式 F2P/P2P 测试，并提供无需 GPU 的 fast split。优先从 fast/lite split 选择 3 至 4 个实例，选择标准为：

1. reference patch 涉及多个生产文件或跨模块调用链；
2. problem statement 不直接泄露目标文件和实现符号；
3. 同时存在 F2P 新功能测试和 P2P 回归测试；
4. 在 macOS arm64 或可用 Docker 环境中能够拉取/构建；
5. 覆盖至少两个不同上游仓库，避免对单一代码结构过拟合。

若 FeatureBench 指定实例在 arm64 环境无法构建，则从 MIT 许可的 **SWE-bench Verified** 选择 1 至 2 个真实 issue 作为兼容性备选。备选必须记录切换原因，不能降低为本地玩具 case。

仓库只保存 benchmark 来源、版本/commit、split、instance id、base commit、problem statement 哈希和运行结果，不复制 gold patch 到 agent 上下文。执行仍使用 HTTP session，不使用 command-shell prompt。最终正确性优先使用官方 harness；若环境只能完成 inference，报告必须把 `evaluation_status` 标为 `not_run`，不得自行宣称通过。

## 8. 验收标准

1. 修改前失败、修改后通过的 trace 不再让离线模块把最终“全部通过”判为错误。
2. 完整 read/diff 已在 artifact 中时，归因模块能展开并作出 `no_defect` 或缺陷判断，不因 preview 截断直接 `unknown`。
3. 关键首句被提取为 claim，Markdown 标题和表头不再成为 claim。
4. 同一 call_id 在语义 trace 中只有一个 canonical tool record。
5. path-only observation 不生成 semantic fact。
6. `inconclusive` 必有 blocking gap 和推荐改进。
7. stress summary 不再把 designed failure target 显示成实际 Root Cause。
8. 至少 3 个 FeatureBench 真实开源实例通过 HTTP 执行并生成完整 trace.html/trace.json；能运行官方 harness 时同时输出 F2P/P2P 结果。
