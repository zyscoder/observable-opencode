from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    BoundedJudgeCallError,
    BoundedJudgeCapability,
    ClaudeCausalJudge,
    OfflineCausalJudgeAdapter,
    OfflineJudgeCapability,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    PredecessorAssessment,
    RootConfirmation,
)
from trace_attribution.errors import (
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer, RecursiveAnalysisState


def observed_trace(*, branching: bool = False, artifact_content: str = "") -> dict:
    records = [
        {
            "record_id": "prompt",
            "component": "prompt",
            "event_type": "message.input",
            "data": {"text": "Implement the complete namespace compatibility contract."},
        },
        {
            "record_id": "decision",
            "component": "agent",
            "event_type": "decision",
            "data": {"rationale": "Implement only the methods found in the first search."},
        },
        {
            "record_id": "change",
            "component": "processor",
            "event_type": "change",
            "data": {"summary": "The change omits parse_namespace_object."},
        },
        {
            "record_id": "observed_defect",
            "component": "evaluation",
            "event_type": "case.observed_defect",
            "source_refs": ["record:change"],
            "data": {
                "failure_type": "missing_namespace_contract",
                "expected": "The parser preserves the namespace compatibility contract.",
                "actual": "parse_namespace_object is absent.",
                "mechanism": "A required method was omitted from the implementation.",
                "scope": "parser_contract_recovery",
                "summary": "The required namespace method is missing.",
            },
        },
    ]
    if branching:
        records.insert(
            2,
            {
                "record_id": "context",
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "The complete compatibility contract is documented here."},
            },
        )
    if artifact_content:
        records[1]["data"]["hydrated_artifacts"] = [
            {
                "artifact_id": "decision-context",
                "hash": "artifact-hash",
                "content": artifact_content,
            }
        ]
    edges = [
        {
            "from": {"type": "record", "id": "decision"},
            "to": {"type": "record", "id": "change"},
            "relation": "decision_guided_change",
            "evidence_type": "confirmed",
            "confidence": 0.98,
            "eligible_for_attribution": True,
        },
        {
            "from": {"type": "record", "id": "change"},
            "to": {"type": "record", "id": "observed_defect"},
            "relation": "change_observed_by_evaluation",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        },
    ]
    if branching:
        edges.append(
            {
                "from": {"type": "record", "id": "context"},
                "to": {"type": "record", "id": "change"},
                "relation": "context_available_to_change",
                "evidence_type": "confirmed",
                "confidence": 0.8,
                "eligible_for_attribution": True,
            }
        )
    return {"case_id": "recursive-case", "records": records, "dataflow_edges": edges}


def relation(
    ref: str,
    relation_name: str,
    *,
    upstream_defect: DefectState | None = None,
    evidence_refs: tuple[str, ...] = (),
) -> PredecessorAssessment:
    return PredecessorAssessment(
        ref=ref,
        relation=relation_name,
        reason="The predecessor semantically explains the current defect.",
        confidence=0.9,
        recurse=relation_name in {"same_defect_propagation", "defect_transformation"},
        upstream_defect=upstream_defect,
        evidence_refs=evidence_refs or (ref,),
    )


def step(
    ref: str,
    *,
    status: str = "present",
    predecessors: tuple[PredecessorAssessment, ...] = (),
    introduction: bool = False,
    missing: tuple[str, ...] = (),
    suggested: dict | None = None,
) -> CausalStepJudgment:
    return CausalStepJudgment(
        current_node_ref=ref,
        current_defect_status=status,
        current_defect_reason="Scripted semantic judgment for {0}.".format(ref),
        predecessors=predecessors,
        candidate_introduction=introduction,
        missing_evidence=missing,
        suggested_investigation=suggested,
        confidence=0.9 if status != "unknown" else 0.0,
    )


class ScriptedCausalJudge(OfflineJudgeCapability):
    def __init__(self, script):
        self.script = script
        self.requests = []
        self.request_count = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

    def judge_step(self, request):
        self.requests.append(request)
        self.request_count += 1
        key = (request.current_node.ref, request.defect_state.label)
        value = self.script.get(key, self.script.get(request.current_node.ref))
        if isinstance(value, list):
            value = value.pop(0)
        if callable(value):
            value = value(request)
        if isinstance(value, Exception):
            self.provider_circuit_open = isinstance(value, JudgeProviderUnavailable)
            self.provider_circuit_reason = str(value)
            raise value
        if value is None:
            return step(request.current_node.ref, status="absent")
        return value


