# Task 7 Report: End-to-End Regression, Documentation, and Release Readiness

Date: 2026-08-16

Status: **DONE_WITH_CONCERNS**. All Task 7 and focused bounded-runtime checks pass. The
remaining concerns are the explicitly excluded CaseTrace positional assertion, unrelated
pre-existing `packages/opencode` typecheck errors, and environment-sensitive host clock
discontinuities described below.

## RED Evidence

The Task 7 coverage was introduced test-first.

### Runner semantic assertions

```text
cd packages/opencode
bun test test/observability/stress-cases/stress-cases.test.mjs \
  test/observability/benchmark-cases/featurebench-cases.test.mjs \
  --timeout 30000 -t 'semantic-category probe'
```

Initial outcome: **0 pass, 2 fail**. Both runners lacked the exported
`assertTraceSemanticCategories` interface and CLI probe path.

### Passive-equivalence fields

```text
cd packages/opencode
bun test test/tool/semantic-observability.test.ts --timeout 30000 \
  -t 'production-projected passive tool behavior'
```

Initial outcome: **0 pass, 1 fail** because the deterministic fixture did not expose
`fileHashes`, `toolCalls`, or `sessionRows` for comparison.

### Real TUI process signals

```text
cd packages/opencode
bun test test/cli/tui/process-trace-e2e.test.ts --timeout 30000
```

Initial outcome: **0 pass, 1 fail** while waiting for the not-yet-implemented real-process
fixture ready file. This replaced Task 3's label-only normal/Ctrl-C evidence with actual
process `SIGINT` and `SIGTERM` delivery and root `trace.json` assertions.

### SIGKILL, resume, retry, and legacy recovery

```text
cd packages/opencode
bun test test/observability/task-7-e2e.test.ts --timeout 30000 \
  -t 'SIGKILL evidence'
```

Initial outcome: **0 pass, 1 fail** while waiting for the not-yet-implemented fixture's
durability handshake.

### Release-readiness regression alignment

The first combined focused run reported **165 pass, 4 fail** in
`case-trace-runtime.test.ts`. Three failures were stale pre-segmentation test assumptions:
the fixture wrote statistics into the physical segment but read them from the logical
root, and two fault-injection tests expected failed segment-local snapshots to prevent a
valid root reconstruction from durable journals. The fourth was a host clock jump during
an otherwise passing sanitizer test. After changing only the stale tests, the four exact
cases passed **4/4**, and the complete runtime suite passed **28/28**.

## Files And Behavior Changed

- Added `test/cli/tui/process-trace-e2e.test.ts` plus a real parent process and Worker
  fixture. Normal exit, real `SIGINT`, and real `SIGTERM` each run production TUI shutdown,
  isolated materialization, publication, and assert root `trace.json`.
- Added `test/observability/task-7-e2e.test.ts` plus a deterministic process fixture. It
  covers a real `SIGKILL`, immutable journal/index/artifact hashes, same-session resume,
  long-context compaction, Tool/Skill/MCP/Subagent/lifecycle/response semantics, a forced
  materialization failure, successful retry, and legacy flat-root recovery.
- Added `test/observability/semantic-categories.mjs` and runner `--semantic-probe` modes.
  Real stress and FeatureBench result paths now fail when required semantic categories are
  absent and record/print the categories they observed.
- Expanded the existing production-projection passive fixture to compare Agent-visible
  model messages, tool calls, file hashes, session rows, and exit code across disabled,
  enabled, and unavailable tracing. Deterministic test-only part IDs remove irrelevant ID
  randomness.
- Made runtime and materializer RSS measurements visible in their acceptance-test output.
- Updated three pre-existing Task 6 regression assertions to distinguish physical segment
  paths from the logical root and to accept root recovery from an immutable journal after
  a segment-local projection failure.
- Rewrote the README runtime sections for immutable segments, one logical Trace, automatic
  and manual finalization, honest SIGKILL recovery, same-session resume, printed locations,
  memory defaults and limits, TUI and HTTP operation, renderer behavior, legacy flat input,
  and the root-Trace/logical-directory attribution rule.
