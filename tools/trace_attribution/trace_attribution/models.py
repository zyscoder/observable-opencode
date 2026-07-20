from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]
DEFECT_STATUSES = {"present", "absent", "unknown"}
CAUSAL_ROLES = {
    "defect_introduction",
    "defect_propagation",
    "defect_evidence",
    "non_defective",
    "unknown",
}
INFLUENCE_RELATIONS = {
    "defect_propagated_from",
    "motivated_by_evidence",
    "derived_from",
}
BRANCH_RELATIONS = {
    "same_defect",
    "causal_precursor",
    "outcome_evidence",
    "unrelated",
    "unknown",
}


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
        for source_limit in (12, 6, 3, 0):
            for text_limit, collection_limit in ((1000, 12), (700, 8), (420, 4), (240, 2)):
                semantic_data = compact_semantic_data(
                    self.data,
                    text_limit=text_limit,
                    collection_limit=collection_limit,
                )
                if not semantic_data:
                    break
                candidate = {
                    "ref": self.ref,
                    "record_id": self.record_id,
                    "component": self.component,
                    "event_type": self.event_type,
                    "title": self.title,
                    "status": self.status,
                    "source_refs": self.source_refs[:source_limit],
                    "data": semantic_data,
                    "truncated": True,
                }
                if len(stable_json(candidate)) <= max_chars:
                    return candidate
        for source_limit in (20, 12, 6, 3, 0):
            candidate = compact_truncated_payload(
                base={
                    "ref": self.ref,
                    "record_id": self.record_id,
                    "component": self.component,
                    "event_type": self.event_type,
                    "title": self.title,
                    "status": self.status,
                    "source_refs": self.source_refs[:source_limit],
                    "truncated": True,
                },
                source_text=text,
                max_chars=max_chars,
            )
            if len(stable_json(candidate)) <= max_chars:
                return candidate
        return {
            "ref": self.ref,
            "record_id": self.record_id,
            "component": self.component,
            "event_type": self.event_type,
            "truncated": True,
        }


SEMANTIC_DATA_KEYS = (
    "text",
    "rationale",
    "decision_type",
    "chosen_action",
    "intent",
    "action",
    "output_text",
    "response_text",
    "summary",
    "claim",
    "description",
    "reason",
    "defect_reason",
    "structured_claim",
    "quality_flags",
    "attribution_summary",
    "direct_evidence_refs",
    "direct_support_refs",
    "candidate_context_refs",
    "superseded_evidence_refs",
    "grounding_candidate_refs",
    "grounding_decisions",
    "grounding_method",
    "grounding_behavior_impact",
    "claim_kind",
    "temporal_scope",
    "repository_revision",
    "effective_for_final_state",
    "verification_phase",
    "verification_refs",
    "verification_repository_revision",
    "verification_status",
    "verification_effective_for_final_state",
    "verification_temporal_role",
    "verification_supersedes_refs",
    "verification_superseded_by_refs",
    "episode_id",
    "phase",
    "member_refs",
    "member_summaries",
    "candidate_member_refs",
    "excluded_member_refs",
    "candidate_selection_method",
    "previous_episode_ref",
    "reasoning_count",
    "action_count",
    "search_read_count",
    "mutation_count",
    "verification_count",
    "delegation_count",
    "error_count",
    "no_delivery_progress",
    "cumulative_change_count",
    "cumulative_verification_count",
    "consecutive_no_delivery_episodes",
    "offline_only",
    "collection_mode",
    "behavior_impact",
    "chronology_index",
    "start_timestamp",
    "end_timestamp",
    "revision_before",
    "revision_after",
    "hydrated_artifacts",
    "selected_context_refs",
    "context_refs",
    "message_transforms",
    "algorithm",
    "algorithm_version",
    "trigger",
    "input_message_count",
    "compaction_request_message_count",
    "output_message_count",
    "tool_name",
    "error_kind",
    "error_message",
    "handled_status",
    "observed_by_model",
    "command",
    "path",
    "diff",
    "files",
    "verification_id",
    "exit_code",
)


def compact_semantic_data(data: JsonDict, *, text_limit: int, collection_limit: int) -> JsonDict:
    output: JsonDict = {}
    for key in SEMANTIC_DATA_KEYS:
        if key not in data:
            continue
        value = data[key]
        if value is None or value == "" or value == [] or value == {}:
            continue
        output[key] = compact_semantic_value(
            value,
            text_limit=text_limit,
            collection_limit=collection_limit,
            depth=0,
        )
    return output


