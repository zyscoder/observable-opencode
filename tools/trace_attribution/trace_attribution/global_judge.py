"""Global comparative judgment over bounded candidate evidence closures."""

from __future__ import annotations

import copy
import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from .causal_state import (
    ActiveFailureRoleBinding,
    GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
    GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
    CausalCandidate,
    DefectState,
    FrozenMapping,
    active_failure_causal_role_for,
    active_failure_signature_for,
    seed_defect_state,
)
from .confirmation_path import (
    has_confirmation_causal_hop,
    is_confirmation_causal_edge,
)
from .evidence_capsule import (
    CandidateEvidenceCapsule,
    JUDGE_PROMPT_COLLECTION_ITEMS,
    JUDGE_PROMPT_MAXIMUM_DEPTH,
    JUDGE_PROMPT_STRING_CHARS,
    validate_candidate_evidence_capsules_against_graph,
)
from .evidence_expansion import (
    EVIDENCE_EXPANSION_CONTEXT_KINDS,
    EvidenceExpansionResult,
    validate_evidence_expansion_result_against_graph,
)
from .graph import TraceGraph
from .judgment_context import build_active_failure_factual_context
from .models import JsonDict, stable_json
from .restoration_obligation import RestorationObligation


GLOBAL_JUDGE_CANDIDATE_CAPSULE_MAX_BYTES = 8192
GLOBAL_JUDGE_CONTEXT_CAPSULE_MAX_BYTES = 4096
GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION = GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION
GLOBAL_JUDGE_DIAGNOSTICS_SCHEMA = "global-judge-diagnostics/v1"
GLOBAL_JUDGE_PROMPT_PROJECTION_SCHEMA = "global-judge-prompt-projection/v2"
GLOBAL_JUDGE_PROMPT_PROJECTION_POLICY = {
    "schema": "global-judge-prompt-projection-policy/v1",
    "string_char_limit": JUDGE_PROMPT_STRING_CHARS,
    "collection_item_limit": JUDGE_PROMPT_COLLECTION_ITEMS,
    "maximum_depth": JUDGE_PROMPT_MAXIMUM_DEPTH,
    "causal_path_collections": [
        "downstream_path",
        "downstream_path_references",
        "causal_path_edges",
    ],
    "causal_path_collection_policy": "preserve_complete",
}
GLOBAL_JUDGE_PROMPT_PROJECTION_KEYS = frozenset(
    {
        "schema",
        "canonical_request_sha256",
        "projected_facts_sha256",
        "omission_manifest_sha256",
        "projection_policy_sha256",
        "projection_identity",
        "projection_policy",
        "canonical_request_bytes",
        "projected_fact_bytes",
        "omitted_sections",
        "omitted_section_count",
        "omission_manifest",
        "candidate_count",
        "evidence_context_count",
    }
)
GLOBAL_JUDGE_OMISSION_MANIFEST_KEYS = frozenset(
    {
        "owner_ref",
        "path",
        "reason",
        "original_utf8_bytes",
        "original_sha256",
        "original_item_count",
        "retained_item_count",
    }
)
MAX_ROOT_CONFIRMATION_CANDIDATES = 3
GLOBAL_OUTCOMES = frozenset(
    {
        "candidate_roots",
        "no_root_candidates",
        "no_defect",
        "needs_expansion",
        "inconclusive",
    }
)
DEFECT_STATUSES = frozenset({"present", "absent", "unknown"})
RESPONSIBILITIES = frozenset({"primary", "shared", "none", "unknown"})
CANDIDATE_PHASES = frozenset(
    {
        "diagnostic",
        "planning",
        "intermediate",
        "implementation",
        "closure",
        "final",
    }
)
OBLIGATION_STATUSES = frozenset({"pending", "satisfied", "violated", "unknown"})
REPAIR_WINDOW_EFFECTS = frozenset({"remained_open", "closed", "unknown"})
FAILURE_MODES = frozenset(
    {
        "positive_introduction",
        "responsible_omission",
        "ordinary_non_repair",
        "omission_enabling_condition",
        "none",
        "unknown",
    }
)
CONTRIBUTION_MECHANISM_TYPES = frozenset(
    {
        "scope_narrowing",
        "misleading_success_signal",
        "faulty_assumption",
        "repair_opportunity_consumption",
    }
)
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
EXPANSION_CONTEXT_KINDS = EVIDENCE_EXPANSION_CONTEXT_KINDS


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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_global_judge_prompt_projection(value: Any) -> JsonDict:
    if not isinstance(value, Mapping) or set(value) != set(
        GLOBAL_JUDGE_PROMPT_PROJECTION_KEYS
    ):
        raise ValueError("global Judge prompt projection schema mismatch")
    payload = _thaw(value)
    if payload.get("schema") != GLOBAL_JUDGE_PROMPT_PROJECTION_SCHEMA:
        raise ValueError("global Judge prompt projection version is unsupported")
    for key in (
        "canonical_request_sha256",
        "projected_facts_sha256",
        "omission_manifest_sha256",
        "projection_policy_sha256",
        "projection_identity",
    ):
        if not _is_sha256(payload.get(key)):
            raise ValueError("global Judge prompt projection identity is invalid")
    policy = payload.get("projection_policy")
    if policy != GLOBAL_JUDGE_PROMPT_PROJECTION_POLICY:
        raise ValueError("global Judge prompt projection policy mismatch")
    expected_policy_hash = hashlib.sha256(
        stable_json(policy).encode("utf-8")
    ).hexdigest()
    if payload["projection_policy_sha256"] != expected_policy_hash:
        raise ValueError("global Judge prompt projection policy hash mismatch")
    omitted_sections = payload.get("omitted_sections")
    manifest = payload.get("omission_manifest")
    if (
        not isinstance(omitted_sections, list)
        or any(
            not isinstance(item, str) or not item
            for item in omitted_sections
        )
        or omitted_sections != sorted(set(omitted_sections))
        or not isinstance(manifest, list)
    ):
        raise ValueError("global Judge prompt projection omissions are invalid")
    for item in manifest:
        if not isinstance(item, Mapping) or set(item) != set(
            GLOBAL_JUDGE_OMISSION_MANIFEST_KEYS
        ):
            raise ValueError("global Judge omission manifest schema mismatch")
        if (
            not isinstance(item.get("owner_ref"), str)
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("reason"), str)
            or not item["reason"]
            or type(item.get("original_utf8_bytes")) is not int
            or item["original_utf8_bytes"] < 0
            or not _is_sha256(item.get("original_sha256"))
            or (
                item.get("original_item_count") is not None
                and (
                    type(item["original_item_count"]) is not int
                    or item["original_item_count"] < 0
                )
            )
            or (
                item.get("retained_item_count") is not None
                and (
                    type(item["retained_item_count"]) is not int
                    or item["retained_item_count"] < 0
                )
            )
        ):
            raise ValueError("global Judge omission manifest entry is invalid")
    if manifest != sorted(
        manifest,
        key=lambda item: (
            str(item.get("owner_ref") or ""),
            str(item.get("path") or ""),
            str(item.get("reason") or ""),
        ),
    ):
        raise ValueError("global Judge omission manifest order is invalid")
    expected_manifest_hash = hashlib.sha256(
        stable_json(manifest).encode("utf-8")
    ).hexdigest()
    if payload["omission_manifest_sha256"] != expected_manifest_hash:
        raise ValueError("global Judge omission manifest hash mismatch")
    for key in (
        "canonical_request_bytes",
        "projected_fact_bytes",
        "omitted_section_count",
        "candidate_count",
        "evidence_context_count",
    ):
        if type(payload.get(key)) is not int or payload[key] < 0:
            raise ValueError("global Judge prompt projection counts are invalid")
    if payload["omitted_section_count"] != len(omitted_sections):
        raise ValueError("global Judge prompt projection omission count mismatch")
    identity_facts = {
        key: payload[key]
        for key in (
            "schema",
            "canonical_request_sha256",
            "projected_facts_sha256",
            "omission_manifest_sha256",
            "projection_policy_sha256",
        )
    }
    expected_identity = hashlib.sha256(
        stable_json(identity_facts).encode("utf-8")
    ).hexdigest()
    if payload["projection_identity"] != expected_identity:
        raise ValueError("global Judge prompt projection identity mismatch")
    return payload


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


