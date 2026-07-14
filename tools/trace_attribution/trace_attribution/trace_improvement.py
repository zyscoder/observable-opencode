from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set

from .models import AttributionReport, JsonDict, RootCauseCandidate, TraceNode


def build_trace_improvement_report(graph: Any, report: AttributionReport) -> JsonDict:
    blocking_gaps: List[JsonDict] = []
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
        blocking_gaps.append(
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
            }
        )
    if incomplete_branches:
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
        add_gap(
            blocking_gaps,
            gap_type="unknown_node_judgment",
            node=node,
            why=(
                "Backward analysis could not classify this node as defective or non-defective. "
                f"Blocking detail: {detail}"
            ),
            missing_semantic_fields=missing_fields,
            related_refs=[ref],
            confidence=1.0,
        )
        blocking_gaps[-1]["component"] = gap_component
        add_recommendation(
            recommendations,
            component=gap_component or "trace",
            priority="high",
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
    recommendations = dedupe_recommendations(recommendations)
    return {
        "summary": {
            "blocking_gap_count": len(blocking_gaps),
            "recommended_change_count": len(recommendations),
            "analysis_confidence": analysis_confidence(blocking_gaps, report),
        },
        "blocking_gaps": blocking_gaps,
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
    for ref, judgment in report.node_judgments.items():
        if not judgment.has_defect:
            continue
        for influence in judgment.influenced_by:
            upstream = report.node_judgments.get(influence.upstream_ref)
            if upstream and not upstream.has_defect:
                refs.append(f"{ref}->{influence.upstream_ref}")
    return refs


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
