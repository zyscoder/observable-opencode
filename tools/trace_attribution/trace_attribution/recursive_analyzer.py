"""Bounded recursive semantic-defect traversal for offline causal attribution."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .causal_judge import CausalJudge, CausalStepRequest
from .causal_retrieval import SemanticPredecessorRetriever
from .causal_state import (
    AttributionHypothesis,
    CausalCandidate,
    CausalFactor,
    CausalStepJudgment,
    DefectState,
    FrontierItem,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RejectedCandidate,
)
from .errors import JudgeProviderError, JudgeProviderUnavailable
from .graph import TraceGraph
from .hypotheses import HypothesisLedger, RecursiveFrontier
from .judgment_context import build_recursive_judgment_context
from .models import JsonDict, TraceNode, stable_json


RECURSIVE_RELATIONS = frozenset({"same_defect_propagation", "defect_transformation"})
EVALUATION_START_EVENTS = frozenset(
    {"case.observed_defect", "case.quality_gap", "case.missing_semantic"}
)


def _dedupe_strings(values: Iterable[str]) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for value in values:
        item = str(value)
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return tuple(output)


def _semantic_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    return stable_json(value)


def _first_semantic_value(data: Mapping[str, Any], keys: Sequence[str], fallback: str) -> str:
    for key in keys:
        value = _semantic_text(data.get(key))
        if value:
            return value
    return fallback


def _seed_defect_state(node: TraceNode, objective: str) -> DefectState:
    data = node.data
    label = _first_semantic_value(
        data,
        ("failure_type", "gap_kind", "dimension", "issue_kind", "defect_type"),
        "observed_defect",
    )
    summary = _first_semantic_value(
        data, ("summary", "description", "reason", "text"), label
    )
    return DefectState.create(
        label=label,
        expected=_first_semantic_value(
            data,
            ("expected", "expected_behavior", "requirement", "criterion"),
            objective,
        ),
        actual=_first_semantic_value(
            data,
            ("actual", "actual_behavior", "observed", "result"),
            summary,
        ),
        mechanism=_first_semantic_value(
            data,
            ("mechanism", "failure_mechanism", "cause", "reason"),
            summary,
        ),
        scope=_first_semantic_value(
            data,
            ("scope", "attribution_domain", "component", "dimension"),
            node.component or node.event_type or "task_quality",
        ),
    )


def _clone_graph(graph: TraceGraph) -> TraceGraph:
    """Create an analysis-owned graph so hydration cannot mutate the caller's graph."""
    return TraceGraph.from_trace(
        copy.deepcopy(graph.raw_trace),
        artifact_root=getattr(graph, "_artifact_root", None),
    )


def _candidate_key(candidate: CausalCandidate) -> Tuple[str, str, str]:
    return (candidate.ref, candidate.source, stable_json(candidate.edge))


