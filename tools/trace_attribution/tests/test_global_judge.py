from __future__ import annotations

import unittest
import json

from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import ClaudeCausalJudge
from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution.evidence_capsule import build_candidate_evidence_capsules
from trace_attribution.global_judge import (
    GlobalCandidateJudgeRequest,
    build_global_candidate_prompt,
    global_candidate_judgment_from_payload,
    validate_global_candidate_payload,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.errors import TransportCallResult


def sample_request() -> GlobalCandidateJudgeRequest:
    trace = {
        "case_id": "global-judge-case",
        "records": [
            {
                "record_id": "verification",
                "component": "tool",
                "event_type": "verification",
                "data": {
                    "verification_status": "passed",
                    "summary": "All focused tests passed.",
                },
            },
            {
                "record_id": "decision",
                "component": "processor",
                "event_type": "decision",
                "data": {"rationale": "Assume direct cancellation models SIGINT."},
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:verification", "record:decision"],
                "data": {"actual": "Cleanup is interrupted after process SIGINT."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "verification"},
                "to": {"type": "record", "id": "defect"},
                "relation": "outcome_evidence",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "decision"},
                "to": {"type": "record", "id": "defect"},
                "relation": "decision_exposed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            },
        ],
    }
    graph = TraceGraph.from_trace(trace)
    defect = DefectState.create(
        label="sigint_cleanup_interrupted",
        expected="Started cleanup completes after process SIGINT.",
        actual="Started cleanup is interrupted.",
        mechanism="The cancellation model may not preserve cleanup.",
        scope="task_quality",
    )
    candidates = [
        CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="progress_window",
            score=0.9,
            evidence_refs=("record:decision",),
        ),
        CausalCandidate(
            ref="record:verification",
            node=graph.nodes["record:verification"],
            source="outcome_evidence",
            score=1.0,
            evidence_refs=("record:verification",),
        ),
    ]
    capsules = build_candidate_evidence_capsules(
        graph=graph,
        candidates=candidates,
        defect_state=defect,
        downstream_paths={
            "record:decision": ("record:decision", "record:defect"),
            "record:verification": ("record:verification", "record:defect"),
        },
        start_refs=("record:defect",),
    )
    return GlobalCandidateJudgeRequest(
        case_id="global-judge-case",
        objective="Find the trace-visible root or determine that the observed defect is contradicted.",
        analysis_perspective="task quality",
        start_refs=("record:defect",),
        capsules=capsules,
    )


def assessment(
    ref: str,
    *,
    defect_status: str,
    causal_role: str,
    evidence_refs: list[str],
) -> dict:
    return {
        "candidate_ref": ref,
        "defect_status": defect_status,
        "causal_role": causal_role,
        "reason": "Grounded comparative judgment for the offered candidate.",
        "evidence_refs": evidence_refs,
        "confidence": 0.9,
    }


def payload(*, outcome: str) -> dict:
    return {
        "outcome": outcome,
        "reason": "Global comparison across all offered candidate evidence closures.",
        "assessments": [
            assessment(
                "record:decision",
                defect_status="present" if outcome == "candidate_roots" else "absent",
                causal_role="root_candidate" if outcome == "candidate_roots" else "unrelated",
                evidence_refs=["record:decision"],
            ),
            assessment(
                "record:verification",
                defect_status="absent",
                causal_role="exculpatory_evidence",
                evidence_refs=["record:verification"],
            ),
        ],
        "selected_candidate_refs": ["record:decision"] if outcome == "candidate_roots" else [],
        "expansion_requests": [],
        "decisive_evidence_refs": ["record:verification"] if outcome == "no_defect" else ["record:decision"],
        "missing_evidence": [],
        "confidence": 0.91,
    }


