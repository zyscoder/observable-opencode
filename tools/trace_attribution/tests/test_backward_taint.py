import hashlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

from trace_attribution.analyzer import BackwardTaintAnalyzer, semantic_episode_predecessors
from trace_attribution import claude as claude_module
from trace_attribution.claude import (
    ClaudeJudgeClient,
    build_judgment_prompt,
    build_root_confirmation_prompt,
    call_with_wall_timeout,
    resolve_thinking_config,
    run_worker_with_timeout,
    validate_judgment_payload,
    validate_root_confirmation_payload,
)
from trace_attribution.cli import judge_cache_output_path, lineage_output_path, parse_args
from trace_attribution.episodes import CausalEpisodeIndex
from trace_attribution.errors import JudgeProviderUnavailable
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
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
    def test_obligation_gap_offline_edge_never_becomes_a_confirmed_fact(self):
        trace = {
            "case_id": "offline-obligation-gap-case",
            "manifest": {
                "case_id": "offline-obligation-gap-case",
                "run_id": "offline-obligation-gap-run",
                "subject_revision": "git:offline-gap",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": "offline-obligation-gap-case",
                    "run_id": "offline-obligation-gap-run",
                },
            },
            "records": [
                {
                    "record_id": "task_contract",
                    "component": "user",
                    "event_type": "message.input",
                    "timestamp": "2026-07-31T00:00:00Z",
                    "data": {
                        "case_id": "offline-obligation-gap-case",
                        "repository_revision": 1,
                        "subject_revision": "git:offline-gap",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                        "task_obligations": [
                            {
                                "obligation_id": "runtime-dependency",
                                "obligation_text": "Preserve the packaging dependency.",
                                "required_capabilities": ["packaging"],
                            }
                        ],
                    },
                },
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "timestamp": "2026-07-31T00:01:00Z",
                    "data": {
                        "case_id": "offline-obligation-gap-case",
                        "repository_revision": 1,
                        "subject_revision": "git:offline-gap",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                        "rationale": (
                            "Packaging is required but tests probably omit it, "
                            "so exclude packaging."
                        ),
                    },
                },
                {
                    "record_id": "failure",
                    "component": "tool",
                    "event_type": "verification",
                    "timestamp": "2026-07-31T00:02:00Z",
                    "status": "failure",
                    "data": {
                        "case_id": "offline-obligation-gap-case",
                        "repository_revision": 1,
                        "subject_revision": "git:offline-gap",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                        "exit_code": 1,
                        "output": "ImportError: packaging is required",
                        "failure_signature": {
                            "schema": "obligation-failure-signature/v1",
                            "signature_id": "offline-packaging-failure",
                            "subsystem": "runtime.packaging",
                            "status": "failed",
                        },
                        "failure_binding": {
                            "binding_type": "obligation",
                            "obligation_id": "runtime-dependency",
                        },
                    },
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        from trace_attribution.reconstruction import (
            reconstruct_obligation_gap_candidates,
        )

        candidate = reconstruct_obligation_gap_candidates(graph)[0]

        self.assertEqual(
            candidate.offline_path_provenance[0]["evidence_type"],
            "offline_reconstruction",
        )
        self.assertFalse(
            candidate.offline_path_provenance[0]["confirmed_fact"]
        )
        self.assertEqual(
            graph.edge_context("record:decision", "record:failure"),
            [],
        )
        self.assertEqual(
            graph.message_lineage["stats"]["confirmed_edge_count"],
            0,
        )

    def test_preserves_normalized_causal_edge_semantics(self):
        trace = {
            "case_id": "edge-context-case",
            "records": [
                {
                    "record_id": "evidence",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {"text": "The relevant test currently fails."},
                },
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"rationale": "Change the implementation because the test fails."},
                },
                {
                    "record_id": "source_only",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:evidence"],
                    "data": {"rationale": "Use the recorded evidence."},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "evidence"},
                    "to": {"type": "record", "id": "decision"},
                    "relation": "motivated_by_evidence",
                    "evidence_type": "confirmed",
                    "confidence": 0.97,
                    "eligible_for_attribution": True,
                    "inference_method": "recorded_dataflow",
                }
            ],
        }

        graph = TraceGraph.from_trace(trace)

        edge = graph.edge_context("record:evidence", "record:decision")[0]
        self.assertEqual(edge["relation"], "motivated_by_evidence")
        self.assertEqual(edge["evidence_type"], "confirmed")
        self.assertEqual(edge["confidence"], 0.97)
        self.assertEqual(edge["inference_method"], "recorded_dataflow")
        self.assertEqual(edge["edge_origin"], "trace.dataflow_edges")
        source_edge = graph.edge_context("record:evidence", "record:source_only")[0]
        self.assertEqual(source_edge["relation"], "record_source")
        self.assertEqual(source_edge["edge_origin"], "record.source_refs")

    def test_builds_structured_causal_judgment_context(self):
        try:
            from trace_attribution.judgment_context import build_causal_judgment_context
        except ModuleNotFoundError as exc:
            self.fail(f"judgment context module is missing: {exc}")
        trace = {
            "case_id": "judgment-context-case",
            "records": [
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Keep searching before making any change.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:01.000Z",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "grep",
                        "rationale": "Search one more location instead of implementing.",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_1",
                        },
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "timestamp": "2026-07-17T10:30:00.000Z",
                    "data": {
                        "failure_type": "deadline_reached_with_empty_patch",
                        "description": "The case ended with an empty patch.",
                        "expected": "Implement and verify the requested change.",
                        "actual": "No repository change was produced.",
                        "scope": "task_delivery",
                    },
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "reasoning"},
                    "to": {"type": "record", "id": "action"},
                    "relation": "reasoning_selected_action",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                    "inference_method": "same_message_call_identity",
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        episode_index = CausalEpisodeIndex.from_graph(graph)
        progress_ref = next(
            ref
            for ref, node in graph.nodes.items()
            if node.event_type == "progress.episode" and "record:action" in node.data["member_refs"]
        )
        observed_judgment = NodeJudgment(
            node_ref="record:observed",
            component="evaluation",
            event_type="case.observed_defect",
            has_defect=True,
            defect_status="present",
            defect_type="empty_patch",
            defect_reason="The requested implementation was not delivered.",
            causal_role="defect_evidence",
            branch_relation="same_defect",
            confidence=0.98,
        )

        context = build_causal_judgment_context(
            graph=graph,
            node_ref="record:action",
            path=["record:observed", progress_ref, "record:action"],
            judgments={"record:observed": observed_judgment},
            episode_index=episode_index,
            objective="Implement and verify the requested change.",
        )

        self.assertEqual(context["active_defect"]["observed_ref"], "record:observed")
        self.assertEqual(context["active_defect"]["expected"], "Implement and verify the requested change.")
        self.assertEqual(context["active_defect"]["actual"], "No repository change was produced.")
        self.assertEqual(context["active_defect"]["mechanism"], "deadline_reached_with_empty_patch")
        self.assertEqual(context["outgoing_edges_on_active_path"][0]["to_ref"], progress_ref)
        self.assertEqual(context["downstream_judgments"][0]["defect_type"], "empty_patch")
        self.assertIn("record:action", context["causal_episode"]["member_refs"])
        self.assertEqual(context["progress_episode"]["ref"], progress_ref)
        self.assertIn("record:reasoning", context["progress_episode"]["member_refs"])
        self.assertEqual(
            context["progress_navigation_window"]["anchor_episode_ref"],
            progress_ref,
        )
        self.assertIn(
            "record:action",
            context["progress_navigation_window"]["candidate_member_refs"],
        )

    def test_reconstructs_progress_episodes_and_projects_observed_defect(self):
        trace = {
            "case_id": "progress-reconstruction-case",
            "records": [
                {
                    "record_id": "search_reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Search the installed package for a reference implementation.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "search_action",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:01.000Z",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "grep",
                        "rationale": "Search the installed package.",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_1",
                        },
                    },
                },
                {
                    "record_id": "search_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "timestamp": "2026-07-17T10:00:02.000Z",
                    "data": {
                        "tool_name": "grep",
                        "call_id": "call_1",
                        "output": "No separate installation exists.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "repeat_reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:01:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Check the installed package again before implementing.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_2"},
                    },
                },
                {
                    "record_id": "repeat_action",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:01:01.000Z",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "bash",
                        "rationale": "Run pip show for the same package.",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_2",
                            "callID": "call_2",
                        },
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "timestamp": "2026-07-17T10:30:00.000Z",
                    "source_refs": ["record:lifecycle_only"],
                    "data": {
                        "failure_type": "deadline_reached_with_empty_patch",
                        "description": "The case ended with an empty patch.",
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)
        episode_refs = [ref for ref, node in graph.nodes.items() if node.event_type == "progress.episode"]

        self.assertEqual(len(episode_refs), 2)
        first, second = episode_refs
        self.assertIn("record:search_reasoning", graph.upstream_refs(first))
        self.assertIn("record:repeat_reasoning", graph.upstream_refs(second))
        self.assertIn(first, graph.upstream_refs(second))
        self.assertIn(second, graph.upstream_refs("record:observed"))
        self.assertTrue(graph.nodes[second].data["offline_only"])
        self.assertEqual(graph.nodes[second].data["behavior_impact"], "none")
        self.assertEqual(graph.nodes[second].data["consecutive_no_delivery_episodes"], 2)

    def test_projects_final_response_to_latest_prior_progress_episode(self):
        trace = {
            "case_id": "response-progress-projection-case",
            "records": [
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Cancel every task a second time after gather is interrupted.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:00:01.000Z",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "write",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_1",
                        },
                    },
                },
                {
                    "record_id": "final_output",
                    "component": "result",
                    "event_type": "response.output",
                    "timestamp": "2026-07-17T10:00:05.000Z",
                    "data": {
                        "text": "All asynchronous cleanup always completes.",
                        "is_final_for_case": True,
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)
        episode_ref = next(ref for ref, node in graph.nodes.items() if node.event_type == "progress.episode")

        self.assertIn(episode_ref, graph.upstream_refs("record:final_output"))
        self.assertNotIn("record:final_output", graph.upstream_refs(episode_ref))

    def test_orders_progress_episodes_by_timestamp_not_record_insertion(self):
        trace = {
            "case_id": "out-of-order-progress-case",
            "records": [
                {
                    "record_id": "late_turn_old_context",
                    "component": "context",
                    "event_type": "context.compaction_check",
                    "timestamp": "2026-07-17T09:00:00.000Z",
                    "data": {
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_2"},
                    },
                },
                {
                    "record_id": "late_reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:02:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_2"},
                    },
                },
                {
                    "record_id": "early_reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:01:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "data": {"failure_type": "empty_patch"},
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)
        episodes = [node for node in graph.nodes.values() if node.event_type == "progress.episode"]

        self.assertEqual(episodes[0].data["member_refs"], ["record:early_reasoning"])
        self.assertEqual(episodes[1].data["member_refs"], ["record:late_reasoning"])
        self.assertEqual(episodes[1].data["previous_episode_ref"], episodes[0].ref)
        self.assertIn(episodes[1].ref, graph.upstream_refs("record:observed"))

    def test_progress_episode_separates_full_members_from_judgment_candidates(self):
        trace = {
            "case_id": "progress-candidate-case",
            "records": [
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "The interface is understood; decide whether to implement.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "search_action",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "grep",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "write_action",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "write",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)
        episode = next(node for node in graph.nodes.values() if node.event_type == "progress.episode")

        self.assertEqual(
            episode.data["member_refs"],
            ["record:reasoning", "record:search_action", "record:write_action"],
        )
        self.assertEqual(
            episode.data["candidate_member_refs"],
            ["record:reasoning", "record:write_action"],
        )
        self.assertEqual(episode.data["excluded_member_refs"], ["record:search_action"])
        self.assertEqual(episode.data["candidate_selection_method"], "offline_semantic_role_projection_v1")

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
            artifact_content = artifact_path.read_bytes()
            artifact_hash = hashlib.sha256(artifact_content).hexdigest()
            trace = {
                "case_id": "retained-decision-case",
                "artifacts": [
                    {
                        "artifact_id": "artifact_request",
                        "path": "artifacts/sha256/request.json",
                        "kind": "json",
                        "hash": artifact_hash,
                        "byte_length": len(artifact_content),
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
                                "hash": artifact_hash,
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

    def test_message_lineage_never_matches_tampered_artifact_bytes(self):
        rationale = (
            "A sufficiently long decision rationale must not become lineage evidence "
            "when the artifact manifest hash does not verify its actual bytes."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = root / "artifacts" / "request.json"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text(json.dumps({"content": rationale}), encoding="utf-8")
            trace = {
                "case_id": "tampered-lineage-case",
                "artifacts": [{
                    "artifact_id": "artifact_request",
                    "path": "artifacts/request.json",
                    "kind": "json",
                    "hash": hashlib.sha256(b"different bytes").hexdigest(),
                    "byte_length": artifact_path.stat().st_size,
                }],
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"decision_type": "reasoning_block", "rationale": rationale},
                    },
                    {
                        "record_id": "llm",
                        "component": "llm",
                        "event_type": "llm.call",
                        "data": {"input_messages": {"artifact_id": "artifact_request"}},
                    },
                ],
            }
            graph = TraceGraph.from_trace(trace, artifact_root=root)

        retained = [
            edge for edge in graph.message_lineage["edges"]
            if edge["relation"] == "retained_in_context"
        ]
        self.assertEqual(retained, [])
        self.assertEqual(graph.message_lineage["gaps"][0]["reason"], "bundle_file_hash_mismatch")

    def test_resolves_source_refs_and_dataflow_edges(self):
        graph = TraceGraph.from_trace(sample_trace())

        self.assertIn("record:claim_bad", graph.nodes)
        upstream = graph.upstream_refs("record:claim_bad")

        self.assertIn("record:change_bad", upstream)
        self.assertIn("record:evidence_old", upstream)

    def test_loads_trace_6_causal_ir_with_legacy_attribution_projection(self):
        trace = {
            "trace_version": "6.0",
            "causal_ir_version": "1.0",
            "manifest": {"case_id": "causal-ir-compatibility-case"},
            "nodes": [
                {
                    "node_id": "canonical_only",
                    "kind": "decision",
                    "component": "processor",
                    "timestamp": "2026-07-14T00:00:00.000Z",
                    "time_ms": 1,
                    "data": {"chosen_action": "this canonical-only node must not load"},
                },
                {
                    "node_id": "dataflow_origin",
                    "kind": "tool.result",
                    "component": "tool",
                    "timestamp": "2026-07-14T00:00:01.000Z",
                    "time_ms": 2,
                    "data": {"output": "This canonical edge intentionally conflicts with compatibility."},
                },
                {
                    "node_id": "dataflow_target",
                    "kind": "case.observed_defect",
                    "component": "evaluation",
                    "timestamp": "2026-07-14T00:00:02.000Z",
                    "time_ms": 3,
                    "data": {"summary": "This canonical edge is the reverse of compatibility dataflow."},
                },
            ],
            "edges": [
                {
                    "edge_id": "canonical_reverse_conflict",
                    "from": {"type": "node", "id": "dataflow_target"},
                    "to": {"type": "node", "id": "dataflow_origin"},
                    "relation": "canonical_conflicting_relation",
                }
            ],
            "records": [
                {
                    "record_id": "source_ref_origin",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"decision_id": "source_ref_origin", "chosen_action": "provide source-ref support"},
                },
                {
                    "record_id": "source_ref_target",
                    "component": "result",
                    "event_type": "response.claim",
                    "source_refs": ["decision:source_ref_origin"],
                    "data": {"claim_id": "source_ref_target", "text": "Compatibility source refs are independent."},
                },
                {
                    "record_id": "dataflow_origin",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {"call_id": "dataflow_origin", "output": "provide edge support"},
                },
                {
                    "record_id": "dataflow_target",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "data": {"defect_id": "dataflow_target", "summary": "Compatibility dataflow is independent."},
                },
            ],
            "dataflow_edges": [
                {
                    "edge_id": "compatibility_dataflow_edge",
                    "from": {"type": "tool_result", "id": "dataflow_origin"},
                    "to": {"type": "record", "id": "dataflow_target"},
                    "relation": "compatibility_dataflow_relation",
                }
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(
            set(graph.nodes),
            {
                "record:source_ref_origin",
                "record:source_ref_target",
                "record:dataflow_origin",
                "record:dataflow_target",
            },
        )
        self.assertNotIn("record:canonical_only", graph.nodes)
        self.assertEqual(
            graph.nodes["record:source_ref_target"].source_refs,
            ["decision:source_ref_origin"],
        )
        self.assertEqual(
            graph.raw_trace["dataflow_edges"],
            [
                {
                    "edge_id": "compatibility_dataflow_edge",
                    "from": {"type": "tool_result", "id": "dataflow_origin"},
                    "to": {"type": "record", "id": "dataflow_target"},
                    "relation": "compatibility_dataflow_relation",
                }
            ],
        )
        self.assertEqual(graph.upstream_refs("record:source_ref_target"), ["record:source_ref_origin"])
        self.assertEqual(graph.upstream_refs("record:dataflow_target"), ["record:dataflow_origin"])
        self.assertEqual(graph.upstream_refs("record:dataflow_origin"), [])
        self.assertEqual(graph.downstream_refs("record:source_ref_origin"), ["record:source_ref_target"])
        self.assertEqual(graph.downstream_refs("record:dataflow_origin"), ["record:dataflow_target"])
        self.assertEqual(graph.downstream_refs("record:dataflow_target"), [])

    def test_skips_explicitly_ineligible_edges_but_keeps_legacy_edges_without_a_flag(self):
        trace = {
            "records": [
                {"record_id": "blocked", "event_type": "tool.result", "data": {"call_id": "blocked"}},
                {"record_id": "legacy", "event_type": "tool.result", "data": {"call_id": "legacy"}},
                {"record_id": "target", "event_type": "response.claim", "data": {"claim_id": "target"}},
            ],
            "dataflow_edges": [
                {
                    "edge_id": "explicit_false",
                    "from": {"type": "tool_result", "id": "blocked"},
                    "to": {"type": "response_claim", "id": "target"},
                    "relation": "supports_claim",
                    "metadata": {"eligible_for_attribution": False},
                },
                {
                    "edge_id": "legacy_default_true",
                    "from": {"type": "tool_result", "id": "legacy"},
                    "to": {"type": "response_claim", "id": "target"},
                    "relation": "supports_claim",
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.upstream_refs("record:target"), ["record:legacy"])

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
            artifact_content = "The decisive architecture boundary is billing-core, which owns renewalQuote end to end."
            artifact_path.write_text(artifact_content, encoding="utf-8")
            trace = {
                "case_id": "artifact-case",
                "artifacts": [
                    {
                        "artifact_id": "artifact_tool_output",
                        "path": "artifacts/sha256/tool-output.txt",
                        "kind": "text",
                        "hash": hashlib.sha256(artifact_content.encode("utf-8")).hexdigest()[:16],
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

    def test_partial_latest_uses_manifest_declared_trace_root_for_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact_path = root / "artifacts" / "sha256" / "decision.txt"
            artifact_path.parent.mkdir(parents=True)
            artifact_path.write_text("Complete cancellation rationale.", encoding="utf-8")
            partial = root / "partial" / "latest.json"
            partial.parent.mkdir()
            partial.write_text(
                json.dumps(
                    {
                        "manifest": {
                            "case_id": "partial-artifact-case",
                            "files": {"partial_latest": "partial/latest.json"},
                        },
                        "artifacts": [
                            {
                                "artifact_id": "decision-rationale",
                                "path": "artifacts/sha256/decision.txt",
                                "kind": "text",
                                "hash": hashlib.sha256(b"Complete cancellation rationale.").hexdigest()[:16],
                            }
                        ],
                        "records": [
                            {
                                "record_id": "decision",
                                "component": "processor",
                                "event_type": "decision",
                                "artifact_refs": ["decision-rationale"],
                                "data": {"rationale": {"artifact_id": "decision-rationale"}},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            graph = TraceGraph.from_file(partial)
            node = graph.hydrate_node("record:decision")

        self.assertEqual(graph.artifact_hydration["artifact_root"], str(root.resolve()))
        self.assertEqual(
            node.data["hydrated_artifacts"][0]["content"],
            "Complete cancellation rationale.",
        )

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

        self.assertEqual(refs, ["record:post_change", "record:change"])
        self.assertNotIn("record:baseline", refs)

    def test_evidence_validation_allows_only_explicit_superseded_lineage_refs(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "superseded-lineage-audit-case",
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
                        "record_id": "current",
                        "component": "tool",
                        "event_type": "verification",
                        "data": {
                            "verification_id": "current",
                            "repository_revision": 1,
                            "effective_for_final_state": True,
                        },
                    },
                ],
            }
        )

        graph.assert_evidence_eligible_references(
            {
                "supersedes_refs": ["verification:baseline"],
                "verification_supersedes_refs": ["verification:baseline"],
            },
            label="historical lineage metadata",
        )
        with self.assertRaisesRegex(
            ValueError,
            "violates graph evidence eligibility",
        ):
            graph.assert_evidence_eligible_references(
                {"evidence_refs": ["verification:baseline"]},
                label="current evidence",
            )

    def test_graph_ranking_helpers_filter_stale_revisions_before_limit(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "active-revision-ranking-case",
                "manifest": {"subject_revision": "git:active"},
                "records": [
                    {
                        "record_id": "active_decision",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_id": "active_decision",
                            "subject_revision": "git:active",
                        },
                    },
                    {
                        "record_id": "stale_decision",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_id": "stale_decision",
                            "subject_revision": "git:stale",
                        },
                    },
                    {
                        "record_id": "stale_support",
                        "component": "tool",
                        "event_type": "verification",
                        "data": {
                            "verification_id": "stale_support",
                            "subject_revision": "git:stale",
                        },
                    },
                    {
                        "record_id": "active_support_a",
                        "component": "tool",
                        "event_type": "verification",
                        "data": {
                            "verification_id": "active_support_a",
                            "subject_revision": "git:active",
                        },
                    },
                    {
                        "record_id": "active_support_b",
                        "component": "tool",
                        "event_type": "verification",
                        "data": {
                            "verification_id": "active_support_b",
                            "subject_revision": "git:active",
                        },
                    },
                    {
                        "record_id": "claim",
                        "component": "result",
                        "event_type": "response.claim",
                        "source_refs": [
                            "verification:stale_support",
                            "verification:active_support_a",
                            "verification:active_support_b",
                            "decision:active_decision",
                            "decision:stale_decision",
                        ],
                        "data": {
                            "subject_revision": "git:active",
                            "direct_support_refs": [
                                "verification:stale_support",
                                "verification:active_support_a",
                                "verification:active_support_b",
                            ],
                        },
                    },
                ],
            }
        )

        self.assertEqual(
            graph.causal_decision_refs("record:claim", limit=1),
            ["record:active_decision"],
        )
        self.assertEqual(
            [
                node.ref
                for node in graph.upstream_nodes("record:claim", limit=2)
            ],
            ["record:active_support_a", "record:active_support_b"],
        )

        stale_only = TraceGraph.from_trace(
            {
                "case_id": "no-active-ranking-case",
                "manifest": {"subject_revision": "git:active"},
                "records": [
                    {
                        "record_id": "stale",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_id": "stale",
                            "subject_revision": "git:stale",
                        },
                    },
                    {
                        "record_id": "target",
                        "component": "result",
                        "event_type": "response.output",
                        "source_refs": ["decision:stale"],
                        "data": {"subject_revision": "git:active"},
                    },
                ],
            }
        )
        self.assertEqual(stale_only.causal_decision_refs("record:target", limit=1), [])
        self.assertEqual(stale_only.upstream_nodes("record:target", limit=1), [])

    def test_judgment_context_and_analyzer_receive_active_upstream_backfill(self):
        from trace_attribution.judgment_context import build_causal_judgment_context

        graph = TraceGraph.from_trace(
            {
                "case_id": "active-context-backfill-case",
                "manifest": {"subject_revision": "git:active"},
                "records": [
                    {
                        "record_id": "stale",
                        "component": "tool",
                        "event_type": "tool.result",
                        "data": {
                            "text": "stale support",
                            "subject_revision": "git:stale",
                        },
                    },
                    {
                        "record_id": "active",
                        "component": "tool",
                        "event_type": "tool.result",
                        "data": {
                            "text": "active support",
                            "subject_revision": "git:active",
                        },
                    },
                    {
                        "record_id": "defect",
                        "component": "result",
                        "event_type": "response.output",
                        "source_refs": ["record:stale", "record:active"],
                        "data": {
                            "actual": "The active result is defective.",
                            "subject_revision": "git:active",
                        },
                    },
                ],
            }
        )
        context = build_causal_judgment_context(
            graph=graph,
            node_ref="record:defect",
            path=["record:defect"],
            judgments={},
            episode_index=CausalEpisodeIndex.from_graph(graph),
            objective="Inspect active evidence only.",
        )

        self.assertEqual(context["context_manifest"]["upstream_candidate_count"], 1)
        self.assertEqual(
            {
                edge["from_ref"]
                for edge in context["incoming_edges"]
            },
            {"record:active"},
        )

        class CapturingJudge:
            def __init__(self):
                self.upstream_refs = []

            def judge_node(
                self, *, node, upstream_nodes, downstream_context, objective
            ):
                self.upstream_refs.append([item.ref for item in upstream_nodes])
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_reason="The test only inspects Judge-facing context.",
                )

        judge = CapturingJudge()
        BackwardTaintAnalyzer(judge=judge).analyze(
            graph,
            start_refs=["record:defect"],
            objective="Inspect active evidence only.",
        )
        self.assertEqual(judge.upstream_refs[0], ["record:active"])

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

    def test_interrupted_trace_prefers_case_failure_over_stale_completion_diagnostics(self):
        trace = {
            "case_id": "interrupted-case",
            "manifest": {"shutdown_disposition": "interrupted_before_case_completion"},
            "records": [
                {
                    "record_id": "missing_semantic_final_test_result",
                    "component": "trace",
                    "event_type": "case.missing_semantic",
                    "data": {"semantic_name": "final_test_result"},
                },
                {
                    "record_id": "observed_defect_missing_verification_after_change",
                    "component": "trace",
                    "event_type": "case.observed_defect",
                    "data": {"failure_type": "final_test_result_missing"},
                },
                {
                    "record_id": "case_failed",
                    "component": "run",
                    "event_type": "case.failed",
                    "status": "cancelled",
                    "data": {
                        "shutdown_signal": "SIGINT",
                        "shutdown_disposition": "interrupted_before_case_completion",
                    },
                },
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.default_start_refs(), ["record:case_failed"])

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

    def test_stale_review_refs_fall_back_to_current_final_result_anchors(self):
        trace = sample_trace()
        review = {
            "case_id": "unit-case",
            "observed_defects": [
                {
                    "component": "verification",
                    "failure_type": "false_completion",
                    "description": "The final answer claims success despite missing verification.",
                    "record_refs": [
                        "record:decisionnode_from_an_older_trace_revision",
                    ],
                }
            ],
        }

        enriched = inject_quality_gap_records(trace, review)
        graph = TraceGraph.from_trace(enriched)
        defect = graph.nodes[
            "record:observed_defect_verification_false_completion"
        ]

        self.assertEqual(defect.source_refs, ["record:claim_bad"])
        self.assertEqual(
            defect.data["source_binding"]["mode"],
            "final_result_fallback",
        )
        self.assertEqual(
            defect.data["source_binding"]["unresolved_declared_refs"],
            ["record:decisionnode_from_an_older_trace_revision"],
        )
        self.assertEqual(
            defect.data["source_binding"]["fallback_refs"],
            ["record:claim_bad"],
        )
        self.assertEqual(
            graph.upstream_refs(defect.ref),
            ["record:claim_bad"],
        )

    def test_existing_offline_observed_defect_is_rebound_in_place(self):
        trace = sample_trace()
        trace["records"].append(
            {
                "record_id": "observed_defect_verification_false_completion",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": [
                    "record:decisionnode_from_an_older_trace_revision",
                ],
                "data": {
                    "offline_only": True,
                    "failure_type": "false_completion",
                },
            }
        )
        review = {
            "case_id": "unit-case",
            "observed_defects": [
                {
                    "component": "verification",
                    "failure_type": "false_completion",
                    "description": "The final answer claims success despite missing verification.",
                    "record_refs": [
                        "record:decisionnode_from_an_older_trace_revision",
                    ],
                }
            ],
        }

        enriched = inject_quality_gap_records(trace, review)
        matching = [
            record
            for record in enriched["records"]
            if record.get("record_id")
            == "observed_defect_verification_false_completion"
        ]
        graph = TraceGraph.from_trace(enriched)
        defect = graph.nodes[
            "record:observed_defect_verification_false_completion"
        ]

        self.assertEqual(len(matching), 1)
        self.assertEqual(defect.source_refs, ["record:claim_bad"])
        self.assertEqual(
            defect.data["source_binding"]["mode"],
            "final_result_fallback",
        )
        self.assertEqual(
            defect.data["description"],
            "The final answer claims success despite missing verification.",
        )

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

    def test_trace_node_compact_preserves_claim_grounding_decisions(self):
        node = TraceNode(
            ref="record:claim",
            record_id="claim",
            component="result",
            event_type="response.claim",
            data={
                "text": "The active discount cap is 15 percent.",
                "verification_refs": ["verification:post_change"],
                "verification_repository_revision": 1,
                "verification_phase": "post_change",
                "verification_status": "passed",
                "verification_effective_for_final_state": True,
                "verification_temporal_role": "current_effective",
                "grounding_candidate_refs": ["evidence:current", "evidence:unrelated"],
                "grounding_method": "confirmed_context_semantic_match_v1",
                "grounding_behavior_impact": "none",
                "grounding_decisions": [
                    {
                        "candidate_ref": "evidence:current",
                        "decision": "selected_direct_support",
                        "agent_attention_observed": False,
                        "behavior_impact": "none",
                    },
                    {
                        "candidate_ref": "evidence:unrelated",
                        "decision": "rejected_inapplicable",
                        "rejection_reason": "superseded_verification",
                        "candidate_repository_revision": 0,
                        "candidate_effective_for_final_state": False,
                        "candidate_verification_status": "failed",
                        "candidate_temporal_role": "superseded",
                        "attribution_eligible": False,
                        "agent_attention_observed": False,
                        "behavior_impact": "none",
                    },
                ],
                "unrelated_payload": "x" * 5000,
            },
        )

        compact = node.compact(max_chars=1200)

        self.assertLessEqual(len(stable_json(compact)), 1200)
        self.assertEqual(compact["data"]["grounding_method"], "confirmed_context_semantic_match_v1")
        self.assertEqual(compact["data"]["grounding_behavior_impact"], "none")
        self.assertEqual(compact["data"]["verification_refs"], ["verification:post_change"])
        self.assertEqual(compact["data"]["verification_repository_revision"], 1)
        self.assertTrue(compact["data"]["verification_effective_for_final_state"])
        self.assertEqual(compact["data"]["verification_temporal_role"], "current_effective")
        self.assertEqual(
            compact["data"]["grounding_decisions"][0]["decision"],
            "selected_direct_support",
        )
        self.assertEqual(
            compact["data"]["grounding_decisions"][1]["rejection_reason"],
            "superseded_verification",
        )

    def test_superseded_verification_advisory_edge_is_not_a_backward_predecessor(self):
        trace = {
            "records": [
                {
                    "record_id": "baseline_fact",
                    "component": "tool",
                    "event_type": "evidence.semantic_fact",
                    "data": {"verification_temporal_role": "superseded"},
                },
                {
                    "record_id": "current_claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "All tests passed."},
                },
            ],
            "edges": [
                {
                    "edge_id": "superseded_context",
                    "from": {"ref_type": "node", "ref_id": "baseline_fact"},
                    "to": {"ref_type": "node", "ref_id": "current_claim"},
                    "relation": "context_to_claim",
                    "evidence_tier": "temporal_advisory",
                    "eligible_for_attribution": False,
                    "metadata": {"causal_semantics": "superseded_verification_context"},
                }
            ],
        }

        graph = TraceGraph.from_trace(trace)

        self.assertEqual(graph.upstream_refs("record:current_claim"), [])

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
    def test_reports_judge_cache_and_provider_circuit_metrics(self):
        class MetricsJudge(FakeJudge):
            model = "metrics-model"
            request_count = 4
            timeout_seconds = 3600
            thinking_mode = "disabled"
            thinking_config = {"type": "disabled"}
            cache_stats = {
                "enabled": True,
                "path": "/tmp/judge-cache.jsonl",
                "loaded_entries": 3,
                "hits": 2,
                "misses": 1,
                "writes": 1,
                "corrupt_entries": 0,
            }
            provider_circuit_stats = {
                "threshold": 3,
                "consecutive_errors": 0,
                "open": False,
                "reason": "",
            }

        report = BackwardTaintAnalyzer(judge=MetricsJudge({})).analyze(
            TraceGraph.from_trace(
                {
                    "case_id": "judge-metrics-case",
                    "records": [
                        {
                            "record_id": "response",
                            "component": "result",
                            "event_type": "response.output",
                            "data": {"text": "Completed."},
                        }
                    ],
                }
            )
        )

        self.assertEqual(report.metadata["judge_cache"]["hits"], 2)
        self.assertEqual(report.metadata["judge_cache"]["writes"], 1)
        self.assertEqual(report.metadata["provider_circuit"]["threshold"], 3)
        self.assertFalse(report.metadata["provider_circuit"]["open"])

    def test_stops_branch_when_provider_circuit_opens(self):
        try:
            from trace_attribution.errors import JudgeProviderUnavailable
        except ModuleNotFoundError as exc:
            self.fail(f"provider error module is missing: {exc}")

        class CircuitJudge(FakeJudge):
            def __init__(self):
                super().__init__({})
                self.calls = []

            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.calls.append(node.ref)
                if len(self.calls) == 3:
                    raise JudgeProviderUnavailable("provider circuit opened")
                upstream_ref = node.source_refs[0] if node.source_refs else ""
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=True,
                    defect_status="present",
                    defect_type="propagated_plan_defect",
                    defect_reason="The same plan defect is present in the upstream decision.",
                    causal_role="defect_propagation",
                    influenced_by=(
                        [
                            TaintInfluence(
                                upstream_ref=upstream_ref,
                                reason="The upstream decision already contains the same defect.",
                                relation="defect_propagated_from",
                                confidence=1.0,
                            )
                        ]
                        if upstream_ref
                        else []
                    ),
                    confidence=0.9,
                )

        trace = {
            "case_id": "provider-circuit-case",
            "records": [
                {
                    "record_id": "n0",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"rationale": "Earliest decision."},
                },
                {
                    "record_id": "n1",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:n0"],
                    "data": {"rationale": "First upstream decision."},
                },
                {
                    "record_id": "n2",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:n1"],
                    "data": {"rationale": "Second upstream decision."},
                },
                {
                    "record_id": "n3",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:n2"],
                    "data": {"rationale": "Latest decision."},
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:n3"],
                    "data": {"failure_type": "bad_result"},
                },
            ],
        }
        judge = CircuitJudge()

        report = BackwardTaintAnalyzer(judge=judge, max_depth=8, max_nodes=16).analyze(
            TraceGraph.from_trace(trace)
        )

        branch = report.defect_branches[0]
        self.assertEqual(branch.metadata["termination_reason"], "provider_unavailable")
        self.assertTrue(branch.metadata["provider_unavailable"])
        self.assertEqual(judge.calls, ["record:n3", "record:n2", "record:n1"])
        self.assertNotIn("record:n0", report.visited_order)
        self.assertIn("record:n1", branch.unresolved_refs)
        self.assertNotIn("record:n1", branch.node_judgments)

    def test_progress_navigation_collapses_no_delivery_turns_without_losing_candidates(self):
        def decision(record_id, message_id, *, rationale="", action=""):
            data = {
                "decision_type": "llm_tool_call" if action else "reasoning_block",
                "rationale": rationale,
                "metadata": {"sessionID": "ses_1", "messageID": message_id},
            }
            if action:
                data["chosen_action"] = action
            return {
                "record_id": record_id,
                "component": "processor",
                "event_type": "decision",
                "timestamp": f"2026-07-17T10:00:0{message_id[-1]}.000Z",
                "data": data,
            }

        graph = TraceGraph.from_trace(
            {
                "case_id": "progress-window-case",
                "records": [
                    decision("before_delivery", "msg_1", rationale="Understand the requirement."),
                    decision("delivery", "msg_2", rationale="Edit the implementation.", action="edit"),
                    decision("reason_after", "msg_3", rationale="Inspect the remaining failure."),
                    decision("search_after", "msg_4", rationale="Search another location.", action="grep"),
                    decision("reason_latest", "msg_5", rationale="Continue searching without a new patch."),
                ],
            }
        )
        episodes = sorted(
            (node for node in graph.nodes.values() if node.event_type == "progress.episode"),
            key=lambda node: node.data["chronology_index"],
        )
        latest = episodes[-1]
        delivery_episode = next(
            node for node in episodes if "record:delivery" in node.data["member_refs"]
        )

        predecessors = semantic_episode_predecessors(
            graph,
            CausalEpisodeIndex.from_graph(graph),
            latest.ref,
        )

        self.assertIn("record:reason_after", predecessors)
        self.assertIn("record:search_after", predecessors)
        self.assertIn("record:reason_latest", predecessors)
        self.assertIn("record:delivery", predecessors)
        self.assertIn(delivery_episode.ref, predecessors)
        self.assertNotIn(episodes[-2].ref, predecessors)

        from trace_attribution.judgment_context import build_causal_judgment_context

        window_member_context = build_causal_judgment_context(
            graph=graph,
            node_ref="record:reason_after",
            path=[latest.ref, "record:reason_after"],
            judgments={},
            episode_index=CausalEpisodeIndex.from_graph(graph),
            objective="Find why delivery stalled.",
        )
        direct_member_context = build_causal_judgment_context(
            graph=graph,
            node_ref="record:reason_latest",
            path=[latest.ref, "record:reason_latest"],
            judgments={},
            episode_index=CausalEpisodeIndex.from_graph(graph),
            objective="Find why delivery stalled.",
        )

        self.assertEqual(
            window_member_context["outgoing_edges_on_active_path"][0]["relation"],
            "progress_window_member",
        )
        self.assertEqual(
            direct_member_context["outgoing_edges_on_active_path"][0]["relation"],
            "progress_episode_member",
        )

    def test_passes_structured_context_to_context_aware_judge(self):
        class ContextAwareJudge(FakeJudge):
            def __init__(self):
                super().__init__({})
                self.contexts = {}

            def judge_node_with_context(
                self,
                *,
                node,
                upstream_nodes,
                downstream_context,
                objective,
                judgment_context,
            ):
                self.contexts[node.ref] = judgment_context
                return self.judge_node(
                    node=node,
                    upstream_nodes=upstream_nodes,
                    downstream_context=downstream_context,
                    objective=objective,
                )

        graph = TraceGraph.from_trace(sample_trace())
        judge = ContextAwareJudge()

        report = BackwardTaintAnalyzer(judge=judge).analyze(
            graph,
            start_refs=["record:claim_bad"],
            objective="Return a truthful final implementation summary.",
        )

        context = judge.contexts["record:claim_bad"]
        self.assertEqual(context["current_ref"], "record:claim_bad")
        self.assertEqual(context["active_defect"]["observed_ref"], "record:claim_bad")
        self.assertTrue(context["incoming_edges"])
        self.assertEqual(context["behavior_impact"], "none_offline_analysis_only")
        recorded = report.defect_branches[0].metadata["judgment_contexts"]["record:claim_bad"]
        self.assertEqual(recorded["active_defect"]["fingerprint"], context["active_defect"]["fingerprint"])
        self.assertEqual(recorded["context_manifest"], context["context_manifest"])

    def test_progress_episode_expands_to_concrete_decision_and_is_not_a_root(self):
        trace = {
            "case_id": "progress-traversal-case",
            "records": [
                {
                    "record_id": "wrong_plan",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Keep searching instead of implementing the requested change.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "later_plan",
                    "component": "processor",
                    "event_type": "decision",
                    "timestamp": "2026-07-17T10:01:00.000Z",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Report that no implementation was completed.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_2"},
                    },
                },
                {
                    "record_id": "shutdown",
                    "component": "lifecycle",
                    "event_type": "lifecycle.signal",
                    "timestamp": "2026-07-17T10:02:00.000Z",
                    "data": {"signal": "SIGTERM"},
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:shutdown"],
                    "data": {"failure_type": "deadline_reached_with_empty_patch"},
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        episode_refs = [ref for ref, node in graph.nodes.items() if node.event_type == "progress.episode"]
        judge = FakeJudge(
            {
                episode_refs[-1]: NodeJudgment(
                    node_ref=episode_refs[-1],
                    component="progress",
                    event_type="progress.episode",
                    has_defect=True,
                    defect_type="no_delivery_progress",
                    defect_reason="The aggregate records a no-delivery episode.",
                    causal_role="defect_introduction",
                    is_root_cause=True,
                ),
                "record:wrong_plan": NodeJudgment(
                    node_ref="record:wrong_plan",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_type="abandoned_implementation",
                    defect_reason="The plan explicitly chooses continued search over implementation.",
                    causal_role="defect_introduction",
                    is_root_cause=True,
                    confidence=0.9,
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(graph)

        self.assertIn(episode_refs[-1], report.visited_order)
        self.assertNotIn(episode_refs[0], report.visited_order)
        self.assertIn("record:wrong_plan", report.visited_order)
        self.assertNotIn("record:shutdown", report.visited_order)
        self.assertEqual([root.node_ref for root in report.root_causes], ["record:wrong_plan"])
        latest_context = report.defect_branches[0].metadata["judgment_contexts"][episode_refs[-1]]
        self.assertEqual(
            latest_context["progress_navigation_window"]["member_episode_refs"],
            episode_refs,
        )

    def test_uses_optional_judge_to_validate_evaluation_assertion(self):
        class EvaluationAwareJudge(FakeJudge):
            def __init__(self):
                super().__init__({})
                self.evaluation_calls = []
                self.model = "evaluation-aware-test-model"
                self.request_count = 1

            def judge_evaluation_assertion(
                self, *, node, upstream_nodes, downstream_context, objective
            ):
                self.evaluation_calls.append(node.ref)
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="absent",
                    defect_reason="The supplied successful verification contradicts the asserted gap.",
                    causal_role="non_defective",
                    branch_relation="unrelated",
                    confidence=0.95,
                )

        trace = {
            "case_id": "evaluation-validation-case",
            "records": [
                {
                    "record_id": "passing_verification",
                    "component": "verification",
                    "event_type": "verification",
                    "data": {"status": "passed", "exit_code": 0},
                },
                {
                    "record_id": "quality_gap",
                    "component": "evaluation",
                    "event_type": "case.quality_gap",
                    "source_refs": ["record:passing_verification"],
                    "data": {"dimension": "implementation_correctness"},
                },
            ],
        }
        judge = EvaluationAwareJudge()

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.evaluation_calls, ["record:quality_gap"])
        self.assertEqual(report.node_judgments["record:quality_gap"].defect_status, "absent")
        self.assertEqual(report.metadata["analysis_outcome"], "no_defect")
        self.assertEqual(report.metadata["judge_model"], "evaluation-aware-test-model")
        self.assertEqual(report.metadata["judge_request_count"], 1)

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

        self.assertEqual([root.node_ref for root in report.root_causes], ["record:action"])
        self.assertEqual(
            set(report.root_causes[0].episode_member_refs),
            {"record:reasoning", "record:action", "record:tool_call"},
        )
        self.assertEqual(report.root_causes[0].observed_defect_refs, ["record:observed"])

    def test_expands_semantic_predecessors_from_an_observed_change_episode(self):
        trace = {
            "case_id": "change-episode-predecessor-case",
            "records": [
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Modify unrelated compatibility code to make the host test run.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "span_id": "span_edit",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "edit",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_edit",
                        },
                    },
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"call_id": "call_edit", "tool_name": "edit"},
                },
                {
                    "record_id": "change",
                    "component": "tool",
                    "event_type": "change",
                    "span_id": "span_edit",
                    "data": {
                        "change_id": "chg_1",
                        "files": ["src/compat.py"],
                        "diff": "- use_new_api()\n+ use_old_api()",
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {"failure_type": "out_of_scope_host_compatibility"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:change": NodeJudgment(
                    node_ref="record:change",
                    component="tool",
                    event_type="change",
                    has_defect=False,
                    defect_status="unknown",
                    defect_type="judge_schema_error",
                    defect_reason="The change judgment was structurally invalid after repair.",
                    causal_role="unknown",
                    branch_relation="unknown",
                    is_root_cause=False,
                ),
                "record:reasoning": NodeJudgment(
                    node_ref="record:reasoning",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="out_of_scope_host_compatibility",
                    defect_reason="The reasoning first chooses to edit unrelated compatibility code.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                    confidence=0.95,
                ),
                "record:action": NodeJudgment(
                    node_ref="record:action",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="out_of_scope_host_compatibility",
                    defect_reason="The edit action materializes the out-of-scope decision.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                    confidence=0.9,
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(
            TraceGraph.from_trace(trace).nodes[judge.calls[0]].event_type,
            "progress.episode",
        )
        self.assertIn("record:change", judge.calls)
        self.assertIn("record:reasoning", judge.calls)
        self.assertIn("record:action", judge.calls)
        self.assertNotIn("record:tool_call", judge.calls)
        self.assertEqual([root.node_ref for root in report.root_causes], ["record:action"])
        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        blocking_types = [item["gap_type"] for item in report.trace_improvement_report["blocking_gaps"]]
        advisory_types = [item["gap_type"] for item in report.trace_improvement_report["advisory_gaps"]]
        self.assertNotIn("unknown_node_judgment", blocking_types)
        self.assertIn("unknown_node_judgment", advisory_types)

    def test_root_confirmation_rejects_a_false_earlier_plan_before_episode_collapse(self):
        trace = {
            "case_id": "root-confirmation-case",
            "records": [
                {
                    "record_id": "plan",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Parse the declaration body without repeating its directive keyword.",
                        "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "edit",
                        "rationale": "Require the struct keyword in the declaration body.",
                        "metadata": {
                            "sessionID": "ses_1",
                            "messageID": "msg_1",
                            "callID": "call_edit",
                        },
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:plan", "record:action"],
                    "data": {"failure_type": "repeated_directive_keyword"},
                },
            ],
        }
        roots = {
            ref: NodeJudgment(
                node_ref=ref,
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="repeated_directive_keyword",
                defect_reason="This node was initially classified as containing the parser defect.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
                confidence=0.9,
            )
            for ref in ("record:plan", "record:action")
        }

        class ConfirmingJudge(FakeJudge):
            def __init__(self):
                super().__init__(roots)
                self.confirmed = []

            def confirm_root(self, *, node, judgment, downstream_context, objective):
                self.confirmed.append(node.ref)
                if node.ref == "record:plan":
                    return NodeJudgment(
                        node_ref=node.ref,
                        component=node.component,
                        event_type=node.event_type,
                        has_defect=False,
                        defect_status="absent",
                        defect_type="",
                        defect_reason="The plan specifies the correct non-repeated-keyword contract.",
                        causal_role="non_defective",
                        branch_relation="unrelated",
                        is_root_cause=False,
                        confidence=0.95,
                    )
                return judgment

        judge = ConfirmingJudge()
        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(set(judge.confirmed), {"record:plan", "record:action"})
        self.assertEqual([root.node_ref for root in report.root_causes], ["record:action"])
        self.assertEqual(report.node_judgments["record:plan"].defect_status, "absent")

    def test_promotes_first_observed_propagation_after_predecessor_is_disproved(self):
        trace = {
            "case_id": "root-confirmation-boundary-case",
            "records": [
                {
                    "record_id": "plan",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Parse the declaration body without repeating its directive keyword.",
                    },
                },
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:plan"],
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "edit",
                        "rationale": "Require the struct keyword in the declaration body.",
                    },
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:action"],
                    "data": {"failure_type": "repeated_directive_keyword"},
                },
            ],
        }
        judgments = {
            "record:action": NodeJudgment(
                node_ref="record:action",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="repeated_directive_keyword",
                defect_reason="The authored edit requires the keyword in the declaration body.",
                causal_role="defect_propagation",
                branch_relation="same_defect",
                influenced_by=[
                    TaintInfluence(
                        upstream_ref="record:plan",
                        reason="The initial judgment said the plan already contained this defect.",
                        relation="defect_propagated_from",
                    )
                ],
                is_root_cause=False,
                confidence=0.9,
            ),
            "record:plan": NodeJudgment(
                node_ref="record:plan",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="repeated_directive_keyword",
                defect_reason="The initial judgment incorrectly treats the correct plan as defective.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
                confidence=0.8,
            ),
        }

        class ConfirmingJudge(FakeJudge):
            def __init__(self):
                super().__init__(judgments)
                self.confirmed = []
                self.confirmation_contexts = {}

            def confirm_root(self, *, node, judgment, downstream_context, objective):
                self.confirmed.append(node.ref)
                self.confirmation_contexts[node.ref] = downstream_context
                if node.ref == "record:plan":
                    return NodeJudgment(
                        node_ref=node.ref,
                        component=node.component,
                        event_type=node.event_type,
                        has_defect=False,
                        defect_status="absent",
                        defect_type="",
                        defect_reason="The plan specifies the correct contract.",
                        causal_role="non_defective",
                        branch_relation="unrelated",
                        is_root_cause=False,
                        confidence=0.95,
                    )
                return judgment

        judge = ConfirmingJudge()
        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(judge.confirmed, ["record:plan", "record:action"])
        self.assertEqual([root.node_ref for root in report.root_causes], ["record:action"])
        action = report.node_judgments["record:action"]
        self.assertEqual(action.causal_role, "defect_introduction")
        self.assertTrue(action.is_root_cause)
        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        self.assertIn("record:action", report.defect_branches[0].metadata["first_observed_propagation_refs"])
        self.assertTrue(
            any(
                "disproved_defect_predecessor" in item and "record:plan" in item
                for item in judge.confirmation_contexts["record:action"]
            )
        )

    def test_does_not_promote_propagation_when_a_predecessor_is_unknown(self):
        trace = {
            "case_id": "unknown-boundary-case",
            "records": [
                {"record_id": "plan", "component": "processor", "event_type": "decision"},
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:plan"],
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:action"],
                    "data": {"failure_type": "wrong_action"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:action": NodeJudgment(
                    node_ref="record:action",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="wrong_action",
                    defect_reason="The action contains the observed defect.",
                    causal_role="defect_propagation",
                    branch_relation="same_defect",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:plan",
                            reason="The plan may contain the same defect.",
                            relation="defect_propagated_from",
                        )
                    ],
                ),
                "record:plan": NodeJudgment(
                    node_ref="record:plan",
                    component="processor",
                    event_type="decision",
                    has_defect=False,
                    defect_status="unknown",
                    defect_type="wrong_action",
                    defect_reason="The plan semantics are incomplete.",
                    causal_role="unknown",
                    branch_relation="unknown",
                    is_root_cause=False,
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:action"].causal_role, "defect_propagation")
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_does_not_promote_a_derived_response_claim_boundary(self):
        trace = {
            "case_id": "derived-claim-boundary-case",
            "records": [
                {"record_id": "output", "component": "result", "event_type": "response.output"},
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "source_refs": ["record:output"],
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:claim"],
                    "data": {"failure_type": "incorrect_claim"},
                },
            ],
        }
        judgments = {
            "record:claim": NodeJudgment(
                node_ref="record:claim",
                component="result",
                event_type="response.claim",
                has_defect=True,
                defect_status="present",
                defect_type="incorrect_claim",
                defect_reason="The claim was initially judged to repeat the output defect.",
                causal_role="defect_propagation",
                branch_relation="same_defect",
                influenced_by=[
                    TaintInfluence(
                        upstream_ref="record:output",
                        reason="The output was initially suspected to contain the same claim defect.",
                        relation="defect_propagated_from",
                    )
                ],
            ),
            "record:output": NodeJudgment(
                node_ref="record:output",
                component="result",
                event_type="response.output",
                has_defect=False,
                defect_status="absent",
                defect_reason="The response output is correct.",
                causal_role="non_defective",
                branch_relation="unrelated",
            ),
        }

        class AcceptingJudge(FakeJudge):
            def confirm_root(self, *, node, judgment, downstream_context, objective):
                return judgment

        report = BackwardTaintAnalyzer(judge=AcceptingJudge(judgments)).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:claim"].causal_role, "defect_propagation")
        branch = report.defect_branches[0]
        self.assertIn("record:claim", branch.metadata["first_observed_propagation_refs"])
        self.assertIn("record:claim", branch.metadata["non_promotable_first_observed_refs"])

    def test_removes_promoted_boundary_when_root_confirmation_rejects_it(self):
        trace = {
            "case_id": "rejected-boundary-case",
            "records": [
                {"record_id": "plan", "component": "processor", "event_type": "decision"},
                {
                    "record_id": "action",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:plan"],
                },
                {
                    "record_id": "observed",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:action"],
                    "data": {"failure_type": "wrong_action"},
                },
            ],
        }
        judgments = {
            "record:action": NodeJudgment(
                node_ref="record:action",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="wrong_action",
                defect_reason="The action contains the observed defect.",
                causal_role="defect_propagation",
                branch_relation="same_defect",
                influenced_by=[
                    TaintInfluence(
                        upstream_ref="record:plan",
                        reason="The plan was initially suspected.",
                        relation="defect_propagated_from",
                    )
                ],
            ),
            "record:plan": NodeJudgment(
                node_ref="record:plan",
                component="processor",
                event_type="decision",
                has_defect=False,
                defect_status="absent",
                defect_reason="The plan is correct.",
                causal_role="non_defective",
                branch_relation="unrelated",
            ),
        }

        class RejectingJudge(FakeJudge):
            def confirm_root(self, *, node, judgment, downstream_context, objective):
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="absent",
                    defect_reason="Counterfactual review rejects this node as the introduction.",
                    causal_role="non_defective",
                    branch_relation="unrelated",
                    is_root_cause=False,
                    confidence=0.9,
                )

        report = BackwardTaintAnalyzer(judge=RejectingJudge(judgments)).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:action"].defect_status, "absent")
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")

    def test_reports_task_quality_outcome_independently_from_trace_health(self):
        trace = {
            "case_id": "attribution-domain-case",
            "records": [
                {"record_id": "task_root", "component": "processor", "event_type": "decision"},
                {"record_id": "trace_evidence", "component": "trace", "event_type": "observation"},
                {
                    "record_id": "observed_task_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:task_root"],
                    "data": {"failure_type": "wrong_implementation", "attribution_domain": "task_quality"},
                },
                {
                    "record_id": "missing_final_test",
                    "component": "evaluation",
                    "event_type": "case.missing_semantic",
                    "source_refs": ["record:trace_evidence"],
                    "data": {"gap_kind": "final_test_result_missing", "attribution_domain": "trace_health"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:task_root": NodeJudgment(
                    node_ref="record:task_root",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="wrong_implementation",
                    defect_reason="The implementation decision introduced the task defect.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                ),
                "record:trace_evidence": NodeJudgment(
                    node_ref="record:trace_evidence",
                    component="trace",
                    event_type="observation",
                    has_defect=True,
                    defect_status="present",
                    defect_type="final_test_result_missing",
                    defect_reason="The trace does not expose the required verification result.",
                    causal_role="defect_propagation",
                    branch_relation="same_defect",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref="record:missing_trace_source",
                            reason="The missing trace source is not available.",
                        )
                    ],
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))

        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        self.assertEqual(
            report.metadata["analysis_outcomes_by_domain"],
            {"task_quality": "root_found", "trace_health": "inconclusive"},
        )
        branches = {branch.start_ref: branch for branch in report.defect_branches}
        self.assertEqual(branches["record:observed_task_defect"].metadata["attribution_domain"], "task_quality")
        self.assertEqual(branches["record:missing_final_test"].metadata["attribution_domain"], "trace_health")
        self.assertNotIn(
            "record:missing_final_test",
            [gap["node_ref"] for gap in report.trace_improvement_report["blocking_gaps"]],
        )
        self.assertIn(
            "record:missing_final_test",
            [gap["node_ref"] for gap in report.trace_improvement_report["advisory_gaps"]],
        )

    def test_trace_health_judge_errors_and_unresolved_refs_remain_advisory(self):
        trace = {
            "case_id": "attribution-secondary-errors-case",
            "records": [
                {"record_id": "task_root", "component": "processor", "event_type": "decision"},
                {"record_id": "trace_error", "component": "trace", "event_type": "observation"},
                {"record_id": "trace_unresolved", "component": "trace", "event_type": "observation"},
                {
                    "record_id": "observed_task_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:task_root"],
                    "data": {"failure_type": "wrong_implementation", "attribution_domain": "task_quality"},
                },
                {
                    "record_id": "missing_error_semantic",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:trace_error"],
                    "data": {"gap_kind": "judge_timeout", "attribution_domain": "trace_health"},
                },
                {
                    "record_id": "missing_ref_semantic",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:trace_unresolved"],
                    "data": {"gap_kind": "unresolved_provenance", "attribution_domain": "trace_health"},
                },
            ],
        }

        class SecondaryFailureJudge(FakeJudge):
            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                if node.ref == "record:trace_error":
                    raise TimeoutError("secondary trace-health judge timed out")
                if node.ref == "record:trace_unresolved":
                    return NodeJudgment(
                        node_ref=node.ref,
                        component=node.component,
                        event_type=node.event_type,
                        has_defect=True,
                        defect_status="present",
                        defect_type="unresolved_provenance",
                        defect_reason="The trace-health branch cites an unavailable provenance node.",
                        causal_role="defect_propagation",
                        influenced_by=[TaintInfluence(upstream_ref="record:missing_trace_source", reason="missing")],
                    )
                return super().judge_node(
                    node=node,
                    upstream_nodes=upstream_nodes,
                    downstream_context=downstream_context,
                    objective=objective,
                )

        judge = SecondaryFailureJudge(
            {
                "record:task_root": NodeJudgment(
                    node_ref="record:task_root",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="wrong_implementation",
                    defect_reason="The implementation decision introduced the task defect.",
                    causal_role="defect_introduction",
                    is_root_cause=True,
                    confidence=0.95,
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=judge).analyze(TraceGraph.from_trace(trace))
        blocking_types = [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]]
        advisory_types = [gap["gap_type"] for gap in report.trace_improvement_report["advisory_gaps"]]

        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        self.assertEqual(report.metadata["judge_error_count"], 0)
        self.assertEqual(report.unresolved_refs, [])
        self.assertEqual(report.metadata["judge_error_counts_by_domain"]["trace_health"], 1)
        self.assertEqual(report.metadata["unresolved_refs_by_domain"]["trace_health"], ["record:missing_trace_source"])
        self.assertNotIn("judge_error", blocking_types)
        self.assertNotIn("unresolved_trace_refs", blocking_types)
        self.assertIn("judge_error", advisory_types)
        self.assertIn("unresolved_trace_refs", advisory_types)

    def test_trace_health_search_limit_does_not_limit_a_found_task_root(self):
        trace = {
            "case_id": "attribution-secondary-limit-case",
            "records": [
                {"record_id": "task_root", "component": "processor", "event_type": "decision"},
                {"record_id": "trace_seed", "component": "trace", "event_type": "observation"},
                {"record_id": "trace_upstream", "component": "trace", "event_type": "observation"},
                {
                    "record_id": "observed_task_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:task_root"],
                    "data": {"failure_type": "wrong_implementation", "attribution_domain": "task_quality"},
                },
                {
                    "record_id": "missing_trace_semantic",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:trace_seed"],
                    "data": {"gap_kind": "deep_trace_gap", "attribution_domain": "trace_health"},
                },
            ],
        }
        judge = FakeJudge(
            {
                "record:task_root": NodeJudgment(
                    node_ref="record:task_root",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="wrong_implementation",
                    defect_reason="The implementation decision introduced the task defect.",
                    causal_role="defect_introduction",
                    is_root_cause=True,
                    confidence=0.95,
                ),
                "record:trace_seed": NodeJudgment(
                    node_ref="record:trace_seed",
                    component="trace",
                    event_type="observation",
                    has_defect=True,
                    defect_status="present",
                    defect_type="deep_trace_gap",
                    defect_reason="The trace-health branch requires a deeper predecessor.",
                    causal_role="defect_propagation",
                    influenced_by=[TaintInfluence(upstream_ref="record:trace_upstream", reason="continue")],
                ),
            }
        )

        report = BackwardTaintAnalyzer(judge=judge, max_nodes=2).analyze(TraceGraph.from_trace(trace))
        blocking_types = [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]]
        advisory_types = [gap["gap_type"] for gap in report.trace_improvement_report["advisory_gaps"]]

        self.assertEqual(report.metadata["analysis_outcome"], "root_found")
        self.assertEqual(report.metadata["termination_reason"], "queue_exhausted")
        self.assertEqual(report.metadata["termination_reasons_by_domain"]["trace_health"], "node_limit")
        self.assertNotIn("analysis_search_limit", blocking_types)
        self.assertIn("analysis_search_limit", advisory_types)

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
        self.assertNotIn(
            "defective_node_points_to_nondefective_upstream",
            [gap["gap_type"] for gap in report.trace_improvement_report["blocking_gaps"]],
        )

    def test_legacy_analyzer_never_judges_or_roots_external_evaluation_fact(self):
        class ExternalBoundaryJudge(FakeJudge):
            def __init__(self, judgments):
                super().__init__(judgments)
                self.evaluation_calls = []

            def judge_evaluation_assertion(self, **kwargs):
                self.evaluation_calls.append(kwargs["node"].ref)
                raise AssertionError("external evaluation fact reached evaluation Judge")

        trace = inject_external_evaluation_facts(
            {
                "manifest": {
                    "case_id": "legacy-external-root-exclusion",
                    "run_id": "legacy-external-root-exclusion-run",
                    "subject_revision": "git:abc123",
                    "subject_revision_provenance": {
                        "method": "case_trace_config",
                        "source": "CaseTraceConfig.subjectRevision",
                        "bound_at": "case_start",
                        "case_id": "legacy-external-root-exclusion",
                        "run_id": "legacy-external-root-exclusion-run",
                    },
                },
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "Cleanup preservation was omitted."},
                    }
                ],
                "dataflow_edges": [],
            },
            [
                {
                    "source": "terminalbench",
                    "scope": "process_sigint_behavior",
                    "subject_revision": "git:abc123",
                    "assertion": "cleanup completes",
                    "observation": "cleanup was interrupted",
                    "status": "failed",
                    "observed_at": "2026-07-21T12:00:00Z",
                    "evidence_refs": ["record:decision"],
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                }
            ],
        )
        fact_ref = "record:{0}".format(trace["records"][-1]["record_id"])
        judge = ExternalBoundaryJudge(
            {
                "record:decision": NodeJudgment(
                    node_ref="record:decision",
                    component="processor",
                    event_type="decision",
                    has_defect=True,
                    defect_status="present",
                    defect_type="missing_cleanup_preservation",
                    defect_reason="The decision omitted cleanup preservation.",
                    causal_role="defect_introduction",
                    is_root_cause=True,
                    confidence=0.95,
                )
            }
        )
        graph = TraceGraph.from_trace(trace)

        report = BackwardTaintAnalyzer(judge=judge).analyze(
            graph, start_refs=[fact_ref]
        )

        self.assertTrue(graph.analysis_start_eligible(fact_ref))
        self.assertEqual(judge.evaluation_calls, [])
        self.assertEqual(judge.calls, ["record:decision"])
        self.assertEqual(report.start_refs, [fact_ref])
        self.assertEqual(report.visited_order, [fact_ref, "record:decision"])
        self.assertFalse(report.node_judgments[fact_ref].is_root_cause)
        self.assertEqual(
            [item.node_ref for item in report.root_causes], ["record:decision"]
        )

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
        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.node_judgments["record:change_bad"].defect_type, "wrong_change")
        self.assertEqual(report.taint_paths, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
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

    def test_provider_circuit_during_root_confirmation_preserves_preliminary_judgment(self):
        class ConfirmationCircuitJudge(FakeJudge):
            def confirm_root(self, *, node, judgment, downstream_context, objective):
                raise JudgeProviderUnavailable("provider circuit opened during root confirmation")

        root_ref = "record:change_bad"
        judge = ConfirmationCircuitJudge(
            {
                root_ref: NodeJudgment(
                    node_ref=root_ref,
                    component="tool",
                    event_type="change",
                    defect_status="present",
                    has_defect=True,
                    defect_type="wrong_discount_change",
                    defect_reason="The authored change introduced the wrong discount behavior.",
                    causal_role="defect_introduction",
                    branch_relation="same_defect",
                    is_root_cause=True,
                    confidence=0.9,
                )
            }
        )

        report = BackwardTaintAnalyzer(judge=judge, max_depth=4).analyze(
            TraceGraph.from_trace(sample_trace()),
            start_refs=[root_ref],
            objective="Find why the discount change is wrong.",
        )

        self.assertEqual(report.root_causes, [])
        self.assertEqual(report.metadata["analysis_outcome"], "inconclusive")
        self.assertEqual(report.metadata["termination_reason"], "provider_unavailable")
        self.assertEqual(report.metadata["judge_error_count"], 1)
        self.assertEqual(
            report.metadata["judge_errors"][0]["stage"],
            "root_confirmation_provider_circuit_open",
        )
        self.assertEqual(report.node_judgments[root_ref].defect_status, "present")
        self.assertEqual(report.node_judgments[root_ref].causal_role, "defect_introduction")


class NodeJudgmentTest(unittest.TestCase):
    def test_decision_compaction_preserves_authored_semantics_before_context_collections(self):
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="processor",
            event_type="decision",
            data={
                "decision_type": "reasoning_block",
                "chosen_action": "search_installed_package",
                "intent": "model requested repository exploration",
                "rationale": (
                    "The missing interfaces are already understood, but I will search the installed "
                    "package for an unavailable reference implementation before writing code."
                ),
                "selected_context_refs": [f"record:context_{index}" for index in range(200)],
                "message_transforms": [{"name": "transform", "payload": "x" * 4000}],
                "hydrated_artifacts": [{"content": "y" * 16000}],
            },
        )

        compacted = node.compact(max_chars=1600)

        self.assertTrue(compacted["truncated"])
        self.assertEqual(compacted["data"]["decision_type"], "reasoning_block")
        self.assertEqual(compacted["data"]["chosen_action"], "search_installed_package")
        self.assertIn("missing interfaces are already understood", compacted["data"]["rationale"])

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


