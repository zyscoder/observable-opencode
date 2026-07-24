from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX,
)
from trace_attribution.causal_state import (
    RecursiveAttributionReport,
    RootConfirmation,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _quarantine_stale_seed_report_payload,
    _confirmation_request_identity,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    STARTS,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    FailingGlobalJudge,
    RecursiveFallbackRootJudge,
    SelectiveGlobalJudge,
    checkpoint_for,
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    stale_seed_trace,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
)
from tools.trace_attribution.tests import (
    test_recursive_analyzer as recursive_test_support,
)


class FullFactualConfirmationRequestIdentityTest(unittest.TestCase):
    def test_every_judge_visible_fact_changes_one_versioned_request_hash(self):
        judge = SharedRootFusionJudge()
        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=(STARTS[0],),
            objective=OBJECTIVE,
            analysis_perspective="fix28-perspective",
        )
        request = judge.confirmation_requests[0]
        base_identity = _confirmation_request_identity(request)
        self.assertTrue(base_identity.startswith("confirmation_request:v2:"))

        mutations = {
            "defect": replace(
                request,
                defect_state=replace(
                    request.defect_state,
                    actual="{0} mutated".format(request.defect_state.actual),
                ),
            ),
            "path": replace(
                request,
                recursive_path=(*request.recursive_path, request.recursive_path[-1]),
            ),
            "path envelope": replace(
                request,
                recursive_path_references=(
                    *request.recursive_path_references,
                    {"resolved_ref": request.candidate_ref, "fact_kind": "mutated"},
                ),
            ),
            "candidate envelope": replace(
                request,
                candidate_reference={
                    **dict(request.candidate_reference),
                    "fix28_mutation": True,
                },
            ),
            "support": replace(
                request,
                supporting_evidence=(
                    *request.supporting_evidence,
                    {"resolved_ref": request.candidate_ref, "fact_kind": "mutated"},
                ),
            ),
            "opposition": replace(
                request,
                opposing_evidence=(
                    *request.opposing_evidence,
                    {"resolved_ref": request.candidate_ref, "fact_kind": "mutated"},
                ),
            ),
            "competitor": replace(
                request,
                competing_hypotheses=(
                    *request.competing_hypotheses,
                    {"hypothesis_id": "hyp:mutated"},
                ),
            ),
            "obligation": replace(
                request,
                task_obligations=(
                    *request.task_obligations,
                    {"source": "fix28", "text": "mutated obligation"},
                ),
            ),
            "perspective": replace(
                request,
                analysis_perspective="mutated-perspective",
            ),
            "hypothesis semantic hash": replace(
                request,
                hypothesis_semantic_hash="sha256:mutated",
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(field=label):
                self.assertNotEqual(
                    _confirmation_request_identity(mutated),
                    base_identity,
                )

    def test_queue_started_and_terminal_actions_share_full_request_hash(self):
        judge = SharedRootFusionJudge()
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            judge,
            starts=(STARTS[0],),
            fusion_mode="retrieval-global",
            name="fix28-full-request-identity",
        )
        self.addCleanup(tempdir.cleanup)
        request_identity = _confirmation_request_identity(
            judge.confirmation_requests[0]
        )
        queued = report.metadata["confirmation_queue"][0]
        started = next(
            item
            for item in checkpoint.actions
            if item.get("operation") == "confirmation_started"
        )
        terminal = next(
            item
            for item in checkpoint.actions
            if item.get("operation")
            in {"confirmation_completed", "confirmation_failed"}
        )
        self.assertEqual(queued["semantic_identity"], request_identity)
        self.assertEqual(started["semantic_key"], "confirmation:{0}".format(request_identity))
        self.assertEqual(started["payload"]["request_identity"], request_identity)
        self.assertEqual(terminal["semantic_key"], started["semantic_key"])
        self.assertEqual(
            terminal["payload"]["action_projection"]["request_identity"],
            request_identity,
        )

    def test_report_and_evaluator_reject_obligation_mutation(self):
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            starts=(STARTS[0],),
            fusion_mode="retrieval-global",
            name="fix28-request-mutation",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())
        payload = report.to_dict()
        payload["metadata"]["confirmation_queue"][0]["task_obligations"].append(
            {"source": "fix28", "text": "mutated after provider completion"}
        )
        restored = RecursiveAttributionReport.from_dict(payload)
        with self.assertRaisesRegex(
            ValueError,
            "request|identity|canonical|factual",
        ):
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="fix28 factual request mutation",
                action_records=checkpoint.actions,
            )
        with self.assertRaises(EvaluationSafetyError):
            compare_report(
                payload,
                permissive_labels(graph.case_id),
                None,
                graph=graph,
            )

    def test_report_rejects_rehashed_terminal_competitor_mutation(self):
        root_test = recursive_test_support.RecursiveRootRankingTest()
        judge = recursive_test_support.ConfirmingScriptedJudge(
            {
                "record:change": recursive_test_support.step(
                    "record:change",
                    predecessors=(
                        recursive_test_support.relation(
                            "record:decision",
                            "same_defect_propagation",
                        ),
                        recursive_test_support.relation(
                            "record:context",
                            "same_defect_propagation",
                        ),
                    ),
                ),
                "record:decision": root_test._confirmation_step,
                "record:context": root_test._confirmation_step,
            },
            {
                "record:context": RootConfirmation.rejected(
                    "record:context",
                    "Availability did not introduce the defect.",
                    factor_role="contributing_condition",
                ),
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt="Implement only the methods found in the first search.",
                    reason="The decision stopped repository discovery.",
                    counterfactual="A complete search prevents the omission.",
                    confidence=0.91,
                    evidence_refs=("record:decision",),
                ),
            },
        )
        trace = recursive_test_support.observed_trace(branching=True)
        graph = TraceGraph.from_trace(trace)
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=("record:observed_defect",),
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Fix28 report validation.",
        )
        payload = report.to_dict()
        queued = next(
            item
            for item in payload["metadata"]["confirmation_queue"]
            if item["candidate_ref"] == "record:decision"
        )
        queued["factual_request_projection"]["facts"][
            "competing_hypotheses"
        ][0]["claim"] = "mutated terminal competitor claim"
        old_identity = queued["semantic_identity"]
        new_identity = "{0}{1}".format(
            ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX,
            hashlib.sha256(
                stable_json(
                    queued["factual_request_projection"]
                ).encode("utf-8")
            ).hexdigest(),
        )
        queued["semantic_identity"] = new_identity
        for entry in payload["metadata"]["confirmation_journal"]:
            if entry.get("semantic_identity") == old_identity:
                entry["semantic_identity"] = new_identity
                entry["semantic_key"] = "confirmation:{0}".format(
                    new_identity
                )
        for projection in payload["metadata"][
            "confirmation_action_projection"
        ]:
            if projection.get("request_identity") == old_identity:
                projection["request_identity"] = new_identity
                projection["semantic_key"] = "confirmation:{0}".format(
                    new_identity
                )

        with self.assertRaisesRegex(
            ValueError,
            "projection|request|identity|competitor",
        ):
            validate_recursive_report_against_graph(
                graph,
                RecursiveAttributionReport.from_dict(payload),
                label="fix28 terminal competitor mutation",
            )

    def test_report_rejects_parallel_terminal_identity_field(self):
        tempdir, _, _, _, report = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            starts=(STARTS[0],),
            fusion_mode="retrieval-global",
            name="fix28-parallel-terminal-identity",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())
        payload = report.to_dict()
        payload["metadata"]["confirmation_queue"][0][
            "legacy_request_identity"
        ] = "parallel-identity-must-not-survive"

        with self.assertRaisesRegex(
            ValueError,
            "schema|extra|identity",
        ):
            validate_recursive_report_against_graph(
                graph,
                RecursiveAttributionReport.from_dict(payload),
                label="fix28 parallel terminal identity",
            )

    def test_started_replay_rejects_extra_parallel_identity(self):
        (
            tempdir,
            _,
            _,
            checkpoint,
            _,
        ) = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            starts=(STARTS[0],),
            fusion_mode="retrieval-global",
            name="fix28-started-action-schema",
        )
        self.addCleanup(tempdir.cleanup)
        actions = copy.deepcopy(list(checkpoint.actions))
        started_index = next(
            index
            for index, item in enumerate(actions)
            if item.get("operation") == "confirmation_started"
        )
        actions = actions[: started_index + 1]
        actions[-1]["payload"]["legacy_request_identity"] = (
            "parallel-identity-must-not-authorize-replay"
        )
        snapshot_transaction = max(
            item["transaction_sequence"]
            for item in actions
            if item.get("operation") == "state_snapshot"
        )
        partial = replace(
            checkpoint,
            transaction_sequence=actions[-1]["transaction_sequence"],
            frontier_records=tuple(
                item
                for item in checkpoint.frontier_records
                if item["transaction_sequence"] <= snapshot_transaction
            ),
            hypothesis_records=tuple(
                item
                for item in checkpoint.hypothesis_records
                if item["transaction_sequence"] <= snapshot_transaction
            ),
            actions=tuple(actions),
        )
        graph = TraceGraph.from_trace(shared_root_trace())
        state = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=partial,
        )

        with self.assertRaisesRegex(
            ValueError,
            "started|schema|extra|identity",
        ):
            AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
            )._confirm_queued_roots(state)

    def test_bit_identical_checkpoint_replays_without_confirmation_provider(self):
        judge = SharedRootFusionJudge()
        tempdir, checkpoint_root, config, _, report = checkpoint_for(
            shared_root_trace(),
            judge,
            starts=(STARTS[0],),
            fusion_mode="retrieval-global",
            name="fix28-identical-replay",
        )
        self.addCleanup(tempdir.cleanup)

        class NoConfirmationReplayJudge(SharedRootFusionJudge):
            def confirm_candidate_offline(self, request):
                raise AssertionError(
                    "bit-identical request must replay without provider call"
                )

        replayed = AgenticRecursiveAnalyzer(
            judge=NoConfirmationReplayJudge(),
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=(STARTS[0],),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        self.assertEqual(replayed.to_dict(), report.to_dict())


class ExactGlobalTerminalRecordClassificationTest(unittest.TestCase):
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
            name="fix28-global-classification",
        )
        cls.graph = TraceGraph.from_trace(shared_root_trace())

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    @staticmethod
    def failed_event(snapshot):
        return next(
            item
            for item in snapshot["investigation_journal"]
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        )

    @staticmethod
    def failed_episode(snapshot):
        return next(
            item
            for item in snapshot["unresolved_branches"]
            if item.get("global_pass_identity")
        )

    def assert_checkpoint_rejects(self, mutation):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        mutation(latest_snapshot(actions))
        with self.assertRaisesRegex(
            ValueError,
            "global|pass|failure|episode|schema|status|marker|field",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(self.checkpoint, actions),
            )

    def test_checkpoint_classifies_all_marked_records_before_bijection(self):
        def unknown_status(snapshot):
            event = copy.deepcopy(self.failed_event(snapshot))
            event["status"] = "retrying"
            snapshot["investigation_journal"].append(event)

        def marker_only(snapshot):
            snapshot["investigation_journal"].append(
                {
                    "status": "failed",
                    "pass_identity": self.failed_event(snapshot)[
                        "pass_identity"
                    ],
                }
            )

        def extra_meaning(snapshot):
            self.failed_event(snapshot)["retryable"] = True

        def omitted_projection_episode(snapshot):
            episode = copy.deepcopy(self.failed_episode(snapshot))
            episode.pop("failure_projection")
            snapshot["unresolved_branches"].append(episode)

        for mutation in (
            unknown_status,
            marker_only,
            extra_meaning,
            omitted_projection_episode,
        ):
            with self.subTest(mutation=mutation.__name__):
                self.assert_checkpoint_rejects(mutation)

    def test_report_and_evaluator_reject_extra_unprojected_episode(self):
        payload = self.report.to_dict()
        episode = copy.deepcopy(
            next(
                item
                for item in payload["metadata"]["unresolved_branches"]
                if item.get("global_pass_identity")
            )
        )
        episode.pop("failure_projection")
        payload["metadata"]["unresolved_branches"].append(episode)
        restored = RecursiveAttributionReport.from_dict(payload)
        with self.assertRaisesRegex(
            ValueError,
            "global|failure|episode|schema|projection",
        ):
            validate_recursive_report_against_graph(
                self.graph,
                restored,
                label="fix28 unprojected global failure episode",
                action_records=self.checkpoint.actions,
            )
        with self.assertRaises(EvaluationSafetyError):
            compare_report(
                payload,
                permissive_labels(self.graph.case_id),
                None,
                graph=self.graph,
            )

    def test_stale_seed_quarantine_validates_before_filtering(self):
        payload = self.report.to_dict()
        stale_pass = copy.deepcopy(
            next(
                item
                for item in payload["investigation_journal"]
                if item.get("seed_ref") == "record:seed_one"
            )
        )
        stale_pass["status"] = "retrying"
        payload["investigation_journal"].append(stale_pass)
        with self.assertRaisesRegex(
            ValueError,
            "global|pass|schema|status",
        ):
            _quarantine_stale_seed_report_payload(
                TraceGraph.from_trace(stale_seed_trace(shared_root_trace())),
                payload,
            )


