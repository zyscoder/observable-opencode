"""Bounded, graph-grounded evidence expansion for Global Judge re-evaluation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from .causal_state import FrozenMapping
from .evidence_capsule import build_action_group_context
from .graph import TraceGraph, is_temporal_only_edge
from .models import JsonDict, compact_semantic_value, stable_json


EVIDENCE_EXPANSION_SCHEMA_VERSION = "evidence-expansion-result/v1"
EVIDENCE_EXPANSION_REQUEST_SCHEMA_VERSION = "evidence-expansion-request/v1"
EVIDENCE_EXPANSION_CONTEXT_KINDS = frozenset(
    {
        "upstream",
        "downstream",
        "artifact",
        "action_group",
        "message_transform",
        "full_node",
    }
)
LLM_EVENT_TYPES = frozenset({"llm.call", "llm.turn"})
RETRIEVAL_EDGE_ORIGINS = frozenset(
    {
        "offline.global_candidate_retrieval",
        "offline.navigation_routing",
        "offline.progress_retrieval",
        "offline.semantic_retrieval",
        "offline.sibling_retrieval",
    }
)
NON_CAUSAL_EXPANSION_RELATIONS = frozenset(
    {
        "claim_group_precedes",
        "fallback_sequence",
        "previous_progress_episode",
        "progress_episode_member",
        "progress_episode_projects_to_target",
        "semantic_navigation_route",
        "temporal_proximity",
        "temporal_sequence",
    }
)


def _canonical_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{0} must be non-empty".format(field_name))
    return text


def stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(stable_json(dict(value)).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ExpansionLimits:
    max_nodes: int = 8
    max_bytes: int = 32_768

    def __post_init__(self) -> None:
        for name in ("max_nodes", "max_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("{0} must be a positive integer".format(name))

    def to_dict(self) -> JsonDict:
        return {
            "max_nodes": self.max_nodes,
            "max_bytes": self.max_bytes,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ExpansionLimits":
        if not isinstance(value, Mapping) or set(value) != {
            "max_nodes",
            "max_bytes",
        }:
            raise ValueError("evidence expansion limits schema mismatch")
        return cls(
            max_nodes=value.get("max_nodes"),
            max_bytes=value.get("max_bytes"),
        )


@dataclass(frozen=True)
class EvidenceExpansionRequest:
    seed_ref: str
    defect_fingerprint: str
    anchor_ref: str
    context_kind: str
    reason: str
    expected_judgment_change: str

    def __post_init__(self) -> None:
        for field_name in (
            "seed_ref",
            "defect_fingerprint",
            "anchor_ref",
            "context_kind",
            "reason",
            "expected_judgment_change",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_text(getattr(self, field_name), field_name),
            )

    @property
    def identity(self) -> str:
        return "evidence_expansion_request:v1:{0}".format(
            stable_hash(self.to_dict())
        )

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": EVIDENCE_EXPANSION_REQUEST_SCHEMA_VERSION,
            "seed_ref": self.seed_ref,
            "defect_fingerprint": self.defect_fingerprint,
            "anchor_ref": self.anchor_ref,
            "context_kind": self.context_kind,
            "reason": self.reason,
            "expected_judgment_change": self.expected_judgment_change,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "EvidenceExpansionRequest":
        required = {
            "schema_version",
            "seed_ref",
            "defect_fingerprint",
            "anchor_ref",
            "context_kind",
            "reason",
            "expected_judgment_change",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != required
            or value.get("schema_version")
            != EVIDENCE_EXPANSION_REQUEST_SCHEMA_VERSION
        ):
            raise ValueError("evidence expansion request schema mismatch")
        return cls(
            seed_ref=value.get("seed_ref"),
            defect_fingerprint=value.get("defect_fingerprint"),
            anchor_ref=value.get("anchor_ref"),
            context_kind=value.get("context_kind"),
            reason=value.get("reason"),
            expected_judgment_change=value.get("expected_judgment_change"),
        )


@dataclass(frozen=True)
class EvidenceExpansionResult:
    request: EvidenceExpansionRequest
    status: str
    items: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    total_bytes: int = 0
    inspected_node_count: int = 0
    truncated: bool = False
    limits: ExpansionLimits = field(default_factory=ExpansionLimits)
    rejection_code: str = ""
    rejection_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.request, EvidenceExpansionRequest):
            raise TypeError("evidence expansion result request is invalid")
        if self.status not in {"expanded", "rejected"}:
            raise ValueError("unsupported evidence expansion result status")
        if (
            isinstance(self.total_bytes, bool)
            or not isinstance(self.total_bytes, int)
            or self.total_bytes < 0
        ):
            raise ValueError("evidence expansion total_bytes is invalid")
        if (
            isinstance(self.inspected_node_count, bool)
            or not isinstance(self.inspected_node_count, int)
            or self.inspected_node_count < 0
        ):
            raise ValueError("evidence expansion inspected count is invalid")
        if type(self.truncated) is not bool:
            raise ValueError("evidence expansion truncated must be boolean")
        if not isinstance(self.limits, ExpansionLimits):
            raise TypeError("evidence expansion limits are invalid")
        object.__setattr__(
            self,
            "items",
            tuple(_freeze(item) for item in self.items),
        )
        object.__setattr__(self, "rejection_code", str(self.rejection_code or ""))
        object.__setattr__(
            self, "rejection_reason", str(self.rejection_reason or "")
        )
        if self.status == "expanded":
            if not self.items or self.rejection_code or self.rejection_reason:
                raise ValueError(
                    "expanded evidence requires items and no rejection"
                )
            if (
                len(self.items) > self.limits.max_nodes
                or len(self.resolved_refs) > self.limits.max_nodes
            ):
                raise ValueError("expanded evidence exceeds the node budget")
            if self.total_bytes != _items_bytes(self.items):
                raise ValueError("expanded evidence byte accounting is invalid")
            if self.total_bytes > self.limits.max_bytes:
                raise ValueError("expanded evidence exceeds the byte budget")
        elif (
            self.items
            or self.total_bytes
            or not self.rejection_code
            or not self.rejection_reason
        ):
            raise ValueError(
                "rejected evidence requires an empty payload and concrete reason"
            )

    @property
    def request_identity(self) -> str:
        return self.request.identity

    @property
    def resolved_refs(self) -> Tuple[str, ...]:
        output = []
        seen = set()
        for item in self.items:
            refs = [
                str(item.get("resolved_ref") or ""),
                *(
                    str(member.get("ref") or "")
                    for member in (
                        item.get("action_group", {}).get("members") or ()
                        if isinstance(item.get("action_group"), Mapping)
                        else ()
                    )
                    if isinstance(member, Mapping)
                ),
            ]
            for ref in refs:
                if ref and ref not in seen:
                    seen.add(ref)
                    output.append(ref)
        return tuple(output)

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": EVIDENCE_EXPANSION_SCHEMA_VERSION,
            "request_identity": self.request_identity,
            "request": self.request.to_dict(),
            "status": self.status,
            "items": [_thaw(item) for item in self.items],
            "total_bytes": self.total_bytes,
            "inspected_node_count": self.inspected_node_count,
            "truncated": self.truncated,
            "limits": self.limits.to_dict(),
            "rejection_code": self.rejection_code,
            "rejection_reason": self.rejection_reason,
            "behavior_impact": "none_offline_analysis_only",
        }

    @classmethod
    def from_dict(cls, value: Any) -> "EvidenceExpansionResult":
        required = {
            "schema_version",
            "request_identity",
            "request",
            "status",
            "items",
            "total_bytes",
            "inspected_node_count",
            "truncated",
            "limits",
            "rejection_code",
            "rejection_reason",
            "behavior_impact",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != required
            or value.get("schema_version") != EVIDENCE_EXPANSION_SCHEMA_VERSION
            or value.get("behavior_impact")
            != "none_offline_analysis_only"
        ):
            raise ValueError("evidence expansion result schema mismatch")
        raw_items = value.get("items")
        if not isinstance(raw_items, list) or any(
            not isinstance(item, Mapping) for item in raw_items
        ):
            raise ValueError("evidence expansion items must be objects")
        result = cls(
            request=EvidenceExpansionRequest.from_dict(value.get("request")),
            status=str(value.get("status") or ""),
            items=tuple(dict(item) for item in raw_items),
            total_bytes=value.get("total_bytes"),
            inspected_node_count=value.get("inspected_node_count"),
            truncated=value.get("truncated"),
            limits=ExpansionLimits.from_dict(value.get("limits")),
            rejection_code=str(value.get("rejection_code") or ""),
            rejection_reason=str(value.get("rejection_reason") or ""),
        )
        if value.get("request_identity") != result.request_identity:
            raise ValueError(
                "evidence expansion request identity is inconsistent"
            )
        return result


def _items_bytes(items: Iterable[Mapping[str, Any]]) -> int:
    payload = [_thaw(item) for item in items]
    return len(stable_json(payload).encode("utf-8"))


def _rejected(
    request: EvidenceExpansionRequest,
    limits: ExpansionLimits,
    code: str,
    reason: str,
    *,
    inspected_node_count: int = 0,
) -> EvidenceExpansionResult:
    return EvidenceExpansionResult(
        request=request,
        status="rejected",
        limits=limits,
        rejection_code=code,
        rejection_reason=reason,
        inspected_node_count=inspected_node_count,
    )


def _eligible_causal_edges(
    graph: TraceGraph,
    *,
    source_ref: str,
    target_ref: str,
) -> Tuple[JsonDict, ...]:
    output = []
    seen = set()
    for edge in graph.edge_context(source_ref, target_ref):
        sanitized = graph.sanitize_judge_edge_evidence(edge)
        if (
            sanitized.get("eligible_for_attribution") is not True
            or is_temporal_only_edge(sanitized)
            or str(sanitized.get("relation") or "")
            in NON_CAUSAL_EXPANSION_RELATIONS
            or sanitized.get("retrieval_candidate") is True
            or str(sanitized.get("edge_origin") or "")
            in RETRIEVAL_EDGE_ORIGINS
        ):
            continue
        identity = stable_json(sanitized)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(sanitized)
    return tuple(output)


def _adjacency_items(
    graph: TraceGraph,
    *,
    anchor_ref: str,
    direction: str,
    limits: ExpansionLimits,
) -> Tuple[Tuple[JsonDict, ...], int, bool]:
    bounded = (
        graph.bounded_upstream_refs(anchor_ref, limit=limits.max_nodes)
        if direction == "upstream"
        else graph.bounded_downstream_refs(anchor_ref, limit=limits.max_nodes)
    )
    output = []
    byte_truncated = False
    for resolved_ref in bounded.refs:
        if graph.nodes[resolved_ref].event_type == "progress.episode":
            continue
        source_ref, target_ref = (
            (resolved_ref, anchor_ref)
            if direction == "upstream"
            else (anchor_ref, resolved_ref)
        )
        edges = _eligible_causal_edges(
            graph,
            source_ref=source_ref,
            target_ref=target_ref,
        )
        if not edges:
            continue
        node = graph.sanitize_judge_node(graph.hydrate_node(resolved_ref))
        item = {
            "kind": "node_context",
            "direction": direction,
            "anchor_ref": anchor_ref,
            "resolved_ref": resolved_ref,
            "node": node.compact(max_chars=4_000),
            "edges": [dict(edge) for edge in edges],
            "provenance": {
                "source": "trace_graph_bounded_adjacency",
                "active_revision_eligible": True,
                "pure_temporal_navigation_excluded": True,
            },
            "truncated": False,
        }
        if _items_bytes((*output, item)) > limits.max_bytes:
            byte_truncated = True
            break
        output.append(item)
    return (
        tuple(output),
        bounded.inspected_count,
        bounded.truncated or byte_truncated,
    )


def _message_transform_item(
    graph: TraceGraph,
    anchor_ref: str,
) -> JsonDict:
    node = graph.sanitize_judge_node(graph.hydrate_node(anchor_ref))
    data = node.data if isinstance(node.data, Mapping) else {}
    snapshot = next(
        (
            dict(item)
            for item in graph.message_lineage.get("snapshots") or ()
            if isinstance(item, Mapping)
            and str(item.get("node_ref") or "") == anchor_ref
        ),
        {},
    )
    edges = []
    for edge in graph.message_lineage.get("edges") or ():
        if (
            not isinstance(edge, Mapping)
            or edge.get("eligible_for_attribution") is not True
            or is_temporal_only_edge(edge)
            or anchor_ref
            not in {
                str(edge.get("from_ref") or ""),
                str(edge.get("to_ref") or ""),
            }
        ):
            continue
        edges.append(graph.sanitize_judge_edge_evidence(edge))
    return {
        "kind": "message_transform",
        "direction": "transform",
        "anchor_ref": anchor_ref,
        "resolved_ref": anchor_ref,
        "message_transforms": compact_semantic_value(
            data.get("message_transforms") or data.get("transforms") or [],
            text_limit=2_000,
            collection_limit=16,
            depth=0,
        ),
        "lineage_snapshot": snapshot,
        "lineage_edges": edges[:16],
        "provenance": {
            "source": "recorded_llm_message_transform_and_offline_lineage",
            "active_revision_eligible": True,
            "pure_temporal_navigation_excluded": True,
        },
        "truncated": len(edges) > 16,
    }


def expand_evidence(
    graph: TraceGraph,
    request: EvidenceExpansionRequest,
    limits: ExpansionLimits,
    *,
    seen_request_identities: Iterable[str] = (),
) -> EvidenceExpansionResult:
    if not isinstance(graph, TraceGraph):
        raise TypeError("evidence expansion requires a TraceGraph")
    if not isinstance(request, EvidenceExpansionRequest):
        raise TypeError(
            "evidence expansion requires an EvidenceExpansionRequest"
        )
    if not isinstance(limits, ExpansionLimits):
        raise TypeError("evidence expansion requires ExpansionLimits")
    if request.identity in {str(item) for item in seen_request_identities}:
        return _rejected(
            request,
            limits,
            "duplicate_request",
            "The same evidence expansion request identity was already executed.",
        )
    if request.context_kind not in EVIDENCE_EXPANSION_CONTEXT_KINDS:
        return _rejected(
            request,
            limits,
            "unsupported_context_kind",
            "The requested context kind is not supported.",
        )
    anchor_ref = graph.resolve(request.anchor_ref)
    if (
        not anchor_ref
        or anchor_ref not in graph.nodes
        or not graph.active_revision_evidence_eligible(anchor_ref)
    ):
        return _rejected(
            request,
            limits,
            "anchor_unresolved",
            "The requested anchor is unresolved or ineligible for the active revision.",
        )
    seed_ref = graph.resolve(request.seed_ref)
    if (
        not seed_ref
        or seed_ref not in graph.nodes
        or not graph.active_revision_start_eligible(seed_ref)
    ):
        return _rejected(
            request,
            limits,
            "seed_unresolved",
            "The evidence expansion seed is unresolved or ineligible.",
        )

    inspected = 1
    truncated = False
    if request.context_kind in {"upstream", "downstream"}:
        items, inspected, truncated = _adjacency_items(
            graph,
            anchor_ref=anchor_ref,
            direction=request.context_kind,
            limits=limits,
        )
    elif request.context_kind == "artifact":
        item = {
            "kind": "artifact",
            "direction": "artifact",
            "anchor_ref": anchor_ref,
            "resolved_ref": anchor_ref,
            "artifact_hydration": graph.artifact_hydration_manifest(anchor_ref),
            "provenance": {
                "source": "verified_artifact_reader",
                "active_revision_eligible": True,
            },
            "truncated": False,
        }
        items = (item,)
    elif request.context_kind == "action_group":
        group = build_action_group_context(
            graph, graph.hydrate_node(anchor_ref)
        )
        raw_members = list(group.get("members") or ())
        group["members"] = raw_members[: limits.max_nodes]
        group["truncated"] = bool(
            group.get("truncated") or len(raw_members) > limits.max_nodes
        )
        truncated = bool(group["truncated"])
        inspected = len(group["members"])
        items = (
            {
                "kind": "action_group",
                "direction": "action_group",
                "anchor_ref": anchor_ref,
                "resolved_ref": anchor_ref,
                "action_group": group,
                "provenance": {
                    "source": "recorded_action_group_or_call_identity",
                    "active_revision_eligible": True,
                },
                "truncated": truncated,
            },
        )
    elif request.context_kind == "message_transform":
        if graph.nodes[anchor_ref].event_type not in LLM_EVENT_TYPES:
            return _rejected(
                request,
                limits,
                "anchor_kind_mismatch",
                "message_transform expansion requires an llm.call or llm.turn anchor.",
                inspected_node_count=1,
            )
        items = (_message_transform_item(graph, anchor_ref),)
        truncated = bool(items[0]["truncated"])
    else:
        compact_limit = max(256, min(16_000, limits.max_bytes - 1_024))
        items = (
            {
                "kind": "full_node",
                "direction": "self",
                "anchor_ref": anchor_ref,
                "resolved_ref": anchor_ref,
                "node": graph.sanitize_judge_node(
                    graph.hydrate_node(anchor_ref)
                ).compact(max_chars=compact_limit),
                "provenance": {
                    "source": "active_trace_graph_node",
                    "active_revision_eligible": True,
                },
                "truncated": False,
            },
        )

    if not items:
        return _rejected(
            request,
            limits,
            "context_unavailable",
            "No eligible causal context exists for the requested anchor and kind.",
            inspected_node_count=inspected,
        )
    total_bytes = _items_bytes(items)
    if total_bytes > limits.max_bytes:
        return _rejected(
            request,
            limits,
            "byte_budget_exceeded",
            (
                "The requested evidence requires {0} bytes, exceeding the "
                "{1}-byte expansion budget."
            ).format(total_bytes, limits.max_bytes),
            inspected_node_count=inspected,
        )
    return EvidenceExpansionResult(
        request=request,
        status="expanded",
        items=items,
        total_bytes=total_bytes,
        inspected_node_count=inspected,
        truncated=truncated,
        limits=limits,
    )


def validate_evidence_expansion_result_against_graph(
    graph: TraceGraph,
    value: EvidenceExpansionResult | Mapping[str, Any],
    *,
    limits: Optional[ExpansionLimits] = None,
) -> EvidenceExpansionResult:
    result = (
        value
        if isinstance(value, EvidenceExpansionResult)
        else EvidenceExpansionResult.from_dict(value)
    )
    expected_limits = limits or result.limits
    if result.limits != expected_limits:
        raise ValueError(
            "evidence expansion limits contradict the authoritative limits"
        )
    seen = (
        {result.request_identity}
        if result.status == "rejected"
        and result.rejection_code == "duplicate_request"
        else ()
    )
    canonical = expand_evidence(
        graph,
        result.request,
        expected_limits,
        seen_request_identities=seen,
    )
    if stable_json(result.to_dict()) != stable_json(canonical.to_dict()):
        raise ValueError(
            "evidence expansion result contradicts the active graph canonical expansion"
        )
    return result


__all__ = [
    "EVIDENCE_EXPANSION_CONTEXT_KINDS",
    "EVIDENCE_EXPANSION_REQUEST_SCHEMA_VERSION",
    "EVIDENCE_EXPANSION_SCHEMA_VERSION",
    "EvidenceExpansionRequest",
    "EvidenceExpansionResult",
    "ExpansionLimits",
    "expand_evidence",
    "stable_hash",
    "validate_evidence_expansion_result_against_graph",
]
