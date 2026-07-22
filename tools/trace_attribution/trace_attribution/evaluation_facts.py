from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .graph import eligible_as_attribution_evidence, record_aliases
from .models import JsonDict, stable_json


REQUIRED_FIELDS = (
    "source",
    "scope",
    "subject_revision",
    "assertion",
    "observation",
    "status",
    "observed_at",
    "evidence_refs",
    "provenance",
)
STRING_FIELDS = (
    "source",
    "scope",
    "subject_revision",
    "assertion",
    "observation",
    "observed_at",
)
EVALUATION_STATUSES = frozenset({"passed", "failed", "unknown"})


def inject_external_evaluation_facts(
    trace: JsonDict, payloads: Iterable[JsonDict]
) -> JsonDict:
    enriched = copy.deepcopy(trace)
    records = enriched.setdefault("records", [])
    edges = enriched.setdefault("dataflow_edges", [])
    if not isinstance(records, list):
        raise ValueError("trace records must be a list")
    if not isinstance(edges, list):
        raise ValueError("trace dataflow_edges must be a list")

    trace_revision, revision_provenance_status = _trace_execution_revision(enriched)
    aliases = _record_alias_index(records)
    records_by_ref = {
        "record:{0}".format(item["record_id"]): item
        for item in records
        if isinstance(item, dict) and item.get("record_id")
    }
    existing_edges: Dict[str, JsonDict] = {}
    for edge in edges:
        if not isinstance(edge, dict) or not edge.get("edge_id"):
            continue
        edge_id = str(edge["edge_id"])
        prior = existing_edges.get(edge_id)
        if prior is not None and prior != edge:
            raise ValueError("existing edge ID collision for {0}".format(edge_id))
        existing_edges[edge_id] = edge
    for index, candidate in enumerate(payloads):
        payload = _validated_payload(candidate, index)
        record_id = "external_evaluation_{0}".format(
            hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]
        )
        revision_status = revision_provenance_status
        if trace_revision is not None:
            revision_status = (
                "matched"
                if payload["subject_revision"] == trace_revision
                else "mismatched"
            )
        resolved_evidence: List[tuple[str, str]] = []
        unresolved_evidence: List[str] = []
        for evidence_ref in payload["evidence_refs"]:
            resolved = _resolve_unique_ref(evidence_ref, aliases)
            if resolved:
                resolved_evidence.append((evidence_ref, resolved))
            else:
                unresolved_evidence.append(evidence_ref)

        data = copy.deepcopy(payload)
        data.update(
            {
                "evaluation_id": record_id,
                "trace_revision": trace_revision,
                "revision_status": revision_status,
                "revision_provenance_status": revision_provenance_status,
                "eligible_for_decisive_judgment": (
                    revision_status == "matched" and payload["status"] == "failed"
                ),
                "unresolved_evidence_refs": unresolved_evidence,
                "root_candidate_eligible": False,
                "offline_only": True,
                "behavior_impact": "none",
            }
        )
        record = {
            "record_id": record_id,
            "event_type": "external.evaluation_fact",
            "component": "evaluation",
            "status": payload["status"],
            "timestamp": payload["observed_at"],
            "title": payload["assertion"],
            "data": data,
        }
        existing = next(
            (
                item
                for item in records
                if isinstance(item, dict) and item.get("record_id") == record_id
            ),
            None,
        )
        if existing is None:
            records.append(record)
            records_by_ref["record:{0}".format(record_id)] = record
            for alias in record_aliases(record):
                aliases.setdefault(alias, set()).add(
                    "record:{0}".format(record_id)
                )
        elif existing != record:
            raise ValueError(
                "external evaluation record ID collision for {0}".format(record_id)
            )

        for evidence_ref, resolved in resolved_evidence:
            edge_id = "external_evaluation_edge_{0}".format(
                hashlib.sha256(
                    stable_json(
                        {"evaluation_id": record_id, "evidence_ref": evidence_ref}
                    ).encode("utf-8")
                ).hexdigest()[:16]
            )
            edge = {
                "edge_id": edge_id,
                "from": {
                    "type": "record",
                    "id": resolved.removeprefix("record:"),
                },
                "to": {"type": "external_evaluation", "id": record_id},
                "relation": "external_evaluation_observed",
                "evidence_type": "external_grader",
                "evidence_refs": [evidence_ref],
                "eligible_for_attribution": bool(
                    records_by_ref.get(resolved)
                    and eligible_as_attribution_evidence(records_by_ref[resolved])
                    and eligible_as_attribution_evidence(record)
                ),
                "metadata": {
                    "revision_status": revision_status,
                    "revision_provenance_status": revision_provenance_status,
                    "subject_revision": payload["subject_revision"],
                    "trace_revision": trace_revision,
                    "offline_only": True,
                    "behavior_impact": "none",
                },
            }
            existing_edge = existing_edges.get(edge_id)
            if existing_edge == edge:
                continue
            if existing_edge is not None:
                raise ValueError(
                    "external evaluation edge ID collision for {0}".format(edge_id)
                )
            edges.append(edge)
            existing_edges[edge_id] = edge
    return enriched


