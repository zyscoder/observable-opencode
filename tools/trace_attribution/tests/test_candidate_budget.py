import unittest

from trace_attribution.candidate_budget import (
    CandidateBudgetPolicy,
    candidate_identity,
    classify_candidate,
    quality_first_candidate_budget,
    select_global_candidates,
)
from trace_attribution.causal_state import CausalCandidate
from trace_attribution.models import TraceNode, stable_json


class FakeGraph:
    def resolve(self, ref):
        return ref


def candidate(
    ref,
    *,
    event_type,
    source="attribution_edge",
    edge=None,
    score=0.0,
    data=None,
):
    return CausalCandidate(
        ref=ref,
        node=TraceNode(
            ref=ref,
            record_id=ref.removeprefix("record:"),
            component="test",
            event_type=event_type,
            data={
                "payload": "candidate-private-detail",
                **(data or {}),
            },
        ),
        source=source,
        edge=edge or {},
        score=score,
    )


class CandidateBudgetTest(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()

    def test_new_selection_emits_the_replayable_v4_funnel_schema(self):
        selection = select_global_candidates(
            self.graph,
            (candidate("record:decision", event_type="decision"),),
        )

        self.assertEqual(
            selection.to_dict()["schema"],
            "candidate-budget-funnel/v4",
        )

    def test_benchmark_score_aliases_do_not_change_shared_candidate_identity(self):
        baseline = candidate("record:identity", event_type="decision")
        aliases = (
            "benchmark_score",
            "benchmarkScore",
            "BenchmarkScore",
            "BENCHMARK_SCORE",
            "evaluationScore",
            "EvaluationScore",
            "reviewerScore",
            "ReviewerScore",
        )

        for alias in aliases:
            with self.subTest(alias=alias):
                relabeled = candidate(
                    "record:identity",
                    event_type="decision",
                    data={alias: 99},
                )
                nested = candidate(
                    "record:identity",
                    event_type="decision",
                    data={"metadata": {alias: 99}},
                )
                self.assertEqual(
                    candidate_identity(relabeled),
                    candidate_identity(baseline),
                )
                self.assertEqual(
                    candidate_identity(nested),
                    candidate_identity(baseline),
                )

    def test_episode_reserve_keeps_early_plan_and_late_execution(self):
        ordinary = tuple(
            candidate(
                "record:ordinary-{0:03d}".format(index),
                event_type="message.input",
            )
            for index in range(300)
        )
        early_plan = candidate(
            "record:early-plan",
            event_type="decision",
            data={
                "episode_id": "implementation-a",
                "phase": "planning",
            },
        )
        late_execution = candidate(
            "record:late-execution",
            event_type="tool.result",
            data={
                "episode_id": "implementation-a",
                "phase": "implementation",
            },
        )

        selection = select_global_candidates(
            self.graph,
            (*ordinary, early_plan, late_execution),
            grounded_decision_refs=(early_plan.ref,),
            candidate_paths={
                early_plan.ref: (early_plan.ref, "record:defect"),
                late_execution.ref: (
                    late_execution.ref,
                    "record:defect",
                ),
            },
        )
        payload = selection.to_dict()
        audit = {
            item["ref"]: item for item in payload["candidate_audit"]
        }

        self.assertEqual(
            [item.ref for item in selection.offered[:2]],
            [early_plan.ref, late_execution.ref],
        )
        self.assertEqual(
            payload["reserved_episode_refs"],
            [early_plan.ref, late_execution.ref],
        )
        self.assertEqual(audit[early_plan.ref]["episode_role"], "authored_plan")
        self.assertEqual(audit[early_plan.ref]["reserve_rank"], 0)
        self.assertEqual(audit[late_execution.ref]["episode_role"], "execution")
        self.assertEqual(audit[late_execution.ref]["reserve_rank"], 1)
        self.assertEqual(audit[late_execution.ref]["grounded_hops"], 1)

    def test_selector_derives_policy_after_candidate_deduplication(self):
        candidates = tuple(
            candidate(
                "record:item-{0:02d}".format(index),
                event_type="message.input",
            )
            for index in range(25)
        ) + (
            candidate("record:item-24", event_type="decision"),
        )

        selection = select_global_candidates(self.graph, candidates)

        self.assertEqual(len(selection.discovered), 25)
        self.assertEqual(
            selection.policy,
            CandidateBudgetPolicy(96, 16),
        )

    def test_policy_rejects_more_than_one_hundred_twenty_eight_reserve_slots(self):
        with self.assertRaisesRegex(
            ValueError,
            "grounded_decision_reserve must not exceed 128",
        ):
            CandidateBudgetPolicy(
                total_limit=256,
                grounded_decision_reserve=129,
            )

    def test_quality_first_policy_expands_before_candidate_loss(self):
        self.assertEqual(
            quality_first_candidate_budget(24),
            CandidateBudgetPolicy(24, 4),
        )
        self.assertEqual(
            quality_first_candidate_budget(25),
            CandidateBudgetPolicy(96, 16),
        )
        self.assertEqual(
            quality_first_candidate_budget(97),
            CandidateBudgetPolicy(192, 32),
        )
        self.assertEqual(
            quality_first_candidate_budget(193),
            CandidateBudgetPolicy(256, 128),
        )

    def test_classification_uses_exclusive_semantic_categories(self):
        cases = (
            (candidate("record:decision", event_type="decision"), "authored_decision"),
            (
                candidate("record:tool", event_type="tool.result"),
                "tool_change_envelope",
            ),
            (
                candidate("record:verification", event_type="verification"),
                "verification_outcome",
            ),
            (
                candidate("record:lifecycle", event_type="process.signal"),
                "factor_or_lifecycle",
            ),
            (candidate("record:other", event_type="message.input"), "other"),
        )

        self.assertEqual(
            [classify_candidate(self.graph, item) for item, _ in cases],
            [classification for _, classification in cases],
        )

    def test_duplicate_refs_preserve_the_first_discovered_candidate(self):
        first = candidate(
            "record:duplicate",
            event_type="tool.result",
            score=0.1,
        )
        duplicate = candidate(
            "record:duplicate",
            event_type="decision",
            score=1.0,
        )

        selection = select_global_candidates(
            self.graph,
            (first, duplicate),
        )

        self.assertEqual(selection.discovered, (first,))
        self.assertEqual(selection.classifications, (("record:duplicate", "tool_change_envelope"),))

    def test_confirmed_fields_do_not_create_a_reservation_without_explicit_refs(self):
        ordinary = tuple(
            candidate("record:ordinary-{0:02d}".format(index), event_type="message.input")
            for index in range(24)
        )
        confirmed_decision = candidate(
            "record:confirmed-decision",
            event_type="decision",
            source="confirmed_edge",
            edge={"evidence_type": "confirmed"},
        )

        selection = select_global_candidates(
            self.graph,
            ordinary + (confirmed_decision,),
        )

        self.assertEqual(selection.reserved_grounded_decision_refs, ())
        self.assertEqual(
            [item.ref for item in selection.offered],
            [item.ref for item in ordinary + (confirmed_decision,)],
        )
        self.assertEqual(selection.dropped, ())

    def test_at_most_four_explicit_grounded_decisions_are_pulled_forward(self):
        ordinary = tuple(
            candidate("record:ordinary-{0:02d}".format(index), event_type="message.input")
            for index in range(19)
        )
        decisions = tuple(
            candidate("record:decision-{0}".format(index), event_type="decision")
            for index in range(5)
        )

        selection = select_global_candidates(
            self.graph,
            ordinary + decisions,
            grounded_decision_refs=tuple(item.ref for item in decisions),
        )

        self.assertEqual(
            selection.reserved_grounded_decision_refs,
            tuple(item.ref for item in decisions[:4]),
        )
        self.assertEqual(
            [item.ref for item in selection.offered],
            [item.ref for item in decisions[:4]]
            + [item.ref for item in ordinary]
            + [decisions[4].ref],
        )
        self.assertEqual(selection.dropped, ())
        self.assertEqual(
            selection.to_dict()["grounded_decision_refs"],
            [item.ref for item in decisions],
        )

    def test_grounded_decision_reserve_respects_explicit_quality_order(self):
        ordinary = tuple(
            candidate(
                "record:ordinary-{0:03d}".format(index),
                event_type="message.input",
            )
            for index in range(256)
        )
        decisions = tuple(
            candidate(
                "record:decision-{0:02d}".format(index),
                event_type="decision",
            )
            for index in range(65)
        )
        preferred = decisions[-1]

        selection = select_global_candidates(
            self.graph,
            ordinary + decisions,
            grounded_decision_refs=(
                preferred.ref,
                *(item.ref for item in decisions[:-1]),
            ),
        )

        self.assertEqual(
            selection.reserved_grounded_decision_refs[0],
            preferred.ref,
        )
        self.assertIn(preferred, selection.offered)
        self.assertNotIn(preferred, selection.dropped)

    def test_large_pool_keeps_grounded_decisions_beyond_old_sixty_four_limit(self):
        ordinary = tuple(
            candidate(
                "record:ordinary-{0:03d}".format(index),
                event_type="message.input",
            )
            for index in range(300)
        )
        decisions = tuple(
            candidate(
                "record:decision-{0:03d}".format(index),
                event_type="decision",
            )
            for index in range(110)
        )

        selection = select_global_candidates(
            self.graph,
            ordinary + decisions,
            grounded_decision_refs=tuple(
                item.ref for item in decisions
            ),
        )

        self.assertIn(decisions[81], selection.offered)
        self.assertIn(
            decisions[81].ref,
            selection.reserved_grounded_decision_refs,
        )
        self.assertEqual(
            selection.policy,
            CandidateBudgetPolicy(256, 128),
        )

    def test_selection_identity_covers_grounded_decisions_beyond_the_reserve(self):
        decisions = tuple(
            candidate("record:decision-{0}".format(index), event_type="decision")
            for index in range(5)
        )
        four_grounded = select_global_candidates(
            self.graph,
            decisions,
            grounded_decision_refs=tuple(item.ref for item in decisions[:4]),
        )
        five_grounded = select_global_candidates(
            self.graph,
            decisions,
            grounded_decision_refs=tuple(item.ref for item in decisions),
        )

        self.assertEqual(four_grounded.offered, five_grounded.offered)
        self.assertNotEqual(
            four_grounded.selection_identity,
            five_grounded.selection_identity,
        )
        self.assertEqual(
            five_grounded.to_dict()["grounded_decision_refs"],
            [item.ref for item in decisions],
        )

    def test_ordinary_candidates_keep_the_caller_input_order(self):
        candidates = (
            candidate("record:third", event_type="message.input", score=0.1),
            candidate("record:first", event_type="tool.result", score=1.0),
            candidate("record:second", event_type="verification", score=0.5),
        )

        selection = select_global_candidates(self.graph, candidates)

        self.assertEqual(selection.discovered, candidates)
        self.assertEqual(selection.offered, candidates)

    def test_unused_reservation_slots_return_to_the_input_ordered_pool(self):
        grounded_decision = candidate("record:decision", event_type="decision")
        others = tuple(
            candidate("record:other-{0:02d}".format(index), event_type="message.input")
            for index in range(300)
        )

        selection = select_global_candidates(
            self.graph,
            (grounded_decision,) + others,
            grounded_decision_refs=(grounded_decision.ref,),
        )

        self.assertEqual(len(selection.offered), 256)
        self.assertEqual(selection.offered[0], grounded_decision)
        self.assertEqual(selection.offered[1:], others[:255])
        self.assertEqual(selection.dropped, others[255:])

    def test_causally_unreachable_candidate_is_context_but_never_assessed(self):
        unreachable_decision = candidate(
            "record:unreachable-decision",
            event_type="decision",
        )
        reachable = candidate(
            "record:reachable",
            event_type="tool.result",
        )

        selection = select_global_candidates(
            self.graph,
            (unreachable_decision, reachable),
            grounded_decision_refs=(unreachable_decision.ref,),
            ineligible_reasons={
                unreachable_decision.ref: "no_active_seed_causal_path",
            },
        )
        payload = selection.to_dict()

        self.assertEqual(selection.offered, (reachable,))
        self.assertEqual(
            selection.evidence_context,
            (unreachable_decision,),
        )
        self.assertEqual(selection.dropped, ())
        self.assertEqual(selection.grounded_decision_refs, ())
        self.assertEqual(
            payload["context_reasons"],
            {"no_active_seed_causal_path": 1},
        )
        self.assertEqual(payload["drop_reasons"], {"total_limit": 0})
        self.assertEqual(
            payload["candidate_audit"][0]["disposition"],
            "evidence_context",
        )
        self.assertEqual(
            payload["candidate_audit"][0]["reason"],
            "no_active_seed_causal_path",
        )

    def test_audit_counts_are_conserved_and_compact(self):
        candidates = tuple(
            candidate("record:{0:02d}".format(index), event_type="message.input")
            for index in range(260)
        )

        selection = select_global_candidates(self.graph, candidates)
        payload = selection.to_dict()

        self.assertEqual(payload["discovered_count"], 260)
        self.assertEqual(payload["offered_count"], 256)
        self.assertEqual(payload["dropped_count"], 4)
        self.assertEqual(
            payload["counts_by_category"]["other"],
            {
                "discovered": 260,
                "offered": 256,
                "evidence_context": 0,
                "dropped": 4,
            },
        )
        self.assertEqual(payload["drop_reasons"], {"total_limit": 4})
        self.assertEqual(
            payload["context_reasons"],
            {"no_active_seed_causal_path": 0},
        )
        self.assertEqual(len(payload["candidate_audit"]), 260)
        self.assertTrue(
            all(
                set(item)
                == {
                    "ref",
                    "category",
                    "discovered_rank",
                    "offered_rank",
                    "context_rank",
                    "assessment_eligible",
                    "disposition",
                    "reason",
                    "grounded_hops",
                    "episode_key",
                    "episode_role",
                    "reserve_rank",
                    "reserve_disposition",
                    "reserve_reason",
                    "candidate_identity",
                }
                for item in payload["candidate_audit"]
            )
        )
        self.assertTrue(
            all(len(item["candidate_identity"]) == 64 for item in payload["candidate_audit"])
        )
        compact = stable_json(payload)
        self.assertNotIn("candidate-private-detail", compact)
        self.assertNotIn('"node"', compact)
        self.assertNotIn('"edge"', compact)
        self.assertEqual(
            payload["discovered_count"],
            payload["offered_count"] + payload["dropped_count"],
        )

    def test_selection_identity_is_stable_for_the_same_input(self):
        candidates = (
            candidate("record:a", event_type="decision"),
            candidate("record:b", event_type="message.input"),
        )

        first = select_global_candidates(
            self.graph,
            candidates,
            grounded_decision_refs=("record:a",),
        )
        replay = select_global_candidates(
            self.graph,
            candidates,
            grounded_decision_refs=("record:a",),
        )

        self.assertEqual(first.selection_identity, replay.selection_identity)
        self.assertEqual(
            first.to_dict()["selection_identity"],
            first.selection_identity,
        )

    def test_selection_identity_excludes_retrieval_ranking_signals(self):
        def selection(*, score, confidence):
            item = candidate(
                "record:a",
                event_type="decision",
                source="semantic_fallback",
                edge={
                    "from_ref": "record:a",
                    "to_ref": "record:defect",
                    "relation": "semantic_predecessor_match",
                    "evidence_type": "semantic_inferred",
                    "retrieval_candidate": True,
                    "confidence": confidence,
                },
                score=score,
            )
            return select_global_candidates(
                self.graph,
                [item],
            )

        low = selection(score=0.01, confidence=0.02)
        high = selection(score=0.99, confidence=0.98)

        self.assertEqual(low.selection_identity, high.selection_identity)
        self.assertEqual(
            low.to_dict()["candidate_audit"][0]["candidate_identity"],
            high.to_dict()["candidate_audit"][0]["candidate_identity"],
        )


if __name__ == "__main__":
    unittest.main()
