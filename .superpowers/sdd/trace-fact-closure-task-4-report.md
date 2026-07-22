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

## Second Review Fix Wave

### RED

- Five focused smuggling-path regressions:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_evaluation_facts.ExternalEvaluationFactsTest.test_matched_fact_cannot_make_mismatched_external_source_edge_eligible tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_semantic_search_and_fallback_exclude_audit_only_external_facts tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_malformed_eligible_edge_from_audit_only_external_source_is_filtered tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_mismatched_external_source_cited_by_matched_failure_never_reaches_frontier_or_judge tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_matched_passed_external_fact_is_counterevidence_but_never_seed_frontier_or_root -v
  Ran 5 tests - FAILED (failures=5)
  ```

  The failures proved that a matched target upgraded a mismatched external
  source edge, semantic search returned mismatched and unknown facts, a
  malformed eligible edge entered graph adjacency, and mismatched and passed
  external facts entered recursive frontier work.

- Judge-directed investigation bypasses:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_audit_only_external_fact_cannot_enter_judge_investigation_input tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_semantic_investigation_excludes_audit_only_external_fact -v
  Ran 2 tests - FAILED (failures=2)
  ```

  Direct node inspection returned the audit-only fact, and the independent
  semantic investigation path ranked it as a Judge input.

### Implementation

- Added canonical `eligible_as_attribution_evidence` and
  `eligible_as_analysis_seed` predicates. Matched `passed` and `failed` facts
  with valid revision and grader provenance can participate as evidence; only
  matched failed facts with explicit decisive eligibility can seed analysis.
- Made generated and loaded graph edges validate both endpoints. Raw facts and
  raw ineligible edges remain in the trace for audit, while audit-only external
  endpoints are excluded from adjacency and semantic predecessor traversal.
- Filtered graph semantic search, layered retrieval, global candidate pools,
  checkpoint candidate restoration, legacy traversal, recursive seed/frontier
  creation, global expansion, and final Judge request construction.
- Kept matched passed facts available as counterevidence candidates while
  preventing them from becoming initial seeds, recursive frontier nodes, or
  roots.
- Applied the same evidence predicate to direct and semantic investigation
  tools so an audit-only fact cannot be reintroduced into a Judge request by
  reference.

### GREEN

- Focused smuggling-path regressions:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_evaluation_facts.ExternalEvaluationFactsTest.test_matched_fact_cannot_make_mismatched_external_source_edge_eligible tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_semantic_search_and_fallback_exclude_audit_only_external_facts tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_malformed_eligible_edge_from_audit_only_external_source_is_filtered tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_mismatched_external_source_cited_by_matched_failure_never_reaches_frontier_or_judge tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_matched_passed_external_fact_is_counterevidence_but_never_seed_frontier_or_root -v
  Ran 5 tests - OK
  ```

- Focused investigation regressions:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_audit_only_external_fact_cannot_enter_judge_investigation_input tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_semantic_investigation_excludes_audit_only_external_fact -v
  Ran 2 tests - OK
  ```

- Focused evaluation, graph/retrieval, recursive CLI/analyzer, and legacy
  suites:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_cli.py tools/trace_attribution/tests/test_backward_taint.py
  Ran 241 tests - OK
  ```

- Full Python trace-attribution suite:

  ```text
  PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task4-wave2-final-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
  Ran 531 tests - OK
  ```

- No TypeScript files changed, so changed Bun suites and TypeScript typecheck
  were not applicable.
- Python compileall and `git diff --check` passed after the final report update.

### Concerns

- None. The change is confined to offline Python attribution and preserves all
  audit-only nodes and unresolved evidence references without exposing them to
  Agent execution or adding dependencies or LLM calls.

## Third Review Fix Wave

### RED

- Graph revalidation, temporal context, investigation paths, recursive
  frontier, and preserved imported/counterevidence behavior:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_evaluation_facts.ExternalEvaluationFactsTest.test_injects_revision_matched_external_failure_as_start_seed tools.trace_attribution.tests.test_evaluation_facts.ExternalEvaluationFactsTest.test_graph_rejects_forged_preexisting_external_fact_without_formal_proof tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_temporal_judgment_context_excludes_audit_only_external_endpoint tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_compare_paths_rejects_every_path_containing_audit_only_external_fact tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_forged_external_fact_never_enters_recursive_frontier tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_matched_passed_external_fact_is_counterevidence_but_never_seed_frontier_or_root -v
  Ran 6 tests - FAILED (failures=3, errors=3)
  ```

  The errors proved that `TraceGraph` did not own evidence/start eligibility.
  The failures proved that a forged pre-existing fact entered recursive state,
  raw temporal adjacency exposed it to Judge context, and a one-node causal
  path accepted it without checking any edge.