class ConfirmingScriptedJudge(ScriptedCausalJudge):
    def __init__(self, script, confirmations):
        super().__init__(script)
        self.confirmations = dict(confirmations)
        self.confirmation_requests = []

    def confirm_candidate(self, request):
        self.confirmation_requests.append(request)
        result = self.confirmations[request.candidate_ref]
        if request.competing_hypotheses and not result.competitor_comparisons:
            result = replace(
                result,
                competitor_comparisons=tuple(
                    {
                        "hypothesis_id": item["hypothesis_id"],
                        "hypothesis_semantic_hash": item["hypothesis_semantic_hash"],
                        "candidate_ref": item["candidate_reference"]["resolved_ref"],
                        "defect_fingerprint": item["active_defect"]["fingerprint"],
                        "confirmation_identity": item["confirmation_identity"],
                        "recursive_path": list(item["recursive_path"]),
                        "requires_independent_confirmation": item[
                            "requires_independent_confirmation"
                        ],
                        "status": getattr(self, "comparison_statuses", {}).get(
                            (
                                request.candidate_ref,
                                item["candidate_reference"]["resolved_ref"],
                            ),
                            (
                            "co_root"
                            if result.status == "confirmed"
                            and (
                                (
                                    item["candidate_reference"]["resolved_ref"] in self.confirmations
                                    and self.confirmations[
                                        item["candidate_reference"]["resolved_ref"]
                                    ].status == "confirmed"
                                )
                                or item["candidate_reference"]["resolved_ref"]
                                in getattr(self, "force_co_root_candidates", set())
                            )
                            else "outperformed"
                            ),
                        ),
                        "reason": "The independent verifier compared this alternative.",
                        "evidence_refs": [item["candidate_reference"]["resolved_ref"]],
                    }
                    for item in request.competing_hypotheses
                    if item["status"] in {"active", "supported", "unresolved"}
                ),
            )
        if result.factor_role in {"contributing_condition", "amplifying_factor"}:
            result = replace(
                result,
                evidence_refs=tuple(dict.fromkeys((
                    *result.evidence_refs,
                    request.candidate_ref,
                    request.recursive_path[-1],
                ))),
                factor_mechanism={
                    "mechanism_type": (
                        "amplification"
                        if result.factor_role == "amplifying_factor"
                        else "enabling_condition"
                    ),
                    "source_ref": request.candidate_ref,
                    "target_ref": request.recursive_path[-1],
                    "effect": "The factor changes defect exposure without independently causing it.",
                },
            )
        return result


class BoundedConfirmingJudge(BoundedJudgeCapability):
    def __init__(self):
        self.transport = ScriptedTransport([])
        self.step_allowances = []
        self.confirmation_allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_allowances.append(max_physical_requests)
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                step(
                    request.current_node.ref,
                    status="unknown",
                    missing=("judge_request_budget_exhausted",),
                ),
                0,
            )
        self.transport.request_count += 1
        return BoundedJudgeCallResult(
            RecursiveRootRankingTest._confirmation_step(request), 1
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_allowances.append(max_physical_requests)
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(
                    request.candidate_ref, "judge_request_budget_exhausted"
                ),
                0,
            )
        self.transport.request_count += 1
        node_content = request.candidate_reference["content"]
        excerpt = "The decision is incomplete."
        if excerpt not in node_content:
            excerpt = "premature closure"
        return BoundedJudgeCallResult(
            RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt=excerpt,
                reason="The candidate contains the tracked defect.",
                counterfactual="Correcting the candidate prevents the defect.",
                confidence=0.9,
                evidence_refs=[request.candidate_ref],
            ),
            1,
        )


class CounterlessBoundedConfirmingJudge(BoundedJudgeCapability):
    def __init__(self):
        self.physical_calls = 0
        self.allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.allowances.append(("step", max_physical_requests))
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                step(request.current_node.ref, status="unknown", missing=("budget_exhausted",)),
                0,
            )
        self.physical_calls += 1
        return BoundedJudgeCallResult(
            RecursiveRootRankingTest._confirmation_step(request), 1
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.allowances.append(("confirmation", max_physical_requests))
        if not max_physical_requests:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(request.candidate_ref, "budget_exhausted"), 0
            )
        self.physical_calls += 1
        return BoundedJudgeCallResult(
            RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt="The decision is incomplete.",
                reason="The candidate contains the tracked defect.",
                counterfactual="Correcting it prevents the defect.",
                confidence=0.9,
                evidence_refs=[request.candidate_ref],
            ),
            1,
        )


