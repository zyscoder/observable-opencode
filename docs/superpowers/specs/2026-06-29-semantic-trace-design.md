# Observable Opencode Semantic Trace Design

## 背景

当前 observable opencode 已经能够在 benchmark case 执行时生成 `events.jsonl`、`trace.json` 和 `trace.html`，并记录 span、event、artifact、耗时、token、工具输入输出和 HTML 流程视图。这解决了“发生了什么”的基础可观测问题。

下一阶段目标是让 trace 支撑人工或下游系统做根因归因。trace 本身不输出自动诊断结论，而是记录足够完整、结构化、可引用的语义证据，使排查者可以判断问题来自上下文构造、模型决策、工具执行、上下文压缩、代码修改、验证不足或最终回答失真。

## 目标

1. 保留现有 trace 兼容性，新增语义层字段，版本升级为 `trace_version: "1.1"`。
2. 大文本继续 artifact 化，`trace.json` 保存摘要、hash 和 artifact 引用。
3. HTML 支持查看完整上下文、证据链、变更和验证闭环。
4. 不做自动根因诊断，只记录可归因证据。
5. 不记录 API key、Authorization、Cookie 等敏感值。

## 新增语义对象

### `context_snapshots`

记录每次 LLM 调用前的最终输入上下文。

字段：

- `snapshot_id`
- `span_id`
- `phase`: `llm_request` 或 `compaction`
- `provider_id`
- `model_id`
- `agent`
- `message_count`
- `system_count`
- `tool_count`
- `token_estimate`
- `messages`
- `system`
- `tools`
- `metadata`

`messages`、`system`、`tools` 作为 artifact-backed summary 保存。

### `semantic_decisions`

记录模型或运行时做出的关键动作选择。

字段：

- `decision_id`
- `span_id`
- `component`
- `decision_type`
- `intent`
- `chosen_action`
- `rationale`
- `confidence`
- `evidence_refs`
- `metadata`

第一版不强制模型产出结构化思考，只从工具调用、文本结束、压缩、验证和变更行为中抽取可观测决策。

### `semantic_edges`

记录语义因果引用关系。

字段：

- `edge_id`
- `from`
- `to`
- `relation`
- `label`
- `metadata`

典型关系：

- `prompt_to_context`
- `context_to_llm`
- `llm_to_tool`
- `tool_to_observation`
- `failure_to_change`
- `change_to_verification`
- `verification_to_final_response`

### `verification_records`

记录命令型验证，尤其是测试命令。

字段：

- `verification_id`
- `span_id`
- `tool_call_id`
- `command`
- `cwd`
- `purpose`
- `stage`: `baseline`、`post_change`、`exploration`、`unknown`
- `exit_code`
- `status`
- `parsed_failures`
- `stdout`
- `stderr`
- `metadata`

第一版采用轻量启发式解析：从输出中提取 `expected ..., got ...`、文件路径行号、常见 test pass/fail 文本。

### `change_records`

记录文件变更的意图、diff 和验证关联。

字段：

- `change_id`
- `span_id`
- `tool_call_id`
- `files`
- `intent`
- `diff`
- `evidence_refs`
- `verification_refs`
- `metadata`

第一版从 `edit` 工具结果和 patch part 中抽取 diff、文件、增删行数；意图来自工具输入和临近文本/决策摘要。

### `constraint_records`

记录用户约束和可观测遵守情况。

字段：

- `constraint_id`
- `source`
- `constraint`
- `status`: `observed_satisfied`、`observed_violated`、`unknown`
- `evidence_refs`
- `metadata`

第一版从用户 prompt 中识别常见硬约束，例如“不修改文件”、“只读”、“运行测试”、“只改必要文件”。

### `final_response_evidence`

记录最终回答中的关键结论与证据引用。

字段：

- `claim_id`
- `response_artifact`
- `claim`
- `evidence_refs`
- `confidence`
- `metadata`

第一版将最终文本整体保存为 artifact，并把最终回答与最近的工具结果、验证记录、变更记录建立引用边。

## 采集点

### `case-trace.ts`

新增语义 API：

- `contextSnapshot(input)`
- `decision(input)`
- `edge(input)`
- `verification(input)`
- `change(input)`
- `constraint(input)`
- `finalEvidence(input)`

所有 API 都写入 `events.jsonl`，并在 `trace.json` 汇总到对应数组。

### `llm.ts`

在调用 provider 前记录 `context_snapshots`：

- system prompt
- model messages
- tool schema
- tool choice
- provider/model/agent
- message/system/tool 数量

同时建立 `context_to_llm` edge。

### `processor.ts`

记录：

- tool call 决策
- tool result 观察
- reasoning/text 聚合 artifact
- 最终回答 artifact
- 最终回答到验证/变更/工具证据的引用

### `tool/tool.ts`

统一记录工具语义：

- 根据 tool id 和参数分类为 `read`、`search`、`edit`、`verification`、`execution`、`other`
- 对 bash 命令做验证记录
- 对 edit 结果做变更记录

### `session/compaction.ts`

记录压缩语义：

- 压缩触发原因
- tail 选择策略和 token 预算
- previous summary
- selected head/tail
- plugin 注入 context/prompt
- next prompt
- compaction output

### `case-trace-html.ts`

新增 HTML 视图：

- `Evidence Chain`
- `LLM Context`
- `Changes & Verification`
- `Constraints`

这些视图只消费 `trace.json` 和 artifact，不依赖运行时服务。

## 安全与脱敏

新增统一 redaction：

- key 名包含 `key`、`token`、`secret`、`authorization`、`cookie`、`password` 时替换为 `[REDACTED]`
- 字符串匹配 `sk-...`、`Bearer ...` 时替换为 `[REDACTED]`
- artifact 写入前也执行 redaction

## 验收标准

1. 原有 trace 测试继续通过。
2. 新 trace 的 `trace_version` 为 `1.1`。
3. case trace 包含新增语义数组。
4. 大文本只在 artifact 中保存，`trace.json` 只保存摘要和引用。
5. HTML 能展示证据链、LLM 上下文、变更验证和约束。
6. 只读分析 case 能结构化看到“测试失败 -> 代码证据 -> 最终结论”。
7. 修复 case 能结构化看到“失败证据 -> edit diff -> 测试通过”。
8. trace 文件不包含 API key。