class GlobalCandidateJudgeContractTest(unittest.TestCase):
    def test_candidate_roots_require_present_root_assessment(self):
        request = sample_request()

        judgment = global_candidate_judgment_from_payload(
            payload(outcome="candidate_roots"), request=request
        )

        self.assertEqual(judgment.outcome, "candidate_roots")
        self.assertEqual(judgment.selected_candidate_refs, ("record:decision",))

    def test_candidate_roots_reject_result_evidence_nodes(self):
        request = sample_request()
        value = payload(outcome="candidate_roots")
        value["assessments"][0].update(
            {"defect_status": "absent", "causal_role": "unrelated"}
        )
        value["assessments"][1].update(
            {"defect_status": "present", "causal_role": "root_candidate"}
        )
        value["selected_candidate_refs"] = ["record:verification"]
        value["decisive_evidence_refs"] = ["record:verification"]

        with self.assertRaisesRegex(ValueError, "ineligible"):
            validate_global_candidate_payload(value, request=request)

    def test_no_defect_requires_grounded_decisive_evidence(self):
        request = sample_request()
        value = payload(outcome="no_defect")

        judgment = global_candidate_judgment_from_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")
        self.assertEqual(judgment.decisive_evidence_refs, ("record:verification",))

        value["decisive_evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "decisive evidence"):
            validate_global_candidate_payload(value, request=request)

    def test_no_defect_allows_a_recorded_observation_refuted_by_counterevidence(self):
        request = sample_request()
        value = payload(outcome="no_defect")
        value["assessments"][0].update(
            {
                "defect_status": "present",
                "causal_role": "outcome_evidence",
            }
        )

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")

        value["assessments"][0]["causal_role"] = "contributing_condition"
        with self.assertRaisesRegex(ValueError, "causal candidate"):
            validate_global_candidate_payload(value, request=request)

    def test_no_defect_accepts_decisive_absent_outcome_evidence(self):
        request = sample_request()
        value = payload(outcome="no_defect")
        value["assessments"][1]["causal_role"] = "outcome_evidence"

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")

        value["decisive_evidence_refs"] = ["record:decision"]
        with self.assertRaisesRegex(ValueError, "decisive absent evidence"):
            validate_global_candidate_payload(value, request=request)

    def test_unknown_selected_candidate_is_rejected(self):
        request = sample_request()
        value = payload(outcome="candidate_roots")
        value["selected_candidate_refs"] = ["record:not_offered"]

        with self.assertRaisesRegex(ValueError, "offered candidate"):
            validate_global_candidate_payload(value, request=request)

    def test_needs_expansion_requires_grounded_anchor_and_context_kind(self):
        request = sample_request()
        value = payload(outcome="needs_expansion")
        value["decisive_evidence_refs"] = []
        value["missing_evidence"] = ["Need the exact process-level SIGINT test result."]
        value["expansion_requests"] = [
            {
                "anchor_ref": "record:decision",
                "context_kind": "upstream",
                "reason": "Inspect the authored assumption that produced this decision.",
            }
        ]

        judgment = global_candidate_judgment_from_payload(value, request=request)

        self.assertEqual(judgment.outcome, "needs_expansion")
        self.assertEqual(judgment.expansion_requests[0]["anchor_ref"], "record:decision")

        value["expansion_requests"][0]["anchor_ref"] = "record:not_offered"
        with self.assertRaisesRegex(ValueError, "grounded anchor"):
            validate_global_candidate_payload(value, request=request)

    def test_prompt_states_that_retrieval_rank_is_not_a_causal_verdict(self):
        prompt = build_global_candidate_prompt(sample_request())

        self.assertIn("retrieval rank is navigation evidence", prompt)
        self.assertIn("no_defect", prompt)
        self.assertIn("needs_expansion", prompt)

    def test_prompt_distinguishes_active_defect_truth_from_record_presence(self):
        prompt = build_global_candidate_prompt(sample_request())
        parsed = json.loads(prompt)

        self.assertIn("whether the active defect is true", prompt)
        self.assertIn("record itself exists", prompt)
        self.assertIn("would change the current judgment", prompt)
        self.assertIn("root_candidate_eligible=false", prompt)
        self.assertEqual(
            parsed["request"]["active_focus"]["defect_state"]["actual"],
            "Started cleanup is interrupted.",
        )
        self.assertIn("Do not substitute another claim", prompt)

    def test_claude_judge_uses_bounded_global_provider_call(self):
        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = []

            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                self.calls.append(
                    {"system": system, "messages": messages, "max_tokens": max_tokens}
                )
                return TransportCallResult(
                    json.dumps(payload(outcome="candidate_roots")),
                    physical_requests=1,
                )

        transport = Transport()
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_candidates_bounded(
            sample_request(), max_physical_requests=1
        )

        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(result.value.outcome, "candidate_roots")
        self.assertIn("globally compare", transport.calls[0]["system"])


if __name__ == "__main__":
    unittest.main()
