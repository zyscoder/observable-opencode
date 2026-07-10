from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass(frozen=True)
class TraceNode:
    ref: str
    record_id: str
    component: str
    event_type: str
    title: str = ""
    status: str = ""
    timestamp: str = ""
    data: JsonDict = field(default_factory=dict)
    source_refs: List[str] = field(default_factory=list)

    def compact(self, max_chars: int = 2400) -> JsonDict:
        payload = {
            "ref": self.ref,
            "record_id": self.record_id,
            "component": self.component,
            "event_type": self.event_type,
            "title": self.title,
            "status": self.status,
            "source_refs": self.source_refs[:20],
            "data": self.data,
        }
        text = stable_json(payload)
        if len(text) <= max_chars:
            return payload
        return {
            "ref": self.ref,
            "record_id": self.record_id,
            "component": self.component,
            "event_type": self.event_type,
            "title": self.title,
            "status": self.status,
            "source_refs": self.source_refs[:20],
            "data_preview": text[:max_chars],
            "truncated": True,
        }


@dataclass(frozen=True)
class TaintInfluence:
    upstream_ref: str
    reason: str
    confidence: float = 0.0


@dataclass(frozen=True)
class NodeJudgment:
    node_ref: str
    component: str
    event_type: str
    has_defect: bool
    defect_type: str = ""
    defect_reason: str = ""
    influenced_by: List[TaintInfluence] = field(default_factory=list)
    is_root_cause: bool = False
    severity: str = "unknown"
    confidence: float = 0.0
    model_notes: str = ""


@dataclass(frozen=True)
class RootCauseCandidate:
    node_ref: str
    component: str
    event_type: str
    defect_type: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class AttributionReport:
    case_id: str
    objective: str
    start_refs: List[str]
    root_causes: List[RootCauseCandidate]
    taint_paths: List[List[str]]
    node_judgments: Dict[str, NodeJudgment]
    visited_order: List[str]
    unresolved_refs: List[str]
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def stable_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def judgment_from_dict(value: JsonDict, fallback_node: TraceNode) -> NodeJudgment:
    influences = []
    for item in value.get("influenced_by") or []:
        if not isinstance(item, dict):
            continue
        upstream = str(item.get("upstream_ref") or item.get("ref") or "").strip()
        if not upstream:
            continue
        influences.append(
            TaintInfluence(
                upstream_ref=upstream,
                reason=str(item.get("reason") or ""),
                confidence=float_or_zero(item.get("confidence")),
            )
        )
    return NodeJudgment(
        node_ref=str(value.get("node_ref") or fallback_node.ref),
        component=str(value.get("component") or fallback_node.component),
        event_type=str(value.get("event_type") or fallback_node.event_type),
        has_defect=bool(value.get("has_defect")),
        defect_type=str(value.get("defect_type") or ""),
        defect_reason=str(value.get("defect_reason") or value.get("reason") or ""),
        influenced_by=influences,
        is_root_cause=bool(value.get("is_root_cause")),
        severity=str(value.get("severity") or "unknown"),
        confidence=float_or_zero(value.get("confidence")),
        model_notes=str(value.get("model_notes") or ""),
    )


def float_or_zero(value: Optional[Any]) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
