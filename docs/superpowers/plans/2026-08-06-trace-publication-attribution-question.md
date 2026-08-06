# Trace 终态回执与问题定向归因接口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 observable-opencode 在可捕获退出路径完成 Trace 落盘后打印 session 产物位置，并让用户通过 CLI 或 Python API 提交自然语言缺陷疑问执行只读离线归因。

**Architecture:** Trace 侧新增独立的终态回执模块，由 `ActiveCaseTrace` 在统一终态持久化之后调用并保证一次性；归因侧新增不可变请求/问题绑定模型和服务入口，将现有 CLI 编排下沉到共享 `analyze()`，CLI 仅负责参数解析。用户问题作为离线 objective 进入起点排序和 LLM 判断，但不会写入 Trace，也不会成为观测事实。

**Tech Stack:** TypeScript、Bun、Python 3、`dataclasses`、`argparse`、`unittest`、现有 Causal IR/recursive attribution/checkpoint 模块。

## Global Constraints

- Trace 回执必须在终态或部分快照落盘后写入 `stderr`，不得污染 `stdout` 协议。
- 每个 active session 最多发布一次回执；回执失败不得改变原始退出码。
- 覆盖正常结束、`beforeExit`、`SIGINT`、`SIGTERM`、`SIGHUP`、未捕获异常和未处理 rejection；不宣称可捕获 `SIGKILL`。
- 用户问题是离线分析控制信息，不得写回 Trace/Causal IR，不得反馈给 Agent，不得提高候选事实可信度。
- CLI 和 Python API 必须共享同一归因编排实现。
- 保留现有 `--objective` 兼容入口；`--question` 和 `--objective` 不允许同时出现。
- 归因前后原始 Trace 字节和内容哈希保持不变。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `packages/opencode/src/observability/trace-publication.ts` | 收集已存在的终态文件、格式化并一次性写入 `stderr`。 |
| `packages/opencode/src/observability/case-trace.ts` | 在正常、异常和信号终态持久化完成后调用回执发布器。 |
| `packages/opencode/test/observability/trace-publication.test.ts` | 纯函数、文件存在性、状态和写入失败测试。 |
| `packages/opencode/test/observability/case-trace.test.ts` | 正常退出及真实信号退出集成测试；其余旧 fixture 显式静默回执。 |
| `tools/trace_attribution/trace_attribution/request.py` | 归因问题规范化、哈希、请求与运行选项数据模型。 |
| `tools/trace_attribution/trace_attribution/service.py` | CLI/Python API 共用的加载、起点选择、Judge、检查点和输出编排。 |
| `tools/trace_attribution/trace_attribution/cli.py` | 参数解析、`AttributionRequest` 构造和用户可读错误。 |
| `tools/trace_attribution/trace_attribution/__init__.py` | 导出 `AttributionRequest`、`AttributionOptions`、`AttributionResult`、`analyze`。 |
| `tools/trace_attribution/tests/test_attribution_request.py` | 问题规范化、身份、只读约束和报告投影测试。 |
| `tools/trace_attribution/tests/test_recursive_cli.py` | CLI 冲突参数、兼容模式和共享服务调用测试。 |
| `tools/trace_attribution/README.md` | Trace 路径回执、CLI/Python API 和信号边界用法。 |

---

### Task 1: Trace 终态回执纯模块

**Files:**
- Create: `packages/opencode/src/observability/trace-publication.ts`
- Create: `packages/opencode/test/observability/trace-publication.test.ts`

**Interfaces:**
- Consumes: session ID、case ID、case 目录、期望状态和候选产物路径。
- Produces: `TracePublication`、`collectTracePublication()`、`formatTracePublication()`、`reportTracePublication()`。

- [ ] **Step 1: 编写失败测试**

测试应覆盖完整产物、仅 partial 产物、绝对路径、缺失文件行省略、`stderr` writer 抛错不外泄：

```ts
test("reports only terminal files that exist", async () => {
  const publication = collectTracePublication({
    sessionID: "ses_test",
    caseID: "case_test",
    status: "completed",
    caseDir,
    traceFile: path.join(caseDir, "trace.json"),
    htmlFile: path.join(caseDir, "trace.html"),
    partialFile: path.join(caseDir, "partial/latest.json"),
  })
  expect(publication?.status).toBe("completed")
  expect(formatTracePublication(publication!)).toContain("session: ses_test")
  expect(formatTracePublication(publication!)).toContain(path.join(caseDir, "trace.json"))
})
```

