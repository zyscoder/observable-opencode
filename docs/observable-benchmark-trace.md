# Observable Opencode Benchmark Trace 使用说明

本文说明如何在 Linux 上安装改造后的 observable opencode，如何替换现有 `opencode` 命令，以及如何在 benchmark case 执行时生成结构化 trace 和可视化报告。

## 1. 获取代码

```bash
git clone https://github.com/zyscoder/observable-opencode.git
cd observable-opencode
```

推荐固定到已验证分支：

```bash
git checkout codex/obversable-opencode-trace
```

## 2. 安装基础依赖

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

## 3. 运行 observable opencode

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

## 4. 替换现有 opencode

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

## 5. 配置 trace 环境变量

开启 case trace：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID="T1-001"
export OPENCODE_CASE_TRACE_DIR="/data/evo-bench/opencode-traces"
```

可选配置：

```bash
# 默认只记录摘要、长度、hash 和 preview。设置为 1 后记录完整内容，谨慎用于私域代码仓。
export OPENCODE_CASE_TRACE_FULL_CONTENT=0

# 单字段 preview 最大长度，默认 2048。
export OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH=4096
```

每个 case 会生成：

```text
$OPENCODE_CASE_TRACE_DIR/
  T1-001/
    events.jsonl
    trace.json
    trace.html
```

其中：

- `events.jsonl`：运行时增量事件流，即使 case 中途失败也能保留已发生事件。
- `trace.json`：case 执行结束后的结构化汇总。
- `trace.html`：离线可视化报告，包含组件瀑布图、token 汇总、工具调用和错误面板。

## 6. 运行 benchmark case

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

## 7. 查看 trace

结构化检查：

```bash
jq '{case_id, status, duration_ms, token_usage, spans: (.spans | length), events: (.events | length), errors: (.errors | length)}' \
  /data/evo-bench/opencode-traces/T1-001/trace.json
```

打开可视化报告：

```bash
xdg-open /data/evo-bench/opencode-traces/T1-001/trace.html
```

无 GUI 服务器可以将 `trace.html` 下载到本地浏览器打开。

## 8. Trace 覆盖范围

当前 trace 覆盖：

- `run`：CLI case 入口、session 选择、权限自动处理、case 结束状态。
- `prompt/context`：prompt parts 解析、文件/引用/agent 解析、上下文消息数、系统提示数量、工具数量。
- `llm`：模型、provider、agent、流式事件、LLM 调用耗时。
- `processor`：step start/finish、token usage、tool call、compact/continue/stop 决策。
- `tool`：通用工具输入输出摘要、耗时、错误。
- `skill`：skill 名称、目录、权限确认、加载结果。
- `task`：subagent 类型、子 session、任务结果。
- `mcp`：MCP 连接、tools/list、tool/call、错误。

## 9. 常见问题

### 没有生成 trace

确认三个变量同时存在：

```bash
echo "$OPENCODE_CASE_TRACE"
echo "$OPENCODE_CASE_ID"
echo "$OPENCODE_CASE_TRACE_DIR"
```

`OPENCODE_CASE_TRACE` 必须是 `1`、`true`、`yes` 或 `on`。

### trace.html 为空或缺少工具调用

先看 `trace.json` 中是否存在 spans：

```bash
jq '.spans[] | {component, operation, name, status, duration_ms}' \
  "$OPENCODE_CASE_TRACE_DIR/$OPENCODE_CASE_ID/trace.json"
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
