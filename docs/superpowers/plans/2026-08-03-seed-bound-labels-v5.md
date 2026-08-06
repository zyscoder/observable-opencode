# Seed-Bound Attribution Labels v5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development`. Implement task-by-task with explicit
> RED and GREEN evidence. Do not commit or push.

**Goal:** Cryptographically bind labels to ordered factual inputs and score all
causal roles per defect seed without cross-seed leakage.

**Architecture:** Exact schema parsing, input binding, and report projection are
separate package modules. The evaluator remains orchestration-only and v3/v4
remain compatible for single-seed reports.

**Constraints:** Python 3.9+, offline, no Provider calls, no Agent-visible data,
3600-second Provider policy unchanged, preserve the dirty worktree.

---

### Task 1: Exact v5 Label Contract

**Files:**
- Create: `tools/trace_attribution/trace_attribution/label_contract.py`
- Create: `tools/trace_attribution/tests/test_evaluation_labels_v5.py`

- [ ] Write failing tests for exact keys, enums, hashes, explicit per-seed
  `expected_outcome`, duplicate defect/start/seed identities, recomputation
  inputs, occurrence and node-ref role conflicts, and invalid shared factors.
- [ ] Run the test module and retain RED output.
- [ ] Implement immutable v3/v4/v5 parsing. Preserve evaluation digest order.
- [ ] Run GREEN. Confirm caller input is not mutated.

### Task 2: Ordered Source And Revision Binding

**Files:**
- Create: `tools/trace_attribution/trace_attribution/input_binding.py`
- Modify: `tools/trace_attribution/tests/test_evaluation_labels_v5.py`

- [ ] Write one-field mutation tests for source bytes, evaluation order and
  membership, effective Trace, case ID, revision, provenance, start ref,
  fingerprint, anchor, occurrence, and seed identity.
- [ ] Run RED.
- [ ] Implement exact-byte hashing, canonical effective hashing, valid revision
  provenance, ordered multi-source seed bindings, authoritative effective
  Trace/graph input, and `seed_binding_identity_for(start_ref, fingerprint)`
  checks.
- [ ] Run GREEN and prove semantically equivalent but byte-different inputs fail.

### Task 3: Seed-Owned Report Projection

**Files:**
- Create: `tools/trace_attribution/trace_attribution/seed_projection.py`
- Modify: `tools/trace_attribution/tests/test_evaluation_labels_v5.py`

- [ ] Build a failing two-seed report test where aggregate nodes are correct but
  the roots are swapped between seeds.
- [ ] Add RED tests for each ownership source: root/condition/amplifier
  `confirmation`, materialization `role_judgment`, unrelated nested judgment,
  and owned step introduction judgments.
- [ ] Implement strict projection keyed by `(seed_binding_identity,
  semantic_occurrence_id, role)`; reject missing, unknown, contradictory, or
  multiply-owned items.
- [ ] Run GREEN and prove item/record permutations do not change results.

### Task 4: Per-Seed And Aggregate Metrics

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/seed_projection.py`
- Modify: `tools/trace_attribution/tests/test_evaluation_labels_v5.py`

- [ ] Add RED tests for per-seed root/role scores, Top-1, micro/macro metrics,
  three coverage measures, `expected_outcome` agreement, shared-factor
  expansion, and explicit no-defect/unresolved controls.
- [ ] Implement deterministic metrics and `all_seed_coverage_passed`.
- [ ] Reject any missing/extra report seed and v3/v4 labels on multi-seed data.
- [ ] Run GREEN.

### Task 5: Evaluator Integration And Comparison v8

**Files:**
- Modify: `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- Modify: `tools/trace_attribution/tests/test_recursive_benchmarks.py`
- Modify: `tools/trace_attribution/tests/test_evaluation_labels_v5.py`

- [ ] Write CLI RED tests: valid v5 writes comparison v8; every binding/safety
  failure exits 2 and writes no output; single-seed v3/v4 shape is unchanged.
- [ ] Delegate v5 work to the three package modules after existing graph/report
  safety checks.
- [ ] Run focused GREEN, then full Python discovery with no network calls.

### Task 6: Real Multi-Seed Acceptance Fixture

**Files:**
- Create: `tools/trace_attribution/tests/fixtures/recursive_cases/multi_seed_bound_v5.json`
- Modify: `tools/trace_attribution/tests/test_recursive_benchmarks.py`

- [ ] Add a two-defect fixture containing introduced-by-Agent and
  baseline-existing-unrepaired origins plus one shared amplifier.
- [ ] Prove swapped ownership fails, all coverage passes when correct, and no
  label data reaches any Judge request/cache/checkpoint/report.
- [ ] Run the fixture twice and require byte-equivalent comparison output.

### Task 7: Flash Regression

- [ ] Bind the clean Sphinx trace, review/evaluation inputs, and human labels to
  v5 without modifying the factual Trace.
- [ ] Evaluate the existing `deepseek-v4-flash` report and retain the known
  prompt/verification role mismatches as real quality evidence.
- [ ] Re-evaluate the prior v4-pro result to demonstrate labels bind to the same
  subject while model-quality differences remain measurable.
