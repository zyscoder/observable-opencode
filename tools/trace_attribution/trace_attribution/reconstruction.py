from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .artifact_reader import VerifiedArtifactReader
from .evaluation_facts import trace_execution_revision
from .models import JsonDict, TraceNode, stable_json
from .restoration_obligation import (
    OBLIGATION_GAP_RECONSTRUCTION_RULE,
    ObligationGapCandidate,
    canonical_fact_key,
    evaluation_only_fact_key,
    normalize_obligation_text,
    scrub_evaluation_only_fact_value,
)


MIN_MATCH_CHARS = 48
MAX_MATCHED_DECISIONS_PER_REQUEST = 24
MAX_DECISION_CANDIDATES_PER_REQUEST = 96
MAX_ARTIFACT_CHARS = 2_000_000
_OBLIGATION_DATA_KEYS = (
    "task_obligations",
    "interface_obligations",
    "restoration_obligations",
    "obligations",
)
_OBLIGATION_SOURCE_EVENTS = frozenset(
    {
        "message.input",
        "prompt.assembly",
        "request.received",
        "task.obligation",
        "interface.obligation",
        "evidence.semantic_fact",
        "tool.result",
    }
)
_NATURAL_OBLIGATION_SOURCE_EVENTS = frozenset(
    {
        "message.input",
        "prompt.assembly",
        "request.received",
        "task.obligation",
        "interface.obligation",
    }
)
_NATURAL_CONTRACT_FIELDS = frozenset(
    {
        "acceptance_criteria",
        "content",
        "contract",
        "objective",
        "requirement",
        "requirements",
        "task_contract",
        "text",
    }
)
_ACTION_EVENTS = frozenset({"change"})
_VERIFICATION_EVENTS = frozenset({"verification", "case.result", "tool.result"})
_FAILURE_EVENTS = frozenset(
    {"verification", "case.result", "case.failed", "tool.error", "tool.result"}
)
_EXCLUSION_MARKERS = (
    "exclude",
    "excluded",
    "skip",
    "omit",
    "omitted",
    "out of scope",
    "not needed",
    "not required",
    "probably omit",
    "probably skip",
    "won't cover",
    "will not cover",
)
_CLOSURE_MARKERS = (
    "complete",
    "completed",
    "done",
    "sufficient",
    "scope",
    "scoped",
    "only",
    "close",
    "closed",
)
_BANNED_PROVENANCE_TERMS = (
    "evaluation",
    "review",
    "reviewer",
    "human",
    "ground_truth",
    "benchmark_label",
)
_NEGATION_PATTERNS = (
    r"\bdid\s+not\b",
    r"\bdoes\s+not\b",
    r"\bdo\s+not\b",
    r"\bnot\s+(?:implement|implemented|restore|restored|verify|verified|run)\b",
    r"\bnever\b",
    r"\bskip(?:ped)?\b",
    r"\bomit(?:ted)?\b",
    r"\bexclude(?:d)?\b",
    r"\bwithout\b",
    r"\bfailed\s+to\b",
    r"\bunverified\b",
    r"\bnot\s+required\b",
    r"\bneed\s+not\b",
    r"\bno\s+longer\b",
)
_CURRENT_SCOPE_NEGATION_PATTERNS = (
    *_NEGATION_PATTERNS,
    r"\bnot\s+now\b",
)
_VERIFICATION_NEGATION_PATTERNS = (
    *_NEGATION_PATTERNS,
    r"\btests?\s+(?:were\s+)?not\s+run\b",
    r"\bnot\s+tested\b",
)
_FAILURE_SIGNATURE_SCHEMAS = frozenset(
    {
        "obligation-failure-signature/v1",
        "evaluation-failure-signature/v1",
    }
)
_NATURAL_DEONTIC_PATTERNS = (
    r"\bmust\b",
    r"\bshall\b",
    r"\bshould\b",
    r"\brequires?\b",
    r"\bis\s+required\b",
    r"\bare\s+required\b",
    r"\bneeds?\s+to\b",
    r"\b(?:ensure|implement|maintain|preserve|restore)\b",
)
_NATURAL_CURRENT_SCOPE_PATTERNS = (
    r"\bcurrent\s+task\b",
    r"\bthis\s+request\b",
    r"\bnow\b",
    r"此次需求",
)
_NATURAL_NON_CURRENT_PATTERNS = (
    r"\bexample\b",
    r"\bfor\s+example\b",
    r"\be\.g\.\b",
    r"\bhistorical\b",
    r"\bhistorically\b",
    r"\bpreviously\b",
    r"\bformerly\b",
    r"\bused\s+to\b",
    r"\bwas\s+required\b",
    r"\bwere\s+required\b",
    r"\bhad\s+to\b",
    r"\bfuture\b",
    r"\bwill\b",
    r"\bwould\b",
    r"\bgoing\s+to\b",
    r"\bplans?\s+to\b",
    r"\bcould\b",
    r"\bmight\b",
    r"\bperhaps\b",
    r"\bspeculative\b",
    r"\balternative\b",
    r"\bversioned\b",
    r"\b(?:version|release)[\s_-]*"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    r"\bretired\b",
    r"\barchived?\b",
    r"\bnext\s+(?:version|release)\b",
    r"\bafter\s+(?:the\s+)?planned\s+migration\b",
    r"\bpost[\s_-]*migration\b",
)
_FORMAL_FAILURE_BINDING_KEYS = (
    "binding_type",
    "obligation_id",
    "decision_ref",
    "subsystem",
    "affected_subsystem",
    "case_id",
    "subject_revision",
    "repository_revision",
    "seed_ref",
    "seed_id",
    "seed_binding_identity",
)


@dataclass(frozen=True)
class _GroundedObligation:
    obligation_id: str
    text: str
    required_capabilities: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    subsystems: Tuple[str, ...] = ()
    seed_bindings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _RevisionContext:
    subject_revision: str
    repository_revision: int


@dataclass(frozen=True)
class _BoundFailure:
    node: TraceNode
    signature: Mapping[str, Any]
    coverage: Tuple[str, ...]
    confirmed_path: Tuple[str, ...]
    explicit_binding: bool
    explicit_offline_binding: bool


@dataclass(frozen=True)
class _FormalFailureBinding:
    conflict: bool
    explicit_match: bool


def reconstruct_obligation_gap_candidates(
    graph: Any,
) -> Tuple[ObligationGapCandidate, ...]:
    """Reconstruct authored planning omissions without creating graph facts."""
    case_id = str(getattr(graph, "case_id", "") or "").strip()
    rejections: List[JsonDict] = []
    revision_context = _formal_revision_context(
        graph,
        case_id=case_id,
        rejections=rejections,
    )
    if not case_id or revision_context is None:
        _publish_obligation_gap_reconstruction_audit(
            graph,
            candidates=(),
            rejections=rejections,
        )
        return ()
    obligations = _grounded_obligations(
        graph,
        case_id=case_id,
        revision_context=revision_context,
        rejections=rejections,
    )
    if not obligations:
        _publish_obligation_gap_reconstruction_audit(
            graph,
            candidates=(),
            rejections=rejections,
        )
        return ()
    nodes = tuple(
        sorted(
            (
                node
                for node in getattr(graph, "nodes", {}).values()
                if _active_case_node(
                    graph,
                    node,
                    case_id=case_id,
                    revision_context=revision_context,
                )
            ),
            key=_node_chronology_key,
        )
    )
    decisions = tuple(
        node
        for node in nodes
        if node.event_type.strip().lower() == "decision"
        and not _banned_provenance_node(node)
        and not bool(node.data.get("offline_only"))
    )
    output = []
    for obligation in obligations:
        for decision in decisions:
            candidate = _candidate_for_decision(
                graph,
                case_id=case_id,
                subject_revision=revision_context.subject_revision,
                obligation=obligation,
                decision=decision,
                nodes=nodes,
                revision_context=revision_context,
                rejections=rejections,
            )
            if candidate is not None:
                output.append(candidate)
    unique = {candidate.identity: candidate for candidate in output}
    candidates = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.decision_ref,
                item.obligation_id,
                item.failure_signature,
                item.identity,
            ),
        )
    )
    _publish_obligation_gap_reconstruction_audit(
        graph,
        candidates=candidates,
        rejections=rejections,
    )
    return candidates


