from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from trace_attribution.benchmark_composition import compose_effective_trace
from trace_attribution.artifact_reader import VerifiedArtifactReader
from trace_attribution.claude import ClaudeJudgeClient
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph, artifact_root_for_trace_path
from trace_attribution.models import stable_json
from trace_attribution.quality_review import inject_quality_gap_records


REAL_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def trace_payload():
    return {
        "manifest": {
            "case_id": "bundle-case",
            "run_id": "bundle-run",
            "subject_revision": "git:abc123",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "bundle-case",
                "run_id": "bundle-run",
            },
        },
        "records": [
            {
                "record_id": "tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "bundle-probe"},
            }
        ],
        "dataflow_edges": [],
    }


def review_payload():
    return {
        "case_id": "bundle-case",
        "quality_review": {
            "quality_gaps": [
                {
                    "dimension": "correctness",
                    "score": 0,
                    "max_score": 1,
                    "gap_context_refs": ["record:tool_result"],
                }
            ]
        },
    }


def evaluation_payload(assertion: str):
    return {
        "source": "benchmark",
        "scope": "bundle-order",
        "subject_revision": "git:abc123",
        "assertion": assertion,
        "observation": "failed",
        "status": "failed",
        "observed_at": "2026-08-03T00:00:00Z",
        "evidence_refs": ["record:tool_result"],
        "provenance": {"method": "offline_test", "version": "1"},
    }


def structured_review_payload(*evidence_refs: str):
    return {
        "case_id": "bundle-case",
        "observed_defects": [
            {
                "failure_signature_schema_version": (
                    "evaluation-failure-signature/v1"
                ),
                "exception_family": "AssertionError",
                "first_business_frame": {
                    "file": "benchmark/check.py",
                    "symbol": "verify_bundle",
                },
                "assertion_contract": "the benchmark closure must replay",
                "relevant_symbol": "verify_bundle",
                "subsystem": "benchmark.bundle",
                "evidence_refs": list(evidence_refs),
            }
        ],
    }


def bundle_digest(data: bytes) -> str:
    return "sha256:{0}".format(hashlib.sha256(data).hexdigest())


def canonical_bytes(value) -> bytes:
    return stable_json(value).encode("utf-8")


def bundle_binding(path: str, data: bytes, *, source_index=None):
    binding = {
        "path": path,
        "sha256": bundle_digest(data),
        "byte_length": len(data),
        "media_type": "application/json",
    }
    if source_index is not None:
        binding["source_index"] = source_index
    return binding


def bundle_member(role: str, binding):
    return {
        "role": role,
        "path": binding["path"],
        "sha256": binding["sha256"],
        "byte_length": binding["byte_length"],
        "media_type": binding["media_type"],
    }


def rewrite_bundle_manifest(root: Path, manifest) -> None:
    identity = copy.deepcopy(manifest)
    identity.pop("bundle_id", None)
    manifest["bundle_id"] = bundle_digest(canonical_bytes(identity))
    (root / "bundle.json").write_bytes(canonical_bytes(manifest))


def replace_bound_member(root: Path, manifest, path: str, data: bytes) -> None:
    (root / path).write_bytes(data)
    digest = bundle_digest(data)
    for member in manifest["members"]:
        if member["path"] == path:
            member["sha256"] = digest
            member["byte_length"] = len(data)
    bindings = [
        manifest["sources"]["trace"],
        manifest["sources"]["review"],
        manifest["sources"]["labels"],
        manifest["effective_trace"],
        *manifest["sources"]["evaluations"],
    ]
    for binding in bindings:
        if binding is not None and binding["path"] == path:
            binding["sha256"] = digest
            binding["byte_length"] = len(data)
    for artifact in manifest["artifacts"]:
        if artifact["member_path"] == path:
            artifact["content_sha256"] = digest
            artifact["byte_length"] = len(data)
    rewrite_bundle_manifest(root, manifest)


