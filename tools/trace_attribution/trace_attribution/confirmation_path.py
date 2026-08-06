"""Confirmation-stage causal path policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, List, Sequence, Tuple

from .graph import is_temporal_only_edge


CONFIRMATION_CAUSAL_RELATIONS = frozenset(
    {
        # Formal trace dataflow relations.
        "assembled",
        "called",
        "claimed_by",
        "compressed_from",
        "compressed_to",
        "consumed",
        "contextualizes_claim",
        "continued_from",
        "delegated_to",
        "derived_from",
        "executed_for_claim",
        "external_evaluation_observed",
        "failed_before",
        "modified_by",
        "motivated_by_evidence",
        "process_lifecycle_observed",
        "produced",
        "prompted",
        "read_from",
        "reported_to",
        "resolved_to",
        "response_to_claim_group",
        "returned_by",
        "returned_to",
        "selected_by",
        "selected_into_context",
        "spawned",
        "submitted",
        "supported_response",
        "supports_claim",
        "transformed_to",
        "used_as_context",
        "verified_by",
        # Graph reconstruction and attribution fixture relations.
        "authored_change_reconstructed",
        "authored_decision_observed_by_evaluation",
        "candidate_informed_outcome",
        "change_created_observed_defect",
        "change_observed_by_evaluation",
        "context_available_to_change",
        "decision_exposed_by_evaluation",
        "decision_guided_claim",
        "decision_guided_change",
        "interruption_amplified_evaluation",
        "outcome_evidence",
        "reasoning_selected_action",
        "record_source",
        "retained_in_context",
    }
)

NON_CAUSAL_CONFIRMATION_RELATIONS = frozenset(
    {
        "claim_group_precedes",
        "temporal_proximity",
        "temporal_sequence",
        "fallback_sequence",
        "previous_progress_episode",
        "progress_episode_member",
        "progress_episode_projects_to_target",
        "semantic_navigation_route",
    }
)
CONFIRMED_TRACE_PROVENANCE_TIER = "confirmed_trace"
OFFLINE_RECONSTRUCTION_PROVENANCE_TIER = "offline_reconstruction"
MIXED_PROVENANCE_TIER = "mixed"
PATH_PROVENANCE_TIERS = frozenset(
    {
        CONFIRMED_TRACE_PROVENANCE_TIER,
        OFFLINE_RECONSTRUCTION_PROVENANCE_TIER,
    }
)


def is_confirmation_causal_edge(
    edge: Mapping[str, Any], *, default_eligible: bool
) -> bool:
    relation = str(edge.get("relation") or "").strip().lower()
    return (
        edge.get("eligible_for_attribution", default_eligible) is True
        and edge.get("retrieval_candidate") is not True
        and str(edge.get("evidence_type") or "").strip().lower()
        != "semantic_inferred"
        and not is_temporal_only_edge(edge)
        and relation not in NON_CAUSAL_CONFIRMATION_RELATIONS
        and relation in CONFIRMATION_CAUSAL_RELATIONS
    )


def has_confirmation_causal_hop(
    edges: Iterable[Mapping[str, Any]],
    *,
    default_eligible: bool,
) -> bool:
    """Prefer explicit dataflow provenance over generic source references."""
    candidates = tuple(
        edge for edge in edges if isinstance(edge, Mapping)
    )
    explicit = tuple(
        edge
        for edge in candidates
        if str(edge.get("edge_origin") or "")
        == "trace.dataflow_edges"
        or str(edge.get("source_container") or "")
        == "trace.dataflow_edges"
    )
    authoritative = explicit or candidates
    return any(
        is_confirmation_causal_edge(
            edge,
            default_eligible=default_eligible,
        )
        for edge in authoritative
    )


def confirmation_edge_provenance_tier(
    edge: Mapping[str, Any],
) -> str:
    origin = " ".join(
        (
            str(edge.get("edge_origin") or ""),
            str(edge.get("source_container") or ""),
            str(edge.get("inference_method") or ""),
            str(edge.get("evidence_type") or ""),
        )
    ).strip().casefold()
    if "offline" in origin or "reconstruct" in origin:
        return OFFLINE_RECONSTRUCTION_PROVENANCE_TIER
    if (
        "trace.dataflow_edges" in origin
        or "record.source_refs" in origin
        or str(edge.get("evidence_type") or "").strip().casefold()
        in {"confirmed", "explicit_reference", "recorded"}
    ):
        return CONFIRMED_TRACE_PROVENANCE_TIER
    return ""


def build_tiered_confirmation_path(
    path_refs: Sequence[str],
    edges: Iterable[Mapping[str, Any]],
) -> dict:
    path = tuple(str(ref) for ref in path_refs)
    if len(path) < 2 or any(not ref for ref in path):
        raise ValueError("tiered path requires at least one grounded hop")
    edge_values = tuple(
        edge for edge in edges if isinstance(edge, Mapping)
    )
    segments: List[dict] = []
    for source_ref, target_ref in zip(path, path[1:]):
        candidates = tuple(
            edge
            for edge in edge_values
            if str(edge.get("from_ref") or "") == source_ref
            and str(edge.get("to_ref") or "") == target_ref
            and is_confirmation_causal_edge(
                edge,
                default_eligible=True,
            )
        )
        explicit = tuple(
            edge
            for edge in candidates
            if str(edge.get("edge_origin") or "")
            == "trace.dataflow_edges"
            or str(edge.get("source_container") or "")
            == "trace.dataflow_edges"
        )
        authoritative = explicit or candidates
        labelled = tuple(
            (confirmation_edge_provenance_tier(edge), edge)
            for edge in authoritative
            if confirmation_edge_provenance_tier(edge)
        )
        if not labelled:
            raise ValueError(
                "unlabelled path provenance for {0}->{1}".format(
                    source_ref,
                    target_ref,
                )
            )
        tier, edge = sorted(
            labelled,
            key=lambda item: (
                0
                if item[0] == CONFIRMED_TRACE_PROVENANCE_TIER
                else 1,
                str(item[1].get("relation") or ""),
                str(item[1].get("edge_origin") or ""),
            ),
        )[0]
        segments.append(
            {
                "from_ref": source_ref,
                "to_ref": target_ref,
                "relation": str(edge.get("relation") or ""),
                "provenance_tier": tier,
                "provenance": {
                    "edge_origin": str(edge.get("edge_origin") or ""),
                    "source_container": str(
                        edge.get("source_container") or ""
                    ),
                    "evidence_type": str(
                        edge.get("evidence_type") or ""
                    ),
                },
            }
        )
    tiers = {segment["provenance_tier"] for segment in segments}
    return {
        "path_refs": list(path),
        "tier": (
            next(iter(tiers))
            if len(tiers) == 1
            else MIXED_PROVENANCE_TIER
        ),
        "segments": segments,
    }


def validate_tiered_confirmation_path(value: Any) -> dict:
    if not isinstance(value, Mapping) or set(value) != {
        "path_refs",
        "tier",
        "segments",
    }:
        raise ValueError("tiered path schema mismatch")
    path = value.get("path_refs")
    segments = value.get("segments")
    if (
        not isinstance(path, (list, tuple))
        or len(path) < 2
        or any(type(ref) is not str or not ref for ref in path)
        or not isinstance(segments, (list, tuple))
        or len(segments) != len(path) - 1
    ):
        raise ValueError("tiered path refs or segments are invalid")
    canonical_segments = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping) or set(segment) != {
            "from_ref",
            "to_ref",
            "relation",
            "provenance_tier",
            "provenance",
        }:
            raise ValueError("unlabelled path provenance segment")
        tier = str(segment.get("provenance_tier") or "")
        provenance = segment.get("provenance")
        if tier not in PATH_PROVENANCE_TIERS:
            raise ValueError("unlabelled path provenance tier")
        if not isinstance(provenance, Mapping) or set(provenance) != {
            "edge_origin",
            "source_container",
            "evidence_type",
        }:
            raise ValueError("unlabelled path provenance facts")
        if (
            str(segment.get("from_ref") or "") != path[index]
            or str(segment.get("to_ref") or "") != path[index + 1]
            or not str(segment.get("relation") or "")
        ):
            raise ValueError("tiered path segment is disconnected")
        canonical_segments.append(
            {
                "from_ref": path[index],
                "to_ref": path[index + 1],
                "relation": str(segment["relation"]),
                "provenance_tier": tier,
                "provenance": {
                    key: str(provenance.get(key) or "")
                    for key in (
                        "edge_origin",
                        "source_container",
                        "evidence_type",
                    )
                },
            }
        )
    tiers = {segment["provenance_tier"] for segment in canonical_segments}
    expected_tier = (
        next(iter(tiers)) if len(tiers) == 1 else MIXED_PROVENANCE_TIER
    )
    if value.get("tier") != expected_tier:
        raise ValueError("tiered path summary contradicts segment provenance")
    return {
        "path_refs": list(path),
        "tier": expected_tier,
        "segments": canonical_segments,
    }
