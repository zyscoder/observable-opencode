# Commitment Evidence Stability 与独立根因确认结果

日期：2026-08-04

## 0. 归因效果摘要

本报告中的 `1720` 条 Python 测试是工程正确性回归，不是 1720 次真实归因
评测。当前归因效果必须分层理解：

| 能力层级 | 测试方式 | 当前结果 | 能否作为归因准确率 |
| --- | --- | --- | --- |
| 候选检索 | 完整 64 候选 Sphinx 重放 | 4/4 人工关注节点被保留，Recall@64=100% | 只能证明候选未丢失 |
| 节点缺陷判断 | 聚焦 `dec_236`，真实 Flash 调用 | 与人工判断一致：`responsible_omission` | 单节点有效，不能外推 |
| 独立根因确认 | 聚焦 `dec_236`，真实 Flash 调用 | 与人工判断一致：`necessary_cause`，置信度 0.85 | 单根因有效，不能外推 |
| 完整端到端归因 | 最终 v14 对完整候选重新运行 | **尚未完成** | Top-1、Precision、Recall 暂无有效结果 |
| 跨 case 泛化 | 多 benchmark、多缺陷类型 | **尚未完成** | 暂无统计显著性 |

因此，本轮能证明的是：最终协议能够在一个已知 Sphinx 过程缺陷上完成有证据
约束的节点判断和独立确认。它尚不能证明当前归因模块已经具备稳定的整体准确率。

### 0.1 物理请求的定义

一次“物理请求”是归因模块实际向 LLM Provider 发出的一次 HTTP API 调用。
它与“逻辑判断”不同：

- 一个候选节点的一次逻辑判断，如果首次响应合法，通常消耗 1 次物理请求；
- 如果响应 JSON/schema 不合法、证据引用错误或反事实字段冲突，修复提示再次
  调用模型，每次都新增 1 次物理请求；
- Provider 重试也会增加物理请求；
- 缓存命中的逻辑判断可以消耗 0 次物理请求。

本轮聚焦 `dec_236` 的首轮判断消耗 3 次物理请求，独立确认也消耗 3 次，
说明两者都经历了初次调用和后续语义/schema 修复。`112` 是较早 64 候选
完整重放的 Provider 调用量，不是 112 个测试，也不是最终 v14 的完整成本。

### 0.2 1720 条测试验证什么

Python 测试来自 69 个文件，源代码中有 1706 个显式测试方法，其余执行数
来自动态/参数化展开。主要覆盖：

| 测试领域 | 代表性测试文件/数量 | 验证内容 |
| --- | --- | --- |
| 后向污点与传播 | `test_backward_taint.py` 129 | 边方向、传播终止、缺陷引入点和错误路径拒绝 |
| 因果/角色 Judge 协议 | factor role 125、causal judge 124、global judge 84 | prompt、schema、证据引用、反事实和非法输出拒绝 |
| 递归状态机 | recursive analyzer 119、investigation 51、hypotheses 25 | 遍历、回溯、预算、候选队列和未决状态 |
| Checkpoint/恢复 | causal checkpoint 86 | 中断恢复、缓存身份、请求计数和不可重复消费 |
| 检索和候选压缩 | retrieval、budget、clustering、paging、triage | Recall 安全门、分页、cluster 展开和预算边界 |
| Causal IR/证据完整性 | causal state、evidence capsule、evaluation facts | 序列化、不可篡改引用、事实来源和角色发布 |
| Benchmark 适配 | bundle 43、labels 48、recursive benchmarks 40 | Bundle 绑定、标签隔离、评测输入和结果解析 |
| 行为隔离与安全 | offline isolation、artifact、CLI 等 | Trace 不反馈 Agent、敏感信息处理和失败闭合 |

这些测试的价值是防止实现回归和伪造因果链，但绝大多数使用确定性 fixture 或
mock Judge，不调用 DeepSeek，也不衡量模型对真实复杂 case 的归因准确率。

