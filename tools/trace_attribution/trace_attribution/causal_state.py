"""Serializable state for recursive offline causal attribution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

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


def _hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_dict(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}


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
        "data": dict(node.data),
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
        return cls(
            defect_state_id=str(value.get("defect_state_id") or ""),
            label=str(value.get("label") or ""),
            expected=str(value.get("expected") or ""),
            actual=str(value.get("actual") or ""),
            mechanism=str(value.get("mechanism") or ""),
            scope=str(value.get("scope") or ""),
            fingerprint=str(value.get("fingerprint") or ""),
            derived_from_defect_state_id=str(value.get("derived_from_defect_state_id") or ""),
            transformation_reason=str(value.get("transformation_reason") or ""),
        )


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
    edge: JsonDict = field(default_factory=dict)
    score: float = 0.0
    evidence_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "node": _trace_node_to_dict(self.node),
            "source": self.source,
            "edge": dict(self.edge),
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
    evidence_refs: List[str] = field(default_factory=list)
    missing_evidence: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))

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
    predecessors: List[PredecessorAssessment] = field(default_factory=list)
    candidate_introduction: bool = False
    missing_evidence: List[str] = field(default_factory=list)
    suggested_investigation: Optional[JsonDict] = None
    confidence: float = 0.0

    def to_dict(self) -> JsonDict:
        return {
            "current_node_ref": self.current_node_ref,
            "current_defect_status": self.current_defect_status,
            "current_defect_reason": self.current_defect_reason,
            "predecessors": [item.to_dict() for item in self.predecessors],
            "candidate_introduction": self.candidate_introduction,
            "missing_evidence": list(self.missing_evidence),
            "suggested_investigation": dict(self.suggested_investigation)
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
    downstream_path: List[str]
    hypothesis_id: str
    hypothesis_semantic_hash: str
    depth: int = 0
    candidate_source: str = ""
    priority: float = 0.0
    checked_evidence_refs: List[str] = field(default_factory=list)
    evidence_hash: str = ""
    reopen_reason: str = ""
    graph_position: int = 0

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
            downstream_path=list(downstream_path),
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis_semantic_hash,
            depth=depth,
            candidate_source=candidate_source,
            priority=priority,
            checked_evidence_refs=list(checked_evidence_refs or []),
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
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "FrontierItem":
        return cls(
            item_id=str(value.get("item_id") or ""),
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
    supporting_evidence: List[HypothesisEvidence] = field(default_factory=list)
    opposing_evidence: List[HypothesisEvidence] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    alternative_hypothesis_ids: List[str] = field(default_factory=list)
    counterfactual: JsonDict = field(default_factory=dict)
    status: str = "active"
    confidence: float = 0.0
    resolution_reason: str = ""
    semantic_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in HYPOTHESIS_STATUSES:
            raise ValueError("unsupported hypothesis status: {0}".format(self.status))

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
        claim: str, candidate_root_ref: str, defect_fingerprint: str, unresolved_questions: List[str]
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
        return replace(updated, semantic_hash=semantic_hash)

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
            "counterfactual": dict(self.counterfactual),
            "status": self.status,
            "confidence": self.confidence,
            "resolution_reason": self.resolution_reason,
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "AttributionHypothesis":
        supporting = value.get("supporting_evidence")
        opposing = value.get("opposing_evidence")
        return cls(
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            claim=str(value.get("claim") or ""),
            candidate_root_ref=str(value.get("candidate_root_ref") or ""),
            active_defect_state_id=str(value.get("active_defect_state_id") or ""),
            active_defect_fingerprint=str(value.get("active_defect_fingerprint") or ""),
            supporting_evidence=[HypothesisEvidence.from_dict(item) for item in supporting if isinstance(item, dict)]
            if isinstance(supporting, list)
            else [],
            opposing_evidence=[HypothesisEvidence.from_dict(item) for item in opposing if isinstance(item, dict)]
            if isinstance(opposing, list)
            else [],
            unresolved_questions=_string_list(value.get("unresolved_questions")),
            alternative_hypothesis_ids=_string_list(value.get("alternative_hypothesis_ids")),
            counterfactual=_json_dict(value.get("counterfactual")),
            status=str(value.get("status") or "active"),
            confidence=_float(value.get("confidence")),
            resolution_reason=str(value.get("resolution_reason") or ""),
            semantic_hash=str(value.get("semantic_hash") or ""),
        )


@dataclass(frozen=True)
class RootConfirmation:
    candidate_ref: str
    status: str
    excerpt: str = ""
    reason: str = ""
    counterfactual: str = ""
    confidence: float = 0.0
    evidence_refs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in CONFIRMATION_STATUSES:
            raise ValueError("unsupported root confirmation status: {0}".format(self.status))

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
    evidence_refs: List[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "defect_state": self.defect_state.to_dict(),
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
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
        )


@dataclass(frozen=True)
class CausalFactor:
    node_ref: str
    relation: str
    reason: str
    confidence: float = 0.0
    evidence_refs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))

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
    evidence_refs: List[str] = field(default_factory=list)

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
    start_refs: List[str] = field(default_factory=list)
    analysis_outcome: str = "inconclusive"
    analysis_perspective: str = ""
    defect_states: List[DefectState] = field(default_factory=list)
    causal_candidates: List[CausalCandidate] = field(default_factory=list)
    causal_relations: List[PredecessorAssessment] = field(default_factory=list)
    step_judgments: List[CausalStepJudgment] = field(default_factory=list)
    hypotheses: List[AttributionHypothesis] = field(default_factory=list)
    introduction_candidates: List[CausalCandidate] = field(default_factory=list)
    confirmations: List[RootConfirmation] = field(default_factory=list)
    confirmed_roots: List[ConfirmedRoot] = field(default_factory=list)
    co_roots: List[ConfirmedRoot] = field(default_factory=list)
    contributing_conditions: List[CausalFactor] = field(default_factory=list)
    amplifying_factors: List[CausalFactor] = field(default_factory=list)
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
    unresolved_hypotheses: List[AttributionHypothesis] = field(default_factory=list)
    taint_paths: List[List[str]] = field(default_factory=list)
    visited_order: List[str] = field(default_factory=list)
    unresolved_refs: List[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

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
            "root_causes": [item.to_dict() for item in self.confirmed_roots],
            "taint_paths": [list(path) for path in self.taint_paths],
            "visited_order": list(self.visited_order),
            "unresolved_refs": list(self.unresolved_refs),
            "metadata": dict(self.metadata),
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
