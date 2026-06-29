# Trace Semantic Contract v4 Design

## 背景

Trace Provenance v3 已经能够把 LLM、tool、MCP、skill、subagent、context pack、context compaction 和 response output 记录为结构化 trace，并通过 HTML 展示组件数据流。但最新验证暴露出两个问题：

1. 正式 trace 入口存在概念分裂：`trace.html` 是早期兼容视图，`viewer.html` 是 v3 语义视图，用户不知道应以哪个为准。
2. 部分低层事件被提升为正式节点，例如 `tool-input-delta` 的流式参数碎片、`part_count`/`part_types` 等 prompt 元数据。这些信息对调试插装有用，但对离线根因归因没有独立语义，会稀释有效流程。

本次改造采用方案 B：统一正式 trace 入口，并建立逐节点语义准入契约。目标不是让 trace 自己做归因，而是让 trace 成为离线归因模块可信、稳定、低噪声的事实输入。

## 目标

- 统一正式可视化入口为 `trace.html`。
- 统一正式结构化数据入口为 `provenance-trace.json`。
- 对每一类节点和插装点进行语义审查，明确其处理策略：保留、补齐、聚合、降级到 raw、删除。
- 删除没有实际归因价值的正式节点，避免 timeline 和 dataflow 被无意义事件污染。
- 对有归因价值但语义不足的节点补齐字段，例如来源位置、上下文保留/丢弃、响应可见性、子任务链路。
- 保留必要的低层调试能力，但与正式语义层隔离。

## 非目标

- 不在 trace 生成阶段做根因判断、诊断建议或责任归因。
- 不要求在正式 JSON 中内嵌所有大文本；大文本继续以 artifact 方式保存。
- 不兼容维护两个长期并行的 HTML 入口。
- 不把 provider token delta、tool input delta 等流式碎片作为正式语义节点。

## 产物设计

### 正式产物

- `provenance-trace.json`：正式语义事实文件，版本升为 `4.0`。
- `trace.html`：正式离线可视化页面，渲染 `provenance-trace.json`。
- `artifacts/`：大文本、完整输入输出、上下文包、压缩摘要等 artifact。
- `manifest.json`：case 级索引，指向正式产物和可选调试产物。
- `partial/latest.json`：运行中或异常退出时的最新正式快照。

### 调试产物

- `raw-events.jsonl`：低层事件流，仅用于 trace 系统自身调试。
- `records.jsonl`：正式 record 的增量落盘，可保留用于流式消费。
- `trace-debug.json`：如需要保留旧 `trace.json` 的 span/event 全量结构，应重命名或在 manifest 中标记为 debug。

### 废弃和合并

- `viewer.html` 不再作为正式产物。
- 过渡期可选择生成一个极薄 alias 文件，内容提示“请打开 trace.html”；最终应删除该产物。
- 旧 `trace.html` 中的低层 span/event 视图迁移到新 `trace.html` 的 Raw Debug 区域，仅在 debug profile 中展示。

## Semantic Event Contract

每个插装点必须归入四类之一：

- `promote`：提升为正式 `records[]` 节点，可进入 timeline/dataflow。
- `aggregate`：不单独成节点，聚合到父级 record 的字段中。
- `raw_only`：只写入 `raw-events.jsonl`，默认不进正式 JSON 和 HTML。
- `drop`：停止插装或完全不落盘。

任何新节点类型必须先加入本契约和测试白名单，否则不能进入 `provenance-trace.json`。

## 节点语义审查表

