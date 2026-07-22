from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from trace_attribution.graph import TraceGraph


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def semantic_slice(start: int, content: str, *, truncated: bool = False, digest: str | None = None):
    end = start + len(content.encode("utf-8"))
    return {
        "byte_range": [start, end],
        "content": content,
        "hash": digest or content_hash(content),
        "truncated": truncated,
    }


def artifact_trace(artifact, *, record_id: str = "verification"):
    return {
        "case_id": "artifact-hydration-case",
        "artifacts": [artifact],
        "records": [
            {
                "record_id": record_id,
                "component": "tool",
                "event_type": "verification",
                "artifact_refs": ["artifact:{0}".format(artifact["artifact_id"])],
                "data": {"artifact_id": artifact["artifact_id"]},
            }
        ],
    }


class ArtifactHydrationTest(unittest.TestCase):
    def test_hydrates_embedded_semantic_slice_when_bundle_file_is_absent(self):
        content = "pytest: 1 failed"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "hash": content_hash("full artifact"),
            "availability": "bundled",
            "semantic_slices": [semantic_slice(0, content, truncated=False)],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["source"], "embedded_semantic_slice")
        self.assertEqual(hydrated["content"], content)
        self.assertEqual(hydrated["hash_status"], "verified")
        self.assertEqual(hydrated["file_hash_status"], "missing")
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 0)
        self.assertEqual(graph.artifact_hydration["loaded"], 1)
        self.assertEqual(graph.artifact_hydration["truncated"], 1)

    def test_prefers_hash_verified_bundle_file_over_embedded_slice(self):
        file_content = "complete artifact content"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "content_hash": content_hash(file_content),
            "hash": content_hash(file_content),
            "semantic_slices": [semantic_slice(0, "fallback")],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_text(file_content, encoding="utf-8")
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["source"], "bundle_file")
        self.assertEqual(hydrated["content"], file_content)
        self.assertEqual(hydrated["hash_status"], "verified")
        self.assertEqual(hydrated["file_hash_status"], "verified")
        self.assertFalse(hydrated["truncated"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 0)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 0)

    def test_file_hash_mismatch_falls_back_only_to_verified_slice(self):
        slice_content = "verified fallback"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "content_hash": content_hash("expected artifact"),
            "hash": content_hash("expected artifact"),
            "semantic_slices": [semantic_slice(0, slice_content)],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_text("tampered artifact", encoding="utf-8")
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["source"], "embedded_semantic_slice")
        self.assertEqual(hydrated["content"], slice_content)
        self.assertNotIn("tampered artifact", hydrated["content"])
        self.assertEqual(hydrated["file_hash_status"], "mismatch")
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)

    def test_slice_hash_mismatch_is_not_hydrated_and_is_reported(self):
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "hash": content_hash("full artifact"),
            "semantic_slices": [semantic_slice(0, "tampered slice", digest=content_hash("different"))],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            node = graph.hydrate_node("record:verification")
            manifest = graph.artifact_hydration_manifest("record:verification")

        self.assertNotIn("hydrated_artifacts", node.data)
        self.assertEqual(manifest["missing_artifact_ids"], ["artifact_1"])
        self.assertEqual(
            manifest["integrity_failures"],
            [{"artifact_id": "artifact_1", "status": "semantic_slice_hash_mismatch"}],
        )
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 0)
        self.assertEqual(graph.artifact_hydration["missing"], 1)

    def test_rejects_absolute_and_parent_paths_without_reading_outside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "case"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside secret", encoding="utf-8")
            for path_value in (str(outside), "../outside.txt"):
                with self.subTest(path=path_value):
                    artifact = {
                        "artifact_id": "artifact_1",
                        "kind": "text",
                        "path": path_value,
                        "hash": content_hash("outside secret"),
                        "semantic_slices": [semantic_slice(0, "safe embedded excerpt")],
                    }
                    graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
                    hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

                    self.assertEqual(hydrated["source"], "embedded_semantic_slice")
                    self.assertEqual(hydrated["content"], "safe embedded excerpt")
                    self.assertEqual(hydrated["file_hash_status"], "invalid_path")
                    self.assertNotIn("outside secret", hydrated["content"])
                    self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)

    def test_sorts_non_overlapping_slices_and_reports_rejected_slices(self):
        first = semantic_slice(0, "alpha ")
        second = semantic_slice(first["byte_range"][1], "世界")
        overlapping = semantic_slice(2, "overlap")
        bad = semantic_slice(second["byte_range"][1] + 5, "bad", digest=content_hash("not bad"))
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "hash": content_hash("full artifact"),
            "semantic_slices": [second, bad, overlapping, first],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["content"], "alpha 世界")
        self.assertEqual(hydrated["byte_ranges"], [first["byte_range"], second["byte_range"]])
        self.assertEqual(hydrated["semantic_slice_count"], 2)
        self.assertEqual(hydrated["rejected_semantic_slice_count"], 2)
        self.assertEqual(hydrated["slice_hash_status"], "partial")
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)
        self.assertEqual(graph.artifact_hydration["truncated"], 1)


if __name__ == "__main__":
    unittest.main()
