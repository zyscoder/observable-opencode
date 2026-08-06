"""Serializable state for recursive offline causal attribution."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import ntpath
import posixpath
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import JsonDict, TraceNode, stable_json


CAUSAL_RELATIONS = frozenset(
    {
        "same_defect_propagation",
        "defect_transformation",
        "introduction_candidate",
        "contributing_condition",
        "amplifying_factor",
        "outcome_evidence",
        "unrelated",
        "unknown",
    }
)
HYPOTHESIS_STATUSES = frozenset({"active", "supported", "rejected", "superseded", "unresolved"})
CONFIRMATION_STATUSES = frozenset({"confirmed", "rejected", "unknown"})
COUNTERFACTUAL_STATUSES = frozenset(
    {"supports_causality", "rejects_causality", "unknown"}
)
CONFIRMATION_FACTOR_ROLES = frozenset(
    {"necessary_cause", "contributing_condition", "amplifying_factor", "unrelated", "unknown"}
)
ACTIVE_FAILURE_CAUSAL_ROLES = frozenset(
    {
        "defect_introduction_root",
        "verification_omission",
        "false_closure",
        "downstream_materialization",
        "amplifying_condition",
        "external_interruption",
    }
)
ACTIVE_FAILURE_KINDS = frozenset(
    {"functional", "acceptance", "verification", "external", "generic"}
)
ACTIVE_FAILURE_ROLE_DISPOSITIONS = frozenset({"root", "factor"})
ACTIVE_FAILURE_ROOT_ROLES_BY_KIND = {
    "functional": frozenset({"defect_introduction_root"}),
    "acceptance": frozenset(
        {
            "defect_introduction_root",
            "verification_omission",
            "false_closure",
        }
    ),
    "verification": frozenset(
        {
            "defect_introduction_root",
            "verification_omission",
            "false_closure",
        }
    ),
    "external": frozenset(
        {"defect_introduction_root", "external_interruption"}
    ),
    "generic": frozenset({"defect_introduction_root"}),
}
ACTIVE_FAILURE_ROLE_BINDING_SCHEMA = "active-failure-role-binding/v2"
LEGACY_ACTIVE_FAILURE_ROLE_BINDING_SCHEMA = "active-failure-role-binding/v1"
FACTOR_ROLE_MECHANISM_SCHEMA = "factor-role-mechanism/v1"
FACTOR_ROLE_COUNTERFACTUAL_SCHEMA = "factor-role-counterfactual/v1"
FACTOR_ROLE_JUDGMENT_IDENTITY_PREFIX = "factor-role-judgment:v1:"
BLOCKING_METADATA_KEYS = frozenset(
    {
        "unresolved_reason",
        "unresolved_reasons",
        "unresolved_refs",
        "missing_evidence",
        "missing_artifact",
        "provider_error",
        "provider_unavailable",
        "provider_circuit_open",
        "blocked",
        "blocking_reason",
    }
)
MODERN_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v22"
PREVIOUS_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v2"
FACTOR_ROLE_PUBLICATION_CONTRACT_VERSION = "factor-role-publication/v2"
MODERN_FACTOR_AUDIT_METADATA_KEYS = frozenset(
    {
        "factor_role_judgments",
        "factor_role_journal",
        "factor_role_action_projections",
        "factor_role_gaps",
        "factor_role_escalation_gaps",
    }
)
LEGACY_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v1-legacy"
MODERN_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "objective",
        "start_refs",
        "seed_results",
        "analysis_outcome",
        "analysis_perspective",
        "defect_states",
        "causal_candidates",
        "causal_relations",
        "step_judgments",
        "hypotheses",
        "introduction_candidates",
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "downstream_materializations",
        "rejected_candidates",
        "unresolved_hypotheses",
        "root_causes",
        "taint_paths",
        "visited_order",
        "visited_entries",
        "unresolved_refs",
        "investigation_journal",
        "metadata",
    }
)
_MODERN_REPORT_OBJECT_COLLECTION_FIELDS = frozenset(
    {
        "seed_results",
        "defect_states",
        "causal_candidates",
        "causal_relations",
        "step_judgments",
        "hypotheses",
        "introduction_candidates",
        "confirmations",
        "confirmed_roots",
        "co_roots",
        "contributing_conditions",
        "amplifying_factors",
        "downstream_materializations",
        "rejected_candidates",
        "unresolved_hypotheses",
        "root_causes",
        "visited_entries",
        "investigation_journal",
    }
)
_MODERN_REPORT_STRING_COLLECTION_FIELDS = frozenset(
    {"start_refs", "visited_order", "unresolved_refs"}
)
_MODERN_REPORT_PATH_COLLECTION_FIELDS = frozenset({"taint_paths"})
_MODERN_REPORT_STRING_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "objective",
        "analysis_outcome",
        "analysis_perspective",
    }
)
CAUSAL_PUBLICATION_CONTRACT_VERSION = "causal-publication/v3"
GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION = "global-candidate-judgment/v11"
GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION = (
    "global-candidate-judgment/v11+validation-envelope/v11+capsule/v8"
    "+evidence-policy/v5+local-state-owner/v1+global-pass-identity/v1"
    "+failure-action/v3+failure-projection/v4+terminal-record-schema/v3"
    "+judge-lifecycle/v1"
    "+graph-seed-authority/v1+objective-authority/v1"
    "+candidate-set-closure/v1"
    "+comparison-matrix-closure/v1"
    "+bounded-evidence-expansion/v1"
)
GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION = (
    "global-candidate-validation-envelope/v11"
)
ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION = (
    "recursive-root-confirmation/v17+resolution/v2+evidence-policy/v5"
    "+artifact-owner/v1+terminal-evidence/v2+local-state-owner/v1"
    "+action-projection/v8+response-identity/v1+counterfactual/v1"
    "+queue-response-identity/v1+published-root-projection/v3"
    "+causal-publication/v3"
    "+perspective-binding/v1"
    "+step-action-projection/v1+confirmation-request-identity/v3"
    "+confirmation-request-projection/v2"
)
ROOT_CONFIRMATION_COUNTERFACTUAL_SCHEMA = (
    "root-confirmation-counterfactual/v1"
)
SEMANTIC_ANCHOR_SCHEMA_VERSION = "semantic-anchor/v2"
SEMANTIC_ANCHOR_PREFIX = "semantic_anchor:v2:"
SEMANTIC_OCCURRENCE_SCHEMA_VERSION = "semantic-occurrence/v1"
SEMANTIC_OCCURRENCE_PREFIX = "semantic_occurrence:v1:"
LOCAL_STATE_OCCURRENCE_PREFIX = "local_state_occurrence:v1:"

_ANCHOR_VOLATILE_KEYS = frozenset(
    {
        "cwd",
        "workdir",
        "working_directory",
        "repository_root",
        "repo_root",
        "workspace_root",
        "timestamp",
        "start_timestamp",
        "end_timestamp",
        "created_at",
        "updated_at",
        "pid",
        "ppid",
        "port",
        "sessionid",
        "session_id",
        "messageid",
        "message_id",
        "requestid",
        "request_id",
        "providerid",
        "provider_id",
        "provider_request_id",
        "span_id",
        "trace_id",
        "run_id",
        "call_id",
        "artifact_id",
        "hydrated_artifacts",
        "model",
        "modelid",
        "model_id",
    }
)
_ANCHOR_PATH_KEYS = frozenset(
    {
        "path",
        "file",
        "file_path",
        "filepath",
        "files",
        "code_location",
        "code_locations",
        "artifact_path",
    }
)
_ANCHOR_ARTIFACT_HASH_KEYS = frozenset(
    {"hash", "sha256", "content_hash", "artifact_hash", "digest"}
)
_ANCHOR_SET_LIKE_KEYS = frozenset(
    {
        "files",
        "artifact_ids",
        "artifact_refs",
        "evidence_refs",
        "direct_evidence_refs",
        "direct_support_refs",
        "candidate_evidence_refs",
        "referenced_artifact_ids",
        "missing_artifact_ids",
        "truncated_artifact_ids",
        "quality_flags",
    }
)
_ANCHOR_IDENTIFIER_KEYS = frozenset(
    {
        "action",
        "action_name",
        "chosen_action",
        "identifier",
        "symbol",
        "symbol_name",
        "method",
        "method_name",
        "function",
        "function_name",
        "class_name",
        "module",
        "module_name",
        "operation",
        "operation_name",
    }
)
_RUNTIME_ID_PATTERN = re.compile(
    r"\b(?:ses|msg|req|call|span|run|trace|evt|event)_[A-Za-z0-9_-]{6,}\b",
    re.IGNORECASE,
)
_UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ISO_TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b"
)
_URL_PORT_PATTERN = re.compile(r"(?P<host>\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)):\d{2,5}\b")


class FrozenMapping(Mapping[str, Any]):
    """Immutable JSON mapping backed by recursively frozen key/value entries."""

    __slots__ = ("_entries", "_sealed")

    def __init__(self, value: Optional[Mapping[str, Any]] = None) -> None:
        object.__setattr__(
            self,
            "_entries",
            tuple((str(key), _freeze(item)) for key, item in (value or {}).items()),
        )
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("FrozenMapping is immutable")
        object.__setattr__(self, name, value)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._entries:
            if candidate == key:
                return value
        raise KeyError(key)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Mapping) and _thaw(self) == _thaw(other)

    def __repr__(self) -> str:
        return repr(dict(self.items()))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenMapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def active_failure_signature_for(
    defect_state: "DefectState",
    *,
    seed_ref: str,
    seed_facts: Any = None,
) -> JsonDict:
    if not isinstance(defect_state, DefectState):
        raise TypeError("active failure signature requires a DefectState")
    if not str(seed_ref).strip():
        raise ValueError("active failure signature requires seed_ref")
    structured = _structured_failure_signature_from_seed_facts(seed_facts)
    if structured is not None:
        signature_id = str(structured["signature_id"])
        identity_source = "structured_failure_signature"
        failure_kind = _structured_failure_kind(structured)
    else:
        signature_id = defect_state.fingerprint
        identity_source = "legacy_defect_state"
        semantic = " ".join(
            (
                defect_state.label,
                defect_state.scope,
                defect_state.mechanism,
            )
        ).casefold()
        if "acceptance" in semantic:
            failure_kind = "acceptance"
        elif "verification" in semantic or "test failure" in semantic:
            failure_kind = "verification"
        elif any(
            token in semantic
            for token in ("external", "interrupt", "provider", "timeout")
        ):
            failure_kind = "external"
        elif any(
            token in semantic
            for token in ("functional", "regression", "behavior", "defect")
        ):
            failure_kind = "functional"
        else:
            failure_kind = "generic"
    return {
        "schema": "active-failure-signature/v2",
        "seed_ref": str(seed_ref),
        "signature_id": signature_id,
        "fingerprint": defect_state.fingerprint,
        "identity_source": identity_source,
        "kind": failure_kind,
        "expected": defect_state.expected,
        "actual": defect_state.actual,
        "mechanism": defect_state.mechanism,
        "scope": defect_state.scope,
    }


def _structured_failure_signature_from_seed_facts(
    value: Any,
) -> Optional[JsonDict]:
    if not isinstance(value, Mapping):
        return None
    if "failure_signature" not in value:
        return None
    raw = value["failure_signature"]
    if not isinstance(raw, Mapping):
        raise ValueError("structured failure signature must be an object")
    required = {
        "signature_id",
        "exception_family",
        "first_business_frame",
        "assertion_contract",
        "contract_template",
        "observation_values",
        "relevant_symbol",
        "subsystem",
    }
    if any(type(key) is not str for key in raw) or set(raw) != required:
        raise ValueError("structured failure signature has an inexact schema")
    observations = raw.get("observation_values")
    if not isinstance(observations, (list, tuple)) or any(
        type(item) is not str for item in observations
    ):
        raise ValueError("structured failure signature observations are invalid")
    from .evaluation_facts import FailureSignature

    signature = FailureSignature(
        exception_family=str(raw.get("exception_family") or ""),
        first_business_frame=str(raw.get("first_business_frame") or ""),
        assertion_contract=str(raw.get("assertion_contract") or ""),
        contract_template=str(raw.get("contract_template") or ""),
        observation_values=tuple(observations),
        relevant_symbol=str(raw.get("relevant_symbol") or ""),
        subsystem=str(raw.get("subsystem") or ""),
    )
    canonical = signature.to_dict()
    if stable_json(_thaw(raw)) != stable_json(canonical):
        raise ValueError("structured failure signature is not canonical")
    return canonical


def _structured_failure_kind(signature: Mapping[str, Any]) -> str:
    semantic = " ".join(
        str(signature.get(field_name) or "")
        for field_name in (
            "exception_family",
            "assertion_contract",
            "contract_template",
            "relevant_symbol",
            "subsystem",
        )
    ).casefold()
    if "acceptance" in semantic:
        return "acceptance"
    if "verification" in semantic:
        return "verification"
    if any(
        token in semantic
        for token in (
            "connectionerror",
            "networkerror",
            "providererror",
            "timeout",
            "transporterror",
        )
    ):
        return "external"
    return "functional"


def active_failure_causal_role_for(
    *,
    candidate_ref: str,
    component: str,
    event_type: str,
    candidate_phase: str,
    failure_mode: str,
    comparative_role: str,
    candidate_facts: Any,
) -> str:
    if not str(candidate_ref).strip():
        raise ValueError("active failure role requires candidate_ref")
    semantic = " ".join(
        (
            str(component),
            str(event_type),
            str(candidate_phase),
            str(failure_mode),
            stable_json(candidate_facts),
        )
    ).casefold()
    recorded_phase = (
        str(candidate_facts.get("phase") or "").strip().casefold()
        if isinstance(candidate_facts, Mapping)
        else ""
    )
    if recorded_phase in {"closure", "final"} or any(
        token in semantic
        for token in (
            "false closure",
            "close despite",
            "closed despite",
        )
    ):
        return "false_closure"
    if recorded_phase == "verification":
        return "verification_omission"
    if (
        any(token in semantic for token in ("verification", "verify", "test"))
        and any(
            token in semantic
            for token in ("omit", "missing", "skip", "without", "unverified")
        )
    ):
        return "verification_omission"
    if any(
        token in semantic
        for token in ("interrupt", "provider error", "tool.error", "timeout")
    ):
        return "external_interruption"
    if comparative_role == "outcome_evidence":
        return "downstream_materialization"
    if comparative_role in {"contributing_condition", "amplifying_factor"}:
        return "amplifying_condition"
    if comparative_role == "root_candidate" or failure_mode == "positive_introduction":
        return "defect_introduction_root"
    return "downstream_materialization"


def active_failure_factor_role_for(factor_role: str) -> Optional[str]:
    normalized = str(factor_role).strip()
    if normalized in {"contributing_condition", "amplifying_factor"}:
        return "amplifying_condition"
    if normalized == "downstream_materialization":
        return "downstream_materialization"
    if normalized in {"unrelated", "unknown"}:
        return None
    raise ValueError("unsupported independent factor role")


@dataclass(frozen=True)
class ActiveFailureRoleBinding:
    candidate_ref: str
    seed_ref: str
    failure_signature: str
    defect_fingerprint: str
    failure_identity_source: str
    failure_kind: str
    causal_role: str
    disposition: str
    counterfactual_prevention_signatures: Tuple[str, ...] = field(
        default_factory=tuple
    )
    confirmation_owner: str = "independent_confirmation"

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_ref",
            "seed_ref",
            "failure_signature",
            "defect_fingerprint",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(
                    "active failure role binding requires {0}".format(
                        field_name
                    )
                )
        if self.failure_identity_source not in {
            "structured_failure_signature",
            "legacy_defect_state",
        }:
            raise ValueError("unsupported active failure identity source")
        if (
            self.failure_identity_source == "legacy_defect_state"
            and self.failure_signature != self.defect_fingerprint
        ):
            raise ValueError(
                "legacy active failure identity must use the defect fingerprint"
            )
        if self.failure_kind not in ACTIVE_FAILURE_KINDS:
            raise ValueError("unsupported active failure kind")
        if self.causal_role not in ACTIVE_FAILURE_CAUSAL_ROLES:
            raise ValueError("unsupported active failure causal role")
        if self.disposition not in ACTIVE_FAILURE_ROLE_DISPOSITIONS:
            raise ValueError("unsupported active failure role disposition")
        prevention = tuple(
            sorted(set(_frozen_strings(self.counterfactual_prevention_signatures)))
        )
        object.__setattr__(
            self,
            "counterfactual_prevention_signatures",
            prevention,
        )
        if self.confirmation_owner != "independent_confirmation":
            raise ValueError(
                "root publication remains owned by independent confirmation"
            )
        if self.disposition == "factor":
            if prevention:
                raise ValueError(
                    "factor role cannot claim root counterfactual prevention"
                )
            return
        if self.causal_role not in ACTIVE_FAILURE_ROOT_ROLES_BY_KIND[
            self.failure_kind
        ]:
            if self.causal_role == "false_closure":
                raise ValueError(
                    "closure substitution cannot replace a functional introduction root"
                )
            if self.causal_role == "verification_omission":
                raise ValueError(
                    "verification omission substitution cannot replace a functional introduction root"
                )
            raise ValueError(
                "active causal role is not root-eligible for this failure signature"
            )
        if self.failure_signature not in prevention:
            raise ValueError(
                "each root must prevent its active failure signature"
            )

    @classmethod
    def create(
        cls,
        *,
        candidate_ref: str,
        seed_ref: str,
        failure_signature: str,
        defect_fingerprint: str = "",
        failure_identity_source: str = "legacy_defect_state",
        failure_kind: str,
        causal_role: str,
        disposition: str,
        counterfactual_prevention_signatures: Iterable[str] = (),
    ) -> "ActiveFailureRoleBinding":
        return cls(
            candidate_ref=str(candidate_ref),
            seed_ref=str(seed_ref),
            failure_signature=str(failure_signature),
            defect_fingerprint=(
                str(defect_fingerprint) or str(failure_signature)
            ),
            failure_identity_source=str(failure_identity_source),
            failure_kind=str(failure_kind),
            causal_role=str(causal_role),
            disposition=str(disposition),
            counterfactual_prevention_signatures=tuple(
                str(item)
                for item in counterfactual_prevention_signatures
            ),
        )

    def to_dict(self) -> JsonDict:
        payload = {
            "schema": ACTIVE_FAILURE_ROLE_BINDING_SCHEMA,
            "candidate_ref": self.candidate_ref,
            "seed_ref": self.seed_ref,
            "failure_signature": self.failure_signature,
            "defect_fingerprint": self.defect_fingerprint,
            "failure_identity_source": self.failure_identity_source,
            "failure_kind": self.failure_kind,
            "causal_role": self.causal_role,
            "disposition": self.disposition,
            "counterfactual_prevention_signatures": list(
                self.counterfactual_prevention_signatures
            ),
            "confirmation_owner": self.confirmation_owner,
        }
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "ActiveFailureRoleBinding":
        required = {
            "schema",
            "candidate_ref",
            "seed_ref",
            "failure_signature",
            "defect_fingerprint",
            "failure_identity_source",
            "failure_kind",
            "causal_role",
            "disposition",
            "counterfactual_prevention_signatures",
            "confirmation_owner",
        }
        legacy_required = required - {
            "defect_fingerprint",
            "failure_identity_source",
        }
        if not isinstance(value, Mapping) or (
            (
                set(value) != required
                or value.get("schema") != ACTIVE_FAILURE_ROLE_BINDING_SCHEMA
            )
            and (
                set(value) != legacy_required
                or value.get("schema")
                != LEGACY_ACTIVE_FAILURE_ROLE_BINDING_SCHEMA
            )
        ):
            raise ValueError("active failure role binding schema mismatch")
        legacy = value.get("schema") == LEGACY_ACTIVE_FAILURE_ROLE_BINDING_SCHEMA
        prevention = value.get("counterfactual_prevention_signatures")
        if not isinstance(prevention, (list, tuple)) or any(
            type(item) is not str or not item
            for item in prevention
        ):
            raise ValueError(
                "active failure role prevention signatures are invalid"
            )
        result = cls(
            candidate_ref=str(value.get("candidate_ref") or ""),
            seed_ref=str(value.get("seed_ref") or ""),
            failure_signature=str(value.get("failure_signature") or ""),
            defect_fingerprint=str(
                value.get("defect_fingerprint")
                or value.get("failure_signature")
                or ""
            ),
            failure_identity_source=str(
                value.get("failure_identity_source")
                or "legacy_defect_state"
            ),
            failure_kind=str(value.get("failure_kind") or ""),
            causal_role=str(value.get("causal_role") or ""),
            disposition=str(value.get("disposition") or ""),
            counterfactual_prevention_signatures=tuple(prevention),
            confirmation_owner=str(value.get("confirmation_owner") or ""),
        )
        if not legacy and result.to_dict() != _thaw(value):
            raise ValueError(
                "active failure role binding is not canonical"
            )
        return result


def canonical_active_failure_role_request_binding(
    *,
    active_role_binding: Any,
    request_projection: Any,
    disposition: str,
    causal_role: Optional[str] = None,
) -> ActiveFailureRoleBinding:
    """Bind one role verdict to its canonical factual request signature."""
    from .causal_judge import (
        FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA,
        ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA,
        validate_factor_role_request_projection,
        validate_root_confirmation_request_projection,
    )

    if not isinstance(request_projection, Mapping):
        raise ValueError(
            "active failure role binding requires an owning request"
        )
    schema = request_projection.get("schema")
    if schema == FACTOR_ROLE_REQUEST_PROJECTION_SCHEMA:
        canonical_request = validate_factor_role_request_projection(
            request_projection
        )
    elif schema == ROOT_CONFIRMATION_REQUEST_PROJECTION_SCHEMA:
        canonical_request = validate_root_confirmation_request_projection(
            request_projection
        )
    else:
        raise ValueError(
            "active failure role binding has an unknown owning request"
        )
    facts = canonical_request["facts"]
    context = facts.get("factual_context")
    signature = (
        context.get("failure_signature")
        if isinstance(context, Mapping)
        else None
    )
    recursive_path = tuple(facts.get("recursive_path") or ())
    if not recursive_path:
        raise ValueError(
            "active failure role binding requires a canonical request path"
        )
    defect_state = DefectState.from_dict(dict(facts["defect_state"]))
    if not isinstance(signature, Mapping):
        signature = active_failure_signature_for(
            defect_state,
            seed_ref=recursive_path[-1],
        )
    binding = ActiveFailureRoleBinding.from_dict(active_role_binding)
    expected = (
        str(facts["candidate_ref"]),
        str(signature.get("seed_ref") or ""),
        str(signature.get("signature_id") or ""),
        str(signature.get("fingerprint") or ""),
        str(signature.get("identity_source") or ""),
        str(signature.get("kind") or ""),
        disposition,
    )
    actual = (
        binding.candidate_ref,
        binding.seed_ref,
        binding.failure_signature,
        binding.defect_fingerprint,
        binding.failure_identity_source,
        binding.failure_kind,
        binding.disposition,
    )
    if (
        actual != expected
        or signature.get("fingerprint") != defect_state.fingerprint
        or signature.get("seed_ref") != recursive_path[-1]
        or (causal_role is not None and binding.causal_role != causal_role)
        or (
            disposition == "root"
            and binding.failure_signature
            not in binding.counterfactual_prevention_signatures
        )
    ):
        raise ValueError(
            "active failure role binding contradicts its owning request "
            "failure signature"
        )
    return binding


FACTOR_ROLE_CONTRACT = tuple(
    FrozenMapping(row)
    for row in (
        {
            "necessity_status": "necessary",
            "factor_role": "unknown",
            "factor_mechanism": {},
            "predicted_effects": ("prevents_defect",),
        },
        {
            "necessity_status": "necessary",
            "factor_role": "downstream_materialization",
            "factor_mechanism": {
                "mechanism_type": "downstream_materialization"
            },
            "predicted_effects": ("prevents_defect",),
        },
        {
            "necessity_status": "unknown",
            "factor_role": "unknown",
            "factor_mechanism": {},
            "predicted_effects": ("insufficient_grounded_evidence",),
        },
        {
            "necessity_status": "not_necessary",
            "factor_role": "contributing_condition",
            "factor_mechanism": {
                "mechanism_type": "enabling_condition"
            },
            "predicted_effects": (
                "reduces_defect_likelihood",
                "reduces_defect_probability",
            ),
        },
        {
            "necessity_status": "not_necessary",
            "factor_role": "amplifying_factor",
            "factor_mechanism": {"mechanism_type": "amplification"},
            "predicted_effects": (
                "reduces_defect_exposure",
                "reduces_defect_severity",
            ),
        },
        {
            "necessity_status": "not_necessary",
            "factor_role": "downstream_materialization",
            "factor_mechanism": {
                "mechanism_type": "downstream_materialization"
            },
            "predicted_effects": (
                "defect_still_present_without_materialization",
            ),
        },
        {
            "necessity_status": "not_necessary",
            "factor_role": "unrelated",
            "factor_mechanism": {},
            "predicted_effects": (
                "no_grounded_causal_influence_established",
            ),
        },
    )
)
FACTOR_NECESSITY_STATUSES = frozenset(
    row["necessity_status"] for row in FACTOR_ROLE_CONTRACT
)
FACTOR_ROLES = frozenset(
    row["factor_role"] for row in FACTOR_ROLE_CONTRACT
)


def factor_role_contract_entry(
    necessity_status: Any,
    factor_role: Any,
) -> Optional[FrozenMapping]:
    return next(
        (
            row
            for row in FACTOR_ROLE_CONTRACT
            if row["necessity_status"] == necessity_status
            and row["factor_role"] == factor_role
        ),
        None,
    )


def _frozen_strings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _concrete_seed_strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            "seed {0} must be a list or tuple".format(field_name)
        )
    entries = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in entries):
        raise ValueError(
            "seed {0} entries must be non-empty strings".format(field_name)
        )
    return entries


def _seed_json_string_list(value: Any, field_name: str) -> List[Any]:
    if not isinstance(value, list):
        raise ValueError(
            "seed {0} JSON payload must be an array".format(field_name)
        )
    return value


def seed_binding_identity_for(start_ref: str, defect_fingerprint: str) -> str:
    semantic = {
        "start_ref": str(start_ref),
        "defect_fingerprint": str(defect_fingerprint),
    }
    return "seed:{0}".format(
        hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()[:24]
    )


def confirmation_identity_for(
    *,
    hypothesis_id: str,
    hypothesis_semantic_hash: str,
    candidate_ref: str,
    defect_fingerprint: str,
    recursive_path: Tuple[str, ...],
    seed_binding_identity: str = "",
) -> str:
    semantic = {
        "hypothesis_id": hypothesis_id,
        "hypothesis_semantic_hash": hypothesis_semantic_hash,
        "candidate_ref": candidate_ref,
        "defect_fingerprint": defect_fingerprint,
        "recursive_path": list(recursive_path),
    }
    if seed_binding_identity:
        semantic["seed_binding_identity"] = seed_binding_identity
    return "confirmation:{0}".format(
        hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()[:24]
    )


def confirmation_response_identity_for(
    *,
    confirmation_identity: str,
    status: str,
    excerpt: str,
    reason: str,
    counterfactual: str,
    confidence: float,
    evidence_refs: Tuple[str, ...],
    counterfactual_status: str,
    factor_role: str,
    competitor_comparisons: Tuple[JsonDict, ...],
    factor_mechanism: JsonDict,
    analysis_perspective: str = "",
    process_confirmation_assessment: Optional[Mapping[str, Any]] = None,
) -> str:
    semantic = {
        "confirmation_identity": confirmation_identity,
        "status": status,
        "excerpt": excerpt,
        "reason": reason,
        "counterfactual": counterfactual,
        "confidence": confidence,
        "evidence_refs": list(evidence_refs),
        "counterfactual_status": counterfactual_status,
        "factor_role": factor_role,
        "competitor_comparisons": [
            _thaw(item) for item in competitor_comparisons
        ],
        "factor_mechanism": _thaw(factor_mechanism),
        "analysis_perspective": analysis_perspective,
    }
    if process_confirmation_assessment:
        semantic["process_confirmation_assessment"] = _thaw(
            process_confirmation_assessment
        )
    return "response:{0}".format(
        hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()[:24]
    )


def confirmation_counterfactual_for(
    candidate_ref: str,
    status: str,
    *,
    counterfactual_status: str = "",
) -> str:
    resolved_counterfactual_status = counterfactual_status or {
        "confirmed": "supports_causality",
        "rejected": "rejects_causality",
        "unknown": "unknown",
    }.get(status, "")
    prediction = {
        ("confirmed", "supports_causality"): (
            "absent",
            "prevents_defect",
        ),
        ("rejected", "rejects_causality"): (
            "present",
            "does_not_prevent_defect",
        ),
        ("rejected", "unknown"): ("unknown", "unknown"),
        ("unknown", "unknown"): ("unknown", "unknown"),
    }.get((status, resolved_counterfactual_status))
    if prediction is None or not str(candidate_ref):
        raise ValueError(
            "unsupported root confirmation counterfactual combination: "
            "status={0}, counterfactual_status={1}".format(
                status,
                resolved_counterfactual_status,
            )
        )
    return stable_json(
        {
            "schema": ROOT_CONFIRMATION_COUNTERFACTUAL_SCHEMA,
            "intervention_ref": str(candidate_ref),
            "intervention_kind": (
                "replace_with_semantically_correct_behavior"
            ),
            "predicted_defect_status": prediction[0],
            "causal_effect": prediction[1],
        }
    )


def validate_root_confirmation_counterfactual(
    confirmation: Any,
) -> str:
    expected = confirmation_counterfactual_for(
        confirmation.candidate_ref,
        confirmation.status,
        counterfactual_status=confirmation.counterfactual_status,
    )
    raw = str(confirmation.counterfactual or "")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "root confirmation counterfactual must be canonical structured "
            "facts"
        ) from exc
    if (
        not isinstance(parsed, Mapping)
        or set(parsed)
        != {
            "schema",
            "intervention_ref",
            "intervention_kind",
            "predicted_defect_status",
            "causal_effect",
        }
        or stable_json(parsed) != raw
        or raw != expected
    ):
        raise ValueError(
            "root confirmation counterfactual contradicts candidate, "
            "prediction, causal effect, status, or canonical encoding"
        )
    return raw


def _has_blocking_metadata(metadata: Mapping[str, Any]) -> bool:
    for raw_key, value in metadata.items():
        if not value:
            continue
        key = str(raw_key).strip().lower()
        if key in BLOCKING_METADATA_KEYS:
            return True
        if key.endswith("_budget_exhausted") or key.endswith("_blocked"):
            return True
        if key.startswith("missing_") or key.endswith("_missing"):
            return True
    return False


def _assessment_is_unresolved(assessment: "PredecessorAssessment") -> bool:
    return assessment.relation == "unknown" or bool(assessment.missing_evidence)


def _has_unresolved_judgment_state(
    step_judgments: Tuple["CausalStepJudgment", ...],
    causal_relations: Tuple["PredecessorAssessment", ...],
) -> bool:
    latest_steps = {}
    for judgment in step_judgments:
        latest_steps[judgment.current_node_ref] = judgment
    latest_nested_assessments = {}
    for judgment in latest_steps.values():
        if judgment.current_defect_status == "unknown" or judgment.missing_evidence:
            return True
        if judgment.current_defect_status == "absent":
            continue
        for assessment in judgment.predecessors:
            latest_nested_assessments[(judgment.current_node_ref, assessment.ref)] = assessment
    if any(_assessment_is_unresolved(item) for item in latest_nested_assessments.values()):
        return True

    latest_relations = {}
    for assessment in causal_relations:
        latest_relations[assessment.ref] = assessment
    return any(_assessment_is_unresolved(item) for item in latest_relations.values())


def _hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _json_dict(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite_float(value: Any, field_name: str, *, unit_interval: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("{0} must be a finite number".format(field_name))
    if not math.isfinite(result):
        raise ValueError("{0} must be finite".format(field_name))
    if unit_interval and not 0.0 <= result <= 1.0:
        raise ValueError("{0} must be between 0 and 1".format(field_name))
    return result


@dataclass(frozen=True)
class LocalStateOwner:
    seed_binding_identity: str
    hypothesis_id: str
    visit_key: str
    occurrence_identity: str

    def __post_init__(self) -> None:
        for name in (
            "seed_binding_identity",
            "hypothesis_id",
            "visit_key",
            "occurrence_identity",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("local state owner {0} is required".format(name))
        if not re.fullmatch(
            r"{0}[0-9a-f]{{64}}".format(re.escape(LOCAL_STATE_OCCURRENCE_PREFIX)),
            self.occurrence_identity,
        ):
            raise ValueError("local state owner occurrence_identity is invalid")

    @classmethod
    def create(
        cls,
        *,
        seed_binding_identity: str,
        hypothesis_id: str,
        visit_key: str,
        occurrence_key: str,
    ) -> "LocalStateOwner":
        semantic = {
            "seed_binding_identity": str(seed_binding_identity),
            "hypothesis_id": str(hypothesis_id),
            "visit_key": str(visit_key),
            "occurrence_key": str(occurrence_key),
        }
        return cls(
            seed_binding_identity=semantic["seed_binding_identity"],
            hypothesis_id=semantic["hypothesis_id"],
            visit_key=semantic["visit_key"],
            occurrence_identity="{0}{1}".format(
                LOCAL_STATE_OCCURRENCE_PREFIX,
                hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest(),
            ),
        )

    def to_dict(self) -> JsonDict:
        payload = {
            "seed_binding_identity": self.seed_binding_identity,
            "hypothesis_id": self.hypothesis_id,
            "visit_key": self.visit_key,
            "occurrence_identity": self.occurrence_identity,
        }
        return payload

    @classmethod
    def from_dict(cls, value: Any) -> "LocalStateOwner":
        if not isinstance(value, Mapping):
            raise ValueError("local state owner must be an object")
        required = {
            "seed_binding_identity",
            "hypothesis_id",
            "visit_key",
            "occurrence_identity",
        }
        if set(value) != required:
            raise ValueError("local state owner schema mismatch")
        if any(not isinstance(value[field], str) for field in required):
            raise ValueError("local state owner fields must be strings")
        return cls(
            seed_binding_identity=value["seed_binding_identity"],
            hypothesis_id=value["hypothesis_id"],
            visit_key=value["visit_key"],
            occurrence_identity=value["occurrence_identity"],
        )


def _confidence(value: Any) -> float:
    return _finite_float(value, "confidence", unit_interval=True)


def _score(value: Any) -> float:
    return _finite_float(value, "score", unit_interval=True)


def _priority(value: Any) -> float:
    return _finite_float(value, "priority")


def _trace_node_to_dict(node: TraceNode) -> JsonDict:
    return {
        "ref": node.ref,
        "record_id": node.record_id,
        "component": node.component,
        "event_type": node.event_type,
        "title": node.title,
        "status": node.status,
        "timestamp": node.timestamp,
        "data": _thaw(node.data),
        "source_refs": list(node.source_refs),
    }


def _trace_node_from_dict(value: JsonDict) -> TraceNode:
    return TraceNode(
        ref=str(value.get("ref") or ""),
        record_id=str(value.get("record_id") or ""),
        component=str(value.get("component") or ""),
        event_type=str(value.get("event_type") or ""),
        title=str(value.get("title") or ""),
        status=str(value.get("status") or ""),
        timestamp=str(value.get("timestamp") or ""),
        data=_json_dict(value.get("data")),
        source_refs=_string_list(value.get("source_refs")),
    )


def _freeze_trace_node(node: TraceNode) -> TraceNode:
    return TraceNode(
        ref=node.ref,
        record_id=node.record_id,
        component=node.component,
        event_type=node.event_type,
        title=node.title,
        status=node.status,
        timestamp=node.timestamp,
        data=FrozenMapping(_thaw(node.data)),
        source_refs=_frozen_strings(node.source_refs),
    )


def _anchor_semantic_role(node: TraceNode) -> str:
    event = node.event_type.strip().lower()
    if "observed_defect" in event or "quality_gap" in event:
        return "observed_quality_outcome"
    if "decision" in event or "reasoning" in event:
        return "authored_decision"
    if "compaction" in event or "context" in event:
        return "context_transformation"
    if "verification" in event or "test" in event:
        return "verification_evidence"
    if "change" in event or "edit" in event:
        return "repository_change"
    if "interruption" in event or "timeout" in event:
        return "execution_interruption"
    if "prompt" in event or "message.input" in event:
        return "task_input"
    return event or "unknown_event"


def _anchor_normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _anchor_identifier(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _is_windows_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def _within_posix_root(path: str, root: str) -> bool:
    try:
        return posixpath.commonpath((path, root)) == root
    except ValueError:
        return False


def _anchor_path(value: str, roots: Tuple[str, ...]) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    windows = _is_windows_path(normalized)
    if windows:
        canonical = ntpath.normpath(normalized).replace("\\", "/")
        escaped: List[str] = []
        for root in roots:
            root_value = unicodedata.normalize("NFKC", root)
            if not _is_windows_path(root_value):
                continue
            canonical_root = ntpath.normpath(root_value).replace("\\", "/").rstrip("/")
            try:
                relative = ntpath.relpath(canonical, canonical_root).replace("\\", "/")
            except ValueError:
                continue
            if relative == ".":
                relative = ""
            if relative == ".." or relative.startswith("../"):
                escaped.append(relative.casefold())
                continue
            return "repo-relative:windows:" + relative.casefold()
        if escaped:
            return "unresolved-path:windows-repo-root-escape:" + sorted(
                escaped, key=lambda item: (item.count("/"), len(item), item)
            )[0]
        return "absolute-windows:" + canonical.casefold()

    normalized = normalized.replace("\\", "/")
    canonical = posixpath.normpath(normalized)
    escaped = []
    for root in roots:
        root_value = unicodedata.normalize("NFKC", root).replace("\\", "/")
        if not root_value.startswith("/"):
            continue
        canonical_root = posixpath.normpath(root_value)
        resolved = canonical if canonical.startswith("/") else posixpath.normpath(
            posixpath.join(canonical_root, canonical)
        )
        relative = posixpath.relpath(resolved, canonical_root)
        if not _within_posix_root(resolved, canonical_root):
            escaped.append(relative)
            continue
        return "repo-relative:posix:" + ("" if relative == "." else relative)
    if escaped:
        return "unresolved-path:posix-repo-root-escape:" + sorted(
            escaped, key=lambda item: (item.count("/"), len(item), item)
        )[0]
    if canonical.startswith("/"):
        return "absolute-posix:" + canonical
    if canonical in ("", "."):
        return "unresolved-path:<empty>"
    if canonical == ".." or canonical.startswith("../"):
        return "unresolved-path:relative-escape:" + canonical
    return "repo-relative:posix:" + canonical


def _anchor_text(value: str, roots: Tuple[str, ...]) -> str:
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    for root in sorted((item for item in roots if item), key=len, reverse=True):
        text = text.replace(root, "<repo>")
        text = text.replace(root.replace("\\", "/"), "<repo>")
    text = _ISO_TIMESTAMP_PATTERN.sub("<timestamp>", text)
    text = _UUID_PATTERN.sub("<runtime-id>", text)
    text = _RUNTIME_ID_PATTERN.sub("<runtime-id>", text)
    text = _URL_PORT_PATTERN.sub(lambda match: match.group("host") + ":<port>", text)
    return text.casefold()


def _anchor_value(value: Any, *, key: str, roots: Tuple[str, ...]) -> Any:
    normalized_key = unicodedata.normalize("NFKC", key.strip())
    lookup_key = normalized_key.casefold()
    if isinstance(value, Mapping):
        output = {}
        for child_key in sorted(value, key=lambda item: unicodedata.normalize("NFKC", str(item))):
            child_name = str(child_key)
            normalized_child_name = unicodedata.normalize("NFKC", child_name.strip())
            child_lookup = normalized_child_name.casefold()
            if child_lookup in _ANCHOR_VOLATILE_KEYS:
                continue
            if "provider" in child_lookup and ("id" in child_lookup or "request" in child_lookup):
                continue
            child_value = value[child_key]
            normalized = _anchor_value(child_value, key=child_name, roots=roots)
            if normalized not in (None, "", [], {}):
                output[normalized_child_name] = normalized
        return output
    if isinstance(value, (list, tuple)):
        normalized = [
            _anchor_value(item, key=key, roots=roots) for item in value
        ]
        values = [item for item in normalized if item not in (None, "", [], {})]
        if lookup_key in _ANCHOR_SET_LIKE_KEYS:
            return sorted(values, key=stable_json)
        return values
    if isinstance(value, str):
        if lookup_key in _ANCHOR_PATH_KEYS:
            return _anchor_path(value, roots)
        if lookup_key in _ANCHOR_ARTIFACT_HASH_KEYS:
            return None
        if lookup_key in _ANCHOR_IDENTIFIER_KEYS or lookup_key.endswith("_identifier"):
            return _anchor_identifier(value)
        return _anchor_text(value, roots)
    return value


def _anchor_artifact_hashes(value: Any) -> List[str]:
    found: Set[str] = set()

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key).strip().casefold())
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child, key)
        elif isinstance(item, str) and key in _ANCHOR_ARTIFACT_HASH_KEYS:
            normalized = unicodedata.normalize("NFKC", item).strip().casefold()
            if normalized:
                found.add(normalized)

    visit(value)
    return sorted(found)


def normalized_anchor_semantics(node: TraceNode) -> JsonDict:
    roots = tuple(
        str(node.data.get(key) or "")
        for key in (
            "repository_root",
            "repo_root",
            "workspace_root",
            "cwd",
            "workdir",
            "working_directory",
        )
        if node.data.get(key)
    )
    data = _anchor_value(node.data, key="data", roots=roots)
    result = {
        "title": _anchor_text(node.title, roots) if node.title else "",
        "status": node.status.strip().lower(),
        "data": data,
    }
    artifact_hashes = _anchor_artifact_hashes(node.data)
    if artifact_hashes:
        result["artifact_hashes"] = artifact_hashes
    return result


def semantic_anchor_id(
    case_id: str,
    node: TraceNode,
) -> str:
    """Return content semantics independent of graph occurrence count or position."""

    identity = {
        "schema_version": SEMANTIC_ANCHOR_SCHEMA_VERSION,
        "case_id": _anchor_identifier(str(case_id)),
        "event_type": _anchor_identifier(node.event_type.strip()),
        "semantic_role": _anchor_semantic_role(node),
        "normalized_semantics": normalized_anchor_semantics(node),
    }
    digest = hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()[:24]
    return SEMANTIC_ANCHOR_PREFIX + digest


def semantic_anchor_index(case_id: str, graph_or_nodes: Any) -> Dict[str, str]:
    """Build content-only anchors without collision-conditioned identity changes."""

    nodes = graph_or_nodes.nodes if hasattr(graph_or_nodes, "nodes") else graph_or_nodes
    return {ref: semantic_anchor_id(case_id, node) for ref, node in nodes.items()}


def semantic_occurrence_id(
    case_id: str,
    semantic_anchor: str,
    causal_neighborhood: Mapping[str, Any],
) -> str:
    """Return a versioned causal occurrence identity distinct from content semantics."""

    identity = {
        "schema_version": SEMANTIC_OCCURRENCE_SCHEMA_VERSION,
        "case_id": _anchor_identifier(str(case_id)),
        "semantic_anchor_id": semantic_anchor,
        "causal_neighborhood": dict(causal_neighborhood),
    }
    digest = hashlib.sha256(stable_json(identity).encode("utf-8")).hexdigest()[:24]
    return SEMANTIC_OCCURRENCE_PREFIX + digest


def semantic_occurrence_index(case_id: str, graph_or_nodes: Any) -> Dict[str, str]:
    """Build occurrence identities for every node from relation-aware causal context."""

    graph = graph_or_nodes if hasattr(graph_or_nodes, "nodes") else None
    nodes = graph.nodes if graph is not None else graph_or_nodes
    anchors = semantic_anchor_index(case_id, nodes)

    def related(ref: str, *, upstream: bool) -> List[JsonDict]:
        node = nodes[ref]
        refs = (
            graph.upstream_refs(ref)
            if graph is not None and upstream
            else graph.downstream_refs(ref)
            if graph is not None
            else list(node.source_refs)
            if upstream
            else []
        )
        entries: List[JsonDict] = []
        for related_ref in refs:
            if related_ref not in anchors:
                continue
            edges = (
                graph.edge_context(related_ref, ref)
                if graph is not None and upstream
                else graph.edge_context(ref, related_ref)
                if graph is not None
                else []
            )
            edge_semantics = [
                {
                    "relation": str(edge.get("relation") or ""),
                    "evidence_type": str(edge.get("evidence_type") or ""),
                    "eligible_for_attribution": edge.get("eligible_for_attribution") is True,
                    "inference_method": str(edge.get("inference_method") or ""),
                    "edge_origin": str(edge.get("edge_origin") or ""),
                }
                for edge in edges
            ]
            entries.append(
                {
                    "semantic_anchor_id": anchors[related_ref],
                    "edges": sorted(edge_semantics, key=stable_json),
                }
            )
        return sorted(entries, key=stable_json)

    return {
        ref: semantic_occurrence_id(
            case_id,
            anchors[ref],
            {
                "upstream": related(ref, upstream=True),
                "downstream": related(ref, upstream=False),
            },
        )
        for ref in nodes
    }


def annotate_report_semantic_anchors(
    case_id: str,
    nodes: Mapping[str, TraceNode],
    report: Mapping[str, Any],
    *,
    graph: Any = None,
) -> JsonDict:
    """Project stable anchors into a report without mutating the report or Trace nodes."""

    projected = copy.deepcopy(dict(report))
    anchors_by_ref = semantic_anchor_index(case_id, graph or nodes)
    occurrences_by_ref = semantic_occurrence_index(case_id, graph or nodes)
    refs_by_anchor: Dict[str, Set[str]] = {}
    refs_by_occurrence: Dict[str, Set[str]] = {}
    for ref, anchor in anchors_by_ref.items():
        refs_by_anchor.setdefault(anchor, set()).add(ref)
        refs_by_occurrence.setdefault(occurrences_by_ref[ref], set()).add(ref)

    def anchor_for(ref: Any) -> str:
        value = str(ref or "")
        node = nodes.get(value)
        if node is None:
            return ""
        return anchors_by_ref[value]

    def annotate(section: str, ref_key: str) -> None:
        values = projected.get(section)
        if not isinstance(values, list):
            return
        for item in values:
            if not isinstance(item, dict):
                continue
            anchor = anchor_for(item.get(ref_key))
            if anchor:
                item["semantic_anchor_id"] = anchor
                item["semantic_occurrence_id"] = occurrences_by_ref[str(item.get(ref_key))]
                embedded = item.get("confirmation")
                if (
                    isinstance(embedded, dict)
                    and "confirmation_identity" in embedded
                ):
                    embedded["semantic_anchor_id"] = anchor
                    embedded["semantic_occurrence_id"] = occurrences_by_ref[str(item.get(ref_key))]

    for section, ref_key in (
        ("causal_candidates", "ref"),
        ("introduction_candidates", "ref"),
        ("confirmed_roots", "node_ref"),
        ("co_roots", "node_ref"),
        ("contributing_conditions", "node_ref"),
        ("amplifying_factors", "node_ref"),
        ("downstream_materializations", "candidate_ref"),
        ("rejected_candidates", "node_ref"),
        ("root_causes", "node_ref"),
        ("confirmations", "candidate_ref"),
        ("step_judgments", "current_node_ref"),
        ("causal_relations", "ref"),
    ):
        annotate(section, ref_key)

    metadata = projected.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        projected["metadata"] = metadata
    metadata["semantic_anchor_schema_version"] = SEMANTIC_ANCHOR_SCHEMA_VERSION
    metadata["semantic_occurrence_schema_version"] = SEMANTIC_OCCURRENCE_SCHEMA_VERSION
    metadata["semantic_anchor_index"] = {
        ref: anchor for ref, anchor in sorted(anchors_by_ref.items())
    }
    metadata["semantic_anchor_collisions"] = [
        {"semantic_anchor_id": anchor, "node_refs": sorted(refs)}
        for anchor, refs in sorted(refs_by_anchor.items())
        if len(refs) > 1
    ]
    metadata["semantic_occurrence_index"] = {
        ref: occurrence for ref, occurrence in sorted(occurrences_by_ref.items())
    }
    metadata["semantic_occurrence_collisions"] = [
        {"semantic_occurrence_id": occurrence, "node_refs": sorted(refs)}
        for occurrence, refs in sorted(refs_by_occurrence.items())
        if len(refs) > 1
    ]
    return projected


@dataclass(frozen=True)
class DefectState:
    defect_state_id: str
    label: str
    expected: str
    actual: str
    mechanism: str
    scope: str
    fingerprint: str
    derived_from_defect_state_id: str = ""
    transformation_reason: str = ""

    @classmethod
    def create(
        cls,
        label: str,
        expected: str,
        actual: str,
        mechanism: str,
        scope: str,
        *,
        derived_from_defect_state_id: str = "",
        transformation_reason: str = "",
    ) -> "DefectState":
        semantic = {
            "label": label,
            "expected": expected,
            "actual": actual,
            "mechanism": mechanism,
            "scope": scope,
            "derived_from_defect_state_id": derived_from_defect_state_id,
            "transformation_reason": transformation_reason,
        }
        fingerprint = _hash(semantic)[:20]
        return cls(defect_state_id="defect:{0}".format(fingerprint), fingerprint=fingerprint, **semantic)

    def transformed(
        self,
        *,
        label: str,
        mechanism: str,
        transformation_reason: str,
        expected: Optional[str] = None,
        actual: Optional[str] = None,
        scope: Optional[str] = None,
    ) -> "DefectState":
        return self.create(
            label=label,
            expected=self.expected if expected is None else expected,
            actual=self.actual if actual is None else actual,
            mechanism=mechanism,
            scope=self.scope if scope is None else scope,
            derived_from_defect_state_id=self.defect_state_id,
            transformation_reason=transformation_reason,
        )

    def to_dict(self) -> JsonDict:
        return {
            "defect_state_id": self.defect_state_id,
            "label": self.label,
            "expected": self.expected,
            "actual": self.actual,
            "mechanism": self.mechanism,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
            "derived_from_defect_state_id": self.derived_from_defect_state_id,
            "transformation_reason": self.transformation_reason,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "DefectState":
        state = cls.create(
            label=str(value.get("label") or ""),
            expected=str(value.get("expected") or ""),
            actual=str(value.get("actual") or ""),
            mechanism=str(value.get("mechanism") or ""),
            scope=str(value.get("scope") or ""),
            derived_from_defect_state_id=str(value.get("derived_from_defect_state_id") or ""),
            transformation_reason=str(value.get("transformation_reason") or ""),
        )
        fingerprint = str(value.get("fingerprint") or "")
        if fingerprint and fingerprint != state.fingerprint:
            raise ValueError("DefectState fingerprint does not match semantic fields")
        defect_state_id = str(value.get("defect_state_id") or "")
        if defect_state_id and defect_state_id != state.defect_state_id:
            raise ValueError("DefectState defect_state_id does not match semantic fields")
        return state


def _seed_semantic_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    return stable_json(value)


def _first_seed_semantic_value(
    data: Mapping[str, Any],
    keys: Tuple[str, ...],
    fallback: str,
) -> str:
    for key in keys:
        value = _seed_semantic_text(data.get(key))
        if value:
            return value
    return fallback


def seed_defect_state(node: TraceNode, objective: str) -> DefectState:
    data = node.data
    if node.event_type == "external.evaluation_fact":
        status = _first_seed_semantic_value(
            data, ("status",), node.status or "unknown"
        ).lower()
        return DefectState.create(
            label="external_evaluation_{0}".format(status),
            expected=_first_seed_semantic_value(
                data,
                ("assertion",),
                "The externally evaluated behavior satisfies its assertion.",
            ),
            actual=_first_seed_semantic_value(
                data,
                ("observation",),
                "The external evaluator did not record an observation.",
            ),
            mechanism="External evaluation status: {0}.".format(status),
            scope=_first_seed_semantic_value(
                data, ("scope",), "external_evaluation"
            ),
        )
    if node.event_type == "response.claim":
        claim = _first_seed_semantic_value(
            data,
            ("text", "claim", "summary", "description"),
            "The final response contains an ungrounded claim candidate.",
        )
        return DefectState.create(
            label="unsupported_response_claim",
            expected=(
                objective
                or "The final response claim is fully grounded, temporally valid, and not contradicted."
            ),
            actual=claim,
            mechanism=(
                "The claim may be unsupported, contradicted, incomplete, or fully valid; "
                "defect presence is unconfirmed until evidence comparison."
            ),
            scope="response_quality",
        )
    label = _first_seed_semantic_value(
        data,
        ("failure_type", "gap_kind", "dimension", "issue_kind", "defect_type"),
        "observed_defect",
    )
    summary = _first_seed_semantic_value(
        data, ("summary", "description", "reason", "text"), label
    )
    return DefectState.create(
        label=label,
        expected=_first_seed_semantic_value(
            data,
            ("expected", "expected_behavior", "requirement", "criterion"),
            objective,
        ),
        actual=_first_seed_semantic_value(
            data,
            ("actual", "actual_behavior", "observed", "result"),
            summary,
        ),
        mechanism=_first_seed_semantic_value(
            data,
            ("mechanism", "failure_mechanism", "cause", "reason"),
            summary,
        ),
        scope=_first_seed_semantic_value(
            data,
            ("scope", "attribution_domain", "component", "dimension"),
            node.component or node.event_type or "task_quality",
        ),
    )


def semantic_visit_key(
    node_ref: str,
    defect_state: DefectState,
    hypothesis_semantic_hash: str,
    seed_binding_identity: str = "",
) -> str:
    return _hash(
        {
            "node_ref": node_ref,
            "defect_fingerprint": defect_state.fingerprint,
            "hypothesis_semantic_hash": hypothesis_semantic_hash,
            "seed_binding_identity": seed_binding_identity,
        }
    )


def _legacy_semantic_visit_key(
    node_ref: str,
    defect_state: DefectState,
    hypothesis_semantic_hash: str,
) -> str:
    return _hash(
        {
            "node_ref": node_ref,
            "defect_fingerprint": defect_state.fingerprint,
            "hypothesis_semantic_hash": hypothesis_semantic_hash,
        }
    )


def _validate_candidate_compression_funnels(value: Any) -> None:
    """Validate only the report-owned candidate_compression projection."""
    entries = value if isinstance(value, (list, tuple)) else (value,)
    for entry in entries:
        if not isinstance(entry, Mapping) or "candidate_funnel" not in entry:
            continue
        # Lazy import avoids the evidence-capsule -> causal-state cycle.
        from .evidence_capsule import validate_candidate_funnel

        validate_candidate_funnel(entry["candidate_funnel"])


def _validate_system_candidate_funnels(
    metadata: Mapping[str, Any], investigation_journal: Iterable[Any]
) -> None:
    _validate_candidate_compression_funnels(metadata.get("candidate_compression"))
    for entry in investigation_journal:
        if (
            isinstance(entry, Mapping)
            and entry.get("kind")
            in {"global_candidate_pass", "global_candidate_gate"}
        ):
            _validate_candidate_compression_funnels(
                entry.get("candidate_compression")
            )


def _validate_candidate_cluster_shadow_events(
    investigation_journal: Iterable[Any],
    *,
    valid_seed_bindings: Iterable[str] = (),
) -> None:
    from .candidate_clustering import (
        validate_candidate_cluster_shadow_event,
    )

    allowed = frozenset(valid_seed_bindings)
    for entry in investigation_journal:
        if (
            not isinstance(entry, Mapping)
            or entry.get("kind")
            != "candidate_cluster_manifest_shadow"
        ):
            continue
        seed_binding_identity = str(
            entry.get("seed_binding_identity") or ""
        )
        if allowed and seed_binding_identity not in allowed:
            raise ValueError(
                "candidate cluster shadow event has no report seed"
            )
        validate_candidate_cluster_shadow_event(
            _thaw(entry),
            expected_seed_binding_identity=seed_binding_identity,
        )


@dataclass(frozen=True)
class CausalCandidate:
    ref: str
    node: TraceNode
    source: str
    edge: JsonDict = field(default_factory=FrozenMapping)
    score: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "score", _score(self.score))
        object.__setattr__(self, "node", _freeze_trace_node(self.node))
        object.__setattr__(self, "edge", FrozenMapping(_thaw(self.edge)))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))

    def to_dict(self) -> JsonDict:
        return {
            "ref": self.ref,
            "node": _trace_node_to_dict(self.node),
            "source": self.source,
            "edge": _thaw(self.edge),
            "score": self.score,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalCandidate":
        return cls(
            ref=str(value.get("ref") or ""),
            node=_trace_node_from_dict(_json_dict(value.get("node"))),
            source=str(value.get("source") or ""),
            edge=_json_dict(value.get("edge")),
            score=_score(value.get("score", 0.0)),
            evidence_refs=_string_list(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class PredecessorAssessment:
    ref: str
    relation: str = "unknown"
    reason: str = ""
    confidence: float = 0.0
    recurse: bool = False
    upstream_defect: Optional[DefectState] = None
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)
    owner: Optional[LocalStateOwner] = None

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "missing_evidence", _frozen_strings(self.missing_evidence))
        if self.owner is not None and not isinstance(self.owner, LocalStateOwner):
            raise TypeError("predecessor assessment owner must be LocalStateOwner")

    def to_dict(self) -> JsonDict:
        value = {
            "ref": self.ref,
            "relation": self.relation,
            "reason": self.reason,
            "confidence": self.confidence,
            "recurse": self.recurse,
            "upstream_defect": self.upstream_defect.to_dict() if self.upstream_defect else None,
            "evidence_refs": list(self.evidence_refs),
            "missing_evidence": list(self.missing_evidence),
        }
        if self.owner is not None:
            value["owner"] = self.owner.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: JsonDict) -> "PredecessorAssessment":
        upstream = value.get("upstream_defect")
        return cls(
            ref=str(value.get("ref") or ""),
            relation=str(value.get("relation") or "unknown"),
            reason=str(value.get("reason") or ""),
            confidence=_confidence(value.get("confidence", 0.0)),
            recurse=bool(value.get("recurse")),
            upstream_defect=DefectState.from_dict(upstream) if isinstance(upstream, dict) else None,
            evidence_refs=_string_list(value.get("evidence_refs")),
            missing_evidence=_string_list(value.get("missing_evidence")),
            owner=(
                LocalStateOwner.from_dict(value.get("owner"))
                if value.get("owner") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CausalStepJudgment:
    current_node_ref: str
    current_defect_status: str
    current_defect_reason: str
    predecessors: Tuple[PredecessorAssessment, ...] = field(default_factory=tuple)
    candidate_introduction: bool = False
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)
    suggested_investigation: Optional[JsonDict] = None
    unselected_predecessor_refs: Tuple[str, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    owner: Optional[LocalStateOwner] = None
    process_assessment: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "predecessors", tuple(self.predecessors))
        object.__setattr__(self, "missing_evidence", _frozen_strings(self.missing_evidence))
        object.__setattr__(
            self,
            "unselected_predecessor_refs",
            _frozen_strings(self.unselected_predecessor_refs),
        )
        if self.suggested_investigation is not None:
            object.__setattr__(
                self, "suggested_investigation", FrozenMapping(_thaw(self.suggested_investigation))
            )
        if self.owner is not None and not isinstance(self.owner, LocalStateOwner):
            raise TypeError("causal step judgment owner must be LocalStateOwner")
        object.__setattr__(
            self,
            "process_assessment",
            FrozenMapping(_thaw(self.process_assessment)),
        )

    def to_dict(self) -> JsonDict:
        value = {
            "current_node_ref": self.current_node_ref,
            "current_defect_status": self.current_defect_status,
            "current_defect_reason": self.current_defect_reason,
            "predecessors": [item.to_dict() for item in self.predecessors],
            "candidate_introduction": self.candidate_introduction,
            "missing_evidence": list(self.missing_evidence),
            "suggested_investigation": _thaw(self.suggested_investigation)
            if self.suggested_investigation
            else None,
            "unselected_predecessor_refs": list(self.unselected_predecessor_refs),
            "confidence": self.confidence,
        }
        if self.owner is not None:
            value["owner"] = self.owner.to_dict()
        if self.process_assessment:
            value["process_assessment"] = _thaw(self.process_assessment)
        return value

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalStepJudgment":
        predecessors = value.get("predecessors")
        return cls(
            current_node_ref=str(value.get("current_node_ref") or ""),
            current_defect_status=str(value.get("current_defect_status") or "unknown"),
            current_defect_reason=str(value.get("current_defect_reason") or ""),
            predecessors=[PredecessorAssessment.from_dict(item) for item in predecessors if isinstance(item, dict)]
            if isinstance(predecessors, list)
            else [],
            candidate_introduction=bool(value.get("candidate_introduction")),
            missing_evidence=_string_list(value.get("missing_evidence")),
            suggested_investigation=_json_dict(value.get("suggested_investigation"))
            if isinstance(value.get("suggested_investigation"), dict)
            else None,
            unselected_predecessor_refs=_string_list(
                value.get("unselected_predecessor_refs")
            ),
            confidence=_confidence(value.get("confidence", 0.0)),
            owner=(
                LocalStateOwner.from_dict(value.get("owner"))
                if value.get("owner") is not None
                else None
            ),
            process_assessment=(
                _json_dict(value.get("process_assessment"))
                if isinstance(value.get("process_assessment"), dict)
                else {}
            ),
        )


@dataclass(frozen=True)
class FrontierItem:
    item_id: str
    node_ref: str
    defect_state: DefectState
    downstream_path: Tuple[str, ...]
    hypothesis_id: str
    hypothesis_semantic_hash: str
    seed_binding_identity: str = ""
    depth: int = 0
    candidate_source: str = ""
    priority: float = 0.0
    checked_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    evidence_hash: str = ""
    reopen_reason: str = ""
    graph_position: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "priority", _priority(self.priority))
        object.__setattr__(self, "downstream_path", _frozen_strings(self.downstream_path))
        object.__setattr__(self, "checked_evidence_refs", _frozen_strings(self.checked_evidence_refs))

    @classmethod
    def create(
        cls,
        *,
        node_ref: str,
        defect_state: DefectState,
        downstream_path: List[str],
        hypothesis_id: str,
        hypothesis_semantic_hash: str,
        seed_binding_identity: str = "",
        depth: int = 0,
        candidate_source: str = "",
        priority: float = 0.0,
        checked_evidence_refs: Optional[List[str]] = None,
        evidence_hash: str = "",
        reopen_reason: str = "",
        graph_position: int = 0,
    ) -> "FrontierItem":
        item_id = "frontier:{0}".format(
            _hash(
                {
                    "node_ref": node_ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "downstream_path": downstream_path,
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis_semantic_hash,
                    "seed_binding_identity": seed_binding_identity,
                    "depth": depth,
                }
            )[:20]
        )
        return cls(
            item_id=item_id,
            node_ref=node_ref,
            defect_state=defect_state,
            downstream_path=tuple(downstream_path),
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis_semantic_hash,
            seed_binding_identity=seed_binding_identity,
            depth=depth,
            candidate_source=candidate_source,
            priority=priority,
            checked_evidence_refs=tuple(checked_evidence_refs or []),
            evidence_hash=evidence_hash,
            reopen_reason=reopen_reason,
            graph_position=graph_position,
        )

    @property
    def visit_key(self) -> str:
        return semantic_visit_key(
            self.node_ref,
            self.defect_state,
            self.hypothesis_semantic_hash,
            self.seed_binding_identity,
        )

    @property
    def heap_key(self) -> tuple:
        return (-self.priority, self.depth, self.graph_position, self.item_id)

    def to_dict(self) -> JsonDict:
        return {
            "item_id": self.item_id,
            "node_ref": self.node_ref,
            "defect_state": self.defect_state.to_dict(),
            "downstream_path": list(self.downstream_path),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "seed_binding_identity": self.seed_binding_identity,
            "depth": self.depth,
            "candidate_source": self.candidate_source,
            "priority": self.priority,
            "checked_evidence_refs": list(self.checked_evidence_refs),
            "evidence_hash": self.evidence_hash,
            "reopen_reason": self.reopen_reason,
            "graph_position": self.graph_position,
            "visit_key": self.visit_key,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "FrontierItem":
        item = cls.create(
            node_ref=str(value.get("node_ref") or ""),
            defect_state=DefectState.from_dict(_json_dict(value.get("defect_state"))),
            downstream_path=_string_list(value.get("downstream_path")),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            hypothesis_semantic_hash=str(value.get("hypothesis_semantic_hash") or ""),
            seed_binding_identity=str(value.get("seed_binding_identity") or ""),
            depth=int(value.get("depth") or 0),
            candidate_source=str(value.get("candidate_source") or ""),
            priority=_priority(value.get("priority", 0.0)),
            checked_evidence_refs=_string_list(value.get("checked_evidence_refs")),
            evidence_hash=str(value.get("evidence_hash") or ""),
            reopen_reason=str(value.get("reopen_reason") or ""),
            graph_position=int(value.get("graph_position") or 0),
        )
        item_id = str(value.get("item_id") or "")
        if not item_id or item_id != item.item_id:
            raise ValueError("FrontierItem item_id does not match semantic fields")
        visit_key = str(value.get("visit_key") or "")
        if not visit_key or visit_key != item.visit_key:
            raise ValueError("FrontierItem visit_key does not match semantic fields")
        return item

    @classmethod
    def from_legacy_dict(
        cls,
        value: JsonDict,
        *,
        seed_binding_identity: str,
    ) -> "FrontierItem":
        """Verify a v1 item before binding it to its enclosing seed."""
        if not seed_binding_identity:
            raise ValueError("legacy FrontierItem requires an unambiguous seed binding")
        defect_state = DefectState.from_dict(_json_dict(value.get("defect_state")))
        node_ref = str(value.get("node_ref") or "")
        downstream_path = _string_list(value.get("downstream_path"))
        hypothesis_id = str(value.get("hypothesis_id") or "")
        hypothesis_semantic_hash = str(value.get("hypothesis_semantic_hash") or "")
        depth = int(value.get("depth") or 0)
        legacy_item_id = "frontier:{0}".format(
            _hash(
                {
                    "node_ref": node_ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "downstream_path": downstream_path,
                    "hypothesis_id": hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis_semantic_hash,
                    "depth": depth,
                }
            )[:20]
        )
        item_id = str(value.get("item_id") or "")
        if not item_id or item_id != legacy_item_id:
            raise ValueError("legacy FrontierItem item_id does not match semantic fields")
        legacy_visit_key = _legacy_semantic_visit_key(
            node_ref,
            defect_state,
            hypothesis_semantic_hash,
        )
        visit_key = str(value.get("visit_key") or "")
        if not visit_key or visit_key != legacy_visit_key:
            raise ValueError("legacy FrontierItem visit_key does not match semantic fields")
        return cls.create(
            node_ref=node_ref,
            defect_state=defect_state,
            downstream_path=list(downstream_path),
            hypothesis_id=hypothesis_id,
            hypothesis_semantic_hash=hypothesis_semantic_hash,
            seed_binding_identity=seed_binding_identity,
            depth=depth,
            candidate_source=str(value.get("candidate_source") or ""),
            priority=_priority(value.get("priority", 0.0)),
            checked_evidence_refs=_string_list(value.get("checked_evidence_refs")),
            evidence_hash=str(value.get("evidence_hash") or ""),
            reopen_reason=str(value.get("reopen_reason") or ""),
            graph_position=int(value.get("graph_position") or 0),
        )


@dataclass(frozen=True)
class HypothesisEvidence:
    ref: str
    reason: str
    confidence: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _confidence(self.confidence))

    def to_dict(self) -> JsonDict:
        return {"ref": self.ref, "reason": self.reason, "confidence": self.confidence}

    @classmethod
    def from_dict(cls, value: JsonDict) -> "HypothesisEvidence":
        return cls(
            str(value.get("ref") or ""),
            str(value.get("reason") or ""),
            _confidence(value.get("confidence", 0.0)),
        )


@dataclass(frozen=True)
class AttributionHypothesis:
    hypothesis_id: str
    claim: str
    candidate_root_ref: str
    active_defect_state_id: str
    active_defect_fingerprint: str
    seed_binding_identity: str = ""
    supporting_evidence: Tuple[HypothesisEvidence, ...] = field(default_factory=tuple)
    opposing_evidence: Tuple[HypothesisEvidence, ...] = field(default_factory=tuple)
    unresolved_questions: Tuple[str, ...] = field(default_factory=tuple)
    alternative_hypothesis_ids: Tuple[str, ...] = field(default_factory=tuple)
    counterfactual: JsonDict = field(default_factory=FrozenMapping)
    status: str = "active"
    confidence: float = 0.0
    resolution_reason: str = ""
    semantic_hash: str = ""

    def __post_init__(self) -> None:
        if self.status not in HYPOTHESIS_STATUSES:
            raise ValueError("unsupported hypothesis status: {0}".format(self.status))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "supporting_evidence", tuple(self.supporting_evidence))
        object.__setattr__(self, "opposing_evidence", tuple(self.opposing_evidence))
        object.__setattr__(self, "unresolved_questions", _frozen_strings(self.unresolved_questions))
        object.__setattr__(self, "alternative_hypothesis_ids", _frozen_strings(self.alternative_hypothesis_ids))
        object.__setattr__(self, "counterfactual", FrozenMapping(_thaw(self.counterfactual)))

    @classmethod
    def create(
        cls,
        claim: str,
        candidate_root_ref: str,
        defect_state: DefectState,
        *,
        seed_binding_identity: str = "",
    ) -> "AttributionHypothesis":
        semantic_hash = cls._semantic_hash(claim, candidate_root_ref, defect_state.fingerprint, [])
        return cls(
            hypothesis_id=cls._hypothesis_id(semantic_hash, seed_binding_identity),
            claim=claim,
            candidate_root_ref=candidate_root_ref,
            active_defect_state_id=defect_state.defect_state_id,
            active_defect_fingerprint=defect_state.fingerprint,
            seed_binding_identity=seed_binding_identity,
            semantic_hash=semantic_hash,
        )

    @staticmethod
    def _hypothesis_id(semantic_hash: str, seed_binding_identity: str) -> str:
        if not seed_binding_identity:
            return "hyp:{0}".format(semantic_hash[:20])
        identity_hash = _hash(
            {
                "semantic_hash": semantic_hash,
                "seed_binding_identity": seed_binding_identity,
            }
        )
        return "hyp:{0}".format(identity_hash[:20])

    @staticmethod
    def _semantic_hash(
        claim: str, candidate_root_ref: str, defect_fingerprint: str, unresolved_questions: Tuple[str, ...]
    ) -> str:
        return _hash(
            {
                "claim": " ".join(claim.split()).lower(),
                "candidate_root_ref": candidate_root_ref,
                "active_defect_fingerprint": defect_fingerprint,
                "unresolved_questions": sorted(unresolved_questions),
            }
        )

    def with_updates(self, **changes: Any) -> "AttributionHypothesis":
        updated = replace(self, **changes)
        semantic_hash = self._semantic_hash(
            updated.claim,
            updated.candidate_root_ref,
            updated.active_defect_fingerprint,
            updated.unresolved_questions,
        )
        return replace(
            updated,
            hypothesis_id=self._hypothesis_id(
                semantic_hash, updated.seed_binding_identity
            ),
            semantic_hash=semantic_hash,
        )

    def to_dict(self) -> JsonDict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "claim": self.claim,
            "candidate_root_ref": self.candidate_root_ref,
            "active_defect_state_id": self.active_defect_state_id,
            "active_defect_fingerprint": self.active_defect_fingerprint,
            "seed_binding_identity": self.seed_binding_identity,
            "supporting_evidence": [item.to_dict() for item in self.supporting_evidence],
            "opposing_evidence": [item.to_dict() for item in self.opposing_evidence],
            "unresolved_questions": list(self.unresolved_questions),
            "alternative_hypothesis_ids": list(self.alternative_hypothesis_ids),
            "counterfactual": _thaw(self.counterfactual),
            "status": self.status,
            "confidence": self.confidence,
            "resolution_reason": self.resolution_reason,
            "semantic_hash": self.semantic_hash,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "AttributionHypothesis":
        supporting = value.get("supporting_evidence")
        opposing = value.get("opposing_evidence")
        claim = str(value.get("claim") or "")
        candidate_root_ref = str(value.get("candidate_root_ref") or "")
        active_defect_fingerprint = str(value.get("active_defect_fingerprint") or "")
        unresolved_questions = _frozen_strings(value.get("unresolved_questions"))
        semantic_hash = cls._semantic_hash(
            claim, candidate_root_ref, active_defect_fingerprint, unresolved_questions
        )
        seed_binding_identity = str(value.get("seed_binding_identity") or "")
        hypothesis_id = cls._hypothesis_id(semantic_hash, seed_binding_identity)
        persisted_semantic_hash = str(value.get("semantic_hash") or "")
        if persisted_semantic_hash and persisted_semantic_hash != semantic_hash:
            raise ValueError("AttributionHypothesis semantic_hash does not match semantic fields")
        persisted_hypothesis_id = str(value.get("hypothesis_id") or "")
        if persisted_hypothesis_id and persisted_hypothesis_id != hypothesis_id:
            raise ValueError("AttributionHypothesis hypothesis_id does not match semantic fields")
        active_defect_state_id = str(value.get("active_defect_state_id") or "")
        expected_defect_state_id = "defect:{0}".format(active_defect_fingerprint)
        if active_defect_state_id and active_defect_state_id != expected_defect_state_id:
            raise ValueError("AttributionHypothesis active_defect_state_id does not match fingerprint")
        return cls(
            hypothesis_id=hypothesis_id,
            claim=claim,
            candidate_root_ref=candidate_root_ref,
            active_defect_state_id=expected_defect_state_id,
            active_defect_fingerprint=active_defect_fingerprint,
            seed_binding_identity=seed_binding_identity,
            supporting_evidence=[HypothesisEvidence.from_dict(item) for item in supporting if isinstance(item, dict)]
            if isinstance(supporting, list)
            else [],
            opposing_evidence=[HypothesisEvidence.from_dict(item) for item in opposing if isinstance(item, dict)]
            if isinstance(opposing, list)
            else [],
            unresolved_questions=unresolved_questions,
            alternative_hypothesis_ids=_string_list(value.get("alternative_hypothesis_ids")),
            counterfactual=_json_dict(value.get("counterfactual")),
            status=str(value.get("status") or "active"),
            confidence=_confidence(value.get("confidence", 0.0)),
            resolution_reason=str(value.get("resolution_reason") or ""),
            semantic_hash=semantic_hash,
        )


@dataclass(frozen=True)
class RootConfirmation:
    candidate_ref: str
    status: str
    excerpt: str = ""
    reason: str = ""
    counterfactual: str = ""
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    counterfactual_status: str = ""
    hypothesis_id: str = ""
    hypothesis_semantic_hash: str = ""
    defect_fingerprint: str = ""
    recursive_path: Tuple[str, ...] = field(default_factory=tuple)
    seed_binding_identity: str = ""
    analysis_perspective: str = ""
    factor_role: str = "unknown"
    competitor_comparisons: Tuple[JsonDict, ...] = field(default_factory=tuple)
    factor_mechanism: JsonDict = field(default_factory=FrozenMapping)
    process_confirmation_assessment: JsonDict = field(
        default_factory=FrozenMapping
    )

    def __post_init__(self) -> None:
        if self.status not in CONFIRMATION_STATUSES:
            raise ValueError("unsupported root confirmation status: {0}".format(self.status))
        default_counterfactual = {
            "confirmed": "supports_causality",
            "rejected": "rejects_causality",
            "unknown": "unknown",
        }[self.status]
        counterfactual_status = self.counterfactual_status or default_counterfactual
        if counterfactual_status not in COUNTERFACTUAL_STATUSES:
            raise ValueError(
                "unsupported counterfactual status: {0}".format(counterfactual_status)
            )
        allowed_counterfactuals = {
            "confirmed": {"supports_causality"},
            "rejected": {"rejects_causality", "unknown"},
            "unknown": {"unknown"},
        }[self.status]
        if counterfactual_status not in allowed_counterfactuals:
            raise ValueError(
                "{0} confirmation does not allow counterfactual_status={1}".format(
                    self.status, counterfactual_status
                )
            )
        object.__setattr__(self, "counterfactual_status", counterfactual_status)
        if self.factor_role not in CONFIRMATION_FACTOR_ROLES:
            raise ValueError("unsupported confirmation factor_role: {0}".format(self.factor_role))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "recursive_path", _frozen_strings(self.recursive_path))
        object.__setattr__(
            self,
            "competitor_comparisons",
            tuple(FrozenMapping(_thaw(item)) for item in self.competitor_comparisons),
        )
        object.__setattr__(self, "factor_mechanism", FrozenMapping(_thaw(self.factor_mechanism)))
        object.__setattr__(
            self,
            "process_confirmation_assessment",
            FrozenMapping(_thaw(self.process_confirmation_assessment)),
        )

    @property
    def confirmation_identity(self) -> str:
        return confirmation_identity_for(
            hypothesis_id=self.hypothesis_id,
            hypothesis_semantic_hash=self.hypothesis_semantic_hash,
            candidate_ref=self.candidate_ref,
            defect_fingerprint=self.defect_fingerprint,
            recursive_path=self.recursive_path,
            seed_binding_identity=self.seed_binding_identity,
        )

    @property
    def response_identity(self) -> str:
        return confirmation_response_identity_for(
            confirmation_identity=self.confirmation_identity,
            status=self.status,
            excerpt=self.excerpt,
            reason=self.reason,
            counterfactual=self.counterfactual,
            confidence=self.confidence,
            evidence_refs=self.evidence_refs,
            counterfactual_status=self.counterfactual_status,
            factor_role=self.factor_role,
            competitor_comparisons=self.competitor_comparisons,
            factor_mechanism=self.factor_mechanism,
            analysis_perspective=self.analysis_perspective,
            process_confirmation_assessment=(
                self.process_confirmation_assessment
            ),
        )

    @classmethod
    def confirmed(
        cls,
        candidate_ref: str,
        *,
        excerpt: str,
        reason: str,
        counterfactual: str,
        confidence: float,
        evidence_refs: Optional[List[str]] = None,
        counterfactual_status: str = "supports_causality",
        factor_role: str = "necessary_cause",
    ) -> "RootConfirmation":
        return cls(
            candidate_ref,
            "confirmed",
            excerpt,
            reason,
            counterfactual,
            confidence,
            list(evidence_refs or []),
            counterfactual_status,
            factor_role=factor_role,
        )

    @classmethod
    def rejected(
        cls,
        candidate_ref: str,
        reason: str,
        evidence_refs: Optional[List[str]] = None,
        *,
        factor_role: str = "unrelated",
        counterfactual: str = "",
        confidence: float = 1.0,
    ) -> "RootConfirmation":
        return cls(
            candidate_ref,
            "rejected",
            reason=reason,
            counterfactual=counterfactual
            or confirmation_counterfactual_for(candidate_ref, "rejected"),
            confidence=confidence,
            evidence_refs=list(evidence_refs or []),
            counterfactual_status="rejects_causality",
            factor_role=factor_role,
        )

    @classmethod
    def unknown(cls, candidate_ref: str, reason: str, evidence_refs: Optional[List[str]] = None) -> "RootConfirmation":
        return cls(
            candidate_ref,
            "unknown",
            reason=reason,
            counterfactual=confirmation_counterfactual_for(
                candidate_ref, "unknown"
            ),
            evidence_refs=list(evidence_refs or []),
            counterfactual_status="unknown",
            factor_role="unknown",
        )

    def to_dict(self) -> JsonDict:
        payload = {
            "candidate_ref": self.candidate_ref,
            "status": self.status,
            "excerpt": self.excerpt,
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "counterfactual_status": self.counterfactual_status,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "defect_fingerprint": self.defect_fingerprint,
            "recursive_path": list(self.recursive_path),
            "seed_binding_identity": self.seed_binding_identity,
            "analysis_perspective": self.analysis_perspective,
            "factor_role": self.factor_role,
            "competitor_comparisons": [_thaw(item) for item in self.competitor_comparisons],
            "factor_mechanism": _thaw(self.factor_mechanism),
            "confirmation_identity": self.confirmation_identity,
            "response_identity": self.response_identity,
        }
        if self.process_confirmation_assessment:
            payload["process_confirmation_assessment"] = _thaw(
                self.process_confirmation_assessment
            )
        return payload

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RootConfirmation":
        persisted_identity = str(value.get("confirmation_identity") or "")
        if not persisted_identity:
            raise ValueError("RootConfirmation confirmation_identity is required")
        persisted_response_identity = str(
            value.get("response_identity") or ""
        )
        if not persisted_response_identity:
            raise ValueError("RootConfirmation response_identity is required")
        status = str(value.get("status") or "unknown")
        result = cls(
            candidate_ref=str(value.get("candidate_ref") or ""),
            status=status,
            excerpt=str(value.get("excerpt") or ""),
            reason=str(value.get("reason") or ""),
            counterfactual=str(value.get("counterfactual") or ""),
            confidence=_confidence(value.get("confidence", 0.0)),
            evidence_refs=_string_list(value.get("evidence_refs")),
            counterfactual_status=str(
                value.get("counterfactual_status")
                or {
                    "confirmed": "supports_causality",
                    "rejected": "rejects_causality",
                    "unknown": "unknown",
                }.get(status, "unknown")
            ),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            hypothesis_semantic_hash=str(value.get("hypothesis_semantic_hash") or ""),
            defect_fingerprint=str(value.get("defect_fingerprint") or ""),
            recursive_path=_string_list(value.get("recursive_path")),
            seed_binding_identity=str(value.get("seed_binding_identity") or ""),
            analysis_perspective=str(value.get("analysis_perspective") or ""),
            factor_role=str(
                value.get("factor_role")
                or {
                    "confirmed": "necessary_cause",
                    "rejected": "unrelated",
                    "unknown": "unknown",
                }.get(status, "unknown")
            ),
            competitor_comparisons=tuple(
                dict(item)
                for item in value.get("competitor_comparisons", [])
                if isinstance(item, Mapping)
            ),
            factor_mechanism=_json_dict(value.get("factor_mechanism")),
            process_confirmation_assessment=_json_dict(
                value.get("process_confirmation_assessment")
            ),
        )
        if persisted_identity != result.confirmation_identity:
            raise ValueError("RootConfirmation confirmation_identity does not match semantic fields")
        if persisted_response_identity != result.response_identity:
            raise ValueError(
                "root confirmation response_identity projection does not "
                "match substantive fields"
            )
        validate_root_confirmation_substantive_invariants(
            result,
            require_canonical_counterfactual=True,
        )
        return result


def _factor_role_judgment_identity(
    *,
    candidate_ref: str,
    necessity_status: str,
    factor_role: str,
    reason: str,
    confidence: float,
    evidence_refs: Tuple[str, ...],
    recursive_path: Tuple[str, ...],
    factor_mechanism: Mapping[str, Any],
    counterfactual: Mapping[str, Any],
    hypothesis_id: str,
    hypothesis_semantic_hash: str,
    defect_fingerprint: str,
    seed_binding_identity: str,
    analysis_perspective: str,
    request_identity: str,
) -> str:
    semantic = {
        "candidate_ref": candidate_ref,
        "necessity_status": necessity_status,
        "factor_role": factor_role,
        "reason": reason,
        "confidence": confidence,
        "evidence_refs": list(evidence_refs),
        "recursive_path": list(recursive_path),
        "factor_mechanism": _thaw(factor_mechanism),
        "counterfactual": _thaw(counterfactual),
        "hypothesis_id": hypothesis_id,
        "hypothesis_semantic_hash": hypothesis_semantic_hash,
        "defect_fingerprint": defect_fingerprint,
        "seed_binding_identity": seed_binding_identity,
        "analysis_perspective": analysis_perspective,
        "request_identity": request_identity,
    }
    return "{0}{1}".format(
        FACTOR_ROLE_JUDGMENT_IDENTITY_PREFIX,
        hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest(),
    )


def _require_factor_role_string(value: Any, *, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(field_name))
    return value


def _require_factor_role_references(
    value: Any,
    *,
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


def _validate_factor_role_mechanism(
    value: Mapping[str, Any],
    *,
    contract_entry: Mapping[str, Any],
) -> FrozenMapping:
    if not isinstance(value, Mapping):
        raise ValueError("factor role mechanism must be an object")
    expected_mechanism = contract_entry["factor_mechanism"]
    expected_mechanism_type = expected_mechanism.get("mechanism_type")
    if not value:
        if expected_mechanism_type is not None:
            raise ValueError("factor role requires a non-empty mechanism")
        return FrozenMapping({})
    expected_keys = {
        "schema",
        "mechanism_type",
        "source_ref",
        "target_ref",
        "effect",
    }
    if (
        {str(key) for key in value} != expected_keys
        or value.get("schema") != FACTOR_ROLE_MECHANISM_SCHEMA
        or any(
            type(value.get(name)) is not str or not value[name].strip()
            for name in expected_keys - {"schema"}
        )
    ):
        raise ValueError("factor role mechanism has an inexact schema")
    if value["mechanism_type"] != expected_mechanism_type:
        raise ValueError("factor_mechanism type contradicts factor_role")
    return FrozenMapping(_thaw(value))


def _validate_factor_role_counterfactual(
    value: Mapping[str, Any],
    *,
    contract_entry: Mapping[str, Any],
    candidate_ref: str,
) -> FrozenMapping:
    expected_keys = {
        "schema",
        "intervention_ref",
        "intervention_kind",
        "predicted_effect",
    }
    if not isinstance(value, Mapping) or (
        {str(key) for key in value} != expected_keys
        or value.get("schema") != FACTOR_ROLE_COUNTERFACTUAL_SCHEMA
        or any(
            type(value.get(name)) is not str or not value[name].strip()
            for name in expected_keys - {"schema"}
        )
    ):
        raise ValueError("factor role counterfactual has an inexact schema")
    if (
        value["intervention_ref"] != candidate_ref
        or value["intervention_kind"]
        != "replace_with_semantically_correct_behavior"
    ):
        raise ValueError("factor role counterfactual contradicts its candidate")
    if value["predicted_effect"] not in contract_entry["predicted_effects"]:
        raise ValueError("factor role counterfactual effect contradicts role")
    return FrozenMapping(_thaw(value))


@dataclass(frozen=True)
class FactorRoleJudgment:
    """Independent necessity and non-root-role facts for one candidate."""

    candidate_ref: str
    necessity_status: str
    factor_role: str
    reason: str
    confidence: float
    evidence_refs: Tuple[str, ...]
    recursive_path: Tuple[str, ...]
    factor_mechanism: Mapping[str, Any]
    counterfactual: Mapping[str, Any]
    hypothesis_id: str
    hypothesis_semantic_hash: str
    defect_fingerprint: str
    seed_binding_identity: str
    analysis_perspective: str
    request_identity: str

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_ref",
            "reason",
            "hypothesis_id",
            "hypothesis_semantic_hash",
            "defect_fingerprint",
            "seed_binding_identity",
            "analysis_perspective",
            "request_identity",
        ):
            _require_factor_role_string(
                getattr(self, field_name), field_name=field_name
            )
        contract_entry = factor_role_contract_entry(
            self.necessity_status,
            self.factor_role,
        )
        if contract_entry is None:
            raise ValueError("factor role pair is absent from role contract")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        evidence_refs = _require_factor_role_references(
            self.evidence_refs, field_name="evidence_refs"
        )
        recursive_path = _require_factor_role_references(
            self.recursive_path, field_name="recursive_path"
        )
        if recursive_path[0] != self.candidate_ref:
            raise ValueError(
                "recursive_path must start with candidate_ref"
            )
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "recursive_path", recursive_path)
        mechanism = _validate_factor_role_mechanism(
            self.factor_mechanism,
            contract_entry=contract_entry,
        )
        object.__setattr__(self, "factor_mechanism", mechanism)
        object.__setattr__(
            self,
            "counterfactual",
            _validate_factor_role_counterfactual(
                self.counterfactual,
                contract_entry=contract_entry,
                candidate_ref=self.candidate_ref,
            ),
        )

    @property
    def judgment_identity(self) -> str:
        return _factor_role_judgment_identity(
            candidate_ref=self.candidate_ref,
            necessity_status=self.necessity_status,
            factor_role=self.factor_role,
            reason=self.reason,
            confidence=self.confidence,
            evidence_refs=self.evidence_refs,
            recursive_path=self.recursive_path,
            factor_mechanism=self.factor_mechanism,
            counterfactual=self.counterfactual,
            hypothesis_id=self.hypothesis_id,
            hypothesis_semantic_hash=self.hypothesis_semantic_hash,
            defect_fingerprint=self.defect_fingerprint,
            seed_binding_identity=self.seed_binding_identity,
            analysis_perspective=self.analysis_perspective,
            request_identity=self.request_identity,
        )

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "necessity_status": self.necessity_status,
            "factor_role": self.factor_role,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "recursive_path": list(self.recursive_path),
            "factor_mechanism": _thaw(self.factor_mechanism),
            "counterfactual": _thaw(self.counterfactual),
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_semantic_hash": self.hypothesis_semantic_hash,
            "defect_fingerprint": self.defect_fingerprint,
            "seed_binding_identity": self.seed_binding_identity,
            "analysis_perspective": self.analysis_perspective,
            "request_identity": self.request_identity,
            "judgment_identity": self.judgment_identity,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "FactorRoleJudgment":
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
            "judgment_identity",
        }
        if not isinstance(value, Mapping) or {str(key) for key in value} != expected_keys:
            raise ValueError("factor role judgment has an inexact schema")
        result = cls(
            candidate_ref=value["candidate_ref"],
            necessity_status=value["necessity_status"],
            factor_role=value["factor_role"],
            reason=value["reason"],
            confidence=_confidence(value["confidence"]),
            evidence_refs=value["evidence_refs"],
            recursive_path=value["recursive_path"],
            factor_mechanism=value["factor_mechanism"],
            counterfactual=value["counterfactual"],
            hypothesis_id=value["hypothesis_id"],
            hypothesis_semantic_hash=value["hypothesis_semantic_hash"],
            defect_fingerprint=value["defect_fingerprint"],
            seed_binding_identity=value["seed_binding_identity"],
            analysis_perspective=value["analysis_perspective"],
            request_identity=value["request_identity"],
        )
        if str(value["judgment_identity"]) != result.judgment_identity:
            raise ValueError("factor role judgment identity does not match semantic fields")
        return result


def validate_root_confirmation_substantive_invariants(
    confirmation: RootConfirmation,
    *,
    require_canonical_counterfactual: bool = False,
) -> RootConfirmation:
    if not isinstance(confirmation, RootConfirmation):
        raise TypeError("root confirmation must be a RootConfirmation")
    if not confirmation.reason.strip():
        raise ValueError("root confirmation reason must be non-empty")
    if (
        confirmation.status != "unknown"
        and confirmation.confidence <= 0.0
    ):
        raise ValueError(
            "non-unknown confirmation requires positive confidence"
        )
    if not confirmation.counterfactual.strip():
        raise ValueError(
            "root confirmation counterfactual must be non-empty"
        )
    if require_canonical_counterfactual:
        validate_root_confirmation_counterfactual(confirmation)
    allowed_counterfactuals = {
        "confirmed": {"supports_causality"},
        "rejected": {"rejects_causality", "unknown"},
        "unknown": {"unknown"},
    }[confirmation.status]
    if confirmation.counterfactual_status not in allowed_counterfactuals:
        raise ValueError(
            "counterfactual status is inconsistent with confirmation status"
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
        raise ValueError(
            "confirmation factor_role contradicts its status"
        )
    if confirmation.status == "confirmed":
        if not confirmation.evidence_refs:
            raise ValueError("confirmed root requires grounded evidence refs")
        if not confirmation.excerpt.strip():
            raise ValueError("confirmed root requires a grounded excerpt")
        if confirmation.counterfactual_status != "supports_causality":
            raise ValueError(
                "confirmed root requires a causality-supporting counterfactual"
            )
        if confirmation.factor_role != "necessary_cause":
            raise ValueError(
                "confirmed root requires factor_role=necessary_cause"
            )
    return confirmation


def is_definitive_confirmation(confirmation: RootConfirmation) -> bool:
    """Return whether validated status facts conclusively resolve the candidate."""
    if confirmation.status == "confirmed":
        return confirmation.counterfactual_status == "supports_causality"
    return (
        confirmation.status == "rejected"
        and confirmation.counterfactual_status == "rejects_causality"
    )


def _canonical_persisted_confirmation(value: Any) -> JsonDict:
    raw = _json_dict(value)
    return RootConfirmation.from_dict(raw).to_dict() if raw else {}


def _canonical_persisted_factor_role_judgment(value: Any) -> JsonDict:
    raw = _json_dict(value)
    if not raw:
        return {}
    try:
        return FactorRoleJudgment.from_dict(raw).to_dict()
    except ValueError:
        # Standalone legacy values remain readable, but v22 report validation
        # accepts only the FactorRoleJudgment projection.
        return RootConfirmation.from_dict(raw).to_dict()


@dataclass(frozen=True)
class ConfirmedRoot:
    node_ref: str
    defect_state: DefectState
    reason: str
    counterfactual: str
    confidence: float
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    component: str = ""
    event_type: str = ""
    defect_type: str = ""
    causal_role: str = "defect_introduction"
    episode_id: str = ""
    episode_member_refs: Tuple[str, ...] = field(default_factory=tuple)
    observed_defect_refs: Tuple[str, ...] = field(default_factory=tuple)
    hypothesis_id: str = ""
    recursive_path: Tuple[str, ...] = field(default_factory=tuple)
    excerpt: str = ""
    confirmation_status: str = "confirmed"
    provenance: JsonDict = field(default_factory=FrozenMapping)
    confirmation: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        if self.causal_role not in {
            "defect_introduction",
            *ACTIVE_FAILURE_CAUSAL_ROLES,
        }:
            raise ValueError("unsupported confirmed root causal role")
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "episode_member_refs", _frozen_strings(self.episode_member_refs))
        object.__setattr__(self, "observed_defect_refs", _frozen_strings(self.observed_defect_refs))
        object.__setattr__(self, "recursive_path", _frozen_strings(self.recursive_path))
        object.__setattr__(self, "provenance", FrozenMapping(_thaw(self.provenance)))
        object.__setattr__(self, "confirmation", FrozenMapping(_thaw(self.confirmation)))
        if self.causal_role in ACTIVE_FAILURE_CAUSAL_ROLES:
            binding = ActiveFailureRoleBinding.from_dict(
                self.provenance.get("active_role_binding")
            )
            if (
                binding.candidate_ref != self.node_ref
                or binding.defect_fingerprint != self.defect_state.fingerprint
                or binding.causal_role != self.causal_role
                or binding.disposition != "root"
            ):
                raise ValueError(
                    "confirmed root contradicts active failure role binding"
                )

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "defect_state": self.defect_state.to_dict(),
            "reason": self.reason,
            "counterfactual": self.counterfactual,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "component": self.component,
            "event_type": self.event_type,
            "defect_type": self.defect_type,
            "causal_role": self.causal_role,
            "episode_id": self.episode_id,
            "episode_member_refs": list(self.episode_member_refs),
            "observed_defect_refs": list(self.observed_defect_refs),
            "hypothesis_id": self.hypothesis_id,
            "recursive_path": list(self.recursive_path),
            "excerpt": self.excerpt,
            "confirmation_status": self.confirmation_status,
            "provenance": _thaw(self.provenance),
            "confirmation": _thaw(self.confirmation),
        }

    def to_legacy_root_cause(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "component": self.component,
            "event_type": self.event_type,
            "defect_type": self.defect_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "causal_role": self.causal_role,
            "episode_id": self.episode_id,
            "episode_member_refs": list(self.episode_member_refs),
            "observed_defect_refs": list(self.observed_defect_refs),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "ConfirmedRoot":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            defect_state=DefectState.from_dict(_json_dict(value.get("defect_state"))),
            reason=str(value.get("reason") or ""),
            counterfactual=str(value.get("counterfactual") or ""),
            confidence=_confidence(value.get("confidence", 0.0)),
            evidence_refs=_string_list(value.get("evidence_refs")),
            component=str(value.get("component") or ""),
            event_type=str(value.get("event_type") or ""),
            defect_type=str(value.get("defect_type") or ""),
            causal_role=str(value.get("causal_role") or "defect_introduction"),
            episode_id=str(value.get("episode_id") or ""),
            episode_member_refs=_string_list(value.get("episode_member_refs")),
            observed_defect_refs=_string_list(value.get("observed_defect_refs")),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            recursive_path=_string_list(value.get("recursive_path")),
            excerpt=str(value.get("excerpt") or ""),
            confirmation_status=str(value.get("confirmation_status") or "confirmed"),
            provenance=_json_dict(value.get("provenance")),
            confirmation=_canonical_persisted_confirmation(
                value.get("confirmation")
            ),
        )


@dataclass(frozen=True)
class CausalFactor:
    node_ref: str
    relation: str
    reason: str
    confidence: float = 0.0
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    recursive_path: Tuple[str, ...] = field(default_factory=tuple)
    factor_label: str = ""
    confirmation_status: str = ""
    confirmation: JsonDict = field(default_factory=FrozenMapping)
    provenance: JsonDict = field(default_factory=FrozenMapping)
    mechanism: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        if self.relation not in CAUSAL_RELATIONS:
            raise ValueError("unsupported causal relation: {0}".format(self.relation))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "recursive_path", _frozen_strings(self.recursive_path))
        object.__setattr__(self, "confirmation", FrozenMapping(_thaw(self.confirmation)))
        object.__setattr__(self, "provenance", FrozenMapping(_thaw(self.provenance)))
        object.__setattr__(self, "mechanism", FrozenMapping(_thaw(self.mechanism)))

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "relation": self.relation,
            "reason": self.reason,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "recursive_path": list(self.recursive_path),
            "factor_label": self.factor_label,
            "confirmation_status": self.confirmation_status,
            "confirmation": _thaw(self.confirmation),
            "provenance": _thaw(self.provenance),
            "mechanism": _thaw(self.mechanism),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalFactor":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            relation=str(value.get("relation") or "unknown"),
            reason=str(value.get("reason") or ""),
            confidence=_confidence(value.get("confidence", 0.0)),
            evidence_refs=_string_list(value.get("evidence_refs")),
            recursive_path=_string_list(value.get("recursive_path")),
            factor_label=str(value.get("factor_label") or ""),
            confirmation_status=str(value.get("confirmation_status") or ""),
            confirmation=_canonical_persisted_factor_role_judgment(
                value.get("confirmation")
            ),
            provenance=_json_dict(value.get("provenance")),
            mechanism=_json_dict(value.get("mechanism")),
        )


@dataclass(frozen=True)
class CausalMaterialization:
    candidate_ref: str
    recursive_path: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    reason: str
    confidence: float
    role_judgment: JsonDict
    provenance: JsonDict
    materialization_mechanism: JsonDict

    def __post_init__(self) -> None:
        _require_factor_role_string(
            self.candidate_ref, field_name="candidate_ref"
        )
        _require_factor_role_string(self.reason, field_name="reason")
        recursive_path = _require_factor_role_references(
            self.recursive_path, field_name="recursive_path"
        )
        evidence_refs = _require_factor_role_references(
            self.evidence_refs, field_name="evidence_refs"
        )
        if recursive_path[0] != self.candidate_ref:
            raise ValueError(
                "materialization path must start with candidate_ref"
            )
        object.__setattr__(self, "recursive_path", recursive_path)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        raw_role_judgment = _json_dict(self.role_judgment)
        if not raw_role_judgment:
            raise ValueError(
                "materialization requires a complete role judgment"
            )
        judgment = FactorRoleJudgment.from_dict(raw_role_judgment)
        role_judgment = judgment.to_dict()
        if (
            judgment.necessity_status
            not in {"necessary", "not_necessary"}
            or judgment.factor_role != "downstream_materialization"
            or self.candidate_ref != judgment.candidate_ref
            or recursive_path != judgment.recursive_path
            or evidence_refs != judgment.evidence_refs
            or self.reason != judgment.reason
            or self.confidence != judgment.confidence
            or _thaw(self.materialization_mechanism)
            != _thaw(judgment.factor_mechanism)
        ):
            raise ValueError(
                "materialization public fields contradict its role judgment"
            )
        object.__setattr__(
            self, "role_judgment", FrozenMapping(role_judgment)
        )
        object.__setattr__(
            self,
            "provenance",
            FrozenMapping(_thaw(self.provenance)),
        )
        object.__setattr__(
            self,
            "materialization_mechanism",
            FrozenMapping(_thaw(self.materialization_mechanism)),
        )

    def to_dict(self) -> JsonDict:
        return {
            "candidate_ref": self.candidate_ref,
            "recursive_path": list(self.recursive_path),
            "evidence_refs": list(self.evidence_refs),
            "reason": self.reason,
            "confidence": self.confidence,
            "role_judgment": _thaw(self.role_judgment),
            "provenance": _thaw(self.provenance),
            "materialization_mechanism": _thaw(
                self.materialization_mechanism
            ),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "CausalMaterialization":
        expected_keys = {
            "candidate_ref",
            "recursive_path",
            "evidence_refs",
            "reason",
            "confidence",
            "role_judgment",
            "provenance",
            "materialization_mechanism",
        }
        optional_annotation_keys = {
            "semantic_anchor_id",
            "semantic_occurrence_id",
        }
        if (
            not isinstance(value, Mapping)
            or not expected_keys.issubset(
                {str(key) for key in value}
            )
            or {
                str(key) for key in value
            } - expected_keys - optional_annotation_keys
        ):
            raise ValueError("causal materialization has an inexact schema")
        result = cls(
            candidate_ref=value["candidate_ref"],
            recursive_path=value["recursive_path"],
            evidence_refs=value["evidence_refs"],
            reason=value["reason"],
            confidence=value["confidence"],
            role_judgment=value["role_judgment"],
            provenance=value["provenance"],
            materialization_mechanism=value[
                "materialization_mechanism"
            ],
        )
        canonical_input = {
            key: _thaw(value[key]) for key in expected_keys
        }
        if stable_json(canonical_input) != stable_json(result.to_dict()):
            raise ValueError("causal materialization is non-canonical")
        return result


@dataclass(frozen=True)
class RejectedCandidate:
    node_ref: str
    reason: str
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    hypothesis_id: str = ""
    recursive_path: Tuple[str, ...] = field(default_factory=tuple)
    confirmation_status: str = ""
    confidence: float = 0.0
    confirmation: JsonDict = field(default_factory=FrozenMapping)
    provenance: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "recursive_path", _frozen_strings(self.recursive_path))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "confirmation", FrozenMapping(_thaw(self.confirmation)))
        object.__setattr__(self, "provenance", FrozenMapping(_thaw(self.provenance)))

    def to_dict(self) -> JsonDict:
        return {
            "node_ref": self.node_ref,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "hypothesis_id": self.hypothesis_id,
            "recursive_path": list(self.recursive_path),
            "confirmation_status": self.confirmation_status,
            "confidence": self.confidence,
            "confirmation": _thaw(self.confirmation),
            "provenance": _thaw(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RejectedCandidate":
        return cls(
            node_ref=str(value.get("node_ref") or ""),
            reason=str(value.get("reason") or ""),
            evidence_refs=_string_list(value.get("evidence_refs")),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            recursive_path=_string_list(value.get("recursive_path")),
            confirmation_status=str(value.get("confirmation_status") or ""),
            confidence=_confidence(value.get("confidence", 0.0)),
            confirmation=_canonical_persisted_factor_role_judgment(
                value.get("confirmation")
            ),
            provenance=_json_dict(value.get("provenance")),
        )


def canonical_confirmation_publication_provenance(
    confirmation: RootConfirmation,
) -> JsonDict:
    """Return the immutable provenance shared by every confirmation publication."""
    return {
        "publication_contract": CAUSAL_PUBLICATION_CONTRACT_VERSION,
        "confirmation_identity": confirmation.confirmation_identity,
        "response_identity": confirmation.response_identity,
        "defect_fingerprint": confirmation.defect_fingerprint,
        "seed_binding_identity": confirmation.seed_binding_identity,
        "analysis_perspective": confirmation.analysis_perspective,
    }


def owning_root_request_projection(
    *,
    confirmation: RootConfirmation,
    confirmation_action_projections: Iterable[Mapping[str, Any]],
) -> JsonDict:
    """Return the sole factual request that owns a terminal confirmation."""
    exact_matches = []
    stable_matches = []
    for action in confirmation_action_projections:
        if not isinstance(action, Mapping) or not isinstance(
            action.get("confirmation"), Mapping
        ):
            continue
        embedded = RootConfirmation.from_dict(
            _thaw(action["confirmation"])
        )
        projection = action.get("factual_request_projection")
        if embedded.confirmation_identity == confirmation.confirmation_identity:
            exact_matches.append(projection)
        if (
            embedded.candidate_ref == confirmation.candidate_ref
            and embedded.hypothesis_id == confirmation.hypothesis_id
            and embedded.hypothesis_semantic_hash
            == confirmation.hypothesis_semantic_hash
            and embedded.defect_fingerprint
            == confirmation.defect_fingerprint
            and embedded.seed_binding_identity
            == confirmation.seed_binding_identity
        ):
            stable_matches.append(projection)
    matches = exact_matches or stable_matches
    if len(matches) != 1 or not isinstance(matches[0], Mapping):
        raise ValueError(
            "published root confirmation action requires exactly one owning "
            "factual request"
        )
    return _thaw(matches[0])


def canonical_factor_label(
    factor_role: str,
    analysis_perspective: str,
) -> str:
    return "{0} for {1}".format(
        factor_role.replace("_", " "),
        analysis_perspective,
    )


def canonical_confirmed_root_publication(
    *,
    confirmation: RootConfirmation,
    defect_state: DefectState,
    candidate_node: TraceNode,
    seed_start_ref: str,
    active_role_binding: Optional[ActiveFailureRoleBinding] = None,
    request_projection: Optional[Mapping[str, Any]] = None,
) -> ConfirmedRoot:
    """Project one confirmed response into its sole authoritative root form."""
    if confirmation.status != "confirmed":
        raise ValueError("canonical root publication requires confirmed status")
    if not seed_start_ref:
        raise ValueError("canonical root publication requires an owning seed")
    causal_role = "defect_introduction"
    provenance = canonical_confirmation_publication_provenance(confirmation)
    if active_role_binding is not None:
        binding = canonical_active_failure_role_request_binding(
            active_role_binding=active_role_binding.to_dict(),
            request_projection=request_projection,
            disposition="root",
        )
        if (
            binding.candidate_ref != confirmation.candidate_ref
            or binding.seed_ref != seed_start_ref
            or binding.defect_fingerprint != defect_state.fingerprint
            or binding.defect_fingerprint != confirmation.defect_fingerprint
            or binding.disposition != "root"
            or binding.failure_signature
            not in binding.counterfactual_prevention_signatures
        ):
            raise ValueError(
                "confirmed root is cross-bound to another active failure signature"
            )
        parsed_counterfactual = json.loads(
            validate_root_confirmation_counterfactual(confirmation)
        )
        if parsed_counterfactual.get("causal_effect") != "prevents_defect":
            raise ValueError(
                "published root requires explicit counterfactual prevention"
            )
        causal_role = binding.causal_role
        provenance["active_role_binding"] = binding.to_dict()
    return ConfirmedRoot(
        node_ref=confirmation.candidate_ref,
        defect_state=defect_state,
        reason=confirmation.reason,
        counterfactual=confirmation.counterfactual,
        confidence=confirmation.confidence,
        evidence_refs=confirmation.evidence_refs,
        component=candidate_node.component,
        event_type=candidate_node.event_type,
        defect_type=defect_state.label,
        causal_role=causal_role,
        episode_id="",
        episode_member_refs=(),
        observed_defect_refs=(seed_start_ref,),
        hypothesis_id=confirmation.hypothesis_id,
        recursive_path=confirmation.recursive_path,
        excerpt=confirmation.excerpt,
        confirmation_status="confirmed",
        provenance=provenance,
        confirmation=confirmation.to_dict(),
    )


def _canonical_publication_tokens(value: str) -> Set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    output: Set[str] = set()
    word: List[str] = []
    cjk_run: List[str] = []

    def flush_word() -> None:
        if word:
            token = "".join(word)
            if len(token) >= 2:
                output.add(token)
            word.clear()

    def flush_cjk() -> None:
        if not cjk_run:
            return
        output.update(cjk_run)
        output.update(
            "".join(cjk_run[index : index + size])
            for size in (2, 3, 4)
            for index in range(max(0, len(cjk_run) - size + 1))
        )
        cjk_run.clear()

    for char in normalized:
        if "\u3400" <= char <= "\u9fff":
            flush_word()
            cjk_run.append(char)
        else:
            flush_cjk()
            if char.isalnum() or char == "_":
                word.append(char)
            else:
                flush_word()
    flush_word()
    flush_cjk()
    return output


def canonical_confirmed_root_rank(
    root: ConfirmedRoot,
) -> Tuple[int, float, int, str, str]:
    """Rank by perspective-bound facts in the confirmed response projection."""
    confirmation = RootConfirmation.from_dict(dict(root.confirmation))
    perspective = _canonical_publication_tokens(
        confirmation.analysis_perspective
    )
    semantic = _canonical_publication_tokens(
        stable_json(
            {
                "component": root.component,
                "event_type": root.event_type,
                "excerpt": root.excerpt,
                "reason": root.reason,
            }
        )
    )
    return (
        -len(perspective.intersection(semantic)),
        -root.confidence,
        len(root.recursive_path),
        root.node_ref,
        root.hypothesis_id,
    )


def canonical_ranked_root_publications(
    roots: Iterable[ConfirmedRoot],
) -> Tuple[Tuple[ConfirmedRoot, ...], Tuple[ConfirmedRoot, ...]]:
    """Partition and order roots with exactly one deterministic primary per seed."""
    roots_by_seed: Dict[str, Dict[str, ConfirmedRoot]] = {}
    for root in roots:
        confirmation = RootConfirmation.from_dict(dict(root.confirmation))
        if not confirmation.seed_binding_identity:
            raise ValueError("canonical root ranking requires a seed binding")
        by_identity = roots_by_seed.setdefault(
            confirmation.seed_binding_identity,
            {},
        )
        prior = by_identity.get(confirmation.confirmation_identity)
        if prior is not None:
            raise ValueError(
                "canonical root ranking received a duplicate confirmation identity"
            )
        by_identity[confirmation.confirmation_identity] = root
        if any(
            existing_confirmation.analysis_perspective
            != confirmation.analysis_perspective
            for existing_root in by_identity.values()
            for existing_confirmation in (
                RootConfirmation.from_dict(dict(existing_root.confirmation)),
            )
        ):
            raise ValueError(
                "canonical root ranking requires one bound perspective per seed"
            )

    primary_roots: List[ConfirmedRoot] = []
    co_roots: List[ConfirmedRoot] = []
    for seed_binding_identity in sorted(roots_by_seed):
        ranked = []
        for root in roots_by_seed[seed_binding_identity].values():
            ranked.append(
                (
                    canonical_confirmed_root_rank(root),
                    root,
                )
            )
        ordered = [root for _, root in sorted(ranked, key=lambda item: item[0])]
        if ordered:
            primary_roots.append(ordered[0])
            co_roots.extend(ordered[1:])
    return tuple(primary_roots), tuple(co_roots)


def canonical_causal_factor_publication(
    *,
    confirmation: RootConfirmation,
    analysis_perspective: str,
) -> CausalFactor:
    if confirmation.factor_role not in {
        "contributing_condition",
        "amplifying_factor",
    }:
        raise ValueError("canonical factor publication requires a factor role")
    return CausalFactor(
        node_ref=confirmation.candidate_ref,
        relation=confirmation.factor_role,
        reason=confirmation.reason,
        confidence=confirmation.confidence,
        evidence_refs=confirmation.evidence_refs,
        recursive_path=confirmation.recursive_path,
        factor_label=canonical_factor_label(
            confirmation.factor_role,
            analysis_perspective,
        ),
        confirmation_status="rejected",
        confirmation=confirmation.to_dict(),
        provenance=canonical_confirmation_publication_provenance(confirmation),
        mechanism=dict(confirmation.factor_mechanism),
    )


def canonical_rejected_candidate_publication(
    confirmation: RootConfirmation,
) -> RejectedCandidate:
    if confirmation.factor_role not in {"unrelated", "unknown"}:
        raise ValueError(
            "canonical rejected publication requires an unrelated or unknown role"
        )
    return RejectedCandidate(
        node_ref=confirmation.candidate_ref,
        reason=confirmation.reason,
        evidence_refs=confirmation.evidence_refs,
        hypothesis_id=confirmation.hypothesis_id,
        recursive_path=confirmation.recursive_path,
        confirmation_status="rejected",
        confidence=confirmation.confidence,
        confirmation=confirmation.to_dict(),
        provenance=canonical_confirmation_publication_provenance(confirmation),
    )


_FACTOR_ROLE_PUBLICATION_ACTION_KEYS = frozenset(
    {
        "operation",
        "semantic_key",
        "owner",
        "origin",
        "candidate_ref",
        "hypothesis_id",
        "defect_fingerprint",
        "seed_binding_identity",
        "request_projection",
        "request_identity",
        "physical_requests_reserved",
        "physical_request_delta",
        "physical_request_exact",
        "judgment",
        "judgment_identity",
        "failure_classification",
        "queue_binding",
        "active_role_binding",
    }
)
FACTOR_ROLE_FAILURE_CLASSIFICATIONS = frozenset(
    {
        "none",
        "bounded_provider_failure",
        "provider_failure",
        "judgment_invalid",
        "capability_error",
        "interrupted",
        "accounting_breach",
    }
)
FACTOR_ROLE_GAP_KEYS = frozenset(
    {
        "candidate_ref",
        "hypothesis_id",
        "defect_fingerprint",
        "seed_binding_identity",
        "request_identity",
        "judgment_identity",
        "reason",
        "failure_classification",
        "owner",
        "origin",
    }
)
FACTOR_ROLE_ESCALATION_ORIGIN_KEYS = frozenset(
    {
        "kind",
        "factor_action_identity",
        "factor_judgment_identity",
        "factor_request_identity",
    }
)
FACTOR_ROLE_ESCALATION_GAP_KEYS = frozenset(
    {
        "candidate_ref",
        "hypothesis_id",
        "defect_fingerprint",
        "seed_binding_identity",
        "factor_action_identity",
        "factor_judgment_identity",
        "factor_request_identity",
        "root_action_identity",
        "root_request_identity",
        "root_response_identity",
        "status",
        "reason",
        "owner",
        "origin",
    }
)
FACTOR_ROLE_QUEUE_BINDING_KEYS = frozenset(
    {
        "seed_key",
        "requested_by_ref",
        "recursive_path",
        "checked_evidence_refs",
        "task_obligations",
        "artifact_evidence_envelopes",
    }
)
TERMINAL_FACTOR_ROLE_QUEUE_REQUIRED_KEYS = frozenset(
    {
        "hypothesis_id",
        "hypothesis_semantic_hash",
        "candidate_ref",
        "defect_fingerprint",
        "seed_binding_identity",
        "semantic_identity",
        "status",
        "owner",
        "analysis_perspective",
        "artifact_evidence_envelopes",
        "factual_request_projection",
        "review_scope",
        "origin",
        "factor_role_judgment",
        "response_identity",
        "failure_classification",
        "seed_key",
        "requested_by_ref",
        "recursive_path",
        "checked_evidence_refs",
        "task_obligations",
    }
)
TERMINAL_FACTOR_ROLE_QUEUE_ALLOWED_KEYS = (
    TERMINAL_FACTOR_ROLE_QUEUE_REQUIRED_KEYS
)


def canonical_factor_role_escalation_origin(value: Any) -> JsonDict:
    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != set(FACTOR_ROLE_ESCALATION_ORIGIN_KEYS)
        or value.get("kind") != "factor_role_escalation"
        or any(
            type(value.get(key)) is not str or not value[key]
            for key in (
                "factor_action_identity",
                "factor_judgment_identity",
                "factor_request_identity",
            )
        )
    ):
        raise ValueError(
            "factor role escalation origin has an inexact identity schema"
        )
    return {
        "kind": "factor_role_escalation",
        "factor_action_identity": value["factor_action_identity"],
        "factor_judgment_identity": value[
            "factor_judgment_identity"
        ],
        "factor_request_identity": value["factor_request_identity"],
    }


def canonical_confirmation_origin(value: Any) -> Any:
    if isinstance(value, Mapping):
        return canonical_factor_role_escalation_origin(value)
    if type(value) is not str or not value:
        raise ValueError("confirmation origin is required")
    return value


def is_factor_role_escalation_origin(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("kind") == "factor_role_escalation"
    )


def canonical_confirmation_queue_key(
    value: Mapping[str, Any],
) -> Tuple[str, ...]:
    locator = (
        str(value.get("hypothesis_id") or ""),
        str(value.get("candidate_ref") or ""),
        str(value.get("defect_fingerprint") or ""),
        str(value.get("seed_binding_identity") or ""),
    )
    if not all(locator):
        raise ValueError("confirmation queue locator is incomplete")
    origin = value.get("origin")
    if is_factor_role_escalation_origin(origin):
        canonical_origin = canonical_factor_role_escalation_origin(origin)
        return (*locator, canonical_origin["factor_action_identity"])
    return locator


def _factor_role_publication_fact_refs(
    request_projection: Mapping[str, Any],
) -> Set[str]:
    facts = request_projection["facts"]
    refs = {
        str(facts["candidate_ref"]),
        *(str(ref) for ref in facts["recursive_path"]),
    }

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            envelope_keys = {
                "raw_ref",
                "resolved_ref",
                "resolution_status",
                "provenance_class",
            }
            if (
                envelope_keys.issubset({str(key) for key in value})
                and type(value.get("resolved_ref")) is str
                and value["resolved_ref"]
                and value.get("resolution_status") == "resolved"
                and value.get("provenance_class")
                in {"recorded", "reconstructed", "inferred"}
            ):
                refs.add(value["resolved_ref"])
            for child in value.values():
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for field_name in (
        "candidate_reference",
        "recursive_path_references",
        "supporting_evidence",
        "opposing_evidence",
    ):
        collect(facts[field_name])
    for summary in facts["confirmed_root_summaries"]:
        if not isinstance(summary, Mapping):
            continue
        candidate_ref = summary.get("candidate_ref")
        if type(candidate_ref) is str and candidate_ref:
            refs.add(candidate_ref)
        for field_name in ("evidence_refs", "recursive_path"):
            refs.update(
                ref
                for ref in summary.get(field_name) or ()
                if type(ref) is str and ref
            )
    return refs


def canonical_factor_role_queue_binding_snapshot(
    *,
    value: Mapping[str, Any],
    request_projection: Mapping[str, Any],
) -> JsonDict:
    """Canonicalize queue-only facts persisted with a terminal action."""
    from .causal_judge import factor_role_request_from_projection

    if (
        not isinstance(value, Mapping)
        or any(type(key) is not str for key in value)
        or set(value) != set(FACTOR_ROLE_QUEUE_BINDING_KEYS)
    ):
        raise ValueError(
            "factor role terminal queue binding has an inexact schema"
        )
    request = factor_role_request_from_projection(request_projection)
    supporting_refs = tuple(
        dict.fromkeys(
            str(
                item.get("resolved_ref")
                or item.get("canonical_ref")
                or item.get("ref")
                or ""
            )
            for item in request.supporting_evidence
            if str(
                item.get("resolved_ref")
                or item.get("canonical_ref")
                or item.get("ref")
                or ""
            )
        )
    )
    requester = value.get("requested_by_ref")
    artifacts = value.get("artifact_evidence_envelopes")
    if (
        value.get("seed_key") != request.seed_binding_identity
        or tuple(value.get("recursive_path") or ())
        != request.recursive_path
        or tuple(value.get("checked_evidence_refs") or ())
        != supporting_refs
        or stable_json(_thaw(value.get("task_obligations")))
        != stable_json(_thaw(request.task_obligations))
        or type(requester) is not str
        or not requester
        or requester
        not in _factor_role_publication_fact_refs(request_projection)
        or not isinstance(artifacts, (list, tuple))
        or any(not isinstance(item, Mapping) for item in artifacts)
    ):
        raise ValueError(
            "factor role terminal queue binding contradicts its request facts"
        )
    return {
        "seed_key": request.seed_binding_identity,
        "requested_by_ref": requester,
        "recursive_path": list(request.recursive_path),
        "checked_evidence_refs": list(supporting_refs),
        "task_obligations": _thaw(request.task_obligations),
        "artifact_evidence_envelopes": _thaw(artifacts),
    }


def _canonical_factor_role_action_binding(
    *,
    judgment: FactorRoleJudgment,
    request_projection: Mapping[str, Any],
    action_projection: Mapping[str, Any],
    require_completed: bool,
) -> JsonDict:
    from .causal_judge import (
        factor_role_request_from_projection,
        factor_role_request_projection_identity,
        parse_factor_role_judgment,
        validate_factor_role_request_projection,
    )

    persisted_judgment = FactorRoleJudgment.from_dict(
        judgment.to_dict()
    )
    canonical_request = validate_factor_role_request_projection(
        request_projection
    )
    request = factor_role_request_from_projection(canonical_request)
    response = persisted_judgment.to_dict()
    response.pop("judgment_identity")
    canonical_judgment = parse_factor_role_judgment(
        response,
        request=request,
    )
    if (
        not isinstance(action_projection, Mapping)
        or {str(key) for key in action_projection}
        != set(_FACTOR_ROLE_PUBLICATION_ACTION_KEYS)
    ):
        raise ValueError(
            "factor role publication action has an inexact schema"
        )
    action = _thaw(action_projection)
    expected_active_role = active_failure_factor_role_for(
        canonical_judgment.factor_role
    )
    role_binding = (
        ActiveFailureRoleBinding.from_dict(
            action.get("active_role_binding")
        )
        if action.get("active_role_binding") is not None
        else None
    )
    queue_binding = canonical_factor_role_queue_binding_snapshot(
        value=action.get("queue_binding"),
        request_projection=canonical_request,
    )
    operation = action.get("operation")
    if operation not in {"factor_role_completed", "factor_role_failed"}:
        raise ValueError("factor role publication action is not terminal")
    request_identity = factor_role_request_projection_identity(
        canonical_request
    )
    owner = LocalStateOwner.from_dict(action.get("owner"))
    facts = canonical_request["facts"]
    defect_state = DefectState.from_dict(dict(facts["defect_state"]))
    if (
        action.get("semantic_key")
        != "factor_role:{0}".format(request_identity)
        or action.get("origin") != "global_candidate_factor_assessment"
        or action.get("candidate_ref") != facts["candidate_ref"]
        or action.get("hypothesis_id") != facts["hypothesis_id"]
        or action.get("defect_fingerprint") != defect_state.fingerprint
        or action.get("seed_binding_identity")
        != facts["seed_binding_identity"]
        or action.get("request_projection") != canonical_request
        or action.get("request_identity") != request_identity
        or action.get("judgment") != canonical_judgment.to_dict()
        or action.get("judgment_identity")
        != canonical_judgment.judgment_identity
        or action.get("queue_binding") != queue_binding
        or owner.seed_binding_identity != facts["seed_binding_identity"]
        or owner.hypothesis_id != facts["hypothesis_id"]
        or type(action.get("physical_requests_reserved")) is not int
        or action["physical_requests_reserved"] < 0
        or type(action.get("physical_request_delta")) is not int
        or action["physical_request_delta"] < 0
        or type(action.get("physical_request_exact")) is not bool
    ):
        raise ValueError(
            "factor role publication action contradicts request, response, "
            "owner, or canonical Global factor assessment provenance"
        )
    if expected_active_role is None:
        if role_binding is not None:
            raise ValueError(
                "non-causal factor publication cannot claim an active "
                "failure binding"
            )
    else:
        if role_binding is None:
            raise ValueError(
                "factor role publication contradicts its independent active "
                "failure role"
            )
        role_binding = canonical_active_failure_role_request_binding(
            active_role_binding=role_binding.to_dict(),
            request_projection=canonical_request,
            disposition="factor",
            causal_role=expected_active_role,
        )
    if any(
        getattr(canonical_judgment, field_name) != expected
        for field_name, expected in (
            ("candidate_ref", facts["candidate_ref"]),
            ("hypothesis_id", facts["hypothesis_id"]),
            (
                "hypothesis_semantic_hash",
                facts["hypothesis_semantic_hash"],
            ),
            ("defect_fingerprint", defect_state.fingerprint),
            ("seed_binding_identity", facts["seed_binding_identity"]),
            ("analysis_perspective", facts["analysis_perspective"]),
            ("request_identity", request_identity),
        )
    ) or canonical_judgment.recursive_path != tuple(
        facts["recursive_path"]
    ):
        raise ValueError(
            "factor role publication judgment contradicts request facts"
        )
    grounded_refs = _factor_role_publication_fact_refs(
        canonical_request
    )
    mechanism_refs = {
        ref
        for ref in (
            canonical_judgment.factor_mechanism.get("source_ref"),
            canonical_judgment.factor_mechanism.get("target_ref"),
        )
        if type(ref) is str and ref
    }
    if (
        not set(canonical_judgment.evidence_refs).issubset(grounded_refs)
        or not set(canonical_judgment.recursive_path).issubset(
            grounded_refs
        )
        or not mechanism_refs.issubset(grounded_refs)
    ):
        raise ValueError(
            "factor role publication evidence, path, or mechanism is not request-grounded"
        )
    failure_classification = action.get("failure_classification")
    reserved = action["physical_requests_reserved"]
    delta = action["physical_request_delta"]
    exact = action["physical_request_exact"]
    within_reservation = delta <= reserved
    if operation == "factor_role_completed":
        terminal_reachable = (
            failure_classification == "none"
            and exact is True
            and within_reservation
        )
    elif (
        canonical_judgment.necessity_status == "unknown"
        and canonical_judgment.factor_role == "unknown"
    ):
        if failure_classification == "accounting_breach":
            terminal_reachable = exact is True and delta > reserved
        elif failure_classification == "interrupted":
            terminal_reachable = (
                exact is False and delta == 0 and within_reservation
            )
        elif failure_classification in {
            "bounded_provider_failure",
            "judgment_invalid",
        }:
            terminal_reachable = exact is True and within_reservation
        elif failure_classification in {
            "provider_failure",
            "capability_error",
        }:
            terminal_reachable = (
                (exact is True and reserved == 0 and delta == 0)
                or (exact is False and delta == reserved)
            )
        else:
            terminal_reachable = False
    else:
        terminal_reachable = False
    if not terminal_reachable:
        raise ValueError(
            "factor role publication action terminal state is unreachable"
        )
    if require_completed and operation != "factor_role_completed":
        raise ValueError(
            "factor role publication requires one successful completed action"
        )
    return action


def canonical_factor_role_escalation_binding(
    *,
    factor_action_projection: Mapping[str, Any],
    root_queue_entry: Mapping[str, Any],
    root_action_projection: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    raw_judgment = (
        factor_action_projection.get("judgment")
        if isinstance(factor_action_projection, Mapping)
        else None
    )
    if not isinstance(raw_judgment, Mapping):
        raise ValueError(
            "factor role escalation source has no judgment"
        )
    judgment = FactorRoleJudgment.from_dict(dict(raw_judgment))
    factor_action = _canonical_factor_role_action_binding(
        judgment=judgment,
        request_projection=factor_action_projection.get(
            "request_projection"
        ),
        action_projection=factor_action_projection,
        require_completed=True,
    )
    if (
        judgment.necessity_status != "necessary"
        or judgment.factor_role != "unknown"
    ):
        raise ValueError(
            "factor role escalation requires a completed necessary judgment"
        )
    if not isinstance(root_queue_entry, Mapping):
        raise ValueError(
            "factor role escalation requires one root queue entry"
        )
    origin = canonical_factor_role_escalation_origin(
        root_queue_entry.get("origin")
    )
    expected_origin = {
        "kind": "factor_role_escalation",
        "factor_action_identity": factor_action["semantic_key"],
        "factor_judgment_identity": judgment.judgment_identity,
        "factor_request_identity": judgment.request_identity,
    }
    if origin != expected_origin:
        raise ValueError(
            "factor role escalation origin contradicts its source identities"
        )

    from .causal_judge import (
        root_confirmation_request_projection_identity,
        validate_root_confirmation_request_projection,
    )

    root_projection = validate_root_confirmation_request_projection(
        root_queue_entry.get("factual_request_projection")
    )
    root_request_identity = (
        root_confirmation_request_projection_identity(root_projection)
    )
    root_facts = root_projection["facts"]
    root_defect = DefectState.from_dict(dict(root_facts["defect_state"]))
    expected_binding = (
        judgment.candidate_ref,
        judgment.hypothesis_id,
        judgment.hypothesis_semantic_hash,
        judgment.defect_fingerprint,
        judgment.seed_binding_identity,
        judgment.analysis_perspective,
        judgment.recursive_path,
    )
    queue_binding = (
        str(root_queue_entry.get("candidate_ref") or ""),
        str(root_queue_entry.get("hypothesis_id") or ""),
        str(root_queue_entry.get("hypothesis_semantic_hash") or ""),
        str(root_queue_entry.get("defect_fingerprint") or ""),
        str(root_queue_entry.get("seed_binding_identity") or ""),
        str(root_queue_entry.get("analysis_perspective") or ""),
        tuple(root_queue_entry.get("recursive_path") or ()),
    )
    request_binding = (
        str(root_facts["candidate_ref"]),
        str(root_facts["hypothesis_id"]),
        str(root_facts["hypothesis_semantic_hash"]),
        root_defect.fingerprint,
        str(root_facts["seed_binding_identity"]),
        str(root_facts["analysis_perspective"]),
        tuple(root_facts["recursive_path"]),
    )
    factor_owner = LocalStateOwner.from_dict(
        factor_action.get("owner")
    ).to_dict()
    queue_owner = LocalStateOwner.from_dict(
        root_queue_entry.get("owner")
    ).to_dict()
    if queue_owner != factor_owner:
        raise ValueError(
            "factor role escalation queue owner does not exactly match "
            "the completed source action owner"
        )
    if (
        root_queue_entry.get("review_scope") != "root"
        or queue_binding != expected_binding
        or request_binding != expected_binding
        or str(root_queue_entry.get("semantic_identity") or "")
        != root_request_identity
    ):
        raise ValueError(
            "factor role escalation root request belongs to another "
            "seed, hypothesis, defect, candidate, or perspective"
        )

    root_action = None
    if root_action_projection is not None:
        if not isinstance(root_action_projection, Mapping):
            raise ValueError(
                "factor role escalation root action must be an object"
            )
        root_action = _thaw(root_action_projection)
        root_action_owner = LocalStateOwner.from_dict(
            root_action.get("owner")
        ).to_dict()
        if root_action_owner != factor_owner:
            raise ValueError(
                "factor role escalation root action owner does not exactly "
                "match the completed source action owner"
            )
        if (
            canonical_factor_role_escalation_origin(
                root_action.get("origin")
            )
            != expected_origin
            or root_action.get("review_scope") != "root"
            or root_action.get("candidate_ref") != judgment.candidate_ref
            or root_action.get("hypothesis_id") != judgment.hypothesis_id
            or root_action.get("hypothesis_semantic_hash")
            != judgment.hypothesis_semantic_hash
            or root_action.get("defect_fingerprint")
            != judgment.defect_fingerprint
            or root_action.get("seed_binding_identity")
            != judgment.seed_binding_identity
            or root_action.get("request_identity")
            != root_request_identity
            or root_action.get("factual_request_projection")
            != root_projection
            or root_queue_entry.get("status")
            != root_action.get("status")
            or root_queue_entry.get("confirmation")
            != root_action.get("confirmation")
            or root_queue_entry.get("response_identity")
            != root_action.get("response_identity")
        ):
            raise ValueError(
                "factor role escalation root action contradicts its "
                "authoritative source and request"
            )
    return {
        "origin": expected_origin,
        "factor_action": factor_action,
        "factor_judgment": judgment.to_dict(),
        "root_request_identity": root_request_identity,
        "root_request_projection": root_projection,
        "root_action": root_action,
    }


def canonical_factor_role_escalation_gap(
    *,
    factor_action_projection: Mapping[str, Any],
    root_queue_entry: Mapping[str, Any],
    root_action_projection: Mapping[str, Any],
) -> JsonDict:
    binding = canonical_factor_role_escalation_binding(
        factor_action_projection=factor_action_projection,
        root_queue_entry=root_queue_entry,
        root_action_projection=root_action_projection,
    )
    root_action = binding["root_action"]
    confirmation = RootConfirmation.from_dict(
        dict(root_action["confirmation"])
    )
    if confirmation.status not in {"rejected", "unknown"}:
        raise ValueError(
            "factor role escalation gap requires a rejected or unknown "
            "RootConfirmation"
        )
    judgment = FactorRoleJudgment.from_dict(
        binding["factor_judgment"]
    )
    return {
        "candidate_ref": judgment.candidate_ref,
        "hypothesis_id": judgment.hypothesis_id,
        "defect_fingerprint": judgment.defect_fingerprint,
        "seed_binding_identity": judgment.seed_binding_identity,
        "factor_action_identity": binding["origin"][
            "factor_action_identity"
        ],
        "factor_judgment_identity": judgment.judgment_identity,
        "factor_request_identity": judgment.request_identity,
        "root_action_identity": root_action["semantic_key"],
        "root_request_identity": root_action["request_identity"],
        "root_response_identity": confirmation.response_identity,
        "status": confirmation.status,
        "reason": confirmation.reason,
        "owner": _thaw(root_action["owner"]),
        "origin": _thaw(binding["origin"]),
    }


def canonical_factor_role_gap(
    *,
    judgment: FactorRoleJudgment,
    action_projection: Mapping[str, Any],
) -> JsonDict:
    """Project the sole audit gap owned by an unknown terminal lifecycle."""
    canonical_judgment = FactorRoleJudgment.from_dict(
        judgment.to_dict()
    )
    action = _canonical_factor_role_action_binding(
        judgment=canonical_judgment,
        request_projection=action_projection.get("request_projection"),
        action_projection=action_projection,
        require_completed=False,
    )
    if canonical_judgment.factor_role != "unknown":
        raise ValueError(
            "factor role audit gap requires an unknown terminal judgment"
        )
    return {
        "candidate_ref": canonical_judgment.candidate_ref,
        "hypothesis_id": canonical_judgment.hypothesis_id,
        "defect_fingerprint": canonical_judgment.defect_fingerprint,
        "seed_binding_identity": (
            canonical_judgment.seed_binding_identity
        ),
        "request_identity": canonical_judgment.request_identity,
        "judgment_identity": canonical_judgment.judgment_identity,
        "reason": canonical_judgment.reason,
        "failure_classification": action["failure_classification"],
        "owner": _thaw(action["owner"]),
        "origin": action["origin"],
    }


def canonical_terminal_factor_role_queue_binding(
    *,
    queue_entry: Mapping[str, Any],
    action_projection: Mapping[str, Any],
    judgment: Optional[FactorRoleJudgment] = None,
) -> JsonDict:
    """Bind a terminal non-root queue entry to one authoritative action."""
    from .causal_judge import factor_role_request_from_projection

    if judgment is None:
        raw_judgment = (
            action_projection.get("judgment")
            if isinstance(action_projection, Mapping)
            else None
        )
        if not isinstance(raw_judgment, Mapping):
            raise ValueError(
                "terminal factor role action has no judgment projection"
            )
        judgment = FactorRoleJudgment.from_dict(dict(raw_judgment))
    canonical_judgment = FactorRoleJudgment.from_dict(judgment.to_dict())
    action = _canonical_factor_role_action_binding(
        judgment=canonical_judgment,
        request_projection=action_projection.get("request_projection"),
        action_projection=action_projection,
        require_completed=False,
    )
    if not isinstance(queue_entry, Mapping):
        raise ValueError("terminal factor role queue entry must be an object")
    actual_keys = {str(key) for key in queue_entry}
    missing = TERMINAL_FACTOR_ROLE_QUEUE_REQUIRED_KEYS - actual_keys
    extra = actual_keys - TERMINAL_FACTOR_ROLE_QUEUE_ALLOWED_KEYS
    if missing or extra or any(type(key) is not str for key in queue_entry):
        raise ValueError(
            "terminal factor role queue schema mismatch "
            "(missing={0}, extra={1})".format(
                sorted(missing),
                sorted(extra),
            )
        )

    queue = _thaw(queue_entry)
    request = factor_role_request_from_projection(
        action["request_projection"]
    )
    expected_status = (
        "completed"
        if action["operation"] == "factor_role_completed"
        else "failed"
    )
    expected_bindings = {
        "candidate_ref": request.candidate_ref,
        "hypothesis_id": request.hypothesis_id,
        "hypothesis_semantic_hash": request.hypothesis_semantic_hash,
        "defect_fingerprint": request.defect_state.fingerprint,
        "seed_binding_identity": request.seed_binding_identity,
        "semantic_identity": action["request_identity"],
        "status": expected_status,
        "owner": action["owner"],
        "analysis_perspective": request.analysis_perspective,
        "factual_request_projection": action["request_projection"],
        "review_scope": "non_root",
        "origin": action["origin"],
        "factor_role_judgment": canonical_judgment.to_dict(),
        "response_identity": canonical_judgment.judgment_identity,
        "failure_classification": action["failure_classification"],
        **action["queue_binding"],
    }
    if any(queue.get(key) != value for key, value in expected_bindings.items()):
        raise ValueError(
            "terminal factor role queue contradicts its authoritative "
            "request, action, judgment, owner, origin, or lifecycle"
        )
    if (
        expected_status == "completed"
        and queue["failure_classification"] != "none"
    ) or (
        expected_status == "failed"
        and queue["failure_classification"] == "none"
    ):
        raise ValueError(
            "terminal factor role queue status and failure classification "
            "are inconsistent"
        )
    return queue


def canonical_factor_role_publication_provenance(
    *,
    judgment: FactorRoleJudgment,
    action_projection: Mapping[str, Any],
) -> JsonDict:
    owner = LocalStateOwner.from_dict(action_projection.get("owner"))
    return {
        "publication_contract": FACTOR_ROLE_PUBLICATION_CONTRACT_VERSION,
        "request_identity": judgment.request_identity,
        "response_identity": judgment.judgment_identity,
        "judgment_identity": judgment.judgment_identity,
        "action_semantic_key": action_projection["semantic_key"],
        "action_operation": action_projection["operation"],
        "origin": action_projection["origin"],
        "owner": owner.to_dict(),
        "hypothesis_id": judgment.hypothesis_id,
        "defect_fingerprint": judgment.defect_fingerprint,
        "seed_binding_identity": judgment.seed_binding_identity,
        "analysis_perspective": judgment.analysis_perspective,
    }


def canonical_factor_role_publication(
    *,
    judgment: FactorRoleJudgment,
    request_projection: Mapping[str, Any],
    action_projection: Mapping[str, Any],
    publication: Optional[Any] = None,
) -> Any:
    """Project one completed role action into its sole public representation."""
    canonical_judgment = FactorRoleJudgment.from_dict(
        judgment.to_dict()
    )
    action = _canonical_factor_role_action_binding(
        judgment=canonical_judgment,
        request_projection=request_projection,
        action_projection=action_projection,
        require_completed=True,
    )
    provenance = canonical_factor_role_publication_provenance(
        judgment=canonical_judgment,
        action_projection=action,
    )
    role_binding = (
        ActiveFailureRoleBinding.from_dict(
            action["active_role_binding"]
        )
        if action["active_role_binding"] is not None
        else None
    )
    if role_binding is not None:
        provenance["active_role_binding"] = role_binding.to_dict()
    if (
        canonical_judgment.necessity_status == "necessary"
        and canonical_judgment.factor_role == "unknown"
    ):
        expected = None
    elif canonical_judgment.factor_role in {
        "contributing_condition",
        "amplifying_factor",
    }:
        expected = CausalFactor(
            node_ref=canonical_judgment.candidate_ref,
            relation=canonical_judgment.factor_role,
            reason=canonical_judgment.reason,
            confidence=canonical_judgment.confidence,
            evidence_refs=canonical_judgment.evidence_refs,
            recursive_path=canonical_judgment.recursive_path,
            factor_label=(
                role_binding.causal_role
                if role_binding is not None
                else canonical_factor_label(
                    canonical_judgment.factor_role,
                    canonical_judgment.analysis_perspective,
                )
            ),
            confirmation_status=canonical_judgment.necessity_status,
            confirmation=canonical_judgment.to_dict(),
            provenance=provenance,
            mechanism=dict(canonical_judgment.factor_mechanism),
        )
    elif canonical_judgment.factor_role == "downstream_materialization":
        expected = CausalMaterialization(
            candidate_ref=canonical_judgment.candidate_ref,
            recursive_path=canonical_judgment.recursive_path,
            evidence_refs=canonical_judgment.evidence_refs,
            reason=canonical_judgment.reason,
            confidence=canonical_judgment.confidence,
            role_judgment=canonical_judgment.to_dict(),
            provenance=provenance,
            materialization_mechanism=dict(
                canonical_judgment.factor_mechanism
            ),
        )
    elif canonical_judgment.factor_role == "unrelated":
        expected = RejectedCandidate(
            node_ref=canonical_judgment.candidate_ref,
            reason=canonical_judgment.reason,
            evidence_refs=canonical_judgment.evidence_refs,
            hypothesis_id=canonical_judgment.hypothesis_id,
            recursive_path=canonical_judgment.recursive_path,
            confirmation_status=canonical_judgment.necessity_status,
            confidence=canonical_judgment.confidence,
            confirmation=canonical_judgment.to_dict(),
            provenance=provenance,
        )
    elif canonical_judgment.factor_role == "unknown":
        expected = {
            "candidate_ref": canonical_judgment.candidate_ref,
            "reason": canonical_judgment.reason,
            "confidence": canonical_judgment.confidence,
            "evidence_refs": list(canonical_judgment.evidence_refs),
            "recursive_path": list(canonical_judgment.recursive_path),
            "necessity_status": canonical_judgment.necessity_status,
            "factor_role": canonical_judgment.factor_role,
            "request_identity": canonical_judgment.request_identity,
            "judgment_identity": canonical_judgment.judgment_identity,
            "provenance": provenance,
        }
    else:
        raise ValueError("factor role has no canonical publication")

    if publication is not None:
        supplied = (
            publication.to_dict()
            if hasattr(publication, "to_dict")
            else _thaw(publication)
        )
        canonical_payload = (
            expected.to_dict()
            if hasattr(expected, "to_dict")
            else _thaw(expected)
        )
        if stable_json(supplied) != stable_json(canonical_payload):
            raise ValueError(
                "caller-supplied factor role publication diverges from terminal facts"
            )
    return expected


@dataclass(frozen=True)
class SeedAttributionResult:
    start_ref: str
    defect_fingerprint: str
    defect_state: DefectState
    outcome: str
    candidate_refs: Tuple[str, ...] = field(default_factory=tuple)
    selected_candidate_refs: Tuple[str, ...] = field(default_factory=tuple)
    confirmation_identities: Tuple[str, ...] = field(default_factory=tuple)
    confirmed_root_refs: Tuple[str, ...] = field(default_factory=tuple)
    decisive_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    decisive_evidence: Tuple[JsonDict, ...] = field(default_factory=tuple)
    missing_evidence: Tuple[str, ...] = field(default_factory=tuple)
    blocking_reasons: Tuple[str, ...] = field(default_factory=tuple)
    global_judgment: JsonDict = field(default_factory=FrozenMapping)
    expansion_history: Tuple[JsonDict, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.outcome not in {
            "confirmed_root",
            "no_defect",
            "evidence_gap",
            "inconclusive",
        }:
            raise ValueError("unsupported per-seed attribution outcome")
        if not self.start_ref:
            raise ValueError("per-seed attribution requires start_ref")
        if self.defect_fingerprint != self.defect_state.fingerprint:
            raise ValueError("per-seed defect fingerprint contradicts defect_state")
        for name in (
            "candidate_refs",
            "selected_candidate_refs",
            "confirmation_identities",
            "confirmed_root_refs",
            "decisive_evidence_refs",
        ):
            object.__setattr__(
                self,
                name,
                tuple(sorted(set(_frozen_strings(getattr(self, name))))),
            )
        for name in ("missing_evidence", "blocking_reasons"):
            object.__setattr__(
                self,
                name,
                tuple(sorted(set(_concrete_seed_strings(getattr(self, name), name)))),
            )
        object.__setattr__(
            self,
            "global_judgment",
            FrozenMapping(_thaw(self.global_judgment)),
        )
        object.__setattr__(
            self,
            "expansion_history",
            tuple(FrozenMapping(_thaw(item)) for item in self.expansion_history),
        )
        object.__setattr__(
            self,
            "decisive_evidence",
            tuple(FrozenMapping(_thaw(item)) for item in self.decisive_evidence),
        )
        validate_seed_outcome_payload(
            outcome=self.outcome,
            confirmed_root_refs=self.confirmed_root_refs,
            missing_evidence=self.missing_evidence,
            blocking_reasons=self.blocking_reasons,
        )

    @property
    def seed_binding_identity(self) -> str:
        return seed_binding_identity_for(self.start_ref, self.defect_fingerprint)

    def to_dict(self) -> JsonDict:
        return {
            "start_ref": self.start_ref,
            "defect_fingerprint": self.defect_fingerprint,
            "seed_binding_identity": self.seed_binding_identity,
            "defect_state": self.defect_state.to_dict(),
            "outcome": self.outcome,
            "candidate_refs": list(self.candidate_refs),
            "selected_candidate_refs": list(self.selected_candidate_refs),
            "confirmation_identities": list(self.confirmation_identities),
            "confirmed_root_refs": list(self.confirmed_root_refs),
            "decisive_evidence_refs": list(self.decisive_evidence_refs),
            "decisive_evidence": [_thaw(item) for item in self.decisive_evidence],
            "missing_evidence": list(self.missing_evidence),
            "blocking_reasons": list(self.blocking_reasons),
            "global_judgment": _thaw(self.global_judgment),
            "expansion_history": [_thaw(item) for item in self.expansion_history],
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "SeedAttributionResult":
        for field_name in (
            "candidate_refs",
            "selected_candidate_refs",
            "confirmation_identities",
            "confirmed_root_refs",
            "decisive_evidence_refs",
            "missing_evidence",
            "blocking_reasons",
        ):
            raw_items = value.get(field_name)
            if (
                isinstance(raw_items, (list, tuple))
                and all(isinstance(item, str) for item in raw_items)
                and len(raw_items) != len(set(raw_items))
            ):
                raise ValueError(
                    "persisted seed attribution {0} contains duplicates".format(
                        field_name
                    )
                )
        defect_state = DefectState.from_dict(_json_dict(value.get("defect_state")))
        global_judgment = _json_dict(value.get("global_judgment"))
        candidate_refs = _string_list(value.get("candidate_refs"))
        selected_candidate_refs = _string_list(value.get("selected_candidate_refs"))
        decisive_evidence_refs = _string_list(value.get("decisive_evidence_refs"))
        expected_seed_binding = seed_binding_identity_for(
            str(value.get("start_ref") or ""),
            str(value.get("defect_fingerprint") or ""),
        )
        if value.get("seed_binding_identity") != expected_seed_binding:
            raise ValueError("persisted seed binding identity is missing or inconsistent")
        decisive_evidence = []
        for item in value.get("decisive_evidence") or ():
            if not isinstance(item, Mapping) or set(item) != {"ref", "owner"}:
                raise ValueError("persisted decisive evidence owner schema mismatch")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.seed_binding_identity != expected_seed_binding:
                raise ValueError("persisted decisive evidence has the wrong seed owner")
            decisive_evidence.append(
                {"ref": str(item.get("ref") or ""), "owner": owner.to_dict()}
            )
        if {
            str(item.get("ref") or "") for item in decisive_evidence
        } != set(decisive_evidence_refs):
            raise ValueError("persisted decisive evidence aggregate is inconsistent")
        for item in value.get("expansion_history") or ():
            if not isinstance(item, Mapping):
                raise ValueError("persisted expansion history must contain objects")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.seed_binding_identity != expected_seed_binding:
                raise ValueError("persisted expansion history has the wrong seed owner")
        _validate_persisted_global_judgment(
            global_judgment,
            start_ref=str(value.get("start_ref") or ""),
            defect_state=defect_state,
            candidate_refs=candidate_refs,
            selected_candidate_refs=selected_candidate_refs,
            decisive_evidence_refs=decisive_evidence_refs,
            seed_binding_identity=expected_seed_binding,
        )
        return cls(
            start_ref=str(value.get("start_ref") or ""),
            defect_fingerprint=str(value.get("defect_fingerprint") or ""),
            defect_state=defect_state,
            outcome=str(value.get("outcome") or "inconclusive"),
            candidate_refs=candidate_refs,
            selected_candidate_refs=selected_candidate_refs,
            confirmation_identities=_string_list(value.get("confirmation_identities")),
            confirmed_root_refs=_string_list(value.get("confirmed_root_refs")),
            decisive_evidence_refs=decisive_evidence_refs,
            decisive_evidence=tuple(decisive_evidence),
            missing_evidence=_seed_json_string_list(
                value.get("missing_evidence"), "missing_evidence"
            ),
            blocking_reasons=_seed_json_string_list(
                value.get("blocking_reasons"), "blocking_reasons"
            ),
            global_judgment=global_judgment,
            expansion_history=tuple(
                item
                for item in value.get("expansion_history", [])
                if isinstance(item, dict)
            ),
        )


def _validate_persisted_global_judgment(
    value: JsonDict,
    *,
    start_ref: str,
    defect_state: DefectState,
    candidate_refs: Iterable[str],
    selected_candidate_refs: Iterable[str],
    decisive_evidence_refs: Iterable[str],
    seed_binding_identity: str,
) -> None:
    if not value:
        return
    if value.get("schema_version") != GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION:
        raise ValueError(
            "persisted global judgment requires current migration or rejudgment"
        )
    required = {
        "schema_version",
        "outcome",
        "reason",
        "assessments",
        "selected_candidate_refs",
        "expansion_requests",
        "decisive_evidence_refs",
        "missing_evidence",
        "confidence",
        "active_focus_binding",
        "validation_envelope",
        "owner",
    }
    if set(value) != required:
        raise ValueError("persisted global judgment schema is incomplete")
    owner = LocalStateOwner.from_dict(value.get("owner"))
    if owner.seed_binding_identity != seed_binding_identity:
        raise ValueError("persisted global judgment has the wrong seed owner")
    from .global_judge import (
        global_candidate_request_from_validation_envelope,
        validate_global_candidate_payload,
    )

    try:
        request = global_candidate_request_from_validation_envelope(
            value.get("validation_envelope")
        )
        if request.seed_ref != start_ref or request.active_defect != defect_state:
            raise ValueError("validation envelope drifts from persisted seed facts")
        judgment = validate_global_candidate_payload(
            {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key not in {"schema_version", "validation_envelope", "owner"}
            },
            request=request,
        )
        if not set(request.offered_candidate_refs).issubset(set(candidate_refs)):
            raise ValueError("validation envelope candidates drift from seed candidates")
        if set(judgment.selected_candidate_refs) != set(selected_candidate_refs):
            raise ValueError("selected roots drift from persisted seed selection")
        if not set(judgment.decisive_evidence_refs).issubset(
            set(decisive_evidence_refs)
        ):
            raise ValueError("decisive refs drift from persisted seed evidence")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "persisted global judgment v4 semantic validation failed: {0}".format(
                exc
            )
        ) from exc


def factor_escalation_outperformed_confirmation_identities(
    *,
    confirmations: Iterable[RootConfirmation],
    published_roots: Iterable[ConfirmedRoot],
    confirmation_queue: Iterable[Mapping[str, Any]],
) -> Set[str]:
    confirmations_by_identity = {
        item.confirmation_identity: item for item in confirmations
    }
    queue_by_confirmation_identity: Dict[
        str, List[Mapping[str, Any]]
    ] = {}
    for queued in confirmation_queue:
        raw_confirmation = queued.get("confirmation")
        if not isinstance(raw_confirmation, Mapping):
            continue
        identity = RootConfirmation.from_dict(
            dict(raw_confirmation)
        ).confirmation_identity
        queue_by_confirmation_identity.setdefault(identity, []).append(
            queued
        )

    resolved: Set[str] = set()
    for root in published_roots:
        source = RootConfirmation.from_dict(dict(root.confirmation))
        source_queue = queue_by_confirmation_identity.get(
            source.confirmation_identity, ()
        )
        if (
            len(source_queue) != 1
            or not is_factor_role_escalation_origin(
                source_queue[0].get("origin")
            )
        ):
            continue
        for comparison in source.competitor_comparisons:
            target_identity = str(
                comparison.get("confirmation_identity") or ""
            )
            target = confirmations_by_identity.get(target_identity)
            target_queue = queue_by_confirmation_identity.get(
                target_identity, ()
            )
            if (
                target is None
                or is_definitive_confirmation(target)
                or target.seed_binding_identity
                != source.seed_binding_identity
                or len(target_queue) != 1
                or str(target_queue[0].get("review_scope") or "root")
                != "root"
                or is_factor_role_escalation_origin(
                    target_queue[0].get("origin")
                )
                or comparison.get("requires_independent_confirmation")
                is not True
                or str(comparison.get("status") or "")
                not in {"outperformed", "rejected"}
            ):
                continue
            resolved.add(target_identity)
    return resolved


def validate_confirmation_ownership(
    confirmations: Iterable[RootConfirmation],
    seed_results: Iterable[SeedAttributionResult],
    *,
    label: str,
    non_blocking_unresolved_confirmation_identities: Iterable[str] = (),
) -> None:
    """Require an exact two-way link between confirmations and seed entries."""
    confirmation_list = tuple(confirmations)
    seeds = tuple(seed_results)
    non_blocking_unresolved = {
        str(identity)
        for identity in non_blocking_unresolved_confirmation_identities
        if str(identity)
    }
    confirmations_by_identity: Dict[str, List[RootConfirmation]] = {}
    for confirmation in confirmation_list:
        confirmations_by_identity.setdefault(
            confirmation.confirmation_identity, []
        ).append(confirmation)
    seeds_by_binding: Dict[str, List[SeedAttributionResult]] = {}
    for seed in seeds:
        seeds_by_binding.setdefault(
            seed_binding_identity_for(seed.start_ref, seed.defect_fingerprint), []
        ).append(seed)
        if len(seed.confirmation_identities) != len(
            set(seed.confirmation_identities)
        ):
            raise ValueError(
                "{0} confirmation ownership contains duplicates; each published root "
                "must belong to exactly one confirmed_root seed".format(label)
            )
        for identity in seed.confirmation_identities:
            matches = confirmations_by_identity.get(identity, [])
            if (
                len(matches) != 1
                or matches[0].seed_binding_identity
                != seed_binding_identity_for(
                    seed.start_ref, seed.defect_fingerprint
                )
                or not matches[0].recursive_path
                or matches[0].recursive_path[-1] != seed.start_ref
            ):
                raise ValueError(
                    "{0} confirmation ownership is not bidirectional: confirmation "
                    "identities must individually bind to their seed; each published "
                    "root must belong to exactly one confirmed_root seed".format(label)
                )
    for identity, matches in confirmations_by_identity.items():
        if len(matches) != 1:
            raise ValueError(
                "{0} confirmation ownership is not unique; each published root must "
                "belong to exactly one confirmed_root seed".format(label)
            )
        confirmation = matches[0]
        owners = seeds_by_binding.get(confirmation.seed_binding_identity, [])
        if (
            len(owners) != 1
            or identity not in owners[0].confirmation_identities
            or not confirmation.recursive_path
            or confirmation.recursive_path[-1] != owners[0].start_ref
        ):
            raise ValueError(
                "{0} confirmation ownership is not bidirectional: confirmation identities "
                "must individually bind to their seed; each published root must belong "
                "to exactly one confirmed_root seed".format(label)
            )
        owner = owners[0]
        if (
            not is_definitive_confirmation(confirmation)
            and identity not in non_blocking_unresolved
            and (
                owner.outcome not in {"evidence_gap", "inconclusive"}
                or not (owner.missing_evidence or owner.blocking_reasons)
            )
        ):
            raise ValueError(
                "{0} unresolved confirmation requires an evidence_gap or inconclusive "
                "owning seed with concrete blocking or missing-evidence facts".format(
                    label
                )
            )
    unknown_identities = {
        confirmation.confirmation_identity
        for confirmation in confirmation_list
        if not is_definitive_confirmation(confirmation)
    }
    if not non_blocking_unresolved.issubset(unknown_identities):
        raise ValueError(
            "{0} non-blocking unresolved confirmation identities must refer "
            "to unknown confirmations".format(label)
        )


def validate_seed_outcome_payload(
    *,
    outcome: str,
    confirmed_root_refs: Iterable[str],
    missing_evidence: Iterable[str],
    blocking_reasons: Iterable[str],
) -> None:
    """Reject terminal seed payloads that contradict their declared outcome."""
    roots = tuple(confirmed_root_refs)
    unresolved_facts = _concrete_seed_strings(missing_evidence, "missing_evidence")
    blockers = _concrete_seed_strings(blocking_reasons, "blocking_reasons")
    if outcome in {"confirmed_root", "no_defect"} and (
        unresolved_facts or blockers
    ):
        raise ValueError(
            "seed outcome payload cannot combine a terminal outcome with unresolved evidence"
        )
    if outcome == "evidence_gap" and not unresolved_facts:
        raise ValueError(
            "seed outcome payload requires concrete unresolved evidence for evidence_gap"
        )
    if outcome != "confirmed_root" and roots:
        raise ValueError(
            "seed outcome payload cannot publish confirmed_root_refs for a non-confirmed outcome"
        )


def _aggregate_seed_outcomes(
    seed_results: Tuple[SeedAttributionResult, ...],
) -> str:
    outcomes = tuple(item.outcome for item in seed_results)
    if outcomes and all(item == "no_defect" for item in outcomes):
        return "no_defect"
    if outcomes and all(
        item in {"confirmed_root", "no_defect"} for item in outcomes
    ) and "confirmed_root" in outcomes:
        return "confirmed_root"
    if outcomes and "inconclusive" not in outcomes and len(set(outcomes)) > 1:
        return "partial"
    return "inconclusive"


def validate_modern_report_shape(value: Any) -> None:
    """Validate the exact serialized container shape of a modern report."""
    if not isinstance(value, Mapping):
        raise TypeError("modern report must be an object")
    non_string_keys = [repr(key) for key in value if type(key) is not str]
    if non_string_keys:
        raise TypeError(
            "modern report top-level keys must be strings; invalid={0}".format(
                sorted(non_string_keys)
            )
        )
    actual_keys = set(value)
    if actual_keys != set(MODERN_REPORT_KEYS):
        missing = sorted(set(MODERN_REPORT_KEYS) - actual_keys)
        unknown = sorted(actual_keys - set(MODERN_REPORT_KEYS))
        raise ValueError(
            "modern report top-level schema mismatch; missing={0}, unknown={1}".format(
                missing,
                unknown,
            )
        )
    if value.get("schema_version") != MODERN_REPORT_SCHEMA_VERSION:
        raise ValueError(
            "modern report schema_version must be {0}".format(
                MODERN_REPORT_SCHEMA_VERSION
            )
        )
    for field_name in _MODERN_REPORT_STRING_FIELDS:
        if type(value.get(field_name)) is not str:
            raise TypeError(
                "modern report {0} must be a string".format(field_name)
            )
    for field_name in (
        *_MODERN_REPORT_OBJECT_COLLECTION_FIELDS,
        *_MODERN_REPORT_STRING_COLLECTION_FIELDS,
        *_MODERN_REPORT_PATH_COLLECTION_FIELDS,
    ):
        if type(value.get(field_name)) is not list:
            raise TypeError(
                "modern report {0} must be an array".format(field_name)
            )
    for field_name in _MODERN_REPORT_OBJECT_COLLECTION_FIELDS:
        if any(
            not isinstance(item, Mapping)
            for item in value[field_name]
        ):
            raise TypeError(
                "modern report {0} must contain only objects".format(
                    field_name
                )
            )
    for field_name in _MODERN_REPORT_STRING_COLLECTION_FIELDS:
        if any(type(item) is not str for item in value[field_name]):
            raise TypeError(
                "modern report {0} must contain only strings".format(
                    field_name
                )
            )
    for path in value["taint_paths"]:
        if type(path) is not list or any(type(ref) is not str for ref in path):
            raise TypeError(
                "modern report taint_paths must contain only string arrays"
            )
    if not isinstance(value.get("metadata"), Mapping):
        raise TypeError("modern report metadata must be an object")
    metadata = value["metadata"]
    for key in MODERN_FACTOR_AUDIT_METADATA_KEYS:
        if type(metadata.get(key)) is not list:
            raise ValueError(
                "modern report metadata {0} is required".format(key)
            )


def _validate_modern_factor_role_raw_surfaces(value: Mapping[str, Any]) -> None:
    annotation_keys = {
        "semantic_anchor_id",
        "semantic_occurrence_id",
    }
    public_schemas = {
        "contributing_conditions": {
            "node_ref",
            "relation",
            "reason",
            "confidence",
            "evidence_refs",
            "recursive_path",
            "factor_label",
            "confirmation_status",
            "confirmation",
            "provenance",
            "mechanism",
        },
        "amplifying_factors": {
            "node_ref",
            "relation",
            "reason",
            "confidence",
            "evidence_refs",
            "recursive_path",
            "factor_label",
            "confirmation_status",
            "confirmation",
            "provenance",
            "mechanism",
        },
        "downstream_materializations": {
            "candidate_ref",
            "recursive_path",
            "evidence_refs",
            "reason",
            "confidence",
            "role_judgment",
            "provenance",
            "materialization_mechanism",
        },
        "rejected_candidates": {
            "node_ref",
            "reason",
            "evidence_refs",
            "hypothesis_id",
            "recursive_path",
            "confirmation_status",
            "confidence",
            "confirmation",
            "provenance",
        },
    }
    for field_name, required_keys in public_schemas.items():
        for item in value[field_name]:
            actual_keys = {str(key) for key in item}
            if (
                any(type(key) is not str for key in item)
                or not required_keys.issubset(actual_keys)
                or actual_keys - required_keys - annotation_keys
            ):
                raise ValueError(
                    "modern report {0} entry has an inexact schema".format(
                        field_name
                    )
                )

    metadata = value["metadata"]
    gap_schemas = {
        "factor_confirmation_gaps": {
            "candidate_ref",
            "reason",
            "confidence",
            "evidence_refs",
            "recursive_path",
            "necessity_status",
            "factor_role",
            "request_identity",
            "judgment_identity",
            "provenance",
        },
        "factor_role_gaps": set(FACTOR_ROLE_GAP_KEYS),
        "factor_role_escalation_gaps": set(
            FACTOR_ROLE_ESCALATION_GAP_KEYS
        ),
    }
    for field_name, exact_keys in gap_schemas.items():
        for item in metadata[field_name]:
            if (
                not isinstance(item, Mapping)
                or any(type(key) is not str for key in item)
                or set(item) != exact_keys
            ):
                raise ValueError(
                    "modern report metadata {0} entry has an inexact schema".format(
                        field_name
                    )
                )


@dataclass(frozen=True)
class RecursiveAttributionReport:
    case_id: str
    objective: str
    schema_version: str = MODERN_REPORT_SCHEMA_VERSION
    start_refs: Tuple[str, ...] = field(default_factory=tuple)
    seed_results: Tuple[SeedAttributionResult, ...] = field(default_factory=tuple)
    analysis_outcome: str = "inconclusive"
    analysis_perspective: str = ""
    defect_states: Tuple[DefectState, ...] = field(default_factory=tuple)
    causal_candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)
    causal_relations: Tuple[PredecessorAssessment, ...] = field(default_factory=tuple)
    step_judgments: Tuple[CausalStepJudgment, ...] = field(default_factory=tuple)
    hypotheses: Tuple[AttributionHypothesis, ...] = field(default_factory=tuple)
    introduction_candidates: Tuple[CausalCandidate, ...] = field(default_factory=tuple)
    confirmations: Tuple[RootConfirmation, ...] = field(default_factory=tuple)
    confirmed_roots: Tuple[ConfirmedRoot, ...] = field(default_factory=tuple)
    co_roots: Tuple[ConfirmedRoot, ...] = field(default_factory=tuple)
    contributing_conditions: Tuple[CausalFactor, ...] = field(default_factory=tuple)
    amplifying_factors: Tuple[CausalFactor, ...] = field(default_factory=tuple)
    downstream_materializations: Tuple[CausalMaterialization, ...] = field(
        default_factory=tuple
    )
    rejected_candidates: Tuple[RejectedCandidate, ...] = field(default_factory=tuple)
    unresolved_hypotheses: Tuple[AttributionHypothesis, ...] = field(default_factory=tuple)
    taint_paths: Tuple[Tuple[str, ...], ...] = field(default_factory=tuple)
    visited_order: Tuple[str, ...] = field(default_factory=tuple)
    visited_entries: Tuple[JsonDict, ...] = field(default_factory=tuple)
    unresolved_refs: Tuple[str, ...] = field(default_factory=tuple)
    investigation_journal: Tuple[JsonDict, ...] = field(default_factory=tuple)
    metadata: JsonDict = field(default_factory=FrozenMapping)

    def __post_init__(self) -> None:
        if self.schema_version != MODERN_REPORT_SCHEMA_VERSION:
            raise ValueError("unsupported modern report schema_version: {0}".format(self.schema_version))
        for name in (
            "defect_states",
            "causal_candidates",
            "causal_relations",
            "step_judgments",
            "hypotheses",
            "introduction_candidates",
            "confirmations",
            "confirmed_roots",
            "co_roots",
            "contributing_conditions",
            "amplifying_factors",
            "downstream_materializations",
            "rejected_candidates",
            "unresolved_hypotheses",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self,
            "start_refs",
            tuple(sorted(set(_frozen_strings(self.start_refs)))),
        )
        if any(
            not isinstance(item, SeedAttributionResult)
            for item in self.seed_results
        ):
            raise TypeError("seed_results must contain SeedAttributionResult objects")
        object.__setattr__(
            self,
            "seed_results",
            tuple(
                sorted(
                    self.seed_results,
                    key=lambda item: (item.start_ref, item.defect_fingerprint),
                )
            ),
        )
        seed_keys = [
            (item.start_ref, item.defect_fingerprint) for item in self.seed_results
        ]
        if len(seed_keys) != len(set(seed_keys)):
            raise ValueError("duplicate per-seed attribution identity")
        if {item.start_ref for item in self.seed_results} != set(self.start_refs):
            raise ValueError(
                "v3 seed_results must cover exactly report start_refs"
            )
        object.__setattr__(self, "taint_paths", tuple(_frozen_strings(path) for path in self.taint_paths))
        object.__setattr__(self, "visited_order", _frozen_strings(self.visited_order))
        visited_entries = []
        seen_occurrences = set()
        for item in self.visited_entries:
            if not isinstance(item, Mapping) or set(item) != {"node_ref", "owner"}:
                raise ValueError("visited entry schema mismatch")
            owner = LocalStateOwner.from_dict(item.get("owner"))
            if owner.occurrence_identity in seen_occurrences:
                raise ValueError("duplicate visited entry occurrence identity")
            seen_occurrences.add(owner.occurrence_identity)
            visited_entries.append(
                FrozenMapping(
                    {
                        "node_ref": str(item.get("node_ref") or ""),
                        "owner": owner.to_dict(),
                    }
                )
            )
        object.__setattr__(self, "visited_entries", tuple(visited_entries))
        if visited_entries:
            derived_visited_order = _frozen_strings(
                tuple(
                    dict.fromkeys(
                        str(item["node_ref"]) for item in visited_entries
                    )
                )
            )
            if self.visited_order and self.visited_order != derived_visited_order:
                raise ValueError(
                    "visited_order does not match owned visited entries: "
                    "{0!r} != {1!r}".format(
                        self.visited_order, derived_visited_order
                    )
                )
            object.__setattr__(self, "visited_order", derived_visited_order)
        object.__setattr__(self, "unresolved_refs", _frozen_strings(self.unresolved_refs))
        object.__setattr__(
            self,
            "investigation_journal",
            tuple(FrozenMapping(_thaw(item)) for item in self.investigation_journal),
        )
        confirmation_by_identity = {}
        node_statuses = {}
        node_identities = {}
        for confirmation in self.confirmations:
            identity = confirmation.confirmation_identity
            prior = confirmation_by_identity.get(identity)
            if prior is not None:
                if prior.status != confirmation.status:
                    raise ValueError("conflicting statuses for confirmation identity {0}".format(identity))
                raise ValueError("duplicate confirmation identity {0}".format(identity))
            confirmation_by_identity[identity] = confirmation
            node_statuses.setdefault(confirmation.candidate_ref, set()).add(confirmation.status)
            node_identities.setdefault(confirmation.candidate_ref, []).append(identity)
        initial_metadata = _thaw(self.metadata)
        _validate_system_candidate_funnels(
            initial_metadata,
            self.investigation_journal,
        )
        _validate_candidate_cluster_shadow_events(
            self.investigation_journal,
            valid_seed_bindings=(
                item.seed_binding_identity
                for item in self.seed_results
            ),
        )
        raw_factor_conflicts = initial_metadata.get(
            "factor_confirmation_conflicts", []
        )
        if not isinstance(raw_factor_conflicts, list):
            raise TypeError("factor_confirmation_conflicts must be an array")
        factor_conflict_identities = {
            str(item.get("confirmation_identity") or "")
            for item in raw_factor_conflicts
            if isinstance(item, Mapping)
        }

        def embedded_confirmation(
            value: Mapping[str, Any], *, role: str
        ) -> RootConfirmation:
            if not value:
                raise ValueError(
                    "modern {0} requires a full confirmation identity".format(role)
                )
            confirmation = RootConfirmation.from_dict(_thaw(value))
            canonical = confirmation_by_identity.get(confirmation.confirmation_identity)
            if canonical is None or canonical != confirmation:
                raise ValueError(
                    "{0} confirmation is absent from top-level confirmations".format(role)
                )
            return confirmation

        canonical_candidate_nodes: Dict[str, TraceNode] = {}
        for candidate in (
            *self.causal_candidates,
            *self.introduction_candidates,
        ):
            prior_node = canonical_candidate_nodes.setdefault(
                candidate.ref,
                candidate.node,
            )
            if prior_node != candidate.node:
                raise ValueError(
                    "candidate publications disagree on canonical graph node"
                )

        def root_identity(root: ConfirmedRoot, *, role: str) -> str:
            if not root.hypothesis_id or not root.recursive_path or not root.confirmation:
                raise ValueError(
                    "modern root requires hypothesis, path, and full confirmation identity"
                )
            confirmation = embedded_confirmation(root.confirmation, role=role)
            if (
                confirmation.status != "confirmed"
                or confirmation.factor_role != "necessary_cause"
                or root.confirmation_status != "confirmed"
                or confirmation.candidate_ref != root.node_ref
                or confirmation.hypothesis_id != root.hypothesis_id
                or confirmation.defect_fingerprint != root.defect_state.fingerprint
                or confirmation.recursive_path != root.recursive_path
                or confirmation.analysis_perspective
                != self.analysis_perspective
                or root.reason != confirmation.reason
                or root.counterfactual != confirmation.counterfactual
                or root.confidence != confirmation.confidence
                or root.evidence_refs != confirmation.evidence_refs
                or root.excerpt != confirmation.excerpt
            ):
                raise ValueError(
                    "root confirmation response projection contradicts root "
                    "semantic fields"
                )
            return confirmation.confirmation_identity

        primary_identities = [
            root_identity(root, role="primary root") for root in self.confirmed_roots
        ]
        co_root_identities = [
            root_identity(root, role="co-root") for root in self.co_roots
        ]
        if len(primary_identities) != len(set(primary_identities)) or len(
            co_root_identities
        ) != len(set(co_root_identities)):
            raise ValueError("duplicate root role confirmation identity")
        if set(primary_identities).intersection(co_root_identities):
            raise ValueError("primary and co-root roles share a confirmation identity")
        primary_identity_set = set(primary_identities)
        co_root_identity_set = set(co_root_identities)
        root_identities = set(primary_identities).union(co_root_identities)
        root_by_identity = {
            root_identity(root, role=role): root
            for role, roots in (
                ("primary root", self.confirmed_roots),
                ("co-root", self.co_roots),
            )
            for root in roots
        }
        raw_confirmation_actions = initial_metadata.get(
            "confirmation_action_projection", []
        )
        unpublished_escalation_confirmation_identities = {
            confirmation.confirmation_identity
            for item in (
                raw_confirmation_actions
                if isinstance(raw_confirmation_actions, list)
                else ()
            )
            if isinstance(item, Mapping)
            and is_factor_role_escalation_origin(item.get("origin"))
            and isinstance(item.get("confirmation"), Mapping)
            for confirmation in (
                RootConfirmation.from_dict(
                    _thaw(item["confirmation"])
                ),
            )
            if confirmation.status == "confirmed"
            and confirmation.confirmation_identity not in root_identities
        }
        defect_states_by_id: Dict[str, DefectState] = {}
        defect_states_by_fingerprint: Dict[str, DefectState] = {}
        for defect_state in (
            *self.defect_states,
            *(item.defect_state for item in self.seed_results),
            *(item.defect_state for item in (*self.confirmed_roots, *self.co_roots)),
        ):
            existing = defect_states_by_id.setdefault(
                defect_state.defect_state_id, defect_state
            )
            if existing != defect_state:
                raise ValueError("conflicting report defect_state identity")
            defect_states_by_fingerprint[defect_state.fingerprint] = defect_state

        def defect_lineage_reaches_seed(
            defect_fingerprint: str, seed: SeedAttributionResult
        ) -> bool:
            current = defect_states_by_fingerprint.get(defect_fingerprint)
            visited_ids: Set[str] = set()
            while current is not None and current.defect_state_id not in visited_ids:
                if current.fingerprint == seed.defect_fingerprint:
                    return True
                visited_ids.add(current.defect_state_id)
                current = defect_states_by_id.get(
                    current.derived_from_defect_state_id
                )
            return False

        root_owner_counts = {identity: 0 for identity in root_identities}

        def has_unambiguous_composite_owner(
            root: ConfirmedRoot,
            seed: SeedAttributionResult,
            confirmation: RootConfirmation,
        ) -> bool:
            owner_candidates = [
                candidate
                for candidate in self.seed_results
                if candidate.outcome == "confirmed_root"
                and confirmation.seed_binding_identity
                == seed_binding_identity_for(
                    candidate.start_ref, candidate.defect_fingerprint
                )
                and root.recursive_path[-1] == candidate.start_ref
                and defect_lineage_reaches_seed(
                    root.defect_state.fingerprint, candidate
                )
            ]
            if owner_candidates != [seed]:
                return False
            return not any(
                candidate.start_ref == seed.start_ref
                and candidate.outcome != "confirmed_root"
                and defect_lineage_reaches_seed(
                    root.defect_state.fingerprint, candidate
                )
                for candidate in self.seed_results
            )

        for seed in self.seed_results:
            expected_seed_binding = seed_binding_identity_for(
                seed.start_ref, seed.defect_fingerprint
            )
            seed_confirmations: Dict[str, RootConfirmation] = {}
            for identity in seed.confirmation_identities:
                confirmation = confirmation_by_identity.get(identity)
                if (
                    confirmation is None
                    or confirmation.seed_binding_identity != expected_seed_binding
                    or not confirmation.recursive_path
                    or confirmation.recursive_path[-1] != seed.start_ref
                    or not defect_lineage_reaches_seed(
                        confirmation.defect_fingerprint, seed
                    )
                ):
                    raise ValueError(
                        "confirmation identities must individually bind to their seed"
                    )
                seed_confirmations[identity] = confirmation

            if seed.outcome != "confirmed_root":
                if seed.confirmed_root_refs:
                    raise ValueError(
                        "non-confirmed seed cannot retain confirmed_root_refs"
                    )
                if set(seed.confirmation_identities).intersection(root_identities):
                    raise ValueError(
                        "non-confirmed seed cannot retain an identity owning a published root"
                    )
                continue
            confirmed_seed_roots = {
                identity: root_by_identity[identity]
                for identity, confirmation in seed_confirmations.items()
                if confirmation.status == "confirmed" and identity in root_by_identity
            }
            primary_seed_identities = set(confirmed_seed_roots).intersection(
                primary_identity_set
            )
            co_root_seed_identities = set(confirmed_seed_roots).intersection(
                co_root_identity_set
            )
            if (
                not seed.confirmation_identities
                or not seed.confirmed_root_refs
                or len(primary_seed_identities) != 1
                or primary_seed_identities.union(co_root_seed_identities)
                != set(confirmed_seed_roots)
                or set(confirmed_seed_roots) != {
                    identity
                    for identity, confirmation in seed_confirmations.items()
                    if confirmation.status == "confirmed"
                    and identity not in factor_conflict_identities
                    and identity
                    not in unpublished_escalation_confirmation_identities
                }
                or {
                    root.node_ref for root in confirmed_seed_roots.values()
                }
                != set(seed.confirmed_root_refs)
                or any(
                    root.observed_defect_refs != (seed.start_ref,)
                    or not has_unambiguous_composite_owner(
                        root,
                        seed,
                        confirmation_by_identity[identity],
                    )
                    for root in confirmed_seed_roots.values()
                )
            ):
                raise ValueError(
                    "confirmed_root seed must own exactly one primary and only "
                    "same-seed co-roots with exact observed_defect_refs owning "
                    "seed projection and an unambiguous composite owner"
                )
            for identity in confirmed_seed_roots:
                root = confirmed_seed_roots[identity]
                candidate_node = canonical_candidate_nodes.get(root.node_ref)
                if candidate_node is None:
                    raise ValueError(
                        "published root has no canonical candidate node"
                    )
                expected_root = canonical_confirmed_root_publication(
                    confirmation=confirmation_by_identity[identity],
                    defect_state=root.defect_state,
                    candidate_node=candidate_node,
                    seed_start_ref=seed.start_ref,
                    active_role_binding=(
                        ActiveFailureRoleBinding.from_dict(
                            root.provenance["active_role_binding"]
                        )
                        if "active_role_binding" in root.provenance
                        else None
                    ),
                    request_projection=owning_root_request_projection(
                        confirmation=confirmation_by_identity[identity],
                        confirmation_action_projections=(
                            raw_confirmation_actions
                            if isinstance(raw_confirmation_actions, list)
                            else ()
                        ),
                    )
                    if "active_role_binding" in root.provenance
                    else None,
                )
                if root != expected_root:
                    raise ValueError(
                        "published root does not match canonical publication "
                        "projection"
                    )
                root_owner_counts[identity] += 1
        if any(count != 1 for count in root_owner_counts.values()):
            raise ValueError(
                "each published root must belong to exactly one confirmed_root seed"
            )
        ordered_root_identities = sorted(root_identities)
        for index, left_identity in enumerate(ordered_root_identities):
            left = confirmation_by_identity[left_identity]
            for right_identity in ordered_root_identities[index + 1 :]:
                right = confirmation_by_identity[right_identity]
                if left.seed_binding_identity != right.seed_binding_identity:
                    continue

                def reciprocal(
                    source: RootConfirmation, target_identity: str
                ) -> List[Mapping[str, Any]]:
                    return [
                        item
                        for item in source.competitor_comparisons
                        if str(item.get("confirmation_identity") or "")
                        == target_identity
                    ]

                left_to_right = reciprocal(left, right_identity)
                right_to_left = reciprocal(right, left_identity)
                if (
                    len(left_to_right) != 1
                    or len(right_to_left) != 1
                    or str(left_to_right[0].get("status") or "") != "co_root"
                    or str(right_to_left[0].get("status") or "") != "co_root"
                    or left_to_right[0].get("requires_independent_confirmation")
                    is not True
                    or right_to_left[0].get("requires_independent_confirmation")
                    is not True
                ):
                    raise ValueError(
                        "published root confirmation graph is not reciprocal and non-dominated"
                    )
        expected_primary_roots, expected_co_roots = (
            canonical_ranked_root_publications(
                (*self.confirmed_roots, *self.co_roots),
            )
        )
        if (
            self.confirmed_roots != expected_primary_roots
            or self.co_roots != expected_co_roots
        ):
            raise ValueError(
                "published primary and co-root roles or order contradict "
                "canonical per-seed ranking"
            )

        raw_factor_judgments = initial_metadata.get(
            "factor_role_judgments", []
        )
        raw_factor_journal = initial_metadata.get(
            "factor_role_journal", []
        )
        raw_factor_actions = initial_metadata.get(
            "factor_role_action_projections", []
        )
        raw_factor_public_gaps = initial_metadata.get(
            "factor_confirmation_gaps", []
        )
        raw_factor_gaps = initial_metadata.get(
            "factor_role_gaps", []
        )
        raw_escalation_gaps = initial_metadata.get(
            "factor_role_escalation_gaps", []
        )
        raw_confirmation_queue = initial_metadata.get(
            "confirmation_queue", []
        )
        raw_confirmation_queue_keys = initial_metadata.get(
            "confirmation_queue_keys", []
        )
        raw_confirmation_actions = initial_metadata.get(
            "confirmation_action_projection", []
        )
        if any(
            not isinstance(value, list)
            for value in (
                raw_factor_judgments,
                raw_factor_journal,
                raw_factor_actions,
                raw_factor_public_gaps,
                raw_factor_gaps,
                raw_escalation_gaps,
                raw_confirmation_queue,
                raw_confirmation_queue_keys,
                raw_confirmation_actions,
            )
        ):
            raise TypeError(
                "factor publication audit surfaces must be arrays"
            )
        candidate_lifecycles: Dict[
            Tuple[str, str], Set[str]
        ] = {}
        canonical_queue_keys: List[Tuple[str, ...]] = []
        for value in raw_confirmation_queue:
            if not isinstance(value, Mapping):
                raise ValueError(
                    "confirmation queue entries must be objects"
                )
            candidate_seed = (
                str(value.get("candidate_ref") or ""),
                str(value.get("seed_binding_identity") or ""),
            )
            origin = value.get("origin")
            if isinstance(origin, Mapping):
                canonical_factor_role_escalation_origin(origin)
            lifecycle_kind = (
                "factor_role_escalation"
                if is_factor_role_escalation_origin(origin)
                else str(value.get("review_scope") or "root")
            )
            kinds = candidate_lifecycles.setdefault(
                candidate_seed, set()
            )
            if not all(candidate_seed) or lifecycle_kind in kinds:
                raise ValueError(
                    "confirmation queue contains a missing or duplicate "
                    "candidate lifecycle for one seed"
                )
            kinds.add(lifecycle_kind)
            if len(kinds) > 1 and kinds != {
                "non_root",
                "factor_role_escalation",
            }:
                raise ValueError(
                    "confirmation queue candidate lifecycles are not "
                    "an exact factor source and escalation pair"
                )
            queue_key = canonical_confirmation_queue_key(value)
            canonical_queue_keys.append(queue_key)
        persisted_queue_keys: List[Tuple[str, ...]] = []
        for value in raw_confirmation_queue_keys:
            if (
                not isinstance(value, (list, tuple))
                or len(value) not in {4, 5}
                or any(type(item) is not str or not item for item in value)
            ):
                raise ValueError(
                    "confirmation queue persisted key is malformed"
                )
            persisted_queue_keys.append(tuple(value))
        if (
            len(canonical_queue_keys) != len(set(canonical_queue_keys))
            or len(persisted_queue_keys) != len(set(persisted_queue_keys))
            or len(persisted_queue_keys) != len(canonical_queue_keys)
            or set(persisted_queue_keys) != set(canonical_queue_keys)
        ):
            raise ValueError(
                "confirmation queue entries and persisted key set must "
                "bijectively match"
            )

        judgments_by_identity: Dict[str, FactorRoleJudgment] = {}
        for value in raw_factor_judgments:
            if not isinstance(value, Mapping):
                raise ValueError(
                    "factor role judgment audit entry must be an object"
                )
            judgment = FactorRoleJudgment.from_dict(dict(value))
            if stable_json(_thaw(value)) != stable_json(
                judgment.to_dict()
            ):
                raise ValueError(
                    "factor role judgment audit entry is non-canonical"
                )
            if judgment.judgment_identity in judgments_by_identity:
                raise ValueError("duplicate factor role judgment identity")
            judgments_by_identity[judgment.judgment_identity] = judgment

        expected_conditions: List[CausalFactor] = []
        expected_amplifiers: List[CausalFactor] = []
        expected_materializations: List[CausalMaterialization] = []
        expected_rejections: List[RejectedCandidate] = []
        expected_public_gaps: List[JsonDict] = []
        expected_factor_gaps: List[JsonDict] = []
        action_judgment_identities: Set[str] = set()
        action_request_identities: Set[str] = set()
        necessary_signals: Set[Tuple[str, str]] = set()
        factor_actions_by_identity: Dict[str, JsonDict] = {}
        for raw_action in raw_factor_actions:
            if not isinstance(raw_action, Mapping):
                raise ValueError(
                    "factor role action projection must be an object"
                )
            raw_judgment = raw_action.get("judgment")
            if not isinstance(raw_judgment, Mapping):
                raise ValueError(
                    "factor role action has no judgment projection"
                )
            judgment = FactorRoleJudgment.from_dict(
                dict(raw_judgment)
            )
            action = _canonical_factor_role_action_binding(
                judgment=judgment,
                request_projection=raw_action.get(
                    "request_projection"
                ),
                action_projection=raw_action,
                require_completed=False,
            )
            if (
                judgment.judgment_identity
                in action_judgment_identities
                or judgment.request_identity
                in action_request_identities
            ):
                raise ValueError(
                    "factor role action identities must be one-to-one"
                )
            action_judgment_identities.add(
                judgment.judgment_identity
            )
            action_request_identities.add(judgment.request_identity)
            factor_action_identity = str(action["semantic_key"])
            if factor_action_identity in factor_actions_by_identity:
                raise ValueError(
                    "factor role action identity is duplicated"
                )
            factor_actions_by_identity[factor_action_identity] = action
            if (
                judgments_by_identity.get(
                    judgment.judgment_identity
                )
                != judgment
                or judgment.analysis_perspective
                != self.analysis_perspective
            ):
                raise ValueError(
                    "factor role action has no exact judgment audit entry or report perspective"
                )
            expected_status = (
                "completed"
                if action["operation"] == "factor_role_completed"
                else "failed"
            )
            matching_journal = [
                value
                for value in raw_factor_journal
                if isinstance(value, Mapping)
                and value.get("judgment_identity")
                == judgment.judgment_identity
            ]
            if (
                len(matching_journal) != 1
                or stable_json(_thaw(matching_journal[0]))
                != stable_json({**action, "status": expected_status})
            ):
                raise ValueError(
                    "factor role request, response, action, and journal identities diverge"
                )
            matching_queue = [
                value
                for value in raw_confirmation_queue
                if isinstance(value, Mapping)
                and value.get("semantic_identity")
                == judgment.request_identity
                and value.get("review_scope") == "non_root"
            ]
            if len(matching_queue) != 1:
                raise ValueError(
                    "factor role action has no exact completed non-root queue owner"
                )
            canonical_terminal_factor_role_queue_binding(
                queue_entry=matching_queue[0],
                action_projection=action,
                judgment=judgment,
            )
            if judgment.necessity_status == "unknown":
                expected_factor_gaps.append(
                    canonical_factor_role_gap(
                        judgment=judgment,
                        action_projection=action,
                    )
                )
            if action["operation"] != "factor_role_completed":
                continue
            publication = canonical_factor_role_publication(
                judgment=judgment,
                request_projection=action["request_projection"],
                action_projection=action,
            )
            if (
                judgment.necessity_status == "necessary"
                and judgment.factor_role == "unknown"
            ):
                necessary_signals.add(
                    (
                        judgment.candidate_ref,
                        judgment.seed_binding_identity,
                    )
                )
            elif judgment.factor_role == "contributing_condition":
                expected_conditions.append(publication)
            elif judgment.factor_role == "amplifying_factor":
                expected_amplifiers.append(publication)
            elif judgment.factor_role == "downstream_materialization":
                expected_materializations.append(publication)
            elif judgment.factor_role == "unrelated":
                expected_rejections.append(publication)
            elif judgment.factor_role == "unknown":
                expected_public_gaps.append(publication)
        if (
            set(judgments_by_identity) != action_judgment_identities
            or len(raw_factor_journal) != len(raw_factor_actions)
        ):
            raise ValueError(
                "factor role judgment, journal, and action surfaces are not bijective"
            )
        if stable_json(raw_factor_gaps) != stable_json(
            expected_factor_gaps
        ):
            raise ValueError(
                "factor role gaps do not bijectively match unknown terminal "
                "FactorRole lifecycles"
            )
        terminal_non_root_request_identities = [
            str(value.get("semantic_identity") or "")
            for value in raw_confirmation_queue
            if isinstance(value, Mapping)
            and value.get("review_scope") == "non_root"
            and value.get("status") in {"completed", "failed"}
        ]
        if (
            len(terminal_non_root_request_identities)
            != len(raw_factor_actions)
            or set(terminal_non_root_request_identities)
            != action_request_identities
        ):
            raise ValueError(
                "terminal non-root queue and FactorRole actions are not bijective"
            )
        from .recursive_analyzer import (
            _validated_confirmation_action_projection,
        )

        escalation_root_actions: List[JsonDict] = []
        for raw_action in raw_confirmation_actions:
            if (
                not isinstance(raw_action, Mapping)
                or not is_factor_role_escalation_origin(
                    raw_action.get("origin")
                )
            ):
                continue
            action = _validated_confirmation_action_projection(
                raw_action
            )
            canonical_factor_role_escalation_origin(
                action["origin"]
            )
            escalation_root_actions.append(action)
        escalation_queues = [
            value
            for value in raw_confirmation_queue
            if isinstance(value, Mapping)
            and is_factor_role_escalation_origin(value.get("origin"))
        ]
        consumed_factor_action_identities: Set[str] = set()
        consumed_root_action_identities: Set[str] = set()
        escalation_confirmation_identities: Set[str] = set()
        escalation_gap_confirmation_identities: Set[str] = set()
        expected_escalation_gaps: List[JsonDict] = []
        for queue in escalation_queues:
            origin = canonical_factor_role_escalation_origin(
                queue.get("origin")
            )
            factor_action_identity = origin[
                "factor_action_identity"
            ]
            if (
                factor_action_identity
                in consumed_factor_action_identities
            ):
                raise ValueError(
                    "factor role action was consumed by more than one "
                    "root escalation"
                )
            factor_action = factor_actions_by_identity.get(
                factor_action_identity
            )
            if factor_action is None:
                raise ValueError(
                    "factor role escalation has no exact source action"
                )
            matching_root_actions = [
                action
                for action in escalation_root_actions
                if canonical_factor_role_escalation_origin(
                    action["origin"]
                )
                == origin
            ]
            if queue.get("status") == "queued":
                if matching_root_actions:
                    raise ValueError(
                        "pending factor role escalation already has a "
                        "terminal root action"
                    )
                canonical_factor_role_escalation_binding(
                    factor_action_projection=factor_action,
                    root_queue_entry=queue,
                )
            else:
                if len(matching_root_actions) != 1:
                    raise ValueError(
                        "terminal factor role escalation must consume "
                        "exactly one root action"
                    )
                root_action = matching_root_actions[0]
                root_action_identity = str(root_action["semantic_key"])
                if (
                    root_action_identity
                    in consumed_root_action_identities
                ):
                    raise ValueError(
                        "factor role escalation root action is consumed "
                        "more than once"
                    )
                binding = canonical_factor_role_escalation_binding(
                    factor_action_projection=factor_action,
                    root_queue_entry=queue,
                    root_action_projection=root_action,
                )
                confirmation = RootConfirmation.from_dict(
                    dict(root_action["confirmation"])
                )
                if confirmation.status == "confirmed":
                    escalation_confirmation_identities.add(
                        confirmation.confirmation_identity
                    )
                if confirmation.status in {"rejected", "unknown"}:
                    if confirmation.status == "unknown":
                        escalation_gap_confirmation_identities.add(
                            confirmation.confirmation_identity
                        )
                    expected_escalation_gaps.append(
                        canonical_factor_role_escalation_gap(
                            factor_action_projection=factor_action,
                            root_queue_entry=queue,
                            root_action_projection=root_action,
                        )
                    )
                consumed_root_action_identities.add(
                    root_action_identity
                )
                if binding["origin"] != origin:
                    raise ValueError(
                        "factor role escalation binding is non-canonical"
                    )
            consumed_factor_action_identities.add(
                factor_action_identity
            )
        necessary_factor_action_identities = {
            action["semantic_key"]
            for action in factor_actions_by_identity.values()
            if action["operation"] == "factor_role_completed"
            and (
                FactorRoleJudgment.from_dict(
                    dict(action["judgment"])
                ).necessity_status
                == "necessary"
            )
            and (
                FactorRoleJudgment.from_dict(
                    dict(action["judgment"])
                ).factor_role
                == "unknown"
            )
        }
        if (
            consumed_factor_action_identities
            != necessary_factor_action_identities
            or len(escalation_root_actions)
            != len(consumed_root_action_identities)
        ):
            raise ValueError(
                "completed necessary FactorRole actions and root "
                "escalations are not bijective"
            )
        if stable_json(raw_escalation_gaps) != stable_json(
            expected_escalation_gaps
        ):
            raise ValueError(
                "factor role escalation gaps do not bijectively match "
                "rejected or unknown RootConfirmation actions"
            )
        if self.contributing_conditions != tuple(expected_conditions):
            raise ValueError(
                "contributing conditions do not match completed FactorRole actions"
            )
        if self.amplifying_factors != tuple(expected_amplifiers):
            raise ValueError(
                "amplifying factors do not match completed FactorRole actions"
            )
        if self.downstream_materializations != tuple(
            expected_materializations
        ):
            raise ValueError(
                "downstream materializations do not match completed FactorRole actions"
            )
        if self.rejected_candidates != tuple(expected_rejections):
            raise ValueError(
                "rejected candidates do not match completed FactorRole actions"
            )
        if stable_json(raw_factor_public_gaps) != stable_json(
            expected_public_gaps
        ):
            raise ValueError(
                "factor confirmation gaps do not match completed unknown "
                "FactorRole actions with a canonical Global non-root assessment"
            )
        for root in (*self.confirmed_roots, *self.co_roots):
            confirmation = RootConfirmation.from_dict(
                dict(root.confirmation)
            )
            if (
                confirmation.candidate_ref,
                confirmation.seed_binding_identity,
            ) in necessary_signals and (
                confirmation.confirmation_identity
                not in escalation_confirmation_identities
            ):
                raise ValueError(
                    "necessary FactorRole signal cannot publish a direct root"
                )

        unresolved_hypothesis_ids = {
            item.hypothesis_id for item in self.unresolved_hypotheses
        }
        root_hypothesis_ids = {
            confirmation_by_identity[identity].hypothesis_id for identity in root_identities
        }
        if root_hypothesis_ids.intersection(unresolved_hypothesis_ids):
            raise ValueError("root and unresolved roles share a hypothesis identity")

        orphan_confirmed = {
            identity: confirmation
            for identity, confirmation in confirmation_by_identity.items()
            if confirmation.status == "confirmed" and identity not in root_identities
        }
        explicit_unresolved_identities = {
            str(item.get("confirmation_identity") or "")
            for item in _thaw(self.metadata).get("unresolved_branches", [])
            if isinstance(item, Mapping)
        }
        for identity, confirmation in orphan_confirmed.items():
            explicitly_unresolved = bool(
                identity in explicit_unresolved_identities
                or identity in factor_conflict_identities
                or identity
                in unpublished_escalation_confirmation_identities
                or confirmation.hypothesis_id in unresolved_hypothesis_ids
                or confirmation.candidate_ref in self.unresolved_refs
            )
            if not explicitly_unresolved:
                raise ValueError(
                    "orphan confirmed confirmation requires explicit unresolved state"
                )

        metadata = _thaw(self.metadata)
        raw_confirmation_queue = metadata.get("confirmation_queue", [])
        if not isinstance(raw_confirmation_queue, list):
            raise TypeError("confirmation_queue must be an array")
        seed_results_by_binding = {
            seed.seed_binding_identity: seed for seed in self.seed_results
        }
        factor_gap_identities: Set[str] = set(
            escalation_gap_confirmation_identities
        )

        raw_factor_enqueue_gaps = metadata.get(
            "factor_confirmation_enqueue_gaps",
            [],
        )
        if not isinstance(raw_factor_enqueue_gaps, list):
            raise TypeError(
                "factor_confirmation_enqueue_gaps must be an array"
            )
        factor_enqueue_gap_keys: Set[Tuple[str, str, str]] = set()
        for gap in raw_factor_enqueue_gaps:
            if (
                not isinstance(gap, Mapping)
                or {str(key) for key in gap}
                != {
                    "candidate_ref",
                    "defect_fingerprint",
                    "seed_binding_identity",
                    "causal_role",
                    "reason",
                    "origin",
                }
            ):
                raise ValueError(
                    "factor confirmation enqueue gap has an inexact schema"
                )
            candidate_ref = str(gap.get("candidate_ref") or "")
            defect_fingerprint = str(
                gap.get("defect_fingerprint") or ""
            )
            seed_binding_identity = str(
                gap.get("seed_binding_identity") or ""
            )
            causal_role = str(gap.get("causal_role") or "")
            key = (
                candidate_ref,
                defect_fingerprint,
                seed_binding_identity,
            )
            seed = seed_results_by_binding.get(seed_binding_identity)
            matching_assessments = [
                assessment
                for assessment in (
                    seed.global_judgment.get("assessments", ())
                    if seed is not None
                    else ()
                )
                if isinstance(assessment, Mapping)
                and str(assessment.get("candidate_ref") or "")
                == candidate_ref
                and str(assessment.get("causal_role") or "")
                == causal_role
            ]
            matching_queue_items = [
                queued
                for queued in raw_confirmation_queue
                if isinstance(queued, Mapping)
                and str(queued.get("candidate_ref") or "")
                == candidate_ref
                and str(queued.get("seed_binding_identity") or "")
                == seed_binding_identity
                and str(queued.get("review_scope") or "root")
                == "non_root"
            ]
            if (
                not all(key)
                or key in factor_enqueue_gap_keys
                or seed is None
                or seed.defect_fingerprint != defect_fingerprint
                or candidate_ref in seed.selected_candidate_refs
                or causal_role
                not in {
                    "contributing_condition",
                    "amplifying_factor",
                    "unrelated",
                }
                or len(matching_assessments) != 1
                or matching_queue_items
                or str(gap.get("reason") or "")
                != (
                    "The bounded non-root confirmation queue rejected "
                    "the candidate before independent review."
                )
                or str(gap.get("origin") or "")
                != "global_candidate_factor_assessment"
            ):
                raise ValueError(
                    "factor confirmation enqueue gap is not grounded in an "
                    "unconfirmed Global non-root assessment"
                )
            factor_enqueue_gap_keys.add(key)

        if raw_factor_conflicts:
            raise ValueError(
                "v22 factor necessity signals cannot use RootConfirmation conflict publications"
            )

        factor_gap_identities.update(
            factor_escalation_outperformed_confirmation_identities(
                confirmations=self.confirmations,
                published_roots=(
                    *self.confirmed_roots,
                    *self.co_roots,
                ),
                confirmation_queue=raw_confirmation_queue,
            )
        )
        validate_confirmation_ownership(
            self.confirmations,
            self.seed_results,
            label="report",
            non_blocking_unresolved_confirmation_identities=(
                factor_gap_identities
            ),
        )

        for key in (
            "confirmation_queue",
            "confirmation_queue_keys",
            "confirmation_journal",
            "confirmation_action_projection",
            "step_action_projection",
            "global_candidate_judgments",
            "global_candidate_failures",
            "candidate_compression",
            "recursive_expansion_reasons",
            "factor_confirmation_gaps",
            "factor_confirmation_conflicts",
            "factor_confirmation_enqueue_gaps",
            *MODERN_FACTOR_AUDIT_METADATA_KEYS,
        ):
            metadata.setdefault(key, [])
        summary = {}
        for node_ref in sorted(node_statuses):
            statuses = node_statuses[node_ref]
            status = next(iter(statuses)) if len(statuses) == 1 else "mixed"
            summary[node_ref] = {
                "status": status,
                "counts": {
                    name: sum(
                        1
                        for item in self.confirmations
                        if item.candidate_ref == node_ref and item.status == name
                    )
                    for name in ("confirmed", "rejected", "unknown")
                },
                "confirmation_identities": sorted(node_identities[node_ref]),
            }
        metadata["confirmation_node_summary"] = summary
        object.__setattr__(self, "metadata", FrozenMapping(metadata))
        outcome = _aggregate_seed_outcomes(self.seed_results)
        if metadata.get("global_candidate_judgments") and metadata.get("unresolved_page_refs"):
            outcome = "partial"
        object.__setattr__(self, "analysis_outcome", outcome)

    def to_dict(self) -> JsonDict:
        legacy_root_causes: List[JsonDict] = []
        seen_legacy_roots: Set[str] = set()
        for root in (*self.confirmed_roots, *self.co_roots):
            projection = root.to_legacy_root_cause()
            projection_key = stable_json(projection)
            if projection_key in seen_legacy_roots:
                continue
            seen_legacy_roots.add(projection_key)
            legacy_root_causes.append(projection)
        payload = {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "objective": self.objective,
            "start_refs": list(self.start_refs),
            "seed_results": [item.to_dict() for item in self.seed_results],
            "analysis_outcome": self.analysis_outcome,
            "analysis_perspective": self.analysis_perspective,
            "defect_states": [item.to_dict() for item in self.defect_states],
            "causal_candidates": [item.to_dict() for item in self.causal_candidates],
            "causal_relations": [item.to_dict() for item in self.causal_relations],
            "step_judgments": [item.to_dict() for item in self.step_judgments],
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "introduction_candidates": [item.to_dict() for item in self.introduction_candidates],
            "confirmations": [item.to_dict() for item in self.confirmations],
            "confirmed_roots": [item.to_dict() for item in self.confirmed_roots],
            "co_roots": [item.to_dict() for item in self.co_roots],
            "contributing_conditions": [item.to_dict() for item in self.contributing_conditions],
            "amplifying_factors": [item.to_dict() for item in self.amplifying_factors],
            "downstream_materializations": [
                item.to_dict() for item in self.downstream_materializations
            ],
            "rejected_candidates": [item.to_dict() for item in self.rejected_candidates],
            "unresolved_hypotheses": [item.to_dict() for item in self.unresolved_hypotheses],
            "root_causes": legacy_root_causes,
            "taint_paths": [list(path) for path in self.taint_paths],
            "visited_order": list(self.visited_order),
            "visited_entries": [_thaw(item) for item in self.visited_entries],
            "unresolved_refs": list(self.unresolved_refs),
            "investigation_journal": [_thaw(item) for item in self.investigation_journal],
            "metadata": _thaw(self.metadata),
        }
        validate_modern_report_shape(payload)
        return payload

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RecursiveAttributionReport":
        if not isinstance(value, Mapping):
            raise TypeError("report must be an object")
        schema_version = str(value.get("schema_version") or "")
        if not schema_version:
            raise ValueError("report schema_version is required")

        def items(key: str, factory: Any, *, strict_objects: bool = False) -> List[Any]:
            raw = value.get(key)
            if not isinstance(raw, list):
                if schema_version == MODERN_REPORT_SCHEMA_VERSION:
                    raise TypeError("{0} must be an array".format(key))
                return []
            parsed = []
            for index, item in enumerate(raw):
                if not isinstance(item, Mapping):
                    if (
                        schema_version == MODERN_REPORT_SCHEMA_VERSION
                        or strict_objects
                    ):
                        raise TypeError("{0}[{1}] must be an object".format(key, index))
                    continue
                parsed.append(factory(dict(item)))
            return parsed

        if schema_version == LEGACY_REPORT_SCHEMA_VERSION:
            legacy_roots = value.get("root_causes")
            if not isinstance(legacy_roots, list):
                legacy_roots = []
            unresolved_refs = tuple(
                dict.fromkeys(
                    str(item.get("node_ref") or "")
                    for item in legacy_roots
                    if isinstance(item, dict) and str(item.get("node_ref") or "")
                )
            )
            metadata = _json_dict(value.get("metadata"))
            metadata.update({
                "legacy_migration_status": "independent_confirmation_required",
                "legacy_unconfirmed_root_causes": [
                    dict(item) for item in legacy_roots if isinstance(item, dict)
                ],
            })
            return cls(
                case_id=str(value.get("case_id") or ""),
                objective=str(value.get("objective") or ""),
                unresolved_refs=unresolved_refs,
                metadata=metadata,
            )
        if schema_version not in {
            MODERN_REPORT_SCHEMA_VERSION,
            PREVIOUS_REPORT_SCHEMA_VERSION,
        }:
            raise ValueError("unsupported report schema_version: {0}".format(schema_version))
        if schema_version == MODERN_REPORT_SCHEMA_VERSION:
            validate_modern_report_shape(value)
            _validate_modern_factor_role_raw_surfaces(value)
        confirmed_roots = items("confirmed_roots", ConfirmedRoot.from_dict)
        co_roots = items("co_roots", ConfirmedRoot.from_dict)
        if not confirmed_roots and value.get("root_causes"):
            raise ValueError(
                "legacy root_causes require schema migration with independent confirmation"
            )
        if schema_version == MODERN_REPORT_SCHEMA_VERSION:
            legacy_roots = value.get("root_causes")
            if not isinstance(legacy_roots, list):
                raise ValueError(
                    "modern report root_causes projection must be an array"
                )
            expected_legacy_roots = []
            seen_legacy_roots = set()
            for root in (*confirmed_roots, *co_roots):
                projection = root.to_legacy_root_cause()
                projection_key = stable_json(projection)
                if projection_key in seen_legacy_roots:
                    continue
                seen_legacy_roots.add(projection_key)
                expected_legacy_roots.append(projection)
            canonical_legacy_roots = [
                {
                    str(key): item
                    for key, item in root.items()
                    if str(key)
                    not in {
                        "semantic_anchor_id",
                        "semantic_occurrence_id",
                    }
                }
                if isinstance(root, Mapping)
                else root
                for root in legacy_roots
            ]
            if stable_json(canonical_legacy_roots) != stable_json(
                expected_legacy_roots
            ):
                raise ValueError(
                    "modern root_causes must exactly project published roots"
                )
        start_refs = _string_list(value.get("start_refs"))
        defect_states = items("defect_states", DefectState.from_dict)
        seed_results = (
            items(
                "seed_results",
                SeedAttributionResult.from_dict,
                strict_objects=True,
            )
            if schema_version == MODERN_REPORT_SCHEMA_VERSION
            else []
        )
        metadata = _json_dict(value.get("metadata"))
        unresolved_refs = _string_list(value.get("unresolved_refs"))
        if schema_version == MODERN_REPORT_SCHEMA_VERSION:
            for key in (
                "factor_confirmation_gaps",
                "factor_confirmation_conflicts",
                "factor_confirmation_enqueue_gaps",
                *MODERN_FACTOR_AUDIT_METADATA_KEYS,
            ):
                if not isinstance(metadata.get(key), list):
                    raise ValueError(
                        "modern report metadata {0} is required".format(key)
                    )
            from .recursive_analyzer import (
                _classify_global_failure_episodes,
                _classify_global_pass_records,
                _seed_authority_from_records,
            )

            report_seed_authority = _seed_authority_from_records(
                value.get("seed_results") or ()
            )
            _classify_global_pass_records(
                value.get("investigation_journal") or (),
                seed_authority=report_seed_authority,
            )
            _classify_global_failure_episodes(
                metadata.get("unresolved_branches") or (),
                seed_authority=report_seed_authority,
            )
        if schema_version == PREVIOUS_REPORT_SCHEMA_VERSION:
            from .graph import EVIDENCE_ELIGIBILITY_POLICY_IDENTITY

            source_evidence_policy_identity = (
                "legacy-report-evidence-policy/unversioned"
            )
            target_evidence_policy_identity = (
                EVIDENCE_ELIGIBILITY_POLICY_IDENTITY
            )
            migrated_confirmations = items(
                "confirmations", RootConfirmation.from_dict
            )
            migrated_states = [
                DefectState.create(
                    label="legacy_seed_attribution_unresolved",
                    expected=str(value.get("objective") or ""),
                    actual="The v2 report did not retain a per-seed defect binding.",
                    mechanism="Read-only v2 migration preserves the evidence gap without inferring a local root.",
                    scope="report_migration:{0}".format(start_ref),
                )
                for start_ref in start_refs
            ]
            seed_results = [
                SeedAttributionResult(
                    start_ref=start_ref,
                    defect_fingerprint=state.fingerprint,
                    defect_state=state,
                    outcome="inconclusive",
                    blocking_reasons=(
                        "evidence_policy_migration_required",
                    ),
                )
                for start_ref, state in zip(start_refs, migrated_states)
            ]
            known_fingerprints = {item.fingerprint for item in defect_states}
            defect_states.extend(
                item for item in migrated_states if item.fingerprint not in known_fingerprints
            )
            metadata.update(
                {
                    "report_migration": {
                        "source_schema": PREVIOUS_REPORT_SCHEMA_VERSION,
                        "status": "per_seed_attribution_inconclusive",
                        "source_evidence_policy_identity": (
                            source_evidence_policy_identity
                        ),
                        "target_evidence_policy_identity": (
                            target_evidence_policy_identity
                        ),
                        "blocking_reason": "evidence_policy_migration_required",
                        "unpublished_confirmed_roots": [
                            root.to_dict() for root in (*confirmed_roots, *co_roots)
                        ],
                        "unpublished_confirmations": [
                            confirmation.to_dict()
                            for confirmation in migrated_confirmations
                        ],
                    }
                }
            )
            unresolved_branches = list(metadata.get("unresolved_branches") or [])
            unresolved_branches.extend(
                {
                    "node_ref": root.node_ref,
                    "confirmation_identity": RootConfirmation.from_dict(
                        dict(root.confirmation)
                    ).confirmation_identity,
                    "reason": "evidence_policy_migration_required",
                    "source_evidence_policy_identity": (
                        source_evidence_policy_identity
                    ),
                    "target_evidence_policy_identity": (
                        target_evidence_policy_identity
                    ),
                }
                for root in (*confirmed_roots, *co_roots)
            )
            metadata["unresolved_branches"] = unresolved_branches
            unresolved_refs = list(
                dict.fromkeys(
                    [
                        *unresolved_refs,
                        *(root.node_ref for root in (*confirmed_roots, *co_roots)),
                    ]
                )
            )
            confirmed_roots = []
            co_roots = []
            for key in (
                "causal_candidates",
                "causal_relations",
                "step_judgments",
                "hypotheses",
                "introduction_candidates",
                "confirmations",
                "contributing_conditions",
                "amplifying_factors",
                "downstream_materializations",
                "rejected_candidates",
                "unresolved_hypotheses",
                "taint_paths",
                "visited_order",
                "visited_entries",
                "investigation_journal",
            ):
                value = {**value, key: []}
            for key in (
                "confirmation_queue",
                "confirmation_queue_keys",
                "confirmation_journal",
                "confirmation_action_projection",
                "global_candidate_judgments",
                "global_candidate_failures",
                "candidate_compression",
                "recursive_expansion_reasons",
            ):
                metadata[key] = []
            metadata["global_candidate_pass_count"] = 0
            metadata["global_judge_physical_request_count"] = 0
        causal_relations = (
            items("causal_relations", PredecessorAssessment.from_dict)
            if schema_version == MODERN_REPORT_SCHEMA_VERSION
            else []
        )
        step_judgments = (
            items("step_judgments", CausalStepJudgment.from_dict)
            if schema_version == MODERN_REPORT_SCHEMA_VERSION
            else []
        )
        visited_entries = (
            [
                dict(item)
                for item in value.get("visited_entries") or ()
                if isinstance(item, Mapping)
            ]
            if schema_version == MODERN_REPORT_SCHEMA_VERSION
            else []
        )
        if schema_version == MODERN_REPORT_SCHEMA_VERSION:
            if not isinstance(
                metadata.get("confirmation_action_projection"), list
            ):
                raise ValueError(
                    "modern report confirmation action projection is required"
                )
            if not isinstance(
                metadata.get("confirmation_queue_keys"), list
            ):
                raise ValueError(
                    "modern report confirmation queue keys are required"
                )
            if not isinstance(
                metadata.get("step_action_projection"), list
            ):
                raise ValueError(
                    "modern report step action projection is required"
                )
            if any(item.owner is None for item in causal_relations):
                raise ValueError("modern report causal relation is ownerless")
            if any(
                item.owner is None
                or any(predecessor.owner is None for predecessor in item.predecessors)
                for item in step_judgments
            ):
                raise ValueError("modern report causal step judgment is ownerless")
            if len(visited_entries) != len(value.get("visited_entries") or ()):
                raise ValueError("modern report visited entries are malformed")
            if value.get("visited_order") and not visited_entries:
                raise ValueError("modern report visited state is ownerless")
            valid_seed_bindings = {
                seed_binding_identity_for(
                    str(item.get("start_ref") or ""),
                    str(item.get("defect_fingerprint") or ""),
                )
                for item in value.get("seed_results") or ()
                if isinstance(item, Mapping)
            }

            def require_owned_items(items_value: Any, label: str) -> None:
                for item in items_value or ():
                    if not isinstance(item, Mapping):
                        raise ValueError("{0} must contain objects".format(label))
                    owner = LocalStateOwner.from_dict(item.get("owner"))
                    if owner.seed_binding_identity not in valid_seed_bindings:
                        raise ValueError("{0} has no active seed owner".format(label))

            require_owned_items(
                [
                    item
                    for item in value.get("investigation_journal") or ()
                    if isinstance(item, Mapping)
                    and item.get("kind") == "global_candidate_pass"
                    and item.get("seed_ref")
                ],
                "global pass journal",
            )
            for key in (
                "confirmation_queue",
                "confirmation_journal",
                "confirmation_action_projection",
                "factor_role_journal",
                "factor_role_action_projections",
                "factor_role_gaps",
                "step_action_projection",
                "global_candidate_judgments",
                "candidate_compression",
                "recursive_expansion_reasons",
            ):
                require_owned_items(metadata.get(key), "report metadata {0}".format(key))
        return cls(
            case_id=str(value.get("case_id") or ""),
            objective=str(value.get("objective") or ""),
            schema_version=MODERN_REPORT_SCHEMA_VERSION,
            start_refs=start_refs,
            seed_results=seed_results,
            analysis_outcome=str(value.get("analysis_outcome") or "inconclusive"),
            analysis_perspective=str(value.get("analysis_perspective") or ""),
            defect_states=defect_states,
            causal_candidates=items("causal_candidates", CausalCandidate.from_dict),
            causal_relations=causal_relations,
            step_judgments=step_judgments,
            hypotheses=items("hypotheses", AttributionHypothesis.from_dict),
            introduction_candidates=items("introduction_candidates", CausalCandidate.from_dict),
            confirmations=items("confirmations", RootConfirmation.from_dict),
            confirmed_roots=confirmed_roots,
            co_roots=co_roots,
            contributing_conditions=items("contributing_conditions", CausalFactor.from_dict),
            amplifying_factors=items("amplifying_factors", CausalFactor.from_dict),
            downstream_materializations=items(
                "downstream_materializations",
                CausalMaterialization.from_dict,
            ),
            rejected_candidates=items("rejected_candidates", RejectedCandidate.from_dict),
            unresolved_hypotheses=items("unresolved_hypotheses", AttributionHypothesis.from_dict),
            taint_paths=[_string_list(path) for path in value.get("taint_paths", []) if isinstance(path, list)],
            visited_order=_string_list(value.get("visited_order")),
            visited_entries=visited_entries,
            unresolved_refs=unresolved_refs,
            investigation_journal=(
                tuple(
                    dict(item)
                    for item in value["investigation_journal"]
                )
                if schema_version == MODERN_REPORT_SCHEMA_VERSION
                else tuple(
                    dict(item)
                    for item in value.get("investigation_journal", [])
                    if isinstance(item, Mapping)
                )
            ),
            metadata=metadata,
        )


__all__ = [
    "ACTIVE_FAILURE_CAUSAL_ROLES",
    "ActiveFailureRoleBinding",
    "CAUSAL_PUBLICATION_CONTRACT_VERSION",
    "CAUSAL_RELATIONS",
    "FACTOR_NECESSITY_STATUSES",
    "FACTOR_ROLE_PUBLICATION_CONTRACT_VERSION",
    "FACTOR_ROLE_FAILURE_CLASSIFICATIONS",
    "FACTOR_ROLE_ESCALATION_GAP_KEYS",
    "FACTOR_ROLE_GAP_KEYS",
    "FACTOR_ROLE_CONTRACT",
    "FACTOR_ROLES",
    "AttributionHypothesis",
    "CausalCandidate",
    "CausalFactor",
    "CausalMaterialization",
    "CausalStepJudgment",
    "ConfirmedRoot",
    "DefectState",
    "FactorRoleJudgment",
    "FrontierItem",
    "HypothesisEvidence",
    "LocalStateOwner",
    "MODERN_REPORT_KEYS",
    "MODERN_REPORT_SCHEMA_VERSION",
    "TERMINAL_FACTOR_ROLE_QUEUE_ALLOWED_KEYS",
    "TERMINAL_FACTOR_ROLE_QUEUE_REQUIRED_KEYS",
    "PredecessorAssessment",
    "RecursiveAttributionReport",
    "RejectedCandidate",
    "RootConfirmation",
    "SeedAttributionResult",
    "canonical_causal_factor_publication",
    "canonical_factor_role_publication",
    "canonical_factor_role_gap",
    "canonical_factor_role_escalation_binding",
    "canonical_factor_role_escalation_gap",
    "canonical_factor_role_escalation_origin",
    "canonical_factor_role_publication_provenance",
    "canonical_factor_role_queue_binding_snapshot",
    "canonical_confirmation_origin",
    "canonical_confirmation_queue_key",
    "canonical_terminal_factor_role_queue_binding",
    "canonical_confirmation_publication_provenance",
    "canonical_confirmed_root_publication",
    "canonical_confirmed_root_rank",
    "canonical_factor_label",
    "canonical_ranked_root_publications",
    "canonical_rejected_candidate_publication",
    "factor_role_contract_entry",
    "is_definitive_confirmation",
    "is_factor_role_escalation_origin",
    "validate_confirmation_ownership",
    "validate_modern_report_shape",
    "validate_root_confirmation_substantive_invariants",
    "SEMANTIC_ANCHOR_SCHEMA_VERSION",
    "SEMANTIC_OCCURRENCE_SCHEMA_VERSION",
    "annotate_report_semantic_anchors",
    "active_failure_causal_role_for",
    "active_failure_factor_role_for",
    "active_failure_signature_for",
    "normalized_anchor_semantics",
    "semantic_anchor_id",
    "semantic_anchor_index",
    "semantic_occurrence_id",
    "semantic_occurrence_index",
    "semantic_visit_key",
    "confirmation_counterfactual_for",
    "confirmation_identity_for",
    "confirmation_response_identity_for",
    "seed_binding_identity_for",
    "seed_defect_state",
    "validate_seed_outcome_payload",
]
