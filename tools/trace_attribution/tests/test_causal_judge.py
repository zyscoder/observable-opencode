from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import (
    ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
    CausalStepRequest,
    ClaudeCausalJudge,
    RootConfirmationRequest,
    build_causal_step_prompt,
    build_recursive_confirmation_prompt,
    validate_causal_step_payload,
    validate_recursive_confirmation,
)
from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution.claude import ClaudeJudgeClient
from trace_attribution.errors import JudgeProviderError, JudgeProviderUnavailable
from trace_attribution.models import NodeJudgment, TraceNode


def sample_step_request(*, context_marker: str = "initial") -> CausalStepRequest:
    current = TraceNode(
        ref="record:change",
        record_id="change",
        component="processor",
        event_type="code.change",
        data={
            "summary": "The change omits parse_namespace_object.",
            "hydrated_artifacts": [
                {
                    "artifact_id": "artifact:change",
                    "content": "class DefinitionParser: pass",
                    "hash": "change-hash",
                }
            ],
        },
    )
    predecessor = TraceNode(
        ref="record:decision",
        record_id="decision",
        component="agent",
        event_type="decision",
        data={"rationale": "Implement only the explicitly listed methods."},
    )
    candidate = CausalCandidate(
        ref=predecessor.ref,
        node=predecessor,
        source="confirmed_edge",
        evidence_refs=("record:evidence",),
    )
    context = {
        "context_version": "2.0",
        "marker": context_marker,
        "current_ref": current.ref,
        "current_reference": {
            "raw_ref": current.ref,
            "resolved_ref": current.ref,
            "resolution_status": "resolved",
        },
        "candidate_predecessors": [
            {
                "ref": predecessor.ref,
                "reference": {
                    "raw_ref": predecessor.ref,
                    "resolved_ref": predecessor.ref,
                    "resolution_status": "resolved",
                },
                "edge_evidence_references": [
                    {
                        "raw_ref": "record:evidence",
                        "resolved_ref": "record:evidence",
                        "resolution_status": "resolved",
                    }
                ],
                "node": predecessor.compact(),
                "artifact_hydration": {
                    "hydrated_artifacts": [],
                    "hydrated_artifact_hash": "evidence-hash",
                },
            }
        ],
        "unresolved_references": [
            {
                "raw_ref": "record:missing",
                "resolved_ref": "",
                "resolution_status": "unresolved",
            }
        ],
    }
    return CausalStepRequest(
        recursive_context=context,
        current_node=current,
        defect_state=DefectState.create(
            label="missing_parser_contract",
            expected="DefinitionParser preserves the compatibility contract",
            actual="parse_namespace_object is absent",
            mechanism="the implementation omitted a required method",
            scope="parser_contract_recovery",
        ),
        candidates=(candidate,),
    )


def valid_step_payload(
    *,
    predecessor_ref: str = "record:decision",
    relation: str = "same_defect_propagation",
    recurse: bool = True,
) -> dict:
    predecessor = {
        "ref": predecessor_ref,
        "relation": relation,
        "reason": "The predecessor carries the defect into the current change.",
        "confidence": 0.9,
        "recurse": recurse,
        "evidence_refs": [predecessor_ref, "record:evidence"],
        "missing_evidence": [],
        "upstream_defect": None,
    }
    if relation == "defect_transformation":
        predecessor["upstream_defect"] = {
            "label": "incomplete_implementation_plan",
            "mechanism": "call sites were not searched",
            "transformation_reason": "the plan controlled authored methods",
        }
    return {
        "current_node_ref": "record:change",
        "current_defect_status": "present",
        "current_defect_reason": "The change omits the required method.",
        "predecessors": [predecessor],
        "candidate_introduction": False,
        "missing_evidence": [],
        "suggested_investigation": None,
        "confidence": 0.9,
    }


def reference_envelope(
    ref: str, *, status: str = "resolved", provenance: str = "recorded", **metadata
) -> dict:
    return {
        "raw_ref": ref,
        "resolved_ref": ref if status == "resolved" else "",
        "resolution_status": status,
        "provenance_class": provenance,
        **metadata,
    }


def sample_confirmation_request(
    *,
    candidate_reference=None,
    path_references=None,
    supporting_evidence=None,
    opposing_evidence=(),
    competing_hypotheses=(),
    task_obligations=None,
) -> RootConfirmationRequest:
    return RootConfirmationRequest(
        candidate_ref="record:decision",
        defect_state=sample_step_request().defect_state,
        recursive_path=("record:decision", "record:change"),
        candidate_reference=candidate_reference
        if candidate_reference is not None
        else reference_envelope("record:decision"),
        recursive_path_references=path_references
        if path_references is not None
        else (
            reference_envelope("record:decision"),
            reference_envelope("record:change"),
        ),
        supporting_evidence=supporting_evidence
        if supporting_evidence is not None
        else (
            {
                **reference_envelope("record:decision"),
                "fact_kind": "candidate_fact",
                "decisive": True,
                "content": "Implement only the explicitly listed methods.",
            },
            {
                **reference_envelope("record:evidence"),
                "fact_kind": "supporting_evidence",
                "content": "The required parse_namespace_object call site was present.",
            },
        ),
        opposing_evidence=opposing_evidence,
        competing_hypotheses=competing_hypotheses,
        task_obligations=task_obligations
        if task_obligations is not None
        else (
            {"source": "task", "text": "Preserve the existing parser compatibility contract."},
        ),
        analysis_perspective="Find the primary controllable cause.",
    )


def valid_confirmation_payload() -> dict:
    return {
        "candidate_ref": "record:decision",
        "status": "confirmed",
        "excerpt": "Implement only the explicitly listed methods.",
        "reason": "The decision excluded a required compatibility method.",
        "counterfactual": {
            "intervention_ref": "record:decision",
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_defect_status": "absent",
            "causal_effect": "prevents_defect",
        },
        "confidence": 0.88,
        "evidence_refs": ["record:decision", "record:evidence"],
    }


