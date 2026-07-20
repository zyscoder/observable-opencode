"""Bounded, read-only investigation tools for offline causal attribution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .graph import TraceGraph
from .judgment_context import task_obligations
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
    if tool_name in {"inspect_node", "expand_upstream", "expand_downstream", "inspect_episode"}:
        _required_text(arguments, "ref")
    elif tool_name == "inspect_artifact":
        _required_text(arguments, "artifact_id")
        _bounded_int(arguments, "offset", default=0, minimum=0, maximum=2**63 - 1)
        _bounded_int(arguments, "length", default=32_000, minimum=1, maximum=1_048_576)
    elif tool_name == "inspect_context_lineage":
        _required_text(arguments, "message_id")
    elif tool_name == "inspect_task_obligations":
        _required_text(arguments, "scope")
    elif tool_name == "compare_causal_paths":
        paths = arguments.get("paths")
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ValueError("paths must be a non-empty list")
        for path in paths:
            if not isinstance(path, (list, tuple)) or not _strings(path):
                raise ValueError("every causal path must contain at least one ref")
    elif tool_name == "search_semantic_nodes":
        _required_text(arguments, "query")
        _required_text(arguments, "before_ref")
        _bounded_int(arguments, "limit", default=12, minimum=1, maximum=64)
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
        arguments = value.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ValueError("investigation arguments must be an object")
        return cls.create(
            str(value.get("tool") or value.get("tool_name") or ""),
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
        restored = cls.create(
            str(value.get("tool_name") or ""),
            value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            requested_by_ref=str(value.get("requested_by_ref") or ""),
            hypothesis_id=str(value.get("hypothesis_id") or ""),
            reason=str(value.get("reason") or ""),
        )
        if value.get("directive_id") and value.get("directive_id") != restored.directive_id:
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
            verifier = arguments.get("independent_verifier_result")
            if not opposing and not (
                isinstance(verifier, Mapping)
                and str(verifier.get("status") or "").strip() == "rejected"
            ):
                raise ValueError(
                    "reject_hypothesis requires grounded opposing evidence or a rejected independent verifier result"
                )
        if name == "request_root_confirmation":
            _required_text(arguments, "candidate_ref")
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
        restored = cls.create(
            str(value.get("action") or ""),
            value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            requested_by_ref=str(value.get("requested_by_ref") or ""),
            reason=str(value.get("reason") or ""),
        )
        if value.get("directive_id") and value.get("directive_id") != restored.directive_id:
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
    evidence_hash: str = ""
    rejection_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in RESULT_STATUSES:
            raise ValueError("unsupported investigation result status")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("byte_count must be a non-negative integer")
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
        evidence_hash: str = "",
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
        digest = evidence_hash or "sha256:{0}".format(
            hashlib.sha256(stable_json(semantic).encode("utf-8")).hexdigest()
        )
        return cls(
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
            evidence_hash=digest,
            rejection_reason=semantic["rejection_reason"],
            error=semantic["error"],
        )

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
        return InvestigationResult(
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
            evidence_hash=self.evidence_hash,
            rejection_reason="duplicate_directive_unchanged_evidence",
        )

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
            "evidence_hash": self.evidence_hash,
            "rejection_reason": self.rejection_reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InvestigationResult":
        return cls(
            directive_id=str(value.get("directive_id") or ""),
            directive_kind=str(value.get("directive_kind") or "evidence_investigation"),
            tool_name=str(value.get("tool_name") or ""),
            status=str(value.get("status") or "error"),
            requested_refs=_strings(value.get("requested_refs") or []),
            resolved_refs=_strings(value.get("resolved_refs") or []),
            provenance=tuple(
                item for item in value.get("provenance") or [] if isinstance(item, Mapping)
            ),
            payload=value.get("payload") if isinstance(value.get("payload"), Mapping) else {},
            truncated=bool(value.get("truncated")),
            byte_count=int(value.get("byte_count") or 0),
            evidence_hash=str(value.get("evidence_hash") or ""),
            rejection_reason=str(value.get("rejection_reason") or ""),
            error=str(value.get("error") or ""),
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
            truncated=len(rows) > len(selected),
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
        content = candidate.read_bytes()
        excerpt = content[offset : offset + length]
        truncated = offset > 0 or offset + len(excerpt) < len(content)
        identity = (
            artifact_id,
            offset,
            len(excerpt),
            str(artifact.get("hash") or hashlib.sha256(content).hexdigest()),
        )
        if identity in self._artifact_ranges:
            previous = InvestigationResult.success(
                directive,
                requested_refs=["artifact:{0}".format(artifact_id)],
                resolved_refs=["artifact:{0}".format(artifact_id)],
                provenance=[],
                payload={},
                evidence_hash="sha256:{0}".format(hashlib.sha256(excerpt).hexdigest()),
            )
            return previous.unchanged()
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
                "hash": artifact.get("hash"),
                "path": path_value,
            },
            "range": {"offset": offset, "requested_length": length, "resolved_length": len(excerpt)},
            "content": excerpt.decode("utf-8", errors="replace"),
            "content_length_bytes": len(content),
        }
        return InvestigationResult.success(
            directive,
            requested_refs=["artifact:{0}".format(artifact_id)],
            resolved_refs=["artifact:{0}".format(artifact_id)],
            provenance=[provenance],
            payload=payload,
            truncated=truncated,
            byte_count=len(excerpt),
        )

    def _inspect_episode(self, directive: InvestigationDirective) -> InvestigationResult:
        raw_ref = _required_text(directive.arguments, "ref")
        resolved, node = self._resolve_node(raw_ref)
        episode_refs = []
        if node.event_type == "progress.episode":
            episode_refs.append(resolved)
        episode_refs.extend(
            ref
            for ref in [*self.graph.upstream_refs(resolved), *self.graph.downstream_refs(resolved)]
            if self.graph.nodes.get(ref)
            and self.graph.nodes[ref].event_type == "progress.episode"
        )
        episode_refs = list(_strings(episode_refs))
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
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _inspect_context_lineage(self, directive: InvestigationDirective) -> InvestigationResult:
        message_id = _required_text(directive.arguments, "message_id")
        lineage = self.graph.message_lineage
        matches: List[JsonDict] = []

        def visit(value: Any, path: str) -> None:
            if isinstance(value, Mapping):
                if message_id in stable_json(value):
                    matches.append({"path": path, "value": _thaw(value)})
                return
            if isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    visit(item, "{0}[{1}]".format(path, index))

        for key in ("turns", "edges", "transforms"):
            visit(lineage.get(key) or [], "message_lineage.{0}".format(key))
        refs = _strings(
            value
            for item in matches
            for value in re.findall(r"record:[A-Za-z0-9_.:-]+", stable_json(item))
            if value in self.graph.nodes
        )
        payload = {"message_id": message_id, "matches": matches[:64]}
        return InvestigationResult.success(
            directive,
            requested_refs=["message:{0}".format(message_id)],
            resolved_refs=refs or ["message:{0}".format(message_id)],
            provenance=[
                {
                    "ref": "message:{0}".format(message_id),
                    "resolution_status": "resolved" if matches else "unresolved",
                    "provenance_class": "reconstructed",
                    "evidence_type": "offline.message_lineage",
                    "inference_method": "message_lineage_reconstruction",
                }
            ],
            payload=payload,
            truncated=len(matches) > 64,
            byte_count=len(stable_json(payload).encode("utf-8")),
        )

    def _inspect_task_obligations(self, directive: InvestigationDirective) -> InvestigationResult:
        scope = _required_text(directive.arguments, "scope")
        obligations = task_obligations(self.graph, "")
        payload = {"scope": scope, "obligations": obligations}
        return InvestigationResult.success(
            directive,
            requested_refs=["obligations:{0}".format(scope)],
            resolved_refs=["obligations:{0}".format(scope)],
            provenance=[
                {
                    "ref": "obligations:{0}".format(scope),
                    "resolution_status": "resolved",
                    "provenance_class": "recorded",
                    "evidence_type": "trace_task_obligations",
                }
            ],
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
            edges = []
            if valid:
                resolved_refs.extend(resolved)
                provenance.extend(self._record_provenance(ref) for ref in resolved)
                for source, target in zip(resolved, resolved[1:]):
                    edges.extend(self.graph.edge_context(source, target))
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
        terms = re.findall(r"[A-Za-z0-9_]{3,}", query)
        matches = self.graph.semantic_search(terms, before_ref=resolved_before, limit=limit)
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