| 当前节点或事件 | 处理策略 | v4 语义要求 |
| --- | --- | --- |
| `run.start` | promote | case 输入、cwd、argv、配置摘要、trace profile、启动时间。 |
| `task.loop` | promote | 只记录 agent 主循环/子循环边界、轮次、触发原因、终止原因；不得承载零散 prompt 元数据。 |
| `llm.call` | promote | provider/model、输入上下文引用、tools schema 引用、token、耗时、finish reason、错误、关联 context pack。 |
| `context.pack` | promote | message/tool/system 计数、token 估算、artifact refs、selected message IDs、source fact refs；不直接塞整包大文本。 |
| `context.compaction` | promote | trigger、算法、压缩前后 token、保留/丢弃 message IDs、保留/丢弃 fact refs、summary artifact、auto-continue 信息。 |
| `tool.call` | promote | tool 名称、参数摘要、参数 artifact、结果摘要、退出码、耗时、是否修改文件、source locations。 |
| `mcp.call` | promote | server/tool、参数、返回资源/事实列表、resource URIs、错误、耗时；raw output 进入 artifact。 |
| `skill.load` | promote | skill 名称、来源路径、版本/哈希、加载文件列表、暴露能力摘要；skill 正文进入 artifact。 |
| `subagent.call` | promote | parent record、child session ID、child trace ref、agent 类型、输入任务、输出摘要、token、耗时、状态。 |
| `observation` | promote 或 aggregate | 从工具/MCP/skill/文件读取/验证中抽取出的独立事实才 promote；纯包装输出必须 aggregate 到来源 record。 |
| `change` | promote | 文件列表、diff artifact、变更意图、关联 tool、前置失败验证、后置验证。 |
| `verification` | promote | 命令、cwd、阶段、退出码、测试框架、失败用例、assertion、stdout/stderr artifact。 |
| `response.output` | promote | response segment、visibility、turn index、is_final_for_case、source refs、artifact ref。 |
| `runtime.event` | remove from formal records | 不再作为正式 record 类型；具体 runtime 事件按本表分类。 |
| `prompt.parts.resolved` | aggregate | 聚合进 `user.request` 或 `context.pack.input_refs`，只保留 part 类型、文件引用、用户文本 artifact。 |
| `user.message.created` | aggregate | 聚合进 `user.request`，不单独成 timeline 节点。 |
| `processor.tool.call` | aggregate | 聚合进对应 `tool.call` 的 `model_requested_tool` 字段。 |
| `tool-input-delta` | raw_only | semantic/minimal profile 不输出；full profile 仅写 raw，用于 streaming parser 调试。最终工具参数以完整 JSON 记录在 `tool.call.input`。 |
| token/text delta | raw_only | semantic/minimal profile 不输出；full profile 仅写 raw。最终文本进入 `llm.call.output` 或 `response.output`。 |
| 空 `tool_overrides`、纯 `part_count`、纯 `part_types` | aggregate 或 drop | 有助于解释上下文构造时 aggregate 为父 record metrics；为空或无解释价值时 drop；不得单独成节点。 |
| MCP server connect/list tools | aggregate | 聚合到首次 `mcp.call` 或 `run.start.capabilities`，除非连接失败。连接失败可 promote 为 `mcp.call` error。 |
| permission/request/approval runtime event | promote 或 raw_only | 影响工具是否执行、是否跳过、是否失败的权限事件 promote；普通状态刷新 raw_only。 |

## 关系语义审查表

`dataflow_edges[].relation` 必须使用严格枚举：

- `selected_into_context`
- `prompted`
- `produced`
- `consumed`
- `compressed_from`
- `compressed_to`
- `spawned`
- `continued_from`
- `derived_from`
- `verified_by`
- `modified_by`
- `failed_before`
- `read_from`
- `returned_by`

旧关系必须迁移：

- `tool_to_change` -> `modified_by`
- `tool_to_observation` -> `produced`
- `failure_to_change` -> `failed_before`
- `change_to_verification` -> `verified_by`
- `context_to_llm` -> `prompted`
- `compaction_to_context` -> `compressed_to`
- `source_to_response` -> `consumed`
- `source_to_observation` -> `derived_from`
- `source_to_compaction` -> `compressed_from`
- `compaction_to_observation` -> `derived_from`

测试中应校验未知 relation 直接失败。

## 关键字段补齐

### `source_locations[]`

用于工具读取、grep、MCP 返回事实、skill 文件、verification failure、response source ref：

```json
{
  "uri": "file:///repo/src/pricing.mjs",
  "path": "src/pricing.mjs",
  "line_start": 10,
  "line_end": 14,
  "content_hash": "sha256:...",
  "snippet_preview": "..."
}
```

