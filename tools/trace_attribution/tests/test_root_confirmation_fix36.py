from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tools.trace_attribution.scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_judge import (
    bind_root_confirmation,
    validate_recursive_confirmation,
)
from trace_attribution.causal_state import (
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    LocalStateOwner,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_response_identity_for,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    OUTPUT_SCHEMA_VERSION,
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _validated_confirmation_action_projection,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests import (
    test_root_confirmation_fix30,
    test_root_confirmation_fix33,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    ArtifactConfirmationJudge,
    artifact_state,
    artifact_trace,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix34 import (
    _replay_fixture,
    _state_snapshot,
)


def _request(root: Path):
    _, state, _, queued = artifact_state(root)
    return AgenticRecursiveAnalyzer._build_confirmation_request(state, queued)


def _valid_payload(request):
    return {
        "candidate_ref": request.candidate_ref,
        "status": "confirmed",
        "excerpt": "verified proof",
        "reason": "The verified candidate fact independently establishes the defect.",
        "counterfactual": {
            "intervention_ref": request.candidate_ref,
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_defect_status": "absent",
            "causal_effect": "prevents_defect",
        },
        "confidence": 0.93,
        "evidence_refs": ["artifact:proof"],
        "factor_role": "necessary_cause",
        "competitor_comparisons": [],
        "factor_mechanism": {},
    }


def _bound_confirmation(root: Path) -> RootConfirmation:
    request = _request(root)
    return validate_recursive_confirmation(
        _valid_payload(request),
        request=request,
    )


def _artifact_run(root: Path):
    trace = artifact_trace()
    content = b"verified proof"
    trace["artifacts"][0]["hash"] = "sha256:{0}".format(
        hashlib.sha256(content).hexdigest()
    )
    trace["artifacts"][0]["byte_length"] = len(content)
    target = root / "artifacts" / "proof.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    graph = TraceGraph.from_trace(trace, artifact_root=root)
    config = build_checkpoint_config(
        trace=trace,
        case_id=graph.case_id,
        objective="Confirm the candidate.",
        analysis_perspective="",
        start_refs=["record:candidate"],
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        model_identity="offline:fix36",
        cache_identity="cache:fix36",
        runtime_identity={
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://fix36",
            "provider_error_threshold": 3,
        },
    )
    checkpoint_root = root / "fix36.checkpoint"
    report = AgenticRecursiveAnalyzer(
        judge=ArtifactConfirmationJudge(),
        checkpoint=CheckpointBundle(checkpoint_root),
        checkpoint_config=config,
    ).analyze(
        graph,
        start_refs=["record:candidate"],
        objective="Confirm the candidate.",
        analysis_perspective="",
    )
    checkpoint = CheckpointBundle(checkpoint_root).restore(
        expected_config=config
    )
    return graph, report, checkpoint


def _replace_first_confirmation(container, confirmation):
    container["confirmations"][0] = confirmation.to_dict()


def _unsafe_field(confirmation, field_name, value):
    tampered = copy.copy(confirmation)
    object.__setattr__(tampered, field_name, value)
    return tampered


def _payload_response_identity(payload):
    return confirmation_response_identity_for(
        confirmation_identity=payload["confirmation_identity"],
        status=payload["status"],
        excerpt=payload["excerpt"],
        reason=payload["reason"],
        counterfactual=payload["counterfactual"],
        confidence=payload["confidence"],
        evidence_refs=tuple(payload["evidence_refs"]),
        counterfactual_status=payload["counterfactual_status"],
        factor_role=payload["factor_role"],
        competitor_comparisons=tuple(payload["competitor_comparisons"]),
        factor_mechanism=payload["factor_mechanism"],
        analysis_perspective=payload.get("analysis_perspective", ""),
    )


class SharedConfirmedInvariantTest(unittest.TestCase):
    def test_offline_binder_rejects_each_incomplete_confirmed_semantic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = _request(root)
            valid = validate_recursive_confirmation(
                _valid_payload(request),
                request=request,
            )
            mutations = {
                "empty evidence": replace(valid, evidence_refs=()),
                "empty excerpt": replace(valid, excerpt=""),
                "ungrounded excerpt": replace(
                    valid, excerpt="not present in the offered facts"
                ),
                "empty reason": replace(valid, reason=""),
                "zero confidence": replace(valid, confidence=0.0),
                "empty counterfactual": replace(valid, counterfactual=""),
                "inconsistent counterfactual": _unsafe_field(
                    valid,
                    "counterfactual_status",
                    "rejects_causality",
                ),
            }
            for label, confirmation in mutations.items():
                with self.subTest(label=label):
                    with self.assertRaises(ValueError):
                        bind_root_confirmation(
                            confirmation,
                            request=request,
                        )

    def test_live_and_offline_paths_share_valid_and_invalid_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory))
            payload = _valid_payload(request)
            live = validate_recursive_confirmation(
                payload,
                request=request,
            )
            offline = bind_root_confirmation(live, request=request)
            self.assertEqual(live.to_dict(), offline.to_dict())

            live_mutations = {
                "empty evidence": lambda value: value.__setitem__(
                    "evidence_refs", []
                ),
                "empty excerpt": lambda value: value.__setitem__("excerpt", ""),
                "empty reason": lambda value: value.__setitem__("reason", ""),
                "zero confidence": lambda value: value.__setitem__(
                    "confidence", 0.0
                ),
                "inconsistent counterfactual": lambda value: value[
                    "counterfactual"
                ].__setitem__("causal_effect", "unknown"),
            }
            offline_mutations = {
                "empty evidence": replace(live, evidence_refs=()),
                "empty excerpt": replace(live, excerpt=""),
                "empty reason": replace(live, reason=""),
                "zero confidence": replace(live, confidence=0.0),
                "inconsistent counterfactual": _unsafe_field(
                    live,
                    "counterfactual_status",
                    "unknown",
                ),
            }
            for label, mutate in live_mutations.items():
                with self.subTest(label=label):
                    candidate = copy.deepcopy(payload)
                    mutate(candidate)
                    with self.assertRaises(ValueError):
                        validate_recursive_confirmation(
                            candidate,
                            request=request,
                        )
                    with self.assertRaises(ValueError):
                        bind_root_confirmation(
                            offline_mutations[label],
                            request=request,
                        )


