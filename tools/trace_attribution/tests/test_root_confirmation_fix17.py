from __future__ import annotations

import json
import unittest

from trace_attribution.causal_judge import (
    CausalStepRequest,
    OfflineJudgeCapability,
    build_causal_step_prompt,
)
from trace_attribution.causal_state import (
    AttributionHypothesis,
    CausalCandidate,
    CausalStepJudgment,
    DefectState,
    HypothesisEvidence,
    RootConfirmation,
)
from trace_attribution.episodes import CausalEpisodeIndex
from trace_attribution.evidence_capsule import build_candidate_evidence_capsules
from trace_attribution.global_judge import (
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    build_global_candidate_prompt,
    validate_global_candidate_request_against_graph,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.investigation import (
    CausalInvestigationTools,
    InvestigationDirective,
)
from trace_attribution.judgment_context import (
    build_recursive_judgment_context,
    progress_episode_context,
)
from trace_attribution.progress import active_progress_episode_data
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)


def defect_state() -> DefectState:
    return DefectState.create(
        label="active_generation_defect",
        expected="The active generation is correct.",
        actual="The active generation contains a defect.",
        mechanism="A stale record must not ground the active defect.",
        scope="task_quality",
    )


def revision_trace() -> dict:
    return {
        "case_id": "fix17-revision",
        "records": [
            {
                "record_id": "decision",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "repository_revision": 1,
                    "rationale": "Author an active-generation decision.",
                },
            },
            {
                "record_id": "stale_change",
                "component": "tool",
                "event_type": "change",
                "data": {
                    "repository_revision": 1,
                    "revision_after": 0,
                    "summary": "A stale generation change.",
                },
            },
            {
                "record_id": "active_defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:stale_change"],
                "data": {
                    "repository_revision": 1,
                    "actual": "The active generation contains a defect.",
                },
            },
            {
                "record_id": "active_claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 1,
                    "is_final_for_case": True,
                    "claim": "The active generation is authoritative.",
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "stale_change"},
                "to": {"type": "record", "id": "active_defect"},
                "relation": "change_observed_by_evaluation",
                "evidence_type": "confirmed",
                "eligible_for_attribution": True,
            }
        ],
    }


class CountingJudge(OfflineJudgeCapability):
    def __init__(self) -> None:
        self.step_requests = []
        self.confirmation_requests = []

    def judge_step(self, request):
        self.step_requests.append(request)
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="The test Judge should not be invoked.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=("unexpected Judge request",),
            confidence=0.0,
        )

    def confirm_candidate(self, request):
        self.confirmation_requests.append(request)
        return RootConfirmation.unknown(
            request.candidate_ref, "The test Judge should not be invoked."
        )


def strip_audit_diagnostics(value):
    if isinstance(value, dict):
        return {
            key: strip_audit_diagnostics(child)
            for key, child in value.items()
            if key
            not in {
                "missing_evidence_refs",
                "unresolved_references",
                "stale_member_refs",
            }
        }
    if isinstance(value, list):
        return [strip_audit_diagnostics(child) for child in value]
    return value


