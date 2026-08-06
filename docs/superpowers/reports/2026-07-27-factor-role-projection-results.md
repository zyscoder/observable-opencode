# Factor Role Projection 真实基准校准结果

## 1. 评估范围

本轮验证 Factor Role Projection 是否能在不改变 Harness、Agent、工具和
Benchmark 执行行为的前提下，基于既有语义 Trace 完成：

1. 必要根因、贡献条件、放大因素和下游物化的区分；
2. 人工后向语义污点分析与离线归因模块结果对照；
3. 大 Trace 下的候选召回、LLM 判断稳定性和成本测量；
4. completed checkpoint 的零 Provider 调用回放；
5. Trace、报告、Judge cache 和 checkpoint 的可复核持久化。

代码基线为分支 `codex/message-context-lineage`、HEAD
`5e2eb4e38b7c516ff498271a82292116ea5f64b8`。工作区包含本阶段尚未提交的
Factor Role Projection 改动，因此结果同时记录输入与输出 SHA-256，不将
Git HEAD 单独视为完整可复现身份。

Provider 使用 Anthropic 兼容协议的 DeepSeek `deepseek-v4-pro`。密钥仅通过
运行时环境注入，没有写入命令、Trace、报告或仓库文件。两个 FeatureBench
case 均通过 `opencode serve -> HTTP session -> message` 执行，不是
command-shell case runner。

## 2. Case 矩阵

| Case | 失败形态 | Trace 规模 | 外部结果 |
| --- | --- | ---: | --- |
| Sanitized Sphinx `sphinx-recursive-minimal` | 缺失契约、条件与超时角色区分 | 6 节点、6 边、2,108 B | 人工冻结标签 |
| FeatureBench Lite Seaborn `mwaskom__seaborn.7001ebe7.test_regression.ce8c62e2.lv1` | 多接口实现错误、目标测试遗漏、错误完成声明 | 2,403 records、10,253 edges、55,289,563 B | `34 failed, 18 passed, 9 skipped` |
| FeatureBench Lite Pydantic `pydantic__pydantic.e1dcaf9e.test_deprecated_fields.40a2ec54.lv1` | 基线缺失定义、Agent 增量遗漏、错误验证目标、SIGTERM | 2,106 records、9,651 edges、49,958,337 B | `9 failed, 3 passed`；case 被 SIGTERM 中断 |

人工分析保存在：

- `.superpowers/sdd/task-6-seaborn-manual-analysis.md`
- `.superpowers/sdd/task-6-pydantic-manual-analysis.md`
- `.benchmark-runs/factor-role-projection-20260728/seaborn/manual-review.json`
- `.benchmark-runs/factor-role-projection-20260728/pydantic/manual-review.json`

## 3. 结果总览

| 指标 | Sphinx | Seaborn | Pydantic |
| --- | ---: | ---: | ---: |
| 最终状态 | `confirmed_root` | `inconclusive` | `inconclusive` |
| 人工根因数 | 1 | 4 | 1 |
| 候选根因覆盖 | 1/1 | 3/4 | 0/1 |
| 最终根因命中 | 1/1 | 0/4 | 0/1 |
| 条件候选覆盖 | 1/1 | 1/1 | 1/1 |
| 放大因素候选覆盖 | 1/1 | 不适用 | 1/1 |
| Physical Provider requests | 9 | 38 | 10 |
| Logical Judge calls | 5 | 18 | 4 |
| Repair/重试开销 | 4 | 20 | 6 |
| Step unknown | 不适用 | 8/18，44.44% | 3/4，75% |
| 最终 FactorRole 判断数 | 3 | 0 | 0 |
| completed replay 新 Provider 请求 | 0 | 0 | 0 |

三例共有 6 个人工根因。模块候选池覆盖 4 个，候选召回为 66.67%；最终只确认
Sphinx 的 1 个根因，最终召回为 16.67%。由于 Seaborn 和 Pydantic 没有发布
根因，不能宣称跨 Benchmark 的量化目标已经达成。

## 4. Sphinx：小图闭环成功

Sphinx 的比较结果为：

- 根因 Top-1：命中 `record:decision`；
- 根因 recall / precision：`1.0 / 1.0`；
- 语义根因 recall / precision：`1.0 / 1.0`；
- introduction recall / precision：`1.0 / 1.0`；
- `record:prompt` 被正确发布为 `contributing_condition`；
- `record:change` 的独立 FactorRole 判断为 `unknown`；
- `record:timeout` 已作为放大因素候选被覆盖，但角色仍被误判为
  `downstream_materialization`；人工标签为 `amplifying_factor`；
