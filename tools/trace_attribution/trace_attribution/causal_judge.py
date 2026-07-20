"""Grounded LLM judgments for recursive causal attribution."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

from .cache import JudgmentCache, build_judge_cache_key
from .causal_state import (
    CAUSAL_RELATIONS,
    CausalCandidate,
    CausalStepJudgment,
    DefectState,
    FrozenMapping,
    PredecessorAssessment,
    RootConfirmation,
)
from .claude import ClaudeJudgeClient
from .errors import JudgeProviderError, JudgeProviderUnavailable
from .models import JsonDict, TraceNode, stable_json


CAUSAL_STEP_PROMPT_SCHEMA_VERSION = "recursive-causal-step-v1"
ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION = "recursive-root-confirmation-v3"

TEMPORAL_CAUSALITY_RULE = "Temporal order or proximity alone is never causal."
RELATION_DEFINITIONS = (
    "same_defect_propagation: the predecessor already contains the same active defect.",
    "defect_transformation: a different predecessor defect transforms into the active defect through an explicit mechanism.",
    "introduction_candidate: the defective node has no viable defective predecessor.",
    "contributing_condition: the predecessor changes likelihood or exposure but does not carry the defect.",
    "outcome_evidence: the predecessor records or exposes the defect outcome without causing it.",
    "unrelated: no grounded causal relationship is established.",
    "unknown: grounded evidence is insufficient to classify the relationship.",
)

CAUSAL_STEP_SYSTEM_PROMPT = """You judge one backward step in an offline causal trace.
Use only the supplied grounded facts. Any component may be causal when its semantics and evidence support it.
Distinguish same-defect propagation from a defect transformation, and cite direct grounded evidence refs.
Return exactly one JSON object with no markdown."""

ROOT_CONFIRMATION_SYSTEM_PROMPT = """You independently try to falsify a proposed recursive root candidate.
Use only the supplied grounded candidate facts, path, obligations, and competing hypotheses.
Do not assume any component type is or is not causal. Return exactly one JSON object with no markdown."""

REPAIR_SYSTEM_PROMPT = """Repair one invalid causal-attribution JSON response.
Correct the exact supplied parse, schema, or grounding error using only the supplied request facts.
Return exactly one JSON object with no markdown and do not invent references."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _snapshot_node(node: TraceNode) -> TraceNode:
    return TraceNode(
        ref=node.ref,
        record_id=node.record_id,
        component=node.component,
        event_type=node.event_type,
        title=node.title,
        status=node.status,
        timestamp=node.timestamp,
        data=_freeze_json(node.data),
        source_refs=tuple(str(item) for item in node.source_refs),
    )


def _node_to_dict(node: TraceNode) -> JsonDict:
    return {
        "ref": node.ref,
        "record_id": node.record_id,
        "component": node.component,
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
        "timestamp": node.timestamp,
        "data": _thaw_json(node.data),
        "source_refs": list(node.source_refs),
    }


@dataclass(frozen=True)
class CausalStepRequest:
    recursive_context: Mapping[str, Any]
    current_node: TraceNode
    defect_state: DefectState
    candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recursive_context", _freeze_json(self.recursive_context))
        object.__setattr__(self, "current_node", _snapshot_node(self.current_node))
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def to_dict(self) -> JsonDict:
        return {
            "recursive_context": _thaw_json(self.recursive_context),
            "current_node": _node_to_dict(self.current_node),
            "defect_state": self.defect_state.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
        }


@dataclass(frozen=True)
class RootConfirmationRequest:
    candidate_ref: str
    defect_state: DefectState
    recursive_path: Tuple[str, ...]
    candidate_reference: Mapping[str, Any]
    recursive_path_references: Tuple[Mapping[str, Any], ...]
    supporting_evidence: Tuple[Mapping[str, Any], ...]
    opposing_evidence: Tuple[Mapping[str, Any], ...]
    competing_hypotheses: Tuple[Mapping[str, Any], ...]
    task_obligations: Tuple[Mapping[str, Any], ...]
    analysis_perspective: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "recursive_path", tuple(str(item) for item in self.recursive_path))
        object.__setattr__(self, "candidate_reference", _freeze_json(self.candidate_reference))
        object.__setattr__(
            self,
            "recursive_path_references",
            tuple(_freeze_json(item) for item in self.recursive_path_references),
        )
        for name in (
            "supporting_evidence",
            "opposing_evidence",
            "competing_hypotheses",
            "task_obligations",
        ):
            object.__setattr__(self, name, tuple(_freeze_json(item) for item in getattr(self, name)))

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "defect_state": self.defect_state.to_dict(),
            "recursive_path": list(self.recursive_path),
            "candidate_reference": _thaw_json(self.candidate_reference),
            "recursive_path_references": _thaw_json(self.recursive_path_references),
            "supporting_evidence": _thaw_json(self.supporting_evidence),
            "opposing_evidence": _thaw_json(self.opposing_evidence),
            "competing_hypotheses": _thaw_json(self.competing_hypotheses),
            "task_obligations": _thaw_json(self.task_obligations),
            "analysis_perspective": self.analysis_perspective,
        }


class CausalJudge(Protocol):
    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        raise NotImplementedError