class JudgmentCacheTest(unittest.TestCase):
    def test_persists_judgment_and_recovers_after_corrupt_tail(self):
        try:
            from trace_attribution.cache import JudgmentCache
        except ModuleNotFoundError as exc:
            self.fail(f"judgment cache module is missing: {exc}")
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="processor",
            event_type="decision",
        )
        judgment = NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_status="present",
            defect_type="wrong_plan",
            defect_reason="The plan abandons the required implementation.",
            causal_role="defect_introduction",
            is_root_cause=True,
            confidence=0.91,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            cache_path = Path(tempdir) / "judge-cache.jsonl"
            cache = JudgmentCache(cache_path)
            cache.put(
                key="cache-key",
                stage="node_judgment",
                model="test-model",
                node=node,
                judgment=judgment,
            )
            with cache_path.open("a", encoding="utf-8") as handle:
                handle.write("{corrupt-tail\n")

            reopened = JudgmentCache(cache_path)
            restored = reopened.get(key="cache-key", node=node)

        self.assertEqual(restored, judgment)
        self.assertEqual(reopened.stats()["loaded_entries"], 1)
        self.assertEqual(reopened.stats()["corrupt_entries"], 1)
        self.assertEqual(reopened.stats()["hits"], 1)

    def test_cache_key_changes_with_request_semantics(self):
        try:
            from trace_attribution.cache import build_judge_cache_key
        except ModuleNotFoundError as exc:
            self.fail(f"judgment cache module is missing: {exc}")
        base = {
            "stage": "node_judgment",
            "model": "test-model",
            "system": "judge system",
            "messages": [{"role": "user", "content": "objective A"}],
            "max_tokens": 1024,
            "thinking_config": None,
            "prompt_schema_version": "causal-judge-v1",
        }

        key = build_judge_cache_key(**base)

        for field, changed in (
            ("model", "other-model"),
            ("messages", [{"role": "user", "content": "objective B"}]),
            ("prompt_schema_version", "causal-judge-v2"),
        ):
            with self.subTest(field=field):
                candidate = dict(base)
                candidate[field] = changed
                self.assertNotEqual(key, build_judge_cache_key(**candidate))