def _global_active_failure_factual_context(
    request: "GlobalCandidateJudgeRequest",
) -> JsonDict:
    entries = []
    competitors = []
    for capsule in request.capsules:
        candidate = _thaw(capsule.candidate)
        node = (
            candidate.get("node")
            if isinstance(candidate, Mapping)
            else {}
        )
        entries.append(
            {
                "candidate_ref": capsule.candidate_ref,
                "candidate_node": node,
                "path_refs": list(capsule.downstream_path),
                "path_references": _thaw(
                    capsule.downstream_path_references
                ),
                "edges": _thaw(capsule.causal_path_edges),
            }
        )
        competitors.append(
            {
                "candidate_ref": capsule.candidate_ref,
                "component": str(node.get("component") or "")
                if isinstance(node, Mapping)
                else "",
                "event_type": str(node.get("event_type") or "")
                if isinstance(node, Mapping)
                else "",
                "root_candidate_eligible": (
                    candidate.get("root_candidate_eligible") is True
                    if isinstance(candidate, Mapping)
                    else False
                ),
            }
        )
    return build_active_failure_factual_context(
        defect_state=request.active_defect,
        seed_ref=request.seed_ref,
        candidate_entries=entries,
        obligations=(
            obligation.to_dict()
            for obligation in request.restoration_obligations
        ),
        competitors=competitors,
    )


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
    restoration_obligations: Tuple[RestorationObligation, ...] = field(
        default_factory=tuple
    )
    evidence_context_capsules: Tuple[CandidateEvidenceCapsule, ...] = field(
        default_factory=tuple
    )
    trace_health: Mapping[str, Any] = field(default_factory=FrozenMapping)
    evidence_expansions: Tuple[EvidenceExpansionResult, ...] = field(
        default_factory=tuple
    )

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
        object.__setattr__(
            self,
            "restoration_obligations",
            tuple(self.restoration_obligations),
        )
        object.__setattr__(
            self,
            "evidence_context_capsules",
            tuple(self.evidence_context_capsules),
        )
        object.__setattr__(self, "trace_health", _freeze(self.trace_health))
        object.__setattr__(
            self, "evidence_expansions", tuple(self.evidence_expansions)
        )
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
        context_refs = [
            item.candidate_ref
            for item in self.evidence_context_capsules
        ]
        if (
            len(context_refs) != len(set(context_refs))
            or set(refs) & set(context_refs)
        ):
            raise ValueError(
                "global candidate request evidence context refs must be "
                "unique and disjoint from assessed candidates"
            )
        if self.start_refs != (self.seed_ref,):
            raise ValueError("global candidate request must bind exactly one active seed_ref")
        obligation_ids = []
        obligation_identities = []
        for obligation in self.restoration_obligations:
            if not isinstance(obligation, RestorationObligation):
                raise TypeError(
                    "global candidate request restoration obligation must be "
                    "RestorationObligation"
                )
            obligation_ids.append(obligation.obligation_id)
            obligation_identities.append(obligation.identity)
        if (
            len(obligation_ids) != len(set(obligation_ids))
            or len(obligation_identities) != len(set(obligation_identities))
        ):
            raise ValueError(
                "global candidate request contains duplicate restoration obligations"
            )
        for capsule in (
            *self.capsules,
            *self.evidence_context_capsules,
        ):
            capsule.validate()
            if capsule.defect_state != self.active_defect:
                raise ValueError("global candidate request capsule defect drifts from active_defect")
            if tuple(capsule.start_refs) != (self.seed_ref,):
                raise ValueError("global candidate request capsule drifts from active seed_ref")
        expansion_identities = []
        for expansion in self.evidence_expansions:
            if not isinstance(expansion, EvidenceExpansionResult):
                raise TypeError(
                    "global candidate request expansion must be EvidenceExpansionResult"
                )
            if expansion.status != "expanded":
                raise ValueError(
                    "global candidate request may include only successful evidence expansions"
                )
            if (
                expansion.request.seed_ref != self.seed_ref
                or expansion.request.defect_fingerprint
                != self.active_defect.fingerprint
            ):
                raise ValueError(
                    "global candidate request expansion drifts from active seed or defect"
                )
            expansion_identities.append(expansion.request_identity)
        if len(expansion_identities) != len(set(expansion_identities)):
            raise ValueError(
                "global candidate request contains duplicate evidence expansions"
            )

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
                and len(item.downstream_path) > 1
                and item.downstream_path[0] == item.candidate_ref
                and item.downstream_path[-1] == self.seed_ref
                and _has_eligible_causal_path_hops(
                    item, item.downstream_path
                )
            )
        )

    @property
    def grounded_refs(self) -> Tuple[str, ...]:
        return self._grounded_refs(include_obligations=True)

    def _grounded_refs(self, *, include_obligations: bool) -> Tuple[str, ...]:
        refs = list(self.start_refs)
        for capsule in (
            *self.capsules,
            *self.evidence_context_capsules,
        ):
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
        for expansion in self.evidence_expansions:
            refs.extend(
                (
                    expansion.request.anchor_ref,
                    *expansion.resolved_refs,
                )
            )
            refs.extend(_expansion_payload_refs(expansion.to_dict()))
        if include_obligations:
            for obligation in self.restoration_obligations:
                refs.append(obligation.obligation_id)
                refs.extend(obligation.scope_refs)
                refs.extend(obligation.acceptance_evidence_refs)
                refs.extend(
                    tuple(obligation.provenance.get("source_refs") or ())
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
        return self._projection(compact_for_judge=False)

    def judge_dict(self) -> JsonDict:
        """Return the bounded prompt projection; to_dict remains lossless."""
        return self._projection(compact_for_judge=True)

    def _projection(self, *, compact_for_judge: bool) -> JsonDict:
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
            "factual_context": _global_active_failure_factual_context(self),
            "trace_health": _thaw(self.trace_health),
            "evidence_expansions": [
                item.to_dict() for item in self.evidence_expansions
            ],
            "restoration_obligations": [
                item.to_dict() for item in self.restoration_obligations
            ],
            "offered_candidate_refs": list(self.offered_candidate_refs),
            "open_authored_root_candidate_refs": list(
                self.open_authored_root_candidate_refs
            ),
            "retrieval_is_not_causal_verdict": True,
            "grounded_refs": list(self.grounded_refs),
            "candidate_evidence_capsules": [
                (
                    item.compact_judge_dict(
                        max_bytes=GLOBAL_JUDGE_CANDIDATE_CAPSULE_MAX_BYTES
                    )
                    if compact_for_judge
                    else item.judge_dict()
                )
                for item in self.capsules
            ],
            "evidence_context_capsules": [
                (
                    item.compact_judge_dict(
                        max_bytes=GLOBAL_JUDGE_CONTEXT_CAPSULE_MAX_BYTES
                    )
                    if compact_for_judge
                    else item.judge_dict()
                )
                for item in self.evidence_context_capsules
            ],
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
            "evidence_expansions": [
                item.to_dict() for item in self.evidence_expansions
            ],
            "restoration_obligations": [
                item.to_dict() for item in self.restoration_obligations
            ],
            "candidate_evidence_capsules": [
                item.to_dict() for item in self.capsules
            ],
            "evidence_context_capsules": [
                item.to_dict()
                for item in self.evidence_context_capsules
            ],
        }

    def judge_prompt_projection(self) -> JsonDict:
        """Build the bounded factual view sent to the Global Judge."""
        canonical = self.to_dict()
        canonical_json = stable_json(canonical)
        omitted_sections: List[str] = []
        omission_manifest: List[JsonDict] = []
        projected: JsonDict = {
            "case_id": self.case_id,
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "seed_ref": self.seed_ref,
            "active_defect": self.active_defect.to_dict(),
            "active_focus_text": self.active_focus_text,
            "active_focus_text_hash": self.active_focus_text_hash,
            "start_refs": list(self.start_refs),
            "active_focus": copy.deepcopy(canonical["active_focus"]),
            "factual_context": copy.deepcopy(canonical["factual_context"]),
            "trace_health": copy.deepcopy(canonical["trace_health"]),
            "evidence_expansions": copy.deepcopy(
                canonical["evidence_expansions"]
            ),
            "restoration_obligations": copy.deepcopy(
                canonical["restoration_obligations"]
            ),
            "offered_candidate_refs": list(self.offered_candidate_refs),
            "open_authored_root_candidate_refs": list(
                self.open_authored_root_candidate_refs
            ),
            "retrieval_is_not_causal_verdict": True,
            "grounded_refs": list(self.grounded_refs),
            "candidate_evidence_capsules": [
                item.judge_prompt_dict(
                    omitted_sections=omitted_sections,
                    omission_manifest=omission_manifest,
                )
                for item in self.capsules
            ],
            "evidence_context_capsules": [
                item.judge_prompt_dict(
                    omitted_sections=omitted_sections,
                    omission_manifest=omission_manifest,
                )
                for item in self.evidence_context_capsules
            ],
        }
        projected_json = stable_json(projected)
        sorted_manifest = sorted(
            omission_manifest,
            key=lambda item: (
                str(item.get("owner_ref") or ""),
                str(item.get("path") or ""),
                str(item.get("reason") or ""),
            ),
        )
        projected_facts_sha256 = hashlib.sha256(
            projected_json.encode("utf-8")
        ).hexdigest()
        omission_manifest_sha256 = hashlib.sha256(
            stable_json(sorted_manifest).encode("utf-8")
        ).hexdigest()
        projection_policy = copy.deepcopy(
            GLOBAL_JUDGE_PROMPT_PROJECTION_POLICY
        )
        projection_policy_sha256 = hashlib.sha256(
            stable_json(projection_policy).encode("utf-8")
        ).hexdigest()
        canonical_request_sha256 = hashlib.sha256(
            canonical_json.encode("utf-8")
        ).hexdigest()
        projection_identity_facts = {
            "schema": GLOBAL_JUDGE_PROMPT_PROJECTION_SCHEMA,
            "canonical_request_sha256": canonical_request_sha256,
            "projected_facts_sha256": projected_facts_sha256,
            "omission_manifest_sha256": omission_manifest_sha256,
            "projection_policy_sha256": projection_policy_sha256,
        }
        projected["prompt_projection"] = {
            **projection_identity_facts,
            "projection_identity": hashlib.sha256(
                stable_json(projection_identity_facts).encode("utf-8")
            ).hexdigest(),
            "projection_policy": projection_policy,
            "canonical_request_bytes": len(
                canonical_json.encode("utf-8")
            ),
            "projected_fact_bytes": len(
                projected_json.encode("utf-8")
            ),
            "omitted_sections": sorted(set(omitted_sections)),
            "omitted_section_count": len(set(omitted_sections)),
            "omission_manifest": sorted_manifest,
            "candidate_count": len(self.capsules),
            "evidence_context_count": len(
                self.evidence_context_capsules
            ),
        }
        return projected


