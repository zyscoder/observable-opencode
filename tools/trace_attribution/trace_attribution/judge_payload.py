"""Graph-bound structural projection for exact Judge-visible payloads."""

from __future__ import annotations

import hashlib
import json
import re
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
        "member_episode_refs",
        "member_refs",
        "opposing_evidence_refs",
        "previous_delivery_candidate_member_refs",
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
        "candidate_ref",
        "canonical_ref",
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
    {"artifact", "edge", "evidence", "fact", "reference"}
)


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
        return key in REFERENCE_LIST_KEYS or key.endswith("_refs")

    def is_reference_scalar(key: str) -> bool:
        return key in REFERENCE_SCALAR_KEYS or (
            key.endswith("_ref") and key not in {"owner_reference"}
        )

    def associated_fact(mapping: Mapping[str, Any], parent_key: str) -> bool:
        tokens = key_tokens(parent_key)
        return bool(
            tokens.intersection(ASSOCIATED_FACT_TOKENS)
            or {
                "artifact_id",
                "evidence_type",
                "provenance_class",
                "resolution_status",
            }.intersection(mapping)
        )

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

    def verified_embedded_artifact(mapping: Mapping[str, Any]) -> bool:
        content = mapping.get("content")
        content_hash = mapping.get("content_hash")
        if not isinstance(content, str) or not isinstance(content_hash, str):
            return False
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_hash):
            return False
        expected = "sha256:{0}".format(
            hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        return content_hash == expected

    def invalid_artifact(mapping: Mapping[str, Any]) -> bool:
        if mapping.get("truncated") is True or mapping.get("missing") is True:
            return True
        artifact_id = mapping.get("artifact_id")
        if artifact_id in (None, ""):
            return False
        if resolve_reference(artifact_id, "artifact_id") is omitted:
            if not verified_embedded_artifact(mapping):
                return True
        for key in ("hash_status", "file_hash_status", "slice_hash_status"):
            status = mapping.get(key)
            if status not in (None, "", "verified", "matched"):
                return True
        return False

    def sanitize(item: Any, parent_key: str = "") -> Any:
        if isinstance(item, Mapping):
            if parent_key in AUDIT_ONLY_KEYS:
                return omitted
            if (
                "resolution_status" in item
                and item.get("resolution_status") != "resolved"
            ):
                return omitted
            if invalid_artifact(item):
                return omitted
            embedded_artifact = bool(
                item.get("artifact_id")
                and verified_embedded_artifact(item)
            )

            canonical_identities = set()
            invalid_identity = False
            for raw_key, child in item.items():
                key = str(raw_key)
                if not is_reference_scalar(key):
                    continue
                resolved = (
                    str(child).removeprefix("artifact:")
                    if key == "artifact_id" and embedded_artifact
                    else resolve_reference(child, key)
                )
                if resolved is omitted:
                    invalid_identity = True
                    continue
                canonical_identities.add(
                    "artifact:{0}".format(resolved)
                    if key == "artifact_id"
                    else str(resolved)
                )
            if len(canonical_identities) > 1 and {
                str(key) for key in item
            }.intersection({"raw_ref", "resolved_ref", "canonical_ref"}):
                invalid_identity = True
            if invalid_identity and (
                associated_fact(item, parent_key)
                or any(
                    str(key) in {"ref", "candidate_ref", "node_ref"}
                    for key in item
                )
            ):
                return omitted

            output = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                if key in AUDIT_ONLY_KEYS:
                    continue
                if is_reference_scalar(key):
                    cleaned = (
                        str(child).removeprefix("artifact:")
                        if key == "artifact_id" and embedded_artifact
                        else resolve_reference(child, key)
                    )
                else:
                    cleaned = sanitize(child, key)
                if cleaned is not omitted:
                    output[key] = cleaned
            return output

        if isinstance(item, (list, tuple)):
            if is_reference_list(parent_key):
                output = []
                seen = set()
                scalar_key = (
                    "artifact_id"
                    if parent_key.endswith("artifact_ids")
                    else "ref"
                )
                for child in item:
                    cleaned = resolve_reference(child, scalar_key)
                    if cleaned is omitted or cleaned in seen:
                        continue
                    seen.add(cleaned)
                    output.append(cleaned)
                return output
            output = []
            for child in item:
                cleaned = sanitize(child, parent_key)
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
                cleaned = sanitize(decoded, parent_key)
                if cleaned is omitted:
                    return omitted
                return stable_json(cleaned)
        return item

    result = sanitize(value)
    return {} if result is omitted else result


def strip_audit_only_payload(value: Any) -> Any:
    """Remove persisted audit diagnostics from an already graph-sanitized value."""
    omitted = object()

    def strip(item: Any) -> Any:
        if isinstance(item, Mapping):
            if (
                item.get("resolution_status") not in (None, "", "resolved")
                or item.get("missing") is True
                or item.get("truncated") is True
            ):
                return omitted
            output = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                if key in AUDIT_ONLY_KEYS:
                    continue
                cleaned = strip(child)
                if cleaned is not omitted:
                    output[key] = cleaned
            return output
        if isinstance(item, (list, tuple)):
            output = []
            for child in item:
                cleaned = strip(child)
                if cleaned is not omitted:
                    output.append(cleaned)
            return output
        return item

    result = strip(value)
    return {} if result is omitted else result


__all__ = [
    "AUDIT_ONLY_KEYS",
    "sanitize_judge_visible_payload",
    "strip_audit_only_payload",
]