- No production runtime source file changed. All changes are tests, deterministic fixtures,
  runner release gates, measurement output, or documentation.

## End-To-End Outcomes

Final Task 7 matrix:

```text
cd packages/opencode
bun test \
  test/cli/tui/process-trace-e2e.test.ts \
  test/cli/tui/thread.test.ts \
  test/cli/tui/worker-trace.test.ts \
  test/cli/tui/trace-materializer-process.test.ts \
  test/observability/task-7-e2e.test.ts \
  test/tool/semantic-observability.test.ts \
  test/observability/stress-cases/stress-cases.test.mjs \
  test/observability/benchmark-cases/featurebench-cases.test.mjs \
  --timeout 1200000
```

Observed: **46 pass, 0 fail, 138 expectations**.

The process assertions observed:

| Scenario | Process result | Durable/materialized result |
| --- | --- | --- |
| Normal TUI exit | exit `0` | root `trace.json`, publication receipt, completed segment |
| TUI `SIGINT` | exit `130` | root `trace.json`, publication receipt, marker retained |
| TUI `SIGTERM` | exit `143` | root `trace.json`, publication receipt, marker retained |
| Runtime `SIGKILL` | nonzero signal exit | no immediate cleanup claim; prior valid journal/artifact/index bytes retained |
| Same-session continuation | exit `0` | first segment `interrupted_unfinalized`, second `completed`, explicit continuation edge |
| First materialization | nonzero forced failure | no root `trace.json`; prior evidence hashes unchanged |
| Materialization retry | exit `0` | unified root `trace.json` with both segments and required semantics |
| Legacy flat finalization | exit `0` | root trace recovered; original `records.jsonl` SHA-256 unchanged; no `session.json` added |

The resumed trace contains `context.compaction`, `tool.call`, `tool.result`, `skill.load`,
`mcp.call`, `subagent.call`, `agent.lifecycle`, and `response.output` records plus artifacts
and the `continued_from`/`run.continuation` relationship.

## Passive Equivalence

The same deterministic production-projection fixture was run with tracing disabled,
enabled, and enabled against an invalid trace root. The comparison fields were:

- `agentVisibleMessages`: exact deep equality, including Tool calls and Tool results;
- `toolCalls`: exact call ID, tool name, and input equality;
- `fileHashes`: exact SHA-256 equality for `agent-output.txt`,
  `c439d57fefd2c910fae83e0700868e21302e1c959dd4fa02ada1b8ca96f92838`;
- `sessionRows`: exact assistant-message and three deterministic Tool-part rows;
- `exitCode`: `0` for disabled, enabled, and unavailable storage;
- callback inputs, metadata, permission requests, error class/prototype identity, projected
  Tool parts, and Agent-visible messages: exact deep equality.

Permitted difference observed: only the enabled run wrote the documented trace publication
receipt to `stderr`. Disabled and unavailable-storage runs wrote no `stderr`. Timing and
trace filesystem output were excluded from Agent-visible equality.

## Stress And FeatureBench Probes

A deterministic local canonical Trace was passed through each runner's actual CLI entry:

```text
cd packages/opencode
bun test/observability/stress-cases/run-stress-cases.mjs \
  --semantic-probe /tmp/task-7-semantic-probe.json
bun test/observability/benchmark-cases/run-featurebench-cases.mjs \
  --semantic-probe /tmp/task-7-semantic-probe.json
```

Both exited `0` and reported:

```text
artifact, compaction, edge, lifecycle, mcp, node, response, skill, subagent, tool
```

The stress runner enforces node, edge, artifact, lifecycle, response, and tool categories
for every real result, then adds compaction, MCP, Skill, and Subagent requirements according
to each case's declared mechanisms. The FeatureBench result path enforces node, edge,
artifact, lifecycle, response, and tool and records the observed list in
`trace_semantic_categories`.

The FeatureBench test runner used only a loopback HTTP fixture. No external network, live
LLM/API, or Docker execution was used.

## Memory Measurements

