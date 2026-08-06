"""Common immutable benchmark input loading for attribution and evaluation."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

from .benchmark_bundle import (
    BenchmarkTraceBundle,
    open_benchmark_trace_bundle,
)
from .benchmark_composition import compose_effective_trace
from .graph import TraceGraph, artifact_root_for_trace_path
from .strict_json import load_json_object


@dataclass(frozen=True)
class LoadedBenchmarkCase:
    source_kind: str
    role: str
    graph: TraceGraph = field(repr=False)
    effective_trace: Mapping[str, Any] = field(repr=False)
    trace_bytes: bytes = field(repr=False)
    review_bytes: Optional[bytes] = field(repr=False)
    evaluation_bytes: Tuple[bytes, ...] = field(repr=False)
    labels_bytes: Optional[bytes] = field(repr=False)
    bundle: Optional[BenchmarkTraceBundle] = field(default=None, repr=False)
    trace_path: Optional[Path] = None
    review_path: Optional[Path] = None
    evaluation_paths: Tuple[Path, ...] = ()
    labels_path: Optional[Path] = None


def load_benchmark_case(
    *,
    bundle_path: Optional[Path] = None,
    trace_path: Optional[Path] = None,
    review_path: Optional[Path] = None,
    evaluation_paths: Sequence[Path] = (),
    labels_path: Optional[Path] = None,
    role: str = "attribution",
) -> LoadedBenchmarkCase:
    """Load one bundle or legacy source set through a single composition path."""

    if role not in {"attribution", "evaluation"}:
        raise ValueError("benchmark case role must be attribution or evaluation")
    evaluation_paths = tuple(Path(path) for path in evaluation_paths)
    raw_supplied = any(
        value is not None
        for value in (trace_path, review_path, labels_path)
    ) or bool(evaluation_paths)
    if bundle_path is not None:
        if raw_supplied:
            raise ValueError("bundle input cannot be combined with raw case inputs")
        bundle = open_benchmark_trace_bundle(
            Path(bundle_path),
            role=role,
        )
        if role == "evaluation" and bundle.labels_bytes is None:
            raise ValueError("evaluation bundle requires verified labels")
        return LoadedBenchmarkCase(
            source_kind="bundle",
            role=role,
            graph=bundle.new_graph(),
            effective_trace=bundle.effective_trace,
            trace_bytes=bundle.trace_bytes,
            review_bytes=bundle.review_bytes,
            evaluation_bytes=bundle.evaluation_bytes,
            labels_bytes=bundle.labels_bytes,
            bundle=bundle,
            trace_path=bundle.trace_path,
            review_path=bundle.review_path,
            evaluation_paths=bundle.evaluation_paths,
            labels_path=bundle.labels_path,
        )

    if trace_path is None:
        raise ValueError("one of bundle_path or trace_path is required")
    if role == "attribution" and labels_path is not None:
        raise ValueError("attribution role cannot load labels")
    if role == "evaluation" and labels_path is None:
        raise ValueError("evaluation role requires labels")

    trace_path = Path(trace_path)
    review_path = Path(review_path) if review_path is not None else None
    labels_path = Path(labels_path) if labels_path is not None else None
    trace_bytes = trace_path.read_bytes()
    trace = load_json_object(trace_bytes, "trace")
    review_bytes = review_path.read_bytes() if review_path is not None else None
    review = (
        load_json_object(review_bytes, "review")
        if review_bytes is not None
        else None
    )
    evaluation_bytes = tuple(path.read_bytes() for path in evaluation_paths)
    evaluations = tuple(
        load_json_object(value, "evaluation {0}".format(index))
        for index, value in enumerate(evaluation_bytes)
    )
    labels_bytes = labels_path.read_bytes() if labels_path is not None else None
    if labels_bytes is not None:
        load_json_object(labels_bytes, "labels")
    effective_trace = compose_effective_trace(
        trace,
        review=review,
        evaluations=evaluations,
    )
    graph = TraceGraph.from_trace(
        copy.deepcopy(effective_trace),
        artifact_root=artifact_root_for_trace_path(trace_path, effective_trace),
    )
    return LoadedBenchmarkCase(
        source_kind="legacy",
        role=role,
        graph=graph,
        effective_trace=_freeze_json(copy.deepcopy(effective_trace)),
        trace_bytes=bytes(trace_bytes),
        review_bytes=bytes(review_bytes) if review_bytes is not None else None,
        evaluation_bytes=tuple(bytes(value) for value in evaluation_bytes),
        labels_bytes=bytes(labels_bytes) if labels_bytes is not None else None,
        trace_path=trace_path,
        review_path=review_path,
        evaluation_paths=evaluation_paths,
        labels_path=labels_path,
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


__all__ = ["LoadedBenchmarkCase", "load_benchmark_case"]
