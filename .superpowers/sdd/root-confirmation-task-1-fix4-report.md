# Root Confirmation Stability Task 1 Fix Wave 4 Report

Base commit: `12e0586963cec9a01845f6df65739c828bc060d6`

## Scope

Implemented the three directed Task 1 corrections only. Attribution remains
offline, passive, and read-only. No Task 2 behavior or dependencies changed.

## Fixes

1. The production semantic predecessor retriever now retains every route for a
   selected ref until route provenance has been canonicalized. Route groups use
   their maximum navigation score only to rank/select refs; their semantic
   source, relation, and factual edge are selected deterministically from the
   complete route group. This prevents a higher-scored duplicate route from
   changing global comparison, hypothesis, or root-confirmation facts while
   preserving recorded graph provenance.
2. Modern v3 report construction validates that every `seed_results` member is
   a `SeedAttributionResult` before sorting. Modern `from_dict` now rejects a
   non-object member with an explicit `TypeError` rather than filtering it.
   The evaluator continues to reject the same malformed item at schema
   validation. Legacy v2 migration retains its existing conservative handling.
3. Added a positive end-to-end transformed-defect regression. A real recursive
   analyzer follows recorded defect transformation lineage, queues and completes
   an independent root confirmation, and produces a report accepted by direct
   construction, strict parsing, and the trace-backed evaluator.

## TDD Evidence

Before production edits, the three directed regressions failed as expected:

- Two recorded routes for the same ref in the standard retriever changed
  normalized global, hypothesis, and confirmation facts when their navigation
  confidences were swapped.
- Direct v3 construction raised an incidental `AttributeError` while parsing
  silently filtered a non-object seed entry.
- The transformed lineage fixture did not yet issue the real
  `request_root_confirmation` control needed to enter the confirmation path.

The completed tests use the standard retriever and a real analyzer flow. The
route-stability comparison excludes navigation score/confidence fields while
leaving recorded provenance untouched; confidence remains trace provenance and
is not fabricated or rewritten.

## Verification

- Focused Task 1 retrieval/capsule/seed/state suite: `66` tests passed.
- Full Python attribution suite: `581` tests passed.
- Isolated-pycache `compileall` and `git diff --check` passed.

## Remaining Risk

Route ranking still intentionally changes which distinct refs are selected or
their order. The fix constrains only duplicate routes for one selected ref, so
the analyzer does not treat navigation confidence as causal provenance.
