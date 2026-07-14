from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .episodes import CausalEpisodeIndex
from .graph import TraceGraph
from .models import (
    AttributionReport,
    DefectBranchResult,
    NodeJudgment,
    RootCauseCandidate,
    TaintInfluence,
    TraceNode,
)
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
        episode_index = CausalEpisodeIndex.from_graph(graph)
        branches = [
            self._analyze_branch(
                graph,
                start_ref=start_ref,
                objective=objective,
                episode_index=episode_index,
            )
            for start_ref in starts
        ]

        judgments: Dict[str, NodeJudgment] = {}
        for branch in branches:
            for ref, judgment in branch.node_judgments.items():
                judgments.setdefault(ref, judgment)
        root_causes = aggregate_root_causes(branches, graph)
        taint_paths = dedupe_paths(path for branch in branches for path in branch.taint_paths)
        visited_order = dedupe(ref for branch in branches for ref in branch.visited_order)
        unresolved_refs = sorted({ref for branch in branches for ref in branch.unresolved_refs})
        judge_errors = [
            dict(error, branch_id=branch.branch_id)
            for branch in branches
            for error in branch.metadata.get("judge_errors") or []
        ]
        branch_outcomes = [branch.analysis_outcome for branch in branches]
        analysis_outcome = aggregate_analysis_outcome(branch_outcomes)
        termination_reason = aggregate_termination_reason(branches)

        report = AttributionReport(
            case_id=graph.case_id,
            objective=objective,
            start_refs=starts,
            root_causes=root_causes,
            taint_paths=taint_paths,
            node_judgments=judgments,
            visited_order=visited_order,
            unresolved_refs=unresolved_refs,
            defect_branches=branches,
            metadata={
                "analysis": "backward_semantic_taint",
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "budget_scope": "per_defect_branch",
                "judge_timeout_seconds": getattr(self.judge, "timeout_seconds", None),
                "judge_thinking_mode": getattr(self.judge, "thinking_mode", None),
                "judge_thinking_config": getattr(self.judge, "thinking_config", None),
                "judge_error_count": len(judge_errors),
                "judge_errors": judge_errors,
                "analysis_outcome": analysis_outcome,
                "termination_reason": termination_reason,
                "defect_branch_count": len(branches),
                "defect_branch_outcomes": {
                    branch.branch_id: branch.analysis_outcome for branch in branches
                },
                "artifact_hydration": graph.artifact_hydration,
                "message_lineage": {
                    "version": graph.message_lineage.get("version"),
                    "collection_mode": graph.message_lineage.get("collection_mode"),
                    "behavior_impact": graph.message_lineage.get("behavior_impact"),
                    "stats": graph.message_lineage.get("stats") or {},
                },
            },
        )
        return replace(report, trace_improvement_report=build_trace_improvement_report(graph, report))

    def _analyze_branch(
        self,
        graph: TraceGraph,
        *,
        start_ref: str,
        objective: str,
        episode_index: CausalEpisodeIndex,
    ) -> DefectBranchResult:
        queue = deque([(start_ref, [start_ref], 0)])
        visited: Set[str] = set()
        visited_order: List[str] = []
        judgments: Dict[str, NodeJudgment] = {}
        root_causes: Dict[str, RootCauseCandidate] = {}
        root_paths: Dict[str, List[str]] = {}
        unresolved_refs: List[str] = []
        visited_paths: Dict[str, List[str]] = {}
        provisional_surface_roots: Dict[str, Tuple[RootCauseCandidate, List[str], List[str]]] = {}
        judge_errors: List[Dict[str, str]] = []
        depth_limit_hit = False

        while queue and len(visited_order) < self.max_nodes:
            ref, path, depth = queue.popleft()
            if ref in visited or ref not in graph.nodes:
                continue
            visited.add(ref)
            visited_order.append(ref)
            visited_paths[ref] = path
            node = graph.hydrate_node(ref)
            upstream_nodes = graph.upstream_nodes(ref)
            if is_observability_gap(node):
                judgment = NodeJudgment(
                    node_ref=ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="unknown",
                    defect_type="trace_semantic_gap",
                    defect_reason=(
                        "This offline record declares an observability gap. It does not prove that any cited "
                        "execution or change node introduced a behavioral defect."
                    ),
                    causal_role="unknown",
                    influenced_by=[],
                    is_root_cause=False,
                    severity="unknown",
                    confidence=1.0,
                )
            elif is_evaluation_assertion(node):
                evaluation_refs = graph.upstream_refs(ref)
                judgment = NodeJudgment(
                    node_ref=ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=True,
                    defect_status="present",
                    defect_type=str(node.data.get("failure_type") or node.data.get("gap_kind") or "evaluated_defect"),
                    defect_reason=(
                        "This offline evaluation boundary declares an observed defect or quality gap. "
                        "Backward analysis starts from its cited outcome evidence without treating the evaluation node as an introducer."
                    ),
                    causal_role="defect_evidence",
                    influenced_by=[
                        TaintInfluence(
                            upstream_ref=item,
                            reason="The offline evaluation cites this trace record as outcome evidence.",
                            confidence=1.0,
                            relation="derived_from",
                        )
                        for item in evaluation_refs
                    ],
                    is_root_cause=False,
                    severity="unknown",
                    confidence=1.0,
                )
            else:
                try:
                    judgment = self.judge.judge_node(
                        node=node,
                        upstream_nodes=upstream_nodes,
                        downstream_context=semantic_downstream_context(graph, path),
                        objective=objective,
                    )
                except Exception as exc:
                    judge_errors.append({"node_ref": ref, "error": f"{type(exc).__name__}: {exc}"})
                    judgment = fallback_judgment_after_error(node=node, upstream_nodes=upstream_nodes, error=exc)
            judgments[ref] = judgment
            if judgment.defect_status != "present":
                continue

            next_refs = self._resolve_influences(graph, judgment, ref)
            if next_refs and depth >= self.max_depth:
                depth_limit_hit = True
                continue
            if not next_refs:
                if is_evaluation_assertion(node):
                    continue
                if judgment.causal_role != "defect_introduction" or not judgment.is_root_cause:
                    continue
                candidate = RootCauseCandidate(
                    node_ref=ref,
                    component=node.component,
                    event_type=node.event_type,
                    defect_type=judgment.defect_type,
                    reason=judgment.defect_reason,
                    confidence=judgment.confidence,
                )
                decision_refs = (
                    graph.causal_decision_refs(ref)
                    if node.event_type in ("llm.call", "llm.turn", "response.output", "response.claim")
                    else []
                )
                if decision_refs:
                    provisional_surface_roots[ref] = (candidate, path, decision_refs)
                    for decision_ref in decision_refs:
                        queue.append((decision_ref, path + [decision_ref], depth + 1))
                    continue
                root_causes[ref] = candidate
                root_paths[ref] = path
                continue

            for next_ref in next_refs:
                if next_ref not in graph.nodes:
                    unresolved_refs.append(next_ref)
                    continue
                queue.append((next_ref, path + [next_ref], depth + 1))

        for ref, judgment in list(judgments.items()):
            if judgment.defect_status != "present" or ref in root_causes:
                continue
            next_refs = self._resolve_influences(graph, judgment, ref)
            if not next_refs:
                continue
            resolved = [item for item in next_refs if item in judgments]
            if not resolved:
                continue
            if len(resolved) != len(next_refs):
                continue
            upstream_statuses = [judgments[item].defect_status for item in resolved]
            if any(status == "present" for status in upstream_statuses):
                continue
            node = graph.nodes.get(ref)
            if not node:
                continue
            if is_evaluation_assertion(node):
                continue
            if any(status == "unknown" for status in upstream_statuses):
                continue
            if judgment.causal_role != "defect_introduction" or not judgment.is_root_cause:
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
            root_paths[ref] = visited_paths.get(ref, [ref])

        for ref, (candidate, path, decision_refs) in provisional_surface_roots.items():
            decision_judgments = [judgments.get(item) for item in decision_refs]
            if any(
                item
                and item.defect_status == "present"
                and item.causal_role in ("defect_introduction", "defect_propagation")
                for item in decision_judgments
            ):
                continue
            if not decision_judgments or any(item is None or item.defect_status == "unknown" for item in decision_judgments):
                continue
            root_causes[ref] = candidate
            root_paths[ref] = path

        node_limit_hit = bool(queue) and len(visited_order) >= self.max_nodes
        termination_reason = (
            "node_limit" if node_limit_hit else "depth_limit" if depth_limit_hit else "queue_exhausted"
        )
        if root_causes:
            analysis_outcome = "root_found"
        elif (
            node_limit_hit
            or depth_limit_hit
            or unresolved_refs
            or judge_errors
            or any(item.defect_status in ("present", "unknown") for item in judgments.values())
        ):
            analysis_outcome = "inconclusive"
        else:
            analysis_outcome = "no_defect"

        collapsed_roots, collapsed_paths = collapse_episode_roots(
            graph=graph,
            episode_index=episode_index,
            roots=root_causes,
            root_paths=root_paths,
            judgments=judgments,
            observed_defect_ref=start_ref,
        )
        if root_causes and not collapsed_roots:
            analysis_outcome = "inconclusive"

        branch_id = f"defect_branch:{start_ref}"
        return DefectBranchResult(
            branch_id=branch_id,
            start_ref=start_ref,
            defect_type=start_defect_type(graph, start_ref),
            analysis_outcome=analysis_outcome,
            root_causes=collapsed_roots,
            taint_paths=collapsed_paths,
            node_judgments=judgments,
            visited_order=visited_order,
            unresolved_refs=sorted(set(unresolved_refs)),
            metadata={
                "judge_error_count": len(judge_errors),
                "judge_errors": judge_errors,
                "termination_reason": termination_reason,
                "node_limit_hit": node_limit_hit,
                "depth_limit_hit": depth_limit_hit,
            },
        )

    def _resolve_influences(self, graph: TraceGraph, judgment: NodeJudgment, current_ref: str) -> List[str]:
        current = graph.nodes.get(current_ref)
        if current and is_evaluation_assertion(current):
            return dedupe(graph.upstream_refs(current_ref))
        explicit = []
        for influence in judgment.influenced_by:
            if influence.relation != "defect_propagated_from":
                continue
            resolved = graph.resolve(influence.upstream_ref) or influence.upstream_ref
            if resolved != current_ref:
                explicit.append(resolved)
        if explicit:
            return dedupe(explicit)
        if judgment.is_root_cause:
            return []
        return []


