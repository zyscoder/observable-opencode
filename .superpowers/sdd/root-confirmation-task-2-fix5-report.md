# Root Confirmation Stability Task 2 Fix Wave 5 Report

Base commit: `9a2956280`

## Scope

Closed all three Important findings from the fifth whole-Task-2 review. The
changes remain offline, passive, read-only, and dependency-free. No Task 3
evidence expansion behavior was added.

## Root Causes

1. Raw `dataflow_edges` always normalized `edge_origin` to
   `trace.dataflow_edges`. This erased recorded temporal provenance before the
   shared confirmation-path policy could classify the edge.
2. Per-seed `global_judgment` payloads had no persisted schema identity and
   report deserialization accepted arbitrary non-empty mappings. Checkpoint
   compatibility also omitted the global judgment contract.
3. Capsule edges retained unresolved evidence refs, and `grounded_refs`
   copied those refs directly. The report restoration audit rejected known
   ineligible nodes but did not reject refs absent from the active graph.

## RED

Tests were added before production changes and run in isolated groups.

- Raw temporal-origin tests failed because normalized edges contained
  `record.source_refs` or `trace.dataflow_edges` instead of
  `offline.temporal_reconstruction`; downstream search and queued confirmation
  consequently accepted the path.
- Stale-resume tests failed because no exception was raised for a pre-v2
  per-seed judgment, `global_judgment_contract` was absent from checkpoint
  configuration, and persisted judgments had no `schema_version`.
- Raw `record:ghost` tests failed because the ref was absent from
  `missing_evidence_refs` and remained present in global grounded evidence.
- A structurally valid v2 completed report containing `record:ghost` was
  returned unchanged instead of failing closed against the active graph.

## Design Decisions

- Preserve the raw edge's top-level or metadata `edge_origin`; record
  `trace.dataflow_edges` separately as `source_container`. This keeps temporal
  classification intact without losing ingestion provenance.
- Persist per-seed judgments with
  `global-candidate-judgment/v2`. Report and checkpoint deserialization reject
  non-empty judgments without the v2 schema, exact active-focus binding, and
  complete assessment fields. This is an explicit migration/rejudgment policy;
  no root is inferred from stale state.
- Add the global judgment contract to the checkpoint compatibility envelope so
  older completed and partial checkpoints cannot match the current behavior
  identity. Existing v1 report migration remains conservative and unchanged.
- Collect edge evidence refs into capsule reference envelopes. Unresolved refs
  remain in raw edge diagnostics and appear in `missing_evidence_refs`, while
  only resolved reference envelopes contribute to `grounded_refs`.
- Audit seed decisive refs, global assessment/path refs, confirmations, and
  published roots against the active graph or artifact manifest before a new
  or restored report can be published.

## Verification

1. Focused Task 2 and compatibility suites:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_causal_state.py tools/trace_attribution/tests/test_seed_attribution.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_confirmation_path.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_recursive_analyzer.py`

   Result: `Ran 331 tests in 1.334s` / `OK`.

2. Full Python attribution suite:

   `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'`

   Result: `Ran 653 tests in 3.250s` / `OK`.

3. Compile check:

   `PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task2-fix5-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/scripts tools/trace_attribution/tests`

   Result: exit status `0`.

4. Diff check:

   `git diff --check 9a2956280..HEAD`

   Result before commit: exit status `0`; rerun after commit is recorded in the
   final response.

## Files

- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/causal_state.py`
- `tools/trace_attribution/trace_attribution/checkpoint.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/global_judge.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_seed_attribution.py`
- `.superpowers/sdd/root-confirmation-task-2-fix5-report.md`

## Remaining Risks

- The migration policy intentionally rejects stale non-empty global judgments
  instead of rewriting them. Callers with old report/checkpoint state must
  re-run attribution under the v2 contract.
- Artifact refs are considered resolved when their identity exists in the
  active artifact manifest; availability and hydration remain separate
  evidence-quality checks already enforced by the judge fact tree.
