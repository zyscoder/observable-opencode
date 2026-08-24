# Observable OpenCode

面向 AI Harness 评测、诊断和持续优化的可观测 OpenCode。该分支在保持
OpenCode Agent 原有执行行为的前提下，被动记录任务编排、上下文变换、LLM
请求、Tool/Skill/MCP、Subagent、代码变更、验证结果和最终回复之间的语义数据流，
并提供独立的离线因果归因模块。

> 本项目基于开源 [OpenCode](https://github.com/anomalyco/opencode) 改造，
> 不是 OpenCode 官方发行版，也不隶属于 OpenCode 团队。

## 核心能力

- **可评估**：可以用 benchmark case、评审事实和外部 evaluation 结果描述最终表现。
- **可观测**：每个 case 生成一份结构化 Trace；可用独立的离线 renderer 生成 `trace.html` 查看完整 Agent 流程和组件数据流。
- **可归因**：基于 Causal IR、重建的数据流和 LLM 判断执行后向语义污点分析。
- **可优化**：把根因、因果链、证据引用和语义缺口转化为 Harness 的下一轮优化输入。
- **行为隔离**：Trace 插装只被动记录；归因模块离线运行，不把分析结果反馈给正在执行的 Agent。

```mermaid
flowchart LR
    B["Benchmark / User Request"] --> H["Observable OpenCode"]
    H --> A["Agent Runtime"]
    A --> L["LLM"]
    A --> T["Tool / Skill / MCP"]
    A --> S["Subagent"]
    A -. "passive events" .-> C["Causal IR Trace"]
    L -. "request / response" .-> C
    T -. "input / output" .-> C
    S -. "delegation / result" .-> C
    C --> V["Offline Trace Renderer"]
    V --> HT["trace.html"]
    C --> R["Offline Attribution"]
    Q["User Defect Question"] --> R
    R --> O["Root Cause / Causal Chain / Evidence / Gaps"]
```

## 快速开始

下面以 OpenAI 兼容接口为例，给出从启动 Agent 到生成 Trace 的最短可执行路径。
`MODEL`、`APIKEY`、`URL` 是配置文件中的环境变量占位符，不是 Observable OpenCode
新增的模型协议。模型解析、Provider 选择和认证仍由 OpenCode 原生配置负责。

```bash
export MODEL="<provider-model-id>"
export URL="https://<openai-compatible-host>/v1"
export APIKEY="<api-key>"

export OPENCODE_CONFIG_CONTENT='{
  "model": "compatible/{env:MODEL}",
  "provider": {
    "compatible": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenAI Compatible",
      "options": {
        "baseURL": "{env:URL}",
        "apiKey": "{env:APIKEY}",
        "timeout": 720000,
        "chunkTimeout": 720000
      },
      "models": {
        "{env:MODEL}": {"name": "{env:MODEL}"}
      }
    }
  }
}'

export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID="benchmark-case-001"
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/traces"

opencode /data/repos/target-project
```

这里的 `MODEL` 是兼容接口直接接收的模型 ID，例如 `glm-5.1`，不要添加
`compatible/` 前缀。OpenCode 使用固定 Provider ID `compatible`，最终选择的完整模型标识为
`compatible/$MODEL`。`URL` 必须填写服务实际要求的 Base URL；如果接口路径是
`/v1/chat/completions`，通常应以 `/v1` 结尾。

在 TUI 中完成提问后正常退出。程序会尝试关闭当前 segment、离线合并全部 segment，并在成功时
打印本次 root session 的 `trace.json` 和 `partial/latest.json` 路径。如果目录中暂时只有
`session.json` 和 `segments/`，原始 Trace 并未丢失，需要使用独立的 `observable-trace finalize`
发布统一 `trace.json`。完整命令见[从 Segment 生成 Trace](#从-segment-生成-trace)。HTML 仅由
独立 renderer 离线生成。HTTP benchmark 使用方式见
[启动 HTTP Server 并记录 Trace](#启动-http-server-并记录-trace)，离线分析方式见
[使用离线归因 CLI](#使用离线归因-cli)。

## 获取 Release 可执行文件

GitHub Actions 会发布 Linux、macOS 和 Windows 的独立可执行文件，不需要在目标机器上
执行 `bun install`。从 [Releases](https://github.com/zyscoder/observable-opencode/releases)
选择对应资产：

| 系统 | Runtime 资产 | 离线 Renderer 资产 |
| --- | --- | --- |
| Linux x86_64 | `opencode-observable-linux-x64` | `observable-trace-linux-x64` |
| 旧 x86_64 CPU（无 AVX2） | `opencode-observable-linux-x64-baseline` | `observable-trace-linux-x64-baseline` |
| Alpine/musl x86_64 | `opencode-observable-linux-x64-musl` | `observable-trace-linux-x64-musl` |
| Alpine/musl 旧 x86_64 CPU（无 AVX2） | `opencode-observable-linux-x64-baseline-musl` | `observable-trace-linux-x64-baseline-musl` |
| Linux arm64 | `opencode-observable-linux-arm64` | `observable-trace-linux-arm64` |
| Alpine/musl Linux arm64 | `opencode-observable-linux-arm64-musl` | `observable-trace-linux-arm64-musl` |
| macOS Apple Silicon | `opencode-observable-darwin-arm64` | `observable-trace-darwin-arm64` |
| macOS Intel | `opencode-observable-darwin-x64` | `observable-trace-darwin-x64` |
| macOS Intel（无 AVX2） | `opencode-observable-darwin-x64-baseline` | `observable-trace-darwin-x64-baseline` |
| Windows arm64 | `opencode-observable-windows-arm64.exe` | `observable-trace-windows-arm64.exe` |
| Windows x64 | `opencode-observable-windows-x64.exe` | `observable-trace-windows-x64.exe` |
| Windows x64（无 AVX2） | `opencode-observable-windows-x64-baseline.exe` | `observable-trace-windows-x64-baseline.exe` |

以 Linux x86_64 为例：

```bash
RELEASE_TAG="<Releases 页面中的版本，例如 v1.2.3-observable.1>"
ASSET="opencode-observable-linux-x64"
TRACE_ASSET="observable-trace-linux-x64"
BASE_URL="https://github.com/zyscoder/observable-opencode/releases/download/${RELEASE_TAG}"

curl -fL -o "$ASSET" "$BASE_URL/$ASSET"
curl -fL -o "$TRACE_ASSET" "$BASE_URL/$TRACE_ASSET"
curl -fL -o SHA256SUMS "$BASE_URL/SHA256SUMS"
grep "  $ASSET$" SHA256SUMS | sha256sum --check
grep "  $TRACE_ASSET$" SHA256SUMS | sha256sum --check
chmod +x "$ASSET"
chmod +x "$TRACE_ASSET"
./"$ASSET" --version
```

替换现有 `opencode` 前建议先备份：

```bash
CURRENT_OPENCODE="$(command -v opencode || true)"
if [ -n "$CURRENT_OPENCODE" ]; then
  cp "$CURRENT_OPENCODE" "${CURRENT_OPENCODE}.backup.$(date +%Y%m%d%H%M%S)"
fi
sudo install -m 755 opencode-observable-linux-x64 /usr/local/bin/opencode
sudo install -m 755 observable-trace-linux-x64 /usr/local/bin/observable-trace
opencode --version
command -v observable-trace
```

macOS 校验可执行：

```bash
RELEASE_TAG="<Releases 页面中的版本，例如 v1.2.3-observable.1>"
ASSET="opencode-observable-darwin-arm64"
TRACE_ASSET="observable-trace-darwin-arm64"
BASE_URL="https://github.com/zyscoder/observable-opencode/releases/download/${RELEASE_TAG}"

curl -fL -o "$ASSET" "$BASE_URL/$ASSET"
curl -fL -o "$TRACE_ASSET" "$BASE_URL/$TRACE_ASSET"
curl -fL -o SHA256SUMS "$BASE_URL/SHA256SUMS"
grep "  $ASSET$" SHA256SUMS | shasum -a 256 --check
grep "  $TRACE_ASSET$" SHA256SUMS | shasum -a 256 --check
chmod +x "$ASSET"
chmod +x "$TRACE_ASSET"
./"$ASSET" --version
```

如需全局调用 renderer：

```bash
sudo install -m 755 observable-trace-darwin-arm64 /usr/local/bin/observable-trace
command -v observable-trace
```

如果 macOS 阻止运行从浏览器下载的二进制，可在确认校验和及来源后移除隔离属性：

```bash
xattr -d com.apple.quarantine opencode-observable-darwin-arm64
xattr -d com.apple.quarantine observable-trace-darwin-arm64
```

## 配置模型与 Provider

Observable OpenCode 不引入专属运行时 Provider，也不读取某个供应商专用 API Key。
模型、认证和 Provider 均由 **OpenCode 原生配置链** 决定；本项目只被动记录已经发生的
执行。请以 OpenCode 实际加载的配置和原生 CLI、请求级覆盖为权威，不应依赖本文推断
配置合并或优先级。

OpenCode 会依次加载并合并全局配置目录（通常为 `~/.config/opencode/`）中的
`config.json`、`opencode.json` 和 `opencode.jsonc`，并发现项目中的 `opencode.json`、
`opencode.jsonc` 以及 `.opencode/opencode.json`、`.opencode/opencode.jsonc`。也可使用原生入口
`OPENCODE_CONFIG`、`OPENCODE_CONFIG_DIR`、`OPENCODE_CONFIG_CONTENT`，或原生 CLI 和
请求参数覆盖。不同版本及配置来源会按 OpenCode 的原生合并规则处理。

下面是一个兼容 OpenAI 风格 API 的通用模板。可放在项目 `opencode.json` 或你选择的
原生配置文件中：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "compatible/{env:MODEL}",
  "provider": {
    "compatible": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenAI Compatible",
      "options": {
        "baseURL": "{env:URL}",
        "apiKey": "{env:APIKEY}",
        "timeout": 720000,
        "chunkTimeout": 720000
      },
      "models": {
        "{env:MODEL}": { "name": "{env:MODEL}" }
      }
    }
  }
}
```

其中 `MODEL` 是 Provider 接口直接接收的原始模型 ID，不包含 `compatible/` 前缀；
`APIKEY` 和 `URL` 分别提供该兼容服务的认证和 Base URL。它们只是此模板选用的环境变量
占位符，可以替换为企业自己的变量名，并不是 Observable OpenCode 的特殊运行时环境变量。
若使用 OpenCode 内置 Provider，应优先使用该 Provider 的原生认证和配置方式。

使用任意 OpenAI 兼容接口时，只需要替换下面三个环境变量。URL 必须以供应商或企业网关的
实际文档为准：

```bash
export MODEL="<provider-model-id>"
export URL="https://<openai-compatible-host>/v1"
export APIKEY="<api-key>"
```

在启动 OpenCode 前，建议先直接验证兼容接口。`URL` 是 Base URL，下面的命令会在其后
追加 `/chat/completions`；因此常见服务应把 `URL` 配成以 `/v1` 结尾，而不是把完整
`/v1/chat/completions` 写入 `URL`：

```bash
curl -sS --fail-with-body \
  --connect-timeout 10 \
  --max-time 120 \
  -H "Authorization: Bearer $APIKEY" \
  -H "Content-Type: application/json" \
  "${URL%/}/chat/completions" \
  --data "$(jq -nc --arg model "$MODEL" '{
    model: $model,
    messages: [{role: "user", content: "只回答数字：1+2等于多少？"}],
    stream: false
  }')" | jq .
```

应同时确认 HTTP 请求成功、响应中包含 assistant 消息、返回模型与 `$MODEL` 一致或符合网关
映射。只得到 HTTP 200 并不等于模型配置正确；若响应不是预期答案，应先核对 `$URL` 是否缺少
`/v1`、模型 ID 是否受网关支持，以及返回体中的错误或路由信息。

企业网络无法稳定访问 `models.dev` 时，可关闭启动阶段的远程模型目录刷新。程序会使用
编译进可执行文件的模型快照：

```bash
export OPENCODE_DISABLE_MODELS_FETCH=1
```

也可以按 OpenCode 原生方式限制刷新超时或指向企业内部镜像：

```bash
export OPENCODE_MODELS_FETCH_TIMEOUT_MS=2500
# export OPENCODE_MODELS_URL="https://models.example.internal"
```

### 运行时环境变量

| 环境变量 | 是否必需 | 作用 |
| --- | --- | --- |
| `OPENCODE_CONFIG` | 否 | 指向额外的 OpenCode 原生配置文件。 |
| `OPENCODE_CONFIG_DIR` | 否 | 指定 OpenCode 原生配置目录。 |
| `OPENCODE_CONFIG_CONTENT` | 否 | 直接注入 OpenCode 原生 JSON 配置，适合 CI 或 benchmark。 |
| `MODEL` | 取决于配置 | 本文兼容接口模板使用的原始模型 ID，不包含 `compatible/` 前缀。 |
| `APIKEY` | 取决于配置 | 本文模板使用的 Provider API Key。请通过 Secret 注入，不要写入仓库。 |
| `URL` | 取决于配置 | 本文模板使用的兼容 API Base URL。 |
| `OPENCODE_DISABLE_MODELS_FETCH` | 推荐 | 设为 `1` 时跳过启动阶段的 `models.dev` 请求，使用内置模型快照。 |
| `OPENCODE_MODELS_FETCH_TIMEOUT_MS` | 否 | 远程模型目录请求超时，单位为毫秒。 |
| `OPENCODE_MODELS_URL` | 否 | 将远程模型目录切换到企业镜像。 |
| `OPENCODE_CASE_TRACE` | 是 | 设为 `1` 启用语义 Trace。 |
| `OPENCODE_CASE_TRACE_DIR` | 推荐 | Trace 根目录；未设置时使用 OpenCode 数据目录下的 `case-traces/`。 |
| `OPENCODE_CASE_ID` | 推荐 | case 的稳定标识，建议使用 benchmark case ID。 |
| `OPENCODE_CASE_TRACE_QUIET` | 否 | 设为 `1` 隐藏退出时的 Trace 路径提示，不影响落盘。 |
| `OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS` | 否 | TUI worker 完成资源清理后，等待独立 journal-close RPC 的最大时长（毫秒）；启用 Trace 时默认 `3600000`，未启用时默认 `5000`。worker 资源清理另有固定 `5000` 毫秒上限；该变量不限制 materializer、Agent 或 LLM 的执行时长。 |
| `OPENCODE_SERVER_PASSWORD` | HTTP 推荐 | 为 `opencode serve` 启用 Basic Auth，用户名固定为 `opencode`。 |

高级变量 `OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH` 控制结构化记录中内联字段的预览长度，
默认值为 `2048`。大文本会写入 `artifacts/` 并由 HTML 按需展示。通常应保持默认值；将它
提高到数十万会显著放大序列化、内存和收尾开销，复杂 case 甚至可能延迟信号处理。

Trace recorder 的默认内存边界是 512 个 hot Causal IR nodes、1,024 个 hot edges、每类最多
256 个 recent refs，以及 2,048 字符的内联语义预览；已完成的 active lifecycle record 会立即
驱逐。冷数据从 segment-local SQLite 查询，增大这些边界不是保持语义完整性的前提。验收上限是
标准 10,000-record payload 在 warm-up 后的 observer-owned RSS 增长不超过 128 MiB，以及 1 GiB
journal 的离线 materializer peak RSS 不超过 256 MiB。这些是 Trace 自身的内存边界，不会改变
OpenCode 的模型 context window、compaction 或 Agent 行为。

### 目标仓库的 `.opencode` 扩展依赖

Release 可执行文件包含 Observable OpenCode 本身，但无法预先打包任意目标仓库中
`.opencode/` 插件、Tool 或 Skill 所依赖的 npm/workspace 包。如果启动时报错类似：

```text
Cannot find module '@opencode-ai/plugin' from '/path/to/project/.opencode/...'
```

应按目标仓库自己的开发说明安装或提供这些依赖。若该扩展与本次 benchmark 无关，也可以
只在 benchmark 副本中停用对应扩展。不要把这类错误误判为 Release 二进制缺少自身依赖，
也不要为了绕过错误而修改生产仓库。

## 交互式 TUI 并记录 Trace

交互式 TUI 是一等入口。先通过上述 OpenCode 原生配置选择 Provider 和模型，再启动
目标项目：

```bash
export MODEL="<provider-model-id>"
export URL="https://<openai-compatible-host>/v1"
export APIKEY="<api-key>"

export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/traces"
# 可选：为一次 benchmark case 指定稳定名称
export OPENCODE_CASE_ID="benchmark-case-001"

opencode /data/repos/target-project
```

同一 root session 的多轮交互写入同一份 Trace。输入 `/new` 会独立完成旧 root，并为
新 root 创建另一份 Trace；Subagent 的事件归入其父 root。正常退出、root session 删除
或收到可捕获的 `SIGINT`、`SIGTERM`、`SIGHUP` 时，程序会先持久化已有数据，再对每个
已完成 root 向 `stderr` 输出保存位置：

```text
[observable-opencode] Session trace saved
  session: ses_...
  case: benchmark-case-001
  status: completed
  directory: /data/evo-bench/traces/benchmark-case-001--ses_...--a1b2c3d4
  json: /data/evo-bench/traces/benchmark-case-001--ses_...--a1b2c3d4/trace.json
  partial: /data/evo-bench/traces/benchmark-case-001--ses_...--a1b2c3d4/partial/latest.json
```

在 TUI 中，正常退出、`Ctrl-C`/`SIGINT` 和 `SIGTERM` 都走同一条 parent/worker 生命周期：
worker 先释放 Session、Tool、MCP 和内部 Server，只向当前 segment journal 追加轻量
`case.runtime_closed` terminal record；parent 随后终止 worker，并在独立进程中运行 materializer，
最后发布 root `trace.json`。完整 Causal IR、provenance 和 legacy projection 不在 Agent worker 的
退出堆内构造。

parent 首先等待 worker 释放 Session、Tool、MCP 和内部 Server；这一步有独立且不可配置的
`5000` 毫秒清理上限。清理成功后才调用 journal-close RPC。`OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS`
只限制这次 journal close，启用 Trace 时默认 `3600000`，未启用时默认 `5000`。它不是 LLM、
Tool 或整个 case 的执行超时。journal close 完成后 parent 终止 worker，再启动独立 materializer；
materializer 不受该变量限制，TUI 会等待其子进程完成，但 materializer 失败仍只降低 observability。
确需覆盖 journal-close 上限时可使用：

```bash
# 允许复杂长 session 在 Ctrl-C 后最多用两小时完成 journal close
export OPENCODE_TUI_SHUTDOWN_TIMEOUT_MS=7200000
```

worker close 或 materializer 失败只降低 observability：不会重试 Agent 操作，不会改变消息、
文件、session rows 或原始退出码。失败时 immutable segment 仍是权威证据，可使用
`observable-trace finalize <logical-case-dir>` 重试；成功后，终端才会打印 logical directory、
root JSON 和 partial 路径。`OPENCODE_CASE_TRACE_QUIET=1` 只隐藏这份 Trace receipt。

使用 `opencode -s <session-id> /path/to/project` 继续同一 session 时，runtime 会定位已有 logical
case directory，在 `segments/` 下创建新的 process-lifetime segment，并用 `continuation_of` 和
统一 Trace 中的 `run.continuation` 语义连接前一 run。已有 journal、artifact、index 和
`segment.json` 不会被打开写入、截断或删除。

`SIGKILL` 和 OOM 无法执行任何用户态 cleanup，因此不会立即完成 Trace，也不应期待当前 run
马上出现新的 `trace.json` 或 `partial/latest.json`。被杀前已完整 append 的 JSONL 行和 artifact
仍是 durable evidence；之后显式 finalization 或下一次同 session continuation 会把无 terminal
record 的前一 segment 标记为 `interrupted_unfinalized`，并从 valid journal prefix 恢复统一 Trace。

## 启动 HTTP Server 并记录 Trace

Benchmark 可以通过 `opencode session <-> HTTP server <-> request` 执行，以贴近
Harness 的实际交互行为。HTTP 路径与 TUI 使用同一套原生配置；请求中不需要重复指定
模型。

```bash
export OPENCODE_SERVER_PASSWORD="<server-password>"
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/traces"
export OPENCODE_CASE_ID="benchmark-case-001"

opencode serve --hostname 127.0.0.1 --port 4096
```

在另一个终端创建 session 并发送消息：

```bash
PROJECT_DIR="/data/repos/target-project"
AUTH="$(printf 'opencode:%s' "$OPENCODE_SERVER_PASSWORD" | base64)"

SESSION_JSON="$(curl -fsS -X POST http://127.0.0.1:4096/session \
  -H "Authorization: Basic $AUTH" \
  -H "x-opencode-directory: $PROJECT_DIR" \
  -H 'content-type: application/json' \
  --data '{}')"
SESSION_ID="$(printf '%s' "$SESSION_JSON" | jq -er '.id')"

curl -fsS --max-time 3600 -X POST "http://127.0.0.1:4096/session/$SESSION_ID/message" \
  -H "Authorization: Basic $AUTH" \
  -H "x-opencode-directory: $PROJECT_DIR" \
  -H 'content-type: application/json' \
  --data '{
    "parts": [{"type": "text", "text": "分析并修复当前项目中的测试失败。"}]
  }'
```

消息接口会同步等待本轮 Agent 执行完成。复杂 case 应同时为 `curl`、Benchmark Harness、
反向代理和负载均衡器设置足够长且一致的超时，避免客户端提前断开后将正常执行误记为
取消或失败。

只有特定 case 需要与项目或全局配置不同的模型时，才使用 OpenCode 原生请求级 model
覆盖。完成一个 HTTP session 时建议显式删除它，以立刻完成该 root 的 Trace；否则服务
关闭时会统一收尾：

```bash
curl -fsS -X DELETE "http://127.0.0.1:4096/session/$SESSION_ID" \
  -H "Authorization: Basic $AUTH" \
  -H "x-opencode-directory: $PROJECT_DIR"
```

HTTP server 与 TUI 使用同一 logical-root/immutable-segment 格式。删除 root session 或正常、
可捕获信号关闭 server 会自动完成可达 Trace；进程被 `SIGKILL` 时同样只能在之后从 durable
segment evidence 恢复。自动发布失败不会改变 HTTP response 或 session 数据，可在 server 外部
用同一个 `observable-trace finalize <logical-case-dir>` 命令重试。

## 从 Segment 生成 Trace

Observable OpenCode 与 Trace 工具承担不同职责：

| 程序 | 职责 | 是否调用 Agent 或 LLM |
| --- | --- | --- |
| `opencode-observable-<platform>` | 运行 Agent，并持续把语义事实追加到当前 segment | 是 |
| `observable-trace-<platform> finalize` | 合并 logical case 的全部 segment，发布统一 `trace.json` | 否 |
| `observable-trace-<platform> render` | 从已有 Trace 生成供人查看的 `trace.html` | 否 |

`segments/` 中的 journal 才是原始、持久化的事实记录。`trace.json` 是从这些事实重建出来的
统一 Causal IR，也是离线归因模块的标准输入。因此，**只看到 `segments/` 和 `session.json`
并不表示 Trace 丢失**，而是表示统一 Trace 尚未发布或需要重新发布。

### 一次完整操作

下面以 Linux x86_64 release 为例。macOS Apple Silicon 只需把 `TRACE_BIN` 换成
`./observable-trace-darwin-arm64`，其他平台使用 Release 中相同平台后缀的资产。

```bash
TRACE_ROOT="/data/evo-bench/traces"
TRACE_BIN="./observable-trace-linux-x64"

# 1. 找到 logical case。每个有效 case 目录的根部都有 session.json。
find "$TRACE_ROOT" -mindepth 2 -maxdepth 2 -name session.json -print

# 2. 从上一步选择目标 session.json。不要选择 segments/<id> 目录。
SESSION_FILE="<将上一步输出中的目标 session.json 完整路径填在这里>"
CASE_DIR="${SESSION_FILE%/session.json}"

# 3. 查看该 logical case 包含哪些 process-lifetime segments。
jq '{logical_case_id, session_id, generation, segments}' "$SESSION_FILE"

# 4. 从全部 segments 发布统一 Trace。重复执行是安全的。
"$TRACE_BIN" finalize "$CASE_DIR"

# 5. 验证正式归因输入已经生成。
test -s "$CASE_DIR/trace.json"
jq '{
  case_id: .manifest.case_id,
  status: .manifest.status,
  session_generation: .manifest.session_generation,
  segment_count: (.manifest.segments | length),
  record_count: (.records | length),
  edge_count: (.dataflow_edges | length)
}' "$CASE_DIR/trace.json"

