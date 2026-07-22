"""Bounded recursive semantic-defect traversal for offline causal attribution."""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .causal_judge import (
    BoundedJudgeCallResult,
    BoundedJudgeCallError,
    BoundedJudgeCapability,
    CausalJudge,
    CausalStepRequest,
    OfflineJudgeCapability,
    RootConfirmationRequest,
    bind_root_confirmation,
    preflight_root_confirmation_request,
)
from .causal_retrieval import (
    SemanticPredecessorRetriever,
    canonical_candidate_route,
    is_evidence_only_node,
    is_navigation_node,
    root_candidate_eligible,
)
from .causal_state import (
    AttributionHypothesis,
    CausalCandidate,
    CausalFactor,
    CausalStepJudgment,
    ConfirmedRoot,
    DefectState,
    FrontierItem,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RejectedCandidate,
    RootConfirmation,
    SeedAttributionResult,
    confirmation_identity_for,
    seed_binding_identity_for,
    validate_confirmation_ownership,
)
from .checkpoint import CheckpointBundle, CheckpointState
from .confirmation_path import is_confirmation_causal_edge
from .errors import JudgeProviderError, JudgeProviderUnavailable
from .evidence_capsule import (
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
    candidate_compression_metrics,
)
from .global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    MAX_ROOT_CONFIRMATION_CANDIDATES,
    GlobalCandidateJudgeRequest,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
    active_focus_text_sha256,
    global_candidate_request_from_validation_envelope,
    validate_active_focus_binding,
    validate_global_candidate_payload,
)
from .graph import TraceGraph
from .hypotheses import HypothesisLedger, RecursiveFrontier
from .investigation import (
    AttributionControlDirective,
    CausalInvestigationTools,
    InvestigationDirective,
    InvestigationResult,
)
from .judgment_context import (
    build_recursive_judgment_context,
    sanitize_judge_evidence_payload,
    task_obligations,
)
from .models import JsonDict, TraceNode, stable_json


RECURSIVE_RELATIONS = frozenset(
    {"same_defect_propagation", "defect_transformation", "contributing_condition"}
)
CAUSAL_STEP_CANDIDATE_LIMIT = 8
NAVIGATION_ROUTE_CANDIDATE_LIMIT = 2
EVALUATION_START_EVENTS = frozenset(
    {
        "case.failed",
        "case.observed_defect",
        "case.quality_gap",
        "case.missing_semantic",
        "external.evaluation_fact",
    }
)
FRONTIER_STATE_SCHEMA = "recursive-analysis-frontier/v2"
LEGACY_FRONTIER_STATE_SCHEMA = "recursive-analysis-frontier/v1"
HYPOTHESIS_STATE_SCHEMA = "recursive-analysis-hypotheses/v1"
ACTION_STATE_SCHEMA = "recursive-analysis-actions/v3"
PROVIDER_STATE_SCHEMA = "recursive-provider-state/v1"
PROVIDER_STATE_KEYS = {
    "schema",
    "circuit",
    "cache_identity",
    "cache_stats",
    "accounting",
    "identity",
}
PROVIDER_CIRCUIT_KEYS = {
    "open",
    "reason",
    "consecutive_provider_errors",
    "provider_error_threshold",
}
PROVIDER_ACCOUNTING_KEYS = {
    "judge_requests",
    "judge_request_uncertainty_count",
    "logical_judge_calls",
    "logical_confirmation_calls",
    "investigation_rounds",
    "artifact_bytes",
}
def _require_exact_checkpoint_keys(
    value: Mapping[str, Any], expected: Set[str], label: str
) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("{0} must be an object".format(label))
    actual = {str(key) for key in value}
    if actual != expected:
        raise ValueError(
            "{0} schema mismatch (missing={1}, extra={2})".format(
                label, sorted(expected - actual), sorted(actual - expected)
            )
        )


def _checkpoint_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _checkpoint_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_checkpoint_json(item) for item in value]
    if isinstance(value, set):
        return [_checkpoint_json(item) for item in sorted(value, key=str)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("checkpoint state contains a non-JSON value: {0}".format(type(value).__name__))


def _migrate_checkpoint_visit_references(
    value: Any,
    frontier: RecursiveFrontier,
) -> Any:
    if isinstance(value, Mapping):
        output: JsonDict = {}
        for key, item in value.items():
            migrated_key = frontier.migrate_visit_key_occurrences(str(key))
            if migrated_key in output:
                raise ValueError("visit-key migration would overwrite a checkpoint mapping")
            output[migrated_key] = _migrate_checkpoint_visit_references(item, frontier)
        return output
    if isinstance(value, list):
        return [_migrate_checkpoint_visit_references(item, frontier) for item in value]
    if isinstance(value, str):
        return frontier.migrate_visit_key_occurrences(value)
    return copy.deepcopy(value)


def _normalize_migrated_context_hashes(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_migrated_context_hashes(item) for item in value]
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)

    output = {
        str(key): _normalize_migrated_context_hashes(item)
        for key, item in value.items()
    }
    context_hashes: Dict[str, str] = {}
    for context_key, hash_key in (
        ("context_before", "context_before_hash"),
        ("context_after", "context_after_hash"),
    ):
        context = output.get(context_key)
        if not isinstance(context, Mapping):
            continue
        normalized_context = dict(context)
        if "evidence_hash" in normalized_context:
            semantic_context = dict(normalized_context)
            semantic_context.pop("evidence_hash")
            normalized_context["evidence_hash"] = hashlib.sha256(
                stable_json(semantic_context).encode("utf-8")
            ).hexdigest()
        output[context_key] = normalized_context
        context_hashes[hash_key] = hashlib.sha256(
            stable_json(normalized_context).encode("utf-8")
        ).hexdigest()
        if hash_key in output:
            output[hash_key] = context_hashes[hash_key]

    linkage = output.get("rejudge_linkage")
    if isinstance(linkage, Mapping):
        normalized_linkage = dict(linkage)
        if (
            "source_context_hash" in normalized_linkage
            and "context_before_hash" in context_hashes
        ):
            normalized_linkage["source_context_hash"] = context_hashes[
                "context_before_hash"
            ]
        if (
            "context_after_hash" in normalized_linkage
            and "context_after_hash" in context_hashes
        ):
            normalized_linkage["context_after_hash"] = context_hashes[
                "context_after_hash"
            ]
        output["rejudge_linkage"] = normalized_linkage
    return output


def _migrate_checkpoint_journal_records(
    records: Sequence[Mapping[str, Any]], frontier: RecursiveFrontier
) -> Dict[str, JsonDict]:
    latest: Dict[str, JsonDict] = {}
    previous_hash = ""
    for source in sorted(records, key=lambda item: int(item["sequence"])):
        migrated = _normalize_migrated_context_hashes(
            _migrate_checkpoint_visit_references(source, frontier)
        )
        unsigned = {
            str(key): copy.deepcopy(value)
            for key, value in migrated.items()
            if key != "record_hash"
        }
        unsigned["previous_hash"] = previous_hash
        record = {
            **unsigned,
            "record_hash": hashlib.sha256(
                stable_json(unsigned).encode("utf-8")
            ).hexdigest(),
        }
        semantic_key = str(record["semantic_key"])
        latest[semantic_key] = record
        previous_hash = record["record_hash"]
    return latest


def _dedupe_strings(values: Iterable[str]) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for value in values:
        item = str(value)
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return tuple(output)


def _semantic_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    return stable_json(value)


def _first_semantic_value(data: Mapping[str, Any], keys: Sequence[str], fallback: str) -> str:
    for key in keys:
        value = _semantic_text(data.get(key))
        if value:
            return value
    return fallback


def _seed_defect_state(node: TraceNode, objective: str) -> DefectState:
    data = node.data
    if node.event_type == "external.evaluation_fact":
        status = _first_semantic_value(
            data, ("status",), node.status or "unknown"
        ).lower()
        return DefectState.create(
            label="external_evaluation_{0}".format(status),
            expected=_first_semantic_value(
                data,
                ("assertion",),
                "The externally evaluated behavior satisfies its assertion.",
            ),
            actual=_first_semantic_value(
                data,
                ("observation",),
                "The external evaluator did not record an observation.",
            ),
            mechanism="External evaluation status: {0}.".format(status),
            scope=_first_semantic_value(
                data, ("scope",), "external_evaluation"
            ),
        )
    if node.event_type == "response.claim":
        claim = _first_semantic_value(
            data,
            ("text", "claim", "summary", "description"),
            "The final response contains an ungrounded claim candidate.",
        )
        return DefectState.create(
            label="unsupported_response_claim",
            expected=(
                objective
                or "The final response claim is fully grounded, temporally valid, and not contradicted."
            ),
            actual=claim,
            mechanism=(
                "The claim may be unsupported, contradicted, incomplete, or fully valid; "
                "defect presence is unconfirmed until evidence comparison."
            ),
            scope="response_quality",
        )
    label = _first_semantic_value(
        data,
        ("failure_type", "gap_kind", "dimension", "issue_kind", "defect_type"),
        "observed_defect",
    )
    summary = _first_semantic_value(
        data, ("summary", "description", "reason", "text"), label
    )
    return DefectState.create(
        label=label,
        expected=_first_semantic_value(
            data,
            ("expected", "expected_behavior", "requirement", "criterion"),
            objective,
        ),
        actual=_first_semantic_value(
            data,
            ("actual", "actual_behavior", "observed", "result"),
            summary,
        ),
        mechanism=_first_semantic_value(
            data,
            ("mechanism", "failure_mechanism", "cause", "reason"),
            summary,
        ),
        scope=_first_semantic_value(
            data,
            ("scope", "attribution_domain", "component", "dimension"),
            node.component or node.event_type or "task_quality",
        ),
    )


def _global_evidence_search_text(node: TraceNode) -> str:
    data = node.data
    payload: JsonDict = {
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
    }
    for key in (
        "tool_name",
        "status",
        "title",
        "command",
        "description",
        "fact_kind",
        "semantic_role",
        "verification_status",
        "verification_result",
        "structured_claim",
    ):
        value = data.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    for key in ("args", "input"):
        value = data.get(key)
        if not isinstance(value, Mapping):
            continue
        selected = {
            nested_key: value[nested_key]
            for nested_key in ("command", "description", "path")
            if nested_key in value
        }
        if selected:
            payload[key] = selected
    for key in ("metadata", "output", "data"):
        value = data.get(key)
        if not isinstance(value, Mapping):
            continue
        selected = {
            nested_key: value[nested_key]
            for nested_key in (
                "preview",
                "exit",
                "exit_code",
                "status",
                "description",
            )
            if nested_key in value
        }
        if selected:
            preview = selected.get("preview")
            if isinstance(preview, str) and len(preview) > 1600:
                selected["preview"] = preview[:1600]
            payload[key] = selected
    return stable_json(payload).casefold()


def _grounded_downstream_path(
    graph: TraceGraph,
    start_ref: str,
    target_refs: Sequence[str],
    *,
    max_hops: int = 48,
) -> Tuple[str, ...]:
    start = graph.resolve(start_ref) or start_ref
    targets = {
        graph.resolve(ref) or str(ref)
        for ref in target_refs
        if str(ref)
    }
    if start not in graph.nodes or not targets:
        return ()
    if start in targets:
        return (start,)
    queue: List[Tuple[str, Tuple[str, ...]]] = [(start, (start,))]
    visited = {start}
    cursor = 0
    while cursor < len(queue):
        current, path = queue[cursor]
        cursor += 1
        if len(path) - 1 >= max_hops:
            continue
        downstream = sorted(
            set(graph.downstream_refs(current)),
            key=lambda ref: (graph.position(ref), ref),
        )
        for next_ref in downstream:
            if next_ref in visited:
                continue
            edges = graph.edge_context(current, next_ref)
            if not any(
                is_confirmation_causal_edge(edge, default_eligible=True)
                for edge in edges
            ) or not graph.edge_endpoints_eligible(current, next_ref):
                continue
            next_path = (*path, next_ref)
            if next_ref in targets:
                return next_path
            visited.add(next_ref)
            queue.append((next_ref, next_path))
    return ()


def _assert_report_grounded_evidence(
    graph: TraceGraph, report: RecursiveAttributionReport, *, label: str
) -> None:
    validate_confirmation_ownership(
        report.confirmations,
        report.seed_results,
        label=label,
    )
    refs: List[str] = []
    identity_refs: List[str] = []
    for seed in report.seed_results:
        if seed.confirmed_root_refs or seed.confirmation_identities:
            identity_refs.append(seed.start_ref)
        identity_refs.extend(seed.selected_candidate_refs)
        identity_refs.extend(seed.confirmed_root_refs)
        refs.extend(seed.decisive_evidence_refs)
        judgment = seed.global_judgment
        if judgment:
            global_candidate_request_from_validation_envelope(
                seed.to_dict()["global_judgment"].get("validation_envelope"),
                graph=graph,
            )
        refs.extend(judgment.get("decisive_evidence_refs") or ())
        identity_refs.extend(judgment.get("selected_candidate_refs") or ())
        for assessment in judgment.get("assessments") or ():
            if not isinstance(assessment, Mapping):
                continue
            identity_refs.append(str(assessment.get("candidate_ref") or ""))
            identity_refs.extend(assessment.get("causal_path_refs") or ())
            refs.extend(assessment.get("evidence_refs") or ())
            refs.extend(assessment.get("causal_path_refs") or ())
    for confirmation in report.confirmations:
        candidate = graph.nodes.get(confirmation.candidate_ref)
        if candidate is not None and not root_candidate_eligible(candidate):
            raise ValueError(
                "{0} confirmation candidate is not authored-root eligible".format(
                    label
                )
            )
        identity_refs.append(confirmation.candidate_ref)
        identity_refs.extend(confirmation.recursive_path)
        refs.extend(confirmation.evidence_refs)
        for competitor in confirmation.competitor_comparisons:
            if not isinstance(competitor, Mapping):
                continue
            identity_refs.append(str(competitor.get("candidate_ref") or ""))
            identity_refs.extend(competitor.get("recursive_path") or ())
    for root in (*report.confirmed_roots, *report.co_roots):
        identity_refs.append(root.node_ref)
        identity_refs.extend(root.recursive_path)
        identity_refs.extend(root.observed_defect_refs)
        identity_refs.extend(root.episode_member_refs)
        refs.extend(root.evidence_refs)
    graph.assert_resolved_evidence_references(
        _dedupe_strings(refs), label=label
    )
    graph.assert_resolved_node_references(
        _dedupe_strings(identity_refs), label=label
    )
    for confirmation in report.confirmations:
        if confirmation.status != "confirmed":
            continue
        owners = [
            seed
            for seed in report.seed_results
            if confirmation.seed_binding_identity
            == seed_binding_identity_for(seed.start_ref, seed.defect_fingerprint)
            and confirmation.recursive_path
            and confirmation.recursive_path[-1] == seed.start_ref
        ]
        if len(owners) != 1:
            raise ValueError(
                "{0} confirmation path has no unique seed owner".format(label)
            )
        seed = owners[0]
        _assert_active_confirmation_path(
            graph,
            confirmation.recursive_path,
            candidate_ref=confirmation.candidate_ref,
            seed_ref=seed.start_ref,
            label=label,
        )
        global_judgment = seed.global_judgment
        if global_judgment.get("outcome") == "candidate_roots":
            selected = tuple(
                str(ref)
                for ref in global_judgment.get("selected_candidate_refs") or ()
            )
            assessments = [
                item
                for item in global_judgment.get("assessments") or ()
                if isinstance(item, Mapping)
                and str(item.get("candidate_ref") or "")
                == confirmation.candidate_ref
            ]
            if (
                confirmation.candidate_ref not in selected
                or len(assessments) != 1
                or tuple(assessments[0].get("causal_path_refs") or ())
                != confirmation.recursive_path
            ):
                raise ValueError(
                    "{0} confirmation path does not match its selected global assessment path".format(
                        label
                    )
                )
    for root in (*report.confirmed_roots, *report.co_roots):
        confirmation = RootConfirmation.from_dict(dict(root.confirmation))
        owner = next(
            (
                seed
                for seed in report.seed_results
                if confirmation.seed_binding_identity
                == seed_binding_identity_for(
                    seed.start_ref, seed.defect_fingerprint
                )
                and root.recursive_path
                and root.recursive_path[-1] == seed.start_ref
            ),
            None,
        )
        if owner is None:
            raise ValueError("{0} root path has no seed owner".format(label))
        _assert_active_confirmation_path(
            graph,
            root.recursive_path,
            candidate_ref=root.node_ref,
            seed_ref=owner.start_ref,
            label=label,
        )


def _assert_active_confirmation_path(
    graph: TraceGraph,
    path: Sequence[str],
    *,
    candidate_ref: str,
    seed_ref: str,
    label: str,
) -> None:
    canonical_path = tuple(str(ref) for ref in path)
    if (
        not canonical_path
        or canonical_path[0] != candidate_ref
        or canonical_path[-1] != seed_ref
        or any(graph.resolve(ref) != ref for ref in canonical_path)
    ):
        raise ValueError(
            "{0} confirmation path must exactly bind its candidate and seed".format(
                label
            )
        )
    for source_ref, target_ref in zip(canonical_path, canonical_path[1:]):
        edges = graph.edge_context(source_ref, target_ref)
        if not graph.edge_endpoints_eligible(source_ref, target_ref) or not any(
            is_confirmation_causal_edge(edge, default_eligible=True)
            for edge in edges
        ):
            raise ValueError(
                "{0} confirmation path lacks a grounded causal edge: {1}->{2}".format(
                    label, source_ref, target_ref
                )
            )


def _global_evidence_score(node: TraceNode) -> float:
    if node.event_type == "external.evaluation_fact":
        return 1.0
    if node.event_type == "verification":
        return 1.0
    if node.event_type not in {
        "tool.result",
        "tool.error",
        "evidence.fact",
        "evidence.semantic_fact",
        "claim.support_assessment",
    }:
        return 0.0
    text = _global_evidence_search_text(node)
    verification_terms = (
        "pytest",
        "unittest",
        "mocha",
        "jest",
        "vitest",
        "npm test",
        "pnpm test",
        "yarn test",
        "run focused",
        " tests pass",
        " test pass",
        " passing",
        " failed",
        "verification_status",
        "verification_result",
    )
    if not any(term in text for term in verification_terms):
        return 0.0
    if node.event_type in {"evidence.fact", "evidence.semantic_fact"}:
        return 0.85
    if node.event_type == "claim.support_assessment":
        return 0.82
    return 0.8


def _clone_graph(graph: TraceGraph) -> TraceGraph:
    """Create an analysis-owned graph so hydration cannot mutate the caller's graph."""
    return TraceGraph.from_trace(
        copy.deepcopy(graph.raw_trace),
        artifact_root=getattr(graph, "_artifact_root", None),
    )


def _candidate_key(candidate: CausalCandidate) -> Tuple[str, str, str]:
    return (candidate.ref, candidate.source, stable_json(candidate.edge))


def _artifact_payloads(value: Any) -> Iterable[Tuple[str, bytes]]:
    """Yield only explicitly hydrated artifact payloads with stable identities."""
    if isinstance(value, Mapping):
        hydrated = value.get("hydrated_artifacts")
        if isinstance(hydrated, (list, tuple)):
            for artifact in hydrated:
                if not isinstance(artifact, Mapping):
                    continue
                artifact_id = str(artifact.get("artifact_id") or "").strip()
                content = artifact.get("content")
                if not artifact_id or not isinstance(content, str) or not content:
                    continue
                identity = stable_json(
                    {
                        "artifact_id": artifact_id,
                        "hash": str(artifact.get("hash") or ""),
                        "path": str(artifact.get("path") or ""),
                    }
                )
                yield identity, content.encode("utf-8")
        for key, child in value.items():
            if key == "hydrated_artifacts":
                continue
            yield from _artifact_payloads(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _artifact_payloads(child)


def _judge_transport(judge: CausalJudge) -> Any:
    return getattr(judge, "transport", judge)


def _provider_circuit(judge: CausalJudge) -> JsonDict:
    target = _judge_transport(judge)
    stats = getattr(target, "provider_circuit_stats", None)
    if callable(stats):
        value = stats()
        if isinstance(value, Mapping):
            return dict(value)
    value = getattr(target, "provider_circuit", None)
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "open": bool(getattr(target, "provider_circuit_open", False)),
        "reason": str(getattr(target, "provider_circuit_reason", "") or ""),
    }


def _provider_state_payload(
    judge: CausalJudge,
    state: "RecursiveAnalysisState",
    *,
    cache_identity: str,
) -> JsonDict:
    target = _judge_transport(judge)
    cache = getattr(judge, "cache", None) or getattr(target, "cache", None)
    stats = getattr(cache, "stats", None)
    cache_stats = stats() if callable(stats) else {"enabled": False}
    unsigned = {
        "schema": PROVIDER_STATE_SCHEMA,
        "circuit": {
            "open": bool(getattr(target, "provider_circuit_open", False)),
            "reason": str(getattr(target, "provider_circuit_reason", "") or ""),
            "consecutive_provider_errors": int(
                getattr(target, "consecutive_provider_errors", 0) or 0
            ),
            "provider_error_threshold": int(
                getattr(target, "provider_error_threshold", 3) or 3
            ),
        },
        "cache_identity": str(cache_identity),
        "cache_stats": _checkpoint_json(cache_stats),
        "accounting": {
            "judge_requests": state.judge_requests,
            "judge_request_uncertainty_count": state.judge_request_uncertainty_count,
            "logical_judge_calls": state.logical_judge_calls,
            "logical_confirmation_calls": state.logical_confirmation_calls,
            "investigation_rounds": state.investigation_rounds,
            "artifact_bytes": state.artifact_bytes,
        },
    }
    return {
        **unsigned,
        "identity": hashlib.sha256(stable_json(unsigned).encode("utf-8")).hexdigest(),
    }


