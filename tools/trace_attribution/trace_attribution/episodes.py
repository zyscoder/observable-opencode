from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .graph import TraceGraph
from .models import NodeJudgment, TraceNode


EPISODE_EVENT_TYPES = {
    "decision",
    "tool.call",
    "tool.result",
    "tool.error",
    "change",
    "execution.observation",
    "verification",
}


@dataclass(frozen=True)
class CausalEpisode:
    episode_id: str
    member_refs: List[str]


class CausalEpisodeIndex:
    def __init__(self, graph: TraceGraph):
        self.graph = graph
        self._parent = {ref: ref for ref in graph.nodes}
        self._rank = {ref: 0 for ref in graph.nodes}
        self.episodes_by_ref: Dict[str, CausalEpisode] = {}

    @classmethod
    def from_graph(cls, graph: TraceGraph) -> "CausalEpisodeIndex":
        index = cls(graph)
        for left_ref, right_ref in confirmed_episode_pairs(graph):
            index.union(left_ref, right_ref)
        index.freeze()
        return index

    def find(self, ref: str) -> str:
        parent = self._parent[ref]
        if parent != ref:
            self._parent[ref] = self.find(parent)
        return self._parent[ref]

    def union(self, left_ref: str, right_ref: str) -> None:
        if left_ref not in self._parent or right_ref not in self._parent:
            return
        left = self.find(left_ref)
        right = self.find(right_ref)
        if left == right:
            return
        if self._rank[left] < self._rank[right]:
            left, right = right, left
        self._parent[right] = left
        if self._rank[left] == self._rank[right]:
            self._rank[left] += 1

    def freeze(self) -> "CausalEpisodeIndex":
        members: Dict[str, List[str]] = defaultdict(list)
        for ref in self.graph.nodes:
            members[self.find(ref)].append(ref)
        for refs in members.values():
            refs.sort(key=self.graph.position)
            episode = CausalEpisode(episode_id=stable_episode_id(refs), member_refs=refs)
            for ref in refs:
                self.episodes_by_ref[ref] = episode
        return self

    def episode_for(self, ref: str) -> CausalEpisode:
        resolved = self.graph.resolve(ref) or ref
        return self.episodes_by_ref.get(
            resolved,
            CausalEpisode(episode_id=stable_episode_id([resolved]), member_refs=[resolved]),
        )

    def representative(self, candidate_refs: Iterable[str], judgments: Dict[str, NodeJudgment]) -> Optional[str]:
        eligible = []
        for ref in candidate_refs:
            resolved = self.graph.resolve(ref) or ref
            judgment = judgments.get(resolved)
            if not judgment:
                continue
            if (
                judgment.defect_status == "present"
                and judgment.causal_role == "defect_introduction"
                and judgment.is_root_cause
                and judgment.branch_relation in {"same_defect", "causal_precursor"}
            ):
                eligible.append(resolved)
        if not eligible:
            return None
        return min(eligible, key=self.graph.position)


def confirmed_episode_pairs(graph: TraceGraph) -> List[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    add_lineage_pairs(graph, pairs)
    add_identity_pairs(graph, pairs, identity_kind="call_id")
    add_identity_pairs(graph, pairs, identity_kind="span_id")
    add_explicit_execution_pairs(graph, pairs)
    return sorted(pairs)


def add_lineage_pairs(graph: TraceGraph, pairs: Set[Tuple[str, str]]) -> None:
    for edge in graph.message_lineage.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        if edge.get("evidence_type") != "confirmed" or edge.get("relation") != "reasoning_selected_action":
            continue
        add_pair(pairs, str(edge.get("from_ref") or ""), str(edge.get("to_ref") or ""), graph)


def add_identity_pairs(graph: TraceGraph, pairs: Set[Tuple[str, str]], *, identity_kind: str) -> None:
    groups: Dict[str, List[str]] = defaultdict(list)
    raw_by_ref = raw_records_by_ref(graph)
    for ref, node in graph.nodes.items():
        if node.event_type not in EPISODE_EVENT_TYPES:
            continue
        identity = (
            node_call_id(graph.hydrate_node(ref))
            if identity_kind == "call_id"
            else raw_span_id(raw_by_ref.get(ref))
        )
        if identity:
            groups[identity].append(ref)
    for refs in groups.values():
        refs.sort(key=graph.position)
        for left_ref, right_ref in zip(refs, refs[1:]):
            if identity_kind == "span_id" and not span_pair_allowed(graph.nodes[left_ref], graph.nodes[right_ref]):
                continue
            add_pair(pairs, left_ref, right_ref, graph)


def add_explicit_execution_pairs(graph: TraceGraph, pairs: Set[Tuple[str, str]]) -> None:
    allowed_targets = {"tool.result", "tool.error", "change"}
    allowed_sources = {"decision", "tool.call", "tool.result", "tool.error", "change"}
    for ref, node in graph.nodes.items():
        if node.event_type not in allowed_targets:
            continue
        for upstream_ref in graph.upstream_refs(ref):
            upstream = graph.nodes.get(upstream_ref)
            if upstream and upstream.event_type in allowed_sources:
                add_pair(pairs, upstream_ref, ref, graph)


def span_pair_allowed(left: TraceNode, right: TraceNode) -> bool:
    event_types = {left.event_type, right.event_type}
    return bool(event_types & {"change", "tool.call", "tool.result", "tool.error", "execution.observation"})


def node_call_id(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for value in (data.get("call_id"), data.get("callID"), metadata.get("call_id"), metadata.get("callID")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    for artifact in data.get("hydrated_artifacts") or []:
        if not isinstance(artifact, dict) or artifact.get("label") != "decision.metadata":
            continue
        try:
            hydrated = json.loads(str(artifact.get("content") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(hydrated, dict):
            continue
        for value in (hydrated.get("call_id"), hydrated.get("callID")):
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def raw_records_by_ref(graph: TraceGraph) -> Dict[str, Dict[str, object]]:
    output: Dict[str, Dict[str, object]] = {}
    for record in graph.raw_trace.get("records") or []:
        if not isinstance(record, dict) or not record.get("record_id"):
            continue
        output[f"record:{record['record_id']}"] = record
    return output


def raw_span_id(record: Optional[Dict[str, object]]) -> str:
    if not record:
        return ""
    value = record.get("span_id")
    return value.strip() if isinstance(value, str) else ""


def add_pair(pairs: Set[Tuple[str, str]], left_ref: str, right_ref: str, graph: TraceGraph) -> None:
    if left_ref not in graph.nodes or right_ref not in graph.nodes or left_ref == right_ref:
        return
    pair = tuple(sorted((left_ref, right_ref)))
    pairs.add(pair)


def stable_episode_id(refs: Iterable[str]) -> str:
    canonical = "\n".join(sorted(set(refs)))
    return f"episode_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"
