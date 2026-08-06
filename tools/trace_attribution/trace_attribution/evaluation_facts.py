from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

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
DERIVED_DATA_FIELDS = frozenset(
    {
        "evaluation_id",
        "trace_revision",
        "revision_status",
        "revision_provenance_status",
        "eligible_for_decisive_judgment",
        "failure_signature_id",
        "observed_defect_seed_id",
        "unresolved_evidence_refs",
        "root_candidate_eligible",
        "offline_only",
        "behavior_impact",
    }
)

FAILURE_SIGNATURE_SCHEMA_VERSION = "evaluation-failure-signature/v1"
FAILURE_SIGNATURE_MINIMUM_FIELDS = (
    frozenset({"exception_family", "exception_type"}),
    frozenset({"first_business_frame", "business_frame"}),
    frozenset({"assertion_contract", "assertion"}),
    frozenset({"relevant_symbol", "symbol"}),
    frozenset({"subsystem", "affected_subsystem"}),
)
PREREQUISITE_NEUTRALIZATION_STRING_FIELDS = (
    "schema_version",
    "signature",
    "prerequisite_signature_id",
    "method",
    "status",
    "evaluation_layer_id",
)
PREREQUISITE_NEUTRALIZATION_BOOLEAN_FIELDS = ("neutralized",)


@dataclass(frozen=True)
class FailureSignature:
    """Order-independent factual identity for one externally observed failure."""

    exception_family: str
    first_business_frame: str
    assertion_contract: str
    contract_template: str
    observation_values: Tuple[str, ...]
    relevant_symbol: str
    subsystem: str
    signature_id: str = field(init=False)

    def __post_init__(self) -> None:
        payload = {
            "contract_template": self.contract_template,
            "exception_family": self.exception_family,
            "first_business_frame": self.first_business_frame,
            "observation_values": list(self.observation_values),
            "relevant_symbol": self.relevant_symbol,
            "subsystem": self.subsystem,
        }
        object.__setattr__(
            self,
            "signature_id",
            hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        )

    def to_dict(self) -> JsonDict:
        return {
            "signature_id": self.signature_id,
            "exception_family": self.exception_family,
            "first_business_frame": self.first_business_frame,
            "assertion_contract": self.assertion_contract,
            "contract_template": self.contract_template,
            "observation_values": list(self.observation_values),
            "relevant_symbol": self.relevant_symbol,
            "subsystem": self.subsystem,
        }


@dataclass(frozen=True)
class ObservedDefectSeed:
    """Immutable offline seed projected from one failure-signature cluster."""

    signature: FailureSignature
    evaluation_fact_ids: Tuple[str, ...]
    test_ids: Tuple[str, ...]
    evaluation_run_ids: Tuple[str, ...]
    evaluation_layer_ids: Tuple[str, ...]
    prerequisite_neutralizations: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    seed_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "seed_id",
            self.signature.signature_id,
        )

    def to_dict(self) -> JsonDict:
        return {
            "seed_id": self.seed_id,
            "failure_signature": self.signature.to_dict(),
            "evaluation_fact_ids": list(self.evaluation_fact_ids),
            "test_ids": list(self.test_ids),
            "evaluation_run_ids": list(self.evaluation_run_ids),
            "evaluation_layer_ids": list(self.evaluation_layer_ids),
            "prerequisite_neutralizations": [
                json.loads(item) for item in self.prerequisite_neutralizations
            ],
            "evidence_refs": list(self.evidence_refs),
            "cluster_size": len(self.evaluation_fact_ids),
            "root_candidate_eligible": False,
            "offline_only": True,
            "behavior_impact": "none",
        }


def project_observed_defect_seeds(
    failures: Iterable[Mapping[str, Any]],
) -> Tuple[ObservedDefectSeed, ...]:
    """Cluster heterogeneous evaluation failures without consuming labels."""

    clusters: Dict[str, List[JsonDict]] = {}
    signatures: Dict[str, FailureSignature] = {}
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        normalized = _normalize_evaluation_failure(failure)
        signature = normalized["signature"]
        signatures[signature.signature_id] = signature
        clusters.setdefault(signature.signature_id, []).append(normalized)

    seeds: List[ObservedDefectSeed] = []
    for signature_id in sorted(clusters):
        members = clusters[signature_id]
        seeds.append(
            ObservedDefectSeed(
                signature=signatures[signature_id],
                evaluation_fact_ids=_sorted_unique(
                    item["evaluation_fact_id"] for item in members
                ),
                test_ids=_sorted_unique(
                    item["test_id"] for item in members if item["test_id"]
                ),
                evaluation_run_ids=_sorted_unique(
                    item["evaluation_run_id"]
                    for item in members
                    if item["evaluation_run_id"]
                ),
                evaluation_layer_ids=_sorted_unique(
                    item["evaluation_layer_id"]
                    for item in members
                    if item["evaluation_layer_id"]
                ),
                prerequisite_neutralizations=_sorted_unique(
                    item["prerequisite_neutralization"]
                    for item in members
                    if item["prerequisite_neutralization"]
                ),
                evidence_refs=_sorted_unique(
                    ref for item in members for ref in item["evidence_refs"]
                ),
            )
        )
    return tuple(seeds)


def has_structured_failure_signature(failure: Mapping[str, Any]) -> bool:
    declared = _declared_failure_signature(failure)
    if declared is not None:
        return True
    available = {
        str(key)
        for mapping in _factual_mappings(failure)
        for key in mapping
    }
    return all(group & available for group in FAILURE_SIGNATURE_MINIMUM_FIELDS)


