# Observable Opencode Benchmark Trace 使用说明

本文说明如何安装改造后的 observable opencode，如何替换现有 `opencode` 命令，以及如何在 benchmark case 执行时生成结构化 trace 和可视化报告。

## 1. 获取代码

```bash
git clone https://github.com/zyscoder/observable-opencode.git
cd observable-opencode
```

推荐固定到已验证分支：

```bash
git checkout codex/observable-opencode-trace
```

## 2. 企业内网推荐：直接使用 GitHub Release 二进制

企业内部 Linux 或 macOS 环境如果无法稳定访问 npm、GitHub dependency、Bun registry，不需要在目标机器上执行 `bun install`。推荐在 GitHub Actions 外网环境构建 Release，然后在目标机器只下载一个可执行文件。

Release workflow 位于：

```text
.github/workflows/release-observable-linux.yml
```

### 2.1 在 GitHub 上触发发布

方式一：推送 tag 自动发布。

```bash
git tag v1.14.48-observable.1
git push origin v1.14.48-observable.1
```

方式二：在 GitHub 页面手动触发。

```text
Actions -> release observable -> Run workflow
```

手动触发时填写 tag，例如：

```text
v1.14.48-observable.1
```

workflow 会发布这些资产：

```text
opencode-observable-linux-x64
opencode-observable-linux-x64-baseline
opencode-observable-linux-x64-musl
opencode-observable-linux-x64-baseline-musl
opencode-observable-linux-arm64
opencode-observable-linux-arm64-musl
opencode-observable-darwin-arm64
opencode-observable-darwin-x64
opencode-observable-darwin-x64-baseline
SHA256SUMS
```

大多数企业 x86_64 glibc Linux 机器使用：

```text
opencode-observable-linux-x64
```

老旧 x86_64 CPU 如果不支持 AVX2，使用：

```text
opencode-observable-linux-x64-baseline
```

Alpine Linux 或 musl 环境使用 `*-musl` 资产。

Apple Silicon macOS 使用：

```text
opencode-observable-darwin-arm64
```

Intel macOS 使用：

```text
opencode-observable-darwin-x64
```

### 2.2 在企业 Linux 机器安装 Release 二进制

```bash
export TAG="v1.14.48-observable.1"
export ASSET="opencode-observable-linux-x64"

curl -L \
  -o /tmp/opencode \
  "https://github.com/zyscoder/observable-opencode/releases/download/${TAG}/${ASSET}"

curl -L \
  -o /tmp/SHA256SUMS \
  "https://github.com/zyscoder/observable-opencode/releases/download/${TAG}/SHA256SUMS"

cd /tmp
grep " ${ASSET}$" SHA256SUMS | sed "s#${ASSET}#opencode#" | sha256sum -c -

chmod +x /tmp/opencode
sudo install -m 0755 /tmp/opencode /usr/local/bin/opencode
opencode --version
```

如果内网机器不能直接访问 GitHub Release，可以先在有外网的机器下载 `opencode-observable-linux-x64` 和 `SHA256SUMS`，再通过企业内部制品库、堡垒机或离线介质分发到目标机器。

### 2.3 在 macOS 机器安装 Release 二进制

```bash
export TAG="v1.14.48-observable.1"
export ASSET="opencode-observable-darwin-arm64"

curl -L \
  -o /tmp/opencode \
  "https://github.com/zyscoder/observable-opencode/releases/download/${TAG}/${ASSET}"

curl -L \
  -o /tmp/SHA256SUMS \
  "https://github.com/zyscoder/observable-opencode/releases/download/${TAG}/SHA256SUMS"

cd /tmp
grep " ${ASSET}$" SHA256SUMS | sed "s#${ASSET}#opencode#" | shasum -a 256 -c -

chmod +x /tmp/opencode
sudo install -m 0755 /tmp/opencode /usr/local/bin/opencode
opencode --version
```

## 3. 源码方式：仅用于开发或外网构建机

Linux 机器需要先安装 Node.js、Git 和 Bun。Bun 版本建议使用仓库声明的 `1.3.13`。

