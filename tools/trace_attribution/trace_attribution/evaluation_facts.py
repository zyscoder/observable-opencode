from __future__ import annotations

import copy
import hashlib
from typing import Any, Dict, Iterable, List, Optional

from .graph import record_aliases, resolve_ref
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

    trace_revision = _trace_execution_revision(enriched)
    aliases = _record_alias_index(records)
    existing_edges = {
        str(edge.get("edge_id"))
        for edge in edges
        if isinstance(edge, dict) and edge.get("edge_id")
    }
    for index, candidate in enumerate(payloads):
        payload = _validated_payload(candidate, index)
        record_id = "external_evaluation_{0}".format(
            hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]
        )
        revision_status = (
            "missing"
            if trace_revision is None
            else "matched"
            if payload["subject_revision"] == trace_revision
            else "mismatched"
        )
        resolved_evidence: List[tuple[str, str]] = []
        unresolved_evidence: List[str] = []
        for evidence_ref in payload["evidence_refs"]:
            resolved = resolve_ref(evidence_ref, aliases)
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
            for alias in record_aliases(record):
                aliases[alias] = "record:{0}".format(record_id)
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
            if edge_id in existing_edges:
                continue
            edges.append(
                {
                    "edge_id": edge_id,
                    "from": {
                        "type": "record",
                        "id": resolved.removeprefix("record:"),
                    },
                    "to": {"type": "external_evaluation", "id": record_id},
                    "relation": "external_evaluation_observed",
                    "evidence_type": "external_grader",
                    "evidence_refs": [evidence_ref],
                    "eligible_for_attribution": revision_status == "matched",
                    "metadata": {
                        "revision_status": revision_status,
                        "subject_revision": payload["subject_revision"],
                        "trace_revision": trace_revision,
                        "offline_only": True,
                        "behavior_impact": "none",
                    },
                }
            )
            existing_edges.add(edge_id)
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
        if not isinstance(payload[field], str) or not payload[field]:
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
    if not isinstance(evidence_refs, list) or any(
        not isinstance(item, str) or not item for item in evidence_refs
    ):
        raise ValueError(
            "evaluation payload {0} evidence_refs must be a list of non-empty strings".format(
                index
            )
        )
    if not isinstance(payload["provenance"], dict):
        raise ValueError(
            "evaluation payload {0} provenance must be an object".format(index)
        )
    return copy.deepcopy(payload)


def _trace_execution_revision(trace: JsonDict) -> Optional[str]:
    manifest = trace.get("manifest")
    if not isinstance(manifest, dict):
        return None
    environment = manifest.get("environment")
    if not isinstance(environment, dict):
        return None
    revision = environment.get("revision")
    if not isinstance(revision, str) or not revision:
        return None
    return revision


def _record_alias_index(records: Iterable[Any]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("record_id"):
            continue
        ref = "record:{0}".format(record["record_id"])
        for alias in record_aliases(record):
            aliases[alias] = ref
    return aliases