def global_candidate_request_from_validation_envelope(
    value: Any,
    *,
    graph: Optional[TraceGraph] = None,
    authoritative_candidates: Sequence[CausalCandidate] = (),
    authoritative_objective: Optional[str] = None,
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
        "evidence_expansions",
        "restoration_obligations",
        "candidate_evidence_capsules",
        "evidence_context_capsules",
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
    evidence_context_capsules = value.get(
        "evidence_context_capsules"
    )
    evidence_expansions = value.get("evidence_expansions")
    restoration_obligations = value.get("restoration_obligations")
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
    if not isinstance(evidence_context_capsules, list):
        raise TypeError(
            "validation envelope evidence_context_capsules must be an array"
        )
    if not isinstance(evidence_expansions, list):
        raise TypeError(
            "validation envelope evidence_expansions must be an array"
        )
    if not isinstance(restoration_obligations, list):
        raise TypeError(
            "validation envelope restoration_obligations must be an array"
        )
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
        restoration_obligations=tuple(
            RestorationObligation.from_dict(item)
            for item in restoration_obligations
        ),
        evidence_context_capsules=tuple(
            CandidateEvidenceCapsule.from_dict(item)
            for item in evidence_context_capsules
        ),
        trace_health=trace_health,
        evidence_expansions=tuple(
            EvidenceExpansionResult.from_dict(item)
            for item in evidence_expansions
        ),
    )
    if graph is not None:
        validate_global_candidate_request_against_graph(
            graph,
            request,
            authoritative_candidates=authoritative_candidates,
            authoritative_objective=authoritative_objective,
        )
    return request


