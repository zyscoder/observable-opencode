"""Tests for independent factor-role facts and factual Judge projections."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import trace_attribution.causal_judge as causal_judge_module
import trace_attribution.causal_state as causal_state_module
import trace_attribution.recursive_analyzer as recursive_analyzer_module
from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    BoundedJudgeCapability,
    FactorRoleRequest,
    OfflineJudgeCapability,
    RootConfirmationRequest,
    build_factor_role_prompt,
    build_recursive_confirmation_prompt,
    factor_role_request_fact_refs,
    factor_role_request_identity,
    factor_role_request_projection,
    factor_role_request_projection_identity,
    parse_factor_role_judgment,
    root_confirmation_request_identity,
    root_confirmation_request_projection,
    validate_factor_role_request_projection,
)
from trace_attribution.cache import build_judge_cache_key
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    FactorRoleJudgment,
    FrozenMapping,
    HypothesisEvidence,
    LocalStateOwner,
    RecursiveAttributionReport,
    RootConfirmation,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    semantic_visit_key,
    validate_modern_report_shape,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.evaluation_facts import FailureSignature
from trace_attribution.global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
)
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    GLOBAL_NON_ROOT_REVIEW_ROLES,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)


def _defect_state() -> DefectState:
    return DefectState.create(
        label="Missing required repository check.",
        expected="The repository verifies the required behavior.",
        actual="The required behavior is omitted.",
        mechanism="An incomplete decision was materialized in the change.",
        scope="record:defect",
    )


def _mechanism() -> dict[str, str]:
    return {
        "schema": "factor-role-mechanism/v1",
        "mechanism_type": "enabling_condition",
        "source_ref": "record:prompt",
        "target_ref": "record:decision",
        "effect": "increased_defect_likelihood",
    }


def _counterfactual(*, predicted_effect: str = "reduces_defect_likelihood") -> dict[str, str]:
    return {
        "schema": "factor-role-counterfactual/v1",
        "intervention_ref": "record:prompt",
        "intervention_kind": "replace_with_semantically_correct_behavior",
        "predicted_effect": predicted_effect,
    }


def _confirmed_root_summary(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema": "factor-role-root-evidence-summary/v2",
        "candidate_ref": "record:decision",
        "hypothesis_id": "hypothesis:confirmed-root",
        "hypothesis_semantic_hash": "sha256:confirmed-root",
        "defect_fingerprint": _defect_state().fingerprint,
        "seed_binding_identity": "seed:test",
        "reason": "The decision introduced the omission.",
        "evidence_refs": ["record:root-evidence"],
        "recursive_path": ["record:decision", "record:defect"],
    }
    values.update(overrides)
    if "confirmation_identity" not in overrides:
        values["confirmation_identity"] = confirmation_identity_for(
            hypothesis_id=values["hypothesis_id"],
            hypothesis_semantic_hash=values["hypothesis_semantic_hash"],
            candidate_ref=values["candidate_ref"],
            defect_fingerprint=values["defect_fingerprint"],
            recursive_path=tuple(values["recursive_path"]),
            seed_binding_identity=values["seed_binding_identity"],
        )
    return values


def _reference_envelope(
    ref: str, *, provenance_class: str = "recorded"
) -> dict[str, object]:
    envelope: dict[str, object] = {
        "raw_ref": ref,
        "resolved_ref": ref,
        "resolution_status": "resolved",
        "provenance_class": provenance_class,
    }
    if provenance_class == "inferred":
        envelope["inference_metadata"] = {
            "evidence_type": "semantic_inferred",
            "inference_method": "semantic_similarity_v1",
        }
    return envelope


class _StringSubclass(str):
    pass


def _judgment(**overrides: object) -> FactorRoleJudgment:
    values: dict[str, object] = {
        "candidate_ref": "record:prompt",
        "necessity_status": "not_necessary",
        "factor_role": "contributing_condition",
        "reason": "The ambiguous requirement enabled the downstream omission.",
        "confidence": 0.83,
        "evidence_refs": ("record:prompt",),
        "recursive_path": ("record:prompt", "record:decision", "record:defect"),
        "factor_mechanism": _mechanism(),
        "counterfactual": _counterfactual(),
        "hypothesis_id": "hypothesis:test",
        "hypothesis_semantic_hash": "sha256:test",
        "defect_fingerprint": "sha256:defect",
        "seed_binding_identity": "seed:test",
        "analysis_perspective": "Improve repository reasoning.",
        "request_identity": "factor-role-request:v1:test",
    }
    values.update(overrides)
    return FactorRoleJudgment(**values)


def _request(**overrides: object) -> FactorRoleRequest:
    values: dict[str, object] = {
        "candidate_ref": "record:prompt",
        "defect_state": _defect_state(),
        "recursive_path": ("record:prompt", "record:decision", "record:defect"),
        "candidate_reference": {"ref": "record:prompt", "content": "ambiguous requirement"},
        "recursive_path_references": (
            {"ref": "record:prompt", "content": "ambiguous requirement"},
            {"ref": "record:decision", "content": "omitted requirement"},
            {"ref": "record:defect", "content": "missing check"},
        ),
        "supporting_evidence": ({"ref": "record:prompt", "content": "ambiguous"},),
        "opposing_evidence": ({"ref": "record:policy", "content": "clear policy"},),
        "task_obligations": ({"ref": "record:task", "content": "verify behavior"},),
        "confirmed_root_summaries": (_confirmed_root_summary(),),
        "hypothesis_id": "hypothesis:test",
        "hypothesis_semantic_hash": "sha256:test",
        "seed_binding_identity": "seed:test",
        "analysis_perspective": "Improve repository reasoning.",
    }
    values.update(overrides)
    return FactorRoleRequest(**values)


def _root_request_from_facts(facts: dict[str, object]) -> RootConfirmationRequest:
    return RootConfirmationRequest(
        candidate_ref=facts["candidate_ref"],
        defect_state=DefectState.from_dict(facts["defect_state"]),
        recursive_path=tuple(facts["recursive_path"]),
        candidate_reference=facts["candidate_reference"],
        recursive_path_references=tuple(
            facts["recursive_path_references"]
        ),
        supporting_evidence=tuple(facts["supporting_evidence"]),
        opposing_evidence=tuple(facts["opposing_evidence"]),
        competing_hypotheses=tuple(facts["competing_hypotheses"]),
        task_obligations=tuple(facts["task_obligations"]),
        hypothesis_id=facts["hypothesis_id"],
        hypothesis_semantic_hash=facts["hypothesis_semantic_hash"],
        seed_binding_identity=facts["seed_binding_identity"],
        analysis_perspective=facts["analysis_perspective"],
        factual_context=facts.get("factual_context") or {},
    )


def _factor_request_from_facts(facts: dict[str, object]) -> FactorRoleRequest:
    return FactorRoleRequest(
        candidate_ref=facts["candidate_ref"],
        defect_state=DefectState.from_dict(facts["defect_state"]),
        recursive_path=tuple(facts["recursive_path"]),
        candidate_reference=facts["candidate_reference"],
        recursive_path_references=tuple(
            facts["recursive_path_references"]
        ),
        supporting_evidence=tuple(facts["supporting_evidence"]),
        opposing_evidence=tuple(facts["opposing_evidence"]),
        task_obligations=tuple(facts["task_obligations"]),
        confirmed_root_summaries=tuple(
            facts["confirmed_root_summaries"]
        ),
        hypothesis_id=facts["hypothesis_id"],
        hypothesis_semantic_hash=facts["hypothesis_semantic_hash"],
        seed_binding_identity=facts["seed_binding_identity"],
        analysis_perspective=facts["analysis_perspective"],
        factual_context=facts.get("factual_context") or {},
    )


def _judge_cache_key(stage: str, prompt: str) -> str:
    return build_judge_cache_key(
        stage=stage,
        model="test-model",
        system="test-system",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
        thinking_config=None,
        prompt_schema_version="test-schema/v1",
    )


def _factor_payload(
    *,
    necessity_status: str = "not_necessary",
    factor_role: str = "contributing_condition",
) -> dict[str, object]:
    mechanisms: dict[str, dict[str, str]] = {
        "contributing_condition": _mechanism(),
        "amplifying_factor": {
            **_mechanism(),
            "mechanism_type": "amplification",
            "effect": "increased_defect_severity",
        },
        "downstream_materialization": {
            **_mechanism(),
            "mechanism_type": "downstream_materialization",
            "target_ref": "record:defect",
            "effect": "exposes_already_introduced_defect",
        },
        "unrelated": {},
        "unknown": {},
    }
    effects = {
        "contributing_condition": "reduces_defect_likelihood",
        "amplifying_factor": "reduces_defect_severity",
        "downstream_materialization": "defect_still_present_without_materialization",
        "unrelated": "no_grounded_causal_influence_established",
        "unknown": (
            "prevents_defect"
            if necessity_status == "necessary"
            else "insufficient_grounded_evidence"
        ),
    }
    request = _request()
    return {
        "candidate_ref": request.candidate_ref,
        "necessity_status": necessity_status,
        "factor_role": factor_role,
        "reason": "The candidate changes how the downstream defect materializes.",
        "confidence": 0.81,
        "evidence_refs": ["record:prompt", "record:decision"],
        "recursive_path": list(request.recursive_path),
        "factor_mechanism": mechanisms[factor_role],
        "counterfactual": {
            "schema": "factor-role-counterfactual/v1",
            "intervention_ref": request.candidate_ref,
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_effect": effects[factor_role],
        },
        "hypothesis_id": request.hypothesis_id,
        "hypothesis_semantic_hash": request.hypothesis_semantic_hash,
        "defect_fingerprint": request.defect_state.fingerprint,
        "seed_binding_identity": request.seed_binding_identity,
        "analysis_perspective": request.analysis_perspective,
        "request_identity": factor_role_request_identity(request),
    }


def _lifecycle_trace() -> dict[str, object]:
    return {
        "case_id": "factor-role-lifecycle",
        "records": [
            {
                "record_id": "root",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "The root decision omitted a requirement."},
            },
            {
                "record_id": "factor",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "The factor enabled the omission."},
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:root", "record:factor"],
                "data": {"actual": "The required repository check is absent."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": source},
                "to": {"type": "record", "id": "defect"},
                "relation": "authored_decision_observed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
            for source in ("root", "factor")
        ],
    }


def _structured_lifecycle_trace(symbol: str) -> tuple[dict[str, object], str]:
    trace = _lifecycle_trace()
    signature = FailureSignature(
        exception_family="assertionerror",
        first_business_frame="service/api.py:test_repository_check",
        assertion_contract="The repository API returns the required check.",
        contract_template="the repository api returns <value>",
        observation_values=("missing",),
        relevant_symbol=symbol,
        subsystem="repository_api",
    )
    defect_record = next(
        item
        for item in trace["records"]
        if item["record_id"] == "defect"
    )
    defect_record["data"] = {
        "failure_type": "assertionerror",
        "expected": "The repository API returns the required check.",
        "actual": "The required repository check is absent.",
        "reason": (
            "External evaluation failures share one deterministic factual "
            "failure signature."
        ),
        "attribution_domain": "repository_api",
        "source": "external_feature_benchmark",
        "failure_signature": signature.to_dict(),
    }
    return trace, signature.signature_id


def _serial_lifecycle_trace() -> dict[str, object]:
    trace = _lifecycle_trace()
    trace["records"][-1]["source_refs"] = ["record:factor"]
    trace["dataflow_edges"] = [
        {
            "from": {"type": "record", "id": "root"},
            "to": {"type": "record", "id": "factor"},
            "relation": "authored_decision_observed_by_evaluation",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        },
        {
            "from": {"type": "record", "id": "factor"},
            "to": {"type": "record", "id": "defect"},
            "relation": "authored_decision_observed_by_evaluation",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        },
    ]
    return trace


def _four_role_pool_trace() -> dict[str, object]:
    candidate_ids = (
        "root",
        "condition",
        "amplifier",
        "outcome",
        "unrelated",
    )
    return {
        "case_id": "factor-role-four-role-pool",
        "records": [
            *[
                {
                    "record_id": candidate_id,
                    "component": "agent",
                    "event_type": "decision",
                    "data": {
                        "rationale": "Candidate {0}.".format(candidate_id)
                    },
                }
                for candidate_id in candidate_ids
            ],
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": [
                    "record:{0}".format(candidate_id)
                    for candidate_id in candidate_ids
                ],
                "data": {"actual": "The required behavior is absent."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": candidate_id},
                "to": {"type": "record", "id": "defect"},
                "relation": "authored_decision_observed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
            for candidate_id in candidate_ids
        ],
    }


def _lifecycle_state(
    *,
    trace: dict[str, object] | None = None,
    checkpoint: CheckpointBundle | None = None,
    checkpoint_config: dict[str, object] | None = None,
    max_judge_requests: int = 2,
) -> RecursiveAnalysisState:
    graph = TraceGraph.from_trace(trace or _lifecycle_trace())
    state = RecursiveAnalysisState.create(
        graph=graph,
        start_refs=["record:defect"],
        objective="Find the necessary root and independently classify the factor.",
        analysis_perspective="Improve repository reasoning.",
        max_hypotheses=24,
    )
    AgenticRecursiveAnalyzer(
        judge=_GlobalLifecycleJudge(),
        fusion_mode="retrieval-global",
        max_judge_requests=max_judge_requests,
        checkpoint=checkpoint,
        checkpoint_config=checkpoint_config,
    )._run_global_candidate_prepass(state, graph)
    if [
        (item["candidate_ref"], item["review_scope"])
        for item in state.confirmation_queue
    ] != [
        ("record:root", "root"),
        ("record:factor", "non_root"),
    ]:
        raise AssertionError("lifecycle fixture produced an unexpected review queue")
    return state


def _inject_coordinated_factor_opposition(
    state: RecursiveAnalysisState,
) -> dict[str, object]:
    factor_queue = next(
        item
        for item in state.confirmation_queue
        if item["review_scope"] == "non_root"
    )
    previous_hypothesis_id = str(factor_queue["hypothesis_id"])
    previous = state.ledger.get(previous_hypothesis_id)
    updated = state.ledger._update_with_frontier(
        state.frontier,
        previous_hypothesis_id,
        claim="{0} Coordinated forged opposition.".format(previous.claim),
        opposing_evidence=(
            *previous.opposing_evidence,
            HypothesisEvidence(
                "record:root",
                "Coordinated state claims the root opposes the factor.",
                0.99,
            ),
        ),
    )
    seed_key = state.hypothesis_seed_keys.pop(previous_hypothesis_id)
    state.hypothesis_seed_keys[updated.hypothesis_id] = seed_key
    for hypothesis_ids in (
        state.introduction_hypothesis_ids,
        state.present_hypothesis_ids,
        state.unresolved_hypothesis_ids,
    ):
        if previous_hypothesis_id in hypothesis_ids:
            hypothesis_ids.remove(previous_hypothesis_id)
            hypothesis_ids.add(updated.hypothesis_id)
    for binding in state.introduction_bindings:
        if binding.get("hypothesis_id") != previous_hypothesis_id:
            continue
        binding["hypothesis_id"] = updated.hypothesis_id
        binding["hypothesis_semantic_hash"] = updated.semantic_hash
    state.introduction_binding_keys = {
        (
            str(binding.get("candidate_ref") or ""),
            str(binding.get("defect_fingerprint") or ""),
            str(binding.get("hypothesis_semantic_hash") or ""),
            str(binding.get("seed_binding_identity") or ""),
        )
        for binding in state.introduction_bindings
    }
    for queued in state.confirmation_queue:
        if queued.get("hypothesis_id") != previous_hypothesis_id:
            continue
        queued["hypothesis_id"] = updated.hypothesis_id
        queued["hypothesis_semantic_hash"] = updated.semantic_hash
        queued["owner"] = LocalStateOwner.create(
            seed_binding_identity=updated.seed_binding_identity,
            hypothesis_id=updated.hypothesis_id,
            visit_key=semantic_visit_key(
                updated.candidate_root_ref,
                state.defect_states[updated.active_defect_fingerprint],
                updated.semantic_hash,
                updated.seed_binding_identity,
            ),
            occurrence_key="confirmation_queue",
        ).to_dict()
    state.confirmation_queue_keys = {
        state._confirmation_queue_key(item)
        for item in state.confirmation_queue
    }
    state.refresh_pending_confirmation_request_identities()
    return factor_queue


class _LifecycleJudge(BoundedJudgeCapability):
    def __init__(self, *, fail_factor: bool = False) -> None:
        self.fail_factor = fail_factor
        self.confirmation_requests = []
        self.factor_requests = []
        self.allowances = []

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_requests.append(request)
        self.allowances.append(("root", max_physical_requests))
        confirmation = RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The root decision omitted a requirement.",
            reason="The root decision independently introduced the omission.",
            counterfactual=confirmation_counterfactual_for(
                request.candidate_ref, "confirmed"
            ),
            confidence=0.93,
            evidence_refs=[request.candidate_ref],
        )
        if request.competing_hypotheses:
            confirmation = replace(
                confirmation,
                competitor_comparisons=tuple(
                    {
                        "hypothesis_id": item["hypothesis_id"],
                        "hypothesis_semantic_hash": item[
                            "hypothesis_semantic_hash"
                        ],
                        "candidate_ref": item["candidate_reference"][
                            "resolved_ref"
                        ],
                        "defect_fingerprint": item["active_defect"][
                            "fingerprint"
                        ],
                        "confirmation_identity": item[
                            "confirmation_identity"
                        ],
                        "recursive_path": list(item["recursive_path"]),
                        "requires_independent_confirmation": item[
                            "requires_independent_confirmation"
                        ],
                        "status": "outperformed",
                        "reason": "The root independently outperforms the factor.",
                        "evidence_refs": [
                            item["candidate_reference"]["resolved_ref"]
                        ],
                    }
                    for item in request.competing_hypotheses
                ),
            )
        return BoundedJudgeCallResult(
            confirmation,
            1,
        )

    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        self.factor_requests.append(request)
        self.allowances.append(("factor", max_physical_requests))
        if self.fail_factor:
            raise BoundedJudgeCallError(
                "factor provider failed",
                physical_requests=1,
            )
        return BoundedJudgeCallResult(
            FactorRoleJudgment(
                candidate_ref=request.candidate_ref,
                necessity_status="not_necessary",
                factor_role="contributing_condition",
                reason="The factor enabled the downstream omission.",
                confidence=0.84,
                evidence_refs=(request.candidate_ref,),
                recursive_path=request.recursive_path,
                factor_mechanism={
                    "schema": "factor-role-mechanism/v1",
                    "mechanism_type": "enabling_condition",
                    "source_ref": request.candidate_ref,
                    "target_ref": request.recursive_path[-1],
                    "effect": "increased_defect_likelihood",
                },
                counterfactual={
                    "schema": "factor-role-counterfactual/v1",
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_effect": "reduces_defect_likelihood",
                },
                hypothesis_id=request.hypothesis_id,
                hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                defect_fingerprint=request.defect_state.fingerprint,
                seed_binding_identity=request.seed_binding_identity,
                analysis_perspective=request.analysis_perspective,
                request_identity=factor_role_request_identity(request),
            ),
            1,
        )


class _GlobalLifecycleJudge(_LifecycleJudge, GlobalJudgeCapability):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        assessments = []
        for capsule in request.capsules:
            selected = capsule.candidate_ref == "record:root"
            contributing = capsule.candidate_ref == "record:factor"
            assessments.append(
                GlobalCandidateAssessment(
                    candidate_ref=capsule.candidate_ref,
                    defect_status=(
                        "present" if selected or contributing else "absent"
                    ),
                    input_defect_status=(
                        "absent" if selected else "unknown"
                    ),
                    output_defect_status=(
                        "present" if selected or contributing else "absent"
                    ),
                    causal_path_refs=(
                        tuple(capsule.downstream_path)
                        if selected or contributing
                        else ()
                    ),
                    counterfactual={
                        "intervention_ref": capsule.candidate_ref,
                        "intervention_kind": (
                            "replace_with_semantically_correct_behavior"
                        ),
                        "predicted_defect_status": (
                            "absent" if selected or contributing else "present"
                        ),
                        "causal_effect": (
                            "prevents_defect"
                            if selected or contributing
                            else "does_not_prevent_defect"
                        ),
                    },
                    compared_candidate_refs=(
                        request.open_authored_root_candidate_refs
                    ),
                    causal_role=(
                        "root_candidate"
                        if selected
                        else (
                            "contributing_condition"
                            if contributing
                            else "unrelated"
                        )
                    ),
                    responsibility=(
                        "primary"
                        if selected
                        else ("shared" if contributing else "none")
                    ),
                    candidate_phase=(
                        "implementation"
                        if selected
                        else ("planning" if contributing else "intermediate")
                    ),
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode=(
                        "positive_introduction"
                        if selected
                        else (
                            "omission_enabling_condition"
                            if contributing
                            else "none"
                        )
                    ),
                    obligation_refs=(),
                    contribution_mechanism=(
                        {
                            "type": "scope_narrowing",
                            "target_ref": request.seed_ref,
                            "effect": (
                                "The factor constrained the behavior reaching "
                                "the observed defect."
                            ),
                            "evidence_refs": (capsule.candidate_ref,),
                        }
                        if contributing
                        else None
                    ),
                    reason="Canonical lifecycle Global assessment.",
                    evidence_refs=(capsule.candidate_ref,),
                    confidence=0.9 if selected else 0.8,
                )
            )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="candidate_roots",
                reason="The root introduced the defect and the factor contributed.",
                assessments=tuple(assessments),
                selected_candidate_refs=("record:root",),
                expansion_requests=(),
                decisive_evidence_refs=("record:root",),
                missing_evidence=(),
                confidence=0.9,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": request.active_defect.fingerprint,
                    "active_focus_text_hash": request.active_focus_text_hash,
                },
            ),
            0,
        )


class _OutcomeLifecycleGlobalJudge(_GlobalLifecycleJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        result = super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        assessments = tuple(
            (
                GlobalCandidateAssessment(
                    candidate_ref=assessment.candidate_ref,
                    defect_status="present",
                    input_defect_status="present",
                    output_defect_status="present",
                    causal_path_refs=assessment.causal_path_refs,
                    counterfactual={
                        "intervention_ref": assessment.candidate_ref,
                        "intervention_kind": (
                            "replace_with_semantically_correct_behavior"
                        ),
                        "predicted_defect_status": "present",
                        "causal_effect": "does_not_prevent_defect",
                    },
                    compared_candidate_refs=(
                        assessment.compared_candidate_refs
                    ),
                    causal_role="outcome_evidence",
                    responsibility="none",
                    candidate_phase="intermediate",
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode="none",
                    obligation_refs=(),
                    contribution_mechanism=None,
                    reason=(
                        "The tool error exposed an already introduced "
                        "defect."
                    ),
                    evidence_refs=assessment.evidence_refs,
                    confidence=assessment.confidence,
                )
                if assessment.candidate_ref == "record:factor"
                else assessment
            )
            for assessment in result.value.assessments
        )
        return BoundedJudgeCallResult(
            replace(result.value, assessments=assessments),
            result.physical_requests,
        )


class _PublicationLifecycleJudge(_LifecycleJudge):
    def __init__(
        self,
        factor_role: str,
        *,
        necessity_status: str | None = None,
    ) -> None:
        super().__init__()
        self.factor_role = factor_role
        self.necessity_status = necessity_status

    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        self.factor_requests.append(request)
        self.allowances.append(("factor", max_physical_requests))
        necessity_status = (
            self.necessity_status
            or (
                "unknown"
                if self.factor_role == "unknown"
                else "not_necessary"
            )
        )
        mechanism_types = {
            "contributing_condition": "enabling_condition",
            "amplifying_factor": "amplification",
            "downstream_materialization": "downstream_materialization",
        }
        effects = {
            "contributing_condition": "reduces_defect_likelihood",
            "amplifying_factor": "reduces_defect_severity",
            "downstream_materialization": (
                "prevents_defect"
                if necessity_status == "necessary"
                else "defect_still_present_without_materialization"
            ),
            "unrelated": "no_grounded_causal_influence_established",
            "unknown": (
                "prevents_defect"
                if necessity_status == "necessary"
                else "insufficient_grounded_evidence"
            ),
        }
        mechanism_type = mechanism_types.get(self.factor_role)
        mechanism = (
            {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": mechanism_type,
                "source_ref": request.candidate_ref,
                "target_ref": request.recursive_path[-1],
                "effect": "Grounded publication test mechanism.",
            }
            if mechanism_type is not None
            else {}
        )
        return BoundedJudgeCallResult(
            FactorRoleJudgment(
                candidate_ref=request.candidate_ref,
                necessity_status=necessity_status,
                factor_role=self.factor_role,
                reason="Canonical {0} publication.".format(
                    self.factor_role
                ),
                confidence=(
                    0.0 if necessity_status == "unknown" else 0.84
                ),
                evidence_refs=(request.candidate_ref,),
                recursive_path=request.recursive_path,
                factor_mechanism=mechanism,
                counterfactual={
                    "schema": "factor-role-counterfactual/v1",
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_effect": effects[self.factor_role],
                },
                hypothesis_id=request.hypothesis_id,
                hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                defect_fingerprint=request.defect_state.fingerprint,
                seed_binding_identity=request.seed_binding_identity,
                analysis_perspective=request.analysis_perspective,
                request_identity=factor_role_request_identity(request),
            ),
            1,
        )


class _EscalationLifecycleJudge(_PublicationLifecycleJudge):
    def __init__(self, escalation_status: str) -> None:
        super().__init__(
            "unknown",
            necessity_status="necessary",
        )
        self.escalation_status = escalation_status

    @staticmethod
    def _competitor_comparisons(request, *, status: str):
        return tuple(
            {
                "hypothesis_id": item["hypothesis_id"],
                "hypothesis_semantic_hash": item[
                    "hypothesis_semantic_hash"
                ],
                "candidate_ref": item["candidate_reference"][
                    "resolved_ref"
                ],
                "defect_fingerprint": item["active_defect"][
                    "fingerprint"
                ],
                "confirmation_identity": item["confirmation_identity"],
                "recursive_path": list(item["recursive_path"]),
                "requires_independent_confirmation": item[
                    "requires_independent_confirmation"
                ],
                "status": status,
                "reason": "The independent root comparison is canonical.",
                "evidence_refs": [
                    item["candidate_reference"]["resolved_ref"]
                ],
            }
            for item in request.competing_hypotheses
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_requests.append(request)
        self.allowances.append(("root", max_physical_requests))
        is_escalation = request.candidate_ref == "record:factor"
        if not is_escalation:
            confirmation = replace(
                RootConfirmation.confirmed(
                    request.candidate_ref,
                    excerpt="The root decision omitted a requirement.",
                    reason=(
                        "The root decision independently introduced the "
                        "omission."
                    ),
                    counterfactual=confirmation_counterfactual_for(
                        request.candidate_ref, "confirmed"
                    ),
                    confidence=0.93,
                    evidence_refs=[request.candidate_ref],
                ),
                competitor_comparisons=self._competitor_comparisons(
                    request,
                    status=(
                        "co_root"
                        if self.escalation_status == "confirmed"
                        else "outperformed"
                    ),
                ),
            )
        elif self.escalation_status == "confirmed":
            confirmation = replace(
                RootConfirmation.confirmed(
                    request.candidate_ref,
                    excerpt="The factor enabled the omission.",
                    reason=(
                        "The factor is independently necessary and reciprocal "
                        "with the existing root."
                    ),
                    counterfactual=confirmation_counterfactual_for(
                        request.candidate_ref, "confirmed"
                    ),
                    confidence=0.89,
                    evidence_refs=[request.candidate_ref],
                ),
                competitor_comparisons=self._competitor_comparisons(
                    request,
                    status="co_root",
                ),
            )
        elif self.escalation_status == "rejected":
            confirmation = replace(
                RootConfirmation.rejected(
                    request.candidate_ref,
                    "The root verifier rejected necessity for the factor.",
                    evidence_refs=[request.candidate_ref],
                ),
                competitor_comparisons=self._competitor_comparisons(
                    request,
                    status="unresolved",
                ),
            )
        elif self.escalation_status == "unknown":
            confirmation = replace(
                RootConfirmation.unknown(
                    request.candidate_ref,
                    "The root verifier could not resolve factor necessity.",
                    evidence_refs=[request.candidate_ref],
                ),
                competitor_comparisons=self._competitor_comparisons(
                    request,
                    status="unresolved",
                ),
            )
        else:
            raise AssertionError(
                "unsupported escalation status: {0}".format(
                    self.escalation_status
                )
            )
        return BoundedJudgeCallResult(confirmation, 1)


class _SemanticEscalationLifecycleJudge(_EscalationLifecycleJudge):
    def __init__(self, causal_role: str) -> None:
        super().__init__("confirmed")
        self.causal_role = causal_role

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        result = super().confirm_candidate_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        if request.candidate_ref != "record:factor":
            return BoundedJudgeCallResult(
                replace(
                    result.value,
                    competitor_comparisons=self._competitor_comparisons(
                        request,
                        status="outperformed",
                    ),
                ),
                result.physical_requests,
            )
        semantics = {
            "false_closure": (
                "Closed despite the required repository check remaining absent.",
                "The independently confirmed candidate falsely closed the task "
                "while the functional requirement remained unsatisfied.",
            ),
            "verification_omission": (
                "Skipped verification of the required repository check.",
                "The independently confirmed candidate omitted verification and "
                "left the functional requirement unverified.",
            ),
        }
        excerpt, reason = semantics[self.causal_role]
        return BoundedJudgeCallResult(
            replace(result.value, excerpt=excerpt, reason=reason),
            result.physical_requests,
        )


class _StructuredPhaseEscalationJudge(_SemanticEscalationLifecycleJudge):
    def __init__(self) -> None:
        super().__init__("false_closure")

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        result = _EscalationLifecycleJudge.confirm_candidate_bounded(
            self,
            request,
            max_physical_requests=max_physical_requests,
        )
        if request.candidate_ref != "record:factor":
            return BoundedJudgeCallResult(
                replace(
                    result.value,
                    competitor_comparisons=self._competitor_comparisons(
                        request,
                        status="outperformed",
                    ),
                ),
                result.physical_requests,
            )
        return BoundedJudgeCallResult(
            replace(
                result.value,
                excerpt="The candidate completed its assigned lifecycle.",
                reason=(
                    "The candidate is independently necessary under the "
                    "offered counterfactual."
                ),
            ),
            result.physical_requests,
        )


class _FourRolePoolGlobalJudge(GlobalJudgeCapability):
    _roles = {
        "record:root": "root_candidate",
        "record:condition": "contributing_condition",
        "record:amplifier": "amplifying_factor",
        "record:outcome": "outcome_evidence",
        "record:unrelated": "unrelated",
    }

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        assessments = []
        page_has_root = any(
            self._roles[capsule.candidate_ref] == "root_candidate"
            for capsule in request.capsules
        )
        for capsule in request.capsules:
            role = self._roles[capsule.candidate_ref]
            judged_role = (
                "exculpatory_evidence"
                if not page_has_root and role == "unrelated"
                else role
            )
            selected = role == "root_candidate"
            causal = role in {
                "root_candidate",
                "contributing_condition",
                "amplifying_factor",
            }
            outcome = role == "outcome_evidence"
            assessments.append(
                GlobalCandidateAssessment(
                    candidate_ref=capsule.candidate_ref,
                    defect_status=(
                        "present" if causal or outcome else "absent"
                    ),
                    input_defect_status=(
                        "absent"
                    ),
                    output_defect_status=(
                        "present" if causal or outcome else "absent"
                    ),
                    causal_path_refs=tuple(capsule.downstream_path),
                    counterfactual={
                        "intervention_ref": capsule.candidate_ref,
                        "intervention_kind": (
                            "replace_with_semantically_correct_behavior"
                        ),
                        "predicted_defect_status": (
                            "absent" if selected else "present"
                        ),
                        "causal_effect": (
                            "prevents_defect"
                            if selected
                            else "does_not_prevent_defect"
                        ),
                    },
                    compared_candidate_refs=(
                        request.open_authored_root_candidate_refs
                    ),
                    causal_role=judged_role,
                    responsibility=(
                        "primary"
                        if selected
                        else (
                            "shared"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "none"
                        )
                    ),
                    candidate_phase=(
                        "implementation"
                        if selected
                        else (
                            "planning"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else (
                                "intermediate"
                                if outcome
                                else "intermediate"
                            )
                        )
                    ),
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode=(
                        "positive_introduction"
                        if selected
                        else (
                            "omission_enabling_condition"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "none"
                        )
                    ),
                    obligation_refs=(),
                    contribution_mechanism=(
                        {
                            "type": (
                                "scope_narrowing"
                                if role == "contributing_condition"
                                else "repair_opportunity_consumption"
                            ),
                            "target_ref": request.seed_ref,
                            "effect": (
                                "The factor changed the severity or reach of "
                                "the observed defect."
                            ),
                            "evidence_refs": (capsule.candidate_ref,),
                        }
                        if role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else None
                    ),
                    reason="Grounded four-role pool assessment.",
                    evidence_refs=(capsule.candidate_ref,),
                    confidence={
                        "root_candidate": 0.95,
                        "contributing_condition": 0.71,
                        "amplifying_factor": 0.72,
                        "outcome_evidence": 0.99,
                        "unrelated": 1.0,
                    }[role],
                )
            )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome=(
                    "candidate_roots"
                    if page_has_root
                    else "no_defect"
                ),
                reason="The root is selected after the complete role comparison.",
                assessments=tuple(assessments),
                selected_candidate_refs=(
                    ("record:root",) if page_has_root else ()
                ),
                expansion_requests=(),
                decisive_evidence_refs=(
                    ("record:root",)
                    if page_has_root
                    else (request.capsules[0].candidate_ref,)
                ),
                missing_evidence=(),
                confidence=0.95,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": request.active_defect.fingerprint,
                    "active_focus_text_hash": request.active_focus_text_hash,
                },
            ),
            0,
        )


def _lifecycle_checkpoint_config(
    trace: dict[str, object] | None = None,
    *,
    max_judge_requests: int = 2,
) -> dict[str, object]:
    trace = trace or _lifecycle_trace()
    return build_checkpoint_config(
        trace=trace,
        case_id=str(trace["case_id"]),
        objective="Find the necessary root and independently classify the factor.",
        analysis_perspective="Improve repository reasoning.",
        start_refs=["record:defect"],
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": max_judge_requests,
        },
        model_identity="bounded:factor-role-lifecycle",
        cache_identity="cache:factor-role-lifecycle",
        runtime_identity={
            "judge_timeout_sec": 60.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://factor-role-lifecycle",
            "provider_error_threshold": 3,
            "fusion_mode": "retrieval-global",
        },
    )


class _ExplodingLifecycleJudge(BoundedJudgeCapability):
    def __init__(self) -> None:
        self.calls = 0

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.calls += 1
        raise AssertionError("completed root replay called the provider")

    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        self.calls += 1
        raise AssertionError("completed factor replay called the provider")


class _OverBudgetResultLifecycleJudge(_LifecycleJudge):
    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        result = super().judge_factor_role_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        return BoundedJudgeCallResult(
            result.value,
            max_physical_requests + 1,
        )


class _OverBudgetErrorLifecycleJudge(_LifecycleJudge):
    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        self.factor_requests.append(request)
        raise BoundedJudgeCallError(
            "malicious factor provider exceeded allowance",
            physical_requests=max_physical_requests + 1,
        )


class _InvalidBindingLifecycleJudge(_LifecycleJudge):
    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        result = super().judge_factor_role_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        return BoundedJudgeCallResult(
            replace(
                result.value,
                request_identity="factor-role-request:v1:foreign",
            ),
            1,
        )


class _OutOfClosureLifecycleJudge(_LifecycleJudge):
    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        result = super().judge_factor_role_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        return BoundedJudgeCallResult(
            replace(
                result.value,
                evidence_refs=("record:foreign",),
                factor_mechanism={
                    **dict(result.value.factor_mechanism),
                    "source_ref": "record:foreign",
                },
            ),
            1,
        )


class _SelfTargetLifecycleJudge(_LifecycleJudge):
    def judge_factor_role_bounded(self, request, *, max_physical_requests):
        result = super().judge_factor_role_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        return BoundedJudgeCallResult(
            replace(
                result.value,
                factor_mechanism={
                    **dict(result.value.factor_mechanism),
                    "target_ref": request.candidate_ref,
                },
            ),
            1,
        )


def _persist_lifecycle_checkpoint(
    checkpoint_root: Path,
    *,
    trace: dict[str, object] | None = None,
    fail_factor: bool = False,
    snapshot_terminal: bool = True,
) -> tuple[RecursiveAnalysisState, object]:
    trace = trace or _lifecycle_trace()
    config = _lifecycle_checkpoint_config(trace)
    bundle = CheckpointBundle(checkpoint_root)
    bundle.initialize(config)
    state = _lifecycle_state(
        trace=trace,
        checkpoint=bundle,
        checkpoint_config=config,
    )
    analyzer = AgenticRecursiveAnalyzer(
        judge=_LifecycleJudge(fail_factor=fail_factor),
        fusion_mode="retrieval-global",
        max_judge_requests=2,
        checkpoint=bundle,
        checkpoint_config=config,
    )
    analyzer._confirm_queued_roots(state)
    if snapshot_terminal:
        analyzer._checkpoint_state(state, "test:factor-role-terminal")
    bundle.flush_all()
    return state, bundle.restore(expected_config=config)


def _persist_necessary_escalation_checkpoint(
    checkpoint_root: Path,
) -> tuple[RecursiveAnalysisState, object, dict[str, object]]:
    trace = _lifecycle_trace()
    config = _lifecycle_checkpoint_config(
        trace,
        max_judge_requests=3,
    )
    bundle = CheckpointBundle(checkpoint_root)
    bundle.initialize(config)
    state = _lifecycle_state(
        trace=trace,
        checkpoint=bundle,
        checkpoint_config=config,
        max_judge_requests=3,
    )
    analyzer = AgenticRecursiveAnalyzer(
        judge=_EscalationLifecycleJudge("rejected"),
        fusion_mode="retrieval-global",
        max_judge_requests=3,
        checkpoint=bundle,
        checkpoint_config=config,
    )
    analyzer._confirm_queued_roots(state)
    bundle.flush_all()
    return state, bundle.restore(expected_config=config), config


def _with_mixed_stale_seed(checkpoint):
    actions = copy.deepcopy(list(checkpoint.actions))
    snapshot = next(
        item
        for item in reversed(actions)
        if item["operation"] == "state_snapshot"
    )
    stale_defect = DefectState.create(
        label="Stale repository-generation defect.",
        expected="Only the active repository generation is authoritative.",
        actual="A superseded generation recorded an older defect.",
        mechanism="The older repository generation is no longer active.",
        scope="record:stale_defect",
    )
    stale_builder = recursive_analyzer_module.SeedAttributionBuilder(
        start_ref="record:stale_defect",
        defect_state=stale_defect,
    )
    snapshot["payload"]["start_refs"].append("record:stale_defect")
    snapshot["payload"]["seed_ledger"].append(stale_builder.to_dict())
    snapshot["payload"]["seed_count"] += 1

    active_trace = copy.deepcopy(_lifecycle_trace())
    active_trace["records"].extend(
        [
            {
                "record_id": "stale_defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "data": {
                    "repository_revision": 0,
                    "actual": "A superseded defect.",
                },
            },
            {
                "record_id": "generation_one",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 1,
                    "is_final_for_case": True,
                    "claim": "Generation one result.",
                },
            },
        ]
    )
    return (
        replace(checkpoint, actions=tuple(actions)),
        active_trace,
        stale_builder.key,
    )


def _refresh_provider_state_identity(provider_state):
    unsigned = {
        key: copy.deepcopy(value)
        for key, value in provider_state.items()
        if key != "identity"
    }
    provider_state["identity"] = hashlib.sha256(
        stable_json(unsigned).encode("utf-8")
    ).hexdigest()


def _stale_factor_owner(projection, stale_seed_key):
    owner = LocalStateOwner.from_dict(projection["owner"])
    return LocalStateOwner.create(
        seed_binding_identity=stale_seed_key,
        hypothesis_id=owner.hypothesis_id,
        visit_key=owner.visit_key,
        occurrence_key="r5_stale_factor_surface_probe",
    ).to_dict()


def _inject_malformed_stale_factor_surface(
    container,
    *,
    surface,
    stale_seed_key,
):
    values = container[surface]
    forged = copy.deepcopy(values[0])
    if surface in {
        "factor_role_journal",
        "factor_role_action_projection",
        "factor_role_action_projections",
        "factor_role_gaps",
    }:
        projection = (
            forged
            if surface != "factor_role_gaps"
            else container[
                "factor_role_action_projection"
                if "factor_role_action_projection" in container
                else "factor_role_action_projections"
            ][0]
        )
        forged["owner"] = _stale_factor_owner(
            projection,
            stale_seed_key,
        )
    elif surface == "factor_role_judgments":
        forged["seed_binding_identity"] = stale_seed_key
    else:
        raise AssertionError("unsupported Factor surface probe")
    values.append(forged)


def _terminal_projection_probe(
    *,
    operation: str,
    physical_requests_reserved: int,
    physical_request_delta: int,
    physical_request_exact: bool,
    failure_classification: str,
    unknown_judgment: bool,
) -> dict[str, object]:
    lifecycle = _lifecycle_state()
    factual_context = copy.deepcopy(
        next(
            item
            for item in lifecycle.confirmation_queue
            if item["review_scope"] == "non_root"
        )["factual_request_projection"]["facts"]["factual_context"]
    )
    base_request = _request()
    factual_context["failure_signature"] = (
        causal_state_module.active_failure_signature_for(
            base_request.defect_state,
            seed_ref=base_request.recursive_path[-1],
        )
    )
    request = _request(factual_context=factual_context)
    payload = _factor_payload()
    payload["request_identity"] = factor_role_request_identity(request)
    if unknown_judgment:
        payload.update(
            {
                "necessity_status": "unknown",
                "factor_role": "unknown",
                "factor_mechanism": {},
                "counterfactual": {
                    **payload["counterfactual"],
                    "predicted_effect": "insufficient_grounded_evidence",
                },
            }
        )
    judgment = parse_factor_role_judgment(payload, request=request)
    request_identity = factor_role_request_identity(request)
    owner = LocalStateOwner.create(
        seed_binding_identity=request.seed_binding_identity,
        hypothesis_id=request.hypothesis_id,
        visit_key=semantic_visit_key(
            request.candidate_ref,
            request.defect_state,
            request.hypothesis_semantic_hash,
            request.seed_binding_identity,
        ),
        occurrence_key="confirmation_queue",
    )
    return recursive_analyzer_module._factor_role_terminal_projection(
        operation=operation,
        semantic_key="factor_role:{0}".format(request_identity),
        owner=owner.to_dict(),
        origin="global_candidate_factor_assessment",
        request_projection=factor_role_request_projection(request),
        request_identity=request_identity,
        physical_requests_reserved=physical_requests_reserved,
        physical_request_delta=physical_request_delta,
        physical_request_exact=physical_request_exact,
        judgment=judgment,
        failure_classification=failure_classification,
        active_role_binding=(
            causal_state_module.ActiveFailureRoleBinding.create(
                candidate_ref=request.candidate_ref,
                seed_ref=request.recursive_path[-1],
                failure_signature=request.defect_state.fingerprint,
                failure_kind="functional",
                causal_role=causal_state_module.active_failure_factor_role_for(
                    judgment.factor_role
                ),
                disposition="factor",
            ).to_dict()
            if causal_state_module.active_failure_factor_role_for(
                judgment.factor_role
            )
            is not None
            else None
        ),
        queue_binding={
            "seed_key": request.seed_binding_identity,
            "requested_by_ref": request.candidate_ref,
            "recursive_path": list(request.recursive_path),
            "checked_evidence_refs": [
                str(item.get("resolved_ref") or item.get("ref") or "")
                for item in request.supporting_evidence
            ],
            "task_obligations": [
                dict(item) for item in request.task_obligations
            ],
            "artifact_evidence_envelopes": [],
        },
    )


def _publication_report(
    factor_role: str,
    *,
    necessity_status: str | None = None,
) -> tuple[RecursiveAttributionReport, RecursiveAnalysisState]:
    state = _lifecycle_state()
    judge = _PublicationLifecycleJudge(
        factor_role,
        necessity_status=necessity_status,
    )
    analyzer = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
        max_judge_requests=(
            3 if necessity_status == "necessary" else 2
        ),
    )
    analyzer._confirm_queued_roots(state)
    return (
        state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        ),
        state,
    )


def _escalation_report(
    escalation_status: str,
) -> tuple[
    RecursiveAttributionReport,
    RecursiveAnalysisState,
    _EscalationLifecycleJudge,
]:
    state = _lifecycle_state()
    judge = _EscalationLifecycleJudge(escalation_status)
    analyzer = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
        max_judge_requests=3,
    )
    analyzer._confirm_queued_roots(state)
    return (
        state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        ),
        state,
        judge,
    )


def _rewrite_escalation_origin(
    payload: dict[str, object],
    origin: dict[str, str],
) -> None:
    metadata = payload["metadata"]
    for queued in metadata["confirmation_queue"]:
        if isinstance(queued.get("origin"), dict):
            queued["origin"] = copy.deepcopy(origin)
    for queue_key in metadata["confirmation_queue_keys"]:
        if len(queue_key) == 5:
            queue_key[4] = origin["factor_action_identity"]
    for journal in metadata["confirmation_journal"]:
        if isinstance(journal.get("origin"), dict):
            journal["origin"] = copy.deepcopy(origin)
    for action in metadata["confirmation_action_projection"]:
        if isinstance(action.get("origin"), dict):
            action["origin"] = copy.deepcopy(origin)
    for gap in metadata["factor_role_escalation_gaps"]:
        gap["origin"] = copy.deepcopy(origin)


def _rewrite_escalation_owner(
    payload: dict[str, object],
) -> dict[str, str]:
    metadata = payload["metadata"]
    escalation_queue = next(
        item
        for item in metadata["confirmation_queue"]
        if isinstance(item.get("origin"), dict)
    )
    owner = LocalStateOwner.from_dict(escalation_queue["owner"])
    foreign_owner = LocalStateOwner.create(
        seed_binding_identity=owner.seed_binding_identity,
        hypothesis_id=owner.hypothesis_id,
        visit_key="visit:foreign-factor-occurrence",
        occurrence_key="factor_role_escalation:foreign",
    ).to_dict()
    escalation_queue["owner"] = copy.deepcopy(foreign_owner)
    for journal in metadata["confirmation_journal"]:
        if isinstance(journal.get("origin"), dict):
            journal["owner"] = copy.deepcopy(foreign_owner)
    for action in metadata["confirmation_action_projection"]:
        if isinstance(action.get("origin"), dict):
            action["owner"] = copy.deepcopy(foreign_owner)
    for gap in metadata["factor_role_escalation_gaps"]:
        gap["owner"] = copy.deepcopy(foreign_owner)
    return foreign_owner


def _persist_publication_report(
    checkpoint_root: Path,
    *,
    factor_role: str,
) -> tuple[RecursiveAttributionReport, object, TraceGraph]:
    trace = _lifecycle_trace()
    config = _lifecycle_checkpoint_config(trace)
    bundle = CheckpointBundle(checkpoint_root)
    bundle.initialize(config)
    state = _lifecycle_state(
        trace=trace,
        checkpoint=bundle,
        checkpoint_config=config,
    )
    judge = _PublicationLifecycleJudge(factor_role)
    analyzer = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
        max_judge_requests=2,
        checkpoint=bundle,
        checkpoint_config=config,
    )
    analyzer._confirm_queued_roots(state)
    analyzer._checkpoint_state(state, "test:factor-publication-terminal")
    bundle.flush_all()
    return (
        state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        ),
        bundle.restore(expected_config=config),
        state.graph,
    )


def _bind_action_to_independent_factor_role(
    action: dict[str, object],
    judgment: FactorRoleJudgment,
) -> None:
    causal_role = causal_state_module.active_failure_factor_role_for(
        judgment.factor_role
    )
    if causal_role is None:
        action["active_role_binding"] = None
        return
    facts = action["request_projection"]["facts"]
    context = facts.get("factual_context") or {}
    signature = context.get("failure_signature") or {}
    action["active_role_binding"] = (
        causal_state_module.ActiveFailureRoleBinding.create(
            candidate_ref=judgment.candidate_ref,
            seed_ref=judgment.recursive_path[-1],
            failure_signature=str(
                signature.get("signature_id")
                or judgment.defect_fingerprint
            ),
            defect_fingerprint=judgment.defect_fingerprint,
            failure_identity_source=str(
                signature.get("identity_source")
                or "legacy_defect_state"
            ),
            failure_kind=str(signature.get("kind") or "functional"),
            causal_role=causal_role,
            disposition="factor",
        ).to_dict()
    )


def _rewrite_all_active_failure_signatures(
    value: object,
    *,
    forged_signature: str,
) -> tuple[int, int]:
    counts = {"factor": 0, "root": 0}

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if item.get("schema") == "active-failure-role-binding/v2":
                disposition = str(item.get("disposition") or "")
                item["failure_signature"] = forged_signature
                item["failure_identity_source"] = (
                    "structured_failure_signature"
                )
                if disposition == "root":
                    item["counterfactual_prevention_signatures"] = [
                        forged_signature
                    ]
                counts[disposition] += 1
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return counts["factor"], counts["root"]


def _rewrite_all_active_failure_roles(
    value: object,
    *,
    failure_kind: str,
    causal_role: str,
) -> int:
    count = 0

    def visit(item: object) -> None:
        nonlocal count
        if isinstance(item, dict):
            if item.get("causal_role") == "defect_introduction_root":
                item["causal_role"] = causal_role
            if item.get("schema") == "active-failure-role-binding/v2":
                item["failure_kind"] = failure_kind
                item["causal_role"] = causal_role
                count += 1
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return count


class _ContextlessFunctionalJudge(OfflineJudgeCapability):
    def judge_step_offline(self, request):
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason=(
                "The decision contains the functional regression."
            ),
            predecessors=(),
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context[
                        "active_hypothesis_id"
                    ],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently confirm the functional root.",
            },
            confidence=0.95,
        )

    def confirm_candidate_offline(self, request):
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The decision introduced a functional regression.",
            reason="The only candidate introduced the functional defect.",
            counterfactual=confirmation_counterfactual_for(
                request.candidate_ref,
                "confirmed",
            ),
            confidence=0.95,
            evidence_refs=[request.candidate_ref],
        )


def _contextless_functional_publication(
    checkpoint_root: Path,
) -> tuple[RecursiveAttributionReport, object, TraceGraph]:
    trace = {
        "case_id": "contextless-functional-active-failure",
        "records": [
            {
                "record_id": "decision",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "failure_type": "functional_regression",
                    "expected": "The implementation preserves the behavior.",
                    "actual": "The decision introduced a functional regression.",
                    "mechanism": "A behavioral defect was introduced.",
                    "scope": "implementation_behavior",
                },
            }
        ],
    }
    config = build_checkpoint_config(
        trace=trace,
        case_id=str(trace["case_id"]),
        objective="Find the functional regression root.",
        analysis_perspective="Improve repository reasoning.",
        start_refs=["record:decision"],
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        model_identity="offline:contextless-functional",
        cache_identity="cache:contextless-functional",
        runtime_identity={
            "judge_timeout_sec": 60.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://contextless-functional",
            "provider_error_threshold": 3,
            "fusion_mode": "off",
        },
    )
    graph = TraceGraph.from_trace(trace)
    bundle = CheckpointBundle(checkpoint_root)
    report = AgenticRecursiveAnalyzer(
        judge=_ContextlessFunctionalJudge(),
        checkpoint=bundle,
        checkpoint_config=config,
    ).analyze(
        graph,
        start_refs=["record:decision"],
        objective="Find the functional regression root.",
        analysis_perspective="Improve repository reasoning.",
    )
    return report, bundle.restore(expected_config=config), graph


def _rewrite_completed_factor_role(
    payload: dict[str, object],
    *,
    factor_role: str,
    evidence_refs: tuple[str, ...] | None = None,
) -> None:
    metadata = payload["metadata"]
    action = metadata["factor_role_action_projections"][0]
    original = FactorRoleJudgment.from_dict(action["judgment"])
    mechanism_types = {
        "contributing_condition": "enabling_condition",
        "amplifying_factor": "amplification",
        "downstream_materialization": "downstream_materialization",
    }
    effects = {
        "contributing_condition": "reduces_defect_likelihood",
        "amplifying_factor": "reduces_defect_severity",
        "downstream_materialization": (
            "defect_still_present_without_materialization"
        ),
        "unrelated": "no_grounded_causal_influence_established",
    }
    mechanism_type = mechanism_types.get(factor_role)
    mechanism = (
        {
            "schema": "factor-role-mechanism/v1",
            "mechanism_type": mechanism_type,
            "source_ref": original.candidate_ref,
            "target_ref": original.recursive_path[-1],
            "effect": "Coordinated rewritten mechanism.",
        }
        if mechanism_type is not None
        else {}
    )
    rewritten = FactorRoleJudgment(
        candidate_ref=original.candidate_ref,
        necessity_status="not_necessary",
        factor_role=factor_role,
        reason="Coordinated rewritten {0}.".format(factor_role),
        confidence=original.confidence,
        evidence_refs=evidence_refs or original.evidence_refs,
        recursive_path=original.recursive_path,
        factor_mechanism=mechanism,
        counterfactual={
            **dict(original.counterfactual),
            "predicted_effect": effects[factor_role],
        },
        hypothesis_id=original.hypothesis_id,
        hypothesis_semantic_hash=original.hypothesis_semantic_hash,
        defect_fingerprint=original.defect_fingerprint,
        seed_binding_identity=original.seed_binding_identity,
        analysis_perspective=original.analysis_perspective,
        request_identity=original.request_identity,
    )
    action["judgment"] = rewritten.to_dict()
    action["judgment_identity"] = rewritten.judgment_identity
    _bind_action_to_independent_factor_role(action, rewritten)
    metadata["factor_role_judgments"] = [rewritten.to_dict()]
    metadata["factor_role_journal"] = [
        {**copy.deepcopy(action), "status": "completed"}
    ]
    queue = next(
        item
        for item in metadata["confirmation_queue"]
        if item["review_scope"] == "non_root"
    )
    queue["factor_role_judgment"] = rewritten.to_dict()
    queue["response_identity"] = rewritten.judgment_identity
    for key in (
        "contributing_conditions",
        "amplifying_factors",
        "downstream_materializations",
        "rejected_candidates",
    ):
        payload[key] = []
    metadata["factor_confirmation_gaps"] = []
    if evidence_refs is not None:
        return
    publication = causal_state_module.canonical_factor_role_publication(
        judgment=rewritten,
        request_projection=action["request_projection"],
        action_projection=action,
    )
    collection = {
        "contributing_condition": "contributing_conditions",
        "amplifying_factor": "amplifying_factors",
        "downstream_materialization": "downstream_materializations",
        "unrelated": "rejected_candidates",
    }[factor_role]
    payload[collection] = [publication.to_dict()]


def _resign_factor_request_projection(
    payload: dict[str, object],
    *,
    mutate,
) -> None:
    metadata = payload["metadata"]
    action = metadata["factor_role_action_projections"][0]
    request_projection = action["request_projection"]
    mutate(request_projection["facts"])
    request_identity = "factor-role-request:v1:{0}".format(
        hashlib.sha256(
            stable_json(request_projection).encode("utf-8")
        ).hexdigest()
    )
    original = FactorRoleJudgment.from_dict(action["judgment"])
    rewritten = replace(
        original,
        request_identity=request_identity,
    )
    action["semantic_key"] = "factor_role:{0}".format(request_identity)
    action["request_identity"] = request_identity
    action["judgment"] = rewritten.to_dict()
    action["judgment_identity"] = rewritten.judgment_identity
    metadata["factor_role_judgments"] = [rewritten.to_dict()]
    metadata["factor_role_journal"] = [
        {**copy.deepcopy(action), "status": "completed"}
    ]
    queue = next(
        item
        for item in metadata["confirmation_queue"]
        if item["review_scope"] == "non_root"
    )
    queue["semantic_identity"] = request_identity
    queue["factual_request_projection"] = copy.deepcopy(
        request_projection
    )
    queue["factor_role_judgment"] = rewritten.to_dict()
    queue["response_identity"] = rewritten.judgment_identity
    publication = payload["contributing_conditions"][0]
    publication["confirmation"] = rewritten.to_dict()
    publication["provenance"].update(
        {
            "request_identity": request_identity,
            "response_identity": rewritten.judgment_identity,
            "judgment_identity": rewritten.judgment_identity,
            "action_semantic_key": action["semantic_key"],
        }
    )


def _resign_factor_mechanism_source(
    payload: dict[str, object],
    *,
    source_ref: str,
) -> None:
    metadata = payload["metadata"]
    action = metadata["factor_role_action_projections"][0]
    original = FactorRoleJudgment.from_dict(action["judgment"])
    rewritten = replace(
        original,
        factor_mechanism={
            **dict(original.factor_mechanism),
            "source_ref": source_ref,
        },
    )
    action["judgment"] = rewritten.to_dict()
    action["judgment_identity"] = rewritten.judgment_identity
    metadata["factor_role_judgments"] = [rewritten.to_dict()]
    metadata["factor_role_journal"] = [
        {**copy.deepcopy(action), "status": "completed"}
    ]
    queue = next(
        item
        for item in metadata["confirmation_queue"]
        if item["review_scope"] == "non_root"
    )
    queue["factor_role_judgment"] = rewritten.to_dict()
    queue["response_identity"] = rewritten.judgment_identity
    publication = payload["contributing_conditions"][0]
    publication["confirmation"] = rewritten.to_dict()
    publication["mechanism"] = dict(rewritten.factor_mechanism)
    publication["provenance"].update(
        {
            "response_identity": rewritten.judgment_identity,
            "judgment_identity": rewritten.judgment_identity,
        }
    )


def _inject_duplicate_factor_lifecycle(
    payload: dict[str, object],
) -> None:
    metadata = payload["metadata"]
    original_action = metadata["factor_role_action_projections"][0]
    original_judgment = FactorRoleJudgment.from_dict(
        original_action["judgment"]
    )
    original_hypothesis = next(
        item
        for item in payload["hypotheses"]
        if item["hypothesis_id"] == original_judgment.hypothesis_id
    )
    duplicate_hypothesis = (
        causal_state_module.AttributionHypothesis.from_dict(
            original_hypothesis
        ).with_updates(
            claim=(
                "Independently classify a second role lifecycle for the "
                "same candidate."
            )
        )
    )
    payload["hypotheses"].append(duplicate_hypothesis.to_dict())

    duplicate_projection = copy.deepcopy(
        original_action["request_projection"]
    )
    duplicate_projection["facts"]["hypothesis_id"] = (
        duplicate_hypothesis.hypothesis_id
    )
    duplicate_projection["facts"]["hypothesis_semantic_hash"] = (
        duplicate_hypothesis.semantic_hash
    )
    duplicate_request_identity = (
        factor_role_request_projection_identity(duplicate_projection)
    )
    duplicate_owner = LocalStateOwner.create(
        seed_binding_identity=original_judgment.seed_binding_identity,
        hypothesis_id=duplicate_hypothesis.hypothesis_id,
        visit_key=semantic_visit_key(
            original_judgment.candidate_ref,
            DefectState.from_dict(
                duplicate_projection["facts"]["defect_state"]
            ),
            duplicate_hypothesis.semantic_hash,
            original_judgment.seed_binding_identity,
        ),
        occurrence_key="confirmation_queue",
    ).to_dict()
    duplicate_judgment = FactorRoleJudgment(
        candidate_ref=original_judgment.candidate_ref,
        necessity_status="not_necessary",
        factor_role="downstream_materialization",
        reason=(
            "A second fully signed lifecycle classifies the same candidate "
            "as a materialization."
        ),
        confidence=original_judgment.confidence,
        evidence_refs=original_judgment.evidence_refs,
        recursive_path=original_judgment.recursive_path,
        factor_mechanism={
            "schema": "factor-role-mechanism/v1",
            "mechanism_type": "downstream_materialization",
            "source_ref": original_judgment.candidate_ref,
            "target_ref": original_judgment.recursive_path[-1],
            "effect": "exposes_already_introduced_defect",
        },
        counterfactual={
            "schema": "factor-role-counterfactual/v1",
            "intervention_ref": original_judgment.candidate_ref,
            "intervention_kind": (
                "replace_with_semantically_correct_behavior"
            ),
            "predicted_effect": (
                "defect_still_present_without_materialization"
            ),
        },
        hypothesis_id=duplicate_hypothesis.hypothesis_id,
        hypothesis_semantic_hash=duplicate_hypothesis.semantic_hash,
        defect_fingerprint=original_judgment.defect_fingerprint,
        seed_binding_identity=original_judgment.seed_binding_identity,
        analysis_perspective=original_judgment.analysis_perspective,
        request_identity=duplicate_request_identity,
    )
    duplicate_action = copy.deepcopy(original_action)
    duplicate_action.update(
        {
            "semantic_key": "factor_role:{0}".format(
                duplicate_request_identity
            ),
            "owner": duplicate_owner,
            "hypothesis_id": duplicate_hypothesis.hypothesis_id,
            "request_projection": duplicate_projection,
            "request_identity": duplicate_request_identity,
            "judgment": duplicate_judgment.to_dict(),
            "judgment_identity": (
                duplicate_judgment.judgment_identity
            ),
        }
    )
    _bind_action_to_independent_factor_role(
        duplicate_action, duplicate_judgment
    )
    metadata["factor_role_action_projections"].append(
        duplicate_action
    )
    metadata["factor_role_judgments"].append(
        duplicate_judgment.to_dict()
    )
    metadata["factor_role_journal"].append(
        {**copy.deepcopy(duplicate_action), "status": "completed"}
    )

    original_queue = next(
        item
        for item in metadata["confirmation_queue"]
        if item["review_scope"] == "non_root"
    )
    duplicate_queue = copy.deepcopy(original_queue)
    duplicate_queue.update(
        {
            "hypothesis_id": duplicate_hypothesis.hypothesis_id,
            "hypothesis_semantic_hash": (
                duplicate_hypothesis.semantic_hash
            ),
            "semantic_identity": duplicate_request_identity,
            "owner": duplicate_owner,
            "factual_request_projection": copy.deepcopy(
                duplicate_projection
            ),
            "factor_role_judgment": (
                duplicate_judgment.to_dict()
            ),
            "response_identity": (
                duplicate_judgment.judgment_identity
            ),
        }
    )
    metadata["confirmation_queue"].append(duplicate_queue)
    metadata["confirmation_queue_keys"].append(
        [
            duplicate_hypothesis.hypothesis_id,
            duplicate_judgment.candidate_ref,
            duplicate_judgment.defect_fingerprint,
            duplicate_judgment.seed_binding_identity,
        ]
    )
    original_binding = next(
        item
        for item in metadata["introduction_bindings"]
        if item["hypothesis_id"] == original_judgment.hypothesis_id
    )
    metadata["introduction_bindings"].append(
        {
            **copy.deepcopy(original_binding),
            "hypothesis_id": duplicate_hypothesis.hypothesis_id,
            "hypothesis_semantic_hash": (
                duplicate_hypothesis.semantic_hash
            ),
        }
    )
    publication = causal_state_module.canonical_factor_role_publication(
        judgment=duplicate_judgment,
        request_projection=duplicate_projection,
        action_projection=duplicate_action,
    )
    payload["downstream_materializations"].append(
        publication.to_dict()
    )


class FactorRoleJudgmentTest(unittest.TestCase):
    def test_active_failure_role_contract_contains_exactly_six_roles(self) -> None:
        self.assertEqual(
            getattr(
                causal_state_module,
                "ACTIVE_FAILURE_CAUSAL_ROLES",
                frozenset(),
            ),
            frozenset(
                {
                    "defect_introduction_root",
                    "verification_omission",
                    "false_closure",
                    "downstream_materialization",
                    "amplifying_condition",
                    "external_interruption",
                }
            ),
        )

    def test_factor_role_contract_is_deeply_immutable(self) -> None:
        contract = getattr(
            causal_state_module, "FACTOR_ROLE_CONTRACT", None
        )
        self.assertIsInstance(contract, tuple)
        self.assertTrue(
            all(isinstance(row, FrozenMapping) for row in contract)
        )
        with self.assertRaises(TypeError):
            contract[0]["factor_role"] = "unrelated"
        with self.assertRaises(TypeError):
            contract[0]["predicted_effects"][0] = "changed"

    def test_necessity_and_role_are_strictly_decoupled(self) -> None:
        self.assertEqual(_judgment().factor_role, "contributing_condition")
        invalid = (
            {"necessity_status": "necessary", "factor_role": "contributing_condition"},
            {"necessity_status": "not_necessary", "factor_role": "unknown"},
            {"necessity_status": "unknown", "factor_role": "amplifying_factor"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                _judgment(**overrides)

    def test_conditions_and_amplifiers_require_a_mechanism(self) -> None:
        for role in ("contributing_condition", "amplifying_factor"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                _judgment(factor_role=role, factor_mechanism={})

    def test_materialization_allows_only_its_exact_counterfactual_effect(self) -> None:
        values = {
            "factor_role": "downstream_materialization",
            "factor_mechanism": {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": "downstream_materialization",
                "source_ref": "record:prompt",
                "target_ref": "record:defect",
                "effect": "exposes_already_introduced_defect",
            },
            "counterfactual": _counterfactual(
                predicted_effect="defect_still_present_without_materialization"
            ),
        }
        self.assertEqual(_judgment(**values).factor_role, "downstream_materialization")
        with self.assertRaises(ValueError):
            _judgment(
                **{
                    **values,
                    "counterfactual": _counterfactual(
                        predicted_effect="reduces_defect_likelihood"
                    ),
                }
            )

    def test_necessary_materialization_is_a_valid_independent_pair(self) -> None:
        judgment = _judgment(
            necessity_status="necessary",
            factor_role="downstream_materialization",
            factor_mechanism={
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": "downstream_materialization",
                "source_ref": "record:prompt",
                "target_ref": "record:defect",
                "effect": "materializes_an_upstream_defect",
            },
            counterfactual=_counterfactual(
                predicted_effect="prevents_defect"
            ),
        )

        self.assertEqual(judgment.necessity_status, "necessary")
        self.assertEqual(
            judgment.factor_role,
            "downstream_materialization",
        )

    def test_unrelated_carries_no_mechanism_or_causal_effect_claim(self) -> None:
        values = {
            "factor_role": "unrelated",
            "factor_mechanism": {},
            "counterfactual": _counterfactual(
                predicted_effect="no_grounded_causal_influence_established"
            ),
        }
        self.assertEqual(_judgment(**values).factor_role, "unrelated")
        with self.assertRaises(ValueError):
            _judgment(**{**values, "factor_mechanism": _mechanism()})
        with self.assertRaises(ValueError):
            _judgment(
                **{
                    **values,
                    "counterfactual": _counterfactual(
                        predicted_effect="reduces_defect_likelihood"
                    ),
                }
            )

    def test_counterfactual_is_bound_to_the_candidate_intervention(self) -> None:
        for field, value in (
            ("intervention_ref", "record:other"),
            ("intervention_kind", "remove_candidate"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                counterfactual = _counterfactual()
                counterfactual[field] = value
                _judgment(counterfactual=counterfactual)

    def test_necessity_status_selects_the_counterfactual_effect(self) -> None:
        necessary = _judgment(
            necessity_status="necessary",
            factor_role="unknown",
            factor_mechanism={},
            counterfactual=_counterfactual(predicted_effect="prevents_defect"),
        )
        self.assertEqual(necessary.necessity_status, "necessary")
        unknown = _judgment(
            necessity_status="unknown",
            factor_role="unknown",
            factor_mechanism={},
            counterfactual=_counterfactual(
                predicted_effect="insufficient_grounded_evidence"
            ),
        )
        self.assertEqual(unknown.necessity_status, "unknown")
        for necessity_status, predicted_effect in (
            ("necessary", "insufficient_grounded_evidence"),
            ("unknown", "prevents_defect"),
        ):
            with self.subTest(necessity_status=necessity_status), self.assertRaises(ValueError):
                _judgment(
                    necessity_status=necessity_status,
                    factor_role="unknown",
                    factor_mechanism={},
                    counterfactual=_counterfactual(predicted_effect=predicted_effect),
                )

    def test_contributing_condition_rejects_amplifier_effects(self) -> None:
        for effect in (
            "reduces_defect_exposure",
            "reduces_defect_severity",
        ):
            with self.subTest(effect=effect), self.assertRaisesRegex(
                ValueError,
                "factor role counterfactual effect contradicts role",
            ):
                _judgment(
                    factor_role="contributing_condition",
                    counterfactual=_counterfactual(
                        predicted_effect=effect
                    ),
                )

    def test_amplifying_factor_rejects_condition_effects(self) -> None:
        mechanism = {
            **_mechanism(),
            "mechanism_type": "amplification",
        }
        for effect in (
            "reduces_defect_likelihood",
            "reduces_defect_probability",
        ):
            with self.subTest(effect=effect), self.assertRaisesRegex(
                ValueError,
                "factor role counterfactual effect contradicts role",
            ):
                _judgment(
                    factor_role="amplifying_factor",
                    factor_mechanism=mechanism,
                    counterfactual=_counterfactual(
                        predicted_effect=effect
                    ),
                )

    def test_judgment_requires_complete_typed_identity_and_path_facts(self) -> None:
        for field, value in (
            ("candidate_ref", ""),
            ("candidate_ref", True),
            ("reason", None),
            ("hypothesis_id", 1),
            ("hypothesis_semantic_hash", ""),
            ("defect_fingerprint", False),
            ("seed_binding_identity", ""),
            ("analysis_perspective", None),
            ("request_identity", 1),
            ("evidence_refs", ()),
            ("evidence_refs", ("",)),
            ("evidence_refs", (True,)),
            ("recursive_path", ()),
            ("recursive_path", ("",)),
            ("recursive_path", (True,)),
            ("recursive_path", ("record:decision", "record:defect")),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _judgment(**{field: value})

    def test_recursive_path_must_start_with_candidate_for_construct_and_parse(
        self,
    ) -> None:
        candidate_later_path = (
            "record:decision",
            "record:prompt",
            "record:defect",
        )
        with self.assertRaisesRegex(
            ValueError, "recursive_path must start with candidate_ref"
        ):
            _judgment(recursive_path=candidate_later_path)

        payload = _judgment().to_dict()
        payload["recursive_path"] = list(candidate_later_path)
        with self.assertRaisesRegex(
            ValueError, "recursive_path must start with candidate_ref"
        ):
            FactorRoleJudgment.from_dict(payload)

    def test_round_trip_preserves_the_exact_canonical_identity(self) -> None:
        judgment = _judgment()
        payload = judgment.to_dict()
        self.assertEqual(FactorRoleJudgment.from_dict(payload), judgment)
        self.assertEqual(payload["judgment_identity"], judgment.judgment_identity)
        payload["judgment_identity"] = "factor-role-judgment:v1:forged"
        with self.assertRaises(ValueError):
            FactorRoleJudgment.from_dict(payload)


class FactorRoleRequestProjectionTest(unittest.TestCase):
    def test_root_and_factor_requests_share_blind_active_failure_factual_context(self) -> None:
        state = _lifecycle_state()
        contexts = [
            item["factual_request_projection"]["facts"].get(
                "factual_context"
            )
            for item in state.confirmation_queue
        ]

        self.assertEqual(len(contexts), 2)
        self.assertTrue(all(isinstance(item, dict) for item in contexts))
        self.assertEqual(
            {
                item["failure_signature"]["fingerprint"]
                for item in contexts
            },
            {next(iter(state.defect_states))},
        )
        self.assertTrue(
            all("active_role_binding" not in item for item in contexts)
        )
        self.assertTrue(
            all("causal_role" not in stable_json(item) for item in contexts)
        )
        self.assertTrue(
            all(
                segment["provenance_tier"]
                in {"confirmed_trace", "offline_reconstruction"}
                for context in contexts
                for path in context["tiered_paths"]
                for segment in path["segments"]
            )
        )

    def test_factual_context_rejects_unresolved_revision_and_unlabelled_path_facts(self) -> None:
        validator = getattr(
            causal_judge_module,
            "validate_active_failure_factual_context",
            None,
        )
        self.assertIsNotNone(validator)
        state = _lifecycle_state()
        context = copy.deepcopy(
            state.confirmation_queue[0]["factual_request_projection"][
                "facts"
            ]["factual_context"]
        )
        validator(context)

        mutations = {}
        unresolved = copy.deepcopy(context)
        unresolved["plans"][0]["reference"]["resolution_status"] = (
            "unresolved"
        )
        mutations["unresolved ref"] = unresolved
        stale_revision = copy.deepcopy(context)
        stale_revision["plans"][0]["reference"][
            "revision_provenance_status"
        ] = "mismatch"
        mutations["revision provenance"] = stale_revision
        unlabelled = copy.deepcopy(context)
        unlabelled["tiered_paths"][0]["segments"][0].pop(
            "provenance_tier"
        )
        mutations["unlabelled path provenance"] = unlabelled

        for reason, mutated in mutations.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    validator(mutated)

    def test_request_preserves_business_fields_but_scrubs_prior_attribution_verdicts(
        self,
    ) -> None:
        recorded_candidate = {
            "ref": "record:prompt",
            "content": {
                "origin": {"type": "recorded_source"},
                "causal_role": "defect_introduction",
                "root_verdict": "confirmed",
                "factor_verdict": "contributing_condition",
                "is_root_cause": True,
                "active_role_binding": {"causal_role": "false_closure"},
                "confirmation": "recorded_tool_result",
                "role_description": "repository administrator",
                "factor_name": "replication_factor",
            },
        }
        recorded_evidence = {
            "ref": "record:prompt",
            "content": {
                "origin": {"type": "recorded_source"},
                "causal_role": "defect_introduction",
                "root_verdict": "confirmed",
                "factor_verdict": "contributing_condition",
                "confirmation": "recorded_tool_result",
                "role_description": "repository administrator",
                "factor_name": "replication_factor",
            },
        }
        request = _request(
            candidate_reference=recorded_candidate,
            supporting_evidence=(recorded_evidence,),
        )
        projection = factor_role_request_projection(request)
        request_text = stable_json(projection)
        for hidden in (
            "causal_role",
            "root_verdict",
            "factor_verdict",
            "is_root_cause",
            "active_role_binding",
        ):
            self.assertNotIn(hidden, request_text)
        projected_content = projection["facts"]["candidate_reference"][
            "content"
        ]
        self.assertEqual(
            projected_content["role_description"],
            "repository administrator",
        )
        self.assertEqual(
            projected_content["factor_name"], "replication_factor"
        )
        self.assertEqual(
            projected_content["confirmation"], "recorded_tool_result"
        )
        changed = copy.deepcopy(projection)
        changed["facts"]["supporting_evidence"][0]["content"]["origin"][
            "type"
        ] = "different_recorded_source"
        self.assertNotEqual(
            factor_role_request_projection_identity(changed),
            factor_role_request_identity(request),
        )

    def test_nested_prior_verdicts_do_not_change_root_or_factor_request_identity_or_prompt(
        self,
    ) -> None:
        state = _lifecycle_state()
        root_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "root"
        )
        clean_root = AgenticRecursiveAnalyzer._build_confirmation_request(
            state,
            root_queue,
        )
        root_facts = clean_root.factual_dict()
        dirty_root_facts = copy.deepcopy(root_facts)
        for field_name in (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "competing_hypotheses",
            "task_obligations",
        ):
            target = dirty_root_facts[field_name]
            if isinstance(target, list) and not target:
                target.append({})
            mapping = target[0] if isinstance(target, list) else target
            mapping["prior_attribution"] = {
                "causal_role": "defect_introduction_root",
                "root_verdict": "confirmed",
                "factor_verdict": "unrelated",
                "active_role_binding": {"causal_role": "false_closure"},
            }
        dirty_context = dirty_root_facts["factual_context"]
        dirty_context["plans"][0]["data"]["prior_attribution"] = {
            "causal_role": "false_closure",
            "is_root_cause": True,
            "role_description": "deployment administrator",
        }
        dirty_root = RootConfirmationRequest(
            candidate_ref=dirty_root_facts["candidate_ref"],
            defect_state=DefectState.from_dict(dirty_root_facts["defect_state"]),
            recursive_path=tuple(dirty_root_facts["recursive_path"]),
            candidate_reference=dirty_root_facts["candidate_reference"],
            recursive_path_references=tuple(
                dirty_root_facts["recursive_path_references"]
            ),
            supporting_evidence=tuple(dirty_root_facts["supporting_evidence"]),
            opposing_evidence=tuple(dirty_root_facts["opposing_evidence"]),
            competing_hypotheses=tuple(
                dirty_root_facts["competing_hypotheses"]
            ),
            task_obligations=tuple(dirty_root_facts["task_obligations"]),
            hypothesis_id=dirty_root_facts["hypothesis_id"],
            hypothesis_semantic_hash=dirty_root_facts[
                "hypothesis_semantic_hash"
            ],
            seed_binding_identity=dirty_root_facts["seed_binding_identity"],
            analysis_perspective=dirty_root_facts["analysis_perspective"],
            factual_context=dirty_context,
        )
        self.assertEqual(
            root_confirmation_request_identity(dirty_root),
            root_confirmation_request_identity(clean_root),
        )
        self.assertEqual(
            build_recursive_confirmation_prompt(dirty_root),
            build_recursive_confirmation_prompt(clean_root),
        )

        factor_context = copy.deepcopy(
            next(
                item
                for item in state.confirmation_queue
                if item["review_scope"] == "non_root"
            )["factual_request_projection"]["facts"]["factual_context"]
        )
        clean_factor_fields = {
            "candidate_reference": {
                "ref": "record:prompt",
                "content": {"role_description": "deployment administrator"},
            },
            "recursive_path_references": (
                {"ref": "record:prompt", "content": {}},
                {"ref": "record:decision", "content": "omitted requirement"},
                {"ref": "record:defect", "content": "missing check"},
            ),
            "supporting_evidence": (
                {"ref": "record:prompt", "content": {}},
            ),
            "opposing_evidence": (
                {"ref": "record:policy", "content": {}},
            ),
            "task_obligations": (
                {"ref": "record:task", "content": {}},
            ),
        }
        clean_factor = _request(
            **clean_factor_fields,
            factual_context=factor_context,
        )
        dirty_factor_context = copy.deepcopy(factor_context)
        dirty_factor_context["plans"][0]["data"]["prior_attribution"] = {
            "causal_role": "false_closure",
            "root_verdict": "confirmed",
            "factor_verdict": "amplifying_factor",
            "role_description": "deployment administrator",
        }
        dirty_factor = _request(
            candidate_reference={
                "ref": "record:prompt",
                "content": {
                    "causal_role": "defect_introduction_root",
                    "root_verdict": "confirmed",
                    "role_description": "deployment administrator",
                },
            },
            recursive_path_references=(
                {
                    "ref": "record:prompt",
                    "content": {"factor_verdict": "contributing_condition"},
                },
                {"ref": "record:decision", "content": "omitted requirement"},
                {"ref": "record:defect", "content": "missing check"},
            ),
            supporting_evidence=(
                {
                    "ref": "record:prompt",
                    "content": {"causal_role": "amplifying_condition"},
                },
            ),
            opposing_evidence=(
                {
                    "ref": "record:policy",
                    "content": {"is_causal_factor": False},
                },
            ),
            task_obligations=(
                {
                    "ref": "record:task",
                    "content": {"active_role_binding": {"forged": True}},
                },
            ),
            factual_context=dirty_factor_context,
        )
        self.assertEqual(
            factor_role_request_identity(dirty_factor),
            factor_role_request_identity(clean_factor),
        )
        self.assertEqual(
            build_factor_role_prompt(dirty_factor),
            build_factor_role_prompt(clean_factor),
        )
        factor_text = stable_json(factor_role_request_projection(dirty_factor))
        for hidden in (
            "causal_role",
            "root_verdict",
            "factor_verdict",
            "is_causal_factor",
            "active_role_binding",
            '"factor_role"',
        ):
            self.assertNotIn(hidden, factor_text)
        self.assertEqual(
            factor_role_request_projection(dirty_factor)["facts"][
                "candidate_reference"
            ]["content"]["role_description"],
            "deployment administrator",
        )

    def test_normalized_verdict_aliases_are_absent_from_all_root_and_factor_request_derivations(
        self,
    ) -> None:
        aliases = {
            "causalRole": "defect_introduction_root",
            "CausalRole": "false_closure",
            "CAUSAL_ROLE": "verification_omission",
            "rootVerdict": "confirmed",
            "RootVerdict": "confirmed",
            "ROOT_VERDICT": "confirmed",
            "factorVerdict": "amplifying_factor",
            "FactorVerdict": "unrelated",
            "activeRoleBinding": {"causalRole": "false_closure"},
            "IsRootCause": True,
            "isCausalFactor": True,
        }

        state = _lifecycle_state()
        root_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "root"
        )
        root_facts = AgenticRecursiveAnalyzer._build_confirmation_request(
            state, root_queue
        ).factual_dict()
        root_facts["candidate_reference"]["business_fields"] = {
            "role_description": "repository administrator",
            "factor_name": "replication_factor",
            "replication_factor": 3,
        }
        clean_root = _root_request_from_facts(root_facts)
        dirty_root_facts = copy.deepcopy(clean_root.factual_dict())
        for field_name in (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "competing_hypotheses",
            "task_obligations",
        ):
            target = dirty_root_facts[field_name]
            if isinstance(target, list):
                if not target:
                    target.append({})
                target[0].update(copy.deepcopy(aliases))
            else:
                target.update(copy.deepcopy(aliases))
        dirty_root_facts["factual_context"]["plans"][0]["data"].update(
            copy.deepcopy(aliases)
        )
        dirty_root = _root_request_from_facts(dirty_root_facts)

        factor_context = copy.deepcopy(
            next(
                item
                for item in state.confirmation_queue
                if item["review_scope"] == "non_root"
            )["factual_request_projection"]["facts"]["factual_context"]
        )
        clean_factor = _request(
            candidate_reference={
                "ref": "record:prompt",
                "content": {
                    "role_description": "repository administrator",
                    "factor_name": "replication_factor",
                    "replication_factor": 3,
                },
            },
            factual_context=factor_context,
        )
        dirty_factor_facts = copy.deepcopy(clean_factor.factual_dict())
        for field_name in (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "task_obligations",
        ):
            target = dirty_factor_facts[field_name]
            if isinstance(target, list):
                if not target:
                    target.append({})
                target[0].update(copy.deepcopy(aliases))
            else:
                target.update(copy.deepcopy(aliases))
        dirty_factor_facts["factual_context"]["plans"][0]["data"].update(
            copy.deepcopy(aliases)
        )
        dirty_factor = _factor_request_from_facts(dirty_factor_facts)

        strict_summary_facts = copy.deepcopy(clean_factor.factual_dict())
        strict_summary_facts["confirmed_root_summaries"][0].update(
            copy.deepcopy(aliases)
        )
        with self.assertRaisesRegex(ValueError, "inexact schema"):
            _factor_request_from_facts(strict_summary_facts)

        clean_root_projection = root_confirmation_request_projection(
            clean_root
        )
        dirty_root_projection = root_confirmation_request_projection(
            dirty_root
        )
        clean_factor_projection = factor_role_request_projection(
            clean_factor
        )
        dirty_factor_projection = factor_role_request_projection(
            dirty_factor
        )
        self.assertEqual(dirty_root_projection, clean_root_projection)
        self.assertEqual(dirty_factor_projection, clean_factor_projection)
        self.assertEqual(
            root_confirmation_request_identity(dirty_root),
            root_confirmation_request_identity(clean_root),
        )
        self.assertEqual(
            factor_role_request_identity(dirty_factor),
            factor_role_request_identity(clean_factor),
        )
        clean_root_prompt = build_recursive_confirmation_prompt(clean_root)
        dirty_root_prompt = build_recursive_confirmation_prompt(dirty_root)
        clean_factor_prompt = build_factor_role_prompt(clean_factor)
        dirty_factor_prompt = build_factor_role_prompt(dirty_factor)
        self.assertEqual(dirty_root_prompt, clean_root_prompt)
        self.assertEqual(dirty_factor_prompt, clean_factor_prompt)
        self.assertEqual(
            _judge_cache_key("root_confirmation", dirty_root_prompt),
            _judge_cache_key("root_confirmation", clean_root_prompt),
        )
        self.assertEqual(
            _judge_cache_key("factor_role", dirty_factor_prompt),
            _judge_cache_key("factor_role", clean_factor_prompt),
        )
        def normalized_keys(value: object) -> set[str]:
            keys: set[str] = set()

            def visit(item: object) -> None:
                if isinstance(item, dict):
                    for key, child in item.items():
                        keys.add(
                            re.sub(
                                r"[^a-z0-9]",
                                "",
                                str(key).casefold(),
                            )
                        )
                        visit(child)
                elif isinstance(item, (list, tuple)):
                    for child in item:
                        visit(child)
                elif isinstance(item, str) and item.strip().startswith(
                    ("{", "[")
                ):
                    try:
                        visit(json.loads(item))
                    except json.JSONDecodeError:
                        pass

            visit(value)
            return keys

        for projection in (dirty_root_projection, dirty_factor_projection):
            projected_keys = normalized_keys(projection)
            for hidden in (
                "causalrole",
                "rootverdict",
                "factorverdict",
                "activerolebinding",
                "isrootcause",
                "iscausalfactor",
            ):
                self.assertNotIn(hidden, projected_keys)
        self.assertEqual(
            dirty_factor_projection["facts"]["candidate_reference"][
                "content"
            ],
            clean_factor_projection["facts"]["candidate_reference"][
                "content"
            ],
        )

    def test_root_and_factor_all_nested_input_surfaces_reject_analysis_control_schemas(
        self,
    ) -> None:
        state = _lifecycle_state()
        root_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "root"
        )
        clean_root_facts = (
            AgenticRecursiveAnalyzer._build_confirmation_request(
                state, root_queue
            ).factual_dict()
        )
        factor_context = copy.deepcopy(
            next(
                item
                for item in state.confirmation_queue
                if item["review_scope"] == "non_root"
            )["factual_request_projection"]["facts"]["factual_context"]
        )
        clean_factor_facts = _request(
            factual_context=factor_context
        ).factual_dict()
        controls = (
            {"schema": "global-candidate-judgment/v11"},
            {"schema": "factor-role-judgment/v1"},
            {"schema": "root-confirmation/v17"},
        )

        def inject(facts, field_name, control):
            dirty = copy.deepcopy(facts)
            if field_name == "factual_context":
                dirty[field_name]["plans"][0]["data"][
                    "nested_analysis_control"
                ] = copy.deepcopy(control)
            else:
                target = dirty[field_name]
                if isinstance(target, list):
                    if not target:
                        target.append({})
                    target[0]["nested_analysis_control"] = copy.deepcopy(
                        control
                    )
                else:
                    target["nested_analysis_control"] = copy.deepcopy(
                        control
                    )
            return dirty

        root_surfaces = (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "competing_hypotheses",
            "task_obligations",
            "factual_context",
        )
        factor_surfaces = (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "task_obligations",
            "confirmed_root_summaries",
            "factual_context",
        )
        for control in controls:
            for field_name in root_surfaces:
                with self.subTest(
                    request="root",
                    field_name=field_name,
                    schema=control["schema"],
                ), self.assertRaisesRegex(
                    ValueError, "analysis-control"
                ):
                    _root_request_from_facts(
                        inject(clean_root_facts, field_name, control)
                    )
            for field_name in factor_surfaces:
                with self.subTest(
                    request="factor",
                    field_name=field_name,
                    schema=control["schema"],
                ), self.assertRaisesRegex(
                    ValueError, "analysis-control"
                ):
                    _factor_request_from_facts(
                        inject(clean_factor_facts, field_name, control)
                    )

    def test_request_rejects_nested_analysis_control_envelopes(self) -> None:
        fields = (
            "candidate_reference",
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "task_obligations",
            "confirmed_root_summaries",
        )
        control_envelopes = (
            {"schema": "global-candidate-judgment/v11"},
            {"schema": "factor-role-judgment/v1"},
            {"schema": "root-confirmation/v17"},
            {"provenance_class": "analysis_control"},
        )
        for field in fields:
            for control_envelope in control_envelopes:
                with self.subTest(field=field, control_envelope=control_envelope), self.assertRaises(ValueError):
                    nested = {"level_one": [{"level_two": control_envelope}]}
                    value = nested if field == "candidate_reference" else (nested,)
                    _request(**{field: value})

    def test_request_rejects_validation_envelope_schema_via_shared_classifier(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "analysis-control"):
            _request(
                candidate_reference={
                    "ref": "record:prompt",
                    "content": "ambiguous requirement",
                    "schema": "global-candidate-validation-envelope/v11",
                }
            )

    def test_request_accepts_unversioned_business_schema_with_control_like_prefix(
        self,
    ) -> None:
        request = _request(
            candidate_reference={
                "ref": "record:prompt",
                "content": "ambiguous requirement",
                "schema": "root-confirmation/reviewer-notes",
            }
        )

        self.assertEqual(
            request.candidate_reference["schema"],
            "root-confirmation/reviewer-notes",
        )

    def test_request_rejects_non_string_keys_in_reference_envelopes(self) -> None:
        envelope = {
            **_reference_envelope("record:strict-envelope"),
            _StringSubclass("extra_key"): "value",
        }
        with self.assertRaisesRegex(
            ValueError, "reference envelope keys must be exact strings"
        ):
            _request(supporting_evidence=({"reference": envelope},))

    def test_request_requires_complete_typed_identity_and_path_facts(self) -> None:
        for field, value in (
            ("candidate_ref", ""),
            ("candidate_ref", True),
            ("hypothesis_id", None),
            ("hypothesis_semantic_hash", ""),
            ("seed_binding_identity", 1),
            ("analysis_perspective", ""),
            ("recursive_path", ()),
            ("recursive_path", ("",)),
            ("recursive_path", (True,)),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _request(**{field: value})

    def test_request_path_must_start_with_candidate(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "recursive_path must start with candidate_ref"
        ):
            _request(
                recursive_path=(
                    "record:decision",
                    "record:prompt",
                    "record:defect",
                )
            )

    def test_confirmed_root_summaries_require_exact_canonical_schema_and_bindings(
        self,
    ) -> None:
        invalid_summaries = []
        for field in _confirmed_root_summary():
            missing = _confirmed_root_summary()
            del missing[field]
            invalid_summaries.append(missing)
        invalid_summaries.extend(
            (
                _confirmed_root_summary(extra_field="forbidden"),
                _confirmed_root_summary(schema="factor-role-root-evidence-summary/v1"),
                _confirmed_root_summary(confirmation_status="confirmed"),
                _confirmed_root_summary(factor_role="necessary_cause"),
                _confirmed_root_summary(candidate_ref=""),
                _confirmed_root_summary(hypothesis_id=""),
                _confirmed_root_summary(hypothesis_semantic_hash=""),
                _confirmed_root_summary(reason=""),
                _confirmed_root_summary(evidence_refs=[]),
                _confirmed_root_summary(evidence_refs=[""]),
                _confirmed_root_summary(recursive_path=[]),
                _confirmed_root_summary(recursive_path=["record:other"]),
                _confirmed_root_summary(defect_fingerprint="sha256:foreign"),
                _confirmed_root_summary(seed_binding_identity="seed:foreign"),
            )
        )
        for summary in invalid_summaries:
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                _request(confirmed_root_summaries=(summary,))

    def test_confirmed_root_summary_rejects_each_identity_field_forgery(
        self,
    ) -> None:
        changed_defect = _defect_state().transformed(
            label="Different bound defect.",
            mechanism="Different bound mechanism.",
            transformation_reason="Exercise summary identity binding.",
        )
        mutations = {
            "hypothesis_id": ("hypothesis:forged", {}),
            "hypothesis_semantic_hash": ("sha256:forged", {}),
            "candidate_ref": ("record:forged-root", {}),
            "defect_fingerprint": (
                changed_defect.fingerprint,
                {"defect_state": changed_defect},
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
        for field, (replacement, request_overrides) in mutations.items():
            projection = factor_role_request_projection(_request())
            forged = projection["facts"]["confirmed_root_summaries"][0]
            forged[field] = replacement
            if field == "candidate_ref":
                forged["recursive_path"][0] = replacement
            for request_field, request_value in request_overrides.items():
                projection["facts"][request_field] = (
                    request_value.to_dict()
                    if isinstance(request_value, DefectState)
                    else request_value
                )
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "confirmation_identity"
            ):
                validate_factor_role_request_projection(projection)

    def test_projection_contains_only_factual_fields_and_matches_request_identity(self) -> None:
        request = _request()
        projection = factor_role_request_projection(request)
        serialized = stable_json(projection)
        self.assertNotIn("global_role", serialized)
        self.assertNotIn("review_scope", serialized)
        self.assertNotIn("origin", serialized)
        self.assertEqual(
            factor_role_request_projection_identity(projection),
            factor_role_request_identity(request),
        )

    def test_each_factual_field_changes_the_request_identity(self) -> None:
        projection = factor_role_request_projection(_request())
        original_identity = factor_role_request_projection_identity(projection)
        replacements = {
            "candidate_ref": "record:other-prompt",
            "defect_state": _defect_state().transformed(
                label="Different defect.",
                mechanism="Different mechanism.",
                transformation_reason="Different fact.",
            ).to_dict(),
            "recursive_path": [
                "record:prompt",
                "record:other",
                "record:defect",
            ],
            "candidate_reference": {"ref": "record:prompt", "content": "changed"},
            "recursive_path_references": [{"ref": "record:other", "content": "changed"}],
            "supporting_evidence": [{"ref": "record:other", "content": "changed"}],
            "opposing_evidence": [{"ref": "record:other", "content": "changed"}],
            "task_obligations": [{"ref": "record:other", "content": "changed"}],
            "confirmed_root_summaries": [
                _confirmed_root_summary(
                    candidate_ref="record:other",
                    reason="Changed grounded summary.",
                    evidence_refs=["record:other-evidence"],
                    recursive_path=["record:other", "record:defect"],
                )
            ],
            "hypothesis_id": "hypothesis:other",
            "hypothesis_semantic_hash": "sha256:other",
            "seed_binding_identity": "seed:other",
            "analysis_perspective": "Different perspective.",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                mutated = copy.deepcopy(projection)
                mutated["facts"][field] = replacement
                if field == "candidate_ref":
                    mutated["facts"]["recursive_path"][0] = replacement
                elif field == "defect_state":
                    summary = mutated["facts"]["confirmed_root_summaries"][0]
                    summary["defect_fingerprint"] = DefectState.from_dict(
                        replacement
                    ).fingerprint
                    summary["confirmation_identity"] = confirmation_identity_for(
                        hypothesis_id=summary["hypothesis_id"],
                        hypothesis_semantic_hash=summary[
                            "hypothesis_semantic_hash"
                        ],
                        candidate_ref=summary["candidate_ref"],
                        defect_fingerprint=summary["defect_fingerprint"],
                        recursive_path=tuple(summary["recursive_path"]),
                        seed_binding_identity=summary[
                            "seed_binding_identity"
                        ],
                    )
                elif field == "seed_binding_identity":
                    summary = mutated["facts"]["confirmed_root_summaries"][0]
                    summary["seed_binding_identity"] = replacement
                    summary["confirmation_identity"] = confirmation_identity_for(
                        hypothesis_id=summary["hypothesis_id"],
                        hypothesis_semantic_hash=summary[
                            "hypothesis_semantic_hash"
                        ],
                        candidate_ref=summary["candidate_ref"],
                        defect_fingerprint=summary["defect_fingerprint"],
                        recursive_path=tuple(summary["recursive_path"]),
                        seed_binding_identity=summary[
                            "seed_binding_identity"
                        ],
                    )
                self.assertNotEqual(
                    factor_role_request_projection_identity(mutated),
                    original_identity,
                )

    def test_projection_rejects_unknown_and_missing_fields(self) -> None:
        projection = factor_role_request_projection(_request())
        with_extra = copy.deepcopy(projection)
        with_extra["facts"]["global_role"] = "contributing_condition"
        with self.assertRaises(ValueError):
            validate_factor_role_request_projection(with_extra)
        missing = copy.deepcopy(projection)
        del missing["facts"]["confirmed_root_summaries"]
        with self.assertRaises(ValueError):
            validate_factor_role_request_projection(missing)


class FactorRolePromptAndParserTest(unittest.TestCase):
    @staticmethod
    def _contract_enum(contract: object, field_name: str) -> str:
        ordered = []
        for row in contract:
            value = row[field_name]
            if value not in ordered:
                ordered.append(value)
        return "|".join(ordered)

    def test_schema_repair_constraints_include_the_exact_factor_role_contract(
        self,
    ) -> None:
        request = _request()
        constraints = causal_judge_module._repair_constraints(
            stage="factor_role_judgment",
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validation_error=(
                "ValueError: factor role judgment has an inexact schema"
            ),
        )

        self.assertEqual(
            set(constraints["exact_top_level_fields"]),
            {
                "candidate_ref",
                "necessity_status",
                "factor_role",
                "reason",
                "confidence",
                "evidence_refs",
                "recursive_path",
                "factor_mechanism",
                "counterfactual",
                "hypothesis_id",
                "hypothesis_semantic_hash",
                "defect_fingerprint",
                "seed_binding_identity",
                "analysis_perspective",
                "request_identity",
            },
        )
        self.assertEqual(
            constraints["exact_bindings"]["candidate_ref"],
            request.candidate_ref,
        )
        self.assertEqual(
            constraints["exact_bindings"]["request_identity"],
            factor_role_request_identity(request),
        )
        self.assertEqual(
            constraints["role_contract"],
            [
                {
                    str(key): (
                        list(value)
                        if isinstance(value, tuple)
                        else dict(value)
                        if isinstance(value, causal_state_module.FrozenMapping)
                        else value
                    )
                    for key, value in row.items()
                }
                for row in causal_state_module.FACTOR_ROLE_CONTRACT
            ],
        )
        self.assertEqual(
            constraints["allowed_mechanism_target_refs"],
            list(request.recursive_path[1:]),
        )
        self.assertEqual(
            constraints["factor_mechanism_contract"],
            {
                "empty_object_roles": [
                    "unknown",
                    "unrelated",
                ],
                "exact_object_schema": {
                    "schema": "factor-role-mechanism/v1",
                    "mechanism_type": (
                        "exact mechanism_type from selected "
                        "role_contract row"
                    ),
                    "source_ref": request.candidate_ref,
                    "target_ref": (
                        "one exact ref from "
                        "allowed_mechanism_target_refs"
                    ),
                    "effect": "non-empty grounded effect",
                },
            },
        )

    def test_prompt_and_repair_share_strict_non_root_role_boundaries(
        self,
    ) -> None:
        request = _request()
        prompt = json.loads(build_factor_role_prompt(request))
        constraints = causal_judge_module._repair_constraints(
            stage="factor_role_judgment",
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validation_error="ValueError: factor role is ambiguous",
        )

        self.assertEqual(
            prompt["role_boundary_contract"],
            constraints["role_boundary_contract"],
        )
        boundaries = prompt["role_boundary_contract"]
        self.assertEqual(
            boundaries["observation_only_verification"][
                "required_factor_role"
            ],
            "unrelated",
        )
        self.assertEqual(
            boundaries["repair_window_closing_interruption"][
                "required_factor_role"
            ],
            "amplifying_factor",
        )
        self.assertIn(
            "Temporal downstream position alone",
            boundaries["temporal_position_rule"],
        )

    def test_necessary_materialization_requires_a_confirmed_upstream_root(
        self,
    ) -> None:
        request = _request(
            candidate_ref="record:change",
            recursive_path=("record:change", "record:defect"),
            candidate_reference={
                "ref": "record:change",
                "content": "The implementation materialized the omission.",
            },
            recursive_path_references=(
                {"ref": "record:change", "content": "implementation"},
                {"ref": "record:defect", "content": "missing behavior"},
            ),
            supporting_evidence=(
                {"ref": "record:change", "content": "implementation"},
            ),
            confirmed_root_summaries=(
                _confirmed_root_summary(
                    recursive_path=(
                        "record:decision",
                        "record:change",
                        "record:defect",
                    )
                ),
            ),
        )
        payload = {
            **_factor_payload(
                necessity_status="necessary",
                factor_role="downstream_materialization",
            ),
            "candidate_ref": request.candidate_ref,
            "evidence_refs": ["record:change"],
            "recursive_path": list(request.recursive_path),
            "factor_mechanism": {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": "downstream_materialization",
                "source_ref": request.candidate_ref,
                "target_ref": "record:defect",
                "effect": "materializes_the_confirmed_upstream_omission",
            },
            "counterfactual": {
                "schema": "factor-role-counterfactual/v1",
                "intervention_ref": request.candidate_ref,
                "intervention_kind": (
                    "replace_with_semantically_correct_behavior"
                ),
                "predicted_effect": "prevents_defect",
            },
            "request_identity": factor_role_request_identity(request),
        }

        parsed = parse_factor_role_judgment(payload, request=request)
        self.assertEqual(
            parsed.factor_role,
            "downstream_materialization",
        )

        unbound_request = replace(
            request,
            confirmed_root_summaries=(
                _confirmed_root_summary(
                    recursive_path=(
                        "record:decision",
                        "record:defect",
                    )
                ),
            ),
        )
        payload["request_identity"] = factor_role_request_identity(
            unbound_request
        )
        with self.assertRaisesRegex(
            ValueError,
            "confirmed upstream root",
        ):
            parse_factor_role_judgment(
                payload,
                request=unbound_request,
            )

    def test_prompt_parser_and_domain_follow_temporary_contract_changes(
        self,
    ) -> None:
        lookup = getattr(
            causal_state_module, "factor_role_contract_entry", None
        )
        self.assertTrue(callable(lookup))
        contract = causal_state_module.FACTOR_ROLE_CONTRACT
        original_entry = lookup(
            "not_necessary", "contributing_condition"
        )
        self.assertIsNotNone(original_entry)
        modified_entry = dict(original_entry)
        modified_entry["predicted_effects"] = ("contract_only_effect",)
        modified_contract = tuple(
            modified_entry if row is original_entry else row
            for row in contract
        )

        with patch.object(
            causal_state_module,
            "FACTOR_ROLE_CONTRACT",
            modified_contract,
        ), patch.object(
            causal_judge_module,
            "FACTOR_ROLE_CONTRACT",
            modified_contract,
        ):
            prompt = json.loads(build_factor_role_prompt(_request()))
            prompt_entry = next(
                row
                for row in prompt["role_contract"]
                if row["necessity_status"] == "not_necessary"
                and row["factor_role"] == "contributing_condition"
            )
            self.assertEqual(
                prompt_entry["predicted_effects"],
                ["contract_only_effect"],
            )
            self.assertEqual(
                prompt["required_json_schema"]["necessity_status"],
                self._contract_enum(
                    modified_contract, "necessity_status"
                ),
            )
            self.assertEqual(
                prompt["required_json_schema"]["factor_role"],
                self._contract_enum(modified_contract, "factor_role"),
            )

            old_payload = _factor_payload()
            with self.assertRaisesRegex(ValueError, "counterfactual effect"):
                parse_factor_role_judgment(old_payload, request=_request())
            with self.assertRaisesRegex(ValueError, "counterfactual effect"):
                _judgment()

            new_payload = _factor_payload()
            new_payload["counterfactual"][
                "predicted_effect"
            ] = "contract_only_effect"
            parsed = parse_factor_role_judgment(
                new_payload, request=_request()
            )
            direct = _judgment(
                counterfactual=_counterfactual(
                    predicted_effect="contract_only_effect"
                )
            )
            self.assertEqual(
                parsed.counterfactual["predicted_effect"],
                "contract_only_effect",
            )
            self.assertEqual(
                direct.counterfactual["predicted_effect"],
                "contract_only_effect",
            )

        removed_contract = tuple(
            row
            for row in contract
            if not (
                row["necessity_status"] == "not_necessary"
                and row["factor_role"] == "contributing_condition"
            )
        )
        with patch.object(
            causal_state_module,
            "FACTOR_ROLE_CONTRACT",
            removed_contract,
        ), patch.object(
            causal_judge_module,
            "FACTOR_ROLE_CONTRACT",
            removed_contract,
        ):
            prompt = json.loads(build_factor_role_prompt(_request()))
            self.assertFalse(
                any(
                    row["necessity_status"] == "not_necessary"
                    and row["factor_role"] == "contributing_condition"
                    for row in prompt["role_contract"]
                )
            )
            self.assertEqual(
                prompt["required_json_schema"]["factor_role"],
                self._contract_enum(removed_contract, "factor_role"),
            )
            self.assertNotIn(
                "contributing_condition",
                prompt["required_json_schema"]["factor_role"].split("|"),
            )
            with self.assertRaisesRegex(ValueError, "role contract"):
                parse_factor_role_judgment(
                    _factor_payload(), request=_request()
                )
            with self.assertRaisesRegex(ValueError, "role contract"):
                _judgment()

        without_unknown_necessity = tuple(
            row
            for row in contract
            if row["necessity_status"] != "unknown"
        )
        with patch.object(
            causal_state_module,
            "FACTOR_ROLE_CONTRACT",
            without_unknown_necessity,
        ), patch.object(
            causal_judge_module,
            "FACTOR_ROLE_CONTRACT",
            without_unknown_necessity,
        ):
            prompt = json.loads(build_factor_role_prompt(_request()))
            self.assertEqual(
                prompt["required_json_schema"]["necessity_status"],
                self._contract_enum(
                    without_unknown_necessity, "necessity_status"
                ),
            )
            self.assertNotIn(
                "unknown",
                prompt["required_json_schema"][
                    "necessity_status"
                ].split("|"),
            )

    def test_prompt_contains_only_factual_request_and_independent_role_contract(self) -> None:
        request = _request()
        prompt = build_factor_role_prompt(request)
        payload = json.loads(prompt)

        self.assertEqual(payload["request"], request.factual_dict())
        self.assertNotIn("global_role", prompt)
        self.assertNotIn("review_scope", prompt)
        self.assertNotIn("scheduling", prompt.lower())
        self.assertNotIn("expected benchmark label", prompt.lower())
        rules = " ".join(payload["rules"]).lower()
        self.assertIn("independent dimensions", rules)
        self.assertIn("necessity", rules)
        self.assertIn("downstream materialization", rules)
        self.assertIn("already introduced defect", rules)
        self.assertIn("exact request identity", rules)
        self.assertIn("cite only", rules)
        self.assertIn("allowed_fact_refs", rules)
        self.assertIn("ordinary nested ref", rules)
        self.assertIn("at least one", rules)
        self.assertIn("empty array", rules)
        allowed_fact_refs = sorted(factor_role_request_fact_refs(request))
        self.assertEqual(payload["allowed_fact_refs"], allowed_fact_refs)
        self.assertEqual(
            payload["required_json_schema"]["necessity_status"],
            self._contract_enum(
                causal_state_module.FACTOR_ROLE_CONTRACT,
                "necessity_status",
            ),
        )
        self.assertEqual(
            payload["required_json_schema"]["factor_role"],
            self._contract_enum(
                causal_state_module.FACTOR_ROLE_CONTRACT,
                "factor_role",
            ),
        )
        evidence_example = payload["required_json_schema"]["evidence_refs"]
        self.assertTrue(evidence_example)
        self.assertTrue(
            set(evidence_example).issubset(
                allowed_fact_refs
            )
        )
        self.assertEqual(
            payload["required_json_schema"]["factor_mechanism"],
            (
                "{} | exact factor-role-mechanism object selected by "
                "factor_mechanism_contract"
            ),
        )
        mechanism_contract = payload["factor_mechanism_contract"]
        self.assertEqual(
            mechanism_contract.get("selection"),
            "exact factor_mechanism from selected role_contract row",
        )
        self.assertEqual(
            set(mechanism_contract["exact_object_schema"]),
            {
                "schema",
                "mechanism_type",
                "source_ref",
                "target_ref",
                "effect",
            },
        )
        self.assertEqual(
            mechanism_contract["exact_object_schema"]["mechanism_type"],
            "exact mechanism_type from selected role_contract row",
        )
        self.assertEqual(
            payload["allowed_mechanism_target_refs"],
            list(request.recursive_path[1:]),
        )
        self.assertEqual(
            mechanism_contract["exact_object_schema"]["target_ref"],
            "one exact ref from allowed_mechanism_target_refs",
        )
        self.assertIn(
            "recursive_path[1:]",
            " ".join(payload["rules"]),
        )
        self.assertNotIn("empty_object_when", mechanism_contract)
        self.assertNotIn("exact_object_when", mechanism_contract)
        self.assertNotIn("counterfactual_effect_contract", payload)

    def test_prompt_role_contract_is_the_exact_parser_and_domain_matrix(self) -> None:
        prompt = json.loads(build_factor_role_prompt(_request()))
        expected_contract = [
            {
                "necessity_status": "necessary",
                "factor_role": "unknown",
                "factor_mechanism": {},
                "predicted_effects": ["prevents_defect"],
            },
            {
                "necessity_status": "necessary",
                "factor_role": "downstream_materialization",
                "factor_mechanism": {
                    "mechanism_type": "downstream_materialization"
                },
                "predicted_effects": ["prevents_defect"],
            },
            {
                "necessity_status": "unknown",
                "factor_role": "unknown",
                "factor_mechanism": {},
                "predicted_effects": ["insufficient_grounded_evidence"],
            },
            {
                "necessity_status": "not_necessary",
                "factor_role": "contributing_condition",
                "factor_mechanism": {
                    "mechanism_type": "enabling_condition"
                },
                "predicted_effects": [
                    "reduces_defect_likelihood",
                    "reduces_defect_probability",
                ],
            },
            {
                "necessity_status": "not_necessary",
                "factor_role": "amplifying_factor",
                "factor_mechanism": {"mechanism_type": "amplification"},
                "predicted_effects": [
                    "reduces_defect_exposure",
                    "reduces_defect_severity",
                ],
            },
            {
                "necessity_status": "not_necessary",
                "factor_role": "downstream_materialization",
                "factor_mechanism": {
                    "mechanism_type": "downstream_materialization"
                },
                "predicted_effects": [
                    "defect_still_present_without_materialization"
                ],
            },
            {
                "necessity_status": "not_necessary",
                "factor_role": "unrelated",
                "factor_mechanism": {},
                "predicted_effects": [
                    "no_grounded_causal_influence_established"
                ],
            },
        ]
        self.assertEqual(prompt["role_contract"], expected_contract)
        required_schema = stable_json(prompt["required_json_schema"])
        self.assertIn("role_contract", required_schema)
        self.assertIn("cross-row", " ".join(prompt["rules"]).lower())

        for row in prompt["role_contract"]:
            with self.subTest(
                necessity_status=row["necessity_status"],
                factor_role=row["factor_role"],
            ):
                request = _request()
                for predicted_effect in row["predicted_effects"]:
                    payload = _factor_payload(
                        necessity_status=row["necessity_status"],
                        factor_role=row["factor_role"],
                    )
                    if (
                        row["necessity_status"] == "necessary"
                        and row["factor_role"]
                        == "downstream_materialization"
                    ):
                        request = _request(
                            candidate_ref="record:decision",
                            recursive_path=(
                                "record:decision",
                                "record:defect",
                            ),
                            candidate_reference={
                                "ref": "record:decision",
                                "content": "materialized omission",
                            },
                            recursive_path_references=(
                                {
                                    "ref": "record:decision",
                                    "content": "materialized omission",
                                },
                                {
                                    "ref": "record:defect",
                                    "content": "missing behavior",
                                },
                            ),
                            supporting_evidence=(
                                {
                                    "ref": "record:decision",
                                    "content": "materialized omission",
                                },
                            ),
                            confirmed_root_summaries=(
                                _confirmed_root_summary(
                                    candidate_ref="record:prompt",
                                    recursive_path=(
                                        "record:prompt",
                                        "record:decision",
                                        "record:defect",
                                    ),
                                ),
                            ),
                        )
                        payload.update(
                            {
                                "candidate_ref": request.candidate_ref,
                                "evidence_refs": [
                                    request.candidate_ref
                                ],
                                "recursive_path": list(
                                    request.recursive_path
                                ),
                                "factor_mechanism": {
                                    "schema": (
                                        "factor-role-mechanism/v1"
                                    ),
                                    "mechanism_type": (
                                        "downstream_materialization"
                                    ),
                                    "source_ref": request.candidate_ref,
                                    "target_ref": "record:defect",
                                    "effect": (
                                        "materializes_upstream_defect"
                                    ),
                                },
                                "request_identity": (
                                    factor_role_request_identity(request)
                                ),
                            }
                        )
                        payload["counterfactual"][
                            "intervention_ref"
                        ] = request.candidate_ref
                    payload["counterfactual"][
                        "predicted_effect"
                    ] = predicted_effect
                    result = parse_factor_role_judgment(
                        payload,
                        request=request,
                    )
                    self.assertEqual(
                        result.necessity_status, row["necessity_status"]
                    )
                    self.assertEqual(result.factor_role, row["factor_role"])
                    self.assertEqual(
                        dict(result.factor_mechanism).get("mechanism_type"),
                        row["factor_mechanism"].get("mechanism_type"),
                    )

        for index, row in enumerate(prompt["role_contract"]):
            other = next(
                candidate
                for candidate in prompt["role_contract"]
                if not set(candidate["predicted_effects"]).intersection(
                    row["predicted_effects"]
                )
            )
            payload = _factor_payload(
                necessity_status=row["necessity_status"],
                factor_role=row["factor_role"],
            )
            payload["counterfactual"]["predicted_effect"] = other[
                "predicted_effects"
            ][0]
            with self.subTest(
                cross_row=(
                    index,
                    prompt["role_contract"].index(other),
                )
            ), self.assertRaises(
                ValueError
            ):
                parse_factor_role_judgment(
                    payload,
                    request=_request(),
                )

    def test_parser_rejects_foreign_identity_and_grounding_bindings(self) -> None:
        mutations = (
            ("candidate_ref", "record:foreign", "candidate_ref must match request"),
            (
                "request_identity",
                "factor-role-request:v1:foreign",
                "request_identity must match request",
            ),
            ("hypothesis_id", "hypothesis:foreign", "hypothesis_id must match request"),
            (
                "hypothesis_semantic_hash",
                "sha256:foreign",
                "hypothesis_semantic_hash must match request",
            ),
            (
                "defect_fingerprint",
                "sha256:foreign",
                "defect_fingerprint must match request",
            ),
            (
                "seed_binding_identity",
                "seed:foreign",
                "seed_binding_identity must match request",
            ),
            (
                "analysis_perspective",
                "Different analysis perspective.",
                "analysis_perspective must match request",
            ),
        )
        for field, replacement, error in mutations:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                payload = _factor_payload()
                payload[field] = replacement
                parse_factor_role_judgment(payload, request=_request())

    def test_parser_rejects_refs_outside_exact_request_fact_closure(self) -> None:
        for ref in ("record:foreign", "record:task"):
            with self.subTest(ref=ref), self.assertRaisesRegex(
                ValueError, "evidence_refs cite refs outside request"
            ):
                payload = _factor_payload()
                payload["evidence_refs"] = [ref]
                parse_factor_role_judgment(payload, request=_request())

    def test_parser_requires_non_empty_mechanism_target_to_be_downstream(
        self,
    ) -> None:
        upstream_ref = "record:upstream-context"
        request = _request(
            supporting_evidence=(
                {
                    "reference": _reference_envelope(upstream_ref),
                },
            ),
        )
        self.assertIn(upstream_ref, factor_role_request_fact_refs(request))
        non_path_allowed_ref = "record:root-evidence"
        self.assertIn(
            non_path_allowed_ref,
            factor_role_request_fact_refs(request),
        )
        for label, target_ref in (
            ("self_target", request.candidate_ref),
            ("upstream_target", upstream_ref),
            ("non_path_allowed_fact", non_path_allowed_ref),
        ):
            payload = _factor_payload()
            payload["request_identity"] = factor_role_request_identity(
                request
            )
            payload["factor_mechanism"]["target_ref"] = target_ref
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError,
                "target_ref must be a downstream recursive path ref",
            ):
                parse_factor_role_judgment(payload, request=request)

        payload = _factor_payload()
        payload["factor_mechanism"]["target_ref"] = request.recursive_path[1]
        parsed = parse_factor_role_judgment(payload, request=_request())
        self.assertEqual(
            parsed.factor_mechanism["target_ref"],
            request.recursive_path[1],
        )

    def test_parser_rejects_empty_or_whitespace_factor_reference_items(
        self,
    ) -> None:
        mutations = (
            ("evidence_refs", ["record:prompt", ""]),
            ("evidence_refs", ["record:prompt", "   "]),
            (
                "recursive_path",
                ["record:prompt", "", "record:decision", "record:defect"],
            ),
            (
                "recursive_path",
                ["record:prompt", " \t", "record:decision", "record:defect"],
            ),
        )
        for field, value in mutations:
            payload = _factor_payload()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                ValueError, "non-empty string references"
            ):
                parse_factor_role_judgment(payload, request=_request())

        payload = _factor_payload()
        payload["recursive_path"] = ["record:prompt", "record:foreign"]
        with self.assertRaisesRegex(ValueError, "recursive_path must match request"):
            parse_factor_role_judgment(payload, request=_request())

        for field in ("source_ref", "target_ref"):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "factor_mechanism {0} is outside request".format(field)
            ):
                payload = _factor_payload()
                payload["factor_mechanism"][field] = "record:foreign"
                parse_factor_role_judgment(payload, request=_request())

    def test_parser_does_not_promote_bare_nested_reference_strings(self) -> None:
        for field in ("candidate_reference", "supporting_evidence"):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "evidence_refs cite refs outside request"
            ):
                fact = {
                    "content": {
                        "foreign_ref": "record:fabricated",
                        "foreign_refs": ["record:also-fabricated"],
                    }
                }
                request = _request(
                    **{
                        field: (
                            fact
                            if field == "candidate_reference"
                            else (fact,)
                        )
                    }
                )
                payload = _factor_payload()
                payload["request_identity"] = factor_role_request_identity(
                    request
                )
                payload["evidence_refs"] = ["record:fabricated"]
                parse_factor_role_judgment(payload, request=request)

    def test_parser_accepts_only_resolved_provenance_envelope_refs(self) -> None:
        for provenance_class in ("recorded", "reconstructed", "inferred"):
            with self.subTest(provenance_class=provenance_class):
                request = _request(
                    supporting_evidence=(
                        {
                            "reference": _reference_envelope(
                                "record:resolved-evidence",
                                provenance_class=provenance_class,
                            )
                        },
                    )
                )
                payload = _factor_payload()
                payload["request_identity"] = factor_role_request_identity(
                    request
                )
                payload["evidence_refs"] = ["record:resolved-evidence"]
                result = parse_factor_role_judgment(
                    payload, request=request
                )
                self.assertEqual(
                    result.evidence_refs, ("record:resolved-evidence",)
                )

    def test_parser_rejects_incomplete_contradictory_or_unauditable_envelopes(
        self,
    ) -> None:
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
                "raw_ref": "record:missing-resolved",
                "resolved_ref": "",
                "resolution_status": "resolved",
                "provenance_class": "recorded",
            },
            {
                "raw_ref": "record:inferred-without-metadata",
                "resolved_ref": "record:inferred-without-metadata",
                "resolution_status": "resolved",
                "provenance_class": "inferred",
            },
            {
                "raw_ref": "record:inferred-contradiction",
                "resolved_ref": "record:inferred-contradiction",
                "resolution_status": "resolved",
                "provenance_class": "inferred",
                "inference_metadata": {
                    "evidence_type": "recorded",
                    "inference_method": "semantic_similarity_v1",
                },
            },
        )
        for envelope in invalid_envelopes:
            cited_ref = str(envelope["resolved_ref"])
            if not cited_ref:
                cited_ref = str(envelope["raw_ref"])
            request = _request(
                supporting_evidence=({"reference": envelope},)
            )
            payload = _factor_payload()
            payload["request_identity"] = factor_role_request_identity(request)
            payload["evidence_refs"] = [cited_ref]
            with self.subTest(envelope=envelope), self.assertRaisesRegex(
                ValueError, "evidence_refs cite refs outside request"
            ):
                parse_factor_role_judgment(payload, request=request)

    def test_parser_rejects_non_exact_envelope_key_and_value_types(self) -> None:
        valid = _reference_envelope("record:strict-envelope")
        invalid_envelopes = (
            {**valid, "raw_ref": 7},
            {**valid, "resolved_ref": True},
            {**valid, "resolution_status": ["resolved"]},
            {**valid, "resolution_status": "Resolved"},
            {**valid, "provenance_class": _StringSubclass("recorded")},
            {
                **_reference_envelope(
                    "record:strict-envelope",
                    provenance_class="inferred",
                ),
                "inference_metadata": {
                    "evidence_type": 1,
                    "inference_method": "semantic_similarity_v1",
                },
            },
            {
                **_reference_envelope(
                    "record:strict-envelope",
                    provenance_class="inferred",
                ),
                "inference_metadata": {
                    "evidence_type": "semantic_inferred",
                    "inference_method": _StringSubclass(
                        "semantic_similarity_v1"
                    ),
                },
            },
        )
        for envelope in invalid_envelopes:
            request = _request(
                supporting_evidence=({"reference": envelope},)
            )
            payload = _factor_payload()
            payload["request_identity"] = factor_role_request_identity(request)
            payload["evidence_refs"] = ["record:strict-envelope"]
            with self.subTest(envelope=envelope), self.assertRaisesRegex(
                ValueError, "evidence_refs cite refs outside request"
            ):
                parse_factor_role_judgment(payload, request=request)

    def test_parser_accepts_refs_from_validated_confirmed_root_summary(self) -> None:
        summary = _confirmed_root_summary(
            candidate_ref="record:confirmed-root",
            evidence_refs=["record:confirmed-root-evidence"],
            recursive_path=[
                "record:confirmed-root",
                "record:confirmed-root-path",
            ],
        )
        request = _request(confirmed_root_summaries=(summary,))
        for ref in (
            "record:confirmed-root",
            "record:confirmed-root-evidence",
            "record:confirmed-root-path",
        ):
            payload = _factor_payload()
            payload["request_identity"] = factor_role_request_identity(request)
            payload["evidence_refs"] = [ref]
            with self.subTest(ref=ref):
                result = parse_factor_role_judgment(payload, request=request)
                self.assertEqual(result.evidence_refs, (ref,))

    def test_parser_rejects_role_and_counterfactual_inconsistency(self) -> None:
        payload = _factor_payload()
        payload["factor_mechanism"]["mechanism_type"] = "amplification"
        with self.assertRaisesRegex(
            ValueError, "factor_mechanism type contradicts factor_role"
        ):
            parse_factor_role_judgment(payload, request=_request())

        payload = _factor_payload()
        payload["necessity_status"] = "necessary"
        with self.assertRaisesRegex(
            ValueError, "role contract"
        ):
            parse_factor_role_judgment(payload, request=_request())

        payload = _factor_payload()
        payload["counterfactual"]["predicted_effect"] = "prevents_defect"
        with self.assertRaisesRegex(
            ValueError, "factor role counterfactual effect contradicts role"
        ):
            parse_factor_role_judgment(payload, request=_request())


class ActiveFailureStructuredSignatureTest(unittest.TestCase):
    @staticmethod
    def _active_signature(state: RecursiveAnalysisState) -> dict[str, object]:
        signatures = {
            stable_json(
                item["factual_request_projection"]["facts"][
                    "factual_context"
                ]["failure_signature"]
            )
            for item in state.confirmation_queue
        }
        if len(signatures) != 1:
            raise AssertionError("fixture did not produce one active signature")
        return json.loads(next(iter(signatures)))

    def test_structured_signatures_sharing_one_defect_projection_remain_distinct_through_restore(
        self,
    ) -> None:
        observed = []
        for symbol in ("repository.check.alpha", "repository.check.beta"):
            with tempfile.TemporaryDirectory() as tempdir:
                trace, signature_id = _structured_lifecycle_trace(symbol)
                config = _lifecycle_checkpoint_config(trace)
                checkpoint_root = Path(tempdir) / "structured-signature.checkpoint"
                bundle = CheckpointBundle(checkpoint_root)
                bundle.initialize(config)
                state = _lifecycle_state(
                    trace=trace,
                    checkpoint=bundle,
                    checkpoint_config=config,
                )
                active_signature = self._active_signature(state)
                defect_fingerprint = next(iter(state.defect_states))
                self.assertEqual(
                    active_signature.get("signature_id"),
                    signature_id,
                )
                self.assertEqual(
                    active_signature["fingerprint"], defect_fingerprint
                )
                self.assertEqual(
                    active_signature["identity_source"],
                    "structured_failure_signature",
                )

                judge = _LifecycleJudge()
                analyzer = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=2,
                    checkpoint=bundle,
                    checkpoint_config=config,
                )
                analyzer._confirm_queued_roots(state)
                analyzer._checkpoint_state(
                    state,
                    "test:structured-active-failure-terminal",
                )
                bundle.flush_all()
                report = state.build_report(
                    judge=judge,
                    fusion_mode="retrieval-global",
                )
                report_payload = report.to_dict()
                self.assertEqual(
                    RecursiveAttributionReport.from_dict(
                        report_payload
                    ).to_dict(),
                    report_payload,
                )
                restored = RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=bundle.restore(expected_config=config),
                )
                report_binding = report.confirmed_roots[0].provenance[
                    "active_role_binding"
                ]
                restored_binding = restored.confirmed_roots[0].provenance[
                    "active_role_binding"
                ]
                self.assertEqual(
                    report_binding["failure_signature"], signature_id
                )
                self.assertEqual(
                    report_binding["defect_fingerprint"], defect_fingerprint
                )
                self.assertEqual(restored_binding, report_binding)
                observed.append(
                    (
                        active_signature["signature_id"],
                        active_signature["fingerprint"],
                        report_binding["failure_signature"],
                    )
                )

        self.assertNotEqual(observed[0][0], observed[1][0])
        self.assertEqual(observed[0][1], observed[1][1])
        self.assertNotEqual(observed[0][2], observed[1][2])

    def test_structured_external_evaluation_seed_uses_failure_semantics_not_transport_label(
        self,
    ) -> None:
        trace, _ = _structured_lifecycle_trace("repository.check")
        state = _lifecycle_state(trace=trace)

        self.assertEqual(
            self._active_signature(state)["kind"],
            "functional",
        )

    def test_legacy_seed_keeps_deterministic_defect_fingerprint_fallback(
        self,
    ) -> None:
        first = self._active_signature(_lifecycle_state())
        second = self._active_signature(_lifecycle_state())

        self.assertEqual(first, second)
        self.assertEqual(first.get("signature_id"), first["fingerprint"])
        self.assertEqual(
            first.get("identity_source"),
            "legacy_defect_state",
        )

    def test_coordinated_report_and_checkpoint_binding_rewrite_cannot_escape_owning_request_signature(
        self,
    ) -> None:
        forged_signature = "sha256:coordinated-foreign-signature"
        with tempfile.TemporaryDirectory() as tempdir:
            report, checkpoint, graph = _persist_publication_report(
                Path(tempdir) / "coordinated-signature.checkpoint",
                factor_role="contributing_condition",
            )
            payload = report.to_dict()
            factor_action = payload["metadata"][
                "factor_role_action_projections"
            ][0]
            original_factor_signature = factor_action[
                "request_projection"
            ]["facts"]["factual_context"]["failure_signature"][
                "signature_id"
            ]
            root_action = next(
                item
                for item in payload["metadata"][
                    "confirmation_action_projection"
                ]
                if item["confirmation"]["status"] == "confirmed"
            )
            original_root_signature = root_action[
                "factual_request_projection"
            ][
                "facts"
            ]["factual_context"]["failure_signature"]["signature_id"]
            factor_count, root_count = (
                _rewrite_all_active_failure_signatures(
                    payload,
                    forged_signature=forged_signature,
                )
            )
            self.assertEqual((factor_count, root_count), (3, 1))
            self.assertEqual(
                factor_action["request_projection"]["facts"][
                    "factual_context"
                ]["failure_signature"]["signature_id"],
                original_factor_signature,
            )
            self.assertEqual(
                root_action["factual_request_projection"]["facts"][
                    "factual_context"
                ]["failure_signature"]["signature_id"],
                original_root_signature,
            )
            with self.assertRaisesRegex(
                ValueError, "failure signature|request"
            ):
                RecursiveAttributionReport.from_dict(payload)

            actions = copy.deepcopy(list(checkpoint.actions))
            factor_count, root_count = (
                _rewrite_all_active_failure_signatures(
                    actions,
                    forged_signature=forged_signature,
                )
            )
            self.assertEqual((factor_count, root_count), (3, 2))
            with self.assertRaisesRegex(
                ValueError, "failure signature|request"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=graph,
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_contextless_functional_request_rejects_coordinated_kind_role_binding_rewrite(
        self,
    ) -> None:
        variants = (
            ("acceptance", "false_closure"),
            ("verification", "verification_omission"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            report, _, _ = _contextless_functional_publication(
                Path(tempdir) / "central-binding.checkpoint"
            )
            root = report.confirmed_roots[0]
            request_projection = report.metadata[
                "confirmation_action_projection"
            ][0]["factual_request_projection"]
            self.assertNotIn(
                "factual_context",
                request_projection["facts"],
            )
            original = root.to_dict()["provenance"]["active_role_binding"]
            self.assertEqual(original["failure_kind"], "functional")
            for failure_kind, causal_role in variants:
                with self.subTest(
                    failure_kind=failure_kind,
                    causal_role=causal_role,
                ), self.assertRaisesRegex(
                    ValueError, "failure signature|owning request"
                ):
                    forged = copy.deepcopy(original)
                    forged["failure_kind"] = failure_kind
                    forged["causal_role"] = causal_role
                    causal_state_module.canonical_active_failure_role_request_binding(
                        active_role_binding=forged,
                        request_projection=request_projection,
                        disposition="root",
                        causal_role=causal_role,
                    )

    def test_report_rejects_contextless_functional_coordinated_kind_role_rewrite(
        self,
    ) -> None:
        variants = (
            ("acceptance", "false_closure"),
            ("verification", "verification_omission"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            report, _, _ = _contextless_functional_publication(
                Path(tempdir) / "report-binding.checkpoint"
            )
            for failure_kind, causal_role in variants:
                with self.subTest(
                    failure_kind=failure_kind,
                    causal_role=causal_role,
                ), self.assertRaisesRegex(
                    ValueError, "failure signature|owning request"
                ):
                    payload = report.to_dict()
                    self.assertGreater(
                        _rewrite_all_active_failure_roles(
                            payload,
                            failure_kind=failure_kind,
                            causal_role=causal_role,
                        ),
                        0,
                    )
                    RecursiveAttributionReport.from_dict(payload)

    def test_checkpoint_rejects_contextless_functional_coordinated_kind_role_rewrite(
        self,
    ) -> None:
        variants = (
            ("acceptance", "false_closure"),
            ("verification", "verification_omission"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint, graph = _contextless_functional_publication(
                Path(tempdir) / "checkpoint-binding.checkpoint"
            )
            for failure_kind, causal_role in variants:
                with self.subTest(
                    failure_kind=failure_kind,
                    causal_role=causal_role,
                ), self.assertRaisesRegex(
                    ValueError, "failure signature|owning request"
                ):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    self.assertGreater(
                        _rewrite_all_active_failure_roles(
                            actions,
                            failure_kind=failure_kind,
                            causal_role=causal_role,
                        ),
                        0,
                    )
                    RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=replace(
                            checkpoint,
                            actions=tuple(actions),
                        ),
                    )

    def test_present_but_malformed_structured_signature_fails_closed_without_legacy_fallback(
        self,
    ) -> None:
        mutations = {}
        extra_trace, _ = _structured_lifecycle_trace(
            "repository.check.extra"
        )
        extra_signature = next(
            item
            for item in extra_trace["records"]
            if item["record_id"] == "defect"
        )["data"]["failure_signature"]
        extra_signature["unexpected_verdict"] = "forbidden"
        mutations["extra field"] = extra_trace

        missing_trace, _ = _structured_lifecycle_trace(
            "repository.check.missing"
        )
        missing_signature = next(
            item
            for item in missing_trace["records"]
            if item["record_id"] == "defect"
        )["data"]["failure_signature"]
        del missing_signature["subsystem"]
        mutations["missing field"] = missing_trace

        noncanonical_trace, _ = _structured_lifecycle_trace(
            "repository.check.noncanonical"
        )
        noncanonical_signature = next(
            item
            for item in noncanonical_trace["records"]
            if item["record_id"] == "defect"
        )["data"]["failure_signature"]
        noncanonical_signature["signature_id"] = "sha256:forged"
        mutations["noncanonical identity"] = noncanonical_trace

        for label, trace in mutations.items():
            with self.subTest(label=label), self.assertRaisesRegex(
                ValueError, "structured failure signature"
            ):
                _lifecycle_state(trace=trace)


class FactorRoleLifecycleTest(unittest.TestCase):
    def test_terminal_schema_rejects_reviewer_impossible_probes(self) -> None:
        probes = (
            {
                "operation": "factor_role_completed",
                "physical_requests_reserved": 1,
                "physical_request_delta": 0,
                "physical_request_exact": False,
                "failure_classification": "none",
                "unknown_judgment": False,
            },
            {
                "operation": "factor_role_failed",
                "physical_requests_reserved": 1,
                "physical_request_delta": 1,
                "physical_request_exact": True,
                "failure_classification": "interrupted",
                "unknown_judgment": True,
            },
        )
        for probe in probes:
            with self.subTest(probe=probe), self.assertRaisesRegex(
                ValueError,
                "accounting or classification",
            ):
                _terminal_projection_probe(**probe)

    def test_terminal_schema_accepts_all_reachable_state_combinations(
        self,
    ) -> None:
        combinations = (
            ("factor_role_completed", 0, 0, True, "none", False),
            ("factor_role_completed", 1, 0, True, "none", False),
            ("factor_role_completed", 1, 1, True, "none", False),
            ("factor_role_failed", 1, 0, False, "interrupted", True),
            (
                "factor_role_failed",
                1,
                2,
                True,
                "accounting_breach",
                True,
            ),
            (
                "factor_role_failed",
                1,
                0,
                True,
                "bounded_provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "bounded_provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                1,
                0,
                True,
                "judgment_invalid",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "judgment_invalid",
                True,
            ),
            (
                "factor_role_failed",
                0,
                0,
                True,
                "provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                False,
                "provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                0,
                0,
                True,
                "capability_error",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                False,
                "capability_error",
                True,
            ),
        )
        for (
            operation,
            reserved,
            delta,
            exact,
            classification,
            unknown,
        ) in combinations:
            with self.subTest(
                operation=operation,
                reserved=reserved,
                delta=delta,
                exact=exact,
                classification=classification,
            ):
                projection = _terminal_projection_probe(
                    operation=operation,
                    physical_requests_reserved=reserved,
                    physical_request_delta=delta,
                    physical_request_exact=exact,
                    failure_classification=classification,
                    unknown_judgment=unknown,
                )
                self.assertEqual(
                    projection["failure_classification"],
                    classification,
                )

    def test_terminal_schema_rejects_unreachable_state_combinations(
        self,
    ) -> None:
        combinations = (
            ("factor_role_failed", 1, 1, False, "interrupted", True),
            (
                "factor_role_failed",
                1,
                1,
                False,
                "bounded_provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                False,
                "judgment_invalid",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                2,
                1,
                False,
                "provider_failure",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "capability_error",
                True,
            ),
            (
                "factor_role_failed",
                2,
                1,
                False,
                "capability_error",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "request_ineligible",
                True,
            ),
            (
                "factor_role_failed",
                1,
                1,
                True,
                "bounded_provider_failure",
                False,
            ),
        )
        for (
            operation,
            reserved,
            delta,
            exact,
            classification,
            unknown,
        ) in combinations:
            with self.subTest(
                operation=operation,
                reserved=reserved,
                delta=delta,
                exact=exact,
                classification=classification,
            ), self.assertRaisesRegex(
                ValueError,
                "accounting or classification",
            ):
                _terminal_projection_probe(
                    operation=operation,
                    physical_requests_reserved=reserved,
                    physical_request_delta=delta,
                    physical_request_exact=exact,
                    failure_classification=classification,
                    unknown_judgment=unknown,
                )

    def test_factor_allowance_breach_is_fatal_and_persists_exact_terminal(
        self,
    ) -> None:
        accounting_error = getattr(
            recursive_analyzer_module,
            "FactorRoleAccountingError",
            RuntimeError,
        )
        for judge in (
            _OverBudgetResultLifecycleJudge(),
            _OverBudgetErrorLifecycleJudge(),
        ):
            state = _lifecycle_state()
            with self.subTest(judge=type(judge).__name__):
                with self.assertRaises(accounting_error) as raised:
                    AgenticRecursiveAnalyzer(
                        judge=judge,
                        max_judge_requests=2,
                    )._confirm_queued_roots(state)
                self.assertEqual(raised.exception.reported_requests, 2)
                self.assertEqual(raised.exception.allowed_requests, 1)
                self.assertEqual(state.judge_requests, 3)
                self.assertEqual(len(state.factor_role_judgments), 1)
                self.assertEqual(
                    state.factor_role_judgments[0].factor_role,
                    "unknown",
                )
                projection = state.factor_role_action_projection[0]
                self.assertEqual(projection["operation"], "factor_role_failed")
                self.assertEqual(
                    projection["failure_classification"],
                    "accounting_breach",
                )
                self.assertEqual(projection["physical_requests_reserved"], 1)
                self.assertEqual(projection["physical_request_delta"], 2)
                self.assertTrue(projection["physical_request_exact"])
                self.assertEqual(state.confirmation_queue[1]["status"], "failed")

    def test_factor_allowance_breach_replays_exact_terminal_without_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-over-budget.checkpoint"
            config = _lifecycle_checkpoint_config()
            bundle = CheckpointBundle(checkpoint_root)
            bundle.initialize(config)
            state = _lifecycle_state(
                checkpoint=bundle,
                checkpoint_config=config,
            )
            with self.assertRaises(
                recursive_analyzer_module.FactorRoleAccountingError
            ):
                AgenticRecursiveAnalyzer(
                    judge=_OverBudgetResultLifecycleJudge(),
                    fusion_mode="retrieval-global",
                    max_judge_requests=2,
                    checkpoint=bundle,
                    checkpoint_config=config,
                )._confirm_queued_roots(state)
            bundle.flush_all()
            checkpoint = bundle.restore(expected_config=config)
            terminal = next(
                item
                for item in checkpoint.actions
                if item["operation"] == "factor_role_failed"
            )
            projection = terminal["payload"]["action_projection"]
            self.assertEqual(
                projection["failure_classification"],
                "accounting_breach",
            )
            self.assertEqual(projection["physical_requests_reserved"], 1)
            self.assertEqual(projection["physical_request_delta"], 2)
            self.assertTrue(projection["physical_request_exact"])

            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(_lifecycle_trace()),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=2,
                checkpoint_config=config,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        self.assertEqual(restored.judge_requests, 3)
        self.assertEqual(restored.judge_request_uncertainty_count, 0)
        restored_projection = restored.factor_role_action_projection[0]
        self.assertEqual(
            restored_projection["failure_classification"],
            "accounting_breach",
        )
        self.assertEqual(
            restored_projection["physical_requests_reserved"],
            1,
        )
        self.assertEqual(
            restored_projection["physical_request_delta"],
            2,
        )
        self.assertTrue(restored_projection["physical_request_exact"])
        self.assertEqual(
            restored.factor_role_judgments[0].factor_role,
            "unknown",
        )
        self.assertEqual(
            next(
                item
                for item in restored.confirmation_queue
                if item["review_scope"] == "non_root"
            )["status"],
            "failed",
        )
        self.assertNotEqual(
            restored_projection["failure_classification"],
            "interrupted",
        )

    def test_invalid_factor_binding_preserves_known_exact_physical_delta(
        self,
    ) -> None:
        state = _lifecycle_state()

        AgenticRecursiveAnalyzer(
            judge=_InvalidBindingLifecycleJudge(),
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        projection = state.factor_role_action_projection[0]
        self.assertEqual(projection["operation"], "factor_role_failed")
        self.assertEqual(projection["physical_request_delta"], 1)
        self.assertTrue(projection["physical_request_exact"])
        self.assertEqual(
            projection["failure_classification"],
            "judgment_invalid",
        )
        self.assertEqual(
            state.factor_role_judgments[0].factor_role,
            "unknown",
        )
        self.assertEqual(state.judge_requests, 2)

    def test_factor_action_boundary_rejects_refs_outside_request_closure(
        self,
    ) -> None:
        state = _lifecycle_state()

        AgenticRecursiveAnalyzer(
            judge=_OutOfClosureLifecycleJudge(),
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        projection = state.factor_role_action_projection[0]
        judgment = state.factor_role_judgments[0]
        self.assertEqual(projection["operation"], "factor_role_failed")
        self.assertEqual(
            projection["failure_classification"],
            "judgment_invalid",
        )
        self.assertEqual(projection["physical_request_delta"], 1)
        self.assertTrue(projection["physical_request_exact"])
        self.assertEqual(judgment.factor_role, "unknown")
        self.assertNotIn("record:foreign", judgment.evidence_refs)

    def test_factor_action_boundary_rejects_self_target_as_failed(
        self,
    ) -> None:
        state = _lifecycle_state()

        AgenticRecursiveAnalyzer(
            judge=_SelfTargetLifecycleJudge(),
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        projection = state.factor_role_action_projection[0]
        self.assertEqual(projection["operation"], "factor_role_failed")
        self.assertEqual(
            projection["failure_classification"],
            "judgment_invalid",
        )
        self.assertEqual(projection["physical_request_delta"], 1)
        self.assertTrue(projection["physical_request_exact"])
        self.assertEqual(
            state.factor_role_judgments[0].factor_role,
            "unknown",
        )

    def test_factor_dispatch_requires_canonical_global_assessment_origin(
        self,
    ) -> None:
        state = _lifecycle_state()
        factor_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "non_root"
        )
        factor_queue["origin"] = "forged_origin"

        with self.assertRaisesRegex(
            ValueError,
            "canonical Global factor assessment",
        ):
            AgenticRecursiveAnalyzer(
                judge=_LifecycleJudge(),
                max_judge_requests=2,
            )._confirm_queued_roots(state)

    def test_factor_dispatch_requires_reviewable_nonselected_global_assessment(
        self,
    ) -> None:
        state = _lifecycle_state()
        builder = next(iter(state.seed_ledger.values()))
        factor_assessment = next(
            item
            for item in builder.global_judgment["assessments"]
            if item["candidate_ref"] == "record:factor"
        )
        factor_assessment["causal_role"] = "unknown"
        completed = next(
            item
            for item in state.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "completed"
        )
        next(
            item
            for item in completed["judgment"]["assessments"]
            if item["candidate_ref"] == "record:factor"
        )["causal_role"] = "unknown"

        with self.assertRaisesRegex(
            ValueError,
            "reviewable non-selected Global assessment",
        ):
            AgenticRecursiveAnalyzer(
                judge=_LifecycleJudge(),
                max_judge_requests=2,
            )._confirm_queued_roots(state)

    def test_coordinated_factor_path_forgery_is_rejected_after_identity_refresh(
        self,
    ) -> None:
        for boundary in ("dispatch", "report"):
            with self.subTest(boundary=boundary):
                state = _lifecycle_state()
                factor_queue = next(
                    item
                    for item in state.confirmation_queue
                    if item["review_scope"] == "non_root"
                )
                original_identity = factor_queue["semantic_identity"]
                factor_queue["recursive_path"] = [
                    "record:factor",
                    "record:root",
                    "record:defect",
                ]
                state.refresh_pending_confirmation_request_identities()
                self.assertNotEqual(
                    factor_queue["semantic_identity"],
                    original_identity,
                )
                self.assertEqual(
                    factor_queue["factual_request_projection"]["facts"][
                        "recursive_path"
                    ],
                    factor_queue["recursive_path"],
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "canonical Global factor request facts|grounded causal edge",
                ):
                    if boundary == "dispatch":
                        AgenticRecursiveAnalyzer(
                            judge=_LifecycleJudge(),
                            max_judge_requests=2,
                        )._confirm_queued_roots(state)
                    else:
                        state.build_report(
                            judge=_LifecycleJudge(),
                            fusion_mode="retrieval-global",
                        )

    def test_coordinated_factor_opposition_forgery_is_rejected_after_rekey(
        self,
    ) -> None:
        for boundary in ("dispatch", "report"):
            with self.subTest(boundary=boundary):
                state = _lifecycle_state()
                factor_queue = _inject_coordinated_factor_opposition(state)
                opposing_refs = [
                    item["resolved_ref"]
                    for item in factor_queue[
                        "factual_request_projection"
                    ]["facts"]["opposing_evidence"]
                ]
                self.assertEqual(opposing_refs, ["record:root"])

                with self.assertRaisesRegex(
                    ValueError,
                    "canonical Global factor request facts",
                ):
                    if boundary == "dispatch":
                        AgenticRecursiveAnalyzer(
                            judge=_LifecycleJudge(),
                            max_judge_requests=2,
                        )._confirm_queued_roots(state)
                    else:
                        state.build_report(
                            judge=_LifecycleJudge(),
                            fusion_mode="retrieval-global",
                        )

    def test_checkpoint_restore_rejects_rekeyed_factor_opposition_forgery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = _lifecycle_checkpoint_config()
            bundle = CheckpointBundle(
                Path(tempdir) / "factor-opposition-forgery.checkpoint"
            )
            bundle.initialize(config)
            state = _lifecycle_state(
                checkpoint=bundle,
                checkpoint_config=config,
            )
            _inject_coordinated_factor_opposition(state)
            bundle.flush_all()
            checkpoint = bundle.restore(expected_config=config)
            actions = copy.deepcopy(list(checkpoint.actions))
            action_snapshot = next(
                item
                for item in reversed(actions)
                if item["operation"] == "state_snapshot"
            )
            action_snapshot["payload"]["introduction_bindings"] = (
                copy.deepcopy(state.introduction_bindings)
            )
            action_snapshot["payload"]["confirmation_queue"] = (
                copy.deepcopy(state.confirmation_queue)
            )
            action_snapshot["payload"]["confirmation_queue_keys"] = [
                list(item)
                for item in sorted(state.confirmation_queue_keys)
            ]
            action_snapshot["payload"]["hypothesis_seed_keys"] = dict(
                state.hypothesis_seed_keys
            )
            hypothesis_records = copy.deepcopy(
                list(checkpoint.hypothesis_records)
            )
            hypothesis_records[-1]["payload"] = (
                state.hypothesis_checkpoint_payload()
            )

            with self.assertRaisesRegex(
                ValueError,
                "canonical Global factor request facts",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                        hypothesis_records=tuple(hypothesis_records),
                    ),
                )

    def test_checkpoint_restore_rejects_identity_refreshed_factor_path_forgery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = _lifecycle_checkpoint_config()
            bundle = CheckpointBundle(
                Path(tempdir) / "factor-path-forgery.checkpoint"
            )
            bundle.initialize(config)
            state = _lifecycle_state(
                checkpoint=bundle,
                checkpoint_config=config,
            )
            factor_queue = next(
                item
                for item in state.confirmation_queue
                if item["review_scope"] == "non_root"
            )
            factor_queue["recursive_path"] = [
                "record:factor",
                "record:root",
                "record:defect",
            ]
            state.refresh_pending_confirmation_request_identities()
            bundle.flush_all()
            checkpoint = bundle.restore(expected_config=config)
            actions = copy.deepcopy(list(checkpoint.actions))
            snapshot = next(
                item
                for item in reversed(actions)
                if item["operation"] == "state_snapshot"
            )
            snapshot["payload"]["confirmation_queue"] = copy.deepcopy(
                state.confirmation_queue
            )

            with self.assertRaisesRegex(
                ValueError,
                "canonical Global factor request facts",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_checkpoint_rejects_coordinated_factor_origin_forgery(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-role.checkpoint"
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            for action in actions:
                if action["operation"] == "state_snapshot":
                    payload = action["payload"]
                    for queued in payload["confirmation_queue"]:
                        if queued["review_scope"] == "non_root":
                            queued["origin"] = "forged_origin"
                    for journal in payload["factor_role_journal"]:
                        journal["origin"] = "forged_origin"
                    for projection in payload[
                        "factor_role_action_projection"
                    ]:
                        projection["origin"] = "forged_origin"
                elif action["operation"] == "factor_role_started":
                    action["payload"]["origin"] = "forged_origin"
                elif action["operation"] in {
                    "factor_role_completed",
                    "factor_role_failed",
                }:
                    action["payload"]["action_projection"][
                        "origin"
                    ] = "forged_origin"

            with self.assertRaisesRegex(
                ValueError,
                "canonical Global factor assessment",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_v15_requires_all_factor_audit_metadata(self) -> None:
        state = _lifecycle_state()
        judge = _LifecycleJudge()
        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=2,
        )._confirm_queued_roots(state)
        payload = state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        ).to_dict()
        required = {
            "factor_role_judgments",
            "factor_role_journal",
            "factor_role_action_projections",
            "factor_role_gaps",
        }
        self.assertTrue(required.issubset(payload["metadata"]))
        self.assertNotIn(
            "factor_role_action_projection",
            payload["metadata"],
        )

        removals = [{key} for key in sorted(required)]
        removals.append(required)
        for removed in removals:
            forged = copy.deepcopy(payload)
            for key in removed:
                forged["metadata"].pop(key)
            with self.subTest(removed=sorted(removed)):
                with self.assertRaisesRegex(
                    ValueError,
                    "modern report metadata",
                ):
                    validate_modern_report_shape(forged)
                with self.assertRaisesRegex(
                    ValueError,
                    "modern report metadata",
                ):
                    RecursiveAttributionReport.from_dict(forged)

    def test_every_non_root_candidate_on_the_final_page_is_reviewed(self) -> None:
        self.assertEqual(
            GLOBAL_NON_ROOT_REVIEW_ROLES,
            frozenset(
                {
                    "contributing_condition",
                    "amplifying_factor",
                    "outcome_evidence",
                    "unrelated",
                }
            ),
        )
        graph = TraceGraph.from_trace(_four_role_pool_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:defect"],
            objective="Find the root and independently review non-root roles.",
            analysis_perspective="Improve repository reasoning.",
            max_hypotheses=24,
        )
        AgenticRecursiveAnalyzer(
            judge=_FourRolePoolGlobalJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=4,
        )._run_global_candidate_prepass(state, graph)

        non_root_queue = [
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "non_root"
        ]
        self.assertEqual(
            [item["candidate_ref"] for item in non_root_queue],
            [
                "record:condition",
                "record:amplifier",
                "record:outcome",
                "record:unrelated",
            ],
        )
        self.assertTrue(
            all(
                item["origin"]
                == "global_candidate_factor_assessment"
                for item in non_root_queue
            )
        )

        factor_judge = _LifecycleJudge()
        AgenticRecursiveAnalyzer(
            judge=factor_judge,
            max_judge_requests=5,
        )._confirm_queued_roots(state)

        self.assertEqual(
            [request.candidate_ref for request in factor_judge.factor_requests],
            [
                "record:condition",
                "record:amplifier",
                "record:outcome",
                "record:unrelated",
            ],
        )
        outcome_request = next(
            request
            for request in factor_judge.factor_requests
            if request.candidate_ref == "record:outcome"
        )
        blind_request = stable_json(outcome_request.factual_dict())
        for leaked_label in (
            "root_candidate",
            "contributing_condition",
            "amplifying_factor",
            "outcome_evidence",
            "unrelated",
            "causal_role",
            "global_role",
            "review_scope",
        ):
            self.assertNotIn(leaked_label, blind_request)

    def test_root_and_non_root_dispatch_share_one_exact_physical_budget(self) -> None:
        state = _lifecycle_state()
        judge = _LifecycleJudge()

        AgenticRecursiveAnalyzer(
            judge=judge,
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        self.assertEqual(
            [request.candidate_ref for request in judge.confirmation_requests],
            ["record:root"],
        )
        self.assertEqual(
            [request.candidate_ref for request in judge.factor_requests],
            ["record:factor"],
        )
        self.assertEqual(
            [
                item["candidate_reference"]["resolved_ref"]
                for item in judge.confirmation_requests[
                    0
                ].competing_hypotheses
            ],
            ["record:factor"],
        )
        self.assertEqual(judge.allowances, [("root", 2), ("factor", 1)])
        self.assertEqual(state.judge_requests, 2)
        factor_request = judge.factor_requests[0]
        root_summary = dict(factor_request.confirmed_root_summaries[0])
        self.assertEqual(
            set(root_summary),
            {
                "schema",
                "candidate_ref",
                "hypothesis_id",
                "hypothesis_semantic_hash",
                "confirmation_identity",
                "defect_fingerprint",
                "seed_binding_identity",
                "reason",
                "evidence_refs",
                "recursive_path",
            },
        )
        judge_fact_text = stable_json(
            factor_role_request_projection(factor_request)["facts"]
        )
        for verdict_field in (
            "factor_role",
            "confirmation_status",
            "root_verdict",
            "factor_verdict",
        ):
            self.assertNotIn('"{0}"'.format(verdict_field), judge_fact_text)
        self.assertEqual(
            [
                summary["candidate_ref"]
                for summary in factor_request.confirmed_root_summaries
            ],
            ["record:root"],
        )
        request_text = stable_json(factor_request.factual_dict())
        self.assertNotIn("global_role", request_text)
        self.assertNotIn("causal_role", request_text)
        self.assertNotIn("contributing_condition", request_text)
        self.assertNotIn("review_scope", request_text)
        self.assertEqual(
            [
                judgment.candidate_ref
                for judgment in state.factor_role_judgments
            ],
            ["record:factor"],
        )
        root_queue, factor_queue = state.confirmation_queue
        self.assertEqual(root_queue["status"], "confirmed")
        self.assertEqual(factor_queue["status"], "completed")
        self.assertEqual(
            factor_queue["semantic_identity"],
            factor_queue["factor_role_judgment"]["request_identity"],
        )
        self.assertEqual(
            factor_queue["response_identity"],
            factor_queue["factor_role_judgment"]["judgment_identity"],
        )
        projection = state.factor_role_action_projection[0]
        self.assertEqual(
            set(projection),
            {
                "operation",
                "semantic_key",
                "owner",
                "origin",
                "candidate_ref",
                "hypothesis_id",
                "defect_fingerprint",
                "seed_binding_identity",
                "request_projection",
                "request_identity",
                "physical_requests_reserved",
                "physical_request_delta",
                "physical_request_exact",
                "judgment",
                "judgment_identity",
                "failure_classification",
                "queue_binding",
                "active_role_binding",
            },
        )
        self.assertEqual(
            projection["active_role_binding"]["causal_role"],
            "amplifying_condition",
        )
        self.assertEqual(projection["operation"], "factor_role_completed")
        self.assertEqual(projection["physical_request_delta"], 1)
        self.assertTrue(projection["physical_request_exact"])
        self.assertEqual(projection["failure_classification"], "none")

    def test_root_request_facts_do_not_depend_on_competitor_review_scope(
        self,
    ) -> None:
        state = _lifecycle_state()
        root_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "root"
        )
        factor_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "non_root"
        )
        with_non_root_competitor = (
            AgenticRecursiveAnalyzer._build_confirmation_request(
                state,
                root_queue,
            )
        )
        factor_queue["review_scope"] = "root"
        with_root_competitor = (
            AgenticRecursiveAnalyzer._build_confirmation_request(
                state,
                root_queue,
            )
        )

        self.assertEqual(
            with_non_root_competitor.factual_dict(),
            with_root_competitor.factual_dict(),
        )
        self.assertEqual(
            [
                item["candidate_reference"]["resolved_ref"]
                for item in with_non_root_competitor.competing_hypotheses
            ],
            ["record:factor"],
        )

    def test_factor_role_request_is_blind_to_global_role_and_root_competitors(
        self,
    ) -> None:
        state = _lifecycle_state()
        judge = _LifecycleJudge()
        AgenticRecursiveAnalyzer(
            judge=judge,
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        self.assertEqual(
            [root.node_ref for root in state.confirmed_roots],
            ["record:root"],
        )
        request = judge.factor_requests[0]
        facts = request.factual_dict()
        self.assertNotIn("competing_hypotheses", facts)
        self.assertEqual(
            [
                summary["candidate_ref"]
                for summary in request.confirmed_root_summaries
            ],
            ["record:root"],
        )
        request_text = stable_json(facts)
        for hidden_label in (
            "global_role",
            "causal_role",
            "contributing_condition",
            "review_scope",
        ):
            self.assertNotIn(hidden_label, request_text)

    def test_factor_provider_failure_is_terminal_unknown_with_a_gap(self) -> None:
        state = _lifecycle_state()
        judge = _LifecycleJudge(fail_factor=True)

        AgenticRecursiveAnalyzer(
            judge=judge,
            max_judge_requests=2,
        )._confirm_queued_roots(state)

        judgment = state.factor_role_judgments[0]
        self.assertEqual(judgment.necessity_status, "unknown")
        self.assertEqual(judgment.factor_role, "unknown")
        self.assertEqual(state.confirmation_queue[1]["status"], "failed")
        self.assertEqual(
            state.factor_role_action_projection[0]["operation"],
            "factor_role_failed",
        )
        self.assertEqual(
            state.factor_role_action_projection[0]["failure_classification"],
            "bounded_provider_failure",
        )
        self.assertEqual(
            [
                (
                    gap["candidate_ref"],
                    gap["judgment_identity"],
                    gap["failure_classification"],
                )
                for gap in state.factor_role_gaps
            ],
            [
                (
                    "record:factor",
                    judgment.judgment_identity,
                    "bounded_provider_failure",
                )
            ],
        )
        self.assertEqual(state.judge_requests, 2)

    def test_completed_factor_checkpoint_replays_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-role.checkpoint"
            config = _lifecycle_checkpoint_config()
            original, checkpoint = _persist_lifecycle_checkpoint(
                checkpoint_root,
                snapshot_terminal=False,
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(_lifecycle_trace()),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            replay_bundle = CheckpointBundle(checkpoint_root)
            replay_bundle.initialize(config)
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=2,
                checkpoint=replay_bundle,
                checkpoint_config=config,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        original_contexts = [
            item["factual_request_projection"]["facts"].get(
                "factual_context"
            )
            for item in original.confirmation_queue
        ]
        restored_contexts = [
            item["factual_request_projection"]["facts"].get(
                "factual_context"
            )
            for item in restored.confirmation_queue
        ]
        self.assertEqual(len(original_contexts), 2)
        self.assertTrue(
            all(isinstance(item, dict) for item in original_contexts)
        )
        self.assertEqual(restored_contexts, original_contexts)
        self.assertEqual(
            [item.to_dict() for item in restored.factor_role_judgments],
            [item.to_dict() for item in original.factor_role_judgments],
        )
        self.assertEqual(
            restored.factor_role_action_projection,
            original.factor_role_action_projection,
        )
        self.assertEqual(
            [
                (
                    item["semantic_identity"],
                    item["response_identity"],
                )
                for item in restored.confirmation_queue
                if item["review_scope"] == "non_root"
            ],
            [
                (
                    item["semantic_identity"],
                    item["response_identity"],
                )
                for item in original.confirmation_queue
                if item["review_scope"] == "non_root"
            ],
        )
        self.assertEqual(restored.judge_requests, 2)
        self.assertEqual(
            restored.judge_request_uncertainty_count,
            original.judge_request_uncertainty_count,
        )

    def test_post_snapshot_factor_terminal_requires_exactly_one_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-terminal-without-start.checkpoint",
                snapshot_terminal=False,
            )
            terminal = next(
                item
                for item in checkpoint.actions
                if item["operation"] == "factor_role_completed"
            )
            latest_snapshot = next(
                item
                for item in reversed(checkpoint.actions)
                if item["operation"] == "state_snapshot"
            )
            self.assertGreater(
                terminal["sequence"],
                latest_snapshot["sequence"],
            )
            without_start = replace(
                checkpoint,
                actions=tuple(
                    item
                    for item in checkpoint.actions
                    if item["operation"] != "factor_role_started"
                ),
            )

            with self.assertRaisesRegex(
                ValueError,
                "factor role lifecycle|matching started action|shared "
                "(Judge|budget)",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=without_start,
                )

    def test_post_snapshot_factor_terminal_accounting_must_match_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-terminal-accounting.checkpoint",
                snapshot_terminal=False,
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            terminal = next(
                item
                for item in actions
                if item["operation"] == "factor_role_completed"
            )
            terminal["payload"]["physical_requests_reserved"] = 2
            terminal["payload"]["physical_request_delta"] = 2
            terminal["payload"]["action_projection"][
                "physical_requests_reserved"
            ] = 2
            terminal["payload"]["action_projection"][
                "physical_request_delta"
            ] = 2

            with self.assertRaisesRegex(
                ValueError,
                "factor role lifecycle|matching started action|shared budget",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_factor_reservation_must_match_shared_budget_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-paired-accounting.checkpoint",
                snapshot_terminal=False,
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            started = next(
                item
                for item in actions
                if item["operation"] == "factor_role_started"
            )
            terminal = next(
                item
                for item in actions
                if item["operation"] == "factor_role_completed"
            )
            started["payload"]["physical_requests_reserved"] = 2
            terminal["payload"]["physical_requests_reserved"] = 2
            terminal["payload"]["physical_request_delta"] = 2
            terminal["payload"]["action_projection"][
                "physical_requests_reserved"
            ] = 2
            terminal["payload"]["action_projection"][
                "physical_request_delta"
            ] = 2

            with self.assertRaisesRegex(
                ValueError,
                "shared Judge budget|budget snapshot|provider accounting",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_shared_accounting_rejects_snapshot_rebase_plus_paired_factor_edit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-rebased-accounting.checkpoint",
                snapshot_terminal=False,
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            started_position = next(
                index
                for index, item in enumerate(actions)
                if item["operation"] == "factor_role_started"
            )
            factor_snapshot = actions[started_position - 1]
            forged_rebase = copy.deepcopy(factor_snapshot)
            forged_rebase["semantic_key"] = (
                "analysis:forged-accounting-rebase"
            )
            forged_rebase["payload"]["judge_requests"] = 0
            forged_rebase["payload"]["logical_judge_calls"] = 2
            accounting = forged_rebase["payload"]["provider_state"][
                "accounting"
            ]
            accounting["judge_requests"] = 0
            accounting["logical_judge_calls"] = 2
            _refresh_provider_state_identity(
                forged_rebase["payload"]["provider_state"]
            )
            actions.insert(started_position - 1, forged_rebase)

            started = next(
                item
                for item in actions
                if item["operation"] == "factor_role_started"
            )
            terminal = next(
                item
                for item in actions
                if item["operation"] == "factor_role_completed"
            )
            started["payload"]["physical_requests_reserved"] = 2
            terminal["payload"]["physical_requests_reserved"] = 2
            terminal["payload"]["physical_request_delta"] = 2
            terminal["payload"]["action_projection"][
                "physical_requests_reserved"
            ] = 2
            terminal["payload"]["action_projection"][
                "physical_request_delta"
            ] = 2

            with self.assertRaisesRegex(
                ValueError,
                "continuous|reconstructed provider accounting|shared Judge",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_failed_factor_checkpoint_replays_with_zero_provider_calls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-role-failed.checkpoint"
            config = _lifecycle_checkpoint_config()
            original, checkpoint = _persist_lifecycle_checkpoint(
                checkpoint_root,
                fail_factor=True,
                snapshot_terminal=False,
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(_lifecycle_trace()),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            replay_bundle = CheckpointBundle(checkpoint_root)
            replay_bundle.initialize(config)
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=2,
                checkpoint=replay_bundle,
                checkpoint_config=config,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        self.assertEqual(
            [item.to_dict() for item in restored.factor_role_judgments],
            [item.to_dict() for item in original.factor_role_judgments],
        )
        projection = restored.factor_role_action_projection[0]
        self.assertEqual(projection["operation"], "factor_role_failed")
        self.assertEqual(
            projection["failure_classification"],
            "bounded_provider_failure",
        )
        self.assertEqual(projection["physical_request_delta"], 1)
        self.assertTrue(projection["physical_request_exact"])
        self.assertEqual(restored.judge_requests, 2)
        self.assertEqual(restored.judge_request_uncertainty_count, 0)

    def test_failed_factor_terminal_rejects_necessary_unknown_judgment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-failed-necessary.checkpoint",
                fail_factor=True,
                snapshot_terminal=False,
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            terminal = next(
                item
                for item in actions
                if item["operation"] == "factor_role_failed"
            )
            projection = terminal["payload"]["action_projection"]
            request = (
                recursive_analyzer_module._factor_role_request_from_projection(
                    projection["request_projection"]
                )
            )
            forged = FactorRoleJudgment(
                candidate_ref=request.candidate_ref,
                necessity_status="necessary",
                factor_role="unknown",
                reason=(
                    "Coordinated failure forgery claims the factor is "
                    "necessary."
                ),
                confidence=0.91,
                evidence_refs=(request.candidate_ref,),
                recursive_path=request.recursive_path,
                factor_mechanism={},
                counterfactual={
                    "schema": "factor-role-counterfactual/v1",
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_effect": "prevents_defect",
                },
                hypothesis_id=request.hypothesis_id,
                hypothesis_semantic_hash=(
                    request.hypothesis_semantic_hash
                ),
                defect_fingerprint=request.defect_state.fingerprint,
                seed_binding_identity=request.seed_binding_identity,
                analysis_perspective=request.analysis_perspective,
                request_identity=factor_role_request_identity(request),
            )
            projection["judgment"] = forged.to_dict()
            projection["judgment_identity"] = forged.judgment_identity
            terminal["payload"]["judgment"] = forged.to_dict()

            with self.assertRaisesRegex(
                ValueError,
                "failed.*unknown|accounting or classification",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(_lifecycle_trace()),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_started_only_factor_replay_closes_interrupted_without_provider_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-role-started.checkpoint"
            _, checkpoint = _persist_lifecycle_checkpoint(
                checkpoint_root,
                snapshot_terminal=False,
            )
            started_only = replace(
                checkpoint,
                actions=tuple(
                    item
                    for item in checkpoint.actions
                    if item["operation"]
                    not in {"factor_role_completed", "factor_role_failed"}
                ),
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(_lifecycle_trace()),
                checkpoint=started_only,
            )
            self.assertEqual(restored.judge_requests, 2)
            self.assertEqual(restored.judge_request_uncertainty_count, 0)
            replay_judge = _ExplodingLifecycleJudge()

            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=2,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        self.assertEqual(restored.judge_requests, 2)
        self.assertEqual(restored.judge_request_uncertainty_count, 1)
        projection = restored.factor_role_action_projection[0]
        judgment = restored.factor_role_judgments[0]
        self.assertEqual(projection["operation"], "factor_role_failed")
        self.assertEqual(
            projection["failure_classification"],
            "interrupted",
        )
        self.assertEqual(projection["physical_requests_reserved"], 1)
        self.assertEqual(projection["physical_request_delta"], 0)
        self.assertFalse(projection["physical_request_exact"])
        self.assertEqual(judgment.factor_role, "unknown")
        self.assertEqual(
            next(
                item
                for item in restored.confirmation_queue
                if item["review_scope"] == "non_root"
            )["status"],
            "failed",
        )

    def test_completed_stale_factor_actions_are_quarantined_on_restore(
        self,
    ) -> None:
        trace = _lifecycle_trace()
        next(
            record
            for record in trace["records"]
            if record["record_id"] == "defect"
        )["data"]["repository_revision"] = 0
        trace["records"].append(
            {
                "record_id": "generation_zero",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 0,
                    "is_final_for_case": True,
                    "claim": "Generation zero result.",
                },
            }
        )
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "stale-factor.checkpoint",
                trace=trace,
            )
            active_trace = copy.deepcopy(trace)
            active_trace["records"].append(
                {
                    "record_id": "generation_one",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "repository_revision": 1,
                        "is_final_for_case": True,
                        "claim": "Generation one result.",
                    },
                }
            )

            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(active_trace),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                max_judge_requests=2,
            )._confirm_queued_roots(restored)

        self.assertEqual(
            restored.seed_results()[0].outcome,
            "evidence_gap",
        )
        self.assertEqual(restored.factor_role_judgments, [])
        self.assertEqual(restored.factor_role_journal, [])
        self.assertEqual(restored.factor_role_action_projection, [])
        self.assertEqual(restored.factor_role_gaps, [])
        self.assertFalse(
            any(
                semantic_key.startswith("factor_role:")
                for semantic_key in restored.replay_actions
            )
        )
        self.assertEqual(replay_judge.calls, 0)

    def test_stale_quarantine_rejects_outer_stale_inner_active_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "mixed-stale-mismatch.checkpoint",
                snapshot_terminal=False,
            )
            checkpoint, active_trace, stale_seed_key = (
                _with_mixed_stale_seed(checkpoint)
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            terminal = next(
                item
                for item in actions
                if item["operation"] == "factor_role_completed"
            )
            projection = terminal["payload"]["action_projection"]
            self.assertNotEqual(
                projection["request_projection"]["facts"][
                    "seed_binding_identity"
                ],
                stale_seed_key,
            )
            projection["seed_binding_identity"] = stale_seed_key

            with self.assertRaisesRegex(
                ValueError,
                "canonical|contradicts|inconsistent",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(active_trace),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(actions),
                    ),
                )

    def test_checkpoint_factor_surfaces_parse_before_stale_quarantine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "mixed-stale-factor-surfaces.checkpoint",
                fail_factor=True,
            )
            checkpoint, active_trace, stale_seed_key = (
                _with_mixed_stale_seed(checkpoint)
            )
            for surface in (
                "factor_role_journal",
                "factor_role_action_projection",
                "factor_role_judgments",
                "factor_role_gaps",
            ):
                with self.subTest(surface=surface):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    snapshot = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "state_snapshot"
                    )
                    _inject_malformed_stale_factor_surface(
                        snapshot["payload"],
                        surface=surface,
                        stale_seed_key=stale_seed_key,
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "factor role|FactorRoleJudgment|local state owner",
                    ):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(active_trace),
                            checkpoint=replace(
                                checkpoint,
                                actions=tuple(actions),
                            ),
                        )

    def test_completed_report_factor_surfaces_parse_before_stale_quarantine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "mixed-stale-report-surfaces.checkpoint",
                fail_factor=True,
            )
            checkpoint, active_trace, stale_seed_key = (
                _with_mixed_stale_seed(checkpoint)
            )
            graph = TraceGraph.from_trace(active_trace)
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=checkpoint,
            )
            payload = restored.build_report(
                judge=_LifecycleJudge(fail_factor=True)
            ).to_dict()
            legal = (
                recursive_analyzer_module
                ._quarantine_stale_seed_report_payload(graph, payload)
            )
            self.assertEqual(
                len(legal["metadata"]["factor_role_journal"]),
                1,
            )

            for surface in (
                "factor_role_journal",
                "factor_role_action_projections",
                "factor_role_judgments",
                "factor_role_gaps",
            ):
                with self.subTest(surface=surface):
                    forged = copy.deepcopy(payload)
                    _inject_malformed_stale_factor_surface(
                        forged["metadata"],
                        surface=surface,
                        stale_seed_key=stale_seed_key,
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "factor role|FactorRoleJudgment|local state owner",
                    ):
                        (
                            recursive_analyzer_module
                            ._quarantine_stale_seed_report_payload(
                                graph,
                                forged,
                            )
                        )

    def test_mixed_active_stale_resume_replays_active_terminal_without_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = (
                Path(tempdir) / "mixed-stale-control.checkpoint"
            )
            config = _lifecycle_checkpoint_config()
            _, checkpoint = _persist_lifecycle_checkpoint(
                checkpoint_root,
                snapshot_terminal=False,
            )
            checkpoint, active_trace, stale_seed_key = (
                _with_mixed_stale_seed(checkpoint)
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(active_trace),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            replay_bundle = CheckpointBundle(checkpoint_root)
            replay_bundle.initialize(config)
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=2,
                checkpoint=replay_bundle,
                checkpoint_config=config,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        self.assertTrue(
            any(
                item.start_ref == "record:stale_defect"
                and item.outcome == "evidence_gap"
                for item in restored.seed_results()
            )
        )
        active_projection = restored.factor_role_action_projection[0]
        self.assertNotEqual(
            active_projection["seed_binding_identity"],
            stale_seed_key,
        )

    def test_factor_checkpoint_rejects_projection_and_accounting_forgery(self) -> None:
        def factor_terminal(actions):
            return next(
                item
                for item in actions
                if item["operation"] == "factor_role_completed"
            )

        def state_snapshot(actions):
            return next(
                item
                for item in reversed(actions)
                if item["operation"] == "state_snapshot"
            )

        mutations = {
            "request facts": lambda actions: factor_terminal(actions)["payload"][
                "action_projection"
            ]["request_projection"]["facts"]["candidate_reference"].update(
                {"forged": True}
            ),
            "response role": lambda actions: factor_terminal(actions)["payload"][
                "action_projection"
            ]["judgment"].update({"factor_role": "unrelated"}),
            "queue owner": lambda actions: next(
                item
                for item in state_snapshot(actions)["payload"][
                    "confirmation_queue"
                ]
                if item["review_scope"] == "non_root"
            )["owner"].update({"hypothesis_id": "hypothesis:forged"}),
            "action origin": lambda actions: factor_terminal(actions)["payload"][
                "action_projection"
            ].update({"origin": "forged_origin"}),
            "physical count": lambda actions: factor_terminal(actions)[
                "payload"
            ]["action_projection"].update(
                {
                    "physical_request_delta": (
                        factor_terminal(actions)["payload"][
                            "action_projection"
                        ]["physical_requests_reserved"]
                        + 1
                    )
                }
            ),
        }

        with tempfile.TemporaryDirectory() as tempdir:
            _, checkpoint = _persist_lifecycle_checkpoint(
                Path(tempdir) / "factor-role.checkpoint"
            )
            graph = TraceGraph.from_trace(_lifecycle_trace())
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=checkpoint,
            )
            started = next(
                item
                for item in checkpoint.actions
                if item["operation"] == "factor_role_started"
            )
            terminal = factor_terminal(checkpoint.actions)
            self.assertEqual(
                set(started["payload"]),
                {
                    "owner",
                    "origin",
                    "candidate_ref",
                    "hypothesis_id",
                    "defect_fingerprint",
                    "seed_binding_identity",
                    "request_projection",
                    "request_identity",
                    "physical_requests_reserved",
                },
            )
            self.assertEqual(
                set(terminal["payload"]),
                {
                    "status",
                    "physical_requests_reserved",
                    "physical_request_delta",
                    "physical_request_exact",
                    "judgment",
                    "action_projection",
                    "provider_state",
                },
            )
            for label, mutate in mutations.items():
                actions = copy.deepcopy(list(checkpoint.actions))
                mutate(actions)
                with self.subTest(label=label), self.assertRaises(ValueError):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=replace(
                            checkpoint,
                            actions=tuple(actions),
                        ),
                    )


class FactorRoleEscalationTest(unittest.TestCase):
    def test_structured_candidate_phase_alone_blocks_escalation_root_substitution(
        self,
    ) -> None:
        expected_roles = {
            "closure": ("false_closure", "closure substitution"),
            "verification": (
                "verification_omission",
                "verification omission",
            ),
        }
        for phase, (
            expected_role,
            expected_reason,
        ) in expected_roles.items():
            with self.subTest(phase=phase, expected_role=expected_role):
                trace = _lifecycle_trace()
                factor_record = next(
                    item
                    for item in trace["records"]
                    if item["record_id"] == "factor"
                )
                factor_record["data"] = {
                    "phase": phase,
                    "rationale": (
                        "The candidate completed its assigned lifecycle."
                    ),
                }
                state = _lifecycle_state(
                    trace=trace,
                    max_judge_requests=3,
                )
                judge = _StructuredPhaseEscalationJudge()
                analyzer = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=3,
                )

                analyzer._confirm_queued_roots(state)
                report = state.build_report(
                    judge=judge,
                    fusion_mode="retrieval-global",
                )

                escalation_action = next(
                    item
                    for item in state.confirmation_action_projection
                    if isinstance(item.get("origin"), dict)
                )
                self.assertIn(
                    escalation_action["status"],
                    {"rejected", "unknown"},
                )
                self.assertIn(expected_reason, (
                    escalation_action["confirmation"]["reason"]
                    .replace("_", " ")
                    .casefold()
                ))
                self.assertFalse(
                    any(
                        item.node_ref == "record:factor"
                        for item in (
                            *report.confirmed_roots,
                            *report.co_roots,
                        )
                    )
                )

    def test_functional_escalation_cannot_relabel_closure_or_verification_omission_as_introduction_root(
        self,
    ) -> None:
        for expected_role in ("false_closure", "verification_omission"):
            with self.subTest(expected_role=expected_role):
                trace = _lifecycle_trace()
                factor_record = next(
                    item
                    for item in trace["records"]
                    if item["record_id"] == "factor"
                )
                factor_record["title"] = {
                    "false_closure": (
                        "Closed despite the required repository check "
                        "remaining absent."
                    ),
                    "verification_omission": (
                        "Skipped verification of the required repository check."
                    ),
                }[expected_role]
                state = _lifecycle_state(
                    trace=trace,
                    max_judge_requests=3,
                )
                judge = _SemanticEscalationLifecycleJudge(expected_role)
                analyzer = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=3,
                )

                analyzer._confirm_queued_roots(state)
                report = state.build_report(
                    judge=judge,
                    fusion_mode="retrieval-global",
                )

                self.assertEqual(
                    [item.node_ref for item in report.confirmed_roots],
                    ["record:root"],
                )
                self.assertFalse(
                    any(
                        item.node_ref == "record:factor"
                        for item in (*report.confirmed_roots, *report.co_roots)
                    )
                )
                escalation_action = next(
                    item
                    for item in state.confirmation_action_projection
                    if isinstance(item.get("origin"), dict)
                )
                self.assertIn(
                    escalation_action["status"],
                    {"rejected", "unknown"},
                )

    def test_necessary_materialization_publishes_without_root_escalation(
        self,
    ) -> None:
        state = _lifecycle_state(trace=_serial_lifecycle_trace())
        judge = _PublicationLifecycleJudge(
            "downstream_materialization",
            necessity_status="necessary",
        )
        analyzer = AgenticRecursiveAnalyzer(
            judge=judge,
            max_judge_requests=2,
        )

        analyzer._confirm_queued_roots(state)
        report = state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        )

        self.assertEqual(len(report.downstream_materializations), 1)
        materialization = report.downstream_materializations[0]
        self.assertEqual(
            materialization.role_judgment["necessity_status"],
            "necessary",
        )
        self.assertFalse(
            any(
                isinstance(item.get("origin"), dict)
                for item in state.confirmation_queue
            )
        )

    def test_completed_necessary_escalation_checkpoint_replays_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = (
                Path(tempdir) / "necessary-escalation.checkpoint"
            )
            original, checkpoint, config = (
                _persist_necessary_escalation_checkpoint(
                    checkpoint_root
                )
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(_lifecycle_trace()),
                checkpoint=checkpoint,
            )
            replay_judge = _ExplodingLifecycleJudge()
            replay_bundle = CheckpointBundle(checkpoint_root)
            replay_bundle.initialize(config)
            AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=3,
                checkpoint=replay_bundle,
                checkpoint_config=config,
            )._confirm_queued_roots(restored)

        self.assertEqual(replay_judge.calls, 0)
        original_escalations = [
            item
            for item in original.confirmation_queue
            if isinstance(item.get("origin"), dict)
        ]
        restored_escalations = [
            item
            for item in restored.confirmation_queue
            if isinstance(item.get("origin"), dict)
        ]
        restored_actions = [
            item
            for item in restored.confirmation_action_projection
            if isinstance(item.get("origin"), dict)
        ]
        self.assertEqual(len(original_escalations), 1)
        self.assertEqual(len(restored_escalations), 1)
        self.assertEqual(len(restored_actions), 1)
        self.assertEqual(
            restored_escalations[0]["origin"],
            original_escalations[0]["origin"],
        )
        self.assertEqual(
            restored_actions[0]["request_identity"],
            restored_escalations[0]["semantic_identity"],
        )
        self.assertEqual(
            restored.factor_role_action_projection,
            original.factor_role_action_projection,
        )
        self.assertEqual(restored.judge_requests, 3)

    def test_necessary_factor_enqueues_one_bound_root_scope_escalation(
        self,
    ) -> None:
        state = _lifecycle_state()
        judge = _EscalationLifecycleJudge("unknown")
        analyzer = AgenticRecursiveAnalyzer(
            judge=judge,
            max_judge_requests=3,
        )
        factor_queue = next(
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "non_root"
        )
        roots_before = (
            copy.deepcopy(state.confirmed_roots),
            copy.deepcopy(state.co_roots),
        )

        analyzer._judge_queued_factor_role(state, factor_queue)

        self.assertEqual(
            (state.confirmed_roots, state.co_roots),
            roots_before,
        )
        self.assertEqual(len(judge.factor_requests), 1)
        self.assertEqual(judge.confirmation_requests, [])
        escalation_queue = [
            item
            for item in state.confirmation_queue
            if isinstance(item.get("origin"), dict)
            and item["origin"].get("kind")
            == "factor_role_escalation"
        ]
        self.assertEqual(len(escalation_queue), 1)
        escalation = escalation_queue[0]
        self.assertEqual(escalation["review_scope"], "root")
        action = state.factor_role_action_projection[0]
        judgment = state.factor_role_judgments[0]
        self.assertEqual(
            escalation["origin"],
            {
                "kind": "factor_role_escalation",
                "factor_action_identity": action["semantic_key"],
                "factor_judgment_identity": (
                    judgment.judgment_identity
                ),
                "factor_request_identity": judgment.request_identity,
            },
        )
        root_facts = escalation["factual_request_projection"]["facts"]
        self.assertEqual(root_facts["candidate_ref"], judgment.candidate_ref)
        self.assertEqual(root_facts["hypothesis_id"], judgment.hypothesis_id)
        self.assertEqual(
            root_facts["hypothesis_semantic_hash"],
            judgment.hypothesis_semantic_hash,
        )
        self.assertEqual(
            root_facts["defect_state"]["fingerprint"],
            judgment.defect_fingerprint,
        )
        self.assertEqual(
            root_facts["seed_binding_identity"],
            judgment.seed_binding_identity,
        )
        self.assertNotIn(
            judgment.reason,
            stable_json(escalation["factual_request_projection"]),
        )
        self.assertFalse(
            analyzer._enqueue_factor_role_escalation(
                state,
                source_queue=factor_queue,
                judgment=judgment,
                action_projection=action,
            )
        )
        self.assertEqual(
            len(
                [
                    item
                    for item in state.confirmation_queue
                    if isinstance(item.get("origin"), dict)
                ]
            ),
            1,
        )

    def test_confirmed_escalation_uses_existing_reciprocal_root_ranking(
        self,
    ) -> None:
        report, state, judge = _escalation_report("confirmed")

        self.assertEqual(
            [request.candidate_ref for request in judge.factor_requests],
            ["record:factor"],
        )
        self.assertEqual(
            [
                request.candidate_ref
                for request in judge.confirmation_requests
            ],
            ["record:root", "record:factor"],
        )
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:root"],
        )
        self.assertEqual(
            [item.node_ref for item in report.co_roots],
            ["record:factor"],
        )
        self.assertEqual(
            report.metadata["factor_role_escalation_gaps"],
            (),
        )
        self.assertEqual(len(state.confirmation_queue), 3)
        self.assertEqual(len(state.confirmation_queue_keys), 3)
        self.assertEqual(
            len(
                {
                    state._confirmation_queue_key(item)
                    for item in state.confirmation_queue
                }
            ),
            3,
        )
        self.assertEqual(
            RecursiveAttributionReport.from_dict(
                report.to_dict()
            ).to_dict(),
            report.to_dict(),
        )

    def test_rejected_and_unknown_escalations_are_independent_gaps(
        self,
    ) -> None:
        for escalation_status in ("rejected", "unknown"):
            with self.subTest(status=escalation_status):
                report, _, judge = _escalation_report(
                    escalation_status
                )

                self.assertEqual(
                    [item.node_ref for item in report.confirmed_roots],
                    ["record:root"],
                )
                self.assertEqual(report.co_roots, ())
                self.assertEqual(
                    report.metadata["factor_confirmation_gaps"],
                    (),
                )
                self.assertEqual(
                    report.metadata["factor_role_gaps"],
                    (),
                )
                gaps = report.metadata[
                    "factor_role_escalation_gaps"
                ]
                self.assertEqual(len(gaps), 1)
                self.assertEqual(gaps[0]["status"], escalation_status)
                self.assertEqual(
                    gaps[0]["candidate_ref"],
                    "record:factor",
                )
                self.assertEqual(
                    [
                        request.candidate_ref
                        for request in judge.confirmation_requests
                    ],
                    ["record:root", "record:factor"],
                )
                self.assertEqual(len(judge.factor_requests), 1)

    def test_escalation_does_not_consume_the_global_root_quota(
        self,
    ) -> None:
        state = _lifecycle_state()
        judge = _EscalationLifecycleJudge("rejected")

        with patch.object(
            recursive_analyzer_module,
            "MAX_ROOT_CONFIRMATION_CANDIDATES",
            1,
        ):
            AgenticRecursiveAnalyzer(
                judge=judge,
                max_judge_requests=3,
            )._confirm_queued_roots(state)

        ordinary_root_entries = [
            item
            for item in state.confirmation_queue
            if item["review_scope"] == "root"
            and not isinstance(item.get("origin"), dict)
        ]
        escalation_entries = [
            item
            for item in state.confirmation_queue
            if isinstance(item.get("origin"), dict)
        ]
        self.assertEqual(len(ordinary_root_entries), 1)
        self.assertEqual(len(escalation_entries), 1)
        self.assertEqual(
            [
                request.candidate_ref
                for request in judge.confirmation_requests
            ],
            ["record:root", "record:factor"],
        )

    def test_report_rejects_non_necessary_escalation_source(
        self,
    ) -> None:
        report, _, _ = _escalation_report("rejected")
        payload = report.to_dict()
        _rewrite_completed_factor_role(
            payload,
            factor_role="contributing_condition",
        )
        action = payload["metadata"][
            "factor_role_action_projections"
        ][0]
        judgment = FactorRoleJudgment.from_dict(action["judgment"])
        origin = {
            "kind": "factor_role_escalation",
            "factor_action_identity": action["semantic_key"],
            "factor_judgment_identity": judgment.judgment_identity,
            "factor_request_identity": judgment.request_identity,
        }
        _rewrite_escalation_origin(payload, origin)

        with self.assertRaisesRegex(
            ValueError,
            "necessary|escalation",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_unbound_or_multiply_consumed_escalation(
        self,
    ) -> None:
        mutations = {
            "foreign factor action": lambda payload: (
                _rewrite_escalation_origin(
                    payload,
                    {
                        **next(
                            item["origin"]
                            for item in payload["metadata"][
                                "confirmation_queue"
                            ]
                            if isinstance(item.get("origin"), dict)
                        ),
                        "factor_action_identity": (
                            "factor_role:foreign"
                        ),
                    },
                )
            ),
            "foreign factor judgment": lambda payload: (
                _rewrite_escalation_origin(
                    payload,
                    {
                        **next(
                            item["origin"]
                            for item in payload["metadata"][
                                "confirmation_queue"
                            ]
                            if isinstance(item.get("origin"), dict)
                        ),
                        "factor_judgment_identity": (
                            "factor-role-judgment:v1:foreign"
                        ),
                    },
                )
            ),
            "foreign factor request": lambda payload: (
                _rewrite_escalation_origin(
                    payload,
                    {
                        **next(
                            item["origin"]
                            for item in payload["metadata"][
                                "confirmation_queue"
                            ]
                            if isinstance(item.get("origin"), dict)
                        ),
                        "factor_request_identity": (
                            "factor-role-request:v1:foreign"
                        ),
                    },
                )
            ),
            "second root action": lambda payload: payload["metadata"][
                "confirmation_action_projection"
            ].append(
                copy.deepcopy(
                    next(
                        item
                        for item in payload["metadata"][
                            "confirmation_action_projection"
                        ]
                        if isinstance(item.get("origin"), dict)
                    )
                )
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                report, _, _ = _escalation_report("rejected")
                payload = report.to_dict()
                mutate(payload)
                with self.assertRaisesRegex(
                    ValueError,
                    "escalation|consum|identity|action",
                ):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_coordinated_foreign_escalation_owner(
        self,
    ) -> None:
        report, _, _ = _escalation_report("rejected")
        payload = report.to_dict()
        source_owner = copy.deepcopy(
            payload["metadata"]["factor_role_action_projections"][0][
                "owner"
            ]
        )
        foreign_owner = _rewrite_escalation_owner(payload)
        self.assertNotEqual(foreign_owner, source_owner)

        with self.assertRaisesRegex(
            ValueError,
            "owner|escalation",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_escalation_binding_field_mutations(
        self,
    ) -> None:
        mutations = {
            "seed_binding_identity": "seed:foreign",
            "hypothesis_id": "hyp:foreign",
            "defect_fingerprint": "defect:foreign",
            "candidate_ref": "record:foreign",
        }
        for field_name, foreign_value in mutations.items():
            with self.subTest(field=field_name):
                report, _, _ = _escalation_report("rejected")
                payload = report.to_dict()
                metadata = payload["metadata"]
                for queued in metadata["confirmation_queue"]:
                    if isinstance(queued.get("origin"), dict):
                        queued[field_name] = foreign_value
                queue_key_index = {
                    "hypothesis_id": 0,
                    "candidate_ref": 1,
                    "defect_fingerprint": 2,
                    "seed_binding_identity": 3,
                }[field_name]
                for queue_key in metadata["confirmation_queue_keys"]:
                    if len(queue_key) == 5:
                        queue_key[queue_key_index] = foreign_value
                for journal in metadata["confirmation_journal"]:
                    if isinstance(journal.get("origin"), dict):
                        journal[field_name] = foreign_value
                for action in metadata["confirmation_action_projection"]:
                    if isinstance(action.get("origin"), dict):
                        action[field_name] = foreign_value
                for gap in metadata["factor_role_escalation_gaps"]:
                    gap[field_name] = foreign_value

                with self.assertRaisesRegex(
                    ValueError,
                    (
                        "escalation|seed|hypothesis|defect|candidate|"
                        "projection"
                    ),
                ):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_non_completed_factor_source(self) -> None:
        report, _, _ = _escalation_report("rejected")
        payload = report.to_dict()
        metadata = payload["metadata"]
        action = metadata["factor_role_action_projections"][0]
        action["operation"] = "factor_role_failed"
        action["failure_classification"] = "provider_failure"
        metadata["factor_role_journal"] = [
            {**copy.deepcopy(action), "status": "failed"}
        ]
        source_queue = next(
            item
            for item in metadata["confirmation_queue"]
            if item["review_scope"] == "non_root"
        )
        source_queue["status"] = "failed"
        source_queue["failure_classification"] = "provider_failure"

        with self.assertRaisesRegex(
            ValueError,
            "completed|terminal|escalation|reachable",
        ):
            RecursiveAttributionReport.from_dict(payload)


class FactorRolePublicationTest(unittest.TestCase):
    def test_independent_factor_judgment_owns_terminal_active_role_provenance(
        self,
    ) -> None:
        expected_roles = {
            "contributing_condition": "amplifying_condition",
            "amplifying_factor": "amplifying_condition",
            "downstream_materialization": "downstream_materialization",
            "unrelated": None,
            "unknown": None,
        }
        for factor_role, expected_active_role in expected_roles.items():
            with self.subTest(factor_role=factor_role):
                report, _ = _publication_report(factor_role)
                action = report.metadata[
                    "factor_role_action_projections"
                ][0]
                binding = action.get("active_role_binding")
                if expected_active_role is None:
                    self.assertIsNone(binding)
                else:
                    self.assertEqual(
                        binding["causal_role"],
                        expected_active_role,
                    )
                    self.assertEqual(binding["disposition"], "factor")
                if factor_role == "unrelated":
                    self.assertEqual(report.contributing_conditions, ())
                    self.assertEqual(report.amplifying_factors, ())
                    self.assertEqual(report.downstream_materializations, ())

    def test_shared_prerequisite_publishes_once_as_active_signature_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            report, _, _ = _persist_publication_report(
                Path(tempdir) / "active-role-factor.checkpoint",
                factor_role="contributing_condition",
            )

        self.assertEqual(len(report.contributing_conditions), 1)
        factor = report.contributing_conditions[0]
        self.assertEqual(factor.node_ref, "record:factor")
        self.assertEqual(factor.factor_label, "amplifying_condition")
        self.assertEqual(
            factor.provenance["active_role_binding"]["disposition"],
            "factor",
        )
        self.assertFalse(
            any(
                item.node_ref == factor.node_ref
                for item in (*report.confirmed_roots, *report.co_roots)
            )
        )

    def test_report_requires_complete_terminal_factor_queue_snapshot(
        self,
    ) -> None:
        for field_name in (
            "seed_key",
            "requested_by_ref",
            "checked_evidence_refs",
            "task_obligations",
        ):
            with self.subTest(deleted=field_name):
                report, _ = _publication_report(
                    "contributing_condition"
                )
                payload = report.to_dict()
                queue = next(
                    item
                    for item in payload["metadata"][
                        "confirmation_queue"
                    ]
                    if item["review_scope"] == "non_root"
                )
                del queue[field_name]
                with self.assertRaises(ValueError):
                    RecursiveAttributionReport.from_dict(payload)

        mutations = {
            "requested_by_ref": lambda queue: queue.update(
                {"requested_by_ref": queue["candidate_ref"]}
            ),
            "artifact_evidence_envelopes": lambda queue: queue.update(
                {"artifact_evidence_envelopes": [{"forged": True}]}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutated=label):
                report, _ = _publication_report(
                    "contributing_condition"
                )
                payload = report.to_dict()
                queue = next(
                    item
                    for item in payload["metadata"][
                        "confirmation_queue"
                    ]
                    if item["review_scope"] == "non_root"
                )
                mutate(queue)
                with self.assertRaises(ValueError):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_rebuilds_confirmation_queue_keys_exactly(
        self,
    ) -> None:
        mutations = {
            "removal": lambda keys, index: keys.pop(index),
            "candidate mutation": lambda keys, index: keys[index].__setitem__(
                1, "record:forged"
            ),
            "duplication": lambda keys, index: keys.append(
                copy.deepcopy(keys[index])
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(mutation=label):
                report, _ = _publication_report(
                    "contributing_condition"
                )
                payload = report.to_dict()
                keys = payload["metadata"]["confirmation_queue_keys"]
                index = next(
                    index
                    for index, key in enumerate(keys)
                    if key[1] == "record:factor"
                )
                mutate(keys, index)
                with self.assertRaises(ValueError):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_closes_factor_role_gaps_to_terminal_lifecycle(
        self,
    ) -> None:
        for label, mutate in {
            "removal": lambda gaps: gaps.clear(),
            "reason mutation": lambda gaps: gaps[0].update(
                {"reason": "A rewritten audit reason."}
            ),
            "duplication": lambda gaps: gaps.append(
                copy.deepcopy(gaps[0])
            ),
        }.items():
            with self.subTest(completed_unknown=label):
                report, _ = _publication_report("unknown")
                payload = report.to_dict()
                gaps = payload["metadata"]["factor_role_gaps"]
                self.assertEqual(len(gaps), 1)
                mutate(gaps)
                with self.assertRaises(ValueError):
                    RecursiveAttributionReport.from_dict(payload)

        state = _lifecycle_state()
        failed_judge = _LifecycleJudge(fail_factor=True)
        AgenticRecursiveAnalyzer(
            judge=failed_judge,
            max_judge_requests=2,
        )._confirm_queued_roots(state)
        failed_payload = state.build_report(
            judge=failed_judge
        ).to_dict()
        self.assertEqual(
            len(failed_payload["metadata"]["factor_role_gaps"]),
            1,
        )
        failed_payload["metadata"]["factor_role_gaps"] = []
        with self.assertRaises(ValueError):
            RecursiveAttributionReport.from_dict(failed_payload)

        definitive_report, _ = _publication_report(
            "contributing_condition"
        )
        definitive_payload = definitive_report.to_dict()
        action = definitive_payload["metadata"][
            "factor_role_action_projections"
        ][0]
        judgment = FactorRoleJudgment.from_dict(action["judgment"])
        definitive_payload["metadata"]["factor_role_gaps"].append(
            {
                "candidate_ref": judgment.candidate_ref,
                "hypothesis_id": judgment.hypothesis_id,
                "defect_fingerprint": judgment.defect_fingerprint,
                "seed_binding_identity": (
                    judgment.seed_binding_identity
                ),
                "request_identity": judgment.request_identity,
                "judgment_identity": judgment.judgment_identity,
                "reason": judgment.reason,
                "failure_classification": action[
                    "failure_classification"
                ],
                "owner": copy.deepcopy(action["owner"]),
                "origin": action["origin"],
            }
        )
        with self.assertRaises(ValueError):
            RecursiveAttributionReport.from_dict(definitive_payload)

    def test_report_rejects_terminal_factor_queue_outer_mutations(
        self,
    ) -> None:
        mutations = {
            "candidate_ref": lambda queue: queue.update(
                {"candidate_ref": "record:forged"}
            ),
            "hypothesis_id": lambda queue: queue.update(
                {"hypothesis_id": "hyp:forged"}
            ),
            "recursive_path": lambda queue: queue.update(
                {
                    "recursive_path": [
                        "record:factor",
                        "record:root",
                        "record:defect",
                    ]
                }
            ),
            "failure_classification": lambda queue: queue.update(
                {"failure_classification": "provider_failure"}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(field=label):
                report, _ = _publication_report(
                    "contributing_condition"
                )
                payload = report.to_dict()
                queue = next(
                    item
                    for item in payload["metadata"][
                        "confirmation_queue"
                    ]
                    if item["review_scope"] == "non_root"
                )
                mutate(queue)
                with self.assertRaises(ValueError):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_completed_factor_queue_provider_failure(
        self,
    ) -> None:
        report, _ = _publication_report("contributing_condition")
        payload = report.to_dict()
        queue = next(
            item
            for item in payload["metadata"]["confirmation_queue"]
            if item["review_scope"] == "non_root"
        )
        self.assertEqual(queue["status"], "completed")
        queue["failure_classification"] = "provider_failure"

        with self.assertRaisesRegex(
            ValueError,
            "failure|terminal|completed",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_duplicate_candidate_seed_lifecycles(
        self,
    ) -> None:
        report, _ = _publication_report("contributing_condition")
        payload = report.to_dict()
        _inject_duplicate_factor_lifecycle(payload)

        with self.assertRaisesRegex(
            ValueError,
            "duplicate candidate|one seed|exclusive",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_unknown_fields_on_every_factor_public_route(
        self,
    ) -> None:
        cases = (
            (
                "condition",
                "contributing_condition",
                "contributing_conditions",
            ),
            (
                "amplifier",
                "amplifying_factor",
                "amplifying_factors",
            ),
            (
                "materialization",
                "downstream_materialization",
                "downstream_materializations",
            ),
            ("unrelated", "unrelated", "rejected_candidates"),
            ("unknown_public", "unknown", "factor_confirmation_gaps"),
            ("unknown_audit_gap", "unknown", "factor_role_gaps"),
        )
        for label, role, collection in cases:
            with self.subTest(surface=label):
                report, _ = _publication_report(role)
                payload = report.to_dict()
                target = (
                    payload["metadata"][collection][0]
                    if collection
                    in {
                        "factor_confirmation_gaps",
                        "factor_role_gaps",
                    }
                    else payload[collection][0]
                )
                target["forged_public_field"] = True
                with self.assertRaisesRegex(
                    ValueError,
                    "inexact|canonical|match|diverge",
                ):
                    RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_coordinated_mechanism_source_resigning(
        self,
    ) -> None:
        report, _ = _publication_report("contributing_condition")
        payload = report.to_dict()
        judgment = FactorRoleJudgment.from_dict(
            payload["metadata"]["factor_role_action_projections"][0][
                "judgment"
            ]
        )
        offered_ref = judgment.recursive_path[-1]
        self.assertNotEqual(offered_ref, judgment.candidate_ref)
        _resign_factor_mechanism_source(
            payload,
            source_ref=offered_ref,
        )

        with self.assertRaisesRegex(
            ValueError,
            "source_ref must match candidate_ref",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_orphan_terminal_non_root_queue(
        self,
    ) -> None:
        report, _ = _publication_report("contributing_condition")
        payload = report.to_dict()
        metadata = payload["metadata"]
        terminal = next(
            item
            for item in metadata["confirmation_queue"]
            if item["review_scope"] == "non_root"
        )
        self.assertEqual(terminal["status"], "completed")
        metadata["factor_role_action_projections"] = []
        metadata["factor_role_journal"] = []
        metadata["factor_role_judgments"] = []
        payload["contributing_conditions"] = []

        with self.assertRaisesRegex(
            ValueError,
            "terminal.*queue|bijective",
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_report_rejects_coordinated_invalid_request_resigning(
        self,
    ) -> None:
        mutations = {
            "invalid candidate reference type": lambda facts: facts.update(
                {"candidate_reference": "record:prompt"}
            ),
            "analysis control envelope": lambda facts: facts[
                "candidate_reference"
            ].update(
                {
                    "forged_control": {
                        "schema": "global-candidate-judgment/v1",
                    }
                }
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                report, _ = _publication_report(
                    "contributing_condition"
                )
                payload = report.to_dict()
                _resign_factor_request_projection(
                    payload,
                    mutate=mutate,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid types|analysis-control",
                ):
                    RecursiveAttributionReport.from_dict(payload)

    def test_tool_error_can_publish_only_as_non_root_materialization(
        self,
    ) -> None:
        trace = _lifecycle_trace()
        factor_record = next(
            item
            for item in trace["records"]
            if item["record_id"] == "factor"
        )
        factor_record["component"] = "tool"
        factor_record["event_type"] = "tool.error"
        factor_record["data"] = {
            "error": "The tool exposed the already introduced omission."
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:defect"],
            objective=(
                "Find the necessary root and classify the downstream "
                "tool outcome."
            ),
            analysis_perspective="Improve repository reasoning.",
            max_hypotheses=24,
        )
        analyzer = AgenticRecursiveAnalyzer(
            judge=_OutcomeLifecycleGlobalJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=2,
        )
        analyzer._run_global_candidate_prepass(state, graph)
        self.assertEqual(
            [
                (item["candidate_ref"], item["review_scope"])
                for item in state.confirmation_queue
            ],
            [
                ("record:root", "root"),
                ("record:factor", "non_root"),
            ],
        )

        judge = _PublicationLifecycleJudge(
            "downstream_materialization"
        )
        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=2,
        )._confirm_queued_roots(state)
        report = state.build_report(
            judge=judge,
            fusion_mode="retrieval-global",
        )

        self.assertEqual(
            [
                item.candidate_ref
                for item in report.downstream_materializations
            ],
            ["record:factor"],
        )
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:root"],
        )
        self.assertFalse(
            any(
                item.candidate_ref == "record:factor"
                for item in report.confirmations
            )
        )
        validate_recursive_report_against_graph(
            graph,
            report,
            label="tool error materialization report",
        )

    def test_completed_factor_roles_publish_to_five_distinct_surfaces(
        self,
    ) -> None:
        expected = {
            "contributing_condition": "contributing_conditions",
            "amplifying_factor": "amplifying_factors",
            "downstream_materialization": "downstream_materializations",
            "unrelated": "rejected_candidates",
            "unknown": "factor_confirmation_gaps",
        }
        for role, collection in expected.items():
            with self.subTest(role=role):
                report, _ = _publication_report(role)
                if collection == "factor_confirmation_gaps":
                    published = report.metadata[collection]
                else:
                    published = getattr(report, collection)
                self.assertEqual(len(published), 1)

    def test_factor_judgments_never_enter_root_confirmations(self) -> None:
        for role in (
            "contributing_condition",
            "amplifying_factor",
            "downstream_materialization",
            "unrelated",
            "unknown",
        ):
            with self.subTest(role=role):
                report, _ = _publication_report(role)
                judgment_identities = {
                    item["judgment_identity"]
                    for item in report.metadata["factor_role_judgments"]
                }
                self.assertTrue(judgment_identities)
                self.assertFalse(
                    any(
                        item.to_dict().get("judgment_identity")
                        in judgment_identities
                        for item in report.confirmations
                    )
                )

    def test_report_v22_round_trip_preserves_materializations(self) -> None:
        report, _ = _publication_report("downstream_materialization")
        payload = report.to_dict()
        self.assertEqual(
            payload["schema_version"],
            "recursive-attribution-report/v23",
        )
        self.assertEqual(len(payload["downstream_materializations"]), 1)
        validate_modern_report_shape(payload)
        restored = RecursiveAttributionReport.from_dict(payload)
        self.assertEqual(restored.to_dict(), payload)

    def test_publication_role_identity_cannot_cross_collections(self) -> None:
        report, _ = _publication_report("contributing_condition")
        condition = report.contributing_conditions[0]
        with self.assertRaises(ValueError):
            replace(
                report,
                amplifying_factors=(condition,),
            )

    def test_canonical_publication_rejects_caller_divergence(self) -> None:
        report, _ = _publication_report("contributing_condition")
        projection = report.metadata["factor_role_action_projections"][0]
        judgment = FactorRoleJudgment.from_dict(projection["judgment"])
        canonical = causal_state_module.canonical_factor_role_publication(
            judgment=judgment,
            request_projection=projection["request_projection"],
            action_projection=projection,
        )
        forged = canonical.to_dict()
        forged["reason"] = "Caller supplied a divergent reason."
        with self.assertRaises(ValueError):
            causal_state_module.canonical_factor_role_publication(
                judgment=judgment,
                request_projection=projection["request_projection"],
                action_projection=projection,
                publication=forged,
            )

    def test_canonical_action_rejects_coordinated_self_target_resigning(
        self,
    ) -> None:
        report, _ = _publication_report("contributing_condition")
        projection = copy.deepcopy(
            report.to_dict()["metadata"][
                "factor_role_action_projections"
            ][0]
        )
        judgment = FactorRoleJudgment.from_dict(projection["judgment"])
        rewritten = replace(
            judgment,
            factor_mechanism={
                **dict(judgment.factor_mechanism),
                "target_ref": judgment.candidate_ref,
            },
        )
        projection["judgment"] = rewritten.to_dict()
        projection["judgment_identity"] = rewritten.judgment_identity

        with self.assertRaisesRegex(
            ValueError,
            "target_ref must be a downstream recursive path ref",
        ):
            causal_state_module.canonical_factor_role_publication(
                judgment=rewritten,
                request_projection=projection["request_projection"],
                action_projection=projection,
            )

    def test_coordinated_role_relabels_conflict_with_action_ledger(
        self,
    ) -> None:
        cases = (
            (
                "downstream_materialization",
                "contributing_condition",
            ),
            ("unrelated", "amplifying_factor"),
        )
        for original_role, forged_role in cases:
            with self.subTest(
                original_role=original_role,
                forged_role=forged_role,
            ), tempfile.TemporaryDirectory() as tempdir:
                report, checkpoint, graph = _persist_publication_report(
                    Path(tempdir) / "publication.checkpoint",
                    factor_role=original_role,
                )
                payload = report.to_dict()
                _rewrite_completed_factor_role(
                    payload,
                    factor_role=forged_role,
                )
                internally_consistent = RecursiveAttributionReport.from_dict(
                    payload
                )
                with self.assertRaises(ValueError):
                    validate_recursive_report_against_graph(
                        graph,
                        internally_consistent,
                        label="coordinated role relabel",
                        action_records=checkpoint.actions,
                    )

    def test_coordinated_unoffered_evidence_is_rejected(self) -> None:
        report, _ = _publication_report("contributing_condition")
        payload = report.to_dict()
        publication = copy.deepcopy(
            payload["contributing_conditions"][0]
        )
        _rewrite_completed_factor_role(
            payload,
            factor_role="contributing_condition",
            evidence_refs=("record:foreign",),
        )
        action = payload["metadata"][
            "factor_role_action_projections"
        ][0]
        rewritten = action["judgment"]
        publication.update(
            {
                "reason": rewritten["reason"],
                "evidence_refs": rewritten["evidence_refs"],
                "confirmation": rewritten,
            }
        )
        publication["provenance"]["response_identity"] = rewritten[
            "judgment_identity"
        ]
        publication["provenance"]["judgment_identity"] = rewritten[
            "judgment_identity"
        ]
        payload["contributing_conditions"] = [publication]
        with self.assertRaisesRegex(
            ValueError, "request-grounded|outside request"
        ):
            RecursiveAttributionReport.from_dict(payload)

    def test_necessary_factor_signal_is_never_published_as_root(self) -> None:
        report, state = _publication_report(
            "unknown",
            necessity_status="necessary",
        )
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:root"],
        )
        self.assertFalse(
            any(
                item.node_ref == "record:factor"
                for item in (*report.confirmed_roots, *report.co_roots)
            )
        )
        self.assertEqual(report.metadata["factor_confirmation_gaps"], ())
        action = report.metadata["factor_role_action_projections"][0]
        judgment = FactorRoleJudgment.from_dict(action["judgment"])
        promoted_confirmation = replace(
            RootConfirmation.confirmed(
                judgment.candidate_ref,
                excerpt="Forged direct promotion from a necessity signal.",
                reason="Forged direct promotion.",
                counterfactual=confirmation_counterfactual_for(
                    judgment.candidate_ref,
                    "confirmed",
                ),
                confidence=judgment.confidence,
                evidence_refs=judgment.evidence_refs,
            ),
            hypothesis_id=judgment.hypothesis_id,
            hypothesis_semantic_hash=(
                judgment.hypothesis_semantic_hash
            ),
            defect_fingerprint=judgment.defect_fingerprint,
            recursive_path=judgment.recursive_path,
            seed_binding_identity=judgment.seed_binding_identity,
            analysis_perspective=judgment.analysis_perspective,
        )
        promoted_root = (
            causal_state_module.canonical_confirmed_root_publication(
                confirmation=promoted_confirmation,
                defect_state=state.defect_states[
                    judgment.defect_fingerprint
                ],
                candidate_node=state.graph.nodes[
                    judgment.candidate_ref
                ],
                seed_start_ref=judgment.recursive_path[-1],
            )
        )
        promoted_seed = replace(
            report.seed_results[0],
            confirmation_identities=(
                promoted_confirmation.confirmation_identity,
            ),
            confirmed_root_refs=(judgment.candidate_ref,),
        )
        with self.assertRaisesRegex(
            ValueError,
            "necessary FactorRole signal",
        ):
            replace(
                report,
                seed_results=(promoted_seed,),
                confirmations=(promoted_confirmation,),
                confirmed_roots=(promoted_root,),
                co_roots=(),
            )


if __name__ == "__main__":
    unittest.main()
