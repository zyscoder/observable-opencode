"""Confirmation-stage causal path policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
        "candidate_informed_outcome",
        "change_created_observed_defect",
        "change_observed_by_evaluation",
        "context_available_to_change",
        "decision_exposed_by_evaluation",
        "decision_guided_claim",
        "decision_guided_change",
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


def is_confirmation_causal_edge(
    edge: Mapping[str, Any], *, default_eligible: bool
) -> bool:
    relation = str(edge.get("relation") or "").strip().lower()
    return (
        edge.get("eligible_for_attribution", default_eligible) is True
        and not is_temporal_only_edge(edge)
        and relation not in NON_CAUSAL_CONFIRMATION_RELATIONS
        and relation in CONFIRMATION_CAUSAL_RELATIONS
    )
