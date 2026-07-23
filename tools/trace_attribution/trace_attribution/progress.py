from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Dict, Iterable, List

from .models import JsonDict, TraceNode


PROGRESS_MEMBER_EVENT_TYPES = {
    "decision",
    "tool.call",
    "tool.result",
    "tool.error",
    "mcp.call",
    "mcp.result",
    "skill.load",
    "change",
    "execution.observation",
    "verification",
}
TERMINAL_PROJECTION_EVENT_TYPES = {
    "case.observed_defect",
    "case.quality_gap",
    "response.output",
    "response.claim",
}
MUTATION_ACTIONS = {"apply_patch", "edit", "multiedit", "write"}
READ_ACTIONS = {"cat", "find", "glob", "grep", "ls", "read", "search"}
VERIFICATION_ACTIONS = {"build", "lint", "test", "typecheck", "verify"}
DELEGATION_DECISION_TYPES = {"subagent_call", "task_delegation"}
PROGRESS_DELIVERY_HISTORY_LIMIT = 8


def reconstruct_progress_episodes(
    *,
    nodes: Dict[str, TraceNode],
    turns: List[JsonDict],
) -> JsonDict:
    """Build passive task-progress aggregates and project them to later terminal nodes."""
    positions = {ref: index for index, ref in enumerate(nodes)}
    episodes: List[TraceNode] = []
    state_by_session: Dict[str, JsonDict] = defaultdict(
        lambda: {
            "previous_episode_ref": "",
            "cumulative_change_count": 0,
            "cumulative_verification_count": 0,
            "consecutive_no_delivery_episodes": 0,
        }
    )

    for chronology_index, turn in enumerate(turns, start=1):
        member_refs = [
            ref
            for ref in turn.get("record_refs") or []
            if ref in nodes and is_progress_member(nodes[ref])
        ]
        if not member_refs:
            continue
        session_id = str(turn.get("session_id") or "")
        state = state_by_session[session_id]
        counts = progress_counts(nodes, member_refs)
        candidate_member_refs = select_candidate_member_refs(nodes, member_refs)
        has_delivery = counts["mutation_count"] > 0 or counts["verification_count"] > 0
        state["cumulative_change_count"] += counts["mutation_count"]
        state["cumulative_verification_count"] += counts["verification_count"]
        state["consecutive_no_delivery_episodes"] = (
            0 if has_delivery else state["consecutive_no_delivery_episodes"] + 1
        )
        episode_id = stable_progress_episode_id(member_refs)
        episode_ref = f"progress_episode:{episode_id}"
        previous_episode_ref = str(state["previous_episode_ref"] or "")
        source_refs = ([previous_episode_ref] if previous_episode_ref else []) + member_refs
        data: JsonDict = {
            "episode_id": episode_id,
            "turn_id": str(turn.get("turn_id") or ""),
            "session_id": session_id,
            "message_id": str(turn.get("message_id") or ""),
            "phase": progress_phase(counts),
            "member_refs": member_refs,
            "member_summaries": [progress_member_summary(nodes[ref]) for ref in member_refs],
            "candidate_member_refs": candidate_member_refs,
            "excluded_member_refs": [ref for ref in member_refs if ref not in candidate_member_refs],
            "candidate_selection_method": "offline_semantic_role_projection_v1",
            "previous_episode_ref": previous_episode_ref,
            **counts,
            "no_delivery_progress": not has_delivery,
            "cumulative_change_count": state["cumulative_change_count"],
            "cumulative_verification_count": state["cumulative_verification_count"],
            "consecutive_no_delivery_episodes": state["consecutive_no_delivery_episodes"],
            "offline_only": True,
            "collection_mode": "offline_passive_reconstruction",
            "behavior_impact": "none",
            "chronology_index": chronology_index,
            "start_position": min(positions[ref] for ref in member_refs),
            "end_position": max(positions[ref] for ref in member_refs),
            "start_timestamp": first_member_timestamp(nodes, member_refs),
            "end_timestamp": last_member_timestamp(nodes, member_refs),
        }
        episodes.append(
            TraceNode(
                ref=episode_ref,
                record_id=episode_id,
                component="progress",
                event_type="progress.episode",
                title=f"Offline progress episode for {turn.get('turn_id') or episode_id}",
                status="derived",
                timestamp=nodes[member_refs[-1]].timestamp,
                data=data,
                source_refs=source_refs,
            )
        )
        state["previous_episode_ref"] = episode_ref

    target_links: Dict[str, List[str]] = {}
    for target_ref, target in nodes.items():
        if target.event_type not in TERMINAL_PROJECTION_EVENT_TYPES:
            continue
        target_position = positions[target_ref]
        candidates = [
            episode
            for episode in episodes
            if episode_precedes_target(episode, target, target_position)
        ]
        if not candidates:
            continue
        latest = candidates[-1]
        target_links[target_ref] = [latest.ref]

    return {
        "version": "1.0",
        "collection_mode": "offline_passive_reconstruction",
        "behavior_impact": "none",
        "episodes": episodes,
        "target_links": target_links,
        "stats": {
            "episode_count": len(episodes),
            "target_link_count": sum(len(refs) for refs in target_links.values()),
            "no_delivery_episode_count": sum(
                bool(episode.data.get("no_delivery_progress")) for episode in episodes
            ),
        },
    }


