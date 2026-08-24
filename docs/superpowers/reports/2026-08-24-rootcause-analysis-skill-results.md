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
| Root-cause Skill fixture, query, and contract suite | PASS, 79/79 |
| `trace_query.py` bytecode compilation | PASS |
| Official `quick_validate.py` Skill validation | PASS, `Skill is valid!` |
| Skill canonical-location discovery in current OpenCode source build | PASS |
| Repository whitespace validation | PASS |
| Fixture immutability check | PASS |

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

- the explicit `$rootcause-analysis` invocation;
- the absolute finalized fixture `trace.json` path;
- the question from `pressure/cases.json`;
- an output prefix under `/tmp` and the analyzed project root;
- the analysis-only instruction.

The hidden `expected` objects in `pressure/cases.json` were reserved for the
evaluator and were never included in an Agent prompt.

### Claude

Both bare and normal non-interactive startup were attempted. The exact result
was:

```text
Not logged in · Please run /login
```

Authentication is a process-wide prerequisite, so no Claude model request was
sent for any of the seven fixtures. Repeating the same unauthenticated call
would not test a different Skill behavior.

### OpenCode

All seven cases were individually submitted through the current source build.
Remote `models.dev` refresh was disabled to keep provider validation bounded:

```bash
OPENCODE_DISABLE_MODELS_FETCH=1 \
XDG_DATA_HOME=/tmp/observable-opencode-rootcause-forward/opencode/data \
XDG_CACHE_HOME=/tmp/observable-opencode-rootcause-forward/opencode/cache \
XDG_CONFIG_HOME=/tmp/observable-opencode-rootcause-forward/opencode/config \
XDG_STATE_HOME=/tmp/observable-opencode-rootcause-forward/opencode/state \
bun run --conditions=browser packages/opencode/src/index.ts run \
  --print-logs --log-level ERROR --format json \
  'Use $rootcause-analysis. Trace: <fixture>/trace.json. Question: <question>. ...'
```

Every case stopped before model inference with the same exact server error:

```text
Error: no providers found
```

The failure originated while selecting `Provider.defaultModel`, before the
Skill could execute a query or produce a report.

## Case Matrix

| Fixture | Claude | OpenCode | Rubric evaluation |
| --- | --- | --- | --- |
| `known-root` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `ambiguous` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `skill-omission` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `context-contamination` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `control-flow-change` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `tool-failure-misreported` | Blocked: not logged in | Blocked: no providers found | Not evaluated |
| `wrong-answer` | Blocked: not logged in | Blocked: no providers found | Not evaluated |

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
