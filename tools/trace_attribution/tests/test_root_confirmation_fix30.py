from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    REPORT_SCHEMA_VERSION as EVALUATOR_REPORT_SCHEMA_VERSION,
    compare_report,
)
from trace_attribution.causal_state import (
    LocalStateOwner,
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    RecursiveAttributionReport,
    annotate_report_semantic_anchors,
    seed_binding_identity_for,
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
    ArtifactConfirmationJudge,
    artifact_state,
    stale_seed_trace,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    SelectiveGlobalJudge,
    checkpoint_for,
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix29 import (
    artifact_checkpoint_config,
    multi_owner_artifact_graph,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_trace,
)


EXPLICIT_GLOBAL_MARKERS = frozenset(
    {
        "kind",
        "global_pass_identity",
        "pass_identity",
        "failure_projection",
        "terminal_status",
        "seed_binding_identity",
    }
)


def seed_authority(seed_results):
    output = {}
    for seed in seed_results:
        seed_ref = str(seed.get("start_ref") or "")
        fingerprint = str(seed.get("defect_fingerprint") or "")
        binding = seed_binding_identity_for(seed_ref, fingerprint)
        output[binding] = {
            "seed_ref": seed_ref,
            "defect_fingerprint": fingerprint,
            "defect_state_id": "defect:{0}".format(fingerprint),
        }
    return output


def substituted_owner(seed_binding_identity):
    return LocalStateOwner.create(
        seed_binding_identity=seed_binding_identity,
        hypothesis_id="hypothesis:fix30-substitution",
        visit_key="visit:fix30-substitution",
        occurrence_key="ordinary_investigation",
    ).to_dict()


def marker_stripped(record):
    value = copy.deepcopy(record)
    original_binding = str(
        value.get("seed_binding_identity")
        or value.get("failure_projection", {}).get(
            "seed_binding_identity"
        )
        or value.get("owner", {}).get("seed_binding_identity")
        or ""
    )
    for key in EXPLICIT_GLOBAL_MARKERS:
        value.pop(key, None)
    value["owner"] = substituted_owner(original_binding)
    return value


class LedgerAwareResidualGlobalSignatureTest(unittest.TestCase):
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
            name="fix30-residual-global",
        )
        cls.graph = TraceGraph.from_trace(shared_root_trace())
        cls.payload = cls.report.to_dict()
        cls.authority = seed_authority(cls.payload["seed_results"])

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def terminal_records(self, container):
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

    def test_classifier_rejects_marker_stripped_passes_and_episode(self):
        snapshot = latest_snapshot(list(self.checkpoint.actions))
        completed, failed, episode = self.terminal_records(snapshot)

        for label, classifier, record in (
            ("completed", _classify_global_pass_records, completed),
            ("failed", _classify_global_pass_records, failed),
            ("episode", _classify_global_failure_episodes, episode),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    classifier(
                        (marker_stripped(record),),
                        seed_authority=self.authority,
                    )

    def test_live_terminal_set_rejects_marker_stripped_passes(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                payload = copy.deepcopy(self.payload)
                event = next(
                    item
                    for item in payload["investigation_journal"]
                    if item.get("status") == status
                )
                state = RecursiveAnalysisState.create(
                    graph=self.graph,
                    start_refs=(event["seed_ref"],),
                    objective=OBJECTIVE,
                    analysis_perspective="",
                )
                state.investigation_journal.append(
                    marker_stripped(event)
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    AgenticRecursiveAnalyzer(
                        judge=SharedRootFusionJudge(),
                        fusion_mode="retrieval-global",
                    )._run_global_candidate_prepass(state, self.graph)

    def test_checkpoint_rejects_every_marker_stripped_terminal_shape(self):
        for label in ("completed", "failed", "episode"):
            with self.subTest(label=label):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                snapshot = latest_snapshot(actions)
                completed, failed, episode = self.terminal_records(snapshot)
                target = {
                    "completed": completed,
                    "failed": failed,
                    "episode": episode,
                }[label]
                original = copy.deepcopy(target)
                target.clear()
                target.update(marker_stripped(original))

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

    def test_report_and_evaluator_reject_every_marker_stripped_terminal_shape(self):
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
                completed, failed, episode = self.terminal_records(
                    container
                )
                target = {
                    "completed": completed,
                    "failed": failed,
                    "episode": episode,
                }[label]
                original = copy.deepcopy(target)
                target.clear()
                target.update(marker_stripped(original))

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
                            label="fix30 residual terminal report",
                            action_records=self.checkpoint.actions,
                        )
                with self.assertRaises(EvaluationSafetyError):
                    compare_report(
                        payload,
                        permissive_labels(self.graph.case_id),
                        None,
                        graph=self.graph,
                    )

    def test_stale_quarantine_rejects_every_marker_stripped_terminal_shape(self):
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
                completed, failed, episode = self.terminal_records(
                    container
                )
                target = {
                    "completed": completed,
                    "failed": failed,
                    "episode": episode,
                }[label]
                original = copy.deepcopy(target)
                target.clear()
                target.update(marker_stripped(original))

                with self.assertRaisesRegex(
                    ValueError,
                    "global|residual|terminal|owner|schema",
                ):
                    _quarantine_stale_seed_report_payload(
                        stale_graph, payload
                    )

    def test_ordinary_investigation_and_unresolved_records_remain_non_global(self):
        binding = next(iter(self.authority))
        ordinary_owner = LocalStateOwner.create(
            seed_binding_identity=binding,
            hypothesis_id="hypothesis:ordinary-investigation",
            visit_key="visit:ordinary-investigation",
            occurrence_key="investigation_evidence:ordinary",
        ).to_dict()
        investigation = {
            "directive_kind": "evidence_investigation",
            "status": "completed",
            "active_visit": {
                "seed_binding_identity": binding,
                "hypothesis_id": "hypothesis:ordinary-investigation",
                "visit_key": "visit:ordinary-investigation",
            },
            "owner": ordinary_owner,
            "result": {"status": "completed", "byte_count": 12},
        }
        unresolved = {
            "node_ref": "record:ordinary_unresolved",
            "defect_state_id": "defect:ordinary",
            "hypothesis_id": "hypothesis:ordinary-investigation",
            "reason": "ordinary_evidence_gap",
            "details": "An ordinary branch remains unresolved.",
            "depth": 1,
            "owner": ordinary_owner,
        }

        self.assertEqual(
            _classify_global_pass_records(
                (investigation,),
                seed_authority=self.authority,
            ),
            ([], []),
        )
        self.assertEqual(
            _classify_global_failure_episodes(
                (unresolved,),
                seed_authority=self.authority,
            ),
            [],
        )


class CountingArtifactConfirmationJudge(ArtifactConfirmationJudge):
    def __init__(self):
        self.step_calls = 0
        self.confirmation_calls = 0

    def judge_step_offline(self, request):
        self.step_calls += 1
        return super().judge_step_offline(request)

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        return super().confirm_candidate_offline(request)


class PreflightArtifactMutationAnalyzer(AgenticRecursiveAnalyzer):
    def __init__(self, *, mutation, **kwargs):
        super().__init__(**kwargs)
        self.mutation = mutation
        self.enqueued_snapshots = {}

    def _confirm_queued_roots(self, state):
        for item in state.confirmation_queue:
            self.enqueued_snapshots[
                str(item.get("candidate_ref") or "")
            ] = copy.deepcopy(
                list(item.get("artifact_evidence_envelopes") or ())
            )
        self.mutation(state)
        return super()._confirm_queued_roots(state)


class CapturingConfirmationAnalyzer(AgenticRecursiveAnalyzer):
    def __init__(self, *, forbid_request_rebuild=False, **kwargs):
        super().__init__(**kwargs)
        self.forbid_request_rebuild = forbid_request_rebuild
        self.captured_actions = []

    def _build_confirmation_request(self, state, queued):
        if self.forbid_request_rebuild:
            raise AssertionError(
                "rejected snapshot replay rebuilt an ordinary request"
            )
        return super()._build_confirmation_request(state, queued)

    def _checkpoint_action(self, operation, semantic_key, payload):
        self.captured_actions.append(
            {
                "operation": operation,
                "semantic_key": semantic_key,
                "payload": copy.deepcopy(dict(payload)),
            }
        )


def artifact_mutation(mode, root):
    def mutate(state):
        if mode in {"owner_substitution", "stale_owner"}:
            state.graph._artifact_records["record:candidate"][
                "artifact_refs"
            ] = []
            return
        if mode == "hash_drift":
            state.graph._artifact_index["proof"]["hash"] = (
                "sha256:{0}".format("0" * 64)
            )
            return
        if mode == "path_drift":
            state.graph._artifact_index["proof"]["path"] = (
                "artifacts/moved-proof.txt"
            )
            return
        if mode == "content_drift":
            (root / "artifacts" / "proof.txt").write_bytes(
                b"drifted proof"
            )
            return
        if mode == "missing":
            (root / "artifacts" / "proof.txt").unlink()
            return
        raise AssertionError("unsupported mutation mode: {0}".format(mode))

    return mutate


class RejectedSnapshotTerminalDispositionTest(unittest.TestCase):
    def run_analysis(
        self,
        mode,
        *,
        record_ids=("candidate", "other_owner"),
        starts=("record:candidate",),
        name="artifact-gap",
    ):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        trace, _ = multi_owner_artifact_graph(
            root,
            record_ids=record_ids,
        )
        if mode == "stale_owner":
            other = next(
                item
                for item in trace["records"]
                if item["record_id"] == "other_owner"
            )
            other["data"]["revision_status"] = "stale"
        graph = TraceGraph.from_trace(trace, artifact_root=root)
        config = artifact_checkpoint_config(trace, graph, starts)
        checkpoint_root = root / "{0}.checkpoint".format(name)
        judge = CountingArtifactConfirmationJudge()
        analyzer = PreflightArtifactMutationAnalyzer(
            mutation=artifact_mutation(mode, root),
            judge=judge,
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        )
        report = analyzer.analyze(
            graph,
            start_refs=starts,
            objective="Confirm each explicitly owned artifact candidate.",
            analysis_perspective="",
        )
        checkpoint = CheckpointBundle(checkpoint_root).restore(
            expected_config=config
        )
        return (
            root,
            graph,
            config,
            checkpoint_root,
            checkpoint,
            report,
            judge,
            analyzer,
        )

    def assert_rejected_snapshot_round_trip(self, mode):
        (
            _,
            graph,
            config,
            checkpoint_root,
            checkpoint,
            report,
            judge,
            analyzer,
        ) = self.run_analysis(mode, name="rejected-{0}".format(mode))

        self.assertEqual(judge.confirmation_calls, 0)
        self.assertEqual(report.seed_results[0].outcome, "evidence_gap")
        self.assertEqual(len(report.confirmations), 1)
        confirmation = report.confirmations[0]
        self.assertEqual(confirmation.status, "unknown")
        self.assertEqual(confirmation.evidence_refs, ())
        self.assertFalse(report.seed_results[0].decisive_evidence_refs)

        payload = report.to_dict()
        queue = payload["metadata"]["confirmation_queue"]
        self.assertEqual(len(queue), 1)
        terminal = queue[0]
        disposition = terminal["evidence_disposition"]
        self.assertEqual(
            disposition["schema"],
            "root-confirmation-terminal-evidence-disposition/v1",
        )
        self.assertEqual(disposition["state"], "rejected_snapshot")
        self.assertTrue(disposition["rejection_reason"])
        self.assertTrue(disposition["graph_comparison_facts"])
        self.assertEqual(
            terminal["artifact_evidence_envelopes"],
            analyzer.enqueued_snapshots["record:candidate"],
        )
        self.assertEqual(
            terminal["confirmation"]["evidence_refs"],
            [],
        )

        terminal_actions = [
            item
            for item in checkpoint.actions
            if item.get("operation")
            in {"confirmation_completed", "confirmation_failed"}
        ]
        self.assertEqual(
            [item["operation"] for item in terminal_actions],
            ["confirmation_failed"],
        )
        action_projection = terminal_actions[0]["payload"][
            "action_projection"
        ]
        self.assertEqual(
            action_projection["artifact_evidence_envelopes"],
            analyzer.enqueued_snapshots["record:candidate"],
        )
        self.assertEqual(
            action_projection["evidence_disposition"],
            disposition,
        )
        self.assertEqual(
            disposition["snapshot_identity"],
            "terminal_evidence_snapshot:v1:{0}".format(
                hashlib.sha256(
                    stable_json(
                        action_projection[
                            "artifact_evidence_envelopes"
                        ]
                    ).encode("utf-8")
                ).hexdigest()
            ),
        )
        self.assertEqual(
            payload["metadata"]["confirmation_action_projection"][0][
                "evidence_disposition"
            ],
            disposition,
        )

        restored = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=partial_checkpoint(
                checkpoint, list(checkpoint.actions)
            ),
        )
        self.assertEqual(restored.confirmations[0].status, "unknown")
        self.assertEqual(restored.confirmations[0].evidence_refs, ())
        restored_report = RecursiveAttributionReport.from_dict(payload)
        validate_recursive_report_against_graph(
            graph,
            restored_report,
            label="fix30 rejected snapshot report",
            action_records=checkpoint.actions,
        )
        comparison = compare_report(
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
        self.assertTrue(comparison["safety"]["passed"])

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

    def test_preflight_artifact_gaps_are_round_trippable_seed_local_terminals(self):
        for mode in (
            "owner_substitution",
            "stale_owner",
            "hash_drift",
            "path_drift",
            "content_drift",
            "missing",
        ):
            with self.subTest(mode=mode):
                self.assert_rejected_snapshot_round_trip(mode)

    def test_rejected_snapshot_never_becomes_support_or_substantive_verdict(self):
        (
            _,
            _,
            _,
            _,
            checkpoint,
            report,
            _,
            _,
        ) = self.run_analysis("content_drift", name="no-support")
        payload = report.to_dict()
        projection = payload["metadata"][
            "confirmation_action_projection"
        ][0]

        self.assertEqual(
            projection["evidence_disposition"]["state"],
            "rejected_snapshot",
        )
        self.assertEqual(projection["confirmation"]["status"], "unknown")
        self.assertEqual(projection["evidence_refs"], [])
        self.assertEqual(
            [
                item["operation"]
                for item in checkpoint.actions
                if item.get("operation")
                in {
                    "confirmation_completed",
                    "confirmation_failed",
                }
            ],
            ["confirmation_failed"],
        )

    def test_durable_rejected_action_replays_before_preflight_or_request_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, failed_state, _, failed_queue = artifact_state(root)
            self.assertTrue(failed_state.enqueue_confirmation(failed_queue))
            (root / "artifacts" / "proof.txt").write_bytes(
                b"drifted proof"
            )
            first_judge = CountingArtifactConfirmationJudge()
            first_analyzer = CapturingConfirmationAnalyzer(
                judge=first_judge
            )
            first_analyzer._confirm_queued_roots(failed_state)
            failed_actions = [
                item
                for item in first_analyzer.captured_actions
                if item["operation"] == "confirmation_failed"
            ]
            self.assertEqual(len(failed_actions), 1)
            self.assertEqual(first_judge.confirmation_calls, 0)

            _, replay_state, _, replay_queue = artifact_state(root)
            self.assertTrue(replay_state.enqueue_confirmation(replay_queue))
            failed_action = failed_actions[0]
            replay_state.replay_actions = {
                failed_action["semantic_key"]: failed_action
            }
            replay_judge = CountingArtifactConfirmationJudge()
            replay_analyzer = CapturingConfirmationAnalyzer(
                judge=replay_judge,
                forbid_request_rebuild=True,
            )

            replay_analyzer._confirm_queued_roots(replay_state)

        self.assertEqual(replay_judge.confirmation_calls, 0)
        self.assertEqual(replay_analyzer.captured_actions, [])
        self.assertEqual(len(replay_state.confirmations), 1)
        self.assertEqual(replay_state.confirmations[0].status, "unknown")
        self.assertEqual(replay_state.confirmations[0].evidence_refs, ())
        self.assertEqual(
            replay_state.confirmation_action_projection[0][
                "evidence_disposition"
            ]["state"],
            "rejected_snapshot",
        )

    def test_artifact_gap_does_not_block_independent_second_seed(self):
        (
            _,
            _,
            _,
            _,
            checkpoint,
            report,
            judge,
            analyzer,
        ) = self.run_analysis(
            "owner_substitution",
            record_ids=("candidate", "candidate_two"),
            starts=("record:candidate", "record:candidate_two"),
            name="independent-seeds",
        )

        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(
            by_ref["record:candidate"].outcome,
            "evidence_gap",
        )
        self.assertEqual(
            by_ref["record:candidate_two"].outcome,
            "confirmed_root",
        )
        self.assertEqual(judge.confirmation_calls, 1)
        self.assertEqual(
            {
                root.node_ref for root in report.confirmed_roots
            },
            {"record:candidate_two"},
        )
        terminal_by_candidate = {
            item["payload"]["action_projection"]["candidate_ref"]: (
                item["operation"],
                item["payload"]["action_projection"][
                    "evidence_disposition"
                ]["state"],
            )
            for item in checkpoint.actions
            if item.get("operation")
            in {"confirmation_completed", "confirmation_failed"}
        }
        self.assertEqual(
            terminal_by_candidate,
            {
                "record:candidate": (
                    "confirmation_failed",
                    "rejected_snapshot",
                ),
                "record:candidate_two": (
                    "confirmation_completed",
                    "validated",
                ),
            },
        )
        self.assertEqual(
            analyzer.enqueued_snapshots["record:candidate"],
            report.to_dict()["metadata"]["confirmation_queue"][0][
                "artifact_evidence_envelopes"
            ],
        )


class Fix30PersistenceVersionTest(unittest.TestCase):
    def test_versions_name_changed_terminal_persistence_contracts(self):
        self.assertEqual(ACTION_STATE_SCHEMA, "recursive-analysis-actions/v13")
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v16",
        )
        self.assertEqual(
            EVALUATOR_REPORT_SCHEMA_VERSION,
            MODERN_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v15",
        )
        self.assertIn(
            "recursive-root-confirmation/v17",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "terminal-evidence/v2",
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


if __name__ == "__main__":
    unittest.main()
