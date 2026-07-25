"""Bounded recursive semantic-defect traversal for offline causal attribution."""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .causal_judge import (
    BoundedJudgeCallResult,
    BoundedJudgeCallError,
    BoundedJudgeCapability,
    CausalJudge,
    CausalStepRequest,
    OfflineJudgeCapability,
    ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX,
    RootConfirmationRequest,
    bind_root_confirmation,
    preflight_root_confirmation_request,
    root_confirmation_request_identity,
    root_confirmation_request_projection,
    root_confirmation_request_projection_identity,
    validate_root_confirmation_request_projection,
)
from .causal_retrieval import (
    SemanticPredecessorRetriever,
    authored_root_candidate_eligible,
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
    LocalStateOwner,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RejectedCandidate,
    RootConfirmation,
    SeedAttributionResult,
    canonical_causal_factor_publication,
    canonical_confirmed_root_publication,
    canonical_ranked_root_publications,
    canonical_rejected_candidate_publication,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    is_definitive_confirmation,
    seed_defect_state,
    semantic_visit_key,
    seed_binding_identity_for,
    validate_confirmation_ownership,
    validate_modern_report_shape,
    validate_root_confirmation_substantive_invariants,
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
    validate_global_candidate_request_against_graph,
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
ACTION_STATE_SCHEMA = "recursive-analysis-actions/v17"
GLOBAL_JUDGE_ACTION_OPERATIONS = frozenset(
    {
        "global_judge_started",
        "global_judge_completed",
        "global_judge_failed",
    }
)
GLOBAL_JUDGE_ACTION_BASE_KEYS = frozenset(
    {
        "status",
        "pass_identity",
        "seed_binding_identity",
        "seed_ref",
        "defect_fingerprint",
        "hypothesis_id",
        "visit_key",
        "owner",
        "request_identity",
        "validation_envelope",
        "capsule_identity",
        "candidate_compression",
        "physical_requests_reserved",
    }
)
GLOBAL_JUDGE_STARTED_PAYLOAD_KEYS = GLOBAL_JUDGE_ACTION_BASE_KEYS
GLOBAL_JUDGE_COMPLETED_PAYLOAD_KEYS = frozenset(
    {
        *GLOBAL_JUDGE_ACTION_BASE_KEYS,
        "physical_request_delta",
        "physical_request_exact",
        "judgment",
        "provider_state",
    }
)
GLOBAL_JUDGE_FAILED_PAYLOAD_KEYS = frozenset(
    {
        *GLOBAL_JUDGE_ACTION_BASE_KEYS,
        "physical_request_delta",
        "physical_request_exact",
        "blocker",
        "detail",
        "failure_projection",
        "provider_state",
    }
)
GLOBAL_FAILURE_PROJECTION_SCHEMA = "global-candidate-failure-projection/v4"
GLOBAL_FAILURE_PROJECTION_KEYS = frozenset(
    {
        "schema",
        "terminal_status",
        "pass_identity",
        "seed_binding_identity",
        "seed_ref",
        "defect_fingerprint",
        "blocker",
        "reason",
        "detail",
        "missing_evidence",
        "physical_request_delta",
        "physical_request_exact",
        "owner",
    }
)
COMPLETED_GLOBAL_PASS_KEYS = frozenset(
    {
        "kind",
        "status",
        "pass_identity",
        "seed_binding_identity",
        "seed_ref",
        "defect_fingerprint",
        "hypothesis_id",
        "visit_key",
        "owner",
        "physical_request_delta",
        "candidate_compression",
        "candidate_evidence_capsules",
        "judgment",
        "behavior_impact",
    }
)
FAILED_GLOBAL_PASS_KEYS = frozenset(
    {
        "kind",
        "status",
        "pass_identity",
        "seed_binding_identity",
        "seed_ref",
        "defect_fingerprint",
        "hypothesis_id",
        "visit_key",
        "owner",
        "blocker",
        "reason",
        "missing_evidence",
        "physical_request_delta",
        "physical_request_exact",
        "candidate_compression",
        "failure_projection",
        "behavior_impact",
    }
)
FAILED_GLOBAL_EPISODE_KEYS = frozenset(
    {
        "node_ref",
        "defect_state_id",
        "hypothesis_id",
        "reason",
        "details",
        "depth",
        "global_pass_identity",
        "owner",
        "failure_projection",
    }
)
GLOBAL_TERMINAL_MARKER_KEYS = frozenset(
    {
        "global_pass_identity",
        "pass_identity",
        "failure_projection",
        "terminal_status",
    }
)
CONFIRMATION_ACTION_OPERATIONS = frozenset(
    {"confirmation_completed", "confirmation_failed"}
)
CONFIRMATION_STARTED_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "candidate_ref",
        "hypothesis_id",
        "request_identity",
        "physical_requests_reserved",
    }
)
CONFIRMATION_ACTION_PROJECTION_KEYS = frozenset(
    {
        "operation",
        "semantic_key",
        "request_identity",
        "response_identity",
        "owner",
        "status",
        "candidate_ref",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "defect_fingerprint",
        "seed_binding_identity",
        "seed_key",
        "recursive_path",
        "evidence_refs",
        "artifact_evidence_envelopes",
        "evidence_disposition",
        "factual_request_projection",
        "physical_requests_reserved",
        "physical_request_delta",
        "physical_request_exact",
        "confirmation",
    }
)
PENDING_CONFIRMATION_REQUIRED_KEYS = frozenset(
    {
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "candidate_ref",
        "defect_fingerprint",
        "seed_binding_identity",
        "semantic_identity",
        "status",
        "owner",
        "analysis_perspective",
        "artifact_evidence_envelopes",
        "factual_request_projection",
    }
)
PENDING_CONFIRMATION_ALLOWED_KEYS = frozenset(
    {
        *PENDING_CONFIRMATION_REQUIRED_KEYS,
        "seed_key",
        "requested_by_ref",
        "recursive_path",
        "checked_evidence_refs",
        "task_obligations",
        "origin",
    }
)
TERMINAL_CONFIRMATION_REQUIRED_KEYS = frozenset(
    {
        *PENDING_CONFIRMATION_REQUIRED_KEYS,
        "confirmation",
        "response_identity",
        "evidence_disposition",
    }
)
TERMINAL_CONFIRMATION_ALLOWED_KEYS = frozenset(
    {
        *PENDING_CONFIRMATION_ALLOWED_KEYS,
        "confirmation",
        "response_identity",
        "evidence_disposition",
    }
)
TERMINAL_EVIDENCE_DISPOSITION_SCHEMA = (
    "root-confirmation-terminal-evidence-disposition/v1"
)
TERMINAL_EVIDENCE_DISPOSITION_KEYS = frozenset(
    {
        "schema",
        "state",
        "snapshot_identity",
        "rejection_reason",
        "graph_comparison_facts",
    }
)
ARTIFACT_EVIDENCE_COMPARISON_KEYS = frozenset(
    {
        "schema",
        "canonical_ref",
        "expected_owner_ref",
        "artifact_reference_status",
        "active_owner_refs",
        "file_verification",
        "validation_status",
        "rejection_reason",
    }
)
STEP_ACTION_PROJECTION_SCHEMA = "step-action-projection/v1"
STEP_ACTION_PROJECTION_KEYS = frozenset(
    {
        "schema",
        "semantic_key",
        "call_kind",
        "seed_binding_identity",
        "hypothesis_id",
        "visit_key",
        "owner",
        "physical_requests_reserved",
        "physical_request_delta",
        "physical_request_exact",
        "provider_judgment",
        "step_judgment",
        "causal_relations",
    }
)
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
PROVIDER_CACHE_STATS_KEYS = {
    "enabled",
    "path",
    "loaded_entries",
    "hits",
    "misses",
    "writes",
    "invalid_entries",
    "corrupt_entries",
    "write_error_count",
    "write_errors",
}
PROVIDER_CACHE_COUNT_KEYS = {
    "loaded_entries",
    "hits",
    "misses",
    "writes",
    "invalid_entries",
    "corrupt_entries",
    "write_error_count",
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


def _global_envelope_authoritative_candidates(
    graph: TraceGraph,
    envelope: Any,
    candidates: Sequence[CausalCandidate],
) -> Tuple[CausalCandidate, ...]:
    if not isinstance(envelope, Mapping):
        raise TypeError("global candidate validation envelope must be an object")
    capsules = envelope.get("candidate_evidence_capsules")
    if not isinstance(capsules, list):
        raise TypeError(
            "global candidate validation envelope capsules must be an array"
        )
    requested_refs = {
        graph.resolve(str(capsule.get("candidate_ref") or ""))
        or str(capsule.get("candidate_ref") or "")
        for capsule in capsules
        if isinstance(capsule, Mapping)
        and str(capsule.get("candidate_ref") or "")
    }
    authoritative = tuple(
        candidate
        for candidate in candidates
        if (graph.resolve(candidate.ref) or candidate.ref) in requested_refs
    )
    authoritative_refs = {
        graph.resolve(candidate.ref) or candidate.ref
        for candidate in authoritative
    }
    if authoritative_refs != requested_refs:
        raise ValueError(
            "global candidate validation envelope has no complete "
            "authoritative candidate route set"
        )
    return authoritative


def _assert_global_envelope_matches_completed_pass(
    report: RecursiveAttributionReport,
    seed: SeedAttributionResult,
    envelope: Any,
) -> None:
    judgment = seed.global_judgment
    owner = LocalStateOwner.from_dict(judgment.get("owner"))
    matching_passes = [
        item
        for item in report.investigation_journal
        if isinstance(item, Mapping)
        and item.get("kind") == "global_candidate_pass"
        and item.get("status") == "completed"
        and LocalStateOwner.from_dict(item.get("owner")) == owner
    ]
    if len(matching_passes) != 1:
        raise ValueError(
            "global judgment has no unique completed pass capsule authority"
        )
    pass_capsules = matching_passes[0].get(
        "candidate_evidence_capsules"
    )
    envelope_capsules = (
        envelope.get("candidate_evidence_capsules")
        if isinstance(envelope, Mapping)
        else None
    )
    if not isinstance(pass_capsules, (list, tuple)) or not isinstance(
        envelope_capsules,
        (list, tuple),
    ):
        raise ValueError(
            "global judgment capsule authority is incomplete"
        )
    if stable_json(_checkpoint_json(pass_capsules)) != stable_json(
        _checkpoint_json(envelope_capsules)
    ):
        raise ValueError(
            "global validation envelope capsules contradict the completed pass"
        )


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
    confirmation_artifact_refs: Set[str] = set()
    for seed in report.seed_results:
        if seed.confirmed_root_refs or seed.confirmation_identities:
            identity_refs.append(seed.start_ref)
        identity_refs.extend(seed.selected_candidate_refs)
        identity_refs.extend(seed.confirmed_root_refs)
        refs.extend(seed.decisive_evidence_refs)
        judgment = seed.global_judgment
        if judgment:
            envelope = seed.to_dict()["global_judgment"].get(
                "validation_envelope"
            )
            _assert_global_envelope_matches_completed_pass(
                report,
                seed,
                envelope,
            )
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=(
                    _global_envelope_authoritative_candidates(
                        graph,
                        envelope,
                        report.causal_candidates,
                    )
                ),
                authoritative_objective=report.objective,
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
        if candidate is not None and not authored_root_candidate_eligible(
            graph, confirmation.candidate_ref
        ):
            raise ValueError(
                "{0} confirmation candidate is not authored-root eligible for the active revision".format(
                    label
                )
            )
        identity_refs.append(confirmation.candidate_ref)
        identity_refs.extend(confirmation.recursive_path)
        refs.extend(confirmation.evidence_refs)
        artifact_refs = {
            ref
            for ref in confirmation.evidence_refs
            if graph.resolve(ref) not in graph.nodes
        }
        confirmation_artifact_refs.update(artifact_refs)
        matching_projections = [
            item
            for item in report.metadata.get(
                "confirmation_action_projection", ()
            )
            if isinstance(item, Mapping)
            and isinstance(item.get("confirmation"), Mapping)
            and str(
                item["confirmation"].get("confirmation_identity") or ""
            )
            == confirmation.confirmation_identity
        ]
        if len(matching_projections) > 1 or (
            artifact_refs and len(matching_projections) != 1
        ):
            raise ValueError(
                "{0} confirmation has no unique terminal artifact "
                "projection".format(label)
            )
        if matching_projections:
            projection = _validated_confirmation_action_projection(
                matching_projections[0]
            )
            disposition = _validated_terminal_evidence_disposition(
                projection["evidence_disposition"],
                confirmation=confirmation,
                artifact_evidence_envelopes=projection[
                    "artifact_evidence_envelopes"
                ],
                operation=projection["operation"],
            )
            if disposition["state"] == "validated":
                _validate_terminal_confirmation_evidence(
                    graph,
                    confirmation=confirmation,
                    artifact_evidence_envelopes=projection[
                        "artifact_evidence_envelopes"
                    ],
                    label="{0} confirmation".format(label),
                )
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
        tuple(
            ref
            for ref in _dedupe_strings(refs)
            if ref not in confirmation_artifact_refs
        ),
        label=label,
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
        if (
            graph.resolve(root.node_ref) in graph.nodes
            and not authored_root_candidate_eligible(graph, root.node_ref)
        ):
            raise ValueError(
                "{0} published root candidate is ineligible for the active revision".format(
                    label
                )
            )
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
        candidate_node = graph.nodes.get(graph.resolve(root.node_ref) or "")
        if candidate_node is None:
            raise ValueError(
                "{0} root has no canonical graph candidate".format(label)
            )
        expected_root = canonical_confirmed_root_publication(
            confirmation=confirmation,
            defect_state=root.defect_state,
            candidate_node=candidate_node,
            seed_start_ref=owner.start_ref,
        )
        if root != expected_root:
            raise ValueError(
                "{0} root contradicts canonical graph publication".format(
                    label
                )
            )
        _assert_active_confirmation_path(
            graph,
            root.recursive_path,
            candidate_ref=root.node_ref,
            seed_ref=owner.start_ref,
            label=label,
        )
    _assert_canonical_published_roots(
        graph,
        confirmations=report.confirmations,
        seed_results=report.seed_results,
        confirmed_roots=report.confirmed_roots,
        co_roots=report.co_roots,
        analysis_perspective=report.analysis_perspective,
        label=label,
    )
    _assert_published_non_root_factors(
        graph,
        confirmations=report.confirmations,
        seed_results=report.seed_results,
        defect_states=report.defect_states,
        contributing_conditions=report.contributing_conditions,
        amplifying_factors=report.amplifying_factors,
        rejected_candidates=report.rejected_candidates,
        analysis_perspective=report.analysis_perspective,
        label=label,
    )


def _investigation_owner_bindings(value: Any) -> Dict[str, Set[str]]:
    """Extract structural owner identities from nested investigation state."""
    output = {
        "seed_binding_identities": set(),
        "hypothesis_ids": set(),
        "visit_keys": set(),
        "start_refs": set(),
    }
    field_groups = {
        "seed_binding_identity": "seed_binding_identities",
        "seed_key": "seed_binding_identities",
        "hypothesis_id": "hypothesis_ids",
        "visit_key": "visit_keys",
        "active_visit_key": "visit_keys",
        "seed_ref": "start_refs",
        "start_ref": "start_refs",
    }

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key)
                if key in {"ledger_before", "ledger_after"}:
                    continue
                group = field_groups.get(key)
                if group is not None and isinstance(child, str) and child:
                    output[group].add(child)
                if isinstance(child, (Mapping, list, tuple)):
                    visit(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return output


def _investigation_owned_by_stale_seed(
    value: Any,
    *,
    stale_seed_keys: Set[str],
    stale_hypothesis_ids: Set[str],
    stale_visit_keys: Set[str],
    stale_start_refs: Set[str],
) -> bool:
    bindings = _investigation_owner_bindings(value)
    return bool(
        bindings["seed_binding_identities"].intersection(stale_seed_keys)
        or bindings["hypothesis_ids"].intersection(stale_hypothesis_ids)
        or bindings["visit_keys"].intersection(stale_visit_keys)
        or bindings["start_refs"].intersection(stale_start_refs)
    )


def _investigation_journal_counters(
    investigation_journal: Iterable[Any],
) -> Tuple[int, int]:
    entries = [
        item
        for item in investigation_journal
        if isinstance(item, Mapping)
        and item.get("directive_kind") == "evidence_investigation"
    ]
    return (
        len(entries),
        sum(
            int(item["result"].get("byte_count") or 0)
            for item in entries
            if isinstance(item.get("result"), Mapping)
            and type(item["result"].get("byte_count", 0)) is int
            and item["result"].get("byte_count", 0) >= 0
        ),
    )


def _quarantine_stale_seed_report_payload(
    graph: TraceGraph, value: Mapping[str, Any]
) -> JsonDict:
    """Conservatively remove every conclusion owned by a stale report seed."""
    payload = copy.deepcopy(dict(value))
    source_metadata = (
        payload.get("metadata")
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    seeds = [
        item
        for item in payload.get("seed_results") or ()
        if isinstance(item, Mapping)
    ]
    seed_authority = _seed_authority_from_records(seeds)
    _classify_global_pass_records(
        payload.get("investigation_journal") or (),
        seed_authority=seed_authority,
    )
    _classify_global_failure_episodes(
        source_metadata.get("unresolved_branches") or (),
        seed_authority=seed_authority,
    )
    stale_start_refs = {
        graph.resolve(str(item.get("start_ref") or ""))
        or str(item.get("start_ref") or "")
        for item in seeds
        if not graph.active_revision_start_eligible(
            str(item.get("start_ref") or "")
        )
    }
    if not stale_start_refs:
        return payload
    stale_seed_keys = {
        seed_binding_identity_for(
            str(item.get("start_ref") or ""),
            str(item.get("defect_fingerprint") or ""),
        )
        for item in seeds
        if (
            graph.resolve(str(item.get("start_ref") or ""))
            or str(item.get("start_ref") or "")
        )
        in stale_start_refs
    }
    stale_hypothesis_ids = {
        str(item.get("hypothesis_id") or "")
        for item in payload.get("hypotheses") or ()
        if isinstance(item, Mapping)
        and str(item.get("seed_binding_identity") or "") in stale_seed_keys
    }
    stale_visit_keys = set()
    frontier_checkpoint = source_metadata.get("frontier_checkpoint")
    frontier_items = []
    if isinstance(frontier_checkpoint, Mapping):
        frontier_items.extend(frontier_checkpoint.get("queued") or ())
        frontier_items.extend(frontier_checkpoint.get("in_flight") or ())
        frontier_items.extend(
            item.get("item")
            for item in frontier_checkpoint.get("completed") or ()
            if isinstance(item, Mapping)
        )
    for item in frontier_items:
        bindings = _investigation_owner_bindings(item)
        if (
            bindings["seed_binding_identities"].intersection(stale_seed_keys)
            or bindings["hypothesis_ids"].intersection(stale_hypothesis_ids)
        ):
            stale_visit_keys.update(bindings["visit_keys"])
    stale_candidate_refs = {
        str(item.get("candidate_root_ref") or "")
        for item in payload.get("hypotheses") or ()
        if isinstance(item, Mapping)
        and str(item.get("seed_binding_identity") or "") in stale_seed_keys
    }
    active_candidate_refs = {
        str(item.get("candidate_root_ref") or "")
        for item in payload.get("hypotheses") or ()
        if isinstance(item, Mapping)
        and str(item.get("seed_binding_identity") or "")
        not in stale_seed_keys
    }

    def seed_binding(item: Any) -> str:
        if not isinstance(item, Mapping):
            return ""
        confirmation = item.get("confirmation")
        source = confirmation if isinstance(confirmation, Mapping) else item
        return str(source.get("seed_binding_identity") or "")

    def keep_publication(item: Any) -> bool:
        return seed_binding(item) not in stale_seed_keys

    for name in (
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "rejected_candidates",
    ):
        payload[name] = [
            item for item in payload.get(name) or () if keep_publication(item)
        ]
    for name in ("hypotheses", "unresolved_hypotheses"):
        payload[name] = [
            item
            for item in payload.get(name) or ()
            if not isinstance(item, Mapping)
            or str(item.get("seed_binding_identity") or "")
            not in stale_seed_keys
        ]
    payload["introduction_candidates"] = [
        item
        for item in payload.get("introduction_candidates") or ()
        if not isinstance(item, Mapping)
        or str(item.get("ref") or "") not in stale_candidate_refs
        or str(item.get("ref") or "") in active_candidate_refs
    ]
    payload["causal_candidates"] = [
        item
        for item in payload.get("causal_candidates") or ()
        if not isinstance(item, Mapping)
        or str(item.get("ref") or "") not in stale_candidate_refs
        or str(item.get("ref") or "") in active_candidate_refs
    ]
    def keep_owned_local_state(item: Any, *, label: str) -> bool:
        if not isinstance(item, Mapping):
            raise ValueError("{0} entry must be an object".format(label))
        owner = LocalStateOwner.from_dict(item.get("owner"))
        return owner.seed_binding_identity not in stale_seed_keys

    payload["causal_relations"] = [
        item
        for item in payload.get("causal_relations") or ()
        if keep_owned_local_state(item, label="causal relation")
    ]
    payload["step_judgments"] = [
        item
        for item in payload.get("step_judgments") or ()
        if keep_owned_local_state(item, label="step judgment")
    ]
    payload["visited_entries"] = [
        item
        for item in payload.get("visited_entries") or ()
        if keep_owned_local_state(item, label="visited state")
    ]
    payload["visited_order"] = list(
        dict.fromkeys(
            str(item.get("node_ref") or "")
            for item in payload["visited_entries"]
        )
    )
    payload["taint_paths"] = [
        path
        for path in payload.get("taint_paths") or ()
        if not any(str(ref) in stale_start_refs for ref in path)
    ]
    payload["investigation_journal"] = [
        item
        for item in payload.get("investigation_journal") or ()
        if not _investigation_owned_by_stale_seed(
            item,
            stale_seed_keys=stale_seed_keys,
            stale_hypothesis_ids=stale_hypothesis_ids,
            stale_visit_keys=stale_visit_keys,
            stale_start_refs=stale_start_refs,
        )
    ]

    migration_detail = (
        "The restored analysis start is ineligible for the active repository "
        "generation."
    )
    for seed in seeds:
        start_ref = graph.resolve(str(seed.get("start_ref") or "")) or str(
            seed.get("start_ref") or ""
        )
        if start_ref not in stale_start_refs:
            continue
        seed["outcome"] = "evidence_gap"
        for key in (
            "candidate_refs",
            "selected_candidate_refs",
            "confirmation_identities",
            "confirmed_root_refs",
            "decisive_evidence_refs",
        ):
            seed[key] = []
        seed["global_judgment"] = {}
        seed["expansion_history"] = []
        seed["decisive_evidence"] = []
        seed["missing_evidence"] = sorted(
            {
                *(
                    str(item)
                    for item in seed.get("missing_evidence") or ()
                    if str(item)
                ),
                migration_detail,
            }
        )
        seed["blocking_reasons"] = sorted(
            {
                *(
                    str(item)
                    for item in seed.get("blocking_reasons") or ()
                    if str(item)
                ),
                "start_ref_active_revision_ineligible",
            }
        )
    payload["seed_results"] = seeds
    outcomes = [str(item.get("outcome") or "") for item in seeds]
    if outcomes and all(item == "no_defect" for item in outcomes):
        payload["analysis_outcome"] = "no_defect"
    elif outcomes and all(
        item in {"confirmed_root", "no_defect"} for item in outcomes
    ) and "confirmed_root" in outcomes:
        payload["analysis_outcome"] = "confirmed_root"
    elif outcomes and "inconclusive" not in outcomes and len(set(outcomes)) > 1:
        payload["analysis_outcome"] = "partial"
    else:
        payload["analysis_outcome"] = "inconclusive"
    payload["unresolved_refs"] = sorted(
        {
            *(str(item) for item in payload.get("unresolved_refs") or ()),
            *stale_start_refs,
        }
    )
    metadata = (
        copy.deepcopy(payload.get("metadata"))
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    for key in (
        "confirmation_queue",
        "confirmation_journal",
        "confirmation_action_projection",
        "step_action_projection",
        "global_candidate_judgments",
        "global_candidate_failures",
        "candidate_compression",
        "recursive_expansion_reasons",
    ):
        metadata[key] = [
            item
            for item in metadata.get(key) or ()
            if keep_owned_local_state(item, label=key)
        ]
    metadata["introduction_bindings"] = [
        item
        for item in metadata.get("introduction_bindings") or ()
        if not isinstance(item, Mapping)
        or str(
            item.get("seed_key")
            or item.get("seed_binding_identity")
            or ""
        )
        not in stale_seed_keys
    ]
    metadata["confirmation_queue_keys"] = [
        list(
            RecursiveAnalysisState._confirmation_queue_key(item)
        )
        for item in metadata.get("confirmation_queue") or ()
        if isinstance(item, Mapping)
    ]
    retained_global_passes = [
        item
        for item in payload["investigation_journal"]
        if isinstance(item, Mapping)
        and item.get("kind") == "global_candidate_pass"
        and item.get("status") in {"completed", "failed"}
    ]
    metadata["global_candidate_pass_count"] = len(retained_global_passes)
    metadata["global_judge_physical_request_count"] = sum(
        int(item.get("physical_request_delta") or 0)
        for item in retained_global_passes
    )
    (
        metadata["investigation_rounds"],
        metadata["investigation_result_bytes"],
    ) = _investigation_journal_counters(payload["investigation_journal"])
    metadata["stale_seed_quarantine"] = {
        "start_refs": sorted(stale_start_refs),
        "blocking_reason": "start_ref_active_revision_ineligible",
        "behavior_impact": "none_offline_analysis_only",
    }
    payload["metadata"] = metadata
    legacy_root_causes = []
    seen_legacy_root_causes = set()
    for item in (
        *payload.get("confirmed_roots", ()),
        *payload.get("co_roots", ()),
    ):
        if not isinstance(item, Mapping):
            continue
        projection = ConfirmedRoot.from_dict(
            copy.deepcopy(dict(item))
        ).to_legacy_root_cause()
        identity = stable_json(projection)
        if identity in seen_legacy_root_causes:
            continue
        seen_legacy_root_causes.add(identity)
        legacy_root_causes.append(projection)
    payload["root_causes"] = legacy_root_causes
    return payload


def _canonical_multiset(values: Iterable[Any]) -> Counter:
    return Counter(
        stable_json(_checkpoint_json(value))
        for value in values
    )


def _require_canonical_bijection(
    authoritative: Iterable[Any],
    derived: Iterable[Any],
    *,
    label: str,
) -> None:
    if _canonical_multiset(authoritative) != _canonical_multiset(derived):
        raise ValueError(
            "{0} owner/payload multiset must bijectively match "
            "authoritative actions".format(label)
        )


def _confirmation_request_identity(
    request: RootConfirmationRequest,
) -> str:
    return root_confirmation_request_identity(request)


def _synthetic_unknown_confirmation(
    request: RootConfirmationRequest,
    *,
    reason: str,
) -> RootConfirmation:
    competitor_comparisons = tuple(
        {
            "hypothesis_id": str(item.get("hypothesis_id") or ""),
            "hypothesis_semantic_hash": str(
                item.get("hypothesis_semantic_hash") or ""
            ),
            "candidate_ref": str(
                item.get("candidate_reference", {}).get(
                    "resolved_ref"
                )
                or ""
            ),
            "defect_fingerprint": str(
                item.get("active_defect", {}).get("fingerprint") or ""
            ),
            "confirmation_identity": str(
                item.get("confirmation_identity") or ""
            ),
            "recursive_path": list(item.get("recursive_path") or ()),
            "requires_independent_confirmation": item.get(
                "requires_independent_confirmation"
            ),
            "status": "unresolved",
            "reason": (
                "The synthetic unknown outcome does not resolve this "
                "offered competitor."
            ),
            "evidence_refs": [],
        }
        for item in request.competing_hypotheses
        if str(item.get("status") or "").strip().lower()
        in {"active", "supported", "unresolved"}
    )
    return RootConfirmation(
        candidate_ref=request.candidate_ref,
        status="unknown",
        counterfactual=confirmation_counterfactual_for(
            request.candidate_ref,
            "unknown",
        ),
        reason=reason,
        counterfactual_status="unknown",
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        recursive_path=request.recursive_path,
        seed_binding_identity=request.seed_binding_identity,
        analysis_perspective=request.analysis_perspective,
        competitor_comparisons=competitor_comparisons,
    )


def _validate_pending_confirmation_identity(
    value: Mapping[str, Any],
) -> Tuple[str, str, str, str]:
    actual_keys = {str(key) for key in value}
    missing = PENDING_CONFIRMATION_REQUIRED_KEYS - actual_keys
    extra = actual_keys - PENDING_CONFIRMATION_ALLOWED_KEYS
    if missing or extra or value.get("status") != "queued":
        raise ValueError(
            "pending confirmation identity schema mismatch "
            "(missing={0}, extra={1})".format(
                sorted(missing),
                sorted(extra),
            )
        )
    queue_key = (
        str(value.get("hypothesis_id") or ""),
        str(value.get("candidate_ref") or ""),
        str(value.get("defect_fingerprint") or ""),
        str(value.get("seed_binding_identity") or ""),
    )
    if not all(queue_key):
        raise ValueError(
            "pending confirmation requires complete request identity fields"
        )
    if not str(value.get("semantic_identity") or "").startswith(
        ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX
    ):
        raise ValueError(
            "pending confirmation semantic identity is not a canonical "
            "versioned request hash"
        )
    return queue_key


def _validate_terminal_confirmation_identity(
    value: Mapping[str, Any],
) -> None:
    actual_keys = {str(key) for key in value}
    missing = TERMINAL_CONFIRMATION_REQUIRED_KEYS - actual_keys
    extra = actual_keys - TERMINAL_CONFIRMATION_ALLOWED_KEYS
    if (
        missing
        or extra
        or value.get("status") not in {"confirmed", "rejected", "unknown"}
        or not isinstance(value.get("confirmation"), Mapping)
    ):
        raise ValueError(
            "terminal confirmation identity schema mismatch "
            "(missing={0}, extra={1})".format(
                sorted(missing),
                sorted(extra),
            )
        )
    confirmation = RootConfirmation.from_dict(
        dict(value.get("confirmation") or {})
    )
    if (
        str(value.get("response_identity") or "")
        != confirmation.response_identity
    ):
        raise ValueError(
            "terminal confirmation queue response_identity contradicts its "
            "confirmation response"
        )


def _validate_confirmation_request_projection_binding(
    value: Mapping[str, Any],
    *,
    defect_state: DefectState,
    analysis_perspective: str,
    ledger: HypothesisLedger,
    frontier: RecursiveFrontier,
    confirmation: Optional[RootConfirmation] = None,
    action_projection: Optional[Mapping[str, Any]] = None,
    label: str,
) -> JsonDict:
    """Bind canonical persisted request facts to graph-independent outer state."""
    if not isinstance(value, Mapping):
        raise ValueError("{0} outer request must be an object".format(label))
    canonical = validate_root_confirmation_request_projection(
        value.get("factual_request_projection")
    )
    semantic_identity = str(value.get("semantic_identity") or "")
    request_identity = str(value.get("request_identity") or "")
    if (
        semantic_identity
        and request_identity
        and semantic_identity != request_identity
    ):
        raise ValueError(
            "{0} projection binding contradicts outer request identity".format(
                label
            )
        )
    outer_request_identity = semantic_identity or request_identity
    if (
        not outer_request_identity
        or root_confirmation_request_projection_identity(canonical)
        != outer_request_identity
    ):
        raise ValueError(
            "{0} projection binding contradicts outer request identity".format(
                label
            )
        )

    outer_perspective = (
        str(value.get("analysis_perspective") or "")
        if "analysis_perspective" in value
        else str(analysis_perspective)
    )
    facts = canonical["facts"]
    projected_binding = (
        facts["candidate_ref"],
        stable_json(facts["defect_state"]),
        tuple(facts["recursive_path"]),
        facts["hypothesis_id"],
        facts["hypothesis_semantic_hash"],
        facts["seed_binding_identity"],
        facts["analysis_perspective"],
    )
    outer_binding = (
        str(value.get("candidate_ref") or ""),
        stable_json(defect_state.to_dict()),
        tuple(str(item) for item in value.get("recursive_path") or ()),
        str(value.get("hypothesis_id") or ""),
        str(value.get("hypothesis_semantic_hash") or ""),
        str(value.get("seed_binding_identity") or ""),
        outer_perspective,
    )
    if (
        projected_binding != outer_binding
        or outer_perspective != str(analysis_perspective)
    ):
        raise ValueError(
            "{0} factual projection binding contradicts outer request "
            "semantics".format(label)
        )

    owner = LocalStateOwner.from_dict(value.get("owner"))
    if (
        owner.hypothesis_id != outer_binding[3]
        or owner.seed_binding_identity != outer_binding[5]
    ):
        raise ValueError(
            "{0} projection binding contradicts outer request owner".format(
                label
            )
        )

    try:
        hypothesis = ledger.get(outer_binding[3])
    except KeyError:
        raise ValueError(
            "{0} hypothesis authority is missing from the ledger".format(
                label
            )
        )
    ledger_binding = (
        hypothesis.hypothesis_id,
        hypothesis.semantic_hash,
        hypothesis.candidate_root_ref,
        hypothesis.active_defect_fingerprint,
        hypothesis.seed_binding_identity,
    )
    expected_authority = (
        outer_binding[3],
        outer_binding[4],
        outer_binding[0],
        defect_state.fingerprint,
        outer_binding[5],
    )
    if ledger_binding != expected_authority:
        raise ValueError(
            "{0} hypothesis authority contradicts the ledger".format(label)
        )

    frontier_items = [
        item
        for item in frontier.lifecycle_items()
        if item.visit_key == owner.visit_key
    ]
    if len(frontier_items) > 1:
        raise ValueError(
            "{0} hypothesis authority has an ambiguous frontier visit".format(
                label
            )
        )
    if frontier_items:
        item = frontier_items[0]
        frontier_binding = (
            item.hypothesis_id,
            item.hypothesis_semantic_hash,
            item.node_ref,
            item.defect_state.fingerprint,
            item.seed_binding_identity,
        )
        expected_owner = _owner_for_item(item, "confirmation_queue")
        if (
            frontier_binding != expected_authority
            or owner != expected_owner
        ):
            raise ValueError(
                "{0} hypothesis authority contradicts the frontier".format(
                    label
                )
            )
    else:
        expected_owner = LocalStateOwner.create(
            seed_binding_identity=outer_binding[5],
            hypothesis_id=outer_binding[3],
            visit_key=semantic_visit_key(
                outer_binding[0],
                defect_state,
                hypothesis.semantic_hash,
                outer_binding[5],
            ),
            occurrence_key="confirmation_queue",
        )
        if owner != expected_owner:
            raise ValueError(
                "{0} hypothesis authority contradicts the ledger owner".format(
                    label
                )
            )

    if confirmation is not None:
        confirmation_binding = (
            confirmation.candidate_ref,
            confirmation.defect_fingerprint,
            confirmation.recursive_path,
            confirmation.hypothesis_id,
            confirmation.hypothesis_semantic_hash,
            confirmation.seed_binding_identity,
            confirmation.analysis_perspective,
        )
        expected_confirmation_binding = (
            facts["candidate_ref"],
            defect_state.fingerprint,
            tuple(facts["recursive_path"]),
            facts["hypothesis_id"],
            facts["hypothesis_semantic_hash"],
            facts["seed_binding_identity"],
            facts["analysis_perspective"],
        )
        if confirmation_binding != expected_confirmation_binding:
            raise ValueError(
                "{0} terminal confirmation contradicts factual projection "
                "binding".format(label)
            )

    if action_projection is not None:
        if not isinstance(action_projection, Mapping):
            raise ValueError(
                "{0} terminal action projection must be an object".format(
                    label
                )
            )
        action_confirmation = RootConfirmation.from_dict(
            _checkpoint_json(action_projection.get("confirmation") or {})
        )
        action_owner = LocalStateOwner.from_dict(
            action_projection.get("owner")
        )
        action_binding = (
            str(action_projection.get("candidate_ref") or ""),
            str(action_projection.get("defect_fingerprint") or ""),
            tuple(
                str(item)
                for item in action_projection.get("recursive_path") or ()
            ),
            str(action_projection.get("hypothesis_id") or ""),
            str(action_projection.get("hypothesis_semantic_hash") or ""),
            str(action_projection.get("seed_binding_identity") or ""),
            str(action_projection.get("request_identity") or ""),
        )
        expected_action_binding = (
            facts["candidate_ref"],
            defect_state.fingerprint,
            tuple(facts["recursive_path"]),
            facts["hypothesis_id"],
            facts["hypothesis_semantic_hash"],
            facts["seed_binding_identity"],
            outer_request_identity,
        )
        action_request_projection = (
            validate_root_confirmation_request_projection(
                action_projection.get("factual_request_projection")
            )
        )
        if (
            action_binding != expected_action_binding
            or action_owner != owner
            or (
                confirmation is not None
                and action_confirmation != confirmation
            )
            or stable_json(action_request_projection)
            != stable_json(canonical)
        ):
            raise ValueError(
                "{0} terminal action contradicts factual projection "
                "binding".format(label)
            )
    return canonical


def _terminal_evidence_snapshot_identity(
    artifact_evidence_envelopes: Any,
) -> str:
    if not isinstance(artifact_evidence_envelopes, (list, tuple)):
        raise ValueError(
            "terminal artifact evidence snapshot must be an array"
        )
    return "terminal_evidence_snapshot:v1:{0}".format(
        hashlib.sha256(
            stable_json(
                _checkpoint_json(list(artifact_evidence_envelopes))
            ).encode("utf-8")
        ).hexdigest()
    )


def _build_terminal_evidence_disposition(
    graph: TraceGraph,
    artifact_evidence_envelopes: Any,
) -> JsonDict:
    if not isinstance(artifact_evidence_envelopes, (list, tuple)):
        raise ValueError(
            "terminal artifact evidence snapshot must be an array"
        )
    snapshot = copy.deepcopy(list(artifact_evidence_envelopes))
    comparison_facts = []
    for value in snapshot:
        owner_reference = (
            value.get("owner_reference")
            if isinstance(value, Mapping)
            else None
        )
        expected_owner_ref = (
            str(owner_reference.get("resolved_ref") or "")
            if isinstance(owner_reference, Mapping)
            else ""
        )
        comparison_facts.append(
            graph.artifact_evidence_comparison_facts(
                value,
                expected_owner_ref=expected_owner_ref,
            )
        )
    rejected = [
        "{0}: {1}".format(
            str(item.get("canonical_ref") or "<unknown>"),
            str(item.get("rejection_reason") or "artifact snapshot rejected"),
        )
        for item in comparison_facts
        if item.get("validation_status") == "rejected"
    ]
    return {
        "schema": TERMINAL_EVIDENCE_DISPOSITION_SCHEMA,
        "state": "rejected_snapshot" if rejected else "validated",
        "snapshot_identity": _terminal_evidence_snapshot_identity(snapshot),
        "rejection_reason": "; ".join(rejected),
        "graph_comparison_facts": comparison_facts,
    }


def _reject_terminal_evidence_disposition(
    disposition: Mapping[str, Any],
    *,
    reason: str,
) -> JsonDict:
    rejected = copy.deepcopy(dict(disposition))
    facts = rejected.get("graph_comparison_facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError(
            "artifact snapshot rejection requires comparison facts"
        )
    facts[0]["validation_status"] = "rejected"
    facts[0]["rejection_reason"] = str(reason or "").strip()
    if not facts[0]["rejection_reason"]:
        raise ValueError("artifact snapshot rejection reason is required")
    rejection_reasons = [
        "{0}: {1}".format(
            str(item.get("canonical_ref") or "<unknown>"),
            str(item.get("rejection_reason") or ""),
        )
        for item in facts
        if item.get("validation_status") == "rejected"
    ]
    rejected["state"] = "rejected_snapshot"
    rejected["rejection_reason"] = "; ".join(rejection_reasons)
    return rejected


def _validated_terminal_evidence_disposition(
    value: Any,
    *,
    artifact_evidence_envelopes: Any,
    confirmation: Optional[RootConfirmation] = None,
    operation: str = "",
) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("terminal evidence disposition must be an object")
    _require_exact_checkpoint_keys(
        value,
        set(TERMINAL_EVIDENCE_DISPOSITION_KEYS),
        "terminal evidence disposition",
    )
    disposition = _checkpoint_json(value)
    state = str(disposition.get("state") or "")
    facts = disposition.get("graph_comparison_facts")
    if (
        disposition.get("schema")
        != TERMINAL_EVIDENCE_DISPOSITION_SCHEMA
        or state not in {"validated", "rejected_snapshot"}
        or not isinstance(facts, list)
        or disposition.get("snapshot_identity")
        != _terminal_evidence_snapshot_identity(
            artifact_evidence_envelopes
        )
    ):
        raise ValueError("terminal evidence disposition is non-canonical")
    snapshot = list(artifact_evidence_envelopes)
    if len(facts) != len(snapshot):
        raise ValueError(
            "terminal evidence disposition does not cover its audit snapshot"
        )
    rejection_reasons = []
    for index, (fact, envelope) in enumerate(zip(facts, snapshot)):
        if not isinstance(fact, Mapping):
            raise ValueError(
                "terminal evidence comparison fact[{0}] is invalid".format(
                    index
                )
            )
        _require_exact_checkpoint_keys(
            fact,
            set(ARTIFACT_EVIDENCE_COMPARISON_KEYS),
            "terminal evidence comparison fact[{0}]".format(index),
        )
        owner_reference = (
            envelope.get("owner_reference")
            if isinstance(envelope, Mapping)
            else None
        )
        expected_owner_ref = (
            str(owner_reference.get("resolved_ref") or "")
            if isinstance(owner_reference, Mapping)
            else ""
        )
        canonical_ref = (
            str(envelope.get("canonical_ref") or "")
            if isinstance(envelope, Mapping)
            else ""
        )
        validation_status = str(
            fact.get("validation_status") or ""
        )
        if (
            fact.get("schema") != "artifact-evidence-comparison/v1"
            or str(fact.get("canonical_ref") or "") != canonical_ref
            or str(fact.get("expected_owner_ref") or "")
            != expected_owner_ref
            or validation_status not in {"validated", "rejected"}
            or not isinstance(fact.get("active_owner_refs"), list)
            or not isinstance(fact.get("file_verification"), Mapping)
        ):
            raise ValueError(
                "terminal evidence comparison fact[{0}] contradicts its "
                "audit snapshot".format(index)
            )
        rejection_reason = str(fact.get("rejection_reason") or "")
        if (
            validation_status == "validated" and rejection_reason
        ) or (
            validation_status == "rejected" and not rejection_reason
        ):
            raise ValueError(
                "terminal evidence comparison fact[{0}] has inconsistent "
                "validation facts".format(index)
            )
        if validation_status == "rejected":
            rejection_reasons.append(
                "{0}: {1}".format(
                    canonical_ref or "<unknown>",
                    rejection_reason,
                )
            )
    expected_rejection = "; ".join(rejection_reasons)
    if (
        state == "validated"
        and (
            rejection_reasons
            or str(disposition.get("rejection_reason") or "")
        )
    ) or (
        state == "rejected_snapshot"
        and (
            not rejection_reasons
            or str(disposition.get("rejection_reason") or "")
            != expected_rejection
        )
    ):
        raise ValueError(
            "terminal evidence disposition state contradicts comparison facts"
        )
    if confirmation is not None:
        if state == "rejected_snapshot":
            expected_reason = "terminal_artifact_preflight_rejected: {0}".format(
                expected_rejection
            )
            if (
                operation != "confirmation_failed"
                or confirmation.status != "unknown"
                or confirmation.evidence_refs
                or confirmation.reason != expected_reason
            ):
                raise ValueError(
                    "rejected artifact snapshot may terminate only as a "
                    "no-evidence unknown confirmation"
                )
        elif (
            confirmation.status in {"confirmed", "rejected"}
            and state != "validated"
        ):
            raise ValueError(
                "substantive confirmation requires validated terminal evidence"
            )
    return copy.deepcopy(disposition)


def _confirmation_action_projection(
    *,
    operation: str,
    semantic_key: str,
    request_identity: str,
    owner: Any,
    seed_key: str,
    confirmation: RootConfirmation,
    physical_requests_reserved: int,
    physical_request_delta: int,
    physical_request_exact: bool,
    factual_request_projection: Any,
    artifact_evidence_envelopes: Sequence[Mapping[str, Any]] = (),
    evidence_disposition: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    if operation not in CONFIRMATION_ACTION_OPERATIONS:
        raise ValueError("confirmation action operation is invalid")
    parsed_owner = LocalStateOwner.from_dict(owner)
    if (
        not request_identity
        or not request_identity.startswith(
            ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX
        )
        or semantic_key != "confirmation:{0}".format(request_identity)
        or parsed_owner.seed_binding_identity
        != confirmation.seed_binding_identity
        or parsed_owner.hypothesis_id != confirmation.hypothesis_id
        or seed_key != confirmation.seed_binding_identity
    ):
        raise ValueError(
            "confirmation action request, response, owner, or seed identity is inconsistent"
        )
    for label, value in (
        ("physical_requests_reserved", physical_requests_reserved),
        ("physical_request_delta", physical_request_delta),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(
                "confirmation action {0} must be a nonnegative integer".format(
                    label
                )
            )
    if type(physical_request_exact) is not bool:
        raise ValueError("confirmation action physical_request_exact must be boolean")
    canonical_request_projection = (
        validate_root_confirmation_request_projection(
            factual_request_projection
        )
    )
    if (
        root_confirmation_request_projection_identity(
            canonical_request_projection
        )
        != request_identity
    ):
        raise ValueError(
            "confirmation action factual request projection contradicts its "
            "semantic identity"
        )
    validated_disposition = _validated_terminal_evidence_disposition(
        evidence_disposition,
        artifact_evidence_envelopes=artifact_evidence_envelopes,
        confirmation=confirmation,
        operation=operation,
    )
    return {
        "operation": operation,
        "semantic_key": semantic_key,
        "request_identity": request_identity,
        "response_identity": confirmation.response_identity,
        "owner": parsed_owner.to_dict(),
        "status": confirmation.status,
        "candidate_ref": confirmation.candidate_ref,
        "hypothesis_id": confirmation.hypothesis_id,
        "hypothesis_semantic_hash": confirmation.hypothesis_semantic_hash,
        "defect_fingerprint": confirmation.defect_fingerprint,
        "seed_binding_identity": confirmation.seed_binding_identity,
        "seed_key": seed_key,
        "recursive_path": list(confirmation.recursive_path),
        "evidence_refs": list(confirmation.evidence_refs),
        "artifact_evidence_envelopes": copy.deepcopy(
            list(artifact_evidence_envelopes)
        ),
        "evidence_disposition": validated_disposition,
        "factual_request_projection": canonical_request_projection,
        "physical_requests_reserved": physical_requests_reserved,
        "physical_request_delta": physical_request_delta,
        "physical_request_exact": physical_request_exact,
        "confirmation": confirmation.to_dict(),
    }


def _validate_terminal_confirmation_evidence(
    graph: TraceGraph,
    *,
    confirmation: RootConfirmation,
    artifact_evidence_envelopes: Any,
    label: str,
) -> Tuple[JsonDict, ...]:
    validate_root_confirmation_substantive_invariants(confirmation)
    if not isinstance(artifact_evidence_envelopes, (list, tuple)):
        raise ValueError(
            "{0} artifact evidence envelopes must be an array".format(label)
        )
    canonical_by_ref: Dict[str, JsonDict] = {}
    for index, value in enumerate(artifact_evidence_envelopes):
        if not isinstance(value, Mapping):
            raise ValueError(
                "{0} artifact evidence envelope[{1}] must be an object".format(
                    label, index
                )
            )
        canonical_ref = str(value.get("canonical_ref") or "")
        owner_reference = value.get("owner_reference")
        owner_ref = (
            str(owner_reference.get("resolved_ref") or "")
            if isinstance(owner_reference, Mapping)
            else ""
        )
        if (
            not canonical_ref
            or canonical_ref in canonical_by_ref
            or not owner_ref
        ):
            raise ValueError(
                "{0} contains a missing or duplicate artifact owner "
                "envelope".format(label)
            )
        persisted_envelope = _checkpoint_json(value)
        canonical = graph.validate_artifact_evidence_envelope(
            persisted_envelope,
            expected_owner_ref=owner_ref,
        )
        if str(canonical.get("canonical_ref") or "") != canonical_ref:
            raise ValueError(
                "{0} artifact envelope canonical ref is inconsistent".format(
                    label
                )
            )
        canonical_by_ref[canonical_ref] = canonical

    for raw_ref in confirmation.evidence_refs:
        resolved = graph.resolve(raw_ref)
        if resolved in graph.nodes:
            if not graph.active_revision_evidence_eligible(resolved):
                raise ValueError(
                    "{0} node evidence is ineligible for the active revision: "
                    "{1}".format(label, raw_ref)
                )
            continue
        status = graph.artifact_reference_status(raw_ref)
        if (
            status is None
            or status.get("resolution_status") != "resolved"
            or status.get("availability") != "available"
        ):
            raise ValueError(
                "{0} artifact evidence is missing, stale, or unresolved: "
                "{1}".format(label, raw_ref)
            )
        canonical_ref = str(status.get("canonical_ref") or "")
        if canonical_ref not in canonical_by_ref:
            raise ValueError(
                "{0} artifact evidence has no exact persisted owner "
                "envelope: {1}".format(label, raw_ref)
            )
    return tuple(canonical_by_ref[key] for key in canonical_by_ref)


def _validated_confirmation_action_projection(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("confirmation action projection must be an object")
    _require_exact_checkpoint_keys(
        value,
        set(CONFIRMATION_ACTION_PROJECTION_KEYS),
        "confirmation action projection",
    )
    confirmation = RootConfirmation.from_dict(
        _checkpoint_json(value.get("confirmation") or {})
    )
    expected = _confirmation_action_projection(
        operation=str(value.get("operation") or ""),
        semantic_key=str(value.get("semantic_key") or ""),
        request_identity=str(value.get("request_identity") or ""),
        owner=value.get("owner"),
        seed_key=str(value.get("seed_key") or ""),
        confirmation=confirmation,
        physical_requests_reserved=value.get("physical_requests_reserved"),
        physical_request_delta=value.get("physical_request_delta"),
        physical_request_exact=value.get("physical_request_exact"),
        factual_request_projection=value.get(
            "factual_request_projection"
        ),
        artifact_evidence_envelopes=value.get(
            "artifact_evidence_envelopes"
        )
        or (),
        evidence_disposition=value.get("evidence_disposition"),
    )
    if stable_json(_checkpoint_json(value)) != stable_json(
        _checkpoint_json(expected)
    ):
        raise ValueError(
            "confirmation action projection contradicts its canonical payload"
        )
    return expected


def _confirmation_action_projection_from_record(
    record: Mapping[str, Any],
) -> JsonDict:
    operation = str(record.get("operation") or "")
    payload = record.get("payload")
    if operation not in CONFIRMATION_ACTION_OPERATIONS or not isinstance(
        payload, Mapping
    ):
        raise ValueError("completed or failed confirmation action is malformed")
    projection = _validated_confirmation_action_projection(
        payload.get("action_projection")
    )
    if (
        projection["operation"] != operation
        or projection["semantic_key"]
        != str(record.get("semantic_key") or "")
        or payload.get("status") != projection["status"]
        or payload.get("confirmation") != projection["confirmation"]
        or payload.get("physical_requests_reserved")
        != projection["physical_requests_reserved"]
        or payload.get("physical_request_delta")
        != projection["physical_request_delta"]
        or payload.get("physical_request_exact")
        != projection["physical_request_exact"]
    ):
        raise ValueError(
            "confirmation action record contradicts its canonical projection"
        )
    return projection


def _validated_confirmation_started_action(
    record: Mapping[str, Any],
    *,
    request: RootConfirmationRequest,
    request_identity: str,
) -> JsonDict:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("started confirmation action payload is invalid")
    _require_exact_checkpoint_keys(
        payload,
        set(CONFIRMATION_STARTED_PAYLOAD_KEYS),
        "started confirmation action payload",
    )
    reserved = payload.get("physical_requests_reserved")
    if (
        record.get("operation") != "confirmation_started"
        or str(record.get("semantic_key") or "")
        != "confirmation:{0}".format(request_identity)
        or payload.get("status") != "in_flight"
        or str(payload.get("candidate_ref") or "")
        != request.candidate_ref
        or str(payload.get("hypothesis_id") or "")
        != request.hypothesis_id
        or str(payload.get("request_identity") or "")
        != request_identity
        or type(reserved) is not int
        or reserved < 0
    ):
        raise ValueError(
            "started confirmation action contradicts its factual request"
        )
    return copy.deepcopy(dict(payload))


def _seed_authority_from_records(
    records: Iterable[Any],
) -> Dict[str, JsonDict]:
    authority: Dict[str, JsonDict] = {}
    for index, item in enumerate(records):
        if not isinstance(item, Mapping):
            raise ValueError(
                "seed authority record[{0}] must be an object".format(index)
            )
        seed_ref = str(item.get("start_ref") or item.get("seed_ref") or "")
        defect_state = item.get("defect_state")
        defect_fingerprint = str(
            item.get("defect_fingerprint")
            or (
                defect_state.get("fingerprint")
                if isinstance(defect_state, Mapping)
                else ""
            )
            or ""
        )
        if not seed_ref or not defect_fingerprint:
            raise ValueError(
                "seed authority record[{0}] is incomplete".format(index)
            )
        binding, _, _ = _canonical_global_pass_facts(
            seed_ref=seed_ref,
            defect_fingerprint=defect_fingerprint,
        )
        if binding in authority:
            raise ValueError("seed authority contains a duplicate binding")
        authority[binding] = {
            "seed_ref": seed_ref,
            "defect_fingerprint": defect_fingerprint,
            "defect_state_id": "defect:{0}".format(defect_fingerprint),
        }
    return authority


def _normalized_seed_authority(
    seed_authority: Optional[Mapping[str, Any]],
) -> Dict[str, JsonDict]:
    output: Dict[str, JsonDict] = {}
    for raw_binding, raw_facts in (seed_authority or {}).items():
        if not isinstance(raw_facts, Mapping):
            raise ValueError("global terminal seed authority must contain objects")
        seed_ref = str(raw_facts.get("seed_ref") or "")
        defect_fingerprint = str(
            raw_facts.get("defect_fingerprint") or ""
        )
        expected_binding, expected_pass, expected_owner = (
            _canonical_global_pass_facts(
                seed_ref=seed_ref,
                defect_fingerprint=defect_fingerprint,
            )
        )
        binding = str(raw_binding or "")
        if binding != expected_binding or binding in output:
            raise ValueError(
                "global terminal seed authority binding is non-canonical"
            )
        defect_state_id = str(
            raw_facts.get("defect_state_id")
            or "defect:{0}".format(defect_fingerprint)
        )
        if defect_state_id != "defect:{0}".format(defect_fingerprint):
            raise ValueError(
                "global terminal seed authority defect state is non-canonical"
            )
        output[binding] = {
            "seed_ref": seed_ref,
            "defect_fingerprint": defect_fingerprint,
            "defect_state_id": defect_state_id,
            "pass_identity": expected_pass,
            "owner": expected_owner.to_dict(),
        }
    return output


def _owner_partially_claims_global_pass(
    value: Any,
    authority: Mapping[str, JsonDict],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    owner_keys = {
        "seed_binding_identity",
        "hypothesis_id",
        "visit_key",
        "occurrence_identity",
    }
    present = owner_keys.intersection(str(key) for key in value)
    if not present:
        return False
    for binding, facts in authority.items():
        canonical = facts["owner"]
        matching = {
            key
            for key in present
            if value.get(key) == canonical.get(key)
        }
        if matching == present and (
            "occurrence_identity" in matching
            or (
                "seed_binding_identity" in matching
                and len(matching) >= 2
            )
            or len(matching) >= 2
        ):
            return True
        if (
            value.get("seed_binding_identity") == binding
            and {
                "hypothesis_id",
                "visit_key",
                "occurrence_identity",
            }.intersection(matching)
        ):
            return True
    return False


def _global_terminal_residual_signature(
    value: Any,
    *,
    seed_authority: Optional[Mapping[str, Any]],
    allowed_kinds: Set[str],
) -> bool:
    """Recognize explicit or ledger-bound residual global terminal state."""
    if not isinstance(value, Mapping):
        return False
    if value.get("kind") == "global_candidate_pass" or any(
        key in value for key in GLOBAL_TERMINAL_MARKER_KEYS
    ):
        return True
    authority = _normalized_seed_authority(seed_authority)
    owner = value.get("owner")
    if _owner_partially_claims_global_pass(owner, authority):
        return True

    keys = {str(key) for key in value}
    status = str(value.get("status") or "")
    seed_ref = str(value.get("seed_ref") or "")
    defect_fingerprint = str(value.get("defect_fingerprint") or "")
    seed_binding = str(value.get("seed_binding_identity") or "")
    pass_action_shape = bool(
        isinstance(value.get("candidate_compression"), Mapping)
        and isinstance(owner, Mapping)
        and status in {"completed", "failed"}
        and (
            {
                "physical_request_delta",
                "behavior_impact",
            }.issubset(keys)
            or bool(
                {
                    "candidate_evidence_capsules",
                    "judgment",
                    "blocker",
                    "missing_evidence",
                    "physical_request_exact",
                }.intersection(keys)
            )
        )
    )

    def uniquely_matches_authority(
        claims: Sequence[Tuple[str, str]],
    ) -> bool:
        claimed = [(name, claim) for name, claim in claims if claim]
        if not claimed:
            return False
        matches: List[Set[str]] = []
        for name, claim in claimed:
            if name == "seed_binding_identity":
                matched = {claim} if claim in authority else set()
            else:
                matched = {
                    binding
                    for binding, facts in authority.items()
                    if claim == facts[name]
                }
            matches.append(matched)
        common = set.intersection(*matches) if matches else set()
        if any(not matched for matched in matches) or len(common) != 1:
            raise ValueError(
                "residual global terminal authority is contradictory or "
                "ambiguous"
            )
        return True

    owner_seed_binding = (
        str(owner.get("seed_binding_identity") or "")
        if isinstance(owner, Mapping)
        else ""
    )
    if "pass" in allowed_kinds and pass_action_shape:
        return uniquely_matches_authority(
            (
                ("seed_binding_identity", seed_binding),
                ("seed_binding_identity", owner_seed_binding),
                ("seed_ref", seed_ref),
                ("defect_fingerprint", defect_fingerprint),
            )
        )

    node_ref = str(value.get("node_ref") or "")
    defect_state_id = str(value.get("defect_state_id") or "")
    episode_shape = bool(
        isinstance(owner, Mapping)
        and {
            "node_ref",
            "hypothesis_id",
            "reason",
            "details",
            "depth",
        }.issubset(keys)
        and type(value.get("depth")) is int
        and not str(value.get("hypothesis_id") or "")
        and str(value.get("reason") or "").startswith("global_")
    )
    if "episode" in allowed_kinds and episode_shape:
        return uniquely_matches_authority(
            (
                ("seed_binding_identity", owner_seed_binding),
                ("seed_ref", node_ref),
                ("defect_state_id", defect_state_id),
            )
        )
    return False


def _classify_global_pass_records(
    investigation_journal: Iterable[Any],
    *,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    completed: List[Mapping[str, Any]] = []
    failed: List[Mapping[str, Any]] = []
    authority = _normalized_seed_authority(seed_authority)
    for index, item in enumerate(investigation_journal):
        if not isinstance(item, Mapping):
            continue
        if not _global_terminal_residual_signature(
            item,
            seed_authority=authority,
            allowed_kinds={"pass"},
        ):
            continue
        status = str(item.get("status") or "")
        expected_keys = (
            COMPLETED_GLOBAL_PASS_KEYS
            if status == "completed"
            else FAILED_GLOBAL_PASS_KEYS
            if status == "failed"
            else frozenset()
        )
        if (
            item.get("kind") != "global_candidate_pass"
            or not expected_keys
            or {str(key) for key in item} != set(expected_keys)
        ):
            raise ValueError(
                "global pass record[{0}] has unknown status, marker, or "
                "exact schema".format(index)
            )
        seed_ref = str(item.get("seed_ref") or "")
        defect_fingerprint = str(
            item.get("defect_fingerprint") or ""
        )
        expected_seed_binding, expected_pass_identity, expected_owner = (
            _canonical_global_pass_facts(
                seed_ref=seed_ref,
                defect_fingerprint=defect_fingerprint,
            )
        )
        seed_binding_identity = str(
            item.get("seed_binding_identity") or ""
        )
        owner = LocalStateOwner.from_dict(item.get("owner"))
        physical_request_delta = item.get("physical_request_delta")
        if (
            seed_binding_identity != expected_seed_binding
            or (
                authority
                and expected_seed_binding not in authority
            )
            or str(item.get("pass_identity") or "")
            != expected_pass_identity
            or owner != expected_owner
            or type(physical_request_delta) is not int
            or physical_request_delta < 0
            or (
                status == "failed"
                and type(item.get("physical_request_exact")) is not bool
            )
            or item.get("behavior_impact")
            != "none_offline_analysis_only"
            or not isinstance(item.get("candidate_compression"), Mapping)
        ):
            raise ValueError(
                "global pass record[{0}] has malformed identity, owner, "
                "accounting, or policy fields".format(index)
            )
        if status == "completed":
            if (
                not isinstance(
                    item.get("candidate_evidence_capsules"),
                    (list, tuple),
                )
                or not isinstance(item.get("judgment"), Mapping)
            ):
                raise ValueError(
                    "completed global pass record[{0}] is malformed".format(
                        index
                    )
                )
            completed.append(item)
            continue
        projection = _global_failure_projection_from_action(item)
        if (
            list(item.get("missing_evidence") or ())
            != projection["missing_evidence"]
            or str(item.get("blocker") or "") != projection["blocker"]
            or str(item.get("reason") or "") != projection["detail"]
        ):
            raise ValueError(
                "failed global pass record[{0}] contradicts its projection".format(
                    index
                )
            )
        failed.append(item)
    return completed, failed


def _classify_global_failure_episodes(
    unresolved_branches: Iterable[Any],
    *,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> List[Mapping[str, Any]]:
    episodes: List[Mapping[str, Any]] = []
    authority = _normalized_seed_authority(seed_authority)
    for index, item in enumerate(unresolved_branches):
        if not isinstance(item, Mapping):
            continue
        if not _global_terminal_residual_signature(
            item,
            seed_authority=authority,
            allowed_kinds={"episode"},
        ):
            continue
        if {str(key) for key in item} != set(FAILED_GLOBAL_EPISODE_KEYS):
            raise ValueError(
                "global failure episode[{0}] has an unknown marker or exact "
                "schema".format(index)
            )
        projection = _validated_global_failure_projection(
            item.get("failure_projection")
        )
        if (
            authority
            and projection["seed_binding_identity"] not in authority
        ):
            raise ValueError(
                "global failure episode[{0}] has no seed authority".format(
                    index
                )
            )
        if (
            str(item.get("global_pass_identity") or "")
            != projection["pass_identity"]
            or str(item.get("node_ref") or "") != projection["seed_ref"]
            or str(item.get("reason") or "") != projection["blocker"]
            or str(item.get("details") or "") != projection["detail"]
            or item.get("owner") != projection["owner"]
            or type(item.get("depth")) is not int
            or item.get("depth") < 0
        ):
            raise ValueError(
                "global failure episode[{0}] contradicts its canonical "
                "projection".format(index)
            )
        episodes.append(item)
    return episodes


def _completed_global_passes(
    investigation_journal: Iterable[Any],
    *,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> List[Mapping[str, Any]]:
    return _classify_global_pass_records(
        investigation_journal,
        seed_authority=seed_authority,
    )[0]


def _terminal_global_passes(
    investigation_journal: Iterable[Any],
    *,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> List[Mapping[str, Any]]:
    completed, failed = _classify_global_pass_records(
        investigation_journal,
        seed_authority=seed_authority,
    )
    return [*completed, *failed]


def _global_passes_by_owner(
    investigation_journal: Iterable[Any],
    *,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Mapping[str, Any]]:
    output = {}
    for action in _terminal_global_passes(
        investigation_journal,
        seed_authority=seed_authority,
    ):
        LocalStateOwner.from_dict(action.get("owner"))
        key = str(action.get("pass_identity") or "")
        expected_seed_binding, expected_pass_identity, expected_owner = (
            _canonical_global_pass_facts(
                seed_ref=str(action.get("seed_ref") or ""),
                defect_fingerprint=str(
                    action.get("defect_fingerprint") or ""
                ),
            )
        )
        if (
            not key
            or str(action.get("seed_binding_identity") or "")
            != expected_seed_binding
            or key != expected_pass_identity
            or LocalStateOwner.from_dict(action.get("owner"))
            != expected_owner
        ):
            raise ValueError(
                "global pass action identity contradicts its seed binding"
            )
        if action.get("status") == "failed":
            _global_failure_projection_from_action(action)
        if key in output:
            raise ValueError(
                "global pass actions contain a duplicate per-seed identity"
            )
        output[key] = action
    return output


def _owned_payload(value: Mapping[str, Any], owner: Any) -> JsonDict:
    return {
        **copy.deepcopy(dict(value)),
        "owner": copy.deepcopy(owner),
    }


def _validate_global_pass_derivations(
    investigation_journal: Iterable[Any],
    metadata: Mapping[str, Any],
    *,
    label: str,
    seed_authority: Optional[Mapping[str, Any]] = None,
) -> None:
    journal = tuple(investigation_journal)
    completed_passes, failed_passes = _classify_global_pass_records(
        journal,
        seed_authority=seed_authority,
    )
    terminal_passes = [*completed_passes, *failed_passes]
    _global_passes_by_owner(
        terminal_passes,
        seed_authority=seed_authority,
    )
    expected_judgments = [
        _owned_payload(action["judgment"], action["owner"])
        for action in completed_passes
        if isinstance(action.get("judgment"), Mapping)
    ]
    expected_compression = [
        _owned_payload(action["candidate_compression"], action["owner"])
        for action in completed_passes
        if isinstance(action.get("candidate_compression"), Mapping)
    ]
    expected_expansions = [
        _owned_payload(expansion, action["owner"])
        for action in completed_passes
        if isinstance(action.get("judgment"), Mapping)
        for expansion in action["judgment"].get("expansion_requests") or ()
        if isinstance(expansion, Mapping)
    ]
    expected_failures = [
        _global_failure_projection_from_action(action)
        for action in terminal_passes
        if action.get("status") == "failed"
    ]
    _require_canonical_bijection(
        expected_judgments,
        metadata.get("global_candidate_judgments") or (),
        label="{0} global candidate judgments".format(label),
    )
    _require_canonical_bijection(
        expected_compression,
        metadata.get("candidate_compression") or (),
        label="{0} candidate compression".format(label),
    )
    _require_canonical_bijection(
        expected_expansions,
        metadata.get("recursive_expansion_reasons") or (),
        label="{0} expansion reasons".format(label),
    )
    _require_canonical_bijection(
        expected_failures,
        metadata.get("global_candidate_failures") or (),
        label="{0} global candidate failures".format(label),
    )
    expected_physical_requests = sum(
        int(action.get("physical_request_delta") or 0)
        for action in terminal_passes
    )
    if (
        int(metadata.get("global_candidate_pass_count") or 0)
        != len(terminal_passes)
        or int(metadata.get("global_judge_physical_request_count") or 0)
        != expected_physical_requests
        or str(metadata.get("fusion_mode") or "")
        != ("retrieval-global" if terminal_passes else "off")
    ):
        raise ValueError(
            "{0} global pass metadata contradicts authoritative actions".format(
                label
            )
        )


def _validate_restored_report_local_state_owners(
    graph: TraceGraph,
    report: RecursiveAttributionReport,
    *,
    action_records: Optional[Iterable[Any]] = None,
) -> None:
    metadata = report.to_dict()["metadata"]
    frontier_payload = metadata.get("frontier_checkpoint")
    hypothesis_payload = metadata.get("hypothesis_snapshot")
    if not isinstance(frontier_payload, Mapping) or not isinstance(
        hypothesis_payload, list
    ):
        raise ValueError(
            "restored report owner validation requires frontier and ledger snapshots"
        )
    ledger = HypothesisLedger.from_snapshot(hypothesis_payload)
    frontier = RecursiveFrontier.from_checkpoint(
        frontier_payload,
        hypotheses_by_id=ledger.hypotheses_by_id(),
    )
    state = RecursiveAnalysisState(
        graph=graph,
        start_refs=report.start_refs,
        objective=report.objective,
        analysis_perspective=report.analysis_perspective,
        ledger=ledger,
        frontier=frontier,
        defect_states={
            item.fingerprint: item for item in report.defect_states
        },
        causal_relations=list(report.causal_relations),
        step_judgments=list(report.step_judgments),
        step_action_projection=copy.deepcopy(
            list(metadata.get("step_action_projection") or ())
        ),
        visited_order=list(report.visited_order),
        visited_entries=[
            copy.deepcopy(dict(item)) for item in report.visited_entries
        ],
        unresolved_branches=copy.deepcopy(
            list(metadata.get("unresolved_branches") or ())
        ),
        investigation_journal=[
            copy.deepcopy(dict(item))
            for item in report.investigation_journal
        ],
        introduction_bindings=copy.deepcopy(
            list(metadata.get("introduction_bindings") or ())
        ),
        confirmations=list(report.confirmations),
        confirmation_queue=copy.deepcopy(
            list(metadata.get("confirmation_queue") or ())
        ),
        confirmation_queue_keys={
            tuple(str(part) for part in item)
            for item in metadata.get("confirmation_queue_keys") or ()
            if isinstance(item, (list, tuple))
        },
        confirmation_journal=copy.deepcopy(
            list(metadata.get("confirmation_journal") or ())
        ),
        confirmation_action_projection=copy.deepcopy(
            list(metadata.get("confirmation_action_projection") or ())
        ),
    )
    state.seed_ledger = {
        builder.key: builder
        for builder in (
            SeedAttributionBuilder.from_dict(item.to_dict())
            for item in report.seed_results
        )
    }
    state.seed_count = len(state.seed_ledger)
    state.hypothesis_seed_keys = {
        hypothesis.hypothesis_id: hypothesis.seed_binding_identity
        for hypothesis in ledger.hypotheses_by_id().values()
    }
    state.validate_confirmation_queue_bound()
    state._validate_local_state_owners(action_records)
    _validate_global_pass_derivations(
        state.investigation_journal,
        metadata,
        label="restored report",
        seed_authority=_seed_authority_from_records(
            builder.to_dict() for builder in state.seed_ledger.values()
        ),
    )


def validate_recursive_report_against_graph(
    graph: TraceGraph,
    report: RecursiveAttributionReport,
    *,
    label: str,
    action_records: Optional[Iterable[Any]] = None,
) -> None:
    """Apply the analyzer's exact restore validation to a report."""
    formal_unbound_starts = [
        ref
        for ref in report.start_refs
        if graph.active_revision_evidence_eligible(ref)
        and not graph.active_revision_start_eligible(ref)
    ]
    if formal_unbound_starts:
        raise ValueError(
            "{0} contains an analysis start without strict active-start "
            "provenance: {1}".format(label, sorted(formal_unbound_starts))
        )
    _assert_report_grounded_evidence(graph, report, label=label)
    _validate_restored_report_local_state_owners(
        graph,
        report,
        action_records=action_records,
    )


def _assert_published_non_root_factors(
    graph: TraceGraph,
    *,
    confirmations: Sequence[RootConfirmation],
    seed_results: Sequence[SeedAttributionResult],
    defect_states: Sequence[DefectState],
    contributing_conditions: Sequence[CausalFactor],
    amplifying_factors: Sequence[CausalFactor],
    rejected_candidates: Sequence[RejectedCandidate],
    analysis_perspective: str,
    label: str,
) -> None:
    confirmations = {
        confirmation.confirmation_identity: confirmation
        for confirmation in confirmations
    }
    seeds_by_binding = {
        seed_binding_identity_for(seed.start_ref, seed.defect_fingerprint): seed
        for seed in seed_results
    }
    publications = [
        ("contributing_condition", item) for item in contributing_conditions
    ]
    publications.extend(
        ("amplifying_factor", item) for item in amplifying_factors
    )
    publications.extend(
        ("rejected_candidate", item) for item in rejected_candidates
    )
    for published_role, item in publications:
        confirmation = RootConfirmation.from_dict(item.to_dict()["confirmation"])
        if not authored_root_candidate_eligible(
            graph, confirmation.candidate_ref
        ):
            raise ValueError(
                "{0} published non-root factor candidate is ineligible for the active revision".format(
                    label
                )
            )
        canonical = confirmations.get(confirmation.confirmation_identity)
        owner = seeds_by_binding.get(confirmation.seed_binding_identity)
        if (
            canonical != confirmation
            or owner is None
            or confirmation.confirmation_identity
            not in owner.confirmation_identities
            or confirmation.defect_fingerprint not in {
                state.fingerprint
                for state in defect_states
            }
            | {owner.defect_fingerprint}
        ):
            raise ValueError(
                "{0} published non-root factor has no exact confirmation owner".format(
                    label
                )
            )
        if published_role != "rejected_candidate" and confirmation.factor_role != published_role:
            raise ValueError(
                "{0} published non-root factor role contradicts confirmation".format(
                    label
                )
            )
        if published_role == "rejected_candidate" and confirmation.factor_role not in {
            "unrelated",
            "unknown",
        }:
            raise ValueError(
                "{0} rejected candidate role contradicts confirmation".format(label)
            )
        expected_publication = (
            canonical_rejected_candidate_publication(confirmation)
            if published_role == "rejected_candidate"
            else canonical_causal_factor_publication(
                confirmation=confirmation,
                analysis_perspective=analysis_perspective,
            )
        )
        if item != expected_publication:
            raise ValueError(
                "{0} published non-root factor contradicts canonical "
                "publication".format(label)
            )
        if not confirmation.evidence_refs:
            raise ValueError(
                "{0} published non-root factor requires grounded evidence".format(label)
            )
        _assert_active_confirmation_path(
            graph,
            confirmation.recursive_path,
            candidate_ref=confirmation.candidate_ref,
            seed_ref=owner.start_ref,
            label="{0} published non-root factor".format(label),
        )
        assessments = [
            assessment
            for assessment in owner.global_judgment.get("assessments") or ()
            if isinstance(assessment, Mapping)
            and str(assessment.get("candidate_ref") or "")
            == confirmation.candidate_ref
        ]
        if assessments:
            assessment = assessments[0]
            if (
                len(assessments) != 1
                or tuple(assessment.get("causal_path_refs") or ())
                != confirmation.recursive_path
                or str(assessment.get("causal_role") or "")
                != confirmation.factor_role
            ):
                raise ValueError(
                    "{0} published non-root factor contradicts its global assessment facts".format(
                        label
                    )
                )


def _assert_canonical_published_roots(
    graph: TraceGraph,
    *,
    confirmations: Sequence[RootConfirmation],
    seed_results: Sequence[SeedAttributionResult],
    confirmed_roots: Sequence[ConfirmedRoot],
    co_roots: Sequence[ConfirmedRoot],
    analysis_perspective: str,
    label: str,
) -> None:
    confirmation_by_identity: Dict[str, RootConfirmation] = {}
    for confirmation in confirmations:
        if confirmation.confirmation_identity in confirmation_by_identity:
            raise ValueError(
                "{0} contains a duplicate confirmation identity".format(label)
            )
        confirmation_by_identity[
            confirmation.confirmation_identity
        ] = confirmation

    seeds_by_binding: Dict[str, SeedAttributionResult] = {}
    for seed in seed_results:
        binding = seed_binding_identity_for(
            seed.start_ref,
            seed.defect_fingerprint,
        )
        if binding in seeds_by_binding:
            raise ValueError(
                "{0} contains a duplicate seed binding".format(label)
            )
        seeds_by_binding[binding] = seed

    roots_by_seed: Dict[str, Set[str]] = {}
    for root in (*confirmed_roots, *co_roots):
        embedded = RootConfirmation.from_dict(dict(root.confirmation))
        canonical = confirmation_by_identity.get(
            embedded.confirmation_identity
        )
        owner = seeds_by_binding.get(embedded.seed_binding_identity)
        candidate_node = graph.nodes.get(graph.resolve(root.node_ref) or "")
        if (
            canonical != embedded
            or owner is None
            or candidate_node is None
            or embedded.status != "confirmed"
            or embedded.factor_role != "necessary_cause"
            or embedded.analysis_perspective != analysis_perspective
            or embedded.candidate_ref != root.node_ref
            or embedded.defect_fingerprint != root.defect_state.fingerprint
            or not embedded.recursive_path
            or embedded.recursive_path[-1] != owner.start_ref
        ):
            raise ValueError(
                "{0} root has no exact confirmation, seed, defect, or graph owner".format(
                    label
                )
            )
        expected = canonical_confirmed_root_publication(
            confirmation=embedded,
            defect_state=root.defect_state,
            candidate_node=candidate_node,
            seed_start_ref=owner.start_ref,
        )
        if root != expected:
            raise ValueError(
                "{0} root contradicts canonical publication fields".format(
                    label
                )
            )
        roots_by_seed.setdefault(embedded.seed_binding_identity, set()).add(
            embedded.confirmation_identity
        )

    expected_primary, expected_co_roots = canonical_ranked_root_publications(
        (*confirmed_roots, *co_roots),
    )
    if (
        tuple(confirmed_roots) != expected_primary
        or tuple(co_roots) != expected_co_roots
    ):
        raise ValueError(
            "{0} primary/co-root roles or order contradict canonical ranking".format(
                label
            )
        )

    for binding, seed in seeds_by_binding.items():
        expected_identities = {
            identity
            for identity in seed.confirmation_identities
            if identity in confirmation_by_identity
            and confirmation_by_identity[identity].status == "confirmed"
        }
        published_identities = roots_by_seed.get(binding, set())
        if seed.outcome == "confirmed_root":
            published_refs = {
                confirmation_by_identity[identity].candidate_ref
                for identity in published_identities
            }
            if (
                published_identities != expected_identities
                or published_refs != set(seed.confirmed_root_refs)
            ):
                raise ValueError(
                    "{0} confirmed seed root publication is incomplete".format(
                        label
                    )
                )
        elif published_identities:
            raise ValueError(
                "{0} non-confirmed seed owns a published root".format(label)
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
    if any(
        not graph.active_revision_evidence_eligible(ref)
        for ref in canonical_path
    ):
        raise ValueError(
            "{0} confirmation path contains evidence ineligible for the active revision".format(
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


def _validate_provider_cache_stats(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("provider cache stats must be an object")
    if any(type(key) is not str for key in value):
        raise ValueError("provider cache stats keys must be strings")
    if set(value) == {"enabled"}:
        if value["enabled"] is not False:
            raise ValueError("minimal provider cache stats must be disabled")
        return {"enabled": False}
    _require_exact_checkpoint_keys(
        value,
        PROVIDER_CACHE_STATS_KEYS,
        "provider cache stats",
    )
    cache_stats = dict(value)
    if type(cache_stats["enabled"]) is not bool:
        raise ValueError("provider cache enabled flag is invalid")
    if type(cache_stats["path"]) is not str:
        raise ValueError("provider cache path is invalid")
    for key in PROVIDER_CACHE_COUNT_KEYS:
        if type(cache_stats[key]) is not int or cache_stats[key] < 0:
            raise ValueError(
                "provider cache counter is invalid: {0}".format(key)
            )
    write_errors = cache_stats["write_errors"]
    if type(write_errors) is not list or any(
        type(item) is not str for item in write_errors
    ):
        raise ValueError("provider cache write errors are invalid")
    if cache_stats["write_error_count"] != len(write_errors):
        raise ValueError("provider cache write error count is inconsistent")
    if cache_stats["enabled"] != bool(cache_stats["path"]):
        raise ValueError(
            "provider cache enabled flag does not match its path"
        )
    return copy.deepcopy(cache_stats)


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
    provider["cache_stats"] = _validate_provider_cache_stats(
        provider["cache_stats"]
    )
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


def _node_semantic_content(graph: TraceGraph, node: TraceNode) -> str:
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

    data = graph.sanitize_judge_visible_payload(
        without_hydrated_artifacts(node.data)
    )
    return stable_json(
        {
            "component": node.component,
            "event_type": node.event_type,
            "title": node.title,
            "status": node.status,
            "data": data,
        }
    )


def _artifact_hydration_manifest(
    graph: TraceGraph, node: TraceNode
) -> Optional[JsonDict]:
    manifest = graph.artifact_hydration_manifest(node.ref)
    if not manifest.get("referenced_artifact_ids"):
        return None
    return copy.deepcopy(manifest) if manifest.get("hydrated_artifacts") else None


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


def _owner_for_item(
    item: FrontierItem, occurrence_key: str
) -> LocalStateOwner:
    return LocalStateOwner.create(
        seed_binding_identity=item.seed_binding_identity,
        hypothesis_id=item.hypothesis_id,
        visit_key=item.visit_key,
        occurrence_key=occurrence_key,
    )


def _owner_for_seed_projection(
    *,
    seed_binding_identity: str,
    node_ref: str,
    defect_state: DefectState,
    occurrence_key: str,
) -> LocalStateOwner:
    return _owner_for_seed_projection_facts(
        seed_binding_identity=seed_binding_identity,
        node_ref=node_ref,
        defect_fingerprint=defect_state.fingerprint,
        occurrence_key=occurrence_key,
    )


def _owner_for_seed_projection_facts(
    *,
    seed_binding_identity: str,
    node_ref: str,
    defect_fingerprint: str,
    occurrence_key: str,
) -> LocalStateOwner:
    hypothesis_id = "seed_projection:{0}".format(
        hashlib.sha256(
            stable_json(
                {
                    "seed_binding_identity": seed_binding_identity,
                    "node_ref": node_ref,
                    "defect_fingerprint": defect_fingerprint,
                }
            ).encode("utf-8")
        ).hexdigest()[:24]
    )
    visit_key = "seed_projection_visit:{0}".format(
        hashlib.sha256(
            stable_json(
                {
                    "seed_binding_identity": seed_binding_identity,
                    "hypothesis_id": hypothesis_id,
                    "node_ref": node_ref,
                }
            ).encode("utf-8")
        ).hexdigest()
    )
    return LocalStateOwner.create(
        seed_binding_identity=seed_binding_identity,
        hypothesis_id=hypothesis_id,
        visit_key=visit_key,
        occurrence_key=occurrence_key,
    )


def _global_pass_identity(seed_binding_identity: str) -> str:
    return "global_pass:v1:{0}".format(
        hashlib.sha256(
            stable_json(
                {
                    "schema": "global-pass-identity/v1",
                    "seed_binding_identity": str(seed_binding_identity),
                }
            ).encode("utf-8")
        ).hexdigest()
    )


def _global_judge_action_key(pass_identity: str) -> str:
    if not str(pass_identity).startswith("global_pass:v1:"):
        raise ValueError("global Judge action requires a canonical pass identity")
    return "global_judge:{0}".format(pass_identity)


def _global_judge_request_identity(
    request: GlobalCandidateJudgeRequest,
) -> str:
    return "global_request:v1:{0}".format(
        hashlib.sha256(
            stable_json(request.validation_envelope()).encode("utf-8")
        ).hexdigest()
    )


def _global_judge_capsule_identity(
    request: GlobalCandidateJudgeRequest,
) -> str:
    return "global_capsules:v1:{0}".format(
        hashlib.sha256(
            stable_json(
                request.validation_envelope()[
                    "candidate_evidence_capsules"
                ]
            ).encode("utf-8")
        ).hexdigest()
    )


def _global_pass_owner(builder: "SeedAttributionBuilder") -> LocalStateOwner:
    return _canonical_global_pass_facts(
        seed_ref=builder.start_ref,
        defect_fingerprint=builder.defect_state.fingerprint,
    )[2]


def _canonical_global_pass_facts(
    *,
    seed_ref: str,
    defect_fingerprint: str,
) -> Tuple[str, str, LocalStateOwner]:
    seed_ref = str(seed_ref or "")
    defect_fingerprint = str(defect_fingerprint or "")
    if not seed_ref or not defect_fingerprint:
        raise ValueError(
            "global pass requires canonical seed ref and defect fingerprint"
        )
    seed_binding_identity = seed_binding_identity_for(
        seed_ref, defect_fingerprint
    )
    owner = _owner_for_seed_projection_facts(
        seed_binding_identity=seed_binding_identity,
        node_ref=seed_ref,
        defect_fingerprint=defect_fingerprint,
        occurrence_key="global_candidate_pass",
    )
    return (
        seed_binding_identity,
        _global_pass_identity(seed_binding_identity),
        owner,
    )


def _global_judge_action_base(
    *,
    builder: "SeedAttributionBuilder",
    item: FrontierItem,
    request: GlobalCandidateJudgeRequest,
    candidate_compression: Mapping[str, Any],
    physical_requests_reserved: int,
) -> JsonDict:
    if (
        isinstance(physical_requests_reserved, bool)
        or not isinstance(physical_requests_reserved, int)
        or physical_requests_reserved < 0
    ):
        raise ValueError("global Judge reserved request count is invalid")
    pass_identity = _global_pass_identity(builder.key)
    return {
        "status": "in_flight",
        "pass_identity": pass_identity,
        "seed_binding_identity": builder.key,
        "seed_ref": builder.start_ref,
        "defect_fingerprint": builder.defect_state.fingerprint,
        "hypothesis_id": item.hypothesis_id,
        "visit_key": item.visit_key,
        "owner": _global_pass_owner(builder).to_dict(),
        "request_identity": _global_judge_request_identity(request),
        "validation_envelope": request.validation_envelope(),
        "capsule_identity": _global_judge_capsule_identity(request),
        "candidate_compression": copy.deepcopy(
            dict(candidate_compression)
        ),
        "physical_requests_reserved": physical_requests_reserved,
    }


def _validate_global_judge_failure_accounting(
    *,
    blocker: str,
    physical_request_delta: int,
    physical_request_exact: bool,
    physical_requests_reserved: int,
) -> None:
    if physical_request_delta > physical_requests_reserved:
        raise ValueError(
            "failed global Judge action exceeded its reserved allowance"
        )
    if not physical_request_exact and (
        blocker != "global_judge_interrupted"
        or physical_request_delta != physical_requests_reserved
    ):
        raise ValueError(
            "inexact global Judge failure must conservatively consume its "
            "full reservation as an interrupted request"
        )


def _validated_global_judge_action_history(
    action_records: Iterable[Any],
    *,
    max_judge_requests: Optional[int] = None,
    cache_identity: Optional[str] = None,
) -> Dict[str, Tuple[JsonDict, ...]]:
    if max_judge_requests is not None and (
        isinstance(max_judge_requests, bool)
        or not isinstance(max_judge_requests, int)
        or max_judge_requests < 0
    ):
        raise ValueError("global Judge lifecycle budget is invalid")
    grouped: Dict[str, List[JsonDict]] = {}
    for record in action_records:
        if not isinstance(record, Mapping):
            continue
        operation = str(record.get("operation") or "")
        if operation not in GLOBAL_JUDGE_ACTION_OPERATIONS:
            continue
        semantic_key = str(record.get("semantic_key") or "")
        payload = record.get("payload")
        if not semantic_key or not isinstance(payload, Mapping):
            raise ValueError("global Judge action record is malformed")
        expected_keys = {
            "global_judge_started": GLOBAL_JUDGE_STARTED_PAYLOAD_KEYS,
            "global_judge_completed": GLOBAL_JUDGE_COMPLETED_PAYLOAD_KEYS,
            "global_judge_failed": GLOBAL_JUDGE_FAILED_PAYLOAD_KEYS,
        }[operation]
        _require_exact_checkpoint_keys(
            payload,
            set(expected_keys),
            "global Judge action payload",
        )
        grouped.setdefault(semantic_key, []).append(
            {
                "operation": operation,
                "semantic_key": semantic_key,
                "payload": copy.deepcopy(dict(payload)),
            }
        )

    validated: Dict[str, Tuple[JsonDict, ...]] = {}
    cumulative_physical_requests = 0
    cumulative_uncertainty = 0
    cumulative_logical_calls = 0
    observed_cache_identity = (
        str(cache_identity) if cache_identity is not None else None
    )
    for semantic_key, records in grouped.items():
        if (
            len(records) not in {1, 2}
            or records[0]["operation"] != "global_judge_started"
            or (
                len(records) == 2
                and records[1]["operation"]
                not in {"global_judge_completed", "global_judge_failed"}
            )
        ):
            raise ValueError(
                "global Judge action lifecycle must contain one started "
                "record followed by at most one terminal record"
            )
        started = records[0]["payload"]
        if started.get("status") != "in_flight":
            raise ValueError("global Judge started action status is invalid")
        envelope = started.get("validation_envelope")
        request = global_candidate_request_from_validation_envelope(envelope)
        expected_request_identity = _global_judge_request_identity(request)
        expected_capsule_identity = _global_judge_capsule_identity(request)
        seed_ref = str(started.get("seed_ref") or "")
        defect_fingerprint = str(
            started.get("defect_fingerprint") or ""
        )
        (
            expected_seed_binding,
            expected_pass_identity,
            expected_owner,
        ) = _canonical_global_pass_facts(
            seed_ref=seed_ref,
            defect_fingerprint=defect_fingerprint,
        )
        reserved = started.get("physical_requests_reserved")
        expected_reserved = (
            None
            if max_judge_requests is None
            else max(
                0,
                max_judge_requests - cumulative_physical_requests,
            )
        )
        if (
            isinstance(reserved, bool)
            or not isinstance(reserved, int)
            or reserved < 0
            or (
                expected_reserved is not None
                and reserved != expected_reserved
            )
            or not str(started.get("hypothesis_id") or "")
            or not str(started.get("visit_key") or "")
            or not isinstance(started.get("candidate_compression"), Mapping)
            or started.get("seed_binding_identity")
            != expected_seed_binding
            or started.get("pass_identity") != expected_pass_identity
            or semantic_key
            != _global_judge_action_key(expected_pass_identity)
            or LocalStateOwner.from_dict(started.get("owner"))
            != expected_owner
            or started.get("request_identity")
            != expected_request_identity
            or started.get("capsule_identity")
            != expected_capsule_identity
            or request.seed_ref != seed_ref
            or request.active_defect.fingerprint != defect_fingerprint
        ):
            raise ValueError(
                "global Judge started action contradicts its factual request"
            )
        cumulative_logical_calls += 1
        if len(records) == 1:
            validated[semantic_key] = tuple(records)
            continue
        terminal_record = records[1]
        terminal = terminal_record["payload"]
        expected_status = (
            "completed"
            if terminal_record["operation"] == "global_judge_completed"
            else "failed"
        )
        expected_terminal_base = copy.deepcopy(dict(started))
        expected_terminal_base["status"] = expected_status
        actual_terminal_base = {
            key: copy.deepcopy(terminal[key])
            for key in GLOBAL_JUDGE_ACTION_BASE_KEYS
        }
        if stable_json(_checkpoint_json(actual_terminal_base)) != stable_json(
            _checkpoint_json(expected_terminal_base)
        ):
            raise ValueError(
                "global Judge terminal action does not match its started action"
            )
        physical_delta = terminal.get("physical_request_delta")
        physical_exact = terminal.get("physical_request_exact")
        if (
            isinstance(physical_delta, bool)
            or not isinstance(physical_delta, int)
            or physical_delta < 0
            or physical_delta > reserved
            or type(physical_exact) is not bool
            or not isinstance(terminal.get("provider_state"), Mapping)
        ):
            raise ValueError(
                "global Judge terminal action accounting is invalid"
            )
        if terminal_record["operation"] == "global_judge_completed":
            if physical_exact is not True:
                raise ValueError(
                    "completed global Judge action requires exact accounting"
                )
            judgment = validate_global_candidate_payload(
                terminal.get("judgment"),
                request=request,
            )
            if judgment.to_dict() != dict(terminal["judgment"]):
                raise ValueError(
                    "completed global Judge action judgment is not canonical"
                )
        else:
            blocker = str(terminal.get("blocker") or "")
            detail = str(terminal.get("detail") or "")
            projection = _validated_global_failure_projection(
                terminal.get("failure_projection")
            )
            if (
                not blocker
                or not detail
                or projection["pass_identity"] != expected_pass_identity
                or projection["seed_binding_identity"]
                != expected_seed_binding
                or projection["seed_ref"] != seed_ref
                or projection["defect_fingerprint"]
                != defect_fingerprint
                or projection["blocker"] != blocker
                or projection["detail"] != detail
                or projection["physical_request_delta"]
                != physical_delta
                or projection["physical_request_exact"]
                != physical_exact
            ):
                raise ValueError(
                    "failed global Judge action projection is inconsistent"
                )
            _validate_global_judge_failure_accounting(
                blocker=blocker,
                physical_request_delta=physical_delta,
                physical_request_exact=physical_exact,
                physical_requests_reserved=reserved,
            )
        cumulative_physical_requests += physical_delta
        if not physical_exact:
            cumulative_uncertainty += 1
        provider_state = terminal["provider_state"]
        terminal_cache_identity = str(
            provider_state.get("cache_identity") or ""
        )
        if observed_cache_identity is None:
            observed_cache_identity = terminal_cache_identity
        if terminal_cache_identity != observed_cache_identity:
            raise ValueError(
                "global Judge lifecycle cache identity is inconsistent"
            )
        _validate_provider_state(
            provider_state,
            SimpleNamespace(
                judge_requests=cumulative_physical_requests,
                judge_request_uncertainty_count=cumulative_uncertainty,
                logical_judge_calls=cumulative_logical_calls,
                logical_confirmation_calls=0,
                investigation_rounds=0,
                artifact_bytes=0,
            ),
            cache_identity=observed_cache_identity or "",
        )
        validated[semantic_key] = tuple(records)
    return validated


def _validated_global_judge_action(
    record: Mapping[str, Any],
    *,
    builder: "SeedAttributionBuilder",
    item: FrontierItem,
    request: GlobalCandidateJudgeRequest,
    candidate_compression: Mapping[str, Any],
    expected_physical_requests_reserved: int,
) -> JsonDict:
    operation = str(record.get("operation") or "")
    if operation not in GLOBAL_JUDGE_ACTION_OPERATIONS:
        raise ValueError("unsupported global Judge action operation")
    expected_key = _global_judge_action_key(
        _global_pass_identity(builder.key)
    )
    if str(record.get("semantic_key") or "") != expected_key:
        raise ValueError("global Judge action semantic key is invalid")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("global Judge action payload must be an object")
    expected_keys = {
        "global_judge_started": GLOBAL_JUDGE_STARTED_PAYLOAD_KEYS,
        "global_judge_completed": GLOBAL_JUDGE_COMPLETED_PAYLOAD_KEYS,
        "global_judge_failed": GLOBAL_JUDGE_FAILED_PAYLOAD_KEYS,
    }[operation]
    _require_exact_checkpoint_keys(
        payload,
        set(expected_keys),
        "global Judge action payload",
    )
    reserved = payload.get("physical_requests_reserved")
    if (
        isinstance(reserved, bool)
        or not isinstance(reserved, int)
        or reserved < 0
        or reserved != expected_physical_requests_reserved
    ):
        raise ValueError(
            "global Judge action reserved request count is invalid"
        )
    expected_base = _global_judge_action_base(
        builder=builder,
        item=item,
        request=request,
        candidate_compression=candidate_compression,
        physical_requests_reserved=reserved,
    )
    actual_base = {
        key: copy.deepcopy(payload[key])
        for key in GLOBAL_JUDGE_ACTION_BASE_KEYS
    }
    expected_status = {
        "global_judge_started": "in_flight",
        "global_judge_completed": "completed",
        "global_judge_failed": "failed",
    }[operation]
    expected_base["status"] = expected_status
    if stable_json(_checkpoint_json(actual_base)) != stable_json(
        _checkpoint_json(expected_base)
    ):
        raise ValueError(
            "global Judge action contradicts its factual request"
        )
    if operation == "global_judge_started":
        return copy.deepcopy(dict(payload))
    physical_delta = payload.get("physical_request_delta")
    physical_exact = payload.get("physical_request_exact")
    if (
        isinstance(physical_delta, bool)
        or not isinstance(physical_delta, int)
        or physical_delta < 0
        or type(physical_exact) is not bool
    ):
        raise ValueError(
            "global Judge terminal action accounting is invalid"
        )
    if operation == "global_judge_completed":
        if physical_exact is not True or physical_delta > reserved:
            raise ValueError(
                "completed global Judge action requires exact accounting"
            )
        judgment_payload = payload.get("judgment")
        if not isinstance(judgment_payload, Mapping):
            raise ValueError(
                "completed global Judge action judgment must be an object"
            )
        judgment = validate_global_candidate_payload(
            judgment_payload,
            request=request,
        )
        if judgment.to_dict() != dict(judgment_payload):
            raise ValueError(
                "completed global Judge action judgment is not canonical"
            )
    else:
        blocker = str(payload.get("blocker") or "")
        detail = str(payload.get("detail") or "")
        if not blocker or not detail:
            raise ValueError(
                "failed global Judge action requires blocker and detail"
            )
        projection = _validated_global_failure_projection(
            payload.get("failure_projection")
        )
        if (
            projection["pass_identity"]
            != _global_pass_identity(builder.key)
            or projection["seed_binding_identity"] != builder.key
            or projection["seed_ref"] != builder.start_ref
            or projection["defect_fingerprint"]
            != builder.defect_state.fingerprint
            or projection["blocker"] != blocker
            or projection["detail"] != detail
            or projection["physical_request_delta"] != physical_delta
            or projection["physical_request_exact"] != physical_exact
        ):
            raise ValueError(
                "failed global Judge action projection is inconsistent"
            )
        _validate_global_judge_failure_accounting(
            blocker=blocker,
            physical_request_delta=physical_delta,
            physical_request_exact=physical_exact,
            physical_requests_reserved=reserved,
        )
    if not isinstance(payload.get("provider_state"), Mapping):
        raise ValueError(
            "global Judge terminal action provider state must be an object"
        )
    return copy.deepcopy(dict(payload))


def _validated_investigation_replay_action(
    record: Mapping[str, Any],
    *,
    action_key: str,
    directive: InvestigationDirective,
    item: FrontierItem,
) -> Tuple[str, JsonDict, Optional[InvestigationResult]]:
    operation = str(record.get("operation") or "")
    if operation not in {
        "investigation_started",
        "investigation_completed",
        "investigation_failed",
    }:
        raise ValueError("unsupported investigation replay action")
    if str(record.get("semantic_key") or "") != action_key:
        raise ValueError(
            "investigation replay action ownership key is invalid"
        )
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("investigation replay action payload is invalid")
    expected_keys = {
        "investigation_started": {
            "directive_id",
            "directive",
            "visit_key",
            "status",
        },
        "investigation_completed": {
            "directive_id",
            "visit_key",
            "status",
            "result",
        },
        "investigation_failed": {
            "directive_id",
            "visit_key",
            "status",
            "reason",
        },
    }[operation]
    _require_exact_checkpoint_keys(
        payload,
        expected_keys,
        "investigation replay action payload",
    )
    if str(payload.get("directive_id") or "") != directive.directive_id:
        raise ValueError(
            "investigation replay directive ownership is invalid"
        )
    if payload.get("visit_key") != item.visit_key:
        raise ValueError(
            "investigation replay visit ownership is invalid"
        )
    if operation == "investigation_started":
        persisted_directive = InvestigationDirective.from_dict(
            payload.get("directive")
            if isinstance(payload.get("directive"), Mapping)
            else {}
        )
        if (
            persisted_directive != directive
            or payload.get("visit_key") != item.visit_key
            or payload.get("status") != "in_flight"
        ):
            raise ValueError(
                "started investigation replay binding is invalid"
            )
        return operation, copy.deepcopy(dict(payload)), None
    if operation == "investigation_failed":
        if (
            payload.get("status") != "unknown"
            or payload.get("reason") != "interrupted_investigation_call"
        ):
            raise ValueError(
                "failed investigation replay binding is invalid"
            )
        return operation, copy.deepcopy(dict(payload)), None
    result_payload = payload.get("result")
    result = InvestigationResult.from_dict(
        dict(result_payload)
        if isinstance(result_payload, Mapping)
        else {}
    )
    if (
        result.directive_id != directive.directive_id
        or result.directive_kind != directive.directive_kind
        or result.tool_name != directive.tool_name
        or payload.get("status") != result.status
    ):
        raise ValueError(
            "completed investigation replay directive binding is invalid"
        )
    return operation, copy.deepcopy(dict(payload)), result


def _global_failure_projection(
    *,
    builder: "SeedAttributionBuilder",
    blocker: str,
    detail: str,
    physical_request_delta: int,
    physical_request_exact: bool,
) -> JsonDict:
    if (
        not isinstance(blocker, str)
        or not blocker.strip()
        or not isinstance(detail, str)
        or not detail.strip()
    ):
        raise ValueError("global failure projection requires blocker and detail")
    if (
        isinstance(physical_request_delta, bool)
        or not isinstance(physical_request_delta, int)
        or physical_request_delta < 0
    ):
        raise ValueError(
            "global failure projection physical request delta is invalid"
        )
    if type(physical_request_exact) is not bool:
        raise ValueError(
            "global failure projection physical request exactness is invalid"
        )
    return {
        "schema": GLOBAL_FAILURE_PROJECTION_SCHEMA,
        "terminal_status": "failed",
        "pass_identity": _global_pass_identity(builder.key),
        "seed_binding_identity": builder.key,
        "seed_ref": builder.start_ref,
        "defect_fingerprint": builder.defect_state.fingerprint,
        "blocker": blocker.strip(),
        "reason": detail.strip(),
        "detail": detail.strip(),
        "missing_evidence": [detail.strip()],
        "physical_request_delta": physical_request_delta,
        "physical_request_exact": physical_request_exact,
        "owner": _global_pass_owner(builder).to_dict(),
    }


def _validated_global_failure_projection(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("global failure projection must be an object")
    _require_exact_checkpoint_keys(
        value,
        set(GLOBAL_FAILURE_PROJECTION_KEYS),
        "global failure projection",
    )
    projection = copy.deepcopy(dict(value))
    seed_binding_identity = str(
        projection.get("seed_binding_identity") or ""
    )
    expected_seed_binding, expected_pass_identity, expected_owner = (
        _canonical_global_pass_facts(
            seed_ref=str(projection.get("seed_ref") or ""),
            defect_fingerprint=str(
                projection.get("defect_fingerprint") or ""
            ),
        )
    )
    if (
        projection.get("schema") != GLOBAL_FAILURE_PROJECTION_SCHEMA
        or projection.get("terminal_status") != "failed"
        or seed_binding_identity != expected_seed_binding
        or str(projection.get("pass_identity") or "")
        != expected_pass_identity
        or not str(projection.get("blocker") or "")
        or not str(projection.get("reason") or "")
        or projection.get("reason") != projection.get("detail")
        or list(projection.get("missing_evidence") or ())
        != [projection.get("detail")]
        or type(projection.get("physical_request_delta")) is not int
        or projection["physical_request_delta"] < 0
        or type(projection.get("physical_request_exact")) is not bool
    ):
        raise ValueError("global failure projection is not canonical")
    owner = LocalStateOwner.from_dict(projection.get("owner"))
    if owner != expected_owner:
        raise ValueError("global failure projection owner is malformed")
    return projection


def _global_failure_projection_from_action(
    action: Mapping[str, Any],
) -> JsonDict:
    projection = {
        "schema": GLOBAL_FAILURE_PROJECTION_SCHEMA,
        "terminal_status": str(action.get("status") or ""),
        "pass_identity": str(action.get("pass_identity") or ""),
        "seed_binding_identity": str(
            action.get("seed_binding_identity") or ""
        ),
        "seed_ref": str(action.get("seed_ref") or ""),
        "defect_fingerprint": str(
            action.get("defect_fingerprint") or ""
        ),
        "blocker": str(action.get("blocker") or ""),
        "reason": str(action.get("reason") or ""),
        "detail": str(action.get("reason") or ""),
        "missing_evidence": copy.deepcopy(
            list(action.get("missing_evidence") or ())
        ),
        "physical_request_delta": action.get("physical_request_delta"),
        "physical_request_exact": action.get("physical_request_exact"),
        "owner": copy.deepcopy(action.get("owner")),
    }
    if (
        projection["terminal_status"] != "failed"
        or projection["pass_identity"]
        != _global_pass_identity(projection["seed_binding_identity"])
        or not projection["seed_ref"]
        or not projection["defect_fingerprint"]
        or not projection["blocker"]
        or not projection["reason"]
        or projection["missing_evidence"] != [projection["detail"]]
        or isinstance(projection["physical_request_delta"], bool)
        or not isinstance(projection["physical_request_delta"], int)
        or projection["physical_request_delta"] < 0
        or type(projection["physical_request_exact"]) is not bool
    ):
        raise ValueError(
            "failed global action does not define a canonical failure projection"
        )
    persisted = _validated_global_failure_projection(
        action.get("failure_projection")
    )
    if stable_json(_checkpoint_json(persisted)) != stable_json(
        _checkpoint_json(projection)
    ):
        raise ValueError(
            "failed global action contradicts its canonical failure projection"
        )
    return projection


def _owned_step_judgment(
    item: FrontierItem,
    judgment: CausalStepJudgment,
    semantic_key: str,
) -> CausalStepJudgment:
    predecessors = tuple(
        replace(
            assessment,
            owner=_owner_for_item(
                item,
                "step_predecessor:{0}:{1}:{2}".format(
                    semantic_key, index, assessment.ref
                ),
            ),
        )
        for index, assessment in enumerate(judgment.predecessors)
    )
    return replace(
        judgment,
        predecessors=predecessors,
        owner=_owner_for_item(
            item, "step_judgment:{0}".format(semantic_key)
        ),
    )


def _step_action_projection(
    *,
    item: FrontierItem,
    semantic_key: str,
    provider_judgment: CausalStepJudgment,
    physical_requests_reserved: int,
    physical_request_delta: int,
    physical_request_exact: bool,
) -> JsonDict:
    if (
        not semantic_key.startswith("step:{0}:".format(item.visit_key))
        or provider_judgment.current_node_ref != item.node_ref
    ):
        raise ValueError(
            "completed step Provider action contradicts its frontier visit"
        )
    for label, amount in (
        ("physical_requests_reserved", physical_requests_reserved),
        ("physical_request_delta", physical_request_delta),
    ):
        if type(amount) is not int or amount < 0:
            raise ValueError(
                "step action {0} must be a nonnegative integer".format(
                    label
                )
            )
    if type(physical_request_exact) is not bool:
        raise ValueError(
            "step action physical_request_exact must be boolean"
        )
    owned = _owned_step_judgment(
        item, provider_judgment, semantic_key
    )
    relations = [
        predecessor.to_dict()
        for predecessor in owned.predecessors
        if not (
            owned.current_defect_status == "absent"
            and predecessor.relation == "unknown"
        )
    ]
    return {
        "schema": STEP_ACTION_PROJECTION_SCHEMA,
        "semantic_key": semantic_key,
        "call_kind": "step",
        "seed_binding_identity": item.seed_binding_identity,
        "hypothesis_id": item.hypothesis_id,
        "visit_key": item.visit_key,
        "owner": owned.owner.to_dict(),
        "physical_requests_reserved": physical_requests_reserved,
        "physical_request_delta": physical_request_delta,
        "physical_request_exact": physical_request_exact,
        "provider_judgment": provider_judgment.to_dict(),
        "step_judgment": owned.to_dict(),
        "causal_relations": relations,
    }


def _step_action_projection_from_record(
    record: Mapping[str, Any],
    *,
    item: FrontierItem,
) -> JsonDict:
    payload = record.get("payload")
    if (
        record.get("operation") != "provider_call_completed"
        or not isinstance(payload, Mapping)
        or payload.get("call_kind") != "step"
        or payload.get("status") != "completed"
    ):
        raise ValueError(
            "completed step Provider action is malformed or incomplete"
        )
    provider_judgment = CausalStepJudgment.from_dict(
        dict(payload.get("judgment") or {})
    )
    return _step_action_projection(
        item=item,
        semantic_key=str(record.get("semantic_key") or ""),
        provider_judgment=provider_judgment,
        physical_requests_reserved=payload.get(
            "physical_requests_reserved"
        ),
        physical_request_delta=payload.get("physical_request_delta"),
        physical_request_exact=payload.get("physical_request_exact"),
    )


def _validated_step_action_projection(
    value: Any,
    *,
    item: FrontierItem,
) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("step action projection must be an object")
    _require_exact_checkpoint_keys(
        value,
        set(STEP_ACTION_PROJECTION_KEYS),
        "step action projection",
    )
    if value.get("schema") != STEP_ACTION_PROJECTION_SCHEMA:
        raise ValueError("unsupported step action projection schema")
    expected = _step_action_projection(
        item=item,
        semantic_key=str(value.get("semantic_key") or ""),
        provider_judgment=CausalStepJudgment.from_dict(
            dict(value.get("provider_judgment") or {})
        ),
        physical_requests_reserved=value.get(
            "physical_requests_reserved"
        ),
        physical_request_delta=value.get("physical_request_delta"),
        physical_request_exact=value.get("physical_request_exact"),
    )
    if stable_json(_checkpoint_json(value)) != stable_json(
        _checkpoint_json(expected)
    ):
        raise ValueError(
            "step action projection contradicts its canonical owner-bound "
            "judgment"
        )
    return expected


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
    decisive_evidence: List[JsonDict] = field(default_factory=list)
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
        owner: LocalStateOwner,
    ) -> None:
        if owner.seed_binding_identity != self.key:
            raise ValueError("global judgment owner does not match seed")
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
        self.global_judgment["owner"] = owner.to_dict()
        self.expansion_history.extend(
            {
                **copy.deepcopy(dict(item)),
                "owner": owner.to_dict(),
            }
            for item in judgment.expansion_requests
        )
        self._record_decisive_evidence(judgment.decisive_evidence_refs, owner)
        if judgment.outcome == "no_defect":
            self.mark_no_defect()
        elif judgment.outcome == "inconclusive":
            self.mark_unresolved(
                "global_judgment_inconclusive",
                "; ".join(judgment.missing_evidence) or judgment.reason,
            )

    def _record_decisive_evidence(
        self, refs: Iterable[str], owner: LocalStateOwner
    ) -> None:
        existing = {
            (
                str(item.get("ref") or ""),
                str(
                    item.get("owner", {}).get("occurrence_identity") or ""
                    if isinstance(item.get("owner"), Mapping)
                    else ""
                ),
            )
            for item in self.decisive_evidence
            if isinstance(item, Mapping)
        }
        for ref in refs:
            key = (str(ref), owner.occurrence_identity)
            if key in existing:
                continue
            existing.add(key)
            self.decisive_evidence.append(
                {"ref": str(ref), "owner": owner.to_dict()}
            )

    def record_confirmation(
        self, confirmation: RootConfirmation, owner: LocalStateOwner
    ) -> None:
        if owner.seed_binding_identity != self.key:
            raise ValueError("confirmation owner does not match seed")
        validate_root_confirmation_substantive_invariants(confirmation)
        self.candidate_refs.add(confirmation.candidate_ref)
        self.confirmation_identities.add(confirmation.confirmation_identity)
        self.decisive_evidence_refs.update(confirmation.evidence_refs)
        self._record_decisive_evidence(confirmation.evidence_refs, owner)
        if confirmation.status == "confirmed":
            self.confirmed_root_confirmation_identities.add(
                confirmation.confirmation_identity
            )
            self.confirmed_root_refs.add(confirmation.candidate_ref)
            return
        if not is_definitive_confirmation(confirmation):
            self.mark_unresolved(
                (
                    "root_confirmation_unknown"
                    if confirmation.status == "unknown"
                    else "root_confirmation_unresolved"
                ),
                confirmation.reason,
            )

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
            decisive_evidence=tuple(self.decisive_evidence),
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
            decisive_evidence=copy.deepcopy(result.to_dict()["decisive_evidence"]),
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
    step_action_projection: List[JsonDict] = field(default_factory=list)
    introduction_candidates: List[CausalCandidate] = field(default_factory=list)
    introduction_bindings: List[JsonDict] = field(default_factory=list)
    introduction_binding_keys: Set[Tuple[str, str, str, str]] = field(default_factory=set)
    contributing_conditions: List[CausalFactor] = field(default_factory=list)
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
    taint_paths: List[Tuple[str, ...]] = field(default_factory=list)
    visited_order: List[str] = field(default_factory=list)
    visited_entries: List[JsonDict] = field(default_factory=list)
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
    confirmation_action_projection: List[JsonDict] = field(default_factory=list)
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

    @staticmethod
    def _confirmation_queue_key(
        value: Mapping[str, Any],
    ) -> Tuple[str, str, str, str]:
        locator = (
            str(value.get("hypothesis_id") or ""),
            str(value.get("candidate_ref") or ""),
            str(value.get("defect_fingerprint") or ""),
            str(value.get("seed_binding_identity") or ""),
        )
        if not all(locator):
            raise ValueError("confirmation queue locator is incomplete")
        return locator

    def _confirmation_artifact_envelopes(
        self,
        value: Mapping[str, Any],
    ) -> List[JsonDict]:
        candidate_ref = str(value.get("candidate_ref") or "")
        path = tuple(
            str(ref) for ref in value.get("recursive_path") or ()
        )
        path_refs = set(path)
        output: List[JsonDict] = []
        candidate_artifacts = self.graph.artifact_hydration_manifest(
            candidate_ref
        ).get("referenced_artifact_ids") or ()
        for ref in _dedupe_strings(
            [
                *(value.get("checked_evidence_refs") or ()),
                *(
                    "artifact:{0}".format(artifact_id)
                    for artifact_id in candidate_artifacts
                ),
            ]
        ):
            if self.graph.resolve(ref) in self.graph.nodes:
                if not self.graph.active_revision_evidence_eligible(ref):
                    raise ValueError(
                        "confirmation evidence is ineligible for the active "
                        "revision: {0}".format(ref)
                    )
                continue
            if self.graph.artifact_reference_status(ref) is None:
                raise ValueError(
                    "confirmation artifact evidence is unresolved: {0}".format(
                        ref
                    )
                )
            active_owners = self.graph.artifact_active_owner_refs(ref)
            path_owners = [
                owner for owner in active_owners if owner in path_refs
            ]
            if candidate_ref in path_owners:
                expected_owner_ref = candidate_ref
            elif len(path_owners) == 1:
                expected_owner_ref = path_owners[0]
            elif not path_owners:
                raise ValueError(
                    "confirmation artifact evidence has no active owner on "
                    "the candidate path: {0}".format(ref)
                )
            else:
                raise ValueError(
                    "confirmation artifact evidence has ambiguous owners on "
                    "the candidate path: {0}".format(ref)
                )
            output.append(
                self.graph.artifact_evidence_envelope(
                    ref,
                    fact_kind="supporting_evidence",
                    expected_owner_ref=expected_owner_ref,
                )
            )
        return output

    def enqueue_confirmation(self, value: Mapping[str, Any]) -> bool:
        self.validate_confirmation_queue_bound()
        candidate_ref = str(value.get("candidate_ref") or "")
        resolved = self.graph.resolve(candidate_ref)
        node = self.graph.nodes.get(resolved or "")
        if candidate_ref and (
            resolved != candidate_ref
            or node is None
            or not authored_root_candidate_eligible(self.graph, candidate_ref)
        ):
            return False
        actual_keys = {str(key) for key in value}
        missing = (
            PENDING_CONFIRMATION_REQUIRED_KEYS
            - {
                "semantic_identity",
                "artifact_evidence_envelopes",
                "factual_request_projection",
            }
            - actual_keys
        )
        extra = actual_keys - PENDING_CONFIRMATION_ALLOWED_KEYS
        if missing or extra or value.get("status") != "queued":
            raise ValueError(
                "pending confirmation input schema mismatch "
                "(missing={0}, extra={1})".format(
                    sorted(missing),
                    sorted(extra),
                )
            )
        hypothesis_id = str(value.get("hypothesis_id") or "")
        defect_fingerprint = str(value.get("defect_fingerprint") or "")
        seed_binding_identity = str(value.get("seed_binding_identity") or "")
        if not all(
            (candidate_ref, hypothesis_id, defect_fingerprint, seed_binding_identity)
        ):
            raise ValueError("confirmation queue entry requires exact semantic identity")
        if (
            resolved != candidate_ref
            or node is None
            or not authored_root_candidate_eligible(self.graph, candidate_ref)
        ):
            return False
        try:
            owner = LocalStateOwner.from_dict(value.get("owner"))
        except (TypeError, ValueError):
            return False
        if (
            owner.seed_binding_identity != seed_binding_identity
            or owner.hypothesis_id != hypothesis_id
        ):
            raise ValueError("confirmation queue owner contradicts semantic identity")
        recursive_path = tuple(
            str(ref) for ref in value.get("recursive_path") or ()
        )
        checked_evidence_refs = tuple(
            str(ref) for ref in value.get("checked_evidence_refs") or ()
        )
        if recursive_path and (
            recursive_path[0] != candidate_ref
            or any(
                not self.graph.active_revision_evidence_eligible(ref)
                for ref in recursive_path
            )
        ):
            return False
        try:
            self._confirmation_artifact_envelopes(value)
        except ValueError:
            return False
        queue_key = self._confirmation_queue_key(value)
        if queue_key in self.confirmation_queue_keys:
            return False
        semantic_identity = str(value.get("semantic_identity") or "")
        if any(
            str(item.get("semantic_identity") or "") == semantic_identity
            for item in self.confirmation_queue
        ):
            raise ValueError(
                "confirmation queue contains a duplicate semantic identity"
            )
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
        queued = copy.deepcopy(dict(value))
        queued["artifact_evidence_envelopes"] = (
            self._confirmation_artifact_envelopes(queued)
        )
        provided_identity = str(queued.get("semantic_identity") or "")
        original_queue = copy.deepcopy(self.confirmation_queue)
        original_keys = set(self.confirmation_queue_keys)
        self.confirmation_queue.append(queued)
        try:
            for item in self.confirmation_queue:
                if item.get("status") != "queued":
                    continue
                request = AgenticRecursiveAnalyzer._build_confirmation_request(
                    self, item
                )
                projection = root_confirmation_request_projection(request)
                expected_identity = _confirmation_request_identity(request)
                if item is queued and provided_identity and (
                    provided_identity != expected_identity
                ):
                    raise ValueError(
                        "pending confirmation semantic identity contradicts "
                        "the canonical factual request"
                    )
                item["factual_request_projection"] = projection
                item["semantic_identity"] = expected_identity
            self.confirmation_queue_keys = {
                self._confirmation_queue_key(item)
                for item in self.confirmation_queue
            }
            self.validate_confirmation_queue_bound()
        except Exception:
            self.confirmation_queue = original_queue
            self.confirmation_queue_keys = original_keys
            raise
        return True

    def refresh_pending_confirmation_request_identities(self) -> None:
        for item in self.confirmation_queue:
            if item.get("status") != "queued":
                continue
            request = AgenticRecursiveAnalyzer._build_confirmation_request(
                self, item
            )
            item["factual_request_projection"] = (
                root_confirmation_request_projection(request)
            )
            item["semantic_identity"] = _confirmation_request_identity(request)

    def validate_confirmation_queue_bound(self) -> None:
        candidates_by_seed: Dict[str, Set[str]] = {}
        canonical_keys: List[Tuple[str, str, str, str]] = []
        semantic_identities: Set[str] = set()
        for item in self.confirmation_queue:
            seed_binding_identity = str(
                item.get("seed_binding_identity") or ""
            )
            candidate_ref = str(item.get("candidate_ref") or "")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if not seed_binding_identity or not candidate_ref:
                raise ValueError("restored confirmation queue identity is incomplete")
            if (
                owner.seed_binding_identity != seed_binding_identity
                or owner.hypothesis_id
                != str(item.get("hypothesis_id") or "")
            ):
                raise ValueError("restored confirmation queue owner is inconsistent")
            candidate = self.graph.nodes.get(candidate_ref)
            if (
                candidate is None
                or not authored_root_candidate_eligible(
                    self.graph, candidate_ref
                )
            ):
                raise ValueError(
                    "restored confirmation queue contains a root candidate ineligible for the active revision"
                )
            is_pending = item.get("status") == "queued"
            terminal_confirmation = None
            if is_pending:
                _validate_pending_confirmation_identity(item)
                terminal_disposition = None
            else:
                _validate_terminal_confirmation_identity(item)
                terminal_confirmation = RootConfirmation.from_dict(
                    dict(item.get("confirmation") or {})
                )
                raw_disposition = item.get("evidence_disposition")
                raw_disposition_state = (
                    str(raw_disposition.get("state") or "")
                    if isinstance(raw_disposition, Mapping)
                    else ""
                )
                terminal_disposition = (
                    _validated_terminal_evidence_disposition(
                        raw_disposition,
                        artifact_evidence_envelopes=item.get(
                            "artifact_evidence_envelopes"
                        ),
                        confirmation=terminal_confirmation,
                        operation=(
                            "confirmation_failed"
                            if raw_disposition_state
                            == "rejected_snapshot"
                            else ""
                        ),
                    )
                )
            recursive_path = tuple(
                str(ref) for ref in item.get("recursive_path") or ()
            )
            checked_evidence_refs = tuple(
                str(ref) for ref in item.get("checked_evidence_refs") or ()
            )
            if recursive_path and (
                recursive_path[0] != candidate_ref
                or any(
                    not self.graph.active_revision_evidence_eligible(ref)
                    for ref in recursive_path
                )
            ):
                raise ValueError(
                    "restored confirmation queue path contains evidence ineligible for the active revision"
                )
            if any(
                self.graph.resolve(ref) in self.graph.nodes
                and not self.graph.active_revision_evidence_eligible(ref)
                for ref in checked_evidence_refs
            ):
                raise ValueError(
                    "restored confirmation queue contains evidence ineligible for the active revision"
                )
            actual_artifacts = item.get("artifact_evidence_envelopes")
            if not isinstance(actual_artifacts, list):
                raise ValueError(
                    "confirmation queue artifact owner envelope must be an array"
                )
            rejected_snapshot = bool(
                terminal_disposition is not None
                and terminal_disposition["state"]
                == "rejected_snapshot"
            )
            defect_state = self.defect_states.get(
                str(item.get("defect_fingerprint") or "")
            )
            if defect_state is None:
                raise ValueError(
                    "confirmation queue projection binding has no canonical "
                    "defect state"
                )
            _validate_confirmation_request_projection_binding(
                item,
                defect_state=defect_state,
                analysis_perspective=self.analysis_perspective,
                ledger=self.ledger,
                frontier=self.frontier,
                confirmation=terminal_confirmation,
                label="confirmation queue",
            )
            if not rejected_snapshot:
                expected_artifacts = self._confirmation_artifact_envelopes(
                    item
                )
                if stable_json(
                    _checkpoint_json(actual_artifacts)
                ) != stable_json(_checkpoint_json(expected_artifacts)):
                    raise ValueError(
                        "confirmation queue artifact owner envelope "
                        "contradicts the active graph"
                    )
            stored_projection = item.get("factual_request_projection")
            if not isinstance(stored_projection, Mapping):
                raise ValueError(
                    "confirmation queue factual request projection is missing"
                )
            semantic_identity = str(item.get("semantic_identity") or "")
            if rejected_snapshot:
                expected_semantic_identity = (
                    root_confirmation_request_projection_identity(
                        stored_projection
                    )
                )
                if semantic_identity != expected_semantic_identity:
                    raise ValueError(
                        "rejected confirmation snapshot factual projection "
                        "contradicts its semantic identity"
                    )
            else:
                validate_root_confirmation_request_projection(
                    stored_projection
                )
                current_request = (
                    AgenticRecursiveAnalyzer._build_confirmation_request(
                        self, item
                    )
                )
                expected_projection = root_confirmation_request_projection(
                    current_request
                )
                if stable_json(
                    _checkpoint_json(stored_projection)
                ) != stable_json(_checkpoint_json(expected_projection)):
                    raise ValueError(
                        "confirmation queue factual request projection changed"
                    )
                expected_semantic_identity = (
                    _confirmation_request_identity(current_request)
                )
                if semantic_identity != expected_semantic_identity:
                    raise ValueError(
                        "confirmation queue contains a non-canonical semantic "
                        "identity"
                    )
                if terminal_confirmation is not None:
                    rebound_confirmation = bind_root_confirmation(
                        terminal_confirmation,
                        request=current_request,
                    )
                    if rebound_confirmation != terminal_confirmation:
                        raise ValueError(
                            "confirmation queue response does not exactly "
                            "rebind to its current factual request"
                        )
            if not semantic_identity or semantic_identity in semantic_identities:
                raise ValueError(
                    "confirmation queue contains a missing, duplicate, or "
                    "non-canonical semantic identity"
                )
            semantic_identities.add(semantic_identity)
            canonical_keys.append(self._confirmation_queue_key(item))
            candidates = candidates_by_seed.setdefault(seed_binding_identity, set())
            if candidate_ref in candidates:
                raise ValueError(
                    "confirmation queue contains a duplicate candidate for "
                    "one seed"
                )
            candidates.add(candidate_ref)
            if len(candidates) > MAX_ROOT_CONFIRMATION_CANDIDATES:
                raise ValueError(
                    "restored confirmation queue exceeds three candidates per seed"
                )
        if (
            len(canonical_keys) != len(set(canonical_keys))
            or set(canonical_keys) != self.confirmation_queue_keys
        ):
            raise ValueError(
                "confirmation queue entries and persisted key set must "
                "bijectively match"
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

    def _frontier_items_by_visit(self) -> Dict[str, FrontierItem]:
        return {
            item.visit_key: item for item in self.frontier.lifecycle_items()
        }

    def _owner_item(
        self,
        owner: LocalStateOwner,
        *,
        label: str,
        occurrence_key: str,
    ) -> FrontierItem:
        item = self._frontier_items_by_visit().get(owner.visit_key)
        if item is None:
            raise ValueError("{0} owner has no frontier visit".format(label))
        expected = _owner_for_item(item, occurrence_key)
        if owner != expected:
            raise ValueError(
                "{0} owner contradicts its exact frontier occurrence".format(
                    label
                )
            )
        try:
            hypothesis = self.ledger.get(owner.hypothesis_id)
        except KeyError:
            raise ValueError("{0} owner has no ledger hypothesis".format(label))
        if (
            hypothesis.hypothesis_id != item.hypothesis_id
            or hypothesis.semantic_hash != item.hypothesis_semantic_hash
            or hypothesis.seed_binding_identity != owner.seed_binding_identity
            or item.seed_binding_identity != owner.seed_binding_identity
            or self.hypothesis_seed_keys.get(owner.hypothesis_id)
            != owner.seed_binding_identity
            or owner.seed_binding_identity not in self.seed_ledger
        ):
            raise ValueError(
                "{0} owner contradicts ledger or seed routing".format(label)
            )
        return item

    def _validate_step_judgment_owner(
        self,
        judgment: CausalStepJudgment,
        *,
        label: str,
    ) -> FrontierItem:
        if judgment.owner is None:
            raise ValueError("{0} is ownerless".format(label))
        semantic_keys = [
            str(projection.get("semantic_key") or "")
            for projection in self.step_action_projection
            if isinstance(projection, Mapping)
            and projection.get("owner") == judgment.owner.to_dict()
        ]
        if len(semantic_keys) != 1 or not semantic_keys[0]:
            raise ValueError(
                "{0} has no unique completed Provider action owner".format(
                    label
                )
            )
        semantic_key = semantic_keys[0]
        item = self._owner_item(
            judgment.owner,
            label=label,
            occurrence_key="step_judgment:{0}".format(semantic_key),
        )
        if item.node_ref != judgment.current_node_ref:
            raise ValueError(
                "{0} owner contradicts the judged node".format(label)
            )
        for index, assessment in enumerate(judgment.predecessors):
            if assessment.owner is None:
                raise ValueError("{0} predecessor is ownerless".format(label))
            owned_item = self._owner_item(
                assessment.owner,
                label="{0} predecessor".format(label),
                occurrence_key="step_predecessor:{0}:{1}:{2}".format(
                    semantic_key, index, assessment.ref
                ),
            )
            if owned_item != item:
                raise ValueError(
                    "{0} predecessor owner contradicts its judgment".format(
                        label
                    )
                )
        return item

    def _validate_confirmation_action_owner(
        self,
        entry: Mapping[str, Any],
        *,
        label: str,
    ) -> LocalStateOwner:
        owner = LocalStateOwner.from_dict(entry.get("owner"))
        item = self._frontier_items_by_visit().get(owner.visit_key)
        if item is not None:
            self._owner_item(
                owner,
                label=label,
                occurrence_key="confirmation_queue",
            )
            if (
                str(entry.get("hypothesis_id") or "") != item.hypothesis_id
                or str(entry.get("seed_binding_identity") or "")
                != item.seed_binding_identity
            ):
                raise ValueError(
                    "{0} owner contradicts frontier action identity".format(
                        label
                    )
                )
            return owner

        hypothesis_id = str(entry.get("hypothesis_id") or "")
        seed_binding_identity = str(
            entry.get("seed_binding_identity") or ""
        )
        candidate_ref = str(entry.get("candidate_ref") or "")
        defect_fingerprint = str(entry.get("defect_fingerprint") or "")
        try:
            hypothesis = self.ledger.get(hypothesis_id)
        except KeyError:
            raise ValueError(
                "{0} owner has no ledger hypothesis".format(label)
            )
        defect_state = self.defect_states.get(defect_fingerprint)
        if (
            defect_state is None
            or hypothesis.candidate_root_ref != candidate_ref
            or hypothesis.active_defect_fingerprint != defect_fingerprint
            or hypothesis.seed_binding_identity != seed_binding_identity
            or owner.hypothesis_id != hypothesis_id
            or owner.seed_binding_identity != seed_binding_identity
            or self.hypothesis_seed_keys.get(hypothesis_id)
            != seed_binding_identity
            or seed_binding_identity not in self.seed_ledger
        ):
            raise ValueError(
                "{0} owner contradicts ledger or action entry".format(label)
            )
        visit_key = semantic_visit_key(
            candidate_ref,
            defect_state,
            hypothesis.semantic_hash,
            seed_binding_identity,
        )
        expected = LocalStateOwner.create(
            seed_binding_identity=seed_binding_identity,
            hypothesis_id=hypothesis_id,
            visit_key=visit_key,
            occurrence_key="confirmation_queue",
        )
        if owner != expected:
            raise ValueError(
                "{0} owner contradicts exact action occurrence".format(label)
            )
        return owner

    def _owned_downstream_judgments(
        self,
        item: FrontierItem,
    ) -> List[CausalStepJudgment]:
        output = []
        for judgment in self.step_judgments:
            try:
                owned_item = self._validate_step_judgment_owner(
                    judgment,
                    label="downstream judgment",
                )
            except ValueError:
                continue
            if (
                owned_item.seed_binding_identity == item.seed_binding_identity
                and judgment.current_node_ref in item.downstream_path
            ):
                output.append(judgment)
        return output

    def _owned_investigation_evidence(
        self,
        item: FrontierItem,
    ) -> List[JsonDict]:
        journal_results = []
        for entry in self.investigation_journal:
            if not isinstance(entry, Mapping):
                continue
            active_visit = entry.get("active_visit")
            result = entry.get("result")
            if (
                isinstance(active_visit, Mapping)
                and isinstance(result, Mapping)
                and str(active_visit.get("visit_key") or "") == item.visit_key
                and str(active_visit.get("hypothesis_id") or "")
                == item.hypothesis_id
                and str(active_visit.get("node_ref") or "") == item.node_ref
            ):
                journal_results.append(dict(result))

        output = []
        for value in self.investigation_evidence.get(item.visit_key, []):
            if not isinstance(value, Mapping):
                continue
            evidence = copy.deepcopy(dict(value))
            evidence_hash = str(evidence.get("evidence_hash") or "")
            raw_owner = evidence.pop("owner", None)
            if raw_owner is not None:
                try:
                    owner = LocalStateOwner.from_dict(raw_owner)
                    self._owner_item(
                        owner,
                        label="investigation evidence",
                        occurrence_key="investigation_evidence:{0}".format(
                            evidence_hash
                        ),
                    )
                except ValueError:
                    continue
                if owner.visit_key != item.visit_key:
                    continue
            elif not any(
                stable_json(evidence) == stable_json(result)
                for result in journal_results
            ):
                continue
            evidence["owner"] = _owner_for_item(
                item,
                "investigation_evidence:{0}".format(evidence_hash),
            ).to_dict()
            output.append(evidence)
        return output

    def _validate_action_payload_reconciliation(
        self,
        action_records: Optional[Iterable[Any]] = None,
    ) -> None:
        lifecycle_records = (
            _validated_global_judge_action_history(tuple(action_records))
            if action_records is not None
            else {}
        )
        seed_authority = _seed_authority_from_records(
            builder.to_dict() for builder in self.seed_ledger.values()
        )
        global_failure_episodes = _classify_global_failure_episodes(
            self.unresolved_branches,
            seed_authority=seed_authority,
        )
        passes_by_owner = _global_passes_by_owner(
            self.investigation_journal,
            seed_authority=seed_authority,
        )
        matched_pass_owners = set()
        expected_decisive_evidence: Dict[str, Dict[str, JsonDict]] = {
            key: {} for key in self.seed_ledger
        }
        expected_candidate_refs: Dict[str, Set[str]] = {
            key: set() for key in self.seed_ledger
        }
        for binding in self.introduction_bindings:
            if not isinstance(binding, Mapping):
                continue
            seed_key = str(
                binding.get("seed_key")
                or binding.get("seed_binding_identity")
                or ""
            )
            candidate_ref = str(binding.get("candidate_ref") or "")
            if seed_key in expected_candidate_refs and candidate_ref:
                expected_candidate_refs[seed_key].add(candidate_ref)
        for builder in self.seed_ledger.values():
            judgment = builder.global_judgment
            expected_expansion = []
            if judgment:
                owner = LocalStateOwner.from_dict(judgment.get("owner"))
                pass_identity = _global_pass_identity(builder.key)
                action = passes_by_owner.get(pass_identity)
                if action is None or action.get("status") != "completed":
                    raise ValueError(
                        "seed global judgment has no completed pass action"
                    )
                if owner != _global_pass_owner(builder):
                    raise ValueError(
                        "seed global judgment owner contradicts canonical pass"
                    )
                matched_pass_owners.add(pass_identity)
                persisted_judgment = {
                    key: copy.deepcopy(value)
                    for key, value in judgment.items()
                    if key
                    not in {
                        "schema_version",
                        "validation_envelope",
                        "owner",
                    }
                }
                if stable_json(_checkpoint_json(persisted_judgment)) != stable_json(
                    _checkpoint_json(action.get("judgment"))
                ):
                    raise ValueError(
                        "seed global judgment payload contradicts pass action"
                    )
                capsules = action.get("candidate_evidence_capsules")
                compression = action.get("candidate_compression")
                envelope = judgment.get("validation_envelope")
                if (
                    not isinstance(capsules, (list, tuple))
                    or not isinstance(compression, Mapping)
                    or not isinstance(envelope, Mapping)
                ):
                    raise ValueError(
                        "completed global pass action payload is incomplete"
                    )
                expected_envelope = {
                    "schema_version": envelope.get("schema_version"),
                    "case_id": self.graph.case_id,
                    "objective": self.objective,
                    "analysis_perspective": self.analysis_perspective,
                    "seed_ref": builder.start_ref,
                    "active_defect": builder.defect_state.to_dict(),
                    "active_focus_text": builder.defect_state.actual,
                    "active_focus_text_hash": active_focus_text_sha256(
                        builder.defect_state.actual
                    ),
                    "start_refs": [builder.start_ref],
                    "trace_health": {
                        "missing_artifact_count": sum(
                            len(capsule.get("missing_evidence_refs") or ())
                            for capsule in capsules
                            if isinstance(capsule, Mapping)
                        ),
                        "candidate_compression": copy.deepcopy(
                            dict(compression)
                        ),
                    },
                    "candidate_evidence_capsules": copy.deepcopy(
                        list(capsules)
                    ),
                }
                if stable_json(_checkpoint_json(envelope)) != stable_json(
                    _checkpoint_json(expected_envelope)
                ):
                    raise ValueError(
                        "seed global judgment validation envelope "
                        "contradicts pass action"
                    )
                expected_expansion = [
                    _owned_payload(item, owner.to_dict())
                    for item in (
                        action.get("judgment", {}).get(
                            "expansion_requests"
                        )
                        or ()
                    )
                    if isinstance(item, Mapping)
                ]
                expected_candidate_refs[builder.key].update(
                    str(capsule.get("candidate_ref") or "")
                    for capsule in capsules
                    if isinstance(capsule, Mapping)
                    and str(capsule.get("candidate_ref") or "")
                )
                for ref in (
                    action.get("judgment", {}).get(
                        "decisive_evidence_refs"
                    )
                    or ()
                ):
                    payload = {
                        "ref": str(ref),
                        "owner": owner.to_dict(),
                    }
                    expected_decisive_evidence[builder.key][
                        stable_json(payload)
                    ] = payload
            _require_canonical_bijection(
                expected_expansion,
                builder.expansion_history,
                label="seed expansion history",
            )

            failure = passes_by_owner.get(
                _global_pass_identity(builder.key)
            )
            if failure is not None and failure.get("status") == "failed":
                projection = _global_failure_projection_from_action(failure)
                episodes = [
                    branch
                    for branch in global_failure_episodes
                    if branch.get("failure_projection") == projection
                ]
                if (
                    len(episodes) != 1
                    or projection["seed_binding_identity"] != builder.key
                    or projection["seed_ref"] != builder.start_ref
                    or projection["defect_fingerprint"]
                    != builder.defect_state.fingerprint
                    or LocalStateOwner.from_dict(projection["owner"])
                    != _global_pass_owner(builder)
                    or episodes[0].get("global_pass_identity")
                    != projection["pass_identity"]
                    or episodes[0].get("reason")
                    != projection["blocker"]
                    or episodes[0].get("details")
                    != projection["detail"]
                    or episodes[0].get("owner") != projection["owner"]
                    or episodes[0].get("node_ref")
                    != projection["seed_ref"]
                    or set(builder.blocking_reasons)
                    != {projection["blocker"]}
                    or set(builder.missing_evidence)
                    != set(projection["missing_evidence"])
                ):
                    raise ValueError(
                        "failed global pass action has no bijective unresolved episode"
                    )
                matched_pass_owners.add(
                    str(failure.get("pass_identity") or "")
                )

        failure_projections = [
            _global_failure_projection_from_action(action)
            for action in passes_by_owner.values()
            if action.get("status") == "failed"
        ]
        episode_projections = [
            copy.deepcopy(branch.get("failure_projection"))
            for branch in global_failure_episodes
        ]
        _require_canonical_bijection(
            failure_projections,
            episode_projections,
            label="global failure episodes",
        )
        if matched_pass_owners != set(passes_by_owner):
            raise ValueError(
                "global pass action has no unique seed judgment or failure episode"
            )
        terminal_lifecycle_by_pass: Dict[str, JsonDict] = {}

        def same_checkpoint_value(left: Any, right: Any) -> bool:
            return stable_json(_checkpoint_json(left)) == stable_json(
                _checkpoint_json(right)
            )

        for records in lifecycle_records.values():
            if len(records) != 2:
                continue
            terminal_record = records[-1]
            terminal = terminal_record["payload"]
            pass_identity = str(terminal.get("pass_identity") or "")
            if pass_identity in terminal_lifecycle_by_pass:
                raise ValueError(
                    "global Judge pass has duplicate terminal lifecycle actions"
                )
            _validate_provider_state(
                terminal.get("provider_state"),
                self,
                cache_identity=str(
                    terminal.get("provider_state", {}).get(
                        "cache_identity"
                    )
                    or ""
                ),
                require_accounting_match=False,
            )
            terminal_lifecycle_by_pass[pass_identity] = terminal_record
            applied = passes_by_owner.get(pass_identity)
            if applied is None:
                continue
            if (
                terminal.get("seed_binding_identity")
                != applied.get("seed_binding_identity")
                or terminal.get("seed_ref") != applied.get("seed_ref")
                or terminal.get("defect_fingerprint")
                != applied.get("defect_fingerprint")
                or terminal.get("hypothesis_id")
                != applied.get("hypothesis_id")
                or terminal.get("visit_key") != applied.get("visit_key")
                or not same_checkpoint_value(
                    terminal.get("owner"), applied.get("owner")
                )
                or not same_checkpoint_value(
                    terminal.get("candidate_compression"),
                    applied.get("candidate_compression"),
                )
                or terminal.get("physical_request_delta")
                != applied.get("physical_request_delta")
            ):
                raise ValueError(
                    "global Judge terminal action contradicts the applied pass"
                )
            if terminal_record["operation"] == "global_judge_completed":
                completed_mismatches = []
                if applied.get("status") != "completed":
                    completed_mismatches.append("status")
                if not same_checkpoint_value(
                    terminal.get("judgment"),
                    applied.get("judgment"),
                ):
                    completed_mismatches.append("judgment")
                if not same_checkpoint_value(
                    terminal.get("validation_envelope", {}).get(
                        "candidate_evidence_capsules"
                    ),
                    applied.get("candidate_evidence_capsules"),
                ):
                    completed_mismatches.append("candidate_evidence_capsules")
                if completed_mismatches:
                    raise ValueError(
                        "completed global Judge action contradicts applied "
                        "judgment fields: {0}".format(
                            ", ".join(completed_mismatches)
                        )
                    )
                builder = self.seed_ledger.get(
                    str(terminal.get("seed_binding_identity") or "")
                )
                if (
                    builder is None
                    or not same_checkpoint_value(
                        builder.global_judgment.get(
                            "validation_envelope"
                        ),
                        terminal.get("validation_envelope"),
                    )
                ):
                    raise ValueError(
                        "completed global Judge action contradicts seed judgment"
                    )
            elif (
                applied.get("status") != "failed"
                or not same_checkpoint_value(
                    terminal.get("failure_projection"),
                    applied.get("failure_projection"),
                )
            ):
                raise ValueError(
                    "failed global Judge action contradicts applied failure"
                )
        if action_records is not None:
            for pass_identity, applied in passes_by_owner.items():
                requires_lifecycle = (
                    applied.get("status") == "completed"
                    or applied.get("blocker")
                    in {
                        "global_judge_bounded_failure",
                        "global_judge_output_invalid",
                        "global_judge_interrupted",
                    }
                )
                if (
                    requires_lifecycle
                    and pass_identity not in terminal_lifecycle_by_pass
                ):
                    raise ValueError(
                        "applied global pass has no terminal Judge lifecycle action"
                    )

        journal_entries = []
        for item in self.confirmation_journal:
            if not isinstance(item, Mapping):
                continue
            if isinstance(item.get("confirmation"), Mapping):
                journal_entries.append(item)
                continue
            if item.get("status") or item.get("candidate_ref"):
                raise ValueError(
                    "confirmation journal action payload is incomplete"
                )
        journal_confirmations = []
        journal_projection = []
        queue_projection_keys = (
            "semantic_identity",
            "candidate_ref",
            "hypothesis_id",
            "defect_fingerprint",
            "seed_binding_identity",
            "seed_key",
            "owner",
            "recursive_path",
            "status",
            "response_identity",
            "artifact_evidence_envelopes",
            "evidence_disposition",
            "factual_request_projection",
            "confirmation",
        )
        for entry in journal_entries:
            confirmation = entry.get("confirmation")
            owner = LocalStateOwner.from_dict(entry.get("owner"))
            parsed = RootConfirmation.from_dict(dict(confirmation))
            canonical_projection = _confirmation_action_projection(
                operation=str(entry.get("action_operation") or ""),
                semantic_key=str(entry.get("semantic_key") or ""),
                request_identity=str(
                    entry.get("semantic_identity") or ""
                ),
                owner=entry.get("owner"),
                seed_key=str(entry.get("seed_key") or ""),
                confirmation=parsed,
                physical_requests_reserved=entry.get(
                    "physical_requests_reserved"
                ),
                physical_request_delta=entry.get(
                    "physical_request_delta"
                ),
                physical_request_exact=entry.get(
                    "physical_request_exact"
                ),
                factual_request_projection=entry.get(
                    "factual_request_projection"
                ),
                artifact_evidence_envelopes=entry.get(
                    "artifact_evidence_envelopes"
                )
                or (),
                evidence_disposition=entry.get(
                    "evidence_disposition"
                ),
            )
            disposition = _validated_terminal_evidence_disposition(
                canonical_projection["evidence_disposition"],
                artifact_evidence_envelopes=canonical_projection[
                    "artifact_evidence_envelopes"
                ],
                confirmation=parsed,
                operation=canonical_projection["operation"],
            )
            if disposition["state"] == "validated":
                _validate_terminal_confirmation_evidence(
                    self.graph,
                    confirmation=parsed,
                    artifact_evidence_envelopes=canonical_projection[
                        "artifact_evidence_envelopes"
                    ],
                    label="restored confirmation action",
                )
            if (
                str(entry.get("candidate_ref") or "")
                != parsed.candidate_ref
                or str(entry.get("hypothesis_id") or "")
                != parsed.hypothesis_id
                or str(entry.get("defect_fingerprint") or "")
                != parsed.defect_fingerprint
                or str(entry.get("seed_binding_identity") or "")
                != parsed.seed_binding_identity
                or str(entry.get("seed_key") or "")
                != parsed.seed_binding_identity
                or str(entry.get("status") or "") != parsed.status
                or str(entry.get("response_identity") or "")
                != parsed.response_identity
                or str(entry.get("response_identity") or "")
                != canonical_projection["response_identity"]
                or tuple(entry.get("recursive_path") or ())
                != parsed.recursive_path
                or owner.seed_binding_identity
                != parsed.seed_binding_identity
                or owner.hypothesis_id != parsed.hypothesis_id
                or entry.get("evidence_disposition")
                != canonical_projection["evidence_disposition"]
                or entry.get("factual_request_projection")
                != canonical_projection["factual_request_projection"]
            ):
                raise ValueError(
                    "confirmation journal fields contradict action payload"
                )
            journal_confirmations.append(copy.deepcopy(dict(confirmation)))
            journal_projection.append(canonical_projection)
            expected = expected_decisive_evidence.get(
                parsed.seed_binding_identity
            )
            if expected is None:
                raise ValueError(
                    "confirmation action has no seed ledger owner"
                )
            expected_candidate_refs[
                parsed.seed_binding_identity
            ].add(parsed.candidate_ref)
            for ref in parsed.evidence_refs:
                payload = {
                    "ref": ref,
                    "owner": owner.to_dict(),
                }
                expected[stable_json(payload)] = payload

        _require_canonical_bijection(
            (item.to_dict() for item in self.confirmations),
            journal_confirmations,
            label="confirmation journal payloads",
        )
        _require_canonical_bijection(
            journal_projection,
            (
                _validated_confirmation_action_projection(item)
                for item in self.confirmation_action_projection
            ),
            label="confirmation journal action projection",
        )
        _require_canonical_bijection(
            (
                {
                    key: copy.deepcopy(entry.get(key))
                    for key in queue_projection_keys
                }
                for entry in journal_entries
            ),
            (
                {
                    key: copy.deepcopy(entry.get(key))
                    for key in queue_projection_keys
                }
                for entry in self.confirmation_queue
                if isinstance(entry, Mapping)
                and isinstance(entry.get("confirmation"), Mapping)
            ),
            label="confirmation queue actions",
        )
        if action_records is not None:
            action_projection = []
            for item in action_records:
                if (
                    not isinstance(item, Mapping)
                    or str(item.get("operation") or "")
                    not in CONFIRMATION_ACTION_OPERATIONS
                ):
                    continue
                projection = _confirmation_action_projection_from_record(item)
                seed_key = projection["seed_binding_identity"]
                builder = self.seed_ledger.get(seed_key)
                if builder is None:
                    raise ValueError(
                        "confirmation action has no seed ledger owner"
                    )
                if (
                    "start_ref_active_revision_ineligible"
                    in builder.blocking_reasons
                ):
                    continue
                action_projection.append(projection)
            _require_canonical_bijection(
                journal_projection,
                action_projection,
                label="completed confirmation actions",
            )
        for builder in self.seed_ledger.values():
            if expected_candidate_refs[builder.key] != builder.candidate_refs:
                raise ValueError(
                    "seed candidate refs contradict action payloads"
                )
            expected = tuple(
                expected_decisive_evidence[builder.key].values()
            )
            _require_canonical_bijection(
                expected,
                builder.decisive_evidence,
                label="seed decisive evidence",
            )
            if {item["ref"] for item in expected} != set(
                builder.decisive_evidence_refs
            ):
                raise ValueError(
                    "seed decisive evidence refs contradict action payloads"
                )

    def _validate_local_state_owners(
        self,
        action_records: Optional[Iterable[Any]] = None,
    ) -> None:
        if action_records is not None:
            action_records = tuple(action_records)
        items_by_visit = self._frontier_items_by_visit()
        for visit_key in (
            set(self.visit_evidence)
            | set(self.investigation_evidence)
            | set(self.investigation_evidence_hashes)
            | set(self.pending_rejudge_journal)
        ):
            if visit_key not in items_by_visit:
                raise ValueError(
                    "local visit state owner has no frontier action entry"
                )

        judgment_occurrences = set()
        predecessor_occurrences = set()
        predecessors_by_occurrence = {}
        expected_nested_relations = []
        for index, judgment in enumerate(self.step_judgments):
            item = self._validate_step_judgment_owner(
                judgment,
                label="step judgment[{0}]".format(index),
            )
            if judgment.owner.occurrence_identity in judgment_occurrences:
                raise ValueError(
                    "step judgments contain a duplicate local occurrence"
                )
            judgment_occurrences.add(judgment.owner.occurrence_identity)
            for predecessor in judgment.predecessors:
                if predecessor.owner is None:
                    raise ValueError(
                        "step judgment predecessor is ownerless"
                    )
                occurrence = predecessor.owner.occurrence_identity
                if occurrence in predecessor_occurrences:
                    raise ValueError(
                        "step judgment predecessors contain a duplicate local occurrence"
                    )
                predecessor_occurrences.add(occurrence)
                predecessors_by_occurrence[occurrence] = predecessor
                if not (
                    judgment.current_defect_status == "absent"
                    and predecessor.relation == "unknown"
                ):
                    expected_nested_relations.append(predecessor.to_dict())

        relation_occurrences = set()
        actual_nested_relations = []
        for index, assessment in enumerate(self.causal_relations):
            owner = assessment.owner
            if owner is None:
                raise ValueError(
                    "causal relation[{0}] is ownerless".format(index)
                )
            if owner.occurrence_identity in relation_occurrences:
                raise ValueError(
                    "causal relations contain a duplicate local occurrence"
                )
            relation_occurrences.add(owner.occurrence_identity)
            source_predecessor = predecessors_by_occurrence.get(
                owner.occurrence_identity
            )
            if source_predecessor is not None:
                if source_predecessor != assessment:
                    raise ValueError(
                        "causal relation[{0}] owner contradicts step judgment".format(
                            index
                        )
                    )
                actual_nested_relations.append(assessment.to_dict())
                continue
            builder = self.seed_ledger.get(owner.seed_binding_identity)
            if builder is None:
                raise ValueError(
                    "causal relation[{0}] owner has no seed".format(index)
                )
            expected = _owner_for_seed_projection(
                seed_binding_identity=builder.key,
                node_ref=assessment.ref,
                defect_state=builder.defect_state,
                occurrence_key="initial_outcome_relation:{0}:{1}".format(
                    builder.start_ref, assessment.ref
                ),
            )
            if owner != expected:
                raise ValueError(
                    "causal relation[{0}] owner contradicts seed projection".format(
                        index
                    )
                )
        _require_canonical_bijection(
            expected_nested_relations,
            actual_nested_relations,
            label="step predecessor causal relation",
        )

        canonical_step_projections = []
        for value in self.step_action_projection:
            if not isinstance(value, Mapping):
                raise ValueError(
                    "step action projection entry must be an object"
                )
            item = items_by_visit.get(str(value.get("visit_key") or ""))
            if item is None:
                raise ValueError(
                    "step action projection has no frontier visit"
                )
            canonical_step_projections.append(
                _validated_step_action_projection(value, item=item)
            )
        _require_canonical_bijection(
            (
                projection["step_judgment"]
                for projection in canonical_step_projections
            ),
            (judgment.to_dict() for judgment in self.step_judgments),
            label="step judgments from completed Provider actions",
        )
        _require_canonical_bijection(
            (
                relation
                for projection in canonical_step_projections
                for relation in projection["causal_relations"]
            ),
            actual_nested_relations,
            label="step causal relations from completed Provider actions",
        )
        if action_records is not None:
            authoritative_step_projections = []
            for record in action_records:
                if (
                    not isinstance(record, Mapping)
                    or record.get("operation")
                    != "provider_call_completed"
                    or not isinstance(record.get("payload"), Mapping)
                    or record["payload"].get("call_kind") != "step"
                ):
                    continue
                visit_key = str(
                    record["payload"].get("visit_key") or ""
                )
                item = items_by_visit.get(visit_key)
                if item is None:
                    raise ValueError(
                        "completed step Provider action has no frontier visit"
                    )
                builder = self.seed_ledger.get(
                    item.seed_binding_identity
                )
                if (
                    builder is not None
                    and "start_ref_active_revision_ineligible"
                    in builder.blocking_reasons
                ):
                    continue
                authoritative_step_projections.append(
                    _step_action_projection_from_record(
                        record,
                        item=item,
                    )
                )
            _require_canonical_bijection(
                authoritative_step_projections,
                canonical_step_projections,
                label="completed step Provider actions",
            )

        for index, entry in enumerate(self.visited_entries):
            owner = LocalStateOwner.from_dict(entry.get("owner"))
            item = self._owner_item(
                owner,
                label="visited entry[{0}]".format(index),
                occurrence_key="visited_node",
            )
            if str(entry.get("node_ref") or "") != item.node_ref:
                raise ValueError(
                    "visited entry[{0}] owner contradicts node".format(index)
                )

        for label, entries in (
            ("confirmation queue", self.confirmation_queue),
            ("confirmation journal", self.confirmation_journal),
        ):
            for index, entry in enumerate(entries):
                self._validate_confirmation_action_owner(
                    entry,
                    label="{0}[{1}]".format(label, index),
                )

        for index, entry in enumerate(self.investigation_journal):
            if not isinstance(entry, Mapping):
                raise ValueError("investigation journal entry must be an object")
            if entry.get("kind") == "global_candidate_pass" and entry.get(
                "seed_ref"
            ):
                owner = LocalStateOwner.from_dict(entry.get("owner"))
                seed_binding_identity = str(
                    entry.get("seed_binding_identity") or ""
                )
                builder = self.seed_ledger.get(seed_binding_identity)
                if (
                    builder is None
                    or owner != _global_pass_owner(builder)
                    or str(entry.get("seed_ref") or "")
                    != builder.start_ref
                    or str(entry.get("defect_fingerprint") or "")
                    != builder.defect_state.fingerprint
                    or str(entry.get("pass_identity") or "")
                    != _global_pass_identity(builder.key)
                ):
                    raise ValueError(
                        "global candidate pass owner contradicts action identity"
                    )
                continue
            active_visit = entry.get("active_visit")
            if not isinstance(active_visit, Mapping):
                continue
            visit_key = str(active_visit.get("visit_key") or "")
            item = items_by_visit.get(visit_key)
            if (
                item is None
                or str(active_visit.get("hypothesis_id") or "")
                != item.hypothesis_id
                or str(active_visit.get("node_ref") or "") != item.node_ref
                or str(active_visit.get("defect_fingerprint") or "")
                != item.defect_state.fingerprint
            ):
                raise ValueError(
                    "investigation journal owner contradicts frontier action"
                )

        for visit_key, evidence in self.investigation_evidence.items():
            item = items_by_visit[visit_key]
            owned = self._owned_investigation_evidence(item)
            if len(owned) != len(evidence):
                raise ValueError(
                    "investigation evidence owner is missing or inconsistent"
                )
            journal_results = [
                entry.get("result")
                for entry in self.investigation_journal
                if isinstance(entry, Mapping)
                and isinstance(entry.get("active_visit"), Mapping)
                and str(entry["active_visit"].get("visit_key") or "")
                == visit_key
                and isinstance(entry.get("result"), Mapping)
            ]
            for value in evidence:
                persisted = dict(value)
                persisted.pop("owner", None)
                if not any(
                    stable_json(_checkpoint_json(persisted))
                    == stable_json(_checkpoint_json(result))
                    for result in journal_results
                ):
                    raise ValueError(
                        "investigation evidence owner has no action entry"
                    )

        for builder in self.seed_ledger.values():
            if builder.global_judgment:
                owner = LocalStateOwner.from_dict(
                    builder.global_judgment.get("owner")
                )
                if owner != _global_pass_owner(builder):
                    raise ValueError(
                        "seed global judgment owner contradicts seed ledger"
                    )
            for label, entries in (
                ("decisive evidence", builder.decisive_evidence),
                ("expansion history", builder.expansion_history),
            ):
                for entry in entries:
                    owner = LocalStateOwner.from_dict(entry.get("owner"))
                    item = items_by_visit.get(owner.visit_key)
                    if owner.seed_binding_identity != builder.key:
                        raise ValueError(
                            "{0} owner contradicts seed ledger".format(label)
                        )
                    valid = owner == _global_pass_owner(builder)
                    if not valid:
                        valid = any(
                            LocalStateOwner.from_dict(action.get("owner"))
                            == owner
                            for action in (
                                *self.confirmation_queue,
                                *self.confirmation_journal,
                            )
                            if isinstance(action, Mapping)
                        )
                    if not valid:
                        raise ValueError(
                            "{0} owner contradicts exact occurrence".format(label)
                        )
        self._validate_action_payload_reconciliation(action_records)

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
        resolved_starts = _dedupe_strings(
            resolved
            for ref in start_refs
            for resolved in (graph.resolve(ref) or str(ref),)
            if (
                graph.active_revision_start_eligible(resolved)
                or not graph.active_revision_evidence_eligible(resolved)
            )
        )
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
            defect_state = seed_defect_state(node, objective)
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
            if not graph.active_revision_start_eligible(start_ref):
                state._mark_seed_unresolved(
                    start_ref,
                    "start_ref_active_revision_ineligible",
                    "The attribution seed does not belong to the active repository generation.",
                    seed_key=builder.key,
                )
                continue
            if node.event_type in EVALUATION_START_EVENTS:
                predecessors = [
                    ref
                    for ref in graph.upstream_refs(start_ref)
                    if graph.nodes.get(ref)
                    and graph.active_revision_evidence_eligible(ref)
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
                            owner=_owner_for_seed_projection(
                                seed_binding_identity=builder.key,
                                node_ref=predecessor_ref,
                                defect_state=defect_state,
                                occurrence_key="initial_outcome_relation:{0}:{1}".format(
                                    start_ref, predecessor_ref
                                ),
                            ),
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
        self._validate_local_state_owners()
        if any(item.owner is None for item in self.causal_relations):
            raise ValueError("action state cannot persist ownerless causal relations")
        if any(
            item.owner is None
            or any(predecessor.owner is None for predecessor in item.predecessors)
            for item in self.step_judgments
        ):
            raise ValueError("action state cannot persist ownerless step judgments")
        if list(dict.fromkeys(self.visited_order)) != list(
            dict.fromkeys(
                str(item.get("node_ref") or "")
                for item in self.visited_entries
                if isinstance(item, Mapping)
            )
        ):
            raise ValueError("action state visited aggregate is not owner-backed")
        for item in self.visited_entries:
            if not isinstance(item, Mapping):
                raise ValueError("action state visited entry must be an object")
            LocalStateOwner.from_dict(item.get("owner"))
        for item in self.confirmation_queue:
            LocalStateOwner.from_dict(item.get("owner"))
        for item in self.confirmation_journal:
            LocalStateOwner.from_dict(item.get("owner"))
        for item in self.investigation_journal:
            if (
                isinstance(item, Mapping)
                and item.get("kind") == "global_candidate_pass"
                and item.get("seed_ref")
            ):
                LocalStateOwner.from_dict(item.get("owner"))
        return {
            "schema": ACTION_STATE_SCHEMA,
            "start_refs": list(self.start_refs),
            "objective": self.objective,
            "analysis_perspective": self.analysis_perspective,
            "causal_candidates": [item.to_dict() for item in self.causal_candidates],
            "causal_relations": [item.to_dict() for item in self.causal_relations],
            "step_judgments": [item.to_dict() for item in self.step_judgments],
            "step_action_projection": _checkpoint_json(
                self.step_action_projection
            ),
            "introduction_candidates": [item.to_dict() for item in self.introduction_candidates],
            "introduction_bindings": _checkpoint_json(self.introduction_bindings),
            "contributing_conditions": [item.to_dict() for item in self.contributing_conditions],
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "taint_paths": [list(item) for item in self.taint_paths],
            "visited_order": list(self.visited_order),
            "visited_entries": _checkpoint_json(self.visited_entries),
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
            "confirmation_action_projection": _checkpoint_json(
                self.confirmation_action_projection
            ),
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
        checkpoint_budgets = checkpoint.config.get("budgets")
        _validated_global_judge_action_history(
            checkpoint.actions,
            max_judge_requests=(
                int(checkpoint_budgets["max_judge_requests"])
                if isinstance(checkpoint_budgets, Mapping)
                and "max_judge_requests" in checkpoint_budgets
                else None
            ),
            cache_identity=(
                str(checkpoint.config["cache_identity"])
                if "cache_identity" in checkpoint.config
                else None
            ),
        )
        checkpoint_seed_authority = _seed_authority_from_records(
            action_payload["seed_ledger"]
        )
        _classify_global_pass_records(
            action_payload["investigation_journal"],
            seed_authority=checkpoint_seed_authority,
        )
        _classify_global_failure_episodes(
            action_payload["unresolved_branches"],
            seed_authority=checkpoint_seed_authority,
        )
        formal_unbound_start_refs = {
            graph.resolve(str(ref)) or str(ref)
            for ref in action_payload["start_refs"]
            if graph.active_revision_evidence_eligible(str(ref))
            and not graph.active_revision_start_eligible(str(ref))
        }
        if formal_unbound_start_refs:
            raise ValueError(
                "checkpoint contains an analysis start without strict "
                "active-start provenance: {0}".format(
                    sorted(formal_unbound_start_refs)
                )
            )
        stale_start_refs = {
            graph.resolve(str(ref)) or str(ref)
            for ref in action_payload["start_refs"]
            if not graph.active_revision_start_eligible(str(ref))
        }
        stale_seed_keys = {
            _seed_ledger_key(
                str(item.get("start_ref") or ""),
                str(item.get("defect_fingerprint") or ""),
            )
            for item in action_payload["seed_ledger"]
            if isinstance(item, Mapping)
            and (graph.resolve(str(item.get("start_ref") or ""))
                 or str(item.get("start_ref") or ""))
            in stale_start_refs
        }
        stale_hypothesis_ids = {
            str(hypothesis_id)
            for hypothesis_id, seed_key in dict(
                action_payload["hypothesis_seed_keys"]
            ).items()
            if str(seed_key) in stale_seed_keys
        }
        graph.assert_evidence_eligible_references(
            frontier_payload,
            label="restored recursive frontier state",
            allowed_ineligible_refs=stale_start_refs,
        )
        graph.assert_evidence_eligible_references(
            hypothesis_payload,
            label="restored recursive hypothesis state",
            allowed_ineligible_refs=stale_start_refs,
        )
        graph.assert_evidence_eligible_references(
            action_payload,
            label="restored recursive action state",
            allowed_ineligible_refs=stale_start_refs,
        )

        ledger = HypothesisLedger.from_snapshot(hypothesis_payload["hypotheses"])
        restored_hypotheses = ledger.hypotheses_by_id()
        stale_owned_candidate_refs = {
            hypothesis.candidate_root_ref
            for hypothesis_id, hypothesis in restored_hypotheses.items()
            if hypothesis_id in stale_hypothesis_ids
        }
        active_owned_candidate_refs = {
            hypothesis.candidate_root_ref
            for hypothesis_id, hypothesis in restored_hypotheses.items()
            if hypothesis_id not in stale_hypothesis_ids
        }
        stale_only_candidate_refs = (
            stale_owned_candidate_refs - active_owned_candidate_refs
        )
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
            if graph.active_revision_evidence_eligible(candidate.ref)
            and candidate.ref not in stale_only_candidate_refs
        ]
        restored_relations = [
            PredecessorAssessment.from_dict(item)
            for item in action_payload["causal_relations"]
        ]
        if any(relation.owner is None for relation in restored_relations):
            raise ValueError("restored causal relation is ownerless")
        state.causal_relations = [
            relation
            for relation in restored_relations
            if relation.owner is not None
            and relation.owner.seed_binding_identity not in stale_seed_keys
        ]
        restored_judgments = [
            CausalStepJudgment.from_dict(item)
            for item in action_payload["step_judgments"]
        ]
        if any(
            judgment.owner is None
            or any(assessment.owner is None for assessment in judgment.predecessors)
            for judgment in restored_judgments
        ):
            raise ValueError("restored causal step judgment is ownerless")
        state.step_judgments = [
            judgment
            for judgment in restored_judgments
            if judgment.owner is not None
            and judgment.owner.seed_binding_identity not in stale_seed_keys
        ]
        state.step_action_projection = []
        items_by_visit = state._frontier_items_by_visit()
        for value in action_payload["step_action_projection"]:
            if not isinstance(value, Mapping):
                raise ValueError(
                    "restored step action projection must be an object"
                )
            seed_binding_identity = str(
                value.get("seed_binding_identity") or ""
            )
            if seed_binding_identity in stale_seed_keys:
                continue
            item = items_by_visit.get(str(value.get("visit_key") or ""))
            if item is None:
                raise ValueError(
                    "restored step action projection has no frontier visit"
                )
            state.step_action_projection.append(
                _validated_step_action_projection(value, item=item)
            )
        state.introduction_candidates = [
            candidate
            for candidate in (
                CausalCandidate.from_dict(item)
                for item in action_payload["introduction_candidates"]
            )
            if authored_root_candidate_eligible(graph, candidate.ref)
            and candidate.ref not in stale_only_candidate_refs
        ]
        state.introduction_bindings = [
            copy.deepcopy(item)
            for item in action_payload["introduction_bindings"]
            if not isinstance(item, Mapping)
            or (
                str(item.get("seed_binding_identity") or "")
                not in stale_seed_keys
                and str(item.get("seed_key") or "") not in stale_seed_keys
                and str(item.get("hypothesis_id") or "")
                not in stale_hypothesis_ids
            )
        ]
        state.introduction_binding_keys = {
            (
                str(item.get("candidate_ref") or ""),
                str(item.get("defect_fingerprint") or ""),
                str(item.get("hypothesis_semantic_hash") or ""),
                str(item.get("seed_binding_identity") or ""),
            )
            for item in state.introduction_bindings
        }
        state.contributing_conditions = [
            factor
            for factor in (
                CausalFactor.from_dict(item)
                for item in action_payload["contributing_conditions"]
            )
            if str(factor.confirmation.get("seed_binding_identity") or "")
            not in stale_seed_keys
            and str(factor.confirmation.get("hypothesis_id") or "")
            not in stale_hypothesis_ids
        ]
        state.rejected_candidates = [
            rejected
            for rejected in (
                RejectedCandidate.from_dict(item)
                for item in action_payload["rejected_candidates"]
            )
            if str(rejected.confirmation.get("seed_binding_identity") or "")
            not in stale_seed_keys
            and str(rejected.confirmation.get("hypothesis_id") or "")
            not in stale_hypothesis_ids
        ]
        state.taint_paths = [
            path
            for path in (
                tuple(str(ref) for ref in item)
                for item in action_payload["taint_paths"]
            )
            if not any(
                ref in stale_start_refs or ref in stale_only_candidate_refs
                for ref in path
            )
        ]
        state.visited_entries = []
        for item in action_payload["visited_entries"]:
            if not isinstance(item, Mapping) or set(item) != {"node_ref", "owner"}:
                raise ValueError("restored visited entry schema mismatch")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.seed_binding_identity in stale_seed_keys:
                continue
            state.visited_entries.append(
                {
                    "node_ref": str(item.get("node_ref") or ""),
                    "owner": owner.to_dict(),
                }
            )
        state.visited_order = list(
            dict.fromkeys(
                str(item["node_ref"]) for item in state.visited_entries
            )
        )
        if list(dict.fromkeys(str(item) for item in action_payload["visited_order"])) != list(
            dict.fromkeys(
                str(item.get("node_ref") or "")
                for item in action_payload["visited_entries"]
                if isinstance(item, Mapping)
            )
        ):
            raise ValueError("restored visited_order does not match owned entries")
        state.unresolved_branches = [
            copy.deepcopy(item)
            for item in action_payload["unresolved_branches"]
            if not isinstance(item, Mapping)
            or (
                str(item.get("hypothesis_id") or "")
                not in stale_hypothesis_ids
                and str(item.get("node_ref") or "")
                not in stale_only_candidate_refs
            )
        ]
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
        investigation_journal = _migrate_checkpoint_visit_references(
            action_payload["investigation_journal"], frontier
        )
        _classify_global_pass_records(
            investigation_journal,
            seed_authority=checkpoint_seed_authority,
        )
        _classify_global_failure_episodes(
            action_payload["unresolved_branches"],
            seed_authority=checkpoint_seed_authority,
        )
        stale_visit_keys = {
            item.visit_key
            for item in frontier.lifecycle_items()
            if item.hypothesis_id in stale_hypothesis_ids
        }

        retained_investigation_journal = [
            item
            for item in investigation_journal
            if not _investigation_owned_by_stale_seed(
                item,
                stale_seed_keys=stale_seed_keys,
                stale_hypothesis_ids=stale_hypothesis_ids,
                stale_visit_keys=stale_visit_keys,
                stale_start_refs=stale_start_refs,
            )
        ]
        if len(retained_investigation_journal) != len(
            investigation_journal
        ):
            (
                state.investigation_rounds,
                state.investigation_result_bytes,
            ) = _investigation_journal_counters(
                retained_investigation_journal
            )
        state.investigation_journal = retained_investigation_journal
        state.investigation_evidence = {
            frontier.migrated_visit_key(str(key)): copy.deepcopy(value)
            for key, value in dict(action_payload["investigation_evidence"]).items()
            if frontier.migrated_visit_key(str(key)) not in stale_visit_keys
        }
        state.investigation_evidence_hashes = {
            frontier.migrated_visit_key(str(key)): {str(item) for item in values}
            for key, values in dict(action_payload["investigation_evidence_hashes"]).items()
            if frontier.migrated_visit_key(str(key)) not in stale_visit_keys
        }
        state.control_directive_ids = {
            str(item.get("directive_id") or "")
            for item in state.investigation_journal
            if isinstance(item, Mapping) and item.get("directive_id")
        }
        state.confirmation_queue = []
        for item in action_payload["confirmation_queue"]:
            if not isinstance(item, Mapping):
                raise ValueError("restored confirmation queue entry must be an object")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.seed_binding_identity in stale_seed_keys:
                continue
            state.confirmation_queue.append(copy.deepcopy(item))
        state.confirmation_queue_keys = {
            tuple(str(part) for part in item)
            for item in action_payload["confirmation_queue_keys"]
            if len(item) >= 4 and str(item[3]) not in stale_seed_keys
        }
        state.validate_confirmation_queue_bound()
        state.confirmations = [
            confirmation
            for confirmation in (
                RootConfirmation.from_dict(item)
                for item in action_payload["confirmations"]
            )
            if confirmation.seed_binding_identity not in stale_seed_keys
        ]
        state.confirmed_roots = [
            root
            for root in (
                ConfirmedRoot.from_dict(item)
                for item in action_payload["confirmed_roots"]
            )
            if root.hypothesis_id not in stale_hypothesis_ids
        ]
        state.co_roots = [
            root
            for root in (
                ConfirmedRoot.from_dict(item)
                for item in action_payload["co_roots"]
            )
            if root.hypothesis_id not in stale_hypothesis_ids
        ]
        state.amplifying_factors = [
            factor
            for factor in (
                CausalFactor.from_dict(item)
                for item in action_payload["amplifying_factors"]
            )
            if str(factor.confirmation.get("seed_binding_identity") or "")
            not in stale_seed_keys
            and str(factor.confirmation.get("hypothesis_id") or "")
            not in stale_hypothesis_ids
        ]
        for confirmation in state.confirmations:
            node = graph.nodes.get(confirmation.candidate_ref)
            if (
                node is None
                or not authored_root_candidate_eligible(
                    graph, confirmation.candidate_ref
                )
            ):
                raise ValueError(
                    "restored confirmation candidate is ineligible for the active revision"
                )
        if any(
            not authored_root_candidate_eligible(graph, root.node_ref)
            for root in (*state.confirmed_roots, *state.co_roots)
        ):
            raise ValueError(
                "restored published root is ineligible for the active revision"
            )
        state.confirmation_journal = []
        for item in action_payload["confirmation_journal"]:
            if not isinstance(item, Mapping):
                raise ValueError("restored confirmation journal entry must be an object")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.seed_binding_identity in stale_seed_keys:
                continue
            state.confirmation_journal.append(copy.deepcopy(item))
        state.confirmation_action_projection = []
        for item in action_payload["confirmation_action_projection"]:
            projection = _validated_confirmation_action_projection(item)
            if projection["seed_binding_identity"] in stale_seed_keys:
                continue
            state.confirmation_action_projection.append(projection)
        state.pending_rejudge_journal = {
            frontier.migrated_visit_key(str(key)): [int(item) for item in values]
            for key, values in dict(action_payload["pending_rejudge_journal"]).items()
            if frontier.migrated_visit_key(str(key)) not in stale_visit_keys
        }
        state.seed_ledger = {
            builder.key: builder
            for builder in (
                SeedAttributionBuilder.from_dict(item)
                for item in action_payload["seed_ledger"]
            )
        }
        for builder in state.seed_ledger.values():
            if builder.key not in stale_seed_keys:
                continue
            builder.no_defect = False
            builder.candidate_refs.clear()
            builder.selected_candidate_refs.clear()
            builder.confirmation_identities.clear()
            builder.confirmed_root_refs.clear()
            builder.confirmed_root_confirmation_identities.clear()
            builder.decisive_evidence_refs.clear()
            builder.decisive_evidence = []
            builder.global_judgment = {}
            builder.expansion_history = []
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
            if judgment and builder.key not in stale_seed_keys:
                envelope = judgment.get("validation_envelope")
                global_candidate_request_from_validation_envelope(
                    envelope,
                    graph=graph,
                    authoritative_candidates=(
                        _global_envelope_authoritative_candidates(
                            graph,
                            envelope,
                            state.causal_candidates,
                        )
                    ),
                    authoritative_objective=state.objective,
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
        for builder in state.seed_ledger.values():
            if builder.key not in stale_seed_keys:
                continue
            builder.mark_unresolved(
                "start_ref_active_revision_ineligible",
                "The restored analysis start is ineligible for the active repository generation.",
            )
        for hypothesis_id, seed_key in state.hypothesis_seed_keys.items():
            if seed_key not in stale_seed_keys:
                continue
            hypothesis = state.ledger.get(hypothesis_id)
            if hypothesis.status not in {"active", "supported"}:
                continue
            state.ledger.reject_with_frontier(
                hypothesis_id,
                "The restored analysis start is ineligible for the active repository generation.",
                opposing_refs=(),
                frontier=state.frontier,
                evidence_hash="start_ref_active_revision_ineligible",
            )
            state.unresolved_hypothesis_ids.add(hypothesis_id)
        if frontier.has_legacy_visit_key_migrations():
            state.replay_actions = _migrate_checkpoint_journal_records(
                checkpoint.actions, frontier
            )
        else:
            state.replay_actions = copy.deepcopy(checkpoint.latest_actions)
        _assert_canonical_published_roots(
            graph,
            confirmations=state.confirmations,
            seed_results=state.seed_results(),
            confirmed_roots=state.confirmed_roots,
            co_roots=state.co_roots,
            analysis_perspective=state.analysis_perspective,
            label="restored recursive state",
        )
        _assert_published_non_root_factors(
            graph,
            confirmations=state.confirmations,
            seed_results=state.seed_results(),
            defect_states=tuple(state.defect_states.values()),
            contributing_conditions=state.contributing_conditions,
            amplifying_factors=state.amplifying_factors,
            rejected_candidates=state.rejected_candidates,
            analysis_perspective=state.analysis_perspective,
            label="restored recursive state",
        )
        snapshot_transaction = int(
            action_record.get("transaction_sequence") or 0
        )
        state._validate_local_state_owners(
            item
            for item in checkpoint.actions
            if int(item.get("transaction_sequence") or 0)
            <= snapshot_transaction
        )
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
            if graph.active_revision_evidence_eligible(candidate.ref)
        )
        retrieved_candidates = tuple(
            candidate
            for candidate in (retrieved_candidates or candidates)
            if graph.active_revision_evidence_eligible(candidate.ref)
        )
        hypothesis = self.ledger.get(item.hypothesis_id)
        chain = self.transformation_chains.get(item.defect_state.fingerprint, (item.defect_state,))
        downstream_judgments = self._owned_downstream_judgments(item)
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
        investigated = self._owned_investigation_evidence(item)
        if item.visit_key in self.investigation_evidence:
            context["investigation_evidence"] = copy.deepcopy(investigated)
        context = graph.sanitize_judge_visible_payload(context)
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
            current_node=graph.sanitize_judge_node(
                graph.hydrate_node(item.node_ref)
            ),
            defect_state=item.defect_state,
            candidates=tuple(
                CausalCandidate(
                    ref=candidate.ref,
                    node=graph.sanitize_judge_node(
                        graph.hydrate_node(candidate.ref)
                    ),
                    source=candidate.source,
                    edge=graph.sanitize_judge_visible_payload(
                        graph.sanitize_judge_edge_evidence(candidate.edge)
                    ),
                    score=candidate.score,
                    evidence_refs=tuple(
                        graph.filter_evidence_refs(candidate.evidence_refs)
                    ),
                )
                for candidate in candidates
            ),
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

    def record_completed_step_projection(
        self,
        item: FrontierItem,
        projection: Mapping[str, Any],
    ) -> None:
        canonical = _validated_step_action_projection(
            projection,
            item=item,
        )
        if any(
            existing.get("semantic_key") == canonical["semantic_key"]
            for existing in self.step_action_projection
            if isinstance(existing, Mapping)
        ):
            raise ValueError(
                "completed step Provider action has duplicate semantic key"
            )
        judgment = CausalStepJudgment.from_dict(
            canonical["step_judgment"]
        )
        self.step_action_projection.append(canonical)
        self.step_judgments.append(judgment)
        self.causal_relations.extend(
            PredecessorAssessment.from_dict(relation)
            for relation in canonical["causal_relations"]
        )

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
        provider_action_key: str,
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
        judgment = _owned_step_judgment(
            item, judgment, provider_action_key
        )
        if not any(
            existing == judgment for existing in self.step_judgments
        ):
            raise ValueError(
                "step state transition has no completed Provider action "
                "projection"
            )
        self.visited_order.append(item.node_ref)
        self.visited_entries.append(
            {
                "node_ref": item.node_ref,
                "owner": _owner_for_item(item, "visited_node").to_dict(),
            }
        )
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
            if node is not None and (
                not authored_root_candidate_eligible(
                    self.graph, item.node_ref
                )
            ):
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
            if authored_root_candidate_eligible(self.graph, candidate.ref)
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
        self.validate_confirmation_queue_bound()
        self._validate_local_state_owners()
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
        seed_authority = _seed_authority_from_records(
            builder.to_dict() for builder in self.seed_ledger.values()
        )
        (
            completed_global_passes,
            failed_global_passes,
        ) = _classify_global_pass_records(
            self.investigation_journal,
            seed_authority=seed_authority,
        )
        global_passes = [
            *completed_global_passes,
            *failed_global_passes,
        ]
        expansion_reasons = []
        for item in completed_global_passes:
            judgment = item.get("judgment")
            if not isinstance(judgment, Mapping):
                continue
            for request in judgment.get("expansion_requests") or ():
                if isinstance(request, Mapping):
                    expansion_reasons.append(
                        {
                            **dict(request),
                            "owner": copy.deepcopy(item.get("owner")),
                        }
                    )
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
            "confirmation_queue_keys": [
                list(item)
                for item in sorted(self.confirmation_queue_keys)
            ],
            "confirmation_journal": list(self.confirmation_journal),
            "confirmation_action_projection": copy.deepcopy(
                self.confirmation_action_projection
            ),
            "step_action_projection": copy.deepcopy(
                self.step_action_projection
            ),
            "logical_confirmation_call_count": self.logical_confirmation_calls,
            "fusion_mode": "retrieval-global" if global_passes else "off",
            "global_candidate_pass_count": len(
                completed_global_passes
            ) + len(failed_global_passes),
            "global_candidate_judgments": [
                {
                    **copy.deepcopy(item.get("judgment")),
                    "owner": copy.deepcopy(item.get("owner")),
                }
                for item in completed_global_passes
                if isinstance(item.get("judgment"), Mapping)
            ],
            "candidate_compression": [
                {
                    **copy.deepcopy(item.get("candidate_compression")),
                    "owner": copy.deepcopy(item.get("owner")),
                }
                for item in completed_global_passes
                if isinstance(item.get("candidate_compression"), Mapping)
            ],
            "recursive_expansion_reasons": expansion_reasons,
            "global_candidate_failures": [
                _global_failure_projection_from_action(item)
                for item in failed_global_passes
            ],
            "global_judge_physical_request_count": sum(
                int(item.get("physical_request_delta") or 0)
                for item in (
                    *completed_global_passes,
                    *failed_global_passes,
                )
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
            visited_entries=tuple(self.visited_entries),
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
        resolved = self.graph.resolve(ref)
        node = self.graph.nodes.get(resolved or "")
        if node is None or not self.graph.active_revision_evidence_eligible(ref):
            return None
        return CausalCandidate(
            ref=resolved,
            node=node,
            source=source,
            edge=dict(edge),
            score=1.0,
            evidence_refs=evidence_refs,
        )

    def _remember_candidate(self, candidate: CausalCandidate) -> None:
        if not self.graph.active_revision_evidence_eligible(candidate.ref):
            return
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

    def _persist_confirmation_action(
        self,
        state: RecursiveAnalysisState,
        queued: JsonDict,
        confirmation: RootConfirmation,
        *,
        operation: str,
        physical_requests_reserved: int,
        physical_request_delta: int,
        physical_request_exact: bool,
        provider_state: Optional[Mapping[str, Any]] = None,
        evidence_disposition: Optional[Mapping[str, Any]] = None,
    ) -> None:
        request_identity = str(queued.get("semantic_identity") or "")
        semantic_key = "confirmation:{0}".format(request_identity)
        artifact_evidence_envelopes = copy.deepcopy(
            list(queued.get("artifact_evidence_envelopes") or ())
        )
        terminal_disposition = (
            copy.deepcopy(dict(evidence_disposition))
            if isinstance(evidence_disposition, Mapping)
            else _build_terminal_evidence_disposition(
                state.graph,
                artifact_evidence_envelopes,
            )
        )
        projection = _confirmation_action_projection(
            operation=operation,
            semantic_key=semantic_key,
            request_identity=request_identity,
            owner=queued.get("owner"),
            seed_key=str(
                queued.get("seed_key")
                or queued.get("seed_binding_identity")
                or ""
            ),
            confirmation=confirmation,
            physical_requests_reserved=physical_requests_reserved,
            physical_request_delta=physical_request_delta,
            physical_request_exact=physical_request_exact,
            factual_request_projection=queued.get(
                "factual_request_projection"
            ),
            artifact_evidence_envelopes=artifact_evidence_envelopes,
            evidence_disposition=terminal_disposition,
        )
        self._validate_terminal_confirmation_for_action(
            state,
            queued,
            confirmation,
            projection,
            label="terminal confirmation",
        )
        payload = {
            "status": confirmation.status,
            "physical_requests_reserved": physical_requests_reserved,
            "physical_request_delta": physical_request_delta,
            "physical_request_exact": physical_request_exact,
            "confirmation": confirmation.to_dict(),
            "action_projection": projection,
        }
        if provider_state is not None:
            payload["provider_state"] = copy.deepcopy(dict(provider_state))
        self._checkpoint_action(operation, semantic_key, payload)
        self._record_confirmation(state, queued, confirmation, projection)

    def _validate_terminal_confirmation_for_action(
        self,
        state: RecursiveAnalysisState,
        queued: Mapping[str, Any],
        confirmation: RootConfirmation,
        action_projection: Mapping[str, Any],
        *,
        label: str,
    ) -> JsonDict:
        defect_state = state.defect_states.get(
            str(queued.get("defect_fingerprint") or "")
        )
        if defect_state is None:
            raise ValueError(
                "{0} projection binding has no canonical defect state".format(
                    label
                )
            )
        _validate_confirmation_request_projection_binding(
            queued,
            defect_state=defect_state,
            analysis_perspective=state.analysis_perspective,
            ledger=state.ledger,
            frontier=state.frontier,
            confirmation=confirmation,
            action_projection=action_projection,
            label=label,
        )
        node = state.graph.nodes.get(confirmation.candidate_ref)
        if (
            node is None
            or not authored_root_candidate_eligible(
                state.graph, confirmation.candidate_ref
            )
        ):
            raise ValueError(
                "{0} candidate is ineligible for the active revision".format(
                    label
                )
            )
        if any(
            not state.graph.active_revision_evidence_eligible(ref)
            for ref in confirmation.recursive_path
        ):
            raise ValueError(
                "{0} path contains evidence ineligible for the active "
                "revision".format(label)
            )
        projection = _validated_confirmation_action_projection(
            action_projection
        )
        disposition = _validated_terminal_evidence_disposition(
            projection["evidence_disposition"],
            artifact_evidence_envelopes=projection[
                "artifact_evidence_envelopes"
            ],
            confirmation=confirmation,
            operation=projection["operation"],
        )
        if disposition["state"] == "validated":
            _validate_terminal_confirmation_evidence(
                state.graph,
                confirmation=confirmation,
                artifact_evidence_envelopes=projection[
                    "artifact_evidence_envelopes"
                ],
                label=label,
            )
            current_request = self._build_confirmation_request(
                state,
                queued,
            )
            rebound_confirmation = bind_root_confirmation(
                confirmation,
                request=current_request,
            )
            if rebound_confirmation != confirmation:
                raise ValueError(
                    "{0} response does not exactly rebind to its current "
                    "factual request".format(label)
                )
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        seed_key = str(
            queued.get("seed_key")
            or state.hypothesis_seed_keys.get(hypothesis_id, "")
        )
        owner = LocalStateOwner.from_dict(queued.get("owner"))
        if (
            owner.seed_binding_identity
            != confirmation.seed_binding_identity
            or owner.hypothesis_id != hypothesis_id
            or projection["owner"] != owner.to_dict()
            or projection["seed_key"] != seed_key
            or projection["confirmation"] != confirmation.to_dict()
            or projection["request_identity"]
            != str(queued.get("semantic_identity") or "")
            or projection["artifact_evidence_envelopes"]
            != list(queued.get("artifact_evidence_envelopes") or ())
            or projection["factual_request_projection"]
            != queued.get("factual_request_projection")
            or (
                queued.get("evidence_disposition") is not None
                and projection["evidence_disposition"]
                != queued.get("evidence_disposition")
            )
        ):
            raise ValueError(
                "{0} owner contradicts confirmed identity".format(label)
            )
        return projection

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
        self._apply_validated_provider_result_state(state, provider)

    def _apply_validated_provider_result_state(
        self,
        state: RecursiveAnalysisState,
        provider: Mapping[str, Any],
    ) -> None:
        state.provider_state = copy.deepcopy(dict(provider))
        circuit = provider["circuit"]
        target = _judge_transport(self.judge)
        target.provider_circuit_open = bool(circuit["open"])
        target.provider_circuit_reason = str(circuit["reason"])
        target.consecutive_provider_errors = int(
            circuit["consecutive_provider_errors"]
        )
        target.provider_error_threshold = int(circuit["provider_error_threshold"])

    def _prevalidate_completed_replay_provider_state(
        self,
        state: RecursiveAnalysisState,
        payload: Mapping[str, Any],
        projection: Mapping[str, Any],
    ) -> JsonDict:
        projected_state = copy.copy(state)
        if projection["physical_request_exact"]:
            projected_requests = (
                state.judge_requests
                + projection["physical_request_delta"]
                - projection["physical_requests_reserved"]
            )
            if projected_requests < 0:
                raise ValueError(
                    "completed confirmation replay accounting is invalid"
                )
            projected_state.judge_requests = projected_requests
        else:
            projected_state.judge_request_uncertainty_count += 1
        provider = _validate_provider_state(
            payload.get("provider_state"),
            projected_state,
            cache_identity=str(
                self.checkpoint_config.get("cache_identity") or ""
            ),
        )
        return provider

    def _prevalidate_global_replay_provider_state(
        self,
        state: RecursiveAnalysisState,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        projected_state = copy.copy(state)
        projected_state.logical_judge_calls += 1
        projected_state.judge_requests += payload[
            "physical_request_delta"
        ]
        if not payload["physical_request_exact"]:
            projected_state.judge_request_uncertainty_count += 1
        return _validate_provider_state(
            payload.get("provider_state"),
            projected_state,
            cache_identity=str(
                self.checkpoint_config.get("cache_identity") or ""
            ),
        )

    @staticmethod
    def _replay_action(state: RecursiveAnalysisState, semantic_key: str) -> Optional[JsonDict]:
        value = state.replay_actions.get(semantic_key)
        return dict(value) if isinstance(value, Mapping) else None

    def _run_global_candidate_prepass(
        self, state: RecursiveAnalysisState, graph: TraceGraph
    ) -> None:
        if self.fusion_mode != "retrieval-global":
            return
        seed_authority = _seed_authority_from_records(
            builder.to_dict() for builder in state.seed_ledger.values()
        )
        terminal_pass_identities = {
            str(event.get("pass_identity") or "")
            for event in _terminal_global_passes(
                state.investigation_journal,
                seed_authority=seed_authority,
            )
        }
        queued_items = [
            FrontierItem.from_dict(item) for item in state.frontier.snapshot()
        ]
        items_by_seed: Dict[str, List[FrontierItem]] = {}
        for item in queued_items:
            builder = state._seed_builder_for_item(item)
            if builder is not None:
                items_by_seed.setdefault(builder.key, []).append(item)

        def fail_seed(
            *,
            builder: SeedAttributionBuilder,
            items: Sequence[FrontierItem],
            blocker: str,
            detail: str,
            physical_request_delta: int = 0,
            physical_request_exact: bool = True,
            candidate_compression: Optional[Mapping[str, Any]] = None,
            accounting_already_applied: bool = False,
        ) -> None:
            if not accounting_already_applied:
                state.judge_requests += physical_request_delta
                if not physical_request_exact:
                    state.judge_request_uncertainty_count += 1
            pass_identity = _global_pass_identity(builder.key)
            owner = _global_pass_owner(builder)
            failure_projection = _global_failure_projection(
                builder=builder,
                blocker=blocker,
                detail=detail,
                physical_request_delta=physical_request_delta,
                physical_request_exact=physical_request_exact,
            )
            event = {
                "kind": "global_candidate_pass",
                "status": "failed",
                "pass_identity": pass_identity,
                "seed_binding_identity": builder.key,
                "seed_ref": builder.start_ref,
                "defect_fingerprint": builder.defect_state.fingerprint,
                "hypothesis_id": (
                    items[0].hypothesis_id if items else ""
                ),
                "visit_key": items[0].visit_key if items else "",
                "owner": owner.to_dict(),
                "blocker": blocker,
                "reason": detail,
                "missing_evidence": [detail],
                "physical_request_delta": physical_request_delta,
                "physical_request_exact": physical_request_exact,
                "candidate_compression": copy.deepcopy(
                    dict(candidate_compression or {})
                ),
                "failure_projection": copy.deepcopy(failure_projection),
                "behavior_impact": "none_offline_analysis_only",
            }
            state.investigation_journal.append(event)
            builder.mark_unresolved(blocker, detail)
            state.unresolved_refs.append(builder.start_ref)
            state.unresolved_branches.append(
                {
                    "node_ref": builder.start_ref,
                    "defect_state_id": builder.defect_state.defect_state_id,
                    "hypothesis_id": "",
                    "reason": blocker,
                    "details": detail,
                    "depth": 0,
                    "global_pass_identity": pass_identity,
                    "owner": owner.to_dict(),
                    "failure_projection": copy.deepcopy(
                        failure_projection
                    ),
                }
            )
            for failed_item in items:
                hypothesis = state.ledger.get(failed_item.hypothesis_id)
                if hypothesis.status in {"active", "supported"}:
                    state.ledger.reject_with_frontier(
                        hypothesis.hypothesis_id,
                        detail,
                        opposing_refs=(),
                        frontier=state.frontier,
                        evidence_hash=pass_identity,
                    )
                state.unresolved_hypothesis_ids.add(
                    failed_item.hypothesis_id
                )
            self._checkpoint_state(
                state, "global:failed:{0}".format(pass_identity)
            )

        for seed_key, seed_items in items_by_seed.items():
            builder = state.seed_ledger[seed_key]
            pass_identity = _global_pass_identity(seed_key)
            if pass_identity in terminal_pass_identities:
                continue
            item = seed_items[0]
            active_seed_ref = builder.start_ref
            if not isinstance(self.judge, GlobalJudgeCapability):
                fail_seed(
                    builder=builder,
                    items=seed_items,
                    blocker="global_judge_capability_missing",
                    detail=(
                        "retrieval-global is enabled but the Judge exposes no "
                        "global candidate capability."
                    ),
                )
                terminal_pass_identities.add(pass_identity)
                continue
            node = graph.nodes.get(item.node_ref)
            if (
                node is None
                or not graph.active_revision_evidence_eligible(item.node_ref)
                or not graph.active_revision_start_eligible(active_seed_ref)
            ):
                fail_seed(
                    builder=builder,
                    items=seed_items,
                    blocker="global_seed_active_start_ineligible",
                    detail=(
                        "The enabled global pass seed lacks strict active-start "
                        "provenance."
                    ),
                )
                terminal_pass_identities.add(pass_identity)
                continue
            hypothesis = state.ledger.get(item.hypothesis_id)
            if hypothesis.status not in {"active", "supported"}:
                continue
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
                if not capsules:
                    raise ValueError("candidate evidence capsules are empty")
            except Exception as exc:
                fail_seed(
                    builder=builder,
                    items=seed_items,
                    blocker="global_candidate_capsule_failure",
                    detail="{0}: {1}".format(type(exc).__name__, exc),
                )
                terminal_pass_identities.add(pass_identity)
                continue
            for candidate in candidates:
                state._remember_candidate(candidate)
            metrics = candidate_compression_metrics(graph, capsules)
            try:
                request = GlobalCandidateJudgeRequest(
                    case_id=graph.case_id,
                    objective=state.objective,
                    analysis_perspective=state.analysis_perspective,
                    seed_ref=active_seed_ref,
                    active_defect=builder.defect_state,
                    active_focus_text=builder.defect_state.actual,
                    active_focus_text_hash=active_focus_text_sha256(
                        builder.defect_state.actual
                    ),
                    start_refs=(active_seed_ref,),
                    capsules=capsules,
                    trace_health={
                        "missing_artifact_count": sum(
                            len(capsule.missing_evidence_refs)
                            for capsule in capsules
                        ),
                        "candidate_compression": metrics,
                    },
                )
                validate_global_candidate_request_against_graph(
                    graph,
                    request,
                    authoritative_candidates=candidates,
                    authoritative_objective=state.objective,
                )
            except Exception as exc:
                fail_seed(
                    builder=builder,
                    items=seed_items,
                    blocker="global_request_validation_failure",
                    detail="{0}: {1}".format(type(exc).__name__, exc),
                    candidate_compression=metrics,
                )
                terminal_pass_identities.add(pass_identity)
                continue
            remaining = max(
                0, self.max_judge_requests - state.judge_requests
            )
            action_key = _global_judge_action_key(pass_identity)
            replay_action = self._replay_action(state, action_key)
            replay_payload: Optional[JsonDict] = None
            replay_provider_state: Optional[JsonDict] = None
            reserved_requests = remaining
            if replay_action is not None:
                replay_payload = _validated_global_judge_action(
                    replay_action,
                    builder=builder,
                    item=item,
                    request=request,
                    candidate_compression=metrics,
                    expected_physical_requests_reserved=remaining,
                )
                reserved_requests = replay_payload[
                    "physical_requests_reserved"
                ]
                if (
                    replay_action["operation"]
                    == "global_judge_started"
                ):
                    state.logical_judge_calls += 1
                    state.judge_requests += reserved_requests
                    state.judge_request_uncertainty_count += 1
                    blocker = "global_judge_interrupted"
                    detail = (
                        "The prior process ended after durably recording the "
                        "Global Judge request but before a terminal result; "
                        "the request is not repeated."
                    )
                    failure_projection = _global_failure_projection(
                        builder=builder,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=reserved_requests,
                        physical_request_exact=False,
                    )
                    failed_payload = {
                        **replay_payload,
                        "status": "failed",
                        "physical_request_delta": reserved_requests,
                        "physical_request_exact": False,
                        "blocker": blocker,
                        "detail": detail,
                        "failure_projection": failure_projection,
                        "provider_state": self._capture_provider_result_state(
                            state
                        ),
                    }
                    self._checkpoint_action(
                        "global_judge_failed",
                        action_key,
                        failed_payload,
                    )
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=reserved_requests,
                        physical_request_exact=False,
                        candidate_compression=metrics,
                        accounting_already_applied=True,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                replay_provider_state = (
                    self._prevalidate_global_replay_provider_state(
                        state,
                        replay_payload,
                    )
                )
                state.logical_judge_calls += 1
                state.judge_requests += replay_payload[
                    "physical_request_delta"
                ]
                if not replay_payload["physical_request_exact"]:
                    state.judge_request_uncertainty_count += 1
                self._apply_validated_provider_result_state(
                    state,
                    replay_provider_state,
                )
                if replay_action["operation"] == "global_judge_failed":
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker=replay_payload["blocker"],
                        detail=replay_payload["detail"],
                        physical_request_delta=replay_payload[
                            "physical_request_delta"
                        ],
                        physical_request_exact=replay_payload[
                            "physical_request_exact"
                        ],
                        candidate_compression=metrics,
                        accounting_already_applied=True,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                judgment = validate_global_candidate_payload(
                    replay_payload["judgment"],
                    request=request,
                )
                physical_delta = replay_payload[
                    "physical_request_delta"
                ]
            else:
                if remaining == 0:
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker="judge_request_budget_exhausted",
                        detail=(
                            "The Global Judge physical request budget is "
                            "exhausted before this seed can start."
                        ),
                        candidate_compression=metrics,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                started_payload = _global_judge_action_base(
                    builder=builder,
                    item=item,
                    request=request,
                    candidate_compression=metrics,
                    physical_requests_reserved=remaining,
                )
                self._checkpoint_action(
                    "global_judge_started",
                    action_key,
                    started_payload,
                )
                state.logical_judge_calls += 1
                result: Any = None
                try:
                    result = self.judge.judge_candidates_bounded(
                        request, max_physical_requests=remaining
                    )
                except BoundedJudgeCallError as exc:
                    physical_delta = exc.physical_requests
                    if physical_delta > remaining:
                        raise ValueError(
                            "Global Judge bounded failure exceeded its "
                            "reserved request allowance"
                        ) from exc
                    state.judge_requests += physical_delta
                    blocker = "global_judge_bounded_failure"
                    detail = "{0}: {1}".format(type(exc).__name__, exc)
                    failure_projection = _global_failure_projection(
                        builder=builder,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=physical_delta,
                        physical_request_exact=True,
                    )
                    failed_payload = {
                        **started_payload,
                        "status": "failed",
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": True,
                        "blocker": blocker,
                        "detail": detail,
                        "failure_projection": failure_projection,
                        "provider_state": self._capture_provider_result_state(
                            state
                        ),
                    }
                    self._checkpoint_action(
                        "global_judge_failed",
                        action_key,
                        failed_payload,
                    )
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=physical_delta,
                        candidate_compression=metrics,
                        accounting_already_applied=True,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                except Exception as exc:
                    state.judge_requests += remaining
                    state.judge_request_uncertainty_count += 1
                    blocker = "global_judge_interrupted"
                    detail = (
                        "The Global Judge call ended without exact physical "
                        "request accounting and is treated as interrupted: "
                        "{0}: {1}"
                    ).format(type(exc).__name__, exc)
                    failure_projection = _global_failure_projection(
                        builder=builder,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=remaining,
                        physical_request_exact=False,
                    )
                    failed_payload = {
                        **started_payload,
                        "status": "failed",
                        "physical_request_delta": remaining,
                        "physical_request_exact": False,
                        "blocker": blocker,
                        "detail": detail,
                        "failure_projection": failure_projection,
                        "provider_state": self._capture_provider_result_state(
                            state
                        ),
                    }
                    self._checkpoint_action(
                        "global_judge_failed",
                        action_key,
                        failed_payload,
                    )
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=remaining,
                        physical_request_exact=False,
                        candidate_compression=metrics,
                        accounting_already_applied=True,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                try:
                    if not isinstance(result, BoundedJudgeCallResult):
                        raise TypeError(
                            "global Judge must return BoundedJudgeCallResult"
                        )
                    physical_delta = result.physical_requests
                    if physical_delta > remaining:
                        raise ValueError(
                            "global Judge exceeded its physical request allowance"
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
                except Exception as exc:
                    physical_delta = (
                        result.physical_requests
                        if isinstance(result, BoundedJudgeCallResult)
                        else 0
                    )
                    if physical_delta > remaining:
                        raise ValueError(
                            "Global Judge result exceeded its reserved request "
                            "allowance"
                        ) from exc
                    state.judge_requests += physical_delta
                    blocker = "global_judge_output_invalid"
                    detail = "{0}: {1}".format(type(exc).__name__, exc)
                    failure_projection = _global_failure_projection(
                        builder=builder,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=physical_delta,
                        physical_request_exact=True,
                    )
                    failed_payload = {
                        **started_payload,
                        "status": "failed",
                        "physical_request_delta": physical_delta,
                        "physical_request_exact": True,
                        "blocker": blocker,
                        "detail": detail,
                        "failure_projection": failure_projection,
                        "provider_state": self._capture_provider_result_state(
                            state
                        ),
                    }
                    self._checkpoint_action(
                        "global_judge_failed",
                        action_key,
                        failed_payload,
                    )
                    fail_seed(
                        builder=builder,
                        items=seed_items,
                        blocker=blocker,
                        detail=detail,
                        physical_request_delta=physical_delta,
                        candidate_compression=metrics,
                        accounting_already_applied=True,
                    )
                    terminal_pass_identities.add(pass_identity)
                    continue
                state.judge_requests += physical_delta
                completed_payload = {
                    **started_payload,
                    "status": "completed",
                    "physical_request_delta": physical_delta,
                    "physical_request_exact": True,
                    "judgment": judgment.to_dict(),
                    "provider_state": self._capture_provider_result_state(
                        state
                    ),
                }
                self._checkpoint_action(
                    "global_judge_completed",
                    action_key,
                    completed_payload,
                )
            owner = _global_pass_owner(builder)
            event = {
                "kind": "global_candidate_pass",
                "status": "completed",
                "pass_identity": pass_identity,
                "seed_binding_identity": builder.key,
                "seed_ref": active_seed_ref,
                "defect_fingerprint": builder.defect_state.fingerprint,
                "hypothesis_id": item.hypothesis_id,
                "visit_key": item.visit_key,
                "owner": owner.to_dict(),
                "physical_request_delta": physical_delta,
                "candidate_compression": metrics,
                "candidate_evidence_capsules": [
                    capsule.to_dict() for capsule in capsules
                ],
                "judgment": judgment.to_dict(),
                "behavior_impact": "none_offline_analysis_only",
            }
            state.investigation_journal.append(event)
            terminal_pass_identities.add(pass_identity)
            self._apply_global_candidate_judgment(
                state=state,
                item=item,
                candidates=candidates,
                capsules=capsules,
                judgment=judgment,
                request=request,
            )
            self._checkpoint_state(
                state, "global:after:{0}".format(pass_identity)
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
        sibling_seed_refs = {
            graph.resolve(seed_ref) or seed_ref
            for seed_ref in state.start_refs
        } - active_path
        for candidate in ordered:
            resolved = graph.resolve(candidate.ref) or candidate.ref
            if (
                not graph.active_revision_evidence_eligible(resolved)
                or resolved in sibling_seed_refs
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
                or not authored_root_candidate_eligible(graph, node.ref)
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
            ) or node.event_type == "progress.episode" or not graph.active_revision_evidence_eligible(
                node.ref
            ):
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
                _global_pass_owner(seed_builder),
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
                    or not authored_root_candidate_eligible(
                        state.graph, selected_ref
                    )
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
                enqueued = state.enqueue_confirmation(
                    {
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis_semantic_hash": hypothesis.semantic_hash,
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
                        "status": "queued",
                        "origin": "global_candidate_judgment",
                        "seed_key": seed_builder.key if seed_builder else "",
                        "owner": LocalStateOwner.create(
                            seed_binding_identity=hypothesis.seed_binding_identity,
                            hypothesis_id=hypothesis.hypothesis_id,
                            visit_key=semantic_visit_key(
                                selected_ref,
                                item.defect_state,
                                hypothesis.semantic_hash,
                                hypothesis.seed_binding_identity,
                            ),
                            occurrence_key="confirmation_queue",
                        ).to_dict(),
                    }
                )
                if not enqueued and seed_builder is not None:
                    state._mark_seed_unresolved(
                        selected_ref,
                        "confirmation_enqueue_failed",
                        "The selected candidate could not be queued for independent confirmation.",
                        seed_key=seed_builder.key,
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
            checkpoint_budgets = self.checkpoint_config.get("budgets")
            runtime_budgets = {
                "max_frontier_items": self.max_frontier_items,
                "max_depth": self.max_depth,
                "max_hypotheses": self.max_hypotheses,
                "max_investigation_rounds": self.max_investigation_rounds,
                "max_artifact_bytes": self.max_artifact_bytes,
                "max_judge_requests": self.max_judge_requests,
            }
            for budget_name, runtime_value in runtime_budgets.items():
                checkpoint_value = (
                    checkpoint_budgets.get(budget_name)
                    if isinstance(checkpoint_budgets, Mapping)
                    else None
                )
                if (
                    isinstance(checkpoint_value, bool)
                    or not isinstance(checkpoint_value, int)
                    or checkpoint_value != runtime_value
                ):
                    raise ValueError(
                        "checkpoint budget {0} must match the analyzer "
                        "runtime budget".format(budget_name)
                    )
            self.checkpoint.initialize(self.checkpoint_config)
            restored_checkpoint = self.checkpoint.restore(
                expected_config=self.checkpoint_config
            )
            _validated_global_judge_action_history(
                restored_checkpoint.actions,
                max_judge_requests=int(
                    restored_checkpoint.config["budgets"][
                        "max_judge_requests"
                    ]
                ),
                cache_identity=str(
                    restored_checkpoint.config.get("cache_identity") or ""
                ),
            )
            final_report = restored_checkpoint.final_report
            if final_report is not None:
                validate_modern_report_shape(final_report)
                final_report = _quarantine_stale_seed_report_payload(
                    analysis_graph, final_report
                )
                stale_report_starts = {
                    analysis_graph.resolve(str(item.get("start_ref") or ""))
                    or str(item.get("start_ref") or "")
                    for item in final_report.get("seed_results") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("outcome") or "") == "evidence_gap"
                    and "start_ref_active_revision_ineligible"
                    in (item.get("blocking_reasons") or ())
                }
                analysis_graph.assert_evidence_eligible_references(
                    final_report,
                    label="restored completed report",
                    allowed_ineligible_refs=stale_report_starts,
                )
                report = RecursiveAttributionReport.from_dict(final_report)
                validate_recursive_report_against_graph(
                    analysis_graph,
                    report,
                    label="restored completed report",
                    action_records=restored_checkpoint.actions,
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
                validate_modern_report_shape(pending_report)
                pending_report = _quarantine_stale_seed_report_payload(
                    analysis_graph, pending_report
                )
                stale_report_starts = {
                    analysis_graph.resolve(str(item.get("start_ref") or ""))
                    or str(item.get("start_ref") or "")
                    for item in pending_report.get("seed_results") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("outcome") or "") == "evidence_gap"
                    and "start_ref_active_revision_ineligible"
                    in (item.get("blocking_reasons") or ())
                }
                analysis_graph.assert_evidence_eligible_references(
                    pending_report,
                    label="restored pending report",
                    allowed_ineligible_refs=stale_report_starts,
                )
                report = RecursiveAttributionReport.from_dict(pending_report)
                validate_recursive_report_against_graph(
                    analysis_graph,
                    report,
                    label="restored pending report",
                    action_records=restored_checkpoint.actions,
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
            if (
                current_node is None
                or not analysis_graph.analysis_start_eligible(item.node_ref)
                or not analysis_graph.active_revision_evidence_eligible(
                    item.node_ref
                )
                or any(
                    not analysis_graph.active_revision_evidence_eligible(ref)
                    for ref in item.downstream_path
                )
            ):
                state.complete_unresolved(
                    item,
                    "frontier_node_active_revision_ineligible",
                    "The frontier path contains evidence outside the active repository generation.",
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
                    if analysis_graph.active_revision_evidence_eligible(
                        candidate.ref
                    )
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
                completed_provider_payload = {
                    "call_kind": "step",
                    "visit_key": item.visit_key,
                    "status": "completed",
                    "physical_requests_reserved": reserved_requests,
                    "physical_request_delta": physical_delta,
                    "physical_request_exact": True,
                    "judgment": judgment.to_dict(),
                    "provider_state": self._capture_provider_result_state(
                        state
                    ),
                }
                self._checkpoint_action(
                    "provider_call_completed",
                    provider_action_key,
                    completed_provider_payload,
                )
                completed_provider_record = {
                    "operation": "provider_call_completed",
                    "semantic_key": provider_action_key,
                    "payload": completed_provider_payload,
                }
            elif replay_action is not None:
                self._apply_provider_result_state(state, replay_action["payload"])
                completed_provider_record = replay_action
            else:
                raise ValueError(
                    "completed step judgment has no Provider action"
                )
            step_projection = _step_action_projection_from_record(
                completed_provider_record,
                item=item,
            )
            state.record_completed_step_projection(item, step_projection)
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
                        provider_action_key=provider_action_key,
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
                provider_action_key=provider_action_key,
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
            state._confirmation_queue_key(queued)
            queued_identity = str(queued.get("semantic_identity") or "")
            confirmation_action_key = "confirmation:{0}".format(
                queued_identity
            )
            replay_action = self._replay_action(
                state, confirmation_action_key
            )
            if (
                replay_action is not None
                and replay_action.get("operation") == "confirmation_failed"
            ):
                replay_projection = (
                    _confirmation_action_projection_from_record(
                        replay_action
                    )
                )
                if (
                    replay_projection["request_identity"]
                    != queued_identity
                ):
                    raise ValueError(
                        "failed confirmation replay factual request identity "
                        "does not match the queued request"
                    )
                if (
                    replay_projection["evidence_disposition"]["state"]
                    == "rejected_snapshot"
                ):
                    confirmation = RootConfirmation.from_dict(
                        dict(replay_projection["confirmation"])
                    )
                    self._record_confirmation(
                        state,
                        queued,
                        confirmation,
                        replay_projection,
                    )
                    continue
            terminal_artifact_envelopes = copy.deepcopy(
                list(queued.get("artifact_evidence_envelopes") or ())
            )
            terminal_disposition = _build_terminal_evidence_disposition(
                state.graph,
                terminal_artifact_envelopes,
            )
            if terminal_disposition["state"] == "rejected_snapshot":
                confirmation = RootConfirmation(
                    candidate_ref=str(queued.get("candidate_ref") or ""),
                    status="unknown",
                    counterfactual=confirmation_counterfactual_for(
                        str(queued.get("candidate_ref") or ""), "unknown"
                    ),
                    reason=(
                        "terminal_artifact_preflight_rejected: {0}"
                    ).format(
                        terminal_disposition["rejection_reason"]
                    ),
                    counterfactual_status="unknown",
                    hypothesis_id=str(queued.get("hypothesis_id") or ""),
                    hypothesis_semantic_hash=str(
                        queued.get("hypothesis_semantic_hash") or ""
                    ),
                    defect_fingerprint=str(
                        queued.get("defect_fingerprint") or ""
                    ),
                    recursive_path=tuple(
                        queued.get("recursive_path") or ()
                    ),
                    seed_binding_identity=str(
                        queued.get("seed_binding_identity") or ""
                    ),
                    analysis_perspective=str(
                        queued.get("analysis_perspective") or ""
                    ),
                )
                self._persist_confirmation_action(
                    state,
                    queued,
                    confirmation,
                    operation="confirmation_failed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                    evidence_disposition=terminal_disposition,
                )
                continue
            try:
                request = self._build_confirmation_request(state, queued)
                self._validate_confirmation_request_graph_eligibility(
                    state, request
                )
                preflight_root_confirmation_request(request)
                factual_projection = root_confirmation_request_projection(
                    request
                )
                request_identity = _confirmation_request_identity(request)
                if (
                    str(queued.get("semantic_identity") or "")
                    != request_identity
                    or stable_json(
                        _checkpoint_json(
                            queued.get("factual_request_projection")
                        )
                    )
                    != stable_json(_checkpoint_json(factual_projection))
                ):
                    raise ValueError(
                        "queued confirmation factual request identity changed"
                    )
            except (KeyError, TypeError, ValueError) as exc:
                confirmation = RootConfirmation(
                    candidate_ref=str(queued.get("candidate_ref") or ""),
                    status="unknown",
                    counterfactual=confirmation_counterfactual_for(
                        str(queued.get("candidate_ref") or ""), "unknown"
                    ),
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
                    analysis_perspective=str(
                        queued.get("analysis_perspective") or ""
                    ),
                )
                self._persist_confirmation_action(
                    state,
                    queued,
                    confirmation,
                    operation="confirmation_failed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                    evidence_disposition=terminal_disposition,
                )
                continue

            bounded_judge = isinstance(self.judge, BoundedJudgeCapability)
            offline_judge = isinstance(self.judge, OfflineJudgeCapability)
            if not bounded_judge and not offline_judge:
                confirmation = _synthetic_unknown_confirmation(
                    request,
                    reason="confirmation_budget_unenforceable: Judge has no explicit bounded or offline capability",
                )
                self._persist_confirmation_action(
                    state,
                    queued,
                    confirmation,
                    operation="confirmation_failed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                    evidence_disposition=terminal_disposition,
                )
                continue

            remaining = max(0, self.max_judge_requests - state.judge_requests)
            replayed_confirmation: Optional[RootConfirmation] = None
            replayed_physical_delta = 0
            replayed_physical_exact = True
            replayed_provider_state: Optional[JsonDict] = None
            replayed_projection: Optional[JsonDict] = None
            reserved_requests = 0
            if (
                replay_action is not None
                and replay_action.get("operation") == "confirmation_failed"
            ):
                projection = _confirmation_action_projection_from_record(
                    replay_action
                )
                if projection["request_identity"] != request_identity:
                    raise ValueError(
                        "failed confirmation replay factual request identity "
                        "does not match the rebuilt request"
                    )
                confirmation = RootConfirmation.from_dict(
                    dict(projection["confirmation"])
                )
                self._record_confirmation(
                    state, queued, confirmation, projection
                )
                continue
            if (
                replay_action is not None
                and replay_action.get("operation") == "confirmation_started"
            ):
                replay_payload = _validated_confirmation_started_action(
                    replay_action,
                    request=request,
                    request_identity=request_identity,
                )
                reserved_requests = replay_payload.get(
                    "physical_requests_reserved"
                )
                state.judge_request_uncertainty_count += 1
                if state.judge_requests >= self.max_judge_requests:
                    state._increment_budget("judge_requests")
                confirmation = _synthetic_unknown_confirmation(
                    request,
                    reason="confirmation_interrupted: the prior in-flight confirmation is not repeated",
                )
                self._persist_confirmation_action(
                    state,
                    queued,
                    confirmation,
                    operation="confirmation_failed",
                    physical_requests_reserved=reserved_requests,
                    physical_request_delta=0,
                    physical_request_exact=False,
                    evidence_disposition=terminal_disposition,
                )
                self._checkpoint_state(state, confirmation_action_key)
                continue
            if replay_action is not None and replay_action.get("operation") == "confirmation_completed":
                projection = _confirmation_action_projection_from_record(
                    replay_action
                )
                if projection["request_identity"] != request_identity:
                    raise ValueError(
                        "completed confirmation replay factual request identity "
                        "does not match the rebuilt request"
                    )
                replay_payload = replay_action["payload"]
                replayed_confirmation = RootConfirmation.from_dict(
                    dict(replay_payload.get("confirmation") or {})
                )
                self._validate_terminal_confirmation_for_action(
                    state,
                    queued,
                    replayed_confirmation,
                    projection,
                    label="completed confirmation replay",
                )
                replayed_physical_delta = projection[
                    "physical_request_delta"
                ]
                reserved_requests = projection[
                    "physical_requests_reserved"
                ]
                replayed_physical_exact = projection[
                    "physical_request_exact"
                ]
                replayed_provider_state = (
                    self._prevalidate_completed_replay_provider_state(
                        state,
                        replay_payload,
                        projection,
                    )
                )
                replayed_projection = projection
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
                        "request_identity": request_identity,
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
                confirmation = _synthetic_unknown_confirmation(
                    request,
                    reason="confirmation_failed: {0}: {1}".format(type(exc).__name__, exc),
                )
            except (JudgeProviderError, JudgeProviderUnavailable, TypeError, ValueError) as exc:
                confirmation = _synthetic_unknown_confirmation(
                    request,
                    reason="confirmation_failed: {0}: {1}".format(type(exc).__name__, exc),
                )
            except Exception as exc:
                confirmation = _synthetic_unknown_confirmation(
                    request,
                    reason="confirmation_capability_error: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                )
            if physical_exact:
                state.judge_requests += physical_delta - reserved_requests
            else:
                state.judge_request_uncertainty_count += 1
            terminal_operation = "confirmation_completed"
            final_disposition = terminal_disposition
            if replayed_confirmation is None:
                final_disposition = _build_terminal_evidence_disposition(
                    state.graph,
                    terminal_artifact_envelopes,
                )
                if stable_json(
                    _checkpoint_json(
                        queued.get("artifact_evidence_envelopes") or ()
                    )
                ) != stable_json(
                    _checkpoint_json(terminal_artifact_envelopes)
                ):
                    final_disposition = (
                        _reject_terminal_evidence_disposition(
                            final_disposition,
                            reason=(
                                "queued artifact audit snapshot changed "
                                "after confirmation preflight"
                            ),
                        )
                    )
                queued["artifact_evidence_envelopes"] = copy.deepcopy(
                    terminal_artifact_envelopes
                )
                try:
                    if final_disposition["state"] == "validated":
                        _validate_terminal_confirmation_evidence(
                            state.graph,
                            confirmation=confirmation,
                            artifact_evidence_envelopes=(
                                terminal_artifact_envelopes
                            ),
                            label="provider terminal confirmation",
                        )
                except (TypeError, ValueError) as exc:
                    if terminal_artifact_envelopes:
                        final_disposition = (
                            _reject_terminal_evidence_disposition(
                                final_disposition,
                                reason="{0}: {1}".format(
                                    type(exc).__name__, exc
                                ),
                            )
                        )
                    else:
                        confirmation = _synthetic_unknown_confirmation(
                            request,
                            reason=(
                                "terminal_confirmation_evidence_invalid: "
                                "{0}: {1}"
                            ).format(type(exc).__name__, exc),
                        )
                        terminal_operation = "confirmation_failed"
                if final_disposition["state"] == "rejected_snapshot":
                    confirmation = _synthetic_unknown_confirmation(
                        request,
                        reason=(
                            "terminal_artifact_preflight_rejected: {0}"
                        ).format(final_disposition["rejection_reason"]),
                    )
                    terminal_operation = "confirmation_failed"
            if replayed_confirmation is None:
                self._persist_confirmation_action(
                    state,
                    queued,
                    confirmation,
                    operation=terminal_operation,
                    physical_requests_reserved=reserved_requests,
                    physical_request_delta=physical_delta,
                    physical_request_exact=physical_exact,
                    provider_state=self._capture_provider_result_state(state),
                    evidence_disposition=final_disposition,
                )
            elif replayed_provider_state is not None:
                self._apply_validated_provider_result_state(
                    state,
                    replayed_provider_state,
                )
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
            if (
                replayed_confirmation is not None
                and replayed_projection is not None
            ):
                self._record_validated_confirmation(
                    state,
                    queued,
                    replayed_confirmation,
                    replayed_projection,
                )
            if (
                confirmation.status == "rejected"
                and is_definitive_confirmation(confirmation)
            ):
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
                if not is_definitive_confirmation(target):
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

    @staticmethod
    def _confirmation_hypothesis_status_at_request(
        state: RecursiveAnalysisState,
        *,
        request_hypothesis_id: str,
        competitor: Mapping[str, Any],
    ) -> str:
        positions: Dict[str, int] = {}
        for index, entry in enumerate(state.confirmation_journal):
            if not isinstance(entry, Mapping):
                continue
            hypothesis_id = str(entry.get("hypothesis_id") or "")
            if not hypothesis_id:
                continue
            if hypothesis_id in positions:
                raise ValueError(
                    "confirmation journal contains a duplicate hypothesis action"
                )
            positions[hypothesis_id] = index
        request_position = positions.get(
            request_hypothesis_id,
            len(state.confirmation_journal),
        )
        competitor_hypothesis_id = str(
            competitor.get("hypothesis_id") or ""
        )
        competitor_position = positions.get(competitor_hypothesis_id)
        status = str(competitor.get("status") or "")
        if (
            status == "rejected"
            and competitor_position is not None
            and competitor_position >= request_position
        ):
            status = (
                "supported"
                if competitor.get("supporting_evidence")
                else "active"
            )
        return status

    @staticmethod
    def _validate_confirmation_request_graph_eligibility(
        state: RecursiveAnalysisState,
        request: RootConfirmationRequest,
    ) -> None:
        for upstream, downstream in zip(
            request.recursive_path, request.recursive_path[1:]
        ):
            edges = state.graph.edge_context(upstream, downstream)
            if not any(
                is_confirmation_causal_edge(edge, default_eligible=True)
                for edge in edges
            ):
                raise ValueError(
                    "queued confirmation path lacks a grounded non-temporal "
                    "edge: {0}->{1}".format(upstream, downstream)
                )

    @staticmethod
    def _build_confirmation_request(
        state: RecursiveAnalysisState, queued: Mapping[str, Any]
    ) -> RootConfirmationRequest:
        if "artifact_evidence_envelopes" not in queued:
            queue_key = state._confirmation_queue_key(queued)
            stored = [
                item
                for item in state.confirmation_queue
                if isinstance(item, Mapping)
                and state._confirmation_queue_key(item) == queue_key
            ]
            if len(stored) == 1:
                queued = stored[0]
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        candidate_ref = str(queued.get("candidate_ref") or "")
        fingerprint = str(queued.get("defect_fingerprint") or "")
        seed_binding_identity = str(queued.get("seed_binding_identity") or "")
        hypothesis = state.ledger.get(hypothesis_id)
        if (
            hypothesis.candidate_root_ref != candidate_ref
            or hypothesis.active_defect_fingerprint != fingerprint
            or hypothesis.seed_binding_identity != seed_binding_identity
            or str(queued.get("hypothesis_semantic_hash") or "")
            != hypothesis.semantic_hash
        ):
            raise ValueError("queued confirmation is cross-bound to another hypothesis")
        analysis_perspective = str(
            queued.get("analysis_perspective") or ""
        )
        if analysis_perspective != state.analysis_perspective:
            raise ValueError(
                "queued confirmation analysis perspective contradicts its run"
            )
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
        if (
            node is None
            or not authored_root_candidate_eligible(
                state.graph, candidate_ref
            )
        ):
            raise ValueError(
                "queued confirmation candidate is ineligible for the active revision"
            )
        path = tuple(str(ref) for ref in queued.get("recursive_path") or ())
        if not path or path[0] != candidate_ref:
            raise ValueError("queued confirmation path is not candidate-rooted")
        if any(state.graph.resolve(ref) != ref for ref in path):
            raise ValueError("queued confirmation path contains unresolved references")
        if any(
            not state.graph.active_revision_evidence_eligible(ref)
            for ref in path
        ):
            raise ValueError(
                "queued confirmation path contains evidence ineligible for the active revision"
            )
        candidate_reference = _reference_envelope(
            candidate_ref,
            content=_node_semantic_content(
                state.graph, state.graph.hydrate_node(candidate_ref)
            ),
            fact_kind="candidate_fact",
        )
        candidate_reference["decisive"] = True
        artifact_manifest = _artifact_hydration_manifest(
            state.graph,
            state.graph.hydrate_node(candidate_ref),
        )
        path_references = tuple(_reference_envelope(ref) for ref in path)
        queued_artifact_envelopes = {
            str(item.get("canonical_ref") or ""): copy.deepcopy(dict(item))
            for item in queued.get("artifact_evidence_envelopes") or ()
            if isinstance(item, Mapping)
        }

        def evidence_facts(refs: Iterable[str], fact_kind: str) -> Tuple[JsonDict, ...]:
            output: List[JsonDict] = []
            for raw_ref in _dedupe_strings(refs):
                resolved = state.graph.resolve(raw_ref)
                if resolved in state.graph.nodes:
                    if not state.graph.active_revision_evidence_eligible(
                        resolved
                    ):
                        raise ValueError(
                            "confirmation evidence ref is ineligible for the active revision: {0}".format(
                                raw_ref
                            )
                        )
                    output.append(
                        _reference_envelope(
                            resolved,
                            content=_node_semantic_content(
                                state.graph,
                                state.graph.hydrate_node(resolved),
                            ),
                            fact_kind=fact_kind,
                        )
                    )
                    continue
                canonical_ref = "artifact:{0}".format(
                    raw_ref.removeprefix("artifact:")
                )
                envelope = queued_artifact_envelopes.get(canonical_ref)
                if envelope is None:
                    raise ValueError(
                        "confirmation artifact evidence has no persisted "
                        "owner envelope: {0}".format(
                            raw_ref
                        )
                    )
                owner_ref = str(
                    envelope.get("owner_reference", {}).get(
                        "resolved_ref"
                    )
                    if isinstance(
                        envelope.get("owner_reference"), Mapping
                    )
                    else ""
                )
                canonical = state.graph.validate_artifact_evidence_envelope(
                    envelope,
                    expected_owner_ref=owner_ref,
                )
                if canonical.get("fact_kind") != fact_kind:
                    canonical = state.graph.artifact_evidence_envelope(
                        raw_ref,
                        fact_kind=fact_kind,
                        expected_owner_ref=owner_ref,
                    )
                output.append(canonical)
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
            competitor_status = (
                AgenticRecursiveAnalyzer._confirmation_hypothesis_status_at_request(
                    state,
                    request_hypothesis_id=hypothesis_id,
                    competitor=value,
                )
            )

            def competitor_evidence(items: Any, kind: str) -> List[JsonDict]:
                output: List[JsonDict] = []
                for item in items if isinstance(items, (list, tuple)) else ():
                    if not isinstance(item, Mapping):
                        continue
                    ref = str(item.get("ref") or "")
                    resolved_ref = state.graph.resolve(ref)
                    if not resolved_ref or resolved_ref not in state.graph.nodes:
                        raise ValueError("competing hypothesis evidence is unresolved")
                    if not state.graph.active_revision_evidence_eligible(
                        resolved_ref
                    ):
                        raise ValueError(
                            "competing hypothesis evidence is ineligible for the active revision"
                        )
                    output.append(
                        {
                            "reason": str(item.get("reason") or ""),
                            "confidence": float(item.get("confidence", 0.0)),
                            "evidence_reference": _reference_envelope(
                                resolved_ref,
                                content=_node_semantic_content(
                                    state.graph,
                                    state.graph.hydrate_node(resolved_ref),
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
                or not authored_root_candidate_eligible(
                    state.graph, resolved
                )
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
            if any(
                not state.graph.active_revision_evidence_eligible(ref)
                for ref in competitor_path
            ):
                continue
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
                    "status": competitor_status or "unresolved",
                    "claim": str(value.get("claim") or ""),
                    "active_defect": competitor_defect.to_dict(),
                    "candidate_reference": _reference_envelope(
                        resolved,
                        content=_node_semantic_content(
                            state.graph, state.graph.hydrate_node(resolved)
                        ),
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

        candidate_reference = state.graph.sanitize_judge_visible_payload(
            candidate_reference
        )
        if artifact_manifest is not None:
            candidate_reference["artifact_hydration"] = artifact_manifest
        path_references = tuple(
            state.graph.sanitize_judge_visible_payload(path_references)
        )
        supporting_evidence = tuple(
            state.graph.sanitize_judge_visible_payload(
                evidence_facts(supporting_refs, "supporting_evidence")
            )
        )
        opposing_evidence = tuple(
            state.graph.sanitize_judge_visible_payload(
                evidence_facts(opposing_refs, "opposing_evidence")
            )
        )
        competitors = list(
            state.graph.sanitize_judge_visible_payload(competitors)
        )
        obligations = list(
            state.graph.sanitize_judge_visible_payload(obligations)
        )
        return RootConfirmationRequest(
            candidate_ref=candidate_ref,
            defect_state=defect_state,
            recursive_path=path,
            candidate_reference=candidate_reference,
            recursive_path_references=path_references,
            supporting_evidence=supporting_evidence,
            opposing_evidence=opposing_evidence,
            competing_hypotheses=tuple(competitors),
            task_obligations=tuple(obligations),
            analysis_perspective=analysis_perspective,
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis.semantic_hash,
            seed_binding_identity=seed_binding_identity,
        )

    def _record_confirmation(
        self,
        state: RecursiveAnalysisState,
        queued: JsonDict,
        confirmation: RootConfirmation,
        action_projection: Mapping[str, Any],
    ) -> None:
        projection = self._validate_terminal_confirmation_for_action(
            state,
            queued,
            confirmation,
            action_projection,
            label="confirmation",
        )
        self._record_validated_confirmation(
            state,
            queued,
            confirmation,
            projection,
        )

    def _record_validated_confirmation(
        self,
        state: RecursiveAnalysisState,
        queued: JsonDict,
        confirmation: RootConfirmation,
        projection: JsonDict,
    ) -> None:
        node = state.graph.nodes[confirmation.candidate_ref]
        queued["status"] = confirmation.status
        queued["confirmation"] = confirmation.to_dict()
        queued["response_identity"] = confirmation.response_identity
        queued["evidence_disposition"] = copy.deepcopy(
            projection["evidence_disposition"]
        )
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        seed_key = str(
            queued.get("seed_key")
            or state.hypothesis_seed_keys.get(hypothesis_id, "")
        )
        seed_builder = state.seed_ledger.get(seed_key)
        owner = LocalStateOwner.from_dict(queued.get("owner"))
        if seed_builder is not None:
            seed_builder.record_confirmation(confirmation, owner)
        state.confirmations.append(confirmation)
        state.confirmation_journal.append(
            {
                "semantic_identity": queued.get("semantic_identity"),
                "candidate_ref": confirmation.candidate_ref,
                "hypothesis_id": hypothesis_id,
                "defect_fingerprint": confirmation.defect_fingerprint,
                "seed_binding_identity": confirmation.seed_binding_identity,
                "seed_key": seed_key,
                "owner": owner.to_dict(),
                "recursive_path": list(confirmation.recursive_path),
                "status": confirmation.status,
                "action_operation": projection["operation"],
                "semantic_key": projection["semantic_key"],
                "response_identity": projection["response_identity"],
                "physical_requests_reserved": projection[
                    "physical_requests_reserved"
                ],
                "physical_request_delta": projection[
                    "physical_request_delta"
                ],
                "physical_request_exact": projection[
                    "physical_request_exact"
                ],
                "artifact_evidence_envelopes": copy.deepcopy(
                    projection["artifact_evidence_envelopes"]
                ),
                "evidence_disposition": copy.deepcopy(
                    projection["evidence_disposition"]
                ),
                "factual_request_projection": copy.deepcopy(
                    projection["factual_request_projection"]
                ),
                "confirmation": confirmation.to_dict(),
            }
        )
        state.confirmation_action_projection.append(projection)
        if confirmation.status == "confirmed":
            state.introduction_hypothesis_ids.discard(hypothesis_id)
            state.unresolved_hypothesis_ids.discard(hypothesis_id)
            defect_state = state.defect_states[confirmation.defect_fingerprint]
            if seed_builder is None:
                raise ValueError(
                    "confirmed root publication has no owning seed builder"
                )
            state.confirmed_roots.append(
                canonical_confirmed_root_publication(
                    confirmation=confirmation,
                    defect_state=defect_state,
                    candidate_node=node,
                    seed_start_ref=seed_builder.start_ref,
                )
            )
            state.refresh_pending_confirmation_request_identities()
            return
        if not is_definitive_confirmation(confirmation):
            state.unresolved_hypothesis_ids.add(hypothesis_id)
            if confirmation.candidate_ref not in state.unresolved_refs:
                state.unresolved_refs.append(confirmation.candidate_ref)
            reason = (
                "root_confirmation_unknown"
                if confirmation.status == "unknown"
                else "root_confirmation_unresolved"
            )
            state.unresolved_branches.append(
                {
                    "node_ref": confirmation.candidate_ref,
                    "defect_state_id": "defect:{0}".format(
                        confirmation.defect_fingerprint
                    ),
                    "hypothesis_id": hypothesis_id,
                    "reason": reason,
                    "details": confirmation.reason,
                    "depth": max(0, len(confirmation.recursive_path) - 1),
                }
            )
            state.refresh_pending_confirmation_request_identities()
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
                factor = canonical_causal_factor_publication(
                    confirmation=confirmation,
                    analysis_perspective=state.analysis_perspective,
                )
                if confirmation.factor_role == "amplifying_factor":
                    state.amplifying_factors.append(factor)
                else:
                    state.contributing_conditions.append(factor)
            else:
                state.rejected_candidates.append(
                    canonical_rejected_candidate_publication(confirmation)
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
            state.refresh_pending_confirmation_request_identities()
            return

    def _rank_confirmed_roots(self, state: RecursiveAnalysisState) -> None:
        roots = (*state.confirmed_roots, *state.co_roots)
        primary_roots, co_roots = canonical_ranked_root_publications(
            roots,
        )
        state.confirmed_roots = list(primary_roots)
        state.co_roots = list(co_roots)

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
        replay_operation = ""
        replayed_result: Optional[InvestigationResult] = None
        if replay_action is not None:
            (
                replay_operation,
                _replay_payload,
                replayed_result,
            ) = _validated_investigation_replay_action(
                replay_action,
                action_key=investigation_action_key,
                directive=directive,
                item=item,
            )
        if replay_operation in {
            "investigation_started",
            "investigation_failed",
        }:
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
            if replay_operation == "investigation_started":
                self._checkpoint_action(
                    "investigation_failed",
                    investigation_action_key,
                    {
                        "directive_id": directive.directive_id,
                        "visit_key": item.visit_key,
                        "status": "unknown",
                        "reason": "interrupted_investigation_call",
                    },
                )
            return "completed"
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
                    "visit_key": item.visit_key,
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
                enqueued = state.enqueue_confirmation(
                    {
                        "hypothesis_id": hypothesis_id,
                        "hypothesis_semantic_hash": hypothesis.semantic_hash,
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
                        "status": "queued",
                        "seed_key": state.hypothesis_seed_keys.get(
                            hypothesis_id, ""
                        ),
                        "owner": _owner_for_item(
                            item, "confirmation_queue"
                        ).to_dict(),
                    }
                )
                if not enqueued:
                    state._mark_ref_unresolved(
                        item.node_ref,
                        item,
                        "confirmation_enqueue_failed",
                        "The active candidate could not be queued for independent confirmation.",
                    )
                    raise ValueError(
                        "confirmation enqueue failed for the active seed"
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
        state.refresh_pending_confirmation_request_identities()
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
