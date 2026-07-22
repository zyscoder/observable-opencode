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

## Review Fix Wave

### RED

- Strict import and revision trust:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py -v
  Ran 15 tests - FAILED (30 failures)
  ```

  The failures proved that legacy `manifest.environment.revision` was trusted,
  empty and malformed values were accepted, ambiguous aliases selected the
  last owner, and incompatible generated edge-ID collisions were ignored.

- Explicit and programmatic decisive eligibility:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_cli.py -v
  ImportError: cannot import name 'analysis_start_refs'

  PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_external_evaluation_seed_preserves_expected_actual_scope_and_status tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_ineligible_external_evaluation_start_never_reaches_recursive_judge -v
  Ran 2 tests - FAILED (5 failures)

  PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint.BackwardTaintAnalyzerTest.test_legacy_analyzer_never_judges_or_roots_external_evaluation_fact -v
  Ran 1 test - FAILED (1 failure)
  ```

  These failures showed that explicit CLI starts had no eligibility gate,
  recursive seeds used generic objective text, and legacy/recursive analyzers
  could judge external evaluation facts directly.

- Formal revision producer:

  ```text
  cd packages/opencode && bun test test/observability/case-trace.test.ts -t 'subject revision' --timeout 30000
  0 pass, 2 fail
  ```

  Both config and lazy environment producer tests observed an absent formal
  `manifest.subject_revision`.

### Implementation

- Added immutable top-level `subject_revision` and
  `subject_revision_provenance` manifest fields. `CaseTrace` captures them
  once at case start from `CaseTraceConfig.subjectRevision` or
  `OPENCODE_TRACE_SUBJECT_REVISION`, records the exact supply method and
  run/case binding, and performs no repository inspection or new startup I/O.
- Changed offline revision matching to trust only the formal field with a valid
  source pair, `bound_at=case_start`, and matching manifest run/case IDs.
  Legacy environment revision data and malformed bindings fail closed.
- Added exact nine-field validation, trimmed non-empty checks, timezone-bearing
  ISO-8601 timestamp parsing, non-empty unique evidence refs, and recursive
  finite JSON-safe provenance validation with required string `method` and
  `version`.
- Added one decisive-judgment predicate for defaults, explicit CLI starts, and
  programmatic legacy/recursive starts. External facts that are mismatched,
  missing, passed, unknown, or otherwise unprovenanced never enter a judge
  request or root promotion path.
- Reused the canonical root eligibility contract for external evaluation
  facts in legacy and recursive analyzers. The global capsule retains only its
  additional evidence-only exclusions, avoiding a duplicate external-fact
  rule.
- Changed external evidence alias indexing to retain every owner and create an
  edge only for a uniquely resolved alias. Ambiguous refs remain in
  `unresolved_evidence_refs`.
- Changed generated edge-ID handling to compare the complete semantic edge:
  identical edges are idempotent and incompatible collisions raise.
- Mapped recursive external seeds from assertion to expected behavior and
  observation to actual behavior, with the evaluation scope and status carried
  in the defect state.

### GREEN

- Required focused Python import and CLI suites:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_recursive_cli.py -v
  Ran 26 tests - OK
  ```

- Focused legacy, recursive, and root-contract suites:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_backward_taint.BackwardTaintAnalyzerTest -v
  Ran 43 tests - OK

  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_recursive_analyzer.py -v
  Ran 68 tests - OK

  PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_evidence_capsule.py -v
  Ran 22 tests - OK
  ```

- Focused Bun revision/evaluation contract:

  ```text
  cd packages/opencode && bun test test/observability/causal-ir.test.ts test/observability/case-trace.test.ts -t 'semantic contract|external evaluation|subject revision' --timeout 30000
  4 pass, 193 filtered, 0 fail
  ```

- Full suites and static checks:

  ```text
  PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
  Ran 524 tests - OK

  cd packages/opencode && bun test test/observability/case-trace.test.ts test/observability/causal-ir.test.ts --timeout 30000
  197 pass, 0 fail

  cd packages/opencode && bun typecheck
  exit 0

  PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task4-review-pycache python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/tests
  exit 0

  git diff --check
  exit 0
  ```

### Files

- `packages/opencode/src/observability/case-trace.ts`
- `packages/opencode/test/observability/case-trace.test.ts`
- `tools/trace_attribution/trace_attribution/analyzer.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/cli.py`
- `tools/trace_attribution/trace_attribution/evaluation_facts.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`
- `tools/trace_attribution/tests/test_backward_taint.py`
- `tools/trace_attribution/tests/test_causal_retrieval.py`
- `tools/trace_attribution/tests/test_evaluation_facts.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_recursive_cli.py`
- `.superpowers/sdd/trace-fact-closure-task-4-report.md`

### Concerns

- None. Revision matching intentionally fails closed for pre-fix traces that
  contain only `manifest.environment.revision`; they must be regenerated with
  formal case-start provenance to become decisive.
- Evaluation facts and derived eligibility remain offline-only and are not
  added to Agent prompts, context, or execution. No dependencies were added.
