from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_attribution.benchmark_bundle import build_benchmark_trace_bundle
from trace_attribution.models import stable_json


REAL_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def trace_value():
    return {
        "manifest": {
            "case_id": "loaded-case",
            "run_id": "loaded-run",
            "subject_revision": "git:loaded123",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "loaded-case",
                "run_id": "loaded-run",
            },
        },
        "records": [
            {
                "record_id": "result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "loaded-call"},
            }
        ],
        "dataflow_edges": [],
    }


def review_value():
    return {
        "case_id": "loaded-case",
        "quality_review": {
            "quality_gaps": [
                {
                    "dimension": "correctness",
                    "score": 0,
                    "max_score": 1,
                    "gap_context_refs": ["record:result"],
                }
            ]
        },
    }


class BenchmarkCaseLoaderTest(unittest.TestCase):
    def sources(self, root: Path, *, labels: bool = True):
        source = root / "source"
        source.mkdir(parents=True)
        artifact_root = source / "artifacts"
        artifact_root.mkdir()
        trace_path = source / "trace.json"
        review_path = source / "review.json"
        labels_path = source / "labels.json"
        trace_path.write_text(stable_json(trace_value()), encoding="utf-8")
        review_path.write_text(stable_json(review_value()), encoding="utf-8")
        if labels:
            labels_path.write_text(
                json.dumps({"schema_version": "labels-test/v1"}),
                encoding="utf-8",
            )
        return trace_path, review_path, labels_path, artifact_root

    def test_bundle_and_legacy_load_the_same_effective_trace(self):
        from trace_attribution.benchmark_case import load_benchmark_case

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            trace_path, review_path, labels_path, artifact_root = self.sources(root)
            bundle = build_benchmark_trace_bundle(
                trace_path=trace_path,
                review_path=review_path,
                labels_path=labels_path,
                artifact_root=artifact_root,
                store_dir=root / "store",
            )

            legacy = load_benchmark_case(
                trace_path=trace_path,
                review_path=review_path,
                role="attribution",
            )
            bundled = load_benchmark_case(
                bundle_path=bundle.root,
                role="attribution",
            )

            self.assertEqual(
                stable_json(legacy.graph.raw_trace),
                stable_json(bundled.graph.raw_trace),
            )
            self.assertIsNone(legacy.labels_bytes)
            self.assertIsNone(bundled.labels_bytes)

    def test_bundle_never_infers_root_and_returns_isolated_working_graphs(self):
        from trace_attribution.benchmark_case import load_benchmark_case

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            trace_path, review_path, labels_path, artifact_root = self.sources(root)
            bundle = build_benchmark_trace_bundle(
                trace_path=trace_path,
                review_path=review_path,
                labels_path=labels_path,
                artifact_root=artifact_root,
                store_dir=root / "store",
            )
            with patch(
                "trace_attribution.graph.artifact_root_for_trace_path"
            ) as infer_root:
                first = load_benchmark_case(bundle_path=bundle.root)
                second = load_benchmark_case(bundle_path=bundle.root)
            infer_root.assert_not_called()
            first.graph.case_id = "forged"
            self.assertEqual(second.graph.case_id, "loaded-case")

    def test_evaluation_requires_verified_labels_and_attribution_hides_them(self):
        from trace_attribution.benchmark_case import load_benchmark_case

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            trace_path, review_path, labels_path, artifact_root = self.sources(root)
            bundle = build_benchmark_trace_bundle(
                trace_path=trace_path,
                review_path=review_path,
                labels_path=labels_path,
                artifact_root=artifact_root,
                store_dir=root / "store",
            )

            attribution = load_benchmark_case(bundle_path=bundle.root)
            evaluation = load_benchmark_case(
                bundle_path=bundle.root,
                role="evaluation",
            )
            self.assertIsNone(attribution.labels_bytes)
            self.assertEqual(evaluation.labels_bytes, labels_path.read_bytes())

            unlabeled_root = root / "unlabeled"
            trace2, review2, _, artifacts2 = self.sources(unlabeled_root, labels=False)
            unlabeled = build_benchmark_trace_bundle(
                trace_path=trace2,
                review_path=review2,
                artifact_root=artifacts2,
                store_dir=root / "unlabeled-store",
            )
            with self.assertRaises(ValueError):
                load_benchmark_case(
                    bundle_path=unlabeled.root,
                    role="evaluation",
                )

    def test_rejects_mixed_bundle_and_raw_inputs(self):
        from trace_attribution.benchmark_case import load_benchmark_case

        with self.assertRaises(ValueError):
            load_benchmark_case(
                bundle_path=Path("/bundle"),
                trace_path=Path("/trace.json"),
            )
        with self.assertRaises(ValueError):
            load_benchmark_case(role="attribution")
        with self.assertRaises(ValueError):
            load_benchmark_case(
                trace_path=Path("/trace.json"),
                labels_path=Path("/labels.json"),
                role="attribution",
            )


if __name__ == "__main__":
    unittest.main()
