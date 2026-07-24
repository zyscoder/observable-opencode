#!/usr/bin/env python3
"""Strict offline acceptance metrics for recursive causal attribution reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from trace_attribution.causal_state import (
    LocalStateOwner,
    RecursiveAttributionReport,
    confirmation_identity_for,
    seed_binding_identity_for,
    semantic_anchor_index,
    semantic_occurrence_index,
    validate_seed_outcome_payload,
)
from trace_attribution.graph import TraceGraph, collect_artifact_ids, resolve_edge_endpoint
from trace_attribution.models import TraceNode, stable_json
from trace_attribution.recursive_analyzer import (
    validate_recursive_report_against_graph,
)


JsonDict = Dict[str, Any]
LABEL_SCHEMA_VERSION = "recursive-attribution-labels/v3"
COMPARISON_SCHEMA_VERSION = "recursive-attribution-comparison/v5"
REPORT_SCHEMA_VERSION = "recursive-attribution-report/v11"
SEMANTIC_ANCHOR_PREFIX = "semantic_anchor:v2:"
SEMANTIC_OCCURRENCE_PREFIX = "semantic_occurrence:v1:"
TEMPORAL_RELATIONS = frozenset(
    {"temporal_proximity", "temporal_sequence", "previous_event", "next_event"}
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "roots",
        "conditions",
        "amplifiers",
        "forbidden_roots",
        "allowed_unresolved_outcomes",
    }
)
LABEL_ENTRY_REQUIRED_KEYS = frozenset(
    {"semantic_anchor_id", "semantic_occurrence_id"}
)
LABEL_ENTRY_ALLOWED_KEYS = frozenset(
    {"node_ref", "semantic_anchor_id", "semantic_occurrence_id"}
)
FIXTURE_CONTROL_KEYS = frozenset({"human_labels", "scripted_analysis"})
FIXTURE_ALLOWED_KEYS = frozenset(
    {
        "case_id",
        "trace_schema_version",
        "manifest",
        "records",
        "dataflow_edges",
        "artifacts",
        "human_labels",
        "scripted_analysis",
    }
)
REPORT_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "objective",
        "start_refs",
        "seed_results",
        "analysis_outcome",
        "analysis_perspective",
        "defect_states",
        "causal_candidates",
        "causal_relations",
        "step_judgments",
        "hypotheses",
        "introduction_candidates",
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "rejected_candidates",
        "unresolved_hypotheses",
        "root_causes",
        "taint_paths",
        "visited_order",
        "visited_entries",
        "unresolved_refs",
        "investigation_journal",
        "metadata",
    }
)
SEED_RESULT_KEYS = frozenset(
    {
        "start_ref",
        "seed_binding_identity",
        "defect_fingerprint",
        "defect_state",
        "outcome",
        "candidate_refs",
        "selected_candidate_refs",
        "confirmation_identities",
        "confirmed_root_refs",
        "decisive_evidence_refs",
        "decisive_evidence",
        "missing_evidence",
        "blocking_reasons",
        "global_judgment",
        "expansion_history",
    }
)


class EvaluationSchemaError(ValueError):
    """Raised when labels, fixtures, or reports violate the benchmark schema."""


class EvaluationSafetyError(ValueError):
    """Raised when a report would score a causally ungrounded root."""


def _exact_keys(
    value: Mapping[str, Any], expected: Union[Set[str], frozenset], label: str
) -> None:
    actual = set(value)
    if actual != set(expected):
        raise EvaluationSchemaError(
            "{0} keys differ: missing={1}, extra={2}".format(
                label, sorted(set(expected) - actual), sorted(actual - set(expected))
            )
        )


def _list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise EvaluationSchemaError("{0} must be a list".format(label))
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationSchemaError("{0} must be an object".format(label))
    return value


def validate_labels(value: Mapping[str, Any]) -> JsonDict:
    labels = dict(_mapping(value, "labels"))
    _exact_keys(labels, LABEL_KEYS, "labels")
    if labels["schema_version"] != LABEL_SCHEMA_VERSION:
        raise EvaluationSchemaError("unsupported labels schema_version")
    if not isinstance(labels["case_id"], str) or not labels["case_id"]:
        raise EvaluationSchemaError("labels case_id must be a non-empty string")
    for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
        entries = _list(labels[role], "labels.{0}".format(role))
        occurrences: Set[str] = set()
        refs: Set[str] = set()
        for index, raw in enumerate(entries):
            item = dict(_mapping(raw, "labels.{0}[{1}]".format(role, index)))
            missing = set(LABEL_ENTRY_REQUIRED_KEYS) - set(item)
            extra = set(item) - set(LABEL_ENTRY_ALLOWED_KEYS)
            if missing or extra:
                raise EvaluationSchemaError(
                    "labels.{0}[{1}] keys differ: missing={2}, extra={3}".format(
                        role, index, sorted(missing), sorted(extra)
                    )
                )
            node_ref = item.get("node_ref")
            anchor = item["semantic_anchor_id"]
            occurrence = item["semantic_occurrence_id"]
            if node_ref is not None and (
                not isinstance(node_ref, str) or not node_ref.startswith("record:")
            ):
                raise EvaluationSchemaError("label node_ref must be a record ref")
            if not isinstance(anchor, str) or not anchor.startswith(SEMANTIC_ANCHOR_PREFIX):
                raise EvaluationSchemaError("label semantic_anchor_id must be versioned")
            if not isinstance(occurrence, str) or not occurrence.startswith(
                SEMANTIC_OCCURRENCE_PREFIX
            ):
                raise EvaluationSchemaError(
                    "label semantic_occurrence_id must be versioned"
                )
            if occurrence in occurrences or (node_ref is not None and node_ref in refs):
                raise EvaluationSchemaError("duplicate label identity in {0}".format(role))
            if node_ref is not None:
                refs.add(node_ref)
            occurrences.add(occurrence)
    outcomes = _list(labels["allowed_unresolved_outcomes"], "allowed_unresolved_outcomes")
    if any(
        item not in {"inconclusive", "partial", "partial_root_found"}
        for item in outcomes
    ):
        raise EvaluationSchemaError(
            "allowed_unresolved_outcomes contains an unsupported state"
        )
    return labels


def _find_control_key(value: Any, path: str = "trace") -> Optional[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FIXTURE_CONTROL_KEYS or str(key) == "expected_roots":
                return "{0}.{1}".format(path, key)
            nested = _find_control_key(item, "{0}.{1}".format(path, key))
            if nested:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _find_control_key(item, "{0}[{1}]".format(path, index))
            if nested:
                return nested
    return None


def load_fixture(path: Path) -> Tuple[JsonDict, JsonDict, JsonDict]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    document = dict(_mapping(value, "fixture"))
    unknown = set(document) - set(FIXTURE_ALLOWED_KEYS)
    if unknown:
        raise EvaluationSchemaError("fixture has unsupported keys: {0}".format(sorted(unknown)))
    for required in ("case_id", "records", "dataflow_edges", "human_labels", "scripted_analysis"):
        if required not in document:
            raise EvaluationSchemaError("fixture is missing {0}".format(required))
    labels = validate_labels(_mapping(document["human_labels"], "fixture human_labels"))
    if labels["case_id"] != document["case_id"]:
        raise EvaluationSchemaError("fixture and labels case_id differ")
    script = dict(_mapping(document["scripted_analysis"], "scripted_analysis"))
    if not script:
        raise EvaluationSchemaError("scripted_analysis must not be empty")
    trace = {
        key: value
        for key, value in document.items()
        if key not in FIXTURE_CONTROL_KEYS
    }
    leak = _find_control_key(trace)
    if leak:
        raise EvaluationSchemaError("human labels leaked into factual trace at {0}".format(leak))
    return trace, labels, script


def _items(value: Any) -> List[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _anchors(items: Iterable[Mapping[str, Any]]) -> List[str]:
    output: List[str] = []
    for item in items:
        anchor = item.get("semantic_anchor_id")
        if isinstance(anchor, str) and anchor:
            output.append(anchor)
    return output


def _occurrences(items: Iterable[Mapping[str, Any]]) -> List[str]:
    output: List[str] = []
    for item in items:
        occurrence = item.get("semantic_occurrence_id")
        if isinstance(occurrence, str) and occurrence:
            output.append(occurrence)
    return output


def _label_anchors(labels: Mapping[str, Any], role: str) -> List[str]:
    return [str(item["semantic_anchor_id"]) for item in labels[role]]


def _label_occurrences(labels: Mapping[str, Any], role: str) -> List[str]:
    return [str(item["semantic_occurrence_id"]) for item in labels[role]]


def _safe_rate(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise EvaluationSafetyError(
            "ratio_bounds_invalid:{0}/{1}".format(numerator, denominator)
        )
    if denominator == 0:
        return empty
    return round(numerator / denominator, 6)


def _request_count(report: Optional[Mapping[str, Any]]) -> Optional[int]:
    if report is None:
        return None
    metadata = report.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    for key in ("physical_judge_request_count", "judge_request_count", "provider_request_count"):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    provider = metadata.get("provider_and_cache_metrics")
    if isinstance(provider, Mapping):
        value = provider.get("physical_request_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _unresolved_refs(report: Mapping[str, Any]) -> Set[str]:
    refs = {str(item) for item in report.get("unresolved_refs") or [] if isinstance(item, str)}
    metadata = report.get("metadata")
    if isinstance(metadata, Mapping):
        for item in _items(metadata.get("unresolved_branches")):
            value = item.get("node_ref")
            if isinstance(value, str) and value:
                refs.add(value)
    return refs


def _validate_report_shape(report: Mapping[str, Any], labels: Mapping[str, Any]) -> None:
    _exact_keys(report, REPORT_KEYS, "report")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise EvaluationSchemaError("recursive report schema_version must be {0}".format(REPORT_SCHEMA_VERSION))
    if report.get("case_id") != labels["case_id"]:
        raise EvaluationSchemaError("report and labels case_id differ")
    for key in (
        "seed_results",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "confirmations",
        "causal_relations",
        "step_judgments",
        "unresolved_hypotheses",
        "unresolved_refs",
        "investigation_journal",
        "visited_entries",
    ):
        _list(report.get(key), "report.{0}".format(key))
    seed_identities: Set[Tuple[str, str]] = set()
    seed_bindings: Set[str] = set()
    report_start_refs = {item for item in _list(report.get("start_refs"), "report.start_refs") if isinstance(item, str)}
    for index, raw in enumerate(report["seed_results"]):
        item = _mapping(raw, "report.seed_results[{0}]".format(index))
        _exact_keys(
            item,
            SEED_RESULT_KEYS,
            "report.seed_results[{0}]".format(index),
        )
        start_ref = item.get("start_ref")
        fingerprint = item.get("defect_fingerprint")
        if not isinstance(start_ref, str) or not start_ref:
            raise EvaluationSchemaError("seed result start_ref must be non-empty")
        if start_ref not in report_start_refs:
            raise EvaluationSchemaError(
                "seed result start_ref must belong to report.start_refs"
            )
        if not isinstance(fingerprint, str) or not fingerprint:
            raise EvaluationSchemaError(
                "seed result defect_fingerprint must be non-empty"
            )
        identity = (start_ref, fingerprint)
        if identity in seed_identities:
            raise EvaluationSchemaError("duplicate seed result identity")
        seed_identities.add(identity)
        expected_seed_binding = seed_binding_identity_for(start_ref, fingerprint)
        if item.get("seed_binding_identity") != expected_seed_binding:
            raise EvaluationSchemaError(
                "seed result seed_binding_identity is missing or inconsistent"
            )
        seed_bindings.add(expected_seed_binding)
        if item.get("outcome") not in {
            "confirmed_root",
            "no_defect",
            "evidence_gap",
            "inconclusive",
        }:
            raise EvaluationSchemaError("unsupported seed result outcome")
        _mapping(item.get("defect_state"), "seed result defect_state")
        _mapping(item.get("global_judgment"), "seed result global_judgment")
        for key in (
            "candidate_refs",
            "selected_candidate_refs",
            "confirmation_identities",
            "confirmed_root_refs",
            "decisive_evidence_refs",
            "decisive_evidence",
            "missing_evidence",
            "blocking_reasons",
            "expansion_history",
        ):
            _list(item.get(key), "seed result {0}".format(key))
        decisive_refs = []
        for evidence_index, evidence in enumerate(item["decisive_evidence"]):
            evidence = _mapping(
                evidence,
                "seed result decisive_evidence[{0}]".format(evidence_index),
            )
            _exact_keys(
                evidence,
                {"ref", "owner"},
                "seed result decisive_evidence[{0}]".format(evidence_index),
            )
            decisive_refs.append(str(evidence.get("ref") or ""))
            _validate_local_owner(
                evidence.get("owner"),
                expected_seed_binding=expected_seed_binding,
                label="seed result decisive_evidence[{0}]".format(
                    evidence_index
                ),
            )
        if sorted(set(decisive_refs)) != sorted(
            set(item["decisive_evidence_refs"])
        ):
            raise EvaluationSchemaError(
                "seed result decisive evidence aggregate is inconsistent"
            )
        global_judgment = item["global_judgment"]
        if global_judgment:
            _validate_local_owner(
                global_judgment.get("owner"),
                expected_seed_binding=expected_seed_binding,
                label="seed result global_judgment",
            )
        for expansion_index, expansion in enumerate(item["expansion_history"]):
            expansion = _mapping(
                expansion,
                "seed result expansion_history[{0}]".format(expansion_index),
            )
            _validate_local_owner(
                expansion.get("owner"),
                expected_seed_binding=expected_seed_binding,
                label="seed result expansion_history[{0}]".format(
                    expansion_index
                ),
            )
        try:
            validate_seed_outcome_payload(
                outcome=str(item.get("outcome") or ""),
                confirmed_root_refs=item.get("confirmed_root_refs") or (),
                missing_evidence=item.get("missing_evidence") or (),
                blocking_reasons=item.get("blocking_reasons") or (),
            )
        except ValueError as exc:
            raise EvaluationSchemaError(str(exc)) from exc
    if {identity[0] for identity in seed_identities} != report_start_refs:
        raise EvaluationSchemaError(
            "v3 seed_results must cover exactly report.start_refs"
        )
    for key in ("causal_relations", "step_judgments", "visited_entries"):
        for index, raw in enumerate(report[key]):
            item = _mapping(raw, "report.{0}[{1}]".format(key, index))
            _validate_local_owner(
                item.get("owner"),
                valid_seed_bindings=seed_bindings,
                label="report.{0}[{1}]".format(key, index),
            )
            if key == "step_judgments":
                for predecessor_index, predecessor in enumerate(
                    _list(
                        item.get("predecessors"),
                        "report.step_judgments[{0}].predecessors".format(index),
                    )
                ):
                    predecessor = _mapping(
                        predecessor,
                        "report.step_judgments[{0}].predecessors[{1}]".format(
                            index, predecessor_index
                        ),
                    )
                    _validate_local_owner(
                        predecessor.get("owner"),
                        valid_seed_bindings=seed_bindings,
                        label=(
                            "report.step_judgments[{0}].predecessors[{1}]".format(
                                index, predecessor_index
                            )
                        ),
                    )
    metadata = _mapping(report.get("metadata"), "report.metadata")
    _list(
        metadata.get("confirmation_action_projection"),
        "report.metadata.confirmation_action_projection",
    )
    for key in (
        "confirmation_queue",
        "confirmation_journal",
        "confirmation_action_projection",
        "global_candidate_judgments",
        "candidate_compression",
        "recursive_expansion_reasons",
    ):
        for index, raw in enumerate(metadata.get(key) or ()):
            item = _mapping(raw, "report.metadata.{0}[{1}]".format(key, index))
            _validate_local_owner(
                item.get("owner"),
                valid_seed_bindings=seed_bindings,
                label="report.metadata.{0}[{1}]".format(key, index),
            )
    for index, raw in enumerate(report["investigation_journal"]):
        item = _mapping(raw, "report.investigation_journal[{0}]".format(index))
        if item.get("kind") == "global_candidate_pass" and item.get("seed_ref"):
            _validate_local_owner(
                item.get("owner"),
                valid_seed_bindings=seed_bindings,
                label="report.investigation_journal[{0}]".format(index),
            )


def _validate_local_owner(
    value: Any,
    *,
    label: str,
    expected_seed_binding: str = "",
    valid_seed_bindings: Optional[Set[str]] = None,
) -> None:
    try:
        owner = LocalStateOwner.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationSchemaError("{0} owner is invalid: {1}".format(label, exc))
    if expected_seed_binding and owner.seed_binding_identity != expected_seed_binding:
        raise EvaluationSchemaError("{0} owner seed binding is inconsistent".format(label))
    if (
        valid_seed_bindings is not None
        and owner.seed_binding_identity not in valid_seed_bindings
    ):
        raise EvaluationSchemaError("{0} owner has no report seed".format(label))


def _without_projection_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_projection_fields(item)
            for key, item in value.items()
            if str(key) not in {"semantic_anchor_id", "semantic_occurrence_id"}
        }
    if isinstance(value, list):
        return [_without_projection_fields(item) for item in value]
    return value


def _node_payload(node: TraceNode) -> JsonDict:
    return {
        "ref": node.ref,
        "record_id": node.record_id,
        "component": node.component,
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
        "timestamp": node.timestamp,
        "data": dict(node.data),
        "source_refs": list(node.source_refs),
    }


def _source_node_payload(graph: TraceGraph, ref: str) -> JsonDict:
    record = graph._artifact_records.get(ref)
    if not isinstance(record, Mapping):
        return _node_payload(graph.nodes[ref])
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    return {
        "ref": ref,
        "record_id": str(record.get("record_id") or ""),
        "component": str(record.get("component") or ""),
        "event_type": str(record.get("event_type") or ""),
        "title": str(record.get("title") or ""),
        "status": str(record.get("status") or ""),
        "timestamp": str(record.get("timestamp") or ""),
        "data": dict(data),
        "source_refs": [str(item) for item in record.get("source_refs") or []],
    }


def _canonical_artifacts(graph: TraceGraph) -> Tuple[Dict[str, JsonDict], Dict[str, Set[str]]]:
    artifacts = {
        str(item.get("artifact_id") or "").removeprefix("artifact:"): dict(item)
        for item in graph.raw_trace.get("artifacts") or []
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    owners: Dict[str, Set[str]] = {}
    for ref, record in graph._artifact_records.items():
        data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
        for artifact_id in collect_artifact_ids(dict(record), dict(data)):
            owners.setdefault(artifact_id, set()).add(ref)
    return artifacts, owners


def _validate_artifact(
    graph: TraceGraph,
    artifact_id: str,
    *,
    owner_ref: str,
    envelope: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    violations: List[str] = []
    normalized = artifact_id.removeprefix("artifact:")
    artifacts, owners = _canonical_artifacts(graph)
    artifact = artifacts.get(normalized)
    if artifact is None:
        return ["artifact_absent_from_trace:{0}".format(normalized)]
    if owner_ref and owner_ref not in owners.get(normalized, set()):
        violations.append("artifact_owner_mismatch:{0}:{1}".format(normalized, owner_ref))
    expected_hash = str(artifact.get("hash") or artifact.get("content_hash") or "").casefold()
    if not SHA256_PATTERN.fullmatch(expected_hash):
        violations.append("artifact_hash_not_canonical:{0}".format(normalized))
        return violations
    artifact_root = graph._artifact_root
    artifact_path = artifact.get("path")
    if artifact_root is None or not isinstance(artifact_path, str) or not artifact_path:
        violations.append("artifact_content_unavailable:{0}".format(normalized))
        return violations
    root = Path(artifact_root).resolve()
    candidate = (root / artifact_path).resolve()
    try:
        candidate.relative_to(root)
        content = candidate.read_bytes()
    except (ValueError, OSError):
        violations.append("artifact_content_unavailable:{0}".format(normalized))
        return violations
    actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        violations.append("artifact_content_hash_mismatch:{0}".format(normalized))
    if envelope is None:
        return violations
    envelope_hash = str(envelope.get("content_hash") or envelope.get("hash") or "").casefold()
    if envelope_hash and envelope_hash != actual_hash:
        violations.append("artifact_envelope_hash_mismatch:{0}".format(normalized))
    raw_range = envelope.get("byte_range")
    if raw_range is None:
        start, end = 0, len(content)
    elif (
        isinstance(raw_range, list)
        and len(raw_range) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in raw_range)
    ):
        start, end = raw_range
    else:
        violations.append("artifact_byte_range_invalid:{0}".format(normalized))
        return violations
    if start < 0 or end < start or end > len(content):
        violations.append("artifact_byte_range_invalid:{0}".format(normalized))
        return violations
    excerpt = envelope.get("content")
    if isinstance(excerpt, str):
        expected_excerpt = content[start:end].decode("utf-8", errors="replace")
        if excerpt != expected_excerpt:
            violations.append("artifact_content_range_mismatch:{0}".format(normalized))
    byte_count = envelope.get("byte_count")
    if byte_count is not None and byte_count != end - start:
        violations.append("artifact_byte_count_mismatch:{0}".format(normalized))
    envelope_owner = envelope.get("owner_reference")
    if envelope_owner is not None and envelope_owner != owner_ref:
        violations.append("artifact_envelope_owner_mismatch:{0}".format(normalized))
    return violations


def _validate_ref(graph: TraceGraph, ref: Any, *, owner_ref: str = "") -> List[str]:
    if not isinstance(ref, str) or not ref:
        return ["empty_or_nonstring_ref"]
    if ref.startswith("artifact:"):
        return _validate_artifact(graph, ref, owner_ref=owner_ref)
    resolved = graph.resolve(ref)
    if resolved is None or resolved != ref:
        return ["trace_ref_unresolved_or_noncanonical:{0}".format(ref)]
    return []


def _eligible_directed_hop(graph: TraceGraph, source: str, target: str) -> bool:
    return any(
        edge.get("eligible_for_attribution") is True
        and str(edge.get("relation") or "") not in TEMPORAL_RELATIONS
        for edge in graph.edge_context(source, target)
    )


def _validate_path(graph: TraceGraph, path: Any, *, label: str) -> List[str]:
    if not isinstance(path, list) or not path:
        return ["{0}_path_missing".format(label)]
    violations: List[str] = []
    for ref in path:
        violations.extend(_validate_ref(graph, ref))
    for source, target in zip(path, path[1:]):
        if not _eligible_directed_hop(graph, source, target):
            violations.append(
                "{0}_path_hop_not_grounded:{1}->{2}".format(label, source, target)
            )
    return violations


def _duplicate_full_identities(report: Mapping[str, Any]) -> List[str]:
    violations: List[str] = []
    for section in (
        "causal_candidates",
        "introduction_candidates",
        "step_judgments",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "confirmations",
    ):
        identities = [stable_json(item) for item in _items(report.get(section))]
        if len(identities) != len(set(identities)):
            violations.append("duplicate_full_identity:{0}".format(section))
    return violations


def _semantic_duplicate_identities(report: Mapping[str, Any]) -> List[str]:
    """Reject prose-only variants of the same scored causal assertion."""

    violations: List[str] = []

    def duplicates(section: str, identities: List[str]) -> None:
        if len(identities) != len(set(identities)):
            violations.append("duplicate_semantic_identity:{0}".format(section))

    for section in ("causal_candidates", "introduction_candidates"):
        identities = []
        for item in _items(report.get(section)):
            edge = item.get("edge") if isinstance(item.get("edge"), Mapping) else {}
            identities.append(
                stable_json(
                    {
                        "occurrence": item.get("semantic_occurrence_id"),
                        "edge": {
                            key: edge.get(key)
                            for key in (
                                "from_ref",
                                "to_ref",
                                "relation",
                                "eligible_for_attribution",
                            )
                        },
                    }
                )
            )
        duplicates(section, identities)

    judgment_identities = []
    for item in _items(report.get("step_judgments")):
        predecessors = []
        for predecessor in _items(item.get("predecessors")):
            upstream = predecessor.get("upstream_defect")
            predecessors.append(
                {
                    "ref": predecessor.get("ref"),
                    "relation": predecessor.get("relation"),
                    "recurse": predecessor.get("recurse"),
                    "upstream_defect_fingerprint": upstream.get("fingerprint")
                    if isinstance(upstream, Mapping)
                    else None,
                    "evidence_refs": sorted(predecessor.get("evidence_refs") or []),
                    "missing_evidence": sorted(predecessor.get("missing_evidence") or []),
                }
            )
        judgment_identities.append(
            stable_json(
                {
                    "occurrence": item.get("semantic_occurrence_id"),
                    "current_defect_status": item.get("current_defect_status"),
                    "candidate_introduction": item.get("candidate_introduction"),
                    "predecessors": sorted(predecessors, key=stable_json),
                    "missing_evidence": sorted(item.get("missing_evidence") or []),
                    "suggested_investigation": item.get("suggested_investigation"),
                }
            )
        )
    duplicates("step_judgments", judgment_identities)

    role_occurrences: Dict[str, List[str]] = {
        "roots": [
            str(item.get("semantic_occurrence_id") or "")
            for item in [
                *_items(report.get("confirmed_roots")),
                *_items(report.get("co_roots")),
            ]
        ],
        "conditions": [
            str(item.get("semantic_occurrence_id") or "")
            for item in _items(report.get("contributing_conditions"))
        ],
        "amplifiers": [
            str(item.get("semantic_occurrence_id") or "")
            for item in _items(report.get("amplifying_factors"))
        ],
    }
    for role, occurrences in role_occurrences.items():
        duplicates(role, occurrences)
    role_sets = {
        role: set(occurrences) for role, occurrences in role_occurrences.items()
    }
    for left, right in (("roots", "conditions"), ("roots", "amplifiers"), ("conditions", "amplifiers")):
        if role_sets[left].intersection(role_sets[right]):
            violations.append("semantic_identity_role_conflict:{0}:{1}".format(left, right))
    return violations


def _source_trace_violations(graph: TraceGraph) -> List[str]:
    violations: List[str] = []
    records = graph.raw_trace.get("records")
    if not isinstance(records, list):
        return ["source_trace_records_not_a_list"]
    record_ids = [
        str(item.get("record_id") or "")
        for item in records
        if isinstance(item, Mapping)
    ]
    if len(record_ids) != len(records) or any(not item for item in record_ids):
        violations.append("source_trace_record_invalid")
    if len(record_ids) != len(set(record_ids)):
        violations.append("source_trace_duplicate_record_id")
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for source_ref in record.get("source_refs") or []:
            if not isinstance(source_ref, str) or graph.resolve(source_ref) is None:
                violations.append(
                    "source_trace_unresolved_source_ref:{0}".format(source_ref)
                )

    edge_ids: List[str] = []
    for edge in graph.raw_trace.get("dataflow_edges") or []:
        if not isinstance(edge, Mapping):
            violations.append("source_trace_dataflow_edge_invalid")
            continue
        edge_id = str(edge.get("edge_id") or "")
        if edge_id:
            edge_ids.append(edge_id)
        metadata = edge.get("metadata") if isinstance(edge.get("metadata"), Mapping) else {}
        eligible = edge.get("eligible_for_attribution") is not False and metadata.get("eligible_for_attribution") is not False
        if eligible and (
            resolve_edge_endpoint(edge.get("from"), graph.aliases) is None
            or resolve_edge_endpoint(edge.get("to"), graph.aliases) is None
        ):
            violations.append("source_trace_unresolved_attribution_edge:{0}".format(edge_id))
    if len(edge_ids) != len(set(edge_ids)):
        violations.append("source_trace_duplicate_edge_id")

    raw_artifacts = graph.raw_trace.get("artifacts") or []
    if not isinstance(raw_artifacts, list):
        violations.append("source_trace_artifacts_not_a_list")
        raw_artifacts = []
    artifact_ids = [
        str(item.get("artifact_id") or "").removeprefix("artifact:")
        for item in raw_artifacts
        if isinstance(item, Mapping)
    ]
    if (
        len(artifact_ids) != len(raw_artifacts)
        or any(not item for item in artifact_ids)
        or len(artifact_ids) != len(set(artifact_ids))
    ):
        violations.append("source_trace_artifact_identity_invalid_or_duplicate")
    for artifact_id in artifact_ids:
        violations.extend(_validate_artifact(graph, artifact_id, owner_ref=""))
    return sorted(set(violations))


def _validate_candidate(
    graph: TraceGraph,
    candidate: Mapping[str, Any],
    anchors: Mapping[str, str],
    *,
    section: str,
) -> List[str]:
    violations: List[str] = []
    ref = str(candidate.get("ref") or "")
    violations.extend(_validate_ref(graph, ref))
    expected_anchor = anchors.get(ref)
    if candidate.get("semantic_anchor_id") != expected_anchor:
        violations.append("{0}_anchor_mismatch:{1}".format(section, ref))
    embedded = candidate.get("node")
    if not isinstance(embedded, Mapping) or ref not in graph.nodes:
        violations.append("{0}_node_snapshot_missing:{1}".format(section, ref))
    else:
        base_payload = _source_node_payload(graph, ref)
        source_node = graph.hydrate_node(ref)
        hydrated_payload = _node_payload(source_node)
        if dict(embedded) not in (base_payload, hydrated_payload):
            violations.append("{0}_node_snapshot_mismatch:{1}".format(section, ref))
        hydrated = embedded.get("data", {}).get("hydrated_artifacts") if isinstance(embedded.get("data"), Mapping) else None
        for artifact in _items(hydrated):
            artifact_id = str(artifact.get("artifact_id") or "")
            violations.extend(
                _validate_artifact(graph, artifact_id, owner_ref=ref, envelope=artifact)
            )
    for evidence_ref in candidate.get("evidence_refs") or []:
        violations.extend(_validate_ref(graph, evidence_ref, owner_ref=ref))
    edge = candidate.get("edge")
    if isinstance(edge, Mapping):
        source = str(edge.get("from_ref") or ref)
        target = str(edge.get("to_ref") or "")
        violations.extend(_validate_ref(graph, source))
        if target:
            violations.extend(_validate_ref(graph, target))
        for edge_evidence_ref in edge.get("evidence_refs") or []:
            violations.extend(_validate_ref(graph, edge_evidence_ref, owner_ref=source))
        if edge.get("eligible_for_attribution") is True:
            matching_edges = [
                source_edge
                for source_edge in graph.edge_context(source, target)
                if source_edge.get("eligible_for_attribution") is True
                and str(source_edge.get("relation") or "") not in TEMPORAL_RELATIONS
            ]
            if not target or not matching_edges:
                violations.append("{0}_edge_not_grounded:{1}->{2}".format(section, source, target))
    return violations


def _validate_confirmation(
    graph: TraceGraph,
    confirmation: Mapping[str, Any],
    anchors: Mapping[str, str],
    confirmation_by_identity: Mapping[str, Mapping[str, Any]],
) -> List[str]:
    ref = str(confirmation.get("candidate_ref") or "")
    violations = _validate_ref(graph, ref)
    if confirmation.get("semantic_anchor_id") != anchors.get(ref):
        violations.append("confirmation_anchor_mismatch:{0}".format(ref))
    path = confirmation.get("recursive_path")
    if path:
        violations.extend(_validate_path(graph, path, label="confirmation"))
    for evidence_ref in confirmation.get("evidence_refs") or []:
        violations.extend(_validate_ref(graph, evidence_ref, owner_ref=ref))
    mechanism = confirmation.get("factor_mechanism")
    if isinstance(mechanism, Mapping) and mechanism:
        violations.extend(_validate_ref(graph, mechanism.get("source_ref")))
        violations.extend(_validate_ref(graph, mechanism.get("target_ref")))
    for comparison in _items(confirmation.get("competitor_comparisons")):
        candidate_ref = str(comparison.get("candidate_ref") or "")
        violations.extend(_validate_ref(graph, candidate_ref))
        comparison_path = comparison.get("recursive_path")
        if comparison_path:
            violations.extend(
                _validate_path(graph, comparison_path, label="competitor_confirmation")
            )
        for evidence_ref in comparison.get("evidence_refs") or []:
            violations.extend(_validate_ref(graph, evidence_ref, owner_ref=candidate_ref))
        persisted_identity = str(comparison.get("confirmation_identity") or "")
        target = confirmation_by_identity.get(persisted_identity)
        expected_identity = confirmation_identity_for(
            hypothesis_id=str(comparison.get("hypothesis_id") or ""),
            hypothesis_semantic_hash=str(
                comparison.get("hypothesis_semantic_hash") or ""
            ),
            candidate_ref=candidate_ref,
            defect_fingerprint=str(comparison.get("defect_fingerprint") or ""),
            recursive_path=tuple(
                str(item) for item in comparison.get("recursive_path") or []
            ),
            seed_binding_identity=(
                str(target.get("seed_binding_identity") or "")
                if target is not None
                else str(comparison.get("seed_binding_identity") or "")
            ),
        )
        target_mismatch = target is not None and any(
            comparison.get(key) != target.get(key)
            for key in ("candidate_ref", "hypothesis_id", "hypothesis_semantic_hash", "defect_fingerprint", "recursive_path")
        )
        if persisted_identity != expected_identity or target_mismatch:
            violations.append(
                "competitor_confirmation_identity_mismatch:{0}".format(candidate_ref)
            )
    return violations


def _validate_factored_item(
    graph: TraceGraph,
    item: Mapping[str, Any],
    anchors: Mapping[str, str],
    *,
    role: str,
) -> List[str]:
    ref = str(item.get("node_ref") or "")
    violations = _validate_ref(graph, ref)
    if item.get("semantic_anchor_id") != anchors.get(ref):
        violations.append("{0}_anchor_mismatch:{1}".format(role, ref))
    path = item.get("recursive_path")
    violations.extend(_validate_path(graph, path, label=role))
    evidence = item.get("evidence_refs")
    if not isinstance(evidence, list) or not evidence:
        violations.append("{0}_evidence_missing:{1}".format(role, ref))
    else:
        for evidence_ref in evidence:
            violations.extend(_validate_ref(graph, evidence_ref, owner_ref=ref))
    if role in {"condition", "amplifier"}:
        mechanism = item.get("mechanism")
        if not isinstance(mechanism, Mapping) or not mechanism:
            violations.append("{0}_mechanism_missing:{1}".format(role, ref))
        else:
            source_ref = str(mechanism.get("source_ref") or "")
            target_ref = str(mechanism.get("target_ref") or "")
            expected_type = "enabling_condition" if role == "condition" else "amplification"
            if source_ref != ref:
                violations.append("{0}_mechanism_source_mismatch:{1}".format(role, ref))
            violations.extend(_validate_ref(graph, source_ref))
            violations.extend(_validate_ref(graph, target_ref))
            if not isinstance(path, list) or not path or target_ref != path[-1]:
                violations.append("{0}_mechanism_target_mismatch:{1}".format(role, ref))
            if mechanism.get("mechanism_type") != expected_type or not mechanism.get("effect"):
                violations.append("{0}_mechanism_semantics_invalid:{1}".format(role, ref))
    return violations


def _trace_backed_safety_violations(
    report: Mapping[str, Any],
    labels: Mapping[str, Any],
    graph: TraceGraph,
) -> Tuple[
    List[str],
    RecursiveAttributionReport,
    Dict[str, str],
    Dict[str, str],
]:
    violations: List[str] = []
    try:
        parsed = RecursiveAttributionReport.from_dict(dict(report))
    except (TypeError, ValueError) as exc:
        raise EvaluationSafetyError("strict_report_invalid:{0}".format(exc))
    try:
        validate_recursive_report_against_graph(
            graph,
            parsed,
            label="evaluator report",
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationSafetyError(
            "strict_report_action_reconciliation:{0}".format(exc)
        ) from exc
    if _without_projection_fields(report) != _without_projection_fields(parsed.to_dict()):
        violations.append("strict_report_canonical_schema_mismatch")
    if parsed.case_id != graph.case_id or parsed.case_id != labels["case_id"]:
        violations.append("case_id_mismatch")
    violations.extend(_source_trace_violations(graph))
    if report.get("analysis_outcome") != parsed.analysis_outcome:
        violations.append("analysis_outcome_state_machine_mismatch")
    if parsed.analysis_outcome in {"inconclusive", "partial"}:
        allowed = set(labels["allowed_unresolved_outcomes"])
        if parsed.analysis_outcome == "partial" and "partial_root_found" in allowed:
            allowed.add("partial")
        if parsed.analysis_outcome not in allowed:
            violations.append(
                "disallowed_unresolved_outcome:{0}".format(
                    parsed.analysis_outcome
                )
            )

    metadata = _mapping(report.get("metadata"), "report.metadata")
    if metadata.get("fabricated_refs"):
        violations.append("fabricated_refs_present")
    anchors = semantic_anchor_index(graph.case_id, graph)
    occurrences = semantic_occurrence_index(graph.case_id, graph)
    label_roles: Dict[str, str] = {}
    for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
        for label in labels[role]:
            anchor = str(label["semantic_anchor_id"])
            occurrence = str(label["semantic_occurrence_id"])
            explicit_ref = label.get("node_ref")
            if explicit_ref is None:
                matching_refs = [
                    ref
                    for ref in graph.nodes
                    if anchors.get(ref) == anchor and occurrences.get(ref) == occurrence
                ]
                if len(matching_refs) != 1:
                    violations.append(
                        "label_identity_{0}:{1}:{2}".format(
                            "unresolved" if not matching_refs else "ambiguous",
                            role,
                            occurrence,
                        )
                    )
                    continue
                ref = matching_refs[0]
            else:
                ref = str(explicit_ref)
                violations.extend(_validate_ref(graph, ref))
            if anchors.get(ref) != anchor:
                violations.append("label_anchor_mismatch:{0}:{1}".format(role, ref))
            if occurrences.get(ref) != occurrence:
                violations.append("label_occurrence_mismatch:{0}:{1}".format(role, ref))
            prior_role = label_roles.get(occurrence)
            if prior_role is not None and prior_role != role:
                violations.append(
                    "label_occurrence_identity_role_conflict:{0}:{1}:{2}".format(
                        occurrence, prior_role, role
                    )
                )
            label_roles[occurrence] = role
    reported_index = metadata.get("semantic_anchor_index")
    if not isinstance(reported_index, Mapping) or dict(reported_index) != anchors:
        violations.append("semantic_anchor_index_mismatch")
    collisions_by_anchor: Dict[str, List[str]] = {}
    for ref, anchor in anchors.items():
        collisions_by_anchor.setdefault(anchor, []).append(ref)
    expected_collisions = [
        {"semantic_anchor_id": anchor, "node_refs": sorted(refs)}
        for anchor, refs in sorted(collisions_by_anchor.items())
        if len(refs) > 1
    ]
    if metadata.get("semantic_anchor_schema_version") != "semantic-anchor/v2":
        violations.append("semantic_anchor_schema_version_mismatch")
    if metadata.get("semantic_anchor_collisions") != expected_collisions:
        violations.append("semantic_anchor_collision_report_mismatch")
    collisions_by_occurrence: Dict[str, List[str]] = {}
    for ref, occurrence in occurrences.items():
        collisions_by_occurrence.setdefault(occurrence, []).append(ref)
    expected_occurrence_collisions = [
        {"semantic_occurrence_id": occurrence, "node_refs": sorted(refs)}
        for occurrence, refs in sorted(collisions_by_occurrence.items())
        if len(refs) > 1
    ]
    if metadata.get("semantic_occurrence_schema_version") != "semantic-occurrence/v1":
        violations.append("semantic_occurrence_schema_version_mismatch")
    reported_occurrences = metadata.get("semantic_occurrence_index")
    if not isinstance(reported_occurrences, Mapping) or dict(reported_occurrences) != occurrences:
        violations.append("semantic_occurrence_index_mismatch")
    if metadata.get("semantic_occurrence_collisions") != expected_occurrence_collisions:
        violations.append("semantic_occurrence_collision_report_mismatch")

    for section, ref_key in (
        ("causal_candidates", "ref"),
        ("introduction_candidates", "ref"),
        ("confirmed_roots", "node_ref"),
        ("co_roots", "node_ref"),
        ("contributing_conditions", "node_ref"),
        ("amplifying_factors", "node_ref"),
        ("rejected_candidates", "node_ref"),
        ("root_causes", "node_ref"),
        ("confirmations", "candidate_ref"),
        ("step_judgments", "current_node_ref"),
        ("causal_relations", "ref"),
    ):
        for item in _items(report.get(section)):
            ref = str(item.get(ref_key) or "")
            if item.get("semantic_occurrence_id") != occurrences.get(ref):
                violations.append("{0}_occurrence_mismatch:{1}".format(section, ref))
            embedded = item.get("confirmation")
            if isinstance(embedded, Mapping) and embedded.get("semantic_occurrence_id") != occurrences.get(ref):
                violations.append(
                    "{0}_confirmation_occurrence_mismatch:{1}".format(section, ref)
                )
    violations.extend(_duplicate_full_identities(report))
    violations.extend(_semantic_duplicate_identities(report))

    for start_ref in report.get("start_refs") or []:
        violations.extend(_validate_ref(graph, start_ref))

    for section in ("causal_candidates", "introduction_candidates"):
        for candidate in _items(report.get(section)):
            violations.extend(
                _validate_candidate(graph, candidate, anchors, section=section)
            )

    for judgment in _items(report.get("step_judgments")):
        current = str(judgment.get("current_node_ref") or "")
        violations.extend(_validate_ref(graph, current))
        if judgment.get("semantic_anchor_id") != anchors.get(current):
            violations.append("judgment_anchor_mismatch:{0}".format(current))
        for predecessor in _items(judgment.get("predecessors")):
            predecessor_ref = str(predecessor.get("ref") or "")
            violations.extend(_validate_ref(graph, predecessor_ref))
            for evidence_ref in predecessor.get("evidence_refs") or []:
                violations.extend(_validate_ref(graph, evidence_ref, owner_ref=predecessor_ref))

    for relation in _items(report.get("causal_relations")):
        relation_ref = str(relation.get("ref") or "")
        violations.extend(_validate_ref(graph, relation_ref))
        if relation.get("semantic_anchor_id") != anchors.get(relation_ref):
            violations.append("causal_relation_anchor_mismatch:{0}".format(relation_ref))
        for evidence_ref in relation.get("evidence_refs") or []:
            violations.extend(_validate_ref(graph, evidence_ref, owner_ref=relation_ref))

    for section in ("hypotheses", "unresolved_hypotheses"):
        for hypothesis in _items(report.get(section)):
            candidate_ref = str(hypothesis.get("candidate_root_ref") or "")
            violations.extend(_validate_ref(graph, candidate_ref))
            for evidence_key in ("supporting_evidence", "opposing_evidence"):
                for evidence in _items(hypothesis.get(evidence_key)):
                    violations.extend(
                        _validate_ref(graph, evidence.get("ref"), owner_ref=candidate_ref)
                    )

    confirmations = _items(report.get("confirmations"))
    confirmation_by_identity = {
        str(item.get("confirmation_identity") or ""): item for item in confirmations
    }
    for confirmation in confirmations:
        violations.extend(
            _validate_confirmation(graph, confirmation, anchors, confirmation_by_identity)
        )

    roots = [*_items(report.get("confirmed_roots")), *_items(report.get("co_roots"))]
    for root in roots:
        violations.extend(_validate_factored_item(graph, root, anchors, role="root"))
    for factor in _items(report.get("contributing_conditions")):
        violations.extend(_validate_factored_item(graph, factor, anchors, role="condition"))
    for factor in _items(report.get("amplifying_factors")):
        violations.extend(_validate_factored_item(graph, factor, anchors, role="amplifier"))
    for rejected in _items(report.get("rejected_candidates")):
        ref = str(rejected.get("node_ref") or "")
        violations.extend(_validate_ref(graph, ref))
        violations.extend(_validate_path(graph, rejected.get("recursive_path"), label="rejected"))
        for evidence_ref in rejected.get("evidence_refs") or []:
            violations.extend(_validate_ref(graph, evidence_ref, owner_ref=ref))

    unresolved = _unresolved_refs(report)
    for root in roots:
        ref = str(root.get("node_ref") or "")
        cited = [*(root.get("recursive_path") or []), *(root.get("evidence_refs") or [])]
        if ref in unresolved or any(item in unresolved for item in cited):
            violations.append("unresolved_confirmed_fact:{0}".format(ref))
        for branch in _items(metadata.get("unresolved_branches")):
            if branch.get("node_ref") == ref:
                violations.append("unresolved_branch_promoted_to_root:{0}".format(ref))

    forbidden = set(_label_occurrences(labels, "forbidden_roots"))
    for root in roots:
        if root.get("semantic_occurrence_id") in forbidden:
            violations.append("forbidden_root_confirmed:{0}".format(root.get("node_ref")))

    logical_calls = metadata_int(report, "logical_judge_call_count")
    reused_calls = metadata_int(report, "checkpoint_reused_judgment_count")
    if reused_calls > logical_calls:
        violations.append("checkpoint_reuse_exceeds_logical_calls")
    return sorted(set(violations)), parsed, anchors, occurrences


def compare_report(
    report: Mapping[str, Any],
    labels: Mapping[str, Any],
    legacy_report: Optional[Mapping[str, Any]] = None,
    *,
    graph: Optional[TraceGraph],
) -> JsonDict:
    if graph is None:
        raise EvaluationSafetyError("acceptance_requires_source_trace")
    report = dict(_mapping(report, "report"))
    labels = validate_labels(labels)
    _validate_report_shape(report, labels)
    violations, parsed, _, _ = _trace_backed_safety_violations(report, labels, graph)
    if violations:
        raise EvaluationSafetyError("; ".join(violations))

    confirmed = [*_items(report["confirmed_roots"]), *_items(report["co_roots"])]
    predicted = _occurrences(confirmed)
    introduced = _occurrences(_items(report.get("introduction_candidates")))
    expected = _label_occurrences(labels, "roots")
    predicted_set = set(predicted)
    introduced_set = set(introduced)
    expected_set = set(expected)
    root_recall = _safe_rate(
        len(predicted_set & expected_set), len(expected_set), empty=1.0 if not predicted else 0.0
    )
    root_precision = _safe_rate(
        len(predicted_set & expected_set), len(predicted_set), empty=1.0 if not expected else 0.0
    )
    introduction_recall = _safe_rate(
        len(introduced_set & expected_set), len(expected_set), empty=1.0 if not introduced else 0.0
    )
    introduction_precision = _safe_rate(
        len(introduced_set & expected_set), len(introduced_set), empty=1.0 if not expected else 0.0
    )
    top1_match = None if not expected else bool(predicted and predicted[0] in expected_set)

    predicted_semantics = set(_anchors(confirmed))
    introduced_semantics = set(_anchors(_items(report.get("introduction_candidates"))))
    expected_semantics = set(_label_anchors(labels, "roots"))
    semantic_root_recall = _safe_rate(
        len(predicted_semantics & expected_semantics),
        len(expected_semantics),
        empty=1.0 if not predicted_semantics else 0.0,
    )
    semantic_root_precision = _safe_rate(
        len(predicted_semantics & expected_semantics),
        len(predicted_semantics),
        empty=1.0 if not expected_semantics else 0.0,
    )
    semantic_introduction_recall = _safe_rate(
        len(introduced_semantics & expected_semantics),
        len(expected_semantics),
        empty=1.0 if not introduced_semantics else 0.0,
    )
    semantic_introduction_precision = _safe_rate(
        len(introduced_semantics & expected_semantics),
        len(introduced_semantics),
        empty=1.0 if not expected_semantics else 0.0,
    )
    negative_control_correct = bool(
        not expected and not predicted and parsed.analysis_outcome == "no_defect"
    )

    current_requests = _request_count(report)
    legacy_requests = _request_count(legacy_report)
    request_reduction = None
    request_ratio = None
    request_delta = None
    if current_requests is not None and legacy_requests is not None and legacy_requests > 0:
        request_delta = legacy_requests - current_requests
        request_reduction = round(request_delta / legacy_requests, 6)
        request_ratio = round(current_requests / legacy_requests, 6)

    path_lengths = [
        len(item.get("recursive_path") or []) for item in confirmed
    ]
    mean_path_length = round(sum(path_lengths) / len(path_lengths), 6) if path_lengths else 0.0

    predicted_conditions = set(_occurrences(_items(report["contributing_conditions"])))
    predicted_amplifiers = set(_occurrences(_items(report["amplifying_factors"])))
    expected_conditions = set(_label_occurrences(labels, "conditions"))
    expected_amplifiers = set(_label_occurrences(labels, "amplifiers"))
    predicted_factors = {
        *(('condition', item) for item in predicted_conditions),
        *(('amplifier', item) for item in predicted_amplifiers),
    }
    expected_factors = {
        *(('condition', item) for item in expected_conditions),
        *(('amplifier', item) for item in expected_amplifiers),
    }
    factor_precision = _safe_rate(
        len(predicted_factors & expected_factors),
        len(predicted_factors),
        empty=1.0 if not expected_factors else 0.0,
    )

    judgments = _items(report["step_judgments"])
    confirmations = _items(report["confirmations"])
    semantic_decisions = len(judgments) + len(confirmations)
    unknown_count = sum(
        1 for item in judgments if item.get("current_defect_status") == "unknown"
    ) + sum(1 for item in confirmations if item.get("status") == "unknown")
    rejected_count = sum(1 for item in confirmations if item.get("status") == "rejected")

    investigations = _items(report["investigation_journal"])
    completed_investigations = [
        item
        for item in investigations
        if isinstance(item.get("rejudge_linkage"), Mapping)
        and item["rejudge_linkage"].get("status") == "completed"
    ]
    yielded_investigations = [
        item
        for item in completed_investigations
        if isinstance(item.get("result"), Mapping)
        and item["result"].get("status") == "success"
        and item.get("context_before_hash")
        and item.get("context_after_hash")
        and item.get("context_before_hash") != item.get("context_after_hash")
    ]

    logical_calls = metadata_int(report, "logical_judge_call_count")
    reused_calls = metadata_int(report, "checkpoint_reused_judgment_count")
    expected_role_pairs = {
        *(('root', item) for item in expected_set),
        *expected_factors,
    }
    predicted_role_pairs = {
        *(('root', item) for item in predicted_set),
        *predicted_factors,
    }
    disagreements = {
        "missing_expected_roots": sorted(expected_set - predicted_set),
        "unexpected_predicted_roots": sorted(predicted_set - expected_set),
        "missing_expected_conditions": sorted(expected_conditions - predicted_conditions),
        "unexpected_predicted_conditions": sorted(predicted_conditions - expected_conditions),
        "missing_expected_amplifiers": sorted(expected_amplifiers - predicted_amplifiers),
        "unexpected_predicted_amplifiers": sorted(predicted_amplifiers - expected_amplifiers),
    }
    disagreement_denominator = len(expected_role_pairs | predicted_role_pairs)
    disagreement_rate = _safe_rate(
        len(expected_role_pairs ^ predicted_role_pairs),
        disagreement_denominator,
        empty=0.0,
    )
    unresolved_hypotheses = len(report["unresolved_hypotheses"])
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "case_id": str(report["case_id"]),
        "metrics": {
            "confirmed_root_recall": root_recall,
            "confirmed_root_precision": root_precision,
            "semantic_confirmed_root_recall": semantic_root_recall,
            "semantic_confirmed_root_precision": semantic_root_precision,
            "introduction_candidate_recall": introduction_recall,
            "introduction_candidate_precision": introduction_precision,
            "semantic_introduction_candidate_recall": semantic_introduction_recall,
            "semantic_introduction_candidate_precision": semantic_introduction_precision,
            "top1_match": top1_match,
            "negative_control_correct": negative_control_correct,
            "judge_request_reduction": request_reduction,
            "request_ratio": request_ratio,
            "mean_causal_path_length": mean_path_length,
            "factor_role_precision": factor_precision,
            "unknown_rate": _safe_rate(unknown_count, semantic_decisions),
            "confirmation_rejection_rate": _safe_rate(rejected_count, len(confirmations)),
            "investigation_yield": _safe_rate(
                len(yielded_investigations), len(completed_investigations)
            ),
            "checkpoint_reuse_rate": _safe_rate(reused_calls, logical_calls),
            "human_llm_disagreement_rate": disagreement_rate,
        },
        "counts": {
            "expected_root_count": len(expected_set),
            "predicted_root_count": len(predicted_set),
            "introduction_candidate_count": len(introduced_set),
            "judge_request_count": current_requests,
            "legacy_judge_request_count": legacy_requests,
            "request_delta": request_delta,
            "causal_path_count": len(path_lengths),
            "unknown_decision_count": unknown_count,
            "semantic_decision_count": semantic_decisions,
            "confirmation_count": len(confirmations),
            "confirmation_rejection_count": rejected_count,
            "investigation_count": len(investigations),
            "investigation_yield_count": len(yielded_investigations),
            "checkpoint_reused_judgment_count": reused_calls,
            "unresolved_hypothesis_count": unresolved_hypotheses,
        },
        "disagreements": disagreements,
        "safety": {
            "passed": True,
            "fabricated_ref_count": 0,
            "unresolved_confirmed_fact_count": 0,
            "duplicate_identity_count": 0,
        },
    }


def metadata_int(report: Mapping[str, Any], key: str) -> int:
    metadata = report.get("metadata")
    value = metadata.get(key) if isinstance(metadata, Mapping) else 0
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--labels", required=True)
    parser.add_argument("--legacy-report", default="")
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def _read_json(path: str) -> JsonDict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict(_mapping(value, path))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        graph = TraceGraph.from_file(Path(args.trace))
        result = compare_report(
            _read_json(args.report),
            _read_json(args.labels),
            _read_json(args.legacy_report) if args.legacy_report else None,
            graph=graph,
        )
    except (EvaluationSchemaError, EvaluationSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("recursive attribution evaluation failed: {0}".format(exc), file=sys.stderr)
        return 2
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
