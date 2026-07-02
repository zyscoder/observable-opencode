# Trace Root-Cause Stress Cases Design

## 背景

当前 observable-opencode 的 trace 已经记录了 LLM 请求、上下文转换、工具调用、MCP/skill/subagent、压缩、结果输出、语义事实和 claim 证据链。但这些字段是否足够支撑离线根因归因，不能只靠字段清单判断。更可靠的方法是构造若干“任务效果不佳”的困难 case，每个 case 都有明确真实根因，然后只基于 trace 判断能否定位到该根因。

本规格定义一套 Trace Root-Cause Stress Cases。该模块不做自动归因，也不要求 agent 在运行时诊断自己；它只生成可复现的失败/低质量执行 trace，并提供人工/离线模块可消费的审查表，用来判断 trace 语义记录是否充分。

## 目标

1. 构造一组困难 benchmark case，覆盖代码发现、证据冲突、MCP 使用、上下文压缩、subagent、测试策略、工具失败处理、方案设计质量等失败模式。
2. 每个 case 都预置“真实根因”和“trace 应具备的语义证据”，用于评估 trace 对离线归因是否充分。
3. 所有 case 通过 `opencode serve` 的 HTTP `/session` + `/message` 方式执行，保持与 benchmark case 真实执行路径一致。
4. 评估输出不是诊断结论，而是 trace 充分性审查结果：充分、部分充分、不充分。
5. 根据审查结果反推下一轮 trace 语义增强的优先级。

## 非目标

1. 不在 observable-opencode 运行时内置根因诊断。
2. 不让 trace 模块生成“谁是根因”的结论。
3. 不把大文本全部塞进 `trace.json`；大 payload 仍走 artifact，HTML 需要可跳转查看。
4. 不依赖 `opencode run` 或 command shell 单次运行路径验证。

## 产物

建议新增以下目录：

```text
packages/opencode/test/observability/stress-cases/
  README.md
  stress-case.schema.json
  cases.json
  fixtures/
    wrong-implementation-target/
    conflicting-evidence/
    ignored-mcp-fact/
    compaction-lost-constraint/
    subagent-misleading-summary/
    insufficient-verification/
    tool-failure-hallucination/
    design-quality-regression/
  run-stress-cases.mjs
  analyze-trace-sufficiency.mjs
```

运行输出建议放在临时目录，不进 git：

```text
/tmp/observable-opencode-stress-run/
  bin/opencode-observable-darwin-arm64
  traces/<case-id>/
  reports/<case-id>.trace-review.json
  reports/summary.md
```

## Stress Case Schema

每个 case 在 `cases.json` 中使用统一结构：

```json
{
  "case_id": "wrong-implementation-target",
  "title": "AI edits legacy implementation instead of active implementation",
  "category": "code_discovery",
  "prompt": "请修复 renewalQuote 的折扣上限问题，运行测试验证。",
  "expected_good_result": "src/pricing.mjs 中 active renewalQuote 的 discount cap 从 0.2 改为 0.15，完整测试通过。",
  "designed_failure_mode": "仓库中存在 legacyRenewalQuote 和 active renewalQuote，旧实现更容易被 grep 命中，agent 可能修改 legacy 文件。",
  "ground_truth_root_cause": {
    "component": "code_discovery",
    "failure_type": "wrong_target_selected",
    "description": "Agent 没有区分 active entrypoint 与 legacy implementation，编辑了错误文件。"
  },
  "required_trace_evidence": [
    "search_query_and_results",
    "candidate_files_considered",
    "target_selection_rationale",
    "edited_file_paths",
    "final_test_result"
  ],
  "sufficiency_questions": [
    "trace 是否能看到 agent 搜索了哪些符号和文件？",
    "trace 是否能看到正确实现入口曾经作为候选出现？",
    "trace 是否能解释为什么最终编辑 legacy 文件？",
    "trace 是否能将失败测试或错误输出关联到错误编辑目标？"
  ]
}
```

## Case 1: 错误实现定位

**ID:** `wrong-implementation-target`

**失败模式:** 仓库同时存在：

- `src/pricing.mjs`：真实 active implementation。
- `src/legacy/pricing.mjs`：旧实现，包含相同函数名或类似逻辑。
- `docs/architecture.md`：明确 active entrypoint。

Agent 可能通过 grep 找到 legacy 文件并修改它，导致测试仍失败。

**真实根因:** 代码发现与目标选择失败。

**trace 必须支持的归因证据:**