def create_manual_bundle(
    root: Path,
    *,
    artifact: bool = False,
    artifact_bytes: bytes = b"immutable artifact bytes",
    evaluations: int = 0,
    labels: bool = False,
    review: bool = False,
):
    root.mkdir(parents=True)
    source_trace = trace_payload()
    artifact_binding = None
    artifact_member = None
    if artifact:
        artifact_sha256 = bundle_digest(artifact_bytes)
        artifact_hex = artifact_sha256.removeprefix("sha256:")
        artifact_path = "artifacts/sha256/{0}/{1}".format(
            artifact_hex[:2], artifact_hex
        )
        source_trace["artifacts"] = [
            {
                "artifact_id": "proof",
                "path": "raw/proof.txt",
                "content_hash": artifact_sha256[:23],
                "byte_length": len(artifact_bytes),
                "media_type": "text/plain",
            }
        ]
        source_trace["records"][0]["artifact_refs"] = ["artifact:proof"]
        artifact_binding = {
            "artifact_id": "proof",
            "source_relative_path": "raw/proof.txt",
            "declared_content_hash": artifact_sha256[:23],
            "member_path": artifact_path,
            "content_sha256": artifact_sha256,
            "byte_length": len(artifact_bytes),
            "media_type": "text/plain",
        }
        artifact_member = {
            "role": "artifact",
            "path": artifact_path,
            "sha256": artifact_sha256,
            "byte_length": len(artifact_bytes),
            "media_type": "text/plain",
        }
        target = root / artifact_path
        target.parent.mkdir(parents=True)
        target.write_bytes(artifact_bytes)

    review_value = review_payload() if review else None
    evaluation_values = [
        evaluation_payload("evaluation {0}".format(index))
        for index in range(evaluations)
    ]
    effective_trace = compose_effective_trace(
        source_trace,
        review=review_value,
        evaluations=evaluation_values,
    )
    if artifact_binding is not None:
        effective_trace["artifacts"] = [
            {
                "artifact_id": "proof",
                "path": artifact_binding["member_path"],
                "content_hash": artifact_binding["content_sha256"],
                "byte_length": artifact_binding["byte_length"],
                "media_type": artifact_binding["media_type"],
            }
        ]
    source_trace_bytes = canonical_bytes(source_trace)
    effective_trace_bytes = canonical_bytes(effective_trace)
    paths_and_bytes = [
        ("inputs/trace.json", source_trace_bytes),
        ("effective/trace.json", effective_trace_bytes),
    ]
    trace_binding = bundle_binding("inputs/trace.json", source_trace_bytes)
    effective_binding = bundle_binding(
        "effective/trace.json", effective_trace_bytes
    )
    review_binding = None
    if review_value is not None:
        review_bytes = canonical_bytes(review_value)
        review_binding = bundle_binding("inputs/review.json", review_bytes)
        paths_and_bytes.append((review_binding["path"], review_bytes))
    evaluation_bindings = []
    for index, value in enumerate(evaluation_values):
        evaluation_bytes = canonical_bytes(value)
        evaluation_sha256 = bundle_digest(evaluation_bytes)
        evaluation_path = "inputs/evaluations/{0:04d}-{1}.json".format(
            index,
            evaluation_sha256.removeprefix("sha256:"),
        )
        binding = bundle_binding(
            evaluation_path,
            evaluation_bytes,
            source_index=index,
        )
        evaluation_bindings.append(binding)
        paths_and_bytes.append((evaluation_path, evaluation_bytes))
    labels_binding = None
    if labels:
        labels_bytes = b"labels bytes are intentionally not parsed"
        labels_binding = bundle_binding("inputs/labels.json", labels_bytes)
        paths_and_bytes.append((labels_binding["path"], labels_bytes))
    for relative_path, data in paths_and_bytes:
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    members = [
        bundle_member("source_trace", trace_binding),
        bundle_member("effective_trace", effective_binding),
    ]
    if review_binding is not None:
        members.append(bundle_member("source_review", review_binding))
    members.extend(
        bundle_member("source_evaluation", binding)
        for binding in evaluation_bindings
    )
    if labels_binding is not None:
        members.append(bundle_member("source_labels", labels_binding))
    if artifact_member is not None:
        members.append(artifact_member)
    members.sort(key=lambda item: item["path"])
    manifest = {
        "schema_version": "benchmark-trace-bundle/v1",
        "bundle_id": "",
        "composition_contract": "benchmark-trace-composition/v1",
        "case_id": "bundle-case",
        "run_id": "bundle-run",
        "subject_revision": "git:abc123",
        "revision_provenance_status": "valid",
        "sources": {
            "trace": trace_binding,
            "review": review_binding,
            "evaluations": evaluation_bindings,
            "labels": labels_binding,
        },
        "effective_trace": effective_binding,
        "artifacts": [artifact_binding] if artifact_binding is not None else [],
        "members": members,
        "counts": {
            "source_count": (
                1
                + int(review_binding is not None)
                + len(evaluation_bindings)
                + int(labels_binding is not None)
            ),
            "evaluation_count": len(evaluation_bindings),
            "declared_artifact_count": int(artifact_binding is not None),
            "referenced_artifact_count": int(artifact_binding is not None),
            "artifact_member_count": int(artifact_member is not None),
            "member_count": len(members),
        },
    }
    rewrite_bundle_manifest(root, manifest)
    return manifest


def open_manual_bundle(path: Path, *, role: str = "attribution"):
    module = importlib.import_module("trace_attribution.benchmark_bundle")
    return module.open_benchmark_trace_bundle(path, role=role)


