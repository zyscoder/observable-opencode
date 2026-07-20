"""Bounded, read-only investigation tools for offline causal attribution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import TraceGraph, is_temporal_only_edge
from .models import JsonDict, stable_json


EVIDENCE_TOOLS = frozenset(
    {
        "inspect_node",
        "expand_upstream",
        "expand_downstream",
        "inspect_artifact",
        "inspect_episode",
        "inspect_context_lineage",
        "inspect_task_obligations",
        "compare_causal_paths",
        "search_semantic_nodes",
    }
)
CONTROL_ACTIONS = frozenset(
    {"record_hypothesis", "reject_hypothesis", "request_root_confirmation"}
)
RESULT_STATUSES = frozenset({"success", "unchanged", "rejected", "error", "deferred"})
EVIDENCE_ARGUMENT_KEYS = {
    "inspect_node": (frozenset({"ref"}), frozenset()),
    "expand_upstream": (frozenset({"ref"}), frozenset({"limit", "relation_filter"})),
    "expand_downstream": (frozenset({"ref"}), frozenset({"limit", "relation_filter"})),
    "inspect_artifact": (
        frozenset({"artifact_id"}),
        frozenset({"offset", "length", "expected_hash"}),
    ),
    "inspect_episode": (frozenset({"ref"}), frozenset()),
    "inspect_context_lineage": (frozenset({"message_id"}), frozenset()),
    "inspect_task_obligations": (frozenset({"scope"}), frozenset()),
    "compare_causal_paths": (frozenset({"paths"}), frozenset()),
    "search_semantic_nodes": (
        frozenset({"query", "before_ref"}),
        frozenset({"limit", "scan_limit"}),
    ),
}
CONTROL_ARGUMENT_KEYS = {
    "record_hypothesis": frozenset({"claim", "candidate_ref"}),
    "reject_hypothesis": frozenset({"hypothesis_id", "opposing_evidence_refs"}),
    "request_root_confirmation": frozenset(
        {"hypothesis_id", "candidate_ref", "defect_fingerprint"}
    ),
}
DIRECTIVE_KEYS = frozenset(
    {
        "directive_id",
        "directive_kind",
        "tool_name",
        "arguments",
        "requested_by_ref",
        "hypothesis_id",
        "reason",
    }
)
CONTROL_KEYS = frozenset(
    {
        "directive_id",
        "directive_kind",
        "action",
        "arguments",
        "requested_by_ref",
        "reason",
    }
)
RESULT_KEYS = frozenset(
    {
        "directive_id",
        "directive_kind",
        "tool_name",
        "status",
        "requested_refs",
        "resolved_refs",
        "provenance",
        "payload",
        "truncated",
        "byte_count",
        "artifact_byte_count",
        "evidence_hash",
        "rejection_reason",
        "error",
    }
)
MAX_CAUSAL_PATHS = 8
MAX_CAUSAL_PATH_LENGTH = 32
MAX_CONTEXT_LINEAGE_MATCHES = 64
MAX_CONTEXT_LINEAGE_SCANS = 4096
MAX_EPISODE_REFS = 64


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _strings(values: Iterable[Any]) -> Tuple[str, ...]:
    output: List[str] = []
    seen: Set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            output.append(item)
    return tuple(output)


def _identity(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(stable_json(_thaw(value)).encode("utf-8")).hexdigest()
    return "{0}:{1}".format(prefix, digest[:24])


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    label: str,
) -> None:
    required_keys = set(required)
    allowed = required_keys | set(optional)
    actual = set(value)
    missing = required_keys - actual
    unexpected = actual - allowed
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing {0}".format(", ".join(sorted(missing))))
        if unexpected:
            details.append("unexpected {0}".format(", ".join(sorted(unexpected))))
        raise ValueError("{0} requires exact keys: {1}".format(label, "; ".join(details)))


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))
    return value.strip()


def _bounded_int(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = arguments.get(name, default)
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError(
            "{0} must be an integer in [{1}, {2}]".format(name, minimum, maximum)
        )
    return value


def validate_evidence_arguments(tool_name: str, arguments: Mapping[str, Any]) -> None:
    if tool_name not in EVIDENCE_TOOLS:
        raise ValueError("unsupported offline investigation tool: {0}".format(tool_name))
    if not isinstance(arguments, Mapping):
        raise ValueError("investigation arguments must be an object")
    required, optional = EVIDENCE_ARGUMENT_KEYS[tool_name]
    _require_exact_keys(
        arguments,
        required=required,
        optional=optional,
        label="{0} arguments".format(tool_name),
    )
    if tool_name in {"inspect_node", "expand_upstream", "expand_downstream", "inspect_episode"}:
        _required_text(arguments, "ref")
    elif tool_name == "inspect_artifact":
        _required_text(arguments, "artifact_id")
        _bounded_int(arguments, "offset", default=0, minimum=0, maximum=2**63 - 1)
        _bounded_int(arguments, "length", default=32_000, minimum=1, maximum=1_048_576)
        expected_hash = arguments.get("expected_hash")
        if expected_hash is not None and (
            not isinstance(expected_hash, str) or not expected_hash.strip()
        ):
            raise ValueError("expected_hash must be a non-empty string")
    elif tool_name == "inspect_context_lineage":
        _required_text(arguments, "message_id")
    elif tool_name == "inspect_task_obligations":
        scope = _required_text(arguments, "scope")
        if scope not in {"case", "manifest", "trace"}:
            raise ValueError("scope must be case, manifest, or trace")
    elif tool_name == "compare_causal_paths":
        paths = arguments.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ValueError("paths must be a non-empty list")
        if len(paths) > MAX_CAUSAL_PATHS:
            raise ValueError("causal path count exceeds {0}".format(MAX_CAUSAL_PATHS))
        for path in paths:
            if not isinstance(path, (list, tuple)) or not _strings(path):
                raise ValueError("every causal path must contain at least one ref")
            if len(path) > MAX_CAUSAL_PATH_LENGTH:
                raise ValueError(
                    "causal path length exceeds {0}".format(MAX_CAUSAL_PATH_LENGTH)
                )
    elif tool_name == "search_semantic_nodes":
        _required_text(arguments, "query")
        _required_text(arguments, "before_ref")
        _bounded_int(arguments, "limit", default=12, minimum=1, maximum=64)
        _bounded_int(arguments, "scan_limit", default=4096, minimum=1, maximum=10_000)
    if tool_name in {"expand_upstream", "expand_downstream"}:
        _bounded_int(arguments, "limit", default=24, minimum=1, maximum=96)
        relation_filter = arguments.get("relation_filter")
        if relation_filter is not None and (
            not isinstance(relation_filter, (list, tuple))
            or any(not isinstance(item, str) or not item for item in relation_filter)
        ):
            raise ValueError("relation_filter must be a list of non-empty strings")


@dataclass(frozen=True)
class InvestigationDirective:
    directive_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    requested_by_ref: str
    hypothesis_id: str = ""
    reason: str = ""
    directive_kind: str = "evidence_investigation"

    def __post_init__(self) -> None:
        if self.directive_kind != "evidence_investigation":
            raise ValueError("directive_kind must be evidence_investigation")
        object.__setattr__(self, "arguments", _freeze(self.arguments))

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        requested_by_ref: str,
        hypothesis_id: str = "",
        reason: str,
        validate_tool: bool = True,
        validate_arguments: bool = True,
    ) -> "InvestigationDirective":
        name = str(tool_name or "").strip()
        requested = str(requested_by_ref or "").strip()
        why = str(reason or "").strip()
        if not requested:
            raise ValueError("requested_by_ref must be non-empty")
        if not why:
            raise ValueError("investigation reason must be non-empty")
        if validate_tool and name not in EVIDENCE_TOOLS:
            raise ValueError("unsupported offline investigation tool: {0}".format(name))
        if validate_arguments and name in EVIDENCE_TOOLS:
            validate_evidence_arguments(name, arguments)
        semantic = {
            "tool_name": name,
            "arguments": _thaw(arguments),
            "requested_by_ref": requested,
            "hypothesis_id": str(hypothesis_id or ""),
            "reason": why,
            "directive_kind": "evidence_investigation",
        }
        return cls(_identity("investigation", semantic), **semantic)

    @classmethod
    def from_suggestion(
        cls,
        value: Mapping[str, Any],
        *,
        requested_by_ref: str,
        hypothesis_id: str,
    ) -> "InvestigationDirective":
        _require_exact_keys(
            value,
            required={"tool", "arguments", "reason"},
            label="evidence investigation suggestion",
        )
        arguments = value.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("investigation arguments must be an object")
        return cls.create(
            str(value.get("tool") or ""),
            arguments,
            requested_by_ref=requested_by_ref,
            hypothesis_id=hypothesis_id,
            reason=str(value.get("reason") or ""),
        )

    def to_dict(self) -> JsonDict:
        return {
            "directive_id": self.directive_id,
            "directive_kind": self.directive_kind,
            "tool_name": self.tool_name,
            "arguments": _thaw(self.arguments),
            "requested_by_ref": self.requested_by_ref,
            "hypothesis_id": self.hypothesis_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InvestigationDirective":
        _require_exact_keys(
            value, required=DIRECTIVE_KEYS, label="persisted investigation directive"
        )
        if value.get("directive_kind") != "evidence_investigation":
            raise ValueError("directive_kind must be evidence_investigation")
        restored = cls.create(
            str(value.get("tool_name") or ""),
            value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            requested_by_ref=str(value.get("requested_by_ref") or ""),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            reason=str(value.get("reason") or ""),
        )
        if value.get("directive_id") != restored.directive_id:
            raise ValueError("investigation directive identity mismatch")
        return restored


@dataclass(frozen=True)
class AttributionControlDirective:
    directive_id: str
    action: str
    arguments: Mapping[str, Any]
    requested_by_ref: str
    reason: str = ""
    directive_kind: str = "attribution_control"

    def __post_init__(self) -> None:
        if self.directive_kind != "attribution_control":
            raise ValueError("directive_kind must be attribution_control")
        object.__setattr__(self, "arguments", _freeze(self.arguments))

    @classmethod
    def create(
        cls,
        action: str,
        arguments: Mapping[str, Any],
        *,
        requested_by_ref: str,
        reason: Optional[str] = None,
    ) -> "AttributionControlDirective":
        name = str(action or "").strip()
        if name not in CONTROL_ACTIONS:
            raise ValueError("unsupported attribution control action: {0}".format(name))
        requested = str(requested_by_ref or "").strip()
        if not requested:
            raise ValueError("requested_by_ref must be non-empty")
        if not isinstance(arguments, Mapping):
            raise ValueError("control arguments must be an object")
        _require_exact_keys(
            arguments,
            required=CONTROL_ARGUMENT_KEYS[name],
            label="{0} control arguments".format(name),
        )
        why = str(reason or arguments.get("reason") or "").strip()
        if not why:
            raise ValueError("control directive reason must be non-empty")
        if name in {"reject_hypothesis", "request_root_confirmation"}:
            _required_text(arguments, "hypothesis_id")
        if name == "record_hypothesis":
            _required_text(arguments, "claim")
            _required_text(arguments, "candidate_ref")
        if name == "reject_hypothesis":
            opposing = _strings(arguments.get("opposing_evidence_refs") or [])
            if not opposing:
                raise ValueError(
                    "reject_hypothesis requires grounded opposing evidence; verifier rejection is deferred to Task 7"
                )
        if name == "request_root_confirmation":
            _required_text(arguments, "candidate_ref")
            _required_text(arguments, "defect_fingerprint")
        semantic = {
            "action": name,
            "arguments": _thaw(arguments),
            "requested_by_ref": requested,
            "reason": why,
            "directive_kind": "attribution_control",
        }
        return cls(_identity("control", semantic), **semantic)

    @classmethod
    def from_suggestion(
        cls, value: Mapping[str, Any], *, requested_by_ref: str
    ) -> "AttributionControlDirective":
        _require_exact_keys(
            value,
            required={"action", "arguments", "reason"},
            label="attribution control suggestion",
        )
        arguments = value.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("control arguments must be an object")
        return cls.create(
            str(value.get("action") or ""),
            arguments,
            requested_by_ref=requested_by_ref,
            reason=str(value.get("reason") or ""),
        )

    def to_dict(self) -> JsonDict:
        return {
            "directive_id": self.directive_id,
            "directive_kind": self.directive_kind,
            "action": self.action,
            "arguments": _thaw(self.arguments),
            "requested_by_ref": self.requested_by_ref,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AttributionControlDirective":
        _require_exact_keys(
            value, required=CONTROL_KEYS, label="persisted attribution control directive"
        )
        if value.get("directive_kind") != "attribution_control":
            raise ValueError("directive_kind must be attribution_control")
        restored = cls.create(
            str(value.get("action") or ""),
            value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            requested_by_ref=str(value.get("requested_by_ref") or ""),
            reason=str(value.get("reason") or ""),
        )
        if value.get("directive_id") != restored.directive_id:
            raise ValueError("control directive identity mismatch")
        return restored


@dataclass(frozen=True)
class InvestigationResult:
    directive_id: str
    directive_kind: str
    tool_name: str
    status: str
    requested_refs: Tuple[str, ...] = field(default_factory=tuple)
    resolved_refs: Tuple[str, ...] = field(default_factory=tuple)
    provenance: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    payload: Mapping[str, Any] = field(default_factory=dict)
    truncated: bool = False
    byte_count: int = 0
    artifact_byte_count: int = 0
    evidence_hash: str = ""
    rejection_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.directive_kind != "evidence_investigation":
            raise ValueError("directive_kind must be evidence_investigation")
        if self.status not in RESULT_STATUSES:
            raise ValueError("unsupported investigation result status")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
        if type(self.artifact_byte_count) is not int or self.artifact_byte_count < 0:
            raise ValueError("artifact_byte_count must be a non-negative integer")
        if self.artifact_byte_count > self.byte_count:
            raise ValueError("artifact_byte_count cannot exceed byte_count")
        object.__setattr__(self, "requested_refs", _strings(self.requested_refs))
        object.__setattr__(self, "resolved_refs", _strings(self.resolved_refs))
        object.__setattr__(self, "provenance", tuple(_freeze(item) for item in self.provenance))
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def _create(
        cls,
        directive: InvestigationDirective,
        *,
        status: str,
        requested_refs: Iterable[str] = (),
        resolved_refs: Iterable[str] = (),
        provenance: Iterable[Mapping[str, Any]] = (),
        payload: Optional[Mapping[str, Any]] = None,
        truncated: bool = False,
        byte_count: int = 0,
        artifact_byte_count: int = 0,
        rejection_reason: str = "",
        error: str = "",
    ) -> "InvestigationResult":
        semantic = {
            "requested_refs": list(_strings(requested_refs)),
            "resolved_refs": list(_strings(resolved_refs)),
            "provenance": [_thaw(item) for item in provenance],
            "payload": _thaw(payload or {}),
            "truncated": bool(truncated),
            "rejection_reason": str(rejection_reason or ""),
            "error": str(error or ""),
        }
        result = cls(
            directive_id=directive.directive_id,
            directive_kind=directive.directive_kind,
            tool_name=directive.tool_name,
            status=status,
            requested_refs=tuple(semantic["requested_refs"]),
            resolved_refs=tuple(semantic["resolved_refs"]),
            provenance=tuple(semantic["provenance"]),
            payload=semantic["payload"],
            truncated=bool(truncated),
            byte_count=byte_count,
            artifact_byte_count=artifact_byte_count,
            evidence_hash="",
            rejection_reason=semantic["rejection_reason"],
            error=semantic["error"],
        )
        object.__setattr__(result, "evidence_hash", _result_evidence_hash(result))
        return result

    @classmethod
    def success(cls, directive: InvestigationDirective, **values: Any) -> "InvestigationResult":
        return cls._create(directive, status="success", **values)

    @classmethod
    def rejected(cls, directive: InvestigationDirective, reason: str) -> "InvestigationResult":
        return cls._create(directive, status="rejected", rejection_reason=reason)

    @classmethod
    def failed(cls, directive: InvestigationDirective, error: str) -> "InvestigationResult":
        return cls._create(directive, status="error", error=error)

    def unchanged(
        self, directive: Optional[InvestigationDirective] = None
    ) -> "InvestigationResult":
        result = InvestigationResult(
            directive_id=directive.directive_id if directive is not None else self.directive_id,
            directive_kind=(
                directive.directive_kind if directive is not None else self.directive_kind
            ),
            tool_name=directive.tool_name if directive is not None else self.tool_name,
            status="unchanged",
            requested_refs=self.requested_refs,
            resolved_refs=self.resolved_refs,
            provenance=self.provenance,
            payload=self.payload,
            truncated=self.truncated,
            byte_count=0,
            artifact_byte_count=0,
            evidence_hash="",
            rejection_reason="duplicate_directive_unchanged_evidence",
        )
        object.__setattr__(result, "evidence_hash", _result_evidence_hash(result))
        return result

    def to_dict(self) -> JsonDict:
        return {
            "directive_id": self.directive_id,
            "directive_kind": self.directive_kind,
            "tool_name": self.tool_name,
            "status": self.status,
            "requested_refs": list(self.requested_refs),
            "resolved_refs": list(self.resolved_refs),
            "provenance": [_thaw(item) for item in self.provenance],
            "payload": _thaw(self.payload),
            "truncated": self.truncated,
            "byte_count": self.byte_count,
            "artifact_byte_count": self.artifact_byte_count,
            "evidence_hash": self.evidence_hash,
            "rejection_reason": self.rejection_reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InvestigationResult":
        _require_exact_keys(
            value, required=RESULT_KEYS, label="persisted investigation result"
        )
        if value.get("directive_kind") != "evidence_investigation":
            raise ValueError("directive_kind must be evidence_investigation")
        for key in ("requested_refs", "resolved_refs", "provenance"):
            if not isinstance(value.get(key), list):
                raise ValueError("persisted investigation result {0} must be a list".format(key))
        if any(not isinstance(item, Mapping) for item in value["provenance"]):
            raise ValueError("persisted investigation result provenance entries must be objects")
        if not isinstance(value.get("payload"), Mapping):
            raise ValueError("persisted investigation result payload must be an object")
        if type(value.get("truncated")) is not bool:
            raise ValueError("persisted investigation result truncated must be boolean")
        for key in ("byte_count", "artifact_byte_count"):
            if type(value.get(key)) is not int:
                raise ValueError("persisted investigation result {0} must be an integer".format(key))
        for key in (
            "directive_id",
            "tool_name",
            "status",
            "evidence_hash",
            "rejection_reason",
            "error",
        ):
            if not isinstance(value.get(key), str):
                raise ValueError("persisted investigation result {0} must be a string".format(key))
        restored = cls(
            directive_id=str(value.get("directive_id") or ""),
            directive_kind=str(value.get("directive_kind") or "evidence_investigation"),
            tool_name=str(value.get("tool_name") or ""),
            status=str(value.get("status") or "error"),
            requested_refs=_strings(value.get("requested_refs") or []),
            resolved_refs=_strings(value.get("resolved_refs") or []),
            provenance=tuple(value["provenance"]),
            payload=value["payload"],
            truncated=value["truncated"],
            byte_count=value["byte_count"],
            artifact_byte_count=value["artifact_byte_count"],
            evidence_hash="",
            rejection_reason=str(value.get("rejection_reason") or ""),
            error=str(value.get("error") or ""),
        )
        expected = _result_evidence_hash(restored)
        if value.get("evidence_hash") != expected:
            raise ValueError("investigation result evidence_hash mismatch")
        object.__setattr__(restored, "evidence_hash", expected)
        return restored


def _result_evidence_hash(result: InvestigationResult) -> str:
    semantic = {
        "status": result.status,
        "resolved_refs": list(result.resolved_refs),
        "provenance": [_thaw(item) for item in result.provenance],
        "payload": _thaw(result.payload),
        "truncated": result.truncated,
        "byte_count": result.byte_count,
        "artifact_byte_count": result.artifact_byte_count,
        "rejection_reason": result.rejection_reason,
        "error": result.error,
    }
    return "sha256:{0}".format(
        hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()
    )


class CausalInvestigationTools:
    """Deterministic sidecar tools over an immutable view of one TraceGraph."""

    ALLOWED = EVIDENCE_TOOLS

    def __init__(self, graph: TraceGraph, *, max_artifact_bytes: int = 1_048_576) -> None:
        self.graph = graph
        self.max_artifact_bytes = max(0, int(max_artifact_bytes))
        self.artifact_bytes_used = 0
        self._results: Dict[str, InvestigationResult] = {}
        self._artifact_ranges: Set[Tuple[str, int, int, str]] = set()
        self._artifact_file_identities: Dict[str, Tuple[int, int, int, str]] = {}

    def for_graph(
        self, graph: TraceGraph, *, max_artifact_bytes: Optional[int] = None
    ) -> "CausalInvestigationTools":
        return CausalInvestigationTools(
            graph,
            max_artifact_bytes=(
                self.max_artifact_bytes if max_artifact_bytes is None else max_artifact_bytes
            ),
        )

    def execute(self, directive: InvestigationDirective) -> InvestigationResult:
        execution_key = _identity(
            "tool-call",
            {"tool_name": directive.tool_name, "arguments": _thaw(directive.arguments)},
        )
        previous = self._results.get(execution_key)
        if previous is not None:
            try:
                if directive.tool_name == "inspect_artifact":
                    self._assert_artifact_identity_current(directive)
            except (OSError, ValueError) as exc:
                return InvestigationResult.rejected(
                    directive, "{0}: {1}".format(type(exc).__name__, exc)
                )
            return previous.unchanged(directive)
        if directive.tool_name not in self.ALLOWED:
            result = InvestigationResult.rejected(
                directive, "unsupported offline investigation tool"
            )
            self._results[execution_key] = result
            return result
        try:
            validate_evidence_arguments(directive.tool_name, directive.arguments)
            result = getattr(self, "_{0}".format(directive.tool_name))(directive)
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            result = InvestigationResult.rejected(
                directive, "{0}: {1}".format(type(exc).__name__, exc)
            )
        except Exception as exc:
            result = InvestigationResult.failed(
                directive, "{0}: {1}".format(type(exc).__name__, exc)
            )
        self._results[execution_key] = result
        return result

    @staticmethod
    def _record_provenance(ref: str) -> JsonDict:
        return {
            "ref": ref,
            "resolved_ref": ref,
            "resolution_status": "resolved",
            "provenance_class": "recorded",
            "evidence_type": "trace_record",
        }

    def _resolve_node(self, raw_ref: str) -> Tuple[str, Any]:
        resolved = self.graph.resolve(raw_ref)
        if not resolved or resolved not in self.graph.nodes:
            raise ValueError("unresolved trace node ref: {0}".format(raw_ref))
        return resolved, self.graph.nodes[resolved]

    def _inspect_node(self, directive: InvestigationDirective) -> InvestigationResult:
        raw_ref = _required_text(directive.arguments, "ref")
        resolved, node = self._resolve_node(raw_ref)
        payload = {"node": node.compact(max_chars=16_000), "graph_position": self.graph.position(resolved)}
        encoded = stable_json(payload).encode("utf-8")
        return InvestigationResult.success(
            directive,
            requested_refs=[raw_ref],
            resolved_refs=[resolved],
            provenance=[self._record_provenance(resolved)],
            payload=payload,
            byte_count=len(encoded),
        )

    def _expand_upstream(self, directive: InvestigationDirective) -> InvestigationResult:
        return self._expand(directive, upstream=True)

    def _expand_downstream(self, directive: InvestigationDirective) -> InvestigationResult:
        return self._expand(directive, upstream=False)

    def _expand(self, directive: InvestigationDirective, *, upstream: bool) -> InvestigationResult:
        raw_ref = _required_text(directive.arguments, "ref")
        resolved, _ = self._resolve_node(raw_ref)
        limit = _bounded_int(directive.arguments, "limit", default=24, minimum=1, maximum=96)
        refs = self.graph.upstream_refs(resolved) if upstream else self.graph.downstream_refs(resolved)
        relation_filter = set(_strings(directive.arguments.get("relation_filter") or []))
        rows: List[JsonDict] = []
        for ref in refs:
            edges = (
                self.graph.edge_context(ref, resolved)
                if upstream
                else self.graph.edge_context(resolved, ref)
            )
            if relation_filter:
                edges = [edge for edge in edges if edge.get("relation") in relation_filter]
                if not edges:
                    continue
            rows.append(
                {
                    "ref": ref,
                    "node": self.graph.nodes[ref].compact(),
                    "edges": edges,
                }
            )
            if len(rows) > limit:
                break
        truncated = len(rows) > limit
        selected = rows[:limit]
        provenance = [self._record_provenance(resolved)]
        for row in selected:
            provenance.append(self._record_provenance(str(row["ref"])))
            provenance.extend(dict(edge) for edge in row["edges"])
        return InvestigationResult.success(
            directive,
            requested_refs=[raw_ref],
            resolved_refs=[resolved, *(str(row["ref"]) for row in selected)],
            provenance=provenance,
            payload={"direction": "upstream" if upstream else "downstream", "nodes": selected},
            truncated=truncated,
            byte_count=len(stable_json(selected).encode("utf-8")),
        )

    def _inspect_artifact(self, directive: InvestigationDirective) -> InvestigationResult:
        artifact_id = _required_text(directive.arguments, "artifact_id").removeprefix("artifact:")
        offset = _bounded_int(
            directive.arguments, "offset", default=0, minimum=0, maximum=2**63 - 1
        )
        length = _bounded_int(
            directive.arguments, "length", default=32_000, minimum=1, maximum=1_048_576
        )
        artifact = self.graph._artifact_index.get(artifact_id)
        root = self.graph._artifact_root
        if not isinstance(artifact, Mapping) or root is None:
            raise ValueError("artifact is not grounded in the trace manifest")
        path_value = str(artifact.get("path") or "")
        if not path_value:
            raise ValueError("artifact manifest path is missing")
        resolved_root = Path(root).resolve()
        candidate = (resolved_root / path_value).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("artifact path escapes the trace root") from exc
        stat = candidate.stat()
        size = stat.st_size
        if offset >= size:
            raise ValueError("artifact range offset is at or beyond EOF")
        manifest_hash = str(artifact.get("hash") or "").strip()
        expected_hash = str(directive.arguments.get("expected_hash") or "").strip()
        if expected_hash and expected_hash != manifest_hash:
            raise ValueError("artifact stable identity is stale or mismatched")
        file_identity = (size, stat.st_mtime_ns, stat.st_ino, manifest_hash)
        previous_identity = self._artifact_file_identities.get(artifact_id)
        if previous_identity is not None and previous_identity != file_identity:
            raise ValueError("artifact stable identity changed during investigation")
        self._artifact_file_identities.setdefault(artifact_id, file_identity)
        with candidate.open("rb") as handle:
            handle.seek(offset)
            excerpt = handle.read(length)
        truncated = offset > 0 or offset + len(excerpt) < size
        identity = (
            artifact_id,
            offset,
            len(excerpt),
            manifest_hash or "size:{0}".format(size),
        )
        if identity in self._artifact_ranges:
            raise ValueError("artifact range was already inspected")
        if self.artifact_bytes_used + len(excerpt) > self.max_artifact_bytes:
            return InvestigationResult.rejected(directive, "artifact_byte_budget_exhausted")
        self._artifact_ranges.add(identity)
        self.artifact_bytes_used += len(excerpt)
        status = self.graph.artifact_reference_status("artifact:{0}".format(artifact_id)) or {}
        provenance = {
            **status,
            "ref": "artifact:{0}".format(artifact_id),
            "resolved_ref": "artifact:{0}".format(artifact_id),
            "provenance_class": "recorded",
            "evidence_type": "grounded_artifact_range",
            "offset": offset,
            "length": len(excerpt),
        }
        payload = {
            "artifact_id": artifact_id,
            "stable_identity": {
                "artifact_id": artifact_id,
                "hash": manifest_hash,
                "path": path_value,
                "size_bytes": size,
            },
            "range": {"offset": offset, "requested_length": length, "resolved_length": len(excerpt)},
            "content": excerpt.decode("utf-8", errors="replace"),
            "content_length_bytes": size,
        }
        return InvestigationResult.success(
            directive,
            requested_refs=["artifact:{0}".format(artifact_id)],
            resolved_refs=["artifact:{0}".format(artifact_id)],
            provenance=[provenance],
            payload=payload,
            truncated=truncated,
            byte_count=len(stable_json(payload).encode("utf-8")),
            artifact_byte_count=len(excerpt),
        )

    def _assert_artifact_identity_current(
        self, directive: InvestigationDirective
    ) -> None:
        artifact_id = _required_text(
            directive.arguments, "artifact_id"
        ).removeprefix("artifact:")
        previous = self._artifact_file_identities.get(artifact_id)
        if previous is None:
            return
        artifact = self.graph._artifact_index.get(artifact_id)
        root = self.graph._artifact_root
        if not isinstance(artifact, Mapping) or root is None:
            raise ValueError("artifact stable identity is no longer grounded")
        candidate = (Path(root).resolve() / str(artifact.get("path") or "")).resolve()
        stat = candidate.stat()
        current = (
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ino,
            str(artifact.get("hash") or "").strip(),
        )
        if current != previous:
            raise ValueError("artifact stable identity changed during investigation")

    def _inspect_episode(self, directive: InvestigationDirective) -> InvestigationResult:
        raw_ref = _required_text(directive.arguments, "ref")
        resolved, node = self._resolve_node(raw_ref)
        episode_refs = []
        if node.event_type == "progress.episode":
            episode_refs.append(resolved)
        truncated = False
        for ref in [*self.graph.upstream_refs(resolved), *self.graph.downstream_refs(resolved)]:
            if not (
                self.graph.nodes.get(ref)
                and self.graph.nodes[ref].event_type == "progress.episode"
            ):
                continue
            if ref not in episode_refs:
                episode_refs.append(ref)
            if len(episode_refs) > MAX_EPISODE_REFS:
                truncated = True
                break
        episode_refs = episode_refs[:MAX_EPISODE_REFS]
        payload = {
            "anchor": node.compact(),
            "episodes": [self.graph.nodes[ref].compact(max_chars=16_000) for ref in episode_refs],
        }
        return InvestigationResult.success(
            directive,
            requested_refs=[raw_ref],
            resolved_refs=[resolved, *episode_refs],
            provenance=[self._record_provenance(ref) for ref in [resolved, *episode_refs]],
            payload=payload,
            truncated=truncated,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _inspect_context_lineage(self, directive: InvestigationDirective) -> InvestigationResult:
        message_id = _required_text(directive.arguments, "message_id")
        lineage = self.graph.message_lineage
        matches: List[JsonDict] = []
        scanned = 0
        truncated = False
        for key in ("turns", "edges", "transforms"):
            values = lineage.get(key) or []
            if not isinstance(values, (list, tuple)):
                continue
            for index, item in enumerate(values):
                if scanned >= MAX_CONTEXT_LINEAGE_SCANS:
                    truncated = True
                    break
                scanned += 1
                if isinstance(item, Mapping) and message_id in stable_json(item):
                    if len(matches) >= MAX_CONTEXT_LINEAGE_MATCHES:
                        truncated = True
                        break
                    matches.append(
                        {
                            "path": "message_lineage.{0}[{1}]".format(key, index),
                            "value": _thaw(item),
                        }
                    )
            if truncated:
                break
        if not matches:
            raise ValueError("context message selector is unresolved")
        refs = _strings(
            value
            for item in matches
            for value in re.findall(r"record:[A-Za-z0-9_.:-]+", stable_json(item))
            if value in self.graph.nodes
        )
        payload = {"message_id": message_id, "matches": matches, "scanned_items": scanned}
        return InvestigationResult.success(
            directive,
            requested_refs=["message:{0}".format(message_id)],
            resolved_refs=refs,
            provenance=[
                {
                    "ref": "message:{0}".format(message_id),
                    "resolution_status": "resolved",
                    "provenance_class": "reconstructed",
                    "evidence_type": "offline.message_lineage",
                    "inference_method": "message_lineage_reconstruction",
                }
            ],
            payload=payload,
            truncated=truncated,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _inspect_task_obligations(self, directive: InvestigationDirective) -> InvestigationResult:
        scope = _required_text(directive.arguments, "scope")
        manifest = self.graph.raw_trace.get("manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        sources: List[Tuple[str, Mapping[str, Any]]] = []
        if scope in {"case", "manifest"}:
            sources.append(("trace.manifest", manifest))
        if scope in {"case", "trace"}:
            sources.append(("trace.root", self.graph.raw_trace))
        obligations: List[JsonDict] = []
        provenance: List[JsonDict] = []
        seen: Set[str] = set()
        for source_name, source in sources:
            for key in ("task_obligations", "obligations", "requirements"):
                raw = source.get(key)
                entries = raw if isinstance(raw, list) else [raw]
                for entry in entries:
                    if entry in (None, "", [], {}):
                        continue
                    text = entry if isinstance(entry, str) else stable_json(entry)
                    fact_id = "obligation:{0}".format(
                        hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
                    )
                    if fact_id in seen:
                        continue
                    seen.add(fact_id)
                    obligations.append({"ref": fact_id, "text": text})
                    provenance.append(
                        {
                            "ref": fact_id,
                            "resolved_ref": fact_id,
                            "resolution_status": "resolved",
                            "provenance_class": "recorded",
                            "evidence_type": "trace_task_obligation",
                            "source": "{0}.{1}".format(source_name, key),
                        }
                    )
        if not obligations:
            raise ValueError("task obligation scope resolved no recorded facts")
        payload = {"obligations": obligations}
        return InvestigationResult.success(
            directive,
            requested_refs=["obligations:{0}".format(scope)],
            resolved_refs=[item["ref"] for item in obligations],
            provenance=provenance,
            payload=payload,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _compare_causal_paths(self, directive: InvestigationDirective) -> InvestigationResult:
        raw_paths = directive.arguments.get("paths") or []
        paths = [list(_strings(path)) for path in raw_paths]
        resolved_paths: List[JsonDict] = []
        resolved_refs: List[str] = []
        provenance: List[JsonDict] = []
        for path in paths:
            resolved = [self.graph.resolve(ref) or "" for ref in path]
            valid = all(ref and ref in self.graph.nodes for ref in resolved)
            if not valid:
                raise ValueError(
                    "causal path contains unresolved refs: {0}".format(
                        ", ".join(ref for ref, item in zip(path, resolved) if not item)
                    )
                )
            edges: List[JsonDict] = []
            if valid:
                resolved_refs.extend(resolved)
                provenance.extend(self._record_provenance(ref) for ref in resolved)
                for source, target in zip(resolved, resolved[1:]):
                    hop_edges = [
                        edge
                        for edge in self.graph.edge_context(source, target)
                        if edge.get("eligible_for_attribution") is not False
                        and not is_temporal_only_edge(edge)
                    ]
                    if not hop_edges:
                        raise ValueError(
                            "causal path has no grounded hop from {0} to {1}".format(
                                source, target
                            )
                        )
                    edges.extend(hop_edges)
                    provenance.extend(dict(edge) for edge in hop_edges)
            resolved_paths.append(
                {"requested_path": path, "resolved_path": resolved, "valid": valid, "edges": edges}
            )
        payload = {"paths": resolved_paths}
        return InvestigationResult.success(
            directive,
            requested_refs=[ref for path in paths for ref in path],
            resolved_refs=resolved_refs,
            provenance=provenance,
            payload=payload,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _search_semantic_nodes(self, directive: InvestigationDirective) -> InvestigationResult:
        query = _required_text(directive.arguments, "query")
        before_ref = _required_text(directive.arguments, "before_ref")
        resolved_before, _ = self._resolve_node(before_ref)
        limit = _bounded_int(directive.arguments, "limit", default=12, minimum=1, maximum=64)
        scan_limit = _bounded_int(
            directive.arguments, "scan_limit", default=4096, minimum=1, maximum=10_000
        )
        terms = re.findall(r"[A-Za-z0-9_]{3,}", query)
        normalized_terms = {term.lower() for term in terms}
        before_position = self.graph.position(resolved_before)
        scored: List[JsonDict] = []
        scanned = 0
        scan_truncated = False
        for node in self.graph.nodes.values():
            if scanned >= scan_limit:
                scan_truncated = True
                break
            scanned += 1
            if node.event_type == "progress.episode" or self.graph.position(node.ref) >= before_position:
                continue
            tokens = set(
                re.findall(r"[a-zA-Z0-9_]{3,}", stable_json(node.compact()).lower())
            )
            overlap = len(normalized_terms.intersection(tokens))
            if overlap:
                scored.append(
                    {
                        "ref": node.ref,
                        "score": overlap / max(len(normalized_terms), 1),
                        "evidence_type": "semantic_inferred",
                    }
                )
        scored.sort(
            key=lambda item: (
                -float(item["score"]),
                self.graph.position(str(item["ref"])),
            )
        )
        matches = scored[:limit]
        truncated = scan_truncated or len(scored) > limit
        refs = [str(item.get("ref") or "") for item in matches]
        payload = {"query": query, "before_ref": resolved_before, "matches": matches}
        return InvestigationResult.success(
            directive,
            requested_refs=[before_ref],
            resolved_refs=[resolved_before, *refs],
            provenance=[
                self._record_provenance(resolved_before),
                *(
                    {
                        "ref": ref,
                        "resolved_ref": ref,
                        "resolution_status": "resolved",
                        "provenance_class": "inferred",
                        "evidence_type": "semantic_inferred",
                        "inference_method": "token_overlap_before_anchor",
                    }
                    for ref in refs
                ),
            ],
            payload=payload,
            truncated=truncated,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )


__all__ = [
    "AttributionControlDirective",
    "CausalInvestigationTools",
    "CONTROL_ACTIONS",
    "EVIDENCE_TOOLS",
    "InvestigationDirective",
    "InvestigationResult",
    "validate_evidence_arguments",
]
