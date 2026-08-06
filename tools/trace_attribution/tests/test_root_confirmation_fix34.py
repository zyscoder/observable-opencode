from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_state import RecursiveAttributionReport
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests import (
    test_root_confirmation_fix30,
    test_root_confirmation_fix33,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    artifact_state,
    artifact_trace,
)


def _resign_provider_state(provider):
    unsigned = {
        key: provider[key]
        for key in provider
        if key != "identity"
    }
    provider["identity"] = hashlib.sha256(
        stable_json(unsigned).encode("utf-8")
    ).hexdigest()


def _configure_nonzero_completed_action(action, state, *, delta=3):
    payload = action["payload"]
    projection = payload["action_projection"]
    payload["physical_requests_reserved"] = 0
    payload["physical_request_delta"] = delta
    payload["physical_request_exact"] = True
    projection["physical_requests_reserved"] = 0
    projection["physical_request_delta"] = delta
    projection["physical_request_exact"] = True
    provider = payload["provider_state"]
    provider["circuit"] = {
        "open": True,
        "reason": "persisted replay circuit",
        "consecutive_provider_errors": 4,
        "provider_error_threshold": 9,
        "disposition": {
            "retryable": True,
            "category": "http_retryable",
            "status_code": 503,
            "error_code": "service_unavailable",
            "reason": "persisted replay circuit",
        },
        "first_request": 1,
        "first_failure_at": "2026-07-31T00:00:00Z",
    }
    provider["accounting"] = {
        "judge_requests": state.judge_requests + delta,
        "judge_request_uncertainty_count": (
            state.judge_request_uncertainty_count
        ),
        "logical_judge_calls": state.logical_judge_calls,
        "logical_confirmation_calls": state.logical_confirmation_calls,
        "investigation_rounds": state.investigation_rounds,
        "artifact_bytes": state.artifact_bytes,
    }
    _resign_provider_state(provider)
    return provider


def _set_nonzero_state(state):
    state.judge_requests = 5
    state.judge_request_uncertainty_count = 2
    state.logical_judge_calls = 7
    state.logical_confirmation_calls = 4
    state.investigation_rounds = 3
    state.artifact_bytes = 11
    state.provider_state = {"sentinel": "unchanged"}


def _set_initial_circuit(judge):
    judge.provider_circuit_open = False
    judge.provider_circuit_reason = "before replay"
    judge.consecutive_provider_errors = 2
    judge.provider_error_threshold = 7


def _state_snapshot(state, judge):
    return {
        "frontier": copy.deepcopy(state.frontier_checkpoint_payload()),
        "hypotheses": copy.deepcopy(
            state.hypothesis_checkpoint_payload()
        ),
        "actions": copy.deepcopy(state.action_checkpoint_payload()),
        "circuit": (
            judge.provider_circuit_open,
            judge.provider_circuit_reason,
            judge.consecutive_provider_errors,
            judge.provider_error_threshold,
        ),
    }


def _replay_fixture(root):
    action = test_root_confirmation_fix33._completed_action(root)
    _, state, item, queued = artifact_state(root)
    state.seed_ledger[
        item.seed_binding_identity
    ].candidate_refs.add(item.node_ref)
    if not state.enqueue_confirmation(queued):
        raise AssertionError("failed to enqueue Fix34 replay fixture")
    _set_nonzero_state(state)
    _configure_nonzero_completed_action(action, state)
    state.replay_actions = {action["semantic_key"]: action}
    judge = (
        test_root_confirmation_fix30.CountingArtifactConfirmationJudge()
    )
    _set_initial_circuit(judge)
    analyzer = AgenticRecursiveAnalyzer(judge=judge)
    return action, state, judge, analyzer


