# Root Confirmation Stability Task 1 Fix Wave 8 Report

Base commit: `aec93ee7a`

## Scope

Closed the remaining Task 1 Global Judge expansion and frontier checkpoint
compatibility gaps. The attribution flow remains offline, passive, and read-only;
the defect fingerprint is unchanged and no Task 2 behavior or dependency changed.

## Root Cause

The `needs_expansion` branch created a new `FrontierItem` from a seed-bound
hypothesis without copying `seed_binding_identity`. For two hypotheses sharing a
defect fingerprint and expansion anchor, their empty-seed visit keys collided and
the second item's recursive work was discarded.

Wave 7 also changed frontier item and visit identity inputs without versioning the
checkpoint payload. Pre-fix v1 items therefore failed the new identity validation
on resume.

## Fix

1. Global expansion entries now carry the complete hypothesis seed binding, so both
   same-fingerprint seeds retain their own frontier step and confirmation lifecycle.
2. Frontier checkpoints now write v2, and recursive frontier state now writes v2.
3. v1 items without a persisted seed binding are migrated only when the enclosing
   hypothesis supplies one. Migration first verifies the v1 `item_id` and
   `visit_key`, then derives the v2 seed-bound identity. Forged or ambiguous v1
   identities fail closed; v1 records that already persist a binding must satisfy
   the current identity validation.
4. Restored visit-indexed evidence and investigation structures follow verified
   legacy-to-v2 visit-key migrations.

## Tests

- Added a pinned pre-fix v1 frontier fixture and tests for successful verified
  migration plus forged-identity rejection.
- Added a real Global Judge `needs_expansion` regression with two start refs, one
  fingerprint, one anchor, two recursive steps, two confirmations, and an
  interruption/resume check that performs only the outstanding confirmation.

## Verification

- Focused frontier, seed, checkpoint, causal-state, and recursive-analyzer suites:
  `189` tests passed.
- Full Python attribution suite: `587` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
