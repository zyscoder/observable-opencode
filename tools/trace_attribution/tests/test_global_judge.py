from __future__ import annotations

import unittest
import json
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import ClaudeCausalJudge
from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution.evidence_capsule import build_candidate_evidence_capsules
from trace_attribution.global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    build_global_candidate_prompt,
    global_candidate_request_from_validation_envelope,
    global_candidate_judgment_from_payload,
    normalize_active_focus_text,
    validate_global_candidate_payload,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.errors import TransportCallResult


def sample_request(
    *,
    decision_edge_fields: dict | None = None,
    include_decision_source_ref: bool = True,
) -> GlobalCandidateJudgeRequest:
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
    if not include_decision_source_ref:
        trace["records"][2]["source_refs"].remove("record:decision")
    trace["dataflow_edges"][1].update(decision_edge_fields or {})
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
        seed_ref="record:defect",
        active_defect=defect,
        active_focus_text=defect.actual,
        active_focus_text_hash=active_focus_text_sha256(defect.actual),
        start_refs=("record:defect",),
        capsules=capsules,
    )


def multi_root_request(*refs: str) -> GlobalCandidateJudgeRequest:
    trace = {
        "case_id": "multi-root-global-judge-case",
        "records": [
            {
                "record_id": ref.removeprefix("record:"),
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "Candidate {0}.".format(ref)},
            }
            for ref in refs
        ]
        + [
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": list(refs),
                "data": {"actual": "The active defect is present."},
            }
        ],
        "dataflow_edges": [
            {
                "from": {
                    "type": "record",
                    "id": ref.removeprefix("record:"),
                },
                "to": {"type": "record", "id": "defect"},
                "relation": "decision_exposed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            }
            for ref in refs
        ],
    }
    graph = TraceGraph.from_trace(trace)
    defect = DefectState.create(
        label="active_defect",
        expected="The active defect is absent.",
        actual="The active defect is present.",
        mechanism="One of the authored decisions introduced it.",
        scope="task_quality",
    )
    capsules = build_candidate_evidence_capsules(
        graph=graph,
        candidates=[
            CausalCandidate(
                ref=ref,
                node=graph.nodes[ref],
                source="confirmed_edge",
                score=0.9,
                evidence_refs=(ref,),
            )
            for ref in refs
        ],
        defect_state=defect,
        downstream_paths={ref: (ref, "record:defect") for ref in refs},
        start_refs=("record:defect",),
    )
    return GlobalCandidateJudgeRequest(
        case_id=trace["case_id"],
        objective="Select no more than three authored root candidates.",
        analysis_perspective="task quality",
        seed_ref="record:defect",
        active_defect=defect,
        active_focus_text=defect.actual,
        active_focus_text_hash=active_focus_text_sha256(defect.actual),
        start_refs=("record:defect",),
        capsules=capsules,
    )


def multi_root_payload(
    request: GlobalCandidateJudgeRequest, selected: list[str]
) -> dict:
    eligible = list(request.open_authored_root_candidate_refs)
    return {
        "outcome": "candidate_roots",
        "reason": "The selected authored candidates each require confirmation.",
        "active_focus_binding": {
            "seed_ref": request.seed_ref,
            "defect_fingerprint": request.active_defect.fingerprint,
            "active_focus_text_hash": request.active_focus_text_hash,
        },
        "assessments": [
            {
                "candidate_ref": capsule.candidate_ref,
                "defect_status": "present",
                "input_defect_status": "absent",
                "output_defect_status": "present",
                "causal_path_refs": list(capsule.downstream_path),
                "counterfactual": counterfactual(
                    capsule.candidate_ref, prevents_defect=True
                ),
                "compared_candidate_refs": eligible,
                "causal_role": "root_candidate",
                "reason": "The candidate can independently explain the defect.",
                "evidence_refs": [capsule.candidate_ref],
                "confidence": 0.8,
            }
            for capsule in request.capsules
        ],
        "selected_candidate_refs": selected,
        "expansion_requests": [],
        "decisive_evidence_refs": list(selected),
        "missing_evidence": [],
        "confidence": 0.8,
    }