def _publish_obligation_gap_reconstruction_audit(
    graph: Any,
    *,
    candidates: Sequence[ObligationGapCandidate],
    rejections: Sequence[Mapping[str, Any]],
) -> None:
    ordered_rejections = tuple(
        {
            "reason": str(item.get("reason") or "rejected_fact"),
            "source_ref": str(item.get("source_ref") or ""),
        }
        for item in sorted(
            rejections,
            key=stable_json,
        )
    )
    payload = {
        "schema": "obligation-gap-reconstruction-audit/v1",
        "behavior_impact": "none_offline_analysis_only",
        "candidate_count": len(candidates),
        "candidate_identities": [item.identity for item in candidates],
        "rejections": list(ordered_rejections),
    }
    payload["audit_identity"] = hashlib.sha256(
        stable_json(payload).encode("utf-8")
    ).hexdigest()
    setattr(graph, "obligation_gap_reconstruction_audit", payload)


def obligation_gap_candidate_audit(
    candidates: Sequence[ObligationGapCandidate],
) -> JsonDict:
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.decision_ref,
                item.obligation_id,
                item.failure_signature,
                item.identity,
            ),
        )
    )
    if any(not isinstance(item, ObligationGapCandidate) for item in ordered):
        raise TypeError("obligation gap audit accepts only immutable candidates")
    payload = {
        "schema": "obligation-gap-candidate-audit/v1",
        "behavior_impact": "none_offline_analysis_only",
        "candidate_count": len(ordered),
        "candidates": [item.to_dict() for item in ordered],
    }
    payload["audit_identity"] = hashlib.sha256(
        stable_json(payload).encode("utf-8")
    ).hexdigest()
    return payload