class ClaudeJudgeClientTest(unittest.TestCase):
    def test_provider_circuit_opens_after_three_consecutive_connection_errors(self):
        try:
            from trace_attribution.errors import JudgeProviderError, JudgeProviderUnavailable
        except ModuleNotFoundError as exc:
            self.fail(f"provider error module is missing: {exc}")

        class FailingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise ConnectionError("provider connection dropped")

        messages = FailingMessages()
        client = object.__new__(ClaudeJudgeClient)
        client.timeout_seconds = None
        client.thinking_config = None
        client.model = "test-model"
        client.client = types.SimpleNamespace(messages=messages)
        client.request_count = 0
        client.provider_error_threshold = 3
        client.consecutive_provider_errors = 0
        client.provider_circuit_open = False

        with self.assertRaises(JudgeProviderError):
            client._create_message_text(system="system", messages=[], max_tokens=8)
        with self.assertRaises(JudgeProviderError):
            client._create_message_text(system="system", messages=[], max_tokens=8)
        with self.assertRaises(JudgeProviderUnavailable):
            client._create_message_text(system="system", messages=[], max_tokens=8)
        with self.assertRaises(JudgeProviderUnavailable):
            client._create_message_text(system="system", messages=[], max_tokens=8)

        self.assertEqual(messages.calls, 3)
        self.assertEqual(client.request_count, 3)
        self.assertTrue(client.provider_circuit_open)

    def test_successful_request_resets_consecutive_provider_errors(self):
        class SuccessfulMessages:
            def create(self, **kwargs):
                return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

        client = object.__new__(ClaudeJudgeClient)
        client.timeout_seconds = None
        client.thinking_config = None
        client.model = "test-model"
        client.client = types.SimpleNamespace(messages=SuccessfulMessages())
        client.request_count = 0
        client.provider_error_threshold = 3
        client.consecutive_provider_errors = 2
        client.provider_circuit_open = False

        text = client._create_message_text(system="system", messages=[], max_tokens=8)

        self.assertEqual(text, "ok")
        self.assertEqual(client.consecutive_provider_errors, 0)
        self.assertFalse(client.provider_circuit_open)

    def test_reuses_cached_node_judgment_without_second_provider_request(self):
        from trace_attribution.cache import JudgmentCache

        payload = {
            "node_ref": "record:decision",
            "component": "processor",
            "event_type": "decision",
            "defect_status": "absent",
            "has_defect": False,
            "defect_type": "",
            "defect_reason": "The decision is consistent with the task objective.",
            "causal_role": "non_defective",
            "branch_relation": "unrelated",
            "influenced_by": [],
            "is_root_cause": False,
            "severity": "low",
            "confidence": 0.95,
            "model_notes": "",
        }

        class FakeMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=json.dumps(payload))]
                )

        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="processor",
            event_type="decision",
            data={"rationale": "Implement the requested behavior."},
        )
        with tempfile.TemporaryDirectory() as tempdir:
            messages = FakeMessages()
            client = object.__new__(ClaudeJudgeClient)
            client.timeout_seconds = None
            client.thinking_config = None
            client.model = "test-model"
            client.client = types.SimpleNamespace(messages=messages)
            client.max_tokens = 4096
            client.repair_max_tokens = 1024
            client.request_count = 0
            client.cache = JudgmentCache(Path(tempdir) / "judge-cache.jsonl")

            first = client.judge_node(
                node=node,
                upstream_nodes=[],
                downstream_context=["record:observed event_type=case.observed_defect"],
                objective="Implement the requested behavior.",
            )
            second = client.judge_node(
                node=node,
                upstream_nodes=[],
                downstream_context=["record:observed event_type=case.observed_defect"],
                objective="Implement the requested behavior.",
            )

        self.assertEqual(first, second)
        self.assertEqual(messages.calls, 1)
        self.assertEqual(client.request_count, 1)
        self.assertEqual(client.cache.stats()["hits"], 1)
        self.assertEqual(client.cache.stats()["writes"], 1)

    def test_reuses_cached_root_confirmation(self):
        from trace_attribution.cache import JudgmentCache

        payload = {
            "node_ref": "record:action",
            "confirmation": "confirmed",
            "exact_semantic_excerpt": "Skip the required implementation.",
            "current_node_would_cause_defect_if_executed_exactly": True,
            "reason": "The action explicitly skips the required implementation.",
            "confidence": 0.94,
        }

        class FakeMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(text=json.dumps(payload))]
                )

        node = TraceNode(
            ref="record:action",
            record_id="action",
            component="processor",
            event_type="decision",
            data={"rationale": "Skip the required implementation."},
        )
        judgment = NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_status="present",
            defect_type="skipped_implementation",
            defect_reason="The action skips required work.",
            causal_role="defect_introduction",
            is_root_cause=True,
            confidence=0.9,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            messages = FakeMessages()
            client = object.__new__(ClaudeJudgeClient)
            client.timeout_seconds = None
            client.thinking_config = None
            client.model = "test-model"
            client.client = types.SimpleNamespace(messages=messages)
            client.max_tokens = 4096
            client.repair_max_tokens = 1024
            client.request_count = 0
            client.cache = JudgmentCache(Path(tempdir) / "judge-cache.jsonl")

            first = client.confirm_root(
                node=node,
                judgment=judgment,
                downstream_context=["record:observed event_type=case.observed_defect"],
                objective="Implement the requested behavior.",
            )
            second = client.confirm_root(
                node=node,
                judgment=judgment,
                downstream_context=["record:observed event_type=case.observed_defect"],
                objective="Implement the requested behavior.",
            )

        self.assertEqual(first, second)
        self.assertEqual(messages.calls, 1)
        self.assertEqual(client.cache.stats()["hits"], 1)
        self.assertEqual(client.cache.stats()["writes"], 1)

    def test_judgment_prompt_includes_structured_causal_context(self):
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="processor",
            event_type="decision",
            data={"rationale": "Keep searching instead of implementing."},
        )
        judgment_context = {
            "context_version": "1.0",
            "active_defect": {
                "fingerprint": "defect-123",
                "expected": "Implement the change.",
                "actual": "No change was produced.",
                "mechanism": "empty_patch",
            },
            "incoming_edges": [
                {
                    "from_ref": "record:evidence",
                    "to_ref": "record:decision",
                    "relation": "motivated_by_evidence",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                }
            ],
            "downstream_judgments": [
                {
                    "node_ref": "record:observed",
                    "defect_status": "present",
                    "causal_role": "defect_evidence",
                }
            ],
        }

        prompt = json.loads(
            build_judgment_prompt(
                node=node,
                upstream_nodes=[],
                downstream_context=["record:observed event_type=case.observed_defect"],
                objective="Find the root cause.",
                judgment_context=judgment_context,
            )
        )

        self.assertEqual(prompt["causal_judgment_context"]["active_defect"]["fingerprint"], "defect-123")
        self.assertEqual(
            prompt["causal_judgment_context"]["incoming_edges"][0]["relation"],
            "motivated_by_evidence",
        )
        self.assertIn(
            "Treat causal_judgment_context edge relations as recorded provenance",
            prompt["rules"],
        )

    def test_evaluation_assertion_reuses_node_judgment_pipeline(self):
        client = object.__new__(ClaudeJudgeClient)
        expected = NodeJudgment(
            node_ref="record:gap",
            component="evaluation",
            event_type="case.quality_gap",
            has_defect=False,
            defect_status="absent",
            defect_reason="The assertion is contradicted by the supplied facts.",
        )
        client.judge_node = mock.Mock(return_value=expected)
        node = TraceNode(
            ref="record:gap",
            record_id="gap",
            component="evaluation",
            event_type="case.quality_gap",
        )

        actual = client.judge_evaluation_assertion(
            node=node,
            upstream_nodes=[],
            downstream_context=["record:gap"],
            objective="Validate the asserted quality gap.",
        )

        self.assertIs(actual, expected)
        client.judge_node.assert_called_once()

    def test_counts_each_actual_model_request(self):
        class FakeMessages:
            def create(self, **kwargs):
                return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

        client = object.__new__(ClaudeJudgeClient)
        client.timeout_seconds = None
        client.thinking_config = None
        client.model = "fake-model"
        client.client = types.SimpleNamespace(messages=FakeMessages())
        client.request_count = 0

        text = client._create_message_text(
            system="system",
            messages=[{"role": "user", "content": "prompt"}],
            max_tokens=8,
        )

        self.assertEqual(text, "ok")
        self.assertEqual(client.request_count, 1)

    def test_rejects_offline_progress_aggregate_as_root_cause(self):
        node = TraceNode(
            ref="progress_episode:progress_1",
            record_id="progress_1",
            component="progress",
            event_type="progress.episode",
            data={"offline_only": True},
        )

        with self.assertRaisesRegex(ValueError, "progress episode"):
            validate_judgment_payload(
                {
                    "node_ref": node.ref,
                    "component": node.component,
                    "event_type": node.event_type,
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "no_delivery_progress",
                    "defect_reason": "The aggregate summarizes no delivery progress.",
                    "causal_role": "defect_introduction",
                    "branch_relation": "same_defect",
                    "influenced_by": [],
                    "is_root_cause": True,
                    "confidence": 0.9,
                },
                node=node,
                allowed_upstream_refs=set(),
            )

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

    def test_rejects_defect_evidence_role_for_authored_action_decision(self):
        node = TraceNode(
            ref="record:test_action",
            record_id="test_action",
            component="processor",
            event_type="decision",
            data={"decision_type": "llm_tool_call"},
        )

        with self.assertRaisesRegex(ValueError, "authored action"):
            validate_judgment_payload(
                {
                    "node_ref": node.ref,
                    "component": node.component,
                    "event_type": node.event_type,
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "wrong_test_contract",
                    "defect_reason": "The authored test script uses the wrong input contract.",
                    "causal_role": "defect_evidence",
                    "branch_relation": "outcome_evidence",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.9,
                },
                node=node,
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

    def test_rejects_propagation_relation_whose_reason_only_describes_motivation(self):
        with self.assertRaisesRegex(ValueError, "motivation/evidence"):
            validate_judgment_payload(
                {
                    "node_ref": "record:scope_change",
                    "component": "tool",
                    "event_type": "change",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "out_of_scope_change",
                    "defect_reason": "The change edits compatibility code outside the task scope.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [
                        {
                            "upstream_ref": "record:environment_failure",
                            "reason": "The failed verification exposed Python 3.9 and motivated this edit.",
                            "relation": "defect_propagated_from",
                            "confidence": 0.9,
                        }
                    ],
                    "is_root_cause": False,
                    "confidence": 0.9,
                },
                allowed_upstream_refs={"record:environment_failure"},
            )

    def test_rejects_decision_propagation_from_duplicate_llm_generation_envelope(self):
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="processor",
            event_type="decision",
            data={"decision_type": "reasoning_block"},
        )

        with self.assertRaisesRegex(ValueError, "generation envelope"):
            validate_judgment_payload(
                {
                    "node_ref": node.ref,
                    "component": node.component,
                    "event_type": node.event_type,
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "excessive_exploration",
                    "defect_reason": "The decision continues exploring after understanding the task.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [
                        {
                            "upstream_ref": "record:llm_call",
                            "reason": "The LLM call produced the reasoning that already exhibited excessive exploration.",
                            "relation": "defect_propagated_from",
                            "confidence": 0.8,
                        }
                    ],
                    "is_root_cause": False,
                    "confidence": 0.9,
                },
                node=node,
                allowed_upstream_refs={"record:llm_call"},
            )

    def test_rejects_influence_ref_outside_supplied_upstream_nodes(self):
        with self.assertRaisesRegex(ValueError, "not in the supplied upstream node set"):
            validate_judgment_payload(
                {
                    "node_ref": "record:scope_change",
                    "component": "tool",
                    "event_type": "change",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "out_of_scope_change",
                    "defect_reason": "The change edits compatibility code outside the task scope.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [
                        {
                            "upstream_ref": "record:invented_decision",
                            "reason": "An unspecified earlier decision introduced the defect.",
                            "relation": "defect_propagated_from",
                            "confidence": 0.9,
                        }
                    ],
                    "is_root_cause": False,
                    "confidence": 0.9,
                },
                allowed_upstream_refs={"record:environment_failure"},
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

    def test_cli_exposes_default_resume_cache_and_provider_threshold(self):
        args = parse_args(["--trace", "trace.json", "--out", "/tmp/case.attribution.json"])
        explicit = parse_args(
            [
                "--trace",
                "trace.json",
                "--out",
                "/tmp/case.attribution.json",
                "--judge-cache",
                "/tmp/shared-cache.jsonl",
                "--provider-error-threshold",
                "5",
            ]
        )

        self.assertEqual(
            judge_cache_output_path(Path(args.out), args.judge_cache),
            Path("/tmp/case.attribution.judge-cache.jsonl"),
        )
        self.assertEqual(args.provider_error_threshold, 3)
        self.assertEqual(
            judge_cache_output_path(Path(explicit.out), explicit.judge_cache),
            Path("/tmp/shared-cache.jsonl"),
        )
        self.assertEqual(explicit.provider_error_threshold, 5)

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
        self.assertIn("evaluate answer quality only", rules)
        self.assertIn("pre-existing requirement/code conflict", rules)
        self.assertIn("does not make the response node defective", rules)
        self.assertIn("repository problem or missing implementation is a task precondition", rules)
        self.assertIn("failure to act, repeated exploration, or empty-patch defect", rules)
        self.assertIn("rationale and executable arguments over generic orchestration labels", rules)

    def test_judgment_prompt_identifies_authored_action_semantics(self):
        prompt = build_judgment_prompt(
            node=TraceNode(
                ref="record:self_test_action",
                record_id="self_test_action",
                component="processor",
                event_type="decision",
                data={
                    "decision_type": "llm_tool_call",
                    "chosen_action": "bash",
                    "rationale": "python - <<'PY'\nassert parse_declaration('struct MyStruct')\nPY",
                },
            ),
            upstream_nodes=[],
            downstream_context=[
                "record:observed event_type=case.observed_defect; failure_type=self_test_contract"
            ],
            objective="Find why the self-test missed the interface contract.",
        )
        payload = json.loads(prompt)

        self.assertEqual(payload["current_node_semantic_role"], "authored_agent_action")
        self.assertEqual(payload["current_node"]["data"]["decision_type"], "llm_tool_call")
        rules = " ".join(payload["rules"])
        self.assertIn("A generic intent to run a test", rules)
        self.assertIn("A correct current plan is not defective merely because a later action", rules)

    def test_root_confirmation_prompt_requires_node_local_evidence_and_counterfactual(self):
        node = TraceNode(
            ref="record:plan",
            record_id="plan",
            component="processor",
            event_type="decision",
            data={
                "decision_type": "reasoning_block",
                "rationale": "Parse the declaration body without repeating its directive keyword.",
            },
        )
        judgment = NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_status="present",
            defect_type="repeated_directive_keyword",
            defect_reason="The plan allegedly introduces the parser defect.",
            causal_role="defect_introduction",
            branch_relation="same_defect",
            is_root_cause=True,
        )

        payload = json.loads(
            build_root_confirmation_prompt(
                node=node,
                judgment=judgment,
                downstream_context=["record:observed repeated_directive_keyword"],
                objective="Locate the parser defect introduction.",
            )
        )

        rules = " ".join(payload["rules"])
        self.assertIn("exact excerpt", rules)
        self.assertIn("counterfactual", rules)
        self.assertIn("pre-existing repository defect", rules)
        with self.assertRaisesRegex(ValueError, "not present in the current node"):
            validate_root_confirmation_payload(
                {
                    "node_ref": node.ref,
                    "confirmation": "confirmed",
                    "exact_semantic_excerpt": "Require the keyword again.",
                    "current_node_would_cause_defect_if_executed_exactly": True,
                    "reason": "The plan requires the keyword.",
                    "confidence": 0.9,
                },
                node=node,
            )
        with self.assertRaisesRegex(ValueError, "counterfactual"):
            validate_root_confirmation_payload(
                {
                    "node_ref": node.ref,
                    "confirmation": "confirmed",
                    "exact_semantic_excerpt": "without repeating its directive keyword",
                    "current_node_would_cause_defect_if_executed_exactly": False,
                    "reason": "The plan would not cause the defect if followed.",
                    "confidence": 0.9,
                },
                node=node,
            )

    def test_root_confirmation_matches_excerpt_across_hydrated_artifact_whitespace(self):
        node = TraceNode(
            ref="record:action",
            record_id="action",
            component="processor",
            event_type="decision",
            data={
                "decision_type": "llm_tool_call",
                "hydrated_artifacts": [
                    {
                        "label": "decision.rationale",
                        "content": "if objectType == 'struct':\n    require_keyword()",
                    }
                ],
            },
        )

        validate_root_confirmation_payload(
            {
                "node_ref": node.ref,
                "confirmation": "confirmed",
                "exact_semantic_excerpt": "if objectType == 'struct': require_keyword()",
                "current_node_would_cause_defect_if_executed_exactly": True,
                "reason": "The authored action requires the repeated keyword.",
                "confidence": 0.95,
            },
            node=node,
        )

    def test_root_confirmation_accepts_a_grounded_paraphrase_with_exact_code_identifiers(self):
        node = TraceNode(
            ref="record:action",
            record_id="action",
            component="processor",
            event_type="decision",
            data={
                "decision_type": "llm_tool_call",
                "rationale": "if objectType == 'struct':\n    declaration = self._parse_struct_keyword()",
            },
        )

        validate_root_confirmation_payload(
            {
                "node_ref": node.ref,
                "confirmation": "confirmed",
                "exact_semantic_excerpt": "objectType struct dispatches to _parse_struct_keyword",
                "current_node_would_cause_defect_if_executed_exactly": True,
                "reason": "The authored action requires the struct keyword in the declaration body.",
                "confidence": 0.95,
            },
            node=node,
        )

    def test_root_confirmation_repairs_an_inconsistent_causation_answer_once(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                payload = {
                    "node_ref": "record:action",
                    "confirmation": "confirmed",
                    "exact_semantic_excerpt": "Require the struct keyword in the declaration body.",
                    "current_node_would_cause_defect_if_executed_exactly": len(calls) > 1,
                    "reason": "The authored action requires the repeated keyword.",
                    "confidence": 0.95,
                }
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(payload))])

        class FakeAnthropic:
            def __init__(self, **kwargs):
                self.messages = FakeMessages()

        fake_module = types.SimpleNamespace(Anthropic=FakeAnthropic)
        with mock.patch.dict(sys.modules, {"anthropic": fake_module}):
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
                client = ClaudeJudgeClient(model="fake-model")
                client.timeout_seconds = None

        node = TraceNode(
            ref="record:action",
            record_id="action",
            component="processor",
            event_type="decision",
            data={
                "decision_type": "llm_tool_call",
                "rationale": "Require the struct keyword in the declaration body.",
            },
        )
        judgment = NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_status="present",
            defect_type="repeated_directive_keyword",
            defect_reason="The action requires a repeated keyword.",
            causal_role="defect_introduction",
            branch_relation="same_defect",
            is_root_cause=True,
            confidence=0.9,
        )

        confirmed = client.confirm_root(
            node=node,
            judgment=judgment,
            downstream_context=["record:observed repeated_directive_keyword"],
            objective="Locate the parser defect introduction.",
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(confirmed.defect_status, "present")
        self.assertTrue(confirmed.is_root_cause)
        repair_payload = json.loads(calls[1]["messages"][0]["content"])
        self.assertIn("requires a true", repair_payload["validation_error"])

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

    def test_repair_receives_specific_judgment_validation_error(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    payload = {
                        "node_ref": "record:plan",
                        "component": "processor",
                        "event_type": "decision",
                        "defect_status": "present",
                        "has_defect": True,
                        "defect_type": "wrong_test_contract",
                        "defect_reason": "The plan carries the later self-test defect.",
                        "causal_role": "defect_propagation",
                        "branch_relation": "same_defect",
                        "influenced_by": [],
                        "is_root_cause": False,
                        "confidence": 0.7,
                    }
                else:
                    payload = {
                        "node_ref": "record:plan",
                        "component": "processor",
                        "event_type": "decision",
                        "defect_status": "absent",
                        "has_defect": False,
                        "defect_type": "",
                        "defect_reason": "The plan only states an intent to run a test.",
                        "causal_role": "non_defective",
                        "branch_relation": "unrelated",
                        "influenced_by": [],
                        "is_root_cause": False,
                        "confidence": 0.9,
                    }
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(payload))])

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
                ref="record:plan",
                record_id="plan",
                component="processor",
                event_type="decision",
                data={
                    "decision_type": "reasoning_block",
                    "chosen_action": "continue_processing_stream",
                    "rationale": "Run a comprehensive test.",
                },
            ),
            upstream_nodes=[],
            downstream_context=["record:observed event_type=case.observed_defect"],
            objective="Find why the self-test used the wrong contract.",
        )

        repair_payload = json.loads(calls[1]["messages"][0]["content"])
        self.assertIn("defect_propagation requires a defect_propagated_from influence", repair_payload["validation_error"])
        self.assertEqual(judgment.defect_status, "absent")
        self.assertEqual(judgment.causal_role, "non_defective")

    def test_exhausted_schema_repairs_return_auditable_unknown_judgment(self):
        calls = []

        class FakeMessages:
            def create(self, **kwargs):
                calls.append(kwargs)
                payload = {
                    "node_ref": "record:plan",
                    "component": "processor",
                    "event_type": "decision",
                    "defect_status": "present",
                    "has_defect": True,
                    "defect_type": "wrong_test_contract",
                    "defect_reason": "The plan carries the later self-test defect.",
                    "causal_role": "defect_propagation",
                    "branch_relation": "same_defect",
                    "influenced_by": [],
                    "is_root_cause": False,
                    "confidence": 0.7,
                }
                return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(payload))])

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
                ref="record:plan",
                record_id="plan",
                component="processor",
                event_type="decision",
                data={"decision_type": "reasoning_block", "rationale": "Run a comprehensive test."},
            ),
            upstream_nodes=[],
            downstream_context=["record:observed event_type=case.observed_defect"],
            objective="Find why the self-test used the wrong contract.",
        )

        self.assertEqual(len(calls), 3)
        self.assertEqual(judgment.defect_status, "unknown")
        self.assertEqual(judgment.causal_role, "unknown")
        self.assertFalse(judgment.is_root_cause)
        self.assertIn("schema repair exhausted", judgment.model_notes)

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
