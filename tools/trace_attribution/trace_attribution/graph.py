from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import JsonDict, TraceNode, stable_json
from .progress import reconstruct_progress_episodes
from .reconstruction import reconstruct_message_lineage


class TraceGraph:
    def __init__(
        self,
        *,
        case_id: str,
        nodes: Dict[str, TraceNode],
        aliases: Dict[str, str],
        upstream: Dict[str, Set[str]],
        downstream: Dict[str, Set[str]],
        raw_trace: JsonDict,
        artifact_hydration: JsonDict,
        artifact_index: Dict[str, JsonDict],
        artifact_root: Optional[Path],
        artifact_records: Dict[str, JsonDict],
        message_lineage: JsonDict,
        edge_context_index: Dict[Tuple[str, str], List[JsonDict]],
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
        self._artifact_records = artifact_records
        self._hydrated_refs: Set[str] = set()
        self.message_lineage = message_lineage
        self._edge_context_index = edge_context_index
        self._positions = {ref: index for index, ref in enumerate(nodes)}

    @classmethod
    def from_file(cls, trace_file: Path) -> "TraceGraph":
        path = Path(trace_file)
        with path.open("r", encoding="utf-8") as handle:
            return cls.from_trace(json.load(handle), artifact_root=path.parent)

    @classmethod
    def from_trace(cls, trace: JsonDict, artifact_root: Optional[Path] = None) -> "TraceGraph":
        nodes: Dict[str, TraceNode] = {}
        aliases: Dict[str, str] = {}
        artifact_index = {
            str(item.get("artifact_id")): item
            for item in trace.get("artifacts") or []
            if isinstance(item, dict) and item.get("artifact_id")
        }
        artifact_hydration: JsonDict = {
            "referenced": 0,
            "indexed": len(artifact_index),
            "requested_nodes": 0,
            "loaded": 0,
            "missing": 0,
            "truncated": 0,
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
            artifact_hydration["referenced"] += len(collect_artifact_ids(record, data))
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

        upstream: Dict[str, Set[str]] = defaultdict(set)
        downstream: Dict[str, Set[str]] = defaultdict(set)
        edge_context_index: Dict[Tuple[str, str], List[JsonDict]] = defaultdict(list)
        for node in nodes.values():
            for source_ref in node.source_refs:
                source = resolve_ref(source_ref, aliases)
                if source and source != node.ref:
                    upstream[node.ref].add(source)
                    downstream[source].add(node.ref)
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
            if edge.get("eligible_for_attribution") is False or metadata.get("eligible_for_attribution") is False:
                continue
            source = resolve_edge_endpoint(edge.get("from"), aliases)
            target = resolve_edge_endpoint(edge.get("to"), aliases)
            if source and target and source != target:
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
                    edge_origin="trace.dataflow_edges",
                    edge_id=str(edge.get("edge_id") or ""),
                )
                upstream[target].add(source)
                downstream[source].add(target)

        message_lineage = reconstruct_message_lineage(
            trace=trace,
            nodes=nodes,
            aliases=aliases,
            artifact_root=artifact_root,
        )
        for edge in message_lineage.get("edges") or []:
            if not isinstance(edge, dict) or not edge.get("eligible_for_attribution"):
                continue
            source = str(edge.get("from_ref") or "")
            target = str(edge.get("to_ref") or "")
            if source in nodes and target in nodes and source != target:
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
                upstream[target].add(source)
                downstream[source].add(target)

        progress_reconstruction = reconstruct_progress_episodes(
            nodes=nodes,
            turns=message_lineage.get("turns") or [],
        )
        for episode in progress_reconstruction.get("episodes") or []:
            nodes[episode.ref] = episode
            aliases[episode.ref] = episode.ref
            aliases[f"record:{episode.record_id}"] = episode.ref
            for source_ref in episode.source_refs:
                source = resolve_ref(source_ref, aliases)
                if source and source != episode.ref:
                    upstream[episode.ref].add(source)
                    downstream[source].add(episode.ref)
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
                if episode_ref not in nodes or episode_ref == target_ref:
                    continue
                upstream[target_ref].add(episode_ref)
                downstream[episode_ref].add(target_ref)
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
            artifact_records=artifact_records,
            message_lineage=message_lineage,
            edge_context_index=edge_context_index,
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
            artifact_root=self._artifact_root,
            stats=self.artifact_hydration,
        )
        if hydrated:
            data["hydrated_artifacts"] = hydrated
            node = replace(node, data=data)
            self.nodes[resolved] = node
        return node

    def resolve(self, ref: str) -> Optional[str]:
        return resolve_ref(ref, self.aliases)

    def upstream_refs(self, ref: str) -> List[str]:
        resolved = self.resolve(ref) or ref
        ordered: List[str] = []
        seen: Set[str] = set()
        node = self.nodes.get(resolved)
        if node:
            for source_ref in node.source_refs:
                source = resolve_ref(source_ref, self.aliases)
                if source and source != resolved and source not in seen:
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

    def edge_context(self, from_ref: str, to_ref: str) -> List[JsonDict]:
        source = self.resolve(from_ref) or from_ref
        target = self.resolve(to_ref) or to_ref
        return [dict(item) for item in self._edge_context_index.get((source, target), [])]

    def semantic_predecessor_edges(self, ref: str) -> List[JsonDict]:
        """Return attribution-eligible incoming edges without changing the trace graph."""
        resolved = self.resolve(ref) or ref
        output: List[JsonDict] = []
        for upstream_ref in self.upstream_refs(resolved):
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
            if node.event_type == "progress.episode" or self.position(node.ref) >= before_position:
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
        return {
            "node_ref": resolved,
            "referenced_artifact_ids": artifact_ids,
            "hydrated_artifacts": hydrated_items,
            "missing_artifact_ids": missing_ids,
            "truncated_artifact_ids": truncated_ids,
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
            if edge_target != target or (allowed is not None and source not in allowed):
                continue
            output.extend(dict(item) for item in edges)
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
        resolved = self.resolve(ref) or ref
        refs = [
            item
            for item in self.upstream_refs(resolved)
            if item in self.nodes and self.nodes[item].event_type == "decision"
        ]
        refs.sort(key=lambda item: self._positions.get(item, 0), reverse=True)
        return refs[:limit]

    def upstream_nodes(self, ref: str, limit: int = 12) -> List[TraceNode]:
        resolved = self.resolve(ref) or ref
        current = self.nodes.get(resolved)
        refs = [item for item in self.upstream_refs(resolved) if item in self.nodes]
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
        offline_defect_starts = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type in ("case.missing_semantic", "case.observed_defect", "case.quality_gap")
        ]
        if offline_defect_starts:
            return dedupe(offline_defect_starts)
        failed_cases = [ref for ref, node in self.nodes.items() if node.event_type == "case.failed"]
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
        return case_records[-1:] if case_records else list(self.nodes.keys())[-1:]