def evidence_only_request(event_type: str) -> GlobalCandidateJudgeRequest:
    ref = "record:evidence_only"
    trace = {
        "case_id": "evidence-only-global-judge-case",
        "records": [
            {
                "record_id": "evidence_only",
                "component": "evidence",
                "event_type": event_type,
                "data": {"summary": "Observed evidence only."},
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": [ref],
                "data": {"actual": "The active defect is present."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "evidence_only"},
                "to": {"type": "record", "id": "defect"},
                "relation": "outcome_evidence",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
        ],
    }
    graph = TraceGraph.from_trace(trace)
    defect = DefectState.create(
        label="active_defect",
        expected="The active defect is absent.",
        actual="The active defect is present.",
        mechanism="Evidence observed the defect.",
        scope="task_quality",
    )
    capsules = build_candidate_evidence_capsules(
        graph=graph,
        candidates=[
            CausalCandidate(
                ref=ref,
                node=graph.nodes[ref],
                source="global_evidence",
                score=1.0,
                evidence_refs=(ref,),
            )
        ],
        defect_state=defect,
        downstream_paths={ref: (ref, "record:defect")},
        start_refs=("record:defect",),
    )
    return GlobalCandidateJudgeRequest(
        case_id=trace["case_id"],
        objective="Keep evidence-only nodes out of root confirmation.",
        analysis_perspective="task quality",
        seed_ref="record:defect",
        active_defect=defect,
        active_focus_text=defect.actual,
        active_focus_text_hash=active_focus_text_sha256(defect.actual),
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


def counterfactual(ref: str, *, prevents_defect: bool) -> dict:
    return {
        "intervention_ref": ref,
        "intervention_kind": "replace_with_semantically_correct_behavior",
        "predicted_defect_status": "absent" if prevents_defect else "present",
        "causal_effect": "prevents_defect" if prevents_defect else "does_not_prevent_defect",
    }


def payload(*, outcome: str, request: GlobalCandidateJudgeRequest | None = None) -> dict:
    request = request or sample_request()
    value = {
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
    eligible_refs = [
        capsule.candidate_ref
        for capsule in request.capsules
        if capsule.candidate.get("root_candidate_eligible")
    ]
    value["active_focus_binding"] = {
        "seed_ref": request.seed_ref,
        "defect_fingerprint": request.active_defect.fingerprint,
        "active_focus_text_hash": request.active_focus_text_hash,
    }
    for item, capsule in zip(value["assessments"], request.capsules):
        is_root = item["causal_role"] == "root_candidate"
        item.update(
            {
                "input_defect_status": "absent" if is_root else "unknown",
                "output_defect_status": item["defect_status"],
                "causal_path_refs": list(capsule.downstream_path),
                "counterfactual": counterfactual(
                    item["candidate_ref"], prevents_defect=is_root
                ),
                "compared_candidate_refs": eligible_refs,
            }
        )
    return value


class GlobalCandidateJudgeContractTest(unittest.TestCase):
    def test_evidence_only_nodes_cannot_enter_global_root_selection(self):
        for event_type in (
            "case.completed",
            "tool.error",
            "tool.result",
            "observation",
            "execution.observation",
            "verification",
            "evidence.fact",
            "evidence.semantic_fact",
            "claim.support_assessment",
        ):
            with self.subTest(event_type=event_type):
                request = evidence_only_request(event_type)
                self.assertEqual(request.open_authored_root_candidate_refs, ())
                restored = global_candidate_request_from_validation_envelope(
                    request.validation_envelope()
                )
                self.assertEqual(restored.open_authored_root_candidate_refs, ())
                with self.assertRaisesRegex(ValueError, "ineligible"):
                    validate_global_candidate_payload(
                        multi_root_payload(request, ["record:evidence_only"]),
                        request=request,
                    )

    def test_live_capsule_rejects_non_boolean_candidate_eligibility(self):
        request = sample_request()

        for malformed in ("true", "false", 1, 0, None):
            with self.subTest(value=malformed):
                candidate = dict(request.capsules[0].candidate)
                candidate["root_candidate_eligible"] = malformed
                with self.assertRaisesRegex(ValueError, "root_candidate_eligible"):
                    replace(request.capsules[0], candidate=candidate)

    def test_global_selection_is_deterministic_and_bounded_to_three(self):
        request = multi_root_request(
            "record:delta",
            "record:alpha",
            "record:charlie",
            "record:bravo",
        )
        four = multi_root_payload(
            request,
            ["record:delta", "record:alpha", "record:charlie", "record:bravo"],
        )

        with self.assertRaisesRegex(ValueError, "at most three"):
            validate_global_candidate_payload(four, request=request)

        first = validate_global_candidate_payload(
            multi_root_payload(
                request, ["record:delta", "record:alpha", "record:charlie"]
            ),
            request=request,
        )
        second = validate_global_candidate_payload(
            multi_root_payload(
                request, ["record:charlie", "record:delta", "record:alpha"]
            ),
            request=request,
        )
        self.assertEqual(
            first.selected_candidate_refs,
            ("record:alpha", "record:charlie", "record:delta"),
        )
        self.assertEqual(second.selected_candidate_refs, first.selected_candidate_refs)

        restored_request = global_candidate_request_from_validation_envelope(
            request.validation_envelope()
        )
        with self.assertRaisesRegex(ValueError, "at most three"):
            validate_global_candidate_payload(
                multi_root_payload(
                    restored_request,
                    [
                        "record:delta",
                        "record:alpha",
                        "record:charlie",
                        "record:bravo",
                    ],
                ),
                request=restored_request,
            )

    def test_validation_envelope_rejects_cross_candidate_capsule_identity(self):
        envelope = sample_request().validation_envelope()
        envelope["candidate_evidence_capsules"][0]["candidate"]["node"][
            "ref"
        ] = "record:verification"

        with self.assertRaisesRegex(ValueError, "candidate identity"):
            global_candidate_request_from_validation_envelope(envelope)

    def test_counterfactual_consistency_applies_to_every_assessment(self):
        request = sample_request()
        unselected_root = payload(outcome="candidate_roots", request=request)
        unselected_root["outcome"] = "inconclusive"
        unselected_root["selected_candidate_refs"] = []
        unselected_root["assessments"][0]["counterfactual"] = counterfactual(
            "record:decision", prevents_defect=False
        )

        non_root_prevents = payload(outcome="no_defect", request=request)
        non_root_prevents["assessments"][0]["counterfactual"] = counterfactual(
            "record:decision", prevents_defect=True
        )

        for label, value in (
            ("unselected root", unselected_root),
            ("non-root role", non_root_prevents),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, "counterfactual.*causal role"):
                    validate_global_candidate_payload(value, request=request)

    def test_active_focus_canonicalization_only_normalizes_line_endings(self):
        self.assertEqual(
            normalize_active_focus_text("first\r\nsecond\rthird"),
            "first\nsecond\nthird",
        )
        self.assertEqual(
            active_focus_text_sha256("first\r\nsecond\rthird"),
            active_focus_text_sha256("first\nsecond\nthird"),
        )

        for actual, changed in (
            ("x\N{SUPERSCRIPT TWO}", "x2"),
            ("ParseJSON", "parsejson"),
            (" leading and  internal trailing ", "leading and internal trailing"),
            ("\N{LATIN SMALL LIGATURE FI}", "fi"),
        ):
            with self.subTest(actual=actual, changed=changed):
                self.assertNotEqual(normalize_active_focus_text(actual), changed)
                self.assertNotEqual(
                    active_focus_text_sha256(actual), active_focus_text_sha256(changed)
                )

    def test_request_rejects_lossy_active_focus_equivalence(self):
        request = sample_request()
        for actual, changed in (
            ("x\N{SUPERSCRIPT TWO}", "x2"),
            ("ParseJSON", "parsejson"),
            (" leading and  internal trailing ", "leading and internal trailing"),
            ("\N{LATIN SMALL LIGATURE FI}", "fi"),
        ):
            with self.subTest(actual=actual, changed=changed):
                defect = request.active_defect.transformed(
                    label="focus_canonicalization_boundary",
                    actual=actual,
                    mechanism=request.active_defect.mechanism,
                    transformation_reason="exercise exact active focus binding",
                )
                with self.assertRaisesRegex(ValueError, "active_focus_text must match"):
                    replace(
                        request,
                        active_defect=defect,
                        active_focus_text=changed,
                        active_focus_text_hash=active_focus_text_sha256(changed),
                        capsules=tuple(
                            replace(capsule, defect_state=defect)
                            for capsule in request.capsules
                        ),
                    )

    def test_request_accepts_line_ending_equivalent_active_focus(self):
        request = sample_request()
        defect = request.active_defect.transformed(
            label="focus_line_ending_boundary",
            actual="first\r\nsecond\rthird",
            mechanism=request.active_defect.mechanism,
            transformation_reason="exercise line ending focus binding",
        )
        normalized_line_endings = "first\nsecond\nthird"

        equivalent = replace(
            request,
            active_defect=defect,
            active_focus_text=normalized_line_endings,
            active_focus_text_hash=active_focus_text_sha256(normalized_line_endings),
            capsules=tuple(
                replace(capsule, defect_state=defect) for capsule in request.capsules
            ),
        )

        self.assertEqual(equivalent.active_focus_text, normalized_line_endings)
        self.assertEqual(
            equivalent.active_focus_text_hash,
            active_focus_text_sha256(defect.actual),
        )

    def test_request_rejects_mismatched_active_focus_hash(self):
        request = sample_request()
        with self.assertRaisesRegex(ValueError, "active_focus_text_hash"):
            replace(request, active_focus_text_hash="0" * 64)

    def test_rejects_self_consistent_neighboring_active_focus_text(self):
        request = sample_request()
        neighboring_text = "All focused tests passed."

        with self.assertRaisesRegex(ValueError, "active_focus_text must match"):
            replace(
                request,
                active_focus_text=neighboring_text,
                active_focus_text_hash=active_focus_text_sha256(neighboring_text),
            )

    def test_rejects_self_consistent_neighboring_focus_before_cache_or_replay(self):
        request = sample_request()
        neighboring_text = "All focused tests passed."
        object.__setattr__(request, "active_focus_text", neighboring_text)
        object.__setattr__(
            request,
            "active_focus_text_hash",
            active_focus_text_sha256(neighboring_text),
        )

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                raise AssertionError("invalid focus must not reach the provider or cache")

        cache = JudgmentCache()
        with self.assertRaisesRegex(ValueError, "active_focus_text must match"):
            ClaudeCausalJudge(transport=Transport(), cache=cache).judge_candidates_bounded(
                request, max_physical_requests=0
            )
        with self.assertRaisesRegex(ValueError, "active_focus_text must match"):
            global_candidate_judgment_from_payload(
                payload(outcome="candidate_roots"), request=request
            )
        self.assertEqual(cache.stats()["hits"], 0)

    def test_rejects_judgment_that_answers_neighboring_claim(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["active_focus_binding"]["seed_ref"] = "record:neighboring_claim"

        with self.assertRaisesRegex(ValueError, "active focus"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_active_focus_binding_with_wrong_text_hash(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["active_focus_binding"]["active_focus_text_hash"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "active focus"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_each_missing_candidate_matrix_field(self):
        request = sample_request()
        required_fields = (
            "input_defect_status",
            "output_defect_status",
            "causal_path_refs",
            "counterfactual",
            "compared_candidate_refs",
        )

        for field_name in required_fields:
            with self.subTest(field_name=field_name):
                value = payload(outcome="candidate_roots", request=request)
                del value["assessments"][0][field_name]
                with self.assertRaisesRegex(ValueError, field_name):
                    validate_global_candidate_payload(value, request=request)

    def test_rejects_extra_candidate_matrix_field(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["retrieval_rank"] = 1

        with self.assertRaisesRegex(ValueError, "extra.*retrieval_rank"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_invalid_candidate_to_seed_path(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["causal_path_refs"] = [
            "record:decision",
            "record:verification",
            "record:defect",
        ]

        with self.assertRaisesRegex(ValueError, "causal_path_refs"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_disconnected_but_individually_grounded_causal_path_hops(self):
        request = sample_request()
        request = replace(
            request,
            capsules=(
                replace(
                    request.capsules[0],
                    downstream_path=(
                        "record:decision",
                        "record:verification",
                        "record:defect",
                    ),
                    downstream_path_references=(
                        request.capsules[0].downstream_path_references[0],
                        request.capsules[1].downstream_path_references[0],
                        request.capsules[0].downstream_path_references[-1],
                    ),
                ),
                request.capsules[1],
            ),
        )
        value = payload(outcome="candidate_roots", request=request)

        with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_eligible_noncausal_confirmation_relations(self):
        request = sample_request()
        for relation in (
            "semantic_navigation_route",
            "temporal_sequence",
            "temporal_availability",
            "available_to_next_request",
            "temporal_adjacency",
            "fallback_sequence",
            "previous_progress_episode",
        ):
            with self.subTest(relation=relation):
                capsule = replace(
                    request.capsules[0],
                    causal_path_edges=(
                        {
                            "from_ref": "record:decision",
                            "to_ref": "record:defect",
                            "relation": relation,
                            "eligible_for_attribution": True,
                        },
                    ),
                    incoming_edges=(),
                    outgoing_edges=(),
                )
                rejected_request = replace(
                    request,
                    capsules=(capsule, request.capsules[1]),
                )

                with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
                    validate_global_candidate_payload(
                        payload(outcome="candidate_roots", request=rejected_request),
                        request=rejected_request,
                    )

    def test_rejects_temporal_confirmation_edge_metadata(self):
        request = sample_request()
        for metadata in (
            {"evidence_type": "temporal_inferred"},
            {"evidence_type": "temporal_only"},
            {"evidence_type": "temporal_advisory"},
            {"edge_origin": "offline.temporal_reconstruction"},
            {"inference_method": "same_session_temporal_order"},
        ):
            with self.subTest(metadata=metadata):
                capsule = replace(
                    request.capsules[0],
                    causal_path_edges=(
                        {
                            "from_ref": "record:decision",
                            "to_ref": "record:defect",
                            "relation": "produced",
                            "eligible_for_attribution": True,
                            **metadata,
                        },
                    ),
                    incoming_edges=(),
                    outgoing_edges=(),
                )
                rejected_request = replace(
                    request,
                    capsules=(capsule, request.capsules[1]),
                )

                with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
                    validate_global_candidate_payload(
                        payload(outcome="candidate_roots", request=rejected_request),
                        request=rejected_request,
                    )

    def test_raw_trace_temporal_origin_survives_ingestion_and_blocks_global_path(self):
        request = sample_request(
            decision_edge_fields={"edge_origin": "offline.temporal_reconstruction"},
            include_decision_source_ref=False,
        )
        path_edge = request.capsules[0].causal_path_edges[0]

        self.assertEqual(
            path_edge["edge_origin"], "offline.temporal_reconstruction"
        )
        with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
            validate_global_candidate_payload(
                payload(outcome="candidate_roots", request=request),
                request=request,
            )

    def test_conflicting_raw_provenance_is_preserved_and_blocks_global_path(self):
        conflicts = (
            ("relation", "produced", "temporal_adjacency"),
            ("evidence_type", "confirmed", "temporal_only"),
            ("edge_origin", "trace.dataflow_edges", "offline.temporal_reconstruction"),
            ("inference_method", "trace_dataflow_edge", "same_session_temporal_order"),
        )
        for field, recorded, temporal in conflicts:
            for top_level, metadata in ((recorded, temporal), (temporal, recorded)):
                with self.subTest(
                    field=field,
                    top_level=top_level,
                    metadata=metadata,
                ):
                    request = sample_request(
                        decision_edge_fields={
                            field: top_level,
                            "metadata": {field: metadata},
                        },
                        include_decision_source_ref=False,
                    )
                    path_edge = request.capsules[0].causal_path_edges[0]
                    provenance = path_edge.get("recorded_provenance")
                    self.assertIsInstance(provenance, Mapping)
                    if not isinstance(provenance, Mapping):
                        continue
                    self.assertEqual(
                        provenance["top_level"][field],
                        top_level,
                    )
                    self.assertEqual(
                        provenance["metadata"][field],
                        metadata,
                    )
                    with self.assertRaisesRegex(
                        ValueError, "causal_path_refs.*eligible.*hop"
                    ):
                        validate_global_candidate_payload(
                            payload(outcome="candidate_roots", request=request),
                            request=request,
                        )

    def test_global_path_requires_exact_boolean_true_eligibility(self):
        request = sample_request()
        for value in (False, "false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                capsule = replace(
                    request.capsules[0],
                    causal_path_edges=(
                        {
                            "from_ref": "record:decision",
                            "to_ref": "record:defect",
                            "relation": "produced",
                            "eligible_for_attribution": value,
                        },
                    ),
                    incoming_edges=(),
                    outgoing_edges=(),
                )
                rejected_request = replace(
                    request,
                    capsules=(capsule, request.capsules[1]),
                )

                with self.assertRaisesRegex(
                    ValueError, "causal_path_refs.*eligible.*hop"
                ):
                    validate_global_candidate_payload(
                        payload(outcome="candidate_roots", request=rejected_request),
                        request=rejected_request,
                    )

    def test_unresolved_raw_edge_evidence_is_missing_not_grounded(self):
        request = sample_request(
            decision_edge_fields={"evidence_refs": ["record:ghost"]}
        )

        self.assertIn("record:ghost", request.capsules[0].missing_evidence_refs)
        self.assertNotIn("record:ghost", request.grounded_refs)
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["evidence_refs"] = ["record:ghost"]
        value["decisive_evidence_refs"] = ["record:ghost"]
        with self.assertRaisesRegex(ValueError, "grounded refs"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_empty_missing_and_unknown_confirmation_relations(self):
        request = sample_request()
        for edge_fields in (
            {},
            {"relation": ""},
            {"relation": "unregistered_causal_guess"},
        ):
            with self.subTest(edge_fields=edge_fields):
                capsule = replace(
                    request.capsules[0],
                    causal_path_edges=(
                        {
                            "from_ref": "record:decision",
                            "to_ref": "record:defect",
                            "eligible_for_attribution": True,
                            **edge_fields,
                        },
                    ),
                    incoming_edges=(),
                    outgoing_edges=(),
                )
                rejected_request = replace(
                    request,
                    capsules=(capsule, request.capsules[1]),
                )

                with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
                    validate_global_candidate_payload(
                        payload(outcome="candidate_roots", request=rejected_request),
                        request=rejected_request,
                    )

    def test_rejects_mixed_noncausal_confirmation_path_hop(self):
        request = sample_request()
        capsule = replace(
            request.capsules[0],
            downstream_path=("record:decision", "record:action", "record:defect"),
            downstream_path_references=(
                request.capsules[0].downstream_path_references[0],
                {
                    "raw_ref": "record:action",
                    "resolved_ref": "record:action",
                    "canonical_ref": "record:action",
                    "resolution_status": "resolved",
                    "node": {"ref": "record:action"},
                },
                request.capsules[0].downstream_path_references[-1],
            ),
            causal_path_edges=(
                {
                    "from_ref": "record:decision",
                    "to_ref": "record:action",
                    "relation": "produced",
                    "eligible_for_attribution": True,
                },
                {
                    "from_ref": "record:action",
                    "to_ref": "record:defect",
                    "relation": "temporal_sequence",
                    "eligible_for_attribution": True,
                },
            ),
            incoming_edges=(),
            outgoing_edges=(),
        )
        rejected_request = replace(request, capsules=(capsule, request.capsules[1]))

        with self.assertRaisesRegex(ValueError, "causal_path_refs.*eligible.*hop"):
            validate_global_candidate_payload(
                payload(outcome="candidate_roots", request=rejected_request),
                request=rejected_request,
            )

    def test_accepts_eligible_causal_confirmation_relation(self):
        request = sample_request()
        for relation in (
            "produced",
            "derived_from",
            "motivated_by_evidence",
            "selected_by",
            "supports_claim",
        ):
            with self.subTest(relation=relation):
                capsule = replace(
                    request.capsules[0],
                    causal_path_edges=(
                        {
                            "from_ref": "record:decision",
                            "to_ref": "record:defect",
                            "relation": relation,
                            "eligible_for_attribution": True,
                        },
                    ),
                    incoming_edges=(),
                    outgoing_edges=(),
                )
                accepted_request = replace(
                    request,
                    capsules=(capsule, request.capsules[1]),
                )

                judgment = validate_global_candidate_payload(
                    payload(outcome="candidate_roots", request=accepted_request),
                    request=accepted_request,
                )

                self.assertEqual(judgment.outcome, "candidate_roots")

    def test_rejects_unselected_present_causal_candidate_without_path(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][1].update(
            {
                "defect_status": "present",
                "output_defect_status": "present",
                "causal_role": "contributing_condition",
                "causal_path_refs": [],
            }
        )

        with self.assertRaisesRegex(ValueError, "causal candidate.*causal_path_refs"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_root_whose_input_already_has_active_defect(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["input_defect_status"] = "present"

        with self.assertRaisesRegex(ValueError, "input defect not present"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_counterfactual_with_missing_prediction(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        del value["assessments"][0]["counterfactual"]["predicted_defect_status"]

        with self.assertRaisesRegex(ValueError, "counterfactual.*missing"):
            validate_global_candidate_payload(value, request=request)

    def test_compared_candidates_cover_all_open_eligible_refs_including_self(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        self.assertEqual(
            value["assessments"][0]["compared_candidate_refs"],
            ["record:decision"],
        )
        value["assessments"][0]["compared_candidate_refs"] = []

        with self.assertRaisesRegex(ValueError, "compared_candidate_refs"):
            validate_global_candidate_payload(value, request=request)

    def test_judgment_is_deeply_immutable_and_roundtrips_deterministically(self):
        request = sample_request()
        judgment = validate_global_candidate_payload(
            payload(outcome="candidate_roots", request=request), request=request
        )

        with self.assertRaises(TypeError):
            judgment.active_focus_binding["seed_ref"] = "record:neighbor"
        with self.assertRaises(TypeError):
            judgment.assessments[0].counterfactual["causal_effect"] = "changed"
        roundtripped = validate_global_candidate_payload(
            judgment.to_dict(), request=request
        )
        self.assertEqual(roundtripped, judgment)
        self.assertEqual(
            json.dumps(roundtripped.to_dict(), sort_keys=True),
            json.dumps(judgment.to_dict(), sort_keys=True),
        )

    def test_schema_version_is_v2(self):
        self.assertEqual(
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            "global-candidate-judgment/v2",
        )

    def test_v2_global_judgment_cache_hits_only_after_validated_write(self):
        request = sample_request()

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = 0

            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                self.calls += 1
                return TransportCallResult(
                    json.dumps(payload(outcome="candidate_roots", request=request)),
                    physical_requests=1,
                )

        transport = Transport()
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "global-cache.jsonl")
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            first = judge.judge_candidates_bounded(request, max_physical_requests=1)
            second = judge.judge_candidates_bounded(request, max_physical_requests=0)

            self.assertEqual(first.physical_requests, 1)
            self.assertEqual(second.physical_requests, 0)
            self.assertEqual(second.value, first.value)
            self.assertEqual(transport.calls, 1)
            self.assertEqual(cache.stats()["writes"], 1)
            self.assertEqual(cache.stats()["hits"], 1)

    def test_candidate_roots_require_present_root_assessment(self):
        request = sample_request()

        judgment = global_candidate_judgment_from_payload(
            payload(outcome="candidate_roots", request=request), request=request
        )

        self.assertEqual(judgment.outcome, "candidate_roots")
        self.assertEqual(judgment.selected_candidate_refs, ("record:decision",))

    def test_candidate_roots_reject_result_evidence_nodes(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0].update(
            {
                "defect_status": "absent",
                "output_defect_status": "absent",
                "causal_role": "unrelated",
                "counterfactual": counterfactual(
                    "record:decision", prevents_defect=False
                ),
            }
        )
        value["assessments"][1].update(
            {
                "defect_status": "present",
                "input_defect_status": "absent",
                "output_defect_status": "present",
                "causal_role": "root_candidate",
                "counterfactual": counterfactual(
                    "record:verification", prevents_defect=True
                ),
            }
        )
        value["selected_candidate_refs"] = ["record:verification"]
        value["decisive_evidence_refs"] = ["record:verification"]

        with self.assertRaisesRegex(ValueError, "ineligible"):
            validate_global_candidate_payload(value, request=request)

    def test_no_defect_requires_grounded_decisive_evidence(self):
        request = sample_request()
        value = payload(outcome="no_defect", request=request)

        judgment = global_candidate_judgment_from_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")
        self.assertEqual(judgment.decisive_evidence_refs, ("record:verification",))

        value["decisive_evidence_refs"] = []
        with self.assertRaisesRegex(ValueError, "decisive evidence"):
            validate_global_candidate_payload(value, request=request)

    def test_no_defect_allows_a_recorded_observation_refuted_by_counterevidence(self):
        request = sample_request()
        value = payload(outcome="no_defect", request=request)
        value["assessments"][0].update(
            {
                "defect_status": "present",
                "output_defect_status": "present",
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
        value = payload(outcome="no_defect", request=request)
        value["assessments"][1]["causal_role"] = "outcome_evidence"

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")

        value["decisive_evidence_refs"] = ["record:decision"]
        with self.assertRaisesRegex(ValueError, "decisive absent evidence"):
            validate_global_candidate_payload(value, request=request)

    def test_unknown_selected_candidate_is_rejected(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["selected_candidate_refs"] = ["record:not_offered"]

        with self.assertRaisesRegex(ValueError, "offered candidate"):
            validate_global_candidate_payload(value, request=request)

    def test_needs_expansion_requires_grounded_anchor_and_context_kind(self):
        request = sample_request()
        value = payload(outcome="needs_expansion", request=request)
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
        parsed = json.loads(prompt)

        self.assertIn("retrieval_is_not_causal_verdict=true", prompt)
        self.assertIn("rank and score are navigation evidence only", prompt)
        self.assertIn("no_defect", prompt)
        self.assertIn("needs_expansion", prompt)
        self.assertEqual(
            parsed["comparison_then_selection"],
            [
                "compare_input_and_output_defect_status",
                "ground_causal_paths",
                "evaluate_counterfactual_predictions",
                "compare_all_open_authored_root_candidates_including_self",
                "select_candidate_roots_last",
            ],
        )

    def test_prompt_distinguishes_active_defect_truth_from_record_presence(self):
        prompt = build_global_candidate_prompt(sample_request())
        parsed = json.loads(prompt)

        self.assertIn("whether the active defect is true", prompt)
        self.assertIn("record itself exists", prompt)
        self.assertIn("would change the current judgment", prompt)
        self.assertIn("root_candidate_eligible=false", prompt)
        self.assertEqual(
            parsed["request"]["active_defect"]["actual"],
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

    def test_claude_judge_fallback_is_a_valid_v2_bound_judgment(self):
        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                return TransportCallResult("{}", physical_requests=1)

        request = sample_request()
        payloads = []
        validator = validate_global_candidate_payload

        def record_validation(value, *, request):
            payloads.append(value)
            return validator(value, request=request)

        with patch(
            "trace_attribution.causal_judge.validate_global_candidate_payload",
            side_effect=record_validation,
        ):
            result = ClaudeCausalJudge(
                transport=Transport(), cache=JudgmentCache()
            ).judge_candidates_bounded(request, max_physical_requests=1)

        self.assertEqual(result.value.outcome, "inconclusive")
        self.assertIn(
            "inconclusive",
            [item.get("outcome") for item in payloads if isinstance(item, dict)],
        )
        self.assertEqual(
            validate_global_candidate_payload(
                result.value.to_dict(), request=request
            ),
            result.value,
        )


if __name__ == "__main__":
    unittest.main()
