from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_state import (
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_identity_for,
    confirmation_response_identity_for,
)
from trace_attribution.hypotheses import RecursiveFrontier
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    artifact_state,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix31 import (
    direct_projection_identity,
)
from tools.trace_attribution.tests import test_root_confirmation_fix30


def _refresh_response_identity(confirmation):
    confirmation["response_identity"] = confirmation_response_identity_for(
        confirmation_identity=confirmation["confirmation_identity"],
        status=confirmation["status"],
        excerpt=confirmation["excerpt"],
        reason=confirmation["reason"],
        counterfactual=confirmation["counterfactual"],
        confidence=confirmation["confidence"],
        evidence_refs=tuple(confirmation["evidence_refs"]),
        counterfactual_status=confirmation["counterfactual_status"],
        factor_role=confirmation["factor_role"],
        competitor_comparisons=tuple(
            confirmation["competitor_comparisons"]
        ),
        factor_mechanism=confirmation["factor_mechanism"],
    )
    return confirmation


def _replacement_confirmation(value, semantic_hash):
    confirmation = copy.deepcopy(dict(value))
    confirmation["hypothesis_semantic_hash"] = semantic_hash
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=semantic_hash,
        candidate_ref=confirmation["candidate_ref"],
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=tuple(confirmation["recursive_path"]),
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    return _refresh_response_identity(confirmation)


def _rewrite_request_projection(entry, semantic_hash):
    projection = copy.deepcopy(entry["factual_request_projection"])
    projection["facts"]["hypothesis_semantic_hash"] = semantic_hash
    request_identity = direct_projection_identity(projection)
    entry["factual_request_projection"] = projection
    if "hypothesis_semantic_hash" in entry:
        entry["hypothesis_semantic_hash"] = semantic_hash
    if "semantic_identity" in entry:
        entry["semantic_identity"] = request_identity
    if "request_identity" in entry:
        entry["request_identity"] = request_identity
    if "semantic_key" in entry:
        entry["semantic_key"] = "confirmation:{0}".format(request_identity)
    return request_identity


def _rewrite_terminal_entry(entry, semantic_hash):
    request_identity = _rewrite_request_projection(entry, semantic_hash)
    confirmation = _replacement_confirmation(
        entry["confirmation"],
        semantic_hash,
    )
    entry["confirmation"] = confirmation
    if "response_identity" in entry:
        entry["response_identity"] = confirmation["response_identity"]
    return request_identity, confirmation


def _rewrite_persisted_confirmation_container(container, semantic_hash):
    queue = container["confirmation_queue"][0]
    request_identity, confirmation = _rewrite_terminal_entry(
        queue,
        semantic_hash,
    )
    journal = container["confirmation_journal"][0]
    _rewrite_terminal_entry(journal, semantic_hash)
    action = container["confirmation_action_projection"][0]
    _rewrite_terminal_entry(action, semantic_hash)
    for item in container.get("confirmations") or ():
        item.clear()
        item.update(copy.deepcopy(confirmation))
    for seed in container.get("seed_ledger") or container.get(
        "seed_results"
    ) or ():
        identities = seed.get("confirmation_identities") or ()
        if identities:
            seed["confirmation_identities"] = [
                confirmation["confirmation_identity"]
            ]
    return request_identity, confirmation


def _rewrite_terminal_action_record(action, semantic_hash):
    projection = action["payload"]["action_projection"]
    request_identity, confirmation = _rewrite_terminal_entry(
        projection,
        semantic_hash,
    )
    action["semantic_key"] = "confirmation:{0}".format(request_identity)
    action["payload"]["confirmation"] = copy.deepcopy(confirmation)
    return confirmation


def _drift_action_confirmation_path(action):
    projection = action["payload"]["action_projection"]
    confirmation = copy.deepcopy(projection["confirmation"])
    confirmation["recursive_path"].append(
        confirmation["recursive_path"][-1]
    )
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=confirmation["candidate_ref"],
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=tuple(confirmation["recursive_path"]),
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    _refresh_response_identity(confirmation)
    projection["recursive_path"] = copy.deepcopy(
        confirmation["recursive_path"]
    )
    projection["response_identity"] = confirmation["response_identity"]
    projection["confirmation"] = copy.deepcopy(confirmation)
    action["payload"]["confirmation"] = copy.deepcopy(confirmation)


