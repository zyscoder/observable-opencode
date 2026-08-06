# Causal Judgment Context 设计

## 目标

在不改变 Agent 行为、Trace 采集行为和 Causal IR 事实内容的前提下，为离线归因 Judge 构造结构化因果上下文，使模型能够区分节点自身缺陷、普通证据依赖和真实缺陷传播。

## 当前问题

现有 Judge 输入包含当前节点、最多 12 个上游节点、artifact 大文本和后向缺陷路径，但存在四类投影损失：

1. 构图只保留节点邻接关系，丢失 `relation`、`evidence_type`、`confidence` 和 `inference_method` 等边语义。
2. active defect 以自由文本路径表达，缺少稳定的 expected/actual/mechanism/scope 指纹。
3. 当前节点看不到路径下游已经完成的缺陷判断，导致缺陷定义在后向遍历中漂移。
4. 具体 decision/action 缺少所属 Causal Episode 和 Progress Episode 的局部进展信息。

## 方案选择

### 方案 A：扩大原始 Trace 上下文

直接提高节点数和字符预算。改动小，但会引入大量生命周期和 Provider 噪声，无法解决边关系丢失，不采用。

### 方案 B：结构化 Causal Judgment Context

在离线图中保存标准化边事实，按当前节点动态投影缺陷指纹、入边、活跃路径出边、下游判断和 Episode 上下文。信息密度高，保持一次 Judge 调用，作为本轮方案。

### 方案 C：节点 Judge 与边 Judge 完全拆分

分别调用 LLM 判断节点缺陷和逐边污点传播。理论边界最清晰，但调用量和不稳定面显著增加。待方案 B 的 benchmark 结果证明仍有必要时实施。

## 架构

新增纯离线 `judgment_context.py`：

- `build_active_defect_fingerprint`：从路径起点、objective 和评估字段生成稳定缺陷身份。
- `build_causal_judgment_context`：组合当前节点、图边、路径下游判断、Causal Episode、Progress Episode 和压缩清单。
- `compact_judgment_context`：按固定预算投影给 Judge，不修改原始 Trace。

`TraceGraph` 增加标准化边索引。边来源包括 record `source_refs`、Trace `dataflow_edges`、重建的 message lineage 和离线 progress links。每条边保留来源、关系、证据类型、置信度、可归因性与推断方法。

`BackwardTaintAnalyzer` 在每个节点判断前构造上下文。支持 `judge_node_with_context` 的 Judge 使用新接口，旧 Fake/第三方 Judge 继续走原接口，保证兼容。

`ClaudeJudgeClient` 把结构化上下文加入现有提示词。根因二次确认保持 node-local，不注入上游上下文，避免连坐式根因确认。

## 上下文结构

```json
{
  "active_defect": {
    "observed_ref": "record:...",
    "expected": "...",
    "actual": "...",
    "mechanism": "...",
    "scope": "...",
    "temporal_anchor": "..."
  },
  "incoming_edges": [],
  "outgoing_edge_on_active_path": [],
  "downstream_judgments": [],
  "causal_episode": {},
  "progress_episode": {},
  "context_manifest": {}
}
```

## 约束

- 被动记录、事后重建、不可反馈给 Agent。
- 不改变 Agent、LLM、工具、MCP、Skill 或上下文管理的执行路径。
- 不把不可归因边作为缺陷传播候选。
- 缺失事实必须显式标记，不允许 Judge 推测。
- 保留现有 Judge API 的兼容路径。
- 根因确认继续只使用当前节点自身语义和 active defect。

## 验收标准

1. Judge 能看到每个候选上游与当前节点之间的边关系及证据来源。
2. 后向路径上所有已判定节点以结构化摘要提供，并共享同一个 active defect 指纹。
3. decision/action 判断能看到所属 Causal Episode 和匹配的 Progress Episode 摘要。
4. 旧 Judge 测试无需改签名仍能运行。
5. 全部离线归因单测通过；Terminal、Pydantic、Sphinx 既有样例不出现新的错误根因。
