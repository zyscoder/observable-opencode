"""Deterministic shadow clustering for complete attribution candidates."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

from .candidate_episode import (
    AUTHORED_PLAN,
    CLOSURE,
    EXECUTION,
    VERIFICATION,
    classify_episode_role,
    extract_episode_key,
)
from .candidate_budget import candidate_identity
from .causal_retrieval import (
    canonical_candidate_route,
    global_authored_root_candidate_eligible,
    obligation_gaps_for_candidate,
)
from .causal_state import CausalCandidate
from .episodes import CausalEpisodeIndex
from .models import JsonDict, stable_json


CANDIDATE_CLUSTER_MANIFEST_SCHEMA = "candidate-cluster-manifest/v2"
CANDIDATE_CLUSTER_ALGORITHM = "deterministic-semantic-shadow/v2"
CANDIDATE_CLUSTER_IDENTITY_SCHEMA = (
    "candidate-cluster-manifest-identity/v1"
)
CANDIDATE_SET_IDENTITY_SCHEMA = "candidate-cluster-set-identity/v1"
CANDIDATE_CLUSTER_SHADOW_EVENT_SCHEMA = (
    "candidate-cluster-shadow-event/v1"
)
L0_EXACT_ACTION = "l0_exact_action"
L1_CONFIRMED_CAUSAL_EPISODE = "l1_confirmed_causal_episode"
L2_MATERIALIZATION_EPISODE = "l2_materialization_episode"
L3_STRUCTURAL_FALLBACK = "l3_structural_fallback"
SHADOW_ONLY = "shadow_only"
SHADOW_REASON = (
    "shadow_manifest_does_not_control_candidate_scheduling"
)

_LEVELS = frozenset(
    {
        L0_EXACT_ACTION,
        L1_CONFIRMED_CAUSAL_EPISODE,
        L2_MATERIALIZATION_EPISODE,
        L3_STRUCTURAL_FALLBACK,
    }
)
_ACTION_GROUP_KEYS = (
    "action_group_id",
    "actionGroupID",
    "actionGroupId",
)
_CALL_ID_KEYS = ("call_id", "callID", "callId", "tool_call_id")
_TOOL_KEYS = ("tool_name", "tool", "action", "chosen_action", "name")
_FILE_KEYS = (
    "file",
    "file_path",
    "filepath",
    "path",
    "modified_file",
    "modified_files",
    "files",
)
_SYMBOL_KEYS = (
    "symbol",
    "symbol_name",
    "function",
    "function_name",
    "class_name",
    "method",
)
_REPRESENTATIVE_KEYS = (
    "earliest_authored_plan",
    "latest_execution",
    "latest_verification",
    "latest_closure",
    "latest_root_eligible",
)
_HEX_DIGITS = frozenset("0123456789abcdef")
_MAX_STRUCTURED_METADATA_NODES = 4096
_MAX_STRUCTURED_METADATA_DEPTH = 64
_SHADOW_EVENT_KEYS = frozenset(
    {
        "kind",
        "event_schema",
        "seed_binding_identity",
        "source_selection_identity",
        "manifest_identity",
        "manifest",
        "behavior_impact",
    }
)


@dataclass(frozen=True)
class CandidateFact:
    ref: str
    candidate_identity: str
    discovered_rank: int
    position: int
    component: str
    event_type: str
    candidate_source: str
    edge_relation: str
    root_candidate_eligible: bool
    episode_role: str
    action_identity: str
    confirmed_episode_id: str
    confirmed_episode_member_count: int
    materialization_episode_key: str
    path_refs: Tuple[str, ...]
    path_shape: Tuple[str, ...]
    tool_names: Tuple[str, ...]
    file_refs: Tuple[str, ...]
    symbol_refs: Tuple[str, ...]
    restoration_obligation_ids: Tuple[str, ...]
    attribution_only_gap_identities: Tuple[str, ...]
    grouping_level: str
    grouping_key: str
    cluster_id: str

    def __post_init__(self) -> None:
        _require_ref(self.ref, "candidate fact ref")
        _require_identity(
            self.candidate_identity,
            "candidate fact identity",
        )
        if type(self.discovered_rank) is not int or self.discovered_rank < 0:
            raise ValueError(
                "candidate fact discovered_rank must be non-negative"
            )
        if type(self.position) is not int:
            raise ValueError("candidate fact position must be an integer")
        if (
            type(self.confirmed_episode_member_count) is not int
            or self.confirmed_episode_member_count < 0
        ):
            raise ValueError(
                "candidate fact confirmed episode member count is invalid"
            )
        if type(self.root_candidate_eligible) is not bool:
            raise ValueError(
                "candidate fact root_candidate_eligible must be boolean"
            )
        if self.grouping_level not in _LEVELS:
            raise ValueError("candidate fact grouping level is invalid")
        _require_text(self.grouping_key, "candidate fact grouping key")
        _require_cluster_id(self.cluster_id)
        _require_unique_strings(self.path_refs, "candidate fact path refs")
        _require_sorted_identities(
            self.attribution_only_gap_identities,
            "candidate fact attribution-only gap identities",
        )

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "candidate_identity": self.candidate_identity,
            "discovered_rank": self.discovered_rank,
            "position": self.position,
            "component": self.component,
            "event_type": self.event_type,
            "candidate_source": self.candidate_source,
            "edge_relation": self.edge_relation,
            "root_candidate_eligible": self.root_candidate_eligible,
            "episode_role": self.episode_role,
            "action_identity": self.action_identity,
            "confirmed_episode_id": self.confirmed_episode_id,
            "confirmed_episode_member_count": (
                self.confirmed_episode_member_count
            ),
            "materialization_episode_key": (
                self.materialization_episode_key
            ),
            "path_refs": list(self.path_refs),
            "path_shape": list(self.path_shape),
            "tool_names": list(self.tool_names),
            "file_refs": list(self.file_refs),
            "symbol_refs": list(self.symbol_refs),
            "restoration_obligation_ids": list(
                self.restoration_obligation_ids
            ),
            "attribution_only_gap_identities": list(
                self.attribution_only_gap_identities
            ),
            "grouping_level": self.grouping_level,
            "grouping_key": self.grouping_key,
            "cluster_id": self.cluster_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateFact":
        payload = _exact_mapping(
            value,
            frozenset(cls.__dataclass_fields__),
            "candidate fact",
        )
        return cls(
            ref=str(payload["ref"]),
            candidate_identity=str(payload["candidate_identity"]),
            discovered_rank=_exact_int(
                payload["discovered_rank"], "candidate fact discovered_rank"
            ),
            position=_exact_int(
                payload["position"], "candidate fact position"
            ),
            component=str(payload["component"]),
            event_type=str(payload["event_type"]),
            candidate_source=str(payload["candidate_source"]),
            edge_relation=str(payload["edge_relation"]),
            root_candidate_eligible=payload[
                "root_candidate_eligible"
            ],
            episode_role=str(payload["episode_role"]),
            action_identity=str(payload["action_identity"]),
            confirmed_episode_id=str(payload["confirmed_episode_id"]),
            confirmed_episode_member_count=_exact_int(
                payload["confirmed_episode_member_count"],
                "candidate fact confirmed episode member count",
            ),
            materialization_episode_key=str(
                payload["materialization_episode_key"]
            ),
            path_refs=_string_tuple(
                payload["path_refs"], "candidate fact path refs"
            ),
            path_shape=_string_tuple(
                payload["path_shape"], "candidate fact path shape"
            ),
            tool_names=_string_tuple(
                payload["tool_names"], "candidate fact tool names"
            ),
            file_refs=_string_tuple(
                payload["file_refs"], "candidate fact file refs"
            ),
            symbol_refs=_string_tuple(
                payload["symbol_refs"], "candidate fact symbol refs"
            ),
            restoration_obligation_ids=_string_tuple(
                payload["restoration_obligation_ids"],
                "candidate fact restoration obligation IDs",
            ),
            attribution_only_gap_identities=_string_tuple(
                payload["attribution_only_gap_identities"],
                "candidate fact attribution-only gap identities",
            ),
            grouping_level=str(payload["grouping_level"]),
            grouping_key=str(payload["grouping_key"]),
            cluster_id=str(payload["cluster_id"]),
        )


@dataclass(frozen=True)
class CandidateCluster:
    cluster_id: str
    grouping_level: str
    grouping_key: str
    grouping_reason: str
    grounding_refs: Tuple[str, ...]
    member_refs: Tuple[str, ...]
    member_identities: Tuple[str, ...]
    root_eligible_refs: Tuple[str, ...]
    representatives: Mapping[str, Optional[str]]
    role_distribution: Tuple[Tuple[str, int], ...]
    event_type_distribution: Tuple[Tuple[str, int], ...]
    component_distribution: Tuple[Tuple[str, int], ...]
    source_distribution: Tuple[Tuple[str, int], ...]
    action_identity_distribution: Tuple[Tuple[str, int], ...]
    obligation_distribution: Tuple[Tuple[str, int], ...]
    attribution_only_gap_identities: Tuple[str, ...]
    support_evidence_refs: Tuple[str, ...]
    opposition_evidence_refs: Tuple[str, ...]
    member_dispositions: Tuple[Tuple[str, str], ...]
    expansion_status: str = SHADOW_ONLY
    expansion_reason: str = SHADOW_REASON

    def __post_init__(self) -> None:
        _require_cluster_id(self.cluster_id)
        if self.grouping_level not in _LEVELS:
            raise ValueError("candidate cluster grouping level is invalid")
        _require_text(self.grouping_key, "candidate cluster grouping key")
        if not self.member_refs:
            raise ValueError("candidate cluster must contain members")
        _require_unique_strings(
            self.member_refs, "candidate cluster member refs"
        )
        if len(self.member_identities) != len(self.member_refs):
            raise ValueError(
                "candidate cluster member identity count does not match members"
            )
        for identity in self.member_identities:
            _require_identity(identity, "candidate cluster member identity")
        if not set(self.root_eligible_refs).issubset(self.member_refs):
            raise ValueError(
                "candidate cluster root eligible refs must be members"
            )
        if set(self.representatives) != set(_REPRESENTATIVE_KEYS):
            raise ValueError(
                "candidate cluster representatives schema mismatch"
            )
        if any(
            ref is not None and ref not in self.member_refs
            for ref in self.representatives.values()
        ):
            raise ValueError(
                "candidate cluster representative must be a member"
            )
        _require_sorted_identities(
            self.attribution_only_gap_identities,
            "candidate cluster attribution-only gap identities",
        )
        dispositions = dict(self.member_dispositions)
        if (
            len(dispositions) != len(self.member_dispositions)
            or set(dispositions) != set(self.member_refs)
        ):
            raise ValueError(
                "candidate cluster member dispositions are incomplete"
            )
        if self.expansion_status != SHADOW_ONLY:
            raise ValueError(
                "candidate cluster expansion status must remain shadow_only"
            )
        if self.expansion_reason != SHADOW_REASON:
            raise ValueError(
                "candidate cluster shadow expansion reason is invalid"
            )
        object.__setattr__(
            self,
            "representatives",
            MappingProxyType(dict(self.representatives)),
        )

    def to_dict(self) -> JsonDict:
        return {
            "cluster_id": self.cluster_id,
            "grouping_level": self.grouping_level,
            "grouping_key": self.grouping_key,
            "grouping_reason": self.grouping_reason,
            "grounding_refs": list(self.grounding_refs),
            "member_refs": list(self.member_refs),
            "member_identities": list(self.member_identities),
            "root_eligible_refs": list(self.root_eligible_refs),
            "representatives": dict(self.representatives),
            "role_distribution": dict(self.role_distribution),
            "event_type_distribution": dict(
                self.event_type_distribution
            ),
            "component_distribution": dict(
                self.component_distribution
            ),
            "source_distribution": dict(self.source_distribution),
            "action_identity_distribution": dict(
                self.action_identity_distribution
            ),
            "obligation_distribution": dict(
                self.obligation_distribution
            ),
            "attribution_only_gap_identities": list(
                self.attribution_only_gap_identities
            ),
            "support_evidence_refs": list(self.support_evidence_refs),
            "opposition_evidence_refs": list(
                self.opposition_evidence_refs
            ),
            "member_dispositions": dict(self.member_dispositions),
            "expansion_status": self.expansion_status,
            "expansion_reason": self.expansion_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateCluster":
        payload = _exact_mapping(
            value,
            frozenset(cls.__dataclass_fields__),
            "candidate cluster",
        )
        return cls(
            cluster_id=str(payload["cluster_id"]),
            grouping_level=str(payload["grouping_level"]),
            grouping_key=str(payload["grouping_key"]),
            grouping_reason=str(payload["grouping_reason"]),
            grounding_refs=_string_tuple(
                payload["grounding_refs"],
                "candidate cluster grounding refs",
            ),
            member_refs=_string_tuple(
                payload["member_refs"], "candidate cluster member refs"
            ),
            member_identities=_string_tuple(
                payload["member_identities"],
                "candidate cluster member identities",
            ),
            root_eligible_refs=_string_tuple(
                payload["root_eligible_refs"],
                "candidate cluster root eligible refs",
            ),
            representatives=_optional_string_mapping(
                payload["representatives"],
                "candidate cluster representatives",
            ),
            role_distribution=_count_distribution(
                payload["role_distribution"],
                "candidate cluster role distribution",
            ),
            event_type_distribution=_count_distribution(
                payload["event_type_distribution"],
                "candidate cluster event type distribution",
            ),
            component_distribution=_count_distribution(
                payload["component_distribution"],
                "candidate cluster component distribution",
            ),
            source_distribution=_count_distribution(
                payload["source_distribution"],
                "candidate cluster source distribution",
            ),
            action_identity_distribution=_count_distribution(
                payload["action_identity_distribution"],
                "candidate cluster action identity distribution",
            ),
            obligation_distribution=_count_distribution(
                payload["obligation_distribution"],
                "candidate cluster obligation distribution",
            ),
            attribution_only_gap_identities=_string_tuple(
                payload["attribution_only_gap_identities"],
                "candidate cluster attribution-only gap identities",
            ),
            support_evidence_refs=_string_tuple(
                payload["support_evidence_refs"],
                "candidate cluster support evidence refs",
            ),
            opposition_evidence_refs=_string_tuple(
                payload["opposition_evidence_refs"],
                "candidate cluster opposition evidence refs",
            ),
            member_dispositions=_string_mapping_tuple(
                payload["member_dispositions"],
                "candidate cluster member dispositions",
            ),
            expansion_status=str(payload["expansion_status"]),
            expansion_reason=str(payload["expansion_reason"]),
        )


@dataclass(frozen=True)
class CandidateClusterManifest:
    schema: str
    algorithm_version: str
    case_id: str
    seed_ref: str
    defect_fingerprint: str
    discovered_count: int
    discovered_refs: Tuple[str, ...]
    candidate_set_identity: str
    source_selection_identity: str
    candidate_facts: Tuple[CandidateFact, ...]
    clusters: Tuple[CandidateCluster, ...]
    manifest_identity: str

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_CLUSTER_MANIFEST_SCHEMA:
            raise ValueError("candidate cluster manifest schema is invalid")
        if self.algorithm_version != CANDIDATE_CLUSTER_ALGORITHM:
            raise ValueError(
                "candidate cluster manifest algorithm is invalid"
            )
        _require_ref(self.seed_ref, "candidate cluster manifest seed ref")
        _require_text(
            self.defect_fingerprint,
            "candidate cluster manifest defect fingerprint",
        )
        if (
            type(self.discovered_count) is not int
            or self.discovered_count < 0
        ):
            raise ValueError(
                "candidate cluster manifest discovered count is invalid"
            )
        if self.discovered_count != len(self.discovered_refs):
            raise ValueError(
                "candidate cluster manifest discovered count mismatch"
            )
        _require_unique_strings(
            self.discovered_refs,
            "candidate cluster manifest discovered refs",
        )
        _require_identity(
            self.candidate_set_identity,
            "candidate cluster set identity",
        )
        _require_identity(
            self.source_selection_identity,
            "candidate cluster source selection identity",
        )
        _require_identity(
            self.manifest_identity,
            "candidate cluster manifest identity",
        )
        fact_refs = tuple(value.ref for value in self.candidate_facts)
        if fact_refs != self.discovered_refs:
            raise ValueError(
                "candidate cluster facts must preserve discovered order"
            )
        cluster_ids = [value.cluster_id for value in self.clusters]
        if len(cluster_ids) != len(set(cluster_ids)):
            raise ValueError(
                "candidate cluster manifest repeats cluster identity"
            )
        member_refs = [
            ref for cluster in self.clusters for ref in cluster.member_refs
        ]
        if (
            len(member_refs) != len(set(member_refs))
            or set(member_refs) != set(self.discovered_refs)
        ):
            raise ValueError(
                "candidate cluster manifest membership is not one-to-one"
            )
        cluster_by_ref = {
            ref: cluster.cluster_id
            for cluster in self.clusters
            for ref in cluster.member_refs
        }
        if any(
            cluster_by_ref.get(fact.ref) != fact.cluster_id
            for fact in self.candidate_facts
        ):
            raise ValueError(
                "candidate cluster manifest fact membership is inconsistent"
            )
        facts_by_ref = {fact.ref: fact for fact in self.candidate_facts}
        for cluster in self.clusters:
            expected_gap_identities = tuple(
                sorted(
                    {
                        identity
                        for ref in cluster.member_refs
                        for identity in facts_by_ref[
                            ref
                        ].attribution_only_gap_identities
                    }
                )
            )
            if (
                cluster.attribution_only_gap_identities
                != expected_gap_identities
            ):
                raise ValueError(
                    "candidate cluster gap identities contradict members"
                )
        expected_set_identity = _identity(
            CANDIDATE_SET_IDENTITY_SCHEMA,
            [
                {
                    "ref": fact.ref,
                    "candidate_identity": fact.candidate_identity,
                }
                for fact in self.candidate_facts
            ],
        )
        if self.candidate_set_identity != expected_set_identity:
            raise ValueError(
                "candidate cluster set identity does not match candidates"
            )
        if self.manifest_identity != _identity(
            CANDIDATE_CLUSTER_IDENTITY_SCHEMA,
            self._unsigned_payload(),
        ):
            raise ValueError(
                "candidate cluster manifest identity does not match facts"
            )

    def _unsigned_payload(self) -> JsonDict:
        return {
            "schema": self.schema,
            "algorithm_version": self.algorithm_version,
            "case_id": self.case_id,
            "seed_ref": self.seed_ref,
            "defect_fingerprint": self.defect_fingerprint,
            "discovered_count": self.discovered_count,
            "discovered_refs": list(self.discovered_refs),
            "candidate_set_identity": self.candidate_set_identity,
            "source_selection_identity": (
                self.source_selection_identity
            ),
            "candidate_facts": [
                value.to_dict() for value in self.candidate_facts
            ],
            "clusters": [value.to_dict() for value in self.clusters],
        }

    def to_dict(self) -> JsonDict:
        return {
            **self._unsigned_payload(),
            "manifest_identity": self.manifest_identity,
        }

    def assert_graph_references(self, graph: Any) -> None:
        if self.case_id != str(getattr(graph, "case_id", "") or ""):
            raise ValueError(
                "candidate cluster manifest case does not match graph"
            )
        for ref in (
            self.seed_ref,
            *self.discovered_refs,
            *(
                ref
                for fact in self.candidate_facts
                for ref in fact.path_refs
            ),
            *(
                ref
                for cluster in self.clusters
                for ref in (
                    *cluster.grounding_refs,
                    *cluster.support_evidence_refs,
                    *cluster.opposition_evidence_refs,
                )
            ),
        ):
            resolved = graph.resolve(ref) or ref
            if (
                resolved not in graph.nodes
                or not graph.active_revision_evidence_eligible(resolved)
            ):
                raise ValueError(
                    "candidate cluster manifest ref is not in the active "
                    "graph: {0}".format(ref)
                )
        episode_index = CausalEpisodeIndex.from_graph(graph)
        facts_by_episode = Counter(
            fact.confirmed_episode_id for fact in self.candidate_facts
        )
        for fact in self.candidate_facts:
            node = graph.hydrate_node(fact.ref)
            episode = episode_index.episode_for(fact.ref)
            path_nodes = tuple(
                graph.nodes[ref] for ref in fact.path_refs
            )
            materialization_key = (
                extract_episode_key(node, path_nodes) or ""
            )
            if (
                fact.position != graph.position(fact.ref)
                or fact.component
                != node.component.strip().lower()
                or fact.event_type
                != node.event_type.strip().lower()
                or fact.root_candidate_eligible
                != global_authored_root_candidate_eligible(
                    graph, fact.ref
                )
                or fact.episode_role
                != classify_episode_role(node)
                or fact.action_identity
                != recorded_action_identity(node)
                or fact.confirmed_episode_id != episode.episode_id
                or fact.confirmed_episode_member_count
                != len(episode.member_refs)
                or fact.materialization_episode_key
                != materialization_key
                or fact.path_shape
                != _path_shape(graph, fact.path_refs)
            ):
                raise ValueError(
                    "candidate cluster manifest fact contradicts active "
                    "graph semantics: {0}".format(fact.ref)
                )
            if fact.action_identity:
                expected_level = L0_EXACT_ACTION
                expected_key = fact.action_identity
            elif facts_by_episode[fact.confirmed_episode_id] > 1:
                expected_level = L1_CONFIRMED_CAUSAL_EPISODE
                expected_key = fact.confirmed_episode_id
            elif fact.materialization_episode_key:
                expected_level = L2_MATERIALIZATION_EPISODE
                expected_key = fact.materialization_episode_key
            else:
                expected_level = L3_STRUCTURAL_FALLBACK
                expected_key = "structural:v1:{0}".format(
                    _digest(_fact_structural_signature(fact))[:20]
                )
            if (
                fact.grouping_level != expected_level
                or fact.grouping_key != expected_key
                or fact.cluster_id
                != _cluster_id(expected_level, expected_key)
            ):
                raise ValueError(
                    "candidate cluster manifest grouping contradicts "
                    "active graph semantics: {0}".format(fact.ref)
                )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        graph: Any | None = None,
    ) -> "CandidateClusterManifest":
        payload = _exact_mapping(
            value,
            frozenset(cls.__dataclass_fields__),
            "candidate cluster manifest",
        )
        manifest = cls(
            schema=str(payload["schema"]),
            algorithm_version=str(payload["algorithm_version"]),
            case_id=str(payload["case_id"]),
            seed_ref=str(payload["seed_ref"]),
            defect_fingerprint=str(payload["defect_fingerprint"]),
            discovered_count=_exact_int(
                payload["discovered_count"],
                "candidate cluster manifest discovered count",
            ),
            discovered_refs=_string_tuple(
                payload["discovered_refs"],
                "candidate cluster manifest discovered refs",
            ),
            candidate_set_identity=str(
                payload["candidate_set_identity"]
            ),
            source_selection_identity=str(
                payload["source_selection_identity"]
            ),
            candidate_facts=tuple(
                CandidateFact.from_dict(item)
                for item in _mapping_sequence(
                    payload["candidate_facts"],
                    "candidate cluster manifest candidate facts",
                )
            ),
            clusters=tuple(
                CandidateCluster.from_dict(item)
                for item in _mapping_sequence(
                    payload["clusters"],
                    "candidate cluster manifest clusters",
                )
            ),
            manifest_identity=str(payload["manifest_identity"]),
        )
        if graph is not None:
            manifest.assert_graph_references(graph)
        return manifest


def build_candidate_cluster_manifest(
    *,
    graph: Any,
    candidates: Iterable[CausalCandidate],
    candidate_paths: Mapping[str, Sequence[str]],
    candidate_audit: Sequence[Mapping[str, Any]],
    source_selection_identity: str,
    seed_ref: str,
    defect_fingerprint: str,
    restoration_obligations: Sequence[Any] = (),
) -> CandidateClusterManifest:
    """Build a complete audit-only manifest without changing scheduling."""
    input_candidates = tuple(candidates)
    canonical = _canonical_candidates(graph, input_candidates)
    canonical_seed = graph.resolve(seed_ref) or str(seed_ref)
    episode_index = CausalEpisodeIndex.from_graph(graph)
    candidate_refs = {candidate.ref for candidate in canonical}
    canonical_paths = {
        candidate.ref: _canonical_path(
            graph,
            candidate_paths.get(candidate.ref) or (candidate.ref,),
        )
        for candidate in canonical
    }
    audit_by_ref: Dict[str, list[Mapping[str, Any]]] = {}
    audit_bindings = []
    for item in candidate_audit:
        if not isinstance(item, Mapping):
            raise TypeError("candidate cluster manifest audit entry must be an object")
        binding = (
            str(item.get("ref") or ""),
            str(item.get("candidate_identity") or ""),
        )
        audit_bindings.append(binding)
        audit_by_ref.setdefault(
            binding[0],
            [],
        ).append(item)
    input_bindings = tuple(
        (candidate.ref, candidate_identity(candidate))
        for candidate in input_candidates
    )
    if (
        len(audit_bindings) != len(input_bindings)
        or len(audit_bindings) != len(set(audit_bindings))
        or set(audit_bindings) != set(input_bindings)
    ):
        raise ValueError(
            "candidate cluster manifest audit must contain exactly one entry per candidate"
        )
    audit_by_candidate_identity = {
        str(item.get("candidate_identity") or ""): item
        for item in candidate_audit
    }
    if len(audit_by_candidate_identity) != len(input_bindings):
        raise ValueError(
            "candidate cluster manifest audit contains a duplicate candidate identity"
        )
    if set(audit_by_ref) != candidate_refs:
        raise ValueError(
            "candidate cluster manifest audit must cover every candidate"
        )
    _require_identity(
        source_selection_identity,
        "candidate cluster source selection identity",
    )
    for rank, candidate in enumerate(canonical):
        if not any(
            audit.get("candidate_identity")
            == candidate_identity(candidate)
            for audit in audit_by_ref[candidate.ref]
        ):
            raise ValueError(
                "candidate cluster manifest audit contradicts candidate "
                "identity or discovered rank"
            )
    action_identity_by_ref = {
        candidate.ref: recorded_action_identity(candidate.node)
        for candidate in canonical
    }
    confirmed_episode_by_ref = {
        candidate.ref: episode_index.episode_for(candidate.ref)
        for candidate in canonical
    }
    materialization_by_ref = {
        candidate.ref: (
            extract_episode_key(
                candidate.node,
                tuple(graph.nodes[ref] for ref in canonical_paths[candidate.ref]),
            )
            or ""
        )
        for candidate in canonical
    }
    root_eligible_by_ref = {
        candidate.ref: global_authored_root_candidate_eligible(
            graph, candidate.ref
        )
        for candidate in canonical
    }
    confirmed_candidate_members: Dict[str, Tuple[str, ...]] = {}
    for candidate in canonical:
        episode = confirmed_episode_by_ref[candidate.ref]
        members = tuple(
            value.ref
            for value in canonical
            if confirmed_episode_by_ref[value.ref].episode_id
            == episode.episode_id
        )
        confirmed_candidate_members[candidate.ref] = members

    provisional = []
    for rank, candidate in enumerate(canonical):
        path = canonical_paths[candidate.ref]
        action_identity = action_identity_by_ref[candidate.ref]
        episode = confirmed_episode_by_ref[candidate.ref]
        materialization_key = materialization_by_ref[candidate.ref]
        structure = _structural_signature(
            candidate,
            path_shape=_path_shape(graph, path),
            root_eligible=root_eligible_by_ref[candidate.ref],
        )
        if action_identity:
            level = L0_EXACT_ACTION
            key = action_identity
        elif len(confirmed_candidate_members[candidate.ref]) > 1:
            level = L1_CONFIRMED_CAUSAL_EPISODE
            key = episode.episode_id
        elif materialization_key:
            level = L2_MATERIALIZATION_EPISODE
            key = materialization_key
        else:
            level = L3_STRUCTURAL_FALLBACK
            key = "structural:v1:{0}".format(
                _digest(structure)[:20]
            )
        cluster_id = _cluster_id(level, key)
        provisional.append(
            CandidateFact(
                ref=candidate.ref,
                candidate_identity=candidate_identity(candidate),
                discovered_rank=rank,
                position=graph.position(candidate.ref),
                component=candidate.node.component.strip().lower(),
                event_type=candidate.node.event_type.strip().lower(),
                candidate_source=candidate.source,
                edge_relation=str(
                    candidate.edge.get("relation")
                    or candidate.edge.get("edge_type")
                    or candidate.edge.get("type")
                    or ""
                ),
                root_candidate_eligible=(
                    root_eligible_by_ref[candidate.ref]
                ),
                episode_role=classify_episode_role(candidate),
                action_identity=action_identity,
                confirmed_episode_id=episode.episode_id,
                confirmed_episode_member_count=len(
                    episode.member_refs
                ),
                materialization_episode_key=materialization_key,
                path_refs=path,
                path_shape=_path_shape(graph, path),
                tool_names=_structured_values(
                    candidate.node.data, _TOOL_KEYS
                ),
                file_refs=_structured_values(
                    candidate.node.data, _FILE_KEYS
                ),
                symbol_refs=_structured_values(
                    candidate.node.data, _SYMBOL_KEYS
                ),
                restoration_obligation_ids=tuple(
                    sorted(
                        {
                            *_matching_obligation_ids(
                                candidate.ref,
                                path,
                                restoration_obligations,
                            ),
                            *(
                                gap.obligation_id
                                for gap in obligation_gaps_for_candidate(
                                    candidate
                                )
                            ),
                        }
                    )
                ),
                attribution_only_gap_identities=tuple(
                    gap.identity
                    for gap in obligation_gaps_for_candidate(candidate)
                ),
                grouping_level=level,
                grouping_key=key,
                cluster_id=cluster_id,
            )
        )

    facts = tuple(provisional)
    candidates_by_ref = {
        candidate.ref: candidate for candidate in canonical
    }
    clusters = []
    for cluster_id in dict.fromkeys(fact.cluster_id for fact in facts):
        members = tuple(
            fact for fact in facts if fact.cluster_id == cluster_id
        )
        member_refs = tuple(fact.ref for fact in members)
        roles = {fact.ref: fact.episode_role for fact in members}
        root_refs = tuple(
            fact.ref for fact in members if fact.root_candidate_eligible
        )
        support_refs = _grounded_evidence_refs(
            graph,
            candidates_by_ref,
            member_refs,
            (
                "evidence_refs",
                "support_evidence_refs",
                "direct_evidence_refs",
            ),
        )
        opposition_refs = _grounded_evidence_refs(
            graph,
            candidates_by_ref,
            member_refs,
            (
                "opposition_evidence_refs",
                "opposing_evidence_refs",
                "counter_evidence_refs",
            ),
        )
        level = members[0].grouping_level
        clusters.append(
            CandidateCluster(
                cluster_id=cluster_id,
                grouping_level=level,
                grouping_key=members[0].grouping_key,
                grouping_reason=_grouping_reason(level),
                grounding_refs=_cluster_grounding_refs(
                    graph, members
                ),
                member_refs=member_refs,
                member_identities=tuple(
                    fact.candidate_identity for fact in members
                ),
                root_eligible_refs=root_refs,
                representatives={
                    "earliest_authored_plan": _earliest_role(
                        members, roles, AUTHORED_PLAN
                    ),
                    "latest_execution": _latest_role(
                        members, roles, EXECUTION
                    ),
                    "latest_verification": _latest_role(
                        members, roles, VERIFICATION
                    ),
                    "latest_closure": _latest_role(
                        members, roles, CLOSURE
                    ),
                    "latest_root_eligible": (
                        root_refs[-1] if root_refs else None
                    ),
                },
                role_distribution=_distribution(
                    fact.episode_role for fact in members
                ),
                event_type_distribution=_distribution(
                    fact.event_type for fact in members
                ),
                component_distribution=_distribution(
                    fact.component for fact in members
                ),
                source_distribution=_distribution(
                    fact.candidate_source for fact in members
                ),
                action_identity_distribution=_distribution(
                    fact.action_identity
                    for fact in members
                    if fact.action_identity
                ),
                obligation_distribution=_distribution(
                    obligation_id
                    for fact in members
                    for obligation_id in fact.restoration_obligation_ids
                ),
                attribution_only_gap_identities=tuple(
                    sorted(
                        {
                            identity
                            for fact in members
                            for identity in (
                                fact.attribution_only_gap_identities
                            )
                        }
                    )
                ),
                support_evidence_refs=support_refs,
                opposition_evidence_refs=opposition_refs,
                member_dispositions=tuple(
                    (
                        fact.ref,
                        str(
                            audit_by_candidate_identity[
                                fact.candidate_identity
                            ].get("disposition")
                            or ""
                        ),
                    )
                    for fact in members
                ),
            )
        )

    candidate_set_identity = _identity(
        CANDIDATE_SET_IDENTITY_SCHEMA,
        [
            {
                "ref": fact.ref,
                "candidate_identity": fact.candidate_identity,
            }
            for fact in facts
        ],
    )
    unsigned = {
        "schema": CANDIDATE_CLUSTER_MANIFEST_SCHEMA,
        "algorithm_version": CANDIDATE_CLUSTER_ALGORITHM,
        "case_id": str(getattr(graph, "case_id", "") or ""),
        "seed_ref": canonical_seed,
        "defect_fingerprint": defect_fingerprint,
        "discovered_count": len(facts),
        "discovered_refs": [fact.ref for fact in facts],
        "candidate_set_identity": candidate_set_identity,
        "source_selection_identity": source_selection_identity,
        "candidate_facts": [fact.to_dict() for fact in facts],
        "clusters": [cluster.to_dict() for cluster in clusters],
    }
    manifest = CandidateClusterManifest(
        schema=CANDIDATE_CLUSTER_MANIFEST_SCHEMA,
        algorithm_version=CANDIDATE_CLUSTER_ALGORITHM,
        case_id=unsigned["case_id"],
        seed_ref=canonical_seed,
        defect_fingerprint=defect_fingerprint,
        discovered_count=len(facts),
        discovered_refs=tuple(fact.ref for fact in facts),
        candidate_set_identity=candidate_set_identity,
        source_selection_identity=source_selection_identity,
        candidate_facts=facts,
        clusters=tuple(clusters),
        manifest_identity=_identity(
            CANDIDATE_CLUSTER_IDENTITY_SCHEMA, unsigned
        ),
    )
    manifest.assert_graph_references(graph)
    return manifest


def build_candidate_cluster_shadow_event(
    *,
    manifest: CandidateClusterManifest,
    seed_binding_identity: str,
) -> JsonDict:
    _require_text(
        seed_binding_identity,
        "candidate cluster shadow seed binding identity",
    )
    return {
        "kind": "candidate_cluster_manifest_shadow",
        "event_schema": CANDIDATE_CLUSTER_SHADOW_EVENT_SCHEMA,
        "seed_binding_identity": seed_binding_identity,
        "source_selection_identity": (
            manifest.source_selection_identity
        ),
        "manifest_identity": manifest.manifest_identity,
        "manifest": manifest.to_dict(),
        "behavior_impact": "none_offline_analysis_only",
    }


def validate_candidate_cluster_shadow_event(
    value: Any,
    *,
    graph: Any | None = None,
    expected_seed_binding_identity: str = "",
    expected_source_selection_identity: str = "",
) -> CandidateClusterManifest:
    event = _exact_mapping(
        value,
        _SHADOW_EVENT_KEYS,
        "candidate cluster shadow event",
    )
    if (
        event["kind"] != "candidate_cluster_manifest_shadow"
        or event["event_schema"]
        != CANDIDATE_CLUSTER_SHADOW_EVENT_SCHEMA
        or event["behavior_impact"]
        != "none_offline_analysis_only"
    ):
        raise ValueError(
            "candidate cluster shadow event contract is invalid"
        )
    seed_binding_identity = _require_text(
        event["seed_binding_identity"],
        "candidate cluster shadow seed binding identity",
    )
    manifest = CandidateClusterManifest.from_dict(
        event["manifest"],
        graph=graph,
    )
    if (
        event["source_selection_identity"]
        != manifest.source_selection_identity
        or event["manifest_identity"] != manifest.manifest_identity
    ):
        raise ValueError(
            "candidate cluster shadow event does not match its manifest"
        )
    if (
        expected_seed_binding_identity
        and seed_binding_identity != expected_seed_binding_identity
    ):
        raise ValueError(
            "candidate cluster shadow event seed binding is inconsistent"
        )
    if (
        expected_source_selection_identity
        and manifest.source_selection_identity
        != expected_source_selection_identity
    ):
        raise ValueError(
            "candidate cluster shadow event source selection is inconsistent"
        )
    return manifest


def recorded_action_identity(node: Any) -> str:
    data = node.data if isinstance(node.data, Mapping) else {}
    metadata = (
        data.get("metadata")
        if isinstance(data.get("metadata"), Mapping)
        else {}
    )
    for namespace, keys in (
        ("action_group_id", _ACTION_GROUP_KEYS),
        ("call_id", _CALL_ID_KEYS),
    ):
        for container in (data, metadata):
            for key in keys:
                value = str(container.get(key) or "").strip()
                if value:
                    return "{0}:{1}".format(namespace, value)
    return ""


def _canonical_candidates(
    graph: Any,
    candidates: Iterable[CausalCandidate],
) -> Tuple[CausalCandidate, ...]:
    values = tuple(candidates)
    grouped: Dict[str, list[CausalCandidate]] = {}
    order = []
    for candidate in values:
        ref = graph.resolve(candidate.ref) or candidate.ref
        if (
            ref not in graph.nodes
            or not graph.active_revision_evidence_eligible(ref)
        ):
            raise ValueError(
                "candidate cluster candidate is not in the active graph: "
                "{0}".format(candidate.ref)
            )
        if ref not in grouped:
            order.append(ref)
            grouped[ref] = []
        grouped[ref].append(
            candidate
            if ref == candidate.ref
            else CausalCandidate(
                ref=ref,
                node=graph.hydrate_node(ref),
                source=candidate.source,
                edge=dict(candidate.edge),
                score=candidate.score,
                evidence_refs=candidate.evidence_refs,
            )
        )
    return tuple(
        canonical_candidate_route(graph, ref, grouped[ref])
        for ref in order
    )


def _canonical_path(
    graph: Any, path: Sequence[str]
) -> Tuple[str, ...]:
    output = []
    for raw_ref in path:
        ref = graph.resolve(str(raw_ref)) or str(raw_ref)
        if (
            ref not in graph.nodes
            or not graph.active_revision_evidence_eligible(ref)
        ):
            raise ValueError(
                "candidate cluster path ref is not in the active graph: "
                "{0}".format(raw_ref)
            )
        if not output or output[-1] != ref:
            output.append(ref)
    if not output:
        raise ValueError("candidate cluster path must not be empty")
    return tuple(output)


def _path_shape(graph: Any, path: Sequence[str]) -> Tuple[str, ...]:
    return tuple(
        "{0}:{1}".format(
            graph.nodes[ref].component.strip().lower(),
            graph.nodes[ref].event_type.strip().lower(),
        )
        for ref in path[1:]
    )


def _structural_signature(
    candidate: CausalCandidate,
    *,
    path_shape: Sequence[str],
    root_eligible: bool,
) -> JsonDict:
    return {
        "component": candidate.node.component.strip().lower(),
        "event_type": candidate.node.event_type.strip().lower(),
        "candidate_source": candidate.source,
        "edge_relation": str(
            candidate.edge.get("relation")
            or candidate.edge.get("edge_type")
            or candidate.edge.get("type")
            or ""
        ),
        "root_candidate_eligible": root_eligible,
        "episode_role": classify_episode_role(candidate),
        "tool_names": list(
            _structured_values(candidate.node.data, _TOOL_KEYS)
        ),
        "file_refs": list(
            _structured_values(candidate.node.data, _FILE_KEYS)
        ),
        "symbol_refs": list(
            _structured_values(candidate.node.data, _SYMBOL_KEYS)
        ),
        "path_shape": list(path_shape),
    }


def _fact_structural_signature(fact: CandidateFact) -> JsonDict:
    return {
        "component": fact.component,
        "event_type": fact.event_type,
        "candidate_source": fact.candidate_source,
        "edge_relation": fact.edge_relation,
        "root_candidate_eligible": fact.root_candidate_eligible,
        "episode_role": fact.episode_role,
        "tool_names": list(fact.tool_names),
        "file_refs": list(fact.file_refs),
        "symbol_refs": list(fact.symbol_refs),
        "path_shape": list(fact.path_shape),
    }


def _structured_values(
    value: Any, keys: Sequence[str]
) -> Tuple[str, ...]:
    output = set()
    pending = [(value, False, 0)]
    active = set()
    visited = 0
    while pending:
        current, exiting, depth = pending.pop()
        if not isinstance(current, Mapping):
            continue
        marker = id(current)
        if exiting:
            active.remove(marker)
            continue
        if marker in active:
            raise ValueError("candidate manifest metadata is recursive")
        visited += 1
        if (
            visited > _MAX_STRUCTURED_METADATA_NODES
            or depth > _MAX_STRUCTURED_METADATA_DEPTH
        ):
            raise ValueError("candidate manifest metadata traversal is bounded")
        active.add(marker)
        pending.append((current, True, depth))
        for key, item in current.items():
            if key in keys:
                if isinstance(item, str) and item.strip():
                    output.add(item.strip())
                elif isinstance(item, Sequence) and not isinstance(
                    item, (str, bytes, bytearray)
                ):
                    output.update(
                        str(member).strip()
                        for member in item
                        if str(member).strip()
                    )
            if key in {"metadata", "input", "arguments", "params"}:
                pending.append((item, False, depth + 1))
    return tuple(sorted(output))


def _matching_obligation_ids(
    candidate_ref: str,
    path: Sequence[str],
    obligations: Sequence[Any],
) -> Tuple[str, ...]:
    candidate_scope = {candidate_ref, *path}
    matched = set()
    for obligation in obligations:
        obligation_id = _value(obligation, "obligation_id")
        scope_refs = {
            str(ref)
            for ref in (_value(obligation, "scope_refs") or ())
        }
        acceptance_refs = {
            str(ref)
            for ref in (
                _value(obligation, "acceptance_evidence_refs") or ()
            )
        }
        if (
            obligation_id
            and candidate_scope & (scope_refs | acceptance_refs)
        ):
            matched.add(str(obligation_id))
    return tuple(sorted(matched))


def _cluster_grounding_refs(
    graph: Any, members: Sequence[CandidateFact]
) -> Tuple[str, ...]:
    if not members:
        return ()
    common = set(members[0].path_refs)
    for member in members[1:]:
        common.intersection_update(member.path_refs)
    return tuple(
        sorted(common, key=lambda ref: (graph.position(ref), ref))
    )


def _grounded_evidence_refs(
    graph: Any,
    candidates_by_ref: Mapping[str, CausalCandidate],
    member_refs: Sequence[str],
    keys: Sequence[str],
) -> Tuple[str, ...]:
    refs = []
    for ref in member_refs:
        candidate = candidates_by_ref[ref]
        if "evidence_refs" in keys:
            refs.extend(candidate.evidence_refs)
        for key in keys:
            refs.extend(
                str(value)
                for value in candidate.edge.get(key) or ()
                if str(value)
            )
    grounded = {
        graph.resolve(ref) or ref
        for ref in refs
        if (graph.resolve(ref) or ref) in graph.nodes
        and graph.active_revision_evidence_eligible(
            graph.resolve(ref) or ref
        )
    }
    return tuple(
        sorted(grounded, key=lambda ref: (graph.position(ref), ref))
    )


def _earliest_role(
    members: Sequence[CandidateFact],
    roles: Mapping[str, str],
    role: str,
) -> Optional[str]:
    return next(
        (member.ref for member in members if roles[member.ref] == role),
        None,
    )


def _latest_role(
    members: Sequence[CandidateFact],
    roles: Mapping[str, str],
    role: str,
) -> Optional[str]:
    return next(
        (
            member.ref
            for member in reversed(tuple(members))
            if roles[member.ref] == role
        ),
        None,
    )


def _distribution(values: Iterable[str]) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted(Counter(value for value in values if value).items()))


def _grouping_reason(level: str) -> str:
    return {
        L0_EXACT_ACTION: "recorded_action_group_or_call_identity",
        L1_CONFIRMED_CAUSAL_EPISODE: (
            "confirmed_lineage_call_span_or_execution_episode"
        ),
        L2_MATERIALIZATION_EPISODE: (
            "recorded_episode_or_candidate_to_seed_materialization_anchor"
        ),
        L3_STRUCTURAL_FALLBACK: (
            "deterministic_structured_semantic_signature"
        ),
    }[level]


def _cluster_id(level: str, key: str) -> str:
    return "cluster:v1:{0}".format(
        _digest({"level": level, "key": key})[:24]
    )


def _identity(schema: str, value: Any) -> str:
    return _digest({"schema": schema, "facts": value})


def _digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be non-empty".format(label))
    return value


def _require_ref(value: Any, label: str) -> str:
    result = _require_text(value, label)
    if ":" not in result:
        raise ValueError("{0} must be a canonical graph ref".format(label))
    return result


def _require_identity(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ValueError(
            "{0} must be a lowercase SHA-256 identity".format(label)
        )
    return value


def _require_cluster_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("cluster:v1:")
        or len(value) != len("cluster:v1:") + 24
    ):
        raise ValueError("candidate cluster identity is invalid")
    return value


def _require_unique_strings(
    values: Sequence[str], label: str
) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("{0} must contain non-empty strings".format(label))
    if len(values) != len(set(values)):
        raise ValueError("{0} must not contain duplicates".format(label))


def _require_sorted_identities(
    values: Sequence[str], label: str
) -> None:
    _require_unique_strings(values, label)
    for value in values:
        _require_identity(value, label)
    if tuple(values) != tuple(sorted(values)):
        raise ValueError("{0} must be sorted".format(label))


def _exact_mapping(
    value: Any, expected: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    if any(not isinstance(key, str) for key in value):
        raise ValueError("{0} contains a non-string schema key".format(label))
    if set(value) != expected:
        raise ValueError("{0} schema mismatch".format(label))
    return value


def _exact_int(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TypeError("{0} must be an integer".format(label))
    return value


def _string_tuple(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("{0} must be a JSON array".format(label))
    if any(not isinstance(item, str) for item in value):
        raise TypeError("{0} must contain strings".format(label))
    return tuple(value)


def _mapping_sequence(
    value: Any, label: str
) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise TypeError("{0} must be a JSON array".format(label))
    if any(not isinstance(item, Mapping) for item in value):
        raise TypeError("{0} must contain objects".format(label))
    return tuple(value)


def _optional_string_mapping(
    value: Any, label: str
) -> Dict[str, Optional[str]]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    if any(not isinstance(key, str) for key in value):
        raise ValueError("{0} contains a non-string schema key".format(label))
    if any(
        item is not None and not isinstance(item, str)
        for item in value.values()
    ):
        raise TypeError(
            "{0} must contain strings or null".format(label)
        )
    return {str(key): item for key, item in value.items()}


def _count_distribution(
    value: Any, label: str
) -> Tuple[Tuple[str, int], ...]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    output = []
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or not key
            or type(count) is not int
            or count <= 0
        ):
            raise ValueError("{0} contains an invalid count".format(label))
        output.append((key, count))
    return tuple(sorted(output))


def _string_mapping_tuple(
    value: Any, label: str
) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        raise TypeError("{0} must be an object".format(label))
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or not item
        for key, item in value.items()
    ):
        raise ValueError("{0} must contain non-empty strings".format(label))
    return tuple((str(key), str(item)) for key, item in value.items())


__all__ = [
    "CANDIDATE_CLUSTER_ALGORITHM",
    "CANDIDATE_CLUSTER_MANIFEST_SCHEMA",
    "CANDIDATE_CLUSTER_SHADOW_EVENT_SCHEMA",
    "CandidateCluster",
    "CandidateClusterManifest",
    "CandidateFact",
    "build_candidate_cluster_manifest",
    "build_candidate_cluster_shadow_event",
    "recorded_action_identity",
    "validate_candidate_cluster_shadow_event",
]