class CompletedReplayAtomicityTest(unittest.TestCase):
    def test_invalid_provider_payload_fails_before_any_state_mutation(self):
        mutations = {
            "threshold": lambda provider: provider["circuit"].__setitem__(
                "provider_error_threshold", 0
            ),
            "cache": lambda provider: provider.__setitem__(
                "cache_identity", "cache:wrong"
            ),
            "accounting": lambda provider: provider[
                "accounting"
            ].__setitem__("judge_requests", -1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                action, state, judge, analyzer = _replay_fixture(
                    Path(directory)
                )
                mutate(action["payload"]["provider_state"])
                _resign_provider_state(
                    action["payload"]["provider_state"]
                )
                before = _state_snapshot(state, judge)

                with self.assertRaisesRegex(
                    ValueError,
                    "provider|cache|accounting|threshold|counter",
                ):
                    analyzer._confirm_queued_roots(state)

                self.assertEqual(_state_snapshot(state, judge), before)
                self.assertEqual(judge.confirmation_calls, 0)

    def test_invalid_terminal_binding_with_nonzero_delta_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            action, state, judge, analyzer = _replay_fixture(
                Path(directory)
            )
            test_root_confirmation_fix33._drift_action_confirmation_path(
                action
            )
            before = _state_snapshot(state, judge)

            with self.assertRaisesRegex(
                ValueError,
                "terminal|projection|binding",
            ):
                analyzer._confirm_queued_roots(state)

        self.assertEqual(_state_snapshot(state, judge), before)
        self.assertEqual(judge.confirmation_calls, 0)

    def test_valid_nonzero_completed_replay_applies_exact_state_once(self):
        with tempfile.TemporaryDirectory() as directory:
            action, state, judge, analyzer = _replay_fixture(
                Path(directory)
            )
            provider = copy.deepcopy(
                action["payload"]["provider_state"]
            )

            analyzer._confirm_queued_roots(state)
            after_first = _state_snapshot(state, judge)
            analyzer._confirm_queued_roots(state)

        self.assertEqual(state.judge_requests, 8)
        self.assertEqual(state.judge_request_uncertainty_count, 2)
        self.assertEqual(state.logical_judge_calls, 7)
        self.assertEqual(state.logical_confirmation_calls, 4)
        self.assertEqual(state.investigation_rounds, 3)
        self.assertEqual(state.artifact_bytes, 11)
        self.assertEqual(state.provider_state, provider)
        self.assertEqual(
            (
                judge.provider_circuit_open,
                judge.provider_circuit_reason,
                judge.consecutive_provider_errors,
                judge.provider_error_threshold,
            ),
            (True, "persisted replay circuit", 4, 9),
        )
        self.assertEqual(len(state.confirmations), 1)
        self.assertEqual(_state_snapshot(state, judge), after_first)
        self.assertEqual(judge.confirmation_calls, 0)


class PendingAndRejectedControlsTest(unittest.TestCase):
    def test_pending_confirmation_survives_checkpoint_and_report_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph, state, item, queued = artifact_state(root)
            state.seed_ledger[
                item.seed_binding_identity
            ].candidate_refs.add(item.node_ref)
            state.processed_items = 1
            self.assertTrue(state.enqueue_confirmation(queued))
            trace = artifact_trace()
            content = b"verified proof"
            trace["artifacts"][0]["hash"] = "sha256:{0}".format(
                hashlib.sha256(content).hexdigest()
            )
            trace["artifacts"][0]["byte_length"] = len(content)
            config = build_checkpoint_config(
                trace=trace,
                case_id=graph.case_id,
                objective=state.objective,
                analysis_perspective=state.analysis_perspective,
                start_refs=list(state.start_refs),
                budgets={
                    "max_frontier_items": 96,
                    "max_depth": 20,
                    "max_hypotheses": 24,
                    "max_investigation_rounds": 12,
                    "max_artifact_bytes": 1_048_576,
                    "max_judge_requests": 128,
                },
                model_identity="offline:fix34-pending",
                cache_identity="cache:fix34-pending",
                runtime_identity={
                    "judge_timeout_sec": 3600.0,
                    "judge_max_tokens": 4096,
                    "thinking_mode": "disabled",
                    "base_url": "offline://fix34-pending",
                    "provider_error_threshold": 3,
                },
            )
            checkpoint_root = root / "checkpoint"
            checkpoint = CheckpointBundle(checkpoint_root)
            checkpoint.initialize(config)
            judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            analyzer = AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=checkpoint,
                checkpoint_config=config,
            )
            analyzer._checkpoint_state(state, "fix34:pending")

            restored_checkpoint = CheckpointBundle(
                checkpoint_root
            ).restore(expected_config=config)
            restored_state = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=restored_checkpoint,
            )
            report_path = root / "pending-report.json"
            report_path.write_text(
                json.dumps(
                    restored_state.build_report(judge=judge).to_dict(),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            restored_report = RecursiveAttributionReport.from_dict(
                json.loads(report_path.read_text(encoding="utf-8"))
            )
            validate_recursive_report_against_graph(
                graph,
                restored_report,
                label="Fix34 pending JSON report",
                action_records=restored_checkpoint.actions,
            )

        self.assertEqual(
            restored_state.confirmation_queue[0]["status"],
            "queued",
        )
        self.assertEqual(
            restored_report.metadata["confirmation_queue"][0]["status"],
            "queued",
        )

    def test_rejected_replay_remains_provider_free(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, failed_state, _, failed_queue = artifact_state(root)
            self.assertTrue(
                failed_state.enqueue_confirmation(failed_queue)
            )
            (root / "artifacts" / "proof.txt").write_bytes(
                b"drifted proof"
            )
            first = (
                test_root_confirmation_fix30
                .CapturingConfirmationAnalyzer(
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
            self.assertNotIn(
                "provider_state",
                failed_action["payload"],
            )

            _, replay_state, _, replay_queue = artifact_state(root)
            self.assertTrue(
                replay_state.enqueue_confirmation(replay_queue)
            )
            replay_state.replay_actions = {
                failed_action["semantic_key"]: failed_action
            }
            judge = (
                test_root_confirmation_fix30
                .CountingArtifactConfirmationJudge()
            )
            replay = (
                test_root_confirmation_fix30
                .CapturingConfirmationAnalyzer(
                    judge=judge,
                    forbid_request_rebuild=True,
                )
            )
            replay._confirm_queued_roots(replay_state)

        self.assertEqual(judge.confirmation_calls, 0)
        self.assertEqual(replay_state.provider_state, {})
        self.assertEqual(len(replay_state.confirmations), 1)


if __name__ == "__main__":
    unittest.main()
