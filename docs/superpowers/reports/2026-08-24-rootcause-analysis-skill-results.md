# Root Cause Analysis Skill Validation Results

Date: 2026-08-24

Branch: `codex/trace-stability-integration`

Scope: Task 6 usage documentation, pressure-test isolation, and forward-test validation

## Executive Result

The deterministic Skill, fixture, query, report, and isolation contracts pass.
The current OpenCode source build discovers the canonical project Skill from
`.claude/skills/rootcause-analysis/SKILL.md`, but repository source-build runs
are discovery/provider smoke tests only. No model-generated forward report
could be evaluated in this environment because Claude is not authenticated and
the historical OpenCode source runs had no configured provider. Those attempts
also predated scored workspace isolation, so they remain explicitly unscored;
no forward result or rubric score was fabricated.

## Deterministic Validation

| Check | Result |
| --- | --- |
| Root-cause Skill fixture, query, and contract suite | PASS, 113 passed and 1 explicitly blocked integration skip (114 discovered) |
| `trace_query.py` bytecode compilation | PASS |
| Official `quick_validate.py` Skill validation | PASS, `Skill is valid!` |
| Skill canonical-location discovery in current OpenCode source build | PASS |
| Repository whitespace validation | PASS |
| Fixture immutability check | PASS |
| Documented `build-requirement` Artifact query | PASS, integrity verified |
| Seven opaque Agent workspace builds and file-tree audits | PASS |
| External-binary OpenCode wrapper synthetic cwd smoke check | PASS, unscored |
| Docker/Podman scored integration | BLOCKED, Docker daemon unavailable and Podman absent |

Commands:

```bash
python3 -m unittest discover -s tools/rootcause_skill_tests -p 'test_*.py' -v

PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache \
python3 -m py_compile \
  .claude/skills/rootcause-analysis/scripts/trace_query.py \
  tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py \
  tools/rootcause_skill_tests/pressure/run_isolated_opencode.py

UV_CACHE_DIR=/tmp/observable-opencode-uv-cache \
uv run --with pyyaml \
  python /Users/zys/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .claude/skills/rootcause-analysis

git diff --check
git diff --exit-code -- tools/rootcause_skill_tests/fixtures

python3 .claude/skills/rootcause-analysis/scripts/trace_query.py artifact \
  --trace tools/rootcause_skill_tests/fixtures/known-root/trace.json \
  --id build-requirement --max-chars 12000
```

The first direct `py_compile` attempt was blocked because the macOS system
Python tried to create bytecode cache directories under the sandboxed
`~/Library/Caches`. Setting `PYTHONPYCACHEPREFIX` to `/tmp` removed that
environment-only restriction and compilation passed.

The first `uv` attempt likewise could not write `~/.cache/uv`; after moving its
cache to `/tmp`, the validator needed PyPI access for PyYAML. The approved fetch
installed the package in the temporary environment and official validation
passed.

## OpenCode Discovery And Executable

No `opencode` executable is installed on `PATH`. The repository's current source
entry point is runnable with Bun:

```bash
XDG_DATA_HOME=/tmp/observable-opencode-rootcause-forward/opencode/data \
XDG_CACHE_HOME=/tmp/observable-opencode-rootcause-forward/opencode/cache \
XDG_CONFIG_HOME=/tmp/observable-opencode-rootcause-forward/opencode/config \
XDG_STATE_HOME=/tmp/observable-opencode-rootcause-forward/opencode/state \
bun run --conditions=browser packages/opencode/src/index.ts debug skill
```

The command returned a `rootcause-analysis` entry whose location was the
canonical project file:

```text
.claude/skills/rootcause-analysis/SKILL.md
```

The source build therefore satisfies only the Skill-discovery smoke check, not
the scored isolation gate. A first run against the default user data directory failed during a
database WAL checkpoint because the sandbox could not write that external
directory. Isolated XDG directories under `/tmp` removed this unrelated startup
restriction.

## Forward-Test Protocol

