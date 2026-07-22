# Root Confirmation Stability Task 1 Fix Wave 11 Report

Base commit: `8536fb299`

## Scope

Closed the two remaining Task 1 ownership-mapping findings. The attribution flow
remains offline, passive, read-only, and dependency-free. No Task 2 behavior or
dependency surface changed.

## Fixes

1. Published primary and co-root `observed_defect_refs` now project to exactly
   the owning confirmed-root seed among report `start_refs`. Provenance refs that
   are not report seeds remain valid and are preserved. Direct construction,
   `from_dict`, and evaluator safety validation reject a second report seed,
   including a synthetic `no_defect` seed.
2. Checkpoint restore now requires every `hypothesis_seed_keys` entry to name a
   restored hypothesis, point to an existing seed-ledger entry, and equal that
   hypothesis's `seed_binding_identity`. Every restored frontier hypothesis must
   have an explicit mapping. Extra ids, missing frontier mappings, and cross-seed
   remaps fail closed.
3. The v1 visit-key migration regression fixture is canonicalized in-test with a
   verified seed identity and seed ledger so it continues to exercise migration
   behavior without relying on an invalid modern action snapshot.

## TDD Coverage

- Added direct and `from_dict` report regressions for another report seed in a
  published root's observed refs while retaining distinguishable provenance.
- Added evaluator coverage using an added `no_defect` report seed.
- Added real bundle/`from_checkpoint` mutations for extra, missing, and
  cross-seed hypothesis mappings, plus restored frontier routing assertions.
- Added resume-path assertions that queued confirmation or frontier work remains
  bound to the owning seed before the resumed report is compared to uninterrupted
  analysis.

## Verification

- Focused seed, benchmark metric, causal-state, and checkpoint suites: `105`
  tests passed.
- Full Python attribution suite: `600` tests passed.
- Isolated-pycache `compileall` passed.
- `git diff --check` passed.
