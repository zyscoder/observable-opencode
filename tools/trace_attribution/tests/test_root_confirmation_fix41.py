from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_judge import BoundedJudgeCallError
from trace_attribution.causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
)


START_REF = "record:seed_one"
OBJECTIVE = "Confirm the unsupported response claim from trace evidence."


class CrashAfterGlobalAction(CheckpointBundle):
    def __init__(self, root: Path, operation: str) -> None:
        super().__init__(root)
        self.operation = operation
        self.triggered = False

    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == self.operation and not self.triggered:
            self.triggered = True
            raise KeyboardInterrupt(
                "fix41 crash after durable {0}".format(operation)
            )
        return record


class CountingGlobalJudge(SharedRootFusionJudge):
    def __init__(
        self,
        *,
        fail_exact: bool = False,
        fail_generic: bool = False,
        forbid_global: bool = False,
        exact_failure_requests: int = 1,
    ):
        super().__init__()
        self.fail_exact = fail_exact
        self.fail_generic = fail_generic
        self.forbid_global = forbid_global
        self.exact_failure_requests = exact_failure_requests
        self.global_calls = 0

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_calls += 1
        if self.forbid_global:
            raise AssertionError("resumed global Judge must not be called")
        if self.fail_exact:
            raise BoundedJudgeCallError(
                "fix41 exact global failure",
                physical_requests=self.exact_failure_requests,
            )
        if self.fail_generic:
            raise RuntimeError("fix41 inexact global failure")
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class InjectedReplayCheckpoint:
    def __init__(self, state, root: Path) -> None:
        self.state = state
        self.output_commit_path = root / "not-published"

    def initialize(self, config):
        del config

    def restore(self, *, expected_config):
        del expected_config
        return self.state

    def record_action(self, operation, semantic_key, payload):
        return {
            "operation": operation,
            "semantic_key": semantic_key,
            "payload": copy.deepcopy(dict(payload)),
        }

    def commit_snapshot(self, **kwargs):
        return copy.deepcopy(kwargs)

    def flush_all(self):
        return None


class ResumableGlobalJudgeLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.resources = tempfile.TemporaryDirectory()
        self.addCleanup(self.resources.cleanup)
        self.root = Path(self.resources.name) / "fix41.checkpoint"
        self.trace = shared_root_trace()
        self.graph = TraceGraph.from_trace(self.trace)
        self.starts = (START_REF,)
        self.config = shared_root_checkpoint_config(
            self.trace,
            OBJECTIVE,
            self.starts,
        )

    def analyze(self, judge, checkpoint):
        return AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            checkpoint=checkpoint,
            checkpoint_config=self.config,
        ).analyze(
            self.graph,
            start_refs=self.starts,
            objective=OBJECTIVE,
            analysis_perspective="",
        )

    def config_with(self, *, max_judge_requests=128, cache_identity="cache:test"):
        config = copy.deepcopy(self.config)
        config["budgets"]["max_judge_requests"] = max_judge_requests
        config["cache_identity"] = cache_identity
        config["config_fingerprint"] = hashlib.sha256(
            stable_json(
                {
                    key: value
                    for key, value in config.items()
                    if key != "config_fingerprint"
                }
            ).encode("utf-8")
        ).hexdigest()
        return config

    def test_completed_action_replays_without_duplicate_request_or_result(self):
        uninterrupted = AgenticRecursiveAnalyzer(
            judge=CountingGlobalJudge(),
            fusion_mode="retrieval-global",
        ).analyze(
            self.graph,
            start_refs=self.starts,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        first_judge = CountingGlobalJudge()
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "global_judge_completed",
        ):
            self.analyze(
                first_judge,
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_completed",
                ),
            )
        self.assertEqual(first_judge.global_calls, 1)

        replay_judge = CountingGlobalJudge(forbid_global=True)
        resumed = self.analyze(
            replay_judge,
            CheckpointBundle(self.root),
        )

        self.assertEqual(replay_judge.global_calls, 0)
        self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
        completed = [
            item
            for item in resumed.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "completed"
        ]
        self.assertEqual(len(completed), 1)

    def test_started_action_becomes_interrupted_gap_without_retry(self):
        first_judge = CountingGlobalJudge()
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "global_judge_started",
        ):
            self.analyze(
                first_judge,
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_started",
                ),
            )
        self.assertEqual(first_judge.global_calls, 0)

        replay_judge = CountingGlobalJudge(forbid_global=True)
        report = self.analyze(
            replay_judge,
            CheckpointBundle(self.root),
        )

        self.assertEqual(replay_judge.global_calls, 0)
        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "execution_failed")
        self.assertEqual(seed.blocking_reasons, ())
        self.assertEqual(
            seed.execution_failures[0]["reason"],
            "analysis_interrupted",
        )
        failure = next(
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        )
        self.assertFalse(failure["physical_request_exact"])

    def test_exact_failed_action_replays_without_duplicate_request_or_cost(self):
        first_judge = CountingGlobalJudge(fail_exact=True)
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "global_judge_failed",
        ):
            self.analyze(
                first_judge,
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_failed",
                ),
            )
        self.assertEqual(first_judge.global_calls, 1)

        replay_judge = CountingGlobalJudge(forbid_global=True)
        report = self.analyze(
            replay_judge,
            CheckpointBundle(self.root),
        )

        self.assertEqual(replay_judge.global_calls, 0)
        self.assertEqual(
            report.metadata["physical_judge_request_count"],
            1,
        )
        failure = next(
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        )
        self.assertEqual(failure["physical_request_delta"], 1)
        self.assertTrue(failure["physical_request_exact"])

    def test_completed_replay_rejects_every_bound_action_field_before_call(self):
        with self.assertRaises(KeyboardInterrupt):
            self.analyze(
                CountingGlobalJudge(),
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_completed",
                ),
            )
        checkpoint = CheckpointBundle(self.root).restore(
            expected_config=self.config
        )
        completed_index = next(
            index
            for index, action in enumerate(checkpoint.actions)
            if action["operation"] == "global_judge_completed"
        )

        def mutate_request_identity(action):
            action["payload"]["request_identity"] = "global_request:v1:tampered"

        def mutate_envelope(action):
            action["payload"]["validation_envelope"]["objective"] = "tampered"

        def mutate_capsules(action):
            action["payload"]["capsule_identity"] = "global_capsules:v1:tampered"

        def mutate_owner(action):
            action["payload"]["owner"]["visit_key"] = "tampered"

        def mutate_accounting(action):
            action["payload"]["physical_request_delta"] = (
                action["payload"]["physical_requests_reserved"] + 1
            )

        def mutate_reserved_budget(action):
            action["payload"]["physical_requests_reserved"] += 1

        def mutate_judgment(action):
            action["payload"]["judgment"]["active_focus_binding"][
                "seed_ref"
            ] = "record:tampered"

        def mutate_provider_state(action):
            action["payload"]["provider_state"]["identity"] = "tampered"

        mutations = {
            "request identity": mutate_request_identity,
            "validation envelope": mutate_envelope,
            "capsule identity": mutate_capsules,
            "owner": mutate_owner,
            "accounting": mutate_accounting,
            "reserved budget": mutate_reserved_budget,
            "judgment": mutate_judgment,
            "provider state": mutate_provider_state,
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                actions = copy.deepcopy(list(checkpoint.actions))
                mutation(actions[completed_index])
                tampered = replace(checkpoint, actions=tuple(actions))
                replay_judge = CountingGlobalJudge(forbid_global=True)
                with self.assertRaises((TypeError, ValueError)):
                    self.analyze(
                        replay_judge,
                        InjectedReplayCheckpoint(tampered, self.root),
                    )
                self.assertEqual(replay_judge.global_calls, 0)

    def test_applied_snapshot_still_rejects_tampered_terminal_action(self):
        self.analyze(
            CountingGlobalJudge(),
            CheckpointBundle(self.root),
        )
        checkpoint = CheckpointBundle(self.root).restore(
            expected_config=self.config
        )
        actions = copy.deepcopy(list(checkpoint.actions))
        completed = next(
            action
            for action in actions
            if action["operation"] == "global_judge_completed"
        )
        completed["payload"]["request_identity"] = (
            "global_request:v1:tampered"
        )
        tampered = replace(checkpoint, actions=tuple(actions))

        with self.assertRaises((TypeError, ValueError)):
            self.analyze(
                CountingGlobalJudge(forbid_global=True),
                InjectedReplayCheckpoint(tampered, self.root),
            )

    def test_applied_snapshot_rejects_coordinated_lifecycle_tampering(self):
        self.analyze(
            CountingGlobalJudge(),
            CheckpointBundle(self.root),
        )
        checkpoint = CheckpointBundle(self.root).restore(
            expected_config=self.config
        )

        def remove_started(actions):
            actions[:] = [
                action
                for action in actions
                if action["operation"] != "global_judge_started"
            ]

        def drift_both_reserved_values(actions):
            for action in actions:
                if action["operation"] in {
                    "global_judge_started",
                    "global_judge_completed",
                }:
                    action["payload"]["physical_requests_reserved"] += 17

        def resign_provider_accounting(actions):
            completed = next(
                action
                for action in actions
                if action["operation"] == "global_judge_completed"
            )
            provider = completed["payload"]["provider_state"]
            provider["accounting"]["logical_judge_calls"] += 10
            unsigned = {
                key: provider[key]
                for key in provider
                if key != "identity"
            }
            provider["identity"] = hashlib.sha256(
                stable_json(unsigned).encode("utf-8")
            ).hexdigest()

        for label, mutation in {
            "missing started": remove_started,
            "coordinated reserved": drift_both_reserved_values,
            "resigned provider accounting": resign_provider_accounting,
        }.items():
            with self.subTest(label=label):
                actions = copy.deepcopy(list(checkpoint.actions))
                mutation(actions)
                tampered = replace(checkpoint, actions=tuple(actions))
                with self.assertRaises((TypeError, ValueError)):
                    self.analyze(
                        CountingGlobalJudge(forbid_global=True),
                        InjectedReplayCheckpoint(tampered, self.root),
                    )

    def test_inexact_failure_requires_full_reservation_and_interrupted_blocker(self):
        with self.assertRaises(KeyboardInterrupt):
            self.analyze(
                CountingGlobalJudge(fail_generic=True),
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_failed",
                ),
            )
        checkpoint = CheckpointBundle(self.root).restore(
            expected_config=self.config
        )

        def resign_provider(provider):
            unsigned = {
                key: provider[key]
                for key in provider
                if key != "identity"
            }
            provider["identity"] = hashlib.sha256(
                stable_json(unsigned).encode("utf-8")
            ).hexdigest()

        def underreport_reserved_request(actions):
            failed = next(
                action
                for action in actions
                if action["operation"] == "global_judge_failed"
            )
            failed["payload"]["physical_request_delta"] = 1
            failed["payload"]["failure_projection"][
                "physical_request_delta"
            ] = 1
            provider = failed["payload"]["provider_state"]
            provider["accounting"]["judge_requests"] = 1
            resign_provider(provider)

        def replace_interrupted_blocker(actions):
            failed = next(
                action
                for action in actions
                if action["operation"] == "global_judge_failed"
            )
            failed["payload"]["blocker"] = "global_judge_bounded_failure"
            failed["payload"]["failure_projection"][
                "blocker"
            ] = "global_judge_bounded_failure"

        for label, mutation in {
            "underreported reservation": underreport_reserved_request,
            "non-interrupted blocker": replace_interrupted_blocker,
        }.items():
            with self.subTest(label=label):
                actions = copy.deepcopy(list(checkpoint.actions))
                mutation(actions)
                tampered = replace(checkpoint, actions=tuple(actions))
                with self.assertRaises((TypeError, ValueError)):
                    self.analyze(
                        CountingGlobalJudge(forbid_global=True),
                        InjectedReplayCheckpoint(tampered, self.root),
                    )

    def test_exact_failure_cannot_exceed_reserved_request_budget(self):
        config = self.config_with(max_judge_requests=1)
        analyzer = AgenticRecursiveAnalyzer(
            judge=CountingGlobalJudge(
                fail_exact=True,
                exact_failure_requests=10,
            ),
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(self.root),
            checkpoint_config=config,
            max_judge_requests=1,
        )
        with self.assertRaisesRegex(
            ValueError,
            "allowance|reserved|budget",
        ):
            analyzer.analyze(
                self.graph,
                start_refs=self.starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )

    def test_interrupted_reservation_blocks_later_seed_request(self):
        starts = ("record:seed_one", "record:seed_two")
        config = shared_root_checkpoint_config(
            self.trace,
            OBJECTIVE,
            starts,
        )
        config["budgets"]["max_judge_requests"] = 1
        config["config_fingerprint"] = hashlib.sha256(
            stable_json(
                {
                    key: value
                    for key, value in config.items()
                    if key != "config_fingerprint"
                }
            ).encode("utf-8")
        ).hexdigest()

        def run(judge, checkpoint):
            return AgenticRecursiveAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
                checkpoint=checkpoint,
                checkpoint_config=config,
                max_judge_requests=1,
            ).analyze(
                self.graph,
                start_refs=starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )

        with self.assertRaises(KeyboardInterrupt):
            run(
                CountingGlobalJudge(),
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_started",
                ),
            )
        replay_judge = CountingGlobalJudge(forbid_global=True)
        report = run(replay_judge, CheckpointBundle(self.root))

        self.assertEqual(replay_judge.global_calls, 0)
        self.assertEqual(
            report.metadata["physical_judge_request_count"],
            1,
        )
        self.assertEqual(
            report.metadata["judge_request_uncertainty_count"],
            1,
        )
        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(by_ref["record:seed_one"].outcome, "execution_failed")
        self.assertEqual(by_ref["record:seed_two"].outcome, "execution_failed")

    def test_checkpoint_budget_mismatch_is_rejected_before_lifecycle_write(self):
        with self.assertRaisesRegex(
            ValueError,
            "max_judge_requests|checkpoint.*budget",
        ):
            AgenticRecursiveAnalyzer(
                judge=CountingGlobalJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(self.root),
                checkpoint_config=self.config,
                max_judge_requests=1,
            ).analyze(
                self.graph,
                start_refs=self.starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
        self.assertFalse(
            (self.root / "investigation-actions.jsonl").exists()
        )

    def test_non_judge_budget_mismatch_is_rejected_before_lifecycle_write(self):
        with self.assertRaisesRegex(
            ValueError,
            "max_frontier_items|checkpoint.*budget",
        ):
            AgenticRecursiveAnalyzer(
                judge=CountingGlobalJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(self.root),
                checkpoint_config=self.config,
                max_frontier_items=0,
            ).analyze(
                self.graph,
                start_refs=self.starts,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
        self.assertFalse(
            (self.root / "investigation-actions.jsonl").exists()
        )

    def test_empty_cache_identity_remains_resumable(self):
        config = self.config_with(cache_identity="")

        first = AgenticRecursiveAnalyzer(
            judge=CountingGlobalJudge(),
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(self.root),
            checkpoint_config=config,
        ).analyze(
            self.graph,
            start_refs=self.starts,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        resumed = AgenticRecursiveAnalyzer(
            judge=CountingGlobalJudge(forbid_global=True),
            fusion_mode="retrieval-global",
            checkpoint=CheckpointBundle(self.root),
            checkpoint_config=config,
        ).analyze(
            self.graph,
            start_refs=self.starts,
            objective=OBJECTIVE,
            analysis_perspective="",
        )

        self.assertEqual(resumed.to_dict(), first.to_dict())

    def test_global_persistence_contract_names_judge_lifecycle(self):
        self.assertIn(
            "judge-lifecycle/v1",
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )

    def test_inexact_failed_action_replays_as_interrupted_gap(self):
        with self.assertRaises(KeyboardInterrupt):
            self.analyze(
                CountingGlobalJudge(fail_generic=True),
                CrashAfterGlobalAction(
                    self.root,
                    "global_judge_failed",
                ),
            )

        report = self.analyze(
            CountingGlobalJudge(forbid_global=True),
            CheckpointBundle(self.root),
        )

        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "execution_failed")
        self.assertEqual(seed.blocking_reasons, ())
        self.assertEqual(
            seed.execution_failures[0]["reason"],
            "analysis_interrupted",
        )
        failure = next(
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        )
        self.assertEqual(failure["blocker"], "global_judge_interrupted")
        self.assertFalse(failure["physical_request_exact"])


if __name__ == "__main__":
    unittest.main()