Runtime acceptance:

```text
cd packages/opencode
bun test test/observability/case-trace-memory.test.ts --timeout 120000
```

Observed: **1 pass, 0 fail**.

```json
{"warmRSS":449462272,"finalRSS":459374592,"growthRSS":9912320}
```

Growth was 9,912,320 bytes versus the 134,217,728-byte (128 MiB) limit.

Materializer acceptance:

```text
cd packages/opencode
bun test test/observability/trace-materializer-memory.test.ts --timeout 600000
```

Observed: **1 pass, 0 fail** for a 1,073,873,543-byte sparse journal.

```json
{"materializerMaxRSS":255688704,"finalMaxRSS":255688704,"maxRSS":255688704}
```

Peak RSS was 255,688,704 bytes versus the 268,435,456-byte (256 MiB) limit. Loading the
materialized result through the renderer did not raise the process maximum.

## Focused Verification

Core bounded/segmented observability:

```text
cd packages/opencode
bun test \
  test/observability/causal-ir.test.ts \
  test/observability/causal-ir-runtime-store.test.ts \
  test/observability/case-trace-session.test.ts \
  test/observability/streaming-json-writer.test.ts \
  test/observability/trace-segment.test.ts \
  test/observability/trace-materializer.test.ts \
  test/observability/trace-materializer-diagnostics.test.ts \
  test/observability/trace-publication.test.ts \
  test/cli/trace-finalize.test.ts \
  --timeout 1200000
```

Observed: **141 pass, 0 fail, 1,092 expectations**.

Runtime persistence:

```text
cd packages/opencode
bun test test/observability/case-trace-runtime.test.ts --timeout 120000
```

Observed: **28 pass, 0 fail, 7,334 expectations**.

Renderer:

```text
cd packages/trace-renderer
bun test test/load.test.ts test/cli.test.ts test/html.test.ts test/viewer.test.ts \
  --timeout 120000
```

Observed: **62 pass, 0 fail, 362 expectations**.

Full CaseTrace compatibility run:

```text
cd packages/opencode
bun test test/observability/case-trace.test.ts --timeout 1200000
```

Observed: **162 pass, 1 fail, 2,134 expectations**. The sole failure is the explicitly
excluded unchanged positional assertion at `case-trace.test.ts:7418`: it assumes
`trace.artifacts[0]` contains `semantic model message`, while artifact zero is the earlier
`run.start.environment` payload. Production behavior was not changed for this assertion.

## Typechecks

```text
cd packages/trace-renderer
bun typecheck
```

Observed: **pass** (`tsgo --noEmit`, exit `0`).

```text
cd packages/opencode
bun typecheck
```

Observed: **exit `2` with pre-existing unrelated errors**. No Task 7 file appears after the
fixture environment type was corrected. The exact remaining classes/locations are:

- `TS7006` implicit `_ctx`/`props` in
  `src/cli/cmd/tui/feature-plugins/sidebar/{context,files,todo}.tsx`;
- `TS2322` duplicate worktree/main SDK client private types in
  `src/cli/cmd/tui/plugin/api.tsx`, `src/plugin/index.ts`, and
  `test/fixture/tui-plugin.ts`;
- `TS2769` missing `slots` overload compatibility in
  `src/cli/cmd/tui/plugin/runtime.ts`;
- `TS2769` `string | undefined` in `test/observability/trace-segment.test.ts:1011`;
- `TS2307` unresolved `zod`, `effect`, `@opentui/core`, `@opentui/keymap`,
  `@opentui/keymap/extras`, and `@opentui/solid` declarations under `../plugin/src/`;
- `TS7016` missing `semver` declaration in `../script/src/index.ts`.

## Known Exclusions And Concerns

1. The unchanged full CaseTrace `artifact[0]` positional assertion remains excluded exactly
   as required.
2. `packages/opencode` typecheck is not green for the unrelated errors listed above;
   `packages/trace-renderer` typecheck is green.
