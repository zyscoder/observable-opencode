from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    BoundedJudgeCapability,
    OfflineJudgeCapability,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    RecursiveAttributionReport,
    RootConfirmation,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointBundle,
    CheckpointCompatibilityError,
    CheckpointCorruptionError,
    _sha256,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)


def sample_trace() -> dict:
    return {
        "case_id": "checkpoint-case",
        "records": [
            {
                "record_id": "only",
                "ref": "record:only",
                "component": "result_processing",
                "event_type": "response.output",
                "title": "Only node",
                "status": "completed",
                "timestamp": "2026-07-21T00:00:00Z",
                "data": {"content": "A complete answer."},
                "source_refs": [],
            }
        ],
    }


def sample_config(**changes: object) -> dict:
    values = {
        "trace": sample_trace(),
        "case_id": "checkpoint-case",
        "objective": "Find the defect.",
        "analysis_perspective": "Improve repository reasoning.",
        "start_refs": ["record:only"],
        "budgets": {
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        "model_identity": "offline:test",
        "cache_identity": "cache:test",
        "runtime_identity": {
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://test",
            "provider_error_threshold": 3,
        },
    }
    values.update(changes)
    return build_checkpoint_config(**values)


class CountingOfflineJudge(OfflineJudgeCapability):
    def __init__(self) -> None:
        self.step_calls = 0
        self.confirmation_calls = 0

    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="absent",
            current_defect_reason="The output satisfies the objective.",
            predecessors=(),
            candidate_introduction=False,
            confidence=1.0,
        )

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        raise AssertionError("no root confirmation is expected")


class InterruptingOfflineJudge(CountingOfflineJudge):
    def judge_step_offline(self, request):
        self.step_calls += 1
        raise KeyboardInterrupt("simulated SIGINT inside Judge boundary")


class InterruptingBoundedJudge(BoundedJudgeCapability):
    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.step_calls = 0
        self.allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        self.allowances.append(max_physical_requests)
        if self.interrupt:
            raise KeyboardInterrupt("provider may have received the request")
        raise AssertionError("an in-flight bounded request must not be repeated")

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class ExactFailureJudge(BoundedJudgeCapability):
    def __init__(self) -> None:
        self.step_calls = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.consecutive_provider_errors = 0
        self.provider_error_threshold = 3

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        raise BoundedJudgeCallError("exact step failure", physical_requests=1)

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class ExactRejudgeFailureJudge(ExactFailureJudge):
    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        if self.step_calls > 1:
            raise BoundedJudgeCallError("exact rejudge failure", physical_requests=1)
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="unknown",
                current_defect_reason="Inspect the local node before deciding.",
                predecessors=(),
                candidate_introduction=False,
                missing_evidence=("inspect current node",),
                suggested_investigation={
                    "tool": "inspect_node",
                    "arguments": {"ref": request.current_node.ref},
                    "reason": "Resolve current node semantics.",
                },
                confidence=0.2,
            ),
            1,
        )


class ExactConfirmationFailureJudge(ExactFailureJudge):
    def __init__(self) -> None:
        super().__init__()
        self.confirmation_calls = 0

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="present",
                current_defect_reason="The answer is incomplete.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context[
                            "active_hypothesis_id"
                        ],
                        "candidate_ref": request.current_node.ref,
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Confirm the introduction candidate.",
                },
                confidence=0.9,
            ),
            1,
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_calls += 1
        raise BoundedJudgeCallError(
            "exact confirmation failure", physical_requests=1
        )


class CrashAfterDurableAction(CheckpointBundle):
    def __init__(self, root, *, operation):
        super().__init__(root)
        self.operation = operation

    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == self.operation:
            raise KeyboardInterrupt("crash after durable action")
        return record


class ResettingSuccessJudge(BoundedJudgeCapability):
    def __init__(self, *, errors=2) -> None:
        self.step_calls = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.consecutive_provider_errors = errors
        self.provider_error_threshold = 3

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        self.consecutive_provider_errors = 0
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="absent",
                current_defect_reason="No defect.",
                predecessors=(),
                candidate_introduction=False,
                confidence=1.0,
            ),
            1,
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class CrashAfterFrontierCompleteSnapshot(CheckpointBundle):
    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        if kwargs["semantic_key"] == "analysis:frontier_complete":
            raise KeyboardInterrupt("crash immediately after the global snapshot")
        return commit


class InvestigationJudge(CountingOfflineJudge):
    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="More local evidence is required.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=("inspect the current node",),
            suggested_investigation={
                "tool": "inspect_node",
                "arguments": {"ref": request.current_node.ref},
                "reason": "Resolve the current node semantics.",
            },
            confidence=0.2,
        )