def build_causal_step_prompt(request: CausalStepRequest) -> str:
    return stable_json(
        {
            "request": request.to_dict(),
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Judge semantics and evidence, not component labels; any component can be causal.",
                *RELATION_DEFINITIONS,
                "Return exactly one assessment for every offered candidate, with no duplicates or omissions.",
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; bare refs never ground evidence.",
                "Use same_defect_propagation only when the predecessor already carries the active defect.",
                "Use defect_transformation only when a different upstream defect transforms into the active defect through an explicit mechanism.",
                "Set recurse=true for same_defect_propagation and defect_transformation, and require current_defect_status=present plus at least one grounded direct evidence ref.",
                "Set recurse=false for introduction_candidate, contributing_condition, outcome_evidence, unrelated, and unknown.",
                "A transformation must provide label, mechanism, and transformation_reason for the upstream defect.",
                "Set candidate_introduction=true only when the current node is defective and no viable defective predecessor remains.",
                "Cite only resolved refs supplied in the grounded request context.",
                "Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Only recorded, reconstructed, or auditable non-temporal inferred provenance is eligible; temporal-only evidence is never confirmation evidence.",
                "Competing hypotheses remain open unless explicitly rejected or superseded, and unresolved alternatives require unknown.",
                "Use unknown and list missing evidence when the grounded facts are insufficient.",
                "Independent confirmation uses a structured counterfactual with intervention_ref, intervention_kind, predicted_defect_status, and causal_effect; model-authored explanation text is not authoritative.",
            ],
            "required_json_schema": {
                "current_node_ref": request.current_node.ref,
                "current_defect_status": "present|absent|unknown",
                "current_defect_reason": "non-empty evidence-based reason",
                "predecessors": [
                    {
                        "ref": "offered candidate ref",
                        "relation": "same_defect_propagation|defect_transformation|introduction_candidate|contributing_condition|outcome_evidence|unrelated|unknown",
                        "reason": "direct causal explanation",
                        "confidence": 0.0,
                        "recurse": False,
                        "upstream_defect": None,
                        "evidence_refs": [],
                        "missing_evidence": [],
                    }
                ],
                "candidate_introduction": False,
                "missing_evidence": [],
                "suggested_investigation": None,
                "confidence": 0.0,
            },
        }
    )


def build_recursive_confirmation_prompt(request: RootConfirmationRequest) -> str:
    return stable_json(
        {
            "request": request.to_dict(),
            "falsification_checks": [
                "The candidate directly contains the tracked defect or a causally explanatory upstream defect.",
                "The candidate precedes the downstream result.",
                "No checked predecessor more completely propagates or transforms the defect.",
                "Replacing the candidate with semantically correct behavior would probably prevent the defect.",
                "No competing hypothesis more completely explains the result.",
                "The conclusion is supported by candidate-local semantics, artifacts, path, and obligations.",
            ],
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Try to falsify the candidate independently; no first-pass verdict is supplied.",
                "Any component may be confirmed when the grounded semantics support it.",
                *RELATION_DEFINITIONS,
                "The causal-step response must contain exactly one assessment for every offered candidate.",
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; candidate_ref and recursive path strings are navigation only.",
                "same_defect_propagation and defect_transformation require current_defect_status=present, recurse=true, and at least one grounded direct evidence ref; every other relation requires recurse=false.",
                "A confirmed excerpt must occur only in a fully resolved candidate-local fact or hydrated artifact that is not missing or truncated.",
                "Cite only refs whose explicit reference envelope is resolved.",
                "Unresolved opposing evidence, ambiguous path references, and missing or truncated decisive candidate evidence require unknown and can never confirm a root.",
                "Walk nested artifact containers recursively. Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Only provenance_class=recorded|reconstructed|inferred is valid. Inferred provenance requires auditable non-temporal inference metadata, and temporal-only evidence can never confirm a root.",
                "Every competing hypothesis must have resolved reference envelopes; only explicitly rejected or superseded alternatives are closed, while active, supported, unresolved, or status-less alternatives require unknown.",
                "candidate_introduction requires no viable defective predecessor.",
                "Use unknown when evidence is missing, unresolved, ambiguous, truncated, or insufficient.",
                "The counterfactual object must use intervention_ref=candidate_ref and intervention_kind=replace_with_semantically_correct_behavior.",
                "confirmed requires predicted_defect_status=absent and causal_effect=prevents_defect; unknown requires unknown and unknown; rejected allows present with does_not_prevent_defect or unknown with unknown.",
            ],
            "required_json_schema": {
                "candidate_ref": request.candidate_ref,
                "status": "confirmed|rejected|unknown",
                "excerpt": "candidate-local excerpt; required for confirmed",
                "reason": "falsification result or missing evidence",
                "counterfactual": {
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "absent|present|unknown",
                    "causal_effect": "prevents_defect|does_not_prevent_defect|unknown",
                },
                "confidence": 0.0,
                "evidence_refs": [],
            },
        }
    )


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{0} must be numeric".format(field_name))
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("{0} must be between 0 and 1".format(field_name))
    return result


def _strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("{0} must be a list of strings".format(field_name))
    return tuple(item for item in value if item)