def collapse_episode_roots(
    *,
    graph: TraceGraph,
    episode_index: CausalEpisodeIndex,
    roots: Dict[str, RootCauseCandidate],
    root_paths: Dict[str, List[str]],
    judgments: Dict[str, NodeJudgment],
    observed_defect_ref: str,
) -> Tuple[List[RootCauseCandidate], List[List[str]]]:
    refs_by_episode: Dict[str, List[str]] = {}
    for ref in roots:
        episode = episode_index.episode_for(ref)
        refs_by_episode.setdefault(episode.episode_id, []).append(ref)

    candidates: List[RootCauseCandidate] = []
    paths: List[List[str]] = []
    for episode_id, candidate_refs in refs_by_episode.items():
        representative = episode_index.representative(candidate_refs, judgments)
        if not representative:
            continue
        episode = episode_index.episode_for(representative)
        candidates.append(
            replace(
                roots[representative],
                episode_id=episode_id,
                episode_member_refs=episode.member_refs,
                observed_defect_refs=[observed_defect_ref],
            )
        )
        paths.append(root_paths.get(representative, [observed_defect_ref, representative]))

    candidates.sort(key=lambda item: graph.position(item.node_ref))
    path_by_root = {path[-1]: path for path in paths if path}
    ordered_paths = [path_by_root[item.node_ref] for item in candidates if item.node_ref in path_by_root]
    return candidates, dedupe_paths(ordered_paths)


