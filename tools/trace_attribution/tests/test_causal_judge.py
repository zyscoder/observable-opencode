from __future__ import annotations

import copy
import json
import hashlib
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import (
    CAUSAL_STEP_SYSTEM_PROMPT,
    ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
    BoundedJudgeCallResult,
    BoundedJudgeCallError,
    CausalJudge,
    CausalStepRequest,
    ClaudeCausalJudge,
    FactorRoleRequest,
    OfflineCausalJudgeAdapter,
    OfflineJudgeCapability,
    RootConfirmationRequest,
    _factor_role_allowed_refs_from_facts,
    _repair_constraints,
    build_causal_step_prompt,
    build_factor_role_prompt,
    build_recursive_confirmation_prompt,
    bind_root_confirmation,
    validate_causal_step_payload,
    validate_recursive_confirmation,
)
from trace_attribution.causal_state import (
    CausalCandidate,
    DefectState,
    FactorRoleJudgment,
    confirmation_counterfactual_for,
    confirmation_identity_for,
)
from trace_attribution.claude import ClaudeJudgeClient
from trace_attribution import claude as claude_module
from trace_attribution import errors as provider_errors
from trace_attribution.errors import (
    JudgeProviderError,
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
)
from trace_attribution.models import NodeJudgment, TraceNode, stable_json


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


class ProcessDefectPromptTests(unittest.TestCase):
    def test_causal_step_prompt_distinguishes_process_defect_from_baseline_failure(self):
        prompt = build_causal_step_prompt(sample_step_request())

        self.assertIn("candidate-local process defect", prompt)
        self.assertIn("pre-existing downstream functional defect", prompt)
        self.assertIn("responsible non-repair", prompt)

    def process_request(self) -> CausalStepRequest:
        base = sample_step_request()
        context = dict(base.recursive_context)
        context["active_hypothesis_id"] = "hyp:process"
        context["candidate_process_trajectory"] = {
            "schema": "candidate-process-trajectory/v1",
            "candidate_ref": "record:change",
            "candidate_reference": reference_envelope("record:change"),
            "post_candidate_episode_count": 12,
            "post_candidate_no_delivery_episode_count": 12,
            "post_candidate_mutation_count": 0,
            "post_candidate_verification_count": 0,
            "post_candidate_delivery_observed": False,
            "episode_summaries": [
                {
                    "episode_ref": "record:trajectory_episode",
                    "reference": reference_envelope(
                        "record:trajectory_episode",
                        provenance="reconstructed",
                    ),
                }
            ],
        }
        context["candidate_commitment_cues"] = {
            "schema": "candidate-commitment-cues/v1",
            "candidate_ref": "record:change",
            "candidate_reference": reference_envelope("record:change"),
            "cue_count": 1,
            "cues": [
                {
                    "cue_id": "commitment_cue:test",
                    "cue_type": "explicit_forward_action_language",
                    "verbatim_excerpt": (
                        "I will implement the required parser methods now."
                    ),
                    "semantic_status": (
                        "candidate_cue_not_a_commitment_verdict"
                    ),
                }
            ],
        }
        context["task_obligations"] = [
            {
                "source": "analysis_objective",
                "text": "Explain why the required parser repair was not delivered.",
            }
        ]
        return replace(
            base,
            recursive_context=context,
            defect_state=DefectState.create(
                label="candidate_local_process_defect",
                expected="The implementation commitment is fulfilled.",
                actual="No mutation or verification followed the commitment.",
                mechanism=(
                    "Judge responsible non-repair separately from the "
                    "pre-existing downstream functional defect."
                ),
                scope="candidate_local_process_execution",
            ),
        )

    def process_payload(self, *, status="present", commitment="unfulfilled"):
        request = self.process_request()
        return {
            "current_node_ref": "record:change",
            "current_defect_status": status,
            "current_defect_reason": (
                "The explicit implementation commitment remained unfulfilled "
                "through twelve no-delivery episodes."
            ),
            "predecessors": [
                {
                    "ref": "record:decision",
                    "relation": "unrelated",
                    "reason": "The predecessor does not carry this commitment breach.",
                    "confidence": 0.8,
                    "recurse": False,
                    "upstream_defect": None,
                    "evidence_refs": ["record:decision"],
                    "missing_evidence": [],
                }
            ],
            "candidate_introduction": status == "present",
            "process_assessment": {
                "candidate_role": "implementation_commitment",
                "commitment_status": commitment,
                "trajectory_relation": "remained_investigation",
                "failure_mode": (
                    "responsible_omission"
                    if status == "present"
                    else "none"
                ),
                "commitment_cue_disposition": "commitment",
                "commitment_cue_reason": (
                    "The first-person forward statement commits to the repair."
                ),
                "obligation_refs": ["analysis_objective"],
                "reason": "The commitment was not materialized before the trace boundary.",
                "evidence_refs": [
                    "record:change",
                    "record:trajectory_episode",
                ],
            },
            "missing_evidence": [],
            "suggested_investigation": (
                {
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": "hyp:process",
                        "candidate_ref": "record:change",
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Independently confirm the commitment breach.",
                }
                if status == "present"
                else None
            ),
            "confidence": 0.9,
        }

    def test_process_assessment_accepts_grounded_unfulfilled_commitment(self):
        request = self.process_request()
        judgment = validate_causal_step_payload(
            self.process_payload(), request=request
        )

        self.assertTrue(judgment.candidate_introduction)
        self.assertEqual(
            judgment.process_assessment["failure_mode"],
            "responsible_omission",
        )

    def test_process_assessment_rejects_absent_unfulfilled_commitment(self):
        with self.assertRaisesRegex(
            ValueError, "unfulfilled commitment requires a present process defect"
        ):
            validate_causal_step_payload(
                self.process_payload(status="absent"),
                request=self.process_request(),
            )

    def test_unfulfilled_commitment_requires_distinct_trajectory_evidence(self):
        payload = self.process_payload()
        payload["process_assessment"]["evidence_refs"] = ["record:change"]

        with self.assertRaisesRegex(
            ValueError,
            "unfulfilled commitment requires grounded candidate and trajectory evidence",
        ):
            validate_causal_step_payload(
                payload,
                request=self.process_request(),
            )

    def test_responsible_omission_requires_exact_task_obligation_reference(self):
        payload = self.process_payload()
        payload["process_assessment"]["obligation_refs"] = [
            "task_obligations[0]"
        ]

        with self.assertRaisesRegex(
            ValueError,
            "responsible omission requires exact supplied task obligation refs",
        ):
            validate_causal_step_payload(
                payload,
                request=self.process_request(),
            )

    def test_process_candidate_requires_structured_assessment(self):
        payload = self.process_payload()
        payload.pop("process_assessment")

        with self.assertRaisesRegex(
            ValueError, "candidate-local process defect requires process_assessment"
        ):
            validate_causal_step_payload(
                payload, request=self.process_request()
            )

    def test_explicit_commitment_cue_cannot_be_silently_ignored(self):
        payload = self.process_payload(status="absent", commitment="not_applicable")
        payload["process_assessment"]["commitment_cue_disposition"] = (
            "not_applicable"
        )
        payload["process_assessment"]["failure_mode"] = "none"

        with self.assertRaisesRegex(
            ValueError,
            "recorded commitment cue requires an explicit disposition",
        ):
            validate_causal_step_payload(
                payload, request=self.process_request()
            )

    def test_fulfilled_commitment_requires_observed_delivery(self):
        payload = self.process_payload(status="absent", commitment="fulfilled")
        payload["process_assessment"]["trajectory_relation"] = "materialized"
        payload["process_assessment"]["failure_mode"] = "none"

        with self.assertRaisesRegex(
            ValueError,
            "fulfilled commitment requires observed delivery",
        ):
            validate_causal_step_payload(
                payload, request=self.process_request()
            )


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
        hypothesis_id="hyp:decision",
        hypothesis_semantic_hash="semantic:decision",
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
        "competitor_comparisons": [],
        "factor_mechanism": {},
    }


