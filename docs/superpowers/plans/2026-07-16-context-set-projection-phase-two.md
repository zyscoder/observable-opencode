# Context Set and Projection Phase Two Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce repeated context-membership edges and terminal storage copies without losing direct attribution paths or existing trace file contracts.

**Architecture:** Represent repeated tool-result selections and temporal advisory windows as canonical `context.pack` set nodes. Keep confirmed member-to-set-to-transform paths attribution eligible, keep temporal windows advisory only, and hard-link terminal `partial/latest.json` to canonical `trace.json` when the filesystem supports it.

**Tech Stack:** TypeScript, Causal IR 1.0, Bun test, POSIX hard links with copy fallback.

## Global Constraints

- No trace-derived information is returned to the agent or used to change execution.
- Confirmed tool-result consumption must remain backward reachable from decisions.
- Temporal proximity remains attribution ineligible.
- `trace.json`, `partial/latest.json`, provenance, legacy, and HTML paths remain available.
- Windows and filesystems without hard links fall back to atomic content writes.

---

### Task 1: Context Selection Sets

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `context.pack` nodes with `context_set_kind=confirmed_tool_selection` and stable member references.
- Produces: one `selected_into_context` edge per member/set and one `used_as_context` edge per set/transform.

- [x] Add a failing test with two transformations of the same message and tool-result set.
- [x] Assert there is one set node, one member edge, two transform edges, and no repeated direct member/transform edge.
- [x] Implement content-addressed set identity and reuse scoped by session, message, and member list.
- [x] Verify decisions remain backward reachable to selected tool results.

### Task 2: Temporal Advisory Sets

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `context.pack` nodes with `context_set_kind=temporal_advisory` and payload-only member refs.
- Produces: one ineligible `recent_source_fallback` edge per set/target.

- [x] Add a failing test proving repeated targets reuse one advisory set.
- [x] Assert no member/target edge is emitted and members are available for viewer expansion in the set payload.
- [x] Implement set reuse without adding members to attribution-bearing source refs.
- [x] Update existing temporal-advisory regressions to verify equivalent advisory visibility.

### Task 3: Terminal Partial De-duplication

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Consumes: completed canonical `trace.json`.
- Produces: `partial/latest.json` with identical bytes through an atomic hard link, or atomic copy fallback.

- [x] Add a failing POSIX test asserting canonical and terminal partial share device and inode.
- [x] Implement temporary hard-link plus rename, with safe content-write fallback.
- [x] Re-run write-failure, signal, and partial-equality tests.

### Task 4: Phase-Two Regression and Metrics

**Files:**
- Create: `docs/superpowers/reports/2026-07-16-context-set-projection-phase-two.md`

**Interfaces:**
- Consumes: pre-change real Stress Trace and post-change regression traces.
- Produces: edge cardinality, storage, reachability, and lifecycle comparison.

- [x] Run full Causal IR, CaseTrace, runner, stress metadata, and typecheck suites.
- [x] Re-run `semantic-requirement-priority` through HTTP with the same model.
- [x] Compare total edges, temporal advisory edges, tool-context edges, total allocated bytes, and root-reachable tool outcomes.
- [x] Reject the phase if direct attribution reachability or lifecycle equivalence regresses.
