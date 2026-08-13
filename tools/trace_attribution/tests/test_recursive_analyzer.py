from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from trace_attribution.cache import JudgmentCache
from trace_attribution.candidate_budget import select_global_candidates
from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    BoundedJudgeCallError,
    BoundedJudgeCapability,
    ClaudeCausalJudge,
    FactorRoleJudgment,
    OfflineCausalJudgeAdapter,
    OfflineJudgeCapability,
    factor_role_request_identity,
)
from trace_attribution.causal_state import (
    CausalCandidate,
    CausalStepJudgment,
    DefectState,
    LocalStateOwner,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RootConfirmation,
    canonical_confirmation_publication_provenance,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    confirmation_response_identity_for,
    semantic_visit_key,
)
from trace_attribution.checkpoint import (
    LegacyProjectionNotRequired,
    LegacyProjectionRequired,
)
from trace_attribution.errors import (
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
)
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph
from trace_attribution.causal_retrieval import (
    SemanticPredecessorRetriever,
    root_candidate_eligible,
)
from trace_attribution.global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
)
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _assert_report_grounded_evidence,
    _grounded_downstream_path,
    _validate_global_pass_derivations,
    classify_legacy_projection_shape,
)
from trace_attribution import recursive_analyzer as recursive_analyzer_module


def observed_trace(*, branching: bool = False, artifact_content: str = "") -> dict:
    records = [
        {
            "record_id": "prompt",
            "component": "prompt",
            "event_type": "message.input",
            "data": {"text": "Implement the complete namespace compatibility contract."},
        },
        {
            "record_id": "decision",
            "component": "agent",
            "event_type": "decision",
            "data": {"rationale": "Implement only the methods found in the first search."},
        },
        {
            "record_id": "change",
            "component": "processor",
            "event_type": "change",
            "data": {"summary": "The change omits parse_namespace_object."},
        },
        {
            "record_id": "observed_defect",
            "component": "evaluation",
            "event_type": "case.observed_defect",
            "source_refs": ["record:change"],
            "data": {
                "failure_type": "missing_namespace_contract",
                "expected": "The parser preserves the namespace compatibility contract.",
                "actual": "parse_namespace_object is absent.",
                "mechanism": "A required method was omitted from the implementation.",
                "scope": "parser_contract_recovery",
                "summary": "The required namespace method is missing.",
            },
        },
    ]
    if branching:
        records.insert(
            2,
            {
                "record_id": "context",
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "The complete compatibility contract is documented here."},
            },
        )
    if artifact_content:
        records[1]["data"]["hydrated_artifacts"] = [
            {
                "artifact_id": "decision-context",
                "content_hash": "sha256:{0}".format(
                    hashlib.sha256(
                        artifact_content.encode("utf-8")
                    ).hexdigest()
                ),
                "content": artifact_content,
            }
        ]
    edges = [
        {
            "from": {"type": "record", "id": "decision"},
            "to": {"type": "record", "id": "change"},
            "relation": "decision_guided_change",
            "evidence_type": "confirmed",
            "confidence": 0.98,
            "eligible_for_attribution": True,
        },
        {
            "from": {"type": "record", "id": "change"},
            "to": {"type": "record", "id": "observed_defect"},
            "relation": "change_observed_by_evaluation",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        },
    ]
    if branching:
        edges.append(
            {
                "from": {"type": "record", "id": "context"},
                "to": {"type": "record", "id": "change"},
                "relation": "context_available_to_change",
                "evidence_type": "confirmed",
                "confidence": 0.8,
                "eligible_for_attribution": True,
            }
        )
    return {"case_id": "recursive-case", "records": records, "dataflow_edges": edges}


def revision_bound_trace() -> dict:
    return {
        "manifest": {
            "case_id": "external-recursive-case",
            "run_id": "external-recursive-run",
            "subject_revision": "git:abc123",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "external-recursive-case",
                "run_id": "external-recursive-run",
            },
        },
        "records": [
            {
                "record_id": "tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"text": "cleanup evaluation evidence"},
            }
        ],
        "dataflow_edges": [],
    }


def external_payload(*, scope: str, status: str, subject_revision: str, evidence_refs):
    return {
        "source": "terminalbench",
        "scope": scope,
        "subject_revision": subject_revision,
        "assertion": "Cleanup completes after SIGINT.",
        "observation": "Cleanup evaluation result for {0}.".format(scope),
        "status": status,
        "observed_at": "2026-07-21T12:00:00Z",
        "evidence_refs": list(evidence_refs),
        "provenance": {"method": "benchmark_grader", "version": "1.0"},
    }


def relation(
    ref: str,
    relation_name: str,
    *,
    upstream_defect: DefectState | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> PredecessorAssessment:
    return PredecessorAssessment(
        ref=ref,
        relation=relation_name,
        reason="The predecessor semantically explains the current defect.",
        confidence=0.9,
        recurse=(
            relation_name in {"same_defect_propagation", "defect_transformation"}
            or (relation_name == "contributing_condition" and upstream_defect is not None)
        ),
        upstream_defect=upstream_defect,
        evidence_refs=evidence_refs or (ref,),
    )


def step(
    ref: str,
    *,
    status: str = "present",
    predecessors: tuple[PredecessorAssessment, ...] = (),
    introduction: bool = False,
    missing: tuple[str, ...] = (),
    suggested: dict | None = None,
) -> CausalStepJudgment:
    return CausalStepJudgment(
        current_node_ref=ref,
        current_defect_status=status,
        current_defect_reason="Scripted semantic judgment for {0}.".format(ref),
        predecessors=predecessors,
        candidate_introduction=introduction,
        missing_evidence=missing,
        suggested_investigation=suggested,
        confidence=0.9 if status != "unknown" else 0.0,
    )


def refresh_confirmation_response_identity(confirmation: dict) -> None:
    confirmation["response_identity"] = confirmation_response_identity_for(
        confirmation_identity=confirmation["confirmation_identity"],
        status=confirmation["status"],
        excerpt=confirmation["excerpt"],
        reason=confirmation["reason"],
        counterfactual=confirmation["counterfactual"],
        confidence=confirmation["confidence"],
        evidence_refs=tuple(confirmation["evidence_refs"]),
        counterfactual_status=confirmation["counterfactual_status"],
        factor_role=confirmation["factor_role"],
        competitor_comparisons=tuple(
            confirmation["competitor_comparisons"]
        ),
        factor_mechanism=confirmation["factor_mechanism"],
        analysis_perspective=confirmation.get("analysis_perspective", ""),
    )


def forge_published_root_identity(report_payload: dict, ghost_ref: str) -> None:
    confirmation = report_payload["confirmations"][0]
    confirmation["candidate_ref"] = ghost_ref
    confirmation["counterfactual"] = confirmation_counterfactual_for(
        ghost_ref,
        confirmation["status"],
        counterfactual_status=confirmation["counterfactual_status"],
    )
    recursive_path = [
        ghost_ref,
        *confirmation["recursive_path"][1:],
    ]
    seed_ref = report_payload["seed_results"][0]["start_ref"]
    if recursive_path[-1] != seed_ref:
        recursive_path.append(seed_ref)
    confirmation["recursive_path"] = recursive_path
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=ghost_ref,
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=confirmation["recursive_path"],
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    refresh_confirmation_response_identity(confirmation)
    root = report_payload["confirmed_roots"][0]
    root["node_ref"] = ghost_ref
    root["recursive_path"] = list(confirmation["recursive_path"])
    root["counterfactual"] = confirmation["counterfactual"]
    root["confirmation"] = copy.deepcopy(confirmation)
    active_role_binding = copy.deepcopy(
        root["provenance"]["active_role_binding"]
    )
    active_role_binding["candidate_ref"] = ghost_ref
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    root["provenance"]["active_role_binding"] = active_role_binding
    report_payload["root_causes"][0]["node_ref"] = ghost_ref
    seed = report_payload["seed_results"][0]
    seed["confirmed_root_refs"] = [ghost_ref]
    seed["confirmation_identities"] = [confirmation["confirmation_identity"]]


def forge_published_root_path(report_payload: dict, recursive_path: list[str]) -> None:
    confirmation = report_payload["confirmations"][0]
    confirmation["recursive_path"] = list(recursive_path)
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=confirmation["candidate_ref"],
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=confirmation["recursive_path"],
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    refresh_confirmation_response_identity(confirmation)
    root = report_payload["confirmed_roots"][0]
    root["recursive_path"] = list(recursive_path)
    root["confirmation"] = copy.deepcopy(confirmation)
    active_role_binding = copy.deepcopy(
        root["provenance"]["active_role_binding"]
    )
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    root["provenance"]["active_role_binding"] = active_role_binding
    report_payload["seed_results"][0]["confirmation_identities"] = [
        confirmation["confirmation_identity"]
    ]


class ScriptedCausalJudge(OfflineJudgeCapability):
    def __init__(self, script):
        self.script = script
        self.requests = []
        self.request_count = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

    def judge_step(self, request):
        self.requests.append(request)
        self.request_count += 1
        key = (request.current_node.ref, request.defect_state.label)
        value = self.script.get(key, self.script.get(request.current_node.ref))
        if isinstance(value, list):
            value = value.pop(0)
        if callable(value):
            value = value(request)
        if isinstance(value, Exception):
            self.provider_circuit_open = isinstance(value, JudgeProviderUnavailable)
            self.provider_circuit_reason = str(value)
            raise value
        if value is None:
            return step(request.current_node.ref, status="absent")
        return value


class ConfirmingScriptedJudge(ScriptedCausalJudge):
    def __init__(self, script, confirmations, *, factor_roles=None):
        super().__init__(script)
        self.confirmations = dict(confirmations)
        self.confirmation_requests = []
        self.factor_roles = dict(factor_roles or {})
        self.factor_requests = []

    def confirm_candidate(self, request):
        self.confirmation_requests.append(request)
        result = self.confirmations[request.candidate_ref]
        if request.competing_hypotheses and not result.competitor_comparisons:
            result = replace(
                result,
                competitor_comparisons=tuple(
                    {
                        "hypothesis_id": item["hypothesis_id"],
                        "hypothesis_semantic_hash": item["hypothesis_semantic_hash"],
                        "candidate_ref": item["candidate_reference"]["resolved_ref"],
                        "defect_fingerprint": item["active_defect"]["fingerprint"],
                        "seed_binding_identity": (
                            request.seed_binding_identity
                        ),
                        "confirmation_identity": item["confirmation_identity"],
                        "recursive_path": list(item["recursive_path"]),
                        "requires_independent_confirmation": item[
                            "requires_independent_confirmation"
                        ],
                        "status": getattr(self, "comparison_statuses", {}).get(
                            (
                                request.candidate_ref,
                                item["candidate_reference"]["resolved_ref"],
                            ),
                            (
                            "co_root"
                            if result.status == "confirmed"
                            and (
                                (
                                    item["candidate_reference"]["resolved_ref"] in self.confirmations
                                    and self.confirmations[
                                        item["candidate_reference"]["resolved_ref"]
                                    ].status == "confirmed"
                                )
                                or item["candidate_reference"]["resolved_ref"]
                                in getattr(self, "force_co_root_candidates", set())
                            )
                            else "outperformed"
                            ),
                        ),
                        "reason": "The independent verifier compared this alternative.",
                        "evidence_refs": [item["candidate_reference"]["resolved_ref"]],
                    }
                    for item in request.competing_hypotheses
                    if item["status"] in {"active", "supported", "unresolved"}
                ),
            )
        if result.factor_role in {"contributing_condition", "amplifying_factor"}:
            result = replace(
                result,
                evidence_refs=tuple(dict.fromkeys((
                    *result.evidence_refs,
                    request.candidate_ref,
                    request.recursive_path[-1],
                ))),
                factor_mechanism={
                    "mechanism_type": (
                        "amplification"
                        if result.factor_role == "amplifying_factor"
                        else "enabling_condition"
                    ),
                    "source_ref": request.candidate_ref,
                    "target_ref": request.recursive_path[-1],
                    "effect": "The factor changes defect exposure without independently causing it.",
                },
            )
        return result

    def judge_factor_role(self, request):
        self.factor_requests.append(request)
        role = str(
            self.factor_roles.get(request.candidate_ref) or "unknown"
        )
        necessity_status = (
            "unknown" if role == "unknown" else "not_necessary"
        )
        mechanism_type = {
            "contributing_condition": "enabling_condition",
            "amplifying_factor": "amplification",
            "downstream_materialization": "downstream_materialization",
        }.get(role)
        mechanism = (
            {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": mechanism_type,
                "source_ref": request.candidate_ref,
                "target_ref": request.recursive_path[-1],
                "effect": "Scripted independent factor-role effect.",
            }
            if mechanism_type is not None
            else {}
        )
        predicted_effect = {
            "contributing_condition": "reduces_defect_likelihood",
            "amplifying_factor": "reduces_defect_severity",
            "downstream_materialization": (
                "defect_still_present_without_materialization"
            ),
            "unrelated": "no_grounded_causal_influence_established",
            "unknown": "insufficient_grounded_evidence",
        }[role]
        return FactorRoleJudgment(
            candidate_ref=request.candidate_ref,
            necessity_status=necessity_status,
            factor_role=role,
            reason="Scripted independent {0} judgment.".format(role),
            confidence=0.0 if role == "unknown" else 0.85,
            evidence_refs=(request.candidate_ref,),
            recursive_path=request.recursive_path,
            factor_mechanism=mechanism,
            counterfactual={
                "schema": "factor-role-counterfactual/v1",
                "intervention_ref": request.candidate_ref,
                "intervention_kind": (
                    "replace_with_semantically_correct_behavior"
                ),
                "predicted_effect": predicted_effect,
            },
            hypothesis_id=request.hypothesis_id,
            hypothesis_semantic_hash=request.hypothesis_semantic_hash,
            defect_fingerprint=request.defect_state.fingerprint,
            seed_binding_identity=request.seed_binding_identity,
            analysis_perspective=request.analysis_perspective,
            request_identity=factor_role_request_identity(request),
        )


class FusionScriptedJudge(ConfirmingScriptedJudge, GlobalJudgeCapability):
    def __init__(
        self,
        *,
        global_outcome,
        script=None,
        confirmations=None,
        selected_candidate_refs=None,
        global_non_root_roles=None,
        factor_roles=None,
    ):
        super().__init__(
            script or {},
            confirmations or {},
            factor_roles=factor_roles,
        )
        self.global_outcome = global_outcome
        self.global_requests = []
        self.selected_candidate_refs = tuple(selected_candidate_refs or ())
        self.global_non_root_roles = dict(
            global_non_root_roles or {}
        )

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        selected = self.selected_candidate_refs if self.global_outcome == "candidate_roots" else ()
        if self.global_outcome == "candidate_roots" and not selected:
            selected = ("record:decision",)
        if self.global_outcome == "needs_expansion":
            selected = ()
        assessments = []
        for capsule in request.capsules:
            is_selected = capsule.candidate_ref in selected
            is_open = (
                capsule.candidate_ref
                in request.open_authored_root_candidate_refs
            )
            non_root_role = self.global_non_root_roles.get(
                capsule.candidate_ref
            )
            is_non_root = non_root_role is not None and not is_selected
            non_root_present = non_root_role in {
                "contributing_condition",
                "amplifying_factor",
                "outcome_evidence",
            }
            input_status = (
                "present"
                if non_root_role == "outcome_evidence"
                else (
                    "unknown"
                    if non_root_present
                    else "absent"
                )
            )
            assessments.append(
                GlobalCandidateAssessment(
                    candidate_ref=capsule.candidate_ref,
                    defect_status=(
                        "present"
                        if is_selected
                        else (
                            "present"
                            if is_non_root and non_root_present
                            else (
                                "absent"
                                if is_non_root
                                else (
                                    "unknown"
                                    if self.global_outcome
                                    in {"needs_expansion", "inconclusive"}
                                    else "absent"
                                )
                            )
                        )
                    ),
                    input_defect_status=(
                        "absent"
                        if is_selected or is_open
                        else (
                            input_status
                            if is_non_root
                            else "unknown"
                        )
                    ),
                    output_defect_status=(
                        "present"
                        if is_selected
                        else (
                            "present"
                            if is_non_root and non_root_present
                            else (
                                "absent"
                                if is_non_root
                                else (
                                    "unknown"
                                    if self.global_outcome
                                    in {"needs_expansion", "inconclusive"}
                                    else "absent"
                                )
                            )
                        )
                    ),
                    causal_path_refs=(
                        tuple(capsule.downstream_path)
                        if is_selected or is_open or is_non_root
                        else ()
                    ),
                    counterfactual={
                        "intervention_ref": capsule.candidate_ref,
                        "intervention_kind": "replace_with_semantically_correct_behavior",
                        "predicted_defect_status": (
                            "absent"
                            if is_selected
                            or non_root_role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "present"
                        ),
                        "causal_effect": (
                            "prevents_defect"
                            if is_selected
                            or non_root_role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "does_not_prevent_defect"
                        ),
                    },
                    compared_candidate_refs=request.open_authored_root_candidate_refs,
                    causal_role=(
                        "root_candidate"
                        if is_selected
                        else (
                            non_root_role
                            if is_non_root
                            else (
                                "unknown"
                                if self.global_outcome
                                in {"needs_expansion", "inconclusive"}
                                else "exculpatory_evidence"
                            )
                        )
                    ),
                    responsibility=(
                        "primary"
                        if is_selected
                        else "shared"
                        if non_root_role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else "unknown"
                        if self.global_outcome
                        in {"needs_expansion", "inconclusive"}
                        else "none"
                    ),
                    candidate_phase=(
                        "implementation"
                        if is_selected
                        else "planning"
                        if non_root_role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else "intermediate"
                    ),
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode=(
                        "positive_introduction"
                        if is_selected
                        else "omission_enabling_condition"
                        if non_root_role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else "unknown"
                        if self.global_outcome
                        in {"needs_expansion", "inconclusive"}
                        else "none"
                    ),
                    obligation_refs=(),
                    contribution_mechanism=(
                        {
                            "type": "scope_narrowing",
                            "target_ref": capsule.candidate_ref,
                            "effect": (
                                "The scripted candidate changes the "
                                "downstream repair scope."
                            ),
                            "evidence_refs": (
                                capsule.candidate_ref,
                            ),
                        }
                        if non_root_role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else None
                    ),
                    reason="Scripted global comparative assessment.",
                    evidence_refs=(capsule.candidate_ref,),
                    confidence=0.9 if self.global_outcome != "inconclusive" else 0.0,
                )
            )
        decisive = selected
        if self.global_outcome == "no_defect":
            decisive = (request.capsules[0].candidate_ref,)
        expansion = ()
        missing = ()
        if self.global_outcome == "needs_expansion":
            expansion = (
                {
                    "anchor_ref": "record:decision",
                    "context_kind": "upstream",
                    "reason": "Inspect the authored implementation assumption.",
                    "expected_judgment_change": (
                        "The candidate may change from root_candidate to "
                        "propagation_only."
                    ),
                },
            )
            missing = ("The candidate-local assumption needs recursive validation.",)
        elif self.global_outcome == "inconclusive":
            missing = (
                "The comparative evidence does not resolve which open "
                "candidate introduced the active defect.",
            )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome=self.global_outcome,
                reason="Scripted global candidate comparison.",
                assessments=tuple(assessments),
                selected_candidate_refs=selected,
                expansion_requests=expansion,
                decisive_evidence_refs=decisive,
                missing_evidence=missing,
                confidence=0.9 if self.global_outcome != "inconclusive" else 0.0,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": request.active_defect.fingerprint,
                    "active_focus_text_hash": request.active_focus_text_hash,
                },
            ),
            0,
        )


