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
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_counterfactual_for,
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
    validate_recursive_report_against_graph,
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


def _live_payload(request):
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


class CanonicalArtifactConfirmationJudge(ArtifactConfirmationJudge):
    def confirm_candidate_offline(self, request):
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="verified proof",
            reason="The complete verified artifact confirms the defect.",
            counterfactual=confirmation_counterfactual_for(
                request.candidate_ref,
                "confirmed",
            ),
            confidence=0.95,
            evidence_refs=["artifact:proof"],
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
        model_identity="offline:fix37",
        cache_identity="cache:fix37",
        runtime_identity={
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://fix37",
            "provider_error_threshold": 3,
        },
    )
    checkpoint_root = root / "fix37.checkpoint"
    report = AgenticRecursiveAnalyzer(
        judge=CanonicalArtifactConfirmationJudge(),
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


def _response_identity(payload):
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


def _mutated_confirmation(payload, **changes):
    result = copy.deepcopy(payload)
    result.update(changes)
    result["response_identity"] = _response_identity(result)
    return result


def _rewrite_terminal_collections(container, confirmation):
    response_identity = confirmation["response_identity"]
    if container.get("confirmations"):
        container["confirmations"][0] = copy.deepcopy(confirmation)
    for root_key in ("confirmed_roots", "co_roots"):
        for root in container.get(root_key, []):
            root["confirmation"] = copy.deepcopy(confirmation)
            for field in (
                "reason",
                "counterfactual",
                "confidence",
                "evidence_refs",
                "excerpt",
            ):
                root[field] = copy.deepcopy(confirmation[field])
    for queue in container.get("confirmation_queue", []):
        queue["confirmation"] = copy.deepcopy(confirmation)
        if "response_identity" in queue:
            queue["response_identity"] = response_identity
    for journal in container.get("confirmation_journal", []):
        journal["confirmation"] = copy.deepcopy(confirmation)
        journal["response_identity"] = response_identity
    for projection in container.get("confirmation_action_projection", []):
        projection["confirmation"] = copy.deepcopy(confirmation)
        projection["response_identity"] = response_identity
        projection["evidence_refs"] = copy.deepcopy(
            confirmation["evidence_refs"]
        )


def _rewrite_report(report, confirmation):
    _rewrite_terminal_collections(report, confirmation)
    _rewrite_terminal_collections(report["metadata"], confirmation)
    for legacy in report.get("root_causes", []):
        legacy["reason"] = confirmation["reason"]
        legacy["confidence"] = confirmation["confidence"]


def _rewrite_checkpoint(actions, confirmation):
    snapshot = latest_snapshot(actions)
    _rewrite_terminal_collections(snapshot, confirmation)
    for action in actions:
        if action.get("operation") not in {
            "confirmation_completed",
            "confirmation_failed",
        }:
            continue
        action["payload"]["confirmation"] = copy.deepcopy(confirmation)
        projection = action["payload"]["action_projection"]
        projection["confirmation"] = copy.deepcopy(confirmation)
        projection["response_identity"] = confirmation["response_identity"]
        projection["evidence_refs"] = copy.deepcopy(
            confirmation["evidence_refs"]
        )


class CanonicalCounterfactualTest(unittest.TestCase):
    def test_live_and_offline_require_the_same_complete_counterfactual(self):
        with tempfile.TemporaryDirectory() as directory:
            request = _request(Path(directory))
            live = validate_recursive_confirmation(
                _live_payload(request),
                request=request,
            )
            self.assertEqual(
                bind_root_confirmation(live, request=request),
                live,
            )

            canonical = json.loads(
                confirmation_counterfactual_for(
                    request.candidate_ref,
                    "confirmed",
                )
            )
            wrong_prediction = copy.deepcopy(canonical)
            wrong_prediction["predicted_defect_status"] = "present"
            wrong_effect = copy.deepcopy(canonical)
            wrong_effect["causal_effect"] = "does_not_prevent_defect"
            mutations = {
                "incomplete": "x",
                "wrong candidate": confirmation_counterfactual_for(
                    "record:other",
                    "confirmed",
                ),
                "wrong prediction": json.dumps(
                    wrong_prediction,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "wrong effect": json.dumps(
                    wrong_effect,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            for label, counterfactual in mutations.items():
                with self.subTest(label=label):
                    with self.assertRaisesRegex(
                        ValueError,
                        "counterfactual",
                    ):
                        bind_root_confirmation(
                            replace(
                                live,
                                counterfactual=counterfactual,
                            ),
                            request=request,
                        )


class RequestAwareRestoreTest(unittest.TestCase):
    def test_checkpoint_rejects_coherently_resigned_semantic_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph, _, checkpoint = _artifact_run(root)
            valid_actions = copy.deepcopy(list(checkpoint.actions))
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, valid_actions),
            )
            valid = latest_snapshot(valid_actions)["confirmations"][0]
            for label, changes in (
                (
                    "ungrounded excerpt",
                    {"excerpt": "not present in the offered candidate facts"},
                ),
                ("malformed counterfactual", {"counterfactual": "x"}),
            ):
                with self.subTest(label=label):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    confirmation = _mutated_confirmation(valid, **changes)
                    _rewrite_checkpoint(actions, confirmation)
                    with self.assertRaisesRegex(
                        ValueError,
                        "counterfactual|excerpt|grounded|binding|response",
                    ):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=graph,
                            checkpoint=partial_checkpoint(
                                checkpoint,
                                actions,
                            ),
                        )

    def test_completed_replay_rejects_resigned_semantic_tampering_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            action, state, judge, analyzer = _replay_fixture(
                Path(directory)
            )
            action = copy.deepcopy(action)
            projection = action["payload"]["action_projection"]
            tampered = _mutated_confirmation(
                projection["confirmation"],
                excerpt="not present in the offered candidate facts",
            )
            action["payload"]["confirmation"] = copy.deepcopy(tampered)
            projection["confirmation"] = copy.deepcopy(tampered)
            projection["response_identity"] = tampered[
                "response_identity"
            ]
            projection["evidence_refs"] = copy.deepcopy(
                tampered["evidence_refs"]
            )
            state.replay_actions = {
                action["semantic_key"]: action,
            }

            before = _state_snapshot(state, judge)
            with self.assertRaisesRegex(
                ValueError,
                "excerpt|grounded|binding|response",
            ):
                analyzer._confirm_queued_roots(state)
            self.assertEqual(_state_snapshot(state, judge), before)
            self.assertEqual(judge.confirmation_calls, 0)

    def test_report_graph_restore_and_evaluator_reject_semantic_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _artifact_run(Path(directory))
            control = json.loads(json.dumps(report.to_dict()))
            restored = RecursiveAttributionReport.from_dict(control)
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix37 valid report",
                action_records=checkpoint.actions,
            )
            self.assertTrue(
                compare_report(
                    annotate_report_semantic_anchors(
                        graph.case_id,
                        graph.nodes,
                        control,
                        graph=graph,
                    ),
                    permissive_labels(graph.case_id),
                    None,
                    graph=graph,
                )["safety"]["passed"]
            )

            valid = control["confirmations"][0]
            for label, changes in (
                (
                    "ungrounded excerpt",
                    {"excerpt": "not present in the offered candidate facts"},
                ),
                ("malformed counterfactual", {"counterfactual": "x"}),
            ):
                with self.subTest(label=label):
                    tampered = copy.deepcopy(control)
                    confirmation = _mutated_confirmation(valid, **changes)
                    _rewrite_report(tampered, confirmation)
                    if label == "malformed counterfactual":
                        with self.assertRaisesRegex(
                            ValueError,
                            "counterfactual",
                        ):
                            RecursiveAttributionReport.from_dict(tampered)
                        with self.assertRaises(EvaluationSafetyError):
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
                        continue
                    try:
                        parsed = RecursiveAttributionReport.from_dict(tampered)
                    except ValueError:
                        parsed = None
                    if parsed is not None:
                        with self.assertRaisesRegex(
                            ValueError,
                            "counterfactual|excerpt|grounded|binding",
                        ):
                            validate_recursive_report_against_graph(
                                graph,
                                parsed,
                                label="Fix37 tampered report",
                            )
                    with self.assertRaises(EvaluationSafetyError):
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


