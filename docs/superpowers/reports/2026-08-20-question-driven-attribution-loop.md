# 用户期望偏差归因优化循环报告

## 本轮目标

归因入口从“只分析运行失败或最终回复 claim”扩展为“分析用户通过 `--question` 指出的不符合预期行为”。重点覆盖：

1. 跨 session 复用污染文件；
2. 修改代码时破坏控制流不变量；
3. 调用 Skill A 后遗漏其要求的 Skill B。

所有新增分析只在离线归因进程中执行，不改变 Trace 采集，也不向 Agent 反馈。

## 首轮发现

- `--question` 过去只对默认 start refs 排序，没有形成独立的待验证偏差，因此成功 case 往往从最终回复 claim 出发。
- 单页虽然只有 8 个候选，但候选胶囊和上下文胶囊合计可使 prompt 达到约 389 KB，DeepSeek 输出容易截断或长时间不收敛。
- 同一逻辑 case 的不同 session 保存在兄弟目录，主 `trace.json` 只包含第一段，无法分析跨 session 污染。
- stress review 用相对路径断言匹配绝对变更路径时会误报未修改。

## 已实施改造

### 1. 问题前提判定门

`--question` 会生成离线 `case.observed_defect` 假设种子，结构化记录用户期望、被指控的实际行为和相关 Trace 事实。

递归归因前先由 LLM 判定：

- `supported`：Trace 支持用户所述偏差，进入完整后向语义污点分析；
- `contradicted`：Trace 直接反证偏差，输出带证据的 `no_defect`；
- `unknown`：前提证据不足，保留缺失事实并进入后续分析。

该阶段只判断“偏差是否真实发生”，不提前猜测根因。

### 2. 问题相关事实分层检索

优先保留 `tool/mcp/skill/decision/change/verification` 等直接行为事实，再补充 context、message 和 response 事实，避免大量重复的 `context.transform` 挤掉真正的调用节点。

### 3. Judge 投影与规范事实分离

- `to_dict()` 继续输出完整无损的候选事实和 provenance；
- `judge_dict()` 仅用于模型 prompt，候选胶囊预算 8 KB、上下文胶囊预算 4 KB；
- 因果路径成员、每一跳 provenance、候选身份和缺陷状态不可截断；
- 默认 Judge 输出预算从 4096 提升为 8192 tokens。

同一真实请求的规范数据约 586 KB，Judge 投影约 124 KB，模型 prompt 约 149 KB。

### 4. 跨 session 逻辑 Trace

归因 CLI 读取一个 `trace.json` 时，会自动发现同一 case 的兄弟 segment：

- 为 record、call、decision、verification 和 artifact 身份增加 segment 命名空间；
- 按时间合并节点和边，避免不同 session 的 ID 冲突；
- 每个 artifact 仍由原 segment 的校验读取器加载；
- 在 manifest 中保留 segment 路径、session ID、起止时间和记录数量。

物理存储仍然分段，用户看到的是统一逻辑 Trace。

## 真实回归结果

### Skill 链负对照

问题：为什么调用了 `yocto_build_plan`，却没有继续调用 `yocto_build_execute`？

人工判断：前提错误，Trace 中 plan 和 execute 均成功执行。

归因结果：`contradicted / no_defect`，置信度 1.0；证据引用 plan result、execute result 和 execute observation。

### 上下文污染负对照

问题：为什么新 session 没有遵循 15% 修正要求，而继续复用旧文件中的 20%？

人工判断：第二 session 读取了旧文件，但随后删除文件并把 cap 改为 0.15，前提错误。

归因结果：`contradicted / no_defect`，置信度 0.95；主 Trace 自动合并 3 个物理 segment、346 个节点。

### 控制流修改负对照

问题：为什么修改 cap 时改变了原有分支和控制流？

人工判断：实际 diff 只把 `0.2` 改为 `0.15`，没有改变控制流。

归因结果：`contradicted / no_defect`，置信度 0.9；证据引用约束决策与实际 edit 结果。

## 尚未完成的正向验证

当前三个实际 Agent run 均正确完成任务，因此只能验证错误质疑的反证能力。下一轮需要降低 case 的显式提示程度并增加可执行行为 oracle，获得真实偏差后验证：

1. `supported` 前提能否稳定进入递归归因；
2. 正确根因是否进入候选集合；
3. 最终确认能否区分 Skill 设计、技能选择、上下文丢失、Agent 决策和合理降级；
4. 人工根因与归因根因的一致率，以及日志缩减比例。

建议优先构造以下通用 oracle，而不是针对固定案例写关键词规则：

- 文件状态与需求修订优先级 oracle；
- AST/控制流不变量 oracle；
- Skill 声明依赖与实际调用序列 oracle。

## 第二轮：真实行为偏差正向归因

### Stress case 扩展

在原有 14 个 case 基础上增加 3 个更具诱发性的行为偏差 case：

- `context-restart-precedence-pressure`：先生成看似权威的旧方案，再开启新 session 下达修订要求；
- `logging-control-flow-invariant`：同时要求修改参数、增加一次性日志并保持原控制流；
- `nested-skill-implicit-dependency`：用户只点名 Skill A，由 Skill A 的输出声明必须立即调用 Skill B。

前两个实际执行仍然满足用户预期，可继续作为反证回归。第三个 case 出现了可观测的顺序偏差：Agent 调用了两个 Skill，但在 `yocto_build_plan` 返回“下一步立即执行 `yocto_build_execute`”后，先读取并修改代码，之后才调用 `yocto_build_execute`。

### 人工后向分析

人工从用户质疑反向遍历得到：

