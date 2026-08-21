"""Grounded LLM judgments for recursive causal attribution."""

from __future__ import annotations

import hashlib
import copy
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

from .cache import JudgmentCache, build_judge_cache_key
from .causal_state import (
    CAUSAL_RELATIONS,
    FACTOR_ROLE_CONTRACT,
    CausalCandidate,
    CausalStepJudgment,
    DefectState,
    FactorRoleJudgment,
    FrozenMapping,
    PredecessorAssessment,
    RootConfirmation,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    factor_role_contract_entry,
    is_definitive_confirmation,
    validate_root_confirmation_substantive_invariants,
)
from .claude import ClaudeJudgeClient
from .candidate_clustering import CandidateClusterManifest
from .causal_retrieval import is_navigation_node, root_candidate_eligible
from .errors import (
    JudgeProviderError,
    JudgeProviderUnavailable,
    TransportCallError,
    TransportCallResult,
)
from .evidence_capsule import CandidateEvidenceCapsule
from .global_judge import (
    CANDIDATE_PHASES,
    CAUSAL_ROLES,
    FAILURE_MODES,
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    OBLIGATION_STATUSES,
    REPAIR_WINDOW_EFFECTS,
    RESPONSIBILITIES,
    GlobalCandidateJudgeRequest,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
    build_global_candidate_prompt,
    canonicalize_global_candidate_structural_bindings,
    global_candidate_comparison_contract_from_context,
    global_judge_diagnostics,
    global_candidate_judgment_from_payload,
    validate_global_candidate_payload,
)
from .models import JsonDict, TraceNode, stable_json
from .judge_payload import (
    reject_analysis_control_envelopes,
    scrub_attribution_verdicts,
)
from .judgment_context import (
    judge_visible_active_failure_factual_context,
    validate_active_failure_factual_context,
)
from .cluster_triage import CandidateClusterTriageRequest
from .cluster_triage_judge import (
    CLUSTER_TRIAGE_PROMPT_SCHEMA_VERSION,
    CLUSTER_TRIAGE_SYSTEM_PROMPT,
    ClusterTriageCapability,
    ClusterTriagePageRequest,
    build_cluster_triage_page_requests,
    build_cluster_triage_prompt,
    merge_cluster_triage_judgments,
    parse_cluster_triage_judgment,
)


CAUSAL_STEP_PROMPT_SCHEMA_VERSION = "recursive-causal-step-v14"
ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION = "recursive-root-confirmation-v14"
FACTOR_ROLE_PROMPT_SCHEMA_VERSION = "independent-factor-role-v1"
MAX_SEMANTIC_REPAIR_ATTEMPTS = 6
MAX_EQUIVALENT_VALIDATION_ERRORS = 3
ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA = (
    "root-confirmation-request-projection/v3"
)
ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX = "confirmation_request:v4:"
ROOT_CONFIRMATION_REQUEST_PROJECTION_KEYS = frozenset({"schema", "facts"})
ROOT_CONFIRMATION_REQUEST_FACT_KEYS = frozenset(
    {
        "candidate_ref",
        "defect_state",
        "recursive_path",
        "candidate_reference",
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
        "competing_hypotheses",
        "task_obligations",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "seed_binding_identity",
        "analysis_perspective",
    }
)
ROOT_CONFIRMATION_REQUEST_OPTIONAL_FACT_KEYS = frozenset(
    {"factual_context", "process_factual_context"}
)
FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA = "factor-role-request-projection/v1"
FACTOR_ROLE_REQUEST_IDENTITY_PREFIX = "factor-role-request:v1:"
FACTOR_ROLE_CONFIRMED_ROOT_SUMMARY_SCHEMA = (
    "factor-role-root-evidence-summary/v2"
)
FACTOR_ROLE_REQUEST_PROJECTION_KEYS = frozenset({"schema", "facts"})
FACTOR_ROLE_REQUEST_FACT_KEYS = frozenset(
    {
        "candidate_ref",
        "defect_state",
        "recursive_path",
        "candidate_reference",
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
        "task_obligations",
        "confirmed_root_summaries",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "seed_binding_identity",
        "analysis_perspective",
    }
)
FACTOR_ROLE_REQUEST_OPTIONAL_FACT_KEYS = frozenset({"factual_context"})
FACTOR_ROLE_CONFIRMED_ROOT_SUMMARY_KEYS = frozenset(
    {
        "schema",
        "candidate_ref",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "confirmation_identity",
        "defect_fingerprint",
        "seed_binding_identity",
        "reason",
        "evidence_refs",
        "recursive_path",
    }
)
_ENVELOPE_KEYS = {
    "raw_ref",
    "resolved_ref",
    "resolution_status",
    "provenance_class",
}
_ENVELOPE_SHAPE_KEYS = {"raw_ref", "resolved_ref", "resolution_status"}
_ALLOWED_PROVENANCE = {"recorded", "reconstructed", "inferred"}
_ALLOWED_RESOLUTION = {
    "resolved",
    "unresolved",
    "ambiguous",
    "missing",
    "truncated",
    "unknown",
}

TEMPORAL_CAUSALITY_RULE = "Temporal order or proximity alone is never causal."
RELATION_DEFINITIONS = (
    "same_defect_propagation: the predecessor already contains the same active defect.",
    "defect_transformation: a different predecessor defect transforms into the active defect through an explicit mechanism.",
    "introduction_candidate: the defective node has no viable defective predecessor.",
    "contributing_condition: the predecessor changes likelihood or exposure but does not carry the active defect; set recurse=true only for a material authored assumption or condition that warrants its own explicit upstream_defect hypothesis.",
    "outcome_evidence: the predecessor records or exposes the defect outcome without causing it.",
    "unrelated: no grounded causal relationship is established.",
    "unknown: grounded evidence is insufficient to classify the relationship.",
)
INPUT_PROVENANCE_EVENT_TYPES = frozenset(
    {"prompt.assembly", "message.input", "context.transform", "llm.call", "task.loop"}
)
PROCESS_CANDIDATE_ROLES = frozenset(
    {
        "bounded_investigation",
        "implementation_commitment",
        "priority_decision",
        "action_selection",
        "closure",
        "other",
        "unknown",
    }
)
PROCESS_COMMITMENT_STATUSES = frozenset(
    {"not_applicable", "fulfilled", "unfulfilled", "unknown"}
)
PROCESS_TRAJECTORY_RELATIONS = frozenset(
    {"materialized", "diverged", "remained_investigation", "unknown"}
)
PROCESS_COMMITMENT_CUE_DISPOSITIONS = frozenset(
    {"commitment", "non_commitment", "ambiguous", "not_applicable"}
)

CAUSAL_STEP_SYSTEM_PROMPT = """You judge one backward step in an offline causal trace.
Use only the supplied grounded facts. Any component may be causal when its semantics and evidence support it.
Distinguish same-defect propagation from a defect transformation, and cite direct grounded evidence refs.
Return exactly one JSON object with no markdown."""

ROOT_CONFIRMATION_SYSTEM_PROMPT = """You independently try to falsify a proposed recursive root candidate.
Use only the supplied grounded candidate facts, path, obligations, and competing hypotheses.
Bind the verdict to factual_context.failure_signature.
A functional failure cannot substitute verification omission or false closure for an earlier defect introduction.
Do not assume any component type is or is not causal. Return exactly one JSON object with no markdown."""

GLOBAL_CANDIDATE_SYSTEM_PROMPT = """You globally compare a bounded set of causal candidates.
Use only supplied evidence closures, compare every candidate, and preserve no-defect as a valid outcome.
Retrieval rank is navigation evidence only. Ask for expansion when decisive facts are absent.
Return exactly one JSON object with no markdown."""

FACTOR_ROLE_SYSTEM_PROMPT = """You independently judge one candidate's causal factor role.
Use only the supplied factual request and exact grounded references.
Bind the verdict to factual_context.failure_signature.
Assess necessity and non-root role as independent dimensions.
For every non-empty factor_mechanism, target_ref must be one of the exact downstream recursive path refs in request.recursive_path[1:].
Return exactly one JSON object with no markdown."""

REPAIR_SYSTEM_PROMPT = """Repair one invalid causal-attribution JSON response.
Correct the exact supplied parse, schema, or grounding error using only the supplied request facts.
Return a full replacement object, not a patch. Every field shown in required_json_schema is mandatory.
Apply every required_field_corrections rule literally, including any exact replacement value.
The top-level confidence must be an unquoted JSON number between 0 and 1.
Return exactly one JSON object with no markdown and do not invent references."""

CLUSTER_TRIAGE_REPAIR_SYSTEM_PROMPT = """Repair one invalid candidate-cluster triage JSON response.
Correct the exact supplied parse, schema, binding, coverage, or grounding error using only the supplied canonical page facts.
Return a full replacement object, not a patch. Every field shown in required_json_schema is mandatory and no additional field is allowed.
Preserve navigation-only semantics and return exactly one decision per supplied cluster.
Return exactly one JSON object with no markdown and do not invent references."""


def _repair_system_prompt(stage: str) -> str:
    if stage == "candidate_cluster_triage_page":
        return CLUSTER_TRIAGE_REPAIR_SYSTEM_PROMPT
    return REPAIR_SYSTEM_PROMPT


def _mandatory_output_contract(stage: str) -> JsonDict:
    if stage == "candidate_cluster_triage_page":
        return {
            "all_required_fields_must_be_present": True,
            "exact_top_level_fields": [
                "schema",
                "page_identity",
                "request_identity",
                "partition_identity",
                "page_index",
                "page_count",
                "decisions",
            ],
            "response_shape": "one complete JSON object, not a patch",
        }
    return {
        "all_required_fields_must_be_present": True,
        "confidence": "required unquoted JSON number between 0 and 1",
        "response_shape": "one complete JSON object, not a patch",
    }


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _snapshot_node(node: TraceNode) -> TraceNode:
    return TraceNode(
        ref=node.ref,
        record_id=node.record_id,
        component=node.component,
        event_type=node.event_type,
        title=node.title,
        status=node.status,
        timestamp=node.timestamp,
        data=_freeze_json(node.data),
        source_refs=tuple(str(item) for item in node.source_refs),
    )


def _node_to_dict(node: TraceNode) -> JsonDict:
    return {
        "ref": node.ref,
        "record_id": node.record_id,
        "component": node.component,
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
        "timestamp": node.timestamp,
        "data": _thaw_json(node.data),
        "source_refs": list(node.source_refs),
    }


def _require_factor_role_request_string(value: Any, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(field_name))
    return value


def _require_factor_role_request_path(value: Any) -> Tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(type(item) is not str or not item.strip() for item in value)
    ):
        raise ValueError("recursive_path must contain non-empty string references")
    return tuple(value)


def _validate_factor_role_confirmed_root_summary(
    value: Any,
    *,
    defect_fingerprint: str,
    seed_binding_identity: str,
) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("confirmed root summary must be an object")
    if {str(key) for key in value} != set(
        FACTOR_ROLE_CONFIRMED_ROOT_SUMMARY_KEYS
    ):
        raise ValueError("confirmed root summary has an inexact schema")
    fixed_values = {"schema": FACTOR_ROLE_CONFIRMED_ROOT_SUMMARY_SCHEMA}
    if any(value.get(key) != expected for key, expected in fixed_values.items()):
        raise ValueError("confirmed root summary has invalid canonical values")
    for field_name in (
        "candidate_ref",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "confirmation_identity",
        "defect_fingerprint",
        "seed_binding_identity",
        "reason",
    ):
        _require_factor_role_request_string(
            value.get(field_name),
            field_name="confirmed_root_summaries.{0}".format(field_name),
        )
    evidence_refs = _require_factor_role_request_path(value.get("evidence_refs"))
    recursive_path = _require_factor_role_request_path(value.get("recursive_path"))
    if recursive_path[0] != value["candidate_ref"]:
        raise ValueError(
            "confirmed root summary recursive_path must start with candidate_ref"
        )
    if value["defect_fingerprint"] != defect_fingerprint:
        raise ValueError(
            "confirmed root summary defect_fingerprint must match request"
        )
    if value["seed_binding_identity"] != seed_binding_identity:
        raise ValueError(
            "confirmed root summary seed_binding_identity must match request"
        )
    expected_confirmation_identity = confirmation_identity_for(
        hypothesis_id=value["hypothesis_id"],
        hypothesis_semantic_hash=value["hypothesis_semantic_hash"],
        candidate_ref=value["candidate_ref"],
        defect_fingerprint=defect_fingerprint,
        recursive_path=recursive_path,
        seed_binding_identity=seed_binding_identity,
    )
    if value["confirmation_identity"] != expected_confirmation_identity:
        raise ValueError(
            "confirmed root summary confirmation_identity must match "
            "canonical identity"
        )
    return {
        "schema": FACTOR_ROLE_CONFIRMED_ROOT_SUMMARY_SCHEMA,
        "candidate_ref": value["candidate_ref"],
        "hypothesis_id": value["hypothesis_id"],
        "hypothesis_semantic_hash": value["hypothesis_semantic_hash"],
        "confirmation_identity": value["confirmation_identity"],
        "defect_fingerprint": value["defect_fingerprint"],
        "seed_binding_identity": value["seed_binding_identity"],
        "reason": value["reason"],
        "evidence_refs": list(evidence_refs),
        "recursive_path": list(recursive_path),
    }


def _reference_inference_errors(
    value: Mapping[str, Any],
    *,
    require_explicit_metadata: bool = False,
) -> List[str]:
    provenance = str(value.get("provenance_class") or "").strip()
    if provenance != "inferred":
        return []
    metadata = value.get("inference_metadata")
    if require_explicit_metadata and not isinstance(metadata, Mapping):
        return ["inferred provenance requires auditable inference metadata"]
    metadata = metadata if isinstance(metadata, Mapping) else value
    evidence_type = str(metadata.get("evidence_type") or "").strip().lower()
    inference_method = str(
        metadata.get("inference_method") or ""
    ).strip().lower()
    if not evidence_type or not inference_method:
        return ["inferred provenance requires auditable inference metadata"]
    if "inferred" not in evidence_type:
        return ["inferred provenance contradicts evidence_type"]
    return []


def _reference_envelope_errors(
    value: Mapping[str, Any],
    *,
    require_explicit_inference_metadata: bool = False,
) -> List[str]:
    errors: List[str] = []
    missing = _ENVELOPE_KEYS - set(value)
    if missing:
        return [
            "reference envelope requires {0}".format(
                ", ".join(sorted(missing))
            )
        ]
    raw_ref = str(value.get("raw_ref") or "").strip()
    resolved_ref = str(value.get("resolved_ref") or "").strip()
    resolution = str(value.get("resolution_status") or "").strip().lower()
    provenance = str(value.get("provenance_class") or "").strip()
    if not raw_ref:
        errors.append("reference envelope raw_ref must be non-empty")
    if resolution not in _ALLOWED_RESOLUTION:
        errors.append("reference envelope resolution_status is invalid")
    if (resolution == "resolved") != bool(resolved_ref):
        errors.append(
            "reference envelope has contradictory raw_ref/resolved_ref fields"
        )
    if provenance not in _ALLOWED_PROVENANCE:
        errors.append(
            "provenance_class must be exactly recorded, reconstructed, or inferred"
        )
    errors.extend(
        _reference_inference_errors(
            value,
            require_explicit_metadata=require_explicit_inference_metadata,
        )
    )
    return errors


def _factor_role_resolved_envelope_ref(
    value: Mapping[str, Any],
) -> Optional[str]:
    if any(type(key) is not str for key in value):
        return None
    for field_name in _ENVELOPE_KEYS:
        field_value = value.get(field_name)
        if type(field_value) is not str or not field_value.strip():
            return None
    if value["resolution_status"] != "resolved":
        return None
    provenance = value["provenance_class"]
    if provenance not in _ALLOWED_PROVENANCE:
        return None
    if provenance == "inferred":
        metadata = value.get("inference_metadata")
        if not isinstance(metadata, Mapping):
            return None
        evidence_type = metadata.get("evidence_type")
        inference_method = metadata.get("inference_method")
        if (
            type(evidence_type) is not str
            or not evidence_type.strip()
            or "inferred" not in evidence_type.lower()
            or type(inference_method) is not str
            or not inference_method.strip()
        ):
            return None
    return value["resolved_ref"]


def _reject_factor_role_analysis_control_envelopes(value: Any) -> None:
    """Fail closed only for nested analysis-control envelopes, never fact names."""

    reject_analysis_control_envelopes(value)

    ancestors: Set[int] = set()

    def visit(item: Any) -> None:
        if not isinstance(item, (Mapping, list, tuple)):
            return
        item_id = id(item)
        if item_id in ancestors:
            raise ValueError("factor role facts cannot contain recursive containers")
        ancestors.add(item_id)
        try:
            if isinstance(item, Mapping):
                if (
                    _ENVELOPE_KEYS.issubset(
                        {str(key) for key in item}
                    )
                    and any(type(key) is not str for key in item)
                ):
                    raise ValueError(
                        "reference envelope keys must be exact strings"
                    )
                if item.get("provenance_class") == "analysis_control":
                    raise ValueError(
                        "factor role facts contain an analysis-control envelope"
                    )
                for nested_value in item.values():
                    visit(nested_value)
            else:
                for nested_value in item:
                    visit(nested_value)
        finally:
            ancestors.remove(item_id)

    visit(value)


@dataclass(frozen=True)
class CausalStepRequest:
    recursive_context: Mapping[str, Any]
    current_node: TraceNode
    defect_state: DefectState
    candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recursive_context", _freeze_json(self.recursive_context))
        object.__setattr__(self, "current_node", _snapshot_node(self.current_node))
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def to_dict(self) -> JsonDict:
        return scrub_attribution_verdicts({
            "recursive_context": _thaw_json(self.recursive_context),
            "current_node": _node_to_dict(self.current_node),
            "defect_state": self.defect_state.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
        })


@dataclass(frozen=True)
class RootConfirmationRequest:
    candidate_ref: str
    defect_state: DefectState
    recursive_path: Tuple[str, ...]
    candidate_reference: Mapping[str, Any]
    recursive_path_references: Tuple[Mapping[str, Any], ...]
    supporting_evidence: Tuple[Mapping[str, Any], ...]
    opposing_evidence: Tuple[Mapping[str, Any], ...]
    competing_hypotheses: Tuple[Mapping[str, Any], ...]
    task_obligations: Tuple[Mapping[str, Any], ...]
    analysis_perspective: str
    hypothesis_id: str = ""
    hypothesis_semantic_hash: str = ""
    seed_binding_identity: str = ""
    factual_context: Mapping[str, Any] = field(default_factory=FrozenMapping)
    process_factual_context: Mapping[str, Any] = field(
        default_factory=FrozenMapping
    )

    def __post_init__(self) -> None:
        for raw_facts in (
            self.candidate_reference,
            self.recursive_path_references,
            self.supporting_evidence,
            self.opposing_evidence,
            self.competing_hypotheses,
            self.task_obligations,
            self.factual_context,
            self.process_factual_context,
        ):
            reject_analysis_control_envelopes(raw_facts)
        object.__setattr__(self, "recursive_path", tuple(str(item) for item in self.recursive_path))
        object.__setattr__(
            self,
            "candidate_reference",
            _freeze_json(scrub_attribution_verdicts(self.candidate_reference)),
        )
        object.__setattr__(
            self,
            "recursive_path_references",
            tuple(
                _freeze_json(item)
                for item in scrub_attribution_verdicts(
                    self.recursive_path_references
                )
            ),
        )
        for name in (
            "supporting_evidence",
            "opposing_evidence",
            "competing_hypotheses",
            "task_obligations",
        ):
            scrubbed_items = scrub_attribution_verdicts(
                getattr(self, name)
            )
            object.__setattr__(
                self,
                name,
                tuple(
                    _freeze_json(item) for item in scrubbed_items
                ),
            )
        if self.factual_context:
            object.__setattr__(
                self,
                "factual_context",
                _freeze_json(
                    judge_visible_active_failure_factual_context(
                        scrub_attribution_verdicts(self.factual_context)
                    )
                ),
            )
        else:
            object.__setattr__(
                self,
                "factual_context",
                FrozenMapping(),
            )
        if self.process_factual_context:
            process_facts = scrub_attribution_verdicts(
                self.process_factual_context
            )
            if (
                not isinstance(process_facts, Mapping)
                or set(process_facts)
                != {
                    "schema",
                    "candidate_commitment_cues",
                    "candidate_process_trajectory",
                }
                or process_facts.get("schema")
                != "candidate-process-confirmation-facts/v1"
                or not isinstance(
                    process_facts.get("candidate_commitment_cues"),
                    Mapping,
                )
                or not isinstance(
                    process_facts.get("candidate_process_trajectory"),
                    Mapping,
                )
            ):
                raise ValueError(
                    "process factual context has an inexact schema"
                )
            object.__setattr__(
                self,
                "process_factual_context",
                _freeze_json(process_facts),
            )
        else:
            object.__setattr__(
                self,
                "process_factual_context",
                FrozenMapping(),
            )

    def to_dict(self) -> JsonDict:
        return self.factual_dict()

    def factual_dict(self) -> JsonDict:
        """Return every Judge-visible fact used by verifier prompts and identity."""
        facts = {
            "candidate_ref": self.candidate_ref,
            "defect_state": self.defect_state.to_dict(),
            "recursive_path": list(self.recursive_path),
            "candidate_reference": _thaw_json(self.candidate_reference),
            "recursive_path_references": _thaw_json(self.recursive_path_references),
            "supporting_evidence": _thaw_json(self.supporting_evidence),
            "opposing_evidence": _thaw_json(self.opposing_evidence),
            "competing_hypotheses": _thaw_json(self.competing_hypotheses),
            "task_obligations": _thaw_json(self.task_obligations),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "seed_binding_identity": self.seed_binding_identity,
            "analysis_perspective": self.analysis_perspective,
        }
        if self.factual_context:
            facts["factual_context"] = _thaw_json(self.factual_context)
        if self.process_factual_context:
            facts["process_factual_context"] = _thaw_json(
                self.process_factual_context
            )
        return facts


def root_confirmation_request_projection(
    request: RootConfirmationRequest,
) -> JsonDict:
    if not isinstance(request, RootConfirmationRequest):
        raise TypeError("root confirmation request projection requires a request")
    return validate_root_confirmation_request_projection(
        {
            "schema": ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA,
            "facts": request.factual_dict(),
        }
    )


