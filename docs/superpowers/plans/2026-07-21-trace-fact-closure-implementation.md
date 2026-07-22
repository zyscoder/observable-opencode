# Trace Fact Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 response claim、artifact 和外部 Benchmark 评测事实形成可独立复制、可校验、不可反馈给 Agent 的 Causal IR 事实闭包。

**Architecture:** 将 claim 原子化从 `case-trace.ts` 提取为纯确定性模块，使用 `marked@17.0.1` 的 source-preserving block lexer 识别 CommonMark/GFM 结构，再由单遍 claim state machine 处理原始字符范围。Artifact 继续由 CaseTrace 写入 case 目录，同时在 manifest 中保存足以离线判断的有界语义片段和完整性元数据；Python 图加载器优先读取文件，文件缺失时只回退到显式标记的语义片段。外部 grader 事实通过独立 JSON 输入投影为 `external.evaluation_fact`，不进入 Agent 上下文。

**Tech Stack:** TypeScript、Bun test、`marked@17.0.1`、Python 3.12、`unittest`、现有 Causal IR 与 CaseTrace API。

## Global Constraints

- 所有插装保持被动、只写、不可反馈给 Agent。
- 不改变工具调用、模型消息、任务编排、测试执行和 Case 结果。
- 不将纯时间邻近关系升级为可归因因果边。
- Artifact 路径必须保持 case 目录内的相对路径，禁止绝对路径和 `..`。
- External evaluation fact 必须绑定执行 revision，revision 不匹配时不能作为决定性证据。
- 仅允许为 `packages/opencode` 增加 catalog 中固定的 `marked@17.0.1` 运行时依赖，不引入其他运行时依赖。
- 每个任务只提交该任务触及的文件，不混入工作区既有未提交改动。

---

### Task 1: Claim Group 与安全原子化模块

**Files:**
- Create: `packages/opencode/src/observability/claim-atomization.ts`
- Create: `packages/opencode/test/observability/claim-atomization.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts:2460-2760`
- Modify: `packages/opencode/package.json`
- Modify: `bun.lock`

**Interfaces:**
- Consumes: 任意 final response 值，通过 `createClaimSourceView(input, 8000)` 建立完整原文与 Unicode 安全扫描窗口。
- Produces: `atomizeResponseClaims(input: unknown): AtomizedResponseClaim[]`。
- Produces: `AtomizedResponseClaim`，包含 `text`、`raw_text`、`canonical_text`、`claim_format`、`claim_group_id`、`claim_index`、`claim_count`、`source_byte_range`、`previous_claim_key`、`next_claim_key`、`atomization_status`、`atomization_reason`。
- Internal: `ClaimSourceView`，包含完整 `originalText`、不截断 UTF-16 代理对的 `scanEnd` 和基于原文的 UTF-8 byte offset 计算。
- Internal: `normalizeClaimSource(input: unknown): string`，对任意输入不抛异常，双重转换失败时返回固定 `[unserializable response]`。
- Internal: `lexMarkdownBlocks(source: ClaimSourceView): ClaimToken[]`，使用 `marked.Lexer.lex(scanText, { gfm: true })` 并将 `token.raw` 顺序映射回原文。
- Internal: `tokenizeClaimSource(source: ClaimSourceView): ClaimToken[]`，token 类型限定为 `TEXT`、`PROTECTED_TEXT`、`SOFT_BREAK` 和 `HARD_BREAK`。
- Internal: `segmentClaimTokens(source: ClaimSourceView, tokens: ClaimToken[]): ClaimSpan[]`，只在 token 状态机中维护括号与活动 span。

- [ ] **Step 1: 写 `_cstack`、括号、代码片段和 UTF-8 字节范围的失败测试**