## 1. 本轮目标

本轮使用同一份不可变 Sphinx FeatureBench Bundle 和
`deepseek-v4-flash`，验证以下问题：

1. 在候选规模扩大后，关键决策节点是否仍能被检索和保留；
2. 模型能否区分普通推理、实施承诺和承诺后的实际执行轨迹；
3. 首轮节点缺陷判断与独立根因确认是否能够分别完成；
4. 归因所需的过程事实是否来自 Trace/Causal IR，而不是首轮 Judge 的结论；
5. 新协议是否保持被动记录、离线分析、不可反馈给 Agent 的行为隔离原则。

## 2. 冻结输入

| 项目 | 值 |
| --- | --- |
| Benchmark | Sphinx FeatureBench |
| Bundle SHA-256 | `0a129a677b9441d3ea00375693f63300d46d0dde88e6079297dc2759fa0d9142` |
| 缺陷种子 | `record:observed_defect_seed_57e1f53d962cbd84854fb1fab46caf485eb109c64a962c91823c4346d034645b` |
| 关键候选 | `record:decisionnode_dec_236_4be2d117` |
| 后续轨迹一 | `progress_episode:progress_f124dc3e9b5a32f3` |
| 后续轨迹二 | `progress_episode:progress_ffb47244983243b2` |
| Provider | DeepSeek Anthropic-compatible endpoint |
| Model | `deepseek-v4-flash` |
| 单请求超时 | 3600 秒 |

本轮没有把人工标签作为归因输入传给模型。人工事实只用于事后比较。

## 3. 问题事实

目标任务要求实现缺失的 Sphinx parser 方法。Agent 在 `dec_236` 中明确
输出了类似“接下来编写缺失方法”的实施承诺，但后续没有发生相应的代码
修改或验证。最终 35 个 fail-to-pass 测试全部失败，81 个 pass-to-pass
测试通过。

因此，待验证的过程缺陷并不是“模型是否知道仓库缺少实现”，而是：

- Agent 已形成并表达了正确的实施意图；
- 该意图没有转换成工具调用、文件修改和验证动作；
- 最终功能缺陷持续存在。

## 4. 改造内容

### 4.1 候选保留

- 将质量优先的导航候选上限扩展到 64；
- 对包含实施承诺线索的候选提升调度优先级；
- 线索只参与检索排序，不直接生成缺陷或根因结论；
- 保留原有全局节点、请求、前沿和假设预算约束。

### 4.2 过程事实层

新增并约束以下事实：

- `candidate_role`：候选在过程中的角色；
- `commitment_cue`：候选是否包含可审计的实施承诺；
- `commitment_status`：承诺是否履行；
- `trajectory_status`：候选后的行为是否向目标推进或发生偏离；
- `task_obligation`：用户任务要求的动作或结果；
- `process_lifecycle_observed`：候选与后续轨迹之间的离线重建关系。

过程事实由不可变 Trace 和 Causal IR 重新计算。首轮 Judge 的缺陷判断不进入
独立确认请求，防止“先判断有罪，再用自身判断证明有罪”的循环证据。

### 4.3 首轮节点缺陷判断

首轮判断必须同时引用：

1. 当前候选节点；
2. 至少一个候选后的轨迹节点；
3. 与任务目标绑定的 obligation 事实。

对于 `responsible_omission`，当前候选证据和后续未履行证据必须是不同引用。
模型不能只凭一句承诺文本直接认定根因。

### 4.4 独立根因确认

独立确认新增结构化 `process_confirmation_assessment`，要求模型重新判断：

- 候选角色和承诺类型；
- 承诺是否履行、轨迹是否偏离；
- 若在该节点落实已承诺动作，当前缺陷是否会被阻止；
- 候选是必要原因、贡献条件、放大因素还是无关因素。

确认协议明确区分：

