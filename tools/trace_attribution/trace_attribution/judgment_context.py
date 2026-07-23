from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional

from .causal_state import AttributionHypothesis, CausalCandidate, CausalStepJudgment, DefectState
from .episodes import CausalEpisodeIndex
from .graph import TraceGraph
from .models import JsonDict, NodeJudgment, TraceNode, stable_json
from .progress import (
    active_progress_episode_data,
    active_progress_navigation_window,
)


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
    return sanitize_judge_evidence_payload(graph, context)


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
    defect_state = coerce_defect_state(values["defect_state"])
    hypothesis = coerce_hypothesis(values["hypothesis"])
    resolved = graph.resolve(node_ref) or node_ref
    if resolved not in graph.nodes:
        raise KeyError("unknown recursive judgment node: {0}".format(node_ref))
    candidates = [coerce_candidate(item) for item in values.get("candidates") or []]
    downstream_path_raw = [str(item) for item in values.get("downstream_path") or []]
    downstream_path = [graph.resolve(item) or item for item in downstream_path_raw]
    if (
        not downstream_path
        or any(
            ref not in graph.nodes
            or not graph.active_revision_evidence_eligible(ref)
            for ref in downstream_path
        )
    ):
        downstream_path_raw = [resolved]
        downstream_path = [resolved]
    downstream_judgments = [coerce_recursive_judgment(item) for item in values.get("downstream_judgments") or []]
    transformation_chain = normalize_defect_chain(
        values.get("defect_transformation_chain") or [], defect_state
    )
    objective = str(values.get("objective") or "")
    episode_index = CausalEpisodeIndex.from_graph(graph)
    graph.hydrate_node(resolved)
    candidate_context = [recursive_candidate_context(graph, item) for item in candidates]
    downstream_path_references = [ground_reference(graph, item, "recorded") for item in downstream_path_raw]
    hypothesis_evidence_references = grounded_hypothesis_evidence(graph, hypothesis)
    hypothesis_unresolved_references = [
        ground_reference(graph, evidence.ref, "recorded")
        for evidence in (
            *hypothesis.supporting_evidence,
            *hypothesis.opposing_evidence,
        )
        if not graph.filter_evidence_refs([evidence.ref])
    ]
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
        "current_reference": ground_reference(graph, node_ref, "recorded"),
        "defect_state": defect_state.to_dict(),
        "defect_transformation_chain": [item.to_dict() for item in transformation_chain],
        "hypothesis": hypothesis.to_dict(),
        "hypothesis_evidence_references": hypothesis_evidence_references,
        "candidate_predecessors": candidate_context,
        "downstream_path": downstream_path,
        "downstream_path_references": downstream_path_references,
        "downstream_judgments": [compact_recursive_judgment(item) for item in downstream_judgments],
        "task_obligations": task_obligations(graph, objective),
        "agent_scope": agent_scope(graph.hydrate_node(resolved)),
        "causal_episode": legacy_context.get("causal_episode", {}),
        "progress_episode": legacy_context.get("progress_episode", {}),
        "progress_navigation_window": legacy_context.get(
            "progress_navigation_window", {}
        ),
        "incoming_edges": legacy_context.get("incoming_edges", []),
        "outgoing_edges_on_active_path": legacy_context.get(
            "outgoing_edges_on_active_path", []
        ),
        "temporal_adjacency": temporal_adjacency_context(graph, resolved),
        "artifact_hydration": dict(graph.artifact_hydration),
    }
    unresolved_references = unresolved_context_references(
        downstream_path_references,
        candidate_context,
        [
            *hypothesis_evidence_references,
            *hypothesis_unresolved_references,
        ],
    )
    missing_artifacts, truncated_artifacts = artifact_context_gaps(candidate_context)
    context["unresolved_references"] = unresolved_references
    context["missing_artifacts"] = missing_artifacts
    context["truncated_artifacts"] = truncated_artifacts
    context["context_manifest"] = {
        "candidate_count": len(candidate_context),
        "defect_transformation_count": len(transformation_chain),
        "downstream_path_length": len(downstream_path),
        "downstream_judgment_count": len(downstream_judgments),
        "obligation_count": len(context["task_obligations"]),
        "hydrated_candidate_count": len(candidate_context),
        "temporal_adjacency_count": len(context["temporal_adjacency"]),
        "unresolved_reference_count": len(unresolved_references),
        "missing_artifact_count": len(missing_artifacts),
        "truncated_artifact_count": len(truncated_artifacts),
        "legacy_context_manifest": legacy_context["context_manifest"],
    }
    sanitized = sanitize_judge_evidence_payload(graph, context)
    manifest = dict(sanitized.get("context_manifest") or {})
    manifest.update(
        {
            "candidate_count": len(sanitized.get("candidate_predecessors") or ()),
            "downstream_path_length": len(sanitized.get("downstream_path") or ()),
            "downstream_judgment_count": len(
                sanitized.get("downstream_judgments") or ()
            ),
            "hydrated_candidate_count": sum(
                1
                for item in sanitized.get("candidate_predecessors") or ()
                if isinstance(item, Mapping) and item.get("artifact_hydration")
            ),
            "unresolved_reference_count": len(
                sanitized.get("unresolved_references") or ()
            ),
            "missing_artifact_count": len(
                sanitized.get("missing_artifacts") or ()
            ),
            "truncated_artifact_count": len(
                sanitized.get("truncated_artifacts") or ()
            ),
        }
    )
    sanitized["context_manifest"] = manifest
    return sanitized


