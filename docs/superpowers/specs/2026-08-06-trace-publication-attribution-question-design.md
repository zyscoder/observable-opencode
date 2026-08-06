# Trace 终态回执与问题定向归因接口设计

## 1. 背景与目标

当前 observable-opencode 已能为 case/session 生成语义 Trace、Causal IR 和
离线归因结果，但使用入口仍存在两处断点：

1. opencode 退出后，用户无法稳定获知当前 session 的 Trace 保存位置；
2. 离线归因主要通过底层 `objective` 驱动，缺少面向用户的“缺陷疑问”契约，
   用户不容易明确表达“为什么回答错误”“为什么编译失败”等具体分析目标。

本次改造提供统一的 Trace 终态回执，以及共享同一执行链的 CLI 和 Python API。
改造必须保持 Trace 插装被动、离线归因与 Agent 运行隔离，不改变模型输入、工具
选择、任务循环和代码仓操作。

## 2. 范围

### 2.1 本期包含

- 在 Trace 终态文件完成落盘后，通过 `stderr` 打印当前 session 的保存位置；
- 覆盖正常结束、`beforeExit`、`SIGINT`、`SIGTERM`、`SIGHUP`、未捕获异常和
  未处理 Promise rejection 等可捕获终止路径；
- 为离线归因增加一等公民 `AttributionRequest`；
- 提供 CLI `--question` 和等价的 Python API；
- 将问题身份绑定到归因检查点和输出报告；
- 输出问题定向的结论、根因、因果链、证据引用、被排除假设、置信度和信息缺口；
- 保留 `--objective` 作为高级兼容入口。

### 2.2 本期不包含

- 不提供 HTTP API 或将 Python 归因运行时嵌入 opencode release binary；
- 不把用户问题写回原始 Trace 或 Causal IR；
- 不让归因结果反馈给运行中的 Agent；
- 不承诺在 `SIGKILL` 下打印路径或执行新的落盘动作，因为该信号不可被进程捕获；
- 不引入自动修复、在线干预或归因结论驱动的任务重试。

## 3. 方案选择

采用“统一终态回执 + 一等归因问题”方案：

- Trace 路径由 `ActiveCaseTrace` 的统一终态持久化流程发布，而不是分散在各命令
  的 `finally` 中；
- 用户问题作为只读分析控制信息存在于 `AttributionRequest`，不作为观测事实节点；
- CLI 和 Python API 共用请求规范化、输入绑定、起点选择、递归分析、检查点和报告
  生成代码。

该方案比简单地将 `--question` 映射为 `--objective` 多一层明确契约，能够防止问题
变化后错误复用检查点，也能在报告中区分“用户提出的问题”和“Trace 已观测到的
事实”。相比 HTTP 服务化方案，本期不引入部署、鉴权、路径访问和跨运行时依赖。

## 4. Trace 终态回执

### 4.1 终态顺序

每个可捕获退出路径遵循相同顺序：

1. 停止接收新的 Trace 事件；
2. 写入终态事件并完成 Causal IR 投影；
3. 持久化 `trace.json`、`trace.html`、manifest 等终态产物；
4. 必要时执行现有 emergency/partial snapshot 回退；
5. 检查实际存在的终态或部分产物；
6. 通过 `stderr` 发布一次 Trace 保存回执；
7. 进程继续执行原有退出逻辑。

回执不得先于终态持久化，否则用户看到路径时文件可能尚不存在。

### 4.2 回执模型

内部使用只读结果对象表达可发布信息：

```ts
type TracePublication = {
  sessionID?: string
  caseID: string
  status: "completed" | "failed" | "cancelled" | "partial"
  caseDir: string
  traceFile?: string
  htmlFile?: string
  partialFile?: string
}
```

`ActiveCaseTrace` 持有 `locationReported` 状态。所有终止路径最终调用同一发布函数，
由该函数保证每个 active session “一次且仅一次”。只报告经过文件存在性检查的
路径；完整产物生成失败但部分快照存在时，状态显示为 `partial`。

### 4.3 终端输出

输出到 `stderr`，避免污染 `run --format json` 等标准输出协议：

```text
[observable-opencode] Session trace saved
  session: ses_xxx
  status: completed
  directory: /data/case-traces/case_xxx
  html: /data/case-traces/case_xxx/trace.html
  json: /data/case-traces/case_xxx/trace.json
```

缺失的文件不打印对应行。路径使用绝对路径。回执失败不得覆盖或改变原始退出码。

### 4.4 信号语义

- `SIGINT`、`SIGTERM`、`SIGHUP`：完成现有信号快照/终态落盘后发布回执；
- 正常结束和 `beforeExit`：完成正常终态落盘后发布回执；
- 未捕获异常和未处理 rejection：尽力完成终态或部分落盘后发布回执，再保持原有
  错误退出行为；
- `SIGKILL`：操作系统直接终止进程，无法执行处理器。只能保留信号到达前已经增量
  写入的内容，不能保证打印回执。

## 5. 问题定向归因接口

### 5.1 请求模型

Python 对外暴露稳定请求类型：

```python
@dataclass(frozen=True)
class AttributionRequest:
    trace_path: Path
    question: str
    output_dir: Path
    start_ref: str | None = None
    review_path: Path | None = None
    evaluation_path: Path | None = None
    model: str | None = None
```

问题经过首尾空白清理和换行规范化，但保留用户原文。空问题、纯空白问题和超过
配置上限的问题在分析开始前失败。`question_id` 由规范化问题计算 SHA-256，且与
有效 Trace 输入绑定共同组成分析身份。

### 5.2 CLI

