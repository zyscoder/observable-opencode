from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional

from .causal_state import AttributionHypothesis, CausalCandidate, CausalStepJudgment, DefectState
from .episodes import CausalEpisodeIndex
from .graph import TraceGraph
from .models import JsonDict, NodeJudgment, TraceNode, stable_json
from .progress import progress_navigation_window


JUDGMENT_CONTEXT_VERSION = "1.0"
EPISODE_MEMBER_LIMIT = 16


def build_causal_judgment_context(
    *,
    graph: TraceGraph,
    node_ref: str,
    path: List[str],
    judgments: Dict[str, NodeJudgment],
    episode_index: CausalEpisodeIndex,
    objective: str,
) -> JsonDict:
    resolved = graph.resolve(node_ref) or node_ref
    upstream_refs = [item.ref for item in graph.upstream_nodes(resolved)]
    outgoing_edges = active_path_outgoing_edges(graph, resolved, path)
    progress_episode = progress_episode_context(graph, resolved)
    downstream_judgments = [
        compact_judgment(judgments[ref])
        for ref in refs_before_current(path, resolved)
        if ref in judgments
    ]
    context = {
        "context_version": JUDGMENT_CONTEXT_VERSION,
        "behavior_impact": "none_offline_analysis_only",
        "active_defect": build_active_defect_fingerprint(
            graph=graph,
            path=path,
            objective=objective,
        ),
        "current_ref": resolved,
        "incoming_edges": graph.incoming_edge_context(resolved, allowed_refs=upstream_refs),
        "outgoing_edges_on_active_path": outgoing_edges,
        "downstream_judgments": downstream_judgments,
        "causal_episode": causal_episode_context(graph, episode_index, resolved),
        "progress_episode": progress_episode,
        "progress_navigation_window": progress_navigation_context(
            graph=graph,
            current_ref=resolved,
            path=path,
        ),
    }
    context["context_manifest"] = {
        "upstream_candidate_count": len(upstream_refs),
        "incoming_edge_count": len(context["incoming_edges"]),
        "active_path_edge_count": len(outgoing_edges),
        "downstream_judgment_count": len(downstream_judgments),
        "has_progress_episode": bool(progress_episode),
        "has_progress_navigation_window": bool(context["progress_navigation_window"]),
        "missing_active_defect_fields": [
            key
            for key in ("expected", "actual", "mechanism")
            if not context["active_defect"].get(key)
        ],
    }
    return context


