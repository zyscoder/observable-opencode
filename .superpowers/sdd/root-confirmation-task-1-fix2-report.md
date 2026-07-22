# Root Confirmation Stability Task 1 Fix Wave 2 Report

Base commit: `892590d179247d37ab8e129f61e45b936d872c67`

## Scope

Implemented the two directed Task 1 fixes only. Attribution remains offline,
passive, and read-only. No agent behavior or dependencies changed.

## Fixes

1. Modern v3 confirmed-seed deserialization now binds a seed to top-level roots
   and confirmations through a complete defect identity chain. The confirmation
   fingerprint must equal the top-level root fingerprint, and that root's
   verified transformed-defect lineage must lead back to the seed fingerprint.
   This rejects a same-`start_ref` copied seed with a fresh internally valid
   defect state while preserving valid upstream defect transformations.
2. Candidate evidence capsules now remove retrieval-rank fields (`confidence`,
   `score`, and `retrieval_score`) from `candidate.retrieval_edge`. Trace edge
   confidence remains in the independently grounded incoming/outgoing edge
   provenance.

## TDD Evidence

Before production edits, two adversarial tests failed as expected:

- The direct report parser and evaluator accepted a new internally valid seed
  fingerprint while reusing the old top-level root and confirmation.
- Changing both `candidate.score` and `candidate.edge.confidence` changed the
  normalized global factual payload on the real retrieval-global fusion path.

The fusion regression builds two independently confirmed competing roots and
compares complete normalized global Judge facts and complete independent
root-confirmation competitor facts across both rank settings. It also verifies
that trace provenance confidence survives outside the retrieval edge.

## Verification

- Focused Task 1 state/Judge/analyzer/evaluator suite: `286` tests passed.
- Full Python attribution suite: `571` tests passed.
- `compileall` and `git diff --check` passed.

## Remaining Risk

The parser requires a transformed root's parent defect states to be available
in the report's retained defect lineage. A report that omits a required
intermediate lineage state fails closed rather than guessing a seed binding.
