from __future__ import annotations

import contextlib
from dataclasses import FrozenInstanceError
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from trace_attribution.graph import TraceGraph
from trace_attribution.models import NodeJudgment
from trace_attribution.models import stable_json
from trace_attribution.causal_state import RecursiveAttributionReport
from trace_attribution.request import (
    DEFAULT_ATTRIBUTION_OBJECTIVE,
    MAX_QUESTION_CHARS,
    AttributionOptions,
    AttributionQuestion,
    AttributionRequest,
    AttributionResult,
    normalize_question,
)


class AttributionRequestTest(unittest.TestCase):
    def test_question_normalization_and_identity_are_stable(self) -> None:
        left = AttributionQuestion.create("  为什么编译失败？\r\n")
        right = AttributionQuestion.create("为什么编译失败？\n")

        self.assertEqual(left.original, "  为什么编译失败？\r\n")
        self.assertEqual(left.normalized, "为什么编译失败？")
        self.assertEqual(normalize_question(left.original), left.normalized)
        self.assertEqual(left.question_id, right.question_id)

    def test_question_rejects_empty_and_overlong_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "question.*non-empty"):
            AttributionQuestion.create(" \t\r\n ")
        with self.assertRaisesRegex(ValueError, "question.*16,384"):
            AttributionQuestion.create("x" * (MAX_QUESTION_CHARS + 1))

    def test_question_and_objective_are_mutually_exclusive(self) -> None:
        trace = Path("trace.json")
        out = Path("out.json")

        with self.assertRaisesRegex(ValueError, "question.*objective"):
            AttributionRequest(
                trace_path=trace,
                output_path=out,
                question="why",
                objective="root",
            )

    def test_request_requires_exactly_one_trace_source(self) -> None:
        out = Path("out.json")

        with self.assertRaisesRegex(ValueError, "exactly one.*trace_path.*benchmark_bundle_path"):
            AttributionRequest(trace_path=None, output_path=out)
        with self.assertRaisesRegex(ValueError, "exactly one.*trace_path.*benchmark_bundle_path"):
            AttributionRequest(
                trace_path=Path("trace.json"),
                output_path=out,
                benchmark_bundle_path=Path("bundle"),
            )

    def test_request_rejects_empty_string_paths_before_path_conversion(self) -> None:
        with self.assertRaisesRegex(ValueError, "trace_path.*non-empty"):
            AttributionRequest(trace_path="", output_path=Path("out.json"))
        with self.assertRaisesRegex(ValueError, "benchmark_bundle_path.*non-empty"):
            AttributionRequest(
                trace_path=None,
                output_path=Path("out.json"),
                benchmark_bundle_path="",
            )
        with self.assertRaisesRegex(ValueError, "output_path.*non-empty"):
            AttributionRequest(trace_path=Path("trace.json"), output_path="")

        dot_request = AttributionRequest(
            trace_path=Path("."),
            output_path=Path("out.json"),
        )
        self.assertEqual(dot_request.trace_path, Path(".").resolve())

    def test_request_rejects_whitespace_only_question(self) -> None:
        with self.assertRaisesRegex(ValueError, "question.*non-empty"):
            AttributionRequest(
                trace_path=Path("trace.json"),
                output_path=Path("out.json"),
                question=" \t\r\n ",
            )

    def test_request_normalizes_paths_and_tuple_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            request = AttributionRequest(
                trace_path=root / "inputs" / ".." / "trace.json",
                output_path=root / "outputs" / ".." / "result.json",
                review_path=root / "review" / ".." / "review.json",
                evaluation_paths=[root / "eval" / ".." / "first.json", root / "second.json"],
                start_refs=[" record:one ", "record:two", "record:one"],
            )

            self.assertEqual(request.trace_path, (root / "trace.json").resolve())
            self.assertEqual(request.output_path, (root / "result.json").resolve())
            self.assertIsNone(request.benchmark_bundle_path)
            self.assertEqual(request.review_path, (root / "review.json").resolve())
            self.assertEqual(
                request.evaluation_paths,
                ((root / "first.json").resolve(), (root / "second.json").resolve()),
            )
            self.assertEqual(
                request.start_refs,
                ("record:one", "record:two", "record:one"),
            )
            self.assertIsInstance(request.evaluation_paths, tuple)
            self.assertIsInstance(request.start_refs, tuple)

            bundle_request = AttributionRequest(
                trace_path=None,
                output_path=root / "bundle-result.json",
                benchmark_bundle_path=root / "bundles" / ".." / "bundle",
            )
            self.assertIsNone(bundle_request.trace_path)
            self.assertEqual(
                bundle_request.benchmark_bundle_path,
                (root / "bundle").resolve(),
            )

    def test_analysis_rejects_writable_path_aliases_before_transport_or_writes(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            trace_bytes = json.dumps(
                {
                    "case_id": "path-isolation",
                    "records": [
                        {
                            "record_id": "failure",
                            "component": "evaluation",
                            "event_type": "case.failed",
                            "title": "Failure",
                        }
                    ],
                }
            ).encode("utf-8")
            trace_path.write_bytes(trace_bytes)
            review_path = root / "review.json"
            review_bytes = b'{"review":"immutable"}\n'
            review_path.write_bytes(review_bytes)
            evaluation_path = root / "evaluation.json"
            evaluation_bytes = b'{"evaluation":"immutable"}\n'
            evaluation_path.write_bytes(evaluation_bytes)

            cases = (
                (
                    "output aliases trace",
                    AttributionRequest(trace_path=trace_path, output_path=trace_path),
                    (),
                ),
                (
                    "lineage aliases output",
                    AttributionRequest(
                        trace_path=trace_path,
                        output_path=root / "lineage-output.json",
                        options=AttributionOptions(
                            lineage_output_path=root / "lineage-output.json"
                        ),
                    ),
                    (root / "lineage-output.json",),
                ),
                (
                    "cache aliases trace",
                    AttributionRequest(
                        trace_path=trace_path,
                        output_path=root / "cache-output.json",
                        options=AttributionOptions(judge_cache_path=trace_path),
                    ),
                    (root / "cache-output.json",),
                ),
                (
                    "checkpoint aliases output",
                    AttributionRequest(
                        trace_path=trace_path,
                        output_path=root / "checkpoint-output.json",
                        options=AttributionOptions(
                            engine="recursive-agentic",
                            checkpoint_path=root / "checkpoint-output.json",
                        ),
                    ),
                    (root / "checkpoint-output.json",),
                ),
                (
                    "output aliases review input",
                    AttributionRequest(
                        trace_path=trace_path,
                        review_path=review_path,
                        output_path=review_path,
                    ),
                    (),
                ),
                (
                    "lineage aliases evaluation input",
                    AttributionRequest(
                        trace_path=trace_path,
                        evaluation_paths=(evaluation_path,),
                        output_path=root / "evaluation-output.json",
                        options=AttributionOptions(
                            lineage_output_path=evaluation_path
                        ),
                    ),
                    (root / "evaluation-output.json",),
                ),
            )

            for label, request, absent_paths in cases:
                with self.subTest(label=label), mock.patch.object(
                    service, "ClaudeJudgeClient"
                ) as transport_factory:
                    with self.assertRaisesRegex(ValueError, "path.*alias|writable.*input"):
                        service.analyze(request)
                    transport_factory.assert_not_called()
                    self.assertEqual(trace_path.read_bytes(), trace_bytes)
                    self.assertEqual(review_path.read_bytes(), review_bytes)
                    self.assertEqual(evaluation_path.read_bytes(), evaluation_bytes)
                    for absent_path in absent_paths:
                        self.assertFalse(absent_path.exists())

    def test_analysis_rejects_checkpoint_descendant_of_bundle_before_loading_or_writing(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = root / "bundle"
            bundle.mkdir()
            marker = bundle / "input-marker.json"
            marker_bytes = b'{"immutable":true}\n'
            marker.write_bytes(marker_bytes)
            output = root / "result.json"
            checkpoint = bundle / "analysis.checkpoint"
            request = AttributionRequest(
                trace_path=None,
                benchmark_bundle_path=bundle,
                output_path=output,
                options=AttributionOptions(
                    engine="recursive-agentic",
                    checkpoint_path=checkpoint,
                ),
            )

            with mock.patch.object(service, "load_graph") as load_graph, mock.patch.object(
                service, "ClaudeJudgeClient"
            ) as transport_factory:
                with self.assertRaisesRegex(ValueError, "benchmark bundle|writable.*input"):
                    service.analyze(request)

            load_graph.assert_not_called()
            transport_factory.assert_not_called()
            self.assertEqual(marker.read_bytes(), marker_bytes)
            self.assertFalse(checkpoint.exists())
            self.assertFalse(output.exists())

    def test_legacy_analysis_ignores_unused_checkpoint_path_alias(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            output_path = root / "result.json"
            trace_path.write_text(
                json.dumps(
                    {
                        "case_id": "legacy-unused-checkpoint",
                        "records": [
                            {
                                "record_id": "failure",
                                "component": "evaluation",
                                "event_type": "case.failed",
                                "title": "Failure",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = AttributionRequest(
                trace_path=trace_path,
                output_path=output_path,
                options=AttributionOptions(checkpoint_path=output_path),
            )
            analyzer = mock.Mock()
            analyzer.analyze.return_value = mock.Mock(
                to_dict=lambda: {
                    "case_id": "legacy-unused-checkpoint",
                    "root_causes": [],
                    "taint_paths": [],
                }
            )

            with mock.patch.object(
                service, "ClaudeJudgeClient", return_value=mock.sentinel.transport
            ), mock.patch.object(
                service, "BackwardTaintAnalyzer", return_value=analyzer
            ):
                result = service.analyze(request)

            self.assertEqual(result.output_path, output_path.resolve())
            self.assertTrue(output_path.exists())

    def test_analysis_rejects_structural_writable_path_collisions(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            trace_path.write_text(
                json.dumps(
                    {
                        "case_id": "structural-path-collision",
                        "records": [
                            {
                                "record_id": "failure",
                                "component": "evaluation",
                                "event_type": "case.failed",
                                "title": "Failure",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            requests = (
                AttributionRequest(
                    trace_path=trace_path,
                    output_path=root / "result",
                    options=AttributionOptions(
                        lineage_output_path=root / "result" / "lineage.json"
                    ),
                ),
                AttributionRequest(
                    trace_path=trace_path,
                    output_path=root / "checkpoint" / "result.json",
                    options=AttributionOptions(
                        engine="recursive-agentic",
                        checkpoint_path=root / "checkpoint",
                    ),
                ),
            )

            for request in requests:
                with self.subTest(output=str(request.output_path)), mock.patch.object(
                    service, "ClaudeJudgeClient"
                ) as transport_factory:
                    with self.assertRaisesRegex(ValueError, "path.*contain|structural.*collision"):
                        service.analyze(request)
                    transport_factory.assert_not_called()
                    self.assertFalse(request.output_path.exists())

    def test_analysis_rejects_existing_hard_link_writable_aliases_without_mutation(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        for writable_kind in ("output", "judge_cache"):
            with self.subTest(writable_kind=writable_kind), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                trace_path = root / "trace.json"
                trace_bytes = b'{"case_id":"hard-link-alias","records":[]}\n'
                trace_path.write_bytes(trace_bytes)
                alias_path = root / "writable-alias"
                os.link(trace_path, alias_path)
                original_inode = trace_path.stat().st_ino
                output_path = alias_path if writable_kind == "output" else root / "result.json"
                options = (
                    AttributionOptions(judge_cache_path=alias_path)
                    if writable_kind == "judge_cache"
                    else AttributionOptions()
                )
                request = AttributionRequest(
                    trace_path=trace_path,
                    output_path=output_path,
                    options=options,
                )

                with mock.patch.object(service, "load_graph") as load_graph, mock.patch.object(
                    service, "ClaudeJudgeClient"
                ) as transport_factory:
                    with self.assertRaisesRegex(ValueError, "filesystem alias|same file"):
                        service.analyze(request)

                load_graph.assert_not_called()
                transport_factory.assert_not_called()
                self.assertTrue(os.path.samefile(trace_path, alias_path))
                self.assertEqual(trace_path.stat().st_ino, original_inode)
                self.assertEqual(alias_path.stat().st_ino, original_inode)
                self.assertEqual(trace_path.read_bytes(), trace_bytes)
                self.assertEqual(alias_path.read_bytes(), trace_bytes)
                if writable_kind == "judge_cache":
                    self.assertFalse(output_path.exists())

    def test_case_only_filesystem_aliases_are_rejected_when_supported(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "Trace.json"
            trace_bytes = b'{"case_id":"case-only-alias","records":[]}\n'
            trace_path.write_bytes(trace_bytes)
            case_alias = root / "trace.json"
            try:
                case_alias_is_same = os.path.samefile(trace_path, case_alias)
            except (FileNotFoundError, OSError):
                case_alias_is_same = False
            if not case_alias_is_same:
                self.skipTest("fixture filesystem is case-sensitive")

            direct_request = AttributionRequest(
                trace_path=trace_path,
                output_path=case_alias,
            )
            writes_dir = root / "Writes"
            writes_dir.mkdir()
            writes_alias = root / "writes"
            self.assertTrue(os.path.samefile(writes_dir, writes_alias))
            structural_request = AttributionRequest(
                trace_path=trace_path,
                output_path=writes_dir / "result",
                options=AttributionOptions(
                    lineage_output_path=writes_alias / "result" / "lineage.json"
                ),
            )
            bundle_dir = root / "Bundle"
            bundle_dir.mkdir()
            bundle_alias = root / "bundle"
            self.assertTrue(os.path.samefile(bundle_dir, bundle_alias))
            bundle_request = AttributionRequest(
                trace_path=None,
                benchmark_bundle_path=bundle_dir,
                output_path=bundle_alias / "result.json",
            )

            for request in (direct_request, structural_request, bundle_request):
                with self.subTest(output=str(request.output_path)), mock.patch.object(
                    service, "load_graph"
                ) as load_graph, mock.patch.object(
                    service, "ClaudeJudgeClient"
                ) as transport_factory:
                    with self.assertRaisesRegex(
                        ValueError,
                        "filesystem alias|containment collision|inside benchmark bundle",
                    ):
                        service.analyze(request)
                    load_graph.assert_not_called()
                    transport_factory.assert_not_called()

            self.assertEqual(trace_path.read_bytes(), trace_bytes)

    def test_case_only_nonexisting_writable_descendants_share_existing_ancestor(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            probe = root / "CaseProbe"
            probe.mkdir()
            probe_alias = root / "caseprobe"
            try:
                case_insensitive = os.path.samefile(probe, probe_alias)
            except (FileNotFoundError, OSError):
                case_insensitive = False
            if not case_insensitive:
                self.skipTest("fixture filesystem is case-sensitive")

            trace_path = root / "trace.json"
            trace_bytes = b'{"case_id":"shared-ancestor-case-alias","records":[]}\n'
            trace_path.write_bytes(trace_bytes)
            request = AttributionRequest(
                trace_path=trace_path,
                output_path=root / "A",
                options=AttributionOptions(lineage_output_path=root / "a" / "b"),
            )

            with mock.patch.object(service, "load_graph") as load_graph, mock.patch.object(
                service, "ClaudeJudgeClient"
            ) as transport_factory:
                with self.assertRaisesRegex(ValueError, "containment collision"):
                    service.analyze(request)

            load_graph.assert_not_called()
            transport_factory.assert_not_called()
            self.assertEqual(trace_path.read_bytes(), trace_bytes)
            self.assertFalse(request.output_path.exists())
            self.assertFalse(request.options.lineage_output_path.exists())

    def test_case_different_nonexisting_descendants_remain_distinct_on_sensitive_filesystem(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            probe = root / "CaseProbe"
            probe.mkdir()
            probe_alias = root / "caseprobe"
            try:
                case_insensitive = os.path.samefile(probe, probe_alias)
            except (FileNotFoundError, OSError):
                case_insensitive = False
            if case_insensitive:
                self.skipTest("fixture filesystem is case-insensitive")

            trace_path = root / "trace.json"
            trace_path.write_text(
                '{"case_id":"case-sensitive-distinct","records":[]}\n',
                encoding="utf-8",
            )
            request = AttributionRequest(
                trace_path=trace_path,
                output_path=root / "A",
                options=AttributionOptions(lineage_output_path=root / "a" / "b"),
            )

            paths = service.validate_request_path_isolation(request)

            self.assertEqual(paths["output"], (root / "A").resolve())
            self.assertEqual(paths["lineage"], (root / "a" / "b").resolve())

    def test_existing_writable_file_inside_case_aliased_bundle_is_rejected(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle = root / "Bundle"
            bundle.mkdir()
            bundle_alias = root / "bundle"
            try:
                case_insensitive = os.path.samefile(bundle, bundle_alias)
            except (FileNotFoundError, OSError):
                case_insensitive = False
            if not case_insensitive:
                self.skipTest("fixture filesystem is case-sensitive")

            existing_output = bundle / "result.json"
            output_bytes = b'{"immutable":"bundle-input"}\n'
            existing_output.write_bytes(output_bytes)
            self.assertTrue(
                service._path_is_within_filesystem(
                    bundle_alias / "result.json",
                    bundle,
                )
            )
            request = AttributionRequest(
                trace_path=None,
                benchmark_bundle_path=bundle,
                output_path=bundle_alias / "result.json",
            )

            with mock.patch.object(service, "load_graph") as load_graph, mock.patch.object(
                service, "ClaudeJudgeClient"
            ) as transport_factory:
                with self.assertRaisesRegex(ValueError, "inside benchmark bundle"):
                    service.analyze(request)

            load_graph.assert_not_called()
            transport_factory.assert_not_called()
            self.assertTrue(os.path.samefile(existing_output, request.output_path))
            self.assertEqual(existing_output.read_bytes(), output_bytes)

    def test_request_is_frozen_and_resolves_effective_objective(self) -> None:
        question_request = AttributionRequest(
            trace_path=Path("trace.json"),
            output_path=Path("out.json"),
            question="  why did the build fail?\n",
        )
        objective_request = AttributionRequest(
            trace_path=Path("trace.json"),
            output_path=Path("out.json"),
            objective="Find the broken decision.",
        )
        default_request = AttributionRequest(
            trace_path=Path("trace.json"),
            output_path=Path("out.json"),
        )

        self.assertEqual(question_request.effective_objective, "why did the build fail?")
        self.assertEqual(question_request.normalized_question, "why did the build fail?")
        self.assertEqual(question_request.question_binding, question_request.question_info)
        self.assertIsNotNone(question_request.question_binding)
        self.assertEqual(objective_request.effective_objective, "Find the broken decision.")
        self.assertIsNone(objective_request.question_binding)
        self.assertEqual(default_request.effective_objective, DEFAULT_ATTRIBUTION_OBJECTIVE)
        with self.assertRaises(FrozenInstanceError):
            question_request.output_path = Path("other.json")

    def test_options_resolve_engine_specific_default_depth(self) -> None:
        self.assertIsNone(AttributionOptions().max_depth)
        self.assertEqual(AttributionOptions().effective_max_depth, 8)
        self.assertEqual(
            AttributionOptions(engine="recursive-agentic").effective_max_depth,
            20,
        )
        self.assertEqual(AttributionOptions(max_depth=7).effective_max_depth, 7)

    def test_result_payload_is_recursively_read_only_and_to_dict_is_independent(self) -> None:
        result = AttributionResult(
            output_path=Path("out.json"),
            lineage_path=None,
            payload={"nested": {"items": [{"value": 1}]}},
        )

        with self.assertRaises(TypeError):
            result.payload["nested"]["items"][0]["value"] = 2
        with self.assertRaises(TypeError):
            result.payload["added"] = "no"

        result_copy = result.to_dict()
        self.assertEqual(
            result_copy["payload"],
            {"nested": {"items": [{"value": 1}]}},
        )
        self.assertIsNone(result_copy["lineage_path"])
        json.dumps(result_copy)
        result_copy["payload"]["nested"]["items"][0]["value"] = 2
        self.assertEqual(result.payload["nested"]["items"][0]["value"], 1)

    def test_options_match_cli_defaults_and_reject_invalid_budgets(self) -> None:
        options = AttributionOptions()

        self.assertEqual(options.engine, "legacy")
        self.assertEqual(options.fusion_mode, "retrieval-global")
        self.assertEqual(options.max_depth, None)
        self.assertEqual(
            (
                options.max_nodes,
                options.max_frontier_items,
                options.max_hypotheses,
                options.max_investigation_rounds,
                options.max_artifact_bytes,
                options.max_judge_requests,
            ),
            (48, 96, 24, 12, 1_048_576, 128),
        )
        for name in (
            "max_depth",
            "max_nodes",
            "max_frontier_items",
            "max_hypotheses",
            "max_investigation_rounds",
            "max_artifact_bytes",
            "max_judge_requests",
        ):
            with self.subTest(name=name):
                kwargs = {name: -1}
                with self.assertRaisesRegex(ValueError, name):
                    AttributionOptions(**kwargs)
                kwargs = {name: True}
                with self.assertRaisesRegex(ValueError, name):
                    AttributionOptions(**kwargs)

    def test_options_capture_cli_transport_and_output_defaults(self) -> None:
        options = AttributionOptions()

        self.assertIsNone(options.lineage_output_path)
        self.assertIsNone(options.judge_cache_path)
        self.assertIsNone(options.checkpoint_path)
        self.assertEqual(options.model, "")
        self.assertEqual(options.api_key_env, "ANTHROPIC_API_KEY")
        self.assertEqual(options.base_url, "")
        self.assertEqual(options.base_url_env, "ANTHROPIC_BASE_URL")
        self.assertEqual(options.judge_timeout_sec, 3600.0)
        self.assertEqual(options.judge_max_tokens, 8192)
        self.assertEqual(options.thinking_mode, "auto")
        self.assertEqual(options.provider_error_threshold, 3)

    def test_transport_limits_require_positive_finite_exact_numeric_types(self) -> None:
        for value in ("12", True, False, 0, -1, math.inf, -math.inf, math.nan):
            with self.subTest(field="judge_timeout_sec", value=value):
                with self.assertRaisesRegex(ValueError, "judge_timeout_sec"):
                    AttributionOptions(judge_timeout_sec=value)

        for name in ("judge_max_tokens", "provider_error_threshold"):
            for value in ("12", True, False, 0, -1, 1.5, math.inf, math.nan):
                with self.subTest(field=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        AttributionOptions(**{name: value})

        self.assertEqual(AttributionOptions(judge_timeout_sec=12).judge_timeout_sec, 12.0)
        self.assertIsInstance(
            AttributionOptions(judge_timeout_sec=12).judge_timeout_sec,
            float,
        )

    def test_python_service_uses_question_as_effective_objective_and_preserves_request(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            trace = {
                "case_id": "question-service",
                "records": [
                    {
                        "record_id": "tests",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Tests failed after the change",
                        "data": {
                            "seed_id": "tests",
                            "failure_signature": {"kind": "test_failure"},
                            "summary": "The verification tests failed",
                        },
                    },
                    {
                        "record_id": "compile",
                        "component": "build",
                        "event_type": "case.observed_defect",
                        "title": "Compiler rejected the patch",
                        "data": {
                            "seed_id": "compile",
                            "failure_signature": {"kind": "compile_failure"},
                            "summary": "Build compiler error",
                        },
                    },
                ],
            }
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            request = AttributionRequest(
                trace_path=trace_path,
                output_path=root / "result.json",
                question="  Why did the compiler fail?  ",
                start_refs=(
                    "record:compile",
                    "record:compile",
                    "record:tests",
                ),
                options=AttributionOptions(max_depth=7, max_nodes=11),
            )
            transport = mock.Mock(
                model="offline-model",
                base_url="offline://judge",
                thinking_config={"type": "disabled"},
                max_tokens=8192,
                provider_error_threshold=3,
            )
            analyzer = mock.Mock()
            analyzer.analyze.return_value = mock.Mock(
                to_dict=lambda: {
                    "case_id": "question-service",
                    "objective": request.effective_objective,
                    "start_refs": [
                        "record:compile",
                        "record:compile",
                        "record:tests",
                    ],
                }
            )

            with mock.patch.object(
                service, "ClaudeJudgeClient", return_value=transport
            ) as transport_factory, mock.patch.object(
                service, "BackwardTaintAnalyzer", return_value=analyzer
            ) as analyzer_factory, mock.patch.object(
                service,
                "attribution_output_payload",
                side_effect=lambda report, _graph: report.to_dict(),
            ):
                result = service.analyze(request)

            transport_factory.assert_called_once_with(
                model="",
                api_key_env="ANTHROPIC_API_KEY",
                base_url="",
                base_url_env="ANTHROPIC_BASE_URL",
                max_tokens=8192,
                timeout_seconds=3600.0,
                thinking_mode="auto",
                cache_path=str(
                    request.output_path.with_name("result.judge-cache.jsonl")
                ),
                provider_error_threshold=3,
            )
            analyzer_factory.assert_called_once_with(
                judge=transport,
                max_depth=7,
                max_nodes=11,
            )
            analyzer.analyze.assert_called_once()
            call = analyzer.analyze.call_args
            self.assertEqual(call.kwargs["objective"], "Why did the compiler fail?")
            self.assertEqual(
                call.kwargs["start_refs"],
                ("record:compile", "record:compile", "record:tests"),
            )
            self.assertEqual(result.output_path, request.output_path)
            self.assertEqual(result.question, request.question_binding)
            self.assertEqual(json.loads(trace_path.read_text(encoding="utf-8")), trace)

    def test_recursive_service_preserves_checkpoint_budget_and_output_transaction(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            graph = TraceGraph.from_trace(
                {
                    "case_id": "recursive-service",
                    "records": [
                        {
                            "record_id": "failure",
                            "component": "evaluation",
                            "event_type": "case.failed",
                            "title": "Validation failed",
                        }
                    ],
                }
            )
            request = AttributionRequest(
                trace_path=root / "trace.json",
                output_path=root / "result.json",
                question="Why did validation fail?",
                options=AttributionOptions(
                    engine="recursive-agentic",
                    max_depth=6,
                    max_frontier_items=7,
                    max_hypotheses=5,
                    max_investigation_rounds=4,
                    max_artifact_bytes=1024,
                    max_judge_requests=3,
                ),
            )
            transport = mock.Mock(
                model="offline-model",
                base_url="offline://judge",
                thinking_config={"type": "disabled"},
                max_tokens=4096,
                provider_error_threshold=3,
                cache=mock.Mock(),
            )
            report = mock.Mock(metadata={})
            analyzer = mock.Mock()
            analyzer.analyze.return_value = report
            checkpoint = mock.Mock()
            checkpoint.completed_replay_output_commit.return_value = None
            shutdown = mock.MagicMock()
            shutdown.stop_requested = mock.Mock(return_value=False)
            signal_context = mock.MagicMock()
            signal_context.__enter__.return_value = shutdown
            report_payload = {
                "case_id": "recursive-service",
                "objective": request.effective_objective,
            }

            with mock.patch.object(
                service, "load_graph", return_value=graph
            ), mock.patch.object(
                service, "ClaudeJudgeClient", return_value=transport
            ), mock.patch.object(
                service, "ClaudeCausalJudge", return_value=mock.sentinel.causal_judge
            ), mock.patch.object(
                service, "CheckpointBundle", return_value=checkpoint
            ), mock.patch.object(
                service, "GracefulSignalState", return_value=signal_context
            ), mock.patch.object(
                service, "AgenticRecursiveAnalyzer", return_value=analyzer
            ) as analyzer_factory, mock.patch.object(
                service, "build_checkpoint_config", return_value={"config": "offline"}
            ) as build_config, mock.patch.object(
                service,
                "publish_output_transaction",
                return_value={
                    "transaction_id": "tx",
                    "output_commit_hash": "hash",
                },
            ) as publish, mock.patch.object(
                service,
                "attribution_output_payload",
                return_value=report_payload,
            ):
                result = service.analyze(request)

            config_call = build_config.call_args.kwargs
            self.assertEqual(config_call["objective"], "Why did validation fail?")
            question_seed = "record:offline_question_{0}".format(
                request.question_binding.question_id[:20]
            )
            self.assertEqual(config_call["start_refs"], (question_seed,))
            self.assertEqual(
                config_call["budgets"],
                {
                    "max_frontier_items": 7,
                    "max_depth": 6,
                    "max_hypotheses": 5,
                    "max_investigation_rounds": 4,
                    "max_artifact_bytes": 1024,
                    "max_judge_requests": 3,
                },
            )
            self.assertEqual(analyzer_factory.call_args.kwargs["max_depth"], 6)
            self.assertEqual(
                analyzer_factory.call_args.kwargs["max_judge_requests"], 3
            )
            self.assertEqual(
                analyzer.analyze.call_args.kwargs["objective"],
                "Why did validation fail?",
            )
            publish.assert_called_once()
            checkpoint.mark_analysis_completed.assert_called_once()
            published_report = publish.call_args.kwargs["report"]
            completed_report = checkpoint.mark_analysis_completed.call_args.kwargs[
                "report"
            ]
            self.assertEqual(published_report, completed_report)
            self.assertEqual(result.to_dict()["payload"], published_report)
            self.assertEqual(
                published_report["analysis_question"]["selected_start_refs"],
                [question_seed],
            )
            self.assertEqual(result.output_path, request.output_path)
            self.assertEqual(result.question, request.question_binding)

    def test_cli_and_python_recursive_runs_share_projection_with_real_output_transaction(self) -> None:
        from trace_attribution import cli, service

        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "recursive_cases"
            / "sphinx_recursive_minimal.json"
        ).resolve()
        original_trace_bytes = fixture.read_bytes()
        question = "为什么本次修改编译失败？"

        class OfflineRecursiveAnalyzer:
            def __init__(self, **kwargs: object) -> None:
                self.checkpoint = kwargs["checkpoint"]
                self.checkpoint_config = kwargs["checkpoint_config"]

            def analyze(
                self,
                graph: TraceGraph,
                *,
                start_refs: tuple[str, ...],
                objective: str,
                analysis_perspective: str,
            ) -> RecursiveAttributionReport:
                del start_refs, analysis_perspective
                self.checkpoint.initialize(self.checkpoint_config)
                return RecursiveAttributionReport(
                    case_id=graph.case_id,
                    objective=objective,
                )

        def projection(payload: dict[str, object]) -> dict[str, object]:
            return {
                key: payload[key]
                for key in (
                    "conclusion",
                    "causal_chain",
                    "supporting_evidence_refs",
                    "rejected_hypotheses",
                    "confidence",
                    "unresolved_gaps",
                )
            }

        options = {
            "engine": "recursive-agentic",
            "model": "offline-smoke",
            "thinking_mode": "disabled",
            "judge_timeout_sec": 1,
            "max_frontier_items": 4,
            "max_depth": 2,
            "max_hypotheses": 2,
            "max_investigation_rounds": 1,
            "max_artifact_bytes": 1024,
            "max_judge_requests": 1,
        }

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            cli_output = root / "cli.attribution.json"
            api_output = root / "api.attribution.json"
            cli_argv = [
                "trace-attribution",
                "--engine",
                "recursive-agentic",
                "--trace",
                str(fixture),
                "--out",
                str(cli_output),
                "--question",
                question,
                "--model",
                "offline-smoke",
                "--thinking-mode",
                "disabled",
                "--judge-timeout-sec",
                "1",
                "--max-frontier-items",
                "4",
                "--max-depth",
                "2",
                "--max-hypotheses",
                "2",
                "--max-investigation-rounds",
                "1",
                "--max-artifact-bytes",
                "1024",
                "--max-judge-requests",
                "1",
            ]
            transport = mock.Mock(
                model="offline-smoke",
                base_url="offline://smoke",
                thinking_config={"type": "disabled"},
                max_tokens=64,
                provider_error_threshold=1,
                cache=None,
            )

            with mock.patch.object(
                service, "ClaudeJudgeClient", return_value=transport
            ), mock.patch.object(
                service, "AgenticRecursiveAnalyzer", OfflineRecursiveAnalyzer
            ), mock.patch.object(sys, "argv", cli_argv), contextlib.redirect_stdout(
                io.StringIO()
            ) as stdout:
                self.assertEqual(cli.main(), 0)

            api_request = AttributionRequest(
                trace_path=fixture,
                output_path=api_output,
                question=question,
                options=AttributionOptions(**options),
            )
            with mock.patch.object(
                service, "ClaudeJudgeClient", return_value=transport
            ), mock.patch.object(
                service, "AgenticRecursiveAnalyzer", OfflineRecursiveAnalyzer
            ):
                api_result = service.analyze(api_request)

            self.assertEqual(
                stdout.getvalue(),
                str(cli_output)
                + "\n"
                + str(cli_output.with_name(cli_output.stem + ".explanation.md"))
                + "\n",
            )
            self.assertTrue(cli_output.is_file())
            self.assertTrue(api_result.output_path.is_file())
            self.assertTrue(
                (root / "cli.attribution.checkpoint" / "output-commit.json").is_file()
            )
            self.assertTrue(
                (root / "api.attribution.checkpoint" / "output-commit.json").is_file()
            )
            self.assertTrue((root / "cli.attribution.message-lineage.json").is_file())
            self.assertTrue((root / "api.attribution.message-lineage.json").is_file())

            cli_payload = json.loads(cli_output.read_text(encoding="utf-8"))
            api_payload = json.loads(api_output.read_text(encoding="utf-8"))
            self.assertEqual(projection(cli_payload), projection(api_payload))
            self.assertEqual(
                cli_payload["analysis_question"],
                api_payload["analysis_question"],
            )
            self.assertEqual(
                cli_payload["analysis_question"]["question_id"],
                api_payload["analysis_question"]["question_id"],
            )
            self.assertEqual(
                cli_payload["analysis_question"]["selected_start_refs"],
                api_payload["analysis_question"]["selected_start_refs"],
            )
            self.assertEqual(
                cli_payload["analysis_question"]["selected_start_refs"],
                [
                    "record:offline_question_{0}".format(
                        api_request.question_binding.question_id[:20]
                    )
                ],
            )
            self.assertEqual(fixture.read_bytes(), original_trace_bytes)

    def test_question_payload_binds_trace_and_projects_only_existing_report_facts(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        raw_trace = {
            "case_id": "question-projection",
            "records": [
                {
                    "record_id": "observed",
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
        graph = TraceGraph.from_trace(raw_trace)
        binding = AttributionQuestion.create(" Why did validation fail? ")
        report = {
            "analysis_outcome": "root_found",
            "confirmed_roots": [
                {
                    "node_ref": "record:root",
                    "reason": "The validation rule rejects the required input.",
                    "confidence": 0.75,
                    "evidence_refs": ["record:observed", "missing:evidence"],
                }
            ],
            "taint_paths": [["record:root", "record:observed"]],
            "rejected_candidates": [
                {
                    "node_ref": "record:other",
                    "reason": "No causal support.",
                    "evidence_refs": ["record:observed"],
                }
            ],
            "hypotheses": [
                {"hypothesis_id": "kept", "status": "supported"},
                {"hypothesis_id": "rejected", "status": "rejected"},
            ],
            "unresolved_refs": ["record:unknown"],
            "metadata": {
                "trace_improvement_report": {
                    "blocking_gaps": [{"gap_type": "missing_causal_edge"}],
                    "analysis_gaps": [{"gap_type": "unresolved_competitor"}],
                }
            },
        }

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=binding,
            starts=("record:observed",),
        )

        self.assertEqual(
            payload["analysis_question"],
            {
                "schema_version": "attribution-question/v1",
                "question": " Why did validation fail? ",
                "normalized_question": "Why did validation fail?",
                "question_id": binding.question_id,
                "trace_binding": "sha256:"
                + hashlib.sha256(stable_json(raw_trace).encode("utf-8")).hexdigest(),
                "selected_start_refs": ["record:observed"],
                "analysis_mode": "offline_read_only",
            },
        )
        self.assertEqual(
            payload["conclusion"],
            "record:root: The validation rule rejects the required input.",
        )
        self.assertEqual(
            payload["causal_chain"], [["record:root", "record:observed"]],
        )
        self.assertEqual(payload["supporting_evidence_refs"], ["record:observed"])
        self.assertNotIn(binding.original, payload["supporting_evidence_refs"])
        self.assertEqual(
            payload["rejected_hypotheses"],
            [
                report["rejected_candidates"][0],
                report["hypotheses"][1],
            ],
        )
        self.assertEqual(payload["confidence"], 0.75)
        self.assertEqual(
            payload["unresolved_gaps"],
            [
                {"kind": "unresolved_ref", "ref": "record:unknown"},
                {
                    "kind": "trace_improvement_gap",
                    "category": "blocking_gaps",
                    "detail": {"gap_type": "missing_causal_edge"},
                },
                {
                    "kind": "trace_improvement_gap",
                    "category": "analysis_gaps",
                    "detail": {"gap_type": "unresolved_competitor"},
                },
                {
                    "kind": "root_evidence_ref_rejected",
                    "root_ref": "record:root",
                    "ref": "missing:evidence",
                    "reason": "missing_or_ineligible",
                },
            ],
        )
        self.assertEqual(graph.raw_trace, raw_trace)

    def test_question_payload_projects_all_roots_and_eligible_artifact_evidence(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            artifact_bytes = b"verified artifact evidence"
            artifact_path = root / "artifacts" / "proof.txt"
            artifact_path.parent.mkdir()
            artifact_path.write_bytes(artifact_bytes)
            graph = TraceGraph.from_trace(
                {
                    "case_id": "question-all-roots",
                    "artifacts": [
                        {
                            "artifact_id": "proof",
                            "kind": "text",
                            "path": "artifacts/proof.txt",
                            "hash": "sha256:"
                            + hashlib.sha256(artifact_bytes).hexdigest(),
                            "byte_length": len(artifact_bytes),
                        }
                    ],
                    "records": [
                        {
                            "record_id": "root_one",
                            "component": "implementation",
                            "event_type": "change.applied",
                            "artifact_refs": ["artifact:proof"],
                            "title": "First root",
                        },
                        {
                            "record_id": "root_two",
                            "component": "implementation",
                            "event_type": "change.applied",
                            "title": "Second root",
                        },
                        {
                            "record_id": "start_one",
                            "component": "evaluation",
                            "event_type": "case.observed_defect",
                            "title": "First failure",
                        },
                        {
                            "record_id": "start_two",
                            "component": "evaluation",
                            "event_type": "case.observed_defect",
                            "title": "Second failure",
                        },
                        {
                            "record_id": "unrelated",
                            "component": "tool",
                            "event_type": "tool.result",
                            "title": "Unrelated path node",
                        },
                    ],
                },
                artifact_root=root,
            )
            report = {
                "analysis_outcome": "root_found",
                "confirmed_roots": [
                    {
                        "node_ref": "record:root_one",
                        "semantic_occurrence_id": "occurrence:one",
                        "reason": "First independently confirmed root.",
                        "confidence": 0.4,
                        "evidence_refs": [
                            "artifact:proof",
                            "record:start_one",
                            "record:missing",
                        ],
                    }
                ],
                "co_roots": [
                    {
                        "node_ref": "record:root_one",
                        "semantic_occurrence_id": "occurrence:one",
                        "reason": "Duplicate publication must not replace the first root.",
                        "confidence": 1.0,
                        "evidence_refs": ["record:unrelated"],
                    },
                    {
                        "node_ref": "record:root_two",
                        "confirmation": {"confirmation_identity": "confirmation:two"},
                        "reason": "Second independently confirmed root.",
                        "confidence": 0.9,
                        "evidence_refs": ["record:start_two", "missing:evidence"],
                    },
                ],
                "taint_paths": [
                    ["record:root_one", "record:start_one"],
                    ["record:start_two", "record:root_two"],
                    ["record:root_one", "record:unrelated"],
                    ["record:unrelated", "record:start_one"],
                ],
            }

            payload = service.question_bound_output_payload(
                report,
                graph,
                binding=AttributionQuestion.create("Why did both failures happen?"),
                starts=("record:start_one", "record:start_two"),
            )

        self.assertEqual(
            payload["conclusion"],
            "record:root_one: First independently confirmed root.\n"
            "record:root_two: Second independently confirmed root.",
        )
        self.assertEqual(payload["confidence"], 0.9)
        self.assertEqual(
            payload["supporting_evidence_refs"],
            ["artifact:proof", "record:start_one", "record:start_two"],
        )
        self.assertEqual(
            payload["causal_chain"],
            [
                ["record:root_one", "record:start_one"],
                ["record:start_two", "record:root_two"],
            ],
        )

    def test_question_payload_zeroes_confidence_and_records_missing_root_evidence(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-missing-root-evidence",
                "records": [
                    {
                        "record_id": "root",
                        "component": "implementation",
                        "event_type": "change.applied",
                        "title": "Unsupported root",
                    },
                    {
                        "record_id": "observed",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Observed failure",
                    },
                ],
            }
        )
        report = {
            "analysis_outcome": "root_found",
            "confirmed_roots": [
                {
                    "node_ref": "record:root",
                    "reason": "The report claims this is the root.",
                    "confidence": 0.99,
                    "evidence_refs": ["record:missing"],
                }
            ],
            "taint_paths": [["record:root", "record:observed"]],
        }

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=AttributionQuestion.create("Why did the result fail?"),
            starts=("record:observed",),
        )

        self.assertEqual(payload["supporting_evidence_refs"], [])
        self.assertEqual(payload["confidence"], 0.0)
        self.assertIn(
            {
                "kind": "root_evidence_ref_rejected",
                "root_ref": "record:root",
                "ref": "record:missing",
                "reason": "missing_or_ineligible",
            },
            payload["unresolved_gaps"],
        )
        self.assertIn(
            {
                "kind": "root_without_eligible_supporting_evidence",
                "root_ref": "record:root",
            },
            payload["unresolved_gaps"],
        )
        self.assertEqual(report["confirmed_roots"][0]["confidence"], 0.99)
        self.assertEqual(report["confirmed_roots"][0]["evidence_refs"], ["record:missing"])

    def test_legacy_question_projection_uses_observed_defect_refs_as_evidence(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        graph = TraceGraph.from_trace(
            {
                "case_id": "legacy-question-projection",
                "records": [
                    {
                        "record_id": "root",
                        "component": "implementation",
                        "event_type": "change.applied",
                        "title": "Incorrect implementation",
                    },
                    {
                        "record_id": "observed",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Compilation failed",
                    },
                ],
            }
        )
        report = {
            "root_causes": [
                {
                    "node_ref": "record:root",
                    "component": "implementation",
                    "event_type": "change.applied",
                    "defect_type": "incorrect_change",
                    "reason": "The implementation violated the build contract.",
                    "confidence": 0.82,
                    "observed_defect_refs": ["record:observed", "record:missing"],
                }
            ],
            "taint_paths": [["record:root", "record:observed"]],
        }

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=AttributionQuestion.create("Why did compilation fail?"),
            starts=("record:observed",),
        )

        self.assertEqual(
            payload["conclusion"],
            "record:root: The implementation violated the build contract.",
        )
        self.assertEqual(payload["causal_chain"], [["record:root", "record:observed"]])
        self.assertEqual(payload["supporting_evidence_refs"], ["record:observed"])
        self.assertEqual(payload["confidence"], 0.82)
        self.assertIn(
            {
                "kind": "root_evidence_ref_rejected",
                "root_ref": "record:root",
                "ref": "record:missing",
                "reason": "missing_or_ineligible",
            },
            payload["unresolved_gaps"],
        )
        self.assertNotIn("evidence_refs", report["root_causes"][0])

    def test_default_legacy_cli_question_projects_root_cause_schema(self) -> None:
        from trace_attribution import cli, service

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            output_path = root / "result.json"
            trace_path.write_text(
                json.dumps(
                    {
                        "case_id": "legacy-cli-question",
                        "records": [
                            {
                                "record_id": "root",
                                "component": "implementation",
                                "event_type": "change.applied",
                                "title": "Incorrect implementation",
                            },
                            {
                                "record_id": "observed",
                                "component": "evaluation",
                                "event_type": "case.observed_defect",
                                "title": "Compilation failed",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report = {
                "case_id": "legacy-cli-question",
                "objective": "Why did compilation fail?",
                "start_refs": ["record:observed"],
                "root_causes": [
                    {
                        "node_ref": "record:root",
                        "component": "implementation",
                        "event_type": "change.applied",
                        "defect_type": "incorrect_change",
                        "reason": "The implementation violated the build contract.",
                        "confidence": 0.82,
                        "observed_defect_refs": ["record:observed"],
                    }
                ],
                "taint_paths": [["record:root", "record:observed"]],
                "node_judgments": {},
                "visited_order": ["record:observed", "record:root"],
                "unresolved_refs": [],
                "defect_branches": [],
                "trace_improvement_report": {},
                "metadata": {},
            }
            analyzer = mock.Mock()
            analyzer.analyze.return_value = mock.Mock(to_dict=lambda: report)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(
                service, "ClaudeJudgeClient", return_value=mock.sentinel.transport
            ), mock.patch.object(
                service, "BackwardTaintAnalyzer", return_value=analyzer
            ), mock.patch.object(
                sys,
                "argv",
                [
                    "trace-attribution",
                    "--trace",
                    str(trace_path),
                    "--out",
                    str(output_path),
                    "--question",
                    "Why did compilation fail?",
                    "--start-ref",
                    "record:observed",
                ],
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(), 0)

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                stdout.getvalue(),
                str(output_path)
                + "\n"
                + str(output_path.with_name(output_path.stem + ".explanation.md"))
                + "\n",
            )
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                payload["conclusion"],
                "record:root: The implementation violated the build contract.",
            )
            self.assertEqual(payload["supporting_evidence_refs"], ["record:observed"])
            self.assertEqual(payload["confidence"], 0.82)
            self.assertEqual(payload["causal_chain"], [["record:root", "record:observed"]])

    def test_cli_reports_ineligible_start_ref_without_traceback(self) -> None:
        from trace_attribution import cli

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            output_path = root / "result.json"
            trace_path.write_text(
                json.dumps(
                    {
                        "case_id": "cli-ineligible-start",
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
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                sys,
                "argv",
                [
                    "trace-attribution",
                    "--trace",
                    str(trace_path),
                    "--out",
                    str(output_path),
                    "--question",
                    "Why did evaluation fail?",
                    "--start-ref",
                    "record:evaluation",
                ],
            ):
                with self.assertRaisesRegex(SystemExit, "^error: .*ineligible.*decisive judgment$"):
                    cli.main()

            self.assertFalse(output_path.exists())

    def test_question_payload_uses_start_paths_without_roots_and_deep_copies(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-no-root-paths",
                "records": [
                    {
                        "record_id": "start",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Failure",
                    },
                    {
                        "record_id": "other",
                        "component": "tool",
                        "event_type": "tool.result",
                        "title": "Other",
                    },
                ],
            }
        )
        report = {
            "analysis_outcome": "inconclusive",
            "taint_paths": [["record:start", "record:other"], ["record:other"]],
            "nested": {"items": [{"value": "original"}]},
        }

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=AttributionQuestion.create("Why did it fail?"),
            starts=iter(("record:start",)),
        )
        payload["nested"]["items"][0]["value"] = "changed"

        self.assertEqual(payload["causal_chain"], [["record:start", "record:other"]])
        self.assertEqual(
            payload["unresolved_gaps"],
            [
                {
                    "kind": "no_confirmed_root_cause",
                    "reason": "evidence_insufficient",
                }
            ],
        )
        self.assertEqual(report["nested"]["items"][0]["value"], "original")

    def test_question_payload_is_explicit_when_no_root_is_confirmed(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        graph = TraceGraph.from_trace(
            {
                "case_id": "question-no-defect",
                "records": [
                    {
                        "record_id": "observed",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Validation failed",
                    }
                ],
            }
        )

        payload = service.question_bound_output_payload(
            {"analysis_outcome": "no_defect", "unresolved_refs": ["record:unknown"]},
            graph,
            binding=AttributionQuestion.create("Why did validation fail?"),
            starts=("record:observed",),
        )

        self.assertEqual(
            payload["conclusion"],
            (
                "The user-reported deviation is not supported by the recorded "
                "trace facts; no defect or root cause was confirmed."
            ),
        )
        self.assertEqual(payload["question_premise_status"], "contradicted")
        self.assertEqual(payload["confidence"], 0.0)
        self.assertEqual(
            payload["unresolved_gaps"],
            [
                {"kind": "unresolved_ref", "ref": "record:unknown"},
            ],
        )

    def test_objective_mode_payload_shape_is_unchanged(self) -> None:
        service = importlib.import_module("trace_attribution.service")
        graph = TraceGraph.from_trace(
            {
                "case_id": "objective-compatibility",
                "records": [
                    {
                        "record_id": "observed",
                        "component": "evaluation",
                        "event_type": "case.observed_defect",
                        "title": "Validation failed",
                    }
                ],
            }
        )
        report = {
            "case_id": "objective-compatibility",
            "objective": "Find the root cause.",
            "root_causes": [],
        }

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=None,
            starts=("record:observed",),
        )

        self.assertEqual(payload, report)
        self.assertNotIn("analysis_question", payload)
        self.assertNotIn("conclusion", payload)
        payload["root_causes"].append({"node_ref": "record:mutated"})
        self.assertEqual(report["root_causes"], [])

    def test_legacy_service_integration_preserves_trace_and_writes_real_outputs(self) -> None:
        service = importlib.import_module("trace_attribution.service")

        class OfflineFakeJudge:
            timeout_seconds = 3600.0
            model = "offline-fake"
            request_count = 0
            thinking_mode = "disabled"
            thinking_config = {"type": "disabled"}
            cache_stats = {"enabled": False}
            provider_circuit_stats = {"open": False}

            def __init__(self) -> None:
                self.objectives: list[str] = []

            def judge_node(self, *, node, upstream_nodes, downstream_context, objective):
                self.request_count += 1
                self.objectives.append(objective)
                return NodeJudgment(
                    node_ref=node.ref,
                    component=node.component,
                    event_type=node.event_type,
                    has_defect=False,
                    defect_status="absent",
                    defect_reason="Offline fake found no defect.",
                )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            trace_bytes = json.dumps(
                {
                    "case_id": "legacy-integration",
                    "records": [
                        {
                            "record_id": "failed",
                            "component": "result",
                            "event_type": "case.failed",
                            "title": "Compilation failed",
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            trace_path.write_bytes(trace_bytes)
            trace_hash = hashlib.sha256(trace_bytes).hexdigest()
            request = AttributionRequest(
                trace_path=trace_path,
                output_path=root / "result.json",
                question="  Why did compilation fail?  ",
            )
            fake_judge = OfflineFakeJudge()
            loaded_graphs = []
            load_graph = service.load_graph

            def capture_graph(*args, **kwargs):
                graph = load_graph(*args, **kwargs)
                loaded_graphs.append(graph)
                return graph

            with mock.patch.object(
                service,
                "ClaudeJudgeClient",
                return_value=fake_judge,
            ), mock.patch.object(
                service,
                "load_graph",
                side_effect=capture_graph,
            ):
                result = service.analyze(request)

            self.assertEqual(fake_judge.objectives, ["Why did compilation fail?"])
            self.assertEqual(result.payload["objective"], "Why did compilation fail?")
            self.assertEqual(
                json.loads(result.output_path.read_text(encoding="utf-8"))["objective"],
                "Why did compilation fail?",
            )
            self.assertEqual(
                result.payload["analysis_question"]["analysis_mode"],
                "offline_read_only",
            )
            self.assertIn("conclusion", result.payload)
            self.assertIn("supporting_evidence_refs", result.payload)
            self.assertIn("unresolved_gaps", result.payload)
            self.assertTrue(result.lineage_path.is_file())
            self.assertEqual(
                hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                trace_hash,
            )
            raw_trace_hash = hashlib.sha256(
                stable_json(loaded_graphs[0].raw_trace).encode("utf-8")
            ).hexdigest()
            self.assertEqual(
                result.payload["analysis_question"]["trace_binding"],
                "sha256:" + raw_trace_hash,
            )
            self.assertNotEqual(trace_hash, raw_trace_hash)


if __name__ == "__main__":
    unittest.main()
