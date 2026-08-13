"""Deterministic candidate-local evidence closure for global causal judgment."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .candidate_budget import (
    AUTHORED_DECISION,
    CANDIDATE_BUDGET_SCHEMA,
    CANDIDATE_BUDGET_V3_SCHEMA,
    CANDIDATE_BUDGET_V2_SCHEMA,
    MAX_GROUNDED_DECISION_RESERVE,
    NO_ACTIVE_SEED_CAUSAL_PATH,
    quality_first_candidate_budget,
)
from .causal_state import CausalCandidate, DefectState, FrozenMapping
from .causal_retrieval import (
    canonical_candidate_route,
    global_authored_root_candidate_eligible,
    root_candidate_eligible,
)
from .graph import TraceGraph
from .models import JsonDict, TraceNode, stable_json
from .restoration_obligation import RestorationObligation


CAPSULE_SCHEMA_VERSION = "candidate-evidence-capsule/v8"
ACTION_GROUP_KEYS = ("action_group_id", "actionGroupID", "actionGroupId")
CALL_ID_KEYS = ("call_id", "callID", "tool_call_id", "toolCallID")
MAX_VALIDATION_SOURCE_BYTES = 16384
GLOBAL_FUSION_MAX_PAYLOAD_BYTES = 65_536
GLOBAL_FUSION_MAX_OPEN_ROOT_CANDIDATES = 3
JUDGE_PROMPT_STRING_CHARS = 1600
JUDGE_PROMPT_COLLECTION_ITEMS = 12
JUDGE_PROMPT_MAXIMUM_DEPTH = 10
VALIDATION_SOURCE_KEYS = frozenset(
    {
        "candidate_ref",
        "candidate_source",
        "candidate_edge",
        "candidate_evidence_refs",
        "downstream_path",
        "start_refs",
        "prompt_collections_sha256",
    }
)
CANDIDATE_FUNNEL_KEYS = frozenset(
    {
        "schema",
        "discovered_count",
        "offered_count",
        "evidence_context_count",
        "dropped_count",
        "counts_by_category",
        "policy",
        "grounded_decision_refs",
        "reserved_grounded_decision_refs",
        "reserved_episode_refs",
        "evidence_context_refs",
        "context_reasons",
        "drop_reasons",
        "candidate_audit",
        "selection_identity",
    }
)
CANDIDATE_FUNNEL_V3_SCHEMA = CANDIDATE_BUDGET_V3_SCHEMA
CANDIDATE_FUNNEL_V3_KEYS = frozenset(
    key for key in CANDIDATE_FUNNEL_KEYS if key != "reserved_episode_refs"
)
CANDIDATE_FUNNEL_V1_SCHEMA = "candidate-budget-funnel/v1"
CANDIDATE_FUNNEL_V2_KEYS = frozenset(
    key
    for key in CANDIDATE_FUNNEL_V3_KEYS
    if key
    not in {
        "evidence_context_count",
        "evidence_context_refs",
        "context_reasons",
    }
)
CANDIDATE_FUNNEL_V1_KEYS = frozenset(
    key
    for key in CANDIDATE_FUNNEL_V2_KEYS
    if key != "grounded_decision_refs"
)
CANDIDATE_FUNNEL_V2_SCHEMA = CANDIDATE_BUDGET_V2_SCHEMA
CANDIDATE_FUNNEL_CATEGORIES = frozenset(
    {
        AUTHORED_DECISION,
        "tool_change_envelope",
        "verification_outcome",
        "factor_or_lifecycle",
        "other",
    }
)
CANDIDATE_FUNNEL_CATEGORY_COUNTS_KEYS = frozenset(
    {"discovered", "offered", "evidence_context", "dropped"}
)
CANDIDATE_FUNNEL_V2_CATEGORY_COUNTS_KEYS = frozenset(
    {"discovered", "offered", "dropped"}
)
CANDIDATE_FUNNEL_POLICY_KEYS = frozenset(
    {"total_limit", "grounded_decision_reserve"}
)
CANDIDATE_FUNNEL_AUDIT_KEYS = frozenset(
    {
        "ref",
        "category",
        "discovered_rank",
        "offered_rank",
        "context_rank",
        "assessment_eligible",
        "disposition",
        "reason",
        "grounded_hops",
        "episode_key",
        "episode_role",
        "reserve_rank",
        "reserve_disposition",
        "reserve_reason",
        "candidate_identity",
    }
)
CANDIDATE_FUNNEL_V3_AUDIT_KEYS = frozenset(
    key
    for key in CANDIDATE_FUNNEL_AUDIT_KEYS
    if key
    not in {
        "grounded_hops",
        "episode_key",
        "episode_role",
        "reserve_rank",
        "reserve_disposition",
        "reserve_reason",
    }
)
CANDIDATE_FUNNEL_V2_AUDIT_KEYS = frozenset(
    key
    for key in CANDIDATE_FUNNEL_V3_AUDIT_KEYS
    if key not in {"context_rank", "assessment_eligible"}
)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _dedupe_strings(values: Iterable[Any]) -> Tuple[str, ...]:
    output: List[str] = []
    seen = set()
    for value in values:
        item = str(value or "")
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return tuple(output)


def _episode_facts(
    value: Any,
    *,
    grounded_hops: int,
) -> JsonDict:
    required = {"episode_key", "episode_role", "grounded_hops"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError(
            "candidate evidence capsule episode_facts schema mismatch"
        )
    episode_key = str(value.get("episode_key") or "").strip()
    episode_role = str(value.get("episode_role") or "").strip()
    supplied_hops = value.get("grounded_hops")
    if not episode_key:
        raise ValueError(
            "candidate evidence capsule episode_key must be non-empty"
        )
    if episode_role not in {
        "authored_plan",
        "execution",
        "verification",
        "closure",
        "other",
    }:
        raise ValueError(
            "candidate evidence capsule episode_role is unsupported"
        )
    if type(supplied_hops) is not int or supplied_hops != grounded_hops:
        raise ValueError(
            "candidate evidence capsule grounded_hops must match downstream path"
        )
    return {
        "episode_key": episode_key,
        "episode_role": episode_role,
        "grounded_hops": supplied_hops,
    }


@dataclass(frozen=True)
class CandidateEvidenceCapsule:
    candidate_ref: str
    defect_state: DefectState
    candidate: Mapping[str, Any]
    downstream_path: Tuple[str, ...] = field(default_factory=tuple)
    downstream_path_references: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    causal_path_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    start_refs: Tuple[str, ...] = field(default_factory=tuple)
    action_group: Mapping[str, Any] = field(default_factory=FrozenMapping)
    incoming_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    outgoing_edges: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    evidence_references: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    artifact_hydration: Mapping[str, Any] = field(default_factory=FrozenMapping)
    missing_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    validation_source: Mapping[str, Any] = field(default_factory=FrozenMapping)
    restoration_obligations: Tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple
    )
    episode_facts: Mapping[str, Any] = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        candidate_ref = str(self.candidate_ref).strip()
        object.__setattr__(self, "candidate_ref", candidate_ref)
        object.__setattr__(self, "candidate", _freeze(self.candidate))
        object.__setattr__(self, "downstream_path", _dedupe_strings(self.downstream_path))
        object.__setattr__(
            self,
            "downstream_path_references",
            tuple(_freeze(item) for item in self.downstream_path_references),
        )
        object.__setattr__(
            self, "causal_path_edges", tuple(_freeze(item) for item in self.causal_path_edges)
        )
        object.__setattr__(self, "start_refs", _dedupe_strings(self.start_refs))
        object.__setattr__(self, "action_group", _freeze(self.action_group))
        object.__setattr__(
            self, "incoming_edges", tuple(_freeze(item) for item in self.incoming_edges)
        )
        object.__setattr__(
            self, "outgoing_edges", tuple(_freeze(item) for item in self.outgoing_edges)
        )
        object.__setattr__(
            self,
            "evidence_references",
            tuple(_freeze(item) for item in self.evidence_references),
        )
        object.__setattr__(self, "artifact_hydration", _freeze(self.artifact_hydration))
        object.__setattr__(
            self, "missing_evidence_refs", _dedupe_strings(self.missing_evidence_refs)
        )
        object.__setattr__(self, "validation_source", _freeze(self.validation_source))
        obligations = tuple(
            RestorationObligation.from_dict(_thaw(item)).to_dict()
            for item in self.restoration_obligations
        )
        object.__setattr__(
            self,
            "restoration_obligations",
            tuple(_freeze(item) for item in obligations),
        )
        object.__setattr__(
            self,
            "episode_facts",
            _freeze(
                _episode_facts(
                    self.episode_facts
                    or {
                        "episode_key": "fallback",
                        "episode_role": "other",
                        "grounded_hops": max(
                            0, len(self.downstream_path) - 1
                        ),
                    },
                    grounded_hops=max(0, len(self.downstream_path) - 1),
                )
            ),
        )
        self.validate()

    def validate(self) -> None:
        if not self.candidate_ref:
            raise ValueError("candidate evidence capsule requires candidate identity")
        if not isinstance(self.candidate, Mapping):
            raise TypeError("candidate evidence capsule candidate must be an object")
        if type(self.candidate.get("root_candidate_eligible")) is not bool:
            raise ValueError("candidate root_candidate_eligible must be an exact boolean")
        if type(self.candidate.get("evidence_eligible")) is not bool:
            raise ValueError("candidate evidence_eligible must be an exact boolean")
        if self.candidate.get("retrieval_is_not_causal_verdict") is not True:
            raise ValueError(
                "candidate retrieval_is_not_causal_verdict must be exact boolean true"
            )
        graph_facts = self.candidate.get("active_graph_facts")
        if not isinstance(graph_facts, Mapping):
            raise ValueError("candidate active_graph_facts must be an object")
        if graph_facts.get("canonical_ref") != self.candidate_ref:
            raise ValueError("candidate identity mismatches active graph canonical identity")
        if type(graph_facts.get("evidence_eligible")) is not bool or type(
            graph_facts.get("root_candidate_eligible")
        ) is not bool:
            raise ValueError("candidate active graph eligibility must be exact booleans")
        if (
            graph_facts.get("evidence_eligible")
            != self.candidate.get("evidence_eligible")
            or graph_facts.get("root_candidate_eligible")
            != self.candidate.get("root_candidate_eligible")
        ):
            raise ValueError("candidate active graph eligibility facts contradict candidate")
        self._validate_identity_mapping(self.candidate, label="candidate")
        node = self.candidate.get("node")
        if not isinstance(node, Mapping):
            raise ValueError("candidate identity requires an embedded candidate node")
        self._validate_identity_mapping(node, label="embedded candidate node")
        if not self.downstream_path or self.downstream_path[0] != self.candidate_ref:
            raise ValueError("candidate identity must match downstream path start")
        self._validate_validation_source()
        obligation_ids = [
            str(item.get("obligation_id") or "")
            for item in self.restoration_obligations
        ]
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError(
                "candidate evidence capsule contains duplicate restoration obligations"
            )
        _episode_facts(
            self.episode_facts,
            grounded_hops=max(0, len(self.downstream_path) - 1),
        )
        if len(self.downstream_path_references) != len(self.downstream_path):
            raise ValueError(
                "candidate downstream path references must cover every path position"
            )
        for path_ref, reference in zip(
            self.downstream_path, self.downstream_path_references
        ):
            if not isinstance(reference, Mapping):
                raise TypeError("candidate downstream path reference must be an object")
            if reference.get("resolution_status") != "resolved":
                raise ValueError("candidate downstream path reference must be resolved")
            resolved_ref = str(reference.get("resolved_ref") or "")
            canonical_ref = str(reference.get("canonical_ref") or resolved_ref)
            raw_ref = str(reference.get("raw_ref") or "")
            if (
                raw_ref != path_ref
                or resolved_ref != path_ref
                or canonical_ref != path_ref
            ):
                raise ValueError(
                    "candidate identity downstream path reference must match its exact path position"
                )
            reference_node = reference.get("node")
            if isinstance(reference_node, Mapping):
                ref = str(reference_node.get("ref") or "")
                if ref != path_ref:
                    raise ValueError(
                        "candidate identity must match embedded downstream path node"
                    )

    def _validate_validation_source(self) -> None:
        source = self.validation_source
        if not isinstance(source, Mapping) or set(source) != VALIDATION_SOURCE_KEYS:
            raise ValueError("candidate evidence capsule validation source schema mismatch")
        if len(stable_json(_thaw(source)).encode("utf-8")) > MAX_VALIDATION_SOURCE_BYTES:
            raise ValueError(
                "candidate evidence capsule validation source must remain bounded"
            )
        if str(source.get("candidate_ref") or "") != self.candidate_ref:
            raise ValueError("candidate evidence capsule validation source identity mismatch")
        if str(source.get("candidate_source") or "") != str(
            self.candidate.get("source") or ""
        ):
            raise ValueError("candidate evidence capsule validation source route mismatch")
        if not isinstance(source.get("candidate_edge"), Mapping):
            raise TypeError("candidate evidence capsule validation source edge must be an object")
        for key in ("candidate_evidence_refs", "downstream_path", "start_refs"):
            value = source.get(key)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise TypeError(
                    "candidate evidence capsule validation source {0} must be a string array".format(
                        key
                    )
                )
        if tuple(source.get("downstream_path") or ()) != self.downstream_path:
            raise ValueError("candidate evidence capsule validation source path mismatch")
        if tuple(source.get("start_refs") or ()) != self.start_refs:
            raise ValueError("candidate evidence capsule validation source seed mismatch")
        expected_hash = _prompt_collections_sha256(
            retrieval_edge=self.candidate.get("retrieval_edge"),
            action_group=self.action_group,
            evidence_references=self.evidence_references,
            artifact_hydration=self.artifact_hydration,
            missing_evidence_refs=self.missing_evidence_refs,
        )
        if str(source.get("prompt_collections_sha256") or "") != expected_hash:
            raise ValueError(
                "candidate evidence capsule prompt-bearing collections do not match validation source"
            )

    def _validate_identity_mapping(
        self, value: Mapping[str, Any], *, label: str
    ) -> None:
        if str(value.get("ref") or "") != self.candidate_ref:
            raise ValueError("candidate identity mismatch in {0}".format(label))
        identity_fields = (
            str(value.get(key) or "")
            for key in ("candidate_ref", "resolved_ref", "canonical_ref")
            if key in value
        )
        if any(ref != self.candidate_ref for ref in identity_fields):
            raise ValueError(
                "candidate identity mismatch in {0}".format(label)
            )

    def to_dict(self) -> JsonDict:
        return {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "candidate_ref": self.candidate_ref,
            "defect_state": self.defect_state.to_dict(),
            "candidate": _thaw(self.candidate),
            "downstream_path": list(self.downstream_path),
            "downstream_path_references": _thaw(self.downstream_path_references),
            "causal_path_edges": _thaw(self.causal_path_edges),
            "start_refs": list(self.start_refs),
            "action_group": _thaw(self.action_group),
            "incoming_edges": _thaw(self.incoming_edges),
            "outgoing_edges": _thaw(self.outgoing_edges),
            "evidence_references": _thaw(self.evidence_references),
            "artifact_hydration": _thaw(self.artifact_hydration),
            "missing_evidence_refs": list(self.missing_evidence_refs),
            "validation_source": _thaw(self.validation_source),
            "restoration_obligations": _thaw(
                self.restoration_obligations
            ),
            "episode_facts": _thaw(self.episode_facts),
        }

    def judge_dict(self) -> JsonDict:
        """Return only grounded evidence collections intended for a Judge."""
        value = self.to_dict()
        value.pop("missing_evidence_refs", None)
        value.pop("validation_source", None)
        hydration = value.get("artifact_hydration")
        artifact_evidence_gaps = []
        if isinstance(hydration, Mapping):
            artifact_evidence_gaps.extend(
                copy.deepcopy(dict(item))
                for item in hydration.get("ineligible_artifact_evidence") or ()
                if isinstance(item, Mapping)
            )
            artifact_evidence_gaps.extend(
                {
                    "artifact_id": str(artifact_id),
                    "raw_ref": "artifact:{0}".format(artifact_id),
                    "owner_ref": self.candidate_ref,
                    "status": status,
                    "reason": reason,
                }
                for status, reason, artifact_ids in (
                    (
                        "missing",
                        "Artifact content is unavailable.",
                        hydration.get("missing_artifact_ids") or (),
                    ),
                    (
                        "truncated",
                        "Artifact content is incomplete.",
                        hydration.get("truncated_artifact_ids") or (),
                    ),
                )
                for artifact_id in artifact_ids
                if not any(
                    str(item.get("artifact_id") or "") == str(artifact_id)
                    for item in artifact_evidence_gaps
                )
            )
            artifact_evidence_gaps.extend(
                {
                    "artifact_id": str(item.get("artifact_id") or ""),
                    "raw_ref": "artifact:{0}".format(
                        item.get("artifact_id") or ""
                    ),
                    "owner_ref": self.candidate_ref,
                    "status": "integrity_failure",
                    "reason": str(item.get("status") or "integrity failure"),
                }
                for item in hydration.get("integrity_failures") or ()
                if isinstance(item, Mapping)
            )
        if artifact_evidence_gaps:
            value["artifact_evidence_gaps"] = artifact_evidence_gaps
        if isinstance(hydration, Mapping) and any(
            hydration.get(key)
            for key in (
                "integrity_failures",
                "ineligible_artifact_evidence",
                "missing_artifact_ids",
                "truncated_artifact_ids",
            )
        ):
            value.pop("artifact_hydration", None)
        elif isinstance(hydration, dict):
            for key in (
                "referenced_artifact_ids",
                "missing_artifact_ids",
                "truncated_artifact_ids",
                "integrity_failures",
                "ineligible_artifact_evidence",
            ):
                hydration.pop(key, None)
        return value

    def judge_prompt_dict(
        self,
        *,
        omitted_sections: List[str],
        omission_manifest: Optional[List[JsonDict]] = None,
    ) -> JsonDict:
        """Return bounded Judge facts without changing canonical evidence."""
        manifest = omission_manifest if omission_manifest is not None else []
        candidate = _thaw(self.candidate)
        allowed_candidate_keys = {
            "ref",
            "source",
            "retrieval_is_not_causal_verdict",
            "evidence_eligible",
            "root_candidate_eligible",
            "active_graph_facts",
            "retrieval_edge",
            "node",
        }
        for key in sorted(set(candidate) - allowed_candidate_keys):
            path = "candidate.{0}".format(key)
            omitted_sections.append(path)
            omitted = candidate.pop(key, None)
            manifest.append(
                _omission_manifest_entry(
                    owner_ref=self.candidate_ref,
                    path=path,
                    value=omitted,
                    reason="field_not_in_prompt_projection",
                    retained_item_count=0,
                )
            )
        return {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "candidate_ref": self.candidate_ref,
            "candidate": _bounded_prompt_value(
                candidate,
                path="candidate",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "downstream_path": list(self.downstream_path),
            "downstream_path_references": _bounded_prompt_value(
                _thaw(self.downstream_path_references),
                path="downstream_path_references",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
                preserve_current_collection=True,
            ),
            "causal_path_edges": _bounded_prompt_value(
                _thaw(self.causal_path_edges),
                path="causal_path_edges",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
                preserve_current_collection=True,
            ),
            "start_refs": list(self.start_refs),
            "action_group": _bounded_prompt_value(
                _thaw(self.action_group),
                path="action_group",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "incoming_edges": _bounded_prompt_value(
                _thaw(self.incoming_edges),
                path="incoming_edges",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "outgoing_edges": _bounded_prompt_value(
                _thaw(self.outgoing_edges),
                path="outgoing_edges",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "evidence_references": _bounded_prompt_value(
                _thaw(self.evidence_references),
                path="evidence_references",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "artifact_hydration": _bounded_prompt_value(
                _thaw(self.artifact_hydration),
                path="artifact_hydration",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "restoration_obligations": _bounded_prompt_value(
                _thaw(self.restoration_obligations),
                path="restoration_obligations",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
            "episode_facts": _bounded_prompt_value(
                _thaw(self.episode_facts),
                path="episode_facts",
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=self.candidate_ref,
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CandidateEvidenceCapsule":
        if not isinstance(value, Mapping):
            raise TypeError("candidate evidence capsule must be an object")
        required = {
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
            "artifact_hydration",
            "missing_evidence_refs",
            "validation_source",
            "restoration_obligations",
            "episode_facts",
        }
        if set(value) != required or value.get("schema_version") != CAPSULE_SCHEMA_VERSION:
            raise ValueError("candidate evidence capsule schema mismatch")

        def mapping(name: str) -> Mapping[str, Any]:
            item = value.get(name)
            if not isinstance(item, Mapping):
                raise TypeError("candidate evidence capsule {0} must be an object".format(name))
            return item

        def mappings(name: str) -> Tuple[Mapping[str, Any], ...]:
            items = value.get(name)
            if not isinstance(items, list) or any(
                not isinstance(item, Mapping) for item in items
            ):
                raise TypeError("candidate evidence capsule {0} must be an object array".format(name))
            return tuple(items)

        def strings(name: str) -> Tuple[str, ...]:
            items = value.get(name)
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item for item in items
            ):
                raise TypeError("candidate evidence capsule {0} must be a string array".format(name))
            return tuple(items)

        return cls(
            candidate_ref=str(value.get("candidate_ref") or ""),
            defect_state=DefectState.from_dict(dict(mapping("defect_state"))),
            candidate=mapping("candidate"),
            downstream_path=strings("downstream_path"),
            downstream_path_references=mappings("downstream_path_references"),
            causal_path_edges=mappings("causal_path_edges"),
            start_refs=strings("start_refs"),
            action_group=mapping("action_group"),
            incoming_edges=mappings("incoming_edges"),
            outgoing_edges=mappings("outgoing_edges"),
            evidence_references=mappings("evidence_references"),
            artifact_hydration=mapping("artifact_hydration"),
            missing_evidence_refs=strings("missing_evidence_refs"),
            validation_source=mapping("validation_source"),
            restoration_obligations=mappings(
                "restoration_obligations"
            ),
            episode_facts=mapping("episode_facts"),
        )


def build_candidate_evidence_capsules(
    *,
    graph: TraceGraph,
    candidates: Sequence[CausalCandidate],
    defect_state: DefectState,
    downstream_paths: Mapping[str, Sequence[str]],
    start_refs: Sequence[str],
    restoration_obligations: Sequence[RestorationObligation] = (),
    episode_facts_by_ref: Mapping[str, Mapping[str, Any]] | None = None,
) -> Tuple[CandidateEvidenceCapsule, ...]:
    owner_refs = tuple(
        dict.fromkeys(
            graph.resolve(item) or str(item)
            for item in start_refs
            if str(item)
        )
    )
    selected: Dict[str, CausalCandidate] = {}
    routes_by_ref: Dict[str, List[CausalCandidate]] = {}
    order: List[str] = []
    for candidate in candidates:
        resolved = graph.resolve(candidate.ref) or candidate.ref
        if not graph.active_revision_evidence_eligible(resolved):
            continue
        if resolved not in routes_by_ref:
            order.append(resolved)
            routes_by_ref[resolved] = []
        routes_by_ref[resolved].append(candidate)
    for ref in order:
        selected[ref] = canonical_candidate_route(
            graph, ref, routes_by_ref[ref]
        )

    capsules: List[CandidateEvidenceCapsule] = []
    for ref in order:
        candidate = selected[ref]
        raw_candidate_evidence_refs = _dedupe_strings(
            [
                *candidate.evidence_refs,
                *(candidate.edge.get("evidence_refs") or ()),
            ]
        )
        if candidate.source in {"confirmed_edge", "attribution_edge"} and candidate.edge:
            candidate = CausalCandidate(
                ref=candidate.ref,
                node=candidate.node,
                source=candidate.source,
                edge=graph.sanitize_judge_edge_evidence(candidate.edge),
                score=candidate.score,
                evidence_refs=_dedupe_strings(
                    graph.filter_evidence_refs(candidate.evidence_refs)
                ),
            )
            candidate = _reconcile_candidate_source(
                graph,
                candidate,
                authoritative_candidates=candidates,
            )
        node = graph.hydrate_node(ref)
        path = tuple(
            graph.resolve(item) or str(item)
            for item in downstream_paths.get(ref, ())
            if str(item)
        )
        if not path:
            path = (ref,)
        elif path[0] != ref:
            path = (ref, *path)
        if any(
            not graph.active_revision_evidence_eligible(path_ref)
            for path_ref in path
        ):
            raise ValueError(
                "candidate downstream path contains evidence ineligible for the active revision"
            )
        causal_path_edges = tuple(_causal_path_edges(graph, path))
        incoming_edges = tuple(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(ref)[:16]
        )
        outgoing_edges = tuple(
            _outgoing_edges(
                graph,
                ref,
                path=path,
                owner_refs=owner_refs,
                limit=16,
            )
        )
        prompt_collections = _build_prompt_collections(
            graph=graph,
            candidate=candidate,
            path=path,
            causal_path_edges=causal_path_edges,
            incoming_edges=incoming_edges,
            outgoing_edges=outgoing_edges,
            diagnostic_evidence_refs=raw_candidate_evidence_refs,
        )
        validation_source = _validation_source(
            graph=graph,
            candidate=candidate,
            path=path,
            start_refs=owner_refs,
            prompt_collections=prompt_collections,
            diagnostic_evidence_refs=raw_candidate_evidence_refs,
        )
        capsule = CandidateEvidenceCapsule(
            candidate_ref=ref,
            defect_state=defect_state,
            candidate={
                "ref": ref,
                "source": candidate.source,
                "retrieval_is_not_causal_verdict": True,
                "evidence_eligible": graph.active_revision_evidence_eligible(ref),
                "root_candidate_eligible": global_authored_root_candidate_eligible(
                    graph, ref
                ),
                "active_graph_facts": _active_candidate_graph_facts(graph, node),
                "retrieval_edge": prompt_collections["retrieval_edge"],
                "node": _grounded_node_snapshot(
                    graph, node, max_chars=3200
                ),
            },
            downstream_path=path,
            downstream_path_references=tuple(_reference(graph, item) for item in path),
            causal_path_edges=causal_path_edges,
            start_refs=owner_refs,
            action_group=prompt_collections["action_group"],
            incoming_edges=incoming_edges,
            outgoing_edges=outgoing_edges,
            evidence_references=tuple(prompt_collections["evidence_references"]),
            artifact_hydration=prompt_collections["artifact_hydration"],
            missing_evidence_refs=tuple(prompt_collections["missing_evidence_refs"]),
            validation_source=validation_source,
            restoration_obligations=tuple(
                obligation.to_dict()
                for obligation in restoration_obligations
                if set(obligation.scope_refs)
                & {
                    ref,
                    *path,
                    *owner_refs,
                    *raw_candidate_evidence_refs,
                }
            ),
            episode_facts=(
                (episode_facts_by_ref or {}).get(ref)
                or {
                    "episode_key": "fallback",
                    "episode_role": "other",
                    "grounded_hops": max(0, len(path) - 1),
                }
            ),
        )
        validate_candidate_evidence_capsule_against_graph(
            graph, capsule, authoritative_candidates=candidates
        )
        capsules.append(capsule)
    return tuple(capsules)


def _candidate_judge_edge(
    graph: TraceGraph,
    candidate: CausalCandidate,
) -> JsonDict:
    edge = graph.sanitize_judge_edge_evidence(_thaw(candidate.edge))
    if candidate.source not in {"confirmed_edge", "attribution_edge"}:
        edge.pop("confidence", None)
    return edge


def _build_prompt_collections(
    *,
    graph: TraceGraph,
    candidate: CausalCandidate,
    path: Tuple[str, ...],
    causal_path_edges: Sequence[Mapping[str, Any]] | None = None,
    incoming_edges: Sequence[Mapping[str, Any]] | None = None,
    outgoing_edges: Sequence[Mapping[str, Any]] | None = None,
    diagnostic_evidence_refs: Sequence[str] | None = None,
    start_refs: Sequence[str] = (),
) -> JsonDict:
    ref = graph.resolve(candidate.ref) or candidate.ref
    node = graph.hydrate_node(ref)
    raw_artifact_hydration = graph.artifact_hydration_manifest(ref)
    missing = [
        "artifact:{0}".format(item)
        for item in (
            raw_artifact_hydration.get("missing_artifact_ids") or ()
        )
    ]
    missing.extend(
        str(item.get("raw_ref") or "")
        for item in (
            raw_artifact_hydration.get("ineligible_artifact_evidence") or ()
        )
        if isinstance(item, Mapping) and str(item.get("raw_ref") or "")
    )
    artifact_hydration = raw_artifact_hydration
    retrieval_edge = _candidate_judge_edge(graph, candidate)
    causal_edges = tuple(
        causal_path_edges
        if causal_path_edges is not None
        else _causal_path_edges(graph, path)
    )
    incoming = tuple(
        incoming_edges
        if incoming_edges is not None
        else (
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(ref)[:16]
        )
    )
    outgoing = tuple(
        outgoing_edges
        if outgoing_edges is not None
        else _outgoing_edges(
            graph,
            ref,
            path=path,
            owner_refs=start_refs,
            limit=16,
        )
    )
    raw_evidence_refs = _dedupe_strings(
        [
            *(
                candidate.evidence_refs
                if diagnostic_evidence_refs is None
                else diagnostic_evidence_refs
            ),
            *(retrieval_edge.get("evidence_refs") or ()),
            *node.source_refs,
            *(
                evidence_ref
                for edge in (*causal_edges, *incoming, *outgoing)
                for evidence_ref in graph.unresolved_edge_evidence_refs(
                    str(edge.get("from_ref") or ""),
                    str(edge.get("to_ref") or ""),
                )
            ),
            *(
                evidence_ref
                for edge in (*causal_edges, *incoming, *outgoing)
                for evidence_ref in edge.get("evidence_refs") or ()
            ),
        ]
    )
    evidence_refs = _dedupe_strings(graph.filter_evidence_refs(raw_evidence_refs))
    grounded_raw_refs = set(evidence_refs)
    missing.extend(
        ref for ref in raw_evidence_refs if ref not in grounded_raw_refs
    )
    evidence_references = []
    for evidence_ref in evidence_refs:
        reference = _reference(graph, evidence_ref)
        evidence_references.append(reference)
    return {
        "retrieval_edge": retrieval_edge,
        "action_group": _action_group(graph, node),
        "evidence_references": evidence_references,
        "artifact_hydration": artifact_hydration,
        "missing_evidence_refs": list(_dedupe_strings(missing)),
    }


def _validation_source(
    *,
    graph: TraceGraph,
    candidate: CausalCandidate,
    path: Tuple[str, ...],
    start_refs: Tuple[str, ...],
    prompt_collections: Mapping[str, Any],
    diagnostic_evidence_refs: Sequence[str] | None = None,
) -> JsonDict:
    candidate_evidence_refs = _dedupe_strings(
        graph.filter_evidence_refs(
            diagnostic_evidence_refs or candidate.evidence_refs
        )
    )
    if (
        not candidate_evidence_refs
        and candidate.source in {"confirmed_edge", "attribution_edge"}
    ):
        candidate_evidence_refs = (candidate.ref,)
    return {
        "candidate_ref": candidate.ref,
        "candidate_source": candidate.source,
        "candidate_edge": _thaw(prompt_collections["retrieval_edge"]),
        "candidate_evidence_refs": list(candidate_evidence_refs),
        "downstream_path": list(path),
        "start_refs": list(start_refs),
        "prompt_collections_sha256": _prompt_collections_sha256(
            retrieval_edge=prompt_collections["retrieval_edge"],
            action_group=prompt_collections["action_group"],
            evidence_references=prompt_collections["evidence_references"],
            artifact_hydration=prompt_collections["artifact_hydration"],
            missing_evidence_refs=prompt_collections["missing_evidence_refs"],
        ),
    }


def _prompt_collections_sha256(
    *,
    retrieval_edge: Any,
    action_group: Any,
    evidence_references: Any,
    artifact_hydration: Any,
    missing_evidence_refs: Any,
) -> str:
    value = {
        "retrieval_edge": _thaw(retrieval_edge),
        "action_group": _thaw(action_group),
        "evidence_references": _thaw(evidence_references),
        "artifact_hydration": _thaw(artifact_hydration),
        "missing_evidence_refs": _thaw(missing_evidence_refs),
    }
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def candidate_compression_metrics(
    graph: TraceGraph,
    capsules: Sequence[CandidateEvidenceCapsule],
    *,
    candidate_funnel: Mapping[str, Any] | None = None,
) -> JsonDict:
    unique = {item.candidate_ref: item for item in capsules}
    trace_nodes = len(graph.nodes)
    candidate_count = len(unique)
    trace_bytes = len(stable_json(graph.raw_trace).encode("utf-8"))
    capsule_bytes = len(
        stable_json([item.to_dict() for item in unique.values()]).encode("utf-8")
    )
    open_root_candidate_count = sum(
        1
        for item in unique.values()
        if item.candidate.get("root_candidate_eligible") is True
        and len(item.downstream_path) > 1
        and item.downstream_path[0] == item.candidate_ref
        and item.downstream_path[-1] in item.start_refs
    )
    metrics = {
        "trace_node_count": trace_nodes,
        "candidate_count": candidate_count,
        "candidate_node_reduction_ratio": (
            round(1.0 - candidate_count / trace_nodes, 6) if trace_nodes else 0.0
        ),
        "trace_json_bytes": trace_bytes,
        "capsule_bytes": capsule_bytes,
        "open_root_candidate_count": open_root_candidate_count,
        "candidate_byte_reduction_ratio": (
            round(1.0 - capsule_bytes / trace_bytes, 6) if trace_bytes else 0.0
        ),
    }
    metrics["global_fusion_payload"] = global_fusion_payload_decision(
        metrics
    )
    return candidate_compression_with_funnel(metrics, candidate_funnel)


def candidate_compression_with_funnel(
    metrics: Mapping[str, Any],
    candidate_funnel: Mapping[str, Any] | None,
) -> JsonDict:
    """Copy validated Task 2 selection facts onto a metrics projection."""
    output = copy.deepcopy(dict(metrics))
    if candidate_funnel is not None:
        output["candidate_funnel"] = validate_candidate_funnel(
            candidate_funnel
        )
    return output


def candidate_funnel_is_valid(value: Any) -> bool:
    try:
        validate_candidate_funnel(value)
    except (TypeError, ValueError):
        return False
    return True


def validate_candidate_funnel(value: Any) -> JsonDict:
    """Validate replayable v4 or signed historical v1-v3 funnel records."""
    if not isinstance(value, Mapping):
        raise ValueError("candidate_funnel must be an object")
    funnel = _thaw(value)
    schema = funnel.get("schema")
    if schema == CANDIDATE_BUDGET_SCHEMA:
        funnel_keys = CANDIDATE_FUNNEL_KEYS
        replayable = True
    elif schema == CANDIDATE_FUNNEL_V3_SCHEMA:
        funnel_keys = CANDIDATE_FUNNEL_V3_KEYS
        replayable = False
    elif schema == CANDIDATE_FUNNEL_V2_SCHEMA:
        funnel_keys = CANDIDATE_FUNNEL_V2_KEYS
        replayable = False
    elif schema == CANDIDATE_FUNNEL_V1_SCHEMA:
        funnel_keys = CANDIDATE_FUNNEL_V1_KEYS
        replayable = False
    else:
        raise ValueError("candidate_funnel schema is unsupported")
    if set(funnel) != set(funnel_keys):
        raise ValueError("candidate_funnel has an invalid schema")
    if not _is_sha256_identity(funnel.get("selection_identity")):
        raise ValueError("candidate_funnel selection_identity is invalid")
    has_context = schema in {
        CANDIDATE_BUDGET_SCHEMA,
        CANDIDATE_FUNNEL_V3_SCHEMA,
    }
    count_keys = ["discovered_count", "offered_count", "dropped_count"]
    if has_context:
        count_keys.append("evidence_context_count")
    if any(
        type(funnel.get(key)) is not int or funnel[key] < 0
        for key in count_keys
    ):
        raise ValueError("candidate_funnel counts must be non-negative integers")
    if funnel["discovered_count"] != (
        funnel["offered_count"]
        + funnel["dropped_count"]
        + (
            funnel["evidence_context_count"]
            if has_context
            else 0
        )
    ):
        raise ValueError("candidate_funnel counts are not conserved")
    _validate_candidate_funnel_policy(funnel["policy"])
    if (
        schema
        in {
            CANDIDATE_BUDGET_SCHEMA,
            CANDIDATE_FUNNEL_V3_SCHEMA,
            CANDIDATE_FUNNEL_V2_SCHEMA,
        }
        and funnel["policy"]
        != quality_first_candidate_budget(
            funnel["discovered_count"]
        ).to_dict()
    ):
        raise ValueError(
            "candidate_funnel policy does not match its quality-first policy tier"
        )
    selected_count = funnel["offered_count"] + (
        funnel["evidence_context_count"]
        if has_context
        else 0
    )
    if selected_count > funnel["policy"]["total_limit"]:
        raise ValueError("candidate_funnel selected count exceeds its policy")
    _validate_candidate_funnel_category_counts(
        funnel,
        schema=str(schema),
    )
    _validate_candidate_funnel_audit(
        funnel,
        replayable=replayable,
        schema=str(schema),
    )
    unsigned = {
        key: funnel[key]
        for key in funnel_keys
        if key != "selection_identity"
    }
    expected_identity = hashlib.sha256(
        stable_json(unsigned).encode("utf-8")
    ).hexdigest()
    if funnel["selection_identity"] != expected_identity:
        raise ValueError("candidate_funnel selection_identity does not match")
    return funnel


def _validate_candidate_funnel_policy(policy: Any) -> None:
    if not isinstance(policy, Mapping) or set(policy) != set(
        CANDIDATE_FUNNEL_POLICY_KEYS
    ):
        raise ValueError("candidate_funnel policy has an invalid schema")
    total_limit = policy.get("total_limit")
    reserve = policy.get("grounded_decision_reserve")
    if (
        type(total_limit) is not int
        or total_limit < 0
        or type(reserve) is not int
        or reserve < 0
        or reserve > MAX_GROUNDED_DECISION_RESERVE
    ):
        raise ValueError("candidate_funnel policy is invalid")


def _validate_candidate_funnel_category_counts(
    funnel: Mapping[str, Any],
    *,
    schema: str,
) -> None:
    counts = funnel.get("counts_by_category")
    if not isinstance(counts, Mapping) or set(counts) != set(
        CANDIDATE_FUNNEL_CATEGORIES
    ):
        raise ValueError("candidate_funnel categories have an invalid schema")
    has_context = schema in {
        CANDIDATE_BUDGET_SCHEMA,
        CANDIDATE_FUNNEL_V3_SCHEMA,
    }
    count_keys = (
        CANDIDATE_FUNNEL_CATEGORY_COUNTS_KEYS
        if has_context
        else CANDIDATE_FUNNEL_V2_CATEGORY_COUNTS_KEYS
    )
    totals = {key: 0 for key in count_keys}
    for category_counts in counts.values():
        if not isinstance(category_counts, Mapping) or set(category_counts) != set(
            count_keys
        ):
            raise ValueError("candidate_funnel category counts are invalid")
        for key in totals:
            count = category_counts.get(key)
            if type(count) is not int or count < 0:
                raise ValueError("candidate_funnel category counts are invalid")
            totals[key] += count
        if category_counts["discovered"] != (
            category_counts["offered"]
            + category_counts["dropped"]
            + (
                category_counts["evidence_context"]
                if has_context
                else 0
            )
        ):
            raise ValueError("candidate_funnel category counts are not conserved")
    expected_totals = {
        "discovered": funnel["discovered_count"],
        "offered": funnel["offered_count"],
        "dropped": funnel["dropped_count"],
    }
    if has_context:
        expected_totals["evidence_context"] = funnel[
            "evidence_context_count"
        ]
    if totals != expected_totals:
        raise ValueError("candidate_funnel category totals do not match")


def _validate_candidate_funnel_audit(
    funnel: Mapping[str, Any],
    *,
    replayable: bool,
    schema: str,
) -> None:
    is_current = schema == CANDIDATE_BUDGET_SCHEMA
    has_context = schema in {
        CANDIDATE_BUDGET_SCHEMA,
        CANDIDATE_FUNNEL_V3_SCHEMA,
    }
    grounded_refs = funnel.get("grounded_decision_refs", [])
    reserved_refs = funnel.get("reserved_grounded_decision_refs")
    reserved_episode_refs = (
        funnel.get("reserved_episode_refs", []) if is_current else []
    )
    context_refs = (
        funnel.get("evidence_context_refs", []) if has_context else []
    )
    audit = funnel.get("candidate_audit")
    drop_reasons = funnel.get("drop_reasons")
    context_reasons = (
        funnel.get("context_reasons", {}) if has_context else {}
    )
    if (
        not isinstance(grounded_refs, list)
        or any(type(ref) is not str or not ref for ref in grounded_refs)
        or len(set(grounded_refs)) != len(grounded_refs)
        or not isinstance(reserved_refs, list)
        or any(type(ref) is not str or not ref for ref in reserved_refs)
        or len(set(reserved_refs)) != len(reserved_refs)
        or not isinstance(reserved_episode_refs, list)
        or any(
            type(ref) is not str or not ref
            for ref in reserved_episode_refs
        )
        or len(set(reserved_episode_refs)) != len(reserved_episode_refs)
        or not isinstance(context_refs, list)
        or any(type(ref) is not str or not ref for ref in context_refs)
        or len(set(context_refs)) != len(context_refs)
        or (
            has_context
            and len(context_refs) != funnel["evidence_context_count"]
        )
        or not isinstance(audit, list)
        or len(audit) != funnel["discovered_count"]
        or not isinstance(drop_reasons, Mapping)
        or set(drop_reasons) != {"total_limit"}
        or type(drop_reasons.get("total_limit")) is not int
        or drop_reasons["total_limit"] != funnel["dropped_count"]
        or (
            has_context
            and (
                not isinstance(context_reasons, Mapping)
                or set(context_reasons)
                != {NO_ACTIVE_SEED_CAUSAL_PATH}
                or type(
                    context_reasons.get(NO_ACTIVE_SEED_CAUSAL_PATH)
                )
                is not int
                or context_reasons[NO_ACTIVE_SEED_CAUSAL_PATH]
                != funnel["evidence_context_count"]
            )
        )
    ):
        raise ValueError("candidate_funnel audit has an invalid schema")
    offered_ranks = set()
    context_ranks = set()
    refs = set()
    audit_by_ref = {}
    count_keys = (
        CANDIDATE_FUNNEL_CATEGORY_COUNTS_KEYS
        if has_context
        else CANDIDATE_FUNNEL_V2_CATEGORY_COUNTS_KEYS
    )
    counts = {
        category: {key: 0 for key in count_keys}
        for category in CANDIDATE_FUNNEL_CATEGORIES
    }
    audit_keys = (
        CANDIDATE_FUNNEL_AUDIT_KEYS
        if is_current
        else CANDIDATE_FUNNEL_V3_AUDIT_KEYS
        if schema == CANDIDATE_FUNNEL_V3_SCHEMA
        else CANDIDATE_FUNNEL_V2_AUDIT_KEYS
    )
    for rank, entry in enumerate(audit):
        if not isinstance(entry, Mapping) or set(entry) != set(audit_keys):
            raise ValueError("candidate_funnel audit entry has an invalid schema")
        ref = entry.get("ref")
        category = entry.get("category")
        disposition = entry.get("disposition")
        offered_rank = entry.get("offered_rank")
        context_rank = entry.get("context_rank") if has_context else None
        assessment_eligible = (
            entry.get("assessment_eligible") if has_context else True
        )
        allowed_dispositions = (
            {"offered", "evidence_context", "dropped"}
            if has_context
            else {"offered", "dropped"}
        )
        if (
            type(ref) is not str
            or not ref
            or ref in refs
            or category not in CANDIDATE_FUNNEL_CATEGORIES
            or type(entry.get("discovered_rank")) is not int
            or entry["discovered_rank"] != rank
            or disposition not in allowed_dispositions
            or (has_context and type(assessment_eligible) is not bool)
            or not _is_sha256_identity(entry.get("candidate_identity"))
        ):
            raise ValueError("candidate_funnel audit entry is invalid")
        if is_current and (
            type(entry.get("grounded_hops")) is not int
            or entry["grounded_hops"] < 0
            or not isinstance(entry.get("episode_key"), str)
            or not entry["episode_key"]
            or entry.get("episode_role")
            not in {
                "authored_plan",
                "execution",
                "verification",
                "closure",
                "other",
            }
            or (
                entry.get("reserve_rank") is not None
                and (
                    type(entry["reserve_rank"]) is not int
                    or entry["reserve_rank"] < 0
                )
            )
            or entry.get("reserve_disposition")
            not in {
                "episode_reserved",
                "grounded_decision_reserved",
                "quality_order_fill",
                "evidence_context",
                "dropped",
            }
            or not isinstance(entry.get("reserve_reason"), str)
            or not entry["reserve_reason"]
        ):
            raise ValueError(
                "candidate_funnel episode reserve audit is invalid"
            )
        refs.add(ref)
        audit_by_ref[ref] = entry
        counts[category]["discovered"] += 1
        if disposition == "offered":
            if (
                type(offered_rank) is not int
                or offered_rank < 0
                or context_rank is not None
                or not assessment_eligible
            ):
                raise ValueError("candidate_funnel offered rank is invalid")
            offered_ranks.add(offered_rank)
            counts[category]["offered"] += 1
            expected_reason = (
                "episode_diverse_reserve"
                if ref in reserved_episode_refs
                else "grounded_decision_reserve"
                if ref in reserved_refs
                else "input_order"
            )
        elif disposition == "evidence_context":
            if (
                offered_rank is not None
                or type(context_rank) is not int
                or context_rank < 0
                or assessment_eligible
            ):
                raise ValueError(
                    "candidate_funnel context rank is invalid"
                )
            context_ranks.add(context_rank)
            counts[category]["evidence_context"] += 1
            expected_reason = NO_ACTIVE_SEED_CAUSAL_PATH
        else:
            if offered_rank is not None or context_rank is not None:
                raise ValueError("candidate_funnel dropped candidate has a rank")
            counts[category]["dropped"] += 1
            expected_reason = "total_limit"
        if entry.get("reason") != expected_reason:
            raise ValueError("candidate_funnel audit reason is invalid")
    if offered_ranks != set(range(funnel["offered_count"])):
        raise ValueError("candidate_funnel offered ranks are not contiguous")
    if has_context and context_ranks != set(
        range(funnel["evidence_context_count"])
    ):
        raise ValueError("candidate_funnel context ranks are not contiguous")
    if counts != funnel["counts_by_category"]:
        raise ValueError("candidate_funnel audit counts do not match")
    reserve_limit = min(
        funnel["policy"]["total_limit"],
        funnel["policy"]["grounded_decision_reserve"],
    )
    if (
        len(reserved_refs) > reserve_limit
        or len(set(reserved_refs) | set(reserved_episode_refs))
        > reserve_limit
        or any(
            ref not in audit_by_ref
            or audit_by_ref[ref]["category"] != AUTHORED_DECISION
            or audit_by_ref[ref]["disposition"] != "offered"
            for ref in reserved_refs
        )
    ):
        raise ValueError("candidate_funnel reserved decisions are invalid")
    if any(
        ref not in audit_by_ref
        or audit_by_ref[ref]["disposition"] != "offered"
        for ref in reserved_episode_refs
    ):
        raise ValueError("candidate_funnel episode reserve is invalid")
    if not replayable:
        return
    if (
        any(
            ref not in audit_by_ref
            or audit_by_ref[ref]["category"] != AUTHORED_DECISION
            or audit_by_ref[ref]["assessment_eligible"] is not True
            for ref in grounded_refs
        )
    ):
        raise ValueError("candidate_funnel grounded decisions are invalid")
    expected_reserved = [
        entry["ref"]
        for entry in sorted(
            (
                entry
                for entry in audit
                if entry.get("reserve_rank") is not None
            ),
            key=lambda entry: entry["reserve_rank"],
        )
    ]
    if (
        set(expected_reserved)
        != set(reserved_refs) | set(reserved_episode_refs)
        or [ref for ref in expected_reserved if ref in grounded_refs]
        != reserved_refs
        or [
            ref for ref in expected_reserved if ref in reserved_episode_refs
        ]
        != reserved_episode_refs
    ):
        raise ValueError(
            "candidate_funnel reserve audit contradicts reserved refs"
        )
    eligible_refs = [
        entry["ref"]
        for entry in audit
        if entry["assessment_eligible"] is True
    ]
    remaining_offered = max(
        0,
        funnel["policy"]["total_limit"] - len(expected_reserved),
    )
    expected_offered = expected_reserved + [
        ref
        for ref in eligible_refs
        if ref not in set(expected_reserved)
    ][:remaining_offered]
    remaining_context = max(
        0,
        funnel["policy"]["total_limit"] - len(expected_offered),
    )
    expected_context = [
        entry["ref"]
        for entry in audit
        if entry["assessment_eligible"] is False
    ][:remaining_context]
    actual_offered = [
        entry["ref"]
        for entry in sorted(
            (
                entry
                for entry in audit
                if entry["disposition"] == "offered"
            ),
            key=lambda entry: entry["offered_rank"],
        )
    ]
    actual_context = [
        entry["ref"]
        for entry in sorted(
            (
                entry
                for entry in audit
                if entry["disposition"] == "evidence_context"
            ),
            key=lambda entry: entry["context_rank"],
        )
    ]
    if (
        actual_offered != expected_offered
        or actual_context != expected_context
        or context_refs != expected_context
    ):
        raise ValueError(
            "candidate_funnel contradicts the v4 selection algorithm"
        )


def _is_sha256_identity(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def global_fusion_payload_decision(
    metrics: Mapping[str, Any],
    *,
    max_payload_bytes: int = GLOBAL_FUSION_MAX_PAYLOAD_BYTES,
    max_open_root_candidates: int = (
        GLOBAL_FUSION_MAX_OPEN_ROOT_CANDIDATES
    ),
) -> JsonDict:
    """Gate oversized payloads only when the root comparison is also dense."""
    trace_bytes = max(0, int(metrics.get("trace_json_bytes") or 0))
    capsule_bytes = max(0, int(metrics.get("capsule_bytes") or 0))
    open_root_candidates = max(
        0, int(metrics.get("open_root_candidate_count") or 0)
    )
    expansion_ratio = round(
        capsule_bytes / max(trace_bytes, 1),
        6,
    )
    negative_compression = capsule_bytes > trace_bytes
    oversized = capsule_bytes > max_payload_bytes
    dense_root_matrix = (
        open_root_candidates > max_open_root_candidates
    )
    eligible = not (oversized and dense_root_matrix)
    if not oversized:
        reason = "within_global_fusion_budget"
    elif not dense_root_matrix:
        reason = (
            "bounded_root_matrix_despite_negative_compression"
            if negative_compression
            else "bounded_root_matrix_despite_oversized_payload"
        )
    elif negative_compression:
        reason = "oversized_negative_compression"
    else:
        reason = "oversized_dense_root_matrix"
    return {
        "eligible": eligible,
        "reason": reason,
        "capsule_to_trace_expansion_ratio": expansion_ratio,
        "max_payload_bytes": max_payload_bytes,
        "open_root_candidate_count": open_root_candidates,
        "max_open_root_candidates": max_open_root_candidates,
        "negative_compression": negative_compression,
        "oversized": oversized,
        "dense_root_matrix": dense_root_matrix,
    }


def _reference(graph: TraceGraph, raw_ref: str) -> JsonDict:
    resolved = graph.resolve(str(raw_ref))
    if (
        resolved
        and resolved in graph.nodes
        and graph.active_revision_evidence_eligible(resolved)
    ):
        return {
            "raw_ref": str(raw_ref),
            "resolved_ref": resolved,
            "canonical_ref": resolved,
            "resolution_status": "resolved",
            "provenance_class": "recorded_or_reconstructed_trace_fact",
            "node": _grounded_node_snapshot(
                graph, graph.hydrate_node(resolved), max_chars=1800
            ),
        }
    artifact = graph.artifact_reference_status(str(raw_ref))
    if artifact is not None:
        return {
            **artifact,
            "resolved_ref": str(artifact.get("canonical_ref") or raw_ref),
            "provenance_class": "recorded_artifact_manifest",
        }
    return {
        "raw_ref": str(raw_ref),
        "resolved_ref": "",
        "resolution_status": "unresolved",
        "provenance_class": "unresolved_reference",
    }


def _active_candidate_graph_facts(
    graph: TraceGraph, node: TraceNode
) -> JsonDict:
    manifest = (
        graph.raw_trace.get("manifest")
        if isinstance(graph.raw_trace.get("manifest"), Mapping)
        else {}
    )
    return {
        "canonical_ref": node.ref,
        "component": node.component,
        "event_type": node.event_type,
        "event_kind": str(
            node.data.get("event_kind") or node.data.get("kind") or ""
        ),
        "subject_revision": str(
            node.data.get("subject_revision")
            or manifest.get("subject_revision")
            or ""
        ),
        "repository_revision": (
            ""
            if node.data.get("repository_revision") is None
            else str(node.data.get("repository_revision"))
        ),
        "revision_before": (
            ""
            if node.data.get("revision_before") is None
            else str(node.data.get("revision_before"))
        ),
        "revision_after": (
            ""
            if node.data.get("revision_after") is None
            else str(node.data.get("revision_after"))
        ),
        "revision_status": str(node.data.get("revision_status") or ""),
        "offline_only": node.data.get("offline_only"),
        "semantic_role": str(node.data.get("semantic_role") or ""),
        "navigation_role": str(node.data.get("navigation_role") or ""),
        "evidence_eligible": graph.active_revision_evidence_eligible(node.ref),
        "root_candidate_eligible": global_authored_root_candidate_eligible(
            graph, node.ref
        ),
    }


def validate_candidate_evidence_capsule_against_graph(
    graph: TraceGraph,
    capsule: CandidateEvidenceCapsule,
    *,
    authoritative_candidates: Sequence[CausalCandidate] = (),
) -> None:
    """Bind persisted candidate and path facts to the active graph."""
    capsule.validate()
    resolved = graph.resolve(capsule.candidate_ref)
    if resolved != capsule.candidate_ref or resolved not in graph.nodes:
        raise ValueError("candidate evidence capsule does not match active graph identity")
    if not graph.active_revision_evidence_eligible(resolved):
        raise ValueError(
            "candidate evidence capsule candidate is ineligible for the active revision"
        )
    node = graph.hydrate_node(resolved)
    expected_facts = _active_candidate_graph_facts(graph, node)
    if _thaw(capsule.candidate.get("active_graph_facts")) != expected_facts:
        raise ValueError("candidate evidence capsule facts do not match active graph")
    if capsule.candidate.get("evidence_eligible") is not True:
        raise ValueError("candidate evidence capsule is ineligible in active graph")
    if _thaw(capsule.candidate.get("node")) != _grounded_node_snapshot(
        graph, node, max_chars=3200
    ):
        raise ValueError("candidate evidence capsule node does not match active graph")
    source = capsule.validation_source
    source_evidence_refs = tuple(source.get("candidate_evidence_refs") or ())
    source_candidate = CausalCandidate(
        ref=str(source.get("candidate_ref") or ""),
        node=node,
        source=str(source.get("candidate_source") or ""),
        edge=_thaw(source.get("candidate_edge")),
        evidence_refs=tuple(graph.filter_evidence_refs(source_evidence_refs)),
    )
    active_candidate = _reconcile_candidate_source(
        graph,
        source_candidate,
        authoritative_candidates=authoritative_candidates,
    )
    source_edge = _candidate_judge_edge(graph, source_candidate)
    recorded_source = active_candidate.source in {
        "confirmed_edge",
        "attribution_edge",
    }
    active_routes = tuple(
        route
        for route in authoritative_candidates
        if (graph.resolve(route.ref) or route.ref) == active_candidate.ref
        and (
            (
                recorded_source
                and graph.resolve(str(route.edge.get("to_ref") or ""))
                == graph.resolve(str(source_edge.get("to_ref") or ""))
                and str(route.edge.get("relation") or "")
                == str(source_edge.get("relation") or "")
                and str(route.edge.get("evidence_type") or "")
                == str(source_edge.get("evidence_type") or "")
            )
            or (
                not recorded_source
                and route.source == source_candidate.source
                and _candidate_judge_edge(graph, route) == source_edge
            )
        )
    )
    diagnostic_candidate = (
        canonical_candidate_route(
            graph,
            active_candidate.ref,
            active_routes,
        )
        if active_routes
        else active_candidate
    )
    diagnostic_source_refs = source_evidence_refs
    if (
        diagnostic_source_refs == (active_candidate.ref,)
        and not diagnostic_candidate.evidence_refs
        and not diagnostic_candidate.edge.get("evidence_refs")
    ):
        diagnostic_source_refs = ()
    diagnostic_evidence_refs = _dedupe_strings(
        [
            *diagnostic_source_refs,
            *diagnostic_candidate.evidence_refs,
            *(diagnostic_candidate.edge.get("evidence_refs") or ()),
        ]
    )
    expected_prompt_collections = _build_prompt_collections(
        graph=graph,
        candidate=active_candidate,
        path=capsule.downstream_path,
        diagnostic_evidence_refs=diagnostic_evidence_refs,
        start_refs=capsule.start_refs,
    )
    missing_artifact_refs = {
        "artifact:{0}".format(artifact_id)
        for artifact_id in (
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "missing_artifact_ids"
                )
                or ()
            ),
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "truncated_artifact_ids"
                )
                or ()
            ),
            *(
                expected_prompt_collections["artifact_hydration"].get(
                    "integrity_failures"
                )
                or ()
            ),
        )
    }
    invalid_diagnostics = [
        ref
        for ref in capsule.missing_evidence_refs
        if graph.filter_evidence_refs([ref]) and ref not in missing_artifact_refs
    ]
    if invalid_diagnostics:
        raise ValueError(
            "candidate evidence capsule prompt-bearing missing refs do not match active diagnostics"
        )
    persisted_prompt_collections = {
        "retrieval_edge": _thaw(capsule.candidate.get("retrieval_edge")),
        "action_group": _thaw(capsule.action_group),
        "evidence_references": _thaw(capsule.evidence_references),
        "artifact_hydration": _thaw(capsule.artifact_hydration),
        "missing_evidence_refs": list(capsule.missing_evidence_refs),
    }
    for label, expected in expected_prompt_collections.items():
        if persisted_prompt_collections[label] != expected:
            raise ValueError(
                "candidate evidence capsule prompt-bearing {0} does not match active construction".format(
                    label
                )
            )
    for path_ref, reference in zip(
        capsule.downstream_path, capsule.downstream_path_references
    ):
        if not graph.active_revision_evidence_eligible(path_ref):
            raise ValueError(
                "candidate downstream path reference is ineligible for the active revision"
            )
        if _thaw(reference) != _reference(graph, path_ref):
            raise ValueError(
                "candidate downstream path reference does not match active graph"
            )
    expected_edges = {
        "causal path": _causal_path_edges(graph, capsule.downstream_path),
        "incoming": [
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.incoming_edge_context(capsule.candidate_ref)[:16]
        ],
        "outgoing": _outgoing_edges(
            graph,
            capsule.candidate_ref,
            path=capsule.downstream_path,
            owner_refs=capsule.start_refs,
            limit=16,
        ),
    }
    persisted_edges = {
        "causal path": capsule.causal_path_edges,
        "incoming": capsule.incoming_edges,
        "outgoing": capsule.outgoing_edges,
    }
    for label, expected in expected_edges.items():
        if _thaw(persisted_edges[label]) != expected:
            raise ValueError(
                "candidate evidence capsule {0} edges do not match active graph".format(
                    label
                )
            )


def _reconcile_candidate_source(
    graph: TraceGraph,
    candidate: CausalCandidate,
    *,
    authoritative_candidates: Sequence[CausalCandidate],
) -> CausalCandidate:
    edge = candidate.edge
    requires_recorded_edge = candidate.source in {
        "confirmed_edge",
        "attribution_edge",
    }
    if not edge:
        matching = [
            route
            for route in authoritative_candidates
            if (graph.resolve(route.ref) or route.ref) == candidate.ref
            and route.source == candidate.source
            and not route.edge
            and (
                tuple(
                    sorted(graph.filter_evidence_refs(route.evidence_refs))
                )
                == tuple(sorted(candidate.evidence_refs))
                or (
                    candidate.source
                    in {"confirmed_edge", "attribution_edge"}
                    and not route.evidence_refs
                    and tuple(candidate.evidence_refs) == (candidate.ref,)
                )
            )
        ]
        if matching:
            return candidate
        raise ValueError(
            "candidate with no recorded edge requires an authoritative retrieval route"
        )
    raw_from_ref = str(edge.get("from_ref") or "")
    raw_to_ref = str(edge.get("to_ref") or "")
    from_ref = graph.resolve(raw_from_ref)
    to_ref = graph.resolve(raw_to_ref)
    if raw_from_ref and from_ref != candidate.ref:
        raise ValueError(
            "candidate evidence capsule validation source edge starts at another candidate"
        )
    if raw_to_ref and not to_ref:
        raise ValueError(
            "candidate evidence capsule validation source edge target is unresolved"
        )
    if not requires_recorded_edge:
        routes = [
            route
            for route in authoritative_candidates
            if (graph.resolve(route.ref) or route.ref) == candidate.ref
        ]
        if not routes:
            raise ValueError(
                "synthetic candidate requires an authoritative retrieval route"
            )
        candidate_edge = _candidate_judge_edge(graph, candidate)
        has_gap_aggregation = bool(
            candidate_edge.get("attribution_only_obligation_gaps")
        )
        if has_gap_aggregation:
            comparable_routes = (
                canonical_candidate_route(graph, candidate.ref, routes),
            )
        else:
            comparable_routes = tuple(routes)
        matching = [
            route
            for route in comparable_routes
            if route.source == candidate.source
            and _candidate_judge_edge(graph, route) == candidate_edge
            and tuple(
                sorted(
                    _dedupe_strings(
                        graph.filter_evidence_refs(route.evidence_refs)
                    )
                )
            )
            == tuple(sorted(candidate.evidence_refs))
        ]
        if not matching:
            raise ValueError(
                "synthetic candidate does not match its authoritative retrieval route"
            )
        return candidate
    routes = [
        route
        for route in authoritative_candidates
        if (graph.resolve(route.ref) or route.ref) == candidate.ref
        and graph.resolve(str(route.edge.get("to_ref") or "")) == to_ref
        and str(route.edge.get("relation") or "")
        == str(candidate.edge.get("relation") or "")
        and str(route.edge.get("evidence_type") or "")
        == str(candidate.edge.get("evidence_type") or "")
    ]
    if not routes:
        raise ValueError(
            "recorded candidate requires an authoritative recorded route"
        )
    active = canonical_candidate_route(graph, candidate.ref, routes)
    active_edge = graph.sanitize_judge_edge_evidence(active.edge)
    active_route_evidence_refs = _dedupe_strings(
        graph.filter_evidence_refs(active.evidence_refs)
    )
    active_evidence_refs = _dedupe_strings(
        (
            *active_route_evidence_refs,
            *graph.filter_evidence_refs(
                active_edge.get("evidence_refs") or ()
            ),
        )
    )
    permitted_evidence_refs = {
        active_route_evidence_refs,
        active_evidence_refs,
    }
    if not active_evidence_refs:
        permitted_evidence_refs.add((candidate.ref,))
    active_to_ref = graph.resolve(str(active_edge.get("to_ref") or ""))
    if (
        not active_to_ref
        or not any(
            str(item.get("relation") or "")
            == str(active_edge.get("relation") or "")
            and str(item.get("evidence_type") or "")
            == str(active_edge.get("evidence_type") or "")
            for item in graph.edge_context(
                candidate.ref,
                active_to_ref,
            )
        )
        or active.source != candidate.source
        or _thaw(active_edge) != _thaw(candidate.edge)
        or tuple(candidate.evidence_refs) not in permitted_evidence_refs
    ):
        raise ValueError(
            "candidate does not match its authoritative recorded route"
        )
    return CausalCandidate(
        ref=candidate.ref,
        node=active.node,
        source=active.source,
        edge=active_edge,
        score=active.score,
        evidence_refs=tuple(candidate.evidence_refs),
    )


def validate_candidate_evidence_capsules_against_graph(
    graph: TraceGraph,
    capsules: Sequence[CandidateEvidenceCapsule],
    *,
    authoritative_candidates: Sequence[CausalCandidate] = (),
) -> None:
    if authoritative_candidates:
        authoritative_refs = {
            graph.resolve(candidate.ref) or candidate.ref
            for candidate in authoritative_candidates
            if graph.active_revision_evidence_eligible(
                graph.resolve(candidate.ref) or candidate.ref
            )
        }
        capsule_refs = {
            graph.resolve(capsule.candidate_ref) or capsule.candidate_ref
            for capsule in capsules
        }
        if capsule_refs != authoritative_refs:
            raise ValueError(
                "candidate capsule set must exactly cover the active "
                "authoritative candidate set"
            )
    for capsule in capsules:
        validate_candidate_evidence_capsule_against_graph(
            graph,
            capsule,
            authoritative_candidates=authoritative_candidates,
        )


def _outgoing_edges(
    graph: TraceGraph,
    ref: str,
    *,
    path: Sequence[str],
    owner_refs: Sequence[str] = (),
    limit: int,
) -> List[JsonDict]:
    output: List[JsonDict] = []
    if not path or path[0] != ref:
        return output
    if len(path) > 1:
        allowed_targets = {path[1]}
    else:
        allowed_targets = _owner_ancestor_refs(graph, owner_refs)
    for downstream in graph.downstream_refs(ref):
        if downstream not in allowed_targets:
            continue
        if not graph.edge_endpoints_eligible(ref, downstream):
            continue
        output.extend(
            graph.sanitize_judge_edge_evidence(edge)
            for edge in graph.edge_context(ref, downstream)
        )
        if len(output) >= limit:
            break
    return output[:limit]


def _owner_ancestor_refs(
    graph: TraceGraph,
    owner_refs: Sequence[str],
) -> Set[str]:
    pending = [
        graph.resolve(owner_ref) or str(owner_ref)
        for owner_ref in owner_refs
        if str(owner_ref)
    ]
    ancestors: Set[str] = set()
    while pending:
        current = pending.pop()
        if (
            current in ancestors
            or not graph.active_revision_evidence_eligible(current)
        ):
            continue
        ancestors.add(current)
        pending.extend(graph.upstream_refs(current))
    return ancestors


def _causal_path_edges(graph: TraceGraph, path: Tuple[str, ...]) -> List[JsonDict]:
    output: List[JsonDict] = []
    seen = set()
    for source_ref, target_ref in zip(path, path[1:]):
        if not graph.edge_endpoints_eligible(source_ref, target_ref):
            continue
        for edge in graph.edge_context(source_ref, target_ref):
            sanitized = graph.sanitize_judge_edge_evidence(edge)
            signature = stable_json(sanitized)
            if signature in seen:
                continue
            seen.add(signature)
            output.append(sanitized)
    return output


def build_action_group_context(
    graph: TraceGraph, candidate: TraceNode
) -> JsonDict:
    identity = _action_identity(candidate)
    members = []
    if identity:
        for node in graph.nodes.values():
            resolved = graph.resolve(node.ref)
            if (
                resolved != node.ref
                or resolved not in graph.nodes
                or not graph.active_revision_evidence_eligible(resolved)
            ):
                continue
            active_node = graph.hydrate_node(resolved)
            if (
                _action_identity(active_node) == identity
                and _action_revision_eligible(graph, candidate, active_node)
            ):
                members.append(
                    _grounded_node_snapshot(
                        graph, active_node, max_chars=1800
                    )
                )
        members.sort(key=lambda item: graph.position(str(item.get("ref") or "")))
    if (
        not members
        and graph.active_revision_evidence_eligible(candidate.ref)
        and root_candidate_eligible(candidate)
    ):
        members = [
            _grounded_node_snapshot(graph, candidate, max_chars=1800)
        ]
    return {
        "identity": identity or "node_ref:{0}".format(candidate.ref),
        "identity_source": "recorded_action_group_or_call_id" if identity else "candidate_ref_fallback",
        "members": members[:12],
        "member_count": len(members),
        "truncated": len(members) > 12,
    }


def _action_group(graph: TraceGraph, candidate: TraceNode) -> JsonDict:
    return build_action_group_context(graph, candidate)


def _action_identity(node: TraceNode) -> str:
    data = node.data if isinstance(node.data, Mapping) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
    for container in (data, metadata):
        for key in ACTION_GROUP_KEYS:
            value = str(container.get(key) or "")
            if value:
                return "action_group_id:{0}".format(value)
    for container in (data, metadata):
        for key in CALL_ID_KEYS:
            value = str(container.get(key) or "")
            if value:
                return "call_id:{0}".format(value)
    return ""


def _action_revision_eligible(
    graph: TraceGraph, candidate: TraceNode, member: TraceNode
) -> bool:
    return (
        graph.active_revision_evidence_eligible(candidate.ref)
        and graph.active_revision_evidence_eligible(member.ref)
    )


def _grounded_node_snapshot(
    graph: TraceGraph, node: TraceNode, *, max_chars: int
) -> JsonDict:
    snapshot = graph.sanitize_judge_node(node).compact(max_chars=max_chars)
    snapshot["source_refs"] = list(snapshot.get("source_refs") or ())
    return snapshot


def _omission_manifest_entry(
    *,
    owner_ref: str,
    path: str,
    value: Any,
    reason: str,
    retained_item_count: Optional[int] = None,
) -> JsonDict:
    canonical = stable_json(_thaw(value))
    raw = canonical.encode("utf-8")
    original_item_count = len(value) if isinstance(value, (list, tuple)) else None
    return {
        "owner_ref": owner_ref,
        "path": path,
        "reason": reason,
        "original_utf8_bytes": len(raw),
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "original_item_count": original_item_count,
        "retained_item_count": retained_item_count,
    }


def _bounded_prompt_value(
    value: Any,
    *,
    path: str,
    omitted_sections: List[str],
    omission_manifest: Optional[List[JsonDict]] = None,
    owner_ref: str = "",
    preserve_current_collection: bool = False,
    depth: int = 0,
) -> Any:
    manifest = omission_manifest if omission_manifest is not None else []
    if depth >= JUDGE_PROMPT_MAXIMUM_DEPTH:
        omitted_sections.append(path)
        manifest.append(
            _omission_manifest_entry(
                owner_ref=owner_ref,
                path=path,
                value=value,
                reason="maximum_projection_depth",
            )
        )
        return {"omitted": True, "reason": "maximum_projection_depth"}
    if isinstance(value, str):
        if len(value) <= JUDGE_PROMPT_STRING_CHARS:
            return value
        omitted_sections.append(path)
        raw = value.encode("utf-8")
        manifest.append(
            _omission_manifest_entry(
                owner_ref=owner_ref,
                path=path,
                value=value,
                reason="string_truncated",
            )
        )
        return "{0}\n...[omitted bytes={1} sha256={2}]".format(
            value[:JUDGE_PROMPT_STRING_CHARS],
            len(raw),
            hashlib.sha256(raw).hexdigest(),
        )
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_prompt_value(
                item,
                path="{0}.{1}".format(path, key),
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=owner_ref,
                depth=depth + 1,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        selected = (
            items
            if preserve_current_collection
            else items[:JUDGE_PROMPT_COLLECTION_ITEMS]
        )
        if len(items) > len(selected):
            omitted_sections.append(path)
            manifest.append(
                _omission_manifest_entry(
                    owner_ref=owner_ref,
                    path=path,
                    value=items,
                    reason="collection_truncated",
                    retained_item_count=len(selected),
                )
            )
        projected = [
            _bounded_prompt_value(
                item,
                path="{0}[{1}]".format(path, index),
                omitted_sections=omitted_sections,
                omission_manifest=manifest,
                owner_ref=owner_ref,
                depth=depth + 1,
            )
            for index, item in enumerate(selected)
        ]
        if len(items) > len(selected):
            projected.append(
                {
                    "omitted_item_count": len(items) - len(selected),
                    "original_item_count": len(items),
                }
            )
        return projected
    return copy.deepcopy(value)


__all__ = [
    "CAPSULE_SCHEMA_VERSION",
    "MAX_VALIDATION_SOURCE_BYTES",
    "JUDGE_PROMPT_COLLECTION_ITEMS",
    "JUDGE_PROMPT_MAXIMUM_DEPTH",
    "JUDGE_PROMPT_STRING_CHARS",
    "CandidateEvidenceCapsule",
    "build_action_group_context",
    "build_candidate_evidence_capsules",
    "candidate_compression_with_funnel",
    "candidate_compression_metrics",
    "candidate_funnel_is_valid",
    "global_fusion_payload_decision",
    "validate_candidate_funnel",
    "validate_candidate_evidence_capsule_against_graph",
    "validate_candidate_evidence_capsules_against_graph",
]