class ResponseIdentityBijectionTest(unittest.TestCase):
    def test_queue_journal_and_action_share_response_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _artifact_run(Path(directory))
            payload = report.to_dict()
            response_identity = payload["confirmations"][0][
                "response_identity"
            ]
            metadata = payload["metadata"]
            self.assertIn(
                "response_identity",
                metadata["confirmation_queue"][0],
            )
            self.assertEqual(
                metadata["confirmation_queue"][0]["response_identity"],
                response_identity,
            )
            self.assertEqual(
                metadata["confirmation_journal"][0]["response_identity"],
                response_identity,
            )
            self.assertEqual(
                metadata["confirmation_action_projection"][0][
                    "response_identity"
                ],
                response_identity,
            )

            for location in ("queue", "journal", "action"):
                with self.subTest(location=location):
                    tampered = copy.deepcopy(payload)
                    metadata = tampered["metadata"]
                    target = {
                        "queue": metadata["confirmation_queue"][0],
                        "journal": metadata["confirmation_journal"][0],
                        "action": metadata[
                            "confirmation_action_projection"
                        ][0],
                    }[location]
                    target["response_identity"] = tampered[
                        "confirmations"
                    ][0]["confirmation_identity"]
                    parsed = RecursiveAttributionReport.from_dict(tampered)
                    with self.assertRaisesRegex(
                        ValueError,
                        "response|bijection|projection|queue|journal",
                    ):
                        validate_recursive_report_against_graph(
                            graph,
                            parsed,
                            label="Fix37 response identity report",
                            action_records=checkpoint.actions,
                        )

    def test_checkpoint_rejects_outer_response_identity_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, _, checkpoint = _artifact_run(Path(directory))
            for location in ("queue", "journal", "action"):
                with self.subTest(location=location):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    snapshot = latest_snapshot(actions)
                    occurrence_identity = snapshot["confirmations"][0][
                        "confirmation_identity"
                    ]
                    target = {
                        "queue": snapshot["confirmation_queue"][0],
                        "journal": snapshot["confirmation_journal"][0],
                        "action": snapshot[
                            "confirmation_action_projection"
                        ][0],
                    }[location]
                    target["response_identity"] = occurrence_identity
                    with self.assertRaisesRegex(
                        ValueError,
                        "response|bijection|projection|queue|journal",
                    ):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=graph,
                            checkpoint=partial_checkpoint(
                                checkpoint,
                                actions,
                            ),
                        )


