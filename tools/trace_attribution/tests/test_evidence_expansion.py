from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from trace_attribution.evidence_expansion import (
    EvidenceExpansionRequest,
    ExpansionLimits,
    expand_evidence,
    validate_evidence_expansion_result_against_graph,
)
from trace_attribution.graph import TraceGraph


def expansion_trace() -> dict:
    return {
        "case_id": "bounded-evidence-expansion",
        "records": [
            {
                "record_id": "prompt",
                "component": "prompt",
                "event_type": "message.input",
                "data": {"text": "Keep cleanup alive after SIGTERM."},
            },
            {
                "record_id": "reasoning",
                "component": "processor",
                "event_type": "decision",
                "source_refs": ["record:prompt"],
                "data": {
                    "session_id": "ses_1",
                    "message_id": "msg_1",
                    "decision_type": "reasoning_block",
                    "rationale": "Direct parent cancellation should model SIGTERM.",
                },
            },
            {
                "record_id": "tool_decision",
                "component": "processor",
                "event_type": "decision",
                "source_refs": ["record:reasoning"],
                "data": {
                    "session_id": "ses_1",
                    "message_id": "msg_1",
                    "decision_type": "llm_tool_call",
                    "call_id": "call_1",
                    "rationale": "Patch the cancellation handler.",
                },
            },
            {
                "record_id": "tool_call",
                "component": "tool",
                "event_type": "tool.call",
                "source_refs": ["record:tool_decision"],
                "data": {
                    "session_id": "ses_1",
                    "message_id": "msg_1",
                    "call_id": "call_1",
                    "tool_name": "write",
                    "path": "src/signal.ts",
                },
            },
            {
                "record_id": "llm",
                "component": "llm",
                "event_type": "llm.call",
                "source_refs": ["record:tool_call"],
                "data": {
                    "session_id": "ses_1",
                    "message_id": "msg_2",
                    "input_message_count": 4,
                    "message_transforms": [
                        {
                            "stage": "provider_conversion",
                            "transforms": [
                                {
                                    "name": "normalize_tool_result",
                                    "input_hash": "sha256:before",
                                    "output_hash": "sha256:after",
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:llm"],
                "data": {
                    "expected": "Started cleanup completes after SIGTERM.",
                    "actual": "Started cleanup is cancelled.",
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "prompt"},
                "to": {"type": "record", "id": "reasoning"},
                "relation": "prompt_informed_decision",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "reasoning"},
                "to": {"type": "record", "id": "tool_decision"},
                "relation": "reasoning_selected_action",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "tool_decision"},
                "to": {"type": "record", "id": "tool_call"},
                "relation": "decision_executed_by_tool",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "tool_call"},
                "to": {"type": "record", "id": "llm"},
                "relation": "tool_result_included_in_request",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "llm"},
                "to": {"type": "record", "id": "defect"},
                "relation": "response_exposed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
        ],
    }


def request(
    *,
    anchor_ref: str = "record:tool_decision",
    context_kind: str = "upstream",
) -> EvidenceExpansionRequest:
    return EvidenceExpansionRequest(
        seed_ref="record:defect",
        defect_fingerprint="defect:v1",
        anchor_ref=anchor_ref,
        context_kind=context_kind,
        reason="Determine whether the active defect existed before this node.",
        expected_judgment_change=(
            "The candidate may change from root_candidate to propagation_only."
        ),
    )


class BoundedEvidenceExpansionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = TraceGraph.from_trace(expansion_trace())

    def test_expands_only_requested_anchor_and_upstream_kind(self):
        result = expand_evidence(
            self.graph,
            request(),
            ExpansionLimits(max_nodes=6, max_bytes=24_000),
        )

        self.assertEqual(result.status, "expanded")
        self.assertTrue(result.items)
        self.assertEqual(result.request_identity, request().identity)
        self.assertTrue(
            all(item["direction"] == "upstream" for item in result.items)
        )
        self.assertEqual(
            {item["resolved_ref"] for item in result.items},
            {"record:reasoning"},
        )
        self.assertLessEqual(result.total_bytes, 24_000)
        validate_evidence_expansion_result_against_graph(
            self.graph, result, limits=ExpansionLimits(max_nodes=6, max_bytes=24_000)
        )

    def test_downstream_expansion_uses_only_causal_adjacency(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="downstream"),
            ExpansionLimits(max_nodes=3, max_bytes=24_000),
        )

        self.assertEqual(result.status, "expanded")
        self.assertEqual(
            [item["resolved_ref"] for item in result.items],
            ["record:tool_call"],
        )
        self.assertTrue(
            all(
                edge["eligible_for_attribution"] is True
                for item in result.items
                for edge in item["edges"]
            )
        )

    def test_rejects_invalid_anchor_without_scanning_the_graph(self):
        result = expand_evidence(
            self.graph,
            request(anchor_ref="record:missing"),
            ExpansionLimits(),
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection_code, "anchor_unresolved")
        self.assertEqual(result.items, ())
        self.assertEqual(result.inspected_node_count, 0)

    def test_rejects_duplicate_request_identity(self):
        expansion_request = request()
        result = expand_evidence(
            self.graph,
            expansion_request,
            ExpansionLimits(),
            seen_request_identities={expansion_request.identity},
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection_code, "duplicate_request")
        self.assertEqual(result.items, ())

    def test_message_transform_requires_an_llm_anchor(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="message_transform"),
            ExpansionLimits(),
        )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection_code, "anchor_kind_mismatch")

    def test_message_transform_preserves_recorded_stages_and_lineage_snapshot(self):
        result = expand_evidence(
            self.graph,
            request(anchor_ref="record:llm", context_kind="message_transform"),
            ExpansionLimits(max_nodes=4, max_bytes=24_000),
        )

        self.assertEqual(result.status, "expanded")
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item["kind"], "message_transform")
        self.assertEqual(
            item["message_transforms"][0]["stage"],
            "provider_conversion",
        )
        self.assertEqual(item["lineage_snapshot"]["node_ref"], "record:llm")

    def test_action_group_expansion_is_bounded_to_recorded_call_identity(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="action_group"),
            ExpansionLimits(max_nodes=2, max_bytes=24_000),
        )

        self.assertEqual(result.status, "expanded")
        group = result.items[0]["action_group"]
        self.assertEqual(group["identity"], "call_id:call_1")
        self.assertLessEqual(len(group["members"]), 2)
        self.assertEqual(
            {item["ref"] for item in group["members"]},
            {"record:tool_decision", "record:tool_call"},
        )

    def test_full_node_expansion_contains_only_the_anchor_snapshot(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="full_node"),
            ExpansionLimits(max_nodes=2, max_bytes=24_000),
        )

        self.assertEqual(result.status, "expanded")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0]["resolved_ref"], "record:tool_decision")
        self.assertEqual(result.items[0]["node"]["ref"], "record:tool_decision")

    def test_artifact_larger_than_byte_budget_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()
            artifact_content = "x" * 16_000
            artifact_bytes = artifact_content.encode("utf-8")
            (artifact_dir / "large.txt").write_text(
                artifact_content, encoding="utf-8"
            )
            trace = expansion_trace()
            trace["artifacts"] = [
                {
                    "artifact_id": "large",
                    "kind": "text",
                    "path": "artifacts/large.txt",
                    "content_hash": "sha256:{0}".format(
                        hashlib.sha256(artifact_bytes).hexdigest()
                    ),
                    "byte_length": len(artifact_bytes),
                }
            ]
            trace["records"][2]["artifact_refs"] = ["artifact:large"]
            graph = TraceGraph.from_trace(trace, artifact_root=root)

            result = expand_evidence(
                graph,
                request(context_kind="artifact"),
                ExpansionLimits(max_nodes=3, max_bytes=2_000),
            )

        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rejection_code, "byte_budget_exceeded")
        self.assertEqual(result.items, ())

    def test_graph_validation_rejects_tampered_expansion_content(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="full_node"),
            ExpansionLimits(max_nodes=2, max_bytes=24_000),
        )
        payload = result.to_dict()
        payload["items"][0]["node"]["data"]["rationale"] = "Forged rationale."

        with self.assertRaisesRegex(
            ValueError, "byte accounting|active graph|canonical"
        ):
            validate_evidence_expansion_result_against_graph(
                self.graph,
                payload,
                limits=ExpansionLimits(max_nodes=2, max_bytes=24_000),
            )

    def test_expanded_evidence_is_deeply_immutable(self):
        result = expand_evidence(
            self.graph,
            request(context_kind="full_node"),
            ExpansionLimits(max_nodes=2, max_bytes=24_000),
        )

        with self.assertRaises(TypeError):
            result.items[0]["node"]["data"]["rationale"] = "Mutated rationale."


if __name__ == "__main__":
    unittest.main()
