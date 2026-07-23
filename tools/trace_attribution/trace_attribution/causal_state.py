"""Serializable state for recursive offline causal attribution."""

from __future__ import annotations

import copy
import hashlib
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
MODERN_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v10"
PREVIOUS_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v2"
LEGACY_REPORT_SCHEMA_VERSION = "recursive-attribution-report/v1-legacy"
GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION = "global-candidate-judgment/v6"
GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION = (
    "global-candidate-judgment/v6+validation-envelope/v6+capsule/v7"
    "+evidence-policy/v5+local-state-owner/v1+global-pass-identity/v1"
    "+failure-action/v1+failure-projection/v2"
)
GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION = (
    "global-candidate-validation-envelope/v6"
)
ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION = (
    "recursive-root-confirmation/v11+resolution/v2+evidence-policy/v5"
    "+artifact-owner/v1+local-state-owner/v1+action-projection/v1"
    "+step-action-projection/v1+confirmation-request-identity/v1"
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
        return {
            "seed_binding_identity": self.seed_binding_identity,
            "hypothesis_id": self.hypothesis_id,
            "visit_key": self.visit_key,
            "occurrence_identity": self.occurrence_identity,
        }

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
                if isinstance(embedded, dict):
                    embedded["semantic_anchor_id"] = anchor
                    embedded["semantic_occurrence_id"] = occurrences_by_ref[str(item.get(ref_key))]

    for section, ref_key in (
        ("causal_candidates", "ref"),
        ("introduction_candidates", "ref"),
        ("confirmed_roots", "node_ref"),
        ("co_roots", "node_ref"),
        ("contributing_conditions", "node_ref"),
        ("amplifying_factors", "node_ref"),
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
    factor_role: str = "unknown"
    competitor_comparisons: Tuple[JsonDict, ...] = field(default_factory=tuple)
    factor_mechanism: JsonDict = field(default_factory=FrozenMapping)

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
    ) -> "RootConfirmation":
        return cls(
            candidate_ref,
            "rejected",
            reason=reason,
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
            evidence_refs=list(evidence_refs or []),
            counterfactual_status="unknown",
            factor_role="unknown",
        )

    def to_dict(self) -> JsonDict:
        return {
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
            "factor_role": self.factor_role,
            "competitor_comparisons": [_thaw(item) for item in self.competitor_comparisons],
            "factor_mechanism": _thaw(self.factor_mechanism),
            "confirmation_identity": self.confirmation_identity,
        }

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RootConfirmation":
        persisted_identity = str(value.get("confirmation_identity") or "")
        if not persisted_identity:
            raise ValueError("RootConfirmation confirmation_identity is required")
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
            factor_role=str(
                value.get("factor_role")
                or {
                    "confirmed": "necessary_cause",
                    "rejected": "unrelated",
                    "unknown": "unknown",
                }.get(status, "unknown")
            ),
            competitor_comparisons=tuple(
                item for item in value.get("competitor_comparisons", []) if isinstance(item, dict)
            ),
            factor_mechanism=_json_dict(value.get("factor_mechanism")),
        )
        if persisted_identity != result.confirmation_identity:
            raise ValueError("RootConfirmation confirmation_identity does not match semantic fields")
        return result


def is_definitive_confirmation(confirmation: RootConfirmation) -> bool:
    """Return whether validated status facts conclusively resolve the candidate."""
    if confirmation.status == "confirmed":
        return confirmation.counterfactual_status == "supports_causality"
    return (
        confirmation.status == "rejected"
        and confirmation.counterfactual_status == "rejects_causality"
    )


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
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        object.__setattr__(self, "evidence_refs", _frozen_strings(self.evidence_refs))
        object.__setattr__(self, "episode_member_refs", _frozen_strings(self.episode_member_refs))
        object.__setattr__(self, "observed_defect_refs", _frozen_strings(self.observed_defect_refs))
        object.__setattr__(self, "recursive_path", _frozen_strings(self.recursive_path))
        object.__setattr__(self, "provenance", FrozenMapping(_thaw(self.provenance)))
        object.__setattr__(self, "confirmation", FrozenMapping(_thaw(self.confirmation)))

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
            confirmation=_json_dict(value.get("confirmation")),
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
            confirmation=_json_dict(value.get("confirmation")),
            provenance=_json_dict(value.get("provenance")),
            mechanism=_json_dict(value.get("mechanism")),
        )


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
            confirmation=_json_dict(value.get("confirmation")),
            provenance=_json_dict(value.get("provenance")),
        )


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
            "persisted global judgment requires v4 migration or rejudgment"
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
        raise ValueError("persisted global judgment v4 schema is incomplete")
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


