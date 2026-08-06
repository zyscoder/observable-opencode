# Attribution Convergence vNext Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make offline causal attribution retain valid work across Provider failures, decompose broad benchmark failures, recall planning omissions, and confirm multi-root causal explanations without changing Agent behavior.

**Architecture:** Normalize external evaluation into independent defect seeds, reconstruct candidates from confirmed dataflow plus explicitly marked offline obligation gaps, compare candidates globally, then recursively confirm roots and non-root roles. Provider lifecycle and Trace storage remain independent from Agent execution.

**Tech Stack:** Python 3.9+, `unittest`, Anthropic-compatible DeepSeek API, TypeScript/Bun, Causal IR JSON.

## Global Constraints

- Attribution remains passive, offline, and invisible to the Agent.
- Human root labels are evaluation-only and never enter candidate or Judge inputs.
- Confirmed Trace edges and offline reconstruction edges remain distinguishable.
- Provider request timeout remains 3600 seconds.
- Quality is prioritized over request cost, but deterministic non-retryable errors stop after one request.
- Existing dirty worktree changes are preserved and never reverted.
- Every production behavior change follows red-green-refactor TDD.

---

### Task 1: Provider Failure Semantics And Partial Result Preservation

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/claude.py`
- Modify: `tools/trace_attribution/trace_attribution/errors.py`
- Modify: `tools/trace_attribution/trace_attribution/checkpoint.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/cli.py`
- Test: `tools/trace_attribution/tests/test_causal_judge.py`
- Test: `tools/trace_attribution/tests/test_causal_checkpoint.py`
- Test: `tools/trace_attribution/tests/test_global_pagination_integration.py`
- Test: `tools/trace_attribution/tests/test_recursive_cli.py`

**Interfaces:**
- Produces: `ProviderFailureDisposition` with `retryable`, `category`, `status_code`, `error_code`, and `reason`.
- Produces: checkpointed successful page judgments that survive later Provider failure.
- Preserves: current CLI flags and report factual validation.

- [ ] **Step 1: Write failing Provider classification tests**

Add table-driven tests asserting:

```python
assert classify_provider_failure(status=402, code="invalid_request_error").retryable is False
assert classify_provider_failure(status=503, code="service_unavailable").retryable is True
assert classify_provider_failure(status=None, code="connect_timeout").retryable is True
```

Assert a 402 opens the circuit after one physical request and a 503 follows
`provider_error_threshold`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_judge \
  tools.trace_attribution.tests.test_recursive_cli -v
```

Expected: failures show that 402 is currently treated as a generic Judge error
and does not open the Provider circuit.

- [ ] **Step 3: Implement failure classification and circuit state**

Add an immutable disposition object and classify deterministic HTTP/API errors
before generic retry handling. Persist classification in the circuit snapshot.
Do not string-match only the human message when structured status/code exists.

- [ ] **Step 4: Write failing partial-page persistence tests**

Script three pages: page 1 succeeds, page 2 raises non-retryable 402, page 3
must not execute. Assert:

```python
assert report.metadata["global_candidate_judgments"] == [page_1_judgment]
assert report.analysis_outcome == "partial"
assert report.metadata["provider_circuit"]["open"] is True
assert physical_requests == 2
```

- [ ] **Step 5: Implement transactional partial publication**

Persist every validated page before requesting the next page. Report builders
must consume retained pages even when convergence is incomplete. Add explicit
`unresolved_page_refs`; never reinterpret incomplete as an empty judgment.

- [ ] **Step 6: Add fusion-mode identity tests**

Create a checkpoint with `fusion_mode="retrieval-global"` and assert resume
and final report preserve that exact value. A mismatched resume configuration
must fail before issuing Provider requests.

- [ ] **Step 7: Run focused GREEN verification**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_judge \
  tools.trace_attribution.tests.test_causal_checkpoint \
  tools.trace_attribution.tests.test_global_pagination_integration \
  tools.trace_attribution.tests.test_recursive_cli -v
```

Expected: all tests pass.

---

### Task 2: Evaluation Failure Signature Clustering

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/evaluation_facts.py`
- Modify: `tools/trace_attribution/trace_attribution/quality_review.py`
- Modify: `tools/trace_attribution/trace_attribution/graph.py`
- Test: `tools/trace_attribution/tests/test_seed_attribution.py`
- Create: `tools/trace_attribution/tests/test_evaluation_failure_clustering.py`

