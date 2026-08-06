"""Immutable schema validation for offline attribution labels."""

from __future__ import annotations

import copy
import math
import re
from typing import Any, Dict, List, Mapping, Set

from .causal_state import seed_binding_identity_for


JsonDict = Dict[str, Any]

LEGACY_LABEL_SCHEMA_VERSION = "recursive-attribution-labels/v3"
LABEL_SCHEMA_VERSION = "recursive-attribution-labels/v4"
V5_LABEL_SCHEMA_VERSION = "recursive-attribution-labels/v5"

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_REF_PATTERN = re.compile(r"^record:\S+$")
_ANCHOR_PATTERN = re.compile(r"^semantic_anchor:v2:\S+$")
_OCCURRENCE_PATTERN = re.compile(r"^semantic_occurrence:v1:\S+$")
_SEED_BINDING_PATTERN = re.compile(r"^seed:[0-9a-f]{24}$")

_LEGACY_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "roots",
        "conditions",
        "amplifiers",
        "forbidden_roots",
        "allowed_unresolved_outcomes",
    }
)
_V4_KEYS = frozenset({*_LEGACY_KEYS, "materializations", "unrelated"})
_V5_KEYS = frozenset(
    {"schema_version", "case_id", "source_binding", "seeds", "case_shared_factors"}
)
_SOURCE_BINDING_KEYS = frozenset(
    {
        "trace_sha256",
        "review_sha256",
        "evaluation_sha256",
        "effective_trace_sha256",
        "subject_revision",
        "revision_provenance_status",
    }
)
_SEED_KEYS = frozenset(
    {
        "defect_id",
        "start_ref",
        "defect_fingerprint",
        "seed_binding_identity",
        "seed_semantic_anchor_id",
        "seed_semantic_occurrence_id",
        "seed_source_bindings",
        "defect_origin_kind",
        "expected_outcome",
        "roots",
        "conditions",
        "amplifiers",
        "materializations",
        "unrelated",
        "forbidden_roots",
        "allowed_unresolved_outcomes",
    }
)
_SEED_SOURCE_BINDING_KEYS = frozenset(
    {"source_kind", "source_index", "source_sha256"}
)
_ROLE_ENTRY_KEYS = frozenset(
    {"node_ref", "semantic_anchor_id", "semantic_occurrence_id"}
)
_LEGACY_ROLE_ENTRY_REQUIRED_KEYS = frozenset(
    {"semantic_anchor_id", "semantic_occurrence_id"}
)
_SHARED_FACTOR_KEYS = frozenset(
    {
        "factor_role",
        "seed_binding_identities",
        *_ROLE_ENTRY_KEYS,
    }
)
_ROLE_FIELDS = (
    ("roots", "root"),
    ("conditions", "contributing_condition"),
    ("amplifiers", "amplifying_factor"),
    ("materializations", "downstream_materialization"),
    ("unrelated", "unrelated"),
    ("forbidden_roots", "forbidden_root"),
)
_V3_ROLE_FIELDS = _ROLE_FIELDS[:3] + (_ROLE_FIELDS[-1],)
_ALLOWED_UNRESOLVED_OUTCOMES = frozenset(
    {"inconclusive", "partial", "partial_root_found"}
)
_SEED_SOURCE_KINDS = frozenset(
    {"raw_trace", "quality_review", "external_evaluation"}
)
_SEED_SOURCE_KIND_ORDER = {
    "raw_trace": 0,
    "quality_review": 1,
    "external_evaluation": 2,
}
_DEFECT_ORIGIN_KINDS = frozenset(
    {"introduced_by_agent", "baseline_existing_unrepaired"}
)
_EXPECTED_OUTCOMES = frozenset({"confirmed_root", "no_defect", "unresolved"})
_SHARED_FACTOR_ROLES = frozenset(
    {
        "contributing_condition",
        "amplifying_factor",
        "downstream_materialization",
        "unrelated",
    }
)