```ts
import { describe, expect, test } from "bun:test"
import { atomizeResponseClaims } from "../../src/observability/claim-atomization"

describe("claim atomization", () => {
  test("keeps a parenthetical cstack statement as one auditable claim", () => {
    const response =
      "When `_cstack` handled a non-Model `right` operand (i.e., a pre-computed separability matrix), it now returns the matrix directly."
    const claims = atomizeResponseClaims(response)

    expect(claims).toHaveLength(1)
    expect(claims[0]).toMatchObject({
      text: response,
      raw_text: response,
      claim_index: 1,
      claim_count: 1,
      atomization_status: "atomic",
    })
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength(response)])
    expect(claims[0]!.text).not.toMatch(/^[,，;；)）\]］}｝]/)
  })

  test("uses UTF-8 byte offsets and preserves claim order", () => {
    const response = "修改完成。All 11 tests pass."
    const claims = atomizeResponseClaims(response)

    expect(claims.map((item) => item.text)).toEqual(["修改完成。", "All 11 tests pass."])
    expect(claims[0]!.source_byte_range).toEqual([0, Buffer.byteLength("修改完成。")])
    expect(claims[1]!.source_byte_range[0]).toBe(Buffer.byteLength("修改完成。"))
    expect(claims.every((item) => item.claim_group_id)).toBe(true)
  })
})
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd packages/opencode && bun test test/observability/claim-atomization.test.ts --timeout 30000`

Expected: FAIL，模块 `claim-atomization` 尚不存在。

- [ ] **Step 3: 实现带原始字符范围的原子化结果**

在新模块中定义以下公开类型和入口：

```ts
export type ClaimAtomizationStatus = "atomic" | "group_required" | "invalid_fragment"

export type AtomizedResponseClaim = {
  key: string
  text: string
  raw_text: string
  canonical_text?: string
  claim_format: "factual_claim" | "table_fact"
  table_cells?: string[]
  table_subject?: string
  table_values?: string[]
  claim_group_id: string
  claim_index: number
  claim_count: number
  source_byte_range: [number, number]
  previous_claim_key?: string
  next_claim_key?: string
  atomization_status: ClaimAtomizationStatus
  atomization_reason: string
}

export function atomizeResponseClaims(input: unknown): AtomizedResponseClaim[] {
  const source = createClaimSourceView(input, 8000)
  const tokens = tokenizeClaimSource(source)
  const spans = segmentClaimTokens(source, tokens)
  const candidates = spans.flatMap(toFactualCandidate).slice(0, 50)
  const claimCount = candidates.length
  return candidates.map((candidate, index) => ({
    ...candidate,
    claim_index: index + 1,
    claim_count: claimCount,
    previous_claim_key: candidates[index - 1]?.key,
    next_claim_key: candidates[index + 1]?.key,
  }))
}
```

`createClaimSourceView()` 保留完整原始文本，仅限制 tokenizer 可访问的 `[0, scanEnd)`。
若 code unit 7,999 是 high surrogate 且 8,000 是对应 low surrogate，`scanEnd` 必须
回退一位。所有 byte range 都由 `originalText.slice(0, charOffset)` 计算，禁止对截断后
产生的孤立 surrogate 编码。扫描窗口外不产生 claim。

`normalizeClaimSource()` 依次处理 string、`JSON.stringify()` 和 `String()`；后两步分别
置于独立 `try/catch`。若 JSON 返回 `undefined` 且显式转换也抛异常，必须返回精确文本
`[unserializable response]`。增加同时让 `toJSON`、`toString` 和 `Symbol.toPrimitive`
抛异常的 hostile object 回归，并断言 atomizer 返回数组且不会抛出。

`lexMarkdownBlocks()` 只能调用同步 `marked.Lexer.lex()`，不得渲染 HTML、调用
`marked.use()` 或修改全局 parser 配置。按 lexer 返回顺序从当前 cursor 消费
`token.raw`；若 raw 与 scan window 当前范围不一致、lexer 抛异常或遇到无法处理的
block token，必须把该 token 或剩余 scan window 作为 `HARD_BREAK`，并返回已有事实，
不得向 CaseTrace 抛异常。

