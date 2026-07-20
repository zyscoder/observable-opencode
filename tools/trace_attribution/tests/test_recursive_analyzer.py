from __future__ import annotations

import copy
import unittest

from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    PredecessorAssessment,
)
from trace_attribution.errors import JudgeProviderUnavailable
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
) -> CausalStepJudgment:
    return CausalStepJudgment(
        current_node_ref=ref,
        current_defect_status=status,
        current_defect_reason="Scripted semantic judgment for {0}.".format(ref),
        predecessors=predecessors,
        candidate_introduction=introduction,
        missing_evidence=missing,
        confidence=0.9 if status != "unknown" else 0.0,
    )


class ScriptedCausalJudge:
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
        if isinstance(value, Exception):
            self.provider_circuit_open = isinstance(value, JudgeProviderUnavailable)
            self.provider_circuit_reason = str(value)
            raise value
        if value is None:
            return step(request.current_node.ref, status="absent")
        return value


class RecursiveTraversalTest(unittest.TestCase):
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

    def test_judge_request_limit_is_checked_before_call(self):
        judge = ScriptedCausalJudge({"record:change": step("record:change", introduction=True)})

        report = AgenticRecursiveAnalyzer(judge=judge, max_judge_requests=0).analyze(
            TraceGraph.from_trace(observed_trace()),
            start_refs=["record:observed_defect"],
            objective="Find the defect.",
        )

        self.assertEqual(judge.requests, [])
        self.assertEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)
        self.assertEqual(report.analysis_outcome, "inconclusive")

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


if __name__ == "__main__":
    unittest.main()