def _validate_provider_state(
    value: Any,
    state: "RecursiveAnalysisState",
    *,
    cache_identity: str,
    require_accounting_match: bool = True,
) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("provider state must be an object")
    _require_exact_checkpoint_keys(value, PROVIDER_STATE_KEYS, "provider state")
    provider = dict(value)
    if provider["schema"] != PROVIDER_STATE_SCHEMA:
        raise ValueError("unsupported provider state schema")
    circuit = provider["circuit"]
    _require_exact_checkpoint_keys(circuit, PROVIDER_CIRCUIT_KEYS, "provider circuit")
    if type(circuit["open"]) is not bool:
        raise ValueError("provider circuit open flag is invalid")
    if not isinstance(circuit["reason"], str):
        raise ValueError("provider circuit reason is invalid")
    for key in ("consecutive_provider_errors", "provider_error_threshold"):
        if type(circuit[key]) is not int or circuit[key] < 0:
            raise ValueError("provider circuit counter is invalid: {0}".format(key))
    if circuit["provider_error_threshold"] < 1:
        raise ValueError("provider error threshold must be positive")
    if provider["cache_identity"] != cache_identity:
        raise ValueError("provider cache identity does not match checkpoint config")
    if not isinstance(provider["cache_stats"], Mapping):
        raise ValueError("provider cache stats must be an object")
    accounting = provider["accounting"]
    _require_exact_checkpoint_keys(
        accounting, PROVIDER_ACCOUNTING_KEYS, "provider accounting"
    )
    for key in PROVIDER_ACCOUNTING_KEYS:
        if type(accounting[key]) is not int or accounting[key] < 0:
            raise ValueError("provider accounting counter is invalid: {0}".format(key))
    expected_accounting = {
        "judge_requests": state.judge_requests,
        "judge_request_uncertainty_count": state.judge_request_uncertainty_count,
        "logical_judge_calls": state.logical_judge_calls,
        "logical_confirmation_calls": state.logical_confirmation_calls,
        "investigation_rounds": state.investigation_rounds,
        "artifact_bytes": state.artifact_bytes,
    }
    if require_accounting_match and dict(accounting) != expected_accounting:
        raise ValueError("provider accounting does not match recursive state")
    unsigned = {key: provider[key] for key in provider if key != "identity"}
    identity = hashlib.sha256(stable_json(unsigned).encode("utf-8")).hexdigest()
    if provider["identity"] != identity:
        raise ValueError("provider state identity does not match contents")
    return copy.deepcopy(provider)


def _reference_envelope(ref: str, *, content: str = "", fact_kind: str = "") -> JsonDict:
    value: JsonDict = {
        "raw_ref": ref,
        "resolved_ref": ref,
        "resolution_status": "resolved",
        "provenance_class": "recorded",
    }
    if content:
        value["content"] = content
    if fact_kind:
        value["fact_kind"] = fact_kind
    return value


def _node_semantic_content(node: TraceNode) -> str:
    def without_hydrated_artifacts(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): without_hydrated_artifacts(child)
                for key, child in value.items()
                if str(key) != "hydrated_artifacts"
            }
        if isinstance(value, (list, tuple)):
            return [without_hydrated_artifacts(child) for child in value]
        return copy.deepcopy(value)

    data = without_hydrated_artifacts(node.data)
    return stable_json(
        {
            "component": node.component,
            "event_type": node.event_type,
            "title": node.title,
            "status": node.status,
            "data": data,
        }
    )