block adapter 规则固定为：

- `paragraph`：按原始范围交给现有 inline protected-text tokenizer；
- `heading`（含 ATX/Setext）、`hr`、`code`、`html`、`def`、`blockquote`、`space`：
  整个 raw range 输出 `HARD_BREAK`；
- `list`：每个 item 的起止都输出 `HARD_BREAK`，item 内 paragraph 按原始范围递归；
- `table`：table block 起止输出 `HARD_BREAK`，中间沿用 table fact 适配器并保留原始 range；
- 其他 token：fail-closed 为 `HARD_BREAK`。

`tokenizeClaimSource()` 必须在原始响应上保留位置并生成：普通文本 `TEXT`、不参与
括号/标点扫描的 `PROTECTED_TEXT`、可在括号未闭合时延续的 `SOFT_BREAK`，以及
heading、fence、空段落、list、table、blockquote 对应的 `HARD_BREAK`。状态机遇到
`HARD_BREAK` 必须终止或丢弃不完整 span 并清空括号栈；普通换行只有在括号未闭合
时才延续。不得通过删除 Markdown 行后再扫描。table fact、非事实过滤、continuation
合并和 broken fragment 判断必须发生在 span 形成之后。`source_byte_range` 使用
`Buffer.byteLength(source.originalText.slice(0, charOffset))` 计算，且从原始响应按范围反切片后
必须包含 claim 原始文本。`claim_group_id` 使用完整合并语义声明的稳定 hash，格式为
`claim_group_<hash前12位>`；现有 `claim_index` 与新增 `claim_count` 表示该 response
segment 中的顺序和总数，相邻 ref 只表达 response 内顺序，不表示因果关系。

除原有定向用例外，必须增加结构边界矩阵：普通括号跨行、inline code 未闭合括号、
heading、fence、空段落、list、table、blockquote、嵌套/连续 list。任何 hard barrier
前后的文本不得组成同一 claim，既有 CaseTrace 135 个测试必须全部通过。

增加 CommonMark/GFM block 回归矩阵：ATX heading、Setext `=`/`-` heading、thematic
break、backtick/tilde fence、indented code、HTML block、link definition、blockquote、
ordered/unordered/nested list 和 table。每个用例在 block 前放置未闭合括号，在 block
后放置 `After 12 tests pass.`，并断言后者仍是独立 claim；list/table 中原有事实提取
行为必须保持。另模拟 lexer 异常或 raw mismatch，断言 atomizer fail-closed 且不抛错。

未闭合 single/double/multi-backtick code span 在当前物理行内找不到同长度闭合符时，
从 opening delimiter 到行末整体生成 `PROTECTED_TEXT`。增加参数化回归，证明其中的
未闭合括号不会吞掉后续行事实。另增加恰好跨越 8,000 code unit 的 emoji 回归，并用
`Buffer.from(originalResponse).subarray(start, end).toString()` 验证 byte range 可逆。

