from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from tools.trace_attribution.scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_judge import (
    root_confirmation_request_projection_identity,
)
from trace_attribution.causal_state import (
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_counterfactual_for,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix29 import (
    artifact_checkpoint_config,
    multi_owner_artifact_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix30 import (
    CountingArtifactConfirmationJudge,
    PreflightArtifactMutationAnalyzer,
    artifact_mutation,
)
from tools.trace_attribution.tests.test_root_confirmation_fix37 import (
    _artifact_run,
    _mutated_confirmation,
    _rewrite_checkpoint,
    _rewrite_report,
)


def _rejected_artifact_run(root: Path):
    trace, _ = multi_owner_artifact_graph(root)
    graph = TraceGraph.from_trace(trace, artifact_root=root)
    config = artifact_checkpoint_config(
        trace,
        graph,
        ("record:candidate",),
    )
    checkpoint_root = root / "fix38-rejected.checkpoint"
    judge = CountingArtifactConfirmationJudge()
    analyzer = PreflightArtifactMutationAnalyzer(
        mutation=artifact_mutation("content_drift", root),
        judge=judge,
        checkpoint=CheckpointBundle(checkpoint_root),
        checkpoint_config=config,
    )
    report = analyzer.analyze(
        graph,
        start_refs=("record:candidate",),
        objective="Confirm each explicitly owned artifact candidate.",
        analysis_perspective="",
    )
    checkpoint = CheckpointBundle(checkpoint_root).restore(
        expected_config=config
    )
    return graph, report, checkpoint


def _assert_evaluator_rejects(testcase, graph, payload):
    with testcase.assertRaises(EvaluationSafetyError):
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


class PersistedCounterfactualInvariantTest(unittest.TestCase):
    def test_root_confirmation_from_dict_rejects_resigned_counterfactual(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report, _ = _artifact_run(Path(directory))
            valid = report.to_dict()["confirmations"][0]
            tampered = _mutated_confirmation(
                valid,
                counterfactual="x",
            )

            with self.assertRaisesRegex(ValueError, "counterfactual"):
                RootConfirmation.from_dict(tampered)

    def test_report_from_dict_rejects_resigned_counterfactual(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report, _ = _artifact_run(Path(directory))
            payload = report.to_dict()
            tampered = _mutated_confirmation(
                payload["confirmations"][0],
                counterfactual="x",
            )
            _rewrite_report(payload, tampered)

            with self.assertRaisesRegex(ValueError, "counterfactual"):
                RecursiveAttributionReport.from_dict(payload)

    def test_valid_confirmed_persistence_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _artifact_run(Path(directory))
            payload = report.to_dict()
            restored = RecursiveAttributionReport.from_dict(payload)

            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix38 valid confirmed report",
                action_records=checkpoint.actions,
            )


class RejectedSnapshotSemanticBoundaryTest(unittest.TestCase):
    def test_valid_rejected_snapshot_retains_narrow_artifact_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _rejected_artifact_run(
                Path(directory)
            )
            payload = report.to_dict()
            confirmation = payload["confirmations"][0]

            self.assertEqual(confirmation["status"], "unknown")
            self.assertEqual(confirmation["counterfactual_status"], "unknown")
            self.assertEqual(confirmation["factor_role"], "unknown")
            self.assertEqual(confirmation["evidence_refs"], [])
            RootConfirmation.from_dict(confirmation)
            restored = RecursiveAttributionReport.from_dict(payload)
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix38 valid rejected snapshot",
                action_records=checkpoint.actions,
            )
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(
                    checkpoint,
                    list(checkpoint.actions),
                ),
            )

    def test_resigned_rejected_counterfactual_is_rejected_everywhere(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _rejected_artifact_run(
                Path(directory)
            )
            valid_payload = report.to_dict()
            malformed = _mutated_confirmation(
                valid_payload["confirmations"][0],
                counterfactual="x",
            )

            with self.assertRaisesRegex(ValueError, "counterfactual"):
                RootConfirmation.from_dict(malformed)

            report_payload = copy.deepcopy(valid_payload)
            _rewrite_report(report_payload, malformed)
            with self.assertRaisesRegex(ValueError, "counterfactual"):
                RecursiveAttributionReport.from_dict(report_payload)
            _assert_evaluator_rejects(self, graph, report_payload)

            actions = copy.deepcopy(list(checkpoint.actions))
            _rewrite_checkpoint(actions, malformed)
            with self.assertRaisesRegex(ValueError, "counterfactual"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=graph,
                    checkpoint=partial_checkpoint(checkpoint, actions),
                )

    def test_rejected_snapshot_requires_exact_synthetic_unknown_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _rejected_artifact_run(
                Path(directory)
            )
            valid_payload = report.to_dict()
            valid = valid_payload["confirmations"][0]
            mutations = {
                "reason": {"reason": "coherently resigned but forged reason"},
                "evidence": {"evidence_refs": ["record:candidate"]},
                "factor": {"factor_role": "unrelated"},
                "counterfactual status": {
                    "counterfactual_status": "supports_causality"
                },
                "status": {
                    "status": "rejected",
                    "counterfactual": confirmation_counterfactual_for(
                        valid["candidate_ref"],
                        "rejected",
                    ),
                    "counterfactual_status": "rejects_causality",
                    "factor_role": "unrelated",
                    "confidence": 0.5,
                },
            }
            for label, changes in mutations.items():
                with self.subTest(label=label):
                    tampered = _mutated_confirmation(valid, **changes)
                    report_payload = copy.deepcopy(valid_payload)
                    _rewrite_report(report_payload, tampered)
                    try:
                        restored = RecursiveAttributionReport.from_dict(
                            report_payload
                        )
                    except ValueError:
                        restored = None
                    if restored is not None:
                        with self.assertRaisesRegex(
                            ValueError,
                            "rejected|unknown|evidence|reason|factor|status",
                        ):
                            validate_recursive_report_against_graph(
                                graph,
                                restored,
                                label="Fix38 forged rejected snapshot",
                                action_records=checkpoint.actions,
                            )

                    actions = copy.deepcopy(list(checkpoint.actions))
                    _rewrite_checkpoint(actions, tampered)
                    with self.assertRaisesRegex(
                        ValueError,
                        "rejected|unknown|evidence|reason|factor|status",
                    ):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=graph,
                            checkpoint=partial_checkpoint(
                                checkpoint,
                                actions,
                            ),
                        )

    def test_rejected_snapshot_still_requires_request_structure_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _rejected_artifact_run(
                Path(directory)
            )
            payload = report.to_dict()
            queue = payload["metadata"]["confirmation_queue"][0]
            queue["factual_request_projection"]["facts"]["candidate_ref"] = (
                "record:other_owner"
            )
            queue["semantic_identity"] = (
                root_confirmation_request_projection_identity(
                    queue["factual_request_projection"]
                )
            )

            restored = RecursiveAttributionReport.from_dict(payload)
            with self.assertRaisesRegex(
                ValueError,
                "projection|binding|identity",
            ):
                validate_recursive_report_against_graph(
                    graph,
                    restored,
                    label="Fix38 rejected request binding",
                    action_records=checkpoint.actions,
                )

    def test_rejected_snapshot_still_requires_outer_response_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, report, checkpoint = _rejected_artifact_run(
                Path(directory)
            )
            payload = report.to_dict()
            queue = payload["metadata"]["confirmation_queue"][0]
            queue["response_identity"] = queue["confirmation"][
                "confirmation_identity"
            ]

            restored = RecursiveAttributionReport.from_dict(payload)
            with self.assertRaisesRegex(
                ValueError,
                "response|identity|projection",
            ):
                validate_recursive_report_against_graph(
                    graph,
                    restored,
                    label="Fix38 rejected response identity",
                    action_records=checkpoint.actions,
                )


if __name__ == "__main__":
    unittest.main()