class ImmediateTypedGlobalFailureCheckpointTest(unittest.TestCase):
    def run_failure_with_checkpoint(self, judge, name, *, patcher=None):
        trace = shared_root_trace()
        starts = (STARTS[0],)
        config = shared_root_checkpoint_config(trace, OBJECTIVE, starts)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name) / "{0}.checkpoint".format(name)
        analyzer = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(root),
            checkpoint_config=config,
        )
        if patcher is None:
            analyzer.analyze(
                TraceGraph.from_trace(trace),
                start_refs=starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
        else:
            with patcher:
                analyzer.analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=starts,
                    objective=OBJECTIVE,
                    analysis_perspective="",
                )
        checkpoint = CheckpointBundle(root).restore(expected_config=config)
        snapshot = latest_snapshot(checkpoint.actions)
        failure = next(
            item
            for item in snapshot["investigation_journal"]
            if item.get("status") == "failed"
        )
        self.assertTrue(
            any(
                item.get("operation") == "state_snapshot"
                and item.get("semantic_key")
                == "global:failed:{0}".format(failure["pass_identity"])
                for item in checkpoint.actions
            )
        )

    def test_every_global_failure_path_writes_terminal_checkpoint(self):
        class GenericFailureJudge(SharedRootFusionJudge):
            def judge_candidates_bounded(
                self, request, *, max_physical_requests
            ):
                raise RuntimeError("fix28 generic provider failure")

        cases = (
            ("missing-capability", RecursiveFallbackRootJudge(), None),
            (
                "capsule-validation",
                SharedRootFusionJudge(),
                patch(
                    "trace_attribution.recursive_analyzer.build_candidate_evidence_capsules",
                    side_effect=ValueError("fix28 capsule failure"),
                ),
            ),
            (
                "request-validation",
                SharedRootFusionJudge(),
                patch(
                    "trace_attribution.recursive_analyzer.validate_global_candidate_request_against_graph",
                    side_effect=ValueError("fix28 request failure"),
                ),
            ),
            ("typed-provider", FailingGlobalJudge("bounded"), None),
            ("generic-provider", GenericFailureJudge(), None),
            ("invalid-output", FailingGlobalJudge("invalid"), None),
        )
        for name, judge, patcher in cases:
            with self.subTest(path=name):
                self.run_failure_with_checkpoint(
                    judge,
                    "fix28-{0}".format(name),
                    patcher=patcher,
                )

    def test_crash_after_failure_checkpoint_restores_terminal_seed_and_counts(self):
        class OneRequestSelectiveJudge(SharedRootFusionJudge):
            def __init__(self):
                super().__init__()
                self.seed_calls = []

            def judge_candidates_bounded(
                self, request, *, max_physical_requests
            ):
                self.seed_calls.append(request.seed_ref)
                if request.seed_ref == "record:seed_one":
                    raise BoundedJudgeCallError(
                        "fix28 typed seed failure",
                        physical_requests=1,
                    )
                return super().judge_candidates_bounded(
                    request,
                    max_physical_requests=max_physical_requests,
                )

        class CrashAfterFailureCheckpointAnalyzer(AgenticRecursiveAnalyzer):
            def _checkpoint_state(self, state, semantic_key):
                super()._checkpoint_state(state, semantic_key)
                if semantic_key.startswith("global:failed:"):
                    raise RuntimeError("fix28 crash after terminal checkpoint")

        trace = shared_root_trace()
        config = shared_root_checkpoint_config(trace, OBJECTIVE, STARTS)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name) / "fix28-crash.checkpoint"
        first_judge = OneRequestSelectiveJudge()
        with self.assertRaisesRegex(
            RuntimeError,
            "crash after terminal checkpoint",
        ):
            CrashAfterFailureCheckpointAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=STARTS,
                objective=OBJECTIVE,
                analysis_perspective="",
            )

        checkpoint = CheckpointBundle(root).restore(expected_config=config)
        snapshot = latest_snapshot(checkpoint.actions)
        failure = next(
            item
            for item in snapshot["investigation_journal"]
            if item.get("status") == "failed"
        )
        episode = next(
            item
            for item in snapshot["unresolved_branches"]
            if item.get("global_pass_identity")
        )
        seed = next(
            item
            for item in snapshot["seed_ledger"]
            if item["start_ref"] == "record:seed_one"
        )
        self.assertEqual(snapshot["judge_requests"], 1)
        self.assertEqual(failure["physical_request_delta"], 1)
        self.assertEqual(
            failure["failure_projection"],
            episode["failure_projection"],
        )
        self.assertEqual(
            seed["blocking_reasons"],
            [failure["blocker"]],
        )
        self.assertEqual(
            seed["missing_evidence"],
            failure["missing_evidence"],
        )

        replay_judge = OneRequestSelectiveJudge()
        report = AgenticRecursiveAnalyzer(
            judge=replay_judge,
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(root),
            checkpoint_config=config,
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        self.assertNotIn("record:seed_one", replay_judge.seed_calls)
        self.assertIn("record:seed_two", replay_judge.seed_calls)
        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(by_ref["record:seed_one"].outcome, "evidence_gap")
        self.assertNotEqual(by_ref["record:seed_two"].outcome, "evidence_gap")
        self.assertEqual(report.metadata["physical_judge_request_count"], 1)


if __name__ == "__main__":
    unittest.main()
