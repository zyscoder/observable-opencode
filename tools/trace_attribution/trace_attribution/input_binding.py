"""Exact factual-input binding for v5 attribution labels."""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .benchmark_composition import compose_effective_trace
from .causal_state import (
    seed_binding_identity_for,
    semantic_anchor_index,
    semantic_occurrence_index,
)
from .evaluation_facts import (
    merge_observed_defect_seed_record,
    reconstruct_external_evaluation_record,
    trace_execution_revision,
)
from .graph import TraceGraph
from .label_contract import V5_LABEL_SCHEMA_VERSION, validate_labels
from .models import JsonDict, stable_json
from .strict_json import StrictJsonError, load_json_object
from .trace_eligibility import build_record_alias_index, resolve_ref


_BUNDLE_ARTIFACT_PATH_PATTERN = re.compile(
    r"^artifacts/sha256/([0-9a-f]{2})/([0-9a-f]{64})$"
)
_NORMALIZABLE_ARTIFACT_FIELDS = frozenset(
    {"path", "hash", "content_hash"}
)


class InputBindingError(ValueError):
    """Raised when labels do not bind exactly to their factual inputs."""


@dataclass(frozen=True)
class SeedSourceBinding:
    source_kind: str
    source_index: int
    source_sha256: str


@dataclass(frozen=True)
class SeedInputBinding:
    defect_id: str
    start_ref: str
    defect_fingerprint: str
    seed_binding_identity: str
    seed_source_bindings: Tuple[SeedSourceBinding, ...]
    expected_outcome: str
    seed_semantic_anchor_id: str
    seed_semantic_occurrence_id: str


@dataclass(frozen=True)
class LabelInputBinding:
    trace_sha256: str
    review_sha256: str
    evaluation_sha256: Tuple[str, ...]
    effective_trace_sha256: str
    case_id: str
    subject_revision: str
    revision_provenance_status: str
    seeds: Tuple[SeedInputBinding, ...]


def _sha256(value: bytes) -> str:
    return "sha256:{0}".format(hashlib.sha256(value).hexdigest())


def _source_bytes(value: Any, label: str) -> bytes:
    if not isinstance(value, bytes):
        raise InputBindingError("{0} must be exact bytes".format(label))
    return value


def _json_object(value: bytes, label: str) -> JsonDict:
    try:
        return load_json_object(value, label)
    except StrictJsonError as error:
        raise InputBindingError(str(error)) from error


def _record_map(trace: Mapping[str, Any], label: str) -> Dict[str, Mapping[str, Any]]:
    records = trace.get("records")
    if records is None:
        return {}
    if not isinstance(records, list):
        raise InputBindingError("{0} records must be a list".format(label))
    output: Dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise InputBindingError("{0} record {1} must be an object".format(label, index))
        record_id = record.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise InputBindingError("{0} record {1} requires record_id".format(label, index))
        ref = "record:{0}".format(record_id)
        if ref in output:
            raise InputBindingError("record ID collision for {0}".format(ref))
        output[ref] = record
    return output


def _without_records(trace: JsonDict, refs: Sequence[str]) -> JsonDict:
    output = copy.deepcopy(trace)
    record_ids = {ref.removeprefix("record:") for ref in refs}
    output["records"] = [
        record
        for record in output.get("records") or []
        if not isinstance(record, Mapping)
        or record.get("record_id") not in record_ids
    ]
    return output


def _is_repository_proven_merge(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
    merged: Mapping[str, Any],
    merged_trace: JsonDict,
) -> bool:
    if (
        existing.get("event_type") == "case.observed_defect"
        and incoming.get("event_type") == "case.observed_defect"
    ):
        candidate = copy.deepcopy(dict(existing))
        try:
            merge_observed_defect_seed_record(candidate, copy.deepcopy(dict(incoming)))
        except (TypeError, ValueError):
            return False
        return candidate == merged
    if existing == incoming == merged:
        reconstructed = reconstruct_external_evaluation_record(
            merged_trace,
            dict(merged),
            merged_trace.get("records") or [],
        )
        return reconstructed == merged
    return False