class BoundedConfirmingJudge(BoundedJudgeCapability):
    def __init__(self):
        self.transport = ScriptedTransport([])
        self.step_allowances = []
        self.confirmation_allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_allowances.append(max_physical_requests)
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                step(
                    request.current_node.ref,
                    status="unknown",
                    missing=("judge_request_budget_exhausted",),
                ),
                0,
            )
        self.transport.request_count += 1
        return BoundedJudgeCallResult(
            RecursiveRootRankingTest._confirmation_step(request), 1
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_allowances.append(max_physical_requests)
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(
                    request.candidate_ref, "judge_request_budget_exhausted"
                ),
                0,
            )
        self.transport.request_count += 1
        node_content = request.candidate_reference["content"]
        excerpt = "The decision is incomplete."
        if excerpt not in node_content:
            excerpt = "premature closure"
        return BoundedJudgeCallResult(
            RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt=excerpt,
                reason="The candidate contains the tracked defect.",
                counterfactual=confirmation_counterfactual_for(
                    request.candidate_ref, "confirmed"
                ),
                confidence=0.9,
                evidence_refs=[request.candidate_ref],
            ),
            1,
        )


class CounterlessBoundedConfirmingJudge(BoundedJudgeCapability):
    def __init__(self):
        self.physical_calls = 0
        self.allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.allowances.append(("step", max_physical_requests))
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                step(request.current_node.ref, status="unknown", missing=("budget_exhausted",)),
                0,
            )
        self.physical_calls += 1
        return BoundedJudgeCallResult(
            RecursiveRootRankingTest._confirmation_step(request), 1
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.allowances.append(("confirmation", max_physical_requests))
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(request.candidate_ref, "budget_exhausted"), 0
            )
        self.physical_calls += 1
        return BoundedJudgeCallResult(
            RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt="The decision is incomplete.",
                reason="The candidate contains the tracked defect.",
                counterfactual=confirmation_counterfactual_for(
                    request.candidate_ref, "confirmed"
                ),
                confidence=0.9,
                evidence_refs=[request.candidate_ref],
            ),
            1,
        )


class ScriptedTransport:
    model = "offline-test-model"
    max_tokens = 2048
    repair_max_tokens = 512
    thinking_config = None

    def __init__(self, responses):
        self.responses = list(responses)
        self.request_count = 0
        self.calls = []
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

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
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise TransportCallError(value, physical_requests=1)
        return TransportCallResult(str(value), physical_requests=1)


class UnboundedProviderJudge:
    """Protocol-compatible Judge whose one logical call spends two requests."""

    def __init__(self):
        self.transport = ScriptedTransport([])
        self.calls = []

    def judge_step(self, request):
        self.calls.append(request)
        self.transport.request_count += 2
        return step(request.current_node.ref, introduction=True)


class LegacyOfflineJudge:
    def __init__(self):
        self.calls = []

    def judge_step(self, request):
        self.calls.append(request)
        return step(request.current_node.ref, introduction=True)


def single_node_trace(*, content: str = "", hydrated_artifacts=None) -> dict:
    data = {
        "failure_type": "incomplete_decision",
        "expected": "The decision covers the complete requirement.",
        "actual": "The decision is incomplete.",
        "mechanism": "premature closure",
        "scope": "repository_reasoning",
    }
    if content:
        data["content"] = content
    if hydrated_artifacts is not None:
        data["hydrated_artifacts"] = hydrated_artifacts
    return {
        "case_id": "single-node-case",
        "records": [
            {
                "record_id": "only",
                "component": "agent",
                "event_type": "decision",
                "data": data,
            }
        ],
    }


def valid_single_node_payload() -> dict:
    state = RecursiveAnalysisState.create(
        graph=TraceGraph.from_trace(single_node_trace()),
        start_refs=["record:only"],
        objective="Find the defect.",
        analysis_perspective="Find the best-supported causal explanation.",
    )
    hypothesis = state.ledger.snapshot()[0]
    defect_fingerprint = next(iter(state.defect_states))
    return {
        "current_node_ref": "record:only",
        "current_defect_status": "present",
        "current_defect_reason": "The decision itself contains the incomplete reasoning.",
        "predecessors": [],
        "candidate_introduction": True,
        "missing_evidence": [],
        "suggested_investigation": {
            "action": "request_root_confirmation",
            "arguments": {
                "hypothesis_id": hypothesis["hypothesis_id"],
                "candidate_ref": "record:only",
                "defect_fingerprint": defect_fingerprint,
            },
            "reason": "Independently confirm the introduction candidate.",
        },
        "confidence": 0.9,
    }


