from __future__ import annotations

import copy
import unittest
import json
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trace_attribution.cache import JudgmentCache
from trace_attribution import causal_judge as causal_judge_module
from trace_attribution import candidate_paging as candidate_paging_module
from trace_attribution import evidence_capsule
from trace_attribution import global_judge as global_judge_module
from trace_attribution.causal_judge import BoundedJudgeCallError, ClaudeCausalJudge
from trace_attribution.causal_state import (
    CausalCandidate,
    seed_defect_state,
)
from trace_attribution.confirmation_path import (
    is_confirmation_causal_edge,
)
from trace_attribution.evidence_capsule import (
    CAPSULE_SCHEMA_VERSION,
    build_candidate_evidence_capsules,
)
from trace_attribution.global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    build_global_candidate_prompt,
    canonicalize_global_candidate_structural_bindings,
    global_candidate_comparison_contract_from_context,
    global_candidate_request_from_validation_envelope,
    global_candidate_judgment_from_payload,
    normalize_active_focus_text,
    validate_global_candidate_request_against_graph,
    validate_global_candidate_payload,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.errors import TransportCallResult
from trace_attribution.models import stable_json
from trace_attribution.restoration_obligation import RestorationObligation


class GlobalPageRepairBudgetTest(unittest.TestCase):
    def test_page_budget_covers_the_full_semantic_repair_loop(self) -> None:
        self.assertGreaterEqual(
            candidate_paging_module
            .GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP,
            causal_judge_module.MAX_SEMANTIC_REPAIR_ATTEMPTS,
        )


def sample_request(
    *,
    decision_edge_fields: dict | None = None,
    include_decision_source_ref: bool = True,
    return_context: bool = False,
):
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
                "data": {"actual": "Started cleanup is interrupted."},
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
    objective = (
        "Find the trace-visible root or determine that the observed defect "
        "is contradicted."
    )
    defect = seed_defect_state(graph.nodes["record:defect"], objective)
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
    request = GlobalCandidateJudgeRequest(
        case_id="global-judge-case",
        objective=objective,
        analysis_perspective="task quality",
        seed_ref="record:defect",
        active_defect=defect,
        active_focus_text=defect.actual,
        active_focus_text_hash=active_focus_text_sha256(defect.actual),
        start_refs=("record:defect",),
        capsules=capsules,
    )
    if return_context:
        return graph, tuple(candidates), request
    return request


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
    objective = "Select no more than three authored root candidates."
    defect = seed_defect_state(graph.nodes["record:defect"], objective)
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
        objective=objective,
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
                "responsibility": "primary",
                "candidate_phase": "implementation",
                "obligation_status_before": "unknown",
                "obligation_status_after": "unknown",
                "repair_window_effect": "remained_open",
                "failure_mode": "positive_introduction",
                "obligation_refs": [],
                "contribution_mechanism": None,
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