def is_progress_member(node: TraceNode) -> bool:
    if node.event_type not in PROGRESS_MEMBER_EVENT_TYPES:
        return False
    if node.event_type != "decision":
        return True
    decision_type = str(node.data.get("decision_type") or "").strip().lower()
    return decision_type not in {"agent_loop_step", "llm_step_finish"}


def progress_counts(nodes: Dict[str, TraceNode], member_refs: Iterable[str]) -> JsonDict:
    counts: JsonDict = {
        "reasoning_count": 0,
        "action_count": 0,
        "search_read_count": 0,
        "mutation_count": 0,
        "verification_count": 0,
        "delegation_count": 0,
        "error_count": 0,
    }
    for ref in member_refs:
        node = nodes[ref]
        action = node_action(node)
        decision_type = str(node.data.get("decision_type") or "").strip().lower()
        if node.event_type == "decision" and decision_type == "reasoning_block":
            counts["reasoning_count"] += 1
        elif node.event_type in {"decision", "tool.call", "mcp.call", "skill.load"}:
            counts["action_count"] += 1
        if action in READ_ACTIONS:
            counts["search_read_count"] += 1
        if node.event_type == "change" or action in MUTATION_ACTIONS:
            counts["mutation_count"] += 1
        if node.event_type == "verification" or action in VERIFICATION_ACTIONS:
            counts["verification_count"] += 1
        if decision_type in DELEGATION_DECISION_TYPES:
            counts["delegation_count"] += 1
        if node.event_type == "tool.error" or node.status.lower() in {"error", "failed"}:
            counts["error_count"] += 1
    return counts


def node_action(node: TraceNode) -> str:
    for key in ("tool_name", "chosen_action", "action", "name"):
        value = node.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def progress_phase(counts: JsonDict) -> str:
    if counts["verification_count"]:
        return "verification"
    if counts["mutation_count"]:
        return "implementation"
    if counts["delegation_count"]:
        return "delegation"
    if counts["search_read_count"]:
        return "exploration"
    return "reasoning"


def progress_member_summary(node: TraceNode) -> JsonDict:
    data = node.data if isinstance(node.data, dict) else {}
    summary: JsonDict = {
        "ref": node.ref,
        "event_type": node.event_type,
    }
    for key in ("decision_type", "tool_name", "chosen_action", "rationale", "summary", "status"):
        value = data.get(key)
        if value not in (None, "", [], {}):
            summary[key] = value
    return summary


