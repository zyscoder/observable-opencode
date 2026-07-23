"""Deterministic candidate-local evidence closure for global causal judgment."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

from .causal_state import CausalCandidate, DefectState, FrozenMapping
from .causal_retrieval import (
    canonical_candidate_route,
    root_candidate_eligible,
)
from .graph import TraceGraph
from .models import JsonDict, TraceNode, stable_json


CAPSULE_SCHEMA_VERSION = "candidate-evidence-capsule/v6"
ACTION_GROUP_KEYS = ("action_group_id", "actionGroupID", "actionGroupId")
CALL_ID_KEYS = ("call_id", "callID", "tool_call_id", "toolCallID")
MAX_VALIDATION_SOURCE_BYTES = 16384
VALIDATION_SOURCE_KEYS = frozenset(
    {
        "candidate_ref",
        "candidate_source",
        "candidate_edge",
        "candidate_evidence_refs",
        "downstream_path",
        "start_refs",
        "prompt_collections_sha256",
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
    validation_source: Mapping[str, Any] = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        candidate_ref = str(self.candidate_ref).strip()
        object.__setattr__(self, "candidate_ref", candidate_ref)
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
        object.__setattr__(self, "validation_source", _freeze(self.validation_source))
        self.validate()

    def validate(self) -> None:
        if not self.candidate_ref:
            raise ValueError("candidate evidence capsule requires candidate identity")
        if not isinstance(self.candidate, Mapping):
            raise TypeError("candidate evidence capsule candidate must be an object")
        if type(self.candidate.get("root_candidate_eligible")) is not bool:
            raise ValueError("candidate root_candidate_eligible must be an exact boolean")
        if type(self.candidate.get("evidence_eligible")) is not bool:
            raise ValueError("candidate evidence_eligible must be an exact boolean")
        if self.candidate.get("retrieval_is_not_causal_verdict") is not True:
            raise ValueError(
                "candidate retrieval_is_not_causal_verdict must be exact boolean true"
            )
        graph_facts = self.candidate.get("active_graph_facts")
        if not isinstance(graph_facts, Mapping):
            raise ValueError("candidate active_graph_facts must be an object")
        if graph_facts.get("canonical_ref") != self.candidate_ref:
            raise ValueError("candidate identity mismatches active graph canonical identity")
        if type(graph_facts.get("evidence_eligible")) is not bool or type(
            graph_facts.get("root_candidate_eligible")
        ) is not bool:
            raise ValueError("candidate active graph eligibility must be exact booleans")
        if (
            graph_facts.get("evidence_eligible")
            != self.candidate.get("evidence_eligible")
            or graph_facts.get("root_candidate_eligible")
            != self.candidate.get("root_candidate_eligible")
        ):
            raise ValueError("candidate active graph eligibility facts contradict candidate")
        self._validate_identity_mapping(self.candidate, label="candidate")
        node = self.candidate.get("node")
        if not isinstance(node, Mapping):
            raise ValueError("candidate identity requires an embedded candidate node")
        self._validate_identity_mapping(node, label="embedded candidate node")
        if not self.downstream_path or self.downstream_path[0] != self.candidate_ref:
            raise ValueError("candidate identity must match downstream path start")
        self._validate_validation_source()
        if len(self.downstream_path_references) != len(self.downstream_path):
            raise ValueError(
                "candidate downstream path references must cover every path position"
            )
        for path_ref, reference in zip(
            self.downstream_path, self.downstream_path_references
        ):
            if not isinstance(reference, Mapping):
                raise TypeError("candidate downstream path reference must be an object")
            if reference.get("resolution_status") != "resolved":
                raise ValueError("candidate downstream path reference must be resolved")
            resolved_ref = str(reference.get("resolved_ref") or "")
            canonical_ref = str(reference.get("canonical_ref") or resolved_ref)
            raw_ref = str(reference.get("raw_ref") or "")
            if (
                raw_ref != path_ref
                or resolved_ref != path_ref
                or canonical_ref != path_ref
            ):
                raise ValueError(
                    "candidate identity downstream path reference must match its exact path position"
                )
            reference_node = reference.get("node")
            if isinstance(reference_node, Mapping):
                ref = str(reference_node.get("ref") or "")
                if ref != path_ref:
                    raise ValueError(
                        "candidate identity must match embedded downstream path node"
                    )

    def _validate_validation_source(self) -> None:
        source = self.validation_source
        if not isinstance(source, Mapping) or set(source) != VALIDATION_SOURCE_KEYS:
            raise ValueError("candidate evidence capsule validation source schema mismatch")
        if len(stable_json(_thaw(source)).encode("utf-8")) > MAX_VALIDATION_SOURCE_BYTES:
            raise ValueError(
                "candidate evidence capsule validation source must remain bounded"
            )
        if str(source.get("candidate_ref") or "") != self.candidate_ref:
            raise ValueError("candidate evidence capsule validation source identity mismatch")
        if str(source.get("candidate_source") or "") != str(
            self.candidate.get("source") or ""
        ):
            raise ValueError("candidate evidence capsule validation source route mismatch")
        if not isinstance(source.get("candidate_edge"), Mapping):
            raise TypeError("candidate evidence capsule validation source edge must be an object")
        for key in ("candidate_evidence_refs", "downstream_path", "start_refs"):
            value = source.get(key)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise TypeError(
                    "candidate evidence capsule validation source {0} must be a string array".format(
                        key
                    )
                )
        if tuple(source.get("downstream_path") or ()) != self.downstream_path:
            raise ValueError("candidate evidence capsule validation source path mismatch")
        if tuple(source.get("start_refs") or ()) != self.start_refs:
            raise ValueError("candidate evidence capsule validation source seed mismatch")
        expected_hash = _prompt_collections_sha256(
            retrieval_edge=self.candidate.get("retrieval_edge"),
            action_group=self.action_group,
            evidence_references=self.evidence_references,
            artifact_hydration=self.artifact_hydration,
            missing_evidence_refs=self.missing_evidence_refs,
        )
        if str(source.get("prompt_collections_sha256") or "") != expected_hash:
            raise ValueError(
                "candidate evidence capsule prompt-bearing collections do not match validation source"
            )

    def _validate_identity_mapping(
        self, value: Mapping[str, Any], *, label: str
    ) -> None:
        if str(value.get("ref") or "") != self.candidate_ref:
            raise ValueError("candidate identity mismatch in {0}".format(label))
        identity_fields = (
            str(value.get(key) or "")
            for key in ("candidate_ref", "resolved_ref", "canonical_ref")
            if key in value
        )
        if any(ref != self.candidate_ref for ref in identity_fields):
            raise ValueError(
                "candidate identity mismatch in {0}".format(label)
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
            "validation_source": _thaw(self.validation_source),
        }

    def judge_dict(self) -> JsonDict:
        """Return only grounded evidence collections intended for a Judge."""
        value = self.to_dict()
        value.pop("missing_evidence_refs", None)
        value.pop("validation_source", None)
        hydration = value.get("artifact_hydration")
        if isinstance(hydration, Mapping) and any(
            hydration.get(key)
            for key in (
                "integrity_failures",
                "missing_artifact_ids",
                "truncated_artifact_ids",
            )
        ):
            value.pop("artifact_hydration", None)
        elif isinstance(hydration, dict):
            for key in (
                "referenced_artifact_ids",
                "missing_artifact_ids",
                "truncated_artifact_ids",
                "integrity_failures",
            ):
                hydration.pop(key, None)
        return value

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
            "validation_source",
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
            validation_source=mapping("validation_source"),
        )


def build_candidate_evidence_capsules(
    *,
    graph: TraceGraph,
    candidates: Sequence[CausalCandidate],
    defect_state: DefectState,
    downstream_paths: Mapping[str, Sequence[str]],
    start_refs: Sequence[str],
) -> Tuple[CandidateEvidenceCapsule, ...]:
    owner_refs = tuple(
        dict.fromkeys(
            graph.resolve(item) or str(item)
            for item in start_refs
            if str(item)
        )
    )
    selected: Dict[str, CausalCandidate] = {}
    routes_by_ref: Dict[str, List[CausalCandidate]] = {}
    order: List[str] = []
    for candidate in candidates:
        resolved = graph.resolve(candidate.ref) or candidate.ref
        if not graph.active_revision_evidence_eligible(resolved):
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
        raw_candidate_evidence_refs = _dedupe_strings(
            [
                *candidate.evidence_refs,
                *(candidate.edge.get("evidence_refs") or ()),
            ]
        )
        if candidate.source in {"confirmed_edge", "attribution_edge"} and candidate.edge:
            candidate = CausalCandidate(
                ref=candidate.ref,
                node=candidate.node,
                source=candidate.source,
                edge=graph.sanitize_judge_edge_evidence(candidate.edge),
                score=candidate.score,
                evidence_refs=_dedupe_strings(
                    graph.filter_evidence_refs(candidate.evidence_refs)
                ),
            )
            candidate = _reconcile_candidate_source(
                graph,
                candidate,
                authoritative_candidates=candidates,
            )
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
        if any(
            not graph.active_revision_evidence_eligible(path_ref)
            for path_ref in path
        ):
            raise ValueError(
                "candidate downstream path contains evidence ineligible for the active revision"
            )
        causal_path_edges = tuple(_causal_path_edges(graph, path))
        incoming_edges = tuple(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(ref)[:16]
        )
        outgoing_edges = tuple(
            _outgoing_edges(
                graph,
                ref,
                path=path,
                owner_refs=owner_refs,
                limit=16,
            )
        )
        prompt_collections = _build_prompt_collections(
            graph=graph,
            candidate=candidate,
            path=path,
            causal_path_edges=causal_path_edges,
            incoming_edges=incoming_edges,
            outgoing_edges=outgoing_edges,
            diagnostic_evidence_refs=raw_candidate_evidence_refs,
        )
        validation_source = _validation_source(
            graph=graph,
            candidate=candidate,
            path=path,
            start_refs=owner_refs,
            prompt_collections=prompt_collections,
            diagnostic_evidence_refs=raw_candidate_evidence_refs,
        )
        capsule = CandidateEvidenceCapsule(
            candidate_ref=ref,
            defect_state=defect_state,
            candidate={
                "ref": ref,
                "source": candidate.source,
                "retrieval_is_not_causal_verdict": True,
                "evidence_eligible": graph.active_revision_evidence_eligible(ref),
                "root_candidate_eligible": root_candidate_eligible(node),
                "active_graph_facts": _active_candidate_graph_facts(graph, node),
                "retrieval_edge": prompt_collections["retrieval_edge"],
                "node": _grounded_node_snapshot(
                    graph, node, max_chars=3200
                ),
            },
            downstream_path=path,
            downstream_path_references=tuple(_reference(graph, item) for item in path),
            causal_path_edges=causal_path_edges,
            start_refs=owner_refs,
            action_group=prompt_collections["action_group"],
            incoming_edges=incoming_edges,
            outgoing_edges=outgoing_edges,
            evidence_references=tuple(prompt_collections["evidence_references"]),
            artifact_hydration=prompt_collections["artifact_hydration"],
            missing_evidence_refs=tuple(prompt_collections["missing_evidence_refs"]),
            validation_source=validation_source,
        )
        validate_candidate_evidence_capsule_against_graph(
            graph, capsule, authoritative_candidates=candidates
        )
        capsules.append(capsule)
    return tuple(capsules)


def _build_prompt_collections(
    *,
    graph: TraceGraph,
    candidate: CausalCandidate,
    path: Tuple[str, ...],
    causal_path_edges: Sequence[Mapping[str, Any]] | None = None,
    incoming_edges: Sequence[Mapping[str, Any]] | None = None,
    outgoing_edges: Sequence[Mapping[str, Any]] | None = None,
    diagnostic_evidence_refs: Sequence[str] | None = None,
    start_refs: Sequence[str] = (),
) -> JsonDict:
    ref = graph.resolve(candidate.ref) or candidate.ref
    node = graph.hydrate_node(ref)
    raw_artifact_hydration = graph.artifact_hydration_manifest(ref)
    missing = [
        "artifact:{0}".format(item)
        for item in (
            raw_artifact_hydration.get("missing_artifact_ids") or ()
        )
    ]
    artifact_hydration = raw_artifact_hydration
    retrieval_edge = graph.sanitize_judge_edge_evidence(candidate.edge)
    causal_edges = tuple(
        causal_path_edges
        if causal_path_edges is not None
        else _causal_path_edges(graph, path)
    )
    incoming = tuple(
        incoming_edges
        if incoming_edges is not None
        else (
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(ref)[:16]
        )
    )
    outgoing = tuple(
        outgoing_edges
        if outgoing_edges is not None
        else _outgoing_edges(
            graph,
            ref,
            path=path,
            owner_refs=start_refs,
            limit=16,
        )
    )
    raw_evidence_refs = _dedupe_strings(
        [
            *(diagnostic_evidence_refs or candidate.evidence_refs),
            *(retrieval_edge.get("evidence_refs") or ()),
            *node.source_refs,
            *(
                evidence_ref
                for edge in (*causal_edges, *incoming, *outgoing)
                for evidence_ref in graph.unresolved_edge_evidence_refs(
                    str(edge.get("from_ref") or ""),
                    str(edge.get("to_ref") or ""),
                )
            ),
            *(
                evidence_ref
                for edge in (*causal_edges, *incoming, *outgoing)
                for evidence_ref in edge.get("evidence_refs") or ()
            ),
        ]
    )
    evidence_refs = _dedupe_strings(graph.filter_evidence_refs(raw_evidence_refs))
    grounded_raw_refs = set(evidence_refs)
    missing.extend(
        ref for ref in raw_evidence_refs if ref not in grounded_raw_refs
    )
    evidence_references = []
    for evidence_ref in evidence_refs:
        reference = _reference(graph, evidence_ref)
        evidence_references.append(reference)
    return {
        "retrieval_edge": retrieval_edge,
        "action_group": _action_group(graph, node),
        "evidence_references": evidence_references,
        "artifact_hydration": artifact_hydration,
        "missing_evidence_refs": list(_dedupe_strings(missing)),
    }


def _validation_source(
    *,
    graph: TraceGraph,
    candidate: CausalCandidate,
    path: Tuple[str, ...],
    start_refs: Tuple[str, ...],
    prompt_collections: Mapping[str, Any],
    diagnostic_evidence_refs: Sequence[str] | None = None,
) -> JsonDict:
    return {
        "candidate_ref": candidate.ref,
        "candidate_source": candidate.source,
        "candidate_edge": _thaw(prompt_collections["retrieval_edge"]),
        "candidate_evidence_refs": list(
            _dedupe_strings(
                graph.filter_evidence_refs(
                    diagnostic_evidence_refs or candidate.evidence_refs
                )
            )
        ),
        "downstream_path": list(path),
        "start_refs": list(start_refs),
        "prompt_collections_sha256": _prompt_collections_sha256(
            retrieval_edge=prompt_collections["retrieval_edge"],
            action_group=prompt_collections["action_group"],
            evidence_references=prompt_collections["evidence_references"],
            artifact_hydration=prompt_collections["artifact_hydration"],
            missing_evidence_refs=prompt_collections["missing_evidence_refs"],
        ),
    }


def _prompt_collections_sha256(
    *,
    retrieval_edge: Any,
    action_group: Any,
    evidence_references: Any,
    artifact_hydration: Any,
    missing_evidence_refs: Any,
) -> str:
    value = {
        "retrieval_edge": _thaw(retrieval_edge),
        "action_group": _thaw(action_group),
        "evidence_references": _thaw(evidence_references),
        "artifact_hydration": _thaw(artifact_hydration),
        "missing_evidence_refs": _thaw(missing_evidence_refs),
    }
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


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
    if (
        resolved
        and resolved in graph.nodes
        and graph.active_revision_evidence_eligible(resolved)
    ):
        return {
            "raw_ref": str(raw_ref),
            "resolved_ref": resolved,
            "canonical_ref": resolved,
            "resolution_status": "resolved",
            "provenance_class": "recorded_or_reconstructed_trace_fact",
            "node": _grounded_node_snapshot(
                graph, graph.hydrate_node(resolved), max_chars=1800
            ),
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


def _active_candidate_graph_facts(
    graph: TraceGraph, node: TraceNode
) -> JsonDict:
    manifest = (
        graph.raw_trace.get("manifest")
        if isinstance(graph.raw_trace.get("manifest"), Mapping)
        else {}
    )
    return {
        "canonical_ref": node.ref,
        "component": node.component,
        "event_type": node.event_type,
        "event_kind": str(
            node.data.get("event_kind") or node.data.get("kind") or ""
        ),
        "subject_revision": str(
            node.data.get("subject_revision")
            or manifest.get("subject_revision")
            or ""
        ),
        "repository_revision": (
            ""
            if node.data.get("repository_revision") is None
            else str(node.data.get("repository_revision"))
        ),
        "revision_before": (
            ""
            if node.data.get("revision_before") is None
            else str(node.data.get("revision_before"))
        ),
        "revision_after": (
            ""
            if node.data.get("revision_after") is None
            else str(node.data.get("revision_after"))
        ),
        "revision_status": str(node.data.get("revision_status") or ""),
        "offline_only": node.data.get("offline_only"),
        "semantic_role": str(node.data.get("semantic_role") or ""),
        "navigation_role": str(node.data.get("navigation_role") or ""),
        "evidence_eligible": graph.active_revision_evidence_eligible(node.ref),
        "root_candidate_eligible": root_candidate_eligible(node),
    }


def validate_candidate_evidence_capsule_against_graph(
    graph: TraceGraph,
    capsule: CandidateEvidenceCapsule,
    *,
    authoritative_candidates: Sequence[CausalCandidate] = (),
) -> None:
    """Bind persisted candidate and path facts to the active graph."""
    capsule.validate()
    resolved = graph.resolve(capsule.candidate_ref)
    if resolved != capsule.candidate_ref or resolved not in graph.nodes:
        raise ValueError("candidate evidence capsule does not match active graph identity")
    if not graph.active_revision_evidence_eligible(resolved):
        raise ValueError(
            "candidate evidence capsule candidate is ineligible for the active revision"
        )
    node = graph.hydrate_node(resolved)
    expected_facts = _active_candidate_graph_facts(graph, node)
    if _thaw(capsule.candidate.get("active_graph_facts")) != expected_facts:
        raise ValueError("candidate evidence capsule facts do not match active graph")
    if capsule.candidate.get("evidence_eligible") is not True:
        raise ValueError("candidate evidence capsule is ineligible in active graph")
    if _thaw(capsule.candidate.get("node")) != _grounded_node_snapshot(
        graph, node, max_chars=3200
    ):
        raise ValueError("candidate evidence capsule node does not match active graph")
    source = capsule.validation_source
    source_evidence_refs = tuple(source.get("candidate_evidence_refs") or ())
    source_candidate = CausalCandidate(
        ref=str(source.get("candidate_ref") or ""),
        node=node,
        source=str(source.get("candidate_source") or ""),
        edge=_thaw(source.get("candidate_edge")),
        evidence_refs=tuple(graph.filter_evidence_refs(source_evidence_refs)),
    )
    active_candidate = _reconcile_candidate_source(
        graph,
        source_candidate,
        authoritative_candidates=authoritative_candidates,
    )
    source_edge = _thaw(
        graph.sanitize_judge_edge_evidence(source_candidate.edge)
    )
    recorded_source = active_candidate.source in {
        "confirmed_edge",
        "attribution_edge",
    }
    active_routes = tuple(
        route
        for route in authoritative_candidates
        if (graph.resolve(route.ref) or route.ref) == active_candidate.ref
        and (
            (
                recorded_source
                and graph.resolve(str(route.edge.get("to_ref") or ""))
                == graph.resolve(str(source_edge.get("to_ref") or ""))
                and str(route.edge.get("relation") or "")
                == str(source_edge.get("relation") or "")
                and str(route.edge.get("evidence_type") or "")
                == str(source_edge.get("evidence_type") or "")
            )
            or (
                not recorded_source
                and route.source == source_candidate.source
                and _thaw(graph.sanitize_judge_edge_evidence(route.edge))
                == source_edge
            )
        )
    )
    diagnostic_candidate = (
        canonical_candidate_route(
            graph,
            active_candidate.ref,
            active_routes,
        )
        if active_routes
        else active_candidate
    )
    diagnostic_evidence_refs = _dedupe_strings(
        [
            *source_evidence_refs,
            *diagnostic_candidate.evidence_refs,
            *(diagnostic_candidate.edge.get("evidence_refs") or ()),
        ]
    )
    expected_prompt_collections = _build_prompt_collections(
        graph=graph,
        candidate=active_candidate,
        path=capsule.downstream_path,
        diagnostic_evidence_refs=diagnostic_evidence_refs,
        start_refs=capsule.start_refs,
    )
    missing_artifact_refs = {
        "artifact:{0}".format(artifact_id)
        for artifact_id in (
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "missing_artifact_ids"
                )
                or ()
            ),
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "truncated_artifact_ids"
                )
                or ()
            ),
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "integrity_failures"
                )
                or ()
            ),
        )
    }
    invalid_diagnostics = [
        ref
        for ref in capsule.missing_evidence_refs
        if graph.filter_evidence_refs([ref]) and ref not in missing_artifact_refs
    ]
    if invalid_diagnostics:
        raise ValueError(
            "candidate evidence capsule prompt-bearing missing refs do not match active diagnostics"
        )
    persisted_prompt_collections = {
        "retrieval_edge": _thaw(capsule.candidate.get("retrieval_edge")),
        "action_group": _thaw(capsule.action_group),
        "evidence_references": _thaw(capsule.evidence_references),
        "artifact_hydration": _thaw(capsule.artifact_hydration),
        "missing_evidence_refs": list(capsule.missing_evidence_refs),
    }
    for label, expected in expected_prompt_collections.items():
        if persisted_prompt_collections[label] != expected:
            raise ValueError(
                "candidate evidence capsule prompt-bearing {0} does not match active construction".format(
                    label
                )
            )
    for path_ref, reference in zip(
        capsule.downstream_path, capsule.downstream_path_references
    ):
        if not graph.active_revision_evidence_eligible(path_ref):
            raise ValueError(
                "candidate downstream path reference is ineligible for the active revision"
            )
        if _thaw(reference) != _reference(graph, path_ref):
            raise ValueError(
                "candidate downstream path reference does not match active graph"
            )
    expected_edges = {
        "causal path": _causal_path_edges(graph, capsule.downstream_path),
        "incoming": [
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(capsule.candidate_ref)[:16]
        ],
        "outgoing": _outgoing_edges(
            graph,
            capsule.candidate_ref,
            path=capsule.downstream_path,
            owner_refs=capsule.start_refs,
            limit=16,
        ),
    }
    persisted_edges = {
        "causal path": capsule.causal_path_edges,
        "incoming": capsule.incoming_edges,
        "outgoing": capsule.outgoing_edges,
    }
    for label, expected in expected_edges.items():
        if _thaw(persisted_edges[label]) != expected:
            raise ValueError(
                "candidate evidence capsule {0} edges do not match active graph".format(
                    label
                )
            )


def _reconcile_candidate_source(
    graph: TraceGraph,
    candidate: CausalCandidate,
    *,
    authoritative_candidates: Sequence[CausalCandidate],
) -> CausalCandidate:
    edge = candidate.edge
    requires_recorded_edge = candidate.source in {
        "confirmed_edge",
        "attribution_edge",
    }
    if not edge:
        matching = [
            route
            for route in authoritative_candidates
            if (graph.resolve(route.ref) or route.ref) == candidate.ref
            and route.source == candidate.source
            and not route.edge
            and tuple(sorted(graph.filter_evidence_refs(route.evidence_refs)))
            == tuple(sorted(candidate.evidence_refs))
        ]
        if matching:
            return candidate
        raise ValueError(
            "candidate with no recorded edge requires an authoritative retrieval route"
        )
    raw_from_ref = str(edge.get("from_ref") or "")
    raw_to_ref = str(edge.get("to_ref") or "")
    from_ref = graph.resolve(raw_from_ref)
    to_ref = graph.resolve(raw_to_ref)
    if raw_from_ref and from_ref != candidate.ref:
        raise ValueError(
            "candidate evidence capsule validation source edge starts at another candidate"
        )
    if raw_to_ref and not to_ref:
        raise ValueError(
            "candidate evidence capsule validation source edge target is unresolved"
        )
    if not requires_recorded_edge:
        routes = [
            route
            for route in authoritative_candidates
            if (graph.resolve(route.ref) or route.ref) == candidate.ref
        ]
        if not routes:
            raise ValueError(
                "synthetic candidate requires an authoritative retrieval route"
            )
        matching = [
            route
            for route in routes
            if route.source == candidate.source
            and _thaw(graph.sanitize_judge_edge_evidence(route.edge))
            == _thaw(candidate.edge)
            and tuple(
                sorted(
                    _dedupe_strings(
                        graph.filter_evidence_refs(route.evidence_refs)
                    )
                )
            )
            == tuple(sorted(candidate.evidence_refs))
        ]
        if not matching:
            raise ValueError(
                "synthetic candidate does not match its authoritative retrieval route"
            )
        return candidate
    routes = [
        route
        for route in authoritative_candidates
        if (graph.resolve(route.ref) or route.ref) == candidate.ref
        and graph.resolve(str(route.edge.get("to_ref") or "")) == to_ref
        and str(route.edge.get("relation") or "")
        == str(candidate.edge.get("relation") or "")
        and str(route.edge.get("evidence_type") or "")
        == str(candidate.edge.get("evidence_type") or "")
    ]
    if not routes:
        raise ValueError(
            "recorded candidate requires an authoritative recorded route"
        )
    active = canonical_candidate_route(graph, candidate.ref, routes)
    active_edge = graph.sanitize_judge_edge_evidence(active.edge)
    active_evidence_refs = _dedupe_strings(
        graph.filter_evidence_refs(active.evidence_refs)
    )
    active_to_ref = graph.resolve(str(active_edge.get("to_ref") or ""))
    matching_active_edges = [
        graph.sanitize_judge_edge_evidence(item)
        for item in graph.edge_context(candidate.ref, active_to_ref or "")
        if str(item.get("relation") or "")
        == str(active_edge.get("relation") or "")
        and str(item.get("evidence_type") or "")
        == str(active_edge.get("evidence_type") or "")
    ]
    if (
        active_edge not in matching_active_edges
        or active.source != candidate.source
        or _thaw(active_edge) != _thaw(candidate.edge)
        or active_evidence_refs != tuple(candidate.evidence_refs)
    ):
        raise ValueError(
            "candidate does not match its authoritative recorded route"
        )
    return CausalCandidate(
        ref=candidate.ref,
        node=active.node,
        source=active.source,
        edge=active_edge,
        score=active.score,
        evidence_refs=active_evidence_refs,
    )


def validate_candidate_evidence_capsules_against_graph(
    graph: TraceGraph,
    capsules: Sequence[CandidateEvidenceCapsule],
    *,
    authoritative_candidates: Sequence[CausalCandidate] = (),
) -> None:
    for capsule in capsules:
        validate_candidate_evidence_capsule_against_graph(
            graph,
            capsule,
            authoritative_candidates=authoritative_candidates,
        )


def _outgoing_edges(
    graph: TraceGraph,
    ref: str,
    *,
    path: Sequence[str],
    owner_refs: Sequence[str] = (),
    limit: int,
) -> List[JsonDict]:
    output: List[JsonDict] = []
    if not path or path[0] != ref:
        return output
    if len(path) > 1:
        allowed_targets = {path[1]}
    else:
        allowed_targets = _owner_ancestor_refs(graph, owner_refs)
    for downstream in graph.downstream_refs(ref):
        if downstream not in allowed_targets:
            continue
        if not graph.edge_endpoints_eligible(ref, downstream):
            continue
        output.extend(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.edge_context(ref, downstream)
        )
        if len(output) >= limit:
            break
    return output[:limit]


def _owner_ancestor_refs(
    graph: TraceGraph,
    owner_refs: Sequence[str],
) -> Set[str]:
    pending = [
        graph.resolve(owner_ref) or str(owner_ref)
        for owner_ref in owner_refs
        if str(owner_ref)
    ]
    ancestors: Set[str] = set()
    while pending:
        current = pending.pop()
        if (
            current in ancestors
            or not graph.active_revision_evidence_eligible(current)
        ):
            continue
        ancestors.add(current)
        pending.extend(graph.upstream_refs(current))
    return ancestors


def _causal_path_edges(graph: TraceGraph, path: Tuple[str, ...]) -> List[JsonDict]:
    output: List[JsonDict] = []
    seen = set()
    for source_ref, target_ref in zip(path, path[1:]):
        if not graph.edge_endpoints_eligible(source_ref, target_ref):
            continue
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
            resolved = graph.resolve(node.ref)
            if (
                resolved != node.ref
                or resolved not in graph.nodes
                or not graph.active_revision_evidence_eligible(resolved)
            ):
                continue
            active_node = graph.hydrate_node(resolved)
            if (
                _action_identity(active_node) == identity
                and _action_revision_eligible(graph, candidate, active_node)
            ):
                members.append(
                    _grounded_node_snapshot(
                        graph, active_node, max_chars=1800
                    )
                )
        members.sort(key=lambda item: graph.position(str(item.get("ref") or "")))
    if (
        not members
        and graph.active_revision_evidence_eligible(candidate.ref)
        and root_candidate_eligible(candidate)
    ):
        members = [
            _grounded_node_snapshot(graph, candidate, max_chars=1800)
        ]
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


def _action_revision_eligible(
    graph: TraceGraph, candidate: TraceNode, member: TraceNode
) -> bool:
    return (
        graph.active_revision_evidence_eligible(candidate.ref)
        and graph.active_revision_evidence_eligible(member.ref)
    )


def _grounded_node_snapshot(
    graph: TraceGraph, node: TraceNode, *, max_chars: int
) -> JsonDict:
    snapshot = graph.sanitize_judge_node(node).compact(max_chars=max_chars)
    snapshot["source_refs"] = list(snapshot.get("source_refs") or ())
    return snapshot


__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "MAX_VALIDATION_SOURCE_BYTES",
    "CandidateEvidenceCapsule",
    "build_candidate_evidence_capsules",
    "candidate_compression_metrics",
    "validate_candidate_evidence_capsule_against_graph",
    "validate_candidate_evidence_capsules_against_graph",
]