def sanitize_judge_evidence_payload(
    graph: TraceGraph,
    value: Any,
    *,
    field_name: str = "",
) -> Any:
    del field_name
    return graph.sanitize_judge_visible_payload(value)


def normalize_defect_chain(value: Iterable[Any], active: DefectState) -> List[DefectState]:
    chain = [coerce_defect_state(item) for item in value]
    if not chain or chain[-1].fingerprint != active.fingerprint:
        chain.append(active)
    return chain


def recursive_candidate_context(graph: TraceGraph, candidate: CausalCandidate) -> JsonDict:
    hydrated = graph.hydrate_node(candidate.ref)
    raw_edge = candidate.edge
    edge = graph.sanitize_judge_edge_evidence(candidate.edge)
    candidate_evidence_refs = graph.filter_evidence_refs(candidate.evidence_refs)
    raw_evidence_refs = dedupe_raw_refs(
        list(raw_edge.get("evidence_refs") or [])
        + list(candidate.evidence_refs)
        + graph.unresolved_edge_evidence_refs(
            str(raw_edge.get("from_ref") or candidate.ref),
            str(raw_edge.get("to_ref") or ""),
        )
    )
    all_edge_evidence_references = [
        ground_reference(graph, ref, edge_provenance_class(edge))
        for ref in raw_evidence_refs
    ]
    edge_evidence_references = [
        item
        for item in all_edge_evidence_references
        if item.get("resolution_status") == "resolved"
    ]
    endpoint_references = {
        "from": ground_reference(
            graph,
            raw_edge.get("from_ref") or candidate.ref,
            edge_provenance_class(edge),
        ),
        "to": ground_reference(
            graph, raw_edge.get("to_ref"), edge_provenance_class(edge)
        ),
    }
    return {
        "ref": candidate.ref,
        "reference": ground_reference(graph, candidate.ref, edge_provenance_class(edge)),
        "source": candidate.source,
        "edge": edge,
        "edge_endpoint_references": {
            key: value
            for key, value in endpoint_references.items()
            if value.get("resolution_status") == "resolved"
        },
        "evidence_refs": candidate_evidence_refs,
        "edge_evidence_references": edge_evidence_references,
        "artifact_hydration": graph.artifact_hydration_manifest(candidate.ref),
        "node": hydrated.compact(),
        "unresolved_references": [
            *(
                item
                for item in endpoint_references.values()
                if item.get("resolution_status") != "resolved"
            ),
            *(
                item
                for item in all_edge_evidence_references
                if item.get("resolution_status") != "resolved"
            ),
        ],
    }


