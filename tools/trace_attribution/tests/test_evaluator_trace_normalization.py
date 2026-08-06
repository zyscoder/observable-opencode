from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    _source_trace_violations,
    _validate_artifact,
)
from trace_attribution.graph import TraceGraph


def _record(
    record_id: str,
    *,
    source_refs: list[str] | None = None,
    data: dict | None = None,
) -> dict:
    return {
        "record_id": record_id,
        "component": "test",
        "event_type": "test.event",
        "title": record_id,
        "status": "completed",
        "timestamp": "2026-07-30T00:00:00Z",
        "data": data or {},
        "source_refs": source_refs or [],
    }


class EvaluatorTraceNormalizationTest(unittest.TestCase):
    def test_external_semantic_source_refs_are_not_node_integrity_failures(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "source-ref-normalization",
                "records": [
                    _record("producer"),
                    _record(
                        "consumer",
                        source_refs=[
                            "record:producer",
                            "context:prompt-transform-1",
                            "record:missing",
                        ],
                    ),
                ],
                "dataflow_edges": [
                    {
                        "edge_id": "external-span",
                        "from": {"type": "span", "id": "trace-span-1"},
                        "to": {"type": "node", "id": "producer"},
                        "eligible_for_attribution": True,
                    },
                    {
                        "edge_id": "missing-node",
                        "from": {"type": "node", "id": "missing"},
                        "to": {"type": "node", "id": "producer"},
                        "eligible_for_attribution": True,
                    },
                ],
                "artifacts": [],
            }
        )

        self.assertEqual(
            _source_trace_violations(graph),
            [
                "source_trace_unresolved_attribution_edge:missing-node",
                "source_trace_unresolved_source_ref:record:missing",
            ],
        )

    def test_verified_short_artifact_hash_is_canonicalized_for_evaluation(self):
        content = b"open-source benchmark artifact"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "artifacts" / "prompt.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            graph = TraceGraph.from_trace(
                {
                    "case_id": "artifact-hash-normalization",
                    "records": [
                        _record(
                            "owner",
                            data={"artifact_id": "artifact:prompt"},
                        )
                    ],
                    "dataflow_edges": [],
                    "artifacts": [
                        {
                            "artifact_id": "prompt",
                            "kind": "text",
                            "path": "artifacts/prompt.json",
                            "hash": digest[:16],
                            "content_hash": digest[:16],
                            "byte_length": len(content),
                        }
                    ],
                },
                artifact_root=root,
            )

            self.assertEqual(
                _validate_artifact(
                    graph,
                    "prompt",
                    owner_ref="record:owner",
                ),
                [],
            )

    def test_short_artifact_hash_mismatch_remains_an_integrity_failure(self):
        content = b"open-source benchmark artifact"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "artifacts" / "prompt.json"
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
            graph = TraceGraph.from_trace(
                {
                    "case_id": "artifact-hash-mismatch",
                    "records": [
                        _record(
                            "owner",
                            data={"artifact_id": "artifact:prompt"},
                        )
                    ],
                    "dataflow_edges": [],
                    "artifacts": [
                        {
                            "artifact_id": "prompt",
                            "kind": "text",
                            "path": "artifacts/prompt.json",
                            "hash": "0" * 16,
                            "content_hash": "0" * 16,
                            "byte_length": len(content),
                        }
                    ],
                },
                artifact_root=root,
            )

            self.assertEqual(
                _validate_artifact(
                    graph,
                    "prompt",
                    owner_ref="record:owner",
                ),
                ["artifact_hash_prefix_mismatch:prompt"],
            )


if __name__ == "__main__":
    unittest.main()
