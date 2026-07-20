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
ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION = "recursive-root-confirmation-v1"

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
    supporting_evidence: Tuple[Mapping[str, Any], ...]
    opposing_evidence: Tuple[Mapping[str, Any], ...]
    competing_hypotheses: Tuple[Mapping[str, Any], ...]
    task_obligations: Tuple[Mapping[str, Any], ...]
    analysis_perspective: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "recursive_path", tuple(str(item) for item in self.recursive_path))
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
                "Judge semantics and evidence, not component labels; any component can be causal.",
                "Use same_defect_propagation only when the predecessor already carries the active defect.",
                "Use defect_transformation only when a different upstream defect transforms into the active defect through an explicit mechanism.",
                "Set recurse=true only for same_defect_propagation or defect_transformation.",
                "A transformation must provide label, mechanism, and transformation_reason for the upstream defect.",
                "Set candidate_introduction=true only when the current node is defective and no viable defective predecessor remains.",
                "Cite only resolved refs supplied in the grounded request context.",
                "Use unknown and list missing evidence when the grounded facts are insufficient.",
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
                "Try to falsify the candidate independently; no first-pass verdict is supplied.",
                "Any component may be confirmed when the grounded semantics support it.",
                "A confirmed excerpt must occur in the offered candidate facts or hydrated artifact.",
                "Cite only resolved refs offered in this request.",
                "Return unknown when evidence is missing, unresolved, or insufficient.",
            ],
            "required_json_schema": {
                "candidate_ref": request.candidate_ref,
                "status": "confirmed|rejected|unknown",
                "excerpt": "candidate-local excerpt; required for confirmed",
                "reason": "falsification result or missing evidence",
                "counterfactual": "required for confirmed",
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
                evidence_refs=_validate_evidence_refs(
                    raw.get("evidence_refs", []),
                    grounded_refs=grounded_refs,
                    field_name="predecessor evidence_refs",
                ),
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


def _confirmation_reference_sets(request: RootConfirmationRequest) -> Tuple[Set[str], Set[str]]:
    facts = (
        request.supporting_evidence,
        request.opposing_evidence,
        request.competing_hypotheses,
        request.task_obligations,
    )
    grounded, unresolved = _reference_sets(facts)
    grounded.add(request.candidate_ref)
    grounded.update(request.recursive_path)
    return grounded - unresolved, unresolved


def _candidate_semantic_corpus(request: RootConfirmationRequest) -> str:
    fragments: List[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            fragments.append(value)
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key not in {"ref", "raw_ref", "resolved_ref", "resolution_status"}:
                    collect(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for evidence in request.supporting_evidence:
        refs = {
            str(evidence.get("ref") or ""),
            str(evidence.get("raw_ref") or ""),
            str(evidence.get("resolved_ref") or ""),
        }
        if request.candidate_ref in refs:
            collect(evidence)
    return re.sub(r"\s+", " ", " ".join(fragments)).strip().lower()


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
    grounded, unresolved = _confirmation_reference_sets(request)
    if status == "confirmed" and request.candidate_ref in unresolved:
        raise ValueError("an unresolved candidate cannot be confirmed as a root")
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs", []),
        grounded_refs=grounded,
        field_name="root confirmation evidence_refs",
    )
    excerpt = str(value.get("excerpt") or "").strip()
    counterfactual = str(value.get("counterfactual") or "").strip()
    if status == "confirmed":
        if not evidence_refs:
            raise ValueError("confirmed root requires grounded evidence refs")
        if not excerpt:
            raise ValueError("confirmed root requires a grounded excerpt")
        if re.sub(r"\s+", " ", excerpt).strip().lower() not in _candidate_semantic_corpus(request):
            raise ValueError("confirmed root requires a grounded excerpt from candidate facts")
        if not counterfactual:
            raise ValueError("confirmed root requires a counterfactual")
    return RootConfirmation(
        candidate_ref=request.candidate_ref,
        status=status,
        excerpt=excerpt,
        reason=reason,
        counterfactual=counterfactual,
        confidence=confidence,
        evidence_refs=evidence_refs,
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
        cached = self.cache.get_payload(key=cache_key)
        if cached is not None:
            try:
                validator(cached)
            except (TypeError, ValueError):
                pass
            else:
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
