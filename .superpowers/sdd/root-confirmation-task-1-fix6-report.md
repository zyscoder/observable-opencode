# Root Confirmation Stability Task 1 Fix Wave 6 Report

Base commit: `e21b13433`

## Scope

Closed the two remaining Task 1 cross-seed and complete-facts findings only.
Attribution remains offline, passive, and read-only. No agent feedback,
dependency, or Task 2 behavior was added.

## Root Causes

1. Confirmation request construction enumerated competing hypotheses across the
   entire ledger, and both runtime reconciliation and final report validation
   compared confirmation graphs across seeds. A same-fingerprint confirmation
   from another `start_ref` could therefore make a valid confirmed seed appear
   unresolved.
2. Candidate retrieval edges already omitted navigation confidence in one
   projection, but global capsule incoming/outgoing edges and recursive Judge
   edge contexts did not share that rule. Ranking-derived route confidence could
   consequently change a complete `GlobalCandidateJudgeRequest.to_dict()`.

## Fixes

1. Competitor construction now requires exact `seed_binding_identity` equality.
   Runtime confirmation reconciliation and final report root-graph validation
   compare only confirmations within the same full seed binding. Legacy
   comparison payloads without a redundant seed field remain valid when their
   target confirmation proves the same seed binding.
2. Added a shared Judge-edge factual sanitizer. It removes `score`,
   `retrieval_score`, and confidence explicitly identified by retrieval markers,
   ranking inference methods, or ranking edge origins. Confirmed trace edges and
   other authentic provenance retain their recorded confidence.
3. Global capsule candidate, incoming, and outgoing edges and recursive Judge
   edge contexts now use the same factual sanitizer.

## TDD Evidence

- Before the seed fix, a real retrieval-global analyzer with one shared defect
  fingerprint returned `evidence_gap` for seed A even though A's independent
  confirmation was confirmed and only seed B was unknown.
- The new checkpoint regression interrupts after the first durable confirmation,
  resumes, and requires A=`confirmed_root`, B=`evidence_gap`, and the top-level
  outcome=`partial` with no repeated completed confirmation.
- Before the confidence fix, swapping only ref A's two ranking-route confidence
  values changed the complete unfiltered global Judge request. The regression
  now directly compares the full request, full hypotheses, and confirmation
  `factual_dict()` values without deleting confidence in test code.
- The same regression proves ref B's confirmed trace provenance confidence
  remains present in both candidate and outgoing edge facts.

## Verification

- Focused seed/analyzer/retrieval/capsule/checkpoint suite: `164` tests passed.
- Full Python attribution suite: `583` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