def _completed_action(root):
    _, state, _, queued = artifact_state(root)
    if not state.enqueue_confirmation(queued):
        raise AssertionError("failed to enqueue the completed replay fixture")
    analyzer = test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
        judge=test_root_confirmation_fix30.CountingArtifactConfirmationJudge()
    )
    analyzer._confirm_queued_roots(state)
    action = next(
        copy.deepcopy(item)
        for item in analyzer.captured_actions
        if item["operation"] == "confirmation_completed"
    )
    return action


class HypothesisAuthorityBindingTest(unittest.TestCase):
    def rejected_fixture(self, name):
        fixture = (
            test_root_confirmation_fix30.RejectedSnapshotTerminalDispositionTest(
                "run"
            )
        )
        result = fixture.run_analysis("content_drift", name=name)
        self.addCleanup(fixture.doCleanups)
        return result

    def test_coordinated_rejected_hash_rewrite_fails_report_and_evaluator(self):
        _, graph, _, _, checkpoint, report, _, _ = self.rejected_fixture(
            "fix33-rejected-report"
        )
        payload = report.to_dict()
        _rewrite_persisted_confirmation_container(
            {
                **payload["metadata"],
                "confirmations": payload["confirmations"],
                "seed_results": payload["seed_results"],
            },
            "fix33-coordinated-rejected-hash",
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        terminal = next(
            item
            for item in actions
            if item.get("operation") == "confirmation_failed"
        )
        _rewrite_terminal_action_record(
            terminal,
            "fix33-coordinated-rejected-hash",
        )
        restored = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "hypothesis.*authority|ledger|frontier",
        ):
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="fix33 coordinated rejected hash",
                action_records=actions,
            )
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

    def test_coordinated_rejected_hash_rewrite_fails_checkpoint_restore(self):
        _, graph, _, _, checkpoint, _, _, _ = self.rejected_fixture(
            "fix33-rejected-checkpoint"
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        snapshot = latest_snapshot(actions)
        request_identity, confirmation = (
            _rewrite_persisted_confirmation_container(
                snapshot,
                "fix33-coordinated-checkpoint-hash",
            )
        )
        terminal = next(
            item
            for item in actions
            if item.get("operation") == "confirmation_failed"
        )
        _rewrite_terminal_action_record(
            terminal,
            "fix33-coordinated-checkpoint-hash",
        )
        self.assertEqual(
            terminal["semantic_key"],
            "confirmation:{0}".format(request_identity),
        )
        self.assertEqual(
            terminal["payload"]["confirmation"],
            confirmation,
        )

        with self.assertRaisesRegex(
            ValueError,
            "hypothesis.*authority|ledger|frontier",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_rejected_replay_hash_rewrite_is_provider_and_rebuild_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, failed_state, _, failed_queue = artifact_state(root)
            self.assertTrue(failed_state.enqueue_confirmation(failed_queue))
            (root / "artifacts" / "proof.txt").write_bytes(
                b"drifted proof"
            )
            first = (
                test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
                judge=(
                    test_root_confirmation_fix30
                    .CountingArtifactConfirmationJudge()
                )
                )
            )
            first._confirm_queued_roots(failed_state)
            failed_action = next(
                copy.deepcopy(item)
                for item in first.captured_actions
                if item["operation"] == "confirmation_failed"
            )
            semantic_hash = "fix33-coordinated-replay-hash"
            _rewrite_terminal_action_record(failed_action, semantic_hash)

            _, replay_state, _, replay_queue = artifact_state(root)
            self.assertTrue(replay_state.enqueue_confirmation(replay_queue))
            stored = replay_state.confirmation_queue[0]
            _rewrite_request_projection(stored, semantic_hash)
            replay_state.replay_actions = {
                failed_action["semantic_key"]: failed_action
            }
            judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            replay = test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
                judge=judge,
                forbid_request_rebuild=True,
            )

            with self.assertRaisesRegex(
                ValueError,
                "hypothesis.*authority|ledger|frontier",
            ):
                replay._confirm_queued_roots(replay_state)

        self.assertEqual(judge.confirmation_calls, 0)
        self.assertEqual(replay.captured_actions, [])
        self.assertEqual(replay_state.provider_state, {})

    def test_pending_and_validated_terminal_hash_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pending_state, _, pending = artifact_state(root)
            self.assertTrue(pending_state.enqueue_confirmation(pending))
            _rewrite_request_projection(
                pending_state.confirmation_queue[0],
                "fix33-pending-hash-drift",
            )
            with self.assertRaisesRegex(
                ValueError,
                "hypothesis.*authority|ledger|frontier|projection.*changed|cross-bound",
            ):
                pending_state.validate_confirmation_queue_bound()

            _, terminal_state, _, terminal = artifact_state(root)
            self.assertTrue(terminal_state.enqueue_confirmation(terminal))
            stored = terminal_state.confirmation_queue[0]
            semantic_hash = "fix33-validated-terminal-hash-drift"
            _rewrite_request_projection(stored, semantic_hash)
            confirmation = RootConfirmation(
                candidate_ref=stored["candidate_ref"],
                status="unknown",
                reason="validated terminal remains unknown",
                counterfactual="The counterfactual outcome remains unknown.",
                counterfactual_status="unknown",
                hypothesis_id=stored["hypothesis_id"],
                hypothesis_semantic_hash=semantic_hash,
                defect_fingerprint=stored["defect_fingerprint"],
                recursive_path=tuple(stored["recursive_path"]),
                seed_binding_identity=stored["seed_binding_identity"],
            )
            analyzer = (
                test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
                    judge=(
                        test_root_confirmation_fix30
                        .CountingArtifactConfirmationJudge()
                    )
                )
            )
            with self.assertRaisesRegex(
                ValueError,
                "hypothesis.*authority|ledger|frontier",
            ):
                analyzer._persist_confirmation_action(
                    terminal_state,
                    stored,
                    confirmation,
                    operation="confirmation_completed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                )

    def test_owner_without_frontier_item_binds_to_ledger_hypothesis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, state, _, queued = artifact_state(root)
            state.frontier = RecursiveFrontier()
            self.assertTrue(state.enqueue_confirmation(queued))
            state.validate_confirmation_queue_bound()
            stored = state.confirmation_queue[0]
            valid_confirmation = RootConfirmation(
                candidate_ref=stored["candidate_ref"],
                status="unknown",
                reason="valid no-frontier terminal",
                counterfactual="The counterfactual outcome remains unknown.",
                counterfactual_status="unknown",
                hypothesis_id=stored["hypothesis_id"],
                hypothesis_semantic_hash=stored[
                    "hypothesis_semantic_hash"
                ],
                defect_fingerprint=stored["defect_fingerprint"],
                recursive_path=tuple(stored["recursive_path"]),
                seed_binding_identity=stored["seed_binding_identity"],
            )
            analyzer = (
                test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
                    judge=(
                        test_root_confirmation_fix30
                        .CountingArtifactConfirmationJudge()
                    )
                )
            )
            analyzer._persist_confirmation_action(
                state,
                stored,
                valid_confirmation,
                operation="confirmation_completed",
                physical_requests_reserved=0,
                physical_request_delta=0,
                physical_request_exact=True,
            )

            _, drifted_state, _, drifted_queue = artifact_state(root)
            drifted_state.frontier = RecursiveFrontier()
            self.assertTrue(
                drifted_state.enqueue_confirmation(drifted_queue)
            )
            stored = drifted_state.confirmation_queue[0]
            semantic_hash = "fix33-no-frontier-hash-drift"
            _rewrite_request_projection(
                stored,
                semantic_hash,
            )
            drifted_confirmation = RootConfirmation(
                candidate_ref=stored["candidate_ref"],
                status="unknown",
                reason="drifted no-frontier terminal",
                counterfactual="The counterfactual outcome remains unknown.",
                counterfactual_status="unknown",
                hypothesis_id=stored["hypothesis_id"],
                hypothesis_semantic_hash=semantic_hash,
                defect_fingerprint=stored["defect_fingerprint"],
                recursive_path=tuple(stored["recursive_path"]),
                seed_binding_identity=stored["seed_binding_identity"],
            )
            with self.assertRaisesRegex(
                ValueError,
                "hypothesis.*authority|ledger",
            ):
                analyzer._persist_confirmation_action(
                    drifted_state,
                    stored,
                    drifted_confirmation,
                    operation="confirmation_completed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                )


class CompletedReplayValidationOrderTest(unittest.TestCase):
    def test_invalid_completed_replay_does_not_apply_provider_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            action = _completed_action(root)
            _drift_action_confirmation_path(action)

            _, state, _, queued = artifact_state(root)
            self.assertTrue(state.enqueue_confirmation(queued))
            state.replay_actions = {action["semantic_key"]: action}
            judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            analyzer = (
                test_root_confirmation_fix30.CapturingConfirmationAnalyzer(
                    judge=judge
                )
            )
            provider_state_before = copy.deepcopy(state.provider_state)
            accounting_before = (
                state.judge_requests,
                state.judge_request_uncertainty_count,
                state.logical_judge_calls,
                state.logical_confirmation_calls,
            )

            with self.assertRaisesRegex(
                ValueError,
                "terminal|projection|binding",
            ):
                analyzer._confirm_queued_roots(state)

        self.assertEqual(state.provider_state, provider_state_before)
        self.assertEqual(
            (
                state.judge_requests,
                state.judge_request_uncertainty_count,
                state.logical_judge_calls,
                state.logical_confirmation_calls,
            ),
            accounting_before,
        )
        self.assertEqual(judge.confirmation_calls, 0)
        self.assertEqual(state.confirmations, [])

    def test_valid_pending_validated_rejected_and_completed_replay_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pending_state, _, pending = artifact_state(root)
            self.assertTrue(pending_state.enqueue_confirmation(pending))
            pending_state.validate_confirmation_queue_bound()

            completed = _completed_action(root)
            _, replay_state, _, replay_queue = artifact_state(root)
            completed_accounting = completed["payload"]["provider_state"][
                "accounting"
            ]
            for field in (
                "logical_judge_calls",
                "logical_confirmation_calls",
                "investigation_rounds",
                "artifact_bytes",
            ):
                setattr(replay_state, field, completed_accounting[field])
            self.assertTrue(replay_state.enqueue_confirmation(replay_queue))
            replay_state.replay_actions = {
                completed["semantic_key"]: completed
            }
            replay_judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            AgenticRecursiveAnalyzer(
                judge=replay_judge
            )._confirm_queued_roots(replay_state)

            fixture = (
                test_root_confirmation_fix30
                .RejectedSnapshotTerminalDispositionTest("run")
            )
            (
                _,
                graph,
                config,
                checkpoint_root,
                _,
                rejected_report,
                rejected_judge,
                _,
            ) = fixture.run_analysis(
                "content_drift",
                name="fix33-valid-rejected",
            )
            self.addCleanup(fixture.doCleanups)
            rejected_replay_judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            rejected_replay = AgenticRecursiveAnalyzer(
                judge=rejected_replay_judge,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=("record:candidate",),
                objective="Confirm each explicitly owned artifact candidate.",
                analysis_perspective="",
            )

        self.assertEqual(pending_state.confirmation_queue[0]["status"], "queued")
        self.assertEqual(replay_judge.confirmation_calls, 0)
        self.assertEqual(len(replay_state.confirmations), 1)
        self.assertEqual(
            replay_state.provider_state,
            completed["payload"]["provider_state"],
        )
        self.assertEqual(rejected_judge.confirmation_calls, 0)
        self.assertEqual(
            rejected_report.seed_results[0].outcome,
            "evidence_gap",
        )
        self.assertEqual(
            rejected_replay.to_dict(),
            rejected_report.to_dict(),
        )
        self.assertEqual(rejected_replay_judge.confirmation_calls, 0)


if __name__ == "__main__":
    unittest.main()
