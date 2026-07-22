# Root Confirmation Stability Task 1 Report

## Status

Implemented on base `e1bcdfe43` using test-driven development.

The recursive attribution report now uses `recursive-attribution-report/v3` and
publishes immutable, deterministic per-seed outcomes. Mutable analysis state owns a
composite-key seed ledger and checkpoints it without collapsing duplicate start refs
that have different defect fingerprints.

## Behavior Delivered

- Added immutable `SeedAttributionResult` values with complete `to_dict()` /
  `from_dict()` round trips and recursively frozen nested JSON.
- Added `RecursiveAttributionReport.seed_results`, deterministic seed ordering, and
  exact seed-only top-level aggregation.
- Added conservative v2 report migration. Every v2 `start_ref` becomes an
  `inconclusive` seed with a migration gap; global roots remain global and are never
  copied into local seed root fields.
- Added `RecursiveAnalysisState.seed_ledger`, keyed by serialized
  `(start_ref, defect_fingerprint)`, plus explicit hypothesis-to-seed ownership.
- Recorded global judgments, expansion requests, local judgments, confirmation
  identities, confirmed roots, decisive evidence, and blockers on their owning seed.
- Kept retrieval scores out of seed and confirmation data; only candidate refs are
  retained for navigation provenance.
- Preserved seed state across checkpoint/resume, including duplicate refs with
  distinct fingerprints and transient signal cleanup.
- Kept failures seed-local. A failed branch cannot erase another completed seed, and
  a later absent judgment cannot clear an existing evidence gap on the same seed.
- Updated the strict offline evaluator for v3 seed result validation.
- Added the required `multi_seed_claims.json` fixture. No dependency was added.

## TDD Evidence

### RED

1. Initial three-seed integration test:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_seed_attribution.py -v`

   Result: failed with `AttributeError: 'RecursiveAttributionReport' object has no
   attribute 'seed_results'`.

2. Expanded model/checkpoint suite before production implementation:

   Same command as above.

   Result: failed to import `SeedAttributionResult`, confirming the model contract did
   not exist.

3. Conservative same-seed failure regression:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_seed_attribution.SeedAttributionModelTests.test_no_defect_does_not_clear_an_existing_seed_failure -v`

   Result: failed because the outcome was `no_defect` instead of `evidence_gap`.

4. Resume convergence regression found during final verification:

   The focused checkpoint suite failed
   `test_resumed_signal_checkpoint_converges_to_uninterrupted_report` because a
   transient `analysis_interrupted` blocker remained in the seed ledger. The existing
   regression test was kept and the ledger cleanup was fixed.

### GREEN

- New seed suite: 9 tests passed.
- Recursive analyzer suite: 72 tests passed.
- Acceptance and benchmark evaluator suites: 47 tests passed.
- Focused brief suite: 78 tests passed.
- Full Python `trace_attribution` suite: 564 tests passed.

## Final Commands

1. Focused model and checkpoint tests:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_causal_state.py tools/trace_attribution/tests/test_causal_checkpoint.py -v`

   Result: `Ran 78 tests in 0.293s` / `OK`.

2. Full Python suite:

   `PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests`

   Result: `Ran 564 tests in 2.415s` / `OK`.

3. Compile check:

   `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-pycache-final2 python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`

   Result: exit 0, no output. `PYTHONPYCACHEPREFIX` keeps bytecode writes inside the
   sandbox; the initial default-cache attempt was blocked by macOS cache permissions.

4. Diff check:

   `git diff --check`

   Result: exit 0, no output.

## Files

- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/__init__.py`
- `tools/trace_attribution/scripts/evaluate_recursive_attribution.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `tools/trace_attribution/tests/test_causal_state.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/fixtures/recursive_cases/multi_seed_claims.json`
- `.superpowers/sdd/root-confirmation-task-1-report.md`

## Concerns

No blocking concerns.

Report v2 inputs have the required read-only migration. Recursive action checkpoint
state was bumped from v2 to v3 because old checkpoints do not contain a seed ledger;
those pre-ledger checkpoints remain deliberately incompatible rather than fabricating
per-seed state.