```ts
for (const delimiter of ["`", "``", "```"]) {
  const response = `Example ${delimiter}foo(\nAll 11 tests pass.`
  expect(atomizeResponseClaims(response).map((claim) => claim.text)).toContain("All 11 tests pass.")
}

const prefix = "All 11 tests pass: "
const response = `${prefix}${"a".repeat(7_999 - prefix.length)}😀`
const claims = atomizeResponseClaims(response)
expect(claims.length).toBeGreaterThan(0)
for (const claim of claims) {
  const [start, end] = claim.source_byte_range
  expect(Buffer.from(response).subarray(start, end).toString()).toContain(claim.raw_text)
}
```

`ActiveCaseTrace.finish()` 必须以 `try/finally` 或等价单一清理点释放所有暂存的原始
response source。CaseTrace 测试必须覆盖 final、non-final/cancelled 和重复 finish，
并对每条路径断言公开 trace status、已有 summary record、artifact 引用和持久化输出
保持原有语义；私有 source map 的 size 断言只能作为补充。本任务只写 append-only
Trace record；不得把新增 claim 字段写入 Causal IR node。

- [ ] **Step 4: 运行原子化测试**

Run: `cd packages/opencode && bun test test/observability/claim-atomization.test.ts --timeout 30000`

Expected: PASS，至少 22 tests passed，包含 Markdown block、malformed code span、
Unicode 边界和 hostile unknown input 回归。

- [ ] **Step 5: 提交原子化模块**

```bash
git add packages/opencode/src/observability/claim-atomization.ts packages/opencode/test/observability/claim-atomization.test.ts packages/opencode/src/observability/case-trace.ts
git commit -m "feat(trace): preserve response claim groups and byte ranges"
```

### Task 2: 将 Claim Group 事实投影到 CaseTrace 与 Causal IR

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts:575-650,1075-1135,6925-7125,7450-7500`
- Modify: `packages/opencode/src/observability/causal-ir.ts:790-825`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/test/observability/causal-ir.test.ts`

**Interfaces:**
- Consumes: Task 1 的 `AtomizedResponseClaim`。
- Produces: `TraceResponseClaimRecord` 新字段和 `response_to_claim_group`、`claim_group_precedes` 边。
- Produces: 所有顺序边均为 `eligible_for_attribution: false`，只用于恢复完整声明。

- [ ] **Step 1: 写 CaseTrace claim group 投影失败测试**

在 `case-trace.test.ts` 增加一个 final response，内容包含 `_cstack` 示例和第二条独立验证声明，并断言：

```ts
const claims = trace.records.filter((record: any) => record.event_type === "response.claim")
expect(claims).toHaveLength(2)
expect(claims[0].data.claim_group_id).toBeTruthy()
expect(claims[0].data.claim_count).toBe(2)
expect(claims[0].data.source_byte_range).toEqual([0, expect.any(Number)])
expect(claims[0].data.next_claim_ref).toBe(`record:${claims[1].record_id}`)
expect(claims[1].data.previous_claim_ref).toBe(`record:${claims[0].record_id}`)
expect(claims.every((record: any) => record.data.atomization_status === "atomic")).toBe(true)
expect(
  trace.dataflow_edges
    .filter((edge: any) => edge.relation === "claim_group_precedes")
    .every((edge: any) => edge.eligible_for_attribution === false),
).toBe(true)
```

- [ ] **Step 2: 运行定向测试并确认新字段缺失**

Run: `cd packages/opencode && bun test test/observability/case-trace.test.ts -t "claim group" --timeout 30000`

Expected: FAIL，`claim_group_id` 或相邻 claim ref 缺失。

- [ ] **Step 3: 扩展 `TraceResponseClaimRecord` 与 `ResponseClaimInput`**

增加以下字段，并在 `responseClaim()` 的 record、causal node data 和 metadata 中保持相同命名：

```ts
claim_group_id: string
claim_count: number
source_byte_range: [number, number]
previous_claim_ref?: string
next_claim_ref?: string
atomization_status: "atomic" | "group_required" | "invalid_fragment"
atomization_reason: string
```

`ensureFinalResponseClaims()` 先为全部 atomized claims 分配稳定 `claim_id`，再调用 `responseClaim()`，从而在写第一个 claim 时已经知道 `previous_claim_ref` 与 `next_claim_ref`。`claim_group_precedes` 只连接同一个 response segment 内的相邻 claim，metadata 必须包含：

```ts
{
  causal_semantics: "claim_group_order_only",
  eligible_for_attribution: false,
  behavior_impact: "none",
}
```

- [ ] **Step 4: 更新 Causal IR alias 和回放测试**

保持 `response.claim -> response_claim:<claim_id>` 现有 alias，同时验证 journal replay 后 group 字段和不可归因顺序边均保留，不生成新的时间因果边。

- [ ] **Step 5: 运行相关测试**

Run: `cd packages/opencode && bun test test/observability/claim-atomization.test.ts test/observability/case-trace.test.ts test/observability/causal-ir.test.ts -t "claim|journal" --timeout 30000`

Expected: PASS。

- [ ] **Step 6: 提交 Claim Causal IR 投影**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/src/observability/causal-ir.ts packages/opencode/test/observability/case-trace.test.ts packages/opencode/test/observability/causal-ir.test.ts
git commit -m "feat(trace): project claim group provenance into causal IR"
```

### Task 3: Artifact Bundle 完整性与语义片段

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts:170-195,9800-9850,10455-10520`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `tools/trace_attribution/trace_attribution/graph.py:68-90,490-550,761-840`
- Modify: `tools/trace_attribution/tests/test_evidence_capsule.py`
- Create: `tools/trace_attribution/tests/test_artifact_hydration.py`

**Interfaces:**
- Produces: `TraceArtifact.availability`、`content_hash`、`byte_length`、`semantic_slices`。
- Consumes: `semantic_slices: [{byte_range, content, hash, truncated}]`。
- Produces: `hydrate_record_artifacts()` 的 `source` 值 `bundle_file` 或 `embedded_semantic_slice`，以及 hash 校验状态。

- [ ] **Step 1: 写“只复制 trace.json 仍可读取语义片段”的失败测试**

在 Python 测试中构造 artifact manifest，但不创建 artifact 文件：

```python
def test_hydrates_embedded_semantic_slice_when_bundle_file_is_absent(self):
    trace = {
        "case_id": "artifact-copy",
        "artifacts": [{
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "hash": "sha256:full",
            "availability": "bundled",
            "semantic_slices": [{
                "byte_range": [0, 18],
                "content": "pytest: 1 failed",
                "hash": "sha256:slice",
                "truncated": True,
            }],
        }],
        "records": [{
            "record_id": "verification",
            "component": "tool",
            "event_type": "verification",
            "artifact_refs": ["artifact:artifact_1"],
            "data": {"artifact_id": "artifact_1"},
        }],
    }
    graph = TraceGraph.from_trace(trace, artifact_root=self.tempdir)
    hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]
    self.assertEqual(hydrated["source"], "embedded_semantic_slice")
    self.assertEqual(hydrated["content"], "pytest: 1 failed")
    self.assertTrue(hydrated["truncated"])
```

- [ ] **Step 2: 运行测试并确认 hydration 为空**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_artifact_hydration.py -v`

Expected: FAIL，缺失文件时没有 `hydrated_artifacts`。

- [ ] **Step 3: 扩展 TraceArtifact 写入契约**

`writeArtifact()` 写入以下确定性字段：

```ts
availability: "bundled"
content_hash: contentHash
byte_length: Buffer.byteLength(storedContent)
semantic_slices: [{
  byte_range: [0, Buffer.byteLength(storedContent.slice(0, maxFieldLength()))],
  content: storedContent.slice(0, maxFieldLength()),
  hash: hash(storedContent.slice(0, maxFieldLength())),
  truncated: storedContent.length > maxFieldLength(),
}]
```

现有 `hash`、`preview`、`original_length` 和 `stored_length` 保留。语义片段总内容不得超过 `maxFieldLength()`，仍须经过现有 `redactText()`。

- [ ] **Step 4: 实现文件优先、片段回退和 hash 校验**

在 `hydrate_record_artifacts()` 中：

1. 路径合法且文件可读时读取文件，校验 manifest hash，输出 `source="bundle_file"`；
2. 文件缺失但存在合法 `semantic_slices` 时拼接片段，输出 `source="embedded_semantic_slice"`；
3. 文件 hash 不匹配时不使用文件内容，记录 `hash_mismatch`，只允许回退到 hash 可校验的 slice；
4. 片段回退始终保持 `truncated=True`，不得被独立确认误认为完整 artifact；
5. 在 `artifact_hydration` 增加 `slice_fallbacks` 和 `hash_mismatches` 计数。

- [ ] **Step 5: 增加 CaseTrace artifact manifest 测试并运行双语言测试**

Run: `cd packages/opencode && bun test test/observability/case-trace.test.ts -t "artifact" --timeout 30000`

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_evidence_capsule.py -v`

Expected: 两组均 PASS；丢失文件时 capsule 显式报告 slice fallback，而不是把 artifact 标为完全可用。

- [ ] **Step 6: 提交 Artifact Bundle 改造**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/test/observability/case-trace.test.ts tools/trace_attribution/trace_attribution/graph.py tools/trace_attribution/tests/test_artifact_hydration.py tools/trace_attribution/tests/test_evidence_capsule.py
git commit -m "feat(trace): close artifact evidence bundles"
```

### Task 4: External Evaluation Fact 导入

**Files:**
- Create: `tools/trace_attribution/trace_attribution/evaluation_facts.py`
- Create: `tools/trace_attribution/tests/test_evaluation_facts.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py:687-720`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py:71-75`
- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `packages/opencode/src/observability/causal-ir.ts:790-825`

**Interfaces:**
- Consumes: CLI `--evaluation <evaluation.json>`，可重复传入。
- Produces: `inject_external_evaluation_facts(trace, payloads) -> JsonDict`。
- Produces: `external.evaluation_fact` record，默认作为评测失败 seed。

- [ ] **Step 1: 写 revision 校验和默认 seed 的失败测试**

```python
def test_injects_revision_matched_external_failure_as_start_seed(self):
    trace = base_trace(revision="git:abc123")
    payload = {
        "source": "terminalbench",
        "scope": "process_sigint_behavior",
        "subject_revision": "git:abc123",
        "assertion": "cleanup completes",
        "observation": "cleanup was interrupted",
        "status": "failed",
        "observed_at": "2026-07-21T12:00:00Z",
        "evidence_refs": ["record:tool_result"],
        "provenance": {"method": "benchmark_grader", "version": "1.0"},
    }
    graph = TraceGraph.from_trace(inject_external_evaluation_facts(trace, [payload]))
    start = graph.default_start_refs()
    self.assertEqual(len(start), 1)
    self.assertEqual(graph.nodes[start[0]].event_type, "external.evaluation_fact")
    self.assertEqual(graph.nodes[start[0]].data["revision_status"], "matched")
```

另加 revision 不匹配测试，断言 `revision_status="mismatched"`、
`eligible_for_decisive_judgment=False` 且不会成为默认 start。

- [ ] **Step 2: 运行测试并确认导入模块不存在**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py -v`

Expected: FAIL，`evaluation_facts` 模块不存在。

- [ ] **Step 3: 实现严格 schema 与确定性 record ID**

`evaluation_facts.py` 必须校验九个字段：`source`、`scope`、`subject_revision`、
`assertion`、`observation`、`status`、`observed_at`、`evidence_refs`、`provenance`。
只接受 `passed|failed|unknown`。record ID 使用规范化 payload SHA-256 前 16 位：

```python
record_id = "external_evaluation_{0}".format(
    hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]
)
```

record 的 `component` 为 `evaluation`，`event_type` 为
`external.evaluation_fact`；从每个可解析 `evidence_ref` 到该 record 增加
`external_evaluation_observed` 边，`evidence_type="external_grader"`，只有 revision
matched 时 `eligible_for_attribution=True`。

- [ ] **Step 4: 接入 CLI 和默认起点**

增加：

```python
parser.add_argument(
    "--evaluation",
    action="append",
    default=[],
    help="External benchmark evaluation fact JSON; repeatable.",
)
```

`load_graph()` 在 `--review` 注入之后、`TraceGraph.from_trace()` 之前读取并注入
evaluation payload。`external.evaluation_fact` 加入 `EVALUATION_START_EVENTS`；只有
`status="failed"` 且 `eligible_for_decisive_judgment=True` 时进入
`default_start_refs()`。

- [ ] **Step 5: 更新 Trace semantic contract 与 Causal IR alias**

将 `external.evaluation_fact` 加入 formal record types，并为 `evaluation_id` 或
record ID 注册 `external_evaluation` alias。该节点根因资格为 false，只能作为
outcome evidence、反证或 seed。

- [ ] **Step 6: 运行导入、CLI 和 Causal IR 测试**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_recursive_cli.py -v`