class ScriptedTransport:
    def __init__(self, responses, *, model: str = "test-model"):
        self.responses = list(responses)
        self.model = model
        self.max_tokens = 2048
        self.repair_max_tokens = 512
        self.thinking_config = None
        self.request_count = 0
        self.calls = []

    def create_message_text(self, *, system, messages, max_tokens):
        self.request_count += 1
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class CausalJudgeValidationTest(unittest.TestCase):
    def test_validator_accepts_grounded_defect_transformation(self):
        result = validate_causal_step_payload(
            valid_step_payload(relation="defect_transformation"),
            request=sample_step_request(),
        )

        upstream = result.predecessors[0].upstream_defect
        self.assertEqual(upstream.derived_from_defect_state_id, sample_step_request().defect_state.defect_state_id)
        self.assertEqual(upstream.transformation_reason, "the plan controlled authored methods")

    def test_validator_rejects_fabricated_predecessor_ref(self):
        with self.assertRaisesRegex(ValueError, "candidate predecessor"):
            validate_causal_step_payload(
                valid_step_payload(predecessor_ref="record:not_offered"),
                request=sample_step_request(),
            )

    def test_validator_rejects_unresolved_or_fabricated_evidence_ref(self):
        for evidence_ref in ("record:missing", "record:fabricated"):
            with self.subTest(evidence_ref=evidence_ref), self.assertRaisesRegex(
                ValueError, "grounded evidence"
            ):
                payload = valid_step_payload()
                payload["predecessors"][0]["evidence_refs"] = [evidence_ref]
                validate_causal_step_payload(payload, request=sample_step_request())

    def test_exact_relation_values_and_recursion_constraint(self):
        valid_relations = {
            "same_defect_propagation",
            "defect_transformation",
            "introduction_candidate",
            "contributing_condition",
            "outcome_evidence",
            "unrelated",
            "unknown",
        }
        for relation in valid_relations:
            with self.subTest(relation=relation):
                validate_causal_step_payload(
                    valid_step_payload(
                        relation=relation,
                        recurse=relation in {"same_defect_propagation", "defect_transformation"},
                    ),
                    request=sample_step_request(),
                )
        with self.assertRaisesRegex(ValueError, "causal relation"):
            validate_causal_step_payload(
                valid_step_payload(relation="motivated_by"),
                request=sample_step_request(),
            )
        with self.assertRaisesRegex(ValueError, "recurse=true"):
            validate_causal_step_payload(
                valid_step_payload(relation="contributing_condition", recurse=True),
                request=sample_step_request(),
            )

    def test_propagation_and_transformation_require_present_status_recursion_and_evidence(self):
        for relation in ("same_defect_propagation", "defect_transformation"):
            for mutation, error in (
                (lambda payload: payload["predecessors"][0].update(recurse=False), "recurse=true"),
                (lambda payload: payload["predecessors"][0].update(evidence_refs=[]), "direct evidence"),
                (lambda payload: payload.update(current_defect_status="absent"), "status=present"),
            ):
                with self.subTest(relation=relation, error=error), self.assertRaisesRegex(
                    ValueError, error
                ):
                    payload = valid_step_payload(relation=relation)
                    mutation(payload)
                    validate_causal_step_payload(payload, request=sample_step_request())

    def test_introduction_requires_no_viable_defective_predecessor(self):
        payload = valid_step_payload()
        payload["candidate_introduction"] = True

        with self.assertRaisesRegex(ValueError, "viable defective predecessor"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_validator_requires_an_assessment_for_every_offered_candidate(self):
        payload = valid_step_payload()
        payload["predecessors"] = []

        with self.assertRaisesRegex(ValueError, "every offered candidate"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_request_snapshots_mutable_recursive_context(self):
        source = {"current_ref": "record:change", "candidate_predecessors": []}
        request = CausalStepRequest(
            recursive_context=source,
            current_node=sample_step_request().current_node,
            defect_state=sample_step_request().defect_state,
            candidates=(),
        )
        source["current_ref"] = "record:mutated"

        self.assertEqual(request.to_dict()["recursive_context"]["current_ref"], "record:change")


class RootConfirmationValidationTest(unittest.TestCase):
    def test_confirmation_requires_grounded_refs_and_excerpt(self):
        result = validate_recursive_confirmation(
            valid_confirmation_payload(), request=sample_confirmation_request()
        )
        self.assertEqual(result.status, "confirmed")

        for field, value, error in (
            ("evidence_refs", ["record:fabricated"], "grounded evidence"),
            ("excerpt", "A sentence absent from the offered facts.", "grounded excerpt"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                payload = valid_confirmation_payload()
                payload[field] = value
                validate_recursive_confirmation(payload, request=sample_confirmation_request())

    def test_confirmation_excerpt_cannot_be_reference_envelope_metadata(self):
        payload = valid_confirmation_payload()
        payload["excerpt"] = "recorded"

        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(payload, request=sample_confirmation_request())

    def test_confirmation_excerpt_cannot_come_from_nested_provenance_metadata(self):
        payload = valid_confirmation_payload()
        payload["excerpt"] = "Provenance-only explanation."
        payload["evidence_refs"] = ["record:decision"]
        candidate_fact = {
            **reference_envelope("record:decision"),
            "content": "A different candidate-local semantic statement.",
            "inference_metadata": {
                "description": "Provenance-only explanation.",
            },
        }

        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
            )

    def test_unresolved_candidate_cannot_be_confirmed(self):
        unresolved = sample_confirmation_request(
            candidate_reference=reference_envelope("record:decision", status="unresolved")
        )

        with self.assertRaisesRegex(ValueError, "unresolved candidate"):
            validate_recursive_confirmation(valid_confirmation_payload(), request=unresolved)

    def test_confirmation_may_cite_an_offered_recursive_path_ref(self):
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:change"]

        result = validate_recursive_confirmation(payload, request=sample_confirmation_request())

        self.assertEqual(result.evidence_refs, ("record:change",))

    def test_bare_candidate_and_path_refs_never_ground_confirmation(self):
        for request, payload, error in (
            (
                sample_confirmation_request(candidate_reference={}),
                valid_confirmation_payload(),
                "candidate reference envelope",
            ),
            (
                sample_confirmation_request(path_references=()),
                {**valid_confirmation_payload(), "evidence_refs": ["record:change"]},
                "path reference envelope",
            ),
            (
                sample_confirmation_request(
                    path_references=(
                        reference_envelope("record:decision"),
                        reference_envelope("record:change", status="ambiguous"),
                    )
                ),
                valid_confirmation_payload(),
                "unresolved or ambiguous",
            ),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_recursive_confirmation(payload, request=request)

    def test_confirmed_rejects_unresolved_opposition_or_evidence_without_resolution_status(self):
        unresolved_opposition = {
            **reference_envelope("record:opposition", status="unresolved"),
            "content": "An earlier predecessor may carry the defect.",
        }
        missing_status = {
            "raw_ref": "record:evidence",
            "resolved_ref": "record:evidence",
            "provenance_class": "recorded",
            "content": "Evidence with no resolution status.",
        }
        for request, error in (
            (
                sample_confirmation_request(opposing_evidence=(unresolved_opposition,)),
                "unresolved opposing evidence",
            ),
            (
                sample_confirmation_request(supporting_evidence=(missing_status,)),
                "resolution_status",
            ),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_recursive_confirmation(valid_confirmation_payload(), request=request)

    def test_confirmed_rejects_excerpt_from_missing_or_truncated_candidate_artifact(self):
        for artifact_status in ("missing", "truncated"):
            artifact = {
                **reference_envelope("artifact:decision"),
                "fact_kind": "candidate_artifact",
                "owner_reference": reference_envelope("record:decision"),
                "artifact_status": artifact_status,
                "decisive": True,
                "content": "Implement only the explicitly listed methods.",
            }
            request = sample_confirmation_request(supporting_evidence=(artifact,))
            with self.subTest(status=artifact_status), self.assertRaisesRegex(
                ValueError, artifact_status
            ):
                validate_recursive_confirmation(valid_confirmation_payload(), request=request)

    def test_nested_candidate_artifact_requires_its_own_resolved_envelope(self):
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "decisive": True,
            "hydrated_artifacts": [
                {
                    "artifact_id": "artifact:decision",
                    "content": "Implement only the explicitly listed methods.",
                    "missing": True,
                }
            ],
        }

        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        with self.assertRaisesRegex(ValueError, "artifact reference envelope"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
            )

    def test_task2_hydration_manifest_recursively_rejects_missing_envelope_less_artifact(self):
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "decisive": True,
            "artifact_hydration": {
                "node_ref": "record:decision",
                "referenced_artifact_ids": ["decision-payload"],
                "hydrated_artifacts": [
                    {
                        "artifact_id": "decision-payload",
                        "content": "Implement only the explicitly listed methods.",
                        "missing": True,
                        "truncated": False,
                    }
                ],
                "missing_artifact_ids": [],
                "truncated_artifact_ids": [],
            },
        }
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]

        with self.assertRaisesRegex(ValueError, "missing"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
            )

    def test_resolved_task2_hydration_manifest_can_ground_candidate_excerpt(self):
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "decisive": True,
            "artifact_hydration": {
                "node_ref": "record:decision",
                "referenced_artifact_ids": ["decision-payload"],
                "hydrated_artifacts": [
                    {
                        "artifact_id": "decision-payload",
                        "content": "Implement only the explicitly listed methods.",
                        "missing": False,
                        "truncated": False,
                    }
                ],
                "missing_artifact_ids": [],
                "truncated_artifact_ids": [],
            },
        }
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]

        result = validate_recursive_confirmation(
            payload,
            request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
        )

        self.assertEqual(result.status, "confirmed")

        noncandidate_fact = {
            **reference_envelope("record:evidence"),
            "fact_kind": "supporting_evidence",
            "artifact_hydration": {
                **candidate_fact["artifact_hydration"],
                "node_ref": "record:evidence",
            },
        }
        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=(noncandidate_fact,)),
            )

    def test_arbitrarily_nested_artifact_requires_resolved_candidate_local_envelope(self):
        nested_artifact = {
            **reference_envelope("artifact:decision-payload"),
            "artifact_id": "decision-payload",
            "owner_reference": reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "missing": False,
            "truncated": False,
        }
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "details": {"captures": {"attachments": [nested_artifact]}},
        }
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]

        result = validate_recursive_confirmation(
            payload,
            request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
        )
        self.assertEqual(result.status, "confirmed")

        nested_artifact["missing"] = True
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
            )

        metadata_payload = valid_confirmation_payload()
        metadata_payload["excerpt"] = "decision-payload.json"
        metadata_payload["evidence_refs"] = ["record:decision"]
        nested_artifact["missing"] = False
        nested_artifact["path"] = "decision-payload.json"
        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(
                metadata_payload,
                request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
            )

    def test_task2_manifest_rejects_missing_truncated_and_contradictory_entries(self):
        base_manifest = {
            "node_ref": "record:decision",
            "referenced_artifact_ids": ["decision-payload"],
            "hydrated_artifacts": [
                {
                    "artifact_id": "decision-payload",
                    "content": "Implement only the explicitly listed methods.",
                    "missing": False,
                    "truncated": False,
                }
            ],
            "missing_artifact_ids": [],
            "truncated_artifact_ids": [],
        }
        manifests = []
        missing = dict(base_manifest)
        missing["hydrated_artifacts"] = []
        missing["missing_artifact_ids"] = ["decision-payload"]
        manifests.append((missing, "missing"))
        truncated = dict(base_manifest)
        truncated["hydrated_artifacts"] = [
            {**base_manifest["hydrated_artifacts"][0], "truncated": True}
        ]
        truncated["truncated_artifact_ids"] = ["decision-payload"]
        manifests.append((truncated, "truncated"))
        contradictory = dict(base_manifest)
        contradictory["hydrated_artifacts"] = [
            {**base_manifest["hydrated_artifacts"][0], "resolution_status": "unresolved"}
        ]
        manifests.append((contradictory, "reference envelope|unresolved|contradictory"))

        for manifest, error in manifests:
            fact = {
                **reference_envelope("record:decision"),
                "artifact_hydration": manifest,
            }
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_recursive_confirmation(
                    valid_confirmation_payload(),
                    request=sample_confirmation_request(supporting_evidence=(fact,)),
                )

    def test_open_or_unresolved_competing_hypothesis_blocks_confirmation(self):
        closed = {
            "hypothesis_id": "hyp_closed",
            "status": "rejected",
            "candidate_reference": reference_envelope("record:alternative"),
            "evidence_references": [reference_envelope("record:alternative_evidence")],
        }
        open_hypothesis = {
            **closed,
            "hypothesis_id": "hyp_open",
            "status": "supported",
        }
        missing_status = {key: value for key, value in closed.items() if key != "status"}
        unresolved_evidence = {
            **closed,
            "evidence_references": [
                reference_envelope("record:alternative_evidence", status="unresolved")
            ],
        }
        validate_recursive_confirmation(
            valid_confirmation_payload(),
            request=sample_confirmation_request(competing_hypotheses=(closed,)),
        )
        for hypothesis in (open_hypothesis, missing_status, unresolved_evidence):
            with self.subTest(hypothesis=hypothesis), self.assertRaisesRegex(
                ValueError, "competing hypothesis"
            ):
                validate_recursive_confirmation(
                    valid_confirmation_payload(),
                    request=sample_confirmation_request(competing_hypotheses=(hypothesis,)),
                )

    def test_provenance_class_and_inference_metadata_are_confirmation_eligible(self):
        semantic_inferred = reference_envelope(
            "record:decision",
            provenance="inferred",
            evidence_type="semantic_inferred",
            inference_method="semantic_similarity_v1",
            edge_origin="offline.semantic_search",
        )
        validate_recursive_confirmation(
            valid_confirmation_payload(),
            request=sample_confirmation_request(candidate_reference=semantic_inferred),
        )
        candidate_fact = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "edge_metadata": {
                "provenance_class": "recorded",
                "relation": "semantic_dependency",
            },
        }
        validate_recursive_confirmation(
            valid_confirmation_payload(),
            request=sample_confirmation_request(supporting_evidence=(
                candidate_fact,
                {
                    **reference_envelope("record:evidence"),
                    "content": "The call site was present.",
                },
            )),
        )
        invalid_envelopes = (
            reference_envelope("record:decision", provenance="fabricated"),
            reference_envelope("record:decision", provenance="Recorded"),
            reference_envelope("record:decision", provenance="inferred"),
            reference_envelope(
                "record:decision",
                provenance="inferred",
                evidence_type="temporal_inferred",
                inference_method="temporal_adjacency",
                edge_origin="offline.temporal",
            ),
            {
                **reference_envelope("record:decision"),
                "resolution_status": "unresolved",
            },
        )
        for envelope in invalid_envelopes:
            with self.subTest(envelope=envelope), self.assertRaisesRegex(
                ValueError, "provenance|inference|temporal|contradictory"
            ):
                validate_recursive_confirmation(
                    valid_confirmation_payload(),
                    request=sample_confirmation_request(candidate_reference=envelope),
                )

    def test_recursive_fact_tree_blocks_status_gap_provider_and_budget_states(self):
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        blocking_values = (
            ("semantic status", {"status": "unknown"}, "unknown"),
            ("missing evidence", {"missing_evidence": ["record:missing"]}, "missing_evidence"),
            ("provider error", {"provider_error": "transport failed"}, "provider_error"),
            ("budget exhausted", {"judge_budget_exhausted": True}, "budget_exhausted"),
        )
        for label, nested, error in blocking_values:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                "nested_analysis": nested,
            }
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, error):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_task_obligations_are_part_of_the_recursive_fact_tree(self):
        obligation = {
            "source": "task",
            "text": "Preserve the parser contract.",
            "nested_provider": {"provider_error": "obligation evidence unavailable"},
        }

        with self.assertRaisesRegex(ValueError, "task_obligations|provider_error"):
            validate_recursive_confirmation(
                valid_confirmation_payload(),
                request=sample_confirmation_request(task_obligations=(obligation,)),
            )

    def test_blocking_normalization_covers_exhausted_and_unavailable_states(self):
        blocking_values = (
            {"status": "budget_exhausted"},
            {"budget_status": "exhausted"},
            {"provider_status": "provider_unavailable"},
            {"circuit_status": "circuit_open"},
            {"availability": "unavailable"},
            {"available": False},
            {"artifact_status": {"availability": "missing"}},
            {"artifact_status": {"availability": "unavailable"}},
            {"artifact_status": {"hydration_status": "unavailable"}},
            {"hydration_status": "not_requested"},
        )
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        for blocking in blocking_values:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                "nested_state": blocking,
            }
            with self.subTest(blocking=blocking), self.assertRaisesRegex(
                ValueError, "blocking|unavailable|exhausted|missing"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_contextual_blocking_normalization_covers_reviewer_representations(self):
        blocking_values = (
            {"provider": {"status": "error"}},
            {"provider_response": {"status": "failed"}},
            {"circuit_breaker": {"state": "open"}},
            {"transport": {"status": "timeout"}},
            {"request": {"status": "failed"}},
            {"request_status": "failed"},
            {"missing_evidence_status": "failed"},
            {"request_failure_state": {"reason": "request could not complete"}},
            {"provider": {"error": "backend rejected the request"}},
            {"reference_status": {"resolved": False}},
            {"artifact_state": {"hydrated": False}},
            {"evidence_status": {"available": False}},
            {"reference_status": {"complete": False}},
            {"evidence_status": {"grounded": False}},
            {"diagnostics": {"missing": True}},
            {"diagnostics": {"truncated": True}},
            {"diagnostics": {"unresolved": True}},
            {"diagnostics": {"error": True}},
            {"diagnostics": {"exhausted": True}},
            {"provider_circuit_open": True},
            {"metrics": {"missing_artifact_count": 1}},
            {"metrics": {"unresolved_reference_count": 2}},
            {"metrics": {"truncated_artifact_count": 1}},
            {"metrics": {"provider_error_count": 1}},
            {"metrics": {"evidence_gap_count": 3}},
            {"metrics": {"failed_request_count": 1}},
            {"metrics": {"exhausted_budget_count": 1}},
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for blocking in blocking_values:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                "nested_analysis": blocking,
            }
            with self.subTest(blocking=blocking), self.assertRaisesRegex(
                ValueError, "blocking"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_blocking_aliases_are_normalized_by_semantic_family(self):
        blocking_aliases = (
            {"evidence_status": {"availability": False}},
            {"reference_status": {"resolution": False}},
            {"artifact_state": {"hydration": False}},
            {"provider_result": "failed"},
            {"provider_status": "failure"},
            {"circuit_breaker": {"state": "tripped"}},
            {"provider_tool_request": {"status": "failed"}},
            {"provider_failure": "timeout"},
            {"circuit_open": "rate limited"},
            {"transport_timeout": {"detail": "deadline exceeded"}},
            {"judge_error": ["invalid response"]},
            {"circuit_tripped": {"detail": "quota exceeded"}},
            {"provider_unavailable": 1},
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for blocking in blocking_aliases:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                "nested_analysis": blocking,
            }
            with self.subTest(blocking=blocking), self.assertRaisesRegex(
                ValueError, "blocking"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_positive_status_aliases_and_grounded_tool_failures_remain_eligible(self):
        eligible_states = (
            {"evidence_status": {"availability": True}},
            {"reference_status": {"resolution": True}},
            {"artifact_state": {"hydration": True}},
            {"provider_result": "succeeded"},
            {"provider_status": "available"},
            {"circuit_breaker": {"state": "closed"}},
            {"provider_failure": ""},
            {"circuit_open": False},
            {"transport_error": []},
            {
                "tool_execution": {
                    "status": "failed",
                    "reason": "The tool returned invalid output.",
                }
            },
            {
                "tool_result": {
                    "status": "failed",
                    "reason": "The domain tool reported a causal failure.",
                }
            },
            {
                "tool_request": {
                    "status": "failed",
                    "reason": "The domain tool request itself failed.",
                }
            },
            {"tool_failure": "timeout"},
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for eligible in eligible_states:
            with self.subTest(eligible=eligible):
                candidate_fact = {
                    **reference_envelope("record:decision"),
                    "content": "Implement only the explicitly listed methods.",
                    "nested_analysis": eligible,
                }

                result = validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(
                        supporting_evidence=(candidate_fact,)
                    ),
                )

                self.assertEqual(result.status, "confirmed")

    def test_structured_provenance_context_preserves_temporal_exclusion(self):
        temporal_containers = (
            {
                "edge_provenance": {
                    "type": "temporal_order",
                    "kind": "recorded_edge",
                }
            },
            {
                "evidence_metadata": {
                    "lineage": {
                        "kind": "temporal_adjacency",
                        "method": "recorded_lookup",
                    }
                }
            },
            {"inference": {"type": "temporal_order"}},
            {"origin": {"type": "temporal_order"}},
            {"method": {"kind": "temporal_order"}},
            {"source": {"type": "temporal_proximity"}},
            {"source": [{"type": "temporal_proximity"}]},
            {"origin": {"edges": [{"type": "temporal_order"}]}},
            {"method": [{"details": {"kind": "temporal_adjacency"}}]},
        )
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        for structured_provenance in temporal_containers:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                **structured_provenance,
            }
            with self.subTest(provenance=structured_provenance), self.assertRaisesRegex(
                ValueError, "temporal"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_nested_non_temporal_provenance_forms_remain_eligible(self):
        eligible_provenance = (
            {"source": {"type": "recorded_artifact"}},
            {"source": [{"type": "reconstructed_fact"}]},
            {"origin": {"edges": [{"type": "confirmed_edge"}]}},
            {"method": [{"details": {"kind": "semantic_analysis"}}]},
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for provenance in eligible_provenance:
            with self.subTest(provenance=provenance):
                candidate_fact = {
                    **reference_envelope("record:decision"),
                    "content": "Implement only the explicitly listed methods.",
                    **provenance,
                }

                result = validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(
                        supporting_evidence=(candidate_fact,)
                    ),
                )

                self.assertEqual(result.status, "confirmed")

    def test_excerpt_cannot_be_synthesized_across_candidate_fact_fragments(self):
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        split_facts = (
            {
                **reference_envelope("record:decision"),
                "content": "Implement only the",
            },
            {
                **reference_envelope("record:decision"),
                "content": "explicitly listed methods.",
            },
        )

        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(
                payload,
                request=sample_confirmation_request(supporting_evidence=split_facts),
            )

        whitespace_fact = {
            **reference_envelope("record:decision"),
            "content": "Implement   only\n the explicitly listed methods.",
        }
        result = validate_recursive_confirmation(
            payload,
            request=sample_confirmation_request(supporting_evidence=(whitespace_fact,)),
        )
        self.assertEqual(result.status, "confirmed")

    def test_task2_artifact_status_schema_accepts_only_available_hydrated_identity(self):
        manifest = {
            "node_ref": "record:decision",
            "referenced_artifact_ids": ["decision-payload"],
            "hydrated_artifacts": [
                {
                    "artifact_id": "decision-payload",
                    "content": "Implement only the explicitly listed methods.",
                    "missing": False,
                    "truncated": False,
                }
            ],
            "missing_artifact_ids": [],
            "truncated_artifact_ids": [],
        }

        def request_for(status):
            fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                "artifact_hydration": manifest,
                "artifact_reference": {
                    **reference_envelope("artifact:decision-payload"),
                    "reference_kind": "artifact",
                    "artifact_id": "decision-payload",
                    "owner_reference": reference_envelope("record:decision"),
                    "artifact_status": status,
                },
            }
            return sample_confirmation_request(supporting_evidence=(fact,))

        valid_status = {
            "raw_ref": "artifact:decision-payload",
            "canonical_ref": "artifact:decision-payload",
            "resolution_status": "resolved",
            "availability": "available",
            "hydration_status": "hydrated",
        }
        result = validate_recursive_confirmation(
            {**valid_confirmation_payload(), "evidence_refs": ["artifact:decision-payload"]},
            request=request_for(valid_status),
        )
        self.assertEqual(result.status, "confirmed")

        invalid_statuses = (
            {**valid_status, "raw_ref": "artifact:other"},
            {**valid_status, "canonical_ref": "artifact:other"},
            {**valid_status, "resolved_ref": "artifact:other"},
            {**valid_status, "resolution_status": "unresolved"},
            {**valid_status, "availability": "missing"},
            {**valid_status, "availability": "unavailable"},
            {**valid_status, "hydration_status": "unavailable"},
        )
        for status in invalid_statuses:
            with self.subTest(status=status), self.assertRaisesRegex(
                ValueError, "artifact_status|artifact status|blocking|unresolved"
            ):
                validate_recursive_confirmation(
                    valid_confirmation_payload(), request=request_for(status)
                )

        with self.assertRaisesRegex(ValueError, "artifact_status|artifact status"):
            validate_recursive_confirmation(
                {**valid_confirmation_payload(), "evidence_refs": ["artifact:decision-payload"]},
                request=request_for({}),
            )

        standalone_fact = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "artifact_hydration": manifest,
            "artifact_status": valid_status,
        }
        with self.assertRaisesRegex(ValueError, "artifact_status|artifact status|parent"):
            validate_recursive_confirmation(
                {**valid_confirmation_payload(), "evidence_refs": ["artifact:decision-payload"]},
                request=sample_confirmation_request(supporting_evidence=(standalone_fact,)),
            )

        two_artifact_manifest = {
            **manifest,
            "referenced_artifact_ids": ["decision-payload", "other-payload"],
            "hydrated_artifacts": [
                *manifest["hydrated_artifacts"],
                {
                    "artifact_id": "other-payload",
                    "content": "Unrelated artifact content.",
                    "missing": False,
                    "truncated": False,
                },
            ],
        }
        mismatched_parent_fact = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "artifact_hydration": two_artifact_manifest,
            "artifact_reference": {
                **reference_envelope("artifact:other-payload"),
                "reference_kind": "artifact",
                "artifact_id": "other-payload",
                "owner_reference": reference_envelope("record:decision"),
                "artifact_status": valid_status,
            },
        }
        with self.assertRaisesRegex(ValueError, "artifact_status|artifact status|parent"):
            validate_recursive_confirmation(
                {**valid_confirmation_payload(), "evidence_refs": ["artifact:decision-payload"]},
                request=sample_confirmation_request(
                    supporting_evidence=(mismatched_parent_fact,)
                ),
            )

    def test_recursive_blockers_apply_to_candidate_path_opposition_and_competition(self):
        gap = {"nested_analysis": {"missing_evidence": ["record:missing"]}}
        requests = (
            sample_confirmation_request(
                candidate_reference={**reference_envelope("record:decision"), **gap}
            ),
            sample_confirmation_request(
                path_references=(
                    reference_envelope("record:decision"),
                    {**reference_envelope("record:change"), **gap},
                )
            ),
            sample_confirmation_request(
                opposing_evidence=(
                    {**reference_envelope("record:opposition"), **gap},
                )
            ),
            sample_confirmation_request(
                competing_hypotheses=(
                    {
                        "hypothesis_id": "hyp_blocked",
                        "status": "rejected",
                        "candidate_reference": reference_envelope("record:alternative"),
                        **gap,
                    },
                )
            ),
        )
        for request in requests:
            with self.subTest(request=request.to_dict()), self.assertRaisesRegex(
                ValueError, "missing_evidence"
            ):
                validate_recursive_confirmation(
                    valid_confirmation_payload(), request=request
                )

    def test_recursive_fact_tree_rejects_nested_fabricated_provenance_and_unresolved_envelope(self):
        nested_values = (
            (
                {"edge": {"provenance_class": "fabricated"}},
                "provenance_class",
            ),
            (
                {
                    "edge": {
                        **reference_envelope("record:hidden", status="unresolved"),
                        "content": "A hidden unresolved alternative.",
                    }
                },
                "unresolved",
            ),
        )
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        for nested, error in nested_values:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                **nested,
            }
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_temporal_order_and_nested_temporal_edge_are_confirmation_ineligible(self):
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = ["record:decision"]
        temporal_values = (
            {"evidence_type": "temporal_order"},
            {
                "edge": {
                    **reference_envelope("record:edge"),
                    "relation": "temporal_predecessor",
                }
            },
        )
        for temporal in temporal_values:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
                **temporal,
            }
            with self.subTest(temporal=temporal), self.assertRaisesRegex(
                ValueError, "temporal"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(supporting_evidence=(candidate_fact,)),
                )

    def test_rejected_envelope_parent_cannot_hide_nested_unresolved_reference(self):
        hypothesis = {
            **reference_envelope("record:alternative"),
            "status": "rejected",
            "nested_evidence": {
                "reference": reference_envelope(
                    "record:hidden-alternative", status="unresolved"
                )
            },
        }

        with self.assertRaisesRegex(ValueError, "unresolved"):
            validate_recursive_confirmation(
                valid_confirmation_payload(),
                request=sample_confirmation_request(competing_hypotheses=(hypothesis,)),
            )

    def test_rejected_hypothesis_cannot_hide_exact_bare_ref_or_refs(self):
        for field, value in (
            ("ref", "record:bare-alternative"),
            ("refs", ["record:bare-evidence"]),
        ):
            hypothesis = {
                "hypothesis_id": "hyp_bare",
                "status": "rejected",
                "candidate_reference": reference_envelope("record:alternative"),
                field: value,
            }
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "bare|unregistered|unresolved"
            ):
                validate_recursive_confirmation(
                    valid_confirmation_payload(),
                    request=sample_confirmation_request(competing_hypotheses=(hypothesis,)),
                )

    def test_v3_counterfactual_all_status_combinations(self):
        allowed = {
            "confirmed": {("absent", "prevents_defect")},
            "unknown": {("unknown", "unknown")},
            "rejected": {
                ("present", "does_not_prevent_defect"),
                ("unknown", "unknown"),
            },
        }
        for status in ("confirmed", "rejected", "unknown"):
            for predicted in ("absent", "present", "unknown"):
                for effect in ("prevents_defect", "does_not_prevent_defect", "unknown"):
                    payload = valid_confirmation_payload()
                    payload["status"] = status
                    payload["counterfactual"] = {
                        "intervention_ref": "record:decision",
                        "intervention_kind": "replace_with_semantically_correct_behavior",
                        "predicted_defect_status": predicted,
                        "causal_effect": effect,
                    }
                    if (predicted, effect) in allowed[status]:
                        result = validate_recursive_confirmation(
                            payload, request=sample_confirmation_request()
                        )
                        self.assertEqual(result.status, status)
                        self.assertEqual(
                            result.counterfactual,
                            "replace_with_semantically_correct_behavior(record:decision) "
                            "predicts defect_status={0}; causal_effect={1}".format(
                                predicted, effect
                            ),
                        )
                        continue
                    with self.subTest(status=status, predicted=predicted, effect=effect), self.assertRaisesRegex(
                        ValueError, "counterfactual"
                    ):
                        validate_recursive_confirmation(
                            payload, request=sample_confirmation_request()
                        )

    def test_v3_counterfactual_requires_exact_intervention(self):
        for field, value in (
            ("intervention_ref", "record:other"),
            ("intervention_kind", "delete_candidate"),
        ):
            payload = valid_confirmation_payload()
            payload["counterfactual"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                validate_recursive_confirmation(payload, request=sample_confirmation_request())

    def test_model_counterfactual_text_cannot_override_structured_result(self):
        payload = valid_confirmation_payload()
        payload["counterfactual_explanation"] = "The defect would still occur."

        result = validate_recursive_confirmation(payload, request=sample_confirmation_request())

        self.assertNotIn("still occur", result.counterfactual)


class CausalJudgePromptTest(unittest.TestCase):
    def test_step_and_confirmation_prompts_define_complete_causal_contract(self):
        prompts = (
            build_causal_step_prompt(sample_step_request()),
            build_recursive_confirmation_prompt(sample_confirmation_request()),
        )
        required = (
            "Temporal order or proximity alone is never causal.",
            "same_defect_propagation",
            "defect_transformation",
            "introduction_candidate",
            "contributing_condition",
            "outcome_evidence",
            "unrelated",
            "unknown",
            "exactly one assessment for every offered candidate",
            "reference envelope",
            "recurse=true",
            "direct evidence",
            "candidate_introduction",
            "intervention_ref",
            "predicted_defect_status",
            "causal_effect",
        )
        for prompt in prompts:
            for phrase in required:
                with self.subTest(phrase=phrase):
                    self.assertIn(phrase, prompt)
        confirmation_prompt = prompts[1]
        for phrase in (
            "every nested mapping and list",
            "provider_error",
            "budget_exhausted",
            "reference-bearing key",
        ):
            with self.subTest(confirmation_phrase=phrase):
                self.assertIn(phrase, confirmation_prompt)
        self.assertEqual(ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION, "recursive-root-confirmation-v3")


class JudgmentCachePayloadTest(unittest.TestCase):
    def test_generic_payload_round_trip_and_legacy_judgment_hydration(self):
        node = sample_step_request().current_node
        legacy = NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=False,
            defect_status="unknown",
            defect_reason="Legacy evidence was incomplete.",
            causal_role="unknown",
            is_root_cause=False,
            confidence=0.0,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "cache.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "cache_version": "1.0",
                        "key": "legacy",
                        "stage": "node_judgment",
                        "node_ref": node.ref,
                        "model": "old-model",
                        "judgment": legacy.__dict__,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cache = JudgmentCache(path)
            restored = cache.get(key="legacy", node=node)
            cache.put_payload(
                key="causal",
                stage="recursive_causal_step",
                model="new-model",
                node_ref=node.ref,
                payload={"current_node_ref": node.ref, "confidence": 0.5},
            )
            reopened = JudgmentCache(path)

            self.assertEqual(restored, legacy)
            self.assertEqual(
                reopened.get_payload(key="causal"),
                {"current_node_ref": node.ref, "confidence": 0.5},
            )


class ClaudeTransportAdapterTest(unittest.TestCase):
    def test_public_transport_adapter_uses_existing_request_path(self):
        client = object.__new__(ClaudeJudgeClient)
        calls = []

        def fake_create(self, *, system, messages, max_tokens):
            calls.append((system, messages, max_tokens))
            return "transport-result"

        client._create_message_text = types.MethodType(fake_create, client)

        result = client.create_message_text(
            system="system", messages=[{"role": "user", "content": "prompt"}], max_tokens=23
        )

        self.assertEqual(result, "transport-result")
        self.assertEqual(calls, [("system", [{"role": "user", "content": "prompt"}], 23)])


class ClaudeCausalJudgeTest(unittest.TestCase):
    def test_valid_result_is_cached_by_full_context(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            transport = ScriptedTransport([json.dumps(valid_step_payload())])
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            first = judge.judge_step(sample_step_request())
            second = judge.judge_step(sample_step_request())

            self.assertEqual(first, second)
            self.assertEqual(transport.request_count, 1)
            self.assertEqual(cache.stats()["writes"], 1)

    def test_stale_invalid_cache_entry_is_not_counted_as_a_hit(self):
        for stale_payload in ({"current_node_ref": "record:stale"}, "not-an-object"):
            with self.subTest(stale_payload=stale_payload), tempfile.TemporaryDirectory() as tempdir:
                path = Path(tempdir) / "cache.jsonl"
                seed_cache = JudgmentCache(path)
                seed_judge = ClaudeCausalJudge(
                    transport=ScriptedTransport([json.dumps(valid_step_payload())]),
                    cache=seed_cache,
                )
                seed_judge.judge_step(sample_step_request())
                cache = JudgmentCache(path)
                key = next(iter(cache._entries))
                cache._entries[key]["payload"] = stale_payload
                transport = ScriptedTransport([json.dumps(valid_step_payload())])

                result = ClaudeCausalJudge(transport=transport, cache=cache).judge_step(
                    sample_step_request()
                )

                self.assertEqual(result.current_defect_status, "present")
                self.assertEqual(transport.request_count, 1)
                self.assertEqual(cache.stats()["hits"], 0)
                self.assertEqual(cache.stats()["misses"], 1)
                self.assertEqual(cache.stats()["invalid_entries"], 1)

    def test_cache_key_changes_with_context_and_hydrated_evidence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            transport = ScriptedTransport(
                [json.dumps(valid_step_payload()), json.dumps(valid_step_payload())]
            )
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            judge.judge_step(sample_step_request(context_marker="first"))
            judge.judge_step(sample_step_request(context_marker="second"))

            self.assertEqual(transport.request_count, 2)
            self.assertEqual(cache.stats()["writes"], 2)

    def test_one_focused_repair_receives_exact_error_and_caches_only_valid_result(self):
        invalid = valid_step_payload(predecessor_ref="record:fabricated")
        transport = ScriptedTransport([json.dumps(invalid), json.dumps(valid_step_payload())])
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            result = judge.judge_step(sample_step_request())
            cached = judge.judge_step(sample_step_request())

            repair_prompt = transport.calls[1]["messages"][0]["content"]
            self.assertEqual(result, cached)
            self.assertIn("candidate predecessor", repair_prompt)
            self.assertIn("record:fabricated", repair_prompt)
            self.assertEqual(transport.request_count, 2)
            self.assertEqual(cache.stats()["writes"], 1)

    def test_invalid_repair_returns_auditable_unknown_and_is_not_cached(self):
        invalid = json.dumps(valid_step_payload(predecessor_ref="record:fabricated"))
        transport = ScriptedTransport([invalid, invalid, invalid, invalid])
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            first = judge.judge_step(sample_step_request())
            second = judge.judge_step(sample_step_request())

            self.assertEqual(first.current_defect_status, "unknown")
            self.assertFalse(first.candidate_introduction)
            self.assertTrue(any("validation" in item for item in first.missing_evidence))
            self.assertEqual(second.current_defect_status, "unknown")
            self.assertEqual(transport.request_count, 4)
            self.assertEqual(cache.stats()["writes"], 0)

    def test_provider_errors_return_unknown_while_transport_opens_its_circuit(self):
        class FailingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                raise ConnectionError("provider connection dropped")

        messages = FailingMessages()
        transport = object.__new__(ClaudeJudgeClient)
        transport.timeout_seconds = None
        transport.thinking_config = None
        transport.model = "test-model"
        transport.max_tokens = 2048
        transport.repair_max_tokens = 512
        transport.client = types.SimpleNamespace(messages=messages)
        transport.request_count = 0
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 0
        transport.provider_circuit_open = False
        transport.provider_circuit_reason = ""
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        results = [judge.judge_step(sample_step_request()) for _ in range(4)]

        self.assertTrue(all(result.current_defect_status == "unknown" for result in results))
        self.assertTrue(all(result.candidate_introduction is False for result in results))
        self.assertTrue(all(result.missing_evidence for result in results))
        self.assertEqual(messages.calls, 3)
        self.assertEqual(transport.request_count, 3)
        self.assertTrue(transport.provider_circuit_open)

    def test_confirmation_uses_same_repair_and_cache_path(self):
        invalid = valid_confirmation_payload()
        invalid["evidence_refs"] = ["record:fabricated"]
        transport = ScriptedTransport([json.dumps(invalid), json.dumps(valid_confirmation_payload())])
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            first = judge.confirm_candidate(sample_confirmation_request())
            second = judge.confirm_candidate(sample_confirmation_request())

            self.assertEqual(first.status, "confirmed")
            self.assertEqual(first, second)
            self.assertIn("grounded evidence", transport.calls[1]["messages"][0]["content"])
            self.assertEqual(transport.request_count, 2)

    def test_decisive_confirmation_grounding_gaps_return_uncached_unknown(self):
        unresolved_opposition = {
            **reference_envelope("record:opposition", status="unresolved"),
            "content": "An earlier predecessor may carry the defect.",
        }
        missing_artifact = {
            **reference_envelope("artifact:decision"),
            "fact_kind": "candidate_artifact",
            "owner_reference": reference_envelope("record:decision"),
            "artifact_status": "missing",
            "decisive": True,
            "content": "Implement only the explicitly listed methods.",
        }
        nested_provider_error = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "nested_analysis": {"provider_error": "evidence provider failed"},
        }
        provider_error_obligation = {
            "source": "task",
            "text": "Preserve the parser contract.",
            "nested_analysis": {"provider_error": "obligation provider failed"},
        }
        aliased_blocker = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "nested_analysis": {"evidence_status": {"availability": False}},
        }
        temporal_source = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "source": [{"type": "temporal_proximity"}],
        }
        scalar_provider_failure = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "provider_failure": "timeout",
        }
        for request in (
            sample_confirmation_request(opposing_evidence=(unresolved_opposition,)),
            sample_confirmation_request(supporting_evidence=(missing_artifact,)),
            sample_confirmation_request(supporting_evidence=(nested_provider_error,)),
            sample_confirmation_request(task_obligations=(provider_error_obligation,)),
            sample_confirmation_request(supporting_evidence=(aliased_blocker,)),
            sample_confirmation_request(supporting_evidence=(temporal_source,)),
            sample_confirmation_request(supporting_evidence=(scalar_provider_failure,)),
        ):
            with self.subTest(request=request.to_dict()):
                transport = ScriptedTransport(
                    [json.dumps(valid_confirmation_payload()), json.dumps(valid_confirmation_payload())]
                )
                with tempfile.TemporaryDirectory() as tempdir:
                    cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
                    result = ClaudeCausalJudge(transport=transport, cache=cache).confirm_candidate(
                        request
                    )

                    self.assertEqual(result.status, "unknown")
                    self.assertEqual(result.counterfactual_status, "unknown")
                    self.assertEqual(transport.request_count, 0)
                    self.assertEqual(transport.calls, [])
                    self.assertEqual(
                        {
                            key: cache.stats()[key]
                            for key in ("hits", "misses", "writes", "invalid_entries")
                        },
                        {"hits": 0, "misses": 0, "writes": 0, "invalid_entries": 0},
                    )

    def test_provider_and_validation_failures_never_confirm_roots(self):
        for response in (
            JudgeProviderError("provider unavailable"),
            json.dumps({"candidate_ref": "record:decision", "status": "confirmed"}),
        ):
            with self.subTest(response=type(response).__name__):
                responses = [response]
                if not isinstance(response, BaseException):
                    responses.append(response)
                judge = ClaudeCausalJudge(
                    transport=ScriptedTransport(responses), cache=JudgmentCache()
                )

                result = judge.confirm_candidate(sample_confirmation_request())

                self.assertEqual(result.status, "unknown")
                self.assertIn("judge", result.reason.lower())


if __name__ == "__main__":
    unittest.main()