def root_role_chain_request(
    *, failure_kind: str = "functional"
) -> GlobalCandidateJudgeRequest:
    refs = (
        "record:dec_147",
        "record:dec_159",
        "record:verification_omission",
        "record:dec_343",
    )
    records = [
        {
            "record_id": "dec_147",
            "component": "agent",
            "event_type": "decision",
            "data": {
                "phase": "planning",
                "rationale": "Introduce the functional implementation strategy.",
            },
        },
        {
            "record_id": "dec_159",
            "component": "agent",
            "event_type": "decision",
            "data": {
                "phase": "implementation",
                "rationale": "Apply the strategy to the implementation.",
            },
        },
        {
            "record_id": "verification_omission",
            "component": "agent",
            "event_type": "decision",
            "data": {
                "phase": "verification",
                "rationale": "Omit the independent functional verification.",
            },
        },
        {
            "record_id": "dec_343",
            "component": "agent",
            "event_type": "decision",
            "data": {
                "phase": "closure",
                "rationale": "Close the task despite the unverified behavior.",
            },
        },
        {
            "record_id": "defect",
            "component": "evaluation",
            "event_type": "case.observed_defect",
            "data": {
                "failure_type": failure_kind,
                "expected": "The implementation preserves the required behavior.",
                "actual": "The implementation violates the required behavior.",
            },
        },
    ]
    path = (*refs, "record:defect")
    trace = {
        "case_id": "root-role-chain-{0}".format(failure_kind),
        "records": records,
        "dataflow_edges": [
            {
                "from": {
                    "type": "record",
                    "id": source.removeprefix("record:"),
                },
                "to": {
                    "type": "record",
                    "id": target.removeprefix("record:"),
                },
                "relation": (
                    "decision_exposed_by_evaluation"
                    if target == "record:defect"
                    else "decision_guided_change"
                ),
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
            for source, target in zip(path, path[1:])
        ],
    }
    graph = TraceGraph.from_trace(trace)
    objective = "Find the root for the active failure signature."
    defect = seed_defect_state(graph.nodes["record:defect"], objective)
    candidates = tuple(
        CausalCandidate(
            ref=ref,
            node=graph.nodes[ref],
            source="confirmed_edge",
            score=1.0,
            evidence_refs=(ref,),
        )
        for ref in refs
    )
    capsules = build_candidate_evidence_capsules(
        graph=graph,
        candidates=candidates,
        defect_state=defect,
        downstream_paths={
            ref: path[path.index(ref) :]
            for ref in refs
        },
        start_refs=("record:defect",),
    )
    return GlobalCandidateJudgeRequest(
        case_id=trace["case_id"],
        objective=objective,
        analysis_perspective="task quality",
        seed_ref="record:defect",
        active_defect=defect,
        active_focus_text=defect.actual,
        active_focus_text_hash=active_focus_text_sha256(defect.actual),
        start_refs=("record:defect",),
        capsules=capsules,
    )


def root_role_chain_payload(
    request: GlobalCandidateJudgeRequest, *, selected_ref: str
) -> dict:
    compared_refs = list(request.open_authored_root_candidate_refs)
    assessments = []
    for capsule in request.capsules:
        selected = capsule.candidate_ref == selected_ref
        evidence_refs = list(capsule.downstream_path[:2])
        recorded_phase = str(
            capsule.candidate["node"]["data"].get("phase")
            or "intermediate"
        )
        assessments.append(
            {
                "candidate_ref": capsule.candidate_ref,
                "defect_status": "present",
                "input_defect_status": "absent" if selected else "present",
                "output_defect_status": "present",
                "causal_path_refs": list(capsule.downstream_path),
                "counterfactual": counterfactual(
                    capsule.candidate_ref,
                    prevents_defect=selected,
                ),
                "compared_candidate_refs": compared_refs,
                "causal_role": (
                    "root_candidate" if selected else "contributing_condition"
                ),
                "responsibility": "primary" if selected else "shared",
                "candidate_phase": (
                    recorded_phase
                    if recorded_phase
                    in {
                        "diagnostic",
                        "planning",
                        "intermediate",
                        "implementation",
                        "closure",
                        "final",
                    }
                    else "intermediate"
                ),
                "obligation_status_before": "unknown",
                "obligation_status_after": "unknown",
                "repair_window_effect": "remained_open",
                "failure_mode": (
                    "positive_introduction"
                    if selected
                    else "omission_enabling_condition"
                ),
                "obligation_refs": [],
                "contribution_mechanism": (
                    None
                    if selected
                    else {
                        "type": "repair_opportunity_consumption",
                        "target_ref": evidence_refs[-1],
                        "effect": "The later decision preserves the active defect.",
                        "evidence_refs": evidence_refs,
                    }
                ),
                "reason": "Compare the candidate against the active signature.",
                "evidence_refs": [capsule.candidate_ref],
                "confidence": 0.9,
            }
        )
    return {
        "outcome": "candidate_roots",
        "reason": "One authored candidate requires independent confirmation.",
        "active_focus_binding": {
            "seed_ref": request.seed_ref,
            "defect_fingerprint": request.active_defect.fingerprint,
            "active_focus_text_hash": request.active_focus_text_hash,
        },
        "assessments": assessments,
        "selected_candidate_refs": [selected_ref],
        "expansion_requests": [],
        "decisive_evidence_refs": [selected_ref],
        "missing_evidence": [],
        "confidence": 0.9,
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
    objective = "Keep evidence-only nodes out of root confirmation."
    defect = seed_defect_state(graph.nodes["record:defect"], objective)
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
        objective=objective,
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
    is_root = causal_role == "root_candidate"
    is_factor = causal_role in {
        "contributing_condition",
        "amplifying_factor",
    }
    return {
        "candidate_ref": ref,
        "defect_status": defect_status,
        "causal_role": causal_role,
        "responsibility": (
            "primary" if is_root else "shared" if is_factor else "none"
        ),
        "candidate_phase": (
            "implementation" if is_root else "planning" if is_factor else "intermediate"
        ),
        "obligation_status_before": "unknown",
        "obligation_status_after": "unknown",
        "repair_window_effect": "remained_open",
        "failure_mode": (
            "positive_introduction"
            if is_root
            else "omission_enabling_condition"
            if is_factor
            else "none"
        ),
        "obligation_refs": [],
        "contribution_mechanism": (
            {
                "type": "scope_narrowing",
                "target_ref": evidence_refs[0],
                "effect": "The candidate narrows the downstream repair scope.",
                "evidence_refs": evidence_refs,
            }
            if is_factor
            else None
        ),
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
        is_open_no_defect = (
            outcome == "no_defect"
            and item["candidate_ref"]
            in request.open_authored_root_candidate_refs
        )
        item.update(
            {
                "input_defect_status": (
                    "absent"
                    if is_root or is_open_no_defect
                    else "unknown"
                ),
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
    def test_functional_signature_rejects_false_closure_substitution(self):
        request = root_role_chain_request(failure_kind="functional")

        with self.assertRaisesRegex(ValueError, "closure substitution"):
            validate_global_candidate_payload(
                root_role_chain_payload(
                    request,
                    selected_ref="record:dec_343",
                ),
                request=request,
            )

    def test_functional_signature_selects_introduction_and_projects_closure_factor(self):
        request = root_role_chain_request(failure_kind="functional")

        judgment = validate_global_candidate_payload(
            root_role_chain_payload(
                request,
                selected_ref="record:dec_147",
            ),
            request=request,
        )
        bindings = {
            item.candidate_ref: item
            for item in getattr(
                judgment,
                "active_failure_role_bindings",
                (),
            )
        }

        self.assertEqual(
            set(bindings),
            {
                "record:dec_147",
                "record:dec_159",
                "record:verification_omission",
                "record:dec_343",
            },
        )
        self.assertEqual(
            bindings["record:dec_147"].causal_role,
            "defect_introduction_root",
        )
        self.assertEqual(bindings["record:dec_147"].disposition, "root")
        self.assertEqual(
            bindings["record:dec_343"].causal_role,
            "false_closure",
        )
        self.assertEqual(bindings["record:dec_343"].disposition, "factor")

    def test_acceptance_signature_may_select_false_closure_as_its_root(self):
        request = root_role_chain_request(failure_kind="acceptance")

        judgment = validate_global_candidate_payload(
            root_role_chain_payload(
                request,
                selected_ref="record:dec_343",
            ),
            request=request,
        )
        binding = next(
            (
                item
                for item in getattr(
                    judgment,
                    "active_failure_role_bindings",
                    (),
                )
                if item.candidate_ref == "record:dec_343"
            ),
            None,
        )

        self.assertIsNotNone(binding)
        self.assertEqual(binding.causal_role, "false_closure")
        self.assertEqual(binding.disposition, "root")
        self.assertEqual(
            binding.counterfactual_prevention_signatures,
            (request.active_defect.fingerprint,),
        )

    def test_two_signatures_have_independent_roots_and_shared_factors(self):
        functional = root_role_chain_request(failure_kind="functional")
        acceptance = root_role_chain_request(failure_kind="acceptance")
        requests_and_roots = (
            (functional, "record:dec_147"),
            (acceptance, "record:dec_343"),
        )
        root_bindings = []
        shared_bindings = []
        for request, selected_ref in requests_and_roots:
            judgment = validate_global_candidate_payload(
                root_role_chain_payload(
                    request,
                    selected_ref=selected_ref,
                ),
                request=request,
            )
            bindings = {
                item.candidate_ref: item
                for item in getattr(
                    judgment,
                    "active_failure_role_bindings",
                    (),
                )
            }
            self.assertEqual(
                set(bindings),
                {
                    "record:dec_147",
                    "record:dec_159",
                    "record:verification_omission",
                    "record:dec_343",
                },
            )
            root_bindings.extend(
                item
                for item in bindings.values()
                if item.disposition == "root"
            )
            shared_bindings.append(bindings["record:dec_159"])

        self.assertEqual(
            [(item.candidate_ref, item.failure_signature) for item in root_bindings],
            [
                ("record:dec_147", functional.active_defect.fingerprint),
                ("record:dec_343", acceptance.active_defect.fingerprint),
            ],
        )
        self.assertTrue(
            all(
                item.counterfactual_prevention_signatures
                == (item.failure_signature,)
                for item in root_bindings
            )
        )
        self.assertTrue(
            all(item.disposition == "factor" for item in shared_bindings)
        )
        self.assertEqual(
            {item.causal_role for item in shared_bindings},
            {"amplifying_condition"},
        )

    def test_role_bindings_are_stable_under_candidate_permutation(self):
        request = root_role_chain_request(failure_kind="functional")
        reversed_request = replace(
            request,
            capsules=tuple(reversed(request.capsules)),
        )

        original = validate_global_candidate_payload(
            root_role_chain_payload(
                request,
                selected_ref="record:dec_147",
            ),
            request=request,
        )
        permuted = validate_global_candidate_payload(
            root_role_chain_payload(
                reversed_request,
                selected_ref="record:dec_147",
            ),
            request=reversed_request,
        )

        self.assertTrue(
            hasattr(original, "active_failure_role_bindings")
            and hasattr(permuted, "active_failure_role_bindings")
        )
        self.assertEqual(
            [item.to_dict() for item in original.active_failure_role_bindings],
            [item.to_dict() for item in permuted.active_failure_role_bindings],
        )

    def test_global_request_contains_bounded_tiered_factual_context_without_human_labels(self):
        request = root_role_chain_request(failure_kind="functional")

        factual = request.to_dict().get("factual_context")

        self.assertIsInstance(factual, dict)
        self.assertEqual(
            set(factual),
            {
                "schema",
                "failure_signature",
                "obligations",
                "plans",
                "edits",
                "verifications",
                "outcomes",
                "competitors",
                "tiered_paths",
            },
        )
        self.assertEqual(
            factual["failure_signature"]["fingerprint"],
            request.active_defect.fingerprint,
        )
        self.assertNotIn("active_role_binding", factual)
        self.assertNotIn("causal_role", stable_json(factual))
        self.assertTrue(factual["plans"])
        self.assertTrue(factual["verifications"])
        self.assertTrue(factual["outcomes"])
        self.assertEqual(len(factual["tiered_paths"]), 4)
        for path in factual["tiered_paths"]:
            self.assertIn(
                path["tier"],
                {"confirmed_trace", "offline_reconstruction", "mixed"},
            )
            self.assertTrue(path["segments"])
            self.assertTrue(
                all(
                    segment["provenance_tier"]
                    in {"confirmed_trace", "offline_reconstruction"}
                    for segment in path["segments"]
                )
            )
        serialized = stable_json(factual).lower()
        self.assertNotIn("human_root", serialized)
        self.assertNotIn("ground_truth_root", serialized)

    def test_mixed_unlabelled_path_provenance_fails_closed(self):
        request = root_role_chain_request(failure_kind="functional")
        capsule = request.capsules[0]
        stripped_edges = []
        for edge in capsule.causal_path_edges:
            value = dict(edge)
            if value.get("from_ref") == "record:dec_159":
                for key in (
                    "edge_origin",
                    "source_container",
                    "evidence_type",
                    "inference_method",
                    "recorded_provenance",
                ):
                    value.pop(key, None)
            stripped_edges.append(value)
        unlabelled = replace(
            capsule,
            causal_path_edges=tuple(stripped_edges),
            outgoing_edges=tuple(stripped_edges),
        )
        mixed_request = replace(
            request,
            capsules=(unlabelled, *request.capsules[1:]),
        )

        with self.assertRaisesRegex(ValueError, "unlabelled path provenance"):
            mixed_request.to_dict()

    def test_selected_responsible_omission_must_reference_request_obligation(self):
        request = sample_request()
        obligation = RestorationObligation.create(
            obligation_id="restore-sigterm-cleanup",
            kind="observed_defect_remediation",
            baseline_state="preexisting_missing",
            required_end_state="cleanup completes before task closure",
            required_capabilities=("signal_cleanup",),
            scope_refs=("record:decision",),
            acceptance_evidence_refs=("record:defect",),
            provenance={
                "source": "external_quality_review",
                "source_refs": ("record:decision",),
                "derivation": "reviewed active defect",
            },
        )
        request = replace(
            request,
            restoration_obligations=(obligation,),
        )
        value = payload(outcome="candidate_roots", request=request)
        root = value["assessments"][0]
        root.update(
            {
                "input_defect_status": "present",
                "responsibility": "primary",
                "candidate_phase": "closure",
                "obligation_status_before": "pending",
                "obligation_status_after": "violated",
                "repair_window_effect": "closed",
                "failure_mode": "responsible_omission",
                "obligation_refs": [obligation.obligation_id],
            }
        )
        root["counterfactual"]["intervention_kind"] = (
            "replace_with_obligation_satisfying_behavior"
        )

        judgment = validate_global_candidate_payload(
            value,
            request=request,
        )

        self.assertEqual(
            judgment.assessments[0].failure_mode,
            "responsible_omission",
        )

        root["obligation_refs"] = ["restore-unseen-obligation"]
        with self.assertRaisesRegex(ValueError, "current request"):
            validate_global_candidate_payload(value, request=request)

    def test_responsible_omission_root_allows_preexisting_defect_at_closure(self):
        assessed = global_judge_module.GlobalCandidateAssessment(
            candidate_ref="record:decision",
            defect_status="present",
            input_defect_status="present",
            output_defect_status="present",
            causal_path_refs=("record:decision", "record:defect"),
            counterfactual={
                "intervention_ref": "record:decision",
                "intervention_kind": (
                    "replace_with_obligation_satisfying_behavior"
                ),
                "predicted_defect_status": "absent",
                "causal_effect": "prevents_defect",
            },
            compared_candidate_refs=("record:decision",),
            causal_role="root_candidate",
            responsibility="primary",
            candidate_phase="closure",
            obligation_status_before="pending",
            obligation_status_after="violated",
            repair_window_effect="closed",
            failure_mode="responsible_omission",
            obligation_refs=("obligation:restore-sigterm-cleanup",),
            contribution_mechanism=None,
            reason=(
                "The task closed while the assigned restoration obligation "
                "remained violated."
            ),
            evidence_refs=("record:decision", "record:defect"),
            confidence=0.9,
        )

        self.assertEqual(assessed.failure_mode, "responsible_omission")
        self.assertEqual(assessed.input_defect_status, "present")

    def test_responsible_omission_root_rejects_open_repair_window(self):
        with self.assertRaisesRegex(ValueError, "repair window"):
            global_judge_module.GlobalCandidateAssessment(
                candidate_ref="record:decision",
                defect_status="present",
                input_defect_status="present",
                output_defect_status="present",
                causal_path_refs=("record:decision", "record:defect"),
                counterfactual={
                    "intervention_ref": "record:decision",
                    "intervention_kind": (
                        "replace_with_obligation_satisfying_behavior"
                    ),
                    "predicted_defect_status": "absent",
                    "causal_effect": "prevents_defect",
                },
                compared_candidate_refs=("record:decision",),
                causal_role="root_candidate",
                responsibility="primary",
                candidate_phase="intermediate",
                obligation_status_before="pending",
                obligation_status_after="violated",
                repair_window_effect="remained_open",
                failure_mode="responsible_omission",
                obligation_refs=("obligation:restore-sigterm-cleanup",),
                contribution_mechanism=None,
                reason="The task could still repair the defect.",
                evidence_refs=("record:decision",),
                confidence=0.8,
            )

    def test_ordinary_non_repair_is_not_a_contributing_condition(self):
        assessed = global_judge_module.GlobalCandidateAssessment(
            candidate_ref="record:diagnosis",
            defect_status="present",
            input_defect_status="present",
            output_defect_status="present",
            causal_path_refs=("record:diagnosis", "record:defect"),
            counterfactual=counterfactual(
                "record:diagnosis",
                prevents_defect=False,
            ),
            compared_candidate_refs=("record:diagnosis",),
            causal_role="unrelated",
            responsibility="none",
            candidate_phase="diagnostic",
            obligation_status_before="pending",
            obligation_status_after="pending",
            repair_window_effect="remained_open",
            failure_mode="ordinary_non_repair",
            obligation_refs=(),
            contribution_mechanism=None,
            reason="A diagnostic observation did not close the repair window.",
            evidence_refs=("record:diagnosis",),
            confidence=0.8,
        )

        self.assertEqual(assessed.failure_mode, "ordinary_non_repair")
        self.assertEqual(assessed.causal_role, "unrelated")

    def test_contributing_condition_requires_grounded_mechanism(self):
        with self.assertRaisesRegex(ValueError, "contribution mechanism"):
            global_judge_module.GlobalCandidateAssessment(
                candidate_ref="record:decision",
                defect_status="present",
                input_defect_status="present",
                output_defect_status="present",
                causal_path_refs=("record:decision", "record:defect"),
                counterfactual=counterfactual(
                    "record:decision",
                    prevents_defect=False,
                ),
                compared_candidate_refs=("record:decision",),
                causal_role="contributing_condition",
                responsibility="shared",
                candidate_phase="planning",
                obligation_status_before="pending",
                obligation_status_after="pending",
                repair_window_effect="remained_open",
                failure_mode="omission_enabling_condition",
                obligation_refs=(),
                contribution_mechanism=None,
                reason="The decision allegedly narrowed later repair scope.",
                evidence_refs=("record:decision",),
                confidence=0.8,
            )

    def test_no_root_candidates_preserves_active_defect_without_selecting_page_roots(self):
        request = sample_request()
        value = payload(
            outcome="no_root_candidates",
            request=request,
        )
        value["decisive_evidence_refs"] = [
            capsule.candidate_ref for capsule in request.capsules
        ]
        for item in value["assessments"]:
            item["defect_status"] = "present"
            item["input_defect_status"] = "present"
            item["output_defect_status"] = "present"
            item["causal_role"] = (
                "outcome_evidence"
                if item["candidate_ref"] == "record:verification"
                else "unrelated"
            )
            item["counterfactual"] = counterfactual(
                item["candidate_ref"],
                prevents_defect=False,
            )

        judgment = validate_global_candidate_payload(
            value,
            request=request,
        )

        self.assertEqual(judgment.outcome, "no_root_candidates")
        self.assertEqual(judgment.selected_candidate_refs, ())
        self.assertTrue(
            all(
                item.output_defect_status == "present"
                for item in judgment.assessments
            )
        )

    def test_custom_confirmed_dataflow_is_causal_but_retrieval_route_is_not(self):
        confirmed = {
            "relation": "authored_decision_observed_by_evaluation",
            "evidence_type": "confirmed",
            "eligible_for_attribution": True,
        }

        self.assertTrue(
            is_confirmation_causal_edge(
                confirmed,
                default_eligible=False,
            )
        )
        self.assertFalse(
            is_confirmation_causal_edge(
                {
                    **confirmed,
                    "retrieval_candidate": True,
                    "edge_origin": "offline.semantic_retrieval",
                },
                default_eligible=False,
            )
        )

    def test_evidence_context_capsules_are_grounded_but_not_assessed(self):
        request = sample_request()
        decision, verification = request.capsules

        separated = replace(
            request,
            capsules=(decision,),
            evidence_context_capsules=(verification,),
        )
        payload = separated.to_dict()
        contract = global_candidate_comparison_contract_from_context(
            payload
        )

        self.assertEqual(
            separated.offered_candidate_refs,
            ("record:decision",),
        )
        self.assertIn("record:verification", separated.grounded_refs)
        self.assertEqual(
            [
                item["candidate_ref"]
                for item in contract["assessment_requirements"]
            ],
            ["record:decision"],
        )
        self.assertEqual(
            [
                item["candidate_ref"]
                for item in payload["evidence_context_capsules"]
            ],
            ["record:verification"],
        )

    def test_comparison_contract_rejects_noncausal_progress_path(self):
        contract = global_candidate_comparison_contract_from_context(
            {
                "seed_ref": "record:defect",
                "active_defect": {"fingerprint": "defect-1"},
                "active_focus_text_hash": "f" * 64,
                "open_authored_root_candidate_refs": [],
                "candidate_evidence_capsules": [
                    {
                        "candidate_ref": "record:progress",
                        "candidate": {
                            "root_candidate_eligible": True,
                        },
                        "downstream_path": [
                            "record:progress",
                            "record:defect",
                        ],
                        "causal_path_edges": [
                            {
                                "from_ref": "record:progress",
                                "to_ref": "record:defect",
                                "relation": (
                                    "progress_episode_projects_to_target"
                                ),
                                "eligible_for_attribution": True,
                            }
                        ],
                        "incoming_edges": [],
                        "outgoing_edges": [],
                    }
                ],
            }
        )

        requirement = contract["assessment_requirements"][0]
        self.assertEqual(
            requirement["required_causal_path_refs"],
            [],
        )
        self.assertEqual(
            requirement["required_compared_candidate_refs"],
            [],
        )
        self.assertFalse(
            requirement["open_authored_root_candidate"],
        )
        for causal_role in (
            "root_candidate",
            "contributing_condition",
            "amplifying_factor",
        ):
            self.assertNotIn(
                causal_role,
                requirement["allowed_causal_roles"],
            )

    def test_structural_binding_canonicalization_changes_only_request_owned_fields(self):
        request = sample_request()
        original = payload(outcome="candidate_roots", request=request)
        assessment = original["assessments"][0]
        assessment["defect_status"] = "absent"
        assessment["causal_path_refs"] = [
            "record:decision",
            "record:verification",
        ]
        assessment["compared_candidate_refs"] = []
        assessment["counterfactual"]["intervention_ref"] = "record:wrong"
        assessment["counterfactual"]["intervention_kind"] = "delete_candidate"
        original["active_focus_binding"] = {
            "seed_ref": "record:wrong",
            "defect_fingerprint": "wrong",
            "active_focus_text_hash": "wrong",
        }
        before = copy.deepcopy(original)

        normalized, corrections = (
            canonicalize_global_candidate_structural_bindings(
                original,
                request=request,
            )
        )

        self.assertEqual(original, before)
        normalized_assessment = normalized["assessments"][0]
        self.assertEqual(
            normalized["active_focus_binding"],
            {
                "seed_ref": request.seed_ref,
                "defect_fingerprint": request.active_defect.fingerprint,
                "active_focus_text_hash": request.active_focus_text_hash,
            },
        )
        self.assertEqual(
            normalized_assessment["causal_path_refs"],
            list(request.capsules[0].downstream_path),
        )
        self.assertEqual(
            normalized_assessment["compared_candidate_refs"],
            list(request.open_authored_root_candidate_refs),
        )
        self.assertEqual(
            normalized_assessment["counterfactual"]["intervention_ref"],
            request.capsules[0].candidate_ref,
        )
        self.assertEqual(
            normalized_assessment["counterfactual"]["intervention_kind"],
            "delete_candidate",
        )
        self.assertEqual(
            normalized_assessment["defect_status"],
            normalized_assessment["output_defect_status"],
        )
        for semantic_field in (
            "input_defect_status",
            "output_defect_status",
            "causal_role",
            "reason",
            "evidence_refs",
            "confidence",
        ):
            self.assertEqual(
                normalized_assessment[semantic_field],
                before["assessments"][0][semantic_field],
            )
        self.assertEqual(
            normalized_assessment["counterfactual"][
                "predicted_defect_status"
            ],
            before["assessments"][0]["counterfactual"][
                "predicted_defect_status"
            ],
        )
        self.assertEqual(
            normalized_assessment["counterfactual"]["causal_effect"],
            before["assessments"][0]["counterfactual"]["causal_effect"],
        )
        for semantic_field in (
            "outcome",
            "reason",
            "selected_candidate_refs",
            "decisive_evidence_refs",
            "missing_evidence",
            "confidence",
        ):
            self.assertEqual(
                normalized[semantic_field],
                before[semantic_field],
            )
        self.assertEqual(
            {item["field"] for item in corrections},
            {
                "active_focus_binding",
                "defect_status",
                "causal_path_refs",
                "compared_candidate_refs",
                "counterfactual.intervention_ref",
            },
        )

    def test_structural_binding_canonicalization_does_not_launder_semantic_evidence(self):
        request = sample_request()
        original = payload(outcome="candidate_roots", request=request)
        original["decisive_evidence_refs"] = ["record:not-grounded"]
        original["assessments"][0]["evidence_refs"] = [
            "record:not-grounded"
        ]

        normalized, _ = canonicalize_global_candidate_structural_bindings(
            original,
            request=request,
        )

        self.assertEqual(
            normalized["decisive_evidence_refs"],
            ["record:not-grounded"],
        )
        self.assertEqual(
            normalized["assessments"][0]["evidence_refs"],
            ["record:not-grounded"],
        )
        with self.assertRaisesRegex(ValueError, "grounded"):
            validate_global_candidate_payload(normalized, request=request)

    def test_claude_judge_canonicalizes_structural_bindings_before_validation(self):
        request = sample_request()
        invalid = payload(outcome="candidate_roots", request=request)
        assessment = invalid["assessments"][0]
        assessment["defect_status"] = "absent"
        assessment["causal_path_refs"] = [
            "record:decision",
            "record:verification",
        ]
        assessment["compared_candidate_refs"] = []
        assessment["counterfactual"]["intervention_ref"] = "record:wrong"
        assessment["counterfactual"]["intervention_kind"] = (
            "replace_with_semantically_correct_behavior"
        )
        invalid["active_focus_binding"] = {
            "seed_ref": "record:wrong",
            "defect_fingerprint": "wrong",
            "active_focus_text_hash": "wrong",
        }

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = []

            def create_message_text_with_usage(
                self,
                *,
                system,
                messages,
                max_tokens,
            ):
                self.calls.append(
                    {
                        "system": system,
                        "messages": messages,
                        "max_tokens": max_tokens,
                    }
                )
                return TransportCallResult(
                    json.dumps(invalid),
                    physical_requests=1,
                )

        transport = Transport()
        result = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        ).judge_candidates_bounded(
            request,
            max_physical_requests=1,
        )

        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            result.value.assessments[0].causal_path_refs,
            request.capsules[0].downstream_path,
        )
        self.assertEqual(
            result.value.assessments[0].compared_candidate_refs,
            request.open_authored_root_candidate_refs,
        )
        self.assertEqual(
            {
                item["field"]
                for item in result.diagnostics[
                    "structural_corrections"
                ]
            },
            {
                "active_focus_binding",
                "defect_status",
                "causal_path_refs",
                "compared_candidate_refs",
                "counterfactual.intervention_ref",
            },
        )

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

    def test_synthetic_validation_envelope_binds_to_active_retrieval_output(self):
        graph, candidates, request = sample_request(return_context=True)
        envelope = request.validation_envelope()
        capsule = envelope["candidate_evidence_capsules"][0]
        capsule["candidate"]["source"] = "semantic_fallback"
        capsule["validation_source"]["candidate_source"] = "semantic_fallback"

        with self.assertRaisesRegex(ValueError, "authoritative retrieval route"):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=candidates,
                authoritative_objective=request.objective,
            )

    def test_valid_synthetic_envelope_requires_active_routes_before_judge_or_anchors(self):
        graph, candidates, request = sample_request(return_context=True)
        envelope = request.validation_envelope()

        with self.assertRaisesRegex(ValueError, "authoritative retrieval route"):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_objective=request.objective,
            )

        restored = global_candidate_request_from_validation_envelope(
            envelope,
            graph=graph,
            authoritative_candidates=candidates,
            authoritative_objective=request.objective,
        )
        self.assertEqual(restored.grounded_refs, request.grounded_refs)

    def test_recorded_envelope_rejects_persisted_evidence_membership_drift(self):
        graph, _, request = sample_request(return_context=True)
        candidates = tuple(
            CausalCandidate(
                ref=ref,
                node=graph.nodes[ref],
                source="confirmed_edge",
                edge=graph.edge_context(ref, "record:defect")[-1],
                score=1.0,
                evidence_refs=(ref, "record:defect"),
            )
            for ref in ("record:decision", "record:verification")
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=candidates,
            defect_state=request.active_defect,
            downstream_paths={
                ref: (ref, "record:defect")
                for ref in ("record:decision", "record:verification")
            },
            start_refs=("record:defect",),
        )
        recorded_request = replace(request, capsules=capsules)
        envelope = recorded_request.validation_envelope()
        envelope["candidate_evidence_capsules"][0]["validation_source"][
            "candidate_evidence_refs"
        ].append("record:decision")

        with self.assertRaisesRegex(ValueError, "authoritative recorded route"):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=candidates,
                authoritative_objective=recorded_request.objective,
            )

        restored = global_candidate_request_from_validation_envelope(
            recorded_request.validation_envelope(),
            graph=graph,
            authoritative_candidates=candidates,
            authoritative_objective=recorded_request.objective,
        )
        self.assertEqual(restored, recorded_request)
        self.assertEqual(restored.grounded_refs, recorded_request.grounded_refs)

    def test_ineligible_action_member_never_reaches_judge_grounding_or_anchors(self):
        trace = {
            "case_id": "action-group-evidence-policy",
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "call_id": "shared-call",
                        "rationale": "Use the recorded implementation plan.",
                    },
                },
                {
                    "record_id": "audit_only_external",
                    "component": "evaluation",
                    "event_type": "external.evaluation_fact",
                    "data": {
                        "call_id": "shared-call",
                        "status": "failed",
                        "subject_revision": "git:stale",
                        "revision_status": "mismatched",
                        "observation": "AUDIT_ONLY_SHARED_CALL_MEMBER",
                    },
                },
                {
                    "record_id": "defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:decision"],
                    "data": {"actual": "The active defect is present."},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "defect"},
                    "relation": "decision_exposed_by_evaluation",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        objective = "Judge the active candidate."
        defect = seed_defect_state(graph.nodes["record:defect"], objective)
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=graph.edge_context("record:decision", "record:defect")[-1],
            score=1.0,
            evidence_refs=("record:decision",),
        )
        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect,
            downstream_paths={
                "record:decision": ("record:decision", "record:defect")
            },
            start_refs=("record:defect",),
        )[0]
        request = GlobalCandidateJudgeRequest(
            case_id=trace["case_id"],
            objective=objective,
            analysis_perspective="task quality",
            seed_ref="record:defect",
            active_defect=defect,
            active_focus_text=defect.actual,
            active_focus_text_hash=active_focus_text_sha256(defect.actual),
            start_refs=("record:defect",),
            capsules=(capsule,),
        )

        self.assertNotIn("record:audit_only_external", request.grounded_refs)
        self.assertNotIn("AUDIT_ONLY_SHARED_CALL_MEMBER", str(request.to_dict()))
        restored = global_candidate_request_from_validation_envelope(
            request.validation_envelope(),
            graph=graph,
            authoritative_candidates=(candidate,),
            authoritative_objective=request.objective,
        )
        self.assertEqual(restored, request)

        decisive = multi_root_payload(request, ["record:decision"])
        decisive["decisive_evidence_refs"] = [
            "record:audit_only_external"
        ]
        with self.assertRaisesRegex(ValueError, "grounded"):
            validate_global_candidate_payload(decisive, request=request)

        expansion = multi_root_payload(request, [])
        expansion["outcome"] = "needs_expansion"
        expansion["selected_candidate_refs"] = []
        expansion["decisive_evidence_refs"] = []
        expansion["expansion_requests"] = [
            {
                "anchor_ref": "record:audit_only_external",
                "context_kind": "action_group",
                "reason": "Inspect the shared-call member.",
                "expected_judgment_change": (
                    "The candidate may become eligible for comparative assessment."
                ),
            }
        ]
        expansion["missing_evidence"] = ["Shared-call context is missing."]
        with self.assertRaisesRegex(ValueError, "grounded anchor"):
            validate_global_candidate_payload(expansion, request=request)

    def test_judge_request_rejects_stale_prompt_bearing_capsule_collection(self):
        envelope = sample_request().validation_envelope()
        envelope["candidate_evidence_capsules"][0]["candidate"][
            "retrieval_edge"
        ]["relation"] = "forged_retrieval_relation"

        with self.assertRaisesRegex(ValueError, "validation source"):
            global_candidate_request_from_validation_envelope(envelope)

    def test_direct_global_validation_rejects_stale_intermediate_path(self):
        trace = {
            "case_id": "stale-intermediate-global-validation",
            "manifest": {
                "case_id": "stale-intermediate-global-validation",
                "run_id": "stale-intermediate-global-run",
                "subject_revision": "git:active",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": "stale-intermediate-global-validation",
                    "run_id": "stale-intermediate-global-run",
                },
            },
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {
                        "rationale": "Use the incomplete implementation.",
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    },
                },
                {
                    "record_id": "fact",
                    "component": "processor",
                    "event_type": "change",
                    "data": {
                        "summary": "The implementation remains incomplete.",
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    },
                },
                {
                    "record_id": "defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "data": {
                        "actual": "The implementation is incomplete.",
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    },
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "fact"},
                    "relation": "decision_guided_change",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "fact"},
                    "to": {"type": "record", "id": "defect"},
                    "relation": "change_observed_by_evaluation",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        objective = "Find the active root."
        defect = seed_defect_state(graph.nodes["record:defect"], objective)
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=graph.edge_context("record:decision", "record:fact")[0],
            score=0.9,
            evidence_refs=("record:decision",),
        )
        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect,
            downstream_paths={
                "record:decision": (
                    "record:decision",
                    "record:fact",
                    "record:defect",
                )
            },
            start_refs=("record:defect",),
        )[0]
        request = GlobalCandidateJudgeRequest(
            case_id=trace["case_id"],
            objective=objective,
            analysis_perspective="task quality",
            seed_ref="record:defect",
            active_defect=defect,
            active_focus_text=defect.actual,
            active_focus_text_hash=active_focus_text_sha256(defect.actual),
            start_refs=("record:defect",),
            capsules=(capsule,),
        )
        validate_global_candidate_request_against_graph(
            graph,
            request,
            authoritative_candidates=(candidate,),
            authoritative_objective=objective,
        )

        stale_trace = copy.deepcopy(trace)
        stale_fact = next(
            item
            for item in stale_trace["records"]
            if item["record_id"] == "fact"
        )
        stale_fact["data"]["subject_revision"] = "git:stale"
        stale_graph = TraceGraph.from_trace(stale_trace)
        with self.assertRaisesRegex(ValueError, "active revision"):
            validate_global_candidate_request_against_graph(
                stale_graph,
                request,
                authoritative_candidates=(candidate,),
                authoritative_objective=objective,
            )

    def test_substituted_action_group_member_cannot_become_expansion_anchor(self):
        envelope = sample_request().validation_envelope()
        envelope["candidate_evidence_capsules"][0]["action_group"]["members"][
            0
        ]["ref"] = "record:forged-expansion-anchor"

        with self.assertRaisesRegex(ValueError, "validation source"):
            restored = global_candidate_request_from_validation_envelope(envelope)
            value = payload(outcome="inconclusive", request=restored)
            value["outcome"] = "needs_expansion"
            value["expansion_requests"] = [
                {
                    "anchor_ref": "record:forged-expansion-anchor",
                    "context_kind": "action_group",
                    "reason": "Inspect the substituted action-group member.",
                    "expected_judgment_change": (
                        "The substituted member may change the candidate role."
                    ),
                }
            ]
            value["missing_evidence"] = ["The action-group member needs context."]
            validate_global_candidate_payload(value, request=restored)

    def test_validation_envelope_v11_round_trip_rejects_stale_identities(self):
        request = sample_request()
        envelope = request.validation_envelope()

        self.assertEqual(
            envelope["schema_version"],
            "global-candidate-validation-envelope/v11",
        )
        self.assertEqual(
            global_candidate_request_from_validation_envelope(envelope), request
        )

        for stale_identity in (
            "global-candidate-validation-envelope/v6",
            "global-candidate-validation-envelope/v5",
            "global-candidate-validation-envelope/v4",
            "global-candidate-validation-envelope/v3",
        ):
            with self.subTest(stale_identity=stale_identity):
                envelope["schema_version"] = stale_identity
                with self.assertRaisesRegex(ValueError, "schema mismatch"):
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
                    episode_facts={
                        **request.capsules[0].episode_facts,
                        "grounded_hops": 2,
                    },
                    downstream_path_references=(
                        request.capsules[0].downstream_path_references[0],
                        request.capsules[1].downstream_path_references[0],
                        request.capsules[0].downstream_path_references[-1],
                    ),
                    validation_source={
                        **request.capsules[0].validation_source,
                        "downstream_path": (
                            "record:decision",
                            "record:verification",
                            "record:defect",
                        ),
                    },
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
            episode_facts={
                **request.capsules[0].episode_facts,
                "grounded_hops": 2,
            },
            validation_source={
                **request.capsules[0].validation_source,
                "downstream_path": (
                    "record:decision",
                    "record:action",
                    "record:defect",
                ),
            },
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
                "responsibility": "shared",
                "candidate_phase": "planning",
                "failure_mode": "omission_enabling_condition",
                "contribution_mechanism": {
                    "type": "scope_narrowing",
                    "target_ref": "record:defect",
                    "effect": "The decision narrows the repair scope.",
                    "evidence_refs": ["record:verification"],
                },
            }
        )

        with self.assertRaisesRegex(ValueError, "causal candidate.*causal_path_refs"):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_root_whose_input_already_has_active_defect(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["input_defect_status"] = "present"

        with self.assertRaisesRegex(ValueError, "input defect absent or unknown"):
            validate_global_candidate_payload(value, request=request)

    def test_accepts_root_with_unknown_input_and_present_output(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["input_defect_status"] = "unknown"

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.selected_candidate_refs, ("record:decision",))
        self.assertEqual(judgment.assessments[0].input_defect_status, "unknown")

    def test_accepts_contributing_condition_with_uncertain_input(self):
        request = multi_root_request("record:first", "record:second")
        value = multi_root_payload(request, ["record:first"])
        second = next(
            item
            for item in value["assessments"]
            if item["candidate_ref"] == "record:second"
        )
        second["input_defect_status"] = "unknown"
        second["causal_role"] = "contributing_condition"
        second["responsibility"] = "shared"
        second["candidate_phase"] = "planning"
        second["failure_mode"] = "omission_enabling_condition"
        second["contribution_mechanism"] = {
            "type": "scope_narrowing",
            "target_ref": "record:second",
            "effect": "The candidate narrows the downstream repair scope.",
            "evidence_refs": ["record:second"],
        }
        second["counterfactual"] = counterfactual(
            "record:second", prevents_defect=False
        )
        second["confidence"] = 0.8

        judgment = validate_global_candidate_payload(
            value, request=request
        )

        assessed = next(
            item
            for item in judgment.assessments
            if item.candidate_ref == "record:second"
        )
        self.assertEqual(assessed.input_defect_status, "unknown")
        self.assertEqual(
            assessed.causal_role, "contributing_condition"
        )

    def test_unknown_root_input_cannot_claim_certain_assessment(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["assessments"][0]["input_defect_status"] = "unknown"
        value["assessments"][0]["confidence"] = 1.0

        with self.assertRaisesRegex(ValueError, "unknown input defect"):
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

    def test_global_schema_is_v11_and_capsule_schema_is_v8(self):
        self.assertEqual(
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            "global-candidate-judgment/v11",
        )
        self.assertEqual(CAPSULE_SCHEMA_VERSION, "candidate-evidence-capsule/v8")

    def test_v4_global_judgment_cache_hits_only_after_validated_write(self):
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
                "responsibility": "none",
                "failure_mode": "none",
                "contribution_mechanism": None,
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
                "responsibility": "primary",
                "candidate_phase": "implementation",
                "failure_mode": "positive_introduction",
                "contribution_mechanism": None,
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
        value["assessments"][0]["causal_role"] = "exculpatory_evidence"
        value["assessments"][1].update(
            {
                "defect_status": "present",
                "output_defect_status": "present",
                "causal_role": "outcome_evidence",
            }
        )
        value["decisive_evidence_refs"] = ["record:decision"]

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")

        value["assessments"][1]["causal_role"] = "contributing_condition"
        value["assessments"][1]["responsibility"] = "shared"
        value["assessments"][1]["candidate_phase"] = "planning"
        value["assessments"][1]["failure_mode"] = "omission_enabling_condition"
        value["assessments"][1]["contribution_mechanism"] = {
            "type": "scope_narrowing",
            "target_ref": "record:defect",
            "effect": "The observation narrows the repair scope.",
            "evidence_refs": ["record:verification"],
        }
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
                "expected_judgment_change": (
                    "The decision may change from root_candidate to propagation_only."
                ),
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

    def test_prompt_contains_exact_per_candidate_comparison_contract(self):
        request = sample_request()

        contract = global_judge_module.global_candidate_comparison_contract(
            request
        )
        parsed = json.loads(build_global_candidate_prompt(request))

        self.assertEqual(
            contract["schema"],
            "global-candidate-comparison-contract/v2",
        )
        self.assertEqual(
            parsed["candidate_comparison_contract"], contract
        )
        self.assertEqual(
            [
                item["candidate_ref"]
                for item in contract["assessment_requirements"]
            ],
            list(request.offered_candidate_refs),
        )
        for capsule, requirement in zip(
            request.capsules, contract["assessment_requirements"]
        ):
            self.assertEqual(
                requirement["required_causal_path_refs"],
                list(capsule.downstream_path),
            )
            self.assertEqual(
                requirement["required_compared_candidate_refs"],
                list(request.open_authored_root_candidate_refs),
            )
            self.assertEqual(
                requirement["defect_status_relation"],
                "defect_status_equals_output_defect_status",
            )
            self.assertEqual(
                requirement["exact_output_fields"][
                    "compared_candidate_refs"
                ],
                list(request.open_authored_root_candidate_refs),
            )
            self.assertEqual(
                requirement["exact_output_fields"]["causal_path_refs"],
                list(capsule.downstream_path),
            )
            if not requirement["root_candidate_eligible"]:
                self.assertNotIn(
                    "root_candidate",
                    requirement["allowed_causal_roles"],
                )

    def test_repair_constraints_reuse_exact_candidate_comparison_contract(self):
        request = sample_request()
        contract = global_judge_module.global_candidate_comparison_contract(
            request
        )

        constraints = causal_judge_module._repair_constraints(
            stage="global_candidate_judgment",
            node_ref=request.seed_ref,
            request_context=request.to_dict(),
        )

        self.assertEqual(
            constraints["candidate_comparison_contract"], contract
        )
        self.assertEqual(
            constraints["open_authored_root_candidate_refs"],
            list(request.open_authored_root_candidate_refs),
        )
        self.assertEqual(
            constraints["exact_compared_candidate_refs"],
            list(request.open_authored_root_candidate_refs),
        )
        self.assertEqual(
            constraints["exact_causal_path_refs_by_candidate"],
            {
                item["candidate_ref"]: item[
                    "required_causal_path_refs"
                ]
                for item in contract["assessment_requirements"]
            },
        )
        self.assertEqual(
            constraints["allowed_decisive_evidence_refs"],
            list(request.grounded_refs),
        )
        self.assertEqual(
            constraints["allowed_assessment_evidence_refs"],
            list(request.grounded_refs),
        )
        self.assertEqual(
            constraints["required_top_level_fields"],
            [
                "outcome",
                "reason",
                "assessments",
                "selected_candidate_refs",
                "expansion_requests",
                "decisive_evidence_refs",
                "missing_evidence",
                "confidence",
                "active_focus_binding",
            ],
        )
        self.assertIn(
            "compared_candidate_refs",
            constraints["required_assessment_fields"],
        )
        self.assertEqual(
            constraints["allowed_assessment_enums"],
            {
                "causal_role": sorted(global_judge_module.CAUSAL_ROLES),
                "responsibility": sorted(
                    global_judge_module.RESPONSIBILITIES
                ),
                "candidate_phase": sorted(
                    global_judge_module.CANDIDATE_PHASES
                ),
                "obligation_status_before": sorted(
                    global_judge_module.OBLIGATION_STATUSES
                ),
                "obligation_status_after": sorted(
                    global_judge_module.OBLIGATION_STATUSES
                ),
                "repair_window_effect": sorted(
                    global_judge_module.REPAIR_WINDOW_EFFECTS
                ),
                "failure_mode": sorted(
                    global_judge_module.FAILURE_MODES
                ),
            },
        )
        self.assertIn(
            {
                "when": {
                    "causal_role": [
                        "contributing_condition",
                        "amplifying_factor",
                    ]
                },
                "require": {
                    "failure_mode": [
                        "omission_enabling_condition",
                        "none",
                    ],
                    "contribution_mechanism": "non-null exact object",
                },
            },
            constraints["assessment_compatibility_rules"],
        )

    def test_repair_constraints_translate_unknown_input_confidence_error(self):
        request = sample_request()

        constraints = causal_judge_module._repair_constraints(
            stage="global_candidate_judgment",
            node_ref=request.seed_ref,
            request_context=request.to_dict(),
            validation_error=(
                "ValueError: candidate with unknown input defect cannot claim "
                "certain confidence"
            ),
        )

        self.assertEqual(
            constraints["required_field_corrections"],
            [
                {
                    "target_candidate_ref": "",
                    "when": {
                        "input_defect_status": "unknown",
                    },
                    "require": {
                        "confidence": 0.99,
                    },
                    "preserve": [
                        "candidate_ref",
                        "defect_status",
                        "output_defect_status",
                        "causal_role",
                        "responsibility",
                        "candidate_phase",
                        "obligation_status_before",
                        "obligation_status_after",
                        "repair_window_effect",
                        "failure_mode",
                        "obligation_refs",
                        "contribution_mechanism",
                        "causal_path_refs",
                        "compared_candidate_refs",
                        "counterfactual",
                        "reason",
                        "evidence_refs",
                    ],
                }
            ],
        )

    def test_repair_constraints_include_page_exclusion_and_unknown_input_resolution(self):
        request = sample_request()

        constraints = causal_judge_module._repair_constraints(
            stage="global_candidate_judgment",
            node_ref=request.seed_ref,
            request_context=request.to_dict(),
            validation_error=(
                "ValueError: no_root_candidates requires known input status "
                "for every open authored root-eligible candidate"
            ),
        )

        self.assertIn(
            "no_root_candidates",
            constraints["valid_outcomes"],
        )
        self.assertEqual(
            constraints["required_outcome_resolution"][
                "unknown_input_status"
            ]["outcome"],
            "inconclusive",
        )
        self.assertTrue(
            constraints["required_outcome_resolution"][
                "unknown_input_status"
            ]["missing_evidence_must_name_candidate_and_prior_state"]
        )

    def test_global_judge_sends_field_correction_in_focused_repair(self):
        request = sample_request()
        invalid = payload(outcome="candidate_roots", request=request)
        invalid["assessments"][0]["input_defect_status"] = "unknown"
        invalid["assessments"][0]["confidence"] = 1.0
        repaired = payload(outcome="candidate_roots", request=request)
        repaired["assessments"][0]["input_defect_status"] = "unknown"
        repaired["assessments"][0]["confidence"] = 0.99

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = []
                self.responses = [invalid, repaired]

            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                self.calls.append(
                    {"system": system, "messages": messages, "max_tokens": max_tokens}
                )
                return TransportCallResult(
                    json.dumps(self.responses.pop(0)),
                    physical_requests=1,
                )

        transport = Transport()
        result = ClaudeCausalJudge(
            transport=transport, cache=JudgmentCache()
        ).judge_candidates_bounded(request, max_physical_requests=2)

        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        self.assertEqual(result.value.outcome, "candidate_roots")
        self.assertEqual(result.physical_requests, 2)
        self.assertNotIn("original_prompt", repair_payload)
        self.assertEqual(
            repair_payload["canonical_request_context"],
            request.to_dict(),
        )
        self.assertEqual(
            repair_payload["repair_constraints"]["required_field_corrections"][0][
                "require"
            ]["confidence"],
            0.99,
        )
        self.assertEqual(
            repair_payload["repair_constraints"]["required_field_corrections"][0][
                "target_candidate_ref"
            ],
            "record:decision",
        )

    def test_global_judge_converges_on_a_valid_fourth_semantic_attempt(self):
        request = sample_request()
        unsupported_responsibility = payload(
            outcome="candidate_roots", request=request
        )
        unsupported_responsibility["assessments"][0][
            "responsibility"
        ] = "secondary"
        invalid_responsible_omission = payload(
            outcome="candidate_roots", request=request
        )
        invalid_responsible_omission["assessments"][0].update(
            {
                "failure_mode": "responsible_omission",
                "responsibility": "primary",
                "candidate_phase": "closure",
                "obligation_status_before": "pending",
                "obligation_status_after": "violated",
                "repair_window_effect": "closed",
            }
        )
        incompatible_non_repair = payload(
            outcome="candidate_roots", request=request
        )
        incompatible_non_repair["assessments"][1].update(
            {
                "causal_role": "contributing_condition",
                "responsibility": "shared",
                "candidate_phase": "planning",
                "failure_mode": "ordinary_non_repair",
                "contribution_mechanism": {
                    "type": "scope_narrowing",
                    "target_ref": "record:defect",
                    "effect": "The candidate narrowed the repair scope.",
                    "evidence_refs": ["record:verification"],
                },
            }
        )
        valid = payload(outcome="candidate_roots", request=request)

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.calls = []
                self.responses = [
                    unsupported_responsibility,
                    invalid_responsible_omission,
                    incompatible_non_repair,
                    valid,
                ]

            def create_message_text_with_usage(
                self, *, system, messages, max_tokens
            ):
                self.calls.append(
                    {
                        "system": system,
                        "messages": messages,
                        "max_tokens": max_tokens,
                    }
                )
                return TransportCallResult(
                    json.dumps(self.responses.pop(0)),
                    physical_requests=1,
                )

        transport = Transport()
        result = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        ).judge_candidates_bounded(
            request,
            max_physical_requests=6,
        )

        self.assertEqual(result.value.outcome, "candidate_roots")
        self.assertEqual(result.physical_requests, 4)
        self.assertEqual(len(transport.calls), 4)
        fourth_payload = json.loads(
            transport.calls[3]["messages"][0]["content"]
        )
        self.assertEqual(len(fourth_payload["validation_history"]), 3)

    def test_global_judge_stops_after_three_equivalent_validation_errors(
        self,
    ):
        request = sample_request()
        invalid = payload(outcome="candidate_roots", request=request)
        invalid["assessments"][0]["responsibility"] = "secondary"

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.request_count = 0

            def create_message_text_with_usage(
                self, *, system, messages, max_tokens
            ):
                self.request_count += 1
                return TransportCallResult(
                    json.dumps(invalid),
                    physical_requests=1,
                )

        transport = Transport()
        with self.assertRaisesRegex(
            BoundedJudgeCallError,
            "validation_stalled_after_3_equivalent_errors",
        ) as raised:
            ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(),
            ).judge_candidates_bounded(
                request,
                max_physical_requests=6,
            )

        self.assertEqual(transport.request_count, 3)
        self.assertEqual(raised.exception.physical_requests, 3)

    def test_global_judge_reports_budget_exhaustion_before_fourth_attempt(
        self,
    ):
        request = sample_request()
        unsupported_responsibility = payload(
            outcome="candidate_roots", request=request
        )
        unsupported_responsibility["assessments"][0][
            "responsibility"
        ] = "secondary"
        invalid_responsible_omission = payload(
            outcome="candidate_roots", request=request
        )
        invalid_responsible_omission["assessments"][0].update(
            {
                "failure_mode": "responsible_omission",
                "responsibility": "primary",
                "candidate_phase": "closure",
                "obligation_status_before": "pending",
                "obligation_status_after": "violated",
                "repair_window_effect": "closed",
            }
        )
        incompatible_non_repair = payload(
            outcome="candidate_roots", request=request
        )
        incompatible_non_repair["assessments"][1].update(
            {
                "causal_role": "contributing_condition",
                "responsibility": "shared",
                "candidate_phase": "planning",
                "failure_mode": "ordinary_non_repair",
                "contribution_mechanism": {
                    "type": "scope_narrowing",
                    "target_ref": "record:defect",
                    "effect": "The candidate narrowed the repair scope.",
                    "evidence_refs": ["record:verification"],
                },
            }
        )
        invalid_outputs = [
            unsupported_responsibility,
            invalid_responsible_omission,
            incompatible_non_repair,
        ]

        class Transport:
            model = "test-model"
            max_tokens = 4096
            repair_max_tokens = 1024
            thinking_config = None

            def __init__(self):
                self.request_count = 0

            def create_message_text_with_usage(
                self, *, system, messages, max_tokens
            ):
                result = invalid_outputs[self.request_count]
                self.request_count += 1
                return TransportCallResult(
                    json.dumps(result),
                    physical_requests=1,
                )

        transport = Transport()
        with self.assertRaisesRegex(
            BoundedJudgeCallError,
            "judge_request_budget_exhausted before semantic repair attempt 4",
        ) as raised:
            ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(),
            ).judge_candidates_bounded(
                request,
                max_physical_requests=3,
            )

        self.assertEqual(transport.request_count, 3)
        self.assertEqual(raised.exception.physical_requests, 3)

    def test_incomplete_open_candidate_error_names_candidate_and_fields(self):
        request = multi_root_request("record:first", "record:second")
        value = multi_root_payload(request, ["record:first"])
        second = next(
            item
            for item in value["assessments"]
            if item["candidate_ref"] == "record:second"
        )
        second["input_defect_status"] = "unknown"
        second["defect_status"] = "unknown"
        second["output_defect_status"] = "unknown"
        second["causal_role"] = "unknown"
        second["responsibility"] = "unknown"
        second["failure_mode"] = "unknown"
        second["contribution_mechanism"] = None
        second["causal_path_refs"] = []
        second["counterfactual"] = counterfactual(
            "record:second", prevents_defect=False
        )

        with self.assertRaisesRegex(
            ValueError,
            "candidate_roots requires a complete comparison",
        ) as raised:
            validate_global_candidate_payload(value, request=request)

        detail = str(raised.exception)
        self.assertIn('"candidate_ref": "record:second"', detail)
        self.assertIn('"causal_path_refs"', detail)
        self.assertIn('"output_defect_status"', detail)
        self.assertIn('"causal_role"', detail)

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

    def test_claude_judge_invalid_output_is_a_typed_bounded_failure(self):
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
        ), self.assertRaises(BoundedJudgeCallError) as raised:
            ClaudeCausalJudge(
                transport=Transport(), cache=JudgmentCache()
            ).judge_candidates_bounded(request, max_physical_requests=1)

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertIn("request_budget_exhausted", str(raised.exception))
        self.assertNotIn(
            "inconclusive",
            [item.get("outcome") for item in payloads if isinstance(item, dict)],
        )


if __name__ == "__main__":
    unittest.main()