def _apply_source_boundary(
    stage: JsonDict,
    next_stage: JsonDict,
    origins: Dict[str, Tuple[SeedSourceBinding, ...]],
    source_binding: SeedSourceBinding,
    compose_source: Callable[[JsonDict], JsonDict],
    label: str,
    preserved_probe_refs: Sequence[str] = (),
) -> Dict[str, Tuple[SeedSourceBinding, ...]]:
    stage_records = _record_map(stage, "{0} input".format(label))
    next_records = _record_map(next_stage, "{0} output".format(label))
    removed_refs = set(stage_records) - set(next_records)
    if removed_refs:
        raise InputBindingError(
            "{0} removed factual record {1}".format(label, sorted(removed_refs)[0])
        )
    updated = dict(origins)
    prior_equivalent_refs = {
        ref
        for ref, bindings in origins.items()
        if any(
            binding.source_kind == source_binding.source_kind
            and binding.source_sha256 == source_binding.source_sha256
            for binding in bindings
        )
    }
    preserved_refs = set(preserved_probe_refs) & set(stage_records)
    for ref in sorted(stage_records):
        try:
            isolated_refs = (prior_equivalent_refs - preserved_refs) | {ref}
            probe = compose_source(
                _without_records(stage, tuple(isolated_refs))
            )
            incoming = _record_map(probe, "{0} collision probe".format(label)).get(ref)
        except (InputBindingError, KeyError, TypeError, ValueError) as error:
            raise InputBindingError(
                "{0} collision probe failed for {1}: {2}".format(
                    label, ref, error
                )
            ) from error
        changed = stage_records[ref] != next_records[ref]
        if incoming is None:
            if changed:
                raise InputBindingError("record ID collision for {0}".format(ref))
            continue
        if not _is_repository_proven_merge(
            stage_records[ref], incoming, next_records[ref], next_stage
        ):
            raise InputBindingError("record ID collision for {0}".format(ref))
        updated[ref] = (*updated[ref], source_binding)
    for ref in sorted(set(next_records) - set(stage_records)):
        updated[ref] = (source_binding,)
    return updated


def _record_origins(
    trace: JsonDict,
    review: Optional[JsonDict],
    evaluations: Sequence[JsonDict],
    *,
    trace_digest: str,
    review_digest: str,
    evaluation_digests: Sequence[str],
) -> Tuple[Dict[str, Tuple[SeedSourceBinding, ...]], JsonDict]:
    raw_records = _record_map(trace, "trace")
    raw_binding = SeedSourceBinding("raw_trace", 0, trace_digest)
    origins = {ref: (raw_binding,) for ref in raw_records}
    stage = copy.deepcopy(trace)
    if review is not None:
        review_binding = SeedSourceBinding(
            "quality_review", 0, review_digest
        )

        def compose_review(base: JsonDict) -> JsonDict:
            return compose_effective_trace(base, review=review)

        next_stage = compose_review(stage)
        origins = _apply_source_boundary(
            stage,
            next_stage,
            origins,
            review_binding,
            compose_review,
            "quality review",
        )
        stage = next_stage

    for index, evaluation in enumerate(evaluations):
        evaluation_binding = SeedSourceBinding(
            "external_evaluation", index, evaluation_digests[index]
        )

        def compose_evaluation(
            base: JsonDict,
            current: JsonDict = evaluation,
        ) -> JsonDict:
            return compose_effective_trace(base, evaluations=(current,))

        next_stage = compose_evaluation(stage)
        preserved_refs = _canonical_evidence_refs(
            stage,
            evaluation,
            "external evaluation {0}".format(index),
        )
        origins = _apply_source_boundary(
            stage,
            next_stage,
            origins,
            evaluation_binding,
            compose_evaluation,
            "external evaluation {0}".format(index),
            preserved_refs,
        )
        stage = next_stage
    return origins, stage