def validate_root_confirmation_request_projection(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("root confirmation request projection must be an object")
    if (
        {str(key) for key in value}
        != set(ROOT_CONFIRMATION_REQUEST_PROJECTION_KEYS)
        or value.get("schema")
        != ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA
        or not isinstance(value.get("facts"), Mapping)
    ):
        raise ValueError(
            "root confirmation request projection has an unknown version or "
            "exact schema"
        )
    facts = value["facts"]
    fact_keys = frozenset(str(key) for key in facts)
    if (
        not ROOT_CONFIRMATION_REQUEST_FACT_KEYS.issubset(fact_keys)
        or not fact_keys.issubset(
            ROOT_CONFIRMATION_REQUEST_FACT_KEYS
            | ROOT_CONFIRMATION_REQUEST_OPTIONAL_FACT_KEYS
        )
    ):
        raise ValueError(
            "root confirmation request projection facts have an inexact schema"
        )
    mapping_fields = (
        "candidate_reference",
        *(("factual_context",) if "factual_context" in facts else ()),
        *(
            ("process_factual_context",)
            if "process_factual_context" in facts
            else ()
        ),
    )
    sequence_fields = (
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
        "competing_hypotheses",
        "task_obligations",
    )
    string_fields = (
        "candidate_ref",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "seed_binding_identity",
        "analysis_perspective",
    )
    if (
        any(type(facts.get(name)) is not str for name in string_fields)
        or any(
            not isinstance(facts.get(name), Mapping)
            for name in mapping_fields
        )
        or not isinstance(facts.get("recursive_path"), (list, tuple))
        or any(
            type(item) is not str
            for item in facts.get("recursive_path") or ()
        )
        or any(
            not isinstance(facts.get(name), (list, tuple))
            for name in sequence_fields
        )
        or any(
            not isinstance(item, Mapping)
            for name in sequence_fields
            for item in facts.get(name) or ()
        )
        or not isinstance(facts.get("defect_state"), Mapping)
    ):
        raise ValueError(
            "root confirmation request projection facts have invalid types"
        )
    request = RootConfirmationRequest(
        candidate_ref=facts["candidate_ref"],
        defect_state=DefectState.from_dict(dict(facts["defect_state"])),
        recursive_path=tuple(facts["recursive_path"]),
        candidate_reference=facts["candidate_reference"],
        recursive_path_references=tuple(
            facts["recursive_path_references"]
        ),
        supporting_evidence=tuple(facts["supporting_evidence"]),
        opposing_evidence=tuple(facts["opposing_evidence"]),
        competing_hypotheses=tuple(facts["competing_hypotheses"]),
        task_obligations=tuple(facts["task_obligations"]),
        analysis_perspective=facts["analysis_perspective"],
        hypothesis_id=facts["hypothesis_id"],
        hypothesis_semantic_hash=facts["hypothesis_semantic_hash"],
        seed_binding_identity=facts["seed_binding_identity"],
        factual_context=facts.get("factual_context") or {},
        process_factual_context=facts.get("process_factual_context") or {},
    )
    canonical = {
        "schema": ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA,
        "facts": request.factual_dict(),
    }
    if stable_json(_thaw_json(value)) != stable_json(canonical):
        raise ValueError(
            "root confirmation request projection contradicts its canonical "
            "facts"
        )
    return canonical


def root_confirmation_request_projection_identity(value: Any) -> str:
    projection = validate_root_confirmation_request_projection(value)
    return "{0}{1}".format(
        ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX,
        hashlib.sha256(stable_json(projection).encode("utf-8")).hexdigest(),
    )


def root_confirmation_request_identity(
    request: RootConfirmationRequest,
) -> str:
    return root_confirmation_request_projection_identity(
        root_confirmation_request_projection(request)
    )


@dataclass(frozen=True)
class FactorRoleRequest:
    """Immutable, blind factual snapshot for independent factor-role review."""

    candidate_ref: str
    defect_state: DefectState
    recursive_path: Tuple[str, ...]
    candidate_reference: Mapping[str, Any]
    recursive_path_references: Tuple[Mapping[str, Any], ...]
    supporting_evidence: Tuple[Mapping[str, Any], ...]
    opposing_evidence: Tuple[Mapping[str, Any], ...]
    task_obligations: Tuple[Mapping[str, Any], ...]
    confirmed_root_summaries: Tuple[Mapping[str, Any], ...]
    hypothesis_id: str
    hypothesis_semantic_hash: str
    seed_binding_identity: str
    analysis_perspective: str
    factual_context: Mapping[str, Any] = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_ref",
            "hypothesis_id",
            "hypothesis_semantic_hash",
            "seed_binding_identity",
            "analysis_perspective",
        ):
            _require_factor_role_request_string(
                getattr(self, field_name), field_name=field_name
            )
        if not isinstance(self.defect_state, DefectState):
            raise ValueError("defect_state must be a DefectState")
        recursive_path = _require_factor_role_request_path(
            self.recursive_path
        )
        if recursive_path[0] != self.candidate_ref:
            raise ValueError(
                "recursive_path must start with candidate_ref"
            )
        object.__setattr__(self, "recursive_path", recursive_path)
        _reject_factor_role_analysis_control_envelopes(
            self.candidate_reference
        )
        candidate_reference = scrub_attribution_verdicts(
            self.candidate_reference
        )
        if not isinstance(candidate_reference, Mapping):
            raise ValueError("candidate_reference must be an object")
        _reject_factor_role_analysis_control_envelopes(candidate_reference)
        object.__setattr__(self, "candidate_reference", _freeze_json(candidate_reference))
        for name in (
            "recursive_path_references",
            "supporting_evidence",
            "opposing_evidence",
            "task_obligations",
        ):
            facts = getattr(self, name)
            if (
                not isinstance(facts, (list, tuple))
                or any(not isinstance(item, Mapping) for item in facts)
            ):
                raise ValueError("{0} must be an array of objects".format(name))
            _reject_factor_role_analysis_control_envelopes(facts)
            scrubbed_facts = tuple(scrub_attribution_verdicts(facts))
            _reject_factor_role_analysis_control_envelopes(scrubbed_facts)
            object.__setattr__(self, name, tuple(_freeze_json(item) for item in scrubbed_facts))
        summaries = self.confirmed_root_summaries
        if (
            not isinstance(summaries, (list, tuple))
            or any(not isinstance(item, Mapping) for item in summaries)
        ):
            raise ValueError("confirmed_root_summaries must be an array of objects")
        _reject_factor_role_analysis_control_envelopes(summaries)
        canonical_summaries = tuple(
            _validate_factor_role_confirmed_root_summary(
                summary,
                defect_fingerprint=self.defect_state.fingerprint,
                seed_binding_identity=self.seed_binding_identity,
            )
            for summary in summaries
        )
        object.__setattr__(
            self,
            "confirmed_root_summaries",
            tuple(_freeze_json(item) for item in canonical_summaries),
        )
        if self.factual_context:
            _reject_factor_role_analysis_control_envelopes(
                self.factual_context
            )
            object.__setattr__(
                self,
                "factual_context",
                _freeze_json(
                    judge_visible_active_failure_factual_context(
                        scrub_attribution_verdicts(self.factual_context)
                    )
                ),
            )
        else:
            object.__setattr__(
                self,
                "factual_context",
                FrozenMapping(),
            )

    def to_dict(self) -> JsonDict:
        return self.factual_dict()

    def factual_dict(self) -> JsonDict:
        """Return all and only the facts visible to the factor-role Judge."""
        facts = {
            "candidate_ref": self.candidate_ref,
            "defect_state": self.defect_state.to_dict(),
            "recursive_path": list(self.recursive_path),
            "candidate_reference": _thaw_json(self.candidate_reference),
            "recursive_path_references": _thaw_json(self.recursive_path_references),
            "supporting_evidence": _thaw_json(self.supporting_evidence),
            "opposing_evidence": _thaw_json(self.opposing_evidence),
            "task_obligations": _thaw_json(self.task_obligations),
            "confirmed_root_summaries": _thaw_json(self.confirmed_root_summaries),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "seed_binding_identity": self.seed_binding_identity,
            "analysis_perspective": self.analysis_perspective,
        }
        if self.factual_context:
            facts["factual_context"] = _thaw_json(self.factual_context)
        return facts


def factor_role_request_projection(request: FactorRoleRequest) -> JsonDict:
    if not isinstance(request, FactorRoleRequest):
        raise TypeError("factor role request projection requires a request")
    return validate_factor_role_request_projection(
        {
            "schema": FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA,
            "facts": request.factual_dict(),
        }
    )


def _factor_role_request_from_canonical_facts(
    facts: Mapping[str, Any],
) -> FactorRoleRequest:
    return FactorRoleRequest(
        candidate_ref=facts["candidate_ref"],
        defect_state=DefectState.from_dict(dict(facts["defect_state"])),
        recursive_path=tuple(facts["recursive_path"]),
        candidate_reference=facts["candidate_reference"],
        recursive_path_references=tuple(
            facts["recursive_path_references"]
        ),
        supporting_evidence=tuple(facts["supporting_evidence"]),
        opposing_evidence=tuple(facts["opposing_evidence"]),
        task_obligations=tuple(facts["task_obligations"]),
        confirmed_root_summaries=tuple(
            facts["confirmed_root_summaries"]
        ),
        hypothesis_id=facts["hypothesis_id"],
        hypothesis_semantic_hash=facts["hypothesis_semantic_hash"],
        seed_binding_identity=facts["seed_binding_identity"],
        analysis_perspective=facts["analysis_perspective"],
        factual_context=facts.get("factual_context") or {},
    )


def validate_factor_role_request_projection(value: Any) -> JsonDict:
    if not isinstance(value, Mapping):
        raise ValueError("factor role request projection must be an object")
    if (
        {str(key) for key in value} != set(FACTOR_ROLE_REQUEST_PROJECTION_KEYS)
        or value.get("schema") != FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA
        or not isinstance(value.get("facts"), Mapping)
    ):
        raise ValueError("factor role request projection has an unknown version or exact schema")
    facts = value["facts"]
    fact_keys = frozenset(str(key) for key in facts)
    if fact_keys not in {
        frozenset(FACTOR_ROLE_REQUEST_FACT_KEYS),
        frozenset(
            FACTOR_ROLE_REQUEST_FACT_KEYS
            | FACTOR_ROLE_REQUEST_OPTIONAL_FACT_KEYS
        ),
    }:
        raise ValueError("factor role request projection facts have an inexact schema")
    mapping_fields = ("candidate_reference",) + (
        ("factual_context",) if "factual_context" in facts else ()
    )
    sequence_fields = (
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
        "task_obligations",
        "confirmed_root_summaries",
    )
    string_fields = (
        "candidate_ref",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "seed_binding_identity",
        "analysis_perspective",
    )
    if (
        any(type(facts.get(name)) is not str for name in string_fields)
        or any(not isinstance(facts.get(name), Mapping) for name in mapping_fields)
        or not isinstance(facts.get("recursive_path"), (list, tuple))
        or any(type(item) is not str for item in facts.get("recursive_path") or ())
        or any(not isinstance(facts.get(name), (list, tuple)) for name in sequence_fields)
        or any(
            not isinstance(item, Mapping)
            for name in sequence_fields
            for item in facts.get(name) or ()
        )
        or not isinstance(facts.get("defect_state"), Mapping)
    ):
        raise ValueError("factor role request projection facts have invalid types")
    request = _factor_role_request_from_canonical_facts(facts)
    canonical = {
        "schema": FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA,
        "facts": request.factual_dict(),
    }
    if stable_json(_thaw_json(value)) != stable_json(canonical):
        raise ValueError("factor role request projection contradicts its canonical facts")
    return canonical


def factor_role_request_from_projection(value: Any) -> FactorRoleRequest:
    projection = validate_factor_role_request_projection(value)
    return _factor_role_request_from_canonical_facts(
        projection["facts"]
    )


def factor_role_request_projection_identity(value: Any) -> str:
    projection = validate_factor_role_request_projection(value)
    return "{0}{1}".format(
        FACTOR_ROLE_REQUEST_IDENTITY_PREFIX,
        hashlib.sha256(stable_json(projection).encode("utf-8")).hexdigest(),
    )


def factor_role_request_identity(request: FactorRoleRequest) -> str:
    return factor_role_request_projection_identity(factor_role_request_projection(request))


def _factor_role_fact_refs(value: Any) -> Set[str]:
    refs: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            resolved_ref = _factor_role_resolved_envelope_ref(item)
            if resolved_ref is not None:
                refs.add(resolved_ref)
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return refs


def _factor_role_allowed_refs_from_facts(facts: Mapping[str, Any]) -> Set[str]:
    refs: Set[str] = set()
    candidate_ref = facts.get("candidate_ref")
    if type(candidate_ref) is str and candidate_ref:
        refs.add(candidate_ref)
    recursive_path = facts.get("recursive_path")
    if isinstance(recursive_path, (list, tuple)):
        refs.update(
            ref for ref in recursive_path if type(ref) is str and ref
        )
    for field_name in (
        "candidate_reference",
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
    ):
        refs.update(_factor_role_fact_refs(facts.get(field_name)))
    root_summaries = facts.get("confirmed_root_summaries")
    if isinstance(root_summaries, (list, tuple)):
        defect_state = facts.get("defect_state")
        defect_fingerprint = (
            defect_state.get("fingerprint")
            if isinstance(defect_state, Mapping)
            else None
        )
        seed_binding_identity = facts.get("seed_binding_identity")
        if (
            type(defect_fingerprint) is str
            and type(seed_binding_identity) is str
        ):
            for summary in root_summaries:
                try:
                    canonical = _validate_factor_role_confirmed_root_summary(
                        summary,
                        defect_fingerprint=defect_fingerprint,
                        seed_binding_identity=seed_binding_identity,
                    )
                except ValueError:
                    continue
                refs.add(canonical["candidate_ref"])
                refs.update(canonical["evidence_refs"])
                refs.update(canonical["recursive_path"])
    return refs


def factor_role_request_fact_refs(request: FactorRoleRequest) -> Set[str]:
    return _factor_role_allowed_refs_from_facts(request.factual_dict())


def _unknown_factor_role_judgment(
    request: FactorRoleRequest, *, reason: str
) -> FactorRoleJudgment:
    contract_entry = factor_role_contract_entry("unknown", "unknown")
    if contract_entry is None:
        raise ValueError("unknown factor fallback is absent from role contract")
    return FactorRoleJudgment(
        candidate_ref=request.candidate_ref,
        necessity_status="unknown",
        factor_role="unknown",
        reason=reason,
        confidence=0.0,
        evidence_refs=(request.candidate_ref,),
        recursive_path=request.recursive_path,
        factor_mechanism={},
        counterfactual={
            "schema": "factor-role-counterfactual/v1",
            "intervention_ref": request.candidate_ref,
            "intervention_kind": "replace_with_semantically_correct_behavior",
            "predicted_effect": contract_entry["predicted_effects"][0],
        },
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        seed_binding_identity=request.seed_binding_identity,
        analysis_perspective=request.analysis_perspective,
        request_identity=factor_role_request_identity(request),
    )


class CausalJudge(Protocol):
    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        raise NotImplementedError

    def judge_factor_role(
        self, request: FactorRoleRequest
    ) -> FactorRoleJudgment:
        raise NotImplementedError


