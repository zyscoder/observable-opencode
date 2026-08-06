"""Strict offline facts describing an agent's restoration obligation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Optional, Tuple

from .models import JsonDict, stable_json


RESTORATION_OBLIGATION_SCHEMA = "restoration-obligation/v1"
RESTORATION_OBLIGATION_IDENTITY_SCHEMA = (
    "restoration-obligation-identity/v1"
)
OFFLINE_JUDGE_VISIBILITY = "offline_judge_only"
OBLIGATION_GAP_CANDIDATE_SCHEMA = "obligation-gap-candidate/v1"
OBLIGATION_GAP_IDENTITY_SCHEMA = "obligation-gap-candidate-identity/v1"
OBLIGATION_GAP_RECONSTRUCTION_RULE = "obligation-gap-reconstruction/v1"

ReferenceResolver = Callable[[str], Optional[str]]

_OBLIGATION_KEYS = frozenset(
    {
        "schema",
        "obligation_id",
        "kind",
        "baseline_state",
        "required_end_state",
        "required_capabilities",
        "scope_refs",
        "acceptance_evidence_refs",
        "provenance",
        "visibility",
        "identity",
    }
)
_PROVENANCE_KEYS = frozenset(
    {"source", "source_refs", "derivation"}
)
_HEX_DIGITS = frozenset("0123456789abcdef")
_EVALUATION_ONLY_FACT_KEY_MARKERS = (
    "answer_key",
    "benchmark_label",
    "benchmark_labels",
    "evaluation",
    "expected_root",
    "expected_roots",
    "expected_answer",
    "ground_truth",
    "human_label",
    "human_labels",
    "reference_answer",
    "review_only",
    "review",
    "reviewer",
    "rubric",
    "score_explanation",
    "scoring",
)
_OBLIGATION_GAP_KEYS = frozenset(
    {
        "schema",
        "case_id",
        "subject_revision",
        "obligation_id",
        "normalized_obligation_text",
        "decision_ref",
        "recognized_evidence_refs",
        "required_coverage",
        "recognized_coverage",
        "planned_coverage",
        "actual_action_coverage",
        "verification_coverage",
        "excluded_coverage",
        "gap_coverage",
        "exclusion_or_closure_rationale",
        "downstream_failure_signature_refs",
        "failure_signature",
        "confirmed_path_provenance",
        "offline_path_provenance",
        "evidence_refs",
        "reconstruction_rule",
        "identity",
    }
)
_OFFLINE_PATH_KEYS = frozenset(
    {
        "from_ref",
        "to_ref",
        "relation",
        "evidence_type",
        "evidence_refs",
        "eligible_for_attribution",
        "confirmed_fact",
        "inference_method",
        "edge_origin",
    }
)


def canonical_fact_key(value: Any) -> str:
    """Canonicalize fact keys before any attribution boundary filtering."""
    text = str(value or "").strip()
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def evaluation_only_fact_key(value: Any) -> bool:
    normalized = canonical_fact_key(value)
    return any(
        normalized == marker
        or normalized.startswith(marker + "_")
        or normalized.endswith("_" + marker)
        for marker in _EVALUATION_ONLY_FACT_KEY_MARKERS
    )


def scrub_evaluation_only_fact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): scrub_evaluation_only_fact_value(item)
            for key, item in value.items()
            if not evaluation_only_fact_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [scrub_evaluation_only_fact_value(item) for item in value]
    return value


def _require_exact_keys(
    value: Any,
    *,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    actual = set(value)
    if actual != expected:
        raise ValueError(
            "{0} schema mismatch (missing={1}, extra={2})".format(
                label,
                sorted(expected - actual),
                sorted(actual - expected),
            )
        )
    return value


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "{0} must be a non-empty string".format(field_name)
        )
    return value.strip()


def _require_identity(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(
            "restoration obligation identity must be a lowercase SHA-256 identity"
        )
    return value


def _string_set(
    value: Any,
    field_name: str,
    *,
    require_json_array: bool = False,
) -> Tuple[str, ...]:
    if require_json_array:
        if not isinstance(value, list):
            raise TypeError(
                "{0} must be a JSON array".format(field_name)
            )
    elif (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TypeError("{0} must be an array".format(field_name))
    normalized = {
        _require_non_empty_string(item, field_name) for item in value
    }
    if not normalized:
        raise ValueError("{0} must not be empty".format(field_name))
    return tuple(sorted(normalized))


def _optional_string_set(
    value: Any,
    field_name: str,
    *,
    require_json_array: bool = False,
) -> Tuple[str, ...]:
    if require_json_array:
        if not isinstance(value, list):
            raise TypeError("{0} must be a JSON array".format(field_name))
    elif (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TypeError("{0} must be an array".format(field_name))
    return tuple(
        sorted(
            {
                _require_non_empty_string(item, field_name)
                for item in value
            }
        )
    )


def normalize_obligation_text(value: Any) -> str:
    text = unicodedata.normalize(
        "NFKC", _require_non_empty_string(value, "obligation text")
    ).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _resolve_reference(
    ref: str,
    field_name: str,
    resolver: ReferenceResolver,
) -> str:
    resolved = resolver(ref)
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError(
            "{0} reference {1!r} could not be resolved".format(
                field_name, ref
            )
        )
    return resolved.strip()


def _reference_set(
    value: Any,
    field_name: str,
    *,
    resolver: Optional[ReferenceResolver],
    require_json_array: bool = False,
    require_canonical: bool = False,
) -> Tuple[str, ...]:
    refs = _string_set(
        value,
        field_name,
        require_json_array=require_json_array,
    )
    if resolver is None:
        canonical = refs
    else:
        if not callable(resolver):
            raise TypeError("reference resolver must be callable")
        canonical = tuple(
            sorted(
                {
                    _resolve_reference(ref, field_name, resolver)
                    for ref in refs
                }
            )
        )
    if require_canonical and refs != canonical:
        raise ValueError(
            "{0} must contain canonical refs".format(field_name)
        )
    return canonical


def _provenance_payload(
    *,
    source: str,
    source_refs: Sequence[str],
    derivation: str,
) -> JsonDict:
    return {
        "source": source,
        "source_refs": list(source_refs),
        "derivation": derivation,
    }


def _normalize_provenance(
    value: Any,
    *,
    resolver: Optional[ReferenceResolver],
    require_json_arrays: bool,
    require_canonical: bool,
) -> Mapping[str, Any]:
    payload = _require_exact_keys(
        value,
        expected=_PROVENANCE_KEYS,
        label="restoration obligation provenance",
    )
    return MappingProxyType(
        {
            "source": _require_non_empty_string(
                payload["source"],
                "restoration obligation provenance source",
            ),
            "source_refs": _reference_set(
                payload["source_refs"],
                "restoration obligation provenance source_refs",
                resolver=resolver,
                require_json_array=require_json_arrays,
                require_canonical=require_canonical,
            ),
            "derivation": _require_non_empty_string(
                payload["derivation"],
                "restoration obligation provenance derivation",
            ),
        }
    )


def _unsigned_payload(
    *,
    obligation_id: str,
    kind: str,
    baseline_state: str,
    required_end_state: str,
    required_capabilities: Sequence[str],
    scope_refs: Sequence[str],
    acceptance_evidence_refs: Sequence[str],
    provenance: Mapping[str, Any],
    visibility: str,
) -> JsonDict:
    return {
        "schema": RESTORATION_OBLIGATION_SCHEMA,
        "obligation_id": obligation_id,
        "kind": kind,
        "baseline_state": baseline_state,
        "required_end_state": required_end_state,
        "required_capabilities": list(required_capabilities),
        "scope_refs": list(scope_refs),
        "acceptance_evidence_refs": list(acceptance_evidence_refs),
        "provenance": _provenance_payload(
            source=provenance["source"],
            source_refs=provenance["source_refs"],
            derivation=provenance["derivation"],
        ),
        "visibility": visibility,
    }


def _identity(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        stable_json(
            {
                "schema": RESTORATION_OBLIGATION_IDENTITY_SCHEMA,
                "facts": payload,
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RestorationObligation:
    obligation_id: str
    kind: str
    baseline_state: str
    required_end_state: str
    required_capabilities: Tuple[str, ...]
    scope_refs: Tuple[str, ...]
    acceptance_evidence_refs: Tuple[str, ...]
    provenance: Mapping[str, Any]
    visibility: str
    identity: str

    def __post_init__(self) -> None:
        obligation_id = _require_non_empty_string(
            self.obligation_id,
            "restoration obligation obligation_id",
        )
        kind = _require_non_empty_string(
            self.kind, "restoration obligation kind"
        )
        baseline_state = _require_non_empty_string(
            self.baseline_state,
            "restoration obligation baseline_state",
        )
        required_end_state = _require_non_empty_string(
            self.required_end_state,
            "restoration obligation required_end_state",
        )
        required_capabilities = _string_set(
            self.required_capabilities,
            "restoration obligation required_capabilities",
        )
        scope_refs = _reference_set(
            self.scope_refs,
            "restoration obligation scope_refs",
            resolver=None,
        )
        acceptance_evidence_refs = _reference_set(
            self.acceptance_evidence_refs,
            "restoration obligation acceptance_evidence_refs",
            resolver=None,
        )
        provenance = _normalize_provenance(
            self.provenance,
            resolver=None,
            require_json_arrays=False,
            require_canonical=False,
        )
        if self.visibility != OFFLINE_JUDGE_VISIBILITY:
            raise ValueError(
                "restoration obligation visibility must be offline_judge_only"
            )
        identity = _require_identity(self.identity)
        expected_identity = _identity(
            _unsigned_payload(
                obligation_id=obligation_id,
                kind=kind,
                baseline_state=baseline_state,
                required_end_state=required_end_state,
                required_capabilities=required_capabilities,
                scope_refs=scope_refs,
                acceptance_evidence_refs=acceptance_evidence_refs,
                provenance=provenance,
                visibility=self.visibility,
            )
        )
        if identity != expected_identity:
            raise ValueError(
                "restoration obligation identity does not match facts"
            )
        object.__setattr__(self, "obligation_id", obligation_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "baseline_state", baseline_state)
        object.__setattr__(
            self, "required_end_state", required_end_state
        )
        object.__setattr__(
            self, "required_capabilities", required_capabilities
        )
        object.__setattr__(self, "scope_refs", scope_refs)
        object.__setattr__(
            self,
            "acceptance_evidence_refs",
            acceptance_evidence_refs,
        )
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "identity", identity)

    @classmethod
    def create(
        cls,
        *,
        obligation_id: str,
        kind: str,
        baseline_state: str,
        required_end_state: str,
        required_capabilities: Sequence[str],
        scope_refs: Sequence[str],
        acceptance_evidence_refs: Sequence[str],
        provenance: Mapping[str, Any],
        resolver: Optional[ReferenceResolver] = None,
    ) -> "RestorationObligation":
        canonical_obligation_id = _require_non_empty_string(
            obligation_id, "restoration obligation obligation_id"
        )
        canonical_kind = _require_non_empty_string(
            kind, "restoration obligation kind"
        )
        canonical_baseline_state = _require_non_empty_string(
            baseline_state, "restoration obligation baseline_state"
        )
        canonical_required_end_state = _require_non_empty_string(
            required_end_state,
            "restoration obligation required_end_state",
        )
        canonical_capabilities = _string_set(
            required_capabilities,
            "restoration obligation required_capabilities",
        )
        canonical_scope_refs = _reference_set(
            scope_refs,
            "restoration obligation scope_refs",
            resolver=resolver,
        )
        canonical_acceptance_refs = _reference_set(
            acceptance_evidence_refs,
            "restoration obligation acceptance_evidence_refs",
            resolver=resolver,
        )
        canonical_provenance = _normalize_provenance(
            provenance,
            resolver=resolver,
            require_json_arrays=False,
            require_canonical=False,
        )
        payload = _unsigned_payload(
            obligation_id=canonical_obligation_id,
            kind=canonical_kind,
            baseline_state=canonical_baseline_state,
            required_end_state=canonical_required_end_state,
            required_capabilities=canonical_capabilities,
            scope_refs=canonical_scope_refs,
            acceptance_evidence_refs=canonical_acceptance_refs,
            provenance=canonical_provenance,
            visibility=OFFLINE_JUDGE_VISIBILITY,
        )
        return cls(
            obligation_id=canonical_obligation_id,
            kind=canonical_kind,
            baseline_state=canonical_baseline_state,
            required_end_state=canonical_required_end_state,
            required_capabilities=canonical_capabilities,
            scope_refs=canonical_scope_refs,
            acceptance_evidence_refs=canonical_acceptance_refs,
            provenance=canonical_provenance,
            visibility=OFFLINE_JUDGE_VISIBILITY,
            identity=_identity(payload),
        )

    def to_dict(self) -> JsonDict:
        payload = _unsigned_payload(
            obligation_id=self.obligation_id,
            kind=self.kind,
            baseline_state=self.baseline_state,
            required_end_state=self.required_end_state,
            required_capabilities=self.required_capabilities,
            scope_refs=self.scope_refs,
            acceptance_evidence_refs=self.acceptance_evidence_refs,
            provenance=self.provenance,
            visibility=self.visibility,
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        resolver: Optional[ReferenceResolver] = None,
    ) -> "RestorationObligation":
        payload = _require_exact_keys(
            value,
            expected=_OBLIGATION_KEYS,
            label="restoration obligation",
        )
        if payload["schema"] != RESTORATION_OBLIGATION_SCHEMA:
            raise ValueError(
                "restoration obligation schema is unsupported"
            )
        required_capabilities = _string_set(
            payload["required_capabilities"],
            "restoration obligation required_capabilities",
            require_json_array=True,
        )
        if list(required_capabilities) != payload["required_capabilities"]:
            raise ValueError(
                "restoration obligation required_capabilities must be canonical"
            )
        scope_refs = _reference_set(
            payload["scope_refs"],
            "restoration obligation scope_refs",
            resolver=resolver,
            require_json_array=True,
            require_canonical=True,
        )
        acceptance_evidence_refs = _reference_set(
            payload["acceptance_evidence_refs"],
            "restoration obligation acceptance_evidence_refs",
            resolver=resolver,
            require_json_array=True,
            require_canonical=True,
        )
        provenance = _normalize_provenance(
            payload["provenance"],
            resolver=resolver,
            require_json_arrays=True,
            require_canonical=True,
        )
        return cls(
            obligation_id=payload["obligation_id"],
            kind=payload["kind"],
            baseline_state=payload["baseline_state"],
            required_end_state=payload["required_end_state"],
            required_capabilities=required_capabilities,
            scope_refs=scope_refs,
            acceptance_evidence_refs=acceptance_evidence_refs,
            provenance=provenance,
            visibility=payload["visibility"],
            identity=payload["identity"],
        )


def _confirmed_paths(
    value: Any,
    *,
    resolver: Optional[ReferenceResolver],
    require_json_arrays: bool,
) -> Tuple[Tuple[str, ...], ...]:
    if require_json_arrays:
        if not isinstance(value, list):
            raise TypeError(
                "obligation gap confirmed_path_provenance must be a JSON array"
            )
    elif (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TypeError(
            "obligation gap confirmed_path_provenance must be an array"
        )
    paths = []
    for raw_path in value:
        if require_json_arrays:
            if not isinstance(raw_path, list):
                raise TypeError("confirmed obligation gap path must be a JSON array")
        elif (
            not isinstance(raw_path, Sequence)
            or isinstance(raw_path, (str, bytes, bytearray))
        ):
            raise TypeError("confirmed obligation gap path must be an array")
        if len(raw_path) < 2:
            raise ValueError("confirmed obligation gap path must contain at least two refs")
        path = tuple(
            _resolve_reference(str(ref), "confirmed path", resolver)
            if resolver is not None
            else _require_non_empty_string(ref, "confirmed path ref")
            for ref in raw_path
        )
        if len(set(path)) != len(path):
            raise ValueError("confirmed obligation gap path must not repeat refs")
        paths.append(path)
    return tuple(sorted(set(paths)))


def _offline_paths(
    value: Any,
    *,
    resolver: Optional[ReferenceResolver],
    require_json_arrays: bool,
) -> Tuple[Mapping[str, Any], ...]:
    if require_json_arrays:
        if not isinstance(value, list):
            raise TypeError(
                "obligation gap offline_path_provenance must be a JSON array"
            )
    elif (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise TypeError("obligation gap offline_path_provenance must be an array")
    output = []
    for raw_edge in value:
        edge = _require_exact_keys(
            raw_edge,
            expected=_OFFLINE_PATH_KEYS,
            label="obligation gap offline path",
        )
        from_ref = _resolve_reference(
            _require_non_empty_string(edge["from_ref"], "offline path from_ref"),
            "offline path from_ref",
            resolver,
        ) if resolver is not None else _require_non_empty_string(
            edge["from_ref"], "offline path from_ref"
        )
        to_ref = _resolve_reference(
            _require_non_empty_string(edge["to_ref"], "offline path to_ref"),
            "offline path to_ref",
            resolver,
        ) if resolver is not None else _require_non_empty_string(
            edge["to_ref"], "offline path to_ref"
        )
        evidence_refs = _reference_set(
            edge["evidence_refs"],
            "offline path evidence_refs",
            resolver=resolver,
            require_json_array=require_json_arrays,
            require_canonical=require_json_arrays,
        )
        if (
            from_ref == to_ref
            or edge["evidence_type"] != "offline_reconstruction"
            or edge["eligible_for_attribution"] is not True
            or edge["confirmed_fact"] is not False
            or edge["inference_method"]
            != OBLIGATION_GAP_RECONSTRUCTION_RULE
            or edge["edge_origin"] != "offline.obligation_gap_reconstruction"
        ):
            raise ValueError(
                "obligation gap offline path must remain attribution-only reconstruction"
            )
        output.append(
            MappingProxyType(
                {
                    "from_ref": from_ref,
                    "to_ref": to_ref,
                    "relation": _require_non_empty_string(
                        edge["relation"], "offline path relation"
                    ),
                    "evidence_type": "offline_reconstruction",
                    "evidence_refs": evidence_refs,
                    "eligible_for_attribution": True,
                    "confirmed_fact": False,
                    "inference_method": OBLIGATION_GAP_RECONSTRUCTION_RULE,
                    "edge_origin": "offline.obligation_gap_reconstruction",
                }
            )
        )
    unique = {
        stable_json(_offline_path_payload(edge)): edge for edge in output
    }
    return tuple(unique[key] for key in sorted(unique))


def _offline_path_payload(edge: Mapping[str, Any]) -> JsonDict:
    return {
        "from_ref": edge["from_ref"],
        "to_ref": edge["to_ref"],
        "relation": edge["relation"],
        "evidence_type": edge["evidence_type"],
        "evidence_refs": list(edge["evidence_refs"]),
        "eligible_for_attribution": edge["eligible_for_attribution"],
        "confirmed_fact": edge["confirmed_fact"],
        "inference_method": edge["inference_method"],
        "edge_origin": edge["edge_origin"],
    }


def _gap_unsigned_payload(
    *,
    case_id: str,
    subject_revision: str,
    obligation_id: str,
    normalized_obligation_text: str,
    decision_ref: str,
    recognized_evidence_refs: Sequence[str],
    required_coverage: Sequence[str],
    recognized_coverage: Sequence[str],
    planned_coverage: Sequence[str],
    actual_action_coverage: Sequence[str],
    verification_coverage: Sequence[str],
    excluded_coverage: Sequence[str],
    gap_coverage: Sequence[str],
    exclusion_or_closure_rationale: str,
    downstream_failure_signature_refs: Sequence[str],
    failure_signature: str,
    confirmed_path_provenance: Sequence[Sequence[str]],
    offline_path_provenance: Sequence[Mapping[str, Any]],
    evidence_refs: Sequence[str],
    reconstruction_rule: str,
) -> JsonDict:
    return {
        "schema": OBLIGATION_GAP_CANDIDATE_SCHEMA,
        "case_id": case_id,
        "subject_revision": subject_revision,
        "obligation_id": obligation_id,
        "normalized_obligation_text": normalized_obligation_text,
        "decision_ref": decision_ref,
        "recognized_evidence_refs": list(recognized_evidence_refs),
        "required_coverage": list(required_coverage),
        "recognized_coverage": list(recognized_coverage),
        "planned_coverage": list(planned_coverage),
        "actual_action_coverage": list(actual_action_coverage),
        "verification_coverage": list(verification_coverage),
        "excluded_coverage": list(excluded_coverage),
        "gap_coverage": list(gap_coverage),
        "exclusion_or_closure_rationale": exclusion_or_closure_rationale,
        "downstream_failure_signature_refs": list(
            downstream_failure_signature_refs
        ),
        "failure_signature": failure_signature,
        "confirmed_path_provenance": [
            list(path) for path in confirmed_path_provenance
        ],
        "offline_path_provenance": [
            _offline_path_payload(edge) for edge in offline_path_provenance
        ],
        "evidence_refs": list(evidence_refs),
        "reconstruction_rule": reconstruction_rule,
    }


def _gap_identity(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        stable_json(
            {
                "schema": OBLIGATION_GAP_IDENTITY_SCHEMA,
                "facts": payload,
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ObligationGapCandidate:
    """Immutable attribution-only reconstruction of an authored obligation gap."""

    case_id: str
    subject_revision: str
    obligation_id: str
    normalized_obligation_text: str
    decision_ref: str
    recognized_evidence_refs: Tuple[str, ...]
    required_coverage: Tuple[str, ...]
    recognized_coverage: Tuple[str, ...]
    planned_coverage: Tuple[str, ...]
    actual_action_coverage: Tuple[str, ...]
    verification_coverage: Tuple[str, ...]
    excluded_coverage: Tuple[str, ...]
    gap_coverage: Tuple[str, ...]
    exclusion_or_closure_rationale: str
    downstream_failure_signature_refs: Tuple[str, ...]
    failure_signature: str
    confirmed_path_provenance: Tuple[Tuple[str, ...], ...]
    offline_path_provenance: Tuple[Mapping[str, Any], ...]
    evidence_refs: Tuple[str, ...]
    reconstruction_rule: str
    identity: str

    def __post_init__(self) -> None:
        case_id = _require_non_empty_string(self.case_id, "obligation gap case_id")
        subject_revision = _require_non_empty_string(
            self.subject_revision, "obligation gap subject_revision"
        )
        obligation_id = _require_non_empty_string(
            self.obligation_id, "obligation gap obligation_id"
        )
        normalized_text = normalize_obligation_text(
            self.normalized_obligation_text
        )
        decision_ref = _require_non_empty_string(
            self.decision_ref, "obligation gap decision_ref"
        )
        recognized_refs = _reference_set(
            self.recognized_evidence_refs,
            "obligation gap recognized_evidence_refs",
            resolver=None,
        )
        required = _string_set(
            self.required_coverage, "obligation gap required_coverage"
        )
        recognized = _optional_string_set(
            self.recognized_coverage, "obligation gap recognized_coverage"
        )
        planned = _optional_string_set(
            self.planned_coverage, "obligation gap planned_coverage"
        )
        acted = _optional_string_set(
            self.actual_action_coverage,
            "obligation gap actual_action_coverage",
        )
        verified = _optional_string_set(
            self.verification_coverage,
            "obligation gap verification_coverage",
        )
        excluded = _optional_string_set(
            self.excluded_coverage, "obligation gap excluded_coverage"
        )
        gap = _string_set(self.gap_coverage, "obligation gap gap_coverage")
        required_set = set(required)
        coverage_sets = (recognized, planned, acted, verified, excluded, gap)
        if any(not set(values) <= required_set for values in coverage_sets):
            raise ValueError("obligation gap coverage exceeds required coverage")
        if excluded != tuple(sorted(required_set - set(planned))):
            raise ValueError("obligation gap excluded coverage is not canonical")
        if gap != tuple(sorted(required_set - (set(acted) & set(verified)))):
            raise ValueError("obligation gap acted/verified difference is not canonical")
        rationale = str(self.exclusion_or_closure_rationale or "").strip()
        failure_refs = _reference_set(
            self.downstream_failure_signature_refs,
            "obligation gap downstream_failure_signature_refs",
            resolver=None,
        )
        failure_signature = _require_identity(self.failure_signature)
        confirmed_paths = _confirmed_paths(
            self.confirmed_path_provenance,
            resolver=None,
            require_json_arrays=False,
        )
        offline_paths = _offline_paths(
            self.offline_path_provenance,
            resolver=None,
            require_json_arrays=False,
        )
        if not confirmed_paths and not offline_paths:
            raise ValueError(
                "obligation gap requires confirmed or offline path provenance"
            )
        evidence_refs = _reference_set(
            self.evidence_refs,
            "obligation gap evidence_refs",
            resolver=None,
        )
        reconstruction_rule = _require_non_empty_string(
            self.reconstruction_rule,
            "obligation gap reconstruction_rule",
        )
        if reconstruction_rule != OBLIGATION_GAP_RECONSTRUCTION_RULE:
            raise ValueError("obligation gap reconstruction rule is unsupported")
        if decision_ref not in evidence_refs or not set(failure_refs) <= set(
            evidence_refs
        ):
            raise ValueError("obligation gap identity refs lack evidence provenance")
        for edge in offline_paths:
            if (
                edge["from_ref"] != decision_ref
                or edge["to_ref"] not in failure_refs
                or not set(edge["evidence_refs"]) <= set(evidence_refs)
            ):
                raise ValueError("obligation gap offline path contradicts candidate refs")
        payload = _gap_unsigned_payload(
            case_id=case_id,
            subject_revision=subject_revision,
            obligation_id=obligation_id,
            normalized_obligation_text=normalized_text,
            decision_ref=decision_ref,
            recognized_evidence_refs=recognized_refs,
            required_coverage=required,
            recognized_coverage=recognized,
            planned_coverage=planned,
            actual_action_coverage=acted,
            verification_coverage=verified,
            excluded_coverage=excluded,
            gap_coverage=gap,
            exclusion_or_closure_rationale=rationale,
            downstream_failure_signature_refs=failure_refs,
            failure_signature=failure_signature,
            confirmed_path_provenance=confirmed_paths,
            offline_path_provenance=offline_paths,
            evidence_refs=evidence_refs,
            reconstruction_rule=reconstruction_rule,
        )
        identity = _require_identity(self.identity)
        if identity != _gap_identity(payload):
            raise ValueError("obligation gap identity does not match facts")
        for name, value in (
            ("case_id", case_id),
            ("subject_revision", subject_revision),
            ("obligation_id", obligation_id),
            ("normalized_obligation_text", normalized_text),
            ("decision_ref", decision_ref),
            ("recognized_evidence_refs", recognized_refs),
            ("required_coverage", required),
            ("recognized_coverage", recognized),
            ("planned_coverage", planned),
            ("actual_action_coverage", acted),
            ("verification_coverage", verified),
            ("excluded_coverage", excluded),
            ("gap_coverage", gap),
            ("exclusion_or_closure_rationale", rationale),
            ("downstream_failure_signature_refs", failure_refs),
            ("failure_signature", failure_signature),
            ("confirmed_path_provenance", confirmed_paths),
            ("offline_path_provenance", offline_paths),
            ("evidence_refs", evidence_refs),
            ("reconstruction_rule", reconstruction_rule),
            ("identity", identity),
        ):
            object.__setattr__(self, name, value)

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        subject_revision: str,
        obligation_id: str,
        obligation_text: str,
        decision_ref: str,
        recognized_evidence_refs: Sequence[str],
        required_coverage: Sequence[str],
        recognized_coverage: Sequence[str],
        planned_coverage: Sequence[str],
        actual_action_coverage: Sequence[str],
        verification_coverage: Sequence[str],
        exclusion_or_closure_rationale: str,
        downstream_failure_signature_refs: Sequence[str],
        failure_signature: str,
        confirmed_path_provenance: Sequence[Sequence[str]],
        offline_path_provenance: Sequence[Mapping[str, Any]],
        evidence_refs: Sequence[str],
        resolver: Optional[ReferenceResolver] = None,
    ) -> "ObligationGapCandidate":
        canonical_required = _string_set(
            required_coverage, "obligation gap required_coverage"
        )
        canonical_planned = _optional_string_set(
            planned_coverage, "obligation gap planned_coverage"
        )
        canonical_acted = _optional_string_set(
            actual_action_coverage,
            "obligation gap actual_action_coverage",
        )
        canonical_verified = _optional_string_set(
            verification_coverage,
            "obligation gap verification_coverage",
        )
        required_set = set(canonical_required)
        excluded = tuple(sorted(required_set - set(canonical_planned)))
        gap = tuple(
            sorted(required_set - (set(canonical_acted) & set(canonical_verified)))
        )
        if not gap:
            raise ValueError("fully acted and verified obligation has no gap")
        values = {
            "case_id": _require_non_empty_string(case_id, "obligation gap case_id"),
            "subject_revision": _require_non_empty_string(
                subject_revision, "obligation gap subject_revision"
            ),
            "obligation_id": _require_non_empty_string(
                obligation_id, "obligation gap obligation_id"
            ),
            "normalized_obligation_text": normalize_obligation_text(
                obligation_text
            ),
            "decision_ref": _resolve_reference(
                decision_ref, "obligation gap decision_ref", resolver
            ) if resolver is not None else _require_non_empty_string(
                decision_ref, "obligation gap decision_ref"
            ),
            "recognized_evidence_refs": _reference_set(
                recognized_evidence_refs,
                "obligation gap recognized_evidence_refs",
                resolver=resolver,
            ),
            "required_coverage": canonical_required,
            "recognized_coverage": _optional_string_set(
                recognized_coverage,
                "obligation gap recognized_coverage",
            ),
            "planned_coverage": canonical_planned,
            "actual_action_coverage": canonical_acted,
            "verification_coverage": canonical_verified,
            "excluded_coverage": excluded,
            "gap_coverage": gap,
            "exclusion_or_closure_rationale": str(
                exclusion_or_closure_rationale or ""
            ).strip(),
            "downstream_failure_signature_refs": _reference_set(
                downstream_failure_signature_refs,
                "obligation gap downstream_failure_signature_refs",
                resolver=resolver,
            ),
            "failure_signature": _require_identity(failure_signature),
            "confirmed_path_provenance": _confirmed_paths(
                confirmed_path_provenance,
                resolver=resolver,
                require_json_arrays=False,
            ),
            "offline_path_provenance": _offline_paths(
                offline_path_provenance,
                resolver=resolver,
                require_json_arrays=False,
            ),
            "evidence_refs": _reference_set(
                evidence_refs,
                "obligation gap evidence_refs",
                resolver=resolver,
            ),
            "reconstruction_rule": OBLIGATION_GAP_RECONSTRUCTION_RULE,
        }
        payload = _gap_unsigned_payload(**values)
        return cls(**values, identity=_gap_identity(payload))

    def to_dict(self) -> JsonDict:
        payload = _gap_unsigned_payload(
            case_id=self.case_id,
            subject_revision=self.subject_revision,
            obligation_id=self.obligation_id,
            normalized_obligation_text=self.normalized_obligation_text,
            decision_ref=self.decision_ref,
            recognized_evidence_refs=self.recognized_evidence_refs,
            required_coverage=self.required_coverage,
            recognized_coverage=self.recognized_coverage,
            planned_coverage=self.planned_coverage,
            actual_action_coverage=self.actual_action_coverage,
            verification_coverage=self.verification_coverage,
            excluded_coverage=self.excluded_coverage,
            gap_coverage=self.gap_coverage,
            exclusion_or_closure_rationale=self.exclusion_or_closure_rationale,
            downstream_failure_signature_refs=(
                self.downstream_failure_signature_refs
            ),
            failure_signature=self.failure_signature,
            confirmed_path_provenance=self.confirmed_path_provenance,
            offline_path_provenance=self.offline_path_provenance,
            evidence_refs=self.evidence_refs,
            reconstruction_rule=self.reconstruction_rule,
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        resolver: Optional[ReferenceResolver] = None,
    ) -> "ObligationGapCandidate":
        payload = _require_exact_keys(
            value,
            expected=_OBLIGATION_GAP_KEYS,
            label="obligation gap candidate",
        )
        if payload["schema"] != OBLIGATION_GAP_CANDIDATE_SCHEMA:
            raise ValueError("obligation gap candidate schema is unsupported")
        created = cls.create(
            case_id=payload["case_id"],
            subject_revision=payload["subject_revision"],
            obligation_id=payload["obligation_id"],
            obligation_text=payload["normalized_obligation_text"],
            decision_ref=payload["decision_ref"],
            recognized_evidence_refs=_reference_set(
                payload["recognized_evidence_refs"],
                "obligation gap recognized_evidence_refs",
                resolver=resolver,
                require_json_array=True,
                require_canonical=True,
            ),
            required_coverage=_string_set(
                payload["required_coverage"],
                "obligation gap required_coverage",
                require_json_array=True,
            ),
            recognized_coverage=_optional_string_set(
                payload["recognized_coverage"],
                "obligation gap recognized_coverage",
                require_json_array=True,
            ),
            planned_coverage=_optional_string_set(
                payload["planned_coverage"],
                "obligation gap planned_coverage",
                require_json_array=True,
            ),
            actual_action_coverage=_optional_string_set(
                payload["actual_action_coverage"],
                "obligation gap actual_action_coverage",
                require_json_array=True,
            ),
            verification_coverage=_optional_string_set(
                payload["verification_coverage"],
                "obligation gap verification_coverage",
                require_json_array=True,
            ),
            exclusion_or_closure_rationale=payload[
                "exclusion_or_closure_rationale"
            ],
            downstream_failure_signature_refs=_reference_set(
                payload["downstream_failure_signature_refs"],
                "obligation gap downstream_failure_signature_refs",
                resolver=resolver,
                require_json_array=True,
                require_canonical=True,
            ),
            failure_signature=payload["failure_signature"],
            confirmed_path_provenance=_confirmed_paths(
                payload["confirmed_path_provenance"],
                resolver=resolver,
                require_json_arrays=True,
            ),
            offline_path_provenance=_offline_paths(
                payload["offline_path_provenance"],
                resolver=resolver,
                require_json_arrays=True,
            ),
            evidence_refs=_reference_set(
                payload["evidence_refs"],
                "obligation gap evidence_refs",
                resolver=resolver,
                require_json_array=True,
                require_canonical=True,
            ),
            resolver=None,
        )
        if created.to_dict() != dict(payload):
            raise ValueError("obligation gap candidate payload is non-canonical")
        return created