- 搜索 query、搜索结果数量、候选文件路径。
- 读取了哪些文件，是否读取 `docs/architecture.md`。
- 编辑操作的目标文件。
- 测试失败是否仍指向 active implementation。
- response claim 是否声称修改了正确入口，以及该 claim 的 evidence refs 是否支持。

**若 trace 缺失:** 需要增强 tool search result 结构化记录、candidate selection rationale、edit target 与 source evidence 的关联。

## Case 2: 证据冲突采信错误

**ID:** `conflicting-evidence`

**失败模式:** 三类证据互相冲突：

- `docs/current-requirement.md`：15%。
- `docs/old-design.md`：20%。
- `test/pricing.test.mjs`：期望 15%。

Agent 读到了旧设计文档并按 20% 修复，忽略当前需求与测试。

**真实根因:** 证据优先级与冲突消解失败。

**trace 必须支持的归因证据:**

- 每个证据源抽取出的 subject/predicate/value/source_span。
- 是否存在 conflict matrix 或至少可由离线模块重建冲突组。
- LLM 最终 claim 引用了哪个证据。
- 修改 diff 中实际采用的值。

**若 trace 缺失:** 需要 canonical semantic fact、事实冲突 grouping、claim-to-fact 直接引用收敛。

## Case 3: MCP 事实正确但未被使用

**ID:** `ignored-mcp-fact`

**失败模式:** MCP `syntheticFacts.repo_fact` 返回：

```json
{
  "subject": "renewalQuote",
  "predicate": "discount_cap",
  "value": "15%",
  "path": "docs/current-requirement.md",
  "line_start": 8,
  "line_end": 8
}
```

Agent 调用了 MCP，但最终仍按 20% 修改或回答。

**真实根因:** 工具/MCP 结果没有进入后续决策或 claim attribution。

**trace 必须支持的归因证据:**

- MCP call 输入输出。
- MCP 返回内容是否被抽取为 `evidence.semantic_fact`。
- 后续 LLM request context 是否包含该 fact 或其摘要。
- final response claim/direct evidence 是否引用 MCP fact。
- 修改 diff 是否与 MCP fact 冲突。

**若 trace 缺失:** 需要记录 MCP fact 从工具输出进入 context package 的路径，以及 claim/diff 与 MCP fact 的冲突关系。

## Case 4: 上下文压缩丢失关键约束

**ID:** `compaction-lost-constraint`

**失败模式:** 用户/文档要求“只能改 `billing/` 模块，不得改 `payment/` 模块”。Agent 压缩前读到了约束，但压缩后继续执行时忘记约束，修改了 `payment/`.

**真实根因:** context compaction 信息损失或压缩摘要遗漏约束。

**trace 必须支持的归因证据:**

- 压缩前上下文中的约束事实。
- `context.compaction` 的 before/after refs、summary artifact、retained/dropped fact refs。
- 压缩后 LLM request 里是否还能看到该约束。
- 修改 diff 是否违反约束。

**若 trace 缺失:** 需要 compaction ledger 标准化、retained/dropped semantic facts、LLM message transform layer 可查看。

## Case 5: Subagent 总结错误导致主 Agent 盲信

**ID:** `subagent-misleading-summary`

**失败模式:** 主 agent 委派 subagent 总结大文档。Subagent 因只读了前几行或旧章节，错误总结 owner/cap。主 agent 未复核，直接采用 subagent 结果。

**真实根因:** subagent 输出质量控制失败。

**trace 必须支持的归因证据:**

- subagent prompt。
- subagent 实际读取/搜索的文件范围。
- subagent evidence facts。
- subagent final result。
- 主 agent 接收 subagent 结果后的上下文与 response claim。
- 主 agent 是否复核 primary source。

**若 trace 缺失:** 需要 subagent child trace artifact、child evidence refs、parent consumption refs、subagent result claim matrix。

## Case 6: 测试执行不充分

**ID:** `insufficient-verification`

**失败模式:** 仓库测试分为 `npm test -- owner`、`npm test -- pricing`、`npm test`。Agent 只运行 owner 子集，声称通过，但完整测试失败。

**真实根因:** 验证策略不足。

**trace 必须支持的归因证据:**

- 执行了哪些测试命令。
- package scripts 或测试文件列表。
- 测试范围与用户需求覆盖点的关系。
- final response 中“测试通过” claim 绑定到哪个 verification evidence。

**若 trace 缺失:** 需要 verification command scope、test coverage hints、claim-to-verification attribution。

## Case 7: 工具失败后幻觉继续

**ID:** `tool-failure-hallucination`