class BoundedJudgeCapability:
    """Nominal capability for Judges that enforce a physical request allowance."""

    def judge_step_bounded(
        self,
        request: CausalStepRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> "BoundedJudgeCallResult":
        raise NotImplementedError

    def confirm_candidate_bounded(
        self,
        request: RootConfirmationRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> "BoundedJudgeCallResult":
        raise NotImplementedError

    def judge_factor_role_bounded(
        self,
        request: FactorRoleRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> "BoundedJudgeCallResult":
        raise NotImplementedError


@dataclass(frozen=True)
class BoundedJudgeCallResult:
    """A bounded Judge result with exact physical transport accounting."""

    value: Any
    physical_requests: int
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.physical_requests, bool)
            or not isinstance(self.physical_requests, int)
            or self.physical_requests < 0
        ):
            raise ValueError("physical_requests must be a non-negative integer")
        if not isinstance(self.diagnostics, Mapping):
            raise TypeError("judge diagnostics must be an object")
        object.__setattr__(
            self,
            "diagnostics",
            _freeze_json(self.diagnostics),
        )


class BoundedJudgeCallError(RuntimeError):
    """A bounded capability failure that preserves exact physical usage."""

    def __init__(
        self,
        message: str,
        *,
        physical_requests: int,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if (
            isinstance(physical_requests, bool)
            or not isinstance(physical_requests, int)
            or physical_requests < 0
        ):
            raise ValueError("physical_requests must be a non-negative integer")
        super().__init__(message)
        self.physical_requests = physical_requests
        self.diagnostics = _freeze_json(diagnostics or {})


class OfflineJudgeCapability:
    """Nominal capability for Judges guaranteed to perform no transport requests."""

    def judge_step_offline(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge_step(request)

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        raise NotImplementedError

    def confirm_candidate_offline(
        self, request: RootConfirmationRequest
    ) -> RootConfirmation:
        return self.confirm_candidate(request)

    def judge_factor_role_offline(
        self, request: FactorRoleRequest
    ) -> FactorRoleJudgment:
        return self.judge_factor_role(request)

    def judge_factor_role(
        self, request: FactorRoleRequest
    ) -> FactorRoleJudgment:
        return _unknown_factor_role_judgment(
            request,
            reason=(
                "Legacy offline Judge does not implement independent "
                "factor-role judgment."
            ),
        )


class OfflineCausalJudgeAdapter(OfflineJudgeCapability):
    """Explicitly opt a legacy in-process Judge into zero-transport execution."""

    def __init__(self, judge: CausalJudge):
        self.judge = judge

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge.judge_step(request)

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        return self.judge.confirm_candidate(request)

    def judge_factor_role(
        self, request: FactorRoleRequest
    ) -> FactorRoleJudgment:
        method = getattr(self.judge, "judge_factor_role", None)
        implementation = getattr(method, "__func__", method)
        if (
            not callable(method)
            or implementation is CausalJudge.judge_factor_role
        ):
            return _unknown_factor_role_judgment(
                request,
                reason=(
                    "Legacy offline Judge does not implement independent "
                    "factor-role judgment."
                ),
            )
        return method(request)


def build_causal_step_prompt(request: CausalStepRequest) -> str:
    judge_request = request.to_dict()
    return stable_json(
        {
            "request": judge_request,
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Judge semantics and evidence, not component labels; any component can be causal.",
                "Every field shown in required_json_schema is mandatory; return the complete object, not a partial object or patch.",
                "The top-level confidence must be an unquoted JSON number between 0 and 1.",
                *RELATION_DEFINITIONS,
                "Return sparse ranked assessments only for offered candidates that plausibly propagate, transform, condition, expose, or materially challenge the active defect; omit low-value candidates instead of emitting unrelated filler.",
                "Order predecessor assessments from strongest to weakest causal relevance and never duplicate a ref. Omitted offered candidates are recorded deterministically as unselected, not treated as model judgments.",
                "The current node itself is never a predecessor. Emit a predecessor object only for an exact ref listed in request.candidates; downstream_path refs are navigation context, not offered predecessors.",
                "Sparse omission is allowed only while candidate_introduction=false. Before setting candidate_introduction=true, assess every offered predecessor so the no-viable-defective-predecessor conclusion is auditable.",
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; bare refs never ground evidence.",
                "Use same_defect_propagation only when the predecessor already carries the active defect.",
                "Prompt, message, context-transform, LLM-call, and task-loop nodes are input provenance envelopes. Being prompted, consumed, transformed, or produced establishes dataflow only. Recurse through such a node only when grounded content shows that the node itself already contains an incorrect, ambiguous, contradictory, or omitted semantic that carries the defect.",
                "Use defect_transformation only when a different upstream defect transforms into the active defect through an explicit mechanism.",
                "When request.defect_state.label=candidate_local_process_defect, judge the current node's candidate-local process defect, not whether it created the pre-existing downstream functional defect. A baseline code gap may predate the node while the node still introduces an erroneous plan, priority drift, action/commitment mismatch, or responsible non-repair that materially prevents repair.",
                "For a candidate-local process defect, current_defect_status=present means the grounded node semantics themselves contain that process defect. Explain the transformation into the downstream failure; do not mark the node absent merely because the final functional defect existed before the Agent run.",
                "When request.defect_state.label starts with localized_ and ends with _deviation, the offline premise gate has localized a supported behavioral divergence but has not assigned root cause. Judge whether the current node is the first semantic commitment to that divergence. If its recorded reasoning explicitly chooses behavior contrary to the supplied contract and no offered predecessor already contains that wrong commitment, set current_defect_status=present, candidate_introduction=true, assess every offered predecessor, and request independent root confirmation.",
                "For a localized behavioral deviation, distinguish the earlier reasoning or planning commitment that introduces the wrong action/order from the later tool call that only materializes it.",
                "When candidate_process_trajectory is supplied, jointly assess the candidate semantics and its bounded post-candidate execution facts. Repeated no-delivery episodes, zero mutations or verification, and an unfulfilled implementation commitment may ground a process defect; they do not make every earlier plan defective by temporal association alone.",
                "For candidate_local_process_defect, complete process_assessment before choosing current_defect_status. Classify the candidate role, any explicit commitment, and whether the bounded post-candidate trajectory materialized or diverged from that commitment.",
                "candidate_commitment_cues contains exact recorded first-person forward-action language and is explicitly not a verdict. For every supplied cue, process_assessment must classify it as commitment, non_commitment, or ambiguous and explain that classification. Never silently ignore it as not_applicable.",
                "A strong cue such as 'I will write/implement/fix the required repair' is an implementation commitment unless candidate-local grounded evidence shows it is quoted third-party text, a hypothetical, or otherwise non-binding. Continued investigation after the statement does not retroactively erase the commitment.",
                "An explicit forward implementation commitment followed by a sufficient grounded opportunity window with no corresponding mutation, verification, or delivery is an unfulfilled commitment and responsible omission. Judge that lifecycle defect rather than declaring the immediate search/tool action locally reasonable. A recorded external interruption excuses non-delivery only when it removed every reasonable opportunity to act.",
                "Trajectory counters are authoritative execution facts. commitment_status=fulfilled and trajectory_relation=materialized require post_candidate_delivery_observed=true plus grounded mutation or verification evidence; never infer eventual implementation when the supplied trajectory records zero mutation and zero verification through the trace boundary.",
                "For responsible omission, obligation_refs must cite exact obligation_id, ref, or source values supplied in task_obligations. Never invent array-position references such as task_obligations[0].",
                "A candidate that merely performs a reasonable bounded investigation remains absent. A candidate that, despite a known repair obligation, commits to implementation but continues an unbounded no-delivery trajectory, or explicitly deprioritizes the blocking repair without grounded justification, may be a candidate-local process defect or responsible non-repair.",
                "Set recurse=true for same_defect_propagation and defect_transformation, and require current_defect_status=present plus at least one grounded direct evidence ref.",
                "Set recurse=false for introduction_candidate, outcome_evidence, unrelated, unknown, and non-material contributing conditions.",
                "For a material contributing_condition such as an authored assumption, plan, or test-oracle decision whose correction could prevent the downstream defect, set recurse=true, cite grounded direct evidence, and provide an upstream_defect with label, mechanism, and transformation_reason. This creates a separate backward hypothesis; do not use it for incidental background context.",
                "introduction_candidate is reserved for the current-node candidate_introduction verdict and must never be assigned as an offered predecessor relation; recurse to a suspected predecessor introduction using same_defect_propagation or defect_transformation, then judge that node directly.",
                "A transformation must provide label, mechanism, and transformation_reason for the upstream defect.",
                "Set candidate_introduction=true only when the current node is defective and no viable defective predecessor remains.",
                "A present defect cannot terminate silently: either select a grounded recursive predecessor, set candidate_introduction=true after assessing every offered predecessor, or provide concrete blocking missing_evidence.",
                "Navigation aggregates, offline-only reconstruction nodes, observed outcomes, context packaging, and lifecycle-start nodes are not eligible introduction roots; keep candidate_introduction=false and recurse through eligible concrete predecessors.",
                "A navigation aggregate is a routing projection, not a defective component. Set current_defect_status=absent when the aggregate itself is clean, but still select at most two strongest recursive predecessors that route the active downstream defect. Absent without a recursive route cannot close the branch; return unknown with concrete missing evidence when no grounded route is available.",
                "Within a navigation aggregate, contributing_condition always denotes a material route and therefore requires recurse=true plus an explicit upstream_defect. Use outcome_evidence or unrelated for non-recursive candidates. Before returning unknown, assess every offered predecessor and state concrete missing evidence.",
                "A root candidate is the earliest trace-visible introduction of the active defect, not necessarily its ultimate real-world origin.",
                "When a boundary event such as process.signal directly records the active defect and no offered predecessor carries it, set candidate_introduction=true; an unknown external sender is outside the trace-visible attribution scope, so mention that scope limit in the reason but do not list it as blocking missing_evidence or move the root to an unrelated earlier node.",
                "When candidate_introduction=true with no blocking missing_evidence, suggested_investigation must request the request_root_confirmation action using the exact active_hypothesis_id, current node ref, and active defect fingerprint from the supplied recursive context.",
                "Cite only resolved refs supplied in the grounded request context.",
                "Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Only recorded, reconstructed, or auditable non-temporal inferred provenance is eligible; temporal-only evidence is never confirmation evidence.",
                "Competing hypotheses remain open unless explicitly rejected or superseded, and unresolved alternatives require unknown.",
                "Use unknown and list missing evidence when the grounded facts are insufficient.",
                "Suggest at most one bounded evidence investigation only for unknown, conflicting, truncated, or missing evidence.",
                "Evidence investigations are limited to inspect_node, expand_upstream, expand_downstream, inspect_artifact, inspect_episode, inspect_context_lineage, inspect_task_obligations, compare_causal_paths, and search_semantic_nodes; provide validated arguments and a concrete evidence-gap reason.",
                "Attribution controls record_hypothesis, reject_hypothesis, and request_root_confirmation are separate from evidence tools; Task 6 rejection requires cited grounded opposing evidence and never trusts a model-authored verifier result.",
                "A root-confirmation request must cite the exact active hypothesis_id, its candidate_root_ref, and the active defect_fingerprint; execution remains deferred to the independent Task 7 verifier.",
                "Independent confirmation uses a structured counterfactual with intervention_ref, intervention_kind, predicted_defect_status, and causal_effect; model-authored explanation text is not authoritative.",
            ],
            "required_json_schema": {
                "current_node_ref": request.current_node.ref,
                "current_defect_status": "present|absent|unknown",
                "current_defect_reason": "non-empty evidence-based reason",
                "predecessors": [
                    {
                        "ref": "offered candidate ref",
                        "relation": "same_defect_propagation|defect_transformation|contributing_condition|outcome_evidence|unrelated|unknown",
                        "reason": "direct causal explanation",
                        "confidence": 0.8,
                        "recurse": False,
                        "upstream_defect": None,
                        "evidence_refs": [],
                        "missing_evidence": [],
                    }
                ],
                "candidate_introduction": False,
                "process_assessment": {
                    "candidate_role": "bounded_investigation|implementation_commitment|priority_decision|action_selection|closure|other|unknown",
                    "commitment_status": "not_applicable|fulfilled|unfulfilled|unknown",
                    "trajectory_relation": "materialized|diverged|remained_investigation|unknown",
                    "failure_mode": "positive_introduction|responsible_omission|ordinary_non_repair|omission_enabling_condition|none|unknown",
                    "commitment_cue_disposition": "commitment|non_commitment|ambiguous|not_applicable",
                    "commitment_cue_reason": "explicit classification of supplied recorded cues",
                    "obligation_refs": [],
                    "reason": "candidate-local lifecycle assessment",
                    "evidence_refs": [],
                } if request.defect_state.label == "candidate_local_process_defect" else None,
                "missing_evidence": [],
                "suggested_investigation": "null | {tool, arguments, reason} | {action, arguments, reason}",
                "confidence": 0.8,
            },
            "root_confirmation_action_schema": {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context.get("active_hypothesis_id", "exact active hypothesis id"),
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "why this trace-visible introduction needs independent confirmation",
            },
        }
    )


def build_recursive_confirmation_prompt(request: RootConfirmationRequest) -> str:
    judge_request = request.factual_dict()
    return stable_json(
        {
            "request": judge_request,
            "falsification_checks": [
                "The candidate directly contains the tracked defect or a causally explanatory upstream defect.",
                "The candidate precedes the downstream result.",
                "No checked predecessor more completely propagates or transforms the defect.",
                "Replacing the candidate with semantically correct behavior would probably prevent the defect.",
                "No competing hypothesis more completely explains the result.",
                "The conclusion is supported by candidate-local semantics, artifacts, path, and obligations.",
            ],
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Try to falsify the candidate independently; no first-pass verdict is supplied.",
                "When process_factual_context is supplied, independently classify the exact commitment cues against the bounded post-candidate trajectory; the cues and counters are facts, not a carried-forward first-pass verdict.",
                "A reasoning_block that records the model's selected plan or forward commitment is a trace-visible Agent decision output, not passive telemetry. Judge the decision together with whether its obligation-consistent follow-up action occurred.",
                "For an implementation commitment, zero mutation, zero verification, and repeated no-delivery episodes through the observation boundary may establish an unfulfilled commitment or responsible omission. Cite the candidate ref and at least one exact trajectory episode ref.",
                "For candidate_local_process_defect, the existing repository gap is the task precondition the Agent was asked to repair; it does not by itself exculpate a grounded action/commitment mismatch or responsible omission.",
                "For a process-defect counterfactual, replace the candidate decision and its committed lifecycle with semantically correct, obligation-consistent follow-up action, such as implementing and verifying the promised repair or a grounded justified reprioritization. Do not model the intervention as merely deleting reasoning text.",
                "When process_factual_context is supplied, process_confirmation_assessment is mandatory. Its downstream_failure_after_intervention must exactly match counterfactual.predicted_defect_status so the lifecycle analysis and top-level verdict cannot contradict each other.",
                "Trajectory counters are authoritative: never infer delivery when post_candidate_delivery_observed=false and mutation and verification counts are zero.",
                "Every field shown in required_json_schema is mandatory; return the complete object, not a partial object or patch.",
                "The top-level confidence must be an unquoted JSON number between 0 and 1.",
                "Any component may be confirmed when the grounded semantics support it.",
                "This is perspective-neutral factual confirmation; presentation perspective is applied only after the confirmed set is fixed.",
                *RELATION_DEFINITIONS,
                "A reference is grounded only by an explicit reference envelope containing resolved_ref, resolution_status=resolved, and provenance_class; candidate_ref and recursive path strings are navigation only.",
                "same_defect_propagation and defect_transformation require current_defect_status=present, recurse=true, and at least one grounded direct evidence ref; every other relation requires recurse=false.",
                "A confirmed excerpt must occur only in a fully resolved candidate-local fact or hydrated artifact that is not missing or truncated.",
                "Cite only refs whose explicit reference envelope is resolved.",
                "Unresolved opposing evidence, ambiguous path references, and missing or truncated decisive candidate evidence require unknown and can never confirm a root.",
                "Walk nested artifact containers recursively. Artifact content is usable only through a resolved artifact reference envelope or a validated Task 2 hydration manifest; missing, truncated, unresolved, envelope-less, or contradictory artifacts require unknown.",
                "Inspect every nested mapping and list without stopping at a valid parent envelope; every reference-bearing key must resolve through a complete offered envelope or validated hydration manifest.",
                "Any nested unknown, unresolved, missing, truncated, provider_error, provider_unavailable, provider circuit, missing_evidence, unresolved reference, or true *_budget_exhausted state requires unknown.",
                "Only provenance_class=recorded|reconstructed|inferred is valid. Inferred provenance requires auditable non-temporal inference metadata, and temporal-only evidence can never confirm a root.",
                "Every competing hypothesis must have resolved reference envelopes. Independently compare active, supported, and unresolved alternatives; a grounded open alternative does not by itself decide the result.",
                "candidate_introduction requires no viable defective predecessor.",
                "Confirm the earliest trace-visible introduction, not an unobserved ultimate real-world origin; an unknown external sender does not by itself invalidate a grounded process.signal root.",
                "Use unknown when evidence is missing, unresolved, ambiguous, truncated, or insufficient.",
                "The counterfactual object must use intervention_ref=candidate_ref and intervention_kind=replace_with_semantically_correct_behavior.",
                "confirmed requires predicted_defect_status=absent and causal_effect=prevents_defect; unknown requires unknown and unknown; rejected allows present with does_not_prevent_defect or unknown with unknown.",
                "confirmed requires factor_role=necessary_cause; rejected may classify a grounded contributing_condition, amplifying_factor, unrelated alternative, or unknown; unknown requires factor_role=unknown.",
                "factor_mechanism must be null for necessary_cause, unrelated, and unknown; use causal_factor_mechanism_schema only for contributing_condition or amplifying_factor.",
            ],
            "required_json_schema": {
                "candidate_ref": request.candidate_ref,
                "status": "confirmed|rejected|unknown",
                "factor_role": "necessary_cause|contributing_condition|amplifying_factor|unrelated|unknown",
                "excerpt": "candidate-local excerpt; required for confirmed",
                "reason": "falsification result or missing evidence",
                "counterfactual": {
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "absent|present|unknown",
                    "causal_effect": "prevents_defect|does_not_prevent_defect|unknown",
                },
                "confidence": 0.8,
                "evidence_refs": [],
                "competitor_comparisons": [
                    {
                        "hypothesis_id": "one offered open competitor",
                        "hypothesis_semantic_hash": "offered semantic hash",
                        "candidate_ref": "offered competitor candidate",
                        "defect_fingerprint": "offered active defect fingerprint",
                        "confirmation_identity": "offered complete confirmation identity",
                        "recursive_path": [],
                        "requires_independent_confirmation": True,
                        "status": "outperformed|rejected|co_root|unresolved",
                        "reason": "grounded comparison",
                        "evidence_refs": [],
                    }
                ],
                "factor_mechanism": None,
                "process_confirmation_assessment": {
                    "candidate_role": "implementation_commitment|bounded_investigation|priority_decision|action_selection|other|unknown",
                    "commitment_cue_disposition": "commitment|non_commitment|ambiguous|not_applicable",
                    "commitment_status": "fulfilled|unfulfilled|not_applicable|unknown",
                    "trajectory_relation": "materialized|diverged|remained_investigation|unknown",
                    "intervention_scope": "decision_and_committed_followup|decision_only|unknown",
                    "task_precondition_disposition": "repair_obligation|exculpatory_precondition|ambiguous",
                    "active_process_defect_after_intervention": "absent|present|unknown",
                    "downstream_failure_after_intervention": "absent|present|unknown",
                    "reason": "independent lifecycle counterfactual analysis",
                    "evidence_refs": [],
                } if request.process_factual_context else None,
            },
            "causal_factor_mechanism_schema": {
                    "mechanism_type": "enabling_condition|amplification",
                    "source_ref": request.candidate_ref,
                    "target_ref": "grounded recursive path ref",
                    "effect": "non-empty causal mechanism",
            },
        }
    )


def _factor_role_boundary_contract() -> JsonDict:
    return {
        "temporal_position_rule": (
            "Temporal downstream position alone does not establish "
            "downstream_materialization."
        ),
        "observation_only_verification": {
            "required_factor_role": "unrelated",
            "rule": (
                "A verification, test, or diagnostic node that only "
                "observes an already existing defect, does not change the "
                "delivered state, and does not alter defect persistence, "
                "severity, or exposure is unrelated."
            ),
        },
        "repair_window_closing_interruption": {
            "required_factor_role": "amplifying_factor",
            "rule": (
                "A timeout, interruption, or shutdown that closes or "
                "reduces a still-open repair opportunity and thereby "
                "increases defect persistence or exposure is an "
                "amplifying_factor, not a downstream_materialization."
            ),
        },
        "downstream_materialization": {
            "required_factor_role": "downstream_materialization",
            "rule": (
                "Use downstream_materialization only when the candidate "
                "executes, stores, emits, or delivers the already introduced "
                "defective state as part of the causal output path."
            ),
        },
    }


def build_factor_role_prompt(request: FactorRoleRequest) -> str:
    contract_enums = {
        field_name: "|".join(
            dict.fromkeys(
                row[field_name] for row in FACTOR_ROLE_CONTRACT
            )
        )
        for field_name in ("necessity_status", "factor_role")
    }
    allowed_fact_refs = sorted(factor_role_request_fact_refs(request))
    evidence_example = [
        allowed_fact_refs[allowed_fact_refs.index(request.candidate_ref)]
    ]
    return stable_json(
        {
            "request": request.factual_dict(),
            "allowed_fact_refs": allowed_fact_refs,
            "allowed_mechanism_target_refs": list(
                request.recursive_path[1:]
            ),
            "rules": [
                TEMPORAL_CAUSALITY_RULE,
                "Necessity and non-root factor role are independent dimensions and must be assessed separately.",
                "Use the selected role_contract row as the only authority for necessity, factor role, mechanism type, and predicted effect.",
                "A downstream materialization executes, stores, exposes, or reports an already introduced defect; it does not introduce or causally enable that defect.",
                "Apply role_boundary_contract before selecting a role; observation-only verification is unrelated, while an interruption that closes an open repair window is an amplifying factor.",
                "A downstream materialization may still be necessary for the observed final defect when replacing that materialization prevents the defect; this pair is valid only when confirmed_root_summaries contains a confirmed upstream root whose recursive path includes the candidate and then exactly follows the candidate recursive_path.",
                "unrelated means no grounded causal influence is established.",
                "unknown is required when the factual request is insufficient.",
                "Select exactly one role_contract row. Necessity status, factor role, factor mechanism shape and type, and predicted effect must all come from that same row; cross-row combinations are forbidden.",
                "factor_mechanism is always present and must exactly match the selected role_contract row: return {} for an empty row mechanism, otherwise return the complete exact object.",
                "Cite only exact references listed in allowed_fact_refs for evidence_refs and factor_mechanism references; ordinary nested ref, *_ref, and *_refs fields are not grounded refs unless their resolved reference appears in allowed_fact_refs.",
                "evidence_refs must contain at least one exact grounded evidence ref; an empty array is invalid.",
                "factor_mechanism.source_ref must be the exact candidate_ref and factor_mechanism.target_ref must be one exact downstream recursive path ref from request.recursive_path[1:] and allowed_mechanism_target_refs.",
                "Return the exact request identity and preserve every candidate, hypothesis, defect, seed, and perspective binding verbatim.",
                "Every field shown in required_json_schema is mandatory; return the complete object with no extra fields.",
                "The top-level confidence must be an unquoted JSON number between 0 and 1.",
            ],
            "required_json_schema": {
                "candidate_ref": request.candidate_ref,
                "necessity_status": contract_enums["necessity_status"],
                "factor_role": contract_enums["factor_role"],
                "reason": "non-empty evidence-based reason",
                "confidence": 0.8,
                "evidence_refs": evidence_example,
                "recursive_path": list(request.recursive_path),
                "factor_mechanism": (
                    "{} | exact factor-role-mechanism object selected by "
                    "factor_mechanism_contract"
                ),
                "counterfactual": {
                    "schema": "factor-role-counterfactual/v1",
                    "intervention_ref": request.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_effect": (
                        "one predicted_effects value from the same "
                        "role_contract row"
                    ),
                },
                "hypothesis_id": request.hypothesis_id,
                "hypothesis_semantic_hash": request.hypothesis_semantic_hash,
                "defect_fingerprint": request.defect_state.fingerprint,
                "seed_binding_identity": request.seed_binding_identity,
                "analysis_perspective": request.analysis_perspective,
                "request_identity": factor_role_request_identity(request),
            },
            "role_contract": _thaw_json(FACTOR_ROLE_CONTRACT),
            "role_boundary_contract": _factor_role_boundary_contract(),
            "dimension_contract": {
                "necessity_status": (
                    "Whether replacing this candidate with semantically "
                    "correct behavior prevents the observed final defect."
                ),
                "factor_role": (
                    "Where this candidate sits relative to the earliest "
                    "confirmed defect introduction."
                ),
                "necessary_downstream_materialization": (
                    "Allowed only when one confirmed_root_summaries path "
                    "contains candidate_ref after its confirmed root and "
                    "the remaining suffix exactly equals exact_recursive_path."
                ),
            },
            "factor_mechanism_contract": {
                "selection": (
                    "exact factor_mechanism from selected "
                    "role_contract row"
                ),
                "exact_object_schema": {
                    "schema": "factor-role-mechanism/v1",
                    "mechanism_type": (
                        "exact mechanism_type from selected "
                        "role_contract row"
                    ),
                    "source_ref": request.candidate_ref,
                    "target_ref": (
                        "one exact ref from "
                        "allowed_mechanism_target_refs"
                    ),
                    "effect": "non-empty grounded effect",
                },
            },
        }
    )


def parse_factor_role_judgment(
    value: Any, *, request: FactorRoleRequest
) -> FactorRoleJudgment:
    if type(value) is str:
        value = _parse_single_json_object(value)
    if not isinstance(value, Mapping):
        raise TypeError("factor role judgment must be one JSON object")
    expected_keys = {
        "candidate_ref",
        "necessity_status",
        "factor_role",
        "reason",
        "confidence",
        "evidence_refs",
        "recursive_path",
        "factor_mechanism",
        "counterfactual",
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "defect_fingerprint",
        "seed_binding_identity",
        "analysis_perspective",
        "request_identity",
    }
    if {str(key) for key in value} != expected_keys:
        raise ValueError("factor role judgment has an inexact schema")
    exact_bindings = {
        "candidate_ref": request.candidate_ref,
        "hypothesis_id": request.hypothesis_id,
        "hypothesis_semantic_hash": request.hypothesis_semantic_hash,
        "defect_fingerprint": request.defect_state.fingerprint,
        "seed_binding_identity": request.seed_binding_identity,
        "analysis_perspective": request.analysis_perspective,
        "request_identity": factor_role_request_identity(request),
    }
    for field_name, expected in exact_bindings.items():
        if value[field_name] != expected:
            raise ValueError("{0} must match request".format(field_name))
    if factor_role_contract_entry(
        value["necessity_status"],
        value["factor_role"],
    ) is None:
        raise ValueError("factor role pair is absent from role contract")
    if (
        value["necessity_status"] == "necessary"
        and value["factor_role"] == "downstream_materialization"
        and not any(
            tuple(summary["recursive_path"])[index:]
            == request.recursive_path
            for summary in request.confirmed_root_summaries
            for index, ref in enumerate(summary["recursive_path"])
            if index > 0 and ref == request.candidate_ref
        )
    ):
        raise ValueError(
            "necessary downstream materialization requires a confirmed "
            "upstream root on the same recursive path"
        )

    evidence_refs = _factor_role_reference_strings(
        value["evidence_refs"], "evidence_refs"
    )
    allowed_refs = factor_role_request_fact_refs(request)
    outside_refs = tuple(ref for ref in evidence_refs if ref not in allowed_refs)
    if outside_refs:
        raise ValueError(
            "evidence_refs cite refs outside request: {0}".format(
                ", ".join(outside_refs)
            )
        )
    recursive_path = _factor_role_reference_strings(
        value["recursive_path"], "recursive_path"
    )
    if recursive_path != request.recursive_path:
        raise ValueError("recursive_path must match request")

    mechanism = value["factor_mechanism"]
    if not isinstance(mechanism, Mapping):
        raise ValueError("factor_mechanism must be an object")
    if mechanism:
        source_ref = mechanism.get("source_ref")
        target_ref = mechanism.get("target_ref")
        if source_ref not in allowed_refs:
            raise ValueError("factor_mechanism source_ref is outside request")
        if source_ref != request.candidate_ref:
            raise ValueError("factor_mechanism source_ref must match candidate_ref")
        if target_ref not in allowed_refs:
            raise ValueError("factor_mechanism target_ref is outside request")
        if target_ref not in request.recursive_path[1:]:
            raise ValueError(
                "factor_mechanism target_ref must be a downstream recursive path ref"
            )

    return FactorRoleJudgment(
        candidate_ref=value["candidate_ref"],
        necessity_status=value["necessity_status"],
        factor_role=value["factor_role"],
        reason=value["reason"],
        confidence=_number(value["confidence"], "confidence"),
        evidence_refs=evidence_refs,
        recursive_path=recursive_path,
        factor_mechanism=mechanism,
        counterfactual=value["counterfactual"],
        hypothesis_id=value["hypothesis_id"],
        hypothesis_semantic_hash=value["hypothesis_semantic_hash"],
        defect_fingerprint=value["defect_fingerprint"],
        seed_binding_identity=value["seed_binding_identity"],
        analysis_perspective=value["analysis_perspective"],
        request_identity=value["request_identity"],
    )


def _number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("{0} must be numeric".format(field_name))
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("{0} must be between 0 and 1".format(field_name))
    return result


def _strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("{0} must be a list of strings".format(field_name))
    return tuple(item for item in value if item)


def _factor_role_reference_strings(
    value: Any,
    field_name: str,
) -> Tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(type(item) is not str or not item.strip() for item in value)
    ):
        raise ValueError(
            "{0} must contain non-empty string references".format(field_name)
        )
    return tuple(value)


