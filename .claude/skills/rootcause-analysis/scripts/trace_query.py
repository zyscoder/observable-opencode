#!/usr/bin/env python3
"""Read-only queries for finalized semantic traces."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Sequence, Tuple


FINALIZE_HINT = "Run observable-trace finalize and pass its logical trace.json output."


class TraceInputError(ValueError):
    """The supplied path is not a finalized semantic trace."""


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


class TraceIndex:
    def __init__(
        self,
        *,
        causal_ir_version: object,
        lifecycle: Mapping[str, object],
        artifacts: Sequence[object],
        nodes: Sequence[NodeView],
        edges: Sequence[EdgeView],
        aliases: Mapping[str, str],
    ) -> None:
        self.causal_ir_version = causal_ir_version
        self.lifecycle = _freeze(dict(lifecycle))
        self.artifacts = tuple(_freeze(item) for item in artifacts)
        self.nodes = tuple(nodes)
        self.edges = tuple(edges)
        self._nodes_by_ref = {node.ref: node for node in self.nodes}
        self._aliases = dict(aliases)

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

        canonical_nodes = trace.get("nodes")
        canonical_edges = trace.get("edges")
        compatibility_nodes = trace.get("records")
        compatibility_edges = trace.get("dataflow_edges")
        has_canonical = isinstance(canonical_nodes, list) and isinstance(canonical_edges, list)
        has_compatibility = isinstance(compatibility_nodes, list) and isinstance(compatibility_edges, list)
        if not has_canonical and not has_compatibility:
            raise TraceInputError("Trace is missing semantic node and edge collections. {0}".format(FINALIZE_HINT))

        source_nodes = canonical_nodes if has_canonical else compatibility_nodes
        source_edges = canonical_edges if has_canonical else compatibility_edges
        record_mode = not has_canonical
        prefix = "record" if record_mode else "node"
        nodes = []
        aliases = {}

        for source in source_nodes:
            if not isinstance(source, MappingABC):
                raise TraceInputError("Trace contains a non-object semantic node. {0}".format(FINALIZE_HINT))
            identifier = _string(source.get("record_id" if record_mode else "node_id"))
            if not identifier:
                raise TraceInputError("Trace contains a semantic node without an id. {0}".format(FINALIZE_HINT))
            ref = "{0}:{1}".format(prefix, identifier)
            if ref in aliases:
                raise TraceInputError("Trace contains duplicate semantic node ids. {0}".format(FINALIZE_HINT))
            aliases[ref] = ref
            aliases["{0}:{1}".format("node" if record_mode else "record", identifier)] = ref
            for alias in _normalize_refs(source.get("aliases")):
                aliases[alias] = ref
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
            relation = _string(source.get("normalized_relation")) or _string(source.get("relation"))
            edges.append(
                EdgeView(
                    ref="edge:{0}".format(identifier),
                    source=resolve_endpoint(source.get("from")),
                    target=resolve_endpoint(source.get("to")),
                    relation=relation,
                    eligible_for_attribution=source.get("eligible_for_attribution") is True,
                    evidence_refs=_normalize_refs(source.get("evidence_refs")),
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
            aliases=aliases,
        )

    def summary(self) -> Mapping[str, object]:
        """Return lifecycle, component, node, edge, and Artifact counts."""
        components = Counter(node.component for node in self.nodes if node.component)
        components.setdefault("agent", 0)
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
        else:
            _write_json(index.node(args.ref))
    except (TraceInputError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
