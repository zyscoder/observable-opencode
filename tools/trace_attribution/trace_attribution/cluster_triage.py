"""Immutable zero-loss data contracts for candidate-cluster triage."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from .candidate_budget import candidate_identity
from .candidate_clustering import (
    CandidateCluster,
    CandidateClusterManifest,
)
from .models import JsonDict, stable_json


CANDIDATE_CLUSTER_TRIAGE_REQUEST_SCHEMA = (
    "candidate-cluster-triage-request/v1"
)
CANDIDATE_CLUSTER_TRIAGE_DECISION_SCHEMA = (
    "candidate-cluster-triage-decision/v1"
)
CANDIDATE_CLUSTER_COVERAGE_PROOF_SCHEMA = (
    "candidate-cluster-coverage-proof/v1"
)
CANDIDATE_CLUSTER_TRIAGE_PLAN_SCHEMA = "candidate-cluster-triage-plan/v1"
CANDIDATE_CLUSTER_TRIAGE_POLICY = "zero-loss-cluster-navigation/v1"

SELECTED = "selected"
UNSELECTED = "unselected"
UNCERTAIN = "uncertain"
FALLBACK_FULL_PAGING = "fallback_full_paging"

_DECISIONS = frozenset({SELECTED, UNSELECTED, UNCERTAIN})
_STRICT_DISPOSITIONS = frozenset(
    {"selected_expanded", "uncertain_expanded", "unselected_deferred"}
)
_REPRESENTATIVE_KEYS = frozenset(
    {
        "earliest_authored_plan",
        "latest_execution",
        "latest_verification",
        "latest_closure",
        "latest_root_eligible",
    }
)
_HEX = frozenset("0123456789abcdef")


def _digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _identity(schema: str, facts: Any) -> str:
    return _digest({"schema": schema, "facts": facts})


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
        raise ValueError("{0} must be a string".format(label))
    return value


def _require_ref(value: Any, label: str) -> str:
    result = _require_text(value, label)
    if ":" not in result:
        raise ValueError("{0} must be a canonical ref".format(label))
    return result


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
                label,
                sorted(expected - actual),
                sorted(actual - expected),
            )
        )
    return value


def _json_array(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TypeError("{0} must be a JSON array".format(label))
    return value


def _string_tuple(
    value: Any, label: str, *, require_json_array: bool = False
) -> Tuple[str, ...]:
    if require_json_array:
        value = _json_array(value, label)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("{0} must be an array".format(label))
    output = tuple(value)
    if any(not isinstance(item, str) or not item for item in output):
        raise ValueError("{0} must contain non-empty strings".format(label))
    return output


def _unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError("{0} must not contain duplicates".format(label))


def _bindings(
    refs: Sequence[str], identities: Sequence[str], label: str
) -> Tuple[Tuple[str, str], ...]:
    refs = tuple(refs)
    identities = tuple(identities)
    if len(refs) != len(identities):
        raise ValueError("{0} refs and identities must align".format(label))
    for ref in refs:
        _require_ref(ref, "{0} ref".format(label))
    for identity in identities:
        _require_identity(identity, "{0} identity".format(label))
    _unique(refs, "{0} refs".format(label))
    _unique(identities, "{0} identities".format(label))
    return tuple(sorted(zip(refs, identities), key=lambda item: (item[1], item[0])))


def _binding_payload(values: Sequence[Tuple[str, str]]) -> list[JsonDict]:
    return [
        {"ref": ref, "candidate_identity": identity}
        for ref, identity in values
    ]


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


def _canonical_cluster_payload(
    cluster: CandidateCluster,
    representatives: Mapping[str, Optional[str]],
) -> JsonDict:
    members = tuple(
        sorted(
            zip(cluster.member_refs, cluster.member_identities),
            key=lambda item: (item[1], item[0]),
        )
    )
    return {
        "cluster_id": cluster.cluster_id,
        "grouping_level": cluster.grouping_level,
        "grouping_key": cluster.grouping_key,
        "grouping_reason": cluster.grouping_reason,
        "grounding_refs": sorted(cluster.grounding_refs),
        "members": _binding_payload(members),
        "root_eligible_refs": sorted(cluster.root_eligible_refs),
        "representatives": {
            key: representatives[key]
            for key in sorted(representatives)
        },
        "role_distribution": dict(sorted(cluster.role_distribution)),
        "event_type_distribution": dict(
            sorted(cluster.event_type_distribution)
        ),
        "component_distribution": dict(
            sorted(cluster.component_distribution)
        ),
        "source_distribution": dict(sorted(cluster.source_distribution)),
        "action_identity_distribution": dict(
            sorted(cluster.action_identity_distribution)
        ),
        "obligation_distribution": dict(
            sorted(cluster.obligation_distribution)
        ),
        "attribution_only_gap_identities": sorted(
            cluster.attribution_only_gap_identities
        ),
        "support_evidence_refs": sorted(cluster.support_evidence_refs),
        "opposition_evidence_refs": sorted(
            cluster.opposition_evidence_refs
        ),
        "member_dispositions": {
            key: value
            for key, value in sorted(cluster.member_dispositions)
        },
        "expansion_status": cluster.expansion_status,
        "expansion_reason": cluster.expansion_reason,
    }


def _cluster_content_identity(
    cluster: CandidateCluster,
    representatives: Mapping[str, Optional[str]],
) -> str:
    return _identity(
        "candidate-cluster-triage-cluster-content/v1",
        _canonical_cluster_payload(cluster, representatives),
    )


def _canonical_representatives(
    cluster: CandidateCluster,
    facts_by_ref: Mapping[str, Any],
) -> Dict[str, Optional[str]]:
    members = tuple(facts_by_ref[ref] for ref in cluster.member_refs)

    def earliest(role: str) -> Optional[str]:
        matches = tuple(fact for fact in members if fact.episode_role == role)
        if not matches:
            return None
        return min(matches, key=lambda fact: (fact.position, fact.ref)).ref

    def latest(role: str) -> Optional[str]:
        matches = tuple(fact for fact in members if fact.episode_role == role)
        if not matches:
            return None
        return max(matches, key=lambda fact: (fact.position, fact.ref)).ref

    root_eligible = tuple(
        fact for fact in members if fact.root_candidate_eligible
    )
    return {
        "earliest_authored_plan": earliest("authored_plan"),
        "latest_execution": latest("execution"),
        "latest_verification": latest("verification"),
        "latest_closure": latest("closure"),
        "latest_root_eligible": (
            max(root_eligible, key=lambda fact: (fact.position, fact.ref)).ref
            if root_eligible
            else None
        ),
    }


_DIRECTORY_KEYS = frozenset(
    {
        "cluster_id",
        "cluster_content_identity",
        "member_refs",
        "member_identities",
        "eligible_candidate_refs",
        "eligible_candidate_identities",
        "representatives",
        "evidence_refs",
    }
)


def _canonical_directory(
    values: Sequence[Mapping[str, Any]],
    *,
    require_json_array: bool = False,
) -> Tuple[Mapping[str, Any], ...]:
    if require_json_array:
        values = _json_array(values, "triage request cluster_directory")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("triage request cluster_directory must be an array")
    output = []
    for raw in values:
        item = _exact_mapping(raw, _DIRECTORY_KEYS, "triage cluster directory entry")
        cluster_id = _require_ref(item["cluster_id"], "triage cluster ID")
        if not cluster_id.startswith("cluster:"):
            raise ValueError("triage cluster ID must be a cluster ref")
        content_identity = _require_identity(
            item["cluster_content_identity"], "triage cluster content identity"
        )
        member_refs = _string_tuple(
            item["member_refs"],
            "triage cluster member refs",
            require_json_array=require_json_array,
        )
        member_identities = _string_tuple(
            item["member_identities"],
            "triage cluster member identities",
            require_json_array=require_json_array,
        )
        members = _bindings(member_refs, member_identities, "triage cluster members")
        eligible_refs = _string_tuple(
            item["eligible_candidate_refs"],
            "triage cluster eligible refs",
            require_json_array=require_json_array,
        )
        eligible_identities = _string_tuple(
            item["eligible_candidate_identities"],
            "triage cluster eligible identities",
            require_json_array=require_json_array,
        )
        eligible = _bindings(
            eligible_refs,
            eligible_identities,
            "triage cluster eligible candidates",
        )
        if not eligible or not set(eligible).issubset(set(members)):
            raise ValueError(
                "triage cluster eligible candidates must be non-empty members"
            )
        representatives = _exact_mapping(
            item["representatives"],
            _REPRESENTATIVE_KEYS,
            "triage cluster representatives",
        )
        member_ref_set = {ref for ref, _identity_value in members}
        normalized_representatives: Dict[str, Optional[str]] = {}
        for key in sorted(representatives):
            ref = representatives[key]
            if ref is not None:
                ref = _require_ref(ref, "triage cluster representative")
                if ref not in member_ref_set or ref.startswith("cluster:"):
                    raise ValueError(
                        "triage representative must be an original cluster member"
                    )
            normalized_representatives[key] = ref
        evidence_refs = tuple(
            sorted(
                _string_tuple(
                    item["evidence_refs"],
                    "triage cluster evidence refs",
                    require_json_array=require_json_array,
                )
            )
        )
        _unique(evidence_refs, "triage cluster evidence refs")
        output.append(
            {
                "cluster_id": cluster_id,
                "cluster_content_identity": content_identity,
                "member_refs": [ref for ref, _identity_value in members],
                "member_identities": [identity for _ref, identity in members],
                "eligible_candidate_refs": [ref for ref, _identity_value in eligible],
                "eligible_candidate_identities": [
                    identity for _ref, identity in eligible
                ],
                "representatives": normalized_representatives,
                "evidence_refs": list(evidence_refs),
            }
        )
    cluster_ids = [item["cluster_id"] for item in output]
    _unique(cluster_ids, "triage cluster directory IDs")
    return tuple(_freeze(item) for item in sorted(output, key=lambda item: item["cluster_id"]))


def _eligible_set_identity(values: Sequence[Tuple[str, str]]) -> str:
    return _identity(
        "candidate-cluster-triage-eligible-set/v1",
        _binding_payload(values),
    )


def _partition_identity(
    eligible_set_identity: str,
    directory: Sequence[Mapping[str, Any]],
) -> str:
    return _identity(
        "candidate-cluster-triage-partition/v1",
        {
            "eligible_set_identity": eligible_set_identity,
            "clusters": [
                {
                    "cluster_id": item["cluster_id"],
                    "cluster_content_identity": item[
                        "cluster_content_identity"
                    ],
                    "eligible_candidates": _binding_payload(
                        tuple(
                            zip(
                                item["eligible_candidate_refs"],
                                item["eligible_candidate_identities"],
                            )
                        )
                    ),
                }
                for item in directory
            ],
        },
    )


def _request_unsigned(
    *,
    case_id: str,
    seed_ref: str,
    defect_fingerprint: str,
    manifest_identity: str,
    candidate_set_identity: str,
    source_selection_identity: str,
    eligible: Sequence[Tuple[str, str]],
    eligible_set_identity: str,
    directory: Sequence[Mapping[str, Any]],
    partition_identity: str,
) -> JsonDict:
    return {
        "schema": CANDIDATE_CLUSTER_TRIAGE_REQUEST_SCHEMA,
        "policy_version": CANDIDATE_CLUSTER_TRIAGE_POLICY,
        "case_id": case_id,
        "seed_ref": seed_ref,
        "defect_fingerprint": defect_fingerprint,
        "manifest_identity": manifest_identity,
        "candidate_set_identity": candidate_set_identity,
        "source_selection_identity": source_selection_identity,
        "eligible_candidate_refs": [ref for ref, _identity_value in eligible],
        "eligible_candidate_identities": [identity for _ref, identity in eligible],
        "eligible_set_identity": eligible_set_identity,
        "cluster_directory": [_thaw(item) for item in directory],
        "partition_identity": partition_identity,
    }


@dataclass(frozen=True)
class CandidateClusterTriageRequest:
    schema: str
    policy_version: str
    case_id: str
    seed_ref: str
    defect_fingerprint: str
    manifest_identity: str
    candidate_set_identity: str
    source_selection_identity: str
    eligible_candidate_refs: Tuple[str, ...]
    eligible_candidate_identities: Tuple[str, ...]
    eligible_set_identity: str
    cluster_directory: Tuple[Mapping[str, Any], ...]
    partition_identity: str
    request_identity: str

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_CLUSTER_TRIAGE_REQUEST_SCHEMA:
            raise ValueError("candidate cluster triage request schema is invalid")
        if self.policy_version != CANDIDATE_CLUSTER_TRIAGE_POLICY:
            raise ValueError("candidate cluster triage policy is invalid")
        _require_text(self.case_id, "triage request case_id", allow_empty=True)
        _require_ref(self.seed_ref, "triage request seed_ref")
        _require_text(self.defect_fingerprint, "triage request defect fingerprint")
        _require_identity(self.manifest_identity, "triage request manifest identity")
        _require_identity(
            self.candidate_set_identity, "triage request candidate set identity"
        )
        _require_identity(
            self.source_selection_identity,
            "triage request source selection identity",
        )
        eligible = _bindings(
            self.eligible_candidate_refs,
            self.eligible_candidate_identities,
            "triage request eligible candidates",
        )
        if tuple(zip(self.eligible_candidate_refs, self.eligible_candidate_identities)) != eligible:
            raise ValueError("triage request eligible candidates must be canonical")
        directory = _canonical_directory(self.cluster_directory)
        partition_members = tuple(
            (ref, identity)
            for item in directory
            for ref, identity in zip(
                item["eligible_candidate_refs"],
                item["eligible_candidate_identities"],
            )
        )
        if len(partition_members) != len(set(partition_members)) or set(
            partition_members
        ) != set(eligible):
            raise ValueError(
                "triage request partition must cover each eligible candidate exactly once"
            )
        expected_eligible_identity = _eligible_set_identity(eligible)
        if self.eligible_set_identity != expected_eligible_identity:
            raise ValueError("triage request eligible set identity is invalid")
        expected_partition_identity = _partition_identity(
            expected_eligible_identity, directory
        )
        if self.partition_identity != expected_partition_identity:
            raise ValueError("triage request partition identity is invalid")
        unsigned = _request_unsigned(
            case_id=self.case_id,
            seed_ref=self.seed_ref,
            defect_fingerprint=self.defect_fingerprint,
            manifest_identity=self.manifest_identity,
            candidate_set_identity=self.candidate_set_identity,
            source_selection_identity=self.source_selection_identity,
            eligible=eligible,
            eligible_set_identity=expected_eligible_identity,
            directory=directory,
            partition_identity=expected_partition_identity,
        )
        if self.request_identity != _identity(
            "candidate-cluster-triage-request-identity/v1", unsigned
        ):
            raise ValueError("triage request identity does not match request facts")
        object.__setattr__(
            self,
            "eligible_candidate_refs",
            tuple(ref for ref, _identity_value in eligible),
        )
        object.__setattr__(
            self,
            "eligible_candidate_identities",
            tuple(identity for _ref, identity in eligible),
        )
        object.__setattr__(self, "cluster_directory", directory)

    @property
    def cluster_ids(self) -> Tuple[str, ...]:
        return tuple(item["cluster_id"] for item in self.cluster_directory)

    @property
    def cluster_content_identities(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(
            (item["cluster_id"], item["cluster_content_identity"])
            for item in self.cluster_directory
        )

    def eligible_refs_for(self, cluster_id: str) -> Tuple[str, ...]:
        item = self._cluster(cluster_id)
        return tuple(item["eligible_candidate_refs"])

    def evidence_refs_for(self, cluster_id: str) -> Tuple[str, ...]:
        return tuple(self._cluster(cluster_id)["evidence_refs"])

    def _cluster(self, cluster_id: str) -> Mapping[str, Any]:
        for item in self.cluster_directory:
            if item["cluster_id"] == cluster_id:
                return item
        raise KeyError(cluster_id)

    def to_dict(self) -> JsonDict:
        payload = _request_unsigned(
            case_id=self.case_id,
            seed_ref=self.seed_ref,
            defect_fingerprint=self.defect_fingerprint,
            manifest_identity=self.manifest_identity,
            candidate_set_identity=self.candidate_set_identity,
            source_selection_identity=self.source_selection_identity,
            eligible=tuple(
                zip(
                    self.eligible_candidate_refs,
                    self.eligible_candidate_identities,
                )
            ),
            eligible_set_identity=self.eligible_set_identity,
            directory=self.cluster_directory,
            partition_identity=self.partition_identity,
        )
        payload["request_identity"] = self.request_identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateClusterTriageRequest":
        payload = _exact_mapping(
            value,
            frozenset(
                {
                    "schema",
                    "policy_version",
                    "case_id",
                    "seed_ref",
                    "defect_fingerprint",
                    "manifest_identity",
                    "candidate_set_identity",
                    "source_selection_identity",
                    "eligible_candidate_refs",
                    "eligible_candidate_identities",
                    "eligible_set_identity",
                    "cluster_directory",
                    "partition_identity",
                    "request_identity",
                }
            ),
            "candidate cluster triage request",
        )
        return cls(
            schema=payload["schema"],
            policy_version=payload["policy_version"],
            case_id=payload["case_id"],
            seed_ref=payload["seed_ref"],
            defect_fingerprint=payload["defect_fingerprint"],
            manifest_identity=payload["manifest_identity"],
            candidate_set_identity=payload["candidate_set_identity"],
            source_selection_identity=payload["source_selection_identity"],
            eligible_candidate_refs=_string_tuple(
                payload["eligible_candidate_refs"],
                "triage request eligible refs",
                require_json_array=True,
            ),
            eligible_candidate_identities=_string_tuple(
                payload["eligible_candidate_identities"],
                "triage request eligible identities",
                require_json_array=True,
            ),
            eligible_set_identity=payload["eligible_set_identity"],
            cluster_directory=_canonical_directory(
                payload["cluster_directory"], require_json_array=True
            ),
            partition_identity=payload["partition_identity"],
            request_identity=payload["request_identity"],
        )


_DECISION_ENTRY_KEYS = frozenset(
    {"cluster_id", "disposition", "rationale", "evidence_refs"}
)


def _canonical_decisions(
    values: Sequence[Mapping[str, Any]], *, require_json_array: bool = False
) -> Tuple[Mapping[str, Any], ...]:
    if require_json_array:
        values = _json_array(values, "triage decision entries")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("triage decision entries must be an array")
    output = []
    for raw in values:
        item = _exact_mapping(raw, _DECISION_ENTRY_KEYS, "triage decision entry")
        cluster_id = _require_ref(item["cluster_id"], "triage decision cluster ID")
        disposition = item["disposition"]
        if disposition not in _DECISIONS:
            raise ValueError("triage decision disposition is invalid")
        rationale = _require_text(item["rationale"], "triage decision rationale")
        evidence_refs = tuple(
            sorted(
                _string_tuple(
                    item["evidence_refs"],
                    "triage decision evidence refs",
                    require_json_array=require_json_array,
                )
            )
        )
        if not evidence_refs:
            raise ValueError("triage decision evidence refs must not be empty")
        _unique(evidence_refs, "triage decision evidence refs")
        output.append(
            _freeze(
                {
                    "cluster_id": cluster_id,
                    "disposition": disposition,
                    "rationale": rationale,
                    "evidence_refs": list(evidence_refs),
                }
            )
        )
    cluster_ids = [item["cluster_id"] for item in output]
    _unique(cluster_ids, "triage decision cluster IDs")
    return tuple(sorted(output, key=lambda item: item["cluster_id"]))


def _selection_identity(
    request_identity: str,
    partition_identity: str,
    decisions: Sequence[Mapping[str, Any]],
) -> str:
    return _identity(
        "candidate-cluster-triage-selection/v1",
        {
            "request_identity": request_identity,
            "partition_identity": partition_identity,
            "decisions": [
                {
                    "cluster_id": item["cluster_id"],
                    "disposition": item["disposition"],
                }
                for item in decisions
            ],
        },
    )


@dataclass(frozen=True)
class CandidateClusterTriageDecision:
    schema: str
    policy_version: str
    request_identity: str
    partition_identity: str
    decisions: Tuple[Mapping[str, Any], ...]
    selection_identity: str
    judgment_identity: str

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_CLUSTER_TRIAGE_DECISION_SCHEMA:
            raise ValueError("candidate cluster triage decision schema is invalid")
        if self.policy_version != CANDIDATE_CLUSTER_TRIAGE_POLICY:
            raise ValueError("candidate cluster triage decision policy is invalid")
        _require_identity(self.request_identity, "triage decision request identity")
        _require_identity(
            self.partition_identity, "triage decision partition identity"
        )
        decisions = _canonical_decisions(self.decisions)
        expected_selection_identity = _selection_identity(
            self.request_identity, self.partition_identity, decisions
        )
        if self.selection_identity != expected_selection_identity:
            raise ValueError("triage decision selection identity is invalid")
        unsigned = {
            "schema": self.schema,
            "policy_version": self.policy_version,
            "request_identity": self.request_identity,
            "partition_identity": self.partition_identity,
            "decisions": [_thaw(item) for item in decisions],
            "selection_identity": self.selection_identity,
        }
        if self.judgment_identity != _identity(
            "candidate-cluster-triage-judgment/v1", unsigned
        ):
            raise ValueError("triage decision judgment identity is invalid")
        object.__setattr__(self, "decisions", decisions)

    def to_dict(self) -> JsonDict:
        return {
            "schema": self.schema,
            "policy_version": self.policy_version,
            "request_identity": self.request_identity,
            "partition_identity": self.partition_identity,
            "decisions": [_thaw(item) for item in self.decisions],
            "selection_identity": self.selection_identity,
            "judgment_identity": self.judgment_identity,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateClusterTriageDecision":
        payload = _exact_mapping(
            value,
            frozenset(
                {
                    "schema",
                    "policy_version",
                    "request_identity",
                    "partition_identity",
                    "decisions",
                    "selection_identity",
                    "judgment_identity",
                }
            ),
            "candidate cluster triage decision",
        )
        return cls(
            schema=payload["schema"],
            policy_version=payload["policy_version"],
            request_identity=payload["request_identity"],
            partition_identity=payload["partition_identity"],
            decisions=_canonical_decisions(
                payload["decisions"], require_json_array=True
            ),
            selection_identity=payload["selection_identity"],
            judgment_identity=payload["judgment_identity"],
        )


def _coverage_unsigned(
    *,
    mode: str,
    request_identity: str,
    selection_identity: str,
    eligible_set_identity: str,
    partition_identity: str,
    selected_cluster_ids: Sequence[str],
    unselected_cluster_ids: Sequence[str],
    expanded: Sequence[Tuple[str, str]],
    expanded_set_identity: str,
    partition_complete: bool,
    expansion_complete: bool,
    original_identity_only: bool,
) -> JsonDict:
    return {
        "schema": CANDIDATE_CLUSTER_COVERAGE_PROOF_SCHEMA,
        "policy_version": CANDIDATE_CLUSTER_TRIAGE_POLICY,
        "mode": mode,
        "request_identity": request_identity,
        "selection_identity": selection_identity,
        "eligible_set_identity": eligible_set_identity,
        "partition_identity": partition_identity,
        "selected_cluster_ids": list(selected_cluster_ids),
        "unselected_cluster_ids": list(unselected_cluster_ids),
        "expanded_candidate_refs": [ref for ref, _identity_value in expanded],
        "expanded_candidate_identities": [
            identity for _ref, identity in expanded
        ],
        "expanded_set_identity": expanded_set_identity,
        "partition_complete": partition_complete,
        "expansion_complete": expansion_complete,
        "original_identity_only": original_identity_only,
    }


@dataclass(frozen=True)
class CandidateClusterCoverageProof:
    schema: str
    policy_version: str
    mode: str
    request_identity: str
    selection_identity: str
    eligible_set_identity: str
    partition_identity: str
    selected_cluster_ids: Tuple[str, ...]
    unselected_cluster_ids: Tuple[str, ...]
    expanded_candidate_refs: Tuple[str, ...]
    expanded_candidate_identities: Tuple[str, ...]
    expanded_set_identity: str
    partition_complete: bool
    expansion_complete: bool
    original_identity_only: bool
    proof_identity: str

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_CLUSTER_COVERAGE_PROOF_SCHEMA:
            raise ValueError("candidate cluster coverage proof schema is invalid")
        if self.policy_version != CANDIDATE_CLUSTER_TRIAGE_POLICY:
            raise ValueError("candidate cluster coverage proof policy is invalid")
        if self.mode not in {"triaged", FALLBACK_FULL_PAGING}:
            raise ValueError("candidate cluster coverage proof mode is invalid")
        for value, label in (
            (self.request_identity, "request"),
            (self.selection_identity, "selection"),
            (self.eligible_set_identity, "eligible set"),
            (self.partition_identity, "partition"),
            (self.expanded_set_identity, "expanded set"),
        ):
            _require_identity(value, "coverage proof {0} identity".format(label))
        selected = tuple(sorted(_string_tuple(self.selected_cluster_ids, "coverage selected clusters")))
        unselected = tuple(sorted(_string_tuple(self.unselected_cluster_ids, "coverage unselected clusters")))
        _unique(selected, "coverage selected clusters")
        _unique(unselected, "coverage unselected clusters")
        if set(selected) & set(unselected):
            raise ValueError("coverage selected and unselected clusters overlap")
        expanded = _bindings(
            self.expanded_candidate_refs,
            self.expanded_candidate_identities,
            "coverage expanded candidates",
        )
        if tuple(zip(self.expanded_candidate_refs, self.expanded_candidate_identities)) != expanded:
            raise ValueError("coverage expanded candidates must be canonical")
        if self.expanded_set_identity != _identity(
            "candidate-cluster-triage-expanded-set/v1",
            _binding_payload(expanded),
        ):
            raise ValueError("coverage expanded set identity is invalid")
        if any(
            type(value) is not bool or not value
            for value in (
                self.partition_complete,
                self.expansion_complete,
                self.original_identity_only,
            )
        ):
            raise ValueError("coverage proof must affirm all structural checks")
        unsigned = _coverage_unsigned(
            mode=self.mode,
            request_identity=self.request_identity,
            selection_identity=self.selection_identity,
            eligible_set_identity=self.eligible_set_identity,
            partition_identity=self.partition_identity,
            selected_cluster_ids=selected,
            unselected_cluster_ids=unselected,
            expanded=expanded,
            expanded_set_identity=self.expanded_set_identity,
            partition_complete=True,
            expansion_complete=True,
            original_identity_only=True,
        )
        if self.proof_identity != _identity(
            "candidate-cluster-coverage-proof-identity/v1", unsigned
        ):
            raise ValueError("coverage proof identity is invalid")
        object.__setattr__(self, "selected_cluster_ids", selected)
        object.__setattr__(self, "unselected_cluster_ids", unselected)
        object.__setattr__(
            self,
            "expanded_candidate_refs",
            tuple(ref for ref, _identity_value in expanded),
        )
        object.__setattr__(
            self,
            "expanded_candidate_identities",
            tuple(identity for _ref, identity in expanded),
        )

    def to_dict(self) -> JsonDict:
        payload = _coverage_unsigned(
            mode=self.mode,
            request_identity=self.request_identity,
            selection_identity=self.selection_identity,
            eligible_set_identity=self.eligible_set_identity,
            partition_identity=self.partition_identity,
            selected_cluster_ids=self.selected_cluster_ids,
            unselected_cluster_ids=self.unselected_cluster_ids,
            expanded=tuple(
                zip(
                    self.expanded_candidate_refs,
                    self.expanded_candidate_identities,
                )
            ),
            expanded_set_identity=self.expanded_set_identity,
            partition_complete=self.partition_complete,
            expansion_complete=self.expansion_complete,
            original_identity_only=self.original_identity_only,
        )
        payload["proof_identity"] = self.proof_identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateClusterCoverageProof":
        payload = _exact_mapping(
            value,
            frozenset(
                {
                    "schema",
                    "policy_version",
                    "mode",
                    "request_identity",
                    "selection_identity",
                    "eligible_set_identity",
                    "partition_identity",
                    "selected_cluster_ids",
                    "unselected_cluster_ids",
                    "expanded_candidate_refs",
                    "expanded_candidate_identities",
                    "expanded_set_identity",
                    "partition_complete",
                    "expansion_complete",
                    "original_identity_only",
                    "proof_identity",
                }
            ),
            "candidate cluster coverage proof",
        )
        return cls(
            schema=payload["schema"],
            policy_version=payload["policy_version"],
            mode=payload["mode"],
            request_identity=payload["request_identity"],
            selection_identity=payload["selection_identity"],
            eligible_set_identity=payload["eligible_set_identity"],
            partition_identity=payload["partition_identity"],
            selected_cluster_ids=_string_tuple(
                payload["selected_cluster_ids"],
                "coverage selected clusters",
                require_json_array=True,
            ),
            unselected_cluster_ids=_string_tuple(
                payload["unselected_cluster_ids"],
                "coverage unselected clusters",
                require_json_array=True,
            ),
            expanded_candidate_refs=_string_tuple(
                payload["expanded_candidate_refs"],
                "coverage expanded refs",
                require_json_array=True,
            ),
            expanded_candidate_identities=_string_tuple(
                payload["expanded_candidate_identities"],
                "coverage expanded identities",
                require_json_array=True,
            ),
            expanded_set_identity=payload["expanded_set_identity"],
            partition_complete=payload["partition_complete"],
            expansion_complete=payload["expansion_complete"],
            original_identity_only=payload["original_identity_only"],
            proof_identity=payload["proof_identity"],
        )


_PARTITION_ENTRY_KEYS = frozenset(
    {
        "cluster_id",
        "member_refs",
        "member_identities",
        "candidate_refs",
        "candidate_identities",
    }
)
_REPRESENTATIVE_ENTRY_KEYS = frozenset({"cluster_id", "refs"})
_DISPOSITION_ENTRY_KEYS = frozenset({"cluster_id", "disposition"})


def _canonical_partitions(
    values: Sequence[Mapping[str, Any]], *, require_json_array: bool = False
) -> Tuple[Mapping[str, Any], ...]:
    if require_json_array:
        values = _json_array(values, "triage plan cluster partitions")
    output = []
    for raw in values:
        item = _exact_mapping(raw, _PARTITION_ENTRY_KEYS, "triage plan partition")
        cluster_id = _require_ref(item["cluster_id"], "triage plan cluster ID")
        member_refs = _string_tuple(
            item["member_refs"],
            "triage plan member refs",
            require_json_array=require_json_array,
        )
        member_identities = _string_tuple(
            item["member_identities"],
            "triage plan member identities",
            require_json_array=require_json_array,
        )
        members = _bindings(
            member_refs, member_identities, "triage plan complete cluster members"
        )
        refs = _string_tuple(
            item["candidate_refs"],
            "triage plan partition refs",
            require_json_array=require_json_array,
        )
        identities = _string_tuple(
            item["candidate_identities"],
            "triage plan partition identities",
            require_json_array=require_json_array,
        )
        bindings = _bindings(refs, identities, "triage plan partition")
        if not set(bindings).issubset(set(members)):
            raise ValueError(
                "triage plan eligible partition must be complete cluster members"
            )
        output.append(
            _freeze(
                {
                    "cluster_id": cluster_id,
                    "member_refs": [ref for ref, _identity_value in members],
                    "member_identities": [
                        identity for _ref, identity in members
                    ],
                    "candidate_refs": [ref for ref, _identity_value in bindings],
                    "candidate_identities": [
                        identity for _ref, identity in bindings
                    ],
                }
            )
        )
    ids = [item["cluster_id"] for item in output]
    _unique(ids, "triage plan partition cluster IDs")
    return tuple(sorted(output, key=lambda item: item["cluster_id"]))


def _canonical_representative_refs(
    values: Sequence[Mapping[str, Any]], *, require_json_array: bool = False
) -> Tuple[Mapping[str, Any], ...]:
    if require_json_array:
        values = _json_array(values, "triage plan representative refs")
    output = []
    for raw in values:
        item = _exact_mapping(
            raw, _REPRESENTATIVE_ENTRY_KEYS, "triage plan representative entry"
        )
        refs = tuple(
            sorted(
                _string_tuple(
                    item["refs"],
                    "triage plan representative refs",
                    require_json_array=require_json_array,
                )
            )
        )
        _unique(refs, "triage plan representative refs")
        if any(ref.startswith("cluster:") for ref in refs):
            raise ValueError("triage representative cannot be a synthetic cluster ref")
        output.append(
            _freeze({"cluster_id": item["cluster_id"], "refs": list(refs)})
        )
    ids = [item["cluster_id"] for item in output]
    _unique(ids, "triage plan representative cluster IDs")
    return tuple(sorted(output, key=lambda item: item["cluster_id"]))


def _canonical_dispositions(
    values: Sequence[Mapping[str, Any]], *, require_json_array: bool = False
) -> Tuple[Tuple[str, str], ...]:
    if require_json_array:
        values = _json_array(values, "triage plan dispositions")
    output = []
    for raw in values:
        item = _exact_mapping(
            raw, _DISPOSITION_ENTRY_KEYS, "triage plan disposition"
        )
        output.append((item["cluster_id"], item["disposition"]))
    ids = [cluster_id for cluster_id, _disposition in output]
    _unique(ids, "triage plan disposition cluster IDs")
    return tuple(sorted(output))


def _plan_unsigned_values(values: Mapping[str, Any]) -> JsonDict:
    return {
        "schema": values["schema"],
        "policy_version": values["policy_version"],
        "case_id": values["case_id"],
        "seed_ref": values["seed_ref"],
        "defect_fingerprint": values["defect_fingerprint"],
        "manifest_identity": values["manifest_identity"],
        "candidate_set_identity": values["candidate_set_identity"],
        "source_selection_identity": values["source_selection_identity"],
        "request_identity": values["request_identity"],
        "decision_identity": values["decision_identity"],
        "selection_identity": values["selection_identity"],
        "eligible_candidate_refs": list(values["eligible_candidate_refs"]),
        "eligible_candidate_identities": list(
            values["eligible_candidate_identities"]
        ),
        "eligible_set_identity": values["eligible_set_identity"],
        "cluster_content_identities": [
            {"cluster_id": cluster_id, "cluster_content_identity": identity}
            for cluster_id, identity in values["cluster_content_identities"]
        ],
        "cluster_partitions": [
            _thaw(item) for item in values["cluster_partitions"]
        ],
        "representative_refs": [
            _thaw(item) for item in values["representative_refs"]
        ],
        "partition_identity": values["partition_identity"],
        "selected_cluster_ids": list(values["selected_cluster_ids"]),
        "uncertain_cluster_ids": list(values["uncertain_cluster_ids"]),
        "effective_selected_cluster_ids": list(
            values["effective_selected_cluster_ids"]
        ),
        "unselected_cluster_ids": list(values["unselected_cluster_ids"]),
        "expanded_candidate_refs": list(values["expanded_candidate_refs"]),
        "expanded_candidate_identities": list(
            values["expanded_candidate_identities"]
        ),
        "cluster_dispositions": [
            {"cluster_id": cluster_id, "disposition": disposition}
            for cluster_id, disposition in values["cluster_dispositions"]
        ],
        "coverage_proof": values["coverage_proof"].to_dict(),
        "fallback_reason": values["fallback_reason"],
    }


def _plan_unsigned(plan: "CandidateClusterTriagePlan") -> JsonDict:
    return _plan_unsigned_values(vars(plan))


@dataclass(frozen=True)
class CandidateClusterTriagePlan:
    schema: str
    policy_version: str
    case_id: str
    seed_ref: str
    defect_fingerprint: str
    manifest_identity: str
    candidate_set_identity: str
    source_selection_identity: str
    request_identity: str
    decision_identity: str
    selection_identity: str
    eligible_candidate_refs: Tuple[str, ...]
    eligible_candidate_identities: Tuple[str, ...]
    eligible_set_identity: str
    cluster_content_identities: Tuple[Tuple[str, str], ...]
    cluster_partitions: Tuple[Mapping[str, Any], ...]
    representative_refs: Tuple[Mapping[str, Any], ...]
    partition_identity: str
    selected_cluster_ids: Tuple[str, ...]
    uncertain_cluster_ids: Tuple[str, ...]
    effective_selected_cluster_ids: Tuple[str, ...]
    unselected_cluster_ids: Tuple[str, ...]
    expanded_candidate_refs: Tuple[str, ...]
    expanded_candidate_identities: Tuple[str, ...]
    cluster_dispositions: Tuple[Tuple[str, str], ...]
    coverage_proof: CandidateClusterCoverageProof
    fallback_reason: str
    plan_identity: str

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_CLUSTER_TRIAGE_PLAN_SCHEMA:
            raise ValueError("candidate cluster triage plan schema is invalid")
        if self.policy_version != CANDIDATE_CLUSTER_TRIAGE_POLICY:
            raise ValueError("candidate cluster triage plan policy is invalid")
        _require_text(self.case_id, "triage plan case_id", allow_empty=True)
        _require_ref(self.seed_ref, "triage plan seed_ref")
        _require_text(self.defect_fingerprint, "triage plan defect fingerprint")
        _require_text(
            self.fallback_reason,
            "triage plan fallback reason",
            allow_empty=True,
        )
        for value, label in (
            (self.manifest_identity, "manifest"),
            (self.candidate_set_identity, "candidate set"),
            (self.source_selection_identity, "source selection"),
            (self.request_identity, "request"),
            (self.decision_identity, "decision"),
            (self.selection_identity, "selection"),
            (self.eligible_set_identity, "eligible set"),
            (self.partition_identity, "partition"),
        ):
            _require_identity(value, "triage plan {0} identity".format(label))
        eligible = _bindings(
            self.eligible_candidate_refs,
            self.eligible_candidate_identities,
            "triage plan eligible candidates",
        )
        if tuple(zip(self.eligible_candidate_refs, self.eligible_candidate_identities)) != eligible:
            raise ValueError("triage plan eligible candidates must be canonical")
        if self.eligible_set_identity != _eligible_set_identity(eligible):
            raise ValueError("triage plan eligible set identity is invalid")
        content_identities = tuple(
            sorted(
                (cluster_id, identity)
                for cluster_id, identity in self.cluster_content_identities
            )
        )
        for cluster_id, identity in content_identities:
            _require_ref(cluster_id, "triage plan cluster ID")
            _require_identity(identity, "triage plan cluster content identity")
        _unique(
            [cluster_id for cluster_id, _identity_value in content_identities],
            "triage plan cluster content IDs",
        )
        partitions = _canonical_partitions(self.cluster_partitions)
        representatives = _canonical_representative_refs(self.representative_refs)
        cluster_ids = tuple(cluster_id for cluster_id, _identity_value in content_identities)
        if (
            tuple(item["cluster_id"] for item in partitions) != cluster_ids
            or tuple(item["cluster_id"] for item in representatives) != cluster_ids
        ):
            raise ValueError("triage plan cluster projections do not align")
        partitions_by_id = {
            item["cluster_id"]: item for item in partitions
        }
        for item in representatives:
            member_refs = set(
                partitions_by_id[item["cluster_id"]]["member_refs"]
            )
            if not set(item["refs"]).issubset(member_refs):
                raise ValueError(
                    "triage plan representative must be a complete cluster member"
                )
        partition_members = tuple(
            (ref, identity)
            for item in partitions
            for ref, identity in zip(
                item["candidate_refs"], item["candidate_identities"]
            )
        )
        if len(partition_members) != len(set(partition_members)) or set(
            partition_members
        ) != set(eligible):
            raise ValueError("triage plan partition is incomplete or overlapping")
        selected = tuple(sorted(self.selected_cluster_ids))
        uncertain = tuple(sorted(self.uncertain_cluster_ids))
        effective = tuple(sorted(self.effective_selected_cluster_ids))
        unselected = tuple(sorted(self.unselected_cluster_ids))
        for values, label in (
            (selected, "selected"),
            (uncertain, "uncertain"),
            (effective, "effective selected"),
            (unselected, "unselected"),
        ):
            _unique(values, "triage plan {0} clusters".format(label))
        if effective != tuple(sorted(set(selected) | set(uncertain))):
            raise ValueError("triage plan uncertain clusters must expand as selected")
        expanded = _bindings(
            self.expanded_candidate_refs,
            self.expanded_candidate_identities,
            "triage plan expanded candidates",
        )
        if tuple(zip(self.expanded_candidate_refs, self.expanded_candidate_identities)) != expanded:
            raise ValueError("triage plan expanded candidates must be canonical")
        if not set(expanded).issubset(set(eligible)):
            raise ValueError("triage plan expansion contains a foreign identity")
        if any(ref.startswith("cluster:") for ref, _identity_value in expanded):
            raise ValueError("triage plan expansion contains a synthetic cluster ref")
        if set(identity for _ref, identity in expanded) & set(
            identity for _cluster_id, identity in content_identities
        ):
            raise ValueError("triage plan expansion substitutes a cluster identity")
        dispositions = tuple(
            sorted(
                (cluster_id, disposition)
                for cluster_id, disposition in self.cluster_dispositions
            )
        )
        disposition_ids = tuple(cluster_id for cluster_id, _value in dispositions)
        if disposition_ids != cluster_ids:
            raise ValueError("triage plan dispositions must cover every cluster")
        fallback = bool(self.fallback_reason)
        if fallback:
            if (
                any(value != FALLBACK_FULL_PAGING for _cluster_id, value in dispositions)
                or selected
                or uncertain
                or effective
                or unselected
                or expanded != eligible
            ):
                raise ValueError("triage fallback must restore the complete eligible set")
        else:
            if set(selected) | set(uncertain) | set(unselected) != set(cluster_ids):
                raise ValueError("triage plan decisions must partition every cluster")
            if (
                set(selected) & set(uncertain)
                or set(effective) & set(unselected)
            ):
                raise ValueError("triage plan cluster decisions overlap")
            expected_dispositions = {
                **{value: "selected_expanded" for value in selected},
                **{value: "uncertain_expanded" for value in uncertain},
                **{value: "unselected_deferred" for value in unselected},
            }
            if dict(dispositions) != expected_dispositions or any(
                value not in _STRICT_DISPOSITIONS
                for _cluster_id, value in dispositions
            ):
                raise ValueError("triage plan dispositions contradict decisions")
            expected_expanded = tuple(
                sorted(
                    (
                        (ref, identity)
                        for item in partitions
                        if item["cluster_id"] in set(effective)
                        for ref, identity in zip(
                            item["candidate_refs"], item["candidate_identities"]
                        )
                    ),
                    key=lambda item: (item[1], item[0]),
                )
            )
            if expanded != expected_expanded:
                raise ValueError("triage plan expansion is incomplete")
        if (
            self.coverage_proof.request_identity != self.request_identity
            or self.coverage_proof.selection_identity != self.selection_identity
            or self.coverage_proof.eligible_set_identity != self.eligible_set_identity
            or self.coverage_proof.partition_identity != self.partition_identity
            or self.coverage_proof.expanded_candidate_refs
            != tuple(ref for ref, _identity_value in expanded)
            or self.coverage_proof.expanded_candidate_identities
            != tuple(identity for _ref, identity in expanded)
            or self.coverage_proof.mode
            != (FALLBACK_FULL_PAGING if fallback else "triaged")
            or self.coverage_proof.selected_cluster_ids
            != (() if fallback else effective)
            or self.coverage_proof.unselected_cluster_ids
            != (() if fallback else unselected)
        ):
            raise ValueError(
                "triage plan coverage proof cluster partition does not align"
            )
        if self.plan_identity != _identity(
            "candidate-cluster-triage-plan-identity/v1", _plan_unsigned(self)
        ):
            raise ValueError("triage plan identity does not match plan facts")
        object.__setattr__(self, "cluster_content_identities", content_identities)
        object.__setattr__(self, "cluster_partitions", partitions)
        object.__setattr__(self, "representative_refs", representatives)
        object.__setattr__(
            self,
            "eligible_candidate_refs",
            tuple(ref for ref, _identity_value in eligible),
        )
        object.__setattr__(
            self,
            "eligible_candidate_identities",
            tuple(identity for _ref, identity in eligible),
        )
        object.__setattr__(self, "selected_cluster_ids", selected)
        object.__setattr__(self, "uncertain_cluster_ids", uncertain)
        object.__setattr__(self, "effective_selected_cluster_ids", effective)
        object.__setattr__(self, "unselected_cluster_ids", unselected)
        object.__setattr__(
            self,
            "expanded_candidate_refs",
            tuple(ref for ref, _identity_value in expanded),
        )
        object.__setattr__(
            self,
            "expanded_candidate_identities",
            tuple(identity for _ref, identity in expanded),
        )
        object.__setattr__(self, "cluster_dispositions", dispositions)

    @property
    def is_fallback(self) -> bool:
        return bool(self.fallback_reason)

    def to_dict(self) -> JsonDict:
        payload = _plan_unsigned(self)
        payload["plan_identity"] = self.plan_identity
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "CandidateClusterTriagePlan":
        expected = frozenset(
            {
                "schema",
                "policy_version",
                "case_id",
                "seed_ref",
                "defect_fingerprint",
                "manifest_identity",
                "candidate_set_identity",
                "source_selection_identity",
                "request_identity",
                "decision_identity",
                "selection_identity",
                "eligible_candidate_refs",
                "eligible_candidate_identities",
                "eligible_set_identity",
                "cluster_content_identities",
                "cluster_partitions",
                "representative_refs",
                "partition_identity",
                "selected_cluster_ids",
                "uncertain_cluster_ids",
                "effective_selected_cluster_ids",
                "unselected_cluster_ids",
                "expanded_candidate_refs",
                "expanded_candidate_identities",
                "cluster_dispositions",
                "coverage_proof",
                "fallback_reason",
                "plan_identity",
            }
        )
        payload = _exact_mapping(value, expected, "candidate cluster triage plan")
        content = []
        for raw in _json_array(
            payload["cluster_content_identities"],
            "triage plan cluster content identities",
        ):
            item = _exact_mapping(
                raw,
                frozenset({"cluster_id", "cluster_content_identity"}),
                "triage plan cluster content identity",
            )
            content.append((item["cluster_id"], item["cluster_content_identity"]))
        return cls(
            schema=payload["schema"],
            policy_version=payload["policy_version"],
            case_id=payload["case_id"],
            seed_ref=payload["seed_ref"],
            defect_fingerprint=payload["defect_fingerprint"],
            manifest_identity=payload["manifest_identity"],
            candidate_set_identity=payload["candidate_set_identity"],
            source_selection_identity=payload["source_selection_identity"],
            request_identity=payload["request_identity"],
            decision_identity=payload["decision_identity"],
            selection_identity=payload["selection_identity"],
            eligible_candidate_refs=_string_tuple(
                payload["eligible_candidate_refs"],
                "triage plan eligible refs",
                require_json_array=True,
            ),
            eligible_candidate_identities=_string_tuple(
                payload["eligible_candidate_identities"],
                "triage plan eligible identities",
                require_json_array=True,
            ),
            eligible_set_identity=payload["eligible_set_identity"],
            cluster_content_identities=tuple(content),
            cluster_partitions=_canonical_partitions(
                payload["cluster_partitions"], require_json_array=True
            ),
            representative_refs=_canonical_representative_refs(
                payload["representative_refs"], require_json_array=True
            ),
            partition_identity=payload["partition_identity"],
            selected_cluster_ids=_string_tuple(
                payload["selected_cluster_ids"],
                "triage plan selected clusters",
                require_json_array=True,
            ),
            uncertain_cluster_ids=_string_tuple(
                payload["uncertain_cluster_ids"],
                "triage plan uncertain clusters",
                require_json_array=True,
            ),
            effective_selected_cluster_ids=_string_tuple(
                payload["effective_selected_cluster_ids"],
                "triage plan effective selected clusters",
                require_json_array=True,
            ),
            unselected_cluster_ids=_string_tuple(
                payload["unselected_cluster_ids"],
                "triage plan unselected clusters",
                require_json_array=True,
            ),
            expanded_candidate_refs=_string_tuple(
                payload["expanded_candidate_refs"],
                "triage plan expanded refs",
                require_json_array=True,
            ),
            expanded_candidate_identities=_string_tuple(
                payload["expanded_candidate_identities"],
                "triage plan expanded identities",
                require_json_array=True,
            ),
            cluster_dispositions=_canonical_dispositions(
                payload["cluster_dispositions"], require_json_array=True
            ),
            coverage_proof=CandidateClusterCoverageProof.from_dict(
                payload["coverage_proof"]
            ),
            fallback_reason=payload["fallback_reason"],
            plan_identity=payload["plan_identity"],
        )


def _directory_from_manifest(
    manifest: CandidateClusterManifest,
    eligible: Sequence[Tuple[str, str]],
) -> Tuple[Mapping[str, Any], ...]:
    eligible_by_ref = dict(eligible)
    offered = {
        ref
        for cluster in manifest.clusters
        for ref, disposition in cluster.member_dispositions
        if disposition == "offered"
    }
    if set(eligible_by_ref) != offered:
        raise ValueError(
            "triage eligible candidates must equal the Candidate Budget offered set"
        )
    facts_by_ref = {fact.ref: fact for fact in manifest.candidate_facts}
    if any(
        ref not in facts_by_ref
        or facts_by_ref[ref].candidate_identity != identity
        for ref, identity in eligible
    ):
        raise ValueError("triage eligible candidate identity is stale")
    directory = []
    for cluster in manifest.clusters:
        representatives = _canonical_representatives(cluster, facts_by_ref)
        cluster_eligible = tuple(
            sorted(
                (
                    (ref, identity)
                    for ref, identity in zip(
                        cluster.member_refs, cluster.member_identities
                    )
                    if ref in eligible_by_ref
                ),
                key=lambda item: (item[1], item[0]),
            )
        )
        if not cluster_eligible:
            continue
        members = tuple(
            sorted(
                zip(cluster.member_refs, cluster.member_identities),
                key=lambda item: (item[1], item[0]),
            )
        )
        evidence_refs = tuple(
            sorted(
                {
                    *cluster.grounding_refs,
                    *cluster.support_evidence_refs,
                    *cluster.opposition_evidence_refs,
                    *(
                        ref
                        for ref in representatives.values()
                        if ref is not None
                    ),
                }
            )
        )
        directory.append(
            {
                "cluster_id": cluster.cluster_id,
                "cluster_content_identity": _cluster_content_identity(
                    cluster, representatives
                ),
                "member_refs": [ref for ref, _identity_value in members],
                "member_identities": [identity for _ref, identity in members],
                "eligible_candidate_refs": [
                    ref for ref, _identity_value in cluster_eligible
                ],
                "eligible_candidate_identities": [
                    identity for _ref, identity in cluster_eligible
                ],
                "representatives": representatives,
                "evidence_refs": list(evidence_refs),
            }
        )
    return _canonical_directory(tuple(directory))


def _make_request(
    manifest: CandidateClusterManifest,
    eligible: Sequence[Tuple[str, str]],
) -> CandidateClusterTriageRequest:
    canonical_eligible = tuple(
        sorted(eligible, key=lambda item: (item[1], item[0]))
    )
    directory = _directory_from_manifest(manifest, canonical_eligible)
    eligible_identity = _eligible_set_identity(canonical_eligible)
    partition_identity = _partition_identity(eligible_identity, directory)
    unsigned = _request_unsigned(
        case_id=manifest.case_id,
        seed_ref=manifest.seed_ref,
        defect_fingerprint=manifest.defect_fingerprint,
        manifest_identity=manifest.manifest_identity,
        candidate_set_identity=manifest.candidate_set_identity,
        source_selection_identity=manifest.source_selection_identity,
        eligible=canonical_eligible,
        eligible_set_identity=eligible_identity,
        directory=directory,
        partition_identity=partition_identity,
    )
    return CandidateClusterTriageRequest(
        schema=CANDIDATE_CLUSTER_TRIAGE_REQUEST_SCHEMA,
        policy_version=CANDIDATE_CLUSTER_TRIAGE_POLICY,
        case_id=manifest.case_id,
        seed_ref=manifest.seed_ref,
        defect_fingerprint=manifest.defect_fingerprint,
        manifest_identity=manifest.manifest_identity,
        candidate_set_identity=manifest.candidate_set_identity,
        source_selection_identity=manifest.source_selection_identity,
        eligible_candidate_refs=tuple(ref for ref, _identity_value in canonical_eligible),
        eligible_candidate_identities=tuple(
            identity for _ref, identity in canonical_eligible
        ),
        eligible_set_identity=eligible_identity,
        cluster_directory=directory,
        partition_identity=partition_identity,
        request_identity=_identity(
            "candidate-cluster-triage-request-identity/v1", unsigned
        ),
    )


def build_candidate_cluster_triage_request(
    *,
    manifest: CandidateClusterManifest,
    eligible_candidates: Sequence[Any],
) -> CandidateClusterTriageRequest:
    """Bind exactly the existing Candidate Budget offered candidates."""
    if not isinstance(manifest, CandidateClusterManifest):
        raise TypeError("triage request manifest must be CandidateClusterManifest")
    if isinstance(eligible_candidates, (str, bytes)) or not isinstance(
        eligible_candidates, Sequence
    ):
        raise TypeError("triage eligible candidates must be an ordered sequence")
    eligible = tuple(
        (candidate.ref, candidate_identity(candidate))
        for candidate in eligible_candidates
    )
    _bindings(
        tuple(ref for ref, _identity_value in eligible),
        tuple(identity for _ref, identity in eligible),
        "triage eligible candidates",
    )
    return _make_request(manifest, eligible)


def build_candidate_cluster_triage_decision(
    *,
    request: CandidateClusterTriageRequest,
    dispositions: Mapping[str, str],
    rationales: Mapping[str, str],
    evidence_refs: Mapping[str, Sequence[str]],
) -> CandidateClusterTriageDecision:
    """Create a complete navigation-only triage judgment."""
    if not isinstance(request, CandidateClusterTriageRequest):
        raise TypeError("triage decision request is invalid")
    expected = set(request.cluster_ids)
    if (
        set(dispositions) != expected
        or set(rationales) != expected
        or set(evidence_refs) != expected
    ):
        raise ValueError("triage decision must cover every cluster exactly once")
    decisions = []
    for cluster_id in request.cluster_ids:
        refs = tuple(sorted(evidence_refs[cluster_id]))
        if not refs or not set(refs).issubset(
            set(request.evidence_refs_for(cluster_id))
        ):
            raise ValueError(
                "triage decision evidence must be present in its cluster directory"
            )
        decisions.append(
            {
                "cluster_id": cluster_id,
                "disposition": dispositions[cluster_id],
                "rationale": rationales[cluster_id],
                "evidence_refs": list(refs),
            }
        )
    canonical = _canonical_decisions(tuple(decisions))
    selection_identity = _selection_identity(
        request.request_identity, request.partition_identity, canonical
    )
    unsigned = {
        "schema": CANDIDATE_CLUSTER_TRIAGE_DECISION_SCHEMA,
        "policy_version": CANDIDATE_CLUSTER_TRIAGE_POLICY,
        "request_identity": request.request_identity,
        "partition_identity": request.partition_identity,
        "decisions": [_thaw(item) for item in canonical],
        "selection_identity": selection_identity,
    }
    return CandidateClusterTriageDecision(
        schema=CANDIDATE_CLUSTER_TRIAGE_DECISION_SCHEMA,
        policy_version=CANDIDATE_CLUSTER_TRIAGE_POLICY,
        request_identity=request.request_identity,
        partition_identity=request.partition_identity,
        decisions=canonical,
        selection_identity=selection_identity,
        judgment_identity=_identity(
            "candidate-cluster-triage-judgment/v1", unsigned
        ),
    )


def _coverage_proof(
    *,
    mode: str,
    request: CandidateClusterTriageRequest,
    selection_identity: str,
    selected_cluster_ids: Sequence[str],
    unselected_cluster_ids: Sequence[str],
    expanded: Sequence[Tuple[str, str]],
) -> CandidateClusterCoverageProof:
    expanded_identity = _identity(
        "candidate-cluster-triage-expanded-set/v1",
        _binding_payload(expanded),
    )
    unsigned = _coverage_unsigned(
        mode=mode,
        request_identity=request.request_identity,
        selection_identity=selection_identity,
        eligible_set_identity=request.eligible_set_identity,
        partition_identity=request.partition_identity,
        selected_cluster_ids=selected_cluster_ids,
        unselected_cluster_ids=unselected_cluster_ids,
        expanded=expanded,
        expanded_set_identity=expanded_identity,
        partition_complete=True,
        expansion_complete=True,
        original_identity_only=True,
    )
    return CandidateClusterCoverageProof(
        schema=CANDIDATE_CLUSTER_COVERAGE_PROOF_SCHEMA,
        policy_version=CANDIDATE_CLUSTER_TRIAGE_POLICY,
        mode=mode,
        request_identity=request.request_identity,
        selection_identity=selection_identity,
        eligible_set_identity=request.eligible_set_identity,
        partition_identity=request.partition_identity,
        selected_cluster_ids=tuple(selected_cluster_ids),
        unselected_cluster_ids=tuple(unselected_cluster_ids),
        expanded_candidate_refs=tuple(ref for ref, _identity_value in expanded),
        expanded_candidate_identities=tuple(
            identity for _ref, identity in expanded
        ),
        expanded_set_identity=expanded_identity,
        partition_complete=True,
        expansion_complete=True,
        original_identity_only=True,
        proof_identity=_identity(
            "candidate-cluster-coverage-proof-identity/v1", unsigned
        ),
    )


def _plan_projections(request: CandidateClusterTriageRequest):
    partitions = tuple(
        {
            "cluster_id": item["cluster_id"],
            "member_refs": list(item["member_refs"]),
            "member_identities": list(item["member_identities"]),
            "candidate_refs": list(item["eligible_candidate_refs"]),
            "candidate_identities": list(
                item["eligible_candidate_identities"]
            ),
        }
        for item in request.cluster_directory
    )
    representatives = tuple(
        {
            "cluster_id": item["cluster_id"],
            "refs": sorted(
                {
                    ref
                    for ref in item["representatives"].values()
                    if ref is not None
                }
            ),
        }
        for item in request.cluster_directory
    )
    return partitions, representatives


def _make_plan(
    *,
    request: CandidateClusterTriageRequest,
    decision_identity: str,
    selection_identity: str,
    selected: Sequence[str],
    uncertain: Sequence[str],
    unselected: Sequence[str],
    expanded: Sequence[Tuple[str, str]],
    dispositions: Sequence[Tuple[str, str]],
    fallback_reason: str,
) -> CandidateClusterTriagePlan:
    effective = tuple(sorted(set(selected) | set(uncertain)))
    partitions, representatives = _plan_projections(request)
    coverage = _coverage_proof(
        mode=FALLBACK_FULL_PAGING if fallback_reason else "triaged",
        request=request,
        selection_identity=selection_identity,
        selected_cluster_ids=() if fallback_reason else effective,
        unselected_cluster_ids=() if fallback_reason else unselected,
        expanded=expanded,
    )
    values = dict(
        schema=CANDIDATE_CLUSTER_TRIAGE_PLAN_SCHEMA,
        policy_version=CANDIDATE_CLUSTER_TRIAGE_POLICY,
        case_id=request.case_id,
        seed_ref=request.seed_ref,
        defect_fingerprint=request.defect_fingerprint,
        manifest_identity=request.manifest_identity,
        candidate_set_identity=request.candidate_set_identity,
        source_selection_identity=request.source_selection_identity,
        request_identity=request.request_identity,
        decision_identity=decision_identity,
        selection_identity=selection_identity,
        eligible_candidate_refs=request.eligible_candidate_refs,
        eligible_candidate_identities=request.eligible_candidate_identities,
        eligible_set_identity=request.eligible_set_identity,
        cluster_content_identities=request.cluster_content_identities,
        cluster_partitions=partitions,
        representative_refs=representatives,
        partition_identity=request.partition_identity,
        selected_cluster_ids=tuple(selected),
        uncertain_cluster_ids=tuple(uncertain),
        effective_selected_cluster_ids=effective,
        unselected_cluster_ids=tuple(unselected),
        expanded_candidate_refs=tuple(ref for ref, _identity_value in expanded),
        expanded_candidate_identities=tuple(
            identity for _ref, identity in expanded
        ),
        cluster_dispositions=tuple(dispositions),
        coverage_proof=coverage,
        fallback_reason=fallback_reason,
    )
    return CandidateClusterTriagePlan(
        **values,
        plan_identity=_identity(
            "candidate-cluster-triage-plan-identity/v1",
            _plan_unsigned_values(values),
        ),
    )


def build_candidate_cluster_triage_plan(
    *,
    request: CandidateClusterTriageRequest,
    decision: CandidateClusterTriageDecision,
) -> CandidateClusterTriagePlan:
    """Expand selected and uncertain clusters to original eligible members."""
    if not isinstance(request, CandidateClusterTriageRequest) or not isinstance(
        decision, CandidateClusterTriageDecision
    ):
        raise TypeError("triage plan inputs are invalid")
    if (
        decision.request_identity != request.request_identity
        or decision.partition_identity != request.partition_identity
        or {item["cluster_id"] for item in decision.decisions}
        != set(request.cluster_ids)
    ):
        raise ValueError("triage decision is incomplete or stale")
    for item in decision.decisions:
        if not set(item["evidence_refs"]).issubset(
            set(request.evidence_refs_for(item["cluster_id"]))
        ):
            raise ValueError("triage decision evidence is stale")
    selected = tuple(
        item["cluster_id"]
        for item in decision.decisions
        if item["disposition"] == SELECTED
    )
    uncertain = tuple(
        item["cluster_id"]
        for item in decision.decisions
        if item["disposition"] == UNCERTAIN
    )
    unselected = tuple(
        item["cluster_id"]
        for item in decision.decisions
        if item["disposition"] == UNSELECTED
    )
    effective = set(selected) | set(uncertain)
    eligible_by_ref = dict(
        zip(
            request.eligible_candidate_refs,
            request.eligible_candidate_identities,
        )
    )
    expanded = tuple(
        sorted(
            (
                (ref, eligible_by_ref[ref])
                for cluster_id in effective
                for ref in request.eligible_refs_for(cluster_id)
            ),
            key=lambda item: (item[1], item[0]),
        )
    )
    dispositions = tuple(
        (cluster_id, "selected_expanded")
        for cluster_id in selected
    ) + tuple(
        (cluster_id, "uncertain_expanded")
        for cluster_id in uncertain
    ) + tuple(
        (cluster_id, "unselected_deferred")
        for cluster_id in unselected
    )
    return _make_plan(
        request=request,
        decision_identity=decision.judgment_identity,
        selection_identity=decision.selection_identity,
        selected=selected,
        uncertain=uncertain,
        unselected=unselected,
        expanded=expanded,
        dispositions=dispositions,
        fallback_reason="",
    )


def _fallback_plan(
    request: CandidateClusterTriageRequest,
    *,
    decision_identity: str,
    reason: str,
) -> CandidateClusterTriagePlan:
    selection_identity = _identity(
        "candidate-cluster-triage-fallback-selection/v1",
        {
            "request_identity": request.request_identity,
            "mode": FALLBACK_FULL_PAGING,
        },
    )
    expanded = tuple(
        zip(
            request.eligible_candidate_refs,
            request.eligible_candidate_identities,
        )
    )
    return _make_plan(
        request=request,
        decision_identity=decision_identity,
        selection_identity=selection_identity,
        selected=(),
        uncertain=(),
        unselected=(),
        expanded=expanded,
        dispositions=tuple(
            (cluster_id, FALLBACK_FULL_PAGING)
            for cluster_id in request.cluster_ids
        ),
        fallback_reason=reason,
    )


def _observed_decision_identity(value: Any) -> str:
    if isinstance(value, CandidateClusterTriageDecision):
        return value.judgment_identity
    return _identity(
        "candidate-cluster-triage-observed-decision/v1",
        {"status": "untrusted_unparsed_decision"},
    )


def safe_build_candidate_cluster_triage_plan(
    *,
    request: CandidateClusterTriageRequest,
    decision: Any,
    manifest: Any,
) -> CandidateClusterTriagePlan:
    """Fail closed to the complete offered set on stale or malformed inputs."""
    if not isinstance(request, CandidateClusterTriageRequest):
        raise TypeError("safe triage requires a previously validated request")
    decision_identity = _observed_decision_identity(decision)
    try:
        restored_manifest = (
            manifest
            if isinstance(manifest, CandidateClusterManifest)
            else CandidateClusterManifest.from_dict(manifest)
        )
        eligible = tuple(
            zip(
                request.eligible_candidate_refs,
                request.eligible_candidate_identities,
            )
        )
        if _make_request(restored_manifest, eligible) != request:
            raise ValueError("triage manifest is stale")
    except Exception:
        return _fallback_plan(
            request,
            decision_identity=decision_identity,
            reason="manifest_validation_failed",
        )
    try:
        restored_decision = (
            decision
            if isinstance(decision, CandidateClusterTriageDecision)
            else CandidateClusterTriageDecision.from_dict(decision)
        )
        return build_candidate_cluster_triage_plan(
            request=request,
            decision=restored_decision,
        )
    except Exception:
        return _fallback_plan(
            request,
            decision_identity=decision_identity,
            reason="decision_or_coverage_validation_failed",
        )
