<p align="center">
  <a href="https://opencode.ai">
    <picture>
      <source srcset="packages/console/app/src/asset/logo-ornate-dark.svg" media="(prefers-color-scheme: dark)">
      <source srcset="packages/console/app/src/asset/logo-ornate-light.svg" media="(prefers-color-scheme: light)">
      <img src="packages/console/app/src/asset/logo-ornate-light.svg" alt="OpenCode Logo">
    </picture>
  </a>
</p>
<p align="center">开源 AI 编程 Agent。</p>
<p align="center">
  <a href="https://opencode.ai/discord"><img alt="Discord" src="https://img.shields.io/discord/1391832426048651334?style=flat-square&label=discord" /></a>
  <a href="https://www.npmjs.com/package/opencode-ai"><img alt="npm" src="https://img.shields.io/npm/v/opencode-ai?style=flat-square" /></a>
  <a href="https://github.com/zyscoder/observable-opencode/actions/workflows/release-observable-opencode.yml"><img alt="Observable Release Status" src="https://img.shields.io/github/actions/workflow/status/zyscoder/observable-opencode/release-observable-opencode.yml?style=flat-square" /></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> |
  <a href="README.zht.md">繁體中文</a> |
  <a href="README.ko.md">한국어</a> |
  <a href="README.de.md">Deutsch</a> |
  <a href="README.es.md">Español</a> |
  <a href="README.fr.md">Français</a> |
  <a href="README.ja.md">日本語</a>
</p>

