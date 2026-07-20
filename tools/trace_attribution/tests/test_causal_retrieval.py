import unittest

from trace_attribution.causal_state import AttributionHypothesis, CausalStepJudgment, DefectState
from trace_attribution.causal_retrieval import SemanticPredecessorRetriever
from trace_attribution.graph import TraceGraph
from trace_attribution.judgment_context import build_recursive_judgment_context


def sample_defect_state():
    return DefectState.create(
        label="missing_namespace_contract",
        expected="The parser preserves the namespace compatibility contract.",
        actual="The namespace compatibility method is absent.",
        mechanism="implementation planning omitted a called method",
        scope="parser_contract_recovery",
    )


def sample_hypothesis(defect_state):
    return AttributionHypothesis.create(
        claim="The implementation path omitted the namespace compatibility contract.",
        candidate_root_ref="record:decision",
        defect_state=defect_state,
    )


def trace_with_confirmed_and_inferred_predecessors():
    return {
        "case_id": "causal-retrieval-case",
        "records": [
            {
                "record_id": "prompt",
                "component": "prompt",
                "event_type": "message.input",
                "timestamp": "2026-07-20T10:00:00.000Z",
                "data": {"text": "Recover the parser namespace compatibility contract."},
            },
            {
                "record_id": "context",
                "component": "context",
                "event_type": "context.snapshot",
                "timestamp": "2026-07-20T10:00:01.000Z",
                "data": {"text": "The namespace contract includes compatibility behavior."},
            },
            {
                "record_id": "temporally_previous",
                "component": "tool",
                "event_type": "tool.result",
                "timestamp": "2026-07-20T10:00:02.000Z",
                "data": {"text": "A temporally adjacent but unrelated result."},
            },
            {
                "record_id": "sibling",
                "component": "processor",
                "event_type": "decision",
                "timestamp": "2026-07-20T10:00:03.000Z",
                "data": {
                    "rationale": "Search repository callers for the missing namespace contract.",
                    "metadata": {"sessionID": "ses_1", "messageID": "msg_sibling"},
                },
            },
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "timestamp": "2026-07-20T10:00:04.000Z",
                "data": {
                    "rationale": "Implement the parser change without searching all namespace callers.",
                    "metadata": {"sessionID": "ses_1", "messageID": "msg_current"},
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "prompt"},
                "to": {"type": "record", "id": "decision"},
                "relation": "prompt_informed_decision",
                "evidence_type": "confirmed",
                "confidence": 0.99,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "context"},
                "to": {"type": "record", "id": "decision"},
                "relation": "context_retained_in_decision",
                "evidence_type": "content_matched",
                "confidence": 0.87,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "temporally_previous"},
                "to": {"type": "record", "id": "decision"},
                "relation": "temporal_availability",
                "evidence_type": "temporal_inferred",
                "confidence": 0.25,
                "eligible_for_attribution": False,
            },
        ],
    }


class CausalRetrievalTest(unittest.TestCase):
    def test_retriever_prefers_confirmed_edges_without_dropping_inferred_candidates(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
            allow_semantic_fallback=True,
        )

        self.assertEqual(
            [item.ref for item in candidates],
            ["record:prompt", "record:context", "record:sibling"],
        )
        self.assertEqual(candidates[0].source, "confirmed_edge")
        self.assertEqual(candidates[-1].source, "semantic_fallback")
        self.assertEqual(candidates[-1].edge["evidence_type"], "semantic_inferred")

    def test_temporal_only_lineage_is_not_a_direct_predecessor(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
        )

        self.assertNotIn("record:temporally_previous", [item.ref for item in candidates])

    def test_semantic_search_is_retrieval_only(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        before_edges = graph.incoming_edge_context("record:decision")

        matches = graph.semantic_search(
            ["namespace", "contract"],
            before_ref="record:decision",
            limit=8,
        )

        self.assertIn("record:sibling", [item["ref"] for item in matches])
        self.assertEqual(graph.incoming_edge_context("record:decision"), before_edges)
        self.assertNotIn("record:sibling", graph.upstream_refs("record:decision"))

    def test_recursive_context_carries_state_evidence_and_hydration_without_prejudging_candidates(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        defect_state = sample_defect_state()
        hypothesis = sample_hypothesis(defect_state)
        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=hypothesis,
            limit=8,
        )

        context = build_recursive_judgment_context(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            defect_transformation_chain=[defect_state],
            hypothesis=hypothesis,
            candidates=candidates,
            downstream_path=["record:observed", "record:decision"],
            downstream_judgments=[
                CausalStepJudgment(
                    current_node_ref="record:observed",
                    current_defect_status="present",
                    current_defect_reason="The final parser contract is incomplete.",
                )
            ],
            objective="Recover all parser namespace compatibility behavior.",
        )

        self.assertEqual(context["defect_state"], defect_state.to_dict())
        self.assertEqual(context["defect_transformation_chain"], [defect_state.to_dict()])
        self.assertEqual(context["hypothesis"]["hypothesis_id"], hypothesis.hypothesis_id)
        self.assertEqual(context["candidate_predecessors"][0]["edge"]["relation"], "prompt_informed_decision")
        self.assertEqual(context["downstream_path"], ["record:observed", "record:decision"])
        self.assertEqual(context["downstream_judgments"][0]["current_node_ref"], "record:observed")
        self.assertEqual(context["task_obligations"][0]["text"], "Recover all parser namespace compatibility behavior.")
        self.assertEqual(context["agent_scope"]["session_id"], "ses_1")
        self.assertIn("artifact_hydration", context)
        self.assertEqual(context["temporal_adjacency"][0]["from_ref"], "record:temporally_previous")
        self.assertNotIn("defect_status", context["candidate_predecessors"][0])


if __name__ == "__main__":
    unittest.main()