def _artifact_payloads(value: Any) -> Iterable[Tuple[str, bytes]]:
    if isinstance(value, Mapping):
        content = value.get("content")
        if isinstance(content, str) and content:
            identity = str(
                value.get("hash")
                or value.get("artifact_id")
                or value.get("path")
                or stable_json({"content": content})
            )
            yield identity, content.encode("utf-8")
        for child in value.values():
            yield from _artifact_payloads(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _artifact_payloads(child)


def _judge_transport(judge: CausalJudge) -> Any:
    return getattr(judge, "transport", judge)


def _judge_request_count(judge: CausalJudge) -> Optional[int]:
    value = getattr(_judge_transport(judge), "request_count", None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _provider_circuit(judge: CausalJudge) -> JsonDict:
    target = _judge_transport(judge)
    stats = getattr(target, "provider_circuit_stats", None)
    if callable(stats):
        value = stats()
        if isinstance(value, Mapping):
            return dict(value)
    value = getattr(target, "provider_circuit", None)
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "open": bool(getattr(target, "provider_circuit_open", False)),
        "reason": str(getattr(target, "provider_circuit_reason", "") or ""),
    }


@dataclass
class RecursiveAnalysisState:
    """Mutable orchestration state containing immutable causal domain values."""

    graph: TraceGraph
    start_refs: Tuple[str, ...]
    objective: str
    analysis_perspective: str
    ledger: HypothesisLedger = field(default_factory=HypothesisLedger)
    frontier: RecursiveFrontier = field(default_factory=RecursiveFrontier)
    defect_states: Dict[str, DefectState] = field(default_factory=dict)
    causal_candidates: List[CausalCandidate] = field(default_factory=list)
    causal_relations: List[PredecessorAssessment] = field(default_factory=list)
    step_judgments: List[CausalStepJudgment] = field(default_factory=list)
    introduction_candidates: List[CausalCandidate] = field(default_factory=list)
    contributing_conditions: List[CausalFactor] = field(default_factory=list)
    rejected_candidates: List[RejectedCandidate] = field(default_factory=list)
    taint_paths: List[Tuple[str, ...]] = field(default_factory=list)
    visited_order: List[str] = field(default_factory=list)
    unresolved_branches: List[JsonDict] = field(default_factory=list)
    unresolved_refs: List[str] = field(default_factory=list)
    unresolved_hypothesis_ids: Set[str] = field(default_factory=set)
    introduction_hypothesis_ids: Set[str] = field(default_factory=set)
    visit_evidence: Dict[str, Set[str]] = field(default_factory=dict)
    transformation_chains: Dict[str, Tuple[DefectState, ...]] = field(default_factory=dict)
    exhausted_budgets: Dict[str, int] = field(default_factory=dict)
    artifact_identities: Set[str] = field(default_factory=set)
    artifact_bytes: int = 0
    processed_items: int = 0
    judge_requests: int = 0
    investigation_rounds: int = 0
    seed_count: int = 0

    @classmethod
    def create(
        cls,
        *,
        graph: TraceGraph,
        start_refs: Iterable[str],
        objective: str,
        analysis_perspective: str,
        max_hypotheses: int = 24,
    ) -> "RecursiveAnalysisState":
        resolved_starts = _dedupe_strings(graph.resolve(ref) or ref for ref in start_refs)
        state = cls(
            graph=graph,
            start_refs=resolved_starts,
            objective=objective,
            analysis_perspective=analysis_perspective,
        )
        for start_ref in resolved_starts:
            node = graph.nodes.get(start_ref)
            if node is None:
                state._mark_seed_unresolved(start_ref, "start_ref_unresolved", "The start reference is absent.")
                continue
            defect_state = _seed_defect_state(node, objective)
            state._remember_defect(defect_state)
            state.seed_count += 1
            if node.event_type in EVALUATION_START_EVENTS:
                predecessors = graph.upstream_refs(start_ref)
                if not predecessors:
                    state._mark_seed_unresolved(
                        start_ref,
                        "outcome_evidence_missing",
                        "The evaluation assertion has no concrete outcome evidence.",
                    )
                    continue
                for predecessor_ref in predecessors:
                    if len(state.ledger.snapshot()) >= max_hypotheses:
                        state._increment_budget("hypotheses")
                        state._mark_seed_unresolved(
                            predecessor_ref,
                            "hypothesis_limit",
                            "The seed hypothesis budget is exhausted.",
                        )
                        continue
                    hypothesis = state.ledger.create(
                        "{0} is upstream outcome evidence for {1}.".format(
                            predecessor_ref, start_ref
                        ),
                        predecessor_ref,
                        defect_state,
                    )
                    item = FrontierItem.create(
                        node_ref=predecessor_ref,
                        defect_state=defect_state,
                        downstream_path=[predecessor_ref, start_ref],
                        hypothesis_id=hypothesis.hypothesis_id,
                        hypothesis_semantic_hash=hypothesis.semantic_hash,
                        candidate_source="outcome_evidence",
                        priority=1.0,
                        checked_evidence_refs=[start_ref],
                        graph_position=graph.position(predecessor_ref),
                    )
                    state.frontier.push(item)
                    state._merge_visit_evidence(item.visit_key, [start_ref])
                    state.causal_relations.append(
                        PredecessorAssessment(
                            ref=predecessor_ref,
                            relation="outcome_evidence",
                            reason="The evaluation assertion cites this record as observed outcome evidence.",
                            confidence=1.0,
                            recurse=False,
                            evidence_refs=(start_ref, predecessor_ref),
                        )
                    )
                    candidate = state._candidate_for_ref(
                        predecessor_ref,
                        source="outcome_evidence",
                        edge={
                            "from_ref": predecessor_ref,
                            "to_ref": start_ref,
                            "relation": "outcome_evidence",
                            "evidence_type": "recorded_evaluation",
                            "eligible_for_attribution": True,
                        },
                        evidence_refs=(start_ref, predecessor_ref),
                    )
                    if candidate is not None:
                        state._remember_candidate(candidate)
                continue
            if len(state.ledger.snapshot()) >= max_hypotheses:
                state._increment_budget("hypotheses")
                state._mark_seed_unresolved(
                    start_ref, "hypothesis_limit", "The seed hypothesis budget is exhausted."
                )
                continue
            hypothesis = state.ledger.create(
                "Investigate the observed defect at {0}.".format(start_ref),
                start_ref,
                defect_state,
            )
            item = FrontierItem.create(
                node_ref=start_ref,
                defect_state=defect_state,
                downstream_path=[start_ref],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                candidate_source="analysis_start",
                priority=1.0,
                checked_evidence_refs=[start_ref],
                graph_position=graph.position(start_ref),
            )
            state.frontier.push(item)
            state._merge_visit_evidence(item.visit_key, [start_ref])
        return state

    def build_step_request(
        self,
        graph: TraceGraph,
        item: FrontierItem,
        candidates: Sequence[CausalCandidate],
    ) -> CausalStepRequest:
        hypothesis = self.ledger.get(item.hypothesis_id)
        chain = self.transformation_chains.get(item.defect_state.fingerprint, (item.defect_state,))
        downstream_judgments = [
            judgment
            for judgment in self.step_judgments
            if judgment.current_node_ref in item.downstream_path
        ]
        context = build_recursive_judgment_context(
            graph=graph,
            node_ref=item.node_ref,
            defect_state=item.defect_state,
            hypothesis=hypothesis,
            candidates=candidates,
            downstream_path=list(item.downstream_path),
            downstream_judgments=downstream_judgments,
            defect_transformation_chain=list(chain),
            objective=self.objective,
        )
        context["objective"] = self.objective
        context["analysis_perspective"] = self.analysis_perspective
        context["active_hypothesis_id"] = hypothesis.hypothesis_id
        context["active_visit_key"] = item.visit_key
        context["checked_evidence_refs"] = sorted(self.visit_evidence.get(item.visit_key, set()))
        context["evidence_hash"] = hashlib.sha256(
            stable_json(context).encode("utf-8")
        ).hexdigest()
        return CausalStepRequest(
            recursive_context=context,
            current_node=graph.hydrate_node(item.node_ref),
            defect_state=item.defect_state,
            candidates=tuple(candidates),
        )

    def reserve_artifact_bytes(self, request: CausalStepRequest, limit: int) -> bool:
        new_payloads: Dict[str, bytes] = {}
        for identity, content in _artifact_payloads(request.to_dict()):
            if identity not in self.artifact_identities:
                new_payloads.setdefault(identity, content)
        new_bytes = sum(len(content) for content in new_payloads.values())
        if self.artifact_bytes + new_bytes > limit:
            return False
        self.artifact_identities.update(new_payloads)
        self.artifact_bytes += new_bytes
        return True

    def apply_step(
        self,
        item: FrontierItem,
        judgment: CausalStepJudgment,
        *,
        graph_position: Any,
        max_hypotheses: int,
    ) -> None:
        self.step_judgments.append(judgment)
        self.visited_order.append(item.node_ref)
        self.taint_paths.append(tuple(item.downstream_path))
        evidence_hash = hashlib.sha256(
            stable_json(judgment.to_dict()).encode("utf-8")
        ).hexdigest()

        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            details = "; ".join(judgment.missing_evidence) or judgment.current_defect_reason
            self.mark_unresolved(item, "judge_unknown", details)
        elif judgment.current_defect_status == "present" and judgment.candidate_introduction:
            candidate = self._candidate_for_ref(
                item.node_ref,
                source="judge_introduction_candidate",
                edge={
                    "relation": "introduction_candidate",
                    "evidence_type": "judge_assessment",
                    "eligible_for_attribution": False,
                },
                evidence_refs=tuple(self.visit_evidence.get(item.visit_key, set())),
            )
            if candidate is not None:
                self.introduction_candidates.append(candidate)
                self._remember_candidate(candidate)
            self.introduction_hypothesis_ids.add(item.hypothesis_id)

        for assessment in judgment.predecessors:
            self.causal_relations.append(assessment)
            if assessment.relation == "contributing_condition":
                self.contributing_conditions.append(
                    CausalFactor(
                        node_ref=assessment.ref,
                        relation=assessment.relation,
                        reason=assessment.reason,
                        confidence=assessment.confidence,
                        evidence_refs=assessment.evidence_refs,
                    )
                )
                continue
            if assessment.relation in {"unrelated", "unknown"}:
                if assessment.relation == "unrelated":
                    self.rejected_candidates.append(
                        RejectedCandidate(assessment.ref, assessment.reason, assessment.evidence_refs)
                    )
                else:
                    self._mark_ref_unresolved(
                        assessment.ref,
                        item,
                        "predecessor_unknown",
                        "; ".join(assessment.missing_evidence) or assessment.reason,
                    )
                continue
            if assessment.relation not in RECURSIVE_RELATIONS or not assessment.recurse:
                continue
            if assessment.ref not in self.graph.nodes:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_ref_unresolved",
                    "The recursive predecessor is not present in the trace graph.",
                )
                continue
            if assessment.missing_evidence or not assessment.evidence_refs:
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "predecessor_evidence_missing",
                    "; ".join(assessment.missing_evidence)
                    or "The recursive predecessor has no grounded supporting evidence.",
                )
                continue
            if judgment.current_defect_status != "present":
                self._mark_ref_unresolved(
                    assessment.ref,
                    item,
                    "unsupported_propagation",
                    "A predecessor cannot carry a defect when the current defect is not present.",
                )
                continue

            upstream_defect = item.defect_state
            hypothesis = self.ledger.get(item.hypothesis_id)
            if assessment.relation == "defect_transformation":
                if assessment.upstream_defect is None:
                    self._mark_ref_unresolved(
                        assessment.ref,
                        item,
                        "missing_transformed_defect",
                        "The transformation does not define an upstream defect state.",
                    )
                    continue
                upstream_defect = assessment.upstream_defect
                self._remember_defect(upstream_defect)
                claim = "{0} transformed into {1} at {2}.".format(
                    upstream_defect.label, item.defect_state.label, item.node_ref
                )
                proposed = AttributionHypothesis.create(
                    claim, assessment.ref, upstream_defect
                )
                snapshot = self.ledger.snapshot()
                existing_ids = {
                    str(existing.get("hypothesis_id") or "") for existing in snapshot
                }
                if (
                    proposed.hypothesis_id not in existing_ids
                    and len(snapshot) >= max_hypotheses
                ):
                    self._increment_budget("hypotheses")
                    self._mark_ref_unresolved(
                        assessment.ref,
                        item,
                        "hypothesis_limit",
                        "The transformed hypothesis budget is exhausted.",
                    )
                    continue
                hypothesis = self.ledger.create(
                    claim,
                    assessment.ref,
                    upstream_defect,
                )
                downstream_chain = self.transformation_chains.get(
                    item.defect_state.fingerprint, (item.defect_state,)
                )
                self.transformation_chains[upstream_defect.fingerprint] = (
                    upstream_defect,
                    *downstream_chain,
                )
            hypothesis = self.ledger.add_support(
                hypothesis.hypothesis_id,
                assessment.ref,
                assessment.reason,
                assessment.confidence,
            )
            predecessor = FrontierItem.create(
                node_ref=assessment.ref,
                defect_state=upstream_defect,
                downstream_path=[assessment.ref, *item.downstream_path],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                depth=item.depth + 1,
                candidate_source=assessment.relation,
                priority=max(assessment.confidence, 0.0),
                checked_evidence_refs=list(assessment.evidence_refs),
                graph_position=graph_position(assessment.ref),
            )
            self._merge_visit_evidence(predecessor.visit_key, assessment.evidence_refs)
            self.frontier.push(predecessor)

        self.frontier.mark_completed(item, evidence_hash)

    def mark_unresolved(
        self,
        item: FrontierItem,
        reason: str,
        details: str = "",
        *,
        exhausted_budget: str = "",
    ) -> None:
        if exhausted_budget:
            self._increment_budget(exhausted_budget)
        self._mark_ref_unresolved(item.node_ref, item, reason, details)

    def complete_unresolved(
        self,
        item: FrontierItem,
        reason: str,
        details: str = "",
        *,
        exhausted_budget: str = "",
    ) -> None:
        self.mark_unresolved(
            item,
            reason,
            details,
            exhausted_budget=exhausted_budget,
        )
        self.frontier.mark_completed(item, "unresolved:{0}".format(reason))

    def build_report(self, *, judge: CausalJudge) -> RecursiveAttributionReport:
        hypotheses = [AttributionHypothesis.from_dict(item) for item in self.ledger.snapshot()]
        by_id = {item.hypothesis_id: item for item in hypotheses}
        unresolved_ids = self.unresolved_hypothesis_ids | self.introduction_hypothesis_ids
        unresolved_hypotheses = [by_id[item] for item in sorted(unresolved_ids) if item in by_id]
        candidate_seen: Set[Tuple[str, str, str]] = set()
        causal_candidates: List[CausalCandidate] = []
        for candidate in self.causal_candidates:
            key = _candidate_key(candidate)
            if key not in candidate_seen:
                candidate_seen.add(key)
                causal_candidates.append(candidate)
        merged = {
            key: tuple(sorted(refs))
            for key, refs in sorted(self.visit_evidence.items())
            if len(refs) > 1
        }
        provider = _provider_circuit(judge)
        metadata = {
            "analysis": "agentic_recursive_semantic_taint",
            "behavior_impact": "none_offline_analysis_only",
            "seed_count": self.seed_count,
            "processed_frontier_items": self.processed_items,
            "judge_request_count": self.judge_requests,
            "artifact_bytes": self.artifact_bytes,
            "investigation_rounds": self.investigation_rounds,
            "exhausted_budgets": dict(sorted(self.exhausted_budgets.items())),
            "unresolved_branches": list(self.unresolved_branches),
            "merged_visit_evidence": merged,
            "frontier_checkpoint": self.frontier.checkpoint(),
            "hypothesis_snapshot": self.ledger.snapshot(),
            "provider_circuit": provider,
            "independent_confirmation": "deferred_to_task_7",
        }
        return RecursiveAttributionReport(
            case_id=self.graph.case_id,
            objective=self.objective,
            start_refs=self.start_refs,
            analysis_perspective=self.analysis_perspective,
            defect_states=tuple(self.defect_states.values()),
            causal_candidates=tuple(causal_candidates),
            causal_relations=tuple(self.causal_relations),
            step_judgments=tuple(self.step_judgments),
            hypotheses=tuple(hypotheses),
            introduction_candidates=tuple(self.introduction_candidates),
            confirmed_roots=(),
            contributing_conditions=tuple(self.contributing_conditions),
            rejected_candidates=tuple(self.rejected_candidates),
            unresolved_hypotheses=tuple(unresolved_hypotheses),
            taint_paths=tuple(dict.fromkeys(self.taint_paths)),
            visited_order=_dedupe_strings(self.visited_order),
            unresolved_refs=_dedupe_strings(self.unresolved_refs),
            metadata=metadata,
        )

    def _candidate_for_ref(
        self,
        ref: str,
        *,
        source: str,
        edge: Mapping[str, Any],
        evidence_refs: Tuple[str, ...],
    ) -> Optional[CausalCandidate]:
        node = self.graph.nodes.get(ref)
        if node is None:
            return None
        return CausalCandidate(
            ref=ref,
            node=node,
            source=source,
            edge=dict(edge),
            score=1.0,
            evidence_refs=evidence_refs,
        )

    def _remember_candidate(self, candidate: CausalCandidate) -> None:
        self.causal_candidates.append(candidate)

    def _remember_defect(self, defect_state: DefectState) -> None:
        self.defect_states.setdefault(defect_state.fingerprint, defect_state)
        self.transformation_chains.setdefault(defect_state.fingerprint, (defect_state,))

    def _merge_visit_evidence(self, visit_key: str, refs: Iterable[str]) -> None:
        self.visit_evidence.setdefault(visit_key, set()).update(str(ref) for ref in refs if ref)

    def _increment_budget(self, name: str) -> None:
        self.exhausted_budgets[name] = self.exhausted_budgets.get(name, 0) + 1

    def _mark_seed_unresolved(self, ref: str, reason: str, details: str) -> None:
        self.unresolved_refs.append(ref)
        self.unresolved_branches.append(
            {
                "node_ref": ref,
                "defect_state_id": "",
                "hypothesis_id": "",
                "reason": reason,
                "details": details,
                "depth": 0,
            }
        )

    def _mark_ref_unresolved(
        self,
        ref: str,
        item: FrontierItem,
        reason: str,
        details: str,
    ) -> None:
        self.unresolved_refs.append(ref)
        self.unresolved_hypothesis_ids.add(item.hypothesis_id)
        self.unresolved_branches.append(
            {
                "node_ref": ref,
                "defect_state_id": item.defect_state.defect_state_id,
                "hypothesis_id": item.hypothesis_id,
                "reason": reason,
                "details": details,
                "depth": item.depth,
            }
        )