- v4 全角色 role-pair precision / recall / F1 为
  `0.5 / 0.25 / 0.333333`；旧 `factor_role_precision` 字段保留为
  pair precision 的兼容别名，不再表示“全角色都正确”；
- overall unknown rate 为 `0.25`，necessity unknown rate 与 non-root role
  unknown rate 均为 `0.333333`；`necessary + factor_role=unknown` 作为升级
  信号，不计入角色 abstention；
- human/LLM item disagreement 为 `3/5 = 0.6`；原 role-pair Jaccard
  distance 以明确字段
  `root_and_factor_role_pair_jaccard_distance` 保留，值为 `0.666667`；
- fabricated refs / unresolved confirmed facts 均为 `0`。

四类完整标签的混淆结果是：condition 正确命中 1 个；人工 amplifier
`record:timeout` 被判为 materialization；人工 materialization
`record:change` 为 unknown；人工 unrelated `record:verification` 未进入角色
评审。v4 metric scope 明确覆盖 condition、amplifier、
downstream_materialization、unrelated；v3 标签仍可读取，但只发布
condition/amplifier 的 partial scope，不再声称全角色 precision。
item-level disagreement 的分母是 1 个根因加 4 个 factor 的 5 个唯一
semantic occurrence；正确 root 和 condition 不分歧，timeout 错类、
change unknown、verification not-reviewed 共 3 项分歧。预测类别显式覆盖
root、四类 factor、unknown、not-reviewed、necessary escalation、rejected
和 conflict。

Global 与独立 FactorRole 对照中，三个非根候选有两个不一致：

- `record:prompt`：双方均为贡献条件；
- `record:change`：Global 为贡献条件，独立判断为 unknown；
- `record:timeout`：Global 为 outcome evidence，独立判断为下游物化。

因此 Global-vs-independent disagreement 为 `2/3`。这表明必要根因闭环已经
稳定，但“中断放大”和“结果物化”仍缺少可靠判别边界。

## 5. Seaborn：候选召回强于最终确认

人工根因是：

- `record:decisionnode_dec_230_cc667198`：线性绘图器实现动作；
- `record:decisionnode_dec_235_6a5a9f9c`：大规模回归绘图器实现动作；
- `record:decisionnode_dec_426_e388d170`：验证选择遗漏目标测试；
- `record:decisionnode_dec_439_6547c429`：错误完成声明。

模块候选池覆盖后三个，根因候选召回为 `75%`，并覆盖人工条件
`record:node_1043_f7724803`。但是：

1. `dec_230` 虽然在 Trace 中存在
   `decision -> edit tool_call -> change` 显式链路，旧 change 已被活动修订
   策略判为非当前证据；一跳证据和 progress 候选又占满 24 项闭包预算，最终
   没有将该活动 decision 投影回候选池。
2. 两个 seed 的 Global capsule 分别达到 1,690,498 B 和 1,603,241 B，
   open root 数为 29 和 18。系统正确触发
   `oversized_dense_root_matrix`，没有把超大请求发送给 Provider。
3. 递归 Judge 的 18 次节点判断中有 8 次因响应契约失败降级为 unknown。
   主要错误是：present defect 无后续动作、输入来源自身无缺陷却继续递归、
   选择未提供 predecessor、以及 absent 状态同时声明缺陷传播。
4. 最终没有进入 root confirmation，也没有 FactorRole 发布。

人工分析能够从 Trace 定位到实现与验证根因；离线模块已经把 2,403 个 records
压缩为 108 个唯一候选、实际访问 12 个节点，但尚不能把候选稳定收敛为根因。
这验证了“先缩小检索范围，再交给 LLM 判断”的方向可行，同时也说明当前
Judge 合约与候选闭包仍是主要瓶颈。

## 6. Pydantic：条件和中断可见，关键实现决策漏召回

人工分析区分了三类事实：

- 基线或未修复缺陷：`_computed_field_schema`、`_update_from_config` 等定义
  缺失；
- Agent 增量缺陷：`record:chgnode_chg_7_c30f39c0` 没有传播 wrapped
  `__deprecated__`；
- 运行时放大因素：`record:process_signal_49ae4714`，只中断最后写入，不是
  源码根因。

人工标签将 `record:decisionnode_dec_327_a0b88b89` 标为 Agent 增量实现根因，
将 `record:decisionnode_dec_170_2e4a8a6e` 标为条件，将 SIGTERM 标为放大因素。

Trace 中完整存在
`dec_327 -> bash tool_call -> chg_7 -> verification`，且这些节点均属于活动
修订。因此问题不是语义 Trace 缺边，而是 Global grounded closure 在遍历到
二跳 authored decision 前已被大量一跳来源占满。模块覆盖了条件、SIGTERM、
change 和 case failure，却没有覆盖关键 decision。