def strict_manifest(content: str, *, owner_ref: str = "record:decision", start: int = 0, end=None):
    encoded = content.encode("utf-8")
    if end is None:
        end = len(encoded)
    artifact_ref = "artifact:decision-payload"
    owner_reference = reference_envelope(owner_ref)
    content_hash = "sha256:" + hashlib.sha256(encoded).hexdigest()
    byte_range = [start, end]
    owner_binding_identity = "artifact_owner:v1:{0}".format(
        hashlib.sha256(
            stable_json(
                {
                    "artifact_id": "decision-payload",
                    "canonical_ref": artifact_ref,
                    "content_hash": content_hash,
                    "byte_range": byte_range,
                    "owner_reference": owner_reference,
                }
            ).encode("utf-8")
        ).hexdigest()
    )
    return {
        "node_ref": owner_ref,
        "referenced_artifact_ids": ["decision-payload"],
        "hydrated_artifacts": [{
            "artifact_id": "decision-payload",
            "raw_ref": artifact_ref,
            "resolved_ref": artifact_ref,
            "canonical_ref": artifact_ref,
            "resolution_status": "resolved",
            "provenance_class": "recorded",
            "content": content,
            "content_hash": content_hash,
            "byte_count": len(encoded),
            "byte_range": byte_range,
            "owner_reference": owner_reference,
            "owner_binding_identity": owner_binding_identity,
            "missing": False,
            "truncated": False,
            "fact_kind": "artifact_hydration",
        }],
        "missing_artifact_ids": [],
        "truncated_artifact_ids": [],
        "ineligible_artifact_evidence": [],
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
        try:
            return self.create_message_text_with_usage(
                system=system, messages=messages, max_tokens=max_tokens
            ).text
        except TransportCallError as exc:
            raise exc.error from exc

    def create_message_text_with_usage(self, *, system, messages, max_tokens):
        self.request_count += 1
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise TransportCallError(response, physical_requests=1)
        return TransportCallResult(str(response), physical_requests=1)


class CausalJudgeValidationTest(unittest.TestCase):
    def test_evidence_only_nodes_cannot_be_recursive_introduction_candidates(self):
        base = sample_step_request()
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
                current = TraceNode(
                    ref="record:evidence_only",
                    record_id="evidence_only",
                    component="evidence",
                    event_type=event_type,
                    data={"summary": "Observed evidence only."},
                )
                request = CausalStepRequest(
                    recursive_context={
                        "active_hypothesis_id": "hyp:active",
                        "current_ref": current.ref,
                        "candidate_predecessors": [],
                    },
                    current_node=current,
                    defect_state=base.defect_state,
                    candidates=(),
                )
                payload = {
                    "current_node_ref": current.ref,
                    "current_defect_status": "present",
                    "current_defect_reason": "The evidence node appears defective.",
                    "predecessors": [],
                    "candidate_introduction": True,
                    "missing_evidence": [],
                    "suggested_investigation": {
                        "action": "request_root_confirmation",
                        "arguments": {
                            "hypothesis_id": "hyp:active",
                            "candidate_ref": current.ref,
                            "defect_fingerprint": base.defect_state.fingerprint,
                        },
                        "reason": "Attempt to confirm evidence as a root.",
                    },
                    "confidence": 0.9,
                }

                with self.assertRaisesRegex(ValueError, "not eligible"):
                    validate_causal_step_payload(payload, request=request)

    def test_zero_confidence_cannot_assert_defect_presence_or_absence(self):
        payload = valid_step_payload(relation="unrelated", recurse=False)
        payload["current_defect_status"] = "absent"
        payload["current_defect_reason"] = "The current node does not contain the tracked defect."
        payload["confidence"] = 0.0

        with self.assertRaisesRegex(ValueError, "non-unknown status requires positive confidence"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_zero_confidence_cannot_assert_a_known_predecessor_relation(self):
        payload = valid_step_payload()
        payload["predecessors"][0]["confidence"] = 0.0

        with self.assertRaisesRegex(
            ValueError,
            r"predecessors\[0\]\.confidence.*record:decision.*same_defect_propagation.*greater than 0",
        ):
            validate_causal_step_payload(payload, request=sample_step_request())

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
            "contributing_condition",
            "outcome_evidence",
            "unrelated",
            "unknown",
        }
        for relation in valid_relations:
            with self.subTest(relation=relation):
                payload = valid_step_payload(
                    relation=relation,
                    recurse=relation in {"same_defect_propagation", "defect_transformation"},
                )
                if relation not in {"same_defect_propagation", "defect_transformation"}:
                    payload["missing_evidence"] = [
                        "No recursive predecessor or introduction verdict is available."
                    ]
                validate_causal_step_payload(
                    payload,
                    request=sample_step_request(),
                )
        with self.assertRaisesRegex(ValueError, "causal relation"):
            validate_causal_step_payload(
                valid_step_payload(relation="motivated_by"),
                request=sample_step_request(),
            )
        with self.assertRaisesRegex(ValueError, "current-node verdict"):
            validate_causal_step_payload(
                valid_step_payload(relation="introduction_candidate", recurse=False),
                request=sample_step_request(),
            )
        with self.assertRaisesRegex(ValueError, "recurse=true"):
            validate_causal_step_payload(
                valid_step_payload(relation="outcome_evidence", recurse=True),
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

    def test_material_contributing_condition_can_recurse_with_an_upstream_defect(self):
        payload = valid_step_payload(
            relation="contributing_condition",
            recurse=True,
        )
        payload["predecessors"][0]["upstream_defect"] = {
            "label": "incorrect_cancellation_assumption",
            "mechanism": "the authored plan assumes task cancellation preserves cleanup",
            "transformation_reason": "the assumption shaped the downstream implementation",
        }

        result = validate_causal_step_payload(payload, request=sample_step_request())

        predecessor = result.predecessors[0]
        self.assertTrue(predecessor.recurse)
        self.assertEqual(
            predecessor.upstream_defect.label,
            "incorrect_cancellation_assumption",
        )

        payload["predecessors"][0]["upstream_defect"] = None
        with self.assertRaisesRegex(ValueError, "upstream_defect"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_input_provenance_envelope_cannot_recurse_from_motivation_alone(self):
        base = sample_step_request()
        prompt = TraceNode(
            ref="record:prompt",
            record_id="prompt",
            component="prompt",
            event_type="prompt.assembly",
            data={"text": "Every started task must complete cleanup."},
        )
        candidate = CausalCandidate(
            ref=prompt.ref,
            node=prompt,
            source="attribution_edge",
            evidence_refs=(prompt.ref,),
        )
        request = CausalStepRequest(
            recursive_context={
                **base.recursive_context,
                "candidate_predecessors": [
                    {
                        "ref": prompt.ref,
                        "reference": reference_envelope(prompt.ref),
                        "edge_evidence_references": [reference_envelope(prompt.ref)],
                        "node": prompt.compact(),
                    }
                ],
            },
            current_node=base.current_node,
            defect_state=base.defect_state,
            candidates=(candidate,),
        )
        payload = valid_step_payload(predecessor_ref=prompt.ref)
        payload["predecessors"][0]["evidence_refs"] = [prompt.ref]
        payload["predecessors"][0]["reason"] = (
            "The requirement prompted the decision and fed the defective assumption into the model."
        )

        with self.assertRaisesRegex(ValueError, "provenance envelope"):
            validate_causal_step_payload(payload, request=request)

        payload["predecessors"][0]["reason"] = (
            "The prompt itself contains the same defect: it incorrectly requires cancellation "
            "without preserving cleanup."
        )
        result = validate_causal_step_payload(payload, request=request)
        self.assertTrue(result.predecessors[0].recurse)

    def test_introduction_requires_no_viable_defective_predecessor(self):
        payload = valid_step_payload()
        payload["candidate_introduction"] = True

        with self.assertRaisesRegex(ValueError, "viable defective predecessor"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_introduction_requires_exact_root_confirmation_control_arguments(self):
        base = sample_step_request()
        request = CausalStepRequest(
            recursive_context={
                "active_hypothesis_id": "hyp:active",
                "current_ref": base.current_node.ref,
                "candidate_predecessors": [],
            },
            current_node=base.current_node,
            defect_state=base.defect_state,
            candidates=(),
        )
        payload = {
            "current_node_ref": base.current_node.ref,
            "current_defect_status": "present",
            "current_defect_reason": "The current boundary node introduces the defect.",
            "predecessors": [],
            "candidate_introduction": True,
            "missing_evidence": [],
            "suggested_investigation": {
                "action": "request_root_confirmation",
                "arguments": {
                    "active_hypothesis_id": "hyp:active",
                    "candidate_root_ref": base.current_node.ref,
                    "active_defect_fingerprint": base.defect_state.fingerprint,
                },
                "reason": "Confirm the trace-visible root.",
            },
            "confidence": 0.9,
        }

        with self.assertRaisesRegex(ValueError, "exact keys"):
            validate_causal_step_payload(payload, request=request)

        payload["suggested_investigation"]["arguments"] = {
            "hypothesis_id": "hyp:active",
            "candidate_ref": base.current_node.ref,
            "defect_fingerprint": base.defect_state.fingerprint,
        }
        result = validate_causal_step_payload(payload, request=request)

        self.assertTrue(result.candidate_introduction)

    def test_root_confirmation_control_requires_an_introduction_verdict(self):
        base = sample_step_request()
        request = CausalStepRequest(
            recursive_context={
                **base.recursive_context,
                "active_hypothesis_id": "hyp:active",
            },
            current_node=base.current_node,
            defect_state=base.defect_state,
            candidates=base.candidates,
        )
        payload = valid_step_payload()
        payload["suggested_investigation"] = {
            "action": "request_root_confirmation",
            "arguments": {
                "hypothesis_id": "hyp:active",
                "candidate_ref": base.current_node.ref,
                "defect_fingerprint": base.defect_state.fingerprint,
            },
            "reason": "Confirm the current node even though it was not judged as an introduction.",
        }

        with self.assertRaisesRegex(ValueError, "requires candidate_introduction=true"):
            validate_causal_step_payload(payload, request=request)

    def test_introduction_requires_assessing_every_offered_predecessor(self):
        base = sample_step_request()
        request = CausalStepRequest(
            recursive_context={
                **base.recursive_context,
                "active_hypothesis_id": "hyp:active",
            },
            current_node=base.current_node,
            defect_state=base.defect_state,
            candidates=base.candidates,
        )
        payload = valid_step_payload()
        payload["predecessors"] = []
        payload["candidate_introduction"] = True
        payload["suggested_investigation"] = {
            "action": "request_root_confirmation",
            "arguments": {
                "hypothesis_id": "hyp:active",
                "candidate_ref": base.current_node.ref,
                "defect_fingerprint": base.defect_state.fingerprint,
            },
            "reason": "Confirm only after every offered predecessor is ruled out.",
        }

        with self.assertRaisesRegex(ValueError, "every offered predecessor"):
            validate_causal_step_payload(payload, request=request)

    def test_validator_accepts_sparse_ranked_predecessors_and_records_unselected_candidates(self):
        payload = valid_step_payload()
        payload["predecessors"] = []
        payload["current_defect_status"] = "absent"
        payload["current_defect_reason"] = "The current node does not contain the active defect."

        result = validate_causal_step_payload(payload, request=sample_step_request())

        self.assertEqual(result.predecessors, ())
        self.assertEqual(result.unselected_predecessor_refs, ("record:decision",))

    def test_present_defect_cannot_end_without_recursion_introduction_or_missing_evidence(self):
        payload = valid_step_payload(
            relation="contributing_condition",
            recurse=False,
        )

        with self.assertRaisesRegex(ValueError, "cannot terminate silently"):
            validate_causal_step_payload(payload, request=sample_step_request())

        payload["missing_evidence"] = ["The omitted predecessor still needs assessment."]
        result = validate_causal_step_payload(payload, request=sample_step_request())
        self.assertEqual(result.current_defect_status, "present")

    def test_validator_rejects_present_status_that_explicitly_denies_a_current_node_defect(self):
        payload = valid_step_payload()
        payload["current_defect_status"] = "present"
        payload["current_defect_reason"] = (
            "The current node itself is not defective; the defect belongs to an earlier decision."
        )

        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_causal_step_payload(payload, request=sample_step_request())

        payload["current_defect_reason"] = (
            "The decision itself does not carry the defect; it only triggered verification."
        )
        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_validator_rejects_absent_status_when_reason_admits_a_defective_assumption(self):
        payload = valid_step_payload(relation="unrelated", recurse=False)
        payload["current_defect_status"] = "absent"
        payload["current_defect_reason"] = (
            "The reasoning block assumes awaiting cancellation guarantees cleanup, but that "
            "assumption is insufficient and does not account for process-level SIGINT timing."
        )
        payload["missing_evidence"] = []

        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_causal_step_payload(payload, request=sample_step_request())

    def test_navigation_aggregate_cannot_request_root_confirmation(self):
        base = sample_step_request()
        current = TraceNode(
            ref="progress_episode:latest",
            record_id="latest",
            component="progress",
            event_type="progress.episode",
            data={"offline_only": True, "semantic_role": "aggregate"},
        )
        request = CausalStepRequest(
            recursive_context={
                "active_hypothesis_id": "hyp:active",
                "current_ref": current.ref,
                "candidate_predecessors": [],
            },
            current_node=current,
            defect_state=base.defect_state,
            candidates=(),
        )
        payload = {
            "current_node_ref": current.ref,
            "current_defect_status": "present",
            "current_defect_reason": "The aggregate appears to introduce the defect.",
            "predecessors": [],
            "candidate_introduction": True,
            "missing_evidence": [],
            "suggested_investigation": {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": "hyp:active",
                    "candidate_ref": current.ref,
                    "defect_fingerprint": base.defect_state.fingerprint,
                },
                "reason": "Confirm the aggregate.",
            },
            "confidence": 0.9,
        }

        with self.assertRaisesRegex(ValueError, "not eligible"):
            validate_causal_step_payload(payload, request=request)

        payload["candidate_introduction"] = False
        payload["current_defect_status"] = "absent"
        payload["current_defect_reason"] = (
            "The navigation aggregate is not itself defective, so stop traversal."
        )
        payload["suggested_investigation"] = None
        with self.assertRaisesRegex(ValueError, "cannot close"):
            validate_causal_step_payload(payload, request=request)

    def test_navigation_contributing_condition_must_open_a_recursive_hypothesis(self):
        base = sample_step_request()
        current = TraceNode(
            ref="progress_episode:latest",
            record_id="latest",
            component="progress",
            event_type="progress.episode",
            data={"offline_only": True, "semantic_role": "aggregate"},
        )
        request = CausalStepRequest(
            recursive_context={
                **base.recursive_context,
                "current_ref": current.ref,
            },
            current_node=current,
            defect_state=base.defect_state,
            candidates=base.candidates,
        )
        payload = valid_step_payload(
            relation="contributing_condition",
            recurse=False,
        )
        payload["current_node_ref"] = current.ref

        with self.assertRaisesRegex(ValueError, "navigation contributing_condition"):
            validate_causal_step_payload(payload, request=request)

        payload["current_defect_status"] = "absent"
        payload["current_defect_reason"] = (
            "The aggregate itself is not defective, but it routes to a material authored assumption."
        )
        payload["predecessors"][0]["recurse"] = True
        payload["predecessors"][0]["upstream_defect"] = {
            "label": "incorrect_authored_assumption",
            "mechanism": "the decision shaped the downstream defect",
            "transformation_reason": "navigation exposes the upstream semantic cause",
        }
        routed = validate_causal_step_payload(payload, request=request)
        self.assertEqual(routed.current_defect_status, "absent")
        self.assertTrue(routed.predecessors[0].recurse)

        payload["current_defect_status"] = "unknown"
        payload["predecessors"] = []
        payload["missing_evidence"] = ["The candidate decision has not been assessed."]
        payload["confidence"] = 0.0
        with self.assertRaisesRegex(ValueError, "every offered predecessor"):
            validate_causal_step_payload(payload, request=request)

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
    def test_zero_confidence_cannot_confirm_or_reject_a_root(self):
        payload = valid_confirmation_payload()
        payload["confidence"] = 0.0

        with self.assertRaisesRegex(
            ValueError, "non-unknown confirmation requires positive confidence"
        ):
            validate_recursive_confirmation(
                payload, request=sample_confirmation_request()
            )

    def test_binding_rejects_foreign_hypothesis_semantic_hash(self):
        request = sample_confirmation_request()
        confirmation = validate_recursive_confirmation(
            valid_confirmation_payload(), request=request
        )
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, "semantic hash"):
            bind_root_confirmation(
                replace(confirmation, hypothesis_semantic_hash="semantic:foreign"),
                request=request,
            )

    def test_artifact_confirmation_recomputes_hash_owner_and_utf8_range(self):
        content = "prefix-需求描述质量-suffix"
        encoded = content.encode("utf-8")
        start = encoded.index("需求".encode("utf-8"))
        end = start + len("需求描述质量".encode("utf-8"))
        manifest = strict_manifest(content, start=start, end=end)
        candidate = {
            **reference_envelope("record:decision"),
            "artifact_hydration": manifest,
        }
        payload = valid_confirmation_payload()
        payload["excerpt"] = "需求描述质量"
        payload["evidence_refs"] = ["record:decision"]

        result = validate_recursive_confirmation(
            payload, request=sample_confirmation_request(candidate_reference=candidate)
        )
        self.assertEqual(result.status, "confirmed")

        mutations = (
            ("content_hash", "sha256:" + "0" * 64, "content_hash"),
            ("byte_range", [end, start], "byte_range"),
            ("byte_range", [0, len(encoded) + 1], "byte_range"),
            ("byte_count", len(encoded) + 1, "byte_count"),
            ("owner_reference", reference_envelope("record:other"), "owner"),
            ("artifact_id", "artifact:decision-payload", "canonical"),
        )
        for field, value, error in mutations:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                forged = strict_manifest(content, start=start, end=end)
                forged["hydrated_artifacts"][0][field] = value
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(candidate_reference={
                        **reference_envelope("record:decision"),
                        "artifact_hydration": forged,
                    }),
                )

        outside = valid_confirmation_payload()
        outside["excerpt"] = "prefix"
        outside["evidence_refs"] = ["record:decision"]
        with self.assertRaisesRegex(ValueError, "grounded excerpt"):
            validate_recursive_confirmation(
                outside, request=sample_confirmation_request(candidate_reference=candidate)
            )

    def test_open_competitor_requires_auditable_structured_comparison(self):
        competitor_path = ("record:prompt", "record:change")
        competitor_identity = confirmation_identity_for(
            hypothesis_id="hyp:alternative",
            hypothesis_semantic_hash="semantic:alternative",
            candidate_ref="record:prompt",
            defect_fingerprint=sample_step_request().defect_state.fingerprint,
            recursive_path=competitor_path,
        )
        competitor = {
            "hypothesis_id": "hyp:alternative",
            "hypothesis_semantic_hash": "semantic:alternative",
            "status": "active",
            "claim": "The prompt omission introduced the defect.",
            "active_defect": sample_step_request().defect_state.to_dict(),
            "candidate_reference": {
                **reference_envelope("record:prompt"),
                "content": "The prompt omitted the compatibility requirement.",
            },
            "supporting_evidence": [{
                "reason": "The requirement is absent.",
                "confidence": 0.8,
                "evidence_reference": reference_envelope("record:prompt"),
            }],
            "opposing_evidence": [],
            "unresolved_questions": ["Could repository discovery compensate?"],
            "counterfactual": {"intervention_ref": "record:prompt"},
            "confirmation_identity": competitor_identity,
            "recursive_path": list(competitor_path),
            "requires_independent_confirmation": True,
        }
        request = sample_confirmation_request(competing_hypotheses=(competitor,))
        payload = valid_confirmation_payload()
        with self.assertRaisesRegex(ValueError, "competitor_comparisons"):
            validate_recursive_confirmation(payload, request=request)

        payload["competitor_comparisons"] = [{
            "hypothesis_id": "hyp:alternative",
            "hypothesis_semantic_hash": "semantic:alternative",
            "candidate_ref": "record:prompt",
            "defect_fingerprint": sample_step_request().defect_state.fingerprint,
            "confirmation_identity": competitor_identity,
            "recursive_path": list(competitor_path),
            "requires_independent_confirmation": True,
            "status": "unresolved",
            "reason": "The alternative remains nondominated.",
            "evidence_refs": ["record:prompt"],
        }]
        forged = dict(payload)
        forged["competitor_comparisons"] = [
            {**payload["competitor_comparisons"][0], "candidate_ref": "record:other"}
        ]
        with self.assertRaisesRegex(ValueError, "competitor.*identity"):
            validate_recursive_confirmation(forged, request=request)
        with self.assertRaisesRegex(ValueError, "unresolved competitor"):
            validate_recursive_confirmation(payload, request=request)

        payload["status"] = "unknown"
        payload["factor_role"] = "unknown"
        payload["excerpt"] = ""
        payload["counterfactual"] = {
            "intervention_ref": "record:decision",
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_defect_status": "unknown",
            "causal_effect": "unknown",
        }
        self.assertEqual(
            validate_recursive_confirmation(payload, request=request).status, "unknown"
        )

    def test_factor_role_requires_grounded_evidence_and_structured_mechanism(self):
        payload = valid_confirmation_payload()
        payload.update({
            "status": "rejected",
            "factor_role": "amplifying_factor",
            "excerpt": "",
            "evidence_refs": [],
            "counterfactual": {
                "intervention_ref": "record:decision",
                "intervention_kind": "replace_with_semantically_correct_behavior",
                "predicted_defect_status": "present",
                "causal_effect": "does_not_prevent_defect",
            },
            "factor_mechanism": {
                "mechanism_type": "amplification",
                "source_ref": "record:decision",
                "target_ref": "record:change",
                "effect": "Increases defect severity.",
            },
        })
        with self.assertRaisesRegex(ValueError, "factor.*evidence"):
            validate_recursive_confirmation(payload, request=sample_confirmation_request())
        payload["evidence_refs"] = ["record:decision"]
        with self.assertRaisesRegex(ValueError, "source and target"):
            validate_recursive_confirmation(payload, request=sample_confirmation_request())
        payload["evidence_refs"] = ["record:decision", "record:change"]
        result = validate_recursive_confirmation(payload, request=sample_confirmation_request())
        self.assertEqual(result.factor_mechanism["mechanism_type"], "amplification")

    def test_rejected_unrelated_role_requires_grounded_evidence(self):
        payload = valid_confirmation_payload()
        payload.update(
            {
                "status": "rejected",
                "factor_role": "unrelated",
                "excerpt": "",
                "evidence_refs": [],
                "counterfactual": {
                    "intervention_ref": "record:decision",
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                "factor_mechanism": {},
            }
        )

        with self.assertRaisesRegex(
            ValueError, "definitive confirmation requires grounded evidence refs"
        ):
            validate_recursive_confirmation(
                payload, request=sample_confirmation_request()
            )

    def test_factual_prompt_includes_analysis_perspective(self):
        request = sample_confirmation_request()
        prompt = build_recursive_confirmation_prompt(request)
        self.assertIn(request.analysis_perspective, prompt)
        self.assertEqual(
            request.factual_dict()["analysis_perspective"],
            request.analysis_perspective,
        )
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
        artifact_content = "Implement only the explicitly listed methods."
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "decisive": True,
            "artifact_hydration": strict_manifest(artifact_content),
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
            "artifact_hydration": strict_manifest(
                artifact_content, owner_ref="record:evidence"
            ),
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

    def test_grounded_open_competitor_is_visible_but_unresolved_facts_block_confirmation(self):
        closed = {
            "hypothesis_id": "hyp_closed",
            "status": "rejected",
            "candidate_reference": reference_envelope("record:alternative"),
            "evidence_references": [reference_envelope("record:alternative_evidence")],
        }
        competitor_path = ("record:alternative", "record:change")
        competitor_identity = confirmation_identity_for(
            hypothesis_id="hyp_open",
            hypothesis_semantic_hash="semantic:open",
            candidate_ref="record:alternative",
            defect_fingerprint=sample_step_request().defect_state.fingerprint,
            recursive_path=competitor_path,
        )
        open_hypothesis = {
            "hypothesis_id": "hyp_open",
            "hypothesis_semantic_hash": "semantic:open",
            "status": "supported",
            "claim": "The alternative decision introduced the defect.",
            "active_defect": sample_step_request().defect_state.to_dict(),
            "candidate_reference": {
                **reference_envelope("record:alternative"),
                "content": "The alternative decision omitted discovery.",
            },
            "supporting_evidence": [{
                "reason": "The evidence supports the alternative.",
                "confidence": 0.8,
                "evidence_reference": reference_envelope("record:alternative_evidence"),
            }],
            "opposing_evidence": [],
            "unresolved_questions": [],
            "counterfactual": {"intervention_ref": "record:alternative"},
            "confirmation_identity": competitor_identity,
            "recursive_path": list(competitor_path),
            "requires_independent_confirmation": True,
        }
        missing_status = {key: value for key, value in closed.items() if key != "status"}
        unresolved_evidence = dict(open_hypothesis)
        unresolved_evidence["supporting_evidence"] = [{
            "reason": "Unresolved evidence.",
            "confidence": 0.8,
            "evidence_reference": reference_envelope(
                "record:alternative_evidence", status="unresolved"
            ),
        }]
        validate_recursive_confirmation(
            valid_confirmation_payload(),
            request=sample_confirmation_request(competing_hypotheses=(closed,)),
        )
        open_payload = valid_confirmation_payload()
        open_payload["competitor_comparisons"] = [{
            "hypothesis_id": "hyp_open",
            "hypothesis_semantic_hash": "semantic:open",
            "candidate_ref": "record:alternative",
            "defect_fingerprint": sample_step_request().defect_state.fingerprint,
            "confirmation_identity": competitor_identity,
            "recursive_path": list(competitor_path),
            "requires_independent_confirmation": True,
            "status": "outperformed",
            "reason": "The candidate has stronger causal evidence.",
            "evidence_refs": ["record:alternative"],
        }]
        validate_recursive_confirmation(
            open_payload,
            request=sample_confirmation_request(competing_hypotheses=(open_hypothesis,)),
        )
        for hypothesis in (missing_status, unresolved_evidence):
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
        temporal_containers = [
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
        ]
        for field in ("source", "origin", "method"):
            temporal_containers.extend(
                (
                    {field: "temporal_proximity"},
                    {field: ["temporal_proximity"]},
                    {field: [["temporal_proximity"]]},
                    {
                        field: [
                            {"type": "recorded_edge"},
                            ["semantic_analysis", {"kind": "temporal_proximity"}],
                        ]
                    },
                )
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
        eligible_provenance = [
            {"source": {"type": "recorded_artifact"}},
            {"source": [{"type": "reconstructed_fact"}]},
            {"origin": {"edges": [{"type": "confirmed_edge"}]}},
            {"method": [{"details": {"kind": "semantic_analysis"}}]},
        ]
        for field in ("source", "origin", "method"):
            eligible_provenance.extend(
                (
                    {field: "recorded_artifact"},
                    {field: ["recorded_artifact"]},
                    {field: [["recorded_artifact"]]},
                    {
                        field: [
                            {"type": "recorded_edge"},
                            ["semantic_analysis", {"kind": "confirmed_edge"}],
                        ]
                    },
                )
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

    def test_provider_false_health_flags_block_in_every_fact_tree_context(self):
        provider_health_failures = (
            ("candidate", {"provider_availability": False}),
            ("candidate", {"provider_status": {"resolution": False}}),
            ("obligation", {"provider_availability": False}),
            ("obligation", {"provider_status": {"available": False}}),
            ("obligation", {"provider": [{"health": {"hydration": False}}]}),
            ("hypothesis", {"provider_resolution": False}),
            ("hypothesis", {"provider_status": {"available": False}}),
            ("hypothesis", {"provider": [[{"health": {"hydrated": False}}]]}),
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for location, failure in provider_health_failures:
            candidate_fact = {
                **reference_envelope("record:decision"),
                "content": "Implement only the explicitly listed methods.",
            }
            request_kwargs = {"supporting_evidence": (candidate_fact,)}
            if location == "candidate":
                candidate_fact.update(failure)
            elif location == "obligation":
                request_kwargs["task_obligations"] = (
                    {"source": "task", "text": "Preserve the parser contract.", **failure},
                )
            else:
                request_kwargs["competing_hypotheses"] = (
                    {
                        "hypothesis_id": "hyp_closed",
                        "status": "rejected",
                        "candidate_reference": reference_envelope("record:alternative"),
                        **failure,
                    },
                )
            with self.subTest(location=location, failure=failure), self.assertRaisesRegex(
                ValueError, "blocking"
            ):
                validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(**request_kwargs),
                )

    def test_non_provider_domain_false_flags_remain_eligible(self):
        domain_facts = (
            {"tool_request": {"status": "failed", "available": False}},
            {"tool_status": {"resolution": False}},
            {"execution_result": {"hydration": False}},
        )
        payload = {**valid_confirmation_payload(), "evidence_refs": ["record:decision"]}
        for domain_fact in domain_facts:
            with self.subTest(domain_fact=domain_fact):
                result = validate_recursive_confirmation(
                    payload,
                    request=sample_confirmation_request(
                        supporting_evidence=(
                            {
                                **reference_envelope("record:decision"),
                                "content": "Implement only the explicitly listed methods.",
                            },
                        ),
                        task_obligations=(
                            {
                                "source": "task",
                                "text": "Preserve the parser contract.",
                                **domain_fact,
                            },
                        ),
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
        manifest = strict_manifest("Implement only the explicitly listed methods.")

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
                            confirmation_counterfactual_for(
                                "record:decision",
                                status,
                                counterfactual_status={
                                    "prevents_defect": "supports_causality",
                                    "does_not_prevent_defect": "rejects_causality",
                                    "unknown": "unknown",
                                }[effect],
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
    def process_confirmation_request(self):
        process_facts = {
            "schema": "candidate-process-confirmation-facts/v1",
            "candidate_commitment_cues": {
                "schema": "candidate-commitment-cues/v1",
                "candidate_ref": "record:decision",
                "candidate_reference": reference_envelope(
                    "record:decision"
                ),
                "cue_count": 1,
                "cues": [
                    {
                        "cue_id": "commitment_cue:test",
                        "verbatim_excerpt": "I will implement the repair.",
                        "semantic_status": (
                            "candidate_cue_not_a_commitment_verdict"
                        ),
                    }
                ],
            },
            "candidate_process_trajectory": {
                "schema": "candidate-process-trajectory/v1",
                "candidate_ref": "record:decision",
                "candidate_reference": reference_envelope(
                    "record:decision"
                ),
                "post_candidate_mutation_count": 0,
                "post_candidate_verification_count": 0,
                "post_candidate_delivery_observed": False,
                "episode_summaries": [
                    {
                        "episode_ref": "record:change",
                        "reference": reference_envelope(
                            "record:change",
                            provenance="reconstructed",
                        ),
                    }
                ],
            },
        }
        return replace(
            sample_confirmation_request(),
            defect_state=DefectState.create(
                label="candidate_local_process_defect",
                expected="The committed repair is delivered and verified.",
                actual="No mutation or verification followed the commitment.",
                mechanism="The decision did not materialize its repair obligation.",
                scope="candidate_local_process_execution",
            ),
            process_factual_context=process_facts,
        )

    def process_confirmation_payload(self):
        payload = valid_confirmation_payload()
        payload["evidence_refs"] = [
            "record:decision",
            "record:change",
        ]
        payload["process_confirmation_assessment"] = {
            "candidate_role": "implementation_commitment",
            "commitment_cue_disposition": "commitment",
            "commitment_status": "unfulfilled",
            "trajectory_relation": "diverged",
            "intervention_scope": "decision_and_committed_followup",
            "task_precondition_disposition": "repair_obligation",
            "active_process_defect_after_intervention": "absent",
            "downstream_failure_after_intervention": "absent",
            "reason": (
                "Fulfilling and verifying the committed repair closes the "
                "candidate-local omission and the downstream failure."
            ),
            "evidence_refs": ["record:decision", "record:change"],
        }
        return payload

    def test_process_confirmation_requires_structured_counterfactual_assessment(self):
        confirmation = validate_recursive_confirmation(
            self.process_confirmation_payload(),
            request=self.process_confirmation_request(),
        )

        self.assertEqual(
            confirmation.process_confirmation_assessment[
                "task_precondition_disposition"
            ],
            "repair_obligation",
        )

    def test_process_confirmation_rejects_counterfactual_self_contradiction(self):
        payload = self.process_confirmation_payload()
        payload.update(
            {
                "status": "rejected",
                "factor_role": "unrelated",
                "excerpt": "",
                "counterfactual": {
                    "intervention_ref": "record:decision",
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                "factor_mechanism": None,
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "process confirmation assessment contradicts top-level counterfactual",
        ):
            validate_recursive_confirmation(
                payload,
                request=self.process_confirmation_request(),
            )

    def test_process_confirmation_receives_facts_without_first_pass_verdict(self):
        process_facts = {
            "schema": "candidate-process-confirmation-facts/v1",
            "candidate_commitment_cues": {
                "schema": "candidate-commitment-cues/v1",
                "candidate_ref": "record:decision",
                "candidate_reference": reference_envelope(
                    "record:decision"
                ),
                "cue_count": 1,
                "cues": [
                    {
                        "cue_id": "commitment_cue:test",
                        "verbatim_excerpt": "I will implement the repair.",
                        "semantic_status": (
                            "candidate_cue_not_a_commitment_verdict"
                        ),
                    }
                ],
            },
            "candidate_process_trajectory": {
                "schema": "candidate-process-trajectory/v1",
                "candidate_ref": "record:decision",
                "candidate_reference": reference_envelope(
                    "record:decision"
                ),
                "post_candidate_mutation_count": 0,
                "post_candidate_verification_count": 0,
                "post_candidate_delivery_observed": False,
                "episode_summaries": [
                    {
                        "episode_ref": "record:change",
                        "reference": reference_envelope(
                            "record:change",
                            provenance="reconstructed",
                        ),
                    }
                ],
            },
        }
        request = replace(
            sample_confirmation_request(),
            process_factual_context=process_facts,
        )

        facts = request.factual_dict()
        prompt = build_recursive_confirmation_prompt(request)

        self.assertEqual(
            facts["process_factual_context"]["schema"],
            "candidate-process-confirmation-facts/v1",
        )
        self.assertNotIn("process_assessment", prompt)
        self.assertIn("candidate_process_trajectory", prompt)
        self.assertIn("independently classify", prompt)
        self.assertIn("trace-visible Agent decision output", prompt)
        self.assertIn("task precondition", prompt)
        self.assertIn("obligation-consistent follow-up action", prompt)

    def test_step_and_confirmation_prompts_define_complete_causal_contract(self):
        prompts = (
            build_causal_step_prompt(sample_step_request()),
            build_recursive_confirmation_prompt(sample_confirmation_request()),
        )
        required = (
            "Temporal order or proximity alone is never causal.",
            "Every field shown in required_json_schema is mandatory",
            "top-level confidence must be an unquoted JSON number",
            "same_defect_propagation",
            "defect_transformation",
            "introduction_candidate",
            "contributing_condition",
            "outcome_evidence",
            "unrelated",
            "unknown",
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
        step_prompt = prompts[0]
        self.assertIn("sparse ranked assessments", step_prompt)
        self.assertIn("current node itself is never a predecessor", step_prompt)
        self.assertIn("earliest trace-visible introduction", step_prompt)
        self.assertIn("unknown external sender", step_prompt)
        self.assertIn("root_confirmation_action_schema", step_prompt)
        self.assertIn("earliest trace-visible introduction", confirmation_prompt)
        self.assertIn("causal_factor_mechanism_schema", confirmation_prompt)
        for phrase in (
            "every nested mapping and list",
            "provider_error",
            "budget_exhausted",
            "reference-bearing key",
        ):
            with self.subTest(confirmation_phrase=phrase):
                self.assertIn(phrase, confirmation_prompt)
        self.assertEqual(ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION, "recursive-root-confirmation-v14")


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


class ProviderFailureClassificationTest(unittest.TestCase):
    def test_structured_provider_failures_have_stable_retry_dispositions(self):
        cases = (
            (400, "invalid_request_error", False),
            (400, "schema_validation_error", False),
            (400, "validation_error", False),
            (400, "unsupported_model", False),
            (400, "invalid_endpoint", False),
            (400, "bad_request", True),
            (400, "temporarily_unavailable", True),
            (400, "", True),
            (401, "authentication_error", False),
            (402, "invalid_request_error", False),
            (403, "permission_error", False),
            (404, "not_found_error", False),
            (408, "request_timeout", True),
            (409, "conflict", True),
            (425, "too_early", True),
            (429, "rate_limit_error", True),
            (503, "service_unavailable", True),
            (None, "connect_timeout", True),
        )

        for status, code, retryable in cases:
            with self.subTest(status=status, code=code):
                disposition = provider_errors.classify_provider_failure(
                    status=status,
                    code=code,
                )
                self.assertEqual(disposition.retryable, retryable)
                self.assertEqual(disposition.status_code, status)
                self.assertEqual(disposition.error_code, code)

    def test_retryable_circuit_records_the_first_failure_not_the_opening_failure(self):
        class FailingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                error = RuntimeError("temporarily unavailable")
                error.status_code = 503
                error.code = "service_unavailable"
                raise error

        messages = FailingMessages()
        transport = object.__new__(ClaudeJudgeClient)
        transport.timeout_seconds = None
        transport.thinking_config = None
        transport.model = "test-model"
        transport.client = types.SimpleNamespace(messages=messages)
        transport.request_count = 0
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 0
        transport.provider_circuit_open = False
        transport.provider_circuit_reason = ""
        transport.provider_circuit_disposition = None
        transport.provider_circuit_first_request = 0
        transport.provider_circuit_first_failure_at = ""

        with self.assertRaises(TransportCallError):
            transport.create_message_text_with_usage(
                system="system",
                messages=[{"role": "user", "content": "prompt"}],
                max_tokens=32,
            )
        first_failure_at = transport.provider_circuit_stats["first_failure_at"]
        self.assertEqual(transport.provider_circuit_stats["first_request"], 1)
        self.assertTrue(first_failure_at)
        self.assertFalse(transport.provider_circuit_open)

        for _ in range(2):
            with self.assertRaises(TransportCallError):
                transport.create_message_text_with_usage(
                    system="system",
                    messages=[{"role": "user", "content": "prompt"}],
                    max_tokens=32,
                )

        self.assertTrue(transport.provider_circuit_open)
        self.assertEqual(transport.provider_circuit_stats["first_request"], 1)
        self.assertEqual(
            transport.provider_circuit_stats["first_failure_at"],
            first_failure_at,
        )

    def test_success_resets_active_failure_streak_provenance(self):
        class SequencedMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                if self.calls != 2:
                    error = RuntimeError("temporarily unavailable")
                    error.status_code = 503
                    error.code = "service_unavailable"
                    raise error
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="recovered")]
                )

        messages = SequencedMessages()
        transport = object.__new__(ClaudeJudgeClient)
        transport.timeout_seconds = None
        transport.thinking_config = None
        transport.model = "test-model"
        transport.client = types.SimpleNamespace(messages=messages)
        transport.request_count = 0
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 0
        transport.provider_circuit_open = False
        transport.provider_circuit_reason = ""
        transport.provider_circuit_disposition = None
        transport.provider_circuit_first_request = 0
        transport.provider_circuit_first_failure_at = ""

        with self.assertRaises(TransportCallError):
            transport.create_message_text_with_usage(
                system="system",
                messages=[{"role": "user", "content": "prompt"}],
                max_tokens=32,
            )
        self.assertEqual(transport.provider_circuit_stats["first_request"], 1)

        recovered = transport.create_message_text_with_usage(
            system="system",
            messages=[{"role": "user", "content": "prompt"}],
            max_tokens=32,
        )
        self.assertEqual(recovered.text, "recovered")
        self.assertEqual(transport.provider_circuit_stats["first_request"], 0)
        self.assertEqual(transport.provider_circuit_stats["first_failure_at"], "")
        self.assertIsNone(transport.provider_circuit_stats["disposition"])

        for _ in range(3):
            with self.assertRaises(TransportCallError):
                transport.create_message_text_with_usage(
                    system="system",
                    messages=[{"role": "user", "content": "prompt"}],
                    max_tokens=32,
                )

        self.assertTrue(transport.provider_circuit_open)
        self.assertEqual(transport.provider_circuit_stats["first_request"], 3)
        self.assertTrue(transport.provider_circuit_stats["first_failure_at"])
        self.assertEqual(
            transport.provider_circuit_stats["disposition"]["status_code"],
            503,
        )

    def test_anthropic_sdk_bad_request_shape_opens_circuit_immediately(self):
        class AnthropicBadRequestError(RuntimeError):
            def __init__(self):
                self.status_code = 400
                self.type = "invalid_request_error"
                self.body = {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_request_error",
                        "message": "model parameter is invalid",
                    },
                }
                super().__init__("Bad Request")

        class FailingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                raise AnthropicBadRequestError()

        messages = FailingMessages()
        transport = object.__new__(ClaudeJudgeClient)
        transport.timeout_seconds = None
        transport.thinking_config = None
        transport.model = "test-model"
        transport.client = types.SimpleNamespace(messages=messages)
        transport.request_count = 0
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 0
        transport.provider_circuit_open = False
        transport.provider_circuit_reason = ""
        transport.provider_circuit_disposition = None
        transport.provider_circuit_first_request = 0
        transport.provider_circuit_first_failure_at = ""

        with self.assertRaises(TransportCallError) as raised:
            transport.create_message_text_with_usage(
                system="system",
                messages=[{"role": "user", "content": "prompt"}],
                max_tokens=32,
            )

        self.assertIsInstance(raised.exception.error, JudgeProviderUnavailable)
        self.assertEqual(messages.calls, 1)
        self.assertTrue(transport.provider_circuit_open)
        disposition = transport.provider_circuit_stats["disposition"]
        self.assertFalse(disposition["retryable"])
        self.assertEqual(disposition["status_code"], 400)
        self.assertEqual(disposition["error_code"], "invalid_request_error")
        self.assertIn("model parameter is invalid", disposition["reason"])

    def test_402_opens_provider_circuit_after_one_physical_request(self):
        class StructuredProviderError(RuntimeError):
            def __init__(self):
                self.status_code = 402
                self.code = "invalid_request_error"
                super().__init__("insufficient balance")

        class FailingMessages:
            def __init__(self):
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                raise StructuredProviderError()

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

        with self.assertRaises(TransportCallError) as raised:
            transport.create_message_text_with_usage(
                system="system",
                messages=[{"role": "user", "content": "prompt"}],
                max_tokens=32,
            )

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertIsInstance(raised.exception.error, JudgeProviderUnavailable)
        self.assertEqual(messages.calls, 1)
        self.assertTrue(transport.provider_circuit_open)
        self.assertEqual(
            transport.provider_circuit_stats["disposition"]["status_code"],
            402,
        )

    def test_restored_disposition_mapping_is_serialized_without_attribute_error(self):
        disposition = provider_errors.classify_provider_failure(
            status=402,
            code="invalid_request_error",
            reason="insufficient balance",
        )
        transport = object.__new__(ClaudeJudgeClient)
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 1
        transport.provider_circuit_open = True
        transport.provider_circuit_reason = "payment required"
        transport.provider_circuit_disposition = disposition.to_dict()
        transport.provider_circuit_first_request = 7
        transport.provider_circuit_first_failure_at = "2026-07-31T00:00:00Z"

        self.assertEqual(
            transport.provider_circuit_stats["disposition"],
            disposition.to_dict(),
        )
        self.assertEqual(
            transport.provider_circuit_stats["first_failure_at"],
            "2026-07-31T00:00:00Z",
        )

    def test_worker_error_result_preserves_structured_provider_fields(self):
        with self.assertRaises(JudgeProviderError) as raised:
            claude_module.raise_worker_failure(
                {
                    "ok": False,
                    "error_type": "APIStatusError",
                    "error": "payment required",
                    "status_code": 402,
                    "error_code": "invalid_request_error",
                }
            )

        disposition = provider_errors.provider_failure_disposition(raised.exception)
        self.assertIsNotNone(disposition)
        self.assertEqual(disposition.status_code, 402)
        self.assertEqual(disposition.error_code, "invalid_request_error")

    def test_timeout_worker_serializes_real_structured_402_result(self):
        class StructuredProviderError(RuntimeError):
            def __init__(self):
                self.status_code = 402
                self.code = "insufficient_balance"
                super().__init__("payment required")

        class FakeAnthropic:
            def __init__(self, **_kwargs):
                self.messages = types.SimpleNamespace(
                    create=lambda **_request: (_ for _ in ()).throw(
                        StructuredProviderError()
                    )
                )

        results = []
        with mock.patch.dict(
            "sys.modules",
            {"anthropic": types.SimpleNamespace(Anthropic=FakeAnthropic)},
        ):
            claude_module.anthropic_request_worker(
                {
                    "api_key": "test-key",
                    "base_url": "https://provider.invalid",
                    "timeout_seconds": 60,
                    "model": "test-model",
                    "max_tokens": 32,
                    "temperature": 0,
                    "thinking": None,
                    "system": "system",
                    "messages": [{"role": "user", "content": "prompt"}],
                },
                types.SimpleNamespace(put=results.append),
            )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["status_code"], 402)
        self.assertEqual(results[0]["error_code"], "insufficient_balance")
        with self.assertRaises(JudgeProviderError) as raised:
            claude_module.raise_worker_failure(results[0])
        disposition = provider_errors.provider_failure_disposition(
            raised.exception
        )
        self.assertIsNotNone(disposition)
        self.assertFalse(disposition.retryable)
        self.assertEqual(disposition.status_code, 402)


class ClaudeCausalJudgeTest(unittest.TestCase):
    def test_confirmation_repair_names_exact_process_evidence_refs(self):
        request = CausalJudgePromptTest().process_confirmation_request()

        constraints = _repair_constraints(
            stage="recursive_root_confirmation",
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validation_error=(
                "ValueError: process confirmation assessment requires "
                "candidate and trajectory evidence"
            ),
        )

        resolution = constraints[
            "required_process_confirmation_evidence_resolution"
        ]
        self.assertEqual(
            resolution["required_candidate_evidence_ref"],
            "record:decision",
        )
        self.assertEqual(
            resolution["allowed_trajectory_evidence_refs"],
            ["record:change"],
        )

    def test_process_repair_names_exact_trajectory_and_obligation_refs(self):
        request = replace(
            ProcessDefectPromptTests().process_request(),
            candidates=(),
        )

        constraints = _repair_constraints(
            stage="recursive_causal_step",
            node_ref=request.current_node.ref,
            request_context=request.to_dict(),
            validation_error=(
                "ValueError: unfulfilled commitment requires grounded "
                "candidate and trajectory evidence"
            ),
        )

        resolution = constraints["required_process_evidence_resolution"]
        self.assertEqual(
            resolution["required_candidate_evidence_ref"],
            "record:change",
        )
        self.assertEqual(
            resolution["allowed_trajectory_evidence_refs"],
            ["record:trajectory_episode"],
        )
        self.assertEqual(
            resolution["allowed_obligation_refs"],
            ["analysis_objective"],
        )

    def test_process_repair_requires_root_action_when_no_predecessor_exists(self):
        request = replace(
            ProcessDefectPromptTests().process_request(),
            candidates=(),
        )
        constraints = _repair_constraints(
            stage="recursive_causal_step",
            node_ref=request.current_node.ref,
            request_context=request.to_dict(),
            validation_error=(
                "ValueError: a present defect cannot terminate silently "
                "without recursion, introduction, or missing_evidence"
            ),
        )

        resolution = constraints["required_process_root_resolution"]
        self.assertEqual(
            resolution["when"],
            {
                "current_defect_status": "present",
                "offered_predecessor_refs": [],
            },
        )
        self.assertEqual(
            resolution["preferred_resolution"],
            {
                "candidate_introduction": True,
                "missing_evidence": [],
                "suggested_investigation": {
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": "hyp:process",
                        "candidate_ref": "record:change",
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "non-empty grounded reason",
                },
            },
        )
        self.assertIn("process_assessment", resolution["preserve_fields"])

    def test_confirmation_repair_names_every_required_competitor(self):
        constraints = _repair_constraints(
            stage="recursive_root_confirmation",
            node_ref="record:factor",
            request_context={
                "competing_hypotheses": [
                    {
                        "candidate_reference": {
                            "resolved_ref": "record:root",
                        },
                        "confirmation_identity": "confirmation:root",
                        "hypothesis_id": "hyp:root",
                    }
                ]
            },
            validation_error=(
                "ValueError: competitor_comparisons must cover every open "
                "competitor"
            ),
        )

        self.assertEqual(
            constraints["required_competitor_comparisons"],
            [
                {
                    "candidate_ref": "record:root",
                    "confirmation_identity": "confirmation:root",
                    "hypothesis_id": "hyp:root",
                }
            ],
        )
        self.assertTrue(constraints["exact_competitor_coverage"])

    def test_local_open_circuit_rejection_costs_zero_physical_requests(self):
        transport = object.__new__(ClaudeJudgeClient)
        transport.model = "test-model"
        transport.max_tokens = 2048
        transport.repair_max_tokens = 512
        transport.thinking_config = None
        transport.provider_circuit_open = True
        transport.provider_circuit_reason = "local circuit is open"
        transport.request_count = 0
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=1
        )

        self.assertEqual(result.physical_requests, 0)
        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertEqual(transport.request_count, 0)

    def test_local_transport_preflight_failure_costs_zero_physical_requests(self):
        transport = object.__new__(ClaudeJudgeClient)
        transport.model = "test-model"
        transport.max_tokens = 2048
        transport.repair_max_tokens = 512
        transport.thinking_config = None
        transport.provider_circuit_open = False
        transport.request_count = 0
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=1
        )

        self.assertEqual(result.physical_requests, 0)
        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertEqual(transport.request_count, 0)

    def test_local_repair_rejection_does_not_add_a_physical_request(self):
        class RepairCircuitTransport(ScriptedTransport):
            def create_message_text_with_usage(self, *, system, messages, max_tokens):
                if self.request_count == 1:
                    raise TransportCallError(
                        JudgeProviderUnavailable("repair circuit opened locally"),
                        physical_requests=0,
                    )
                return super().create_message_text_with_usage(
                    system=system, messages=messages, max_tokens=max_tokens
                )

        transport = RepairCircuitTransport([
            json.dumps(valid_step_payload(predecessor_ref="record:fabricated"))
        ])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=2
        )

        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertEqual(transport.request_count, 1)

    def test_cache_adapter_failure_after_transport_raises_typed_usage_error(self):
        class FailingCache(JudgmentCache):
            def put_payload(self, **kwargs):
                raise RuntimeError("cache adapter failed")

        transport = ScriptedTransport([json.dumps(valid_step_payload())])
        judge = ClaudeCausalJudge(transport=transport, cache=FailingCache())

        with self.assertRaises(BoundedJudgeCallError) as raised:
            judge.judge_step_bounded(
                sample_step_request(), max_physical_requests=1
            )

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertEqual(transport.request_count, 1)

    def test_bounded_runtime_failure_after_transport_attempt_preserves_physical_usage(self):
        transport = ScriptedTransport([RuntimeError("adapter decode failed")])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=1
        )

        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertIn("RuntimeError", " ".join(result.value.missing_evidence))
        self.assertEqual(transport.request_count, 1)

    def test_bounded_step_allows_one_valid_physical_request(self):
        transport = ScriptedTransport([json.dumps(valid_step_payload())])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=1
        )

        self.assertEqual(result.value.current_defect_status, "present")
        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(transport.request_count, 1)

    def test_bounded_step_cache_hit_costs_zero_requests(self):
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport([json.dumps(valid_step_payload())])
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            first = judge.judge_step_bounded(sample_step_request(), max_physical_requests=1)

            result = judge.judge_step_bounded(
                sample_step_request(), max_physical_requests=0
            )

        self.assertEqual(first.physical_requests, 1)
        self.assertEqual(result.value.current_defect_status, "present")
        self.assertEqual(result.physical_requests, 0)
        self.assertEqual(transport.request_count, 1)

    def test_bounded_step_blocks_repair_after_one_physical_request(self):
        invalid = json.dumps(valid_step_payload(predecessor_ref="record:fabricated"))
        transport = ScriptedTransport([invalid])
        cache = JudgmentCache()
        judge = ClaudeCausalJudge(transport=transport, cache=cache)

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=1
        )

        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertEqual(result.physical_requests, 1)
        self.assertTrue(
            any("judge_request_budget_exhausted" in item for item in result.value.missing_evidence)
        )
        self.assertEqual(transport.request_count, 1)
        self.assertEqual(cache.stats()["writes"], 0)

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
            self.assertEqual(transport.calls[1]["max_tokens"], transport.max_tokens)
            self.assertEqual(transport.request_count, 2)
            self.assertEqual(cache.stats()["writes"], 1)

    def test_focused_repair_names_the_exact_predecessor_boundary_and_valid_endings(self):
        invalid = valid_step_payload(predecessor_ref="record:change")
        transport = ScriptedTransport([json.dumps(invalid), json.dumps(valid_step_payload())])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        judge.judge_step(sample_step_request())

        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        constraints = repair_payload["repair_constraints"]
        self.assertEqual(constraints["current_node_ref"], "record:change")
        self.assertEqual(constraints["offered_predecessor_refs"], ["record:decision"])
        self.assertIn(
            "must never appear in predecessors",
            constraints["current_node_is_not_a_predecessor"],
        )
        self.assertEqual(
            constraints["valid_present_defect_endings"],
            [
                "recurse through one or two offered grounded predecessors",
                "declare candidate_introduction after assessing every offered predecessor and request root confirmation",
                "return concrete blocking missing_evidence",
            ],
        )

    def test_full_retry_recovers_after_two_responses_omit_required_confidence(self):
        invalid = valid_step_payload()
        invalid.pop("confidence")
        transport = ScriptedTransport(
            [json.dumps(invalid), json.dumps(invalid), json.dumps(valid_step_payload())]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=3
        )

        self.assertEqual(result.value.current_defect_status, "present")
        self.assertEqual(result.physical_requests, 3)
        self.assertEqual(transport.request_count, 3)
        retry_payload = json.loads(transport.calls[2]["messages"][0]["content"])
        self.assertEqual(retry_payload["retry_instruction"], "Return a complete replacement JSON object.")
        self.assertIn("confidence must be numeric", retry_payload["validation_errors"][0])
        self.assertIn("confidence must be numeric", retry_payload["validation_errors"][1])
        self.assertEqual(transport.calls[2]["system"], CAUSAL_STEP_SYSTEM_PROMPT)

    def test_bounded_step_blocks_full_retry_after_two_physical_requests(self):
        invalid = valid_step_payload()
        invalid.pop("confidence")
        transport = ScriptedTransport([json.dumps(invalid), json.dumps(invalid)])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.judge_step_bounded(
            sample_step_request(), max_physical_requests=2
        )

        self.assertEqual(result.value.current_defect_status, "unknown")
        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        self.assertTrue(
            any(
                "judge_request_budget_exhausted before full retry" in item
                for item in result.value.missing_evidence
            )
        )

    def test_invalid_repair_returns_auditable_unknown_and_is_not_cached(self):
        invalid = json.dumps(valid_step_payload(predecessor_ref="record:fabricated"))
        transport = ScriptedTransport([invalid, invalid, invalid, invalid, invalid, invalid])
        with tempfile.TemporaryDirectory() as tempdir:
            cache = JudgmentCache(Path(tempdir) / "cache.jsonl")
            judge = ClaudeCausalJudge(transport=transport, cache=cache)

            first = judge.judge_step(sample_step_request())
            second = judge.judge_step(sample_step_request())

            self.assertEqual(first.current_defect_status, "unknown")
            self.assertFalse(first.candidate_introduction)
            self.assertTrue(any("validation" in item for item in first.missing_evidence))
            self.assertEqual(second.current_defect_status, "unknown")
            self.assertEqual(transport.request_count, 6)
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

    def test_rejected_confirmation_repair_requires_nonempty_grounded_evidence(self):
        invalid = valid_confirmation_payload()
        invalid.update(
            {
                "status": "rejected",
                "factor_role": "unrelated",
                "excerpt": "",
                "evidence_refs": [],
                "counterfactual": {
                    "intervention_ref": "record:decision",
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
            }
        )
        repaired = dict(invalid)
        repaired["evidence_refs"] = ["record:decision"]
        transport = ScriptedTransport(
            [json.dumps(invalid), json.dumps(repaired)]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = judge.confirm_candidate_bounded(
            sample_confirmation_request(), max_physical_requests=2
        )

        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        self.assertEqual(result.value.status, "rejected")
        self.assertEqual(result.value.factor_role, "unrelated")
        self.assertEqual(result.value.evidence_refs, ("record:decision",))
        self.assertIn(
            "non-empty array",
            repair_payload["repair_constraints"]["required_field_corrections"][0][
                "require"
            ]["evidence_refs"],
        )

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
        temporal_scalar_shapes = (
            "temporal_proximity",
            ["temporal_proximity"],
            [["temporal_proximity"]],
            [
                {"type": "recorded_edge"},
                ["semantic_analysis", {"kind": "temporal_proximity"}],
            ],
        )
        temporal_scalar_requests = tuple(
            sample_confirmation_request(
                supporting_evidence=(
                    {
                        **reference_envelope("record:decision"),
                        "content": "Implement only the explicitly listed methods.",
                        field: shape,
                    },
                )
            )
            for field in ("source", "origin", "method")
            for shape in temporal_scalar_shapes
        )
        false_provider_candidate = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "provider_availability": False,
        }
        scalar_provider_failure = {
            **reference_envelope("record:decision"),
            "content": "Implement only the explicitly listed methods.",
            "provider_failure": "timeout",
        }
        false_provider_obligation = {
            "source": "task",
            "text": "Preserve the parser contract.",
            "provider_status": {"available": False},
        }
        false_provider_hypothesis = {
            "hypothesis_id": "hyp_closed",
            "status": "rejected",
            "candidate_reference": reference_envelope("record:alternative"),
            "provider": [{"health": {"hydration": False}}],
        }
        for request in (
            sample_confirmation_request(opposing_evidence=(unresolved_opposition,)),
            sample_confirmation_request(supporting_evidence=(missing_artifact,)),
            sample_confirmation_request(supporting_evidence=(nested_provider_error,)),
            sample_confirmation_request(task_obligations=(provider_error_obligation,)),
            sample_confirmation_request(supporting_evidence=(aliased_blocker,)),
            sample_confirmation_request(supporting_evidence=(temporal_source,)),
            *temporal_scalar_requests,
            sample_confirmation_request(supporting_evidence=(scalar_provider_failure,)),
            sample_confirmation_request(supporting_evidence=(false_provider_candidate,)),
            sample_confirmation_request(task_obligations=(false_provider_obligation,)),
            sample_confirmation_request(
                competing_hypotheses=(false_provider_hypothesis,)
            ),
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
                    responses.extend([response, response])
                judge = ClaudeCausalJudge(
                    transport=ScriptedTransport(responses), cache=JudgmentCache()
                )

                result = judge.confirm_candidate(sample_confirmation_request())

                self.assertEqual(result.status, "unknown")
                self.assertIn("judge", result.reason.lower())


class CausalRootConfirmationTest(unittest.TestCase):
    def test_confirmation_prompt_binds_branch_without_first_judge_verdict(self):
        request = sample_confirmation_request()

        prompt = build_recursive_confirmation_prompt(request)
        payload = json.loads(prompt)

        self.assertEqual(payload["request"]["hypothesis_id"], "hyp:decision")
        self.assertEqual(
            payload["request"]["hypothesis_semantic_hash"], "semantic:decision"
        )
        self.assertNotIn("first Judge verdict", prompt)
        self.assertNotIn("current_defect_reason", prompt)

    def test_bounded_confirmation_cache_hit_uses_zero_physical_requests(self):
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport([json.dumps(valid_confirmation_payload())])
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            request = sample_confirmation_request()
            first = judge.confirm_candidate_bounded(
                request, max_physical_requests=1
            )
            second = judge.confirm_candidate_bounded(
                request, max_physical_requests=0
            )

        self.assertEqual(first.value, second.value)
        self.assertEqual(first.physical_requests, 1)
        self.assertEqual(second.physical_requests, 0)
        self.assertEqual(first.value.hypothesis_id, "hyp:decision")
        self.assertEqual(first.value.defect_fingerprint, request.defect_state.fingerprint)
        self.assertEqual(transport.request_count, 1)


def sample_factor_role_request() -> FactorRoleRequest:
    defect = DefectState.create(
        label="Missing required repository check.",
        expected="The repository verifies the required behavior.",
        actual="The required behavior is omitted.",
        mechanism="An incomplete decision was materialized in the change.",
        scope="record:defect",
    )
    root_hypothesis_id = "hypothesis:confirmed-root"
    root_hypothesis_semantic_hash = "sha256:confirmed-root"
    root_candidate_ref = "record:decision"
    root_recursive_path = ("record:decision", "record:defect")
    root_confirmation_identity = confirmation_identity_for(
        hypothesis_id=root_hypothesis_id,
        hypothesis_semantic_hash=root_hypothesis_semantic_hash,
        candidate_ref=root_candidate_ref,
        defect_fingerprint=defect.fingerprint,
        recursive_path=root_recursive_path,
        seed_binding_identity="seed:test",
    )
    return FactorRoleRequest(
        candidate_ref="record:prompt",
        defect_state=defect,
        recursive_path=("record:prompt", "record:decision", "record:defect"),
        candidate_reference={"ref": "record:prompt", "content": "ambiguous requirement"},
        recursive_path_references=(
            {"ref": "record:prompt", "content": "ambiguous requirement"},
            {"ref": "record:decision", "content": "omitted requirement"},
            {"ref": "record:defect", "content": "missing check"},
        ),
        supporting_evidence=({"ref": "record:prompt", "content": "ambiguous"},),
        opposing_evidence=({"ref": "record:policy", "content": "clear policy"},),
        task_obligations=({"ref": "record:task", "content": "verify behavior"},),
        confirmed_root_summaries=(
            {
                "schema": "factor-role-root-evidence-summary/v2",
                "candidate_ref": root_candidate_ref,
                "hypothesis_id": root_hypothesis_id,
                "hypothesis_semantic_hash": root_hypothesis_semantic_hash,
                "confirmation_identity": root_confirmation_identity,
                "defect_fingerprint": defect.fingerprint,
                "seed_binding_identity": "seed:test",
                "reason": "The decision introduced the omission.",
                "evidence_refs": ["record:root-evidence"],
                "recursive_path": list(root_recursive_path),
            },
        ),
        hypothesis_id="hypothesis:test",
        hypothesis_semantic_hash="sha256:test",
        seed_binding_identity="seed:test",
        analysis_perspective="Improve repository reasoning.",
    )


class _FactorStringSubclass(str):
    pass


def valid_factor_role_payload(request=None) -> dict:
    request = request or sample_factor_role_request()
    from trace_attribution.causal_judge import factor_role_request_identity

    return {
        "candidate_ref": request.candidate_ref,
        "necessity_status": "not_necessary",
        "factor_role": "contributing_condition",
        "reason": "The ambiguous requirement increased the likelihood of omission.",
        "confidence": 0.83,
        "evidence_refs": ["record:prompt", "record:decision"],
        "recursive_path": list(request.recursive_path),
        "factor_mechanism": {
            "schema": "factor-role-mechanism/v1",
            "mechanism_type": "enabling_condition",
            "source_ref": "record:prompt",
            "target_ref": "record:decision",
            "effect": "increased_defect_likelihood",
        },
        "counterfactual": {
            "schema": "factor-role-counterfactual/v1",
            "intervention_ref": "record:prompt",
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_effect": "reduces_defect_likelihood",
        },
        "hypothesis_id": request.hypothesis_id,
        "hypothesis_semantic_hash": request.hypothesis_semantic_hash,
        "defect_fingerprint": request.defect_state.fingerprint,
        "seed_binding_identity": request.seed_binding_identity,
        "analysis_perspective": request.analysis_perspective,
        "request_identity": factor_role_request_identity(request),
    }


class FactorRoleJudgeBoundaryTest(unittest.TestCase):
    def test_legacy_offline_adapter_returns_unknown_without_root_confirmation(self):
        class LegacyJudge:
            def __init__(self):
                self.confirmation_calls = 0

            def confirm_candidate(self, request):
                self.confirmation_calls += 1
                return object()

        legacy = LegacyJudge()
        adapter = OfflineCausalJudgeAdapter(legacy)

        result = adapter.judge_factor_role_offline(sample_factor_role_request())

        self.assertIsInstance(result, FactorRoleJudgment)
        self.assertEqual(result.necessity_status, "unknown")
        self.assertEqual(result.factor_role, "unknown")
        self.assertIn("does not implement", result.reason)
        self.assertEqual(legacy.confirmation_calls, 0)

    def test_inherited_protocol_stub_returns_unknown_without_calling_stub(self):
        class ProtocolLegacyJudge(CausalJudge):
            def judge_step(self, request):
                return object()

            def confirm_candidate(self, request):
                return object()

        request = sample_factor_role_request()
        result = OfflineCausalJudgeAdapter(
            ProtocolLegacyJudge()
        ).judge_factor_role_offline(request)

        self.assertEqual(result.necessity_status, "unknown")
        self.assertEqual(result.factor_role, "unknown")
        self.assertEqual(result.recursive_path, request.recursive_path)

    def test_real_factor_implementation_not_implemented_error_is_not_swallowed(self):
        class BrokenFactorJudge(CausalJudge):
            def judge_step(self, request):
                return object()

            def confirm_candidate(self, request):
                return object()

            def judge_factor_role(self, request):
                raise NotImplementedError("real factor implementation failed")

        with self.assertRaisesRegex(
            NotImplementedError, "real factor implementation failed"
        ):
            OfflineCausalJudgeAdapter(
                BrokenFactorJudge()
            ).judge_factor_role_offline(sample_factor_role_request())

    def test_legacy_unknown_fallback_preserves_request_path_exactly(self):
        request = sample_factor_role_request()
        result = OfflineCausalJudgeAdapter(
            object()
        ).judge_factor_role_offline(request)

        self.assertEqual(result.recursive_path, request.recursive_path)

    def test_offline_capability_delegates_independent_factor_method(self):
        expected = object()

        class FactorJudge(OfflineJudgeCapability):
            def judge_factor_role(self, request):
                return expected

        self.assertIs(
            FactorJudge().judge_factor_role_offline(sample_factor_role_request()),
            expected,
        )

    def test_bounded_factor_repair_and_cache_use_exact_physical_counts(self):
        request = replace(
            sample_factor_role_request(),
            candidate_reference={
                "content": {
                    "foreign_ref": "record:fabricated",
                }
            },
            supporting_evidence=(
                {
                    "content": {
                        "foreign_refs": ["record:also-fabricated"],
                    }
                },
            ),
        )
        invalid = valid_factor_role_payload()
        invalid["evidence_refs"] = ["record:fabricated"]
        invalid["request_identity"] = valid_factor_role_payload(
            request
        )["request_identity"]
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport(
                [
                    json.dumps(invalid),
                    json.dumps(valid_factor_role_payload(request)),
                ]
            )
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            repaired = judge.judge_factor_role_bounded(
                request, max_physical_requests=2
            )
            cached = judge.judge_factor_role_bounded(
                request, max_physical_requests=0
            )

        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        self.assertEqual(repaired.physical_requests, 2)
        self.assertEqual(cached.physical_requests, 0)
        self.assertEqual(repaired.value, cached.value)
        self.assertEqual(transport.request_count, 2)
        self.assertIn("record:fabricated", repair_payload["invalid_output"])
        self.assertIn("evidence_refs cite refs outside request", repair_payload["validation_error"])
        self.assertEqual(
            repair_payload["canonical_request_context"], request.factual_dict()
        )
        initial_prompt = json.loads(build_factor_role_prompt(request))
        self.assertEqual(
            initial_prompt["allowed_fact_refs"],
            repair_payload["repair_constraints"]["allowed_fact_refs"],
        )
        self.assertNotIn(
            "record:fabricated",
            repair_payload["repair_constraints"]["allowed_fact_refs"],
        )
        self.assertNotIn(
            "record:also-fabricated",
            repair_payload["repair_constraints"]["allowed_fact_refs"],
        )
        self.assertNotIn(
            "record:task",
            repair_payload["repair_constraints"]["allowed_fact_refs"],
        )
        self.assertEqual(
            repair_payload["repair_constraints"]["exact_request_identity"],
            valid_factor_role_payload(request)["request_identity"],
        )

    def test_bounded_factor_repairs_self_target_with_downstream_contract(self):
        request = sample_factor_role_request()
        invalid = valid_factor_role_payload(request)
        invalid["factor_mechanism"]["target_ref"] = request.candidate_ref
        transport = ScriptedTransport(
            [
                json.dumps(invalid),
                json.dumps(valid_factor_role_payload(request)),
            ]
        )
        result = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        ).judge_factor_role_bounded(request, max_physical_requests=2)

        repair_payload = json.loads(
            transport.calls[1]["messages"][0]["content"]
        )
        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(
            result.value.factor_mechanism["target_ref"],
            request.recursive_path[1],
        )
        self.assertIn(
            "target_ref must be a downstream recursive path ref",
            repair_payload["validation_error"],
        )
        self.assertEqual(
            repair_payload["repair_constraints"][
                "allowed_mechanism_target_refs"
            ],
            list(request.recursive_path[1:]),
        )

    def test_factor_repair_closure_rejects_each_root_identity_forgery(self):
        request = sample_factor_role_request()
        base_facts = request.factual_dict()
        root_summary = base_facts["confirmed_root_summaries"][0]
        self.assertEqual(
            root_summary["schema"],
            "factor-role-root-evidence-summary/v2",
        )
        self.assertNotIn("confirmation_status", root_summary)
        self.assertNotIn("factor_role", root_summary)
        changed_defect = request.defect_state.transformed(
            label="Different bound defect.",
            mechanism="Different bound mechanism.",
            transformation_reason="Exercise repair identity binding.",
        )
        mutations = {
            "hypothesis_id": ("hypothesis:forged", {}),
            "hypothesis_semantic_hash": ("sha256:forged", {}),
            "candidate_ref": ("record:forged-root", {}),
            "defect_fingerprint": (
                changed_defect.fingerprint,
                {"defect_state": changed_defect.to_dict()},
            ),
            "recursive_path": (
                ["record:decision", "record:forged-path"],
                {},
            ),
            "seed_binding_identity": (
                "seed:forged",
                {"seed_binding_identity": "seed:forged"},
            ),
        }
        for field, (replacement, fact_overrides) in mutations.items():
            facts = copy.deepcopy(base_facts)
            summary = facts["confirmed_root_summaries"][0]
            summary[field] = replacement
            if field == "candidate_ref":
                summary["recursive_path"][0] = replacement
            facts.update(fact_overrides)
            with self.subTest(field=field):
                allowed_refs = _factor_role_allowed_refs_from_facts(facts)
                direct_refs = {
                    facts["candidate_ref"],
                    *facts["recursive_path"],
                }
                summary_refs = {
                    summary["candidate_ref"],
                    *summary["evidence_refs"],
                    *summary["recursive_path"],
                }
                for ref in summary_refs - direct_refs:
                    self.assertNotIn(ref, allowed_refs)

    def test_factor_repair_constraints_exclude_invalid_reference_envelopes(self):
        invalid_envelopes = (
            {
                "resolved_ref": "record:three-field",
                "resolution_status": "resolved",
                "provenance_class": "recorded",
            },
            {
                "raw_ref": "record:contradictory",
                "resolved_ref": "record:contradictory",
                "resolution_status": "unresolved",
                "provenance_class": "recorded",
            },
            {
                "raw_ref": "record:inferred-without-metadata",
                "resolved_ref": "record:inferred-without-metadata",
                "resolution_status": "resolved",
                "provenance_class": "inferred",
            },
        )
        request = replace(
            sample_factor_role_request(),
            supporting_evidence=tuple(
                {"reference": envelope} for envelope in invalid_envelopes
            ),
        )
        invalid = valid_factor_role_payload(request)
        invalid["evidence_refs"] = [
            "record:three-field",
            "record:contradictory",
            "record:inferred-without-metadata",
        ]
        transport = ScriptedTransport(
            [json.dumps(invalid), json.dumps(valid_factor_role_payload(request))]
        )
        result = ClaudeCausalJudge(
            transport=transport, cache=JudgmentCache()
        ).judge_factor_role_bounded(request, max_physical_requests=2)

        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        for ref in invalid["evidence_refs"]:
            self.assertNotIn(
                ref,
                repair_payload["repair_constraints"]["allowed_fact_refs"],
            )

    def test_factor_repair_constraints_exclude_non_exact_envelope_types(self):
        base = {
            "raw_ref": "record:strict-envelope",
            "resolved_ref": "record:strict-envelope",
            "resolution_status": "resolved",
            "provenance_class": "recorded",
        }
        invalid_envelopes = (
            {**base, "raw_ref": 1},
            {**base, "resolved_ref": True},
            {**base, "resolution_status": ["resolved"]},
            {**base, "provenance_class": _FactorStringSubclass("recorded")},
            {
                **base,
                "provenance_class": "inferred",
                "inference_metadata": {
                    "evidence_type": "semantic_inferred",
                    "inference_method": 9,
                },
            },
        )
        request = replace(
            sample_factor_role_request(),
            supporting_evidence=tuple(
                {"reference": envelope} for envelope in invalid_envelopes
            ),
        )
        invalid = valid_factor_role_payload(request)
        invalid["evidence_refs"] = ["record:strict-envelope"]
        transport = ScriptedTransport(
            [json.dumps(invalid), json.dumps(valid_factor_role_payload(request))]
        )
        result = ClaudeCausalJudge(
            transport=transport, cache=JudgmentCache()
        ).judge_factor_role_bounded(request, max_physical_requests=2)

        self.assertEqual(transport.request_count, 2)
        repair_payload = json.loads(transport.calls[1]["messages"][0]["content"])
        self.assertEqual(result.physical_requests, 2)
        self.assertNotIn(
            "record:strict-envelope",
            repair_payload["repair_constraints"]["allowed_fact_refs"],
        )

    def test_bounded_factor_exhaustion_raises_with_exact_usage(self):
        invalid = valid_factor_role_payload()
        del invalid["confidence"]
        transport = ScriptedTransport([json.dumps(invalid)])
        with self.assertRaises(BoundedJudgeCallError) as raised:
            ClaudeCausalJudge(
                transport=transport, cache=JudgmentCache()
            ).judge_factor_role_bounded(
                sample_factor_role_request(), max_physical_requests=1
            )

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertIn("request_budget_exhausted", str(raised.exception))
        self.assertEqual(transport.request_count, 1)

    def test_factor_provider_failure_raises_with_exact_usage(self):
        transport = ScriptedTransport([JudgeProviderUnavailable("timed out")])
        with self.assertRaises(BoundedJudgeCallError) as raised:
            ClaudeCausalJudge(
                transport=transport, cache=JudgmentCache()
            ).judge_factor_role_bounded(
                sample_factor_role_request(), max_physical_requests=1
            )

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertIn("provider_error", str(raised.exception))
        self.assertEqual(transport.request_count, 1)


if __name__ == "__main__":
    unittest.main()