```bash
curl -fsSL https://bun.sh/install | bash
export PATH="$HOME/.bun/bin:$PATH"
bun --version
```

安装仓库依赖：

```bash
bun install
```

验证 TypeScript：

```bash
bun run --cwd packages/opencode typecheck
```

## 4. 运行 observable opencode

### 方式 A：源码方式运行，推荐用于 benchmark

源码方式不需要构建平台二进制，适合快速替换 benchmark runner 中的 `opencode` 命令。

```bash
cd /path/to/observable-opencode/packages/opencode
bun run --conditions=browser ./src/index.ts --version
```

创建一个 wrapper：

```bash
cat > /tmp/opencode-observable <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd /path/to/observable-opencode/packages/opencode
exec bun run --conditions=browser ./src/index.ts "$@"
EOF
chmod +x /tmp/opencode-observable
sudo install -m 0755 /tmp/opencode-observable /usr/local/bin/opencode-observable
```

验证：

```bash
opencode-observable --version
opencode-observable run "hello"
```

### 方式 B：构建 Linux 二进制

如果希望用二进制替换现有 `opencode`，可以在目标 Linux 机器上构建当前平台二进制。

```bash
bun run --cwd packages/opencode build --single --skip-embed-web-ui
```

构建产物通常位于：

```text
packages/opencode/dist/opencode-linux-x64/bin/opencode
packages/opencode/dist/opencode-linux-arm64/bin/opencode
```

按机器架构选择对应文件：

```bash
./packages/opencode/dist/opencode-linux-x64/bin/opencode --version
```

## 5. 替换现有 opencode

先确认当前 `opencode` 位置：

```bash
command -v opencode
opencode --version
```

备份原命令：

```bash
sudo mv "$(command -v opencode)" /usr/local/bin/opencode.bak.$(date +%Y%m%d%H%M%S)
```

### 使用源码 wrapper 替换

```bash
sudo ln -sf /usr/local/bin/opencode-observable /usr/local/bin/opencode
opencode --version
```

### 使用二进制替换

```bash
sudo install -m 0755 packages/opencode/dist/opencode-linux-x64/bin/opencode /usr/local/bin/opencode
opencode --version
```

如果线上环境不允许替换全局命令，推荐只在 benchmark runner 中把 `opencode` 命令路径改成 `/usr/local/bin/opencode-observable`。

## 6. 配置 trace 环境变量

开启 case trace：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID="T1-001"
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/opencode-traces"
```

可选配置：

```bash
# 单字段 preview 最大长度，默认 2048。
export OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH=4096
```

每个 case 会生成：

```text
$OPENCODE_CASE_TRACE_DIR/
  T1-001/
    manifest.json
    trace.json
    legacy-trace.json
    provenance-trace.json
    events.jsonl
    records.jsonl
    raw-events.jsonl
    trace.html
    partial/
      latest.json
    artifacts/
      sha256/
        <hash>_<label>.json
        <hash>_<label>.txt