- [ ] **Step 2: 验证测试先失败**

Run: `bun test packages/opencode/test/observability/trace-publication.test.ts`

Expected: FAIL，提示 `trace-publication` 模块不存在。

- [ ] **Step 3: 实现最小纯模块**

实现以下稳定接口：

```ts
export type TracePublicationStatus = "completed" | "failed" | "cancelled" | "partial"

export type TracePublication = {
  sessionID?: string
  caseID: string
  status: TracePublicationStatus
  caseDir: string
  traceFile?: string
  htmlFile?: string
  partialFile?: string
}

export function collectTracePublication(input: TracePublicationInput): TracePublication | undefined
export function formatTracePublication(input: TracePublication): string
export function reportTracePublication(input: TracePublication, write?: (text: string) => unknown): void
```

`collectTracePublication()` 使用 `fs.existsSync`，将路径规范化为绝对路径；完整 JSON/HTML 不齐但 partial 存在时改为 `partial`。`reportTracePublication()` 默认调用 `process.stderr.write`，并吞掉自身格式化/写入错误。

- [ ] **Step 4: 验证纯模块测试通过**

Run: `bun test packages/opencode/test/observability/trace-publication.test.ts`

Expected: PASS。

- [ ] **Step 5: 提交任务**

```bash
git add packages/opencode/src/observability/trace-publication.ts packages/opencode/test/observability/trace-publication.test.ts
git commit -m "feat: add terminal trace publication receipt"
```

---

### Task 2: 接入 ActiveCaseTrace 全部终态路径

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts:5118-5280`
- Modify: `packages/opencode/src/observability/case-trace.ts:8624-8775`
- Modify: `packages/opencode/test/observability/case-trace.test.ts:1-30`
- Modify: `packages/opencode/test/observability/case-trace.test.ts:4353-4420`
- Modify: `packages/opencode/test/observability/case-trace.test.ts:5164-5310`

**Interfaces:**
- Consumes: Task 1 的 `collectTracePublication()` 和 `reportTracePublication()`。
- Produces: `ActiveCaseTrace.publishTerminalLocation(status)`，对同一 active trace 一次性发布。

- [ ] **Step 1: 编写正常退出与信号退出失败测试**

在旧测试文件顶部为不关注回执的 fixture 设置：

```ts
process.env.OPENCODE_CASE_TRACE_QUIET = "1"
```

新增 fixture 时显式覆盖为 `"0"`，断言：

```ts
expect(stderr.match(/Session trace saved/g)).toHaveLength(1)
expect(stderr).toContain(`session: ses_publication`)
expect(stderr).toContain(path.join(caseDir, "trace.html"))
expect(stderr).toContain(path.join(caseDir, "trace.json"))
expect(await exists(path.join(caseDir, "trace.json"))).toBe(true)
```

分别覆盖显式 `CaseTrace.finish()` 和真实 `proc.kill("SIGTERM")`；在 SIGTERM fixture 中保持退出码 `143`。

- [ ] **Step 2: 验证集成测试先失败**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts -t "trace publication"`

Expected: FAIL，`stderr` 中没有终态回执。

- [ ] **Step 3: 接入统一发布调用**

在 `ActiveCaseTrace` 增加：

```ts
private locationReported = false

private publishTerminalLocation(status: TracePublicationStatus) {
  if (this.locationReported || process.env.OPENCODE_CASE_TRACE_QUIET === "1") return
  const publication = collectTracePublication({
    sessionID: this.sessionID,
    caseID: this.caseID,
    status,
    caseDir: this.caseDir,
    traceFile: this.traceFile,
    htmlFile: this.htmlFile,
    partialFile: this.partialFile,
  })
  if (!publication) return
  this.locationReported = true
  reportTracePublication(publication)
}
```

在 `finish()`、`persistSignalSnapshot()` 和 `persistSignalSnapshotBestEffort()` 的持久化完成点调用。状态映射为：`success -> completed`、`error -> failed`、`cancelled -> cancelled`；仅存在部分快照时由收集器降级为 `partial`。发布调用放在终态写入之后，且不得提前设置 `locationReported`。

