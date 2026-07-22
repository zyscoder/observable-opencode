# Root Confirmation Stability Task 1 Fix Wave 10 Report

Base commit: `abcd91c56`

## Scope

Closed the final Task 1 report and migration integrity findings. The attribution
flow remains offline, passive, read-only, and dependency-free. No Task 2 behavior
or dependency surface changed.

## Root Cause

Report validation checked each `confirmed_root` seed against its referenced roots,
but did not reconcile published roots back to exactly one owning seed. A top-level
root could therefore remain orphaned, and a non-confirmed seed could retain its
published confirmation identity.

The v1 frontier migration recursively rewrote visit keys and context-derived hashes
inside replay action records, but retained the old journal `previous_hash` and
`record_hash` values. Modern frontier deserialization also treated absent `item_id`
and `visit_key` as permission to derive them from semantic fields. Finally, report
parsing accepted an absent `schema_version` as modern input, including checkpoint
restoration paths.

## Fix

1. Added bidirectional root ownership reconciliation. Every published primary or
   co-root confirmation identity must be owned by exactly one `confirmed_root` seed;
   non-confirmed seeds cannot retain identities that own published roots. This also
   rejects all-`no_defect` reports that still publish top-level roots in direct
   construction, parsing, and evaluator safety validation.
2. Migrated the complete action journal in sequence order and deterministically
   rebuilt every `previous_hash` and `record_hash`. The regression fixture now uses
   real two-record hashed journals and proves valid chaining plus convergence with
   native v2 replay records.
3. Required non-empty persisted `item_id` and `visit_key` for native v2 frontier
   items, with exact semantic-field matching. Legacy v1 migration retains its
   separate verified identity path.
4. Required an explicit supported report schema. Schema-less direct parsing and
   checkpoint report restoration now fail closed, consistent with the evaluator.
   Explicit v2 reports still migrate conservatively, but unowned historical roots
   are unpublished and retained as unresolved migration facts rather than projected
   into v3 root output.

## TDD Coverage

- Added adversarial direct/from-dict tests for orphaned roots, non-confirmed identity
  retention, and all-`no_defect` seeds with published roots.
- Added evaluator coverage for the same contradictory aggregate state.
- Added native v2 frontier tests for missing `item_id` and missing `visit_key`.
- Reworked v1 migration convergence coverage around real hashed journal records and
  asserted every link and record digest.
- Added schema-less direct parser and checkpoint restoration rejection tests, plus
  positive explicit-v2 conservative migration assertions.

## Verification

- Focused frontier, seed, causal-state, checkpoint, recursive-analyzer, and evaluator
  suites: `219` tests passed.
- Full Python attribution suite: `597` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
