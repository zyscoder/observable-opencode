import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

from trace_attribution.analyzer import BackwardTaintAnalyzer
from trace_attribution.claude import ClaudeJudgeClient, build_judgment_prompt
from trace_attribution.graph import TraceGraph
from trace_attribution.models import NodeJudgment, TaintInfluence
from trace_attribution.models import TraceNode
from trace_attribution.quality_review import inject_quality_gap_records


class FakeJudge:
    def __init__(self, judgments):
        self.judgments = judgments
        self.calls = []

    def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
        self.calls.append(node.ref)
        return self.judgments.get(
            node.ref,
            NodeJudgment(
                node_ref=node.ref,
                component=node.component,
                event_type=node.event_type,
                has_defect=False,
                defect_reason="No defect detected in fake judge.",
            ),
        )


def sample_trace():
    return {
        "trace_version": "5.5",
        "manifest": {"case_id": "unit-case"},
        "records": [
            {
                "record_id": "evidence_old",
                "component": "tool",
                "event_type": "evidence.semantic_fact",
                "data": {
                    "structured_claim": {
                        "subject": "discount",
                        "predicate": "discount_cap",
                        "value": "20 percent",
                    },
                    "semantic_role": "legacy_historical",
                },
            },
            {
                "record_id": "change_bad",
                "component": "tool",
                "event_type": "change",
                "source_refs": ["evidence:evidence_old"],
                "data": {"change_id": "chg_bad", "files": ["src/pricing.mjs"], "diff": "- 0.2\n+ 0.2"},
            },
            {
                "record_id": "claim_bad",
                "component": "result",
                "event_type": "response.claim",
                "source_refs": ["change:chg_bad", "evidence:evidence_old"],
                "data": {
                    "text": "Fixed the discount cap to 20 percent.",
                    "quality_flags": ["conflicting_evidence"],
                    "direct_evidence_refs": ["evidence:evidence_old"],
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "evidence", "id": "evidence_old"},
                "to": {"type": "change", "id": "change_bad"},
                "relation": "informed",
            },
            {
                "from": {"type": "change", "id": "change_bad"},
                "to": {"type": "response_claim", "id": "claim_bad"},
                "relation": "claimed_by",
            },
        ],
        "metrics": {"trace_health": {"legacy_fact_used_in_final_claim": 1}},
    }


class TraceGraphTest(unittest.TestCase):
    def test_resolves_source_refs_and_dataflow_edges(self):
        graph = TraceGraph.from_trace(sample_trace())

        self.assertIn("record:claim_bad", graph.nodes)
        upstream = graph.upstream_refs("record:claim_bad")

        self.assertIn("record:change_bad", upstream)
        self.assertIn("record:evidence_old", upstream)

    def test_loads_trace_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_file = Path(tmp) / "trace.json"
            trace_file.write_text(json.dumps(sample_trace()), encoding="utf-8")

            graph = TraceGraph.from_file(trace_file)

        self.assertEqual(graph.case_id, "unit-case")
        self.assertEqual(graph.nodes["record:evidence_old"].event_type, "evidence.semantic_fact")

    def test_quality_review_gaps_become_default_start_refs(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "quality_review": {
                "total_score": 55,
                "target_score": 80,
                "quality_gaps": [
                    {
                        "dimension": "architecture_reasoning",
                        "score": 5,
                        "max_score": 20,
                        "missing_evidence": ["architecture_boundary_reasoning"],
                        "record_refs": ["record:claim_bad"],
                    }
                ],
            },
        }

        enriched = inject_quality_gap_records(trace, review)
        graph = TraceGraph.from_trace(enriched)

        self.assertIn("record:quality_gap_architecture_reasoning", graph.nodes)
        gap = graph.nodes["record:quality_gap_architecture_reasoning"]
        self.assertEqual(gap.data["gap_kind"], "quality_dimension_under_target")
        self.assertEqual(gap.data["score_ratio"], 0.25)
        self.assertEqual(graph.default_start_refs(), ["record:quality_gap_architecture_reasoning"])
        self.assertIn("record:claim_bad", graph.upstream_refs("record:quality_gap_architecture_reasoning"))


