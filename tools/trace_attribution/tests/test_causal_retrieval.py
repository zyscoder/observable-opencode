import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_state import AttributionHypothesis, CausalStepJudgment, DefectState, HypothesisEvidence
from trace_attribution.causal_retrieval import (
    SemanticPredecessorRetriever,
    root_candidate_eligible,
)
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


def external_fact_record(
    record_id: str,
    *,
    status: str,
    revision_status: str,
    decisive: bool = False,
):
    return {
        "record_id": record_id,
        "component": "evaluation",
        "event_type": "external.evaluation_fact",
        "status": status,
        "data": {
            "assertion": "The namespace contract remains compatible.",
            "observation": "The namespace contract evaluation completed.",
            "scope": "namespace_contract",
            "status": status,
            "subject_revision": "git:abc123",
            "trace_revision": (
                "git:abc123" if revision_status == "matched" else "git:different"
            ),
            "revision_status": revision_status,
            "revision_provenance_status": "valid",
            "provenance": {"method": "benchmark_grader", "version": "1.0"},
            "eligible_for_decisive_judgment": decisive,
            "offline_only": True,
        },
    }


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


def trace_with_temporal_variants():
    records = []
    for record_id in ("temporal_true", "temporal_omitted", "temporal_false"):
        records.append(
            {
                "record_id": record_id,
                "component": "tool",
                "event_type": "tool.result",
                "data": {"text": "Temporal availability is not causal lineage."},
            }
        )
    records.append(
        {
            "record_id": "decision",
            "component": "processor",
            "event_type": "decision",
            "data": {"rationale": "Make a decision."},
        }
    )
    return {
        "case_id": "temporal-variants-case",
        "records": records,
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "temporal_true"},
                "to": {"type": "record", "id": "decision"},
                "relation": "temporal_availability",
                "evidence_type": "temporal_inferred",
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "temporal_omitted"},
                "to": {"type": "record", "id": "decision"},
                "relation": "available_to_next_request",
                "edge_origin": "offline.temporal_reconstruction",
            },
            {
                "from": {"type": "record", "id": "temporal_false"},
                "to": {"type": "record", "id": "decision"},
                "relation": "temporal_availability",
                "evidence_type": "temporal_inferred",
                "eligible_for_attribution": False,
            },
        ],
    }


def trace_with_all_concrete_siblings():
    records = []
    for record_id, component, event_type in (
        ("prompt", "prompt", "message.input"),
        ("context", "context", "context.snapshot"),
        ("llm", "llm", "llm.call"),
        ("subagent", "subagent", "subagent.note"),
        ("harness", "harness", "harness.check"),
        ("tool", "tool", "tool.result"),
        ("result", "result", "response.output"),
    ):
        records.append(
            {
                "record_id": record_id,
                "component": component,
                "event_type": event_type,
                "data": {"metadata": {"messageID": "msg_shared"}, "text": record_id},
            }
        )
    records.append(
        {
            "record_id": "decision",
            "component": "processor",
            "event_type": "decision",
            "data": {"metadata": {"messageID": "msg_shared"}, "rationale": "Decide after all facts."},
        }
    )
    return {"case_id": "all-concrete-siblings", "records": records}


def trace_with_unresolved_evidence_and_artifact():
    return {
        "case_id": "grounded-context-case",
        "artifacts": [
            {
                "artifact_id": "missing_payload",
                "kind": "text",
                "path": "artifacts/missing-payload.txt",
            }
        ],
        "records": [
            {
                "record_id": "prompt",
                "component": "prompt",
                "event_type": "message.input",
                "data": {"payload_ref": "artifact:missing_payload", "text": "Evidence from a missing artifact."},
            },
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "data": {"rationale": "Use prompt evidence."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "prompt"},
                "to": {"type": "record", "id": "decision"},
                "relation": "prompt_informed_decision",
                "evidence_type": "confirmed",
                "evidence_refs": ["record:missing_evidence"],
                "eligible_for_attribution": True,
            }
        ],
    }