**失败模式:** `read` 或 MCP 工具因路径/服务错误失败。Agent 没有修复工具失败，却回答“我已确认文档要求为 15%”。

**真实根因:** 工具错误处理失败与 unsupported claim。

**trace 必须支持的归因证据:**

- 工具调用错误、stderr/exit code。
- 错误是否进入 execution observation。
- 后续 response claim 是否缺少 direct evidence。
- LLM 是否在错误后继续生成结论。

**若 trace 缺失:** 需要 tool failure semantic observation、unsupported claim 标记、错误到后续 claim 的时间链。

## Case 8: 测试通过但方案质量差

**ID:** `design-quality-regression`

**失败模式:** Agent 为了通过测试直接硬编码输入值，或者绕过公共 API，测试通过但违反架构约束。

**真实根因:** 方案设计质量失败，不是工具执行失败。

**trace 必须支持的归因证据:**

- 架构约束/设计约束事实。
- change diff 的语义摘要，例如 hardcode、bypass、新增分支。
- verification 通过的证据。
- final response claim 是否只引用测试通过，而没有引用设计约束。

**若 trace 缺失:** 需要 change semantic extraction、design constraint facts、diff-to-constraint conflict。

## Trace Sufficiency Review

每个 case 执行完成后生成一份 `trace-review.json`：

```json
{
  "case_id": "wrong-implementation-target",
  "ground_truth_root_cause": {
    "component": "code_discovery",
    "failure_type": "wrong_target_selected"
  },
  "trace_sufficiency": "partial",
  "evidence_found": [
    {
      "required": "edited_file_paths",
      "status": "found",
      "record_refs": ["change:chg_1"]
    },
    {
      "required": "target_selection_rationale",
      "status": "missing",
      "record_refs": []
    }
  ],
  "can_offline_module_identify_root_cause": false,
  "missing_semantics": ["candidate_files_considered", "target_selection_rationale"],
  "redundant_or_noisy_semantics": ["broad response output refs include unrelated prompt/context nodes"],
  "recommended_trace_changes": [
    "Record search result candidate set with ranking and selected target",
    "Attach edit target to prior candidate/source evidence"
  ]
}
```

评分规则：

- `sufficient`：无需看 repo 运行现场，只看 `trace.json`/artifact/HTML 即可定位到真实根因，并能列出证据链。
- `partial`：能看出异常发生在哪个大组件，但缺少关键中间语义，无法稳定定位到真实根因。
- `insufficient`：只能看到最终失败或低质量结果，看不出根因来自哪个组件。

## 执行方式

1. 使用 GitHub release binary，不使用本地源码直接运行。
2. 每个 case 用独立临时 repo fixture。
3. 通过 HTTP 方式执行：
   - 启动 `opencode-observable-darwin-arm64 serve --hostname 127.0.0.1 --port 0`
   - `POST /session?directory=<fixture>`
   - `POST /session/<id>/message?directory=<fixture>`
4. 每个 case 设置：
   - `OPENCODE_CASE_TRACE=1`
   - `OPENCODE_CASE_TRACE_DIR=/tmp/observable-opencode-stress-run/traces`
   - `OPENCODE_CASE_ID=<case_id>`
5. 必要 case 可设置：
   - `OPENCODE_TRACE_FORCE_COMPACTION=1`
6. case 结束后关闭 server，等待 `trace.html` 和 `trace.json` finalize。

## 与 v5.3 语义增强的关系

Stress cases 是 v5.3 的输入，不是替代 v5.3。预期流程：

1. 先落地 stress case fixtures、runner、review schema。
2. 用当前最新 release 跑 8 个 case，人工审查 trace 充分性。
3. 统计缺失语义的高频项。
4. 再实施 v5.3 trace 语义增强，优先解决 stress cases 暴露出的归因断点。
5. 再跑 8 个 case，对比 v5.2/v5.3 充分性变化。

目前预期 v5.3 可能包含：

- canonical semantic facts 与 duplicate occurrence folding。
- response output/claim source refs 收敛。
- compaction ledger 统一。
- verification assertion 结构化。
- search candidate set 与 target selection rationale。
- change diff semantic extraction。
- tool failure to unsupported claim linkage。

## 自审

- 无占位符：所有 case 都有明确场景、真实根因和必须 trace 证据。
- 范围聚焦：只负责构造失败/低质量 case 与 trace 充分性审查，不做自动根因诊断。
- 执行路径一致：明确要求 HTTP server/session/message。
- 与当前项目兼容：使用现有 trace 输出、artifact、HTML 和 release binary 流程。