两个 seed 的 Global capsule 为 1,732,701 B 和 1,837,281 B，open root 数为
15 和 14；均被 `oversized_dense_root_matrix` 正确旁路。4 次递归节点判断有
3 次契约失败，最终没有发布根因或角色。模块在旧的默认起点运行中能够明确说明
SIGTERM 是 amplifier 而不是 root，但显式双起点运行仍优先分析了验证/中断
分支，没有回溯到实现决策。

## 7. Trace 与 Evaluator 质量

两个 FeatureBench Trace 的核心 Agent 数据流可用于人工归因，但仍不满足当前
严格 Evaluator 的完整源 Trace 契约：

| Trace | 非规范 artifact hash | 未解析 attribution edge | 未解析 source ref | 总计 |
| --- | ---: | ---: | ---: | ---: |
| Seaborn | 1,269 | 479 | 513 | 2,261 |
| Pydantic | 1,089 | 423 | 449 | 1,961 |

主要原因是旧 Trace 使用 16 位截断 artifact hash，以及
`span:`、`observation:`、`context_snapshot:` 等别名未全部投影为规范
`record:` 引用。严格评分因此拒绝这两个源 Trace；本报告没有绕过该安全约束，
也没有将手工评分投影伪装成严格 evaluator 输出。

本轮修复了以下真实运行问题：

1. Factor mechanism 的 target 必须属于 `recursive_path[1:]`；合法中间下游
   节点继续允许，self-target、上游 target 和非路径 allowed fact 均在
   prompt、repair、parser、action/report reconciliation 中 fail closed。
2. Evaluator v4 覆盖四类非根角色并发布 role-pair precision/recall/F1、混淆
   矩阵和 metric scope；necessity unknown 与 non-root role unknown 分开计量。
3. report、checkpoint 和 restore 使用同一个规范候选快照，消除重复候选来源
   对同一 graph node 的快照分歧。
4. 超大且稠密的 Global root matrix 一律旁路，避免 100 万 token 级请求和
   repair overflow。
5. 每个 completed checkpoint 的正式 restore 都只返回不可变 replay proof；
   只有共享 exact legacy-shape classifier 返回 `LegacyProjectionRequired`
   时，才从该 proof 派生 config/output/lineage/journal/terminal-action 绑定
   migration decision。现代 Sphinx 不派生 decision，普通 evaluator 始终
   传 `None`。
6. 四个 superseded lineage 字段统一作为 audit-only 投影；它们保留在
   Trace/report 审计中，但不会进入 FactorRole、root confirmation 或 Global
   Judge prompt。
7. Evaluator 接受原始、水合、净化原始和净化水合四种规范候选快照，同时继续
   拒绝 artifact 内容漂移、owner 漂移和伪造引用。

这些变更都发生在离线 Trace/归因路径，不反馈给 Agent，不改变被测 Agent 的
决策、上下文、工具调用或 Benchmark 结果。

## 8. 产物、体积与哈希

| Case | 产物 | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| Sphinx | Trace | 2,108 | `5e63e8ae155a29740b8b0c2852c5e0964926be18f89a2e4023a66055e8d51490` |
| Sphinx | Human labels v4 | 1,771 | `b3298456344596bd800e8e000af9c24d4a8a6f8bf4c49b7501e43a58bda01c98` |
| Sphinx | Report | 400,724 | `43a74183fa11dd976592b31242c106d2ce7fb56298892b7e02b6b31e9072e1a3` |
| Sphinx | Judge cache | 14,125 | `39edfa459209fc8c5c81bb49ffd7d0c976d7cca4c7197cc8aa7332fe720d7a6f` |
| Sphinx | Comparison v7 | 6,608 | `fb8ad2069d72c50c9d3b95b591cb7066d6686db9166e4530e3143ba124b38269` |
| Seaborn | Trace | 55,289,563 | `9d9aeacf8df828368ca837fe2de74f09da72edadb78ea6700b9d6cd96f3ee94f` |
| Seaborn | Report | 2,117,591 | `0c9cda6f19ca0ad32a36b9c468281941e86a30b10d2c54ff5710e4ac3beb70ca` |
| Seaborn | Judge cache | 41,080 | `0aec8f747a30ee1335c1a565f9e5534cb59c19da4c576d481bd63320ac2e1073` |
| Pydantic | Trace | 49,958,337 | `97d48e13df5b7113d99fd40637b298721f086b7a7b028c8dc31aa20be977088e` |
| Pydantic | Report | 1,171,608 | `e85dd4fdc34f83fdd8c67550da8cc815804bd88259318effec26ccf6c8a0503e` |
| Pydantic | Judge cache | 5,459 | `04c15a9658e2ee36c185e184089c7975961460b004a11e660390ced92c1c5ad1` |