- 仓库原始缺口是任务前置条件，不自动为 Agent 的未执行行为免责；
- 决策文本是 Agent 可观察的输出，不等于被动环境文本；
- 过程反事实是“落实已承诺的后续动作”，不是“删除这句话”。

## 5. DeepSeek V4 Flash 实测

### 5.1 全候选重放

| 版本/配置 | 结果 |
| --- | --- |
| 24 候选早期版本 | 关键候选可能落在窗口外，无法完成根因确认 |
| 64 候选第一轮 | 112 次物理请求、68 次逻辑判断；关键候选 4/4 保留 |
| 64 候选过程约束后 | 能识别 `dec_236`，但暴露“无交付却标记 fulfilled”的协议缺口 |

64 候选配置解决了关键节点召回问题，但全量逐候选判断成本仍然偏高。

### 5.2 聚焦首轮判断

对 `dec_236` 的聚焦冷启动重放结果：

- 物理请求：3；
- 判断状态：`present`；
- `candidate_introduction=true`；
- 候选角色：`implementation_commitment`；
- 承诺状态：`unfulfilled`；
- 轨迹状态：`diverged`；
- 缺陷类型：`responsible_omission`；
- obligation：`analysis_objective`；
- 证据同时包含候选节点和两个后续 progress episode。

首轮判断证明当前协议能够将“说要实现但未行动”识别为过程缺陷，而不是把
仓库缺失实现、最终测试失败或一句计划文本中的任意一个单独当作根因。

### 5.3 独立根因确认

独立确认同样使用 3 次物理请求，最终输出：

- 状态：`confirmed`；
- 因素角色：`necessary_cause`；
- 置信度：`0.85`；
- 候选角色：`implementation_commitment`；
- 承诺线索：`commitment`；
- 承诺状态：`unfulfilled`；
- 轨迹状态：`diverged`；
- 反事实干预：`decision_and_committed_followup`；
- 任务前置条件：`repair_obligation`；
- 干预后活动过程缺陷：`absent`；
- 干预后下游失败：`absent`；
- 反事实结论：落实该节点已经形成的实施决策可阻止当前缺陷。

独立确认引用了 `dec_236` 和两个后续 progress episode，没有引用首轮 Judge
的 verdict。人工复核与该结论一致：当前 case 的直接可优化位置是 Agent
decision/action generation 边界，即正确计划未转换成实际 action。

## 6. 当前效果评价

### 已证明的能力

1. 关键候选召回：本 case 的 4 个人工关注节点均进入 64 候选窗口；
2. 过程语义识别：能区分实施承诺、未履行轨迹和普通仓库前置缺口；
3. 证据闭环：候选、后续轨迹和任务 obligation 均有独立引用；
4. 判断隔离：独立确认只消费重建事实，不消费首轮 verdict；
5. 反事实约束：确认结果同时给出干预对象、活动缺陷和下游失败状态；
6. 行为隔离：全部改造位于 Trace/Causal IR 和离线归因模块，没有改变
   Agent 可见 prompt、工具结果或任务循环行为。

### 尚未证明的能力

1. 当前只有一个 Sphinx 过程缺陷完成了双阶段独立确认，不能据此宣称跨
   benchmark 稳定；
2. 尚未覆盖错误代码修改、错误上下文压缩、工具结果误读、subagent 交接、
   MCP/skill 选择错误等不同根因类型；
3. 首轮和独立确认都经历了两次协议修复后才在第三次请求通过，说明结构化
   引用生成仍不够稳定；
4. 当前结论能够定位到 Agent 决策到动作转换边界，但仅凭现有 Trace 还不能
   稳定映射到 observable-opencode 内部某一个具体源码函数；
5. 64 候选全量重放成本为 112 次物理请求，质量优先目标已满足，但调度效率
   仍需明显改善。

## 7. 回归验证

