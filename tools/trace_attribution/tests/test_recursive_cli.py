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
    def test_load_graph_combines_sibling_session_segments_without_id_collisions(self):
        with tempfile.TemporaryDirectory() as root_value:
            root = Path(root_value)
            main = root / "context-case"
            resumed = root / "context-case--ses_second--abc123"
            main.mkdir()
            resumed.mkdir()
            (main / "trace.json").write_text(
                json.dumps(
                    {
                        "manifest": {
                            "case_id": "context-case",
                            "session_id": "ses_first",
                            "started_at": "2026-08-20T00:00:00Z",
                        },
                        "records": [
                            {
                                "record_id": "action",
                                "component": "tool",
                                "event_type": "tool.call",
                                "timestamp": "2026-08-20T00:00:01Z",
                                "data": {"call_id": "call_1", "tool_name": "write"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (resumed / "trace.json").write_text(
                json.dumps(
                    {
                        "manifest": {
                            "case_id": "context-case--ses_second--abc123",
                            "session_id": "ses_second",
                            "started_at": "2026-08-20T00:01:00Z",
                        },
                        "records": [
                            {
                                "record_id": "action",
                                "component": "mcp",
                                "event_type": "mcp.call",
                                "timestamp": "2026-08-20T00:01:01Z",
                                "data": {
                                    "call_id": "call_1",
                                    "input": {"tool": "yocto_build_execute"},
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            graph = service.load_graph(main / "trace.json")

            self.assertEqual(graph.case_id, "context-case")
            self.assertEqual(len(graph.nodes), 2)
            self.assertEqual(
                graph.raw_trace["manifest"]["logical_segment_count"], 2
            )
            self.assertEqual(
                {node.data.get("call_id") for node in graph.nodes.values()},
                {"seg_0__call_1", "seg_1__call_1"},
            )
            self.assertTrue(
                any("yocto_build_execute" in str(node.data) for node in graph.nodes.values())
            )

    def test_question_premise_gate_uses_direct_facts_and_can_reject_a_false_omission(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "premise-gate",
                "records": [
                    {
                        "record_id": "execute",
                        "component": "mcp",
                        "event_type": "mcp.call",
                        "data": {
                            "input": {
                                "server": "build",
                                "tool": "yocto_build_execute",
                            },
                            "status": "success",
                        },
                    }
                ],
            }
        )
        question = AttributionQuestion.create(
            "为什么没有调用 yocto_build_execute？"
        )
        bound = service.bind_question_hypothesis(graph, question)

        class FakeTransport:
            def __init__(self):
                self.prompt = ""

            def create_message_text(self, *, system, messages, max_tokens):
                self.prompt = messages[0]["content"]
                return json.dumps(
                    {
                        "status": "contradicted",
                        "expected_behavior": "Call yocto_build_execute.",
                        "alleged_actual_behavior": "The tool was not called.",
                        "reason": "The trace contains a successful MCP call.",
                        "evidence_refs": ["record:execute"],
                        "missing_evidence": [],
                        "confidence": 0.99,
                    }
                )

        transport = FakeTransport()
        assessment = service.assess_question_premise(
            transport,
            bound,
            question,
            seed_ref=bound.default_start_refs()[0],
        )

        self.assertEqual(assessment["status"], "contradicted")
        self.assertEqual(assessment["evidence_refs"], ["record:execute"])
        self.assertIn("yocto_build_execute", transport.prompt)
        self.assertLess(len(transport.prompt.encode("utf-8")), 64_000)

    def test_supported_question_premise_binds_a_grounded_first_deviation(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "localized-premise",
                "records": [
                    {
                        "record_id": "plan",
                        "component": "mcp",
                        "event_type": "tool.result",
                        "data": {"next_tool": "yocto_build_execute"},
                    },
                    {
                        "record_id": "reasoning",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "Edit first, then execute."},
                    },
                    {
                        "record_id": "edit",
                        "component": "tool",
                        "event_type": "tool.call",
                        "data": {"tool_name": "edit"},
                    },
                    {
                        "record_id": "execute",
                        "component": "mcp",
                        "event_type": "mcp.call",
                        "data": {"tool": "yocto_build_execute"},
                    },
                ],
            }
        )
        question = AttributionQuestion.create(
            "为什么先 edit，之后才调用 yocto_build_execute？"
        )
        bound = service.bind_question_hypothesis(graph, question)
        seed_ref = bound.default_start_refs()[0]
        assessment = service._validate_question_premise_assessment(
            {
                "status": "supported",
                "expected_behavior": "Execute the plan before editing.",
                "alleged_actual_behavior": "The edit happened first.",
                "reason": "The recorded order violates the plan.",
                "evidence_refs": ["record:reasoning", "record:edit", "record:execute"],
                "missing_evidence": [],
                "confidence": 0.97,
                "deviation_type": "action_order",
                "contract_source_refs": ["record:plan"],
                "actual_sequence_refs": ["record:edit", "record:execute"],
                "first_deviation_ref": "record:reasoning",
                "upstream_influence_refs": ["record:plan"],
                "localization_reason": "The reasoning first commits to the reversed order.",
            },
            allowed_refs=frozenset(
                {"record:plan", "record:reasoning", "record:edit", "record:execute"}
            ),
        )

        localized = service.bind_question_premise_assessment(bound, assessment)
        projection = localized.raw_trace["offline_question_projection"]
        seed = localized.nodes[seed_ref]

        self.assertEqual(projection["first_deviation_ref"], "record:reasoning")
        self.assertEqual(seed.data["failure_type"], "action_order")
        self.assertNotIn("failure_signature", seed.data)
        self.assertEqual(seed.data["deviation_type"], "action_order")
        self.assertEqual(seed.data["expected"], "Execute the plan before editing.")
        self.assertEqual(seed.data["actual"], "The edit happened first.")
        self.assertEqual(
            seed.data["contract_source_refs"], ["record:plan"]
        )

    def test_question_analysis_start_uses_the_bound_seed_even_with_a_case_failure(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-start-precedence",
                "records": [
                    {
                        "record_id": "case_failed",
                        "component": "process",
                        "event_type": "case.failed",
                        "data": {
                            "failure_signature": {
                                "kind": "signal",
                                "signal": "SIGINT",
                            }
                        },
                    }
                ],
            }
        )
        question = AttributionQuestion.create(
            "为什么没有先执行计划返回的下一步？"
        )
        bound = service.bind_question_hypothesis(graph, question)

        starts = service.question_analysis_start_refs(
            bound,
            explicit_refs=(),
            question=question.normalized,
        )

        self.assertEqual(
            starts,
            (bound.raw_trace["offline_question_projection"]["seed_ref"],),
        )
        self.assertNotIn("record:case_failed", starts)

        self.assertEqual(
            service.question_analysis_start_refs(
                bound,
                explicit_refs=("record:case_failed",),
                question=question.normalized,
            ),
            ("record:case_failed",),
        )

    def test_contradicted_premise_report_is_a_grounded_no_defect_result(self):
        report = service.question_premise_no_defect_report(
            case_id="premise-report",
            objective="Why was the tool omitted?",
            seed_ref="record:offline_question",
            assessment={
                "status": "contradicted",
                "expected_behavior": "Call the tool.",
                "alleged_actual_behavior": "The tool was omitted.",
                "reason": "The recorded tool call disproves the omission.",
                "evidence_refs": ["record:tool_call"],
                "missing_evidence": [],
                "confidence": 0.98,
            },
        )

        self.assertEqual(report["analysis_outcome"], "no_defect")
        self.assertEqual(report["seed_results"][0]["outcome"], "no_defect")
        self.assertEqual(
            report["seed_results"][0]["decisive_evidence_refs"],
            ["record:tool_call"],
        )
        self.assertEqual(report["premise_assessment"]["status"], "contradicted")

    def test_question_creates_an_offline_expectation_gap_seed_over_relevant_trace_facts(self):
        duplicated_context = [
            {
                "record_id": "context_{0}".format(index),
                "component": "context",
                "event_type": "context.transform",
                "data": {
                    "text": (
                        "为什么调用了 yocto_build_plan，却没有继续调用它要求的 "
                        "yocto_build_execute？"
                    )
                },
            }
            for index in range(40)
        ]
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-hypothesis",
                "records": [
                    {
                        "record_id": "plan",
                        "component": "mcp",
                        "event_type": "mcp.call",
                        "data": {"tool_name": "yocto_build_plan"},
                    },
                    {
                        "record_id": "execute",
                        "component": "mcp",
                        "event_type": "mcp.call",
                        "source_refs": ["record:plan"],
                        "data": {"tool_name": "yocto_build_execute"},
                    },
                    *duplicated_context,
                    {
                        "record_id": "answer",
                        "component": "result",
                        "event_type": "response.claim",
                        "source_refs": ["record:execute"],
                        "data": {"text": "Yocto build completed."},
                    },
                ],
            }
        )
        question = AttributionQuestion.create(
            "为什么调用了 yocto_build_plan，却没有继续调用 yocto_build_execute？"
        )

        bound = service.bind_question_hypothesis(graph, question)
        starts = bound.default_start_refs()

        self.assertEqual(len(starts), 1)
        seed = bound.nodes[starts[0]]
        self.assertEqual(seed.event_type, "case.observed_defect")
        self.assertEqual(seed.data["hypothesis_origin"], "user_question")
        self.assertEqual(seed.data["premise_status"], "unverified")
        self.assertIn("record:plan", bound.upstream_refs(seed.ref))
        self.assertIn("record:execute", bound.upstream_refs(seed.ref))
        self.assertNotIn(seed.ref, graph.nodes)

    def test_no_defect_question_projection_reports_a_contradicted_premise(self):
        graph = TraceGraph.from_trace({"case_id": "no-defect", "records": []})
        question = AttributionQuestion.create("为什么没有调用构建工具？")

        payload = service.question_bound_output_payload(
            {
                "analysis_outcome": "no_defect",
                "confirmed_roots": [],
                "co_roots": [],
                "root_causes": [],
                "unresolved_refs": [],
            },
            graph,
            binding=question,
            starts=("record:offline_question",),
        )

        self.assertEqual(payload["question_premise_status"], "contradicted")
        self.assertIn("not supported", payload["conclusion"])
        self.assertNotIn(
            "no_confirmed_root_cause",
            {item.get("kind") for item in payload["unresolved_gaps"]},
        )
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

    def test_main_constructs_shared_request_and_prints_result_and_explanation_paths(self):
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
        self.assertEqual(
            stdout.getvalue(),
            "/tmp/result.json\n/tmp/result.explanation.md\n",
        )

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
        self.assertEqual(
            stdout.getvalue(),
            "result.json\nresult.explanation.md\n",
        )

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