def _declared_failure_signature(
    failure: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    direct = failure.get("failure_signature")
    if (
        isinstance(direct, Mapping)
        and direct.get("schema_version") == FAILURE_SIGNATURE_SCHEMA_VERSION
    ):
        return direct
    provenance = failure.get("provenance")
    if isinstance(provenance, Mapping):
        nested = provenance.get("failure_signature")
        if (
            isinstance(nested, Mapping)
            and nested.get("schema_version")
            == FAILURE_SIGNATURE_SCHEMA_VERSION
        ):
            return nested
    version = failure.get("failure_signature_schema_version")
    if version == FAILURE_SIGNATURE_SCHEMA_VERSION:
        return failure
    return None


def _allowlisted_declared_failure_signature(
    declared: Mapping[str, Any],
) -> JsonDict:
    output: JsonDict = {"schema_version": FAILURE_SIGNATURE_SCHEMA_VERSION}
    string_fields = (
        "exception_family",
        "exception_type",
        "failure_type",
        "descriptor",
        "assertion_contract",
        "assertion",
        "contract",
        "relevant_symbol",
        "symbol",
        "business_symbol",
        "subsystem",
        "affected_subsystem",
        "module",
        "package",
        "traceback",
        "message",
        "observation",
    )
    for field_name in string_fields:
        value = declared.get(field_name)
        if isinstance(value, str) and value.strip():
            output[field_name] = value
    for field_name in ("first_business_frame", "business_frame"):
        frame = _allowlisted_business_frame(declared.get(field_name))
        if frame not in (None, "", {}):
            output[field_name] = frame
    for field_name in ("business_frames", "frames", "stack"):
        frames = declared.get(field_name)
        if not isinstance(frames, list):
            continue
        allowed_frames = [
            frame
            for frame in (_allowlisted_business_frame(item) for item in frames)
            if frame not in (None, "", {})
        ]
        if allowed_frames:
            output[field_name] = allowed_frames
    for field_name in ("expected", "actual"):
        value = declared.get(field_name)
        if _is_typed_contract_value(value):
            output[field_name] = copy.deepcopy(value)
    return output


def _allowlisted_business_frame(value: Any) -> Any:
    if isinstance(value, str) and value.strip():
        return value
    if not isinstance(value, Mapping):
        return None
    frame: JsonDict = {}
    for field_name in (
        "file",
        "path",
        "filename",
        "symbol",
        "function",
        "name",
    ):
        item = value.get(field_name)
        if isinstance(item, str) and item.strip():
            frame[field_name] = item
    is_business_frame = value.get("is_business_frame")
    if type(is_business_frame) is bool:
        frame["is_business_frame"] = is_business_frame
    return frame or None


def _is_typed_contract_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return not isinstance(value, float) or math.isfinite(value)
    if isinstance(value, list):
        return all(_is_typed_contract_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_typed_contract_value(item)
            for key, item in value.items()
        )
    return False


def _normalize_evaluation_failure(
    failure: Mapping[str, Any],
) -> JsonDict:
    declared_signature = _declared_failure_signature(failure)
    if declared_signature is not None:
        signature_mappings = (
            _allowlisted_declared_failure_signature(declared_signature),
        )
        member_mappings = (failure, declared_signature)
    else:
        signature_mappings = _factual_mappings(failure)
        member_mappings = signature_mappings
    diagnostic_text = _diagnostic_text(signature_mappings)
    exception_family = _normalized_text(
        _first_factual_value(
            signature_mappings,
            (
                "exception_family",
                "exception_type",
                "failure_type",
                "descriptor",
            ),
        ),
        fallback=_exception_family_from_text(diagnostic_text),
    )
    frame_value = _first_factual_value(
        signature_mappings,
        ("first_business_frame", "business_frame"),
    )
    if frame_value is None:
        frame_value = _first_business_frame_from_stack(signature_mappings)
    first_business_frame = _normalize_business_frame(frame_value)
    declared_contract = _first_factual_value(
        signature_mappings,
        ("assertion_contract", "assertion", "contract"),
    )
    contract_template, observation_values = _project_assertion_contract(
        declared_contract,
        mappings=signature_mappings,
        diagnostic_text=diagnostic_text,
    )
    relevant_symbol = _normalized_text(
        _first_factual_value(
            signature_mappings,
            ("relevant_symbol", "symbol", "business_symbol"),
        ),
        fallback=_symbol_from_business_frame(first_business_frame),
    )
    subsystem = _normalized_text(
        _first_factual_value(
            signature_mappings,
            ("subsystem", "affected_subsystem", "module", "package"),
        ),
        fallback=_subsystem_from_business_frame(first_business_frame),
    )
    signature = FailureSignature(
        exception_family=exception_family,
        first_business_frame=first_business_frame,
        assertion_contract=contract_template,
        contract_template=contract_template,
        observation_values=observation_values,
        relevant_symbol=relevant_symbol,
        subsystem=subsystem,
    )

    test_id = _normalized_text(
        _first_factual_value(
            member_mappings,
            ("test_id", "test", "nodeid"),
        ),
        fallback="",
    )
    evaluation_run_id = _normalized_text(
        _first_factual_value(
            member_mappings,
            ("evaluation_run_id", "run_id"),
        ),
        fallback="",
    )
    evaluation_layer_id = _normalized_text(
        _first_factual_value(
            member_mappings,
            ("evaluation_layer_id", "layer_id", "evaluation_layer"),
        ),
        fallback="",
    )
    neutralization = _first_factual_value(
        member_mappings,
        (
            "prerequisite_neutralization",
            "prerequisite_neutralization_fact",
        ),
    )
    neutralization_json = ""
    if neutralization is not None:
        normalized_neutralization = _normalize_prerequisite_neutralization(
            neutralization
        )
        if normalized_neutralization:
            neutralization_json = _canonical_json(normalized_neutralization)
    evidence_refs = _normalized_refs(
        _first_factual_value(
            member_mappings,
            ("record_refs", "evidence_refs"),
        )
    )
    identity_payload = {
        "evaluation_layer_id": evaluation_layer_id,
        "evaluation_run_id": evaluation_run_id,
        "prerequisite_neutralization": neutralization_json,
        "signature_id": signature.signature_id,
        "test_id": test_id,
    }
    explicit_fact_id = _normalized_text(
        _first_factual_value(
            member_mappings,
            (
                "evaluation_fact_id",
                "evaluation_record_id",
                "evaluation_id",
            ),
        ),
        fallback="",
    )
    return {
        "signature": signature,
        "evaluation_fact_id": explicit_fact_id
        or hashlib.sha256(
            _canonical_json(identity_payload).encode("utf-8")
        ).hexdigest(),
        "test_id": test_id,
        "evaluation_run_id": evaluation_run_id,
        "evaluation_layer_id": evaluation_layer_id,
        "prerequisite_neutralization": neutralization_json,
        "evidence_refs": evidence_refs,
    }


def _factual_mappings(value: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    mappings: List[Mapping[str, Any]] = []
    queue: List[Mapping[str, Any]] = [value]
    while queue:
        current = queue.pop(0)
        mappings.append(current)
        for key in (
            "data",
            "details",
            "evaluation",
            "exception",
            "failure",
            "failure_signature",
            "provenance",
        ):
            child = current.get(key)
            if isinstance(child, Mapping):
                queue.append(child)
    return tuple(mappings)


def _first_factual_value(
    mappings: Tuple[Mapping[str, Any], ...],
    keys: Tuple[str, ...],
) -> Any:
    for key in keys:
        for mapping in mappings:
            value = mapping.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _first_business_frame_from_stack(
    mappings: Tuple[Mapping[str, Any], ...],
) -> Any:
    stack = _first_factual_value(
        mappings,
        ("business_frames", "frames", "stack", "traceback"),
    )
    if isinstance(stack, str):
        return _business_frame_from_traceback(stack)
    if not isinstance(stack, list):
        return None
    for frame in stack:
        if not isinstance(frame, Mapping):
            continue
        if frame.get("is_business_frame") is False:
            continue
        path = str(frame.get("file") or frame.get("path") or "")
        if "site-packages/" in path or "/lib/python" in path:
            continue
        return frame
    return None


def _business_frame_from_traceback(value: str) -> Optional[JsonDict]:
    matches = re.findall(
        r'File\s+["\']([^"\']+)["\'],\s+line\s+\d+,\s+in\s+([^\s]+)',
        value,
    )
    for path, symbol in matches:
        normalized = path.replace("\\", "/")
        if "site-packages/" in normalized or "/lib/python" in normalized:
            continue
        return {"file": path, "symbol": symbol}
    return None


def _normalize_business_frame(value: Any) -> str:
    if isinstance(value, Mapping):
        path = _normalized_path(
            value.get("file") or value.get("path") or value.get("filename")
        )
        symbol = _normalized_text(
            value.get("symbol")
            or value.get("function")
            or value.get("name"),
            fallback="unknown",
        )
        if path:
            return "{0}::{1}".format(path, symbol)
        return symbol
    if isinstance(value, str) and "Traceback" in value:
        parsed = _business_frame_from_traceback(value)
        if parsed is not None:
            return _normalize_business_frame(parsed)
    return _normalized_text(value, fallback="unknown")


def _project_assertion_contract(
    declared_contract: Any,
    *,
    mappings: Tuple[Mapping[str, Any], ...],
    diagnostic_text: str,
) -> Tuple[str, Tuple[str, ...]]:
    if declared_contract not in (None, "", [], {}):
        return _project_contract_value(declared_contract)

    expected_value = _first_factual_value(mappings, ("expected",))
    actual_value = _first_factual_value(
        mappings,
        ("actual", "message", "observation"),
    )
    if actual_value in (None, "", [], {}):
        actual_value = _exception_message_from_text(diagnostic_text)
    expected, expected_observations = _project_contract_value(expected_value)
    actual, actual_observations = _project_contract_value(
        actual_value,
        observation_side=True,
    )
    if expected or actual:
        return (
            "expected={0}; actual={1}".format(
                expected or "unknown",
                actual or "unknown",
            ),
            tuple([*expected_observations, *actual_observations]),
        )
    return "unknown", ()


def _diagnostic_text(
    mappings: Tuple[Mapping[str, Any], ...],
) -> str:
    value = _first_factual_value(
        mappings,
        ("traceback", "message", "observation"),
    )
    return value if isinstance(value, str) else ""


def _exception_family_from_text(value: str) -> str:
    matches = re.findall(
        r"(?:^|\n)([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Annotation))\s*:",
        value,
    )
    if matches:
        return matches[-1].rsplit(".", 1)[-1]
    return "UnknownFailure"


def _exception_message_from_text(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.match(
            r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Annotation)\s*:",
            line,
        ):
            return line.split(":", 1)[1].strip()
    return value


def _project_contract_value(
    value: Any,
    *,
    observation_side: bool = False,
) -> Tuple[str, Tuple[str, ...]]:
    if isinstance(value, (Mapping, list, tuple)):
        text = _canonical_json(value)
    else:
        text = _normalized_text(value, fallback="")
    if not text:
        return "", ()
    if "Traceback" in text:
        text = _exception_message_from_text(text)
    observations: List[str] = []

    def replace_with(marker: str, category: str):
        def replace(_: re.Match[str]) -> str:
            observations.append(category)
            return marker

        return replace

    text = re.sub(
        r"0x[0-9a-fA-F]+",
        replace_with("<addr>", "address"),
        text,
    )
    text = re.sub(
        r"<([A-Za-z_][A-Za-z0-9_.]*)\s+[^<>]*>",
        lambda match: _replace_repr(match, observations),
        text,
    )
    text = re.sub(
        r"/(?:private/)?tmp/[^\s;,\"']+",
        replace_with("<tmp_path>", "temporary_path"),
        text,
    )
    text = re.sub(
        r"/var/folders/[^\s;,\"']+",
        replace_with("<tmp_path>", "temporary_path"),
        text,
    )
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b",
        replace_with("<uuid>", "uuid"),
        text,
    )
    text = re.sub(
        r"\b(?P<label>(?:request|instance)[\s_-]*id)"
        r"(?P<quote>[\"']?)\s*(?P<separator>[:=]?\s*)"
        r"(?P<value_quote>[\"']?)"
        r"(?P<value>[A-Za-z0-9._-]+)(?P=value_quote)"
        r"(?=$|[\s,;}\]])",
        lambda match: _replace_dynamic_identifier(match, observations),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?P<label>(?:request|instance|field|parameter|test))"
        r"(?P<quote>[\"']?)"
        r"\s*(?P<separator>[:=]?\s*)"
        r"(?P<value>[-+]?\d+)\b",
        lambda match: _replace_dynamic_identifier(match, observations),
        text,
        flags=re.IGNORECASE,
    )
    if observation_side:
        text = _normalize_observation_numbers(text, observations)
    else:
        marker = re.search(
            r"\b(?:got|actual|observed)\b",
            text,
            flags=re.IGNORECASE,
        )
        if marker is not None:
            text = "{0}{1}".format(
                text[: marker.start()],
                _normalize_observation_numbers(
                    text[marker.start() :],
                    observations,
                ),
            )
    return " ".join(text.split()) or "unknown", tuple(observations)