def _artifact_hydration_manifest(node: TraceNode) -> Optional[JsonDict]:
    artifacts = node.data.get("hydrated_artifacts")
    if not isinstance(artifacts, (list, tuple)) or not artifacts:
        return None
    referenced: List[str] = []
    hydrated: List[JsonDict] = []
    missing: List[str] = []
    truncated: List[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        artifact_id = str(artifact.get("artifact_id") or "").strip().removeprefix(
            "artifact:"
        )
        if not artifact_id:
            continue
        referenced.append(artifact_id)
        is_missing = bool(artifact.get("missing")) or not isinstance(
            artifact.get("content"), str
        )
        is_truncated = bool(artifact.get("truncated"))
        if is_missing:
            missing.append(artifact_id)
            continue
        content = str(artifact.get("content") or "")
        if is_truncated:
            truncated.append(artifact_id)
        hydrated.append(
            {
                "artifact_id": artifact_id,
                "content": content,
                "content_hash": "sha256:{0}".format(
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                ),
                "byte_count": len(content.encode("utf-8")),
                "byte_range": [0, len(content.encode("utf-8"))],
                "owner_reference": _reference_envelope(node.ref),
                "missing": False,
                "truncated": is_truncated,
            }
        )
    if not referenced:
        return None
    return {
        "node_ref": node.ref,
        "referenced_artifact_ids": list(dict.fromkeys(referenced)),
        "hydrated_artifacts": hydrated,
        "missing_artifact_ids": list(dict.fromkeys(missing)),
        "truncated_artifact_ids": list(dict.fromkeys(truncated)),
    }


def _perspective_tokens(value: str) -> Set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    output: Set[str] = set()
    word: List[str] = []

    def flush_word() -> None:
        if word:
            token = "".join(word)
            if len(token) >= 2:
                output.add(token)
            word.clear()

    cjk_run: List[str] = []
    for char in normalized:
        if "\u3400" <= char <= "\u9fff":
            flush_word()
            cjk_run.append(char)
            continue
        if cjk_run:
            output.update(cjk_run)
            output.update(
                "".join(cjk_run[index : index + size])
                for size in (2, 3, 4)
                for index in range(max(0, len(cjk_run) - size + 1))
            )
            cjk_run.clear()
        if char.isalnum() or char == "_":
            word.append(char)
        else:
            flush_word()
    flush_word()
    if cjk_run:
        output.update(cjk_run)
        output.update(
            "".join(cjk_run[index : index + size])
            for size in (2, 3, 4)
            for index in range(max(0, len(cjk_run) - size + 1))
        )
    return output


def _rejudge_success_terminal_state(
    judgment: CausalStepJudgment,
    *,
    bounded_judge: bool,
    offline_judge: bool,
    physical_request_delta: Optional[int],
) -> str:
    missing = " ".join(judgment.missing_evidence).casefold()
    if "judge_request_budget_exhausted" in missing:
        return "budget_exhausted"
    if "judge_validation_error" in missing:
        return "validation_error"
    if "judge_provider_error" in missing:
        return "provider_error"
    if offline_judge:
        return "offline_success"
    if bounded_judge and physical_request_delta == 0:
        return "cache_hit"
    return "success"


def _seed_ledger_key(start_ref: str, defect_fingerprint: str) -> str:
    return seed_binding_identity_for(start_ref, defect_fingerprint)


@dataclass
class SeedAttributionBuilder:
    start_ref: str
    defect_state: DefectState
    candidate_refs: Set[str] = field(default_factory=set)
    selected_candidate_refs: Set[str] = field(default_factory=set)
    confirmation_identities: Set[str] = field(default_factory=set)
    confirmed_root_confirmation_identities: Set[str] = field(default_factory=set)
    confirmed_root_refs: Set[str] = field(default_factory=set)
    decisive_evidence_refs: Set[str] = field(default_factory=set)
    missing_evidence: Set[str] = field(default_factory=set)
    blocking_reasons: Set[str] = field(default_factory=set)
    global_judgment: JsonDict = field(default_factory=dict)
    expansion_history: List[JsonDict] = field(default_factory=list)
    no_defect: bool = False

    @property
    def key(self) -> str:
        return _seed_ledger_key(self.start_ref, self.defect_state.fingerprint)

    def mark_no_defect(self) -> None:
        self.no_defect = True

    def mark_unresolved(self, reason: str, details: str = "") -> None:
        normalized_reason = (
            reason.strip() if isinstance(reason, str) else ""
        ) or "unresolved_evidence"
        self.blocking_reasons.add(normalized_reason)
        self.missing_evidence.add(
            (details.strip() if isinstance(details, str) else "")
            or "The required evidence remains unresolved: {0}.".format(
                normalized_reason
            )
        )

    def record_global_judgment(
        self,
        judgment: GlobalCandidateJudgment,
        candidate_refs: Iterable[str],
        request: GlobalCandidateJudgeRequest,
    ) -> None:
        self.candidate_refs.update(str(ref) for ref in candidate_refs if ref)
        self.selected_candidate_refs.update(judgment.selected_candidate_refs)
        self.decisive_evidence_refs.update(judgment.decisive_evidence_refs)
        self.global_judgment = copy.deepcopy(judgment.to_dict())
        self.global_judgment["schema_version"] = (
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION
        )
        self.global_judgment["validation_envelope"] = (
            request.validation_envelope()
        )
        self.expansion_history.extend(
            copy.deepcopy(dict(item)) for item in judgment.expansion_requests
        )
        if judgment.outcome == "no_defect":
            self.mark_no_defect()
        elif judgment.outcome == "inconclusive":
            self.mark_unresolved(
                "global_judgment_inconclusive",
                "; ".join(judgment.missing_evidence) or judgment.reason,
            )

    def record_confirmation(self, confirmation: RootConfirmation) -> None:
        self.candidate_refs.add(confirmation.candidate_ref)
        self.confirmation_identities.add(confirmation.confirmation_identity)
        self.decisive_evidence_refs.update(confirmation.evidence_refs)
        if confirmation.status == "confirmed":
            self.confirmed_root_confirmation_identities.add(
                confirmation.confirmation_identity
            )
            self.confirmed_root_refs.add(confirmation.candidate_ref)
            return
        if confirmation.status == "unknown":
            self.mark_unresolved("root_confirmation_unknown", confirmation.reason)

    def to_result(self) -> SeedAttributionResult:
        if self.blocking_reasons or self.missing_evidence:
            outcome = "evidence_gap"
        elif self.confirmed_root_refs:
            outcome = "confirmed_root"
        elif self.no_defect:
            outcome = "no_defect"
        else:
            outcome = "inconclusive"
        confirmed_root_refs = self.confirmed_root_refs
        if outcome != "confirmed_root":
            confirmed_root_refs = set()
        return SeedAttributionResult(
            start_ref=self.start_ref,
            defect_fingerprint=self.defect_state.fingerprint,
            defect_state=self.defect_state,
            outcome=outcome,
            candidate_refs=tuple(self.candidate_refs),
            selected_candidate_refs=tuple(self.selected_candidate_refs),
            confirmation_identities=tuple(self.confirmation_identities),
            confirmed_root_refs=tuple(confirmed_root_refs),
            decisive_evidence_refs=tuple(self.decisive_evidence_refs),
            missing_evidence=tuple(self.missing_evidence),
            blocking_reasons=tuple(self.blocking_reasons),
            global_judgment=self.global_judgment,
            expansion_history=tuple(self.expansion_history),
        )

    def to_dict(self) -> JsonDict:
        return {
            **self.to_result().to_dict(),
            "no_defect": self.no_defect,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "SeedAttributionBuilder":
        result = SeedAttributionResult.from_dict(value)
        builder = cls(
            start_ref=result.start_ref,
            defect_state=result.defect_state,
            candidate_refs=set(result.candidate_refs),
            selected_candidate_refs=set(result.selected_candidate_refs),
            confirmation_identities=set(result.confirmation_identities),
            confirmed_root_confirmation_identities=set(),
            confirmed_root_refs=set(result.confirmed_root_refs),
            decisive_evidence_refs=set(result.decisive_evidence_refs),
            missing_evidence=set(result.missing_evidence),
            blocking_reasons=set(result.blocking_reasons),
            global_judgment=copy.deepcopy(result.to_dict()["global_judgment"]),
            expansion_history=copy.deepcopy(result.to_dict()["expansion_history"]),
            no_defect=bool(value.get("no_defect", result.outcome == "no_defect")),
        )
        return builder


@dataclass
class RecursiveAnalysisState:
    """Mutable orchestration state containing immutable causal domain values."""

    graph: TraceGraph
    start_refs: Tuple[str, ...]
    objective: str
    analysis_perspective: str
    ledger: HypothesisLedger = field(default_factory=HypothesisLedger)
    frontier: RecursiveFrontier = field(default_factory=RecursiveFrontier)
    defect_states: Dict[str, DefectState] = field(default_factory=dict)
    causal_candidates: List[CausalCandidate] = field(default_factory=list)
    causal_relations: List[PredecessorAssessment] = field(default_factory=list)
    step_judgments: List[CausalStepJudgment] = field(default_factory=list)
    introduction_candidates: List[CausalCandidate] = field(default_factory=list)
    introduction_bindings: List[JsonDict] = field(default_factory=list)
    introduction_binding_keys: Set[Tuple[str, str, str, str]] = field(default_factory=set)
    contributing_conditions: List[CausalFactor] = field(default_factory=list)
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
    taint_paths: List[Tuple[str, ...]] = field(default_factory=list)
    visited_order: List[str] = field(default_factory=list)
    unresolved_branches: List[JsonDict] = field(default_factory=list)
    unresolved_refs: List[str] = field(default_factory=list)
    unresolved_hypothesis_ids: Set[str] = field(default_factory=set)
    introduction_hypothesis_ids: Set[str] = field(default_factory=set)
    present_hypothesis_ids: Set[str] = field(default_factory=set)
    visit_evidence: Dict[str, Set[str]] = field(default_factory=dict)
    transformation_chains: Dict[str, Tuple[DefectState, ...]] = field(default_factory=dict)
    exhausted_budgets: Dict[str, int] = field(default_factory=dict)
    artifact_identities: Set[str] = field(default_factory=set)
    artifact_bytes: int = 0
    processed_items: int = 0
    judge_requests: int = 0
    judge_request_uncertainty_count: int = 0
    logical_judge_calls: int = 0
    investigation_rounds: int = 0
    investigation_result_bytes: int = 0
    investigation_journal: List[JsonDict] = field(default_factory=list)
    investigation_evidence: Dict[str, List[JsonDict]] = field(default_factory=dict)
    investigation_evidence_hashes: Dict[str, Set[str]] = field(default_factory=dict)
    control_directive_ids: Set[str] = field(default_factory=set)
    confirmation_queue: List[JsonDict] = field(default_factory=list)
    confirmation_queue_keys: Set[Tuple[str, str, str, str]] = field(default_factory=set)
    confirmations: List[RootConfirmation] = field(default_factory=list)
    confirmed_roots: List[ConfirmedRoot] = field(default_factory=list)
    co_roots: List[ConfirmedRoot] = field(default_factory=list)
    amplifying_factors: List[CausalFactor] = field(default_factory=list)
    confirmation_journal: List[JsonDict] = field(default_factory=list)
    logical_confirmation_calls: int = 0
    pending_rejudge_journal: Dict[str, List[int]] = field(default_factory=dict)
    seed_count: int = 0
    provider_state: JsonDict = field(default_factory=dict)
    replay_actions: Dict[str, JsonDict] = field(default_factory=dict)
    seed_ledger: Dict[str, SeedAttributionBuilder] = field(default_factory=dict)
    hypothesis_seed_keys: Dict[str, str] = field(default_factory=dict)

    def _ensure_seed(
        self, start_ref: str, defect_state: DefectState
    ) -> SeedAttributionBuilder:
        key = _seed_ledger_key(start_ref, defect_state.fingerprint)
        builder = self.seed_ledger.get(key)
        if builder is None:
            builder = SeedAttributionBuilder(
                start_ref=str(start_ref),
                defect_state=defect_state,
            )
            self.seed_ledger[key] = builder
            self.seed_count = len(self.seed_ledger)
        return builder

    def enqueue_confirmation(self, value: Mapping[str, Any]) -> bool:
        candidate_ref = str(value.get("candidate_ref") or "")
        hypothesis_id = str(value.get("hypothesis_id") or "")
        defect_fingerprint = str(value.get("defect_fingerprint") or "")
        seed_binding_identity = str(value.get("seed_binding_identity") or "")
        if not all(
            (candidate_ref, hypothesis_id, defect_fingerprint, seed_binding_identity)
        ):
            raise ValueError("confirmation queue entry requires exact semantic identity")
        node = self.graph.nodes.get(candidate_ref)
        if node is None or not root_candidate_eligible(node):
            return False
        queue_key = (
            hypothesis_id,
            candidate_ref,
            defect_fingerprint,
            seed_binding_identity,
        )
        if queue_key in self.confirmation_queue_keys:
            return False
        same_seed_candidates = {
            str(item.get("candidate_ref") or "")
            for item in self.confirmation_queue
            if str(item.get("seed_binding_identity") or "")
            == seed_binding_identity
        }
        if candidate_ref in same_seed_candidates:
            return False
        if len(same_seed_candidates) >= MAX_ROOT_CONFIRMATION_CANDIDATES:
            return False
        self.confirmation_queue_keys.add(queue_key)
        self.confirmation_queue.append(copy.deepcopy(dict(value)))
        return True

    def validate_confirmation_queue_bound(self) -> None:
        candidates_by_seed: Dict[str, Set[str]] = {}
        for item in self.confirmation_queue:
            seed_binding_identity = str(
                item.get("seed_binding_identity") or ""
            )
            candidate_ref = str(item.get("candidate_ref") or "")
            if not seed_binding_identity or not candidate_ref:
                raise ValueError("restored confirmation queue identity is incomplete")
            candidate = self.graph.nodes.get(candidate_ref)
            if candidate is None or not root_candidate_eligible(candidate):
                raise ValueError(
                    "restored confirmation queue contains an ineligible root candidate"
                )
            candidates = candidates_by_seed.setdefault(seed_binding_identity, set())
            candidates.add(candidate_ref)
            if len(candidates) > MAX_ROOT_CONFIRMATION_CANDIDATES:
                raise ValueError(
                    "restored confirmation queue exceeds three candidates per seed"
                )

    def _seed_builder_for_item(
        self, item: FrontierItem
    ) -> Optional[SeedAttributionBuilder]:
        key = self.hypothesis_seed_keys.get(item.hypothesis_id)
        if key is not None:
            return self.seed_ledger.get(key)
        start_ref = item.downstream_path[-1] if item.downstream_path else item.node_ref
        candidates = [
            builder
            for builder in self.seed_ledger.values()
            if builder.start_ref == start_ref
            and any(
                state.fingerprint == builder.defect_state.fingerprint
                for state in self.transformation_chains.get(
                    item.defect_state.fingerprint, (item.defect_state,)
                )
            )
        ]
        if len(candidates) != 1:
            return None
        self.hypothesis_seed_keys[item.hypothesis_id] = candidates[0].key
        return candidates[0]

    def _bind_hypothesis_to_seed(
        self, hypothesis_id: str, builder: Optional[SeedAttributionBuilder]
    ) -> None:
        if builder is not None:
            self.hypothesis_seed_keys[hypothesis_id] = builder.key

    def seed_results(self) -> Tuple[SeedAttributionResult, ...]:
        return tuple(
            self.seed_ledger[key].to_result() for key in sorted(self.seed_ledger)
        )

    @classmethod
    def create(
        cls,
        *,
        graph: TraceGraph,
        start_refs: Iterable[str],
        objective: str,
        analysis_perspective: str,
        max_hypotheses: int = 24,
    ) -> "RecursiveAnalysisState":
        resolved_starts = _dedupe_strings(graph.resolve(ref) or ref for ref in start_refs)
        state = cls(
            graph=graph,
            start_refs=resolved_starts,
            objective=objective,
            analysis_perspective=analysis_perspective,
        )
        for start_ref in resolved_starts:
            node = graph.nodes.get(start_ref)
            if node is None:
                defect_state = DefectState.create(
                    label="unresolved_attribution_seed",
                    expected=objective,
                    actual="The start reference is absent.",
                    mechanism="No trace node can ground this attribution seed.",
                    scope="attribution_seed:{0}".format(start_ref),
                )
                builder = state._ensure_seed(start_ref, defect_state)
                state._remember_defect(defect_state)
                state._mark_seed_unresolved(
                    start_ref,
                    "start_ref_unresolved",
                    "The start reference is absent.",
                    seed_key=builder.key,
                )
                continue
            defect_state = _seed_defect_state(node, objective)
            builder = state._ensure_seed(start_ref, defect_state)
            state._remember_defect(defect_state)
            if not graph.analysis_start_eligible(start_ref):
                state._mark_seed_unresolved(
                    start_ref,
                    "start_ref_ineligible",
                    "The external evaluation fact is ineligible for decisive judgment.",
                    seed_key=builder.key,
                )
                continue
            if node.event_type in EVALUATION_START_EVENTS:
                predecessors = [
                    ref
                    for ref in graph.upstream_refs(start_ref)
                    if graph.nodes.get(ref)
                    and graph.evidence_eligible(ref)
                ]
                manifest = (
                    graph.raw_trace.get("manifest")
                    if isinstance(graph.raw_trace.get("manifest"), Mapping)
                    else {}
                )
                interrupted_failure = node.event_type == "case.failed" and (
                    node.data.get("shutdown_disposition")
                    == "interrupted_before_case_completion"
                    or manifest.get("shutdown_disposition")
                    == "interrupted_before_case_completion"
                )
                if interrupted_failure:
                    signal_predecessors = [
                        ref
                        for ref in predecessors
                        if graph.nodes.get(ref)
                        and graph.nodes[ref].event_type == "process.signal"
                    ]
                    if not signal_predecessors:
                        state._mark_seed_unresolved(
                            start_ref,
                            "process_signal_node_missing",
                            "The interrupted case records a shutdown signal but has no distinct process.signal causal node.",
                            seed_key=builder.key,
                        )
                        continue
                    predecessors = signal_predecessors
                if not predecessors:
                    state._mark_seed_unresolved(
                        start_ref,
                        "outcome_evidence_missing",
                        "The evaluation assertion has no concrete outcome evidence.",
                        seed_key=builder.key,
                    )
                    continue
                progress_predecessors = [
                    ref
                    for ref in predecessors
                    if graph.nodes.get(ref)
                    and graph.nodes[ref].event_type == "progress.episode"
                ]
                traversal_predecessors = set(predecessors)
                if progress_predecessors:
                    traversal_predecessors = {
                        max(progress_predecessors, key=graph.position)
                    }
                for predecessor_ref in predecessors:
                    state.causal_relations.append(
                        PredecessorAssessment(
                            ref=predecessor_ref,
                            relation="outcome_evidence",
                            reason="The evaluation assertion cites this record as observed outcome evidence.",
                            confidence=1.0,
                            recurse=False,
                            evidence_refs=(start_ref, predecessor_ref),
                        )
                    )
                    candidate = state._candidate_for_ref(
                        predecessor_ref,
                        source="outcome_evidence",
                        edge={
                            "from_ref": predecessor_ref,
                            "to_ref": start_ref,
                            "relation": "outcome_evidence",
                            "evidence_type": "recorded_evaluation",
                            "eligible_for_attribution": True,
                        },
                        evidence_refs=(start_ref, predecessor_ref),
                    )
                    if candidate is not None:
                        state._remember_candidate(candidate)
                    if predecessor_ref not in traversal_predecessors:
                        continue
                    if not graph.analysis_start_eligible(predecessor_ref):
                        continue
                    if len(state.ledger.snapshot()) >= max_hypotheses:
                        state._increment_budget("hypotheses")
                        state._mark_seed_unresolved(
                            predecessor_ref,
                            "hypothesis_limit",
                            "The seed hypothesis budget is exhausted.",
                            seed_key=builder.key,
                        )
                        continue
                    hypothesis = state.ledger.create(
                        "{0} is upstream outcome evidence for {1}.".format(
                            predecessor_ref, start_ref
                        ),
                        predecessor_ref,
                        defect_state,
                        seed_binding_identity=builder.key,
                    )
                    state._bind_hypothesis_to_seed(
                        hypothesis.hypothesis_id, builder
                    )
                    item = FrontierItem.create(
                        node_ref=predecessor_ref,
                        defect_state=defect_state,
                        downstream_path=[predecessor_ref, start_ref],
                        hypothesis_id=hypothesis.hypothesis_id,
                        hypothesis_semantic_hash=hypothesis.semantic_hash,
                        seed_binding_identity=hypothesis.seed_binding_identity,
                        candidate_source="outcome_evidence",
                        priority=1.0,
                        checked_evidence_refs=[start_ref],
                        graph_position=graph.position(predecessor_ref),
                    )
                    state.frontier.push(item)
                    state._merge_visit_evidence(item.visit_key, [start_ref])
                continue
            if len(state.ledger.snapshot()) >= max_hypotheses:
                state._increment_budget("hypotheses")
                state._mark_seed_unresolved(
                    start_ref,
                    "hypothesis_limit",
                    "The seed hypothesis budget is exhausted.",
                    seed_key=builder.key,
                )
                continue
            hypothesis = state.ledger.create(
                "Investigate the observed defect at {0}.".format(start_ref),
                start_ref,
                defect_state,
                seed_binding_identity=builder.key,
            )
            state._bind_hypothesis_to_seed(hypothesis.hypothesis_id, builder)
            item = FrontierItem.create(
                node_ref=start_ref,
                defect_state=defect_state,
                downstream_path=[start_ref],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=hypothesis.seed_binding_identity,
                candidate_source="analysis_start",
                priority=1.0,
                checked_evidence_refs=[start_ref],
                graph_position=graph.position(start_ref),
            )
            state.frontier.push(item)
            state._merge_visit_evidence(item.visit_key, [start_ref])
        return state

    def frontier_checkpoint_payload(self) -> JsonDict:
        return {
            "schema": FRONTIER_STATE_SCHEMA,
            "frontier": self.frontier.checkpoint(),
            "visit_evidence": {
                key: sorted(values) for key, values in sorted(self.visit_evidence.items())
            },
        }

    def hypothesis_checkpoint_payload(self) -> JsonDict:
        return {
            "schema": HYPOTHESIS_STATE_SCHEMA,
            "hypotheses": self.ledger.snapshot(),
            "defect_states": [
                item.to_dict() for _, item in sorted(self.defect_states.items())
            ],
            "transformation_chains": {
                key: [item.to_dict() for item in chain]
                for key, chain in sorted(self.transformation_chains.items())
            },
        }

    def action_checkpoint_payload(self) -> JsonDict:
        return {
            "schema": ACTION_STATE_SCHEMA,
            "start_refs": list(self.start_refs),
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "causal_candidates": [item.to_dict() for item in self.causal_candidates],
            "causal_relations": [item.to_dict() for item in self.causal_relations],
            "step_judgments": [item.to_dict() for item in self.step_judgments],
            "introduction_candidates": [item.to_dict() for item in self.introduction_candidates],
            "introduction_bindings": _checkpoint_json(self.introduction_bindings),
            "contributing_conditions": [item.to_dict() for item in self.contributing_conditions],
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "taint_paths": [list(item) for item in self.taint_paths],
            "visited_order": list(self.visited_order),
            "unresolved_branches": _checkpoint_json(self.unresolved_branches),
            "unresolved_refs": list(self.unresolved_refs),
            "unresolved_hypothesis_ids": sorted(self.unresolved_hypothesis_ids),
            "introduction_hypothesis_ids": sorted(self.introduction_hypothesis_ids),
            "present_hypothesis_ids": sorted(self.present_hypothesis_ids),
            "exhausted_budgets": dict(sorted(self.exhausted_budgets.items())),
            "artifact_identities": sorted(self.artifact_identities),
            "artifact_bytes": self.artifact_bytes,
            "processed_items": self.processed_items,
            "judge_requests": self.judge_requests,
            "judge_request_uncertainty_count": self.judge_request_uncertainty_count,
            "logical_judge_calls": self.logical_judge_calls,
            "investigation_rounds": self.investigation_rounds,
            "investigation_result_bytes": self.investigation_result_bytes,
            "investigation_journal": _checkpoint_json(self.investigation_journal),
            "investigation_evidence": _checkpoint_json(self.investigation_evidence),
            "investigation_evidence_hashes": {
                key: sorted(values)
                for key, values in sorted(self.investigation_evidence_hashes.items())
            },
            "control_directive_ids": sorted(self.control_directive_ids),
            "confirmation_queue": _checkpoint_json(self.confirmation_queue),
            "confirmation_queue_keys": [list(item) for item in sorted(self.confirmation_queue_keys)],
            "confirmations": [item.to_dict() for item in self.confirmations],
            "confirmed_roots": [item.to_dict() for item in self.confirmed_roots],
            "co_roots": [item.to_dict() for item in self.co_roots],
            "amplifying_factors": [item.to_dict() for item in self.amplifying_factors],
            "confirmation_journal": _checkpoint_json(self.confirmation_journal),
            "logical_confirmation_calls": self.logical_confirmation_calls,
            "pending_rejudge_journal": _checkpoint_json(self.pending_rejudge_journal),
            "seed_count": self.seed_count,
            "seed_ledger": [
                self.seed_ledger[key].to_dict() for key in sorted(self.seed_ledger)
            ],
            "hypothesis_seed_keys": dict(sorted(self.hypothesis_seed_keys.items())),
            "provider_state": _checkpoint_json(self.provider_state),
        }

    @classmethod
    def from_checkpoint(
        cls,
        *,
        graph: TraceGraph,
        checkpoint: CheckpointState,
    ) -> "RecursiveAnalysisState":
        frontier_payload = checkpoint.frontier_payload
        hypothesis_payload = checkpoint.hypothesis_payload
        action_record = next(
            (
                item
                for item in reversed(checkpoint.actions)
                if item.get("operation") == "state_snapshot"
            ),
            None,
        )
        if not frontier_payload or not hypothesis_payload or action_record is None:
            raise ValueError("checkpoint does not contain a complete recursive state snapshot")
        snapshot_records = (
            checkpoint.frontier_records[-1],
            checkpoint.hypothesis_records[-1],
            action_record,
        )
        if len(
            {
                (
                    item.get("transaction_sequence"),
                    item.get("semantic_key"),
                )
                for item in snapshot_records
            }
        ) != 1:
            raise ValueError(
                "checkpoint recursive state members do not share one committed transaction"
            )
        action_payload = dict(action_record["payload"])
        _require_exact_checkpoint_keys(
            frontier_payload,
            {"schema", "frontier", "visit_evidence"},
            "frontier state",
        )
        _require_exact_checkpoint_keys(
            hypothesis_payload,
            {"schema", "hypotheses", "defect_states", "transformation_chains"},
            "hypothesis state",
        )
        action_keys = set(cls(graph=graph, start_refs=(), objective="", analysis_perspective="").action_checkpoint_payload())
        _require_exact_checkpoint_keys(action_payload, action_keys, "action state")
        if frontier_payload["schema"] not in {
            LEGACY_FRONTIER_STATE_SCHEMA,
            FRONTIER_STATE_SCHEMA,
        }:
            raise ValueError("unsupported recursive frontier state schema")
        if hypothesis_payload["schema"] != HYPOTHESIS_STATE_SCHEMA:
            raise ValueError("unsupported recursive hypothesis state schema")
        if action_payload["schema"] != ACTION_STATE_SCHEMA:
            raise ValueError("unsupported recursive action state schema")
        graph.assert_evidence_eligible_references(
            frontier_payload,
            label="restored recursive frontier state",
        )
        graph.assert_evidence_eligible_references(
            hypothesis_payload,
            label="restored recursive hypothesis state",
        )
        graph.assert_evidence_eligible_references(
            action_payload,
            label="restored recursive action state",
        )

        ledger = HypothesisLedger.from_snapshot(hypothesis_payload["hypotheses"])
        frontier = RecursiveFrontier.from_checkpoint(
            frontier_payload["frontier"],
            hypotheses_by_id=ledger.hypotheses_by_id(),
        )
        if frontier.has_legacy_visit_key_migrations():
            visit_evidence_payload = _migrate_checkpoint_visit_references(
                frontier_payload["visit_evidence"], frontier
            )
            action_payload = _normalize_migrated_context_hashes(
                _migrate_checkpoint_visit_references(action_payload, frontier)
            )
        else:
            visit_evidence_payload = copy.deepcopy(frontier_payload["visit_evidence"])
        state = cls(
            graph=graph,
            start_refs=tuple(str(item) for item in action_payload["start_refs"]),
            objective=str(action_payload["objective"]),
            analysis_perspective=str(action_payload["analysis_perspective"]),
            ledger=ledger,
            frontier=frontier,
        )
        state.visit_evidence = {
            str(key): {str(item) for item in values}
            for key, values in dict(visit_evidence_payload).items()
        }
        state.defect_states = {
            item.fingerprint: item
            for item in (
                DefectState.from_dict(value) for value in hypothesis_payload["defect_states"]
            )
        }
        state.transformation_chains = {
            str(key): tuple(DefectState.from_dict(item) for item in values)
            for key, values in dict(hypothesis_payload["transformation_chains"]).items()
        }
        state.causal_candidates = [
            candidate
            for candidate in (
                CausalCandidate.from_dict(item)
                for item in action_payload["causal_candidates"]
            )
            if graph.nodes.get(candidate.ref)
            and graph.evidence_eligible(candidate.ref)
        ]
        state.causal_relations = [PredecessorAssessment.from_dict(item) for item in action_payload["causal_relations"]]
        state.step_judgments = [CausalStepJudgment.from_dict(item) for item in action_payload["step_judgments"]]
        state.introduction_candidates = [
            candidate
            for candidate in (
                CausalCandidate.from_dict(item)
                for item in action_payload["introduction_candidates"]
            )
            if graph.nodes.get(candidate.ref)
            and root_candidate_eligible(graph.nodes[candidate.ref])
        ]
        state.introduction_bindings = copy.deepcopy(action_payload["introduction_bindings"])
        state.introduction_binding_keys = {
            (
                str(item.get("candidate_ref") or ""),
                str(item.get("defect_fingerprint") or ""),
                str(item.get("hypothesis_semantic_hash") or ""),
                str(item.get("seed_binding_identity") or ""),
            )
            for item in state.introduction_bindings
        }
        state.contributing_conditions = [CausalFactor.from_dict(item) for item in action_payload["contributing_conditions"]]
        state.rejected_candidates = [RejectedCandidate.from_dict(item) for item in action_payload["rejected_candidates"]]
        state.taint_paths = [tuple(str(ref) for ref in item) for item in action_payload["taint_paths"]]
        state.visited_order = [str(item) for item in action_payload["visited_order"]]
        state.unresolved_branches = copy.deepcopy(action_payload["unresolved_branches"])
        state.unresolved_refs = [str(item) for item in action_payload["unresolved_refs"]]
        state.unresolved_hypothesis_ids = {str(item) for item in action_payload["unresolved_hypothesis_ids"]}
        state.introduction_hypothesis_ids = {str(item) for item in action_payload["introduction_hypothesis_ids"]}
        state.present_hypothesis_ids = {str(item) for item in action_payload["present_hypothesis_ids"]}
        state.exhausted_budgets = {str(key): int(value) for key, value in dict(action_payload["exhausted_budgets"]).items()}
        state.artifact_identities = {str(item) for item in action_payload["artifact_identities"]}
        for name in (
            "artifact_bytes",
            "processed_items",
            "judge_requests",
            "judge_request_uncertainty_count",
            "logical_judge_calls",
            "investigation_rounds",
            "investigation_result_bytes",
            "logical_confirmation_calls",
            "seed_count",
        ):
            value = action_payload[name]
            if type(value) is not int or value < 0:
                raise ValueError("{0} checkpoint counter is invalid".format(name))
            setattr(state, name, value)
        requeued_inflight = len(frontier_payload["frontier"]["in_flight"])
        if requeued_inflight > state.processed_items:
            raise ValueError("checkpoint in-flight frontier count exceeds processed items")
        state.processed_items -= requeued_inflight
        state.investigation_journal = _migrate_checkpoint_visit_references(
            action_payload["investigation_journal"], frontier
        )
        state.investigation_evidence = {
            frontier.migrated_visit_key(str(key)): copy.deepcopy(value)
            for key, value in dict(action_payload["investigation_evidence"]).items()
        }
        state.investigation_evidence_hashes = {
            frontier.migrated_visit_key(str(key)): {str(item) for item in values}
            for key, values in dict(action_payload["investigation_evidence_hashes"]).items()
        }
        state.control_directive_ids = {str(item) for item in action_payload["control_directive_ids"]}
        state.confirmation_queue = copy.deepcopy(action_payload["confirmation_queue"])
        state.confirmation_queue_keys = {
            tuple(str(part) for part in item) for item in action_payload["confirmation_queue_keys"]
        }
        state.validate_confirmation_queue_bound()
        state.confirmations = [RootConfirmation.from_dict(item) for item in action_payload["confirmations"]]
        state.confirmed_roots = [ConfirmedRoot.from_dict(item) for item in action_payload["confirmed_roots"]]
        state.co_roots = [ConfirmedRoot.from_dict(item) for item in action_payload["co_roots"]]
        state.amplifying_factors = [CausalFactor.from_dict(item) for item in action_payload["amplifying_factors"]]
        state.confirmation_journal = copy.deepcopy(action_payload["confirmation_journal"])
        state.pending_rejudge_journal = {
            frontier.migrated_visit_key(str(key)): [int(item) for item in values]
            for key, values in dict(action_payload["pending_rejudge_journal"]).items()
        }
        state.seed_ledger = {
            builder.key: builder
            for builder in (
                SeedAttributionBuilder.from_dict(item)
                for item in action_payload["seed_ledger"]
            )
        }
        for builder in state.seed_ledger.values():
            builder.confirmed_root_confirmation_identities.update(
                confirmation.confirmation_identity
                for confirmation in state.confirmations
                if confirmation.status == "confirmed"
                and confirmation.seed_binding_identity == builder.key
            )
        validate_confirmation_ownership(
            state.confirmations,
            state.seed_results(),
            label="restored recursive state",
        )
        for builder in state.seed_ledger.values():
            judgment = builder.global_judgment
            if judgment:
                global_candidate_request_from_validation_envelope(
                    judgment.get("validation_envelope"), graph=graph
                )
        if len(state.seed_ledger) != len(action_payload["seed_ledger"]):
            raise ValueError("checkpoint contains duplicate per-seed attribution identity")
        state.hypothesis_seed_keys = {
            str(key): str(value)
            for key, value in dict(action_payload["hypothesis_seed_keys"]).items()
        }
        restored_hypotheses = state.ledger.hypotheses_by_id()
        if any(
            hypothesis_id not in restored_hypotheses
            for hypothesis_id in state.hypothesis_seed_keys
        ):
            raise ValueError(
                "checkpoint hypothesis_seed_keys contains an unknown restored hypothesis"
            )
        if any(
            seed_key not in state.seed_ledger
            or restored_hypotheses[hypothesis_id].seed_binding_identity != seed_key
            for hypothesis_id, seed_key in state.hypothesis_seed_keys.items()
        ):
            raise ValueError(
                "checkpoint hypothesis_seed_keys must match restored hypothesis seed bindings"
            )
        frontier_hypothesis_ids = {
            item.hypothesis_id for item in state.frontier.lifecycle_items()
        }
        if not frontier_hypothesis_ids.issubset(state.hypothesis_seed_keys):
            raise ValueError(
                "checkpoint hypothesis_seed_keys is missing frontier hypothesis routing"
            )
        if state.seed_count != len(state.seed_ledger):
            raise ValueError("checkpoint seed_count contradicts seed ledger")
        state.provider_state = _validate_provider_state(
            action_payload["provider_state"],
            state,
            cache_identity=str(checkpoint.config["cache_identity"]),
        )
        transient_signal_refs = {
            str(item.get("node_ref") or "")
            for item in state.unresolved_branches
            if item.get("reason") == "analysis_interrupted"
        }
        transient_signal_details = {
            str(item.get("details") or "")
            for item in state.unresolved_branches
            if item.get("reason") == "analysis_interrupted"
        }
        for builder in state.seed_ledger.values():
            builder.blocking_reasons.discard("analysis_interrupted")
            builder.missing_evidence.difference_update(transient_signal_details)
        state.unresolved_branches = [
            item
            for item in state.unresolved_branches
            if item.get("reason") != "analysis_interrupted"
        ]
        retained_unresolved_refs = {
            str(item.get("node_ref") or "") for item in state.unresolved_branches
        }
        state.unresolved_refs = [
            ref
            for ref in state.unresolved_refs
            if ref not in transient_signal_refs or ref in retained_unresolved_refs
        ]
        if frontier.has_legacy_visit_key_migrations():
            state.replay_actions = _migrate_checkpoint_journal_records(
                checkpoint.actions, frontier
            )
        else:
            state.replay_actions = copy.deepcopy(checkpoint.latest_actions)
        return state

    def build_step_request(
        self,
        graph: TraceGraph,
        item: FrontierItem,
        candidates: Sequence[CausalCandidate],
        retrieved_candidates: Optional[Sequence[CausalCandidate]] = None,
    ) -> CausalStepRequest:
        candidates = tuple(
            candidate
            for candidate in candidates
            if graph.evidence_eligible(candidate.ref)
        )
        retrieved_candidates = tuple(
            candidate
            for candidate in (retrieved_candidates or candidates)
            if graph.evidence_eligible(candidate.ref)
        )
        hypothesis = self.ledger.get(item.hypothesis_id)
        chain = self.transformation_chains.get(item.defect_state.fingerprint, (item.defect_state,))
        downstream_judgments = [
            judgment
            for judgment in self.step_judgments
            if judgment.current_node_ref in item.downstream_path
        ]
        context = build_recursive_judgment_context(
            graph=graph,
            node_ref=item.node_ref,
            defect_state=item.defect_state,
            hypothesis=hypothesis,
            candidates=candidates,
            downstream_path=list(item.downstream_path),
            downstream_judgments=downstream_judgments,
            defect_transformation_chain=list(chain),
            objective=self.objective,
        )
        context["objective"] = self.objective
        context["analysis_perspective"] = self.analysis_perspective
        context["active_hypothesis_id"] = hypothesis.hypothesis_id
        context["active_visit_key"] = item.visit_key
        context["checked_evidence_refs"] = sorted(self.visit_evidence.get(item.visit_key, set()))
        retrieved = retrieved_candidates
        offered_refs = {candidate.ref for candidate in candidates}
        context["candidate_pagination"] = {
            "retrieved_count": len(retrieved),
            "offered_count": len(candidates),
            "offered_candidate_refs": [candidate.ref for candidate in candidates],
            "omitted_candidate_refs": [
                candidate.ref
                for candidate in retrieved
                if candidate.ref not in offered_refs
            ],
            "has_more": len(retrieved) > len(candidates),
            "page_size": CAUSAL_STEP_CANDIDATE_LIMIT,
            "selection_method": "structural_provenance_ranked_shortlist_v1",
        }
        investigated = self.investigation_evidence.get(item.visit_key, [])
        if investigated:
            context["investigation_evidence"] = copy.deepcopy(investigated)
        context = sanitize_judge_evidence_payload(graph, context)
        context["evidence_hash"] = hashlib.sha256(
            stable_json(context).encode("utf-8")
        ).hexdigest()
        context_snapshot = copy.deepcopy(context)
        context_hash = hashlib.sha256(
            stable_json(context_snapshot).encode("utf-8")
        ).hexdigest()
        for index in self.pending_rejudge_journal.get(item.visit_key, []):
            entry = self.investigation_journal[index]
            entry["context_after"] = context_snapshot
            entry["context_after_hash"] = context_hash
            entry["rejudge_linkage"] = {
                **entry["rejudge_linkage"],
                "status": "pending_judge",
                "rejudge_visit_key": item.visit_key,
                "context_after_hash": context_hash,
            }
        return CausalStepRequest(
            recursive_context=context,
            current_node=graph.hydrate_node(item.node_ref),
            defect_state=item.defect_state,
            candidates=candidates,
        )

    def record_investigation_result(
        self,
        item: FrontierItem,
        directive: InvestigationDirective,
        result: InvestigationResult,
        judgment: CausalStepJudgment,
        request: CausalStepRequest,
    ) -> bool:
        context_before = copy.deepcopy(request.to_dict()["recursive_context"])
        context_before_hash = hashlib.sha256(
            stable_json(context_before).encode("utf-8")
        ).hexdigest()
        seen = self.investigation_evidence_hashes.setdefault(item.visit_key, set())
        changed = result.status == "success" and result.evidence_hash not in seen
        linkage_status = "scheduled" if changed else "not_scheduled"
        journal = {
            **directive.to_dict(),
            **result.to_dict(),
            "active_visit": self._active_visit_snapshot(item),
            "judgment_before_investigation": judgment.to_dict(),
            "context_before": context_before,
            "context_before_hash": context_before_hash,
            "result": result.to_dict(),
            "context_after": None if changed else context_before,
            "context_after_hash": "" if changed else context_before_hash,
            "rejudge_linkage": {
                "status": linkage_status,
                "source_visit_key": item.visit_key,
                "source_context_hash": context_before_hash,
                "evidence_hash": result.evidence_hash,
            },
        }
        self.investigation_journal.append(journal)
        if not changed:
            return False
        seen.add(result.evidence_hash)
        self.investigation_evidence.setdefault(item.visit_key, []).append(result.to_dict())
        self._merge_visit_evidence(item.visit_key, result.resolved_refs)
        self.pending_rejudge_journal.setdefault(item.visit_key, []).append(
            len(self.investigation_journal) - 1
        )
        return True

    def record_terminal_action(
        self,
        *,
        item: FrontierItem,
        judgment: CausalStepJudgment,
        request: CausalStepRequest,
        directive: Mapping[str, Any],
        status: str,
        rejection_reason: str,
        result: Optional[Mapping[str, Any]] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        context_before = copy.deepcopy(request.to_dict()["recursive_context"])
        context_hash = hashlib.sha256(
            stable_json(context_before).encode("utf-8")
        ).hexdigest()
        terminal_result = dict(
            result
            or {
                "status": status,
                "rejection_reason": rejection_reason,
                "evidence_hash": hashlib.sha256(
                    stable_json(
                        {"status": status, "rejection_reason": rejection_reason}
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        self.investigation_journal.append(
            {
                **dict(directive),
                "status": status,
                "rejection_reason": rejection_reason,
                "active_visit": self._active_visit_snapshot(item),
                "judgment_before_investigation": judgment.to_dict(),
                "context_before": context_before,
                "context_before_hash": context_hash,
                "result": terminal_result,
                "context_after": context_before,
                "context_after_hash": context_hash,
                "rejudge_linkage": {
                    "status": "not_scheduled",
                    "source_visit_key": item.visit_key,
                    "source_context_hash": context_hash,
                },
                **dict(extra or {}),
            }
        )

    @staticmethod
    def _active_visit_snapshot(item: FrontierItem) -> JsonDict:
        return {
            "visit_key": item.visit_key,
            "node_ref": item.node_ref,
            "hypothesis_id": item.hypothesis_id,
            "defect_state_id": item.defect_state.defect_state_id,
            "defect_fingerprint": item.defect_state.fingerprint,
            "depth": item.depth,
        }

    def record_intermediate_judgment(
        self, item: FrontierItem, judgment: CausalStepJudgment
    ) -> str:
        return hashlib.sha256(
            stable_json(judgment.to_dict()).encode("utf-8")
        ).hexdigest()

    def complete_rejudge(
        self,
        item: FrontierItem,
        *,
        terminal_state: str,
        judgment: Optional[CausalStepJudgment] = None,
        detail: str = "",
        physical_request_delta: Optional[int] = None,
    ) -> None:
        judgment_after = judgment.to_dict() if judgment is not None else None
        judgment_hash = (
            hashlib.sha256(stable_json(judgment_after).encode("utf-8")).hexdigest()
            if judgment_after is not None
            else ""
        )
        for index in self.pending_rejudge_journal.pop(item.visit_key, []):
            entry = self.investigation_journal[index]
            if entry.get("context_after") is None:
                entry["context_after"] = entry["context_before"]
                entry["context_after_hash"] = entry["context_before_hash"]
            entry["judgment_after_investigation"] = judgment_after
            entry["judgment_after_hash"] = judgment_hash
            entry["rejudge_linkage"] = {
                **entry["rejudge_linkage"],
                "status": "completed",
                "terminal_state": terminal_state,
                "detail": str(detail or ""),
                "physical_request_delta": physical_request_delta,
                "rejudge_visit_key": item.visit_key,
                "context_after_hash": entry["context_after_hash"],
                "judgment_after_hash": judgment_hash,
            }

    def finalize_pending_rejudges(self) -> None:
        pending = list(self.pending_rejudge_journal)
        for visit_key in pending:
            indexes = self.pending_rejudge_journal.pop(visit_key, [])
            for index in indexes:
                entry = self.investigation_journal[index]
                entry["context_after"] = entry["context_before"]
                entry["context_after_hash"] = entry["context_before_hash"]
                entry["judgment_after_investigation"] = None
                entry["judgment_after_hash"] = ""
                entry["rejudge_linkage"] = {
                    **entry["rejudge_linkage"],
                    "status": "completed",
                    "terminal_state": "traversal_terminated_before_rejudge",
                    "detail": "recursive traversal terminated before re-judgment",
                    "physical_request_delta": None,
                    "context_after_hash": entry["context_before_hash"],
                    "judgment_after_hash": "",
                }

    def reserve_artifact_bytes(self, request: CausalStepRequest, limit: int) -> bool:
        new_payloads: Dict[str, bytes] = {}
        for identity, content in _artifact_payloads(request.to_dict()):
            if identity not in self.artifact_identities:
                new_payloads.setdefault(identity, content)
        new_bytes = sum(len(content) for content in new_payloads.values())
        if self.artifact_bytes + new_bytes > limit:
            return False
        self.artifact_identities.update(new_payloads)
        self.artifact_bytes += new_bytes
        return True

    def apply_step(
        self,
        item: FrontierItem,
        judgment: CausalStepJudgment,
        *,
        graph_position: Any,
        max_hypotheses: int,
    ) -> None:
        hypothesis = self.ledger.get(item.hypothesis_id)
        seed_builder = self._seed_builder_for_item(item)
        if hypothesis.status in {"rejected", "superseded"}:
            self._mark_ref_unresolved(
                item.node_ref,
                item,
                "inactive_hypothesis_result_discarded",
                "A stale Judge result cannot update a rejected or superseded hypothesis.",
            )
            self.frontier.complete_if_in_flight(
                item, "discarded:{0}".format(hypothesis.status)
            )
            return
        self.step_judgments.append(judgment)
        self.visited_order.append(item.node_ref)
        self.taint_paths.append(tuple(item.downstream_path))
        evidence_hash = hashlib.sha256(
            stable_json(judgment.to_dict()).encode("utf-8")
        ).hexdigest()
        is_present = judgment.current_defect_status == "present"
        current_node = self.graph.nodes.get(item.node_ref)
        is_navigation = bool(current_node and is_navigation_node(current_node))
        if is_present:
            self.present_hypothesis_ids.add(item.hypothesis_id)
        elif (
            judgment.current_defect_status == "absent"
            and seed_builder is not None
        ):
            seed_builder.mark_no_defect()

        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            details = "; ".join(judgment.missing_evidence) or judgment.current_defect_reason
            self.mark_unresolved(item, "judge_unknown", details)
        elif is_present and judgment.candidate_introduction:
            node = self.graph.nodes.get(item.node_ref)
            if node is not None and not root_candidate_eligible(node):
                if not is_evidence_only_node(node):
                    self.mark_unresolved(
                        item,
                        "root_candidate_ineligible",
                        "Navigation, outcome, lifecycle-start, and context-packaging aggregates cannot introduce a reportable defect root.",
                    )
            elif hypothesis.candidate_root_ref != item.node_ref:
                self.mark_unresolved(
                    item,
                    "introduction_hypothesis_mismatch",
                    "The introduction candidate is not bound to the active hypothesis root.",
                )
            else:
                binding_key = (
                    item.node_ref,
                    item.defect_state.fingerprint,
                    hypothesis.semantic_hash,
                    hypothesis.seed_binding_identity,
                )
                if binding_key not in self.introduction_binding_keys:
                    candidate = self._candidate_for_ref(
                        item.node_ref,
                        source="judge_introduction_candidate",
                        edge={
                            "relation": "introduction_candidate",
                            "evidence_type": "judge_assessment",
                            "eligible_for_attribution": False,
                        },
                        evidence_refs=tuple(self.visit_evidence.get(item.visit_key, set())),
                    )
                    if candidate is not None:
                        if seed_builder is not None:
                            seed_builder.candidate_refs.add(candidate.ref)
                        self.introduction_candidates.append(candidate)
                        self._remember_candidate(candidate)
                        self.introduction_bindings.append(
                            {
                                "candidate_ref": item.node_ref,
                                "defect_state_id": item.defect_state.defect_state_id,
                                "defect_fingerprint": item.defect_state.fingerprint,
                                "hypothesis_id": hypothesis.hypothesis_id,
                                "hypothesis_semantic_hash": hypothesis.semantic_hash,
                                "seed_binding_identity": hypothesis.seed_binding_identity,
                                "seed_key": seed_builder.key if seed_builder else "",
                            }
                        )
                        self.introduction_binding_keys.add(binding_key)
                self.introduction_hypothesis_ids.add(item.hypothesis_id)

        declared_recursive = False
        for assessment in judgment.predecessors:
            if not (
                judgment.current_defect_status == "absent"
                and assessment.relation == "unknown"
            ):
                self.causal_relations.append(assessment)
            if assessment.relation == "contributing_condition" and not assessment.recurse:
                continue
            if assessment.relation in {"unrelated", "unknown"}:
                if assessment.relation == "unrelated":
                    self.ledger.add_opposition(
                        item.hypothesis_id,
                        assessment.ref,
                        assessment.reason,
                        assessment.confidence,
                    )
                elif is_present:
                    self._mark_ref_unresolved(
                        assessment.ref,
                        item,
                        "predecessor_unknown",
                        "; ".join(assessment.missing_evidence) or assessment.reason,
                    )
                continue
            if assessment.relation not in RECURSIVE_RELATIONS or not assessment.recurse:
                continue
            declared_recursive = True
            if assessment.ref not in self.graph.nodes:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_ref_unresolved",
                    "The recursive predecessor is not present in the trace graph.",
                )
                continue
            if not self.graph.analysis_start_eligible(assessment.ref):
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_ineligible_for_analysis",
                    "The external evaluation fact may be retained as evidence but cannot enter recursive analysis.",
                )
                continue
            if assessment.missing_evidence or not assessment.evidence_refs:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_evidence_missing",
                    "; ".join(assessment.missing_evidence)
                    or "The recursive predecessor has no grounded supporting evidence.",
                )
                continue
            if judgment.current_defect_status != "present" and not is_navigation:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "unsupported_propagation",
                    "A predecessor cannot carry a defect when the current defect is not present.",
                )
                continue

            upstream_defect = item.defect_state
            if assessment.relation in {
                "defect_transformation",
                "contributing_condition",
            }:
                if assessment.upstream_defect is None:
                    self._mark_ref_unresolved(
                        assessment.ref,
                        item,
                        "missing_transformed_defect",
                        "The transformation does not define an upstream defect state.",
                    )
                    continue
                upstream_defect = assessment.upstream_defect
                self._remember_defect(upstream_defect)
                downstream_chain = self.transformation_chains.get(
                    item.defect_state.fingerprint, (item.defect_state,)
                )
                self.transformation_chains[upstream_defect.fingerprint] = (
                    upstream_defect,
                    *downstream_chain,
                )
            claim = "{0} from {1} explains {2} for defect {3}: {4}".format(
                assessment.relation,
                assessment.ref,
                item.node_ref,
                upstream_defect.label,
                " ".join(assessment.reason.split()),
            )
            proposed = AttributionHypothesis.create(
                claim,
                assessment.ref,
                upstream_defect,
                seed_binding_identity=seed_builder.key if seed_builder else "",
            )
            snapshot = self.ledger.snapshot()
            existing_ids = {
                str(existing.get("hypothesis_id") or "") for existing in snapshot
            }
            if (
                proposed.hypothesis_id not in existing_ids
                and len(snapshot) >= max_hypotheses
            ):
                self._increment_budget("hypotheses")
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "hypothesis_limit",
                    "The recursive explanation hypothesis budget is exhausted.",
                )
                continue
            hypothesis = self.ledger.create(
                claim,
                assessment.ref,
                upstream_defect,
                seed_binding_identity=seed_builder.key if seed_builder else "",
            )
            self._bind_hypothesis_to_seed(
                hypothesis.hypothesis_id, seed_builder
            )
            hypothesis = self.ledger.add_support(
                hypothesis.hypothesis_id,
                assessment.ref,
                assessment.reason,
                assessment.confidence,
            )
            predecessor = FrontierItem.create(
                node_ref=assessment.ref,
                defect_state=upstream_defect,
                downstream_path=[assessment.ref, *item.downstream_path],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=hypothesis.seed_binding_identity,
                depth=item.depth + 1,
                candidate_source=assessment.relation,
                priority=max(assessment.confidence, 0.0),
                checked_evidence_refs=list(assessment.evidence_refs),
                graph_position=graph_position(assessment.ref),
            )
            self._merge_visit_evidence(predecessor.visit_key, assessment.evidence_refs)
            self.frontier.push(predecessor)

        if is_present and not judgment.candidate_introduction and not declared_recursive:
            self.mark_unresolved(
                item,
                "defective_dead_end",
                "The Judge retained a present defect without a supported predecessor or introduction candidate.",
            )

        self.frontier.mark_completed(item, evidence_hash)

    def route_navigation_candidates(
        self,
        item: FrontierItem,
        candidates: Sequence[CausalCandidate],
        *,
        graph_position: Any,
        max_hypotheses: int,
    ) -> int:
        seed_builder = self._seed_builder_for_item(item)
        selected = [
            candidate
            for candidate in candidates
            if candidate.ref in self.graph.nodes
            and root_candidate_eligible(candidate.node)
        ][:NAVIGATION_ROUTE_CANDIDATE_LIMIT]
        if not selected:
            self.complete_unresolved(
                item,
                "navigation_candidates_missing",
                "The progress aggregate has no concrete semantic candidate to route backward.",
            )
            return 0

        successors: List[AttributionHypothesis] = []
        for candidate in selected:
            if len(self.ledger.snapshot()) >= max_hypotheses:
                self._increment_budget("hypotheses")
                self._mark_ref_unresolved(
                    candidate.ref,
                    item,
                    "hypothesis_limit",
                    "The navigation route hypothesis budget is exhausted.",
                )
                continue
            upstream_defect = item.defect_state.transformed(
                label="navigation_candidate_semantic_cause",
                expected=(
                    "The recorded semantics at {0} are consistent with avoiding the downstream defect {1}."
                ).format(candidate.ref, item.defect_state.label),
                actual=(
                    "The recorded semantics at {0} are a high-relevance candidate that may contain an "
                    "upstream assumption or action leading to {1}; defect presence remains unconfirmed."
                ).format(candidate.ref, item.defect_state.label),
                mechanism=(
                    "Offline semantic retrieval selected this concrete node for independent LLM defect "
                    "judgment; ranking is navigation evidence, not a causal verdict."
                ),
                scope="navigation_candidate_validation",
                transformation_reason=(
                    "The progress aggregate is offline routing state, so causal judgment moves to its "
                    "highest-relevance concrete predecessor."
                ),
            )
            self._remember_defect(upstream_defect)
            downstream_chain = self.transformation_chains.get(
                item.defect_state.fingerprint, (item.defect_state,)
            )
            self.transformation_chains[upstream_defect.fingerprint] = (
                upstream_defect,
                *downstream_chain,
            )
            claim = (
                "Independently judge whether {0} introduces an upstream semantic cause of {1}; "
                "offline ranking is retrieval-only."
            ).format(candidate.ref, item.defect_state.label)
            hypothesis = self.ledger.create(
                claim,
                candidate.ref,
                upstream_defect,
                seed_binding_identity=seed_builder.key if seed_builder else "",
            )
            self._bind_hypothesis_to_seed(
                hypothesis.hypothesis_id, seed_builder
            )
            hypothesis = self.ledger.add_support(
                hypothesis.hypothesis_id,
                candidate.ref,
                "Selected as a bounded progress-navigation candidate; causality is unconfirmed.",
                0.0,
            )
            evidence_refs = tuple(
                dict.fromkeys(
                    [
                        *candidate.evidence_refs,
                        candidate.ref,
                        *item.checked_evidence_refs,
                    ]
                )
            )
            self.graph.add_offline_navigation_edge(
                candidate.ref,
                item.node_ref,
                evidence_refs=evidence_refs,
                confidence=0.0,
            )
            predecessor = FrontierItem.create(
                node_ref=candidate.ref,
                defect_state=upstream_defect,
                downstream_path=[candidate.ref, *item.downstream_path],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=hypothesis.seed_binding_identity,
                depth=item.depth + 1,
                candidate_source="navigation_semantic_hypothesis",
                priority=max(candidate.score, 0.0),
                checked_evidence_refs=list(evidence_refs),
                graph_position=graph_position(candidate.ref),
            )
            self._merge_visit_evidence(predecessor.visit_key, evidence_refs)
            self.frontier.push(predecessor)
            successors.append(hypothesis)

        if not successors:
            self.complete_unresolved(
                item,
                "navigation_hypothesis_limit",
                "No navigation candidate could be queued within the hypothesis budget.",
                exhausted_budget="hypotheses",
            )
            return 0
        parent = self.ledger.get(item.hypothesis_id)
        if parent.status in {"active", "supported"}:
            self.ledger.supersede(
                parent.hypothesis_id,
                successors[0].hypothesis_id,
                "Offline progress navigation moved causal judgment to concrete predecessors.",
            )
        route_hash = hashlib.sha256(
            stable_json([candidate.ref for candidate in selected]).encode("utf-8")
        ).hexdigest()
        self.frontier.mark_completed(item, "navigation:{0}".format(route_hash))
        return len(successors)

    def mark_unresolved(
        self,
        item: FrontierItem,
        reason: str,
        details: str = "",
        *,
        exhausted_budget: str = "",
    ) -> None:
        if exhausted_budget:
            self._increment_budget(exhausted_budget)
        self._mark_ref_unresolved(item.node_ref, item, reason, details)

    def complete_unresolved(
        self,
        item: FrontierItem,
        reason: str,
        details: str = "",
        *,
        exhausted_budget: str = "",
    ) -> None:
        self.mark_unresolved(
            item,
            reason,
            details,
            exhausted_budget=exhausted_budget,
        )
        self.frontier.mark_completed(item, "unresolved:{0}".format(reason))

    def build_report(self, *, judge: CausalJudge) -> RecursiveAttributionReport:
        self.finalize_pending_rejudges()
        self._suppress_roots_for_conservative_seed_outcomes()
        hypotheses = [AttributionHypothesis.from_dict(item) for item in self.ledger.snapshot()]
        by_id = {item.hypothesis_id: item for item in hypotheses}
        unresolved_ids = self.unresolved_hypothesis_ids | self.introduction_hypothesis_ids
        if self.present_hypothesis_ids and not unresolved_ids and not (
            self.confirmed_roots or self.co_roots
        ):
            unresolved_ids.update(self.present_hypothesis_ids)
            self.unresolved_hypothesis_ids.update(self.present_hypothesis_ids)
            for hypothesis_id in self.present_hypothesis_ids:
                seed_key = self.hypothesis_seed_keys.get(hypothesis_id, "")
                builder = self.seed_ledger.get(seed_key)
                if builder is not None:
                    builder.mark_unresolved(
                        "defect_chain_unresolved",
                        "A visited defect remained present without an introduction candidate.",
                    )
            for judgment in self.step_judgments:
                if judgment.current_defect_status != "present":
                    continue
                self.unresolved_refs.append(judgment.current_node_ref)
                self.unresolved_branches.append(
                    {
                        "node_ref": judgment.current_node_ref,
                        "defect_state_id": "",
                        "hypothesis_id": "",
                        "reason": "defect_chain_unresolved",
                        "details": "A visited defect remained present without an introduction candidate.",
                        "depth": 0,
                    }
                )
        unresolved_hypotheses = [by_id[item] for item in sorted(unresolved_ids) if item in by_id]
        candidate_seen: Set[Tuple[str, str, str]] = set()
        causal_candidates: List[CausalCandidate] = []
        for candidate in self.causal_candidates:
            key = _candidate_key(candidate)
            if key not in candidate_seen:
                candidate_seen.add(key)
                causal_candidates.append(candidate)
        merged = {
            key: tuple(sorted(refs))
            for key, refs in sorted(self.visit_evidence.items())
            if len(refs) > 1
        }
        provider = _provider_circuit(judge)
        global_passes = [
            item
            for item in self.investigation_journal
            if isinstance(item, Mapping)
            and item.get("kind") == "global_candidate_pass"
        ]
        completed_global_passes = [
            item for item in global_passes if item.get("status") == "completed"
        ]
        expansion_reasons = []
        for item in completed_global_passes:
            judgment = item.get("judgment")
            if not isinstance(judgment, Mapping):
                continue
            for request in judgment.get("expansion_requests") or ():
                if isinstance(request, Mapping):
                    expansion_reasons.append(dict(request))
        metadata = {
            "analysis": (
                "retrieval_global_recursive_fusion"
                if global_passes
                else "agentic_recursive_semantic_taint"
            ),
            "behavior_impact": "none_offline_analysis_only",
            "seed_count": self.seed_count,
            "processed_frontier_items": self.processed_items,
            "judge_request_count": self.judge_requests,
            "physical_judge_request_count": self.judge_requests,
            "judge_request_uncertainty_count": self.judge_request_uncertainty_count,
            "logical_judge_call_count": self.logical_judge_calls,
            "artifact_bytes": self.artifact_bytes,
            "investigation_rounds": self.investigation_rounds,
            "investigation_result_bytes": self.investigation_result_bytes,
            "investigation_budget_exhausted": bool(
                self.exhausted_budgets.get("investigation_rounds")
            ),
            "exhausted_budgets": dict(sorted(self.exhausted_budgets.items())),
            "unresolved_branches": list(self.unresolved_branches),
            "merged_visit_evidence": merged,
            "introduction_bindings": list(self.introduction_bindings),
            "frontier_checkpoint": self.frontier.checkpoint(),
            "hypothesis_snapshot": self.ledger.snapshot(),
            "provider_circuit": provider,
            "independent_confirmation": "completed",
            "confirmation_queue": list(self.confirmation_queue),
            "confirmation_journal": list(self.confirmation_journal),
            "logical_confirmation_call_count": self.logical_confirmation_calls,
            "fusion_mode": "retrieval-global" if global_passes else "off",
            "global_candidate_pass_count": len(completed_global_passes),
            "global_candidate_judgments": [
                copy.deepcopy(item.get("judgment"))
                for item in completed_global_passes
                if isinstance(item.get("judgment"), Mapping)
            ],
            "candidate_compression": [
                copy.deepcopy(item.get("candidate_compression"))
                for item in completed_global_passes
                if isinstance(item.get("candidate_compression"), Mapping)
            ],
            "recursive_expansion_reasons": expansion_reasons,
            "global_judge_physical_request_count": sum(
                int(item.get("physical_request_delta") or 0)
                for item in completed_global_passes
            ),
        }
        return RecursiveAttributionReport(
            case_id=self.graph.case_id,
            objective=self.objective,
            start_refs=self.start_refs,
            seed_results=self.seed_results(),
            analysis_perspective=self.analysis_perspective,
            defect_states=tuple(self.defect_states.values()),
            causal_candidates=tuple(causal_candidates),
            causal_relations=tuple(self.causal_relations),
            step_judgments=tuple(self.step_judgments),
            hypotheses=tuple(hypotheses),
            introduction_candidates=tuple(self.introduction_candidates),
            confirmations=tuple(self.confirmations),
            confirmed_roots=tuple(self.confirmed_roots),
            co_roots=tuple(self.co_roots),
            contributing_conditions=tuple(self.contributing_conditions),
            amplifying_factors=tuple(self.amplifying_factors),
            rejected_candidates=tuple(self.rejected_candidates),
            unresolved_hypotheses=tuple(unresolved_hypotheses),
            taint_paths=tuple(dict.fromkeys(self.taint_paths)),
            visited_order=_dedupe_strings(self.visited_order),
            unresolved_refs=_dedupe_strings(self.unresolved_refs),
            investigation_journal=tuple(self.investigation_journal),
            metadata=metadata,
        )

    def _suppress_roots_for_conservative_seed_outcomes(self) -> None:
        unpublished_seed_bindings = {
            builder.key
            for builder in self.seed_ledger.values()
            if builder.to_result().outcome != "confirmed_root"
        }
        if not unpublished_seed_bindings:
            return
        retained_primary: List[ConfirmedRoot] = []
        retained_co_roots: List[ConfirmedRoot] = []
        for roots, retained in (
            (self.confirmed_roots, retained_primary),
            (self.co_roots, retained_co_roots),
        ):
            for root in roots:
                confirmation = RootConfirmation.from_dict(dict(root.confirmation))
                if confirmation.seed_binding_identity not in unpublished_seed_bindings:
                    retained.append(root)
                    continue
                self.unresolved_hypothesis_ids.add(root.hypothesis_id)
                if root.node_ref not in self.unresolved_refs:
                    self.unresolved_refs.append(root.node_ref)
                self.unresolved_branches.append(
                    {
                        "node_ref": root.node_ref,
                        "defect_state_id": root.defect_state.defect_state_id,
                        "hypothesis_id": root.hypothesis_id,
                        "confirmation_identity": confirmation.confirmation_identity,
                        "reason": "seed_outcome_conservative",
                        "details": "The seed retained unresolved evidence, so its confirmed branch remains diagnostic only.",
                        "depth": max(0, len(root.recursive_path) - 1),
                    }
                )
        self.confirmed_roots = retained_primary
        self.co_roots = retained_co_roots

    def _candidate_for_ref(
        self,
        ref: str,
        *,
        source: str,
        edge: Mapping[str, Any],
        evidence_refs: Tuple[str, ...],
    ) -> Optional[CausalCandidate]:
        node = self.graph.nodes.get(ref)
        if node is None or not self.graph.evidence_eligible(ref):
            return None
        return CausalCandidate(
            ref=ref,
            node=node,
            source=source,
            edge=dict(edge),
            score=1.0,
            evidence_refs=evidence_refs,
        )

    def _remember_candidate(self, candidate: CausalCandidate) -> None:
        if any(existing == candidate for existing in self.causal_candidates):
            return
        self.causal_candidates.append(candidate)

    def _remember_defect(self, defect_state: DefectState) -> None:
        self.defect_states.setdefault(defect_state.fingerprint, defect_state)
        self.transformation_chains.setdefault(defect_state.fingerprint, (defect_state,))

    def _merge_visit_evidence(self, visit_key: str, refs: Iterable[str]) -> None:
        self.visit_evidence.setdefault(visit_key, set()).update(str(ref) for ref in refs if ref)

    def _increment_budget(self, name: str) -> None:
        self.exhausted_budgets[name] = self.exhausted_budgets.get(name, 0) + 1

    def _mark_seed_unresolved(
        self,
        ref: str,
        reason: str,
        details: str,
        *,
        seed_key: str = "",
    ) -> None:
        if seed_key:
            builder = self.seed_ledger.get(seed_key)
            if builder is not None:
                builder.mark_unresolved(reason, details)
        elif ref == "analysis:signal":
            for builder in self.seed_ledger.values():
                if builder.to_result().outcome == "inconclusive":
                    builder.mark_unresolved(reason, details)
        self.unresolved_refs.append(ref)
        self.unresolved_branches.append(
            {
                "node_ref": ref,
                "defect_state_id": "",
                "hypothesis_id": "",
                "reason": reason,
                "details": details,
                "depth": 0,
            }
        )

    def _mark_ref_unresolved(
        self,
        ref: str,
        item: FrontierItem,
        reason: str,
        details: str,
    ) -> None:
        builder = self._seed_builder_for_item(item)
        if builder is not None:
            builder.mark_unresolved(reason, details)
        self.unresolved_refs.append(ref)
        self.unresolved_hypothesis_ids.add(item.hypothesis_id)
        self.unresolved_branches.append(
            {
                "node_ref": ref,
                "defect_state_id": item.defect_state.defect_state_id,
                "hypothesis_id": item.hypothesis_id,
                "reason": reason,
                "details": details,
                "depth": item.depth,
            }
        )


class AgenticRecursiveAnalyzer:
    def __init__(
        self,
        *,
        judge: CausalJudge,
        retriever: Optional[SemanticPredecessorRetriever] = None,
        tools: Optional[CausalInvestigationTools] = None,
        max_frontier_items: int = 96,
        max_depth: int = 20,
        max_hypotheses: int = 24,
        max_investigation_rounds: int = 12,
        max_artifact_bytes: int = 1_048_576,
        max_judge_requests: int = 128,
        checkpoint: Optional[CheckpointBundle] = None,
        checkpoint_config: Optional[Mapping[str, Any]] = None,
        stop_requested: Optional[Callable[[], bool]] = None,
        fusion_mode: str = "off",
    ) -> None:
        self.judge = judge
        self.retriever = retriever or SemanticPredecessorRetriever()
        self.tools = tools
        self.max_frontier_items = max(0, int(max_frontier_items))
        self.max_depth = max(0, int(max_depth))
        self.max_hypotheses = max(0, int(max_hypotheses))
        self.max_investigation_rounds = max(0, int(max_investigation_rounds))
        self.max_artifact_bytes = max(0, int(max_artifact_bytes))
        self.max_judge_requests = max(0, int(max_judge_requests))
        if fusion_mode not in {"off", "retrieval-global"}:
            raise ValueError("unsupported fusion_mode: {0}".format(fusion_mode))
        self.fusion_mode = fusion_mode
        if checkpoint is not None and checkpoint_config is None:
            raise ValueError("checkpoint_config is required with checkpoint")
        self.checkpoint = checkpoint
        self.checkpoint_config = dict(checkpoint_config or {})
        self.stop_requested = stop_requested or (lambda: False)

    def _checkpoint_state(self, state: RecursiveAnalysisState, semantic_key: str) -> None:
        if self.checkpoint is None:
            return
        state.provider_state = _provider_state_payload(
            self.judge,
            state,
            cache_identity=str(self.checkpoint_config["cache_identity"]),
        )
        self.checkpoint.commit_snapshot(
            semantic_key=semantic_key,
            frontier_payload=state.frontier_checkpoint_payload(),
            hypothesis_payload=state.hypothesis_checkpoint_payload(),
            action_payload=state.action_checkpoint_payload(),
        )

    def _checkpoint_action(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> None:
        if self.checkpoint is not None:
            self.checkpoint.record_action(operation, semantic_key, payload)

    def _restore_provider_state(self, state: RecursiveAnalysisState) -> None:
        payload = _validate_provider_state(
            state.provider_state,
            state,
            cache_identity=str(self.checkpoint_config["cache_identity"]),
        )
        circuit = payload["circuit"]
        target = _judge_transport(self.judge)
        target.provider_circuit_open = bool(circuit["open"])
        target.provider_circuit_reason = str(circuit["reason"])
        target.consecutive_provider_errors = int(
            circuit["consecutive_provider_errors"]
        )
        target.provider_error_threshold = int(circuit["provider_error_threshold"])

    def _capture_provider_result_state(self, state: RecursiveAnalysisState) -> JsonDict:
        return _provider_state_payload(
            self.judge,
            state,
            cache_identity=str(self.checkpoint_config.get("cache_identity") or ""),
        )

    def _apply_provider_result_state(
        self, state: RecursiveAnalysisState, payload: Mapping[str, Any]
    ) -> None:
        provider = _validate_provider_state(
            payload.get("provider_state"),
            state,
            cache_identity=str(self.checkpoint_config.get("cache_identity") or ""),
            require_accounting_match=False,
        )
        state.provider_state = provider
        circuit = provider["circuit"]
        target = _judge_transport(self.judge)
        target.provider_circuit_open = bool(circuit["open"])
        target.provider_circuit_reason = str(circuit["reason"])
        target.consecutive_provider_errors = int(
            circuit["consecutive_provider_errors"]
        )
        target.provider_error_threshold = int(circuit["provider_error_threshold"])

    @staticmethod
    def _replay_action(state: RecursiveAnalysisState, semantic_key: str) -> Optional[JsonDict]:
        value = state.replay_actions.get(semantic_key)
        return dict(value) if isinstance(value, Mapping) else None

    def _run_global_candidate_prepass(
        self, state: RecursiveAnalysisState, graph: TraceGraph
    ) -> None:
        if self.fusion_mode != "retrieval-global":
            return
        if not isinstance(self.judge, GlobalJudgeCapability):
            state.investigation_journal.append(
                {
                    "kind": "global_candidate_pass",
                    "status": "fallback_recursive",
                    "reason": "judge_missing_global_capability",
                    "behavior_impact": "none_offline_analysis_only",
                }
            )
            return

        completed_seed_visits = {
            (
                str(event.get("hypothesis_id") or ""),
                str(event.get("defect_fingerprint") or ""),
            )
            for event in state.investigation_journal
            if isinstance(event, Mapping)
            and event.get("kind") == "global_candidate_pass"
            and event.get("status") == "completed"
        }

        queued_items = [
            FrontierItem.from_dict(item) for item in state.frontier.snapshot()
        ]
        for item in queued_items:
            seed_visit = (item.hypothesis_id, item.defect_state.fingerprint)
            if seed_visit in completed_seed_visits:
                continue
            node = graph.nodes.get(item.node_ref)
            if node is None or not graph.analysis_start_eligible(item.node_ref):
                continue
            hypothesis = state.ledger.get(item.hypothesis_id)
            if hypothesis.status not in {"active", "supported"}:
                continue
            active_seed_ref = (
                graph.resolve(item.downstream_path[-1])
                or item.downstream_path[-1]
            )
            try:
                candidates, paths = self._global_candidate_pool(
                    state, graph, item
                )
                capsules = build_candidate_evidence_capsules(
                    graph=graph,
                    candidates=candidates,
                    defect_state=item.defect_state,
                    downstream_paths=paths,
                    start_refs=(active_seed_ref,),
                )
            except Exception as exc:
                state.investigation_journal.append(
                    {
                        "kind": "global_candidate_pass",
                        "status": "fallback_recursive",
                        "seed_ref": active_seed_ref,
                        "reason": "capsule_build_error: {0}: {1}".format(
                            type(exc).__name__, exc
                        ),
                        "behavior_impact": "none_offline_analysis_only",
                    }
                )
                continue
            if not capsules:
                state.investigation_journal.append(
                    {
                        "kind": "global_candidate_pass",
                        "status": "fallback_recursive",
                        "seed_ref": active_seed_ref,
                        "reason": "candidate_evidence_capsules_empty",
                        "behavior_impact": "none_offline_analysis_only",
                    }
                )
                continue
            for candidate in candidates:
                state._remember_candidate(candidate)
            metrics = candidate_compression_metrics(graph, capsules)
            request = GlobalCandidateJudgeRequest(
                case_id=graph.case_id,
                objective=state.objective,
                analysis_perspective=state.analysis_perspective,
                seed_ref=active_seed_ref,
                active_defect=item.defect_state,
                active_focus_text=item.defect_state.actual,
                active_focus_text_hash=active_focus_text_sha256(
                    item.defect_state.actual
                ),
                start_refs=(active_seed_ref,),
                capsules=capsules,
                trace_health={
                    "missing_artifact_count": sum(
                        len(capsule.missing_evidence_refs) for capsule in capsules
                    ),
                    "candidate_compression": metrics,
                },
            )
            remaining = max(0, self.max_judge_requests - state.judge_requests)
            self._checkpoint_state(
                state, "global:before:{0}".format(item.visit_key)
            )
            try:
                result = self.judge.judge_candidates_bounded(
                    request, max_physical_requests=remaining
                )
                if not isinstance(result, BoundedJudgeCallResult):
                    raise TypeError(
                        "global Judge must return BoundedJudgeCallResult"
                    )
                judgment = result.value
                if not isinstance(judgment, GlobalCandidateJudgment):
                    raise TypeError(
                        "global Judge returned an unsupported judgment"
                    )
                validate_active_focus_binding(request, judgment)
                judgment = validate_global_candidate_payload(
                    judgment.to_dict(), request=request
                )
                state.logical_judge_calls += 1
                state.judge_requests += result.physical_requests
            except BoundedJudgeCallError as exc:
                state.judge_requests += exc.physical_requests
                state.investigation_journal.append(
                    {
                        "kind": "global_candidate_pass",
                        "status": "fallback_recursive",
                        "seed_ref": active_seed_ref,
                        "reason": "global_judge_error: {0}".format(exc),
                        "physical_request_delta": exc.physical_requests,
                        "candidate_compression": metrics,
                        "behavior_impact": "none_offline_analysis_only",
                    }
                )
                continue
            except Exception as exc:
                state.investigation_journal.append(
                    {
                        "kind": "global_candidate_pass",
                        "status": "fallback_recursive",
                        "seed_ref": active_seed_ref,
                        "reason": "global_judge_error: {0}: {1}".format(
                            type(exc).__name__, exc
                        ),
                        "physical_request_delta": 0,
                        "candidate_compression": metrics,
                        "behavior_impact": "none_offline_analysis_only",
                    }
                )
                continue
            event = {
                "kind": "global_candidate_pass",
                "status": "completed",
                "seed_ref": active_seed_ref,
                "hypothesis_id": item.hypothesis_id,
                "defect_fingerprint": item.defect_state.fingerprint,
                "visit_key": item.visit_key,
                "physical_request_delta": result.physical_requests,
                "candidate_compression": metrics,
                "candidate_evidence_capsules": [
                    capsule.to_dict() for capsule in capsules
                ],
                "judgment": judgment.to_dict(),
                "behavior_impact": "none_offline_analysis_only",
            }
            state.investigation_journal.append(event)
            self._apply_global_candidate_judgment(
                state=state,
                item=item,
                candidates=candidates,
                capsules=capsules,
                judgment=judgment,
                request=request,
            )
            self._checkpoint_state(
                state, "global:after:{0}".format(item.visit_key)
            )

    def _global_candidate_pool(
        self,
        state: RecursiveAnalysisState,
        graph: TraceGraph,
        item: FrontierItem,
    ) -> Tuple[List[CausalCandidate], Dict[str, Tuple[str, ...]]]:
        retrieved = self.retriever.retrieve(
            graph,
            item.node_ref,
            item.defect_state,
            state.ledger.get(item.hypothesis_id),
            limit=24,
            allow_semantic_fallback=True,
        )
        decisive_evidence = self._global_decisive_evidence_candidates(
            graph, item
        )
        related_existing = []
        active_path = set(item.downstream_path)
        for candidate in state.causal_candidates:
            target = str(candidate.edge.get("to_ref") or "")
            if candidate.ref in active_path or target in active_path:
                related_existing.append(candidate)
        authored_siblings = self._global_authored_decision_siblings(
            graph,
            [*related_existing, *retrieved],
        )
        ordered = [
            *related_existing,
            *retrieved,
            *authored_siblings,
            *decisive_evidence,
        ]
        selected: Dict[str, CausalCandidate] = {}
        routes_by_ref: Dict[str, List[CausalCandidate]] = {}
        refs: List[str] = []
        for candidate in ordered:
            resolved = graph.resolve(candidate.ref) or candidate.ref
            if (
                resolved not in graph.nodes
                or not graph.evidence_eligible(resolved)
            ):
                continue
            if resolved not in routes_by_ref:
                refs.append(resolved)
                routes_by_ref[resolved] = []
            routes_by_ref[resolved].append(candidate)
        for ref in refs:
            selected[ref] = canonical_candidate_route(
                graph, ref, routes_by_ref[ref]
            )
        paths: Dict[str, Tuple[str, ...]] = {}
        for ref in refs:
            grounded_path = _grounded_downstream_path(
                graph,
                ref,
                item.downstream_path,
            )
            if grounded_path:
                joined_at = grounded_path[-1]
                offset = item.downstream_path.index(joined_at)
                paths[ref] = (
                    *grounded_path,
                    *item.downstream_path[offset + 1 :],
                )
            elif ref == item.node_ref:
                paths[ref] = item.downstream_path
            else:
                paths[ref] = (ref,)
        return [selected[ref] for ref in refs], paths

    def _global_authored_decision_siblings(
        self,
        graph: TraceGraph,
        candidates: Sequence[CausalCandidate],
        *,
        limit: int = 8,
    ) -> List[CausalCandidate]:
        seeds = [
            candidate.node
            for candidate in candidates
            if candidate.node.event_type == "decision"
        ]
        selected_refs = {graph.resolve(item.ref) or item.ref for item in candidates}
        ranked: List[Tuple[int, int, CausalCandidate]] = []
        for node in graph.nodes.values():
            if (
                node.ref in selected_refs
                or node.event_type != "decision"
                or not root_candidate_eligible(node)
            ):
                continue
            node_sources = {
                graph.resolve(ref) or ref for ref in node.source_refs if str(ref)
            }
            if len(node_sources) < 2:
                continue
            best: Optional[Tuple[int, int, TraceNode, Tuple[str, ...]]] = None
            for seed in seeds:
                seed_sources = {
                    graph.resolve(ref) or ref for ref in seed.source_refs if str(ref)
                }
                shared = tuple(sorted(node_sources & seed_sources))
                distance = abs(graph.position(node.ref) - graph.position(seed.ref))
                if len(shared) < 2 or distance > 12:
                    continue
                rank = (len(shared), -distance)
                if best is None or rank > (best[0], -best[1]):
                    best = (len(shared), distance, seed, shared)
            if best is None:
                continue
            overlap, distance, seed, shared = best
            score = min(0.86, 0.68 + overlap * 0.03)
            ranked.append(
                (
                    -overlap,
                    distance,
                    CausalCandidate(
                        ref=node.ref,
                        node=node,
                        source="global_authored_decision_sibling",
                        edge={
                            "from_ref": node.ref,
                            "to_ref": seed.ref,
                            "relation": "shared_generation_provenance_candidate",
                            "evidence_type": "recorded_provenance_overlap",
                            "evidence_refs": list(shared),
                            "confidence": score,
                            "eligible_for_attribution": False,
                            "retrieval_candidate": True,
                            "inference_method": "bounded_same_turn_authored_sibling_v1",
                            "edge_origin": "offline.global_candidate_retrieval",
                        },
                        score=score,
                        evidence_refs=shared,
                    ),
                )
            )
        ranked.sort(key=lambda item: (item[0], item[1], item[2].ref))
        return [candidate for _, _, candidate in ranked[:limit]]

    def _global_decisive_evidence_candidates(
        self, graph: TraceGraph, item: FrontierItem
    ) -> List[CausalCandidate]:
        seed_ref = item.downstream_path[-1]
        boundary = graph.position(seed_ref)
        seed_node = graph.nodes.get(graph.resolve(seed_ref) or seed_ref)
        include_post_boundary_evidence = bool(
            seed_node and seed_node.event_type in EVALUATION_START_EVENTS
        )
        selected: List[Tuple[int, CausalCandidate]] = []
        for node in graph.nodes.values():
            position = graph.position(node.ref)
            if (
                position >= boundary and not include_post_boundary_evidence
            ) or node.event_type == "progress.episode" or not graph.evidence_eligible(node.ref):
                continue
            score = _global_evidence_score(node)
            if score <= 0.0:
                continue
            selected.append(
                (
                    position,
                    CausalCandidate(
                        ref=node.ref,
                        node=node,
                        source="global_decisive_evidence",
                        edge={
                            "from_ref": node.ref,
                            "to_ref": item.downstream_path[-1],
                            "relation": "global_evidence_candidate",
                            "evidence_type": "recorded_evidence_retrieval",
                            "evidence_refs": [node.ref],
                            "confidence": score,
                            "eligible_for_attribution": False,
                            "retrieval_candidate": True,
                            "inference_method": "bounded_verification_counterevidence_retrieval_v1",
                            "edge_origin": "offline.global_candidate_retrieval",
                            "temporal_relation": (
                                "after_derived_evaluation_node"
                                if position >= boundary
                                else "at_or_before_evaluation_node"
                            ),
                        },
                        score=score,
                        evidence_refs=(node.ref,),
                    ),
                )
            )
        selected.sort(key=lambda item: (-item[0], item[1].ref))
        return [candidate for _, candidate in selected[:8]]

    def _apply_global_candidate_judgment(
        self,
        *,
        state: RecursiveAnalysisState,
        item: FrontierItem,
        candidates: Sequence[CausalCandidate],
        capsules: Sequence[CandidateEvidenceCapsule],
        judgment: GlobalCandidateJudgment,
        request: GlobalCandidateJudgeRequest,
    ) -> None:
        seed_builder = state._seed_builder_for_item(item)
        if seed_builder is not None:
            seed_builder.record_global_judgment(
                judgment,
                (candidate.ref for candidate in candidates),
                request,
            )
        if judgment.outcome == "no_defect":
            hypothesis = state.ledger.get(item.hypothesis_id)
            if hypothesis.status in {"active", "supported"}:
                state.ledger.reject_with_frontier(
                    item.hypothesis_id,
                    judgment.reason,
                    opposing_refs=judgment.decisive_evidence_refs,
                    frontier=state.frontier,
                    evidence_hash=hashlib.sha256(
                        stable_json(judgment.to_dict()).encode("utf-8")
                    ).hexdigest(),
                )
            return

        candidate_by_ref = {
            state.graph.resolve(candidate.ref) or candidate.ref: candidate
            for candidate in candidates
        }
        capsule_by_ref = {capsule.candidate_ref: capsule for capsule in capsules}
        assessments = {
            assessment.candidate_ref: assessment
            for assessment in judgment.assessments
        }
        created_hypotheses: Set[str] = set()

        if judgment.outcome == "candidate_roots":
            for selected_ref in judgment.selected_candidate_refs:
                node = state.graph.nodes.get(selected_ref)
                candidate = candidate_by_ref.get(selected_ref)
                capsule = capsule_by_ref.get(selected_ref)
                if (
                    node is None
                    or candidate is None
                    or capsule is None
                    or not root_candidate_eligible(node)
                ):
                    continue
                assessment = assessments[selected_ref]
                hypothesis = state.ledger.create(
                    "Global comparison selected {0} as a candidate root for {1}.".format(
                        selected_ref, item.defect_state.label
                    ),
                    selected_ref,
                    item.defect_state,
                    seed_binding_identity=seed_builder.key if seed_builder else "",
                )
                state._bind_hypothesis_to_seed(
                    hypothesis.hypothesis_id, seed_builder
                )
                created_hypotheses.add(hypothesis.hypothesis_id)
                support_refs = _dedupe_strings(
                    [
                        selected_ref,
                        *assessment.evidence_refs,
                        *judgment.decisive_evidence_refs,
                    ]
                )
                for evidence_ref in support_refs:
                    if state.graph.resolve(evidence_ref):
                        state.ledger.add_support(
                            hypothesis.hypothesis_id,
                            evidence_ref,
                            assessment.reason,
                            max(assessment.confidence, 0.01),
                        )
                hypothesis = state.ledger.get(hypothesis.hypothesis_id)
                binding_key = (
                    selected_ref,
                    item.defect_state.fingerprint,
                    hypothesis.semantic_hash,
                    hypothesis.seed_binding_identity,
                )
                if binding_key not in state.introduction_binding_keys:
                    state.introduction_binding_keys.add(binding_key)
                    state.introduction_bindings.append(
                        {
                            "candidate_ref": selected_ref,
                            "defect_state_id": item.defect_state.defect_state_id,
                            "defect_fingerprint": item.defect_state.fingerprint,
                            "hypothesis_id": hypothesis.hypothesis_id,
                            "hypothesis_semantic_hash": hypothesis.semantic_hash,
                            "seed_binding_identity": hypothesis.seed_binding_identity,
                            "origin": "global_candidate_judgment",
                            "seed_key": seed_builder.key if seed_builder else "",
                        }
                    )
                    state.introduction_candidates.append(candidate)
                    state._remember_candidate(candidate)
                state.introduction_hypothesis_ids.add(hypothesis.hypothesis_id)
                queue_key = (
                    hypothesis.hypothesis_id,
                    selected_ref,
                    item.defect_state.fingerprint,
                    hypothesis.seed_binding_identity,
                )
                state.enqueue_confirmation(
                    {
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "candidate_ref": selected_ref,
                        "defect_fingerprint": item.defect_state.fingerprint,
                        "seed_binding_identity": hypothesis.seed_binding_identity,
                        "requested_by_ref": item.node_ref,
                        "recursive_path": list(capsule.downstream_path),
                        "checked_evidence_refs": list(support_refs),
                        "task_obligations": task_obligations(
                            state.graph, state.objective
                        ),
                        "analysis_perspective": state.analysis_perspective,
                        "semantic_identity": hashlib.sha256(
                            stable_json(queue_key).encode("utf-8")
                        ).hexdigest(),
                        "status": "queued",
                        "origin": "global_candidate_judgment",
                        "seed_key": seed_builder.key if seed_builder else "",
                    }
                )

        if judgment.outcome == "needs_expansion":
            for request in judgment.expansion_requests:
                anchor = state.graph.resolve(str(request.get("anchor_ref") or ""))
                if not anchor or anchor not in state.graph.nodes:
                    continue
                if not state.graph.analysis_start_eligible(anchor):
                    continue
                owner = next(
                    (
                        capsule
                        for capsule in capsules
                        if anchor == capsule.candidate_ref
                        or any(
                            str(member.get("ref") or "") == anchor
                            for member in capsule.action_group.get("members") or ()
                            if isinstance(member, Mapping)
                        )
                    ),
                    None,
                )
                path = (
                    owner.downstream_path
                    if owner is not None and anchor == owner.candidate_ref
                    else (
                        (anchor, *owner.downstream_path)
                        if owner is not None
                        else (anchor, *item.downstream_path)
                    )
                )
                hypothesis = state.ledger.create(
                    "Global comparison requested {0} expansion at {1}.".format(
                        str(request.get("context_kind") or "context"), anchor
                    ),
                    anchor,
                    item.defect_state,
                    seed_binding_identity=seed_builder.key if seed_builder else "",
                )
                state._bind_hypothesis_to_seed(
                    hypothesis.hypothesis_id, seed_builder
                )
                created_hypotheses.add(hypothesis.hypothesis_id)
                state.ledger.add_support(
                    hypothesis.hypothesis_id,
                    anchor,
                    str(request.get("reason") or judgment.reason),
                    max(judgment.confidence, 0.01),
                )
                hypothesis = state.ledger.get(hypothesis.hypothesis_id)
                state.frontier.push(
                    FrontierItem.create(
                        node_ref=anchor,
                        defect_state=item.defect_state,
                        downstream_path=path,
                        hypothesis_id=hypothesis.hypothesis_id,
                        hypothesis_semantic_hash=hypothesis.semantic_hash,
                        seed_binding_identity=hypothesis.seed_binding_identity,
                        depth=item.depth + 1,
                        candidate_source="global_requested_expansion",
                        priority=1.0,
                        checked_evidence_refs=judgment.decisive_evidence_refs,
                        graph_position=state.graph.position(anchor),
                    )
                )

        original = state.ledger.get(item.hypothesis_id)
        if created_hypotheses and item.hypothesis_id not in created_hypotheses and original.status in {
            "active",
            "supported",
        }:
            state.ledger.reject_with_frontier(
                item.hypothesis_id,
                "Global comparison superseded the seed with selected candidates.",
                opposing_refs=(),
                frontier=state.frontier,
                evidence_hash=hashlib.sha256(
                    stable_json(judgment.to_dict()).encode("utf-8")
                ).hexdigest(),
            )

    def analyze(
        self,
        graph: TraceGraph,
        *,
        start_refs: Optional[Iterable[str]] = None,
        objective: str,
        analysis_perspective: str = "Find the best-supported causal explanation.",
    ) -> RecursiveAttributionReport:
        analysis_graph = _clone_graph(graph)
        requested_starts = tuple(start_refs) if start_refs is not None else ()
        if not requested_starts:
            requested_starts = tuple(analysis_graph.default_start_refs())
        restored_checkpoint: Optional[CheckpointState] = None
        if self.checkpoint is not None:
            self.checkpoint.initialize(self.checkpoint_config)
            restored_checkpoint = self.checkpoint.restore(
                expected_config=self.checkpoint_config
            )
            final_report = restored_checkpoint.final_report
            if final_report is not None:
                analysis_graph.assert_evidence_eligible_references(
                    final_report,
                    label="restored completed report",
                )
                report = RecursiveAttributionReport.from_dict(final_report)
                _assert_report_grounded_evidence(
                    analysis_graph,
                    report,
                    label="restored completed report",
                )
                if restored_checkpoint.tail_repair_count:
                    metadata = dict(report.metadata)
                    metadata["checkpoint_audit"] = {
                        "tail_repair_count": restored_checkpoint.tail_repair_count,
                        "tail_repair_events": [
                            dict(item)
                            for item in restored_checkpoint.tail_repair_events
                        ],
                    }
                    report = replace(report, metadata=metadata)
                return report
            pending_report = restored_checkpoint.pending_report
            pending_action = restored_checkpoint.latest_actions.get("analysis:result")
            pending_interrupted = bool(
                isinstance(pending_action, Mapping)
                and isinstance(pending_action.get("payload"), Mapping)
                and pending_action["payload"].get("interrupted")
            )
            if pending_report is not None and (
                not pending_interrupted or self.checkpoint.output_commit_path.exists()
            ):
                analysis_graph.assert_evidence_eligible_references(
                    pending_report,
                    label="restored pending report",
                )
                report = RecursiveAttributionReport.from_dict(pending_report)
                _assert_report_grounded_evidence(
                    analysis_graph,
                    report,
                    label="restored pending report",
                )
                if restored_checkpoint.tail_repair_count:
                    metadata = dict(report.metadata)
                    metadata["checkpoint_audit"] = {
                        "tail_repair_count": restored_checkpoint.tail_repair_count,
                        "tail_repair_events": [
                            dict(item)
                            for item in restored_checkpoint.tail_repair_events
                        ],
                    }
                    report = replace(report, metadata=metadata)
                return report
        if (
            restored_checkpoint is not None
            and restored_checkpoint.frontier_payload
            and restored_checkpoint.hypothesis_payload
            and any(
                item.get("operation") == "state_snapshot"
                for item in restored_checkpoint.actions
            )
        ):
            state = RecursiveAnalysisState.from_checkpoint(
                graph=analysis_graph, checkpoint=restored_checkpoint
            )
            self._restore_provider_state(state)
            if (
                state.objective != objective
                or state.analysis_perspective != analysis_perspective
                or state.start_refs
                != _dedupe_strings(analysis_graph.resolve(ref) or ref for ref in requested_starts)
            ):
                raise ValueError("restored recursive state does not match analysis inputs")
        else:
            state = RecursiveAnalysisState.create(
                graph=analysis_graph,
                start_refs=requested_starts,
                objective=objective,
                analysis_perspective=analysis_perspective,
                max_hypotheses=self.max_hypotheses,
            )
            self._checkpoint_state(state, "analysis:initialized")
        tools = (
            self.tools.for_graph(
                analysis_graph, max_artifact_bytes=self.max_artifact_bytes
            )
            if self.tools is not None
            else CausalInvestigationTools(
                analysis_graph, max_artifact_bytes=self.max_artifact_bytes
            )
        )
        self._run_global_candidate_prepass(state, analysis_graph)
        if not requested_starts:
            state._mark_seed_unresolved(
                "analysis:start",
                "analysis_start_missing",
                "The trace does not contain a concrete analysis start node.",
            )
        interrupted = False
        while state.frontier and state.processed_items < self.max_frontier_items:
            if self.stop_requested():
                interrupted = True
                state._mark_seed_unresolved(
                    "analysis:signal",
                    "analysis_interrupted",
                    "SIGINT or SIGTERM requested graceful attribution shutdown.",
                )
                self._checkpoint_state(state, "analysis:interrupted")
                break
            self._checkpoint_state(state, "analysis:before_frontier_pop")
            item = state.frontier.pop()
            state.processed_items += 1
            current_node = analysis_graph.nodes.get(item.node_ref)
            if current_node is None or not analysis_graph.analysis_start_eligible(item.node_ref):
                state.complete_unresolved(
                    item,
                    "frontier_node_ineligible",
                    "The external evaluation fact cannot participate as an analysis seed.",
                )
                continue
            if item.depth > self.max_depth:
                state.complete_rejudge(
                    item,
                    terminal_state="depth_limit",
                    detail="Recursive depth exceeds the configured limit.",
                )
                state.complete_unresolved(
                    item,
                    "depth_limit",
                    "Recursive depth {0} exceeds limit {1}.".format(item.depth, self.max_depth),
                    exhausted_budget="depth",
                )
                continue
            if _provider_circuit(self.judge).get("open"):
                state.complete_rejudge(
                    item,
                    terminal_state="provider_circuit_open",
                    detail=str(
                        _provider_circuit(self.judge).get("reason")
                        or "Provider circuit is open."
                    ),
                )
                state.complete_unresolved(
                    item,
                    "provider_circuit_open",
                    str(_provider_circuit(self.judge).get("reason") or "Provider circuit is open."),
                    exhausted_budget="provider_circuit",
                )
                continue
            try:
                retrieved_candidates = self.retriever.retrieve(
                    analysis_graph,
                    item.node_ref,
                    item.defect_state,
                    state.ledger.get(item.hypothesis_id),
                    allow_semantic_fallback=True,
                )
                retrieved_candidates = [
                    candidate
                    for candidate in retrieved_candidates
                    if analysis_graph.evidence_eligible(candidate.ref)
                ]
                candidates = retrieved_candidates[:CAUSAL_STEP_CANDIDATE_LIMIT]
            except Exception as exc:
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item, terminal_state="retrieval_error", detail=detail
                )
                state.complete_unresolved(item, "retrieval_error", detail)
                continue
            for candidate in candidates:
                state._remember_candidate(candidate)
            if current_node is not None and is_navigation_node(current_node):
                state.route_navigation_candidates(
                    item,
                    candidates,
                    graph_position=analysis_graph.position,
                    max_hypotheses=self.max_hypotheses,
                )
                self._checkpoint_state(state, "analysis:navigation_routed")
                continue
            try:
                request = state.build_step_request(
                    analysis_graph,
                    item,
                    candidates,
                    retrieved_candidates=retrieved_candidates,
                )
            except Exception as exc:
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item, terminal_state="request_build_error", detail=detail
                )
                state.complete_unresolved(item, "request_build_error", detail)
                continue
            if not state.reserve_artifact_bytes(request, self.max_artifact_bytes):
                state.complete_rejudge(
                    item,
                    terminal_state="artifact_byte_limit",
                    detail="Hydrated artifact content exceeds the analysis byte budget.",
                )
                state.complete_unresolved(
                    item,
                    "artifact_byte_limit",
                    "Hydrated artifact content exceeds the analysis byte budget.",
                    exhausted_budget="artifact_bytes",
                )
                continue
            remaining_requests = max(0, self.max_judge_requests - state.judge_requests)
            bounded_judge = isinstance(self.judge, BoundedJudgeCapability)
            offline_judge = isinstance(self.judge, OfflineJudgeCapability)
            if not bounded_judge and not offline_judge:
                state.complete_rejudge(
                    item,
                    terminal_state="judge_budget_unenforceable",
                    detail="Judge has no explicit bounded or offline capability.",
                )
                state.complete_unresolved(
                    item,
                    "judge_budget_unenforceable",
                    "The Judge exposes neither a bounded transport capability nor an explicit zero-transport capability.",
                )
                continue
            evidence_hash = str(request.recursive_context.get("evidence_hash") or "")
            provider_action_key = "step:{0}:{1}".format(item.visit_key, evidence_hash)
            replay_action = self._replay_action(state, provider_action_key)
            if (
                replay_action is not None
                and replay_action.get("operation") == "provider_call_failed"
                and isinstance(replay_action.get("payload"), Mapping)
                and replay_action["payload"].get("physical_request_exact") is True
            ):
                replay_payload = replay_action["payload"]
                reserved_requests = int(
                    replay_payload.get("physical_requests_reserved") or 0
                )
                physical_delta = int(
                    replay_payload.get("physical_request_delta") or 0
                )
                if physical_delta < 0 or reserved_requests < physical_delta:
                    raise ValueError("exact failed Provider accounting is invalid")
                state.judge_requests += physical_delta - reserved_requests
                terminal_state = str(replay_payload.get("terminal_state") or "")
                unresolved_reason = str(
                    replay_payload.get("unresolved_reason") or ""
                )
                detail = str(replay_payload.get("detail") or "")
                exhausted_budget = str(
                    replay_payload.get("exhausted_budget") or ""
                )
                if not terminal_state or not unresolved_reason or not detail:
                    raise ValueError("exact failed Provider semantics are incomplete")
                self._apply_provider_result_state(state, replay_payload)
                state.complete_rejudge(
                    item,
                    terminal_state=terminal_state,
                    detail=detail,
                    physical_request_delta=physical_delta,
                )
                state.complete_unresolved(
                    item,
                    unresolved_reason,
                    detail,
                    exhausted_budget=exhausted_budget,
                )
                self._checkpoint_state(
                    state, "provider:exact_failure:{0}".format(item.visit_key)
                )
                continue
            if replay_action is not None and replay_action.get("operation") in {
                "provider_call_started",
                "provider_call_failed",
                "provider_call_interrupted",
            }:
                replay_payload = replay_action.get("payload")
                if not isinstance(replay_payload, Mapping):
                    raise ValueError("in-flight Provider action payload is invalid")
                if replay_action.get("operation") == "provider_call_started" or (
                    replay_action.get("operation") == "provider_call_failed"
                    and not replay_payload.get("physical_request_exact", False)
                ):
                    state.judge_request_uncertainty_count += 1
                if replay_action.get("operation") == "provider_call_failed":
                    self._apply_provider_result_state(state, replay_payload)
                if state.judge_requests >= self.max_judge_requests:
                    state._increment_budget("judge_requests")
                state.complete_rejudge(
                    item,
                    terminal_state="interrupted_judge_call",
                    detail="The prior process ended after fsyncing call intent but before a durable result.",
                    physical_request_delta=None,
                )
                state.complete_unresolved(
                    item,
                    "interrupted_judge_call",
                    "The in-flight Judge call is not repeated and no success is fabricated.",
                )
                self._checkpoint_state(state, "provider:interrupted:{0}".format(item.visit_key))
                self._checkpoint_action(
                    "provider_call_interrupted",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "unknown",
                        "replayed": False,
                    },
                )
                continue
            replayed_judgment: Optional[CausalStepJudgment] = None
            replayed_physical_delta = 0
            reserved_requests = 0
            if replay_action is not None and replay_action.get("operation") == "provider_call_completed":
                replay_payload = replay_action.get("payload")
                if not isinstance(replay_payload, Mapping):
                    raise ValueError("completed Provider action payload is invalid")
                replayed_judgment = CausalStepJudgment.from_dict(
                    dict(replay_payload.get("judgment") or {})
                )
                replayed_physical_delta = int(replay_payload.get("physical_request_delta") or 0)
                reserved_requests = int(
                    replay_payload.get("physical_requests_reserved") or 0
                )
            if replay_action is None:
                state.logical_judge_calls += 1
                reserved_requests = remaining_requests if bounded_judge else 0
                state.judge_requests += reserved_requests
                self._checkpoint_state(state, "provider:before:{0}".format(item.visit_key))
                self._checkpoint_action(
                    "provider_call_started",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "evidence_hash": evidence_hash,
                        "status": "in_flight",
                        "physical_requests_reserved": reserved_requests,
                    },
                )
            physical_delta = 0
            try:
                if replayed_judgment is not None:
                    judgment = replayed_judgment
                    physical_delta = replayed_physical_delta
                elif bounded_judge:
                    bounded_result = self.judge.judge_step_bounded(
                        request,
                        max_physical_requests=remaining_requests,
                    )
                    if not isinstance(bounded_result, BoundedJudgeCallResult):
                        raise TypeError(
                            "bounded Judge must return BoundedJudgeCallResult"
                        )
                    physical_delta = bounded_result.physical_requests
                    if physical_delta > remaining_requests:
                        raise ValueError("bounded Judge exceeded its physical request allowance")
                    judgment = bounded_result.value
                else:
                    judgment = self.judge.judge_step_offline(request)
            except BoundedJudgeCallError as exc:
                physical_delta = exc.physical_requests
                state.judge_requests += physical_delta - reserved_requests
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item,
                    terminal_state="judge_error",
                    detail=detail,
                    physical_request_delta=physical_delta,
                )
                state.complete_unresolved(
                    item,
                    "judge_error",
                    detail,
                )
                self._checkpoint_action(
                    "provider_call_failed",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "failed",
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": True,
                        "terminal_state": "judge_error",
                        "unresolved_reason": "judge_error",
                        "detail": detail,
                        "exhausted_budget": "",
                        "error": detail,
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
                continue
            except (JudgeProviderError, JudgeProviderUnavailable) as exc:
                if bounded_judge:
                    state.judge_request_uncertainty_count += 1
                terminal_state = (
                    "provider_unavailable"
                    if isinstance(exc, JudgeProviderUnavailable)
                    else "provider_error"
                )
                unresolved_reason = (
                    "provider_circuit_open"
                    if isinstance(exc, JudgeProviderUnavailable)
                    else "provider_error"
                )
                exhausted_budget = (
                    "provider_circuit"
                    if isinstance(exc, JudgeProviderUnavailable)
                    else ""
                )
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item,
                    terminal_state=terminal_state,
                    detail=detail,
                    physical_request_delta=physical_delta,
                )
                state.complete_unresolved(
                    item,
                    unresolved_reason,
                    detail,
                    exhausted_budget=exhausted_budget,
                )
                self._checkpoint_action(
                    "provider_call_failed",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "failed",
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": not bounded_judge,
                        "terminal_state": terminal_state,
                        "unresolved_reason": unresolved_reason,
                        "detail": detail,
                        "exhausted_budget": exhausted_budget,
                        "error": detail,
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
                continue
            except Exception as exc:
                if bounded_judge:
                    state.judge_request_uncertainty_count += 1
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item,
                    terminal_state="judge_error",
                    detail=detail,
                    physical_request_delta=physical_delta,
                )
                state.complete_unresolved(
                    item,
                    "judge_error",
                    detail,
                )
                self._checkpoint_action(
                    "provider_call_failed",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "failed",
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": not bounded_judge,
                        "terminal_state": "judge_error",
                        "unresolved_reason": "judge_error",
                        "detail": detail,
                        "exhausted_budget": "",
                        "error": detail,
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
                continue
            state.judge_requests += physical_delta - reserved_requests
            if not isinstance(judgment, CausalStepJudgment):
                detail = "Judge returned {0}, expected CausalStepJudgment.".format(
                    type(judgment).__name__
                )
                state.complete_rejudge(
                    item,
                    terminal_state="validation_error",
                    detail=detail,
                    physical_request_delta=physical_delta,
                )
                state.complete_unresolved(item, "judge_validation_error", detail)
                self._checkpoint_action(
                    "provider_call_failed",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "failed",
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": True,
                        "terminal_state": "validation_error",
                        "unresolved_reason": "judge_validation_error",
                        "detail": detail,
                        "exhausted_budget": "",
                        "error": detail,
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
                continue
            if replayed_judgment is None:
                self._checkpoint_action(
                    "provider_call_completed",
                    provider_action_key,
                    {
                        "call_kind": "step",
                        "visit_key": item.visit_key,
                        "status": "completed",
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": True,
                        "judgment": judgment.to_dict(),
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
            elif replay_action is not None:
                self._apply_provider_result_state(state, replay_action["payload"])
            state.complete_rejudge(
                item,
                terminal_state=_rejudge_success_terminal_state(
                    judgment,
                    bounded_judge=bounded_judge,
                    offline_judge=offline_judge,
                    physical_request_delta=physical_delta,
                ),
                judgment=judgment,
                physical_request_delta=physical_delta,
            )
            if any(
                "judge_request_budget_exhausted" in detail
                for detail in judgment.missing_evidence
            ):
                state._increment_budget("judge_requests")
            if judgment.suggested_investigation is not None:
                is_confirmation_request = (
                    isinstance(judgment.suggested_investigation, Mapping)
                    and judgment.suggested_investigation.get("action")
                    == "request_root_confirmation"
                )
                if is_confirmation_request:
                    state.apply_step(
                        item,
                        judgment,
                        graph_position=analysis_graph.position,
                        max_hypotheses=self.max_hypotheses,
                    )
                handled = self._handle_investigation(
                    state=state,
                    tools=tools,
                    item=item,
                    judgment=judgment,
                    request=request,
                )
                if is_confirmation_request or handled in {
                    "reopened",
                    "completed",
                    "branch_rejected",
                }:
                    continue
            state.apply_step(
                item,
                judgment,
                graph_position=analysis_graph.position,
                max_hypotheses=self.max_hypotheses,
            )

        if state.frontier and not interrupted:
            while state.frontier:
                item = state.frontier.pop()
                state.complete_rejudge(
                    item,
                    terminal_state="frontier_item_limit",
                    detail="The recursive frontier item budget is exhausted.",
                )
                state.complete_unresolved(
                    item,
                    "frontier_item_limit",
                    "The recursive frontier item budget is exhausted.",
                    exhausted_budget="frontier_items",
                )
        self._checkpoint_state(state, "analysis:frontier_complete")
        if not interrupted and not self.stop_requested():
            self._confirm_queued_roots(state)
            if self.stop_requested():
                interrupted = True
                state._mark_seed_unresolved(
                    "analysis:signal",
                    "analysis_interrupted",
                    "SIGINT or SIGTERM requested graceful attribution shutdown during confirmation.",
                )
                self._checkpoint_state(
                    state, "analysis:interrupted_during_confirmation"
                )
        elif not interrupted:
            interrupted = True
            state._mark_seed_unresolved(
                "analysis:signal",
                "analysis_interrupted",
                "SIGINT or SIGTERM requested graceful attribution shutdown before confirmation.",
            )
            self._checkpoint_state(state, "analysis:interrupted_before_confirmation")
        report = state.build_report(judge=self.judge)
        from .trace_improvement import build_recursive_trace_improvement_report

        metadata = dict(report.metadata)
        if (
            restored_checkpoint is not None
            and restored_checkpoint.tail_repair_count
        ):
            metadata["checkpoint_audit"] = {
                "tail_repair_count": restored_checkpoint.tail_repair_count,
                "tail_repair_events": [
                    dict(item) for item in restored_checkpoint.tail_repair_events
                ],
            }
        metadata["trace_improvement_report"] = build_recursive_trace_improvement_report(
            analysis_graph, report
        )
        if interrupted:
            metadata["termination_reason"] = "signal_interrupted"
            metadata["checkpoint_resume_available"] = self.checkpoint is not None
        report = replace(report, metadata=metadata)
        _assert_report_grounded_evidence(
            analysis_graph,
            report,
            label="final attribution report",
        )
        self._checkpoint_state(state, "analysis:final_state")
        self._checkpoint_action(
            "analysis_ready",
            "analysis:result",
            {"report": report.to_dict(), "interrupted": interrupted},
        )
        if self.checkpoint is not None:
            self.checkpoint.flush_all()
        return report

    def _confirm_queued_roots(self, state: RecursiveAnalysisState) -> None:
        pending_confirmations = [
            item for item in state.confirmation_queue if item.get("status") == "queued"
        ]
        while pending_confirmations:
            if self.stop_requested():
                break
            queued = pending_confirmations.pop(0)
            try:
                request = self._build_confirmation_request(state, queued)
                preflight_root_confirmation_request(request)
            except (KeyError, TypeError, ValueError) as exc:
                confirmation = RootConfirmation(
                    candidate_ref=str(queued.get("candidate_ref") or ""),
                    status="unknown",
                    reason="confirmation_request_ineligible: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    counterfactual_status="unknown",
                    hypothesis_id=str(queued.get("hypothesis_id") or ""),
                    hypothesis_semantic_hash=str(
                        queued.get("hypothesis_semantic_hash") or ""
                    ),
                    defect_fingerprint=str(queued.get("defect_fingerprint") or ""),
                    recursive_path=tuple(queued.get("recursive_path") or ()),
                    seed_binding_identity=str(
                        queued.get("seed_binding_identity") or ""
                    ),
                )
                self._record_confirmation(state, queued, confirmation, 0)
                continue

            bounded_judge = isinstance(self.judge, BoundedJudgeCapability)
            offline_judge = isinstance(self.judge, OfflineJudgeCapability)
            if not bounded_judge and not offline_judge:
                confirmation = RootConfirmation(
                    candidate_ref=request.candidate_ref,
                    status="unknown",
                    reason="confirmation_budget_unenforceable: Judge has no explicit bounded or offline capability",
                    counterfactual_status="unknown",
                    hypothesis_id=request.hypothesis_id,
                    hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                    defect_fingerprint=request.defect_state.fingerprint,
                    recursive_path=request.recursive_path,
                    seed_binding_identity=request.seed_binding_identity,
                )
                self._record_confirmation(state, queued, confirmation, 0)
                continue

            remaining = max(0, self.max_judge_requests - state.judge_requests)
            confirmation_action_key = "confirmation:{0}".format(
                str(
                    queued.get("semantic_identity")
                    or confirmation_identity_for(
                        hypothesis_id=request.hypothesis_id,
                        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                        candidate_ref=request.candidate_ref,
                        defect_fingerprint=request.defect_state.fingerprint,
                        recursive_path=request.recursive_path,
                        seed_binding_identity=request.seed_binding_identity,
                    )
                )
            )
            replay_action = self._replay_action(state, confirmation_action_key)
            replayed_confirmation: Optional[RootConfirmation] = None
            replayed_physical_delta = 0
            replayed_physical_exact = True
            reserved_requests = 0
            if replay_action is not None and replay_action.get("operation") in {
                "confirmation_started",
                "confirmation_failed",
            }:
                if replay_action.get("operation") == "confirmation_started":
                    state.judge_request_uncertainty_count += 1
                if state.judge_requests >= self.max_judge_requests:
                    state._increment_budget("judge_requests")
                confirmation = RootConfirmation(
                    candidate_ref=request.candidate_ref,
                    status="unknown",
                    reason="confirmation_interrupted: the prior in-flight confirmation is not repeated",
                    counterfactual_status="unknown",
                    hypothesis_id=request.hypothesis_id,
                    hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                    defect_fingerprint=request.defect_state.fingerprint,
                    recursive_path=request.recursive_path,
                    seed_binding_identity=request.seed_binding_identity,
                )
                self._record_confirmation(state, queued, confirmation, 0)
                self._checkpoint_state(state, confirmation_action_key)
                self._checkpoint_action(
                    "confirmation_failed",
                    confirmation_action_key,
                    {
                        "status": "unknown",
                        "reason": "confirmation_interrupted",
                        "confirmation": confirmation.to_dict(),
                    },
                )
                continue
            if replay_action is not None and replay_action.get("operation") == "confirmation_completed":
                replay_payload = replay_action.get("payload")
                if not isinstance(replay_payload, Mapping):
                    raise ValueError("completed confirmation action payload is invalid")
                replayed_confirmation = RootConfirmation.from_dict(
                    dict(replay_payload.get("confirmation") or {})
                )
                replayed_physical_delta = int(
                    replay_payload.get("physical_request_delta") or 0
                )
                reserved_requests = int(
                    replay_payload.get("physical_requests_reserved") or 0
                )
                replayed_physical_exact = bool(
                    replay_payload.get("physical_request_exact", True)
                )
            if replay_action is None:
                state.logical_judge_calls += 1
                state.logical_confirmation_calls += 1
                reserved_requests = remaining if bounded_judge else 0
                state.judge_requests += reserved_requests
                self._checkpoint_state(state, confirmation_action_key)
                self._checkpoint_action(
                    "confirmation_started",
                    confirmation_action_key,
                    {
                        "status": "in_flight",
                        "candidate_ref": request.candidate_ref,
                        "hypothesis_id": request.hypothesis_id,
                        "confirmation_identity": confirmation_action_key.split(":", 1)[1],
                        "physical_requests_reserved": reserved_requests,
                    },
                )
            physical_delta = 0
            physical_exact = not bounded_judge
            try:
                if replayed_confirmation is not None:
                    confirmation = replayed_confirmation
                    physical_delta = replayed_physical_delta
                    physical_exact = replayed_physical_exact
                elif bounded_judge:
                    bounded_result = self.judge.confirm_candidate_bounded(
                        request, max_physical_requests=remaining
                    )
                    if not isinstance(bounded_result, BoundedJudgeCallResult):
                        raise TypeError(
                            "bounded Judge must return BoundedJudgeCallResult"
                        )
                    physical_delta = bounded_result.physical_requests
                    physical_exact = True
                    if physical_delta > remaining:
                        raise ValueError("bounded Judge exceeded its physical request allowance")
                    raw = bounded_result.value
                else:
                    raw = self.judge.confirm_candidate_offline(request)
                if replayed_confirmation is None:
                    if not isinstance(raw, RootConfirmation):
                        raise TypeError(
                            "confirmation Judge returned {0}, expected RootConfirmation".format(
                                type(raw).__name__
                            )
                        )
                    confirmation = bind_root_confirmation(raw, request=request)
            except BoundedJudgeCallError as exc:
                physical_delta = exc.physical_requests
                physical_exact = True
                confirmation = RootConfirmation(
                    candidate_ref=request.candidate_ref,
                    status="unknown",
                    reason="confirmation_failed: {0}: {1}".format(type(exc).__name__, exc),
                    counterfactual_status="unknown",
                    hypothesis_id=request.hypothesis_id,
                    hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                    defect_fingerprint=request.defect_state.fingerprint,
                    recursive_path=request.recursive_path,
                    seed_binding_identity=request.seed_binding_identity,
                )
            except (JudgeProviderError, JudgeProviderUnavailable, TypeError, ValueError) as exc:
                confirmation = RootConfirmation(
                    candidate_ref=request.candidate_ref,
                    status="unknown",
                    reason="confirmation_failed: {0}: {1}".format(type(exc).__name__, exc),
                    counterfactual_status="unknown",
                    hypothesis_id=request.hypothesis_id,
                    hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                    defect_fingerprint=request.defect_state.fingerprint,
                    recursive_path=request.recursive_path,
                    seed_binding_identity=request.seed_binding_identity,
                )
            except Exception as exc:
                confirmation = RootConfirmation(
                    candidate_ref=request.candidate_ref,
                    status="unknown",
                    reason="confirmation_capability_error: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    counterfactual_status="unknown",
                    hypothesis_id=request.hypothesis_id,
                    hypothesis_semantic_hash=request.hypothesis_semantic_hash,
                    defect_fingerprint=request.defect_state.fingerprint,
                    recursive_path=request.recursive_path,
                    seed_binding_identity=request.seed_binding_identity,
                )
            if physical_exact:
                state.judge_requests += physical_delta - reserved_requests
            else:
                state.judge_request_uncertainty_count += 1
            if replayed_confirmation is None:
                self._checkpoint_action(
                    "confirmation_completed",
                    confirmation_action_key,
                    {
                        "status": confirmation.status,
                        "physical_requests_reserved": reserved_requests,
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": physical_exact,
                        "confirmation": confirmation.to_dict(),
                        "provider_state": self._capture_provider_result_state(state),
                    },
                )
            elif replay_action is not None:
                self._apply_provider_result_state(state, replay_action["payload"])
            normalized_reason = confirmation.reason.casefold()
            if any(
                marker in normalized_reason
                for marker in (
                    "judge_request_budget_exhausted",
                    "request_budget_exhausted",
                    "budget_exhausted",
                )
            ):
                state._increment_budget("judge_requests")
            self._record_confirmation(state, queued, confirmation, physical_delta)
            if confirmation.status == "rejected":
                backtrack_id = str(
                    state.confirmation_journal[-1].get(
                        "backtracked_to_hypothesis_id"
                    )
                    or ""
                )
                for index, pending in enumerate(pending_confirmations):
                    if pending.get("hypothesis_id") == backtrack_id:
                        pending_confirmations.insert(
                            0, pending_confirmations.pop(index)
                        )
                        break

        self._reconcile_competing_confirmations(state)
        self._rank_confirmed_roots(state)

    def _reconcile_competing_confirmations(self, state: RecursiveAnalysisState) -> None:
        confirmations = {
            item.confirmation_identity: item for item in state.confirmations
        }
        blocked_identities: Set[str] = set()
        reasons: Dict[str, Set[str]] = {}

        def block(identity: str, reason: str) -> None:
            if not identity:
                return
            blocked_identities.add(identity)
            reasons.setdefault(identity, set()).add(reason)

        def comparison_to(
            confirmation: RootConfirmation, target_identity: str
        ) -> Optional[Mapping[str, Any]]:
            matches = [
                item
                for item in confirmation.competitor_comparisons
                if str(item.get("confirmation_identity") or "") == target_identity
            ]
            return matches[0] if len(matches) == 1 else None

        confirmed_items = sorted(
            (
                (identity, confirmation)
                for identity, confirmation in confirmations.items()
                if confirmation.status == "confirmed"
            ),
            key=lambda item: item[0],
        )
        for index, (left_identity, left) in enumerate(confirmed_items):
            for right_identity, right in confirmed_items[index + 1 :]:
                if left.seed_binding_identity != right.seed_binding_identity:
                    continue
                left_to_right = comparison_to(left, right_identity)
                right_to_left = comparison_to(right, left_identity)
                if (
                    left_to_right is None
                    or right_to_left is None
                    or str(left_to_right.get("status") or "") != "co_root"
                    or str(right_to_left.get("status") or "") != "co_root"
                    or left_to_right.get("requires_independent_confirmation")
                    is not True
                    or right_to_left.get("requires_independent_confirmation")
                    is not True
                ):
                    block(left_identity, "confirmed_competitor_graph_inconsistent")
                    block(right_identity, "confirmed_competitor_graph_inconsistent")

        for source_identity, source in sorted(confirmations.items()):
            if source.status != "confirmed":
                continue
            for comparison in source.competitor_comparisons:
                target_identity = str(
                    comparison.get("confirmation_identity") or ""
                )
                status = str(comparison.get("status") or "")
                requires_confirmation = bool(
                    comparison.get("requires_independent_confirmation")
                )
                target = confirmations.get(target_identity)
                comparison_seed = str(
                    comparison.get("seed_binding_identity") or ""
                )
                if target is not None:
                    if target.seed_binding_identity != source.seed_binding_identity:
                        continue
                elif (
                    comparison_seed
                    and comparison_seed != source.seed_binding_identity
                ):
                    continue
                if not requires_confirmation:
                    if status == "co_root":
                        block(source_identity, "co_root_lacks_independent_confirmation")
                    continue
                if target is None:
                    block(source_identity, "competitor_confirmation_missing")
                    continue
                if target.status == "unknown":
                    block(source_identity, "competitor_confirmation_unresolved")
                    continue
                if target.status == "rejected":
                    if status not in {"outperformed", "rejected"}:
                        block(source_identity, "rejected_competitor_relation_conflict")
                    continue

                reciprocal = comparison_to(target, source_identity)
                if reciprocal is None:
                    block(source_identity, "competitor_comparison_missing_reciprocal")
                    block(target_identity, "competitor_comparison_missing_reciprocal")
                    continue
                reciprocal_status = str(reciprocal.get("status") or "")
                if status != "co_root" or reciprocal_status != "co_root":
                    block(source_identity, "confirmed_competitor_relation_conflict")
                    block(target_identity, "confirmed_competitor_relation_conflict")

        if not blocked_identities:
            return
        retained: List[ConfirmedRoot] = []
        for root in state.confirmed_roots:
            identity = str(root.confirmation.get("confirmation_identity") or "")
            if identity not in blocked_identities:
                retained.append(root)
                continue
            state.unresolved_hypothesis_ids.add(root.hypothesis_id)
            if root.node_ref not in state.unresolved_refs:
                state.unresolved_refs.append(root.node_ref)
            state.unresolved_branches.append(
                {
                    "node_ref": root.node_ref,
                    "defect_state_id": root.defect_state.defect_state_id,
                    "hypothesis_id": root.hypothesis_id,
                    "confirmation_identity": identity,
                    "reason": "confirmation_graph_inconsistent",
                    "details": ", ".join(sorted(reasons.get(identity, ()))),
                    "depth": max(0, len(root.recursive_path) - 1),
                }
            )
            seed_key = state.hypothesis_seed_keys.get(root.hypothesis_id, "")
            builder = state.seed_ledger.get(seed_key)
            if builder is not None:
                builder.confirmed_root_refs.discard(root.node_ref)
                builder.mark_unresolved(
                    "confirmation_graph_inconsistent",
                    ", ".join(sorted(reasons.get(identity, ()))),
                )
        state.confirmed_roots = retained

    def _build_confirmation_request(
        self, state: RecursiveAnalysisState, queued: Mapping[str, Any]
    ) -> RootConfirmationRequest:
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        candidate_ref = str(queued.get("candidate_ref") or "")
        fingerprint = str(queued.get("defect_fingerprint") or "")
        seed_binding_identity = str(queued.get("seed_binding_identity") or "")
        hypothesis = state.ledger.get(hypothesis_id)
        if (
            hypothesis.candidate_root_ref != candidate_ref
            or hypothesis.active_defect_fingerprint != fingerprint
            or hypothesis.seed_binding_identity != seed_binding_identity
        ):
            raise ValueError("queued confirmation is cross-bound to another hypothesis")
        binding = next(
            (
                item
                for item in state.introduction_bindings
                if item.get("candidate_ref") == candidate_ref
                and item.get("hypothesis_id") == hypothesis_id
                and item.get("defect_fingerprint") == fingerprint
                and item.get("hypothesis_semantic_hash") == hypothesis.semantic_hash
                and item.get("seed_binding_identity") == seed_binding_identity
            ),
            None,
        )
        if binding is None:
            raise ValueError("queued confirmation has no exact introduction binding")
        defect_state = state.defect_states.get(fingerprint)
        if defect_state is None:
            raise ValueError("queued confirmation defect state is unavailable")
        node = state.graph.nodes.get(candidate_ref)
        if node is None:
            raise ValueError("queued confirmation candidate is unresolved")
        path = tuple(str(ref) for ref in queued.get("recursive_path") or ())
        if not path or path[0] != candidate_ref:
            raise ValueError("queued confirmation path is not candidate-rooted")
        if any(state.graph.resolve(ref) != ref for ref in path):
            raise ValueError("queued confirmation path contains unresolved references")
        for upstream, downstream in zip(path, path[1:]):
            edges = state.graph.edge_context(upstream, downstream)
            if not any(
                is_confirmation_causal_edge(edge, default_eligible=True)
                for edge in edges
            ):
                raise ValueError(
                    "queued confirmation path lacks a grounded non-temporal edge: {0}->{1}".format(
                        upstream, downstream
                    )
                )

        candidate_reference = _reference_envelope(
            candidate_ref,
            content=_node_semantic_content(state.graph.hydrate_node(candidate_ref)),
            fact_kind="candidate_fact",
        )
        candidate_reference["decisive"] = True
        artifact_manifest = _artifact_hydration_manifest(
            state.graph.hydrate_node(candidate_ref)
        )
        if artifact_manifest is not None:
            candidate_reference["artifact_hydration"] = artifact_manifest
        path_references = tuple(_reference_envelope(ref) for ref in path)

        def evidence_facts(refs: Iterable[str], fact_kind: str) -> Tuple[JsonDict, ...]:
            output: List[JsonDict] = []
            for raw_ref in _dedupe_strings(refs):
                resolved = state.graph.resolve(raw_ref)
                if not resolved or resolved not in state.graph.nodes:
                    raise ValueError("confirmation evidence ref is unresolved: {0}".format(raw_ref))
                output.append(
                    _reference_envelope(
                        resolved,
                        content=_node_semantic_content(state.graph.hydrate_node(resolved)),
                        fact_kind=fact_kind,
                    )
                )
            return tuple(output)

        supporting_refs = [candidate_ref]
        supporting_refs.extend(item.ref for item in hypothesis.supporting_evidence)
        supporting_refs.extend(queued.get("checked_evidence_refs") or ())
        opposing_refs = [item.ref for item in hypothesis.opposing_evidence]
        competitors: List[JsonDict] = []
        for value in state.ledger.snapshot():
            if value.get("hypothesis_id") == hypothesis_id:
                continue
            competitor_hypothesis_id = str(value.get("hypothesis_id") or "")
            competitor_fingerprint = str(
                value.get("active_defect_fingerprint") or ""
            )
            competitor_semantic_hash = str(value.get("semantic_hash") or "")
            competitor_seed_binding_identity = str(
                value.get("seed_binding_identity") or ""
            )
            if competitor_seed_binding_identity != seed_binding_identity:
                continue
            competitor_binding = next(
                (
                    item
                    for item in state.introduction_bindings
                    if str(item.get("hypothesis_id") or "")
                    == competitor_hypothesis_id
                    and str(item.get("defect_fingerprint") or "")
                    == competitor_fingerprint
                    and str(item.get("hypothesis_semantic_hash") or "")
                    == competitor_semantic_hash
                    and str(item.get("seed_binding_identity") or "")
                    == competitor_seed_binding_identity
                ),
                None,
            )
            if competitor_binding is None:
                continue
            competitor_ref = str(value.get("candidate_root_ref") or "")
            resolved = state.graph.resolve(competitor_ref)
            if not resolved:
                raise ValueError("competing hypothesis candidate is unresolved")
            if str(competitor_binding.get("candidate_ref") or "") != resolved:
                continue
            if resolved in path[1:]:
                continue
            support = value.get("supporting_evidence") or []
            opposition = value.get("opposing_evidence") or []

            def competitor_evidence(items: Any, kind: str) -> List[JsonDict]:
                output: List[JsonDict] = []
                for item in items if isinstance(items, (list, tuple)) else ():
                    if not isinstance(item, Mapping):
                        continue
                    ref = str(item.get("ref") or "")
                    resolved_ref = state.graph.resolve(ref)
                    if not resolved_ref or resolved_ref not in state.graph.nodes:
                        raise ValueError("competing hypothesis evidence is unresolved")
                    output.append(
                        {
                            "reason": str(item.get("reason") or ""),
                            "confidence": float(item.get("confidence", 0.0)),
                            "evidence_reference": _reference_envelope(
                                resolved_ref,
                                content=_node_semantic_content(
                                    state.graph.hydrate_node(resolved_ref)
                                ),
                                fact_kind=kind,
                            ),
                        }
                    )
                return output

            competitor_defect = state.defect_states.get(competitor_fingerprint)
            if competitor_defect is None:
                raise ValueError("competing hypothesis defect is unresolved")
            queued_competitor = next(
                (
                    item
                    for item in state.confirmation_queue
                    if str(item.get("hypothesis_id") or "") == competitor_hypothesis_id
                    and str(item.get("candidate_ref") or "") == resolved
                    and str(item.get("defect_fingerprint") or "")
                    == competitor_fingerprint
                    and str(item.get("seed_binding_identity") or "")
                    == competitor_seed_binding_identity
                ),
                None,
            )
            competitor_node = state.graph.nodes.get(resolved)
            if (
                queued_competitor is None
                or competitor_node is None
                or not root_candidate_eligible(competitor_node)
            ):
                continue
            competitor_path = tuple(
                str(item)
                for item in (
                    queued_competitor.get("recursive_path")
                    if queued_competitor is not None
                    else ()
                )
            )
            competitor_confirmation_identity = confirmation_identity_for(
                hypothesis_id=competitor_hypothesis_id,
                hypothesis_semantic_hash=competitor_semantic_hash,
                candidate_ref=resolved,
                defect_fingerprint=competitor_fingerprint,
                recursive_path=competitor_path,
                seed_binding_identity=competitor_seed_binding_identity,
            )
            competitors.append(
                {
                    "hypothesis_id": competitor_hypothesis_id,
                    "hypothesis_semantic_hash": competitor_semantic_hash,
                    "confirmation_identity": competitor_confirmation_identity,
                    "recursive_path": list(competitor_path),
                    "seed_binding_identity": competitor_seed_binding_identity,
                    "requires_independent_confirmation": queued_competitor is not None,
                    "status": str(value.get("status") or "unresolved"),
                    "claim": str(value.get("claim") or ""),
                    "active_defect": competitor_defect.to_dict(),
                    "candidate_reference": _reference_envelope(
                        resolved,
                        content=_node_semantic_content(state.graph.hydrate_node(resolved)),
                        fact_kind="competing_hypothesis_candidate",
                    ),
                    "supporting_evidence": competitor_evidence(
                        support, "competing_hypothesis_support"
                    ),
                    "opposing_evidence": competitor_evidence(
                        opposition, "competing_hypothesis_opposition"
                    ),
                    "unresolved_questions": list(value.get("unresolved_questions") or ()),
                    "counterfactual": copy.deepcopy(
                        value.get("counterfactual")
                        or {
                            "intervention_ref": resolved,
                            "intervention_kind": "replace_with_semantically_correct_behavior",
                            "causal_question": "Would this intervention prevent the active defect?",
                        }
                    ),
                }
            )

        obligations: List[JsonDict] = []
        for item in queued.get("task_obligations") or ():
            if not isinstance(item, Mapping):
                continue
            safe = {
                key: copy.deepcopy(item[key])
                for key in (
                    "source",
                    "text",
                    "description",
                    "expected",
                    "requirement",
                    "criterion",
                )
                if key in item and item[key] not in (None, "", [], {})
            }
            if safe:
                obligations.append(safe)

        return RootConfirmationRequest(
            candidate_ref=candidate_ref,
            defect_state=defect_state,
            recursive_path=path,
            candidate_reference=candidate_reference,
            recursive_path_references=path_references,
            supporting_evidence=evidence_facts(
                supporting_refs, "supporting_evidence"
            ),
            opposing_evidence=evidence_facts(
                opposing_refs, "opposing_evidence"
            ),
            competing_hypotheses=tuple(competitors),
            task_obligations=tuple(obligations),
            analysis_perspective="",
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis.semantic_hash,
            seed_binding_identity=seed_binding_identity,
        )

    def _record_confirmation(
        self,
        state: RecursiveAnalysisState,
        queued: JsonDict,
        confirmation: RootConfirmation,
        physical_request_delta: int,
    ) -> None:
        queued["status"] = confirmation.status
        queued["confirmation"] = confirmation.to_dict()
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        seed_key = str(
            queued.get("seed_key")
            or state.hypothesis_seed_keys.get(hypothesis_id, "")
        )
        seed_builder = state.seed_ledger.get(seed_key)
        if seed_builder is not None:
            seed_builder.record_confirmation(confirmation)
        state.confirmations.append(confirmation)
        state.confirmation_journal.append(
            {
                "semantic_identity": queued.get("semantic_identity"),
                "candidate_ref": confirmation.candidate_ref,
                "hypothesis_id": hypothesis_id,
                "defect_fingerprint": confirmation.defect_fingerprint,
                "seed_binding_identity": confirmation.seed_binding_identity,
                "seed_key": seed_key,
                "recursive_path": list(confirmation.recursive_path),
                "status": confirmation.status,
                "physical_request_delta": physical_request_delta,
                "confirmation": confirmation.to_dict(),
            }
        )
        if confirmation.status == "confirmed":
            state.introduction_hypothesis_ids.discard(hypothesis_id)
            state.unresolved_hypothesis_ids.discard(hypothesis_id)
            node = state.graph.nodes[confirmation.candidate_ref]
            defect_state = state.defect_states[confirmation.defect_fingerprint]
            state.confirmed_roots.append(
                ConfirmedRoot(
                    node_ref=confirmation.candidate_ref,
                    defect_state=defect_state,
                    reason=confirmation.reason,
                    counterfactual=confirmation.counterfactual,
                    confidence=confirmation.confidence,
                    evidence_refs=confirmation.evidence_refs,
                    component=node.component,
                    event_type=node.event_type,
                    defect_type=defect_state.label,
                    observed_defect_refs=(
                        (seed_builder.start_ref,)
                        if seed_builder is not None
                        else state.start_refs
                    ),
                    hypothesis_id=hypothesis_id,
                    recursive_path=confirmation.recursive_path,
                    excerpt=confirmation.excerpt,
                    provenance={
                        "confirmation_semantic_identity": queued.get("semantic_identity"),
                        "defect_fingerprint": confirmation.defect_fingerprint,
                    },
                    confirmation=confirmation.to_dict(),
                )
            )
            return
        if confirmation.status == "rejected":
            state.introduction_hypothesis_ids.discard(hypothesis_id)
            state.unresolved_hypothesis_ids.discard(hypothesis_id)
            hypothesis = state.ledger.get(hypothesis_id)
            if hypothesis.status in {"active", "supported"}:
                state.ledger.reject(hypothesis_id, confirmation.reason)
            if confirmation.factor_role in {
                "contributing_condition",
                "amplifying_factor",
            }:
                factor = CausalFactor(
                    node_ref=confirmation.candidate_ref,
                    relation=(
                        "amplifying_factor"
                        if confirmation.factor_role == "amplifying_factor"
                        else "contributing_condition"
                    ),
                    reason=confirmation.reason,
                    confidence=confirmation.confidence,
                    evidence_refs=confirmation.evidence_refs,
                    recursive_path=confirmation.recursive_path,
                    factor_label="{0} for {1}".format(
                        confirmation.factor_role.replace("_", " "),
                        state.analysis_perspective,
                    ),
                    confirmation_status="rejected",
                    confirmation=confirmation.to_dict(),
                    provenance={
                        "confirmation_semantic_identity": queued.get("semantic_identity"),
                        "defect_fingerprint": confirmation.defect_fingerprint,
                    },
                    mechanism=dict(confirmation.factor_mechanism),
                )
                if confirmation.factor_role == "amplifying_factor":
                    state.amplifying_factors.append(factor)
                else:
                    state.contributing_conditions.append(factor)
            else:
                state.rejected_candidates.append(
                    RejectedCandidate(
                        confirmation.candidate_ref,
                        confirmation.reason,
                        confirmation.evidence_refs,
                        hypothesis_id=hypothesis_id,
                        recursive_path=confirmation.recursive_path,
                        confirmation_status="rejected",
                        confidence=confirmation.confidence,
                        confirmation=confirmation.to_dict(),
                        provenance={
                            "confirmation_semantic_identity": queued.get(
                                "semantic_identity"
                            ),
                            "defect_fingerprint": confirmation.defect_fingerprint,
                        },
                    )
                )
            pending_alternatives = [
                item
                for item in state.confirmation_queue
                if item.get("status") == "queued"
                and item.get("hypothesis_id") != hypothesis_id
            ]
            pending_alternatives.sort(
                key=lambda item: (
                    -sum(
                        evidence.confidence
                        for evidence in state.ledger.get(
                            str(item.get("hypothesis_id") or "")
                        ).supporting_evidence
                    ),
                    str(item.get("hypothesis_id") or ""),
                )
            )
            state.confirmation_journal[-1]["backtracked_to_hypothesis_id"] = (
                str(pending_alternatives[0].get("hypothesis_id") or "")
                if pending_alternatives
                else ""
            )
            state.confirmation_journal[-1]["backtrack_status"] = (
                "pending_confirmation_queue"
                if pending_alternatives
                else "no_queued_unresolved_alternative"
            )
            return

        state.unresolved_hypothesis_ids.add(hypothesis_id)
        state.unresolved_refs.append(confirmation.candidate_ref)
        state.unresolved_branches.append(
            {
                "node_ref": confirmation.candidate_ref,
                "defect_state_id": "defect:{0}".format(
                    confirmation.defect_fingerprint
                ),
                "hypothesis_id": hypothesis_id,
                "reason": "root_confirmation_unknown",
                "details": confirmation.reason,
                "depth": max(0, len(confirmation.recursive_path) - 1),
            }
        )

    def _rank_confirmed_roots(self, state: RecursiveAnalysisState) -> None:
        perspective = _perspective_tokens(state.analysis_perspective)

        def rank(root: ConfirmedRoot) -> Tuple[int, float, int, int, str, str]:
            node = state.graph.nodes[root.node_ref]
            semantic_tokens = _perspective_tokens(_node_semantic_content(node))
            return (
                -len(perspective.intersection(semantic_tokens)),
                -root.confidence,
                len(root.recursive_path),
                state.graph.position(root.node_ref),
                root.node_ref,
                root.hypothesis_id,
            )

        ordered = sorted(state.confirmed_roots, key=rank)
        state.confirmed_roots = ordered[:1]
        state.co_roots = ordered[1:]

    def _handle_investigation(
        self,
        *,
        state: RecursiveAnalysisState,
        tools: CausalInvestigationTools,
        item: FrontierItem,
        judgment: CausalStepJudgment,
        request: CausalStepRequest,
    ) -> str:
        suggestion = judgment.suggested_investigation
        if not isinstance(suggestion, Mapping):
            return "not_handled"
        if suggestion.get("kind") == "judge_retry" and not suggestion.get("action"):
            return "not_handled_internal_diagnostic"
        if suggestion.get("action"):
            return self._apply_control_directive(
                state, item, judgment, request, suggestion
            )
        try:
            directive = InvestigationDirective.from_suggestion(
                suggestion,
                requested_by_ref=item.node_ref,
                hypothesis_id=item.hypothesis_id,
            )
        except ValueError as exc:
            reason = "invalid_investigation_directive: {0}".format(exc)
            state.record_terminal_action(
                item=item,
                judgment=judgment,
                request=request,
                directive={
                    "directive_kind": "evidence_investigation",
                    "tool_name": str(suggestion.get("tool") or ""),
                    "requested_by_ref": item.node_ref,
                    "hypothesis_id": item.hypothesis_id,
                },
                status="rejected",
                rejection_reason=reason,
            )
            state._mark_ref_unresolved(
                item.node_ref,
                item,
                "investigation_rejected",
                str(exc),
            )
            return "rejected"
        if not self._evidence_investigation_eligible(judgment, request, suggestion):
            result = InvestigationResult.rejected(
                directive, "investigation_not_evidence_eligible"
            )
            state.record_investigation_result(
                item, directive, result, judgment, request
            )
            return "rejected"
        investigation_action_key = "investigation:{0}".format(directive.directive_id)
        replay_action = self._replay_action(state, investigation_action_key)
        if replay_action is not None and replay_action.get("operation") == "investigation_started":
            state.record_terminal_action(
                item=item,
                judgment=judgment,
                request=request,
                directive=directive.to_dict(),
                status="rejected",
                rejection_reason="interrupted_investigation_call",
                extra={"resume_policy": "never_repeat_inflight_call"},
            )
            state._mark_ref_unresolved(
                item.node_ref,
                item,
                "interrupted_investigation_call",
                "The in-flight local investigation is not repeated and no result is fabricated.",
            )
            state.frontier.complete_if_in_flight(
                item, "unresolved:interrupted_investigation_call"
            )
            self._checkpoint_state(state, investigation_action_key)
            self._checkpoint_action(
                "investigation_failed",
                investigation_action_key,
                {
                    "directive_id": directive.directive_id,
                    "status": "unknown",
                    "reason": "interrupted_investigation_call",
                },
            )
            return "completed"
        replayed_result: Optional[InvestigationResult] = None
        if replay_action is not None and replay_action.get("operation") == "investigation_completed":
            replay_payload = replay_action.get("payload")
            if not isinstance(replay_payload, Mapping):
                raise ValueError("completed investigation action payload is invalid")
            replayed_result = InvestigationResult.from_dict(
                dict(replay_payload.get("result") or {})
            )
        if replay_action is None and state.investigation_rounds >= self.max_investigation_rounds:
            state._increment_budget("investigation_rounds")
            result = InvestigationResult.rejected(
                directive, "investigation_round_budget_exhausted"
            )
            state.record_investigation_result(
                item, directive, result, judgment, request
            )
            state._mark_ref_unresolved(
                item.node_ref,
                item,
                "investigation_budget_exhausted",
                "The exact investigation round budget is exhausted.",
            )
            return "exhausted"
        if replay_action is None:
            state.investigation_rounds += 1
            self._checkpoint_state(state, investigation_action_key)
            self._checkpoint_action(
                "investigation_started",
                investigation_action_key,
                {
                    "directive_id": directive.directive_id,
                    "directive": directive.to_dict(),
                    "visit_key": item.visit_key,
                    "status": "in_flight",
                },
            )
        tools.max_artifact_bytes = tools.artifact_bytes_used + max(
            0, self.max_artifact_bytes - state.artifact_bytes
        )
        result = replayed_result if replayed_result is not None else tools.execute(directive)
        if replayed_result is None:
            self._checkpoint_action(
                "investigation_completed",
                investigation_action_key,
                {
                    "directive_id": directive.directive_id,
                    "status": result.status,
                    "result": result.to_dict(),
                },
            )
        state.investigation_result_bytes += result.byte_count
        if result.status == "success" and result.artifact_byte_count:
            state.artifact_bytes += result.artifact_byte_count
        changed = state.record_investigation_result(
            item, directive, result, judgment, request
        )
        if changed:
            judgment_hash = state.record_intermediate_judgment(item, judgment)
            state.frontier.mark_completed(item, judgment_hash)
            reopened = state.frontier.reopen(
                item,
                evidence_hash=result.evidence_hash,
                reason="investigation_evidence_changed",
            )
            if reopened:
                return "reopened"
            state._mark_ref_unresolved(
                item.node_ref,
                item,
                "investigation_reentry_failed",
                "Changed evidence could not reopen the completed semantic visit.",
            )
            return "completed"
        reason = (
            "investigation_unchanged"
            if result.status == "unchanged"
            else "investigation_rejected"
        )
        if result.rejection_reason == "artifact_byte_budget_exhausted":
            state._increment_budget("artifact_bytes")
            reason = "artifact_byte_limit"
        state._mark_ref_unresolved(
            item.node_ref,
            item,
            reason,
            result.rejection_reason or result.error or "No new grounded evidence was produced.",
        )
        return "unchanged"

    @staticmethod
    def _evidence_investigation_eligible(
        judgment: CausalStepJudgment,
        request: CausalStepRequest,
        suggestion: Mapping[str, Any],
    ) -> bool:
        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            return True
        if any(
            item.relation == "unknown" or item.missing_evidence
            for item in judgment.predecessors
        ):
            return True
        context = request.recursive_context
        if any(
            context.get(key)
            for key in ("missing_artifacts", "truncated_artifacts", "unresolved_references")
        ):
            return True
        return False

    def _apply_control_directive(
        self,
        state: RecursiveAnalysisState,
        item: FrontierItem,
        judgment: CausalStepJudgment,
        request: CausalStepRequest,
        suggestion: Mapping[str, Any],
    ) -> str:
        try:
            directive = AttributionControlDirective.from_suggestion(
                suggestion, requested_by_ref=item.node_ref
            )
        except ValueError as exc:
            reason = "invalid_control_directive: {0}".format(exc)
            state.record_terminal_action(
                item=item,
                judgment=judgment,
                request=request,
                directive={
                    "directive_kind": "attribution_control",
                    "action": str(suggestion.get("action") or ""),
                    "requested_by_ref": item.node_ref,
                },
                status="rejected",
                rejection_reason=reason,
            )
            return "rejected"
        if directive.directive_id in state.control_directive_ids:
            state.record_terminal_action(
                item=item,
                judgment=judgment,
                request=request,
                directive=directive.to_dict(),
                status="unchanged",
                rejection_reason="duplicate_control_directive",
            )
            return "control"
        state.control_directive_ids.add(directive.directive_id)
        before = state.ledger.snapshot()
        arguments = directive.arguments
        status = "applied"
        rejection_reason = ""
        try:
            if directive.action == "record_hypothesis":
                candidate_ref = str(arguments["candidate_ref"])
                resolved = state.graph.resolve(candidate_ref)
                if not resolved:
                    raise ValueError("candidate_ref is unresolved")
                proposed = AttributionHypothesis.create(
                    str(arguments["claim"]),
                    resolved,
                    item.defect_state,
                    seed_binding_identity=(
                        state._seed_builder_for_item(item).key
                        if state._seed_builder_for_item(item) is not None
                        else ""
                    ),
                )
                existing_ids = {
                    str(value.get("hypothesis_id") or "") for value in before
                }
                if (
                    proposed.hypothesis_id not in existing_ids
                    and len(before) >= self.max_hypotheses
                ):
                    raise ValueError("hypothesis budget exhausted")
                created = state.ledger.create(
                    str(arguments["claim"]),
                    resolved,
                    item.defect_state,
                    seed_binding_identity=(
                        state._seed_builder_for_item(item).key
                        if state._seed_builder_for_item(item) is not None
                        else ""
                    ),
                )
                state._bind_hypothesis_to_seed(
                    created.hypothesis_id,
                    state._seed_builder_for_item(item),
                )
            elif directive.action == "reject_hypothesis":
                hypothesis_id = str(arguments["hypothesis_id"])
                if hypothesis_id != item.hypothesis_id:
                    raise ValueError(
                        "reject_hypothesis must target the exact active hypothesis"
                    )
                opposing = tuple(
                    str(ref) for ref in arguments.get("opposing_evidence_refs") or []
                )
                resolved = [state.graph.resolve(ref) for ref in opposing]
                if opposing and any(ref is None for ref in resolved):
                    raise ValueError("opposing evidence contains unresolved refs")
                rejection_hash = hashlib.sha256(
                    stable_json(
                        {
                            "directive": directive.to_dict(),
                            "resolved_opposition": resolved,
                        }
                    ).encode("utf-8")
                ).hexdigest()
                state.ledger.reject_with_frontier(
                    hypothesis_id,
                    directive.reason,
                    opposing_refs=(str(ref) for ref in resolved),
                    frontier=state.frontier,
                    evidence_hash=rejection_hash,
                )
            elif directive.action == "request_root_confirmation":
                hypothesis_id = str(arguments["hypothesis_id"])
                if hypothesis_id != item.hypothesis_id:
                    raise ValueError("confirmation hypothesis_id must exactly match the active hypothesis")
                hypothesis = state.ledger.get(hypothesis_id)
                candidate_ref = state.graph.resolve(str(arguments["candidate_ref"]))
                if not candidate_ref:
                    raise ValueError("candidate_ref is unresolved")
                defect_fingerprint = str(arguments["defect_fingerprint"])
                if (
                    candidate_ref != item.node_ref
                    or candidate_ref != hypothesis.candidate_root_ref
                ):
                    raise ValueError("confirmation candidate_ref does not match the active hypothesis root")
                if (
                    defect_fingerprint != item.defect_state.fingerprint
                    or defect_fingerprint != hypothesis.active_defect_fingerprint
                ):
                    raise ValueError("confirmation defect_fingerprint does not match the active defect")
                binding_exists = any(
                    binding.get("candidate_ref") == candidate_ref
                    and binding.get("hypothesis_id") == hypothesis_id
                    and binding.get("defect_fingerprint") == defect_fingerprint
                    and binding.get("seed_binding_identity")
                    == hypothesis.seed_binding_identity
                    for binding in state.introduction_bindings
                )
                if not binding_exists:
                    raise ValueError("confirmation requires an existing introduction binding")
                queue_key = (
                    hypothesis_id,
                    candidate_ref,
                    defect_fingerprint,
                    hypothesis.seed_binding_identity,
                )
                state.enqueue_confirmation(
                    {
                        "hypothesis_id": hypothesis_id,
                        "candidate_ref": candidate_ref,
                        "defect_fingerprint": defect_fingerprint,
                        "seed_binding_identity": hypothesis.seed_binding_identity,
                        "requested_by_ref": item.node_ref,
                        "recursive_path": list(item.downstream_path),
                        "checked_evidence_refs": sorted(
                            state.visit_evidence.get(item.visit_key, set())
                        ),
                        "task_obligations": copy.deepcopy(
                            list(request.recursive_context.get("task_obligations") or [])
                        ),
                        "analysis_perspective": state.analysis_perspective,
                        "semantic_identity": hashlib.sha256(
                            stable_json(queue_key).encode("utf-8")
                        ).hexdigest(),
                        "status": "queued",
                        "seed_key": state.hypothesis_seed_keys.get(
                            hypothesis_id, ""
                        ),
                    }
                )
                status = "deferred"
        except (KeyError, ValueError) as exc:
            status = "rejected"
            rejection_reason = str(exc)
        after = state.ledger.snapshot()
        state.record_terminal_action(
            item=item,
            judgment=judgment,
            request=request,
            directive=directive.to_dict(),
            status=status,
            rejection_reason=rejection_reason,
            extra={
                "ledger_before": before,
                "ledger_after": after,
                "ledger_before_hash": hashlib.sha256(
                    stable_json(before).encode("utf-8")
                ).hexdigest(),
                "ledger_after_hash": hashlib.sha256(
                    stable_json(after).encode("utf-8")
                ).hexdigest(),
            },
        )
        if directive.action == "reject_hypothesis" and status == "applied":
            return "branch_rejected"
        if directive.action == "reject_hypothesis" and status == "rejected":
            return "rejected"
        return "control"


__all__ = [
    "AgenticRecursiveAnalyzer",
    "RecursiveAnalysisState",
    "SeedAttributionBuilder",
]