def _reference_sets(value: Any) -> Tuple[Set[str], Set[str]]:
    grounded: Set[str] = set()
    unresolved: Set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            status = str(item.get("resolution_status") or "").strip().lower()
            if status:
                target = grounded if status == "resolved" else unresolved
                for key in ("ref", "raw_ref", "resolved_ref"):
                    ref = str(item.get(key) or "").strip()
                    if ref:
                        target.add(ref)
            for child in item.values():
                visit(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return grounded, unresolved


def _candidate_grounding(request: CausalStepRequest) -> Tuple[Set[str], Set[str], Set[str]]:
    offered = {item.ref for item in request.candidates}
    grounded, unresolved = _reference_sets(request.recursive_context)
    grounded.add(request.current_node.ref)
    candidate_facts = request.recursive_context.get("candidate_predecessors", ())
    grounded_candidates: Set[str] = set()
    if isinstance(candidate_facts, (list, tuple)):
        for fact in candidate_facts:
            if not isinstance(fact, Mapping):
                continue
            ref = str(fact.get("ref") or "")
            reference = fact.get("reference")
            if (
                ref in offered
                and isinstance(reference, Mapping)
                and str(reference.get("resolution_status") or "") == "resolved"
                and str(reference.get("resolved_ref") or "") == ref
            ):
                grounded_candidates.add(ref)
    return offered, grounded_candidates, grounded - unresolved


def _validate_evidence_refs(value: Any, *, grounded_refs: Set[str], field_name: str) -> Tuple[str, ...]:
    refs = _strings(value, field_name)
    invalid = [ref for ref in refs if ref not in grounded_refs]
    if invalid:
        raise ValueError(
            "{0} must cite grounded evidence refs; invalid: {1}".format(field_name, ", ".join(invalid))
        )
    return refs


def _task_obligation_reference_set(
    recursive_context: Mapping[str, Any],
) -> Set[str]:
    refs: Set[str] = set()
    obligations = recursive_context.get("task_obligations")
    if not isinstance(obligations, (list, tuple)):
        return refs
    for obligation in obligations:
        if not isinstance(obligation, Mapping):
            continue
        for key in ("obligation_id", "ref", "source"):
            ref = str(obligation.get(key) or "").strip()
            if ref:
                refs.add(ref)
    return refs


def _validate_process_assessment(
    value: Any,
    *,
    request: CausalStepRequest,
    status: str,
    introduction: bool,
    grounded_refs: Set[str],
) -> JsonDict:
    is_process_candidate = request.defect_state.label == "candidate_local_process_defect"
    if not is_process_candidate:
        if value not in (None, {}):
            raise ValueError(
                "process_assessment must be null outside candidate-local process defect judgment"
            )
        return {}
    if not isinstance(value, dict):
        raise ValueError(
            "candidate-local process defect requires process_assessment"
        )
    expected_fields = {
        "candidate_role",
        "commitment_status",
        "trajectory_relation",
        "failure_mode",
        "commitment_cue_disposition",
        "commitment_cue_reason",
        "obligation_refs",
        "reason",
        "evidence_refs",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "process_assessment requires exact fields: {0}".format(
                ", ".join(sorted(expected_fields))
            )
        )
    candidate_role = str(value.get("candidate_role") or "").strip().lower()
    if candidate_role not in PROCESS_CANDIDATE_ROLES:
        raise ValueError("unsupported process candidate_role")
    commitment_status = str(value.get("commitment_status") or "").strip().lower()
    if commitment_status not in PROCESS_COMMITMENT_STATUSES:
        raise ValueError("unsupported process commitment_status")
    trajectory_relation = str(value.get("trajectory_relation") or "").strip().lower()
    if trajectory_relation not in PROCESS_TRAJECTORY_RELATIONS:
        raise ValueError("unsupported process trajectory_relation")
    failure_mode = str(value.get("failure_mode") or "").strip().lower()
    if failure_mode not in FAILURE_MODES:
        raise ValueError("unsupported process failure_mode")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("process_assessment reason must be non-empty")
    obligation_refs = _strings(
        value.get("obligation_refs"), "process_assessment obligation_refs"
    )
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs"),
        grounded_refs=grounded_refs,
        field_name="process_assessment evidence_refs",
    )
    cue_disposition = str(
        value.get("commitment_cue_disposition") or ""
    ).strip().lower()
    if cue_disposition not in PROCESS_COMMITMENT_CUE_DISPOSITIONS:
        raise ValueError("unsupported process commitment_cue_disposition")
    cue_reason = str(value.get("commitment_cue_reason") or "").strip()
    if not cue_reason:
        raise ValueError(
            "process_assessment commitment_cue_reason must be non-empty"
        )
    cue_context = request.recursive_context.get(
        "candidate_commitment_cues"
    )
    has_recorded_cue = bool(
        isinstance(cue_context, Mapping)
        and int(cue_context.get("cue_count") or 0) > 0
    )
    if has_recorded_cue and cue_disposition == "not_applicable":
        raise ValueError(
            "recorded commitment cue requires an explicit disposition"
        )
    if has_recorded_cue and request.current_node.ref not in evidence_refs:
        raise ValueError(
            "commitment cue disposition requires candidate-local evidence"
        )
    if cue_disposition == "commitment":
        if candidate_role != "implementation_commitment":
            raise ValueError(
                "commitment cue requires candidate_role=implementation_commitment"
            )
        if commitment_status == "not_applicable":
            raise ValueError(
                "commitment cue requires an applicable commitment status"
            )
    if (
        cue_disposition == "non_commitment"
        and commitment_status != "not_applicable"
    ):
        raise ValueError(
            "non-commitment cue requires commitment_status=not_applicable"
        )
    trajectory = request.recursive_context.get("candidate_process_trajectory")
    delivery_observed = bool(
        isinstance(trajectory, Mapping)
        and trajectory.get("post_candidate_delivery_observed") is True
        and (
            int(trajectory.get("post_candidate_mutation_count") or 0) > 0
            or int(trajectory.get("post_candidate_verification_count") or 0)
            > 0
        )
    )
    if commitment_status == "fulfilled" and not delivery_observed:
        raise ValueError(
            "fulfilled commitment requires observed delivery"
        )
    if trajectory_relation == "materialized" and not delivery_observed:
        raise ValueError(
            "materialized trajectory requires observed delivery"
        )
    if status == "absent" and failure_mode in {
        "positive_introduction",
        "responsible_omission",
        "omission_enabling_condition",
    }:
        raise ValueError(
            "an absent process defect cannot carry a positive or responsible failure mode"
        )
    if failure_mode in {
        "responsible_omission",
        "omission_enabling_condition",
    }:
        allowed_obligation_refs = _task_obligation_reference_set(
            request.recursive_context
        )
        if not obligation_refs or any(
            ref not in allowed_obligation_refs for ref in obligation_refs
        ):
            raise ValueError(
                "responsible omission requires exact supplied task obligation refs"
            )
    if commitment_status == "unfulfilled":
        if status != "present":
            raise ValueError(
                "unfulfilled commitment requires a present process defect"
            )
        if candidate_role != "implementation_commitment":
            raise ValueError(
                "unfulfilled commitment requires candidate_role=implementation_commitment"
            )
        if trajectory_relation not in {"diverged", "remained_investigation"}:
            raise ValueError(
                "unfulfilled commitment requires a divergent post-candidate trajectory"
            )
        if failure_mode not in {
            "responsible_omission",
            "omission_enabling_condition",
        }:
            raise ValueError(
                "unfulfilled commitment requires a responsible omission failure mode"
            )
        trajectory_grounded, trajectory_unresolved = _reference_sets(trajectory)
        trajectory_refs = trajectory_grounded - trajectory_unresolved
        if request.current_node.ref not in evidence_refs or not any(
            ref != request.current_node.ref and ref in trajectory_refs
            for ref in evidence_refs
        ):
            raise ValueError(
                "unfulfilled commitment requires grounded candidate and trajectory evidence"
            )
    if introduction and failure_mode not in {
        "positive_introduction",
        "responsible_omission",
    }:
        raise ValueError(
            "candidate introduction requires positive_introduction or responsible_omission"
        )
    return {
        "candidate_role": candidate_role,
        "commitment_status": commitment_status,
        "trajectory_relation": trajectory_relation,
        "failure_mode": failure_mode,
        "commitment_cue_disposition": cue_disposition,
        "commitment_cue_reason": cue_reason,
        "obligation_refs": list(obligation_refs),
        "reason": reason,
        "evidence_refs": list(evidence_refs),
    }


def _canonicalize_causal_step_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> Dict[str, Any]:
    """Resolve mechanically contradictory control fields without adding a verdict."""
    if not isinstance(value, dict):
        return value
    normalized = copy.deepcopy(value)
    predecessors = normalized.get("predecessors")
    if isinstance(predecessors, list):
        normalized["predecessors"] = [
            predecessor
            for predecessor in predecessors
            if not (
                isinstance(predecessor, Mapping)
                and str(predecessor.get("ref") or "").strip()
                == request.current_node.ref
            )
        ]
    else:
        return normalized
    status = str(normalized.get("current_defect_status") or "").strip().lower()
    current_data = request.current_node.data
    current_rationale = current_data.get("rationale")
    recent_reasoning = (
        str(current_rationale.get("recent_reasoning") or "").strip()
        if isinstance(current_rationale, Mapping)
        else ""
    )
    current_metadata = current_data.get("metadata")
    current_message_id = (
        str(
            current_metadata.get("messageID")
            or current_metadata.get("message_id")
            or ""
        )
        if isinstance(current_metadata, Mapping)
        else ""
    )
    same_generation_reasoning_refs: Set[str] = set()
    if (
        status == "present"
        and str(current_data.get("decision_type") or "") == "llm_tool_call"
        and recent_reasoning
        and current_message_id
    ):
        normalized_reasoning = " ".join(recent_reasoning.split())
        for candidate in request.candidates:
            candidate_data = candidate.node.data
            candidate_metadata = candidate_data.get("metadata")
            candidate_message_id = (
                str(
                    candidate_metadata.get("messageID")
                    or candidate_metadata.get("message_id")
                    or ""
                )
                if isinstance(candidate_metadata, Mapping)
                else ""
            )
            candidate_rationale = candidate_data.get("rationale")
            if (
                str(candidate_data.get("decision_type") or "")
                == "reasoning_block"
                and candidate_message_id == current_message_id
                and isinstance(candidate_rationale, str)
                and " ".join(candidate_rationale.split()) == normalized_reasoning
            ):
                same_generation_reasoning_refs.add(candidate.ref)
    for predecessor in normalized["predecessors"]:
        if not isinstance(predecessor, dict):
            continue
        predecessor_ref = str(predecessor.get("ref") or "").strip()
        if predecessor_ref in same_generation_reasoning_refs:
            predecessor["relation"] = "same_defect_propagation"
            predecessor["recurse"] = True
            predecessor["upstream_defect"] = None
            predecessor["reason"] = (
                "The current tool-action decision repeats this earlier same-message "
                "reasoning verbatim, so the defect-bearing semantic commitment "
                "predates its action projection."
            )
            evidence_refs = predecessor.get("evidence_refs")
            if isinstance(evidence_refs, list) and predecessor_ref not in evidence_refs:
                evidence_refs.append(predecessor_ref)
        relation = str(predecessor.get("relation") or "").strip().lower()
        if relation in {"same_defect_propagation", "defect_transformation"}:
            if status == "present":
                predecessor["recurse"] = True
            else:
                predecessor["relation"] = "unknown"
                predecessor["recurse"] = False
                predecessor["upstream_defect"] = None
        elif (
            relation == "contributing_condition"
            and predecessor.get("recurse") is True
            and status != "present"
        ):
            predecessor["recurse"] = False
            predecessor["upstream_defect"] = None
    recursive_relations = {
        "same_defect_propagation",
        "defect_transformation",
        "contributing_condition",
    }
    has_recursive_predecessor = any(
        isinstance(predecessor, Mapping)
        and predecessor.get("recurse") is True
        and str(predecessor.get("relation") or "").strip().lower()
        in recursive_relations
        for predecessor in normalized["predecessors"]
    )
    if normalized.get("candidate_introduction") is True and has_recursive_predecessor:
        normalized["candidate_introduction"] = False
    investigation = normalized.get("suggested_investigation")
    if (
        normalized.get("candidate_introduction") is not True
        and isinstance(investigation, Mapping)
        and investigation.get("action") == "request_root_confirmation"
    ):
        normalized["suggested_investigation"] = None
    return normalized


def validate_causal_step_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> CausalStepJudgment:
    if not isinstance(value, dict):
        raise TypeError("causal step payload must be an object")
    if str(value.get("current_node_ref") or "") != request.current_node.ref:
        raise ValueError("current_node_ref must match the offered current node")
    status = str(value.get("current_defect_status") or "").strip().lower()
    if status not in {"present", "absent", "unknown"}:
        raise ValueError("current_defect_status must be present, absent, or unknown")
    reason = str(value.get("current_defect_reason") or "").strip()
    if not reason:
        raise ValueError("current_defect_reason must be non-empty")
    normalized_reason = " ".join(reason.casefold().split())
    if status == "present" and (
        re.search(
            r"\b(?:current node|this node|candidate|node)\b.{0,32}\b(?:is|was) not (?:itself )?(?:defective|a defect|the defect)\b",
            normalized_reason,
        )
        or re.search(
            r"\b(?:current node|this node|candidate|node)\b.{0,24}\bdoes not (?:contain|introduce|carry) (?:the |a )?defect\b",
            normalized_reason,
        )
        or re.search(
            r"\b(?:decision|tool call|claim|episode|result)(?: node)? itself\b.{0,24}\bdoes not (?:contain|introduce|carry) (?:the |a )?defect\b",
            normalized_reason,
        )
    ):
        raise ValueError(
            "current_defect_status=present contradicts current_defect_reason"
        )
    if status == "absent" and re.search(
        r"\b(?:reasoning block|decision|plan|assumption)\b.{0,160}"
        r"\b(?:insufficient|incorrect|wrong|defective|does not account for|fails? to account for|omits?)\b",
        normalized_reason,
    ):
        raise ValueError(
            "current_defect_status=absent contradicts current_defect_reason"
        )
    predecessors = value.get("predecessors")
    if not isinstance(predecessors, list):
        raise ValueError("predecessors must be a list")
    offered, grounded_candidates, grounded_refs = _candidate_grounding(request)
    assessments = []
    seen = set()
    for index, raw in enumerate(predecessors):
        if not isinstance(raw, dict):
            raise ValueError("predecessors[{0}] must be an object".format(index))
        ref = str(raw.get("ref") or "").strip()
        if ref not in offered:
            raise ValueError("predecessor ref {0} is not an offered candidate predecessor".format(ref))
        if ref not in grounded_candidates:
            raise ValueError("candidate predecessor {0} is not grounded as resolved".format(ref))
        if ref in seen:
            raise ValueError("candidate predecessor {0} is duplicated".format(ref))
        seen.add(ref)
        relation = str(raw.get("relation") or "").strip().lower()
        if relation not in CAUSAL_RELATIONS:
            raise ValueError("relation must be one of the allowed causal relation values")
        if relation == "introduction_candidate":
            raise ValueError(
                "predecessor introduction_candidate is invalid; it is a current-node verdict"
            )
        predecessor_reason = str(raw.get("reason") or "").strip()
        if not predecessor_reason:
            raise ValueError("predecessor reason must be non-empty")
        if not isinstance(raw.get("recurse"), bool):
            raise ValueError("predecessor recurse must be boolean")
        recurse = raw["recurse"]
        candidate_node = next(
            (candidate.node for candidate in request.candidates if candidate.ref == ref),
            None,
        )
        if (
            recurse
            and candidate_node is not None
            and candidate_node.event_type in INPUT_PROVENANCE_EVENT_TYPES
            and not reason_identifies_candidate_local_defect(predecessor_reason)
        ):
            raise ValueError(
                "an input provenance envelope can recurse only when its own content contains the defect"
            )
        if recurse and relation not in {
            "same_defect_propagation",
            "defect_transformation",
            "contributing_condition",
        }:
            raise ValueError(
                "recurse=true is allowed only for propagation, transformation, or a material contributing condition"
            )
        carries_defect = relation in {"same_defect_propagation", "defect_transformation"}
        recursively_influences = relation == "contributing_condition" and recurse
        if (
            (carries_defect or recursively_influences)
            and status != "present"
            and not is_navigation_node(request.current_node)
        ):
            raise ValueError(
                "recursive propagation, transformation, and material contribution require current_defect_status=present"
            )
        if carries_defect and not recurse:
            raise ValueError("propagation and transformation require recurse=true")
        evidence_refs = _validate_evidence_refs(
            raw.get("evidence_refs", []),
            grounded_refs=grounded_refs,
            field_name="predecessor evidence_refs",
        )
        if (carries_defect or recursively_influences) and not evidence_refs:
            raise ValueError(
                "recursive propagation, transformation, and material contribution require grounded direct evidence"
            )
        upstream_defect = None
        raw_upstream = raw.get("upstream_defect")
        if relation == "defect_transformation" or recursively_influences:
            if not isinstance(raw_upstream, dict):
                raise ValueError(
                    "defect_transformation and recursive contributing_condition require an upstream_defect object"
                )
            label = str(raw_upstream.get("label") or "").strip()
            mechanism = str(raw_upstream.get("mechanism") or "").strip()
            transformation_reason = str(raw_upstream.get("transformation_reason") or "").strip()
            if not label or not mechanism or not transformation_reason:
                raise ValueError(
                    "transformation upstream_defect requires label, mechanism, and transformation_reason"
                )
            supplied_parent = str(raw_upstream.get("derived_from_defect_state_id") or "")
            if supplied_parent and supplied_parent != request.defect_state.defect_state_id:
                raise ValueError("transformation derived_from_defect_state_id does not match active defect")
            upstream_defect = request.defect_state.transformed(
                label=label,
                expected=str(raw_upstream.get("expected") or request.defect_state.expected),
                actual=str(raw_upstream.get("actual") or request.defect_state.actual),
                mechanism=mechanism,
                scope=str(raw_upstream.get("scope") or request.defect_state.scope),
                transformation_reason=transformation_reason,
            )
        elif raw_upstream not in (None, {}):
            raise ValueError(
                "upstream_defect is valid only for defect_transformation or a recursive contributing_condition"
            )
        predecessor_confidence = _number(
            raw.get("confidence"), "predecessor confidence"
        )
        if relation != "unknown" and predecessor_confidence <= 0.0:
            raise ValueError(
                "predecessors[{0}].confidence for {1} with relation={2} must be greater than 0".format(
                    index, ref, relation
                )
            )
        assessments.append(
            PredecessorAssessment(
                ref=ref,
                relation=relation,
                reason=predecessor_reason,
                confidence=predecessor_confidence,
                recurse=recurse,
                upstream_defect=upstream_defect,
                evidence_refs=evidence_refs,
                missing_evidence=_strings(
                    raw.get("missing_evidence", []), "predecessor missing_evidence"
                ),
            )
        )
    unselected = tuple(sorted(offered - seen))
    if sum(bool(item.recurse) for item in assessments) > 2:
        raise ValueError("a causal step may select at most two recursive predecessors")
    if not isinstance(value.get("candidate_introduction"), bool):
        raise ValueError("candidate_introduction must be boolean")
    introduction = value["candidate_introduction"]
    if introduction and status != "present":
        raise ValueError("candidate introduction requires current_defect_status=present")
    if introduction and not root_candidate_eligible(request.current_node):
        raise ValueError("the current navigation or outcome node is not eligible as a root candidate")
    if introduction and any(
        item.relation in {"same_defect_propagation", "defect_transformation"}
        or (item.relation == "contributing_condition" and item.recurse)
        for item in assessments
    ):
        raise ValueError("candidate introduction requires no viable defective predecessor")
    if introduction and unselected:
        raise ValueError(
            "candidate introduction requires assessing every offered predecessor"
        )
    process_assessment = _validate_process_assessment(
        value.get("process_assessment"),
        request=request,
        status=status,
        introduction=introduction,
        grounded_refs=grounded_refs,
    )
    if (
        status != "present"
        and any(item.recurse for item in assessments)
        and not is_navigation_node(request.current_node)
    ):
        raise ValueError("a non-present current defect cannot recurse")
    investigation = value.get("suggested_investigation")
    if investigation is not None and not isinstance(investigation, dict):
        raise ValueError("suggested_investigation must be an object or null")
    missing_evidence = _strings(value.get("missing_evidence", []), "missing_evidence")
    if is_navigation_node(request.current_node):
        recursive_predecessors = [item for item in assessments if item.recurse]
        if any(
            item.relation == "contributing_condition" and not item.recurse
            for item in assessments
        ):
            raise ValueError(
                "navigation contributing_condition requires recurse=true and an upstream_defect"
            )
        if len(recursive_predecessors) > 2:
            raise ValueError(
                "a navigation aggregate may select at most two recursive predecessors"
            )
        if status in {"present", "absent"} and not recursive_predecessors and not missing_evidence:
            raise ValueError(
                "a navigation aggregate cannot close the active branch without a recursive predecessor or concrete missing_evidence"
            )
        if status == "unknown" and unselected:
            raise ValueError(
                "an unknown navigation judgment requires assessing every offered predecessor"
            )
        if status == "unknown" and not missing_evidence:
            raise ValueError(
                "an unknown navigation judgment requires concrete missing_evidence"
            )
    if (
        status == "present"
        and not introduction
        and not any(item.recurse for item in assessments)
        and not missing_evidence
    ):
        raise ValueError(
            "a present defect cannot terminate silently without recursion, introduction, or missing_evidence"
        )
    if (
        isinstance(investigation, dict)
        and investigation.get("action") == "request_root_confirmation"
        and not introduction
    ):
        raise ValueError(
            "request_root_confirmation requires candidate_introduction=true"
        )
    active_hypothesis_id = str(request.recursive_context.get("active_hypothesis_id") or "")
    if introduction and not missing_evidence and active_hypothesis_id:
        if not isinstance(investigation, dict) or investigation.get("action") != "request_root_confirmation":
            raise ValueError(
                "candidate introduction requires request_root_confirmation"
            )
        arguments = investigation.get("arguments")
        expected_arguments = {
            "hypothesis_id": active_hypothesis_id,
            "candidate_ref": request.current_node.ref,
            "defect_fingerprint": request.defect_state.fingerprint,
        }
        if not isinstance(arguments, dict) or set(arguments) != set(expected_arguments):
            raise ValueError(
                "request_root_confirmation arguments require exact keys: "
                "hypothesis_id, candidate_ref, defect_fingerprint"
            )
        if arguments != expected_arguments:
            raise ValueError(
                "request_root_confirmation arguments must match the active hypothesis, current node, and defect fingerprint"
            )
    confidence = _number(value.get("confidence"), "confidence")
    if status != "unknown" and confidence <= 0.0:
        raise ValueError("non-unknown status requires positive confidence")
    return CausalStepJudgment(
        current_node_ref=request.current_node.ref,
        current_defect_status=status,
        current_defect_reason=reason,
        predecessors=tuple(assessments),
        candidate_introduction=introduction,
        missing_evidence=missing_evidence,
        suggested_investigation=dict(investigation) if investigation is not None else None,
        unselected_predecessor_refs=unselected,
        confidence=confidence,
        process_assessment=process_assessment,
    )


def causal_step_from_payload(
    value: Dict[str, Any], *, request: CausalStepRequest
) -> CausalStepJudgment:
    return validate_causal_step_payload(value, request=request)


def reason_identifies_candidate_local_defect(reason: str) -> bool:
    normalized = " ".join(reason.lower().split())
    explicit_phrases = (
        "itself contains",
        "itself carries",
        "already contains",
        "already carries",
        "contains the same defect",
        "carries the same defect",
    )
    if any(phrase in normalized for phrase in explicit_phrases):
        return True
    subject = r"(?:prompt|requirement|message|context|llm call|task loop)"
    defect = r"(?:incorrect|ambiguous|contradictory|defective|incomplete)"
    return bool(
        re.search(rf"\b{subject}\b.{{0,24}}\b(?:itself\s+)?(?:is|was)\s+{defect}\b", normalized)
        or re.search(
            rf"\b{subject}\b.{{0,24}}\b(?:omits|omitted|contradicts|misstates|misstated)\b",
            normalized,
        )
    )


_TASK2_MANIFEST_KEYS = {
    "node_ref",
    "referenced_artifact_ids",
    "hydrated_artifacts",
    "missing_artifact_ids",
    "truncated_artifact_ids",
    "ineligible_artifact_evidence",
}
_TASK2_ARTIFACT_STATUS_KEYS = {
    "raw_ref",
    "canonical_ref",
    "resolution_status",
    "availability",
    "hydration_status",
}
_BLOCKING_STATUS_VALUES = {
    "budget_exhausted",
    "circuit_open",
    "exhausted",
    "not_available",
    "not_hydrated",
    "not_requested",
    "unavailable",
    "unknown",
    "unresolved",
    "ambiguous",
    "missing",
    "truncated",
    "provider_error",
    "provider_unavailable",
}
_PROVENANCE_KEYS = {
    "edge_metadata",
    "evidence_type",
    "edge_origin",
    "edge_provenance",
    "evidence_metadata",
    "inference",
    "inference_method",
    "inference_metadata",
    "lineage",
    "method",
    "origin",
    "provenance",
    "provenance_class",
    "relation",
    "source",
}
_PROVENANCE_CONTEXT_FIELDS = {
    "evidence_type",
    "kind",
    "method",
    "origin",
    "relation",
    "type",
}
_SEMANTIC_TOKEN_ALIASES = {
    "availability": "available",
    "completeness": "complete",
    "failure": "failed",
    "grounding": "grounded",
    "hydration": "hydrated",
    "resolution": "resolved",
    "tripped": "open",
}
_STATUS_KEY_TOKENS = {
    "availability",
    "hydration",
    "outcome",
    "resolution",
    "result",
    "state",
    "status",
}
_CONTEXTUAL_FAILURE_STATES = {
    "error",
    "failed",
    "open",
    "timeout",
    "unavailable",
}
_SEMANTIC_TEXT_FIELDS = {
    "actual",
    "body",
    "content",
    "description",
    "excerpt",
    "expected",
    "mechanism",
    "rationale",
    "reason",
    "summary",
    "text",
    "title",
}


def _normalized_artifact_id(value: Any) -> str:
    return str(value or "").strip().removeprefix("artifact:")


def _nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (Mapping, list, tuple, set)):
        return bool(value)
    return bool(value)


def _is_provenance_key(key: str) -> bool:
    return (
        key in _PROVENANCE_KEYS
        or "provenance" in key
        or "inference" in key
        or "lineage" in key
        or "evidence_metadata" in key
        or "edge_metadata" in key
        or key.startswith("inference_")
        or key.endswith("_origin")
        or key.endswith("_method")
        or key.endswith("_source")
    )


def _key_tokens(key: str) -> Set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", key.lower()) if token}


def _semantic_tokens(key: str) -> Set[str]:
    tokens = _key_tokens(key)
    return tokens | {
        _SEMANTIC_TOKEN_ALIASES[token]
        for token in tokens
        if token in _SEMANTIC_TOKEN_ALIASES
    }


