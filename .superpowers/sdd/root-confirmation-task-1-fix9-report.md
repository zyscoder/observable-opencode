# Root Confirmation Stability Task 1 Fix Wave 9 Report

Base commit: `cb94cdefa`

## Scope

Hardened v1-to-v2 recursive frontier migration for Task 1 only. The attribution
flow remains offline, passive, read-only, and dependency-free; no Task 2 behavior
changed.

## Root Cause

Wave 8 validated legacy item and visit identities before adding a seed binding,
but the migration input supplied only a hypothesis-id-to-binding lookup. A frontier
item could therefore reuse a valid hypothesis id with a different self-consistent
semantic hash, or carry an empty/mismatched binding, without one authoritative
comparison against the enclosing hypothesis.

Legacy visit-key migration also assigned directly into an old-to-new dictionary,
so duplicate old keys could overwrite prior mappings before the v2 lifecycle audit.
Finally, restored state migrated only selected visit-key fields and top-level maps;
nested `active_visit_key` values, embedded semantic keys, replay actions, and hashes
derived from migrated contexts retained v1 identity.

## Fix

1. `RecursiveFrontier.from_checkpoint` now requires the complete enclosing
   hypothesis map. One authoritative item loader validates hypothesis id, semantic
   hash, and a non-empty exact seed binding for both v1 and v2 before accepting an
   item or deriving v2 identity.
2. v1 lifecycle sections are scanned for duplicate legacy `visit_key` values before
   migration. Old-to-new registration rejects duplicate sources, duplicate targets,
   and mapping overwrite.
3. Verified mappings recursively migrate dictionary keys, scalar values, embedded
   semantic keys, nested journals/evidence, `context_before` and `context_after`
   `active_visit_key` values, and replay actions. Migrated context evidence hashes,
   context hashes, and rejudge linkage hashes are normalized to native v2 values.
4. Native v2 checkpoints do not enter the migration/hash-normalization path, so
   modern corruption is not silently repaired.

## TDD Coverage

- Replaced the synthetic v1 fixture identity with a pinned, self-consistent
  enclosing hypothesis plus legacy frontier payload.
- Added v1/v2 adversarial tests for empty or mismatched binding and a reused
  hypothesis id paired with a different self-consistent semantic hash.
- Added a duplicate legacy visit-key test whose two enclosing hypotheses derive
  distinct seed-bound v2 identities.
- Added a full recursive checkpoint convergence test covering map keys, nested
  `active_visit_key`, investigation and confirmation structures, embedded semantic
  keys, replay actions, context-derived hashes, and an audit proving no legacy key
  remains.

## Verification

- Focused frontier, seed, checkpoint, causal-state, and recursive-analyzer suites:
  `193` tests passed.
- Full Python attribution suite: `591` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