class PublishedRootProjectionTest(unittest.TestCase):
    def test_each_duplicate_response_field_must_match_embedded_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report, _ = _artifact_run(Path(directory))
            control = report.to_dict()
            mutations = {
                "reason": "FORGED ROOT REASON",
                "counterfactual": "FORGED ROOT COUNTERFACTUAL",
                "confidence": 0.11,
                "evidence_refs": ["record:candidate"],
                "excerpt": "FORGED ROOT EXCERPT",
            }
            for field, value in mutations.items():
                with self.subTest(field=field):
                    tampered = copy.deepcopy(control)
                    tampered["confirmed_roots"][0][field] = copy.deepcopy(
                        value
                    )
                    if field in {"reason", "confidence"}:
                        tampered["root_causes"][0][field] = copy.deepcopy(
                            value
                        )
                    with self.assertRaisesRegex(
                        ValueError,
                        "root|confirmation|projection|response",
                    ):
                        RecursiveAttributionReport.from_dict(tampered)


class Fix37PersistenceVersionTest(unittest.TestCase):
    def test_changed_persistence_contracts_are_explicitly_versioned(self):
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v20",
        )
        self.assertIn(
            "recursive-root-confirmation/v17",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "counterfactual/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "queue-response-identity/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "published-root-projection/v3",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "action-projection/v7",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v20",
        )
        self.assertEqual(
            OUTPUT_SCHEMA_VERSION,
            "recursive-attribution-output/v9",
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v17",
        )


if __name__ == "__main__":
    unittest.main()