def aggregate_root_causes(branches: List[DefectBranchResult], graph: TraceGraph) -> List[RootCauseCandidate]:
    aggregated: Dict[Tuple[str, str], RootCauseCandidate] = {}
    for branch in branches:
        for candidate in branch.root_causes:
            key = (candidate.node_ref, candidate.defect_type)
            existing = aggregated.get(key)
            if not existing:
                aggregated[key] = candidate
                continue
            aggregated[key] = replace(
                existing,
                observed_defect_refs=dedupe(
                    existing.observed_defect_refs + candidate.observed_defect_refs
                ),
                confidence=max(existing.confidence, candidate.confidence),
            )
    return sorted(aggregated.values(), key=lambda item: graph.position(item.node_ref))


def aggregate_analysis_outcome(branch_outcomes: List[str]) -> str:
    if not branch_outcomes:
        return "inconclusive"
    if all(outcome == "root_found" for outcome in branch_outcomes):
        return "root_found"
    if any(outcome == "root_found" for outcome in branch_outcomes):
        return "partial_root_found"
    if all(outcome == "no_defect" for outcome in branch_outcomes):
        return "no_defect"
    return "inconclusive"


def aggregate_termination_reason(branches: List[DefectBranchResult]) -> str:
    reasons = {str(branch.metadata.get("termination_reason") or "") for branch in branches}
    if "node_limit" in reasons:
        return "node_limit"
    if "depth_limit" in reasons:
        return "depth_limit"
    return "queue_exhausted"


def start_defect_type(graph: TraceGraph, start_ref: str) -> str:
    node = graph.nodes.get(start_ref)
    if not node:
        return "analysis_start"
    for key in ("failure_type", "gap_kind", "dimension", "issue_kind"):
        value = node.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "analysis_start"


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


def is_evaluation_assertion(node: TraceNode) -> bool:
    return node.event_type in ("case.observed_defect", "case.quality_gap", "case.missing_semantic")