class RecursiveTraversalTest(unittest.TestCase):
    def test_process_confirmation_rebuilds_fact_only_context(self):
        defect = DefectState.create(
            label="candidate_local_process_defect",
            expected="The repair is delivered.",
            actual="No repair was delivered.",
            mechanism="An implementation commitment was not materialized.",
            scope="candidate_local_process_execution",
        )
        cue = {
            "schema": "candidate-commitment-cues/v1",
            "candidate_ref": "record:decision",
            "candidate_reference": {
                "raw_ref": "record:decision",
                "resolved_ref": "record:decision",
                "resolution_status": "resolved",
                "provenance_class": "recorded",
            },
            "cue_count": 1,
            "cues": [
                {
                    "cue_id": "commitment_cue:test",
                    "verbatim_excerpt": "I will implement the repair.",
                    "source_artifact_id": "artifact_unenveloped",
                }
            ],
        }
        trajectory = {
            "schema": "candidate-process-trajectory/v1",
            "candidate_ref": "record:decision",
            "candidate_reference": {
                "raw_ref": "record:decision",
                "resolved_ref": "record:decision",
                "resolution_status": "resolved",
                "provenance_class": "recorded",
            },
            "post_candidate_episode_count": 1,
            "episode_summaries": [
                {
                    "episode_ref": "progress_episode:test",
                    "reference": {
                        "raw_ref": "progress_episode:test",
                        "resolved_ref": "progress_episode:test",
                        "resolution_status": "resolved",
                        "provenance_class": "reconstructed",
                    },
                    "mutation_count": 0,
                    "member_summaries": [
                        {"ref": "record:unenveloped_member"}
                    ],
                }
            ],
        }

        with patch.object(
            recursive_analyzer_module,
            "candidate_commitment_cue_context",
            return_value=cue,
        ), patch.object(
            recursive_analyzer_module,
            "candidate_process_trajectory_context",
            return_value=trajectory,
        ):
            facts = recursive_analyzer_module._process_confirmation_factual_context(
                graph=object(),
                candidate_ref="record:decision",
                path=("record:decision", "record:outcome"),
                defect_state=defect,
            )

        self.assertEqual(
            facts["schema"],
            "candidate-process-confirmation-facts/v1",
        )
        projected_cue = facts["candidate_commitment_cues"]["cues"][0]
        self.assertNotIn("source_artifact_id", projected_cue)
        projected_episode = facts["candidate_process_trajectory"][
            "episode_summaries"
        ][0]
        self.assertNotIn("member_summaries", projected_episode)
        self.assertEqual(projected_episode["mutation_count"], 0)
        self.assertNotIn("process_assessment", facts)

    def test_present_defective_dead_end_is_explicitly_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "unrelated"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.step_judgments[0].current_defect_status, "present")
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertIn("record:change", report.unresolved_refs)
        self.assertIn(
            "defective_dead_end",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_queue_exhaustion_is_not_no_defect_after_any_present_judgment(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                ),
                "record:decision": step("record:decision", status="absent"),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(any(item.current_defect_status == "present" for item in report.step_judgments))
        self.assertTrue(report.unresolved_hypotheses)
        improvement = report.metadata["trace_improvement_report"]
        self.assertIn(
            "competing_hypotheses_unresolved",
            [item["gap_type"] for item in improvement["analysis_gaps"]],
        )
        self.assertNotIn(
            "competing_hypotheses_unresolved",
            [item["gap_type"] for item in improvement["blocking_gaps"]],
        )
        self.assertTrue(improvement["recommended_attribution_changes"])
        self.assertFalse(any(
            "independent_comparison" in item["change"]
            for item in improvement["recommended_trace_changes"]
        ))

    def test_same_defect_competing_predecessors_get_distinct_hypotheses(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": step("record:decision", introduction=True),
                "record:context": step("record:context", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=3).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Compare competing same-defect explanations.",
        )

        branch_hypotheses = {
            item.candidate_root_ref: item
            for item in report.hypotheses
            if item.candidate_root_ref in {"record:decision", "record:context"}
        }
        self.assertEqual(set(branch_hypotheses), {"record:decision", "record:context"})
        self.assertNotEqual(
            branch_hypotheses["record:decision"].hypothesis_id,
            branch_hypotheses["record:context"].hypothesis_id,
        )
        self.assertTrue(
            all(item.supporting_evidence for item in branch_hypotheses.values())
        )
        bindings = report.metadata["introduction_bindings"]
        self.assertEqual(
            {(item["candidate_ref"], item["hypothesis_id"]) for item in bindings},
            {
                (ref, hypothesis.hypothesis_id)
                for ref, hypothesis in branch_hypotheses.items()
            },
        )

    def test_same_defect_alternatives_obey_hypothesis_budget(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=1).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Compare competing same-defect explanations.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["hypotheses"], 2)
        self.assertEqual(
            [item.current_node.ref for item in judge.requests], ["record:change"]
        )
        self.assertEqual(report.introduction_candidates, ())

    def test_unrelated_candidate_is_retained_as_hypothesis_opposition(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "unrelated"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        change_hypothesis = next(
            item for item in report.hypotheses if item.candidate_root_ref == "record:change"
        )
        self.assertEqual(
            [item.ref for item in change_hypothesis.opposing_evidence],
            ["record:decision"],
        )

    def test_recursive_state_create_uses_default_hypothesis_budget(self):
        graph = TraceGraph.from_trace(observed_trace())

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
            analysis_perspective="Improve repository reasoning.",
        )

        self.assertTrue(state.frontier)

    def test_empty_start_refs_fall_back_to_graph_defaults(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=[],
            objective="Find the defect.",
        )

        self.assertEqual(report.start_refs, ("record:observed_defect",))
        self.assertEqual([item.current_node.ref for item in judge.requests], ["record:change"])

    def test_trace_without_analysis_start_is_inconclusive(self):
        report = AgenticRecursiveAnalyzer(judge=ScriptedCausalJudge({})).analyze(
            TraceGraph.from_trace({"case_id": "empty", "records": []}),
            objective="Find the defect.",
        )

        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertIn("analysis_start_missing", report.metadata["unresolved_branches"][0]["reason"])

    def test_analyzer_follows_transformed_defect_to_agent_decision(self):
        transformed = DefectState.create(
            label="incomplete_plan",
            expected="The plan covers the complete parser contract.",
            actual="The plan stops after the first search result.",
            mechanism="premature search closure",
            scope="repository_reasoning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                        ),
                    ),
                ),
                ("record:decision", "incomplete_plan"): step(
                    "record:decision", introduction=True
                ),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=16).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why the required method is missing.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(report.introduction_candidates[0].ref, "record:decision")
        self.assertIn("defect_transformation", [item.relation for item in report.causal_relations])
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.confirmed_roots, ())
        self.assertIn(
            ("record:decision", "record:change", "record:observed_defect"),
            report.taint_paths,
        )

    def test_analyzer_recurses_into_a_material_contributing_condition(self):
        transformed = DefectState.create(
            label="incorrect_cancellation_assumption",
            expected="The plan models process-level SIGINT cleanup correctly.",
            actual="The plan assumes task cancellation is equivalent to process SIGINT.",
            mechanism="the authored assumption shaped implementation and verification",
            scope="repository_reasoning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "contributing_condition",
                            upstream_defect=transformed,
                        ),
                    ),
                ),
                ("record:decision", "incorrect_cancellation_assumption"): step(
                    "record:decision", introduction=True
                ),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=16).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why process-level cancellation cleanup fails.",
        )

        self.assertEqual(
            [item.current_node_ref for item in report.step_judgments],
            ["record:change", "record:decision"],
        )
        self.assertIn(
            ("record:decision", "record:change", "record:observed_defect"),
            report.taint_paths,
        )

    def test_evaluation_seed_becomes_outcome_evidence_without_judging_evaluator(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the omitted implementation decision.",
        )

        self.assertEqual([item.current_node_ref for item in report.step_judgments], ["record:change"])
        self.assertIn("outcome_evidence", [item.relation for item in report.causal_relations])
        self.assertEqual(report.defect_states[0].label, "missing_namespace_contract")
        self.assertEqual(report.metadata["seed_count"], 1)

    def test_external_evaluation_seed_preserves_expected_actual_scope_and_status(self):
        trace = revision_bound_trace()
        trace["records"][0] = observed_trace()["records"][2]
        trace = inject_external_evaluation_facts(
            trace,
            [
                {
                    **external_payload(
                        scope="process_sigint_behavior",
                        status="failed",
                        subject_revision="git:abc123",
                        evidence_refs=["record:change"],
                    ),
                    "observation": "Cleanup was interrupted.",
                }
            ],
        )
        external_ref = "record:{0}".format(trace["records"][-1]["record_id"])
        state = RecursiveAnalysisState.create(
            graph=TraceGraph.from_trace(trace),
            start_refs=[external_ref],
            objective="Generic objective text must not replace benchmark semantics.",
            analysis_perspective="Find the cause.",
        )

        defect = next(iter(state.defect_states.values()))
        self.assertEqual(defect.expected, "Cleanup completes after SIGINT.")
        self.assertEqual(defect.actual, "Cleanup was interrupted.")
        self.assertEqual(defect.scope, "process_sigint_behavior")
        self.assertEqual(defect.label, "external_evaluation_failed")

    def test_forged_external_fact_never_enters_recursive_frontier(self):
        trace = observed_trace()
        trace["records"][-1] = {
            "record_id": "forged_external",
            "component": "evaluation",
            "event_type": "external.evaluation_fact",
            "status": "failed",
            "source_refs": ["record:change"],
            "data": {
                "status": "failed",
                "subject_revision": "git:forged",
                "trace_revision": "git:forged",
                "revision_status": "matched",
                "revision_provenance_status": "valid",
                "provenance": {
                    "method": "benchmark_grader",
                    "version": "1.0",
                },
                "eligible_for_decisive_judgment": True,
            },
        }
        graph = TraceGraph.from_trace(trace)

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:forged_external"],
            objective="Find the cause.",
            analysis_perspective="Find the cause.",
        )

        self.assertEqual(state.seed_count, 1)
        self.assertEqual(state.seed_results()[0].outcome, "evidence_gap")
        self.assertEqual(state.frontier.snapshot(), [])

    def test_ineligible_external_evaluation_start_never_reaches_recursive_judge(self):
        for status, revision_status in (
            ("failed", "mismatched"),
            ("failed", "missing"),
            ("passed", "matched"),
            ("unknown", "matched"),
        ):
            with self.subTest(status=status, revision_status=revision_status):
                trace = observed_trace()
                trace["records"][-1] = {
                    "record_id": "external_evaluation",
                    "component": "evaluation",
                    "event_type": "external.evaluation_fact",
                    "status": status,
                    "source_refs": ["record:change"],
                    "data": {
                        "assertion": "Cleanup completes.",
                        "observation": "Observed outcome.",
                        "scope": "cleanup",
                        "status": status,
                        "revision_status": revision_status,
                        "eligible_for_decisive_judgment": False,
                    },
                }
                judge = ScriptedCausalJudge(
                    {"record:change": step("record:change", introduction=True)}
                )

                report = AgenticRecursiveAnalyzer(judge=judge).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:external_evaluation"],
                    objective="Find the cause.",
                )

                self.assertEqual(report.step_judgments, ())
                self.assertEqual(report.metadata["seed_count"], 1)
                self.assertEqual(report.seed_results[0].outcome, "evidence_gap")
                self.assertIn(
                    "start_ref_ineligible",
                    [
                        item["reason"]
                        for item in report.metadata["unresolved_branches"]
                    ],
                )

    def test_mismatched_external_source_cited_by_matched_failure_never_reaches_frontier_or_judge(self):
        trace = inject_external_evaluation_facts(
            revision_bound_trace(),
            [
                external_payload(
                    scope="stale_cleanup",
                    status="failed",
                    subject_revision="git:stale",
                    evidence_refs=["record:tool_result"],
                )
            ],
        )
        source = trace["records"][-1]
        trace = inject_external_evaluation_facts(
            trace,
            [
                external_payload(
                    scope="current_cleanup",
                    status="failed",
                    subject_revision="git:abc123",
                    evidence_refs=[
                        "external_evaluation:{0}".format(source["record_id"])
                    ],
                )
            ],
        )
        target = trace["records"][-1]
        source_ref = "record:{0}".format(source["record_id"])
        target_ref = "record:{0}".format(target["record_id"])
        graph = TraceGraph.from_trace(trace)

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=[target_ref],
            objective="Find the cleanup failure cause.",
            analysis_perspective="Find the cause.",
        )
        judge = ScriptedCausalJudge({source_ref: step(source_ref, introduction=True)})
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=[target_ref],
            objective="Find the cleanup failure cause.",
        )

        self.assertNotIn(
            source_ref,
            [item["node_ref"] for item in state.frontier.snapshot()],
        )
        self.assertNotIn(
            source_ref,
            [candidate.ref for candidate in state.causal_candidates],
        )
        self.assertNotIn(
            source_ref,
            [request.current_node.ref for request in judge.requests],
        )
        self.assertNotIn(source_ref, report.visited_order)

    def test_matched_passed_external_fact_is_counterevidence_but_never_seed_frontier_or_root(self):
        trace = inject_external_evaluation_facts(
            revision_bound_trace(),
            [
                external_payload(
                    scope="passing_cleanup",
                    status="passed",
                    subject_revision="git:abc123",
                    evidence_refs=["record:tool_result"],
                )
            ],
        )
        passed = trace["records"][-1]
        trace = inject_external_evaluation_facts(
            trace,
            [
                external_payload(
                    scope="failing_cleanup",
                    status="failed",
                    subject_revision="git:abc123",
                    evidence_refs=[
                        "external_evaluation:{0}".format(passed["record_id"])
                    ],
                )
            ],
        )
        failed = trace["records"][-1]
        passed_ref = "record:{0}".format(passed["record_id"])
        failed_ref = "record:{0}".format(failed["record_id"])
        graph = TraceGraph.from_trace(trace)

        passed_seed = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=[passed_ref],
            objective="Find the cleanup failure cause.",
            analysis_perspective="Find the cause.",
        )
        failed_seed = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=[failed_ref],
            objective="Find the cleanup failure cause.",
            analysis_perspective="Find the cause.",
        )

        self.assertEqual(passed_seed.seed_count, 1)
        self.assertEqual(passed_seed.seed_results()[0].outcome, "evidence_gap")
        self.assertEqual(passed_seed.frontier.snapshot(), [])
        self.assertTrue(graph.evidence_eligible(passed_ref))
        self.assertFalse(graph.analysis_start_eligible(passed_ref))
        self.assertIn(
            passed_ref,
            [candidate.ref for candidate in failed_seed.causal_candidates],
        )
        self.assertNotIn(
            passed_ref,
            [item["node_ref"] for item in failed_seed.frontier.snapshot()],
        )
        self.assertFalse(root_candidate_eligible(graph.nodes[passed_ref]))

    def test_evaluation_seed_prefers_progress_navigation_over_parallel_outcome_surfaces(self):
        trace = {
            "case_id": "progress-seed-priority",
            "records": [
                {
                    "record_id": "response",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "All tests pass."},
                },
                {
                    "record_id": "tool_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {"output": "PASS"},
                },
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {"offline_only": True, "member_refs": []},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": [
                        "record:response",
                        "record:tool_result",
                        "record:progress",
                    ],
                    "data": {
                        "defect_type": "false_success",
                        "expected": "The failure is detected.",
                        "actual": "The agent reports success.",
                    },
                },
            ],
        }
        judge = ScriptedCausalJudge({})

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why success was reported.",
        )

        self.assertEqual(
            [request.current_node.ref for request in judge.requests],
            [],
        )
        self.assertEqual(
            {candidate.ref for candidate in report.causal_candidates},
            {"record:response", "record:tool_result", "record:progress"},
        )

    def test_clean_progress_navigation_routes_a_transformed_defect_to_a_decision(self):
        trace = {
            "case_id": "progress-routing",
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"decision_type": "reasoning_block", "rationale": "Assume cancellation is equivalent."},
                },
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {
                        "offline_only": True,
                        "member_refs": ["record:decision"],
                        "candidate_member_refs": ["record:decision"],
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:progress"],
                    "data": {"defect_type": "sigint_cleanup_failure"},
                },
            ],
        }
        judge = ConfirmingScriptedJudge(
            {
                ("record:decision", "candidate_local_process_defect"): (
                    lambda request: step(
                        request.current_node.ref,
                        introduction=True,
                        suggested={
                            "action": "request_root_confirmation",
                            "arguments": {
                                "hypothesis_id": request.recursive_context[
                                    "active_hypothesis_id"
                                ],
                                "candidate_ref": request.current_node.ref,
                                "defect_fingerprint": (
                                    request.defect_state.fingerprint
                                ),
                            },
                            "reason": (
                                "Independently verify this process candidate."
                            ),
                        },
                    )
                ),
            },
            {
                "record:decision": RootConfirmation(
                    candidate_ref="record:decision",
                    status="unknown",
                    reason="Exercise the factual confirmation request.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "unknown",
                    ),
                    counterfactual_status="unknown",
                    factor_role="unknown",
                    process_confirmation_assessment={
                        "candidate_role": "bounded_investigation",
                        "commitment_cue_disposition": "not_applicable",
                        "commitment_status": "not_applicable",
                        "trajectory_relation": "unknown",
                        "intervention_scope": "unknown",
                        "task_precondition_disposition": "ambiguous",
                        "active_process_defect_after_intervention": "unknown",
                        "downstream_failure_after_intervention": "unknown",
                        "reason": "The synthetic fixture leaves causality unknown.",
                        "evidence_refs": [
                            "record:decision",
                            "record:progress",
                        ],
                    },
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the cancellation root cause.",
        )

        self.assertEqual(
            [request.current_node.ref for request in judge.requests],
            ["record:decision"],
        )
        self.assertEqual(len(judge.confirmation_requests), 1)
        confirmation_request = judge.confirmation_requests[0]
        self.assertEqual(
            confirmation_request.recursive_path,
            ("record:decision", "record:observed_defect"),
        )
        self.assertEqual(
            confirmation_request.process_factual_context["schema"],
            "candidate-process-confirmation-facts/v1",
        )

    def test_progress_navigation_judges_the_complete_bounded_candidate_page(self):
        decision_count = 40
        decision_refs = [
            "record:decision_{0}".format(index)
            for index in range(decision_count)
        ]
        trace = {
            "case_id": "progress-routing-complete-page",
            "records": [
                *[
                    {
                        "record_id": "decision_{0}".format(index),
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": "Planning candidate {0}.".format(index),
                        },
                    }
                    for index in range(decision_count)
                ],
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {
                        "offline_only": True,
                        "member_refs": decision_refs,
                        "candidate_member_refs": decision_refs,
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:progress"],
                    "data": {
                        "defect_type": "repair_not_completed",
                        "expected": "The required repair is implemented.",
                        "actual": "The repository remains unchanged.",
                    },
                },
            ],
        }
        judge = ScriptedCausalJudge({})

        AgenticRecursiveAnalyzer(
            judge=judge,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why the repair was not completed.",
        )

        judged_refs = {
            request.current_node.ref for request in judge.requests
        }
        self.assertEqual(judged_refs, set(decision_refs))
        self.assertTrue(
            all(
                request.defect_state.label
                == "candidate_local_process_defect"
                for request in judge.requests
            )
        )
        self.assertTrue(
            all(
                "pre-existing downstream functional defect"
                in request.defect_state.mechanism
                for request in judge.requests
            )
        )

    def test_progress_navigation_prioritizes_recorded_commitment_cue(self):
        decision_refs = [
            "record:ordinary_0",
            "record:ordinary_1",
            "record:committed",
        ]
        graph = TraceGraph.from_trace(
            {
                "case_id": "progress-routing-commitment-priority",
                "records": [
                    {
                        "record_id": "ordinary_0",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": "Inspect the parser implementation.",
                        },
                    },
                    {
                        "record_id": "ordinary_1",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": "Read another reference file.",
                        },
                    },
                    {
                        "record_id": "committed",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": (
                                "I will implement the required parser methods now."
                            ),
                        },
                    },
                    {
                        "record_id": "progress",
                        "component": "progress",
                        "event_type": "progress.episode",
                        "data": {
                            "offline_only": True,
                            "member_refs": decision_refs,
                            "candidate_member_refs": decision_refs,
                        },
                    },
                    {
                        "record_id": "observed_defect",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "source_refs": ["record:progress"],
                        "data": {
                            "defect_type": "repair_not_completed",
                            "expected": "The repair is implemented.",
                            "actual": "The repository remains unchanged.",
                        },
                    },
                ],
            }
        )
        judge = ScriptedCausalJudge({})

        AgenticRecursiveAnalyzer(
            judge=judge,
            max_hypotheses=16,
        ).analyze(
            graph,
            start_refs=["record:observed_defect"],
            objective="Find why the repair was not completed.",
        )

        self.assertEqual(
            judge.requests[0].current_node.ref,
            "record:committed",
        )
        self.assertEqual(
            {request.current_node.ref for request in judge.requests},
            set(decision_refs),
        )

    def test_interrupted_case_failure_seeds_only_the_recorded_process_signal(self):
        trace = {
            "case_id": "recorded-signal-boundary",
            "manifest": {"shutdown_disposition": "interrupted_before_case_completion"},
            "records": [
                {"record_id": "run", "component": "run", "event_type": "run.start"},
                {
                    "record_id": "signal",
                    "component": "runtime",
                    "event_type": "process.signal",
                    "data": {"signal": "SIGTERM"},
                },
                {
                    "record_id": "failed",
                    "component": "run",
                    "event_type": "case.failed",
                    "source_refs": ["record:run", "record:signal"],
                    "data": {
                        "shutdown_signal": "SIGTERM",
                        "shutdown_disposition": "interrupted_before_case_completion",
                    },
                },
            ],
        }
        judge = ScriptedCausalJudge(
            {"record:signal": step("record:signal", introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:failed"],
            objective="Find why the case was interrupted.",
        )

        self.assertEqual([item.current_node.ref for item in judge.requests], ["record:signal"])
        self.assertEqual([item.ref for item in report.introduction_candidates], ["record:signal"])
        self.assertNotIn("record:failed", report.visited_order)

    def test_legacy_interrupted_failure_without_signal_node_reports_trace_gap(self):
        trace = {
            "case_id": "legacy-signal-boundary",
            "manifest": {"shutdown_disposition": "interrupted_before_case_completion"},
            "records": [
                {"record_id": "run", "component": "run", "event_type": "run.start"},
                {
                    "record_id": "failed",
                    "component": "run",
                    "event_type": "case.failed",
                    "source_refs": ["record:run"],
                    "data": {
                        "shutdown_signal": "SIGINT",
                        "shutdown_disposition": "interrupted_before_case_completion",
                    },
                },
            ],
        }
        judge = ScriptedCausalJudge({})

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:failed"],
            objective="Find why the case was interrupted.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertIn(
            "process_signal_node_missing",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )
        self.assertIn(
            "process_signal_node_missing",
            [
                item["gap_type"]
                for item in report.metadata["trace_improvement_report"]["blocking_gaps"]
            ],
        )

    def test_same_defect_propagation_preserves_defect_fingerprint(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                ),
                "record:decision": step("record:decision", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the omitted implementation decision.",
        )

        fingerprints = {request.defect_state.fingerprint for request in judge.requests}
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(report.visited_order, ("record:change", "record:decision"))

    def test_unknown_branch_is_unresolved_not_root(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", status="unknown", missing=("artifact truncated",))}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.unresolved_refs, ("record:change",))
        self.assertIn("artifact truncated", report.metadata["unresolved_branches"][0]["details"])

    def test_internal_judge_retry_diagnostic_is_not_routed_to_investigation_tools(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    status="unknown",
                    missing=("judge_validation_error",),
                    suggested={"kind": "judge_retry", "reason": "invalid Provider payload"},
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        reasons = [item["reason"] for item in report.metadata["unresolved_branches"]]
        self.assertNotIn("investigation_rejected", reasons)
        self.assertEqual(report.metadata.get("investigation_journal", ()), ())

    def test_navigation_progress_episode_routes_to_concrete_decision_instead_of_becoming_root(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "recursive-progress-root-policy",
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {
                            "decision_type": "reasoning_block",
                            "rationale": "Keep searching instead of implementing.",
                            "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                        },
                    }
                ],
            }
        )
        progress_ref = next(
            ref for ref, node in graph.nodes.items() if node.event_type == "progress.episode"
        )
        judge = ScriptedCausalJudge(
            {progress_ref: step(progress_ref, introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=[progress_ref],
            objective="Find why implementation was not delivered.",
        )

        self.assertEqual(report.introduction_candidates, ())
        self.assertEqual(
            [request.current_node.ref for request in judge.requests],
            ["record:decision"],
        )

    def test_causal_step_shortlists_eight_candidates_and_records_omitted_refs(self):
        records = [
            {
                "record_id": "candidate_{0}".format(index),
                "component": "processor",
                "event_type": "decision",
                "data": {"rationale": "Candidate {0}".format(index)},
            }
            for index in range(10)
        ]
        records.append(
            {
                "record_id": "current",
                "component": "processor",
                "event_type": "change",
                "data": {
                    "failure_type": "bad_result",
                    "expected": "correct",
                    "actual": "incorrect",
                },
            }
        )
        graph = TraceGraph.from_trace(
            {
                "case_id": "candidate-shortlist",
                "records": records,
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": "candidate_{0}".format(index)},
                        "to": {"type": "record", "id": "current"},
                        "relation": "candidate_informed_outcome",
                        "evidence_type": "confirmed",
                        "confidence": 0.9,
                        "eligible_for_attribution": True,
                    }
                    for index in range(10)
                ],
            }
        )
        judge = ScriptedCausalJudge({"record:current": step("record:current", status="absent")})

        AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=["record:current"],
            objective="Check the observed result.",
        )

        request = judge.requests[0]
        self.assertEqual(len(request.candidates), 8)
        pagination = request.recursive_context["candidate_pagination"]
        self.assertEqual(pagination["retrieved_count"], 10)
        self.assertEqual(pagination["offered_count"], 8)
        self.assertEqual(len(pagination["omitted_candidate_refs"]), 2)

    def test_missing_predecessor_evidence_does_not_propagate(self):
        unsupported = PredecessorAssessment(
            ref="record:decision",
            relation="same_defect_propagation",
            reason="The predecessor might carry the defect, but its artifact is missing.",
            confidence=0.4,
            recurse=True,
            evidence_refs=(),
            missing_evidence=("decision artifact missing",),
        )
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", predecessors=(unsupported,))}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual([item.current_node.ref for item in judge.requests], ["record:change"])
        self.assertIn("record:decision", report.unresolved_refs)
        self.assertIn(
            "predecessor_evidence_missing",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_fabricated_predecessor_is_unresolved_instead_of_crashing(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:not_in_trace", "same_defect_propagation"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertIn("record:not_in_trace", report.unresolved_refs)
        self.assertIn(
            "predecessor_ref_unresolved",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_absent_seed_path_finishes_as_no_defect(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Check whether the observed gap is supported.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(report.unresolved_refs, ())
        self.assertEqual(report.introduction_candidates, ())

    def test_unknown_predecessor_does_not_block_an_absent_current_defect(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    status="absent",
                    predecessors=(relation("record:decision", "unknown"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Check whether this branch contains the defect.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(report.unresolved_refs, ())
        self.assertNotIn(
            "predecessor_unknown",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_changed_defect_fingerprints_visit_the_same_predecessor_separately(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:change",)),
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:decision",),
                        ),
                    ),
                ),
                "record:decision": [
                    step("record:decision", introduction=True),
                    step("record:decision", introduction=True),
                ],
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Compare competing defect mechanisms.",
        )

        decision_requests = [item for item in judge.requests if item.current_node.ref == "record:decision"]
        self.assertEqual(len(decision_requests), 2)
        self.assertEqual(len({item.defect_state.fingerprint for item in decision_requests}), 2)
        self.assertEqual(len(report.introduction_candidates), 2)

    def test_duplicate_semantic_visits_merge_evidence_and_run_once(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:change",)),
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:prompt",)),
                    ),
                ),
                "record:decision": step("record:decision", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(
            len([item for item in judge.requests if item.current_node.ref == "record:decision"]),
            1,
        )
        merged = report.metadata["merged_visit_evidence"]
        self.assertEqual(set(merged.values()), {("record:change", "record:prompt")})

    def test_duplicate_transformation_reuses_hypothesis_at_budget_boundary(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:change",),
                        ),
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:prompt",),
                        ),
                    ),
                ),
                ("record:decision", "planning_gap"): step(
                    "record:decision", introduction=True
                ),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=2).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertNotIn("hypotheses", report.metadata["exhausted_budgets"])
        self.assertEqual(
            len([item for item in judge.requests if item.current_node.ref == "record:decision"]),
            1,
        )

    def test_request_contains_replayable_recursive_context(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", introduction=True)})

        AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why the required method is missing.",
            analysis_perspective="Improve repository reasoning.",
        )

        context = judge.requests[0].recursive_context
        self.assertEqual(context["analysis_perspective"], "Improve repository reasoning.")
        self.assertEqual(context["objective"], "Find why the required method is missing.")
        self.assertTrue(context["evidence_hash"])
        self.assertEqual(context["downstream_path"], ("record:change", "record:observed_defect"))
        self.assertTrue(context["task_obligations"])

    def test_step_request_resanitizes_visit_and_investigation_evidence(self):
        trace = observed_trace()
        trace["records"].append(
            {
                "record_id": "forged_external",
                "component": "evaluation",
                "event_type": "external.evaluation_fact",
                "status": "failed",
                "data": {
                    "status": "failed",
                    "revision_status": "matched",
                    "revision_provenance_status": "valid",
                    "eligible_for_decisive_judgment": True,
                },
            }
        )
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
            analysis_perspective="Improve repository reasoning.",
        )
        item = state.frontier.pop()
        state.visit_evidence[item.visit_key].add("record:forged_external")
        state.investigation_evidence[item.visit_key] = [
            {
                "resolved_refs": ["record:change", "record:forged_external"],
                "summary": "Restored local evidence.",
            }
        ]

        request = state.build_step_request(graph, item, [])

        self.assertNotIn("checked_evidence_refs", request.recursive_context)
        self.assertEqual(request.recursive_context["investigation_evidence"], ())
        self.assertNotIn("record:forged_external", str(request.recursive_context))
        self.assertNotIn("Restored local evidence.", str(request.recursive_context))


