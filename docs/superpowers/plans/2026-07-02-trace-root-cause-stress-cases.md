# Trace Root-Cause Stress Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible stress-case suite that runs observable-opencode through HTTP sessions and reviews whether each trace contains enough semantics for offline root-cause attribution.

**Architecture:** The suite lives under `packages/opencode/test/observability/stress-cases/` and is independent from production trace instrumentation. Fixtures are small synthetic repositories; `run-stress-cases.mjs` copies a fixture, starts a release binary with tracing enabled, sends the case prompt through `/session` and `/message`, then calls `analyze-trace-sufficiency.mjs` logic to write review reports.

**Tech Stack:** Node.js ESM scripts, built-in `node:test`, JSON schema metadata, local HTTP requests via `fetch`, existing observable-opencode release binary.

## Global Constraints

- Use `opencode serve` plus HTTP `/session` and `/message`; do not use `opencode run`.
- Trace module itself must not diagnose root cause; review scripts only judge trace sufficiency against ground truth metadata.
- Stress run outputs go to `/tmp/observable-opencode-stress-run` or a caller-specified temp directory, not git.
- Large trace payloads remain in trace artifacts; review reports reference records and missing semantics.
- The suite must be runnable with an existing release binary path and DeepSeek API key supplied by environment.

---

### Task 1: Metadata And Review Tests

**Files:**

- Create: `packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`
- Create: `packages/opencode/test/observability/stress-cases/lib/stress-review.mjs`
- Create: `packages/opencode/test/observability/stress-cases/cases.json`

**Interfaces:**

- Produces `loadCases(rootDir): CaseDefinition[]`.
- Produces `reviewTraceSufficiency({ caseDefinition, trace }): TraceReview`.

- [ ] **Step 1: Write failing tests**

Add `node:test` coverage that asserts there are exactly 8 case definitions, required fields are present, fixture directories exist, and synthetic trace review classifies missing evidence as `insufficient`.

- [ ] **Step 2: Run red test**

Run: `node --test packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`

Expected: fails because stress-case files are not implemented yet.

### Task 2: Fixtures And Case Metadata

**Files:**

- Create: `packages/opencode/test/observability/stress-cases/README.md`
- Create: `packages/opencode/test/observability/stress-cases/stress-case.schema.json`
- Create fixture folders under `packages/opencode/test/observability/stress-cases/fixtures/`

**Interfaces:**

- Each fixture has `package.json`, `opencode.json`, source/docs/tests, and optional `mcp-server.mjs`.
- Each `cases.json` entry names a `fixture_dir`.

- [ ] **Step 1: Implement 8 fixtures**

Create synthetic repos for wrong implementation target, conflicting evidence, ignored MCP fact, compaction lost constraint, subagent misleading summary, insufficient verification, tool failure hallucination, and design quality regression.

- [ ] **Step 2: Run metadata test**

Run: `node --test packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`

Expected: metadata and fixture existence pass.

### Task 3: HTTP Runner

**Files:**

- Create: `packages/opencode/test/observability/stress-cases/run-stress-cases.mjs`

**Interfaces:**

- CLI flags:
  - `--binary <path>`
  - `--out <dir>`
  - `--case <case_id>` optional, repeatable
  - `--config <opencode_config_dir>` optional
- Env:
  - `DEEPSEEK_API_KEY`
  - optional `OPENCODE_STRESS_MODEL`, default `deepseek-v4-pro`

- [ ] **Step 1: Implement fixture copy and server lifecycle**

Start binary with isolated HOME/XDG dirs, parse dynamic port, create session, send case message, stop server, ensure trace files exist.

- [ ] **Step 2: Add dry-run metadata mode**

Support `--dry-run` to list selected cases without starting opencode, so local tests do not need network.

### Task 4: Sufficiency Analyzer

**Files:**

- Create: `packages/opencode/test/observability/stress-cases/analyze-trace-sufficiency.mjs`
- Modify: `packages/opencode/test/observability/stress-cases/lib/stress-review.mjs`

**Interfaces:**

- CLI:
  - `node analyze-trace-sufficiency.mjs --cases cases.json --traces /tmp/.../traces --out /tmp/.../reports`
- Outputs `<case_id>.trace-review.json` and `summary.md`.

- [ ] **Step 1: Implement evidence detection**

Map required evidence names to trace predicates, for example `mcp_call_output`, `edited_file_paths`, `verification_commands`, `subagent_trace`, `compaction_ledger`, and `unsupported_claims`.

- [ ] **Step 2: Implement sufficiency scoring**

`sufficient` if all required evidence is found; `partial` if at least half is found; `insufficient` otherwise. Preserve `missing_semantics` and `redundant_or_noisy_semantics`.

### Task 5: Verification And Commit

**Files:**

- All files above.

**Interfaces:**

- Verification commands:
  - `node --test packages/opencode/test/observability/stress-cases/stress-cases.test.mjs`
  - `node --check packages/opencode/test/observability/stress-cases/run-stress-cases.mjs`
  - `node --check packages/opencode/test/observability/stress-cases/analyze-trace-sufficiency.mjs`
  - `./node_modules/.bin/prettier --check packages/opencode/test/observability/stress-cases docs/superpowers/plans/2026-07-02-trace-root-cause-stress-cases.md`
  - `git diff --check`

- [ ] **Step 1: Run verification**

Run each command and inspect exit codes.

- [ ] **Step 2: Commit**

Commit with message `test: add trace root cause stress cases`.

- [ ] **Step 3: Optional real run**

If a release binary exists locally or can be downloaded, run at least a selected subset through HTTP and generate reports.
