from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, List, Optional

from .evaluation_facts import (
    ObservedDefectSeed,
    has_structured_failure_signature,
    merge_observed_defect_seed_record,
    observed_defect_seed_source_binding,
    project_observed_defect_seeds,
    reconstruct_external_evaluation_record,
    trace_execution_revision,
)
from .trace_eligibility import (
    TraceEligibilityPolicy,
    build_record_alias_index,
    record_aliases,
    resolve_ref,
)


JsonDict = Dict[str, Any]
SourceBinding = Callable[[JsonDict, List[str]], JsonDict]


def inject_quality_gap_records(trace: JsonDict, review: JsonDict) -> JsonDict:
    return _inject_quality_gap_records(
        trace,
        review,
        bind_structured_observed_defect_source_refs,
    )


def inject_quality_gap_records_graph_free(
    trace: JsonDict,
    review: JsonDict,
) -> JsonDict:
    return _inject_quality_gap_records(
        trace,
        review,
        bind_structured_observed_defect_source_refs_graph_free,
    )


def _inject_quality_gap_records(
    trace: JsonDict,
    review: JsonDict,
    structured_source_binding: SourceBinding,
) -> JsonDict:
    enriched = copy.deepcopy(trace)
    manifest = (
        enriched.get("manifest")
        if isinstance(enriched.get("manifest"), dict)
        else {}
    )
    trace_case_id = manifest.get("case_id")
    review_case_id = review.get("case_id")
    if (
        isinstance(trace_case_id, str)
        and trace_case_id
        and isinstance(review_case_id, str)
        and review_case_id
        and trace_case_id != review_case_id
    ):
        raise ValueError(
            "quality review case_id does not match trace case_id: {0!r} != {1!r}".format(
                review_case_id,
                trace_case_id,
            )
        )
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
    structured_defects = [
        item
        for item in observed_defects
        if isinstance(item, dict) and has_structured_failure_signature(item)
    ]
    for seed in project_observed_defect_seeds(structured_defects):
        inject_observed_defect_seed_record(
            enriched,
            records,
            review,
            seed,
            structured_source_binding,
        )
    for observed_defect in observed_defects:
        if not isinstance(observed_defect, dict):
            continue
        if has_structured_failure_signature(observed_defect):
            continue
        inject_observed_defect_record(enriched, records, review, observed_defect)
    return enriched


def inject_observed_defect_seed_record(
    enriched: JsonDict,
    records: List[Any],
    review: JsonDict,
    seed: ObservedDefectSeed,
    structured_source_binding: SourceBinding,
) -> None:
    record_id = "observed_defect_seed_{0}".format(seed.seed_id)
    existing = next(
        (
            record
            for record in records
            if isinstance(record, dict) and record.get("record_id") == record_id
        ),
        None,
    )
    declared_refs = list(seed.evidence_refs) or review_evidence_refs(review)
    source_binding = structured_source_binding(
        enriched,
        declared_refs,
    )
    included_refs = list(source_binding["all_refs"])
    subject_revision, revision_provenance_status = trace_execution_revision(
        enriched
    )
    revision_status = (
        "matched"
        if subject_revision is not None
        and revision_provenance_status == "valid"
        else revision_provenance_status
    )
    data = seed.to_dict()
    data.update(
        {
            "case_id": review.get("case_id")
            or enriched.get("manifest", {}).get("case_id"),
            "subject_revision": subject_revision,
            "revision_status": revision_status,
            "revision_provenance_status": revision_provenance_status,
            "component": seed.signature.subsystem,
            "failure_type": seed.signature.exception_family,
            "defect_type": seed.signature.exception_family,
            "description": (
                "Observed {0} at {1} while evaluating: {2}".format(
                    seed.signature.exception_family,
                    seed.signature.first_business_frame,
                    seed.signature.assertion_contract,
                )
            ),
            "evidence_refs": included_refs,
            "source_ref_count_total": source_binding["declared_total"],
            "source_ref_count_included": source_binding["included"],
            "source_refs_truncated": source_binding["truncated"],
            "source_binding": source_binding,
            "attribution_domain": "task_quality",
            "reason": (
                "External evaluation failures share one deterministic factual "
                "failure signature."
            ),
        }
    )
    incoming = {
        "record_id": record_id,
        "event_type": "case.observed_defect",
        "component": "evaluation",
        "status": "warning",
        "source_refs": included_refs,
        "data": data,
    }
    if existing is None:
        records.append(incoming)
    else:
        merge_observed_defect_seed_record(existing, incoming)


