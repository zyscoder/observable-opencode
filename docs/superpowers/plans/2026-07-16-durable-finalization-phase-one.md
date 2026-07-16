# Durable Finalization Phase One Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a terminal canonical Causal IR reliably on supported signals without duplicating the complete trace in the final journal entry.

**Architecture:** Keep periodic full checkpoints for `SIGKILL` recovery, but make `case.finalized` a compact integrity record. Build the terminal Causal IR once, publish canonical files before compatibility projections, and give the FeatureBench runner an explicit signal-forward-and-wait lifecycle.

**Tech Stack:** TypeScript, Bun test, Node.js child processes, JSONL Causal IR journal.

## Global Constraints

- Collection remains a passive sidecar and cannot alter agent-visible behavior.
- `SIGINT`, `SIGTERM`, and `SIGHUP` are catchable; `SIGKILL` is checkpoint-only.
- Existing trace IDs, semantic payloads, artifact hashes, and compatibility projections remain readable.
- Production changes require a failing regression test first.

---

### Task 1: Compact Final Journal Contract

**Files:**
- Modify: `packages/opencode/src/observability/causal-ir.ts`
- Modify: `packages/opencode/test/observability/causal-ir.test.ts`

**Interfaces:**
- Consumes: `CausalIRStore.finalize(data)` and incremental journal entries.
- Produces: a `case.finalized` entry with terminal metadata, graph counts, graph hashes, and no `snapshot` or `trace` field.

- [ ] Add a failing test asserting finalization payload size does not grow with graph payload size and excludes full snapshots.
- [ ] Add a failing replay test proving incremental entries plus a compact finalization reconstruct the store snapshot.
- [ ] Implement compact finalization payload construction and replay compatibility.
- [ ] Run `bun test test/observability/causal-ir.test.ts` and verify all tests pass.

### Task 2: Canonical Trace First Publication

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: terminal `CausalIRTraceSummary` and compact journal commit result.
- Produces: terminal `manifest.json`, `trace.json`, and `partial/latest.json` before compatibility JSON/HTML.

- [ ] Add a failing order test that blocks compatibility rendering and verifies the canonical files already contain terminal state.
- [ ] Add a large-payload signal test with a bounded flush deadline and bounded final journal entry size.
- [ ] Refactor final publication into terminal-core and compatibility-projection methods.
- [ ] Make file replacement atomic within the trace directory while preserving passive write-failure behavior.
- [ ] Run focused signal, write-failure, journal-replay, and passive-sidecar tests.

### Task 3: Runner Signal Forwarding

**Files:**
- Modify: `packages/opencode/test/observability/benchmark-cases/run-featurebench-cases.mjs`
- Modify: `packages/opencode/test/observability/benchmark-cases/featurebench-cases.test.mjs`

**Interfaces:**
- Consumes: parent `SIGINT`/`SIGTERM`/`SIGHUP`, active child process, expected trace path.
- Produces: exactly-once child signal forwarding followed by bounded wait for child exit and canonical trace publication.

- [ ] Add a failing unit test using a synthetic child that records the forwarded signal and delays canonical trace creation.
- [ ] Export a scoped runner lifecycle helper with install, forward, await, and dispose behavior.
- [ ] Use the helper in `runOneCase` without changing HTTP case execution.
- [ ] Run `node --test packages/opencode/test/observability/benchmark-cases/featurebench-cases.test.mjs`.

### Task 4: Phase-One Verification and Baseline

**Files:**
- Create: `docs/superpowers/reports/2026-07-16-durable-finalization-phase-one.md`

**Interfaces:**
- Consumes: focused tests and a synthetic large trace.
- Produces: phase-one acceptance report and phase-two baseline metrics.

- [ ] Run Causal IR, CaseTrace, FeatureBench runner, and typecheck verification commands.
- [ ] Run a synthetic large-trace `SIGTERM` regression through `opencode serve` and record output files, statuses, sizes, and flush duration.
- [ ] Compare terminal journal entry size with canonical trace size and verify replay equivalence.
- [ ] Record graph counts, bytes, finalization time, and attribution edge categories as the phase-two baseline.
- [ ] Start phase two only if every phase-one acceptance gate passes.