def _validated_payload(payload: Any, index: int) -> JsonDict:
    if not isinstance(payload, dict):
        raise ValueError("evaluation payload {0} must be an object".format(index))
    if set(payload) != set(REQUIRED_FIELDS):
        raise ValueError(
            "evaluation payload {0} fields must be exactly: {1}".format(
                index, ", ".join(REQUIRED_FIELDS)
            )
        )
    for field in STRING_FIELDS:
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(
                "evaluation payload {0} field {1} must be a non-empty string".format(
                    index, field
                )
            )
    status = payload["status"]
    if not isinstance(status, str) or status not in EVALUATION_STATUSES:
        raise ValueError(
            "evaluation payload {0} status must be passed, failed, or unknown".format(
                index
            )
        )
    evidence_refs = payload["evidence_refs"]
    if not isinstance(evidence_refs, list) or not evidence_refs or any(
        not isinstance(item, str) or not item.strip() for item in evidence_refs
    ):
        raise ValueError(
            "evaluation payload {0} evidence_refs must be a list of non-empty strings".format(
                index
            )
        )
    if len({item.strip() for item in evidence_refs}) != len(evidence_refs):
        raise ValueError(
            "evaluation payload {0} evidence_refs must contain unique strings".format(
                index
            )
        )
    try:
        observed_at = datetime.fromisoformat(
            payload["observed_at"].replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError(
            "evaluation payload {0} observed_at must be a valid ISO-8601 timestamp with timezone".format(
                index
            )
        ) from exc
    if (
        "T" not in payload["observed_at"]
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError(
            "evaluation payload {0} observed_at must be a valid ISO-8601 timestamp with timezone".format(
                index
            )
        )
    provenance = payload["provenance"]
    if not isinstance(provenance, dict) or not provenance:
        raise ValueError(
            "evaluation payload {0} provenance must be a non-empty object".format(
                index
            )
        )
    for field in ("method", "version"):
        if (
            not isinstance(provenance.get(field), str)
            or not provenance[field].strip()
        ):
            raise ValueError(
                "evaluation payload {0} provenance {1} must be a non-empty string".format(
                    index, field
                )
            )
    try:
        _validate_json_safe(provenance)
    except ValueError as exc:
        raise ValueError(
            "evaluation payload {0} provenance must contain JSON-safe finite values: {1}".format(
                index, exc
            )
        ) from exc
    return copy.deepcopy(payload)


def _validate_json_safe(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_safe(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("object keys must be strings")
        for item in value.values():
            _validate_json_safe(item)
        return
    raise ValueError("unsupported value type {0}".format(type(value).__name__))


def _trace_execution_revision(trace: JsonDict) -> Tuple[Optional[str], str]:
    manifest = trace.get("manifest")
    if not isinstance(manifest, dict):
        return None, "missing"
    revision = manifest.get("subject_revision")
    if not isinstance(revision, str) or not revision.strip():
        return None, "missing"
    provenance = manifest.get("subject_revision_provenance")
    if not isinstance(provenance, dict):
        return None, "unprovenanced"
    method_and_source = (provenance.get("method"), provenance.get("source"))
    valid_sources = {
        ("case_trace_config", "CaseTraceConfig.subjectRevision"),
        ("environment_variable", "OPENCODE_TRACE_SUBJECT_REVISION"),
    }
    if (
        method_and_source not in valid_sources
        or provenance.get("bound_at") != "case_start"
        or not isinstance(manifest.get("case_id"), str)
        or not manifest["case_id"]
        or provenance.get("case_id") != manifest["case_id"]
        or not isinstance(manifest.get("run_id"), str)
        or not manifest["run_id"]
        or provenance.get("run_id") != manifest["run_id"]
    ):
        return None, "unprovenanced"
    return revision, "valid"


def _record_alias_index(records: Iterable[Any]) -> Dict[str, Set[str]]:
    aliases: Dict[str, Set[str]] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("record_id"):
            continue
        ref = "record:{0}".format(record["record_id"])
        for alias in record_aliases(record):
            aliases.setdefault(alias, set()).add(ref)
    return aliases


def _resolve_unique_ref(
    ref: str, aliases: Dict[str, Set[str]]
) -> Optional[str]:
    candidates = [ref]
    if ":" not in ref:
        candidates = ["record:{0}".format(ref), "node:{0}".format(ref)]
    for candidate in candidates:
        owners = aliases.get(candidate, set())
        if len(owners) == 1:
            return next(iter(owners))
        if len(owners) > 1:
            return None
    return None
