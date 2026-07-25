from __future__ import annotations

import copy
import hashlib
import unittest

from trace_attribution import causal_judge
from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    REPORT_SCHEMA_VERSION as EVALUATOR_REPORT_SCHEMA_VERSION,
    compare_report,
)
from trace_attribution.causal_state import (
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    RecursiveAttributionReport,
    annotate_report_semantic_anchors,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointBundle,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _classify_global_failure_episodes,
    _classify_global_pass_records,
    _quarantine_stale_seed_report_payload,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    stale_seed_trace,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    SelectiveGlobalJudge,
    checkpoint_for,
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix30 import (
    CountingArtifactConfirmationJudge,
    marker_stripped,
    seed_authority,
)
from tools.trace_attribution.tests import test_root_confirmation_fix30
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_trace,
)


def remove_residual_authority_field(record, label):
    value = marker_stripped(record)
    value.pop(
        "defect_state_id" if label == "episode" else "defect_fingerprint",
        None,
    )
    return value


class PartialLedgerAuthorityGlobalTerminalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.tempdir,
            _,
            _,
            cls.checkpoint,
            cls.report,
        ) = checkpoint_for(
            shared_root_trace(),
            SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
            name="fix31-partial-ledger-authority",
        )
        cls.graph = TraceGraph.from_trace(shared_root_trace())
        cls.payload = cls.report.to_dict()
        cls.authority = seed_authority(cls.payload["seed_results"])

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    @staticmethod
    def terminal_records(container):
        completed = next(
            item
            for item in container["investigation_journal"]
            if item.get("status") == "completed"
        )
        failed = next(
            item
            for item in container["investigation_journal"]
            if item.get("status") == "failed"
        )
        episode = next(
            item
            for item in container["unresolved_branches"]
            if item.get("global_pass_identity")
        )
        return completed, failed, episode

    def mutated_terminal(self, container, label):
        completed, failed, episode = self.terminal_records(container)
        return remove_residual_authority_field(
            {
                "completed": completed,
                "failed": failed,
                "episode": episode,
            }[label],
            label,
        )

    def replace_terminal(self, container, label):
        target = {
            "completed": next(
                item
                for item in container["investigation_journal"]
                if item.get("status") == "completed"
            ),
            "failed": next(
                item
                for item in container["investigation_journal"]
                if item.get("status") == "failed"
            ),
            "episode": next(
                item
                for item in container["unresolved_branches"]
                if item.get("global_pass_identity")
            ),
        }[label]
        mutated = remove_residual_authority_field(target, label)
        target.clear()
        target.update(mutated)

    def test_classifier_rejects_one_authority_field_residual_terminals(self):
        snapshot = latest_snapshot(list(self.checkpoint.actions))
        for label, classifier in (
            ("completed", _classify_global_pass_records),
            ("failed", _classify_global_pass_records),
            ("episode", _classify_global_failure_episodes),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    classifier(
                        (self.mutated_terminal(snapshot, label),),
                        seed_authority=self.authority,
                    )

    def test_live_terminal_derivation_rejects_partial_passes(self):
        for label in ("completed", "failed"):
            with self.subTest(label=label):
                event = remove_residual_authority_field(
                    next(
                        item
                        for item in self.payload["investigation_journal"]
                        if item.get("status") == label
                    ),
                    label,
                )
                state = RecursiveAnalysisState.create(
                    graph=self.graph,
                    start_refs=(event["seed_ref"],),
                    objective=OBJECTIVE,
                    analysis_perspective="",
                )
                state.investigation_journal.append(event)
                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    AgenticRecursiveAnalyzer(
                        judge=SharedRootFusionJudge(),
                        fusion_mode="retrieval-global",
                    )._run_global_candidate_prepass(state, self.graph)

    def test_checkpoint_rejects_partial_terminal_shapes(self):
        for label in ("completed", "failed", "episode"):
            with self.subTest(label=label):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                self.replace_terminal(latest_snapshot(actions), label)
                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=self.graph,
                        checkpoint=partial_checkpoint(
                            self.checkpoint, actions
                        ),
                    )

    def test_report_evaluator_and_stale_quarantine_reject_partial_terminals(self):
        stale_graph = TraceGraph.from_trace(
            stale_seed_trace(shared_root_trace())
        )
        for label in ("completed", "failed", "episode"):
            with self.subTest(label=label):
                payload = copy.deepcopy(self.payload)
                container = {
                    "investigation_journal": payload[
                        "investigation_journal"
                    ],
                    "unresolved_branches": payload["metadata"][
                        "unresolved_branches"
                    ],
                }
                self.replace_terminal(container, label)
                try:
                    restored = RecursiveAttributionReport.from_dict(payload)
                except ValueError as exc:
                    self.assertRegex(
                        str(exc),
                        "global|residual|terminal|owner|schema",
                    )
                else:
                    with self.assertRaisesRegex(
                        ValueError,
                        "global|residual|terminal|owner|schema",
                    ):
                        validate_recursive_report_against_graph(
                            self.graph,
                            restored,
                            label="fix31 partial terminal report",
                            action_records=self.checkpoint.actions,
                        )
                with self.assertRaises(EvaluationSafetyError):
                    compare_report(
                        payload,
                        permissive_labels(self.graph.case_id),
                        None,
                        graph=self.graph,
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    _quarantine_stale_seed_report_payload(
                        stale_graph, payload
                    )

    def test_known_seed_controls_require_distinctive_terminal_structure(self):
        facts = next(iter(self.authority.values()))
        ordinary_investigation = {
            "seed_ref": facts["seed_ref"],
            "status": "completed",
            "directive_kind": "evidence_investigation",
            "result": {"status": "completed"},
        }
        ordinary_unresolved = {
            "node_ref": facts["seed_ref"],
            "hypothesis_id": "hypothesis:ordinary",
            "reason": "ordinary_evidence_gap",
            "details": "Evidence remains incomplete.",
            "depth": 1,
        }
        self.assertEqual(
            _classify_global_pass_records(
                (ordinary_investigation,),
                seed_authority=self.authority,
            ),
            ([], []),
        )
        self.assertEqual(
            _classify_global_failure_episodes(
                (ordinary_unresolved,),
                seed_authority=self.authority,
            ),
            [],
        )


def tamper_projection(projection):
    projection["facts"]["analysis_perspective"] = (
        str(projection["facts"].get("analysis_perspective") or "")
        + " tampered"
    )


def direct_projection_identity(projection):
    return "confirmation_request:v3:{0}".format(
        hashlib.sha256(
            stable_json(projection).encode("utf-8")
        ).hexdigest()
    )


class RejectedProjectionIdentityBindingTest(
    test_root_confirmation_fix30.RejectedSnapshotTerminalDispositionTest
):
    def rejected_fixture(self, name):
        return self.run_analysis("content_drift", name=name)

    def assert_report_rejected(self, graph, checkpoint, payload):
        try:
            report = RecursiveAttributionReport.from_dict(payload)
        except ValueError:
            return
        with self.assertRaisesRegex(
            ValueError,
            "projection|identity|biject|canonical",
        ):
            validate_recursive_report_against_graph(
                graph,
                report,
                label="fix31 rejected projection report",
                action_records=checkpoint.actions,
            )

    def test_rejected_queue_projection_tampering_is_rejected_on_restore(self):
        _, graph, _, _, checkpoint, _, _, _ = self.rejected_fixture(
            "fix31-queue-tamper"
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        queue = latest_snapshot(actions)["confirmation_queue"][0]
        tamper_projection(queue["factual_request_projection"])

        with self.assertRaisesRegex(
            ValueError,
            "projection|identity|canonical",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_rejected_report_representations_reject_single_projection_drift(self):
        _, graph, _, _, checkpoint, report, _, _ = self.rejected_fixture(
            "fix31-report-tamper"
        )
        for location in (
            ("confirmation_queue", "factual_request_projection"),
            ("confirmation_journal", "factual_request_projection"),
            (
                "confirmation_action_projection",
                "factual_request_projection",
            ),
        ):
            with self.subTest(location=location[0]):
                payload = report.to_dict()
                item = payload["metadata"][location[0]][0]
                self.assertIn(location[1], item)
                tamper_projection(item[location[1]])
                self.assert_report_rejected(graph, checkpoint, payload)
                with self.assertRaises(EvaluationSafetyError):
                    compare_report(
                        annotate_report_semantic_anchors(
                            graph.case_id,
                            graph.nodes,
                            payload,
                            graph=graph,
                        ),
                        permissive_labels(graph.case_id),
                        None,
                        graph=graph,
                    )

    def test_rejected_checkpoint_and_terminal_action_reject_projection_drift(self):
        _, graph, _, _, checkpoint, _, _, _ = self.rejected_fixture(
            "fix31-checkpoint-tamper"
        )
        for location in (
            "confirmation_journal",
            "confirmation_action_projection",
        ):
            with self.subTest(location=location):
                actions = copy.deepcopy(list(checkpoint.actions))
                item = latest_snapshot(actions)[location][0]
                self.assertIn("factual_request_projection", item)
                tamper_projection(item["factual_request_projection"])
                with self.assertRaisesRegex(
                    ValueError,
                    "projection|identity|bijection|canonical",
                ):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=partial_checkpoint(checkpoint, actions),
                    )

        actions = copy.deepcopy(list(checkpoint.actions))
        terminal = next(
            item
            for item in actions
            if item.get("operation") == "confirmation_failed"
        )
        self.assertIn(
            "factual_request_projection",
            terminal["payload"]["action_projection"],
        )
        tamper_projection(
            terminal["payload"]["action_projection"][
                "factual_request_projection"
            ]
        )
        with self.assertRaisesRegex(
            ValueError,
            "projection|identity|canonical",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_coordinated_rejected_queue_projection_requires_correct_identity(self):
        _, graph, _, _, checkpoint, _, _, _ = self.rejected_fixture(
            "fix31-coordinated-tamper"
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        queue = latest_snapshot(actions)["confirmation_queue"][0]
        tamper_projection(queue["factual_request_projection"])
        queue["semantic_identity"] = direct_projection_identity(
            queue["factual_request_projection"]
        )
        with self.assertRaisesRegex(
            ValueError,
            "projection|identity|biject|canonical",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_valid_rejected_snapshot_round_trip_is_provider_free_and_seed_local(self):
        (
            _,
            graph,
            config,
            checkpoint_root,
            checkpoint,
            report,
            judge,
            _,
        ) = self.rejected_fixture("fix31-valid-rejected")
        payload = report.to_dict()
        queue = payload["metadata"]["confirmation_queue"][0]
        journal = payload["metadata"]["confirmation_journal"][0]
        action = payload["metadata"]["confirmation_action_projection"][0]
        helper = getattr(
            causal_judge,
            "root_confirmation_request_projection_identity",
            None,
        )
        self.assertTrue(callable(helper))
        self.assertEqual(
            queue["semantic_identity"],
            helper(queue["factual_request_projection"]),
        )
        self.assertEqual(
            queue["factual_request_projection"],
            journal["factual_request_projection"],
        )
        self.assertEqual(
            queue["factual_request_projection"],
            action["factual_request_projection"],
        )
        self.assertEqual(judge.confirmation_calls, 0)
        self.assertEqual(report.seed_results[0].outcome, "evidence_gap")

        replay_judge = CountingArtifactConfirmationJudge()
        replayed = AgenticRecursiveAnalyzer(
            judge=replay_judge,
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        ).analyze(
            graph,
            start_refs=("record:candidate",),
            objective="Confirm each explicitly owned artifact candidate.",
            analysis_perspective="",
        )
        self.assertEqual(replayed.to_dict(), payload)
        self.assertEqual(replay_judge.step_calls, 0)
        self.assertEqual(replay_judge.confirmation_calls, 0)
        self.assertEqual(
            len(
                [
                    item
                    for item in checkpoint.actions
                    if item.get("operation") == "confirmation_failed"
                ]
            ),
            1,
        )

    def test_validated_terminal_projection_round_trip_remains_accepted(self):
        (
            _,
            graph,
            _,
            _,
            checkpoint,
            report,
            judge,
            _,
        ) = self.run_analysis(
            "owner_substitution",
            record_ids=("candidate", "candidate_two"),
            starts=("record:candidate", "record:candidate_two"),
            name="fix31-valid-ordinary",
        )
        payload = report.to_dict()
        validated = next(
            item
            for item in payload["metadata"][
                "confirmation_action_projection"
            ]
            if item["evidence_disposition"]["state"] == "validated"
        )
        queue = next(
            item
            for item in payload["metadata"]["confirmation_queue"]
            if item["candidate_ref"] == validated["candidate_ref"]
        )
        self.assertIn("factual_request_projection", validated)
        self.assertEqual(
            validated["factual_request_projection"],
            queue["factual_request_projection"],
        )
        self.assertEqual(judge.confirmation_calls, 1)
        restored = RecursiveAttributionReport.from_dict(payload)
        validate_recursive_report_against_graph(
            graph,
            restored,
            label="fix31 valid ordinary terminal",
            action_records=checkpoint.actions,
        )


class Fix31PersistenceVersionTest(unittest.TestCase):
    def test_versions_name_projection_identity_binding_contracts(self):
        self.assertEqual(
            causal_judge.ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA,
            "root-confirmation-request-projection/v2",
        )
        self.assertEqual(
            causal_judge.ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX,
            "confirmation_request:v3:",
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v18",
        )
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v20",
        )
        self.assertEqual(
            EVALUATOR_REPORT_SCHEMA_VERSION,
            MODERN_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v20",
        )
        self.assertIn(
            "recursive-root-confirmation/v17",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "action-projection/v7",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "response-identity/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "confirmation-request-projection/v2",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
