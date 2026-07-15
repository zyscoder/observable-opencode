from __future__ import annotations

import copy
import re
from typing import Any, Dict, List


JsonDict = Dict[str, Any]


def inject_quality_gap_records(trace: JsonDict, review: JsonDict) -> JsonDict:
    enriched = copy.deepcopy(trace)
    records = enriched.setdefault("records", [])
    if not isinstance(records, list):
        enriched["records"] = records = []

    quality = review.get("quality_review") if isinstance(review.get("quality_review"), dict) else {}
    gaps = quality.get("quality_gaps") if isinstance(quality.get("quality_gaps"), list) else []
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        dimension = str(gap.get("dimension") or "unknown")
        record_id = f"quality_gap_{slugify(dimension)}"
        if any(isinstance(record, dict) and record.get("record_id") == record_id for record in records):
            continue
        records.append(
            {
                "record_id": record_id,
                "event_type": "case.quality_gap",
                "component": "evaluation",
                "status": "warning",
                "source_refs": normalize_refs(gap.get("gap_context_refs")) or normalize_refs(gap.get("record_refs")),
                "data": {
                    "case_id": review.get("case_id") or enriched.get("manifest", {}).get("case_id"),
                    "dimension": dimension,
                    "gap_kind": "quality_dimension_under_target",
                    "score": gap.get("score"),
                    "max_score": gap.get("max_score"),
                    "score_ratio": score_ratio(gap.get("score"), gap.get("max_score")),
                    "missing_evidence": gap.get("missing_evidence") if isinstance(gap.get("missing_evidence"), list) else [],
                    "total_score": quality.get("total_score"),
                    "target_score": quality.get("target_score"),
                    "minimum_acceptable_score": quality.get("minimum_acceptable_score"),
                    "quality_status": quality.get("status"),
                    "attribution_objective": quality.get("attribution_objective"),
                    "attribution_domain": "task_quality",
                },
            }
        )
    missing_semantics = review.get("missing_semantics") if isinstance(review.get("missing_semantics"), list) else []
    evidence_by_name = evidence_refs_by_required_name(review.get("evidence_found"))
    for name in missing_semantics:
        semantic_name = str(name or "").strip()
        if not semantic_name:
            continue
        record_id = f"missing_semantic_{slugify(semantic_name)}"
        if any(isinstance(record, dict) and record.get("record_id") == record_id for record in records):
            continue
        records.append(
            {
                "record_id": record_id,
                "event_type": "case.missing_semantic",
                "component": "evaluation",
                "status": "warning",
                "source_refs": evidence_by_name.get(semantic_name, []),
                "data": {
                    "case_id": review.get("case_id") or enriched.get("manifest", {}).get("case_id"),
                    "semantic_name": semantic_name,
                    "gap_kind": "required_trace_semantic_missing",
                    "reason": f"Stress review did not find required trace semantic: {semantic_name}.",
                    "attribution_domain": "trace_health",
                },
            }
        )
    observed_defects = review.get("observed_defects") if isinstance(review.get("observed_defects"), list) else []
    for observed_defect in observed_defects:
        if not isinstance(observed_defect, dict):
            continue
        inject_observed_defect_record(enriched, records, review, observed_defect)
    return enriched


def inject_observed_defect_record(
    enriched: JsonDict,
    records: List[Any],
    review: JsonDict,
    observed_defect: JsonDict,
) -> None:
    component = str(observed_defect.get("component") or "").strip()
    failure_type = str(observed_defect.get("failure_type") or "").strip()
    if not component or not failure_type:
        return
    record_id = f"observed_defect_{slugify(component)}_{slugify(failure_type)}"
    if any(isinstance(record, dict) and record.get("record_id") == record_id for record in records):
        return
    all_refs = normalize_refs(observed_defect.get("record_refs")) or review_evidence_refs(review)
    included_refs = all_refs[:24]
    records.append(
        {
            "record_id": record_id,
            "event_type": "case.observed_defect",
            "component": "evaluation",
            "status": "warning",
            "source_refs": included_refs,
            "data": {
                "case_id": review.get("case_id") or enriched.get("manifest", {}).get("case_id"),
                "component": component,
                "failure_type": failure_type,
                "defect_type": failure_type,
                "description": observed_defect.get("description"),
                "trace_sufficiency": review.get("trace_sufficiency"),
                "case_effectiveness": review.get("case_effectiveness"),
                "can_offline_module_identify_root_cause": review.get("can_offline_module_identify_root_cause"),
                "source_ref_count_total": len(all_refs),
                "source_ref_count_included": len(included_refs),
                "source_refs_truncated": len(included_refs) < len(all_refs),
                "offline_only": True,
                "attribution_domain": "task_quality",
                "reason": "Stress review explicitly declares an observed execution defect with evidence refs.",
            },
        }
    )


def review_evidence_refs(review: JsonDict) -> List[str]:
    refs: List[str] = []
    for field in ("mechanism_evidence_found", "evidence_found"):
        value = review.get(field)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip()
            if status and status not in ("found", "partial"):
                continue
            refs.extend(normalize_refs(item.get("record_refs")))
    return dedupe(refs)


def evidence_refs_by_required_name(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, list):
        return {}
    output: Dict[str, List[str]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        required = str(item.get("required") or "").strip()
        if not required:
            continue
        output[required] = normalize_refs(item.get("record_refs"))
    return output


def normalize_refs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def dedupe(values: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "unknown"


def score_ratio(score: Any, max_score: Any) -> float:
    try:
        denominator = float(max_score)
        if denominator <= 0:
            return 0.0
        return round(float(score) / denominator, 4)
    except (TypeError, ValueError):
        return 0.0
