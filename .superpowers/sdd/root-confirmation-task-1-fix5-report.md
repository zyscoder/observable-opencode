# Root Confirmation Stability Task 1 Fix Wave 5 Report

Base commit: `a54a13a8d`

## Scope

Implemented the final Task 1 provenance-envelope truncation correction only.
Attribution remains offline, passive, and read-only. No dependencies or other
task behavior changed.

## Root Cause

`bound_provenance_envelopes` applied `PROVENANCE_ENVELOPE_LIMIT` to individual
routes before factual provenance was canonicalized. Two routes for one resolved
ref could therefore consume both slots and suppress a different ref, or a
higher-confidence ordering could retain only the synthetic route for a ref.

## Fix

1. Provenance envelopes are now grouped by resolved ref. The limit ranks and
   selects refs using each group's maximum navigation score, then keeps every
   route for every selected ref until canonicalization.
2. Rank merging also groups on resolved refs and retains all routes for each
   selected ref. Any optional bound now limits distinct refs rather than routes.
3. A standard production-retriever and real fusion-analyzer regression records
   two routes for A (authentic and synthetic) plus one higher-scored route for
   B. Swapping A route confidences preserves the complete normalized global
   capsules, hypotheses, and confirmation facts. The selected A capsule keeps
   its authentic route, while B still precedes A by navigation score.

## TDD Evidence

Before the production change, the new regression failed because the global
candidate pool contained only `change` and B: A was discarded when its two
provenance-envelope routes consumed the route-count limit. No A confirmation
request was queued. After grouping before limiting, the same regression passes
with A canonicalized from its authentic recorded route.

## Verification

- Focused Task 1 retrieval/analyzer/seed/state suite: `152` tests passed.
- Full Python attribution suite: `582` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