**Interfaces:**
- Produces: immutable `FailureSignature` and `ObservedDefectSeed`.
- Consumes: external evaluation JSON only.
- Preserves: human labels remain absent from all factual projections.

- [ ] **Step 1: Write failing clustering tests**

Build fixtures where 12 tests share descriptor `AttributeError`, one layered
run exposes `PydanticUndefinedAnnotation`, and one exposes JSON Schema
`AttributeError`. Assert three stable clusters regardless of input order.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_evaluation_failure_clustering -v
```

Expected: module or types do not exist.

- [ ] **Step 3: Implement exact failure normalization**

Normalize exception family, first business frame, assertion contract, symbol,
and subsystem. Compute stable SHA-256 identities over canonical JSON.

- [ ] **Step 4: Project one seed per cluster**

Inject one observed-defect node per cluster. Keep layered evaluation identity
and prerequisite neutralization facts explicit.

- [ ] **Step 5: Verify clustering and seed regression**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_evaluation_failure_clustering \
  tools.trace_attribution.tests.test_seed_attribution -v
```

Expected: all tests pass and no label fields enter projections.

---

### Task 3: Obligation Gap And Planning Omission Candidates

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/restoration_obligation.py`
- Modify: `tools/trace_attribution/trace_attribution/reconstruction.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- Modify: `tools/trace_attribution/trace_attribution/candidate_budget.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`
- Create: `tools/trace_attribution/tests/test_omission_candidate_reconstruction.py`

**Interfaces:**
- Produces: `ObligationGapCandidate` with trace-grounded recognized, planned,
  excluded, acted-on, and verified facts.
- Produces: offline reconstruction edges that are ineligible as confirmed
  Trace facts but eligible for candidate navigation.

- [ ] **Step 1: Write failing Pydantic and Seaborn omission tests**

Use minimal fixtures representing:

```text
dec_293: plan lists __get__ but omits known __set_name__ obligation
dec_147: dependency is recognized, then excluded because tests probably omit it
```

Assert both decisions enter the candidate pool with evidence refs and no
fabricated confirmed edge.

- [ ] **Step 2: Verify RED**

Run the new test module and confirm the two decisions are absent.

- [ ] **Step 3: Implement obligation coverage reconstruction**

Build obligation coverage from prompt/interface facts, authored plans, tool
actions, edits, and verification commands. Generate a gap only when a concrete
decision acknowledges, scopes, excludes, or closes the obligation.

- [ ] **Step 4: Integrate candidates before budget selection**

Merge omission candidates into canonical discovery with stable identity,
candidate source, causal path, and failure-signature binding. Candidate
clustering and paging must retain their provenance.

- [ ] **Step 5: Verify recall and safety**

Assert `dec_293` and `dec_147` are present, fabricated refs remain zero, and
candidate selection is deterministic under record-order permutation.

---