def _canonical_evidence_refs(
    stage: JsonDict,
    source: Mapping[str, Any],
    label: str,
) -> Tuple[str, ...]:
    refs = source.get("evidence_refs")
    if refs is None:
        return ()
    if not isinstance(refs, list):
        raise InputBindingError("{0} evidence_refs must be a list".format(label))
    try:
        index = build_record_alias_index(stage.get("records") or [])
    except ValueError as error:
        raise InputBindingError(
            "{0} evidence alias index is ambiguous: {1}".format(label, error)
        ) from error
    canonical = []
    for position, ref in enumerate(refs):
        if not isinstance(ref, str) or not ref:
            raise InputBindingError(
                "{0} evidence alias {1} is unresolved".format(label, position)
            )
        owners = index.owners.get(ref, frozenset())
        resolved = resolve_ref(ref, index.aliases)
        if resolved is None or not owners:
            raise InputBindingError(
                "{0} evidence alias is unresolved: {1}".format(label, ref)
            )
        if len(owners) != 1 or resolved not in owners:
            raise InputBindingError(
                "{0} evidence alias is ambiguous: {1}".format(label, ref)
            )
        canonical.append(resolved)
    return tuple(canonical)


def _prevalidate_seed_identities(labels: Mapping[str, Any]) -> None:
    seeds = labels.get("seeds")
    if not isinstance(seeds, list):
        return
    for seed in seeds:
        if not isinstance(seed, Mapping):
            continue
        start_ref = seed.get("start_ref")
        fingerprint = seed.get("defect_fingerprint")
        supplied = seed.get("seed_binding_identity")
        if not isinstance(start_ref, str) or not isinstance(fingerprint, str):
            continue
        if supplied != seed_binding_identity_for(start_ref, fingerprint):
            raise InputBindingError(
                "start_ref/defect_fingerprint/seed_binding_identity mismatch"
            )


def _prevalidate_canonical_start_refs(labels: Mapping[str, Any]) -> None:
    seeds = labels.get("seeds")
    if not isinstance(seeds, list):
        return
    for index, seed in enumerate(seeds):
        if not isinstance(seed, Mapping):
            continue
        start_ref = seed.get("start_ref")
        if (
            not isinstance(start_ref, str)
            or not start_ref.startswith("record:")
            or not start_ref.removeprefix("record:")
            or any(character.isspace() for character in start_ref)
        ):
            raise InputBindingError(
                "seeds[{0}].start_ref must be a canonical record: ref".format(
                    index
                )
            )


def _require_equal(label: str, supplied: Any, computed: Any) -> None:
    if supplied != computed:
        raise InputBindingError("{0} mismatch".format(label))


def _without_artifact_declarations(trace: Mapping[str, Any]) -> JsonDict:
    output = copy.deepcopy(dict(trace))
    output.pop("artifacts", None)
    return output


def _reject_artifact_normalization(reason: str) -> None:
    raise InputBindingError("artifact normalization mismatch: {0}".format(reason))


def _validate_artifact_normalization(
    composed_trace: Mapping[str, Any],
    authoritative_trace: Mapping[str, Any],
) -> None:
    if ("artifacts" in composed_trace) != ("artifacts" in authoritative_trace):
        _reject_artifact_normalization("artifact list presence changed")
    composed_artifacts = composed_trace.get("artifacts")
    authoritative_artifacts = authoritative_trace.get("artifacts")
    if not isinstance(composed_artifacts, list) or not isinstance(
        authoritative_artifacts, list
    ):
        _reject_artifact_normalization("artifacts must be lists")
    if len(composed_artifacts) != len(authoritative_artifacts):
        _reject_artifact_normalization("artifact list cardinality changed")
    for index, (composed, authoritative) in enumerate(
        zip(composed_artifacts, authoritative_artifacts)
    ):
        if not isinstance(composed, Mapping) or not isinstance(
            authoritative, Mapping
        ):
            _reject_artifact_normalization(
                "artifact {0} must remain an object".format(index)
            )
        if set(composed) != set(authoritative):
            _reject_artifact_normalization(
                "artifact {0} key set changed".format(index)
            )
        for side, artifact in (
            ("composed", composed),
            ("authoritative", authoritative),
        ):
            byte_length = artifact.get("byte_length")
            if type(byte_length) is not int or byte_length < 0:
                _reject_artifact_normalization(
                    "artifact {0} {1} byte_length must be a non-negative integer".format(
                        index, side
                    )
                )
        for field in set(composed) - _NORMALIZABLE_ARTIFACT_FIELDS:
            if composed[field] != authoritative[field]:
                _reject_artifact_normalization(
                    "artifact {0} field {1} changed".format(index, field)
                )
        path = authoritative.get("path")
        match = (
            _BUNDLE_ARTIFACT_PATH_PATTERN.fullmatch(path)
            if isinstance(path, str)
            else None
        )
        if match is None or match.group(1) != match.group(2)[:2]:
            _reject_artifact_normalization(
                "artifact {0} path is not content addressed".format(index)
            )
        digest = "sha256:{0}".format(match.group(2))
        hash_fields = [
            field
            for field in ("hash", "content_hash")
            if field in authoritative
        ]
        if not hash_fields:
            _reject_artifact_normalization(
                "artifact {0} has no retained hash field".format(index)
            )
        for field in hash_fields:
            if authoritative[field] != digest:
                _reject_artifact_normalization(
                    "artifact {0} {1} does not match its path".format(
                        index, field
                    )
                )


