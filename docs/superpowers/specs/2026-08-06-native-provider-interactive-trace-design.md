# 原生模型配置与交互式 Session Trace 设计

## 1. 背景与目标

observable-opencode 当前发行说明过度使用 DeepSeek 和 HTTP server 作为示例，容易让
使用者误以为可观测版本只支持 `DEEPSEEK_API_KEY`，或只有通过 `opencode serve`
执行的请求才会生成 Trace。这不符合 OpenCode 原有的 Provider 配置和交互式运行方式。

本次改造目标是：

1. 保留 OpenCode 原生模型配置、Provider 发现和配置优先级，不增加 DeepSeek 专用行为；
2. 通过原生配置插值支持 `MODEL`、`APIKEY`、`URL`，适配 DeepSeek、GLM 及其他
   OpenAI-compatible API；
3. 同时覆盖 HTTP server 和 TUI 交互式运行；
4. 交互式运行按逻辑 session 生成独立 Trace，并在 session 结束或进程退出时完成落盘；
5. Trace 插装继续保持被动、只读、不可反馈给 Agent，不改变模型输入、工具调用或任务循环。

## 2. 方案比较

### 2.1 方案 A：在 OpenCode 核心中直接读取通用环境变量

启动时直接读取 `MODEL`、`APIKEY`、`URL`，构造运行时 Provider 并覆盖原生配置。

优点是部署命令短。缺点是通用变量容易与宿主环境冲突，会绕过原生配置优先级、Provider
schema、认证存储和模型能力声明，并使 observable 分支产生与上游不同的行为。不采用。

### 2.2 方案 B：只允许用户手工维护原生配置文件

完全依赖 `~/.config/opencode/opencode.json[c]` 或项目配置，不提供通用环境变量示例。

该方案最贴近上游，但 benchmark 和企业部署需要为每个运行环境改写配置文件，不利于
密钥注入、模型矩阵测试和容器化部署。

### 2.3 方案 C：原生配置为权威，环境变量仅作为配置插值

采用方案 C。OpenCode 仍通过原生 Config 模块加载配置；`MODEL`、`APIKEY`、`URL`
只在配置中的 `{env:...}` 占位符被解析。benchmark harness 需要临时配置时，可生成
标准 `opencode.json[c]`，或通过 `OPENCODE_CONFIG_CONTENT` 注入等价配置。

方案 C 同时保留上游行为和部署灵活性，不需要在 Provider 或 SessionPrompt 中增加
observable 专用分支。

## 3. 模型与 Provider 配置

### 3.1 配置权威与优先级

模型信息由 OpenCode 原生配置链路加载，包含：

- 全局配置目录中的 `config.json`、`opencode.json` 或 `opencode.jsonc`；
- 项目级 `opencode.json[c]` 和 `.opencode` 配置；
- `OPENCODE_CONFIG` 指定的配置文件；
- `OPENCODE_CONFIG_DIR` 指定的配置目录；
- `OPENCODE_CONFIG_CONTENT` 提供的内联配置；
- CLI 或单次 HTTP 请求中由上游原生支持的显式模型覆盖。

本次不改变上述加载顺序和合并规则，也不新增 `DEEPSEEK_API_KEY`、`MODEL`、
`APIKEY`、`URL` 的核心代码特判。

### 3.2 通用环境变量模板

对于 OpenAI-compatible 服务，可使用如下原生配置：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "{env:MODEL}",
  "provider": {
    "compatible": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenAI Compatible",
      "options": {
        "baseURL": "{env:URL}",
        "apiKey": "{env:APIKEY}"
      },
      "models": {
        "glm-4.5": { "name": "GLM 4.5" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    }
  }
}
```

`MODEL` 使用完整的 `provider/model` 标识，例如 `compatible/glm-4.5`。模型列表仍需
符合 OpenCode Provider schema；环境变量只替换值，不能绕过模型声明。使用
`/v1/chat/completions` 兼容接口时采用 `@ai-sdk/openai-compatible`；若服务实现
`/v1/responses`，则可按原生能力选择 `@ai-sdk/openai`。

对于 OpenCode 已内置支持的 Provider，继续优先使用其官方配置和认证方式。DeepSeek
和 GLM 只是文档示例，不成为 observable-opencode 的特殊依赖。

### 3.3 HTTP 请求模型选择

README 中的默认 HTTP 示例不携带硬编码 `model`，由 session 所在项目和全局配置决定。
只有 benchmark case 明确要求固定模型时，才在请求中使用上游支持的显式模型覆盖，并在
Trace 中记录该覆盖的来源和最终解析结果。

## 4. 双运行模式 Trace

### 4.1 HTTP server

`opencode serve` 继续由 HTTP session 创建、消息提交和 session 结束事件驱动 Trace。
每个逻辑 session 对应一个 case Trace 目录。并发 session 之间不得共享 active Trace
状态、事件序号、终态状态或输出目录。

### 4.2 TUI 交互式运行

`opencode <project>` 是与 HTTP server 等价的一等运行模式。用户首次进入或创建逻辑
session 时建立 Trace 上下文；同一个 session 的多轮对话写入同一份 Trace。用户新建、
切换或恢复另一个 session 时，Trace 事件按 `sessionID` 路由到对应的 session Trace，
而不是把整个 TUI 进程合并为一个 case。

Session Trace 的终态规则为：

- 明确结束或删除 session：完成该 session 的终态落盘；
- 切换 session：至少完成当前增量快照；若 session 后续恢复，则继续写入同一 Trace；
- 正常退出 TUI：完成所有仍活跃 session 的终态落盘；
- `SIGINT`、`SIGTERM`、`SIGHUP`：基于已有事件完成可捕获的 cancelled/partial 终态落盘；
- `SIGKILL`：无法执行信号处理器，只保证信号到达前已经实时写入的部分产物。

每个完成落盘的 session 通过 `stderr` 输出一次 Trace 回执，包含 session、状态、目录、
`trace.html`、`trace.json` 和可用的 partial 路径。回执不得污染结构化 stdout，也不得
改变原始退出码。

## 5. Session Trace 路由

当前单例 active case 状态需要收敛为按 `sessionID` 索引的轻量注册表：

```ts
type SessionTraceRegistry = {
  getOrCreate(sessionID: string, metadata: SessionMetadata): ActiveCaseTrace
  snapshot(sessionID: string, reason: string): Promise<void>
  finish(sessionID: string, result: SessionResult): Promise<TracePublication>
  finishAll(result: ProcessExitResult): Promise<TracePublication[]>
}
```

注册表只负责 Trace 状态隔离与生命周期管理，不参与 Agent 编排。业务事件仍在原有位置
被动投影到相应 `sessionID`；事件缺少 session 身份时，只能写入明确的 process-level
诊断流，不得猜测并污染某个 session。

为兼容 benchmark 的“一次进程一个 case”模式，显式 `OPENCODE_CASE_ID` 仍可作为目录
命名输入；一个进程存在多个 session 时，目录名必须再包含稳定 session 标识，避免覆盖。

## 6. 数据流与行为隔离

```text
环境变量/配置文件
        |
        v