class BackwardTaintAnalyzerTest(unittest.TestCase):
    def test_backtracks_until_defect_introduction_node(self):
        fake = FakeJudge(
            {
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_type="wrong_final_claim",
                    defect_reason="Final claim used a stale discount cap.",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:change_bad",
                            reason="The final claim reported the bad change result.",
                            confidence=0.8,
                        )
                    ],
                ),
                "record:change_bad": NodeJudgment(
                    node_ref="record:change_bad",
                    component="tool",
                    event_type="change",
                    has_defect=True,
                    defect_type="wrong_change",
                    defect_reason="Change preserved the stale 20 percent cap.",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:evidence_old",
                            reason="The change followed stale evidence.",
                            confidence=0.9,
                        )
                    ],
                ),
                "record:evidence_old": NodeJudgment(
                    node_ref="record:evidence_old",
                    component="tool",
                    event_type="evidence.semantic_fact",
                    has_defect=True,
                    defect_type="stale_evidence_selected",
                    defect_reason="Legacy evidence was treated as current requirement evidence.",
                    influenced_by=[],
                    is_root_cause=True,
                ),
            }
        )
        graph = TraceGraph.from_trace(sample_trace())
        report = BackwardTaintAnalyzer(judge=fake, max_depth=8).analyze(
            graph,
            start_refs=["record:claim_bad"],
            objective="Find why the final answer used the wrong discount cap.",
        )

        self.assertEqual(report.case_id, "unit-case")
        self.assertEqual([candidate.node_ref for candidate in report.root_causes], ["record:evidence_old"])
        self.assertEqual(report.node_judgments["record:change_bad"].defect_type, "wrong_change")
        self.assertEqual(report.taint_paths[0], ["record:claim_bad", "record:change_bad", "record:evidence_old"])


class ClaudeJudgeClientTest(unittest.TestCase):
    def test_passes_base_url_to_anthropic_compatible_client(self):
        calls = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            with mock.patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "test-key",
                    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                },
            ):
                client = ClaudeJudgeClient()

        self.assertEqual(client.base_url, "https://api.deepseek.com/anthropic")
        self.assertEqual(client.max_tokens, 4096)
        self.assertEqual(calls, [{"api_key": "test-key", "base_url": "https://api.deepseek.com/anthropic"}])

    def test_quality_gap_prompt_treats_gap_as_defect_to_explain(self):
        prompt = build_judgment_prompt(
            node=TraceNode(
                ref="record:quality_gap_solution_design",
                record_id="quality_gap_solution_design",
                component="evaluation",
                event_type="case.quality_gap",
                data={
                    "dimension": "solution_design",
                    "missing_evidence": ["risk_assessment"],
                    "score": 17,
                    "max_score": 25,
                },
            ),
            upstream_nodes=[],
            downstream_context=["record:quality_gap_solution_design"],
            objective="Explain the quality gap.",
        )

        self.assertIn("case.quality_gap", prompt)
        self.assertIn("treat the quality gap as the defect to explain", prompt)

    def test_repairs_malformed_json_judgment_once(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return types.SimpleNamespace(
                        content=[
                            types.SimpleNamespace(
                                text='{"has_defect": true, "defect_type": "missing_risk", "defect_reason": "truncated"'
                            )
                        ]
                    )
                return types.SimpleNamespace(
                    content=[
                        types.SimpleNamespace(
                            text=json.dumps(
                                {
                                    "node_ref": "record:quality_gap_solution_design",
                                    "component": "evaluation",
                                    "event_type": "case.quality_gap",
                                    "has_defect": True,
                                    "defect_type": "missing_risk",
                                    "defect_reason": "The response omitted risk assessment.",
                                    "influenced_by": [],
                                    "is_root_cause": True,
                                    "severity": "medium",
                                    "confidence": 0.7,
                                    "model_notes": "repaired from malformed output",
                                }
                            )
                        )
                    ]
                )

        class FakeAnthropic:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                client = ClaudeJudgeClient(model="fake-model")

        judgment = client.judge_node(
            node=TraceNode(
                ref="record:quality_gap_solution_design",
                record_id="quality_gap_solution_design",
                component="evaluation",
                event_type="case.quality_gap",
            ),
            upstream_nodes=[],
            downstream_context=["record:quality_gap_solution_design"],
            objective="Explain the quality gap.",
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(judgment.has_defect)
        self.assertEqual(judgment.defect_type, "missing_risk")
        self.assertEqual(judgment.model_notes, "repaired from malformed output")


if __name__ == "__main__":
    unittest.main()
