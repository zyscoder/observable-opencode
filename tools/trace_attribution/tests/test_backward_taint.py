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
from trace_attribution import claude as claude_module
from trace_attribution.claude import (
    ClaudeJudgeClient,
    build_judgment_prompt,
    call_with_wall_timeout,
    resolve_thinking_config,
    run_worker_with_timeout,
    validate_judgment_payload,
)
from trace_attribution.cli import lineage_output_path, parse_args
from trace_attribution.graph import TraceGraph
from trace_attribution.models import NodeJudgment, TaintInfluence, judgment_from_dict, stable_json
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
    def test_reconstructs_same_message_reasoning_before_action_decision(self):
        trace = {
            "case_id": "same-message-lineage-case",
            "records": [
                {
                    "record_id": "reasoning_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-13T10:00:00.000Z",
                    "data": {
                        "decision_id": "dec_reasoning",
                        "decision_type": "reasoning_block",
                        "rationale": "The broad failures are outside the requested scope.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-13T10:00:01.000Z",
                    "data": {
                        "decision_id": "dec_action",
                        "decision_type": "llm_tool_call",
                        "chosen_action": "bash",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1", "callID": "call_1"},
                    },
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "timestamp": "2026-07-13T10:00:02.000Z",
                    "data": {"call_id": "call_1", "tool_name": "bash", "session_id": "ses_1"},
                },
                {
                    "record_id": "tool_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "timestamp": "2026-07-13T10:00:03.000Z",
                    "source_refs": ["tool_call:call_1"],
                    "data": {"call_id": "call_1", "output": "smoke check passed"},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "decision", "id": "dec_action"},
                    "to": {"type": "tool_call", "id": "call_1"},
                    "relation": "selected_by",
                }
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertIn("record:reasoning_decision", graph.upstream_refs("record:action_decision"))
        self.assertIn("record:action_decision", graph.upstream_refs("record:tool_call"))
        self.assertIn("record:tool_call", graph.upstream_refs("record:tool_result"))
        self.assertEqual(graph.message_lineage["turns"][0]["message_id"], "msg_1")

    def test_reconstructs_decision_retained_in_later_llm_request_artifact(self):
        rationale = (
            "The three broad test failures are edge cases outside the requested interfaces, "
            "so a narrow smoke verification is sufficient."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = root / "artifacts" / "sha256" / "request.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                json.dumps([{"role": "assistant", "content": rationale}, {"role": "user", "content": "continue"}]),
                encoding="utf-8",
            )
            trace = {
                "case_id": "retained-decision-case",
                "artifacts": [
                    {
                        "artifact_id": "artifact_request",
                        "path": "artifacts/sha256/request.json",
                        "kind": "json",
                        "hash": "request-hash",
                    }
                ],
                "records": [
                    {
                        "record_id": "scope_decision",
                        "component": "processor",
                        "event_type": "decision",
                        "timestamp": "2026-07-13T10:00:00.000Z",
                        "data": {
                            "decision_id": "dec_scope",
                            "decision_type": "reasoning_block",
                            "rationale": rationale,
                            "metadata": {"sessionID": "ses_1", "messageID": "msg_old"},
                        },
                    },
                    {
                        "record_id": "final_llm",
                        "component": "llm",
                        "event_type": "llm.call",
                        "timestamp": "2026-07-13T10:00:05.000Z",
                        "data": {
                            "input": {"sessionID": "ses_1"},
                            "input_messages": {
                                "artifact_id": "artifact_request",
                                "hash": "request-hash",
                            },
                            "generated_response_refs": ["response_segment:final"],
                        },
                    },
                    {
                        "record_id": "final_output",
                        "component": "result",
                        "event_type": "response.output",
                        "timestamp": "2026-07-13T10:00:06.000Z",
                        "data": {"segment_id": "final", "is_final_for_case": True, "text": "All work is verified."},
                    },
                ],
            }

            graph = TraceGraph.from_trace(trace, artifact_root=root)

        self.assertIn("record:scope_decision", graph.upstream_refs("record:final_llm"))
        retained = [
            edge
            for edge in graph.message_lineage["edges"]
            if edge["relation"] == "retained_in_context" and edge["to_ref"] == "record:final_llm"
        ]
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0]["evidence_type"], "content_matched")
        self.assertTrue(retained[0]["eligible_for_attribution"])

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

    def test_resolves_semantic_decision_id_in_tool_call_dataflow_edge(self):
        trace = {
            "case_id": "decision-tool-case",
            "records": [
                {
                    "record_id": "decisionnode_dec_1",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"decision_id": "dec_1", "chosen_action": "bash"},
                },
                {
                    "record_id": "toolcall_call_1",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"call_id": "call_1", "tool_name": "bash"},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "decision", "id": "dec_1"},
                    "to": {"type": "tool_call", "id": "call_1"},
                    "relation": "selected_by",
                }
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.upstream_refs("record:toolcall_call_1"), ["record:decisionnode_dec_1"])

    def test_response_claim_upstream_nodes_prioritize_parent_output(self):
        records = [
            {"record_id": f"fact_{index}", "component": "tool", "event_type": "evidence.semantic_fact"}
            for index in range(15)
        ]
        records.extend(
            [
                {
                    "record_id": "full_output",
                    "component": "result",
                    "event_type": "response.output",
                    "data": {"text": "Complete answer with the necessary qualification."},
                },
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "source_refs": [f"record:fact_{index}" for index in range(15)],
                    "data": {"text": "Short claim."},
                },
            ]
        )
        graph = TraceGraph.from_trace(
            {
                "case_id": "parent-output-case",
                "records": records,
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": "full_output"},
                        "to": {"type": "record", "id": "claim"},
                    }
                ],
            }
        )

        upstream = graph.upstream_nodes("record:claim", limit=12)

        self.assertEqual(upstream[0].ref, "record:full_output")
        self.assertEqual(len(upstream), 12)

    def test_loads_trace_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_file = Path(tmp) / "trace.json"
            trace_file.write_text(json.dumps(sample_trace()), encoding="utf-8")

            graph = TraceGraph.from_file(trace_file)

        self.assertEqual(graph.case_id, "unit-case")
        self.assertEqual(graph.nodes["record:evidence_old"].event_type, "evidence.semantic_fact")

    def test_hydrates_artifact_backed_semantics_from_trace_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = root / "artifacts" / "sha256" / "tool-output.txt"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(
                "The decisive architecture boundary is billing-core, which owns renewalQuote end to end.",
                encoding="utf-8",
            )
            trace = {
                "case_id": "artifact-case",
                "artifacts": [
                    {
                        "artifact_id": "artifact_tool_output",
                        "path": "artifacts/sha256/tool-output.txt",
                        "kind": "text",
                        "hash": "unit-hash",
                    }
                ],
                "records": [
                    {
                        "record_id": "tool_result",
                        "component": "tool",
                        "event_type": "tool.result",
                        "artifact_refs": ["artifact_tool_output"],
                        "data": {
                            "output": {
                                "type": "text",
                                "preview": "The decisive architecture boundary is bill...",
                                "artifact_id": "artifact_tool_output",
                            }
                        },
                    }
                ],
            }
            trace_file = root / "trace.json"
            trace_file.write_text(json.dumps(trace), encoding="utf-8")

            graph = TraceGraph.from_file(trace_file)
            node = graph.nodes["record:tool_result"]

            self.assertNotIn("hydrated_artifacts", node.data)
            self.assertEqual(graph.artifact_hydration["loaded"], 0)
            node = graph.hydrate_node("record:tool_result")

        hydrated = node.data["hydrated_artifacts"]
        self.assertEqual(hydrated[0]["artifact_id"], "artifact_tool_output")
        self.assertIn("billing-core", hydrated[0]["content"])
        self.assertEqual(graph.artifact_hydration["loaded"], 1)

    def test_response_claim_upstream_prioritizes_current_revision_direct_support(self):
        trace = {
            "case_id": "revision-order-case",
            "records": [
                {
                    "record_id": "baseline",
                    "component": "tool",
                    "event_type": "verification",
                    "data": {
                        "verification_id": "baseline",
                        "repository_revision": 0,
                        "effective_for_final_state": False,
                    },
                },
                {
                    "record_id": "change",
                    "component": "tool",
                    "event_type": "change",
                    "data": {"change_id": "change", "revision_after": 1},
                },
                {
                    "record_id": "post_change",
                    "component": "tool",
                    "event_type": "verification",
                    "data": {
                        "verification_id": "post_change",
                        "repository_revision": 1,
                        "effective_for_final_state": True,
                    },
                },
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "source_refs": [
                        "verification:baseline",
                        "change:change",
                        "verification:post_change",
                    ],
                    "data": {
                        "claim_kind": "verification",
                        "repository_revision": 1,
                        "direct_support_refs": ["verification:post_change"],
                        "superseded_evidence_refs": ["verification:baseline"],
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)
        refs = [node.ref for node in graph.upstream_nodes("record:claim")]

        self.assertEqual(refs[0], "record:post_change")
        self.assertLess(refs.index("record:change"), refs.index("record:baseline"))
        self.assertEqual(refs[-1], "record:baseline")

    def test_completed_trace_prefers_atomic_claims_over_full_response_output(self):
        trace = {
            "case_id": "claim-first-case",
            "records": [
                {
                    "record_id": "final_output",
                    "component": "result",
                    "event_type": "response.output",
                    "data": {"text": "Full answer", "is_final_for_case": True},
                },
                {
                    "record_id": "claim_verification",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "All tests passed.", "claim_kind": "verification"},
                },
                {
                    "record_id": "claim_architecture",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "text": "billing-core owns renewalQuote.",
                        "claim_kind": "architecture",
                        "quality_flags": ["weak_evidence_match"],
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(
            graph.default_start_refs(),
            ["record:claim_architecture", "record:claim_verification"],
        )

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
        self.assertNotIn("ground_truth_component", missing.data)
        self.assertNotIn("ground_truth_failure_type", missing.data)
        self.assertEqual(
            graph.default_start_refs(),
            ["record:missing_semantic_final_test_result", "record:missing_semantic_change_diff_semantics"],
        )

    def test_review_ground_truth_does_not_become_observed_defect(self):
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

        self.assertNotIn("record:observed_defect_evidence_selection_stale_evidence_trusted", graph.nodes)
        self.assertEqual(graph.default_start_refs(), ["record:claim_bad"])

    def test_explicit_review_observed_defect_caps_broad_evidence_refs(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "observed_defects": [
                {
                    "component": "tool_error_handling",
                    "failure_type": "hallucinated_after_tool_failure",
                    "description": "The final answer relied on a failed tool call.",
                    "record_refs": ["record:evidence_old"]
                    + [f"record:broad_{index}" for index in range(80)],
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

    def test_trace_node_compact_preserves_semantic_text_before_large_refs(self):
        node = TraceNode(
            ref="record:claim",
            record_id="claim",
            component="result",
            event_type="response.claim",
            source_refs=[f"record:source_{index}" for index in range(40)],
            data={
                "text": "I did not guess the missing requirement value.",
                "direct_evidence_refs": [f"evidence:fact_{index}" for index in range(50)],
                "context_refs": [f"context:item_{index}" for index in range(80)],
                "attribution_summary": {"support_level": "contextual", "match_score": 0},
            },
        )

        compact = node.compact(max_chars=700)

        self.assertLessEqual(len(stable_json(compact)), 700)
        self.assertEqual(compact["data"]["text"], "I did not guess the missing requirement value.")
        self.assertEqual(compact["data"]["attribution_summary"]["support_level"], "contextual")

    def test_completed_trace_prefers_flagged_atomic_claim_over_final_response_output(self):
        trace = sample_trace()
        trace["records"].extend(
            [
                {
                    "record_id": "final_output",
                    "component": "result",
                    "event_type": "response.output",
                    "data": {"text": "Complete supported answer."},
                }
            ]
        )

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.default_start_refs(), ["record:claim_bad"])

    def test_completed_trace_does_not_use_explicitly_nonfinal_response_output(self):
        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "progress_output",
                "component": "result",
                "event_type": "response.output",
                "data": {"text": "Still investigating.", "is_final_for_case": False},
            }
        )

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.default_start_refs(), ["record:claim_bad"])


class BackwardTaintAnalyzerTest(unittest.TestCase):
    def test_judges_shared_node_independently_for_each_observed_defect(self):
        class BranchJudge(FakeJudge):
            def __init__(self):
                super().__init__({})
                self.contexts = []

            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                self.contexts.append(list(downstream_context))
                active_defect = "parser_contract" if "parser_contract" in downstream_context[0] else "scope_coverage"
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=True,
                    defect_status="present",
                    defect_type=active_defect,
                    defect_reason=f"The shared decision introduces {active_defect} on this branch.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                    confidence=0.9,
                )

        trace = {
            "case_id": "branch-isolation-case",
            "records": [
                {
                    "record_id": "shared_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"rationale": "Implement and verify the requested change."},
                },
                {
                    "record_id": "parser_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:shared_decision"],
                    "data": {"failure_type": "parser_contract"},
                },
                {
                    "record_id": "scope_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:shared_decision"],
                    "data": {"failure_type": "scope_coverage"},
                },
            ],
        }
        judge = BranchJudge()

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.calls, ["record:shared_decision", "record:shared_decision"])
        self.assertEqual(len(report.defect_branches), 2)
        branch_by_start = {branch.start_ref: branch for branch in report.defect_branches}
        self.assertEqual(
            branch_by_start["record:parser_defect"].node_judgments["record:shared_decision"].defect_type,
            "parser_contract",
        )
        self.assertEqual(
            branch_by_start["record:scope_defect"].node_judgments["record:shared_decision"].defect_type,
            "scope_coverage",
        )
        self.assertEqual(
            [branch.analysis_outcome for branch in report.defect_branches],
            ["root_found", "root_found"],
        )

    def test_reports_partial_root_found_when_any_defect_branch_is_unresolved(self):
        trace = {
            "case_id": "partial-coverage-case",
            "records": [
                {"record_id": "root_a", "component": "processor", "event_type": "decision"},
                {"record_id": "evidence_b", "component": "result", "event_type": "response.claim"},
                {
                    "record_id": "observed_a",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:root_a"],
                    "data": {"failure_type": "defect_a"},
                },
                {
                    "record_id": "observed_b",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:evidence_b"],
                    "data": {"failure_type": "defect_b"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:root_a": NodeJudgment(
                    node_ref="record:root_a",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="defect_a",
                    defect_reason="This decision introduces defect A.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                ),
                "record:evidence_b": NodeJudgment(
                    node_ref="record:evidence_b",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_status="present",
                    defect_type="defect_b",
                    defect_reason="The defect is visible, but its source is absent from the trace.",
                    causal_role="defect_propagation",
                    branch_relation="same_defect",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:missing_source",
                            reason="The missing source generated this claim.",
                        )
                    ],
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(report.metadata["analysis_outcome"], "partial_root_found")
        self.assertEqual(
            {branch.start_ref: branch.analysis_outcome for branch in report.defect_branches},
            {"record:observed_a": "root_found", "record:observed_b": "inconclusive"},
        )
        self.assertIn(
            "unresolved_defect_branch",
            [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]],
        )
        self.assertEqual(report.trace_improvement_report["summary"]["analysis_confidence"], "partial")

    def test_collapses_duplicate_roots_within_the_same_causal_episode(self):
        trace = {
            "case_id": "episode-root-case",
            "records": [
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_1",
                        },
                    },
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"call_id": "call_1"},
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:reasoning", "record:action", "record:tool_call"],
                    "data": {"failure_type": "wrong_parser_contract"},
                },
            ],
        }
        roots = {
            ref: NodeJudgment(
                node_ref=ref,
                component="processor" if ref != "record:tool_call" else "tool",
                event_type="decision" if ref != "record:tool_call" else "tool.call",
                has_defect=True,
                defect_status="present",
                defect_type="wrong_parser_contract",
                defect_reason="This member materializes the same defective action.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
            )
            for ref in ("record:reasoning", "record:action", "record:tool_call")
        }

        report = BackwardTaintAnalyzer(judge=FakeJudge(roots)).analyze(TraceGraph.from_trace(trace))

        self.assertEqual([root.node_ref for root in report.root_causes], ["record:reasoning"])
        self.assertEqual(
            set(report.root_causes[0].episode_member_refs),
            {"record:reasoning", "record:action", "record:tool_call"},
        )
        self.assertEqual(report.root_causes[0].observed_defect_refs, ["record:observed"])

    def test_motivating_evidence_does_not_become_a_defect_predecessor(self):
        trace = {
            "case_id": "motivation-is-not-propagation-case",
            "records": [
                {"record_id": "environment", "component": "tool", "event_type": "tool.result"},
                {
                    "record_id": "scope_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:environment"],
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:scope_decision"],
                    "data": {"failure_type": "scope_coverage"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:scope_decision": NodeJudgment(
                    node_ref="record:scope_decision",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="scope_coverage",
                    defect_reason="The decision incorrectly narrows the required scope.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:environment",
                            reason="The environment result motivated the decision.",
                            relation="motivated_by_evidence",
                        )
                    ],
                    is_root_cause=True,
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.calls, ["record:scope_decision"])
        self.assertEqual([root.node_ref for root in report.root_causes], ["record:scope_decision"])
        self.assertNotIn("record:environment", report.visited_order)

    def test_defers_llm_surface_root_until_reconstructed_decisions_are_judged(self):
        trace = {
            "case_id": "deferred-surface-root-case",
            "records": [
                {
                    "record_id": "scope_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_id": "dec_scope",
                        "decision_type": "reasoning_block",
                        "rationale": "Known failures are outside scope, so narrow verification is sufficient.",
                    },
                },
                {
                    "record_id": "final_llm",
                    "component": "llm",
                    "event_type": "llm.call",
                    "data": {"output_text": "All requested behavior is implemented and verified."},
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:final_llm"],
                    "data": {"failure_type": "premature_completion"},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "decision", "id": "dec_scope"},
                    "to": {"type": "record", "id": "final_llm"},
                    "relation": "retained_in_context",
                }
            ],
        }
        judge = FakeJudge(
            {
                "record:final_llm": NodeJudgment(
                    node_ref="record:final_llm",
                    component="llm",
                    event_type="llm.call",
                    has_defect=True,
                    defect_status="present",
                    defect_type="premature_completion",
                    defect_reason="The model emitted an overclaim.",
                    causal_role="defect_introduction",
                    influenced_by=[],
                    is_root_cause=True,
                ),
                "record:scope_decision": NodeJudgment(
                    node_ref="record:scope_decision",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="incorrect_scope_assessment",
                    defect_reason="The decision incorrectly dismissed relevant failures.",
                    causal_role="defect_introduction",
                    influenced_by=[],
                    is_root_cause=True,
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertIn("record:scope_decision", report.visited_order)
        self.assertEqual([item.node_ref for item in report.root_causes], ["record:scope_decision"])
        self.assertIn(
            ["record:observed", "record:final_llm", "record:scope_decision"],
            report.taint_paths,
        )

    def test_trace_health_missing_verification_observation_is_a_gap_not_a_behavioral_defect(self):
        trace = {
            "case_id": "trace-health-gap-case",
            "records": [
                {"record_id": "change_1", "component": "tool", "event_type": "change"},
                {
                    "record_id": "trace_gap",
                    "component": "trace",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change_1"],
                    "data": {
                        "issue_kind": "missing_verification_after_change",
                        "failure_type": "final_test_result_missing",
                    },
                },
            ],
        }
        judge = FakeJudge({})

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.calls, [])
        self.assertNotIn("record:change_1", report.visited_order)
        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:trace_gap"].causal_role, "unknown")

    def test_report_records_message_lineage_summary(self):
        report = BackwardTaintAnalyzer(judge=FakeJudge({})).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=["record:claim_bad"],
        )

        self.assertIn("message_lineage", report.metadata)
        self.assertEqual(report.metadata["message_lineage"]["behavior_impact"], "none")
        self.assertGreaterEqual(report.metadata["message_lineage"]["stats"]["snapshot_count"], 0)

    def test_present_nonroot_dead_end_is_not_promoted_to_root(self):
        trace = {
            "case_id": "truthful-evidence-case",
            "records": [
                {
                    "record_id": "pytest_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {"output": "2 failed, 10 passed"},
                }
            ],
        }
        judge = FakeJudge(
            {
                "record:pytest_result": NodeJudgment(
                    node_ref="record:pytest_result",
                    component="tool",
                    event_type="tool.result",
                    has_defect=True,
                    defect_status="present",
                    defect_type="test_failure_evidence",
                    defect_reason="The tool faithfully reports failures but did not introduce them.",
                    influenced_by=[],
                    is_root_cause=False,
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:pytest_result"],
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_missing_semantic_does_not_taint_cited_code_changes(self):
        trace = {
            "case_id": "missing-semantic-isolation-case",
            "records": [
                {
                    "record_id": "change_1",
                    "component": "tool",
                    "event_type": "change",
                    "data": {"files": ["src/feature.py"]},
                },
                {
                    "record_id": "missing_verification",
                    "component": "evaluation",
                    "event_type": "case.missing_semantic",
                    "source_refs": ["record:change_1"],
                    "data": {"gap_kind": "final_test_result"},
                },
            ],
        }
        judge = FakeJudge({})

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.calls, [])
        self.assertNotIn("record:change_1", report.visited_order)
        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_judge_receives_semantic_context_for_the_active_defect_branch(self):
        class ContextJudge(FakeJudge):
            def __init__(self):
                super().__init__({})
                self.contexts = []

            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                self.contexts.append(downstream_context)
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="absent",
                    defect_reason="No defect at this evidence node.",
                )

        trace = {
            "case_id": "branch-context-case",
            "records": [
                {"record_id": "claim", "component": "result", "event_type": "response.claim"},
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:claim"],
                    "data": {
                        "failure_type": "false_completion_claim",
                        "description": "The final answer says all tests pass while verification failed.",
                    },
                },
            ],
        }
        judge = ContextJudge()

        BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.calls, ["record:claim"])
        self.assertIn("false_completion_claim", judge.contexts[0][0])
        self.assertIn("all tests pass", judge.contexts[0][0])

    def test_external_observed_defect_propagates_without_judging_the_evaluation_boundary(self):
        trace = {
            "case_id": "external-defect-case",
            "records": [
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "All requested behavior is complete."},
                },
                {
                    "record_id": "failed_verification",
                    "component": "tool",
                    "event_type": "verification",
                    "status": "failed",
                    "data": {"verification_id": "failed", "final_test_result": {"status": "failed"}},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:claim", "record:failed_verification"],
                    "data": {"description": "External benchmark tests confirmed partial correctness."},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:claim": NodeJudgment(
                    node_ref="record:claim",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_status="present",
                    defect_type="false_completion",
                    defect_reason="The completion claim conflicts with benchmark verification.",
                    influenced_by=[],
                    is_root_cause=True,
                    confidence=0.9,
                ),
                "record:failed_verification": NodeJudgment(
                    node_ref="record:failed_verification",
                    component="tool",
                    event_type="verification",
                    has_defect=False,
                    defect_status="absent",
                    defect_reason="This node reports the failure without introducing it.",
                    influenced_by=[],
                    is_root_cause=False,
                    confidence=0.9,
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertNotIn("record:observed_defect", judge.calls)
        self.assertEqual(judge.calls, ["record:claim", "record:failed_verification"])
        self.assertEqual(report.visited_order[0], "record:observed_defect")
        self.assertEqual([item.node_ref for item in report.root_causes], ["record:claim"])

    def test_trace_without_analysis_start_is_inconclusive(self):
        report = BackwardTaintAnalyzer(judge=FakeJudge({}), max_depth=8).analyze(
            TraceGraph.from_trace({"case_id": "empty-case", "records": []})
        )

        self.assertEqual(report.start_refs, [])
        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        self.assertIn(
            "no_analysis_start",
            [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]],
        )

    def test_unknown_judgment_always_produces_a_blocking_gap(self):
        graph = TraceGraph.from_trace(sample_trace())
        judge = FakeJudge(
            {
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=False,
                    defect_status="unknown",
                    defect_reason="The complete tool output was not available to the judge.",
                    model_notes="Need the artifact-backed tool result.",
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=judge, max_depth=8).analyze(
            graph,
            start_refs=["record:claim_bad"],
        )

        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        gap_types = [item["gap_type"] for item in report.trace_improvement_report["blocking_gaps"]]
        self.assertIn("unknown_node_judgment", gap_types)

    def test_report_records_effective_judge_timeout(self):
        judge = FakeJudge({})
        judge.timeout_seconds = 60 * 60

        report = BackwardTaintAnalyzer(judge=judge, max_depth=1).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=["record:claim_bad"],
        )

        self.assertEqual(report.metadata["judge_timeout_seconds"], 60 * 60)

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
        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        self.assertEqual(report.metadata["termination_reason"], "queue_exhausted")

    def test_nondefective_start_reports_no_defect(self):
        report = BackwardTaintAnalyzer(judge=FakeJudge({}), max_depth=8).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=["record:claim_bad"],
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:claim_bad"].defect_status, "absent")
        self.assertEqual(report.metadata["analysis_outcome"], "no_defect")
        self.assertEqual(report.metadata["termination_reason"], "queue_exhausted")
        self.assertEqual(report.trace_improvement_report["summary"]["analysis_confidence"], "no_defect")

    def test_depth_limit_is_inconclusive_instead_of_fabricating_root(self):
        fake = FakeJudge(
            {
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_type="wrong_final_claim",
                    defect_reason="The final claim is wrong.",
                    influenced_by=[TaintInfluence(upstream_ref="record:change_bad", reason="Bad change.")],
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=fake, max_depth=0).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=["record:claim_bad"],
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        self.assertEqual(report.metadata["termination_reason"], "depth_limit")

    def test_node_limit_is_inconclusive_instead_of_fabricating_root(self):
        fake = FakeJudge(
            {
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=True,
                    defect_type="wrong_final_claim",
                    defect_reason="The final claim is wrong.",
                    influenced_by=[TaintInfluence(upstream_ref="record:change_bad", reason="Bad change.")],
                ),
                "record:change_bad": NodeJudgment(
                    node_ref="record:change_bad",
                    component="tool",
                    event_type="change",
                    has_defect=True,
                    defect_type="wrong_change",
                    defect_reason="The change is wrong.",
                    influenced_by=[TaintInfluence(upstream_ref="record:evidence_old", reason="Stale evidence.")],
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=fake, max_depth=8, max_nodes=1).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=["record:claim_bad"],
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        self.assertEqual(report.metadata["termination_reason"], "node_limit")

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

    def test_preserves_external_evaluation_boundary_when_upstream_is_nondefective(self):
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

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.taint_paths, [])
        self.assertEqual(report.node_judgments["record:quality_gap"].defect_status, "present")
        self.assertNotIn("record:quality_gap", fake.calls)
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_does_not_reject_evaluation_assertion_after_only_partial_upstream_review(self):
        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "quality_gap",
                "component": "evaluation",
                "event_type": "case.quality_gap",
                "source_refs": ["record:claim_bad", "record:change_bad"],
                "data": {"dimension": "solution_design"},
            }
        )
        fake = FakeJudge(
            {
                "record:quality_gap": NodeJudgment(
                    node_ref="record:quality_gap",
                    component="evaluation",
                    event_type="case.quality_gap",
                    has_defect=True,
                    defect_reason="The answer may omit design evidence.",
                    influenced_by=[
                        TaintInfluence(upstream_ref="record:claim_bad", reason="Candidate answer defect."),
                        TaintInfluence(upstream_ref="record:change_bad", reason="Candidate implementation defect."),
                    ],
                ),
                "record:claim_bad": NodeJudgment(
                    node_ref="record:claim_bad",
                    component="result",
                    event_type="response.claim",
                    has_defect=False,
                    defect_reason="The claim is supported.",
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=fake, max_depth=4, max_nodes=2).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:quality_gap"],
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:quality_gap"].defect_status, "present")
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        self.assertEqual(report.metadata["termination_reason"], "node_limit")

    def test_judge_errors_become_unknown_without_roots(self):
        class ErrorJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                raise TimeoutError("judge timed out")

        graph = TraceGraph.from_trace(sample_trace())

        report = BackwardTaintAnalyzer(judge=ErrorJudge({}), max_depth=4).analyze(
            graph,
            start_refs=["record:change_bad"],
            objective="Explain the final claim.",
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:change_bad"].defect_status, "unknown")
        self.assertEqual(report.metadata["judge_error_count"], 1)
        self.assertIn("record:change_bad", report.metadata["judge_errors"][0]["node_ref"])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_observed_defect_boundary_stays_present_when_upstream_judges_fail(self):
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
        self.assertEqual(observed.defect_status, "present")
        self.assertEqual(observed.defect_type, "hallucinated_after_tool_failure")
        self.assertEqual(report.root_causes, [])
        self.assertEqual(
            report.visited_order,
            ["record:observed_defect_tool_failure", "record:claim_bad", "record:evidence_old"],
        )
        self.assertNotIn("record:observed_defect_tool_failure", report.metadata["judge_errors"][0]["node_ref"])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_tool_error_judge_error_uses_unknown_semantic_fallback(self):
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

        judgment = report.node_judgments["record:tool_error"]

        self.assertEqual(report.root_causes, [])
        self.assertEqual(judgment.defect_status, "unknown")
        self.assertEqual(judgment.defect_type, "judge_error")
        self.assertIn("read", judgment.defect_reason)
        self.assertIn("File not found", judgment.defect_reason)
        self.assertEqual(judgment.model_notes, "TimeoutError: judge timed out")

    def test_common_semantic_nodes_use_readable_unknown_fallbacks(self):
        class ErrorJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                raise TimeoutError("judge timed out")

        cases = [
            (
                {
                    "record_id": "compaction",
                    "component": "context",
                    "event_type": "context.compaction",
                    "data": {
                        "trigger": "manual",
                        "model_id": "deepseek-v4-pro",
                        "selected_tail_messages": 5,
                        "output_summary": "Constraint: do not modify src/payment.",
                    },
                },
                ["manual", "deepseek-v4-pro", "do not modify src/payment"],
            ),
            (
                {
                    "record_id": "fact",
                    "component": "tool",
                    "event_type": "evidence.semantic_fact",
                    "data": {
                        "canonical_subject": "renewalQuote",
                        "semantic_role": "observed_pre_change_code",
                        "structured_claim": {
                            "subject": "renewalQuote",
                            "predicate": "discount_cap",
                            "value": "20 percent",
                        },
                        "conflict_group_id": "fact_conflict_1",
                        "applicability_status": "unknown",
                    },
                },
                ["renewalQuote", "discount_cap", "fact_conflict_1"],
            ),
            (
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "text": "Changed src/billing/pricing.mjs and npm test passed.",
                        "direct_evidence_refs": ["evidence:fact_1", "verification:ver_1"],
                        "quality_flags": ["missing_tradeoff"],
                    },
                },
                ["Changed src/billing/pricing.mjs", "direct_evidence_refs=2", "missing_tradeoff"],
            ),
            (
                {
                    "record_id": "obligation",
                    "component": "processor",
                    "event_type": "task.obligation",
                    "data": {
                        "obligation_type": "path_scope_exclusion",
                        "requirement_text": "Do not modify src/payment.",
                        "target_path": "src/payment",
                        "status": "fulfilled",
                    },
                },
                ["path_scope_exclusion", "Do not modify src/payment", "status=fulfilled"],
            ),
        ]

        for record, expected_fragments in cases:
            with self.subTest(record_id=record["record_id"]):
                graph = TraceGraph.from_trace({"case_id": "fallback-case", "records": [record]})

                report = BackwardTaintAnalyzer(judge=ErrorJudge({}), max_depth=2).analyze(
                    graph,
                    start_refs=[f"record:{record['record_id']}"],
                    objective="Explain the observed semantic defect.",
                )
                judgment = report.node_judgments[f"record:{record['record_id']}"]

                self.assertEqual(report.root_causes, [])
                self.assertEqual(judgment.defect_status, "unknown")
                self.assertEqual(judgment.defect_type, "judge_error")
                for fragment in expected_fragments:
                    self.assertIn(fragment, judgment.defect_reason)


class NodeJudgmentTest(unittest.TestCase):
    def test_missing_defect_boolean_is_unknown(self):
        node = TraceNode(ref="record:test", record_id="test", component="result", event_type="response.claim")

        judgment = judgment_from_dict({}, node)

        self.assertEqual(judgment.defect_status, "unknown")
        self.assertFalse(judgment.has_defect)

    def test_legacy_boolean_maps_to_tri_state(self):
        node = TraceNode(ref="record:test", record_id="test", component="result", event_type="response.claim")

        present = judgment_from_dict({"has_defect": True}, node)
        absent = judgment_from_dict({"has_defect": False}, node)

        self.assertEqual(present.defect_status, "present")
        self.assertTrue(present.has_defect)
        self.assertEqual(absent.defect_status, "absent")
        self.assertFalse(absent.has_defect)


class ClaudeJudgeClientTest(unittest.TestCase):
    def test_cli_derives_lineage_output_next_to_attribution_report(self):
        self.assertEqual(
            lineage_output_path(Path("/tmp/result.json"), ""),
            Path("/tmp/result.message-lineage.json"),
        )
        self.assertEqual(
            lineage_output_path(Path("/tmp/result.json"), "/tmp/custom-lineage.json"),
            Path("/tmp/custom-lineage.json"),
        )

    def test_parses_defect_evidence_causal_role(self):
        node = TraceNode(ref="record:test", record_id="test", component="tool", event_type="tool.result")

        judgment = judgment_from_dict(
            {
                "node_ref": node.ref,
                "component": node.component,
                "event_type": node.event_type,
                "defect_status": "present",
                "has_defect": True,
                "defect_type": "test_failure_evidence",
                "defect_reason": "The result truthfully exposes a failure.",
                "causal_role": "defect_evidence",
                "influenced_by": [],
                "is_root_cause": False,
                "confidence": 0.9,
            },
            node,
        )

        self.assertEqual(getattr(judgment, "causal_role", None), "defect_evidence")

    def test_rejects_evidence_role_marked_as_root_cause(self):
        with self.assertRaisesRegex(ValueError, "defect_evidence"):
            validate_judgment_payload(
                {
                    "node_ref": "record:test",
                    "component": "tool",
                    "event_type": "tool.result",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "test_failure_evidence",
                    "defect_reason": "The result truthfully exposes a failure.",
                    "causal_role": "defect_evidence",
                    "influenced_by": [],
                    "is_root_cause": True,
                    "confidence": 0.9,
                }
            )

    def test_rejects_propagation_without_defective_upstream(self):
        with self.assertRaisesRegex(ValueError, "defect_propagated_from"):
            validate_judgment_payload(
                {
                    "node_ref": "record:scope_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "out_of_scope_change",
                    "defect_reason": "The decision applies an unrelated compatibility change.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.9,
                }
            )

    def test_rejects_propagation_motivated_only_by_evidence(self):
        with self.assertRaisesRegex(ValueError, "defect_propagated_from"):
            validate_judgment_payload(
                {
                    "node_ref": "record:scope_decision",
                    "component": "processor",
                    "event_type": "decision",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "out_of_scope_change",
                    "defect_reason": "A truthful environment failure motivated an unrelated compatibility change.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [
                        {
                            "upstream_ref": "record:environment_failure",
                            "reason": "The environment failure motivated the decision.",
                            "relation": "motivated_by_evidence",
                            "confidence": 0.9,
                        }
                    ],
                    "is_root_cause": False,
                    "confidence": 0.9,
                }
            )

    def test_rejects_speculative_root_reason(self):
        with self.assertRaisesRegex(ValueError, "speculative"):
            validate_judgment_payload(
                {
                    "node_ref": "record:large_change",
                    "component": "tool",
                    "event_type": "change",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "possible_symbol_bug",
                    "defect_reason": "The complex branching logic could introduce a symbol resolution defect.",
                    "causal_role": "defect_introduction",
                    "branch_relation": "causal_precursor",
                    "influenced_by": [],
                    "is_root_cause": True,
                    "confidence": 0.9,
                }
            )

    def test_accepts_root_with_motivating_evidence_but_no_defect_predecessor(self):
        validate_judgment_payload(
            {
                "node_ref": "record:scope_decision",
                "component": "processor",
                "event_type": "decision",
                "defect_status": "present",
                "has_defect": True,
                "defect_type": "scope_coverage",
                "defect_reason": "The decision itself incorrectly narrows the required scope.",
                "causal_role": "defect_introduction",
                "branch_relation": "same_defect",
                "influenced_by": [
                    {
                        "upstream_ref": "record:environment_failure",
                        "reason": "The environment failure motivated the scope decision.",
                        "relation": "motivated_by_evidence",
                        "confidence": 0.9,
                    }
                ],
                "is_root_cause": True,
                "confidence": 0.9,
            }
        )

    def test_rejects_absent_judgment_with_nonempty_defect_type(self):
        with self.assertRaises(ValueError):
            validate_judgment_payload(
                {
                    "defect_status": "absent",
                    "has_defect": False,
                    "defect_type": "incorrect_claim",
                    "defect_reason": "The claim is a semantic defect but not the code-level root cause.",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.8,
                }
            )

    def test_rejects_invalid_explicit_defect_status(self):
        with self.assertRaises(ValueError):
            validate_judgment_payload(
                {
                    "defect_status": "maybe",
                    "has_defect": False,
                    "defect_reason": "Invalid status value.",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.5,
                }
            )

    def test_rejects_inconsistent_tri_state_and_legacy_boolean(self):
        with self.assertRaises(ValueError):
            validate_judgment_payload(
                {
                    "defect_status": "absent",
                    "has_defect": True,
                    "defect_reason": "Contradictory schema fields.",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.5,
                }
            )

    def test_defaults_each_judge_request_to_one_hour(self):
        calls = []

        class FakeAnthropic:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=True):
                client = ClaudeJudgeClient()

        self.assertEqual(claude_module.DEFAULT_JUDGE_TIMEOUT_SECONDS, 60 * 60)
        self.assertEqual(client.timeout_seconds, 60 * 60)
        self.assertEqual(calls, [{"api_key": "test-key", "timeout": 60 * 60}])

    def test_cli_defaults_each_judge_request_to_one_hour(self):
        args = parse_args(["--trace", "trace.json", "--out", "attribution.json"])

        self.assertEqual(args.judge_timeout_sec, 60 * 60)

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

    def test_disables_thinking_by_default_for_deepseek_anthropic_endpoint(self):
        self.assertEqual(
            resolve_thinking_config("https://api.deepseek.com/anthropic", "auto", max_tokens=4096),
            {"type": "disabled"},
        )
        self.assertIsNone(resolve_thinking_config("https://api.anthropic.com", "auto", max_tokens=4096))

    def test_cli_exposes_attribution_thinking_mode(self):
        args = parse_args(["--trace", "trace.json", "--out", "attribution.json"])
        explicit = parse_args(
            ["--trace", "trace.json", "--out", "attribution.json", "--thinking-mode", "enabled"]
        )

        self.assertEqual(args.thinking_mode, "auto")
        self.assertEqual(explicit.thinking_mode, "enabled")

    def test_quality_gap_prompt_treats_gap_as_assertion_to_validate(self):
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
        self.assertIn("evaluation assertion to validate", prompt)
        self.assertIn("may be rejected", prompt)
        self.assertNotIn("treat the quality gap as the defect to explain", prompt)

    def test_judgment_prompt_defines_branch_and_influence_semantics(self):
        prompt = build_judgment_prompt(
            node=TraceNode(
                ref="record:scope_decision",
                record_id="scope_decision",
                component="processor",
                event_type="decision",
            ),
            upstream_nodes=[
                TraceNode(
                    ref="record:environment_failure",
                    record_id="environment_failure",
                    component="tool",
                    event_type="tool.result",
                )
            ],
            downstream_context=[
                "record:observed_scope event_type=case.observed_defect; failure_type=scope_coverage"
            ],
            objective="Find the root cause of the scope coverage defect.",
        )
        payload = json.loads(prompt)

        self.assertIn("active_defect_branch", payload)
        self.assertEqual(
            payload["required_json_schema"]["branch_relation"],
            "same_defect|causal_precursor|outcome_evidence|unrelated|unknown",
        )
        self.assertEqual(
            payload["required_json_schema"]["influenced_by"][0]["relation"],
            "defect_propagated_from|motivated_by_evidence|derived_from",
        )
        rules = " ".join(payload["rules"])
        self.assertIn("may motivate a decision but does not propagate", rules)
        self.assertIn("Authored tool-call arguments or test scripts are action semantics", rules)
        self.assertIn("Code size or complexity alone", rules)

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

    def test_judgment_prompt_forbids_root_inference_from_truncated_artifacts(self):
        prompt = build_judgment_prompt(
            node=TraceNode(
                ref="record:change",
                record_id="change",
                component="tool",
                event_type="change",
                data={
                    "hydrated_artifacts": [
                        {
                            "artifact_id": "artifact_diff",
                            "content": "partial diff",
                            "content_length": 50000,
                            "truncated": True,
                        }
                    ]
                },
            ),
            upstream_nodes=[],
            downstream_context=["record:claim"],
            objective="Find the first defect introduction point.",
        )

        self.assertIn("must not infer a defect or root cause from the missing portion", prompt)

    def test_retries_full_judgment_after_malformed_repair(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    text = '{"defect_status": "present"'
                elif len(calls) == 2:
                    text = "still malformed"
                else:
                    text = json.dumps(
                        {
                            "node_ref": "record:claim",
                            "component": "result",
                            "event_type": "response.claim",
                            "defect_status": "present",
                            "has_defect": True,
                            "defect_type": "false_completion",
                            "defect_reason": "The claim contradicts the failed verification.",
                            "influenced_by": [],
                            "is_root_cause": True,
                            "severity": "high",
                            "confidence": 0.9,
                            "model_notes": "fresh retry succeeded",
                        }
                    )
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

        class FakeAnthropic:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                client = ClaudeJudgeClient(model="fake-model")
                client.timeout_seconds = None

        judgment = client.judge_node(
            node=TraceNode(
                ref="record:claim",
                record_id="claim",
                component="result",
                event_type="response.claim",
            ),
            upstream_nodes=[],
            downstream_context=["record:claim"],
            objective="Check the completion claim.",
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(judgment.defect_status, "present")
        self.assertEqual(judgment.model_notes, "fresh retry succeeded")

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
                client.timeout_seconds = None

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

    def test_repairs_incomplete_json_judgment_once(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"has_defect": false}')])
                return types.SimpleNamespace(
                    content=[
                        types.SimpleNamespace(
                            text=json.dumps(
                                {
                                    "node_ref": "record:claim",
                                    "component": "result",
                                    "event_type": "response.claim",
                                    "defect_status": "absent",
                                    "has_defect": False,
                                    "defect_type": "",
                                    "defect_reason": "The claim is supported by direct evidence.",
                                    "influenced_by": [],
                                    "is_root_cause": False,
                                    "severity": "unknown",
                                    "confidence": 0.8,
                                    "model_notes": "repaired incomplete output",
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
                client.timeout_seconds = None

        judgment = client.judge_node(
            node=TraceNode(
                ref="record:claim",
                record_id="claim",
                component="result",
                event_type="response.claim",
            ),
            upstream_nodes=[],
            downstream_context=["record:claim"],
            objective="Check whether the claim is defective.",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(judgment.defect_status, "absent")
        self.assertFalse(judgment.has_defect)
        self.assertEqual(judgment.model_notes, "repaired incomplete output")


if __name__ == "__main__":
    unittest.main()