class ConfirmationResponseIdentityTest(unittest.TestCase):
    def test_response_identity_round_trips_and_covers_all_substantive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = _bound_confirmation(Path(directory))
            payload = valid.to_dict()
            self.assertTrue(payload["response_identity"].startswith("response:"))
            self.assertNotEqual(
                payload["response_identity"],
                payload["confirmation_identity"],
            )
            self.assertEqual(
                RootConfirmation.from_dict(
                    json.loads(json.dumps(payload))
                ).to_dict(),
                payload,
            )
            missing_identity = copy.deepcopy(payload)
            missing_identity.pop("response_identity")
            with self.assertRaisesRegex(ValueError, "response_identity"):
                RootConfirmation.from_dict(missing_identity)

            mutations = {
                "status": lambda value: value.__setitem__("status", "rejected"),
                "excerpt": lambda value: value.__setitem__(
                    "excerpt", "verified"
                ),
                "reason": lambda value: value.__setitem__(
                    "reason", "A substantively different reason."
                ),
                "counterfactual": lambda value: value.__setitem__(
                    "counterfactual", "A different counterfactual."
                ),
                "confidence": lambda value: value.__setitem__("confidence", 0.71),
                "evidence_refs": lambda value: value.__setitem__(
                    "evidence_refs",
                    ["artifact:proof", "record:candidate"],
                ),
                "counterfactual_status": lambda value: value.__setitem__(
                    "counterfactual_status", "unknown"
                ),
                "factor_role": lambda value: value.__setitem__(
                    "factor_role", "unknown"
                ),
                "competitor_comparisons": lambda value: value.__setitem__(
                    "competitor_comparisons",
                    [{"candidate_ref": "record:other", "outcome": "weaker"}],
                ),
                "factor_mechanism": lambda value: value.__setitem__(
                    "factor_mechanism",
                    {"mechanism_type": "amplification"},
                ),
            }
            for field, mutate in mutations.items():
                with self.subTest(field=field):
                    tampered = copy.deepcopy(payload)
                    mutate(tampered)
                    self.assertEqual(
                        tampered["confirmation_identity"],
                        payload["confirmation_identity"],
                    )
                    self.assertNotEqual(
                        _payload_response_identity(tampered),
                        payload["response_identity"],
                    )
                    with self.assertRaises(ValueError):
                        RootConfirmation.from_dict(tampered)

    def test_occurrence_identity_is_stable_while_response_identity_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = _bound_confirmation(Path(directory))
            variants = (
                replace(valid, excerpt="verified"),
                replace(valid, reason="Different grounded reasoning."),
                replace(valid, confidence=0.81),
                replace(
                    valid,
                    evidence_refs=("artifact:proof", "record:candidate"),
                ),
            )
            for variant in variants:
                with self.subTest(response=variant.to_dict()):
                    self.assertEqual(
                        variant.confirmation_identity,
                        valid.confirmation_identity,
                    )
                    self.assertNotEqual(
                        variant.response_identity,
                        valid.response_identity,
                    )