def validate_confirmation_ownership(
    confirmations: Iterable[RootConfirmation],
    seed_results: Iterable[SeedAttributionResult],
    *,
    label: str,
) -> None:
    """Require an exact two-way link between confirmations and seed entries."""
    confirmation_list = tuple(confirmations)
    seeds = tuple(seed_results)
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
        if not is_definitive_confirmation(confirmation) and (
            owner.outcome not in {"evidence_gap", "inconclusive"}
            or not (owner.missing_evidence or owner.blocking_reasons)
        ):
            raise ValueError(
                "{0} unresolved confirmation requires an evidence_gap or inconclusive "
                "owning seed with concrete blocking or missing-evidence facts".format(
                    label
                )
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
            ):
                raise ValueError("root confirmation identity contradicts root semantic fields")
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
        root_identities = set(primary_identities).union(co_root_identities)
        root_by_identity = {
            root_identity(root, role=role): root
            for role, roots in (
                ("primary root", self.confirmed_roots),
                ("co-root", self.co_roots),
            )
            for root in roots
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
        report_seed_refs = {seed.start_ref for seed in self.seed_results}

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
            if (
                not seed.confirmation_identities
                or not seed.confirmed_root_refs
                or set(confirmed_seed_roots) != {
                    identity
                    for identity, confirmation in seed_confirmations.items()
                    if confirmation.status == "confirmed"
                }
                or {
                    root.node_ref for root in confirmed_seed_roots.values()
                }
                != set(seed.confirmed_root_refs)
                or any(
                    {
                        ref
                        for ref in root.observed_defect_refs
                        if ref in report_seed_refs
                    }
                    != {seed.start_ref}
                    or not has_unambiguous_composite_owner(
                        root,
                        seed,
                        confirmation_by_identity[identity],
                    )
                    for root in confirmed_seed_roots.values()
                )
            ):
                raise ValueError(
                    "confirmed_root seed is not bound to top-level confirmed roots or "
                    "observed_defect_refs owning seed projection or composite owner"
                )
            for identity in confirmed_seed_roots:
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

        factor_role_identities: Dict[str, Set[str]] = {
            "contributing_condition": set(),
            "amplifying_factor": set(),
        }
        for role, factors in (
            ("contributing_condition", self.contributing_conditions),
            ("amplifying_factor", self.amplifying_factors),
        ):
            for factor in factors:
                confirmation = embedded_confirmation(factor.confirmation, role=role)
                identity = confirmation.confirmation_identity
                if (
                    not is_definitive_confirmation(confirmation)
                    or confirmation.status != "rejected"
                    or confirmation.factor_role != role
                    or factor.confirmation_status != "rejected"
                    or factor.node_ref != confirmation.candidate_ref
                    or factor.recursive_path != confirmation.recursive_path
                    or factor.relation != role
                    or factor.reason != confirmation.reason
                    or factor.confidence != confirmation.confidence
                    or factor.evidence_refs != confirmation.evidence_refs
                    or factor.mechanism != confirmation.factor_mechanism
                    or not confirmation.evidence_refs
                    or not confirmation.factor_mechanism
                ):
                    raise ValueError(
                        "{0} has inconsistent confirmation facts or grounded role".format(
                            role
                        )
                    )
                if identity in factor_role_identities[role]:
                    raise ValueError("duplicate {0} confirmation identity".format(role))
                factor_role_identities[role].add(identity)
        if factor_role_identities["contributing_condition"].intersection(
            factor_role_identities["amplifying_factor"]
        ):
            raise ValueError("condition and amplifier roles share a confirmation identity")

        rejected_identities: Set[str] = set()
        for rejected in self.rejected_candidates:
            confirmation = embedded_confirmation(
                rejected.confirmation, role="rejected candidate"
            )
            identity = confirmation.confirmation_identity
            if (
                not is_definitive_confirmation(confirmation)
                or confirmation.status != "rejected"
                or confirmation.factor_role not in {"unrelated", "unknown"}
                or rejected.confirmation_status != "rejected"
                or rejected.node_ref != confirmation.candidate_ref
                or rejected.hypothesis_id != confirmation.hypothesis_id
                or rejected.recursive_path != confirmation.recursive_path
                or rejected.reason != confirmation.reason
                or rejected.confidence != confirmation.confidence
                or rejected.evidence_refs != confirmation.evidence_refs
                or not confirmation.recursive_path
                or not confirmation.evidence_refs
            ):
                raise ValueError(
                    "rejected candidate has inconsistent confirmation facts or confirmation role"
                )
            if identity in rejected_identities:
                raise ValueError("duplicate rejected candidate confirmation identity")
            rejected_identities.add(identity)

        factor_identities = set().union(*factor_role_identities.values())
        if factor_identities.intersection(rejected_identities):
            raise ValueError(
                "factor and rejected roles share a confirmation identity"
            )
        if root_identities.intersection(factor_identities | rejected_identities):
            raise ValueError("confirmation role conflict between root and non-root roles")
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
                or confirmation.hypothesis_id in unresolved_hypothesis_ids
                or confirmation.candidate_ref in self.unresolved_refs
            )
            if not explicitly_unresolved:
                raise ValueError(
                    "orphan confirmed confirmation requires explicit unresolved state"
                )

        validate_confirmation_ownership(
            self.confirmations,
            self.seed_results,
            label="report",
        )

        metadata = _thaw(self.metadata)
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
        object.__setattr__(
            self,
            "analysis_outcome",
            _aggregate_seed_outcomes(self.seed_results),
        )

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
        return {
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

    @classmethod
    def from_dict(cls, value: JsonDict) -> "RecursiveAttributionReport":
        schema_version = str(value.get("schema_version") or "")
        if not schema_version:
            raise ValueError("report schema_version is required")

        def items(key: str, factory: Any, *, strict_objects: bool = False) -> List[Any]:
            raw = value.get(key)
            if not isinstance(raw, list):
                return []
            parsed = []
            for index, item in enumerate(raw):
                if not isinstance(item, Mapping):
                    if strict_objects:
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
        confirmed_roots = items("confirmed_roots", ConfirmedRoot.from_dict)
        co_roots = items("co_roots", ConfirmedRoot.from_dict)
        if not confirmed_roots and value.get("root_causes"):
            raise ValueError(
                "legacy root_causes require schema migration with independent confirmation"
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
            rejected_candidates=items("rejected_candidates", RejectedCandidate.from_dict),
            unresolved_hypotheses=items("unresolved_hypotheses", AttributionHypothesis.from_dict),
            taint_paths=[_string_list(path) for path in value.get("taint_paths", []) if isinstance(path, list)],
            visited_order=_string_list(value.get("visited_order")),
            visited_entries=visited_entries,
            unresolved_refs=unresolved_refs,
            investigation_journal=tuple(
                item
                for item in value.get("investigation_journal", [])
                if isinstance(item, dict)
            ),
            metadata=metadata,
        )


__all__ = [
    "CAUSAL_RELATIONS",
    "AttributionHypothesis",
    "CausalCandidate",
    "CausalFactor",
    "CausalStepJudgment",
    "ConfirmedRoot",
    "DefectState",
    "FrontierItem",
    "HypothesisEvidence",
    "LocalStateOwner",
    "PredecessorAssessment",
    "RecursiveAttributionReport",
    "RejectedCandidate",
    "RootConfirmation",
    "SeedAttributionResult",
    "is_definitive_confirmation",
    "validate_confirmation_ownership",
    "SEMANTIC_ANCHOR_SCHEMA_VERSION",
    "SEMANTIC_OCCURRENCE_SCHEMA_VERSION",
    "annotate_report_semantic_anchors",
    "normalized_anchor_semantics",
    "semantic_anchor_id",
    "semantic_anchor_index",
    "semantic_occurrence_id",
    "semantic_occurrence_index",
    "semantic_visit_key",
    "confirmation_identity_for",
    "seed_binding_identity_for",
    "validate_seed_outcome_payload",
]