| 验证项 | 结果 |
| --- | --- |
| 归因核心测试 | 317/317 通过 |
| Python 全量归因测试 | 1720/1720 通过，耗时 234.649 秒 |
| FeatureBench runner | 13/13 通过；沙箱外本机端口验证 |
| Trace/HTML Bun 测试 | 183/184 通过 |
| macOS arm64 单目标构建 | 通过 |
| 二进制 smoke test | `opencode --version` 通过 |
| `models.dev` 不可用 | 5 秒后回退 bundled snapshot，构建继续 |
| `git diff --check` | 通过 |

唯一未通过的 Bun 用例是 5001 条、每条约 1 KiB 的 HTML 流式压力测试。
生成结果未出现语义断言错误，但耗时约 956 秒，超过 120 秒上限。该问题属于
大 Trace HTML 渲染性能，不影响本轮归因事实协议的正确性，但会影响真实大
case 的报告生成体验。

## 8. 下一轮计划

### P0：跨缺陷类型验证

在不少于 3 个开源 benchmark case 上分别构造或选择：

1. 正确计划未执行；
2. 执行了错误修改；
3. 正确工具结果被错误解释；
4. 可选：subagent/主 Agent 交接丢失。

每个 case 都保存人工后向语义污点分析，计算候选 Recall@K、Top-1、根因角色
一致率和首次分歧节点。

### P1：候选调度收敛

保留 64 候选安全上限，但使用语义 cluster 和 lifecycle cue 分层调度：

- 第一阶段低成本目录判断；
- 第二阶段只扩展高价值 cluster；
- 第三阶段对少量候选做严格节点判断和独立确认。

目标是在保持人工根因 Recall@64 为 100% 的前提下，将单 case 物理请求从
112 降到 30-50，并保持 Top-1 不下降。

### P2：独立确认稳定性

将 candidate、trajectory、obligation 的合法引用改为更强的结构化选择协议，
减少模型在自由文本中复制长 ID 的错误。目标是首轮与独立确认平均不超过
1.5 次物理请求，不依赖第三次修复。

### P3：大 Trace HTML 性能

对 5001 记录压力用例做阶段计时和复杂度分析，重点检查重复全量排序、重复
HTML 转义、字符串拼接和多视图重复物化。目标是保持 HTML 内容等价和每块
不超过 4096 bytes，同时将总耗时压到 60 秒以内。

## 9. 结论

本轮把归因模块从“能召回一个可疑计划节点”推进到“能够基于候选、任务义务
和后续轨迹，独立确认一个未履行实施承诺是必要过程原因”。这是一项有效的
能力跃迁，但当前证据仍是单 case 成功。下一轮的核心不是继续针对 `dec_236`
调 prompt，而是用不同 benchmark 和不同缺陷机制验证可迁移性，并同步降低
候选判断成本和独立确认的修复次数。

## 10. 2026-08-04 冷启动全链路复查与修复

### 10.1 修复前基线

Sphinx FeatureBench 的最终 v14 Bundle 在全新 cache、64 候选和
`deepseek-v4-flash` 下完成冷启动运行：

| 指标 | 修复前结果 |
| --- | --- |
| 逻辑 Judge 调用 | 128 |
| 真实物理请求 | 232 |
| 根因假设 / 独立确认 | 0 / 0 |
| `dec236` 召回 | 已发现，`discovered_rank=14` |
| `dec236` 评估资格 | 否，`no_active_seed_causal_path` |
| 最终结果 | `inconclusive` |

人工后向污点分析发现，`dec236` 已被 progress-window 检索召回，但全局候选
资格先检查 grounded path；用于连接“候选决策、后续无交付轨迹和失败 seed”
的 `process_lifecycle_observed` 边却要到后续导航阶段才建立。候选因此永远
无法进入该阶段，这是候选发现与节点判断之间的顺序缺陷。

### 10.2 实施修复