def build_recursive_judgment_context(
    request: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> JsonDict:
    """Build Judge facts for a recursive branch without assigning candidate defect status.

    ``request`` accepts the same named fields as keyword arguments so checkpointed
    callers can persist and replay the input as a JSON mapping.
    """
    values: Dict[str, Any] = dict(request or {})
    values.update(kwargs)
    graph = values["graph"]
    if not isinstance(graph, TraceGraph):
        raise TypeError("graph must be a TraceGraph")
    node_ref = str(values["node_ref"])
    defect_state = values["defect_state"]
    hypothesis = values["hypothesis"]
    if not isinstance(defect_state, DefectState):
        raise TypeError("defect_state must be a DefectState")
    if not isinstance(hypothesis, AttributionHypothesis):
        raise TypeError("hypothesis must be an AttributionHypothesis")
    resolved = graph.resolve(node_ref) or node_ref
    if resolved not in graph.nodes:
        raise KeyError("unknown recursive judgment node: {0}".format(node_ref))
    candidates = list(values.get("candidates") or [])
    if not all(isinstance(item, CausalCandidate) for item in candidates):
        raise TypeError("candidates must contain CausalCandidate values")
    downstream_path = [graph.resolve(str(item)) or str(item) for item in values.get("downstream_path") or []]
    if not downstream_path:
        downstream_path = [resolved]
    downstream_judgments = list(values.get("downstream_judgments") or [])
    transformation_chain = normalize_defect_chain(
        values.get("defect_transformation_chain") or [], defect_state
    )
    objective = str(values.get("objective") or "")
    episode_index = CausalEpisodeIndex.from_graph(graph)
    graph.hydrate_node(resolved)
    candidate_context = [recursive_candidate_context(graph, item) for item in candidates]
    legacy_judgments = {
        item.node_ref: item for item in downstream_judgments if isinstance(item, NodeJudgment)
    }
    legacy_context = build_causal_judgment_context(
        graph=graph,
        node_ref=resolved,
        path=downstream_path,
        judgments=legacy_judgments,
        episode_index=episode_index,
        objective=objective,
    )
    context = {
        "context_version": "2.0",
        "behavior_impact": "none_offline_analysis_only",
        "current_ref": resolved,
        "defect_state": defect_state.to_dict(),
        "defect_transformation_chain": [item.to_dict() for item in transformation_chain],
        "hypothesis": hypothesis.to_dict(),
        "candidate_predecessors": candidate_context,
        "downstream_path": downstream_path,
        "downstream_judgments": [compact_recursive_judgment(item) for item in downstream_judgments],
        "task_obligations": task_obligations(graph, objective),
        "agent_scope": agent_scope(graph.hydrate_node(resolved)),
        "causal_episode": legacy_context["causal_episode"],
        "progress_episode": legacy_context["progress_episode"],
        "progress_navigation_window": legacy_context["progress_navigation_window"],
        "incoming_edges": legacy_context["incoming_edges"],
        "outgoing_edges_on_active_path": legacy_context["outgoing_edges_on_active_path"],
        "temporal_adjacency": temporal_adjacency_context(graph, resolved),
        "artifact_hydration": dict(graph.artifact_hydration),
    }
    context["context_manifest"] = {
        "candidate_count": len(candidate_context),
        "defect_transformation_count": len(transformation_chain),
        "downstream_path_length": len(downstream_path),
        "downstream_judgment_count": len(downstream_judgments),
        "obligation_count": len(context["task_obligations"]),
        "hydrated_candidate_count": len(candidate_context),
        "temporal_adjacency_count": len(context["temporal_adjacency"]),
        "legacy_context_manifest": legacy_context["context_manifest"],
    }
    return context


def normalize_defect_chain(value: Iterable[Any], active: DefectState) -> List[DefectState]:
    chain = [item for item in value if isinstance(item, DefectState)]
    if not chain or chain[-1].fingerprint != active.fingerprint:
        chain.append(active)
    return chain


def recursive_candidate_context(graph: TraceGraph, candidate: CausalCandidate) -> JsonDict:
    hydrated = graph.hydrate_node(candidate.ref)
    return {
        "ref": candidate.ref,
        "source": candidate.source,
        "score": candidate.score,
        "edge": dict(candidate.edge),
        "evidence_refs": list(candidate.evidence_refs),
        "node": hydrated.compact(),
    }


def compact_recursive_judgment(value: Any) -> JsonDict:
    if isinstance(value, CausalStepJudgment):
        return value.to_dict()
    if isinstance(value, NodeJudgment):
        return compact_judgment(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("downstream judgments must be causal judgments or mappings")


def task_obligations(graph: TraceGraph, objective: str) -> List[JsonDict]:
    obligations: List[JsonDict] = []
    if objective:
        obligations.append({"source": "analysis_objective", "text": objective})
    manifest = graph.raw_trace.get("manifest")
    sources = [manifest] if isinstance(manifest, dict) else []
    sources.append(graph.raw_trace)
    for source in sources:
        for key in ("task_obligations", "obligations", "requirements"):
            value = source.get(key) if isinstance(source, Mapping) else None
            entries = value if isinstance(value, list) else [value]
            for entry in entries:
                if entry in (None, "", [], {}):
                    continue
                text = entry if isinstance(entry, str) else stable_json(entry)
                obligation = {"source": "trace.{0}".format(key), "text": text}
                if obligation not in obligations:
                    obligations.append(obligation)
    return obligations


def agent_scope(node: TraceNode) -> JsonDict:
    values = collect_identity_values(node.data)
    return {
        "session_id": values.get("sessionid") or values.get("session_id") or "",
        "agent_id": values.get("agentid") or values.get("agent_id") or "",
        "subagent_id": values.get("subagentid") or values.get("subagent_id") or "",
        "message_id": values.get("messageid") or values.get("message_id") or "",
    }


def collect_identity_values(value: Any) -> Dict[str, str]:
    output: Dict[str, str] = {}
    if not isinstance(value, Mapping):
        return output
    for key, child in value.items():
        normalized = str(key).lower()
        if normalized in {"sessionid", "session_id", "agentid", "agent_id", "subagentid", "subagent_id", "messageid", "message_id"}:
            if child not in (None, ""):
                output[normalized] = str(child)
        if isinstance(child, Mapping):
            output.update({key: item for key, item in collect_identity_values(child).items() if key not in output})
    return output


def temporal_adjacency_context(graph: TraceGraph, node_ref: str) -> List[JsonDict]:
    """Expose temporal-only facts to the Judge without promoting them to graph predecessors."""
    output: List[JsonDict] = []
    for edge in graph.raw_trace.get("dataflow_edges") or []:
        if not isinstance(edge, Mapping):
            continue
        metadata = edge.get("metadata") if isinstance(edge.get("metadata"), Mapping) else {}
        evidence_type = str(edge.get("evidence_type") or metadata.get("evidence_type") or "")
        if evidence_type != "temporal_inferred":
            continue
        target = raw_edge_ref(graph, edge.get("to"))
        if target != node_ref:
            continue
        source = raw_edge_ref(graph, edge.get("from"))
        if not source:
            continue
        output.append(
            {
                "from_ref": source,
                "to_ref": target,
                "relation": str(edge.get("relation") or metadata.get("relation") or "temporal_availability"),
                "evidence_type": "temporal_inferred",
                "evidence_refs": list(edge.get("evidence_refs") or metadata.get("evidence_refs") or []),
                "confidence": edge.get("confidence", metadata.get("confidence", 0.0)),
                "eligible_for_attribution": False,
                "inference_method": str(
                    edge.get("inference_method") or metadata.get("inference_method") or "temporal_adjacency"
                ),
                "edge_origin": "trace.dataflow_edges",
            }
        )
    for edge in graph.message_lineage.get("edges") or []:
        if not isinstance(edge, Mapping) or str(edge.get("evidence_type") or "") != "temporal_inferred":
            continue
        target = graph.resolve(str(edge.get("to_ref") or "")) or str(edge.get("to_ref") or "")
        if target != node_ref:
            continue
        source = graph.resolve(str(edge.get("from_ref") or "")) or str(edge.get("from_ref") or "")
        if source:
            output.append({**dict(edge), "from_ref": source, "to_ref": target, "eligible_for_attribution": False})
    output.sort(key=lambda item: (graph.position(str(item["from_ref"])), str(item.get("relation") or "")))
    return output


def raw_edge_ref(graph: TraceGraph, value: Any) -> str:
    if isinstance(value, Mapping):
        ref_type = str(value.get("type") or "")
        ref_id = str(value.get("id") or "")
        candidates = ["{0}:{1}".format(ref_type, ref_id), "record:{0}".format(ref_id)]
    else:
        candidates = [str(value or "")]
    for candidate in candidates:
        resolved = graph.resolve(candidate)
        if resolved:
            return resolved
    return ""


def build_active_defect_fingerprint(*, graph: TraceGraph, path: List[str], objective: str) -> JsonDict:
    observed_ref = graph.resolve(path[0]) or path[0] if path else ""
    observed = graph.nodes.get(observed_ref)
    data = observed.data if observed else {}
    expected = first_text(
        data,
        "expected",
        "expected_behavior",
        "requirement",
        "obligation",
        "target_behavior",
    ) or objective
    actual = first_text(
        data,
        "actual",
        "actual_behavior",
        "observed",
        "description",
        "defect_reason",
        "text",
    )
    mechanism = first_text(data, "failure_type", "gap_kind", "defect_type", "dimension", "reason")
    scope = first_value(data, "scope", "affected_scope", "files", "component")
    payload = {
        "observed_ref": observed_ref,
        "observed_event_type": observed.event_type if observed else "",
        "expected": expected,
        "actual": actual,
        "mechanism": mechanism,
        "scope": scope,
        "temporal_anchor": {
            "timestamp": observed.timestamp if observed else "",
            "repository_revision": data.get("repository_revision") or "",
            "status": observed.status if observed else "",
        },
    }
    fingerprint_source = {key: payload[key] for key in ("expected", "actual", "mechanism", "scope")}
    payload["fingerprint"] = hashlib.sha256(stable_json(fingerprint_source).encode("utf-8")).hexdigest()[:20]
    return payload


def active_path_outgoing_edges(graph: TraceGraph, current_ref: str, path: List[str]) -> List[JsonDict]:
    resolved_path = [graph.resolve(ref) or ref for ref in path]
    try:
        current_index = len(resolved_path) - 1 - resolved_path[::-1].index(current_ref)
    except ValueError:
        return []
    if current_index == 0:
        return []
    downstream_ref = resolved_path[current_index - 1]
    edges = graph.edge_context(current_ref, downstream_ref)
    if edges:
        return edges
    downstream = graph.nodes.get(downstream_ref)
    if downstream and downstream.event_type == "progress.episode":
        window = progress_navigation_window(graph.nodes, downstream_ref)
        if current_ref in (window.get("member_refs") or []):
            evidence_refs = [
                episode_ref
                for episode_ref in window.get("member_episode_refs") or []
                if current_ref in (graph.nodes[episode_ref].data.get("member_refs") or [])
            ]
            return [
                {
                    "from_ref": current_ref,
                    "to_ref": downstream_ref,
                    "relation": "progress_window_member",
                    "evidence_type": "offline_reconstruction",
                    "evidence_refs": evidence_refs,
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                    "inference_method": str(
                        window.get("navigation_method")
                        or "delivery_bounded_no_delivery_window_v1"
                    ),
                    "edge_origin": "offline.progress_navigation",
                }
            ]
    return [
        {
            "from_ref": current_ref,
            "to_ref": downstream_ref,
            "relation": "backward_path_predecessor",
            "evidence_type": "analyzer_traversal",
            "evidence_refs": [],
            "confidence": 1.0,
            "eligible_for_attribution": True,
            "inference_method": "active_backward_path",
            "edge_origin": "offline.analyzer_path",
        }
    ]


def refs_before_current(path: List[str], current_ref: str) -> List[str]:
    output: List[str] = []
    for ref in path:
        if ref == current_ref:
            break
        output.append(ref)
    return output


def compact_judgment(judgment: NodeJudgment) -> JsonDict:
    return {
        "node_ref": judgment.node_ref,
        "component": judgment.component,
        "event_type": judgment.event_type,
        "defect_status": judgment.defect_status,
        "defect_type": judgment.defect_type,
        "defect_reason": judgment.defect_reason,
        "causal_role": judgment.causal_role,
        "branch_relation": judgment.branch_relation,
        "influenced_by": [
            {
                "upstream_ref": item.upstream_ref,
                "relation": item.relation,
                "reason": item.reason,
                "confidence": item.confidence,
            }
            for item in judgment.influenced_by
        ],
        "confidence": judgment.confidence,
    }


def causal_episode_context(
    graph: TraceGraph,
    episode_index: CausalEpisodeIndex,
    node_ref: str,
) -> JsonDict:
    episode = episode_index.episode_for(node_ref)
    refs = episode.member_refs[:EPISODE_MEMBER_LIMIT]
    return {
        "episode_id": episode.episode_id,
        "member_refs": refs,
        "member_count": len(episode.member_refs),
        "members_truncated": len(refs) < len(episode.member_refs),
        "member_summaries": [compact_node_summary(graph.hydrate_node(ref)) for ref in refs],
    }


def progress_episode_context(graph: TraceGraph, node_ref: str) -> JsonDict:
    candidates = [
        node
        for node in graph.nodes.values()
        if node.event_type == "progress.episode" and node_ref in (node.data.get("member_refs") or [])
    ]
    if not candidates:
        return {}
    episode = max(candidates, key=lambda item: graph.position(item.ref))
    data = episode.data
    keys = (
        "phase",
        "member_refs",
        "candidate_member_refs",
        "excluded_member_refs",
        "candidate_selection_method",
        "previous_episode_ref",
        "reasoning_count",
        "action_count",
        "search_read_count",
        "mutation_count",
        "verification_count",
        "delegation_count",
        "error_count",
        "no_delivery_progress",
        "cumulative_change_count",
        "cumulative_verification_count",
        "consecutive_no_delivery_episodes",
        "chronology_index",
        "start_timestamp",
        "end_timestamp",
    )
    return {
        "ref": episode.ref,
        "event_type": episode.event_type,
        **{key: data[key] for key in keys if data.get(key) not in (None, "", [], {})},
    }


def progress_navigation_context(*, graph: TraceGraph, current_ref: str, path: List[str]) -> JsonDict:
    anchor_ref = ""
    current = graph.nodes.get(current_ref)
    if current and current.event_type == "progress.episode":
        anchor_ref = current_ref
    else:
        for ref in reversed(path[:-1]):
            resolved = graph.resolve(ref) or ref
            node = graph.nodes.get(resolved)
            if node and node.event_type == "progress.episode":
                anchor_ref = resolved
                break
    if not anchor_ref:
        return {}
    return progress_navigation_window(graph.nodes, anchor_ref)


def compact_node_summary(node: TraceNode) -> JsonDict:
    data = node.data
    return {
        "ref": node.ref,
        "component": node.component,
        "event_type": node.event_type,
        "status": node.status,
        **{
            key: data[key]
            for key in ("decision_type", "rationale", "chosen_action", "tool_name", "path", "exit_code")
            if data.get(key) not in (None, "", [], {})
        },
    }


def first_text(data: JsonDict, *keys: str) -> str:
    value = first_value(data, *keys)
    if isinstance(value, str):
        return value
    if value in (None, "", [], {}):
        return ""
    return stable_json(value)


def first_value(data: JsonDict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""
