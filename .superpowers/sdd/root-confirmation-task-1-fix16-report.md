# Root Confirmation Stability Task 1 Fix Wave 16 Report

Base commit: `250592559`

## Scope

Closed the checkpoint seed-routing coverage gap for restored frontier lifecycle
items. The change is limited to Task 1, uses no dependencies, and remains
offline/passive with respect to trace execution.

## Fix

`RecursiveFrontier.lifecycle_items()` now exposes an immutable tuple of the
verified queued, in-flight, and completed `FrontierItem` values. Checkpoint
restoration uses this complete lifecycle view when it verifies that every
frontier hypothesis has a `hypothesis_seed_keys` entry.

The pre-existing checks still reject unknown extra mappings and mappings whose
seed does not exactly match the restored hypothesis binding. Interrupted
in-flight work remains requeued by the existing frontier restore path and is
therefore included in the same complete lifecycle routing check.

## TDD Coverage

1. RED: a completed-only checkpoint with its sole seed mapping removed was
   accepted because validation used the queued-only snapshot. The focused test
   failed with `AssertionError: ValueError not raised`.
2. GREEN: lifecycle-aware validation rejects that completed-only mutation.
3. Mixed lifecycle coverage creates queued, in-flight/requeued, and completed
   hypotheses. It rejects missing mapping mutations for each lifecycle item,
   a cross-seed mapping, and an unknown extra mapping.
4. The valid mixed checkpoint resume verifies that every restored lifecycle
   item routes back to the original seed builder.

## Verification

- Focused hypothesis, seed-attribution, and recursive-analyzer suites:
  `138` tests passed.
- Full Python attribution suite: `611` tests passed.
- Isolated-pycache `compileall` passed for `trace_attribution`, `scripts`, and
  `tests`.
- `git diff --check` passed.