全局候选池现在会在资格计算前，仅对确实属于已重建 progress window 的
authored 候选建立离线 lifecycle 事实边。该边不包含缺陷 verdict，不改变
原 Trace，不反馈 Agent；节点是否有缺陷、是否受上游影响以及是否为根因仍
完全由后续 Judge 和独立确认决定。

真实 Bundle 的无模型预检结果：

- `dec236` 在三个缺陷 seed 中均变为 `assessment_eligible=true`；
- `offered_rank` 分别为 19、23、23；
- 每条边均引用候选自身、两个实际 progress episode 和对应失败 seed；
- Python 全量回归为 1721/1721 通过；
- `git diff --check` 通过。

### 10.3 修复后运行结果

修复后冷启动运行完成全部 18 个全局 Judge 分页，并继续执行 26 个递归节点
判断。由于后续请求与 checkpoint 写入出现长尾，在事务边界用 SIGINT 收口；
系统正常生成 33 MiB 报告并保留可恢复 checkpoint。

| 指标 | 修复后结果 |
| --- | --- |
| 全局 Judge 物理请求 | 74 |
| 总逻辑 Judge 调用 | 48 |
| 总物理请求 | 132 |
| 递归节点判断 | 26 |
| 已识别 present 过程缺陷 | `dec213` 未兑现实施承诺 |
| 根因确认 | 0 |
| 最终结果 | `partial`，`signal_interrupted` |

相比修复前，正确候选已进入聚类、全局判断和递归回退，说明候选资格修复有效。
但新的首个分歧出现在完整节点语义交付：`dec236` 的 10999 字节 rationale
artifact 被标成 `owner_ineligible`，全局 Judge 只能看到约 300 字的开头预览，
看不到后文的 `I'll write the missing methods`。递归 Judge 两次尝试均因缺少
candidate-local commitment evidence 而被严格校验为 `unknown`，没有发布伪根因。

直接原因是 artifact owner 校验复用了分析起点的正式 revision 绑定条件：
`dec236` 是有效 active evidence，也确实直接引用该 artifact，但当前 Trace
只在 case manifest 记录 `subject_revision`，没有在 artifact owner 节点记录
逐节点 revision binding。完整内容能够通过哈希读取，却不能进入 Judge capsule。

### 10.4 新增性能事实

修复后 checkpoint 的 `investigation-actions.jsonl` 增长到约 1.7 GiB，而最终
报告为 33 MiB、Judge cache 仅 264 KiB。后期单次模型请求多为 31-1078 秒，
但请求之间还出现约 17 分钟的 checkpoint 序列化间隔。当前快照式 journal
重复保存大对象，已成为长 Trace 全量归因的主要运行瓶颈之一。

### 10.5 下一优先级

1. Trace 侧为 artifact-owning Causal IR 节点记录 case-start subject revision
   及有效 provenance，保持严格 owner 校验而不是放宽归因安全边界；
2. checkpoint 改为增量状态事件与内容寻址快照，避免每个节点重复写入全量
   candidate capsule、journal 和大文本；
3. 重新生成一个真实 benchmark Trace，验证完整 rationale 进入 capsule 后，
   `dec213/dec236` 的节点判断、后向传播与独立确认能否闭环；
4. 再扩展到错误修改、工具结果误读和 subagent 交接等不同开源 benchmark
   缺陷，报告人工分析与模块分析的 Recall@K、Top-1 和根因角色一致率。

## 11. 2026-08-05 Revision Binding + Merkle Checkpoint 迭代

### 11.1 实施内容

本轮解决上一轮暴露的两个基础设施问题：

1. `CaseTrace.node()` 在配置了 `subjectRevision` 时，为所有在线 Causal IR 节点
   注入不可伪造的 `case_id`、`subject_revision` 和
   `revision_provenance_status=valid`。调用方同名字段不能覆盖 case-start 绑定；
2. checkpoint 对大于 16 KiB 的嵌套 payload 子树使用 SHA-256 内容寻址存储。
   journal 只保存 Merkle 引用；恢复时严格校验路径、类型、长度、哈希、JSON 和
   循环引用，再还原为与原逻辑 payload 完全相同的对象；