class EventSpecificRevisionEligibilityTest(unittest.TestCase):
    def test_change_uses_revision_after_and_preserves_numeric_zero(self):
        graph = TraceGraph.from_trace(revision_trace())

        self.assertEqual(graph.active_repository_revision(), 1)
        self.assertFalse(
            graph.active_revision_evidence_eligible("record:stale_change")
        )
        self.assertTrue(graph.active_revision_evidence_eligible("record:decision"))
        self.assertTrue(graph.active_revision_evidence_eligible("record:active_defect"))

    def test_change_revision_after_contract_covers_missing_malformed_alias_and_no_authority(self):
        for label, value, expected in (
            ("matching", 1, True),
            ("zero", 0, False),
            ("boolean", True, False),
            ("negative", -1, False),
            ("string", "1", False),
            ("missing", None, False),
        ):
            with self.subTest(label=label):
                trace = revision_trace()
                if value is None:
                    trace["records"][1]["data"].pop("revision_after")
                else:
                    trace["records"][1]["data"]["revision_after"] = value
                graph = TraceGraph.from_trace(trace)
                self.assertEqual(
                    graph.active_revision_evidence_eligible(
                        "record:stale_change"
                    ),
                    expected,
                )
                self.assertEqual(
                    graph.active_revision_evidence_eligible("stale_change"),
                    expected,
                )

        no_authority_missing = TraceGraph.from_trace(
            {
                "case_id": "legacy-change",
                "records": [
                    {
                        "record_id": "legacy_change",
                        "component": "tool",
                        "event_type": "change",
                        "data": {"summary": "No generation authority exists."},
                    }
                ],
            }
        )
        self.assertIsNone(no_authority_missing.active_repository_revision())
        self.assertTrue(
            no_authority_missing.active_revision_evidence_eligible(
                "record:legacy_change"
            )
        )
        zero_authority = TraceGraph.from_trace(
            {
                "case_id": "zero-change",
                "records": [
                    {
                        "record_id": "zero_change",
                        "component": "tool",
                        "event_type": "change",
                        "data": {"revision_after": 0},
                    }
                ],
            }
        )
        self.assertEqual(zero_authority.active_repository_revision(), 0)
        self.assertTrue(
            zero_authority.active_revision_evidence_eligible(
                "record:zero_change"
            )
        )

    def test_stale_change_cannot_build_a_capsule_or_global_request(self):
        graph = TraceGraph.from_trace(revision_trace())
        candidate = CausalCandidate(
            ref="record:stale_change",
            node=graph.nodes["record:stale_change"],
            source="confirmed_edge",
            edge=graph.edge_context(
                "record:stale_change", "record:active_defect"
            )[0],
            evidence_refs=("record:stale_change", "record:active_defect"),
        )

        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect_state(),
            downstream_paths={
                "record:stale_change": (
                    "record:stale_change",
                    "record:active_defect",
                )
            },
            start_refs=("record:active_defect",),
        )

        self.assertEqual(capsules, ())

    def test_direct_global_validation_rejects_a_stale_seed_and_start(self):
        active_trace = revision_trace()
        active_trace["records"][1]["data"]["revision_after"] = 1
        graph = TraceGraph.from_trace(active_trace)
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="semantic_fallback",
            edge={},
            evidence_refs=("record:decision",),
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect_state(),
            downstream_paths={"record:decision": ("record:decision",)},
            start_refs=("record:stale_change",),
        )
        request = GlobalCandidateJudgeRequest(
            case_id=graph.case_id,
            objective="Find the active root.",
            analysis_perspective="task quality",
            seed_ref="record:stale_change",
            active_defect=defect_state(),
            active_focus_text=defect_state().actual,
            active_focus_text_hash=active_focus_text_sha256(
                defect_state().actual
            ),
            start_refs=("record:stale_change",),
            capsules=capsules,
        )

        with self.assertRaisesRegex(ValueError, "seed|start"):
            validate_global_candidate_request_against_graph(
                TraceGraph.from_trace(revision_trace()),
                request,
                authoritative_candidates=(candidate,),
            )


class StaleAnalysisStartTest(unittest.TestCase):
    def stale_start_trace(self) -> dict:
        return {
            "case_id": "fix17-stale-start",
            "records": [
                {
                    "record_id": "stale_claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "repository_revision": 0,
                        "is_final_for_case": True,
                        "claim": "A stale final claim.",
                    },
                },
                {
                    "record_id": "active_change",
                    "component": "tool",
                    "event_type": "change",
                    "data": {
                        "revision_after": 1,
                        "summary": "The active generation.",
                    },
                },
            ],
        }

    def test_explicit_stale_start_is_audited_without_frontier_or_judge(self):
        graph = TraceGraph.from_trace(self.stale_start_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=("record:stale_claim",),
            objective="Assess the final claim.",
            analysis_perspective="task quality",
        )

        self.assertFalse(state.frontier)
        result = state.seed_results()[0]
        self.assertEqual(result.outcome, "evidence_gap")
        self.assertIn(
            "start_ref_active_revision_ineligible", result.blocking_reasons
        )

        judge = CountingJudge()
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=("record:stale_claim",),
            objective="Assess the final claim.",
        )
        self.assertEqual(judge.step_requests, [])
        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.seed_results[0].outcome, "evidence_gap")

    def test_default_starts_exclude_stale_claims(self):
        graph = TraceGraph.from_trace(self.stale_start_trace())

        self.assertNotIn("record:stale_claim", graph.default_start_refs())

    def test_stale_seed_does_not_block_an_active_seed(self):
        graph = TraceGraph.from_trace(self.stale_start_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=("record:stale_claim", "record:active_change"),
            objective="Assess both seeds independently.",
            analysis_perspective="task quality",
        )

        by_ref = {item.start_ref: item for item in state.seed_results()}
        self.assertEqual(by_ref["record:stale_claim"].outcome, "evidence_gap")
        self.assertEqual(by_ref["record:active_change"].outcome, "inconclusive")
        self.assertEqual(
            [item["node_ref"] for item in state.frontier.snapshot()],
            ["record:active_change"],
        )


