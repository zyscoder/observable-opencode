from __future__ import annotations

import unittest

from trace_attribution.confirmation_path import is_confirmation_causal_edge


class ConfirmationPathPolicyTest(unittest.TestCase):
    def test_rejects_complete_graph_temporal_only_classification(self):
        temporal_edges = (
            {"relation": "temporal_availability"},
            {"relation": "available_to_next_request"},
            {"relation": "temporal_adjacency"},
            {"relation": "produced", "evidence_type": "temporal_inferred"},
            {"relation": "produced", "evidence_type": "temporal_only"},
            {"relation": "produced", "evidence_type": "temporal_advisory"},
            {"relation": "produced", "edge_origin": "offline.temporal_reconstruction"},
            {"relation": "produced", "inference_method": "same_session_temporal_order"},
        )

        for edge in temporal_edges:
            with self.subTest(edge=edge):
                self.assertFalse(
                    is_confirmation_causal_edge(
                        {**edge, "eligible_for_attribution": True},
                        default_eligible=False,
                    )
                )

    def test_rejects_navigation_fallback_and_progress_relations(self):
        for relation in (
            "semantic_navigation_route",
            "fallback_sequence",
            "previous_progress_episode",
            "progress_episode_member",
            "progress_episode_projects_to_target",
        ):
            with self.subTest(relation=relation):
                self.assertFalse(
                    is_confirmation_causal_edge(
                        {
                            "relation": relation,
                            "eligible_for_attribution": True,
                        },
                        default_eligible=False,
                    )
                )

    def test_fails_closed_for_empty_missing_and_unknown_relations(self):
        for edge in (
            {},
            {"relation": ""},
            {"relation": "   "},
            {"relation": "unregistered_causal_guess"},
        ):
            with self.subTest(edge=edge):
                self.assertFalse(
                    is_confirmation_causal_edge(
                        {**edge, "eligible_for_attribution": True},
                        default_eligible=False,
                    )
                )

    def test_accepts_repository_causal_relations_with_graph_canonicalization(self):
        for relation in (
            "produced",
            "derived_from",
            "motivated_by_evidence",
            "selected_by",
            "supports_claim",
            "external_evaluation_observed",
            "record_source",
            "reasoning_selected_action",
            "retained_in_context",
            "authored_change_reconstructed",
            "decision_guided_claim",
            "  PrOdUcEd  ",
        ):
            with self.subTest(relation=relation):
                self.assertTrue(
                    is_confirmation_causal_edge(
                        {
                            "relation": relation,
                            "eligible_for_attribution": True,
                        },
                        default_eligible=False,
                    )
                )

    def test_accepts_fact_grounded_process_lifecycle_reconstruction(self):
        self.assertTrue(
            is_confirmation_causal_edge(
                {
                    "relation": "process_lifecycle_observed",
                    "evidence_type": "offline_reconstruction",
                    "edge_origin": (
                        "offline.process_lifecycle_reconstruction"
                    ),
                    "inference_method": (
                        "candidate_process_trajectory_v1"
                    ),
                    "eligible_for_attribution": True,
                },
                default_eligible=False,
            )
        )

    def test_rejects_ineligible_repository_causal_relation(self):
        self.assertFalse(
            is_confirmation_causal_edge(
                {"relation": "produced", "eligible_for_attribution": False},
                default_eligible=True,
            )
        )

    def test_requires_exact_boolean_true_eligibility(self):
        for value in (False, "false", "true", 0, 1, None, [], {}):
            with self.subTest(value=value):
                self.assertFalse(
                    is_confirmation_causal_edge(
                        {
                            "relation": "produced",
                            "eligible_for_attribution": value,
                        },
                        default_eligible=True,
                    )
                )

        self.assertTrue(
            is_confirmation_causal_edge(
                {"relation": "produced"},
                default_eligible=True,
            )
        )
        self.assertFalse(
            is_confirmation_causal_edge(
                {"relation": "produced"},
                default_eligible=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
