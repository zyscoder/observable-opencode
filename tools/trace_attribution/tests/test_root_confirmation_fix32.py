from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_state import (
    DefectState,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_identity_for,
    confirmation_response_identity_for,
)
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
from tools.trace_attribution.tests.test_root_confirmation_fix30 import (
    CapturingConfirmationAnalyzer,
    CountingArtifactConfirmationJudge,
)
from tools.trace_attribution.tests import test_root_confirmation_fix31


def _mutate_projection_entry(entry, mutate_facts):
    projection = copy.deepcopy(entry["factual_request_projection"])
    mutate_facts(projection["facts"])
    identity = test_root_confirmation_fix31.direct_projection_identity(
        projection
    )
    entry["factual_request_projection"] = projection
    if "semantic_identity" in entry:
        entry["semantic_identity"] = identity
    if "request_identity" in entry:
        entry["request_identity"] = identity
    if "semantic_key" in entry:
        entry["semantic_key"] = "confirmation:{0}".format(identity)


def coordinated_projection_drift(container, mutate_facts):
    for name in ("confirmation_queue", "confirmation_journal"):
        for entry in container.get(name) or ():
            _mutate_projection_entry(entry, mutate_facts)
    for entry in container.get("confirmation_action_projection") or ():
        _mutate_projection_entry(entry, mutate_facts)


def coordinated_checkpoint_drift(actions, mutate_facts):
    for action in actions:
        if action.get("operation") == "state_snapshot":
            coordinated_projection_drift(action["payload"], mutate_facts)
            continue
        if action.get("operation") not in {
            "confirmation_completed",
            "confirmation_failed",
        }:
            continue
        projection = action["payload"]["action_projection"]
        _mutate_projection_entry(projection, mutate_facts)
        action["semantic_key"] = projection["semantic_key"]


def _replace_defect_state(facts):
    original = DefectState.from_dict(dict(facts["defect_state"]))
    facts["defect_state"] = DefectState.create(
        label=original.label,
        expected=original.expected,
        actual=original.actual,
        mechanism="{0} coordinated drift".format(original.mechanism),
        scope=original.scope,
        derived_from_defect_state_id=original.derived_from_defect_state_id,
        transformation_reason=original.transformation_reason,
    ).to_dict()


def _append(value):
    def mutate(facts):
        facts[value] = "{0}:coordinated-drift".format(facts[value])

    return mutate


def _replace_candidate(facts):
    facts["candidate_ref"] = "record:coordinated-candidate"


def _replace_path(facts):
    facts["recursive_path"] = [
        *facts["recursive_path"],
        facts["recursive_path"][-1],
    ]


COORDINATED_FACT_DRIFTS = (
    ("candidate", _replace_candidate),
    ("defect_state", _replace_defect_state),
    ("recursive_path", _replace_path),
    ("hypothesis_id", _append("hypothesis_id")),
    ("hypothesis_semantic_hash", _append("hypothesis_semantic_hash")),
    ("seed_binding_identity", _append("seed_binding_identity")),
)


# These mutations synchronize projection/identity copies while deliberately
# preserving outer authority. A coherent rewrite of every authority is outside
# this internal-consistency contract.
def drift_terminal_path(action):
    projection = action["payload"]["action_projection"]
    confirmation = copy.deepcopy(projection["confirmation"])
    confirmation["recursive_path"].append(confirmation["recursive_path"][-1])
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=confirmation["candidate_ref"],
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=tuple(confirmation["recursive_path"]),
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
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
    projection["recursive_path"] = copy.deepcopy(
        confirmation["recursive_path"]
    )
    projection["response_identity"] = confirmation["response_identity"]
    projection["confirmation"] = copy.deepcopy(confirmation)
    action["payload"]["confirmation"] = copy.deepcopy(confirmation)


