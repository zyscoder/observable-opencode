"""Deterministic Global Judge paging and fact-only survivor projection."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

from .global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
)
from .models import JsonDict, stable_json


CANDIDATE_PAGE_SIZE = 8
DEFAULT_FINALIST_SOFT_LIMIT = 48
GLOBAL_CANDIDATE_MAX_COMPARISON_ROUNDS = 4
GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP = 6
GLOBAL_CANDIDATE_PAGING_POLICY_SCHEMA = "global-candidate-paging-policy/v3"
CANDIDATE_PAGE_SCHEMA = "global-candidate-page/v2"
CANDIDATE_PAGE_PLAN_SCHEMA = "global-candidate-page-plan/v2"
CANDIDATE_PAGE_OUTCOME_SCHEMA = "global-candidate-page-outcome/v1"
CANDIDATE_ROUND_SUMMARY_SCHEMA = "global-candidate-round-summary/v1"

SELECTED_ROOT_HYPOTHESIS = "selected_root_hypothesis"
SUPPORTED_ROOT_HYPOTHESIS = "supported_root_hypothesis"
UNRESOLVED_ROOT_HYPOTHESIS = "unresolved_root_hypothesis"
NON_ROOT_FACTOR = "non_root_factor"
EXCLUDED = "excluded"

SURVIVOR_CLASSIFICATIONS = (
    SELECTED_ROOT_HYPOTHESIS,
    SUPPORTED_ROOT_HYPOTHESIS,
    UNRESOLVED_ROOT_HYPOTHESIS,
    NON_ROOT_FACTOR,
    EXCLUDED,
)


def global_candidate_paging_policy() -> JsonDict:
    return {
        "schema": GLOBAL_CANDIDATE_PAGING_POLICY_SCHEMA,
        "page_size": CANDIDATE_PAGE_SIZE,
        "finalist_soft_limit": DEFAULT_FINALIST_SOFT_LIMIT,
        "max_comparison_rounds": GLOBAL_CANDIDATE_MAX_COMPARISON_ROUNDS,
        "page_physical_request_cap": (
            GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP
        ),
    }
_FACTOR_ROLES = frozenset(
    {"contributing_condition", "amplifying_factor", "outcome_evidence"}
)
_COUNTERFACTUAL_KEYS = frozenset(
    {
        "intervention_ref",
        "intervention_kind",
        "predicted_defect_status",
        "causal_effect",
    }
)
_HEX_DIGITS = frozenset("0123456789abcdef")


def _identity(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _is_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _require_identity(value: Any, field_name: str) -> str:
    if not _is_identity(value):
        raise ValueError("{0} must be a lowercase SHA-256 identity".format(field_name))
    return value


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(field_name))
    return value.strip()


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("{0} must be a non-negative integer".format(field_name))
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("{0} must be a positive integer".format(field_name))
    return value


def _string_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("{0} must be an array".format(field_name))
    output = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                "{0} must contain non-empty strings".format(field_name)
            )
        output.append(item.strip())
    if not allow_empty and not output:
        raise ValueError("{0} must not be empty".format(field_name))
    return tuple(output)


def _identity_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    values = _string_tuple(value, field_name, allow_empty=allow_empty)
    for item in values:
        _require_identity(item, field_name)
    return values


def _json_string_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("{0} must be a JSON array".format(field_name))
    return _string_tuple(value, field_name, allow_empty=allow_empty)


def _json_identity_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    values = _json_string_tuple(
        value, field_name, allow_empty=allow_empty
    )
    for item in values:
        _require_identity(item, field_name)
    return values


def _identity_groups(
    value: Any,
    field_name: str,
    *,
    require_json_arrays: bool = False,
) -> Tuple[Tuple[str, ...], ...]:
    if require_json_arrays:
        if not isinstance(value, list):
            raise TypeError("{0} must be a JSON array".format(field_name))
    elif not isinstance(value, (list, tuple)):
        raise TypeError("{0} must be an array".format(field_name))
    output = []
    for index, group in enumerate(value):
        identities = (
            _json_identity_tuple(
                group,
                "{0}[{1}]".format(field_name, index),
            )
            if require_json_arrays
            else _identity_tuple(
                group,
                "{0}[{1}]".format(field_name, index),
            )
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError(
                "{0}[{1}] must be sorted and unique".format(
                    field_name, index
                )
            )
        output.append(identities)
    return tuple(output)


def _require_exact_keys(
    value: Any,
    *,
    expected: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    actual = {str(key) for key in value}
    if actual != expected:
        raise ValueError(
            "{0} schema mismatch (missing={1}, extra={2})".format(
                label,
                sorted(expected - actual),
                sorted(actual - expected),
            )
        )
    return value


def _require_unique(values: Sequence[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError("{0} must not contain duplicates".format(field_name))


def _candidate_capsule_identity(capsule: Any) -> str:
    candidate_ref = _require_non_empty_string(
        getattr(capsule, "candidate_ref", None),
        "candidate capsule candidate_ref",
    )
    to_dict = getattr(capsule, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("candidate capsule must provide to_dict()")
    payload = to_dict()
    if not isinstance(payload, Mapping):
        raise TypeError("candidate capsule to_dict() must return an object")
    if str(payload.get("candidate_ref") or "").strip() != candidate_ref:
        raise ValueError(
            "candidate capsule payload candidate_ref must match candidate_ref"
        )
    return _identity(
        {
            "schema": "global-candidate-capsule-identity/v1",
            "capsule": payload,
        }
    )


def _candidate_capsule_gap_identities(capsule: Any) -> Tuple[str, ...]:
    to_dict = getattr(capsule, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("candidate capsule must provide to_dict()")
    payload = to_dict()
    candidate = payload.get("candidate") if isinstance(payload, Mapping) else None
    edge = (
        candidate.get("retrieval_edge")
        if isinstance(candidate, Mapping)
        else None
    )
    if not isinstance(edge, Mapping):
        return ()
    raw_gaps = edge.get("attribution_only_obligation_gaps")
    if raw_gaps is None:
        raw_gaps = ()
    if not isinstance(raw_gaps, (list, tuple)):
        raise TypeError(
            "candidate capsule attribution-only obligation gaps must be an array"
        )
    identities = []
    for raw_gap in raw_gaps:
        if not isinstance(raw_gap, Mapping):
            raise TypeError(
                "candidate capsule attribution-only obligation gap must be an object"
            )
        identities.append(
            _require_identity(
                raw_gap.get("identity"),
                "candidate capsule attribution-only gap identity",
            )
        )
    return tuple(sorted(set(identities)))


def _page_input_identity(
    *,
    seed_ref: str,
    defect_fingerprint: str,
    candidate_identities: Sequence[str],
    attribution_only_gap_identities: Sequence[Sequence[str]],
) -> str:
    return _identity(
        {
            "schema": "global-candidate-page-input/v2",
            "seed_ref": seed_ref,
            "defect_fingerprint": defect_fingerprint,
            "candidate_identities": list(candidate_identities),
            "attribution_only_gap_identities": [
                list(values) for values in attribution_only_gap_identities
            ],
        }
    )


def _page_identity_payload(
    *,
    input_identity: str,
    round_index: int,
    page_index: int,
    candidate_refs: Sequence[str],
    candidate_identities: Sequence[str],
    attribution_only_gap_identities: Sequence[Sequence[str]],
) -> JsonDict:
    return {
        "schema": CANDIDATE_PAGE_SCHEMA,
        "input_identity": input_identity,
        "round_index": round_index,
        "page_index": page_index,
        "candidate_refs": list(candidate_refs),
        "candidate_identities": list(candidate_identities),
        "attribution_only_gap_identities": [
            list(values) for values in attribution_only_gap_identities
        ],
    }


@dataclass(frozen=True)
class CandidatePage:
    input_identity: str
    round_index: int
    page_index: int
    candidate_refs: Tuple[str, ...]
    candidate_identities: Tuple[str, ...]
    attribution_only_gap_identities: Tuple[Tuple[str, ...], ...]
    identity: str

    def __post_init__(self) -> None:
        input_identity = _require_identity(
            self.input_identity, "candidate page input_identity"
        )
        round_index = _require_non_negative_int(
            self.round_index, "candidate page round_index"
        )
        page_index = _require_non_negative_int(
            self.page_index, "candidate page page_index"
        )
        candidate_refs = _string_tuple(
            self.candidate_refs,
            "candidate page candidate_refs",
            allow_empty=False,
        )
        candidate_identities = _identity_tuple(
            self.candidate_identities,
            "candidate page candidate_identities",
            allow_empty=False,
        )
        gap_identities = _identity_groups(
            self.attribution_only_gap_identities,
            "candidate page attribution-only gap identities",
        )
        if len(candidate_refs) != len(candidate_identities):
            raise ValueError(
                "candidate page refs and identities must have equal length"
            )
        if len(candidate_refs) != len(gap_identities):
            raise ValueError(
                "candidate page gap identities must align with candidate refs"
            )
        if len(candidate_refs) > CANDIDATE_PAGE_SIZE:
            raise ValueError(
                "candidate page must contain at most {0} candidates".format(
                    CANDIDATE_PAGE_SIZE
                )
            )
        _require_unique(candidate_refs, "candidate page candidate_refs")
        _require_unique(
            candidate_identities, "candidate page candidate_identities"
        )
        expected_identity = _identity(
            _page_identity_payload(
                input_identity=input_identity,
                round_index=round_index,
                page_index=page_index,
                candidate_refs=candidate_refs,
                candidate_identities=candidate_identities,
                attribution_only_gap_identities=gap_identities,
            )
        )
        if self.identity != expected_identity:
            raise ValueError("candidate page identity does not match page facts")
        object.__setattr__(self, "input_identity", input_identity)
        object.__setattr__(self, "round_index", round_index)
        object.__setattr__(self, "page_index", page_index)
        object.__setattr__(self, "candidate_refs", candidate_refs)
        object.__setattr__(
            self, "candidate_identities", candidate_identities
        )
        object.__setattr__(
            self,
            "attribution_only_gap_identities",
            gap_identities,
        )

    @classmethod
    def create(
        cls,
        *,
        input_identity: str,
        round_index: int,
        page_index: int,
        candidate_refs: Sequence[str],
        candidate_identities: Sequence[str],
        attribution_only_gap_identities: Sequence[Sequence[str]],
    ) -> "CandidatePage":
        refs = tuple(candidate_refs)
        identities = tuple(candidate_identities)
        gap_identities = tuple(
            tuple(values) for values in attribution_only_gap_identities
        )
        identity = _identity(
            _page_identity_payload(
                input_identity=input_identity,
                round_index=round_index,
                page_index=page_index,
                candidate_refs=refs,
                candidate_identities=identities,
                attribution_only_gap_identities=gap_identities,
            )
        )
        return cls(
            input_identity=input_identity,
            round_index=round_index,
            page_index=page_index,
            candidate_refs=refs,
            candidate_identities=identities,
            attribution_only_gap_identities=gap_identities,
            identity=identity,
        )

    def to_dict(self) -> JsonDict:
        payload = _page_identity_payload(
            input_identity=self.input_identity,
            round_index=self.round_index,
            page_index=self.page_index,
            candidate_refs=self.candidate_refs,
            candidate_identities=self.candidate_identities,
            attribution_only_gap_identities=(
                self.attribution_only_gap_identities
            ),
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidatePage":
        payload = _require_exact_keys(
            value,
            expected=frozenset(
                {
                    "schema",
                    "input_identity",
                    "round_index",
                    "page_index",
                    "candidate_refs",
                    "candidate_identities",
                    "attribution_only_gap_identities",
                    "identity",
                }
            ),
            label="candidate page",
        )
        if payload["schema"] != CANDIDATE_PAGE_SCHEMA:
            raise ValueError("candidate page schema is unsupported")
        return cls(
            input_identity=payload["input_identity"],
            round_index=payload["round_index"],
            page_index=payload["page_index"],
            candidate_refs=_json_string_tuple(
                payload["candidate_refs"], "candidate page candidate_refs"
            ),
            candidate_identities=_json_identity_tuple(
                payload["candidate_identities"],
                "candidate page candidate_identities",
            ),
            attribution_only_gap_identities=_identity_groups(
                payload["attribution_only_gap_identities"],
                "candidate page attribution-only gap identities",
                require_json_arrays=True,
            ),
            identity=_require_identity(
                payload["identity"], "candidate page identity"
            ),
        )


def _plan_unsigned_payload(
    *,
    seed_ref: str,
    defect_fingerprint: str,
    round_index: int,
    page_size: int,
    candidate_refs: Sequence[str],
    candidate_identities: Sequence[str],
    attribution_only_gap_identities: Sequence[Sequence[str]],
    input_identity: str,
    pages: Sequence[CandidatePage],
) -> JsonDict:
    return {
        "schema": CANDIDATE_PAGE_PLAN_SCHEMA,
        "seed_ref": seed_ref,
        "defect_fingerprint": defect_fingerprint,
        "round_index": round_index,
        "page_size": page_size,
        "candidate_refs": list(candidate_refs),
        "candidate_identities": list(candidate_identities),
        "attribution_only_gap_identities": [
            list(values) for values in attribution_only_gap_identities
        ],
        "input_identity": input_identity,
        "pages": [page.to_dict() for page in pages],
    }


@dataclass(frozen=True)
class CandidatePagePlan:
    seed_ref: str
    defect_fingerprint: str
    round_index: int
    page_size: int
    candidate_refs: Tuple[str, ...]
    candidate_identities: Tuple[str, ...]
    attribution_only_gap_identities: Tuple[Tuple[str, ...], ...]
    input_identity: str
    pages: Tuple[CandidatePage, ...]
    identity: str

    def __post_init__(self) -> None:
        seed_ref = _require_non_empty_string(
            self.seed_ref, "candidate page plan seed_ref"
        )
        defect_fingerprint = _require_non_empty_string(
            self.defect_fingerprint,
            "candidate page plan defect_fingerprint",
        )
        round_index = _require_non_negative_int(
            self.round_index, "candidate page plan round_index"
        )
        if self.page_size != CANDIDATE_PAGE_SIZE:
            raise ValueError(
                "candidate page plan page_size must equal {0}".format(
                    CANDIDATE_PAGE_SIZE
                )
            )
        candidate_refs = _string_tuple(
            self.candidate_refs, "candidate page plan candidate_refs"
        )
        candidate_identities = _identity_tuple(
            self.candidate_identities,
            "candidate page plan candidate_identities",
        )
        gap_identities = _identity_groups(
            self.attribution_only_gap_identities,
            "candidate page plan attribution-only gap identities",
        )
        if len(candidate_refs) != len(candidate_identities):
            raise ValueError(
                "candidate page plan refs and identities must have equal length"
            )
        if len(candidate_refs) != len(gap_identities):
            raise ValueError(
                "candidate page plan gap identities must align with refs"
            )
        _require_unique(candidate_refs, "candidate page plan candidate_refs")
        _require_unique(
            candidate_identities, "candidate page plan candidate_identities"
        )
        input_identity = _require_identity(
            self.input_identity, "candidate page plan input_identity"
        )
        expected_input_identity = _page_input_identity(
            seed_ref=seed_ref,
            defect_fingerprint=defect_fingerprint,
            candidate_identities=candidate_identities,
            attribution_only_gap_identities=gap_identities,
        )
        if input_identity != expected_input_identity:
            raise ValueError(
                "candidate page plan input_identity does not match input facts"
            )
        pages = tuple(self.pages)
        if any(not isinstance(page, CandidatePage) for page in pages):
            raise TypeError("candidate page plan pages must be CandidatePage values")
        expected_page_count = (
            (len(candidate_refs) + CANDIDATE_PAGE_SIZE - 1)
            // CANDIDATE_PAGE_SIZE
        )
        if len(pages) != expected_page_count:
            raise ValueError(
                "candidate page plan pages must cover the candidate set exactly once"
            )
        for page_index, page in enumerate(pages):
            start = page_index * CANDIDATE_PAGE_SIZE
            end = start + CANDIDATE_PAGE_SIZE
            if (
                page.input_identity != input_identity
                or page.round_index != round_index
                or page.page_index != page_index
                or page.candidate_refs != candidate_refs[start:end]
                or page.candidate_identities != candidate_identities[start:end]
                or page.attribution_only_gap_identities
                != gap_identities[start:end]
            ):
                raise ValueError(
                    "candidate page plan page order or membership does not match"
                )
        flattened_refs = tuple(
            ref for page in pages for ref in page.candidate_refs
        )
        flattened_identities = tuple(
            identity
            for page in pages
            for identity in page.candidate_identities
        )
        if (
            flattened_refs != candidate_refs
            or flattened_identities != candidate_identities
            or len(flattened_refs) != len(set(flattened_refs))
            or len(flattened_identities) != len(set(flattened_identities))
        ):
            raise ValueError(
                "candidate page plan contains duplicate or omitted membership"
            )
        unsigned = _plan_unsigned_payload(
            seed_ref=seed_ref,
            defect_fingerprint=defect_fingerprint,
            round_index=round_index,
            page_size=CANDIDATE_PAGE_SIZE,
            candidate_refs=candidate_refs,
            candidate_identities=candidate_identities,
            attribution_only_gap_identities=gap_identities,
            input_identity=input_identity,
            pages=pages,
        )
        if self.identity != _identity(unsigned):
            raise ValueError(
                "candidate page plan identity does not match plan facts"
            )
        object.__setattr__(self, "seed_ref", seed_ref)
        object.__setattr__(
            self, "defect_fingerprint", defect_fingerprint
        )
        object.__setattr__(self, "round_index", round_index)
        object.__setattr__(self, "candidate_refs", candidate_refs)
        object.__setattr__(
            self, "candidate_identities", candidate_identities
        )
        object.__setattr__(
            self,
            "attribution_only_gap_identities",
            gap_identities,
        )
        object.__setattr__(self, "input_identity", input_identity)
        object.__setattr__(self, "pages", pages)

    def to_dict(self) -> JsonDict:
        payload = _plan_unsigned_payload(
            seed_ref=self.seed_ref,
            defect_fingerprint=self.defect_fingerprint,
            round_index=self.round_index,
            page_size=self.page_size,
            candidate_refs=self.candidate_refs,
            candidate_identities=self.candidate_identities,
            attribution_only_gap_identities=(
                self.attribution_only_gap_identities
            ),
            input_identity=self.input_identity,
            pages=self.pages,
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidatePagePlan":
        payload = _require_exact_keys(
            value,
            expected=frozenset(
                {
                    "schema",
                    "seed_ref",
                    "defect_fingerprint",
                    "round_index",
                    "page_size",
                    "candidate_refs",
                    "candidate_identities",
                    "attribution_only_gap_identities",
                    "input_identity",
                    "pages",
                    "identity",
                }
            ),
            label="candidate page plan",
        )
        if payload["schema"] != CANDIDATE_PAGE_PLAN_SCHEMA:
            raise ValueError("candidate page plan schema is unsupported")
        raw_pages = payload["pages"]
        if not isinstance(raw_pages, list):
            raise TypeError("candidate page plan pages must be an array")
        return cls(
            seed_ref=payload["seed_ref"],
            defect_fingerprint=payload["defect_fingerprint"],
            round_index=payload["round_index"],
            page_size=payload["page_size"],
            candidate_refs=_json_string_tuple(
                payload["candidate_refs"],
                "candidate page plan candidate_refs",
            ),
            candidate_identities=_json_identity_tuple(
                payload["candidate_identities"],
                "candidate page plan candidate_identities",
            ),
            attribution_only_gap_identities=_identity_groups(
                payload["attribution_only_gap_identities"],
                "candidate page plan attribution-only gap identities",
                require_json_arrays=True,
            ),
            input_identity=_require_identity(
                payload["input_identity"],
                "candidate page plan input_identity",
            ),
            pages=tuple(CandidatePage.from_dict(page) for page in raw_pages),
            identity=_require_identity(
                payload["identity"], "candidate page plan identity"
            ),
        )


def build_candidate_page_plan(
    *,
    seed_ref: str,
    defect_fingerprint: str,
    capsules: Sequence[Any],
    round_index: int,
) -> CandidatePagePlan:
    """Project ordered complete capsules into fixed-size deterministic pages."""
    canonical_seed_ref = _require_non_empty_string(
        seed_ref, "candidate page plan seed_ref"
    )
    canonical_fingerprint = _require_non_empty_string(
        defect_fingerprint, "candidate page plan defect_fingerprint"
    )
    canonical_round_index = _require_non_negative_int(
        round_index, "candidate page plan round_index"
    )
    if isinstance(capsules, (str, bytes)) or not isinstance(capsules, Sequence):
        raise TypeError("candidate page plan capsules must be an ordered sequence")
    candidate_refs = tuple(
        _require_non_empty_string(
            getattr(capsule, "candidate_ref", None),
            "candidate capsule candidate_ref",
        )
        for capsule in capsules
    )
    if len(candidate_refs) != len(set(candidate_refs)):
        raise ValueError(
            "candidate page plan capsules contain a duplicate candidate_ref"
        )
    candidate_identities = tuple(
        _candidate_capsule_identity(capsule) for capsule in capsules
    )
    gap_identities = tuple(
        _candidate_capsule_gap_identities(capsule)
        for capsule in capsules
    )
    if len(candidate_identities) != len(set(candidate_identities)):
        raise ValueError(
            "candidate page plan capsules contain a duplicate capsule identity"
        )
    input_identity = _page_input_identity(
        seed_ref=canonical_seed_ref,
        defect_fingerprint=canonical_fingerprint,
        candidate_identities=candidate_identities,
        attribution_only_gap_identities=gap_identities,
    )
    pages = tuple(
        CandidatePage.create(
            input_identity=input_identity,
            round_index=canonical_round_index,
            page_index=page_index,
            candidate_refs=candidate_refs[start : start + CANDIDATE_PAGE_SIZE],
            candidate_identities=candidate_identities[
                start : start + CANDIDATE_PAGE_SIZE
            ],
            attribution_only_gap_identities=gap_identities[
                start : start + CANDIDATE_PAGE_SIZE
            ],
        )
        for page_index, start in enumerate(
            range(0, len(candidate_refs), CANDIDATE_PAGE_SIZE)
        )
    )
    unsigned = _plan_unsigned_payload(
        seed_ref=canonical_seed_ref,
        defect_fingerprint=canonical_fingerprint,
        round_index=canonical_round_index,
        page_size=CANDIDATE_PAGE_SIZE,
        candidate_refs=candidate_refs,
        candidate_identities=candidate_identities,
        attribution_only_gap_identities=gap_identities,
        input_identity=input_identity,
        pages=pages,
    )
    return CandidatePagePlan(
        seed_ref=canonical_seed_ref,
        defect_fingerprint=canonical_fingerprint,
        round_index=canonical_round_index,
        page_size=CANDIDATE_PAGE_SIZE,
        candidate_refs=candidate_refs,
        candidate_identities=candidate_identities,
        attribution_only_gap_identities=gap_identities,
        input_identity=input_identity,
        pages=pages,
        identity=_identity(unsigned),
    )


def _outcome_unsigned_payload(
    *,
    page_identity: str,
    round_index: int,
    page_index: int,
    candidate_refs: Sequence[str],
    selected_root_hypothesis_refs: Sequence[str],
    supported_root_hypothesis_refs: Sequence[str],
    unresolved_root_hypothesis_refs: Sequence[str],
    non_root_factor_refs: Sequence[str],
    excluded_refs: Sequence[str],
    judgment_identity: str,
) -> JsonDict:
    return {
        "schema": CANDIDATE_PAGE_OUTCOME_SCHEMA,
        "page_identity": page_identity,
        "round_index": round_index,
        "page_index": page_index,
        "candidate_refs": list(candidate_refs),
        "selected_root_hypothesis_refs": list(
            selected_root_hypothesis_refs
        ),
        "supported_root_hypothesis_refs": list(
            supported_root_hypothesis_refs
        ),
        "unresolved_root_hypothesis_refs": list(
            unresolved_root_hypothesis_refs
        ),
        "non_root_factor_refs": list(non_root_factor_refs),
        "excluded_refs": list(excluded_refs),
        "judgment_identity": judgment_identity,
    }


@dataclass(frozen=True)
class CandidatePageOutcome:
    page_identity: str
    round_index: int
    page_index: int
    candidate_refs: Tuple[str, ...]
    selected_root_hypothesis_refs: Tuple[str, ...]
    supported_root_hypothesis_refs: Tuple[str, ...]
    unresolved_root_hypothesis_refs: Tuple[str, ...]
    non_root_factor_refs: Tuple[str, ...]
    excluded_refs: Tuple[str, ...]
    judgment_identity: str
    identity: str

    def __post_init__(self) -> None:
        page_identity = _require_identity(
            self.page_identity, "candidate page outcome page_identity"
        )
        judgment_identity = _require_identity(
            self.judgment_identity,
            "candidate page outcome judgment_identity",
        )
        round_index = _require_non_negative_int(
            self.round_index, "candidate page outcome round_index"
        )
        page_index = _require_non_negative_int(
            self.page_index, "candidate page outcome page_index"
        )
        candidate_refs = _string_tuple(
            self.candidate_refs,
            "candidate page outcome candidate_refs",
            allow_empty=False,
        )
        _require_unique(
            candidate_refs, "candidate page outcome candidate_refs"
        )
        classifications = (
            (
                SELECTED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.selected_root_hypothesis_refs,
                    "selected_root_hypothesis_refs",
                ),
            ),
            (
                SUPPORTED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.supported_root_hypothesis_refs,
                    "supported_root_hypothesis_refs",
                ),
            ),
            (
                UNRESOLVED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.unresolved_root_hypothesis_refs,
                    "unresolved_root_hypothesis_refs",
                ),
            ),
            (
                NON_ROOT_FACTOR,
                _string_tuple(
                    self.non_root_factor_refs, "non_root_factor_refs"
                ),
            ),
            (
                EXCLUDED,
                _string_tuple(self.excluded_refs, "excluded_refs"),
            ),
        )
        category_by_ref: Dict[str, str] = {}
        page_position = {
            candidate_ref: index
            for index, candidate_ref in enumerate(candidate_refs)
        }
        for category, refs in classifications:
            _require_unique(refs, "{0} refs".format(category))
            if tuple(sorted(refs, key=page_position.get)) != refs:
                raise ValueError(
                    "{0} refs must preserve candidate page order".format(
                        category
                    )
                )
            for candidate_ref in refs:
                if candidate_ref not in page_position:
                    raise ValueError(
                        "candidate page outcome classification contains a foreign ref"
                    )
                if candidate_ref in category_by_ref:
                    raise ValueError(
                        "candidate page outcome classifications must be disjoint"
                    )
                category_by_ref[candidate_ref] = category
        if set(category_by_ref) != set(candidate_refs):
            raise ValueError(
                "candidate page outcome must classify every candidate exactly once"
            )
        unsigned = _outcome_unsigned_payload(
            page_identity=page_identity,
            round_index=round_index,
            page_index=page_index,
            candidate_refs=candidate_refs,
            selected_root_hypothesis_refs=classifications[0][1],
            supported_root_hypothesis_refs=classifications[1][1],
            unresolved_root_hypothesis_refs=classifications[2][1],
            non_root_factor_refs=classifications[3][1],
            excluded_refs=classifications[4][1],
            judgment_identity=judgment_identity,
        )
        if self.identity != _identity(unsigned):
            raise ValueError(
                "candidate page outcome identity does not match outcome facts"
            )
        object.__setattr__(self, "page_identity", page_identity)
        object.__setattr__(self, "round_index", round_index)
        object.__setattr__(self, "page_index", page_index)
        object.__setattr__(self, "candidate_refs", candidate_refs)
        object.__setattr__(
            self, "selected_root_hypothesis_refs", classifications[0][1]
        )
        object.__setattr__(
            self, "supported_root_hypothesis_refs", classifications[1][1]
        )
        object.__setattr__(
            self,
            "unresolved_root_hypothesis_refs",
            classifications[2][1],
        )
        object.__setattr__(
            self, "non_root_factor_refs", classifications[3][1]
        )
        object.__setattr__(self, "excluded_refs", classifications[4][1])
        object.__setattr__(self, "judgment_identity", judgment_identity)

    @property
    def classification_by_ref(self) -> Dict[str, str]:
        categories = (
            (
                self.selected_root_hypothesis_refs,
                SELECTED_ROOT_HYPOTHESIS,
            ),
            (
                self.supported_root_hypothesis_refs,
                SUPPORTED_ROOT_HYPOTHESIS,
            ),
            (
                self.unresolved_root_hypothesis_refs,
                UNRESOLVED_ROOT_HYPOTHESIS,
            ),
            (self.non_root_factor_refs, NON_ROOT_FACTOR),
            (self.excluded_refs, EXCLUDED),
        )
        by_ref = {
            candidate_ref: category
            for refs, category in categories
            for candidate_ref in refs
        }
        return {
            candidate_ref: by_ref[candidate_ref]
            for candidate_ref in self.candidate_refs
        }

    @property
    def classified_candidate_refs(self) -> Tuple[str, ...]:
        return tuple(self.classification_by_ref)

    def to_dict(self) -> JsonDict:
        payload = _outcome_unsigned_payload(
            page_identity=self.page_identity,
            round_index=self.round_index,
            page_index=self.page_index,
            candidate_refs=self.candidate_refs,
            selected_root_hypothesis_refs=self.selected_root_hypothesis_refs,
            supported_root_hypothesis_refs=self.supported_root_hypothesis_refs,
            unresolved_root_hypothesis_refs=self.unresolved_root_hypothesis_refs,
            non_root_factor_refs=self.non_root_factor_refs,
            excluded_refs=self.excluded_refs,
            judgment_identity=self.judgment_identity,
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidatePageOutcome":
        payload = _require_exact_keys(
            value,
            expected=frozenset(
                {
                    "schema",
                    "page_identity",
                    "round_index",
                    "page_index",
                    "candidate_refs",
                    "selected_root_hypothesis_refs",
                    "supported_root_hypothesis_refs",
                    "unresolved_root_hypothesis_refs",
                    "non_root_factor_refs",
                    "excluded_refs",
                    "judgment_identity",
                    "identity",
                }
            ),
            label="candidate page outcome",
        )
        if payload["schema"] != CANDIDATE_PAGE_OUTCOME_SCHEMA:
            raise ValueError("candidate page outcome schema is unsupported")
        return cls(
            page_identity=payload["page_identity"],
            round_index=payload["round_index"],
            page_index=payload["page_index"],
            candidate_refs=_json_string_tuple(
                payload["candidate_refs"],
                "candidate page outcome candidate_refs",
            ),
            selected_root_hypothesis_refs=_json_string_tuple(
                payload["selected_root_hypothesis_refs"],
                "selected_root_hypothesis_refs",
            ),
            supported_root_hypothesis_refs=_json_string_tuple(
                payload["supported_root_hypothesis_refs"],
                "supported_root_hypothesis_refs",
            ),
            unresolved_root_hypothesis_refs=_json_string_tuple(
                payload["unresolved_root_hypothesis_refs"],
                "unresolved_root_hypothesis_refs",
            ),
            non_root_factor_refs=_json_string_tuple(
                payload["non_root_factor_refs"], "non_root_factor_refs"
            ),
            excluded_refs=_json_string_tuple(
                payload["excluded_refs"], "excluded_refs"
            ),
            judgment_identity=payload["judgment_identity"],
            identity=payload["identity"],
        )


def _counterfactual_is_complete(
    assessment: GlobalCandidateAssessment,
) -> bool:
    counterfactual = assessment.counterfactual
    if not isinstance(counterfactual, Mapping):
        return False
    if {str(key) for key in counterfactual} != _COUNTERFACTUAL_KEYS:
        return False
    predicted = counterfactual.get("predicted_defect_status")
    effect = counterfactual.get("causal_effect")
    return (
        predicted == "absent"
        and effect == "prevents_defect"
    ) or (
        predicted == "present"
        and effect == "does_not_prevent_defect"
    )


def _assessment_supports_root(
    assessment: GlobalCandidateAssessment,
) -> bool:
    return (
        assessment.causal_role == "root_candidate"
        and assessment.output_defect_status == "present"
        and assessment.counterfactual.get("predicted_defect_status")
        == "absent"
        and assessment.counterfactual.get("causal_effect")
        == "prevents_defect"
    )


def _assessment_is_incomplete(
    assessment: GlobalCandidateAssessment,
) -> bool:
    return (
        assessment.defect_status == "unknown"
        or assessment.input_defect_status == "unknown"
        or assessment.output_defect_status == "unknown"
        or assessment.causal_role == "unknown"
        or not assessment.causal_path_refs
        or not _counterfactual_is_complete(assessment)
    )


def build_candidate_page_outcome(
    *,
    page: CandidatePage,
    judgment: GlobalCandidateJudgment,
    root_eligible_candidate_refs: Iterable[str],
) -> CandidatePageOutcome:
    """Classify canonical assessment facts without interpreting reason text."""
    if not isinstance(page, CandidatePage):
        raise TypeError("page must be a CandidatePage")
    if not isinstance(judgment, GlobalCandidateJudgment):
        raise TypeError("judgment must be a GlobalCandidateJudgment")
    assessments = tuple(judgment.assessments)
    if any(
        not isinstance(item, GlobalCandidateAssessment)
        for item in assessments
    ):
        raise TypeError(
            "judgment assessments must be GlobalCandidateAssessment values"
        )
    assessment_refs = tuple(item.candidate_ref for item in assessments)
    if (
        len(assessment_refs) != len(set(assessment_refs))
        or set(assessment_refs) != set(page.candidate_refs)
    ):
        raise ValueError(
            "canonical judgment must assess every page candidate exactly once"
        )
    selected_refs = set(judgment.selected_candidate_refs)
    if not selected_refs.issubset(set(page.candidate_refs)):
        raise ValueError(
            "canonical judgment selected refs must belong to the candidate page"
        )
    eligible_refs = _string_tuple(
        tuple(root_eligible_candidate_refs),
        "root-eligible candidate refs",
    )
    _require_unique(eligible_refs, "root-eligible candidate refs")
    if not set(eligible_refs).issubset(set(page.candidate_refs)):
        raise ValueError(
            "root-eligible candidate refs must belong to the candidate page"
        )
    eligible_set = set(eligible_refs)
    assessment_by_ref = {
        item.candidate_ref: item for item in assessments
    }
    classified: Dict[str, list[str]] = {
        category: [] for category in SURVIVOR_CLASSIFICATIONS
    }
    for candidate_ref in page.candidate_refs:
        assessment = assessment_by_ref[candidate_ref]
        if candidate_ref in selected_refs:
            category = SELECTED_ROOT_HYPOTHESIS
        elif _assessment_supports_root(assessment):
            category = SUPPORTED_ROOT_HYPOTHESIS
        elif (
            candidate_ref in eligible_set
            and _assessment_is_incomplete(assessment)
        ):
            category = UNRESOLVED_ROOT_HYPOTHESIS
        elif assessment.causal_role in _FACTOR_ROLES:
            category = NON_ROOT_FACTOR
        else:
            category = EXCLUDED
        classified[category].append(candidate_ref)
    judgment_identity = _identity(
        {
            "schema": "global-candidate-judgment-identity/v1",
            "judgment": judgment.to_dict(),
        }
    )
    unsigned = _outcome_unsigned_payload(
        page_identity=page.identity,
        round_index=page.round_index,
        page_index=page.page_index,
        candidate_refs=page.candidate_refs,
        selected_root_hypothesis_refs=classified[
            SELECTED_ROOT_HYPOTHESIS
        ],
        supported_root_hypothesis_refs=classified[
            SUPPORTED_ROOT_HYPOTHESIS
        ],
        unresolved_root_hypothesis_refs=classified[
            UNRESOLVED_ROOT_HYPOTHESIS
        ],
        non_root_factor_refs=classified[NON_ROOT_FACTOR],
        excluded_refs=classified[EXCLUDED],
        judgment_identity=judgment_identity,
    )
    return CandidatePageOutcome(
        page_identity=page.identity,
        round_index=page.round_index,
        page_index=page.page_index,
        candidate_refs=page.candidate_refs,
        selected_root_hypothesis_refs=tuple(
            classified[SELECTED_ROOT_HYPOTHESIS]
        ),
        supported_root_hypothesis_refs=tuple(
            classified[SUPPORTED_ROOT_HYPOTHESIS]
        ),
        unresolved_root_hypothesis_refs=tuple(
            classified[UNRESOLVED_ROOT_HYPOTHESIS]
        ),
        non_root_factor_refs=tuple(classified[NON_ROOT_FACTOR]),
        excluded_refs=tuple(classified[EXCLUDED]),
        judgment_identity=judgment_identity,
        identity=_identity(unsigned),
    )


def _round_unsigned_payload(
    *,
    round_index: int,
    page_outcome_identities: Sequence[str],
    candidate_refs: Sequence[str],
    selected_root_hypothesis_refs: Sequence[str],
    supported_root_hypothesis_refs: Sequence[str],
    unresolved_root_hypothesis_refs: Sequence[str],
    non_root_factor_refs: Sequence[str],
    excluded_refs: Sequence[str],
    finalist_candidate_refs: Sequence[str],
    finalist_soft_limit: int,
    requires_comparison_round: bool,
) -> JsonDict:
    return {
        "schema": CANDIDATE_ROUND_SUMMARY_SCHEMA,
        "round_index": round_index,
        "page_outcome_identities": list(page_outcome_identities),
        "candidate_refs": list(candidate_refs),
        "selected_root_hypothesis_refs": list(
            selected_root_hypothesis_refs
        ),
        "supported_root_hypothesis_refs": list(
            supported_root_hypothesis_refs
        ),
        "unresolved_root_hypothesis_refs": list(
            unresolved_root_hypothesis_refs
        ),
        "non_root_factor_refs": list(non_root_factor_refs),
        "excluded_refs": list(excluded_refs),
        "finalist_candidate_refs": list(finalist_candidate_refs),
        "finalist_soft_limit": finalist_soft_limit,
        "requires_comparison_round": requires_comparison_round,
    }


@dataclass(frozen=True)
class CandidateRoundSummary:
    round_index: int
    page_outcome_identities: Tuple[str, ...]
    candidate_refs: Tuple[str, ...]
    selected_root_hypothesis_refs: Tuple[str, ...]
    supported_root_hypothesis_refs: Tuple[str, ...]
    unresolved_root_hypothesis_refs: Tuple[str, ...]
    non_root_factor_refs: Tuple[str, ...]
    excluded_refs: Tuple[str, ...]
    finalist_candidate_refs: Tuple[str, ...]
    finalist_soft_limit: int
    requires_comparison_round: bool
    identity: str

    def __post_init__(self) -> None:
        round_index = _require_non_negative_int(
            self.round_index, "candidate round summary round_index"
        )
        page_outcome_identities = _identity_tuple(
            self.page_outcome_identities,
            "candidate round summary page_outcome_identities",
        )
        _require_unique(
            page_outcome_identities,
            "candidate round summary page_outcome_identities",
        )
        candidate_refs = _string_tuple(
            self.candidate_refs, "candidate round summary candidate_refs"
        )
        _require_unique(
            candidate_refs, "candidate round summary candidate_refs"
        )
        category_fields = (
            (
                SELECTED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.selected_root_hypothesis_refs,
                    "selected_root_hypothesis_refs",
                ),
            ),
            (
                SUPPORTED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.supported_root_hypothesis_refs,
                    "supported_root_hypothesis_refs",
                ),
            ),
            (
                UNRESOLVED_ROOT_HYPOTHESIS,
                _string_tuple(
                    self.unresolved_root_hypothesis_refs,
                    "unresolved_root_hypothesis_refs",
                ),
            ),
            (
                NON_ROOT_FACTOR,
                _string_tuple(
                    self.non_root_factor_refs, "non_root_factor_refs"
                ),
            ),
            (
                EXCLUDED,
                _string_tuple(self.excluded_refs, "excluded_refs"),
            ),
        )
        classified_refs = [
            candidate_ref
            for _, refs in category_fields
            for candidate_ref in refs
        ]
        if (
            len(classified_refs) != len(set(classified_refs))
            or set(classified_refs) != set(candidate_refs)
        ):
            raise ValueError(
                "candidate round summary must classify every candidate exactly once"
            )
        candidate_position = {
            candidate_ref: index
            for index, candidate_ref in enumerate(candidate_refs)
        }
        for category, refs in category_fields:
            if tuple(sorted(refs, key=candidate_position.get)) != refs:
                raise ValueError(
                    "{0} refs must preserve round candidate order".format(
                        category
                    )
                )
        finalist_candidate_refs = _string_tuple(
            self.finalist_candidate_refs,
            "candidate round summary finalist_candidate_refs",
        )
        expected_finalists = tuple(
            candidate_ref
            for candidate_ref in candidate_refs
            if candidate_ref
            in set(category_fields[0][1]) | set(category_fields[1][1])
        )
        if finalist_candidate_refs != expected_finalists:
            raise ValueError(
                "candidate round finalists must be selected or supported roots"
            )
        finalist_soft_limit = _require_positive_int(
            self.finalist_soft_limit,
            "candidate round summary finalist_soft_limit",
        )
        if type(self.requires_comparison_round) is not bool:
            raise TypeError(
                "candidate round summary requires_comparison_round must be a boolean"
            )
        expected_requires_comparison = (
            len(finalist_candidate_refs) > finalist_soft_limit
        )
        if self.requires_comparison_round != expected_requires_comparison:
            raise ValueError(
                "candidate round comparison flag contradicts finalist count"
            )
        unsigned = _round_unsigned_payload(
            round_index=round_index,
            page_outcome_identities=page_outcome_identities,
            candidate_refs=candidate_refs,
            selected_root_hypothesis_refs=category_fields[0][1],
            supported_root_hypothesis_refs=category_fields[1][1],
            unresolved_root_hypothesis_refs=category_fields[2][1],
            non_root_factor_refs=category_fields[3][1],
            excluded_refs=category_fields[4][1],
            finalist_candidate_refs=finalist_candidate_refs,
            finalist_soft_limit=finalist_soft_limit,
            requires_comparison_round=self.requires_comparison_round,
        )
        if self.identity != _identity(unsigned):
            raise ValueError(
                "candidate round summary identity does not match summary facts"
            )
        object.__setattr__(self, "round_index", round_index)
        object.__setattr__(
            self, "page_outcome_identities", page_outcome_identities
        )
        object.__setattr__(self, "candidate_refs", candidate_refs)
        object.__setattr__(
            self, "selected_root_hypothesis_refs", category_fields[0][1]
        )
        object.__setattr__(
            self, "supported_root_hypothesis_refs", category_fields[1][1]
        )
        object.__setattr__(
            self,
            "unresolved_root_hypothesis_refs",
            category_fields[2][1],
        )
        object.__setattr__(
            self, "non_root_factor_refs", category_fields[3][1]
        )
        object.__setattr__(self, "excluded_refs", category_fields[4][1])
        object.__setattr__(
            self, "finalist_candidate_refs", finalist_candidate_refs
        )
        object.__setattr__(
            self, "finalist_soft_limit", finalist_soft_limit
        )

    def to_dict(self) -> JsonDict:
        payload = _round_unsigned_payload(
            round_index=self.round_index,
            page_outcome_identities=self.page_outcome_identities,
            candidate_refs=self.candidate_refs,
            selected_root_hypothesis_refs=self.selected_root_hypothesis_refs,
            supported_root_hypothesis_refs=self.supported_root_hypothesis_refs,
            unresolved_root_hypothesis_refs=self.unresolved_root_hypothesis_refs,
            non_root_factor_refs=self.non_root_factor_refs,
            excluded_refs=self.excluded_refs,
            finalist_candidate_refs=self.finalist_candidate_refs,
            finalist_soft_limit=self.finalist_soft_limit,
            requires_comparison_round=self.requires_comparison_round,
        )
        payload["identity"] = self.identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateRoundSummary":
        payload = _require_exact_keys(
            value,
            expected=frozenset(
                {
                    "schema",
                    "round_index",
                    "page_outcome_identities",
                    "candidate_refs",
                    "selected_root_hypothesis_refs",
                    "supported_root_hypothesis_refs",
                    "unresolved_root_hypothesis_refs",
                    "non_root_factor_refs",
                    "excluded_refs",
                    "finalist_candidate_refs",
                    "finalist_soft_limit",
                    "requires_comparison_round",
                    "identity",
                }
            ),
            label="candidate round summary",
        )
        if payload["schema"] != CANDIDATE_ROUND_SUMMARY_SCHEMA:
            raise ValueError("candidate round summary schema is unsupported")
        return cls(
            round_index=payload["round_index"],
            page_outcome_identities=_json_identity_tuple(
                payload["page_outcome_identities"],
                "candidate round summary page_outcome_identities",
            ),
            candidate_refs=_json_string_tuple(
                payload["candidate_refs"],
                "candidate round summary candidate_refs",
            ),
            selected_root_hypothesis_refs=_json_string_tuple(
                payload["selected_root_hypothesis_refs"],
                "selected_root_hypothesis_refs",
            ),
            supported_root_hypothesis_refs=_json_string_tuple(
                payload["supported_root_hypothesis_refs"],
                "supported_root_hypothesis_refs",
            ),
            unresolved_root_hypothesis_refs=_json_string_tuple(
                payload["unresolved_root_hypothesis_refs"],
                "unresolved_root_hypothesis_refs",
            ),
            non_root_factor_refs=_json_string_tuple(
                payload["non_root_factor_refs"], "non_root_factor_refs"
            ),
            excluded_refs=_json_string_tuple(
                payload["excluded_refs"], "excluded_refs"
            ),
            finalist_candidate_refs=_json_string_tuple(
                payload["finalist_candidate_refs"],
                "candidate round summary finalist_candidate_refs",
            ),
            finalist_soft_limit=payload["finalist_soft_limit"],
            requires_comparison_round=payload[
                "requires_comparison_round"
            ],
            identity=payload["identity"],
        )


def summarize_candidate_round(
    *,
    round_index: int,
    page_outcomes: Sequence[CandidatePageOutcome],
    finalist_soft_limit: int = DEFAULT_FINALIST_SOFT_LIMIT,
) -> CandidateRoundSummary:
    """Aggregate page facts without truncating finalists or unresolved refs."""
    canonical_round_index = _require_non_negative_int(
        round_index, "candidate round summary round_index"
    )
    canonical_soft_limit = _require_positive_int(
        finalist_soft_limit,
        "candidate round summary finalist_soft_limit",
    )
    if (
        isinstance(page_outcomes, (str, bytes))
        or not isinstance(page_outcomes, Sequence)
    ):
        raise TypeError(
            "candidate round summary page_outcomes must be an ordered sequence"
        )
    outcomes = tuple(page_outcomes)
    if any(
        not isinstance(outcome, CandidatePageOutcome)
        for outcome in outcomes
    ):
        raise TypeError(
            "candidate round summary requires CandidatePageOutcome values"
        )
    if any(
        outcome.round_index != canonical_round_index
        for outcome in outcomes
    ):
        raise ValueError(
            "candidate round summary page outcome round_index mismatch"
        )
    if tuple(outcome.page_index for outcome in outcomes) != tuple(
        range(len(outcomes))
    ):
        raise ValueError(
            "candidate round summary page outcomes must be contiguous and ordered"
        )
    page_outcome_identities = tuple(outcome.identity for outcome in outcomes)
    _require_unique(
        page_outcome_identities,
        "candidate round summary page outcome identities",
    )
    candidate_refs = tuple(
        candidate_ref
        for outcome in outcomes
        for candidate_ref in outcome.candidate_refs
    )
    _require_unique(
        candidate_refs, "candidate round summary candidate refs"
    )
    classification_by_ref = {
        candidate_ref: category
        for outcome in outcomes
        for candidate_ref, category in outcome.classification_by_ref.items()
    }

    def refs_for(category: str) -> Tuple[str, ...]:
        return tuple(
            candidate_ref
            for candidate_ref in candidate_refs
            if classification_by_ref[candidate_ref] == category
        )

    selected = refs_for(SELECTED_ROOT_HYPOTHESIS)
    supported = refs_for(SUPPORTED_ROOT_HYPOTHESIS)
    unresolved = refs_for(UNRESOLVED_ROOT_HYPOTHESIS)
    factors = refs_for(NON_ROOT_FACTOR)
    excluded = refs_for(EXCLUDED)
    finalist_set = set(selected) | set(supported)
    finalists = tuple(
        candidate_ref
        for candidate_ref in candidate_refs
        if candidate_ref in finalist_set
    )
    requires_comparison_round = len(finalists) > canonical_soft_limit
    unsigned = _round_unsigned_payload(
        round_index=canonical_round_index,
        page_outcome_identities=page_outcome_identities,
        candidate_refs=candidate_refs,
        selected_root_hypothesis_refs=selected,
        supported_root_hypothesis_refs=supported,
        unresolved_root_hypothesis_refs=unresolved,
        non_root_factor_refs=factors,
        excluded_refs=excluded,
        finalist_candidate_refs=finalists,
        finalist_soft_limit=canonical_soft_limit,
        requires_comparison_round=requires_comparison_round,
    )
    return CandidateRoundSummary(
        round_index=canonical_round_index,
        page_outcome_identities=page_outcome_identities,
        candidate_refs=candidate_refs,
        selected_root_hypothesis_refs=selected,
        supported_root_hypothesis_refs=supported,
        unresolved_root_hypothesis_refs=unresolved,
        non_root_factor_refs=factors,
        excluded_refs=excluded,
        finalist_candidate_refs=finalists,
        finalist_soft_limit=canonical_soft_limit,
        requires_comparison_round=requires_comparison_round,
        identity=_identity(unsigned),
    )


__all__ = [
    "CANDIDATE_PAGE_SIZE",
    "DEFAULT_FINALIST_SOFT_LIMIT",
    "GLOBAL_CANDIDATE_MAX_COMPARISON_ROUNDS",
    "GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP",
    "GLOBAL_CANDIDATE_PAGING_POLICY_SCHEMA",
    "CANDIDATE_PAGE_SCHEMA",
    "CANDIDATE_PAGE_PLAN_SCHEMA",
    "CANDIDATE_PAGE_OUTCOME_SCHEMA",
    "CANDIDATE_ROUND_SUMMARY_SCHEMA",
    "SELECTED_ROOT_HYPOTHESIS",
    "SUPPORTED_ROOT_HYPOTHESIS",
    "UNRESOLVED_ROOT_HYPOTHESIS",
    "NON_ROOT_FACTOR",
    "EXCLUDED",
    "SURVIVOR_CLASSIFICATIONS",
    "CandidatePage",
    "CandidatePagePlan",
    "CandidatePageOutcome",
    "CandidateRoundSummary",
    "build_candidate_page_plan",
    "build_candidate_page_outcome",
    "summarize_candidate_round",
    "global_candidate_paging_policy",
]
