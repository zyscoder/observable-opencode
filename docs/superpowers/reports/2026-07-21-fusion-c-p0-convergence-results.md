# Fusion C P0 收敛迭代结果

## 目标

本轮围绕 TerminalBench `cancel-async-tasks` 的真实 Trace，验证融合方案 C 能否从外部观察到的
`process SIGINT` 清理缺陷，稳定后向定位到实现假设与测试设计节点，同时保持离线分析、被动记录、
不改变 Agent 行为、不把归因结论反馈给 Agent。

## 已实施改造

1. 修正派生 Trace 事实：完善测试命令识别、验证结果一致性和 claim 分句，避免把成功验证误标为失败或缺失。
2. 为 progress navigation 增加最多 8 个 delivery episode 的历史窗口，并按当前缺陷语义排序候选。
3. 将 progress aggregate 改为纯离线路由节点，确定性选择最多两个具体候选，再由 LLM 独立判断缺陷。
4. 在分析图中增加可审计的 `semantic_navigation_route`，但不修改输入 Trace 图。
5. 收紧 Judge 状态一致性、候选引用、引入点和独立确认协议；repair 请求明确当前节点不能成为自身前驱。
6. 将 prompt、context transform、LLM call、task loop 等 provenance envelope 每步限制为最多两个，并按语义相关性保留。
7. 修复 `partial/latest.json` 的 artifact 根目录解析：依据 manifest 的 `files.partial_latest` 回到 case 根目录，且拒绝绝对路径与 `..`。
8. 将 `independent_comparison`、`falsification_result` 从 Trace 采集建议移到离线归因建议，继续遵守被动观测边界。

## 实测结果

人工期望根因节点为 `dec_2`（实现假设）和 `dec_18`（测试写入）；`dec_17` 是语义更完整的测试设计推理。

| 版本 | 访问节点 | 物理/逻辑请求 | 结果 |
| --- | ---: | ---: | --- |
| P0 前 | 3 | 9/4 | 未访问人工期望决策，progress aggregate 被误当候选根因 |
| P11 | 2 | 6/2 | 命中 `dec_17`、`dec_2`，但 self-predecessor 输出导致校验失败 |
| P14 | 2 | 4/2 | 候选窗口与请求成本收敛；`dec_2` 仍出现 present 状态无合法收尾 |
| P15 | 2 | 4/2 | 稳定访问 `dec_17`、`dec_2`；恢复 154,760 字节实际存在的 artifact；最终仍为 `inconclusive` |

P15 的自动分析没有确认根因。它把 `dec_2` 判为 absent，并在 `dec_17` 的 provenance predecessor 上产生一次
协议性 unknown。模块没有将不完整结论提升为根因，这是正确的安全行为，但 decisive-result 能力仍未达到可用门槛。

一次三缺陷误跑提供了额外证据：模块曾把 `dec_2` 正确判为 introduction candidate，其理由与人工分析一致，
但独立确认发现 `artifact_6_7e23f4e46793539d` 只有 preview，完整 16,141 字节 rationale 文件未随 benchmark
Trace 打包，因此保持 unknown。该结果说明候选检索已改善，当前主要瓶颈转移到证据完整性和 Judge 协议稳定性。

## 人工对照

- **候选发现**：从人工期望节点 0/2 未访问，提升到精确访问 `dec_2`，并访问语义等价且更丰富的 `dec_17`。
- **执行节点映射**：`dec_18` 在 progress reconstruction 中被配对的 `dec_19 llm_tool_call` 投影替代，缺少稳定的等价身份映射，精确 ref recall 仍为 1/2。
- **语义判断**：LLM 能解释错误 cancellation 假设，但不同调用会在 present/absent 之间波动，说明单一大 JSON 判定协议仍过重。
- **根因确认**：完整候选局部 artifact 缺失时坚持 unknown，没有使用 preview 猜测完整语义。
- **成本**：聚焦单缺陷时物理请求由 9 次降为 4 次；repair 开销仍占 2/4，尚未达到低于 20% 的目标。

## 当前评价

### 语义 Trace

原始流程、决策、工具、变更、验证和结果链路已足以支持人工后向分析；已存在 artifact 的读取路径也已恢复。
主要缺口是 partial bundle 未完整携带 decision rationale 与 tool-argument artifact，以及同一动作的
reasoning、llm tool call、tool execute 三类 occurrence 缺少稳定等价映射。

### 离线归因模块

候选检索和搜索成本已明显改善，保守性与可审计性较好；但节点缺陷判断、前驱选择、引入点声明被塞在一次
复杂响应中，模型容易产生状态与收尾协议不一致。当前适合辅助人工调查，尚不适合自动给出高置信根因结论。

## 下一轮建议

1. **Trace artifact 完整性**：partial/final flush 必须原子复制所有被 manifest 引用的 decision rationale、tool args 和 test content，并生成缺失率校验。
2. **动作身份统一**：为 reasoning decision、llm tool call、tool execute、tool result 建立同一 action occurrence/group ID，避免 `dec_18` 被投影后无法与人工标签对齐。
3. **拆分 Judge 协议**：先单独判断当前节点是否有缺陷，再选择可传播前驱，最后单独判断 introduction；每步使用短 schema，并保存无敏感内容的 invalid-output hash、错误类型和修复结果。
4. **provenance 语义判断**：用结构化的 candidate-local defect 声明替代自然语言正则门槛，让 LLM 可以递归到真正错误的 prompt/context，同时阻止仅有 dataflow 的包装节点扩散污点。
5. **多 benchmark 回归**：用新 collector 重新生成 TerminalBench，并复跑 Axios、Astropy、SWE-bench 与 FeatureBench；同时报告根因 Top-k recall、no-defect accuracy、unknown rate、repair rate 和每例成本。

## 验证

- Python：`467` tests passed。
- TypeScript：本轮前序完整回归 `136` tests passed；本次最终增量仅修改 Python 归因与读取逻辑。
- 最终实测报告：`/private/tmp/observable-opencode-benchmark-eval-20260721-p15-focused/terminalbench/recursive.attribution.json`。
