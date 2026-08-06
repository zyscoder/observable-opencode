from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_attribution.benchmark_bundle import (
    build_benchmark_trace_bundle,
    open_benchmark_trace_bundle,
)
from trace_attribution.models import stable_json


REAL_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def canonical_bytes(value) -> bytes:
    return stable_json(value).encode("utf-8")


def sha256(value: bytes) -> str:
    return "sha256:{0}".format(hashlib.sha256(value).hexdigest())


def source_trace(*, artifact: bool = False):
    trace = {
        "manifest": {
            "case_id": "builder-case",
            "run_id": "builder-run",
            "subject_revision": "git:builder123",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "builder-case",
                "run_id": "builder-run",
            },
        },
        "records": [
            {
                "record_id": "result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "builder-call"},
            }
        ],
        "dataflow_edges": [],
    }
    if artifact:
        content = b"builder artifact evidence\n"
        trace["artifacts"] = [
            {
                "artifact_id": "proof",
                "path": "raw/proof.txt",
                "content_hash": sha256(content)[:23],
                "byte_length": len(content),
                "media_type": "text/plain",
            }
        ]
        trace["records"][0]["artifact_refs"] = ["artifact:proof"]
    return trace


def evaluation(assertion: str):
    return {
        "source": "featurebench",
        "scope": "builder-order",
        "subject_revision": "git:builder123",
        "assertion": assertion,
        "observation": "failed",
        "status": "failed",
        "observed_at": "2026-08-03T00:00:00Z",
        "evidence_refs": ["record:result"],
        "provenance": {"method": "offline_test", "version": "1"},
    }


