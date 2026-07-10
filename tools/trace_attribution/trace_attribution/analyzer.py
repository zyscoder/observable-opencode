from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .graph import TraceGraph
from .models import AttributionReport, NodeJudgment, RootCauseCandidate, TraceNode
from .trace_improvement import build_trace_improvement_report


class JudgeClient:
    def judge_node(
        self,
        *,
        node: TraceNode,
        upstream_nodes: List[TraceNode],
        downstream_context: List[str],
        objective: str,
    ) -> NodeJudgment:
        raise NotImplementedError


class BackwardTaintAnalyzer:
    def __init__(self, *, judge: JudgeClient, max_depth: int = 8, max_nodes: int = 48):
        self.judge = judge
        self.max_depth = max_depth
        self.max_nodes = max_nodes

    def analyze(
        self,
        graph: TraceGraph,
        *,
        start_refs: Optional[Iterable[str]] = None,
        objective: str = "Find the root cause of the observed bad final result.",
    ) -> AttributionReport:
        starts = [graph.resolve(ref) or ref for ref in (start_refs or graph.default_start_refs())]
        starts = [ref for ref in starts if ref in graph.nodes]
        queue = deque((ref, [ref], 0) for ref in starts)
        visited: Set[str] = set()
        visited_order: List[str] = []
        judgments: Dict[str, NodeJudgment] = {}
        root_causes: Dict[str, RootCauseCandidate] = {}
        taint_paths: List[List[str]] = []
        unresolved_refs: List[str] = []

        while queue and len(visited_order) < self.max_nodes:
            ref, path, depth = queue.popleft()
            if ref in visited or ref not in graph.nodes:
                continue
            visited.add(ref)
            visited_order.append(ref)
            node = graph.nodes[ref]
            upstream_nodes = graph.upstream_nodes(ref)
            judgment = self.judge.judge_node(
                node=node,
                upstream_nodes=upstream_nodes,
                downstream_context=path,
                objective=objective,
            )
            judgments[ref] = judgment
            if not judgment.has_defect:
                continue

            next_refs = self._resolve_influences(graph, judgment, ref)
            if depth >= self.max_depth:
                next_refs = []
            if not next_refs:
                root_causes[ref] = RootCauseCandidate(
                    node_ref=ref,
                    component=node.component,
                    event_type=node.event_type,
                    defect_type=judgment.defect_type,
                    reason=judgment.defect_reason,
                    confidence=judgment.confidence,
                )
                taint_paths.append(path)
                continue

            enqueued = False
            for next_ref in next_refs:
                if next_ref not in graph.nodes:
                    unresolved_refs.append(next_ref)
                    continue
                queue.append((next_ref, path + [next_ref], depth + 1))
                enqueued = True
            if not enqueued:
                root_causes[ref] = RootCauseCandidate(
                    node_ref=ref,
                    component=node.component,
                    event_type=node.event_type,
                    defect_type=judgment.defect_type,
                    reason=judgment.defect_reason,
                    confidence=judgment.confidence,
                )
                taint_paths.append(path)

        report = AttributionReport(
            case_id=graph.case_id,
            objective=objective,
            start_refs=starts,
            root_causes=list(root_causes.values()),
            taint_paths=dedupe_paths(taint_paths),
            node_judgments=judgments,
            visited_order=visited_order,
            unresolved_refs=sorted(set(unresolved_refs)),
            metadata={
                "analysis": "backward_semantic_taint",
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
            },
        )
        return replace(report, trace_improvement_report=build_trace_improvement_report(graph, report))

    def _resolve_influences(self, graph: TraceGraph, judgment: NodeJudgment, current_ref: str) -> List[str]:
        explicit = []
        for influence in judgment.influenced_by:
            resolved = graph.resolve(influence.upstream_ref) or influence.upstream_ref
            if resolved != current_ref:
                explicit.append(resolved)
        if explicit:
            return dedupe(explicit)
        if judgment.is_root_cause:
            return []
        return []


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def dedupe_paths(paths: Iterable[List[str]]) -> List[List[str]]:
    seen: Set[Tuple[str, ...]] = set()
    output: List[List[str]] = []
    for path in paths:
        key = tuple(path)
        if key in seen:
            continue
        seen.add(key)
        output.append(path)
    return output