def bind_structured_observed_defect_source_refs(
    trace: JsonDict,
    declared_refs: List[str],
) -> JsonDict:
    from .graph import TraceGraph

    aliases: Dict[str, set[str]] = {}
    for record in trace.get("records") or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            continue
        canonical = f"record:{record_id}"
        for alias in record_aliases(record):
            aliases.setdefault(str(alias), set()).add(canonical)

    graph = TraceGraph.from_trace(trace)
    included_refs: List[str] = []
    rejected_refs: List[str] = []
    unresolved_refs: List[str] = []
    for declared_ref in dedupe(declared_refs):
        owners = aliases.get(declared_ref, set())
        if len(owners) != 1:
            unresolved_refs.append(declared_ref)
            continue
        resolved = next(iter(owners))
        if graph.active_revision_evidence_eligible(resolved):
            included_refs.append(resolved)
        else:
            rejected_refs.append(resolved)
    return observed_defect_seed_source_binding(
        review_refs=included_refs,
        declared_review_refs=declared_refs,
        rejected_review_refs=rejected_refs,
        unresolved_review_refs=unresolved_refs,
    )


def bind_structured_observed_defect_source_refs_graph_free(
    trace: JsonDict,
    declared_refs: List[str],
) -> JsonDict:
    records = trace.get("records") if isinstance(trace.get("records"), list) else []
    aliases = build_record_alias_index(records)

    def evidence_eligible(_ref: str, record: JsonDict) -> bool:
        if record.get("event_type") != "external.evaluation_fact":
            return True
        reconstructed = reconstruct_external_evaluation_record(trace, record, records)
        data = reconstructed["data"] if reconstructed else {}
        return bool(
            reconstructed
            and data.get("status") in {"failed", "passed"}
            and data.get("revision_status") == "matched"
            and data.get("revision_provenance_status") == "valid"
        )
    policy = TraceEligibilityPolicy(
        trace=trace,
        refs=aliases.records_by_ref,
        resolve=lambda ref: resolve_ref(ref, aliases.aliases),
        node_for_ref=aliases.records_by_ref.get,
        node_data=lambda record: (
            record.get("data") if isinstance(record.get("data"), dict) else {}
        ),
        node_event_type=lambda record: str(record.get("event_type") or ""),
        evidence_eligible=evidence_eligible,
        trace_execution_revision=trace_execution_revision,
    )

    included_refs: List[str] = []
    rejected_refs: List[str] = []
    unresolved_refs: List[str] = []
    for declared_ref in dedupe(declared_refs):
        owners = aliases.owners.get(declared_ref, frozenset())
        if len(owners) != 1:
            unresolved_refs.append(declared_ref)
            continue
        resolved = next(iter(owners))
        if policy.active_revision_evidence_eligible(resolved):
            included_refs.append(resolved)
        else:
            rejected_refs.append(resolved)
    return observed_defect_seed_source_binding(
        review_refs=included_refs,
        declared_review_refs=declared_refs,
        rejected_review_refs=rejected_refs,
        unresolved_review_refs=unresolved_refs,
    )


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
    existing = next(
        (
            record
            for record in records
            if isinstance(record, dict)
            and record.get("record_id") == record_id
        ),
        None,
    )
    all_refs = normalize_refs(observed_defect.get("record_refs")) or review_evidence_refs(review)
    included_refs, source_binding = bind_observed_defect_source_refs(
        enriched,
        all_refs,
    )
    data = (
        dict(existing.get("data"))
        if isinstance(existing, dict)
        and isinstance(existing.get("data"), dict)
        else {}
    )
    data.update(
        {
            "case_id": review.get("case_id") or enriched.get("manifest", {}).get("case_id"),
            "component": component,
            "failure_type": failure_type,
            "defect_type": failure_type,
            "description": observed_defect.get("description"),
            "trace_sufficiency": review.get("trace_sufficiency"),
            "case_effectiveness": review.get("case_effectiveness"),
            "source_ref_count_total": len(all_refs),
            "source_ref_count_included": len(included_refs),
            "source_refs_truncated": len(included_refs) < len(all_refs),
            "source_binding": source_binding,
            "offline_only": True,
            "attribution_domain": "task_quality",
            "reason": "Stress review explicitly declares an observed execution defect with evidence refs.",
        }
    )
    payload = {
        "record_id": record_id,
        "event_type": "case.observed_defect",
        "component": "evaluation",
        "status": "warning",
        "source_refs": included_refs,
        "data": data,
    }
    if existing is None:
        records.append(payload)
    else:
        existing.clear()
        existing.update(payload)