class ScriptedTransport:
    model = "offline-test-model"
    max_tokens = 2048
    repair_max_tokens = 512
    thinking_config = None

    def __init__(self, responses):
        self.responses = list(responses)
        self.request_count = 0
        self.calls = []
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

    def create_message_text(self, *, system, messages, max_tokens):
        try:
            return self.create_message_text_with_usage(
                system=system, messages=messages, max_tokens=max_tokens
            ).text
        except TransportCallError as exc:
            raise exc.error from exc

    def create_message_text_with_usage(self, *, system, messages, max_tokens):
        self.request_count += 1
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise TransportCallError(value, physical_requests=1)
        return TransportCallResult(str(value), physical_requests=1)


class UnboundedProviderJudge:
    """Protocol-compatible Judge whose one logical call spends two requests."""

    def __init__(self):
        self.transport = ScriptedTransport([])
        self.calls = []

    def judge_step(self, request):
        self.calls.append(request)
        self.transport.request_count += 2
        return step(request.current_node.ref, introduction=True)


class LegacyOfflineJudge:
    def __init__(self):
        self.calls = []

    def judge_step(self, request):
        self.calls.append(request)
        return step(request.current_node.ref, introduction=True)


def single_node_trace(*, content: str = "", hydrated_artifacts=None) -> dict:
    data = {
        "failure_type": "incomplete_decision",
        "expected": "The decision covers the complete requirement.",
        "actual": "The decision is incomplete.",
        "mechanism": "premature closure",
        "scope": "repository_reasoning",
    }
    if content:
        data["content"] = content
    if hydrated_artifacts is not None:
        data["hydrated_artifacts"] = hydrated_artifacts
    return {
        "case_id": "single-node-case",
        "records": [
            {
                "record_id": "only",
                "component": "agent",
                "event_type": "decision",
                "data": data,
            }
        ],
    }


def valid_single_node_payload() -> dict:
    return {
        "current_node_ref": "record:only",
        "current_defect_status": "present",
        "current_defect_reason": "The decision itself contains the incomplete reasoning.",
        "predecessors": [],
        "candidate_introduction": True,
        "missing_evidence": [],
        "suggested_investigation": None,
        "confidence": 0.9,
    }


