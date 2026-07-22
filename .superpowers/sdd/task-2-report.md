# Task 2 Report: Claim Group Projection

## Status

Completed.

## RED

- CaseTrace RED: the parenthetical `_cstack` final response emitted two claims without `next_claim_ref` / `previous_claim_ref`.
- Causal IR RED: `response_to_claim_group` and `claim_group_precedes` replayed as unknown `derived_from` relations.

## Implementation

- Preallocate final-response claim IDs before emission and derive record references from the projected record identity: `record:responseclaim_<claim_id>`.
- Preserve Task 1 `previous_claim_key` / `next_claim_key` and add the corresponding record references.
- Project group, count, byte range, record references, and atomization fields to the Trace record, Causal node payload, and node metadata.
- Retain `response_to_claim`; add `response_to_claim_group` with `claim_group_id` metadata.
- Add `claim_group_precedes` only for adjacent claims in the same segment and same group. Its top-level and metadata attribution eligibility are false, with `causal_semantics=claim_group_order_only` and `behavior_impact=none`.
- Register both relations as formal dataflow relations so journal replay preserves them without unknown-relation fallback.

## GREEN

- Focused CaseTrace claim group tests: 2 pass.
- Focused Causal IR claim group replay test: 1 pass.
- Focused `claim|journal` suite: 79 pass, 0 fail.
- Full Causal IR suite: 54 pass, 0 fail.
- Claim atomizer regression suite: 39 pass, 0 fail.
- Full CaseTrace suite completed with all 135 test lines passing.
- Typecheck: `tsgo --noEmit` passed.
- `git diff --check` passed.

## Commit

`feat(trace): project claim group provenance into causal IR`

## Files

- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/src/observability/trace-semantic-contract.ts`
- `packages/opencode/test/observability/case-trace.test.ts`
- `packages/opencode/test/observability/causal-ir.test.ts`
- `.superpowers/sdd/task-2-report.md`

## Concerns

- Task 1 assigns distinct `claim_group_id` values to independent atomic statements. Consequently, a two-statement response with separate groups has no `claim_group_precedes` edge by design. The implementation will preserve and replay the ordering edge when adjacent claims share a group, without changing atomizer semantics.
- Existing Causal IR aliasing already provides `response_claim:<claim_id>` and generic journal replay already preserves arbitrary payload and metadata fields, so no direct `causal-ir.ts` production change was required.

## Review Fix: Fact Closure

### RED

- The generated parenthetical final response produced two independent claims but zero `claim_group_precedes` edges because emission required equal `claim_group_id` values.
- A replayed `claim_group_precedes` edge retained `eligible_for_attribution: false` in metadata but the compatibility `dataflow_edges` projection omitted the top-level field.

### GREEN

- Adjacent claims from one response segment now produce exactly one order edge regardless of their distinct claim groups; explicit cross-segment claims and a generated single-claim final response produce none.
- The production CaseTrace -> journal -> replay test verifies both response.claim nodes' data and metadata, byte ranges, previous/next record refs, order-edge semantics, and attribution isolation.
- The replayed compatibility projection retains `eligible_for_attribution: false` both at the dataflow-edge top level and in metadata.
- Verified: focused `claim|journal` suite (80 pass); CaseTrace suite (136 pass); Causal IR suite (54 pass); claim atomizer suite (39 pass); `bun run typecheck`; `git diff --check`.

### Commit

`fix(trace): close claim group projection review gaps`