def validate_global_candidate_request_against_graph(
    graph: TraceGraph,
    request: GlobalCandidateJudgeRequest,
    *,
    authoritative_candidates: Sequence[CausalCandidate] = (),
    authoritative_objective: Optional[str] = None,
) -> None:
    request.validate()
    if authoritative_objective is None:
        raise ValueError(
            "global candidate graph validation requires an authoritative objective"
        )
    if request.objective != authoritative_objective:
        raise ValueError(
            "global candidate request objective drifts from the authoritative objective"
        )
    resolved_seed = graph.resolve(request.seed_ref)
    seed_node = graph.nodes.get(resolved_seed or "")
    if seed_node is None:
        raise ValueError(
            "global candidate request graph seed is unresolved"
        )
    authoritative_defect = seed_defect_state(
        seed_node,
        authoritative_objective,
    )
    if request.active_defect != authoritative_defect:
        raise ValueError(
            "global candidate request active defect contradicts the graph seed"
        )
    if (
        normalize_active_focus_text(request.active_focus_text)
        != normalize_active_focus_text(authoritative_defect.actual)
        or request.active_focus_text_hash
        != active_focus_text_sha256(authoritative_defect.actual)
    ):
        raise ValueError(
            "global candidate request active focus contradicts the graph seed"
        )
    for label, refs in (
        ("seed", (request.seed_ref,)),
        ("start", request.start_refs),
    ):
        for ref in refs:
            resolved = graph.resolve(ref)
            if (
                not resolved
                or resolved not in graph.nodes
                or not graph.active_revision_start_eligible(resolved)
            ):
                raise ValueError(
                    "global candidate request {0} ref is unresolved or "
                    "ineligible for strict active-start provenance: {1}".format(
                        label, ref
                    )
                )
    validate_candidate_evidence_capsules_against_graph(
        graph,
        (
            *request.capsules,
            *request.evidence_context_capsules,
        ),
        authoritative_candidates=authoritative_candidates,
    )
    for expansion in request.evidence_expansions:
        validate_evidence_expansion_result_against_graph(
            graph,
            expansion,
            limits=expansion.limits,
        )
    for obligation in request.restoration_obligations:
        for ref in (
            *obligation.scope_refs,
            *obligation.acceptance_evidence_refs,
            *tuple(obligation.provenance.get("source_refs") or ()),
        ):
            resolved = graph.resolve(ref)
            if not resolved or resolved not in graph.nodes:
                raise ValueError(
                    "global candidate restoration obligation contains an "
                    "unresolved graph ref"
                )
    if any(
        not graph.active_revision_evidence_eligible(capsule.candidate_ref)
        for capsule in request.capsules
    ):
        raise ValueError(
            "global candidate request contains a candidate ineligible for the active revision"
        )


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
    responsibility: str
    candidate_phase: str
    obligation_status_before: str
    obligation_status_after: str
    repair_window_effect: str
    failure_mode: str
    obligation_refs: Tuple[str, ...]
    contribution_mechanism: Optional[Mapping[str, Any]]
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
        if self.responsibility not in RESPONSIBILITIES:
            raise ValueError("unsupported global candidate responsibility")
        if self.candidate_phase not in CANDIDATE_PHASES:
            raise ValueError("unsupported global candidate candidate_phase")
        if self.obligation_status_before not in OBLIGATION_STATUSES:
            raise ValueError(
                "unsupported global candidate obligation_status_before"
            )
        if self.obligation_status_after not in OBLIGATION_STATUSES:
            raise ValueError(
                "unsupported global candidate obligation_status_after"
            )
        if self.repair_window_effect not in REPAIR_WINDOW_EFFECTS:
            raise ValueError(
                "unsupported global candidate repair_window_effect"
            )
        if self.failure_mode not in FAILURE_MODES:
            raise ValueError("unsupported global candidate failure_mode")
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
        object.__setattr__(
            self,
            "obligation_refs",
            _immutable_strings(self.obligation_refs, "obligation_refs"),
        )
        object.__setattr__(
            self,
            "contribution_mechanism",
            _contribution_mechanism(self.contribution_mechanism),
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
            "responsibility": self.responsibility,
            "candidate_phase": self.candidate_phase,
            "obligation_status_before": self.obligation_status_before,
            "obligation_status_after": self.obligation_status_after,
            "repair_window_effect": self.repair_window_effect,
            "failure_mode": self.failure_mode,
            "obligation_refs": list(self.obligation_refs),
            "contribution_mechanism": (
                _thaw(self.contribution_mechanism)
                if self.contribution_mechanism is not None
                else None
            ),
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
    active_failure_role_bindings: Tuple[
        ActiveFailureRoleBinding, ...
    ] = field(default_factory=tuple)

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
        bindings = tuple(
            sorted(
                self.active_failure_role_bindings,
                key=lambda item: (
                    item.candidate_ref,
                    item.failure_signature,
                    item.causal_role,
                ),
            )
        )
        if any(
            not isinstance(item, ActiveFailureRoleBinding)
            for item in bindings
        ):
            raise TypeError(
                "active failure role bindings must be typed bindings"
            )
        object.__setattr__(
            self,
            "active_failure_role_bindings",
            bindings,
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


def active_failure_role_binding_for_assessment(
    *,
    request: GlobalCandidateJudgeRequest,
    assessment: GlobalCandidateAssessment,
    selected: bool,
) -> ActiveFailureRoleBinding:
    capsule = next(
        (
            item
            for item in request.capsules
            if item.candidate_ref == assessment.candidate_ref
        ),
        None,
    )
    if capsule is None:
        raise ValueError(
            "active failure role binding requires an offered candidate"
        )
    candidate = capsule.candidate
    node = candidate.get("node")
    if not isinstance(node, Mapping):
        node = {}
    signature = active_failure_signature_for(
        request.active_defect,
        seed_ref=request.seed_ref,
    )
    role = active_failure_causal_role_for(
        candidate_ref=assessment.candidate_ref,
        component=str(node.get("component") or ""),
        event_type=str(node.get("event_type") or ""),
        candidate_phase=assessment.candidate_phase,
        failure_mode=assessment.failure_mode,
        comparative_role=assessment.causal_role,
        candidate_facts=node.get("data") or {},
    )
    prevents = (
        selected
        and assessment.counterfactual.get("predicted_defect_status")
        == "absent"
        and assessment.counterfactual.get("causal_effect")
        == "prevents_defect"
    )
    return ActiveFailureRoleBinding.create(
        candidate_ref=assessment.candidate_ref,
        seed_ref=request.seed_ref,
        failure_signature=str(signature["fingerprint"]),
        failure_kind=str(signature["kind"]),
        causal_role=role,
        disposition="root" if selected else "factor",
        counterfactual_prevention_signatures=(
            (str(signature["fingerprint"]),) if prevents else ()
        ),
    )


def global_candidate_comparison_contract_from_context(
    request_context: Mapping[str, Any],
) -> JsonDict:
    """Build the exact structural obligations for every offered candidate."""
    raw_capsules = request_context.get("candidate_evidence_capsules")
    capsules = (
        [item for item in raw_capsules if isinstance(item, Mapping)]
        if isinstance(raw_capsules, list)
        else []
    )
    open_refs = [
        str(item)
        for item in request_context.get(
            "open_authored_root_candidate_refs"
        )
        or ()
        if str(item)
    ]
    active_focus = request_context.get("active_focus")
    if not isinstance(active_focus, Mapping):
        active_defect = request_context.get("active_defect")
        active_focus = {
            "seed_ref": request_context.get("seed_ref"),
            "defect_fingerprint": (
                active_defect.get("fingerprint")
                if isinstance(active_defect, Mapping)
                else ""
            ),
            "active_focus_text_hash": request_context.get(
                "active_focus_text_hash"
            ),
        }
    requirements = []
    role_order = [
        "root_candidate",
        "contributing_condition",
        "amplifying_factor",
        "outcome_evidence",
        "exculpatory_evidence",
        "unrelated",
        "unknown",
    ]
    for capsule in capsules:
        candidate = capsule.get("candidate")
        if not isinstance(candidate, Mapping):
            candidate = {}
        candidate_ref = str(capsule.get("candidate_ref") or "")
        path = [
            str(item)
            for item in capsule.get("downstream_path") or ()
            if str(item)
        ]
        root_eligible = (
            candidate.get("root_candidate_eligible") is True
        )
        exact_path = (
            path
            if len(path) > 1
            and path[0] == candidate_ref
            and path[-1] == str(active_focus.get("seed_ref") or "")
            and _mapping_has_eligible_causal_path_hops(
                capsule,
                tuple(path),
            )
            else []
        )
        open_candidate = (
            candidate_ref in open_refs and bool(exact_path)
        )
        requirements.append(
            {
                "candidate_ref": candidate_ref,
                "root_candidate_eligible": root_eligible,
                "open_authored_root_candidate": open_candidate,
                "required_causal_path_refs": exact_path,
                "required_compared_candidate_refs": open_refs,
                "exact_output_fields": {
                    "candidate_ref": candidate_ref,
                    "causal_path_refs": exact_path,
                    "compared_candidate_refs": open_refs,
                    "counterfactual_intervention_ref": candidate_ref,
                },
                "model_decides_fields": [
                    "defect_status",
                    "input_defect_status",
                    "output_defect_status",
                    "counterfactual.intervention_kind",
                    "counterfactual.predicted_defect_status",
                    "counterfactual.causal_effect",
                    "causal_role",
                    "responsibility",
                    "candidate_phase",
                    "obligation_status_before",
                    "obligation_status_after",
                    "repair_window_effect",
                    "failure_mode",
                    "obligation_refs",
                    "contribution_mechanism",
                    "reason",
                    "evidence_refs",
                    "confidence",
                ],
                "allowed_causal_roles": [
                    role
                    for role in role_order
                    if (
                        (open_candidate or role != "root_candidate")
                        and (
                            bool(exact_path)
                            or role
                            not in {
                                "root_candidate",
                                "contributing_condition",
                                "amplifying_factor",
                            }
                        )
                    )
                ],
                "status_values": ["present", "absent", "unknown"],
                "defect_status_relation": (
                    "defect_status_equals_output_defect_status"
                ),
                "causal_path_requirement": (
                    "exact_required_path_for_any_causal_role"
                ),
                "counterfactual_requirement": (
                    "decidable_present_or_absent_prediction_for_any_causal_role"
                ),
                "root_candidate_requirement": (
                    "positive_introduction_or_responsible_omission_with_"
                    "output_present_and_preventing_counterfactual"
                ),
            }
        )
    return {
        "schema": "global-candidate-comparison-contract/v2",
        "active_focus_binding": {
            "seed_ref": str(active_focus.get("seed_ref") or ""),
            "defect_fingerprint": str(
                active_focus.get("defect_fingerprint") or ""
            ),
            "active_focus_text_hash": str(
                active_focus.get("active_focus_text_hash") or ""
            ),
        },
        "open_authored_root_candidate_refs": open_refs,
        "assessment_requirements": requirements,
        "candidate_roots_terminal_matrix": {
            "candidate_refs": open_refs,
            "every_candidate_requires": [
                "non_unknown_output_defect_status",
                "non_unknown_causal_role",
                "exact_non_empty_causal_path_refs",
                (
                    "unknown_input_for_root_condition_or_amplifier_requires_"
                    "confidence_below_1"
                ),
            ],
            "selected_root_requires": [
                "output_defect_status_is_present",
                "causal_role_is_root_candidate",
                "counterfactual_predicted_defect_status_is_absent",
                "counterfactual_causal_effect_is_prevents_defect",
                (
                    "failure_mode_is_positive_introduction_with_nonpresent_"
                    "input_or_responsible_omission_with_grounded_closed_"
                    "restoration_obligation"
                ),
            ],
        },
        "matrix_completion_rule": (
            "assess_every_offered_candidate_exactly_once_before_selection"
        ),
    }


def global_candidate_comparison_contract(
    request: GlobalCandidateJudgeRequest,
) -> JsonDict:
    request.validate()
    return global_candidate_comparison_contract_from_context(
        request.judge_dict()
    )


def canonicalize_global_candidate_structural_bindings(
    value: Mapping[str, Any],
    *,
    request: GlobalCandidateJudgeRequest,
) -> Tuple[JsonDict, Tuple[JsonDict, ...]]:
    """Bind request-owned fields without changing the model's semantic verdict."""
    request.validate()
    if not isinstance(value, Mapping):
        raise TypeError("global candidate judgment must be an object")
    normalized = copy.deepcopy(dict(value))
    corrections: List[JsonDict] = []

    def replace(
        container: JsonDict,
        field: str,
        expected: Any,
        *,
        candidate_ref: str = "",
        event_field: Optional[str] = None,
    ) -> None:
        before = container.get(field)
        if before == expected:
            return
        container[field] = copy.deepcopy(expected)
        corrections.append(
            {
                "scope": (
                    "assessment" if candidate_ref else "judgment"
                ),
                "candidate_ref": candidate_ref,
                "field": event_field or field,
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(expected),
                "reason": "deterministic_request_binding",
            }
        )

    replace(
        normalized,
        "active_focus_binding",
        {
            "seed_ref": request.seed_ref,
            "defect_fingerprint": request.active_defect.fingerprint,
            "active_focus_text_hash": request.active_focus_text_hash,
        },
    )
    capsules_by_ref = {
        capsule.candidate_ref: capsule for capsule in request.capsules
    }
    raw_assessments = normalized.get("assessments")
    if not isinstance(raw_assessments, list):
        return normalized, tuple(corrections)
    compared_refs = list(request.open_authored_root_candidate_refs)
    for raw_assessment in raw_assessments:
        if not isinstance(raw_assessment, dict):
            continue
        candidate_ref = str(
            raw_assessment.get("candidate_ref") or ""
        )
        capsule = capsules_by_ref.get(candidate_ref)
        if capsule is None:
            continue
        output_status = raw_assessment.get("output_defect_status")
        if output_status in DEFECT_STATUSES:
            replace(
                raw_assessment,
                "defect_status",
                output_status,
                candidate_ref=candidate_ref,
            )
        exact_path = (
            list(capsule.downstream_path)
            if len(capsule.downstream_path) > 1
            and capsule.downstream_path[0] == candidate_ref
            and capsule.downstream_path[-1] == request.seed_ref
            and _has_eligible_causal_path_hops(
                capsule,
                capsule.downstream_path,
            )
            else []
        )
        replace(
            raw_assessment,
            "causal_path_refs",
            exact_path,
            candidate_ref=candidate_ref,
        )
        replace(
            raw_assessment,
            "compared_candidate_refs",
            compared_refs,
            candidate_ref=candidate_ref,
        )
        counterfactual = raw_assessment.get("counterfactual")
        if not isinstance(counterfactual, dict):
            continue
        replace(
            counterfactual,
            "intervention_ref",
            candidate_ref,
            candidate_ref=candidate_ref,
            event_field="counterfactual.intervention_ref",
        )
    return normalized, tuple(corrections)


def global_judge_diagnostics(
    structural_corrections: Sequence[Mapping[str, Any]] = (),
) -> JsonDict:
    return validate_global_judge_diagnostics(
        {
            "schema": GLOBAL_JUDGE_DIAGNOSTICS_SCHEMA,
            "structural_corrections": [
                copy.deepcopy(dict(item))
                for item in structural_corrections
            ],
        }
    )


def validate_global_judge_diagnostics(value: Any) -> JsonDict:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "structural_corrections",
    }:
        raise ValueError("global Judge diagnostics schema mismatch")
    if value.get("schema") != GLOBAL_JUDGE_DIAGNOSTICS_SCHEMA:
        raise ValueError("unsupported global Judge diagnostics schema")
    raw_corrections = value.get("structural_corrections")
    if not isinstance(raw_corrections, (list, tuple)):
        raise TypeError(
            "global Judge structural_corrections must be an array"
        )
    corrections = []
    for item in raw_corrections:
        if not isinstance(item, Mapping) or set(item) != {
            "scope",
            "candidate_ref",
            "field",
            "before",
            "after",
            "reason",
        }:
            raise ValueError(
                "global Judge structural correction schema mismatch"
            )
        scope = str(item.get("scope") or "")
        candidate_ref = str(item.get("candidate_ref") or "")
        field_name = str(item.get("field") or "")
        reason = str(item.get("reason") or "")
        if (
            scope not in {"judgment", "assessment"}
            or (scope == "assessment" and not candidate_ref)
            or not field_name
            or reason != "deterministic_request_binding"
        ):
            raise ValueError(
                "global Judge structural correction is invalid"
            )
        correction = {
            "scope": scope,
            "candidate_ref": candidate_ref,
            "field": field_name,
            "before": _thaw(item.get("before")),
            "after": _thaw(item.get("after")),
            "reason": reason,
        }
        stable_json(correction)
        corrections.append(correction)
    return {
        "schema": GLOBAL_JUDGE_DIAGNOSTICS_SCHEMA,
        "structural_corrections": corrections,
    }


def build_global_candidate_prompt(request: GlobalCandidateJudgeRequest) -> str:
    request.validate()
    return stable_json(
        {
            "request": request.judge_prompt_projection(),
            "candidate_comparison_contract": (
                global_candidate_comparison_contract(request)
            ),
            "comparison_then_selection": [
                "compare_input_and_output_defect_status",
                "ground_causal_paths",
                "evaluate_counterfactual_predictions",
                "compare_all_open_authored_root_candidates_including_self",
                "select_candidate_roots_last",
            ],
            "rules": [
                "Use only grounded facts in candidate_evidence_capsules and evidence_context_capsules.",
                "Assess exactly candidate_evidence_capsules. evidence_context_capsules are citation-ready supporting or counterevidence and MUST NOT receive assessments or causal roles.",
                "retrieval_is_not_causal_verdict=true. Retrieval rank and score are navigation evidence only and MUST NOT enter root selection rules.",
                "First compare every offered candidate and return exactly one complete assessment per candidate; only after the comparison matrix is complete may selected_candidate_refs be chosen.",
                "Judge only request.active_focus. Do not substitute another claim or defect from a shared response, neighboring capsule, or broader objective.",
                "For each assessment, judge input_defect_status before the candidate and output_defect_status after it to determine whether the active defect is true; defect_status must equal output_defect_status and does not answer whether the record itself exists or contains defect-related words.",
                "Judge physical defect propagation separately from normative repair responsibility. A preexisting input defect does not by itself exclude a responsible omission root.",
                "Use positive_introduction only when the candidate changes a non-present input defect to a present output defect.",
                "Use responsible_omission only when a primary or shared restoration obligation was pending before the candidate, violated after it, the candidate is a closure or final boundary, the repair window closes, and replacing it with obligation-satisfying behavior prevents the defect.",
                "A diagnostic or intermediate node whose repair window remains open is ordinary_non_repair, not a root and not a contributor merely because the defect remains present.",
                "A contributing_condition or amplifying_factor requires an explicit grounded contribution_mechanism with type, affected target, effect, and evidence refs. Present-to-present propagation alone is insufficient.",
                "Any candidate may use input_defect_status=unknown when the supplied facts do not establish its prior state; for a root_candidate, contributing_condition, or amplifying_factor, its reason and confidence must preserve that uncertainty and confidence cannot be 1.0.",
                "For every open authored root-eligible candidate, causal_path_refs must equal the supplied candidate-to-seed path regardless of the selected role; counterfactual must make a decidable present-or-absent output prediction.",
                "compared_candidate_refs must exactly equal request.open_authored_root_candidate_refs, which may be empty; root_candidate_eligible=true alone does not add a candidate without a complete eligible causal path.",
                "When decisive counterevidence refutes a derived defect observation, mark that observation absent for the active defect even though its trace record exists.",
                "Choose candidate_roots only for authored nodes that positively introduce the active defect or are responsible closure boundaries that omit a required restoration.",
                "Never select a candidate with root_candidate_eligible=false as a root; treat tool results, verification, and evidence facts as evidence instead.",
                "Choose no_root_candidates when every offered candidate has a complete assessment, none is supported as a root, and the active defect has not itself been refuted. This is the normal page-level outcome when the root is likely on another page.",
                "Choose no_defect only when decisive grounded evidence contradicts the observed defect and every open authored root-eligible candidate has exactly one conclusive assessment that rules it out.",
                "For no_defect, every open authored root-eligible candidate must have known input and output status, absent output, a known causal role, a complete grounded candidate-to-seed path, and a decidable counterfactual; uncertainty requires needs_expansion or inconclusive with concrete missing_evidence.",
                "Choose needs_expansion only when a specific grounded anchor needs more upstream, downstream, artifact, action_group, message_transform, or full_node context and the missing content would change the current judgment.",
                "An unavailable or truncated artifact is not automatically blocking when recorded structured facts or previews already decide the objective; explain any actual semantic gap instead of expanding by default.",
                "Outcome evidence and successful verification can expose or contradict a defect but are not root causes merely because they are adjacent.",
                "Do not force a root; inconclusive and no_defect are valid outcomes.",
            ],
            "required_json_schema": {
                "outcome": "candidate_roots|no_root_candidates|no_defect|needs_expansion|inconclusive",
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
                            "intervention_kind": "replace_with_semantically_correct_behavior|replace_with_obligation_satisfying_behavior",
                            "predicted_defect_status": "present|absent",
                            "causal_effect": "prevents_defect|does_not_prevent_defect",
                        },
                        "compared_candidate_refs": ["all open authored root-eligible refs, including self when eligible"],
                        "causal_role": "root_candidate|contributing_condition|amplifying_factor|outcome_evidence|exculpatory_evidence|unrelated|unknown",
                        "responsibility": "primary|shared|none|unknown",
                        "candidate_phase": "diagnostic|planning|intermediate|implementation|closure|final",
                        "obligation_status_before": "pending|satisfied|violated|unknown",
                        "obligation_status_after": "pending|satisfied|violated|unknown",
                        "repair_window_effect": "remained_open|closed|unknown",
                        "failure_mode": "positive_introduction|responsible_omission|ordinary_non_repair|omission_enabling_condition|none|unknown",
                        "obligation_refs": ["restoration obligation ids supplied by request"],
                        "contribution_mechanism": {
                            "type": "scope_narrowing|misleading_success_signal|faulty_assumption|repair_opportunity_consumption",
                            "target_ref": "grounded affected ref",
                            "effect": "non-empty grounded causal effect",
                            "evidence_refs": ["grounded refs"],
                        },
                        "reason": "non-empty grounded comparative reason",
                        "evidence_refs": ["grounded refs"],
                        "confidence": "number 0..1",
                    }
                ],
                "selected_candidate_refs": ["exact offered refs"],
                "expansion_requests": [
                    {
                        "anchor_ref": "grounded ref",
                        "context_kind": "upstream|downstream|artifact|action_group|message_transform|full_node",
                        "reason": "non-empty evidence-gap reason",
                        "expected_judgment_change": "non-empty statement of which assessment or outcome may change",
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
    obligation_ids = {
        obligation.obligation_id
        for obligation in request.restoration_obligations
    }
    decisive = _strings(value.get("decisive_evidence_refs"), "decisive_evidence_refs")
    if any(ref not in grounded for ref in decisive):
        raise ValueError("decisive evidence must use grounded refs")
    open_authored_ref_set = set(
        request.open_authored_root_candidate_refs
    )
    for item in assessments:
        if any(ref not in obligation_ids for ref in item.obligation_refs):
            raise ValueError(
                "assessment obligation_refs must use restoration obligations "
                "from the current request"
            )
        if any(ref not in grounded for ref in item.evidence_refs):
            raise ValueError("assessment evidence must use grounded refs")
        if item.contribution_mechanism is not None:
            mechanism_refs = (
                str(item.contribution_mechanism.get("target_ref") or ""),
                *tuple(item.contribution_mechanism.get("evidence_refs") or ()),
            )
            if any(ref not in grounded for ref in mechanism_refs):
                raise ValueError(
                    "contribution mechanism must use grounded refs"
                )
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
        if set(item.compared_candidate_refs) != open_authored_ref_set:
            raise ValueError(
                "compared_candidate_refs must cover every open authored root-eligible candidate"
            )
    missing = _strings(value.get("missing_evidence"), "missing_evidence")
    expansion = _expansion_requests(value.get("expansion_requests"), grounded)
    by_ref = {item.candidate_ref: item for item in assessments}
    open_assessments = [
        by_ref[ref]
        for ref in sorted(open_authored_ref_set)
    ]
    incomplete_open_assessment_fields = []
    for item in open_assessments:
        missing_fields = []
        if not item.causal_path_refs:
            missing_fields.append("causal_path_refs")
        if item.output_defect_status == "unknown":
            missing_fields.append("output_defect_status")
        if item.causal_role == "unknown":
            missing_fields.append("causal_role")
        if missing_fields:
            incomplete_open_assessment_fields.append(
                {
                    "candidate_ref": item.candidate_ref,
                    "incomplete_fields": missing_fields,
                }
            )
    incomplete_open_assessments = [
        by_ref[item["candidate_ref"]]
        for item in incomplete_open_assessment_fields
    ]
    if outcome == "candidate_roots":
        if not selected:
            raise ValueError("candidate_roots requires selected candidates")
        if not decisive:
            raise ValueError("candidate_roots requires decisive evidence")
        if incomplete_open_assessments:
            raise ValueError(
                "candidate_roots requires a complete comparison for every "
                "open authored root-eligible candidate, including known "
                "status, role, and causal_path_refs; incomplete={0}".format(
                    stable_json(incomplete_open_assessment_fields)
                )
            )
        for ref in selected:
            assessment = by_ref[ref]
            if (
                assessment.output_defect_status != "present"
                or assessment.causal_role != "root_candidate"
            ):
                raise ValueError(
                    "selected root requires a valid introduction or responsible "
                    "omission with present output defect"
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
    elif outcome == "no_root_candidates":
        if selected or expansion:
            raise ValueError(
                "no_root_candidates cannot select roots or request expansion"
            )
        if not decisive:
            raise ValueError(
                "no_root_candidates requires decisive candidate evidence"
            )
        if missing:
            raise ValueError(
                "no_root_candidates cannot retain missing evidence"
            )
        if incomplete_open_assessments:
            raise ValueError(
                "no_root_candidates requires a complete comparison for every "
                "open authored root-eligible candidate; incomplete={0}".format(
                    stable_json(incomplete_open_assessment_fields)
                )
            )
        if any(
            item.input_defect_status == "unknown"
            for item in open_assessments
        ):
            raise ValueError(
                "no_root_candidates requires known input status for every "
                "open authored root-eligible candidate"
            )
        if any(
            item.causal_role == "root_candidate"
            or (
                item.output_defect_status == "present"
                and item.counterfactual["predicted_defect_status"] == "absent"
                and item.counterfactual["causal_effect"]
                == "prevents_defect"
            )
            for item in open_assessments
        ):
            raise ValueError(
                "no_root_candidates cannot retain a supported root assessment"
            )
    elif outcome == "no_defect":
        if selected or expansion:
            raise ValueError("no_defect cannot select roots or request expansion")
        if not decisive:
            raise ValueError("no_defect requires decisive evidence")
        if missing:
            raise ValueError("no_defect cannot retain missing evidence")
        open_authored_refs = tuple(
            request.open_authored_root_candidate_refs
        )
        if (
            len(open_assessments) != len(open_authored_refs)
            or {item.candidate_ref for item in open_assessments}
            != open_authored_ref_set
        ):
            raise ValueError(
                "no_defect must assess every open authored root-eligible "
                "candidate exactly once"
            )
        for item in open_assessments:
            if (
                item.defect_status != "absent"
                or item.input_defect_status == "unknown"
                or item.output_defect_status != "absent"
                or item.causal_role == "unknown"
                or not item.causal_path_refs
                or item.counterfactual.get("predicted_defect_status")
                not in {"present", "absent"}
            ):
                raise ValueError(
                    "no_defect requires every open authored root-eligible "
                    "candidate to have known status, absent output, a known "
                    "role, a resolved causal path, and a conclusive "
                    "counterfactual"
                )
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
    elif not missing:
        raise ValueError(
            "inconclusive requires concrete missing evidence"
        )
    elif not incomplete_open_assessments and any(
        item.output_defect_status == "present"
        and item.causal_role == "root_candidate"
        and item.causal_path_refs
        and item.counterfactual["predicted_defect_status"] == "absent"
        for item in open_assessments
    ):
        raise ValueError(
            "inconclusive cannot hide a complete selectable root matrix"
        )
    active_role_bindings = tuple(
        active_failure_role_binding_for_assessment(
            request=request,
            assessment=item,
            selected=item.candidate_ref in selected,
        )
        for item in sorted(
            assessments,
            key=lambda assessment: assessment.candidate_ref,
        )
    )
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
        active_failure_role_bindings=active_role_bindings,
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
        "responsibility",
        "candidate_phase",
        "obligation_status_before",
        "obligation_status_after",
        "repair_window_effect",
        "failure_mode",
        "obligation_refs",
        "contribution_mechanism",
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
        responsibility=str(value.get("responsibility") or ""),
        candidate_phase=str(value.get("candidate_phase") or ""),
        obligation_status_before=str(
            value.get("obligation_status_before") or ""
        ),
        obligation_status_after=str(
            value.get("obligation_status_after") or ""
        ),
        repair_window_effect=str(value.get("repair_window_effect") or ""),
        failure_mode=str(value.get("failure_mode") or ""),
        obligation_refs=_strings(
            value.get("obligation_refs"), "obligation_refs"
        ),
        contribution_mechanism=value.get("contribution_mechanism"),
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
    if intervention_kind not in {
        "replace_with_semantically_correct_behavior",
        "replace_with_obligation_satisfying_behavior",
    }:
        raise ValueError(
            "counterfactual intervention_kind must replace with correct or "
            "obligation-satisfying behavior"
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


def _contribution_mechanism(
    value: Any,
) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("contribution_mechanism must be an object or null")
    required = {"type", "target_ref", "effect", "evidence_refs"}
    actual = {str(key) for key in value}
    if actual != required:
        raise ValueError(
            "contribution mechanism schema mismatch (missing={0}, extra={1})".format(
                sorted(required - actual), sorted(actual - required)
            )
        )
    mechanism_type = str(value.get("type") or "").strip()
    target_ref = str(value.get("target_ref") or "").strip()
    effect = str(value.get("effect") or "").strip()
    if mechanism_type not in CONTRIBUTION_MECHANISM_TYPES:
        raise ValueError("unsupported contribution mechanism type")
    if not target_ref or not effect:
        raise ValueError(
            "contribution mechanism requires target_ref and effect"
        )
    evidence_refs = _immutable_strings(
        value.get("evidence_refs"), "contribution_mechanism.evidence_refs"
    )
    if not evidence_refs:
        raise ValueError(
            "contribution mechanism requires grounded evidence_refs"
        )
    return FrozenMapping(
        {
            "type": mechanism_type,
            "target_ref": target_ref,
            "effect": effect,
            "evidence_refs": evidence_refs,
        }
    )


def _validate_assessment_counterfactual_consistency(
    assessment: GlobalCandidateAssessment,
) -> None:
    counterfactual_prevents = (
        assessment.counterfactual.get("predicted_defect_status") == "absent"
        and assessment.counterfactual.get("causal_effect") == "prevents_defect"
    )
    if (
        assessment.input_defect_status == "unknown"
        and assessment.causal_role
        in {
            "root_candidate",
            "contributing_condition",
            "amplifying_factor",
        }
        and assessment.confidence >= 1.0
    ):
        raise ValueError(
            "candidate {0} with unknown input defect cannot claim certain "
            "confidence; confidence must be set to 0.99".format(
                assessment.candidate_ref
            )
        )
    if assessment.causal_role == "root_candidate":
        if assessment.output_defect_status != "present" or not assessment.causal_path_refs:
            raise ValueError(
                "root_candidate causal role requires present output and a causal path"
            )
        if not counterfactual_prevents:
            raise ValueError(
                "counterfactual contradicts assessment causal role and "
                "defect status"
            )
        if assessment.failure_mode == "positive_introduction":
            if assessment.input_defect_status == "present":
                raise ValueError(
                    "positive introduction requires input defect absent or unknown"
                )
            if (
                assessment.counterfactual.get("intervention_kind")
                != "replace_with_semantically_correct_behavior"
            ):
                raise ValueError(
                    "positive introduction requires semantically correct "
                    "behavior counterfactual"
                )
        elif assessment.failure_mode == "responsible_omission":
            if assessment.input_defect_status != "present":
                raise ValueError(
                    "responsible omission requires a preexisting input defect"
                )
            if assessment.responsibility not in {"primary", "shared"}:
                raise ValueError(
                    "responsible omission requires primary or shared responsibility"
                )
            if not assessment.obligation_refs:
                raise ValueError(
                    "responsible omission requires restoration obligation refs"
                )
            if (
                assessment.obligation_status_before != "pending"
                or assessment.obligation_status_after != "violated"
            ):
                raise ValueError(
                    "responsible omission requires pending-to-violated obligation status"
                )
            if (
                assessment.candidate_phase not in {"closure", "final"}
                or assessment.repair_window_effect != "closed"
            ):
                raise ValueError(
                    "responsible omission requires a closure boundary that "
                    "closes the repair window"
                )
            if (
                assessment.counterfactual.get("intervention_kind")
                != "replace_with_obligation_satisfying_behavior"
            ):
                raise ValueError(
                    "responsible omission requires obligation-satisfying "
                    "counterfactual behavior"
                )
        else:
            raise ValueError(
                "root_candidate requires positive_introduction or "
                "responsible_omission failure mode"
            )
    elif assessment.failure_mode == "responsible_omission":
        raise ValueError(
            "responsible omission failure mode requires root_candidate causal role"
        )
    elif assessment.failure_mode == "positive_introduction":
        raise ValueError(
            "positive introduction failure mode requires root_candidate causal role"
        )
    elif assessment.failure_mode == "ordinary_non_repair" and (
        assessment.causal_role
        in {"contributing_condition", "amplifying_factor"}
    ):
        raise ValueError(
            "ordinary non-repair cannot be a contributing or amplifying factor"
        )
    if assessment.causal_role in {
        "contributing_condition",
        "amplifying_factor",
    }:
        if assessment.contribution_mechanism is None:
            raise ValueError(
                "contributing condition requires a contribution mechanism"
            )
        if assessment.failure_mode not in {
            "omission_enabling_condition",
            "none",
        }:
            raise ValueError(
                "contributing condition has incompatible failure mode"
            )
    elif assessment.contribution_mechanism is not None:
        raise ValueError(
            "contribution mechanism requires a contributing or amplifying role"
        )
    if (
        assessment.failure_mode == "ordinary_non_repair"
        and assessment.repair_window_effect != "remained_open"
    ):
        raise ValueError(
            "ordinary non-repair requires the repair window to remain open"
        )
    if (
        assessment.failure_mode == "omission_enabling_condition"
        and assessment.contribution_mechanism is None
    ):
        raise ValueError(
            "omission enabling condition requires a contribution mechanism"
        )
    if (
        assessment.causal_role
        in {
            "outcome_evidence",
            "exculpatory_evidence",
            "unrelated",
            "unknown",
        }
        and counterfactual_prevents
    ):
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
    return _mapping_has_eligible_causal_path_hops(
        capsule.to_dict(),
        path_refs,
    )


def _mapping_has_eligible_causal_path_hops(
    capsule: Mapping[str, Any],
    path_refs: Tuple[str, ...],
) -> bool:
    edges = (
        *(capsule.get("causal_path_edges") or ()),
        *(capsule.get("outgoing_edges") or ()),
        *(capsule.get("incoming_edges") or ()),
    )
    return all(
        has_confirmation_causal_hop(
            (
                edge
                for edge in edges
                if isinstance(edge, Mapping)
                and str(edge.get("from_ref") or "") == source_ref
                and str(edge.get("to_ref") or "") == target_ref
            ),
            default_eligible=False,
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
        if not isinstance(item, Mapping) or set(item) != {
            "anchor_ref",
            "context_kind",
            "reason",
            "expected_judgment_change",
        }:
            raise ValueError("expansion request schema mismatch")
        anchor = str(item.get("anchor_ref") or "")
        kind = str(item.get("context_kind") or "")
        reason = str(item.get("reason") or "").strip()
        expected_change = str(
            item.get("expected_judgment_change") or ""
        ).strip()
        if anchor not in grounded:
            raise ValueError("expansion request requires a grounded anchor")
        if kind not in EXPANSION_CONTEXT_KINDS:
            raise ValueError("unsupported expansion context kind")
        if not reason:
            raise ValueError("expansion request reason must be non-empty")
        if not expected_change:
            raise ValueError(
                "expansion request expected_judgment_change must be non-empty"
            )
        identity = (anchor, kind)
        if identity in seen:
            raise ValueError("duplicate expansion request")
        seen.add(identity)
        output.append(
            FrozenMapping(
                {
                    "anchor_ref": anchor,
                    "context_kind": kind,
                    "reason": reason,
                    "expected_judgment_change": expected_change,
                }
            )
        )
    return tuple(output)


def _expansion_payload_refs(value: Any) -> Tuple[str, ...]:
    output = []
    seen = set()

    def visit(item: Any) -> None:
        if isinstance(item, str) and item.startswith(
            ("record:", "progress_episode:", "artifact:")
        ):
            if item not in seen:
                seen.add(item)
                output.append(item)
            return
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(output)


__all__ = [
    "GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION",
    "GLOBAL_JUDGE_PROMPT_PROJECTION_SCHEMA",
    "GLOBAL_JUDGE_DIAGNOSTICS_SCHEMA",
    "MAX_ROOT_CONFIRMATION_CANDIDATES",
    "GlobalCandidateAssessment",
    "GlobalCandidateJudgeRequest",
    "GlobalCandidateJudgment",
    "GlobalJudgeCapability",
    "build_global_candidate_prompt",
    "canonicalize_global_candidate_structural_bindings",
    "global_judge_diagnostics",
    "active_focus_text_sha256",
    "global_candidate_comparison_contract",
    "global_candidate_comparison_contract_from_context",
    "global_candidate_judgment_from_payload",
    "normalize_active_focus_text",
    "validate_active_focus_binding",
    "validate_global_candidate_request_against_graph",
    "validate_global_candidate_payload",
    "validate_global_judge_prompt_projection",
    "validate_global_judge_diagnostics",
]