def select_candidate_member_refs(
    nodes: Dict[str, TraceNode],
    member_refs: List[str],
) -> List[str]:
    candidate_set = set()
    authored_action_refs: List[str] = []
    selected_authored_action = False
    for ref in member_refs:
        node = nodes[ref]
        decision_type = str(node.data.get("decision_type") or "").strip().lower()
        action = node_action(node)
        is_authored_action = node.event_type == "decision" and decision_type in {
            "llm_tool_call",
            "agent_tool_call",
            "tool_call",
        }
        if is_authored_action:
            authored_action_refs.append(ref)
        if node.event_type == "decision" and decision_type == "reasoning_block":
            candidate_set.add(ref)
        elif decision_type in DELEGATION_DECISION_TYPES:
            candidate_set.add(ref)
            selected_authored_action = True
        elif action in MUTATION_ACTIONS | VERIFICATION_ACTIONS:
            candidate_set.add(ref)
            selected_authored_action = selected_authored_action or is_authored_action
        elif node.event_type in {
            "change",
            "verification",
            "tool.error",
            "mcp.call",
            "mcp.result",
            "skill.load",
        }:
            candidate_set.add(ref)
    if authored_action_refs and not selected_authored_action:
        candidate_set.add(authored_action_refs[0])
    if candidate_set:
        return [ref for ref in member_refs if ref in candidate_set]
    fallback = next(
        (
            ref
            for ref in member_refs
            if nodes[ref].event_type in {"decision", "tool.call", "mcp.call", "skill.load"}
        ),
        member_refs[0],
    )
    return [fallback]


def progress_navigation_window(nodes: Dict[str, TraceNode], current_ref: str) -> JsonDict:
    current = nodes.get(current_ref)
    if not current or current.event_type != "progress.episode":
        return {}
    episode_refs: List[str] = []
    cursor = current
    previous_delivery_ref = ""
    while cursor and cursor.event_type == "progress.episode":
        episode_refs.append(cursor.ref)
        previous_ref = str(cursor.data.get("previous_episode_ref") or "")
        previous = nodes.get(previous_ref)
        if not previous or previous.event_type != "progress.episode":
            break
        if progress_episode_has_delivery(previous):
            previous_delivery_ref = previous.ref
            break
        cursor = previous

    chronological_refs = list(reversed(episode_refs))
    previous_delivery_candidate_refs = (
        [
            str(ref)
            for ref in (
                nodes[previous_delivery_ref].data.get("candidate_member_refs")
                or nodes[previous_delivery_ref].data.get("member_refs")
                or []
            )
        ]
        if previous_delivery_ref
        else []
    )
    delivery_history_refs: List[str] = []
    history_cursor_ref = previous_delivery_ref
    while history_cursor_ref and len(delivery_history_refs) < PROGRESS_DELIVERY_HISTORY_LIMIT:
        history_cursor = nodes.get(history_cursor_ref)
        if not history_cursor or history_cursor.event_type != "progress.episode":
            break
        if progress_episode_has_delivery(history_cursor):
            delivery_history_refs.append(history_cursor.ref)
        history_cursor_ref = str(history_cursor.data.get("previous_episode_ref") or "")
    delivery_history_refs.reverse()
    delivery_history_candidate_refs = dedupe_progress_refs(
        str(ref)
        for episode_ref in delivery_history_refs
        for ref in (
            nodes[episode_ref].data.get("candidate_member_refs")
            or nodes[episode_ref].data.get("member_refs")
            or []
        )
    )
    candidate_member_refs = dedupe_progress_refs(
        [
            *previous_delivery_candidate_refs,
            *(
                str(ref)
                for episode_ref in chronological_refs
                for ref in (
                    nodes[episode_ref].data.get("candidate_member_refs")
                    or nodes[episode_ref].data.get("member_refs")
                    or []
                )
            ),
        ]
    )
    retrieval_candidate_member_refs = dedupe_progress_refs(
        [
            *delivery_history_candidate_refs,
            *candidate_member_refs,
        ]
    )
    member_refs = dedupe_progress_refs(
        str(ref)
        for episode_ref in chronological_refs
        for ref in nodes[episode_ref].data.get("member_refs") or []
    )
    return {
        "anchor_episode_ref": current_ref,
        "member_episode_refs": chronological_refs,
        "member_refs": member_refs,
        "candidate_member_refs": candidate_member_refs,
        "retrieval_candidate_member_refs": retrieval_candidate_member_refs,
        "previous_delivery_episode_ref": previous_delivery_ref,
        "previous_delivery_candidate_member_refs": previous_delivery_candidate_refs,
        "delivery_history_episode_refs": delivery_history_refs,
        "delivery_history_candidate_member_refs": delivery_history_candidate_refs,
        "delivery_history_limit": PROGRESS_DELIVERY_HISTORY_LIMIT,
        "navigation_method": "bounded_delivery_history_progress_window_v3",
        "episode_count": len(chronological_refs),
        "candidate_count": len(candidate_member_refs),
        "retrieval_candidate_count": len(retrieval_candidate_member_refs),
        "no_delivery_episode_count": sum(
            bool(nodes[ref].data.get("no_delivery_progress")) for ref in chronological_refs
        ),
        "start_chronology_index": nodes[chronological_refs[0]].data.get("chronology_index"),
        "end_chronology_index": current.data.get("chronology_index"),
    }


