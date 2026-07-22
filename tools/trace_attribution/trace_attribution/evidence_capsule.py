"""Deterministic candidate-local evidence closure for global causal judgment."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from .causal_state import CausalCandidate, DefectState, FrozenMapping
from .causal_retrieval import canonical_candidate_route, root_candidate_eligible
from .graph import TraceGraph
from .models import JsonDict, TraceNode, stable_json


CAPSULE_SCHEMA_VERSION = "candidate-evidence-capsule/v1"
ACTION_GROUP_KEYS = ("action_group_id", "actionGroupID", "actionGroupId")
CALL_ID_KEYS = ("call_id", "callID", "tool_call_id", "toolCallID")
GLOBAL_EVIDENCE_ONLY_EVENT_TYPES = frozenset(
    {
        "claim.support_assessment",
        "evidence.fact",
        "evidence.semantic_fact",
        "tool.result",
        "verification",
    }
)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _dedupe_strings(values: Iterable[Any]) -> Tuple[str, ...]:
    output: List[str] = []
    seen = set()
    for value in values:
        item = str(value or "")
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return tuple(output)


@dataclass(frozen=True)
class CandidateEvidenceCapsule:
    candidate_ref: str
    defect_state: DefectState
    candidate: Mapping[str, Any]
    downstream_path: Tuple[str, ...] = field(default_factory=tuple)
    downstream_path_references: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    causal_path_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    start_refs: Tuple[str, ...] = field(default_factory=tuple)
    action_group: Mapping[str, Any] = field(default_factory=FrozenMapping)
    incoming_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    outgoing_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidence_references: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    artifact_hydration: Mapping[str, Any] = field(default_factory=FrozenMapping)
    missing_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", _freeze(self.candidate))
        object.__setattr__(self, "downstream_path", _dedupe_strings(self.downstream_path))
        object.__setattr__(
            self,
            "downstream_path_references",
            tuple(_freeze(item) for item in self.downstream_path_references),
        )
        object.__setattr__(
            self, "causal_path_edges", tuple(_freeze(item) for item in self.causal_path_edges)
        )
        object.__setattr__(self, "start_refs", _dedupe_strings(self.start_refs))
        object.__setattr__(self, "action_group", _freeze(self.action_group))
        object.__setattr__(
            self, "incoming_edges", tuple(_freeze(item) for item in self.incoming_edges)
        )
        object.__setattr__(
            self, "outgoing_edges", tuple(_freeze(item) for item in self.outgoing_edges)
        )
        object.__setattr__(
            self,
            "evidence_references",
            tuple(_freeze(item) for item in self.evidence_references),
        )
        object.__setattr__(self, "artifact_hydration", _freeze(self.artifact_hydration))
        object.__setattr__(
            self, "missing_evidence_refs", _dedupe_strings(self.missing_evidence_refs)
        )

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "candidate_ref": self.candidate_ref,
            "defect_state": self.defect_state.to_dict(),
            "candidate": _thaw(self.candidate),
            "downstream_path": list(self.downstream_path),
            "downstream_path_references": _thaw(self.downstream_path_references),
            "causal_path_edges": _thaw(self.causal_path_edges),
            "start_refs": list(self.start_refs),
            "action_group": _thaw(self.action_group),
            "incoming_edges": _thaw(self.incoming_edges),
            "outgoing_edges": _thaw(self.outgoing_edges),
            "evidence_references": _thaw(self.evidence_references),
            "artifact_hydration": _thaw(self.artifact_hydration),
            "missing_evidence_refs": list(self.missing_evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateEvidenceCapsule":
        if not isinstance(value, Mapping):
            raise TypeError("candidate evidence capsule must be an object")
        required = {
            "schema_version",
            "candidate_ref",
            "defect_state",
            "candidate",
            "downstream_path",
            "downstream_path_references",
            "causal_path_edges",
            "start_refs",
            "action_group",
            "incoming_edges",
            "outgoing_edges",
            "evidence_references",
            "artifact_hydration",
            "missing_evidence_refs",
        }
        if set(value) != required or value.get("schema_version") != CAPSULE_SCHEMA_VERSION:
            raise ValueError("candidate evidence capsule schema mismatch")

        def mapping(name: str) -> Mapping[str, Any]:
            item = value.get(name)
            if not isinstance(item, Mapping):
                raise TypeError("candidate evidence capsule {0} must be an object".format(name))
            return item

        def mappings(name: str) -> Tuple[Mapping[str, Any], ...]:
            items = value.get(name)
            if not isinstance(items, list) or any(
                not isinstance(item, Mapping) for item in items
            ):
                raise TypeError("candidate evidence capsule {0} must be an object array".format(name))
            return tuple(items)

        def strings(name: str) -> Tuple[str, ...]:
            items = value.get(name)
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item for item in items
            ):
                raise TypeError("candidate evidence capsule {0} must be a string array".format(name))
            return tuple(items)

        return cls(
            candidate_ref=str(value.get("candidate_ref") or ""),
            defect_state=DefectState.from_dict(dict(mapping("defect_state"))),
            candidate=mapping("candidate"),
            downstream_path=strings("downstream_path"),
            downstream_path_references=mappings("downstream_path_references"),
            causal_path_edges=mappings("causal_path_edges"),
            start_refs=strings("start_refs"),
            action_group=mapping("action_group"),
            incoming_edges=mappings("incoming_edges"),
            outgoing_edges=mappings("outgoing_edges"),
            evidence_references=mappings("evidence_references"),
            artifact_hydration=mapping("artifact_hydration"),
            missing_evidence_refs=strings("missing_evidence_refs"),
        )


def build_candidate_evidence_capsules(
    *,
    graph: TraceGraph,
    candidates: Sequence[CausalCandidate],
    defect_state: DefectState,
    downstream_paths: Mapping[str, Sequence[str]],
    start_refs: Sequence[str],
) -> Tuple[CandidateEvidenceCapsule, ...]:
    selected: Dict[str, CausalCandidate] = {}
    routes_by_ref: Dict[str, List[CausalCandidate]] = {}
    order: List[str] = []
    for candidate in candidates:
        resolved = graph.resolve(candidate.ref) or candidate.ref
        if resolved not in graph.nodes or not graph.evidence_eligible(resolved):
            continue
        if resolved not in routes_by_ref:
            order.append(resolved)
            routes_by_ref[resolved] = []
        routes_by_ref[resolved].append(candidate)
    for ref in order:
        selected[ref] = canonical_candidate_route(
            graph, ref, routes_by_ref[ref]
        )

    capsules: List[CandidateEvidenceCapsule] = []
    for ref in order:
        candidate = selected[ref]
        node = graph.hydrate_node(ref)
        path = tuple(
            graph.resolve(item) or str(item)
            for item in downstream_paths.get(ref, ())
            if str(item)
        )
        if not path:
            path = (ref,)
        elif path[0] != ref:
            path = (ref, *path)
        artifact_hydration = graph.artifact_hydration_manifest(ref)
        missing = [
            "artifact:{0}".format(item)
            for item in artifact_hydration.get("missing_artifact_ids") or []
        ]
        retrieval_edge = graph.sanitize_judge_edge_evidence(candidate.edge)
        causal_path_edges = tuple(_causal_path_edges(graph, path))
        incoming_edges = tuple(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(ref)[:16]
        )
        outgoing_edges = tuple(_outgoing_edges(graph, ref, limit=16))
        evidence_refs = _dedupe_strings(
            graph.filter_evidence_refs(
                [
                    *candidate.evidence_refs,
                    *(retrieval_edge.get("evidence_refs") or ()),
                    *node.source_refs,
                    *(
                        evidence_ref
                        for edge in (*causal_path_edges, *incoming_edges, *outgoing_edges)
                        for evidence_ref in edge.get("evidence_refs") or ()
                    ),
                ]
            )
        )
        evidence_references = []
        for evidence_ref in evidence_refs:
            reference = _reference(graph, evidence_ref)
            evidence_references.append(reference)
            if reference.get("resolution_status") != "resolved":
                missing.append(evidence_ref)
        capsules.append(
            CandidateEvidenceCapsule(
                candidate_ref=ref,
                defect_state=defect_state,
                candidate={
                    "ref": ref,
                    "source": candidate.source,
                    "retrieval_is_not_causal_verdict": True,
                    "root_candidate_eligible": (
                        root_candidate_eligible(node)
                        and node.event_type not in GLOBAL_EVIDENCE_ONLY_EVENT_TYPES
                    ),
                    "retrieval_edge": retrieval_edge,
                    "node": node.compact(max_chars=3200),
                },
                downstream_path=path,
                downstream_path_references=tuple(_reference(graph, item) for item in path),
                causal_path_edges=causal_path_edges,
                start_refs=tuple(graph.resolve(item) or str(item) for item in start_refs),
                action_group=_action_group(graph, node),
                incoming_edges=incoming_edges,
                outgoing_edges=outgoing_edges,
                evidence_references=tuple(evidence_references),
                artifact_hydration=artifact_hydration,
                missing_evidence_refs=tuple(missing),
            )
        )
    return tuple(capsules)


def candidate_compression_metrics(
    graph: TraceGraph, capsules: Sequence[CandidateEvidenceCapsule]
) -> JsonDict:
    unique = {item.candidate_ref: item for item in capsules}
    trace_nodes = len(graph.nodes)
    candidate_count = len(unique)
    trace_bytes = len(stable_json(graph.raw_trace).encode("utf-8"))
    capsule_bytes = len(
        stable_json([item.to_dict() for item in unique.values()]).encode("utf-8")
    )
    return {
        "trace_node_count": trace_nodes,
        "candidate_count": candidate_count,
        "candidate_node_reduction_ratio": (
            round(1.0 - candidate_count / trace_nodes, 6) if trace_nodes else 0.0
        ),
        "trace_json_bytes": trace_bytes,
        "capsule_bytes": capsule_bytes,
        "candidate_byte_reduction_ratio": (
            round(1.0 - capsule_bytes / trace_bytes, 6) if trace_bytes else 0.0
        ),
    }


def _reference(graph: TraceGraph, raw_ref: str) -> JsonDict:
    resolved = graph.resolve(str(raw_ref))
    if resolved and resolved in graph.nodes:
        return {
            "raw_ref": str(raw_ref),
            "resolved_ref": resolved,
            "resolution_status": "resolved",
            "provenance_class": "recorded_or_reconstructed_trace_fact",
            "node": graph.hydrate_node(resolved).compact(max_chars=1800),
        }
    artifact = graph.artifact_reference_status(str(raw_ref))
    if artifact is not None:
        return {
            **artifact,
            "resolved_ref": str(artifact.get("canonical_ref") or raw_ref),
            "provenance_class": "recorded_artifact_manifest",
        }
    return {
        "raw_ref": str(raw_ref),
        "resolved_ref": "",
        "resolution_status": "unresolved",
        "provenance_class": "unresolved_reference",
    }


def _outgoing_edges(graph: TraceGraph, ref: str, *, limit: int) -> List[JsonDict]:
    output: List[JsonDict] = []
    for downstream in graph.downstream_refs(ref):
        output.extend(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.edge_context(ref, downstream)
        )
        if len(output) >= limit:
            break
    return output[:limit]


def _causal_path_edges(graph: TraceGraph, path: Tuple[str, ...]) -> List[JsonDict]:
    output: List[JsonDict] = []
    seen = set()
    for source_ref, target_ref in zip(path, path[1:]):
        for edge in graph.edge_context(source_ref, target_ref):
            sanitized = graph.sanitize_judge_edge_evidence(edge)
            signature = stable_json(sanitized)
            if signature in seen:
                continue
            seen.add(signature)
            output.append(sanitized)
    return output


def _action_group(graph: TraceGraph, candidate: TraceNode) -> JsonDict:
    identity = _action_identity(candidate)
    members = []
    if identity:
        for node in graph.nodes.values():
            if _action_identity(node) == identity:
                members.append(graph.hydrate_node(node.ref).compact(max_chars=1800))
        members.sort(key=lambda item: graph.position(str(item.get("ref") or "")))
    if not members:
        members = [candidate.compact(max_chars=1800)]
    return {
        "identity": identity or "node_ref:{0}".format(candidate.ref),
        "identity_source": "recorded_action_group_or_call_id" if identity else "candidate_ref_fallback",
        "members": members[:12],
        "member_count": len(members),
        "truncated": len(members) > 12,
    }


def _action_identity(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, Mapping) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    for container in (data, metadata):
        for key in ACTION_GROUP_KEYS:
            value = str(container.get(key) or "")
            if value:
                return "action_group_id:{0}".format(value)
    for container in (data, metadata):
        for key in CALL_ID_KEYS:
            value = str(container.get(key) or "")
            if value:
                return "call_id:{0}".format(value)
    return ""


__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "CandidateEvidenceCapsule",
    "build_candidate_evidence_capsules",
    "candidate_compression_metrics",
]
