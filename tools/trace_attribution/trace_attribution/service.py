"""Shared offline attribution orchestration for CLI and Python callers."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import signal
import tempfile
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .analyzer import BackwardTaintAnalyzer
from .artifact_reader import VerifiedArtifactReader
from .benchmark_case import load_benchmark_case
from .cache import JudgmentCache
from .causal_judge import ClaudeCausalJudge
from .causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    annotate_report_semantic_anchors,
)
from .checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
    publish_output_transaction,
)
from .claude import ClaudeJudgeClient, TransportCallError, parse_json_object
from .defect_explanation import (
    DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION,
    build_defect_evolution,
    explanation_output_path,
    publish_defect_explanation,
    render_defect_explanation_markdown,
    synthesize_defect_evolution,
)
from .errors import AttributionInputError
from .expected_action import build_expected_action_projection
from .graph import TraceGraph, artifact_root_for_trace_path
from .models import stable_json
from .recursive_analyzer import AgenticRecursiveAnalyzer
from .request import (
    QUESTION_SCHEMA_VERSION,
    AttributionQuestion,
    AttributionRequest,
    AttributionResult,
)


class GracefulSignalState:
    """Signal-safe stop flag shared by SIGINT and SIGTERM."""

    def __init__(self) -> None:
        self._requested = False
        self.signal_name = ""
        self._previous: dict[int, Any] = {}

    def handle(self, signum: int, _frame: Any) -> None:
        self._requested = True
        try:
            self.signal_name = signal.Signals(signum).name
        except ValueError:
            self.signal_name = str(signum)

    def stop_requested(self) -> bool:
        return self._requested

    def __enter__(self) -> "GracefulSignalState":
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self.handle)
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()


def analyze(request: AttributionRequest) -> AttributionResult:
    """Run offline attribution and publish its report and lineage outputs."""
    if not isinstance(request, AttributionRequest):
        raise TypeError("request must be an AttributionRequest")

    paths = validate_request_path_isolation(request)
    graph = load_graph(
        request.trace_path,
        request.review_path,
        request.evaluation_paths,
        bundle_path=request.benchmark_bundle_path,
    )
    if request.question_binding is not None and not request.start_refs:
        graph = bind_question_hypothesis(graph, request.question_binding)
    starts = question_analysis_start_refs(
        graph,
        request.start_refs,
        question=request.normalized_question,
    )
    options = request.options
    objective = request.effective_objective
    out = request.output_path
    cache_path = paths["judge_cache"]
    transport = ClaudeJudgeClient(
        model=options.model,
        api_key_env=options.api_key_env,
        base_url=options.base_url,
        base_url_env=options.base_url_env,
        max_tokens=options.judge_max_tokens,
        context_window_tokens=options.judge_context_window_tokens,
        context_safety_margin_tokens=(
            options.judge_context_safety_margin_tokens
        ),
        timeout_seconds=options.judge_timeout_sec,
        thinking_mode=options.thinking_mode,
        cache_path=str(cache_path),
        provider_error_threshold=options.provider_error_threshold,
    )
    lineage_out = paths["lineage"]

    if (
        request.question_binding is not None
        and not request.start_refs
        and options.engine == "recursive-agentic"
    ):
        premise_assessment, premise_physical_requests = (
            _load_or_assess_question_premise(
                transport=transport,
                graph=graph,
                binding=request.question_binding,
                seed_ref=starts[0],
                checkpoint_path=paths["checkpoint"],
                maximum_requests=options.max_judge_requests,
            )
        )
        graph = bind_question_premise_assessment(graph, premise_assessment)
        starts = question_analysis_start_refs(
            graph,
            request.start_refs,
            question=request.normalized_question,
        )
        expected_action_projection = graph.raw_trace.get(
            "offline_question_projection", {}
        )
        expected_action_projection = (
            expected_action_projection.get("expected_action_projection")
            if isinstance(expected_action_projection, Mapping)
            else None
        )
        if premise_assessment["status"] == "contradicted" or (
            premise_assessment["status"] == "unknown"
            and isinstance(expected_action_projection, Mapping)
        ):
            terminal_report = (
                question_premise_no_defect_report(
                    case_id=graph.case_id,
                    objective=objective,
                    seed_ref=starts[0],
                    assessment=premise_assessment,
                )
                if premise_assessment["status"] == "contradicted"
                else question_premise_inconclusive_report(
                    case_id=graph.case_id,
                    objective=objective,
                    seed_ref=starts[0],
                    assessment=premise_assessment,
                )
            )
            report_payload = question_bound_output_payload(
                terminal_report,
                graph,
                binding=request.question_binding,
                starts=starts,
            )
            report_payload = enrich_attribution_explanation(
                report_payload,
                graph,
                transport=(
                    transport
                    if premise_assessment["status"] == "contradicted"
                    else None
                )
                if (
                    premise_assessment["status"] == "contradicted"
                    and _remaining_judge_requests(
                        transport, options.max_judge_requests
                    )
                    > 0
                )
                else None,
            )
            report_payload = _annotate_shared_judge_budget(
                report_payload,
                maximum=options.max_judge_requests,
                premise_physical_requests=premise_physical_requests,
                analyzer_physical_requests=0,
                explanation_physical_requests=_explanation_physical_requests(
                    report_payload
                ),
            )
            atomic_write_json(out, report_payload)
            atomic_write_json(lineage_out, graph.message_lineage)
            publish_defect_explanation(
                out,
                report_payload,
                report_payload["defect_evolution"],
            )
            return AttributionResult(
                output_path=out,
                lineage_path=lineage_out,
                payload=report_payload,
                question=request.question_binding,
            )

    if options.engine == "recursive-agentic":
        report_payload = _run_recursive_analysis(
            request=request,
            graph=graph,
            starts=starts,
            objective=objective,
            transport=transport,
            lineage_out=lineage_out,
            cache_path=cache_path,
            checkpoint_path=paths["checkpoint"],
            premise_physical_requests=(
                premise_physical_requests
                if request.question_binding is not None and not request.start_refs
                else 0
            ),
        )
    else:
        report = BackwardTaintAnalyzer(
            judge=transport,
            max_depth=options.effective_max_depth,
            max_nodes=options.max_nodes,
        ).analyze(
            graph,
            start_refs=starts,
            objective=objective,
        )
        report_payload = question_bound_output_payload(
            attribution_output_payload(report, graph),
            graph,
            binding=request.question_binding,
            starts=starts,
        )
        report_payload = enrich_attribution_explanation(
            report_payload,
            graph,
            transport=transport,
        )
        atomic_write_json(out, report_payload)
        atomic_write_json(lineage_out, graph.message_lineage)
        publish_defect_explanation(
            out,
            report_payload,
            report_payload["defect_evolution"],
        )

    return AttributionResult(
        output_path=out,
        lineage_path=lineage_out,
        payload=report_payload,
        question=request.question_binding,
    )


def _run_recursive_analysis(
    *,
    request: AttributionRequest,
    graph: TraceGraph,
    starts: tuple[str, ...],
    objective: str,
    transport: ClaudeJudgeClient,
    lineage_out: Path,
    cache_path: Path,
    checkpoint_path: Path,
    premise_physical_requests: int,
) -> dict[str, Any]:
    options = request.options
    out = request.output_path
    budgets = {
        "max_frontier_items": options.max_frontier_items,
        "max_depth": options.effective_max_depth,
        "max_hypotheses": options.max_hypotheses,
        "max_investigation_rounds": options.max_investigation_rounds,
        "max_artifact_bytes": options.max_artifact_bytes,
        "max_judge_requests": options.max_judge_requests,
    }
    model_identity = stable_json(
        {
            "model": transport.model,
            "base_url": transport.base_url,
            "thinking": transport.thinking_config,
            "max_tokens": transport.max_tokens,
            "context_budget": transport.context_budget.to_dict(),
            "provider_error_threshold": transport.provider_error_threshold,
            "fusion_mode": options.fusion_mode,
            "global_judgment_contract": GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            "root_confirmation_contract": ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
            "defect_explanation_contract": DEFECT_EXPLANATION_PROMPT_SCHEMA_VERSION,
        }
    )
    checkpoint_config = build_checkpoint_config(
        trace=graph.raw_trace,
        case_id=graph.case_id,
        objective=objective,
        analysis_perspective=options.analysis_perspective,
        start_refs=starts,
        budgets=budgets,
        model_identity=model_identity,
        cache_identity=str(cache_path.expanduser().resolve()),
        runtime_identity={
            "judge_timeout_sec": options.judge_timeout_sec,
            "judge_max_tokens": transport.max_tokens,
            "thinking_mode": stable_json(transport.thinking_config),
            "base_url": transport.base_url,
            "provider_error_threshold": transport.provider_error_threshold,
            "fusion_mode": options.fusion_mode,
        },
    )
    checkpoint = CheckpointBundle(checkpoint_path)
    restored_replay = None
    historical_analyzer_requests = 0
    if _checkpoint_has_persisted_state(checkpoint):
        restored_replay = checkpoint.restore_for_replay(
            expected_config=checkpoint_config,
            expected_lineage=graph.message_lineage,
        )
        historical_analyzer_requests = _checkpoint_analyzer_request_count(
            restored_replay.state
        )
        if restored_replay.state.final_report is not None:
            report_payload = dict(restored_replay.state.final_report)
            checkpoint.completed_replay_output_commit(
                attribution_path=out,
                lineage_path=lineage_out,
                report=report_payload,
                message_lineage=graph.message_lineage,
            )
            evolution = report_payload.get("defect_evolution")
            if isinstance(evolution, Mapping):
                publish_defect_explanation(out, report_payload, evolution)
            return report_payload

    current_requests = _transport_physical_request_count(transport)
    remaining_requests = max(
        0,
        options.max_judge_requests
        - premise_physical_requests
        - historical_analyzer_requests,
    )
    causal_judge = ClaudeCausalJudge(
        transport=_SharedJudgeBudgetTransport(
            transport,
            maximum=current_requests + remaining_requests,
        ),
        cache=(
            transport.cache
            if isinstance(transport.cache, JudgmentCache)
            else JudgmentCache(cache_path)
        ),
    )
    with GracefulSignalState() as shutdown:
        report = AgenticRecursiveAnalyzer(
            judge=causal_judge,
            max_frontier_items=options.max_frontier_items,
            max_depth=options.effective_max_depth,
            max_hypotheses=options.max_hypotheses,
            max_investigation_rounds=options.max_investigation_rounds,
            max_artifact_bytes=options.max_artifact_bytes,
            max_judge_requests=options.max_judge_requests,
            checkpoint=checkpoint,
            checkpoint_config=checkpoint_config,
            stop_requested=shutdown.stop_requested,
            fusion_mode=options.fusion_mode,
        ).analyze(
            graph,
            start_refs=starts,
            objective=objective,
            analysis_perspective=options.analysis_perspective,
        )
        report_payload = question_bound_output_payload(
            attribution_output_payload(report, graph),
            graph,
            binding=request.question_binding,
            starts=starts,
        )
        analyzer_physical_requests = _report_analyzer_request_count(report_payload)
        report_payload = enrich_attribution_explanation(
            report_payload,
            graph,
            transport=(
                transport
                if (
                    premise_physical_requests + analyzer_physical_requests
                    < options.max_judge_requests
                )
                else None
            ),
        )
        report_payload = _annotate_shared_judge_budget(
            report_payload,
            maximum=options.max_judge_requests,
            premise_physical_requests=premise_physical_requests,
            analyzer_physical_requests=analyzer_physical_requests,
            explanation_physical_requests=_explanation_physical_requests(
                report_payload
            ),
        )
        output_commit = checkpoint.completed_replay_output_commit(
            attribution_path=out,
            lineage_path=lineage_out,
            report=report_payload,
            message_lineage=graph.message_lineage,
        )
        if output_commit is None:
            output_commit = publish_output_transaction(
                bundle=checkpoint,
                attribution_path=out,
                lineage_path=lineage_out,
                report=report_payload,
                message_lineage=graph.message_lineage,
                stop_requested=shutdown.stop_requested,
            )
            if report.metadata.get("termination_reason") == "signal_interrupted":
                checkpoint.record_action(
                    "analysis_interrupted",
                    "analysis:result",
                    {
                        "report": report_payload,
                        "interrupted": True,
                        "output_transaction_id": output_commit["transaction_id"],
                        "output_commit_hash": output_commit["output_commit_hash"],
                    },
                )
            else:
                checkpoint.mark_analysis_completed(
                    report=report_payload,
                    output_commit=output_commit,
                )
        publish_defect_explanation(
            out,
            report_payload,
            report_payload["defect_evolution"],
        )
    return report_payload


def _transport_physical_request_count(transport: Any) -> int:
    value = getattr(transport, "request_count", 0)
    return value if type(value) is int and value >= 0 else 0


class _SharedJudgeBudgetTransport:
    """Enforce the service-wide physical request cap without changing checkpoints."""

    def __init__(self, transport: Any, *, maximum: int) -> None:
        self._transport = transport
        self._maximum = max(0, int(maximum))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)

    def create_message_text_with_usage(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> Any:
        if _remaining_judge_requests(self._transport, self._maximum) <= 0:
            raise TransportCallError(
                RuntimeError("shared Judge request budget exhausted"),
                physical_requests=0,
            )
        return self._transport.create_message_text_with_usage(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )

    def create_message_text(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> str:
        try:
            return self.create_message_text_with_usage(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
            ).text
        except TransportCallError as exc:
            raise exc.error from exc


def _remaining_judge_requests(transport: Any, maximum: int) -> int:
    return max(0, int(maximum) - _transport_physical_request_count(transport))


def _annotate_shared_judge_budget(
    report: Mapping[str, Any],
    *,
    maximum: int,
    premise_physical_requests: int,
    analyzer_physical_requests: int,
    explanation_physical_requests: int,
) -> dict[str, Any]:
    payload = _json_safe_copy(report)
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        payload["metadata"] = metadata
    if isinstance(metadata.get("shared_judge_request_budget"), Mapping):
        return payload
    physical = (
        max(0, int(premise_physical_requests))
        + max(0, int(analyzer_physical_requests))
        + max(0, int(explanation_physical_requests))
    )
    metadata["shared_judge_request_budget"] = {
        "maximum_physical_requests": int(maximum),
        "physical_requests": physical,
        "remaining_physical_requests": max(0, int(maximum) - physical),
        "premise_physical_requests": max(0, int(premise_physical_requests)),
        "analyzer_physical_requests": max(0, int(analyzer_physical_requests)),
        "explanation_physical_requests": max(
            0, int(explanation_physical_requests)
        ),
        "enforced_across": [
            "question_premise",
            "recursive_attribution",
            "defect_explanation",
        ],
    }
    metadata["total_physical_judge_request_count"] = physical
    return payload


def _checkpoint_analyzer_request_count(state: Any) -> int:
    final_report = getattr(state, "final_report", None)
    if isinstance(final_report, Mapping):
        count = _report_analyzer_request_count(final_report)
        if count:
            return count
    actions = getattr(state, "actions", ())
    for record in reversed(tuple(actions)):
        if not isinstance(record, Mapping) or record.get("operation") != "state_snapshot":
            continue
        payload = record.get("payload")
        value = payload.get("judge_requests") if isinstance(payload, Mapping) else None
        if type(value) is int and value >= 0:
            return value
    return 0


def _checkpoint_has_persisted_state(checkpoint: Any) -> bool:
    paths = (
        getattr(checkpoint, "manifest_path", None),
        getattr(checkpoint, "current_path", None),
    )
    return any(isinstance(path, Path) and path.exists() for path in paths)


def _report_analyzer_request_count(report: Mapping[str, Any]) -> int:
    metadata = report.get("metadata")
    value = (
        metadata.get("physical_judge_request_count")
        if isinstance(metadata, Mapping)
        else 0
    )
    return value if type(value) is int and value >= 0 else 0


def _explanation_physical_requests(report: Mapping[str, Any]) -> int:
    evolution = report.get("defect_evolution")
    generation = (
        evolution.get("generation") if isinstance(evolution, Mapping) else None
    )
    value = (
        generation.get("physical_requests")
        if isinstance(generation, Mapping)
        else 0
    )
    return value if type(value) is int and value >= 0 else 0


def enrich_attribution_explanation(
    report: Mapping[str, Any],
    graph: TraceGraph,
    *,
    transport: ClaudeJudgeClient | None,
) -> dict[str, Any]:
    """Attach a grounded defect-evolution projection to a published report."""
    payload = _json_safe_copy(report)
    evolution = build_defect_evolution(payload, graph)
    if transport is not None and evolution.get("steps") and (
        payload.get("confirmed_roots") or payload.get("co_roots")
    ):
        evolution = synthesize_defect_evolution(
            evolution,
            transport=transport,
        )
    payload["defect_evolution"] = evolution
    return payload


def analysis_start_refs(
    graph: TraceGraph,
    explicit_refs: Iterable[str],
    *,
    question: str = "",
) -> tuple[str, ...]:
    requested = tuple(explicit_refs)
    if not requested:
        defaults = tuple(graph.default_start_refs())
        question_tokens = _normalized_tokens(question)
        if not question_tokens:
            return defaults
        indexed = list(enumerate(defaults))
        indexed.sort(
            key=lambda item: (
                -_start_relevance(graph, item[1], question_tokens),
                item[0],
            )
        )
        return tuple(ref for _, ref in indexed)

    resolved = tuple(graph.resolve(ref) or ref for ref in requested)
    for requested_ref, resolved_ref in zip(requested, resolved):
        node = graph.nodes.get(resolved_ref)
        if node is None or graph.analysis_start_eligible(resolved_ref):
            continue
        raise AttributionInputError(
            "--start-ref {0} resolves to an external evaluation fact that is ineligible for decisive judgment".format(
                requested_ref
            )
        )
    return resolved


def question_analysis_start_refs(
    graph: TraceGraph,
    explicit_refs: Iterable[str],
    *,
    question: str = "",
) -> tuple[str, ...]:
    """Select the bound user-question seed unless the caller chose explicit starts."""
    requested = tuple(explicit_refs)
    if requested or not question:
        return analysis_start_refs(graph, requested, question=question)

    projection = graph.raw_trace.get("offline_question_projection")
    if not isinstance(projection, Mapping):
        raise AttributionInputError("offline question projection is missing")
    seed_ref = str(projection.get("seed_ref") or "")
    resolved_ref = graph.resolve(seed_ref) or seed_ref
    seed = graph.nodes.get(resolved_ref)
    if seed is None or seed.data.get("hypothesis_origin") != "user_question":
        raise AttributionInputError("question premise seed is missing or invalid")
    return (resolved_ref,)


def bind_question_hypothesis(
    graph: TraceGraph, binding: AttributionQuestion
) -> TraceGraph:
    """Add an offline-only reported-deviation seed grounded in relevant facts."""
    if not isinstance(binding, AttributionQuestion):
        raise TypeError("binding must be an AttributionQuestion")
    trace = copy.deepcopy(graph.raw_trace)
    source_trace_hash = hashlib.sha256(
        stable_json(trace).encode("utf-8")
    ).hexdigest()
    record_id = "offline_question_{0}".format(binding.question_id[:20])
    expected_action = build_expected_action_projection(graph, binding.normalized)
    scoped_refs = (
        tuple(expected_action.get("evidence_refs") or ())
        if isinstance(expected_action, Mapping)
        else ()
    )
    source_refs = tuple(
        dict.fromkeys((*scoped_refs, *_question_evidence_refs(graph, binding.normalized)))
    )[:32]
    provenance = _question_revision_provenance(graph, source_refs)
    record = {
        "record_id": record_id,
        "component": "offline_attribution",
        "event_type": "case.observed_defect",
        "title": "User-reported expectation gap",
        "source_refs": list(source_refs),
        "data": {
            "seed_id": binding.question_id,
            "failure_type": "user_reported_expectation_gap",
            "expected": (
                "The recorded execution satisfies the user expectation expressed "
                "by the attribution question."
            ),
            "actual": binding.original,
            "mechanism": (
                "The question alleges an execution deviation. This premise is "
                "unverified until the offline Judge compares it with trace facts."
            ),
            "scope": "user_expectation",
            "question": binding.original,
            "hypothesis_origin": "user_question",
            "premise_status": "unverified",
            "offline_only": True,
            "behavior_impact": "none_offline_analysis_only",
            "root_candidate_eligible": False,
            **(
                {"expected_action_projection": expected_action}
                if expected_action is not None
                else {}
            ),
            **provenance,
        },
    }
    records = trace.get("records")
    if not isinstance(records, list):
        records = []
        trace["records"] = records
    records.append(record)
    trace["offline_question_projection"] = {
        "schema": "offline-question-hypothesis/v1",
        "question_id": binding.question_id,
        "seed_ref": "record:{0}".format(record_id),
        "source_trace_sha256": source_trace_hash,
        "evidence_refs": list(source_refs),
        "analysis_mode": "offline_read_only",
        **(
            {"expected_action_projection": expected_action}
            if expected_action is not None
            else {}
        ),
    }
    return TraceGraph.from_trace(
        trace,
        artifact_root=getattr(graph, "_artifact_root", None),
        artifact_reader=getattr(graph, "_artifact_reader", None),
    )


def assess_question_premise(
    transport: Any,
    graph: TraceGraph,
    binding: AttributionQuestion,
    *,
    seed_ref: str,
) -> dict[str, Any]:
    """Use a small LLM judgment to test the user's alleged deviation first."""
    seed = graph.nodes.get(seed_ref)
    if seed is None or seed.data.get("hypothesis_origin") != "user_question":
        raise AttributionInputError("question premise seed is missing or invalid")
    evidence_refs = tuple(graph.upstream_refs(seed_ref))[:24]
    evidence = [
        {
            "ref": ref,
            "fact": graph.nodes[ref].compact(max_chars=1400),
            "question_match_excerpt": _question_match_excerpt(
                stable_json(graph.nodes[ref].data), binding.normalized
            ),
        }
        for ref in evidence_refs
        if ref in graph.nodes
    ]
    prompt = stable_json(
        {
            "schema": "question-premise-assessment-request/v1",
            "question": binding.original,
            "reported_deviation": seed.data,
            "trace_facts": evidence,
            "instructions": [
                "Extract the expected behavior and the behavior alleged by the question.",
                "Return contradicted when trace facts directly show the allegedly missing or wrong behavior did not occur as alleged.",
                "Return supported when trace facts substantiate the alleged deviation.",
                "Return unknown when the provided facts cannot decide the premise.",
                "Judge only whether the question premise is true; do not perform root-cause attribution yet.",
                "When status is supported, localize the recorded behavioral contract without assigning root cause: cite the contract source, the actual event sequence, and the earliest offered ref where behavior first diverges.",
                "When an earlier reasoning or planning decision explicitly commits to the wrong order or action, use that decision as first_deviation_ref rather than the later tool call that merely materializes it.",
                "When expected_action_projection is present, compare each expected Skill lifecycle stage in order: discovery, selection, exposure, tool availability, then invocation; first_absent_stage localizes the observed boundary but is not itself a root-cause verdict.",
                "A Skill catalog node that proves successful exposure is contract/context evidence, not the first deviation for a later invocation omission; localize the first post-exposure LLM decision, tool choice, or exit that omitted the expected action.",
                "If expected_skill_names is empty or the relevant catalog artifact is unavailable, return unknown instead of guessing which Skill was required.",
                "Use only offered refs in evidence_refs and return one JSON object with no markdown.",
            ],
            "required_output": {
                "status": "supported|contradicted|unknown",
                "expected_behavior": "string",
                "alleged_actual_behavior": "string",
                "reason": "string",
                "evidence_refs": ["offered trace ref"],
                "missing_evidence": ["string"],
                "confidence": "number from 0 to 1",
                "deviation_type": "action_order|action_omission|state_precedence|semantic_change|tool_choice|output_quality|other",
                "contract_source_refs": ["offered trace ref"],
                "actual_sequence_refs": ["offered trace ref in chronological order"],
                "first_deviation_ref": "offered trace ref or empty string",
                "upstream_influence_refs": ["offered trace ref"],
                "localization_reason": "string",
            },
        }
    )
    text = transport.create_message_text(
        system=(
            "You verify a user's defect premise against a semantic agent trace. "
            "Do not infer a root cause. Return exactly one JSON object."
        ),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    return _validate_question_premise_assessment(
        parse_json_object(text), allowed_refs=frozenset(evidence_refs)
    )


def _load_or_assess_question_premise(
    *,
    transport: Any,
    graph: TraceGraph,
    binding: AttributionQuestion,
    seed_ref: str,
    checkpoint_path: Path,
    maximum_requests: int,
) -> tuple[dict[str, Any], int]:
    """Persist the premise gate so resume never re-judges checkpoint identity."""
    checkpoint = CheckpointBundle(checkpoint_path)
    store_path = Path(checkpoint_path) / "question-premise.json"
    identity = hashlib.sha256(
        stable_json(
            {
                "schema": "question-premise-checkpoint/v1",
                "trace_binding": _source_trace_hash(graph),
                "question_id": binding.question_id,
                "seed_ref": seed_ref,
            }
        ).encode("utf-8")
    ).hexdigest()
    allowed_refs = frozenset(graph.upstream_refs(seed_ref))
    migrated_store: Optional[dict[str, Any]] = None
    if not store_path.exists() and _checkpoint_has_persisted_state(checkpoint):
        restored = checkpoint.restore()
        final_report = restored.final_report
        analysis_question = (
            final_report.get("analysis_question")
            if isinstance(final_report, Mapping)
            else None
        )
        premise = (
            analysis_question.get("premise_assessment")
            if isinstance(analysis_question, Mapping)
            else None
        )
        if premise is None:
            raise AttributionInputError(
                "legacy incomplete checkpoint has no lossless question premise; "
                "refusing to alter its causal input or re-query the Provider"
            )
        else:
            migrated_assessment = _validate_question_premise_assessment(
                premise,
                allowed_refs=allowed_refs,
            )
            migration = "completed_checkpoint_report"
        migrated_store = {
            "schema": "question-premise-checkpoint/v1",
            "identity": identity,
            "physical_requests": _legacy_premise_request_count(final_report),
            "assessment": migrated_assessment,
            "migration": migration,
        }

    request_count_before_creation = _transport_physical_request_count(transport)

    def create_store() -> Mapping[str, Any]:
        if migrated_store is not None:
            return migrated_store
        before = _transport_physical_request_count(transport)
        if before >= int(maximum_requests):
            assessment = {
                "status": "unknown",
                "expected_behavior": "",
                "alleged_actual_behavior": binding.original,
                "reason": "Premise assessment skipped because the shared Judge request budget is exhausted.",
                "evidence_refs": [],
                "missing_evidence": ["judge_request_budget_exhausted"],
                "confidence": 0.0,
            }
        else:
            try:
                assessment = assess_question_premise(
                    transport,
                    graph,
                    binding,
                    seed_ref=seed_ref,
                )
            except Exception as exc:
                assessment = {
                    "status": "unknown",
                    "expected_behavior": "",
                    "alleged_actual_behavior": binding.original,
                    "reason": "Premise assessment failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    "evidence_refs": [],
                    "missing_evidence": ["premise_assessment_unavailable"],
                    "confidence": 0.0,
                }
        return {
            "schema": "question-premise-checkpoint/v1",
            "identity": identity,
            "physical_requests": max(
                0,
                _transport_physical_request_count(transport) - before,
            ),
            "assessment": assessment,
        }

    try:
        stored, _created = checkpoint.load_or_create_question_premise(
            identity=identity,
            reserve_physical_request=(
                migrated_store is None
                and request_count_before_creation < int(maximum_requests)
            ),
            factory=create_store,
        )
    except Exception as exc:
        if isinstance(exc, AttributionInputError):
            raise
        raise AttributionInputError(
            "question premise checkpoint could not be loaded or created"
        ) from exc
    physical_requests = stored.get("physical_requests")
    if type(physical_requests) is not int or physical_requests < 0:
        raise AttributionInputError(
            "question premise checkpoint has invalid request accounting"
        )
    if stored.get("state") == "inflight":
        return {
            "schema": "question-premise-assessment/v1",
            "status": "unknown",
            "expected_behavior": "",
            "alleged_actual_behavior": binding.original,
            "reason": "The prior premise Provider request completed or was interrupted before its response could be durably recorded.",
            "evidence_refs": [],
            "missing_evidence": ["premise_response_not_durably_recorded"],
            "confidence": 0.0,
            "deviation_type": "other",
            "contract_source_refs": [],
            "actual_sequence_refs": [],
            "first_deviation_ref": "",
            "upstream_influence_refs": [],
            "localization_reason": "",
            "analysis_mode": "offline_read_only",
        }, physical_requests
    assessment = _validate_question_premise_assessment(
        stored.get("assessment"),
        allowed_refs=allowed_refs,
    )
    return assessment, physical_requests


def _legacy_premise_request_count(report: Any) -> int:
    metadata = report.get("metadata") if isinstance(report, Mapping) else None
    shared = (
        metadata.get("shared_judge_request_budget")
        if isinstance(metadata, Mapping)
        else None
    )
    value = (
        shared.get("premise_physical_requests")
        if isinstance(shared, Mapping)
        else None
    )
    return value if type(value) is int and value >= 0 else 1


def _validate_question_premise_assessment(
    value: Any, *, allowed_refs: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("question premise assessment must be an object")
    status = str(value.get("status") or "")
    if status not in {"supported", "contradicted", "unknown"}:
        raise ValueError("question premise status is invalid")
    refs = _dedupe_report_values(
        ref
        for ref in value.get("evidence_refs") or ()
        if isinstance(ref, str) and ref in allowed_refs
    )
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("question premise confidence must be numeric")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("question premise confidence must be between 0 and 1")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("question premise reason is required")
    def grounded_refs(field: str) -> list[str]:
        return _dedupe_report_values(
            ref
            for ref in value.get(field) or ()
            if isinstance(ref, str) and ref in allowed_refs
        )

    first_deviation_ref = str(value.get("first_deviation_ref") or "").strip()
    if first_deviation_ref not in allowed_refs:
        first_deviation_ref = ""
    deviation_type = str(value.get("deviation_type") or "").strip().lower()
    if deviation_type not in {
        "action_order",
        "action_omission",
        "state_precedence",
        "semantic_change",
        "tool_choice",
        "output_quality",
        "other",
    }:
        deviation_type = "other"
    localization_reason = str(value.get("localization_reason") or "").strip()
    expected_behavior = str(value.get("expected_behavior") or "").strip()
    alleged_actual_behavior = str(
        value.get("alleged_actual_behavior") or ""
    ).strip()
    missing_evidence = _dedupe_report_values(
        str(item)
        for item in value.get("missing_evidence") or ()
        if str(item).strip()
    )
    if status == "contradicted":
        if not refs or not expected_behavior or not alleged_actual_behavior:
            raise ValueError(
                "contradicted premise requires grounded evidence_refs, "
                "expected_behavior, and alleged_actual_behavior"
            )
        if confidence < 0.5 or missing_evidence:
            raise ValueError(
                "contradicted premise requires decisive evidence without unresolved gaps"
            )
    return {
        "schema": "question-premise-assessment/v1",
        "status": status,
        "expected_behavior": expected_behavior,
        "alleged_actual_behavior": alleged_actual_behavior,
        "reason": reason,
        "evidence_refs": refs,
        "missing_evidence": missing_evidence,
        "confidence": confidence,
        "deviation_type": deviation_type,
        "contract_source_refs": grounded_refs("contract_source_refs"),
        "actual_sequence_refs": grounded_refs("actual_sequence_refs"),
        "first_deviation_ref": first_deviation_ref,
        "upstream_influence_refs": grounded_refs("upstream_influence_refs"),
        "localization_reason": localization_reason,
        "analysis_mode": "offline_read_only",
    }


def _question_match_excerpt(data: str, question: str, *, limit: int = 800) -> str:
    needles = re.findall(
        r"[a-z0-9][a-z0-9_.:/-]{3,}",
        unicodedata.normalize("NFKC", question).casefold(),
    )
    lowered = data.casefold()
    positions = [lowered.find(needle) for needle in needles if needle in lowered]
    if not positions:
        return data[:limit]
    start = max(0, min(positions) - limit // 4)
    return data[start : start + limit]


def bind_question_premise_assessment(
    graph: TraceGraph, assessment: Mapping[str, Any]
) -> TraceGraph:
    trace = copy.deepcopy(graph.raw_trace)
    projection = trace.get("offline_question_projection")
    if not isinstance(projection, dict):
        raise AttributionInputError("offline question projection is missing")
    seed_ref = str(projection.get("seed_ref") or "")
    seed_id = seed_ref.removeprefix("record:")
    for record in trace.get("records") or ():
        if isinstance(record, dict) and record.get("record_id") == seed_id:
            data = record.setdefault("data", {})
            data["premise_status"] = str(assessment.get("status") or "unknown")
            data["premise_assessment"] = _json_safe_copy(assessment)
            if assessment.get("status") == "supported":
                deviation_type = str(
                    assessment.get("deviation_type") or "user_reported_expectation_gap"
                )
                data["failure_type"] = deviation_type
                data["deviation_type"] = deviation_type
                data["expected"] = str(
                    assessment.get("expected_behavior") or data.get("expected") or ""
                )
                data["actual"] = str(
                    assessment.get("alleged_actual_behavior") or data.get("actual") or ""
                )
                data["mechanism"] = str(
                    assessment.get("localization_reason")
                    or assessment.get("reason")
                    or data.get("mechanism")
                    or ""
                )
                for field in (
                    "contract_source_refs",
                    "actual_sequence_refs",
                    "upstream_influence_refs",
                ):
                    data[field] = list(assessment.get(field) or ())
                data["first_deviation_ref"] = str(
                    assessment.get("first_deviation_ref") or ""
                )
            break
    else:
        raise AttributionInputError("offline question seed record is missing")
    projection["premise_assessment"] = _json_safe_copy(assessment)
    projection["first_deviation_ref"] = str(
        assessment.get("first_deviation_ref") or ""
    )
    projection["contract_source_refs"] = list(
        assessment.get("contract_source_refs") or ()
    )
    projection["actual_sequence_refs"] = list(
        assessment.get("actual_sequence_refs") or ()
    )
    return TraceGraph.from_trace(
        trace,
        artifact_root=getattr(graph, "_artifact_root", None),
        artifact_reader=getattr(graph, "_artifact_reader", None),
    )


def question_premise_no_defect_report(
    *,
    case_id: str,
    objective: str,
    seed_ref: str,
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_refs = list(assessment.get("evidence_refs") or ())
    return {
        "schema_version": "causal-attribution-report/v1",
        "case_id": case_id,
        "objective": objective,
        "analysis_outcome": "no_defect",
        "start_refs": [seed_ref],
        "confirmed_roots": [],
        "co_roots": [],
        "root_causes": [],
        "taint_paths": [],
        "unresolved_refs": [],
        "rejected_candidates": [],
        "hypotheses": [],
        "premise_assessment": _json_safe_copy(assessment),
        "seed_results": [
            {
                "start_ref": seed_ref,
                "outcome": "no_defect",
                "blocking_reasons": [],
                "missing_evidence": list(
                    assessment.get("missing_evidence") or ()
                ),
                "decisive_evidence_refs": evidence_refs,
                "confirmed_root_refs": [],
                "candidate_refs": [],
            }
        ],
        "metadata": {
            "analysis_mode": "offline_read_only",
            "termination_reason": "question_premise_contradicted",
            "judge_stage": "question_premise_assessment",
        },
    }


def question_premise_inconclusive_report(
    *,
    case_id: str,
    objective: str,
    seed_ref: str,
    assessment: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "causal-attribution-report/v1",
        "case_id": case_id,
        "objective": objective,
        "analysis_outcome": "inconclusive",
        "start_refs": [seed_ref],
        "confirmed_roots": [],
        "co_roots": [],
        "root_causes": [],
        "taint_paths": [],
        "unresolved_refs": [],
        "rejected_candidates": [],
        "hypotheses": [],
        "premise_assessment": _json_safe_copy(assessment),
        "seed_results": [
            {
                "start_ref": seed_ref,
                "outcome": "evidence_gap",
                "blocking_reasons": ["question_premise_unknown"],
                "missing_evidence": list(
                    assessment.get("missing_evidence") or ()
                ),
                "decisive_evidence_refs": list(
                    assessment.get("evidence_refs") or ()
                ),
                "confirmed_root_refs": [],
                "candidate_refs": [],
            }
        ],
        "metadata": {
            "analysis_mode": "offline_read_only",
            "termination_reason": "question_premise_unknown",
            "judge_stage": "question_premise_assessment",
        },
    }


def _question_evidence_refs(
    graph: TraceGraph, question: str, *, limit: int = 32
) -> tuple[str, ...]:
    tokens = _normalized_tokens(question)
    direct_ranked: list[tuple[int, int, str]] = []
    contextual_ranked: list[tuple[int, int, str]] = []
    direct_fact_types = {
        "decision",
        "change",
        "verification",
        "execution.observation",
        "tool.call",
        "tool.result",
        "tool.error",
        "mcp.call",
        "mcp.result",
        "skill.load",
        "skill.result",
    }
    fallback_types = {
        "decision",
        "change",
        "verification",
        "tool.call",
        "tool.result",
        "tool.error",
        "mcp.call",
        "mcp.result",
        "skill.load",
        "context.compaction",
        "message.input",
        "response.output",
        "response.claim",
    }
    for ref, node in graph.nodes.items():
        searchable = stable_json(node.compact(max_chars=6000))
        overlap = len(tokens.intersection(_normalized_tokens(searchable)))
        exact_bonus = 0
        normalized_question = unicodedata.normalize("NFKC", question).casefold()
        for value in re.findall(r"[a-z0-9][a-z0-9_.:/-]{3,}", normalized_question):
            if value in searchable.casefold():
                exact_bonus += 8
        event_bonus = 2 if node.event_type in fallback_types else 0
        score = overlap * 10 + exact_bonus + (event_bonus if overlap or exact_bonus else 0)
        if score:
            target = direct_ranked if node.event_type in direct_fact_types else contextual_ranked
            target.append((score, graph.position(ref), ref))
    direct_ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    contextual_ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    direct_reserve = min(len(direct_ranked), max(8, limit // 2))
    selected = [ref for _, _, ref in direct_ranked[:direct_reserve]]
    selected.extend(
        ref
        for _, _, ref in (*contextual_ranked, *direct_ranked[direct_reserve:])
        if ref not in selected
    )
    selected = selected[:limit]
    if not selected:
        selected = [
            ref
            for ref, node in reversed(tuple(graph.nodes.items()))
            if node.event_type in fallback_types
        ][: min(limit, 12)]
    return tuple(selected)


def _question_revision_provenance(
    graph: TraceGraph, refs: Iterable[str]
) -> dict[str, Any]:
    keys = (
        "repository_revision",
        "subject_revision",
        "revision_status",
        "revision_provenance_status",
    )
    for ref in refs:
        node = graph.nodes.get(ref)
        if node is None:
            continue
        values = {key: node.data.get(key) for key in keys if key in node.data}
        if values:
            return values
    return {}


def _normalized_tokens(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = set(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))
    for run in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", normalized):
        tokens.update(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    return frozenset(tokens)


def _start_relevance(
    graph: TraceGraph,
    ref: str,
    question_tokens: frozenset[str],
) -> int:
    node = graph.nodes.get(ref)
    if node is None:
        return 0
    summary = node.data.get("summary")
    fields = (
        node.title,
        summary if isinstance(summary, str) else "",
        node.component,
        node.event_type,
    )
    return len(question_tokens.intersection(_normalized_tokens(" ".join(fields))))


def attribution_output_payload(report: Any, graph: TraceGraph) -> dict[str, Any]:
    return annotate_report_semantic_anchors(
        graph.case_id,
        graph.nodes,
        report.to_dict(),
        graph=graph,
    )


def question_bound_output_payload(
    report: Mapping[str, Any],
    graph: TraceGraph,
    *,
    binding: AttributionQuestion | None,
    starts: Iterable[str],
) -> dict[str, Any]:
    """Attach a read-only question projection without changing objective reports."""
    payload = _json_safe_copy(report)
    selected_starts = tuple(starts)
    if binding is None:
        return payload
    offline_projection = graph.raw_trace.get("offline_question_projection")
    expected_action_projection = (
        offline_projection.get("expected_action_projection")
        if isinstance(offline_projection, Mapping)
        else None
    )

    payload.update(
        {
            "analysis_question": {
                "schema_version": QUESTION_SCHEMA_VERSION,
                "question": binding.original,
                "normalized_question": binding.normalized,
                "question_id": binding.question_id,
                "trace_binding": "sha256:{0}".format(
                    _source_trace_hash(graph)
                ),
                "selected_start_refs": list(selected_starts),
                "analysis_mode": "offline_read_only",
                **(
                    {"premise_assessment": _recorded_premise_assessment(graph)}
                    if _recorded_premise_assessment(graph)
                    else {}
                ),
                **(
                    {
                        "expected_action_projection": _json_safe_copy(
                            expected_action_projection
                        )
                    }
                    if isinstance(expected_action_projection, Mapping)
                    else {}
                ),
            },
            **_question_report_projection(payload, graph, starts=selected_starts),
        }
    )
    return payload


def _question_report_projection(
    report: Mapping[str, Any], graph: TraceGraph, *, starts: Iterable[str]
) -> dict[str, Any]:
    confirmed_roots = _question_roots(report)
    evidence_refs, evidence_gaps, supported_root_indexes = _root_evidence_projection(
        confirmed_roots,
        graph,
    )
    metadata = report.get("metadata")
    execution_failures = (
        list(metadata.get("analysis_execution_failures") or ())
        if isinstance(metadata, Mapping)
        else []
    )
    outcome = str(report.get("analysis_outcome") or "inconclusive")
    premise_assessment = _report_premise_assessment(report, graph)
    premise_evidence_refs = [
        ref
        for ref in premise_assessment.get("evidence_refs") or ()
        if isinstance(ref, str)
        and graph.active_revision_evidence_reference_eligible(ref)
    ]
    rootless_gaps = (
        []
        if confirmed_roots or execution_failures or outcome == "no_defect"
        else [
            {
                "kind": "no_confirmed_root_cause",
                "reason": "evidence_insufficient",
            }
        ]
    )
    return {
        "question_premise_status": _question_premise_status(
            confirmed_roots,
            outcome,
            assessed_status=str(premise_assessment.get("status") or ""),
        ),
        "conclusion": _question_conclusion(
            confirmed_roots,
            outcome,
            has_execution_failures=bool(execution_failures),
        ),
        "causal_chain": _taint_paths(
            report.get("taint_paths"),
            starts=starts,
            roots=confirmed_roots,
        ),
        "supporting_evidence_refs": _dedupe_report_values(
            [*evidence_refs, *premise_evidence_refs]
        ),
        "rejected_hypotheses": _rejected_hypotheses(report),
        "confidence": (
            float(premise_assessment.get("confidence") or 0.0)
            if outcome == "no_defect"
            else _root_confidence(
                confirmed_roots,
                supported_root_indexes=supported_root_indexes,
            )
        ),
        "unresolved_gaps": _dedupe_report_values(
            [
                *execution_failures,
                *_unresolved_gaps(report),
                *evidence_gaps,
                *rootless_gaps,
            ]
        ),
    }


def _source_trace_hash(graph: TraceGraph) -> str:
    projection = graph.raw_trace.get("offline_question_projection")
    if isinstance(projection, Mapping):
        value = str(projection.get("source_trace_sha256") or "")
        if len(value) == 64:
            return value
    return hashlib.sha256(stable_json(graph.raw_trace).encode("utf-8")).hexdigest()


def _recorded_premise_assessment(graph: TraceGraph) -> dict[str, Any]:
    projection = graph.raw_trace.get("offline_question_projection")
    if not isinstance(projection, Mapping):
        return {}
    assessment = projection.get("premise_assessment")
    return _json_safe_copy(assessment) if isinstance(assessment, Mapping) else {}


def _report_premise_assessment(
    report: Mapping[str, Any], graph: TraceGraph
) -> dict[str, Any]:
    assessment = report.get("premise_assessment")
    if isinstance(assessment, Mapping):
        return _json_safe_copy(assessment)
    return _recorded_premise_assessment(graph)


def _question_premise_status(
    roots: Iterable[Mapping[str, Any]],
    outcome: str,
    *,
    assessed_status: str = "",
) -> str:
    if outcome == "no_defect":
        return "contradicted"
    if assessed_status in {"supported", "contradicted", "unknown"}:
        return assessed_status
    if tuple(roots):
        return "supported"
    return "unknown"


def _question_roots(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    roots: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for root in (
        *_mapping_items(report.get("confirmed_roots")),
        *_mapping_items(report.get("co_roots")),
        *_mapping_items(report.get("root_causes")),
    ):
        key = (str(root.get("node_ref") or ""), _root_identity(root))
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _root_identity(root: Mapping[str, Any]) -> str:
    occurrence = root.get("semantic_occurrence_id")
    if isinstance(occurrence, str) and occurrence:
        return occurrence
    direct = root.get("confirmation_identity")
    if isinstance(direct, str) and direct:
        return direct
    confirmation = root.get("confirmation")
    if isinstance(confirmation, Mapping):
        identity = confirmation.get("confirmation_identity")
        if isinstance(identity, str) and identity:
            return identity
    return ""


def _question_conclusion(
    roots: Iterable[Mapping[str, Any]],
    outcome: str,
    *,
    has_execution_failures: bool = False,
) -> str:
    roots = list(roots)
    if not roots:
        if outcome == "execution_failed":
            return (
                "execution_failed: semantic attribution did not complete; "
                "no root-cause conclusion was attempted."
            )
        if has_execution_failures:
            return (
                "{0}: semantic attribution completed only for some seeds; "
                "no root-cause conclusion was attempted for the failed seeds."
            ).format(outcome)
        if outcome == "no_defect":
            return (
                "The user-reported deviation is not supported by the recorded "
                "trace facts; no defect or root cause was confirmed."
            )
        return "{0}: no confirmed root cause; evidence is insufficient.".format(outcome)
    return "\n".join(
        "{0}: {1}".format(
            str(root.get("node_ref") or ""),
            _root_reason(root) or "no recorded root reason.",
        )
        for root in roots
    )


def _root_reason(root: Mapping[str, Any]) -> str:
    reason = str(root.get("reason") or "")
    if not reason:
        defect_state = root.get("defect_state")
        if isinstance(defect_state, Mapping):
            reason = str(defect_state.get("mechanism") or "")
    return reason


def _taint_paths(
    value: Any,
    *,
    starts: Iterable[str],
    roots: Iterable[Mapping[str, Any]],
) -> list[list[Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    start_refs = {str(ref) for ref in starts if isinstance(ref, str) and ref}
    root_refs = {
        str(root.get("node_ref") or "")
        for root in roots
        if str(root.get("node_ref") or "")
    }
    output: list[list[Any]] = []
    for path in value:
        if not isinstance(path, (list, tuple)):
            continue
        path_refs = {ref for ref in path if isinstance(ref, str)}
        if not path_refs.intersection(start_refs):
            continue
        if root_refs and not path_refs.intersection(root_refs):
            continue
        output.append(_json_safe_copy(path))
    return output


def _root_evidence_projection(
    roots: Iterable[Mapping[str, Any]], graph: TraceGraph
) -> tuple[list[str], list[dict[str, Any]], frozenset[int]]:
    refs: list[str] = []
    gaps: list[dict[str, Any]] = []
    supported_root_indexes: set[int] = set()
    for index, root in enumerate(roots):
        root_ref = str(root.get("node_ref") or "")
        eligible_for_root: list[str] = []
        for ref in _root_evidence_refs(root):
            if (
                not isinstance(ref, str)
                or not ref
                or not graph.active_revision_evidence_reference_eligible(ref)
            ):
                if isinstance(ref, str) and ref:
                    gaps.append(
                        {
                            "kind": "root_evidence_ref_rejected",
                            "root_ref": root_ref,
                            "ref": ref,
                            "reason": "missing_or_ineligible",
                        }
                    )
                continue
            refs.append(ref)
            eligible_for_root.append(ref)
        if eligible_for_root:
            supported_root_indexes.add(index)
        else:
            gaps.append(
                {
                    "kind": "root_without_eligible_supporting_evidence",
                    "root_ref": root_ref,
                }
            )
    return (
        _dedupe_report_values(refs),
        _dedupe_report_values(gaps),
        frozenset(supported_root_indexes),
    )


def _root_evidence_refs(root: Mapping[str, Any]) -> list[Any]:
    refs: list[Any] = []
    for field in ("evidence_refs", "observed_defect_refs"):
        value = root.get(field)
        if isinstance(value, (list, tuple)):
            refs.extend(value)
    return _dedupe_report_values(refs)


def _rejected_hypotheses(report: Mapping[str, Any]) -> list[Any]:
    rejected = list(_mapping_items(report.get("rejected_candidates")))
    rejected.extend(
        item
        for item in _mapping_items(report.get("hypotheses"))
        if item.get("status") == "rejected"
    )
    return _dedupe_report_values(rejected)


def _root_confidence(
    roots: Iterable[Mapping[str, Any]], *, supported_root_indexes: frozenset[int]
) -> float:
    values = [
        float(root["confidence"])
        for index, root in enumerate(roots)
        if index in supported_root_indexes
        if not isinstance(root.get("confidence"), bool)
        and isinstance(root.get("confidence"), (int, float))
    ]
    return max(values, default=0.0)


def _unresolved_gaps(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = [
        {"kind": "unresolved_ref", "ref": ref}
        for ref in report.get("unresolved_refs") or ()
        if isinstance(ref, str) and ref
    ]
    trace_report = report.get("trace_improvement_report")
    if not isinstance(trace_report, Mapping):
        metadata = report.get("metadata")
        trace_report = (
            metadata.get("trace_improvement_report")
            if isinstance(metadata, Mapping)
            else None
        )
    if isinstance(trace_report, Mapping):
        for key in ("blocking_gaps", "analysis_gaps", "advisory_gaps"):
            gaps.extend(
                {
                    "kind": "trace_improvement_gap",
                    "category": key,
                    "detail": _json_safe_copy(gap),
                }
                for gap in _mapping_items(trace_report.get(key))
            )
    return _dedupe_report_values(gaps)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _dedupe_report_values(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        identity = stable_json(value)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return result


def _json_safe_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_copy(item) for item in value]
    return value


def lineage_output_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(
        "{0}.message-lineage.json".format(attribution_out.stem)
    )


def judge_cache_output_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name(
        "{0}.judge-cache.jsonl".format(attribution_out.stem)
    )


def recursive_checkpoint_path(
    attribution_out: Path,
    configured: Path | str | None,
) -> Path:
    if configured:
        return Path(configured)
    return attribution_out.with_name("{0}.checkpoint".format(attribution_out.stem))


def validate_request_path_isolation(request: AttributionRequest) -> dict[str, Path]:
    """Resolve all writable paths and reject aliases before any analysis side effect."""
    output = request.output_path.resolve()
    lineage = lineage_output_path(output, request.options.lineage_output_path).resolve()
    judge_cache = judge_cache_output_path(output, request.options.judge_cache_path).resolve()
    writable = {
        "output": output,
        "explanation": explanation_output_path(output).resolve(),
        "lineage": lineage,
        "judge_cache": judge_cache,
    }
    if request.options.engine == "recursive-agentic":
        writable["checkpoint"] = recursive_checkpoint_path(
            output, request.options.checkpoint_path
        ).resolve()
    items = tuple(writable.items())
    for index, (left_name, left_path) in enumerate(items):
        for right_name, right_path in items[index + 1 :]:
            if left_path == right_path:
                raise AttributionInputError(
                    "writable path alias: {0} and {1} both resolve to {2}".format(
                        left_name, right_name, left_path
                    )
                )
            if _same_existing_filesystem_object(left_path, right_path):
                raise AttributionInputError(
                    "writable filesystem alias: {0} and {1} resolve to the same file".format(
                        left_name, right_name
                    )
                )
            if _path_is_within_filesystem(
                left_path, right_path
            ) or _path_is_within_filesystem(right_path, left_path):
                raise AttributionInputError(
                    "writable path containment collision: {0} resolves to {1}; {2} resolves to {3}".format(
                        left_name,
                        left_path,
                        right_name,
                        right_path,
                    )
                )

    inputs = {
        "trace": request.trace_path,
        "benchmark_bundle": request.benchmark_bundle_path,
        "review": request.review_path,
        **{
            "evaluation[{0}]".format(index): path
            for index, path in enumerate(request.evaluation_paths)
        },
    }
    for writable_name, writable_path in items:
        for input_name, input_path in inputs.items():
            if input_path is None:
                continue
            resolved_input = input_path.resolve()
            if writable_path == resolved_input:
                raise AttributionInputError(
                    "writable path aliases input: {0} and {1} both resolve to {2}".format(
                        writable_name, input_name, writable_path
                    )
                )
            if _same_existing_filesystem_object(writable_path, resolved_input):
                raise AttributionInputError(
                    "writable filesystem alias: {0} and {1} resolve to the same file".format(
                        writable_name, input_name
                    )
                )

    bundle = request.benchmark_bundle_path
    if bundle is not None and bundle.is_dir():
        resolved_bundle = bundle.resolve()
        for writable_name, writable_path in items:
            if _path_is_within_filesystem(writable_path, resolved_bundle):
                raise AttributionInputError(
                    "writable path is inside benchmark bundle input: {0} resolves to {1}".format(
                        writable_name, writable_path
                    )
                )

    checkpoint = writable.get("checkpoint")
    if checkpoint is not None and checkpoint.exists() and not checkpoint.is_dir():
        raise AttributionInputError(
            "recursive checkpoint directory resolves to an existing file: {0}".format(
                checkpoint
            )
        )
    return writable


def _path_is_within_filesystem(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        pass

    if _existing_directory_is_physical_ancestor(path, directory):
        return True

    path_ancestor, path_suffix = _deepest_existing_ancestor(path)
    directory_ancestor, directory_suffix = _deepest_existing_ancestor(directory)
    if not _same_existing_filesystem_object(path_ancestor, directory_ancestor):
        return False
    comparison_root = (
        path_ancestor
        if path_ancestor.is_dir()
        else path_ancestor.parent
    )
    if _filesystem_is_case_insensitive(comparison_root):
        path_suffix = tuple(part.casefold() for part in path_suffix)
        directory_suffix = tuple(part.casefold() for part in directory_suffix)
    return path_suffix[: len(directory_suffix)] == directory_suffix


def _existing_directory_is_physical_ancestor(path: Path, directory: Path) -> bool:
    try:
        if not directory.is_dir():
            return False
    except OSError:
        return False

    candidate, _ = _deepest_existing_ancestor(path)
    try:
        if not candidate.is_dir():
            candidate = candidate.parent
    except OSError:
        candidate = candidate.parent
    while True:
        if _same_existing_filesystem_object(candidate, directory):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def _same_existing_filesystem_object(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(str(left), str(right))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def _deepest_existing_ancestor(path: Path) -> tuple[Path, tuple[str, ...]]:
    candidate = path
    suffix: list[str] = []
    while True:
        try:
            if candidate.exists():
                return candidate, tuple(reversed(suffix))
        except OSError:
            pass
        parent = candidate.parent
        if parent == candidate:
            return candidate, tuple(reversed(suffix))
        suffix.append(candidate.name)
        candidate = parent


def _filesystem_is_case_insensitive(directory: Path) -> bool:
    """Detect directory lookup semantics using only existing filesystem objects."""
    try:
        with os.scandir(str(directory)) as iterator:
            entries = tuple(iterator)
        entry_names = {entry.name for entry in entries}
        for entry in entries:
            if entry.is_symlink():
                continue
            alternate_name = _ascii_case_variant(entry.name)
            if alternate_name is None:
                continue
            if alternate_name in entry_names:
                return False
            return _same_existing_filesystem_object(
                Path(entry.path), directory / alternate_name
            )
    except OSError:
        pass

    candidate = directory
    while candidate.parent != candidate:
        alternate_name = _ascii_case_variant(candidate.name)
        if alternate_name is not None:
            return _same_existing_filesystem_object(
                candidate, candidate.with_name(alternate_name)
            )
        candidate = candidate.parent
    return False


def _ascii_case_variant(name: str) -> str | None:
    for index, character in enumerate(name):
        if "a" <= character <= "z":
            return name[:index] + character.upper() + name[index + 1 :]
        if "A" <= character <= "Z":
            return name[:index] + character.lower() + name[index + 1 :]
    return None


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a JSON output after fsyncing file and directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_graph(
    trace_path: Optional[Path],
    review_path: Optional[Path] = None,
    evaluation_paths: Iterable[Path] = (),
    *,
    bundle_path: Optional[Path] = None,
) -> TraceGraph:
    graph = load_benchmark_case(
        bundle_path=bundle_path,
        trace_path=trace_path,
        review_path=review_path,
        evaluation_paths=tuple(evaluation_paths),
        role="attribution",
    ).graph
    return graph


class _CompositeArtifactReader:
    def __init__(
        self,
        routes: Mapping[str, tuple[VerifiedArtifactReader, str]],
    ) -> None:
        self.routes = dict(routes)

    def read(self, artifact_id: str, *, refresh: bool = False) -> Any:
        route = self.routes.get(artifact_id)
        if route is None:
            return VerifiedArtifactReader({}, None).read(artifact_id)
        reader, original_id = route
        return replace(
            reader.read(original_id, refresh=refresh),
            artifact_id=artifact_id,
        )


def _logical_segment_paths(trace_path: Path) -> tuple[Path, ...]:
    path = Path(trace_path).expanduser().resolve()
    if path.name != "trace.json" or not path.parent.is_dir():
        return (path,)
    directory_name = path.parent.name
    base_name = directory_name.split("--", 1)[0]
    parent = path.parent.parent
    candidates = []
    for directory in parent.iterdir():
        if not directory.is_dir():
            continue
        if directory.name != base_name and not directory.name.startswith(
            base_name + "--"
        ):
            continue
        candidate = directory / "trace.json"
        if candidate.is_file():
            candidates.append(candidate.resolve())
    return tuple(sorted(set(candidates), key=str)) or (path,)


def _merge_logical_case_segments(
    primary_graph: TraceGraph,
    *,
    primary_path: Path,
    segment_paths: Iterable[Path],
) -> TraceGraph:
    loaded: list[tuple[Path, dict[str, Any], VerifiedArtifactReader]] = []
    for path in segment_paths:
        if path == primary_path:
            trace = copy.deepcopy(primary_graph.raw_trace)
            reader = getattr(primary_graph, "_artifact_reader")
        else:
            with path.open("r", encoding="utf-8") as handle:
                trace = json.load(handle)
            artifact_index = {
                str(item.get("artifact_id")): item
                for item in trace.get("artifacts") or ()
                if isinstance(item, dict) and item.get("artifact_id")
            }
            reader = VerifiedArtifactReader(
                artifact_index,
                artifact_root_for_trace_path(path, trace),
            )
        loaded.append((path, trace, reader))
    loaded.sort(
        key=lambda item: (
            str(
                item[1].get("manifest", {}).get("started_at")
                if isinstance(item[1].get("manifest"), Mapping)
                else ""
            ),
            str(item[0]),
        )
    )
    base_case_id = primary_path.parent.name.split("--", 1)[0]
    merged_records: list[tuple[str, int, int, dict[str, Any]]] = []
    merged_edges: list[dict[str, Any]] = []
    merged_artifacts: list[dict[str, Any]] = []
    artifact_routes: dict[str, tuple[VerifiedArtifactReader, str]] = {}
    segment_manifest = []
    for segment_index, (path, trace, reader) in enumerate(loaded):
        prefix = "seg_{0}".format(segment_index)
        manifest = trace.get("manifest") if isinstance(trace.get("manifest"), Mapping) else {}
        artifact_ids = {
            str(item.get("artifact_id"))
            for item in trace.get("artifacts") or ()
            if isinstance(item, Mapping) and item.get("artifact_id")
        }
        for artifact in trace.get("artifacts") or ():
            if not isinstance(artifact, Mapping) or not artifact.get("artifact_id"):
                continue
            original_id = str(artifact["artifact_id"])
            namespaced_id = "{0}__{1}".format(prefix, original_id)
            copied = _namespace_segment_value(
                artifact,
                prefix=prefix,
                artifact_ids=artifact_ids,
            )
            copied["artifact_id"] = namespaced_id
            copied["logical_segment_id"] = prefix
            merged_artifacts.append(copied)
            artifact_routes[namespaced_id] = (reader, original_id)
        for record_index, record in enumerate(trace.get("records") or ()):
            if not isinstance(record, Mapping) or not record.get("record_id"):
                continue
            copied = _namespace_segment_record(
                record,
                prefix=prefix,
                artifact_ids=artifact_ids,
                logical_case_id=base_case_id,
            )
            timestamp = str(copied.get("timestamp") or manifest.get("started_at") or "")
            merged_records.append(
                (timestamp, segment_index, record_index, copied)
            )
        for edge in trace.get("dataflow_edges") or ():
            if not isinstance(edge, Mapping):
                continue
            copied = _namespace_segment_value(
                edge,
                prefix=prefix,
                artifact_ids=artifact_ids,
            )
            for endpoint_name in ("from", "to"):
                endpoint = copied.get(endpoint_name)
                if isinstance(endpoint, dict) and endpoint.get("id"):
                    endpoint["id"] = _namespace_identity(
                        str(endpoint["id"]), prefix
                    )
            metadata = copied.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                copied["metadata"] = metadata
            metadata["logical_segment_id"] = prefix
            merged_edges.append(copied)
        segment_manifest.append(
            {
                "segment_id": prefix,
                "path": str(path),
                "physical_case_id": str(manifest.get("case_id") or ""),
                "session_id": str(manifest.get("session_id") or ""),
                "started_at": str(manifest.get("started_at") or ""),
                "ended_at": str(manifest.get("ended_at") or ""),
                "record_count": len(trace.get("records") or ()),
                "artifact_count": len(trace.get("artifacts") or ()),
            }
        )
    merged_records.sort(key=lambda item: item[:3])
    primary_manifest = copy.deepcopy(
        primary_graph.raw_trace.get("manifest")
        if isinstance(primary_graph.raw_trace.get("manifest"), Mapping)
        else {}
    )
    primary_manifest.update(
        {
            "case_id": base_case_id,
            "logical_trace": True,
            "logical_segment_count": len(segment_manifest),
            "logical_segments": segment_manifest,
            "collection_mode": "offline_logical_segment_merge",
            "behavior_impact": "none_offline_analysis_only",
        }
    )
    merged_trace = {
        "case_id": base_case_id,
        "manifest": primary_manifest,
        "records": [item[3] for item in merged_records],
        "dataflow_edges": merged_edges,
        "artifacts": merged_artifacts,
        "logical_segments": segment_manifest,
    }
    return TraceGraph.from_trace(
        merged_trace,
        artifact_reader=_CompositeArtifactReader(artifact_routes),
    )


_SEGMENT_IDENTITY_KEYS = frozenset(
    {
        "call_id",
        "callID",
        "message_id",
        "messageID",
        "part_id",
        "partID",
        "decision_id",
        "verification_id",
        "change_id",
        "claim_id",
        "assessment_id",
        "evaluation_id",
        "segment_id",
        "span_id",
        "parent_span_id",
        "context_snapshot_id",
        "llm_turn_id",
    }
)


def _namespace_identity(value: str, prefix: str) -> str:
    if not value or value.startswith(prefix + "__"):
        return value
    return "{0}__{1}".format(prefix, value)


def _namespace_reference(value: str, prefix: str) -> str:
    if ":" not in value:
        return _namespace_identity(value, prefix)
    ref_type, ref_id = value.split(":", 1)
    if not ref_type or not ref_id:
        return value
    return "{0}:{1}".format(ref_type, _namespace_identity(ref_id, prefix))


def _namespace_segment_value(
    value: Any,
    *,
    prefix: str,
    artifact_ids: set[str],
    key: str = "",
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(child_key): _namespace_segment_value(
                child,
                prefix=prefix,
                artifact_ids=artifact_ids,
                key=str(child_key),
            )
            for child_key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _namespace_segment_value(
                child,
                prefix=prefix,
                artifact_ids=artifact_ids,
                key=key,
            )
            for child in value
        ]
    if not isinstance(value, str):
        return copy.deepcopy(value)
    if value.startswith("artifact:") and value[9:] in artifact_ids:
        return "artifact:{0}__{1}".format(prefix, value[9:])
    if key == "artifact_id" and value in artifact_ids:
        return "{0}__{1}".format(prefix, value)
    if key in _SEGMENT_IDENTITY_KEYS:
        return _namespace_identity(value, prefix)
    if key.endswith("_ref") or key.endswith("Ref") or key.endswith("_refs") or key.endswith("Refs"):
        return _namespace_reference(value, prefix)
    return value


def _namespace_segment_record(
    record: Mapping[str, Any],
    *,
    prefix: str,
    artifact_ids: set[str],
    logical_case_id: str,
) -> dict[str, Any]:
    copied = _namespace_segment_value(
        record,
        prefix=prefix,
        artifact_ids=artifact_ids,
    )
    copied["record_id"] = _namespace_identity(
        str(record.get("record_id") or ""), prefix
    )
    copied["source_refs"] = [
        _namespace_reference(str(ref), prefix)
        for ref in record.get("source_refs") or ()
    ]
    copied["logical_segment_id"] = prefix
    data = copied.get("data")
    if isinstance(data, dict):
        data["logical_segment_id"] = prefix
        if "case_id" in data:
            data["case_id"] = logical_case_id
    return copied