3. 归因输入与人工答案分离。模型输入只包含官方测试事实和结构化失败签名，
   不包含人工选择的 `dec_211`、`chg_7` 或根因标签。

对应设计和执行计划：

- `docs/superpowers/specs/2026-08-05-revision-bound-merkle-checkpoint-design.md`
- `docs/superpowers/plans/2026-08-05-revision-bound-merkle-checkpoint.md`

### 11.2 回归验证

| 验证项 | 结果 |
| --- | --- |
| `case-trace.test.ts` | 144/144 通过，1973 assertions |
| checkpoint 单测 | 89/89 通过 |
| Python 全量归因测试 | 1724/1724 通过，239.677 秒 |
| TypeScript typecheck | 通过 |
| macOS arm64 单目标构建 | 通过 |
| 二进制 smoke test | 通过 |
| `models.dev` 不可用 | 5 秒后使用 bundled snapshot，构建继续 |
| `git diff --check` | 通过 |

真实上一轮 22,365,815 字节状态快照重复提交 10 次时，逻辑数据约 223.7 MB，
新 checkpoint 的 journal 与 blob 合计 12,711,742 字节，恢复值完全一致，
物理存储减少约 94.3%。

### 11.3 真实 FeatureBench 执行

使用本轮 macOS arm64 二进制和 `deepseek-v4-flash`，通过
`opencode serve -> HTTP session -> request` 执行官方开源 Sphinx case：

`sphinx-doc__sphinx.e347e59c.test_domain_c.4068b9e8.lv1`

运行在外部 SIGINT 前记录：

| 指标 | 结果 |
| --- | --- |
| LLM 调用 | 45 |
| 工具调用 | 65 |
| 生产变更 | 8 |
| 正式 verification | 0 |
| Trace records / artifacts | 1361 / 753 |
| 最终状态 | `cancelled`，`interrupted_before_case_completion` |

Trace 最终完整生成，但约 900 KiB 上下文的多层 message transform 单阶段耗时
达到 5 至 7 分钟，SIGINT 后 finalization 也超过 runner 的 5 分钟等待上限。
这是在线 Trace 大上下文物化与退出收尾的性能问题，不是本轮离线 checkpoint
的写入问题。

官方 FeatureBench Docker image 评测结果：

- Fail-to-pass: 2/35 通过，33/35 失败；
- Pass-to-pass: 81/81 通过；
- 主失败：`sphinx/domains/c/_symbol.py:472` 抛出
  `NameError: on_missing_qualified_symbol is not defined`。

人工后向语义污点链为：

1. 官方测试在 `_symbol.py:472` 暴露未定义回调；
2. `chg_7` 将该调用写入生产代码；
3. edit 工具按输入原样落盘，没有独立引入语义变换；
4. `record:decisionnode_dec_211_af641618` 生成并选择了包含该错误的完整 edit
   payload，因此是首个可优化的缺陷引入节点；
5. SIGINT 是缺少验证和 case 取消的独立外部原因，不是 NameError 的代码根因。

### 11.4 Artifact 语义交付效果

`dec_211` 的 14,877 字节 rationale artifact 已通过正式 owner 绑定和哈希校验，
完整进入全局 Judge capsule，而不是只提供 preview。该 capsule 同时包含：

- decision、tool.call、tool.result 的 action group；
- 完整 edit payload 和 recent reasoning；
- decision 到 tool.call、tool.result 和后续 context 的已记录数据流；
- case-start subject revision 和 owner binding identity。

这证明上一轮 `owner_ineligible` 导致关键大文本无法进入 Judge 的问题已在决策
节点上修复。真实 Trace 中 21 个被检查的 artifact-backed decision owner 均为
`active_revision_start_eligible`。