Run: `cd packages/opencode && bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts -t "semantic contract|external evaluation" --timeout 30000`

Expected: PASS。

- [ ] **Step 7: 提交 External Evaluation Fact**

```bash
git add tools/trace_attribution/trace_attribution/evaluation_facts.py tools/trace_attribution/trace_attribution/cli.py tools/trace_attribution/trace_attribution/graph.py tools/trace_attribution/trace_attribution/recursive_analyzer.py tools/trace_attribution/tests/test_evaluation_facts.py packages/opencode/src/observability/trace-semantic-contract.ts packages/opencode/src/observability/causal-ir.ts
git commit -m "feat(attribution): ingest revision-bound benchmark facts"
```

### Task 5: 被动观测不变量与阶段一回归

**Files:**
- Modify: `packages/opencode/test/tool/semantic-observability.test.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `tools/trace_attribution/tests/test_recursive_benchmarks.py`
- Create: `docs/superpowers/reports/2026-07-21-trace-fact-closure-results.md`

**Interfaces:**
- Consumes: Tasks 1-4 的 Trace 事实层。
- Produces: 行为等价测试和 Axios、Astropy、TerminalBench 历史 Trace 重建报告。

- [ ] **Step 1: 写 tracing 开关前后行为等价测试**

固定同一组 tool 参数和回调，分别在 tracing disabled/enabled 下执行，断言以下值
完全相同：tool input、tool output、抛出的 error 类型、回调次数、Agent 可见消息。
只允许 trace 目录和 trace 写调用不同。测试中不得读取归因输出后再影响第二次执行。

- [ ] **Step 2: 运行等价测试**

Run: `cd packages/opencode && bun test test/tool/semantic-observability.test.ts -t "passive|behavior" --timeout 30000`

Expected: PASS；若失败，先修复插装副作用，再继续 Benchmark。

- [ ] **Step 3: 运行完整 TypeScript 和 Python 回归**

Run: `cd packages/opencode && bun test test/observability/claim-atomization.test.ts test/observability/causal-ir.test.ts test/observability/case-trace.test.ts test/tool/semantic-observability.test.ts --timeout 30000`

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`