OpenCode Config -> Provider/Model 解析 -> 原有 Agent 执行
                                          |
                                          | 被动事件副本
                                          v
                               SessionTraceRegistry
                                          |
                             Causal IR / JSON / HTML
```

Trace 代码不得：

- 修改 Config、Provider 或模型解析结果；
- 向 prompt、message、tool arguments 或 context 注入 Trace 字段；
- 因 Trace 失败而取消、重试或改写 Agent 行为；
- 将离线归因结果反馈给运行中的 Agent；
- 为获得 `read_reason` 等语义而增加额外 LLM 请求或工具调用。

所有衍生语义必须来自已发生事实的被动记录或离线投影，并标明 observed、derived 或
inferred provenance。

## 7. 错误处理

- 配置缺失、Provider 不合法或模型不存在：保持上游原有错误，不由 Trace 层兜底为
  DeepSeek 或其他默认模型；
- `{env:...}` 变量缺失：保持原生 Config 校验行为并输出可定位的配置错误；
- 某个 session 的 Trace 写入失败：记录到该 session 的 emergency/partial 产物，不影响
  其他 session，也不得覆盖 Agent 的原始退出状态；
- TUI 退出时单个 session 终态失败：继续尽力完成其他 session，并分别打印实际可用路径；
- 重复终态调用：每个 session 一次且仅一次发布回执，保证幂等。

## 8. 文档改造

根 README 将把“配置 DeepSeek”改为“配置模型与 Provider”，并说明：

1. 原生配置文件路径、项目配置和 `OPENCODE_CONFIG*`；
2. `MODEL/APIKEY/URL` 是可选的配置插值约定，不是核心专用环境变量；
3. OpenAI-compatible 配置模板和 GLM、DeepSeek 示例；
4. TUI 交互式运行与 HTTP server 是并列入口；
5. 两种模式的 Trace 开关、目录结构、session 粒度、终态回执和信号语义；
6. HTTP 请求默认使用配置模型，不再硬编码 DeepSeek。

## 9. 测试与验收

### 9.1 配置兼容性

1. `{env:MODEL}`、`{env:APIKEY}`、`{env:URL}` 经原生 Config 正确解析；
2. GLM 与 DeepSeek 的 OpenAI-compatible fixture 均能解析为正确 Provider/model；
3. 配置文件、`OPENCODE_CONFIG_CONTENT`、CLI/请求覆盖遵循既有优先级；
4. 未设置通用变量时，已有内置 Provider 和认证方式行为不变；
5. Trace 开启与关闭时，最终 Provider、模型和请求内容一致。

### 9.2 交互式 Trace

1. 单 session 多轮 TUI 对话生成一份完整 Trace；
2. 同一 TUI 进程创建或切换两个 session 时生成两份互不污染的 Trace；
3. 恢复历史 session 时继续绑定正确 Trace 身份；
4. 正常退出为每个 active session 完成落盘并打印一次回执；
5. `SIGINT`、`SIGTERM`、`SIGHUP` 生成可查看的 cancelled/partial Trace；
6. TUI alternate screen 退出后，回执在普通终端中可见；
7. HTTP session 行为和既有 benchmark case 目录保持兼容；
8. Trace 失败不改变 Agent 输出、工具副作用和进程原始退出码。

### 9.3 回归验证

- 运行 Config、Provider、TUI worker、HTTP session、CaseTrace 和 Causal IR 测试；
- 使用伪终端执行真实 TUI 退出与信号测试；
- 比较 Trace 开关前后的模型请求、工具调用和最终输出；
- 运行类型检查、格式检查、`git diff --check` 和凭据扫描；
- README 中所有命令均与 release binary 实际参数一致。

## 10. 非目标

- 不设计新的 Provider 协议或模型注册中心；
- 不把 attribution Python 运行时嵌入 opencode binary；
- 不保证 `SIGKILL` 后执行 HTML 渲染或打印回执；
- 不在本轮改变模型能力、费用预算、重试策略或 Agent 决策；
- 不让用户必须采用 `MODEL/APIKEY/URL` 这组变量名，配置可引用任意环境变量。