3. The host wall clock jumped forward by roughly 7.5 to 15 minutes during several combined
   Bun runs. Nested command wall time remained seconds, but Bun's duration/timeout clock
   reported values such as 906,213 ms. Every affected test passed alone; final aggregate
   runs used a 1,200,000 ms test timeout to avoid a false timeout. No signal or child-process
   defect remained after isolated reproduction.
4. Official FeatureBench Docker evaluation and live benchmark LLM calls were intentionally
   not run. Task 7 uses deterministic runner probes and loopback fixtures, as required.

## Self-Review

- Confirmed no production source file changed.
- Confirmed real-process signal readiness is published only after the signal handler is
  installed, and every spawned TUI fixture has best-effort `SIGKILL` teardown.
- Confirmed runner assertions execute only after `trace.json` exists and do not affect an
  Agent process or response.
- Confirmed failed materialization and invalid trace storage remain trace-only failures.
- Confirmed README does not claim that `SIGKILL` performs immediate finalization.
- Confirmed legacy journals and prior segment evidence are hashed before and after recovery.
- Confirmed the complete diff passes `git diff --check` before commit.

## Fix Round 1/5 Completion

Fix Round 1 replaces the manual TUI and Agent substitutes with production subprocess
coverage. Checkpoints are `ad8b4cbe2` (production TUI and Agent), `7412b3976` (passive
equivalence and actual runners), and `9378ada4c` (runtime-recovery separation and cleanup).

### RED Evidence

- `bun test test/observability/production-agent-e2e.test.ts --timeout 1200000` reached the
  real CLI but stalled after `ProviderModelNotFoundError`; the subprocess inherited the
  checkout `PWD` despite its temporary `cwd`. After using production `--dir`, successive
  RED results exposed whole-history workflow matching, inherited `OPENCODE_DB=:memory:`,
  resume misclassification, and the real four-row compaction persistence contract.
- `bun test test/tool/semantic-observability.test.ts --timeout 1200000 -t "production Agent behavior"`
  initially failed before comparison, then reported the persisted write title's slashless
  temporary path as the only enabled/disabled difference.
- `bun test test/observability/stress-cases/stress-cases.test.mjs --timeout 1200000 -t "actual production case"`
  failed **0 pass, 1 fail** with HTTP `500 ProviderModelNotFoundError` because the runner
  hardcoded provider `deepseek`.
- `bun test test/observability/benchmark-cases/featurebench-cases.test.mjs --timeout 1200000 -t "actual production case"`
  failed **0 pass, 1 fail** first on `unknown argument: --manifest`, then on
  `unknown argument: --skip-docker`, then on the hardcoded provider.
- `bun test test/observability/case-trace.test.ts --timeout 1200000 -t "persists semantic trace records with artifacts and redaction"`
  failed **0 pass, 1 fail** because artifact zero was `run.start.environment`.
- `bun typecheck` reported Task-owned `TS2345` in
  `fixture/task-7-production-harness.ts:329` and `TS2769` in
  `trace-segment.test.ts:1011`, in addition to the environmental baseline errors.

### GREEN Evidence

```text
bun test test/cli/tui/process-trace-e2e.test.ts \
  test/observability/production-agent-e2e.test.ts --timeout 1200000
```

Observed: **2 pass, 0 fail, 44 expectations**. The TUI uses the production CLI in a PTY for
normal EOF, `SIGINT`, and `SIGTERM`; the Agent uses production `opencode run`, persisted
SQLite rows, a loopback model, local MCP, temporary Skill, and the production subagent.

```text
bun test test/tool/semantic-observability.test.ts \
  test/observability/stress-cases/stress-cases.test.mjs \
  test/observability/benchmark-cases/featurebench-cases.test.mjs \
  --timeout 1200000 -t production
```

Observed: **3 pass, 0 fail**. Passive equivalence compares real model requests, persisted
sessions/messages/parts, Tool calls, output hashes, stdout, non-trace stderr, and exit code.
Stress runs a canonical case through its CLI. FeatureBench clones and seals a temporary
local git repo, runs its CLI, and records Docker as disabled without probing it.

```text
bun test test/observability/task-7-e2e.test.ts --timeout 120000
```