def _semantic_state(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return _SEMANTIC_TOKEN_ALIASES.get(normalized, normalized)


def _is_temporal_provenance_value(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized.startswith("temporal_") or "temporal" in normalized


def _is_provider_context_key(key: str) -> bool:
    tokens = _semantic_tokens(key)
    if tokens.intersection({"circuit", "judge", "provider", "transport"}):
        return True
    if {"request", "tool"}.issubset(tokens):
        return False
    return "request" in tokens


def _is_grounding_status_context_key(key: str) -> bool:
    return any(token in key for token in ("artifact", "evidence", "reference"))


def _is_missing_evidence_context_key(key: str) -> bool:
    return any(
        token in key for token in ("missing", "unresolved", "truncated", "gap")
    ) and any(
        token in key for token in ("artifact", "evidence", "reference")
    )


def _is_reference_key(key: str) -> bool:
    return (
        key in {
            "ref",
            "refs",
            "node_id",
            "node_ids",
            "edge_id",
            "edge_ids",
            "artifact_id",
            "artifact_ids",
            "referenced_artifact_ids",
            "missing_artifact_ids",
            "truncated_artifact_ids",
        }
        or key.endswith("_ref")
        or key.endswith("_refs")
    ) and key not in {"raw_ref", "resolved_ref"}


def _is_reference_container_key(key: str) -> bool:
    return (
        key in {"reference", "references"}
        or key.endswith("_reference")
        or key.endswith("_references")
    )


def _is_semantic_metadata_key(key: str) -> bool:
    return (
        key in {
            "artifact_status",
            "confidence",
            "decisive",
            "fact_kind",
            "hash",
            "inference_metadata",
            "kind",
            "label",
            "missing",
            "owner_reference",
            "path",
            "reference_kind",
            "status",
            "timestamp",
            "truncated",
        }
        or key in _ENVELOPE_KEYS
        or _is_provenance_key(key)
        or _is_reference_key(key)
        or _is_reference_container_key(key)
        or key.endswith("_status")
        or key.endswith("_budget_exhausted")
        or key.startswith("missing_")
        or key.startswith("unresolved_")
        or key.startswith("truncated_")
        or key.startswith("provider_")
    )


def _looks_like_artifact_fact(value: Mapping[str, Any], parent_key: str) -> bool:
    if parent_key == "artifact_status":
        return False
    return (
        "artifact_id" in value
        or str(value.get("reference_kind") or "").strip().lower() == "artifact"
        or parent_key in {"artifact", "artifacts", "hydrated_artifact", "hydrated_artifacts"}
    )


@dataclass(frozen=True)
class _ConfirmationFactTreeResult:
    grounded_refs: Set[str]
    candidate_semantic_fragments: Tuple[str, ...]


@dataclass(frozen=True)
class _FactTreeContext:
    in_provider_context: bool = False
    in_provenance_context: bool = False
    in_grounding_status_context: bool = False
    in_missing_evidence_context: bool = False
    artifact_envelope_ref: str = ""

    def descend(
        self,
        key: str,
        *,
        artifact_envelope_ref: Optional[str] = None,
    ) -> "_FactTreeContext":
        return _FactTreeContext(
            in_provider_context=(
                self.in_provider_context or _is_provider_context_key(key)
            ),
            in_provenance_context=(
                self.in_provenance_context or _is_provenance_key(key)
            ),
            in_grounding_status_context=(
                self.in_grounding_status_context
                or _is_grounding_status_context_key(key)
            ),
            in_missing_evidence_context=(
                self.in_missing_evidence_context
                or _is_missing_evidence_context_key(key)
            ),
            artifact_envelope_ref=(
                self.artifact_envelope_ref
                if artifact_envelope_ref is None
                else artifact_envelope_ref
            ),
        )


class _ConfirmationFactTreeValidator:
    """Validate every confirmation fact twice before exposing grounded semantics."""

    def __init__(self, request: RootConfirmationRequest) -> None:
        self.request = request
        self.errors: List[str] = []
        self.grounded_refs: Set[str] = set()
        self.resolved_envelopes: Dict[int, str] = {}
        self.validated_manifests: Set[int] = set()
        self.hydrated_artifact_nodes: Set[int] = set()
        self.hydrated_artifact_refs: Set[str] = set()
        self.hydrated_artifact_fragments: Dict[int, str] = {}
        self.pending_artifact_statuses: List[
            Tuple[Mapping[str, Any], str, str]
        ] = []
        self.validated_artifact_status_nodes: Set[int] = set()
        self.roots = (
            ("candidate_reference", request.candidate_reference),
            ("recursive_path_references", request.recursive_path_references),
            ("supporting_evidence", request.supporting_evidence),
            ("opposing_evidence", request.opposing_evidence),
            ("competing hypothesis", request.competing_hypotheses),
            ("task_obligations", request.task_obligations),
        ) + (
            (
                (
                    "process_factual_context",
                    request.process_factual_context,
                ),
            )
            if request.process_factual_context
            else ()
        )

    def validate(self) -> _ConfirmationFactTreeResult:
        for path, value in self.roots:
            self._pass_one(
                value,
                path=path,
                parent_key="",
                context=_FactTreeContext().descend(path),
            )
        self._validate_artifact_statuses()
        self._validate_required_roots()
        for path, value in self.roots:
            self._pass_two(
                value,
                path=path,
                parent_key="",
                candidate_local=False,
                context=_FactTreeContext().descend(path),
            )
        self._validate_competing_hypotheses()
        if self.errors:
            unique_errors = list(dict.fromkeys(self.errors))
            raise ValueError(
                "confirmation fact tree is ineligible: {0}".format(
                    "; ".join(unique_errors)
                )
            )
        fragments: List[str] = []
        self._collect_candidate_semantics(
            self.request.candidate_reference,
            fragments=fragments,
            parent_key="candidate_reference",
            candidate_local=False,
        )
        self._collect_candidate_semantics(
            self.request.supporting_evidence,
            fragments=fragments,
            parent_key="supporting_evidence",
            candidate_local=False,
        )
        normalized_fragments = tuple(
            dict.fromkeys(
                normalized
                for normalized in (
                    re.sub(r"\s+", " ", fragment).strip().lower()
                    for fragment in fragments
                )
                if normalized
            )
        )
        return _ConfirmationFactTreeResult(
            set(self.grounded_refs), normalized_fragments
        )

    def _error(self, path: str, message: str) -> None:
        self.errors.append("{0}: {1}".format(path, message))

    def _register_ref(self, value: Any) -> None:
        ref = str(value or "").strip()
        if not ref:
            return
        self.grounded_refs.add(ref)
        if ref.startswith("artifact:"):
            self.grounded_refs.add(ref.removeprefix("artifact:"))

    def _envelope_errors(self, value: Mapping[str, Any]) -> List[str]:
        return _reference_envelope_errors(value)

    def _inference_errors(self, value: Mapping[str, Any]) -> List[str]:
        return _reference_inference_errors(value)

    def _temporal_errors(
        self,
        value: Mapping[str, Any],
        *,
        in_provenance_context: bool,
    ) -> List[str]:
        errors: List[str] = []
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            provenance_value = _is_provenance_key(key) or (
                in_provenance_context and key in _PROVENANCE_CONTEXT_FIELDS
            )
            if not provenance_value or not isinstance(child, str):
                continue
            if _is_temporal_provenance_value(child):
                errors.append("temporal provenance field {0} is confirmation-ineligible".format(key))
        return errors

    def _pass_one(
        self,
        value: Any,
        *,
        path: str,
        parent_key: str,
        context: _FactTreeContext,
    ) -> None:
        if (
            isinstance(value, str)
            and context.in_provenance_context
            and _is_temporal_provenance_value(value)
        ):
            self._error(path, "temporal provenance value is confirmation-ineligible")
            return
        if isinstance(value, Mapping):
            artifact_status_shaped = parent_key == "artifact_status"
            envelope_shaped = (
                bool(_ENVELOPE_SHAPE_KEYS.intersection(value))
                and not artifact_status_shaped
            )
            provenance_errors: List[str] = []
            if "provenance_class" in value:
                provenance = str(value.get("provenance_class") or "").strip()
                if provenance not in _ALLOWED_PROVENANCE:
                    provenance_errors.append(
                        "provenance_class must be exactly recorded, reconstructed, or inferred",
                    )
                provenance_errors.extend(self._inference_errors(value))
            temporal_errors = self._temporal_errors(
                value, in_provenance_context=context.in_provenance_context
            )
            for error in provenance_errors + temporal_errors:
                self._error(path, error)
            current_artifact_ref = context.artifact_envelope_ref
            if envelope_shaped:
                envelope_errors = self._envelope_errors(value)
                for error in envelope_errors:
                    self._error(path, error)
                if (
                    not envelope_errors
                    and not provenance_errors
                    and not temporal_errors
                    and value.get("resolution_status") == "resolved"
                ):
                    resolved_ref = str(value.get("resolved_ref") or "").strip()
                    self.resolved_envelopes[id(value)] = resolved_ref
                    self._register_ref(value.get("raw_ref"))
                    self._register_ref(resolved_ref)
                    if resolved_ref.startswith("artifact:"):
                        current_artifact_ref = resolved_ref
            if parent_key == "artifact_hydration":
                self._register_hydration_manifest(value, path=path)
            if artifact_status_shaped:
                self.pending_artifact_statuses.append(
                    (value, path, context.artifact_envelope_ref)
                )
            child_context = context.descend(
                "", artifact_envelope_ref=current_artifact_ref
            )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                self._pass_one(
                    child,
                    path="{0}.{1}".format(path, raw_key),
                    parent_key=key,
                    context=child_context.descend(key),
                )
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                self._pass_one(
                    child,
                    path="{0}[{1}]".format(path, index),
                    parent_key=parent_key,
                    context=context,
                )

    def _artifact_id_list(self, value: Any, *, path: str) -> Optional[Set[str]]:
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(item, str) for item in value
        ):
            self._error(path, "must be a list of artifact ids")
            return None
        normalized = [_normalized_artifact_id(item) for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            self._error(path, "contains empty or duplicate artifact ids")
            return None
        return set(normalized)

    def _register_hydration_manifest(self, value: Mapping[str, Any], *, path: str) -> None:
        missing_keys = _TASK2_MANIFEST_KEYS - set(value)
        audit_keys = {
            "referenced_artifact_ids",
            "missing_artifact_ids",
            "truncated_artifact_ids",
            "ineligible_artifact_evidence",
        }
        judge_visible_manifest = missing_keys == audit_keys
        if missing_keys and not judge_visible_manifest:
            self._error(
                path,
                "Task 2 hydration manifest is missing {0}".format(
                    ", ".join(sorted(missing_keys))
                ),
            )
            return
        hydrated_value = value.get("hydrated_artifacts")
        if not isinstance(hydrated_value, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in hydrated_value
        ):
            self._error(path, "hydrated_artifacts must be a list of objects")
            return
        if judge_visible_manifest:
            referenced = {
                _normalized_artifact_id(item.get("artifact_id"))
                for item in hydrated_value
                if isinstance(item, Mapping)
            }
            missing: Optional[Set[str]] = set()
            truncated: Optional[Set[str]] = set()
        else:
            referenced = self._artifact_id_list(
                value.get("referenced_artifact_ids"),
                path="{0}.referenced_artifact_ids".format(path),
            )
            missing = self._artifact_id_list(
                value.get("missing_artifact_ids"),
                path="{0}.missing_artifact_ids".format(path),
            )
            truncated = self._artifact_id_list(
                value.get("truncated_artifact_ids"),
                path="{0}.truncated_artifact_ids".format(path),
            )
        if referenced is None or missing is None or truncated is None:
            return
        node_ref = str(value.get("node_ref") or "").strip()
        if not node_ref:
            self._error(path, "manifest owner identity is missing")
        hydrated: Set[str] = set()
        valid_items: List[Tuple[Mapping[str, Any], str, str]] = []
        for index, artifact in enumerate(hydrated_value):
            item_path = "{0}.hydrated_artifacts[{1}]".format(path, index)
            raw_artifact_id = artifact.get("artifact_id")
            artifact_id = _normalized_artifact_id(raw_artifact_id)
            if not isinstance(raw_artifact_id, str) or raw_artifact_id != artifact_id:
                self._error(item_path, "artifact_id must be a canonical unprefixed string")
                continue
            if not artifact_id or artifact_id in hydrated:
                self._error(item_path, "has an empty or duplicate artifact_id")
                continue
            hydrated.add(artifact_id)
            if artifact_id not in referenced:
                self._error(item_path, "is not listed in referenced_artifact_ids")
                continue
            if artifact.get("missing") is True:
                self._error(item_path, "artifact is missing")
                continue
            if bool(artifact.get("truncated")) != (artifact_id in truncated):
                self._error(item_path, "has contradictory truncation fields")
                continue
            if artifact_id in missing or artifact_id in truncated:
                self._error(item_path, "artifact is missing or truncated")
                continue
            content = artifact.get("content")
            if not isinstance(content, str):
                self._error(item_path, "content must be UTF-8 text")
                continue
            content_bytes = content.encode("utf-8")
            content_hash = artifact.get("content_hash")
            expected_hash = "sha256:{0}".format(hashlib.sha256(content_bytes).hexdigest())
            if not isinstance(content_hash, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", content_hash
            ):
                self._error(item_path, "content_hash must be canonical sha256")
                continue
            if content_hash != expected_hash:
                self._error(item_path, "content_hash does not match actual bytes")
                continue
            byte_count = artifact.get("byte_count")
            if (
                isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count != len(content_bytes)
            ):
                self._error(item_path, "byte_count must equal the actual UTF-8 byte count")
                continue
            byte_range = artifact.get("byte_range")
            if (
                not isinstance(byte_range, (list, tuple))
                or len(byte_range) != 2
                or any(isinstance(item, bool) or not isinstance(item, int) for item in byte_range)
            ):
                self._error(item_path, "byte_range must contain two integers")
                continue
            start, end = byte_range
            if start < 0 or end < start:
                self._error(item_path, "byte_range is out of bounds or reversed")
                continue
            if end <= len(content_bytes):
                try:
                    fragment = content_bytes[start:end].decode(
                        "utf-8",
                        errors="strict",
                    )
                except UnicodeDecodeError:
                    self._error(
                        item_path,
                        "byte_range must align to UTF-8 boundaries",
                    )
                    continue
            elif end - start == len(content_bytes):
                fragment = content
            else:
                self._error(item_path, "byte_range is out of bounds or reversed")
                continue
            owner = artifact.get("owner_reference")
            if not isinstance(owner, Mapping) or self._envelope_errors(owner):
                self._error(item_path, "owner_reference must be a resolved reference envelope")
                continue
            if (
                str(owner.get("resolution_status") or "") != "resolved"
                or str(owner.get("resolved_ref") or "") != node_ref
            ):
                self._error(item_path, "owner_reference contradicts manifest owner identity")
                continue
            if _ENVELOPE_SHAPE_KEYS.intersection(artifact) and self._envelope_errors(
                artifact
            ):
                continue
            valid_items.append((artifact, artifact_id, fragment))
        if not missing.issubset(referenced) or not truncated.issubset(referenced):
            self._error(path, "contains artifact ids absent from referenced_artifact_ids")
        if referenced != hydrated | missing:
            self._error(path, "does not account for every referenced artifact")
        if missing or truncated or len(valid_items) != len(hydrated_value):
            return
        self.validated_manifests.add(id(value))
        for artifact, artifact_id, fragment in valid_items:
            self.hydrated_artifact_nodes.add(id(artifact))
            self.hydrated_artifact_fragments[id(artifact)] = fragment
            self.hydrated_artifact_refs.add(artifact_id)
            self._register_ref(artifact_id)
            self._register_ref("artifact:{0}".format(artifact_id))

    def _validate_artifact_statuses(self) -> None:
        for value, path, parent_artifact_ref in self.pending_artifact_statuses:
            errors: List[str] = []
            if not parent_artifact_ref:
                errors.append(
                    "Task 2 artifact status requires a validated parent artifact envelope"
                )
            missing_keys = _TASK2_ARTIFACT_STATUS_KEYS - set(value)
            if missing_keys:
                errors.append(
                    "Task 2 artifact status is missing {0}".format(
                        ", ".join(sorted(missing_keys))
                    )
                )
            raw_ref = str(value.get("raw_ref") or "").strip()
            canonical_ref = str(value.get("canonical_ref") or "").strip()
            raw_id = _normalized_artifact_id(raw_ref)
            canonical_id = _normalized_artifact_id(canonical_ref)
            if not raw_id or not canonical_id:
                errors.append("Task 2 artifact status requires non-empty raw and canonical refs")
            if canonical_ref != "artifact:{0}".format(canonical_id):
                errors.append("Task 2 artifact status canonical_ref is not canonical")
            if raw_id != canonical_id:
                errors.append("Task 2 artifact status raw_ref contradicts canonical_ref")
            if canonical_ref != parent_artifact_ref:
                errors.append(
                    "Task 2 artifact status canonical_ref contradicts parent artifact envelope"
                )
            parent_artifact_id = _normalized_artifact_id(parent_artifact_ref)
            if raw_id != parent_artifact_id:
                errors.append(
                    "Task 2 artifact status raw_ref contradicts parent artifact envelope"
                )
            resolved_ref = str(value.get("resolved_ref") or "").strip()
            if resolved_ref and resolved_ref != canonical_ref:
                errors.append("Task 2 artifact status resolved_ref contradicts canonical_ref")
            artifact_id = _normalized_artifact_id(value.get("artifact_id"))
            if artifact_id and artifact_id != canonical_id:
                errors.append("Task 2 artifact status artifact_id contradicts canonical_ref")
            if artifact_id and artifact_id != parent_artifact_id:
                errors.append(
                    "Task 2 artifact status artifact_id contradicts parent artifact envelope"
                )
            if str(value.get("resolution_status") or "").strip().lower() != "resolved":
                errors.append("Task 2 artifact status is unresolved")
            if str(value.get("availability") or "").strip().lower() != "available":
                errors.append("Task 2 artifact status is not available")
            if str(value.get("hydration_status") or "").strip().lower() != "hydrated":
                errors.append("Task 2 artifact status is not hydrated")
            if canonical_id not in self.hydrated_artifact_refs:
                errors.append(
                    "Task 2 artifact status is absent from a validated hydration manifest"
                )
            provenance = value.get("provenance_class")
            if provenance is not None and str(provenance).strip() != "recorded":
                errors.append("Task 2 artifact status provenance must be recorded")
            errors.extend(
                self._temporal_errors(value, in_provenance_context=False)
            )
            errors.extend(
                self._blocking_errors(
                    value,
                    context=_FactTreeContext(
                        in_grounding_status_context=True,
                        artifact_envelope_ref=parent_artifact_ref,
                    ),
                )
            )
            for error in errors:
                self._error(path, error)
            if not errors:
                self.validated_artifact_status_nodes.add(id(value))

    def _validate_required_roots(self) -> None:
        candidate = self.resolved_envelopes.get(id(self.request.candidate_reference))
        if candidate != self.request.candidate_ref:
            self._error(
                "candidate_reference",
                "unresolved candidate requires a complete resolved candidate reference envelope",
            )
        if len(self.request.recursive_path_references) != len(self.request.recursive_path):
            self._error(
                "recursive_path_references",
                "every navigation path ref requires a path reference envelope",
            )
        for index, navigation_ref in enumerate(self.request.recursive_path):
            if index >= len(self.request.recursive_path_references):
                continue
            resolved = self.resolved_envelopes.get(
                id(self.request.recursive_path_references[index])
            )
            if resolved != navigation_ref:
                self._error(
                    "recursive_path_references[{0}]".format(index),
                    "path reference envelope is unresolved or ambiguous",
                )
        for label, facts in (
            ("supporting_evidence", self.request.supporting_evidence),
            ("opposing_evidence", self.request.opposing_evidence),
        ):
            for index, fact in enumerate(facts):
                if id(fact) not in self.resolved_envelopes:
                    if (
                        label == "opposing_evidence"
                        and str(fact.get("resolution_status") or "").lower()
                        != "resolved"
                    ):
                        self._error(
                            "{0}[{1}]".format(label, index),
                            "unresolved opposing evidence can invalidate root confirmation",
                        )
                    self._error(
                        "{0}[{1}]".format(label, index),
                        "fact requires a complete resolved reference envelope",
                    )

    def _blocking_errors(
        self,
        value: Mapping[str, Any],
        *,
        context: _FactTreeContext,
    ) -> List[str]:
        errors: List[str] = []
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            key_tokens = _semantic_tokens(key)
            provider_context = (
                context.in_provider_context or _is_provider_context_key(key)
            )
            grounding_status_context = (
                context.in_grounding_status_context
                or _is_grounding_status_context_key(key)
            )
            missing_evidence_context = (
                context.in_missing_evidence_context
                or _is_missing_evidence_context_key(key)
            )
            status_like = bool(key_tokens.intersection(_STATUS_KEY_TOKENS))
            if status_like and isinstance(child, str):
                status = child.strip().lower()
                semantic_status = _semantic_state(child)
                if (
                    status in _BLOCKING_STATUS_VALUES
                    or semantic_status in _BLOCKING_STATUS_VALUES
                ):
                    errors.append("blocking {0}={1}".format(key, status))
                if (
                    provider_context or missing_evidence_context
                ) and semantic_status in _CONTEXTUAL_FAILURE_STATES:
                    errors.append("blocking contextual {0}={1}".format(key, status))
            if (
                provider_context
                and key_tokens.intersection(_CONTEXTUAL_FAILURE_STATES)
                and _nonempty(child)
            ):
                errors.append("blocking declared provider/circuit failure {0}".format(key))
            if provider_context and "reason" in key_tokens and _nonempty(child):
                errors.append("blocking provider/circuit reason {0}".format(key))
            if isinstance(child, bool):
                true_blockers = {
                    "error",
                    "exhausted",
                    "missing",
                    "truncated",
                    "unresolved",
                }
                if provider_context:
                    true_blockers.update({"failed", "open", "unavailable"})
                if child and key_tokens.intersection(true_blockers):
                    errors.append("blocking {0}=true".format(key))
                if (
                    not child
                    and (
                        (
                            grounding_status_context
                            and key_tokens.intersection(
                                {
                                    "available",
                                    "complete",
                                    "grounded",
                                    "hydrated",
                                    "resolved",
                                }
                            )
                        )
                        or (
                            provider_context
                            and key_tokens.intersection(
                                {"available", "hydrated", "resolved"}
                            )
                        )
                    )
                ):
                    errors.append("blocking {0}=false".format(key))
            if (
                isinstance(child, (int, float))
                and not isinstance(child, bool)
                and math.isfinite(float(child))
                and child > 0
                and key_tokens.intersection(
                    {
                        "error",
                        "exhausted",
                        "failed",
                        "gap",
                        "missing",
                        "truncated",
                        "unresolved",
                    }
                )
            ):
                errors.append("blocking positive {0}".format(key))
            if not _nonempty(child):
                continue
            if key in {
                "missing_evidence",
                "unresolved_refs",
                "unresolved_references",
                "missing_artifact_ids",
                "truncated_artifact_ids",
                "provider_circuit_reason",
                "blocking_reasons",
                "evidence_gaps",
                "gaps",
                "missing",
                "truncated",
                "unresolved",
                "unavailable",
            }:
                errors.append("blocking {0}".format(key))
            elif (
                key.startswith("missing_")
                or (key.startswith("unresolved_") and key != "unresolved_questions")
                or key.startswith("truncated_")
            ) and isinstance(child, (Mapping, list, tuple, set, str)):
                errors.append("blocking {0}".format(key))
            elif key.endswith("_gaps") and isinstance(
                child, (Mapping, list, tuple, set, str)
            ):
                errors.append("blocking {0}".format(key))
        return errors

    def _reference_is_grounded(self, value: Any) -> bool:
        ref = str(value or "").strip()
        if not ref:
            return False
        if ref in self.grounded_refs:
            return True
        artifact_id = _normalized_artifact_id(ref)
        return artifact_id in self.hydrated_artifact_refs

    def _validate_reference_value(self, value: Any, *, path: str) -> None:
        if isinstance(value, str):
            if not self._reference_is_grounded(value):
                self._error(path, "bare or unregistered ref is unresolved: {0}".format(value))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                child_path = "{0}[{1}]".format(path, index)
                if isinstance(child, Mapping):
                    if not _ENVELOPE_SHAPE_KEYS.intersection(child):
                        self._error(child_path, "reference entry requires an envelope")
                elif not self._reference_is_grounded(child):
                    self._error(
                        child_path,
                        "bare or unregistered ref is unresolved: {0}".format(child),
                    )
            return
        if isinstance(value, Mapping):
            if not _ENVELOPE_SHAPE_KEYS.intersection(value):
                self._error(path, "reference value requires an envelope")
            return
        self._error(path, "reference value must be a grounded ref or envelope")

    def _pass_two(
        self,
        value: Any,
        *,
        path: str,
        parent_key: str,
        candidate_local: bool,
        context: _FactTreeContext,
    ) -> None:
        if (
            isinstance(value, str)
            and context.in_provenance_context
            and _is_temporal_provenance_value(value)
        ):
            self._error(path, "temporal provenance value is confirmation-ineligible")
            return
        if isinstance(value, Mapping):
            artifact_status_shaped = parent_key == "artifact_status"
            envelope_shaped = (
                bool(_ENVELOPE_SHAPE_KEYS.intersection(value))
                and not artifact_status_shaped
            )
            current_local = candidate_local
            current_artifact_ref = context.artifact_envelope_ref
            if envelope_shaped:
                for error in self._envelope_errors(value):
                    self._error(path, error)
                resolved = self.resolved_envelopes.get(id(value))
                if resolved:
                    current_local = resolved == self.request.candidate_ref
                    if resolved.startswith("artifact:"):
                        current_artifact_ref = resolved
            owner = value.get("owner_reference")
            if isinstance(owner, Mapping):
                owner_ref = self.resolved_envelopes.get(id(owner))
                if owner_ref == self.request.candidate_ref:
                    current_local = True
            for error in self._temporal_errors(
                value, in_provenance_context=context.in_provenance_context
            ):
                self._error(path, error)
            for error in self._blocking_errors(value, context=context):
                self._error(path, error)
            if (
                artifact_status_shaped
                and id(value) not in self.validated_artifact_status_nodes
            ):
                self._error(path, "artifact_status is not valid for its parent envelope")
            artifact_fact = _looks_like_artifact_fact(value, parent_key)
            hydrated_item = id(value) in self.hydrated_artifact_nodes
            if artifact_fact and not hydrated_item and not envelope_shaped:
                self._error(path, "artifact reference envelope is required")
            if artifact_fact and envelope_shaped:
                artifact_id = _normalized_artifact_id(value.get("artifact_id"))
                resolved = self.resolved_envelopes.get(id(value), "")
                if artifact_id and _normalized_artifact_id(resolved) != artifact_id:
                    self._error(path, "artifact_id contradicts resolved_ref")
                if candidate_local and isinstance(owner, Mapping):
                    owner_ref = self.resolved_envelopes.get(id(owner))
                    if owner_ref and owner_ref != self.request.candidate_ref:
                        self._error(path, "candidate artifact has a contradictory owner")
            child_context = context.descend(
                "", artifact_envelope_ref=current_artifact_ref
            )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                child_path = "{0}.{1}".format(path, raw_key)
                if _is_reference_key(key):
                    self._validate_reference_value(child, path=child_path)
                if _is_reference_container_key(key):
                    if isinstance(child, Mapping):
                        if not _ENVELOPE_SHAPE_KEYS.intersection(child):
                            self._error(child_path, "reference container requires an envelope")
                    elif isinstance(child, (list, tuple)):
                        if any(
                            not isinstance(item, Mapping)
                            or not _ENVELOPE_SHAPE_KEYS.intersection(item)
                            for item in child
                        ):
                            self._error(
                                child_path,
                                "reference collection requires complete envelopes",
                            )
                    else:
                        self._error(child_path, "bare reference requires an envelope")
                self._pass_two(
                    child,
                    path=child_path,
                    parent_key=key,
                    candidate_local=current_local,
                    context=child_context.descend(key),
                )
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                self._pass_two(
                    child,
                    path="{0}[{1}]".format(path, index),
                    parent_key=parent_key,
                    candidate_local=candidate_local,
                    context=context,
                )

    def _validate_competing_hypotheses(self) -> None:
        for index, hypothesis in enumerate(self.request.competing_hypotheses):
            path = "competing hypothesis[{0}]".format(index)
            status = str(hypothesis.get("status") or "").strip().lower()
            if status not in {"active", "supported", "unresolved", "rejected", "superseded"}:
                self._error(
                    path,
                    "has an invalid lifecycle status {0}".format(
                        status or "missing status"
                    ),
                )
            if status in {"active", "supported", "unresolved"}:
                required = {
                    "hypothesis_id",
                    "hypothesis_semantic_hash",
                    "claim",
                    "active_defect",
                    "candidate_reference",
                    "supporting_evidence",
                    "opposing_evidence",
                    "unresolved_questions",
                    "counterfactual",
                    "confirmation_identity",
                    "recursive_path",
                    "requires_independent_confirmation",
                }
                missing = required - set(hypothesis)
                if missing:
                    self._error(path, "missing auditable competitor fields {0}".format(
                        ", ".join(sorted(missing))
                    ))
                if not str(hypothesis.get("claim") or "").strip():
                    self._error(path, "competitor claim must be non-empty")
                counterfactual = hypothesis.get("counterfactual")
                if not isinstance(counterfactual, Mapping) or not str(
                    counterfactual.get("intervention_ref") or ""
                ).strip():
                    self._error(path, "competitor counterfactual must be structured")
                recursive_path = hypothesis.get("recursive_path")
                requires_confirmation = hypothesis.get(
                    "requires_independent_confirmation"
                )
                if (
                    not isinstance(recursive_path, (list, tuple))
                    or any(not isinstance(item, str) for item in recursive_path)
                ):
                    self._error(path, "competitor recursive_path must be a list of strings")
                if not isinstance(requires_confirmation, bool):
                    self._error(
                        path,
                        "competitor requires_independent_confirmation must be boolean",
                    )
                active_defect = hypothesis.get("active_defect")
                candidate_reference = hypothesis.get("candidate_reference")
                expected_identity = confirmation_identity_for(
                    hypothesis_id=str(hypothesis.get("hypothesis_id") or ""),
                    hypothesis_semantic_hash=str(
                        hypothesis.get("hypothesis_semantic_hash") or ""
                    ),
                    candidate_ref=(
                        str(candidate_reference.get("resolved_ref") or "")
                        if isinstance(candidate_reference, Mapping)
                        else ""
                    ),
                    defect_fingerprint=(
                        str(active_defect.get("fingerprint") or "")
                        if isinstance(active_defect, Mapping)
                        else ""
                    ),
                    recursive_path=(
                        tuple(recursive_path)
                        if isinstance(recursive_path, (list, tuple))
                        else ()
                    ),
                    seed_binding_identity=str(
                        hypothesis.get("seed_binding_identity") or ""
                    ),
                )
                if str(hypothesis.get("confirmation_identity") or "") != expected_identity:
                    self._error(path, "competitor confirmation identity is invalid")
                candidate = hypothesis.get("candidate_reference")
                if not isinstance(candidate, Mapping) or not str(candidate.get("content") or "").strip():
                    self._error(path, "competitor candidate semantics must be non-empty")
                for evidence_kind in ("supporting_evidence", "opposing_evidence"):
                    evidence_items = hypothesis.get(evidence_kind)
                    if not isinstance(evidence_items, (list, tuple)):
                        self._error(path, "{0} must be a list".format(evidence_kind))
                        continue
                    for item in evidence_items:
                        if not isinstance(item, Mapping) or not str(item.get("reason") or "").strip():
                            self._error(path, "{0} requires reasons".format(evidence_kind))

    def _collect_candidate_semantics(
        self,
        value: Any,
        *,
        fragments: List[str],
        parent_key: str,
        candidate_local: bool,
    ) -> None:
        if isinstance(value, Mapping):
            if id(value) in self.hydrated_artifact_fragments:
                if candidate_local:
                    fragments.append(self.hydrated_artifact_fragments[id(value)])
                return
            current_local = candidate_local
            resolved = self.resolved_envelopes.get(id(value))
            if resolved is not None:
                current_local = resolved == self.request.candidate_ref
            owner = value.get("owner_reference")
            if isinstance(owner, Mapping):
                current_local = (
                    self.resolved_envelopes.get(id(owner)) == self.request.candidate_ref
                )
            if id(value) in self.validated_manifests:
                current_local = (
                    str(value.get("node_ref") or "") == self.request.candidate_ref
                )
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower()
                if _is_semantic_metadata_key(key):
                    continue
                if current_local and isinstance(child, str) and (
                    key in _SEMANTIC_TEXT_FIELDS
                    or key.endswith("_content")
                    or key.endswith("_excerpt")
                    or key.endswith("_summary")
                    or key.endswith("_rationale")
                ):
                    fragments.append(child)
                self._collect_candidate_semantics(
                    child,
                    fragments=fragments,
                    parent_key=key,
                    candidate_local=current_local,
                )
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                self._collect_candidate_semantics(
                    child,
                    fragments=fragments,
                    parent_key=parent_key,
                    candidate_local=candidate_local,
                )


def _open_competitor_ids(request: RootConfirmationRequest) -> Tuple[str, ...]:
    return tuple(
        str(item.get("hypothesis_id") or "")
        for item in request.competing_hypotheses
        if str(item.get("status") or "").strip().lower()
        in {"active", "supported", "unresolved"}
    )


def _validate_competitor_comparisons(
    value: Any,
    *,
    request: RootConfirmationRequest,
    confirmation_status: str,
    grounded_refs: Set[str],
) -> Tuple[JsonDict, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("competitor_comparisons must be a list")
    open_ids = _open_competitor_ids(request)
    open_competitors = {
        str(item.get("hypothesis_id") or ""): item
        for item in request.competing_hypotheses
        if str(item.get("hypothesis_id") or "") in open_ids
    }
    comparisons: List[JsonDict] = []
    seen: Set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("competitor_comparisons entries must be objects")
        hypothesis_id = str(item.get("hypothesis_id") or "").strip()
        if not hypothesis_id or hypothesis_id in seen or hypothesis_id not in open_ids:
            raise ValueError("competitor_comparisons must bind each open competitor exactly once")
        seen.add(hypothesis_id)
        competitor = open_competitors[hypothesis_id]
        active_defect = competitor.get("active_defect")
        candidate_reference = competitor.get("candidate_reference")
        expected_identity = (
            str(competitor.get("hypothesis_semantic_hash") or ""),
            str(candidate_reference.get("resolved_ref") or "")
            if isinstance(candidate_reference, Mapping)
            else "",
            str(active_defect.get("fingerprint") or "")
            if isinstance(active_defect, Mapping)
            else "",
            str(competitor.get("confirmation_identity") or ""),
            tuple(str(value) for value in competitor.get("recursive_path") or ()),
            competitor.get("requires_independent_confirmation"),
        )
        actual_identity = (
            str(item.get("hypothesis_semantic_hash") or ""),
            str(item.get("candidate_ref") or ""),
            str(item.get("defect_fingerprint") or ""),
            str(item.get("confirmation_identity") or ""),
            tuple(str(value) for value in item.get("recursive_path") or ()),
            item.get("requires_independent_confirmation"),
        )
        if actual_identity != expected_identity:
            raise ValueError("competitor comparison identity does not match offered facts")
        status = str(item.get("status") or "").strip().lower()
        if status not in {"outperformed", "rejected", "co_root", "unresolved"}:
            raise ValueError("competitor comparison status is invalid")
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ValueError("competitor comparison reason must be non-empty")
        evidence_refs = _validate_evidence_refs(
            item.get("evidence_refs", []),
            grounded_refs=grounded_refs,
            field_name="competitor comparison evidence_refs",
        )
        if status != "unresolved" and not evidence_refs:
            raise ValueError("decisive competitor comparison requires grounded evidence refs")
        comparisons.append(
            {
                "hypothesis_id": hypothesis_id,
                "hypothesis_semantic_hash": actual_identity[0],
                "candidate_ref": actual_identity[1],
                "defect_fingerprint": actual_identity[2],
                "confirmation_identity": actual_identity[3],
                "recursive_path": list(actual_identity[4]),
                "requires_independent_confirmation": actual_identity[5],
                "status": status,
                "reason": reason,
                "evidence_refs": list(evidence_refs),
            }
        )
    if set(open_ids) != seen:
        raise ValueError("competitor_comparisons must cover every open competitor")
    if confirmation_status == "confirmed" and any(
        item["status"] == "unresolved" for item in comparisons
    ):
        raise ValueError("unresolved competitor blocks confirmed root")
    return tuple(comparisons)


def _validate_factor_mechanism(
    value: Any,
    *,
    role: str,
    request: RootConfirmationRequest,
    evidence_refs: Tuple[str, ...],
) -> JsonDict:
    if role not in {"contributing_condition", "amplifying_factor"}:
        if value not in (None, {}, FrozenMapping()):
            raise ValueError(
                "factor_mechanism must be null or empty when factor_role={0}".format(role)
            )
        return {}
    if not evidence_refs:
        raise ValueError("factor role requires grounded factor evidence")
    if not isinstance(value, Mapping):
        raise ValueError("factor role requires a structured factor_mechanism")
    expected_type = (
        "enabling_condition" if role == "contributing_condition" else "amplification"
    )
    mechanism_type = str(value.get("mechanism_type") or "").strip()
    source_ref = str(value.get("source_ref") or "").strip()
    target_ref = str(value.get("target_ref") or "").strip()
    effect = str(value.get("effect") or "").strip()
    if mechanism_type != expected_type:
        raise ValueError("factor mechanism_type contradicts factor role")
    if source_ref != request.candidate_ref:
        raise ValueError("factor mechanism source_ref must match candidate")
    if target_ref not in request.recursive_path:
        raise ValueError("factor mechanism target_ref must be grounded in recursive path")
    if source_ref not in evidence_refs or target_ref not in evidence_refs:
        raise ValueError("factor evidence must ground both mechanism source and target")
    if not effect:
        raise ValueError("factor mechanism effect must be non-empty")
    return {
        "mechanism_type": mechanism_type,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "effect": effect,
    }


def _validate_process_confirmation_assessment(
    value: Any,
    *,
    request: RootConfirmationRequest,
    confirmation_status: str,
    predicted_defect_status: str,
    grounded_refs: Set[str],
) -> JsonDict:
    if not request.process_factual_context:
        if value not in (None, {}, FrozenMapping()):
            raise ValueError(
                "process confirmation assessment is only valid with process facts"
            )
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            "process confirmation requires a structured counterfactual assessment"
        )
    expected_fields = {
        "candidate_role",
        "commitment_cue_disposition",
        "commitment_status",
        "trajectory_relation",
        "intervention_scope",
        "task_precondition_disposition",
        "active_process_defect_after_intervention",
        "downstream_failure_after_intervention",
        "reason",
        "evidence_refs",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "process confirmation assessment requires exact fields: {0}".format(
                ", ".join(sorted(expected_fields))
            )
        )

    def enum(name: str, allowed: Set[str]) -> str:
        item = str(value.get(name) or "").strip().lower()
        if item not in allowed:
            raise ValueError(
                "process confirmation assessment has unsupported {0}".format(
                    name
                )
            )
        return item

    candidate_role = enum(
        "candidate_role",
        {
            "implementation_commitment",
            "bounded_investigation",
            "priority_decision",
            "action_selection",
            "other",
            "unknown",
        },
    )
    cue_disposition = enum(
        "commitment_cue_disposition",
        {"commitment", "non_commitment", "ambiguous", "not_applicable"},
    )
    commitment_status = enum(
        "commitment_status",
        {"fulfilled", "unfulfilled", "not_applicable", "unknown"},
    )
    trajectory_relation = enum(
        "trajectory_relation",
        {"materialized", "diverged", "remained_investigation", "unknown"},
    )
    intervention_scope = enum(
        "intervention_scope",
        {"decision_and_committed_followup", "decision_only", "unknown"},
    )
    task_disposition = enum(
        "task_precondition_disposition",
        {"repair_obligation", "exculpatory_precondition", "ambiguous"},
    )
    active_after = enum(
        "active_process_defect_after_intervention",
        {"absent", "present", "unknown"},
    )
    downstream_after = enum(
        "downstream_failure_after_intervention",
        {"absent", "present", "unknown"},
    )
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError(
            "process confirmation assessment reason must be non-empty"
        )
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs"),
        grounded_refs=grounded_refs,
        field_name="process confirmation assessment evidence_refs",
    )
    process_grounded, process_unresolved = _reference_sets(
        request.process_factual_context
    )
    process_refs = process_grounded - process_unresolved
    if request.candidate_ref not in evidence_refs or not any(
        ref != request.candidate_ref and ref in process_refs
        for ref in evidence_refs
    ):
        raise ValueError(
            "process confirmation assessment requires candidate and trajectory evidence"
        )
    if cue_disposition == "commitment":
        if candidate_role != "implementation_commitment":
            raise ValueError(
                "process commitment cue requires implementation_commitment role"
            )
        if commitment_status == "not_applicable":
            raise ValueError(
                "process commitment cue requires an applicable commitment status"
            )
    if commitment_status == "unfulfilled" and trajectory_relation not in {
        "diverged",
        "remained_investigation",
    }:
        raise ValueError(
            "unfulfilled process commitment requires a divergent trajectory"
        )
    if downstream_after != predicted_defect_status:
        raise ValueError(
            "process confirmation assessment contradicts top-level counterfactual"
        )
    if (
        cue_disposition == "commitment"
        and candidate_role == "implementation_commitment"
        and commitment_status == "unfulfilled"
        and intervention_scope == "decision_and_committed_followup"
        and task_disposition == "repair_obligation"
        and (active_after != "absent" or downstream_after != "absent")
    ):
        raise ValueError(
            "process confirmation assessment contradicts top-level counterfactual"
        )
    if confirmation_status == "confirmed" and (
        active_after != "absent" or downstream_after != "absent"
    ):
        raise ValueError(
            "confirmed process root requires an absent post-intervention defect"
        )
    return {
        "candidate_role": candidate_role,
        "commitment_cue_disposition": cue_disposition,
        "commitment_status": commitment_status,
        "trajectory_relation": trajectory_relation,
        "intervention_scope": intervention_scope,
        "task_precondition_disposition": task_disposition,
        "active_process_defect_after_intervention": active_after,
        "downstream_failure_after_intervention": downstream_after,
        "reason": reason,
        "evidence_refs": list(evidence_refs),
    }


def validate_recursive_confirmation(
    value: Dict[str, Any], *, request: RootConfirmationRequest
) -> RootConfirmation:
    if not isinstance(value, dict):
        raise TypeError("root confirmation payload must be an object")
    if str(value.get("candidate_ref") or "") != request.candidate_ref:
        raise ValueError("candidate_ref must match the offered candidate")
    status = str(value.get("status") or "").strip().lower()
    if status not in {"confirmed", "rejected", "unknown"}:
        raise ValueError("root confirmation status must be confirmed, rejected, or unknown")
    reason = str(value.get("reason") or "").strip()
    if not reason:
        raise ValueError("root confirmation reason must be non-empty")
    confidence = _number(value.get("confidence"), "confidence")
    if status != "unknown" and confidence <= 0.0:
        raise ValueError(
            "non-unknown confirmation requires positive confidence"
        )
    fact_tree = _ConfirmationFactTreeValidator(request).validate()
    evidence_refs = _validate_evidence_refs(
        value.get("evidence_refs", []),
        grounded_refs=fact_tree.grounded_refs,
        field_name="root confirmation evidence_refs",
    )
    excerpt = str(value.get("excerpt") or "").strip()
    raw_counterfactual = value.get("counterfactual")
    if not isinstance(raw_counterfactual, Mapping):
        raise ValueError("counterfactual must be a structured object")
    intervention_ref = str(raw_counterfactual.get("intervention_ref") or "").strip()
    if intervention_ref != request.candidate_ref:
        raise ValueError("counterfactual intervention_ref must match candidate_ref")
    intervention_kind = str(raw_counterfactual.get("intervention_kind") or "").strip()
    if intervention_kind != "replace_with_semantically_correct_behavior":
        raise ValueError(
            "counterfactual intervention_kind must be replace_with_semantically_correct_behavior"
        )
    predicted_status = str(
        raw_counterfactual.get("predicted_defect_status") or ""
    ).strip().lower()
    if predicted_status not in {"absent", "present", "unknown"}:
        raise ValueError("counterfactual predicted_defect_status is invalid")
    causal_effect = str(raw_counterfactual.get("causal_effect") or "").strip().lower()
    if causal_effect not in {"prevents_defect", "does_not_prevent_defect", "unknown"}:
        raise ValueError("counterfactual causal_effect is invalid")
    allowed_counterfactuals = {
        "confirmed": {("absent", "prevents_defect")},
        "rejected": {
            ("present", "does_not_prevent_defect"),
            ("unknown", "unknown"),
        },
        "unknown": {("unknown", "unknown")},
    }[status]
    if (predicted_status, causal_effect) not in allowed_counterfactuals:
        raise ValueError(
            "counterfactual status combination is inconsistent with confirmation status={0}".format(
                status
            )
        )
    counterfactual_status = {
        "prevents_defect": "supports_causality",
        "does_not_prevent_defect": "rejects_causality",
        "unknown": "unknown",
    }[causal_effect]
    factor_role = str(
        value.get("factor_role")
        or {
            "confirmed": "necessary_cause",
            "rejected": "unrelated",
            "unknown": "unknown",
        }[status]
    ).strip().lower()
    allowed_factor_roles = {
        "confirmed": {"necessary_cause"},
        "rejected": {
            "contributing_condition",
            "amplifying_factor",
            "unrelated",
            "unknown",
        },
        "unknown": {"unknown"},
    }[status]
    if factor_role not in allowed_factor_roles:
        raise ValueError(
            "factor_role is inconsistent with confirmation status={0}".format(status)
        )
    competitor_comparisons = _validate_competitor_comparisons(
        value.get("competitor_comparisons", []),
        request=request,
        confirmation_status=status,
        grounded_refs=fact_tree.grounded_refs,
    )
    factor_mechanism = _validate_factor_mechanism(
        value.get("factor_mechanism", {}),
        role=factor_role,
        request=request,
        evidence_refs=evidence_refs,
    )
    process_confirmation_assessment = (
        _validate_process_confirmation_assessment(
            value.get("process_confirmation_assessment"),
            request=request,
            confirmation_status=status,
            predicted_defect_status=predicted_status,
            grounded_refs=fact_tree.grounded_refs,
        )
    )
    if (
        status != "unknown"
        and counterfactual_status != "unknown"
        and not evidence_refs
    ):
        raise ValueError(
            "definitive confirmation requires grounded evidence refs"
        )
    counterfactual = confirmation_counterfactual_for(
        intervention_ref,
        status,
        counterfactual_status=counterfactual_status,
    )
    if status == "confirmed":
        if not evidence_refs:
            raise ValueError("confirmed root requires grounded evidence refs")
        if not excerpt:
            raise ValueError("confirmed root requires a grounded excerpt")
        normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip().lower()
        if not any(
            normalized_excerpt in fragment
            for fragment in fact_tree.candidate_semantic_fragments
        ):
            raise ValueError("confirmed root requires a grounded excerpt from candidate facts")
    confirmation = RootConfirmation(
        candidate_ref=request.candidate_ref,
        status=status,
        excerpt=excerpt,
        reason=reason,
        counterfactual=counterfactual,
        confidence=confidence,
        evidence_refs=evidence_refs,
        counterfactual_status=counterfactual_status,
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        recursive_path=request.recursive_path,
        seed_binding_identity=request.seed_binding_identity,
        analysis_perspective=request.analysis_perspective,
        factor_role=factor_role,
        competitor_comparisons=competitor_comparisons,
        factor_mechanism=factor_mechanism,
        process_confirmation_assessment=(
            process_confirmation_assessment
        ),
    )
    return validate_root_confirmation_substantive_invariants(
        confirmation,
        require_canonical_counterfactual=True,
    )


def preflight_root_confirmation_request(
    request: RootConfirmationRequest,
) -> _ConfirmationFactTreeResult:
    """Validate the complete independent-verifier fact tree before any capability call."""
    return _ConfirmationFactTreeValidator(request).validate()


def bind_root_confirmation(
    confirmation: RootConfirmation,
    *,
    request: RootConfirmationRequest,
) -> RootConfirmation:
    """Bind and validate an offline confirmation result against its exact request."""
    facts = preflight_root_confirmation_request(request)
    if confirmation.candidate_ref != request.candidate_ref:
        raise ValueError("confirmation candidate_ref does not match its request")
    if confirmation.hypothesis_id and confirmation.hypothesis_id != request.hypothesis_id:
        raise ValueError("confirmation is cross-bound to another hypothesis")
    if (
        confirmation.hypothesis_semantic_hash
        and confirmation.hypothesis_semantic_hash != request.hypothesis_semantic_hash
    ):
        raise ValueError("confirmation is cross-bound to another hypothesis semantic hash")
    if (
        confirmation.defect_fingerprint
        and confirmation.defect_fingerprint != request.defect_state.fingerprint
    ):
        raise ValueError("confirmation is cross-bound to another defect")
    if confirmation.recursive_path and confirmation.recursive_path != request.recursive_path:
        raise ValueError("confirmation is cross-bound to another recursive path")
    if (
        confirmation.seed_binding_identity
        and confirmation.seed_binding_identity != request.seed_binding_identity
    ):
        raise ValueError("confirmation is cross-bound to another seed")
    if (
        confirmation.analysis_perspective
        and confirmation.analysis_perspective != request.analysis_perspective
    ):
        raise ValueError(
            "confirmation is cross-bound to another analysis perspective"
        )
    validate_root_confirmation_substantive_invariants(
        confirmation,
        require_canonical_counterfactual=True,
    )
    allowed_factor_roles = {
        "confirmed": {"necessary_cause"},
        "rejected": {
            "contributing_condition",
            "amplifying_factor",
            "unrelated",
            "unknown",
        },
        "unknown": {"unknown"},
    }[confirmation.status]
    if confirmation.factor_role not in allowed_factor_roles:
        raise ValueError("confirmation factor_role contradicts its status")
    competitor_comparisons = _validate_competitor_comparisons(
        confirmation.competitor_comparisons,
        request=request,
        confirmation_status=confirmation.status,
        grounded_refs=facts.grounded_refs,
    )
    if confirmation.status == "confirmed":
        if confirmation.counterfactual_status != "supports_causality":
            raise ValueError("confirmed root requires a causality-supporting counterfactual")
        evidence_refs = _validate_evidence_refs(
            confirmation.evidence_refs,
            grounded_refs=facts.grounded_refs,
            field_name="root confirmation evidence_refs",
        )
        excerpt = re.sub(r"\s+", " ", confirmation.excerpt).strip().lower()
        if not excerpt or not any(
            excerpt in fragment for fragment in facts.candidate_semantic_fragments
        ):
            raise ValueError("confirmed root requires a grounded excerpt from candidate facts")
    else:
        evidence_refs = _validate_evidence_refs(
            confirmation.evidence_refs,
            grounded_refs=facts.grounded_refs,
            field_name="root confirmation evidence_refs",
        )
    factor_mechanism = _validate_factor_mechanism(
        confirmation.factor_mechanism,
        role=confirmation.factor_role,
        request=request,
        evidence_refs=evidence_refs,
    )
    counterfactual_payload = json.loads(confirmation.counterfactual)
    process_confirmation_assessment = (
        _validate_process_confirmation_assessment(
            confirmation.process_confirmation_assessment,
            request=request,
            confirmation_status=confirmation.status,
            predicted_defect_status=str(
                counterfactual_payload.get("predicted_defect_status") or ""
            ),
            grounded_refs=facts.grounded_refs,
        )
    )
    if (
        confirmation.status == "rejected"
        and is_definitive_confirmation(confirmation)
        and not evidence_refs
    ):
        raise ValueError(
            "definitive confirmation requires grounded evidence refs"
        )
    result = RootConfirmation(
        candidate_ref=confirmation.candidate_ref,
        status=confirmation.status,
        excerpt=confirmation.excerpt,
        reason=confirmation.reason,
        counterfactual=confirmation.counterfactual,
        confidence=confirmation.confidence,
        evidence_refs=evidence_refs,
        counterfactual_status=confirmation.counterfactual_status,
        hypothesis_id=request.hypothesis_id,
        hypothesis_semantic_hash=request.hypothesis_semantic_hash,
        defect_fingerprint=request.defect_state.fingerprint,
        recursive_path=request.recursive_path,
        seed_binding_identity=request.seed_binding_identity,
        analysis_perspective=request.analysis_perspective,
        factor_role=confirmation.factor_role,
        competitor_comparisons=competitor_comparisons,
        factor_mechanism=factor_mechanism,
        process_confirmation_assessment=(
            process_confirmation_assessment
        ),
    )
    return validate_root_confirmation_substantive_invariants(
        result,
        require_canonical_counterfactual=True,
    )


def root_confirmation_from_payload(
    value: Dict[str, Any], *, request: RootConfirmationRequest
) -> RootConfirmation:
    return validate_recursive_confirmation(value, request=request)


def _canonicalize_root_confirmation_payload(
    value: Dict[str, Any], *, request: RootConfirmationRequest
) -> Dict[str, Any]:
    """Normalize fact-equivalent confirmation fields without changing its verdict."""
    if not isinstance(value, dict):
        return value
    normalized = copy.deepcopy(value)
    if not request.process_factual_context:
        normalized["process_confirmation_assessment"] = None
    if str(normalized.get("status") or "").strip().lower() != "confirmed":
        return normalized

    try:
        fact_tree = _ConfirmationFactTreeValidator(request).validate()
    except (TypeError, ValueError):
        return normalized
    excerpt = str(normalized.get("excerpt") or "").strip()
    normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip().lower()
    if normalized_excerpt and any(
        normalized_excerpt in fragment
        for fragment in fact_tree.candidate_semantic_fragments
    ):
        return normalized

    return normalized


@dataclass(frozen=True)
class _RequestOutcome:
    payload: Optional[JsonDict]
    error_kind: str = ""
    error_detail: str = ""
    physical_requests: int = 0


def _repair_constraints(
    *,
    stage: str,
    node_ref: str,
    request_context: JsonDict,
    validation_error: str = "",
) -> JsonDict:
    if stage == "factor_role_judgment":
        allowed_refs = _factor_role_allowed_refs_from_facts(request_context)
        candidate_ref = str(request_context.get("candidate_ref") or "")
        recursive_path = request_context.get("recursive_path")
        defect_state = request_context.get("defect_state")
        request_identity = factor_role_request_projection_identity(
            {
                "schema": FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA,
                "facts": request_context,
            }
        )
        return {
            "exact_top_level_fields": [
                "candidate_ref",
                "necessity_status",
                "factor_role",
                "reason",
                "confidence",
                "evidence_refs",
                "recursive_path",
                "factor_mechanism",
                "counterfactual",
                "hypothesis_id",
                "hypothesis_semantic_hash",
                "defect_fingerprint",
                "seed_binding_identity",
                "analysis_perspective",
                "request_identity",
            ],
            "allowed_fact_refs": sorted(ref for ref in allowed_refs if ref),
            "allowed_mechanism_target_refs": (
                list(recursive_path[1:])
                if isinstance(recursive_path, list)
                else []
            ),
            "exact_candidate_ref": candidate_ref,
            "exact_request_identity": request_identity,
            "exact_recursive_path": (
                list(recursive_path) if isinstance(recursive_path, list) else []
            ),
            "exact_bindings": {
                "candidate_ref": candidate_ref,
                "hypothesis_id": request_context.get("hypothesis_id"),
                "hypothesis_semantic_hash": request_context.get(
                    "hypothesis_semantic_hash"
                ),
                "defect_fingerprint": (
                    defect_state.get("fingerprint")
                    if isinstance(defect_state, Mapping)
                    else ""
                ),
                "seed_binding_identity": request_context.get(
                    "seed_binding_identity"
                ),
                "analysis_perspective": request_context.get(
                    "analysis_perspective"
                ),
                "request_identity": request_identity,
            },
            "exact_identity_bindings": {
                "hypothesis_id": request_context.get("hypothesis_id"),
                "hypothesis_semantic_hash": request_context.get(
                    "hypothesis_semantic_hash"
                ),
                "defect_fingerprint": (
                    defect_state.get("fingerprint")
                    if isinstance(defect_state, Mapping)
                    else ""
                ),
                "seed_binding_identity": request_context.get(
                    "seed_binding_identity"
                ),
                "analysis_perspective": request_context.get(
                    "analysis_perspective"
                ),
            },
            "role_contract": _thaw_json(FACTOR_ROLE_CONTRACT),
            "role_boundary_contract": _factor_role_boundary_contract(),
            "dimension_contract": {
                "necessity_status": (
                    "Counterfactual effect of replacing the candidate on "
                    "the observed final defect."
                ),
                "factor_role": (
                    "Candidate position relative to the earliest confirmed "
                    "defect introduction."
                ),
                "necessary_downstream_materialization": (
                    "Allowed only when one confirmed_root_summaries path "
                    "contains candidate_ref after its confirmed root and "
                    "the remaining suffix exactly equals exact_recursive_path."
                ),
            },
            "factor_mechanism_contract": {
                "empty_object_roles": [
                    "unknown",
                    "unrelated",
                ],
                "exact_object_schema": {
                    "schema": "factor-role-mechanism/v1",
                    "mechanism_type": (
                        "exact mechanism_type from selected "
                        "role_contract row"
                    ),
                    "source_ref": candidate_ref,
                    "target_ref": (
                        "one exact ref from "
                        "allowed_mechanism_target_refs"
                    ),
                    "effect": "non-empty grounded effect",
                },
            },
            "fact_closure_must_not_expand": True,
            "exact_validation_error": validation_error,
            "schema_rule": (
                "Return every exact_top_level_fields entry exactly once "
                "and no additional top-level fields."
            ),
        }
    if stage == "global_candidate_judgment":
        offered = request_context.get("offered_candidate_refs")
        grounded = request_context.get("grounded_refs")
        comparison_contract = (
            global_candidate_comparison_contract_from_context(
                request_context
            )
        )
        assessment_requirements = comparison_contract.get(
            "assessment_requirements"
        )
        exact_paths = {
            str(item.get("candidate_ref") or ""): list(
                item.get("required_causal_path_refs") or []
            )
            for item in (
                assessment_requirements
                if isinstance(assessment_requirements, list)
                else []
            )
            if isinstance(item, Mapping)
            and str(item.get("candidate_ref") or "")
        }
        open_refs = list(
            request_context.get(
                "open_authored_root_candidate_refs"
            )
            or []
        )
        grounded_refs = (
            list(grounded) if isinstance(grounded, list) else []
        )
        constraints = {
            "offered_candidate_refs": list(offered) if isinstance(offered, list) else [],
            "grounded_refs": grounded_refs,
            "active_focus_binding": {
                "seed_ref": request_context.get("seed_ref"),
                "defect_fingerprint": (
                    request_context.get("active_defect", {}).get("fingerprint")
                    if isinstance(request_context.get("active_defect"), Mapping)
                    else ""
                ),
                "active_focus_text_hash": request_context.get("active_focus_text_hash"),
            },
            "open_authored_root_candidate_refs": open_refs,
            "candidate_comparison_contract": comparison_contract,
            "exact_causal_path_refs_by_candidate": exact_paths,
            "exact_compared_candidate_refs": open_refs,
            "allowed_decisive_evidence_refs": grounded_refs,
            "allowed_assessment_evidence_refs": grounded_refs,
            "allowed_restoration_obligation_refs": [
                str(item.get("obligation_id") or "")
                for item in (
                    request_context.get("restoration_obligations") or ()
                )
                if isinstance(item, Mapping)
                and str(item.get("obligation_id") or "")
            ],
            "required_top_level_fields": [
                "outcome",
                "reason",
                "assessments",
                "selected_candidate_refs",
                "expansion_requests",
                "decisive_evidence_refs",
                "missing_evidence",
                "confidence",
                "active_focus_binding",
            ],
            "required_assessment_fields": [
                "candidate_ref",
                "defect_status",
                "input_defect_status",
                "output_defect_status",
                "causal_path_refs",
                "counterfactual",
                "compared_candidate_refs",
                "causal_role",
                "responsibility",
                "candidate_phase",
                "obligation_status_before",
                "obligation_status_after",
                "repair_window_effect",
                "failure_mode",
                "obligation_refs",
                "contribution_mechanism",
                "reason",
                "evidence_refs",
                "confidence",
            ],
            "allowed_assessment_enums": {
                "causal_role": sorted(CAUSAL_ROLES),
                "responsibility": sorted(RESPONSIBILITIES),
                "candidate_phase": sorted(CANDIDATE_PHASES),
                "obligation_status_before": sorted(
                    OBLIGATION_STATUSES
                ),
                "obligation_status_after": sorted(
                    OBLIGATION_STATUSES
                ),
                "repair_window_effect": sorted(
                    REPAIR_WINDOW_EFFECTS
                ),
                "failure_mode": sorted(FAILURE_MODES),
            },
            "assessment_compatibility_rules": [
                {
                    "when": {
                        "causal_role": [
                            "contributing_condition",
                            "amplifying_factor",
                        ]
                    },
                    "require": {
                        "failure_mode": [
                            "omission_enabling_condition",
                            "none",
                        ],
                        "contribution_mechanism": (
                            "non-null exact object"
                        ),
                    },
                },
                {
                    "when": {
                        "failure_mode": "ordinary_non_repair"
                    },
                    "forbid": {
                        "causal_role": [
                            "root_candidate",
                            "contributing_condition",
                            "amplifying_factor",
                        ]
                    },
                },
                {
                    "when": {
                        "causal_role": [
                            "outcome_evidence",
                            "exculpatory_evidence",
                            "unrelated",
                            "unknown",
                        ]
                    },
                    "require": {
                        "contribution_mechanism": None,
                    },
                },
            ],
            "comparison_then_selection": True,
            "valid_outcomes": [
                "candidate_roots",
                "no_root_candidates",
                "no_defect",
                "needs_expansion",
                "inconclusive",
            ],
        }
        if "unknown input defect cannot claim certain confidence" in validation_error:
            candidate_match = re.search(
                r"candidate\s+(\S+)\s+with unknown input defect",
                validation_error,
            )
            constraints["required_field_corrections"] = [
                {
                    "target_candidate_ref": (
                        candidate_match.group(1) if candidate_match else ""
                    ),
                    "when": {
                        "input_defect_status": "unknown",
                    },
                    "require": {
                        "confidence": 0.99,
                    },
                    "preserve": [
                        "candidate_ref",
                        "defect_status",
                        "output_defect_status",
                        "causal_role",
                        "responsibility",
                        "candidate_phase",
                        "obligation_status_before",
                        "obligation_status_after",
                        "repair_window_effect",
                        "failure_mode",
                        "obligation_refs",
                        "contribution_mechanism",
                        "causal_path_refs",
                        "compared_candidate_refs",
                        "counterfactual",
                        "reason",
                        "evidence_refs",
                    ],
                }
            ]
        if (
            "no_root_candidates requires known input status"
            in validation_error
        ):
            constraints["required_outcome_resolution"] = {
                "unknown_input_status": {
                    "outcome": "inconclusive",
                    "selected_candidate_refs": [],
                    "expansion_requests": [],
                    "missing_evidence_must_name_candidate_and_prior_state": True,
                    "reason": (
                        "An unknown pre-candidate defect state prevents "
                        "conclusive page-level root exclusion."
                    ),
                }
            }
        return constraints
    if stage == "recursive_root_confirmation":
        constraints = {"expected_node_ref": node_ref}
        process_facts = request_context.get("process_factual_context")
        if isinstance(process_facts, Mapping):
            process_grounded, process_unresolved = _reference_sets(
                process_facts
            )
            constraints[
                "required_process_confirmation_evidence_resolution"
            ] = {
                "required_candidate_evidence_ref": node_ref,
                "allowed_trajectory_evidence_refs": sorted(
                    ref
                    for ref in process_grounded - process_unresolved
                    if ref != node_ref
                ),
                "required_fields": {
                    "process_confirmation_assessment.evidence_refs": [
                        "required_candidate_evidence_ref",
                        "at_least_one_allowed_trajectory_evidence_ref",
                    ]
                },
                "invented_refs_forbidden": True,
            }
        if "competitor_comparisons must cover every open competitor" in validation_error:
            competitors = request_context.get("competing_hypotheses")
            required_comparisons = []
            for item in competitors if isinstance(competitors, list) else ():
                if not isinstance(item, Mapping):
                    continue
                candidate_reference = item.get("candidate_reference")
                candidate_ref = (
                    str(candidate_reference.get("resolved_ref") or "")
                    if isinstance(candidate_reference, Mapping)
                    else ""
                )
                required_comparisons.append(
                    {
                        "candidate_ref": candidate_ref,
                        "confirmation_identity": str(
                            item.get("confirmation_identity") or ""
                        ),
                        "hypothesis_id": str(
                            item.get("hypothesis_id") or ""
                        ),
                    }
                )
            constraints["required_competitor_comparisons"] = (
                required_comparisons
            )
            constraints["exact_competitor_coverage"] = True
        if "definitive confirmation requires grounded evidence refs" in validation_error:
            constraints["required_field_corrections"] = [
                {
                    "target_candidate_ref": node_ref,
                    "when": {
                        "status": ["confirmed", "rejected"],
                    },
                    "require": {
                        "evidence_refs": (
                            "non-empty array containing only grounded refs from "
                            "the factual request; include the candidate ref when "
                            "the candidate facts support the judgment"
                        ),
                    },
                    "preserve": [
                        "candidate_ref",
                        "status",
                        "factor_role",
                        "counterfactual",
                        "reason",
                        "competitor_comparisons",
                    ],
                }
            ]
        return constraints
    if stage == "candidate_cluster_triage_page":
        clusters = request_context.get("clusters")
        cluster_ids = [
            str(item.get("cluster_id") or "")
            for item in clusters
            if isinstance(item, Mapping) and item.get("cluster_id")
        ] if isinstance(clusters, list) else []
        return {
            "expected_page_identity": request_context.get("page_identity"),
            "expected_request_identity": request_context.get("request_identity"),
            "expected_partition_identity": request_context.get(
                "partition_identity"
            ),
            "expected_page_index": request_context.get("page_index"),
            "expected_page_count": request_context.get("page_count"),
            "required_cluster_ids_exactly_once": cluster_ids,
            "unselected_requires_trace_grounded_mismatch_evidence": True,
            "unknown_or_incomplete_must_be_uncertain": True,
            "navigation_only": True,
        }
    if stage != "recursive_causal_step":
        return {"expected_node_ref": node_ref}
    candidates = request_context.get("candidates")
    offered_refs = [
        str(item.get("ref") or "")
        for item in candidates
        if isinstance(item, Mapping) and item.get("ref")
    ] if isinstance(candidates, list) else []
    constraints = {
        "current_node_ref": node_ref,
        "offered_predecessor_refs": offered_refs,
        "current_node_is_not_a_predecessor": (
            "The current_node_ref must never appear in predecessors unless it is "
            "separately listed in offered_predecessor_refs."
        ),
        "valid_present_defect_endings": [
            "recurse through one or two offered grounded predecessors",
            (
                "declare candidate_introduction after assessing every offered predecessor "
                "and request root confirmation"
            ),
            "return concrete blocking missing_evidence",
        ],
    }
    defect_state = request_context.get("defect_state")
    recursive_context = request_context.get("recursive_context")
    if (
        isinstance(defect_state, Mapping)
        and defect_state.get("label") == "candidate_local_process_defect"
        and isinstance(recursive_context, Mapping)
    ):
        trajectory = recursive_context.get("candidate_process_trajectory")
        trajectory_grounded, trajectory_unresolved = _reference_sets(
            trajectory
        )
        constraints["required_process_evidence_resolution"] = {
            "required_candidate_evidence_ref": node_ref,
            "allowed_trajectory_evidence_refs": sorted(
                ref
                for ref in trajectory_grounded - trajectory_unresolved
                if ref != node_ref
            ),
            "allowed_obligation_refs": sorted(
                _task_obligation_reference_set(recursive_context)
            ),
            "when_commitment_status_is_unfulfilled": {
                "evidence_refs_must_include": [
                    "required_candidate_evidence_ref",
                    "at_least_one_allowed_trajectory_evidence_ref",
                ],
                "obligation_refs_must_include": (
                    "at_least_one_allowed_obligation_ref"
                ),
            },
            "invented_or_positional_refs_forbidden": True,
        }
    if (
        "present defect cannot terminate silently" in validation_error
        and not offered_refs
        and isinstance(defect_state, Mapping)
        and defect_state.get("label") == "candidate_local_process_defect"
        and isinstance(recursive_context, Mapping)
    ):
        constraints["required_process_root_resolution"] = {
            "when": {
                "current_defect_status": "present",
                "offered_predecessor_refs": [],
            },
            "preferred_resolution": {
                "candidate_introduction": True,
                "missing_evidence": [],
                "suggested_investigation": {
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": str(
                            recursive_context.get("active_hypothesis_id") or ""
                        ),
                        "candidate_ref": node_ref,
                        "defect_fingerprint": str(
                            defect_state.get("fingerprint") or ""
                        ),
                    },
                    "reason": "non-empty grounded reason",
                },
            },
            "alternative_when_evidence_is_insufficient": {
                "current_defect_status": "unknown",
                "candidate_introduction": False,
                "missing_evidence": "non-empty concrete evidence gap",
            },
            "preserve_fields": [
                "current_node_ref",
                "current_defect_reason",
                "predecessors",
                "process_assessment",
                "confidence",
            ],
        }
    return constraints


