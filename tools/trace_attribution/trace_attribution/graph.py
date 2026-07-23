from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .artifact_reader import VerifiedArtifactReader
from .evaluation_facts import reconstruct_external_evaluation_record
from .models import JsonDict, TraceNode, stable_json
from .progress import reconstruct_progress_episodes
from .reconstruction import reconstruct_message_lineage


EVIDENCE_ELIGIBILITY_POLICY_IDENTITY = "graph-external-evidence-eligibility/v1"
RANKING_CONFIDENCE_EDGE_ORIGINS = frozenset(
    {
        "offline.global_candidate_retrieval",
        "offline.navigation_routing",
        "offline.progress_retrieval",
        "offline.semantic_retrieval",
        "offline.sibling_retrieval",
    }
)
RANKING_CONFIDENCE_INFERENCE_METHODS = frozenset(
    {
        "bounded_delivery_history_semantic_ranking_v1",
        "token_overlap_retrieval",
    }
)
RECORDED_PROVENANCE_KEYS = (
    "relation",
    "evidence_type",
    "edge_origin",
    "inference_method",
)


@dataclass(frozen=True)
class BoundedAdjacencyResult:
    refs: Tuple[str, ...]
    truncated: bool
    inspected_count: int
    scan_limit: int
    scan_truncated: bool


