#!/usr/bin/env python3
"""Read-only queries for finalized semantic traces."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from collections import Counter, deque
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Tuple


FINALIZE_HINT = "Run observable-trace finalize and pass its logical trace.json output."
SAFE_DESCRIPTOR_SUPPORT = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


class TraceInputError(ValueError):
    """The supplied path is not a finalized semantic trace."""


class ArtifactIntegrityError(ValueError):
    """A declared Artifact cannot be safely read from the Trace bundle."""


@dataclass(frozen=True)
class NodeView:
    ref: str
    kind: str
    component: str
    status: str
    title: str
    scope: Mapping[str, object]
    payload: Mapping[str, object]
    source_refs: Tuple[str, ...]
    artifact_refs: Tuple[str, ...]


@dataclass(frozen=True)
class EdgeView:
    ref: str
    source: str
    target: str
    relation: str
    eligible_for_attribution: bool
    evidence_refs: Tuple[str, ...]
    evidence_tier: str
    derivation_method: str


def _freeze(value: object) -> object:
    if isinstance(value, MappingABC):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value: object) -> object:
    if isinstance(value, MappingABC):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, MappingABC):
        return value
    return {}


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _normalize_ref(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, MappingABC):
        return ""
    ref_type = _string(value.get("ref_type")) or _string(value.get("type"))
    ref_id = _string(value.get("ref_id")) or _string(value.get("id"))
    return "{0}:{1}".format(ref_type, ref_id) if ref_type and ref_id else ""


def _normalize_refs(value: object) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(ref for ref in (_normalize_ref(item) for item in value) if ref)


def _node_payload(node: Mapping[str, object]) -> Mapping[str, object]:
    payload = node.get("payload")
    if isinstance(payload, MappingABC):
        return payload
    return _mapping(node.get("data"))


def _node_scope(node: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(node.get("scope"))


def _finalized_collections(trace: Mapping[str, object]) -> Tuple[Sequence[object], Sequence[object], bool]:
    required_mappings = ("manifest", "journal", "metrics", "compatibility")
    required_strings = ("trace_version", "causal_ir_version")
    if any(not _string(trace.get(key)) for key in required_strings) or any(
        not isinstance(trace.get(key), MappingABC) for key in required_mappings
    ):
        raise TraceInputError("Trace is missing the finalized Causal IR envelope. {0}".format(FINALIZE_HINT))

    canonical_nodes = trace.get("nodes")
    canonical_edges = trace.get("edges")
    compatibility_nodes = trace.get("records")
    compatibility_edges = trace.get("dataflow_edges")
    has_canonical_fields = "nodes" in trace or "edges" in trace
    has_compatibility_fields = "records" in trace or "dataflow_edges" in trace
    has_canonical = isinstance(canonical_nodes, list) and isinstance(canonical_edges, list)
    has_compatibility = isinstance(compatibility_nodes, list) and isinstance(compatibility_edges, list)
    if (has_canonical_fields and not has_canonical) or (has_compatibility_fields and not has_compatibility):
        raise TraceInputError("Trace has incomplete semantic collections. {0}".format(FINALIZE_HINT))
    if not has_canonical and not has_compatibility:
        raise TraceInputError("Trace is missing semantic node and edge collections. {0}".format(FINALIZE_HINT))
    return (
        canonical_nodes if has_canonical else compatibility_nodes,
        canonical_edges if has_canonical else compatibility_edges,
        not has_canonical,
    )


class TraceIndex:
    def __init__(
        self,
        *,
        causal_ir_version: object,
        lifecycle: Mapping[str, object],
        artifacts: Sequence[object],
        nodes: Sequence[NodeView],
        edges: Sequence[EdgeView],
        traversal_edges: Sequence[EdgeView],
        aliases: Mapping[str, str],
        case_directory: Path,
    ) -> None:
        self.causal_ir_version = causal_ir_version
        self.lifecycle = _freeze(dict(lifecycle))
        self.artifacts = tuple(_freeze(item) for item in artifacts)
        self.nodes = tuple(nodes)
        self.edges = tuple(edges)
        self._traversal_edges = tuple(traversal_edges)
        self._nodes_by_ref = {node.ref: node for node in self.nodes}
        self._aliases = dict(aliases)
        self._case_directory = case_directory
        try:
            self._case_directory_identity = self._file_identity(
                os.stat(str(case_directory), follow_symlinks=False)
            )
        except (OSError, TypeError):
            self._case_directory_identity = None
        self._upstream_edges = {}
        self._downstream_edges = {}
        for edge in self._traversal_edges:
            self._upstream_edges.setdefault(edge.target, []).append(edge)
            self._downstream_edges.setdefault(edge.source, []).append(edge)

    @classmethod
    def load(cls, path: Path) -> "TraceIndex":
        """Validate and normalize one finalized Trace without changing it."""
        if path.name == "records.jsonl" or path.suffix.lower() == ".jsonl":
            raise TraceInputError("Physical segment journals are not queryable. {0}".format(FINALIZE_HINT))
        if path.suffix.lower() != ".json":
            raise TraceInputError("Trace input must be finalized JSON. {0}".format(FINALIZE_HINT))
        try:
            with path.open("r", encoding="utf-8") as handle:
                trace = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TraceInputError("Trace input is not valid finalized JSON. {0}".format(FINALIZE_HINT)) from error

        if not isinstance(trace, MappingABC):
            raise TraceInputError("Trace input must be a JSON object. {0}".format(FINALIZE_HINT))

        source_nodes, source_edges, record_mode = _finalized_collections(trace)
        prefix = "record" if record_mode else "node"
        nodes = []
        aliases = {}

        def register_alias(alias: str, ref: str) -> None:
            previous = aliases.get(alias)
            if previous is not None and previous != ref:
                raise TraceInputError("Trace contains ambiguous alias {0}. {1}".format(alias, FINALIZE_HINT))
            aliases[alias] = ref

        for source in source_nodes:
            if not isinstance(source, MappingABC):
                raise TraceInputError("Trace contains a non-object semantic node. {0}".format(FINALIZE_HINT))
            identifier = _string(source.get("record_id" if record_mode else "node_id"))
            if not identifier:
                raise TraceInputError("Trace contains a semantic node without an id. {0}".format(FINALIZE_HINT))
            ref = "{0}:{1}".format(prefix, identifier)
            if ref in aliases:
                raise TraceInputError("Trace contains duplicate semantic node ids. {0}".format(FINALIZE_HINT))
            register_alias(ref, ref)
            register_alias("{0}:{1}".format("node" if record_mode else "record", identifier), ref)
            for alias in _normalize_refs(source.get("aliases")):
                register_alias(alias, ref)
            nodes.append(
                NodeView(
                    ref=ref,
                    kind=_string(source.get("event_type" if record_mode else "kind")),
                    component=_string(source.get("component")),
                    status=_string(source.get("status")),
                    title=_string(source.get("title")),
                    scope=_freeze(_node_scope(source)),
                    payload=_freeze(_node_payload(source)),
                    source_refs=_normalize_refs(source.get("source_refs")),
                    artifact_refs=_normalize_refs(source.get("artifact_refs")),
                )
            )

        def resolve_endpoint(value: object) -> str:
            reference = _normalize_ref(value)
            if not reference:
                raise TraceInputError("Trace contains an edge without typed endpoints. {0}".format(FINALIZE_HINT))
            return aliases.get(reference, reference)

        edges = []
        for source in source_edges:
            if not isinstance(source, MappingABC):
                raise TraceInputError("Trace contains a non-object semantic edge. {0}".format(FINALIZE_HINT))
            identifier = _string(source.get("edge_id"))
            if not identifier:
                raise TraceInputError("Trace contains a semantic edge without an id. {0}".format(FINALIZE_HINT))
            metadata = _mapping(source.get("metadata"))
            if record_mode:
                relation = (
                    _string(source.get("relation"))
                    or _string(source.get("normalized_relation"))
                    or _string(metadata.get("normalized_relation"))
                    or _string(source.get("original_relation"))
                    or _string(metadata.get("original_relation"))
                )
            else:
                relation = (
                    _string(source.get("original_relation"))
                    or _string(metadata.get("original_relation"))
                    or _string(source.get("normalized_relation"))
                    or _string(source.get("relation"))
                    or _string(metadata.get("normalized_relation"))
                )
            eligibility = source.get("eligible_for_attribution")
            if not isinstance(eligibility, bool):
                eligibility = metadata.get("eligible_for_attribution")
            edges.append(
                EdgeView(
                    ref="edge:{0}".format(identifier),
                    source=resolve_endpoint(source.get("from")),
                    target=resolve_endpoint(source.get("to")),
                    relation=relation,
                    eligible_for_attribution=eligibility is True,
                    evidence_refs=_normalize_refs(source.get("evidence_refs")),
                    evidence_tier=_string(source.get("evidence_tier"))
                    or _string(metadata.get("evidence_tier")),
                    derivation_method=_string(source.get("derivation_method"))
                    or _string(metadata.get("derivation_method")),
                )
            )

        known_refs = {node.ref for node in nodes}
        traversal_edges = [
            edge
            for edge in edges
            if edge.eligible_for_attribution
            and edge.evidence_tier.casefold() != "temporal_advisory"
            and edge.source != edge.target
            and edge.source in known_refs
            and edge.target in known_refs
        ]
        for node in nodes:
            for source_ref in node.source_refs:
                source = aliases.get(source_ref, source_ref)
                if source == node.ref or source not in known_refs:
                    continue
                traversal_edges.append(
                    EdgeView(
                        ref="edge:record_source:{0}:{1}".format(node.ref, source),
                        source=source,
                        target=node.ref,
                        relation="record_source",
                        eligible_for_attribution=True,
                        evidence_refs=(source_ref,),
                        evidence_tier="explicit",
                        derivation_method="source_refs",
                    )
                )

        manifest = _mapping(trace.get("manifest"))
        lifecycle = {"status": _string(manifest.get("status"))}
        return cls(
            causal_ir_version=trace.get("causal_ir_version"),
            lifecycle=lifecycle,
            artifacts=trace.get("artifacts") if isinstance(trace.get("artifacts"), list) else (),
            nodes=nodes,
            edges=edges,
            traversal_edges=traversal_edges,
            aliases=aliases,
            case_directory=path.parent.resolve(),
        )

    def summary(self) -> Mapping[str, object]:
        """Return lifecycle, component, node, edge, and Artifact counts."""
        components = Counter(node.component for node in self.nodes if node.component)
        return {
            "causal_ir_version": self.causal_ir_version,
            "lifecycle": _json_value(self.lifecycle),
            "components": dict(sorted(components.items())),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "eligible_edge_count": sum(edge.eligible_for_attribution for edge in self.edges),
            "artifact_count": len(self.artifacts),
        }

    def search(
        self,
        *,
        query: str = "",
        kind: str = "",
        component: str = "",
        status: str = "",
        limit: int = 50,
    ) -> Sequence[NodeView]:
        """Return matching nodes in recorded order up to one response limit."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = query.casefold()
        kind = kind.casefold()
        component = component.casefold()
        status = status.casefold()
        matches = []
        for node in self.nodes:
            if kind and node.kind.casefold() != kind:
                continue
            if component and node.component.casefold() != component:
                continue
            if status and node.status.casefold() != status:
                continue
            searchable = json.dumps(_node_to_dict(node), ensure_ascii=False, sort_keys=True).casefold()
            if query and query not in searchable:
                continue
            matches.append(node)
            if len(matches) == limit:
                break
        return tuple(matches)

    def node(self, ref: str) -> Mapping[str, object]:
        """Return one hydrated node plus explicit incoming and outgoing edges."""
        canonical_ref = self._aliases.get(ref, ref)
        node = self._nodes_by_ref.get(canonical_ref)
        if node is None:
            raise KeyError(ref)
        payload = _node_to_dict(node)
        payload["incoming_edges"] = [_edge_to_dict(edge) for edge in self.edges if edge.target == node.ref]
        payload["outgoing_edges"] = [_edge_to_dict(edge) for edge in self.edges if edge.source == node.ref]
        return payload

    def neighbors(
        self,
        ref: str,
        direction: str,
        depth: int,
        limit: int,
    ) -> Mapping[str, object]:
        """Return bounded eligible upstream or downstream recorded edges."""
        if direction not in ("upstream", "downstream"):
            raise ValueError("direction must be upstream or downstream")
        if depth < 1:
            raise ValueError("depth must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        start_ref = self._canonical_node_ref(ref)
        adjacency = self._upstream_edges if direction == "upstream" else self._downstream_edges
        queue = deque([(start_ref, 0)])
        visited = {start_ref}
        edges = []
        remaining = []

        while queue:
            current_ref, current_depth = queue.popleft()
            if current_depth == depth:
                continue
            candidates = adjacency.get(current_ref, ())
            for position, edge in enumerate(candidates):
                if len(edges) == limit:
                    remaining.extend(
                        self._edge_neighbor(candidate, direction) for candidate in candidates[position:]
                    )
                    remaining.append(current_ref)
                    remaining.extend(item[0] for item in queue)
                    return self._neighbors_response(
                        start_ref, direction, depth, limit, edges, remaining
                    )
                edges.append(_edge_to_dict(edge))
                neighbor = self._edge_neighbor(edge, direction)
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                if current_depth + 1 < depth:
                    queue.append((neighbor, current_depth + 1))
                elif adjacency.get(neighbor):
                    remaining.append(neighbor)

        return self._neighbors_response(start_ref, direction, depth, limit, edges, remaining)

    def backward_paths(
        self,
        start_ref: str,
        max_depth: int,
        limit: int,
    ) -> Mapping[str, object]:
        """Return bounded breadth-first paths over eligible recorded edges."""
        if max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        canonical_start = self._canonical_node_ref(start_ref)
        queue = deque([((canonical_start,), ())])
        paths = []
        remaining = []

        while queue and len(paths) < limit:
            node_refs, path_edges = queue.popleft()
            current_ref = node_refs[-1]
            candidates = [
                edge for edge in self._upstream_edges.get(current_ref, ()) if edge.source not in node_refs
            ]
            if len(path_edges) == max_depth:
                paths.append(self._path_to_dict(node_refs, path_edges))
                if candidates:
                    remaining.append(current_ref)
                continue
            if not candidates:
                paths.append(self._path_to_dict(node_refs, path_edges))
                continue
            for edge in candidates:
                if len(queue) >= limit:
                    remaining.append(edge.source)
                    continue
                queue.append((node_refs + (edge.source,), path_edges + (edge,)))

        remaining.extend(node_refs[-1] for node_refs, _ in queue)
        return {
            "start_ref": canonical_start,
            "paths": paths,
            "limit": limit,
            "max_depth": max_depth,
            "truncated": bool(remaining),
            "remaining_frontier_refs": self._unique_refs(remaining),
        }

    def artifact(self, artifact_id: str, max_chars: int) -> Mapping[str, object]:
        """Hydrate one declared UTF-8 Artifact after verifying its SHA-256."""
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")
        artifact = next(
            (
                _mapping(item)
                for item in self.artifacts
                if _string(_mapping(item).get("artifact_id")) == artifact_id
            ),
            None,
        )
        if artifact is None:
            raise KeyError(artifact_id)
        relative_path = _string(artifact.get("path"))
        digest = self._artifact_digest(artifact, artifact_id)
        if not relative_path:
            raise ArtifactIntegrityError("Artifact {0} has no declared path".format(artifact_id))
        content_bytes = self._read_artifact_bytes(relative_path, artifact_id)
        observed_digest = hashlib.sha256(content_bytes).hexdigest()
        if observed_digest != digest:
            raise ArtifactIntegrityError("Artifact {0} hash mismatch".format(artifact_id))
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ArtifactIntegrityError("Artifact {0} is not UTF-8 text".format(artifact_id)) from error
        return {
            "artifact_id": artifact_id,
            "path": relative_path,
            "integrity": "verified",
            "content_sha256": observed_digest,
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }

    def _canonical_node_ref(self, ref: str) -> str:
        canonical_ref = self._aliases.get(ref, ref)
        if canonical_ref not in self._nodes_by_ref:
            raise KeyError(ref)
        return canonical_ref

    @staticmethod
    def _edge_neighbor(edge: EdgeView, direction: str) -> str:
        return edge.source if direction == "upstream" else edge.target

    def _neighbors_response(
        self,
        ref: str,
        direction: str,
        depth: int,
        limit: int,
        edges: Sequence[Mapping[str, object]],
        remaining: Sequence[str],
    ) -> Mapping[str, object]:
        remaining_refs = self._unique_refs(remaining)
        return {
            "ref": ref,
            "direction": direction,
            "depth": depth,
            "limit": limit,
            "edges": list(edges),
            "truncated": bool(remaining_refs),
            "remaining_frontier_refs": remaining_refs,
        }

    @staticmethod
    def _path_to_dict(
        node_refs: Sequence[str], edges: Sequence[EdgeView]
    ) -> Mapping[str, object]:
        return {
            "node_refs": list(node_refs),
            "edge_refs": [edge.ref for edge in edges],
            "edges": [_edge_to_dict(edge) for edge in edges],
        }

    @staticmethod
    def _unique_refs(refs: Sequence[str]) -> Sequence[str]:
        return list(dict.fromkeys(ref for ref in refs if ref))

    @staticmethod
    def _sha256_digest(value: str) -> str:
        if value.startswith("sha256:"):
            value = value[len("sha256:") :]
        if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
            return ""
        return value.casefold()

    def _artifact_digest(self, artifact: Mapping[str, object], artifact_id: str) -> str:
        """Use content_hash when declared, requiring both declarations to agree."""
        hash_digest = self._declared_digest(artifact, "hash", artifact_id)
        content_digest = self._declared_digest(artifact, "content_hash", artifact_id)
        if hash_digest and content_digest and hash_digest != content_digest:
            raise ArtifactIntegrityError(
                "Artifact {0} has conflicting SHA-256 declarations".format(artifact_id)
            )
        digest = content_digest or hash_digest
        if not digest:
            raise ArtifactIntegrityError("Artifact {0} has an invalid SHA-256 digest".format(artifact_id))
        return digest

    def _declared_digest(self, artifact: Mapping[str, object], key: str, artifact_id: str) -> str:
        if key not in artifact:
            return ""
        digest = self._sha256_digest(_string(artifact.get(key)))
        if not digest:
            raise ArtifactIntegrityError(
                "Artifact {0} has an invalid {1} SHA-256 digest".format(artifact_id, key)
            )
        return digest

    @staticmethod
    def _file_identity(file_stat: os.stat_result) -> Tuple[int, int]:
        return file_stat.st_dev, file_stat.st_ino

    @staticmethod
    def _descriptor_support_available() -> bool:
        return SAFE_DESCRIPTOR_SUPPORT

    @classmethod
    def _same_file(cls, first: os.stat_result, second: os.stat_result) -> bool:
        return cls._file_identity(first) == cls._file_identity(second)

    def _read_artifact_bytes(self, declared_path: str, artifact_id: str) -> bytes:
        """Read one regular Artifact through no-follow descriptors rooted at the case directory."""
        if not self._descriptor_support_available() or self._case_directory_identity is None:
            raise ArtifactIntegrityError(
                "Artifact {0} cannot be safely opened on this platform".format(artifact_id)
            )
        relative_path = Path(declared_path)
        if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
            raise ArtifactIntegrityError("Artifact {0} path escapes the Trace case directory".format(artifact_id))

        directory_fd = None
        artifact_fd = None
        try:
            expected_root = os.stat(str(self._case_directory), follow_symlinks=False)
            directory_fd = os.open(
                str(self._case_directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            opened_root = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(opened_root.st_mode)
                or self._file_identity(expected_root) != self._case_directory_identity
                or not self._same_file(expected_root, opened_root)
            ):
                raise ArtifactIntegrityError(
                    "Artifact {0} Trace case directory changed during access".format(artifact_id)
                )

            for component in relative_path.parts[:-1]:
                next_fd = None
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                    opened_directory = os.fstat(next_fd)
                    named_directory = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(opened_directory.st_mode) or not self._same_file(
                        named_directory, opened_directory
                    ):
                        raise ArtifactIntegrityError(
                            "Artifact {0} path cannot be safely opened".format(artifact_id)
                        )
                    previous_fd = directory_fd
                    directory_fd = next_fd
                    next_fd = None
                    os.close(previous_fd)
                finally:
                    if next_fd is not None:
                        os.close(next_fd)

            artifact_fd = os.open(
                relative_path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            opened_artifact = os.fstat(artifact_fd)
            named_artifact = os.stat(
                relative_path.parts[-1], dir_fd=directory_fd, follow_symlinks=False
            )
            if not stat.S_ISREG(opened_artifact.st_mode) or not self._same_file(
                named_artifact, opened_artifact
            ):
                raise ArtifactIntegrityError("Artifact {0} path cannot be safely opened".format(artifact_id))
            chunks = []
            while True:
                chunk = os.read(artifact_fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        except ArtifactIntegrityError:
            raise
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ArtifactIntegrityError(
                    "Artifact {0} path contains a symlink".format(artifact_id)
                ) from error
            if error.errno in (errno.ENOENT, errno.ENOTDIR):
                raise ArtifactIntegrityError("Artifact {0} is missing".format(artifact_id)) from error
            raise ArtifactIntegrityError(
                "Artifact {0} path cannot be safely opened".format(artifact_id)
            ) from error
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)
            if directory_fd is not None:
                os.close(directory_fd)


def _node_to_dict(node: NodeView) -> Mapping[str, object]:
    return {
        "ref": node.ref,
        "kind": node.kind,
        "component": node.component,
        "status": node.status,
        "title": node.title,
        "scope": _json_value(node.scope),
        "payload": _json_value(node.payload),
        "source_refs": list(node.source_refs),
        "artifact_refs": list(node.artifact_refs),
    }


def _edge_to_dict(edge: EdgeView) -> Mapping[str, object]:
    return {
        "ref": edge.ref,
        "source": edge.source,
        "target": edge.target,
        "relation": edge.relation,
        "eligible_for_attribution": edge.eligible_for_attribution,
        "evidence_refs": list(edge.evidence_refs),
        "evidence_tier": edge.evidence_tier,
        "derivation_method": edge.derivation_method,
    }


def _write_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read a finalized semantic trace without modifying it.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "summary"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--trace", required=True, type=Path)
    search = commands.add_parser("search")
    search.add_argument("--trace", required=True, type=Path)
    search.add_argument("--query", default="")
    search.add_argument("--kind", default="")
    search.add_argument("--component", default="")
    search.add_argument("--status", default="")
    search.add_argument("--limit", default=50, type=int)
    node = commands.add_parser("node")
    node.add_argument("--trace", required=True, type=Path)
    node.add_argument("--ref", required=True)
    neighbors = commands.add_parser("neighbors")
    neighbors.add_argument("--trace", required=True, type=Path)
    neighbors.add_argument("--ref", required=True)
    neighbors.add_argument("--direction", choices=("upstream", "downstream"), required=True)
    neighbors.add_argument("--depth", default=1, type=int)
    neighbors.add_argument("--limit", default=50, type=int)
    paths = commands.add_parser("paths")
    paths.add_argument("--trace", required=True, type=Path)
    paths.add_argument("--start", required=True)
    paths.add_argument("--max-depth", default=8, type=int)
    paths.add_argument("--limit", default=50, type=int)
    artifact = commands.add_parser("artifact")
    artifact.add_argument("--trace", required=True, type=Path)
    artifact.add_argument("--id", required=True)
    artifact.add_argument("--max-chars", default=20000, type=int)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        index = TraceIndex.load(args.trace)
        if args.command == "validate":
            _write_json({"valid": True, "causal_ir_version": index.causal_ir_version})
        elif args.command == "summary":
            _write_json(index.summary())
        elif args.command == "search":
            nodes = index.search(
                query=args.query,
                kind=args.kind,
                component=args.component,
                status=args.status,
                limit=args.limit,
            )
            _write_json({"nodes": [_node_to_dict(node) for node in nodes], "limit": args.limit})
        elif args.command == "node":
            _write_json(index.node(args.ref))
        elif args.command == "neighbors":
            _write_json(index.neighbors(args.ref, args.direction, args.depth, args.limit))
        elif args.command == "paths":
            _write_json(index.backward_paths(args.start, args.max_depth, args.limit))
        else:
            _write_json(index.artifact(args.id, args.max_chars))
    except ArtifactIntegrityError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (TraceInputError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