class BenchmarkTraceBundleBuilderTest(unittest.TestCase):
    def create_sources(self, root: Path, *, artifact: bool = False):
        source_root = root / "source"
        source_root.mkdir()
        artifact_root = source_root / "artifacts"
        artifact_root.mkdir()
        trace_path = source_root / "trace.json"
        trace_path.write_bytes(canonical_bytes(source_trace(artifact=artifact)))
        if artifact:
            target = artifact_root / "raw" / "proof.txt"
            target.parent.mkdir()
            target.write_bytes(b"builder artifact evidence\n")
        return source_root, trace_path, artifact_root

    def test_builds_self_contained_bundle_and_replays_after_sources_disappear(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            labels_path = source_root / "labels.json"
            labels_path.write_bytes(b'{"schema_version":"labels-test/v1"}')

            built = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=root / "store",
                artifact_root=artifact_root,
                labels_path=labels_path,
            )
            bundle_root = built.root
            self.assertEqual(bundle_root.parent.name, "sha256")
            self.assertEqual(bundle_root.name, built.bundle_id.removeprefix("sha256:"))
            artifact = built.graph._artifact_reader.read("proof")
            self.assertEqual(artifact.content, "builder artifact evidence\n")

            shutil.rmtree(source_root)
            reopened = open_benchmark_trace_bundle(
                bundle_root,
                role="evaluation",
            )
            self.assertEqual(reopened.bundle_id, built.bundle_id)
            self.assertEqual(
                reopened.graph._artifact_reader.read("proof").content,
                "builder artifact evidence\n",
            )
            self.assertIsNotNone(reopened.labels_source)

    def test_normalizes_trace_v6_artifact_media_type_without_rewriting_source_trace(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            trace["artifacts"][0].pop("media_type")
            trace["artifacts"][0]["kind"] = "text"
            source_bytes = canonical_bytes(trace)
            trace_path.write_bytes(source_bytes)

            built = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=root / "store",
                artifact_root=artifact_root,
            )

            self.assertEqual(built.trace_bytes, source_bytes)
            effective = json.loads(built.effective_trace_bytes)
            self.assertEqual(effective["artifacts"][0]["media_type"], "text/plain")

    def test_prunes_unreferenced_trace_artifacts_from_the_effective_bundle(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            orphan = b"orphaned recorder payload\n"
            trace["artifacts"].append(
                {
                    "artifact_id": "orphan",
                    "path": "raw/orphan.txt",
                    "content_hash": sha256(orphan)[:23],
                    "byte_length": len(orphan),
                    "media_type": "text/plain",
                }
            )
            (artifact_root / "raw" / "orphan.txt").write_bytes(orphan)
            source_bytes = canonical_bytes(trace)
            trace_path.write_bytes(source_bytes)

            built = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=root / "store",
                artifact_root=artifact_root,
            )

            self.assertEqual(built.trace_bytes, source_bytes)
            self.assertEqual([item.artifact_id for item in built.artifacts], ["proof"])
            effective = json.loads(built.effective_trace_bytes)
            self.assertEqual(
                [item["artifact_id"] for item in effective["artifacts"]],
                ["proof"],
            )

    def test_preserves_exact_ordered_duplicate_evaluation_sources(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(root)
            first = source_root / "evaluation-a.json"
            second = source_root / "evaluation-b.json"
            payload = canonical_bytes(evaluation("same assertion"))
            first.write_bytes(payload)
            second.write_bytes(payload)

            built = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=root / "store",
                artifact_root=artifact_root,
                evaluation_paths=(first, second),
            )

            self.assertEqual(
                [item.source_index for item in built.evaluation_sources],
                [0, 1],
            )
            self.assertEqual(
                [item.sha256 for item in built.evaluation_sources],
                [sha256(payload), sha256(payload)],
            )
            self.assertNotEqual(
                built.evaluation_sources[0].path,
                built.evaluation_sources[1].path,
            )

    def test_rejects_missing_unknown_and_symlinked_artifacts(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            store = root / "store"

            (artifact_root / "raw" / "proof.txt").unlink()
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )
            self.assertFalse((store / "sha256").exists())

            outside = root / "outside.txt"
            outside.write_text("builder artifact evidence\n", encoding="utf-8")
            (artifact_root / "raw" / "proof.txt").symlink_to(outside)
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

            (artifact_root / "raw" / "proof.txt").unlink()
            (artifact_root / "raw" / "proof.txt").write_bytes(
                b"builder artifact evidence\n"
            )
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            trace["records"][0]["artifact_refs"] = ["artifact:unknown"]
            trace_path.write_bytes(canonical_bytes(trace))
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

    def test_rejects_source_symlinks_and_changed_artifact_without_publication(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            store = root / "store"
            real_trace = source_root / "real-trace.json"
            trace_path.rename(real_trace)
            trace_path.symlink_to(real_trace)
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )
            self.assertFalse((store / "sha256").exists())

            trace_path.unlink()
            real_trace.rename(trace_path)
            import trace_attribution.benchmark_bundle as bundle_module

            original = bundle_module._read_source_artifact

            def mutate_after_read(root_handle, relative_path):
                value = original(root_handle, relative_path)
                target = artifact_root / relative_path
                target.write_bytes(b"changed after verified read")
                return value

            with patch.object(
                bundle_module,
                "_read_source_artifact",
                side_effect=mutate_after_read,
            ):
                with self.assertRaises(ValueError):
                    build_benchmark_trace_bundle(
                        trace_path=trace_path,
                        store_dir=store,
                        artifact_root=artifact_root,
                    )
            self.assertFalse((store / "sha256").exists())

    def test_rejects_artifact_parent_symlink_special_file_and_unsafe_path(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(
                root,
                artifact=True,
            )
            store = root / "store"
            outside = root / "outside"
            outside.mkdir()
            (outside / "proof.txt").write_bytes(b"builder artifact evidence\n")
            shutil.rmtree(artifact_root / "raw")
            (artifact_root / "raw").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

            (artifact_root / "raw").unlink()
            (artifact_root / "raw").mkdir()
            fifo = artifact_root / "raw" / "proof.txt"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

            fifo.unlink()
            fifo.write_bytes(b"builder artifact evidence\n")
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            trace["artifacts"][0]["path"] = "../outside/proof.txt"
            trace_path.write_bytes(canonical_bytes(trace))
            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )
            self.assertFalse((store / "sha256").exists())

    def test_publish_failure_is_atomic_and_existing_identity_is_reused(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(root)
            store = root / "store"
            with patch(
                "trace_attribution.benchmark_bundle.os.rename",
                side_effect=OSError("injected publish failure"),
            ):
                with self.assertRaises(ValueError):
                    build_benchmark_trace_bundle(
                        trace_path=trace_path,
                        store_dir=store,
                        artifact_root=artifact_root,
                    )
            published = list((store / "sha256").glob("[0-9a-f]" * 64))
            self.assertEqual(published, [])

            first = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=store,
                artifact_root=artifact_root,
            )
            before = {
                path.relative_to(first.root).as_posix(): path.read_bytes()
                for path in first.root.rglob("*")
                if path.is_file()
            }
            second = build_benchmark_trace_bundle(
                trace_path=trace_path,
                store_dir=store,
                artifact_root=artifact_root,
            )
            after = {
                path.relative_to(second.root).as_posix(): path.read_bytes()
                for path in second.root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first.bundle_id, second.bundle_id)
            self.assertEqual(before, after)

    def test_rejects_symlinked_output_namespace_before_writing_members(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(root)
            store = root / "store"
            outside = root / "outside"
            store.mkdir()
            outside.mkdir()
            (store / "sha256").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ValueError):
                build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

            self.assertEqual(list(outside.iterdir()), [])

    def test_staging_mutation_at_publish_is_not_left_public(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(root)
            store = root / "store"
            original_rename = os.rename

            def corrupt_then_publish(src, dst, *args, **kwargs):
                if str(src).startswith(".staging-"):
                    staging = next((store / "sha256").glob(".staging-*"))
                    (staging / "inputs" / "trace.json").write_bytes(b"{}")
                return original_rename(src, dst, *args, **kwargs)

            with patch(
                "trace_attribution.benchmark_bundle.os.rename",
                side_effect=corrupt_then_publish,
            ):
                with self.assertRaises(ValueError):
                    build_benchmark_trace_bundle(
                        trace_path=trace_path,
                        store_dir=store,
                        artifact_root=artifact_root,
                    )

            namespace = store / "sha256"
            self.assertEqual(
                [path for path in namespace.iterdir() if not path.name.startswith(".")],
                [],
            )

    def test_concurrent_valid_same_identity_is_reused(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(root)
            store = root / "store"
            original_rename = os.rename
            published_concurrently = False

            def publish_competing_copy(src, dst, *args, **kwargs):
                nonlocal published_concurrently
                if not published_concurrently:
                    published_concurrently = True
                    namespace = store / "sha256"
                    shutil.copytree(namespace / str(src), namespace / str(dst))
                return original_rename(src, dst, *args, **kwargs)

            with patch(
                "trace_attribution.benchmark_bundle.os.rename",
                side_effect=publish_competing_copy,
            ):
                built = build_benchmark_trace_bundle(
                    trace_path=trace_path,
                    store_dir=store,
                    artifact_root=artifact_root,
                )

            self.assertTrue(published_concurrently)
            self.assertEqual(built.root.name, built.bundle_id.removeprefix("sha256:"))
            self.assertEqual(
                [path for path in (store / "sha256").iterdir() if path.name.startswith(".staging-")],
                [],
            )

    def test_cleanup_refuses_a_replaced_staging_identity(self):
        import trace_attribution.benchmark_bundle as bundle_module

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            namespace = Path(tempdir)
            staging = namespace / ".staging-owned"
            saved = namespace / ".saved-owned"
            victim = namespace / "victim"
            staging.mkdir()
            victim.mkdir()
            (victim / "proof.txt").write_text("keep", encoding="utf-8")
            expected = os.stat(staging, follow_symlinks=False)
            os.rename(staging, saved)
            os.rename(victim, staging)
            descriptor = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(ValueError, "identity|changed"):
                    bundle_module._remove_tree_entry(
                        descriptor,
                        staging.name,
                        (expected.st_dev, expected.st_ino),
                    )
            finally:
                os.close(descriptor)

            self.assertEqual(
                (staging / "proof.txt").read_text(encoding="utf-8"),
                "keep",
            )

    def test_rollback_refuses_identity_swapped_during_rename(self):
        import trace_attribution.benchmark_bundle as bundle_module

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            namespace = Path(tempdir)
            target = namespace / ("a" * 64)
            victim = namespace / "victim"
            saved = namespace / ".saved-target"
            target.mkdir()
            victim.mkdir()
            (victim / "proof.txt").write_text("keep", encoding="utf-8")
            expected = os.stat(target, follow_symlinks=False)
            descriptor = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY)
            original_rename = os.rename
            swapped = False

            def swap_before_rename(src, dst, *args, **kwargs):
                nonlocal swapped
                if not swapped and src == target.name:
                    swapped = True
                    original_rename(
                        target.name,
                        saved.name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                    original_rename(
                        victim.name,
                        target.name,
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                return original_rename(src, dst, *args, **kwargs)

            try:
                with patch.object(
                    bundle_module.os,
                    "rename",
                    side_effect=swap_before_rename,
                ):
                    with self.assertRaisesRegex(ValueError, "identity|changed"):
                        bundle_module._rollback_published_bundle(
                            descriptor,
                            target.name,
                            (expected.st_dev, expected.st_ino),
                        )
            finally:
                os.close(descriptor)

            retained = list(namespace.glob(".rollback-*/proof.txt"))
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_text(encoding="utf-8"), "keep")

    def test_thin_cli_builds_with_repeated_ordered_evaluations(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            source_root, trace_path, artifact_root = self.create_sources(root)
            first = source_root / "evaluation-1.json"
            second = source_root / "evaluation-2.json"
            first.write_bytes(canonical_bytes(evaluation("first")))
            second.write_bytes(canonical_bytes(evaluation("second")))
            script = (
                Path(__file__).parents[1]
                / "scripts"
                / "build_benchmark_trace_bundle.py"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).parents[1])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--trace",
                    str(trace_path),
                    "--artifact-root",
                    str(artifact_root),
                    "--evaluation",
                    str(first),
                    "--evaluation",
                    str(second),
                    "--store",
                    str(root / "store"),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads(completed.stdout)
            opened = open_benchmark_trace_bundle(Path(output["bundle_path"]))
            self.assertEqual(
                [source.source_index for source in opened.evaluation_sources],
                [0, 1],
            )
            self.assertEqual(
                json.loads(opened.evaluation_bytes[0])["assertion"],
                "first",
            )
            self.assertEqual(
                json.loads(opened.evaluation_bytes[1])["assertion"],
                "second",
            )

    def test_thin_cli_invalid_input_exits_two_without_publication(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as tempdir:
            root = Path(tempdir)
            _, trace_path, artifact_root = self.create_sources(root)
            trace_path.write_bytes(b'{"manifest":')
            script = (
                Path(__file__).parents[1]
                / "scripts"
                / "build_benchmark_trace_bundle.py"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(Path(__file__).parents[1])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--trace",
                    str(trace_path),
                    "--artifact-root",
                    str(artifact_root),
                    "--store",
                    str(root / "store"),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertTrue(completed.stderr.startswith("error:"), completed.stderr)
            self.assertFalse((root / "store" / "sha256").exists())


if __name__ == "__main__":
    unittest.main()
