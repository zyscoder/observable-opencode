#!/usr/bin/env python3
"""Deterministically characterize archived trace fact-closure inputs without an LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Sequence

from trace_attribution.graph import TraceGraph


JsonDict = Dict[str, Any]
HYDRATION_KEYS = ("loaded", "missing", "truncated", "slice_fallbacks", "hash_mismatches")


def broken_claim_fragments(trace: JsonDict) -> list[str]:
    return [
        text
        for record in trace.get("records") or []
        if record.get("event_type") == "response.claim"
        for text in [record.get("data", {}).get("text")]
        if isinstance(text, str)
        and text.lstrip().startswith((",", "，", ";", "；", ")", "）"))
    ]


def characterize_archive(path: Path) -> JsonDict:
    source = Path(path).resolve()
    raw = source.read_bytes()
    trace = json.loads(raw.decode("utf-8"))

    def reconstruct() -> JsonDict:
        graph = TraceGraph.from_file(source)
        for ref in graph.nodes:
            graph.hydrate_node(ref)
        return {
            "artifact_hydration": {
                key: graph.artifact_hydration[key] for key in HYDRATION_KEYS
            },
            "graph_nodes": len(graph.nodes),
            "graph_edges": sum(len(graph.downstream_refs(ref)) for ref in graph.nodes),
        }

    first = reconstruct()
    second = reconstruct()
    if first != second:
        raise RuntimeError("archive reconstruction is not deterministic: {0}".format(source))

    case_root = source.parent.parent if source.parent.name == "partial" else source.parent
    artifact_root = case_root / "artifacts"
    manifest = trace.get("manifest") if isinstance(trace.get("manifest"), dict) else {}
    return {
        "path": str(source),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "case_id": str(manifest.get("case_id") or trace.get("case_id") or ""),
        "broken_claim_fragments": len(broken_claim_fragments(trace)),
        "artifact_hydration": first["artifact_hydration"],
        "indexed_artifacts": len(trace.get("artifacts") or []),
        "artifact_files": sum(1 for item in artifact_root.rglob("*") if item.is_file())
        if artifact_root.exists()
        else 0,
        "external_evaluation_seeds": sum(
            1
            for record in trace.get("records") or []
            if isinstance(record, dict)
            and record.get("event_type") == "external.evaluation_fact"
        ),
        "subject_revision": manifest.get("subject_revision"),
        "ir_nodes": len(trace.get("nodes") or []),
        "ir_edges": len(trace.get("edges") or []),
        "graph_nodes": first["graph_nodes"],
        "graph_edges": first["graph_edges"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            [characterize_archive(path) for path in args.archives],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
