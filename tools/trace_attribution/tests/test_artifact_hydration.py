from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from trace_attribution.graph import TraceGraph


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def full_content_hash(value: bytes | str) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def semantic_slice(start: int, content: str, *, truncated: bool = False, digest: str | None = None):
    end = start + len(content.encode("utf-8"))
    return {
        "byte_range": [start, end],
        "content": content,
        "hash": digest or content_hash(content),
        "truncated": truncated,
    }


def artifact_trace(artifact, *, record_id: str = "verification", records=None):
    return {
        "case_id": "artifact-hydration-case",
        "artifacts": [artifact],
        "records": records or [
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
            "byte_length": len(content.encode("utf-8")) + 10,
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
            "byte_length": len("expected artifact".encode("utf-8")),
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
            "byte_length": len("tampered slice".encode("utf-8")),
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
                        "byte_length": len("safe embedded excerpt".encode("utf-8")),
                        "semantic_slices": [semantic_slice(0, "safe embedded excerpt")],
                    }
                    graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
                    hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

                    self.assertEqual(hydrated["source"], "embedded_semantic_slice")
                    self.assertEqual(hydrated["content"], "safe embedded excerpt")
                    self.assertEqual(hydrated["file_hash_status"], "invalid_path")
                    self.assertNotIn("outside secret", hydrated["content"])
                    self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)

    def test_rejects_symlink_escape_without_exposing_target_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "case"
            (root / "artifacts").mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("outside symlink secret", encoding="utf-8")
            (root / "artifacts" / "linked.txt").symlink_to(outside)
            artifact = {
                "artifact_id": "artifact_1",
                "kind": "text",
                "path": "artifacts/linked.txt",
                "hash": content_hash("outside symlink secret"),
                "byte_length": len("outside symlink secret"),
                "semantic_slices": [semantic_slice(0, "safe embedded excerpt")],
            }
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["source"], "embedded_semantic_slice")
        self.assertEqual(hydrated["content"], "safe embedded excerpt")
        self.assertEqual(hydrated["file_hash_status"], "invalid_path")
        self.assertNotIn("outside symlink secret", hydrated["content"])

    def test_invalid_utf8_bundle_file_is_never_exposed(self):
        invalid = b"verified-prefix\xffsecret"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/invalid.txt",
            "hash": full_content_hash(invalid),
            "byte_length": len(invalid),
            "semantic_slices": [semantic_slice(0, "safe fallback")],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(invalid)
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["content"], "safe fallback")
        self.assertEqual(hydrated["file_hash_status"], "invalid_utf8")
        self.assertNotIn("verified-prefix", hydrated["content"])

    def test_accepts_supported_raw_full_and_prefixed_sha256_forms(self):
        content = "hash-form evidence"
        digest = full_content_hash(content)
        for declared_hash in (digest[:16], digest, "sha256:" + digest[:16], "sha256:" + digest):
            with self.subTest(declared_hash=declared_hash), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "artifacts" / "hash.txt"
                target.parent.mkdir(parents=True)
                target.write_text(content, encoding="utf-8")
                artifact = {
                    "artifact_id": "artifact_1",
                    "kind": "text",
                    "path": "artifacts/hash.txt",
                    "hash": declared_hash,
                    "byte_length": len(content.encode("utf-8")),
                }
                graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
                hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

                self.assertEqual(hydrated["content"], content)
                self.assertEqual(hydrated["hash_status"], "verified")

    def test_rejects_malformed_byte_length_and_out_of_bounds_slices(self):
        content = "bounded"
        for byte_length in (-1, True, "7", None):
            with self.subTest(byte_length=byte_length), tempfile.TemporaryDirectory() as directory:
                artifact = {
                    "artifact_id": "artifact_1",
                    "kind": "text",
                    "path": "artifacts/missing.txt",
                    "hash": content_hash("full artifact"),
                    "byte_length": byte_length,
                    "semantic_slices": [semantic_slice(0, content)],
                }
                graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
                node = graph.hydrate_node("record:verification")

                self.assertNotIn("hydrated_artifacts", node.data)
                self.assertTrue(graph.artifact_hydration["integrity_failures"])

        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/missing.txt",
            "hash": content_hash("full artifact"),
            "byte_length": 3,
            "semantic_slices": [semantic_slice(0, content)],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            node = graph.hydrate_node("record:verification")

        self.assertNotIn("hydrated_artifacts", node.data)
        self.assertIn(
            {"artifact_id": "artifact_1", "status": "semantic_slice_out_of_bounds"},
            graph.artifact_hydration["integrity_failures"],
        )

    def test_disjoint_slices_do_not_masquerade_as_contiguous_text(self):
        first = semantic_slice(0, "alpha ")
        second = semantic_slice(first["byte_range"][1] + 5, "世界")
        overlapping = semantic_slice(2, "overlap")
        bad = semantic_slice(second["byte_range"][1] + 5, "bad", digest=content_hash("not bad"))
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/sha256/full.txt",
            "hash": content_hash("full artifact"),
            "byte_length": bad["byte_range"][1],
            "semantic_slices": [second, bad, overlapping, first],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        self.assertEqual(hydrated["content"], "alpha ")
        self.assertEqual(hydrated["byte_ranges"], [first["byte_range"]])
        self.assertEqual(hydrated["semantic_slice_count"], 1)
        self.assertEqual(hydrated["rejected_semantic_slice_count"], 3)
        self.assertEqual(hydrated["slice_hash_status"], "partial")
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)
        self.assertEqual(graph.artifact_hydration["truncated"], 1)
        self.assertIn(
            {"artifact_id": "artifact_1", "status": "semantic_slice_gap"},
            graph.artifact_hydration["integrity_failures"],
        )

    def test_shared_artifact_outcomes_are_counted_once_across_records(self):
        content = "shared verified evidence"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/shared.txt",
            "hash": content_hash(content),
            "byte_length": len(content.encode("utf-8")),
        }
        records = [
            {
                "record_id": record_id,
                "component": "tool",
                "event_type": "verification",
                "artifact_refs": ["artifact:artifact_1"],
                "data": {"artifact_id": "artifact_1"},
            }
            for record_id in ("first", "second")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            graph = TraceGraph.from_trace(artifact_trace(artifact, records=records), artifact_root=root)
            first = graph.hydrate_node("record:first")
            second = graph.hydrate_node("record:second")

        self.assertEqual(first.data["hydrated_artifacts"][0]["content"], content)
        self.assertEqual(second.data["hydrated_artifacts"][0]["content"], content)
        self.assertEqual(graph.artifact_hydration["referenced"], 2)
        self.assertEqual(graph.artifact_hydration["unique_referenced"], 1)
        self.assertEqual(graph.artifact_hydration["loaded"], 1)
        self.assertEqual(graph.artifact_hydration["truncated"], 0)
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 0)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 0)

    def test_truncated_slice_reports_only_the_exposed_utf8_byte_range(self):
        content = "é" * 40_000
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/missing.txt",
            "hash": content_hash("full artifact"),
            "byte_length": len(content.encode("utf-8")) + 5,
            "semantic_slices": [semantic_slice(0, content)],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=Path(directory))
            hydrated = graph.hydrate_node("record:verification").data["hydrated_artifacts"][0]

        exposed_length = len(hydrated["content"].encode("utf-8"))
        self.assertEqual(len(hydrated["content"]), 32_000)
        self.assertEqual(hydrated["byte_ranges"], [[0, exposed_length]])
        self.assertLess(exposed_length, artifact["semantic_slices"][0]["byte_range"][1] + 1)
        self.assertTrue(hydrated["truncated"])

    def test_shared_fallback_and_mismatch_outcomes_are_counted_once(self):
        fallback = "shared fallback"
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/shared.txt",
            "hash": content_hash("expected content"),
            "byte_length": len("expected content".encode("utf-8")),
            "semantic_slices": [semantic_slice(0, fallback)],
        }
        records = [
            {
                "record_id": record_id,
                "component": "tool",
                "event_type": "verification",
                "artifact_refs": ["artifact:artifact_1"],
                "data": {"artifact_id": "artifact_1"},
            }
            for record_id in ("first", "second")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_text("tampered content", encoding="utf-8")
            graph = TraceGraph.from_trace(artifact_trace(artifact, records=records), artifact_root=root)
            first = graph.hydrate_node("record:first")
            second = graph.hydrate_node("record:second")

        self.assertEqual(first.data["hydrated_artifacts"][0]["content"], fallback)
        self.assertEqual(second.data["hydrated_artifacts"][0]["content"], fallback)
        self.assertEqual(graph.artifact_hydration["loaded"], 1)
        self.assertEqual(graph.artifact_hydration["truncated"], 1)
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)

    def test_legacy_artifact_without_verifiable_hash_fails_closed(self):
        artifact = {
            "artifact_id": "artifact_1",
            "kind": "text",
            "path": "artifacts/legacy.txt",
            "hash": "legacy-unverifiable",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / artifact["path"]
            target.parent.mkdir(parents=True)
            target.write_text("legacy secret", encoding="utf-8")
            graph = TraceGraph.from_trace(artifact_trace(artifact), artifact_root=root)
            node = graph.hydrate_node("record:verification")

        self.assertNotIn("hydrated_artifacts", node.data)
        self.assertEqual(graph.artifact_hydration["loaded"], 0)
        self.assertEqual(graph.artifact_hydration["hash_mismatches"], 1)


if __name__ == "__main__":
    unittest.main()