class BenchmarkCompositionTest(unittest.TestCase):
    def test_composes_trace_review_then_evaluations_in_caller_order(self):
        composed = compose_effective_trace(
            trace_payload(),
            review=review_payload(),
            evaluations=(evaluation_payload("first"), evaluation_payload("second")),
        )

        self.assertEqual(
            [record["event_type"] for record in composed["records"]],
            [
                "tool.result",
                "case.quality_gap",
                "external.evaluation_fact",
                "external.evaluation_fact",
            ],
        )
        self.assertEqual(
            [record["data"]["assertion"] for record in composed["records"][-2:]],
            ["first", "second"],
        )

    def test_evaluation_order_changes_the_canonical_binding(self):
        first = evaluation_payload("first")
        second = evaluation_payload("second")

        forward = compose_effective_trace(trace_payload(), evaluations=(first, second))
        reverse = compose_effective_trace(trace_payload(), evaluations=(second, first))

        self.assertNotEqual(stable_json(forward), stable_json(reverse))

    def test_returns_canonical_json_without_mutating_inputs(self):
        trace = trace_payload()
        review = review_payload()
        evaluations = [evaluation_payload("one")]
        before = copy.deepcopy((trace, review, evaluations))

        composed = compose_effective_trace(trace, review=review, evaluations=evaluations)

        self.assertEqual((trace, review, evaluations), before)
        self.assertEqual(composed, json.loads(stable_json(composed)))

    def test_returns_recursively_canonical_dictionary_key_order(self):
        trace = trace_payload()
        trace["records"][0]["data"] = {
            "zeta": {"right": 2, "left": 1},
            "alpha": {"z": 2, "a": 1},
        }
        trace["zeta"] = {"right": 2, "left": 1}
        trace["alpha"] = {"z": 2, "a": 1}

        composed = compose_effective_trace(trace)

        self.assertEqual(
            json.dumps(composed, ensure_ascii=False, default=str),
            stable_json(composed),
        )

    def test_rejects_duplicate_declared_artifact_ids(self):
        trace = trace_payload()
        trace["artifacts"] = [
            {"artifact_id": "duplicate", "path": "unreadable/one"},
            {"artifact_id": "duplicate", "path": "unreadable/two"},
        ]

        with self.assertRaisesRegex(ValueError, "duplicate artifact_id: duplicate"):
            compose_effective_trace(trace)

    def test_rejects_non_string_artifact_ids(self):
        for artifact_id in (None, 7, True, {"nested": "id"}):
            with self.subTest(artifact_id=artifact_id):
                trace = trace_payload()
                trace["artifacts"] = [
                    {"artifact_id": artifact_id, "path": "unreadable/value"},
                ]

                with self.assertRaisesRegex(ValueError, "artifact_id must be a string"):
                    compose_effective_trace(trace)

    def test_never_reads_declared_artifacts(self):
        trace = trace_payload()
        trace["artifacts"] = [
            {"artifact_id": "declared", "path": "/does/not/exist"},
        ]

        composed = compose_effective_trace(trace)

        self.assertEqual(composed["artifacts"], trace["artifacts"])

    def test_structured_review_composition_calls_no_graph_io_or_provider_boundary(self):
        original_from_trace = TraceGraph.from_trace
        original_reader_init = VerifiedArtifactReader.__init__
        with ExitStack() as stack:
            graph_from_trace = stack.enter_context(
                patch.object(
                    TraceGraph,
                    "from_trace",
                    side_effect=original_from_trace,
                )
            )
            reader_init = stack.enter_context(
                patch.object(
                    VerifiedArtifactReader,
                    "__init__",
                    autospec=True,
                    side_effect=original_reader_init,
                )
            )
            artifact_read = stack.enter_context(
                patch.object(VerifiedArtifactReader, "read")
            )
            infer_artifact_root = stack.enter_context(
                patch(
                    "trace_attribution.graph.artifact_root_for_trace_path",
                    wraps=artifact_root_for_trace_path,
                )
            )
            path_open = stack.enter_context(patch.object(Path, "open", autospec=True))
            builtin_open = stack.enter_context(patch("builtins.open"))
            provider_init = stack.enter_context(
                patch.object(ClaudeJudgeClient, "__init__", autospec=True)
            )
            composed = compose_effective_trace(
                trace_payload(),
                review=structured_review_payload("record:tool_result"),
            )

        self.assertEqual(composed["records"][-1]["source_refs"], ["record:tool_result"])
        graph_from_trace.assert_not_called()
        reader_init.assert_not_called()
        artifact_read.assert_not_called()
        infer_artifact_root.assert_not_called()
        path_open.assert_not_called()
        builtin_open.assert_not_called()
        provider_init.assert_not_called()

    def test_graph_free_review_matches_legacy_source_binding_semantics(self):
        cases = []

        canonical = trace_payload()
        cases.append(("canonical", canonical, "record:tool_result"))

        ambiguous = trace_payload()
        ambiguous["records"].append(
            {
                "record_id": "other_tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "bundle-probe"},
            }
        )
        cases.append(("ambiguous", ambiguous, "tool_result:bundle-probe"))

        revision_matched = trace_payload()
        revision_matched["records"][0]["data"].update(
            {
                "subject_revision": "git:abc123",
                "revision_status": "matched",
                "revision_provenance_status": "valid",
            }
        )
        cases.append(("revision_matched", revision_matched, "record:tool_result"))

        revision_mismatched = trace_payload()
        revision_mismatched["records"][0]["data"].update(
            {
                "subject_revision": "git:different",
                "revision_status": "mismatched",
                "revision_provenance_status": "valid",
            }
        )
        cases.append(("revision_mismatched", revision_mismatched, "record:tool_result"))

        original_from_trace = TraceGraph.from_trace
        for label, trace, evidence_ref in cases:
            with self.subTest(label=label):
                review = structured_review_payload(evidence_ref)
                legacy = inject_quality_gap_records(trace, review)
                with patch.object(
                    TraceGraph,
                    "from_trace",
                    side_effect=original_from_trace,
                ) as graph_from_trace:
                    composed = compose_effective_trace(trace, review=review)

                self.assertEqual(stable_json(composed), stable_json(legacy))
                graph_from_trace.assert_not_called()

    def test_graph_free_review_rejects_failure_signature_alias_collisions(self):
        trace = trace_payload()
        trace["records"].extend(
            [
                {
                    "record_id": record_id,
                    "event_type": "case.observed_defect",
                    "data": {
                        "failure_signature": {"signature_id": "duplicate-signature"}
                    },
                }
                for record_id in ("seed_one", "seed_two")
            ]
        )

        with self.assertRaisesRegex(
            ValueError,
            "failure_signature alias is ambiguous",
        ):
            compose_effective_trace(
                trace,
                review=structured_review_payload("record:tool_result"),
            )

    def test_distinct_evaluations_from_one_source_retain_caller_order(self):
        first = evaluation_payload("first")
        second = evaluation_payload("second")
        second["observed_at"] = "2026-08-03T00:00:01Z"

        composed = compose_effective_trace(
            trace_payload(),
            evaluations=(first, second),
        )

        facts = [
            record
            for record in composed["records"]
            if record["event_type"] == "external.evaluation_fact"
        ]
        self.assertEqual([record["data"]["source"] for record in facts], ["benchmark", "benchmark"])
        self.assertEqual([record["data"]["assertion"] for record in facts], ["first", "second"])

    def test_duplicate_evaluation_content_is_effective_fact_idempotent(self):
        evaluation = evaluation_payload("same content")

        once = compose_effective_trace(trace_payload(), evaluations=(evaluation,))
        twice = compose_effective_trace(
            trace_payload(),
            evaluations=(evaluation, copy.deepcopy(evaluation)),
        )

        self.assertEqual(twice, once)

    def test_cyclic_progress_membership_fails_closed_in_graph_and_composition(self):
        trace = trace_payload()
        trace["records"].extend(
            [
                {
                    "record_id": "episode_a",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:episode_b"]},
                },
                {
                    "record_id": "episode_b",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:episode_a"]},
                },
            ]
        )

        try:
            graph_eligible = TraceGraph.from_trace(
                copy.deepcopy(trace)
            ).active_revision_evidence_eligible("record:episode_a")
        except RecursionError:
            self.fail("cyclic graph progress membership raised RecursionError")
        self.assertFalse(graph_eligible)

        composed = compose_effective_trace(
            trace,
            review=structured_review_payload("record:episode_a"),
        )
        binding = composed["records"][-1]["data"]["source_binding"]
        self.assertEqual(binding["rejected_review_refs"], ["record:episode_a"])

    def test_shared_policy_and_alias_helpers_are_single_owners(self):
        spec = importlib.util.find_spec("trace_attribution.trace_eligibility")
        self.assertIsNotNone(spec, "shared trace eligibility module is missing")
        shared = importlib.import_module("trace_attribution.trace_eligibility")
        graph_module = importlib.import_module("trace_attribution.graph")
        quality_module = importlib.import_module("trace_attribution.quality_review")

        for name in ("record_aliases", "resolve_ref", "resolve_edge_endpoint"):
            self.assertIs(getattr(graph_module, name), getattr(shared, name))
        for name in (
            "subject_provenance_binding_eligible",
            "authority_value",
            "active_repository_revision",
            "active_revision_evidence_eligible",
        ):
            self.assertTrue(callable(getattr(shared.TraceEligibilityPolicy, name)))
        self.assertNotIn(
            "def subject_binding_eligible",
            inspect.getsource(
                quality_module.bind_structured_observed_defect_source_refs_graph_free
            ),
        )
        self.assertIn(
            "_eligibility_policy",
            inspect.getsource(graph_module.TraceGraph.active_revision_evidence_eligible),
        )

    def test_cold_composition_import_excludes_graph_io_reconstruction_and_provider_modules(self):
        package_dir = Path(__file__).parents[1] / "trace_attribution"
        script = """
import importlib
import json
import sys
import types

package = types.ModuleType("trace_attribution")
package.__path__ = [{package_dir!r}]
sys.modules["trace_attribution"] = package
importlib.import_module("trace_attribution.benchmark_composition")
forbidden = [
    name
    for name in sorted(sys.modules)
    if name in {{
        "trace_attribution.artifact_reader",
        "trace_attribution.claude",
        "trace_attribution.graph",
        "trace_attribution.reconstruction",
    }}
]
print(json.dumps(forbidden))
""".format(package_dir=str(package_dir))

        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(json.loads(result.stdout), [])

    def test_graph_and_composition_share_deterministic_eligibility_matrix(self):
        trace = trace_payload()

        def bound_record(record_id, event_type, **data):
            return {
                "record_id": record_id,
                "event_type": event_type,
                "data": {
                    **data,
                    "subject_revision": "git:abc123",
                    "revision_status": "matched",
                    "revision_provenance_status": "valid",
                },
            }

        trace["records"].extend(
            [
                {
                    "record_id": "ambiguous_tool_result",
                    "event_type": "tool.result",
                    "data": {"call_id": "bundle-probe"},
                },
                bound_record(
                    "response_current",
                    "response.claim",
                    repository_revision=2,
                ),
                bound_record(
                    "response_stale",
                    "response.claim",
                    repository_revision=1,
                ),
                bound_record("change_current", "change", revision_after=2),
                bound_record("change_stale", "change", revision_after=1),
                bound_record(
                    "verification_current",
                    "verification",
                    repository_revision=2,
                    effective_for_final_state=True,
                ),
                bound_record(
                    "verification_stale",
                    "verification",
                    repository_revision=1,
                    effective_for_final_state=True,
                ),
                bound_record(
                    "malformed_revision",
                    "decision",
                    repository_revision=True,
                ),
                {
                    "record_id": "mismatched_revision",
                    "event_type": "decision",
                    "data": {
                        "subject_revision": "git:different",
                        "revision_status": "mismatched",
                        "revision_provenance_status": "valid",
                    },
                },
                {
                    "record_id": "progress_inner",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:response_current"]},
                },
                {
                    "record_id": "progress_outer",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:progress_inner"]},
                },
                {
                    "record_id": "progress_cycle_a",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:progress_cycle_b"]},
                },
                {
                    "record_id": "progress_cycle_b",
                    "event_type": "progress.episode",
                    "data": {"member_refs": ["record:progress_cycle_a"]},
                },
            ]
        )
        matched_external = evaluation_payload("matched external")
        mismatched_external = evaluation_payload("mismatched external")
        mismatched_external["subject_revision"] = "git:different"
        mismatched_external["observed_at"] = "2026-08-03T00:00:01Z"
        trace = inject_external_evaluation_facts(
            trace,
            (matched_external, mismatched_external),
        )
        external_refs = {
            record["data"]["assertion"]: "record:{0}".format(record["record_id"])
            for record in trace["records"]
            if record.get("event_type") == "external.evaluation_fact"
        }
        cases = [
            ("canonical", "record:tool_result", "included"),
            ("ambiguous", "tool_result:bundle-probe", "unresolved"),
            ("response current", "record:response_current", "included"),
            ("response stale", "record:response_stale", "rejected"),
            ("change current", "record:change_current", "included"),
            ("change stale", "record:change_stale", "rejected"),
            ("verification current", "record:verification_current", "included"),
            ("verification stale", "record:verification_stale", "rejected"),
            ("malformed", "record:malformed_revision", "rejected"),
            ("mismatched", "record:mismatched_revision", "rejected"),
            ("external matched", external_refs["matched external"], "included"),
            ("external mismatched", external_refs["mismatched external"], "rejected"),
            ("nested progress", "record:progress_outer", "included"),
            ("cyclic progress", "record:progress_cycle_a", "rejected"),
        ]
        graph = TraceGraph.from_trace(copy.deepcopy(trace))

        for label, declared_ref, expected in cases:
            with self.subTest(label=label):
                composed = compose_effective_trace(
                    trace,
                    review=structured_review_payload(declared_ref),
                )
                seed = next(
                    record
                    for record in composed["records"]
                    if record.get("record_id", "").startswith("observed_defect_seed_")
                )
                binding = seed["data"]["source_binding"]
                actual = (
                    "included"
                    if binding["review_refs"]
                    else (
                        "rejected"
                        if binding["rejected_review_refs"]
                        else "unresolved"
                    )
                )
                self.assertEqual(actual, expected)
                if expected == "unresolved":
                    continue
                try:
                    graph_eligible = graph.active_revision_evidence_eligible(
                        declared_ref
                    )
                except RecursionError:
                    self.fail("matrix cycle raised RecursionError")
                self.assertEqual(graph_eligible, expected == "included")