class ClaudeCausalJudge(
    BoundedJudgeCapability,
    GlobalJudgeCapability,
    ClusterTriageCapability,
):
    def __init__(self, *, transport: ClaudeJudgeClient, cache: JudgmentCache):
        self.transport = transport
        self.cache = cache

    def judge_candidates_bounded(
        self,
        request: GlobalCandidateJudgeRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        prompt = build_global_candidate_prompt(request)
        structural_corrections: List[JsonDict] = []

        def normalize_global_candidate(
            value: JsonDict,
        ) -> JsonDict:
            normalized, corrections = (
                canonicalize_global_candidate_structural_bindings(
                    value,
                    request=request,
                )
            )
            structural_corrections.extend(
                correction for correction in corrections
            )
            return normalized

        outcome = self._request_validated(
            stage="global_candidate_judgment",
            schema_version=GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            system=GLOBAL_CANDIDATE_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref="global:{0}:{1}".format(request.case_id, request.seed_ref),
            request_context=request.judge_prompt_projection(),
            validator=lambda value: validate_global_candidate_payload(
                value, request=request
            ),
            normalizer=normalize_global_candidate,
            max_tokens=int(getattr(self.transport, "max_tokens", 4096)),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                judgment = global_candidate_judgment_from_payload(
                    outcome.payload, request=request
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation global adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                    diagnostics=global_judge_diagnostics(
                        structural_corrections
                    ),
                ) from exc
            return BoundedJudgeCallResult(
                judgment,
                outcome.physical_requests,
                {
                    **global_judge_diagnostics(
                        structural_corrections
                    ),
                },
            )
        detail = "global_judge_{0}: {1}".format(
            outcome.error_kind or "error", outcome.error_detail
        )
        raise BoundedJudgeCallError(
            detail,
            physical_requests=outcome.physical_requests,
            diagnostics=global_judge_diagnostics(
                structural_corrections
            ),
        )

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
    ) -> BoundedJudgeCallResult:
        pages = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=eligible_capsules,
            active_defect=active_defect,
            objective=objective,
            analysis_perspective=analysis_perspective,
        )
        physical_requests = 0
        judgments = []
        for page in pages:
            remaining = (
                None
                if max_physical_requests is None
                else max(0, int(max_physical_requests) - physical_requests)
            )
            try:
                outcome = self.triage_candidate_cluster_page_bounded(
                    page,
                    max_physical_requests=remaining,
                )
            except BoundedJudgeCallError as exc:
                raise BoundedJudgeCallError(
                    str(exc),
                    physical_requests=(
                        physical_requests + exc.physical_requests
                    ),
                    diagnostics={
                        "completed_pages": len(judgments),
                        "page_count": len(pages),
                        "failed_page_index": page.page_index,
                        "page_diagnostics": _thaw_json(exc.diagnostics),
                    },
                ) from exc
            physical_requests += outcome.physical_requests
            judgments.append(outcome.value)
        try:
            decision = merge_cluster_triage_judgments(
                request=request,
                manifest=manifest,
                eligible_capsules=eligible_capsules,
                active_defect=active_defect,
                objective=objective,
                analysis_perspective=analysis_perspective,
                pages=pages,
                judgments=tuple(judgments),
            )
        except Exception as exc:
            raise BoundedJudgeCallError(
                "cluster triage merge failed: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
                physical_requests=physical_requests,
                diagnostics={
                    "completed_pages": len(judgments),
                    "page_count": len(pages),
                },
            ) from exc
        return BoundedJudgeCallResult(
            decision,
            physical_requests,
            {
                "completed_pages": len(judgments),
                "page_count": len(pages),
            },
        )

    def triage_candidate_cluster_page_bounded(
        self,
        page: ClusterTriagePageRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        if not isinstance(page, ClusterTriagePageRequest):
            raise TypeError("cluster triage page request is invalid")
        outcome = self._request_validated(
            stage="candidate_cluster_triage_page",
            schema_version=CLUSTER_TRIAGE_PROMPT_SCHEMA_VERSION,
            system=CLUSTER_TRIAGE_SYSTEM_PROMPT,
            prompt=build_cluster_triage_prompt(page),
            node_ref="cluster-triage-page:{0}".format(page.page_index),
            request_context=page.to_dict(),
            validator=lambda value: parse_cluster_triage_judgment(
                value,
                page=page,
            ),
            max_tokens=min(
                int(getattr(self.transport, "max_tokens", 4096)),
                8192,
            ),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is None:
            raise BoundedJudgeCallError(
                "cluster_triage_{0}: {1}".format(
                    outcome.error_kind or "error",
                    outcome.error_detail,
                ),
                physical_requests=outcome.physical_requests,
                diagnostics={
                    "page_index": page.page_index,
                    "page_count": page.page_count,
                },
            )
        try:
            judgment = parse_cluster_triage_judgment(
                outcome.payload,
                page=page,
            )
        except Exception as exc:
            raise BoundedJudgeCallError(
                "post-validation cluster triage adapter failed: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
                physical_requests=outcome.physical_requests,
                diagnostics={
                    "page_index": page.page_index,
                    "page_count": page.page_count,
                },
            ) from exc
        return BoundedJudgeCallResult(
            judgment,
            outcome.physical_requests,
            {
                "page_index": page.page_index,
                "page_count": page.page_count,
            },
        )

    def judge_step(self, request: CausalStepRequest) -> CausalStepJudgment:
        return self.judge_step_bounded(request, max_physical_requests=None).value

    def judge_factor_role(
        self, request: FactorRoleRequest
    ) -> FactorRoleJudgment:
        return self.judge_factor_role_bounded(
            request, max_physical_requests=None
        ).value

    def judge_factor_role_bounded(
        self,
        request: FactorRoleRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        prompt = build_factor_role_prompt(request)
        outcome = self._request_validated(
            stage="factor_role_judgment",
            schema_version=FACTOR_ROLE_PROMPT_SCHEMA_VERSION,
            system=FACTOR_ROLE_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validator=lambda value: parse_factor_role_judgment(
                value, request=request
            ),
            max_tokens=min(
                int(getattr(self.transport, "max_tokens", 4096)), 2048
            ),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                value = parse_factor_role_judgment(
                    outcome.payload, request=request
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation factor adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(value, outcome.physical_requests)
        raise BoundedJudgeCallError(
            "factor_role_{0}: {1}".format(
                outcome.error_kind or "error",
                outcome.error_detail,
            ),
            physical_requests=outcome.physical_requests,
        )

    def judge_step_bounded(
        self,
        request: CausalStepRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        prompt = build_causal_step_prompt(request)
        outcome = self._request_validated(
            stage="recursive_causal_step",
            schema_version=CAUSAL_STEP_PROMPT_SCHEMA_VERSION,
            system=CAUSAL_STEP_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.current_node.ref,
            request_context=request.to_dict(),
            validator=lambda value: validate_causal_step_payload(
                _canonicalize_causal_step_payload(value, request=request),
                request=request,
            ),
            max_tokens=int(getattr(self.transport, "max_tokens", 4096)),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                value = causal_step_from_payload(
                    _canonicalize_causal_step_payload(
                        outcome.payload, request=request
                    ),
                    request=request,
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(value, outcome.physical_requests)
        detail = "judge_{0}: {1}".format(outcome.error_kind or "error", outcome.error_detail)
        return BoundedJudgeCallResult(CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="The causal relation judge could not produce a validated result.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=(detail,),
            suggested_investigation={"kind": "judge_retry", "reason": detail},
            confidence=0.0,
        ), outcome.physical_requests)

    def confirm_candidate(self, request: RootConfirmationRequest) -> RootConfirmation:
        return self.confirm_candidate_bounded(request, max_physical_requests=None).value

    def confirm_candidate_bounded(
        self,
        request: RootConfirmationRequest,
        *,
        max_physical_requests: Optional[int],
    ) -> BoundedJudgeCallResult:
        try:
            _ConfirmationFactTreeValidator(request).validate()
        except (TypeError, ValueError) as exc:
            return BoundedJudgeCallResult(
                RootConfirmation.unknown(
                    request.candidate_ref,
                    "Judge request_ineligible: {0}: {1}".format(type(exc).__name__, exc),
                ),
                0,
            )
        prompt = build_recursive_confirmation_prompt(request)
        outcome = self._request_validated(
            stage="recursive_root_confirmation",
            schema_version=ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
            system=ROOT_CONFIRMATION_SYSTEM_PROMPT,
            prompt=prompt,
            node_ref=request.candidate_ref,
            request_context=request.factual_dict(),
            validator=lambda value: validate_recursive_confirmation(value, request=request),
            normalizer=lambda value: _canonicalize_root_confirmation_payload(
                value,
                request=request,
            ),
            max_tokens=min(int(getattr(self.transport, "max_tokens", 4096)), 2048),
            max_physical_requests=max_physical_requests,
        )
        if outcome.payload is not None:
            try:
                value = root_confirmation_from_payload(
                    _canonicalize_root_confirmation_payload(
                        outcome.payload,
                        request=request,
                    ),
                    request=request,
                )
            except Exception as exc:
                raise BoundedJudgeCallError(
                    "post-validation adapter failed: {0}: {1}".format(
                        type(exc).__name__, exc
                    ),
                    physical_requests=outcome.physical_requests,
                ) from exc
            return BoundedJudgeCallResult(value, outcome.physical_requests)
        return BoundedJudgeCallResult(
            RootConfirmation.unknown(
                request.candidate_ref,
                "Judge {0}: {1}".format(outcome.error_kind or "error", outcome.error_detail),
            ),
            outcome.physical_requests,
        )

    def _request_validated(
        self,
        *,
        stage: str,
        schema_version: str,
        system: str,
        prompt: str,
        node_ref: str,
        request_context: JsonDict,
        validator: Callable[[JsonDict], Any],
        max_tokens: int,
        normalizer: Optional[Callable[[JsonDict], JsonDict]] = None,
        max_physical_requests: Optional[int] = None,
    ) -> _RequestOutcome:
        evidence_hash = hashlib.sha256(stable_json(request_context).encode("utf-8")).hexdigest()
        cache_context = stable_json(
            {
                "canonical_request_context": request_context,
                "hydrated_evidence_hash": evidence_hash,
            }
        )
        cache_key = build_judge_cache_key(
            stage=stage,
            model=str(getattr(self.transport, "model", "")),
            system=system,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "user", "content": cache_context},
            ],
            max_tokens=max_tokens,
            thinking_config=getattr(self.transport, "thinking_config", None),
            prompt_schema_version=schema_version,
        )
        def normalized(value: JsonDict) -> JsonDict:
            if normalizer is None:
                return value
            normalized_value = normalizer(value)
            value.clear()
            value.update(normalized_value)
            return value

        def validate_normalized(value: JsonDict) -> Any:
            return validator(normalized(value))

        try:
            cached = self.cache.get_validated_payload(
                key=cache_key,
                validator=validate_normalized,
            )
        except Exception as exc:
            raise BoundedJudgeCallError(
                "cache adapter failed before transport: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
                physical_requests=0,
            ) from exc
        if cached is not None:
            return _RequestOutcome(
                normalized(cached),
                physical_requests=0,
            )
        remaining_requests = (
            None
            if max_physical_requests is None
            else max(0, int(max_physical_requests))
        )
        if remaining_requests == 0:
            return _RequestOutcome(
                None,
                "request_budget_exhausted",
                "judge_request_budget_exhausted before initial request",
                0,
            )
        if remaining_requests is not None:
            remaining_requests -= 1
        messages = [{"role": "user", "content": prompt}]
        physical_requests = 0
        try:
            transport_result = self._call_transport(
                system=system,
                messages=messages,
                max_tokens=max_tokens,
            )
            physical_requests += transport_result.physical_requests
            text = transport_result.text
        except TransportCallError as exc:
            physical_requests += exc.physical_requests
            error = exc.error
            return _RequestOutcome(
                None,
                (
                    "provider_error"
                    if isinstance(error, (JudgeProviderError, JudgeProviderUnavailable))
                    else "adapter_error"
                ),
                "{0}: {1}".format(type(error).__name__, error),
                physical_requests,
            )
        try:
            payload = normalized(_parse_single_json_object(text))
            validator(payload)
        except Exception as first_error:
            exact_error = "{0}: {1}".format(type(first_error).__name__, first_error)
            if remaining_requests == 0:
                return _RequestOutcome(
                    None,
                    "request_budget_exhausted",
                    "{0}; judge_request_budget_exhausted before focused repair".format(
                        exact_error
                    ),
                    physical_requests,
                )
            if remaining_requests is not None:
                remaining_requests -= 1
            try:
                repair_payload = {
                    "invalid_output": text[:16000],
                    "validation_error": exact_error,
                    "validation_history": [exact_error],
                    "semantic_repair_attempt": 2,
                    "schema_version": schema_version,
                    "canonical_request_context": request_context,
                    "mandatory_output_contract": (
                        _mandatory_output_contract(stage)
                    ),
                    "repair_constraints": _repair_constraints(
                        stage=stage,
                        node_ref=node_ref,
                        request_context=request_context,
                        validation_error=exact_error,
                    ),
                }
                if stage != "global_candidate_judgment":
                    repair_payload["original_prompt"] = prompt
                repair_result = self._call_transport(
                    system=_repair_system_prompt(stage),
                    messages=[
                        {
                            "role": "user",
                            "content": stable_json(repair_payload),
                        }
                    ],
                    max_tokens=max_tokens,
                )
                physical_requests += repair_result.physical_requests
                repaired = repair_result.text
            except TransportCallError as exc:
                physical_requests += exc.physical_requests
                error = exc.error
                return _RequestOutcome(
                    None,
                    (
                        "provider_error"
                        if isinstance(
                            error, (JudgeProviderError, JudgeProviderUnavailable)
                        )
                        else "adapter_error"
                    ),
                    "{0}; repair {1}: {2}: {3}".format(
                        exact_error,
                        "provider error"
                        if isinstance(
                            error, (JudgeProviderError, JudgeProviderUnavailable)
                        )
                        else "adapter error",
                        type(error).__name__,
                        error,
                    ),
                    physical_requests,
                )
            try:
                payload = normalized(
                    _parse_single_json_object(repaired)
                )
                validator(payload)
            except Exception as repair_error:
                repair_error_detail = "{0}: {1}".format(
                    type(repair_error).__name__, repair_error
                )
                if remaining_requests == 0:
                    return _RequestOutcome(
                        None,
                        "request_budget_exhausted",
                        (
                            "{0}; focused repair invalid: {1}; "
                            "judge_request_budget_exhausted before full retry"
                        ).format(exact_error, repair_error_detail),
                        physical_requests,
                    )
                if remaining_requests is not None:
                    remaining_requests -= 1
                try:
                    retry_payload = {
                        "retry_instruction": (
                            "Return a complete replacement JSON object."
                        ),
                        "mandatory_output_contract": (
                            _mandatory_output_contract(stage)
                        ),
                        "validation_errors": [
                            exact_error,
                            repair_error_detail,
                        ],
                        "validation_history": [
                            exact_error,
                            repair_error_detail,
                        ],
                        "latest_validation_error": (
                            repair_error_detail
                        ),
                        "repair_constraints": _repair_constraints(
                            stage=stage,
                            node_ref=node_ref,
                            request_context=request_context,
                            validation_error="\n".join(
                                [exact_error, repair_error_detail]
                            ),
                        ),
                        "invalid_outputs": [
                            text[:16000],
                            repaired[:16000],
                        ],
                        "latest_invalid_output": repaired[:16000],
                        "semantic_repair_attempt": 3,
                        "schema_version": schema_version,
                        "canonical_request_context": request_context,
                    }
                    if stage != "global_candidate_judgment":
                        retry_payload["original_prompt"] = prompt
                    retry_result = self._call_transport(
                        system=system,
                        messages=[
                            {
                                "role": "user",
                                "content": stable_json(retry_payload),
                            }
                        ],
                        max_tokens=max_tokens,
                    )
                    physical_requests += retry_result.physical_requests
                    retried = retry_result.text
                except TransportCallError as exc:
                    physical_requests += exc.physical_requests
                    error = exc.error
                    return _RequestOutcome(
                        None,
                        (
                            "provider_error"
                            if isinstance(
                                error,
                                (JudgeProviderError, JudgeProviderUnavailable),
                            )
                            else "adapter_error"
                        ),
                        "{0}; focused repair invalid: {1}; full retry {2}: {3}: {4}".format(
                            exact_error,
                            repair_error_detail,
                            "provider error"
                            if isinstance(
                                error,
                                (JudgeProviderError, JudgeProviderUnavailable),
                            )
                            else "adapter error",
                            type(error).__name__,
                            error,
                        ),
                        physical_requests,
                    )
                try:
                    payload = normalized(
                        _parse_single_json_object(retried)
                    )
                    validator(payload)
                except Exception as retry_error:
                    retry_error_detail = "{0}: {1}".format(
                        type(retry_error).__name__,
                        retry_error,
                    )
                    validation_history = [
                        exact_error,
                        repair_error_detail,
                        retry_error_detail,
                    ]
                    invalid_outputs = [
                        text[:16000],
                        repaired[:16000],
                        retried[:16000],
                    ]

                    def validation_signature(detail: str) -> str:
                        return re.sub(
                            r"(?i)(?:record|node|artifact|obligation):"
                            r"[A-Za-z0-9_.:/-]+",
                            "<grounded-ref>",
                            detail,
                        )

                    def validation_history_detail() -> str:
                        details = [
                            validation_history[0],
                            "focused repair invalid: {0}".format(
                                validation_history[1]
                            ),
                            "full retry invalid: {0}".format(
                                validation_history[2]
                            ),
                        ]
                        details.extend(
                            (
                                "semantic repair attempt {0} invalid: "
                                "{1}"
                            ).format(
                                attempt,
                                validation_history[attempt - 1],
                            )
                            for attempt in range(
                                4,
                                len(validation_history) + 1,
                            )
                        )
                        return "; ".join(details)

                    if len(
                        {
                            validation_signature(detail)
                            for detail in validation_history[-3:]
                        }
                    ) == 1:
                        return _RequestOutcome(
                            None,
                            "validation_error",
                            (
                                "{0}; "
                                "validation_stalled_after_3_"
                                "equivalent_errors"
                            ).format(validation_history_detail()),
                            physical_requests,
                        )
                    if remaining_requests == 0:
                        return _RequestOutcome(
                            None,
                            "request_budget_exhausted",
                            (
                                "{0}; judge_request_budget_exhausted "
                                "before semantic repair attempt 4"
                            ).format(validation_history_detail()),
                            physical_requests,
                        )
                    semantic_repair_succeeded = False
                    for semantic_attempt in range(
                        4,
                        MAX_SEMANTIC_REPAIR_ATTEMPTS + 1,
                    ):
                        if remaining_requests == 0:
                            return _RequestOutcome(
                                None,
                                "request_budget_exhausted",
                                (
                                    "{0}; judge_request_budget_exhausted "
                                    "before semantic repair attempt {1}"
                                ).format(
                                    validation_history_detail(),
                                    semantic_attempt,
                                ),
                                physical_requests,
                            )
                        if remaining_requests is not None:
                            remaining_requests -= 1
                        semantic_repair_payload = {
                            "retry_instruction": (
                                "Return a complete replacement JSON "
                                "object."
                            ),
                            "mandatory_output_contract": (
                                _mandatory_output_contract(stage)
                            ),
                            "validation_errors": list(
                                validation_history
                            ),
                            "validation_history": list(
                                validation_history
                            ),
                            "latest_validation_error": (
                                validation_history[-1]
                            ),
                            "repair_constraints": _repair_constraints(
                                stage=stage,
                                node_ref=node_ref,
                                request_context=request_context,
                                validation_error="\n".join(
                                    validation_history
                                ),
                            ),
                            "invalid_outputs": list(invalid_outputs),
                            "latest_invalid_output": (
                                invalid_outputs[-1]
                            ),
                            "semantic_repair_attempt": semantic_attempt,
                            "schema_version": schema_version,
                            "canonical_request_context": (
                                request_context
                            ),
                        }
                        if stage != "global_candidate_judgment":
                            semantic_repair_payload[
                                "original_prompt"
                            ] = prompt
                        try:
                            semantic_repair_result = (
                                self._call_transport(
                                    system=system,
                                    messages=[
                                        {
                                            "role": "user",
                                            "content": stable_json(
                                                semantic_repair_payload
                                            ),
                                        }
                                    ],
                                    max_tokens=max_tokens,
                                )
                            )
                            physical_requests += (
                                semantic_repair_result.physical_requests
                            )
                            semantic_repair_text = (
                                semantic_repair_result.text
                            )
                        except TransportCallError as exc:
                            physical_requests += exc.physical_requests
                            error = exc.error
                            return _RequestOutcome(
                                None,
                                (
                                    "provider_error"
                                    if isinstance(
                                        error,
                                        (
                                            JudgeProviderError,
                                            JudgeProviderUnavailable,
                                        ),
                                    )
                                    else "adapter_error"
                                ),
                                (
                                    "{0}; semantic repair attempt {1} "
                                    "failed: {2}: {3}"
                                ).format(
                                    validation_history_detail(),
                                    semantic_attempt,
                                    type(error).__name__,
                                    error,
                                ),
                                physical_requests,
                            )
                        try:
                            payload = normalized(
                                _parse_single_json_object(
                                    semantic_repair_text
                                )
                            )
                            validator(payload)
                            semantic_repair_succeeded = True
                            break
                        except Exception as semantic_repair_error:
                            validation_history.append(
                                "{0}: {1}".format(
                                    type(semantic_repair_error).__name__,
                                    semantic_repair_error,
                                )
                            )
                            invalid_outputs.append(
                                semantic_repair_text[:16000]
                            )
                            if len(
                                {
                                    validation_signature(detail)
                                    for detail in validation_history[-3:]
                                }
                            ) == 1:
                                return _RequestOutcome(
                                    None,
                                    "validation_error",
                                    (
                                        "{0}; "
                                        "validation_stalled_after_3_"
                                        "equivalent_errors"
                                    ).format(
                                        validation_history_detail()
                                    ),
                                    physical_requests,
                                )
                    if not semantic_repair_succeeded:
                        return _RequestOutcome(
                            None,
                            "validation_error",
                            (
                                "{0}; semantic_repair_attempt_limit_"
                                "reached_after_{1}"
                            ).format(
                                validation_history_detail(),
                                MAX_SEMANTIC_REPAIR_ATTEMPTS,
                            ),
                            physical_requests,
                        )
        try:
            self.cache.put_payload(
                key=cache_key,
                stage=stage,
                model=str(getattr(self.transport, "model", "")),
                node_ref=node_ref,
                payload=payload,
            )
        except Exception as exc:
            raise BoundedJudgeCallError(
                "cache adapter failed after validated transport: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
                physical_requests=physical_requests,
            ) from exc
        return _RequestOutcome(payload, physical_requests=physical_requests)

    def _call_transport(
        self, *, system: str, messages: List[JsonDict], max_tokens: int
    ) -> TransportCallResult:
        call = getattr(self.transport, "create_message_text_with_usage", None)
        if not callable(call):
            raise TransportCallError(
                TypeError(
                    "transport must implement create_message_text_with_usage"
                ),
                physical_requests=0,
            )
        try:
            result = call(system=system, messages=messages, max_tokens=max_tokens)
        except TransportCallError:
            raise
        except Exception as exc:
            raise TransportCallError(exc, physical_requests=0) from exc
        if not isinstance(result, TransportCallResult):
            raise TransportCallError(
                TypeError("transport must return TransportCallResult"),
                physical_requests=0,
            )
        return result


def _parse_single_json_object(text: str) -> JsonDict:
    value = json.loads(text.strip())
    if not isinstance(value, dict):
        raise TypeError("judge response must be one JSON object")
    return value


__all__ = [
    "BoundedJudgeCallResult",
    "BoundedJudgeCallError",
    "BoundedJudgeCapability",
    "CAUSAL_STEP_PROMPT_SCHEMA_VERSION",
    "CAUSAL_STEP_SYSTEM_PROMPT",
    "FACTOR_ROLE_PROMPT_SCHEMA_VERSION",
    "FACTOR_ROLE_SYSTEM_PROMPT",
    "ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION",
    "ROOT_CONFIRMATION_REQUEST_IDENTITY_PREFIX",
    "ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA",
    "ROOT_CONFIRMATION_SYSTEM_PROMPT",
    "CausalJudge",
    "CausalStepRequest",
    "ClaudeCausalJudge",
    "FactorRoleRequest",
    "OfflineCausalJudgeAdapter",
    "OfflineJudgeCapability",
    "RootConfirmationRequest",
    "build_causal_step_prompt",
    "build_factor_role_prompt",
    "build_recursive_confirmation_prompt",
    "causal_step_from_payload",
    "root_confirmation_from_payload",
    "root_confirmation_request_identity",
    "root_confirmation_request_projection",
    "factor_role_request_from_projection",
    "factor_role_request_identity",
    "factor_role_request_projection",
    "factor_role_request_projection_identity",
    "parse_factor_role_judgment",
    "validate_causal_step_payload",
    "validate_recursive_confirmation",
]
