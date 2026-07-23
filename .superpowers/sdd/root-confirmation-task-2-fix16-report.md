# Root Confirmation Task 2 Fix Wave 16 Report

## Scope

- Branch: `codex/message-context-lineage`
- Baseline: `e3629ac203c55750139fe5c683ffa1e747165b96`
- Task: Root Confirmation Task 2 only
- Constraints preserved: offline, passive, read-only attribution; no Agent
  feedback; no new dependencies; no Task 3 behavior; no unrelated or existing
  untracked files changed.

## RED

The first focused RED run covered stale intermediate evidence, pre-limit
filtering/backfill, semantic investigation, direct global validation, and
end-to-end publication:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_backward_taint.TraceGraphTest.test_graph_ranking_helpers_filter_stale_revisions_before_limit \
  tools.trace_attribution.tests.test_backward_taint.TraceGraphTest.test_judgment_context_and_analyzer_receive_active_upstream_backfill \
  tools.trace_attribution.tests.test_investigation.InvestigationToolTest.test_semantic_investigation_filters_stale_revisions_before_limit \
  tools.trace_attribution.tests.test_global_judge.GlobalCandidateJudgeContractTest.test_direct_global_validation_rejects_stale_intermediate_path \
  tools.trace_attribution.tests.test_recursive_analyzer.RetrievalGlobalFusionTest.test_stale_intermediate_invalidates_capsule_confirmation_and_publication -v

Ran 5 tests
FAILED (failures=5)
```

The failures demonstrated:

- `causal_decision_refs()` allowed a newer stale decision to consume `limit=1`
  instead of backfilling the active decision.
- `upstream_nodes()` and Judge-facing context retained stale support.
- `_search_semantic_nodes()` ranked and truncated a higher-scoring stale node
  before eligibility filtering.
- direct global request validation accepted a capsule whose candidate and
  defect were active but whose intermediate path fact was stale.
- stale `record:change` reached capsule paths, grounded refs, global causal
  assessments, confirmation paths, and root/factor publication surfaces.

The checkpoint RED run added both root and factor restore coverage:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest \
  tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_partial_and_completed_restore_reject_stale_intermediate_root_path \
  tools.trace_attribution.tests.test_causal_checkpoint.CausalCheckpointTest.test_partial_and_completed_restore_reject_stale_intermediate_factor_path -v

Ran 2 tests
FAILED (failures=2)
```

Both partial state restoration and completed checkpoint restoration accepted
paths containing a stale intermediate node before the fix.

## Implementation

`TraceGraph.active_revision_evidence_eligible(ref)` is now the graph-level
eligibility policy. It requires:

1. canonical resolution to a graph node;
2. generic graph evidence eligibility;
3. a matched explicit `revision_status`, when present;
4. a `subject_revision` matching the active manifest revision, when both are
   authoritative strings;
5. a nonnegative `repository_revision` matching the authoritative active
   repository generation, when one is available.

`active_revision_candidate_eligible()` remains as a compatibility alias. The
authored-root predicate remains an additional candidate gate and is not used as
a substitute for evidence eligibility.

The policy is applied before sorting and limit truncation in
`causal_decision_refs()`, `upstream_nodes()`, graph semantic search, and
investigation semantic search. This permits active backfill and returns an
empty result when no eligible item remains.

Judge-facing paths, edge endpoints, incoming context, evidence capsules,
action groups, global request validation, confirmation requests and results,
root/factor publication, and checkpoint restore now reject stale intermediate
evidence. Unresolved references remain available only to diagnostic handling;
grounded and publication reference validators fail closed.

No persistence identity or schema version was changed. Serialized shapes are
unchanged, and restored payloads are revalidated against the live graph policy.

## Files

Implementation:

- `tools/trace_attribution/trace_attribution/graph.py`
- `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- `tools/trace_attribution/trace_attribution/investigation.py`
- `tools/trace_attribution/trace_attribution/evidence_capsule.py`
- `tools/trace_attribution/trace_attribution/recursive_analyzer.py`

Tests:

- `tools/trace_attribution/tests/test_backward_taint.py`
- `tools/trace_attribution/tests/test_investigation.py`
- `tools/trace_attribution/tests/test_global_judge.py`
- `tools/trace_attribution/tests/test_recursive_analyzer.py`
- `tools/trace_attribution/tests/test_causal_checkpoint.py`

Report:

- `.superpowers/sdd/root-confirmation-task-2-fix16-report.md`

## GREEN

Focused regression tests:

```text
Ran 7 tests in 0.083s
OK
```

The active-path counterparts also pass: active intermediates reach capsule,
decisive evidence, confirmation, and root publication, while stale-only
ranking/search inputs return empty results.

Related attribution modules:

```text
Ran 440 tests in 2.350s
OK
```

Complete Python attribution suite:

```text
PYTHONPATH=tools/trace_attribution python3 -m unittest discover \
  -s tools/trace_attribution/tests -p 'test_*.py'

Ran 731 tests in 4.786s
OK
```

Compilation:

```text
PYTHONPYCACHEPREFIX=/private/tmp/root-confirmation-task2-fix16-pycache \
  python3 -m compileall -q \
  tools/trace_attribution/trace_attribution \
  tools/trace_attribution/tests \
  tools/trace_attribution/scripts

exit 0
```

Working-tree whitespace validation:

```text
git diff --check e3629ac20

exit 0
```

The production audit:

```text
rg -n '(^|[^A-Za-z_])(graph|self\.graph)\.evidence_eligible\(' \
  tools/trace_attribution/trace_attribution --glob '*.py'

no matches
```

## Self-review

- Filtering precedes score/rank ordering and `limit` slicing at all three
  requested ranking entry points.
- Candidate root eligibility remains a separate additional gate.
- Edge and path validation is fail closed for stale endpoints, including
  restored root and factor paths.
- Capsule and global request validation cover every downstream path reference,
  not only the candidate node.
- Confirmation evidence is resolved and active-revision eligible before
  publication.
- The diff does not alter serialized payload shapes, transport behavior,
  attribution budgets, or Agent-visible behavior.
- Existing unrelated untracked files remain untouched and excluded.

## Risks And Concerns

No blocking concerns.

The intentional behavioral change is conservative: evidence carrying an
explicit mismatched revision can no longer appear in Judge-facing context or
causal publication even if an underlying graph edge remains present for
diagnostics. Traces without authoritative revision data retain the prior
generic eligibility behavior.

## Commit

Planned commit: `fix(attribution): enforce active-revision evidence`.
This report is included in that commit; the final response records the
resulting SHA.