Expected: 全部 PASS，无新增 skipped 或 flaky retry。

- [ ] **Step 4: 只重建三组历史 Trace 并记录事实层变化**

对 Axios、Astropy、TerminalBench 当前冻结输入执行 Causal IR/Graph 重建，不调用
Agent 和归因 LLM。报告必须记录：broken claim fragments、artifact loaded/missing/
slice fallback/hash mismatch、external evaluation seed、节点和边数量。

- [ ] **Step 5: 检查阶段一验收门槛**

必须满足：Astropy `_cstack` 声明不再被拆成逗号开头片段；所有 bundled artifact
要么可读取且 hash 匹配，要么明确降级为 truncated semantic slice；TerminalBench
外部 oracle 成为 revision-matched evaluation seed；被动行为测试通过。

- [ ] **Step 6: 提交阶段一报告**

```bash
git add packages/opencode/test/tool/semantic-observability.test.ts packages/opencode/test/observability/case-trace.test.ts tools/trace_attribution/tests/test_recursive_benchmarks.py docs/superpowers/reports/2026-07-21-trace-fact-closure-results.md
git commit -m "test(trace): verify passive fact closure on benchmarks"
```

## Phase Completion Gate

只有 Task 1-5 全部通过且阶段一报告证明 Agent 行为无损后，才能执行
`2026-07-21-root-confirmation-stability-implementation.md`。事实闭包失败时不得通过
扩大 LLM 上下文掩盖问题。
