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
    confirmation_identity_for,
)
from .claude import ClaudeJudgeClient
from .causal_retrieval import is_navigation_node, root_candidate_eligible
from .errors import (
    JudgeProviderError,
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
)
from .global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    GlobalCandidateJudgeRequest,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
    build_global_candidate_prompt,
    global_candidate_judgment_from_payload,
    validate_global_candidate_payload,
)
from .models import JsonDict, TraceNode, stable_json


CAUSAL_STEP_PROMPT_SCHEMA_VERSION = "recursive-causal-step-v9"
ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION = "recursive-root-confirmation-v8"

TEMPORAL_CAUSALITY_RULE = "Temporal order or proximity alone is never causal."
RELATION_DEFINITIONS = (
    "same_defect_propagation: the predecessor already contains the same active defect.",
    "defect_transformation: a different predecessor defect transforms into the active defect through an explicit mechanism.",
    "introduction_candidate: the defective node has no viable defective predecessor.",
    "contributing_condition: the predecessor changes likelihood or exposure but does not carry the active defect; set recurse=true only for a material authored assumption or condition that warrants its own explicit upstream_defect hypothesis.",
    "outcome_evidence: the predecessor records or exposes the defect outcome without causing it.",
    "unrelated: no grounded causal relationship is established.",
    "unknown: grounded evidence is insufficient to classify the relationship.",
)
INPUT_PROVENANCE_EVENT_TYPES = frozenset(
    {"prompt.assembly", "message.input", "context.transform", "llm.call", "task.loop"}
)

CAUSAL_STEP_SYSTEM_PROMPT = """You judge one backward step in an offline causal trace.
Use only the supplied grounded facts. Any component may be causal when its semantics and evidence support it.
Distinguish same-defect propagation from a defect transformation, and cite direct grounded evidence refs.
Return exactly one JSON object with no markdown."""

ROOT_CONFIRMATION_SYSTEM_PROMPT = """You independently try to falsify a proposed recursive root candidate.
Use only the supplied grounded candidate facts, path, obligations, and competing hypotheses.
Do not assume any component type is or is not causal. Return exactly one JSON object with no markdown."""

GLOBAL_CANDIDATE_SYSTEM_PROMPT = """You globally compare a bounded set of causal candidates.
Use only supplied evidence closures, compare every candidate, and preserve no-defect as a valid outcome.
Retrieval rank is navigation evidence only. Ask for expansion when decisive facts are absent.
Return exactly one JSON object with no markdown."""