Checkpoint bundle 体积分别为：

- Sphinx：2,717,257 B；
- Seaborn：46,157,525 B；
- Pydantic：12,608,730 B。

对三个 completed checkpoint 使用相同 trace、review、起点、模型身份、预算和
输出路径重放。三次均未修改 report/cache SHA 或 mtime，可确认新增 Physical
Provider request 为 `0`。Sphinx classifier 返回
`LegacyProjectionNotRequired`，migration decision 派生次数为 `0`；
Seaborn-v2 和 Pydantic-v2 均精确匹配历史 bypass shape，各派生一次 decision。

Checkpoint bundle 使用未加密 SHA-256 链和原子 commit head，承诺的是本地
崩溃恢复、意外截断和非协同损坏检测，不承诺抵抗能够改写同目录全部文件并重算
所有 SHA 的恶意本地写入者；它不是签名、时间戳服务或透明日志。completed
replay 现在复用正式 `CheckpointBundle.restore` 验证完整 config identity、
三条 journal 的 schema/head/hash chain，并额外验证 published report 与当前
Trace 重建出的 lineage 字节、terminal action 和 output commit。所有 completed
bundle 都得到只表达完整性的 replay proof；exact classifier 的身份、原因和
完整 shape identity 进入 migration decision identity，授权语义不再预签发。
该过程不写旁路迁移文件，因此三份 completed 产物保持字节不变。剩余风险是：
若攻击者同时控制调用方提供的 Trace/config 和整个 bundle，仍可整体重签；
消除此风险需要工作区外的可信签名或透明日志。

## 9. 验证结果

- 最终复审 replay-proof/classifier/evaluator 定向模块：
  `207 tests in 9.888s`，全部通过。
- 三个 completed checkpoint 最终回放：Sphinx decision `0`，
  Seaborn-v2/Pydantic-v2 decision 各 `1`；Provider 调用均为 `0`，每例
  report/cache/lineage/checkpoint 共 9 个文件的 SHA-256 与 mtime 均不变。
- 最终分支审查受影响模块：`614 tests`，全部通过（FactorRole/Judge/
  Judge-visible projection `402`；checkpoint/evaluator/analyzer/CLI `212`）。
- Sphinx v4 labels evaluator 在现有 comparison 路径纯离线重跑并生成
  comparison v7：退出 `0`。
- `python3 -m compileall -q tools/trace_attribution`（独立 pycache）：通过。
- 聚焦 checkpoint、capsule、recursive analyzer、benchmark 测试：
  `217 tests`，全部通过。
- artifact snapshot 与 Evaluator 受影响回归：
  `144 tests`，全部通过。
- 最终全量 attribution 测试：
  `1234 tests in 47.087s`，全部通过。
- `git diff --check`：通过。

## 10. 结论与下一阶段

当前版本在小图上已实现“事实闭环、必要根因确认、非根角色投影、零调用回放”。
在真实大 Trace 上，候选发现能力已经有价值：Seaborn 将 2,403 个 records
缩到 108 个唯一候选，并覆盖 75% 的人工根因；但“确认根因”的能力仍明显弱于
“寻找候选”的能力。

下一阶段优先级应为：

1. **候选闭包分层配额。** 对 authored decision、tool/change envelope、
   verification/outcome 和 factor 分配独立预算，保证
   `decision -> tool_call -> change` 的二跳根因不会被一跳证据挤出。
2. **Judge 状态机输出收敛。** 将节点缺陷、传播关系、下一跳选择拆成可独立
   修复的最小响应；对“present 但静默终止”“absent 却传播”等冲突做确定性
   状态归一化，同时保留原始响应供审计。
3. **外部评测事实一等化。** 把官方 target test、revision、失败断言和
   review 起点绑定为结构化 external evaluation fact，避免 Pydantic 的
   取消状态抢占实现缺陷分析。
4. **Trace 生产契约升级。** 新生成 Trace 使用完整 SHA-256，所有 span、
   observation、context alias 生成规范 record 映射；旧 Trace 保留原始字节，
   通过离线规范化视图参与严格评分。
5. **继续真实 Benchmark 校准。** 至少覆盖成功但错误完成、部分修复、工具
   误用、上下文压缩丢失、subagent 失配和外部中断六类 case，再测候选 recall、
   root Top-1、FactorRole F1、unknown rate、请求成本和人工一致率。

因此，本阶段可以判定为：**结构与回放闭环通过，Sphinx 根因闭环通过；真实
Benchmark 的候选缩减有效，但跨 Benchmark 根因确认与 FactorRole 质量目标
尚未达成。**
