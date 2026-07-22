# Root Confirmation Stability Task 1 Fix Wave 7 Report

Base commit: `f263a0d40`

## Scope

Closed the remaining frontier visit identity gap only. The attribution path remains
offline, passive, and read-only; no dependency or unrelated behavior changed.

## Root Cause

`AttributionHypothesis` bound its identifier to a seed, but its semantic hash
intentionally did not. `FrontierItem.visit_key` used that semantic hash together
with the node and defect fingerprint, while omitting `seed_binding_identity`.
Consequently, two seeds with the same fingerprint could reach the same root through
different earlier hypotheses, then produce equal root visit keys. `RecursiveFrontier`
discarded the second root item before its root step and independent confirmation.

## Fix

1. Added `seed_binding_identity` to `semantic_visit_key` and persisted it on
   `FrontierItem`, including its item identity and checkpoint payload.
2. Passed the exact hypothesis seed binding into initial frontier entries, recursive
   predecessors, navigation predecessors, and frontier hypothesis migrations.
3. Kept the defect semantic fingerprint unchanged; seed binding is traversal and
   confirmation identity, not defect semantics.

## TDD Evidence

- Added a real recursive-analyzer regression with two different start refs, one
  shared defect fingerprint, a shared intermediate node, and a shared root reached
  after two hops.
- Before the fix it failed with one root step instead of two (`1 != 2`), proving
  the second root frontier visit was deduplicated.
- The regression now requires two root steps, two confirmations with distinct seed
  bindings, and two `confirmed_root` seed results.
- Its interruption/resume variant crashes after the first durable confirmation,
  requires the resumed report to equal uninterrupted output, and proves the resumed
  judge performs only the remaining confirmation.

## Verification

- Focused seed/frontier/state/recursive-analyzer suite: `149` tests passed.
- Full Python attribution suite: `584` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
