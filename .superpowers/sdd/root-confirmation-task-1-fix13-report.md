# Root Confirmation Stability Task 1 Fix Wave 13 Report

Base commit: `09ff3ab31`

## Scope

Closed the remaining per-seed outcome/payload consistency gaps in Task 1.
The attribution flow remains offline, passive, read-only, and dependency-free.
No Task 2 behavior or dependency surface changed.

## Fix

`SeedAttributionBuilder` now suppresses published root references and the
confirmed root-owning identities whenever unresolved evidence makes its terminal
outcome conservative. Confirmed branch records remain in the diagnostic
confirmation history, while report construction moves their roots into explicit
unresolved diagnostic state instead of raising a `ValueError`.

`SeedAttributionResult` and the evaluator now share one outcome/payload
validator. `confirmed_root` and `no_defect` reject missing evidence or blockers;
`evidence_gap` requires a concrete unresolved fact; and non-confirmed outcomes
cannot publish confirmed root references. A blocker without supplied detail is
given an explicit unresolved-evidence fact by the builder.

Checkpoint restoration rebuilds the non-published confirmed-identity tracking
from the durable confirmation history, preserving the same conservative result
after resume.

## TDD Coverage

1. RED: an unresolved builder retained a confirmed root reference and identity.
2. RED: direct construction, `from_dict`, and evaluator parsing accepted
   contradictory outcome/payload combinations.
3. RED: global inconclusive followed by recursive confirmation, and mixed
   branch failure/confirmation in one seed, raised the non-confirmed root
   ownership `ValueError`.
4. GREEN: both analyzer cases now return valid conservative reports with
   diagnostic confirmation history and no published roots.

## Verification

- Focused seed, recursive analyzer, benchmark, causal-state, and acceptance
  suites: `194` tests passed.
- Full Python attribution suite: `608` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
