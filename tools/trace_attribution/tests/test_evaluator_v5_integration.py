from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.evaluate_recursive_attribution as evaluator
from tools.trace_attribution.tests.test_recursive_benchmarks import (
    FIXTURE_ROOT,
    load_fixture,
    run_fixture,
)
from trace_attribution.causal_state import (
    semantic_anchor_index,
    semantic_occurrence_index,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json


def _sha256(value: bytes) -> str:
    return "sha256:{0}".format(hashlib.sha256(value).hexdigest())


def _v5_sphinx_case():
    report, legacy_labels, _judge = run_fixture(
        FIXTURE_ROOT / "sphinx_recursive_minimal.json"
    )
    trace, _labels, _script = load_fixture(
        FIXTURE_ROOT / "sphinx_recursive_minimal.json"
    )
    trace["manifest"] = {
        "case_id": report["case_id"],
        "run_id": "v5-evaluator-run",
        "subject_revision": "git:v5-evaluator",
        "subject_revision_provenance": {
            "method": "case_trace_config",
            "source": "CaseTraceConfig.subjectRevision",
            "bound_at": "case_start",
            "case_id": report["case_id"],
            "run_id": "v5-evaluator-run",
        },
    }
    trace_bytes = stable_json(trace).encode("utf-8")
    graph = TraceGraph.from_trace(trace)
    anchors = semantic_anchor_index(graph.case_id, graph)
    occurrences = semantic_occurrence_index(graph.case_id, graph)
    result_seed = report["seed_results"][0]
    start_ref = result_seed["start_ref"]
    labels = {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": report["case_id"],
        "source_binding": {
            "trace_sha256": _sha256(trace_bytes),
            "review_sha256": "",
            "evaluation_sha256": [],
            "effective_trace_sha256": _sha256(
                stable_json(trace).encode("utf-8")
            ),
            "subject_revision": "git:v5-evaluator",
            "revision_provenance_status": "valid",
        },
        "seeds": [
            {
                "defect_id": "sphinx-observed-defect",
                "start_ref": start_ref,
                "defect_fingerprint": result_seed["defect_fingerprint"],
                "seed_binding_identity": result_seed["seed_binding_identity"],
                "seed_semantic_anchor_id": anchors[start_ref],
                "seed_semantic_occurrence_id": occurrences[start_ref],
                "seed_source_bindings": [
                    {
                        "source_kind": "raw_trace",
                        "source_index": 0,
                        "source_sha256": _sha256(trace_bytes),
                    }
                ],
                "defect_origin_kind": "introduced_by_agent",
                "expected_outcome": "confirmed_root",
                "roots": legacy_labels["roots"],
                "conditions": legacy_labels["conditions"],
                "amplifiers": legacy_labels["amplifiers"],
                "materializations": legacy_labels["materializations"],
                "unrelated": legacy_labels["unrelated"],
                "forbidden_roots": [],
                "allowed_unresolved_outcomes": legacy_labels[
                    "allowed_unresolved_outcomes"
                ],
            }
        ],
        "case_shared_factors": [],
    }
    loaded = SimpleNamespace(
        trace_bytes=trace_bytes,
        review_bytes=None,
        evaluation_bytes=(),
        effective_trace=trace,
        graph=graph,
        bundle=None,
        labels_bytes=stable_json(labels).encode("utf-8"),
    )
    return report, labels, loaded


class EvaluatorV5IntegrationTest(unittest.TestCase):
    def test_valid_v5_inputs_write_seed_owned_comparison_v8(self):
        report, labels, loaded = _v5_sphinx_case()

        with patch.object(
            evaluator,
            "_trace_backed_safety_violations",
            return_value=([], None, {}, {}),
        ):
            result = evaluator.compare_v5_report(
                report,
                labels,
                loaded_case=loaded,
            )

        self.assertEqual(
            result["schema_version"],
            "recursive-attribution-comparison/v8",
        )
        self.assertEqual(result["micro"]["root_metrics"]["tp"], 1)
        self.assertTrue(result["all_seed_coverage_passed"])

    def test_cli_dispatches_v5_and_never_writes_on_binding_failure(self):
        report, labels, loaded = _v5_sphinx_case()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            report_path = root / "report.json"
            labels_path = root / "labels.json"
            trace_path = root / "trace.json"
            output_path = root / "comparison.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            labels_path.write_text(json.dumps(labels), encoding="utf-8")
            trace_path.write_bytes(loaded.trace_bytes)

            with patch.object(
                evaluator,
                "load_benchmark_case",
                return_value=loaded,
            ), patch.object(
                evaluator,
                "compare_v5_report",
                side_effect=ValueError("trace_sha256 mismatch"),
            ), patch.object(evaluator, "compare_report") as legacy_compare:
                status = evaluator.main(
                    [
                        "--trace",
                        str(trace_path),
                        "--report",
                        str(report_path),
                        "--labels",
                        str(labels_path),
                        "--out",
                        str(output_path),
                    ]
                )

            self.assertEqual(status, 2)
            self.assertFalse(output_path.exists())
            legacy_compare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