- [ ] **Step 4: 验证正常、重复 finish 和三类信号**

Run: `bun test packages/opencode/test/observability/case-trace.test.ts -t "trace publication|receives SIGINT|receives SIGTERM|signal listener"`

Expected: PASS；`stdout` 不变，信号退出码不变，每个 fixture 仅一份回执。

- [ ] **Step 5: 运行 Trace 回归**

Run: `bun test packages/opencode/test/observability/trace-publication.test.ts packages/opencode/test/observability/case-trace.test.ts`

Expected: PASS，现有 stderr 空值断言通过静默环境保持兼容。

- [ ] **Step 6: 提交任务**

```bash
git add packages/opencode/src/observability/case-trace.ts packages/opencode/test/observability/case-trace.test.ts
git commit -m "feat: print trace location after terminal persistence"
```

---

### Task 3: 归因问题与请求契约

**Files:**
- Create: `tools/trace_attribution/trace_attribution/request.py`
- Create: `tools/trace_attribution/tests/test_attribution_request.py`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`

**Interfaces:**
- Consumes: Trace 路径、输出路径、用户问题或兼容 objective、已有运行参数。
- Produces: `AttributionQuestion`、`AttributionOptions`、`AttributionRequest`、`AttributionResult`、`normalize_question()`。

- [ ] **Step 1: 编写请求契约失败测试**

```python
def test_question_normalization_and_identity_are_stable(self):
    left = AttributionQuestion.create("  为什么编译失败？\r\n")
    right = AttributionQuestion.create("为什么编译失败？\n")
    self.assertEqual(left.normalized, "为什么编译失败？")
    self.assertEqual(left.question_id, right.question_id)

def test_question_and_objective_are_mutually_exclusive(self):
    with self.assertRaisesRegex(ValueError, "question.*objective"):
        AttributionRequest(trace_path=trace, output_path=out, question="why", objective="root")
```

同时测试空问题、16,384 字符上限、路径规范化、不可变 dataclass 和默认运行参数。

- [ ] **Step 2: 验证请求测试先失败**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request -v`

Expected: FAIL，`trace_attribution.request` 不存在。

- [ ] **Step 3: 实现请求模型**

```python
QUESTION_SCHEMA_VERSION = "attribution-question/v1"
MAX_QUESTION_CHARS = 16_384

@dataclass(frozen=True)
class AttributionQuestion:
    original: str
    normalized: str
    question_id: str

@dataclass(frozen=True)
class AttributionOptions:
    engine: str = "legacy"
    fusion_mode: str = "retrieval-global"
    analysis_perspective: str = "Find the best-supported causal explanation."
    max_depth: int | None = None
    max_nodes: int = 48
    max_frontier_items: int = 96
    max_hypotheses: int = 24
    max_investigation_rounds: int = 12
    max_artifact_bytes: int = 1_048_576
    max_judge_requests: int = 128

@dataclass(frozen=True)
class AttributionRequest:
    trace_path: Path | None
    output_path: Path
    question: str = ""
    objective: str = ""
    benchmark_bundle_path: Path | None = None
    review_path: Path | None = None
    evaluation_paths: tuple[Path, ...] = ()
    start_refs: tuple[str, ...] = ()
    options: AttributionOptions = field(default_factory=AttributionOptions)
```

请求的 `effective_objective` 在 question 模式下等于规范化问题，在兼容模式下等于显式 objective 或现有默认 objective。问题文本不生成 Trace 节点。

- [ ] **Step 4: 验证请求测试通过并导出 API**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request -v`

Expected: PASS；`from trace_attribution import AttributionRequest` 可用。

- [ ] **Step 5: 提交任务**

```bash
git add tools/trace_attribution/trace_attribution/request.py tools/trace_attribution/trace_attribution/__init__.py tools/trace_attribution/tests/test_attribution_request.py
git commit -m "feat: add question-bound attribution request"
```

---

### Task 4: 共享 Python 服务与 CLI 接入

**Files:**
- Create: `tools/trace_attribution/trace_attribution/service.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py:1-260`
- Modify: `tools/trace_attribution/trace_attribution/__init__.py`
- Modify: `tools/trace_attribution/tests/test_recursive_cli.py`
- Modify: `tools/trace_attribution/tests/test_attribution_request.py`

**Interfaces:**
- Consumes: Task 3 的 `AttributionRequest`。
- Produces: `analyze(request) -> AttributionResult`；CLI `main()` 仅构造请求并打印结果路径。

- [ ] **Step 1: 编写 CLI 和服务失败测试**

覆盖以下行为：

```python
args = parse_args(["--trace", "/tmp/trace.json", "--out", "/tmp/out.json", "--question", "为什么回答错误？"])
self.assertEqual(args.question, "为什么回答错误？")

