# Trace v5.1 Claim, Inline Subagent, Lifecycle, And Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make trace v5.1 cleaner for offline attribution by canonicalizing claims, representing inline subagent traces honestly, reducing lifecycle health noise, and filling forced compaction estimates from existing checks.

**Architecture:** Keep `case-trace.ts` as the semantic producer and `causal-trace-viewer.ts` as the single HTML renderer. Add small helper functions for claim classification, inline subagent enrichment, lifecycle finalization policy, and compaction estimate backfill. Existing trace fields remain compatible, but v5.1 adds more precise fields.

**Tech Stack:** TypeScript, Bun test suite, opencode observability trace writer, static HTML renderer, `tsgo --noEmit`.

## Global Constraints

- Do not implement root-cause diagnosis.
- Keep `trace.html` as the only HTML entry point.
- Keep large payloads in artifacts.
- Preserve legacy trace files and compatibility fields.
- Release validation should continue to use `opencode serve` plus HTTP `/session` and `/session/:id/message`.

---

### Task 1: Claim Canonicalization Tests

**Files:**

- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Produces tests that require section/table scaffolding to be filtered and factual table rows to be canonicalized.

- [x] Add a test final answer containing Chinese headings, Markdown table header/separator rows, and factual owner/discount/test rows.
- [x] Assert no claims are emitted for `各来源的关键结论对比`, `子 Agent 独立总结`, `是否需要改动`, or table header/separator rows.
- [x] Assert factual table rows have `claim_format: "table_fact"`, `raw_text`, `canonical_text`, `table_cells`, and `table_subject`.
- [x] Attempt the focused Bun test. Local execution is blocked because `bun` is not available on PATH; the test is written to fail on v5.0 behavior where scaffold claims and raw table strings are still emitted.

### Task 2: Claim Canonicalization Implementation

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/trace-semantic-contract.ts`
- Modify: `docs/observable-benchmark-trace.md`

**Interfaces:**

- Consumes final response text.
- Produces `response.claim` with `claim_format` and table canonical fields.

- [x] Advance `TRACE_VERSION` to `"5.1"`.
- [x] Add claim classification helpers for `section_scaffold`, `table_scaffold`, `table_fact`, and `factual_claim`.
- [x] Drop scaffold candidates before `response.claim` records are created.
- [x] Attach canonical table fields to factual table row claims.
- [x] Update docs to describe v5.1 claim behavior.

### Task 3: Inline Subagent Tests And Implementation

**Files:**

- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-trace-viewer.ts`

**Interfaces:**

- Consumes `subagent.call` records with `child_session_id`.
- Produces inline child trace fields when same-trace records exist.

- [x] Add a test that creates a subagent call for `child_session_id`.
- [x] Add same-trace `prompt.assembly`, `tool.call`, and `response.output` records for that child session.
- [x] Assert `child_trace_available: true`, `child_trace_mode: "inline_same_trace"`, `child_record_count > 0`, and no `child_trace_unavailable_reason`.
- [x] Implement finalization enrichment that scans records for child session ids.
- [x] Update the Subagents HTML section to show inline child record counts and refs.

### Task 4: Lifecycle And Compaction Tests And Implementation

**Files:**

- Modify: `packages/opencode/test/observability/case-trace.test.ts`
- Modify: `packages/opencode/src/observability/case-trace.ts`

**Interfaces:**

- Consumes final trace records.
- Produces cleaner health metrics and compaction estimate fields.

- [x] Add a test where `run.start` and `agent.lifecycle` remain running until finish.
- [x] Assert expected finalization does not increase `unexpected_missing_close_records`.
- [x] Add a test where a `context.compaction_check` has `token_estimate` and forced `context.compaction` lacks estimates.
- [x] Assert compaction gets `token_estimate_before` and `estimate_source: "nearest_compaction_check"`.
- [x] Implement expected lifecycle close policy and compaction estimate backfill.

### Task 5: Verification

**Files:**

- Modify: `docs/superpowers/plans/2026-07-01-trace-v51-claim-subagent-lifecycle.md`

**Interfaces:**

- Produces verification notes.

- [x] Run Prettier on touched files.
- [x] Run `./node_modules/.bin/tsgo --noEmit` from `packages/opencode`.
- [x] Run `git diff --check`.
- [x] If Bun is unavailable, record that Bun tests could not run locally and rely on typecheck plus later release HTTP E2E.

### Task 6: Payload Provenance

**Files:**

- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**

- Consumes large field summaries that spill into artifacts.
- Produces `payload_ref` and `payload_dedupe_group_id` on the field summary.

- [x] Add a test that forces a context transform input/output field into an artifact with `OPENCODE_CASE_TRACE_MAX_FIELD_LENGTH=64`.
- [x] Assert the resulting field summary includes `payload_ref` and `payload_dedupe_group_id`.
- [x] Add these fields to `TraceFieldSummary`.
- [x] Populate them when `summarizeText` or `summarizeJson` writes an artifact.

## Verification Notes

- `bun test packages/opencode/test/observability/case-trace.test.ts --timeout 30000`: not run locally because `bun` is not available on PATH in this environment.
- `./node_modules/.bin/prettier --write ...`: passed.
- `./node_modules/.bin/tsgo --noEmit` from `packages/opencode`: passed.
- `git diff --check`: passed.
