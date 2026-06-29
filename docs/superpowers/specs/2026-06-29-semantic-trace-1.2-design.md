# Observable Opencode Semantic Trace 1.2 Design

## 背景

`trace_version: "1.1"` 已经补齐了上下文快照、语义决策、验证记录、变更记录、约束记录和最终回答证据链。通过真实 DeepSeek case 复盘后，trace 对“还原流程”已经有帮助，但对根因归因仍存在五类语义精度问题：

1. 脱敏规则过宽，把 `token_usage`、`token_estimate` 等正常计量字段误脱敏。
2. 测试失败解析会优先命中源码模板行，例如 ``expected ${item.expected}, got ${actual}``，导致无法看到真实 expected/actual。
3. 用户约束只记录为 `unknown`，case 结束后没有根据可观测事实收敛。
4. 最终回答证据引用是 `recent_*` 这类泛化占位符，无法跳转到具体验证、变更和上下文。
5. 架构/方案设计类任务没有结构化设计记录，只能看最终回答原文。

## 目标

1. 保持 1.1 schema 兼容，新增字段只做向后兼容扩展。
2. 提升 trace 的语义精确度，让人工归因可以定位到具体证据对象。
3. 继续把大文本存为 artifact，`trace.json` 只保留摘要、hash 和引用。
4. 不做自动诊断和根因结论，只提供更完整的证据材料。
5. 不泄露 API key、Authorization、Cookie、Password、Secret 等敏感值。

## 方案

### 1. 精准脱敏

将 key 级脱敏改为精确敏感字段匹配：

- 保留脱敏：`apiKey`、`api_key`、`authorization`、`cookie`、`secret`、`password`、`credential`、`access_token`、`refresh_token`、`auth_token`。
- 不再脱敏：`token_usage`、`token_estimate`、`tokens`、`totalTokens`、`inputTokens`、`outputTokens` 等计量字段。
- 字符串内容继续按模式脱敏：`sk-...`、`Bearer ...`。

### 2. 测试失败解析修正

`parseVerificationFailures` 优先解析真实错误行：

- 跳过包含 `${...}` 的源码模板行。
- 优先选择以 `Error:` 开头的 `expected ..., got ...`。
- 记录 `message`、`expected`、`actual`、`file`、`line`、`column`。

### 3. 约束结束态评估

在 trace `finish()` 前根据可观测事实更新 `unknown` 约束：

- `Do not modify repository files`：无 `change_records` 则 `observed_satisfied`，否则 `observed_violated`。
- `Run verification tests`：出现 test-like verification command 则 `observed_satisfied`，否则 `observed_violated`。
- `Only make necessary changes`：无变更则 `observed_satisfied`；有变更且能关联失败验证或后置验证则 `observed_satisfied`；否则保持 `unknown`。

### 4. 精确最终证据引用

维护最近上下文、工具 span、验证、变更 ID：

- `context_snapshot:<id>`
- `tool_span:<id>`
- `verification:<id>`
- `change:<id>`

如果调用方未传证据，或传入 `recent_*` 占位符，`finalEvidence()` 自动替换为最近的具体证据 ID。

### 5. 设计记录

新增 `design_records`，用于架构理解和方案设计 case：

- `design_id`
- `span_id`
- `source`
- `requirement_summary`
- `existing_boundaries`
- `design_constraints`
- `candidate_solutions`
- `selected_solution`
- `tradeoffs`
- `risks`
- `test_strategy`
- `evidence_refs`
- `metadata`

第一版从最终回答文本启发式抽取设计记录：将完整最终回答作为 artifact-backed `selected_solution`，并按标题关键词抽取测试策略、风险和取舍片段；证据引用沿用最终回答的具体 evidence refs。

## 验收标准

1. `token_usage`、`token_estimate`、`tokens` 不被 key 级脱敏误伤。
2. `Error: expected 170, got 30` 解析为 `expected=170`、`actual=30`。
3. 只读 case 无变更时，约束结束态为 `observed_satisfied`；出现变更时为 `observed_violated`。
4. 最终回答证据不再出现 `recent_*` 占位符，而是具体对象引用。
5. 设计类回答生成 `design_records`，HTML 有 `Design Records` 区块并能展开 artifact。
6. 本地 typecheck 通过，发布 workflow 通过。
