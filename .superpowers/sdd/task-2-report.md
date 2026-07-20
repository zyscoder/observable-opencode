# Task 2 Report: Layered Semantic Predecessor Retrieval

## Status

Implemented and verified on branch `codex/message-context-lineage`.

Final commit subject: `feat(attribution): retrieve semantic causal predecessors`.
The authoritative final commit hash is reported with this task's completion status.

## Files

- Created `tools/trace_attribution/trace_attribution/causal_retrieval.py`
- Created `tools/trace_attribution/tests/test_causal_retrieval.py`
- Modified `tools/trace_attribution/trace_attribution/graph.py`
- Modified `tools/trace_attribution/trace_attribution/judgment_context.py`
- Created this report: `.superpowers/sdd/task-2-report.md`

No pre-existing untracked benchmark, report, spec, plan, or bytecode artifact was staged or modified.

## Implementation

- Added `TraceGraph.semantic_predecessor_edges()` for ordered, attribution-eligible incoming Causal IR edges and `TraceGraph.semantic_search()` for earlier token-overlap retrieval. Search returns retrieval facts only and never changes graph connectivity.
- Added `SemanticPredecessorRetriever.retrieve()` with ordered direct-edge, concrete episode/window, recorded sibling-context, and opt-in semantic-fallback layers. Deduplication retains the earliest, higher-provenance layer for a ref while retaining distinct inferred candidates through the requested limit.
- Confirmed/content-matched edges are marked `confirmed_edge`; semantic fallback is explicitly `semantic_fallback` with `semantic_inferred` evidence and no causal edge insertion.
- Added `build_recursive_judgment_context()` as an additive context builder. It serializes `DefectState` and its transformation chain, `AttributionHypothesis` evidence, candidate edge semantics, downstream path/judgments, obligations, agent/subagent scope, episode/window context, temporal-only advisory facts, and artifact hydration stats. Candidate entries deliberately carry no defect-status conclusion.
- Temporal-only facts remain visible in `temporal_adjacency`, but are excluded from direct predecessor retrieval.

## TDD Evidence

### RED 1

Command:

```bash
cd tools/trace_attribution
PYTHONPATH=. python3 -m unittest tests.test_causal_retrieval -v
```

Observed result before implementation: `ModuleNotFoundError: No module named 'trace_attribution.causal_retrieval'`; 1 test module failed to import. This was the intended missing-feature failure.

### GREEN 1

The same focused suite passed after adding graph and retriever/context production code: 4 tests passed.

### RED 2

After the first GREEN state, the recursive-context test was extended to require visible temporal-only context. Running:

```bash
PYTHONPATH=. python3 -m unittest tests.test_causal_retrieval.CausalRetrievalTest.test_recursive_context_carries_state_evidence_and_hydration_without_prejudging_candidates -v
```

failed with `KeyError: 'temporal_adjacency'`. This established that temporal facts were excluded from traversal but not yet visible to the Judge.

### GREEN 2 and Regression Verification

```bash
PYTHONPATH=. python3 -m unittest tests.test_causal_retrieval tests.test_backward_taint.TraceGraphTest -v
# Ran 32 tests: OK

PYTHONPATH=. python3 -m unittest discover -s tests -v
# Ran 143 tests: OK

PYTHONPYCACHEPREFIX=/private/tmp/trace-attribution-pycache \
  python3 -m py_compile trace_attribution/causal_retrieval.py \
  trace_attribution/graph.py trace_attribution/judgment_context.py
# Exit 0

git diff --check
# Exit 0, no whitespace errors
```

The full suite grows the stated 139-test baseline to 143 tests.

## Self-Review

- `DefectState`, `CausalCandidate`, and `AttributionHypothesis` are consumed as immutable value objects. The retriever never mutates their frozen mappings or tuples.
- Existing Causal IR edge context, message lineage, artifact hydration, `CausalEpisodeIndex`, and delivery-window reconstruction remain the source of truth; no parallel graph is created.
- Semantic fallback uses only `TraceGraph.semantic_search()` results, labels them as inferred, and does not update `_upstream`, `_downstream`, or edge context indexes.
- Direct retrieval accepts only attribution-eligible edges and skips aggregate `progress.episode` nodes. Episode/window retrieval returns concrete members instead.
- Temporal-only edges are emitted as context-only facts with `eligible_for_attribution: false`, and the retrieval tests assert they are absent from direct candidates.
- The recursive context does not supply a candidate `defect_status`, preserving Judge responsibility for semantic classification.

## Concerns

- The initial semantic fallback is deterministic token overlap over compact node content. It is intentionally conservative and provenance-marked, but richer ranking by obligation/state-change relevance belongs to a later recursive scheduler task.
- Task 2 supplies retrieval and Judge context only; it does not wire the retriever into the existing analyzer frontier. That integration is deferred to the subsequent recursive scheduling task in the approved implementation sequence.