def active_progress_episode_data(graph: object, episode_ref: str) -> JsonDict:
    """Rebuild one progress aggregate from active-revision members only."""
    nodes = graph.nodes
    episode = nodes.get(episode_ref)
    if not episode or episode.event_type != "progress.episode":
        return {}
    raw_member_refs = [
        str(ref)
        for ref in episode.data.get("member_refs") or []
        if str(ref) in nodes
    ]
    member_refs = [
        ref
        for ref in raw_member_refs
        if graph.active_revision_evidence_eligible(ref)
    ]
    counts = progress_counts(nodes, member_refs)
    candidate_member_refs = (
        select_candidate_member_refs(nodes, member_refs) if member_refs else []
    )
    return {
        **dict(episode.data),
        "member_refs": member_refs,
        "member_summaries": [
            progress_member_summary(nodes[ref]) for ref in member_refs
        ],
        "candidate_member_refs": candidate_member_refs,
        "excluded_member_refs": [
            ref for ref in member_refs if ref not in candidate_member_refs
        ],
        "stale_member_refs": [
            ref for ref in raw_member_refs if ref not in member_refs
        ],
        "member_count": len(member_refs),
        **counts,
        "phase": progress_phase(counts),
        "no_delivery_progress": not (
            counts["mutation_count"] or counts["verification_count"]
        ),
        "start_position": (
            min(graph.position(ref) for ref in member_refs)
            if member_refs
            else None
        ),
        "end_position": (
            max(graph.position(ref) for ref in member_refs)
            if member_refs
            else None
        ),
        "start_timestamp": first_member_timestamp(nodes, member_refs),
        "end_timestamp": last_member_timestamp(nodes, member_refs),
    }