with self.assertRaises(SystemExit):
    parse_args(["--trace", "/tmp/trace.json", "--out", "/tmp/out.json", "--question", "why", "--objective", "root"])
```

使用 mock Judge/Analyzer 调用 `analyze(request)`，断言 CLI 和 Python API 传入相同的 effective objective、起点、预算和输出路径。

- [ ] **Step 2: 验证服务测试先失败**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request tools.trace_attribution.tests.test_recursive_cli -v`

Expected: FAIL，缺少 `analyze()` 或 `--question`。

- [ ] **Step 3: 从 CLI 提取共享编排**

将当前 `main()` 中从 `load_graph()` 到输出事务提交的逻辑移动到：

```python
def analyze(request: AttributionRequest) -> AttributionResult:
    graph = load_graph(...)
    starts = analysis_start_refs(graph, request.start_refs, question=request.normalized_question)
    report_payload = run_selected_engine(...)
    return AttributionResult(
        output_path=request.output_path,
        lineage_path=lineage_path,
        payload=report_payload,
        question=request.question_binding,
    )
```

服务层保留现有 Judge cache、recursive checkpoint、信号状态和原子输出事务。允许测试通过模块内工厂替换 transport，但公开 API 不要求用户传入测试依赖。

- [ ] **Step 4: 增加问题相关起点排序**

将 `analysis_start_refs()` 扩展为：

```python
def analysis_start_refs(
    graph: TraceGraph,
    explicit_refs: Iterable[str],
    *,
    question: str = "",
) -> tuple[str, ...]:
```

显式起点仍具有最高优先级。没有显式起点时保留 `graph.default_start_refs()` 的完整集合，只基于问题与节点 title/summary/component/event_type 的规范化词元重叠做稳定重排；零相关性时保持旧顺序。不得把不合格 external evaluation 节点提升为合法起点。

- [ ] **Step 5: CLI 构造请求并调用服务**

使用互斥参数组：

```python
target = parser.add_mutually_exclusive_group()
target.add_argument("--question", default="")
target.add_argument("--objective", default="")
```

无二者时由 `AttributionRequest` 使用现有默认 objective。CLI 调用 `analyze(request)`，成功后只向 `stdout` 打印 attribution 输出路径，保持现有脚本兼容。

- [ ] **Step 6: 验证 CLI/Python API 一致性**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request tools.trace_attribution.tests.test_recursive_cli -v`

Expected: PASS。

- [ ] **Step 7: 提交任务**

```bash
git add tools/trace_attribution/trace_attribution/service.py tools/trace_attribution/trace_attribution/cli.py tools/trace_attribution/trace_attribution/__init__.py tools/trace_attribution/tests/test_attribution_request.py tools/trace_attribution/tests/test_recursive_cli.py
git commit -m "feat: expose shared attribution service API"
```

---

### Task 5: 问题绑定、报告投影与只读证明

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/request.py`
- Modify: `tools/trace_attribution/trace_attribution/service.py`
- Modify: `tools/trace_attribution/tests/test_attribution_request.py`
- Modify: `tools/trace_attribution/tests/test_recursive_cli.py`

**Interfaces:**
- Consumes: 原始 report、有效 Trace、问题绑定和 selected start refs。
- Produces: `analysis_question`、`conclusion`、`causal_chain`、`supporting_evidence_refs`、`rejected_hypotheses`、`confidence`、`unresolved_gaps`。

- [ ] **Step 1: 编写问题绑定和只读失败测试**

测试分析前后 Trace SHA-256 相同；不同问题产生不同 `question_id` 和不同 recursive checkpoint config fingerprint；报告必须包含：

```python
self.assertEqual(payload["analysis_question"]["analysis_mode"], "offline_read_only")
self.assertEqual(payload["analysis_question"]["selected_start_refs"], list(starts))
self.assertIn("conclusion", payload)
self.assertIn("supporting_evidence_refs", payload)
self.assertIn("unresolved_gaps", payload)
```

