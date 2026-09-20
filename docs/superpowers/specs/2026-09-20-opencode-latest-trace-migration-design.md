# OpenCode 最新基线语义 Trace 迁移设计

## 目标

将当前 Observable OpenCode 基于旧版 OpenCode 1.14.48 的完整语义 Trace、分段持久化、Causal IR、离线 finalize/render 和 session 恢复能力迁移到上游最新 `dev` 基线，同时保持 OpenCode 原有 Agent 行为不变。

上游基线：`anomalyco/opencode` `dev`，提交 `ebb7b76eca82342642c78645109e865614533827`。

## 背景与约束

当前版本的 Trace 实现集中在旧版 `packages/opencode/src/observability/`，而最新上游已将主要运行时迁移到 `packages/core`，并重构了 session、runner、数据库、TUI 和 CLI。旧版 Trace 不能直接 cherry-pick 到新版。

必须满足以下约束：

1. Trace 只做被动记录，不向 Agent、LLM、Tool、Skill 或 MCP 注入反馈。
2. 不改变原有消息、上下文、工具选择、执行顺序、重试、压缩和退出行为。
3. 原始事实以 append-only segment journal 持久化；完整 `trace.json` 只在离线 finalize 阶段生成。
4. 长 session 不能因累计完整 Trace 或 HTML 快照导致 Agent worker 内存持续增长。
5. 旧版已发布 Trace 的核心语义能够继续被根因分析 Skill 和离线工具读取。
6. Trace 失败只能降低可观测性，不能导致 Agent 主流程失败。

## 目标架构

采用新版 OpenCode 主体加独立 Trace 适配层：

```text
OpenCode core/session/runner
        |
        | passive observation hooks
        v
Trace runtime adapter
        |
        +--> segment journal + artifacts
        +--> session manifest / continuation
        +--> offline Causal IR materializer
        +--> finalize / render CLI
        v
trace.json / partial/latest.json / trace.html
```

Trace runtime 负责事件收集、字段裁剪、artifact 外置、segment 生命周期和安全落盘；Trace materializer 负责读取 journal、恢复跨 segment 顺序、构建 Causal IR、发布派生文件。两者不参与 Agent 决策。

## 代码映射

### Trace 内核

旧版以下模块迁移到新版 `packages/core/src/observability/`，必要时拆分以适配新版 Effect 和数据库 API：

- `case-trace.ts`：语义事件收集和组件级记录
- `case-trace-session.ts`：logical case 与 session continuation
- `trace-segment.ts`：append-only segment journal、锁和恢复
- `causal-ir.ts`：Causal IR 类型、关系和规范化
- `causal-ir-runtime-store.ts`：运行时 durable store
- `trace-semantic-contract.ts`：record、relation 和字段契约
- `streaming-json-writer.ts`：有界 JSONL 写入
- `repository-snapshot.ts`：仓库状态快照
- `trace-materializer.ts`：离线 materialization
- `trace-publication.ts`：派生文件原子发布

### 新版插装边界

插装不再依赖旧版函数名，而绑定新版稳定职责：

- `packages/core/src/session/prompt.ts`：用户请求、prompt assembly、任务循环
- `packages/core/src/session/runner/`：LLM turn、decision、tool loop、subagent
- `packages/core/src/session/compaction.ts`：压缩前后上下文及算法事实
- `packages/core/src/session/message.ts` 和 `message-v2.ts`：消息与 message part 生命周期
- `packages/core/src/tool.ts`、工具实现和 tool registry：Tool 输入、输出、错误和文件影响
- `packages/core/src/plugin/skill.ts`、instruction context：Skill 暴露、加载和使用事实
- `packages/core/src/mcp/`：MCP server、调用和返回事实
- `packages/core/src/session/event.ts`、projectors：事件与 Agent 生命周期边界
- `packages/opencode/src/cli/cmd/tui/`、`run/`、`serve/`：TUI、HTTP、signal 和退出收尾

### CLI 与离线工具

保留并适配以下用户接口：

```text
observable-trace finalize <logical-case-dir>
observable-trace render <logical-case-dir-or-trace>
```

