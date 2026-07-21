from __future__ import annotations

import unittest

from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution.evidence_capsule import (
    build_candidate_evidence_capsules,
    candidate_compression_metrics,
)
from trace_attribution.graph import TraceGraph


def sample_graph() -> TraceGraph:
    return TraceGraph.from_trace(
        {
            "case_id": "evidence-capsule-case",
            "artifacts": [
                {
                    "artifact_id": "decision-rationale",
                    "kind": "text",
                    "path": "artifacts/missing-rationale.txt",
                }
            ],
            "records": [
                {
                    "record_id": "prompt",
                    "component": "prompt",
                    "event_type": "message.input",
                    "data": {"text": "Preserve cleanup after process SIGINT."},
                },
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:prompt"],
                    "artifact_refs": ["artifact:decision-rationale"],
                    "data": {
                        "decision_id": "dec_1",
                        "rationale": "Direct parent cancellation is sufficient to model process SIGINT.",
                        "metadata": {"callID": "call_1"},
                    },
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"call_id": "call_1", "tool_name": "write"},
                },
                {
                    "record_id": "tool_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "call_id": "call_1",
                        "status": "success",
                        "output": {"preview": "Wrote cancellation test."},
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:decision"],
                    "data": {
                        "failure_type": "sigint_cleanup_interrupted",
                        "expected": "Started cleanup completes after SIGINT.",
                        "actual": "Started cleanup is interrupted.",
                    },
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "prompt"},
                    "to": {"type": "record", "id": "decision"},
                    "relation": "prompt_informed_decision",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "decision_exposed_by_evaluation",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                },
            ],
        }
    )


class CandidateEvidenceCapsuleTest(unittest.TestCase):
    def test_capsule_closes_candidate_path_action_group_and_missing_artifacts(self):
        graph = sample_graph()
        defect = DefectState.create(
            label="sigint_cleanup_interrupted",
            expected="Started cleanup completes after SIGINT.",
            actual="Started cleanup is interrupted.",
            mechanism="The authored cancellation model may not preserve cleanup.",
            scope="task_quality",
        )
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="progress_window",
            edge={
                "from_ref": "record:decision",
                "to_ref": "record:observed_defect",
                "relation": "progress_window_candidate",
                "evidence_type": "offline_reconstruction",
                "evidence_refs": ["record:decision"],
            },
            score=0.91,
            evidence_refs=("record:decision",),
        )

        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate],
            defect_state=defect,
            downstream_paths={
                "record:decision": (
                    "record:decision",
                    "record:observed_defect",
                )
            },
            start_refs=("record:observed_defect",),
        )

        self.assertEqual(len(capsules), 1)
        capsule = capsules[0].to_dict()
        self.assertEqual(capsule["candidate_ref"], "record:decision")
        self.assertEqual(
            capsule["downstream_path"],
            ["record:decision", "record:observed_defect"],
        )
        self.assertEqual(capsule["action_group"]["identity"], "call_id:call_1")
        self.assertEqual(
            {item["ref"] for item in capsule["action_group"]["members"]},
            {"record:decision", "record:tool_call", "record:tool_result"},
        )
        self.assertEqual(
            capsule["artifact_hydration"]["missing_artifact_ids"],
            ["decision-rationale"],
        )
        self.assertIn("artifact:decision-rationale", capsule["missing_evidence_refs"])
        self.assertTrue(
            any(
                edge["relation"] == "prompt_informed_decision"
                for edge in capsule["incoming_edges"]
            )
        )
        self.assertTrue(
            any(
                edge["relation"] == "decision_exposed_by_evaluation"
                for edge in capsule["outgoing_edges"]
            )
        )

    def test_compression_metrics_compare_unique_capsules_with_trace_nodes(self):
        graph = sample_graph()
        defect = DefectState.create(
            label="sigint_cleanup_interrupted",
            expected="cleanup completes",
            actual="cleanup interrupted",
            mechanism="cancellation mismatch",
            scope="task_quality",
        )
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="semantic_fallback",
            score=0.8,
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate, candidate],
            defect_state=defect,
            downstream_paths={},
            start_refs=("record:observed_defect",),
        )

        metrics = candidate_compression_metrics(graph, capsules)

        self.assertEqual(metrics["trace_node_count"], 5)
        self.assertEqual(metrics["candidate_count"], 1)
        self.assertEqual(metrics["candidate_node_reduction_ratio"], 0.8)
        self.assertGreater(metrics["capsule_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
