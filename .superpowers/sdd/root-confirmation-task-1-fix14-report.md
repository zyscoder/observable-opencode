# Root Confirmation Stability Task 1 Fix Wave 14 Report

Base commit: `35d9c6cc4`

## Scope

Closed the final concrete-evidence validation gap for per-seed attribution
results. The change remains offline, passive, read-only, and dependency-free.
No Task 2 behavior changed.

## Fix

`SeedAttributionResult` now accepts `missing_evidence` and
`blocking_reasons` entries only when each entry is a string whose `strip()`
content is non-empty. The same shared seed outcome validator enforces that
contract for evaluator payloads, so direct construction, deserialization, and
benchmark evaluation reject empty, whitespace-only, and non-string entries.

`SeedAttributionBuilder.mark_unresolved` now normalizes blank details to the
stable fallback `The required evidence remains unresolved: <reason>.`; blank
or non-string reasons use the stable `unresolved_evidence` reason instead.
An `evidence_gap` still requires at least one concrete unresolved fact.

## TDD Coverage

1. RED: one focused cross-entry-point test failed with 19 expected failures:
   direct construction, `from_dict`, evaluator validation, and Builder
   whitespace handling all accepted invalid detail values.
2. GREEN: the same test passes after the validation and fallback changes.
3. The regression covers empty strings, whitespace-only strings, non-strings,
   stable Builder fallbacks, and a positive concrete-fact round trip.

## Verification

- Focused seed, recursive analyzer, benchmark, causal-state, and acceptance
  suites: `196` tests passed.
- Full Python attribution suite: `609` tests passed.
- Isolated-pycache `compileall` passed for `trace_attribution`, `scripts`, and
  `tests`.
- `git diff --check` passed.
