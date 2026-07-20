from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from .causal_state import RecursiveAttributionReport
from .models import AttributionReport, JsonDict, RootCauseCandidate, TraceNode


def build_recursive_trace_improvement_report(
    graph: Any, report: RecursiveAttributionReport
) -> JsonDict:
    """Derive recursive-attribution observability gaps without model calls."""
    blocking_gaps: List[JsonDict] = []
    recommendations: List[JsonDict] = []

    def add_recursive_gap(
        gap_type: str,
        *,
        node_ref: str,
        why: str,
        missing_fields: Iterable[str],
        related_refs: Iterable[str],
    ) -> None:
        node = graph.nodes.get(node_ref) if node_ref else None
        blocking_gaps.append(
            {
                "gap_type": gap_type,
                "node_ref": node_ref,
                "component": node.component if node is not None else "attribution",
                "event_type": node.event_type if node is not None else "recursive_analysis",
                "why_it_blocks_root_cause_analysis": why,
                "missing_semantic_fields": list(missing_fields),
                "related_refs": list(dict.fromkeys(str(ref) for ref in related_refs if ref)),
                "confidence": 1.0,
            }
        )
        recommendations.append(
            {
                "component": node.component if node is not None else "attribution",
                "priority": "high",
                "change": "Record {0} for {1}.".format(
                    ", ".join(missing_fields), node_ref or "the recursive analysis"
                ),
                "unblocks": [gap_type],
            }
        )

    paths = [tuple(path) for path in report.taint_paths]
    paths.extend(tuple(item.recursive_path) for item in report.confirmations)
    for path in dict.fromkeys(paths):
        for upstream, downstream in zip(path, path[1:]):
            edge_context = graph.edge_context(upstream, downstream)
            eligible = any(
                bool(edge.get("eligible_for_attribution", True))
                and str(edge.get("relation") or "") not in {
                    "temporal_proximity",
                    "temporal_sequence",
                }
                for edge in edge_context
            )
            if not eligible:
                add_recursive_gap(
                    "recursive_path_missing_edge",
                    node_ref=upstream,
                    why="A recursive path hop has no grounded non-temporal causal edge.",
                    missing_fields=("causal_edge", "edge_provenance", "edge_reason"),
                    related_refs=(upstream, downstream),
                )

    for relation in report.causal_relations:
        if relation.relation != "defect_transformation":
            continue
        transformed = relation.upstream_defect
        if transformed is None or not transformed.transformation_reason:
            add_recursive_gap(
                "defect_transformation_missing_intermediate",
                node_ref=relation.ref,
                why="The transformation does not preserve the intermediate defect mechanism.",
                missing_fields=(
                    "upstream_defect_state",
                    "transformation_reason",
                    "intermediate_node_ref",
                ),
                related_refs=(relation.ref,),
            )

    unresolved = [
        item
        for item in report.unresolved_hypotheses
        if item.status in {"active", "supported", "unresolved"}
    ]
    if unresolved:
        add_recursive_gap(
            "competing_hypotheses_unresolved",
            node_ref=unresolved[0].candidate_root_ref,
            why="One or more grounded competing explanations remain unresolved.",
            missing_fields=("independent_comparison", "falsification_result"),
            related_refs=(item.candidate_root_ref for item in unresolved),
        )

    confirmed_refs = {
        item.node_ref for item in (*report.confirmed_roots, *report.co_roots)
    }
    for confirmation in report.confirmations:
        if confirmation.status == "unknown":
            add_recursive_gap(
                "root_confirmation_missing_evidence",
                node_ref=confirmation.candidate_ref,
                why=confirmation.reason or "Independent confirmation lacked decisive evidence.",
                missing_fields=(
                    "candidate_local_excerpt",
                    "grounded_counterfactual",
                    "resolved_evidence_refs",
                ),
                related_refs=confirmation.recursive_path,
            )
    confirmed_or_attempted = confirmed_refs | {
        item.candidate_ref for item in report.confirmations
    }
    for candidate in report.introduction_candidates:
        if candidate.ref not in confirmed_or_attempted:
            add_recursive_gap(
                "root_confirmation_missing_evidence",
                node_ref=candidate.ref,
                why="The introduction candidate was not independently confirmed.",
                missing_fields=("root_confirmation_request", "independent_verdict"),
                related_refs=(candidate.ref,),
            )

    bindings = report.metadata.get("introduction_bindings") or ()
    identities: Dict[str, Set[tuple[str, str]]] = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        hypothesis_id = str(binding.get("hypothesis_id") or "")
        identities.setdefault(hypothesis_id, set()).add(
            (
                str(binding.get("candidate_ref") or ""),
                str(binding.get("defect_fingerprint") or ""),
            )
        )
    for hypothesis_id, values in identities.items():
        if hypothesis_id and len(values) > 1:
            refs = sorted(ref for ref, _ in values if ref)
            add_recursive_gap(
                "semantic_anchor_unstable",
                node_ref=refs[0] if refs else "",
                why="One hypothesis identity maps to multiple candidate or defect anchors.",
                missing_fields=(
                    "stable_hypothesis_id",
                    "candidate_ref",
                    "defect_fingerprint",
                ),
                related_refs=refs,
            )

    blocking_gaps = dedupe_gaps(blocking_gaps)
    recommendations = dedupe_recommendations(recommendations)
    return {
        "summary": {
            "blocking_gap_count": len(blocking_gaps),
            "advisory_gap_count": 0,
            "recommended_change_count": len(recommendations),
            "analysis_confidence": (
                "blocked"
                if blocking_gaps and not confirmed_refs
                else "partial"
                if blocking_gaps
                else "high"
            ),
        },
        "blocking_gaps": blocking_gaps,
        "advisory_gaps": [],
        "recommended_trace_changes": recommendations,
    }