def trace_with_same_message_temporal_candidate():
    return {
        "case_id": "same-message-temporal-candidate",
        "records": [
            {
                "record_id": "temporal_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"metadata": {"messageID": "msg_shared"}, "text": "Only temporally available."},
            },
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "data": {"metadata": {"messageID": "msg_shared"}, "rationale": "Choose a response."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "temporal_result"},
                "to": {"type": "record", "id": "decision"},
                "relation": "temporal_availability",
                "evidence_type": "temporal_inferred",
                "eligible_for_attribution": True,
            }
        ],
    }


def trace_with_artifact_evidence():
    return {
        "case_id": "artifact-reference-case",
        "artifacts": [
            {
                "artifact_id": "known_payload",
                "kind": "text",
                "path": "artifacts/known-payload.txt",
            },
            {
                "artifact_id": "missing_payload",
                "kind": "text",
                "path": "artifacts/missing-payload.txt",
            },
        ],
        "records": [
            {
                "record_id": "prompt",
                "component": "prompt",
                "event_type": "message.input",
                "data": {"payload_ref": "artifact:known_payload", "text": "Artifact-backed prompt."},
            },
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "data": {"rationale": "Use artifact evidence."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "prompt"},
                "to": {"type": "record", "id": "decision"},
                "relation": "prompt_informed_decision",
                "evidence_type": "confirmed",
                "evidence_refs": ["artifact:known_payload", "artifact:missing_payload", "unknown_payload"],
                "eligible_for_attribution": True,
            }
        ],
    }


