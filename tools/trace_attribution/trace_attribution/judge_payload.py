"""Graph-bound structural projection for exact Judge-visible payloads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .models import stable_json


AUDIT_ONLY_KEYS = frozenset(
    {
        "integrity_failures",
        "missing_artifact_ids",
        "missing_artifacts",
        "missing_evidence_refs",
        "referenced_artifact_ids",
        "stale_member_refs",
        "truncated_artifact_ids",
        "truncated_artifacts",
        "unresolved_references",
        "validation_source",
    }
)
REFERENCE_LIST_KEYS = frozenset(
    {
        "artifact_refs",
        "candidate_evidence_refs",
        "candidate_member_refs",
        "checked_evidence_refs",
        "decisive_evidence_refs",
        "delivery_history_candidate_member_refs",
        "delivery_history_episode_refs",
        "downstream_path",
        "evidence_refs",
        "citation_refs",
        "member_episode_refs",
        "member_refs",
        "opposing_evidence_refs",
        "previous_delivery_candidate_member_refs",
        "referenced_artifact_ids",
        "recursive_path",
        "resolved_refs",
        "retrieval_candidate_member_refs",
        "source_refs",
        "start_refs",
    }
)
REFERENCE_SCALAR_KEYS = frozenset(
    {
        "anchor_ref",
        "artifact_id",
        "candidate_ref",
        "canonical_ref",
        "citation_ref",
        "from_ref",
        "intervention_ref",
        "node_ref",
        "raw_ref",
        "ref",
        "resolved_ref",
        "seed_ref",
        "source_ref",
        "target_ref",
        "to_ref",
    }
)
ASSOCIATED_FACT_TOKENS = frozenset(
    {"artifact", "citation", "edge", "evidence", "fact", "reference"}
)
TYPED_MAPPING_KEYS = frozenset(
    {
        "anchor_ref",
        "artifact_id",
        "candidate_ref",
        "canonical_ref",
        "citation_ref",
        "citation_refs",
        "evidence_refs",
        "evidence_type",
        "from_ref",
        "intervention_ref",
        "node_ref",
        "owner_reference",
        "provenance_class",
        "raw_ref",
        "ref",
        "resolution_status",
        "resolved_ref",
        "seed_ref",
        "source_ref",
        "source_refs",
        "target_ref",
        "to_ref",
    }
)
SEMANTIC_TEXT_KEYS = frozenset(
    {
        "actual",
        "body",
        "content",
        "description",
        "excerpt",
        "expected",
        "label",
        "mechanism",
        "name",
        "rationale",
        "reason",
        "summary",
        "text",
        "title",
    }
)
EQUIVALENT_IDENTITY_KEYS = frozenset(
    {
        "canonical_ref",
        "citation_ref",
        "node_ref",
        "raw_ref",
        "ref",
        "resolved_ref",
    }
)
SHAPE_IDENTITY_ALIASES = {
    "anchor": "anchor_ref",
    "candidate": "candidate_ref",
    "intervention": "intervention_ref",
    "seed": "seed_ref",
    "source": "source_ref",
    "target": "target_ref",
}
REFERENCE_ENVELOPE_KEYS = frozenset(
    {
        "provenance_class",
        "raw_ref",
        "resolution_status",
        "resolved_ref",
    }
)
ALLOWED_OWNER_PROVENANCE = frozenset({"inferred", "reconstructed", "recorded"})


def sanitize_judge_visible_payload(graph: Any, value: Any) -> Any:
    """Return the active, resolved structural projection of a payload.

    The projection resolves aliases, removes ineligible associated facts as a
    unit, and recursively sanitizes JSON encoded inside string fields.
    """

    omitted = object()

    def key_tokens(value: str) -> set[str]:
        return {
            token
            for token in value.lower().replace("-", "_").split("_")
            if token
        }

    def is_reference_list(key: str) -> bool:
        return key in REFERENCE_LIST_KEYS

    def is_reference_scalar(key: str) -> bool:
        return key in REFERENCE_SCALAR_KEYS

    def associated_fact(mapping: Mapping[str, Any], parent_key: str) -> bool:
        tokens = key_tokens(parent_key)
        return bool(
            tokens.intersection(ASSOCIATED_FACT_TOKENS)
            or TYPED_MAPPING_KEYS.intersection(str(key) for key in mapping)
        )

    def equivalent_identity_keys(
        mapping: Mapping[str, Any],
        parent_key: str,
    ) -> set[str]:
        keys = {str(key) for key in mapping}
        identities = keys.intersection(EQUIVALENT_IDENTITY_KEYS)
        if "artifact_id" in keys:
            identities.add("artifact_id")
        shape_tokens = key_tokens(parent_key)
        fact_kind = mapping.get("fact_kind")
        if isinstance(fact_kind, str):
            shape_tokens.update(key_tokens(fact_kind))
        present_shape_aliases = {
            alias for alias in SHAPE_IDENTITY_ALIASES.values() if alias in keys
        }
        for token, alias in SHAPE_IDENTITY_ALIASES.items():
            if token in shape_tokens and alias in keys:
                identities.add(alias)
        if len(present_shape_aliases) == 1:
            identities.update(present_shape_aliases)
        return identities

    def resolve_reference(raw_value: Any, key: str) -> Any:
        if not isinstance(raw_value, str) or not raw_value.strip():
            return omitted
        raw_ref = raw_value.strip()
        if key == "artifact_id":
            raw_ref = "artifact:{0}".format(raw_ref.removeprefix("artifact:"))
        resolved = graph.resolve(raw_ref)
        if (
            resolved
            and resolved in graph.nodes
            and graph.active_revision_evidence_eligible(resolved)
        ):
            return resolved
        artifact = graph.artifact_reference_status(raw_ref)
        if (
            artifact is not None
            and artifact.get("resolution_status") == "resolved"
            and artifact.get("availability") == "available"
        ):
            canonical = str(artifact.get("canonical_ref") or "")
            return (
                canonical.removeprefix("artifact:")
                if key == "artifact_id"
                else canonical
            )
        return omitted

    def canonical_identity(raw_value: Any, key: str) -> Any:
        resolved = resolve_reference(raw_value, key)
        if resolved is omitted:
            return omitted
        return (
            "artifact:{0}".format(resolved)
            if key == "artifact_id"
            else str(resolved)
        )

    def resolved_mapping_identity(
        mapping: Mapping[str, Any],
        parent_key: str,
    ) -> Any:
        identities = [
            canonical_identity(mapping.get(key), key)
            for key in equivalent_identity_keys(mapping, parent_key)
        ]
        if any(identity is omitted for identity in identities):
            return omitted
        canonical = set(identities)
        if len(canonical) != 1:
            return omitted
        return next(iter(canonical), omitted)

    def valid_owner_reference(
        owner: Any,
        *,
        expected_owner: Any,
    ) -> Any:
        if not isinstance(owner, Mapping):
            return omitted
        if not REFERENCE_ENVELOPE_KEYS.issubset(str(key) for key in owner):
            return omitted
        if (
            owner.get("resolution_status") != "resolved"
            or owner.get("provenance_class") not in ALLOWED_OWNER_PROVENANCE
        ):
            return omitted
        resolved = resolved_mapping_identity(owner, "owner_reference")
        if resolved is omitted:
            return omitted
        if expected_owner is not None and resolved != expected_owner:
            return omitted
        return resolved

    def valid_artifact(
        mapping: Mapping[str, Any],
        *,
        expected_owner: Any,
    ) -> bool:
        if mapping.get("truncated") is True or mapping.get("missing") is True:
            return False
        if (
            ("availability" in mapping and mapping.get("availability") != "available")
            or ("available" in mapping and mapping.get("available") is not True)
        ):
            return False
        artifact_id = mapping.get("artifact_id")
        if artifact_id in (None, ""):
            return True
        canonical_id = str(artifact_id).removeprefix("artifact:")
        if canonical_id not in graph._artifact_index:
            return False
        verified = graph._artifact_reader.read(canonical_id)
        if verified.content is None or verified.content_bytes is None:
            return False
        for key in ("hash_status", "file_hash_status", "slice_hash_status"):
            status = mapping.get(key)
            if status not in (
                None,
                "",
                "verified",
                "matched",
                "not_used",
            ):
                return False
        if "content" not in mapping:
            if "owner_reference" in mapping:
                return (
                    valid_owner_reference(
                        mapping.get("owner_reference"),
                        expected_owner=expected_owner,
                    )
                    is not omitted
                )
            return True
        content = mapping.get("content")
        if not isinstance(content, str):
            return False
        content_bytes = content.encode("utf-8")
        expected_hash = "sha256:{0}".format(hashlib.sha256(content_bytes).hexdigest())
        if mapping.get("content_hash") != expected_hash:
            return False
        byte_count = mapping.get("byte_count")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count != len(content_bytes)
        ):
            return False
        byte_range = mapping.get("byte_range")
        if (
            not isinstance(byte_range, (list, tuple))
            or len(byte_range) != 2
            or any(
                isinstance(offset, bool) or not isinstance(offset, int)
                for offset in byte_range
            )
        ):
            return False
        start, end = byte_range
        if start < 0 or end < start or end > len(verified.content_bytes):
            return False
        try:
            verified.content_bytes[:start].decode("utf-8", errors="strict")
            verified.content_bytes[:end].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        if content_bytes != verified.content_bytes[start:end]:
            return False
        return (
            valid_owner_reference(
                mapping.get("owner_reference"),
                expected_owner=expected_owner,
            )
            is not omitted
        )

    def contains_unknown_graph_reference(
        mapping: Mapping[str, Any],
        *,
        typed: bool,
    ) -> bool:
        if not typed:
            return False
        for raw_key, child in mapping.items():
            key = str(raw_key)
            if (
                key in REFERENCE_SCALAR_KEYS
                or key in REFERENCE_LIST_KEYS
                or key in SEMANTIC_TEXT_KEYS
                or key in AUDIT_ONLY_KEYS
                or isinstance(child, (Mapping, list, tuple))
                or not isinstance(child, str)
            ):
                continue
            candidate = child.strip()
            if candidate.startswith(("record:", "artifact:")):
                return True
            if candidate and graph.resolve(candidate) in graph.nodes:
                return True
        return False

    def sanitize(
        item: Any,
        parent_key: str = "",
        *,
        expected_owner: Any = None,
    ) -> Any:
        if isinstance(item, Mapping):
            if parent_key in AUDIT_ONLY_KEYS:
                return omitted
            typed = associated_fact(item, parent_key)
            if (
                "resolution_status" in item
                and item.get("resolution_status") != "resolved"
            ):
                return omitted
            if any(
                item.get(key)
                for key in (
                    "integrity_failures",
                    "missing_artifact_ids",
                    "truncated_artifact_ids",
                )
            ):
                return omitted
            if "artifact_id" in item and not valid_artifact(
                item,
                expected_owner=expected_owner,
            ):
                return omitted
            if contains_unknown_graph_reference(item, typed=typed):
                return omitted

            equivalent_keys = equivalent_identity_keys(item, parent_key)
            canonical_identities = set()
            invalid_identity = False
            for raw_key, child in item.items():
                key = str(raw_key)
                if is_reference_scalar(key):
                    resolved = resolve_reference(child, key)
                    if resolved is omitted:
                        invalid_identity = True
                        continue
                    if key in equivalent_keys:
                        canonical_identities.add(
                            canonical_identity(child, key)
                        )
                elif is_reference_list(key):
                    if not isinstance(child, (list, tuple)):
                        invalid_identity = True
                        continue
                    scalar_key = (
                        "artifact_id"
                        if key in {"artifact_refs", "referenced_artifact_ids"}
                        else "ref"
                    )
                    resolved_items = [
                        resolve_reference(value, scalar_key) for value in child
                    ]
                    if any(value is omitted for value in resolved_items):
                        invalid_identity = True
            if len(canonical_identities) > 1:
                invalid_identity = True
            if invalid_identity and typed:
                return omitted

            manifest_owner = expected_owner
            if (
                "node_ref" in item
                and "hydrated_artifacts" in item
                and isinstance(item.get("hydrated_artifacts"), (list, tuple))
            ):
                manifest_owner = canonical_identity(item.get("node_ref"), "node_ref")
                if manifest_owner is omitted:
                    return omitted
            output = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                if key in AUDIT_ONLY_KEYS:
                    continue
                if is_reference_scalar(key):
                    cleaned = resolve_reference(child, key)
                elif is_reference_list(key):
                    cleaned = sanitize(
                        child,
                        key,
                        expected_owner=(
                            manifest_owner
                            if key == "hydrated_artifacts"
                            else expected_owner
                        ),
                    )
                else:
                    cleaned = sanitize(
                        child,
                        key,
                        expected_owner=(
                            manifest_owner
                            if key == "hydrated_artifacts"
                            else expected_owner
                        ),
                    )
                if cleaned is not omitted:
                    output[key] = cleaned
            return output

        if isinstance(item, (list, tuple)):
            if is_reference_list(parent_key):
                output = []
                seen = set()
                scalar_key = (
                    "artifact_id"
                    if parent_key in {"artifact_refs", "referenced_artifact_ids"}
                    else "ref"
                )
                for child in item:
                    cleaned = resolve_reference(child, scalar_key)
                    if cleaned is omitted:
                        return omitted
                    if cleaned in seen:
                        continue
                    seen.add(cleaned)
                    output.append(cleaned)
                return output
            output = []
            for child in item:
                cleaned = sanitize(
                    child,
                    parent_key,
                    expected_owner=expected_owner,
                )
                if cleaned is not omitted:
                    output.append(cleaned)
            return output

        if isinstance(item, str):
            stripped = item.strip()
            if stripped.startswith(("{", "[")):
                try:
                    decoded = json.loads(item)
                except json.JSONDecodeError:
                    return item
                cleaned = sanitize(
                    decoded,
                    parent_key,
                    expected_owner=expected_owner,
                )
                if cleaned is omitted:
                    return omitted
                return stable_json(cleaned)
        return item

    result = sanitize(value)
    return {} if result is omitted else result


__all__ = [
    "AUDIT_ONLY_KEYS",
    "sanitize_judge_visible_payload",
]