def compact_semantic_value(value: Any, *, text_limit: int, collection_limit: int, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) <= text_limit:
            return value
        return value[: max(0, text_limit - 3)] + "..."
    if isinstance(value, list):
        return [
            compact_semantic_value(
                item,
                text_limit=min(text_limit, 240),
                collection_limit=collection_limit,
                depth=depth + 1,
            )
            for item in value[:collection_limit]
        ]
    if isinstance(value, dict):
        if depth >= 3:
            return short_stable_json(value, text_limit)
        return {
            str(key): compact_semantic_value(
                item,
                text_limit=min(text_limit, 360),
                collection_limit=collection_limit,
                depth=depth + 1,
            )
            for key, item in list(value.items())[:collection_limit]
        }
    return value


def short_stable_json(value: Any, limit: int) -> str:
    text = stable_json(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


@dataclass(frozen=True)
class TaintInfluence:
    upstream_ref: str
    reason: str
    confidence: float = 0.0
    relation: str = "defect_propagated_from"


@dataclass(frozen=True)
class NodeJudgment:
    node_ref: str
    component: str
    event_type: str
    has_defect: bool
    defect_status: str = ""
    defect_type: str = ""
    defect_reason: str = ""
    causal_role: str = ""
    branch_relation: str = ""
    influenced_by: List[TaintInfluence] = field(default_factory=list)
    is_root_cause: bool = False
    severity: str = "unknown"
    confidence: float = 0.0
    model_notes: str = ""

    def __post_init__(self) -> None:
        status = self.defect_status.strip().lower()
        if status not in DEFECT_STATUSES:
            status = "present" if self.has_defect else "absent"
        role = self.causal_role.strip().lower()
        if role not in CAUSAL_ROLES:
            if status == "absent":
                role = "non_defective"
            elif status == "unknown":
                role = "unknown"
            elif self.is_root_cause:
                role = "defect_introduction"
            elif self.influenced_by:
                role = "defect_propagation"
            else:
                role = "unknown"
        object.__setattr__(self, "defect_status", status)
        object.__setattr__(self, "has_defect", status == "present")
        object.__setattr__(self, "causal_role", role)
        branch_relation = self.branch_relation.strip().lower()
        if branch_relation not in BRANCH_RELATIONS:
            if status == "absent":
                branch_relation = "unrelated"
            elif status == "unknown":
                branch_relation = "unknown"
            elif role == "defect_evidence":
                branch_relation = "outcome_evidence"
            else:
                branch_relation = "same_defect"
        object.__setattr__(self, "branch_relation", branch_relation)


@dataclass(frozen=True)
class RootCauseCandidate:
    node_ref: str
    component: str
    event_type: str
    defect_type: str
    reason: str
    confidence: float
    causal_role: str = "defect_introduction"
    episode_id: str = ""
    episode_member_refs: List[str] = field(default_factory=list)
    observed_defect_refs: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DefectBranchResult:
    branch_id: str
    start_ref: str
    defect_type: str
    analysis_outcome: str
    root_causes: List[RootCauseCandidate]
    taint_paths: List[List[str]]
    node_judgments: Dict[str, NodeJudgment]
    visited_order: List[str]
    unresolved_refs: List[str]
    metadata: JsonDict = field(default_factory=dict)


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
    defect_branches: List[DefectBranchResult] = field(default_factory=list)
    trace_improvement_report: JsonDict = field(default_factory=dict)
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


def stable_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def compact_truncated_payload(*, base: JsonDict, source_text: str, max_chars: int) -> JsonDict:
    candidate = dict(base)
    candidate["data_preview"] = ""
    preview_budget = max(0, max_chars - len(stable_json(candidate)))
    while preview_budget >= 0:
        candidate["data_preview"] = source_text[:preview_budget]
        size = len(stable_json(candidate))
        if size <= max_chars:
            return candidate
        if preview_budget == 0:
            break
        preview_budget = max(0, preview_budget - (size - max_chars) - 1)
    candidate["data_preview"] = ""
    return candidate


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
                relation=str(item.get("relation") or "defect_propagated_from"),
            )
        )
    raw_status = str(value.get("defect_status") or "").strip().lower()
    if raw_status not in DEFECT_STATUSES:
        raw_has_defect = value.get("has_defect")
        if isinstance(raw_has_defect, bool):
            raw_status = "present" if raw_has_defect else "absent"
        else:
            raw_status = "unknown"
    return NodeJudgment(
        node_ref=str(value.get("node_ref") or fallback_node.ref),
        component=str(value.get("component") or fallback_node.component),
        event_type=str(value.get("event_type") or fallback_node.event_type),
        has_defect=raw_status == "present",
        defect_status=raw_status,
        defect_type=str(value.get("defect_type") or ""),
        defect_reason=str(value.get("defect_reason") or value.get("reason") or ""),
        causal_role=str(value.get("causal_role") or ""),
        branch_relation=str(value.get("branch_relation") or ""),
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
