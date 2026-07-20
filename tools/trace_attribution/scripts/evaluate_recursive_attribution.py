#!/usr/bin/env python3
"""Strict offline acceptance metrics for recursive causal attribution reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


JsonDict = Dict[str, Any]
LABEL_SCHEMA_VERSION = "recursive-attribution-labels/v1"
COMPARISON_SCHEMA_VERSION = "recursive-attribution-comparison/v1"
REPORT_SCHEMA_VERSION = "recursive-attribution-report/v2"
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
LABEL_ENTRY_KEYS = frozenset({"node_ref", "semantic_anchor_id"})
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
        anchors: Set[str] = set()
        refs: Set[str] = set()
        for index, raw in enumerate(entries):
            item = dict(_mapping(raw, "labels.{0}[{1}]".format(role, index)))
            _exact_keys(item, LABEL_ENTRY_KEYS, "labels.{0}[{1}]".format(role, index))
            node_ref = item["node_ref"]
            anchor = item["semantic_anchor_id"]
            if not isinstance(node_ref, str) or not node_ref.startswith("record:"):
                raise EvaluationSchemaError("label node_ref must be a record ref")
            if not isinstance(anchor, str) or not anchor.startswith("semantic_anchor:v1:"):
                raise EvaluationSchemaError("label semantic_anchor_id must be versioned")
            if node_ref in refs or anchor in anchors:
                raise EvaluationSchemaError("duplicate label identity in {0}".format(role))
            refs.add(node_ref)
            anchors.add(anchor)
    outcomes = _list(labels["allowed_unresolved_outcomes"], "allowed_unresolved_outcomes")
    if any(not isinstance(item, str) for item in outcomes):
        raise EvaluationSchemaError("allowed_unresolved_outcomes must contain strings")
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


def _label_anchors(labels: Mapping[str, Any], role: str) -> List[str]:
    return [str(item["semantic_anchor_id"]) for item in labels[role]]


def _safe_rate(numerator: int, denominator: int, *, empty: float = 0.0) -> float:
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


def _known_fact_refs(report: Mapping[str, Any]) -> Set[str]:
    refs: Set[str] = set()

    def collect_grounded(value: Any) -> None:
        if isinstance(value, Mapping):
            artifact_id = value.get("artifact_id")
            artifact_hash = value.get("content_hash") or value.get("hash")
            if (
                isinstance(artifact_id, str)
                and artifact_id.strip()
                and isinstance(artifact_hash, str)
                and artifact_hash.startswith("sha256:")
                and value.get("missing") is not True
                and value.get("truncated") is not True
            ):
                normalized = artifact_id.strip().removeprefix("artifact:")
                refs.add(normalized)
                refs.add("artifact:" + normalized)
            resolved_ref = value.get("resolved_ref")
            if (
                isinstance(resolved_ref, str)
                and resolved_ref
                and value.get("resolution_status") == "resolved"
                and value.get("provenance_class")
                in {"recorded", "reconstructed", "inferred"}
            ):
                refs.add(resolved_ref)
            edge_id = value.get("edge_id")
            if (
                isinstance(edge_id, str)
                and edge_id
                and value.get("eligible_for_attribution") is True
            ):
                refs.add(edge_id)
            for child in value.values():
                collect_grounded(child)
        elif isinstance(value, list):
            for child in value:
                collect_grounded(child)

    refs.update(str(item) for item in report.get("start_refs") or [] if isinstance(item, str))
    refs.update(str(item) for item in report.get("visited_order") or [] if isinstance(item, str))
    for candidate in _items(report.get("causal_candidates")):
        collect_grounded(candidate)
        for value in (candidate.get("ref"), candidate.get("node_ref")):
            if isinstance(value, str) and value:
                refs.add(value)
        node = candidate.get("node")
        if isinstance(node, Mapping) and isinstance(node.get("ref"), str):
            refs.add(str(node["ref"]))
    for judgment in _items(report.get("step_judgments")):
        value = judgment.get("current_node_ref")
        if isinstance(value, str) and value:
            refs.add(value)
    return refs


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
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise EvaluationSchemaError("recursive report schema_version must be {0}".format(REPORT_SCHEMA_VERSION))
    if report.get("case_id") != labels["case_id"]:
        raise EvaluationSchemaError("report and labels case_id differ")
    for key in (
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "confirmations",
        "step_judgments",
        "unresolved_hypotheses",
        "unresolved_refs",
        "investigation_journal",
    ):
        _list(report.get(key), "report.{0}".format(key))
    _mapping(report.get("metadata"), "report.metadata")


def _safety_violations(report: Mapping[str, Any], labels: Mapping[str, Any]) -> List[str]:
    violations: List[str] = []
    metadata = _mapping(report.get("metadata"), "report.metadata")
    fabricated = metadata.get("fabricated_refs") or []
    if fabricated:
        violations.append("fabricated_refs_present")
    if metadata.get("semantic_anchor_collisions"):
        violations.append("semantic_anchor_collisions_present")
    roots = [*_items(report.get("confirmed_roots")), *_items(report.get("co_roots"))]
    known_refs = _known_fact_refs(report)
    unresolved = _unresolved_refs(report)
    confirmation_items = _items(report.get("confirmations"))
    confirmation_identities = [
        str(item.get("confirmation_identity") or "") for item in confirmation_items
    ]
    if (
        any(not item.startswith("confirmation:") for item in confirmation_identities)
        or len(confirmation_identities) != len(set(confirmation_identities))
    ):
        violations.append("confirmation_identity_invalid_or_duplicate")
    confirmations = {
        str(item.get("confirmation_identity") or ""): item
        for item in confirmation_items
        if item.get("confirmation_identity")
    }
    anchors: Dict[str, str] = {}
    for root in roots:
        ref = str(root.get("node_ref") or "")
        anchor = str(root.get("semantic_anchor_id") or "")
        if not anchor.startswith("semantic_anchor:v1:"):
            violations.append("root_missing_semantic_anchor:{0}".format(ref))
        elif anchor in anchors and anchors[anchor] != ref:
            violations.append("duplicate_root_semantic_identity:{0}".format(anchor))
        else:
            anchors[anchor] = ref
        if not ref or ref not in known_refs:
            violations.append("root_ref_unresolved:{0}".format(ref))
        path = root.get("recursive_path")
        evidence = root.get("evidence_refs")
        if not isinstance(path, list) or not path:
            violations.append("root_path_missing:{0}".format(ref))
            path = []
        if not isinstance(evidence, list) or not evidence:
            violations.append("root_evidence_missing:{0}".format(ref))
            evidence = []
        for cited in [*path, *evidence]:
            if not isinstance(cited, str) or cited not in known_refs:
                violations.append("root_citation_unresolved:{0}:{1}".format(ref, cited))
            if cited in unresolved:
                violations.append("root_citation_marked_unresolved:{0}:{1}".format(ref, cited))
        embedded = root.get("confirmation")
        if not isinstance(embedded, Mapping):
            violations.append("root_confirmation_missing:{0}".format(ref))
            continue
        identity = str(embedded.get("confirmation_identity") or "")
        canonical = confirmations.get(identity)
        if (
            not identity
            or canonical is None
            or dict(canonical) != dict(embedded)
            or embedded.get("status") != "confirmed"
            or embedded.get("candidate_ref") != ref
            or embedded.get("recursive_path") != path
            or embedded.get("evidence_refs") != evidence
            or root.get("confirmation_status") != "confirmed"
        ):
            violations.append("root_confirmation_unresolved:{0}".format(ref))
        if ref in unresolved:
            violations.append("confirmed_root_marked_unresolved:{0}".format(ref))
        for branch in _items(metadata.get("unresolved_branches")):
            if branch.get("node_ref") == ref and any(
                token in str(branch.get("reason") or "")
                for token in ("budget", "limit", "provider", "unknown", "unresolved")
            ):
                violations.append("budget_or_unknown_promoted_to_root:{0}".format(ref))

    forbidden = set(_label_anchors(labels, "forbidden_roots"))
    for anchor in anchors:
        if anchor in forbidden:
            violations.append("forbidden_root_confirmed:{0}".format(anchor))
    return sorted(set(violations))


def compare_report(
    report: Mapping[str, Any],
    labels: Mapping[str, Any],
    legacy_report: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    report = dict(_mapping(report, "report"))
    labels = validate_labels(labels)
    _validate_report_shape(report, labels)
    violations = _safety_violations(report, labels)
    if violations:
        raise EvaluationSafetyError("; ".join(violations))

    confirmed = [*_items(report["confirmed_roots"]), *_items(report["co_roots"])]
    predicted = _anchors(confirmed)
    if not predicted:
        predicted = _anchors(_items(report.get("introduction_candidates")))
    expected = _label_anchors(labels, "roots")
    predicted_set = set(predicted)
    expected_set = set(expected)
    candidate_recall = (
        _safe_rate(len(predicted_set & expected_set), len(expected_set), empty=1.0 if not predicted else 0.0)
    )
    top1_match = bool(
        (not expected and not predicted) or (predicted and predicted[0] in expected_set)
    )

    current_requests = _request_count(report)
    legacy_requests = _request_count(legacy_report)
    request_reduction = None
    if current_requests is not None and legacy_requests is not None and legacy_requests > 0:
        request_reduction = round((legacy_requests - current_requests) / legacy_requests, 6)

    path_lengths = [
        len(item.get("recursive_path") or []) for item in confirmed
    ]
    mean_path_length = round(sum(path_lengths) / len(path_lengths), 6) if path_lengths else 0.0

    predicted_conditions = set(_anchors(_items(report["contributing_conditions"])))
    predicted_amplifiers = set(_anchors(_items(report["amplifying_factors"])))
    expected_conditions = set(_label_anchors(labels, "conditions"))
    expected_amplifiers = set(_label_anchors(labels, "amplifiers"))
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
            "candidate_recall": candidate_recall,
            "top1_match": top1_match,
            "judge_request_reduction": request_reduction,
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
            "judge_request_count": current_requests,
            "legacy_judge_request_count": legacy_requests,
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
        result = compare_report(
            _read_json(args.report),
            _read_json(args.labels),
            _read_json(args.legacy_report) if args.legacy_report else None,
        )
    except (EvaluationSchemaError, EvaluationSafetyError, OSError, json.JSONDecodeError) as exc:
        print("recursive attribution evaluation failed: {0}".format(exc), file=sys.stderr)
        return 2
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
