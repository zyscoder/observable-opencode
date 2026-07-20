from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import JsonDict, TraceNode


MIN_MATCH_CHARS = 48
MAX_MATCHED_DECISIONS_PER_REQUEST = 24
MAX_DECISION_CANDIDATES_PER_REQUEST = 96
MAX_ARTIFACT_CHARS = 2_000_000


def reconstruct_message_lineage(
    *,
    trace: JsonDict,
    nodes: Dict[str, TraceNode],
    aliases: Dict[str, str],
    artifact_root: Optional[Path],
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

    artifact_index = {
        str(item.get("artifact_id")): item
        for item in trace.get("artifacts") or []
        if isinstance(item, dict) and item.get("artifact_id")
    }
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
                artifact_index=artifact_index,
                artifact_root=artifact_root,
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
    artifact_index: Dict[str, JsonDict],
    artifact_root: Optional[Path],
) -> Tuple[str, str]:
    artifact = artifact_index.get(artifact_id)
    if not artifact or not artifact.get("path"):
        return "", "artifact_not_indexed"
    if artifact_root is None:
        return "", "artifact_root_missing"
    root = Path(artifact_root).resolve()
    candidate = (root / str(artifact["path"])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return "", "artifact_path_outside_trace_root"
    try:
        content = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", "artifact_unreadable"
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