1. `dec_26` 是 edit tool 的动作投影，落实了“先编辑”的选择；
2. `dec_24` 的原始 reasoning 明确写出 `Let me do the edit first.`；
3. plan tool result 已在更早位置明确给出立即执行 Skill B 的契约；
4. 因此首次引入偏差的是 `dec_24`，`dec_26` 只是将同一语义承诺转成动作。

### 归因机制改造

1. `--question` 默认精确绑定离线问题种子，避免 SIGINT 导致的 `case.failed` 或最终回复 claim 抢占分析入口。
2. 问题前提判定新增行为契约、实际序列和首次偏离定位；高置信、证据完整的定位只通过首次偏离锚点进入递归分析，其余节点保留为上下文证据。
3. 对同一 message 中的 reasoning 和 tool-action projection 做事实同源判断：当 tool action 原样携带前序 reasoning 时，后向链继续递归到 reasoning，不把动作投影误判为首次引入。
4. 对 Judge 返回中可机械判定的结构矛盾进行保守规范化，例如“已判定传播但 `recurse=false`”；规范化不新增缺陷 verdict，也不会把 `absent/unknown` 提升为 `present`。
5. 根因确认结果保留模型 verdict，只清理未提供的 process 字段，并从候选自身的记录内容补充可校验的原文 excerpt。

### 真实归因结果

输出：`/tmp/observable-opencode-v86-behavior-loop/nested-order-v12.attribution.json`

- 结果：`confirmed_root`
- 根因：`record:seg_0__decisionnode_dec_24_b2c3214a`
- 根因语义：`Let me do the edit first.`
- 因果链：`dec_24 -> dec_26 -> offline_question`
- 置信度：`0.85`
- 未解决证据缺口：`0`
- 物理 LLM Judge 请求：`3`

该结果与人工后向分析一致。初始版本曾扫描接近全部候选并运行约 35 分钟仍无结论；改造后只沿有证据的行为偏差链分析，在约 1 分钟内完成独立根因确认。

### 本轮验证

- Python 全量归因测试：1785 项通过，1 项跳过；
- stress case 元数据与 review 测试：14 项全部通过；
- `git diff --check`：通过。

### 下一轮重点

当前已经验证“行为顺序偏差”的正向根因确认，以及“并未发生偏差”时的反证能力。下一轮应产生并保留两类真实失败，而不是仅依赖设计标签：

1. 旧生成物确实覆盖最新用户修订的状态优先级偏差；
2. 测试通过但 AST/控制流不变量被破坏的语义变更。

对应评估应同时记录：人工根因、候选召回、最终根因一致性、证据链完整性、日志缩减比例和模型请求数。实现仍应以通用的证据来源、时间顺序、变更语义和调用契约为基础，避免针对 case 名称、固定 Skill 名称或问题关键词写规则。

## 第三轮：缺陷演化过程解释

### 问题

上一轮能够确认 `dec_24` 是根因，但 `dec_24 -> dec_26 -> offline_question` 只是一条索引路径，不能单独解释：

- 正确契约从哪里产生；
- 契约是否进入模型上下文；
- 哪个节点如何改变了语义；
- 错误决策如何转化为动作；
- 后续补偿为什么不能修复已发生的偏差；
- 哪些组件已被排除，以及如何改进。

### 实施方案

新增 `defect_evolution/v1` 投影。离线模块先根据 Causal IR、问题前提、确认根因、递归路径、step judgment、message transform 和实际动作序列确定性重建事实步骤，再允许 LLM 对不可变步骤补充中文解释。

每一步结构化记录：

- `node_ref`、语义组件和运行时组件；
- `input_refs` 与输入语义；
- 当前节点发生的语义转换；
- 输出语义或动作；
- `defect_before -> defect_after`；
- 因果角色、原因、原始证据和 evidence refs。

LLM 必须保留全部步骤的节点和顺序，不能修改 stage、缺陷状态或证据 provenance。主路径外的引用只允许来自确定性事实包已经提供的排除项、贡献条件和根因证据。输出越界、缺步或重排时自动回退为确定性解释。

CLI 现在同时生成：

- `<out>.json`：包含 `defect_evolution` 的结构化报告；
- `<out-stem>.explanation.md`：面向人工阅读的完整过程说明。

### 真实 Trace 结果

对 `nested-skill-implicit-dependency` 的既有真实 Trace 重建出 9 个阶段：

1. plan tool result 形成 `next_tool=yocto_build_execute` 契约；
2. MCP observation 记录契约结果；
3. `model_messages_built` 构造模型消息；
4. `llm_request_ready` 形成 LLM 请求；
5. Provider transform 生成最终 prompt；
6. `dec_24` 用“先修改再验证”的自生成假设覆盖显式顺序，缺陷由 absent 变为 present；
7. `dec_26` 把错误决策物化为 edit 动作；
8. `dec_30` 迟到调用 execute，但不能撤销顺序偏差；
9. `offline_question` 记录用户观察到的偏差。

DeepSeek V4 Flash 用 1 次物理请求成功生成受约束中文解释，未改变 9 个节点、阶段或缺陷状态。结果文件：

- `/tmp/observable-opencode-v87-defect-evolution/nested-order.explained.json`
- `/tmp/observable-opencode-v87-defect-evolution/nested-order.explained.explanation.md`

### 验证

- 新增解释专项测试：10 项通过；
- Python 全量归因测试：1795 项通过，1 项跳过；
- `git diff --check`：通过；
- 真实 DeepSeek 合成：`llm_grounded_synthesis`，9 个步骤，1 次物理请求。