class ActiveEpisodeContextTest(unittest.TestCase):
    def episode_graph(self) -> TraceGraph:
        return TraceGraph.from_trace(
            {
                "case_id": "fix17-episode",
                "records": [
                    {
                        "record_id": "active_decision",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "repository_revision": 1,
                            "call_id": "call-1",
                            "decision_type": "reasoning_block",
                            "rationale": "Active reasoning.",
                        },
                    },
                    {
                        "record_id": "stale_change",
                        "component": "tool",
                        "event_type": "change",
                        "data": {
                            "repository_revision": 1,
                            "revision_after": 0,
                            "call_id": "call-1",
                            "summary": "Stale mutation.",
                        },
                    },
                    {
                        "record_id": "active_change",
                        "component": "tool",
                        "event_type": "change",
                        "data": {
                            "revision_after": 1,
                            "call_id": "call-1",
                            "summary": "Active mutation.",
                        },
                    },
                    {
                        "record_id": "episode",
                        "component": "progress",
                        "event_type": "progress.episode",
                        "data": {
                            "member_refs": [
                                "record:active_decision",
                                "record:stale_change",
                                "record:active_change",
                            ],
                            "candidate_member_refs": [
                                "record:stale_change",
                                "record:active_change",
                            ],
                            "member_summaries": [
                                {"ref": "record:stale_change"},
                                {"ref": "record:active_change"},
                            ],
                            "mutation_count": 2,
                            "verification_count": 0,
                            "no_delivery_progress": False,
                            "chronology_index": 1,
                        },
                    },
                    {
                        "record_id": "active_claim",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {
                            "repository_revision": 1,
                            "is_final_for_case": True,
                        },
                    },
                ],
            }
        )

    def test_causal_and_progress_episode_payloads_rebuild_from_active_members(self):
        graph = self.episode_graph()
        causal = CausalEpisodeIndex.from_graph(graph).episode_for(
            "record:active_decision"
        )
        progress = progress_episode_context(graph, "record:active_change")
        audit = active_progress_episode_data(graph, "record:episode")

        self.assertEqual(
            causal.member_refs,
            ["record:active_decision", "record:active_change"],
        )
        self.assertEqual(
            progress["member_refs"],
            ["record:active_decision", "record:active_change"],
        )
        self.assertEqual(
            progress["candidate_member_refs"],
            ["record:active_decision", "record:active_change"],
        )
        self.assertEqual(progress["mutation_count"], 1)
        self.assertEqual(progress["member_count"], 2)
        self.assertNotIn("stale_member_refs", progress)
        self.assertEqual(audit["stale_member_refs"], ["record:stale_change"])


class EligibleBudgetPrefilterTest(unittest.TestCase):
    def scan_graph(self) -> TraceGraph:
        return TraceGraph.from_trace(
            {
                "case_id": "fix17-scan",
                "records": [
                    {
                        "record_id": "stale",
                        "component": "tool",
                        "event_type": "change",
                        "data": {
                            "repository_revision": 1,
                            "revision_after": 0,
                            "summary": "needle stale",
                        },
                    },
                    {
                        "record_id": "active",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "repository_revision": 1,
                            "rationale": "needle active",
                        },
                    },
                    {
                        "record_id": "target",
                        "component": "result",
                        "event_type": "response.claim",
                        "source_refs": ["record:stale", "record:active"],
                        "data": {
                            "repository_revision": 1,
                            "is_final_for_case": True,
                        },
                    },
                ],
            }
        )

    def test_stale_adjacency_does_not_consume_scan_limit_one(self):
        graph = self.scan_graph()

        result = graph.bounded_upstream_refs(
            "record:target", limit=1, scan_limit=1
        )

        self.assertEqual(result.refs, ("record:active",))
        self.assertEqual(result.inspected_count, 1)
        self.assertFalse(result.scan_truncated)

    def test_stale_semantic_node_does_not_consume_scan_limit_one(self):
        graph = self.scan_graph()
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "search_semantic_nodes",
                {
                    "query": "needle",
                    "before_ref": "record:target",
                    "limit": 1,
                    "scan_limit": 1,
                },
                requested_by_ref="record:target",
                reason="Find the first active semantic match.",
            )
        )

        self.assertEqual(
            [item["ref"] for item in result.payload["matches"]],
            ["record:active"],
        )
        self.assertFalse(result.truncated)


