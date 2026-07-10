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
                },
            }
        )
    return enriched


def normalize_refs(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


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