def hydrate_record_artifacts(
    *,
    record: JsonDict,
    data: JsonDict,
    artifact_index: Dict[str, JsonDict],
    artifact_root: Optional[Path],
    stats: JsonDict,
    max_chars: int = 64000,
    max_artifact_chars: int = 32000,
    max_artifacts: int = 6,
) -> List[JsonDict]:
    artifact_ids = collect_artifact_ids(record, data)
    if not artifact_ids or artifact_root is None:
        return []
    root = Path(artifact_root).resolve()
    remaining = max_chars
    hydrated: List[JsonDict] = []
    for artifact_id in artifact_ids[:max_artifacts]:
        artifact = artifact_index.get(artifact_id)
        if not artifact or not artifact.get("path"):
            stats["missing"] += 1
            continue
        candidate = (root / str(artifact["path"])).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            stats["missing"] += 1
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stats["missing"] += 1
            continue
        limit = min(max_artifact_chars, remaining)
        if limit <= 0:
            stats["truncated"] += 1
            break
        excerpt = content[:limit]
        truncated = len(content) > len(excerpt)
        hydrated.append(
            {
                "artifact_id": artifact_id,
                "kind": artifact.get("kind"),
                "label": artifact.get("label"),
                "path": artifact.get("path"),
                "hash": artifact.get("hash"),
                "content": excerpt,
                "content_length": len(content),
                "truncated": truncated,
            }
        )
        stats["loaded"] += 1
        if truncated:
            stats["truncated"] += 1
        remaining -= len(excerpt)
    return hydrated


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
    edge_id: str = "",
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
    if edge_id:
        edge["edge_id"] = edge_id
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


def is_temporal_only_edge(edge: JsonDict) -> bool:
    """Identify advisory time adjacency even when a producer marked it eligible."""
    evidence_type = str(edge.get("evidence_type") or "").strip().lower()
    relation = str(edge.get("relation") or "").strip().lower()
    origin = str(edge.get("edge_origin") or "").strip().lower()
    method = str(edge.get("inference_method") or "").strip().lower()
    if evidence_type in {"temporal_inferred", "temporal_only", "temporal_advisory"}:
        return True
    if relation in {"temporal_availability", "available_to_next_request", "temporal_adjacency"}:
        return True
    return "temporal" in origin or "temporal" in method


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
