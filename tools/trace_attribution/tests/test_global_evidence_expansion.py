from __future__ import annotations

from dataclasses import replace
import unittest

from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from tools.trace_attribution.tests.test_recursive_analyzer import (
    FusionScriptedJudge,
    observed_trace,
)


def expandable_observed_graph() -> TraceGraph:
    trace = observed_trace()
    trace["records"][1]["source_refs"] = ["record:prompt"]
    trace["dataflow_edges"].insert(
        0,
        {
            "from": {"type": "record", "id": "prompt"},
            "to": {"type": "record", "id": "decision"},
            "relation": "prompt_informed_decision",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        },
    )
    return TraceGraph.from_trace(trace)


class SequencedGlobalJudge(FusionScriptedJudge):
    def __init__(self, outcomes):
        super().__init__(global_outcome=outcomes[0])
        self.outcomes = list(outcomes)

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_outcome = self.outcomes.pop(0)
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class RoundBudgetGlobalJudge(FusionScriptedJudge):
    def __init__(self):
        super().__init__(global_outcome="needs_expansion")
        self.context_kinds = iter(
            ("upstream", "downstream", "full_node", "upstream")
        )

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        result = super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        context_kind = next(self.context_kinds)
        round_number = len(self.global_requests)
        return replace(
            result,
            value=replace(
                result.value,
                expansion_requests=(
                    {
                        "anchor_ref": "record:decision",
                        "context_kind": context_kind,
                        "reason": "Inspect bounded evidence round {0}.".format(
                            round_number
                        ),
                        "expected_judgment_change": (
                            "Resolve the candidate role after expansion round "
                            "{0}."
                        ).format(round_number),
                    },
                ),
            ),
        )


class GlobalEvidenceExpansionLoopTest(unittest.TestCase):
    def test_global_judge_rejudges_with_bounded_expanded_evidence(self):
        judge = SequencedGlobalJudge(["needs_expansion", "no_defect"])

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            expandable_observed_graph(),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(len(judge.global_requests), 2)
        self.assertEqual(judge.global_requests[0].evidence_expansions, ())
        second_expansions = judge.global_requests[1].evidence_expansions
        self.assertEqual(len(second_expansions), 1)
        self.assertEqual(second_expansions[0].status, "expanded")
        self.assertEqual(
            second_expansions[0].resolved_refs,
            ("record:prompt",),
        )
        self.assertEqual(judge.requests, [])
        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "no_defect")
        self.assertEqual(len(seed.expansion_history), 1)
        self.assertEqual(seed.expansion_history[0]["status"], "expanded")
        self.assertEqual(
            seed.global_judgment["validation_envelope"][
                "evidence_expansions"
            ][0]["request_identity"],
            seed.expansion_history[0]["request_identity"],
        )

    def test_duplicate_expansion_request_stops_as_concrete_evidence_gap(self):
        judge = SequencedGlobalJudge(
            ["needs_expansion", "needs_expansion", "no_defect"]
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            expandable_observed_graph(),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(len(judge.global_requests), 2)
        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "evidence_gap")
        self.assertIn(
            "global_evidence_expansion_duplicate_request",
            seed.blocking_reasons,
        )
        self.assertEqual(
            [item["status"] for item in seed.expansion_history],
            ["expanded", "rejected"],
        )
        self.assertEqual(
            seed.expansion_history[-1]["rejection_code"],
            "duplicate_request",
        )

    def test_round_budget_exhaustion_becomes_concrete_evidence_gap(self):
        judge = RoundBudgetGlobalJudge()

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            expandable_observed_graph(),
            start_refs=["record:observed_defect"],
            objective="Find the primary trace-visible root.",
        )

        self.assertEqual(len(judge.global_requests), 4)
        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "evidence_gap")
        self.assertIn(
            "global_evidence_expansion_round_budget_exhausted",
            seed.blocking_reasons,
        )
        self.assertEqual(len(seed.expansion_history), 3)
        self.assertEqual(
            [item["status"] for item in seed.expansion_history],
            ["expanded", "expanded", "expanded"],
        )
        self.assertEqual(
            [
                item["request"]["context_kind"]
                for item in seed.expansion_history
            ],
            ["upstream", "downstream", "full_node"],
        )


if __name__ == "__main__":
    unittest.main()
