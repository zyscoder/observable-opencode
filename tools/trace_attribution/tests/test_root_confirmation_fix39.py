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
    REPORT_SCHEMA_VERSION,
    compare_report,
)
from trace_attribution import causal_state
from trace_attribution.causal_state import (
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_counterfactual_for,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    OUTPUT_SCHEMA_VERSION,
    CheckpointBundle,
    _sha256,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_recursive_analyzer import (
    ConfirmingScriptedJudge,
    FusionScriptedJudge,
    observed_trace,
    relation,
    step,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    permissive_labels,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    CrashAfterFirstCompletedConfirmation,
    SharedRootFusionJudge,
    TwoHopSharedRootJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
    two_hop_shared_root_trace,
)


PERSPECTIVE = "publication quality"
CAUSAL_PUBLICATION_CONTRACT_VERSION = getattr(
    causal_state,
    "CAUSAL_PUBLICATION_CONTRACT_VERSION",
    "missing-causal-publication-contract",
)


def _evaluate(graph: TraceGraph, payload: dict) -> dict:
    return compare_report(
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


def _run_shared_root(
    *,
    checkpoint: CheckpointBundle | None = None,
    checkpoint_config: dict | None = None,
):
    trace = shared_root_trace()
    graph = TraceGraph.from_trace(trace)
    report = AgenticRecursiveAnalyzer(
        judge=SharedRootFusionJudge(),
        fusion_mode="retrieval-global",
        checkpoint=checkpoint,
        checkpoint_config=checkpoint_config,
    ).analyze(
        graph,
        start_refs=("record:seed_one", "record:seed_two"),
        objective="Confirm each identical claim independently.",
        analysis_perspective="",
    )
    return trace, graph, report


def _co_root_judge() -> ConfirmingScriptedJudge:
    confirmation_step = _confirmation_step
    return ConfirmingScriptedJudge(
        {
            "record:change": step(
                "record:change",
                predecessors=(
                    relation("record:decision", "same_defect_propagation"),
                    relation("record:context", "same_defect_propagation"),
                ),
            ),
            "record:decision": confirmation_step,
            "record:context": confirmation_step,
        },
        {
            "record:decision": RootConfirmation.confirmed(
                "record:decision",
                excerpt="Implement only the methods found in the first search.",
                reason="The decision was independently necessary.",
                counterfactual=confirmation_counterfactual_for(
                    "record:decision", "confirmed"
                ),
                confidence=0.9,
                evidence_refs=["record:decision"],
            ),
            "record:context": RootConfirmation.confirmed(
                "record:context",
                excerpt="The complete compatibility contract is documented here.",
                reason="The context was independently necessary.",
                counterfactual=confirmation_counterfactual_for(
                    "record:context", "confirmed"
                ),
                confidence=0.8,
                evidence_refs=["record:context"],
            ),
        },
    )


def _run_co_roots(
    *,
    checkpoint: CheckpointBundle | None = None,
    checkpoint_config: dict | None = None,
):
    trace = observed_trace(branching=True)
    graph = TraceGraph.from_trace(trace)
    report = AgenticRecursiveAnalyzer(
        judge=_co_root_judge(),
        checkpoint=checkpoint,
        checkpoint_config=checkpoint_config,
    ).analyze(
        graph,
        start_refs=("record:observed_defect",),
        objective="Find every necessary cause.",
        analysis_perspective=PERSPECTIVE,
    )
    return trace, graph, report


def _run_non_root(role: str):
    trace = observed_trace(branching=True)
    graph = TraceGraph.from_trace(trace)
    judge = FusionScriptedJudge(
        global_outcome="candidate_roots",
        selected_candidate_refs=("record:decision",),
        global_non_root_roles={"record:context": role},
        factor_roles={"record:context": role},
        confirmations={
            "record:decision": RootConfirmation.confirmed(
                "record:decision",
                excerpt=(
                    "Implement only the methods found in the first search."
                ),
                reason="The decision was independently necessary.",
                counterfactual=confirmation_counterfactual_for(
                    "record:decision",
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=["record:decision"],
            ),
        },
    )
    report = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
    ).analyze(
        graph,
        start_refs=("record:observed_defect",),
        objective="Classify the candidate's causal role.",
        analysis_perspective=PERSPECTIVE,
    )
    return graph, report


def _confirmation_step(request):
    return step(
        request.current_node.ref,
        introduction=True,
        suggested={
            "action": "request_root_confirmation",
            "arguments": {
                "hypothesis_id": request.recursive_context[
                    "active_hypothesis_id"
                ],
                "candidate_ref": request.current_node.ref,
                "defect_fingerprint": request.defect_state.fingerprint,
            },
            "reason": "Independently verify this introduction candidate.",
        },
    )


def _coordinated_root_mutation(
    payload: dict,
    field: str,
    value,
) -> dict:
    result = copy.deepcopy(payload)
    root = result["confirmed_roots"][0]
    legacy = next(
        item
        for item in result["root_causes"]
        if item["node_ref"] == root["node_ref"]
        and item["observed_defect_refs"] == root["observed_defect_refs"]
    )
    if field == "provenance" and isinstance(value, dict):
        value = {
            **copy.deepcopy(value),
            "active_role_binding": copy.deepcopy(
                root["provenance"]["active_role_binding"]
            ),
        }
    root[field] = copy.deepcopy(value)
    if field in legacy:
        legacy[field] = copy.deepcopy(value)
    return result


def _legacy_root_projection(root: dict) -> dict:
    return {
        key: copy.deepcopy(root[key])
        for key in (
            "node_ref",
            "component",
            "event_type",
            "defect_type",
            "reason",
            "confidence",
            "causal_role",
            "episode_id",
            "episode_member_refs",
            "observed_defect_refs",
        )
    }


def _refresh_legacy_root_order(payload: dict) -> None:
    payload["root_causes"] = [
        _legacy_root_projection(root)
        for root in (
            *payload["confirmed_roots"],
            *payload["co_roots"],
        )
    ]


def _latest_state_snapshot(actions: list[dict]) -> dict:
    return next(
        item["payload"]
        for item in reversed(actions)
        if item["operation"] == "state_snapshot"
    )


class PerSeedRootPublicationTest(unittest.TestCase):
    def test_independent_seeds_publish_independent_primary_roots(self):
        _, _, report = _run_shared_root()

        self.assertEqual(len(report.confirmed_roots), 2)
        self.assertEqual(report.co_roots, ())
        self.assertEqual(
            {
                RootConfirmation.from_dict(dict(root.confirmation))
                .seed_binding_identity
                for root in report.confirmed_roots
            },
            {
                confirmation.seed_binding_identity
                for confirmation in report.confirmations
            },
        )
        self.assertEqual(
            [root.node_ref for root in report.confirmed_roots],
            ["record:shared_root", "record:shared_root"],
        )

    def test_one_seed_with_reciprocal_roots_publishes_primary_and_co_root(self):
        _, _, report = _run_co_roots()
        primary = RootConfirmation.from_dict(
            dict(report.confirmed_roots[0].confirmation)
        )
        co_root = RootConfirmation.from_dict(dict(report.co_roots[0].confirmation))

        self.assertEqual(len(report.confirmed_roots), 1)
        self.assertEqual(len(report.co_roots), 1)
        self.assertEqual(
            primary.seed_binding_identity,
            co_root.seed_binding_identity,
        )
        self.assertEqual(
            {report.confirmed_roots[0].node_ref, report.co_roots[0].node_ref},
            {"record:decision", "record:context"},
        )

    def test_resume_preserves_cross_seed_primaries_and_same_seed_co_roots(self):
        cases = (
            (
                "cross-seed",
                two_hop_shared_root_trace(),
                ("record:seed_one", "record:seed_two"),
                "Confirm each identical claim through the shared causal path.",
                TwoHopSharedRootJudge,
                None,
                (2, 0),
            ),
            (
                "same-seed-co-root",
                observed_trace(branching=True),
                ("record:observed_defect",),
                "Find every necessary cause.",
                _co_root_judge,
                PERSPECTIVE,
                (1, 1),
            ),
        )
        for (
            label,
            trace,
            start_refs,
            objective,
            judge_factory,
            perspective,
            expected_counts,
        ) in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                graph = TraceGraph.from_trace(trace)
                config = shared_root_checkpoint_config(
                    trace,
                    objective,
                    start_refs,
                    fusion_mode="off",
                )
                if perspective:
                    config = {
                        **config,
                        "analysis_perspective": perspective,
                    }
                    config["config_fingerprint"] = _sha256(
                        {
                            key: value
                            for key, value in config.items()
                            if key != "config_fingerprint"
                        }
                    )
                root = Path(directory) / "{0}.checkpoint".format(label)

                def analyze(judge, checkpoint):
                    return AgenticRecursiveAnalyzer(
                        judge=judge,
                        checkpoint=checkpoint,
                        checkpoint_config=config,
                    ).analyze(
                        graph,
                        start_refs=start_refs,
                        objective=objective,
                        analysis_perspective=perspective or "",
                    )

                uninterrupted = AgenticRecursiveAnalyzer(
                    judge=judge_factory()
                ).analyze(
                    graph,
                    start_refs=start_refs,
                    objective=objective,
                    analysis_perspective=perspective or "",
                )
                with self.assertRaises(KeyboardInterrupt):
                    analyze(
                        judge_factory(),
                        CrashAfterFirstCompletedConfirmation(root),
                    )
                resumed = analyze(judge_factory(), CheckpointBundle(root))

                self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
                self.assertEqual(
                    (
                        len(resumed.confirmed_roots),
                        len(resumed.co_roots),
                    ),
                    expected_counts,
                )
                restored_state = RecursiveAnalysisState.from_checkpoint(
                    graph=graph,
                    checkpoint=CheckpointBundle(root).restore(
                        expected_config=config
                    ),
                )
                self.assertEqual(
                    (
                        len(restored_state.confirmed_roots),
                        len(restored_state.co_roots),
                    ),
                    expected_counts,
                )

    def test_restore_and_evaluator_reject_cross_seed_primary_as_co_root(self):
        _, graph, report = _run_shared_root()
        control = report.to_dict()
        RecursiveAttributionReport.from_dict(control)
        self.assertTrue(_evaluate(graph, control)["safety"]["passed"])

        tampered = copy.deepcopy(control)
        tampered["co_roots"].append(tampered["confirmed_roots"].pop())
        with self.assertRaisesRegex(ValueError, "primary|co-root|seed"):
            RecursiveAttributionReport.from_dict(tampered)
        with self.assertRaises(EvaluationSafetyError):
            _evaluate(graph, tampered)

    def test_restore_and_evaluator_reject_same_seed_primary_co_root_swap(self):
        _, graph, report = _run_co_roots()
        tampered = report.to_dict()
        tampered["confirmed_roots"], tampered["co_roots"] = (
            tampered["co_roots"],
            tampered["confirmed_roots"],
        )
        _refresh_legacy_root_order(tampered)

        with self.assertRaisesRegex(
            ValueError,
            "canonical|primary|rank|order|perspective|response projection",
        ):
            RecursiveAttributionReport.from_dict(tampered)
        with self.assertRaises(EvaluationSafetyError):
            _evaluate(graph, tampered)

    def test_restore_and_evaluator_reject_cross_seed_primary_order_drift(self):
        _, graph, report = _run_shared_root()
        tampered = report.to_dict()
        tampered["confirmed_roots"].reverse()
        _refresh_legacy_root_order(tampered)

        with self.assertRaisesRegex(ValueError, "canonical|primary|rank|order"):
            RecursiveAttributionReport.from_dict(tampered)
        with self.assertRaises(EvaluationSafetyError):
            _evaluate(graph, tampered)

    def test_restore_rejects_coordinated_perspective_and_role_drift(self):
        _, _, report = _run_co_roots()
        tampered = report.to_dict()
        tampered["analysis_perspective"] = "compatibility"
        tampered["confirmed_roots"], tampered["co_roots"] = (
            tampered["co_roots"],
            tampered["confirmed_roots"],
        )
        _refresh_legacy_root_order(tampered)

        with self.assertRaisesRegex(
            ValueError,
            "canonical|primary|rank|order|perspective|response projection",
        ):
            RecursiveAttributionReport.from_dict(tampered)

    def test_hydrated_artifact_content_cannot_change_primary_role(self):
        _, _, report = _run_co_roots()
        payload = report.to_dict()
        artifact = {
            "artifact_id": "untrusted-ranking-context",
            "content_hash": "sha256:{0}".format(
                hashlib.sha256(PERSPECTIVE.encode("utf-8")).hexdigest()
            ),
            "content": PERSPECTIVE,
        }
        for section in ("causal_candidates", "introduction_candidates"):
            for candidate in payload[section]:
                if candidate["ref"] == "record:context":
                    candidate["node"]["data"]["hydrated_artifacts"] = [
                        copy.deepcopy(artifact)
                    ]
        restored = RecursiveAttributionReport.from_dict(payload)
        self.assertEqual(
            [root.node_ref for root in restored.confirmed_roots],
            ["record:decision"],
        )
        self.assertEqual(
            [root.node_ref for root in restored.co_roots],
            ["record:context"],
        )

    def test_checkpoint_restore_rejects_cross_seed_role_and_root_field_drift(self):
        trace = shared_root_trace()
        graph = TraceGraph.from_trace(trace)
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Confirm each identical claim independently."
        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fix39-checkpoint-tamper"
            _run_shared_root(
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            )
            checkpoint = CheckpointBundle(root).restore(
                expected_config=config
            )

            for label, mutate in (
                (
                    "cross-seed role",
                    lambda payload: payload["co_roots"].append(
                        payload["confirmed_roots"].pop()
                    ),
                ),
                (
                    "component",
                    lambda payload: payload["confirmed_roots"][0].__setitem__(
                        "component", "forged.component"
                    ),
                ),
                (
                    "provenance",
                    lambda payload: payload["confirmed_roots"][0].__setitem__(
                        "provenance",
                        {
                            **payload["confirmed_roots"][0]["provenance"],
                            "publication_contract": "forged",
                        },
                    ),
                ),
            ):
                with self.subTest(label=label):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    mutate(_latest_state_snapshot(actions))
                    tampered = replace(checkpoint, actions=tuple(actions))
                    with self.assertRaisesRegex(
                        ValueError,
                        "canonical|primary|co-root|publication|provenance|component|seed",
                    ):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=graph,
                            checkpoint=tampered,
                        )


class CanonicalPublicationProjectionTest(unittest.TestCase):
    def test_root_fields_are_canonical_under_coordinated_modern_legacy_edits(self):
        _, graph, report = _run_shared_root()
        control = report.to_dict()
        self.assertTrue(_evaluate(graph, control)["safety"]["passed"])
        other_seed = next(
            seed.start_ref
            for seed in report.seed_results
            if seed.start_ref
            != report.confirmed_roots[0].observed_defect_refs[0]
        )
        mutations = {
            "causal_role": "unrelated",
            "component": "forged.component",
            "event_type": "forged.event",
            "defect_type": "forged_defect",
            "episode_id": "episode:forged",
            "episode_member_refs": ["record:shared_root"],
            "observed_defect_refs": [
                report.confirmed_roots[0].observed_defect_refs[0],
                other_seed,
            ],
            "provenance": {
                "publication_contract": CAUSAL_PUBLICATION_CONTRACT_VERSION,
                "confirmation_identity": "forged",
            },
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                tampered = _coordinated_root_mutation(
                    control,
                    field,
                    value,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "canonical|publication|projection|root|seed|episode",
                ):
                    RecursiveAttributionReport.from_dict(tampered)
                with self.assertRaises(EvaluationSafetyError):
                    _evaluate(graph, tampered)

    def test_graph_validation_rejects_coordinated_candidate_and_root_forgery(self):
        _, graph, report = _run_co_roots()
        tampered = report.to_dict()
        root = tampered["confirmed_roots"][0]
        root["component"] = "forged.component"
        legacy = next(
            item
            for item in tampered["root_causes"]
            if item["observed_defect_refs"] == root["observed_defect_refs"]
        )
        legacy["component"] = "forged.component"
        for candidate in (
            *tampered["causal_candidates"],
            *tampered["introduction_candidates"],
        ):
            if candidate["ref"] == root["node_ref"]:
                candidate["node"]["component"] = "forged.component"

        restored = RecursiveAttributionReport.from_dict(tampered)
        with self.assertRaisesRegex(ValueError, "canonical|component|publication"):
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix39 forged graph publication",
            )
        with self.assertRaises(EvaluationSafetyError):
            _evaluate(graph, tampered)

    def test_factor_label_and_provenance_are_canonical(self):
        graph, report = _run_non_root("contributing_condition")
        control = report.to_dict()
        self.assertEqual(len(control["contributing_conditions"]), 1)
        self.assertTrue(_evaluate(graph, control)["safety"]["passed"])
        for field, value in (
            ("factor_label", "FORGED ROOT-LIKE LABEL"),
            ("provenance", {"publication_contract": "forged"}),
        ):
            with self.subTest(field=field):
                tampered = copy.deepcopy(control)
                tampered["contributing_conditions"][0][field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "factor|condition|canonical|provenance|publication",
                ):
                    RecursiveAttributionReport.from_dict(tampered)
                with self.assertRaises(EvaluationSafetyError):
                    _evaluate(graph, tampered)

    def test_rejected_candidate_provenance_is_canonical(self):
        graph, report = _run_non_root("unrelated")
        control = report.to_dict()
        self.assertEqual(len(control["rejected_candidates"]), 1)
        self.assertTrue(_evaluate(graph, control)["safety"]["passed"])

        tampered = copy.deepcopy(control)
        tampered["rejected_candidates"][0]["provenance"] = {
            "publication_contract": "forged"
        }
        with self.assertRaisesRegex(
            ValueError,
            "rejected|canonical|provenance|publication",
        ):
            RecursiveAttributionReport.from_dict(tampered)
        with self.assertRaises(EvaluationSafetyError):
            _evaluate(graph, tampered)

    def test_valid_report_checkpoint_and_evaluator_controls_pass(self):
        trace = shared_root_trace()
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Confirm each identical claim independently."
        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as directory:
            checkpoint_root = Path(directory) / "control.checkpoint"
            _, graph, report = _run_shared_root(
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            )
            payload = json.loads(json.dumps(report.to_dict()))
            restored = RecursiveAttributionReport.from_dict(payload)
            checkpoint = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )
            state = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=checkpoint,
            )

            validate_recursive_report_against_graph(
                graph,
                restored,
                label="Fix39 valid report",
                action_records=checkpoint.actions,
            )
            self.assertEqual(len(state.confirmed_roots), 2)
            self.assertEqual(state.co_roots, [])
            self.assertTrue(_evaluate(graph, payload)["safety"]["passed"])


class Fix39PersistenceVersionTest(unittest.TestCase):
    def test_publication_contract_and_persisted_envelopes_are_versioned(self):
        self.assertEqual(
            CAUSAL_PUBLICATION_CONTRACT_VERSION,
            "causal-publication/v3",
        )
        self.assertIn(
            "published-root-projection/v3",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            CAUSAL_PUBLICATION_CONTRACT_VERSION,
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v23",
        )
        self.assertEqual(REPORT_SCHEMA_VERSION, MODERN_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v29",
        )
        self.assertEqual(
            OUTPUT_SCHEMA_VERSION,
            "recursive-attribution-output/v16",
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v26",
        )


if __name__ == "__main__":
    unittest.main()
