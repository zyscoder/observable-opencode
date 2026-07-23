"""Global comparative judgment over bounded candidate evidence closures."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .causal_state import (
    GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
    GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
    CausalCandidate,
    DefectState,
    FrozenMapping,
)
from .confirmation_path import is_confirmation_causal_edge
from .evidence_capsule import (
    CandidateEvidenceCapsule,
    validate_candidate_evidence_capsules_against_graph,
)
from .graph import TraceGraph
from .models import JsonDict, stable_json


GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION = GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION
MAX_ROOT_CONFIRMATION_CANDIDATES = 3
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


def normalize_active_focus_text(value: str) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def active_focus_text_sha256(value: str) -> str:
    normalized = normalize_active_focus_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GlobalCandidateJudgeRequest:
    case_id: str
    objective: str
    analysis_perspective: str
    seed_ref: str
    active_defect: DefectState
    active_focus_text: str
    active_focus_text_hash: str
    start_refs: Tuple[str, ...]
    capsules: Tuple[CandidateEvidenceCapsule, ...]
    trace_health: Mapping[str, Any] = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        seed_ref = str(self.seed_ref).strip()
        focus_text = str(self.active_focus_text)
        if not seed_ref:
            raise ValueError("global candidate request requires seed_ref")
        if not isinstance(self.active_defect, DefectState):
            raise TypeError("global candidate request active_defect must be DefectState")
        if not focus_text.strip():
            raise ValueError("global candidate request requires active_focus_text")
        object.__setattr__(self, "seed_ref", seed_ref)
        object.__setattr__(self, "active_focus_text", focus_text)
        object.__setattr__(self, "start_refs", tuple(str(item) for item in self.start_refs))
        object.__setattr__(self, "capsules", tuple(self.capsules))
        object.__setattr__(self, "trace_health", _freeze(self.trace_health))
        self.validate()

    def validate(self) -> None:
        if not str(self.seed_ref).strip():
            raise ValueError("global candidate request requires seed_ref")
        if not isinstance(self.active_defect, DefectState):
            raise TypeError("global candidate request active_defect must be DefectState")
        if not str(self.active_focus_text).strip():
            raise ValueError("global candidate request requires active_focus_text")
        expected_hash = active_focus_text_sha256(self.active_focus_text)
        if self.active_focus_text_hash != expected_hash:
            raise ValueError("global candidate request active_focus_text_hash mismatch")
        if normalize_active_focus_text(self.active_focus_text) != normalize_active_focus_text(
            self.active_defect.actual
        ):
            raise ValueError("global candidate request active_focus_text must match active_defect.actual")
        refs = [item.candidate_ref for item in self.capsules]
        if len(refs) != len(set(refs)):
            raise ValueError("global candidate request contains duplicate candidate refs")
        if self.start_refs != (self.seed_ref,):
            raise ValueError("global candidate request must bind exactly one active seed_ref")
        for capsule in self.capsules:
            capsule.validate()
            if capsule.defect_state != self.active_defect:
                raise ValueError("global candidate request capsule defect drifts from active_defect")
            if tuple(capsule.start_refs) != (self.seed_ref,):
                raise ValueError("global candidate request capsule drifts from active seed_ref")

    @property
    def offered_candidate_refs(self) -> Tuple[str, ...]:
        return tuple(item.candidate_ref for item in self.capsules)

    @property
    def open_authored_root_candidate_refs(self) -> Tuple[str, ...]:
        return tuple(
            sorted(
                item.candidate_ref
                for item in self.capsules
                if item.candidate.get("root_candidate_eligible") is True
            )
        )

    @property
    def grounded_refs(self) -> Tuple[str, ...]:
        refs = list(self.start_refs)
        for capsule in self.capsules:
            value = capsule.to_dict()
            refs.append(capsule.candidate_ref)
            refs.extend(value.get("downstream_path") or [])
            for reference in value.get("downstream_path_references") or []:
                if (
                    isinstance(reference, Mapping)
                    and reference.get("resolution_status") == "resolved"
                ):
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
            resolved_evidence_refs = {
                str(reference.get(key) or "")
                for reference in value.get("evidence_references") or []
                if isinstance(reference, Mapping)
                and reference.get("resolution_status") == "resolved"
                for key in ("raw_ref", "resolved_ref", "canonical_ref")
            }
            action_group = value.get("action_group")
            if isinstance(action_group, Mapping):
                for member in action_group.get("members") or []:
                    if isinstance(member, Mapping):
                        refs.append(str(member.get("ref") or ""))
            for edge_key in (
                "causal_path_edges",
                "incoming_edges",
                "outgoing_edges",
            ):
                for edge in value.get(edge_key) or []:
                    if not isinstance(edge, Mapping):
                        continue
                    refs.extend(
                        str(edge.get(key) or "") for key in ("from_ref", "to_ref")
                    )
                    refs.extend(
                        str(item)
                        for item in edge.get("evidence_refs") or []
                        if str(item) in resolved_evidence_refs
                    )
        output = []
        seen = set()
        for ref in refs:
            item = str(ref or "")
            if item and item not in seen:
                seen.add(item)
                output.append(item)
        return tuple(output)

    def to_dict(self) -> JsonDict:
        self.validate()
        return {
            "case_id": self.case_id,
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "seed_ref": self.seed_ref,
            "active_defect": self.active_defect.to_dict(),
            "active_focus_text": self.active_focus_text,
            "active_focus_text_hash": self.active_focus_text_hash,
            "start_refs": list(self.start_refs),
            "active_focus": {
                "seed_ref": self.seed_ref,
                "defect_fingerprint": self.active_defect.fingerprint,
                "active_focus_text": self.active_focus_text,
                "active_focus_text_hash": self.active_focus_text_hash,
            },
            "trace_health": _thaw(self.trace_health),
            "offered_candidate_refs": list(self.offered_candidate_refs),
            "open_authored_root_candidate_refs": list(
                self.open_authored_root_candidate_refs
            ),
            "retrieval_is_not_causal_verdict": True,
            "grounded_refs": list(self.grounded_refs),
            "candidate_evidence_capsules": [item.to_dict() for item in self.capsules],
        }

    def validation_envelope(self) -> JsonDict:
        self.validate()
        return {
            "schema_version": GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
            "case_id": self.case_id,
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "seed_ref": self.seed_ref,
            "active_defect": self.active_defect.to_dict(),
            "active_focus_text": self.active_focus_text,
            "active_focus_text_hash": self.active_focus_text_hash,
            "start_refs": list(self.start_refs),
            "trace_health": _thaw(self.trace_health),
            "candidate_evidence_capsules": [
                item.to_dict() for item in self.capsules
            ],
        }


def global_candidate_request_from_validation_envelope(
    value: Any,
    *,
    graph: Optional[TraceGraph] = None,
    authoritative_candidates: Sequence[CausalCandidate] = (),
) -> GlobalCandidateJudgeRequest:
    if not isinstance(value, Mapping):
        raise TypeError("global candidate validation envelope must be an object")
    required = {
        "schema_version",
        "case_id",
        "objective",
        "analysis_perspective",
        "seed_ref",
        "active_defect",
        "active_focus_text",
        "active_focus_text_hash",
        "start_refs",
        "trace_health",
        "candidate_evidence_capsules",
    }
    if (
        set(value) != required
        or value.get("schema_version")
        != GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION
    ):
        raise ValueError("global candidate validation envelope schema mismatch")
    active_defect = value.get("active_defect")
    trace_health = value.get("trace_health")
    start_refs = value.get("start_refs")
    capsules = value.get("candidate_evidence_capsules")
    if not isinstance(active_defect, Mapping):
        raise TypeError("validation envelope active_defect must be an object")
    if not isinstance(trace_health, Mapping):
        raise TypeError("validation envelope trace_health must be an object")
    if not isinstance(start_refs, list) or any(
        not isinstance(item, str) for item in start_refs
    ):
        raise TypeError("validation envelope start_refs must be a string array")
    if not isinstance(capsules, list):
        raise TypeError("validation envelope capsules must be an array")
    request = GlobalCandidateJudgeRequest(
        case_id=str(value.get("case_id") or ""),
        objective=str(value.get("objective") or ""),
        analysis_perspective=str(value.get("analysis_perspective") or ""),
        seed_ref=str(value.get("seed_ref") or ""),
        active_defect=DefectState.from_dict(dict(active_defect)),
        active_focus_text=str(value.get("active_focus_text") or ""),
        active_focus_text_hash=str(value.get("active_focus_text_hash") or ""),
        start_refs=tuple(start_refs),
        capsules=tuple(
            CandidateEvidenceCapsule.from_dict(item) for item in capsules
        ),
        trace_health=trace_health,
    )
    if graph is not None:
        validate_candidate_evidence_capsules_against_graph(
            graph,
            request.capsules,
            authoritative_candidates=authoritative_candidates,
        )
    return request


@dataclass(frozen=True)
class GlobalCandidateAssessment:
    candidate_ref: str
    defect_status: str
    input_defect_status: str
    output_defect_status: str
    causal_path_refs: Tuple[str, ...]
    counterfactual: Mapping[str, Any]
    compared_candidate_refs: Tuple[str, ...]
    causal_role: str
    reason: str
    evidence_refs: Tuple[str, ...]
    confidence: float

    def __post_init__(self) -> None:
        if self.defect_status not in DEFECT_STATUSES:
            raise ValueError("unsupported global candidate defect_status")
        if self.input_defect_status not in DEFECT_STATUSES:
            raise ValueError("unsupported global candidate input_defect_status")
        if self.output_defect_status not in DEFECT_STATUSES:
            raise ValueError("unsupported global candidate output_defect_status")
        if self.defect_status != self.output_defect_status:
            raise ValueError("defect_status must match output_defect_status")
        if self.causal_role not in CAUSAL_ROLES:
            raise ValueError("unsupported global candidate causal_role")
        if not self.reason.strip():
            raise ValueError("global candidate assessment reason must be non-empty")
        causal_path_refs = _immutable_strings(
            self.causal_path_refs, "causal_path_refs"
        )
        compared_candidate_refs = tuple(
            sorted(
                _immutable_strings(
                    self.compared_candidate_refs, "compared_candidate_refs"
                )
            )
        )
        object.__setattr__(self, "causal_path_refs", causal_path_refs)
        object.__setattr__(
            self,
            "counterfactual",
            _counterfactual(self.counterfactual, candidate_ref=self.candidate_ref),
        )
        object.__setattr__(
            self, "compared_candidate_refs", compared_candidate_refs
        )
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        _validate_assessment_counterfactual_consistency(self)

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "defect_status": self.defect_status,
            "input_defect_status": self.input_defect_status,
            "output_defect_status": self.output_defect_status,
            "causal_path_refs": list(self.causal_path_refs),
            "counterfactual": _thaw(self.counterfactual),
            "compared_candidate_refs": list(self.compared_candidate_refs),
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
    active_focus_binding: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.outcome not in GLOBAL_OUTCOMES:
            raise ValueError("unsupported global candidate outcome")
        if not self.reason.strip():
            raise ValueError("global candidate judgment reason must be non-empty")
        object.__setattr__(self, "assessments", tuple(self.assessments))
        selected_candidate_refs = tuple(
            sorted(
                _immutable_strings(
                    self.selected_candidate_refs, "selected_candidate_refs"
                )
            )
        )
        if len(selected_candidate_refs) > MAX_ROOT_CONFIRMATION_CANDIDATES:
            raise ValueError("global judgment may select at most three candidates")
        object.__setattr__(
            self, "selected_candidate_refs", selected_candidate_refs
        )
        object.__setattr__(
            self, "expansion_requests", tuple(_freeze(item) for item in self.expansion_requests)
        )
        object.__setattr__(self, "decisive_evidence_refs", tuple(self.decisive_evidence_refs))
        object.__setattr__(self, "missing_evidence", tuple(self.missing_evidence))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(
            self, "active_focus_binding", _active_focus_binding(self.active_focus_binding)
        )

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
            "active_focus_binding": _thaw(self.active_focus_binding),
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
    request.validate()
    return stable_json(
        {
            "request": request.to_dict(),
            "comparison_then_selection": [
                "compare_input_and_output_defect_status",
                "ground_causal_paths",
                "evaluate_counterfactual_predictions",
                "compare_all_open_authored_root_candidates_including_self",
                "select_candidate_roots_last",
            ],
            "rules": [
                "Use only grounded facts in the supplied candidate evidence capsules.",
                "retrieval_is_not_causal_verdict=true. Retrieval rank and score are navigation evidence only and MUST NOT enter root selection rules.",
                "First compare every offered candidate and return exactly one complete assessment per candidate; only after the comparison matrix is complete may selected_candidate_refs be chosen.",
                "Judge only request.active_focus. Do not substitute another claim or defect from a shared response, neighboring capsule, or broader objective.",
                "For each assessment, judge input_defect_status before the candidate and output_defect_status after it to determine whether the active defect is true; defect_status must equal output_defect_status and does not answer whether the record itself exists or contains defect-related words.",
                "A root_candidate may use input_defect_status=unknown because unknown is not proof that the defect was present, but its reason and confidence must preserve that uncertainty; confidence cannot be 1.0.",
                "causal_path_refs must be the supplied candidate-to-seed path when claiming a causal role, and counterfactual must make a decidable present-or-absent output prediction.",
                "compared_candidate_refs must list every open authored root-eligible candidate, including the assessed candidate itself when eligible; retrieval order never changes this set.",
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
                "active_focus_binding": {
                    "seed_ref": "exact request.seed_ref",
                    "defect_fingerprint": "exact request.active_defect.fingerprint",
                    "active_focus_text_hash": "exact request.active_focus_text_hash",
                },
                "assessments": [
                    {
                        "candidate_ref": "one exact offered candidate ref",
                        "defect_status": "present|absent|unknown",
                        "input_defect_status": "present|absent|unknown",
                        "output_defect_status": "present|absent|unknown",
                        "causal_path_refs": ["grounded candidate-to-seed refs, or empty for non-causal evidence"],
                        "counterfactual": {
                            "intervention_ref": "exact candidate_ref",
                            "intervention_kind": "replace_with_semantically_correct_behavior",
                            "predicted_defect_status": "present|absent",
                            "causal_effect": "prevents_defect|does_not_prevent_defect",
                        },
                        "compared_candidate_refs": ["all open authored root-eligible refs, including self when eligible"],
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
    request.validate()
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
        "active_focus_binding",
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
    binding = _active_focus_binding(value.get("active_focus_binding"))
    raw_assessments = value.get("assessments")
    if not isinstance(raw_assessments, list):
        raise TypeError("global candidate assessments must be an array")
    assessments = tuple(_assessment_from_payload(item) for item in raw_assessments)
    offered = set(request.offered_candidate_refs)
    assessed = [item.candidate_ref for item in assessments]
    if len(assessed) != len(set(assessed)) or set(assessed) != offered:
        raise ValueError("global judgment must assess every offered candidate exactly once")
    selected = _strings(value.get("selected_candidate_refs"), "selected_candidate_refs")
    if len(selected) > MAX_ROOT_CONFIRMATION_CANDIDATES:
        raise ValueError("global judgment may select at most three candidates")
    selected = tuple(sorted(selected))
    if any(ref not in offered for ref in selected):
        raise ValueError("selected root must be an offered candidate")
    grounded = set(request.grounded_refs)
    decisive = _strings(value.get("decisive_evidence_refs"), "decisive_evidence_refs")
    if any(ref not in grounded for ref in decisive):
        raise ValueError("decisive evidence must use grounded refs")
    for item in assessments:
        if any(ref not in grounded for ref in item.evidence_refs):
            raise ValueError("assessment evidence must use grounded refs")
        if any(ref not in grounded for ref in item.causal_path_refs):
            raise ValueError("causal_path_refs must use grounded refs")
        capsule = next(
            capsule
            for capsule in request.capsules
            if capsule.candidate_ref == item.candidate_ref
        )
        if item.causal_path_refs and item.causal_path_refs != tuple(capsule.downstream_path):
            raise ValueError("causal_path_refs must match the grounded candidate path")
        if item.causal_path_refs and (
            item.causal_path_refs[0] != item.candidate_ref
            or item.causal_path_refs[-1] != request.seed_ref
        ):
            raise ValueError("causal_path_refs must connect candidate to active seed")
        if item.causal_path_refs and not _has_eligible_causal_path_hops(
            capsule, item.causal_path_refs
        ):
            raise ValueError(
                "causal_path_refs must use an eligible correctly directed capsule hop"
            )
        if (
            item.causal_role
            in {"root_candidate", "contributing_condition", "amplifying_factor"}
            and item.output_defect_status == "present"
            and not item.causal_path_refs
        ):
            raise ValueError(
                "present causal candidate requires causal_path_refs to the active seed"
            )
        expected_compared = set(request.open_authored_root_candidate_refs)
        if set(item.compared_candidate_refs) != expected_compared:
            raise ValueError(
                "compared_candidate_refs must cover every open authored root-eligible candidate"
            )
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
            if (
                assessment.input_defect_status == "present"
                or assessment.output_defect_status != "present"
                or assessment.causal_role != "root_candidate"
            ):
                raise ValueError(
                    "selected root requires input defect not present and output defect present"
                )
            if not assessment.causal_path_refs:
                raise ValueError("selected root requires a grounded causal_path_refs")
            if assessment.counterfactual["predicted_defect_status"] != "absent":
                raise ValueError("selected root counterfactual must predict absent output defect")
            capsule = next(item for item in request.capsules if item.candidate_ref == ref)
            if capsule.candidate.get("root_candidate_eligible") is not True:
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
            item.causal_role in causal_roles and item.output_defect_status != "absent"
            for item in assessments
        ):
            raise ValueError("no_defect cannot retain a present causal candidate")
        if any(
            item.output_defect_status == "present"
            and item.causal_role != "outcome_evidence"
            for item in assessments
        ):
            raise ValueError(
                "no_defect permits present status only for a refuted outcome observation"
            )
        decisive_set = set(decisive)
        if not any(
            item.output_defect_status == "absent"
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
    judgment = GlobalCandidateJudgment(
        outcome=outcome,
        reason=reason,
        assessments=assessments,
        selected_candidate_refs=selected,
        expansion_requests=expansion,
        decisive_evidence_refs=decisive,
        missing_evidence=missing,
        confidence=_confidence(value.get("confidence")),
        active_focus_binding=binding,
    )
    validate_active_focus_binding(request, judgment)
    return judgment


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
        "input_defect_status",
        "output_defect_status",
        "causal_path_refs",
        "counterfactual",
        "compared_candidate_refs",
        "causal_role",
        "reason",
        "evidence_refs",
        "confidence",
    }
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError(
            "global candidate assessment schema mismatch (missing={0}, extra={1})".format(
                sorted(required - actual), sorted(actual - required)
            )
        )
    return GlobalCandidateAssessment(
        candidate_ref=str(value.get("candidate_ref") or ""),
        defect_status=str(value.get("defect_status") or ""),
        input_defect_status=str(value.get("input_defect_status") or ""),
        output_defect_status=str(value.get("output_defect_status") or ""),
        causal_path_refs=_strings(value.get("causal_path_refs"), "causal_path_refs"),
        counterfactual=value.get("counterfactual"),
        compared_candidate_refs=_strings(
            value.get("compared_candidate_refs"), "compared_candidate_refs"
        ),
        causal_role=str(value.get("causal_role") or ""),
        reason=str(value.get("reason") or ""),
        evidence_refs=_strings(value.get("evidence_refs"), "assessment evidence_refs"),
        confidence=_confidence(value.get("confidence")),
    )


def _counterfactual(
    value: Any, *, candidate_ref: str = ""
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("counterfactual must be an object")
    required = {
        "intervention_ref",
        "intervention_kind",
        "predicted_defect_status",
        "causal_effect",
    }
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError(
            "counterfactual schema mismatch (missing={0}, extra={1})".format(
                sorted(required - actual), sorted(actual - required)
            )
        )
    intervention_ref = str(value.get("intervention_ref") or "").strip()
    intervention_kind = str(value.get("intervention_kind") or "").strip()
    predicted = str(value.get("predicted_defect_status") or "").strip()
    causal_effect = str(value.get("causal_effect") or "").strip()
    if not intervention_ref or (candidate_ref and intervention_ref != candidate_ref):
        raise ValueError("counterfactual intervention_ref must match candidate_ref")
    if intervention_kind != "replace_with_semantically_correct_behavior":
        raise ValueError(
            "counterfactual intervention_kind must replace with correct behavior"
        )
    if predicted not in {"present", "absent"}:
        raise ValueError("counterfactual prediction must be decidable as present or absent")
    expected_effect = {
        "absent": "prevents_defect",
        "present": "does_not_prevent_defect",
    }[predicted]
    if causal_effect != expected_effect:
        raise ValueError("counterfactual causal_effect contradicts prediction")
    return FrozenMapping(
        {
            "intervention_ref": intervention_ref,
            "intervention_kind": intervention_kind,
            "predicted_defect_status": predicted,
            "causal_effect": causal_effect,
        }
    )


def _validate_assessment_counterfactual_consistency(
    assessment: GlobalCandidateAssessment,
) -> None:
    counterfactual_prevents = (
        assessment.counterfactual.get("predicted_defect_status") == "absent"
        and assessment.counterfactual.get("causal_effect") == "prevents_defect"
    )
    if assessment.causal_role == "root_candidate":
        if (
            assessment.input_defect_status == "present"
            or assessment.output_defect_status != "present"
            or not assessment.causal_path_refs
        ):
            raise ValueError(
                "root_candidate causal role requires input defect not present, "
                "present output, and a causal path"
            )
        if (
            assessment.input_defect_status == "unknown"
            and assessment.confidence >= 1.0
        ):
            raise ValueError(
                "root_candidate with unknown input defect cannot claim certain confidence"
            )
        expected_prevents = True
    else:
        expected_prevents = False
    if counterfactual_prevents is not expected_prevents:
        raise ValueError(
            "counterfactual contradicts assessment causal role and defect status"
        )


def _immutable_strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("{0} must be an array".format(field_name))
    return _strings(list(value), field_name)


def _has_eligible_causal_path_hops(
    capsule: CandidateEvidenceCapsule, path_refs: Tuple[str, ...]
) -> bool:
    edges = (
        *capsule.causal_path_edges,
        *capsule.outgoing_edges,
        *capsule.incoming_edges,
    )
    return all(
        any(
            str(edge.get("from_ref") or "") == source_ref
            and str(edge.get("to_ref") or "") == target_ref
            and is_confirmation_causal_edge(edge, default_eligible=False)
            for edge in edges
            if isinstance(edge, Mapping)
        )
        for source_ref, target_ref in zip(path_refs, path_refs[1:])
    )


def _active_focus_binding(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("active focus binding must be an object")
    required = {"seed_ref", "defect_fingerprint", "active_focus_text_hash"}
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError(
            "active focus binding schema mismatch (missing={0}, extra={1})".format(
                sorted(required - actual), sorted(actual - required)
            )
        )
    return FrozenMapping({key: str(value.get(key) or "") for key in sorted(required)})


def validate_active_focus_binding(
    request: GlobalCandidateJudgeRequest, judgment: GlobalCandidateJudgment
) -> None:
    expected = {
        "seed_ref": request.seed_ref,
        "defect_fingerprint": request.active_defect.fingerprint,
        "active_focus_text_hash": request.active_focus_text_hash,
    }
    if _thaw(judgment.active_focus_binding) != expected:
        raise ValueError("active focus binding does not match request")


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
    "MAX_ROOT_CONFIRMATION_CANDIDATES",
    "GlobalCandidateAssessment",
    "GlobalCandidateJudgeRequest",
    "GlobalCandidateJudgment",
    "GlobalJudgeCapability",
    "build_global_candidate_prompt",
    "active_focus_text_sha256",
    "global_candidate_judgment_from_payload",
    "normalize_active_focus_text",
    "validate_active_focus_binding",
    "validate_global_candidate_payload",
]