[![OpenCode Terminal UI](packages/web/src/assets/lander/screenshot.png)](https://opencode.ai)

---

## 项目简介

`observable-opencode` 是基于最新 OpenCode 基线改造的可观测版本。它保持 OpenCode 原有的模型请求、工具调用、Skill/MCP 执行、子 Agent 调度和会话行为，同时旁路记录语义 Trace，用于离线分析、根因定位和 Agent Harness 持续优化。

Trace 运行时只追加写入 segment journal；完整的 `trace.json` 和 `trace.html` 在会话结束后通过离线命令生成，不会在每个事件发生时重复构造大文件。

## 安装

### 直接安装 OpenCode

```bash
# 直接安装
curl -fsSL https://opencode.ai/install | bash

# 软件包管理器
npm i -g opencode-ai@latest        # 也可以使用 bun/pnpm/yarn
scoop install opencode             # Windows
choco install opencode             # Windows
brew install anomalyco/tap/opencode # macOS 和 Linux
sudo pacman -S opencode            # Arch Linux
paru -S opencode-bin               # Arch Linux 最新版本
mise use -g opencode               # 任意系统
nix run nixpkgs#opencode           # 或使用 github:anomalyco/opencode
```

> [!TIP]
> 安装前请移除 0.1.x 之前的旧版本。

### 安装 observable-opencode Release

从 [observable-opencode Releases](https://github.com/zyscoder/observable-opencode/releases/latest) 下载对应平台的压缩包。Release 中的可执行文件名称仍然是 `opencode`，不需要在目标 Linux 或 macOS 机器上安装 Bun。

下面是 macOS Apple Silicon 的安装示例：

```bash
VERSION=v1.18.31-observable.1
curl -fL "https://github.com/zyscoder/observable-opencode/releases/download/${VERSION}/opencode-darwin-arm64.zip" -o /tmp/opencode.zip
unzip -q /tmp/opencode.zip -d /tmp/opencode-bin
chmod +x /tmp/opencode-bin/opencode
sudo install -m 0755 /tmp/opencode-bin/opencode /usr/local/bin/opencode
opencode --version
```

Linux 使用 `.tar.gz`，Windows 使用 `.zip`。当前 Release 覆盖 macOS、Linux、Windows 的 arm64、x64、baseline 和 musl 目标。

## 模型配置

observable-opencode 支持所有 OpenAI-compatible API。使用三个通用环境变量：`MODEL`、`URL`、`APIKEY`。其中 `URL` 应填写服务的基础地址，通常需要包含 `/v1`，不要把 `/chat/completions` 再拼到 URL 中。

```bash
export MODEL="deepseek-v4-flash"
export URL="https://api.example.com/v1"
export APIKEY="replace-with-your-api-key"

export OPENCODE_CONFIG_CONTENT='{
  "model": "compatible/{env:MODEL}",
  "provider": {
    "compatible": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "OpenAI Compatible",
      "options": {
        "baseURL": "{env:URL}",
        "apiKey": "{env:APIKEY}"
      },
      "models": {
        "{env:MODEL}": {
          "id": "{env:MODEL}",
          "name": "{env:MODEL}"
        }
      }
    }
  }
}'

# 内网或受限网络环境可关闭启动时的 models.dev 拉取
export OPENCODE_DISABLE_MODELS_FETCH=1
```

这套配置适用于 DeepSeek、GLM 或其他 OpenAI-compatible 服务。更换模型时只需要修改 `MODEL`、`URL`、`APIKEY`。也可以将同样的 JSON 保存为项目配置目录中的 `opencode.json` 或 `opencode.jsonc`。`{env:VAR}` 占位符由 OpenCode 在加载配置时展开。

如果需要与普通 OpenCode 共用 session 数据库，请使用相同的系统用户和项目目录，不要额外修改 `XDG_DATA_HOME`、`XDG_STATE_HOME` 或 OpenCode 配置目录。

## 运行 observable-opencode

### 交互式运行

启动前开启语义 Trace，并为同一个逻辑任务设置稳定的 case ID：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID=my-case-001
export OPENCODE_CASE_TRACE_DIR=/tmp/opencode-traces

cd /path/to/your/project
opencode
```

恢复已有 session：

```bash
opencode -s ses_XXXXXXXXXXXXXXXX
```

按 `Ctrl-C` 退出交互界面。运行期间只会追加写入 segment journal，不会每隔几秒重新生成完整的 `trace.json` 或 `trace.html`。

### HTTP server 运行

HTTP server 使用相同的模型配置、Trace 配置和 session 数据库：

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID=http-case-001
export OPENCODE_CASE_TRACE_DIR=/tmp/opencode-traces

opencode serve --hostname 127.0.0.1 --port 4096
```

另开一个终端创建 session 并发送请求：

```bash
BASE=http://127.0.0.1:4096
PROJECT=/path/to/your/project
SESSION_ID=$(curl -fsS -X POST "$BASE/session" \
  -H "x-opencode-directory: $PROJECT" \
  -H "Content-Type: application/json" \
  -d '{}' | jq -r .id)

curl -N -X POST "$BASE/session/$SESSION_ID/prompt" \
  -H "x-opencode-directory: $PROJECT" \
  -H "Content-Type: application/json" \
  -d '{"parts":[{"type":"text","text":"Inspect the repository and summarize the build flow."}]}'
```

请求结束后停止 server。交互式运行和 HTTP 运行都会写入相同的 case 目录结构：

```text
/tmp/opencode-traces/http-case-001/
├── session.json
├── segments/<segment-id>/records.jsonl
├── segments/<segment-id>/artifacts/*
└── segments/<segment-id>/index.sqlite
```

## 生成和查看 Trace

### finalize 生成标准 Trace

退出 session 或停止 server 后，使用发布的 `opencode` 二进制执行 finalize：

```bash
CASE_DIR=/tmp/opencode-traces/my-case-001
opencode trace finalize "$CASE_DIR"
```

执行后会生成：

```text
trace.json              # Causal IR，离线归因模块的标准输入
manifest.json           # case 和 segment 清单
partial/latest.json     # 最近一次物化结果
provenance-trace.json   # provenance 投影
```

如果同一个 case 后续又运行了新的 session，可以再次执行 `finalize`，它会重新读取所有 segment 并更新统一的 `trace.json`。进程被 `SIGTERM`、`SIGINT` 或 `SIGHUP` 终止时，已经写入的 segment 仍然可以被 finalize；`SIGKILL` 无法被捕获，最终结果可能被标记为 `incomplete`，但不会伪造成功链路。

### render 生成人类可读 HTML

HTML 由离线 renderer 生成，输出路径是位置参数：

```bash
opencode trace render "$CASE_DIR" "$CASE_DIR/trace.html"
```

最终目录示例：

```text
/tmp/opencode-traces/my-case-001/
├── trace.json
├── manifest.json
├── partial/latest.json
├── provenance-trace.json
├── trace.html
├── session.json
├── segments/
└── artifacts/
```

`trace.json` 面向离线查询和根因分析，`trace.html` 面向人工查看；两者都由同一批 segment journal 生成。大段输入、输出和文件内容保存在 `artifacts/`，结构化 Trace 中只保存摘要、哈希、大小和引用。

更多 Trace 节点、Causal IR 和迁移说明见 [`docs/opencode-latest-trace.md`](docs/opencode-latest-trace.md)。

## Agent

OpenCode 内置以下 Agent，可使用 `Tab` 键切换：

- **build**：默认模式，具备完整权限，适合开发任务。
- **plan**：只读模式，适合代码分析和方案设计，默认拒绝文件修改。
- **general**：用于复杂搜索和多步骤任务的子 Agent，可在对话中使用 `@general` 调用。

更多 Agent 配置请参考 [OpenCode 文档](https://opencode.ai/docs/agents)。

## 发布流程

当前仓库的 [`release-observable-opencode`](.github/workflows/release-observable-opencode.yml) workflow 支持手动发布：

1. 打开 [Release workflow](https://github.com/zyscoder/observable-opencode/actions/workflows/release-observable-opencode.yml)。
2. 点击 **Run workflow** 并选择要发布的分支。
3. `version` 留空时，会根据当前 `packages/opencode/package.json` 版本生成 `版本号-observable.<run_number>`；也可以手动填写不带 `v` 前缀的版本号。

workflow 会构建所有目标平台、创建 GitHub Release、上传 `.tar.gz`/`.zip` 资产，并同时保留 Actions artifact。

## 桌面应用（BETA）

OpenCode 也提供桌面版应用，可从 [官方 Releases](https://github.com/anomalyco/opencode/releases) 或 [opencode.ai/download](https://opencode.ai/download) 下载。

| 平台 | 下载文件 |
| --- | --- |
| macOS Apple Silicon | `opencode-desktop-mac-arm64.dmg` |
| macOS Intel | `opencode-desktop-mac-x64.dmg` |
| Windows | `opencode-desktop-windows-x64.exe` |
| Linux | `.deb`、`.rpm` 或 `.AppImage` |

## 安装目录

安装脚本按以下优先级选择目录：

1. `$OPENCODE_INSTALL_DIR`：自定义安装目录。
2. `$XDG_BIN_DIR`：符合 XDG Base Directory 规范的目录。
3. `$HOME/bin`：用户级二进制目录。
4. `$HOME/.opencode/bin`：默认备用目录。

```bash
OPENCODE_INSTALL_DIR=/usr/local/bin curl -fsSL https://opencode.ai/install | bash
XDG_BIN_DIR=$HOME/.local/bin curl -fsSL https://opencode.ai/install | bash
```

## 参与贡献

提交 Pull Request 前请阅读 [贡献指南](./CONTRIBUTING.md)。如果基于 OpenCode 创建相关项目，请在项目 README 中明确说明与 OpenCode 官方团队没有隶属关系。

---

加入社区：[Discord](https://discord.gg/opencode) | [X.com](https://x.com/opencode)