### Implementation

- Added one strict `reconstruct_external_evaluation_record()` path shared by
  ingestion and graph construction. It revalidates the exact nine-field
  payload, formal revision provenance and case/run binding, stable record ID,
  record envelope, resolved/unresolved evidence derivation, and every derived
  field. Any mismatch leaves the raw record present but audit-only.
- Moved contextual truth to `TraceGraph.evidence_eligible()`,
  `TraceGraph.analysis_start_eligible()`, and
  `TraceGraph.edge_endpoints_eligible()`. Migrated legacy, CLI, retrieval,
  recursive, and investigation consumers away from standalone node trust.
- Added `TraceGraph.temporal_adjacency_edges()` so judgment context no longer
  scans raw edges without canonical endpoint validation. Normal non-external
  temporal edges remain advisory for true, false, and omitted eligibility.
- Rejected malformed non-boolean or contradictory pre-existing edge
  eligibility declarations before adjacency construction.
- Made all investigation results reject resolved audit-only external refs and
  made `compare_causal_paths` validate every node before validating hops, so
  both one-node and multi-node paths fail closed.
- Preserved matched failed facts as evidence/seeds, matched passed facts as
  counterevidence only, and all external facts as root-ineligible.

### GREEN

- Focused evaluation, graph/retrieval, judgment-context, investigation,
  recursive analyzer, and recursive CLI suites:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_investigation.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_cli.py
  Ran 173 tests in 0.407s
  OK
  ```

- Full Python trace-attribution suite:

  ```text
  PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task4-wave3-pycache PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
  Ran 538 tests in 2.348s
  OK
  ```

- Python compileall:

  ```text
  PYTHONPYCACHEPREFIX=/tmp/observable-opencode-task4-wave3-compile python3 -m compileall -q tools/trace_attribution/trace_attribution tools/trace_attribution/tests
  exit 0
  ```

- `git diff --check` passed. No TypeScript files changed, so Bun tests and
  TypeScript typecheck were not applicable.

### Concerns

- None. Legacy or handcrafted external records that cannot be reconstructed
  exactly from the strict payload and formal manifest provenance are
  intentionally retained as audit-only. No dependencies, Agent behavior, LLM
  calls, or TypeScript surfaces changed.

## Fourth Review Fix Wave

### RED

- Edge-carried evidence refs in temporal context and global evidence capsules:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_temporal_context_filters_ineligible_resolved_evidence_refs_only tools.trace_attribution.tests.test_evidence_capsule.CandidateEvidenceCapsuleTest.test_capsule_filters_audit_only_external_refs_but_preserves_other_ref_classes -v
  Ran 2 tests - FAILED (failures=2)
  ```

  Both failures showed that an eligible edge could carry an audit-only
  external node ref into Judge-visible structures. The matched passed external
  ref, normal record ref, artifact ref, and unresolved raw ref were present as
  expected.

- Checkpoint policy identity and restored state/report validation:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_checkpoint_config_fingerprints_graph_evidence_eligibility_policy tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_restore_rejects_checkpoint_from_before_evidence_eligibility_policy tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_restore_rejects_older_evidence_eligibility_policy_identity tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_recursive_restore_rejects_audit_only_refs_across_all_state_surfaces tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_completed_and_pending_reports_reject_restored_audit_only_refs -v
  Ran 5 tests - FAILED (failures=6, errors=3)
  ```

  The failures proved that pre-policy state resumed and that hypothesis,
  visit, investigation, judgment, and pending report refs were trusted. Two
  errors proved the policy field was absent; the completed-report fixture was
  then corrected to model the durable action directly.

- Completed and pending report validation was also proven independently by
  temporarily reverting only the new return-path guards:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_completed_and_pending_reports_reject_restored_audit_only_refs -v
  Ran 1 test - FAILED (failures=2)
  ```