```

其中：

- `manifest.json`：case 入口文件，记录 case id、run id、状态、时间、模型/环境、结果和各 trace 文件路径。
- `trace.json`：Trace Semantic Contract v5.2 主文件，包含事实记录、回答 claim、组件数据流、artifact 索引、token/耗时和 trace health 指标。该文件只记录可观测事实，不输出根因判断或诊断提示。
- `legacy-trace.json`：旧版 v1.3 调试摘要，保留 `spans`、`events`、`context_snapshots`、`verification_records`、`change_records`、`constraint_records`、`response_segments`、`design_records` 和旧 `dataflow_edges`。
- `provenance-trace.json`：兼容别名，内容与 v5.2 `trace.json` 保持一致。新分析链路应优先读取 `trace.json`。
- `events.jsonl`：旧版事件流，保留用于兼容。
- `records.jsonl`：语义 write-ahead log。节点、边、artifact、finish 等记录会边运行边写入，便于长跑 case 追踪。
- `raw-events.jsonl`：低层运行事件流，主要用于调试 trace 系统本身，不作为主要归因入口。
- `partial/latest.json`：运行中快照。长时间运行或收到 `SIGINT/SIGTERM/SIGHUP` 时也能保留可查看状态。
- `artifacts/sha256/`：按内容 hash 去重保存大文本，例如模型上下文包、MCP 返回、skill 指令、工具输出、压缩前后摘要等。
- `trace.html`：唯一正式离线可视化报告，包含 Overview、Trace Health、Semantic Pipeline、LLM Turns、Lifecycle、Subagents、Claim Evidence Matrix、Semantic Evidence、Execution Observations、Agent Flow、Component Dataflow、IO Inspector、Semantic Facts、Context And Compaction 和 Artifacts。大文本通过 artifact 链接查看，避免 HTML 过度膨胀。

当前实现不再生成 `viewer.html`。`events.jsonl`、`legacy-trace.json`、`provenance-trace.json` 仍作为兼容副产物保留；新分析链路应优先使用 `manifest.json`、`trace.json` 和 `trace.html`。

> 注意：`OPENCODE_CASE_TRACE=1` 会记录用于复盘的语义信息，可能包含私域代码、工具输出和模型上下文。生产或企业内网环境请把 `OPENCODE_CASE_TRACE_DIR` 指向受控目录，并按企业数据策略管理 trace 文件。

## 7. 运行 benchmark case

普通 prompt case 示例：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID="T1-001"
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/opencode-traces"

opencode run --format json "请阅读当前代码仓，定位用户登录失败可能涉及的模块和入口函数。"
```

带文件输入示例：

```bash
export OPENCODE_CASE_ID="T2-014"

opencode run \
  --format json \
  --file ./requirements/T2-014.md \
  "请根据需求文档分析需要修改哪些模块，并给出实现方案。"
```

如果你的 benchmark runner 会循环执行 case，建议每个 case 单独设置 `OPENCODE_CASE_ID`：

```bash
for case_id in T1-001 T1-002 T2-001; do
  export OPENCODE_CASE_TRACE=1
  export OPENCODE_CASE_ID="$case_id"
  export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/opencode-traces"

  opencode run --format json "$(cat ./cases/$case_id/prompt.txt)" \
    > "./outputs/$case_id/result.jsonl"
done
```

v5.0 起，若要和端到端 benchmark runner 的真实交互链路保持一致，推荐通过 headless server 创建 session 并发送 HTTP message 请求，而不是用 `opencode run` 直接启动一次性 shell case：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID="T1-001"
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/opencode-traces"

opencode serve --hostname 127.0.0.1 --port 0
```

拿到 stdout 中的 `http://127.0.0.1:<port>` 后：

```bash
SERVER="http://127.0.0.1:<port>"
WORKDIR="/path/to/repo"

SESSION_ID="$(
  curl -sS -X POST "$SERVER/session?directory=$WORKDIR" \
    -H 'content-type: application/json' \
    -d '{"title":"T1-001"}' | jq -r '.id'
)"

curl -sS -X POST "$SERVER/session/$SESSION_ID/message?directory=$WORKDIR" \
  -H 'content-type: application/json' \
  -d '{
    "model": {"providerID": "deepseek", "modelID": "deepseek-v4-pro"},
    "agent": "build",
    "parts": [{"type": "text", "text": "请阅读当前代码仓，定位用户登录失败可能涉及的模块和入口函数。"}]
  }'
```

这种方式的 trace 会覆盖 server request、session、message、processor、tool/MCP/subagent 与 final response 的完整链路，更贴近 benchmark case 的实际执行行为。

## 8. 查看 trace

结构化检查：

```bash
jq '{case_id, status, duration_ms, token_usage, files}' \
  /data/evo-bench/opencode-traces/T1-001/manifest.json

jq '{records: (.records | length), dataflow_edges: (.dataflow_edges | length), artifacts: (.artifacts | length), metrics}' \
  /data/evo-bench/opencode-traces/T1-001/trace.json
```

打开可视化报告：

```bash
xdg-open /data/evo-bench/opencode-traces/T1-001/trace.html
```

无 GUI 服务器可以将整个 `T1-001/` 目录下载到本地，再用浏览器打开 `trace.html`。由于大文本 artifact 不再默认内嵌进 HTML，单独下载 `trace.html` 只能看到摘要，无法打开 artifact 链接。