def build_trace_improvement_report(graph: Any, report: AttributionReport) -> JsonDict:
    blocking_gaps: List[JsonDict] = []
    advisory_gaps: List[JsonDict] = []
    recommendations: List[JsonDict] = []

    if not report.start_refs:
        blocking_gaps.append(
            {
                "gap_type": "no_analysis_start",
                "node_ref": "",
                "component": "trace_graph",
                "event_type": "analysis_start",
                "why_it_blocks_root_cause_analysis": (
                    "The trace contains no final response, explicit observed defect, quality gap, "
                    "missing-semantic assertion, failed case, or response claim that can start backward analysis."
                ),
                "missing_semantic_fields": ["analysis_start_node"],
                "related_refs": [],
                "confidence": 1.0,
            }
        )
        add_recommendation(
            recommendations,
            component="trace_graph",
            priority="high",
            change="Record a final response or explicit offline evaluation assertion before running attribution.",
            unblocks=["no_analysis_start"],
        )

    incomplete_branches = [
        branch
        for branch in report.defect_branches
        if branch.analysis_outcome not in ("root_found", "no_defect")
    ]
    for branch in incomplete_branches:
        gap_target = (
            advisory_gaps
            if branch.metadata.get("attribution_domain") == "trace_health"
            and report.metadata.get("primary_attribution_domain") == "task_quality"
            else blocking_gaps
        )
        gap_target.append(
            {
                "gap_type": "unresolved_defect_branch",
                "node_ref": branch.start_ref,
                "component": "attribution",
                "event_type": "defect_branch",
                "why_it_blocks_root_cause_analysis": (
                    f"The observed defect branch {branch.defect_type} did not reach a confirmed "
                    "defect-introduction episode. Other successful branches do not cover this defect."
                ),
                "missing_semantic_fields": ["branch_root_cause_episode"],
                "related_refs": dedupe(branch.visited_order + branch.unresolved_refs),
                "confidence": 1.0,
                "attribution_domain": branch.metadata.get("attribution_domain", "task_quality"),
            }
        )
    if any(
        branch.metadata.get("attribution_domain") != "trace_health"
        or report.metadata.get("primary_attribution_domain") != "task_quality"
        for branch in incomplete_branches
    ):
        add_recommendation(
            recommendations,
            component="attribution",
            priority="high",
            change=(
                "Review each unresolved defect branch independently and add the missing causal identity, "
                "semantic predecessor, or judge evidence needed to reach its introduction episode."
            ),
            unblocks=["unresolved_defect_branch"],
        )

    if report.metadata.get("primary_attribution_domain") == "task_quality":
        for branch in report.defect_branches:
            if branch.metadata.get("attribution_domain") != "trace_health":
                continue
            judge_errors = branch.metadata.get("judge_errors") or []
            if judge_errors:
                advisory_gaps.append(
                    {
                        "gap_type": "judge_error",
                        "node_ref": branch.start_ref,
                        "component": "attribution",
                        "event_type": "node_judgment",
                        "why_it_blocks_root_cause_analysis": (
                            "A trace-health node could not be classified by the offline judge. "
                            "The task-quality root remains valid, but observability diagnostics are incomplete."
                        ),
                        "missing_semantic_fields": ["completed_node_judgment"],
                        "related_refs": [
                            str(item.get("node_ref") or "")
                            for item in judge_errors
                            if isinstance(item, dict) and item.get("node_ref")
                        ],
                        "confidence": 1.0,
                        "attribution_domain": "trace_health",
                    }
                )
            termination_reason = branch.metadata.get("termination_reason")
            if termination_reason in ("depth_limit", "node_limit"):
                advisory_gaps.append(
                    {
                        "gap_type": "analysis_search_limit",
                        "node_ref": branch.start_ref,
                        "component": "attribution",
                        "event_type": "analysis_boundary",
                        "why_it_blocks_root_cause_analysis": (
                            f"The trace-health branch stopped at the configured {termination_reason}. "
                            "This does not invalidate the independently found task-quality root."
                        ),
                        "missing_semantic_fields": [],
                        "related_refs": branch.visited_order,
                        "confidence": 1.0,
                        "attribution_domain": "trace_health",
                    }
                )
            if branch.unresolved_refs:
                advisory_gaps.append(
                    {
                        "gap_type": "unresolved_trace_refs",
                        "node_ref": branch.start_ref,
                        "component": "trace_graph",
                        "event_type": "reference_resolution",
                        "why_it_blocks_root_cause_analysis": (
                            "The trace-health branch cites refs that do not resolve. "
                            "They limit observability review but do not replace the task-quality outcome."
                        ),
                        "missing_semantic_fields": [],
                        "related_refs": sorted(set(branch.unresolved_refs)),
                        "confidence": 1.0,
                        "attribution_domain": "trace_health",
                    }
                )

    for root in report.root_causes:
        node = graph.nodes.get(root.node_ref)
        if not node:
            continue
        if root.confidence < 0.7:
            add_gap(
                blocking_gaps,
                gap_type="low_confidence_root_cause",
                node=node,
                why=(
                    "The analyzer reached a root-cause candidate with low confidence. "
                    "This usually means the trace has enough facts to locate a suspect layer, "
                    "but not enough semantic evidence to prove defect introduction."
                ),
                related_refs=[root.node_ref],
                confidence=root.confidence,
            )
            add_recommendation(
                recommendations,
                component=node.component or "trace",
                priority="medium",
                change="Record the semantic input, output, and decision rationale around this root-cause candidate.",
                unblocks=["low_confidence_root_cause"],
            )
        if node.event_type == "llm.call" or node.component == "llm":
            missing = missing_llm_generation_fields(node)
            if missing:
                add_gap(
                    blocking_gaps,
                    gap_type="llm_call_missing_generation_semantics",
                    node=node,
                    why=(
                        "Backward taint stopped at an LLM call, but the node does not contain enough "
                        "message construction, selected context, or generated output semantics to continue "
                        "attributing the defect to prompt construction, context selection, compaction, "
                        "planner intent, or model generation."
                    ),
                    missing_semantic_fields=missing,
                    related_refs=path_refs_containing(report.taint_paths, root.node_ref),
                    confidence=root.confidence,
                )
                add_recommendation(
                    recommendations,
                    component="llm",
                    priority="high",
                    change=(
                        "For each llm.call, record normalized input messages, message transform stages, "
                        "selected context refs, compaction provenance, assistant output text/claims, and "
                        "token usage as trace artifacts."
                    ),
                    unblocks=["llm_call_missing_generation_semantics"],
                )
        if node.event_type in ("response.claim", "response.output"):
            add_gap(
                blocking_gaps,
                gap_type="answer_surface_root_cause",
                node=node,
                why=(
                    "The analyzer could only identify the final answer surface as the defect introduction point. "
                    "This is useful for detecting answer quality issues, but not enough to distinguish whether "
                    "the defect came from planning, context, tool execution, or LLM generation."
                ),
                related_refs=path_refs_containing(report.taint_paths, root.node_ref),
                confidence=root.confidence,
            )
            add_recommendation(
                recommendations,
                component="result",
                priority="high",
                change=(
                    "Link every response.output and response.claim to the generating llm.call, tool result, "
                    "context refs, and claim-support assessment used to produce it."
                ),
                unblocks=["answer_surface_root_cause"],
            )

    if report.metadata.get("judge_error_count", 0):
        judge_error_refs = [
            str(item.get("node_ref") or "")
            for item in report.metadata.get("judge_errors", [])
            if isinstance(item, dict) and item.get("node_ref")
        ]
        blocking_gaps.append(
            {
                "gap_type": "judge_error",
                "node_ref": "",
                "component": "attribution",
                "event_type": "node_judgment",
                "why_it_blocks_root_cause_analysis": (
                    "One or more nodes could not be classified by the offline judge. "
                    "They remain unknown and are never promoted to root causes."
                ),
                "missing_semantic_fields": ["completed_node_judgment"],
                "related_refs": judge_error_refs,
                "confidence": 1.0,
            }
        )
        add_recommendation(
            recommendations,
            component="attribution",
            priority="high",
            change="Retry unknown node judgments with a complete schema, bounded prompt, and sufficient request timeout.",
            unblocks=["judge_error"],
        )

    unknown_branch_outcomes: Dict[str, List[tuple[str, str]]] = {}
    for branch in report.defect_branches:
        for ref, judgment in branch.node_judgments.items():
            if judgment.defect_status == "unknown":
                unknown_branch_outcomes.setdefault(ref, []).append(
                    (
                        branch.analysis_outcome,
                        str(branch.metadata.get("attribution_domain") or "task_quality"),
                    )
                )

    for ref, judgment in report.node_judgments.items():
        if judgment.defect_status != "unknown":
            continue
        node = graph.nodes.get(ref)
        if not node:
            continue
        serialized = str(node.data)
        has_artifact_reference = "artifact_id" in serialized or "payload_ref" in serialized
        has_hydrated_artifact = bool(node.data.get("hydrated_artifacts"))
        gap_component = "attribution_hydration" if has_artifact_reference and not has_hydrated_artifact else node.component
        missing_fields = (
            ["hydrated_artifact_content"]
            if gap_component == "attribution_hydration"
            else ["decisive_semantic_evidence"]
        )
        detail = judgment.model_notes or judgment.defect_reason or "The judge could not classify this node."
        outcomes = unknown_branch_outcomes.get(ref) or []
        all_non_blocking = bool(outcomes) and all(
            outcome == "root_found"
            or (
                domain == "trace_health"
                and report.metadata.get("primary_attribution_domain") == "task_quality"
            )
            for outcome, domain in outcomes
        )
        gap_target = (
            advisory_gaps
            if all_non_blocking
            else blocking_gaps
        )
        add_gap(
            gap_target,
            gap_type="unknown_node_judgment",
            node=node,
            why=(
                "Backward analysis could not classify this node as defective or non-defective. "
                + (
                    "The active defect branch still reached a confirmed root episode, so this is retained "
                    "as an advisory completeness gap. "
                    if gap_target is advisory_gaps
                    else "This remains a blocking gap for at least one active defect branch. "
                )
                + f"Detail: {detail}"
            ),
            missing_semantic_fields=missing_fields,
            related_refs=[ref],
            confidence=1.0,
        )
        gap_target[-1]["component"] = gap_component
        add_recommendation(
            recommendations,
            component=gap_component or "trace",
            priority="medium" if gap_target is advisory_gaps else "high",
            change=(
                "Hydrate the cited trace artifact before judging this node."
                if gap_component == "attribution_hydration"
                else "Record or expose the decisive semantic evidence needed to classify this node."
            ),
            unblocks=["unknown_node_judgment"],
        )

    termination_reason = report.metadata.get("termination_reason")
    if termination_reason in ("depth_limit", "node_limit"):
        blocking_gaps.append(
            {
                "gap_type": "analysis_search_limit",
                "node_ref": "",
                "component": "attribution",
                "event_type": "analysis_boundary",
                "why_it_blocks_root_cause_analysis": (
                    f"Backward traversal stopped at the configured {termination_reason}; "
                    "unvisited upstream nodes may still contain the defect-introduction point."
                ),
                "missing_semantic_fields": [],
                "related_refs": report.visited_order,
                "confidence": 1.0,
            }
        )
        add_recommendation(
            recommendations,
            component="attribution",
            priority="high",
            change="Increase traversal bounds or narrow the start set while preserving every causal predecessor.",
            unblocks=["analysis_search_limit"],
        )

    if report.unresolved_refs:
        blocking_gaps.append(
            {
                "gap_type": "unresolved_trace_refs",
                "node_ref": "",
                "component": "trace_graph",
                "event_type": "reference_resolution",
                "why_it_blocks_root_cause_analysis": (
                    "Some refs cited by judgments or provenance edges do not resolve to trace nodes, "
                    "so the analyzer cannot continue backward through those causal links."
                ),
                "missing_semantic_fields": [],
                "related_refs": sorted(set(report.unresolved_refs)),
                "confidence": 1.0,
            }
        )
        add_recommendation(
            recommendations,
            component="trace_graph",
            priority="high",
            change="Emit stable aliases for every source_ref, dataflow endpoint, tool call, evidence fact, and response claim.",
            unblocks=["unresolved_trace_refs"],
        )

    contradictory_edges = defective_nodes_pointing_to_nondefective_upstream(report)
    if contradictory_edges:
        blocking_gaps.append(
            {
                "gap_type": "defective_node_points_to_nondefective_upstream",
                "node_ref": "",
                "component": "trace_provenance",
                "event_type": "semantic_edge",
                "why_it_blocks_root_cause_analysis": (
                    "A defective node claimed an upstream cause, but that upstream node was judged non-defective. "
                    "This often indicates overly broad provenance edges, missing intermediate semantic nodes, "
                    "or insufficient context for judging the upstream node."
                ),
                "missing_semantic_fields": ["edge_reason", "intermediate_semantic_node", "upstream_usage_context"],
                "related_refs": contradictory_edges,
                "confidence": 0.8,
            }
        )
        add_recommendation(
            recommendations,
            component="trace_provenance",
            priority="high",
            change=(
                "For each source_ref/dataflow edge, record why the upstream fact was used, which downstream "
                "decision or claim consumed it, and whether the edge is direct evidence, contextual support, or legacy noise."
            ),
            unblocks=["defective_node_points_to_nondefective_upstream"],
        )

    if not report.root_causes and any(judgment.has_defect for judgment in report.node_judgments.values()):
        blocking_gaps.append(
            {
                "gap_type": "taint_path_stopped_without_root",
                "node_ref": "",
                "component": "attribution",
                "event_type": "analysis_boundary",
                "why_it_blocks_root_cause_analysis": (
                    "The analyzer found defective semantics but no root-cause candidate. "
                    "The trace or judgments do not provide a usable backward path to a defect-introduction node."
                ),
                "missing_semantic_fields": ["causal_upstream_ref", "defect_introduction_boundary"],
                "related_refs": report.visited_order,
                "confidence": 0.9,
            }
        )
        add_recommendation(
            recommendations,
            component="attribution",
            priority="high",
            change=(
                "When a defective node has no defective upstream, preserve that node as a boundary candidate and "
                "record which missing trace facts prevent deeper attribution."
            ),
            unblocks=["taint_path_stopped_without_root"],
        )

    blocking_gaps = dedupe_gaps(blocking_gaps)
    advisory_gaps = dedupe_gaps(advisory_gaps)
    recommendations = dedupe_recommendations(recommendations)
    return {
        "summary": {
            "blocking_gap_count": len(blocking_gaps),
            "advisory_gap_count": len(advisory_gaps),
            "recommended_change_count": len(recommendations),
            "analysis_confidence": analysis_confidence(blocking_gaps, report),
        },
        "blocking_gaps": blocking_gaps,
        "advisory_gaps": advisory_gaps,
        "recommended_trace_changes": recommendations,
    }