class LabelContractError(ValueError):
    """Raised when offline human labels do not satisfy their exact schema."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LabelContractError("{0} must be an object".format(label))
    return value


def _list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise LabelContractError("{0} must be a list".format(label))
    return value


def _exact_keys(value: Mapping[str, Any], expected: Set[str], label: str) -> None:
    actual = set(value)
    if actual != set(expected):
        raise LabelContractError(
            "{0} keys differ: missing={1}, extra={2}".format(
                label, sorted(set(expected) - actual), sorted(actual - set(expected))
            )
        )


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LabelContractError("{0} must be a non-empty string".format(label))
    return value


def _matches(value: Any, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LabelContractError("{0} has an invalid format".format(label))
    return value


def _validate_json_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise LabelContractError("{0} must contain JSON-safe values".format(label))
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, "{0}[{1}]".format(label, index))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LabelContractError("{0} must contain string object keys".format(label))
            _validate_json_value(item, "{0}.{1}".format(label, key))
        return
    raise LabelContractError("{0} must contain JSON-safe values".format(label))


def _validate_role_entry(
    value: Any,
    label: str,
    *,
    require_node_ref: bool,
) -> str:
    entry = _mapping(value, label)
    expected = _ROLE_ENTRY_KEYS if require_node_ref else _LEGACY_ROLE_ENTRY_REQUIRED_KEYS
    allowed = _ROLE_ENTRY_KEYS
    missing = set(expected) - set(entry)
    extra = set(entry) - allowed
    if missing or extra:
        raise LabelContractError(
            "{0} keys differ: missing={1}, extra={2}".format(
                label, sorted(missing), sorted(extra)
            )
        )
    if "node_ref" in entry:
        node_ref = entry["node_ref"]
        if require_node_ref:
            _matches(node_ref, _RECORD_REF_PATTERN, "{0}.node_ref".format(label))
        elif not isinstance(node_ref, str) or not node_ref.startswith("record:"):
            raise LabelContractError("{0}.node_ref has an invalid format".format(label))
    if require_node_ref:
        _matches(
            entry["semantic_anchor_id"],
            _ANCHOR_PATTERN,
            "{0}.semantic_anchor_id".format(label),
        )
        return _matches(
            entry["semantic_occurrence_id"],
            _OCCURRENCE_PATTERN,
            "{0}.semantic_occurrence_id".format(label),
        )
    anchor = entry["semantic_anchor_id"]
    occurrence = entry["semantic_occurrence_id"]
    if not isinstance(anchor, str) or not anchor.startswith("semantic_anchor:v2:"):
        raise LabelContractError("{0}.semantic_anchor_id has an invalid format".format(label))
    if not isinstance(occurrence, str) or not occurrence.startswith(
        "semantic_occurrence:v1:"
    ):
        raise LabelContractError(
            "{0}.semantic_occurrence_id has an invalid format".format(label)
        )
    return occurrence


def _validate_outcomes(value: Any, label: str) -> None:
    outcomes = _list(value, label)
    if any(item not in _ALLOWED_UNRESOLVED_OUTCOMES for item in outcomes):
        raise LabelContractError("{0} contains an unsupported state".format(label))


def _validate_v3_or_v4(labels: Mapping[str, Any]) -> None:
    schema_version = labels.get("schema_version")
    expected_keys = _V4_KEYS if schema_version == LABEL_SCHEMA_VERSION else _LEGACY_KEYS
    _exact_keys(labels, expected_keys, "labels")
    if not isinstance(labels["case_id"], str) or not labels["case_id"]:
        raise LabelContractError("labels.case_id must be a non-empty string")
    role_fields = _ROLE_FIELDS if schema_version == LABEL_SCHEMA_VERSION else _V3_ROLE_FIELDS
    factor_occurrences: Dict[str, str] = {}
    for field, role in role_fields:
        occurrences: Set[str] = set()
        refs: Set[str] = set()
        for index, entry in enumerate(_list(labels[field], "labels.{0}".format(field))):
            occurrence = _validate_role_entry(
                entry,
                "labels.{0}[{1}]".format(field, index),
                require_node_ref=False,
            )
            node_ref = entry.get("node_ref")
            if occurrence in occurrences or (node_ref is not None and node_ref in refs):
                raise LabelContractError("duplicate label identity in {0}".format(field))
            occurrences.add(occurrence)
            if node_ref is not None:
                refs.add(node_ref)
            if role in _SHARED_FACTOR_ROLES:
                prior = factor_occurrences.get(occurrence)
                if prior is not None:
                    raise LabelContractError(
                        "factor label occurrence appears in both {0} and {1}".format(
                            prior, field
                        )
                    )
                factor_occurrences[occurrence] = field
    _validate_outcomes(labels["allowed_unresolved_outcomes"], "allowed_unresolved_outcomes")


def _validate_source_binding(value: Any) -> None:
    binding = _mapping(value, "labels.source_binding")
    _exact_keys(binding, _SOURCE_BINDING_KEYS, "labels.source_binding")
    _matches(binding["trace_sha256"], _SHA256_PATTERN, "labels.source_binding.trace_sha256")
    review = binding["review_sha256"]
    if review != "":
        _matches(review, _SHA256_PATTERN, "labels.source_binding.review_sha256")
    for index, digest in enumerate(
        _list(binding["evaluation_sha256"], "labels.source_binding.evaluation_sha256")
    ):
        _matches(
            digest,
            _SHA256_PATTERN,
            "labels.source_binding.evaluation_sha256[{0}]".format(index),
        )
    _matches(
        binding["effective_trace_sha256"],
        _SHA256_PATTERN,
        "labels.source_binding.effective_trace_sha256",
    )
    _non_empty_string(binding["subject_revision"], "labels.source_binding.subject_revision")
    if binding["revision_provenance_status"] != "valid":
        raise LabelContractError("labels.source_binding.revision_provenance_status must be valid")


def _validate_seed_source_bindings(value: Any, label: str) -> None:
    bindings = _list(value, label)
    if not bindings:
        raise LabelContractError("{0} must contain at least one entry".format(label))
    prior_order = None
    source_identities: Set[tuple] = set()
    for index, raw_binding in enumerate(bindings):
        entry_label = "{0}[{1}]".format(label, index)
        binding = _mapping(raw_binding, entry_label)
        _exact_keys(binding, _SEED_SOURCE_BINDING_KEYS, entry_label)
        source_kind = binding["source_kind"]
        if source_kind not in _SEED_SOURCE_KINDS:
            raise LabelContractError(
                "{0}.source_kind is unsupported".format(entry_label)
            )
        source_index = binding["source_index"]
        if type(source_index) is not int or source_index < 0:
            raise LabelContractError(
                "{0}.source_index is invalid".format(entry_label)
            )
        if source_kind in {"raw_trace", "quality_review"} and source_index != 0:
            raise LabelContractError(
                "{0}.source_index must be zero".format(entry_label)
            )
        _matches(
            binding["source_sha256"],
            _SHA256_PATTERN,
            "{0}.source_sha256".format(entry_label),
        )
        source_identity = (source_kind, source_index)
        if source_identity in source_identities:
            raise LabelContractError(
                "{0} contains a duplicate source identity".format(label)
            )
        source_identities.add(source_identity)
        order = (_SEED_SOURCE_KIND_ORDER[source_kind], source_index)
        if prior_order is not None and order <= prior_order:
            raise LabelContractError(
                "{0} must be in composition order".format(label)
            )
        prior_order = order


def _validate_v5(labels: Mapping[str, Any]) -> None:
    _exact_keys(labels, _V5_KEYS, "labels")
    _non_empty_string(labels["case_id"], "labels.case_id")
    _validate_source_binding(labels["source_binding"])

    seed_bindings: Set[str] = set()
    defect_ids: Set[str] = set()
    start_refs: Set[str] = set()
    seed_roles_by_occurrence: Dict[str, Dict[str, str]] = {}
    seed_roles_by_ref: Dict[str, Dict[str, str]] = {}
    expected_outcomes: Dict[str, str] = {}
    for index, raw_seed in enumerate(_list(labels["seeds"], "labels.seeds")):
        label = "labels.seeds[{0}]".format(index)
        seed = _mapping(raw_seed, label)
        _exact_keys(seed, _SEED_KEYS, label)
        defect_id = _non_empty_string(seed["defect_id"], "{0}.defect_id".format(label))
        start_ref = _matches(seed["start_ref"], _RECORD_REF_PATTERN, "{0}.start_ref".format(label))
        defect_fingerprint = _non_empty_string(
            seed["defect_fingerprint"], "{0}.defect_fingerprint".format(label)
        )
        binding = _matches(
            seed["seed_binding_identity"],
            _SEED_BINDING_PATTERN,
            "{0}.seed_binding_identity".format(label),
        )
        if binding != seed_binding_identity_for(start_ref, defect_fingerprint):
            raise LabelContractError("{0}.seed_binding_identity does not match its seed".format(label))
        _matches(
            seed["seed_semantic_anchor_id"],
            _ANCHOR_PATTERN,
            "{0}.seed_semantic_anchor_id".format(label),
        )
        _matches(
            seed["seed_semantic_occurrence_id"],
            _OCCURRENCE_PATTERN,
            "{0}.seed_semantic_occurrence_id".format(label),
        )
        _validate_seed_source_bindings(
            seed["seed_source_bindings"],
            "{0}.seed_source_bindings".format(label),
        )
        if seed["defect_origin_kind"] not in _DEFECT_ORIGIN_KINDS:
            raise LabelContractError("{0}.defect_origin_kind is unsupported".format(label))
        expected_outcome = seed["expected_outcome"]
        if expected_outcome not in _EXPECTED_OUTCOMES:
            raise LabelContractError("{0}.expected_outcome is unsupported".format(label))
        if defect_id in defect_ids or start_ref in start_refs or binding in seed_bindings:
            raise LabelContractError("duplicate v5 seed identity")
        defect_ids.add(defect_id)
        start_refs.add(start_ref)
        seed_bindings.add(binding)

        assigned_roles_by_occurrence: Dict[str, str] = {}
        assigned_roles_by_ref: Dict[str, str] = {}
        for field, role in _ROLE_FIELDS:
            node_refs: Set[str] = set()
            occurrences: Set[str] = set()
            for role_index, raw_entry in enumerate(_list(seed[field], "{0}.{1}".format(label, field))):
                role_label = "{0}.{1}[{2}]".format(label, field, role_index)
                occurrence = _validate_role_entry(
                    raw_entry, role_label, require_node_ref=True
                )
                node_ref = raw_entry["node_ref"]
                if occurrence in occurrences or node_ref in node_refs:
                    raise LabelContractError("duplicate label identity in {0}".format(role_label))
                if occurrence in assigned_roles_by_occurrence:
                    raise LabelContractError(
                        "seed role conflict for occurrence {0}".format(occurrence)
                    )
                if node_ref in assigned_roles_by_ref:
                    raise LabelContractError(
                        "seed role conflict for node_ref {0}".format(node_ref)
                    )
                occurrences.add(occurrence)
                node_refs.add(node_ref)
                assigned_roles_by_occurrence[occurrence] = role
                assigned_roles_by_ref[node_ref] = role
        _validate_outcomes(seed["allowed_unresolved_outcomes"], "{0}.allowed_unresolved_outcomes".format(label))
        has_positive_roles = any(
            seed[field] for field in ("roots", "conditions", "amplifiers", "materializations")
        )
        if expected_outcome == "confirmed_root" and not seed["roots"]:
            raise LabelContractError("confirmed_root expected_outcome requires a root")
        if expected_outcome == "no_defect" and has_positive_roles:
            raise LabelContractError("no_defect expected_outcome forbids positive roles")
        if expected_outcome == "no_defect" and seed["allowed_unresolved_outcomes"]:
            raise LabelContractError(
                "no_defect expected_outcome forbids unresolved outcomes"
            )
        if expected_outcome == "unresolved" and has_positive_roles:
            raise LabelContractError("unresolved expected_outcome forbids positive roles")
        if expected_outcome == "unresolved" and not seed["allowed_unresolved_outcomes"]:
            raise LabelContractError(
                "unresolved expected_outcome requires an allowed unresolved outcome"
            )
        seed_roles_by_occurrence[binding] = assigned_roles_by_occurrence
        seed_roles_by_ref[binding] = assigned_roles_by_ref
        expected_outcomes[binding] = expected_outcome

    shared_entries: Set[tuple] = set()
    for index, raw_factor in enumerate(
        _list(labels["case_shared_factors"], "labels.case_shared_factors")
    ):
        label = "labels.case_shared_factors[{0}]".format(index)
        factor = _mapping(raw_factor, label)
        _exact_keys(factor, _SHARED_FACTOR_KEYS, label)
        role = factor["factor_role"]
        if role not in _SHARED_FACTOR_ROLES:
            raise LabelContractError("{0}.factor_role is unsupported".format(label))
        identities = _list(factor["seed_binding_identities"], "{0}.seed_binding_identities".format(label))
        if len(identities) < 2 or identities != sorted(identities) or len(identities) != len(set(identities)):
            raise LabelContractError("{0}.seed_binding_identities must be sorted and unique".format(label))
        for identity in identities:
            _matches(identity, _SEED_BINDING_PATTERN, "{0}.seed_binding_identities".format(label))
            if identity not in seed_bindings:
                raise LabelContractError("{0} references an unknown seed".format(label))
        _matches(factor["node_ref"], _RECORD_REF_PATTERN, "{0}.node_ref".format(label))
        _matches(
            factor["semantic_anchor_id"],
            _ANCHOR_PATTERN,
            "{0}.semantic_anchor_id".format(label),
        )
        occurrence = _matches(
            factor["semantic_occurrence_id"],
            _OCCURRENCE_PATTERN,
            "{0}.semantic_occurrence_id".format(label),
        )
        identity = (role, tuple(identities), occurrence)
        if identity in shared_entries:
            raise LabelContractError("duplicate shared factor identity")
        shared_entries.add(identity)
        for seed_identity in identities:
            if occurrence in seed_roles_by_occurrence[seed_identity]:
                raise LabelContractError(
                    "shared factor conflicts by occurrence with a seed role"
                )
            node_ref = factor["node_ref"]
            if node_ref in seed_roles_by_ref[seed_identity]:
                raise LabelContractError(
                    "shared factor conflicts by node_ref with a seed role"
                )
            if (
                role != "unrelated"
                and expected_outcomes[seed_identity] in {"no_defect", "unresolved"}
            ):
                raise LabelContractError(
                    "{0} expected_outcome forbids positive shared factors".format(
                        expected_outcomes[seed_identity]
                    )
                )
            seed_roles_by_occurrence[seed_identity][occurrence] = role
            seed_roles_by_ref[seed_identity][node_ref] = role


def validate_labels(value: Mapping[str, Any]) -> JsonDict:
    """Validate a label document and return a JSON-safe deep copy of it."""
    _validate_json_value(value, "labels")
    labels = copy.deepcopy(dict(_mapping(value, "labels")))
    schema_version = labels.get("schema_version")
    if schema_version in {LEGACY_LABEL_SCHEMA_VERSION, LABEL_SCHEMA_VERSION}:
        _validate_v3_or_v4(labels)
        return labels
    if schema_version == V5_LABEL_SCHEMA_VERSION:
        _validate_v5(labels)
        return labels
    raise LabelContractError("unsupported labels schema_version")


__all__ = [
    "LEGACY_LABEL_SCHEMA_VERSION",
    "LABEL_SCHEMA_VERSION",
    "V5_LABEL_SCHEMA_VERSION",
    "LabelContractError",
    "validate_labels",
]
