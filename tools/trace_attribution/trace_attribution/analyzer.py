from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .graph import TraceGraph
from .models import AttributionReport, NodeJudgment, RootCauseCandidate, TaintInfluence, TraceNode
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
        visited_paths: Dict[str, List[str]] = {}
        judge_errors: List[Dict[str, str]] = []

        while queue and len(visited_order) < self.max_nodes:
            ref, path, depth = queue.popleft()
            if ref in visited or ref not in graph.nodes:
                continue
            visited.add(ref)
            visited_order.append(ref)
            visited_paths[ref] = path
            node = graph.nodes[ref]
            upstream_nodes = graph.upstream_nodes(ref)
            try:
                judgment = self.judge.judge_node(
                    node=node,
                    upstream_nodes=upstream_nodes,
                    downstream_context=path,
                    objective=objective,
                )
            except Exception as exc:
                judge_errors.append({"node_ref": ref, "error": f"{type(exc).__name__}: {exc}"})
                judgment = fallback_judgment_after_error(node=node, upstream_nodes=upstream_nodes, error=exc)
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

        for ref, judgment in list(judgments.items()):
            if not judgment.has_defect or ref in root_causes:
                continue
            next_refs = self._resolve_influences(graph, judgment, ref)
            if not next_refs:
                continue
            resolved = [item for item in next_refs if item in judgments]
            if not resolved:
                continue
            if any(judgments[item].has_defect for item in resolved):
                continue
            node = graph.nodes.get(ref)
            if not node:
                continue
            root_causes[ref] = RootCauseCandidate(
                node_ref=ref,
                component=node.component,
                event_type=node.event_type,
                defect_type=judgment.defect_type or "defect_boundary",
                reason=(
                    judgment.defect_reason
                    + " Upstream refs were judged non-defective, so this node is preserved as the "
                    "defect-introduction boundary for partial attribution."
                ).strip(),
                confidence=judgment.confidence,
            )
            taint_paths.append(visited_paths.get(ref, [ref]))

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
                "judge_error_count": len(judge_errors),
                "judge_errors": judge_errors,
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


def fallback_judgment_after_error(
    *,
    node: TraceNode,
    upstream_nodes: List[TraceNode],
    error: Exception,
) -> NodeJudgment:
    error_text = f"{type(error).__name__}: {error}"
    if node.event_type in ("case.observed_defect", "case.quality_gap", "case.missing_semantic") and upstream_nodes:
        return NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_type=f"judge_unavailable_{offline_boundary_name(node.event_type)}_boundary",
            defect_reason=(
                "The attribution judge failed on an offline defect boundary node. The analyzer used the "
                "node's explicit source_refs as a conservative fallback path instead of treating the "
                "evaluation boundary itself as the root cause."
            ),
            influenced_by=[
                TaintInfluence(
                    upstream_ref=item.ref,
                    reason="Fallback propagation through the offline defect node's explicit source_refs.",
                    confidence=0.2,
                )
                for item in upstream_nodes
            ],
            is_root_cause=False,
            severity="unknown",
            confidence=0.2,
            model_notes=error_text,
        )
    if node.event_type == "tool.error":
        return NodeJudgment(
            node_ref=node.ref,
            component=node.component,
            event_type=node.event_type,
            has_defect=True,
            defect_type="tool_error_observed",
            defect_reason=tool_error_fallback_reason(node),
            influenced_by=[],
            is_root_cause=True,
            severity="medium",
            confidence=0.45,
            model_notes=error_text,
        )
    return NodeJudgment(
        node_ref=node.ref,
        component=node.component,
        event_type=node.event_type,
        has_defect=True,
        defect_type="judge_error",
        defect_reason=(
            "The attribution judge failed while evaluating this node, so the analyzer "
            "kept it as a partial boundary candidate instead of dropping the trace."
        ),
        influenced_by=[],
        is_root_cause=True,
        severity="unknown",
        confidence=0.1,
        model_notes=error_text,
    )


def offline_boundary_name(event_type: str) -> str:
    return {
        "case.observed_defect": "observed_defect",
        "case.quality_gap": "quality_gap",
        "case.missing_semantic": "missing_semantic",
    }.get(event_type, "offline_defect")


def tool_error_fallback_reason(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    tool_name = str(data.get("tool_name") or data.get("name") or "unknown_tool")
    call_id = str(data.get("call_id") or data.get("callID") or "")
    error_kind = str(data.get("error_kind") or "")
    error_message = str(data.get("error_message") or "")
    if not error_message and isinstance(data.get("error"), dict):
        error_message = str(data["error"].get("message") or "")
    handled_status = str(data.get("handled_status") or "")
    observed_by_model = data.get("observed_by_model")
    parts = [f"Tool call failed while running {tool_name}"]
    if call_id:
        parts.append(f"call_id={call_id}")
    if error_kind:
        parts.append(f"error_kind={error_kind}")
    if error_message:
        parts.append(f"error_message={error_message}")
    if observed_by_model is not None:
        parts.append(f"observed_by_model={bool(observed_by_model)}")
    if handled_status:
        parts.append(f"handled_status={handled_status}")
    return ". ".join(parts) + "."