def _replace_repr(
    match: re.Match[str],
    observations: List[str],
) -> str:
    observations.append("repr")
    return "<{0} repr>".format(match.group(1))


def _replace_dynamic_identifier(
    match: re.Match[str],
    observations: List[str],
) -> str:
    label = re.sub(r"[\s_-]+", "_", match.group("label").lower())
    category = label if label.endswith("_id") else "{0}_id".format(label)
    observations.append(category)
    separator = match.group("separator") or " "
    value_quote = match.groupdict().get("value_quote") or ""
    return "{0}{1}{2}{3}<instance_id>{3}".format(
        match.group("label"),
        match.group("quote") or "",
        separator,
        value_quote,
    )


def _normalize_observation_numbers(
    text: str,
    observations: List[str],
) -> str:
    def replace(_: re.Match[str]) -> str:
        observations.append("number")
        return "<observed_number>"

    return re.sub(
        r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?![A-Za-z_])",
        replace,
        text,
    )


def _symbol_from_business_frame(frame: str) -> str:
    if "::" in frame:
        return frame.rsplit("::", 1)[1]
    return frame


def _subsystem_from_business_frame(frame: str) -> str:
    path = frame.split("::", 1)[0]
    if path == "unknown":
        return "unknown"
    without_suffix = re.sub(r"\.py$", "", path)
    return without_suffix.replace("/", ".")


