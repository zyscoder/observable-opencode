"""Question-scoped expected-action facts derived without changing the Trace."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from .graph import TraceGraph


_SKILL_QUESTION = re.compile(r"(?:\bskills?\b|技能)", re.IGNORECASE)
_REQUEST_EVENTS = frozenset(
    {"message.input", "prompt.assembly", "request.received"}
)
_CATALOG_EVENT = "skill.catalog.exposed"
_INVOCATION_EVENTS = frozenset({"skill.load", "skill.call", "skill.result"})
_EXECUTION_WINDOW_EVENTS = frozenset(
    {
        "llm.call",
        "llm.turn",
        "decision",
        "tool.call",
        "tool.result",
        "tool.error",
        "mcp.call",
        "skill.load",
        "subagent.call",
        "response.output",
        "final.claim",
        "exit.gate",
    }
)
_MAX_REQUEST_REFS = 8
_MAX_CATALOGS = 8
_MAX_SKILLS = 64
_MAX_EXECUTION_WINDOW_REFS = 24
_MAX_INVOCATION_REFS = 16
_GENERIC_MATCH_TERMS = frozenset(
    {
        "skill",
        "skills",
        "use",
        "using",
        "call",
        "invoke",
        "load",
        "tool",
        "agent",
        "使用",
        "调用",
        "技能",
        "进行",
        "结果",
        "输出",
        "为什么",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _strings(values: Sequence[Any]) -> list[str]:
    output = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _text_fragments(value: Any, *, limit: int = 64) -> list[str]:
    output: list[str] = []

    def visit(item: Any) -> None:
        if len(output) >= limit:
            return
        if isinstance(item, str):
            text = item.strip()
            if text:
                output.append(text[:4_000])
            return
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return output


def _lexical_units(value: str) -> set[str]:
    text = str(value or "").lower()
    units = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_.-]{1,}", text)
        if token not in _GENERIC_MATCH_TERMS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        for size in range(2, min(6, len(run)) + 1):
            units.update(
                run[index : index + size]
                for index in range(0, len(run) - size + 1)
                if run[index : index + size] not in _GENERIC_MATCH_TERMS
            )
    return units


def _expected_skill_candidates(
    candidates: Sequence[Mapping[str, Any]], query_text: str
) -> list[dict[str, Any]]:
    query_units = _lexical_units(query_text)
    query_lower = query_text.lower()
    ranked = []
    for item in candidates:
        name = str(item.get("name") or "").strip()
        if not name or item.get("status") == "parse_failed":
            continue
        description = str(item.get("description") or "").strip()
        matched_terms = sorted(
            query_units.intersection(_lexical_units(" ".join((name, description))))
        )
        exact_name_match = name.lower() in query_lower
        score = len(matched_terms) + (100 if exact_name_match else 0)
        if score < 2 and not exact_name_match:
            continue
        ranked.append(
            {
                "name": name,
                "description": description,
                "location": str(item.get("location") or ""),
                "source_family": str(item.get("source_family") or ""),
                "source_scope": str(item.get("source_scope") or ""),
                "score": score,
                "matched_terms": matched_terms[:16],
                "match_basis": (
                    "exact_skill_name" if exact_name_match else "request_description_overlap"
                ),
                "catalog_ref": str(item.get("catalog_ref") or ""),
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["name"]))
    if not ranked:
        return []
    highest = ranked[0]["score"]
    return [item for item in ranked if item["score"] >= max(2, highest * 0.6)][:_MAX_SKILLS]


def _skill_name(node: Any) -> str:
    data = _mapping(getattr(node, "data", {}))
    for key in ("skill_name", "name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    args = _mapping(data.get("args"))
    value = str(args.get("name") or "").strip()
    if value:
        return value
    return str(getattr(node, "title", "") or "").strip()


def _catalog_field(
    graph: TraceGraph,
    ref: str,
    node: Any,
    key: str,
) -> Sequence[Any]:
    raw = _mapping(node.data).get(key)
    if isinstance(raw, (list, tuple)):
        return raw
    summary = _mapping(raw)
    artifact_id = str(summary.get("artifact_id") or "")
    if not artifact_id:
        return ()
    hydrated = graph.hydrate_node(ref)
    for artifact in _sequence(_mapping(hydrated.data).get("hydrated_artifacts")):
        item = _mapping(artifact)
        if str(item.get("artifact_id") or "") != artifact_id:
            continue
        try:
            value = json.loads(str(item.get("content") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return value if isinstance(value, list) else ()
    return ()


def _catalog_entries(
    graph: TraceGraph,
    catalogs: Sequence[tuple[str, Any]],
    key: str,
) -> list[dict]:
    output = []
    seen = set()
    for ref, node in catalogs:
        for raw in _catalog_field(graph, ref, node, key):
            item = _mapping(raw)
            name = str(item.get("name") or "").strip()
            location = str(item.get("location") or "").strip()
            identity = (ref, key, name, location, str(item.get("error") or ""))
            if identity in seen:
                continue
            seen.add(identity)
            output.append({**dict(item), "catalog_ref": ref})
            if len(output) >= _MAX_SKILLS:
                return output
    return output


def build_expected_action_projection(
    graph: TraceGraph, question: str
) -> Optional[dict[str, Any]]:
    """Collect bounded Skill invocation facts for a Skill-oriented question."""
    if not _SKILL_QUESTION.search(str(question or "")):
        return None

    ordered = sorted(graph.nodes.items(), key=lambda item: graph.position(item[0]))
    requests = [
        (ref, node) for ref, node in ordered if node.event_type in _REQUEST_EVENTS
    ][-_MAX_REQUEST_REFS:]
    catalogs = [
        (ref, node) for ref, node in ordered if node.event_type == _CATALOG_EVENT
    ][-_MAX_CATALOGS:]
    invocations = [
        (ref, node) for ref, node in ordered if node.event_type in _INVOCATION_EVENTS
    ]
    first_catalog_position = (
        graph.position(catalogs[0][0]) if catalogs else len(ordered)
    )
    execution_window = [
        (ref, node)
        for ref, node in ordered
        if graph.position(ref) > first_catalog_position
        and node.event_type in _EXECUTION_WINDOW_EVENTS
    ][-_MAX_EXECUTION_WINDOW_REFS:]

    candidates = _catalog_entries(graph, catalogs, "candidates")
    exposed = _catalog_entries(graph, catalogs, "exposed")
    selected = _catalog_entries(graph, catalogs, "selected")
    permission_evaluations = _catalog_entries(
        graph, catalogs, "permission_evaluations"
    )
    parse_failures = _catalog_entries(graph, catalogs, "parse_failures")
    conflicts = _catalog_entries(graph, catalogs, "conflicts")
    invoked_names = _strings([_skill_name(node) for _, node in invocations])
    candidate_names = _strings(
        [item.get("name") for item in candidates if item.get("status") != "parse_failed"]
    )
    exposed_names = _strings([item.get("name") for item in exposed])
    selected_names = _strings([item.get("name") for item in selected])
    request_text = "\n".join(
        fragment
        for _, node in requests
        for fragment in _text_fragments(node.data)
    )
    expected_candidates = _expected_skill_candidates(
        candidates,
        "\n".join((str(question), request_text)),
    )
    expected_names = [item["name"] for item in expected_candidates]
    matching_invocations = sorted(set(invoked_names).intersection(expected_names))
    matching_invocation_entries = [
        (ref, node)
        for ref, node in invocations
        if _skill_name(node) in set(expected_names)
    ]
    bounded_invocation_entries = []
    bounded_invocation_seen = set()
    for ref, node in [
        *matching_invocation_entries,
        *invocations[-_MAX_INVOCATION_REFS:],
    ]:
        if ref in bounded_invocation_seen:
            continue
        bounded_invocation_seen.add(ref)
        bounded_invocation_entries.append((ref, node))
    selected_name_set = set(selected_names)
    exposed_name_set = set(exposed_names)
    invoked_name_set = set(invoked_names)
    permission_by_name = {
        str(item.get("name") or ""): str(item.get("action") or "unknown")
        for item in permission_evaluations
        if str(item.get("name") or "")
    }
    skill_tool_available = any(
        bool(_mapping(node.data).get("skill_tool_available"))
        for _, node in catalogs
    )
    expected_skill_lifecycle = []
    for item in expected_candidates:
        name = item["name"]
        selected_for_model = name in selected_name_set
        exposed_to_model = name in exposed_name_set
        invoked = name in invoked_name_set
        first_absent_stage = (
            "selection"
            if not selected_for_model
            else "exposure"
            if not exposed_to_model
            else "tool_availability"
            if not skill_tool_available
            else "invocation"
            if not invoked
            else "none"
        )
        expected_skill_lifecycle.append(
            {
                "name": name,
                "location": item["location"],
                "source_family": item["source_family"],
                "source_scope": item["source_scope"],
                "discovered": True,
                "selected": selected_for_model,
                "exposed": exposed_to_model,
                "permission_action": permission_by_name.get(name, "unknown"),
                "skill_tool_available": skill_tool_available,
                "invoked": invoked,
                "first_absent_stage": first_absent_stage,
                "trigger_match_basis": item["match_basis"],
            }
        )
    evidence_refs = _strings(
        [ref for ref, _ in requests]
        + [ref for ref, _ in catalogs]
        + [ref for ref, _ in execution_window]
        + [ref for ref, _ in bounded_invocation_entries]
    )

    return {
        "schema": "expected-action-projection/v1",
        "action_kind": "skill.invoke",
        "question": str(question),
        "observed_action": (
            "present"
            if matching_invocations
            else "absent"
            if expected_names
            else "unknown"
        ),
        "request_refs": [ref for ref, _ in requests],
        "catalog_refs": [ref for ref, _ in catalogs],
        "invocation_refs": [ref for ref, _ in bounded_invocation_entries],
        "execution_window_refs": [ref for ref, _ in execution_window],
        "candidate_skill_names": candidate_names,
        "selected_skill_names": selected_names,
        "exposed_skill_names": exposed_names,
        "expected_skill_names": expected_names,
        "expected_skill_candidates": expected_candidates,
        "expected_skill_lifecycle": expected_skill_lifecycle,
        "invoked_skill_names": invoked_names,
        "matching_invoked_skill_names": matching_invocations,
        "candidates": candidates,
        "exposed": exposed,
        "permission_evaluations": [
            {key: value for key, value in item.items() if key != "catalog_ref"}
            for item in permission_evaluations
        ],
        "parse_failures": [
            {key: value for key, value in item.items() if key != "catalog_ref"}
            for item in parse_failures
        ],
        "conflicts": conflicts,
        "skill_tool_available": skill_tool_available,
        "evidence_refs": evidence_refs,
        "analysis_mode": "offline_read_only",
    }
