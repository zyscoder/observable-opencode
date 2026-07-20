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
from .causal_retrieval import SemanticPredecessorRetriever
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
    confirmation_identity_for,
)
from .checkpoint import CheckpointBundle, CheckpointState
from .errors import JudgeProviderError, JudgeProviderUnavailable
from .graph import TraceGraph
from .hypotheses import HypothesisLedger, RecursiveFrontier
from .investigation import (
    AttributionControlDirective,
    CausalInvestigationTools,
    InvestigationDirective,
    InvestigationResult,
)
from .judgment_context import build_recursive_judgment_context
from .models import JsonDict, TraceNode, stable_json


RECURSIVE_RELATIONS = frozenset({"same_defect_propagation", "defect_transformation"})
EVALUATION_START_EVENTS = frozenset(
    {"case.observed_defect", "case.quality_gap", "case.missing_semantic"}
)
FRONTIER_STATE_SCHEMA = "recursive-analysis-frontier/v1"
HYPOTHESIS_STATE_SCHEMA = "recursive-analysis-hypotheses/v1"
ACTION_STATE_SCHEMA = "recursive-analysis-actions/v2"
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
    introduction_binding_keys: Set[Tuple[str, str, str]] = field(default_factory=set)
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
    confirmation_queue_keys: Set[Tuple[str, str, str]] = field(default_factory=set)
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
                state._mark_seed_unresolved(start_ref, "start_ref_unresolved", "The start reference is absent.")
                continue
            defect_state = _seed_defect_state(node, objective)
            state._remember_defect(defect_state)
            state.seed_count += 1
            if node.event_type in EVALUATION_START_EVENTS:
                predecessors = graph.upstream_refs(start_ref)
                if not predecessors:
                    state._mark_seed_unresolved(
                        start_ref,
                        "outcome_evidence_missing",
                        "The evaluation assertion has no concrete outcome evidence.",
                    )
                    continue
                for predecessor_ref in predecessors:
                    if len(state.ledger.snapshot()) >= max_hypotheses:
                        state._increment_budget("hypotheses")
                        state._mark_seed_unresolved(
                            predecessor_ref,
                            "hypothesis_limit",
                            "The seed hypothesis budget is exhausted.",
                        )
                        continue
                    hypothesis = state.ledger.create(
                        "{0} is upstream outcome evidence for {1}.".format(
                            predecessor_ref, start_ref
                        ),
                        predecessor_ref,
                        defect_state,
                    )
                    item = FrontierItem.create(
                        node_ref=predecessor_ref,
                        defect_state=defect_state,
                        downstream_path=[predecessor_ref, start_ref],
                        hypothesis_id=hypothesis.hypothesis_id,
                        hypothesis_semantic_hash=hypothesis.semantic_hash,
                        candidate_source="outcome_evidence",
                        priority=1.0,
                        checked_evidence_refs=[start_ref],
                        graph_position=graph.position(predecessor_ref),
                    )
                    state.frontier.push(item)
                    state._merge_visit_evidence(item.visit_key, [start_ref])
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
                continue
            if len(state.ledger.snapshot()) >= max_hypotheses:
                state._increment_budget("hypotheses")
                state._mark_seed_unresolved(
                    start_ref, "hypothesis_limit", "The seed hypothesis budget is exhausted."
                )
                continue
            hypothesis = state.ledger.create(
                "Investigate the observed defect at {0}.".format(start_ref),
                start_ref,
                defect_state,
            )
            item = FrontierItem.create(
                node_ref=start_ref,
                defect_state=defect_state,
                downstream_path=[start_ref],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
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
        if frontier_payload["schema"] != FRONTIER_STATE_SCHEMA:
            raise ValueError("unsupported recursive frontier state schema")
        if hypothesis_payload["schema"] != HYPOTHESIS_STATE_SCHEMA:
            raise ValueError("unsupported recursive hypothesis state schema")
        if action_payload["schema"] != ACTION_STATE_SCHEMA:
            raise ValueError("unsupported recursive action state schema")

        state = cls(
            graph=graph,
            start_refs=tuple(str(item) for item in action_payload["start_refs"]),
            objective=str(action_payload["objective"]),
            analysis_perspective=str(action_payload["analysis_perspective"]),
            ledger=HypothesisLedger.from_snapshot(hypothesis_payload["hypotheses"]),
            frontier=RecursiveFrontier.from_checkpoint(frontier_payload["frontier"]),
        )
        state.visit_evidence = {
            str(key): {str(item) for item in values}
            for key, values in dict(frontier_payload["visit_evidence"]).items()
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
        state.causal_candidates = [CausalCandidate.from_dict(item) for item in action_payload["causal_candidates"]]
        state.causal_relations = [PredecessorAssessment.from_dict(item) for item in action_payload["causal_relations"]]
        state.step_judgments = [CausalStepJudgment.from_dict(item) for item in action_payload["step_judgments"]]
        state.introduction_candidates = [CausalCandidate.from_dict(item) for item in action_payload["introduction_candidates"]]
        state.introduction_bindings = copy.deepcopy(action_payload["introduction_bindings"])
        state.introduction_binding_keys = {
            (
                str(item.get("candidate_ref") or ""),
                str(item.get("defect_fingerprint") or ""),
                str(item.get("hypothesis_semantic_hash") or ""),
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
        state.investigation_journal = copy.deepcopy(action_payload["investigation_journal"])
        state.investigation_evidence = copy.deepcopy(action_payload["investigation_evidence"])
        state.investigation_evidence_hashes = {
            str(key): {str(item) for item in values}
            for key, values in dict(action_payload["investigation_evidence_hashes"]).items()
        }
        state.control_directive_ids = {str(item) for item in action_payload["control_directive_ids"]}
        state.confirmation_queue = copy.deepcopy(action_payload["confirmation_queue"])
        state.confirmation_queue_keys = {
            tuple(str(part) for part in item) for item in action_payload["confirmation_queue_keys"]
        }
        state.confirmations = [RootConfirmation.from_dict(item) for item in action_payload["confirmations"]]
        state.confirmed_roots = [ConfirmedRoot.from_dict(item) for item in action_payload["confirmed_roots"]]
        state.co_roots = [ConfirmedRoot.from_dict(item) for item in action_payload["co_roots"]]
        state.amplifying_factors = [CausalFactor.from_dict(item) for item in action_payload["amplifying_factors"]]
        state.confirmation_journal = copy.deepcopy(action_payload["confirmation_journal"])
        state.pending_rejudge_journal = {
            str(key): [int(item) for item in values]
            for key, values in dict(action_payload["pending_rejudge_journal"]).items()
        }
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
        state.replay_actions = checkpoint.latest_actions
        return state

    def build_step_request(
        self,
        graph: TraceGraph,
        item: FrontierItem,
        candidates: Sequence[CausalCandidate],
    ) -> CausalStepRequest:
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
        investigated = self.investigation_evidence.get(item.visit_key, [])
        if investigated:
            context["investigation_evidence"] = copy.deepcopy(investigated)
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
            candidates=tuple(candidates),
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
        if is_present:
            self.present_hypothesis_ids.add(item.hypothesis_id)

        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            details = "; ".join(judgment.missing_evidence) or judgment.current_defect_reason
            self.mark_unresolved(item, "judge_unknown", details)
        elif is_present and judgment.candidate_introduction:
            if hypothesis.candidate_root_ref != item.node_ref:
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
                        self.introduction_candidates.append(candidate)
                        self._remember_candidate(candidate)
                        self.introduction_bindings.append(
                            {
                                "candidate_ref": item.node_ref,
                                "defect_state_id": item.defect_state.defect_state_id,
                                "defect_fingerprint": item.defect_state.fingerprint,
                                "hypothesis_id": hypothesis.hypothesis_id,
                                "hypothesis_semantic_hash": hypothesis.semantic_hash,
                            }
                        )
                        self.introduction_binding_keys.add(binding_key)
                self.introduction_hypothesis_ids.add(item.hypothesis_id)

        declared_recursive = False
        for assessment in judgment.predecessors:
            self.causal_relations.append(assessment)
            if assessment.relation == "contributing_condition":
                continue
            if assessment.relation in {"unrelated", "unknown"}:
                if assessment.relation == "unrelated":
                    self.ledger.add_opposition(
                        item.hypothesis_id,
                        assessment.ref,
                        assessment.reason,
                        assessment.confidence,
                    )
                else:
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
            if assessment.missing_evidence or not assessment.evidence_refs:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_evidence_missing",
                    "; ".join(assessment.missing_evidence)
                    or "The recursive predecessor has no grounded supporting evidence.",
                )
                continue
            if judgment.current_defect_status != "present":
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "unsupported_propagation",
                    "A predecessor cannot carry a defect when the current defect is not present.",
                )
                continue

            upstream_defect = item.defect_state
            if assessment.relation == "defect_transformation":
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
                claim, assessment.ref, upstream_defect
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
        hypotheses = [AttributionHypothesis.from_dict(item) for item in self.ledger.snapshot()]
        by_id = {item.hypothesis_id: item for item in hypotheses}
        unresolved_ids = self.unresolved_hypothesis_ids | self.introduction_hypothesis_ids
        if self.present_hypothesis_ids and not unresolved_ids and not (
            self.confirmed_roots or self.co_roots
        ):
            unresolved_ids.update(self.present_hypothesis_ids)
            self.unresolved_hypothesis_ids.update(self.present_hypothesis_ids)
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
        metadata = {
            "analysis": "agentic_recursive_semantic_taint",
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
        }
        return RecursiveAttributionReport(
            case_id=self.graph.case_id,
            objective=self.objective,
            start_refs=self.start_refs,
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

    def _candidate_for_ref(
        self,
        ref: str,
        *,
        source: str,
        edge: Mapping[str, Any],
        evidence_refs: Tuple[str, ...],
    ) -> Optional[CausalCandidate]:
        node = self.graph.nodes.get(ref)
        if node is None:
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
        self.causal_candidates.append(candidate)

    def _remember_defect(self, defect_state: DefectState) -> None:
        self.defect_states.setdefault(defect_state.fingerprint, defect_state)
        self.transformation_chains.setdefault(defect_state.fingerprint, (defect_state,))

    def _merge_visit_evidence(self, visit_key: str, refs: Iterable[str]) -> None:
        self.visit_evidence.setdefault(visit_key, set()).update(str(ref) for ref in refs if ref)

    def _increment_budget(self, name: str) -> None:
        self.exhausted_budgets[name] = self.exhausted_budgets.get(name, 0) + 1

    def _mark_seed_unresolved(self, ref: str, reason: str, details: str) -> None:
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
                report = RecursiveAttributionReport.from_dict(final_report)
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
                report = RecursiveAttributionReport.from_dict(pending_report)
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
                candidates = self.retriever.retrieve(
                    analysis_graph,
                    item.node_ref,
                    item.defect_state,
                    state.ledger.get(item.hypothesis_id),
                    allow_semantic_fallback=True,
                )
            except Exception as exc:
                detail = "{0}: {1}".format(type(exc).__name__, exc)
                state.complete_rejudge(
                    item, terminal_state="retrieval_error", detail=detail
                )
                state.complete_unresolved(item, "retrieval_error", detail)
                continue
            for candidate in candidates:
                state._remember_candidate(candidate)
            try:
                request = state.build_step_request(analysis_graph, item, candidates)
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
        state.confirmed_roots = retained

    def _build_confirmation_request(
        self, state: RecursiveAnalysisState, queued: Mapping[str, Any]
    ) -> RootConfirmationRequest:
        hypothesis_id = str(queued.get("hypothesis_id") or "")
        candidate_ref = str(queued.get("candidate_ref") or "")
        fingerprint = str(queued.get("defect_fingerprint") or "")
        hypothesis = state.ledger.get(hypothesis_id)
        if (
            hypothesis.candidate_root_ref != candidate_ref
            or hypothesis.active_defect_fingerprint != fingerprint
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
                bool(edge.get("eligible_for_attribution", True))
                and str(edge.get("relation") or "")
                not in {"temporal_proximity", "temporal_sequence"}
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
            competitor_ref = str(value.get("candidate_root_ref") or "")
            resolved = state.graph.resolve(competitor_ref)
            if not resolved:
                raise ValueError("competing hypothesis candidate is unresolved")
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

            competitor_fingerprint = str(value.get("active_defect_fingerprint") or "")
            competitor_defect = state.defect_states.get(competitor_fingerprint)
            if competitor_defect is None:
                raise ValueError("competing hypothesis defect is unresolved")
            competitor_hypothesis_id = str(value.get("hypothesis_id") or "")
            competitor_semantic_hash = str(value.get("semantic_hash") or "")
            queued_competitor = next(
                (
                    item
                    for item in state.confirmation_queue
                    if str(item.get("hypothesis_id") or "") == competitor_hypothesis_id
                    and str(item.get("candidate_ref") or "") == resolved
                    and str(item.get("defect_fingerprint") or "")
                    == competitor_fingerprint
                ),
                None,
            )
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
            )
            competitors.append(
                {
                    "hypothesis_id": competitor_hypothesis_id,
                    "hypothesis_semantic_hash": competitor_semantic_hash,
                    "confirmation_identity": competitor_confirmation_identity,
                    "recursive_path": list(competitor_path),
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
        state.confirmations.append(confirmation)
        state.confirmation_journal.append(
            {
                "semantic_identity": queued.get("semantic_identity"),
                "candidate_ref": confirmation.candidate_ref,
                "hypothesis_id": hypothesis_id,
                "defect_fingerprint": confirmation.defect_fingerprint,
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
                    observed_defect_refs=state.start_refs,
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
                    str(arguments["claim"]), resolved, item.defect_state
                )
                existing_ids = {
                    str(value.get("hypothesis_id") or "") for value in before
                }
                if (
                    proposed.hypothesis_id not in existing_ids
                    and len(before) >= self.max_hypotheses
                ):
                    raise ValueError("hypothesis budget exhausted")
                state.ledger.create(
                    str(arguments["claim"]), resolved, item.defect_state
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
                    for binding in state.introduction_bindings
                )
                if not binding_exists:
                    raise ValueError("confirmation requires an existing introduction binding")
                queue_key = (hypothesis_id, candidate_ref, defect_fingerprint)
                if queue_key not in state.confirmation_queue_keys:
                    state.confirmation_queue_keys.add(queue_key)
                    state.confirmation_queue.append(
                        {
                            "hypothesis_id": hypothesis_id,
                            "candidate_ref": candidate_ref,
                            "defect_fingerprint": defect_fingerprint,
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


__all__ = ["AgenticRecursiveAnalyzer", "RecursiveAnalysisState"]