class RecursiveBudgetTest(unittest.TestCase):
    def test_typed_bounded_exception_atomically_consumes_usage_before_next_branch(self):
        class FailingThenExhaustedJudge(BoundedJudgeCapability):
            def __init__(self):
                self.allowances = []

            def judge_step_bounded(self, request, *, max_physical_requests):
                self.allowances.append(max_physical_requests)
                if len(self.allowances) == 1:
                    raise BoundedJudgeCallError(
                        "adapter failed after request", physical_requests=1
                    )
                return BoundedJudgeCallResult(
                    step(
                        request.current_node.ref,
                        status="unknown",
                        missing=("judge_request_budget_exhausted",),
                    ),
                    0,
                )

        trace = {
            "case_id": "two-branch-budget",
            "records": [
                {
                    "record_id": "first",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"failure_type": "first defect"},
                },
                {
                    "record_id": "second",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"failure_type": "second defect"},
                },
            ],
        }
        judge = FailingThenExhaustedJudge()

        report = AgenticRecursiveAnalyzer(
            judge=judge, max_judge_requests=1
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:first", "record:second"],
            objective="Inspect both failures.",
        )

        self.assertEqual(judge.allowances, [1, 0])
        self.assertEqual(report.metadata["physical_judge_request_count"], 1)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_unbounded_provider_judge_is_rejected_before_any_side_effect(self):
        judge = UnboundedProviderJudge()

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(judge.calls, [])
        self.assertEqual(judge.transport.request_count, 0)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 0)
        self.assertIn(
            "judge_budget_unenforceable",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )
        self.assertEqual(report.introduction_candidates, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_explicit_adapter_preserves_legacy_zero_transport_judge(self):
        legacy = LegacyOfflineJudge()
        judge = OfflineCausalJudgeAdapter(legacy)

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(len(legacy.calls), 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertNotIn("judge_requests", report.metadata["exhausted_budgets"])
        self.assertEqual([item.ref for item in report.introduction_candidates], ["record:only"])

    def test_ordinary_content_text_does_not_consume_artifact_budget(self):
        judge = ScriptedCausalJudge({"record:only": step("record:only", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=8).analyze(
            TraceGraph.from_trace(single_node_trace(content="x" * 64)),
            start_refs=["record:only"],
            objective="Check the decision.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["artifact_bytes"], 0)
        self.assertNotIn("artifact_bytes", report.metadata["exhausted_budgets"])

    def test_duplicate_unindexed_artifact_payload_is_excluded_from_budget(self):
        artifact = {
            "artifact_id": "artifact-1",
            "content_hash": "sha256:{0}".format(
                hashlib.sha256(b"abcdef").hexdigest()
            ),
            "path": "artifacts/decision.txt",
            "content": "abcdef",
        }
        judge = ScriptedCausalJudge({"record:only": step("record:only", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=6).analyze(
            TraceGraph.from_trace(
                single_node_trace(hydrated_artifacts=[artifact, dict(artifact)])
            ),
            start_refs=["record:only"],
            objective="Check the decision artifact.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["artifact_bytes"], 0)
        self.assertNotIn("artifact_bytes", report.metadata["exhausted_budgets"])

    def test_one_physical_request_budget_allows_judgment_but_not_confirmation(self):
        transport = ScriptedTransport([json.dumps(valid_single_node_payload())])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.metadata["logical_judge_call_count"], 2)
        self.assertEqual([item.ref for item in report.introduction_candidates], ["record:only"])
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)

    def test_cache_hit_costs_zero_physical_requests_with_zero_remaining_budget(self):
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport([json.dumps(valid_single_node_payload())])
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            graph = TraceGraph.from_trace(single_node_trace())
            AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
            )

            report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
            )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 2)
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)

    def test_repair_is_blocked_at_exact_physical_request_boundary(self):
        transport = ScriptedTransport(["not-json"])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_frontier_budget_counts_items_that_stop_at_depth_limit(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge, max_depth=0, max_frontier_items=2
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["processed_frontier_items"], 2)
        self.assertEqual(report.metadata["exhausted_budgets"]["depth"], 1)
        self.assertEqual(report.metadata["exhausted_budgets"]["frontier_items"], 1)

    def test_depth_limit_is_explicit_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_depth=0).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["depth"], 1)
        self.assertIn("record:decision", report.unresolved_refs)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_frontier_limit_marks_remaining_items_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=1).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["frontier_items"], 2)
        self.assertEqual(set(report.unresolved_refs), {"record:decision", "record:context"})

    def test_hypothesis_limit_rejects_new_transformation_as_unresolved(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                        ),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=1).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["hypotheses"], 1)
        self.assertIn("record:decision", report.unresolved_refs)

    def test_unindexed_artifact_does_not_consume_byte_budget(self):
        judge = ScriptedCausalJudge({})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=8).analyze(
            TraceGraph.from_trace(observed_trace(artifact_content="a" * 64)),
            start_refs=["record:decision"],
            objective="Inspect the decision artifact.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["artifact_bytes"], 0)
        self.assertNotIn("artifact_bytes", report.metadata["exhausted_budgets"])

    def test_explicit_offline_judge_uses_no_physical_request_budget(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", introduction=True)})

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertNotIn("judge_requests", report.metadata["exhausted_budgets"])

    def test_provider_circuit_is_unresolved_and_preserves_no_root(self):
        judge = ScriptedCausalJudge(
            {"record:change": JudgeProviderUnavailable("provider circuit open")}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(report.metadata["provider_circuit"]["open"])
        self.assertEqual(report.metadata["exhausted_budgets"]["provider_circuit"], 1)

    def test_analysis_does_not_mutate_input_graph(self):
        graph = TraceGraph.from_trace(observed_trace(artifact_content="semantic evidence"))
        nodes_before = copy.deepcopy(graph.nodes)
        hydration_before = copy.deepcopy(graph.artifact_hydration)
        judge = ScriptedCausalJudge({"record:decision": step("record:decision", status="absent")})

        AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=["record:decision"],
            objective="Check the decision.",
        )

        self.assertEqual(graph.nodes, nodes_before)
        self.assertEqual(graph.artifact_hydration, hydration_before)


class RecursiveRootRankingTest(unittest.TestCase):
    @staticmethod
    def _confirmation_step(request):
        return step(
            request.current_node.ref,
            introduction=True,
            suggested={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently verify this introduction candidate.",
            },
        )

    def test_unknown_confirmation_never_becomes_root(self):
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {
                "record:only": RootConfirmation.unknown(
                    "record:only", "missing candidate-local evidence"
                )
            },
        )
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertIn(
            "root_confirmation_missing_evidence",
            {
                item["gap_type"]
                for item in report.metadata["trace_improvement_report"]["blocking_gaps"]
            },
        )
        self.assertEqual(judge.confirmation_requests.__len__(), 1)

    def test_queued_confirmation_rejects_temporal_metadata_path(self):
        trace = observed_trace()
        trace["dataflow_edges"][0]["evidence_type"] = "temporal_advisory"
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="The decision omitted required search coverage.",
                    reason="The decision is a necessary local root.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(len(report.confirmations), 1)
        confirmation = report.confirmations[0]
        self.assertEqual(confirmation.status, "unknown")
        self.assertEqual(
            report.seed_results[0].confirmation_identities,
            (confirmation.confirmation_identity,),
        )
        self.assertIn(
            "queued confirmation path lacks a grounded non-temporal edge",
            confirmation.reason,
        )

    def test_queued_confirmation_rejects_raw_temporal_origin_path(self):
        trace = observed_trace()
        trace["dataflow_edges"][0]["edge_origin"] = "offline.temporal_reconstruction"
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="The decision omitted required search coverage.",
                    reason="The decision is a necessary local root.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(len(report.confirmations), 1)
        confirmation = report.confirmations[0]
        self.assertEqual(confirmation.status, "unknown")
        self.assertEqual(
            report.seed_results[0].confirmation_identities,
            (confirmation.confirmation_identity,),
        )
        self.assertIn(
            "queued confirmation path lacks a grounded non-temporal edge",
            confirmation.reason,
        )

    def test_queued_confirmation_rejects_conflicting_raw_provenance(self):
        trace = observed_trace()
        trace["dataflow_edges"][0].update(
            {
                "evidence_type": "confirmed",
                "metadata": {"evidence_type": "temporal_advisory"},
            }
        )
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="The decision omitted required search coverage.",
                    reason="The decision is a necessary local root.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(len(report.confirmations), 1)
        confirmation = report.confirmations[0]
        self.assertEqual(confirmation.status, "unknown")
        self.assertEqual(
            report.seed_results[0].confirmation_identities,
            (confirmation.confirmation_identity,),
        )
        self.assertIn(
            "queued confirmation path lacks a grounded non-temporal edge",
            confirmation.reason,
        )

    def test_queued_confirmation_rejects_malformed_explicit_eligibility(self):
        trace = observed_trace()
        trace["dataflow_edges"][0]["eligible_for_attribution"] = "false"
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="The decision omitted required search coverage.",
                    reason="The decision is a necessary local root.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.confirmed_roots, ())

    def test_rejected_candidate_backtracks_to_independently_confirmed_alternative(self):
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            selected_candidate_refs=("record:decision",),
            global_non_root_roles={
                "record:context": "contributing_condition",
            },
            factor_roles={
                "record:context": "contributing_condition",
            },
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision stopped repository discovery.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.91,
                    evidence_refs=["record:decision"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(
            [root.node_ref for root in report.confirmed_roots], ["record:decision"]
        )
        self.assertEqual(
            [item.node_ref for item in report.contributing_conditions],
            ["record:context"],
        )
        self.assertEqual(report.rejected_candidates, ())
        requests = {
            item.candidate_ref: item
            for item in judge.confirmation_requests
        }
        decision_request = requests["record:decision"]
        competitor_refs = {
            item["candidate_reference"]["resolved_ref"]
            for item in decision_request.competing_hypotheses
        }
        self.assertIn("record:context", competitor_refs)
        self.assertNotIn("record:change", competitor_refs)
        self.assertEqual(
            [
                item["candidate_ref"]
                for item in judge.factor_requests[
                    0
                ].confirmed_root_summaries
            ],
            ["record:decision"],
        )
        self.assertTrue(decision_request.hypothesis_id)
        self.assertEqual(
            decision_request.defect_state.fingerprint,
            next(
                item["defect_fingerprint"]
                for item in report.metadata["introduction_bindings"]
                if item["candidate_ref"] == "record:decision"
            ),
        )
        self.assertNotIn(
            "The decision semantically explains the current defect.",
            json.dumps(decision_request.to_dict()),
        )

    def test_unrelated_rejected_candidate_is_not_published_as_a_factor(self):
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            selected_candidate_refs=("record:decision",),
            global_non_root_roles={
                "record:context": "unrelated",
            },
            factor_roles={"record:context": "unrelated"},
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision stopped repository discovery.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.91,
                    evidence_refs=["record:decision"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(
            [item.node_ref for item in report.rejected_candidates],
            ["record:context"],
        )
        self.assertEqual(report.contributing_conditions, ())
        self.assertEqual(report.amplifying_factors, ())

    def test_every_published_non_root_role_requires_an_active_causal_path(self):
        for role in (
            "contributing_condition",
            "amplifying_factor",
            "unrelated",
        ):
            with self.subTest(role=role):
                judge = FusionScriptedJudge(
                    global_outcome="candidate_roots",
                    selected_candidate_refs=("record:decision",),
                    global_non_root_roles={
                        "record:context": role,
                    },
                    factor_roles={"record:context": role},
                    confirmations={
                        "record:decision": RootConfirmation.confirmed(
                            "record:decision",
                            excerpt=(
                                "Implement only the methods found in the "
                                "first search."
                            ),
                            reason=(
                                "The decision stopped repository discovery."
                            ),
                            counterfactual=confirmation_counterfactual_for(
                                "record:decision",
                                "confirmed",
                            ),
                            confidence=0.91,
                            evidence_refs=["record:decision"],
                        ),
                    },
                )
                trace = observed_trace(branching=True)
                report = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed_defect"],
                    objective="Find why the implementation omitted the method.",
                )
                for mutation in ("disconnected", "reversed", "temporal"):
                    mutated_trace = copy.deepcopy(trace)
                    context_edge = next(
                        edge
                        for edge in mutated_trace["dataflow_edges"]
                        if edge["from"]["id"] == "context"
                    )
                    if mutation == "disconnected":
                        mutated_trace["dataflow_edges"].remove(context_edge)
                    elif mutation == "reversed":
                        context_edge["from"], context_edge["to"] = (
                            context_edge["to"],
                            context_edge["from"],
                        )
                    else:
                        context_edge["relation"] = "temporal_sequence"

                    with self.subTest(role=role, mutation=mutation):
                        with self.assertRaisesRegex(
                            ValueError,
                            (
                                "non-root.*causal edge|confirmation path|"
                                "capsule incoming edges"
                            ),
                        ):
                            _assert_report_grounded_evidence(
                                TraceGraph.from_trace(mutated_trace),
                                report,
                                label="published report",
                            )

    def test_rejected_candidate_with_unknown_counterfactual_stays_unresolved(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation(
                    candidate_ref="record:decision",
                    status="rejected",
                    reason="The counterfactual evidence is incomplete.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "rejected",
                        counterfactual_status="unknown",
                    ),
                    confidence=0.5,
                    counterfactual_status="unknown",
                    factor_role="unknown",
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(report.seed_results[0].outcome, "evidence_gap")
        self.assertIn(
            "root_confirmation_unresolved",
            report.seed_results[0].blocking_reasons,
        )
        self.assertIn("record:decision", report.unresolved_refs)
        self.assertEqual(report.rejected_candidates, ())

    def test_successful_no_defect_does_not_invoke_confirmation(self):
        judge = ConfirmingScriptedJudge(
            {"record:only": step("record:only", status="absent")}, {}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Check the decision.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(judge.confirmation_requests, [])

    def test_step_and_confirmation_share_one_exact_physical_request_budget(self):
        exhausted = BoundedConfirmingJudge()
        unresolved = AgenticRecursiveAnalyzer(
            judge=exhausted, max_judge_requests=1
        ).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(exhausted.step_allowances, [1])
        self.assertEqual(exhausted.confirmation_allowances, [0])
        self.assertEqual(exhausted.transport.request_count, 1)
        self.assertEqual(unresolved.confirmed_roots, ())
        self.assertEqual(unresolved.metadata["judge_request_count"], 1)

        sufficient = BoundedConfirmingJudge()
        confirmed = AgenticRecursiveAnalyzer(
            judge=sufficient, max_judge_requests=2
        ).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(sufficient.step_allowances, [2])
        self.assertEqual(sufficient.confirmation_allowances, [1])
        self.assertEqual(sufficient.transport.request_count, 2)
        self.assertEqual([item.node_ref for item in confirmed.confirmed_roots], ["record:only"])
        self.assertEqual(confirmed.metadata["judge_request_count"], 2)

    def test_global_budget_uses_explicit_physical_delta_without_transport_counter(self):
        judge = CounterlessBoundedConfirmingJudge()

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(judge.allowances, [("step", 1), ("confirmation", 0)])
        self.assertEqual(judge.physical_calls, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.confirmed_roots, ())

    def test_multiple_independently_confirmed_roots_are_ranked_deterministically(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision was independently necessary.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt="The complete compatibility contract is documented here.",
                    reason="The context defect was independently necessary.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:context", "confirmed"
                    ),
                    confidence=0.8,
                    evidence_refs=["record:context"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual([item.node_ref for item in report.confirmed_roots], ["record:decision"])
        self.assertEqual([item.node_ref for item in report.co_roots], ["record:context"])
        self.assertEqual(
            [item["node_ref"] for item in report.to_dict()["root_causes"]],
            ["record:decision", "record:context"],
        )
        for request in judge.confirmation_requests:
            competitor_refs = {
                item["candidate_reference"]["resolved_ref"]
                for item in request.competing_hypotheses
            }
            self.assertLessEqual(competitor_refs, {"record:decision", "record:context"})
            self.assertNotIn("record:change", competitor_refs)

    def test_declared_co_root_blocks_single_root_until_competitor_is_independently_confirmed(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision requires its co-root alternative.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.unknown(
                    "record:context", "The alternative lacks decisive evidence."
                ),
            },
        )
        judge.force_co_root_candidates = {"record:context"}

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_mutual_dominance_cannot_publish_primary_and_co_root(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "outperformed",
            ("record:context", "record:decision"): "outperformed",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_dominance_and_co_root_disagreement_blocks_both_confirmations(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "outperformed",
            ("record:context", "record:decision"): "co_root",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_rejected_and_confirmed_disagreement_blocks_both_confirmations(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "rejected",
            ("record:context", "record:decision"): "co_root",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def _two_confirmed_competitor_judge(self):
        return ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision was independently necessary.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt="The complete compatibility contract is documented here.",
                    reason="The context was independently necessary.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:context", "confirmed"
                    ),
                    confidence=0.8,
                    evidence_refs=["record:context"],
                ),
            },
        )

    def test_confirmation_cannot_cross_bind_hypothesis_defect_or_path_identity(self):
        forged = replace(
            RootConfirmation.confirmed(
                "record:only",
                excerpt="The decision is incomplete.",
                reason="A forged confirmation attempts to cross-bind another branch.",
                counterfactual=confirmation_counterfactual_for(
                    "record:only", "confirmed"
                ),
                confidence=0.9,
                evidence_refs=["record:only"],
            ),
            hypothesis_id="hyp:other",
            defect_fingerprint="defect:other",
            recursive_path=("record:other",),
        )
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {"record:only": forged},
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertIn("cross-bound", report.confirmations[0].reason)

    def test_candidate_artifact_excerpt_keeps_exact_identity_hash_and_byte_range(self):
        artifact_text = "Unique hydrated evidence: repository search stopped before call sites."
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {
                "record:only": RootConfirmation.confirmed(
                    "record:only",
                    excerpt=artifact_text,
                    reason="The hydrated candidate artifact contains the stopping decision.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:only", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:only"],
                )
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            artifact_root = Path(directory)
            artifact_path = Path("artifacts/decision-evidence.txt")
            target = artifact_root / artifact_path
            target.parent.mkdir(parents=True)
            target.write_text(artifact_text, encoding="utf-8")
            digest = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
            trace = single_node_trace()
            trace["artifacts"] = [
                {
                    "artifact_id": "decision-evidence",
                    "kind": "text",
                    "path": str(artifact_path),
                    "content_hash": digest,
                    "byte_length": len(artifact_text.encode("utf-8")),
                }
            ]
            trace["records"][0]["artifact_refs"] = [
                "artifact:decision-evidence"
            ]
            report = AgenticRecursiveAnalyzer(judge=judge).analyze(
                TraceGraph.from_trace(trace, artifact_root=artifact_root),
                start_refs=["record:only"],
                objective="Find the defect.",
            )

        self.assertEqual([item.node_ref for item in report.confirmed_roots], ["record:only"])
        manifest = judge.confirmation_requests[0].candidate_reference[
            "artifact_hydration"
        ]
        hydrated = manifest["hydrated_artifacts"][0]
        self.assertEqual(hydrated["artifact_id"], "decision-evidence")
        self.assertEqual(
            hydrated["content_hash"],
            "sha256:" + hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            hydrated["byte_range"], (0, len(artifact_text.encode("utf-8")))
        )

    def test_chinese_perspective_participates_in_confirmation_identity_and_ranking(self):
        trace = observed_trace(branching=True)
        trace["records"][1]["data"]["rationale"] = "任务编排策略提前结束代码搜索。"
        trace["records"][2]["data"]["text"] = "需求描述质量存在歧义。"

        def run(perspective):
            judge = ConfirmingScriptedJudge(
                {
                    "record:change": step(
                        "record:change",
                        predecessors=(
                            relation("record:decision", "same_defect_propagation"),
                            relation("record:context", "same_defect_propagation"),
                        ),
                    ),
                    "record:decision": self._confirmation_step,
                    "record:context": self._confirmation_step,
                },
                {
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="任务编排策略提前结束代码搜索。",
                        reason="Independent task-orchestration cause.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    ),
                    "record:context": RootConfirmation.confirmed(
                        "record:context",
                        excerpt="需求描述质量存在歧义。",
                        reason="Independent requirement-quality cause.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:context", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:context"],
                    ),
                },
            )
            report = AgenticRecursiveAnalyzer(judge=judge).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find every necessary cause.",
                analysis_perspective=perspective,
            )
            return report, judge

        requirement, requirement_judge = run("重点评估需求描述质量")
        orchestration, orchestration_judge = run("重点评估任务编排策略")

        self.assertEqual(requirement.confirmed_roots[0].node_ref, "record:context")
        self.assertEqual(orchestration.confirmed_roots[0].node_ref, "record:decision")
        self.assertTrue(
            all(
                item.analysis_perspective == "重点评估需求描述质量"
                for item in requirement_judge.confirmation_requests
            )
        )
        self.assertTrue(
            all(
                item.analysis_perspective == "重点评估任务编排策略"
                for item in orchestration_judge.confirmation_requests
            )
        )
        self.assertNotEqual(
            [item.factual_dict() for item in requirement_judge.confirmation_requests],
            [item.factual_dict() for item in orchestration_judge.confirmation_requests],
        )


class RetrievalGlobalFusionTest(unittest.TestCase):
    def test_single_page_bounded_failure_is_execution_failure_not_evidence_gap(self):
        class FailingGlobalJudge(FusionScriptedJudge):
            def judge_candidates_bounded(
                self, request, *, max_physical_requests
            ):
                raise BoundedJudgeCallError(
                    "JudgeContextBudgetExceeded: prompt exceeds context budget",
                    physical_requests=0,
                )

        report = AgenticRecursiveAnalyzer(
            judge=FailingGlobalJudge(global_outcome="no_defect"),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the trace-grounded introduction.",
        )

        self.assertEqual(report.analysis_outcome, "execution_failed")
        self.assertEqual(report.seed_results[0].outcome, "execution_failed")
        self.assertEqual(report.seed_results[0].missing_evidence, ())
        failure = report.seed_results[0].execution_failures[0]
        self.assertEqual(failure["kind"], "analysis_execution_failed")
        self.assertEqual(failure["reason"], "context_window_exceeded")
        self.assertEqual(failure["physical_requests"], 0)
        self.assertEqual(
            report.metadata["termination_reason"],
            "analysis_execution_failed",
        )

    def test_global_candidate_funnel_is_seed_local_when_later_pool_fails(self):
        trace = observed_trace()
        observed = next(
            item
            for item in trace["records"]
            if item["record_id"] == "observed_defect"
        )
        second = copy.deepcopy(observed)
        second["record_id"] = "second_observed_defect"
        trace["records"].append(second)
        trace["dataflow_edges"].append(
            {
                "from": {"type": "record", "id": "change"},
                "to": {"type": "record", "id": "second_observed_defect"},
                "relation": "change_observed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
        )

        class SecondPoolFailsAnalyzer(AgenticRecursiveAnalyzer):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.pool_calls = 0

            def _global_candidate_pool(self, state, graph, item):
                self.pool_calls += 1
                if self.pool_calls == 2:
                    raise ValueError("second seed candidate pool failed")
                return super()._global_candidate_pool(state, graph, item)

        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=[
                "record:observed_defect",
                "record:second_observed_defect",
            ],
            objective="Diagnose both observed defects independently.",
            analysis_perspective="task quality",
        )
        analyzer = SecondPoolFailsAnalyzer(
            judge=FusionScriptedJudge(global_outcome="no_defect"),
            fusion_mode="retrieval-global",
        )
        analyzer._run_global_candidate_prepass(state, graph)

        passes = [
            item
            for item in state.investigation_journal
            if item.get("kind") == "global_candidate_pass"
        ]
        self.assertEqual(
            [item["status"] for item in passes],
            ["completed", "failed"],
        )
        self.assertIn(
            "candidate_funnel",
            passes[0]["candidate_compression"],
        )
        self.assertNotIn(
            "candidate_funnel",
            passes[1]["candidate_compression"],
        )

    def test_global_candidate_funnel_survives_completed_bypassed_and_capsule_failure(self):
        trace = observed_trace()
        trace["records"] = [
            item for item in trace["records"] if item["record_id"] != "prompt"
        ]
        completed = AgenticRecursiveAnalyzer(
            judge=FusionScriptedJudge(global_outcome="no_defect"),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )
        completed_pass = next(
            item
            for item in completed.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "completed"
        )
        self.assertEqual(
            completed_pass["candidate_compression"]["candidate_funnel"]["schema"],
            "candidate-budget-funnel/v4",
        )

        oversized_metrics = {
            "trace_node_count": 4,
            "candidate_count": 3,
            "candidate_node_reduction_ratio": 0.25,
            "trace_json_bytes": 1_591,
            "capsule_bytes": 38_966,
            "candidate_byte_reduction_ratio": -23.491515,
            "open_root_candidate_count": 4,
            "global_fusion_payload": {"eligible": False, "reason": "patched"},
        }
        with patch(
            "trace_attribution.recursive_analyzer.candidate_compression_metrics",
            return_value=oversized_metrics,
        ):
            bypassed = AgenticRecursiveAnalyzer(
                judge=FusionScriptedJudge(global_outcome="no_defect"),
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )
        gate = next(
            item
            for item in bypassed.investigation_journal
            if item.get("kind") == "global_candidate_gate"
        )
        self.assertIn("candidate_funnel", gate["candidate_compression"])

        with patch(
            "trace_attribution.recursive_analyzer.build_candidate_evidence_capsules",
            side_effect=ValueError("candidate capsule construction failed"),
        ):
            failed = AgenticRecursiveAnalyzer(
                judge=FusionScriptedJudge(global_outcome="no_defect"),
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )
        failure = next(
            item
            for item in failed.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        )
        self.assertEqual(
            failure["candidate_compression"]["candidate_funnel"][
                "selection_identity"
            ],
            completed_pass["candidate_compression"]["candidate_funnel"][
                "selection_identity"
            ],
        )

    def test_completed_legacy_bypassed_gate_projection_remains_replayable(self):
        journal = [
            {
                "behavior_impact": "none_offline_analysis_only",
                "candidate_compression": {
                    "candidate_byte_reduction_ratio": 0.5,
                    "candidate_count": 4,
                    "candidate_node_reduction_ratio": 0.5,
                    "capsule_bytes": 100,
                    "global_fusion_payload": {
                        "capsule_to_trace_expansion_ratio": 0.5,
                        "dense_root_matrix": True,
                        "eligible": False,
                        "max_open_root_candidates": 3,
                        "max_payload_bytes": 65536,
                        "negative_compression": False,
                        "open_root_candidate_count": 4,
                        "oversized": True,
                        "reason": "oversized_dense_root_matrix",
                    },
                    "open_root_candidate_count": 4,
                    "trace_json_bytes": 200,
                    "trace_node_count": 8,
                },
                "defect_fingerprint": "defect:legacy",
                "fallback": "recursive_backward_taint",
                "kind": "global_candidate_gate",
                "seed_ref": "record:observed_defect",
                "status": "bypassed",
                "reason": "oversized_dense_root_matrix",
            }
        ]
        metadata = {
            "analysis": "agentic_recursive_semantic_taint",
            "fusion_mode": "off",
            "global_candidate_pass_count": 0,
            "global_judge_physical_request_count": 0,
            "global_candidate_judgments": [],
            "candidate_compression": [],
            "recursive_expansion_reasons": [],
            "global_candidate_failures": [],
        }

        with self.assertRaisesRegex(
            ValueError,
            "global pass metadata contradicts authoritative actions",
        ):
            _validate_global_pass_derivations(
                journal,
                metadata,
                label="live report",
            )

        classification = classify_legacy_projection_shape(
            journal,
            metadata,
        )
        self.assertIsInstance(
            classification,
            LegacyProjectionRequired,
        )
        _validate_global_pass_derivations(
            journal,
            metadata,
            label="restored completed report",
            authorized_legacy_projection=classification,
        )

    def test_versioned_candidate_funnel_is_not_classified_as_legacy_bypass(self):
        graph = TraceGraph.from_trace(observed_trace())
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
        )
        funnel = select_global_candidates(
            graph,
            [candidate],
        ).to_dict()
        journal = [
            {
                "behavior_impact": "none_offline_analysis_only",
                "candidate_compression": {
                    "candidate_byte_reduction_ratio": 0.5,
                    "candidate_count": 1,
                    "candidate_node_reduction_ratio": 0.5,
                    "capsule_bytes": 100,
                    "global_fusion_payload": {
                        "capsule_to_trace_expansion_ratio": 0.5,
                        "dense_root_matrix": True,
                        "eligible": False,
                        "max_open_root_candidates": 3,
                        "max_payload_bytes": 65536,
                        "negative_compression": False,
                        "open_root_candidate_count": 4,
                        "oversized": True,
                        "reason": "oversized_dense_root_matrix",
                    },
                    "open_root_candidate_count": 4,
                    "trace_json_bytes": 200,
                    "trace_node_count": 8,
                    "candidate_funnel": funnel,
                },
                "defect_fingerprint": "defect:modern",
                "fallback": "recursive_backward_taint",
                "kind": "global_candidate_gate",
                "seed_ref": "record:observed_defect",
                "status": "bypassed",
                "reason": "oversized_dense_root_matrix",
            }
        ]
        metadata = {
            "analysis": "agentic_recursive_semantic_taint",
            "fusion_mode": "off",
            "global_candidate_pass_count": 0,
            "global_judge_physical_request_count": 0,
            "global_candidate_judgments": [],
            "candidate_compression": [],
            "recursive_expansion_reasons": [],
            "global_candidate_failures": [],
        }

        classification = classify_legacy_projection_shape(
            journal,
            metadata,
        )

        self.assertIsInstance(
            classification,
            LegacyProjectionNotRequired,
        )
        self.assertEqual(
            classification.reason,
            "legacy_shape_not_exact",
        )

    def test_global_root_judgment_consumes_all_initial_seed_branches(self):
        trace = observed_trace()
        trace["records"].insert(
            -1,
            {
                "record_id": "timeout",
                "component": "harness",
                "event_type": "run.interruption",
                "data": {"reason": "request deadline exceeded"},
            },
        )
        observed = next(
            item
            for item in trace["records"]
            if item["record_id"] == "observed_defect"
        )
        observed["source_refs"] = [
            "record:change",
            "record:timeout",
        ]
        trace["dataflow_edges"].append(
            {
                "from": {"type": "record", "id": "timeout"},
                "to": {"type": "record", "id": "observed_defect"},
                "relation": "interruption_amplified_evaluation",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
        )
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:decision"],
        )
        self.assertNotIn("record:timeout", report.unresolved_refs)

    def test_oversized_negative_compression_bypasses_global_judge_and_recurses(self):
        judge = FusionScriptedJudge(global_outcome="inconclusive")
        oversized_metrics = {
            "trace_node_count": 4,
            "candidate_count": 3,
            "candidate_node_reduction_ratio": 0.25,
            "trace_json_bytes": 1_591,
            "capsule_bytes": 38_966,
            "candidate_byte_reduction_ratio": -23.491515,
            "open_root_candidate_count": 4,
            "global_fusion_payload": {
                "eligible": False,
                "reason": "oversized_negative_compression",
                "capsule_to_trace_expansion_ratio": 24.491515,
                "max_payload_bytes": 65_536,
                "max_open_root_candidates": 3,
            },
        }

        with patch(
            "trace_attribution.recursive_analyzer.candidate_compression_metrics",
            return_value=oversized_metrics,
        ):
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(observed_trace()),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )

        self.assertEqual(judge.global_requests, [])
        self.assertGreater(len(judge.requests), 0)
        self.assertFalse(
            any(
                item.get("kind") == "global_candidate_pass"
                and item.get("status") == "failed"
                for item in report.investigation_journal
            )
        )
        gates = [
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_gate"
        ]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["status"], "bypassed")
        self.assertEqual(
            gates[0]["reason"], "oversized_negative_compression"
        )
        self.assertEqual(
            report.metadata["fusion_mode"],
            "retrieval-global",
        )

    def test_progress_navigation_score_only_changes_queue_priority(self):
        class ScoreAdjustedRetriever(SemanticPredecessorRetriever):
            def __init__(self, score):
                self.score = score

            def retrieve(self, *args, **kwargs):
                return [
                    replace(candidate, score=self.score)
                    for candidate in super().retrieve(*args, **kwargs)
                ]

        trace = {
            "case_id": "progress-score-invariance",
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": "Assume cancellation is equivalent.",
                    },
                },
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {
                        "offline_only": True,
                        "member_refs": ["record:decision"],
                        "candidate_member_refs": ["record:decision"],
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:progress"],
                    "data": {"defect_type": "sigint_cleanup_failure"},
                },
            ],
        }

        def run(score):
            judge = ScriptedCausalJudge(
                {
                    (
                        "record:decision",
                        "navigation_candidate_semantic_cause",
                    ): step("record:decision", introduction=True),
                }
            )
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                retriever=ScoreAdjustedRetriever(score),
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the cancellation root cause.",
            )
            hypothesis = next(
                item
                for item in report.hypotheses
                if item.candidate_root_ref == "record:decision"
            )
            return {
                "claim": hypothesis.claim,
                "hypothesis_id": hypothesis.hypothesis_id,
                "semantic_hash": hypothesis.semantic_hash,
                "supporting_evidence": [
                    item.to_dict() for item in hypothesis.supporting_evidence
                ],
                "recursive_judgment_facts": judge.requests[0].to_dict(),
            }

        self.assertEqual(run(0.01), run(0.99))

    def test_duplicate_retrieval_routes_only_use_score_to_select_and_order_refs(self):
        class DuplicateRouteRetriever(SemanticPredecessorRetriever):
            def __init__(self, *, authentic_score, inferred_score, reverse):
                self.authentic_score = authentic_score
                self.inferred_score = inferred_score
                self.reverse = reverse

            def retrieve(self, graph, node_ref, defect_state, hypothesis, **kwargs):
                recorded_edge = next(
                    edge
                    for edge in graph.semantic_predecessor_edges(node_ref)
                    if edge.get("ref") == "record:decision"
                )
                node = graph.nodes["record:decision"]
                routes = [
                    CausalCandidate(
                        ref=node.ref,
                        node=node,
                        source="confirmed_edge",
                        edge=recorded_edge,
                        score=self.authentic_score,
                        evidence_refs=(node.ref,),
                    ),
                    CausalCandidate(
                        ref=node.ref,
                        node=node,
                        source="semantic_fallback",
                        edge={
                            "from_ref": node.ref,
                            "to_ref": node_ref,
                            "relation": "semantic_predecessor_match",
                            "evidence_type": "semantic_inferred",
                            "confidence": self.inferred_score,
                            "eligible_for_attribution": False,
                            "retrieval_candidate": True,
                            "inference_method": "token_overlap_retrieval",
                            "edge_origin": "offline.semantic_retrieval",
                        },
                        score=self.inferred_score,
                        evidence_refs=(node.ref,),
                    ),
                ]
                return list(reversed(routes)) if self.reverse else routes

        def run(*, authentic_score, inferred_score, reverse):
            judge = FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="Implement only the methods found in the first search.",
                        reason="The decision is the necessary local root.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    )
                },
            )
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                retriever=DuplicateRouteRetriever(
                    authentic_score=authentic_score,
                    inferred_score=inferred_score,
                    reverse=reverse,
                ),
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(observed_trace()),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )
            hypothesis = next(
                item
                for item in report.hypotheses
                if item.candidate_root_ref == "record:decision"
            )
            return {
                "global_facts": judge.global_requests[0].to_dict(),
                "candidate_source": report.introduction_candidates[0].source,
                "candidate_relation": report.introduction_candidates[0].edge["relation"],
                "hypothesis_claim": hypothesis.claim,
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_semantic_hash": hypothesis.semantic_hash,
                "confirmation_identity": report.confirmations[0].confirmation_identity,
                "independent_confirmation_facts": judge.confirmation_requests[
                    0
                ].factual_dict(),
            }

        authentic_high = run(
            authentic_score=0.99,
            inferred_score=0.01,
            reverse=False,
        )
        inferred_high = run(
            authentic_score=0.01,
            inferred_score=0.99,
            reverse=True,
        )

        self.assertEqual(authentic_high, inferred_high)
        candidate = next(
            capsule["candidate"]
            for capsule in authentic_high["global_facts"][
                "candidate_evidence_capsules"
            ]
            if capsule["candidate_ref"] == "record:decision"
        )
        self.assertEqual(candidate["source"], "confirmed_edge")
        self.assertEqual(
            candidate["retrieval_edge"]["relation"],
            "decision_guided_change",
        )
        self.assertEqual(candidate["retrieval_edge"]["confidence"], 0.98)

    def test_standard_retriever_keeps_real_duplicate_routes_until_provenance_is_canonical(self):
        def without_navigation_scores(value):
            if isinstance(value, dict):
                return {
                    key: without_navigation_scores(item)
                    for key, item in value.items()
                    if key not in {"confidence", "score", "retrieval_score"}
                }
            if isinstance(value, list):
                return [without_navigation_scores(item) for item in value]
            return value

        def run(*, confirmed_confidence, inferred_confidence):
            trace = {
                "case_id": "standard-duplicate-route-provenance",
                "records": [
                    {
                        "record_id": "decision",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "rationale": "Search only the first matching implementation."
                        },
                    },
                    {
                        "record_id": "observed_defect",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "data": {
                            "expected": "Every implementation is inspected.",
                            "actual": "One implementation was omitted.",
                        },
                    },
                ],
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": "decision"},
                        "to": {"type": "record", "id": "observed_defect"},
                        "relation": "decision_guided_change",
                        "evidence_type": "confirmed",
                        "confidence": confirmed_confidence,
                        "eligible_for_attribution": True,
                    },
                    {
                        "from": {"type": "record", "id": "decision"},
                        "to": {"type": "record", "id": "observed_defect"},
                        "relation": "semantic_predecessor_match",
                        "evidence_type": "semantic_inferred",
                        "confidence": inferred_confidence,
                        "eligible_for_attribution": True,
                    },
                ],
            }
            judge = FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="Search only the first matching implementation.",
                        reason="The decision is the necessary local root.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    )
                },
            )
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the trace-visible root.",
            )
            hypothesis = next(
                item
                for item in report.hypotheses
                if item.candidate_root_ref == "record:decision"
            )
            return {
                "global_facts": without_navigation_scores(
                    judge.global_requests[0].to_dict()
                ),
                "hypothesis_claim": hypothesis.claim,
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_semantic_hash": hypothesis.semantic_hash,
                "confirmation_facts": judge.confirmation_requests[0].factual_dict(),
            }

        confirmed_high = run(confirmed_confidence=0.99, inferred_confidence=0.01)
        inferred_high = run(confirmed_confidence=0.01, inferred_confidence=0.99)

        self.assertEqual(confirmed_high, inferred_high)

    def test_provenance_envelope_limit_keeps_all_routes_for_selected_resolved_refs(self):
        def run(*, first_route_confidence, second_route_confidence):
            trace = {
                "case_id": "provenance-envelope-resolved-ref-limit",
                "records": [
                    {
                        "record_id": "route_a",
                        "component": "context",
                        "event_type": "message.input",
                        "data": {"text": "The implementation search was incomplete."},
                    },
                    {
                        "record_id": "route_b",
                        "component": "context",
                        "event_type": "context.transform",
                        "data": {
                            "text": (
                                "The implementation search omitted every required "
                                "implementation before the observed defect."
                            )
                        },
                    },
                    {
                        "record_id": "change",
                        "component": "agent",
                        "event_type": "change",
                        "data": {"summary": "Applied the incomplete implementation search."},
                    },
                    {
                        "record_id": "observed_defect",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "data": {
                            "expected": "Every implementation is inspected.",
                            "actual": "One implementation was omitted.",
                        },
                    },
                ],
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": "route_a"},
                        "to": {"type": "record", "id": "change"},
                        "relation": "ranking_route_a",
                        "evidence_type": "semantic_inferred",
                        "confidence": first_route_confidence,
                        "eligible_for_attribution": True,
                        "retrieval_candidate": True,
                        "inference_method": "bounded_delivery_history_semantic_ranking_v1",
                        "edge_origin": "offline.progress_retrieval",
                    },
                    {
                        "from": {"type": "record", "id": "route_a"},
                        "to": {"type": "record", "id": "change"},
                        "relation": "ranking_route_b",
                        "evidence_type": "semantic_inferred",
                        "confidence": second_route_confidence,
                        "eligible_for_attribution": True,
                        "retrieval_candidate": True,
                        "inference_method": "token_overlap_retrieval",
                        "edge_origin": "offline.semantic_retrieval",
                    },
                    {
                        "from": {"type": "record", "id": "route_a"},
                        "to": {"type": "record", "id": "change"},
                        "relation": "used_as_context",
                        "evidence_type": "recorded_dataflow",
                        "confidence": 0.0,
                        "eligible_for_attribution": True,
                    },
                    {
                        "from": {"type": "record", "id": "route_b"},
                        "to": {"type": "record", "id": "change"},
                        "relation": "higher_scored_distinct_route",
                        "evidence_type": "confirmed",
                        "confidence": 0.8,
                        "eligible_for_attribution": True,
                    },
                    {
                        "from": {"type": "record", "id": "change"},
                        "to": {"type": "record", "id": "observed_defect"},
                        "relation": "change_created_observed_defect",
                        "evidence_type": "confirmed",
                        "confidence": 0.9,
                        "eligible_for_attribution": True,
                    },
                ],
            }
            judge = FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:route_a": RootConfirmation.confirmed(
                        "record:route_a",
                        excerpt="The implementation search was incomplete.",
                        reason="The authentic route carries the omitted-search defect.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:route_a", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:route_a"],
                    )
                },
                selected_candidate_refs=("record:route_a",),
            )
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the trace-visible root.",
            )
            global_request = judge.global_requests[0].to_dict()
            capsules = [
                *global_request["candidate_evidence_capsules"],
                *global_request["evidence_context_capsules"],
            ]
            return {
                "global_request": global_request,
                "capsule_refs": [item["candidate_ref"] for item in capsules],
                "hypotheses": [item.to_dict() for item in report.hypotheses],
                "confirmation_facts": [
                    item.factual_dict() for item in judge.confirmation_requests
                ],
            }

        first_route_high = run(
            first_route_confidence=0.99,
            second_route_confidence=0.01,
        )
        second_route_high = run(
            first_route_confidence=0.01,
            second_route_confidence=0.99,
        )

        self.assertEqual(first_route_high, second_route_high)
        self.assertEqual(len(first_route_high["confirmation_facts"]), 1)
        self.assertIn("record:route_a", first_route_high["capsule_refs"])
        self.assertIn("record:route_b", first_route_high["capsule_refs"])
        all_capsules = [
            *first_route_high["global_request"][
                "candidate_evidence_capsules"
            ],
            *first_route_high["global_request"][
                "evidence_context_capsules"
            ],
        ]
        route_a = next(
            item
            for item in all_capsules
            if item["candidate_ref"] == "record:route_a"
        )
        self.assertEqual(route_a["candidate"]["source"], "attribution_edge")
        self.assertEqual(
            route_a["candidate"]["retrieval_edge"]["relation"],
            "ranking_route_a",
        )
        self.assertNotIn("confidence", route_a["candidate"]["retrieval_edge"])
        self.assertTrue(
            all(
                "confidence" not in edge
                for edge in route_a["outgoing_edges"]
                if edge["relation"] in {"ranking_route_a", "ranking_route_b"}
            )
        )
        route_b = next(
            item
            for item in all_capsules
            if item["candidate_ref"] == "record:route_b"
        )
        self.assertEqual(route_b["candidate"]["retrieval_edge"]["confidence"], 0.8)
        self.assertEqual(route_b["outgoing_edges"][0]["confidence"], 0.8)

    def test_retrieval_score_does_not_change_confirmation_facts_on_real_fusion_path(self):
        class ScoreAdjustedRetriever(SemanticPredecessorRetriever):
            def __init__(self, score):
                self.score = score

            def retrieve(self, *args, **kwargs):
                return [
                    replace(candidate, score=self.score)
                    for candidate in super().retrieve(*args, **kwargs)
                ]

        def run(score):
            judge = FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="The decision omitted required search coverage.",
                        reason="The decision is the necessary local root.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    )
                },
            )
            AgenticRecursiveAnalyzer(
                judge=judge,
                retriever=ScoreAdjustedRetriever(score),
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(observed_trace()),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )
            return {
                "global_confirmation_facts": judge.global_requests[0].to_dict(),
                "independent_confirmation_facts": judge.confirmation_requests[0].factual_dict(),
            }

        self.assertEqual(run(0.01), run(0.99))

    def test_retrieval_rank_data_does_not_change_global_or_competitor_confirmation_facts(self):
        class RankAdjustedRetriever(SemanticPredecessorRetriever):
            def __init__(self, score, edge_confidence):
                self.score = score
                self.edge_confidence = edge_confidence

            def retrieve(self, *args, **kwargs):
                return [
                    replace(
                        candidate,
                        score=self.score,
                        edge={
                            **{
                                key: value
                                for key, value in candidate.edge.items()
                                if key
                                not in {
                                    "retrieval_candidate",
                                    "inference_method",
                                    "edge_origin",
                                }
                            },
                            "confidence": self.edge_confidence,
                        },
                    )
                    for candidate in super().retrieve(*args, **kwargs)
                ]

        trace = observed_trace()
        trace["records"].insert(
            2,
            {
                "record_id": "rival_decision",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "A competing decision omitted the same search."},
            },
        )
        trace["dataflow_edges"].insert(
            1,
            {
                "from": {"type": "record", "id": "rival_decision"},
                "to": {"type": "record", "id": "change"},
                "relation": "decision_guided_change",
                "evidence_type": "confirmed",
                "confidence": 0.73,
                "eligible_for_attribution": True,
            },
        )

        def normalized_facts(value):
            return json.loads(json.dumps(value, sort_keys=True))

        def run(score, edge_confidence):
            judge = FusionScriptedJudge(
                global_outcome="candidate_roots",
                selected_candidate_refs=("record:decision", "record:rival_decision"),
                confirmations={
                    ref: RootConfirmation.confirmed(
                        ref,
                        excerpt="The decision omitted required search coverage.",
                        reason="The decision is a necessary local root.",
                        counterfactual=confirmation_counterfactual_for(
                            ref, "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=[ref],
                    )
                    for ref in ("record:decision", "record:rival_decision")
                },
            )
            AgenticRecursiveAnalyzer(
                judge=judge,
                retriever=RankAdjustedRetriever(score, edge_confidence),
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find every trace-visible root.",
            )
            confirmation_facts = [
                request.factual_dict() for request in judge.confirmation_requests
            ]
            self.assertTrue(
                any(item["competing_hypotheses"] for item in confirmation_facts)
            )
            return normalized_facts(
                {
                    "global": [request.to_dict() for request in judge.global_requests],
                    "independent_confirmation": confirmation_facts,
                }
            )

        baseline = run(0.01, 0.02)
        reranked = run(0.99, 0.98)

        self.assertEqual(baseline, reranked)
        capsules = [
            *baseline["global"][0]["candidate_evidence_capsules"],
            *baseline["global"][0]["evidence_context_capsules"],
        ]
        retrieved_prompt = next(
            item for item in capsules if item["candidate_ref"] == "record:prompt"
        )
        self.assertNotIn(
            "confidence",
            retrieved_prompt["candidate"]["retrieval_edge"],
        )
        changed_record = next(
            item for item in capsules if item["candidate_ref"] == "record:change"
        )
        self.assertTrue(
            any(
                edge.get("confidence") == 0.98
                for edge in changed_record["incoming_edges"]
            )
        )

    def test_grounded_downstream_path_uses_recorded_non_temporal_edges(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "grounded-path",
                "records": [
                    {"record_id": ref, "component": "processor", "event_type": "decision"}
                    for ref in ("decision", "action", "result", "defect")
                ],
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": source},
                        "to": {"type": "record", "id": target},
                        "relation": relation,
                        "eligible_for_attribution": True,
                    }
                    for source, target, relation in (
                        ("decision", "defect", "temporal_sequence"),
                        ("decision", "defect", "progress_episode_projects_to_target"),
                        ("decision", "action", "selected_by"),
                        ("action", "result", "produced"),
                        ("result", "defect", "outcome_evidence"),
                    )
                ],
            }
        )

        self.assertEqual(
            _grounded_downstream_path(
                graph,
                "record:decision",
                ("record:defect",),
            ),
            (
                "record:decision",
                "record:action",
                "record:result",
                "record:defect",
            ),
        )

    def test_grounded_downstream_path_rejects_eligible_noncausal_hops(self):
        for relation in (
            "semantic_navigation_route",
            "temporal_sequence",
            "temporal_availability",
            "available_to_next_request",
            "temporal_adjacency",
            "fallback_sequence",
            "previous_progress_episode",
            "",
            "unregistered_causal_guess",
        ):
            with self.subTest(relation=relation):
                graph = TraceGraph.from_trace(
                    {
                        "case_id": "noncausal-grounded-path",
                        "records": [
                            {
                                "record_id": ref,
                                "component": "processor",
                                "event_type": "decision",
                            }
                            for ref in ("decision", "defect")
                        ],
                        "dataflow_edges": [
                            {
                                "from": {"type": "record", "id": "decision"},
                                "to": {"type": "record", "id": "defect"},
                                "relation": relation,
                                "eligible_for_attribution": True,
                            }
                        ],
                    }
                )

                self.assertEqual(
                    _grounded_downstream_path(
                        graph,
                        "record:decision",
                        ("record:defect",),
                    ),
                    (),
                )

        graph = TraceGraph.from_trace(
            {
                "case_id": "mixed-noncausal-grounded-path",
                "records": [
                    {"record_id": ref, "component": "processor", "event_type": "decision"}
                    for ref in ("decision", "action", "defect")
                ],
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": source},
                        "to": {"type": "record", "id": target},
                        "relation": relation,
                        "eligible_for_attribution": True,
                    }
                    for source, target, relation in (
                        ("decision", "action", "produced"),
                        ("action", "defect", "temporal_sequence"),
                    )
                ],
            }
        )

        self.assertEqual(
            _grounded_downstream_path(
                graph,
                "record:decision",
                ("record:defect",),
            ),
            (),
        )

    def test_grounded_downstream_path_rejects_temporal_edge_metadata(self):
        for metadata in (
            {"evidence_type": "temporal_inferred"},
            {"evidence_type": "temporal_only"},
            {"evidence_type": "temporal_advisory"},
            {"edge_origin": "offline.temporal_reconstruction"},
            {"inference_method": "same_session_temporal_order"},
        ):
            with self.subTest(metadata=metadata):
                graph = TraceGraph.from_trace(
                    {
                        "case_id": "temporal-metadata-grounded-path",
                        "records": [
                            {
                                "record_id": ref,
                                "component": "processor",
                                "event_type": "decision",
                            }
                            for ref in ("decision", "defect")
                        ],
                        "dataflow_edges": [
                            {
                                "from": {"type": "record", "id": "decision"},
                                "to": {"type": "record", "id": "defect"},
                                "relation": "produced",
                                "eligible_for_attribution": True,
                                **metadata,
                            }
                        ],
                    }
                )

                self.assertEqual(
                    _grounded_downstream_path(
                        graph,
                        "record:decision",
                        ("record:defect",),
                    ),
                    (),
                )

    def test_grounded_downstream_path_rejects_conflicting_raw_provenance(self):
        conflicts = (
            ("relation", "produced", "temporal_adjacency"),
            ("evidence_type", "confirmed", "temporal_only"),
            ("edge_origin", "trace.dataflow_edges", "offline.temporal_reconstruction"),
            ("inference_method", "trace_dataflow_edge", "same_session_temporal_order"),
        )
        for field, recorded, temporal in conflicts:
            with self.subTest(field=field):
                graph = TraceGraph.from_trace(
                    {
                        "case_id": "conflicting-provenance-grounded-path",
                        "records": [
                            {
                                "record_id": ref,
                                "component": "processor",
                                "event_type": "decision",
                            }
                            for ref in ("decision", "defect")
                        ],
                        "dataflow_edges": [
                            {
                                "from": {"type": "record", "id": "decision"},
                                "to": {"type": "record", "id": "defect"},
                                "relation": "produced",
                                "evidence_type": "confirmed",
                                "edge_origin": "trace.dataflow_edges",
                                "inference_method": "trace_dataflow_edge",
                                "eligible_for_attribution": True,
                                field: recorded,
                                "metadata": {field: temporal},
                            }
                        ],
                    }
                )

                self.assertEqual(
                    _grounded_downstream_path(
                        graph,
                        "record:decision",
                        ("record:defect",),
                    ),
                    (),
                )

    def test_response_claim_seed_is_an_unconfirmed_claim_quality_candidate(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "claim-seed",
                "records": [
                    {
                        "record_id": "claim",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {"text": "All 11 tests pass."},
                    }
                ],
            }
        )

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:claim"],
            objective="Check whether final claims are grounded.",
            analysis_perspective="response quality",
        )

        defect = next(iter(state.defect_states.values()))
        self.assertEqual(defect.label, "unsupported_response_claim")
        self.assertEqual(defect.actual, "All 11 tests pass.")
        self.assertIn("unconfirmed", defect.mechanism)

    def test_global_no_defect_terminates_without_recursive_step_calls(self):
        judge = FusionScriptedJudge(global_outcome="no_defect")
        trace = observed_trace()
        trace["records"] = [
            item
            for item in trace["records"]
            if item["record_id"] != "prompt"
        ]

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Determine whether the observed defect is supported.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(judge.requests, [])
        self.assertEqual(len(judge.global_requests), 1)
        self.assertEqual(
            report.metadata["global_candidate_judgments"][0]["outcome"],
            "no_defect",
        )
        self.assertEqual(report.seed_results[0].outcome, "no_defect")
        self.assertEqual(
            report.seed_results[0].global_judgment["outcome"],
            "no_defect",
        )
        self.assertNotIn("score", json.dumps(report.seed_results[0].to_dict()))

    def test_global_pool_keeps_disconnected_successful_verification_as_counterevidence(self):
        trace = {
            "case_id": "verification-recall",
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"rationale": "Apply the focused change."},
                },
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "source_refs": ["record:decision"],
                    "data": {"summary": "Focused implementation change."},
                },
                {
                    "record_id": "test_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "tool_name": "bash",
                        "args": {"command": "npm run focused"},
                        "metadata": {"exit": 0, "output": "4 passing"},
                    },
                },
                {
                    "record_id": "defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {
                        "failure_type": "missing_verification_after_change",
                        "expected": "The change is verified.",
                        "actual": "No verification was observed.",
                    },
                },
            ],
        }
        judge = FusionScriptedJudge(global_outcome="no_defect")

        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:defect"],
            objective="Determine whether verification occurred.",
        )

        self.assertIn(
            "record:test_result",
            tuple(
                capsule.candidate_ref
                for capsule in (
                    judge.global_requests[0]
                    .evidence_context_capsules
                )
            ),
        )

    def test_global_pass_gates_when_only_evidence_context_is_available(self):
        class ContextOnlyAnalyzer(AgenticRecursiveAnalyzer):
            def _global_candidate_pool(self, state, graph, item):
                candidates, _paths, _funnel = super()._global_candidate_pool(
                    state, graph, item
                )
                selection = select_global_candidates(
                    graph,
                    candidates,
                    ineligible_reasons={
                        candidate.ref: "no_active_seed_causal_path"
                        for candidate in candidates
                    },
                )
                selected = [
                    *selection.offered,
                    *selection.evidence_context,
                ]
                return (
                    selected,
                    {candidate.ref: (candidate.ref,) for candidate in selected},
                    selection.to_dict(),
                )

        judge = FusionScriptedJudge(global_outcome="candidate_roots")

        report = ContextOnlyAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the trace-grounded introduction.",
        )

        self.assertEqual(judge.global_requests, [])
        self.assertEqual(report.metadata["global_candidate_failures"], ())
        gate = next(
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_gate"
        )
        self.assertEqual(gate["status"], "bypassed")
        self.assertEqual(
            gate["reason"],
            "no_assessment_eligible_candidates",
        )
        self.assertGreater(
            gate["candidate_compression"]["candidate_funnel"][
                "evidence_context_count"
            ],
            0,
        )

    def test_global_pool_recalls_large_nested_tool_result_verification(self):
        trace = {
            "case_id": "large-verification-recall",
            "records": [
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Focused implementation change."},
                },
                {
                    "record_id": "test_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "tool_name": "bash",
                        "args": {
                            "command": "npx mocha test/unit/http.js",
                            "description": "Run focused tests",
                        },
                        "status": "success",
                        "metadata": {
                            "preview": "{0} 24 passing".format("x" * 3600),
                        },
                        "output": {
                            "preview": "{0} all tests passed".format("y" * 3600),
                        },
                    },
                },
                {
                    "record_id": "defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {
                        "failure_type": "missing_verification_after_change",
                        "expected": "The change is verified.",
                        "actual": "No verification was observed.",
                    },
                },
            ],
        }
        trace["records"].append(trace["records"].pop(1))
        judge = FusionScriptedJudge(global_outcome="no_defect")

        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:defect"],
            objective="Determine whether verification occurred.",
        )

        self.assertIn(
            "record:test_result",
            tuple(
                capsule.candidate_ref
                for capsule in (
                    judge.global_requests[0]
                    .evidence_context_capsules
                )
            ),
        )

    def test_global_pool_recalls_same_turn_authored_decision_sibling(self):
        trace = {
            "case_id": "decision-sibling-recall",
            "records": [
                {
                    "record_id": "context_a",
                    "component": "context",
                    "event_type": "context.transform",
                },
                {
                    "record_id": "context_b",
                    "component": "context",
                    "event_type": "context.transform",
                },
                {
                    "record_id": "reasoning",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:context_a", "record:context_b"],
                    "data": {"rationale": "Treat the failing behavior as a test issue."},
                },
                {
                    "record_id": "write_test",
                    "component": "tool",
                    "event_type": "decision",
                    "source_refs": ["record:context_a", "record:context_b"],
                    "data": {"chosen_action": "write", "intent": "rewrite the test"},
                },
                {
                    "record_id": "defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:reasoning"],
                    "data": {"actual": "The rewritten test misses the production failure."},
                },
            ],
        }
        judge = FusionScriptedJudge(global_outcome="no_defect")

        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:defect"],
            objective="Determine whether the verification strategy masks the defect.",
        )

        self.assertIn(
            "record:write_test",
            tuple(
                capsule.candidate_ref
                for capsule in (
                    judge.global_requests[0]
                    .evidence_context_capsules
                )
            ),
        )

    def test_global_root_candidate_goes_directly_to_independent_confirmation(self):
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The global candidate remains necessary under independent review.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(
            [item.candidate_ref for item in judge.confirmation_requests],
            ["record:decision"],
        )
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:decision"],
        )
        self.assertEqual(report.seed_results[0].outcome, "confirmed_root")
        self.assertEqual(
            report.seed_results[0].selected_candidate_refs,
            ("record:decision",),
        )
        self.assertEqual(
            report.seed_results[0].confirmed_root_refs,
            ("record:decision",),
        )
        self.assertNotIn("score", json.dumps(report.seed_results[0].to_dict()))
        self.assertEqual(
            report.seed_results[0].global_judgment["schema_version"],
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
        )

    def test_persisted_global_matrix_round_trips_only_with_full_semantics(self):
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The global candidate remains necessary under independent review.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )
        payload = report.to_dict()

        self.assertEqual(
            RecursiveAttributionReport.from_dict(copy.deepcopy(payload)).to_dict(),
            payload,
        )

        invalid_payloads = []
        missing_candidate = copy.deepcopy(payload)
        judgment = missing_candidate["seed_results"][0]["global_judgment"]
        selected = set(judgment["selected_candidate_refs"])
        removable = next(
            item
            for item in judgment["assessments"]
            if item["candidate_ref"] not in selected
        )
        judgment["assessments"].remove(removable)
        invalid_payloads.append(("missing candidate", missing_candidate))

        incomplete_comparison = copy.deepcopy(payload)
        incomplete_comparison["seed_results"][0]["global_judgment"][
            "assessments"
        ][0]["compared_candidate_refs"] = []
        invalid_payloads.append(("competitor coverage", incomplete_comparison))

        invalid_status = copy.deepcopy(payload)
        invalid_status["seed_results"][0]["global_judgment"]["assessments"][0][
            "output_defect_status"
        ] = "malformed"
        invalid_payloads.append(("invalid status", invalid_status))

        contradictory_counterfactual = copy.deepcopy(payload)
        judgment = contradictory_counterfactual["seed_results"][0][
            "global_judgment"
        ]
        selected_ref = judgment["selected_candidate_refs"][0]
        next(
            item
            for item in judgment["assessments"]
            if item["candidate_ref"] == selected_ref
        )["counterfactual"]["causal_effect"] = "does_not_prevent_defect"
        invalid_payloads.append(
            ("contradictory counterfactual", contradictory_counterfactual)
        )

        for label, invalid in invalid_payloads:
            with self.subTest(case=label):
                with self.assertRaisesRegex(
                    ValueError, "persisted global judgment.*semantic"
                ):
                    RecursiveAttributionReport.from_dict(invalid)

    def test_publication_audit_rejects_unresolved_root_identity_with_real_evidence(self):
        graph = TraceGraph.from_trace(observed_trace())
        report = AgenticRecursiveAnalyzer(
            judge=FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="Implement only the methods found in the first search.",
                        reason="The decision remains necessary under independent review.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    )
                },
            ),
            fusion_mode="retrieval-global",
        ).analyze(
            graph,
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )
        forged = report.to_dict()
        forge_published_root_identity(forged, "record:ghost")

        with self.assertRaisesRegex(
            ValueError,
            "publication identity.*record:ghost|canonical candidate node",
        ):
            forged_report = RecursiveAttributionReport.from_dict(forged)
            _assert_report_grounded_evidence(
                TraceGraph.from_trace(observed_trace()),
                forged_report,
                label="final attribution report",
            )

    def test_publication_audit_rejects_real_path_that_drifts_from_global_selection(self):
        trace = observed_trace(branching=True)
        trace["dataflow_edges"].append(
            {
                "from": {"type": "record", "id": "decision"},
                "to": {"type": "record", "id": "context"},
                "relation": "used_as_context",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            }
        )
        graph = TraceGraph.from_trace(trace)
        report = AgenticRecursiveAnalyzer(
            judge=FusionScriptedJudge(
                global_outcome="candidate_roots",
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="Implement only the methods found in the first search.",
                        reason="The decision remains necessary under independent review.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    )
                },
            ),
            fusion_mode="retrieval-global",
        ).analyze(
            graph,
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )
        forged = report.to_dict()
        forge_published_root_path(
            forged,
            [
                "record:decision",
                "record:context",
                "record:change",
                "record:observed_defect",
            ],
        )
        forged_report = RecursiveAttributionReport.from_dict(forged)

        with self.assertRaisesRegex(ValueError, "selected global assessment path"):
            _assert_report_grounded_evidence(
                graph,
                forged_report,
                label="final attribution report",
            )

    def test_publication_audit_rejects_capsule_drift_from_active_graph(self):
        trace = observed_trace()
        trace["records"] = [
            item
            for item in trace["records"]
            if item["record_id"] != "prompt"
        ]
        decision = next(
            item for item in trace["records"] if item["record_id"] == "decision"
        )
        decision["data"]["subject_revision"] = "git:active"
        report = AgenticRecursiveAnalyzer(
            judge=FusionScriptedJudge(global_outcome="no_defect"),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Determine whether the observed defect is supported.",
        )

        for label, mutate in (
            (
                "event type",
                lambda record: record.update({"event_type": "tool.result"}),
            ),
            (
                "revision",
                lambda record: record["data"].update(
                    {"subject_revision": "git:stale"}
                ),
            ),
        ):
            with self.subTest(case=label):
                active_trace = copy.deepcopy(trace)
                active_decision = next(
                    item
                    for item in active_trace["records"]
                    if item["record_id"] == "decision"
                )
                mutate(active_decision)
                with self.assertRaisesRegex(ValueError, "active graph"):
                    _assert_report_grounded_evidence(
                        TraceGraph.from_trace(active_trace),
                        report,
                        label="final attribution report",
                    )

    def test_stale_revision_candidate_never_reaches_confirmation_or_root_publication(self):
        trace = observed_trace()
        trace["manifest"] = {"subject_revision": "git:active"}
        decision = next(
            item for item in trace["records"] if item["record_id"] == "decision"
        )
        decision["data"]["subject_revision"] = "git:stale"
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            selected_candidate_refs=("record:decision",),
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The stale decision appears necessary under independent review.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        leaked_surfaces = []
        requests = [
            request
            for request in judge.global_requests
            if "record:decision" in request.offered_candidate_refs
        ]
        if requests:
            request = requests[0]
            capsule = next(
                capsule
                for capsule in request.capsules
                if capsule.candidate_ref == "record:decision"
            )
            if "record:decision" in {
                str(member.get("ref") or "")
                for member in capsule.action_group.get("members") or ()
            }:
                leaked_surfaces.append("action_group")
            if "record:decision" in request.grounded_refs:
                leaked_surfaces.append("grounded_refs")
        if any(
            str(item.get("candidate_ref") or "") == "record:decision"
            for item in report.metadata["confirmation_queue"]
        ):
            leaked_surfaces.append("confirmation_queue")
        if any(
            root.node_ref == "record:decision" for root in report.confirmed_roots
        ):
            leaked_surfaces.append("published_roots")

        self.assertEqual(leaked_surfaces, [])

    def test_stale_intermediate_invalidates_capsule_confirmation_and_publication(self):
        class PathDecisiveFusionJudge(FusionScriptedJudge):
            def judge_candidates_bounded(self, request, *, max_physical_requests):
                result = super().judge_candidates_bounded(
                    request, max_physical_requests=max_physical_requests
                )
                if "record:decision" not in result.value.selected_candidate_refs:
                    return result
                return BoundedJudgeCallResult(
                    replace(
                        result.value,
                        decisive_evidence_refs=("record:decision", "record:change"),
                    ),
                    result.physical_requests,
                )

        def analyze(*, stale_intermediate):
            trace = observed_trace()
            trace["manifest"] = {
                "case_id": trace["case_id"],
                "run_id": "stale-intermediate-analysis-run",
                "subject_revision": "git:active",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": trace["case_id"],
                    "run_id": "stale-intermediate-analysis-run",
                },
            }
            for record in trace["records"]:
                record.setdefault("data", {}).update(
                    {
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    }
                )
            if stale_intermediate:
                change = next(
                    item
                    for item in trace["records"]
                    if item["record_id"] == "change"
                )
                change["data"]["subject_revision"] = "git:stale"
            judge = PathDecisiveFusionJudge(
                global_outcome="candidate_roots",
                selected_candidate_refs=("record:decision",),
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="Implement only the methods found in the first search.",
                        reason="The active decision appears necessary under independent review.",
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision", "confirmed"
                        ),
                        confidence=0.9,
                        evidence_refs=["record:decision", "record:change"],
                    )
                },
            )
            report = AgenticRecursiveAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the primary trace-visible root.",
            )
            return judge, report

        stale_judge, stale_report = analyze(stale_intermediate=True)
        stale_leaks = []
        for request in stale_judge.global_requests:
            if "record:decision" not in request.offered_candidate_refs:
                continue
            capsule = next(
                item
                for item in request.capsules
                if item.candidate_ref == "record:decision"
            )
            if "record:change" in capsule.downstream_path:
                stale_leaks.append("capsule_path")
            if "record:change" in request.grounded_refs:
                stale_leaks.append("grounded_refs")
        for judgment in stale_report.metadata["global_candidate_judgments"]:
            if "record:change" in {
                ref
                for assessment in judgment.get("assessments") or ()
                for ref in assessment.get("causal_path_refs") or ()
            }:
                stale_leaks.append("global_assessment")
            if "record:change" in judgment.get("decisive_evidence_refs", ()):
                stale_leaks.append("decisive_evidence")
        if any(
            "record:change" in item.get("recursive_path", ())
            for item in stale_report.metadata["confirmation_queue"]
        ):
            stale_leaks.append("confirmation")
        if any(
            "record:change" in root.recursive_path
            for root in (
                *stale_report.confirmed_roots,
                *stale_report.co_roots,
                *stale_report.contributing_conditions,
                *stale_report.amplifying_factors,
            )
        ):
            stale_leaks.append("publication")
        self.assertEqual(stale_leaks, [])

        active_judge, active_report = analyze(stale_intermediate=False)
        active_request = next(
            request
            for request in active_judge.global_requests
            if "record:decision" in request.offered_candidate_refs
        )
        active_capsule = next(
            item
            for item in active_request.capsules
            if item.candidate_ref == "record:decision"
        )
        self.assertEqual(
            active_capsule.downstream_path,
            (
                "record:decision",
                "record:change",
                "record:observed_defect",
            ),
        )
        self.assertEqual(
            [root.node_ref for root in active_report.confirmed_roots],
            ["record:decision"],
        )

    def test_current_numeric_repository_generation_can_reach_root_publication(self):
        trace = observed_trace()
        trace["manifest"] = {
            "case_id": trace["case_id"],
            "run_id": "current-generation-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": trace["case_id"],
                "run_id": "current-generation-run",
            },
        }
        decision = next(
            item for item in trace["records"] if item["record_id"] == "decision"
        )
        decision["data"].update(
            {
                "subject_revision": "git:active",
                "repository_revision": 1,
                "revision_provenance_status": "valid",
            }
        )
        change = next(
            item for item in trace["records"] if item["record_id"] == "change"
        )
        change["data"].update(
            {
                "revision_after": 1,
                "subject_revision": "git:active",
                "revision_provenance_status": "valid",
            }
        )
        trace["records"].append(
            {
                "record_id": "current_claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "temporal_scope": "current_revision",
                    "repository_revision": 1,
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                },
            }
        )
        for record in trace["records"]:
            record.setdefault("data", {}).setdefault(
                "subject_revision", "git:active"
            )
            record["data"].setdefault(
                "revision_provenance_status", "valid"
            )
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            selected_candidate_refs=("record:decision",),
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The current decision remains necessary under independent review.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(
            [root.node_ref for root in report.confirmed_roots],
            ["record:decision"],
        )

    def test_confirmation_queue_revision_filter_preserves_other_seed_candidates(self):
        graph = TraceGraph.from_trace(
            {
                "manifest": {"subject_revision": "git:active"},
                "records": [
                    {
                        "record_id": "stale",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {"subject_revision": "git:stale"},
                    },
                    {
                        "record_id": "active",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {"subject_revision": "git:active"},
                    },
                ],
            }
        )
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:seed-one", "record:seed-two"),
            objective="Keep confirmation candidates seed-local.",
            analysis_perspective="task quality",
        )
        defect_state = DefectState.create(
            "queue filter",
            "Only active-revision candidates are queued.",
            "A stale candidate was offered.",
            "revision eligibility",
            "confirmation queue",
        )
        state.defect_states[defect_state.fingerprint] = defect_state
        hypothesis = state.ledger.create(
            "The active record introduced the defect.",
            "record:active",
            defect_state,
            seed_binding_identity="seed:two",
        )
        state.introduction_bindings.append(
            {
                "candidate_ref": "record:active",
                "defect_fingerprint": defect_state.fingerprint,
                "hypothesis_id": hypothesis.hypothesis_id,
                "hypothesis_semantic_hash": hypothesis.semantic_hash,
                "seed_binding_identity": "seed:two",
            }
        )

        accepted = [
            state.enqueue_confirmation(
                {
                    "candidate_ref": ref,
                    "hypothesis_id": (
                        hypothesis.hypothesis_id
                        if ref == "record:active"
                        else "hypothesis:stale"
                    ),
                    "hypothesis_semantic_hash": (
                        hypothesis.semantic_hash
                        if ref == "record:active"
                        else "sha256:stale"
                    ),
                    "defect_fingerprint": defect_state.fingerprint,
                    "seed_binding_identity": seed,
                    "recursive_path": [ref],
                    "checked_evidence_refs": [],
                    "task_obligations": [],
                    "analysis_perspective": "task quality",
                    "status": "queued",
                    "owner": LocalStateOwner.create(
                        seed_binding_identity=seed,
                        hypothesis_id=(
                            hypothesis.hypothesis_id
                            if ref == "record:active"
                            else "hypothesis:stale"
                        ),
                        visit_key=(
                            semantic_visit_key(
                                ref,
                                defect_state,
                                hypothesis.semantic_hash,
                                seed,
                            )
                            if ref == "record:active"
                            else "visit:record:stale"
                        ),
                        occurrence_key="confirmation_queue",
                    ).to_dict(),
                }
            )
            for ref, seed in (
                ("record:stale", "seed:one"),
                ("record:active", "seed:two"),
            )
        ]

        self.assertEqual(accepted, [False, True])
        self.assertEqual(
            [
                str(item.get("candidate_ref") or "")
                for item in state.confirmation_queue
            ],
            ["record:active"],
        )

    def test_confirmation_queue_never_accepts_more_than_three_candidates_per_seed(self):
        refs = ("record:alpha", "record:bravo", "record:charlie", "record:delta")
        graph = TraceGraph.from_trace(
            {
                "case_id": "confirmation-queue-bound",
                "records": [
                    {
                        "record_id": ref.removeprefix("record:"),
                        "component": "agent",
                        "event_type": "decision",
                        "data": {"rationale": "Candidate {0}.".format(ref)},
                    }
                    for ref in refs
                ],
                "dataflow_edges": [],
            }
        )
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:defect",),
            objective="Bound independent confirmation.",
            analysis_perspective="task quality",
        )
        defect_state = DefectState.create(
            "queue bound",
            "At most three roots are independently confirmed per seed.",
            "Four candidate roots were offered.",
            "bounded confirmation",
            "confirmation queue",
        )
        state.defect_states[defect_state.fingerprint] = defect_state

        accepted = []
        for ref in refs:
            hypothesis = state.ledger.create(
                "Candidate {0} introduced the defect.".format(ref),
                ref,
                defect_state,
                seed_binding_identity="seed-binding",
            )
            state.introduction_bindings.append(
                {
                    "candidate_ref": ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis.semantic_hash,
                    "seed_binding_identity": "seed-binding",
                }
            )
            accepted.append(
                state.enqueue_confirmation(
                    {
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis_semantic_hash": hypothesis.semantic_hash,
                        "candidate_ref": ref,
                        "defect_fingerprint": defect_state.fingerprint,
                        "seed_binding_identity": "seed-binding",
                        "recursive_path": [ref],
                        "checked_evidence_refs": [],
                        "task_obligations": [],
                        "analysis_perspective": "task quality",
                        "status": "queued",
                        "owner": LocalStateOwner.create(
                            seed_binding_identity="seed-binding",
                            hypothesis_id=hypothesis.hypothesis_id,
                            visit_key=semantic_visit_key(
                                ref,
                                defect_state,
                                hypothesis.semantic_hash,
                                "seed-binding",
                            ),
                            occurrence_key="confirmation_queue",
                        ).to_dict(),
                    }
                )
            )

        self.assertEqual(accepted, [True, True, True, False])
        self.assertEqual(
            [item["candidate_ref"] for item in state.confirmation_queue],
            ["record:alpha", "record:bravo", "record:charlie"],
        )

    def test_evidence_only_nodes_fail_direct_live_and_restored_queue_guards(self):
        event_types = (
            "case.completed",
            "tool.error",
            "tool.result",
            "observation",
            "execution.observation",
            "verification",
            "evidence.fact",
            "evidence.semantic_fact",
            "claim.support_assessment",
        )
        graph = TraceGraph.from_trace(
            {
                "case_id": "evidence-only-confirmation-queue",
                "records": [
                    {
                        "record_id": "candidate_{0}".format(index),
                        "component": "evidence",
                        "event_type": event_type,
                        "data": {"summary": "Observed evidence only."},
                    }
                    for index, event_type in enumerate(event_types)
                ],
                "dataflow_edges": [],
            }
        )

        for index, event_type in enumerate(event_types):
            ref = "record:candidate_{0}".format(index)
            with self.subTest(event_type=event_type):
                self.assertFalse(root_candidate_eligible(graph.nodes[ref]))
                state = RecursiveAnalysisState(
                    graph=graph,
                    start_refs=("record:defect",),
                    objective="Keep evidence out of root confirmation.",
                    analysis_perspective="task quality",
                )
                entry = {
                    "hypothesis_id": "hyp:{0}".format(index),
                    "candidate_ref": ref,
                    "defect_fingerprint": "defect-fingerprint",
                    "seed_binding_identity": "seed-binding",
                    "status": "queued",
                    "owner": LocalStateOwner.create(
                        seed_binding_identity="seed-binding",
                        hypothesis_id="hyp:{0}".format(index),
                        visit_key="visit:{0}".format(index),
                        occurrence_key="evidence_only:{0}".format(index),
                    ).to_dict(),
                }
                self.assertFalse(state.enqueue_confirmation(entry))
                state.confirmation_queue = [entry]
                with self.assertRaisesRegex(ValueError, "ineligible"):
                    state.validate_confirmation_queue_bound()

    def test_observation_and_outcome_nodes_never_reach_root_publication(self):
        for event_type in ("case.completed", "observation", "execution.observation"):
            with self.subTest(event_type=event_type):
                trace = observed_trace()
                decision = next(
                    item
                    for item in trace["records"]
                    if item["record_id"] == "decision"
                )
                decision["event_type"] = event_type
                report = AgenticRecursiveAnalyzer(
                    judge=FusionScriptedJudge(
                        global_outcome="candidate_roots",
                        confirmations={
                            "record:decision": RootConfirmation.confirmed(
                                "record:decision",
                                excerpt="The recorded outcome exposes the defect.",
                                reason="Attempt to publish an outcome as the root.",
                                counterfactual=confirmation_counterfactual_for(
                                    "record:decision", "confirmed"
                                ),
                                confidence=0.9,
                                evidence_refs=["record:decision"],
                            )
                        },
                    ),
                    fusion_mode="retrieval-global",
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed_defect"],
                    objective="Keep observed outcomes as evidence only.",
                )

                self.assertEqual(report.confirmed_roots, ())
                self.assertEqual(report.co_roots, ())

    def test_conflicting_raw_provenance_never_publishes_global_root(self):
        trace = observed_trace()
        trace["dataflow_edges"][0].update(
            {
                "relation": "decision_guided_change",
                "metadata": {"relation": "temporal_adjacency"},
            }
        )
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision remains necessary under independent review.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertTrue(judge.global_requests)
        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual(report.confirmed_roots, ())

    def test_unresolved_global_evidence_never_reaches_persisted_root_judgment(self):
        trace = observed_trace()
        trace["dataflow_edges"][0]["evidence_refs"] = ["record:ghost"]

        class GhostEvidenceGlobalJudge(FusionScriptedJudge):
            def judge_candidates_bounded(self, request, *, max_physical_requests):
                result = super().judge_candidates_bounded(
                    request, max_physical_requests=max_physical_requests
                )
                judgment = result.value
                assessments = tuple(
                    replace(item, evidence_refs=("record:ghost",))
                    if item.candidate_ref == "record:decision"
                    else item
                    for item in judgment.assessments
                )
                return BoundedJudgeCallResult(
                    replace(
                        judgment,
                        assessments=assessments,
                        decisive_evidence_refs=("record:ghost",),
                    ),
                    result.physical_requests,
                )

        judge = GhostEvidenceGlobalJudge(global_outcome="candidate_roots")
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertIn(
            "record:ghost",
            judge.global_requests[0].capsules[0].missing_evidence_refs,
        )
        self.assertEqual(report.confirmed_roots, ())
        self.assertNotIn(
            "record:ghost",
            json.dumps(report.seed_results[0].to_dict()["global_judgment"]),
        )
        self.assertTrue(
            any(
                "decisive evidence must use grounded refs" in item.get("reason", "")
                for item in report.investigation_journal
                if item.get("kind") == "global_candidate_pass"
            )
        )

    def test_global_inconclusive_then_recursive_confirmation_stays_conservative(self):
        judge = FusionScriptedJudge(
            global_outcome="inconclusive",
            script={
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                    ),
                ),
                "record:decision": RecursiveRootRankingTest._confirmation_step,
            },
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The local decision introduced the observed omission.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=("record:decision",),
                )
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        seed = report.seed_results[0]
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(seed.outcome, "evidence_gap")
        self.assertEqual(seed.confirmed_root_refs, ())
        self.assertEqual(len(seed.confirmation_identities), 1)
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(judge.confirmation_requests, [])
        self.assertEqual([item.status for item in report.confirmations], ["unknown"])
        self.assertEqual(
            seed.confirmation_identities,
            (report.confirmations[0].confirmation_identity,),
        )
        self.assertIn(
            "active causal role is not root-eligible",
            report.confirmations[0].reason,
        )
        self.assertEqual(RecursiveAttributionReport.from_dict(report.to_dict()), report)

    def test_mixed_seed_branch_failure_and_confirmation_stays_conservative(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": RecursiveRootRankingTest._confirmation_step,
                "record:context": step(
                    "record:context",
                    status="unknown",
                    missing=("The sibling context branch is truncated.",),
                ),
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The branch-local counterfactual holds.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision", "confirmed"
                    ),
                    confidence=0.9,
                    evidence_refs=("record:decision",),
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        seed = report.seed_results[0]
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(seed.outcome, "evidence_gap")
        self.assertEqual(seed.confirmed_root_refs, ())
        self.assertEqual(len(seed.confirmation_identities), 1)
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual([item.status for item in report.confirmations], ["confirmed"])
        self.assertEqual(RecursiveAttributionReport.from_dict(report.to_dict()), report)

    def test_global_expansion_unavailable_context_is_an_evidence_gap(self):
        judge = FusionScriptedJudge(
            global_outcome="needs_expansion",
            script={"record:decision": step("record:decision", status="absent")},
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(report.visited_order, ())
        self.assertEqual(
            report.seed_results[0].expansion_history[0]["request"][
                "anchor_ref"
            ],
            "record:decision",
        )
        self.assertEqual(
            report.seed_results[0].expansion_history[0]["status"],
            "rejected",
        )
        self.assertEqual(
            report.seed_results[0].expansion_history[0][
                "rejection_code"
            ],
            "context_unavailable",
        )
        self.assertEqual(
            report.seed_results[0].outcome,
            "evidence_gap",
        )


class QualityFirstGlobalCandidateBudgetTest(unittest.TestCase):
    def test_global_pool_preserves_latest_grounded_decision_beyond_discovery_and_reserve_caps(
        self,
    ):
        class EmptyRetriever(SemanticPredecessorRetriever):
            def retrieve(self, *args, **kwargs):
                return []

        noise_count = 300
        decision_count = 65
        trace = {
            "case_id": "quality-first-late-decision-after-dense-closure",
            "records": [
                *[
                    {
                        "record_id": "noise_{0:03d}".format(index),
                        "component": "context",
                        "event_type": "context.snapshot",
                        "data": {"text": "Candidate context {0}.".format(index)},
                    }
                    for index in range(noise_count)
                ],
                *[
                    {
                        "record_id": "decision_{0:02d}".format(index),
                        "component": "agent",
                        "event_type": "decision",
                        "data": {
                            "rationale": "Implementation decision {0}.".format(
                                index
                            )
                        },
                    }
                    for index in range(decision_count)
                ],
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Materialize the implementation."},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {"actual": "The implementation is incomplete."},
                },
            ],
            "dataflow_edges": [
                *[
                    {
                        "from": {
                            "type": "record",
                            "id": "noise_{0:03d}".format(index),
                        },
                        "to": {"type": "record", "id": "change"},
                        "relation": "context_available_to_change",
                        "evidence_type": "confirmed",
                        "eligible_for_attribution": True,
                    }
                    for index in range(noise_count)
                ],
                *[
                    {
                        "from": {
                            "type": "record",
                            "id": "decision_{0:02d}".format(index),
                        },
                        "to": {"type": "record", "id": "change"},
                        "relation": "decision_guided_change",
                        "evidence_type": "confirmed",
                        "eligible_for_attribution": True,
                    }
                    for index in range(decision_count)
                ],
                {
                    "from": {"type": "record", "id": "change"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "change_created_observed_defect",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]
        preferred_ref = "record:decision_64"

        analyzer = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({}),
            retriever=EmptyRetriever(),
        )
        closure, _, _ = analyzer._global_grounded_upstream_closure(
            graph,
            {
                "record:observed_defect": (
                    "record:observed_defect",
                ),
            },
        )
        closure_refs = [candidate.ref for candidate in closure]

        self.assertGreater(len(closure), 256)
        self.assertIn(preferred_ref, closure_refs)

        candidates, paths, funnel = analyzer._global_candidate_pool(
            state,
            graph,
            item,
        )
        offered_refs = [candidate.ref for candidate in candidates]

        self.assertGreater(funnel["discovered_count"], 256)
        self.assertEqual(
            funnel["reserved_grounded_decision_refs"][0],
            preferred_ref,
        )
        self.assertIn(preferred_ref, offered_refs)
        self.assertEqual(
            paths[preferred_ref],
            (
                preferred_ref,
                "record:change",
                "record:observed_defect",
            ),
        )

    def test_global_pool_finds_and_reserves_deep_decision_after_more_than_256_shallow_candidates(
        self,
    ):
        class EmptyRetriever(SemanticPredecessorRetriever):
            def retrieve(self, *args, **kwargs):
                return []

        noise_count = 300
        trace = {
            "case_id": "quality-first-deep-decision-after-shallow-noise",
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"rationale": "Select the incomplete implementation."},
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"tool": "edit"},
                },
                *[
                    {
                        "record_id": "noise_{0:03d}".format(index),
                        "component": "context",
                        "event_type": "context.snapshot",
                        "data": {"text": "Shallow candidate {0}.".format(index)},
                    }
                    for index in range(noise_count)
                ],
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Materialize the incomplete implementation."},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {"actual": "The implementation is incomplete."},
                },
            ],
            "dataflow_edges": [
                *[
                    {
                        "from": {
                            "type": "record",
                            "id": "noise_{0:03d}".format(index),
                        },
                        "to": {"type": "record", "id": "change"},
                        "relation": "context_available_to_change",
                        "evidence_type": "confirmed",
                        "eligible_for_attribution": True,
                    }
                    for index in range(noise_count)
                ],
                {
                    "from": {"type": "record", "id": "tool_call"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "modified_by",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "tool_call"},
                    "relation": "reasoning_selected_action",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "change"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "change_created_observed_defect",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]

        candidates, paths, funnel = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({}),
            retriever=EmptyRetriever(),
        )._global_candidate_pool(state, graph, item)
        offered_refs = [candidate.ref for candidate in candidates]
        discovered_refs = [
            entry["ref"] for entry in funnel["candidate_audit"]
        ]

        self.assertIn("record:decision", discovered_refs)
        self.assertIn(
            "record:decision",
            funnel["reserved_grounded_decision_refs"],
        )
        self.assertIn("record:decision", offered_refs)
        self.assertEqual(
            paths["record:decision"],
            (
                "record:decision",
                "record:tool_call",
                "record:change",
                "record:observed_defect",
            ),
        )

    def test_grounded_upstream_scan_limit_is_a_hard_edge_bound(self):
        noise_count = 40
        trace = {
            "case_id": "grounded-upstream-hard-scan-bound",
            "records": [
                *[
                    {
                        "record_id": "noise_{0:02d}".format(index),
                        "component": "context",
                        "event_type": "context.snapshot",
                    }
                    for index in range(noise_count)
                ],
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                },
            ],
            "dataflow_edges": [
                {
                    "from": {
                        "type": "record",
                        "id": "noise_{0:02d}".format(index),
                    },
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "context_available_to_change",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                }
                for index in range(noise_count)
            ],
        }
        graph = TraceGraph.from_trace(trace)

        closure = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({})
        )._global_grounded_upstream_closure
        self.assertIn("scan_limit", closure.__code__.co_varnames)

        candidates, _, _ = closure(
            graph,
            {
                "record:observed_defect": (
                    "record:observed_defect",
                )
            },
            limit=256,
            scan_limit=17,
        )

        self.assertEqual(len(candidates), 17)

    def test_global_pool_closes_every_seed_outcome_branch_not_only_progress_frontier(
        self,
    ):
        trace = {
            "case_id": "quality-first-multi-anchor-seed",
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"rationale": "Select the incomplete implementation."},
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"tool": "edit"},
                },
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Materialize the incomplete implementation."},
                },
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {
                        "offline_only": True,
                        "semantic_role": "navigation",
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": [
                        "record:change",
                        "record:progress",
                    ],
                    "data": {
                        "expected": "The complete requirement is implemented.",
                        "actual": "The implementation is incomplete.",
                    },
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "tool_call"},
                    "relation": "reasoning_selected_action",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "tool_call"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "modified_by",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "progress"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "progress_episode_projects_to_target",
                    "evidence_type": "offline_reconstruction",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]
        self.assertEqual(item.node_ref, "record:progress")

        candidates, paths, funnel = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({})
        )._global_candidate_pool(state, graph, item)

        self.assertIn(
            "record:decision",
            [candidate.ref for candidate in candidates],
        )
        self.assertEqual(
            paths["record:decision"],
            (
                "record:decision",
                "record:tool_call",
                "record:change",
                "record:observed_defect",
            ),
        )
        self.assertIn(
            "record:decision",
            funnel["reserved_grounded_decision_refs"],
        )
        self.assertIn(
            "record:progress",
            [candidate.ref for candidate in candidates],
        )
        self.assertEqual(paths["record:progress"], ("record:progress",))
        progress_audit = next(
            entry
            for entry in funnel["candidate_audit"]
            if entry["ref"] == "record:progress"
        )
        self.assertEqual(
            progress_audit["disposition"],
            "evidence_context",
        )
        self.assertEqual(
            progress_audit["reason"],
            "no_active_seed_causal_path",
        )

    def test_global_pool_materializes_process_lifecycle_before_eligibility(self):
        trace = {
            "case_id": "global-process-lifecycle-eligibility",
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": (
                            "I will implement the required parser methods now."
                        ),
                    },
                },
                {
                    "record_id": "progress",
                    "component": "progress",
                    "event_type": "progress.episode",
                    "data": {
                        "offline_only": True,
                        "member_refs": ["record:decision"],
                        "candidate_member_refs": ["record:decision"],
                        "no_delivery_progress": True,
                        "chronology_index": 1,
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:progress"],
                    "data": {
                        "expected": "The required repair is implemented.",
                        "actual": "The repository remains unchanged.",
                    },
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose why the repair remained undelivered.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]

        candidates, paths, funnel = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({})
        )._global_candidate_pool(state, graph, item)

        decision_audit = next(
            entry
            for entry in funnel["candidate_audit"]
            if entry["ref"] == "record:decision"
        )
        self.assertIn(
            "record:decision",
            [candidate.ref for candidate in candidates],
        )
        self.assertTrue(decision_audit["assessment_eligible"])
        self.assertEqual(decision_audit["disposition"], "offered")
        self.assertEqual(
            paths["record:decision"],
            ("record:decision", "record:observed_defect"),
        )
        lifecycle_edges = graph.edge_context(
            "record:decision",
            "record:observed_defect",
        )
        self.assertTrue(
            any(
                edge["relation"] == "process_lifecycle_observed"
                and edge["evidence_type"] == "offline_reconstruction"
                and edge["edge_origin"]
                == "offline.process_lifecycle_reconstruction"
                for edge in lifecycle_edges
            )
        )

    def test_global_pool_canonicalizes_grounded_route_before_budget_selection(
        self,
    ):
        trace = {
            "case_id": "quality-first-route-canonicalization",
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"rationale": "Select the implementation action."},
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"tool": "edit"},
                },
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Materialize the action."},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {"actual": "The implementation is incomplete."},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "tool_call"},
                    "relation": "reasoning_selected_action",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "tool_call"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "modified_by",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "change"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "change_created_observed_defect",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]

        class SemanticDecisionRetriever(SemanticPredecessorRetriever):
            def retrieve(self, *args, **kwargs):
                return [
                    CausalCandidate(
                        ref="record:decision",
                        node=graph.nodes["record:decision"],
                        source="semantic_fallback",
                        edge={
                            "from_ref": "record:decision",
                            "to_ref": "record:change",
                            "relation": "semantic_predecessor_match",
                            "evidence_type": "semantic_inferred",
                            "eligible_for_attribution": False,
                            "retrieval_candidate": True,
                        },
                        score=0.99,
                        evidence_refs=("record:decision",),
                    )
                ]

        candidates, paths, funnel = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({}),
            retriever=SemanticDecisionRetriever(),
        )._global_candidate_pool(state, graph, item)
        decision = next(
            candidate
            for candidate in candidates
            if candidate.ref == "record:decision"
        )

        self.assertEqual(decision.source, "confirmed_edge")
        self.assertEqual(
            decision.edge["relation"],
            "reasoning_selected_action",
        )
        self.assertEqual(
            paths["record:decision"],
            (
                "record:decision",
                "record:tool_call",
                "record:change",
                "record:observed_defect",
            ),
        )
        self.assertEqual(
            funnel["reserved_grounded_decision_refs"],
            ["record:decision"],
        )

    def test_global_pool_reserves_grounded_two_hop_authored_decision_after_dense_first_hop(
        self,
    ):
        filler_records = [
            {
                "record_id": "context_{0:02d}".format(index),
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "Dense first-hop context {0}.".format(index)},
            }
            for index in range(24)
        ]
        trace = {
            "case_id": "quality-first-grounded-decision-reserve",
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"rationale": "Select the implementation action."},
                },
                *filler_records,
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"tool": "edit"},
                },
                {
                    "record_id": "change",
                    "component": "processor",
                    "event_type": "change",
                    "data": {"summary": "Materialize the selected implementation."},
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:change"],
                    "data": {
                        "expected": "The complete requirement is implemented.",
                        "actual": "The implementation is incomplete.",
                    },
                },
            ],
            "dataflow_edges": [
                *[
                    {
                        "from": {
                            "type": "record",
                            "id": "context_{0:02d}".format(index),
                        },
                        "to": {"type": "record", "id": "change"},
                        "relation": "context_available_to_change",
                        "evidence_type": "confirmed",
                        "eligible_for_attribution": True,
                    }
                    for index in range(24)
                ],
                {
                    "from": {"type": "record", "id": "tool_call"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "modified_by",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "tool_call"},
                    "relation": "reasoning_selected_action",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "change"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "change_created_observed_defect",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]

        pool = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({})
        )._global_candidate_pool(state, graph, item)
        candidates, paths, funnel = pool

        self.assertIn(
            "record:decision",
            [candidate.ref for candidate in candidates],
        )
        self.assertEqual(
            paths["record:decision"],
            (
                "record:decision",
                "record:tool_call",
                "record:change",
                "record:observed_defect",
            ),
        )
        self.assertEqual(len(candidates), 27)
        self.assertEqual(
            funnel["policy"],
            {
                "total_limit": 96,
                "grounded_decision_reserve": 16,
            },
        )
        self.assertEqual(funnel["dropped_count"], 0)
        self.assertEqual(
            funnel["reserved_grounded_decision_refs"],
            ["record:decision"],
        )
        self.assertEqual(
            funnel["discovered_count"],
            funnel["offered_count"]
            + funnel["evidence_context_count"]
            + funnel["dropped_count"],
        )

        replay_candidates, replay_paths, replay_funnel = (
            AgenticRecursiveAnalyzer(
                judge=ScriptedCausalJudge({})
            )._global_candidate_pool(state, graph, item)
        )
        self.assertEqual(replay_candidates, candidates)
        self.assertEqual(replay_paths, paths)
        self.assertEqual(
            replay_funnel["selection_identity"],
            funnel["selection_identity"],
        )

    def test_global_pool_deduplicates_routes_and_filters_inactive_navigation(
        self,
    ):
        trace = observed_trace(branching=True)
        trace["records"].extend(
            [
                {
                    "record_id": "navigation",
                    "component": "offline",
                    "event_type": "message.input",
                    "data": {"semantic_role": "navigation"},
                },
                {
                    "record_id": "stale",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"revision_status": "mismatched"},
                },
            ]
        )
        trace["dataflow_edges"].extend(
            [
                {
                    "from": {"type": "record", "id": "navigation"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "context_available_to_change",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "stale"},
                    "to": {"type": "record", "id": "change"},
                    "relation": "decision_guided_change",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ]
        )
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the incomplete implementation.",
            analysis_perspective="task quality",
        )
        item = state.frontier.lifecycle_items()[0]
        retriever = SemanticPredecessorRetriever()
        before = retriever.retrieve(
            graph,
            item.node_ref,
            item.defect_state,
            state.ledger.get(item.hypothesis_id),
            limit=24,
            allow_semantic_fallback=True,
        )

        candidates, _paths, funnel = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({}),
            retriever=retriever,
        )._global_candidate_pool(state, graph, item)
        after = retriever.retrieve(
            graph,
            item.node_ref,
            item.defect_state,
            state.ledger.get(item.hypothesis_id),
            limit=24,
            allow_semantic_fallback=True,
        )
        refs = [candidate.ref for candidate in candidates]

        self.assertEqual(before, after)
        self.assertEqual(len(refs), len(set(refs)))
        self.assertNotIn("record:navigation", refs)
        self.assertNotIn("record:stale", refs)
        self.assertEqual(
            funnel["discovered_count"],
            funnel["offered_count"]
            + funnel["evidence_context_count"]
            + funnel["dropped_count"],
        )


if __name__ == "__main__":
    unittest.main()