def _normalized_text(value: Any, *, fallback: str) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return fallback
    normalized = " ".join(str(value).strip().split())
    return normalized or fallback


def _normalized_path(value: Any) -> str:
    text = _normalized_text(value, fallback="").replace("\\", "/")
    marker = "/site-packages/"
    if marker in text:
        text = text.split(marker, 1)[1]
    text = re.sub(
        r"^/(?:private/)?tmp/[^/]+/",
        "<tmp>/",
        text,
    )
    text = re.sub(
        r"^/var/folders/[^/]+/[^/]+/T/[^/]+/",
        "<tmp>/",
        text,
    )
    return text.removeprefix("./")


def _normalized_refs(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return _sorted_unique(
        str(item).strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _normalize_prerequisite_neutralization(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        return {}
    output: JsonDict = {}
    for key in PREREQUISITE_NEUTRALIZATION_STRING_FIELDS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            output[key] = " ".join(item.strip().split())
    for key in PREREQUISITE_NEUTRALIZATION_BOOLEAN_FIELDS:
        item = value.get(key)
        if type(item) is bool:
            output[key] = item
    evidence_refs = value.get("evidence_refs")
    normalized_refs = _normalized_refs(evidence_refs)
    if normalized_refs:
        output["evidence_refs"] = list(normalized_refs)
    return output


def _sorted_unique(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def inject_external_evaluation_facts(
    trace: JsonDict, payloads: Iterable[JsonDict]
) -> JsonDict:
    from .trace_eligibility import record_aliases

    enriched = copy.deepcopy(trace)
    records = enriched.setdefault("records", [])
    edges = enriched.setdefault("dataflow_edges", [])
    if not isinstance(records, list):
        raise ValueError("trace records must be a list")
    if not isinstance(edges, list):
        raise ValueError("trace dataflow_edges must be a list")

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
    validated_payloads = [
        _validated_payload(candidate, index)
        for index, candidate in enumerate(payloads)
    ]
    structured_failures = [
        {
            **payload,
            "evaluation_fact_id": _external_evaluation_record_id(payload),
        }
        for payload in validated_payloads
        if payload["status"] == "failed"
        and has_structured_failure_signature(payload)
        and _external_seed_revision_eligible(enriched, payload)
    ]
    projected_seeds = project_observed_defect_seeds(structured_failures)
    for payload in validated_payloads:
        record_id = _external_evaluation_record_id(payload)
        ref = "record:{0}".format(record_id)
        identity_record = {
            "record_id": record_id,
            "event_type": "external.evaluation_fact",
            "data": {"evaluation_id": record_id},
        }
        for alias in record_aliases(identity_record):
            aliases.setdefault(alias, set()).add(ref)

    prepared: List[Tuple[JsonDict, JsonDict, List[Tuple[str, str]]]] = []
    for payload in validated_payloads:
        record_ref = "record:{0}".format(_external_evaluation_record_id(payload))
        resolution_aliases = {
            alias: owners - {record_ref}
            for alias, owners in aliases.items()
            if owners - {record_ref}
        }
        record, resolved_evidence = _build_external_evaluation_record(
            enriched, payload, resolution_aliases
        )
        record_id = str(record["record_id"])
        ref = "record:{0}".format(record_id)
        existing = records_by_ref.get(ref)
        if existing is None:
            records.append(record)
            records_by_ref[ref] = record
        elif existing != record:
            raise ValueError(
                "external evaluation record ID collision for {0}".format(record_id)
            )
        prepared.append((payload, record, resolved_evidence))

    for payload, record, resolved_evidence in prepared:
        record_id = str(record["record_id"])
        data = record["data"]
        revision_status = str(data["revision_status"])
        trace_revision = data["trace_revision"]
        revision_provenance_status = str(data["revision_provenance_status"])
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
                    and _record_evidence_eligible(
                        enriched, records_by_ref[resolved], records
                    )
                    and _record_evidence_eligible(enriched, record, records)
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
    for seed in projected_seeds:
        source_refs = sorted(
            "record:{0}".format(_external_evaluation_record_id(payload))
            for payload in validated_payloads
            if payload["status"] == "failed"
            and has_structured_failure_signature(payload)
            and _external_seed_revision_eligible(enriched, payload)
            and _normalize_evaluation_failure(payload)["signature"].signature_id
            == seed.signature.signature_id
        )
        source_revisions = {
            payload["subject_revision"]
            for payload in validated_payloads
            if "record:{0}".format(_external_evaluation_record_id(payload))
            in source_refs
        }
        if len(source_revisions) != 1:
            raise ValueError(
                "observed defect seed must have one provenanced source revision"
            )
        seed_record = _build_external_observed_defect_seed_record(
            seed,
            source_refs,
            case_id=(
                enriched.get("manifest", {}).get("case_id")
                if isinstance(enriched.get("manifest"), dict)
                else None
            ),
            subject_revision=next(iter(source_revisions)),
        )
        seed_ref = "record:{0}".format(seed_record["record_id"])
        existing = records_by_ref.get(seed_ref)
        if existing is None:
            records.append(seed_record)
            records_by_ref[seed_ref] = seed_record
        else:
            merge_observed_defect_seed_record(existing, seed_record)
    return enriched


def _external_seed_revision_eligible(
    trace: JsonDict,
    payload: Mapping[str, Any],
) -> bool:
    trace_revision, provenance_status = trace_execution_revision(trace)
    return bool(
        provenance_status == "valid"
        and trace_revision is not None
        and payload.get("subject_revision") == trace_revision
    )


def reconstruct_external_evaluation_record(
    trace: JsonDict,
    record: Any,
    records: Optional[Iterable[Any]] = None,
) -> Optional[JsonDict]:
    """Rebuild an external fact from trusted manifest data and its strict payload."""
    if not isinstance(record, dict) or record.get("event_type") != "external.evaluation_fact":
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    if set(data) != set(REQUIRED_FIELDS) | DERIVED_DATA_FIELDS:
        return None
    try:
        payload = _validated_payload(
            {field: data[field] for field in REQUIRED_FIELDS}, 0
        )
    except (KeyError, ValueError):
        return None
    record_id = str(record.get("record_id") or "")
    source_records = list(
        records if records is not None else trace.get("records") or []
    )
    aliases = _record_alias_index(
        item
        for item in source_records
        if not (
            isinstance(item, dict)
            and str(item.get("record_id") or "") == record_id
        )
    )
    expected, _ = _build_external_evaluation_record(trace, payload, aliases)
    return expected if record == expected else None


def _build_external_evaluation_record(
    trace: JsonDict,
    payload: JsonDict,
    aliases: Dict[str, Set[str]],
) -> Tuple[JsonDict, List[Tuple[str, str]]]:
    record_id = _external_evaluation_record_id(payload)
    trace_revision, revision_provenance_status = trace_execution_revision(trace)
    revision_status = revision_provenance_status
    if trace_revision is not None:
        revision_status = (
            "matched"
            if payload["subject_revision"] == trace_revision
            else "mismatched"
        )
    resolved_evidence: List[Tuple[str, str]] = []
    unresolved_evidence: List[str] = []
    for evidence_ref in payload["evidence_refs"]:
        resolved = _resolve_unique_ref(evidence_ref, aliases)
        if resolved:
            resolved_evidence.append((evidence_ref, resolved))
        else:
            unresolved_evidence.append(evidence_ref)
    structured_signature: Optional[FailureSignature] = None
    if payload["status"] == "failed" and has_structured_failure_signature(payload):
        structured_signature = _normalize_evaluation_failure(payload)["signature"]
    signature_id = (
        structured_signature.signature_id if structured_signature is not None else None
    )
    seed_id = (
        signature_id
        if signature_id is not None
        and _external_seed_revision_eligible(trace, payload)
        else None
    )
    data = copy.deepcopy(payload)
    data.update(
        {
            "evaluation_id": record_id,
            "trace_revision": trace_revision,
            "revision_status": revision_status,
            "revision_provenance_status": revision_provenance_status,
            "eligible_for_decisive_judgment": (
                revision_status == "matched"
                and payload["status"] == "failed"
                and seed_id is None
            ),
            "failure_signature_id": signature_id,
            "observed_defect_seed_id": seed_id,
            "unresolved_evidence_refs": unresolved_evidence,
            "root_candidate_eligible": False,
            "offline_only": True,
            "behavior_impact": "none",
        }
    )
    return (
        {
            "record_id": record_id,
            "event_type": "external.evaluation_fact",
            "component": "evaluation",
            "status": payload["status"],
            "timestamp": payload["observed_at"],
            "title": payload["assertion"],
            "data": data,
        },
        resolved_evidence,
    )


def _build_external_observed_defect_seed_record(
    seed: ObservedDefectSeed,
    source_refs: List[str],
    *,
    case_id: Any,
    subject_revision: str,
) -> JsonDict:
    if not source_refs:
        raise ValueError("observed defect seed requires at least one source fact")
    data = seed.to_dict()
    data.update(
        {
            "case_id": case_id,
            "subject_revision": subject_revision,
            "revision_status": "matched",
            "revision_provenance_status": "valid",
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
            "source_ref_count_total": len(source_refs),
            "source_ref_count_included": len(source_refs),
            "source_refs_truncated": False,
            "source_binding": observed_defect_seed_source_binding(
                external_refs=source_refs,
                declared_external_refs=source_refs,
            ),
            "attribution_domain": "task_quality",
            "reason": (
                "External evaluation failures share one deterministic factual "
                "failure signature."
            ),
        }
    )
    return {
        "record_id": "observed_defect_seed_{0}".format(seed.seed_id),
        "event_type": "case.observed_defect",
        "component": "evaluation",
        "status": "warning",
        "source_refs": source_refs,
        "data": data,
    }


def observed_defect_seed_source_binding(
    *,
    review_refs: Iterable[str] = (),
    external_refs: Iterable[str] = (),
    declared_review_refs: Iterable[str] = (),
    declared_external_refs: Iterable[str] = (),
    rejected_review_refs: Iterable[str] = (),
    rejected_external_refs: Iterable[str] = (),
    unresolved_review_refs: Iterable[str] = (),
    unresolved_external_refs: Iterable[str] = (),
    historical_incompleteness: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    declared_review = set(_normalized_string_set(declared_review_refs))
    declared_external = set(_normalized_string_set(declared_external_refs))
    raw_review = set(_normalized_string_set(review_refs))
    raw_external = set(_normalized_string_set(external_refs))
    all_refs = sorted(raw_review | raw_external)
    eligible = set(all_refs)
    raw_rejected_review = set(_normalized_string_set(rejected_review_refs))
    raw_rejected_external = set(_normalized_string_set(rejected_external_refs))
    raw_unresolved_review = set(
        _normalized_string_set(unresolved_review_refs)
    )
    raw_unresolved_external = set(
        _normalized_string_set(unresolved_external_refs)
    )
    declared_review.update(
        raw_review | raw_rejected_review | raw_unresolved_review
    )
    declared_external.update(
        raw_external | raw_rejected_external | raw_unresolved_external
    )
    declared = declared_review | declared_external
    rejected = (
        raw_rejected_review | raw_rejected_external
    ) - eligible
    unresolved = (
        raw_unresolved_review | raw_unresolved_external
    ) - eligible - rejected
    unresolved.update(declared - eligible - rejected - unresolved)

    normalized_review = sorted(
        raw_review | (declared_review & eligible)
    )
    normalized_external = sorted(
        raw_external | (declared_external & eligible)
    )
    rejected_review = sorted(raw_rejected_review & rejected)
    rejected_external = sorted(raw_rejected_external & rejected)
    rejected_refs = sorted(rejected)
    unresolved_review = sorted(
        (raw_unresolved_review | (declared_review & unresolved)) & unresolved
    )
    unresolved_external = sorted(
        (raw_unresolved_external | (declared_external & unresolved))
        & unresolved
    )
    unresolved_refs = sorted(unresolved)
    declared_review_refs_output = sorted(declared_review)
    declared_external_refs_output = sorted(declared_external)
    declared_refs = sorted(declared)
    declared_total = len(declared_refs)
    included = len(all_refs)
    unresolved_count = len(unresolved_refs)
    rejected_count = len(rejected_refs)
    if declared_total != included + unresolved_count + rejected_count:
        raise ValueError("source binding current classifications must be disjoint")
    history = _normalize_historical_incompleteness(
        historical_incompleteness
    )
    return {
        "schema": "observed-defect-seed-source-binding/v3",
        "review_refs": normalized_review,
        "review_ref_count": len(normalized_review),
        "external_refs": normalized_external,
        "external_ref_count": len(normalized_external),
        "all_refs": all_refs,
        "all_ref_count": len(all_refs),
        "declared_review_refs": declared_review_refs_output,
        "declared_review_count": len(declared_review_refs_output),
        "declared_external_refs": declared_external_refs_output,
        "declared_external_count": len(declared_external_refs_output),
        "declared_refs": declared_refs,
        "rejected_review_refs": rejected_review,
        "rejected_external_refs": rejected_external,
        "rejected_refs": rejected_refs,
        "unresolved_review_refs": unresolved_review,
        "unresolved_external_refs": unresolved_external,
        "unresolved_refs": unresolved_refs,
        "declared_total": declared_total,
        "included": included,
        "unresolved": unresolved_count,
        "rejected": rejected_count,
        "truncated": False,
        "source_ref_count_included": included,
        "source_refs_truncated": False,
        "historical_incompleteness": history,
    }


def merge_observed_defect_seed_record(
    existing: JsonDict,
    incoming: JsonDict,
) -> None:
    existing_data = (
        existing.get("data") if isinstance(existing.get("data"), dict) else {}
    )
    incoming_data = (
        incoming.get("data") if isinstance(incoming.get("data"), dict) else {}
    )
    existing_signature = existing_data.get("failure_signature")
    incoming_signature = incoming_data.get("failure_signature")
    if (
        existing.get("event_type") != "case.observed_defect"
        or existing_signature != incoming_signature
    ):
        raise ValueError(
            "observed defect seed record ID collision for {0}".format(
                incoming.get("record_id")
            )
        )
    _validate_observed_defect_seed_owner(existing_data, incoming_data)
    merged_data: JsonDict = {}
    list_fields = {
        "evaluation_fact_ids",
        "test_ids",
        "evaluation_run_ids",
        "evaluation_layer_ids",
        "prerequisite_neutralizations",
        "evidence_refs",
    }
    special_fields = list_fields | {
        "cluster_size",
        "source_binding",
        "source_ref_count_total",
        "source_ref_count_included",
        "source_refs_truncated",
    }
    owner_fields = {
        "case_id",
        "subject_revision",
        "revision_status",
        "revision_provenance_status",
    }
    for field in sorted(set(existing_data) | set(incoming_data)):
        if field in special_fields:
            continue
        if field in owner_fields:
            merged_data[field] = copy.deepcopy(existing_data.get(field))
            continue
        values = [
            value
            for value in (existing_data.get(field), incoming_data.get(field))
            if value not in (None, "")
        ]
        if not values:
            continue
        merged_data[field] = copy.deepcopy(
            min(values, key=_canonical_json)
        )
    for field in (
        "evaluation_fact_ids",
        "test_ids",
        "evaluation_run_ids",
        "evaluation_layer_ids",
        "prerequisite_neutralizations",
    ):
        values = [
            item
            for item in [
                *(existing_data.get(field) or []),
                *(incoming_data.get(field) or []),
            ]
        ]
        by_identity = {_canonical_json(item): copy.deepcopy(item) for item in values}
        merged_data[field] = [by_identity[key] for key in sorted(by_identity)]
    existing_binding = _observed_defect_binding_refs(existing)
    incoming_binding = _observed_defect_binding_refs(incoming)
    source_binding = observed_defect_seed_source_binding(
        review_refs=[
            *existing_binding["review_refs"],
            *incoming_binding["review_refs"],
        ],
        external_refs=[
            *existing_binding["external_refs"],
            *incoming_binding["external_refs"],
        ],
        declared_review_refs=[
            *existing_binding["declared_review_refs"],
            *incoming_binding["declared_review_refs"],
        ],
        declared_external_refs=[
            *existing_binding["declared_external_refs"],
            *incoming_binding["declared_external_refs"],
        ],
        rejected_review_refs=[
            *existing_binding["rejected_review_refs"],
            *incoming_binding["rejected_review_refs"],
        ],
        rejected_external_refs=[
            *existing_binding["rejected_external_refs"],
            *incoming_binding["rejected_external_refs"],
        ],
        unresolved_review_refs=[
            *existing_binding["unresolved_review_refs"],
            *incoming_binding["unresolved_review_refs"],
        ],
        unresolved_external_refs=[
            *existing_binding["unresolved_external_refs"],
            *incoming_binding["unresolved_external_refs"],
        ],
        historical_incompleteness=_merge_historical_incompleteness(
            existing_binding["historical_incompleteness"],
            incoming_binding["historical_incompleteness"],
        ),
    )
    evidence_candidates = set(
        _normalized_string_set(
            [
                *(existing_data.get("evidence_refs") or []),
                *(incoming_data.get("evidence_refs") or []),
            ]
        )
    )
    semantic_evidence = evidence_candidates - set(
        source_binding["declared_refs"]
    )
    merged_data["evidence_refs"] = sorted(
        semantic_evidence | set(source_binding["all_refs"])
    )
    source_refs = source_binding["all_refs"]
    merged = {
        "record_id": str(existing.get("record_id") or incoming.get("record_id")),
        "event_type": "case.observed_defect",
        "component": "evaluation",
        "status": "warning",
        "source_refs": source_refs,
        "data": merged_data,
    }
    merged_data["cluster_size"] = len(merged_data["evaluation_fact_ids"])
    merged_data["source_binding"] = source_binding
    merged_data["source_ref_count_total"] = source_binding["declared_total"]
    merged_data["source_ref_count_included"] = source_binding["included"]
    merged_data["source_refs_truncated"] = source_binding["truncated"]
    existing.clear()
    existing.update(merged)


def _observed_defect_binding_refs(record: Mapping[str, Any]) -> JsonDict:
    data = record.get("data") if isinstance(record.get("data"), Mapping) else {}
    binding = (
        data.get("source_binding")
        if isinstance(data.get("source_binding"), Mapping)
        else {}
    )
    schema = binding.get("schema")
    if schema in {
        "observed-defect-seed-source-binding/v2",
        "observed-defect-seed-source-binding/v3",
    }:
        review_refs = list(binding.get("review_refs") or [])
        external_refs = list(binding.get("external_refs") or [])
        unresolved_review = list(binding.get("unresolved_review_refs") or [])
        unresolved_external = list(
            binding.get("unresolved_external_refs") or []
        )
        for ref in binding.get("unresolved_refs") or []:
            if ref not in unresolved_review and ref not in unresolved_external:
                unresolved_review.append(ref)
        rejected_review = list(binding.get("rejected_review_refs") or [])
        rejected_external = list(binding.get("rejected_external_refs") or [])
        for ref in binding.get("rejected_refs") or []:
            if ref not in rejected_review and ref not in rejected_external:
                rejected_review.append(ref)
        declared_review = list(binding.get("declared_review_refs") or [])
        declared_external = list(binding.get("declared_external_refs") or [])
        for ref in binding.get("declared_refs") or []:
            if ref not in declared_review and ref not in declared_external:
                declared_review.append(ref)
        declared_total = int(
            binding.get("declared_total")
            or data.get("source_ref_count_total")
            or len(binding.get("declared_refs") or [])
        )
        included = len(set(review_refs) | set(external_refs))
        rejected_count = len(set(rejected_review) | set(rejected_external))
        unresolved_count = len(
            set(unresolved_review) | set(unresolved_external)
        )
        was_truncated = bool(
            binding.get("truncated")
            or binding.get("source_refs_truncated")
            or data.get("source_refs_truncated")
        )
        history = _normalize_historical_incompleteness(
            binding.get("historical_incompleteness")
        )
        if was_truncated or declared_total > (
            included + rejected_count + unresolved_count
        ):
            historical_unresolved = set(unresolved_review) | set(
                unresolved_external
            )
            history = _merge_historical_incompleteness(
                history,
                {
                    "max_declared_total": declared_total,
                    "ever_truncated": was_truncated,
                    "prior_unresolved_refs": [
                        *unresolved_review,
                        *unresolved_external,
                    ],
                },
            )
            declared_review = [
                ref for ref in declared_review if ref not in historical_unresolved
            ]
            declared_external = [
                ref
                for ref in declared_external
                if ref not in historical_unresolved
            ]
            unresolved_review = []
            unresolved_external = []
        return {
            "review_refs": review_refs,
            "external_refs": external_refs,
            "declared_review_refs": declared_review
            or [*review_refs, *rejected_review, *unresolved_review],
            "declared_external_refs": declared_external
            or [*external_refs, *rejected_external, *unresolved_external],
            "rejected_review_refs": rejected_review,
            "rejected_external_refs": rejected_external,
            "unresolved_review_refs": unresolved_review,
            "unresolved_external_refs": unresolved_external,
            "historical_incompleteness": history,
        }
    if schema == "external-evaluation-seed-binding/v1":
        external_refs = list(binding.get("external_evaluation_refs") or [])
        history = _legacy_historical_incompleteness(
            declared_total=int(data.get("source_ref_count_total") or 0),
            included=len(external_refs),
            unresolved_refs=(),
            truncated=bool(data.get("source_refs_truncated")),
        )
        return {
            "review_refs": [],
            "external_refs": external_refs,
            "declared_review_refs": [],
            "declared_external_refs": external_refs,
            "rejected_review_refs": [],
            "rejected_external_refs": [],
            "unresolved_review_refs": [],
            "unresolved_external_refs": [],
            "historical_incompleteness": history,
        }
    source_refs = list(record.get("source_refs") or [])
    history = _legacy_historical_incompleteness(
        declared_total=int(
            data.get("source_ref_count_total") or len(source_refs)
        ),
        included=len(source_refs),
        unresolved_refs=(),
        truncated=bool(data.get("source_refs_truncated")),
    )
    return {
        "review_refs": source_refs,
        "external_refs": [],
        "declared_review_refs": source_refs,
        "declared_external_refs": [],
        "rejected_review_refs": [],
        "rejected_external_refs": [],
        "unresolved_review_refs": [],
        "unresolved_external_refs": [],
        "historical_incompleteness": history,
    }


def _validate_observed_defect_seed_owner(
    existing_data: Mapping[str, Any],
    incoming_data: Mapping[str, Any],
) -> None:
    for field in (
        "case_id",
        "subject_revision",
        "revision_status",
        "revision_provenance_status",
    ):
        if existing_data.get(field) != incoming_data.get(field):
            raise ValueError(
                "observed defect seed owner {0} mismatch: {1!r} != {2!r}".format(
                    field,
                    existing_data.get(field),
                    incoming_data.get(field),
                )
            )


def _normalized_string_set(values: Iterable[str]) -> List[str]:
    return sorted(
        {
            ref.strip()
            for ref in values
            if isinstance(ref, str) and ref.strip()
        }
    )


def _normalize_historical_incompleteness(
    value: Optional[Mapping[str, Any]],
) -> JsonDict:
    source = value if isinstance(value, Mapping) else {}
    max_declared_total = source.get("max_declared_total")
    if type(max_declared_total) is not int or max_declared_total < 0:
        max_declared_total = 0
    return {
        "max_declared_total": max_declared_total,
        "ever_truncated": source.get("ever_truncated") is True,
        "prior_unresolved_refs": _normalized_string_set(
            source.get("prior_unresolved_refs")
            if isinstance(source.get("prior_unresolved_refs"), list)
            else ()
        ),
    }


def _merge_historical_incompleteness(
    *values: Optional[Mapping[str, Any]],
) -> JsonDict:
    normalized = [
        _normalize_historical_incompleteness(value) for value in values
    ]
    return {
        "max_declared_total": max(
            (item["max_declared_total"] for item in normalized),
            default=0,
        ),
        "ever_truncated": any(
            item["ever_truncated"] for item in normalized
        ),
        "prior_unresolved_refs": _normalized_string_set(
            ref
            for item in normalized
            for ref in item["prior_unresolved_refs"]
        ),
    }


def _legacy_historical_incompleteness(
    *,
    declared_total: int,
    included: int,
    unresolved_refs: Iterable[str],
    truncated: bool,
) -> JsonDict:
    incomplete = truncated or declared_total > included
    return _normalize_historical_incompleteness(
        {
            "max_declared_total": declared_total if incomplete else 0,
            "ever_truncated": truncated,
            "prior_unresolved_refs": (
                list(unresolved_refs) if incomplete else []
            ),
        }
    )


def _external_evaluation_record_id(payload: JsonDict) -> str:
    return "external_evaluation_{0}".format(
        hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]
    )


def _record_evidence_eligible(
    trace: JsonDict, record: JsonDict, records: Iterable[Any]
) -> bool:
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


def trace_execution_revision(
    trace: JsonDict,
) -> Tuple[Optional[str], str]:
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


_trace_execution_revision = trace_execution_revision


def _record_alias_index(records: Iterable[Any]) -> Dict[str, Set[str]]:
    from .trace_eligibility import record_aliases

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
