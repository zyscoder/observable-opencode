# 最新版 OpenCode 语义 Trace

本分支基于 upstream OpenCode `dev` 的 `ebb7b76eca82342642c78645109e865614533827`，版本为 `1.18.31`。语义 Trace 以旁路方式接入最新版本的真实会话路径，不改变模型请求、工具选择、Skill/MCP 执行、子 Agent 调度或会话控制流。

## 运行时记录

开启记录：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID=my-case
export OPENCODE_CASE_TRACE_DIR=/tmp/opencode-traces
```

运行时只追加写入：

```text
/tmp/opencode-traces/my-case/
├── session.json
├── segments/<segment-id>/records.jsonl
├── segments/<segment-id>/artifacts/*
└── segments/<segment-id>/index.sqlite
```

每个 LLM 回合、工具执行和上下文压缩都是独立 segment。长 session 不需要把完整历史保存在进程内，也不会在每个事件发生时生成完整 `trace.json` 或 HTML。

记录内容包括：

- 用户 prompt 的 admitted 形态及其外置 artifact；
- 上下文准备、压缩选择、摘要提示、Skill/MCP 指令和工具集合；
- provider request transformation 及最终模型调用；
- LLM reasoning/text/tool 事件、token usage、provider error；
- Skill、Task/subagent、MCP 和普通工具的输入、输出、metadata、错误和执行结果；
- 子 Agent 的 session、父子关系和独立运行片段。

大文本写入 `artifacts/`，`records.jsonl` 只保留哈希、大小、预览和 artifact 引用。

## 退出和恢复

`SIGINT`、`SIGTERM`、`SIGHUP` 到达时，活动 segment 会先落盘为 `cancelled`，然后保持原有进程退出语义。`SIGKILL` 无法被进程捕获；此时保留的 running segment 会由 finalize 按最后一个有效 journal 前缀恢复为 incomplete，而不会伪造成功。

## 生成标准 Trace

退出后，在仓库根目录执行：

```bash
bun --cwd packages/opencode ./src/index.ts trace finalize /tmp/opencode-traces/my-case
```

成功后产生：

```text
/tmp/opencode-traces/my-case/trace.json       # Causal IR，归因模块的标准输入
/tmp/opencode-traces/my-case/manifest.json
/tmp/opencode-traces/my-case/partial/latest.json
/tmp/opencode-traces/my-case/provenance-trace.json
```

如果进程被强制中断，仍可再次执行 `finalize`。它会重新读取所有有效 segment，更新根 `trace.json`；不完整时会明确标记 `incomplete`，不会把未完成链路当成成功结果。

## 生成人类可读 HTML

HTML 由独立的离线 renderer 生成，不属于 OpenCode 运行时：

```bash
bun --cwd packages/opencode ./src/index.ts trace render \
  /tmp/opencode-traces/my-case \
  --output /tmp/opencode-traces/my-case/trace.html
```

也可以直接使用 renderer：

```bash
bun --cwd packages/trace-renderer ./src/cli.ts render \
  /tmp/opencode-traces/my-case \
  --output /tmp/opencode-traces/my-case/trace.html
```

`trace.json` 面向离线归因、查询和机器处理；`trace.html` 面向人工查看，二者都来自同一批 segment journal，不是两套独立事实。

## 开发验证

```bash
bun --cwd packages/core typecheck
bun --cwd packages/opencode typecheck
bun --cwd packages/trace-renderer typecheck
bun --cwd packages/core test test/observability/latest-trace.test.ts test/observability/trace-runtime.test.ts test/observability/trace-materializer.test.ts
bun --cwd packages/trace-renderer test
```