def active_progress_navigation_window(graph: object, current_ref: str) -> JsonDict:
    """Build a delivery window whose members and signals are all active."""
    nodes = graph.nodes
    current = nodes.get(current_ref)
    if not current or current.event_type != "progress.episode":
        return {}
    active_data: Dict[str, JsonDict] = {}

    def data(ref: str) -> JsonDict:
        if ref not in active_data:
            active_data[ref] = active_progress_episode_data(graph, ref)
        return active_data[ref]

    episode_refs: List[str] = []
    cursor = current
    previous_delivery_ref = ""
    while cursor and cursor.event_type == "progress.episode":
        episode_refs.append(cursor.ref)
        previous_ref = str(cursor.data.get("previous_episode_ref") or "")
        previous = nodes.get(previous_ref)
        if not previous or previous.event_type != "progress.episode":
            break
        previous_data = data(previous.ref)
        if (
            previous_data.get("mutation_count")
            or previous_data.get("verification_count")
        ):
            previous_delivery_ref = previous.ref
            break
        cursor = previous

    chronological_refs = list(reversed(episode_refs))
    previous_delivery_candidate_refs = (
        list(data(previous_delivery_ref).get("candidate_member_refs") or [])
        if previous_delivery_ref
        else []
    )
    delivery_history_refs: List[str] = []
    history_cursor_ref = previous_delivery_ref
    while (
        history_cursor_ref
        and len(delivery_history_refs) < PROGRESS_DELIVERY_HISTORY_LIMIT
    ):
        history_cursor = nodes.get(history_cursor_ref)
        if not history_cursor or history_cursor.event_type != "progress.episode":
            break
        history_data = data(history_cursor_ref)
        if (
            history_data.get("mutation_count")
            or history_data.get("verification_count")
        ):
            delivery_history_refs.append(history_cursor_ref)
        history_cursor_ref = str(
            history_cursor.data.get("previous_episode_ref") or ""
        )
    delivery_history_refs.reverse()
    delivery_history_candidate_refs = dedupe_progress_refs(
        str(ref)
        for episode_ref in delivery_history_refs
        for ref in data(episode_ref).get("candidate_member_refs") or []
    )
    candidate_member_refs = dedupe_progress_refs(
        [
            *previous_delivery_candidate_refs,
            *(
                str(ref)
                for episode_ref in chronological_refs
                for ref in data(episode_ref).get("candidate_member_refs") or []
            ),
        ]
    )
    retrieval_candidate_member_refs = dedupe_progress_refs(
        [*delivery_history_candidate_refs, *candidate_member_refs]
    )
    member_refs = dedupe_progress_refs(
        str(ref)
        for episode_ref in chronological_refs
        for ref in data(episode_ref).get("member_refs") or []
    )
    stale_member_refs = dedupe_progress_refs(
        str(ref)
        for episode_ref in chronological_refs
        for ref in data(episode_ref).get("stale_member_refs") or []
    )
    return {
        "anchor_episode_ref": current_ref,
        "member_episode_refs": chronological_refs,
        "member_refs": member_refs,
        "candidate_member_refs": candidate_member_refs,
        "retrieval_candidate_member_refs": retrieval_candidate_member_refs,
        "previous_delivery_episode_ref": previous_delivery_ref,
        "previous_delivery_candidate_member_refs": previous_delivery_candidate_refs,
        "delivery_history_episode_refs": delivery_history_refs,
        "delivery_history_candidate_member_refs": delivery_history_candidate_refs,
        "delivery_history_limit": PROGRESS_DELIVERY_HISTORY_LIMIT,
        "navigation_method": "bounded_delivery_history_progress_window_v4",
        "episode_count": len(chronological_refs),
        "candidate_count": len(candidate_member_refs),
        "retrieval_candidate_count": len(retrieval_candidate_member_refs),
        "no_delivery_episode_count": sum(
            bool(data(ref).get("no_delivery_progress"))
            for ref in chronological_refs
        ),
        "start_chronology_index": data(chronological_refs[0]).get(
            "chronology_index"
        ),
        "end_chronology_index": data(current_ref).get("chronology_index"),
        "stale_member_refs": stale_member_refs,
    }


def progress_episode_has_delivery(node: TraceNode) -> bool:
    return bool(
        int(node.data.get("mutation_count") or 0)
        or int(node.data.get("verification_count") or 0)
    )


def dedupe_progress_refs(refs: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        output.append(ref)
    return output


def stable_progress_episode_id(member_refs: Iterable[str]) -> str:
    canonical = "\n".join(member_refs)
    return f"progress_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def first_member_timestamp(nodes: Dict[str, TraceNode], member_refs: List[str]) -> str:
    values = [nodes[ref].timestamp for ref in member_refs if nodes[ref].timestamp]
    return min(values) if values else ""


def last_member_timestamp(nodes: Dict[str, TraceNode], member_refs: List[str]) -> str:
    values = [nodes[ref].timestamp for ref in member_refs if nodes[ref].timestamp]
    return max(values) if values else ""


def episode_precedes_target(episode: TraceNode, target: TraceNode, target_position: int) -> bool:
    episode_timestamp = str(episode.data.get("end_timestamp") or "")
    target_timestamp = str(target.timestamp or "")
    if episode_timestamp and target_timestamp:
        return episode_timestamp < target_timestamp
    return int(episode.data.get("end_position") or -1) < target_position