class TraceGraph:
    def __init__(
        self,
        *,
        case_id: str,
        nodes: Dict[str, TraceNode],
        aliases: Dict[str, str],
        upstream: Dict[str, Dict[str, None]],
        downstream: Dict[str, Dict[str, None]],
        raw_trace: JsonDict,
        artifact_hydration: JsonDict,
        artifact_index: Dict[str, JsonDict],
        artifact_root: Optional[Path],
        artifact_reader: VerifiedArtifactReader,
        artifact_records: Dict[str, JsonDict],
        message_lineage: JsonDict,
        edge_context_index: Dict[Tuple[str, str], List[JsonDict]],
        evidence_eligible_refs: Set[str],
        analysis_start_eligible_refs: Set[str],
    ):
        self.case_id = case_id
        self.nodes = nodes
        self.aliases = aliases
        self._upstream = upstream
        self._downstream = downstream
        self.raw_trace = raw_trace
        self.artifact_hydration = artifact_hydration
        self._artifact_index = artifact_index
        self._artifact_root = artifact_root
        self._artifact_reader = artifact_reader
        self._artifact_records = artifact_records
        self._hydrated_refs: Set[str] = set()
        self._counted_artifact_outcomes: Set[str] = set()
        self.message_lineage = message_lineage
        self._edge_context_index = edge_context_index
        self._positions = {ref: index for index, ref in enumerate(nodes)}
        self._evidence_eligible_refs = frozenset(evidence_eligible_refs)
        self._analysis_start_eligible_refs = frozenset(
            analysis_start_eligible_refs
        )

    @classmethod
    def from_file(cls, trace_file: Path) -> "TraceGraph":
        path = Path(trace_file)
        with path.open("r", encoding="utf-8") as handle:
            trace = json.load(handle)
        return cls.from_trace(
            trace,
            artifact_root=artifact_root_for_trace_path(path, trace),
        )

    @classmethod
    def from_trace(cls, trace: JsonDict, artifact_root: Optional[Path] = None) -> "TraceGraph":
        nodes: Dict[str, TraceNode] = {}
        aliases: Dict[str, str] = {}
        artifact_index = {
            str(item.get("artifact_id")): item
            for item in trace.get("artifacts") or []
            if isinstance(item, dict) and item.get("artifact_id")
        }
        artifact_reader = VerifiedArtifactReader(artifact_index, artifact_root)
        unique_referenced_artifacts: Set[str] = set()
        artifact_hydration: JsonDict = {
            # "referenced" sums per-record unique refs; "unique_referenced" deduplicates
            # across records. loaded/missing/truncated/fallback/mismatch count each
            # artifact identity once, even when multiple records share it.
            "referenced": 0,
            "unique_referenced": 0,
            "indexed": len(artifact_index),
            "requested_nodes": 0,
            "loaded": 0,
            "missing": 0,
            "truncated": 0,
            "slice_fallbacks": 0,
            "hash_mismatches": 0,
            "integrity_failures": [],
            "artifact_root": str(artifact_root) if artifact_root else "",
        }
        artifact_records: Dict[str, JsonDict] = {}
        records = trace.get("records") or []
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id") or "")
            if not record_id:
                continue
            ref = f"record:{record_id}"
            data = dict(record.get("data")) if isinstance(record.get("data"), dict) else {}
            record_artifact_ids = collect_artifact_ids(record, data)
            artifact_hydration["referenced"] += len(record_artifact_ids)
            unique_referenced_artifacts.update(record_artifact_ids)
            artifact_records[ref] = record
            node = TraceNode(
                ref=ref,
                record_id=record_id,
                component=str(record.get("component") or ""),
                event_type=str(record.get("event_type") or ""),
                title=str(record.get("title") or ""),
                status=str(record.get("status") or ""),
                timestamp=str(record.get("timestamp") or ""),
                data=data,
                source_refs=[str(item) for item in record.get("source_refs") or []],
            )
            nodes[ref] = node
            for alias in record_aliases(record):
                aliases[alias] = ref
            aliases[ref] = ref

        evidence_eligible_refs: Set[str] = set()
        analysis_start_eligible_refs: Set[str] = set()
        for ref, node in nodes.items():
            if node.event_type != "external.evaluation_fact":
                evidence_eligible_refs.add(ref)
                analysis_start_eligible_refs.add(ref)
                continue
            reconstructed = reconstruct_external_evaluation_record(
                trace, artifact_records.get(ref), records
            )
            if not reconstructed:
                continue
            data = reconstructed["data"]
            status = data.get("status")
            revision_matched = (
                data.get("revision_status") == "matched"
                and data.get("revision_provenance_status") == "valid"
            )
            if revision_matched and status in {"failed", "passed"}:
                evidence_eligible_refs.add(ref)
            if (
                revision_matched
                and status == "failed"
                and data.get("eligible_for_decisive_judgment") is True
            ):
                analysis_start_eligible_refs.add(ref)

        upstream: Dict[str, Dict[str, None]] = defaultdict(dict)
        downstream: Dict[str, Dict[str, None]] = defaultdict(dict)
        edge_context_index: Dict[Tuple[str, str], List[JsonDict]] = defaultdict(list)
        for node in nodes.values():
            for source_ref in node.source_refs:
                source = resolve_ref(source_ref, aliases)
                if (
                    source
                    and source != node.ref
                    and _edge_endpoints_eligible(
                        nodes, evidence_eligible_refs, source, node.ref
                    )
                ):
                    upstream[node.ref][source] = None
                    downstream[source][node.ref] = None
                    add_edge_context(
                        edge_context_index,
                        from_ref=source,
                        to_ref=node.ref,
                        relation="record_source",
                        evidence_type="explicit_reference",
                        evidence_refs=[source_ref],
                        confidence=1.0,
                        eligible_for_attribution=True,
                        inference_method="record.source_refs",
                        edge_origin="record.source_refs",
                    )

        for edge in trace.get("dataflow_edges") or []:
            if not isinstance(edge, dict):
                continue
            metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
            if not recorded_edge_eligible(edge):
                continue
            source = resolve_edge_endpoint(edge.get("from"), aliases)
            target = resolve_edge_endpoint(edge.get("to"), aliases)
            if (
                source
                and target
                and source != target
                and _edge_endpoints_eligible(
                    nodes, evidence_eligible_refs, source, target
                )
            ):
                add_edge_context(
                    edge_context_index,
                    from_ref=source,
                    to_ref=target,
                    relation=str(edge.get("relation") or metadata.get("relation") or "dataflow"),
                    evidence_type=str(
                        edge.get("evidence_type") or metadata.get("evidence_type") or "recorded_dataflow"
                    ),
                    evidence_refs=string_list(edge.get("evidence_refs") or metadata.get("evidence_refs")),
                    confidence=normalized_confidence(edge.get("confidence", metadata.get("confidence", 1.0))),
                    eligible_for_attribution=True,
                    inference_method=str(
                        edge.get("inference_method")
                        or metadata.get("inference_method")
                        or "trace_dataflow_edge"
                    ),
                    edge_origin=str(
                        edge.get("edge_origin")
                        or metadata.get("edge_origin")
                        or "trace.dataflow_edges"
                    ),
                    source_container="trace.dataflow_edges",
                    edge_id=str(edge.get("edge_id") or ""),
                    recorded_provenance={
                        "top_level": {
                            key: edge[key]
                            for key in RECORDED_PROVENANCE_KEYS
                            if key in edge
                        },
                        "metadata": {
                            key: metadata[key]
                            for key in RECORDED_PROVENANCE_KEYS
                            if key in metadata
                        },
                    },
                    metadata=metadata,
                )
                upstream[target][source] = None
                downstream[source][target] = None

        artifact_hydration["unique_referenced"] = len(unique_referenced_artifacts)
        message_lineage = reconstruct_message_lineage(
            trace=trace,
            nodes=nodes,
            aliases=aliases,
            artifact_reader=artifact_reader,
        )
        for edge in message_lineage.get("edges") or []:
            if not isinstance(edge, dict) or not edge.get("eligible_for_attribution"):
                continue
            source = str(edge.get("from_ref") or "")
            target = str(edge.get("to_ref") or "")
            if (
                source in nodes
                and target in nodes
                and source != target
                and _edge_endpoints_eligible(
                    nodes, evidence_eligible_refs, source, target
                )
            ):
                add_edge_context(
                    edge_context_index,
                    from_ref=source,
                    to_ref=target,
                    relation=str(edge.get("relation") or "message_lineage"),
                    evidence_type=str(edge.get("evidence_type") or "reconstructed_lineage"),
                    evidence_refs=string_list(edge.get("evidence_refs")),
                    confidence=normalized_confidence(edge.get("confidence", 1.0)),
                    eligible_for_attribution=True,
                    inference_method=str(edge.get("inference_method") or "message_lineage_reconstruction"),
                    edge_origin="offline.message_lineage",
                    edge_id=str(edge.get("edge_id") or ""),
                )
                upstream[target][source] = None
                downstream[source][target] = None

        progress_reconstruction = reconstruct_progress_episodes(
            nodes=nodes,
            turns=message_lineage.get("turns") or [],
        )
        for episode in progress_reconstruction.get("episodes") or []:
            nodes[episode.ref] = episode
            aliases[episode.ref] = episode.ref
            aliases[f"record:{episode.record_id}"] = episode.ref
            evidence_eligible_refs.add(episode.ref)
            analysis_start_eligible_refs.add(episode.ref)
            for source_ref in episode.source_refs:
                source = resolve_ref(source_ref, aliases)
                if (
                    source
                    and source != episode.ref
                    and _edge_endpoints_eligible(
                        nodes, evidence_eligible_refs, source, episode.ref
                    )
                ):
                    upstream[episode.ref][source] = None
                    downstream[source][episode.ref] = None
                    add_edge_context(
                        edge_context_index,
                        from_ref=source,
                        to_ref=episode.ref,
                        relation=(
                            "previous_progress_episode"
                            if nodes.get(source) and nodes[source].event_type == "progress.episode"
                            else "progress_episode_member"
                        ),
                        evidence_type="offline_reconstruction",
                        evidence_refs=[source],
                        confidence=1.0,
                        eligible_for_attribution=True,
                        inference_method="progress_episode_reconstruction",
                        edge_origin="offline.progress_reconstruction",
                    )
        for target_ref, episode_refs in (progress_reconstruction.get("target_links") or {}).items():
            if target_ref not in nodes:
                continue
            for episode_ref in episode_refs:
                if (
                    episode_ref not in nodes
                    or episode_ref == target_ref
                    or not _edge_endpoints_eligible(
                        nodes, evidence_eligible_refs, episode_ref, target_ref
                    )
                ):
                    continue
                upstream[target_ref][episode_ref] = None
                downstream[episode_ref][target_ref] = None
                add_edge_context(
                    edge_context_index,
                    from_ref=episode_ref,
                    to_ref=target_ref,
                    relation="progress_episode_projects_to_target",
                    evidence_type="offline_reconstruction",
                    evidence_refs=[episode_ref],
                    confidence=1.0,
                    eligible_for_attribution=True,
                    inference_method="progress_target_projection",
                    edge_origin="offline.progress_reconstruction",
                )
        message_lineage["progress_reconstruction"] = {
            "version": progress_reconstruction.get("version"),
            "collection_mode": progress_reconstruction.get("collection_mode"),
            "behavior_impact": progress_reconstruction.get("behavior_impact"),
            "stats": progress_reconstruction.get("stats") or {},
        }

        manifest = trace.get("manifest") if isinstance(trace.get("manifest"), dict) else {}
        return cls(
            case_id=str(manifest.get("case_id") or trace.get("case_id") or ""),
            nodes=nodes,
            aliases=aliases,
            upstream=upstream,
            downstream=downstream,
            raw_trace=trace,
            artifact_hydration=artifact_hydration,
            artifact_index=artifact_index,
            artifact_root=artifact_root,
            artifact_reader=artifact_reader,
            artifact_records=artifact_records,
            message_lineage=message_lineage,
            edge_context_index=edge_context_index,
            evidence_eligible_refs=evidence_eligible_refs,
            analysis_start_eligible_refs=analysis_start_eligible_refs,
        )

    def hydrate_node(self, ref: str) -> TraceNode:
        resolved = self.resolve(ref) or ref
        node = self.nodes[resolved]
        if resolved in self._hydrated_refs:
            return node
        self._hydrated_refs.add(resolved)
        self.artifact_hydration["requested_nodes"] += 1
        record = self._artifact_records.get(resolved) or {}
        data = dict(node.data)
        hydrated = hydrate_record_artifacts(
            record=record,
            data=data,
            artifact_index=self._artifact_index,
            artifact_reader=self._artifact_reader,
            stats=self.artifact_hydration,
            counted_artifact_outcomes=self._counted_artifact_outcomes,
        )
        if hydrated:
            data["hydrated_artifacts"] = hydrated
            node = replace(node, data=data)
            self.nodes[resolved] = node
        return node

    def resolve(self, ref: str) -> Optional[str]:
        return resolve_ref(ref, self.aliases)

    def evidence_eligible(self, ref: str) -> bool:
        resolved = self.resolve(ref) or ref
        return resolved in self._evidence_eligible_refs

    def active_repository_revision(self) -> Optional[int]:
        """Return the current CaseTrace generation only from authoritative records."""
        cached = getattr(self, "_active_repository_revision", None)
        if hasattr(self, "_active_repository_revision"):
            return cached
        revisions: List[int] = []
        for node in self.nodes.values():
            value = None
            if node.event_type == "response.claim":
                value = node.data.get("repository_revision")
            elif (
                node.event_type == "verification"
                and node.data.get("effective_for_final_state") is True
            ):
                value = node.data.get("repository_revision")
            elif node.event_type == "change":
                value = node.data.get("revision_after")
            if type(value) is int and value >= 0:
                revisions.append(value)
        self._active_repository_revision = max(revisions) if revisions else None
        return self._active_repository_revision

    def active_revision_evidence_eligible(self, ref: str) -> bool:
        """Require canonical evidence to satisfy active revision contracts."""
        resolved = self.resolve(str(ref))
        if (
            not resolved
            or resolved not in self.nodes
            or not self.evidence_eligible(resolved)
        ):
            return False
        data = self.nodes[resolved].data
        revision_status = data.get("revision_status")
        if revision_status not in (None, ""):
            if (
                not isinstance(revision_status, str)
                or revision_status.strip().lower() != "matched"
            ):
                return False

        subject_revision = data.get("subject_revision")
        if subject_revision not in (None, ""):
            if not isinstance(subject_revision, str):
                return False
            manifest = (
                self.raw_trace.get("manifest")
                if isinstance(self.raw_trace.get("manifest"), Mapping)
                else {}
            )
            active_subject_revision = manifest.get("subject_revision")
            if (
                isinstance(active_subject_revision, str)
                and active_subject_revision.strip()
                and subject_revision.strip() != active_subject_revision.strip()
            ):
                return False

        repository_revision = data.get("repository_revision")
        if repository_revision is not None:
            if (
                type(repository_revision) is not int
                or repository_revision < 0
            ):
                return False
            active_repository_revision = self.active_repository_revision()
            if (
                active_repository_revision is not None
                and repository_revision != active_repository_revision
            ):
                return False
        return True

    def active_revision_candidate_eligible(self, ref: str) -> bool:
        """Compatibility alias for the graph-level active evidence policy."""
        return self.active_revision_evidence_eligible(ref)

    def filter_evidence_refs(self, refs: Iterable[Any]) -> List[str]:
        """Keep unresolved refs and refs to nodes allowed by the evidence policy."""
        output: List[str] = []
        for value in refs:
            ref = str(value or "")
            if not ref:
                continue
            resolved = self.resolve(ref)
            if resolved in self.nodes and not self.evidence_eligible(resolved):
                continue
            if (
                resolved in self.nodes
                and not self.active_revision_evidence_eligible(resolved)
            ):
                continue
            output.append(ref)
        return output

    def sanitize_edge_evidence(self, edge: Mapping[str, Any]) -> JsonDict:
        output = dict(edge)
        if "evidence_refs" in output:
            refs = output.get("evidence_refs")
            output["evidence_refs"] = self.filter_evidence_refs(
                refs if isinstance(refs, (list, tuple)) else string_list(refs)
            )
        return output

    def sanitize_judge_edge_evidence(self, edge: Mapping[str, Any]) -> JsonDict:
        output = self.sanitize_edge_evidence(edge)
        output.pop("score", None)
        output.pop("retrieval_score", None)
        if (
            bool(edge.get("retrieval_candidate"))
            or str(edge.get("edge_origin") or "")
            in RANKING_CONFIDENCE_EDGE_ORIGINS
            or str(edge.get("inference_method") or "")
            in RANKING_CONFIDENCE_INFERENCE_METHODS
        ):
            output.pop("confidence", None)
        return output

    def assert_evidence_eligible_references(
        self, value: Any, *, label: str
    ) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                self.assert_evidence_eligible_references(child, label=label)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for child in value:
                self.assert_evidence_eligible_references(child, label=label)
            return
        if not isinstance(value, str):
            return
        resolved = self.resolve(value)
        if (
            resolved in self.nodes
            and not self.active_revision_evidence_eligible(resolved)
        ):
            raise ValueError(
                "{0} violates graph evidence eligibility: {1}".format(label, value)
            )

    def assert_resolved_evidence_references(
        self, refs: Iterable[Any], *, label: str
    ) -> None:
        for value in refs:
            ref = str(value or "")
            resolved = self.resolve(ref)
            if (
                resolved in self.nodes
                and self.active_revision_evidence_eligible(resolved)
            ):
                continue
            artifact = self.artifact_reference_status(ref)
            if artifact is not None and artifact.get("resolution_status") == "resolved":
                continue
            raise ValueError(
                "{0} contains unresolved grounded evidence: {1}".format(label, ref)
            )

    def assert_resolved_node_references(
        self, refs: Iterable[Any], *, label: str
    ) -> None:
        for value in refs:
            ref = str(value or "")
            resolved = self.resolve(ref)
            if (
                resolved in self.nodes
                and self.active_revision_evidence_eligible(resolved)
            ):
                continue
            raise ValueError(
                "{0} contains unresolved or revision-ineligible publication identity: {1}".format(
                    label, ref
                )
            )

    def analysis_start_eligible(self, ref: str) -> bool:
        resolved = self.resolve(ref) or ref
        return resolved in self._analysis_start_eligible_refs

    def edge_endpoints_eligible(self, source_ref: str, target_ref: str) -> bool:
        source = self.resolve(source_ref) or source_ref
        target = self.resolve(target_ref) or target_ref
        return (
            self.active_revision_evidence_eligible(source)
            and self.active_revision_evidence_eligible(target)
        )

    def upstream_refs(self, ref: str) -> List[str]:
        resolved = self.resolve(ref) or ref
        ordered: List[str] = []
        seen: Set[str] = set()
        node = self.nodes.get(resolved)
        if node:
            for source_ref in node.source_refs:
                source = resolve_ref(source_ref, self.aliases)
                if (
                    source
                    and source != resolved
                    and source not in seen
                    and self.edge_endpoints_eligible(source, resolved)
                ):
                    ordered.append(source)
                    seen.add(source)
        for source in sorted(self._upstream.get(resolved, set())):
            if source not in seen:
                ordered.append(source)
                seen.add(source)
        return ordered

    def downstream_refs(self, ref: str) -> List[str]:
        resolved = self.resolve(ref) or ref
        return sorted(self._downstream.get(resolved, set()))

    def bounded_upstream_refs(
        self,
        ref: str,
        *,
        limit: int,
        relation_filter: Iterable[str] = (),
        event_type: str = "",
        exclude: Iterable[str] = (),
        scan_limit: Optional[int] = None,
    ) -> BoundedAdjacencyResult:
        return self._bounded_adjacent_refs(
            ref,
            adjacency=self._upstream,
            upstream=True,
            limit=limit,
            relation_filter=relation_filter,
            event_type=event_type,
            exclude=exclude,
            scan_limit=scan_limit,
        )

    def bounded_downstream_refs(
        self,
        ref: str,
        *,
        limit: int,
        relation_filter: Iterable[str] = (),
        event_type: str = "",
        exclude: Iterable[str] = (),
        scan_limit: Optional[int] = None,
    ) -> BoundedAdjacencyResult:
        return self._bounded_adjacent_refs(
            ref,
            adjacency=self._downstream,
            upstream=False,
            limit=limit,
            relation_filter=relation_filter,
            event_type=event_type,
            exclude=exclude,
            scan_limit=scan_limit,
        )

    def _bounded_adjacent_refs(
        self,
        ref: str,
        *,
        adjacency: Dict[str, Dict[str, None]],
        upstream: bool,
        limit: int,
        relation_filter: Iterable[str],
        event_type: str,
        exclude: Iterable[str],
        scan_limit: Optional[int],
    ) -> BoundedAdjacencyResult:
        """Return deterministic adjacency under independent output and scan bounds."""
        resolved = self.resolve(ref) or ref
        maximum = max(0, int(limit))
        declared_scan_limit = maximum + 1
        physical_limit = (
            declared_scan_limit
            if scan_limit is None
            else min(max(0, int(scan_limit)), declared_scan_limit)
        )
        relations = {str(item) for item in relation_filter if str(item)}
        excluded = {str(item) for item in exclude}
        eligible: List[str] = []
        inspected = 0
        output_truncated = False
        adjacent_items = adjacency.get(resolved, {})
        for adjacent in islice(adjacent_items, physical_limit):
            inspected += 1
            if adjacent in excluded:
                continue
            node = self.nodes.get(adjacent)
            source, target = (
                (adjacent, resolved) if upstream else (resolved, adjacent)
            )
            if not self.edge_endpoints_eligible(source, target):
                continue
            if event_type and (node is None or node.event_type != event_type):
                continue
            if relations:
                edges = (
                    self.edge_context(adjacent, resolved)
                    if upstream
                    else self.edge_context(resolved, adjacent)
                )
                if not any(str(edge.get("relation") or "") in relations for edge in edges):
                    continue
            if len(eligible) < maximum:
                eligible.append(adjacent)
            else:
                output_truncated = True
        scan_truncated = inspected < len(adjacent_items)
        return BoundedAdjacencyResult(
            refs=tuple(eligible),
            truncated=output_truncated or scan_truncated,
            inspected_count=inspected,
            scan_limit=physical_limit,
            scan_truncated=scan_truncated,
        )

    def edge_context(self, from_ref: str, to_ref: str) -> List[JsonDict]:
        source = self.resolve(from_ref) or from_ref
        target = self.resolve(to_ref) or to_ref
        return [
            self.sanitize_edge_evidence(item)
            for item in self._edge_context_index.get((source, target), [])
        ]

    def temporal_adjacency_edges(self, ref: str) -> List[JsonDict]:
        """Return advisory temporal edges whose endpoints are valid evidence."""
        target_ref = self.resolve(ref) or ref
        output: List[JsonDict] = []
        for edge in self.raw_trace.get("dataflow_edges") or []:
            if not isinstance(edge, dict) or not edge_eligibility_well_formed(edge):
                continue
            metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
            normalized = {
                "evidence_type": edge.get("evidence_type") or metadata.get("evidence_type"),
                "relation": edge.get("relation") or metadata.get("relation"),
                "edge_origin": edge.get("edge_origin") or metadata.get("edge_origin"),
                "inference_method": edge.get("inference_method") or metadata.get("inference_method"),
            }
            if not is_temporal_only_edge(normalized):
                continue
            source = resolve_edge_endpoint(edge.get("from"), self.aliases)
            target = resolve_edge_endpoint(edge.get("to"), self.aliases)
            if (
                target != target_ref
                or not source
                or not self.edge_endpoints_eligible(source, target)
            ):
                continue
            output.append(
                {
                    "from_ref": source,
                    "to_ref": target,
                    "relation": str(
                        edge.get("relation")
                        or metadata.get("relation")
                        or "temporal_availability"
                    ),
                    "evidence_type": str(
                        edge.get("evidence_type")
                        or metadata.get("evidence_type")
                        or "temporal_only"
                    ),
                    "evidence_refs": self.filter_evidence_refs(
                        string_list(
                            edge.get("evidence_refs") or metadata.get("evidence_refs")
                        )
                    ),
                    "confidence": edge.get(
                        "confidence", metadata.get("confidence", 0.0)
                    ),
                    "recorded_eligible_for_attribution": declared_edge_eligibility(
                        edge
                    ),
                    "direct_predecessor_eligible": False,
                    "inference_method": str(
                        edge.get("inference_method")
                        or metadata.get("inference_method")
                        or "temporal_adjacency"
                    ),
                    "edge_origin": str(
                        edge.get("edge_origin")
                        or metadata.get("edge_origin")
                        or "trace.dataflow_edges"
                    ),
                }
            )
        for edge in self.message_lineage.get("edges") or []:
            if not isinstance(edge, dict) or not is_temporal_only_edge(edge):
                continue
            source = self.resolve(str(edge.get("from_ref") or "")) or ""
            target = self.resolve(str(edge.get("to_ref") or "")) or ""
            if (
                target != target_ref
                or not source
                or not self.edge_endpoints_eligible(source, target)
            ):
                continue
            output.append(
                self.sanitize_edge_evidence({
                    **dict(edge),
                    "from_ref": source,
                    "to_ref": target,
                    "recorded_eligible_for_attribution": edge.get(
                        "eligible_for_attribution"
                    ),
                    "direct_predecessor_eligible": False,
                })
            )
        output.sort(
            key=lambda item: (
                self.position(str(item["from_ref"])),
                str(item.get("relation") or ""),
            )
        )
        return output

    def add_offline_navigation_edge(
        self,
        from_ref: str,
        to_ref: str,
        *,
        evidence_refs: Iterable[str],
        confidence: float,
    ) -> None:
        source = self.resolve(from_ref) or from_ref
        target = self.resolve(to_ref) or to_ref
        if source not in self.nodes or target not in self.nodes or source == target:
            raise ValueError("offline navigation edge endpoints must resolve to distinct nodes")
        if not self.edge_endpoints_eligible(source, target):
            return
        self._upstream[target][source] = None
        self._downstream[source][target] = None
        add_edge_context(
            self._edge_context_index,
            from_ref=source,
            to_ref=target,
            relation="semantic_navigation_route",
            evidence_type="semantic_inferred",
            evidence_refs=[str(ref) for ref in evidence_refs],
            confidence=normalized_confidence(confidence),
            eligible_for_attribution=True,
            inference_method="bounded_delivery_history_semantic_ranking_v1",
            edge_origin="offline.navigation_routing",
        )

    def semantic_predecessor_edges(self, ref: str) -> List[JsonDict]:
        """Return attribution-eligible incoming edges without changing the trace graph."""
        resolved = self.resolve(ref) or ref
        output: List[JsonDict] = []
        for upstream_ref in self.upstream_refs(resolved):
            if (
                not self.edge_endpoints_eligible(upstream_ref, resolved)
                or not self.active_revision_evidence_eligible(upstream_ref)
            ):
                continue
            for edge in self.edge_context(upstream_ref, resolved):
                if is_temporal_only_edge(edge) or not edge.get("eligible_for_attribution"):
                    continue
                output.append({"ref": upstream_ref, **edge})
        return sorted(
            output,
            key=lambda item: (
                -float(item.get("confidence", 0.0)),
                self.position(str(item.get("ref") or "")),
                str(item.get("relation") or ""),
            ),
        )

    def semantic_search(
        self,
        query_terms: List[str],
        *,
        before_ref: str,
        limit: int,
    ) -> List[JsonDict]:
        """Find earlier semantically overlapping records without manufacturing graph edges."""
        before_position = self.position(self.resolve(before_ref) or before_ref)
        terms = {term.lower() for term in query_terms if len(term) >= 3}
        if not terms or limit <= 0:
            return []
        scored: List[JsonDict] = []
        for node in self.nodes.values():
            if (
                node.event_type == "progress.episode"
                or self.position(node.ref) >= before_position
                or not self.active_revision_evidence_eligible(node.ref)
            ):
                continue
            tokens = set(re.findall(r"[a-zA-Z0-9_]{3,}", stable_json(node.compact()).lower()))
            overlap = len(terms & tokens)
            if overlap:
                scored.append(
                    {
                        "ref": node.ref,
                        "score": overlap / max(len(terms), 1),
                        "evidence_type": "semantic_inferred",
                    }
                )
        return sorted(
            scored,
            key=lambda item: (-float(item["score"]), self.position(str(item["ref"]))),
        )[:limit]

    def artifact_hydration_manifest(self, ref: str) -> JsonDict:
        """Return per-node artifact hydration facts, including unavailable evidence."""
        resolved = self.resolve(ref) or ref
        node = self.hydrate_node(resolved)
        record = self._artifact_records.get(resolved) or {}
        artifact_ids = collect_artifact_ids(record, node.data)
        hydrated = node.data.get("hydrated_artifacts")
        hydrated_items = [dict(item) for item in hydrated if isinstance(item, dict)] if isinstance(hydrated, list) else []
        hydrated_ids = {str(item.get("artifact_id") or "") for item in hydrated_items}
        missing_ids = [artifact_id for artifact_id in artifact_ids if artifact_id not in hydrated_ids]
        truncated_ids = [
            str(item.get("artifact_id") or "")
            for item in hydrated_items
            if item.get("truncated") and item.get("artifact_id")
        ]
        integrity_failures = [
            dict(item)
            for item in self.artifact_hydration.get("integrity_failures") or []
            if isinstance(item, dict) and str(item.get("artifact_id") or "") in artifact_ids
        ]
        return {
            "node_ref": resolved,
            "referenced_artifact_ids": artifact_ids,
            "hydrated_artifacts": hydrated_items,
            "missing_artifact_ids": missing_ids,
            "truncated_artifact_ids": truncated_ids,
            "integrity_failures": integrity_failures,
        }

    def artifact_reference_status(self, ref: str) -> Optional[JsonDict]:
        """Resolve a manifest artifact reference without pretending it is a trace node."""
        raw_ref = str(ref or "")
        artifact_id = raw_ref.removeprefix("artifact:")
        artifact = self._artifact_index.get(artifact_id)
        if not artifact:
            return None
        path_value = artifact.get("path")
        verified = self._artifact_reader.read(artifact_id)
        availability = "available" if verified.file_available else "missing"
        hydration_status = "not_requested"
        for node in self.nodes.values():
            hydrated = node.data.get("hydrated_artifacts")
            if not isinstance(hydrated, list):
                continue
            if any(str(item.get("artifact_id") or "") == artifact_id for item in hydrated if isinstance(item, dict)):
                hydration_status = "hydrated"
                break
        return {
            "raw_ref": raw_ref,
            "artifact_id": artifact_id,
            "canonical_ref": "artifact:{0}".format(artifact_id),
            "resolution_status": "resolved",
            "availability": availability,
            "hydration_status": hydration_status,
            "kind": artifact.get("kind"),
            "label": artifact.get("label"),
            "path": path_value,
            "hash": artifact.get("hash"),
        }

    def incoming_edge_context(
        self,
        ref: str,
        allowed_refs: Optional[Iterable[str]] = None,
    ) -> List[JsonDict]:
        target = self.resolve(ref) or ref
        allowed = None
        if allowed_refs is not None:
            allowed = {self.resolve(item) or item for item in allowed_refs}
        output: List[JsonDict] = []
        for (source, edge_target), edges in self._edge_context_index.items():
            if (
                edge_target != target
                or (allowed is not None and source not in allowed)
                or not self.edge_endpoints_eligible(source, edge_target)
            ):
                continue
            output.extend(self.sanitize_edge_evidence(item) for item in edges)
        output.sort(
            key=lambda item: (
                self.position(str(item.get("from_ref") or "")),
                str(item.get("relation") or ""),
                str(item.get("edge_origin") or ""),
            )
        )
        return output

    def position(self, ref: str) -> int:
        resolved = self.resolve(ref) or ref
        return self._positions.get(resolved, len(self._positions))

    def causal_decision_refs(self, ref: str, limit: int = 24) -> List[str]:
        from .causal_retrieval import root_candidate_eligible

        resolved = self.resolve(ref) or ref
        refs = [
            item
            for item in self.upstream_refs(resolved)
            if item in self.nodes
            and self.nodes[item].event_type == "decision"
            and self.active_revision_evidence_eligible(item)
            and root_candidate_eligible(self.nodes[item])
        ]
        refs.sort(key=lambda item: self._positions.get(item, 0), reverse=True)
        return refs[:limit]

    def upstream_nodes(self, ref: str, limit: int = 12) -> List[TraceNode]:
        resolved = self.resolve(ref) or ref
        current = self.nodes.get(resolved)
        refs = [
            item
            for item in self.upstream_refs(resolved)
            if item in self.nodes
            and self.active_revision_evidence_eligible(item)
        ]
        if current and current.event_type == "response.claim":
            direct_support = resolved_data_refs(current.data.get("direct_support_refs"), self.aliases)
            superseded = resolved_data_refs(current.data.get("superseded_evidence_refs"), self.aliases)
            candidate_context = resolved_data_refs(current.data.get("candidate_context_refs"), self.aliases)
            current_revision = current.data.get("repository_revision")
            original_positions = {item: index for index, item in enumerate(refs)}

            def claim_upstream_priority(item: str) -> tuple:
                node = self.nodes[item]
                revision = node.data.get("repository_revision")
                if item in direct_support:
                    rank = 0
                elif (
                    node.event_type == "verification"
                    and node.data.get("effective_for_final_state") is True
                    and revision == current_revision
                ):
                    rank = 1
                elif node.event_type == "change" and node.data.get("revision_after") == current_revision:
                    rank = 2
                elif node.event_type == "response.output":
                    rank = 2.5
                elif node.event_type in ("evidence.semantic_fact", "evidence.fact"):
                    rank = 3
                elif node.event_type in ("tool.result", "tool.error", "tool.call", "mcp.call", "skill.load"):
                    rank = 4
                elif node.event_type in ("llm.call", "llm.turn"):
                    rank = 5
                elif node.component == "context" or node.event_type.startswith("context."):
                    rank = 6
                elif item in candidate_context:
                    rank = 8
                else:
                    rank = 9
                if item in superseded or node.data.get("effective_for_final_state") is False:
                    rank = 10
                return (rank, original_positions[item])

            refs.sort(key=claim_upstream_priority)
        elif current and current.component == "evaluation":
            refs.sort(
                key=lambda item: (
                    0
                    if self.nodes[item].event_type == "response.claim"
                    else 1
                    if self.nodes[item].event_type == "response.output"
                    else 2
                    if self.nodes[item].event_type == "progress.episode"
                    else 3
                )
            )
        elif current and current.event_type == "response.output":
            refs.sort(
                key=lambda item: (
                    0
                    if self.nodes[item].event_type == "progress.episode"
                    else 1
                    if self.nodes[item].event_type in ("decision", "change", "verification")
                    else 2
                    if self.nodes[item].event_type in ("llm.call", "llm.turn")
                    else 8
                    if self.nodes[item].component == "context"
                    else 4,
                    -self._positions.get(item, 0),
                )
            )
        elif current and current.event_type == "progress.episode":
            refs.sort(
                key=lambda item: (
                    0
                    if self.nodes[item].event_type == "decision"
                    and str(self.nodes[item].data.get("decision_type") or "") == "reasoning_block"
                    else 1
                    if self.nodes[item].event_type == "decision"
                    else 2
                    if self.nodes[item].event_type in ("change", "verification")
                    else 3
                    if self.nodes[item].event_type in ("tool.call", "mcp.call", "skill.load")
                    else 4
                    if self.nodes[item].event_type in ("tool.result", "tool.error", "execution.observation")
                    else 9,
                    self._positions.get(item, 0),
                )
            )
        elif current and current.event_type in ("llm.call", "llm.turn"):
            refs.sort(
                key=lambda item: (
                    0 if self.nodes[item].event_type == "decision" else 1,
                    -self._positions.get(item, 0),
                )
            )
        return [self.hydrate_node(item) for item in refs[:limit]]

    def default_start_refs(self) -> List[str]:
        external_evaluation_starts = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type == "external.evaluation_fact"
            and self.analysis_start_eligible(ref)
        ]
        if external_evaluation_starts:
            return dedupe(external_evaluation_starts)
        failed_cases = [ref for ref, node in self.nodes.items() if node.event_type == "case.failed"]
        manifest = self.raw_trace.get("manifest") if isinstance(self.raw_trace.get("manifest"), dict) else {}
        interrupted = manifest.get("shutdown_disposition") == "interrupted_before_case_completion"
        if not interrupted:
            interrupted = any(
                self.nodes[ref].data.get("shutdown_disposition") == "interrupted_before_case_completion"
                for ref in failed_cases
            )
        if failed_cases and interrupted:
            return failed_cases[-1:]
        offline_defect_starts = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type in ("case.missing_semantic", "case.observed_defect", "case.quality_gap")
        ]
        if offline_defect_starts:
            return dedupe(offline_defect_starts)
        if failed_cases:
            return failed_cases[-1:]
        final_claims = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type == "response.claim"
            and node.component == "result"
            and node.data.get("is_final_for_case") is not False
        ]
        if final_claims:
            claim_kind_rank = {
                "verification": 1,
                "change": 1,
                "requirement": 2,
                "architecture": 2,
                "risk": 3,
                "fact": 4,
            }
            positions = {ref: index for index, ref in enumerate(final_claims)}
            final_claims.sort(
                key=lambda ref: (
                    0 if self.nodes[ref].data.get("quality_flags") else 1,
                    claim_kind_rank.get(str(self.nodes[ref].data.get("claim_kind") or "fact"), 4),
                    positions[ref],
                )
            )
            return final_claims[:8]
        response_outputs = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type == "response.output" and node.component == "result"
        ]
        explicit_final_outputs = [
            ref for ref in response_outputs if self.nodes[ref].data.get("is_final_for_case") is True
        ]
        if explicit_final_outputs:
            return explicit_final_outputs[-1:]
        final_outputs = [
            ref
            for ref in response_outputs
            if self.nodes[ref].data.get("is_final_for_case") is not False
            and self.nodes[ref].data.get("response_role") not in ("progress", "intermediate")
        ]
        if final_outputs:
            return final_outputs[-1:]
        starts: List[str] = []
        for ref, node in self.nodes.items():
            flags = node.data.get("quality_flags")
            if node.event_type == "response.claim" and isinstance(flags, list) and flags:
                starts.append(ref)
        if starts:
            return dedupe(starts)
        case_records = [ref for ref, node in self.nodes.items() if node.event_type in ("case.completed", "case.failed")]
        fallback_records = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type != "external.evaluation_fact"
        ]
        return case_records[-1:] if case_records else fallback_records[-1:]