def compact_recursive_judgment(value: Any) -> JsonDict:
    if isinstance(value, CausalStepJudgment):
        return value.to_dict()
    if isinstance(value, NodeJudgment):
        return compact_judgment(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("downstream judgments must be causal judgments or mappings")


def coerce_defect_state(value: Any) -> DefectState:
    if isinstance(value, DefectState):
        return value
    if isinstance(value, Mapping):
        return DefectState.from_dict(dict(value))
    raise TypeError("defect_state must be a DefectState or serialized DefectState mapping")


def coerce_hypothesis(value: Any) -> AttributionHypothesis:
    if isinstance(value, AttributionHypothesis):
        return value
    if isinstance(value, Mapping):
        return AttributionHypothesis.from_dict(dict(value))
    raise TypeError("hypothesis must be an AttributionHypothesis or serialized mapping")


def coerce_candidate(value: Any) -> CausalCandidate:
    if isinstance(value, CausalCandidate):
        return value
    if isinstance(value, Mapping):
        return CausalCandidate.from_dict(dict(value))
    raise TypeError("candidates must contain CausalCandidate values or serialized mappings")


def coerce_recursive_judgment(value: Any) -> Any:
    if isinstance(value, (CausalStepJudgment, NodeJudgment)):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        if "current_node_ref" in data:
            return CausalStepJudgment.from_dict(data)
        return data
    raise TypeError("downstream judgments must be causal judgments or mappings")


def ground_reference(graph: TraceGraph, raw_ref: Any, provenance_class: str) -> JsonDict:
    raw = str(raw_ref or "")
    resolved = graph.resolve(raw)
    if (
        resolved in graph.nodes
        and graph.active_revision_evidence_eligible(resolved)
    ):
        return {
            "raw_ref": raw,
            "resolved_ref": resolved,
            "provenance_class": provenance_class,
            "resolution_status": "resolved",
        }
    artifact_status = graph.artifact_reference_status(raw)
    if artifact_status:
        return {
            "raw_ref": raw,
            "resolved_ref": artifact_status["canonical_ref"],
            "provenance_class": "recorded",
            "resolution_status": (
                "resolved"
                if artifact_status.get("availability") == "available"
                else "unresolved"
            ),
            "reference_kind": "artifact",
            "artifact_status": artifact_status,
        }
    return {
        "raw_ref": raw,
        "resolved_ref": "",
        "provenance_class": provenance_class,
        "resolution_status": "unresolved",
    }


def edge_provenance_class(edge: Mapping[str, Any]) -> str:
    evidence_type = str(edge.get("evidence_type") or "").lower()
    origin = str(edge.get("edge_origin") or "").lower()
    if evidence_type in {"semantic_inferred", "temporal_inferred"} or "semantic" in origin:
        return "inferred"
    if "offline" in origin or "reconstruction" in evidence_type:
        return "reconstructed"
    return "recorded"


def dedupe_raw_refs(values: Iterable[Any]) -> List[str]:
    output: List[str] = []
    seen = set()
    for value in values:
        ref = str(value or "")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        output.append(ref)
    return output


def grounded_hypothesis_evidence(graph: TraceGraph, hypothesis: AttributionHypothesis) -> List[JsonDict]:
    output = []
    for evidence_class, evidence_items in (
        ("supporting", hypothesis.supporting_evidence),
        ("opposing", hypothesis.opposing_evidence),
    ):
        for evidence in evidence_items:
            if not graph.filter_evidence_refs([evidence.ref]):
                continue
            output.append(
                {
                    **ground_reference(graph, evidence.ref, "recorded"),
                    "evidence_class": evidence_class,
                    "reason": evidence.reason,
                    "confidence": evidence.confidence,
                }
            )
    return output


def unresolved_context_references(
    path_references: List[JsonDict],
    candidate_context: List[JsonDict],
    hypothesis_evidence_references: List[JsonDict],
) -> List[JsonDict]:
    output = [item for item in path_references if item["resolution_status"] != "resolved"]
    output.extend(
        item for item in hypothesis_evidence_references if item["resolution_status"] != "resolved"
    )
    for candidate in candidate_context:
        output.extend(candidate.get("unresolved_references") or ())
        output.extend(
            item
            for item in candidate["edge_endpoint_references"].values()
            if item["resolution_status"] != "resolved"
        )
        output.extend(
            item
            for item in candidate["edge_evidence_references"]
            if item["resolution_status"] != "resolved"
        )
    return dedupe_reference_facts(output)


def artifact_context_gaps(candidate_context: List[JsonDict]) -> tuple:
    missing = []
    truncated = []
    for candidate in candidate_context:
        hydration = candidate["artifact_hydration"]
        for artifact_id in hydration["missing_artifact_ids"]:
            missing.append({"candidate_ref": candidate["ref"], "artifact_id": artifact_id})
        for artifact_id in hydration["truncated_artifact_ids"]:
            truncated.append({"candidate_ref": candidate["ref"], "artifact_id": artifact_id})
    return missing, truncated


def dedupe_reference_facts(values: Iterable[JsonDict]) -> List[JsonDict]:
    output: List[JsonDict] = []
    seen = set()
    for value in values:
        key = (value.get("raw_ref"), value.get("provenance_class"), value.get("resolution_status"))
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


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
    return [
        graph.sanitize_judge_edge_evidence(edge)
        for edge in graph.temporal_adjacency_edges(node_ref)
    ]


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
            "repository_revision": (
                ""
                if data.get("repository_revision") is None
                else data.get("repository_revision")
            ),
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
    raw_downstream_ref = str(path[current_index - 1])
    downstream_ref = resolved_path[current_index - 1]
    current_resolved = graph.resolve(current_ref)
    downstream_resolved = graph.resolve(raw_downstream_ref)
    if current_resolved not in graph.nodes or downstream_resolved not in graph.nodes:
        return [
            {
                "from_ref": current_ref,
                "to_ref": downstream_ref,
                "relation": "unresolved_path_advisory",
                "evidence_type": "unresolved_path",
                "evidence_refs": [str(current_ref), raw_downstream_ref],
                "confidence": 0.0,
                "eligible_for_attribution": False,
                "resolution_status": "unresolved",
                "inference_method": "active_backward_path_unresolved_endpoint",
                "edge_origin": "offline.analyzer_path",
            }
        ]
    edges = graph.edge_context(current_ref, downstream_ref)
    if edges:
        return edges
    downstream = graph.nodes.get(downstream_ref)
    if downstream and downstream.event_type == "progress.episode":
        window = active_progress_navigation_window(graph, downstream_ref)
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
    refs = [
        ref
        for ref in episode.member_refs
        if graph.active_revision_evidence_eligible(ref)
    ][:EPISODE_MEMBER_LIMIT]
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
    data = active_progress_episode_data(graph, episode.ref)
    keys = (
        "phase",
        "member_refs",
        "candidate_member_refs",
        "excluded_member_refs",
        "member_count",
        "member_summaries",
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
    return active_progress_navigation_window(graph, anchor_ref)


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