class AgenticRecursiveAnalyzer:
    def __init__(
        self,
        *,
        judge: CausalJudge,
        retriever: Optional[SemanticPredecessorRetriever] = None,
        max_frontier_items: int = 96,
        max_depth: int = 20,
        max_hypotheses: int = 24,
        max_investigation_rounds: int = 12,
        max_artifact_bytes: int = 1_048_576,
        max_judge_requests: int = 128,
    ) -> None:
        self.judge = judge
        self.retriever = retriever or SemanticPredecessorRetriever()
        self.max_frontier_items = max(0, int(max_frontier_items))
        self.max_depth = max(0, int(max_depth))
        self.max_hypotheses = max(0, int(max_hypotheses))
        self.max_investigation_rounds = max(0, int(max_investigation_rounds))
        self.max_artifact_bytes = max(0, int(max_artifact_bytes))
        self.max_judge_requests = max(0, int(max_judge_requests))

    def analyze(
        self,
        graph: TraceGraph,
        *,
        start_refs: Optional[Iterable[str]] = None,
        objective: str,
        analysis_perspective: str = "Find the best-supported causal explanation.",
    ) -> RecursiveAttributionReport:
        analysis_graph = _clone_graph(graph)
        requested_starts = tuple(start_refs) if start_refs is not None else ()
        if not requested_starts:
            requested_starts = tuple(analysis_graph.default_start_refs())
        state = RecursiveAnalysisState.create(
            graph=analysis_graph,
            start_refs=requested_starts,
            objective=objective,
            analysis_perspective=analysis_perspective,
            max_hypotheses=self.max_hypotheses,
        )
        if not requested_starts:
            state._mark_seed_unresolved(
                "analysis:start",
                "analysis_start_missing",
                "The trace does not contain a concrete analysis start node.",
            )
        initial_request_count = _judge_request_count(self.judge)

        while state.frontier and state.processed_items < self.max_frontier_items:
            item = state.frontier.pop()
            state.processed_items += 1
            if item.depth > self.max_depth:
                state.complete_unresolved(
                    item,
                    "depth_limit",
                    "Recursive depth {0} exceeds limit {1}.".format(item.depth, self.max_depth),
                    exhausted_budget="depth",
                )
                continue
            if _provider_circuit(self.judge).get("open"):
                state.complete_unresolved(
                    item,
                    "provider_circuit_open",
                    str(_provider_circuit(self.judge).get("reason") or "Provider circuit is open."),
                    exhausted_budget="provider_circuit",
                )
                continue
            candidates = self.retriever.retrieve(
                analysis_graph,
                item.node_ref,
                item.defect_state,
                state.ledger.get(item.hypothesis_id),
                allow_semantic_fallback=True,
            )
            for candidate in candidates:
                state._remember_candidate(candidate)
            request = state.build_step_request(analysis_graph, item, candidates)
            if not state.reserve_artifact_bytes(request, self.max_artifact_bytes):
                state.complete_unresolved(
                    item,
                    "artifact_byte_limit",
                    "Hydrated artifact content exceeds the analysis byte budget.",
                    exhausted_budget="artifact_bytes",
                )
                continue
            reserve = 2 if hasattr(self.judge, "transport") else 1
            current_count = _judge_request_count(self.judge)
            consumed = (
                state.judge_requests
                if current_count is None or initial_request_count is None
                else max(0, current_count - initial_request_count)
            )
            if consumed + reserve > self.max_judge_requests:
                state.complete_unresolved(
                    item,
                    "judge_request_limit",
                    "The Judge request budget cannot cover a judgment and its possible repair.",
                    exhausted_budget="judge_requests",
                )
                continue
            before = _judge_request_count(self.judge)
            try:
                judgment = self.judge.judge_step(request)
            except (JudgeProviderError, JudgeProviderUnavailable) as exc:
                after = _judge_request_count(self.judge)
                state.judge_requests += max(1, (after or 0) - (before or 0))
                state.complete_unresolved(
                    item,
                    "provider_circuit_open"
                    if isinstance(exc, JudgeProviderUnavailable)
                    else "provider_error",
                    "{0}: {1}".format(type(exc).__name__, exc),
                    exhausted_budget="provider_circuit"
                    if isinstance(exc, JudgeProviderUnavailable)
                    else "",
                )
                continue
            except Exception as exc:
                after = _judge_request_count(self.judge)
                state.judge_requests += max(1, (after or 0) - (before or 0))
                state.complete_unresolved(
                    item,
                    "judge_error",
                    "{0}: {1}".format(type(exc).__name__, exc),
                )
                continue
            after = _judge_request_count(self.judge)
            state.judge_requests += max(1, (after or 0) - (before or 0))
            state.apply_step(
                item,
                judgment,
                graph_position=analysis_graph.position,
                max_hypotheses=self.max_hypotheses,
            )
            if judgment.suggested_investigation is not None:
                if state.investigation_rounds >= self.max_investigation_rounds:
                    state._increment_budget("investigation_rounds")
                else:
                    state.investigation_rounds += 1
                state._mark_ref_unresolved(
                    item.node_ref,
                    item,
                    "investigation_deferred",
                    "Bounded investigation tools are introduced in Task 6.",
                )

        if state.frontier:
            while state.frontier:
                item = state.frontier.pop()
                state.complete_unresolved(
                    item,
                    "frontier_item_limit",
                    "The recursive frontier item budget is exhausted.",
                    exhausted_budget="frontier_items",
                )
        return state.build_report(judge=self.judge)


__all__ = ["AgenticRecursiveAnalyzer", "RecursiveAnalysisState"]
