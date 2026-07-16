# Verification Temporal Grounding Design

## Goal

Prevent superseded or repository-revision-incompatible verification facts from
becoming direct support for a current final Claim, while preserving those facts
as inspectable historical context for offline analysis.

The implementation remains a passive sidecar. It must not change prompts,
context selection, model requests, tool inputs, repository state, or Agent loop
behavior.

## Observed Defect

`TraceVerificationRecord` already stores `repository_revision`,
`verification_phase`, `effective_for_final_state`, and supersession refs. A
verification tool result is also projected into an execution observation and a
`verification_output` semantic fact. Those derived nodes currently omit the
verification lifecycle fields.

Final Claim grounding therefore sees two textually relevant facts:

- a baseline test failure at repository revision 0; and
- a post-change test pass at repository revision 1.

For a current Claim such as "npm test passed", both facts can score above the
semantic-match threshold. The old failure then becomes direct support even
though its verification record is explicitly superseded.

## Considered Approaches

### A. Match-Time Filtering Only

Traverse from each candidate to its verification record while grounding the
Claim and reject stale candidates. This is small, but HTML and offline consumers
still receive evidence facts without temporal meaning.

### B. Propagated Verification Provenance

Project lifecycle fields onto verification observations and facts, synchronize
them when a verification is superseded, and enforce temporal eligibility during
Claim grounding. Preserve rejected historical facts through explicit advisory
edges.

This is the selected approach because both online trace rendering and offline
attribution receive the same self-contained semantics.

### C. Replace Verification Facts With One Specialized Node Type

Remove generic `verification_output` facts and make all consumers use the
verification lifecycle node. This is conceptually cleaner but has a larger
compatibility and migration surface than this iteration requires.

## Data Model

Verification-derived observations and facts receive:

- `verification_refs`
- `verification_repository_revision`
- `verification_phase`
- `verification_status`
- `verification_effective_for_final_state`
- `verification_temporal_role`: `current_effective`, `superseded`, or `unknown`
- `verification_supersedes_refs`
- `verification_superseded_by_refs`

Grounding decisions additionally receive the candidate lifecycle fields and
`attribution_eligible`.

`rejected_inapplicable` is added as a grounding decision. Its reasons are:

- `superseded_verification`
- `verification_revision_mismatch`
- `verification_status_mismatch`

## Grounding Rules

For a current verification Claim:

1. Prefer facts linked to an effective verification at the current repository
   revision.
2. Require the verification status to agree with an explicit passed or failed
   Claim.
3. Exclude superseded, wrong-revision, and status-incompatible candidates before
   semantic ranking.
4. Preserve candidates without observable verification provenance as unknown;
   do not invent lifecycle metadata. When at least one canonical verification
   candidate exists, reject unscoped shell facts as
   `unscoped_verification_candidate` instead of using a successful setup command
   to prove a test-result Claim.

For an explicitly historical Claim such as "the baseline test failed",
superseded facts remain eligible when their status matches the Claim.

The existing mixed Claim lanes remain in force: a Claim may independently select
change, verification, and requirement evidence.

## Causal Edges

Selected current evidence keeps the existing attribution-eligible
`evidence_to_claim` or `execution_to_claim` edge.

Rejected superseded verification evidence receives a `context_to_claim` edge
with:

- `evidence_tier=temporal_advisory`
- `eligible_for_attribution=false`
- `causal_semantics=superseded_verification_context`

This makes the baseline visible in Trace HTML without allowing backward taint to
cross it as if it proved the current result.

## Offline Attribution

The offline graph loader preserves the new lifecycle and decision fields. The
judge prompt is not changed unless a real stress trace shows that it ignores the
structured temporal semantics.

Attribution should:

- report `no_defect` for a fully supported successful answer;
- avoid citing a superseded failure as proof of a current pass;
- use the current failed verification as a defect outcome in an intentionally
  failing case; and
- remain inconclusive when no current verification exists.

## Verification Plan

1. Add a red test with baseline failure, repository change, post-change pass,
   and a final current-pass Claim.
2. Assert only the post-change fact and verification are direct support.
3. Assert the baseline fact is `rejected_inapplicable`, listed as superseded,
   and connected only through an attribution-ineligible advisory edge.
4. Add a historical-Claim test proving the baseline failure remains usable.
5. Run CaseTrace, Causal IR, attribution, and typecheck suites.
6. Run one successful and one intentionally failing case through the HTTP
   session path, then compare manual and offline attribution results.
