"""Layered, provenance-preserving predecessor retrieval for recursive attribution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Iterable, List, Sequence, Tuple

from .causal_state import AttributionHypothesis, CausalCandidate, DefectState
from .episodes import CausalEpisodeIndex
from .graph import TraceGraph, is_temporal_only_edge
from .models import TraceNode
from .progress import progress_navigation_window


SIBLING_REFERENCE_KEYS = (
    "candidate_context_refs",
    "grounding_candidate_refs",
    "selected_context_refs",
    "verification_refs",
    "artifact_refs",
)


class SemanticPredecessorRetriever:
    def retrieve(
        self,
        graph: TraceGraph,
        node_ref: str,
        defect_state: DefectState,
        hypothesis: AttributionHypothesis,
        *,
        limit: int = 24,
        allow_semantic_fallback: bool = False,
    ) -> List[CausalCandidate]:
        if limit <= 0:
            return []
        resolved = graph.resolve(node_ref) or node_ref
        if resolved not in graph.nodes:
            return []
        layers = [
            self._direct_candidates(graph, resolved),
            self._episode_candidates(graph, resolved),
            self._sibling_candidates(graph, resolved),
        ]
        if allow_semantic_fallback:
            layers.append(self._semantic_candidates(graph, resolved, defect_state, hypothesis))
        return merge_ranked_candidates(layers, limit=limit)

    def _direct_candidates(self, graph: TraceGraph, node_ref: str) -> List[CausalCandidate]:
        candidates: List[CausalCandidate] = []
        for edge in graph.semantic_predecessor_edges(node_ref):
            ref = str(edge.get("ref") or "")
            node = graph.nodes.get(ref)
            if not node or is_navigation_node(node) or is_temporal_only_edge(edge):
                continue
            evidence_type = str(edge.get("evidence_type") or "")
            source = "confirmed_edge" if evidence_type in {"confirmed", "content_matched"} else "attribution_edge"
            candidates.append(
                CausalCandidate(
                    ref=ref,
                    node=node,
                    source=source,
                    edge=edge,
                    score=float(edge.get("confidence") or 0.0),
                    evidence_refs=edge_evidence_refs(edge),
                )
            )
        return candidates

    def _episode_candidates(self, graph: TraceGraph, node_ref: str) -> List[CausalCandidate]:
        current = graph.nodes[node_ref]
        refs: List[str] = []
        source = "episode_candidate"
        if current.event_type == "progress.episode":
            window = progress_navigation_window(graph.nodes, node_ref)
            refs = [str(item) for item in window.get("member_refs") or []]
            source = "progress_window"
        else:
            episode = CausalEpisodeIndex.from_graph(graph).episode_for(node_ref)
            refs = [
                ref
                for ref in episode.member_refs
                if graph.position(ref) < graph.position(node_ref)
                and graph.nodes.get(ref)
                and not is_navigation_node(graph.nodes[ref])
            ]
        candidates: List[CausalCandidate] = []
        for ref in dedupe_refs(graph, refs):
            node = graph.nodes.get(ref)
            if not node or ref == node_ref or is_navigation_node(node):
                continue
            candidates.append(
                CausalCandidate(
                    ref=ref,
                    node=node,
                    source=source,
                    edge={
                        "from_ref": ref,
                        "to_ref": node_ref,
                        "relation": "progress_window_candidate" if source == "progress_window" else "episode_member",
                        "evidence_type": "offline_reconstruction",
                        "evidence_refs": [ref],
                        "confidence": 0.7,
                        "eligible_for_attribution": False,
                        "retrieval_candidate": True,
                        "inference_method": "delivery_bounded_progress_window"
                        if source == "progress_window"
                        else "causal_episode_membership",
                        "edge_origin": "offline.progress_retrieval",
                    },
                    score=0.7,
                    evidence_refs=(ref,),
                )
            )
        return candidates

    def _sibling_candidates(self, graph: TraceGraph, node_ref: str) -> List[CausalCandidate]:
        current = graph.nodes[node_ref]
        direct_refs = {item.ref for item in self._direct_candidates(graph, node_ref)}
        refs = referenced_sibling_refs(graph, current)
        message_id = identity_value(current, "messageid", "message_id")
        if message_id:
            refs.extend(
                node.ref
                for node in graph.nodes.values()
                if graph.position(node.ref) < graph.position(node_ref)
                and not is_navigation_node(node)
                and identity_value(node, "messageid", "message_id") == message_id
            )
        candidates: List[CausalCandidate] = []
        for ref in dedupe_refs(graph, refs):
            node = graph.nodes.get(ref)
            if not node or ref == node_ref or ref in direct_refs or is_navigation_node(node):
                continue
            candidates.append(
                CausalCandidate(
                    ref=ref,
                    node=node,
                    source="sibling_context",
                    edge={
                        "from_ref": ref,
                        "to_ref": node_ref,
                        "relation": "sibling_context_candidate",
                        "evidence_type": "recorded_context_reference",
                        "evidence_refs": [ref],
                        "confidence": 0.55,
                        "eligible_for_attribution": False,
                        "retrieval_candidate": True,
                        "inference_method": "candidate_context_or_message_scope",
                        "edge_origin": "offline.sibling_retrieval",
                    },
                    score=0.55,
                    evidence_refs=(ref,),
                )
            )
        return candidates

    def _semantic_candidates(
        self,
        graph: TraceGraph,
        node_ref: str,
        defect_state: DefectState,
        hypothesis: AttributionHypothesis,
    ) -> List[CausalCandidate]:
        terms = semantic_terms(defect_state, hypothesis)
        candidates: List[CausalCandidate] = []
        for match in graph.semantic_search(terms, before_ref=node_ref, limit=24):
            ref = str(match["ref"])
            node = graph.nodes.get(ref)
            if not node:
                continue
            score = float(match["score"])
            candidates.append(
                CausalCandidate(
                    ref=ref,
                    node=node,
                    source="semantic_fallback",
                    edge={
                        "from_ref": ref,
                        "to_ref": node_ref,
                        "relation": "semantic_predecessor_match",
                        "evidence_type": "semantic_inferred",
                        "evidence_refs": [ref],
                        "confidence": score,
                        "eligible_for_attribution": False,
                        "retrieval_candidate": True,
                        "inference_method": "token_overlap_retrieval",
                        "edge_origin": "offline.semantic_retrieval",
                    },
                    score=score,
                    evidence_refs=(ref,),
                )
            )
        return candidates


def merge_ranked_candidates(layers: Sequence[Sequence[CausalCandidate]], *, limit: int) -> List[CausalCandidate]:
    """Deduplicate by ref while preserving the highest-provenance first occurrence."""
    selected: List[Tuple[int, int, CausalCandidate]] = []
    positions = {}
    for layer_index, layer in enumerate(layers):
        for ordinal, candidate in enumerate(layer):
            existing = positions.get(candidate.ref)
            if existing is None:
                positions[candidate.ref] = len(selected)
                selected.append((layer_index, ordinal, candidate))
                continue
            existing_layer, existing_ordinal, existing_candidate = selected[existing]
            if layer_index == existing_layer and candidate.score > existing_candidate.score:
                selected[existing] = (layer_index, min(existing_ordinal, ordinal), candidate)
    selected.sort(key=lambda item: (item[0], -item[2].score, item[1], item[2].ref))
    return [item[2] for item in selected[:limit]]


def edge_evidence_refs(edge: Mapping[str, Any]) -> Tuple[str, ...]:
    refs = edge.get("evidence_refs")
    if isinstance(refs, (list, tuple)):
        return tuple(str(item) for item in refs)
    edge_id = str(edge.get("edge_id") or "")
    return (edge_id,) if edge_id else ()


def referenced_sibling_refs(graph: TraceGraph, node: TraceNode) -> List[str]:
    refs: List[str] = []
    for key in SIBLING_REFERENCE_KEYS:
        value = node.data.get(key)
        values = value if isinstance(value, (list, tuple)) else [value]
        for raw_ref in values:
            if raw_ref in (None, ""):
                continue
            resolved = graph.resolve(str(raw_ref)) or str(raw_ref)
            if resolved in graph.nodes:
                refs.append(resolved)
    return refs


def dedupe_refs(graph: TraceGraph, refs: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for raw_ref in refs:
        ref = graph.resolve(str(raw_ref)) or str(raw_ref)
        if ref in seen:
            continue
        seen.add(ref)
        output.append(ref)
    return output


def semantic_terms(defect_state: DefectState, hypothesis: AttributionHypothesis) -> List[str]:
    source = " ".join(
        (
            defect_state.label,
            defect_state.expected,
            defect_state.actual,
            defect_state.mechanism,
            defect_state.scope,
            hypothesis.claim,
        )
    )
    return re.findall(r"[A-Za-z0-9_]{3,}", source)


def identity_value(node: TraceNode, *keys: str) -> str:
    normalized = {key.lower() for key in keys}
    stack = [node.data]
    while stack:
        value = stack.pop()
        if not isinstance(value, Mapping):
            continue
        for key, child in value.items():
            if str(key).lower() in normalized and child not in (None, ""):
                return str(child)
            if isinstance(child, Mapping):
                stack.append(child)
    return ""


def is_navigation_node(node: TraceNode) -> bool:
    role = str(node.data.get("semantic_role") or node.data.get("navigation_role") or "").strip().lower()
    return node.event_type == "progress.episode" or bool(node.data.get("offline_only")) or role in {
        "aggregate",
        "navigation",
        "progress_episode",
    }