class RecursiveTraversalTest(unittest.TestCase):
    def test_present_defective_dead_end_is_explicitly_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "unrelated"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.step_judgments[0].current_defect_status, "present")
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertIn("record:change", report.unresolved_refs)
        self.assertIn(
            "defective_dead_end",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_queue_exhaustion_is_not_no_defect_after_any_present_judgment(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                ),
                "record:decision": step("record:decision", status="absent"),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(any(item.current_defect_status == "present" for item in report.step_judgments))
        self.assertTrue(report.unresolved_hypotheses)

    def test_same_defect_competing_predecessors_get_distinct_hypotheses(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": step("record:decision", introduction=True),
                "record:context": step("record:context", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=3).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Compare competing same-defect explanations.",
        )

        branch_hypotheses = {
            item.candidate_root_ref: item
            for item in report.hypotheses
            if item.candidate_root_ref in {"record:decision", "record:context"}
        }
        self.assertEqual(set(branch_hypotheses), {"record:decision", "record:context"})
        self.assertNotEqual(
            branch_hypotheses["record:decision"].hypothesis_id,
            branch_hypotheses["record:context"].hypothesis_id,
        )
        self.assertTrue(
            all(item.supporting_evidence for item in branch_hypotheses.values())
        )
        bindings = report.metadata["introduction_bindings"]
        self.assertEqual(
            {(item["candidate_ref"], item["hypothesis_id"]) for item in bindings},
            {
                (ref, hypothesis.hypothesis_id)
                for ref, hypothesis in branch_hypotheses.items()
            },
        )

    def test_same_defect_alternatives_obey_hypothesis_budget(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=1).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Compare competing same-defect explanations.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["hypotheses"], 2)
        self.assertEqual(
            [item.current_node.ref for item in judge.requests], ["record:change"]
        )
        self.assertEqual(report.introduction_candidates, ())

    def test_unrelated_candidate_is_retained_as_hypothesis_opposition(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "unrelated"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        change_hypothesis = next(
            item for item in report.hypotheses if item.candidate_root_ref == "record:change"
        )
        self.assertEqual(
            [item.ref for item in change_hypothesis.opposing_evidence],
            ["record:decision"],
        )

    def test_recursive_state_create_uses_default_hypothesis_budget(self):
        graph = TraceGraph.from_trace(observed_trace())

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
            analysis_perspective="Improve repository reasoning.",
        )

        self.assertTrue(state.frontier)

    def test_empty_start_refs_fall_back_to_graph_defaults(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=[],
            objective="Find the defect.",
        )

        self.assertEqual(report.start_refs, ("record:observed_defect",))
        self.assertEqual([item.current_node.ref for item in judge.requests], ["record:change"])

    def test_trace_without_analysis_start_is_inconclusive(self):
        report = AgenticRecursiveAnalyzer(judge=ScriptedCausalJudge({})).analyze(
            TraceGraph.from_trace({"case_id": "empty", "records": []}),
            objective="Find the defect.",
        )

        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertIn("analysis_start_missing", report.metadata["unresolved_branches"][0]["reason"])

    def test_analyzer_follows_transformed_defect_to_agent_decision(self):
        transformed = DefectState.create(
            label="incomplete_plan",
            expected="The plan covers the complete parser contract.",
            actual="The plan stops after the first search result.",
            mechanism="premature search closure",
            scope="repository_reasoning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                        ),
                    ),
                ),
                ("record:decision", "incomplete_plan"): step(
                    "record:decision", introduction=True
                ),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=16).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why the required method is missing.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(report.introduction_candidates[0].ref, "record:decision")
        self.assertIn("defect_transformation", [item.relation for item in report.causal_relations])
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.confirmed_roots, ())
        self.assertIn(
            ("record:decision", "record:change", "record:observed_defect"),
            report.taint_paths,
        )

    def test_evaluation_seed_becomes_outcome_evidence_without_judging_evaluator(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", introduction=True)}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the omitted implementation decision.",
        )

        self.assertEqual([item.current_node_ref for item in report.step_judgments], ["record:change"])
        self.assertIn("outcome_evidence", [item.relation for item in report.causal_relations])
        self.assertEqual(report.defect_states[0].label, "missing_namespace_contract")
        self.assertEqual(report.metadata["seed_count"], 1)

    def test_same_defect_propagation_preserves_defect_fingerprint(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                ),
                "record:decision": step("record:decision", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the omitted implementation decision.",
        )

        fingerprints = {request.defect_state.fingerprint for request in judge.requests}
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(report.visited_order, ("record:change", "record:decision"))

    def test_unknown_branch_is_unresolved_not_root(self):
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", status="unknown", missing=("artifact truncated",))}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.unresolved_refs, ("record:change",))
        self.assertIn("artifact truncated", report.metadata["unresolved_branches"][0]["details"])

    def test_missing_predecessor_evidence_does_not_propagate(self):
        unsupported = PredecessorAssessment(
            ref="record:decision",
            relation="same_defect_propagation",
            reason="The predecessor might carry the defect, but its artifact is missing.",
            confidence=0.4,
            recurse=True,
            evidence_refs=(),
            missing_evidence=("decision artifact missing",),
        )
        judge = ScriptedCausalJudge(
            {"record:change": step("record:change", predecessors=(unsupported,))}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual([item.current_node.ref for item in judge.requests], ["record:change"])
        self.assertIn("record:decision", report.unresolved_refs)
        self.assertIn(
            "predecessor_evidence_missing",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_fabricated_predecessor_is_unresolved_instead_of_crashing(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:not_in_trace", "same_defect_propagation"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertIn("record:not_in_trace", report.unresolved_refs)
        self.assertIn(
            "predecessor_ref_unresolved",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )

    def test_absent_seed_path_finishes_as_no_defect(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Check whether the observed gap is supported.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(report.unresolved_refs, ())
        self.assertEqual(report.introduction_candidates, ())

    def test_changed_defect_fingerprints_visit_the_same_predecessor_separately(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:change",)),
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:decision",),
                        ),
                    ),
                ),
                "record:decision": [
                    step("record:decision", introduction=True),
                    step("record:decision", introduction=True),
                ],
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Compare competing defect mechanisms.",
        )

        decision_requests = [item for item in judge.requests if item.current_node.ref == "record:decision"]
        self.assertEqual(len(decision_requests), 2)
        self.assertEqual(len({item.defect_state.fingerprint for item in decision_requests}), 2)
        self.assertEqual(len(report.introduction_candidates), 2)

    def test_duplicate_semantic_visits_merge_evidence_and_run_once(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:change",)),
                        relation("record:decision", "same_defect_propagation", evidence_refs=("record:prompt",)),
                    ),
                ),
                "record:decision": step("record:decision", introduction=True),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(
            len([item for item in judge.requests if item.current_node.ref == "record:decision"]),
            1,
        )
        merged = report.metadata["merged_visit_evidence"]
        self.assertEqual(set(merged.values()), {("record:change", "record:prompt")})

    def test_duplicate_transformation_reuses_hypothesis_at_budget_boundary(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:change",),
                        ),
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                            evidence_refs=("record:prompt",),
                        ),
                    ),
                ),
                ("record:decision", "planning_gap"): step(
                    "record:decision", introduction=True
                ),
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=2).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertNotIn("hypotheses", report.metadata["exhausted_budgets"])
        self.assertEqual(
            len([item for item in judge.requests if item.current_node.ref == "record:decision"]),
            1,
        )

    def test_request_contains_replayable_recursive_context(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", introduction=True)})

        AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find why the required method is missing.",
            analysis_perspective="Improve repository reasoning.",
        )

        context = judge.requests[0].recursive_context
        self.assertEqual(context["analysis_perspective"], "Improve repository reasoning.")
        self.assertEqual(context["objective"], "Find why the required method is missing.")
        self.assertTrue(context["evidence_hash"])
        self.assertEqual(context["downstream_path"], ("record:change", "record:observed_defect"))
        self.assertTrue(context["task_obligations"])