def artifact_root_for_trace_path(trace_path: Path, trace: JsonDict) -> Path:
    path = Path(trace_path).resolve()
    manifest = trace.get("manifest") if isinstance(trace.get("manifest"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for value in files.values():
        declared = Path(str(value or ""))
        if not declared.parts or declared.is_absolute() or ".." in declared.parts:
            continue
        if len(declared.parts) > len(path.parts):
            continue
        if tuple(path.parts[-len(declared.parts):]) != declared.parts:
            continue
        root = path
        for _ in declared.parts:
            root = root.parent
        return root
    return path.parent


def _edge_endpoints_eligible(
    nodes: Dict[str, TraceNode],
    evidence_eligible_refs: Iterable[str],
    source_ref: str,
    target_ref: str,
) -> bool:
    eligible = evidence_eligible_refs
    return bool(
        source_ref in nodes
        and target_ref in nodes
        and source_ref in eligible
        and target_ref in eligible
    )


def recorded_edge_eligible(edge: JsonDict) -> bool:
    return edge_eligibility_well_formed(edge) and declared_edge_eligibility(edge) is not False


def edge_eligibility_well_formed(edge: JsonDict) -> bool:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    declarations = []
    for container in (edge, metadata):
        if "eligible_for_attribution" not in container:
            continue
        value = container["eligible_for_attribution"]
        if type(value) is not bool:
            return False
        declarations.append(value)
    return len(set(declarations)) <= 1


def declared_edge_eligibility(edge: JsonDict) -> Optional[bool]:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    if "eligible_for_attribution" in edge:
        return edge["eligible_for_attribution"]
    if "eligible_for_attribution" in metadata:
        return metadata["eligible_for_attribution"]
    return None


def hydrate_record_artifacts(
    *,
    record: JsonDict,
    data: JsonDict,
    artifact_index: Dict[str, JsonDict],
    artifact_reader: VerifiedArtifactReader,
    stats: JsonDict,
    counted_artifact_outcomes: Set[str],
    max_chars: int = 64000,
    max_artifact_chars: int = 32000,
    max_artifacts: int = 6,
) -> List[JsonDict]:
    artifact_ids = collect_artifact_ids(record, data)
    if not artifact_ids:
        return []
    remaining = max_chars
    hydrated: List[JsonDict] = []
    for artifact_id in artifact_ids[:max_artifacts]:
        artifact = artifact_index.get(artifact_id)
        if not artifact:
            if artifact_id not in counted_artifact_outcomes:
                counted_artifact_outcomes.add(artifact_id)
                stats["missing"] += 1
            continue
        limit = min(max_artifact_chars, remaining)
        if limit <= 0:
            break
        resolved = artifact_reader.read(artifact_id)
        for failure in resolved.failures:
            _record_artifact_integrity_failure(stats, artifact_id, failure)
        if resolved.content is None or resolved.content_bytes is None:
            if artifact_id not in counted_artifact_outcomes:
                counted_artifact_outcomes.add(artifact_id)
                stats["missing"] += 1
                if resolved.hash_mismatch:
                    stats["hash_mismatches"] += 1
            continue

        excerpt = resolved.content[:limit]
        truncated = resolved.truncated or len(excerpt) < len(resolved.content)
        hydrated_item = {
            "artifact_id": artifact_id,
            "kind": artifact.get("kind"),
            "label": artifact.get("label"),
            "path": artifact.get("path"),
            "hash": artifact.get("hash"),
            "content_hash": artifact.get("content_hash") or artifact.get("hash"),
            "content": excerpt,
            "content_length": len(resolved.content),
            "byte_length": len(resolved.content_bytes),
            "source": resolved.source,
            "hash_status": "verified",
            "file_hash_status": resolved.file_hash_status,
            "truncated": truncated,
        }
        if resolved.source == "embedded_semantic_slice":
            hydrated_item.update(
                {
                    "byte_ranges": _exposed_byte_ranges(
                        resolved.byte_ranges, len(excerpt.encode("utf-8"))
                    ),
                    "semantic_slice_count": resolved.semantic_slice_count,
                    "rejected_semantic_slice_count": resolved.rejected_semantic_slice_count,
                    "slice_hash_status": resolved.slice_hash_status,
                }
            )
        hydrated.append(hydrated_item)
        if artifact_id not in counted_artifact_outcomes:
            counted_artifact_outcomes.add(artifact_id)
            stats["loaded"] += 1
            if truncated:
                stats["truncated"] += 1
            if resolved.source == "embedded_semantic_slice":
                stats["slice_fallbacks"] += 1
            if resolved.hash_mismatch:
                stats["hash_mismatches"] += 1
        remaining -= len(excerpt)
    return hydrated


def _exposed_byte_ranges(
    ranges: Tuple[Tuple[int, int], ...], byte_count: int
) -> List[List[int]]:
    remaining = byte_count
    output: List[List[int]] = []
    for start, end in ranges:
        if remaining <= 0:
            break
        resolved_end = min(end, start + remaining)
        output.append([start, resolved_end])
        remaining -= resolved_end - start
    return output


def _record_artifact_integrity_failure(stats: JsonDict, artifact_id: str, status: str) -> None:
    failures = stats.setdefault("integrity_failures", [])
    failure = {"artifact_id": artifact_id, "status": status}
    if isinstance(failures, list) and failure not in failures:
        failures.append(failure)


def collect_artifact_ids(record: JsonDict, data: JsonDict) -> List[str]:
    found: List[str] = []
    ignored: List[str] = []
    ignored_keys = {
        "candidate_evidence_refs",
        "derived_tool_outcome_refs",
        "candidate_tool_outcome_refs",
        "dependency_tool_outcome_refs",
        "legacy_context_refs",
    }

    def visit(value: Any, *, ignored_branch: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("artifact_id", "payload_ref", "raw_artifact_ref") and isinstance(item, str):
                    target = ignored if ignored_branch else found
                    target.append(item.removeprefix("artifact:"))
                else:
                    visit(item, ignored_branch=ignored_branch or key in ignored_keys)
        elif isinstance(value, list):
            for item in value:
                visit(item, ignored_branch=ignored_branch)

    visit(data)
    ignored_set = set(ignored)
    for value in record.get("artifact_refs") or []:
        if isinstance(value, str) and value.removeprefix("artifact:") not in ignored_set:
            found.append(value.removeprefix("artifact:"))
    return dedupe(found)


def resolved_data_refs(value: Any, aliases: Dict[str, str]) -> Set[str]:
    if not isinstance(value, list):
        return set()
    return {
        resolved
        for item in value
        if isinstance(item, str)
        for resolved in [resolve_ref(item, aliases)]
        if resolved
    }


def record_aliases(record: JsonDict) -> Iterable[str]:
    record_id = str(record.get("record_id") or "")
    event_type = str(record.get("event_type") or "")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if record_id:
        yield f"record:{record_id}"
        yield f"node:{record_id}"
    if event_type in ("evidence.semantic_fact", "evidence.fact") and record_id:
        yield f"evidence:{record_id}"
    if event_type == "external.evaluation_fact":
        if record_id:
            yield f"external_evaluation:{record_id}"
        if data.get("evaluation_id"):
            yield f"external_evaluation:{data['evaluation_id']}"
    if event_type == "change":
        if record_id:
            yield f"change:{record_id}"
        if data.get("change_id"):
            yield f"change:{data['change_id']}"
    if event_type == "verification" and data.get("verification_id"):
        yield f"verification:{data['verification_id']}"
    if event_type == "response.output" and data.get("segment_id"):
        yield f"response_segment:{data['segment_id']}"
    if event_type == "response.claim":
        if record_id:
            yield f"response_claim:{record_id}"
        if data.get("claim_id"):
            yield f"response_claim:{data['claim_id']}"
    if event_type == "claim.support_assessment":
        if record_id:
            yield f"claim_support:{record_id}"
        if data.get("assessment_id"):
            yield f"claim_support:{data['assessment_id']}"
    if event_type == "decision" and data.get("decision_id"):
        yield f"decision:{data['decision_id']}"
    call_id = data.get("call_id") or data.get("callID")
    if call_id:
        if event_type == "tool.error":
            yield f"tool_error:{call_id}"
        elif event_type == "tool.result":
            yield f"tool_result:{call_id}"
        elif event_type == "tool.call":
            yield f"tool_call:{call_id}"
        elif event_type == "mcp.call":
            yield f"mcp:{call_id}"


def resolve_edge_endpoint(endpoint: Any, aliases: Dict[str, str]) -> Optional[str]:
    if not isinstance(endpoint, dict):
        return None
    ref_type = endpoint.get("type")
    ref_id = endpoint.get("id")
    if not ref_type or not ref_id:
        return None
    candidates = [f"{ref_type}:{ref_id}", f"record:{ref_id}", f"node:{ref_id}"]
    for candidate in candidates:
        resolved = resolve_ref(candidate, aliases)
        if resolved:
            return resolved
    return None


def resolve_ref(ref: str, aliases: Dict[str, str]) -> Optional[str]:
    if ref in aliases:
        return aliases[ref]
    if ref.startswith("record:"):
        return aliases.get(ref)
    if ":" not in ref:
        return aliases.get(f"record:{ref}") or aliases.get(f"node:{ref}")
    return None


def add_edge_context(
    index: Dict[Tuple[str, str], List[JsonDict]],
    *,
    from_ref: str,
    to_ref: str,
    relation: str,
    evidence_type: str,
    evidence_refs: List[str],
    confidence: float,
    eligible_for_attribution: bool,
    inference_method: str,
    edge_origin: str,
    source_container: str = "",
    edge_id: str = "",
    recorded_provenance: Optional[Mapping[str, Any]] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> None:
    edge = {
        "from_ref": from_ref,
        "to_ref": to_ref,
        "relation": relation,
        "evidence_type": evidence_type,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "eligible_for_attribution": eligible_for_attribution,
        "inference_method": inference_method,
        "edge_origin": edge_origin,
    }
    if source_container:
        edge["source_container"] = source_container
    if edge_id:
        edge["edge_id"] = edge_id
    if recorded_provenance:
        edge["recorded_provenance"] = {
            str(key): dict(value) if isinstance(value, Mapping) else value
            for key, value in recorded_provenance.items()
        }
    if metadata:
        edge["metadata"] = {
            str(key): copy.deepcopy(value) for key, value in metadata.items()
        }
    bucket = index[(from_ref, to_ref)]
    signature = stable_edge_signature(edge)
    if any(stable_edge_signature(item) == signature for item in bucket):
        return
    bucket.append(edge)


def stable_edge_signature(edge: JsonDict) -> tuple:
    return (
        str(edge.get("from_ref") or ""),
        str(edge.get("to_ref") or ""),
        str(edge.get("relation") or ""),
        str(edge.get("evidence_type") or ""),
        tuple(str(item) for item in edge.get("evidence_refs") or []),
        str(edge.get("inference_method") or ""),
        str(edge.get("edge_origin") or ""),
        str(edge.get("source_container") or ""),
        stable_json(edge.get("recorded_provenance") or {}),
        stable_json(edge.get("metadata") or {}),
    )


def string_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def normalized_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, confidence))


def is_temporal_only_edge(edge: Mapping[str, Any]) -> bool:
    """Identify advisory time adjacency even when a producer marked it eligible."""
    provenance = edge.get("recorded_provenance")
    containers = [edge]
    metadata = edge.get("metadata")
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    if isinstance(provenance, Mapping):
        containers.extend(
            value
            for value in (
                provenance.get("top_level"),
                provenance.get("metadata"),
            )
            if isinstance(value, Mapping)
        )
    for container in containers:
        evidence_type = str(container.get("evidence_type") or "").strip().lower()
        relation = str(container.get("relation") or "").strip().lower()
        origin = str(container.get("edge_origin") or "").strip().lower()
        method = str(container.get("inference_method") or "").strip().lower()
        if evidence_type in {
            "temporal_inferred",
            "temporal_only",
            "temporal_advisory",
        }:
            return True
        if relation in {
            "temporal_availability",
            "available_to_next_request",
            "temporal_adjacency",
        }:
            return True
        if "temporal" in origin or "temporal" in method:
            return True
    return False


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
