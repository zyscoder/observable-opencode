# Revision-Bound Evidence and Merkle Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make formally bound artifact evidence Judge-visible and reduce repeated checkpoint storage while preserving exact logical replay.

**Architecture:** The trace writer injects immutable case-start revision binding through its common Causal IR node factory. The checkpoint layer transparently encodes large nested payload values as verified content-addressed blobs and hydrates them after journal integrity validation.

**Tech Stack:** TypeScript/Bun tests, Python 3 standard library, unittest/pytest-compatible test suite.

## Global Constraints

- Trace collection remains passive and must not modify Agent behavior.
- Attribution must fail closed for stale, ambiguous, unbound, missing, or tampered evidence.
- Existing inline checkpoints remain restorable.
- Restored logical checkpoint payloads remain byte-for-byte JSON equivalent.

---

### Task 1: Formal Causal IR Node Revision Binding

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

- [ ] Add a failing integration test for a large decision rationale artifact
  whose owning record must carry the case-start revision binding.
- [ ] Run the focused Bun test and verify the missing binding failure.
- [ ] Inject immutable binding fields in the common `node()` path.
- [ ] Verify configured, forged-input, and unconfigured cases.

### Task 2: Content-Addressed Checkpoint Payloads

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/checkpoint.py`
- Test: `tools/trace_attribution/tests/test_causal_checkpoint.py`

- [ ] Add failing tests proving repeated large values are stored once and
  restored exactly.
- [ ] Add failing corruption tests for missing and modified blobs.
- [ ] Implement bottom-up Merkle encoding before journal append.
- [ ] Implement strict recursive hydration after committed-record validation.
- [ ] Verify crash consistency, transaction identity, and inline compatibility.

### Task 3: Attribution And Benchmark Regression

**Files:**
- Modify: `docs/superpowers/reports/2026-08-04-commitment-evidence-stability-results.md`

- [ ] Run focused TypeScript and Python tests.
- [ ] Run the complete offline-attribution Python suite.
- [ ] Generate a revision-bound trace fixture and verify its artifact envelope.
- [ ] Re-run the Sphinx Bundle preflight and compare evidence eligibility.
- [ ] Measure checkpoint bytes and serialization latency on a representative
  repeated-state workload.
- [ ] Record observed results, remaining gaps, and next benchmark action.
