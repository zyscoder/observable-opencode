# Root Confirmation Stability Task 1 Fix Wave 12 Report

Base commit: `045a0e7c3`

## Scope

Closed the remaining same-`start_ref` composite root-owner ambiguity in Task 1.
The attribution flow remains offline, passive, read-only, and dependency-free.
No Task 2 behavior or dependency surface changed.

## Fix

Published-root ownership now resolves through the full seed binding identity and
defect lineage. A root confirmation must identify exactly one `confirmed_root`
seed whose `(start_ref, defect_fingerprint)` binding, terminal path, and defect
lineage agree. A same-`start_ref` non-confirmed sibling whose fingerprint is
also reached by the published root's defect lineage is rejected rather than
being accepted through a collapsed start-ref set.

The existing `observed_defect_refs` report-seed projection remains a provenance
constraint only; it is no longer sufficient to establish owner identity.

## TDD Coverage

1. RED: direct construction and `from_dict` accepted both `no_defect` and
   `evidence_gap` same-start-ref siblings whose distinct fingerprint matched the
   published root lineage.
2. GREEN: both entry points now reject the ambiguous composite owner.
3. Added a positive round-trip case where the same-start-ref sibling has a
   distinct, non-overlapping lineage and the confirmed owner remains uniquely
   identified.
4. Added evaluator coverage that rejects the serialized same-start-ref,
   lineage-matching `no_defect` sibling before scoring.

## Verification

- Focused seed, benchmark metric, causal-state, and checkpoint suites: `122`
  tests passed.
- Full Python attribution suite: `603` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
