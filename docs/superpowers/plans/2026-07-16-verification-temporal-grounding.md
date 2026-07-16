# Verification Temporal Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep superseded verification facts visible as historical context while preventing them from directly supporting a current final Claim.

**Architecture:** Propagate lifecycle state from canonical verification records to their observation and semantic-fact projections. Filter verification candidates by temporal applicability and status before Claim evidence ranking, then add attribution-ineligible advisory edges for rejected historical evidence.

**Tech Stack:** TypeScript, Causal IR 1.0, Bun test, Python 3 unittest, Anthropic-compatible offline judge, HTTP stress runner.

## Global Constraints

- Trace collection remains passive and never changes Agent-visible inputs or execution behavior.
- Unknown lifecycle state remains unknown instead of being inferred from event proximity.
- Superseded facts remain inspectable but cannot create attribution-eligible Claim support edges.
- Historical Claims may use historical verification facts when their status matches.
- Existing Trace and Causal IR identities remain stable.

---

### Task 1: Verification Lifecycle Projection

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `verificationProvenanceForRefs(refs, spanID)` and lifecycle fields on observation/evidence nodes.
- Consumes: `TraceVerificationRecord` and canonical source refs.

```ts
type VerificationFactProvenance = {
  verification_refs: string[]
  verification_repository_revision?: number
  verification_phase?: TraceVerificationRecord["verification_phase"]
  verification_status?: TraceVerificationRecord["status"]
  verification_effective_for_final_state?: boolean
  verification_temporal_role: "current_effective" | "superseded" | "unknown"
  verification_supersedes_refs?: string[]
  verification_superseded_by_refs?: string[]
}
```

- [ ] Add a fixture containing a baseline failure, repository change, and post-change success for the same command.
- [ ] Assert the baseline and post-change evidence facts expose their verification refs, revision, phase, status, and effective state.
- [ ] Run the focused test and confirm it fails because derived facts currently omit lifecycle fields.
- [ ] Implement transitive verification-ref resolution with a bounded source-ref walk and exact span fallback.
- [ ] Synchronize matching observation and evidence nodes whenever `syncVerificationNode()` changes canonical lifecycle state.
- [ ] Run the focused test and confirm it passes.

### Task 2: Temporally Eligible Claim Grounding

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `rejected_inapplicable` grounding decisions and candidate temporal metadata.
- Consumes: lifecycle fields produced by Task 1.

```ts
type VerificationCandidateRejection =
  | "superseded_verification"
  | "verification_revision_mismatch"
  | "verification_status_mismatch"
```

- [ ] Assert a current passed Claim selects only the post-change fact and effective verification record.
- [ ] Assert the baseline fact has rejection reason `superseded_verification` and appears in `superseded_evidence_refs`.
- [ ] Assert the baseline fact has no attribution-eligible `evidence_to_claim` edge.
- [ ] Add a historical baseline-failure Claim assertion that retains the old failure as direct support.
- [ ] Run the focused test and confirm the current Claim assertions fail before implementation.
- [ ] Filter current verification candidates by effective state, repository revision, and explicit status compatibility before ranking.
- [ ] Add candidate lifecycle fields and `attribution_eligible` to grounding decisions.
- [ ] Run focused tests and confirm both current and historical Claim behavior passes.

### Task 3: Historical Advisory Edge

**Files:**
- Modify: `packages/opencode/src/observability/case-trace.ts`
- Test: `packages/opencode/test/observability/case-trace.test.ts`

**Interfaces:**
- Produces: `context_to_claim` edges with `superseded_verification_context` metadata.
- Consumes: `superseded_evidence_refs` from Task 2.

```ts
this.causalEdge({
  from: parsed,
  to: { type: "response_claim", id: claimNodeID },
  relation: "context_to_claim",
  evidence_tier: "temporal_advisory",
  eligible_for_attribution: false,
  metadata: { causal_semantics: "superseded_verification_context" },
})
```

- [ ] Assert the old fact retains an edge to the current Claim for HTML inspection.
- [ ] Assert `evidence_tier=temporal_advisory` and `eligible_for_attribution=false`.
- [ ] Implement a dedicated superseded-context edge helper without reusing direct support linking.
- [ ] Run the focused test and verify the direct and advisory paths are distinct.

### Task 4: Offline Attribution Compatibility

**Files:**
- Modify: `tools/trace_attribution/trace_attribution/models.py`
- Test: `tools/trace_attribution/tests/test_backward_taint.py`

**Interfaces:**
- Consumes: temporal grounding decisions and advisory edges.
- Produces: preserved structured semantics without traversing superseded evidence as a defect predecessor.

```python
for key in (
    "verification_refs",
    "verification_repository_revision",
    "verification_effective_for_final_state",
    "verification_temporal_role",
):
    assert key in compact["data"]
```

- [ ] Add a compact trace fixture containing one selected current fact and one rejected superseded fact.
- [ ] Assert graph construction preserves candidate temporal fields.
- [ ] Assert backward predecessors exclude the advisory superseded edge.
- [ ] Run the full attribution suite.

### Task 5: Regression and Stress Validation

**Files:**
- Create: `docs/superpowers/reports/2026-07-16-verification-temporal-grounding-iteration.md`

**Interfaces:**
- Consumes: current native binary and HTTP session API.
- Produces: manual-versus-offline attribution comparison for successful and failing cases.

- [ ] Run the complete CaseTrace and Causal IR suites.
- [ ] Run the complete offline attribution suite and TypeScript typecheck.
- [ ] Build the native macOS binary.
- [ ] Run a successful requirement-conflict repair through `serve -> session -> message`.
- [ ] Run an intentionally failing verification case through the same path.
- [ ] Confirm the successful pass Claim excludes baseline failure direct support.
- [ ] Compare manual and offline attribution for both cases.
- [ ] Write the iteration report with remaining semantic gaps and storage observations.
- [ ] Stage and commit only files belonging to this iteration.
