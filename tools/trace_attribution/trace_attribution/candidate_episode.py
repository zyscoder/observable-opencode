"""Pure, deterministic episode-aware reservation for attribution candidates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


AUTHORED_PLAN = "authored_plan"
EXECUTION = "execution"
VERIFICATION = "verification"
CLOSURE = "closure"
OTHER = "other"
FALLBACK_EPISODE_KEY = "fallback"

_EPISODE_ID_KEYS = (
    "episode_id",
    "episode_ref",
    "progress_episode_id",
)
_MATERIALIZATION_ID_KEYS = (
    "materialization_id",
    "materialization_ref",
)
_MATERIALIZATION_EVENT_TYPES = {
    "change",
    "execution.observation",
    "mcp.call",
    "mcp.result",
    "skill.load",
    "skill.result",
    "tool.call",
    "tool.error",
    "tool.result",
    "verification",
}
_PLAN_PHASES = {"design", "exploration", "plan", "planning", "reasoning"}
_EXECUTION_PHASES = {
    "delivery",
    "execution",
    "implementation",
    "mutation",
}
_VERIFICATION_PHASES = {"check", "test", "validation", "verification"}
_CLOSURE_PHASES = {
    "closure",
    "completed",
    "completion",
    "final",
    "finalization",
}
_EXECUTION_DECISION_TYPES = {
    "action_generation",
    "agent_tool_call",
    "llm_tool_call",
    "tool_call",
    "tool_execute",
}
_PLAN_DECISION_TYPES = {"plan", "planning", "reasoning_block"}
_VERIFICATION_ACTIONS = {
    "check",
    "lint",
    "test",
    "typecheck",
    "validate",
    "verification",
    "verify",
}
_CLOSURE_ACTIONS = {
    "close",
    "complete",
    "finish",
    "finalize",
    "respond",
}
_CLOSURE_EVENT_TYPES = {
    "case.completed",
    "final.answer",
    "message.output",
    "session.completed",
}


@dataclass(frozen=True)
class EpisodeReserveAudit:
    ref: str
    episode_key: str
    episode_role: str
    reserve_rank: Optional[int]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "episode_key": self.episode_key,
            "episode_role": self.episode_role,
            "reserve_rank": self.reserve_rank,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EpisodeReserveSelection:
    reserve_refs: Tuple[str, ...]
    fallback_refs: Tuple[str, ...]
    audit: Tuple[EpisodeReserveAudit, ...]

    @property
    def selected_audit(self) -> Tuple[EpisodeReserveAudit, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.audit
                    if item.reserve_rank is not None
                ),
                key=lambda item: item.reserve_rank,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reserve_refs": list(self.reserve_refs),
            "fallback_refs": list(self.fallback_refs),
            "audit": [item.to_dict() for item in self.audit],
        }


@dataclass(frozen=True)
class _CandidateMetadata:
    ref: str
    position: int
    input_rank: int
    value: Any

    @property
    def order_key(self) -> Tuple[int, str]:
        return (self.position, self.ref)


def extract_episode_key(
    candidate_metadata: Any,
    candidate_to_seed_path: Sequence[Any],
) -> Optional[str]:
    """Return an episode key from recorded IDs or a path materialization."""
    node = _candidate_node(candidate_metadata)
    containers = (
        candidate_metadata,
        node,
        _value(node, "data"),
        _value(candidate_metadata, "edge"),
        _value(_value(node, "data"), "metadata"),
    )
    explicit = _explicit_episode_identity(containers)
    if explicit is not None:
        return _stable_episode_key(explicit)

    candidate_ref = _candidate_ref(candidate_metadata)
    for path_member in candidate_to_seed_path:
        if _path_ref(path_member) == candidate_ref:
            continue
        path_node = _candidate_node(path_member)
        path_containers = (
            path_member,
            path_node,
            _value(path_node, "data"),
            _value(_value(path_node, "data"), "metadata"),
        )
        explicit = _explicit_episode_identity(path_containers)
        if explicit is not None:
            return _stable_episode_key(explicit)
        if _is_materialization_path_member(path_node):
            anchor_ref = _path_ref(path_node)
            if anchor_ref:
                return _stable_episode_key(
                    ("path_materialization", anchor_ref)
                )
    return None


def classify_episode_role(candidate_metadata: Any) -> str:
    """Classify a candidate using only structured, recorded semantics."""
    node = _candidate_node(candidate_metadata)
    data = _mapping(_value(node, "data"))
    phase = _normalized(data.get("phase"))
    event_type = _normalized(_value(node, "event_type"))
    decision_type = _normalized(data.get("decision_type"))
    action = _normalized(
        data.get("chosen_action")
        or data.get("tool_name")
        or data.get("action")
        or data.get("name")
    )

    if phase in _CLOSURE_PHASES or event_type in _CLOSURE_EVENT_TYPES:
        return CLOSURE
    if phase in _VERIFICATION_PHASES or event_type == "verification":
        return VERIFICATION
    if phase in _EXECUTION_PHASES:
        return EXECUTION
    if phase in _PLAN_PHASES:
        return AUTHORED_PLAN
    if event_type in _MATERIALIZATION_EVENT_TYPES:
        return VERIFICATION if event_type == "verification" else EXECUTION
    if action in _CLOSURE_ACTIONS:
        return CLOSURE
    if action in _VERIFICATION_ACTIONS:
        return VERIFICATION
    if event_type == "decision" and decision_type in _PLAN_DECISION_TYPES:
        return AUTHORED_PLAN
    if event_type == "decision" and not decision_type:
        return AUTHORED_PLAN
    if decision_type in _EXECUTION_DECISION_TYPES or action:
        return EXECUTION
    return OTHER


def select_episode_diverse_reserve(
    candidates: Iterable[Any],
    candidate_paths: Mapping[str, Sequence[Any]],
    *,
    limit: int,
) -> EpisodeReserveSelection:
    """Reserve episode representatives round-robin, then stable fallbacks."""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")

    normalized = _normalize_candidates(candidates)
    episode_by_ref: Dict[str, Optional[str]] = {}
    role_by_ref: Dict[str, str] = {}
    groups: Dict[str, list[_CandidateMetadata]] = {}
    fallback = []
    for item in normalized:
        inline_path = _value(item.value, "candidate_to_seed_path")
        if inline_path is None:
            inline_path = _value(item.value, "path")
        path = candidate_paths.get(item.ref, inline_path or ())
        episode_key = extract_episode_key(item.value, path)
        role = classify_episode_role(item.value)
        episode_by_ref[item.ref] = episode_key
        role_by_ref[item.ref] = role
        if episode_key is None:
            fallback.append(item)
        else:
            groups.setdefault(episode_key, []).append(item)

    representative_queues: list[
        Tuple[str, Tuple[_CandidateMetadata, ...]]
    ] = []
    representative_reason: Dict[str, str] = {}
    episode_has_plan: Dict[str, bool] = {}
    for episode_key, members in groups.items():
        ordered = tuple(sorted(members, key=lambda item: item.order_key))
        plans = tuple(
            item
            for item in ordered
            if role_by_ref[item.ref] == AUTHORED_PLAN
        )
        lifecycle = tuple(
            item
            for item in ordered
            if role_by_ref[item.ref]
            in {EXECUTION, VERIFICATION, CLOSURE}
        )
        queue = []
        if plans:
            plan = plans[0]
            queue.append(plan)
            representative_reason[plan.ref] = (
                "earliest_authored_plan_in_episode"
            )
        if lifecycle:
            latest = lifecycle[-1]
            if not queue or queue[0].ref != latest.ref:
                queue.append(latest)
                representative_reason[latest.ref] = (
                    "latest_{0}_in_episode".format(
                        role_by_ref[latest.ref]
                    )
                )
        if queue:
            episode_has_plan[episode_key] = bool(plans)
            representative_queues.append(
                (episode_key, tuple(queue))
            )

    representative_queues.sort(
        key=lambda item: (
            0 if episode_has_plan[item[0]] else 1,
            min(
                member.order_key
                for member in groups[item[0]]
            ),
            item[0],
        )
    )
    full_order = list(_round_robin(representative_queues))
    fallback.sort(key=lambda item: item.order_key)
    full_order.extend(fallback)
    selected = tuple(full_order[:limit])
    selected_rank = {
        item.ref: rank for rank, item in enumerate(selected)
    }
    full_order_refs = {item.ref for item in full_order}
    fallback_refs = tuple(item.ref for item in fallback)

    audit = []
    for item in sorted(normalized, key=lambda value: value.order_key):
        episode_key = episode_by_ref[item.ref]
        reserve_rank = selected_rank.get(item.ref)
        if reserve_rank is not None:
            reason = (
                "episode_unresolved_fallback"
                if episode_key is None
                else representative_reason[item.ref]
            )
        elif item.ref in full_order_refs:
            reason = "reserve_limit"
        else:
            reason = "non_representative_episode_member"
        audit.append(
            EpisodeReserveAudit(
                ref=item.ref,
                episode_key=episode_key or FALLBACK_EPISODE_KEY,
                episode_role=role_by_ref[item.ref],
                reserve_rank=reserve_rank,
                reason=reason,
            )
        )

    return EpisodeReserveSelection(
        reserve_refs=tuple(item.ref for item in selected),
        fallback_refs=fallback_refs,
        audit=tuple(audit),
    )


def _round_robin(
    queues: Sequence[Tuple[str, Tuple[_CandidateMetadata, ...]]],
) -> Iterable[_CandidateMetadata]:
    cursor = 0
    while any(cursor < len(queue) for _, queue in queues):
        for _, queue in queues:
            if cursor < len(queue):
                yield queue[cursor]
        cursor += 1


def _normalize_candidates(
    candidates: Iterable[Any],
) -> Tuple[_CandidateMetadata, ...]:
    unique: Dict[str, _CandidateMetadata] = {}
    for input_rank, candidate in enumerate(candidates):
        ref = _candidate_ref(candidate)
        if not ref or ref in unique:
            continue
        raw_position = _value(candidate, "position")
        if type(raw_position) is not int:
            raw_position = _value(_candidate_node(candidate), "position")
        position = (
            raw_position
            if type(raw_position) is int
            else input_rank
        )
        unique[ref] = _CandidateMetadata(
            ref=ref,
            position=position,
            input_rank=input_rank,
            value=candidate,
        )
    return tuple(unique.values())


def _candidate_ref(value: Any) -> str:
    ref = _value(value, "ref")
    if ref is None:
        ref = _value(_candidate_node(value), "ref")
    return str(ref or "").strip()


def _candidate_node(value: Any) -> Any:
    return _value(value, "node") or value


def _path_ref(value: Any) -> str:
    return _candidate_ref(value)


def _explicit_episode_identity(
    containers: Sequence[Any],
) -> Optional[Tuple[str, str]]:
    for namespace, keys in (
        ("episode", _EPISODE_ID_KEYS),
        ("materialization", _MATERIALIZATION_ID_KEYS),
    ):
        for container in containers:
            mapping = _mapping(container)
            for key in keys:
                value = mapping.get(key)
                if isinstance(value, str) and value.strip():
                    return (namespace, value.strip())
    return None


def _is_materialization_path_member(node: Any) -> bool:
    event_type = _normalized(_value(node, "event_type"))
    if event_type in _MATERIALIZATION_EVENT_TYPES:
        return True
    phase = _normalized(_mapping(_value(node, "data")).get("phase"))
    return phase in (
        _EXECUTION_PHASES
        | _VERIFICATION_PHASES
        | _CLOSURE_PHASES
    )


def _stable_episode_key(identity: Tuple[str, str]) -> str:
    payload = "{0}\0{1}".format(*identity)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return "episode:v1:{0}".format(digest)


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()