class ProjectionOuterTerminalBindingTest(
    test_root_confirmation_fix31.RejectedProjectionIdentityBindingTest
):
    def test_coordinated_rejected_perspective_drift_fails_checkpoint_restore(
        self,
    ):
        _, graph, _, _, checkpoint, _, _, _ = self.rejected_fixture(
            "fix32-perspective-checkpoint"
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        coordinated_checkpoint_drift(
            actions,
            _append("analysis_perspective"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "projection.*binding|outer request",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_coordinated_request_fact_drift_fails_report_and_evaluator(
        self,
    ):
        _, graph, _, _, _, report, _, _ = self.rejected_fixture(
            "fix32-report-bindings"
        )
        for label, mutate in COORDINATED_FACT_DRIFTS:
            with self.subTest(field=label):
                payload = report.to_dict()
                coordinated_projection_drift(payload["metadata"], mutate)
                restored = type(report).from_dict(payload)
                with self.assertRaisesRegex(
                    ValueError,
                    "projection.*binding|outer request",
                ):
                    validate_recursive_report_against_graph(
                        graph,
                        restored,
                        label="fix32 coordinated projection drift",
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

    def test_new_terminal_action_rejects_outer_path_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, state, _, queued = artifact_state(root)
            self.assertTrue(state.enqueue_confirmation(queued))
            stored = state.confirmation_queue[0]
            confirmation = RootConfirmation(
                candidate_ref=stored["candidate_ref"],
                status="unknown",
                reason="terminal confirmation remains unknown",
                counterfactual_status="unknown",
                hypothesis_id=stored["hypothesis_id"],
                hypothesis_semantic_hash=stored[
                    "hypothesis_semantic_hash"
                ],
                defect_fingerprint=stored["defect_fingerprint"],
                recursive_path=(
                    *stored["recursive_path"],
                    stored["recursive_path"][-1],
                ),
                seed_binding_identity=stored["seed_binding_identity"],
            )
            analyzer = CapturingConfirmationAnalyzer(
                judge=CountingArtifactConfirmationJudge()
            )

            with self.assertRaisesRegex(
                ValueError,
                "projection.*binding|outer request",
            ):
                analyzer._persist_confirmation_action(
                    state,
                    stored,
                    confirmation,
                    operation="confirmation_failed",
                    physical_requests_reserved=0,
                    physical_request_delta=0,
                    physical_request_exact=True,
                )

    def test_rejected_replay_rejects_terminal_outer_drift_without_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, failed_state, _, failed_queue = artifact_state(root)
            self.assertTrue(failed_state.enqueue_confirmation(failed_queue))
            (root / "artifacts" / "proof.txt").write_bytes(b"drifted proof")
            first = CapturingConfirmationAnalyzer(
                judge=CountingArtifactConfirmationJudge()
            )
            first._confirm_queued_roots(failed_state)
            failed_action = next(
                copy.deepcopy(item)
                for item in first.captured_actions
                if item["operation"] == "confirmation_failed"
            )
            drift_terminal_path(failed_action)

            _, replay_state, _, replay_queue = artifact_state(root)
            self.assertTrue(replay_state.enqueue_confirmation(replay_queue))
            replay_state.replay_actions = {
                failed_action["semantic_key"]: failed_action
            }
            replay_judge = CountingArtifactConfirmationJudge()
            replay = CapturingConfirmationAnalyzer(
                judge=replay_judge,
                forbid_request_rebuild=True,
            )

            with self.assertRaisesRegex(
                ValueError,
                "projection.*binding|outer request",
            ):
                replay._confirm_queued_roots(replay_state)

        self.assertEqual(replay_judge.confirmation_calls, 0)
        self.assertEqual(replay.captured_actions, [])

    def test_valid_pending_projection_json_round_trip_remains_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            _, state, _, queued = artifact_state(Path(directory))
            self.assertTrue(state.enqueue_confirmation(queued))
            round_tripped = json.loads(
                json.dumps(state.confirmation_queue[0])
            )
            state.confirmation_queue = [round_tripped]
            state.confirmation_queue_keys = {
                state._confirmation_queue_key(round_tripped)
            }
            state.validate_confirmation_queue_bound()

        self.assertEqual(round_tripped["status"], "queued")