class CausalRetrievalTest(unittest.TestCase):
    def test_canonical_root_contract_excludes_external_evaluation_facts(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "canonical-root-contract",
                "records": [
                    {
                        "record_id": "candidate",
                        "component": "evaluation",
                        "event_type": "external.evaluation_fact",
                    }
                ],
            }
        )

        self.assertFalse(root_candidate_eligible(graph.nodes["record:candidate"]))

    def test_retriever_bounds_provenance_envelopes_and_keeps_the_semantic_match(self):
        records = []
        edges = []
        for index, event_type in enumerate(
            (
                "prompt.assembly",
                "message.input",
                "context.transform",
                "llm.call",
                "task.loop",
            )
        ):
            record_id = "wrapper_{0}".format(index)
            records.append(
                {
                    "record_id": record_id,
                    "component": "context",
                    "event_type": event_type,
                    "data": {
                        "text": (
                            "Process SIGINT can interrupt already-started async cleanup."
                            if index == 3
                            else "Generic transport envelope {0}.".format(index)
                        )
                    },
                }
            )
            edges.append(
                {
                    "from": {"type": "record", "id": record_id},
                    "to": {"type": "record", "id": "decision"},
                    "relation": "record_source",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                }
            )
        records.append(
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "data": {"rationale": "Choose a cancellation implementation."},
            }
        )
        graph = TraceGraph.from_trace(
            {"case_id": "bounded-provenance", "records": records, "dataflow_edges": edges}
        )
        defect_state = DefectState.create(
            label="started_async_cleanup_interrupted_by_process_sigint",
            expected="Already-started async cleanup completes after process SIGINT.",
            actual="Process SIGINT interrupts already-started async cleanup.",
            mechanism="The cancellation assumption ignores signal timing.",
            scope="task_cancellation",
        )

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
        )

        wrapper_refs = [item.ref for item in candidates if item.node.event_type in {
            "prompt.assembly", "message.input", "context.transform", "llm.call", "task.loop"
        }]
        self.assertLessEqual(len(wrapper_refs), 2)
        self.assertIn("record:wrapper_3", wrapper_refs)

    def test_analysis_navigation_edge_is_auditable_and_non_temporal(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "navigation-edge",
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "A candidate assumption."},
                    },
                    {
                        "record_id": "progress",
                        "component": "progress",
                        "event_type": "progress.episode",
                        "data": {"offline_only": True},
                    },
                ],
            }
        )

        graph.add_offline_navigation_edge(
            "record:decision",
            "record:progress",
            evidence_refs=["record:decision"],
            confidence=0.9,
        )

        edge = graph.edge_context("record:decision", "record:progress")[0]
        self.assertEqual(edge["relation"], "semantic_navigation_route")
        self.assertEqual(edge["evidence_type"], "semantic_inferred")
        self.assertTrue(edge["eligible_for_attribution"])
        self.assertEqual(graph.upstream_refs("record:progress"), ["record:decision"])

    def test_progress_shortlist_recovers_older_semantically_relevant_delivery(self):
        records = []
        for index in range(1, 8):
            relevant = index == 1
            records.extend(
                [
                    {
                        "record_id": "reason_{0}".format(index),
                        "component": "processor",
                        "event_type": "decision",
                        "timestamp": "2026-07-21T10:00:{0:02d}.000Z".format(index * 2),
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": (
                                "Assume process SIGINT cancellation preserves already-started async cleanup."
                                if relevant
                                else "Perform unrelated import cleanup edit {0}.".format(index)
                            ),
                            "metadata": {
                                "sessionID": "ses_progress",
                                "messageID": "msg_{0}".format(index),
                            },
                        },
                    },
                    {
                        "record_id": "edit_{0}".format(index),
                        "component": "processor",
                        "event_type": "decision",
                        "timestamp": "2026-07-21T10:00:{0:02d}.500Z".format(index * 2),
                        "data": {
                            "decision_type": "llm_tool_call",
                            "chosen_action": "edit",
                            "rationale": "Apply edit {0}.".format(index),
                            "metadata": {
                                "sessionID": "ses_progress",
                                "messageID": "msg_{0}".format(index),
                            },
                        },
                    },
                ]
            )
        records.append(
            {
                "record_id": "final_reason",
                "component": "processor",
                "event_type": "decision",
                "timestamp": "2026-07-21T10:00:30.000Z",
                "data": {
                    "decision_type": "reasoning_block",
                    "rationale": "All tests pass after import cleanup.",
                    "metadata": {
                        "sessionID": "ses_progress",
                        "messageID": "msg_8",
                    },
                },
            }
        )
        graph = TraceGraph.from_trace(
            {"case_id": "progress-semantic-history", "records": records}
        )
        latest = max(
            (node for node in graph.nodes.values() if node.event_type == "progress.episode"),
            key=lambda node: int(node.data.get("chronology_index") or 0),
        )
        defect_state = DefectState.create(
            label="process_sigint_cleanup_interrupted",
            expected="Already-started async cleanup completes after process SIGINT cancellation.",
            actual="Process SIGINT interrupts already-started async cleanup.",
            mechanism="The cancellation assumption is incorrect.",
            scope="task_cancellation",
        )
        hypothesis = AttributionHypothesis.create(
            claim="An earlier cancellation decision introduced the cleanup defect.",
            candidate_root_ref=latest.ref,
            defect_state=defect_state,
        )

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref=latest.ref,
            defect_state=defect_state,
            hypothesis=hypothesis,
            limit=8,
        )

        by_ref = {item.ref: item for item in candidates}
        self.assertIn("record:reason_1", by_ref)
        self.assertEqual(by_ref["record:reason_1"].source, "progress_window")
        self.assertLessEqual(len(candidates), 8)
        self.assertTrue(all(item.source == "progress_window" for item in candidates))

    def test_grounding_candidates_are_not_promoted_to_causal_predecessors(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "candidate-grounding-is-not-dataflow",
                "records": [
                    {
                        "record_id": "considered",
                        "component": "tool",
                        "event_type": "tool.result",
                        "data": {"text": "A considered but unconsumed candidate."},
                    },
                    {
                        "record_id": "selected",
                        "component": "tool",
                        "event_type": "tool.result",
                        "data": {"text": "The selected direct support."},
                    },
                    {
                        "record_id": "claim",
                        "component": "result",
                        "event_type": "response.claim",
                        "source_refs": ["record:selected"],
                        "data": {
                            "text": "The selected support justifies this claim.",
                            "grounding_candidate_refs": ["record:considered"],
                        },
                    },
                ],
            }
        )
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:claim",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
        )

        self.assertEqual([item.ref for item in candidates], ["record:selected"])

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

    def test_temporal_edges_are_advisory_for_every_eligibility_variant(self):
        graph = TraceGraph.from_trace(trace_with_temporal_variants())
        defect_state = sample_defect_state()
        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
        )
        context = build_recursive_judgment_context(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            candidates=candidates,
            downstream_path=["record:decision"],
        )

        temporal_refs = {"record:temporal_true", "record:temporal_omitted", "record:temporal_false"}
        self.assertFalse(temporal_refs & {item.ref for item in candidates})
        self.assertEqual(temporal_refs, {item["from_ref"] for item in context["temporal_adjacency"]})
        self.assertTrue(
            next(item for item in context["temporal_adjacency"] if item["from_ref"] == "record:temporal_true")[
                "recorded_eligible_for_attribution"
            ]
        )
        self.assertIsNone(
            next(item for item in context["temporal_adjacency"] if item["from_ref"] == "record:temporal_omitted")[
                "recorded_eligible_for_attribution"
            ]
        )

    def test_sibling_retrieval_accepts_every_concrete_recorded_node_type(self):
        graph = TraceGraph.from_trace(trace_with_all_concrete_siblings())
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=16,
        )

        self.assertEqual(
            {"record:prompt", "record:context", "record:llm", "record:subagent", "record:harness", "record:tool", "record:result"},
            {item.ref for item in candidates},
        )
        self.assertTrue(all(item.source == "sibling_context" for item in candidates))

    def test_same_message_temporal_node_is_context_candidate_not_attribution_edge(self):
        graph = TraceGraph.from_trace(trace_with_same_message_temporal_candidate())
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
        )

        self.assertEqual([item.ref for item in candidates], ["record:temporal_result"])
        self.assertEqual(candidates[0].source, "sibling_context")
        self.assertFalse(candidates[0].edge["eligible_for_attribution"])
        self.assertTrue(candidates[0].edge["retrieval_candidate"])

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

    def test_semantic_search_and_fallback_exclude_audit_only_external_facts(self):
        records = [
            external_fact_record(
                "mismatched_external",
                status="failed",
                revision_status="mismatched",
            ),
            external_fact_record(
                "unknown_external",
                status="unknown",
                revision_status="matched",
            ),
            {
                "record_id": "normal_decision",
                "component": "processor",
                "event_type": "decision",
                "data": {
                    "rationale": "Recover the missing namespace compatibility contract."
                },
            },
            {
                "record_id": "current",
                "component": "result",
                "event_type": "response.claim",
                "data": {"text": "The namespace compatibility contract is missing."},
            },
        ]
        graph = TraceGraph.from_trace(
            {"case_id": "external-semantic-filter", "records": records}
        )
        defect_state = sample_defect_state()

        matches = graph.semantic_search(
            ["namespace", "contract"], before_ref="record:current", limit=8
        )
        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:current",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
            allow_semantic_fallback=True,
        )

        self.assertEqual([item["ref"] for item in matches], ["record:normal_decision"])
        self.assertEqual([item.ref for item in candidates], ["record:normal_decision"])
        self.assertEqual(candidates[0].source, "semantic_fallback")

    def test_malformed_eligible_edge_from_audit_only_external_source_is_filtered(self):
        trace = {
            "case_id": "malformed-external-edge",
            "records": [
                external_fact_record(
                    "mismatched_external",
                    status="failed",
                    revision_status="mismatched",
                ),
                {
                    "record_id": "current",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "The namespace contract failed."},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "external_evaluation", "id": "mismatched_external"},
                    "to": {"type": "record", "id": "current"},
                    "relation": "external_evaluation_observed",
                    "evidence_type": "external_grader",
                    "eligible_for_attribution": True,
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:current",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            allow_semantic_fallback=True,
        )

        self.assertEqual(graph.upstream_refs("record:current"), [])
        self.assertNotIn(
            "record:mismatched_external", {item.ref for item in candidates}
        )

    def test_semantic_fallback_has_a_bounded_exploration_quota(self):
        records = [
            {
                "record_id": f"candidate_{index}",
                "component": "processor",
                "event_type": "decision",
                "data": {
                    "rationale": "Recover the missing parser namespace compatibility contract."
                },
            }
            for index in range(20)
        ]
        records.append(
            {
                "record_id": "current",
                "component": "result",
                "event_type": "response.claim",
                "data": {"text": "The parser namespace contract remains incomplete."},
            }
        )
        graph = TraceGraph.from_trace(
            {"case_id": "bounded-semantic-fallback", "records": records}
        )
        defect_state = sample_defect_state()

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:current",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=24,
            allow_semantic_fallback=True,
        )

        self.assertEqual(len(candidates), 5)
        self.assertTrue(all(item.source == "semantic_fallback" for item in candidates))

    def test_interrupted_failure_excludes_stale_completion_diagnostics_from_candidates(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "interrupted-candidate-filter",
                "manifest": {"shutdown_disposition": "interrupted_before_case_completion"},
                "records": [
                    {
                        "record_id": "signal",
                        "component": "runtime",
                        "event_type": "process.signal",
                        "data": {"signal": "SIGTERM"},
                    },
                    {
                        "record_id": "missing_semantic_final_test_result",
                        "component": "trace",
                        "event_type": "case.missing_semantic",
                        "data": {
                            "semantic_name": "final_test_result",
                            "reason": "No test-like verification command was recorded after repository changes.",
                        },
                    },
                    {
                        "record_id": "observed_defect_missing_verification_after_change",
                        "component": "trace",
                        "event_type": "case.observed_defect",
                        "data": {"failure_type": "final_test_result_missing"},
                    },
                    {
                        "record_id": "failed",
                        "component": "run",
                        "event_type": "case.failed",
                        "source_refs": ["record:signal"],
                        "data": {
                            "shutdown_signal": "SIGTERM",
                            "shutdown_disposition": "interrupted_before_case_completion",
                        },
                    },
                ],
            }
        )
        defect_state = DefectState.create(
            label="interrupted_missing_verification",
            expected="The changed repository is verified before completion.",
            actual="SIGTERM interrupted the case before final verification.",
            mechanism="The process signal interrupted the case and final verification is missing.",
            scope="process_lifecycle",
        )

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:failed",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
            allow_semantic_fallback=True,
        )

        refs = [item.ref for item in candidates]
        self.assertIn("record:signal", refs)
        self.assertNotIn("record:missing_semantic_final_test_result", refs)
        self.assertNotIn("record:observed_defect_missing_verification_after_change", refs)

    def test_process_signal_boundary_does_not_use_global_semantic_fallback(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "signal-boundary",
                "records": [
                    {
                        "record_id": "unrelated_prior",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "Consider how SIGTERM interruption affects completion."},
                    },
                    {
                        "record_id": "signal",
                        "component": "runtime",
                        "event_type": "process.signal",
                        "data": {
                            "signal": "SIGTERM",
                            "shutdown_disposition": "interrupted_before_case_completion",
                        },
                    },
                ],
            }
        )
        defect_state = DefectState.create(
            label="process_interruption",
            expected="The process completes normally.",
            actual="SIGTERM interrupted completion.",
            mechanism="The process received SIGTERM.",
            scope="process_lifecycle",
        )

        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:signal",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            limit=8,
            allow_semantic_fallback=True,
        )

        self.assertEqual(candidates, [])

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

    def test_recursive_context_grounds_unresolved_refs_and_candidate_artifacts(self):
        defect_state = sample_defect_state()
        hypothesis = sample_hypothesis(defect_state).with_updates(
            supporting_evidence=(
                HypothesisEvidence(
                    ref="record:missing_hypothesis_evidence",
                    reason="Missing corroboration remains relevant.",
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_file = root / "trace.json"
            trace_file.write_text(json.dumps(trace_with_unresolved_evidence_and_artifact()), encoding="utf-8")
            graph = TraceGraph.from_file(trace_file)
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
                hypothesis=hypothesis,
                candidates=candidates,
                downstream_path=["record:missing_downstream", "record:decision"],
            )

        candidate = context["candidate_predecessors"][0]
        self.assertEqual(context["downstream_path_references"][0]["raw_ref"], "record:missing_downstream")
        self.assertEqual(context["downstream_path_references"][0]["resolution_status"], "unresolved")
        self.assertEqual(candidate["edge_evidence_references"][0]["raw_ref"], "record:missing_evidence")
        self.assertEqual(candidate["edge_evidence_references"][0]["resolution_status"], "unresolved")
        self.assertEqual(candidate["edge_endpoint_references"]["from"]["raw_ref"], "record:prompt")
        self.assertEqual(context["hypothesis_evidence_references"][0]["raw_ref"], "record:missing_hypothesis_evidence")
        self.assertEqual(candidate["artifact_hydration"]["missing_artifact_ids"], ["missing_payload"])
        self.assertEqual(context["context_manifest"]["unresolved_reference_count"], 3)
        self.assertEqual(context["context_manifest"]["missing_artifact_count"], 1)

    def test_recursive_context_rebuilds_from_serialized_state_and_reloaded_graph(self):
        trace = trace_with_confirmed_and_inferred_predecessors()
        defect_state = sample_defect_state()
        hypothesis = sample_hypothesis(defect_state)
        graph = TraceGraph.from_trace(trace)
        candidates = SemanticPredecessorRetriever().retrieve(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=hypothesis,
            limit=8,
        )
        judgment = CausalStepJudgment(
            current_node_ref="record:observed",
            current_defect_status="present",
            current_defect_reason="The final parser contract is incomplete.",
        )
        typed_context = build_recursive_judgment_context(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            defect_transformation_chain=[defect_state],
            hypothesis=hypothesis,
            candidates=candidates,
            downstream_path=["record:observed", "record:decision"],
            downstream_judgments=[judgment],
            objective="Recover all parser namespace compatibility behavior.",
        )
        rebuilt_context = build_recursive_judgment_context(
            {
                "node_ref": "record:decision",
                "defect_state": defect_state.to_dict(),
                "defect_transformation_chain": [defect_state.to_dict()],
                "hypothesis": hypothesis.to_dict(),
                "candidates": [item.to_dict() for item in candidates],
                "downstream_path": ["record:observed", "record:decision"],
                "downstream_judgments": [judgment.to_dict()],
                "objective": "Recover all parser namespace compatibility behavior.",
            },
            graph=TraceGraph.from_trace(trace),
        )

        self.assertEqual(rebuilt_context, typed_context)

    def test_ground_reference_resolves_indexed_artifacts_without_inventing_nodes(self):
        defect_state = sample_defect_state()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "known-payload.txt").write_text("artifact evidence", encoding="utf-8")
            trace_file = root / "trace.json"
            trace_file.write_text(json.dumps(trace_with_artifact_evidence()), encoding="utf-8")
            graph = TraceGraph.from_file(trace_file)
            candidates = SemanticPredecessorRetriever().retrieve(
                graph=graph,
                node_ref="record:decision",
                defect_state=defect_state,
                hypothesis=sample_hypothesis(defect_state),
                limit=8,
            )
            context = build_recursive_judgment_context(
                graph=graph,
                node_ref="record:decision",
                defect_state=defect_state,
                hypothesis=sample_hypothesis(defect_state),
                candidates=candidates,
                downstream_path=["record:decision"],
            )

        known, missing, unknown = context["candidate_predecessors"][0]["edge_evidence_references"]
        self.assertEqual(known["raw_ref"], "artifact:known_payload")
        self.assertEqual(known["resolved_ref"], "artifact:known_payload")
        self.assertEqual(known["reference_kind"], "artifact")
        self.assertEqual(known["artifact_status"]["availability"], "available")
        self.assertEqual(missing["resolved_ref"], "artifact:missing_payload")
        self.assertEqual(missing["artifact_status"]["availability"], "missing")
        self.assertEqual(unknown["raw_ref"], "unknown_payload")
        self.assertEqual(unknown["resolution_status"], "unresolved")
        self.assertEqual(graph.artifact_reference_status("known_payload")["canonical_ref"], "artifact:known_payload")

    def test_unresolved_downstream_path_never_emits_an_eligible_causal_edge(self):
        graph = TraceGraph.from_trace(trace_with_confirmed_and_inferred_predecessors())
        defect_state = sample_defect_state()
        context = build_recursive_judgment_context(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect_state,
            hypothesis=sample_hypothesis(defect_state),
            candidates=[],
            downstream_path=["record:missing_downstream", "record:decision"],
        )

        self.assertEqual(context["outgoing_edges_on_active_path"][0]["relation"], "unresolved_path_advisory")
        self.assertFalse(context["outgoing_edges_on_active_path"][0]["eligible_for_attribution"])
        self.assertEqual(context["outgoing_edges_on_active_path"][0]["confidence"], 0.0)
        self.assertEqual(context["outgoing_edges_on_active_path"][0]["resolution_status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
