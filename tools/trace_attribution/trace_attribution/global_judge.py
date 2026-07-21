"""Global comparative judgment over bounded candidate evidence closures."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .causal_state import FrozenMapping
from .evidence_capsule import CandidateEvidenceCapsule
from .models import JsonDict, stable_json


GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION = "global-candidate-judgment/v1"
GLOBAL_OUTCOMES = frozenset(
    {"candidate_roots", "no_defect", "needs_expansion", "inconclusive"}
)
DEFECT_STATUSES = frozenset({"present", "absent", "unknown"})
CAUSAL_ROLES = frozenset(
    {
        "root_candidate",
        "contributing_condition",
        "amplifying_factor",
        "outcome_evidence",
        "exculpatory_evidence",
        "unrelated",
        "unknown",
    }
)
EXPANSION_CONTEXT_KINDS = frozenset(
    {"upstream", "downstream", "artifact", "action_group", "full_node"}
)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("{0} must be an array".format(field_name))
    output: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("{0} must contain non-empty strings".format(field_name))
        text = item.strip()
        if text in seen:
            raise ValueError("{0} must not contain duplicates".format(field_name))
        seen.add(text)
        output.append(text)
    return tuple(output)


def _confidence(value: Any, field_name: str = "confidence") -> float:
    if isinstance(value, bool):
        raise ValueError("{0} must be a number between 0 and 1".format(field_name))
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("{0} must be a number between 0 and 1".format(field_name))
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("{0} must be a number between 0 and 1".format(field_name))
    return result


@dataclass(frozen=True)
class GlobalCandidateJudgeRequest:
    case_id: str
    objective: str
    analysis_perspective: str
    start_refs: Tuple[str, ...]
    capsules: Tuple[CandidateEvidenceCapsule, ...]
    trace_health: Mapping[str, Any] = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_refs", tuple(str(item) for item in self.start_refs))
        object.__setattr__(self, "capsules", tuple(self.capsules))
        object.__setattr__(self, "trace_health", _freeze(self.trace_health))
        refs = [item.candidate_ref for item in self.capsules]
        if len(refs) != len(set(refs)):
            raise ValueError("global candidate request contains duplicate candidate refs")

    @property
    def offered_candidate_refs(self) -> Tuple[str, ...]:
        return tuple(item.candidate_ref for item in self.capsules)

    @property
    def grounded_refs(self) -> Tuple[str, ...]:
        refs = list(self.start_refs)
        for capsule in self.capsules:
            value = capsule.to_dict()
            refs.append(capsule.candidate_ref)
            refs.extend(value.get("downstream_path") or [])
            for reference in value.get("downstream_path_references") or []:
                if isinstance(reference, Mapping):
                    refs.extend(
                        str(reference.get(key) or "")
                        for key in ("raw_ref", "resolved_ref")
                    )
            for reference in value.get("evidence_references") or []:
                if not isinstance(reference, Mapping) or reference.get("resolution_status") != "resolved":
                    continue
                refs.extend(
                    str(reference.get(key) or "")
                    for key in ("raw_ref", "resolved_ref", "canonical_ref")
                )
            action_group = value.get("action_group")
            if isinstance(action_group, Mapping):
                for member in action_group.get("members") or []:
                    if isinstance(member, Mapping):
                        refs.append(str(member.get("ref") or ""))
            for edge_key in ("incoming_edges", "outgoing_edges"):
                for edge in value.get(edge_key) or []:
                    if not isinstance(edge, Mapping):
                        continue
                    refs.extend(
                        str(edge.get(key) or "") for key in ("from_ref", "to_ref")
                    )
                    refs.extend(str(item) for item in edge.get("evidence_refs") or [])
        output = []
        seen = set()
        for ref in refs:
            item = str(ref or "")
            if item and item not in seen:
                seen.add(item)
                output.append(item)
        return tuple(output)

    def to_dict(self) -> JsonDict:
        defect_state = (
            self.capsules[0].defect_state.to_dict() if self.capsules else {}
        )
        return {
            "case_id": self.case_id,
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "start_refs": list(self.start_refs),
            "active_focus": {
                "start_refs": list(self.start_refs),
                "defect_state": defect_state,
            },
            "trace_health": _thaw(self.trace_health),
            "offered_candidate_refs": list(self.offered_candidate_refs),
            "grounded_refs": list(self.grounded_refs),
            "candidate_evidence_capsules": [item.to_dict() for item in self.capsules],
        }


@dataclass(frozen=True)
class GlobalCandidateAssessment:
    candidate_ref: str
    defect_status: str
    causal_role: str
    reason: str
    evidence_refs: Tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if self.defect_status not in DEFECT_STATUSES:
            raise ValueError("unsupported global candidate defect_status")
        if self.causal_role not in CAUSAL_ROLES:
            raise ValueError("unsupported global candidate causal_role")
        if not self.reason.strip():
            raise ValueError("global candidate assessment reason must be non-empty")
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "confidence", _confidence(self.confidence))

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "defect_status": self.defect_status,
            "causal_role": self.causal_role,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class GlobalCandidateJudgment:
    outcome: str
    reason: str
    assessments: Tuple[GlobalCandidateAssessment, ...]
    selected_candidate_refs: Tuple[str, ...]
    expansion_requests: Tuple[Mapping[str, Any], ...]
    decisive_evidence_refs: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if self.outcome not in GLOBAL_OUTCOMES:
            raise ValueError("unsupported global candidate outcome")
        if not self.reason.strip():
            raise ValueError("global candidate judgment reason must be non-empty")
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "selected_candidate_refs", tuple(self.selected_candidate_refs))
        object.__setattr__(
            self, "expansion_requests", tuple(_freeze(item) for item in self.expansion_requests)
        )
        object.__setattr__(self, "decisive_evidence_refs", tuple(self.decisive_evidence_refs))
        object.__setattr__(self, "missing_evidence", tuple(self.missing_evidence))
        object.__setattr__(self, "confidence", _confidence(self.confidence))

    def to_dict(self) -> JsonDict:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "assessments": [item.to_dict() for item in self.assessments],
            "selected_candidate_refs": list(self.selected_candidate_refs),
            "expansion_requests": _thaw(self.expansion_requests),
            "decisive_evidence_refs": list(self.decisive_evidence_refs),
            "missing_evidence": list(self.missing_evidence),
            "confidence": self.confidence,
        }


class GlobalJudgeCapability:
    def judge_candidates_bounded(
        self,
        request: GlobalCandidateJudgeRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> Any:
        raise NotImplementedError


def build_global_candidate_prompt(request: GlobalCandidateJudgeRequest) -> str:
    return stable_json(
        {
            "request": request.to_dict(),
            "rules": [
                "Use only grounded facts in the supplied candidate evidence capsules.",
                "The retrieval rank is navigation evidence, not a causal verdict.",
                "Compare every offered candidate and return exactly one assessment per candidate.",
                "Judge only request.active_focus. Do not substitute another claim or defect from a shared response, neighboring capsule, or broader objective.",
                "For each assessment, defect_status answers whether the active defect is true at that candidate; it does not answer whether the record itself exists or contains defect-related words.",
                "When decisive counterevidence refutes a derived defect observation, mark that observation absent for the active defect even though its trace record exists.",
                "Choose candidate_roots only for defective authored nodes that may introduce the active defect.",
                "Never select a candidate with root_candidate_eligible=false as a root; treat tool results, verification, and evidence facts as evidence instead.",
                "Choose no_defect only when decisive grounded evidence contradicts the observed defect.",
                "Choose needs_expansion only when a specific grounded anchor needs more upstream, downstream, artifact, action_group, or full_node context and the missing content would change the current judgment.",
                "An unavailable or truncated artifact is not automatically blocking when recorded structured facts or previews already decide the objective; explain any actual semantic gap instead of expanding by default.",
                "Outcome evidence and successful verification can expose or contradict a defect but are not root causes merely because they are adjacent.",
                "Do not force a root; inconclusive and no_defect are valid outcomes.",
            ],
            "required_json_schema": {
                "outcome": "candidate_roots|no_defect|needs_expansion|inconclusive",
                "reason": "non-empty string",
                "assessments": [
                    {
                        "candidate_ref": "one exact offered candidate ref",
                        "defect_status": "present|absent|unknown",
                        "causal_role": "root_candidate|contributing_condition|amplifying_factor|outcome_evidence|exculpatory_evidence|unrelated|unknown",
                        "reason": "non-empty grounded comparative reason",
                        "evidence_refs": ["grounded refs"],
                        "confidence": "number 0..1",
                    }
                ],
                "selected_candidate_refs": ["exact offered refs"],
                "expansion_requests": [
                    {
                        "anchor_ref": "grounded ref",
                        "context_kind": "upstream|downstream|artifact|action_group|full_node",
                        "reason": "non-empty evidence-gap reason",
                    }
                ],
                "decisive_evidence_refs": ["grounded refs"],
                "missing_evidence": ["non-empty descriptions"],
                "confidence": "number 0..1",
            },
        }
    )


def validate_global_candidate_payload(
    value: Any, *, request: GlobalCandidateJudgeRequest
) -> GlobalCandidateJudgment:
    if not isinstance(value, Mapping):
        raise TypeError("global candidate judgment must be an object")
    required = {
        "outcome",
        "reason",
        "assessments",
        "selected_candidate_refs",
        "expansion_requests",
        "decisive_evidence_refs",
        "missing_evidence",
        "confidence",
    }
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError(
            "global candidate judgment schema mismatch (missing={0}, extra={1})".format(
                sorted(required - actual), sorted(actual - required)
            )
        )
    outcome = str(value.get("outcome") or "")
    if outcome not in GLOBAL_OUTCOMES:
        raise ValueError("unsupported global candidate outcome")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("global candidate judgment reason must be non-empty")
    raw_assessments = value.get("assessments")
    if not isinstance(raw_assessments, list):
        raise TypeError("global candidate assessments must be an array")
    assessments = tuple(_assessment_from_payload(item) for item in raw_assessments)
    offered = set(request.offered_candidate_refs)
    assessed = [item.candidate_ref for item in assessments]
    if len(assessed) != len(set(assessed)) or set(assessed) != offered:
        raise ValueError("global judgment must assess every offered candidate exactly once")
    selected = _strings(value.get("selected_candidate_refs"), "selected_candidate_refs")
    if any(ref not in offered for ref in selected):
        raise ValueError("selected root must be an offered candidate")
    grounded = set(request.grounded_refs)
    decisive = _strings(value.get("decisive_evidence_refs"), "decisive_evidence_refs")
    if any(ref not in grounded for ref in decisive):
        raise ValueError("decisive evidence must use grounded refs")
    for item in assessments:
        if any(ref not in grounded for ref in item.evidence_refs):
            raise ValueError("assessment evidence must use grounded refs")
    missing = _strings(value.get("missing_evidence"), "missing_evidence")
    expansion = _expansion_requests(value.get("expansion_requests"), grounded)
    by_ref = {item.candidate_ref: item for item in assessments}
    if outcome == "candidate_roots":
        if not selected:
            raise ValueError("candidate_roots requires selected candidates")
        if not decisive:
            raise ValueError("candidate_roots requires decisive evidence")
        for ref in selected:
            assessment = by_ref[ref]
            if assessment.defect_status != "present" or assessment.causal_role != "root_candidate":
                raise ValueError("selected root requires a present root_candidate assessment")
            capsule = next(item for item in request.capsules if item.candidate_ref == ref)
            if not bool(capsule.candidate.get("root_candidate_eligible")):
                raise ValueError("selected root candidate is ineligible for attribution")
        if expansion:
            raise ValueError("candidate_roots cannot simultaneously request expansion")
    elif outcome == "no_defect":
        if selected or expansion:
            raise ValueError("no_defect cannot select roots or request expansion")
        if not decisive:
            raise ValueError("no_defect requires decisive evidence")
        if missing:
            raise ValueError("no_defect cannot retain missing evidence")
        causal_roles = {
            "root_candidate",
            "contributing_condition",
            "amplifying_factor",
        }
        if any(
            item.causal_role in causal_roles and item.defect_status != "absent"
            for item in assessments
        ):
            raise ValueError("no_defect cannot retain a present causal candidate")
        if any(
            item.defect_status == "present"
            and item.causal_role != "outcome_evidence"
            for item in assessments
        ):
            raise ValueError(
                "no_defect permits present status only for a refuted outcome observation"
            )
        decisive_set = set(decisive)
        if not any(
            item.defect_status == "absent"
            and item.causal_role in {"exculpatory_evidence", "outcome_evidence"}
            and item.candidate_ref in decisive_set
            for item in assessments
        ):
            raise ValueError("no_defect requires decisive absent evidence")
    elif outcome == "needs_expansion":
        if selected:
            raise ValueError("needs_expansion cannot select roots")
        if not expansion or not missing:
            raise ValueError("needs_expansion requires requests and missing evidence")
    elif selected or expansion:
        raise ValueError("inconclusive cannot select roots or request expansion")
    return GlobalCandidateJudgment(
        outcome=outcome,
        reason=reason,
        assessments=assessments,
        selected_candidate_refs=selected,
        expansion_requests=expansion,
        decisive_evidence_refs=decisive,
        missing_evidence=missing,
        confidence=_confidence(value.get("confidence")),
    )


def global_candidate_judgment_from_payload(
    value: Any, *, request: GlobalCandidateJudgeRequest
) -> GlobalCandidateJudgment:
    return validate_global_candidate_payload(value, request=request)


def _assessment_from_payload(value: Any) -> GlobalCandidateAssessment:
    if not isinstance(value, Mapping):
        raise TypeError("global candidate assessment must be an object")
    required = {
        "candidate_ref",
        "defect_status",
        "causal_role",
        "reason",
        "evidence_refs",
        "confidence",
    }
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError("global candidate assessment schema mismatch")
    return GlobalCandidateAssessment(
        candidate_ref=str(value.get("candidate_ref") or ""),
        defect_status=str(value.get("defect_status") or ""),
        causal_role=str(value.get("causal_role") or ""),
        reason=str(value.get("reason") or ""),
        evidence_refs=_strings(value.get("evidence_refs"), "assessment evidence_refs"),
        confidence=_confidence(value.get("confidence")),
    )


def _expansion_requests(value: Any, grounded: set[str]) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise TypeError("expansion_requests must be an array")
    output = []
    seen = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"anchor_ref", "context_kind", "reason"}:
            raise ValueError("expansion request schema mismatch")
        anchor = str(item.get("anchor_ref") or "")
        kind = str(item.get("context_kind") or "")
        reason = str(item.get("reason") or "").strip()
        if anchor not in grounded:
            raise ValueError("expansion request requires a grounded anchor")
        if kind not in EXPANSION_CONTEXT_KINDS:
            raise ValueError("unsupported expansion context kind")
        if not reason:
            raise ValueError("expansion request reason must be non-empty")
        identity = (anchor, kind)
        if identity in seen:
            raise ValueError("duplicate expansion request")
        seen.add(identity)
        output.append(FrozenMapping({"anchor_ref": anchor, "context_kind": kind, "reason": reason}))
    return tuple(output)


__all__ = [
    "GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION",
    "GlobalCandidateAssessment",
    "GlobalCandidateJudgeRequest",
    "GlobalCandidateJudgment",
    "GlobalJudgeCapability",
    "build_global_candidate_prompt",
    "global_candidate_judgment_from_payload",
    "validate_global_candidate_payload",
]
