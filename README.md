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
  <a href="https://github.com/zyscoder/observable-opencode/actions/workflows/release-observable-opencode.yml"><img alt="Observable release status" src="https://img.shields.io/github/actions/workflow/status/zyscoder/observable-opencode/release-observable-opencode.yml?style=flat-square" /></a>
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

### Observable Releases

This fork publishes the latest OpenCode baseline with semantic trace instrumentation from the
[`release-observable-opencode`](.github/workflows/release-observable-opencode.yml) workflow.

To publish the current branch manually:

1. Open the [release workflow](https://github.com/zyscoder/observable-opencode/actions/workflows/release-observable-opencode.yml).
2. Select **Run workflow** and choose the branch to release.
3. Leave `version` empty to generate `1.18.31-observable.<run_number>`, or provide a version without the leading `v`.

The workflow creates a GitHub Release and uploads executable archives for Linux, macOS, and Windows targets.
The same archives are also available as workflow artifacts. Release assets use `.tar.gz` on Linux and `.zip` on
macOS and Windows.

The current release source is the repository default branch. The generated executable is named `opencode` inside
each platform archive and includes the complete semantic trace instrumentation described in
[`docs/opencode-latest-trace.md`](docs/opencode-latest-trace.md).

### Observable OpenCode Usage

The release binary keeps the normal OpenCode command name and behavior. The repository is named
`observable-opencode`; after extracting an archive, the executable itself is named `opencode`.

#### Install a release binary

Open the [latest observable release](https://github.com/zyscoder/observable-opencode/releases/latest), then choose the
archive for your platform. For example:

```bash
# macOS Apple Silicon
VERSION=v1.18.31-observable.1
curl -fL "https://github.com/zyscoder/observable-opencode/releases/download/${VERSION}/opencode-darwin-arm64.zip" -o /tmp/opencode.zip
unzip -q /tmp/opencode.zip -d /tmp/opencode-bin
chmod +x /tmp/opencode-bin/opencode
sudo install -m 0755 /tmp/opencode-bin/opencode /usr/local/bin/opencode
opencode --version
```

Linux archives use `.tar.gz`, and Windows archives use `.zip`. The archive contains the executable directly; no Bun
installation is required on the target machine.

#### Configure any OpenAI-compatible service

The configuration uses the three generic environment variables `MODEL`, `URL`, and `APIKEY`. `URL` is the provider
base URL and normally includes `/v1`; do not append `/chat/completions` yourself.

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

# Optional for an offline or restricted network: use only the configured provider.
export OPENCODE_DISABLE_MODELS_FETCH=1
```

The same JSON can be saved as `opencode.json` or `opencode.jsonc` in the project configuration directory instead of
using `OPENCODE_CONFIG_CONTENT`. The `{env:VAR}` placeholders are expanded by OpenCode. This setup works with
DeepSeek, GLM, or another service that exposes an OpenAI-compatible API; only `MODEL`, `URL`, and `APIKEY` change.

OpenCode uses its normal data and session database locations. Do not override `XDG_DATA_HOME`, `XDG_STATE_HOME`, or
the OpenCode config directory if the observable binary must share sessions with a regular OpenCode installation.

#### Interactive sessions

Enable semantic tracing before starting the session. Use a stable case ID when a session may be resumed later:

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID=my-case-001
export OPENCODE_CASE_TRACE_DIR=/tmp/opencode-traces

cd /path/to/your/project
opencode
```

To resume a normal OpenCode session, use the same command-line option as upstream OpenCode:

```bash
opencode -s ses_XXXXXXXXXXXXXXXX
```

Press `Ctrl-C` to leave the interactive UI. The runtime keeps append-only segment journals while the session is
running; it does not repeatedly rebuild the large `trace.json` or `trace.html` snapshot.

#### HTTP server sessions

The HTTP server uses the same tracing environment and the same session database:

```bash
export OPENCODE_CASE_TRACE=1
export OPENCODE_CASE_ID=http-case-001
export OPENCODE_CASE_TRACE_DIR=/tmp/opencode-traces

opencode serve --hostname 127.0.0.1 --port 4096
```

In another terminal, create a session and send a prompt. The directory header tells the server which project should
own the session:

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

Stop the server after the request completes. Both interactive and HTTP execution write the same case layout:

```text
/tmp/opencode-traces/http-case-001/
├── session.json
├── segments/<segment-id>/records.jsonl
├── segments/<segment-id>/artifacts/*
└── segments/<segment-id>/index.sqlite
```

#### Finalize and inspect a trace

`trace.json` and `trace.html` are generated after execution, by an explicit offline step:

```bash
CASE_DIR=/tmp/opencode-traces/my-case-001

# Rebuild the canonical Causal IR. Safe to run again after a resumed session.
opencode trace finalize "$CASE_DIR"

# Generate the human-readable HTML view. The output path is positional.
opencode trace render "$CASE_DIR" "$CASE_DIR/trace.html"
```

The case directory then contains `trace.json` for offline analysis, `manifest.json`, `partial/latest.json`,
`provenance-trace.json`, and `trace.html` for human inspection. Large inputs and outputs remain in `artifacts/` and
are referenced from the structured trace. A later session with the same case ID can append new segments; running
`finalize` again updates the unified trace.

`SIGINT`, `SIGTERM`, and `SIGHUP` close the active segment before the process exits. `SIGKILL` cannot be caught, so
finalize may report an incomplete segment while preserving all journal records already written to disk.

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

---

**Join our community** [Discord](https://discord.gg/opencode) | [X.com](https://x.com/opencode)
