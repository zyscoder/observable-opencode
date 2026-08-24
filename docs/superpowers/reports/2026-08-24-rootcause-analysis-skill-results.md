# Root Cause Analysis Skill Validation Results

Date: 2026-08-24

Branch: `codex/trace-stability-integration`

Scope: Task 6 usage documentation and forward-test validation

## Executive Result

The deterministic Skill, fixture, query, and report contracts pass. The current
OpenCode source build discovers the canonical project Skill from
`.claude/skills/rootcause-analysis/SKILL.md`. No model-generated forward report
could be evaluated in this environment because Claude is not authenticated and
OpenCode has no configured provider. These are explicit environment blockers;
no forward result or rubric score was fabricated.

## Deterministic Validation

| Check | Result |
| --- | --- |
| Root-cause Skill fixture, query, and contract suite | PASS, 84/84 |
| `trace_query.py` bytecode compilation | PASS |
| Official `quick_validate.py` Skill validation | PASS, `Skill is valid!` |
| Skill canonical-location discovery in current OpenCode source build | PASS |
| Repository whitespace validation | PASS |
| Fixture immutability check | PASS |
| Documented `build-requirement` Artifact query | PASS, integrity verified |

Commands:

```bash
python3 -m unittest discover -s tools/rootcause_skill_tests -p 'test_*.py' -v

PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache \
python3 -m py_compile \
  .claude/skills/rootcause-analysis/scripts/trace_query.py

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

The source build therefore satisfies the Skill-discovery part of the forward
test. A first run against the default user data directory failed during a
database WAL checkpoint because the sandbox could not write that external
directory. Isolated XDG directories under `/tmp` removed this unrelated startup
restriction.

## Forward-Test Protocol

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

All seven cases were individually submitted through the current source build
in the preserved round-2 run.
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

The hardened wrapper now accepts `--batch-root` or
`ROOTCAUSE_FORWARD_BATCH_ROOT`, otherwise creates a unique timestamp/PID/UUID
root, and refuses to overwrite existing directories. It records signal
attempts separately from successful operating-system signal calls,
post-signal state, bounded final cleanup, and per-case `wrapper_error` while
continuing the remaining cases. A fresh `known-root` spot check exercised this
schema with a one-second timeout. Its observed post-signal return code is
reported only as post-signal state; no signal is asserted as the cause.

## Case Matrix

| Fixture | Claude | OpenCode | Rubric evaluation |
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
- No forward-test output was written into a fixture, Trace bundle, or analyzed
  project.
- No Trace capture, OpenCode runtime, existing Python attribution module, or
  observed Agent behavior was modified for Task 6.
- The README now distinguishes the flexible Agent Skill from the separate,
  paused Python automation path and documents the analysis-only boundary.

## Remaining Validation

After authenticating Claude or configuring an OpenCode provider, rerun the
same seven prompts and score the actual JSON and Markdown against
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