最终 logical case 目录保持：

```text
<case-dir>/
├── session.json
├── segments/
├── trace.json
├── manifest.json
├── partial/latest.json
├── provenance-trace.json
├── legacy-trace.json
└── trace.html
```

## 语义覆盖

至少恢复以下可归因语义：

1. 用户任务、约束、验收目标和任务编排结果。
2. 上下文来源、筛选、转换、压缩算法、压缩前后摘要和传递给 LLM 的最终 message。
3. LLM 请求、响应、token、耗时、模型、停止原因和 turn 关联。
4. Agent decision/action，包括工具选择、调用理由、预期产物和实际 action。
5. Tool、Skill、MCP 的发现、加载、输入、输出、错误、调用链和嵌套关系。
6. Subagent 的创建、父子关系、委托任务、返回摘要和父 Agent 消费结果。
7. 文件读取、编辑、删除、生成、验证以及对应的 source artifact。
8. 重试、失败、取消、压缩、续跑和最终响应。

## 兼容策略

- 新版运行时使用新的 OpenCode 数据库和 session API，不直接复用旧版 Drizzle schema。
- Trace 数据库/文件格式独立于 OpenCode session 数据库。
- 新版 Trace 维持旧版 `trace.json` 的 Causal IR 核心字段，并增加 `producer.version`、`trace_schema_version` 和 source mapping。
- 对旧版 Trace 提供读取兼容，不要求旧版 Trace 重新采集。
- 旧版 legacy projection 继续生成，方便已有归因工具过渡。
- 任何不确定的新版事件先记录为 `observation`，不得伪造正式因果关系。

## 生命周期与故障处理

- 正常退出、`SIGINT`、`SIGTERM`、`SIGHUP`：先关闭 journal，再由独立 materializer 尝试 finalize。
- `SIGKILL`、OOM、断电：保留已经 fsync 的 journal；后续手动 finalize 将未闭合 segment 标记为 `interrupted_unfinalized`。
- materializer 失败不阻断 Agent 退出，并打印可恢复的 case directory。
- finalize 发现 generation 在构建期间变化时拒绝发布，用户停止 session 后重试。
- artifact 使用外置文件存储，Trace JSON 只保留引用、摘要、大小和 hash。

## 验收标准

### 编译与基础回归

- 上游最新基线通过核心 typecheck。
- OpenCode 原有 session、TUI、HTTP、Tool、MCP、Skill、Compaction 测试不因 Trace 改动回归。
- Trace 关闭时，运行时行为与上游基线等价。

### Trace 回归

- 交互式 TUI session 退出后生成 root `trace.json`。
- HTTP session 删除或 server 关闭后生成 root `trace.json`。
- 同一 session 续跑会增加 segment，并可通过再次 finalize 更新统一 Trace。
- `SIGINT`、`SIGTERM` 保留有效语义；`SIGKILL` 后可从 journal 恢复。
- Tool、Skill、MCP、Subagent、Compaction、LLM 请求均有可定位语义事实。
- 旧版 Trace 读取、归因 CLI 和 rootcause-analysis Skill 继续工作。
- 新版最终 Trace 能被 `trace_query.py validate` 验证，并可被 renderer 打开。

### 行为无损

- 开启 Trace 前后，发送给 LLM 的 message 内容、Tool 输入、Tool 输出和 Agent action 序列一致。
- Trace 写入异常不得改变 Agent 结果，只记录 `observability_degraded`。
- 不在运行时生成完整 `trace.json` 或 HTML 快照。

## 实施阶段

1. 新版基线与 Trace 内核移植：先让 segment、manifest、finalize 跑通。
2. 运行时插装迁移：按 session、context、LLM、action、tool、skill、MCP、subagent、exit 顺序恢复语义。
3. CLI、renderer、兼容投影、旧 Trace 读取和发布 workflow 迁移。
4. 复杂 benchmark、信号、续跑和行为无损回归。

每个阶段都必须有独立测试和可运行产物，不能等全部插装完成后才验证。