Observed: **2 pass, 0 fail, 26 expectations** for real `SIGKILL` durability,
failed-materialization retry, and legacy flat recovery. This low-level runtime fixture no
longer emits Tool, Skill, MCP, Subagent, compaction, lifecycle, or response records.

```text
bun test test/observability/case-trace.test.ts --timeout 1200000 \
  -t "persists semantic trace records with artifacts and redaction"
bun test test/observability/trace-segment.test.ts --timeout 120000 \
  -t "failed atomic manifest publication"
```

Observed: **1 pass, 0 fail** for each command. The semantic artifact is selected through
the context snapshot's `artifact_id`; the publication assertion now supplies a definite
string to Bun's typed matcher.

### Final Verification

```text
bun test test/cli/tui/process-trace-e2e.test.ts \
  test/cli/tui/thread.test.ts test/cli/tui/worker-trace.test.ts \
  test/cli/tui/trace-materializer-process.test.ts \
  test/observability/production-agent-e2e.test.ts \
  test/observability/task-7-e2e.test.ts \
  test/tool/semantic-observability.test.ts \
  test/observability/stress-cases/stress-cases.test.mjs \
  test/observability/benchmark-cases/featurebench-cases.test.mjs \
  --timeout 1200000
```

Observed: **47 pass, 0 fail, 136 expectations**. This includes each runner's actual
production case; no fabricated trace is used by runner acceptance.

```text
bun test test/observability/causal-ir.test.ts \
  test/observability/causal-ir-runtime-store.test.ts \
  test/observability/case-trace-session.test.ts \
  test/observability/streaming-json-writer.test.ts \
  test/observability/trace-segment.test.ts \
  test/observability/trace-materializer.test.ts \
  test/observability/trace-materializer-diagnostics.test.ts \
  test/observability/trace-publication.test.ts \
  test/cli/trace-finalize.test.ts --timeout 1200000
```

Observed: **141 pass, 0 fail, 1,092 expectations**.

```text
bun test test/observability/case-trace-runtime.test.ts --timeout 120000
bun test test/observability/case-trace.test.ts --timeout 1200000
```

Observed: runtime **28 pass, 0 fail, 7,334 expectations**; full CaseTrace **163 pass,
0 fail, 2,137 expectations**. The prior order-dependent CaseTrace exclusion is closed.

```text
cd packages/trace-renderer
bun test test/load.test.ts test/cli.test.ts test/html.test.ts test/viewer.test.ts \
  --timeout 120000
bun typecheck
```

Observed: renderer **62 pass, 0 fail, 362 expectations**; typecheck exit `0`.

```text
cd packages/opencode
bun typecheck
```

Observed at HEAD: exit `2` with **22 environmental diagnostics** in the same sidebar,
duplicate SDK client, plugin slot, unresolved plugin dependency, and `semver` declaration
classes recorded above. No Task 7 file appears.

Base comparison used `git archive 7521658c333d12b26ea0713d53ca230b8d8e7ee9` in a
temporary directory, linked the identical root and package-local installed dependencies,
and ran the same `bun typecheck` command. Base exits `2` with the same 22 environmental
diagnostics plus the Task-owned `trace-segment.test.ts:1011` `TS2769`. HEAD removes that
diagnostic and adds none. The temporary archive and dependency links were removed and
their absence verified.

```text
bun test test/observability/case-trace-memory.test.ts --timeout 120000
bun test test/observability/trace-materializer-memory.test.ts --timeout 600000
```

Observed: both **1 pass, 0 fail**. Runtime RSS was
`{"warmRSS":449970176,"finalRSS":461029376,"growthRSS":11059200}` against the
128 MiB growth limit. The 1,073,873,543-byte sparse journal reported
`{"materializerMaxRSS":255016960,"finalMaxRSS":255016960,"maxRSS":255016960}`
against the 256 MiB peak limit, including renderer load.

No live external API, paid model, Docker command, or Docker availability probe ran in this
round. `git diff --check` final result: **pass** (exit `0`, no output).
