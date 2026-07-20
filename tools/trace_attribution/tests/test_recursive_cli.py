from __future__ import annotations

import json
import tempfile
import unittest
import signal
from pathlib import Path
from unittest import mock

from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
    publish_output_transaction,
)
from trace_attribution.cli import (
    GracefulSignalState,
    atomic_write_json,
    judge_cache_output_path,
    lineage_output_path,
    parse_args,
    recursive_checkpoint_path,
)


def output_config():
    return build_checkpoint_config(
        trace={"case_id": "output-case", "records": []},
        case_id="output-case",
        objective="Find the defect.",
        analysis_perspective="Improve reasoning.",
        start_refs=[],
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        model_identity="offline:test",
        cache_identity="cache:test",
        runtime_identity={
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://test",
            "provider_error_threshold": 3,
        },
    )


class RecursiveCliTest(unittest.TestCase):
    def test_legacy_remains_default_and_existing_paths_are_unchanged(self):
        args = parse_args(["--trace", "/tmp/trace.json", "--out", "/tmp/result.json"])
        self.assertEqual(args.engine, "legacy")
        self.assertEqual(args.max_depth, 8)
        self.assertEqual(args.max_nodes, 48)
        self.assertEqual(
            lineage_output_path(Path(args.out), args.lineage_out),
            Path("/tmp/result.message-lineage.json"),
        )
        self.assertEqual(
            judge_cache_output_path(Path(args.out), args.judge_cache),
            Path("/tmp/result.judge-cache.jsonl"),
        )

    def test_recursive_engine_defaults_and_deterministic_checkpoint_path(self):
        args = parse_args(
            [
                "--trace",
                "/tmp/trace.json",
                "--out",
                "/tmp/case.attribution.json",
                "--engine",
                "recursive-agentic",
                "--analysis-perspective",
                "Improve Agent repository reasoning",
            ]
        )
        self.assertEqual(args.engine, "recursive-agentic")
        self.assertEqual(args.max_frontier_items, 96)
        self.assertEqual(args.recursive_max_depth, 20)
        self.assertEqual(args.max_hypotheses, 24)
        self.assertEqual(args.max_investigation_rounds, 12)
        self.assertEqual(args.max_artifact_bytes, 1_048_576)
        self.assertEqual(args.max_judge_requests, 128)
        self.assertEqual(
            recursive_checkpoint_path(Path(args.out), args.checkpoint_dir),
            Path("/tmp/case.attribution.checkpoint"),
        )

    def test_recursive_flags_accept_exact_overrides(self):
        with tempfile.TemporaryDirectory() as tempdir:
            args = parse_args(
                [
                    "--trace",
                    "/tmp/trace.json",
                    "--out",
                    "/tmp/result.json",
                    "--engine",
                    "recursive-agentic",
                    "--checkpoint-dir",
                    tempdir,
                    "--max-frontier-items",
                    "7",
                    "--recursive-max-depth",
                    "6",
                    "--max-hypotheses",
                    "5",
                    "--max-investigation-rounds",
                    "4",
                    "--max-artifact-bytes",
                    "1024",
                    "--max-judge-requests",
                    "3",
                ]
            )
            self.assertEqual(recursive_checkpoint_path(Path(args.out), args.checkpoint_dir), Path(tempdir))
            self.assertEqual(
                (
                    args.max_frontier_items,
                    args.recursive_max_depth,
                    args.max_hypotheses,
                    args.max_investigation_rounds,
                    args.max_artifact_bytes,
                    args.max_judge_requests,
                ),
                (7, 6, 5, 4, 1024, 3),
            )

    def test_sigint_and_sigterm_share_the_same_graceful_stop_state(self):
        state = GracefulSignalState()
        state.handle(signal.SIGINT, None)
        self.assertTrue(state.stop_requested())
        self.assertEqual(state.signal_name, "SIGINT")

        state = GracefulSignalState()
        state.handle(signal.SIGTERM, None)
        self.assertTrue(state.stop_requested())
        self.assertEqual(state.signal_name, "SIGTERM")

    def test_signal_context_restores_previous_handlers(self):
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        with GracefulSignalState() as state:
            self.assertFalse(state.stop_requested())
            self.assertNotEqual(signal.getsignal(signal.SIGINT), previous_int)
            self.assertNotEqual(signal.getsignal(signal.SIGTERM), previous_term)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous_int)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_term)

    def test_atomic_json_output_fsyncs_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "result.json"
            with mock.patch("trace_attribution.cli.os.fsync") as fsync:
                atomic_write_json(path, {"outcome": "inconclusive"})
            self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "outcome": "inconclusive"\n}\n')
            self.assertGreaterEqual(fsync.call_count, 2)
            self.assertEqual(list(Path(tempdir).glob("*.tmp")), [])

    def test_output_transaction_repairs_crash_between_two_publications(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = CheckpointBundle(root / "case.checkpoint")
            bundle.initialize(output_config())
            attribution = root / "case.attribution.json"
            lineage = root / "case.message-lineage.json"
            report = {"case_id": "output-case", "analysis_outcome": "inconclusive"}
            message_lineage = {"turns": [], "edges": []}

            def crash(stage):
                if stage == "output_after_attribution_publish":
                    raise KeyboardInterrupt("crash between output files")

            with self.assertRaises(KeyboardInterrupt):
                publish_output_transaction(
                    bundle=bundle,
                    attribution_path=attribution,
                    lineage_path=lineage,
                    report=report,
                    message_lineage=message_lineage,
                    fault_hook=crash,
                )
            self.assertTrue(attribution.is_file())
            self.assertFalse(lineage.exists())
            self.assertIsNone(bundle.restore(expected_config=output_config()).final_report)

            output_commit = publish_output_transaction(
                bundle=bundle,
                attribution_path=attribution,
                lineage_path=lineage,
                report=report,
                message_lineage=message_lineage,
            )
            self.assertEqual(output_commit["status"], "published")
            bundle.mark_analysis_completed(report=report, output_commit=output_commit)
            restored = bundle.restore(expected_config=output_config())
            self.assertEqual(restored.final_report, report)
            self.assertEqual(json.loads(attribution.read_text(encoding="utf-8")), report)
            self.assertEqual(
                json.loads(lineage.read_text(encoding="utf-8")), message_lineage
            )

    def test_published_outputs_are_not_complete_before_checkpoint_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = CheckpointBundle(root / "case.checkpoint")
            bundle.initialize(output_config())
            report = {"case_id": "output-case", "analysis_outcome": "inconclusive"}
            output_commit = publish_output_transaction(
                bundle=bundle,
                attribution_path=root / "attribution.json",
                lineage_path=root / "lineage.json",
                report=report,
                message_lineage={"turns": []},
                stop_requested=lambda: True,
            )
            self.assertIsNone(bundle.restore(expected_config=output_config()).final_report)
            bundle.mark_analysis_completed(report=report, output_commit=output_commit)
            self.assertEqual(
                bundle.restore(expected_config=output_config()).final_report, report
            )

    def test_completion_rejects_report_different_from_published_attribution(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = CheckpointBundle(root / "case.checkpoint")
            bundle.initialize(output_config())
            published = {
                "case_id": "output-case",
                "analysis_outcome": "inconclusive",
            }
            output_commit = publish_output_transaction(
                bundle=bundle,
                attribution_path=root / "attribution.json",
                lineage_path=root / "lineage.json",
                report=published,
                message_lineage={"turns": []},
            )
            actions_before = len(
                bundle.restore(expected_config=output_config()).actions
            )
            with self.assertRaises(ValueError):
                bundle.mark_analysis_completed(
                    report={
                        "case_id": "output-case",
                        "analysis_outcome": "no_defect",
                    },
                    output_commit=output_commit,
                )
            restored = bundle.restore(expected_config=output_config())
            self.assertIsNone(restored.final_report)
            self.assertEqual(len(restored.actions), actions_before)

    def test_signal_between_output_publications_does_not_split_transaction(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = CheckpointBundle(root / "case.checkpoint")
            bundle.initialize(output_config())
            shutdown = GracefulSignalState()

            def request_stop(stage):
                if stage == "output_after_attribution_publish":
                    shutdown.handle(signal.SIGTERM, None)

            output_commit = publish_output_transaction(
                bundle=bundle,
                attribution_path=root / "attribution.json",
                lineage_path=root / "lineage.json",
                report={"case_id": "output-case", "analysis_outcome": "inconclusive"},
                message_lineage={"turns": []},
                stop_requested=shutdown.stop_requested,
                fault_hook=request_stop,
            )
            self.assertTrue(shutdown.stop_requested())
            self.assertEqual(output_commit["status"], "published")
            self.assertTrue((root / "attribution.json").is_file())
            self.assertTrue((root / "lineage.json").is_file())
            self.assertIsNone(bundle.restore(expected_config=output_config()).final_report)


if __name__ == "__main__":
    unittest.main()
