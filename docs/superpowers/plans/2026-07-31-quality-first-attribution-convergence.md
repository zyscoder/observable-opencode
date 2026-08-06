# Quality-First Attribution Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make offline causal attribution converge reliably under strict semantic validation, improve non-root role expressiveness, and validate the result repeatedly on open-source benchmark traces.

**Architecture:** Keep trace capture and Agent execution unchanged. Extend only the offline Judge orchestration with a bounded progress-aware semantic repair loop, then revise the factor-role fact model only if cross-case evidence shows the current necessity/role matrix loses valid causal facts. Evaluate cold runs with isolated caches and compare every prediction against separately maintained human labels.

**Tech Stack:** Python 3.9+, `unittest`, Anthropic-compatible DeepSeek API, Causal IR JSON, observable-opencode semantic traces.

## Global Constraints

- Attribution remains passive, offline, and invisible to the Agent.
- Never use benchmark labels as Judge input, candidate selection input, or repair input.
- Preserve strict grounding: fabricated refs, unresolved confirmed facts, and duplicate identities must remain zero.
- Prefer attribution quality and convergence over request cost; a case may use up to 256 physical Judge requests.
- Each Provider request timeout remains 3600 seconds.
- Every production behavior change follows red-green-refactor TDD.
- Do not weaken root confirmation, grounding, or report validation to improve metrics.

---

### Task 1: Progress-Aware Semantic Repair Loop

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Test: `tools/trace_attribution/tests/test_causal_judge.py`
- Test: `tools/trace_attribution/tests/test_global_judge.py`

**Interfaces:**
- Consumes: `_request_validated(...)`, `_repair_constraints(...)`, `max_physical_requests`.
- Produces: a bounded validation loop that can use up to six attempts when the caller budget permits and records the complete ordered validation-error history.

- [x] **Step 1: Add a failing test for fourth-attempt convergence**

Create a scripted Global Judge sequence whose first three responses fail with different semantic validation errors and whose fourth response is valid. Assert that `judge_candidates_bounded(..., max_physical_requests=6)` returns the fourth response with `physical_requests == 4`.

- [x] **Step 2: Add a failing test for repeated-error termination**

Return the same invalid semantic combination repeatedly. Assert that the loop stops once the same normalized validation error repeats without semantic progress and does not consume all six requests.

- [x] **Step 3: Add a failing test for budget authority**

Call the same sequence with `max_physical_requests=3`. Assert that no fourth call occurs and the error states that the caller budget was exhausted.

- [x] **Step 4: Implement the bounded loop**

Replace the fixed initial/focused/full sequence with a loop that:

```python
MAX_SEMANTIC_REPAIR_ATTEMPTS = 6
```

Each retry must include the latest invalid output, ordered validation history, canonical request facts, stage-specific constraints, and the original prompt for non-global stages. Stop on valid output, caller budget exhaustion, Provider failure, or repeated normalized error with no progress.

- [x] **Step 5: Run targeted tests**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_judge \
  tools.trace_attribution.tests.test_global_judge -q
```

Expected: all tests pass.

---

### Task 2: Factor Necessity and Role Expressiveness

**Files:**
- Modify if justified: `tools/trace_attribution/trace_attribution/causal_state.py`
- Modify if justified: `tools/trace_attribution/trace_attribution/causal_judge.py`
- Modify if justified: `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- Test: `tools/trace_attribution/tests/test_factor_role_projection.py`
- Test: `tools/trace_attribution/tests/test_recursive_acceptance_review.py`

**Interfaces:**
- Consumes: `FACTOR_ROLE_CONTRACT`, `FactorRoleJudgment`, canonical factor-role publication.
- Produces: a representation that preserves independently judged necessity and non-root role without allowing a non-root judgment to publish a second confirmed root.

- [x] **Step 1: Review cross-case facts before changing the contract**

Compare Sphinx `record:change` with at least one Pydantic and one Seaborn materialization candidate. Record whether “necessary + downstream_materialization” is semantically useful across cases.

- [x] **Step 2: Add failing contract tests if evidence supports decoupling**

Assert that `necessity_status="necessary"` can coexist with `factor_role="downstream_materialization"` while root publication remains owned exclusively by independent root confirmation.

- [x] **Step 3: Implement the smallest contract extension**

Add only evidence-supported matrix rows. Keep `factor_role` and `necessity_status` factual; do not infer a root from necessity alone.

- [x] **Step 4: Verify publication and evaluator behavior**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_factor_role_projection \
  tools.trace_attribution.tests.test_recursive_acceptance_review -q
```

Expected: all tests pass, with no duplicate root publication.

---

### Task 3: Repeated Open-Source Benchmark Validation

**Files:**
- Create: `.benchmark-runs/quality-first-convergence-20260731/`
- Create: `docs/superpowers/reports/2026-07-31-quality-first-attribution-convergence-results.md`

**Interfaces:**
- Consumes: Sphinx fixture, open-source Pydantic and Seaborn traces, separate human label files.
- Produces: cold-run attribution reports, evaluator comparisons, convergence metrics, and manual-versus-module analysis.

- [x] **Step 1: Run Sphinx at least three cold times**

Use a distinct output and cache directory for each run. Record valid-root convergence, physical requests, role F1, unknown rate, and safety counters.

- [ ] **Step 2: Run representative Pydantic and Seaborn cases**

Use one labeled semantic defect seed from each trace. Do not send labels to the Judge.

- [ ] **Step 3: Compare module output with manual backward taint analysis**

For every case, identify the terminal defect, candidate introduction set, selected root, causal factors, unresolved facts, and the first node that introduced each mismatch.

- [x] **Step 4: Write the result report**

Report best, worst, and median behavior. Do not characterize one successful sample as stable convergence.

---

### Task 4: Full Regression and Isolation Verification

**Files:**
- Test: `tools/trace_attribution/tests/test_offline_behavior_isolation.py`
- Test: all `tools/trace_attribution/tests/test_*.py`

**Interfaces:**
- Consumes: final implementation from Tasks 1-3.
- Produces: complete regression evidence and behavior-isolation evidence.

- [x] **Step 1: Run offline behavior isolation**

Assert original trace bytes and Agent-visible message, Provider, tool, and final-answer surfaces are unchanged after attribution.

- [x] **Step 2: Run the full test suite**

Run:

```bash
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py' -q
```

Expected: zero failures and zero errors.

- [x] **Step 3: Record acceptance metrics**

Target valid-attribution convergence `>= 90%` over repeated labeled runs, root Top-1/recall/precision `>= 0.85`, factor-role F1 `>= 0.65`, unknown rate `<= 0.15`, and all safety counters equal to zero. Mark unmet targets as remaining work rather than weakening validation.
