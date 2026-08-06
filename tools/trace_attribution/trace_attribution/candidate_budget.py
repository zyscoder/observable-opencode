"""Deterministic, offline candidate budgeting for global attribution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Tuple

from .causal_state import CausalCandidate
from .candidate_episode import select_episode_diverse_reserve
from .models import JsonDict, stable_json
from .restoration_obligation import (
    canonical_fact_key,
    evaluation_only_fact_key,
)


CANDIDATE_BUDGET_SCHEMA = "candidate-budget-funnel/v4"
CANDIDATE_BUDGET_V3_SCHEMA = "candidate-budget-funnel/v3"
CANDIDATE_BUDGET_V2_SCHEMA = "candidate-budget-funnel/v2"
NO_ACTIVE_SEED_CAUSAL_PATH = "no_active_seed_causal_path"
MAX_GROUNDED_DECISION_RESERVE = 128
AUTHORED_DECISION = "authored_decision"
TOOL_CHANGE_ENVELOPE = "tool_change_envelope"
VERIFICATION_OUTCOME = "verification_outcome"
FACTOR_OR_LIFECYCLE = "factor_or_lifecycle"
OTHER = "other"

_CATEGORIES = (
    AUTHORED_DECISION,
    TOOL_CHANGE_ENVELOPE,
    VERIFICATION_OUTCOME,
    FACTOR_OR_LIFECYCLE,
    OTHER,
)
_TOOL_OR_CHANGE_PREFIXES = ("tool.", "mcp.", "skill.", "execution.")
_FACTOR_OR_LIFECYCLE_TERMS = (
    "context",
    "compaction",
    "subagent",
    "signal",
    "lifecycle",
    "factor",
)
_IGNORED_CANDIDATE_IDENTITY_KEYS = frozenset(
    {
        "human_labels",
        "human_label",
        "ground_truth",
        "benchmark_labels",
        "benchmark_label",
        "attribution_only_obligation_gaps",
        "score",
        "retrieval_score",
    }
)
_IGNORED_CANDIDATE_IDENTITY_MARKERS = (
    "benchmark_score",
    "benchmark_label",
    "evaluation",
    "expected_root",
    "ground_truth",
    "human_label",
    "review",
    "reviewer",
    "rubric",
    "score_explanation",
    "scoring",
)
_MAX_CANDIDATE_IDENTITY_NODES = 4096
_MAX_CANDIDATE_IDENTITY_DEPTH = 64


@dataclass(frozen=True)
class CandidateBudgetPolicy:
    total_limit: int = 24
    grounded_decision_reserve: int = 4

    def __post_init__(self) -> None:
        if self.total_limit < 0:
            raise ValueError("total_limit must be non-negative")
        if self.grounded_decision_reserve < 0:
            raise ValueError("grounded_decision_reserve must be non-negative")
        if self.grounded_decision_reserve > MAX_GROUNDED_DECISION_RESERVE:
            raise ValueError(
                "grounded_decision_reserve must not exceed {0}".format(
                    MAX_GROUNDED_DECISION_RESERVE
                )
            )

    def to_dict(self) -> JsonDict:
        return {
            "total_limit": self.total_limit,
            "grounded_decision_reserve": self.grounded_decision_reserve,
        }


def quality_first_candidate_budget(
    discovered_count: int,
) -> CandidateBudgetPolicy:
    """Expand deterministic recall tiers before any candidate is discarded."""
    if type(discovered_count) is not int or discovered_count < 0:
        raise ValueError("discovered_count must be a non-negative integer")
    if discovered_count <= 24:
        return CandidateBudgetPolicy(24, 4)
    if discovered_count <= 96:
        return CandidateBudgetPolicy(96, 16)
    if discovered_count <= 192:
        return CandidateBudgetPolicy(192, 32)
    return CandidateBudgetPolicy(256, 128)


@dataclass(frozen=True)
class CandidateBudgetSelection:
    policy: CandidateBudgetPolicy
    discovered: Tuple[CausalCandidate, ...]
    offered: Tuple[CausalCandidate, ...]
    evidence_context: Tuple[CausalCandidate, ...]
    dropped: Tuple[CausalCandidate, ...]
    classifications: Tuple[Tuple[str, str], ...]
    grounded_decision_refs: Tuple[str, ...] = ()
    reserved_grounded_decision_refs: Tuple[str, ...] = ()
    reserved_episode_refs: Tuple[str, ...] = ()
    episode_reserve_audit: Tuple[Mapping[str, Any], ...] = ()
    candidate_paths: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    ineligible_reasons: Tuple[Tuple[str, str], ...] = ()
    selection_identity: str = field(init=False)

    def __post_init__(self) -> None:
        expected_policy = quality_first_candidate_budget(len(self.discovered))
        if self.policy != expected_policy:
            raise ValueError(
                "{0} policy must match the quality-first discovered-count tier".format(
                    CANDIDATE_BUDGET_SCHEMA
                )
            )
        discovered_refs = {candidate.ref for candidate in self.discovered}
        ineligible = dict(self.ineligible_reasons)
        if (
            len(ineligible) != len(self.ineligible_reasons)
            or any(ref not in discovered_refs for ref in ineligible)
            or any(
                reason != NO_ACTIVE_SEED_CAUSAL_PATH
                for reason in ineligible.values()
            )
        ):
            raise ValueError("candidate ineligibility audit is invalid")
        object.__setattr__(
            self,
            "selection_identity",
            _identity(self._unsigned_audit()),
        )

    def to_dict(self) -> JsonDict:
        return {
            **self._unsigned_audit(),
            "selection_identity": self.selection_identity,
        }

    def _unsigned_audit(self) -> JsonDict:
        classifications_by_ref = dict(self.classifications)
        offered_ranks = {
            candidate_identity(candidate): rank
            for rank, candidate in enumerate(self.offered)
        }
        context_ranks = {
            candidate_identity(candidate): rank
            for rank, candidate in enumerate(self.evidence_context)
        }
        reserved_refs = set(self.reserved_grounded_decision_refs)
        episode_reserved_refs = set(self.reserved_episode_refs)
        episode_audit_by_ref = {
            str(item.get("ref") or ""): item
            for item in self.episode_reserve_audit
        }
        candidate_paths = dict(self.candidate_paths)
        ineligible_reasons = dict(self.ineligible_reasons)
        counts_by_category = {
            category: {
                "discovered": 0,
                "offered": 0,
                "evidence_context": 0,
                "dropped": 0,
            }
            for category in _CATEGORIES
        }
        context_reasons = {NO_ACTIVE_SEED_CAUSAL_PATH: 0}
        drop_reasons = {"total_limit": 0}
        candidate_audit = []
        for discovered_rank, candidate in enumerate(self.discovered):
            identity = candidate_identity(candidate)
            category = classifications_by_ref[candidate.ref]
            omission_gap = _obligation_gap(candidate)
            if identity in offered_ranks:
                disposition = "offered"
            elif identity in context_ranks:
                disposition = "evidence_context"
            else:
                disposition = "dropped"
            if disposition == "offered":
                reason = (
                    "obligation_gap_reserve"
                    if omission_gap is not None
                    and candidate.ref in reserved_refs
                    else "episode_diverse_reserve"
                    if candidate.ref in episode_reserved_refs
                    else "grounded_decision_reserve"
                    if candidate.ref in reserved_refs
                    else "input_order"
                )
            elif disposition == "evidence_context":
                reason = ineligible_reasons[candidate.ref]
                context_reasons[reason] += 1
            else:
                reason = "total_limit"
                drop_reasons[reason] += 1
            counts_by_category[category]["discovered"] += 1
            counts_by_category[category][disposition] += 1
            candidate_audit.append(
                {
                    "ref": candidate.ref,
                    "category": category,
                    "discovered_rank": discovered_rank,
                    "offered_rank": offered_ranks.get(identity),
                    "context_rank": context_ranks.get(identity),
                    "assessment_eligible": (
                        candidate.ref not in ineligible_reasons
                    ),
                    "disposition": disposition,
                    "reason": reason,
                    "grounded_hops": max(
                        0, len(candidate_paths.get(candidate.ref, ())) - 1
                    ),
                    "episode_key": str(
                        episode_audit_by_ref.get(candidate.ref, {}).get(
                            "episode_key"
                        )
                        or "fallback"
                    ),
                    "episode_role": str(
                        episode_audit_by_ref.get(candidate.ref, {}).get(
                            "episode_role"
                        )
                        or "other"
                    ),
                    "reserve_rank": (
                        offered_ranks.get(identity)
                        if candidate.ref
                        in episode_reserved_refs | reserved_refs
                        else None
                    ),
                    "reserve_disposition": (
                        "obligation_gap_reserved"
                        if omission_gap is not None
                        and candidate.ref in reserved_refs
                        else "episode_reserved"
                        if candidate.ref in episode_reserved_refs
                        else "grounded_decision_reserved"
                        if candidate.ref in reserved_refs
                        else "quality_order_fill"
                        if disposition == "offered"
                        else disposition
                    ),
                    "reserve_reason": str(
                        episode_audit_by_ref.get(candidate.ref, {}).get(
                            "reason"
                        )
                        or reason
                    ),
                    "candidate_identity": identity,
                }
            )

        return {
            "schema": CANDIDATE_BUDGET_SCHEMA,
            "discovered_count": len(self.discovered),
            "offered_count": len(self.offered),
            "evidence_context_count": len(self.evidence_context),
            "dropped_count": len(self.dropped),
            "counts_by_category": counts_by_category,
            "policy": self.policy.to_dict(),
            "grounded_decision_refs": list(self.grounded_decision_refs),
            "reserved_grounded_decision_refs": list(
                self.reserved_grounded_decision_refs
            ),
            "reserved_episode_refs": list(self.reserved_episode_refs),
            "evidence_context_refs": [
                candidate.ref for candidate in self.evidence_context
            ],
            "context_reasons": context_reasons,
            "drop_reasons": drop_reasons,
            "candidate_audit": candidate_audit,
        }


def classify_candidate(graph: Any, candidate: CausalCandidate) -> str:
    """Assign one scheduling category from trace-node semantics alone."""
    del graph
    event_type = candidate.node.event_type.strip().lower()
    if event_type == "decision":
        return AUTHORED_DECISION
    if event_type == "change" or event_type.startswith(_TOOL_OR_CHANGE_PREFIXES):
        return TOOL_CHANGE_ENVELOPE
    if event_type in {"verification", "external.evaluation_fact", "case.result"}:
        return VERIFICATION_OUTCOME
    if any(term in event_type for term in _FACTOR_OR_LIFECYCLE_TERMS):
        return FACTOR_OR_LIFECYCLE
    return OTHER


def select_global_candidates(
    graph: Any,
    candidates: Iterable[CausalCandidate],
    *,
    grounded_decision_refs: Iterable[str] = (),
    candidate_paths: Mapping[str, Iterable[str]] | None = None,
    ineligible_reasons: Mapping[str, str] | None = None,
) -> CandidateBudgetSelection:
    """Derive the quality-first tier, reserve decisions, then fill in order."""
    supplied = tuple(candidates)
    reconstructed = _reconstructed_obligation_gap_candidates(graph)
    canonical_input = tuple(
        _canonical_candidate(graph, candidate)
        for candidate in (*reconstructed, *supplied)
    )
    task3_routes_present = bool(reconstructed) or any(
        _obligation_gap(candidate) is not None for candidate in supplied
    )
    if task3_routes_present:
        from .causal_retrieval import canonicalize_ranked_candidates

        canonical = tuple(
            canonicalize_ranked_candidates(
                graph,
                canonical_input,
                limit=len(canonical_input),
            )
        )
        discovered = _first_candidate_by_identity(canonical)
    else:
        canonical = canonical_input
        discovered = _first_candidate_by_ref(canonical)
    policy = quality_first_candidate_budget(len(discovered))
    classifications = tuple(
        (candidate.ref, classify_candidate(graph, candidate))
        for candidate in discovered
    )
    classifications_by_ref = dict(classifications)
    discovered_refs = {candidate.ref for candidate in discovered}
    canonical_ineligible_reasons = {}
    for raw_ref, reason in (ineligible_reasons or {}).items():
        ref = _canonical_ref(graph, raw_ref)
        if ref not in discovered_refs:
            raise ValueError("ineligible candidate ref was not discovered")
        if reason != NO_ACTIVE_SEED_CAUSAL_PATH:
            raise ValueError("unsupported candidate ineligibility reason")
        canonical_ineligible_reasons[ref] = reason
    eligible = tuple(
        candidate
        for candidate in discovered
        if candidate.ref not in canonical_ineligible_reasons
    )
    explicit_grounded_ref_order = _first_ref(
        _canonical_ref(graph, ref)
        for ref in grounded_decision_refs
    )
    explicit_grounded_refs = set(explicit_grounded_ref_order)
    if task3_routes_present:
        grounded = tuple(
            candidate
            for candidate in eligible
            if classifications_by_ref[candidate.ref] == AUTHORED_DECISION
            and (
                _obligation_gap(candidate) is not None
                or candidate.ref in explicit_grounded_refs
            )
        )
    else:
        eligible_by_ref = {
            candidate.ref: candidate for candidate in eligible
        }
        grounded = tuple(
            eligible_by_ref[ref]
            for ref in explicit_grounded_ref_order
            if ref in eligible_by_ref
            and classifications_by_ref[ref] == AUTHORED_DECISION
        )
    reserve_limit = min(policy.total_limit, policy.grounded_decision_reserve)
    canonical_candidate_paths = {
        _canonical_ref(graph, ref): tuple(
            _canonical_ref(graph, path_ref)
            for path_ref in path
            if str(path_ref)
        )
        for ref, path in (candidate_paths or {}).items()
    }
    graph_nodes = getattr(graph, "nodes", {})
    episode_paths = {
        ref: tuple(
            graph_nodes.get(path_ref, path_ref)
            if isinstance(graph_nodes, Mapping)
            else path_ref
            for path_ref in path
        )
        for ref, path in canonical_candidate_paths.items()
    }
    episode_candidates = (
        _first_candidate_by_ref(eligible)
        if task3_routes_present
        else eligible
    )
    episode_selection = select_episode_diverse_reserve(
        episode_candidates,
        episode_paths,
        limit=reserve_limit,
    )
    episode_audit_by_ref = {
        item.ref: item for item in episode_selection.audit
    }
    episode_refs = tuple(
        ref
        for ref in episode_selection.reserve_refs
        if episode_audit_by_ref[ref].episode_key != "fallback"
    )
    if task3_routes_present:
        eligible_by_identity = {
            candidate_identity(candidate): candidate
            for candidate in eligible
        }
        episode_identity_by_ref = {
            candidate.ref: candidate_identity(candidate)
            for candidate in episode_candidates
        }
        merged_reserve_identities = _balanced_reserve_refs(
            grounded_refs=tuple(
                candidate_identity(candidate) for candidate in grounded
            ),
            episode_refs=tuple(
                episode_identity_by_ref[ref]
                for ref in episode_refs
                if ref in episode_identity_by_ref
            ),
            limit=reserve_limit,
        )
        reserved = tuple(
            eligible_by_identity[identity]
            for identity in merged_reserve_identities
            if identity in eligible_by_identity
        )
    else:
        eligible_by_ref = {
            candidate.ref: candidate for candidate in eligible
        }
        merged_reserve_refs = _balanced_reserve_refs(
            grounded_refs=tuple(
                candidate.ref for candidate in grounded
            ),
            episode_refs=episode_refs,
            limit=reserve_limit,
        )
        reserved = tuple(
            eligible_by_ref[ref]
            for ref in merged_reserve_refs
            if ref in eligible_by_ref
        )
    reserved_identities = frozenset(
        candidate_identity(candidate) for candidate in reserved
    )
    offered = reserved + tuple(
        candidate
        for candidate in eligible
        if candidate_identity(candidate) not in reserved_identities
    )[: max(0, policy.total_limit - len(reserved))]
    offered_identities = frozenset(
        candidate_identity(candidate) for candidate in offered
    )
    context_limit = max(0, policy.total_limit - len(offered))
    evidence_context = tuple(
        candidate
        for candidate in discovered
        if candidate.ref in canonical_ineligible_reasons
    )[:context_limit]
    context_identities = frozenset(
        candidate_identity(candidate) for candidate in evidence_context
    )
    dropped = tuple(
        candidate
        for candidate in discovered
        if candidate_identity(candidate) not in offered_identities
        and candidate_identity(candidate) not in context_identities
    )

    return CandidateBudgetSelection(
        policy=policy,
        discovered=discovered,
        offered=offered,
        evidence_context=evidence_context,
        dropped=dropped,
        classifications=classifications,
        grounded_decision_refs=tuple(candidate.ref for candidate in grounded),
        reserved_grounded_decision_refs=tuple(
            candidate.ref
            for candidate in reserved
            if candidate_identity(candidate)
            in {candidate_identity(item) for item in grounded}
        ),
        reserved_episode_refs=tuple(
            candidate.ref
            for candidate in reserved
            if candidate.ref in set(episode_refs)
        ),
        episode_reserve_audit=tuple(
            item.to_dict() for item in episode_selection.audit
        ),
        candidate_paths=tuple(
            (candidate.ref, canonical_candidate_paths.get(candidate.ref, ()))
            for candidate in discovered
        ),
        ineligible_reasons=tuple(
            (candidate.ref, canonical_ineligible_reasons[candidate.ref])
            for candidate in discovered
            if candidate.ref in canonical_ineligible_reasons
        ),
    )


def candidate_identity(candidate: CausalCandidate) -> str:
    """Return the shared semantic identity used by funnel and manifest."""
    omission_gap = _obligation_gap(candidate)
    if omission_gap is not None:
        return omission_gap.identity
    payload = _scrub_candidate_identity_value(candidate.to_dict())
    edge = dict(payload.get("edge") or {})
    if (
        candidate.source not in {"confirmed_edge", "attribution_edge"}
        or bool(edge.get("retrieval_candidate"))
        or str(edge.get("edge_origin") or "").startswith("offline.")
    ):
        edge.pop("confidence", None)
    payload["edge"] = edge
    return _identity(payload)


def _scrub_candidate_identity_value(
    value: Any,
    *,
    _active: Any = None,
    _budget: Any = None,
    _depth: int = 0,
) -> Any:
    active = set() if _active is None else _active
    budget = [0] if _budget is None else _budget
    if isinstance(value, (Mapping, list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("candidate identity metadata is recursive")
        budget[0] += 1
        if (
            budget[0] > _MAX_CANDIDATE_IDENTITY_NODES
            or _depth > _MAX_CANDIDATE_IDENTITY_DEPTH
        ):
            raise ValueError("candidate identity metadata traversal is bounded")
        active.add(marker)
    else:
        marker = None
    try:
        if isinstance(value, Mapping):
            output = {}
            for key, item in value.items():
                if _ignored_candidate_identity_key(str(key)):
                    continue
                scrubbed = _scrub_candidate_identity_value(
                    item,
                    _active=active,
                    _budget=budget,
                    _depth=_depth + 1,
                )
                if isinstance(item, (Mapping, list, tuple)) and not scrubbed:
                    continue
                output[str(key)] = scrubbed
            return output
        if isinstance(value, (list, tuple)):
            return [
                _scrub_candidate_identity_value(
                    item,
                    _active=active,
                    _budget=budget,
                    _depth=_depth + 1,
                )
                for item in value
            ]
        return value
    finally:
        if marker is not None:
            active.remove(marker)


def _ignored_candidate_identity_key(value: str) -> bool:
    normalized = canonical_fact_key(value)
    if normalized in _IGNORED_CANDIDATE_IDENTITY_KEYS:
        return True
    if evaluation_only_fact_key(normalized):
        return True
    return any(
        normalized == marker
        or normalized.startswith(marker + "_")
        or normalized.endswith("_" + marker)
        or normalized == marker + "s"
        for marker in _IGNORED_CANDIDATE_IDENTITY_MARKERS
    )


_candidate_identity = candidate_identity


def _obligation_gap(candidate: CausalCandidate) -> Any:
    from .causal_retrieval import obligation_gap_for_candidate

    return obligation_gap_for_candidate(candidate)


def _reconstructed_obligation_gap_candidates(
    graph: Any,
) -> Tuple[CausalCandidate, ...]:
    if not isinstance(getattr(graph, "nodes", None), Mapping):
        return ()
    from .causal_retrieval import obligation_gap_causal_candidates

    return obligation_gap_causal_candidates(graph)


def _identity(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _canonical_candidate(graph: Any, candidate: CausalCandidate) -> CausalCandidate:
    ref = _canonical_ref(graph, candidate.ref)
    return candidate if ref == candidate.ref else replace(candidate, ref=ref)


def _canonical_ref(graph: Any, ref: str) -> str:
    resolve = getattr(graph, "resolve", None)
    resolved = resolve(ref) if callable(resolve) else ref
    return str(resolved or ref)


def _first_candidate_by_ref(
    candidates: Iterable[CausalCandidate],
) -> Tuple[CausalCandidate, ...]:
    unique_by_ref = {}
    for candidate in candidates:
        unique_by_ref.setdefault(candidate.ref, candidate)
    return tuple(unique_by_ref.values())


def _first_candidate_by_identity(
    candidates: Iterable[CausalCandidate],
) -> Tuple[CausalCandidate, ...]:
    unique = {}
    for candidate in candidates:
        unique.setdefault(candidate_identity(candidate), candidate)
    return tuple(unique.values())


def _first_ref(refs: Iterable[str]) -> Tuple[str, ...]:
    unique = {}
    for ref in refs:
        unique.setdefault(ref, None)
    return tuple(unique)


def _balanced_reserve_refs(
    *,
    grounded_refs: Tuple[str, ...],
    episode_refs: Tuple[str, ...],
    limit: int,
) -> Tuple[str, ...]:
    """Favor authored decisions while retaining a lifecycle evidence lane."""
    if limit <= 0:
        return ()
    grounded_quota = min(len(grounded_refs), (limit * 3) // 4)
    episode_quota = min(len(episode_refs), limit - grounded_quota)
    selected = list(
        _first_ref(
            (
                *grounded_refs[:grounded_quota],
                *episode_refs[:episode_quota],
            )
        )
    )
    if len(selected) < limit:
        selected = list(
            _first_ref(
                (
                    *selected,
                    *grounded_refs[grounded_quota:],
                    *episode_refs[episode_quota:],
                )
            )
        )
    return tuple(selected[:limit])