### Task 4: Root Role And Multi-Root Confirmation

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/global_judge.py`
- Modify: `tools/trace_attribution/trace_attribution/judgment_context.py`
- Modify: `tools/trace_attribution/trace_attribution/confirmation_path.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_global_judge.py`
- Test: `tools/trace_attribution/tests/test_recursive_acceptance_review.py`
- Test: `tools/trace_attribution/tests/test_factor_role_projection.py`

**Interfaces:**
- Produces roles: `defect_introduction_root`, `verification_omission`,
  `false_closure`, `downstream_materialization`, `amplifying_condition`,
  `external_interruption`.
- Preserves: root publication remains owned by independent confirmation.

- [ ] **Step 1: Write failing closure-vs-introduction tests**

Provide `dec_147 -> dec_159 -> verification omission -> dec_343`. Assert a
functional defect seed selects the introduction root and classifies `dec_343`
as false closure, while a separate acceptance seed may treat closure as root.

- [ ] **Step 2: Verify RED**

Run focused Global Judge and acceptance tests. Expected: current logic promotes
the closure node for the functional seed.

- [ ] **Step 3: Extend factual request context**

Include failure signature, obligation, plan, edit, verification, outcome,
competing candidates, and tiered paths. Reject requests that mix confirmed and
offline edges without provenance.

- [ ] **Step 4: Implement role and multi-root rules**

Require each root to prevent at least one independent failure signature.
Publish closure and verification omissions as factors unless the active seed
specifically describes acceptance/verification failure.

- [ ] **Step 5: Verify independent confirmation**

Run the focused role, Global Judge, and recursive acceptance suites. Expected:
no duplicate roots, no closure substitution, no unresolved confirmed refs.

---

### Task 5: Candidate Cluster Triage Activation

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/candidate_clustering.py`
- Modify: `tools/trace_attribution/trace_attribution/candidate_paging.py`
- Modify: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_candidate_clustering.py`
- Test: `tools/trace_attribution/tests/test_global_pagination_integration.py`

**Interfaces:**
- Consumes: existing zero-loss `CandidateClusterManifest`.
- Produces: cluster triage that always expands selected clusters back to
  original candidates before strict Judge evaluation.

- [ ] **Step 1: Add failing zero-loss triage tests**

Assert every human-root fixture remains in an expanded selected cluster and
fallback restores full paging whenever coverage proof is incomplete.

- [ ] **Step 2: Implement bounded triage**

Judge representatives only for navigation. A cluster can never be confirmed
as root. Persist selected and unselected cluster dispositions.

- [ ] **Step 3: Verify recall and page reduction**

Require 100% fixture root recall and at least 50% initial page reduction on the
Pydantic replay fixture.

---

### Task 6: Streaming Trace Finalization

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/src/observability/causal-ir.ts`
- Modify: `packages/opencode/src/observability/case-trace-html.ts`
- Modify: `packages/opencode/src/observability/repository-snapshot.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`
- Create: `packages/opencode/test/observability/case-trace-html.test.ts`

**Interfaces:**
- Preserves: Causal IR and trace.html user-visible semantics.
- Produces: bounded-memory artifact/snapshot/finalizer pipeline.

- [ ] **Step 1: Add failing large-trace memory/finalization tests**

Generate at least 5000 records with large artifacts. Assert finalization uses
streaming artifact refs, emits valid JSON/HTML, and does not duplicate payloads
in memory-visible projections.

- [ ] **Step 2: Verify RED**

Run focused Bun tests and confirm current implementation exceeds the structural
duplication bound or loads all payloads during HTML generation.

- [ ] **Step 3: Implement streaming storage and projection**

Write artifacts incrementally, snapshot indexes separately, and render HTML
from iterators/chunks. Keep signal handlers bounded and idempotent.

- [ ] **Step 4: Verify signal and compatibility behavior**

Assert SIGINT/SIGTERM produce readable HTML from partial state, legacy fields
remain readable, and Agent-visible projections are byte-identical.

---

### Task 7: Benchmark Replay, Regression And Acceptance

**Files:**
- Create: `.benchmark-runs/attribution-convergence-vnext-20260731/`
- Create: `docs/superpowers/reports/2026-07-31-attribution-convergence-vnext-results.md`
- Test: all `tools/trace_attribution/tests/test_*.py`
- Test: observability Bun tests

**Interfaces:**
- Consumes: existing Pydantic, Seaborn, and Sphinx traces, then fresh release
  traces after runtime changes.
- Produces: manual-vs-module comparison and acceptance decision.

- [ ] **Step 1: Replay existing traces without Provider**

Verify seed clustering, candidate recall, report replay, and zero fabricated
refs before making network calls.

- [ ] **Step 2: Run cold DeepSeek attribution**

After API balance is available, use isolated cache/checkpoint directories and
3600-second request timeout. Never send human root labels.

- [ ] **Step 3: Compare with manual analysis**

Report candidate recall, candidate reduction, root precision/recall, multi-root
coverage, role F1, unknown rate, requests, and first mismatch node.

- [ ] **Step 4: Run complete regression**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py' -q
bun test packages/opencode/test/observability
git diff --check
```

Expected: zero failures, zero errors, and no whitespace defects.

- [ ] **Step 5: Write acceptance report**

Record every target from the design specification. Unmet targets remain
explicit gaps; do not weaken validators or labels to declare success.
