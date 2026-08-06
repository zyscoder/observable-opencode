# Causal Role Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Independently confirm and publish bounded non-root causal roles from a completed Global Judge comparison without changing Agent behavior or weakening root confirmation.

**Architecture:** Reuse the existing `RootConfirmationRequest` and provider lifecycle. Add a blind scheduling scope and separate per-seed root/non-root quotas, then enqueue deterministic non-root candidates from completed Global assessments. Final factors continue to come only from definitive independent confirmations.

**Tech Stack:** Python 3 standard library, `unittest`, existing Causal IR graph, attribution checkpoint journals, Anthropic-compatible DeepSeek validation.

## Global Constraints

- Analysis remains passive, offline, read-only, and unable to affect Agent execution.
- Global causal roles are scheduling hints only and are absent from Judge-visible confirmation requests.
- Existing Top-3 root selection remains unchanged.
- At most three non-root candidates are independently reviewed per seed.
- Existing report schema collections and canonical publication functions remain authoritative.
- No API key or provider credential may be persisted.

---

### Task 1: Blind Dual-Quota Confirmation Queue

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_causal_role_closure.py`

**Interfaces:**
- Produces: queue field `review_scope: "root"|"non_root"`.
- Produces: `MAX_NON_ROOT_CONFIRMATION_CANDIDATES = 3`.
- Preserves: `RootConfirmationRequest.factual_dict()` without Global role or review scope.

- [x] **Step 1: Write failing queue tests**

Create a state with three root and three non-root queue entries for one seed and
assert all six are accepted, a fourth entry in either scope is rejected, and a
duplicate candidate across scopes is rejected.

- [x] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_role_closure -v
```

Expected: failure because `review_scope` is not allowed and the current queue
enforces one shared three-candidate limit.

- [x] **Step 3: Implement the scoped queue contract**

Add `review_scope` to pending and terminal queue schemas. Validate exact values,
count candidates by seed and scope, preserve one-candidate-per-seed uniqueness,
and enforce root `<=3`, non-root `<=3`, total `<=6`.

- [x] **Step 4: Verify request blindness**

Build queued root and non-root requests and assert their factual payload contains
neither `review_scope` nor the Global role.

- [x] **Step 5: Run focused queue and checkpoint tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_causal_checkpoint -v
```

Expected: all tests pass.

### Task 2: Global Non-Root Candidate Scheduling

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_causal_role_closure.py`

**Interfaces:**
- Produces: `_rank_global_non_root_assessments(...)`.
- Consumes: complete `GlobalCandidateJudgment.assessments` and candidate capsules.
- Produces: up to three blind `review_scope="non_root"` confirmation entries.

- [x] **Step 1: Write failing Global scheduling tests**

Use a scripted Global Judge with one selected root, one condition, one
amplifier, one unrelated row, and one outcome-evidence row. Assert the root and
three reviewable non-root rows are confirmed, while outcome evidence is not
sent to the verifier.

- [x] **Step 2: Run the focused tests and verify RED**

Expected: only the selected root reaches `confirmation_requests`.

- [x] **Step 3: Implement deterministic non-root scheduling**

Create a hypothesis and confirmation binding for each selected non-root row,
add grounded support refs, enqueue it with `review_scope="non_root"`, and leave
`introduction_candidates` and `introduction_hypothesis_ids` unchanged.

- [x] **Step 4: Add bounded coverage-gap behavior**

When a valid non-root candidate cannot be queued, record
an exact `factor_confirmation_enqueue_gaps` entry without invalidating an
already confirmed root.

- [x] **Step 5: Run focused analyzer tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_recursive_analyzer -v
```

Expected: all tests pass.

### Task 3: Canonical Factor Publication And Replay

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Test: `tools/trace_attribution/tests/test_causal_role_closure.py`
- Test: `tools/trace_attribution/tests/test_root_confirmation_fix40.py`

**Interfaces:**
- Preserves: `canonical_causal_factor_publication(...)`.
- Preserves: `canonical_rejected_candidate_publication(...)`.
- Produces: exact queue scope and selection provenance across checkpoints.

- [x] **Step 1: Write failing publication and replay tests**

Assert that an independently rejected condition and amplifier are published in
their canonical collections, an unrelated candidate is rejected, and a
checkpoint resume reproduces these results without an additional provider call.

- [x] **Step 2: Run tests and verify RED**

Expected: failure because non-root candidates are not queued or replayed.

- [x] **Step 3: Complete projection and validation**

Carry `review_scope` through queue, confirmation journal, checkpoint state, and
grounded-report validation. Carry `review_scope` and `origin` through started
and terminal actions, version the action state as v19 and action projection as
v8, and keep Global assessment role separate from the canonical independent
confirmation role.

- [x] **Step 4: Run schema, replay, and factor regressions**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_role_closure \
  tools.trace_attribution.tests.test_causal_state \
  tools.trace_attribution.tests.test_root_confirmation_fix40 \
  tools.trace_attribution.tests.test_root_confirmation_fix42 -v
```

Expected: all tests pass.

### Task 5: Independent Review Remediation

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Test: `tools/trace_attribution/tests/test_causal_role_closure.py`

- [x] Permit and validate an auditable one-sided non-root necessary-cause
  conflict when no root was confirmed.
- [x] Restrict non-root competitor requests to already published confirmed
  roots.
- [x] Bind every factor gap/conflict to exactly one completed non-root queue
  owner and exact scheduler origin.
- [x] Persist scope and origin through action lifecycle schema v19.
- [x] Persist and validate bounded non-root enqueue coverage gaps.
- [x] Re-run the full suite and a real-provider Sphinx control.
- [x] Require mutual co-root support against every published same-seed root.
- [x] Reject coordinated selected-root-to-factor-gap forgery by grounding
  factor ownership in the Global non-root assessment.
- [x] Version report/checkpoint/output as v21/v21/v10 and require pending
  queue origin at restore time.
- [x] Verify enqueue-gap completed checkpoint replay without provider calls.

### Task 4: Full Regression And Sphinx Validation

**Files:**
- Modify: `docs/superpowers/reports/2026-07-27-causal-role-closure-results.md`

**Interfaces:**
- Consumes: sanitized Sphinx fixture and configured DeepSeek endpoint.
- Produces: root metrics, factor-role metrics, request counts, payload sizes,
  disagreement analysis, and the next-stage recommendation.

- [x] **Step 1: Run the complete attribution suite**

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'
```

Expected: all tests pass.

- [x] **Step 2: Run compilation and diff checks**

```bash
PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache \
  python3 -m compileall -q tools/trace_attribution
git diff --check
```

Expected: both commands exit 0.

- [x] **Step 3: Run sanitized Sphinx with DeepSeek**

Use the authorized Anthropic-compatible endpoint and a non-echoing environment
injection. Never write the API key to a file or report.

- [x] **Step 4: Compare against the previous baseline**

Record:

- primary root and Top-1 match;
- condition, amplifier, and unrelated publications;
- factor-role precision;
- unresolved branches;
- logical and physical Judge calls;
- root regression status.

- [x] **Step 5: Write the result report**

Document measured improvements, remaining disagreements, and whether the project
is ready to enter cross-Benchmark calibration.