class ConfirmationProjectionAndPersistenceTest(unittest.TestCase):
    def test_action_projection_uses_response_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            action = test_root_confirmation_fix33._completed_action(
                Path(directory)
            )
            projection = action["payload"]["action_projection"]
            confirmation = RootConfirmation.from_dict(
                projection["confirmation"]
            )
            self.assertEqual(
                projection["response_identity"],
                confirmation.response_identity,
            )
            self.assertNotEqual(
                projection["response_identity"],
                confirmation.confirmation_identity,
            )

            forged = copy.deepcopy(projection)
            forged["response_identity"] = confirmation.confirmation_identity
            with self.assertRaisesRegex(ValueError, "projection|response"):
                _validated_confirmation_action_projection(forged)

    def test_checkpoint_restore_and_completed_replay_reject_tampering_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label in ("response tamper", "empty evidence"):
                with self.subTest(label=label):
                    action, state, judge, analyzer = _replay_fixture(root)
                    action = json.loads(json.dumps(action))
                    state.replay_actions = {
                        action["semantic_key"]: action
                    }
                    projection = action["payload"]["action_projection"]
                    if label == "response tamper":
                        tampered = copy.deepcopy(
                            projection["confirmation"]
                        )
                        tampered["reason"] = (
                            "Tampered after response identity was signed."
                        )
                    else:
                        valid = RootConfirmation.from_dict(
                            projection["confirmation"]
                        )
                        tampered = replace(
                            valid, evidence_refs=()
                        ).to_dict()
                        projection["evidence_refs"] = []
                        projection["response_identity"] = tampered[
                            "response_identity"
                        ]
                    projection["confirmation"] = tampered
                    action["payload"]["confirmation"] = copy.deepcopy(
                        tampered
                    )
                    before = _state_snapshot(state, judge)
                    with self.assertRaisesRegex(
                        ValueError,
                        "response_identity|projection|semantic|evidence|confirmed",
                    ):
                        analyzer._confirm_queued_roots(state)
                    self.assertEqual(_state_snapshot(state, judge), before)
                    self.assertEqual(judge.confirmation_calls, 0)

            graph, _, checkpoint = _artifact_run(root / "checkpoint-run")
            actions = copy.deepcopy(list(checkpoint.actions))
            snapshot = latest_snapshot(actions)
            valid_payload = snapshot["confirmations"][0]
            valid = RootConfirmation.from_dict(valid_payload)
            evidence_free = replace(valid, evidence_refs=())
            _replace_first_confirmation(snapshot, evidence_free)
            with self.assertRaisesRegex(
                ValueError,
                "evidence|confirmed|response",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=graph,
                    checkpoint=partial_checkpoint(checkpoint, actions),
                )

    def test_report_restore_graph_validation_and_evaluator_share_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _artifact_run(Path(directory))
            control = json.loads(json.dumps(report.to_dict()))
            restored = RecursiveAttributionReport.from_dict(control)
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix36 valid report",
                action_records=checkpoint.actions,
            )
            comparison = compare_report(
                annotate_report_semantic_anchors(
                    graph.case_id,
                    graph.nodes,
                    control,
                    graph=graph,
                ),
                permissive_labels(graph.case_id),
                None,
                graph=graph,
            )
            self.assertTrue(comparison["safety"]["passed"])

            for label, mutate in (
                (
                    "response tamper",
                    lambda value: value.__setitem__(
                        "reason", "Tampered report reason."
                    ),
                ),
                (
                    "empty evidence",
                    lambda value: value.__setitem__("evidence_refs", []),
                ),
            ):
                with self.subTest(label=label):
                    tampered = copy.deepcopy(control)
                    if label == "empty evidence":
                        serialized = RootConfirmation.from_dict(
                            tampered["confirmations"][0]
                        )
                        tampered["confirmations"][0] = replace(
                            serialized, evidence_refs=()
                        ).to_dict()
                    else:
                        mutate(tampered["confirmations"][0])
                    with self.assertRaises(ValueError):
                        RecursiveAttributionReport.from_dict(tampered)
                    with self.assertRaises(
                        (EvaluationSafetyError, ValueError)
                    ):
                        compare_report(
                            annotate_report_semantic_anchors(
                                graph.case_id,
                                graph.nodes,
                                tampered,
                                graph=graph,
                            ),
                            permissive_labels(graph.case_id),
                            None,
                            graph=graph,
                        )

    def test_seed_builder_cannot_publish_evidence_free_confirmed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, state, item, queued = artifact_state(root)
            builder = state.seed_ledger[item.seed_binding_identity]
            owner = LocalStateOwner.from_dict(queued["owner"])
            confirmation = _bound_confirmation(root)
            evidence_free = replace(confirmation, evidence_refs=())

            with self.assertRaisesRegex(ValueError, "evidence|confirmed"):
                builder.record_confirmation(evidence_free, owner)

            self.assertNotEqual(
                builder.to_result().outcome,
                "confirmed_root",
            )


class ConfirmationContractVersionTest(unittest.TestCase):
    def test_changed_persistence_contracts_are_explicitly_versioned(self):
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v22",
        )
        self.assertIn(
            "recursive-root-confirmation/v17",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "response-identity/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "action-projection/v8",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v28",
        )
        self.assertEqual(
            OUTPUT_SCHEMA_VERSION,
            "recursive-attribution-output/v15",
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v25",
        )


if __name__ == "__main__":
    unittest.main()