REPAIR_SYSTEM_PROMPT = """Repair one invalid causal-attribution JSON response.
Correct the exact supplied parse, schema, or grounding error using only the supplied request facts.
Return a full replacement object, not a patch. Every field shown in required_json_schema is mandatory.
The top-level confidence must be an unquoted JSON number between 0 and 1.
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
    hypothesis_id: str = ""
    hypothesis_semantic_hash: str = ""
    seed_binding_identity: str = ""

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
            **self.factual_dict(),
            "analysis_perspective": self.analysis_perspective,
        }

    def factual_dict(self) -> JsonDict:
        """Return perspective-neutral facts used by verifier prompts and cache identity."""
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
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "seed_binding_identity": self.seed_binding_identity,
        }


class CausalJudge(Protocol):
    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        raise NotImplementedError


class BoundedJudgeCapability:
    """Nominal capability for Judges that enforce a physical request allowance."""

    def judge_step_bounded(
        self,
        request: CausalStepRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> "BoundedJudgeCallResult":
        raise NotImplementedError

    def confirm_candidate_bounded(
        self,
        request: RootConfirmationRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> "BoundedJudgeCallResult":
        raise NotImplementedError


@dataclass(frozen=True)
class BoundedJudgeCallResult:
    """A bounded Judge result with exact physical transport accounting."""

    value: Any
    physical_requests: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.physical_requests, bool)
            or not isinstance(self.physical_requests, int)
            or self.physical_requests < 0
        ):
            raise ValueError("physical_requests must be a non-negative integer")


class BoundedJudgeCallError(RuntimeError):
    """A bounded capability failure that preserves exact physical usage."""

    def __init__(self, message: str, *, physical_requests: int) -> None:
        if (
            isinstance(physical_requests, bool)
            or not isinstance(physical_requests, int)
            or physical_requests < 0
        ):
            raise ValueError("physical_requests must be a non-negative integer")
        super().__init__(message)
        self.physical_requests = physical_requests


class OfflineJudgeCapability:
    """Nominal capability for Judges guaranteed to perform no transport requests."""

    def judge_step_offline(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge_step(request)

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate_offline(
        self, request: RootConfirmationRequest
    ) -> RootConfirmation:
        return self.confirm_candidate(request)


class OfflineCausalJudgeAdapter(OfflineJudgeCapability):
    """Explicitly opt a legacy in-process Judge into zero-transport execution."""

    def __init__(self, judge: CausalJudge):
        self.judge = judge

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge.judge_step(request)

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        return self.judge.confirm_candidate(request)


def build_causal_step_prompt(request: CausalStepRequest) -> str:
    judge_request = request.to_dict()
    return stable_json(
        {
            "request": judge_request,
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Judge semantics and evidence, not component labels; any component can be causal.",
                "Every field shown in required_json_schema is mandatory; return the complete object, not a partial object or patch.",
                "The top-level confidence must be an unquoted JSON number between 0 and 1.",
                *RELATION_DEFINITIONS,
                "Return sparse ranked assessments only for offered candidates that plausibly propagate, transform, condition, expose, or materially challenge the active defect; omit low-value candidates instead of emitting unrelated filler.",
                "Order predecessor assessments from strongest to weakest causal relevance and never duplicate a ref. Omitted offered candidates are recorded deterministically as unselected, not treated as model judgments.",
                "The current node itself is never a predecessor. Emit a predecessor object only for an exact ref listed in request.candidates; downstream_path refs are navigation context, not offered predecessors.",
                "Sparse omission is allowed only while candidate_introduction=false. Before setting candidate_introduction=true, assess every offered predecessor so the no-viable-defective-predecessor conclusion is auditable.",
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; bare refs never ground evidence.",
                "Use same_defect_propagation only when the predecessor already carries the active defect.",
                "Prompt, message, context-transform, LLM-call, and task-loop nodes are input provenance envelopes. Being prompted, consumed, transformed, or produced establishes dataflow only. Recurse through such a node only when grounded content shows that the node itself already contains an incorrect, ambiguous, contradictory, or omitted semantic that carries the defect.",
                "Use defect_transformation only when a different upstream defect transforms into the active defect through an explicit mechanism.",
                "Set recurse=true for same_defect_propagation and defect_transformation, and require current_defect_status=present plus at least one grounded direct evidence ref.",
                "Set recurse=false for introduction_candidate, outcome_evidence, unrelated, unknown, and non-material contributing conditions.",
                "For a material contributing_condition such as an authored assumption, plan, or test-oracle decision whose correction could prevent the downstream defect, set recurse=true, cite grounded direct evidence, and provide an upstream_defect with label, mechanism, and transformation_reason. This creates a separate backward hypothesis; do not use it for incidental background context.",
                "introduction_candidate is reserved for the current-node candidate_introduction verdict and must never be assigned as an offered predecessor relation; recurse to a suspected predecessor introduction using same_defect_propagation or defect_transformation, then judge that node directly.",
                "A transformation must provide label, mechanism, and transformation_reason for the upstream defect.",
                "Set candidate_introduction=true only when the current node is defective and no viable defective predecessor remains.",
                "A present defect cannot terminate silently: either select a grounded recursive predecessor, set candidate_introduction=true after assessing every offered predecessor, or provide concrete blocking missing_evidence.",
                "Navigation aggregates, offline-only reconstruction nodes, observed outcomes, context packaging, and lifecycle-start nodes are not eligible introduction roots; keep candidate_introduction=false and recurse through eligible concrete predecessors.",
                "A navigation aggregate is a routing projection, not a defective component. Set current_defect_status=absent when the aggregate itself is clean, but still select at most two strongest recursive predecessors that route the active downstream defect. Absent without a recursive route cannot close the branch; return unknown with concrete missing evidence when no grounded route is available.",
                "Within a navigation aggregate, contributing_condition always denotes a material route and therefore requires recurse=true plus an explicit upstream_defect. Use outcome_evidence or unrelated for non-recursive candidates. Before returning unknown, assess every offered predecessor and state concrete missing evidence.",
                "A root candidate is the earliest trace-visible introduction of the active defect, not necessarily its ultimate real-world origin.",
                "When a boundary event such as process.signal directly records the active defect and no offered predecessor carries it, set candidate_introduction=true; an unknown external sender is outside the trace-visible attribution scope, so mention that scope limit in the reason but do not list it as blocking missing_evidence or move the root to an unrelated earlier node.",
                "When candidate_introduction=true with no blocking missing_evidence, suggested_investigation must request the request_root_confirmation action using the exact active_hypothesis_id, current node ref, and active defect fingerprint from the supplied recursive context.",
                "Cite only resolved refs supplied in the grounded request context.",
                "Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Only recorded, reconstructed, or auditable non-temporal inferred provenance is eligible; temporal-only evidence is never confirmation evidence.",
                "Competing hypotheses remain open unless explicitly rejected or superseded, and unresolved alternatives require unknown.",
                "Use unknown and list missing evidence when the grounded facts are insufficient.",
                "Suggest at most one bounded evidence investigation only for unknown, conflicting, truncated, or missing evidence.",
                "Evidence investigations are limited to inspect_node, expand_upstream, expand_downstream, inspect_artifact, inspect_episode, inspect_context_lineage, inspect_task_obligations, compare_causal_paths, and search_semantic_nodes; provide validated arguments and a concrete evidence-gap reason.",
                "Attribution controls record_hypothesis, reject_hypothesis, and request_root_confirmation are separate from evidence tools; Task 6 rejection requires cited grounded opposing evidence and never trusts a model-authored verifier result.",
                "A root-confirmation request must cite the exact active hypothesis_id, its candidate_root_ref, and the active defect_fingerprint; execution remains deferred to the independent Task 7 verifier.",
                "Independent confirmation uses a structured counterfactual with intervention_ref, intervention_kind, predicted_defect_status, and causal_effect; model-authored explanation text is not authoritative.",
            ],
            "required_json_schema": {
                "current_node_ref": request.current_node.ref,
                "current_defect_status": "present|absent|unknown",
                "current_defect_reason": "non-empty evidence-based reason",
                "predecessors": [
                    {
                        "ref": "offered candidate ref",
                        "relation": "same_defect_propagation|defect_transformation|contributing_condition|outcome_evidence|unrelated|unknown",
                        "reason": "direct causal explanation",
                        "confidence": 0.8,
                        "recurse": False,
                        "upstream_defect": None,
                        "evidence_refs": [],
                        "missing_evidence": [],
                    }
                ],
                "candidate_introduction": False,
                "missing_evidence": [],
                "suggested_investigation": "null | {tool, arguments, reason} | {action, arguments, reason}",
                "confidence": 0.8,
            },
            "root_confirmation_action_schema": {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context.get("active_hypothesis_id", "exact active hypothesis id"),
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "why this trace-visible introduction needs independent confirmation",
            },
        }
    )


def build_recursive_confirmation_prompt(request: RootConfirmationRequest) -> str:
    judge_request = request.factual_dict()
    return stable_json(
        {
            "request": judge_request,
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
                "Every field shown in required_json_schema is mandatory; return the complete object, not a partial object or patch.",
                "The top-level confidence must be an unquoted JSON number between 0 and 1.",
                "Any component may be confirmed when the grounded semantics support it.",
                "This is perspective-neutral factual confirmation; presentation perspective is applied only after the confirmed set is fixed.",
                *RELATION_DEFINITIONS,
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; candidate_ref and recursive path strings are navigation only.",
                "same_defect_propagation and defect_transformation require current_defect_status=present, recurse=true, and at least one grounded direct evidence ref; every other relation requires recurse=false.",
                "A confirmed excerpt must occur only in a fully resolved candidate-local fact or hydrated artifact that is not missing or truncated.",
                "Cite only refs whose explicit reference envelope is resolved.",
                "Unresolved opposing evidence, ambiguous path references, and missing or truncated decisive candidate evidence require unknown and can never confirm a root.",
                "Walk nested artifact containers recursively. Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Inspect every nested mapping and list without stopping at a valid parent envelope; every reference-bearing key must resolve through a complete offered envelope or validated hydration manifest.",
                "Any nested unknown, unresolved, missing, truncated, provider_error, provider_unavailable, provider circuit, missing_evidence, unresolved reference, or true *_budget_exhausted state requires unknown.",
                "Only provenance_class=recorded|reconstructed|inferred is valid. Inferred provenance requires auditable non-temporal inference metadata, and temporal-only evidence can never confirm a root.",
                "Every competing hypothesis must have resolved reference envelopes. Independently compare active, supported, and unresolved alternatives; a grounded open alternative does not by itself decide the result.",
                "candidate_introduction requires no viable defective predecessor.",
                "Confirm the earliest trace-visible introduction, not an unobserved ultimate real-world origin; an unknown external sender does not by itself invalidate a grounded process.signal root.",
                "Use unknown when evidence is missing, unresolved, ambiguous, truncated, or insufficient.",
                "The counterfactual object must use intervention_ref=candidate_ref and intervention_kind=replace_with_semantically_correct_behavior.",
                "confirmed requires predicted_defect_status=absent and causal_effect=prevents_defect; unknown requires unknown and unknown; rejected allows present with does_not_prevent_defect or unknown with unknown.",
                "confirmed requires factor_role=necessary_cause; rejected may classify a grounded contributing_condition, amplifying_factor, unrelated alternative, or unknown; unknown requires factor_role=unknown.",
                "factor_mechanism must be null for necessary_cause, unrelated, and unknown; use causal_factor_mechanism_schema only for contributing_condition or amplifying_factor.",
            ],
            "required_json_schema": {
                "candidate_ref": request.candidate_ref,
                "status": "confirmed|rejected|unknown",
                "factor_role": "necessary_cause|contributing_condition|amplifying_factor|unrelated|unknown",
                "excerpt": "candidate-local excerpt; required for confirmed",
                "reason": "falsification result or missing evidence",
                "counterfactual": {
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "absent|present|unknown",
                    "causal_effect": "prevents_defect|does_not_prevent_defect|unknown",
                },
                "confidence": 0.8,
                "evidence_refs": [],
                "competitor_comparisons": [
                    {
                        "hypothesis_id": "one offered open competitor",
                        "hypothesis_semantic_hash": "offered semantic hash",
                        "candidate_ref": "offered competitor candidate",
                        "defect_fingerprint": "offered active defect fingerprint",
                        "confirmation_identity": "offered complete confirmation identity",
                        "recursive_path": [],
                        "requires_independent_confirmation": True,
                        "status": "outperformed|rejected|co_root|unresolved",
                        "reason": "grounded comparison",
                        "evidence_refs": [],
                    }
                ],
                "factor_mechanism": None,
            },
            "causal_factor_mechanism_schema": {
                    "mechanism_type": "enabling_condition|amplification",
                    "source_ref": request.candidate_ref,
                    "target_ref": "grounded recursive path ref",
                    "effect": "non-empty causal mechanism",
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
    normalized_reason = " ".join(reason.casefold().split())
    if status == "present" and (
        re.search(
            r"\b(?:current node|this node|candidate|node)\b.{0,32}\b(?:is|was) not (?:itself )?(?:defective|a defect|the defect)\b",
            normalized_reason,
        )
        or re.search(
            r"\b(?:current node|this node|candidate|node)\b.{0,24}\bdoes not (?:contain|introduce|carry) (?:the |a )?defect\b",
            normalized_reason,
        )
        or re.search(
            r"\b(?:decision|tool call|claim|episode|result)(?: node)? itself\b.{0,24}\bdoes not (?:contain|introduce|carry) (?:the |a )?defect\b",
            normalized_reason,
        )
    ):
        raise ValueError(
            "current_defect_status=present contradicts current_defect_reason"
        )
    if status == "absent" and re.search(
        r"\b(?:reasoning block|decision|plan|assumption)\b.{0,160}"
        r"\b(?:insufficient|incorrect|wrong|defective|does not account for|fails? to account for|omits?)\b",
        normalized_reason,
    ):
        raise ValueError(
            "current_defect_status=absent contradicts current_defect_reason"
        )
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
            raise ValueError("relation must be one of the allowed causal relation values")
        if relation == "introduction_candidate":
            raise ValueError(
                "predecessor introduction_candidate is invalid; it is a current-node verdict"
            )
        predecessor_reason = str(raw.get("reason") or "").strip()
        if not predecessor_reason:
            raise ValueError("predecessor reason must be non-empty")
        if not isinstance(raw.get("recurse"), bool):
            raise ValueError("predecessor recurse must be boolean")
        recurse = raw["recurse"]
        candidate_node = next(
            (candidate.node for candidate in request.candidates if candidate.ref == ref),
            None,
        )
        if (
            recurse
            and candidate_node is not None
            and candidate_node.event_type in INPUT_PROVENANCE_EVENT_TYPES
            and not reason_identifies_candidate_local_defect(predecessor_reason)
        ):
            raise ValueError(
                "an input provenance envelope can recurse only when its own content contains the defect"
            )
        if recurse and relation not in {
            "same_defect_propagation",
            "defect_transformation",
            "contributing_condition",
        }:
            raise ValueError(
                "recurse=true is allowed only for propagation, transformation, or a material contributing condition"
            )
        carries_defect = relation in {"same_defect_propagation", "defect_transformation"}
        recursively_influences = relation == "contributing_condition" and recurse
        if (
            (carries_defect or recursively_influences)
            and status != "present"
            and not is_navigation_node(request.current_node)
        ):
            raise ValueError(
                "recursive propagation, transformation, and material contribution require current_defect_status=present"
            )
        if carries_defect and not recurse:
            raise ValueError("propagation and transformation require recurse=true")
        evidence_refs = _validate_evidence_refs(
            raw.get("evidence_refs", []),
            grounded_refs=grounded_refs,
            field_name="predecessor evidence_refs",
        )
        if (carries_defect or recursively_influences) and not evidence_refs:
            raise ValueError(
                "recursive propagation, transformation, and material contribution require grounded direct evidence"
            )
        upstream_defect = None
        raw_upstream = raw.get("upstream_defect")
        if relation == "defect_transformation" or recursively_influences:
            if not isinstance(raw_upstream, dict):
                raise ValueError(
                    "defect_transformation and recursive contributing_condition require an upstream_defect object"
                )
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
            raise ValueError(
                "upstream_defect is valid only for defect_transformation or a recursive contributing_condition"
            )
        predecessor_confidence = _number(
            raw.get("confidence"), "predecessor confidence"
        )
        if relation != "unknown" and predecessor_confidence <= 0.0:
            raise ValueError(
                "predecessors[{0}].confidence for {1} with relation={2} must be greater than 0".format(
                    index, ref, relation
                )
            )
        assessments.append(
            PredecessorAssessment(
                ref=ref,
                relation=relation,
                reason=predecessor_reason,
                confidence=predecessor_confidence,
                recurse=recurse,
                upstream_defect=upstream_defect,
                evidence_refs=evidence_refs,
                missing_evidence=_strings(
                    raw.get("missing_evidence", []), "predecessor missing_evidence"
                ),
            )
        )
    unselected = tuple(sorted(offered - seen))
    if sum(bool(item.recurse) for item in assessments) > 2:
        raise ValueError("a causal step may select at most two recursive predecessors")
    if not isinstance(value.get("candidate_introduction"), bool):
        raise ValueError("candidate_introduction must be boolean")
    introduction = value["candidate_introduction"]
    if introduction and status != "present":
        raise ValueError("candidate introduction requires current_defect_status=present")
    if introduction and not root_candidate_eligible(request.current_node):
        raise ValueError("the current navigation or outcome node is not eligible as a root candidate")
    if introduction and any(
        item.relation in {"same_defect_propagation", "defect_transformation"}
        or (item.relation == "contributing_condition" and item.recurse)
        for item in assessments
    ):
        raise ValueError("candidate introduction requires no viable defective predecessor")
    if introduction and unselected:
        raise ValueError(
            "candidate introduction requires assessing every offered predecessor"
        )
    if (
        status != "present"
        and any(item.recurse for item in assessments)
        and not is_navigation_node(request.current_node)
    ):
        raise ValueError("a non-present current defect cannot recurse")
    investigation = value.get("suggested_investigation")
    if investigation is not None and not isinstance(investigation, dict):
        raise ValueError("suggested_investigation must be an object or null")
    missing_evidence = _strings(value.get("missing_evidence", []), "missing_evidence")
    if is_navigation_node(request.current_node):
        recursive_predecessors = [item for item in assessments if item.recurse]
        if any(
            item.relation == "contributing_condition" and not item.recurse
            for item in assessments
        ):
            raise ValueError(
                "navigation contributing_condition requires recurse=true and an upstream_defect"
            )
        if len(recursive_predecessors) > 2:
            raise ValueError(
                "a navigation aggregate may select at most two recursive predecessors"
            )
        if status in {"present", "absent"} and not recursive_predecessors and not missing_evidence:
            raise ValueError(
                "a navigation aggregate cannot close the active branch without a recursive predecessor or concrete missing_evidence"
            )
        if status == "unknown" and unselected:
            raise ValueError(
                "an unknown navigation judgment requires assessing every offered predecessor"
            )
        if status == "unknown" and not missing_evidence:
            raise ValueError(
                "an unknown navigation judgment requires concrete missing_evidence"
            )
    if (
        status == "present"
        and not introduction
        and not any(item.recurse for item in assessments)
        and not missing_evidence
    ):
        raise ValueError(
            "a present defect cannot terminate silently without recursion, introduction, or missing_evidence"
        )
    if (
        isinstance(investigation, dict)
        and investigation.get("action") == "request_root_confirmation"
        and not introduction
    ):
        raise ValueError(
            "request_root_confirmation requires candidate_introduction=true"
        )
    active_hypothesis_id = str(request.recursive_context.get("active_hypothesis_id") or "")
    if introduction and not missing_evidence and active_hypothesis_id:
        if not isinstance(investigation, dict) or investigation.get("action") != "request_root_confirmation":
            raise ValueError(
                "candidate introduction requires request_root_confirmation"
            )
        arguments = investigation.get("arguments")
        expected_arguments = {
            "hypothesis_id": active_hypothesis_id,
            "candidate_ref": request.current_node.ref,
            "defect_fingerprint": request.defect_state.fingerprint,
        }
        if not isinstance(arguments, dict) or set(arguments) != set(expected_arguments):
            raise ValueError(
                "request_root_confirmation arguments require exact keys: "
                "hypothesis_id, candidate_ref, defect_fingerprint"
            )
        if arguments != expected_arguments:
            raise ValueError(
                "request_root_confirmation arguments must match the active hypothesis, current node, and defect fingerprint"
            )
    confidence = _number(value.get("confidence"), "confidence")
    if status != "unknown" and confidence <= 0.0:
        raise ValueError("non-unknown status requires positive confidence")
    return CausalStepJudgment(
        current_node_ref=request.current_node.ref,
        current_defect_status=status,
        current_defect_reason=reason,
        predecessors=tuple(assessments),
        candidate_introduction=introduction,
        missing_evidence=missing_evidence,
        suggested_investigation=dict(investigation) if investigation is not None else None,
        unselected_predecessor_refs=unselected,
        confidence=confidence,
    )


def causal_step_from_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> CausalStepJudgment:
    return validate_causal_step_payload(value, request=request)


def reason_identifies_candidate_local_defect(reason: str) -> bool:
    normalized = " ".join(reason.lower().split())
    explicit_phrases = (
        "itself contains",
        "itself carries",
        "already contains",
        "already carries",
        "contains the same defect",
        "carries the same defect",
    )
    if any(phrase in normalized for phrase in explicit_phrases):
        return True
    subject = r"(?:prompt|requirement|message|context|llm call|task loop)"
    defect = r"(?:incorrect|ambiguous|contradictory|defective|incomplete)"
    return bool(
        re.search(rf"\b{subject}\b.{{0,24}}\b(?:itself\s+)?(?:is|was)\s+{defect}\b", normalized)
        or re.search(
            rf"\b{subject}\b.{{0,24}}\b(?:omits|omitted|contradicts|misstates|misstated)\b",
            normalized,
        )
    )


_ENVELOPE_KEYS = {
    "raw_ref",
    "resolved_ref",
    "resolution_status",
    "provenance_class",
}
_ENVELOPE_SHAPE_KEYS = {"raw_ref", "resolved_ref", "resolution_status"}
_TASK2_MANIFEST_KEYS = {
    "node_ref",
    "referenced_artifact_ids",
    "hydrated_artifacts",
    "missing_artifact_ids",
    "truncated_artifact_ids",
}
_TASK2_ARTIFACT_STATUS_KEYS = {
    "raw_ref",
    "canonical_ref",
    "resolution_status",
    "availability",
    "hydration_status",
}
_ALLOWED_PROVENANCE = {"recorded", "reconstructed", "inferred"}
_ALLOWED_RESOLUTION = {
    "resolved",
    "unresolved",
    "ambiguous",
    "missing",
    "truncated",
    "unknown",
}
_BLOCKING_STATUS_VALUES = {
    "budget_exhausted",
    "circuit_open",
    "exhausted",
    "not_available",
    "not_hydrated",
    "not_requested",
    "unavailable",
    "unknown",
    "unresolved",
    "ambiguous",
    "missing",
    "truncated",
    "provider_error",
    "provider_unavailable",
}
_PROVENANCE_KEYS = {
    "edge_metadata",
    "evidence_type",
    "edge_origin",
    "edge_provenance",
    "evidence_metadata",
    "inference",
    "inference_method",
    "inference_metadata",
    "lineage",
    "method",
    "origin",
    "provenance",
    "provenance_class",
    "relation",
    "source",
}
_PROVENANCE_CONTEXT_FIELDS = {
    "evidence_type",
    "kind",
    "method",
    "origin",
    "relation",
    "type",
}
_SEMANTIC_TOKEN_ALIASES = {
    "availability": "available",
    "completeness": "complete",
    "failure": "failed",
    "grounding": "grounded",
    "hydration": "hydrated",
    "resolution": "resolved",
    "tripped": "open",
}
_STATUS_KEY_TOKENS = {
    "availability",
    "hydration",
    "outcome",
    "resolution",
    "result",
    "state",
    "status",
}
_CONTEXTUAL_FAILURE_STATES = {
    "error",
    "failed",
    "open",
    "timeout",
    "unavailable",
}
_SEMANTIC_TEXT_FIELDS = {
    "actual",
    "body",
    "content",
    "description",
    "excerpt",
    "expected",
    "mechanism",
    "rationale",
    "reason",
    "summary",
    "text",
    "title",
}


def _normalized_artifact_id(value: Any) -> str:
    return str(value or "").strip().removeprefix("artifact:")


def _nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set)):
        return bool(value)
    return bool(value)


def _is_provenance_key(key: str) -> bool:
    return (
        key in _PROVENANCE_KEYS
        or "provenance" in key
        or "inference" in key
        or "lineage" in key
        or "evidence_metadata" in key
        or "edge_metadata" in key
        or key.startswith("inference_")
        or key.endswith("_origin")
        or key.endswith("_method")
        or key.endswith("_source")
    )


def _key_tokens(key: str) -> Set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", key.lower()) if token}


def _semantic_tokens(key: str) -> Set[str]:
    tokens = _key_tokens(key)
    return tokens | {
        _SEMANTIC_TOKEN_ALIASES[token]
        for token in tokens
        if token in _SEMANTIC_TOKEN_ALIASES
    }


def _semantic_state(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return _SEMANTIC_TOKEN_ALIASES.get(normalized, normalized)


def _is_temporal_provenance_value(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("temporal_") or "temporal" in normalized


def _is_provider_context_key(key: str) -> bool:
    tokens = _semantic_tokens(key)
    if tokens.intersection({"circuit", "judge", "provider", "transport"}):
        return True
    if {"request", "tool"}.issubset(tokens):
        return False
    return "request" in tokens


def _is_grounding_status_context_key(key: str) -> bool:
    return any(token in key for token in ("artifact", "evidence", "reference"))


def _is_missing_evidence_context_key(key: str) -> bool:
    return any(
        token in key for token in ("missing", "unresolved", "truncated", "gap")
    ) and any(
        token in key for token in ("artifact", "evidence", "reference")
    )


def _is_reference_key(key: str) -> bool:
    return (
        key in {
            "ref",
            "refs",
            "node_id",
            "node_ids",
            "edge_id",
            "edge_ids",
            "artifact_id",
            "artifact_ids",
            "referenced_artifact_ids",
            "missing_artifact_ids",
            "truncated_artifact_ids",
        }
        or key.endswith("_ref")
        or key.endswith("_refs")
    ) and key not in {"raw_ref", "resolved_ref"}


def _is_reference_container_key(key: str) -> bool:
    return (
        key in {"reference", "references"}
        or key.endswith("_reference")
        or key.endswith("_references")
    )


def _is_semantic_metadata_key(key: str) -> bool:
    return (
        key in {
            "artifact_status",
            "confidence",
            "decisive",
            "fact_kind",
            "hash",
            "inference_metadata",
            "kind",
            "label",
            "missing",
            "owner_reference",
            "path",
            "reference_kind",
            "status",
            "timestamp",
            "truncated",
        }
        or key in _ENVELOPE_KEYS
        or _is_provenance_key(key)
        or _is_reference_key(key)
        or _is_reference_container_key(key)
        or key.endswith("_status")
        or key.endswith("_budget_exhausted")
        or key.startswith("missing_")
        or key.startswith("unresolved_")
        or key.startswith("truncated_")
        or key.startswith("provider_")
    )


def _looks_like_artifact_fact(value: Mapping[str, Any], parent_key: str) -> bool:
    if parent_key == "artifact_status":
        return False
    return (
        "artifact_id" in value
        or str(value.get("reference_kind") or "").strip().lower() == "artifact"
        or parent_key in {"artifact", "artifacts", "hydrated_artifact", "hydrated_artifacts"}
    )


@dataclass(frozen=True)
class _ConfirmationFactTreeResult:
    grounded_refs: Set[str]
    candidate_semantic_fragments: Tuple[str, ...]


@dataclass(frozen=True)
class _FactTreeContext:
    in_provider_context: bool = False
    in_provenance_context: bool = False
    in_grounding_status_context: bool = False
    in_missing_evidence_context: bool = False
    artifact_envelope_ref: str = ""

    def descend(
        self,
        key: str,
        *,
        artifact_envelope_ref: Optional[str] = None,
    ) -> "_FactTreeContext":
        return _FactTreeContext(
            in_provider_context=(
                self.in_provider_context or _is_provider_context_key(key)
            ),
            in_provenance_context=(
                self.in_provenance_context or _is_provenance_key(key)
            ),
            in_grounding_status_context=(
                self.in_grounding_status_context
                or _is_grounding_status_context_key(key)
            ),
            in_missing_evidence_context=(
                self.in_missing_evidence_context
                or _is_missing_evidence_context_key(key)
            ),
            artifact_envelope_ref=(
                self.artifact_envelope_ref
                if artifact_envelope_ref is None
                else artifact_envelope_ref
            ),
        )


class _ConfirmationFactTreeValidator:
    """Validate every confirmation fact twice before exposing grounded semantics."""

    def __init__(self, request: RootConfirmationRequest) -> None:
        self.request = request
        self.errors: List[str] = []
        self.grounded_refs: Set[str] = set()
        self.resolved_envelopes: Dict[int, str] = {}
        self.validated_manifests: Set[int] = set()
        self.hydrated_artifact_nodes: Set[int] = set()
        self.hydrated_artifact_refs: Set[str] = set()
        self.hydrated_artifact_fragments: Dict[int, str] = {}
        self.pending_artifact_statuses: List[
            Tuple[Mapping[str, Any], str, str]
        ] = []
        self.validated_artifact_status_nodes: Set[int] = set()
        self.roots = (
            ("candidate_reference", request.candidate_reference),
            ("recursive_path_references", request.recursive_path_references),
            ("supporting_evidence", request.supporting_evidence),
            ("opposing_evidence", request.opposing_evidence),
            ("competing hypothesis", request.competing_hypotheses),
            ("task_obligations", request.task_obligations),
        )

    def validate(self) -> _ConfirmationFactTreeResult:
        for path, value in self.roots:
            self._pass_one(
                value,
                path=path,
                parent_key="",
                context=_FactTreeContext().descend(path),
            )
        self._validate_artifact_statuses()
        self._validate_required_roots()
        for path, value in self.roots:
            self._pass_two(
                value,
                path=path,
                parent_key="",
                candidate_local=False,
                context=_FactTreeContext().descend(path),
            )
        self._validate_competing_hypotheses()
        if self.errors:
            unique_errors = list(dict.fromkeys(self.errors))
            raise ValueError(
                "confirmation fact tree is ineligible: {0}".format(
                    "; ".join(unique_errors)
                )
            )
        fragments: List[str] = []
        self._collect_candidate_semantics(
            self.request.candidate_reference,
            fragments=fragments,
            parent_key="candidate_reference",
            candidate_local=False,
        )
        self._collect_candidate_semantics(
            self.request.supporting_evidence,
            fragments=fragments,
            parent_key="supporting_evidence",
            candidate_local=False,
        )
        normalized_fragments = tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    re.sub(r"\s+", " ", fragment).strip().lower()
                    for fragment in fragments
                )
                if normalized
            )
        )
        return _ConfirmationFactTreeResult(
            set(self.grounded_refs), normalized_fragments
        )

    def _error(self, path: str, message: str) -> None:
        self.errors.append("{0}: {1}".format(path, message))

    def _register_ref(self, value: Any) -> None:
        ref = str(value or "").strip()
        if not ref:
            return
        self.grounded_refs.add(ref)
        if ref.startswith("artifact:"):
            self.grounded_refs.add(ref.removeprefix("artifact:"))

    def _envelope_errors(self, value: Mapping[str, Any]) -> List[str]:
        errors: List[str] = []
        missing = _ENVELOPE_KEYS - set(value)
        if missing:
            errors.append(
                "reference envelope requires {0}".format(", ".join(sorted(missing)))
            )
            return errors
        raw_ref = str(value.get("raw_ref") or "").strip()
        resolved_ref = str(value.get("resolved_ref") or "").strip()
        resolution = str(value.get("resolution_status") or "").strip().lower()
        provenance = str(value.get("provenance_class") or "").strip()
        if not raw_ref:
            errors.append("reference envelope raw_ref must be non-empty")
        if resolution not in _ALLOWED_RESOLUTION:
            errors.append("reference envelope resolution_status is invalid")
        if (resolution == "resolved") != bool(resolved_ref):
            errors.append("reference envelope has contradictory raw_ref/resolved_ref fields")
        if provenance not in _ALLOWED_PROVENANCE:
            errors.append(
                "provenance_class must be exactly recorded, reconstructed, or inferred"
            )
        errors.extend(self._inference_errors(value))
        return errors

    def _inference_errors(self, value: Mapping[str, Any]) -> List[str]:
        provenance = str(value.get("provenance_class") or "").strip()
        if provenance != "inferred":
            return []
        metadata = value.get("inference_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else value
        evidence_type = str(metadata.get("evidence_type") or "").strip().lower()
        inference_method = str(metadata.get("inference_method") or "").strip().lower()
        if not evidence_type or not inference_method:
            return ["inferred provenance requires auditable inference metadata"]
        if "inferred" not in evidence_type:
            return ["inferred provenance contradicts evidence_type"]
        return []

    def _temporal_errors(
        self,
        value: Mapping[str, Any],
        *,
        in_provenance_context: bool,
    ) -> List[str]:
        errors: List[str] = []
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            provenance_value = _is_provenance_key(key) or (
                in_provenance_context and key in _PROVENANCE_CONTEXT_FIELDS
            )
            if not provenance_value or not isinstance(child, str):
                continue
            if _is_temporal_provenance_value(child):
                errors.append("temporal provenance field {0} is confirmation-ineligible".format(key))
        return errors

    def _pass_one(
        self,
        value: Any,
        *,
        path: str,
        parent_key: str,
        context: _FactTreeContext,
    ) -> None:
        if (
            isinstance(value, str)
            and context.in_provenance_context
            and _is_temporal_provenance_value(value)
        ):
            self._error(path, "temporal provenance value is confirmation-ineligible")
            return
        if isinstance(value, Mapping):
            artifact_status_shaped = parent_key == "artifact_status"
            envelope_shaped = (
                bool(_ENVELOPE_SHAPE_KEYS.intersection(value))
                and not artifact_status_shaped
            )
            provenance_errors: List[str] = []
            if "provenance_class" in value:
                provenance = str(value.get("provenance_class") or "").strip()
                if provenance not in _ALLOWED_PROVENANCE:
                    provenance_errors.append(
                        "provenance_class must be exactly recorded, reconstructed, or inferred",
                    )
                provenance_errors.extend(self._inference_errors(value))
            temporal_errors = self._temporal_errors(
                value, in_provenance_context=context.in_provenance_context
            )
            for error in provenance_errors + temporal_errors:
                self._error(path, error)
            current_artifact_ref = context.artifact_envelope_ref
            if envelope_shaped:
                envelope_errors = self._envelope_errors(value)
                for error in envelope_errors:
                    self._error(path, error)
                if (
                    not envelope_errors
                    and not provenance_errors
                    and not temporal_errors
                    and value.get("resolution_status") == "resolved"
                ):
                    resolved_ref = str(value.get("resolved_ref") or "").strip()
                    self.resolved_envelopes[id(value)] = resolved_ref
                    self._register_ref(value.get("raw_ref"))
                    self._register_ref(resolved_ref)
                    if resolved_ref.startswith("artifact:"):
                        current_artifact_ref = resolved_ref
            if parent_key == "artifact_hydration":
                self._register_hydration_manifest(value, path=path)
            if artifact_status_shaped:
                self.pending_artifact_statuses.append(
                    (value, path, context.artifact_envelope_ref)
                )
            child_context = context.descend(
                "", artifact_envelope_ref=current_artifact_ref
            )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                self._pass_one(
                    child,
                    path="{0}.{1}".format(path, raw_key),
                    parent_key=key,
                    context=child_context.descend(key),
                )
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                self._pass_one(
                    child,
                    path="{0}[{1}]".format(path, index),
                    parent_key=parent_key,
                    context=context,
                )

    def _artifact_id_list(self, value: Any, *, path: str) -> Optional[Set[str]]:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) for item in value
        ):
            self._error(path, "must be a list of artifact ids")
            return None
        normalized = [_normalized_artifact_id(item) for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            self._error(path, "contains empty or duplicate artifact ids")
            return None
        return set(normalized)

    def _register_hydration_manifest(self, value: Mapping[str, Any], *, path: str) -> None:
        missing_keys = _TASK2_MANIFEST_KEYS - set(value)
        audit_keys = {
            "referenced_artifact_ids",
            "missing_artifact_ids",
            "truncated_artifact_ids",
        }
        judge_visible_manifest = missing_keys == audit_keys
        if missing_keys and not judge_visible_manifest:
            self._error(
                path,
                "Task 2 hydration manifest is missing {0}".format(
                    ", ".join(sorted(missing_keys))
                ),
            )
            return
        hydrated_value = value.get("hydrated_artifacts")
        if not isinstance(hydrated_value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in hydrated_value
        ):
            self._error(path, "hydrated_artifacts must be a list of objects")
            return
        if judge_visible_manifest:
            referenced = {
                _normalized_artifact_id(item.get("artifact_id"))
                for item in hydrated_value
                if isinstance(item, Mapping)
            }
            missing: Optional[Set[str]] = set()
            truncated: Optional[Set[str]] = set()
        else:
            referenced = self._artifact_id_list(
                value.get("referenced_artifact_ids"),
                path="{0}.referenced_artifact_ids".format(path),
            )
            missing = self._artifact_id_list(
                value.get("missing_artifact_ids"),
                path="{0}.missing_artifact_ids".format(path),
            )
            truncated = self._artifact_id_list(
                value.get("truncated_artifact_ids"),
                path="{0}.truncated_artifact_ids".format(path),
            )
        if referenced is None or missing is None or truncated is None:
            return
        node_ref = str(value.get("node_ref") or "").strip()
        if not node_ref:
            self._error(path, "manifest owner identity is missing")
        hydrated: Set[str] = set()
        valid_items: List[Tuple[Mapping[str, Any], str, str]] = []
        for index, artifact in enumerate(hydrated_value):
            item_path = "{0}.hydrated_artifacts[{1}]".format(path, index)
            raw_artifact_id = artifact.get("artifact_id")
            artifact_id = _normalized_artifact_id(raw_artifact_id)
            if not isinstance(raw_artifact_id, str) or raw_artifact_id != artifact_id:
                self._error(item_path, "artifact_id must be a canonical unprefixed string")
                continue
            if not artifact_id or artifact_id in hydrated:
                self._error(item_path, "has an empty or duplicate artifact_id")
                continue
            hydrated.add(artifact_id)
            if artifact_id not in referenced:
                self._error(item_path, "is not listed in referenced_artifact_ids")
                continue
            if artifact.get("missing") is True:
                self._error(item_path, "artifact is missing")
                continue
            if bool(artifact.get("truncated")) != (artifact_id in truncated):
                self._error(item_path, "has contradictory truncation fields")
                continue
            if artifact_id in missing or artifact_id in truncated:
                self._error(item_path, "artifact is missing or truncated")
                continue
            content = artifact.get("content")
            if not isinstance(content, str):
                self._error(item_path, "content must be UTF-8 text")
                continue
            content_bytes = content.encode("utf-8")
            content_hash = artifact.get("content_hash")
            expected_hash = "sha256:{0}".format(hashlib.sha256(content_bytes).hexdigest())
            if not isinstance(content_hash, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", content_hash
            ):
                self._error(item_path, "content_hash must be canonical sha256")
                continue
            if content_hash != expected_hash:
                self._error(item_path, "content_hash does not match actual bytes")
                continue
            byte_count = artifact.get("byte_count")
            if (
                isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count != len(content_bytes)
            ):
                self._error(item_path, "byte_count must equal the actual UTF-8 byte count")
                continue
            byte_range = artifact.get("byte_range")
            if (
                not isinstance(byte_range, (list, tuple))
                or len(byte_range) != 2
                or any(isinstance(item, bool) or not isinstance(item, int) for item in byte_range)
            ):
                self._error(item_path, "byte_range must contain two integers")
                continue
            start, end = byte_range
            if start < 0 or end < start or end > len(content_bytes):
                self._error(item_path, "byte_range is out of bounds or reversed")
                continue
            try:
                fragment = content_bytes[start:end].decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                self._error(item_path, "byte_range must align to UTF-8 boundaries")
                continue
            owner = artifact.get("owner_reference")
            if not isinstance(owner, Mapping) or self._envelope_errors(owner):
                self._error(item_path, "owner_reference must be a resolved reference envelope")
                continue
            if (
                str(owner.get("resolution_status") or "") != "resolved"
                or str(owner.get("resolved_ref") or "") != node_ref
            ):
                self._error(item_path, "owner_reference contradicts manifest owner identity")
                continue
            if _ENVELOPE_SHAPE_KEYS.intersection(artifact) and self._envelope_errors(
                artifact
            ):
                continue
            valid_items.append((artifact, artifact_id, fragment))
        if not missing.issubset(referenced) or not truncated.issubset(referenced):
            self._error(path, "contains artifact ids absent from referenced_artifact_ids")
        if referenced != hydrated | missing:
            self._error(path, "does not account for every referenced artifact")
        if missing or truncated or len(valid_items) != len(hydrated_value):
            return
        self.validated_manifests.add(id(value))
        for artifact, artifact_id, fragment in valid_items:
            self.hydrated_artifact_nodes.add(id(artifact))
            self.hydrated_artifact_fragments[id(artifact)] = fragment
            self.hydrated_artifact_refs.add(artifact_id)
            self._register_ref(artifact_id)
            self._register_ref("artifact:{0}".format(artifact_id))

    def _validate_artifact_statuses(self) -> None:
        for value, path, parent_artifact_ref in self.pending_artifact_statuses:
            errors: List[str] = []
            if not parent_artifact_ref:
                errors.append(
                    "Task 2 artifact status requires a validated parent artifact envelope"
                )
            missing_keys = _TASK2_ARTIFACT_STATUS_KEYS - set(value)
            if missing_keys:
                errors.append(
                    "Task 2 artifact status is missing {0}".format(
                        ", ".join(sorted(missing_keys))
                    )
                )
            raw_ref = str(value.get("raw_ref") or "").strip()
            canonical_ref = str(value.get("canonical_ref") or "").strip()
            raw_id = _normalized_artifact_id(raw_ref)
            canonical_id = _normalized_artifact_id(canonical_ref)
            if not raw_id or not canonical_id:
                errors.append("Task 2 artifact status requires non-empty raw and canonical refs")
            if canonical_ref != "artifact:{0}".format(canonical_id):
                errors.append("Task 2 artifact status canonical_ref is not canonical")
            if raw_id != canonical_id:
                errors.append("Task 2 artifact status raw_ref contradicts canonical_ref")
            if canonical_ref != parent_artifact_ref:
                errors.append(
                    "Task 2 artifact status canonical_ref contradicts parent artifact envelope"
                )
            parent_artifact_id = _normalized_artifact_id(parent_artifact_ref)
            if raw_id != parent_artifact_id:
                errors.append(
                    "Task 2 artifact status raw_ref contradicts parent artifact envelope"
                )
            resolved_ref = str(value.get("resolved_ref") or "").strip()
            if resolved_ref and resolved_ref != canonical_ref:
                errors.append("Task 2 artifact status resolved_ref contradicts canonical_ref")
            artifact_id = _normalized_artifact_id(value.get("artifact_id"))
            if artifact_id and artifact_id != canonical_id:
                errors.append("Task 2 artifact status artifact_id contradicts canonical_ref")
            if artifact_id and artifact_id != parent_artifact_id:
                errors.append(
                    "Task 2 artifact status artifact_id contradicts parent artifact envelope"
                )
            if str(value.get("resolution_status") or "").strip().lower() != "resolved":
                errors.append("Task 2 artifact status is unresolved")
            if str(value.get("availability") or "").strip().lower() != "available":
                errors.append("Task 2 artifact status is not available")
            if str(value.get("hydration_status") or "").strip().lower() != "hydrated":
                errors.append("Task 2 artifact status is not hydrated")
            if canonical_id not in self.hydrated_artifact_refs:
                errors.append(
                    "Task 2 artifact status is absent from a validated hydration manifest"
                )
            provenance = value.get("provenance_class")
            if provenance is not None and str(provenance).strip() != "recorded":
                errors.append("Task 2 artifact status provenance must be recorded")
            errors.extend(
                self._temporal_errors(value, in_provenance_context=False)
            )
            errors.extend(
                self._blocking_errors(
                    value,
                    context=_FactTreeContext(
                        in_grounding_status_context=True,
                        artifact_envelope_ref=parent_artifact_ref,
                    ),
                )
            )
            for error in errors:
                self._error(path, error)
            if not errors:
                self.validated_artifact_status_nodes.add(id(value))

    def _validate_required_roots(self) -> None:
        candidate = self.resolved_envelopes.get(id(self.request.candidate_reference))
        if candidate != self.request.candidate_ref:
            self._error(
                "candidate_reference",
                "unresolved candidate requires a complete resolved candidate reference envelope",
            )
        if len(self.request.recursive_path_references) != len(self.request.recursive_path):
            self._error(
                "recursive_path_references",
                "every navigation path ref requires a path reference envelope",
            )
        for index, navigation_ref in enumerate(self.request.recursive_path):
            if index >= len(self.request.recursive_path_references):
                continue
            resolved = self.resolved_envelopes.get(
                id(self.request.recursive_path_references[index])
            )
            if resolved != navigation_ref:
                self._error(
                    "recursive_path_references[{0}]".format(index),
                    "path reference envelope is unresolved or ambiguous",
                )
        for label, facts in (
            ("supporting_evidence", self.request.supporting_evidence),
            ("opposing_evidence", self.request.opposing_evidence),
        ):
            for index, fact in enumerate(facts):
                if id(fact) not in self.resolved_envelopes:
                    if (
                        label == "opposing_evidence"
                        and str(fact.get("resolution_status") or "").lower()
                        != "resolved"
                    ):
                        self._error(
                            "{0}[{1}]".format(label, index),
                            "unresolved opposing evidence can invalidate root confirmation",
                        )
                    self._error(
                        "{0}[{1}]".format(label, index),
                        "fact requires a complete resolved reference envelope",
                    )

    def _blocking_errors(
        self,
        value: Mapping[str, Any],
        *,
        context: _FactTreeContext,
    ) -> List[str]:
        errors: List[str] = []
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            key_tokens = _semantic_tokens(key)
            provider_context = (
                context.in_provider_context or _is_provider_context_key(key)
            )
            grounding_status_context = (
                context.in_grounding_status_context
                or _is_grounding_status_context_key(key)
            )
            missing_evidence_context = (
                context.in_missing_evidence_context
                or _is_missing_evidence_context_key(key)
            )
            status_like = bool(key_tokens.intersection(_STATUS_KEY_TOKENS))
            if status_like and isinstance(child, str):
                status = child.strip().lower()
                semantic_status = _semantic_state(child)
                if (
                    status in _BLOCKING_STATUS_VALUES
                    or semantic_status in _BLOCKING_STATUS_VALUES
                ):
                    errors.append("blocking {0}={1}".format(key, status))
                if (
                    provider_context or missing_evidence_context
                ) and semantic_status in _CONTEXTUAL_FAILURE_STATES:
                    errors.append("blocking contextual {0}={1}".format(key, status))
            if (
                provider_context
                and key_tokens.intersection(_CONTEXTUAL_FAILURE_STATES)
                and _nonempty(child)
            ):
                errors.append("blocking declared provider/circuit failure {0}".format(key))
            if provider_context and "reason" in key_tokens and _nonempty(child):
                errors.append("blocking provider/circuit reason {0}".format(key))
            if isinstance(child, bool):
                true_blockers = {
                    "error",
                    "exhausted",
                    "missing",
                    "truncated",
                    "unresolved",
                }
                if provider_context:
                    true_blockers.update({"failed", "open", "unavailable"})
                if child and key_tokens.intersection(true_blockers):
                    errors.append("blocking {0}=true".format(key))
                if (
                    not child
                    and (
                        (
                            grounding_status_context
                            and key_tokens.intersection(
                                {
                                    "available",
                                    "complete",
                                    "grounded",
                                    "hydrated",
                                    "resolved",
                                }
                            )
                        )
                        or (
                            provider_context
                            and key_tokens.intersection(
                                {"available", "hydrated", "resolved"}
                            )
                        )
                    )
                ):
                    errors.append("blocking {0}=false".format(key))
            if (
                isinstance(child, (int, float))
                and not isinstance(child, bool)
                and math.isfinite(float(child))
                and child > 0
                and key_tokens.intersection(
                    {
                        "error",
                        "exhausted",
                        "failed",
                        "gap",
                        "missing",
                        "truncated",
                        "unresolved",
                    }
                )
            ):
                errors.append("blocking positive {0}".format(key))
            if not _nonempty(child):
                continue
            if key in {
                "missing_evidence",
                "unresolved_refs",
                "unresolved_references",
                "missing_artifact_ids",
                "truncated_artifact_ids",
                "provider_circuit_reason",
                "blocking_reasons",
                "evidence_gaps",
                "gaps",
                "missing",
                "truncated",
                "unresolved",
                "unavailable",
            }:
                errors.append("blocking {0}".format(key))
            elif (
                key.startswith("missing_")
                or (key.startswith("unresolved_") and key != "unresolved_questions")
                or key.startswith("truncated_")
            ) and isinstance(child, (Mapping, list, tuple, set, str)):
                errors.append("blocking {0}".format(key))
            elif key.endswith("_gaps") and isinstance(
                child, (Mapping, list, tuple, set, str)
            ):
                errors.append("blocking {0}".format(key))
        return errors

    def _reference_is_grounded(self, value: Any) -> bool:
        ref = str(value or "").strip()
        if not ref:
            return False
        if ref in self.grounded_refs:
            return True
        artifact_id = _normalized_artifact_id(ref)
        return artifact_id in self.hydrated_artifact_refs

    def _validate_reference_value(self, value: Any, *, path: str) -> None:
        if isinstance(value, str):
            if not self._reference_is_grounded(value):
                self._error(path, "bare or unregistered ref is unresolved: {0}".format(value))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                child_path = "{0}[{1}]".format(path, index)
                if isinstance(child, Mapping):
                    if not _ENVELOPE_SHAPE_KEYS.intersection(child):
                        self._error(child_path, "reference entry requires an envelope")
                elif not self._reference_is_grounded(child):
                    self._error(
                        child_path,
                        "bare or unregistered ref is unresolved: {0}".format(child),
                    )
            return
        if isinstance(value, Mapping):
            if not _ENVELOPE_SHAPE_KEYS.intersection(value):
                self._error(path, "reference value requires an envelope")
            return
        self._error(path, "reference value must be a grounded ref or envelope")

    def _pass_two(
        self,
        value: Any,
        *,
        path: str,
        parent_key: str,
        candidate_local: bool,
        context: _FactTreeContext,
    ) -> None:
        if (
            isinstance(value, str)
            and context.in_provenance_context
            and _is_temporal_provenance_value(value)
        ):
            self._error(path, "temporal provenance value is confirmation-ineligible")
            return
        if isinstance(value, Mapping):
            artifact_status_shaped = parent_key == "artifact_status"
            envelope_shaped = (
                bool(_ENVELOPE_SHAPE_KEYS.intersection(value))
                and not artifact_status_shaped
            )
            current_local = candidate_local
            current_artifact_ref = context.artifact_envelope_ref
            if envelope_shaped:
                for error in self._envelope_errors(value):
                    self._error(path, error)
                resolved = self.resolved_envelopes.get(id(value))
                if resolved:
                    current_local = resolved == self.request.candidate_ref
                    if resolved.startswith("artifact:"):
                        current_artifact_ref = resolved
            owner = value.get("owner_reference")
            if isinstance(owner, Mapping):
                owner_ref = self.resolved_envelopes.get(id(owner))
                if owner_ref == self.request.candidate_ref:
                    current_local = True
            for error in self._temporal_errors(
                value, in_provenance_context=context.in_provenance_context
            ):
                self._error(path, error)
            for error in self._blocking_errors(value, context=context):
                self._error(path, error)
            if (
                artifact_status_shaped
                and id(value) not in self.validated_artifact_status_nodes
            ):
                self._error(path, "artifact_status is not valid for its parent envelope")
            artifact_fact = _looks_like_artifact_fact(value, parent_key)
            hydrated_item = id(value) in self.hydrated_artifact_nodes
            if artifact_fact and not hydrated_item and not envelope_shaped:
                self._error(path, "artifact reference envelope is required")
            if artifact_fact and envelope_shaped:
                artifact_id = _normalized_artifact_id(value.get("artifact_id"))
                resolved = self.resolved_envelopes.get(id(value), "")
                if artifact_id and _normalized_artifact_id(resolved) != artifact_id:
                    self._error(path, "artifact_id contradicts resolved_ref")
                if candidate_local and isinstance(owner, Mapping):
                    owner_ref = self.resolved_envelopes.get(id(owner))
                    if owner_ref and owner_ref != self.request.candidate_ref:
                        self._error(path, "candidate artifact has a contradictory owner")
            child_context = context.descend(
                "", artifact_envelope_ref=current_artifact_ref
            )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                child_path = "{0}.{1}".format(path, raw_key)
                if _is_reference_key(key):
                    self._validate_reference_value(child, path=child_path)
                if _is_reference_container_key(key):
                    if isinstance(child, Mapping):
                        if not _ENVELOPE_SHAPE_KEYS.intersection(child):
                            self._error(child_path, "reference container requires an envelope")
                    elif isinstance(child, (list, tuple)):
                        if any(
                            not isinstance(item, Mapping)
                            or not _ENVELOPE_SHAPE_KEYS.intersection(item)
                            for item in child
                        ):
                            self._error(
                                child_path,
                                "reference collection requires complete envelopes",
                            )
                    else:
                        self._error(child_path, "bare reference requires an envelope")
                self._pass_two(
                    child,
                    path=child_path,
                    parent_key=key,
                    candidate_local=current_local,
                    context=child_context.descend(key),
                )
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                self._pass_two(
                    child,
                    path="{0}[{1}]".format(path, index),
                    parent_key=parent_key,
                    candidate_local=candidate_local,
                    context=context,
                )

    def _validate_competing_hypotheses(self) -> None:
        for index, hypothesis in enumerate(self.request.competing_hypotheses):
            path = "competing hypothesis[{0}]".format(index)
            status = str(hypothesis.get("status") or "").strip().lower()
            if status not in {"active", "supported", "unresolved", "rejected", "superseded"}:
                self._error(
                    path,
                    "has an invalid lifecycle status {0}".format(
                        status or "missing status"
                    ),
                )
            if status in {"active", "supported", "unresolved"}:
                required = {
                    "hypothesis_id",
                    "hypothesis_semantic_hash",
                    "claim",
                    "active_defect",
                    "candidate_reference",
                    "supporting_evidence",
                    "opposing_evidence",
                    "unresolved_questions",
                    "counterfactual",
                    "confirmation_identity",
                    "recursive_path",
                    "requires_independent_confirmation",
                }
                missing = required - set(hypothesis)
                if missing:
                    self._error(path, "missing auditable competitor fields {0}".format(
                        ", ".join(sorted(missing))
                    ))
                if not str(hypothesis.get("claim") or "").strip():
                    self._error(path, "competitor claim must be non-empty")
                counterfactual = hypothesis.get("counterfactual")
                if not isinstance(counterfactual, Mapping) or not str(
                    counterfactual.get("intervention_ref") or ""
                ).strip():
                    self._error(path, "competitor counterfactual must be structured")
                recursive_path = hypothesis.get("recursive_path")
                requires_confirmation = hypothesis.get(
                    "requires_independent_confirmation"
                )
                if (
                    not isinstance(recursive_path, (list, tuple))
                    or any(not isinstance(item, str) for item in recursive_path)
                ):
                    self._error(path, "competitor recursive_path must be a list of strings")
                if not isinstance(requires_confirmation, bool):
                    self._error(
                        path,
                        "competitor requires_independent_confirmation must be boolean",
                    )
                active_defect = hypothesis.get("active_defect")
                candidate_reference = hypothesis.get("candidate_reference")
                expected_identity = confirmation_identity_for(
                    hypothesis_id=str(hypothesis.get("hypothesis_id") or ""),
                    hypothesis_semantic_hash=str(
                        hypothesis.get("hypothesis_semantic_hash") or ""
                    ),
                    candidate_ref=(
                        str(candidate_reference.get("resolved_ref") or "")
                        if isinstance(candidate_reference, Mapping)
                        else ""
                    ),
                    defect_fingerprint=(
                        str(active_defect.get("fingerprint") or "")
                        if isinstance(active_defect, Mapping)
                        else ""
                    ),
                    recursive_path=(
                        tuple(recursive_path)
                        if isinstance(recursive_path, (list, tuple))
                        else ()
                    ),
                    seed_binding_identity=str(
                        hypothesis.get("seed_binding_identity") or ""
                    ),
                )
                if str(hypothesis.get("confirmation_identity") or "") != expected_identity:
                    self._error(path, "competitor confirmation identity is invalid")
                candidate = hypothesis.get("candidate_reference")
                if not isinstance(candidate, Mapping) or not str(candidate.get("content") or "").strip():
                    self._error(path, "competitor candidate semantics must be non-empty")
                for evidence_kind in ("supporting_evidence", "opposing_evidence"):
                    evidence_items = hypothesis.get(evidence_kind)
                    if not isinstance(evidence_items, (list, tuple)):
                        self._error(path, "{0} must be a list".format(evidence_kind))
                        continue
                    for item in evidence_items:
                        if not isinstance(item, Mapping) or not str(item.get("reason") or "").strip():
                            self._error(path, "{0} requires reasons".format(evidence_kind))

    def _collect_candidate_semantics(
        self,
        value: Any,
        *,
        fragments: List[str],
        parent_key: str,
        candidate_local: bool,
    ) -> None:
        if isinstance(value, Mapping):
            if id(value) in self.hydrated_artifact_fragments:
                if candidate_local:
                    fragments.append(self.hydrated_artifact_fragments[id(value)])
                return
            current_local = candidate_local
            resolved = self.resolved_envelopes.get(id(value))
            if resolved is not None:
                current_local = resolved == self.request.candidate_ref
            owner = value.get("owner_reference")
            if isinstance(owner, Mapping):
                current_local = (
                    self.resolved_envelopes.get(id(owner)) == self.request.candidate_ref
                )
            if id(value) in self.validated_manifests:
                current_local = (
                    str(value.get("node_ref") or "") == self.request.candidate_ref
                )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                if _is_semantic_metadata_key(key):
                    continue
                if current_local and isinstance(child, str) and (
                    key in _SEMANTIC_TEXT_FIELDS
                    or key.endswith("_content")
                    or key.endswith("_excerpt")
                    or key.endswith("_summary")
                    or key.endswith("_rationale")
                ):
                    fragments.append(child)
                self._collect_candidate_semantics(
                    child,
                    fragments=fragments,
                    parent_key=key,
                    candidate_local=current_local,
                )
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                self._collect_candidate_semantics(
                    child,
                    fragments=fragments,
                    parent_key=parent_key,
                    candidate_local=candidate_local,
                )


def _open_competitor_ids(request: RootConfirmationRequest) -> Tuple[str, ...]:
    return tuple(
        str(item.get("hypothesis_id") or "")
        for item in request.competing_hypotheses
        if str(item.get("status") or "").strip().lower()
        in {"active", "supported", "unresolved"}
    )


def _validate_competitor_comparisons(
    value: Any,
    *,
    request: RootConfirmationRequest,
    confirmation_status: str,
    grounded_refs: Set[str],
) -> Tuple[JsonDict, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("competitor_comparisons must be a list")
    open_ids = _open_competitor_ids(request)
    open_competitors = {
        str(item.get("hypothesis_id") or ""): item
        for item in request.competing_hypotheses
        if str(item.get("hypothesis_id") or "") in open_ids
    }
    comparisons: List[JsonDict] = []
    seen: Set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("competitor_comparisons entries must be objects")
        hypothesis_id = str(item.get("hypothesis_id") or "").strip()
        if not hypothesis_id or hypothesis_id in seen or hypothesis_id not in open_ids:
            raise ValueError("competitor_comparisons must bind each open competitor exactly once")
        seen.add(hypothesis_id)
        competitor = open_competitors[hypothesis_id]
        active_defect = competitor.get("active_defect")
        candidate_reference = competitor.get("candidate_reference")
        expected_identity = (
            str(competitor.get("hypothesis_semantic_hash") or ""),
            str(candidate_reference.get("resolved_ref") or "")
            if isinstance(candidate_reference, Mapping)
            else "",
            str(active_defect.get("fingerprint") or "")
            if isinstance(active_defect, Mapping)
            else "",
            str(competitor.get("confirmation_identity") or ""),
            tuple(str(value) for value in competitor.get("recursive_path") or ()),
            competitor.get("requires_independent_confirmation"),
        )
        actual_identity = (
            str(item.get("hypothesis_semantic_hash") or ""),
            str(item.get("candidate_ref") or ""),
            str(item.get("defect_fingerprint") or ""),
            str(item.get("confirmation_identity") or ""),
            tuple(str(value) for value in item.get("recursive_path") or ()),
            item.get("requires_independent_confirmation"),
        )
        if actual_identity != expected_identity:
            raise ValueError("competitor comparison identity does not match offered facts")
        status = str(item.get("status") or "").strip().lower()
        if status not in {"outperformed", "rejected", "co_root", "unresolved"}:
            raise ValueError("competitor comparison status is invalid")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError("competitor comparison reason must be non-empty")
        evidence_refs = _validate_evidence_refs(
            item.get("evidence_refs", []),
            grounded_refs=grounded_refs,
            field_name="competitor comparison evidence_refs",
        )
        if status != "unresolved" and not evidence_refs:
            raise ValueError("decisive competitor comparison requires grounded evidence refs")
        comparisons.append(
            {
                "hypothesis_id": hypothesis_id,
                "hypothesis_semantic_hash": actual_identity[0],
                "candidate_ref": actual_identity[1],
                "defect_fingerprint": actual_identity[2],
                "confirmation_identity": actual_identity[3],
                "recursive_path": list(actual_identity[4]),
                "requires_independent_confirmation": actual_identity[5],
                "status": status,
                "reason": reason,
                "evidence_refs": list(evidence_refs),
            }
        )
    if set(open_ids) != seen:
        raise ValueError("competitor_comparisons must cover every open competitor")
    if confirmation_status == "confirmed" and any(
        item["status"] == "unresolved" for item in comparisons
    ):
        raise ValueError("unresolved competitor blocks confirmed root")
    return tuple(comparisons)


def _validate_factor_mechanism(
    value: Any,
    *,
    role: str,
    request: RootConfirmationRequest,
    evidence_refs: Tuple[str, ...],
) -> JsonDict:
    if role not in {"contributing_condition", "amplifying_factor"}:
        if value not in (None, {}, FrozenMapping()):
            raise ValueError(
                "factor_mechanism must be null or empty when factor_role={0}".format(role)
            )
        return {}
    if not evidence_refs:
        raise ValueError("factor role requires grounded factor evidence")
    if not isinstance(value, Mapping):
        raise ValueError("factor role requires a structured factor_mechanism")
    expected_type = (
        "enabling_condition" if role == "contributing_condition" else "amplification"
    )
    mechanism_type = str(value.get("mechanism_type") or "").strip()
    source_ref = str(value.get("source_ref") or "").strip()
    target_ref = str(value.get("target_ref") or "").strip()
    effect = str(value.get("effect") or "").strip()
    if mechanism_type != expected_type:
        raise ValueError("factor mechanism_type contradicts factor role")
    if source_ref != request.candidate_ref:
        raise ValueError("factor mechanism source_ref must match candidate")
    if target_ref not in request.recursive_path:
        raise ValueError("factor mechanism target_ref must be grounded in recursive path")
    if source_ref not in evidence_refs or target_ref not in evidence_refs:
        raise ValueError("factor evidence must ground both mechanism source and target")
    if not effect:
        raise ValueError("factor mechanism effect must be non-empty")
    return {
        "mechanism_type": mechanism_type,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "effect": effect,
    }


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
    if status != "unknown" and confidence <= 0.0:
        raise ValueError(
            "non-unknown confirmation requires positive confidence"
        )
    fact_tree = _ConfirmationFactTreeValidator(request).validate()
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs", []),
        grounded_refs=fact_tree.grounded_refs,
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
    factor_role = str(
        value.get("factor_role")
        or {
            "confirmed": "necessary_cause",
            "rejected": "unrelated",
            "unknown": "unknown",
        }[status]
    ).strip().lower()
    allowed_factor_roles = {
        "confirmed": {"necessary_cause"},
        "rejected": {
            "contributing_condition",
            "amplifying_factor",
            "unrelated",
            "unknown",
        },
        "unknown": {"unknown"},
    }[status]
    if factor_role not in allowed_factor_roles:
        raise ValueError(
            "factor_role is inconsistent with confirmation status={0}".format(status)
        )
    competitor_comparisons = _validate_competitor_comparisons(
        value.get("competitor_comparisons", []),
        request=request,
        confirmation_status=status,
        grounded_refs=fact_tree.grounded_refs,
    )
    factor_mechanism = _validate_factor_mechanism(
        value.get("factor_mechanism", {}),
        role=factor_role,
        request=request,
        evidence_refs=evidence_refs,
    )
    counterfactual = (
        "replace_with_semantically_correct_behavior({0}) predicts defect_status={1}; "
        "causal_effect={2}"
    ).format(intervention_ref, predicted_status, causal_effect)
    if status == "confirmed":
        if not evidence_refs:
            raise ValueError("confirmed root requires grounded evidence refs")
        if not excerpt:
            raise ValueError("confirmed root requires a grounded excerpt")
        normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip().lower()
        if not any(
            normalized_excerpt in fragment
            for fragment in fact_tree.candidate_semantic_fragments
        ):
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
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        recursive_path=request.recursive_path,
        seed_binding_identity=request.seed_binding_identity,
        factor_role=factor_role,
        competitor_comparisons=competitor_comparisons,
        factor_mechanism=factor_mechanism,
    )


def preflight_root_confirmation_request(
    request: RootConfirmationRequest,
) -> _ConfirmationFactTreeResult:
    """Validate the complete independent-verifier fact tree before any capability call."""
    return _ConfirmationFactTreeValidator(request).validate()


def bind_root_confirmation(
    confirmation: RootConfirmation,
    *,
    request: RootConfirmationRequest,
) -> RootConfirmation:
    """Bind and validate an offline confirmation result against its exact request."""
    facts = preflight_root_confirmation_request(request)
    if confirmation.candidate_ref != request.candidate_ref:
        raise ValueError("confirmation candidate_ref does not match its request")
    if confirmation.hypothesis_id and confirmation.hypothesis_id != request.hypothesis_id:
        raise ValueError("confirmation is cross-bound to another hypothesis")
    if (
        confirmation.hypothesis_semantic_hash
        and confirmation.hypothesis_semantic_hash != request.hypothesis_semantic_hash
    ):
        raise ValueError("confirmation is cross-bound to another hypothesis semantic hash")
    if (
        confirmation.defect_fingerprint
        and confirmation.defect_fingerprint != request.defect_state.fingerprint
    ):
        raise ValueError("confirmation is cross-bound to another defect")
    if confirmation.recursive_path and confirmation.recursive_path != request.recursive_path:
        raise ValueError("confirmation is cross-bound to another recursive path")
    if (
        confirmation.seed_binding_identity
        and confirmation.seed_binding_identity != request.seed_binding_identity
    ):
        raise ValueError("confirmation is cross-bound to another seed")
    allowed_factor_roles = {
        "confirmed": {"necessary_cause"},
        "rejected": {
            "contributing_condition",
            "amplifying_factor",
            "unrelated",
            "unknown",
        },
        "unknown": {"unknown"},
    }[confirmation.status]
    if confirmation.factor_role not in allowed_factor_roles:
        raise ValueError("confirmation factor_role contradicts its status")
    competitor_comparisons = _validate_competitor_comparisons(
        confirmation.competitor_comparisons,
        request=request,
        confirmation_status=confirmation.status,
        grounded_refs=facts.grounded_refs,
    )
    if confirmation.status == "confirmed":
        if confirmation.counterfactual_status != "supports_causality":
            raise ValueError("confirmed root requires a causality-supporting counterfactual")
        evidence_refs = _validate_evidence_refs(
            confirmation.evidence_refs,
            grounded_refs=facts.grounded_refs,
            field_name="root confirmation evidence_refs",
        )
        excerpt = re.sub(r"\s+", " ", confirmation.excerpt).strip().lower()
        if not excerpt or not any(
            excerpt in fragment for fragment in facts.candidate_semantic_fragments
        ):
            raise ValueError("confirmed root requires a grounded excerpt from candidate facts")
    else:
        evidence_refs = _validate_evidence_refs(
            confirmation.evidence_refs,
            grounded_refs=facts.grounded_refs,
            field_name="root confirmation evidence_refs",
        )
    factor_mechanism = _validate_factor_mechanism(
        confirmation.factor_mechanism,
        role=confirmation.factor_role,
        request=request,
        evidence_refs=evidence_refs,
    )
    return RootConfirmation(
        candidate_ref=confirmation.candidate_ref,
        status=confirmation.status,
        excerpt=confirmation.excerpt,
        reason=confirmation.reason,
        counterfactual=confirmation.counterfactual,
        confidence=confirmation.confidence,
        evidence_refs=evidence_refs,
        counterfactual_status=confirmation.counterfactual_status,
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        recursive_path=request.recursive_path,
        seed_binding_identity=request.seed_binding_identity,
        factor_role=confirmation.factor_role,
        competitor_comparisons=competitor_comparisons,
        factor_mechanism=factor_mechanism,
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
    physical_requests: int = 0


def _repair_constraints(
    *, stage: str, node_ref: str, request_context: JsonDict
) -> JsonDict:
    if stage == "global_candidate_judgment":
        offered = request_context.get("offered_candidate_refs")
        grounded = request_context.get("grounded_refs")
        return {
            "offered_candidate_refs": list(offered) if isinstance(offered, list) else [],
            "grounded_refs": list(grounded) if isinstance(grounded, list) else [],
            "active_focus_binding": {
                "seed_ref": request_context.get("seed_ref"),
                "defect_fingerprint": (
                    request_context.get("active_defect", {}).get("fingerprint")
                    if isinstance(request_context.get("active_defect"), Mapping)
                    else ""
                ),
                "active_focus_text_hash": request_context.get("active_focus_text_hash"),
            },
            "open_authored_root_candidate_refs": list(
                request_context.get("open_authored_root_candidate_refs") or []
            ),
            "comparison_then_selection": True,
            "valid_outcomes": [
                "candidate_roots",
                "no_defect",
                "needs_expansion",
                "inconclusive",
            ],
        }
    if stage != "recursive_causal_step":
        return {"expected_node_ref": node_ref}
    candidates = request_context.get("candidates")
    offered_refs = [
        str(item.get("ref") or "")
        for item in candidates
        if isinstance(item, Mapping) and item.get("ref")
    ] if isinstance(candidates, list) else []
    return {
        "current_node_ref": node_ref,
        "offered_predecessor_refs": offered_refs,
        "current_node_is_not_a_predecessor": (
            "The current_node_ref must never appear in predecessors unless it is "
            "separately listed in offered_predecessor_refs."
        ),
        "valid_present_defect_endings": [
            "recurse through one or two offered grounded predecessors",
            (
                "declare candidate_introduction after assessing every offered predecessor "
                "and request root confirmation"
            ),
            "return concrete blocking missing_evidence",
        ],
    }


class ClaudeCausalJudge(BoundedJudgeCapability, GlobalJudgeCapability):
    def __init__(self, *, transport: ClaudeJudgeClient, cache: JudgmentCache):
        self.transport = transport
        self.cache = cache

    def judge_candidates_bounded(
        self,
        request: GlobalCandidateJudgeRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        prompt = build_global_candidate_prompt(request)
        outcome = self._request_validated(
            stage="global_candidate_judgment",
            schema_version=GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            system=GLOBAL_CANDIDATE_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref="global:{0}:{1}".format(request.case_id, request.seed_ref),
            request_context=request.to_dict(),
            validator=lambda value: validate_global_candidate_payload(
                value, request=request
            ),
            max_tokens=int(getattr(self.transport, "max_tokens", 4096)),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                judgment = global_candidate_judgment_from_payload(
                    outcome.payload, request=request
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation global adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(judgment, outcome.physical_requests)
        detail = "global_judge_{0}: {1}".format(
            outcome.error_kind or "error", outcome.error_detail
        )
        fallback_payload = {
            "outcome": "inconclusive",
            "reason": "The global candidate Judge could not produce a validated result: {0}".format(
                detail
            ),
            "assessments": [
                {
                    "candidate_ref": capsule.candidate_ref,
                    "defect_status": "unknown",
                    "input_defect_status": "unknown",
                    "output_defect_status": "unknown",
                    "causal_path_refs": [],
                    "counterfactual": {
                        "intervention_ref": capsule.candidate_ref,
                        "intervention_kind": "replace_with_semantically_correct_behavior",
                        "predicted_defect_status": "present",
                        "causal_effect": "does_not_prevent_defect",
                    },
                    "compared_candidate_refs": list(
                        request.open_authored_root_candidate_refs
                    ),
                    "causal_role": "unknown",
                    "reason": "Global candidate judgment is unavailable.",
                    "evidence_refs": [],
                    "confidence": 0.0,
                }
                for capsule in request.capsules
            ],
            "selected_candidate_refs": [],
            "expansion_requests": [],
            "decisive_evidence_refs": [],
            "missing_evidence": [detail],
            "confidence": 0.0,
            "active_focus_binding": {
                "seed_ref": request.seed_ref,
                "defect_fingerprint": request.active_defect.fingerprint,
                "active_focus_text_hash": request.active_focus_text_hash,
            },
        }
        return BoundedJudgeCallResult(
            validate_global_candidate_payload(fallback_payload, request=request),
            outcome.physical_requests,
        )

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge_step_bounded(request, max_physical_requests=None).value

    def judge_step_bounded(
        self,
        request: CausalStepRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
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
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                value = causal_step_from_payload(outcome.payload, request=request)
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(value, outcome.physical_requests)
        detail = "judge_{0}: {1}".format(outcome.error_kind or "error", outcome.error_detail)
        return BoundedJudgeCallResult(CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="The causal relation judge could not produce a validated result.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=(detail,),
            suggested_investigation={"kind": "judge_retry", "reason": detail},
            confidence=0.0,
        ), outcome.physical_requests)

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        return self.confirm_candidate_bounded(request, max_physical_requests=None).value

    def confirm_candidate_bounded(
        self,
        request: RootConfirmationRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        try:
            _ConfirmationFactTreeValidator(request).validate()
        except (TypeError, ValueError) as exc:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(
                    request.candidate_ref,
                    "Judge request_ineligible: {0}: {1}".format(type(exc).__name__, exc),
                ),
                0,
            )
        prompt = build_recursive_confirmation_prompt(request)
        outcome = self._request_validated(
            stage="recursive_root_confirmation",
            schema_version=ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validator=lambda value: validate_recursive_confirmation(value, request=request),
            max_tokens=min(int(getattr(self.transport, "max_tokens", 4096)), 2048),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                value = root_confirmation_from_payload(
                    outcome.payload, request=request
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(value, outcome.physical_requests)
        return BoundedJudgeCallResult(
            RootConfirmation.unknown(
                request.candidate_ref,
                "Judge {0}: {1}".format(outcome.error_kind or "error", outcome.error_detail),
            ),
            outcome.physical_requests,
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
        max_physical_requests: Optional[int] = None,
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
            return _RequestOutcome(cached, physical_requests=0)
        remaining_requests = (
            None
            if max_physical_requests is None
            else max(0, int(max_physical_requests))
        )
        if remaining_requests == 0:
            return _RequestOutcome(
                None,
                "request_budget_exhausted",
                "judge_request_budget_exhausted before initial request",
                0,
            )
        if remaining_requests is not None:
            remaining_requests -= 1
        messages = [{"role": "user", "content": prompt}]
        physical_requests = 0
        try:
            transport_result = self._call_transport(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
            )
            physical_requests += transport_result.physical_requests
            text = transport_result.text
        except TransportCallError as exc:
            physical_requests += exc.physical_requests
            error = exc.error
            return _RequestOutcome(
                None,
                (
                    "provider_error"
                    if isinstance(error, (JudgeProviderError, JudgeProviderUnavailable))
                    else "adapter_error"
                ),
                "{0}: {1}".format(type(error).__name__, error),
                physical_requests,
            )
        try:
            payload = _parse_single_json_object(text)
            validator(payload)
        except Exception as first_error:
            exact_error = "{0}: {1}".format(type(first_error).__name__, first_error)
            if remaining_requests == 0:
                return _RequestOutcome(
                    None,
                    "request_budget_exhausted",
                    "{0}; judge_request_budget_exhausted before focused repair".format(
                        exact_error
                    ),
                    physical_requests,
                )
            if remaining_requests is not None:
                remaining_requests -= 1
            try:
                repair_result = self._call_transport(
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
                                    "repair_constraints": _repair_constraints(
                                        stage=stage,
                                        node_ref=node_ref,
                                        request_context=request_context,
                                    ),
                                }
                            ),
                        }
                    ],
                    max_tokens=max_tokens,
                )
                physical_requests += repair_result.physical_requests
                repaired = repair_result.text
            except TransportCallError as exc:
                physical_requests += exc.physical_requests
                error = exc.error
                return _RequestOutcome(
                    None,
                    (
                        "provider_error"
                        if isinstance(
                            error, (JudgeProviderError, JudgeProviderUnavailable)
                        )
                        else "adapter_error"
                    ),
                    "{0}; repair {1}: {2}: {3}".format(
                        exact_error,
                        "provider error"
                        if isinstance(
                            error, (JudgeProviderError, JudgeProviderUnavailable)
                        )
                        else "adapter error",
                        type(error).__name__,
                        error,
                    ),
                    physical_requests,
                )
            try:
                payload = _parse_single_json_object(repaired)
                validator(payload)
            except Exception as repair_error:
                repair_error_detail = "{0}: {1}".format(
                    type(repair_error).__name__, repair_error
                )
                if remaining_requests == 0:
                    return _RequestOutcome(
                        None,
                        "request_budget_exhausted",
                        (
                            "{0}; focused repair invalid: {1}; "
                            "judge_request_budget_exhausted before full retry"
                        ).format(exact_error, repair_error_detail),
                        physical_requests,
                    )
                if remaining_requests is not None:
                    remaining_requests -= 1
                try:
                    retry_result = self._call_transport(
                        system=system,
                        messages=[
                            {
                                "role": "user",
                                "content": stable_json(
                                    {
                                        "retry_instruction": "Return a complete replacement JSON object.",
                                        "mandatory_output_contract": {
                                            "all_required_fields_must_be_present": True,
                                            "confidence": "required unquoted JSON number between 0 and 1",
                                            "response_shape": "one complete JSON object, not a patch",
                                        },
                                        "validation_errors": [
                                            exact_error,
                                            repair_error_detail,
                                        ],
                                        "repair_constraints": _repair_constraints(
                                            stage=stage,
                                            node_ref=node_ref,
                                            request_context=request_context,
                                        ),
                                        "invalid_outputs": [
                                            text[:16000],
                                            repaired[:16000],
                                        ],
                                        "schema_version": schema_version,
                                        "original_prompt": prompt,
                                        "canonical_request_context": request_context,
                                    }
                                ),
                            }
                        ],
                        max_tokens=max_tokens,
                    )
                    physical_requests += retry_result.physical_requests
                    retried = retry_result.text
                except TransportCallError as exc:
                    physical_requests += exc.physical_requests
                    error = exc.error
                    return _RequestOutcome(
                        None,
                        (
                            "provider_error"
                            if isinstance(
                                error,
                                (JudgeProviderError, JudgeProviderUnavailable),
                            )
                            else "adapter_error"
                        ),
                        "{0}; focused repair invalid: {1}; full retry {2}: {3}: {4}".format(
                            exact_error,
                            repair_error_detail,
                            "provider error"
                            if isinstance(
                                error,
                                (JudgeProviderError, JudgeProviderUnavailable),
                            )
                            else "adapter error",
                            type(error).__name__,
                            error,
                        ),
                        physical_requests,
                    )
                try:
                    payload = _parse_single_json_object(retried)
                    validator(payload)
                except Exception as retry_error:
                    return _RequestOutcome(
                        None,
                        "validation_error",
                        (
                            "{0}; focused repair invalid: {1}; "
                            "full retry invalid: {2}: {3}"
                        ).format(
                            exact_error,
                            repair_error_detail,
                            type(retry_error).__name__,
                            retry_error,
                        ),
                        physical_requests,
                    )
        try:
            self.cache.put_payload(
                key=cache_key,
                stage=stage,
                model=str(getattr(self.transport, "model", "")),
                node_ref=node_ref,
                payload=payload,
            )
        except Exception as exc:
            raise BoundedJudgeCallError(
                "cache adapter failed after validated transport: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
                physical_requests=physical_requests,
            ) from exc
        return _RequestOutcome(payload, physical_requests=physical_requests)

    def _call_transport(
        self, *, system: str, messages: List[JsonDict], max_tokens: int
    ) -> TransportCallResult:
        call = getattr(self.transport, "create_message_text_with_usage", None)
        if not callable(call):
            raise TransportCallError(
                TypeError(
                    "transport must implement create_message_text_with_usage"
                ),
                physical_requests=0,
            )
        try:
            result = call(system=system, messages=messages, max_tokens=max_tokens)
        except TransportCallError:
            raise
        except Exception as exc:
            raise TransportCallError(exc, physical_requests=0) from exc
        if not isinstance(result, TransportCallResult):
            raise TransportCallError(
                TypeError("transport must return TransportCallResult"),
                physical_requests=0,
            )
        return result


def _parse_single_json_object(text: str) -> JsonDict:
    value = json.loads(text.strip())
    if not isinstance(value, dict):
        raise TypeError("judge response must be one JSON object")
    return value


__all__ = [
    "BoundedJudgeCallResult",
    "BoundedJudgeCallError",
    "BoundedJudgeCapability",
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
