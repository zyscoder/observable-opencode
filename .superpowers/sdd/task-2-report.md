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

## Final Interface Closure

### RED

- A JavaScript/`as any` `responseClaim()` input omitting all five atomization facts wrote an incomplete `response.claim` record, node data payload, and node metadata payload.
- The initial legacy normalization used a summarized source string. A Unicode legacy input larger than 8 KB exposed a truncated `source_byte_range` and a group hash derived from the truncated text.
- The `claim_group_precedes` label still described ordering as being within one claim group even though the edge correctly joins adjacent claims from the same response segment regardless of group.

### GREEN

- `TraceResponseClaimRecord` and `ResponseClaimInput` now require `claim_group_id`, `claim_count`, `source_byte_range`, `atomization_status`, and `atomization_reason`; status is exactly `atomic | group_required | invalid_fragment`.
- `normalizeLegacyResponseClaimAtomizationFacts()` is an isolated runtime boundary. Valid atomized inputs bypass it unchanged; incomplete or invalid dynamic inputs receive a deterministic group ID, count `1`, the original string's UTF-8 byte range, `group_required`, and `legacy_response_claim_missing_atomization_facts`.
- The normal atomized path now verifies the five facts for every response-claim record, Causal IR node data payload, and node metadata payload. The legacy runtime regression verifies the same closure and journal replay for a long Unicode source.
- `claim_group_precedes` now labels the fact as `Adjacent claim/group order within a response segment`; relation, eligibility, and metadata semantics are unchanged.

### Verification

- Focused CaseTrace RED/GREEN tests: parenthetical normal closure and legacy runtime normalization, 2 pass.
- Full CaseTrace suite: 138 pass, 0 fail.
- Full Causal IR suite: 54 pass, 0 fail.
- Claim atomizer suite: 39 pass, 0 fail.
- `bun run typecheck`: passed.
- `git diff --check`: passed.

### Commit

`fix(trace): close Task 2 response claim interface`

### Concerns

- The compatibility boundary intentionally treats malformed dynamic values like missing values so persistence cannot produce a partial atomization closure. Statically typed callers must provide all five facts and retain their normal atomized values unchanged.