`chg_7` 本身虽然已经携带正确 subject revision，但其 `revision_after=7` 早于
case 最终 repository revision 8，因此被当前 active-revision 策略视为历史状态
而不具备 active evidence 资格。它不是缺少 revision binding，而是当前策略把
“可证明发生过的历史变更事件”和“可代表最终仓库状态的证据”使用了同一资格。
后续应拆分这两类资格，允许历史变更作为因果事件证据，同时禁止其声明当前状态。

### 11.5 无答案泄露的离线归因结果

模型只收到官方测试的结构化 `NameError` 失败签名，失败 seed 的
`source_refs=[]`。归因结果：

| 指标 | 结果 |
| --- | --- |
| Trace 图节点 | 1407 |
| 全局候选 | 24，分为 3 页，每页 8 个 |
| 人工根因 `dec_211` | 第 1 页第 1 位；185 个扩展候选中第 11 位 |
| 单页节点压缩率 | 99.4314% |
| 单页字节压缩率 | 98.31% 至 98.54% |
| 物理 Judge 请求 | 64 |
| 逻辑 Judge 判断 | 22 |
| 全局 Judge 物理请求 | 18 |
| confirmed root | 0 |
| 最终结果 | `inconclusive` |

因此本轮得到两个不同层次的结论：

1. 候选召回有效。正确节点在没有根因引用提示的情况下进入 Top-1 全局候选，
   日志检索范围从 1407 节点缩小到首屏 8 节点；
2. 根因确认无效。三个全局页都因输出契约校验失败被拒绝，递归阶段对
   `dec_211` 的判断也因 evidence ref 或 commitment lifecycle 字段不符合严格
   约束而变成 `judge_unknown`，最终没有发布伪根因。

主要校验失败包括：

- `unsupported contribution mechanism type`；
- `unsupported global candidate candidate_phase`；
- `positive introduction failure mode requires root_candidate causal role`；
- `process_assessment evidence_refs must cite grounded evidence refs`；
- `fulfilled commitment requires observed delivery`。

64 次物理请求只形成 22 次逻辑判断，说明请求预算主要消耗在 JSON/语义契约
修复，而不是候选覆盖。候选发现能力已经明显强于根因判断与确认能力。

### 11.6 本轮 checkpoint 实际效果

本次真实归因产生 193 条 checkpoint journal 记录。将全部 Merkle 引用还原后，
逻辑 payload 为 269,686,238 字节；物理 checkpoint 为 21,975,046 字节，包含
210 个去重 blob。物理存储减少 91.85%，等效去重倍数 12.27。

相比上一轮约 1.7 GiB 的 action journal，内容寻址方案已把 checkpoint 从主要
运行瓶颈降为可接受量级，并保持精确恢复和完整性失败关闭。

### 11.7 下一轮优先级

1. P0：统一 Judge 输出协议。由系统提供枚举候选和合法 evidence ref selector，
   模型只选择 ID 和角色，不再自由生成 `candidate_phase`、mechanism 和长引用；
2. P0：把一次失败后的修复拆为语法修复与语义归一化。可确定映射的同义值由
   本地规范化器处理，真正矛盾才重新请求模型；目标是物理/逻辑请求比降至 1.5；
3. P0：对 Top-K 候选做独立最终裁决。候选发现阶段保持高召回，最终 Judge
   一次获得失败签名、Top-K capsule、候选间数据流和反事实问题，完成比较式确认；
4. P1：拆分 `historical_event_eligible` 与 `active_state_eligible`，让 `chg_7`
   可作为历史因果事件参与链路，又不能冒充最终仓库状态；
5. P1：优化大上下文 transform 和 signal finalization，保证 SIGINT/SIGTERM
   后在 runner 等待窗口内完成 Trace/HTML 收口；
6. P2：在错误工具解释、上下文压缩和 subagent 交接 case 上重复无泄露测试，
   报告 Candidate Recall@K、Top-1、confirmed-root accuracy 和首次分歧节点。
