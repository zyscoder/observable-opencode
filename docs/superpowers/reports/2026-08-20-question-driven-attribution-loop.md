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