def bind_observed_defect_source_refs(
    trace: JsonDict,
    declared_refs: List[str],
    *,
    limit: Optional[int] = 24,
) -> tuple[List[str], JsonDict]:
    aliases: Dict[str, str] = {}
    for record in trace.get("records") or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            continue
        canonical = f"record:{record_id}"
        for alias in record_aliases(record):
            aliases[str(alias)] = canonical

    resolved_declared: List[str] = []
    unresolved_declared: List[str] = []
    for source_ref in declared_refs:
        resolved = aliases.get(source_ref)
        if resolved:
            resolved_declared.append(resolved)
        else:
            unresolved_declared.append(source_ref)
    resolved_declared = dedupe(resolved_declared)
    unresolved_declared = dedupe(unresolved_declared)

    fallback_refs: List[str] = []
    if resolved_declared:
        mode = "declared_current_trace_refs"
        included = resolved_declared if limit is None else resolved_declared[:limit]
    else:
        fallback_refs = final_result_anchor_refs(trace)
        if limit is not None:
            fallback_refs = fallback_refs[:limit]
        included = fallback_refs
        mode = "final_result_fallback" if fallback_refs else "unbound"

    return included, {
        "schema": "review-observed-defect-source-binding/v1",
        "mode": mode,
        "declared_ref_count": len(declared_refs),
        "resolved_declared_ref_count": len(resolved_declared),
        "resolved_declared_refs": resolved_declared[:24],
        "unresolved_declared_ref_count": len(unresolved_declared),
        "unresolved_declared_refs": unresolved_declared[:24],
        "fallback_ref_count": len(fallback_refs),
        "fallback_refs": fallback_refs,
        "binding_basis": (
            "review_declared_trace_reference"
            if resolved_declared
            else (
                "external_evaluation_targets_current_final_result"
                if fallback_refs
                else "no_current_trace_anchor_available"
            )
        ),
    }


def final_result_anchor_refs(trace: JsonDict) -> List[str]:
    claims: List[str] = []
    outputs: List[str] = []
    terminal_cases: List[str] = []
    for record in trace.get("records") or []:
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id") or "")
        if not record_id:
            continue
        event_type = str(record.get("event_type") or "")
        component = str(record.get("component") or "")
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        ref = f"record:{record_id}"
        if (
            event_type == "response.claim"
            and component == "result"
            and data.get("is_final_for_case") is not False
        ):
            claims.append(ref)
        elif (
            event_type == "response.output"
            and component == "result"
            and data.get("is_final_for_case") is not False
        ):
            outputs.append(ref)
        elif event_type in {
            "case.failed",
            "case.completed",
            "case.cancelled",
        }:
            terminal_cases.append(ref)
    return dedupe([*claims, *outputs, *terminal_cases])


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