```bash
python -m trace_attribution \
  --trace /data/case-traces/case_xxx/trace.json \
  --question "为什么本次修改编译失败？" \
  --out /data/attribution/case_xxx
```

`--question` 是推荐的用户入口。现有 `--objective` 保留为高级兼容参数；两者同时
出现时立即报错，避免一个请求存在两个互相冲突的分析目标。现有 `--start-ref`、
review、evaluation、模型和预算参数继续生效。

### 5.3 Python API

```python
from trace_attribution import AttributionRequest, analyze

result = analyze(
    AttributionRequest(
        trace_path="/data/case-traces/case_xxx/trace.json",
        question="为什么最终回复不是预期的正确答案？",
        output_dir="/data/attribution/case_xxx",
    )
)
```

CLI 只负责参数解析和错误展示，随后调用同一个 `analyze(request)`。不得复制一套
独立的归因编排逻辑。

## 6. 问题、事实与数据流边界

用户问题是离线分析目标，不是 Trace 事实：

- 原始 Trace 和 Causal IR 以只读方式加载，分析前后内容哈希必须一致；
- 不创建可被当作“已观测缺陷”的问题节点；
- 不允许用户问题提高某个候选节点的事实可信度；
- 问题只影响终态锚点选择、候选相关性排序、节点缺陷判断、污点传播判断和最终
  根因确认；
- 所有结论仍需引用 Trace、review 或 evaluation 中真实存在的 evidence ref；
- 若证据不足，报告必须输出信息缺口，不得用问题文本补全事实。

归因数据流如下：

1. 校验并绑定 `AttributionRequest` 与有效 Trace 输入；
2. 重建语义数据流和因果图；
3. 建立高召回终态锚点池，包括外部预期/实际差异、失败验证、失败工具、case
   终态、最终回答和关键 claim；
4. 在显式 `start_ref` 缺失时，基于问题从锚点池选择起点；
5. 执行 LLM 驱动的递归后向语义污点分析和多假设回溯；
6. 对候选根因执行独立确认；
7. 生成直接回答用户问题的归因报告。

问题相关性只能缩小检索和判断范围，不能删除权威失败事实，也不能突破现有
revision、artifact ownership 和 evidence eligibility 约束。

## 7. 检查点与输出契约

### 7.1 检查点身份

检查点身份至少包含：

- `question_id`；
- 有效 Trace/benchmark bundle 的输入绑定；
- review/evaluation 输入绑定；
- 显式起点；
- 模型与影响语义结果的关键配置。

同一 Trace 使用不同问题分析时必须产生不同身份，禁止静默恢复另一问题的检查点。

### 7.2 报告结构

```json
{
  "analysis_question": {
    "schema_version": "attribution-question/v1",
    "question": "为什么本次修改编译失败？",
    "question_id": "sha256:...",
    "trace_binding": "sha256:...",
    "selected_start_refs": ["..."],
    "analysis_mode": "offline_read_only"
  },
  "conclusion": "...",
  "root_causes": [],
  "causal_chain": [],
  "supporting_evidence_refs": [],
  "rejected_hypotheses": [],
  "confidence": 0.0,
  "unresolved_gaps": []
}
```

最终结论必须直接回应 `question`。根因、因果链和被排除假设都要提供可解析的
evidence ref；置信度不能替代证据。无法回答时使用 `unresolved_gaps` 说明缺少的
具体语义或事实。

## 8. 错误处理

- Trace 不存在、不可读或绑定校验失败：不启动 LLM 分析；
- 问题为空或冲突参数同时出现：返回明确的 CLI/API 输入错误；
- 未找到合法起点：输出结构化不可分析原因，不伪造起点；
- 模型请求失败：保留已完成检查点，输出可恢复状态；
- 报告落盘失败：返回非零状态，不修改原始 Trace；
- Trace 路径回执自身失败：保持原始进程退出状态，不反馈给 Agent。

## 9. 测试与验收

### 9.1 Trace 回执

1. 正常完成后只在 `stderr` 打印一次完整路径；
2. `run --format json` 的 `stdout` 保持原协议；
3. `SIGINT`、`SIGTERM`、`SIGHUP` 路径在落盘后打印一次；
4. 异常退出在存在部分快照时打印 `partial`；
5. 重复触发 finalizer 不重复打印；
6. 输出的每个文件路径在打印时真实存在；
7. 回执格式化失败不改变既有退出码和 Agent 行为。

### 9.2 归因问题接口

1. CLI 与 Python API 对同一请求产生相同的请求身份、起点和报告结构；
2. 不同问题不能复用同一检查点；
3. 问题原文和规范化哈希被稳定保存；
4. 同时传入 `--question` 与 `--objective` 时失败；
5. 成功但回答错误、编译失败、工具失败、外部 expected/actual 不一致等场景均能
   建立合法起点；
6. 报告结论直接回答问题，并引用合法 evidence ref；
7. 证据不足时输出 `unresolved_gaps`；
8. 归因前后原始 Trace 哈希一致；
9. 现有 objective 模式、检查点恢复和递归归因测试保持兼容；
10. Agent 模型输入、工具调用序列和任务结果不因本功能变化。

## 10. 完成标准

- 用户结束 observable-opencode 后能立即看到当前 session 的可访问 Trace 位置；
- 用户能够通过一句自然语言缺陷疑问启动离线归因；
- CLI 与 Python API 使用同一分析实现；
- 问题贯穿起点选择、递归判断和最终确认，同时不污染事实层；
- 所有新增测试和相关回归测试通过；
- 使用文档提供正常退出、信号退出、CLI 和 Python API 的可执行示例。
