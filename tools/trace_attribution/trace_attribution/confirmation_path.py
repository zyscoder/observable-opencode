"""Confirmation-stage causal path policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


NON_CAUSAL_CONFIRMATION_RELATIONS = frozenset(
    {
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
    return bool(edge.get("eligible_for_attribution", default_eligible)) and str(
        edge.get("relation") or ""
    ).strip().lower() not in NON_CAUSAL_CONFIRMATION_RELATIONS