def _verified_effective_trace_and_graph(
    composed_trace: JsonDict,
    effective_trace: Optional[Mapping[str, Any]],
    graph: Optional[TraceGraph],
    bundle: Optional[Any],
    *,
    trace_digest: str,
    review_digest: str,
    evaluation_digests: Sequence[str],
) -> Tuple[JsonDict, TraceGraph]:
    if bundle is not None:
        if effective_trace is not None or graph is not None:
            raise InputBindingError(
                "bundle cannot be combined with effective_trace or graph"
            )
        return _verified_bundle_trace_and_graph(
            composed_trace,
            bundle,
            trace_digest=trace_digest,
            review_digest=review_digest,
            evaluation_digests=evaluation_digests,
        )
    if (effective_trace is None) != (graph is None):
        raise InputBindingError(
            "effective_trace and graph must be supplied together"
        )
    if effective_trace is None:
        return composed_trace, TraceGraph.from_trace(composed_trace)
    if not isinstance(effective_trace, Mapping):
        raise InputBindingError("effective_trace must be a Trace object")
    if not isinstance(graph, TraceGraph):
        raise InputBindingError("graph must be a TraceGraph")
    authoritative_trace = copy.deepcopy(dict(effective_trace))
    if stable_json(graph.raw_trace) != stable_json(authoritative_trace):
        raise InputBindingError("graph/raw-trace mismatch")
    if stable_json(_without_artifact_declarations(composed_trace)) != stable_json(
        _without_artifact_declarations(authoritative_trace)
    ):
        raise InputBindingError(
            "authoritative effective Trace factual records mismatch"
        )
    if stable_json(composed_trace) != stable_json(authoritative_trace):
        _validate_artifact_normalization(composed_trace, authoritative_trace)
        raise InputBindingError(
            "artifact-normalized effective Trace requires a verified benchmark bundle"
        )
    return authoritative_trace, TraceGraph.from_trace(
        copy.deepcopy(authoritative_trace)
    )


def _mutable_json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _mutable_json_copy(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_mutable_json_copy(item) for item in value]
    return copy.deepcopy(value)


def _verified_bundle_trace_and_graph(
    composed_trace: JsonDict,
    bundle: Any,
    *,
    trace_digest: str,
    review_digest: str,
    evaluation_digests: Sequence[str],
) -> Tuple[JsonDict, TraceGraph]:
    from .benchmark_bundle import BenchmarkTraceBundle

    if type(bundle) is not BenchmarkTraceBundle:
        raise InputBindingError(
            "bundle must be a fully verified BenchmarkTraceBundle"
        )
    expected_review = bundle.review_source.sha256 if bundle.review_source else ""
    expected_evaluations = tuple(
        source.sha256 for source in bundle.evaluation_sources
    )
    _require_equal("bundle trace_sha256", bundle.trace_source.sha256, trace_digest)
    _require_equal("bundle review_sha256", expected_review, review_digest)
    _require_equal(
        "bundle evaluation_sha256",
        expected_evaluations,
        tuple(evaluation_digests),
    )
    authoritative_trace = _mutable_json_copy(bundle.effective_trace)
    if stable_json(_without_artifact_declarations(composed_trace)) != stable_json(
        _without_artifact_declarations(authoritative_trace)
    ):
        raise InputBindingError(
            "authoritative effective Trace factual records mismatch"
        )
    if stable_json(composed_trace) != stable_json(authoritative_trace):
        _validate_artifact_normalization(composed_trace, authoritative_trace)
    bound_graph = bundle.new_graph()
    if stable_json(bound_graph.raw_trace) != stable_json(authoritative_trace):
        raise InputBindingError(
            "verified benchmark bundle graph does not match its effective Trace"
        )
    return authoritative_trace, bound_graph