- The final request-assembly review found visit and investigation evidence was
  added after the context builder's sanitizer:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_recursive_analyzer.RecursiveTraversalTest.test_step_request_resanitizes_visit_and_investigation_evidence -v
  Ran 1 test - FAILED (failures=1)
  ```

### Implementation

- Added `TraceGraph.filter_evidence_refs()` as the central policy boundary.
  Any ref that resolves to a trace node must pass `evidence_eligible()`;
  unresolved artifact/raw refs remain unchanged. Edge context, incoming edge
  context, and temporal adjacency all sanitize refs before returning them.
- Defensively reapplied the policy in evidence capsule and judgment context
  construction, including retrieval edges, candidate refs, hypothesis
  evidence, downstream judgment evidence, source refs, and temporal evidence.
  Audit-only external payloads are therefore never hydrated through those
  refs. Matched passed external evidence remains available as counterevidence.
- Added the deterministic
  `graph-external-evidence-eligibility/v1` policy identity to the strict
  checkpoint config schema and config fingerprint, and bumped the checkpoint
  schema to `recursive-attribution-checkpoint/v3`. Pre-policy and older-policy
  manifests fail compatibility validation before resume.
- Added recursive restored-value validation for frontier/visit, hypothesis,
  action/investigation/judgment state and for pending/completed reports before
  materialization or return. This closes every current checkpoint surface
  containing node refs rather than filtering only causal candidates.
- Reapplied the judgment-context sanitizer after live visit and investigation
  evidence is attached to each step request, before hashing and Judge delivery.

### GREEN

- New boundary, policy, restore, and current-resume regressions:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools.trace_attribution.tests.test_causal_retrieval.CausalRetrievalTest.test_temporal_context_filters_ineligible_resolved_evidence_refs_only tools.trace_attribution.tests.test_evidence_capsule.CandidateEvidenceCapsuleTest.test_capsule_filters_audit_only_external_refs_but_preserves_other_ref_classes tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_checkpoint_config_fingerprints_graph_evidence_eligibility_policy tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_restore_rejects_checkpoint_from_before_evidence_eligibility_policy tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_restore_rejects_older_evidence_eligibility_policy_identity tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_recursive_restore_rejects_audit_only_refs_across_all_state_surfaces tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_completed_and_pending_reports_reject_restored_audit_only_refs tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_completed_recursive_run_restores_exact_report_without_repeating_calls -v
  Ran 8 tests - OK
  ```

- Focused graph/evidence capsule/judgment/checkpoint/recursive suites:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest tools/trace_attribution/tests/test_evaluation_facts.py tools/trace_attribution/tests/test_causal_retrieval.py tools/trace_attribution/tests/test_evidence_capsule.py tools/trace_attribution/tests/test_causal_judge.py tools/trace_attribution/tests/test_global_judge.py tools/trace_attribution/tests/test_causal_checkpoint.py tools/trace_attribution/tests/test_recursive_analyzer.py tools/trace_attribution/tests/test_recursive_acceptance_review.py -v
  Ran 277 tests - OK
  ```

- Full Python trace-attribution suite:

  ```text
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tools/trace_attribution python3 -m unittest discover -s tools/trace_attribution/tests -p 'test_*.py'
  Ran 546 tests - OK
  ```

- Python compileall and `git diff --check` passed after the final report update.
- No TypeScript files changed, so Bun tests and TypeScript typecheck were not
  applicable.

### Concerns

- None. The policy change is passive and confined to offline Python
  attribution. Strict external status/revision semantics, exact checkpoint
  schemas, unresolved evidence preservation, and dependency count are
  unchanged.
