# Trace Fact Closure Task 4 Report

## Status

Complete. The offline attribution CLI now imports strict, revision-bound
external evaluation facts without exposing evaluation payloads to Agent
execution. Matched failures can seed attribution but remain ineligible as
defect-introduction roots.

## TDD Evidence

### RED

1. Initial Python focused command:

   ```text
   PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py -v
   ```

   Failed at import with
   `ModuleNotFoundError: No module named 'trace_attribution.evaluation_facts'`.

2. Initial Bun contract command:

   ```text
   cd packages/opencode && bun test test/observability/causal-ir.test.ts -t "external evaluation" --timeout 30000
   ```

   Ran one test and failed because `external.evaluation_fact` was absent from
   `FORMAL_RECORD_TYPES`.

3. The first Python GREEN attempt ran nine tests and exposed four expected
   fallback failures: mismatched, missing-revision, passed, and unknown facts
   were excluded by the explicit evaluation gate but selected by the generic
   final fallback. The fallback now excludes all external evaluation facts.

4. Typecheck then exposed the shared contract closure:
   `external_evaluation_observed` was missing from the compatibility
   `DataflowEdge` union, and `evaluation` was missing from `TraceComponent` and
   its exhaustive HTML label registry. These schema surfaces were added before
   the final GREEN run.

### GREEN

- Required focused Python command:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_recursive_cli.py -v
  Ran 19 tests - OK
  ```

- Required focused Bun command:

  ```text
  cd packages/opencode && bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts -t "semantic contract|external evaluation" --timeout 30000
  2 pass, 193 filtered, 0 fail
  ```

- Full Python trace-attribution suite:

  ```text
  PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task4-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -v
  Ran 513 tests - OK
  ```

- Full changed Bun observability suites:

  ```text
  cd packages/opencode && bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts --timeout 30000
  195 pass, 0 fail
  ```

- TypeScript typecheck: `cd packages/opencode && bun typecheck` passed.
- Python compileall with an isolated pycache passed.
- `git diff --check` passed.

## Implementation

- Added `inject_external_evaluation_facts(trace, payloads)`, which deep-copies
  the trace, validates exactly nine fields and the exact
  `passed|failed|unknown` enum, and fails explicitly on malformed input.
- Bound `subject_revision` to `manifest.environment.revision`. Missing trace
  revision produces `revision_status=missing`; unequal values produce
  `mismatched`. Only matched failures are decisive and become default starts.
- Derived record IDs with the exact `stable_json(payload)` SHA-256 first-16
  rule. Repeated identical payloads and their evidence edges are idempotent.
- Preserved unresolved evidence refs in fact data. Resolvable refs receive
  `external_evaluation_observed` edges with `external_grader` evidence; edge
  attribution eligibility depends only on revision match.
- Added repeatable `--evaluation` loading after optional review enrichment and
  before `TraceGraph.from_trace()`.
- Registered the formal record type and relation, added
  `external_evaluation` aliases for record/evaluation IDs, and verified journal
  replay preserves the alias and relation.
- Added external evaluation facts to both local and global Python root
  exclusion. They can act as outcome evidence, counterevidence, or seeds, but
  never as defect-introduction roots.
- Extended the TypeScript trace component and compatibility edge schemas so
  the required offline record and relation remain type-safe end to end.

## Files

- `tools/trace_attribution/trace_attribution/evaluation_facts.py`
- `tools/trace_attribution/trace_attribution/cli.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/tests/test_evaluation_facts.py`
- `packages/opencode/src/observability/trace-semantic-contract.ts`
- `packages/opencode/src/observability/causal-ir.ts`
- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/src/observability/case-trace-html.ts`
- `packages/opencode/test/observability/causal-ir.test.ts`
- `.superpowers/sdd/trace-fact-closure-task-4-report.md`

## Concerns

- Revision matching intentionally has one deterministic source:
  `manifest.environment.revision`. Traces without that string retain imported
  facts as non-decisive observations with `revision_status=missing`.
- The payload schema is closed: extra fields are rejected along with missing or
  wrongly typed fields. This prevents silent partial injection and keeps the
  stable-ID input unambiguous.
- All ingestion and enrichment are confined to the offline Python attribution
  CLI. No dependencies or Agent-visible runtime inputs were added.

## Commit

Planned commit: `feat(attribution): ingest revision-bound benchmark facts`.
This report is included in the Task 4 commit; the final response records the
resulting SHA.