def _bind_label_inputs(
    labels_value: Mapping[str, Any],
    *,
    trace_bytes: bytes,
    review_bytes: Optional[bytes],
    evaluation_bytes: Sequence[bytes],
    effective_trace: Optional[Mapping[str, Any]],
    graph: Optional[TraceGraph],
    bundle: Optional[Any],
) -> LabelInputBinding:
    _prevalidate_canonical_start_refs(labels_value)
    _prevalidate_seed_identities(labels_value)
    labels = validate_labels(labels_value)
    if labels["schema_version"] != V5_LABEL_SCHEMA_VERSION:
        raise InputBindingError("input binding requires v5 labels")

    trace_source = _source_bytes(trace_bytes, "trace_bytes")
    review_source = (
        _source_bytes(review_bytes, "review_bytes")
        if review_bytes is not None
        else None
    )
    evaluation_sources = tuple(
        _source_bytes(value, "evaluation_bytes[{0}]".format(index))
        for index, value in enumerate(evaluation_bytes)
    )
    computed_source_binding = {
        "trace_sha256": _sha256(trace_source),
        "review_sha256": _sha256(review_source) if review_source is not None else "",
        "evaluation_sha256": tuple(_sha256(value) for value in evaluation_sources),
    }
    source_binding = labels["source_binding"]
    _require_equal(
        "trace_sha256", source_binding["trace_sha256"], computed_source_binding["trace_sha256"]
    )
    _require_equal(
        "review_sha256", source_binding["review_sha256"], computed_source_binding["review_sha256"]
    )
    _require_equal(
        "evaluation_sha256",
        tuple(source_binding["evaluation_sha256"]),
        computed_source_binding["evaluation_sha256"],
    )

    trace = _json_object(trace_source, "trace_bytes")
    review = _json_object(review_source, "review_bytes") if review_source is not None else None
    evaluations = tuple(
        _json_object(value, "evaluation_bytes[{0}]".format(index))
        for index, value in enumerate(evaluation_sources)
    )
    composed_effective_trace = compose_effective_trace(
        trace,
        review=review,
        evaluations=evaluations,
    )
    origins, boundary_effective_trace = _record_origins(
        trace,
        review,
        evaluations,
        trace_digest=computed_source_binding["trace_sha256"],
        review_digest=computed_source_binding["review_sha256"],
        evaluation_digests=computed_source_binding["evaluation_sha256"],
    )
    if stable_json(boundary_effective_trace) != stable_json(
        composed_effective_trace
    ):
        raise InputBindingError("effective trace composition boundary mismatch")
    bound_effective_trace, bound_graph = _verified_effective_trace_and_graph(
        composed_effective_trace,
        effective_trace,
        graph,
        bundle,
        trace_digest=computed_source_binding["trace_sha256"],
        review_digest=computed_source_binding["review_sha256"],
        evaluation_digests=computed_source_binding["evaluation_sha256"],
    )
    effective_digest = _sha256(
        stable_json(bound_effective_trace).encode("utf-8")
    )
    _require_equal(
        "effective_trace_sha256",
        source_binding["effective_trace_sha256"],
        effective_digest,
    )

    _require_equal("case_id", labels["case_id"], bound_graph.case_id)
    subject_revision, revision_status = trace_execution_revision(
        bound_effective_trace
    )
    if revision_status != "valid" or subject_revision is None:
        raise InputBindingError(
            "revision_provenance_status mismatch: expected valid, computed {0}".format(
                revision_status
            )
        )
    _require_equal(
        "subject_revision", source_binding["subject_revision"], subject_revision
    )
    _require_equal(
        "revision_provenance_status",
        source_binding["revision_provenance_status"],
        revision_status,
    )

    anchors = semantic_anchor_index(bound_graph.case_id, bound_graph)
    occurrences = semantic_occurrence_index(bound_graph.case_id, bound_graph)
    seeds = []
    for index, seed in enumerate(labels["seeds"]):
        canonical_start_ref = bound_graph.resolve(seed["start_ref"])
        if canonical_start_ref != seed["start_ref"]:
            raise InputBindingError(
                "seeds[{0}].start_ref must be a canonical record: ref".format(
                    index
                )
            )
        if canonical_start_ref is None or canonical_start_ref not in origins:
            raise InputBindingError(
                "seeds[{0}].start_ref does not resolve to a factual source record".format(
                    index
                )
            )
        expected_identity = seed_binding_identity_for(
            canonical_start_ref, seed["defect_fingerprint"]
        )
        _require_equal(
            "seeds[{0}].seed_binding_identity".format(index),
            seed["seed_binding_identity"],
            expected_identity,
        )
        _require_equal(
            "seeds[{0}].seed_semantic_anchor_id".format(index),
            seed["seed_semantic_anchor_id"],
            anchors[canonical_start_ref],
        )
        _require_equal(
            "seeds[{0}].seed_semantic_occurrence_id".format(index),
            seed["seed_semantic_occurrence_id"],
            occurrences[canonical_start_ref],
        )
        supplied_source_bindings = tuple(
            SeedSourceBinding(
                source_kind=item["source_kind"],
                source_index=item["source_index"],
                source_sha256=item["source_sha256"],
            )
            for item in seed["seed_source_bindings"]
        )
        _require_equal(
            "seeds[{0}].seed_source_bindings".format(index),
            supplied_source_bindings,
            origins[canonical_start_ref],
        )
        seeds.append(
            SeedInputBinding(
                defect_id=seed["defect_id"],
                start_ref=canonical_start_ref,
                defect_fingerprint=seed["defect_fingerprint"],
                seed_binding_identity=expected_identity,
                seed_source_bindings=origins[canonical_start_ref],
                expected_outcome=seed["expected_outcome"],
                seed_semantic_anchor_id=anchors[canonical_start_ref],
                seed_semantic_occurrence_id=occurrences[canonical_start_ref],
            )
        )
    return LabelInputBinding(
        trace_sha256=computed_source_binding["trace_sha256"],
        review_sha256=computed_source_binding["review_sha256"],
        evaluation_sha256=computed_source_binding["evaluation_sha256"],
        effective_trace_sha256=effective_digest,
        case_id=bound_graph.case_id,
        subject_revision=subject_revision,
        revision_provenance_status=revision_status,
        seeds=tuple(seeds),
    )


def bind_label_inputs(
    labels: Mapping[str, Any],
    *,
    trace_bytes: bytes,
    review_bytes: Optional[bytes] = None,
    evaluation_bytes: Sequence[bytes] = (),
    effective_trace: Optional[Mapping[str, Any]] = None,
    graph: Optional[TraceGraph] = None,
    bundle: Optional[Any] = None,
) -> LabelInputBinding:
    """Validate exact v5 factual inputs and return their immutable binding."""
    try:
        return _bind_label_inputs(
            labels,
            trace_bytes=trace_bytes,
            review_bytes=review_bytes,
            evaluation_bytes=evaluation_bytes,
            effective_trace=effective_trace,
            graph=graph,
            bundle=bundle,
        )
    except InputBindingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise InputBindingError(str(error)) from error


validate_label_input_binding = bind_label_inputs
LabelSeedInputBinding = SeedInputBinding


__all__ = [
    "InputBindingError",
    "LabelInputBinding",
    "LabelSeedInputBinding",
    "SeedInputBinding",
    "SeedSourceBinding",
    "bind_label_inputs",
    "validate_label_input_binding",
]