## 9. Trace 覆盖范围

当前 trace 覆盖：

- `run`：CLI case 入口、session 选择、权限自动处理、case 结束状态。
- `prompt/context`：prompt parts 解析、文件/引用/agent 解析、上下文消息数、系统提示数量、工具数量。
- `llm`：模型、provider、agent、LLM turn 生命周期、流式事件、LLM 调用耗时、finish/stop reason 和 token usage。
- `processor`：step start/finish、token usage、tool call、compact/continue/stop 决策、exit gate 与 turn lifecycle。
- `tool`：通用工具输入输出摘要、耗时、错误。
- `skill`：skill 名称、目录、权限确认、加载结果。
- `task`：subagent 类型、子 session、任务结果。
- `mcp`：MCP 连接、tools/list、tool/call、错误。

## 10. Trace Semantic Contract v5.2

`trace.json` 的 `trace_version` 为 `"5.2"`。v5.2 的目标不是自动判断根因，而是把离线归因分析需要消费的事实、回答 claim、输入输出、上下文快照、任务循环决策、生命周期闭环、证据来源和组件间数据流结构化记录下来，并额外暴露 trace 自身的质量信号。相比 v5.1，v5.2 重点收敛 evidence 分层、case/server 状态拆分、claim 归因降噪、subagent 引用摘要化和 compaction 质量字段。

主字段：

- `records`：组件事实记录，每条记录包含 `record_id`、`component`、`event_type`、时间、状态、摘要数据、`source_refs`、`source_locations`、`typed_resources` 和 `artifact_refs`。正式 records 不包含 `runtime.event` 或流式 delta。
- `dataflow_edges`：组件间数据流边，只表达数据如何流转，例如 `selected_into_context`、`prompted`、`produced`、`consumed`、`compressed_from`、`compressed_to`、`spawned`、`continued_from`、`derived_from`、`verified_by`、`modified_by`、`transformed_to`、`used_as_context`、`selected_by`、`called`、`returned_to`、`delegated_to`、`reported_to`、`supported_response`、`claimed_by`、`supports_claim`、`contextualizes_claim` 和 `executed_for_claim`。
- `artifacts`：大文本或结构化大对象索引，完整内容在 `artifacts/sha256/` 下按 hash 去重保存。
- `metrics`：spans、events、records、dataflow_edges、artifacts、token/cost 统计和 `trace_health` 质量指标。
- `manifest.status` / `manifest.server_status`：server/process 结束状态，例如 HTTP server 被 SIGINT 停止时为 `cancelled`。
- `manifest.case_status`：case 业务完成状态；如果最终用户可见回答已经产生且没有错误，即使 server 关闭状态是 `cancelled`，case 也可标记为 `success`。正式 record 中会同步写入 `case.completed` 或 `case.failed`。

事实引用采用 `type:id` 格式，例如：

```text
context_snapshot:ctx_1_xxxxxxxx
tool_span:span_2_xxxxxxxx
verification:ver_1_xxxxxxxx
change:chg_1_xxxxxxxx
```

大文本不会直接塞进 `trace.json`。字段中如果出现 `artifact_id` 或 `payload_ref`，说明完整内容保存在 `artifacts/` 中，并可通过 `trace.html` 的 artifact 链接查看。v5.2 的大字段摘要还会记录 `payload_dedupe_group_id`，便于离线分析识别多处重复引用的同一 payload。

v5.2 增强了这些语义事实：

