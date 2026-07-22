# Root Confirmation Stability Task 1 Fix Wave 15 Report

Base commit: `16055634f`

## Scope

Closed the final fail-open path for per-seed unresolved-evidence containers.
The change remains limited to Task 1 and adds no dependencies or network work.

## Fix

`SeedAttributionResult` now rejects `missing_evidence` and
`blocking_reasons` unless direct model construction provides a `list` or
`tuple`; unsupported containers are never normalized to empty tuples.

`SeedAttributionResult.from_dict` now requires JSON arrays (`list`) for both
fields before model construction. This keeps direct construction, seed
deserialization, report parsing, and evaluator validation aligned while
preserving list output for serialized round trips.

## TDD Coverage

1. RED: the new cross-boundary regression failed with 25 failures because
   scalar strings, mappings, numbers, sets, and a tuple JSON payload bypassed
   terminal `no_defect` validation as empty evidence.
2. GREEN: direct construction, seed `from_dict`, report parsing, and evaluator
   each reject those containers for both fields.
3. Positive coverage verifies empty and filled `list`/`tuple` model sequences,
   JSON-array round trips, and terminal-outcome bypass rejection.

## Verification

- Focused seed, recursive analyzer, benchmark, causal-state, and acceptance
  suites: `197` tests passed.
- Full Python attribution suite: `610` tests passed.
- Isolated-pycache `compileall` passed for `trace_attribution`, `scripts`, and
  `tests`.
- `git diff --check` passed.