# 6. 可选：生成人工查看页面。
"$TRACE_BIN" render "$CASE_DIR"
echo "$CASE_DIR/trace.html"
```

`finalize` 成功时会明确打印：

```text
completeness: complete
trace: <logical-case-dir>/trace.json
manifest: <logical-case-dir>/manifest.json
partial: <logical-case-dir>/partial/latest.json
```

`completeness: incomplete` 表示仍成功生成了可查看、可分析的统一 Trace，但至少一个 segment
没有完整 terminal record，例如进程曾被 `SIGKILL` 或 OOM 终止。此状态不能被当作一次正常
完成的 Agent 执行，归因时也应保留该中断事实。

### 如何确定 CASE_DIR

`OPENCODE_CASE_TRACE_DIR` 是所有 Trace 的**根目录**，不是某个 case 的输入目录。
`OPENCODE_CASE_ID` 是期望的 case 名称，但 `/new`、多个 HTTP root session、名称冲突和
process-level 记录可能产生带 session/digest 后缀的 sibling directory：

```text
/data/evo-bench/traces/
├── benchmark-case-001/
│   ├── session.json                    # 一个 logical root
│   └── segments/
├── benchmark-case-001--ses_...--a1b2c3d4/
│   ├── session.json                    # 另一个 logical root
│   └── segments/
└── benchmark-case-001--process--e5f6a7b8/
    ├── session.json                    # process-level root
    └── segments/
