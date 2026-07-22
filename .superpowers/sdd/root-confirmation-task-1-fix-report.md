# Root Confirmation Stability Task 1 Fix Report

Base commit: `e3d731fd4a930bb38d78a9ea99f56dce143dc243`

## Scope

Implemented the four Task 1 review fixes only. Attribution remains offline,
passive, and read-only; no agent behavior or dependencies changed.

## Fixes

1. Global fusion resume now records completed work by `(hypothesis_id,
   defect_fingerprint)`. A resume skips only that completed seed. An interruption
   during the second global call re-runs that seed and converges with an
   uninterrupted report. Logical global-call accounting is recorded only after a
   successful result, so the resumed and uninterrupted metadata also converge.
2. Modern v3 report deserialization rejects seed results whose `start_ref` is not
   in report `start_refs`, rejects non-confirmed outcomes carrying root refs, and
   requires each `confirmed_root` seed to have root refs and confirmation
   identities bound to top-level confirmed roots for the same seed. The evaluator
   invokes this strict report parse as part of its safety checks.
3. Candidate evidence capsules no longer include `retrieval_score`. The score
   remains internal navigation metadata and cannot change global or independent
   root-confirmation factual inputs.
4. `FrozenMapping` seals its own slots after construction. Its `_entries` storage,
   including nested mappings inside `SeedAttributionResult`, cannot be reassigned.

## TDD Evidence

The new regression tests were first run against the baseline and failed as
expected: global resume did not converge; forged seed bindings passed both model
and evaluator validation; different retrieval scores changed fusion factual
payloads; and `FrozenMapping._entries` was mutable.

## Verification

- Focused Task 1 suite: `182` tests passed.
- Full Python attribution suite: `569` tests passed.
- `compileall`, `git diff --check`, and the final focused re-run passed.

## Remaining Risk

For a genuinely in-flight remote global call, physical usage remains deliberately
conservative because the provider result may be unknown. The per-seed completion
ledger avoids skipping an unrecorded seed; it does not fabricate a result for an
ambiguous transport outcome.