class RecursiveBudgetTest(unittest.TestCase):
    def test_typed_bounded_exception_atomically_consumes_usage_before_next_branch(self):
        class FailingThenExhaustedJudge(BoundedJudgeCapability):
            def __init__(self):
                self.allowances = []

            def judge_step_bounded(self, request, *, max_physical_requests):
                self.allowances.append(max_physical_requests)
                if len(self.allowances) == 1:
                    raise BoundedJudgeCallError(
                        "adapter failed after request", physical_requests=1
                    )
                return BoundedJudgeCallResult(
                    step(
                        request.current_node.ref,
                        status="unknown",
                        missing=("judge_request_budget_exhausted",),
                    ),
                    0,
                )

        trace = {
            "case_id": "two-branch-budget",
            "records": [
                {
                    "record_id": "first",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"failure_type": "first defect"},
                },
                {
                    "record_id": "second",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"failure_type": "second defect"},
                },
            ],
        }
        judge = FailingThenExhaustedJudge()

        report = AgenticRecursiveAnalyzer(
            judge=judge, max_judge_requests=1
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:first", "record:second"],
            objective="Inspect both failures.",
        )

        self.assertEqual(judge.allowances, [1, 0])
        self.assertEqual(report.metadata["physical_judge_request_count"], 1)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_unbounded_provider_judge_is_rejected_before_any_side_effect(self):
        judge = UnboundedProviderJudge()

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(judge.calls, [])
        self.assertEqual(judge.transport.request_count, 0)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 0)
        self.assertIn(
            "judge_budget_unenforceable",
            [item["reason"] for item in report.metadata["unresolved_branches"]],
        )
        self.assertEqual(report.introduction_candidates, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_explicit_adapter_preserves_legacy_zero_transport_judge(self):
        legacy = LegacyOfflineJudge()
        judge = OfflineCausalJudgeAdapter(legacy)

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(len(legacy.calls), 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertNotIn("judge_requests", report.metadata["exhausted_budgets"])
        self.assertEqual([item.ref for item in report.introduction_candidates], ["record:only"])

    def test_ordinary_content_text_does_not_consume_artifact_budget(self):
        judge = ScriptedCausalJudge({"record:only": step("record:only", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=8).analyze(
            TraceGraph.from_trace(single_node_trace(content="x" * 64)),
            start_refs=["record:only"],
            objective="Check the decision.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["artifact_bytes"], 0)
        self.assertNotIn("artifact_bytes", report.metadata["exhausted_budgets"])

    def test_duplicate_grounded_artifact_payload_is_counted_once(self):
        artifact = {
            "artifact_id": "artifact-1",
            "hash": "sha256:artifact-1",
            "path": "artifacts/decision.txt",
            "content": "abcdef",
        }
        judge = ScriptedCausalJudge({"record:only": step("record:only", status="absent")})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=6).analyze(
            TraceGraph.from_trace(
                single_node_trace(hydrated_artifacts=[artifact, dict(artifact)])
            ),
            start_refs=["record:only"],
            objective="Check the decision artifact.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["artifact_bytes"], 6)
        self.assertNotIn("artifact_bytes", report.metadata["exhausted_budgets"])

    def test_one_physical_request_budget_allows_valid_claude_judgment(self):
        transport = ScriptedTransport([json.dumps(valid_single_node_payload())])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertEqual([item.ref for item in report.introduction_candidates], ["record:only"])

    def test_cache_hit_costs_zero_physical_requests_with_zero_remaining_budget(self):
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport([json.dumps(valid_single_node_payload())])
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            graph = TraceGraph.from_trace(single_node_trace())
            AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
            )

            report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
            )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertNotIn("judge_requests", report.metadata["exhausted_budgets"])

    def test_repair_is_blocked_at_exact_physical_request_boundary(self):
        transport = ScriptedTransport(["not-json"])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(transport.request_count, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_frontier_budget_counts_items_that_stop_at_depth_limit(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge, max_depth=0, max_frontier_items=2
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["processed_frontier_items"], 2)
        self.assertEqual(report.metadata["exhausted_budgets"]["depth"], 1)
        self.assertEqual(report.metadata["exhausted_budgets"]["frontier_items"], 1)

    def test_depth_limit_is_explicit_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(relation("record:decision", "same_defect_propagation"),),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_depth=0).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["depth"], 1)
        self.assertIn("record:decision", report.unresolved_refs)
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_frontier_limit_marks_remaining_items_unresolved(self):
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_frontier_items=1).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["frontier_items"], 2)
        self.assertEqual(set(report.unresolved_refs), {"record:decision", "record:context"})

    def test_hypothesis_limit_rejects_new_transformation_as_unresolved(self):
        transformed = DefectState.create(
            label="planning_gap",
            expected="Complete plan",
            actual="Partial plan",
            mechanism="omitted search",
            scope="planning",
        )
        judge = ScriptedCausalJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation(
                            "record:decision",
                            "defect_transformation",
                            upstream_defect=transformed,
                        ),
                    ),
                )
            }
        )

        report = AgenticRecursiveAnalyzer(judge=judge, max_hypotheses=1).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.metadata["exhausted_budgets"]["hypotheses"], 1)
        self.assertIn("record:decision", report.unresolved_refs)

    def test_artifact_byte_limit_stops_before_judge(self):
        judge = ScriptedCausalJudge({})

        report = AgenticRecursiveAnalyzer(judge=judge, max_artifact_bytes=8).analyze(
            TraceGraph.from_trace(observed_trace(artifact_content="a" * 64)),
            start_refs=["record:decision"],
            objective="Inspect the decision artifact.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(report.metadata["exhausted_budgets"]["artifact_bytes"], 1)
        self.assertIn("record:decision", report.unresolved_refs)

    def test_explicit_offline_judge_uses_no_physical_request_budget(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", introduction=True)})

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.metadata["judge_request_count"], 0)
        self.assertEqual(report.metadata["logical_judge_call_count"], 1)
        self.assertNotIn("judge_requests", report.metadata["exhausted_budgets"])

    def test_provider_circuit_is_unresolved_and_preserves_no_root(self):
        judge = ScriptedCausalJudge(
            {"record:change": JudgeProviderUnavailable("provider circuit open")}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(report.metadata["provider_circuit"]["open"])
        self.assertEqual(report.metadata["exhausted_budgets"]["provider_circuit"], 1)

    def test_analysis_does_not_mutate_input_graph(self):
        graph = TraceGraph.from_trace(observed_trace(artifact_content="semantic evidence"))
        nodes_before = copy.deepcopy(graph.nodes)
        hydration_before = copy.deepcopy(graph.artifact_hydration)
        judge = ScriptedCausalJudge({"record:decision": step("record:decision", status="absent")})

        AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=["record:decision"],
            objective="Check the decision.",
        )

        self.assertEqual(graph.nodes, nodes_before)
        self.assertEqual(graph.artifact_hydration, hydration_before)


class RecursiveRootRankingTest(unittest.TestCase):
    @staticmethod
    def _confirmation_step(request):
        return step(
            request.current_node.ref,
            introduction=True,
            suggested={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently verify this introduction candidate.",
            },
        )

    def test_unknown_confirmation_never_becomes_root(self):
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {
                "record:only": RootConfirmation.unknown(
                    "record:only", "missing candidate-local evidence"
                )
            },
        )
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertIn(
            "root_confirmation_missing_evidence",
            {
                item["gap_type"]
                for item in report.metadata["trace_improvement_report"]["blocking_gaps"]
            },
        )
        self.assertEqual(judge.confirmation_requests.__len__(), 1)

    def test_rejected_candidate_backtracks_to_independently_confirmed_alternative(self):
        def first_step(request):
            return step(
                "record:change",
                predecessors=(
                    relation("record:decision", "same_defect_propagation"),
                    relation("record:context", "same_defect_propagation"),
                ),
            )

        judge = ConfirmingScriptedJudge(
            {
                "record:change": first_step,
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:context": RootConfirmation.rejected(
                    "record:context",
                    "availability alone did not introduce the defect",
                    factor_role="contributing_condition",
                ),
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision stopped repository discovery.",
                    counterfactual="A complete search would prevent the omission.",
                    confidence=0.91,
                    evidence_refs=["record:decision"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(
            [root.node_ref for root in report.confirmed_roots], ["record:decision"]
        )
        self.assertEqual(
            [item.node_ref for item in report.contributing_conditions],
            ["record:context"],
        )
        self.assertEqual(report.rejected_candidates, ())
        requests = {item.candidate_ref: item for item in judge.confirmation_requests}
        decision_request = requests["record:decision"]
        self.assertTrue(decision_request.hypothesis_id)
        self.assertEqual(
            decision_request.defect_state.fingerprint,
            next(
                item["defect_fingerprint"]
                for item in report.metadata["introduction_bindings"]
                if item["candidate_ref"] == "record:decision"
            ),
        )
        self.assertNotIn(
            "The decision semantically explains the current defect.",
            json.dumps(decision_request.to_dict()),
        )

    def test_unrelated_rejected_candidate_is_not_published_as_a_factor(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:context": RootConfirmation.rejected(
                    "record:context", "The context did not introduce the defect."
                ),
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision stopped repository discovery.",
                    counterfactual="A complete search would prevent the omission.",
                    confidence=0.91,
                    evidence_refs=["record:decision"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
        )

        self.assertEqual(
            [item.node_ref for item in report.rejected_candidates],
            ["record:context"],
        )
        self.assertEqual(report.contributing_conditions, ())
        self.assertEqual(report.amplifying_factors, ())

    def test_successful_no_defect_does_not_invoke_confirmation(self):
        judge = ConfirmingScriptedJudge(
            {"record:only": step("record:only", status="absent")}, {}
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Check the decision.",
        )

        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(judge.confirmation_requests, [])

    def test_step_and_confirmation_share_one_exact_physical_request_budget(self):
        exhausted = BoundedConfirmingJudge()
        unresolved = AgenticRecursiveAnalyzer(
            judge=exhausted, max_judge_requests=1
        ).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(exhausted.step_allowances, [1])
        self.assertEqual(exhausted.confirmation_allowances, [0])
        self.assertEqual(exhausted.transport.request_count, 1)
        self.assertEqual(unresolved.confirmed_roots, ())
        self.assertEqual(unresolved.metadata["judge_request_count"], 1)

        sufficient = BoundedConfirmingJudge()
        confirmed = AgenticRecursiveAnalyzer(
            judge=sufficient, max_judge_requests=2
        ).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(sufficient.step_allowances, [2])
        self.assertEqual(sufficient.confirmation_allowances, [1])
        self.assertEqual(sufficient.transport.request_count, 2)
        self.assertEqual([item.node_ref for item in confirmed.confirmed_roots], ["record:only"])
        self.assertEqual(confirmed.metadata["judge_request_count"], 2)

    def test_global_budget_uses_explicit_physical_delta_without_transport_counter(self):
        judge = CounterlessBoundedConfirmingJudge()

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=1).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(judge.allowances, [("step", 1), ("confirmation", 0)])
        self.assertEqual(judge.physical_calls, 1)
        self.assertEqual(report.metadata["judge_request_count"], 1)
        self.assertEqual(report.confirmed_roots, ())

    def test_multiple_independently_confirmed_roots_are_ranked_deterministically(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision was independently necessary.",
                    counterfactual="Correcting it prevents the omission.",
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt="The complete compatibility contract is documented here.",
                    reason="The context defect was independently necessary.",
                    counterfactual="Correcting it prevents the omission.",
                    confidence=0.8,
                    evidence_refs=["record:context"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual([item.node_ref for item in report.confirmed_roots], ["record:decision"])
        self.assertEqual([item.node_ref for item in report.co_roots], ["record:context"])
        self.assertEqual(
            [item["node_ref"] for item in report.to_dict()["root_causes"]],
            ["record:decision", "record:context"],
        )

    def test_declared_co_root_blocks_single_root_until_competitor_is_independently_confirmed(self):
        judge = ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision requires its co-root alternative.",
                    counterfactual="Correcting both prevents the omission.",
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.unknown(
                    "record:context", "The alternative lacks decisive evidence."
                ),
            },
        )
        judge.force_co_root_candidates = {"record:context"}

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_mutual_dominance_cannot_publish_primary_and_co_root(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "outperformed",
            ("record:context", "record:decision"): "outperformed",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_dominance_and_co_root_disagreement_blocks_both_confirmations(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "outperformed",
            ("record:context", "record:decision"): "co_root",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_rejected_and_confirmed_disagreement_blocks_both_confirmations(self):
        judge = self._two_confirmed_competitor_judge()
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "rejected",
            ("record:context", "record:decision"): "co_root",
        }

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find every necessary cause.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.co_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")

    def _two_confirmed_competitor_judge(self):
        return ConfirmingScriptedJudge(
            {
                "record:change": step(
                    "record:change",
                    predecessors=(
                        relation("record:decision", "same_defect_propagation"),
                        relation("record:context", "same_defect_propagation"),
                    ),
                ),
                "record:decision": self._confirmation_step,
                "record:context": self._confirmation_step,
            },
            {
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision was independently necessary.",
                    counterfactual="Correcting it prevents the omission.",
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt="The complete compatibility contract is documented here.",
                    reason="The context was independently necessary.",
                    counterfactual="Correcting it prevents the omission.",
                    confidence=0.8,
                    evidence_refs=["record:context"],
                ),
            },
        )

    def test_confirmation_cannot_cross_bind_hypothesis_defect_or_path_identity(self):
        forged = replace(
            RootConfirmation.confirmed(
                "record:only",
                excerpt="The decision is incomplete.",
                reason="A forged confirmation attempts to cross-bind another branch.",
                counterfactual="Correcting it prevents the defect.",
                confidence=0.9,
                evidence_refs=["record:only"],
            ),
            hypothesis_id="hyp:other",
            defect_fingerprint="defect:other",
            recursive_path=("record:other",),
        )
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {"record:only": forged},
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(single_node_trace()),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.confirmations[0].status, "unknown")
        self.assertIn("cross-bound", report.confirmations[0].reason)

    def test_candidate_artifact_excerpt_keeps_exact_identity_hash_and_byte_range(self):
        artifact_text = "Unique hydrated evidence: repository search stopped before call sites."
        judge = ConfirmingScriptedJudge(
            {"record:only": self._confirmation_step},
            {
                "record:only": RootConfirmation.confirmed(
                    "record:only",
                    excerpt=artifact_text,
                    reason="The hydrated candidate artifact contains the stopping decision.",
                    counterfactual="Continuing the search prevents the omission.",
                    confidence=0.9,
                    evidence_refs=["record:only"],
                )
            },
        )

        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(
                single_node_trace(
                    hydrated_artifacts=[
                        {
                            "artifact_id": "decision-evidence",
                            "hash": "sha256:decision-evidence",
                            "content": artifact_text,
                        }
                    ]
                )
            ),
            start_refs=["record:only"],
            objective="Find the defect.",
        )

        self.assertEqual([item.node_ref for item in report.confirmed_roots], ["record:only"])
        manifest = judge.confirmation_requests[0].candidate_reference[
            "artifact_hydration"
        ]
        hydrated = manifest["hydrated_artifacts"][0]
        self.assertEqual(hydrated["artifact_id"], "decision-evidence")
        import hashlib
        self.assertEqual(
            hydrated["content_hash"],
            "sha256:" + hashlib.sha256(artifact_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            hydrated["byte_range"], (0, len(artifact_text.encode("utf-8")))
        )

    def test_chinese_perspective_changes_only_post_confirmation_ranking(self):
        trace = observed_trace(branching=True)
        trace["records"][1]["data"]["rationale"] = "任务编排策略提前结束代码搜索。"
        trace["records"][2]["data"]["text"] = "需求描述质量存在歧义。"

        def run(perspective):
            judge = ConfirmingScriptedJudge(
                {
                    "record:change": step(
                        "record:change",
                        predecessors=(
                            relation("record:decision", "same_defect_propagation"),
                            relation("record:context", "same_defect_propagation"),
                        ),
                    ),
                    "record:decision": self._confirmation_step,
                    "record:context": self._confirmation_step,
                },
                {
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt="任务编排策略提前结束代码搜索。",
                        reason="Independent task-orchestration cause.",
                        counterfactual="Correct orchestration prevents the defect.",
                        confidence=0.9,
                        evidence_refs=["record:decision"],
                    ),
                    "record:context": RootConfirmation.confirmed(
                        "record:context",
                        excerpt="需求描述质量存在歧义。",
                        reason="Independent requirement-quality cause.",
                        counterfactual="Clear requirements prevent the defect.",
                        confidence=0.9,
                        evidence_refs=["record:context"],
                    ),
                },
            )
            report = AgenticRecursiveAnalyzer(judge=judge).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find every necessary cause.",
                analysis_perspective=perspective,
            )
            return report, judge

        requirement, requirement_judge = run("重点评估需求描述质量")
        orchestration, orchestration_judge = run("重点评估任务编排策略")

        self.assertEqual(requirement.confirmed_roots[0].node_ref, "record:context")
        self.assertEqual(orchestration.confirmed_roots[0].node_ref, "record:decision")
        self.assertTrue(all(not item.analysis_perspective for item in requirement_judge.confirmation_requests))
        self.assertEqual(
            [item.factual_dict() for item in requirement_judge.confirmation_requests],
            [item.factual_dict() for item in orchestration_judge.confirmation_requests],
        )


if __name__ == "__main__":
    unittest.main()
