from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .models import JsonDict, TraceNode


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
    ):
        self.case_id = case_id
        self.nodes = nodes
        self.aliases = aliases
        self._upstream = upstream
        self._downstream = downstream
        self.raw_trace = raw_trace

    @classmethod
    def from_file(cls, trace_file: Path) -> "TraceGraph":
        with Path(trace_file).open("r", encoding="utf-8") as handle:
            return cls.from_trace(json.load(handle))

    @classmethod
    def from_trace(cls, trace: JsonDict) -> "TraceGraph":
        nodes: Dict[str, TraceNode] = {}
        aliases: Dict[str, str] = {}
        records = trace.get("records") or []
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id") or "")
            if not record_id:
                continue
            ref = f"record:{record_id}"
            node = TraceNode(
                ref=ref,
                record_id=record_id,
                component=str(record.get("component") or ""),
                event_type=str(record.get("event_type") or ""),
                title=str(record.get("title") or ""),
                status=str(record.get("status") or ""),
                timestamp=str(record.get("timestamp") or ""),
                data=record.get("data") if isinstance(record.get("data"), dict) else {},
                source_refs=[str(item) for item in record.get("source_refs") or []],
            )
            nodes[ref] = node
            for alias in record_aliases(record):
                aliases[alias] = ref
            aliases[ref] = ref

        upstream: Dict[str, Set[str]] = defaultdict(set)
        downstream: Dict[str, Set[str]] = defaultdict(set)
        for node in nodes.values():
            for source_ref in node.source_refs:
                source = resolve_ref(source_ref, aliases)
                if source and source != node.ref:
                    upstream[node.ref].add(source)
                    downstream[source].add(node.ref)

        for edge in trace.get("dataflow_edges") or []:
            if not isinstance(edge, dict):
                continue
            source = resolve_edge_endpoint(edge.get("from"), aliases)
            target = resolve_edge_endpoint(edge.get("to"), aliases)
            if source and target and source != target:
                upstream[target].add(source)
                downstream[source].add(target)

        manifest = trace.get("manifest") if isinstance(trace.get("manifest"), dict) else {}
        return cls(
            case_id=str(manifest.get("case_id") or trace.get("case_id") or ""),
            nodes=nodes,
            aliases=aliases,
            upstream=upstream,
            downstream=downstream,
            raw_trace=trace,
        )

    def resolve(self, ref: str) -> Optional[str]:
        return resolve_ref(ref, self.aliases)

    def upstream_refs(self, ref: str) -> List[str]:
        resolved = self.resolve(ref) or ref
        return sorted(self._upstream.get(resolved, set()))

    def downstream_refs(self, ref: str) -> List[str]:
        resolved = self.resolve(ref) or ref
        return sorted(self._downstream.get(resolved, set()))

    def upstream_nodes(self, ref: str, limit: int = 12) -> List[TraceNode]:
        return [self.nodes[item] for item in self.upstream_refs(ref)[:limit] if item in self.nodes]

    def default_start_refs(self) -> List[str]:
        offline_defect_starts = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type in ("case.missing_semantic", "case.observed_defect", "case.quality_gap")
        ]
        if offline_defect_starts:
            return dedupe(offline_defect_starts)
        starts: List[str] = []
        for ref, node in self.nodes.items():
            flags = node.data.get("quality_flags")
            if node.event_type == "response.claim" and isinstance(flags, list) and flags:
                starts.append(ref)
            elif node.event_type == "case.failed":
                starts.append(ref)
        if starts:
            return dedupe(starts)
        final_claims = [
            ref
            for ref, node in self.nodes.items()
            if node.event_type == "response.claim" and node.component == "result"
        ]
        if final_claims:
            return final_claims[-3:]
        case_records = [ref for ref, node in self.nodes.items() if node.event_type in ("case.completed", "case.failed")]
        return case_records[-1:] if case_records else list(self.nodes.keys())[-1:]


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


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