### `context_ledger`

用于 `context.pack` 和 `context.compaction`：

```json
{
  "token_estimate_before": 42180,
  "token_estimate_after": 11820,
  "retained_message_ids": [],
  "dropped_message_ids": [],
  "retained_fact_refs": [],
  "dropped_fact_refs": [],
  "summary_artifact_id": "artifact_...",
  "algorithm": "head-tail-summary"
}
```

### `response_segments[]`

```json
{
  "segment_id": "resp_1",
  "visibility": "user_visible",
  "turn_index": 4,
  "is_final_for_case": true,
  "text": {"type": "text", "artifact_id": "artifact_..."},
  "source_refs": []
}
```

`visibility` 允许值：

- `user_visible`
- `internal_continue`
- `compaction_followup`
- `debug`

### `subagent.trace_ref`

```json
{
  "parent_record_id": "node_...",
  "child_session_id": "ses_...",
  "child_trace_dir": "subtraces/ses_...",
  "child_status": "success"
}
```

## `trace.html` 设计

新 `trace.html` 是唯一正式视觉入口，包含：

1. Overview：case、状态、耗时、token、records、artifacts、trace profile。
2. Component Dataflow：只展示正式 semantic records 和严格枚举 edge。
3. Execution Timeline：默认不展示 raw events，不展示 delta 和纯元数据节点。
4. IO Inspector：LLM/tool/MCP/skill/subagent 的输入输出，左右滚动，artifact 可打开。
5. Context Ledger：上下文构造和压缩前后的保留/丢弃信息。
6. Response Segments：区分最终回答、内部续写、压缩后续写。
7. Artifacts：大文本索引。
8. Raw Debug：仅 debug profile 下展示原始事件入口。

## Trace Profiles

- `semantic`：默认 profile，只输出正式语义层和必要 artifact。
- `full`：额外输出 raw events、debug trace、旧兼容信息。
- `minimal`：只输出 manifest、provenance-trace 和必要 artifacts。

benchmark 默认使用 `semantic`。

## 实施策略

1. 新增 `SemanticEventContract`，集中定义 record 类型、relation 枚举、事件分类和字段要求。
2. 修改 `CaseTrace.event()`：不再把所有 `runtime`/`prompt` 事件自动提升为 `runtime.event`。
3. 为每个现有 `CaseTrace.event()` 调用点逐项审查并迁移：
   - 有语义价值：调用更明确的 record API。
   - 只有父级意义：聚合到父 record。
   - 仅调试价值：raw_only。
   - 无价值：删除插装点。
4. 修改 `provenanceSummary()` 输出 v4 schema，并强制 relation/type 白名单。
5. 将 `viewer.html` 渲染器并入 `trace.html` 渲染器。
6. 更新文档、测试和 release 使用说明。

## 验证标准

- 每个 case 只需要打开 `trace.html` 即可查看正式可视化流程。
- `viewer.html` 不再作为正式入口；如存在也只能是 alias。
- `provenance-trace.json.trace_version` 为 `"4.0"`。
- `records[].event_type` 不包含 `runtime.event`。
- `records[]` 不包含 `tool-input-delta`、token delta、空 `tool_overrides`、纯 `part_count` 节点。
- 每个 `records[].event_type` 均在 `SemanticEventContract` 白名单内。
- 每个 `dataflow_edges[].relation` 均在严格枚举内，不再出现 `tool_to_change`。
- context compaction case 可看到 retained/dropped ledger。
- subagent case 可看到 parent-child trace linkage。
- MCP/skill case 可看到 typed facts/resources 或明确的 artifact ref。
- release binary 跑 synthetic benchmark 时，trace 可用于离线消费且没有密钥泄漏。

## 风险与取舍

- 删除/降级节点会减少调试视图中的细节，因此需要通过 `full` profile 保留必要原始事件。
- v4 schema 会与 v3 有差异，下游消费者需要按版本解析。
- 逐插装点审查会增加实施成本，但这是降低 trace 噪声和提升离线归因价值的必要成本。
