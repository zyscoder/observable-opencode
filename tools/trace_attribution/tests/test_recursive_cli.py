from __future__ import annotations

import tempfile
import unittest
import signal
from pathlib import Path
from unittest import mock

from trace_attribution.cli import (
    GracefulSignalState,
    atomic_write_json,
    judge_cache_output_path,
    lineage_output_path,
    parse_args,
    recursive_checkpoint_path,
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


if __name__ == "__main__":
    unittest.main()