- [ ] **Step 2: 验证报告测试先失败**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request -v`

Expected: FAIL，报告缺少 `analysis_question` 和问题定向投影。

- [ ] **Step 3: 实现问题输出绑定**

`analysis_question` 固定包含：

```python
{
    "schema_version": "attribution-question/v1",
    "question": binding.original,
    "normalized_question": binding.normalized,
    "question_id": binding.question_id,
    "trace_binding": "sha256:" + sha256(stable_json(graph.raw_trace)),
    "selected_start_refs": list(starts),
    "analysis_mode": "offline_read_only",
}
```

现有 checkpoint config 的 `objective` 写入规范化问题，因此不同问题必然改变 config fingerprint；报告额外公开 `question_id`，无需改变当前 checkpoint schema。

- [ ] **Step 4: 实现确定性用户报告投影**

从已经经过独立确认的 roots、taint paths、rejected candidates 和 unresolved refs 生成用户层字段。投影不得新增根因判断：

- `conclusion`：优先使用已确认 root 的 defect/root reason；没有确认根因时明确说明 outcome 和证据不足；
- `causal_chain`：复用已有 `taint_paths`，不推断新边；
- `supporting_evidence_refs`：只收集报告中实际存在且可解析的 evidence refs；
- `rejected_hypotheses`：复用 rejected candidates/hypotheses；
- `confidence`：使用已确认根因的现有 confidence，缺失时为 `0.0`；
- `unresolved_gaps`：由 unresolved refs 和 trace improvement gaps 去重生成。

- [ ] **Step 5: 验证兼容检查点和原始 Trace 不变**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_attribution_request tools.trace_attribution.tests.test_recursive_cli tools.trace_attribution.tests.test_causal_checkpoint -v`

Expected: PASS；旧 objective 模式仍可恢复，问题变化不能恢复旧检查点。

- [ ] **Step 6: 提交任务**

```bash
git add tools/trace_attribution/trace_attribution/request.py tools/trace_attribution/trace_attribution/service.py tools/trace_attribution/tests/test_attribution_request.py tools/trace_attribution/tests/test_recursive_cli.py
git commit -m "feat: bind questions to attribution evidence reports"
```

---

### Task 6: 文档与全量回归

**Files:**
- Modify: `tools/trace_attribution/README.md`
- Modify: `docs/superpowers/specs/2026-08-06-trace-publication-attribution-question-design.md` only if implementation reveals an exact interface correction

**Interfaces:**
- Consumes: 已实现 CLI/Python API 和 Trace 回执格式。
- Produces: 可直接执行的用户手册与最终验证证据。

- [ ] **Step 1: 更新用户用法**

加入 CLI 示例：

```bash
PYTHONPATH=tools/trace_attribution \
python3 -m trace_attribution \
  --engine recursive-agentic \
  --trace /data/case-traces/case_xxx/trace.json \
  --question "为什么本次修改编译失败？" \
  --out /data/attribution/case_xxx.attribution.json
```

加入 Python API、`stderr` 回执、`OPENCODE_CASE_TRACE_QUIET=1`、SIGINT/SIGTERM/SIGHUP 与 SIGKILL 边界说明。

- [ ] **Step 2: 运行 TypeScript 格式与测试**

Run: `bun test packages/opencode/test/observability/trace-publication.test.ts packages/opencode/test/observability/case-trace.test.ts`

Expected: PASS。

Run: `bun run --cwd packages/opencode typecheck`

Expected: PASS。

- [ ] **Step 3: 运行 Python 全量回归**

Run: `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`

Expected: PASS。

- [ ] **Step 4: 执行无网络 smoke test**

使用 mock transport 对同一 fixture 分别通过 CLI 和 Python API 分析，验证输出路径、question ID、selected start refs、根因投影一致，并验证输入 Trace SHA-256 未变化。

- [ ] **Step 5: 检查工作区差异**

Run: `git diff --check`

Expected: 无空白错误。只暂存本计划涉及文件，保留用户和既有 loop 的其他改动。

- [ ] **Step 6: 提交文档与最终验证**

```bash
git add tools/trace_attribution/README.md
git commit -m "docs: document trace receipts and question attribution"
```