def _candidate_for_decision(
    graph: Any,
    *,
    case_id: str,
    subject_revision: str,
    obligation: _GroundedObligation,
    decision: TraceNode,
    nodes: Sequence[TraceNode],
    revision_context: _RevisionContext,
    rejections: List[JsonDict],
) -> Optional[ObligationGapCandidate]:
    rationale = normalize_text(decision_rationale_text(decision))
    if not rationale:
        return None
    declared_obligation_refs = _declared_refs(
        decision.data,
        ("obligation_refs", "recognized_obligation_refs", "evidence_refs"),
    )
    canonical_declared = _canonical_evidence_refs(
        graph,
        declared_obligation_refs,
        case_id=case_id,
        revision_context=revision_context,
    )
    if declared_obligation_refs and canonical_declared is None:
        rejections.append(
            {
                "reason": "invalid_decision_evidence_refs",
                "source_ref": decision.ref,
                "obligation_id": obligation.obligation_id,
            }
        )
        return None
    required = obligation.required_capabilities
    recognized = _mentioned_coverage(required, rationale)
    explicit_recognized = _coverage_values(
        required,
        _fact_value(decision.data, "recognized_capabilities"),
    )
    recognized = tuple(sorted(set(recognized) | set(explicit_recognized)))
    explicit_binding = obligation.obligation_id in {
        str(item)
        for item in (
            _fact_value(decision.data, "obligation_ids")
            or _fact_value(decision.data, "recognized_obligation_ids")
            or ()
        )
        if str(item)
    }
    if not recognized and not explicit_binding:
        return None
    text_excluded = set(
        _negated_coverage(required, rationale, _NEGATION_PATTERNS)
    )
    explicit_excluded = text_excluded | set(
        _coverage_values(
            required,
            _fact_value(decision.data, "excluded_capabilities"),
        )
    )
    if any(
        canonical_fact_key(key) == "planned_capabilities"
        for key in decision.data
    ):
        planned = _coverage_values(
            required,
            _fact_value(decision.data, "planned_capabilities"),
        )
    else:
        planned = tuple(
            sorted(
                set(
                    _affirmative_coverage(
                        required,
                        rationale,
                        _NEGATION_PATTERNS,
                    )
                )
                - explicit_excluded
            )
        )
    later_nodes = tuple(
        node
        for node in nodes
        if node.ref != decision.ref
        and _temporally_after(
            graph,
            later=node,
            earlier=decision,
            confirmed_path=_confirmed_path(graph, decision.ref, node.ref),
        )
    )
    action_refs_by_capability: Dict[str, List[str]] = defaultdict(list)
    action_paths: Dict[str, Tuple[str, ...]] = {}
    verification_coverage = set()
    verification_refs = []
    failures: List[_BoundFailure] = []
    for node in later_nodes:
        if not _is_action_node(node):
            continue
        confirmed_path = _confirmed_path(graph, decision.ref, node.ref)
        if not confirmed_path:
            continue
        action_coverage = _affirmative_coverage(
            required,
            _node_search_text(node),
            _NEGATION_PATTERNS,
        )
        for capability in action_coverage:
            action_refs_by_capability[capability].append(node.ref)
            action_paths[node.ref] = confirmed_path
    for node in later_nodes:
        event_type = node.event_type.strip().lower()
        text = _node_search_text(node)
        confirmed_path = _confirmed_path(graph, decision.ref, node.ref)
        if event_type in _VERIFICATION_EVENTS and _successful_verification(node):
            verified_here = set(
                _affirmative_coverage(
                    required,
                    text,
                    _VERIFICATION_NEGATION_PATTERNS,
                )
            )
            chained = {
                capability
                for capability in verified_here
                if any(
                    _temporally_after(
                        graph,
                        later=node,
                        earlier=getattr(graph, "nodes", {})[action_ref],
                        confirmed_path=_confirmed_path(
                            graph,
                            action_ref,
                            node.ref,
                        ),
                    )
                    and _confirmed_path(graph, action_ref, node.ref)
                    for action_ref in action_refs_by_capability.get(
                        capability,
                        (),
                    )
                )
            }
            if chained:
                verification_coverage.update(chained)
                verification_refs.append(node.ref)
        if event_type in _FAILURE_EVENTS and _failed_observation(node):
            signature = _eligible_failure_signature(node)
            if signature is None:
                continue
            formal_binding = _formal_failure_binding(
                graph,
                node,
                signature=signature,
                obligation=obligation,
                decision=decision,
                case_id=case_id,
                revision_context=revision_context,
            )
            if formal_binding.conflict:
                continue
            obligation_paths = tuple(
                path
                for source_ref in obligation.evidence_refs
                for path in (_confirmed_path(graph, source_ref, node.ref),)
                if path
            )
            failure_path = confirmed_path or (
                min(obligation_paths, key=lambda path: (len(path), path))
                if obligation_paths
                else ()
            )
            if not failure_path and not formal_binding.explicit_match:
                continue
            failure_coverage = _mentioned_coverage(
                required,
                " ".join((text, stable_json(signature))),
            )
            if not failure_coverage and not formal_binding.explicit_match:
                continue
            failures.append(
                _BoundFailure(
                    node=node,
                    signature=signature,
                    coverage=failure_coverage,
                    confirmed_path=failure_path,
                    explicit_binding=formal_binding.explicit_match,
                    explicit_offline_binding=bool(
                        formal_binding.explicit_match and not failure_path
                    ),
                )
            )
    action_coverage = set(action_refs_by_capability)
    gap = set(required) - (action_coverage & verification_coverage)
    if not gap:
        return None
    relevant_failures = tuple(
        failure
        for failure in failures
        if set(failure.coverage) & gap
        or failure.explicit_binding
    )
    if not relevant_failures:
        return None
    failure_refs = tuple(
        sorted(failure.node.ref for failure in relevant_failures)
    )
    failure_signature = hashlib.sha256(
        stable_json(
            {
                "schema": "obligation-gap-failure-signature/v1",
                "case_id": case_id,
                "subject_revision": subject_revision,
                "obligation_id": obligation.obligation_id,
                "failures": [
                    {
                        "ref": failure.node.ref,
                        "event_type": failure.node.event_type,
                        "coverage": list(failure.coverage),
                        "signature": dict(failure.signature),
                        "binding": (
                            "explicit_offline"
                            if failure.explicit_offline_binding
                            else "confirmed_path"
                        ),
                    }
                    for failure in sorted(
                        relevant_failures,
                        key=lambda item: _node_chronology_key(item.node),
                    )
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    evidence_refs = tuple(
        sorted(
            {
                *obligation.evidence_refs,
                decision.ref,
                *(
                    ref
                    for refs in action_refs_by_capability.values()
                    for ref in refs
                ),
                *verification_refs,
                *failure_refs,
                *(canonical_declared or ()),
            }
        )
    )
    if _canonical_evidence_refs(
        graph,
        evidence_refs,
        case_id=case_id,
        revision_context=revision_context,
    ) != evidence_refs:
        rejections.append(
            {
                "reason": "ineligible_candidate_evidence_refs",
                "source_ref": decision.ref,
                "obligation_id": obligation.obligation_id,
            }
        )
        return None
    confirmed_paths = tuple(
        sorted(
            {
                *action_paths.values(),
                *(
                    failure.confirmed_path
                    for failure in relevant_failures
                    if failure.confirmed_path
                ),
            }
        )
    )
    offline_paths = tuple(
        {
            "from_ref": decision.ref,
            "to_ref": failure_ref,
            "relation": "authored_obligation_gap_precedes_failure",
            "evidence_type": "offline_reconstruction",
            "evidence_refs": list(evidence_refs),
            "eligible_for_attribution": True,
            "confirmed_fact": False,
            "inference_method": OBLIGATION_GAP_RECONSTRUCTION_RULE,
            "edge_origin": "offline.obligation_gap_reconstruction",
        }
        for failure in relevant_failures
        if failure.explicit_offline_binding
        for failure_ref in (failure.node.ref,)
    )
    excluded_decision = bool(explicit_excluded)
    rationale_projection = (
        rationale
        if excluded_decision
        or any(marker in rationale for marker in _CLOSURE_MARKERS)
        else ""
    )
    return ObligationGapCandidate.create(
        case_id=case_id,
        subject_revision=subject_revision,
        obligation_id=obligation.obligation_id,
        obligation_text=obligation.text,
        decision_ref=decision.ref,
        recognized_evidence_refs=(decision.ref,),
        required_coverage=required,
        recognized_coverage=recognized,
        planned_coverage=planned,
        actual_action_coverage=tuple(action_coverage),
        verification_coverage=tuple(verification_coverage),
        exclusion_or_closure_rationale=rationale_projection,
        downstream_failure_signature_refs=failure_refs,
        failure_signature=failure_signature,
        confirmed_path_provenance=confirmed_paths,
        offline_path_provenance=offline_paths,
        evidence_refs=evidence_refs,
        resolver=graph.resolve,
    )


def _grounded_obligations(
    graph: Any,
    *,
    case_id: str,
    revision_context: _RevisionContext,
    rejections: List[JsonDict],
) -> Tuple[_GroundedObligation, ...]:
    output = []
    for node in sorted(
        getattr(graph, "nodes", {}).values(), key=_node_chronology_key
    ):
        if (
            node.event_type.strip().lower() not in _OBLIGATION_SOURCE_EVENTS
            or _banned_provenance_node(node)
            or not _active_case_node(
                graph,
                node,
                case_id=case_id,
                revision_context=revision_context,
            )
            or bool(node.data.get("offline_only"))
        ):
            continue
        containers = tuple(_iter_obligation_containers(node.data))
        declared_obligation_data = bool(containers)
        for key, raw_obligations in containers:
            if not isinstance(raw_obligations, (list, tuple)):
                rejections.append(
                    {
                        "reason": "invalid_obligation_container",
                        "source_ref": node.ref,
                        "obligation_id": "",
                    }
                )
                continue
            for raw in raw_obligations:
                if not isinstance(raw, Mapping):
                    rejections.append(
                        {
                            "reason": "invalid_obligation_record",
                            "source_ref": node.ref,
                            "obligation_id": "",
                        }
                    )
                    continue
                obligation_id = str(
                    _fact_value(raw, "obligation_id", "id") or ""
                ).strip()
                text = str(
                    _fact_value(
                        raw,
                        "obligation_text",
                        "text",
                        "required_end_state",
                    )
                    or ""
                ).strip()
                capabilities = _fact_value(raw, "required_capabilities")
                if (
                    not obligation_id
                    or not text
                    or not isinstance(capabilities, (list, tuple))
                    or isinstance(capabilities, (str, bytes, bytearray))
                ):
                    rejections.append(
                        {
                            "reason": "invalid_obligation_contract",
                            "source_ref": node.ref,
                            "obligation_id": obligation_id,
                        }
                    )
                    continue
                required = tuple(
                    sorted(
                        {
                            str(item).strip()
                            for item in capabilities
                            if str(item).strip()
                        }
                    )
                )
                if not required:
                    rejections.append(
                        {
                            "reason": "invalid_obligation_capabilities",
                            "source_ref": node.ref,
                            "obligation_id": obligation_id,
                        }
                    )
                    continue
                declared_refs = _fact_value(raw, "evidence_refs") or ()
                if not isinstance(declared_refs, (list, tuple)):
                    rejections.append(
                        {
                            "reason": "invalid_obligation_evidence_refs",
                            "source_ref": node.ref,
                            "obligation_id": obligation_id,
                        }
                    )
                    continue
                evidence_refs = _canonical_evidence_refs(
                    graph,
                    (node.ref, *declared_refs),
                    case_id=case_id,
                    revision_context=revision_context,
                )
                if evidence_refs is None:
                    rejections.append(
                        {
                            "reason": "unresolved_obligation_evidence_refs",
                            "source_ref": node.ref,
                            "obligation_id": obligation_id,
                        }
                    )
                    continue
                normalized_text = normalize_obligation_text(text)
                subsystems = _formal_string_values(
                    raw,
                    ("subsystem", "affected_subsystem"),
                )
                seed_bindings = _formal_string_values(
                    raw,
                    (
                        "seed_ref",
                        "seed_id",
                        "seed_binding_identity",
                    ),
                )
                output.append(
                    _GroundedObligation(
                        obligation_id=obligation_id,
                        text=normalized_text,
                        required_capabilities=required,
                        evidence_refs=evidence_refs,
                        subsystems=subsystems,
                        seed_bindings=seed_bindings,
                    )
                )
        if declared_obligation_data or _failed_observation(node):
            continue
        natural_text = _natural_contract_text(node)
        natural_capabilities = _natural_required_capabilities(natural_text)
        if not natural_capabilities:
            continue
        normalized_text = normalize_obligation_text(natural_text)
        obligation_id = "obligation:trace:{0}".format(
            hashlib.sha256(
                stable_json(
                    {
                        "schema": "trace-grounded-obligation/v1",
                        "case_id": case_id,
                        "source_ref": node.ref,
                        "text": normalized_text,
                        "required_capabilities": list(
                            natural_capabilities
                        ),
                    }
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        output.append(
            _GroundedObligation(
                obligation_id=obligation_id,
                text=normalized_text,
                required_capabilities=natural_capabilities,
                evidence_refs=(node.ref,),
            )
        )
    unique = {
        (
            item.obligation_id,
            item.text,
            item.required_capabilities,
            item.evidence_refs,
            item.subsystems,
            item.seed_bindings,
        ): item
        for item in output
    }
    return tuple(unique[key] for key in sorted(unique))


def _active_subject_revision(graph: Any) -> str:
    context = _formal_revision_context(
        graph,
        case_id=str(getattr(graph, "case_id", "") or "").strip(),
        rejections=[],
    )
    return context.subject_revision if context is not None else ""


def _formal_revision_context(
    graph: Any,
    *,
    case_id: str,
    rejections: List[JsonDict],
) -> Optional[_RevisionContext]:
    raw_trace = getattr(graph, "raw_trace", {})
    if not case_id or not isinstance(raw_trace, Mapping):
        rejections.append(
            {
                "reason": "missing_formal_case_binding",
                "source_ref": "",
                "obligation_id": "",
            }
        )
        return None
    manifest = raw_trace.get("manifest")
    if not isinstance(manifest, Mapping) or str(
        manifest.get("case_id") or ""
    ).strip() != case_id:
        rejections.append(
            {
                "reason": "missing_formal_case_binding",
                "source_ref": "",
                "obligation_id": "",
            }
        )
        return None
    subject_revision, provenance_status = trace_execution_revision(
        dict(raw_trace)
    )
    if provenance_status != "valid" or not subject_revision:
        rejections.append(
            {
                "reason": "missing_authoritative_active_generation",
                "source_ref": "",
                "obligation_id": "",
            }
        )
        return None
    relevant_nodes = tuple(
        node
        for node in getattr(graph, "nodes", {}).values()
        if not _banned_provenance_node(node)
        and not bool(node.data.get("offline_only"))
        and (
            node.event_type.strip().lower()
            in (
                _OBLIGATION_SOURCE_EVENTS
                | _ACTION_EVENTS
                | _VERIFICATION_EVENTS
                | _FAILURE_EVENTS
                | {"decision"}
            )
        )
    )
    revisions = set()
    for node in relevant_nodes:
        data = node.data
        revision = data.get("repository_revision")
        if (
            str(data.get("case_id") or "").strip() != case_id
            or str(data.get("subject_revision") or "").strip()
            != subject_revision
            or str(data.get("revision_status") or "").strip().lower()
            != "matched"
            or str(
                data.get("revision_provenance_status") or ""
            ).strip().lower()
            != "valid"
            or type(revision) is not int
            or revision < 0
        ):
            rejections.append(
                {
                    "reason": "incompatible_formal_revision_binding",
                    "source_ref": node.ref,
                    "obligation_id": "",
                }
            )
            return None
        revisions.add(revision)
    if len(revisions) != 1:
        rejections.append(
            {
                "reason": "incompatible_formal_revision_binding",
                "source_ref": "",
                "obligation_id": "",
            }
        )
        return None
    return _RevisionContext(
        subject_revision=subject_revision,
        repository_revision=next(iter(revisions)),
    )


def _active_case_node(
    graph: Any,
    node: TraceNode,
    *,
    case_id: str,
    revision_context: _RevisionContext,
) -> bool:
    record_case_id = str(node.data.get("case_id") or "").strip()
    return bool(
        record_case_id == case_id
        and str(node.data.get("subject_revision") or "").strip()
        == revision_context.subject_revision
        and str(node.data.get("revision_status") or "").strip().lower()
        == "matched"
        and str(
            node.data.get("revision_provenance_status") or ""
        ).strip().lower()
        == "valid"
        and node.data.get("repository_revision")
        == revision_context.repository_revision
        and graph.resolve(node.ref) == node.ref
        and graph.active_revision_evidence_eligible(node.ref)
    )


def _banned_provenance_node(node: TraceNode) -> bool:
    provenance = " ".join(
        (
            node.component,
            node.event_type,
            str(node.data.get("source") or ""),
            str(node.data.get("provenance_source") or ""),
        )
    ).casefold()
    return any(term in provenance for term in _BANNED_PROVENANCE_TERMS) or node.event_type.casefold() in {
        "case.observed_defect",
        "case.quality_gap",
        "external.evaluation_fact",
    }


def _canonical_evidence_refs(
    graph: Any,
    refs: Iterable[Any],
    *,
    case_id: str,
    revision_context: _RevisionContext,
) -> Optional[Tuple[str, ...]]:
    output = set()
    for raw_ref in refs:
        ref = str(raw_ref or "").strip()
        if not ref:
            continue
        resolved = graph.resolve(ref)
        node = getattr(graph, "nodes", {}).get(resolved or "")
        if (
            not resolved
            or node is None
            or not _active_case_node(
                graph,
                node,
                case_id=case_id,
                revision_context=revision_context,
            )
        ):
            return None
        output.add(resolved)
    return tuple(sorted(output))


def _iter_obligation_containers(
    value: Any,
) -> Iterable[Tuple[str, Any]]:
    if not isinstance(value, Mapping):
        return
    for raw_key, item in value.items():
        key = canonical_fact_key(raw_key)
        if _evaluation_only_key(key):
            continue
        if key in _OBLIGATION_DATA_KEYS:
            yield key, item
            continue
        if isinstance(item, Mapping):
            yield from _iter_obligation_containers(item)
        elif isinstance(item, (list, tuple)):
            for member in item:
                yield from _iter_obligation_containers(member)


def _fact_value(value: Mapping[str, Any], *keys: str) -> Any:
    wanted = {canonical_fact_key(key) for key in keys}
    for raw_key, item in value.items():
        if canonical_fact_key(raw_key) in wanted:
            return item
    return None


def _formal_string_values(
    value: Mapping[str, Any], keys: Sequence[str]
) -> Tuple[str, ...]:
    output = set()
    wanted = {canonical_fact_key(key) for key in keys}
    for raw_key, item in value.items():
        if canonical_fact_key(raw_key) not in wanted:
            continue
        if isinstance(item, str) and item.strip():
            output.add(item.strip())
        elif type(item) is int:
            output.add(str(item))
        elif isinstance(item, (list, tuple)):
            output.update(
                str(member).strip()
                for member in item
                if str(member).strip()
            )
    return tuple(sorted(output))


def _conflicting_formal_aliases(
    value: Mapping[str, Any], keys: Sequence[str]
) -> bool:
    return any(
        len(_formal_string_values(value, (key,))) > 1
        for key in {canonical_fact_key(item) for item in keys}
    )


def _declared_refs(data: Mapping[str, Any], keys: Sequence[str]) -> Tuple[str, ...]:
    output = []
    for key in keys:
        value = _fact_value(data, key)
        if isinstance(value, str):
            output.append(value)
        elif isinstance(value, (list, tuple)):
            output.extend(str(item) for item in value if str(item))
    return tuple(output)


def _coverage_values(
    required: Sequence[str], value: Any
) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    requested = {str(item).strip() for item in value if str(item).strip()}
    return tuple(sorted(set(required) & requested))


def _natural_required_capabilities(text: str) -> Tuple[str, ...]:
    normalized = normalize_text(text)
    if not normalized or not any(
        cue in normalized
        for cue in (
            " must ",
            "require",
            "contract",
            "dependency",
            "interface",
            "protocol",
            "restore",
            "import ",
            "from ",
        )
    ):
        return ()
    capabilities = set(
        re.findall(r"(?<![a-z0-9_])__[a-z0-9_]+__(?![a-z0-9_])", normalized)
    )
    capabilities.update(
        match
        for match in re.findall(r"`([a-z_][a-z0-9_.-]*)`", normalized)
    )
    capabilities.update(
        match.split(".", 1)[0]
        for match in re.findall(
            r"(?:\bimport\s+|\bfrom\s+)([a-z_][a-z0-9_.]*)",
            normalized,
        )
    )
    capabilities.update(
        re.findall(
            r"\brequires?\s+([a-z_][a-z0-9_.-]*)",
            normalized,
        )
    )
    capabilities.update(
        re.findall(
            r"\b([a-z_][a-z0-9_.-]*)\s+(?:runtime\s+)?dependency\b",
            normalized,
        )
    )
    stop_words = {
        "a",
        "an",
        "complete",
        "contract",
        "dependency",
        "descriptor",
        "interface",
        "protocol",
        "runtime",
        "the",
    }
    return tuple(
        sorted(
            capability.strip(".-")
            for capability in capabilities
            if capability.strip(".-")
            and capability.strip(".-") not in stop_words
        )
    )


def _natural_contract_text(node: TraceNode) -> str:
    if node.event_type.strip().lower() not in _NATURAL_OBLIGATION_SOURCE_EVENTS:
        return ""
    values = []
    for raw_key, item in node.data.items():
        key = canonical_fact_key(raw_key)
        if (
            key in _NATURAL_CONTRACT_FIELDS
            and not evaluation_only_fact_key(key)
            and isinstance(item, str)
            and item.strip()
        ):
            values.append(item.strip())
    accepted = []
    for value in values:
        accepted.extend(_affirmative_present_contract_clauses(value))
    return normalize_text(". ".join(accepted))


def _affirmative_present_contract_clauses(text: str) -> Tuple[str, ...]:
    normalized = normalize_text(text)
    if not normalized:
        return ()
    sentences = tuple(
        item.strip()
        for item in re.split(r"[;.\n]+", normalized)
        if item.strip()
    )
    accepted = []
    for sentence in sentences:
        clauses = _text_clauses(sentence)
        if (
            any(character in sentence for character in ('"', "'", "“", "”", "‘", "’"))
            or any(
                re.search(pattern, sentence)
                for pattern in _NATURAL_NON_CURRENT_PATTERNS
            )
            or not any(
                any(
                    re.search(pattern, clause)
                    for pattern in _NATURAL_CURRENT_SCOPE_PATTERNS
                )
                and not _negative_clause(
                    clause,
                    _CURRENT_SCOPE_NEGATION_PATTERNS,
                )
                for clause in clauses
            )
        ):
            continue
        deontic_scope = False
        for clause in clauses:
            if any(
                re.search(pattern, clause)
                for pattern in _NATURAL_DEONTIC_PATTERNS
            ):
                deontic_scope = True
            if (
                deontic_scope
                and not _negative_clause(clause, _NEGATION_PATTERNS)
            ):
                accepted.append(clause)
    return tuple(accepted)


def _coverage_in_text(
    required: Sequence[str], text: str
) -> Tuple[str, ...]:
    return _mentioned_coverage(required, text)


def _mentioned_coverage(
    required: Sequence[str], text: str
) -> Tuple[str, ...]:
    normalized = normalize_text(text)
    return tuple(
        sorted(
            capability
            for capability in required
            if _text_contains_capability(normalized, capability)
        )
    )


def _affirmative_coverage(
    required: Sequence[str],
    text: str,
    negative_patterns: Sequence[str],
) -> Tuple[str, ...]:
    clauses = _text_clauses(text)
    return tuple(
        sorted(
            capability
            for capability in required
            if any(
                _text_contains_capability(clause, capability)
                and not _negative_clause(clause, negative_patterns)
                for clause in clauses
            )
        )
    )


def _negated_coverage(
    required: Sequence[str],
    text: str,
    negative_patterns: Sequence[str],
) -> Tuple[str, ...]:
    clauses = _text_clauses(text)
    return tuple(
        sorted(
            capability
            for capability in required
            if any(
                _text_contains_capability(clause, capability)
                and _negative_clause(clause, negative_patterns)
                for clause in clauses
            )
        )
    )


def _text_clauses(text: str) -> Tuple[str, ...]:
    normalized = normalize_text(text)
    clauses = []
    for segment in re.split(
        r"(?:[;.\n]+|,\s*(?=(?:but|so|then|however|yet)\b)|\b(?:but|however|yet)\b)",
        normalized,
    ):
        previous_negative = False
        for index, item in enumerate(re.split(r"\band\b|(?:以及|并且)", segment)):
            clause = item.strip(" ,")
            if not clause:
                continue
            has_predicate = bool(
                re.search(
                    r"\b(?:implement(?:ed)?|restore(?:d)?|verify|verified|"
                    r"test(?:ed|s)?|run|ran|require(?:d|s)?|need(?:ed|s)?|"
                    r"skip(?:ped)?|omit(?:ted)?|exclude(?:d)?|update(?:d)?)\b",
                    clause,
                )
            )
            if index and previous_negative and not has_predicate:
                clause = "excluded " + clause
            previous_negative = _negative_clause(
                clause, _NEGATION_PATTERNS
            )
            clauses.append(clause)
    clauses = tuple(clauses)
    return clauses or ((normalized,) if normalized else ())


def _negative_clause(
    clause: str,
    patterns: Sequence[str],
) -> bool:
    return any(re.search(pattern, clause) for pattern in patterns)


def _text_contains_capability(text: str, capability: str) -> bool:
    normalized = normalize_text(capability)
    if not normalized:
        return False
    return bool(
        re.search(
            r"(?<![a-z0-9_]){0}(?![a-z0-9_])".format(re.escape(normalized)),
            text,
        )
    )


def _node_search_text(node: TraceNode) -> str:
    return normalize_text(
        " ".join(
            (
                node.title,
                node.status,
                *_iter_attribution_text_values(node.data),
            )
        )
    )


def _evaluation_only_key(value: str) -> bool:
    return evaluation_only_fact_key(value)


def _scrub_evaluation_only_value(value: Any) -> Any:
    return scrub_evaluation_only_fact_value(value)


def _iter_attribution_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not _evaluation_only_key(str(key)):
                yield from _iter_attribution_text_values(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_attribution_text_values(item)


def _is_action_node(node: TraceNode) -> bool:
    event_type = node.event_type.strip().lower()
    if event_type in _ACTION_EVENTS:
        return True
    if event_type != "tool.result":
        return False
    category = str(
        node.data.get("category") or node.data.get("operation_kind") or ""
    ).strip().lower()
    return category in {"repository_change", "edit", "write"}


def _successful_verification(node: TraceNode) -> bool:
    status = node.status.strip().lower()
    exit_code = node.data.get("exit_code", node.data.get("exit"))
    if exit_code == 0:
        return True
    return status in {"success", "passed", "pass", "ok"} and exit_code in {
        None,
        0,
    }


def _failed_observation(node: TraceNode) -> bool:
    if _banned_provenance_node(node):
        return False
    event_type = node.event_type.strip().lower()
    status = node.status.strip().lower()
    exit_code = node.data.get("exit_code", node.data.get("exit"))
    return bool(
        event_type in {"case.failed", "tool.error"}
        or status in {"failure", "failed", "error"}
        or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0)
        or node.data.get("success") is False
    )


def _eligible_failure_signature(
    node: TraceNode,
) -> Optional[Mapping[str, Any]]:
    raw_signature = _fact_value(node.data, "failure_signature")
    if not isinstance(raw_signature, Mapping):
        return None
    scrubbed = _scrub_evaluation_only_value(raw_signature)
    if not isinstance(scrubbed, Mapping):
        return None
    signature = {}
    for raw_key, item in scrubbed.items():
        key = canonical_fact_key(raw_key)
        if not key or evaluation_only_fact_key(key):
            continue
        if key in signature and stable_json(signature[key]) != stable_json(item):
            return None
        signature[key] = item
    schema = str(
        _fact_value(signature, "schema", "schema_version")
        or ""
    ).strip()
    status = str(
        _fact_value(signature, "status") or node.status or ""
    ).strip().lower()
    signature_id = str(
        _fact_value(signature, "signature_id")
        or _fact_value(node.data, "failure_signature_id")
        or ""
    ).strip()
    structured_evaluation_signature = bool(
        schema == "evaluation-failure-signature/v1"
        and any(
            _fact_value(signature, key)
            for key in (
                "exception_family",
                "exception_type",
                "assertion_contract",
                "assertion",
                "relevant_symbol",
                "symbol",
                "subsystem",
                "affected_subsystem",
            )
        )
    )
    if (
        schema not in _FAILURE_SIGNATURE_SCHEMAS
        or status not in {"failure", "failed", "error"}
        or not (signature_id or structured_evaluation_signature)
    ):
        return None
    return {
        str(key): signature[key]
        for key in sorted(signature)
    }


def _formal_failure_binding(
    graph: Any,
    node: TraceNode,
    *,
    signature: Mapping[str, Any],
    obligation: _GroundedObligation,
    decision: TraceNode,
    case_id: str,
    revision_context: _RevisionContext,
) -> _FormalFailureBinding:
    binding = _fact_value(node.data, "failure_binding")
    if not isinstance(binding, Mapping):
        binding = {}
    binding_types = tuple(
        item.lower()
        for item in _formal_string_values(binding, ("binding_type",))
    )
    binding_type = binding_types[0] if len(binding_types) == 1 else ""
    explicit_match = False
    conflict = len(binding_types) > 1 or any(
        _conflicting_formal_aliases(source, _FORMAL_FAILURE_BINDING_KEYS)
        for source in (binding, signature)
    )
    if binding_type == "obligation":
        obligation_ids = _formal_string_values(
            binding, ("obligation_id",)
        )
        explicit_match = obligation_ids == (obligation.obligation_id,)
        conflict = conflict or not explicit_match
    elif binding_type == "decision":
        decision_refs = _formal_string_values(binding, ("decision_ref",))
        resolved = tuple(
            sorted(
                {
                    graph.resolve(ref) or ""
                    for ref in decision_refs
                    if ref
                }
            )
        )
        explicit_match = resolved == (decision.ref,)
        conflict = conflict or not explicit_match
    elif binding_type:
        conflict = True

    declared_sources = (binding, signature)
    expected_fields = (
        (("obligation_id",), (obligation.obligation_id,)),
        (("subsystem", "affected_subsystem"), obligation.subsystems),
        (("case_id",), (case_id,)),
        (
            ("subject_revision",),
            (revision_context.subject_revision,),
        ),
        (
            ("repository_revision",),
            (str(revision_context.repository_revision),),
        ),
        (
            ("seed_ref", "seed_id", "seed_binding_identity"),
            obligation.seed_bindings,
        ),
    )
    for keys, expected in expected_fields:
        if not expected:
            continue
        declared = {
            value
            for source in declared_sources
            for value in _formal_string_values(source, keys)
        }
        if declared and not declared <= set(expected):
            conflict = True
    return _FormalFailureBinding(
        conflict=conflict,
        explicit_match=explicit_match and not conflict,
    )


def _node_chronology_key(node: TraceNode) -> Tuple[int, Any, str]:
    chronology_index = node.data.get("chronology_index")
    if type(chronology_index) is int and chronology_index >= 0:
        return (0, chronology_index, node.ref)
    timestamp = str(node.timestamp or "").strip()
    if timestamp:
        return (1, timestamp, node.ref)
    return (2, 0, node.ref)


def _temporal_comparison(
    later: TraceNode,
    earlier: TraceNode,
) -> Optional[bool]:
    later_index = later.data.get("chronology_index")
    earlier_index = earlier.data.get("chronology_index")
    if (
        type(later_index) is int
        and later_index >= 0
        and type(earlier_index) is int
        and earlier_index >= 0
    ):
        return later_index > earlier_index
    later_timestamp = str(later.timestamp or "").strip()
    earlier_timestamp = str(earlier.timestamp or "").strip()
    if later_timestamp and earlier_timestamp:
        return later_timestamp > earlier_timestamp
    return None


def _temporally_after(
    graph: Any,
    *,
    later: TraceNode,
    earlier: TraceNode,
    confirmed_path: Sequence[str],
) -> bool:
    if confirmed_path:
        return True
    comparison = _temporal_comparison(later, earlier)
    if comparison is not None:
        return comparison
    return False


def _confirmed_path(
    graph: Any,
    source_ref: str,
    target_ref: str,
    *,
    max_hops: int = 24,
) -> Tuple[str, ...]:
    if source_ref == target_ref:
        return ()
    queue = [(source_ref, (source_ref,))]
    visited = {source_ref}
    cursor = 0
    while cursor < len(queue):
        current, path = queue[cursor]
        cursor += 1
        if len(path) - 1 >= max_hops:
            continue
        for next_ref in sorted(graph.downstream_refs(current)):
            if next_ref in visited:
                continue
            confirmed = any(
                edge.get("eligible_for_attribution") is True
                and str(edge.get("evidence_type") or "").casefold()
                not in {
                    "offline_reconstruction",
                    "semantic_inferred",
                    "temporal_inferred",
                    "temporal_only",
                }
                and not str(edge.get("edge_origin") or "").startswith("offline.")
                for edge in graph.edge_context(current, next_ref)
            )
            if not confirmed:
                continue
            next_path = (*path, next_ref)
            if next_ref == target_ref:
                return next_path
            visited.add(next_ref)
            queue.append((next_ref, next_path))
    return ()


def reconstruct_message_lineage(
    *,
    trace: JsonDict,
    nodes: Dict[str, TraceNode],
    aliases: Dict[str, str],
    artifact_reader: VerifiedArtifactReader,
) -> JsonDict:
    positions = {ref: index for index, ref in enumerate(nodes)}
    identities = {ref: record_identity(node) for ref, node in nodes.items()}
    edges: List[JsonDict] = []
    turns = build_turns(nodes=nodes, identities=identities, positions=positions)
    snapshots = build_snapshots(nodes=nodes, identities=identities)

    for turn in turns:
        refs = sorted(turn["record_refs"], key=lambda item: positions[item])
        latest_reasoning: Optional[str] = None
        for ref in refs:
            node = nodes[ref]
            if node.event_type != "decision":
                continue
            decision_type = str(node.data.get("decision_type") or "")
            if decision_type == "reasoning_block":
                latest_reasoning = ref
                continue
            if latest_reasoning and decision_type in {
                "llm_tool_call",
                "tool_execute",
                "mcp_tool_call",
                "skill_call",
                "subagent_call",
                "task_delegation",
            }:
                edges.append(
                    lineage_edge(
                        from_ref=latest_reasoning,
                        to_ref=ref,
                        relation="reasoning_selected_action",
                        evidence_type="confirmed",
                        evidence_refs=[
                            f"session:{turn['session_id']}",
                            f"message:{turn['message_id']}",
                        ],
                        confidence=1.0,
                        eligible_for_attribution=True,
                        inference_method="same_message_order",
                    )
                )

    artifact_gaps: List[JsonDict] = []
    decision_refs = [ref for ref, node in nodes.items() if node.event_type == "decision"]
    llm_refs = [ref for ref, node in nodes.items() if node.event_type in ("llm.call", "llm.turn")]
    for llm_ref in llm_refs:
        llm_node = nodes[llm_ref]
        artifact_ids = input_message_artifact_ids(llm_node.data)
        if not artifact_ids:
            continue
        search_texts = []
        for artifact_id in artifact_ids:
            text, gap = read_artifact_search_text(
                artifact_id=artifact_id,
                artifact_reader=artifact_reader,
            )
            if gap:
                artifact_gaps.append({"node_ref": llm_ref, "artifact_id": artifact_id, "reason": gap})
            if text:
                search_texts.append(text)
        if not search_texts:
            continue
        request_text = normalize_text("\n".join(search_texts))
        llm_position = positions[llm_ref]
        llm_session = identities[llm_ref].get("session_id")
        candidates = [
            ref
            for ref in decision_refs
            if positions[ref] < llm_position
            and (
                not llm_session
                or not identities[ref].get("session_id")
                or identities[ref].get("session_id") == llm_session
            )
        ][-MAX_DECISION_CANDIDATES_PER_REQUEST:]
        matches = []
        for decision_ref in reversed(candidates):
            rationale = decision_rationale_text(nodes[decision_ref])
            normalized = normalize_text(rationale)
            if len(normalized) < MIN_MATCH_CHARS or normalized not in request_text:
                continue
            matches.append(decision_ref)
            if len(matches) >= MAX_MATCHED_DECISIONS_PER_REQUEST:
                break
        for decision_ref in reversed(matches):
            edges.append(
                lineage_edge(
                    from_ref=decision_ref,
                    to_ref=llm_ref,
                    relation="retained_in_context",
                    evidence_type="content_matched",
                    evidence_refs=[f"artifact:{item}" for item in artifact_ids],
                    confidence=0.95,
                    eligible_for_attribution=True,
                    inference_method="normalized_exact_text",
                )
            )

    edges.extend(build_temporal_availability_edges(nodes=nodes, identities=identities, positions=positions))
    edges = dedupe_edges(edges)
    return {
        "version": "1.0",
        "collection_mode": "offline_passive_reconstruction",
        "behavior_impact": "none",
        "turns": turns,
        "snapshots": snapshots,
        "edges": edges,
        "gaps": artifact_gaps,
        "stats": {
            "turn_count": len(turns),
            "snapshot_count": len(snapshots),
            "edge_count": len(edges),
            "confirmed_edge_count": sum(item["evidence_type"] == "confirmed" for item in edges),
            "content_matched_edge_count": sum(item["evidence_type"] == "content_matched" for item in edges),
            "temporal_inferred_edge_count": sum(item["evidence_type"] == "temporal_inferred" for item in edges),
            "attribution_eligible_edge_count": sum(bool(item["eligible_for_attribution"]) for item in edges),
            "gap_count": len(artifact_gaps),
        },
    }


def record_identity(node: TraceNode) -> JsonDict:
    data = node.data if isinstance(node.data, dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    input_data = data.get("input") if isinstance(data.get("input"), dict) else {}
    return {
        "session_id": first_text(
            data.get("session_id"),
            data.get("sessionID"),
            metadata.get("session_id"),
            metadata.get("sessionID"),
            input_data.get("session_id"),
            input_data.get("sessionID"),
        ),
        "message_id": first_text(
            data.get("message_id"),
            data.get("messageID"),
            metadata.get("message_id"),
            metadata.get("messageID"),
            input_data.get("message_id"),
            input_data.get("messageID"),
        ),
        "part_id": first_text(data.get("part_id"), data.get("partID"), metadata.get("partID")),
        "call_id": first_text(
            data.get("call_id"),
            data.get("callID"),
            metadata.get("call_id"),
            metadata.get("callID"),
        ),
        "span_id": first_text(data.get("span_id"), data.get("spanID")),
        "decision_id": first_text(data.get("decision_id"), data.get("decisionID")),
    }


def build_turns(
    *,
    nodes: Dict[str, TraceNode],
    identities: Dict[str, JsonDict],
    positions: Dict[str, int],
) -> List[JsonDict]:
    grouped: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for ref in nodes:
        session_id = identities[ref].get("session_id")
        message_id = identities[ref].get("message_id")
        if session_id and message_id:
            grouped[(session_id, message_id)].append(ref)
    output = []
    for index, ((session_id, message_id), refs) in enumerate(
        sorted(
            grouped.items(),
            key=lambda item: turn_chronology_key(
                nodes=nodes,
                refs=item[1],
                positions=positions,
            ),
        ),
        start=1,
    ):
        ordered = sorted(
            refs,
            key=lambda item: record_chronology_key(nodes[item], positions[item]),
        )
        output.append(
            {
                "turn_id": f"turn_{index}",
                "session_id": session_id,
                "message_id": message_id,
                "record_refs": ordered,
                "reasoning_refs": filter_refs(nodes, ordered, event_type="decision", decision_type="reasoning_block"),
                "action_decision_refs": [
                    ref
                    for ref in ordered
                    if nodes[ref].event_type == "decision"
                    and str(nodes[ref].data.get("decision_type") or "") != "reasoning_block"
                ],
                "tool_call_refs": filter_refs(nodes, ordered, event_type="tool.call"),
                "tool_result_refs": filter_refs(nodes, ordered, event_type="tool.result"),
                "response_refs": [
                    ref for ref in ordered if nodes[ref].event_type in ("response.output", "response.claim")
                ],
            }
        )
    for index, turn in enumerate(output):
        turn["previous_turn_ref"] = output[index - 1]["turn_id"] if index else ""
    return output


def record_chronology_key(node: TraceNode, fallback_position: int) -> Tuple[int, str, int]:
    timestamp = str(node.timestamp or "").strip()
    if timestamp:
        return (0, timestamp, fallback_position)
    return (1, "", fallback_position)


def turn_chronology_key(
    *,
    nodes: Dict[str, TraceNode],
    refs: List[str],
    positions: Dict[str, int],
) -> Tuple[int, str, int]:
    decision_refs = [ref for ref in refs if nodes[ref].event_type == "decision"]
    candidates = decision_refs or refs
    return min(record_chronology_key(nodes[ref], positions[ref]) for ref in candidates)


def build_snapshots(*, nodes: Dict[str, TraceNode], identities: Dict[str, JsonDict]) -> List[JsonDict]:
    output = []
    for ref, node in nodes.items():
        stage = snapshot_stage(node)
        if not stage:
            continue
        output.append(
            {
                "snapshot_id": f"snapshot:{node.record_id}",
                "node_ref": ref,
                "stage": stage,
                "session_id": identities[ref].get("session_id") or "",
                "message_id": identities[ref].get("message_id") or "",
                "message_count": numeric_value(node.data.get("message_count"), node.data.get("input_message_count")),
                "content_hashes": sorted(set(collect_hashes(node.data))),
                "artifact_refs": [f"artifact:{item}" for item in collect_artifact_ids(node.data)],
                "transform_names": collect_transform_names(node.data),
            }
        )
    return output


def snapshot_stage(node: TraceNode) -> str:
    if node.event_type == "prompt.assembly":
        return "prompt_assembled"
    if node.event_type == "context.pack":
        return "context_selected"
    if node.event_type == "context.compaction":
        return "compaction_output"
    if node.event_type == "context.transform":
        return str(node.data.get("stage") or node.title or "context_transformed")
    if node.event_type in ("llm.call", "llm.turn"):
        return "llm_request_sent"
    return ""


def input_message_artifact_ids(data: JsonDict) -> List[str]:
    value = data.get("input_messages")
    if not isinstance(value, dict):
        return []
    return dedupe(
        str(item).removeprefix("artifact:")
        for item in (value.get("artifact_id"), value.get("payload_ref"))
        if isinstance(item, str) and item
    )


def read_artifact_search_text(
    *,
    artifact_id: str,
    artifact_reader: VerifiedArtifactReader,
) -> Tuple[str, str]:
    resolved = artifact_reader.read(artifact_id)
    if resolved.content is None:
        return "", resolved.failures[0] if resolved.failures else "artifact_unreadable"
    content = resolved.content
    if len(content) > MAX_ARTIFACT_CHARS:
        return "", "artifact_too_large_for_exact_matching"
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return content, ""
    return "\n".join(iter_text_values(parsed)), ""


def build_temporal_availability_edges(
    *,
    nodes: Dict[str, TraceNode],
    identities: Dict[str, JsonDict],
    positions: Dict[str, int],
) -> List[JsonDict]:
    llm_by_session: Dict[str, List[str]] = defaultdict(list)
    for ref, node in nodes.items():
        session_id = identities[ref].get("session_id")
        if session_id and node.event_type in ("llm.call", "llm.turn"):
            llm_by_session[session_id].append(ref)
    for refs in llm_by_session.values():
        refs.sort(key=lambda item: positions[item])
    output = []
    for ref, node in nodes.items():
        if node.event_type != "tool.result":
            continue
        session_id = identities[ref].get("session_id")
        if not session_id:
            continue
        next_llm = next((item for item in llm_by_session.get(session_id, []) if positions[item] > positions[ref]), None)
        if not next_llm:
            continue
        output.append(
            lineage_edge(
                from_ref=ref,
                to_ref=next_llm,
                relation="available_to_next_request",
                evidence_type="temporal_inferred",
                evidence_refs=[f"session:{session_id}"],
                confidence=0.5,
                eligible_for_attribution=False,
                inference_method="same_session_next_llm_order",
            )
        )
    return output


def lineage_edge(
    *,
    from_ref: str,
    to_ref: str,
    relation: str,
    evidence_type: str,
    evidence_refs: List[str],
    confidence: float,
    eligible_for_attribution: bool,
    inference_method: str,
) -> JsonDict:
    digest = hashlib.sha256(f"{from_ref}|{to_ref}|{relation}|{evidence_type}".encode("utf-8")).hexdigest()[:16]
    return {
        "edge_id": f"lineage_{digest}",
        "from_ref": from_ref,
        "to_ref": to_ref,
        "relation": relation,
        "evidence_type": evidence_type,
        "evidence_refs": evidence_refs,
        "confidence": confidence,
        "eligible_for_attribution": eligible_for_attribution,
        "inference_method": inference_method,
    }


def decision_rationale_text(node: TraceNode) -> str:
    rationale = node.data.get("rationale")
    if isinstance(rationale, str):
        return rationale
    if isinstance(rationale, dict):
        recent = rationale.get("recent_reasoning")
        if isinstance(recent, str):
            return recent
        preview = rationale.get("preview")
        if isinstance(preview, str):
            return preview
    return ""


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def iter_text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_text_values(item)


def collect_artifact_ids(value: Any) -> List[str]:
    output = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("artifact_id", "payload_ref", "raw_artifact_ref") and isinstance(item, str):
                output.append(item.removeprefix("artifact:"))
            else:
                output.extend(collect_artifact_ids(item))
    elif isinstance(value, list):
        for item in value:
            output.extend(collect_artifact_ids(item))
    return dedupe(output)


def collect_hashes(value: Any) -> List[str]:
    output = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("hash", "content_hash", "payload_hash") and isinstance(item, str):
                output.append(item)
            else:
                output.extend(collect_hashes(item))
    elif isinstance(value, list):
        for item in value:
            output.extend(collect_hashes(item))
    return output


def collect_transform_names(data: JsonDict) -> List[str]:
    output = []
    for transform in data.get("transforms") or []:
        if isinstance(transform, dict) and transform.get("name"):
            output.append(str(transform["name"]))
    for stage in data.get("message_transforms") or []:
        if not isinstance(stage, dict):
            continue
        for transform in stage.get("transforms") or []:
            if isinstance(transform, dict) and transform.get("name"):
                output.append(str(transform["name"]))
    return dedupe(output)


def filter_refs(
    nodes: Dict[str, TraceNode],
    refs: Iterable[str],
    *,
    event_type: str,
    decision_type: str = "",
) -> List[str]:
    output = []
    for ref in refs:
        node = nodes[ref]
        if node.event_type != event_type:
            continue
        if decision_type and str(node.data.get("decision_type") or "") != decision_type:
            continue
        output.append(ref)
    return output


def first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""


def numeric_value(*values: Any) -> int:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def dedupe_edges(edges: Iterable[JsonDict]) -> List[JsonDict]:
    seen = set()
    output = []
    for edge in edges:
        key = (edge["from_ref"], edge["to_ref"], edge["relation"], edge["evidence_type"])
        if key in seen:
            continue
        seen.add(key)
        output.append(edge)
    return output