class InterruptingConfirmationJudge(CountingOfflineJudge):
    def __init__(self, *, interrupt: bool) -> None:
        super().__init__()
        self.interrupt = interrupt

    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason="The answer is semantically incomplete.",
            predecessors=(),
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently confirm the introduction candidate.",
            },
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        if self.interrupt:
            raise KeyboardInterrupt("simulated interruption inside confirmation")
        raise AssertionError("an interrupted confirmation must not be repeated")


class UnknownConfirmationJudge(InterruptingConfirmationJudge):
    def __init__(self) -> None:
        super().__init__(interrupt=False)

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        return RootConfirmation.unknown(
            request.candidate_ref, "The trace lacks candidate-local confirmation evidence."
        )


class StopDuringConfirmationJudge(UnknownConfirmationJudge):
    def __init__(self, stop_flag) -> None:
        super().__init__()
        self.stop_flag = stop_flag

    def confirm_candidate_offline(self, request):
        result = super().confirm_candidate_offline(request)
        self.stop_flag[0] = True
        return result


class InterruptingTools:
    artifact_bytes_used = 0
    max_artifact_bytes = 1_048_576

    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.calls = 0

    def for_graph(self, graph, *, max_artifact_bytes):
        self.max_artifact_bytes = max_artifact_bytes
        return self

    def execute(self, directive):
        self.calls += 1
        if self.interrupt:
            raise KeyboardInterrupt("simulated interruption inside investigation")
        raise AssertionError("an interrupted investigation must not be repeated")