def is_observability_gap(node: TraceNode) -> bool:
    if node.event_type == "case.missing_semantic":
        return True
    if node.component != "trace":
        return False
    issue_kind = str(node.data.get("issue_kind") or "")
    failure_type = str(node.data.get("failure_type") or "")
    return issue_kind.startswith("missing_") or failure_type.endswith("_missing")


def semantic_downstream_context(graph: TraceGraph, path: List[str]) -> List[str]:
    output: List[str] = []
    keys = (
        "failure_type",
        "gap_kind",
        "dimension",
        "description",
        "text",
        "status",
        "defect_type",
        "reason",
    )
    for ref in path:
        node = graph.nodes.get(ref)
        if not node:
            output.append(ref)
            continue
        semantics = [f"{key}={node.data[key]}" for key in keys if node.data.get(key) not in (None, "", [], {})]
        suffix = "; ".join(semantics)
        output.append(f"{ref} event_type={node.event_type}" + (f"; {suffix}" if suffix else ""))
    return output


def fallback_judgment_after_error(
    *,
    node: TraceNode,
    upstream_nodes: List[TraceNode],
    error: Exception,
) -> NodeJudgment:
    error_text = f"{type(error).__name__}: {error}"
    reason = "The attribution judge failed before this node could be classified."
    if node.event_type == "tool.error":
        reason = tool_error_fallback_reason(node)
    elif node.event_type == "context.compaction":
        reason = context_compaction_fallback_reason(node)
    elif node.event_type in ("evidence.semantic_fact", "evidence.fact", "execution.observation"):
        reason = semantic_fact_fallback_reason(node)
    elif node.event_type in ("response.claim", "response.output"):
        reason = response_surface_fallback_reason(node)
    elif node.event_type == "task.obligation":
        reason = task_obligation_fallback_reason(node)
    elif is_evaluation_assertion(node):
        reason = "The attribution judge failed before the offline evaluation assertion could be validated."
    return NodeJudgment(
        node_ref=node.ref,
        component=node.component,
        event_type=node.event_type,
        has_defect=False,
        defect_status="unknown",
        defect_type="judge_error",
        defect_reason=reason,
        influenced_by=[],
        is_root_cause=False,
        severity="unknown",
        confidence=0.0,
        model_notes=error_text,
    )


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


def context_compaction_fallback_reason(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    parts = ["Context compaction boundary observed"]
    for key in ("trigger", "provider_id", "model_id", "selected_head_messages", "selected_tail_messages"):
        if key in data:
            parts.append(f"{key}={data[key]}")
    summary = short_text(data.get("output_summary") or data.get("summary") or "")
    if summary:
        parts.append(f"output_summary={summary}")
    return ". ".join(parts) + "."


def semantic_fact_fallback_reason(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    parts = ["Semantic evidence boundary observed"]
    for key in ("canonical_subject", "semantic_role", "fact_scope", "applicability_status", "conflict_group_id"):
        if key in data:
            parts.append(f"{key}={data[key]}")
    structured = data.get("structured_claim")
    if isinstance(structured, dict):
        subject = structured.get("subject")
        predicate = structured.get("predicate")
        value = structured.get("value")
        parts.append(f"structured_claim={subject}.{predicate}={value}")
    summary = short_text(data.get("summary") or data.get("claim") or "")
    if summary:
        parts.append(f"summary={summary}")
    return ". ".join(parts) + "."


def response_surface_fallback_reason(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    parts = ["Answer surface boundary observed"]
    text = short_text(data.get("text") or "")
    if text:
        parts.append(f"text={text}")
    direct_refs = data.get("direct_evidence_refs")
    if isinstance(direct_refs, list):
        parts.append(f"direct_evidence_refs={len(direct_refs)}")
    context_refs = data.get("context_refs")
    if isinstance(context_refs, list):
        parts.append(f"context_refs={len(context_refs)}")
    quality_flags = data.get("quality_flags")
    if isinstance(quality_flags, list) and quality_flags:
        parts.append(f"quality_flags={','.join(str(item) for item in quality_flags[:8])}")
    return ". ".join(parts) + "."


def task_obligation_fallback_reason(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, dict) else {}
    parts = ["Task obligation boundary observed"]
    for key in ("obligation_type", "status", "target_path"):
        if key in data:
            parts.append(f"{key}={data[key]}")
    requirement_text = short_text(data.get("requirement_text") or "")
    if requirement_text:
        parts.append(f"requirement_text={requirement_text}")
    quality_flags = data.get("quality_flags")
    if isinstance(quality_flags, list) and quality_flags:
        parts.append(f"quality_flags={','.join(str(item) for item in quality_flags[:8])}")
    return ". ".join(parts) + "."


def short_text(value: object, limit: int = 240) -> str:
    text = str(value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