The bundle protocol first uses
`tools/rootcause_skill_tests/pressure/prepare_isolated_bundle.py` to build one
no-overwrite workspace per case. Each workspace contains exactly the selected
finalized Trace and verified Artifacts, the canonical Skill, and two
runtime-specific prompts. It contains no evaluator cases, expected outcomes,
rubric, sibling fixture, report, Git history, repository path in either prompt,
or symlink/path escape. It also replaces semantic fixture identity with a
per-run opaque ID and emits original/derived integrity provenance only to the
host-side evaluator.

An isolated cwd is only smoke isolation. The scored OpenCode protocol requires
a working Docker or Podman filesystem sandbox and an external Linux release
`OPENCODE_BIN`. The image must be an explicit immutable
`name@sha256:<64hex>` reference; the binary must match the evaluator-supplied
SHA-256 and a complete supported ELF64 header. Its x86_64/aarch64 machine field
selects the matching container platform. Before case creation, a secret-free
forced `/opt/opencode` entrypoint must self-exit zero for `--version` and emit a
plausible version. The preflight audit contains digest/version facts only.

The Agent container receives exactly two read-only bind mounts: one
opaque workspace at `/workspace` and the binary at `/opt/opencode`. It cannot
see the workspace parent, siblings, repository, batch root, audit JSONL,
evaluator mapping, or hidden rubric. Provider values are passed through an
environment-name whitelist. Strict mount construction rejects relative,
symlinked, noncanonical, comma-bearing, or control-character paths. Probe and
Agent entrypoints are forced to `/bin/sh` and `/opt/opencode`.

`OPENCODE_CONFIG_CONTENT` is parsed recursively for normalized sensitive keys.
Secret leaf values and explicit provider secret environment values are expanded
to raw, JSON/slash-escaped, URL percent/plus-encoded, and layered serialized
forms. Child stdout/stderr, wrapper errors, and final audit JSONL are scrubbed;
the child receives unchanged inputs. Malformed config fails closed in scored
mode.

Only the evaluator reads `pressure/cases.json` and `pressure/rubric.json` after
execution. The builder extracts the exact `question` but never copies the
`expected` object. Successful sandboxed outputs remain
`scored_pending_evaluator` until this
outside-workspace scoring step completes.

The attempts below are preserved historical smoke evidence. They did not use
the current isolated scored protocol and therefore remain unscored.

Prompts contained only:

- the runtime-specific Skill request: Claude Code `/rootcause-analysis`, or an
  OpenCode instruction to invoke the `skill` tool with name
  `rootcause-analysis` before analysis;
- the absolute finalized fixture `trace.json` path;
- the question from `pressure/cases.json`;
- the analysis-only instruction.

The hidden `expected` objects in `pressure/cases.json` were reserved for the
evaluator and were never included in an Agent prompt.

The exact expanded commands, separated stdout/stderr, timing, raw self-exit
return code, timeout state, and termination action are in
[the raw attempt appendix](2026-08-24-rootcause-analysis-skill-raw-attempts.md).

### Claude Code

The auditable normal-runtime attempt used `/rootcause-analysis`. It exited with
status `1`; stdout was:

```text
Not logged in · Please run /login
```

stderr was empty. Authentication is a process-wide prerequisite, so no Claude
model request was sent. Repeating the same unauthenticated call for every
fixture would not exercise a different Skill path.

### OpenCode

All seven cases were individually submitted through the repository source build
in the preserved round-2 smoke run.
Every prompt instructed the Agent to invoke the `skill` tool with name
`rootcause-analysis` before analysis. Remote `models.dev` refresh and the file
watcher were disabled only to keep the provider preflight bounded:

```bash
OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER=1 \
OPENCODE_DISABLE_MODELS_FETCH=1 \
XDG_DATA_HOME=/tmp/rootcause-task6-fix/opencode/<fixture>/data \
XDG_CACHE_HOME=/tmp/rootcause-task6-fix/opencode/<fixture>/cache \
XDG_CONFIG_HOME=/tmp/rootcause-task6-fix/opencode/<fixture>/config \
XDG_STATE_HOME=/tmp/rootcause-task6-fix/opencode/<fixture>/state \
bun run --conditions=browser packages/opencode/src/index.ts run \
  --print-logs --log-level ERROR --format json \
  '开始分析前，先调用 skill 工具并传入 name=rootcause-analysis。Trace: <fixture>/trace.json. Question: <question>. ...'
```

Every command produced empty stdout and stderr beginning with:

```text
error=no providers found cause=Error: no providers found
```

The captured stacks place the observed error in `Provider.defaultModel`, before
the Agent could invoke the Skill, execute a Trace query, or produce a model
result. None of the seven processes exited by itself within the documented
8-second timeout. Each record therefore has `timed_out=true` and
`raw_returncode=null`. The legacy wrapper recorded that it requested SIGINT and
that communication later completed, but it did not independently check whether
the signal was delivered. Those records therefore establish neither signal
delivery nor termination cause and must not be reported as an OpenCode self-exit
code.

The current runner accepts `--batch-root` or
`ROOTCAUSE_FORWARD_BATCH_ROOT`, otherwise creates a unique timestamp/PID/UUID
root, and refuses to overwrite existing directories. It requires
`--opencode-bin` or `OPENCODE_BIN`, `--opencode-sha256`, and a pinned
`--container-image`; it completes release verification and container preflight
before building each opaque workspace. It records signal attempts separately
from successful operating-system
signal calls, records post-signal state and bounded final cleanup, and
continues after per-case `wrapper_error`. The earlier one-second `known-root`
spot check and all prior local cwd runs are smoke only and are not scored Agent
results. The current machine has a Docker CLI but cannot access its daemon
socket; Podman is absent. Therefore the scored adversarial probe and Agent run
are explicitly blocked rather than reported as passed.

## Case Matrix

| Fixture | Historical Claude smoke | Historical OpenCode source smoke | Rubric evaluation |
| --- | --- | --- | --- |
| `known-root` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `ambiguous` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `skill-omission` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `context-contamination` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `control-flow-change` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `tool-failure-misreported` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |
| `wrong-answer` | Blocked: not logged in | Provider error observed; legacy 8 s timeout; signal delivery unknown | Not evaluated |

No raw Agent outcome exists to compare with the RED baseline. Consequently the
11 pressure-rubric items, including introduction-versus-propagation accuracy,
competing-hypothesis testing, resolvable evidence citations, and ambiguous-case
restraint, remain **not evaluated** rather than passed or failed.

## Observed Guarantees

- The seven canonical fixtures remained unchanged.
- All seven opaque workspace file trees were audited against exact allowlists; no hidden evaluator data, semantic fixture identity, or repository symlink was present.
- No forward-test output was written into a fixture, Trace bundle, or analyzed
  project.
- No Trace capture, OpenCode runtime, existing Python attribution module, or
  observed Agent behavior was modified for Task 6.
- The README now distinguishes the flexible Agent Skill from the separate,
  paused Python automation path and documents the analysis-only boundary.

## Remaining Validation

After configuring a provider, a hash-verified Linux release OpenCode binary, a
digest-pinned container image, and an available Docker or Podman daemon, build
all seven opaque workspaces, run them through
the scored container mode, and
let only the external evaluator score the captured JSON and Markdown against
`tools/rootcause_skill_tests/pressure/rubric.json`. The key acceptance checks
remain:

1. `known-root` identifies `node:dec_1` or a stronger evidence-backed root and
   does not blame the successful GCC execution.
2. `ambiguous` remains `inconclusive`.
3. `skill-omission` explains the missing expected action without inventing an
   invocation node.
4. The four remaining cases locate their question-specific semantic
   introduction points instead of collapsing into generic missing
   verification.
5. Every report is evidence-linked, analysis-only, and includes structured
   recommendations without applying them.

No proposed change was applied to the analyzed system; all recommendations
require separate review and execution.