def missing_llm_generation_fields(node: TraceNode) -> List[str]:
    data = node.data if isinstance(node.data, dict) else {}
    groups = [
        ("input_messages", ["input_messages", "messages", "request_messages", "messages_preview"]),
        ("message_transforms", ["message_transforms", "message_pipeline", "prompt_layers", "message_versions"]),
        ("selected_context_refs", ["selected_context_refs", "context_refs", "context_snapshot", "context_items"]),
        ("compaction_provenance", ["compaction_refs", "compaction_provenance", "compression_algorithm", "context_compression"]),
        ("output_text", ["output_text", "response_text", "assistant_message", "semantic_output"]),
        ("token_usage", ["token_usage", "tokens", "input_tokens", "output_tokens"]),
    ]
    missing = []
    for canonical, aliases in groups:
        if not any(has_meaningful_value(data.get(alias)) for alias in aliases):
            missing.append(canonical)
    return missing


def has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def defective_nodes_pointing_to_nondefective_upstream(report: AttributionReport) -> List[str]:
    refs = []
    branch_judgments = [branch.node_judgments for branch in report.defect_branches]
    judgment_sets = branch_judgments or [report.node_judgments]
    for judgments in judgment_sets:
        for ref, judgment in judgments.items():
            if judgment.defect_status != "present" or judgment.causal_role != "defect_propagation":
                continue
            for influence in judgment.influenced_by:
                if influence.relation != "defect_propagated_from":
                    continue
                upstream = judgments.get(influence.upstream_ref)
                if upstream and upstream.defect_status == "absent":
                    refs.append(f"{ref}->{influence.upstream_ref}")
    return dedupe(refs)


