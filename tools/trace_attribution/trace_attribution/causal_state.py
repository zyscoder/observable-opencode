"""Serializable state for recursive offline causal attribution."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional, Tuple

from .models import JsonDict, TraceNode, stable_json


CAUSAL_RELATIONS = frozenset(
    {
        "same_defect_propagation",
        "defect_transformation",
        "introduction_candidate",
        "contributing_condition",
        "outcome_evidence",
        "unrelated",
        "unknown",
    }
)
HYPOTHESIS_STATUSES = frozenset({"active", "supported", "rejected", "superseded", "unresolved"})
CONFIRMATION_STATUSES = frozenset({"confirmed", "rejected", "unknown"})
BLOCKING_METADATA_KEYS = frozenset(
    {
        "unresolved_reason",
        "unresolved_reasons",
        "unresolved_refs",
        "missing_evidence",
        "missing_artifact",
        "provider_error",
        "provider_unavailable",
        "provider_circuit_open",
        "blocked",
        "blocking_reason",
    }
)


class FrozenMapping(Mapping[str, Any]):
    """Immutable JSON mapping backed by recursively frozen key/value entries."""

    __slots__ = ("_entries",)

    def __init__(self, value: Optional[Mapping[str, Any]] = None) -> None:
        self._entries = tuple((str(key), _freeze(item)) for key, item in (value or {}).items())

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._entries:
            if candidate == key:
                return value
        raise KeyError(key)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __repr__(self) -> str:
        return repr(dict(self.items()))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _frozen_strings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _has_blocking_metadata(metadata: Mapping[str, Any]) -> bool:
    for raw_key, value in metadata.items():
        if not value:
            continue
        key = str(raw_key).strip().lower()
        if key in BLOCKING_METADATA_KEYS:
            return True
        if key.endswith("_budget_exhausted") or key.endswith("_blocked"):
            return True
        if key.startswith("missing_") or key.endswith("_missing"):
            return True
    return False


def _assessment_is_unresolved(assessment: "PredecessorAssessment") -> bool:
    return assessment.relation == "unknown" or bool(assessment.missing_evidence)


def _has_unresolved_judgment_state(
    step_judgments: Tuple["CausalStepJudgment", ...],
    causal_relations: Tuple["PredecessorAssessment", ...],
) -> bool:
    latest_steps = {}
    for judgment in step_judgments:
        latest_steps[judgment.current_node_ref] = judgment
    latest_nested_assessments = {}
    for judgment in latest_steps.values():
        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            return True
        for assessment in judgment.predecessors:
            latest_nested_assessments[(judgment.current_node_ref, assessment.ref)] = assessment
    if any(_assessment_is_unresolved(item) for item in latest_nested_assessments.values()):
        return True

    latest_relations = {}
    for assessment in causal_relations:
        latest_relations[assessment.ref] = assessment
    return any(_assessment_is_unresolved(item) for item in latest_relations.values())


def _hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _json_dict(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _trace_node_to_dict(node: TraceNode) -> JsonDict:
    return {
        "ref": node.ref,
        "record_id": node.record_id,
        "component": node.component,
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
        "timestamp": node.timestamp,
        "data": _thaw(node.data),
        "source_refs": list(node.source_refs),
    }


def _trace_node_from_dict(value: JsonDict) -> TraceNode:
    return TraceNode(
        ref=str(value.get("ref") or ""),
        record_id=str(value.get("record_id") or ""),
        component=str(value.get("component") or ""),
        event_type=str(value.get("event_type") or ""),
        title=str(value.get("title") or ""),
        status=str(value.get("status") or ""),
        timestamp=str(value.get("timestamp") or ""),
        data=_json_dict(value.get("data")),
        source_refs=_string_list(value.get("source_refs")),
    )


def _freeze_trace_node(node: TraceNode) -> TraceNode:
    return TraceNode(
        ref=node.ref,
        record_id=node.record_id,
        component=node.component,
        event_type=node.event_type,
        title=node.title,
        status=node.status,
        timestamp=node.timestamp,
        data=FrozenMapping(_thaw(node.data)),
        source_refs=_frozen_strings(node.source_refs),
    )


@dataclass(frozen=True)
class DefectState:
    defect_state_id: str
    label: str
    expected: str
    actual: str
    mechanism: str
    scope: str
    fingerprint: str
    derived_from_defect_state_id: str = ""
    transformation_reason: str = ""

    @classmethod
    def create(
        cls,
        label: str,
        expected: str,
        actual: str,
        mechanism: str,
        scope: str,
        *,
        derived_from_defect_state_id: str = "",
        transformation_reason: str = "",
    ) -> "DefectState":
        semantic = {
            "label": label,
            "expected": expected,
            "actual": actual,
            "mechanism": mechanism,
            "scope": scope,
            "derived_from_defect_state_id": derived_from_defect_state_id,
            "transformation_reason": transformation_reason,
        }
        fingerprint = _hash(semantic)[:20]
        return cls(defect_state_id="defect:{0}".format(fingerprint), fingerprint=fingerprint, **semantic)

    def transformed(
        self,
        *,
        label: str,
        mechanism: str,
        transformation_reason: str,
        expected: Optional[str] = None,
        actual: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> "DefectState":
        return self.create(
            label=label,
            expected=self.expected if expected is None else expected,
            actual=self.actual if actual is None else actual,
            mechanism=mechanism,
            scope=self.scope if scope is None else scope,
            derived_from_defect_state_id=self.defect_state_id,
            transformation_reason=transformation_reason,
        )

    def to_dict(self) -> JsonDict:
        return {
            "defect_state_id": self.defect_state_id,
            "label": self.label,
            "expected": self.expected,
            "actual": self.actual,
            "mechanism": self.mechanism,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
            "derived_from_defect_state_id": self.derived_from_defect_state_id,
            "transformation_reason": self.transformation_reason,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "DefectState":
        state = cls.create(
            label=str(value.get("label") or ""),
            expected=str(value.get("expected") or ""),
            actual=str(value.get("actual") or ""),
            mechanism=str(value.get("mechanism") or ""),
            scope=str(value.get("scope") or ""),
            derived_from_defect_state_id=str(value.get("derived_from_defect_state_id") or ""),
            transformation_reason=str(value.get("transformation_reason") or ""),
        )
        fingerprint = str(value.get("fingerprint") or "")
        if fingerprint and fingerprint != state.fingerprint:
            raise ValueError("DefectState fingerprint does not match semantic fields")
        defect_state_id = str(value.get("defect_state_id") or "")
        if defect_state_id and defect_state_id != state.defect_state_id:
            raise ValueError("DefectState defect_state_id does not match semantic fields")
        return state


def semantic_visit_key(node_ref: str, defect_state: DefectState, hypothesis_semantic_hash: str) -> str:
    return _hash(
        {
            "node_ref": node_ref,
            "defect_fingerprint": defect_state.fingerprint,
            "hypothesis_semantic_hash": hypothesis_semantic_hash,
        }
    )


@dataclass(frozen=True)
class CausalCandidate:
    ref: str
    node: TraceNode
    source: str
    edge: JsonDict = field(default_factory=FrozenMapping)
    score: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node", _freeze_trace_node(self.node))
        object.__setattr__(self, "edge", FrozenMapping(_thaw(self.edge)))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "node": _trace_node_to_dict(self.node),
            "source": self.source,
            "edge": _thaw(self.edge),
            "score": self.score,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalCandidate":
        return cls(
            ref=str(value.get("ref") or ""),
            node=_trace_node_from_dict(_json_dict(value.get("node"))),
            source=str(value.get("source") or ""),
            edge=_json_dict(value.get("edge")),
            score=_float(value.get("score")),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class PredecessorAssessment:
    ref: str
    relation: str = "unknown"
    reason: str = ""
    confidence: float = 0.0
    recurse: bool = False
    upstream_defect: Optional[DefectState] = None
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "missing_evidence", _frozen_strings(self.missing_evidence))

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "relation": self.relation,
            "reason": self.reason,
            "confidence": self.confidence,
            "recurse": self.recurse,
            "upstream_defect": self.upstream_defect.to_dict() if self.upstream_defect else None,
            "evidence_refs": list(self.evidence_refs),
            "missing_evidence": list(self.missing_evidence),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "PredecessorAssessment":
        upstream = value.get("upstream_defect")
        return cls(
            ref=str(value.get("ref") or ""),
            relation=str(value.get("relation") or "unknown"),
            reason=str(value.get("reason") or ""),
            confidence=_float(value.get("confidence")),
            recurse=bool(value.get("recurse")),
            upstream_defect=DefectState.from_dict(upstream) if isinstance(upstream, dict) else None,
            evidence_refs=_string_list(value.get("evidence_refs")),
            missing_evidence=_string_list(value.get("missing_evidence")),
        )


@dataclass(frozen=True)
class CausalStepJudgment:
    current_node_ref: str
    current_defect_status: str
    current_defect_reason: str
    predecessors: Tuple[PredecessorAssessment, ...] = field(default_factory=tuple)
    candidate_introduction: bool = False
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)
    suggested_investigation: Optional[JsonDict] = None
    confidence: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "predecessors", tuple(self.predecessors))
        object.__setattr__(self, "missing_evidence", _frozen_strings(self.missing_evidence))
        if self.suggested_investigation is not None:
            object.__setattr__(
                self, "suggested_investigation", FrozenMapping(_thaw(self.suggested_investigation))
            )

    def to_dict(self) -> JsonDict:
        return {
            "current_node_ref": self.current_node_ref,
            "current_defect_status": self.current_defect_status,
            "current_defect_reason": self.current_defect_reason,
            "predecessors": [item.to_dict() for item in self.predecessors],
            "candidate_introduction": self.candidate_introduction,
            "missing_evidence": list(self.missing_evidence),
            "suggested_investigation": _thaw(self.suggested_investigation)
            if self.suggested_investigation
            else None,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalStepJudgment":
        predecessors = value.get("predecessors")
        return cls(
            current_node_ref=str(value.get("current_node_ref") or ""),
            current_defect_status=str(value.get("current_defect_status") or "unknown"),
            current_defect_reason=str(value.get("current_defect_reason") or ""),
            predecessors=[PredecessorAssessment.from_dict(item) for item in predecessors if isinstance(item, dict)]
            if isinstance(predecessors, list)
            else [],
            candidate_introduction=bool(value.get("candidate_introduction")),
            missing_evidence=_string_list(value.get("missing_evidence")),
            suggested_investigation=_json_dict(value.get("suggested_investigation"))
            if isinstance(value.get("suggested_investigation"), dict)
            else None,
            confidence=_float(value.get("confidence")),
        )


@dataclass(frozen=True)
class FrontierItem:
    item_id: str
    node_ref: str
    defect_state: DefectState
    downstream_path: Tuple[str, ...]
    hypothesis_id: str
    hypothesis_semantic_hash: str
    depth: int = 0
    candidate_source: str = ""
    priority: float = 0.0
    checked_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    evidence_hash: str = ""
    reopen_reason: str = ""
    graph_position: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "downstream_path", _frozen_strings(self.downstream_path))
        object.__setattr__(self, "checked_evidence_refs", _frozen_strings(self.checked_evidence_refs))

    @classmethod
    def create(
        cls,
        *,
        node_ref: str,
        defect_state: DefectState,
        downstream_path: List[str],
        hypothesis_id: str,
        hypothesis_semantic_hash: str,
        depth: int = 0,
        candidate_source: str = "",
        priority: float = 0.0,
        checked_evidence_refs: Optional[List[str]] = None,
        evidence_hash: str = "",
        reopen_reason: str = "",
        graph_position: int = 0,
    ) -> "FrontierItem":
        item_id = "frontier:{0}".format(
            _hash(
                {
                    "node_ref": node_ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "downstream_path": downstream_path,
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis_semantic_hash,
                    "depth": depth,
                }
            )[:20]
        )
        return cls(
            item_id=item_id,
            node_ref=node_ref,
            defect_state=defect_state,
            downstream_path=tuple(downstream_path),
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis_semantic_hash,
            depth=depth,
            candidate_source=candidate_source,
            priority=priority,
            checked_evidence_refs=tuple(checked_evidence_refs or []),
            evidence_hash=evidence_hash,
            reopen_reason=reopen_reason,
            graph_position=graph_position,
        )

    @property
    def visit_key(self) -> str:
        return semantic_visit_key(self.node_ref, self.defect_state, self.hypothesis_semantic_hash)

    @property
    def heap_key(self) -> tuple:
        return (-self.priority, self.depth, self.graph_position, self.item_id)

    def to_dict(self) -> JsonDict:
        return {
            "item_id": self.item_id,
            "node_ref": self.node_ref,
            "defect_state": self.defect_state.to_dict(),
            "downstream_path": list(self.downstream_path),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "depth": self.depth,
            "candidate_source": self.candidate_source,
            "priority": self.priority,
            "checked_evidence_refs": list(self.checked_evidence_refs),
            "evidence_hash": self.evidence_hash,
            "reopen_reason": self.reopen_reason,
            "graph_position": self.graph_position,
            "visit_key": self.visit_key,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "FrontierItem":
        item = cls.create(
            node_ref=str(value.get("node_ref") or ""),
            defect_state=DefectState.from_dict(_json_dict(value.get("defect_state"))),
            downstream_path=_string_list(value.get("downstream_path")),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            hypothesis_semantic_hash=str(value.get("hypothesis_semantic_hash") or ""),
            depth=int(value.get("depth") or 0),
            candidate_source=str(value.get("candidate_source") or ""),
            priority=_float(value.get("priority")),
            checked_evidence_refs=_string_list(value.get("checked_evidence_refs")),
            evidence_hash=str(value.get("evidence_hash") or ""),
            reopen_reason=str(value.get("reopen_reason") or ""),
            graph_position=int(value.get("graph_position") or 0),
        )
        item_id = str(value.get("item_id") or "")
        if item_id and item_id != item.item_id:
            raise ValueError("FrontierItem item_id does not match semantic fields")
        visit_key = str(value.get("visit_key") or "")
        if visit_key and visit_key != item.visit_key:
            raise ValueError("FrontierItem visit_key does not match semantic fields")
        return item


@dataclass(frozen=True)
class HypothesisEvidence:
    ref: str
    reason: str
    confidence: float = 0.0

    def to_dict(self) -> JsonDict:
        return {"ref": self.ref, "reason": self.reason, "confidence": self.confidence}

    @classmethod
    def from_dict(cls, value: JsonDict) -> "HypothesisEvidence":
        return cls(str(value.get("ref") or ""), str(value.get("reason") or ""), _float(value.get("confidence")))


@dataclass(frozen=True)
class AttributionHypothesis:
    hypothesis_id: str
    claim: str
    candidate_root_ref: str
    active_defect_state_id: str
    active_defect_fingerprint: str
    supporting_evidence: Tuple[HypothesisEvidence, ...] = field(default_factory=tuple)
    opposing_evidence: Tuple[HypothesisEvidence, ...] = field(default_factory=tuple)
    unresolved_questions: Tuple[str, ...] = field(default_factory=tuple)
    alternative_hypothesis_ids: Tuple[str, ...] = field(default_factory=tuple)
    counterfactual: JsonDict = field(default_factory=FrozenMapping)
    status: str = "active"
    confidence: float = 0.0
    resolution_reason: str = ""
    semantic_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in HYPOTHESIS_STATUSES:
            raise ValueError("unsupported hypothesis status: {0}".format(self.status))
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "opposing_evidence", tuple(self.opposing_evidence))
        object.__setattr__(self, "unresolved_questions", _frozen_strings(self.unresolved_questions))
        object.__setattr__(self, "alternative_hypothesis_ids", _frozen_strings(self.alternative_hypothesis_ids))
        object.__setattr__(self, "counterfactual", FrozenMapping(_thaw(self.counterfactual)))

    @classmethod
    def create(
        cls, claim: str, candidate_root_ref: str, defect_state: DefectState
    ) -> "AttributionHypothesis":
        semantic_hash = cls._semantic_hash(claim, candidate_root_ref, defect_state.fingerprint, [])
        return cls(
            hypothesis_id="hyp:{0}".format(semantic_hash[:20]),
            claim=claim,
            candidate_root_ref=candidate_root_ref,
            active_defect_state_id=defect_state.defect_state_id,
            active_defect_fingerprint=defect_state.fingerprint,
            semantic_hash=semantic_hash,
        )

    @staticmethod
    def _semantic_hash(
        claim: str, candidate_root_ref: str, defect_fingerprint: str, unresolved_questions: Tuple[str, ...]
    ) -> str:
        return _hash(
            {
                "claim": " ".join(claim.split()).lower(),
                "candidate_root_ref": candidate_root_ref,
                "active_defect_fingerprint": defect_fingerprint,
                "unresolved_questions": sorted(unresolved_questions),
            }
        )

    def with_updates(self, **changes: Any) -> "AttributionHypothesis":
        updated = replace(self, **changes)
        semantic_hash = self._semantic_hash(
            updated.claim,
            updated.candidate_root_ref,
            updated.active_defect_fingerprint,
            updated.unresolved_questions,
        )
        return replace(
            updated,
            hypothesis_id="hyp:{0}".format(semantic_hash[:20]),
            semantic_hash=semantic_hash,
        )

    def to_dict(self) -> JsonDict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "candidate_root_ref": self.candidate_root_ref,
            "active_defect_state_id": self.active_defect_state_id,
            "active_defect_fingerprint": self.active_defect_fingerprint,
            "supporting_evidence": [item.to_dict() for item in self.supporting_evidence],
            "opposing_evidence": [item.to_dict() for item in self.opposing_evidence],
            "unresolved_questions": list(self.unresolved_questions),
            "alternative_hypothesis_ids": list(self.alternative_hypothesis_ids),
            "counterfactual": _thaw(self.counterfactual),
            "status": self.status,
            "confidence": self.confidence,
            "resolution_reason": self.resolution_reason,
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "AttributionHypothesis":
        supporting = value.get("supporting_evidence")
        opposing = value.get("opposing_evidence")
        claim = str(value.get("claim") or "")
        candidate_root_ref = str(value.get("candidate_root_ref") or "")
        active_defect_fingerprint = str(value.get("active_defect_fingerprint") or "")
        unresolved_questions = _frozen_strings(value.get("unresolved_questions"))
        semantic_hash = cls._semantic_hash(
            claim, candidate_root_ref, active_defect_fingerprint, unresolved_questions
        )
        hypothesis_id = "hyp:{0}".format(semantic_hash[:20])
        persisted_semantic_hash = str(value.get("semantic_hash") or "")
        if persisted_semantic_hash and persisted_semantic_hash != semantic_hash:
            raise ValueError("AttributionHypothesis semantic_hash does not match semantic fields")
        persisted_hypothesis_id = str(value.get("hypothesis_id") or "")
        if persisted_hypothesis_id and persisted_hypothesis_id != hypothesis_id:
            raise ValueError("AttributionHypothesis hypothesis_id does not match semantic fields")
        active_defect_state_id = str(value.get("active_defect_state_id") or "")
        expected_defect_state_id = "defect:{0}".format(active_defect_fingerprint)
        if active_defect_state_id and active_defect_state_id != expected_defect_state_id:
            raise ValueError("AttributionHypothesis active_defect_state_id does not match fingerprint")
        return cls(
            hypothesis_id=hypothesis_id,
            claim=claim,
            candidate_root_ref=candidate_root_ref,
            active_defect_state_id=expected_defect_state_id,
            active_defect_fingerprint=active_defect_fingerprint,
            supporting_evidence=[HypothesisEvidence.from_dict(item) for item in supporting if isinstance(item, dict)]
            if isinstance(supporting, list)
            else [],
            opposing_evidence=[HypothesisEvidence.from_dict(item) for item in opposing if isinstance(item, dict)]
            if isinstance(opposing, list)
            else [],
            unresolved_questions=unresolved_questions,
            alternative_hypothesis_ids=_string_list(value.get("alternative_hypothesis_ids")),
            counterfactual=_json_dict(value.get("counterfactual")),
            status=str(value.get("status") or "active"),
            confidence=_float(value.get("confidence")),
            resolution_reason=str(value.get("resolution_reason") or ""),
            semantic_hash=semantic_hash,
        )


@dataclass(frozen=True)
class RootConfirmation:
    candidate_ref: str
    status: str
    excerpt: str = ""
    reason: str = ""
    counterfactual: str = ""
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.status not in CONFIRMATION_STATUSES:
            raise ValueError("unsupported root confirmation status: {0}".format(self.status))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))

    @classmethod
    def confirmed(
        cls,
        candidate_ref: str,
        *,
        excerpt: str,
        reason: str,
        counterfactual: str,
        confidence: float,
        evidence_refs: Optional[List[str]] = None,
    ) -> "RootConfirmation":
        return cls(candidate_ref, "confirmed", excerpt, reason, counterfactual, confidence, list(evidence_refs or []))

    @classmethod
    def rejected(cls, candidate_ref: str, reason: str, evidence_refs: Optional[List[str]] = None) -> "RootConfirmation":
        return cls(candidate_ref, "rejected", reason=reason, evidence_refs=list(evidence_refs or []))

    @classmethod
    def unknown(cls, candidate_ref: str, reason: str, evidence_refs: Optional[List[str]] = None) -> "RootConfirmation":
        return cls(candidate_ref, "unknown", reason=reason, evidence_refs=list(evidence_refs or []))

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "status": self.status,
            "excerpt": self.excerpt,
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RootConfirmation":
        return cls(
            candidate_ref=str(value.get("candidate_ref") or ""),
            status=str(value.get("status") or "unknown"),
            excerpt=str(value.get("excerpt") or ""),
            reason=str(value.get("reason") or ""),
            counterfactual=str(value.get("counterfactual") or ""),
            confidence=_float(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class ConfirmedRoot:
    node_ref: str
    defect_state: DefectState
    reason: str
    counterfactual: str
    confidence: float
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    component: str = ""
    event_type: str = ""
    defect_type: str = ""
    causal_role: str = "defect_introduction"
    episode_id: str = ""
    episode_member_refs: Tuple[str, ...] = field(default_factory=tuple)
    observed_defect_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "episode_member_refs", _frozen_strings(self.episode_member_refs))
        object.__setattr__(self, "observed_defect_refs", _frozen_strings(self.observed_defect_refs))

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "defect_state": self.defect_state.to_dict(),
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "component": self.component,
            "event_type": self.event_type,
            "defect_type": self.defect_type,
            "causal_role": self.causal_role,
            "episode_id": self.episode_id,
            "episode_member_refs": list(self.episode_member_refs),
            "observed_defect_refs": list(self.observed_defect_refs),
        }

    def to_legacy_root_cause(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "component": self.component,
            "event_type": self.event_type,
            "defect_type": self.defect_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "causal_role": self.causal_role,
            "episode_id": self.episode_id,
            "episode_member_refs": list(self.episode_member_refs),
            "observed_defect_refs": list(self.observed_defect_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "ConfirmedRoot":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            defect_state=DefectState.from_dict(_json_dict(value.get("defect_state"))),
            reason=str(value.get("reason") or ""),
            counterfactual=str(value.get("counterfactual") or ""),
            confidence=_float(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
            component=str(value.get("component") or ""),
            event_type=str(value.get("event_type") or ""),
            defect_type=str(value.get("defect_type") or ""),
            causal_role=str(value.get("causal_role") or "defect_introduction"),
            episode_id=str(value.get("episode_id") or ""),
            episode_member_refs=_string_list(value.get("episode_member_refs")),
            observed_defect_refs=_string_list(value.get("observed_defect_refs")),
        )


@dataclass(frozen=True)
class CausalFactor:
    node_ref: str
    relation: str
    reason: str
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "relation": self.relation,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalFactor":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            relation=str(value.get("relation") or "unknown"),
            reason=str(value.get("reason") or ""),
            confidence=_float(value.get("confidence")),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class RejectedCandidate:
    node_ref: str
    reason: str
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))

    def to_dict(self) -> JsonDict:
        return {"node_ref": self.node_ref, "reason": self.reason, "evidence_refs": list(self.evidence_refs)}

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RejectedCandidate":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            reason=str(value.get("reason") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class RecursiveAttributionReport:
    case_id: str
    objective: str
    start_refs: Tuple[str, ...] = field(default_factory=tuple)
    analysis_outcome: str = "inconclusive"
    analysis_perspective: str = ""
    defect_states: Tuple[DefectState, ...] = field(default_factory=tuple)
    causal_candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)
    causal_relations: Tuple[PredecessorAssessment, ...] = field(default_factory=tuple)
    step_judgments: Tuple[CausalStepJudgment, ...] = field(default_factory=tuple)
    hypotheses: Tuple[AttributionHypothesis, ...] = field(default_factory=tuple)
    introduction_candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)
    confirmations: Tuple[RootConfirmation, ...] = field(default_factory=tuple)
    confirmed_roots: Tuple[ConfirmedRoot, ...] = field(default_factory=tuple)
    co_roots: Tuple[ConfirmedRoot, ...] = field(default_factory=tuple)
    contributing_conditions: Tuple[CausalFactor, ...] = field(default_factory=tuple)
    amplifying_factors: Tuple[CausalFactor, ...] = field(default_factory=tuple)
    rejected_candidates: Tuple[RejectedCandidate, ...] = field(default_factory=tuple)
    unresolved_hypotheses: Tuple[AttributionHypothesis, ...] = field(default_factory=tuple)
    taint_paths: Tuple[Tuple[str, ...], ...] = field(default_factory=tuple)
    visited_order: Tuple[str, ...] = field(default_factory=tuple)
    unresolved_refs: Tuple[str, ...] = field(default_factory=tuple)
    metadata: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        for name in (
            "defect_states",
            "causal_candidates",
            "causal_relations",
            "step_judgments",
            "hypotheses",
            "introduction_candidates",
            "confirmations",
            "confirmed_roots",
            "co_roots",
            "contributing_conditions",
            "amplifying_factors",
            "rejected_candidates",
            "unresolved_hypotheses",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "start_refs", _frozen_strings(self.start_refs))
        object.__setattr__(self, "taint_paths", tuple(_frozen_strings(path) for path in self.taint_paths))
        object.__setattr__(self, "visited_order", _frozen_strings(self.visited_order))
        object.__setattr__(self, "unresolved_refs", _frozen_strings(self.unresolved_refs))
        object.__setattr__(self, "metadata", FrozenMapping(_thaw(self.metadata)))
        confirmed_root_refs = {item.node_ref for item in (*self.confirmed_roots, *self.co_roots)}
        overlapping_refs = confirmed_root_refs.intersection(self.unresolved_refs)
        if overlapping_refs:
            raise ValueError("a node cannot be both confirmed and unresolved")
        confirmation_statuses = {}
        for confirmation in self.confirmations:
            confirmation_statuses.setdefault(confirmation.candidate_ref, set()).add(confirmation.status)
        for root_ref in confirmed_root_refs:
            statuses = confirmation_statuses.get(root_ref, set())
            if not statuses:
                raise ValueError("missing confirmed root confirmation for {0}".format(root_ref))
            if statuses != {"confirmed"}:
                raise ValueError("root has unknown or rejected confirmation for {0}".format(root_ref))
        has_unknown_non_root_confirmation = any(
            confirmation.status == "unknown" and confirmation.candidate_ref not in confirmed_root_refs
            for confirmation in self.confirmations
        )
        has_blocking_evidence = bool(
            self.unresolved_refs
            or self.unresolved_hypotheses
            or has_unknown_non_root_confirmation
            or _has_unresolved_judgment_state(self.step_judgments, self.causal_relations)
            or _has_blocking_metadata(self.metadata)
        )
        if confirmed_root_refs:
            outcome = "partial_root_found" if has_blocking_evidence else "root_found"
        else:
            outcome = "inconclusive" if has_blocking_evidence else "no_defect"
        object.__setattr__(self, "analysis_outcome", outcome)

    def to_dict(self) -> JsonDict:
        return {
            "case_id": self.case_id,
            "objective": self.objective,
            "start_refs": list(self.start_refs),
            "analysis_outcome": self.analysis_outcome,
            "analysis_perspective": self.analysis_perspective,
            "defect_states": [item.to_dict() for item in self.defect_states],
            "causal_candidates": [item.to_dict() for item in self.causal_candidates],
            "causal_relations": [item.to_dict() for item in self.causal_relations],
            "step_judgments": [item.to_dict() for item in self.step_judgments],
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "introduction_candidates": [item.to_dict() for item in self.introduction_candidates],
            "confirmations": [item.to_dict() for item in self.confirmations],
            "confirmed_roots": [item.to_dict() for item in self.confirmed_roots],
            "co_roots": [item.to_dict() for item in self.co_roots],
            "contributing_conditions": [item.to_dict() for item in self.contributing_conditions],
            "amplifying_factors": [item.to_dict() for item in self.amplifying_factors],
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "unresolved_hypotheses": [item.to_dict() for item in self.unresolved_hypotheses],
            "root_causes": [item.to_legacy_root_cause() for item in self.confirmed_roots],
            "taint_paths": [list(path) for path in self.taint_paths],
            "visited_order": list(self.visited_order),
            "unresolved_refs": list(self.unresolved_refs),
            "metadata": _thaw(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RecursiveAttributionReport":
        def items(key: str, factory: Any) -> List[Any]:
            raw = value.get(key)
            return [factory(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

        confirmed_roots = items("confirmed_roots", ConfirmedRoot.from_dict)
        if not confirmed_roots:
            confirmed_roots = items("root_causes", ConfirmedRoot.from_dict)
        return cls(
            case_id=str(value.get("case_id") or ""),
            objective=str(value.get("objective") or ""),
            start_refs=_string_list(value.get("start_refs")),
            analysis_outcome=str(value.get("analysis_outcome") or "inconclusive"),
            analysis_perspective=str(value.get("analysis_perspective") or ""),
            defect_states=items("defect_states", DefectState.from_dict),
            causal_candidates=items("causal_candidates", CausalCandidate.from_dict),
            causal_relations=items("causal_relations", PredecessorAssessment.from_dict),
            step_judgments=items("step_judgments", CausalStepJudgment.from_dict),
            hypotheses=items("hypotheses", AttributionHypothesis.from_dict),
            introduction_candidates=items("introduction_candidates", CausalCandidate.from_dict),
            confirmations=items("confirmations", RootConfirmation.from_dict),
            confirmed_roots=confirmed_roots,
            co_roots=items("co_roots", ConfirmedRoot.from_dict),
            contributing_conditions=items("contributing_conditions", CausalFactor.from_dict),
            amplifying_factors=items("amplifying_factors", CausalFactor.from_dict),
            rejected_candidates=items("rejected_candidates", RejectedCandidate.from_dict),
            unresolved_hypotheses=items("unresolved_hypotheses", AttributionHypothesis.from_dict),
            taint_paths=[_string_list(path) for path in value.get("taint_paths", []) if isinstance(path, list)],
            visited_order=_string_list(value.get("visited_order")),
            unresolved_refs=_string_list(value.get("unresolved_refs")),
            metadata=_json_dict(value.get("metadata")),
        )


__all__ = [
    "CAUSAL_RELATIONS",
    "AttributionHypothesis",
    "CausalCandidate",
    "CausalFactor",
    "CausalStepJudgment",
    "ConfirmedRoot",
    "DefectState",
    "FrontierItem",
    "HypothesisEvidence",
    "PredecessorAssessment",
    "RecursiveAttributionReport",
    "RejectedCandidate",
    "RootConfirmation",
    "semantic_visit_key",
]