- `prompt.assembly` 记录用户原始请求、模板解析、持久化 user message、subagent prompt 等 prompt 组装过程。
- `context.transform` 记录 session messages 在插件转换前后、转为 model messages、LLM request ready、provider message transform、compaction prompt build 等层级转换。
- `decision` 作为正式事实进入 `records`，覆盖任务编排 step、模型 reasoning block、LLM tool call、tool execute、compaction context selection、LLM step finish 等可观测决策。
- `mcp.call` 和 MCP observation 会尝试解析 text JSON，提取 `typed_resources` 和 `source_locations`，例如 `repo_fact` 的 `subject/predicate/value/path/line_start/line_end`。MCP JSON text 还会优先进入 `evidence.semantic_fact.structured_claim`，`extraction_method` 保留为 `mcp_json_text`，避免退化成 `returned N content item` 这类泛化事实，也避免被较弱的行级正则抢先解析。
- `subagent.call` 会在记录顶层保留 `child_session_id`、`child_status`、`child_trace_available`、`child_trace_unavailable_reason` 和 `output_artifact_id`，并通过 `prompt.assembly` 与 `dataflow_edges` 展示 parent agent 与 subagent 的 prompt/结果交互。v5.2 会优先识别同一份 trace 内的 child session 记录，并用 `child_trace_mode=inline_same_trace`、`child_timeline_summary`、`child_metric_summary`、`child_key_evidence_refs` 和 `child_trace_artifact_ref` 表达内联子任务链路。父节点只保留少量 `child_record_refs` 样本，完整 child refs 写入 artifact；只有既没有内联记录也没有子 trace 文件时才保留 `child_trace_unavailable_reason`。
- `response.output` 会标记 `response_role`，区分 `final_answer`、`intermediate_summary`、`subagent_result` 和 `auto_continue_summary`。只有最终用户答案应为 `is_final_for_case: true`。同时会把引用拆为 `direct_evidence_refs`、`context_refs` 和 `execution_refs`。
- `response.claim` 会把最终用户答案拆成可归因的结论单元。v5.2 只对 `response_role=final_answer`、`visibility=user_visible`、`is_final_for_case=true` 的最终回答生成 claim，避免中间工具说明或 compaction summary 污染 claim matrix。拆分时会保护代码路径、函数调用、小数、百分比和内联代码，并过滤 `Here's the summary`、`No further steps needed`、Markdown 标题、纯序号、表格表头/分隔线、上下文资料目录等无事实含义文本。Markdown 表格中的事实行会保留为 `claim_format=table_fact`，并额外记录 `raw_text`、`canonical_text`、`table_cells`、`table_subject` 和 `table_values`。每个 claim 记录 `direct_evidence_refs`、`legacy_context_refs`、`context_refs`、`execution_refs`、`matched_evidence_refs`、`candidate_evidence_refs`、`match_reasons`、`match_strategy`、`match_score`、`support_level`、`quality_flags` 和 `attribution_summary`。v5.2 会把顶层 `response.claim.source_refs` 收敛为直接证据优先；宽泛 prompt/context/tool refs 放入 `legacy_context_refs`，离线归因不应把它们当直接证据。
- `context.compaction_check` 会记录一次是否需要压缩的判定，包括 provider/model、token estimate、context limit、reserved tokens、overflow、selected algorithm 和 trigger reason。即使没有真正触发 `context.compaction`，也能解释为什么当前轮未压缩。测试场景可通过 `OPENCODE_TRACE_FORCE_COMPACTION=1` 触发每个 session 一次确定性强制压缩，并在 metadata 中标记 `deterministic_forced_compaction`。
- `context.compaction` 的 `context_ledger` 会记录 retained/dropped message id、算法名和 `ledger_id_quality`；v5.2 还会把 `algorithm`、`before_context_refs`、`after_context_refs`、`serialized_tail_artifact_ref`、`summary_artifact_ref`、`retained_message_ids`、`dropped_message_ids`、`retained_fact_refs`、`dropped_fact_refs`、`retained_fact_count`、`dropped_fact_count`、`token_estimate_before`、`token_estimate_after`、`retention_ratio`、`compression_loss_risks` 和 `auto_continue_prompt_ref` 提升到顶层，便于离线归因模块不解析深层 ledger 也能消费压缩前后链路。`output_summary` 即使较短也会保留 artifact ref，`auto_continue_prompt_ref` 会在缺少显式 after refs 时进入 `after_context_refs`。如果压缩记录自身缺少 token estimate，但同 session/message 附近已有 `context.compaction_check`，v5.2 会回填 `token_estimate_before` 并记录 `estimate_source=nearest_compaction_check`。
- `llm.call` 会记录 request-level facts，包括 agent、provider、model、message/tool 数、token/cache 使用和 stop/finish reason。
- `llm.turn` 作为 LLM 调用的语义生命周期视图，记录 turn id、session/message、agent role、provider/model、输入上下文引用、状态、耗时、finish/stop reason、request id 和 token usage。离线归因应优先消费 `llm.turn`，再回看 `llm.call` 的 span 级细节。
- `agent.lifecycle` 记录关键任务循环节点，例如 `turn.started`、`response.completed`、`compaction.required`、`subagent.resume` 等，用于还原 agent 在主流程、压缩流程和子任务流程中的阶段性状态。
- `exit.gate` 记录非交互式执行是否继续、退出、等待或取消，以及当时是否已有 final answer、是否需要 compaction、是否触发 auto-continue、continuation 来源和原因。
- `evidence.semantic_fact` 从高价值 observation 中抽取稳定事实，例如工具读取、MCP 返回、skill 加载、验证输出和 subagent 输出。记录中会包含 `fact_kind`、`canonical_subject`、`claim`、`structured_claim`、`support_level` 和 `quality_flags`。`structured_claim` 会尽量表达为 `subject/predicate/value/source_span/extraction_method`。v5.2 会递归解析 `content[].text`、JSON 文本、`<path>...</path>` 文件输出和带行号源码行，并优先抽取行级语义事实；无法可靠解析的普通工具输出进入 `execution.observation`，todo/todowrite 进入 `task.plan_state`，不再混入 semantic evidence。
- `skill.load` 除了真实 skill 加载事件，也会记录用户显式请求某个 skill 但模型上下文未暴露该 skill 的事实。此类记录包含 `skill_name`、`request_source`、`request_status`、`available_skill_names` 和错误信息。v5.2 会递归解析 prompt `parts/input/messages/prompt/body` 中的 skill request，把缺失或失败的 skill 工具调用闭合为 `status=error`，并通过 `skill_request_unresolved` 计入 trace health，避免 finalizer 把缺失 skill 误标为成功。
- `loop.decision` 会把 processor step finish / loop finish 类运行事件提升为正式事实，记录 decision、reason、agent、message id、part count、part types、final-answer 状态和 auto-continue 标记。
- 工具、MCP、skill、subagent 和 shell/grep/read/edit 输出会尽量抽取 `typed_resources` 与 `source_locations`，包括常见的 `path:line` 文本位置。
- `metrics.trace_health` 记录 trace 质量事实，包括结构化 JSON 中的 circular marker、未闭合/被 finalizer 收口的 running records、expected lifecycle finalized records、unexpected missing close records、缺 token usage 的 LLM turn、缺 finish reason 的 LLM turn、background/title LLM turn、compaction quality flags、空 subagent 结果、过宽 response refs、带 legacy context refs 且缺直接证据的 response claims、重复 semantic facts、generic semantic facts、execution observations、task plan states、generic MCP facts、path-only evidence facts、unsupported response claims、context-only response claims、broken claim fragments、non-final response claims、weak evidence matches、MCP JSON parse shadowing、unresolved skill requests、payload duplication groups 和 compaction check missing。v5.2 会把 server 进程结束时被 finalizer 收口的 `run.start` 和 `agent.lifecycle` 视为 expected lifecycle finalization，避免把正常 HTTP server 收尾误报为 unexpected missing close。这些指标只描述 trace 质量，不给出根因诊断。
- `metrics.token_usage`、`manifest.token_usage` 和 `llm.call.token_usage` 使用路径栈 JSON-safe 序列化；共享对象引用不会被误判为 `"[Circular]"`，真实路径环才会被标记。
- trace 结束时会对仍处于 `running` 的 span/node 做 finalizer 收口，写入 `finalized_status: "finalized_without_close"` 和 `finalized_reason`，避免成功 case 的主 trace 中残留未解释的 running 记录。
- 只重复路径且没有独立语义价值的 `tool_output` observation 不进入正式 records。