```

退出时打印的 `directory:` 是最可靠的 `CASE_DIR`。如果没有收到退出回执，可列出全部
`session.json`，再根据 `session_id`、`logical_case_id` 和 segment 时间选择目标：

```bash
find "$TRACE_ROOT" -mindepth 2 -maxdepth 2 -name session.json -print \
  | while IFS= read -r session_file; do
      jq -r --arg directory "${session_file%/session.json}" \
        '[$directory, .logical_case_id, .session_id, .generation,
          (.segments[-1].started_at // "-")] | @tsv' "$session_file"
    done
```

不要把 `OPENCODE_CASE_TRACE_DIR`、`segments/`、`segments/<segment-id>` 或某个
`records.jsonl` 当作 `finalize` 的 `<case-dir>`。正确目录必须在根部直接包含 `session.json`。

## Trace 目录结构

### 采集中或尚未 finalize

Agent 执行期间，看到下面的最小结构是正常现象：

```text
<logical-case-dir>/
├── session.json                        # segment 顺序、session identity 和 generation
└── segments/
    └── <segment-id>/
        ├── segment.json                # 本次 process lifetime 的身份和状态
        ├── records.jsonl               # 权威的 append-only Causal IR journal
        ├── events.jsonl                # 兼容事件流
        ├── raw-events.jsonl            # 原始兼容事件流
        ├── index.sqlite                # observer 私有索引
        └── artifacts/                  # 大文本、Tool 输出等语义载荷
```

此时不要求存在 `trace.json`。运行时持续追加 segment，不在 Agent 进程内反复构造完整 Trace 或
HTML，以避免长上下文 session 的内存和 I/O 开销。

### finalize 后

`observable-trace finalize <case-dir>` 会从 `session.json` 指定顺序的所有 segment 重建并原子
发布一个 generation：

```text
<logical-case-dir>/
├── session.json
├── segments/                           # 原始证据，finalize 不改写
├── .derived/                           # generation 管理，勿手工编辑
├── trace.json                          # 统一 Causal IR；归因标准输入
├── manifest.json                       # 本次统一发布的元数据
├── partial/latest.json                 # 完整性、终态和恢复视图
├── provenance-trace.json               # 兼容投影
├── legacy-trace.json                   # 旧格式兼容投影
└── artifacts/                          # 对 segment artifacts 的兼容访问
```

根部派生文件由 `.derived` 中的当前 generation 原子切换，具体是符号链接还是兼容副本属于实现
细节。使用者应始终访问根部的 `trace.json`、`manifest.json` 和 `partial/latest.json`，不要直接
绑定某个 `.derived/generations/<n>` 路径。

`records.jsonl` 与 segment artifacts 是 durable authority；`session.json` 是 ordered segment
manifest；root `trace.json` 是可重建的统一派生输出。不要手工合并、截断或移动 segment 文件。
归档时应复制整个 logical case directory，而不是只复制 `trace.json`。

现有 flat root 仍作为只读 legacy segment zero 支持。原有 root `records.jsonl`、`index.sqlite`
和 artifacts 不会被 runtime 改写；后续 continuation 会写入新 segment。

## 使用 observable-trace 工具

### finalize：正式发布 trace.json

```text
usage: observable-trace finalize <case-dir>
```

`finalize` 只接受 logical case directory。它会读取当前 `session.json.generation`，从每个
ordered segment 的 valid journal prefix 重建统一 Trace，再原子发布 root derived outputs。
如果 finalization 期间 session generation 持续变化，命令会拒绝发布并提示 session changed；
应等待当前 Agent 请求结束或关闭 session 后重试。

典型命令：

```bash
"$TRACE_BIN" finalize "$CASE_DIR"
```

以下场景都可以重复执行 `finalize`：

- 自动 materialization 失败，只剩 `session.json` 和 `segments/`；
- `SIGKILL`、OOM 或机器重启后恢复已有 journal；
- 使用 `opencode -s <session-id>` 续跑，新增了 segment；
- `trace.json` 的 `.manifest.session_generation` 落后于 `session.json.generation`；
- 需要确认当前 root projection 与全部 segment 一致。

### render：生成人工查看的 trace.html

```text
usage: observable-trace render <case-dir-or-file> [--output <path>]
```

推荐把 logical case directory 作为输入：

```bash
"$TRACE_BIN" render "$CASE_DIR"
"$TRACE_BIN" render "$CASE_DIR" --output "/data/evo-bench/reports/case-001.html"
```

不指定 `--output` 时，默认生成 `<case-dir>/trace.html`。`render` 还接受 root
`session.json`、root `trace.json` 和 legacy flat `records.jsonl`。

如果 case directory 只有 `session.json` 和 `segments/`，或者 root `trace.json` 已落后于当前
generation，`render` 会在临时目录进行一次**只读 materialization**，然后生成 HTML。这个便捷
路径不会把 `trace.json` 发布回源 case directory。因此：

- 只想临时查看执行过程，可以直接 `render "$CASE_DIR"`；
- 需要运行归因、归档统一 Trace 或得到正式 `trace.json`，必须先执行 `finalize "$CASE_DIR"`；
- runtime 不会生成或刷新 HTML；每次新增 segment 或重新 finalize 后，应重新执行 `render`。

HTML 只用于人工查看主 Agent、Subagent、任务编排、上下文压缩、message 转换、LLM、
Tool/Skill/MCP、文件变更、验证和最终回复之间的数据流。不要把 `trace.html`、
`legacy-trace.json`、某个 physical segment 或 segment-local output 传给归因模块。

## 退出、续跑与恢复

| 场景 | Segment 是否保留 | 是否自动尝试 finalize | 使用者下一步 |
| --- | --- | --- | --- |
| TUI 正常退出 | 是 | 是 | 收到 Trace 回执后直接使用；没有 `trace.json` 时手动 `finalize` |
| TUI `Ctrl-C` / `SIGINT` | 是 | 是 | 等待收尾和路径回执，不要连续强杀；必要时手动 `finalize` |
| `SIGTERM` / `SIGHUP` | 是 | 是 | 状态可能为 `cancelled`，已有语义仍保留；检查并手动 `finalize` |
| HTTP 删除 root session | 是 | 是 | 使用回执或在 server 外手动 `finalize` |
| HTTP server 正常关闭 | 是 | 是 | 对所有可达 root 收尾；缺失派生文件时手动 `finalize` |
| `SIGKILL` / OOM / 断电 | 已完整 append 的记录保留 | 不可能 | 恢复 session 或直接对已有 case 执行 `finalize` |
| `opencode -s <session-id>` 续跑 | 旧 segment 只读，新建 segment | 退出时是 | 续跑完成后重新 `finalize` 和 `render` |

`SIGKILL` 无法运行任何用户态 handler，因此不能承诺当场生成 `trace.json`。下次 continuation 或
手动 finalization 会把没有 terminal record 的旧 segment 标记为 `interrupted_unfinalized`，并从
valid journal prefix 恢复可用事实。

使用同一 session 续跑：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/traces"
export OPENCODE_CASE_ID="benchmark-case-001"

opencode -s "<session-id>" /data/repos/target-project
```

runtime 会定位已有 logical case directory，在 `segments/` 下创建新的 process-lifetime segment，
并用 `continuation_of` 和统一 Trace 中的 `run.continuation` 连接前一 run。已有 journal、artifact、
index 和 `segment.json` 不会被重新打开写入、截断或删除。

### 常见问题

**目录中只有 `segments/` 和 `session.json`**

这是未发布统一 Trace 的状态。先确认 Agent/session 已停止或当前请求已结束，然后执行：

```bash
"$TRACE_BIN" finalize "$CASE_DIR"
```

**`finalize` 报 expected session.json**

传入了错误层级。`CASE_DIR` 必须直接包含 `session.json`，不能传 Trace 根目录、`segments/` 或
单个 segment。

**`finalize` 报 trace session changed repeatedly during materialization**

Agent 仍在产生新 segment 或更新 manifest。等待请求完成、删除 HTTP root session 或正常关闭
TUI/server 后重试。

**已经有 `trace.html`，但没有 `trace.json`**

说明 HTML 可能由 `render` 的临时只读 materialization 生成。HTML 不能用于归因；执行
`finalize "$CASE_DIR"` 正式发布 root `trace.json`。

**`trace.json` 已存在，但新增 session continuation 后内容没有更新**

旧文件对应上一 generation。重新执行 `finalize`，随后重新执行 `render`。可用下面命令比较：

```bash
jq -r '.generation' "$CASE_DIR/session.json"
jq -r '.manifest.session_generation' "$CASE_DIR/trace.json"
```

**如何判断 Trace 是否可用于归因**

至少确认 root `trace.json` 存在、JSON 可解析，并且 generation 一致：

```bash
test -s "$CASE_DIR/trace.json"
jq -e . "$CASE_DIR/trace.json" >/dev/null
test "$(jq -r '.generation' "$CASE_DIR/session.json")" = \
  "$(jq -r '.manifest.session_generation' "$CASE_DIR/trace.json")"
```

即使 generation 一致，`manifest.status=cancelled` 或 `completeness=incomplete` 仍表示执行曾被
取消或中断。此类 Trace 包含有效证据，但分析结论必须考虑缺少正常终态的影响。

## 使用 Root Cause Analysis Skill

`rootcause-analysis` 是面向 Claude Code、OpenCode 等 Agent 的灵活根因分析 Skill，规范文件位于
仓库唯一的规范位置：`.claude/skills/rootcause-analysis/SKILL.md`。它让 Agent 根据用户提出的
具体疑问读取已经 finalize 的语义 Trace，执行递归后向语义污点分析、多假设回溯和独立根因确认，
最终同时给出结构化 JSON 与面向人的 Markdown 说明。

不同 Agent runtime 的 Skill 调用语法不同，不能混用。

### Claude Code

Claude Code 使用 slash command `/rootcause-analysis`：

```text
/rootcause-analysis
Trace: /absolute/path/to/case/trace.json
Question: 为什么用户明确要求通过构建 Skill 使用 Yocto，但实际只进行了 GCC 局部编译？
```

### OpenCode

OpenCode 不使用带美元符号的 Skill 简写。在请求中明确要求 Agent 先 invoke the `skill` tool，
并把 Skill 名称设置为 `rootcause-analysis`，加载成功后再开始分析：

```text
开始分析前，先调用 skill 工具并传入 name=rootcause-analysis。
Trace: /absolute/path/to/case/trace.json
Question: 为什么用户明确要求通过构建 Skill 使用 Yocto，但实际只进行了 GCC 局部编译？
```

输入必须满足以下条件：

- `Trace` 是绝对路径，并指向通过 `observable-trace finalize` 发布的 root `trace.json`；
- `Question` 准确描述用户期望、实际行为和希望解释的差异，不能只写“为什么失败”；
- 不要把 `records.jsonl`、单个 segment、`trace.html` 或尚未 finalize 的 partial Trace 作为输入。

默认情况下，Skill 只在对话中返回 `rootcause-analysis/v1` JSON 和与其证据互证的 Markdown，
**不会写任何文件**。如需保存报告，必须同时提供显式输出前缀和被分析项目根目录，例如：

```text
使用当前 runtime 的上述方式加载 rootcause-analysis Skill。
Trace: /data/evo-bench/traces/case-001/trace.json
Question: 为什么用户要求使用构建 Skill 完成 Yocto 验证，但 Agent 最终只执行了 GCC 局部编译并声称验证完成？
Output prefix: /data/evo-bench/rootcause-results/case-001/analysis
Analyzed project root: /workspace/product-repo
```

输出前缀必须位于 Trace bundle 和被分析项目之外。Skill 会生成
`/data/evo-bench/rootcause-results/case-001/analysis.json` 与同名 `.md`；无法可靠确认目录边界时，
它会停止写入并请求补充信息，而不是猜测路径。

### 诊断 Trace 查询

Skill 自带只读、标准库实现的查询工具，适合在 Agent 分析前检查 Trace，或人工复核报告引用：

```bash
export TRACE=/absolute/path/to/case/trace.json
export TRACE_QUERY=.claude/skills/rootcause-analysis/scripts/trace_query.py

python3 "$TRACE_QUERY" validate --trace "$TRACE"
python3 "$TRACE_QUERY" summary --trace "$TRACE"
python3 "$TRACE_QUERY" search --trace "$TRACE" --query "yocto" --limit 20 --offset 0
python3 "$TRACE_QUERY" node --trace "$TRACE" --ref node:dec_1
python3 "$TRACE_QUERY" neighbors --trace "$TRACE" --ref node:dec_1 --direction upstream
python3 "$TRACE_QUERY" paths --trace "$TRACE" --start node:final_1 --max-depth 8 --limit 20

# 先从相关节点的 artifact_refs 找到实际 Artifact ID，再读取内容。
python3 "$TRACE_QUERY" node --trace "$TRACE" --ref node:req_1
python3 "$TRACE_QUERY" artifact --trace "$TRACE" --id build-requirement --max-chars 12000
```

`search` 返回 `truncated` 和 `next_offset`；结果被截断时应继续翻页，不能把单页上限当成候选预算。
`node` 会展示节点及全部已记录边，`neighbors`/`paths` 只沿允许用于因果推断的边遍历；Artifact
读取会校验 Trace 声明的 SHA-256，不读取未经验证的外部内容。具体参数可用
`python3 "$TRACE_QUERY" <command> --help` 查看。

### 与 Python 归因模块的关系

两者彼此独立，也不会互相调用：

| 路径 | 适用场景 | 当前状态 |
| --- | --- | --- |
| `rootcause-analysis` Agent Skill | 需要结合问题与语义数据流灵活探索、多假设回溯，并给出可读解释 | 推荐；当前主要使用路径 |
| `tools/trace_attribution` Python 模块 | 需要固定 CLI/API、checkpoint 和批处理自动化 | 保留但暂停迭代 |

无论使用哪条路径，分析都必须保持被动、只读、事后执行。`rootcause-analysis` 只读取 finalize 后的
Trace，不修改 Agent、Harness、Skill、MCP、Tool、Prompt、模型配置、代码、构建环境、Trace 或
原 Session，也不会把结论反馈给原 Agent。报告中的优化项仅是建议，必须由独立任务评审和实施。

### Root-Cause Skill 评估交接

本仓库只负责生成评估输入，不实现 scored Provider-backed 执行，也不承担执行环境的信任边界。
对 Skill 做效果评分时，不能直接把仓库内 fixture 路径交给 Agent，否则 Agent 可能读取
`cases.json`、隐藏期望、rubric、其他 fixture 或 Git 历史。先由评估器构造单 case workspace。
Agent 可见目录、prompt 和派生 Trace 只使用每轮新生成的 opaque ID；fixture 到 opaque ID 的
映射、原始/派生摘要和隐藏 rubric 只保存在评估器侧：

```bash
export FORWARD_ROOT=$(mktemp -d /tmp/rootcause-forward.XXXXXX)
export OPAQUE_CASE_ID=$(python3 -c 'import uuid; print("case-" + uuid.uuid4().hex)')
python3 tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py \
  --case known-root \
  --opaque-case-id "$OPAQUE_CASE_ID" \
  --agent-trace-path /workspace/trace.json \
  --destination "$FORWARD_ROOT/$OPAQUE_CASE_ID" \
  > "$FORWARD_ROOT/evaluator-provenance.json"
```

每个 workspace 只包含该 case 的 finalize 派生 `trace.json`、Trace 声明且摘要校验通过的 Artifacts、
规范 `.claude/skills/rootcause-analysis`，以及 `prompt-claude.md`、`prompt-opencode.md`。builder
拒绝覆盖已有目录、symlink 越界、节点完整性错误和 Artifact 摘要不一致。它不会复制
`cases.json`、`rubric.json`、隐藏 `expected`、fixture 语义标签、其他 fixture、reports 或 Git 历史。
每个 canonical node 必须声明有效 `source_hash`；`--agent-trace-path` 必须是无控制字符、无 `..`
的绝对 POSIX 路径。派生 Trace 只替换 manifest/scope 的运行身份并重算节点完整性，任务事实与
用户问题保持原样。

把 opaque workspace 提交给组织现有的 **可信 benchmark Harness / 隔离执行服务**。该外部系统
必须独立管理认证凭据、文件系统与网络隔离、运行时清理、镜像或执行环境证明、输出审计及超时终止；
这些能力不由本仓库实现或声明。本仓库也不提供任何接收 Provider secret、启动 Agent 或包装本地
源码执行的 root-cause 测试 helper。Harness 完成执行后，只有评估器可以在 Agent workspace 之外
读取 provenance、`pressure/cases.json` 和 `pressure/rubric.json` 并评分。

人工直接运行 Claude/OpenCode 或从本仓库源码启动 Agent，只能作为 Skill 发现、Prompt 可读性等
**unscored smoke** 检查，不能作为评分结果，也不能通过本仓库的 bundle helper 传入 Provider
凭据。执行、凭据注入和日志审计应完全留在既有可信 Harness 中。

## 使用离线归因 CLI

归因模块目前随源码仓库发布，并未安装为系统级命令。它不要求当前目录位于
`observable-opencode`；只需要把仓库中的 Python 包绝对路径加入 `PYTHONPATH`。
下面的 DeepSeek/Anthropic 兼容配置仅用于可选的离线归因 Judge，和 OpenCode 运行时的
Provider、模型选择及认证完全无关。

```bash
export OBSERVABLE_OPENCODE_HOME="/opt/observable-opencode"
export ATTRIBUTION_VENV="$HOME/.venvs/observable-opencode-attribution"
python3 -m venv "$ATTRIBUTION_VENV"
source "$ATTRIBUTION_VENV/bin/activate"
python -m pip install --upgrade pip anthropic

export ANTHROPIC_API_KEY="<your-deepseek-api-key>"
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_MODEL="deepseek-v4-flash"
export CLAUDE_TIMEOUT_SECONDS=3600

# 必须是 finalize 后、根部直接包含 trace.json 的实际 logical case 目录。
export CASE_DIR="/data/evo-bench/traces/benchmark-case-001--ses_...--a1b2c3d4"
test -s "$CASE_DIR/trace.json"

PYTHONPATH="$OBSERVABLE_OPENCODE_HOME/tools/trace_attribution" \
python -m trace_attribution \
  --engine recursive-agentic \
  --fusion-mode retrieval-global \
  --trace "$CASE_DIR/trace.json" \
  --question "为什么本次修改编译失败？" \
  --out /data/evo-bench/attribution/benchmark-case-001.json \
  --judge-timeout-sec 3600 \
  --judge-max-tokens 16000
```

渲染和归因是两个独立的离线步骤：使用 `observable-trace render <case-dir>` 查看 HTML；
使用上面的源码 checkout 命令，以 `--engine recursive-agentic --trace <case-dir>/trace.json`
和 `--question "..." --out <report>.json` 执行归因。即使已经渲染过 HTML，也必须把
`trace.json` 而不是 `trace.html` 传给归因 CLI。

归因 Judge 通过 Anthropic SDK 调用 Anthropic 兼容接口。它和运行 Observable OpenCode 的
模型相互独立，因此可以用一个模型执行 case、另一个模型离线归因。环境变量含义如下：

| 环境变量 | 是否必需 | 作用 |
| --- | --- | --- |
| `OBSERVABLE_OPENCODE_HOME` | 推荐 | 源码仓绝对路径；用于构造 `PYTHONPATH`，不要求 `cd` 到仓库。 |
| `ATTRIBUTION_VENV` | 否 | 本文示例使用的 Python 虚拟环境路径，可换成已有环境。 |
| `PYTHONPATH` | 是 | 至少包含 `$OBSERVABLE_OPENCODE_HOME/tools/trace_attribution`。 |
| `ANTHROPIC_API_KEY` | LLM Judge 必需 | Anthropic 或 Anthropic 兼容服务的 API Key。 |
| `ANTHROPIC_BASE_URL` | 兼容服务必需 | 例如 DeepSeek 的 `https://api.deepseek.com/anthropic`。 |
| `CLAUDE_MODEL` | 推荐 | 归因 Judge 使用的模型 ID。 |
| `CLAUDE_TIMEOUT_SECONDS` | 否 | 每次 Judge/repair 请求超时，默认 `3600` 秒。 |

推荐先使用 CLI 默认候选预算：`max_nodes=48`、`max_frontier_items=96`、
`max_hypotheses=24`、`max_investigation_rounds=12`、`max_judge_requests=128`。只有在报告
明确显示预算耗尽时再扩大相应参数。`--judge-max-tokens` 默认是 `8192`；推理模型输出
较长 JSON 时可像上例提高，但需确认服务端支持该上限。

`--question` 用于描述用户真正想定位的缺陷，例如：

- `为什么问题回答错误？`
- `为什么编译失败？`
- `为什么最终回复没有给出正确的配置项？`
- `为什么 Agent 计划调用 Subagent，但实际没有执行？`
- `项目任务属于特性迁移，migration-consistency Skill 明确要求执行一致性校验，但本次没有调用。请判断要求是否进入模型上下文、Skill 是否被发现和暴露、权限与 Skill 工具是否可用、首次偏离发生在哪里，以及偏差属于 Skill 配置、权限、上下文还是 Agent 决策。`

它与兼容参数 `--objective` 互斥。推荐使用 `recursive-agentic` 引擎执行
“候选检索、递归后向污点、多假设回溯、独立根因确认”的融合流程。

### Skill 调用遗漏归因

新版本 Trace 会被动记录正式语义节点 `skill.catalog.exposed`，其中包括：

- 各来源中发现的 Skill 候选、来源族和 `global/project/configured` 作用域；
- frontmatter 解析失败、同名冲突，以及 OpenCode 原运行时最终选中的文件；
- Agent 权限判断、实际暴露给模型的 Skill 名称和描述；
- 当前请求是否向模型提供了 `skill` 工具；
- catalog 节点到最终 `model_messages_built` 上下文的 Causal IR 数据流引用。

归因模块不会仅凭“某个 Skill 没有调用”直接认定 Agent 有错。它会把用户请求、归因问题与
Skill 描述做问题域匹配，得到 `expected_skill_names`，再按下面的生命周期检查首次缺席阶段：

```text
discovery -> selection -> exposure -> tool_availability -> invocation
```

结构化报告的离线问题投影中可查看：

- `expected_action_projection.expected_skill_candidates`：为什么该 Skill 被视为预期动作候选；
- `expected_action_projection.expected_skill_lifecycle`：发现、选中、暴露、权限、工具可用性和调用事实；
- `expected_action_projection.execution_window_refs`：catalog 暴露后的 LLM 回合、实际工具选择、Subagent 和最终回复；
- `first_absent_stage`：首次观察到动作缺席的边界；它只是定位事实，不是根因结论。

如果 Skill 未发现或 frontmatter 解析失败，候选根因偏向 Skill 安装/配置；如果被权限过滤或
`skill` 工具不可用，偏向权限或工具注册；如果 Skill 描述已随系统提示暴露、工具可用且调用仍
缺席，归因 Judge 会继续后向检查暴露后的 LLM 决策、工具选择和退出节点，确认是 Agent 决策
省略还是存在上游上下文影响。成功暴露 catalog 本身是契约/上下文证据，不能因为它靠近缺陷就
直接当作 Agent 决策根因。

只有 Skill 完整正文而 frontmatter `description` 没有写明触发条件时，Agent 在调用前无法看到
正文中的“特性迁移必须调用”要求。此时 Trace 能确认“描述已暴露但触发契约不可见”，并把优化
方向指向 Skill 描述设计；不能把未进入模型上下文的正文要求伪装成 Agent 已知事实。旧版本 Trace
若没有 `skill.catalog.exposed`，归因会以 `question_premise_unknown` / `evidence_insufficient`
结束，不会从普通 Tool 日志猜测 Skill 是否可用。

归因 JSON 在普通报告之外增加：

- `analysis_question`：规范化问题、稳定问题 ID 和 Trace 绑定；
- `conclusion`：已确认根因，或明确的证据不足结论；
- `causal_chain`：从表现缺陷回溯到引入节点的语义路径；
- `supporting_evidence_refs`：可审计的 Trace 证据引用；
- `rejected_hypotheses`：被排除的候选及原因；
- `confidence`：已确认根因的置信度；
- `unresolved_gaps`：Trace 缺失或仍无法判定的语义信息。
- `defect_evolution`：从行为契约、上下文传递、缺陷首次引入、动作物化、迟到补偿到用户观察的逐步语义演化。它同时保存机器可审计的节点、状态和证据，以及面向使用者的“谁做了什么、当时知道什么、为什么有问题、怎样影响下一步”。
- `defect_subject`、`expected_sequence`、`actual_sequence`、`first_deviation`：明确命名本次追踪的具体偏差、比较的动作顺序和首次偏离位置，避免使用“比较期望顺序”“缺陷出现”等没有对象的抽象表述。

对于已确认根因，模块会先根据 Causal IR、递归路径和数据流边确定性重建
`defect_evolution`，再调用同一个离线 Judge 对这些不可变步骤生成中文解释。LLM 只能补充说明，
不能增加、删除、合并或重排节点，也不能修改缺陷状态和证据引用；若输出违反约束，会自动回退
到确定性解释。该过程只读取 Trace，不改变 Agent、OpenCode session 或 Trace 采集行为。

如果没有足够证据，模块会返回
`no_confirmed_root_cause / evidence_insufficient`，不会为了给出答案而虚构根因。
对于已知缺陷节点，可重复传入 `--start-ref decision:dec_107`、`--start-ref node:<id>` 等
Trace 引用收窄起点；若不知道节点，保持自动候选检索即可。被中断且没有最终回复的 Trace
可能只能确认“中断触发了失败状态”，这不等于中断就是任务质量缺陷的根因，归因模块会在
证据不足时保留 `inconclusive` 结论。

### CLI 产物

若 `--out` 为 `/data/evo-bench/attribution/case-001.json`，归因过程中会同时维护结果、
message lineage、Judge cache 和递归 checkpoint。发生网络中断或进程重启后，可使用相同
参数继续分析，避免重复消耗已经完成的 Judge 请求。分析正常结束后，CLI 会依次打印
`--out` 指定的 JSON 路径和面向人工阅读的 Markdown 解释路径。

以上述 `--out` 为例，默认会得到：

```text
/data/evo-bench/attribution/
├── case-001.json                       # 最终结构化归因报告
├── case-001.explanation.md             # 完整缺陷描述和逐节点产生过程
├── case-001.message-lineage.json       # 离线重建的消息、上下文和数据流
├── case-001.judge-cache.jsonl          # 已完成的 LLM Judge 判断缓存
└── case-001.checkpoint/                # 递归分析断点，用于中断续跑
    ├── question-premise.json           # 用户问题前提判断及物理请求预留
    ├── manifest.json                   # Trace、问题、模型、预算等兼容性绑定
    ├── frontier.jsonl                  # 待分析/已分析节点
    ├── hypotheses.jsonl                # 多假设状态
    ├── investigation-actions.jsonl     # Judge、确认、预算和 Provider 生命周期
    ├── commit.json                     # 三类 journal 的一致提交点
    └── output-commit.json              # JSON 与 lineage 的原子发布状态
```

`--question` 首先经过独立的 premise gate，判断 Trace 是否支持“用户描述的偏差确实发生”。该结果
会在首次 Provider 请求前以 `inflight` 预留写入 `question-premise.json`，成功后原子替换为完整
assessment。这样，中断恢复不会重复调用 Provider，也不会把一次未可靠落盘的响应伪装成已完成
判断。premise、递归归因和缺陷解释共同受 `--max-judge-requests` 的物理请求上限约束；报告中的
`metadata.shared_judge_request_budget` 会分别列出三阶段的使用量。

归因被 `Ctrl-C`、`SIGTERM`、网络超时或进程重启打断时，应使用**完全相同的命令**重跑，包括
Trace、问题、模型、endpoint、预算、输出路径和 checkpoint 路径。CLI 会从已 fsync 的提交点恢复，
不会重放已完成 Judge 请求。若改变问题、模型、预算或 Trace，应使用新的 `--out`、
`--checkpoint-dir` 和 `--judge-cache`；不要删除旧 checkpoint 后覆盖原报告，以便保留审计链。

旧的未完成 checkpoint 如果没有无损保存 premise，会明确拒绝恢复，而不是重新询问模型并改变
因果输入。此时请保留旧目录用于审计，并使用新的输出与 checkpoint 路径重新分析。

`trace.html` 只用于人工查看 Agent 的原始执行流程，不承载归因结论。归因结果中的 Trace 引用
可以回到同一 case 的 `trace.html` 或 `trace.json` 核验，但不要把 HTML 当作归因输入。

直接阅读完整的缺陷产生过程：

```bash
cat /data/evo-bench/attribution/case-001.explanation.md
```

Markdown 默认采用工程复盘式表达：先明确本次追踪的偏差、期望动作顺序、实际动作顺序和首次
偏离，再按数据流逐步说明每个参与者做了什么、当时掌握了什么信息、为何引入或没有引入偏差、
结果怎样影响下一步。`tool.result`、节点 ID、`absent/present/propagated` 等内部字段只出现在文末
“技术证据附录”，用于开发者审计，不要求普通使用者理解。

本次升级不改变归因 CLI、Python API 或环境变量的使用方式。成功分析后仍然同时得到结构化
JSON 和 Markdown 报告；变化仅包括 `defect_evolution/v2` 新增偏差对象、期望/实际动作序列、
首次偏离和人类可读步骤字段，以及 Markdown 增加工程复盘正文和技术证据附录。对已经存在的
`trace.json`，使用原命令重新执行归因即可生成新版报告，不需要重新运行 Agent 或重新采集 Trace。

#### 查看分析结果

先指定报告路径：

```bash
REPORT=/data/evo-bench/attribution/case-001.json
```

查看面向用户的核心结论：

```bash
jq '{
  analysis_question,
  analysis_outcome,
  conclusion,
  confidence,
  defect_evolution,
  causal_chain,
  supporting_evidence_refs,
  rejected_hypotheses,
  unresolved_gaps
}' "$REPORT"
```

其中 `analysis_outcome` 表示整体判定状态：

- `root_found`：至少一个缺陷分支已经找到并确认根因；
- `partial_root_found`：部分缺陷分支已确认根因，其他分支仍未收敛；
- `no_defect`：现有证据没有确认用户所描述的缺陷；
- `inconclusive`：Trace、Judge 或搜索证据不足，无法可靠确认。

查看已确认根因及其置信度、理由和证据：

```bash
jq '{
  outcome: .analysis_outcome,
  conclusion,
  roots: .confirmed_roots,
  confidence
}' "$REPORT"
```

查看完整后向因果链：

```bash
jq '.causal_chain' "$REPORT"
```

查看各缺陷分支的节点判断、遍历路径和终止原因：

```bash
jq '.defect_branches' "$REPORT" | less
```

查看哪些 Trace 语义缺失或仍阻碍归因：

```bash
jq '{
  unresolved_gaps,
  trace_improvement_report
}' "$REPORT"
```

最终重点字段如下：

- `conclusion`：确认的根因或证据不足结论；
- `analysis_outcome`：整体判定状态，区分找到根因、部分找到、无缺陷和证据不足；
- `confirmed_roots`：已独立确认的根因节点、原因、置信度和证据；
- `causal_chain`：从表象缺陷后向回溯到引入位置的路径；
- `supporting_evidence_refs`：可回到 Trace 核验的证据引用；
- `rejected_hypotheses`：已排除候选及排除依据；
- `unresolved_gaps`：仍需补充的 Trace 语义信息；
- `trace_improvement_report`：本次归因暴露出的 Trace 插装改进建议。

## Python API

下面的 API 可嵌入任意 Python 程序。运行该程序时，只需通过 `PYTHONPATH` 提供仓库中
`tools/trace_attribution` 的绝对路径；程序的当前工作目录无需位于
`observable-opencode` 仓库。

```python
from pathlib import Path

from trace_attribution import AttributionOptions, AttributionRequest, analyze

result = analyze(
    AttributionRequest(
        trace_path=Path("/data/evo-bench/traces/benchmark-case-001/trace.json"),
        output_path=Path("/data/evo-bench/attribution/benchmark-case-001.json"),
        question="为什么本次修改编译失败？",
        options=AttributionOptions(
            engine="recursive-agentic",
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/anthropic",
        ),
    )
)

print(result.payload["conclusion"])
print(result.payload["causal_chain"])
print(result.payload["supporting_evidence_refs"])
```

详细的归因算法、预算、checkpoint、benchmark bundle 和报告字段说明见
[tools/trace_attribution/README.md](tools/trace_attribution/README.md)。

## 从源码运行与验证

源码构建需要 Bun；GitHub Release 二进制的目标机器不需要 Bun。

```bash
bun install
bun run --cwd packages/opencode typecheck
bun --cwd packages/opencode test \
  test/observability/trace-publication.test.ts \
  test/observability/case-trace.test.ts

PYTHONPATH=tools/trace_attribution \
python3 -m unittest discover -s tools/trace_attribution/tests
```

---

## 上游 OpenCode 说明

<p align="center">
  <a href="https://opencode.ai">
    <picture>
      <source srcset="packages/console/app/src/asset/logo-ornate-dark.svg" media="(prefers-color-scheme: dark)">
      <source srcset="packages/console/app/src/asset/logo-ornate-light.svg" media="(prefers-color-scheme: light)">
      <img src="packages/console/app/src/asset/logo-ornate-light.svg" alt="OpenCode logo">
    </picture>
  </a>
</p>
<p align="center">The open source AI coding agent.</p>
<p align="center">
  <a href="https://opencode.ai/discord"><img alt="Discord" src="https://img.shields.io/discord/1391832426048651334?style=flat-square&label=discord" /></a>
  <a href="https://www.npmjs.com/package/opencode-ai"><img alt="npm" src="https://img.shields.io/npm/v/opencode-ai?style=flat-square" /></a>
  <a href="https://github.com/anomalyco/opencode/actions/workflows/publish.yml"><img alt="Build status" src="https://img.shields.io/github/actions/workflow/status/anomalyco/opencode/publish.yml?style=flat-square&branch=dev" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh.md">简体中文</a> |
  <a href="README.zht.md">繁體中文</a> |
  <a href="README.ko.md">한국어</a> |
  <a href="README.de.md">Deutsch</a> |
  <a href="README.es.md">Español</a> |
  <a href="README.fr.md">Français</a> |
  <a href="README.it.md">Italiano</a> |
  <a href="README.da.md">Dansk</a> |
  <a href="README.ja.md">日本語</a> |
  <a href="README.pl.md">Polski</a> |
  <a href="README.ru.md">Русский</a> |
  <a href="README.bs.md">Bosanski</a> |
  <a href="README.ar.md">العربية</a> |
  <a href="README.no.md">Norsk</a> |
  <a href="README.br.md">Português (Brasil)</a> |
  <a href="README.th.md">ไทย</a> |
  <a href="README.tr.md">Türkçe</a> |
  <a href="README.uk.md">Українська</a> |
  <a href="README.bn.md">বাংলা</a> |
  <a href="README.gr.md">Ελληνικά</a> |
  <a href="README.vi.md">Tiếng Việt</a>
</p>

[![OpenCode Terminal UI](packages/web/src/assets/lander/screenshot.png)](https://opencode.ai)

---

### Installation

```bash
# YOLO
curl -fsSL https://opencode.ai/install | bash

# Package managers
npm i -g opencode-ai@latest        # or bun/pnpm/yarn
scoop install opencode             # Windows
choco install opencode             # Windows
brew install anomalyco/tap/opencode # macOS and Linux (recommended, always up to date)
brew install opencode              # macOS and Linux (official brew formula, updated less)
sudo pacman -S opencode            # Arch Linux (Stable)
paru -S opencode-bin               # Arch Linux (Latest from AUR)
mise use -g opencode               # Any OS
nix run nixpkgs#opencode           # or github:anomalyco/opencode for latest dev branch
```

> [!TIP]
> Remove versions older than 0.1.x before installing.

### Desktop App (BETA)

OpenCode is also available as a desktop application. Download directly from the [releases page](https://github.com/anomalyco/opencode/releases) or [opencode.ai/download](https://opencode.ai/download).

| Platform              | Download                           |
| --------------------- | ---------------------------------- |
| macOS (Apple Silicon) | `opencode-desktop-mac-arm64.dmg`   |
| macOS (Intel)         | `opencode-desktop-mac-x64.dmg`     |
| Windows               | `opencode-desktop-windows-x64.exe` |
| Linux                 | `.deb`, `.rpm`, or `.AppImage`     |

```bash
# macOS (Homebrew)
brew install --cask opencode-desktop
# Windows (Scoop)
scoop bucket add extras; scoop install extras/opencode-desktop
```

#### Installation Directory

The install script respects the following priority order for the installation path:

1. `$OPENCODE_INSTALL_DIR` - Custom installation directory
2. `$XDG_BIN_DIR` - XDG Base Directory Specification compliant path
3. `$HOME/bin` - Standard user binary directory (if it exists or can be created)
4. `$HOME/.opencode/bin` - Default fallback

```bash
# Examples
OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/install | bash
XDG_BIN_DIR=$HOME/.local/bin curl -fsSL https://opencode.ai/install | bash
```

### Agents

OpenCode includes two built-in agents you can switch between with the `Tab` key.

- **build** - Default, full-access agent for development work
- **plan** - Read-only agent for analysis and code exploration
  - Denies file edits by default
  - Asks permission before running bash commands
  - Ideal for exploring unfamiliar codebases or planning changes

Also included is a **general** subagent for complex searches and multistep tasks.
This is used internally and can be invoked using `@general` in messages.

Learn more about [agents](https://opencode.ai/docs/agents).

### Documentation

For more info on how to configure OpenCode, [**head over to our docs**](https://opencode.ai/docs).

### Contributing

If you're interested in contributing to OpenCode, please read our [contributing docs](./CONTRIBUTING.md) before submitting a pull request.

### Building on OpenCode

If you are working on a project that's related to OpenCode and is using "opencode" as part of its name, for example "opencode-dashboard" or "opencode-mobile", please add a note to your README to clarify that it is not built by the OpenCode team and is not affiliated with us in any way.

### FAQ

#### How is this different from Claude Code?

It's very similar to Claude Code in terms of capability. Here are the key differences:

- 100% open source
- Not coupled to any provider. Although we recommend the models we provide through [OpenCode Zen](https://opencode.ai/zen), OpenCode can be used with Claude, OpenAI, Google, or even local models. As models evolve, the gaps between them will close and pricing will drop, so being provider-agnostic is important.
- Built-in opt-in LSP support
- A focus on TUI. OpenCode is built by neovim users and the creators of [terminal.shop](https://terminal.shop); we are going to push the limits of what's possible in the terminal.
- A client/server architecture. This, for example, can allow OpenCode to run on your computer while you drive it remotely from a mobile app, meaning that the TUI frontend is just one of the possible clients.

---

**Join our community** [Discord](https://discord.gg/opencode) | [X.com](https://x.com/opencode)