class CausalCheckpointTest(unittest.TestCase):
    def test_snapshot_commit_has_one_run_id_and_global_transaction_sequence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            commit = bundle.commit_snapshot(
                semantic_key="snapshot:a",
                frontier_payload={"frontier": ["a"]},
                hypothesis_payload={"hypotheses": ["a"]},
                action_payload={"state": "a"},
            )
            records = [
                json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                for path in (
                    bundle.frontier_path,
                    bundle.hypotheses_path,
                    bundle.actions_path,
                )
            ]
            self.assertEqual({item["run_id"] for item in records}, {commit["run_id"]})
            self.assertEqual(
                {item["transaction_sequence"] for item in records},
                {commit["transaction_sequence"]},
            )
            self.assertEqual(
                commit["journal_heads"]["frontier"]["record_hash"],
                records[0]["record_hash"],
            )

    def test_recursive_restore_rejects_state_members_from_different_transactions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=bundle,
                checkpoint_config=sample_config(),
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            stable = bundle.restore(expected_config=sample_config())
            bundle.record_frontier(
                "snapshot", "forged:mixed", stable.frontier_payload
            )
            mixed = bundle.restore(expected_config=sample_config())
            with self.assertRaises(ValueError):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(sample_trace()), checkpoint=mixed
                )

    def test_restore_rejects_cross_run_journal_mix(self):
        with tempfile.TemporaryDirectory() as tempdir:
            left = CheckpointBundle(Path(tempdir) / "left.checkpoint")
            right = CheckpointBundle(Path(tempdir) / "right.checkpoint")
            left.initialize(sample_config())
            right.initialize(sample_config())
            left.commit_snapshot(
                semantic_key="snapshot:left",
                frontier_payload={"run": "left"},
                hypothesis_payload={"run": "left"},
                action_payload={"run": "left"},
            )
            right.commit_snapshot(
                semantic_key="snapshot:right",
                frontier_payload={"run": "right"},
                hypothesis_payload={"run": "right"},
                action_payload={"run": "right"},
            )
            shutil.copyfile(right.hypotheses_path, left.hypotheses_path)
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(left.root).restore(expected_config=sample_config())

    def test_restore_rejects_deletion_of_a_committed_valid_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.record_action("completed", "call:a", {"status": "completed"})
            lines = bundle.actions_path.read_text(encoding="utf-8").splitlines()
            bundle.actions_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(bundle.root).restore(expected_config=sample_config())

    def test_parseable_record_without_newline_is_repaired_before_append(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.actions_path.write_bytes(bundle.actions_path.read_bytes().rstrip(b"\n"))

            reopened = CheckpointBundle(bundle.root)
            reopened.initialize(sample_config())
            reopened.record_action("completed", "call:a", {"status": "completed"})
            restored = CheckpointBundle(bundle.root).restore(expected_config=sample_config())
            self.assertEqual(len(restored.actions), 2)
            self.assertGreaterEqual(restored.tail_repair_count, 1)
            self.assertTrue(reopened.actions_path.read_bytes().endswith(b"\n"))

    def test_uncommitted_cross_journal_crash_tail_is_not_mixed_into_restore(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            stable = CheckpointBundle(root)
            stable.initialize(sample_config())
            stable.commit_snapshot(
                semantic_key="stable",
                frontier_payload={"version": "stable"},
                hypothesis_payload={"version": "stable"},
                action_payload={"version": "stable"},
            )

            def crash(stage):
                if stage == "snapshot_after_hypotheses":
                    raise KeyboardInterrupt("crash before action member and commit publish")

            crashing = CheckpointBundle(root, fault_hook=crash)
            crashing.initialize(sample_config())
            with self.assertRaises(KeyboardInterrupt):
                crashing.commit_snapshot(
                    semantic_key="unstable",
                    frontier_payload={"version": "unstable"},
                    hypothesis_payload={"version": "unstable"},
                    action_payload={"version": "unstable"},
                )

            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            restored = reopened.restore(expected_config=sample_config())
            self.assertEqual(restored.frontier_payload, {"version": "stable"})
            self.assertEqual(restored.hypothesis_payload, {"version": "stable"})
            self.assertEqual(restored.actions[-1]["payload"], {"version": "stable"})
            self.assertGreaterEqual(restored.tail_repair_count, 2)

    def test_initialize_creates_and_directory_fsyncs_all_journals(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            with mock.patch("trace_attribution.checkpoint.os.fsync") as fsync:
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
            self.assertTrue(bundle.frontier_path.is_file())
            self.assertTrue(bundle.hypotheses_path.is_file())
            self.assertTrue(bundle.actions_path.is_file())
            self.assertTrue(bundle.commit_path.is_file())
            self.assertGreaterEqual(fsync.call_count, 6)

    def test_directory_durability_boundary_is_reached_during_initialize(self):
        with tempfile.TemporaryDirectory() as tempdir:
            stages = []
            bundle = CheckpointBundle(
                Path(tempdir) / "case.checkpoint", fault_hook=stages.append
            )
            bundle.initialize(sample_config())
            self.assertIn("checkpoint_directory_durable", stages)

    def test_timeout_drift_is_checkpoint_incompatible(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            changed_runtime = dict(sample_config()["runtime_identity"])
            changed_runtime["judge_timeout_sec"] = 120.0
            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(root).restore(
                    expected_config=sample_config(runtime_identity=changed_runtime)
                )

    def test_three_journals_are_fsynced_and_restore_after_corrupt_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            with mock.patch("trace_attribution.checkpoint.os.fsync") as fsync:
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
                bundle.record_frontier("snapshot", "visit:a", {"frontier": ["a"]})
                bundle.record_hypothesis("snapshot", "hyp:a", {"hypotheses": ["a"]})
                bundle.record_action("investigation_completed", "action:a", {"status": "success"})
                self.assertGreaterEqual(fsync.call_count, 4)

            with bundle.frontier_path.open("a", encoding="utf-8") as handle:
                handle.write('{"truncated"')

            state = CheckpointBundle(root).restore(expected_config=sample_config())
            self.assertEqual(state.frontier_payload, {"frontier": ["a"]})
            self.assertEqual(state.hypothesis_payload, {"hypotheses": ["a"]})
            self.assertEqual(state.actions[-1]["payload"], {"status": "success"})
            self.assertEqual(state.corrupt_entries, 1)

            raw = json.loads(bundle.actions_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(
                set(raw),
                {
                    "schema_version",
                    "run_id",
                    "journal",
                    "sequence",
                    "transaction_sequence",
                    "timestamp",
                    "operation",
                    "semantic_key",
                    "payload",
                    "previous_hash",
                    "record_hash",
                },
            )
            self.assertEqual(raw["schema_version"], CHECKPOINT_SCHEMA_VERSION)

    def test_reopen_repairs_only_a_truncated_tail_before_the_next_append(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')

            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            reopened.record_action("completed", "call:a", {"status": "completed"})
            state = CheckpointBundle(root).restore(expected_config=sample_config())
            self.assertEqual([item["sequence"] for item in state.actions], [1, 2])
            self.assertEqual(state.corrupt_entries, 0)

    def test_restore_rejects_interior_corruption_reorder_and_hash_break(self):
        mutations = ("interior", "reorder", "hash")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "case.checkpoint"
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
                bundle.record_action("queued", "call:a", {"status": "queued"})
                bundle.record_action("completed", "call:a", {"status": "completed"})
                lines = bundle.actions_path.read_text(encoding="utf-8").splitlines()
                if mutation == "interior":
                    lines[0] = "{broken"
                elif mutation == "reorder":
                    lines.reverse()
                else:
                    record = json.loads(lines[0])
                    record["payload"] = {"status": "forged"}
                    lines[0] = json.dumps(record, sort_keys=True)
                bundle.actions_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaises(CheckpointCorruptionError):
                    CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_nonmonotonic_global_transaction_sequence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.record_action("completed", "call:a", {"status": "completed"})

            records = [
                json.loads(line)
                for line in bundle.actions_path.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["transaction_sequence"] = records[0]["transaction_sequence"]
            records[1]["record_hash"] = _sha256(
                {key: value for key, value in records[1].items() if key != "record_hash"}
            )
            bundle.actions_path.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n",
                encoding="utf-8",
            )
            commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
            commit["journal_heads"]["actions"]["record_hash"] = records[1]["record_hash"]
            commit["commit_hash"] = _sha256(
                {key: value for key, value in commit.items() if key != "commit_hash"}
            )
            bundle.commit_path.write_text(
                json.dumps(commit, sort_keys=True) + "\n", encoding="utf-8"
            )

            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_stale_trace_or_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            changed_trace = sample_trace()
            changed_trace["records"][0]["data"]["content"] = "changed"
            for changed in (
                sample_config(trace=changed_trace),
                sample_config(objective="Different objective."),
                sample_config(model_identity="provider:different"),
                sample_config(cache_identity="cache:different"),
                sample_config(budgets={**sample_config()["budgets"], "max_depth": 21}),
            ):
                with self.subTest(config=changed):
                    with self.assertRaises(CheckpointCompatibilityError):
                        CheckpointBundle(root).restore(expected_config=changed)

    def test_completed_recursive_run_restores_exact_report_without_repeating_calls(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            config = sample_config()
            first_judge = CountingOfflineJudge()
            analyzer = AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=bundle,
                checkpoint_config=config,
            )
            first = analyzer.analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(first_judge.step_calls, 1)

            resumed_judge = CountingOfflineJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(bundle.root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertIsInstance(resumed, RecursiveAttributionReport)
            self.assertEqual(resumed.to_dict(), first.to_dict())
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)

    def test_inflight_provider_call_is_never_replayed_or_fabricated(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            config = sample_config()
            bundle.initialize(config)
            bundle.record_frontier(
                "snapshot",
                "analysis",
                {"frontier": {"schema": "recursive-frontier-checkpoint", "version": 2, "queued": [], "in_flight": [], "completed": []}},
            )
            bundle.record_hypothesis("snapshot", "analysis", {"hypotheses": []})
            bundle.record_action(
                "provider_call_started",
                "step:visit:a",
                {"call_kind": "step", "status": "in_flight", "physical_requests_reserved": 1},
            )
            restored = bundle.restore(expected_config=config)
            self.assertEqual(restored.inflight_actions[0]["semantic_key"], "step:visit:a")
            self.assertFalse(restored.inflight_actions[0]["payload"].get("success", False))

    def test_resume_conservatively_closes_an_interrupted_judge_call_without_replay(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            interrupted_judge = InterruptingOfflineJudge()
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=interrupted_judge,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(interrupted_judge.step_calls, 1)

            resumed_judge = CountingOfflineJudge()
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertIn("record:only", report.unresolved_refs)
            self.assertTrue(
                any(
                    item.get("reason") == "interrupted_judge_call"
                    for item in report.metadata.get("unresolved_branches", [])
                )
            )

    def test_inflight_bounded_call_conservatively_keeps_max_one_budget_debited(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 1
            config = sample_config(budgets=budgets)
            first = InterruptingBoundedJudge(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=first,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                    max_judge_requests=1,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first.allowances, [1])

            resumed_judge = InterruptingBoundedJudge(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                max_judge_requests=1,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(report.metadata["physical_judge_request_count"], 1)
            self.assertEqual(report.metadata["judge_request_uncertainty_count"], 1)
            self.assertGreaterEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)

    def test_exact_failed_step_replays_terminal_semantics_and_exact_debit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="provider_call_failed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

            resumed_judge = ExactFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 1)
            self.assertEqual(
                resumed.metadata["unresolved_branches"][0]["reason"], "judge_error"
            )

    def test_exact_failed_rejudge_replays_without_becoming_interrupted(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactRejudgeFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactRejudgeFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="provider_call_failed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            resumed_judge = ExactRejudgeFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 2)

    def test_exact_failed_confirmation_replays_exact_unknown_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactConfirmationFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactConfirmationFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="confirmation_completed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            resumed_judge = ExactConfirmationFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 2)

    def test_resume_never_repeats_an_inflight_investigation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_tools = InterruptingTools(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=first_tools,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first_tools.calls, 1)

            resumed_tools = InterruptingTools(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=InvestigationJudge(),
                tools=resumed_tools,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_tools.calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertTrue(
                any(
                    item.get("rejection_reason") == "interrupted_investigation_call"
                    for item in report.investigation_journal
                )
            )

    def test_resume_never_repeats_an_inflight_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = InterruptingConfirmationJudge(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first_judge.step_calls, 1)
            self.assertEqual(first_judge.confirmation_calls, 1)

            resumed_judge = InterruptingConfirmationJudge(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertTrue(
                any(
                    item.reason.startswith("confirmation_interrupted")
                    for item in report.confirmations
                )
            )

    def test_resume_restores_provider_circuit_state_before_traversal(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = CountingOfflineJudge()
            first_judge.provider_circuit_open = True
            first_judge.provider_circuit_reason = "provider unavailable"
            first_judge.consecutive_provider_errors = 3
            first_judge.provider_error_threshold = 3
            AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )

            resumed_judge = CountingOfflineJudge()
            resumed_judge.provider_circuit_open = False
            resumed_judge.provider_circuit_reason = ""
            resumed_judge.consecutive_provider_errors = 0
            resumed_judge.provider_error_threshold = 3
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertTrue(report.metadata["provider_circuit"]["open"])
            self.assertEqual(resumed_judge.consecutive_provider_errors, 3)

    def test_provider_state_is_atomic_with_recursive_snapshot(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ResettingSuccessJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ResettingSuccessJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterFrontierCompleteSnapshot(root),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            restored = CheckpointBundle(root).restore(expected_config=config)
            snapshot = next(
                item
                for item in reversed(restored.actions)
                if item["operation"] == "state_snapshot"
            )
            self.assertIn("provider_state", snapshot["payload"])
            self.assertFalse(
                any(item["operation"] == "provider_state" for item in restored.actions)
            )

            resumed_judge = ResettingSuccessJudge(errors=99)
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.consecutive_provider_errors, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())

    def test_recursive_restore_rejects_missing_or_mismatched_provider_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            for mutation in ("missing", "identity"):
                with self.subTest(mutation=mutation):
                    actions = json.loads(json.dumps(restored.actions))
                    snapshot = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "state_snapshot"
                    )
                    if mutation == "missing":
                        snapshot["payload"].pop("provider_state")
                    else:
                        snapshot["payload"]["provider_state"]["identity"] = "0" * 64
                    with self.assertRaises(ValueError):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(sample_trace()),
                            checkpoint=replace(restored, actions=tuple(actions)),
                        )

    def test_final_state_resume_does_not_repeat_an_already_recorded_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = UnknownConfirmationJudge()
            first = AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            actions_path = root / "investigation-actions.jsonl"
            lines = actions_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[-1])["operation"], "analysis_ready")

            resumed_judge = UnknownConfirmationJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(resumed.to_dict(), first.to_dict())

    def test_signal_arriving_during_confirmation_writes_partial_resume_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            stop_flag = [False]
            report = AgenticRecursiveAnalyzer(
                judge=StopDuringConfirmationJudge(stop_flag),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=sample_config(),
                stop_requested=lambda: stop_flag[0],
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertEqual(report.metadata["termination_reason"], "signal_interrupted")
            actions = (root / "investigation-actions.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(json.loads(actions[-1])["operation"], "analysis_ready")

    def test_resumed_signal_checkpoint_converges_to_uninterrupted_report(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            partial = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(partial.analysis_outcome, "inconclusive")

            resumed = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge()
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())

    def test_tail_repair_audit_is_persisted_in_report_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')
            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            report = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=reopened,
                checkpoint_config=sample_config(),
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertGreaterEqual(
                report.metadata["checkpoint_audit"]["tail_repair_count"], 1
            )

    def test_restore_rejects_nonexact_tail_repair_event_schema(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')
            bundle.initialize(sample_config())

            commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
            commit["tail_repair_events"][0]["unexpected"] = True
            commit["commit_hash"] = _sha256(
                {key: value for key, value in commit.items() if key != "commit_hash"}
            )
            bundle.commit_path.write_text(
                json.dumps(commit, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(root).restore(expected_config=sample_config())


if __name__ == "__main__":
    unittest.main()
