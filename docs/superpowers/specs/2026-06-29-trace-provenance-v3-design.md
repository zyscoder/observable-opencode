# Trace Provenance v3 Design

## 目标

Trace Provenance v3 将 observable-opencode 的 case trace 收敛为事实记录层。trace 只记录组件输入、输出、上下文、artifact 和数据流，不做根因判断、诊断提示、责任归因或 claim 正确性评价。离线归因分析作为独立模块消费 trace。

## 非目标

- 不在 trace 运行时判断哪个组件是根因。
- 不输出 `diagnostics_hints`、`root_cause`、`blame`、`likely_reason`、`confidence` 等诊断字段。
- 不把 compaction summary 伪装成最终回答 claim。

## 输出文件

每个 case 主输出为：

- `manifest.json`：case 元信息和文件索引，`trace_version` 为 `3.0`。
- `provenance-trace.json`：v3 主文件，包含 `records`、`dataflow_edges`、`artifacts` 和 `metrics`。
- `records.jsonl`：运行中 write-ahead log。
- `raw-events.jsonl`：低层事件流，用于 trace 系统调试。
- `partial/latest.json`：运行中或中断时的最新 v3 快照。
- `viewer.html`：离线 HTML 查看器。
- `artifacts/sha256/`：大文本和大对象按内容 hash 去重保存。

`trace.json` 和 `trace.html` 保留为低层调试/兼容副产物，但正式分析链路应优先消费 `provenance-trace.json`。

## Schema

`provenance-trace.json` 顶层结构：

```json
{
  "trace_version": "3.0",
  "manifest": {},
  "records": [],
  "dataflow_edges": [],
  "artifacts": [],
  "metrics": {}
}
```

`records[]` 表示可观测事实：

- `record_id`
- `component`
- `event_type`
- `span_id`
- `timestamp`
- `time_ms`
- `status`
- `duration_ms`
- `token_usage`
- `data`
- `source_refs`
- `artifact_refs`
- `metadata`

`dataflow_edges[]` 表示组件间数据流，只允许表达流转，不表达归因：

- `produced`
- `consumed`
- `prompted`
- `returned`
- `selected_into_context`
- `compressed_from`
- `compressed_to`
- `spawned`
- `continued_from`
- `wrote_artifact`

## 组件语义

- LLM：记录模型、provider、agent、上下文包、token usage、输出完成状态。
- Tool：记录工具名、参数、stdout/stderr、exit code、读写结果和 diff。
- MCP：记录 server/tool、入参、返回 content、错误。
- Skill：记录 skill 名称、路径、加载内容和加载状态。
- Subagent：记录父 session、子 session、任务 prompt、模型和子任务输出。
- Context：记录进入 LLM 前的 system/messages/tools，compaction 前后摘要和保留/隐藏消息数量。
- Runtime loop：记录 turn、auto-continue、compaction 触发、cancel/signal/finalize。

## Viewer

`viewer.html` 包含：

- `Component Dataflow`
- `Execution Timeline`
- `IO Inspector`
- `Context Ledger`
- `Artifacts`

页面不使用 `Evidence Inspector`、`Causal Graph`、`Final Claim` 等容易暗示在线归因的词。

## 验收标准

- `provenance-trace.json` 不包含 `diagnostics_hints`、`final.claim`、`final_response_evidence`、`evidence_refs`。
- `viewer.html` 不出现 `Evidence Inspector`。
- LLM、tool、MCP、skill、subagent、context compaction 和 SIGINT finalization 均能生成 v3 bundle。
- 大文本继续进入 artifact，并保持密钥脱敏。
