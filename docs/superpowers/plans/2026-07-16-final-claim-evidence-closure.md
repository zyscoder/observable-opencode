# Final Claim Evidence Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close final Claim evidence provenance from confirmed model context and preserve contradictory propagation nodes as confirmable defect-introduction boundaries.

**Architecture:** Reconstruct passive grounding candidates from confirmed generation context sets, reuse the existing deterministic semantic matcher, and store selected/rejected decisions on response claims and support assessments. Extend backward taint post-processing to promote only fully evaluated propagation contradictions to provisional roots, subject to root confirmation.

**Tech Stack:** TypeScript, Causal IR 1.0, Bun test, Python 3 unittest, Anthropic-compatible offline judge, HTTP stress runner.

## Global Constraints

- Trace-derived data never enters Agent input or changes execution.
- Confirmed context presence does not assert Agent attention or reasoning.
- Rejected candidates create no attribution-eligible edge.
- Temporal advisory edges remain attribution ineligible.
- Missing evidence remains unsupported/inconclusive instead of being guessed.

---

### Task 1: Persist the Current Two-Phase Baseline

**Files:** Existing phase-one and phase-two implementation and reports.

- [x] Run Causal IR, CaseTrace, attribution, and typecheck regressions.
- [x] Stage only phase-one and phase-two files, excluding unrelated reports.
- [x] Commit the verified baseline as `988296fad`.

### Task 2: Confirmed Generation Evidence Candidates

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `generationEvidenceCandidates(provenance)` with confirmed tool, evidence, verification, and change refs.
- Consumes: final response generation provenance and confirmed context-set membership.

- [ ] Add a failing HTTP-shaped trace fixture whose final response has no explicit source refs.
- [ ] Assert evidence derived from selected tool results appears as generation grounding candidates.
- [ ] Assert evidence outside the selected generation context is excluded.
- [ ] Implement candidate reconstruction without writing to Agent-visible state.
- [ ] Run the focused test and confirm it passes.

### Task 3: Per-Claim Grounding Decisions

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Modify: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `grounding_candidate_refs`, `grounding_decisions`, `grounding_method`, and `grounding_behavior_impact`.
- Produces: attribution edges only for selected direct support refs.

- [ ] Extend the failing fixture with one matching and one unrelated candidate.
- [ ] Assert the matching candidate is selected and linked to the Claim.
- [ ] Assert the unrelated candidate records `rejected_no_match` and has no Claim edge.
- [ ] Implement deterministic decision records using existing match scores and reasons.
- [ ] Add grounding decisions to `claim.support_assessment`.
- [ ] Run focused Claim tests and confirm they pass.

### Task 4: First-Observed Defect Boundary

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/analyzer.py`
- Modify: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: a present `defect_propagation` judgment and all declared defect predecessors.
- Produces: a provisional `defect_introduction` candidate only when every predecessor is absent.

- [ ] Add a failing test where a propagation node points only to absent defect predecessors.
- [ ] Assert it becomes a root candidate and is listed as a first-observed boundary.
- [ ] Add tests proving unknown/unvisited predecessors prevent promotion.
- [ ] Add a confirmer-rejection test proving the candidate is removed.
- [ ] Implement the minimal deterministic boundary promotion.
- [ ] Run the full attribution suite.

### Task 5: Full Regression and Real Semantic Cases

**Files:**
- Create: `docs/superpowers/reports/2026-07-16-final-claim-evidence-closure.md`

**Interfaces:**
- Consumes: before/after traces for three semantic stress cases.
- Produces: coverage, reachability, attribution, size, and edge metrics.

- [ ] Run Causal IR, CaseTrace, FeatureBench runner, stress, attribution, and typecheck suites.
- [ ] Build a native macOS binary from the current source.
- [ ] Run `semantic-requirement-priority`, `semantic-architecture-boundary`, and `semantic-verification-depth` through HTTP.
- [ ] Compare Claim evidence coverage, quality score, unresolved refs, temporal eligibility, edges, and bytes.
- [ ] Run the offline attribution module on at least one quality-gap trace.
- [ ] Write the report and reject the iteration if passive behavior or causal reachability regresses.