def _reference_sets(value: Any) -> Tuple[Set[str], Set[str]]:
    grounded: Set[str] = set()
    unresolved: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            status = str(item.get("resolution_status") or "").strip().lower()
            if status:
                target = grounded if status == "resolved" else unresolved
                for key in ("ref", "raw_ref", "resolved_ref"):
                    ref = str(item.get(key) or "").strip()
                    if ref:
                        target.add(ref)
            for child in item.values():
                visit(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return grounded, unresolved


def _candidate_grounding(request: CausalStepRequest) -> Tuple[Set[str], Set[str], Set[str]]:
    offered = {item.ref for item in request.candidates}
    grounded, unresolved = _reference_sets(request.recursive_context)
    grounded.add(request.current_node.ref)
    candidate_facts = request.recursive_context.get("candidate_predecessors", ())
    grounded_candidates: Set[str] = set()
    if isinstance(candidate_facts, (list, tuple)):
        for fact in candidate_facts:
            if not isinstance(fact, Mapping):
                continue
            ref = str(fact.get("ref") or "")
            reference = fact.get("reference")
            if (
                ref in offered
                and isinstance(reference, Mapping)
                and str(reference.get("resolution_status") or "") == "resolved"
                and str(reference.get("resolved_ref") or "") == ref
            ):
                grounded_candidates.add(ref)
    return offered, grounded_candidates, grounded - unresolved


def _validate_evidence_refs(value: Any, *, grounded_refs: Set[str], field_name: str) -> Tuple[str, ...]:
    refs = _strings(value, field_name)
    invalid = [ref for ref in refs if ref not in grounded_refs]
    if invalid:
        raise ValueError(
            "{0} must cite grounded evidence refs; invalid: {1}".format(field_name, ", ".join(invalid))
        )
    return refs


def validate_causal_step_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> CausalStepJudgment:
    if not isinstance(value, dict):
        raise TypeError("causal step payload must be an object")
    if str(value.get("current_node_ref") or "") != request.current_node.ref:
        raise ValueError("current_node_ref must match the offered current node")
    status = str(value.get("current_defect_status") or "").strip().lower()
    if status not in {"present", "absent", "unknown"}:
        raise ValueError("current_defect_status must be present, absent, or unknown")
    reason = str(value.get("current_defect_reason") or "").strip()
    if not reason:
        raise ValueError("current_defect_reason must be non-empty")
    predecessors = value.get("predecessors")
    if not isinstance(predecessors, list):
        raise ValueError("predecessors must be a list")
    offered, grounded_candidates, grounded_refs = _candidate_grounding(request)
    assessments = []
    seen = set()
    for index, raw in enumerate(predecessors):
        if not isinstance(raw, dict):
            raise ValueError("predecessors[{0}] must be an object".format(index))
        ref = str(raw.get("ref") or "").strip()
        if ref not in offered:
            raise ValueError("predecessor ref {0} is not an offered candidate predecessor".format(ref))
        if ref not in grounded_candidates:
            raise ValueError("candidate predecessor {0} is not grounded as resolved".format(ref))
        if ref in seen:
            raise ValueError("candidate predecessor {0} is duplicated".format(ref))
        seen.add(ref)
        relation = str(raw.get("relation") or "").strip().lower()
        if relation not in CAUSAL_RELATIONS:
            raise ValueError("relation must be one of the exact seven causal relation values")
        predecessor_reason = str(raw.get("reason") or "").strip()
        if not predecessor_reason:
            raise ValueError("predecessor reason must be non-empty")
        if not isinstance(raw.get("recurse"), bool):
            raise ValueError("predecessor recurse must be boolean")
        recurse = raw["recurse"]
        if recurse and relation not in {"same_defect_propagation", "defect_transformation"}:
            raise ValueError("recurse=true is allowed only for propagation or transformation")
        carries_defect = relation in {"same_defect_propagation", "defect_transformation"}
        if carries_defect and status != "present":
            raise ValueError("propagation and transformation require current_defect_status=present")
        if carries_defect and not recurse:
            raise ValueError("propagation and transformation require recurse=true")
        evidence_refs = _validate_evidence_refs(
            raw.get("evidence_refs", []),
            grounded_refs=grounded_refs,
            field_name="predecessor evidence_refs",
        )
        if carries_defect and not evidence_refs:
            raise ValueError("propagation and transformation require grounded direct evidence")
        upstream_defect = None
        raw_upstream = raw.get("upstream_defect")
        if relation == "defect_transformation":
            if not isinstance(raw_upstream, dict):
                raise ValueError("defect_transformation requires an upstream_defect object")
            label = str(raw_upstream.get("label") or "").strip()
            mechanism = str(raw_upstream.get("mechanism") or "").strip()
            transformation_reason = str(raw_upstream.get("transformation_reason") or "").strip()
            if not label or not mechanism or not transformation_reason:
                raise ValueError(
                    "transformation upstream_defect requires label, mechanism, and transformation_reason"
                )
            supplied_parent = str(raw_upstream.get("derived_from_defect_state_id") or "")
            if supplied_parent and supplied_parent != request.defect_state.defect_state_id:
                raise ValueError("transformation derived_from_defect_state_id does not match active defect")
            upstream_defect = request.defect_state.transformed(
                label=label,
                expected=str(raw_upstream.get("expected") or request.defect_state.expected),
                actual=str(raw_upstream.get("actual") or request.defect_state.actual),
                mechanism=mechanism,
                scope=str(raw_upstream.get("scope") or request.defect_state.scope),
                transformation_reason=transformation_reason,
            )
        elif raw_upstream not in (None, {}):
            raise ValueError("upstream_defect is valid only for defect_transformation")
        assessments.append(
            PredecessorAssessment(
                ref=ref,
                relation=relation,
                reason=predecessor_reason,
                confidence=_number(raw.get("confidence"), "predecessor confidence"),
                recurse=recurse,
                upstream_defect=upstream_defect,
                evidence_refs=evidence_refs,
                missing_evidence=_strings(
                    raw.get("missing_evidence", []), "predecessor missing_evidence"
                ),
            )
        )
    if seen != offered:
        raise ValueError(
            "predecessors must assess every offered candidate; missing: {0}".format(
                ", ".join(sorted(offered - seen))
            )
        )
    if not isinstance(value.get("candidate_introduction"), bool):
        raise ValueError("candidate_introduction must be boolean")
    introduction = value["candidate_introduction"]
    if introduction and status != "present":
        raise ValueError("candidate introduction requires current_defect_status=present")
    if introduction and any(
        item.relation in {"same_defect_propagation", "defect_transformation"}
        for item in assessments
    ):
        raise ValueError("candidate introduction requires no viable defective predecessor")
    if status != "present" and any(item.recurse for item in assessments):
        raise ValueError("a non-present current defect cannot recurse")
    investigation = value.get("suggested_investigation")
    if investigation is not None and not isinstance(investigation, dict):
        raise ValueError("suggested_investigation must be an object or null")
    return CausalStepJudgment(
        current_node_ref=request.current_node.ref,
        current_defect_status=status,
        current_defect_reason=reason,
        predecessors=tuple(assessments),
        candidate_introduction=introduction,
        missing_evidence=_strings(value.get("missing_evidence", []), "missing_evidence"),
        suggested_investigation=dict(investigation) if investigation is not None else None,
        confidence=_number(value.get("confidence"), "confidence"),
    )


def causal_step_from_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> CausalStepJudgment:
    return validate_causal_step_payload(value, request=request)


def _validate_reference_envelope(
    value: Any, *, field_name: str
) -> Tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("{0} must be an explicit reference envelope".format(field_name))
    for key in ("resolved_ref", "resolution_status", "provenance_class"):
        if key not in value or (
            key != "resolved_ref" and not str(value.get(key) or "").strip()
        ):
            raise ValueError(
                "{0} reference envelope requires {1}".format(field_name, key)
            )
    resolved_ref = str(value.get("resolved_ref") or "").strip()
    status = str(value.get("resolution_status") or "").strip().lower()
    provenance = str(value.get("provenance_class") or "").strip()
    if provenance not in {"recorded", "reconstructed", "inferred"}:
        raise ValueError(
            "{0} provenance_class must be recorded, reconstructed, or inferred".format(
                field_name
            )
        )
    if (status == "resolved" and not resolved_ref) or (
        status != "resolved" and resolved_ref
    ):
        raise ValueError("{0} has contradictory envelope fields".format(field_name))
    inference = value.get("inference_metadata")
    inference = inference if isinstance(inference, Mapping) else value
    evidence_type = str(inference.get("evidence_type") or "").strip().lower()
    inference_method = str(inference.get("inference_method") or "").strip().lower()
    edge_origin = str(inference.get("edge_origin") or "").strip().lower()
    relation = str(inference.get("relation") or "").strip().lower()
    temporal_values = (evidence_type, inference_method, edge_origin, relation)
    if evidence_type in {"temporal_inferred", "temporal_only", "temporal_advisory"} or any(
        "temporal" in item for item in temporal_values[1:]
    ):
        raise ValueError(
            "{0} temporal-only evidence is not confirmation-eligible".format(field_name)
        )
    if provenance == "inferred":
        if not inference_method or not evidence_type:
            raise ValueError(
                "{0} inferred provenance requires auditable inference metadata".format(
                    field_name
                )
            )
        if "inferred" not in evidence_type:
            raise ValueError("{0} has contradictory inferred provenance".format(field_name))
    elif "inferred" in evidence_type:
        raise ValueError(
            "{0} has contradictory provenance and evidence_type".format(field_name)
        )
    return resolved_ref, status


def _fact_is_candidate_local(value: Mapping[str, Any], candidate_ref: str) -> bool:
    if str(value.get("resolved_ref") or "") == candidate_ref:
        return True
    owner = value.get("owner_reference")
    return (
        isinstance(owner, Mapping)
        and str(owner.get("resolved_ref") or "") == candidate_ref
        and str(owner.get("resolution_status") or "").lower() == "resolved"
    )


def _fact_gap(value: Mapping[str, Any]) -> str:
    status = str(value.get("resolution_status") or "").strip().lower()
    if status and status != "resolved":
        return status
    raw_artifact_status = value.get("artifact_status")
    if isinstance(raw_artifact_status, Mapping):
        if raw_artifact_status.get("available") is False or raw_artifact_status.get("missing"):
            return "missing"
        if raw_artifact_status.get("truncated"):
            return "truncated"
        if raw_artifact_status.get("resolved") is False:
            return "unresolved"
    else:
        artifact_status = str(raw_artifact_status or "").strip().lower()
        if artifact_status in {"missing", "truncated", "unresolved", "ambiguous"}:
            return artifact_status
    for key, label in (
        ("missing", "missing"),
        ("truncated", "truncated"),
        ("trace_artifact_truncated", "truncated"),
        ("prompt_excerpt_truncated", "truncated"),
    ):
        if value.get(key):
            return label
    hydration = value.get("artifact_hydration")
    if isinstance(hydration, Mapping):
        if hydration.get("missing_artifact_ids"):
            return "missing"
        if hydration.get("truncated_artifact_ids"):
            return "truncated"
    return ""


_REFERENCE_METADATA_KEYS = {
    "artifact_id",
    "artifact_status",
    "decisive",
    "fact_kind",
    "evidence_type",
    "inference_metadata",
    "inference_method",
    "edge_origin",
    "hash",
    "missing",
    "owner_reference",
    "provenance_class",
    "raw_ref",
    "ref",
    "reference_kind",
    "relation",
    "resolved_ref",
    "resolution_status",
    "trace_artifact_truncated",
    "truncated",
    "prompt_excerpt_truncated",
}
_TASK2_MANIFEST_KEYS = {
    "node_ref",
    "referenced_artifact_ids",
    "hydrated_artifacts",
    "missing_artifact_ids",
    "truncated_artifact_ids",
}


def _semantic_fragments(value: Any, *, skip_artifacts: bool = True) -> List[str]:
    fragments: List[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        if skip_artifacts and (
            _looks_like_artifact(value) or _TASK2_MANIFEST_KEYS.issubset(value)
        ):
            return fragments
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if (
                normalized in _REFERENCE_METADATA_KEYS
                or normalized.endswith("_ref")
                or normalized.endswith("_refs")
                or normalized.endswith("_reference")
                or normalized.endswith("_references")
                or (skip_artifacts and "artifact" in normalized)
            ):
                continue
            fragments.extend(_semantic_fragments(child, skip_artifacts=skip_artifacts))
    elif isinstance(value, (list, tuple)):
        for child in value:
            fragments.extend(_semantic_fragments(child, skip_artifacts=skip_artifacts))
    return fragments


def _artifact_semantic_fragments(value: Mapping[str, Any]) -> List[str]:
    fragments: List[str] = []
    for key in ("content", "excerpt", "text", "body", "summary"):
        if key in value:
            fragments.extend(_semantic_fragments(value[key], skip_artifacts=False))
    return fragments


def _artifact_id(value: Any) -> str:
    return str(value or "").strip().removeprefix("artifact:")


def _string_id_set(value: Any, *, field_name: str) -> Set[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError("{0} must be a list of artifact ids".format(field_name))
    normalized = [_artifact_id(item) for item in value]
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        raise ValueError("{0} contains empty or duplicate artifact ids".format(field_name))
    return set(normalized)


def _looks_like_artifact(value: Mapping[str, Any]) -> bool:
    return (
        "artifact_id" in value
        or str(value.get("reference_kind") or "").strip().lower() == "artifact"
        or "artifact_status" in value
    )


def _validate_task2_hydration_manifest(
    value: Mapping[str, Any],
    *,
    field_name: str,
    candidate_ref: str,
    candidate_local: bool,
) -> List[str]:
    missing_keys = _TASK2_MANIFEST_KEYS - set(value)
    if missing_keys:
        raise ValueError(
            "{0} Task 2 hydration manifest is missing {1}".format(
                field_name, ", ".join(sorted(missing_keys))
            )
        )
    node_ref = str(value.get("node_ref") or "").strip()
    if not node_ref or (candidate_local and node_ref != candidate_ref):
        raise ValueError(
            "{0} Task 2 hydration manifest has contradictory node_ref".format(field_name)
        )
    referenced = _string_id_set(
        value.get("referenced_artifact_ids"),
        field_name="{0}.referenced_artifact_ids".format(field_name),
    )
    missing = _string_id_set(
        value.get("missing_artifact_ids"),
        field_name="{0}.missing_artifact_ids".format(field_name),
    )
    truncated = _string_id_set(
        value.get("truncated_artifact_ids"),
        field_name="{0}.truncated_artifact_ids".format(field_name),
    )
    hydrated_value = value.get("hydrated_artifacts")
    if not isinstance(hydrated_value, (list, tuple)) or any(
        not isinstance(item, Mapping) for item in hydrated_value
    ):
        raise ValueError("{0}.hydrated_artifacts must be a list of objects".format(field_name))
    hydrated: Set[str] = set()
    fragments: List[str] = []
    for index, artifact in enumerate(hydrated_value):
        artifact_name = "{0}.hydrated_artifacts[{1}]".format(field_name, index)
        artifact_id = _artifact_id(artifact.get("artifact_id"))
        if not artifact_id or artifact_id in hydrated:
            raise ValueError("{0} has an empty or duplicate artifact_id".format(artifact_name))
        hydrated.add(artifact_id)
        if artifact_id not in referenced:
            raise ValueError("{0} is not listed in referenced_artifact_ids".format(artifact_name))
        if any(
            key in artifact
            for key in ("resolved_ref", "resolution_status", "provenance_class")
        ):
            artifact_ref, artifact_resolution = _validate_reference_envelope(
                artifact, field_name="{0} optional reference envelope".format(artifact_name)
            )
            if (
                artifact_resolution != "resolved"
                or _artifact_id(artifact_ref) != artifact_id
            ):
                raise ValueError(
                    "{0} has contradictory artifact envelope fields".format(artifact_name)
                )
        gap = _fact_gap(artifact)
        if gap:
            raise ValueError("{0} artifact is {1}".format(artifact_name, gap))
        if artifact_id in missing:
            raise ValueError("{0} artifact is both hydrated and missing".format(artifact_name))
        if bool(artifact.get("truncated")) != (artifact_id in truncated):
            raise ValueError("{0} has contradictory truncation fields".format(artifact_name))
        if artifact_id in truncated:
            raise ValueError("{0} artifact is truncated".format(artifact_name))
        if candidate_local:
            fragments.extend(_artifact_semantic_fragments(artifact))
        fragments.extend(
            _validate_artifact_tree(
                artifact,
                field_name=artifact_name,
                candidate_ref=candidate_ref,
                candidate_local=candidate_local,
                inspect_self=False,
                trusted_manifest_item=True,
            )
        )
    if not missing.issubset(referenced) or not truncated.issubset(referenced):
        raise ValueError(
            "{0} has artifact ids absent from referenced_artifact_ids".format(field_name)
        )
    if referenced != hydrated | missing:
        raise ValueError("{0} does not account for every referenced artifact".format(field_name))
    if missing:
        raise ValueError("{0} contains missing artifacts".format(field_name))
    return fragments


def _validate_artifact_tree(
    value: Any,
    *,
    field_name: str,
    candidate_ref: str,
    candidate_local: bool,
    inspect_self: bool = False,
    trusted_manifest_item: bool = False,
) -> List[str]:
    fragments: List[str] = []
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            fragments.extend(
                _validate_artifact_tree(
                    child,
                    field_name="{0}[{1}]".format(field_name, index),
                    candidate_ref=candidate_ref,
                    candidate_local=candidate_local,
                    inspect_self=inspect_self,
                    trusted_manifest_item=trusted_manifest_item,
                )
            )
        return fragments
    if not isinstance(value, Mapping):
        return fragments
    if not trusted_manifest_item and (inspect_self or _looks_like_artifact(value)):
        artifact_ref, artifact_status = _validate_reference_envelope(
            value, field_name="{0} artifact reference envelope".format(field_name)
        )
        owner = value.get("owner_reference")
        local = candidate_local or _fact_is_candidate_local(value, candidate_ref)
        if owner is not None:
            owner_ref, owner_status = _validate_reference_envelope(
                owner, field_name="{0} artifact owner reference envelope".format(field_name)
            )
            if owner_status != "resolved":
                raise ValueError("{0} artifact owner is unresolved".format(field_name))
            if candidate_local and owner_ref != candidate_ref:
                raise ValueError("{0} artifact has a contradictory owner".format(field_name))
            local = local or owner_ref == candidate_ref
        artifact_id = _artifact_id(value.get("artifact_id"))
        if artifact_id and _artifact_id(artifact_ref) != artifact_id:
            raise ValueError("{0} artifact id contradicts resolved_ref".format(field_name))
        gap = _fact_gap(value)
        if artifact_status != "resolved" or gap:
            raise ValueError(
                "{0} artifact is {1}: {2}".format(
                    field_name, gap or artifact_status, artifact_ref
                )
            )
        if local:
            fragments.extend(_artifact_semantic_fragments(value))
    for key, child in value.items():
        normalized = str(key).strip().lower()
        child_name = "{0}.{1}".format(field_name, key)
        if normalized == "artifact_hydration":
            if not isinstance(child, Mapping):
                raise ValueError("{0} must be a Task 2 hydration manifest".format(child_name))
            fragments.extend(
                _validate_task2_hydration_manifest(
                    child,
                    field_name=child_name,
                    candidate_ref=candidate_ref,
                    candidate_local=candidate_local,
                )
            )
            continue
        artifact_container = "artifact" in normalized and normalized not in {
            "artifact_id",
            "artifact_status",
            "referenced_artifact_ids",
            "missing_artifact_ids",
            "truncated_artifact_ids",
        }
        fragments.extend(
            _validate_artifact_tree(
                child,
                field_name=child_name,
                candidate_ref=candidate_ref,
                candidate_local=candidate_local,
                inspect_self=artifact_container,
            )
        )
    return fragments


def _validate_competing_hypotheses(request: RootConfirmationRequest) -> None:
    def validate_references(value: Any, field_name: str) -> None:
        if isinstance(value, Mapping):
            if any(
                key in value
                for key in ("resolved_ref", "resolution_status", "provenance_class")
            ):
                _, resolution = _validate_reference_envelope(value, field_name=field_name)
                if resolution != "resolved":
                    raise ValueError("{0} is unresolved".format(field_name))
                return
            for key, child in value.items():
                normalized = str(key).strip().lower()
                child_name = "{0}.{1}".format(field_name, key)
                if normalized.endswith("_reference"):
                    if not isinstance(child, Mapping):
                        raise ValueError("{0} must be a reference envelope".format(child_name))
                elif normalized.endswith("_references"):
                    if not isinstance(child, (list, tuple)) or any(
                        not isinstance(item, Mapping) for item in child
                    ):
                        raise ValueError("{0} must contain reference envelopes".format(child_name))
                elif normalized.endswith("_ref") or normalized.endswith("_refs"):
                    if normalized not in {"node_ref"}:
                        raise ValueError("{0} is a bare reference".format(child_name))
                validate_references(child, child_name)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                validate_references(child, "{0}[{1}]".format(field_name, index))

    for index, hypothesis in enumerate(request.competing_hypotheses):
        field_name = "competing hypothesis[{0}]".format(index)
        status = str(hypothesis.get("status") or "").strip().lower()
        if status not in {"rejected", "superseded"}:
            raise ValueError(
                "{0} must be explicitly rejected or superseded, not {1}".format(
                    field_name, status or "missing status"
                )
            )
        try:
            validate_references(hypothesis, field_name)
        except ValueError as exc:
            raise ValueError("{0} has invalid references: {1}".format(field_name, exc)) from exc


def _validate_confirmation_request(request: RootConfirmationRequest) -> str:
    candidate_resolved, candidate_status = _validate_reference_envelope(
        request.candidate_reference,
        field_name="candidate reference envelope",
    )
    if candidate_status != "resolved" or candidate_resolved != request.candidate_ref:
        raise ValueError(
            "unresolved candidate is not explicitly resolved by its candidate reference envelope"
        )
    if len(request.recursive_path_references) != len(request.recursive_path):
        raise ValueError("every navigation path ref requires a path reference envelope")
    for index, (navigation_ref, reference) in enumerate(
        zip(request.recursive_path, request.recursive_path_references)
    ):
        resolved_ref, status = _validate_reference_envelope(
            reference,
            field_name="path reference envelope[{0}]".format(index),
        )
        if status != "resolved" or resolved_ref != navigation_ref:
            raise ValueError(
                "path reference envelope[{0}] is unresolved or ambiguous".format(index)
            )
    candidate_fragments: List[str] = []
    for evidence_class, facts in (
        ("supporting evidence", request.supporting_evidence),
        ("opposing evidence", request.opposing_evidence),
    ):
        for index, fact in enumerate(facts):
            resolved_ref, status = _validate_reference_envelope(
                fact,
                field_name="{0}[{1}]".format(evidence_class, index),
            )
            owner = fact.get("owner_reference")
            if owner is not None:
                _validate_reference_envelope(
                    owner,
                    field_name="{0}[{1}].owner_reference".format(evidence_class, index),
                )
            candidate_local = _fact_is_candidate_local(fact, request.candidate_ref)
            gap = _fact_gap(fact)
            if evidence_class == "opposing evidence" and status != "resolved":
                raise ValueError("unresolved opposing evidence can invalidate root confirmation")
            if (candidate_local or bool(fact.get("decisive"))) and gap:
                raise ValueError(
                    "decisive candidate evidence or artifact is {0}: {1}".format(
                        gap, resolved_ref
                    )
                )
            if candidate_local and status == "resolved" and not gap:
                candidate_fragments.extend(_semantic_fragments(fact))
            candidate_fragments.extend(
                _validate_artifact_tree(
                    fact,
                    field_name="{0}[{1}]".format(evidence_class, index),
                    candidate_ref=request.candidate_ref,
                    candidate_local=candidate_local,
                )
            )
    _validate_competing_hypotheses(request)
    return re.sub(r"\s+", " ", " ".join(candidate_fragments)).strip().lower()


def _confirmation_reference_sets(request: RootConfirmationRequest) -> Tuple[Set[str], Set[str]]:
    facts = (
        request.candidate_reference,
        request.recursive_path_references,
        request.supporting_evidence,
        request.opposing_evidence,
        request.competing_hypotheses,
    )
    grounded, unresolved = _reference_sets(facts)
    return grounded - unresolved, unresolved


def validate_recursive_confirmation(
    value: Dict[str, Any], *, request: RootConfirmationRequest
) -> RootConfirmation:
    if not isinstance(value, dict):
        raise TypeError("root confirmation payload must be an object")
    if str(value.get("candidate_ref") or "") != request.candidate_ref:
        raise ValueError("candidate_ref must match the offered candidate")
    status = str(value.get("status") or "").strip().lower()
    if status not in {"confirmed", "rejected", "unknown"}:
        raise ValueError("root confirmation status must be confirmed, rejected, or unknown")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("root confirmation reason must be non-empty")
    confidence = _number(value.get("confidence"), "confidence")
    candidate_semantic_corpus = _validate_confirmation_request(request)
    grounded, _ = _confirmation_reference_sets(request)
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs", []),
        grounded_refs=grounded,
        field_name="root confirmation evidence_refs",
    )
    excerpt = str(value.get("excerpt") or "").strip()
    raw_counterfactual = value.get("counterfactual")
    if not isinstance(raw_counterfactual, Mapping):
        raise ValueError("counterfactual must be a structured object")
    intervention_ref = str(raw_counterfactual.get("intervention_ref") or "").strip()
    if intervention_ref != request.candidate_ref:
        raise ValueError("counterfactual intervention_ref must match candidate_ref")
    intervention_kind = str(raw_counterfactual.get("intervention_kind") or "").strip()
    if intervention_kind != "replace_with_semantically_correct_behavior":
        raise ValueError(
            "counterfactual intervention_kind must be replace_with_semantically_correct_behavior"
        )
    predicted_status = str(
        raw_counterfactual.get("predicted_defect_status") or ""
    ).strip().lower()
    if predicted_status not in {"absent", "present", "unknown"}:
        raise ValueError("counterfactual predicted_defect_status is invalid")
    causal_effect = str(raw_counterfactual.get("causal_effect") or "").strip().lower()
    if causal_effect not in {"prevents_defect", "does_not_prevent_defect", "unknown"}:
        raise ValueError("counterfactual causal_effect is invalid")
    allowed_counterfactuals = {
        "confirmed": {("absent", "prevents_defect")},
        "rejected": {
            ("present", "does_not_prevent_defect"),
            ("unknown", "unknown"),
        },
        "unknown": {("unknown", "unknown")},
    }[status]
    if (predicted_status, causal_effect) not in allowed_counterfactuals:
        raise ValueError(
            "counterfactual status combination is inconsistent with confirmation status={0}".format(
                status
            )
        )
    counterfactual_status = {
        "prevents_defect": "supports_causality",
        "does_not_prevent_defect": "rejects_causality",
        "unknown": "unknown",
    }[causal_effect]
    counterfactual = (
        "replace_with_semantically_correct_behavior({0}) predicts defect_status={1}; "
        "causal_effect={2}"
    ).format(intervention_ref, predicted_status, causal_effect)
    if status == "confirmed":
        if not evidence_refs:
            raise ValueError("confirmed root requires grounded evidence refs")
        if not excerpt:
            raise ValueError("confirmed root requires a grounded excerpt")
        if re.sub(r"\s+", " ", excerpt).strip().lower() not in candidate_semantic_corpus:
            raise ValueError("confirmed root requires a grounded excerpt from candidate facts")
    return RootConfirmation(
        candidate_ref=request.candidate_ref,
        status=status,
        excerpt=excerpt,
        reason=reason,
        counterfactual=counterfactual,
        confidence=confidence,
        evidence_refs=evidence_refs,
        counterfactual_status=counterfactual_status,
    )


def root_confirmation_from_payload(
    value: Dict[str, Any], *, request: RootConfirmationRequest
) -> RootConfirmation:
    return validate_recursive_confirmation(value, request=request)


@dataclass(frozen=True)
class _RequestOutcome:
    payload: Optional[JsonDict]
    error_kind: str = ""
    error_detail: str = ""


class ClaudeCausalJudge:
    def __init__(self, *, transport: ClaudeJudgeClient, cache: JudgmentCache):
        self.transport = transport
        self.cache = cache

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        prompt = build_causal_step_prompt(request)
        outcome = self._request_validated(
            stage="recursive_causal_step",
            schema_version=CAUSAL_STEP_PROMPT_SCHEMA_VERSION,
            system=CAUSAL_STEP_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.current_node.ref,
            request_context=request.to_dict(),
            validator=lambda value: validate_causal_step_payload(value, request=request),
            max_tokens=int(getattr(self.transport, "max_tokens", 4096)),
        )
        if outcome.payload is not None:
            return causal_step_from_payload(outcome.payload, request=request)
        detail = "judge_{0}: {1}".format(outcome.error_kind or "error", outcome.error_detail)
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="The causal relation judge could not produce a validated result.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=(detail,),
            suggested_investigation={"kind": "judge_retry", "reason": detail},
            confidence=0.0,
        )

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        prompt = build_recursive_confirmation_prompt(request)
        outcome = self._request_validated(
            stage="recursive_root_confirmation",
            schema_version=ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.candidate_ref,
            request_context=request.to_dict(),
            validator=lambda value: validate_recursive_confirmation(value, request=request),
            max_tokens=min(int(getattr(self.transport, "max_tokens", 4096)), 2048),
        )
        if outcome.payload is not None:
            return root_confirmation_from_payload(outcome.payload, request=request)
        return RootConfirmation.unknown(
            request.candidate_ref,
            "Judge {0}: {1}".format(outcome.error_kind or "error", outcome.error_detail),
        )

    def _request_validated(
        self,
        *,
        stage: str,
        schema_version: str,
        system: str,
        prompt: str,
        node_ref: str,
        request_context: JsonDict,
        validator: Callable[[JsonDict], Any],
        max_tokens: int,
    ) -> _RequestOutcome:
        evidence_hash = hashlib.sha256(stable_json(request_context).encode("utf-8")).hexdigest()
        cache_context = stable_json(
            {
                "canonical_request_context": request_context,
                "hydrated_evidence_hash": evidence_hash,
            }
        )
        cache_key = build_judge_cache_key(
            stage=stage,
            model=str(getattr(self.transport, "model", "")),
            system=system,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "user", "content": cache_context},
            ],
            max_tokens=max_tokens,
            thinking_config=getattr(self.transport, "thinking_config", None),
            prompt_schema_version=schema_version,
        )
        cached = self.cache.get_validated_payload(key=cache_key, validator=validator)
        if cached is not None:
            return _RequestOutcome(cached)
        messages = [{"role": "user", "content": prompt}]
        try:
            text = self.transport.create_message_text(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
            )
        except (JudgeProviderError, JudgeProviderUnavailable) as exc:
            return _RequestOutcome(None, "provider_error", "{0}: {1}".format(type(exc).__name__, exc))
        try:
            payload = _parse_single_json_object(text)
            validator(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as first_error:
            exact_error = "{0}: {1}".format(type(first_error).__name__, first_error)
            try:
                repaired = self.transport.create_message_text(
                    system=REPAIR_SYSTEM_PROMPT,
                    messages=[
                        {
                            "role": "user",
                            "content": stable_json(
                                {
                                    "invalid_output": text[:16000],
                                    "validation_error": exact_error,
                                    "schema_version": schema_version,
                                    "original_prompt": prompt,
                                    "canonical_request_context": request_context,
                                }
                            ),
                        }
                    ],
                    max_tokens=min(
                        int(getattr(self.transport, "repair_max_tokens", 1024)), 1024
                    ),
                )
            except (JudgeProviderError, JudgeProviderUnavailable) as exc:
                return _RequestOutcome(
                    None,
                    "provider_error",
                    "{0}; repair provider error: {1}: {2}".format(
                        exact_error, type(exc).__name__, exc
                    ),
                )
            try:
                payload = _parse_single_json_object(repaired)
                validator(payload)
            except (TypeError, ValueError, json.JSONDecodeError) as repair_error:
                return _RequestOutcome(
                    None,
                    "validation_error",
                    "{0}; focused repair invalid: {1}: {2}".format(
                        exact_error, type(repair_error).__name__, repair_error
                    ),
                )
        self.cache.put_payload(
            key=cache_key,
            stage=stage,
            model=str(getattr(self.transport, "model", "")),
            node_ref=node_ref,
            payload=payload,
        )
        return _RequestOutcome(payload)


def _parse_single_json_object(text: str) -> JsonDict:
    value = json.loads(text.strip())
    if not isinstance(value, dict):
        raise TypeError("judge response must be one JSON object")
    return value


__all__ = [
    "CAUSAL_STEP_PROMPT_SCHEMA_VERSION",
    "CAUSAL_STEP_SYSTEM_PROMPT",
    "ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION",
    "ROOT_CONFIRMATION_SYSTEM_PROMPT",
    "CausalJudge",
    "CausalStepRequest",
    "ClaudeCausalJudge",
    "RootConfirmationRequest",
    "build_causal_step_prompt",
    "build_recursive_confirmation_prompt",
    "causal_step_from_payload",
    "root_confirmation_from_payload",
    "validate_causal_step_payload",
    "validate_recursive_confirmation",
]