`trace.html` 视图：

- `Overview`：查看 case、run、耗时、records、dataflow、artifacts、tokens 和组件统计。
- `Trace Health`：查看 trace 自身质量事实，例如 circular markers、open/finalized records、expected lifecycle finalized records、LLM token/finish reason 缺失、compaction flags、空 subagent 结果、过宽 response refs、重复 semantic facts、generic semantic facts 和 unsupported response claims。
- `Semantic Pipeline`：按“用户请求、prompt 组装、上下文转换、LLM 调用、决策、工具/skill/MCP/subagent、输出”的顺序展示语义流水线。
- `LLM Turns`：集中展示规范化 LLM turn，按角色区分主 agent、subagent、compaction、title/background 等调用。
- `Lifecycle`：展示 turn milestone、compaction required、auto continue 和 exit gate 决策。
- `Subagents`：展示 delegated prompt、child session/message、returned output、child timeline summary、child metric summary、关键 evidence refs 和完整 child trace refs artifact。
- `Claim Evidence Matrix`：按最终回答 claim 展示 direct evidence、legacy refs、context refs、execution refs、support level 和 quality flags，是人工检查与离线归因消费前的主入口。
- `Semantic Evidence`：集中展示从工具、MCP、skill、验证和 subagent 输出中抽取出的稳定事实及其来源，优先展示 `structured_claim` 的 subject、predicate、value、source span 和 extraction method。
- `Execution Observations`：展示普通工具观察和 todo/plan state，这些记录用于还原执行过程，但默认不作为回答 claim 的直接证据。
- `Agent Flow`：按时间展示全部组件事实记录，不再只展示前 120 条。
- `Component Dataflow`：查看组件间数据流边，不做根因判断。
- `IO Inspector`：聚合 prompt/context/LLM/decision/tool/MCP/skill/subagent/response 的输入输出摘要，输入和输出左右分栏并支持横向滚动。
- `Semantic Facts`：集中展示 typed resources、source locations、source refs、artifact refs、最终回答事实和 loop decisions。
- `Context And Compaction`：展示 LLM context package、`context.compaction_check` 和 compaction 前后摘要；页面中保留 `Context Ledger` 兼容文案。
- `Artifacts`：查看大文本 artifact 索引和路径。