def path_refs_containing(paths: Iterable[List[str]], ref: str) -> List[str]:
    refs: List[str] = []
    for path in paths:
        if ref in path:
            refs.extend(path)
    return dedupe(refs)


def add_gap(
    gaps: List[JsonDict],
    *,
    gap_type: str,
    node: TraceNode,
    why: str,
    missing_semantic_fields: List[str] = None,
    related_refs: List[str] = None,
    confidence: float = 0.0,
) -> None:
    gaps.append(
        {
            "gap_type": gap_type,
            "node_ref": node.ref,
            "component": node.component,
            "event_type": node.event_type,
            "why_it_blocks_root_cause_analysis": why,
            "missing_semantic_fields": list(missing_semantic_fields or []),
            "related_refs": list(related_refs or []),
            "confidence": confidence,
        }
    )


def add_recommendation(
    recommendations: List[JsonDict],
    *,
    component: str,
    priority: str,
    change: str,
    unblocks: List[str],
) -> None:
    recommendations.append(
        {
            "component": component,
            "priority": priority,
            "change": change,
            "unblocks": list(unblocks),
        }
    )


def analysis_confidence(blocking_gaps: List[JsonDict], report: AttributionReport) -> str:
    outcome = report.metadata.get("analysis_outcome")
    if outcome == "no_defect":
        return "no_defect"
    if outcome == "partial_root_found":
        return "partial"
    if outcome == "inconclusive":
        return "blocked"
    if not report.root_causes:
        return "blocked"
    limited_gap_types = {
        "answer_surface_root_cause",
        "defective_node_points_to_nondefective_upstream",
        "llm_call_missing_generation_semantics",
        "unresolved_trace_refs",
    }
    if any(gap.get("gap_type") in limited_gap_types for gap in blocking_gaps):
        return "limited"
    if any(gap.get("gap_type") == "low_confidence_root_cause" for gap in blocking_gaps):
        return "medium"
    return "high"


def dedupe(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    output: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def dedupe_gaps(gaps: List[JsonDict]) -> List[JsonDict]:
    seen: Set[str] = set()
    output: List[JsonDict] = []
    for gap in gaps:
        key = f"{gap.get('gap_type')}|{gap.get('node_ref')}|{','.join(gap.get('related_refs') or [])}"
        if key in seen:
            continue
        seen.add(key)
        output.append(gap)
    return output


def dedupe_recommendations(recommendations: List[JsonDict]) -> List[JsonDict]:
    seen: Set[str] = set()
    output: List[JsonDict] = []
    for recommendation in recommendations:
        key = f"{recommendation.get('component')}|{recommendation.get('change')}"
        if key in seen:
            continue
        seen.add(key)
        output.append(recommendation)
    return output
