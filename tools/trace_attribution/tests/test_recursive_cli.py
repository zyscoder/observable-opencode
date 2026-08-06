from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import signal
from pathlib import Path
from unittest import mock

from trace_attribution.checkpoint import (
    CheckpointBundle,
    CheckpointCompatibilityError,
    build_checkpoint_config,
    publish_output_transaction,
)
from trace_attribution.cli import (
    GracefulSignalState,
    analysis_start_refs,
    atomic_write_json,
    judge_cache_output_path,
    lineage_output_path,
    parse_args,
    recursive_checkpoint_path,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.request import AttributionQuestion, AttributionResult
from trace_attribution import service


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
    def test_question_is_accepted_and_mutually_exclusive_with_objective(self):
        args = parse_args(
            [
                "--trace",
                "/tmp/trace.json",
                "--out",
                "/tmp/out.json",
                "--question",
                "为什么回答错误？",
            ]
        )
        self.assertEqual(args.question, "为什么回答错误？")

        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--trace",
                    "/tmp/trace.json",
                    "--out",
                    "/tmp/out.json",
                    "--question",
                    "why",
                    "--objective",
                    "root",
                ]
            )

    def test_question_reorders_all_default_starts_stably_but_not_explicit_starts(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-starts",
                "records": [
                    {
                        "record_id": "tests",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Tests failed",
                        "data": {
                            "seed_id": "tests",
                            "failure_signature": {"kind": "test_failure"},
                            "summary": "verification tests",
                        },
                    },
                    {
                        "record_id": "compile",
                        "component": "build",
                        "event_type": "case.observed_defect",
                        "title": "编译失败",
                        "data": {
                            "seed_id": "compile",
                            "failure_signature": {"kind": "compile_failure"},
                            "summary": "编译器拒绝源码",
                        },
                    },
                    {
                        "record_id": "docs",
                        "component": "documentation",
                        "event_type": "case.observed_defect",
                        "title": "Documentation issue",
                        "data": {
                            "seed_id": "docs",
                            "failure_signature": {"kind": "docs_failure"},
                            "summary": "missing prose",
                        },
                    },
                ],
            }
        )

        defaults = tuple(graph.default_start_refs())
        ranked = analysis_start_refs(graph, [], question="为什么编译失败？")

        self.assertEqual(ranked[0], "record:compile")
        self.assertCountEqual(ranked, defaults)
        self.assertEqual(ranked[1:], ("record:tests", "record:docs"))
        self.assertEqual(
            analysis_start_refs(
                graph,
                ["record:tests", "record:compile"],
                question="compiler",
            ),
            ("record:tests", "record:compile"),
        )

    def test_single_cjk_character_reorders_related_default_start(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "single-cjk-start",
                "records": [
                    {
                        "record_id": "tests",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "测试失败",
                        "data": {
                            "seed_id": "tests",
                            "failure_signature": {"kind": "test_failure"},
                        },
                    },
                    {
                        "record_id": "compile",
                        "component": "build",
                        "event_type": "case.observed_defect",
                        "title": "编译失败",
                        "data": {
                            "seed_id": "compile",
                            "failure_signature": {"kind": "compile_failure"},
                        },
                    },
                ],
            }
        )

        self.assertEqual(
            analysis_start_refs(graph, [], question="译"),
            ("record:compile", "record:tests"),
        )

    def test_cli_helper_aliases_are_shared_service_objects(self):
        from trace_attribution import cli

        for name in (
            "GracefulSignalState",
            "analysis_start_refs",
            "analyze",
            "attribution_output_payload",
            "atomic_write_json",
            "judge_cache_output_path",
            "lineage_output_path",
            "load_graph",
            "recursive_checkpoint_path",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(cli, name), getattr(service, name))

    def test_main_constructs_shared_request_and_prints_only_result_path(self):
        from trace_attribution import cli

        result = AttributionResult(
            output_path=Path("/tmp/result.json"),
            lineage_path=Path("/tmp/lineage.json"),
            payload={"analysis_outcome": "inconclusive"},
        )
        argv = [
            "trace-attribution",
            "--trace",
            "/tmp/trace.json",
            "--out",
            "/tmp/result.json",
            "--question",
            "  Why did validation fail?  ",
            "--engine",
            "recursive-agentic",
            "--max-frontier-items",
            "7",
            "--model",
            "offline-model",
            "--judge-timeout-sec",
            "12.5",
            "--thinking-mode",
            "disabled",
        ]

        with mock.patch.object(cli, "analyze", return_value=result) as analyze, mock.patch(
            "sys.argv", argv
        ), mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as stdout:
            self.assertEqual(cli.main(), 0)

        request = analyze.call_args.args[0]
        self.assertEqual(request.effective_objective, "Why did validation fail?")
        self.assertEqual(request.output_path, Path("/tmp/result.json").resolve())
        self.assertEqual(request.start_refs, ())
        self.assertEqual(request.options.engine, "recursive-agentic")
        self.assertEqual(request.options.max_frontier_items, 7)
        self.assertEqual(request.options.model, "offline-model")
        self.assertEqual(request.options.judge_timeout_sec, 12.5)
        self.assertEqual(request.options.thinking_mode, "disabled")
        self.assertEqual(stdout.getvalue(), "/tmp/result.json\n")

    def test_main_prints_original_relative_out_and_preserves_duplicate_start_refs(self):
        from trace_attribution import cli

        result = AttributionResult(
            output_path=Path("result.json"),
            lineage_path=None,
            payload={"analysis_outcome": "inconclusive"},
        )
        argv = [
            "trace-attribution",
            "--trace",
            "/tmp/trace.json",
            "--out",
            "result.json",
            "--start-ref",
            " record:failure ",
            "--start-ref",
            "record:failure",
        ]

        with mock.patch.object(cli, "analyze", return_value=result) as analyze, mock.patch(
            "sys.argv", argv
        ), mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as stdout:
            self.assertEqual(cli.main(), 0)

        request = analyze.call_args.args[0]
        self.assertTrue(request.output_path.is_absolute())
        self.assertEqual(
            request.start_refs,
            ("record:failure", "record:failure"),
        )
        self.assertEqual(stdout.getvalue(), "result.json\n")

    def test_main_does_not_rewrite_service_value_error(self):
        from trace_attribution import cli

        argv = [
            "trace-attribution",
            "--trace",
            "/tmp/trace.json",
            "--out",
            "/tmp/result.json",
        ]
        with mock.patch.object(
            cli,
            "analyze",
            side_effect=ValueError("checkpoint corruption"),
        ), mock.patch("sys.argv", argv):
            with self.assertRaisesRegex(ValueError, "checkpoint corruption"):
                cli.main()

    def test_main_rewrites_invalid_transport_limits_as_user_input_error(self):
        from trace_attribution import cli

        for flag in (
            "--judge-timeout-sec",
            "--judge-max-tokens",
            "--provider-error-threshold",
        ):
            argv = [
                "trace-attribution",
                "--trace",
                "/tmp/trace.json",
                "--out",
                "/tmp/result.json",
                flag,
                "-1",
            ]
            with self.subTest(flag=flag), mock.patch.object(cli, "analyze") as analyze, mock.patch(
                "sys.argv", argv
            ):
                with self.assertRaisesRegex(SystemExit, "error: .*must be.*positive"):
                    cli.main()
                analyze.assert_not_called()

    def test_explicit_ineligible_external_evaluation_start_is_rejected(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "ineligible-external-start",
                "records": [
                    {
                        "record_id": "evaluation",
                        "component": "evaluation",
                        "event_type": "external.evaluation_fact",
                        "status": "failed",
                        "data": {
                            "revision_status": "mismatched",
                            "eligible_for_decisive_judgment": False,
                        },
                    }
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "ineligible.*decisive judgment"):
            analysis_start_refs(graph, ["record:evaluation"])

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

    def test_different_questions_produce_different_checkpoint_config_fingerprints(self):
        trace = {"case_id": "question-checkpoint", "records": []}
        kwargs = {
            "trace": trace,
            "case_id": "question-checkpoint",
            "analysis_perspective": "",
            "start_refs": [],
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

        compile_question = build_checkpoint_config(
            **kwargs,
            objective="Why did compilation fail?",
        )
        test_question = build_checkpoint_config(
            **kwargs,
            objective="Why did verification fail?",
        )

        self.assertNotEqual(
            compile_question["config_fingerprint"],
            test_question["config_fingerprint"],
        )

    def test_question_bound_payload_replays_through_real_checkpoint_transaction(self):
        trace = {
            "case_id": "question-replay",
            "records": [
                {
                    "record_id": "start",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "title": "Validation failed",
                },
                {
                    "record_id": "root",
                    "component": "implementation",
                    "event_type": "change.applied",
                    "title": "Invalid validation rule",
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        question = AttributionQuestion.create("Why did validation fail?")
        report = service.question_bound_output_payload(
            {
                "case_id": "question-replay",
                "analysis_outcome": "root_found",
                "confirmed_roots": [
                    {
                        "node_ref": "record:root",
                        "reason": "The validation rule is invalid.",
                        "confidence": 0.8,
                        "evidence_refs": ["record:start"],
                    }
                ],
                "taint_paths": [["record:root", "record:start"]],
            },
            graph,
            binding=question,
            starts=("record:start",),
        )
        lineage = {"turns": [], "edges": []}
        config_kwargs = {
            "trace": graph.raw_trace,
            "case_id": graph.case_id,
            "analysis_perspective": "",
            "start_refs": ["record:start"],
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
        config = build_checkpoint_config(
            **config_kwargs,
            objective=question.normalized,
        )
        other_config = build_checkpoint_config(
            **config_kwargs,
            objective="Why did another validation fail?",
        )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = CheckpointBundle(root / "question.checkpoint")
            bundle.initialize(config)
            output = publish_output_transaction(
                bundle=bundle,
                attribution_path=root / "report.json",
                lineage_path=root / "lineage.json",
                report=report,
                message_lineage=lineage,
            )
            bundle.mark_analysis_completed(report=report, output_commit=output)

            replay_bundle = CheckpointBundle(bundle.root)
            replay_bundle.restore_for_replay(
                expected_config=config,
                expected_lineage=lineage,
            )
            replay = replay_bundle.completed_replay_output_commit(
                attribution_path=root / "report.json",
                lineage_path=root / "lineage.json",
                report=report,
                message_lineage=lineage,
            )

            self.assertEqual(replay["status"], "published")
            self.assertEqual(replay["attribution_hash"], output["attribution_hash"])
            self.assertEqual(
                hashlib.sha256((root / "report.json").read_bytes()).hexdigest(),
                output["attribution_hash"],
            )
            self.assertEqual(
                json.loads((root / "report.json").read_text(encoding="utf-8")),
                report,
            )
            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=other_config,
                    expected_lineage=lineage,
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