class BenchmarkBundleValidationTest(unittest.TestCase):
    def assert_manifest_rejected(
        self,
        mutate,
        *,
        artifact=False,
        evaluations=0,
        labels=False,
    ):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(
                root,
                artifact=artifact,
                evaluations=evaluations,
                labels=labels,
            )
            mutate(manifest)
            rewrite_bundle_manifest(root, manifest)
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_opens_minimal_bundle_as_frozen_read_only_results(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root, artifact=True)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            module = importlib.import_module("trace_attribution.benchmark_bundle")
            original_from_trace = TraceGraph.from_trace
            with ExitStack() as stack:
                graph_from_trace = stack.enter_context(
                    patch.object(
                        TraceGraph,
                        "from_trace",
                        side_effect=original_from_trace,
                    )
                )
                infer_root = stack.enter_context(
                    patch(
                        "trace_attribution.graph.artifact_root_for_trace_path",
                        wraps=artifact_root_for_trace_path,
                    )
                )
                provider_init = stack.enter_context(
                    patch.object(ClaudeJudgeClient, "__init__", autospec=True)
                )
                opened = module.open_benchmark_trace_bundle(root)

            self.assertIsInstance(opened, module.BenchmarkTraceBundle)
            self.assertIsInstance(opened.trace_source, module.BenchmarkBundleSource)
            self.assertIsInstance(opened.members[0], module.BenchmarkBundleMember)
            self.assertIsInstance(opened.artifacts[0], module.BenchmarkBundleArtifact)
            self.assertEqual(opened.bundle_id, manifest["bundle_id"])
            self.assertEqual(opened.artifact_root, root)
            self.assertEqual(opened.effective_trace["manifest"]["case_id"], "bundle-case")
            self.assertIsInstance(opened.graph, TraceGraph)
            for result, field, value in (
                (opened, "bundle_id", "changed"),
                (opened.trace_source, "path", "changed"),
                (opened.members[0], "role", "changed"),
                (opened.artifacts[0], "artifact_id", "changed"),
            ):
                with self.subTest(result=type(result).__name__):
                    with self.assertRaises(FrozenInstanceError):
                        setattr(result, field, value)
            graph_from_trace.assert_called_once()
            self.assertEqual(graph_from_trace.call_args.kwargs["artifact_root"], root)
            infer_root.assert_not_called()
            provider_init.assert_not_called()
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_labels_are_verified_but_only_returned_for_evaluation_role(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root, labels=True)

            attribution = open_manual_bundle(root, role="attribution")
            evaluation = open_manual_bundle(root, role="evaluation")

            self.assertIsNone(attribution.labels_source)
            self.assertIsNone(attribution.labels_path)
            self.assertNotIn("source_labels", [item.role for item in attribution.members])
            self.assertIsNotNone(evaluation.labels_source)
            self.assertEqual(evaluation.labels_path, root / "inputs/labels.json")
            self.assertIn("source_labels", [item.role for item in evaluation.members])

            (root / "inputs/labels.json").write_bytes(b"corrupted labels")
            with self.assertRaises(ValueError):
                open_manual_bundle(root, role="attribution")

    def test_rejects_every_exact_key_field_class(self):
        cases = (
            ("top missing", False, lambda value: value.pop("composition_contract")),
            ("top extra", False, lambda value: value.__setitem__("extra", True)),
            (
                "sources missing",
                False,
                lambda value: value["sources"].pop("labels"),
            ),
            (
                "sources extra",
                False,
                lambda value: value["sources"].__setitem__("extra", None),
            ),
            (
                "source binding missing",
                False,
                lambda value: value["sources"]["trace"].pop("media_type"),
            ),
            (
                "source binding extra",
                False,
                lambda value: value["sources"]["trace"].__setitem__("extra", 1),
            ),
            (
                "effective binding missing",
                False,
                lambda value: value["effective_trace"].pop("sha256"),
            ),
            (
                "effective binding extra",
                False,
                lambda value: value["effective_trace"].__setitem__("extra", 1),
            ),
            (
                "member missing",
                False,
                lambda value: value["members"][0].pop("role"),
            ),
            (
                "member extra",
                False,
                lambda value: value["members"][0].__setitem__("extra", 1),
            ),
            (
                "counts missing",
                False,
                lambda value: value["counts"].pop("member_count"),
            ),
            (
                "counts extra",
                False,
                lambda value: value["counts"].__setitem__("extra", 1),
            ),
            (
                "artifact missing",
                True,
                lambda value: value["artifacts"][0].pop("member_path"),
            ),
            (
                "artifact extra",
                True,
                lambda value: value["artifacts"][0].__setitem__("extra", 1),
            ),
        )
        for label, artifact, mutate in cases:
            with self.subTest(label=label):
                self.assert_manifest_rejected(mutate, artifact=artifact)

        self.assert_manifest_rejected(
            lambda value: value["sources"]["evaluations"][0].pop("source_index"),
            evaluations=1,
        )
        self.assert_manifest_rejected(
            lambda value: value["sources"]["evaluations"][0].__setitem__(
                "extra", True
            ),
            evaluations=1,
        )

    def test_rejects_invalid_types_enums_and_integrity_identities(self):
        cases = (
            lambda value: value.__setitem__("schema_version", "wrong"),
            lambda value: value.__setitem__("composition_contract", "wrong"),
            lambda value: value.__setitem__("revision_provenance_status", "unknown"),
            lambda value: value.__setitem__("case_id", ""),
            lambda value: value["sources"].__setitem__("evaluations", {}),
            lambda value: value.__setitem__("artifacts", {}),
            lambda value: value.__setitem__("members", {}),
            lambda value: value["members"][0].__setitem__("role", "unknown"),
            lambda value: value["members"][0].__setitem__("role", []),
            lambda value: value["members"][0].__setitem__("sha256", "sha256:1234"),
            lambda value: value["members"][0].__setitem__(
                "sha256", "sha256:" + "A" * 64
            ),
            lambda value: value["members"][0].__setitem__("byte_length", True),
            lambda value: value["members"][0].__setitem__("media_type", ""),
            lambda value: value["counts"].__setitem__("member_count", True),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.assert_manifest_rejected(mutate)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root)
            manifest["bundle_id"] = "sha256:" + "0" * 64
            (root / "bundle.json").write_bytes(canonical_bytes(manifest))
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_rejects_bundle_member_source_effective_and_length_corruption_before_graph(self):
        cases = (
            (False, False, "inputs/trace.json"),
            (False, False, "effective/trace.json"),
            (True, False, "artifact"),
            (False, True, "inputs/labels.json"),
        )
        for artifact, labels, target_name in cases:
            with self.subTest(target=target_name):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(
                        root,
                        artifact=artifact,
                        labels=labels,
                    )
                    target_path = (
                        manifest["artifacts"][0]["member_path"]
                        if target_name == "artifact"
                        else target_name
                    )
                    target = root / target_path
                    target.write_bytes(target.read_bytes() + b"x")
                    with patch.object(TraceGraph, "from_trace") as graph_from_trace:
                        with self.assertRaises(ValueError):
                            open_manual_bundle(root)
                    graph_from_trace.assert_not_called()

        def corrupt_length(value):
            value["sources"]["trace"]["byte_length"] += 1
            next(
                member
                for member in value["members"]
                if member["role"] == "source_trace"
            )["byte_length"] += 1

        self.assert_manifest_rejected(corrupt_length)

    def test_rejects_member_evaluation_order_and_count_equation_errors(self):
        mutations = (
            lambda value: value["members"].reverse(),
            lambda value: value["members"].append(copy.deepcopy(value["members"][0])),
            lambda value: value["sources"]["evaluations"].reverse(),
            lambda value: value["sources"]["evaluations"][1].__setitem__(
                "source_index", 4
            ),
            lambda value: value["sources"]["evaluations"][0].__setitem__(
                "path", "inputs/evaluations/0000-wrong.json"
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(root, evaluations=2)
                    mutate(manifest)
                    rewrite_bundle_manifest(root, manifest)
                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

        for count_name in (
            "source_count",
            "evaluation_count",
            "declared_artifact_count",
            "referenced_artifact_count",
            "artifact_member_count",
            "member_count",
        ):
            with self.subTest(count=count_name):
                self.assert_manifest_rejected(
                    lambda value, name=count_name: value["counts"].__setitem__(
                        name, value["counts"][name] + 1
                    )
                )

    def test_rejects_role_binding_and_artifact_cross_reference_errors(self):
        self.assert_manifest_rejected(
            lambda value: next(
                member
                for member in value["members"]
                if member["role"] == "source_trace"
            ).__setitem__("role", "source_review")
        )
        self.assert_manifest_rejected(
            lambda value: value["sources"]["trace"].__setitem__(
                "sha256", "sha256:" + "0" * 64
            )
        )

        def artifact_member_mismatch(value):
            value["artifacts"][0]["content_sha256"] = "sha256:" + "0" * 64

        self.assert_manifest_rejected(artifact_member_mismatch, artifact=True)

    def test_accepts_one_artifact_and_rejects_duplicate_or_open_closure(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root, artifact=True)
            opened = open_manual_bundle(root)
            self.assertEqual([item.artifact_id for item in opened.artifacts], ["proof"])

        def duplicate_artifact_id(value):
            value["artifacts"].append(copy.deepcopy(value["artifacts"][0]))

        def duplicate_source_path(value):
            duplicate = copy.deepcopy(value["artifacts"][0])
            duplicate["artifact_id"] = "other"
            value["artifacts"].append(duplicate)

        self.assert_manifest_rejected(duplicate_artifact_id, artifact=True)
        self.assert_manifest_rejected(duplicate_source_path, artifact=True)

        semantic_mutations = (
            lambda trace: trace["artifacts"][0].__setitem__(
                "content_hash", "sha256:" + "0" * 64
            ),
            lambda trace: trace["records"][0]["artifact_refs"].append(
                "artifact:unknown"
            ),
            lambda trace: trace["records"][0].pop("artifact_refs"),
            lambda trace: trace.__setitem__("artifacts", []),
        )
        for index, mutate in enumerate(semantic_mutations):
            with self.subTest(closure=index):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(root, artifact=True)
                    effective = json.loads(
                        (root / "effective/trace.json").read_text(encoding="utf-8")
                    )
                    mutate(effective)
                    replace_bound_member(
                        root,
                        manifest,
                        "effective/trace.json",
                        canonical_bytes(effective),
                    )
                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_rejects_undeclared_files_and_all_unsafe_path_forms(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root)
            (root / "undeclared.txt").write_text("not declared", encoding="utf-8")
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        attacks = (
            "",
            ".",
            "..",
            "/absolute/path",
            "inputs/../trace.json",
            "inputs\\trace.json",
            "inputs//trace.json",
            "bundle.json",
        )
        for attack in attacks:
            with self.subTest(path=attack):
                def mutate(value, path=attack):
                    value["sources"]["trace"]["path"] = path
                    next(
                        member
                        for member in value["members"]
                        if member["role"] == "source_trace"
                    )["path"] = path

                self.assert_manifest_rejected(mutate)

        for attack in attacks:
            with self.subTest(source_relative_path=attack):
                self.assert_manifest_rejected(
                    lambda value, path=attack: value["artifacts"][0].__setitem__(
                        "source_relative_path", path
                    ),
                    artifact=True,
                )

    def test_rejects_root_parent_leaf_symlinks_special_files_and_duplicate_real_paths(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            base = Path(directory)
            real = base / "real"
            create_manual_bundle(real)
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ValueError):
                open_manual_bundle(linked)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "not-a-directory"
            root.write_text("regular file", encoding="utf-8")
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            base = Path(directory)
            root = base / "bundle"
            create_manual_bundle(root)
            target = root / "inputs/trace.json"
            outside = base / "outside.json"
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            base = Path(directory)
            root = base / "bundle"
            create_manual_bundle(root)
            inputs = root / "inputs"
            outside_inputs = base / "outside-inputs"
            inputs.rename(outside_inputs)
            inputs.symlink_to(outside_inputs, target_is_directory=True)
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                root = Path(directory) / "bundle"
                create_manual_bundle(root)
                target = root / "inputs/trace.json"
                target.unlink()
                os.mkfifo(target)
                with self.assertRaises(ValueError):
                    open_manual_bundle(root)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root)
            effective = root / "effective/trace.json"
            effective.unlink()
            os.link(root / "inputs/trace.json", effective)
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_rejects_noncanonical_effective_json_and_identity_disagreement(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root)
            effective = json.loads(
                (root / "effective/trace.json").read_text(encoding="utf-8")
            )
            replace_bound_member(
                root,
                manifest,
                "effective/trace.json",
                json.dumps(effective, indent=2).encode("utf-8"),
            )
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        disagreements = (
            ("case_id", "other-case"),
            ("run_id", "other-run"),
            ("subject_revision", "git:other"),
        )
        for field, value in disagreements:
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(root)
                    effective = json.loads(
                        (root / "effective/trace.json").read_text(encoding="utf-8")
                    )
                    effective["manifest"][field] = value
                    replace_bound_member(
                        root,
                        manifest,
                        "effective/trace.json",
                        canonical_bytes(effective),
                    )
                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_rejects_duplicate_manifest_keys_and_source_trace_identity_disagreement(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root)
            serialized = canonical_bytes(manifest).decode("utf-8")
            duplicate = (
                '{"schema_version":"benchmark-trace-bundle/v1",'
                + serialized[1:]
            )
            (root / "bundle.json").write_text(duplicate, encoding="utf-8")
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root)
            source_trace = json.loads(
                (root / "inputs/trace.json").read_text(encoding="utf-8")
            )
            source_trace["manifest"]["case_id"] = "other-case"
            source_trace["manifest"]["subject_revision_provenance"][
                "case_id"
            ] = "other-case"
            replace_bound_member(
                root,
                manifest,
                "inputs/trace.json",
                canonical_bytes(source_trace),
            )
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_rejects_conflicting_declared_artifact_hash_assertions(self):
        for value in ("0" * 8, "sha256:" + "0" * 16, "sha256:" + "0" * 64):
            with self.subTest(value=value):
                self.assert_manifest_rejected(
                    lambda manifest, declared=value: manifest["artifacts"][
                        0
                    ].__setitem__("declared_content_hash", declared),
                    artifact=True,
                )

    def test_rejects_malformed_non_label_json_sources(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root, review=True)
            replace_bound_member(
                root,
                manifest,
                "inputs/review.json",
                b"not JSON",
            )
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root, evaluations=1)
            binding = manifest["sources"]["evaluations"][0]
            old_path = binding["path"]
            malformed = b"not JSON"
            digest = bundle_digest(malformed)
            new_path = "inputs/evaluations/0000-{0}.json".format(
                digest.removeprefix("sha256:")
            )
            (root / old_path).rename(root / new_path)
            (root / new_path).write_bytes(malformed)
            binding.update(
                {
                    "path": new_path,
                    "sha256": digest,
                    "byte_length": len(malformed),
                }
            )
            member = next(
                value
                for value in manifest["members"]
                if value["path"] == old_path
            )
            member.update(
                {
                    "path": new_path,
                    "sha256": digest,
                    "byte_length": len(malformed),
                }
            )
            manifest["members"].sort(key=lambda value: value["path"])
            rewrite_bundle_manifest(root, manifest)
            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_artifact_root_poisoning_cannot_change_explicit_graph_root(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root, artifact=True)
            source = json.loads(
                (root / "inputs/trace.json").read_text(encoding="utf-8")
            )
            effective = json.loads(
                (root / "effective/trace.json").read_text(encoding="utf-8")
            )
            poisoned_files = {
                "trace_dir": "../../outside",
                "artifact_root": "/tmp/outside",
            }
            source["manifest"]["files"] = poisoned_files
            effective["manifest"]["files"] = poisoned_files
            replace_bound_member(
                root,
                manifest,
                "inputs/trace.json",
                canonical_bytes(source),
            )
            replace_bound_member(
                root,
                manifest,
                "effective/trace.json",
                canonical_bytes(effective),
            )
            original_from_trace = TraceGraph.from_trace
            with patch.object(
                TraceGraph,
                "from_trace",
                side_effect=original_from_trace,
            ) as graph_from_trace, patch(
                "trace_attribution.graph.artifact_root_for_trace_path",
                wraps=artifact_root_for_trace_path,
            ) as infer_root:
                opened = open_manual_bundle(root)

            self.assertEqual(opened.artifact_root, root)
            self.assertEqual(graph_from_trace.call_args.kwargs["artifact_root"], root)
            infer_root.assert_not_called()

    def test_rejects_member_ancestor_replaced_during_the_pinned_read(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            base = Path(directory)
            root = base / "bundle"
            create_manual_bundle(root)
            inputs = root / "inputs"
            trace_stat = (inputs / "trace.json").stat()
            trace_identity = (trace_stat.st_dev, trace_stat.st_ino)
            parked_inputs = base / "parked-inputs"
            outside_inputs = base / "outside-inputs"
            outside_inputs.mkdir()
            outside_marker = b"outside bytes must never be exposed"
            (outside_inputs / "trace.json").write_bytes(outside_marker)
            original_read = os.read
            replaced = []

            def replace_ancestor_then_read(descriptor, byte_count):
                descriptor_stat = os.fstat(descriptor)
                if (
                    not replaced
                    and (descriptor_stat.st_dev, descriptor_stat.st_ino)
                    == trace_identity
                ):
                    inputs.rename(parked_inputs)
                    inputs.symlink_to(outside_inputs, target_is_directory=True)
                    replaced.append(True)
                return original_read(descriptor, byte_count)

            with patch(
                "trace_attribution.benchmark_bundle.os.read",
                side_effect=replace_ancestor_then_read,
            ):
                with self.assertRaises(ValueError) as raised:
                    open_manual_bundle(root)

            self.assertEqual(replaced, [True])
            self.assertNotIn(outside_marker.decode("utf-8"), str(raised.exception))

    def test_rejects_symlinked_bundle_root_ancestor(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            base = Path(directory)
            real_parent = base / "real-parent"
            root = real_parent / "bundle"
            create_manual_bundle(root)
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaises(ValueError):
                open_manual_bundle(linked_parent / "bundle")

    def test_graph_hydration_and_refresh_retain_verified_artifact_bytes(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            verified_bytes = b"verified artifact bytes"
            manifest = create_manual_bundle(
                root,
                artifact=True,
                artifact_bytes=verified_bytes,
            )
            opened = open_manual_bundle(root)
            artifact_path = root / manifest["artifacts"][0]["member_path"]
            artifact_path.write_bytes(b"mutated after validation")

            hydrated = opened.graph.hydrate_node("record:tool_result")
            self.assertEqual(
                hydrated.data["hydrated_artifacts"][0]["content"],
                verified_bytes.decode("utf-8"),
            )
            refreshed = opened.graph._artifact_reader.read("proof", refresh=True)
            self.assertEqual(refreshed.content_bytes, verified_bytes)
            with patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=AssertionError("pinned artifact refresh reopened a path"),
            ):
                refreshed_again = opened.graph._artifact_reader.read(
                    "proof",
                    refresh=True,
                )
            self.assertEqual(refreshed_again.content_bytes, verified_bytes)

    def test_rejects_bool_and_float_effective_artifact_lengths(self):
        for declared_length in (True, 1.0):
            with self.subTest(declared_length=declared_length):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(
                        root,
                        artifact=True,
                        artifact_bytes=b"x",
                    )
                    effective = json.loads(
                        (root / "effective/trace.json").read_text(encoding="utf-8")
                    )
                    effective["artifacts"][0]["byte_length"] = declared_length
                    replace_bound_member(
                        root,
                        manifest,
                        "effective/trace.json",
                        canonical_bytes(effective),
                    )
                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_rejects_external_hardlinks_for_manifest_and_members(self):
        for relative_path in ("bundle.json", "inputs/trace.json"):
            with self.subTest(relative_path=relative_path):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    base = Path(directory)
                    root = base / "bundle"
                    create_manual_bundle(root)
                    os.link(root / relative_path, base / "outside-hardlink")

                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_public_effective_trace_is_recursively_immutable_and_unaliased(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root)
            opened = open_manual_bundle(root)

            self.assertIsNot(opened.effective_trace, opened.graph.raw_trace)
            with self.assertRaises(TypeError):
                opened.effective_trace["manifest"]["case_id"] = "mutated"
            with self.assertRaises(TypeError):
                opened.effective_trace["records"][0]["data"]["call_id"] = "mutated"
            opened.graph.raw_trace["manifest"]["case_id"] = "graph-mutated"
            self.assertEqual(
                opened.effective_trace["manifest"]["case_id"],
                "bundle-case",
            )

    def test_effective_trace_is_exact_recomposition_of_all_bound_sources(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            manifest = create_manual_bundle(root, review=True, evaluations=1)
            effective = json.loads(
                (root / "effective/trace.json").read_text(encoding="utf-8")
            )
            effective["records"] = [
                record
                for record in effective["records"]
                if record.get("event_type") == "tool.result"
            ]
            replace_bound_member(
                root,
                manifest,
                "effective/trace.json",
                canonical_bytes(effective),
            )

            with self.assertRaises(ValueError):
                open_manual_bundle(root)

    def test_verified_source_bytes_are_pinned_and_labels_remain_role_gated(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(
                root,
                review=True,
                evaluations=1,
                labels=True,
            )
            expected_trace = (root / "inputs/trace.json").read_bytes()
            expected_review = (root / "inputs/review.json").read_bytes()
            expected_evaluations = tuple(
                path.read_bytes()
                for path in sorted((root / "inputs/evaluations").iterdir())
            )
            expected_labels = (root / "inputs/labels.json").read_bytes()
            expected_effective = (root / "effective/trace.json").read_bytes()

            attribution = open_manual_bundle(root, role="attribution")
            evaluation = open_manual_bundle(root, role="evaluation")
            for path in root.rglob("*.json"):
                if path.name != "bundle.json":
                    path.write_bytes(b"{}")

            self.assertEqual(attribution.trace_bytes, expected_trace)
            self.assertEqual(attribution.review_bytes, expected_review)
            self.assertEqual(attribution.evaluation_bytes, expected_evaluations)
            self.assertEqual(attribution.effective_trace_bytes, expected_effective)
            self.assertIsNone(attribution.labels_bytes)
            self.assertEqual(evaluation.labels_bytes, expected_labels)

    def test_each_graph_access_returns_a_fresh_verified_working_graph(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root, artifact=True)
            opened = open_manual_bundle(root)

            first = opened.graph
            first.case_id = "forged-case"
            first.aliases["record:forged"] = "record:tool_result"
            first.raw_trace["manifest"]["case_id"] = "forged-case"
            second = opened.graph

            self.assertIsNot(first, second)
            self.assertEqual(second.case_id, "bundle-case")
            self.assertNotIn("record:forged", second.aliases)
            self.assertEqual(
                second._artifact_reader.read("proof", refresh=True).content,
                "immutable artifact bytes",
            )

    def test_rejects_malformed_record_items_even_when_sources_recompose(self):
        for malformed in (False, 1, "record", [], {"event_type": "tool.result"}):
            with self.subTest(malformed=malformed):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(root)
                    source = json.loads(
                        (root / "inputs/trace.json").read_text(encoding="utf-8")
                    )
                    source["records"].append(malformed)
                    replace_bound_member(
                        root,
                        manifest,
                        "inputs/trace.json",
                        canonical_bytes(source),
                    )
                    replace_bound_member(
                        root,
                        manifest,
                        "effective/trace.json",
                        canonical_bytes(source),
                    )

                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_rejects_falsy_malformed_effective_trace_containers(self):
        for field in ("artifacts", "records"):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
                    root = Path(directory) / "bundle"
                    manifest = create_manual_bundle(root)
                    effective = json.loads(
                        (root / "effective/trace.json").read_text(encoding="utf-8")
                    )
                    effective[field] = {}
                    replace_bound_member(
                        root,
                        manifest,
                        "effective/trace.json",
                        canonical_bytes(effective),
                    )

                    with self.assertRaises(ValueError):
                        open_manual_bundle(root)

    def test_rejects_invalid_open_modes(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root)
            for role in ("", "builder", None, []):
                with self.subTest(role=role):
                    with self.assertRaises(ValueError):
                        open_manual_bundle(root, role=role)
            module = importlib.import_module("trace_attribution.benchmark_bundle")
            with self.assertRaises(ValueError):
                module.open_benchmark_trace_bundle(root, verify="partial")


if __name__ == "__main__":
    unittest.main()
