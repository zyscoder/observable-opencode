import unittest

from trace_attribution.causal_state import (
    AttributionHypothesis,
    CausalCandidate,
    CausalFactor,
    CausalStepJudgment,
    ConfirmedRoot,
    DefectState,
    FrontierItem,
    HypothesisEvidence,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RejectedCandidate,
    RootConfirmation,
    semantic_visit_key,
)
from trace_attribution.models import TraceNode


def sample_defect_state():
    return DefectState.create(
        label="missing_parse_namespace_object_contract",
        expected="compatibility method exists",
        actual="method is absent",
        mechanism="implementation omission",
        scope="parser_contract_recovery",
    )


def sample_frontier_item():
    defect_state = sample_defect_state()
    return FrontierItem.create(
        node_ref="record:dec_85",
        defect_state=defect_state,
        downstream_path=["record:observed", "record:change", "record:dec_85"],
        hypothesis_id="hyp_a",
        hypothesis_semantic_hash="hyp_semantic_a",
        depth=2,
        candidate_source="confirmed_edge",
        priority=0.85,
        checked_evidence_refs=["record:change"],
        graph_position=17,
    )


class CausalStateTest(unittest.TestCase):
    def test_transformed_defect_preserves_chain_and_changes_visit_identity(self):
        downstream = sample_defect_state()
        upstream = downstream.transformed(
            label="premature_repository_search_closure",
            mechanism="the plan stopped before searching call sites",
            transformation_reason="the incomplete search produced the incomplete plan",
        )

        self.assertEqual(upstream.derived_from_defect_state_id, downstream.defect_state_id)
        self.assertEqual(upstream.expected, downstream.expected)
        self.assertEqual(upstream.actual, downstream.actual)
        self.assertEqual(upstream.scope, downstream.scope)
        self.assertNotEqual(
            semantic_visit_key("record:dec_85", downstream, "hyp_a"),
            semantic_visit_key("record:dec_85", upstream, "hyp_a"),
        )

    def test_frontier_round_trip_preserves_semantic_identity(self):
        item = sample_frontier_item()
        self.assertEqual(FrontierItem.from_dict(item.to_dict()), item)
        self.assertEqual(
            item.visit_key,
            semantic_visit_key(item.node_ref, item.defect_state, item.hypothesis_semantic_hash),
        )

    def test_recursive_state_types_round_trip_and_report_projects_legacy_fields(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
            title="Stop discovery",
            data={"rationale": "The search is complete."},
        )
        candidate = CausalCandidate(
            ref=node.ref,
            node=node,
            source="confirmed_edge",
            edge={"relation": "reasoning_selected_action", "confidence": 1.0},
            score=0.9,
            evidence_refs=["record:change"],
        )
        assessment = PredecessorAssessment(
            ref=node.ref,
            relation="defect_transformation",
            reason="The incomplete plan produced the incomplete change.",
            confidence=0.9,
            recurse=True,
            upstream_defect=defect_state.transformed(
                label="incomplete_plan",
                mechanism="call sites were not searched",
                transformation_reason="the plan controlled authored methods",
            ),
            evidence_refs=["record:change"],
        )
        judgment = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="present",
            current_defect_reason="The change omits the method.",
            predecessors=[assessment],
            confidence=0.9,
        )
        evidence = HypothesisEvidence("record:decision", "The plan declared completion.", 0.8)
        hypothesis = AttributionHypothesis.create(
            "Agent prematurely narrowed implementation.", node.ref, defect_state
        )
        hypothesis = hypothesis.with_updates(
            supporting_evidence=[evidence],
            unresolved_questions=["Were call sites searched?"],
        )
        confirmation = RootConfirmation.confirmed(
            node.ref,
            excerpt="Now I have a comprehensive understanding",
            reason="The decision stopped discovery before checking call sites.",
            counterfactual="Searching call sites would reveal the method.",
            confidence=0.9,
            evidence_refs=[node.ref],
        )
        root = ConfirmedRoot(
            node_ref=node.ref,
            defect_state=defect_state,
            reason=confirmation.reason,
            counterfactual=confirmation.counterfactual,
            confidence=confirmation.confidence,
            evidence_refs=confirmation.evidence_refs,
            component=node.component,
            event_type=node.event_type,
            defect_type=defect_state.label,
            episode_id="episode:decision",
            episode_member_refs=[node.ref],
            observed_defect_refs=["record:observed"],
        )
        factor = CausalFactor(
            node_ref="record:prompt",
            relation="contributing_condition",
            reason="The request omitted a compatibility hint.",
            confidence=0.6,
            evidence_refs=["record:prompt"],
        )
        rejected = RejectedCandidate(
            node_ref="record:prompt",
            reason="Repository search could still satisfy the task.",
            evidence_refs=["record:decision"],
        )
        report = RecursiveAttributionReport(
            case_id="case-1",
            objective="Find the defect origin.",
            start_refs=["record:observed"],
            analysis_outcome="inconclusive",
            analysis_perspective="Improve Agent repository reasoning.",
            defect_states=[defect_state],
            causal_candidates=[candidate],
            causal_relations=[assessment],
            step_judgments=[judgment],
            hypotheses=[hypothesis],
            introduction_candidates=[candidate],
            confirmations=[confirmation],
            confirmed_roots=[root],
            contributing_conditions=[factor],
            rejected_candidates=[rejected],
            taint_paths=[["record:observed", "record:change", node.ref]],
            visited_order=["record:observed", "record:change", node.ref],
            unresolved_refs=["record:prompt"],
            metadata={"offline_only": True},
        )

        for value, type_ in (
            (defect_state, DefectState),
            (candidate, CausalCandidate),
            (assessment, PredecessorAssessment),
            (judgment, CausalStepJudgment),
            (sample_frontier_item(), FrontierItem),
            (evidence, HypothesisEvidence),
            (hypothesis, AttributionHypothesis),
            (confirmation, RootConfirmation),
            (root, ConfirmedRoot),
            (factor, CausalFactor),
            (rejected, RejectedCandidate),
            (report, RecursiveAttributionReport),
        ):
            self.assertEqual(type_.from_dict(value.to_dict()), value)

        payload = report.to_dict()
        self.assertEqual(payload["case_id"], "case-1")
        self.assertEqual(payload["objective"], "Find the defect origin.")
        self.assertEqual(report.analysis_outcome, "partial_root_found")
        self.assertEqual(payload["root_causes"], [root.to_legacy_root_cause()])
        self.assertEqual(payload["taint_paths"], [list(path) for path in report.taint_paths])
        self.assertEqual(payload["visited_order"], list(report.visited_order))
        self.assertEqual(payload["unresolved_refs"], list(report.unresolved_refs))
        self.assertEqual(payload["metadata"], report.metadata)

    def test_relation_values_are_exact_and_reject_unknown_values(self):
        self.assertEqual(
            PredecessorAssessment(
                ref="record:prompt", relation="same_defect_propagation"
            ).relation,
            "same_defect_propagation",
        )
        with self.assertRaisesRegex(ValueError, "relation"):
            PredecessorAssessment(ref="record:prompt", relation="caused_by")

    def test_report_outcome_preserves_partial_roots_and_keeps_pure_inconclusive_root_free(self):
        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        )
        partial = RecursiveAttributionReport(
            case_id="partial",
            objective="Find the root.",
            analysis_outcome="inconclusive",
            confirmed_roots=[root],
            unresolved_refs=["record:prompt"],
        )
        self.assertEqual(partial.analysis_outcome, "partial_root_found")
        self.assertEqual(partial.confirmed_roots, (root,))
        self.assertEqual(
            RecursiveAttributionReport.from_dict(partial.to_dict()).analysis_outcome,
            "partial_root_found",
        )

        for metadata in (
            {"unresolved_reason": "provider_unavailable"},
            {"unresolved_reason": "judge_budget_exhausted"},
            {"unresolved_reason": "artifact_missing"},
        ):
            report = RecursiveAttributionReport(
                case_id="unresolved",
                objective="Find the root.",
                analysis_outcome="inconclusive",
                unresolved_refs=["record:change"],
                metadata=metadata,
            )
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertEqual(report.confirmed_roots, ())
            self.assertEqual(report.co_roots, ())

    def test_recursive_state_collections_are_deeply_immutable(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
            data={"nested": {"reason": "stop"}},
            source_refs=["record:prompt"],
        )
        candidate = CausalCandidate(
            ref=node.ref,
            node=node,
            source="confirmed_edge",
            edge={"context": {"confidence": 1.0}},
            evidence_refs=["record:change"],
        )
        assessment = PredecessorAssessment(ref=node.ref, evidence_refs=["record:change"])
        judgment = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="unknown",
            current_defect_reason="artifact missing",
            predecessors=[assessment],
            missing_evidence=["artifact:change"],
            suggested_investigation={"tool": "inspect_artifact"},
        )
        hypothesis = AttributionHypothesis.create("Decision is the root.", node.ref, defect_state).with_updates(
            unresolved_questions=["Was the artifact complete?"],
            counterfactual={"action": "search"},
        )
        confirmation = RootConfirmation.unknown(
            node.ref, "artifact missing", evidence_refs=["artifact:change"]
        )
        factor = CausalFactor(
            node_ref="record:prompt",
            relation="contributing_condition",
            reason="The prompt omitted a hint.",
            evidence_refs=["record:prompt"],
        )
        report = RecursiveAttributionReport(
            case_id="immutable",
            objective="Find the root.",
            causal_candidates=[candidate],
            step_judgments=[judgment],
            hypotheses=[hypothesis],
            confirmations=[confirmation],
            contributing_conditions=[factor],
            taint_paths=[["record:observed", node.ref]],
            metadata={"nested": {"offline_only": True}},
        )
        item = sample_frontier_item()
        before = (candidate.to_dict(), hypothesis.semantic_hash, item.visit_key, report.to_dict())

        for callback in (
            lambda: candidate.evidence_refs.append("record:forged"),
            lambda: candidate.edge.__setitem__("forged", True),
            lambda: candidate.node.data["nested"].__setitem__("forged", True),
            lambda: item.downstream_path.append("record:forged"),
            lambda: judgment.predecessors.append(assessment),
            lambda: judgment.suggested_investigation.__setitem__("forged", True),
            lambda: hypothesis.unresolved_questions.append("forged"),
            lambda: hypothesis.counterfactual.__setitem__("forged", True),
            lambda: confirmation.evidence_refs.append("record:forged"),
            lambda: factor.evidence_refs.append("record:forged"),
            lambda: report.causal_candidates.append(candidate),
            lambda: report.taint_paths[0].append("record:forged"),
            lambda: report.metadata["nested"].__setitem__("forged", True),
        ):
            with self.assertRaises((AttributeError, TypeError)):
                callback()

        self.assertEqual(before, (candidate.to_dict(), hypothesis.semantic_hash, item.visit_key, report.to_dict()))

    def test_from_dict_rejects_forged_persisted_identity(self):
        defect_payload = sample_defect_state().to_dict()
        defect_payload["fingerprint"] = "forged"
        with self.assertRaisesRegex(ValueError, "DefectState fingerprint"):
            DefectState.from_dict(defect_payload)

        hypothesis = AttributionHypothesis.create(
            "Decision is the root.", "record:decision", sample_defect_state()
        )
        hypothesis_payload = hypothesis.to_dict()
        hypothesis_payload["semantic_hash"] = "forged"
        with self.assertRaisesRegex(ValueError, "AttributionHypothesis semantic_hash"):
            AttributionHypothesis.from_dict(hypothesis_payload)

        frontier_payload = sample_frontier_item().to_dict()
        frontier_payload["item_id"] = "frontier:forged"
        with self.assertRaisesRegex(ValueError, "FrontierItem item_id"):
            FrontierItem.from_dict(frontier_payload)
        frontier_payload = sample_frontier_item().to_dict()
        frontier_payload["visit_key"] = "forged"
        with self.assertRaisesRegex(ValueError, "FrontierItem visit_key"):
            FrontierItem.from_dict(frontier_payload)

    def test_legacy_root_cause_projection_matches_existing_candidate_fields(self):
        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The plan closed before searching call sites.",
            counterfactual="Searching call sites would reveal the method.",
            confidence=0.9,
            evidence_refs=["record:decision"],
            component="agent",
            event_type="decision",
            defect_type="premature_repository_search_closure",
            causal_role="defect_introduction",
            episode_id="episode:decision",
            episode_member_refs=["record:decision", "record:change"],
            observed_defect_refs=["record:observed"],
        )
        self.assertEqual(ConfirmedRoot.from_dict(root.to_dict()), root)
        self.assertEqual(
            root.to_legacy_root_cause(),
            {
                "node_ref": "record:decision",
                "component": "agent",
                "event_type": "decision",
                "defect_type": "premature_repository_search_closure",
                "reason": "The plan closed before searching call sites.",
                "confidence": 0.9,
                "causal_role": "defect_introduction",
                "episode_id": "episode:decision",
                "episode_member_refs": ["record:decision", "record:change"],
                "observed_defect_refs": ["record:observed"],
            },
        )
        report = RecursiveAttributionReport(case_id="legacy", objective="Find root.", confirmed_roots=[root])
        self.assertEqual(report.to_dict()["confirmed_roots"], [root.to_dict()])
        self.assertEqual(report.to_dict()["root_causes"], [root.to_legacy_root_cause()])


if __name__ == "__main__":
    unittest.main()