`legacy-trace.json` 作为兼容副产物保留旧版 `spans`、`events`、`context_snapshots`、`verification_records`、`change_records`、`constraint_records`、`response_segments`、`design_records` 和 `dataflow_edges`。旧版 `final_response_evidence`、`semantic_edges`、`evidence_refs` 不再作为正式输出字段使用。

语义层会对常见敏感字段做脱敏，包括 `apiKey`、`authorization`、`cookie`、`secret`、`password`、`credential`、`access_token`、`refresh_token`、`auth_token`，以及 `sk-...`、`Bearer ...` 等字符串模式。`token_usage`、`token_estimate`、`tokens`、`inputTokens`、`outputTokens` 等计量字段不会被误脱敏。

## 11. 常见问题

### 没有生成 trace

确认三个变量同时存在：

```bash
echo "$OPENCODE_CASE_TRACE"
echo "$OPENCODE_CASE_ID"
echo "$OPENCODE_CASE_TRACE_DIR"
```

`OPENCODE_CASE_TRACE` 必须是 `1`、`true`、`yes` 或 `on`。

### trace.html 为空或缺少工具调用

先看 `legacy-trace.json` 中是否存在 spans：

```bash
jq '.spans[] | {component, operation, name, status, duration_ms}' \
  "$OPENCODE_CASE_TRACE_DIR/$OPENCODE_CASE_ID/legacy-trace.json"
```

如果没有工具调用，通常说明该 case 没有触发工具，或者模型在失败前还没有发起 tool call。

### 不想记录完整 prompt 或代码内容

保持默认配置即可。默认只写摘要、长度、hash 和 preview。不要设置：

```bash
OPENCODE_CASE_TRACE_FULL_CONTENT=1
```

### 希望彻底恢复原 opencode

找到备份文件后恢复：

```bash
sudo install -m 0755 /usr/local/bin/opencode.bak.<timestamp> /usr/local/bin/opencode
opencode --version
```