class JudgePayloadGroundingTest(unittest.TestCase):
    def payload_graph(self) -> TraceGraph:
        trace = revision_trace()
        trace["records"][0]["source_refs"] = ["record:missing-source"]
        trace["records"][2]["source_refs"] = ["record:decision"]
        trace["dataflow_edges"][0]["from"]["id"] = "decision"
        return TraceGraph.from_trace(trace)

    def test_unresolved_candidate_and_hypothesis_refs_never_reach_judge_payloads(self):
        graph = self.payload_graph()
        defect = defect_state()
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=graph.edge_context("record:decision", "record:active_defect")[0],
            evidence_refs=("record:decision", "record:missing-evidence"),
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect,
            downstream_paths={
                "record:decision": (
                    "record:decision",
                    "record:active_defect",
                )
            },
            start_refs=("record:active_defect",),
        )
        self.assertEqual(len(capsules), 1)
        capsule_payload = capsules[0].to_dict()
        self.assertIn(
            "record:missing-evidence", capsule_payload["missing_evidence_refs"]
        )
        self.assertNotIn(
            "record:missing-evidence",
            json.dumps(strip_audit_diagnostics(capsule_payload), sort_keys=True),
        )

        request = GlobalCandidateJudgeRequest(
            case_id=graph.case_id,
            objective="Find the active root.",
            analysis_perspective="task quality",
            seed_ref="record:active_defect",
            active_defect=defect,
            active_focus_text=defect.actual,
            active_focus_text_hash=active_focus_text_sha256(defect.actual),
            start_refs=("record:active_defect",),
            capsules=capsules,
        )
        global_payload = json.loads(build_global_candidate_prompt(request))
        self.assertNotIn(
            "record:missing-evidence",
            json.dumps(global_payload, sort_keys=True),
        )

        hypothesis = AttributionHypothesis.create(
            claim="The active decision introduced the defect.",
            candidate_root_ref="record:decision",
            defect_state=defect,
        ).with_updates(
            supporting_evidence=(
                HypothesisEvidence(
                    "record:missing-support",
                    "An unresolved ref is diagnostic only.",
                    0.5,
                ),
            )
        )
        context = build_recursive_judgment_context(
            graph=graph,
            node_ref="record:decision",
            defect_state=defect,
            hypothesis=hypothesis,
            candidates=(candidate,),
            downstream_path=["record:decision", "record:active_defect"],
            objective="Find the active root.",
        )
        self.assertNotIn("unresolved_references", context)
        self.assertNotIn(
            "record:diagnostic-only",
            context["candidate_predecessors"][0]["evidence_refs"],
        )
        step_request = CausalStepRequest(
            recursive_context=context,
            current_node=graph.sanitize_judge_node(
                graph.nodes["record:decision"]
            ),
            defect_state=defect,
            candidates=(
                CausalCandidate(
                    ref=candidate.ref,
                    node=graph.sanitize_judge_node(candidate.node),
                    source=candidate.source,
                    edge=graph.sanitize_judge_edge_evidence(candidate.edge),
                    evidence_refs=tuple(
                        graph.filter_evidence_refs(candidate.evidence_refs)
                    ),
                ),
            ),
        )
        step_payload = json.loads(build_causal_step_prompt(step_request))
        self.assertNotIn(
            "record:missing-support",
            json.dumps(step_payload, sort_keys=True),
        )
        self.assertNotIn(
            "record:missing-source",
            json.dumps(step_payload, sort_keys=True),
        )
        self.assertNotIn(
            "record:diagnostic-only",
            json.dumps(step_payload, sort_keys=True),
        )


class EligibilityNamingTest(unittest.TestCase):
    def test_generic_candidate_alias_is_removed_in_favor_of_authored_root_gate(self):
        import trace_attribution.causal_retrieval as retrieval

        graph = TraceGraph.from_trace(revision_trace())
        self.assertFalse(hasattr(graph, "active_revision_candidate_eligible"))
        self.assertFalse(
            hasattr(retrieval, "active_revision_candidate_eligible")
        )
        self.assertTrue(hasattr(retrieval, "authored_root_candidate_eligible"))


if __name__ == "__main__":
    unittest.main()
