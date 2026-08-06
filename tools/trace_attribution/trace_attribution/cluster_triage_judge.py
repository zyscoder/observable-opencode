"""Bounded, navigation-only LLM contract for candidate-cluster triage."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Optional, Protocol, Tuple

from .candidate_clustering import CandidateClusterManifest
from .causal_state import DefectState
from .cluster_triage import (
    CandidateClusterTriageDecision,
    CandidateClusterTriageRequest,
    build_candidate_cluster_triage_decision,
)
from .evidence_capsule import CandidateEvidenceCapsule
from .judge_payload import reject_analysis_control_envelopes
from .models import JsonDict, stable_json


CLUSTER_TRIAGE_PAGE_SIZE = 8
CLUSTER_TRIAGE_PAGE_SCHEMA = "candidate-cluster-triage-page/v1"
CLUSTER_TRIAGE_PAGE_RESPONSE_SCHEMA = (
    "candidate-cluster-triage-page-response/v1"
)
CLUSTER_TRIAGE_JUDGMENT_SCHEMA = (
    "candidate-cluster-triage-page-judgment/v1"
)
CLUSTER_TRIAGE_PROMPT_SCHEMA_VERSION = "candidate-cluster-triage-prompt/v1"
CLUSTER_TRIAGE_SYSTEM_PROMPT = """You navigate a bounded directory of trace-grounded candidate clusters.
Select clusters that need full original-candidate expansion. Do not judge, rank, confirm, or publish roots, factors, candidates, or representatives.
Use only the supplied active defect, objective, perspective, cluster facts, and representative evidence capsules.
Unknown or semantically incomplete evidence must be uncertain. Unselected requires explicit trace-grounded mismatch evidence.
Return exactly one JSON object with no markdown."""

_DISPOSITIONS = frozenset({"selected", "unselected", "uncertain"})
_HEX = frozenset("0123456789abcdef")
_PAGE_KEYS = frozenset(
    {
        "schema",
        "case_id",
        "seed_ref",
        "request_identity",
        "manifest_identity",
        "candidate_set_identity",
        "source_selection_identity",
        "eligible_set_identity",
        "partition_identity",
        "defect_fingerprint",
        "page_index",
        "page_count",
        "active_defect",
        "objective",
        "analysis_perspective",
        "clusters",
        "page_identity",
    }
)
_CLUSTER_KEYS = frozenset(
    {
        "cluster_id",
        "cluster_content_identity",
        "grouping_level",
        "grouping_key",
        "grouping_reason",
        "member_count",
        "eligible_member_count",
        "root_eligible_count",
        "member_refs",
        "eligible_candidate_refs",
        "role_distribution",
        "event_type_distribution",
        "component_distribution",
        "source_distribution",
        "action_identity_distribution",
        "obligation_distribution",
        "attribution_only_gap_identities",
        "support_evidence_refs",
        "opposition_evidence_refs",
        "evidence_refs",
        "original_representative_refs",
        "semantic_summary",
        "representative_capsules",
    }
)
_SUMMARY_KEYS = frozenset(
    {"representative_count", "representative_facts"}
)
_SUMMARY_FACT_KEYS = frozenset(
    {
        "candidate_ref",
        "component",
        "event_type",
        "title",
        "status",
        "source",
        "episode_role",
        "semantic_data",
        "downstream_path",
        "evidence_refs",
        "restoration_obligation_ids",
    }
)
_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "page_identity",
        "request_identity",
        "partition_identity",
        "page_index",
        "page_count",
        "decisions",
    }
)
_JUDGMENT_KEYS = frozenset({*_RESPONSE_KEYS, "judgment_identity"})
_DECISION_KEYS = frozenset(
    {
        "cluster_id",
        "disposition",
        "rationale",
        "evidence_refs",
        "mismatch_evidence",
    }
)
_MISMATCH_KEYS = frozenset({"evidence_ref", "mismatch"})
_CAPSULE_BASE_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "candidate_ref",
        "defect_state",
        "candidate",
        "downstream_path",
        "downstream_path_references",
        "causal_path_edges",
        "start_refs",
        "action_group",
        "incoming_edges",
        "outgoing_edges",
        "evidence_references",
        "restoration_obligations",
        "episode_facts",
    }
)
_CAPSULE_EVIDENCE_STATE_KEYS = frozenset(
    {"artifact_hydration", "artifact_evidence_gaps"}
)
_EVALUATION_ONLY_KEYS = frozenset(
    {
        "benchmark",
        "benchmarklabel",
        "benchmarkscore",
        "benchmarkscores",
        "evaluationlabel",
        "goldanswer",
        "goldlabel",
        "goldroot",
        "humanlabel",
        "humanlabels",
        "referenceanswer",
        "referenceanswers",
        "reviewerfact",
        "reviewerfacts",
        "reviewerlabel",
        "reviewerlabels",
        "reviewerscore",
        "priorverdict",
        "priorverdicts",
    }
)
_ATTRIBUTION_VERDICT_KEYS = frozenset(
    {
        "activerolebinding",
        "attributiondecision",
        "attributionresult",
        "attributionverdict",
        "causalrole",
        "factorrole",
        "factorroleverdict",
        "factorverdict",
        "iscausalfactor",
        "isrootcause",
        "priorattribution",
        "priorattributionverdict",
        "priorverdict",
        "priorverdicts",
        "rootcauseverdict",
        "rootverdict",
    }
)
_MAX_FACT_DEPTH = 64
_MAX_FACT_NODES = 20000
_MAX_JSON_STRING_DECODES = 4
_EVALUATION_ONLY_CONCEPTS = (
    ("answer", "key"),
    ("benchmark",),
    ("evaluation", "answer"),
    ("evaluation", "label"),
    ("evaluation", "score"),
    ("expected", "answer"),
    ("expected", "root"),
    ("expected", "roots"),
    ("gold", "answer"),
    ("gold", "label"),
    ("gold", "root"),
    ("ground", "truth"),
    ("human", "label"),
    ("reference", "answer"),
    ("review", "only"),
    ("reviewer",),
    ("rubric",),
    ("score", "explanation"),
    ("scoring",),
)
_ATTRIBUTION_VERDICT_CONCEPTS = (
    ("active", "role", "binding"),
    ("attribution", "decision"),
    ("attribution", "result"),
    ("attribution", "verdict"),
    ("causal", "role"),
    ("factor", "role"),
    ("factor", "verdict"),
    ("is", "causal", "factor"),
    ("is", "root", "cause"),
    ("prior", "attribution"),
    ("prior", "verdict"),
    ("root", "cause", "verdict"),
    ("root", "verdict"),
)


def _digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _require_identity(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValueError("{0} must be a lowercase SHA-256 identity".format(label))
    return value


def _require_text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError("{0} must be a non-empty string".format(label))
    return value


def _exact_mapping(
    value: Any, expected: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    if any(not isinstance(key, str) for key in value):
        raise ValueError("{0} contains a non-string schema key".format(label))
    actual = set(value)
    if actual != expected:
        raise ValueError(
            "{0} schema mismatch (missing={1}, extra={2})".format(
                label, sorted(expected - actual), sorted(actual - expected)
            )
        )
    return value


def _json_array(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TypeError("{0} must be a JSON array".format(label))
    return value


def _string_tuple(
    value: Any,
    label: str,
    *,
    require_json_array: bool = False,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if require_json_array:
        value = _json_array(value, label)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("{0} must be an array".format(label))
    output = tuple(value)
    if any(not isinstance(item, str) or not item for item in output):
        raise ValueError("{0} must contain non-empty strings".format(label))
    if not allow_empty and not output:
        raise ValueError("{0} must not be empty".format(label))
    if len(output) != len(set(output)):
        raise ValueError("{0} must not contain duplicates".format(label))
    return output


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _normalized_key(value: Any) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", str(value)).casefold()
        if character.isalnum()
    )


def _concept_tokens(value: Any) -> Tuple[str, ...]:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return tuple(re.findall(r"[a-z0-9]+", text.casefold()))


def _contains_concept(
    tokens: Tuple[str, ...], concept: Tuple[str, ...]
) -> bool:
    size = len(concept)
    return any(tokens[index : index + size] == concept for index in range(len(tokens)))


def _is_evaluation_or_attribution_key(value: Any) -> bool:
    normalized = _normalized_key(value)
    if normalized in (_EVALUATION_ONLY_KEYS | _ATTRIBUTION_VERDICT_KEYS):
        return True
    tokens = _concept_tokens(value)
    return any(
        _contains_concept(tokens, concept)
        for concept in (
            *_EVALUATION_ONLY_CONCEPTS,
            *_ATTRIBUTION_VERDICT_CONCEPTS,
        )
    )


def _decode_structured_json_string(value: str) -> Any:
    candidate = value
    for _ in range(_MAX_JSON_STRING_DECODES):
        stripped = candidate.strip()
        if not stripped.startswith(("{", "[", '"')):
            return None
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if isinstance(decoded, (Mapping, list)):
            return decoded
        if not isinstance(decoded, str):
            return None
        candidate = decoded
    if candidate.strip().startswith(("{", "[", '"')):
        raise ValueError("cluster triage facts exceed bounded JSON encoding depth")
    return None


def _walk_fact_payload(value: Any, *, sanitize: bool) -> Any:
    reject_analysis_control_envelopes(value)
    omitted = object()
    ancestors = set()
    node_count = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if depth > _MAX_FACT_DEPTH or node_count > _MAX_FACT_NODES:
            raise ValueError("cluster triage facts exceed bounded depth or complexity")
        if isinstance(item, str):
            decoded = _decode_structured_json_string(item)
            if decoded is None:
                return item
            cleaned = visit(decoded, depth + 1)
            if cleaned is omitted:
                return omitted
            return stable_json(cleaned) if sanitize else item
        if not isinstance(item, (Mapping, list, tuple)):
            return item
        item_id = id(item)
        if item_id in ancestors:
            raise ValueError("cluster triage facts contain recursive containers")
        ancestors.add(item_id)
        try:
            if isinstance(item, Mapping):
                output = {}
                for raw_key, child in item.items():
                    key = str(raw_key)
                    if _is_evaluation_or_attribution_key(key):
                        if sanitize:
                            continue
                        raise ValueError(
                            "cluster triage facts contain evaluation-only or prior-verdict data"
                        )
                    cleaned = visit(child, depth + 1)
                    if cleaned is not omitted:
                        output[key] = cleaned
                return output
            output = []
            for child in item:
                cleaned = visit(child, depth + 1)
                if cleaned is not omitted:
                    output.append(cleaned)
            return output
        finally:
            ancestors.remove(item_id)

    cleaned = visit(value, 0)
    return {} if cleaned is omitted else cleaned


def _sanitize_fact_payload(value: Any) -> Any:
    return _walk_fact_payload(value, sanitize=True)


def _validate_no_forbidden_facts(value: Any) -> None:
    _walk_fact_payload(value, sanitize=False)


def _count_mapping(value: Any, label: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str)
        or not key
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for key, count in value.items()
    ):
        raise ValueError("{0} must be a string-to-count object".format(label))
    return MappingProxyType(dict(sorted(value.items())))


def _validate_capsule(value: Any, *, expected_ref: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("representative capsule must be an object")
    if not _CAPSULE_BASE_REQUIRED_KEYS.issubset(set(value)):
        raise ValueError("representative capsule schema is incomplete")
    evidence_states = _CAPSULE_EVIDENCE_STATE_KEYS.intersection(value)
    if len(evidence_states) != 1:
        raise ValueError(
            "representative capsule must contain exactly one artifact evidence state"
        )
    if "artifact_evidence_gaps" in evidence_states:
        gaps = value["artifact_evidence_gaps"]
        if not isinstance(gaps, (list, tuple)) or not gaps or any(
            not isinstance(item, Mapping)
            or not str(item.get("artifact_id") or "")
            or str(item.get("status") or "")
            not in {
                "missing",
                "truncated",
                "integrity_failure",
                "owner_ineligible",
            }
            for item in gaps
        ):
            raise ValueError("representative capsule artifact evidence gaps are invalid")
    if value.get("candidate_ref") != expected_ref:
        raise ValueError("representative capsule candidate identity mismatch")
    candidate = value.get("candidate")
    if not isinstance(candidate, Mapping) or candidate.get("ref") != expected_ref:
        raise ValueError("representative capsule facts do not bind candidate")
    _validate_no_forbidden_facts(value)
    return _freeze(value)


def _canonical_summary(value: Any) -> Mapping[str, Any]:
    summary = _exact_mapping(value, _SUMMARY_KEYS, "cluster semantic summary")
    count = summary["representative_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("cluster semantic representative_count is invalid")
    facts_raw = summary["representative_facts"]
    if not isinstance(facts_raw, (list, tuple)):
        raise TypeError("cluster semantic representative_facts must be an array")
    facts = []
    for raw in facts_raw:
        fact = _exact_mapping(raw, _SUMMARY_FACT_KEYS, "cluster semantic fact")
        candidate_ref = _require_text(
            fact["candidate_ref"], "cluster semantic candidate ref"
        )
        for key in (
            "component",
            "event_type",
            "title",
            "status",
            "source",
            "episode_role",
        ):
            if not isinstance(fact[key], str):
                raise ValueError("cluster semantic {0} must be a string".format(key))
        if not isinstance(fact["semantic_data"], Mapping):
            raise TypeError("cluster semantic data must be an object")
        downstream = _string_tuple(
            fact["downstream_path"], "cluster semantic downstream path"
        )
        evidence = _string_tuple(
            fact["evidence_refs"], "cluster semantic evidence refs"
        )
        obligations = _string_tuple(
            fact["restoration_obligation_ids"],
            "cluster semantic obligation IDs",
        )
        facts.append(
            {
                **{key: fact[key] for key in _SUMMARY_FACT_KEYS},
                "candidate_ref": candidate_ref,
                "semantic_data": _thaw(_freeze(fact["semantic_data"])),
                "downstream_path": list(downstream),
                "evidence_refs": list(evidence),
                "restoration_obligation_ids": list(obligations),
            }
        )
    if len(facts) != count:
        raise ValueError("cluster semantic representative_count is stale")
    if len({fact["candidate_ref"] for fact in facts}) != len(facts):
        raise ValueError("cluster semantic facts repeat a representative")
    _validate_no_forbidden_facts(facts)
    return _freeze(
        {
            "representative_count": count,
            "representative_facts": sorted(
                facts, key=lambda fact: fact["candidate_ref"]
            ),
        }
    )


def _canonical_cluster(value: Any) -> Mapping[str, Any]:
    item = _exact_mapping(value, _CLUSTER_KEYS, "cluster triage page entry")
    cluster_id = _require_text(item["cluster_id"], "cluster ID")
    if not cluster_id.startswith("cluster:"):
        raise ValueError("cluster ID must be a cluster ref")
    content_identity = _require_identity(
        item["cluster_content_identity"], "cluster content identity"
    )
    for key in ("grouping_level", "grouping_key", "grouping_reason"):
        _require_text(item[key], "cluster {0}".format(key))
    for key in ("member_count", "eligible_member_count", "root_eligible_count"):
        count = item[key]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("cluster {0} is invalid".format(key))
    member_refs = _string_tuple(item["member_refs"], "cluster member refs")
    eligible_refs = _string_tuple(
        item["eligible_candidate_refs"],
        "cluster eligible refs",
        allow_empty=False,
    )
    if item["member_count"] != len(member_refs):
        raise ValueError("cluster member_count is stale")
    if item["eligible_member_count"] != len(eligible_refs):
        raise ValueError("cluster eligible_member_count is stale")
    if not set(eligible_refs).issubset(member_refs):
        raise ValueError("cluster eligible refs must be members")
    distributions = {
        key: _count_mapping(item[key], "cluster {0}".format(key))
        for key in (
            "role_distribution",
            "event_type_distribution",
            "component_distribution",
            "source_distribution",
            "action_identity_distribution",
            "obligation_distribution",
        )
    }
    list_fields = {
        key: _string_tuple(item[key], "cluster {0}".format(key))
        for key in (
            "attribution_only_gap_identities",
            "support_evidence_refs",
            "opposition_evidence_refs",
            "evidence_refs",
            "original_representative_refs",
        )
    }
    if not list_fields["evidence_refs"]:
        raise ValueError("cluster evidence refs must not be empty")
    representatives = list_fields["original_representative_refs"]
    if not representatives or not set(representatives).issubset(member_refs):
        raise ValueError("cluster representative refs must be original members")
    summary = _canonical_summary(item["semantic_summary"])
    capsules_raw = item["representative_capsules"]
    if not isinstance(capsules_raw, (list, tuple)) or not capsules_raw:
        raise ValueError("cluster representative capsules must not be empty")
    capsules = tuple(
        _validate_capsule(raw, expected_ref=str(raw.get("candidate_ref") or ""))
        for raw in capsules_raw
    )
    capsule_refs = tuple(capsule["candidate_ref"] for capsule in capsules)
    if (
        len(capsule_refs) != len(set(capsule_refs))
        or not set(capsule_refs).issubset(representatives)
        or set(capsule_refs)
        != {
            fact["candidate_ref"]
            for fact in summary["representative_facts"]
        }
    ):
        raise ValueError("cluster representative capsule bindings are invalid")
    canonical = {
        "cluster_id": cluster_id,
        "cluster_content_identity": content_identity,
        "grouping_level": item["grouping_level"],
        "grouping_key": item["grouping_key"],
        "grouping_reason": item["grouping_reason"],
        "member_count": item["member_count"],
        "eligible_member_count": item["eligible_member_count"],
        "root_eligible_count": item["root_eligible_count"],
        "member_refs": list(member_refs),
        "eligible_candidate_refs": list(eligible_refs),
        **{key: dict(value) for key, value in distributions.items()},
        **{key: list(value) for key, value in list_fields.items()},
        "semantic_summary": _thaw(summary),
        "representative_capsules": [
            _thaw(capsule)
            for capsule in sorted(capsules, key=lambda value: value["candidate_ref"])
        ],
    }
    _validate_no_forbidden_facts(canonical)
    return _freeze(canonical)


def _page_unsigned(
    *,
    case_id: str,
    seed_ref: str,
    request_identity: str,
    manifest_identity: str,
    candidate_set_identity: str,
    source_selection_identity: str,
    eligible_set_identity: str,
    partition_identity: str,
    defect_fingerprint: str,
    page_index: int,
    page_count: int,
    active_defect: DefectState,
    objective: str,
    analysis_perspective: str,
    clusters: Sequence[Mapping[str, Any]],
) -> JsonDict:
    return {
        "schema": CLUSTER_TRIAGE_PAGE_SCHEMA,
        "case_id": case_id,
        "seed_ref": seed_ref,
        "request_identity": request_identity,
        "manifest_identity": manifest_identity,
        "candidate_set_identity": candidate_set_identity,
        "source_selection_identity": source_selection_identity,
        "eligible_set_identity": eligible_set_identity,
        "partition_identity": partition_identity,
        "defect_fingerprint": defect_fingerprint,
        "page_index": page_index,
        "page_count": page_count,
        "active_defect": active_defect.to_dict(),
        "objective": objective,
        "analysis_perspective": analysis_perspective,
        "clusters": [_thaw(cluster) for cluster in clusters],
    }


@dataclass(frozen=True)
class ClusterTriagePageRequest:
    schema: str
    case_id: str
    seed_ref: str
    request_identity: str
    manifest_identity: str
    candidate_set_identity: str
    source_selection_identity: str
    eligible_set_identity: str
    partition_identity: str
    defect_fingerprint: str
    page_index: int
    page_count: int
    active_defect: DefectState
    objective: str
    analysis_perspective: str
    clusters: Tuple[Mapping[str, Any], ...]
    page_identity: str

    def __post_init__(self) -> None:
        if self.schema != CLUSTER_TRIAGE_PAGE_SCHEMA:
            raise ValueError("cluster triage page schema is invalid")
        _require_text(self.case_id, "cluster triage case_id", allow_empty=True)
        _require_text(self.seed_ref, "cluster triage seed_ref")
        for value, label in (
            (self.request_identity, "request"),
            (self.manifest_identity, "manifest"),
            (self.candidate_set_identity, "candidate set"),
            (self.source_selection_identity, "source selection"),
            (self.eligible_set_identity, "eligible set"),
            (self.partition_identity, "partition"),
        ):
            _require_identity(value, "cluster triage {0} identity".format(label))
        _require_text(self.defect_fingerprint, "cluster triage defect fingerprint")
        if self.active_defect.fingerprint != self.defect_fingerprint:
            raise ValueError("cluster triage active defect binding is stale")
        if (
            isinstance(self.page_index, bool)
            or not isinstance(self.page_index, int)
            or self.page_index < 0
            or isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count < 1
            or self.page_index >= self.page_count
        ):
            raise ValueError("cluster triage page bounds are invalid")
        _require_text(self.objective, "cluster triage objective")
        _require_text(
            self.analysis_perspective, "cluster triage analysis perspective"
        )
        clusters = tuple(_canonical_cluster(value) for value in self.clusters)
        if not clusters or len(clusters) > CLUSTER_TRIAGE_PAGE_SIZE:
            raise ValueError("cluster triage page size is invalid")
        cluster_ids = tuple(value["cluster_id"] for value in clusters)
        if cluster_ids != tuple(sorted(cluster_ids)) or len(cluster_ids) != len(
            set(cluster_ids)
        ):
            raise ValueError("cluster triage page clusters must be canonical")
        unsigned = _page_unsigned(
            case_id=self.case_id,
            seed_ref=self.seed_ref,
            request_identity=self.request_identity,
            manifest_identity=self.manifest_identity,
            candidate_set_identity=self.candidate_set_identity,
            source_selection_identity=self.source_selection_identity,
            eligible_set_identity=self.eligible_set_identity,
            partition_identity=self.partition_identity,
            defect_fingerprint=self.defect_fingerprint,
            page_index=self.page_index,
            page_count=self.page_count,
            active_defect=self.active_defect,
            objective=self.objective,
            analysis_perspective=self.analysis_perspective,
            clusters=clusters,
        )
        if self.page_identity != _digest(
            {"schema": "candidate-cluster-triage-page-identity/v1", "facts": unsigned}
        ):
            raise ValueError("cluster triage page identity does not match facts")
        object.__setattr__(self, "clusters", clusters)

    @property
    def cluster_ids(self) -> Tuple[str, ...]:
        return tuple(value["cluster_id"] for value in self.clusters)

    def evidence_refs_for(self, cluster_id: str) -> Tuple[str, ...]:
        for value in self.clusters:
            if value["cluster_id"] == cluster_id:
                return tuple(value["evidence_refs"])
        raise KeyError(cluster_id)

    def to_dict(self) -> JsonDict:
        value = _page_unsigned(
            case_id=self.case_id,
            seed_ref=self.seed_ref,
            request_identity=self.request_identity,
            manifest_identity=self.manifest_identity,
            candidate_set_identity=self.candidate_set_identity,
            source_selection_identity=self.source_selection_identity,
            eligible_set_identity=self.eligible_set_identity,
            partition_identity=self.partition_identity,
            defect_fingerprint=self.defect_fingerprint,
            page_index=self.page_index,
            page_count=self.page_count,
            active_defect=self.active_defect,
            objective=self.objective,
            analysis_perspective=self.analysis_perspective,
            clusters=self.clusters,
        )
        value["page_identity"] = self.page_identity
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterTriagePageRequest":
        payload = _exact_mapping(value, _PAGE_KEYS, "cluster triage page")
        clusters = _json_array(payload["clusters"], "cluster triage page clusters")
        return cls(
            schema=payload["schema"],
            case_id=payload["case_id"],
            seed_ref=payload["seed_ref"],
            request_identity=payload["request_identity"],
            manifest_identity=payload["manifest_identity"],
            candidate_set_identity=payload["candidate_set_identity"],
            source_selection_identity=payload["source_selection_identity"],
            eligible_set_identity=payload["eligible_set_identity"],
            partition_identity=payload["partition_identity"],
            defect_fingerprint=payload["defect_fingerprint"],
            page_index=payload["page_index"],
            page_count=payload["page_count"],
            active_defect=DefectState.from_dict(payload["active_defect"]),
            objective=payload["objective"],
            analysis_perspective=payload["analysis_perspective"],
            clusters=tuple(clusters),
            page_identity=payload["page_identity"],
        )


def _semantic_summary(capsule_payloads: Sequence[Mapping[str, Any]]) -> JsonDict:
    facts = []
    for payload in capsule_payloads:
        candidate = payload["candidate"]
        node = candidate.get("node")
        node = node if isinstance(node, Mapping) else {}
        episode = payload.get("episode_facts")
        episode = episode if isinstance(episode, Mapping) else {}
        evidence_refs = sorted(
            {
                str(value.get("resolved_ref") or value.get("canonical_ref") or "")
                for value in payload.get("evidence_references") or ()
                if isinstance(value, Mapping)
            }
            - {""}
        )
        obligations = sorted(
            {
                str(value.get("obligation_id") or "")
                for value in payload.get("restoration_obligations") or ()
                if isinstance(value, Mapping)
            }
            - {""}
        )
        semantic_data = node.get("data")
        semantic_data = semantic_data if isinstance(semantic_data, Mapping) else {}
        facts.append(
            {
                "candidate_ref": payload["candidate_ref"],
                "component": str(node.get("component") or ""),
                "event_type": str(node.get("event_type") or ""),
                "title": str(node.get("title") or ""),
                "status": str(node.get("status") or ""),
                "source": str(candidate.get("source") or ""),
                "episode_role": str(episode.get("episode_role") or "other"),
                "semantic_data": _thaw(_freeze(semantic_data)),
                "downstream_path": list(payload.get("downstream_path") or ()),
                "evidence_refs": evidence_refs,
                "restoration_obligation_ids": obligations,
            }
        )
    return {
        "representative_count": len(facts),
        "representative_facts": sorted(facts, key=lambda value: value["candidate_ref"]),
    }


def build_cluster_triage_page_requests(
    *,
    request: CandidateClusterTriageRequest,
    manifest: CandidateClusterManifest,
    eligible_capsules: Sequence[CandidateEvidenceCapsule],
    active_defect: DefectState,
    objective: str,
    analysis_perspective: str,
) -> Tuple[ClusterTriagePageRequest, ...]:
    """Build deterministic Judge pages from the Stage A zero-loss directory."""
    if not isinstance(request, CandidateClusterTriageRequest):
        raise TypeError("cluster triage request must be a Stage A request")
    if not isinstance(manifest, CandidateClusterManifest):
        raise TypeError("cluster triage manifest is invalid")
    if not isinstance(active_defect, DefectState):
        raise TypeError("cluster triage active defect is invalid")
    if (
        request.manifest_identity != manifest.manifest_identity
        or request.candidate_set_identity != manifest.candidate_set_identity
        or request.source_selection_identity != manifest.source_selection_identity
        or request.case_id != manifest.case_id
        or request.seed_ref != manifest.seed_ref
        or request.defect_fingerprint != manifest.defect_fingerprint
        or request.defect_fingerprint != active_defect.fingerprint
    ):
        raise ValueError("cluster triage Stage A identity binding is stale")
    _require_text(objective, "cluster triage objective")
    _require_text(analysis_perspective, "cluster triage analysis perspective")
    if isinstance(eligible_capsules, (str, bytes)) or not isinstance(
        eligible_capsules, Sequence
    ):
        raise TypeError("cluster triage capsules must be an ordered sequence")
    capsules_by_ref: Dict[str, CandidateEvidenceCapsule] = {}
    for capsule in eligible_capsules:
        if not isinstance(capsule, CandidateEvidenceCapsule):
            raise TypeError("cluster triage capsule is invalid")
        if capsule.candidate_ref in capsules_by_ref:
            raise ValueError("cluster triage capsules repeat a candidate")
        if capsule.defect_state.fingerprint != active_defect.fingerprint:
            raise ValueError("cluster triage capsule defect binding is stale")
        capsules_by_ref[capsule.candidate_ref] = capsule
    if set(capsules_by_ref) != set(request.eligible_candidate_refs):
        raise ValueError(
            "cluster triage capsules must cover every eligible candidate exactly once"
        )
    clusters_by_id = {cluster.cluster_id: cluster for cluster in manifest.clusters}
    entries = []
    for directory in request.cluster_directory:
        cluster_id = directory["cluster_id"]
        cluster = clusters_by_id.get(cluster_id)
        if cluster is None:
            raise ValueError("cluster triage directory contains a foreign cluster")
        canonical_representatives = tuple(
            sorted(
                {
                    ref
                    for ref in directory["representatives"].values()
                    if ref is not None and ref in capsules_by_ref
                }
            )
        )
        if not canonical_representatives:
            canonical_representatives = (
                tuple(directory["eligible_candidate_refs"])[0],
            )
        capsule_payloads = []
        for ref in canonical_representatives:
            raw = capsules_by_ref[ref].judge_dict()
            sanitized = _sanitize_fact_payload(raw)
            if not isinstance(sanitized, Mapping):
                raise ValueError("cluster triage representative capsule was erased")
            capsule_payloads.append(sanitized)
        evidence_refs = tuple(directory["evidence_refs"])
        entries.append(
            _canonical_cluster(
                {
                    "cluster_id": cluster_id,
                    "cluster_content_identity": directory[
                        "cluster_content_identity"
                    ],
                    "grouping_level": cluster.grouping_level,
                    "grouping_key": cluster.grouping_key,
                    "grouping_reason": cluster.grouping_reason,
                    "member_count": len(cluster.member_refs),
                    "eligible_member_count": len(
                        directory["eligible_candidate_refs"]
                    ),
                    "root_eligible_count": len(cluster.root_eligible_refs),
                    "member_refs": list(cluster.member_refs),
                    "eligible_candidate_refs": list(
                        directory["eligible_candidate_refs"]
                    ),
                    "role_distribution": dict(cluster.role_distribution),
                    "event_type_distribution": dict(
                        cluster.event_type_distribution
                    ),
                    "component_distribution": dict(
                        cluster.component_distribution
                    ),
                    "source_distribution": dict(cluster.source_distribution),
                    "action_identity_distribution": dict(
                        cluster.action_identity_distribution
                    ),
                    "obligation_distribution": dict(
                        cluster.obligation_distribution
                    ),
                    "attribution_only_gap_identities": list(
                        cluster.attribution_only_gap_identities
                    ),
                    "support_evidence_refs": list(cluster.support_evidence_refs),
                    "opposition_evidence_refs": list(
                        cluster.opposition_evidence_refs
                    ),
                    "evidence_refs": list(evidence_refs),
                    "original_representative_refs": list(
                        canonical_representatives
                    ),
                    "semantic_summary": _semantic_summary(capsule_payloads),
                    "representative_capsules": capsule_payloads,
                }
            )
        )
    entries = sorted(entries, key=lambda value: value["cluster_id"])
    page_count = (len(entries) + CLUSTER_TRIAGE_PAGE_SIZE - 1) // CLUSTER_TRIAGE_PAGE_SIZE
    if page_count < 1:
        raise ValueError("cluster triage requires at least one eligible cluster")
    pages = []
    for page_index in range(page_count):
        page_clusters = tuple(
            entries[
                page_index
                * CLUSTER_TRIAGE_PAGE_SIZE : (page_index + 1)
                * CLUSTER_TRIAGE_PAGE_SIZE
            ]
        )
        unsigned = _page_unsigned(
            case_id=request.case_id,
            seed_ref=request.seed_ref,
            request_identity=request.request_identity,
            manifest_identity=request.manifest_identity,
            candidate_set_identity=request.candidate_set_identity,
            source_selection_identity=request.source_selection_identity,
            eligible_set_identity=request.eligible_set_identity,
            partition_identity=request.partition_identity,
            defect_fingerprint=request.defect_fingerprint,
            page_index=page_index,
            page_count=page_count,
            active_defect=active_defect,
            objective=objective,
            analysis_perspective=analysis_perspective,
            clusters=page_clusters,
        )
        pages.append(
            ClusterTriagePageRequest(
                **{
                    **unsigned,
                    "active_defect": active_defect,
                },
                page_identity=_digest(
                    {
                        "schema": "candidate-cluster-triage-page-identity/v1",
                        "facts": unsigned,
                    }
                ),
            )
        )
    return tuple(pages)


def build_cluster_triage_prompt(page: ClusterTriagePageRequest) -> str:
    if not isinstance(page, ClusterTriagePageRequest):
        raise TypeError("cluster triage prompt requires a page request")
    return stable_json(
        {
            "request": page.to_dict(),
            "rules": [
                "Return exactly one decision for every supplied cluster.",
                "selected means expand all eligible original candidates in the cluster.",
                "uncertain means evidence is unknown or semantically incomplete and must also be expanded.",
                "unselected requires a non-empty rationale plus explicit trace-grounded mismatch_evidence.",
                "Use only evidence_refs supplied for that same cluster.",
                "Cluster IDs and representatives are navigation aids, never causal conclusions.",
            ],
            "required_json_schema": {
                "schema": CLUSTER_TRIAGE_PAGE_RESPONSE_SCHEMA,
                "page_identity": page.page_identity,
                "request_identity": page.request_identity,
                "partition_identity": page.partition_identity,
                "page_index": page.page_index,
                "page_count": page.page_count,
                "decisions": [
                    {
                        "cluster_id": "one exact supplied cluster_id",
                        "disposition": "selected|unselected|uncertain",
                        "rationale": "non-empty trace-grounded explanation",
                        "evidence_refs": ["one or more supplied cluster evidence refs"],
                        "mismatch_evidence": [
                            {
                                "evidence_ref": "supplied cluster evidence ref",
                                "mismatch": "non-empty semantic mismatch",
                            }
                        ],
                    }
                ],
            },
        }
    )


def _canonical_decision(
    value: Any,
    *,
    allowed_cluster_ids: Optional[set[str]] = None,
    evidence_by_cluster: Optional[Mapping[str, set[str]]] = None,
) -> Mapping[str, Any]:
    item = _exact_mapping(value, _DECISION_KEYS, "cluster triage decision")
    cluster_id = _require_text(item["cluster_id"], "cluster triage decision ID")
    if allowed_cluster_ids is not None and cluster_id not in allowed_cluster_ids:
        raise ValueError("cluster triage decision contains a foreign cluster")
    disposition = item["disposition"]
    if disposition not in _DISPOSITIONS:
        raise ValueError("cluster triage disposition is invalid")
    rationale = _require_text(item["rationale"], "cluster triage rationale")
    _validate_no_forbidden_facts(rationale)
    evidence_refs = tuple(
        sorted(
            _string_tuple(
                item["evidence_refs"],
                "cluster triage evidence refs",
                allow_empty=False,
            )
        )
    )
    allowed_evidence = (
        evidence_by_cluster.get(cluster_id, set())
        if evidence_by_cluster is not None
        else None
    )
    if allowed_evidence is not None and not set(evidence_refs).issubset(
        allowed_evidence
    ):
        raise ValueError("cluster triage decision evidence is foreign")
    mismatch_raw = item["mismatch_evidence"]
    if not isinstance(mismatch_raw, (list, tuple)):
        raise TypeError("cluster triage mismatch evidence must be an array")
    mismatches = []
    for raw in mismatch_raw:
        mismatch = _exact_mapping(raw, _MISMATCH_KEYS, "cluster mismatch evidence")
        evidence_ref = _require_text(
            mismatch["evidence_ref"], "cluster mismatch evidence ref"
        )
        detail = _require_text(mismatch["mismatch"], "cluster mismatch detail")
        _validate_no_forbidden_facts(detail)
        if allowed_evidence is not None and evidence_ref not in allowed_evidence:
            raise ValueError("cluster mismatch evidence is foreign")
        if evidence_ref not in evidence_refs:
            raise ValueError("cluster mismatch evidence must be cited by the decision")
        mismatches.append({"evidence_ref": evidence_ref, "mismatch": detail})
    if len({value["evidence_ref"] for value in mismatches}) != len(mismatches):
        raise ValueError("cluster mismatch evidence contains duplicates")
    if disposition == "unselected" and not mismatches:
        raise ValueError("unselected cluster requires explicit mismatch evidence")
    if disposition != "unselected" and mismatches:
        raise ValueError("only unselected clusters may contain mismatch evidence")
    return _freeze(
        {
            "cluster_id": cluster_id,
            "disposition": disposition,
            "rationale": rationale,
            "evidence_refs": list(evidence_refs),
            "mismatch_evidence": sorted(
                mismatches, key=lambda value: value["evidence_ref"]
            ),
        }
    )


def _judgment_unsigned(
    *,
    page_identity: str,
    request_identity: str,
    partition_identity: str,
    page_index: int,
    page_count: int,
    decisions: Sequence[Mapping[str, Any]],
) -> JsonDict:
    return {
        "schema": CLUSTER_TRIAGE_JUDGMENT_SCHEMA,
        "page_identity": page_identity,
        "request_identity": request_identity,
        "partition_identity": partition_identity,
        "page_index": page_index,
        "page_count": page_count,
        "decisions": [_thaw(value) for value in decisions],
    }


@dataclass(frozen=True)
class ClusterTriageJudgment:
    schema: str
    page_identity: str
    request_identity: str
    partition_identity: str
    page_index: int
    page_count: int
    decisions: Tuple[Mapping[str, Any], ...]
    judgment_identity: str

    def __post_init__(self) -> None:
        if self.schema != CLUSTER_TRIAGE_JUDGMENT_SCHEMA:
            raise ValueError("cluster triage judgment schema is invalid")
        for value, label in (
            (self.page_identity, "page"),
            (self.request_identity, "request"),
            (self.partition_identity, "partition"),
        ):
            _require_identity(value, "cluster triage judgment {0} identity".format(label))
        if (
            isinstance(self.page_index, bool)
            or not isinstance(self.page_index, int)
            or self.page_index < 0
            or isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count < 1
            or self.page_index >= self.page_count
        ):
            raise ValueError("cluster triage judgment page bounds are invalid")
        decisions = tuple(_canonical_decision(value) for value in self.decisions)
        cluster_ids = tuple(value["cluster_id"] for value in decisions)
        if not decisions or len(cluster_ids) != len(set(cluster_ids)):
            raise ValueError("cluster triage judgment repeats a cluster")
        decisions = tuple(sorted(decisions, key=lambda value: value["cluster_id"]))
        unsigned = _judgment_unsigned(
            page_identity=self.page_identity,
            request_identity=self.request_identity,
            partition_identity=self.partition_identity,
            page_index=self.page_index,
            page_count=self.page_count,
            decisions=decisions,
        )
        if self.judgment_identity != _digest(
            {
                "schema": "candidate-cluster-triage-judgment-identity/v1",
                "facts": unsigned,
            }
        ):
            raise ValueError("cluster triage judgment identity does not match facts")
        object.__setattr__(self, "decisions", decisions)

    def to_dict(self) -> JsonDict:
        value = _judgment_unsigned(
            page_identity=self.page_identity,
            request_identity=self.request_identity,
            partition_identity=self.partition_identity,
            page_index=self.page_index,
            page_count=self.page_count,
            decisions=self.decisions,
        )
        value["judgment_identity"] = self.judgment_identity
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "ClusterTriageJudgment":
        payload = _exact_mapping(value, _JUDGMENT_KEYS, "cluster triage judgment")
        decisions = _json_array(
            payload["decisions"], "cluster triage judgment decisions"
        )
        return cls(
            schema=payload["schema"],
            page_identity=payload["page_identity"],
            request_identity=payload["request_identity"],
            partition_identity=payload["partition_identity"],
            page_index=payload["page_index"],
            page_count=payload["page_count"],
            decisions=tuple(decisions),
            judgment_identity=payload["judgment_identity"],
        )


def parse_cluster_triage_judgment(
    value: Any, *, page: ClusterTriagePageRequest
) -> ClusterTriageJudgment:
    if not isinstance(page, ClusterTriagePageRequest):
        raise TypeError("cluster triage parser requires a page")
    payload = _exact_mapping(value, _RESPONSE_KEYS, "cluster triage response")
    if payload["schema"] != CLUSTER_TRIAGE_PAGE_RESPONSE_SCHEMA:
        raise ValueError("cluster triage response schema is invalid")
    expected_bindings = {
        "page_identity": page.page_identity,
        "request_identity": page.request_identity,
        "partition_identity": page.partition_identity,
        "page_index": page.page_index,
        "page_count": page.page_count,
    }
    if any(payload[key] != expected for key, expected in expected_bindings.items()):
        raise ValueError("cluster triage response binding is stale")
    raw_decisions = _json_array(
        payload["decisions"], "cluster triage response decisions"
    )
    allowed = set(page.cluster_ids)
    evidence_by_cluster = {
        cluster_id: set(page.evidence_refs_for(cluster_id))
        for cluster_id in page.cluster_ids
    }
    decisions = tuple(
        _canonical_decision(
            raw,
            allowed_cluster_ids=allowed,
            evidence_by_cluster=evidence_by_cluster,
        )
        for raw in raw_decisions
    )
    cluster_ids = tuple(value["cluster_id"] for value in decisions)
    if len(cluster_ids) != len(set(cluster_ids)):
        raise ValueError("cluster triage response contains a duplicate cluster")
    if set(cluster_ids) != allowed:
        raise ValueError("cluster triage response must cover every cluster exactly once")
    decisions = tuple(sorted(decisions, key=lambda item: item["cluster_id"]))
    unsigned = _judgment_unsigned(
        page_identity=page.page_identity,
        request_identity=page.request_identity,
        partition_identity=page.partition_identity,
        page_index=page.page_index,
        page_count=page.page_count,
        decisions=decisions,
    )
    return ClusterTriageJudgment(
        **unsigned,
        judgment_identity=_digest(
            {
                "schema": "candidate-cluster-triage-judgment-identity/v1",
                "facts": unsigned,
            }
        ),
    )


def merge_cluster_triage_judgments(
    *,
    request: CandidateClusterTriageRequest,
    manifest: CandidateClusterManifest,
    eligible_capsules: Sequence[CandidateEvidenceCapsule],
    active_defect: DefectState,
    objective: str,
    analysis_perspective: str,
    pages: Sequence[ClusterTriagePageRequest],
    judgments: Sequence[ClusterTriageJudgment],
) -> CandidateClusterTriageDecision:
    """Merge only a complete, exactly bound page set into a Stage A decision."""
    if not isinstance(request, CandidateClusterTriageRequest):
        raise TypeError("cluster triage merge request is invalid")
    canonical_pages = build_cluster_triage_page_requests(
        request=request,
        manifest=manifest,
        eligible_capsules=eligible_capsules,
        active_defect=active_defect,
        objective=objective,
        analysis_perspective=analysis_perspective,
    )
    canonical_by_index = {
        page.page_index: page for page in canonical_pages
    }
    if not pages or not judgments or len(judgments) < len(pages):
        raise ValueError("cluster triage merge requires a complete page set")
    if len(judgments) > len(pages):
        raise ValueError(
            "cluster triage merge contains a duplicate or foreign judgment page"
        )
    page_by_index: Dict[int, ClusterTriagePageRequest] = {}
    all_cluster_ids = []
    for page in pages:
        if not isinstance(page, ClusterTriagePageRequest):
            raise TypeError("cluster triage merge page is invalid")
        if page.page_index in page_by_index:
            raise ValueError("cluster triage merge contains a duplicate page")
        canonical_page = canonical_by_index.get(page.page_index)
        if canonical_page is None or page.to_dict() != canonical_page.to_dict():
            raise ValueError(
                "cluster triage merge page does not match the authoritative canonical plan"
            )
        if (
            page.request_identity != request.request_identity
            or page.partition_identity != request.partition_identity
            or page.manifest_identity != request.manifest_identity
            or page.candidate_set_identity != request.candidate_set_identity
            or page.source_selection_identity != request.source_selection_identity
            or page.eligible_set_identity != request.eligible_set_identity
        ):
            raise ValueError("cluster triage merge page request is stale")
        page_by_index[page.page_index] = page
        all_cluster_ids.extend(page.cluster_ids)
    expected_page_count = len(canonical_pages)
    if set(page_by_index) != set(range(expected_page_count)) or any(
        page.page_count != expected_page_count for page in page_by_index.values()
    ):
        raise ValueError("cluster triage merge page set is incomplete")
    if (
        len(all_cluster_ids) != len(set(all_cluster_ids))
        or set(all_cluster_ids) != set(request.cluster_ids)
    ):
        raise ValueError("cluster triage merge cluster coverage is incomplete")
    judgment_by_index: Dict[int, ClusterTriageJudgment] = {}
    for judgment in judgments:
        if not isinstance(judgment, ClusterTriageJudgment):
            raise TypeError("cluster triage merge judgment is invalid")
        if judgment.page_index in judgment_by_index:
            raise ValueError("cluster triage merge contains a duplicate judgment page")
        page = page_by_index.get(judgment.page_index)
        if page is None:
            raise ValueError("cluster triage merge contains a foreign judgment page")
        if (
            judgment.page_identity != page.page_identity
            or judgment.request_identity != request.request_identity
            or judgment.partition_identity != request.partition_identity
            or judgment.page_count != expected_page_count
            or {value["cluster_id"] for value in judgment.decisions}
            != set(page.cluster_ids)
        ):
            raise ValueError("cluster triage merge judgment binding is stale")
        for decision in judgment.decisions:
            _canonical_decision(
                decision,
                allowed_cluster_ids=set(page.cluster_ids),
                evidence_by_cluster={
                    cluster_id: set(page.evidence_refs_for(cluster_id))
                    for cluster_id in page.cluster_ids
                },
            )
        judgment_by_index[judgment.page_index] = judgment
    if set(judgment_by_index) != set(page_by_index):
        raise ValueError("cluster triage merge judgment pages are incomplete")
    decisions = {
        value["cluster_id"]: value
        for page_index in sorted(judgment_by_index)
        for value in judgment_by_index[page_index].decisions
    }
    if set(decisions) != set(request.cluster_ids):
        raise ValueError("cluster triage merge decisions are incomplete")
    return build_candidate_cluster_triage_decision(
        request=request,
        dispositions={
            cluster_id: value["disposition"]
            for cluster_id, value in decisions.items()
        },
        rationales={
            cluster_id: value["rationale"]
            for cluster_id, value in decisions.items()
        },
        evidence_refs={
            cluster_id: tuple(value["evidence_refs"])
            for cluster_id, value in decisions.items()
        },
    )


class ClusterTriageCapability(Protocol):
    def triage_candidate_cluster_page_bounded(
        self,
        page: ClusterTriagePageRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> Any:
        raise NotImplementedError

    def triage_candidate_clusters_bounded(
        self,
        request: CandidateClusterTriageRequest,
        *,
        manifest: CandidateClusterManifest,
        eligible_capsules: Sequence[CandidateEvidenceCapsule],
        active_defect: DefectState,
        objective: str,
        analysis_perspective: str,
        max_physical_requests: Optional[int],
    ) -> Any:
        raise NotImplementedError


__all__ = [
    "CLUSTER_TRIAGE_JUDGMENT_SCHEMA",
    "CLUSTER_TRIAGE_PAGE_RESPONSE_SCHEMA",
    "CLUSTER_TRIAGE_PAGE_SCHEMA",
    "CLUSTER_TRIAGE_PAGE_SIZE",
    "CLUSTER_TRIAGE_PROMPT_SCHEMA_VERSION",
    "CLUSTER_TRIAGE_SYSTEM_PROMPT",
    "ClusterTriageCapability",
    "ClusterTriageJudgment",
    "ClusterTriagePageRequest",
    "build_cluster_triage_page_requests",
    "build_cluster_triage_prompt",
    "merge_cluster_triage_judgments",
    "parse_cluster_triage_judgment",
]
