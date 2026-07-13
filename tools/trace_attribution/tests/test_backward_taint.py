import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

from trace_attribution.analyzer import BackwardTaintAnalyzer
from trace_attribution.claude import ClaudeJudgeClient, build_judgment_prompt, call_with_wall_timeout, run_worker_with_timeout
from trace_attribution.graph import TraceGraph
from trace_attribution.models import NodeJudgment, TaintInfluence, stable_json
from trace_attribution.models import TraceNode
from trace_attribution.quality_review import inject_quality_gap_records
from trace_attribution.trace_improvement import build_trace_improvement_report


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


def slow_worker(payload, result_queue):
    time.sleep(payload["sleep"])
    result_queue.put({"ok": True, "text": "done"})


class TraceGraphTest(unittest.TestCase):
    def test_resolves_source_refs_and_dataflow_edges(self):
        graph = TraceGraph.from_trace(sample_trace())

        self.assertIn("record:claim_bad", graph.nodes)
        upstream = graph.upstream_refs("record:claim_bad")

        self.assertIn("record:change_bad", upstream)
        self.assertIn("record:evidence_old", upstream)

    def test_upstream_refs_preserve_explicit_source_ref_order(self):
        trace = {
            "case_id": "order-case",
            "records": [
                {"record_id": "a", "component": "tool", "event_type": "tool.result"},
                {"record_id": "b", "component": "tool", "event_type": "tool.result"},
                {"record_id": "c", "component": "tool", "event_type": "tool.error"},
                {
                    "record_id": "target",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:c", "record:a"],
                },
            ],
            "dataflow_edges": [
                {"from": {"type": "record", "id": "b"}, "to": {"type": "record", "id": "target"}}
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.upstream_refs("record:target"), ["record:c", "record:a", "record:b"])

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

    def test_review_missing_semantics_become_default_start_refs(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "missing_semantics": ["final_test_result", "change_diff_semantics"],
            "evidence_found": [
                {"required": "final_test_result", "status": "missing", "record_refs": []},
                {
                    "required": "change_diff_semantics",
                    "status": "missing",
                    "record_refs": ["record:change_bad"],
                },
            ],
            "ground_truth_root_cause": {
                "component": "verification",
                "failure_type": "insufficient_test_scope",
            },
        }

        enriched = inject_quality_gap_records(trace, review)
        graph = TraceGraph.from_trace(enriched)

        self.assertIn("record:missing_semantic_final_test_result", graph.nodes)
        self.assertIn("record:missing_semantic_change_diff_semantics", graph.nodes)
        missing = graph.nodes["record:missing_semantic_change_diff_semantics"]
        self.assertEqual(missing.event_type, "case.missing_semantic")
        self.assertEqual(missing.data["semantic_name"], "change_diff_semantics")
        self.assertEqual(missing.source_refs, ["record:change_bad"])
        self.assertEqual(
            graph.default_start_refs(),
            ["record:missing_semantic_final_test_result", "record:missing_semantic_change_diff_semantics"],
        )

    def test_review_root_cause_becomes_offline_observed_defect_start_ref(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "trace_sufficiency": "sufficient",
            "case_effectiveness": "effective",
            "ground_truth_root_cause": {
                "component": "evidence_selection",
                "failure_type": "stale_evidence_trusted",
                "description": "The agent trusted stale evidence.",
            },
            "evidence_found": [
                {"required": "conflict_fact_group", "status": "found", "record_refs": ["record:evidence_old"]},
                {"required": "claim_direct_evidence_refs", "status": "found", "record_refs": ["record:claim_bad"]},
            ],
        }

        enriched = inject_quality_gap_records(trace, review)
        graph = TraceGraph.from_trace(enriched)

        self.assertIn("record:observed_defect_evidence_selection_stale_evidence_trusted", graph.nodes)
        defect = graph.nodes["record:observed_defect_evidence_selection_stale_evidence_trusted"]
        self.assertEqual(defect.event_type, "case.observed_defect")
        self.assertEqual(defect.data["component"], "evidence_selection")
        self.assertEqual(defect.data["failure_type"], "stale_evidence_trusted")
        self.assertEqual(defect.source_refs, ["record:evidence_old", "record:claim_bad"])
        self.assertEqual(graph.default_start_refs(), ["record:observed_defect_evidence_selection_stale_evidence_trusted"])
        self.assertIn("record:evidence_old", graph.upstream_refs("record:observed_defect_evidence_selection_stale_evidence_trusted"))

    def test_review_observed_defect_caps_broad_evidence_refs(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "ground_truth_root_cause": {
                "component": "tool_error_handling",
                "failure_type": "hallucinated_after_tool_failure",
            },
            "mechanism_evidence_found": [
                {"required": "tool.error", "status": "found", "record_refs": ["record:evidence_old"]},
            ],
            "evidence_found": [
                {
                    "required": "broad_context",
                    "status": "found",
                    "record_refs": [f"record:broad_{index}" for index in range(80)],
                }
            ],
        }

        enriched = inject_quality_gap_records(trace, review)
        graph = TraceGraph.from_trace(enriched)
        defect = graph.nodes["record:observed_defect_tool_error_handling_hallucinated_after_tool_failure"]

        self.assertLessEqual(len(defect.source_refs), 24)
        self.assertEqual(defect.source_refs[0], "record:evidence_old")
        self.assertEqual(defect.data["source_ref_count_total"], 81)
        self.assertEqual(defect.data["source_ref_count_included"], len(defect.source_refs))

    def test_trace_node_compact_respects_character_budget(self):
        node = TraceNode(
            ref="record:large_context",
            record_id="large_context",
            component="context",
            event_type="context.snapshot",
            source_refs=[f"record:source_{index}" for index in range(40)],
            data={"large_text": "x" * 5000, "decision": "keep only budgeted semantic preview"},
        )

        compact = node.compact(max_chars=700)

        self.assertLessEqual(len(stable_json(compact)), 700)
        self.assertTrue(compact["truncated"])
        self.assertEqual(compact["ref"], "record:large_context")
        self.assertIn("data_preview", compact)


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

    def test_reports_trace_gaps_when_root_cause_stops_at_thin_llm_call(self):
        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "llm_thin",
                "component": "llm",
                "event_type": "llm.call",
                "data": {"model": "deepseek-v4-pro", "output_tokens": 34},
            }
        )
        trace["records"].append(
            {
                "record_id": "quality_gap",
                "component": "evaluation",
                "event_type": "case.quality_gap",
                "source_refs": ["record:claim_bad"],
                "data": {
                    "dimension": "solution_design",
                    "missing_evidence": ["risk_assessment"],
                    "score": 17,
                    "max_score": 25,
                },
            }
        )
        trace["dataflow_edges"].append(
            {
                "from": {"type": "record", "id": "llm_thin"},
                "to": {"type": "record", "id": "claim_bad"},
                "relation": "generated_claim",
            }
        )
        fake = FakeJudge(
            {
                "record:quality_gap": NodeJudgment(
                    node_ref="record:quality_gap",
                    component="evaluation",
                    event_type="case.quality_gap",
                    has_defect=True,
                    defect_type="missing_evidence",
                    defect_reason="The quality gap is caused by a final claim that omits risk assessment.",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:claim_bad",
                            reason="The final claim omitted risk assessment.",
                            confidence=0.9,
                        )
                    ],
                ),
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_type="missing_risk_assessment",
                    defect_reason="The final claim omitted risk assessment.",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:llm_thin",
                            reason="The LLM generated the incomplete final claim.",
                            confidence=0.8,
                        )
                    ],
                ),
                "record:llm_thin": NodeJudgment(
                    node_ref="record:llm_thin",
                    component="llm",
                    event_type="llm.call",
                    has_defect=True,
                    defect_type="missing_risk_assessment",
                    defect_reason="The LLM call appears to have generated the incomplete response.",
                    influenced_by=[],
                    is_root_cause=True,
                    confidence=0.55,
                ),
            }
        )
        graph = TraceGraph.from_trace(trace)
        report = BackwardTaintAnalyzer(judge=fake, max_depth=8).analyze(
            graph,
            start_refs=["record:quality_gap"],
            objective="Explain the solution design quality gap.",
        )

        improvement = build_trace_improvement_report(graph, report)
        report_dict = report.to_dict()

        self.assertIn("trace_improvement_report", report_dict)
        self.assertEqual(report_dict["trace_improvement_report"], improvement)
        self.assertIn("llm_call_missing_generation_semantics", [gap["gap_type"] for gap in improvement["blocking_gaps"]])
        self.assertIn("low_confidence_root_cause", [gap["gap_type"] for gap in improvement["blocking_gaps"]])
        llm_gap = next(gap for gap in improvement["blocking_gaps"] if gap["gap_type"] == "llm_call_missing_generation_semantics")
        self.assertEqual(llm_gap["node_ref"], "record:llm_thin")
        self.assertIn("message_transforms", llm_gap["missing_semantic_fields"])
        self.assertIn("output_text", llm_gap["missing_semantic_fields"])
        self.assertIn("llm", [item["component"] for item in improvement["recommended_trace_changes"]])

    def test_answer_surface_roots_make_improvement_confidence_limited(self):
        fake = FakeJudge(
            {
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_type="unsupported_final_claim",
                    defect_reason="The final claim is unsupported by the trace.",
                    influenced_by=[],
                    is_root_cause=True,
                    confidence=0.9,
                ),
            }
        )
        graph = TraceGraph.from_trace(sample_trace())
        report = BackwardTaintAnalyzer(judge=fake, max_depth=8).analyze(
            graph,
            start_refs=["record:claim_bad"],
            objective="Explain the unsupported final claim.",
        )

        improvement = report.trace_improvement_report

        self.assertIn("answer_surface_root_cause", [gap["gap_type"] for gap in improvement["blocking_gaps"]])
        self.assertEqual(improvement["summary"]["analysis_confidence"], "limited")

    def test_preserves_defective_boundary_when_upstream_is_nondefective(self):
        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "quality_gap",
                "component": "evaluation",
                "event_type": "case.quality_gap",
                "source_refs": ["record:claim_bad"],
                "data": {
                    "dimension": "solution_design",
                    "missing_evidence": ["risk_assessment"],
                },
            }
        )
        fake = FakeJudge(
            {
                "record:quality_gap": NodeJudgment(
                    node_ref="record:quality_gap",
                    component="evaluation",
                    event_type="case.quality_gap",
                    has_defect=True,
                    defect_type="missing_risk_assessment",
                    defect_reason="The answer lacks risk assessment.",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:claim_bad",
                            reason="The final claim did not include risk assessment.",
                            confidence=0.9,
                        )
                    ],
                    confidence=0.9,
                ),
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=False,
                    defect_reason="The claim is factually correct within its narrow scope.",
                    confidence=0.8,
                ),
            }
        )
        graph = TraceGraph.from_trace(trace)

        report = BackwardTaintAnalyzer(judge=fake, max_depth=4).analyze(
            graph,
            start_refs=["record:quality_gap"],
            objective="Explain the solution design quality gap.",
        )

        self.assertEqual([candidate.node_ref for candidate in report.root_causes], ["record:quality_gap"])
        self.assertEqual(report.root_causes[0].defect_type, "missing_risk_assessment")
        self.assertEqual(report.taint_paths, [["record:quality_gap"]])
        gap_types = [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]]
        self.assertIn("defective_node_points_to_nondefective_upstream", gap_types)

    def test_judge_errors_become_partial_boundary_roots(self):
        class ErrorJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                raise TimeoutError("judge timed out")

        graph = TraceGraph.from_trace(sample_trace())

        report = BackwardTaintAnalyzer(judge=ErrorJudge({}), max_depth=4).analyze(
            graph,
            start_refs=["record:claim_bad"],
            objective="Explain the final claim.",
        )

        self.assertEqual([candidate.node_ref for candidate in report.root_causes], ["record:claim_bad"])
        self.assertEqual(report.root_causes[0].defect_type, "judge_error")
        self.assertEqual(report.metadata["judge_error_count"], 1)
        self.assertIn("record:claim_bad", report.metadata["judge_errors"][0]["node_ref"])
        gap_types = [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]]
        self.assertIn("judge_error", gap_types)

    def test_observed_defect_judge_error_falls_back_to_source_refs(self):
        class ErrorJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                raise TimeoutError("judge timed out")

        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "observed_defect_tool_failure",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:claim_bad", "record:evidence_old"],
                "data": {
                    "component": "tool_error_handling",
                    "failure_type": "hallucinated_after_tool_failure",
                },
            }
        )
        graph = TraceGraph.from_trace(trace)

        report = BackwardTaintAnalyzer(judge=ErrorJudge({}), max_depth=2, max_nodes=8).analyze(
            graph,
            start_refs=["record:observed_defect_tool_failure"],
            objective="Explain the observed tool failure defect.",
        )

        observed = report.node_judgments["record:observed_defect_tool_failure"]
        root_refs = [candidate.node_ref for candidate in report.root_causes]

        self.assertEqual(observed.defect_type, "judge_unavailable_observed_defect_boundary")
        self.assertEqual(
            sorted(influence.upstream_ref for influence in observed.influenced_by),
            ["record:claim_bad", "record:evidence_old"],
        )
        self.assertNotIn("record:observed_defect_tool_failure", root_refs)
        self.assertIn("record:claim_bad", report.visited_order)
        self.assertIn("record:evidence_old", report.visited_order)

    def test_tool_error_judge_error_uses_semantic_fallback(self):
        class ErrorJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                raise TimeoutError("judge timed out")

        trace = {
            "case_id": "tool-error-case",
            "records": [
                {
                    "record_id": "tool_error",
                    "component": "tool",
                    "event_type": "tool.error",
                    "data": {
                        "tool_name": "read",
                        "call_id": "call_123",
                        "error_kind": "file_not_found",
                        "error_message": "File not found: docs/current-requirement.md",
                        "observed_by_model": True,
                        "handled_status": "recovered_with_replacement_evidence",
                    },
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)

        report = BackwardTaintAnalyzer(judge=ErrorJudge({}), max_depth=2).analyze(
            graph,
            start_refs=["record:tool_error"],
            objective="Explain the observed tool failure defect.",
        )

        root = report.root_causes[0]

        self.assertEqual(root.node_ref, "record:tool_error")
        self.assertEqual(root.defect_type, "tool_error_observed")
        self.assertGreater(root.confidence, 0.1)
        self.assertIn("read", root.reason)
        self.assertIn("File not found", root.reason)
        self.assertEqual(report.node_judgments["record:tool_error"].model_notes, "TimeoutError: judge timed out")


class ClaudeJudgeClientTest(unittest.TestCase):
    def test_call_with_wall_timeout_raises_timeout_error(self):
        started = time.time()

        with self.assertRaises(TimeoutError):
            call_with_wall_timeout(lambda: time.sleep(1), 0.05)

        self.assertLess(time.time() - started, 0.5)

    def test_run_worker_with_timeout_terminates_blocked_worker(self):
        started = time.time()

        with self.assertRaises(TimeoutError):
            run_worker_with_timeout(slow_worker, {"sleep": 1}, 0.05)

        self.assertLess(time.time() - started, 0.5)

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
                client = ClaudeJudgeClient(timeout_seconds=12)

        self.assertEqual(client.base_url, "https://api.deepseek.com/anthropic")
        self.assertEqual(client.max_tokens, 4096)
        self.assertEqual(
            calls,
            [{"api_key": "test-key", "base_url": "https://api.deepseek.com/anthropic", "timeout": 12}],
        )

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

    def test_judgment_prompt_keeps_semantics_under_budget(self):
        node = TraceNode(
            ref="record:observed_defect_tool_error",
            record_id="observed_defect_tool_error",
            component="evaluation",
            event_type="case.observed_defect",
            data={"failure_type": "hallucinated_after_tool_failure", "description": "The answer ignored tool errors."},
        )
        upstream_nodes = [
            TraceNode(
                ref=f"record:upstream_{index}",
                record_id=f"upstream_{index}",
                component="tool",
                event_type="tool.result",
                data={"text": "important result " + ("x" * 4000), "call_id": f"call_{index}"},
            )
            for index in range(12)
        ]

        prompt = build_judgment_prompt(
            node=node,
            upstream_nodes=upstream_nodes,
            downstream_context=[node.ref],
            objective="Find the first component that introduced the observed defect.",
        )

        self.assertLess(len(prompt), 14000)
        self.assertIn("record:observed_defect_tool_error", prompt)
        self.assertIn("hallucinated_after_tool_failure", prompt)
        self.assertIn("record:upstream_0", prompt)
        self.assertIn("tool.result", prompt)
        self.assertIn("prompt_compaction", prompt)

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
