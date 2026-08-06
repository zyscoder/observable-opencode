#!/usr/bin/env python3
"""Build and evaluate shadow candidate manifests without Provider calls."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from trace_attribution.candidate_clustering import (
    CandidateClusterManifest,
)
from trace_attribution.cli import analysis_start_refs, load_graph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", required=True)
    parser.add_argument("--review")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--start-ref",
        action="append",
        default=[],
        help="Explicit observed-defect ref; may be repeated.",
    )
    parser.add_argument(
        "--objective",
        default="Find the causal origin of the observed task-quality defect.",
    )
    parser.add_argument(
        "--analysis-perspective",
        default="Improve Agent task execution quality.",
    )
    return parser.parse_args()


def manifest_metrics(
    manifest: CandidateClusterManifest,
    *,
    candidate_audit: Sequence[Mapping[str, Any]],
    human_root_refs: Sequence[str],
    page_size: int = 8,
) -> dict[str, Any]:
    disposition_by_ref = {
        str(item.get("ref") or ""): str(
            item.get("disposition") or ""
        )
        for item in candidate_audit
    }
    offered_refs = {
        ref
        for ref, disposition in disposition_by_ref.items()
        if disposition == "offered"
    }
    discovered_roots = tuple(
        ref for ref in human_root_refs if ref in manifest.discovered_refs
    )
    offered_roots = tuple(
        ref for ref in discovered_roots if ref in offered_refs
    )
    cluster_by_ref = {
        ref: cluster
        for cluster in manifest.clusters
        for ref in cluster.member_refs
    }
    root_groups = [
        {
            "root_ref": root_ref,
            "cluster_id": cluster_by_ref[root_ref].cluster_id,
            "grouping_level": (
                cluster_by_ref[root_ref].grouping_level
            ),
            "member_count": len(
                cluster_by_ref[root_ref].member_refs
            ),
            "member_refs": list(
                cluster_by_ref[root_ref].member_refs
            ),
            "representatives": dict(
                cluster_by_ref[root_ref].representatives
            ),
            "root_is_representative": root_ref
            in {
                value
                for value in cluster_by_ref[
                    root_ref
                ].representatives.values()
                if value
            },
        }
        for root_ref in discovered_roots
    ]
    offered_cluster_ids = {
        cluster.cluster_id
        for cluster in manifest.clusters
        if set(cluster.member_refs) & offered_refs
    }
    level_counts: dict[str, int] = {}
    for cluster in manifest.clusters:
        level_counts[cluster.grouping_level] = (
            level_counts.get(cluster.grouping_level, 0) + 1
        )
    cluster_count = len(manifest.clusters)
    offered_count = len(offered_refs)
    offered_cluster_count = len(offered_cluster_ids)
    return {
        "candidate_membership_recall": (
            1.0
            if manifest.discovered_count
            == len(
                {
                    ref
                    for cluster in manifest.clusters
                    for ref in cluster.member_refs
                }
            )
            else 0.0
        ),
        "duplicate_membership_count": (
            sum(len(cluster.member_refs) for cluster in manifest.clusters)
            - len(
                {
                    ref
                    for cluster in manifest.clusters
                    for ref in cluster.member_refs
                }
            )
        ),
        "discovered_count": manifest.discovered_count,
        "offered_count": offered_count,
        "cluster_count": cluster_count,
        "offered_cluster_count": offered_cluster_count,
        "singleton_cluster_count": sum(
            1
            for cluster in manifest.clusters
            if len(cluster.member_refs) == 1
        ),
        "cluster_counts_by_level": dict(sorted(level_counts.items())),
        "current_initial_pages": math.ceil(
            offered_count / page_size
        ),
        "shadow_group_catalog_pages": math.ceil(
            offered_cluster_count / page_size
        ),
        "offered_catalog_reduction_ratio": (
            0.0
            if not offered_count
            else 1.0 - (offered_cluster_count / offered_count)
        ),
        "human_root_count": len(human_root_refs),
        "human_root_discovered_count": len(discovered_roots),
        "human_root_offered_count": len(offered_roots),
        "human_root_group_coverage": (
            1.0
            if not human_root_refs
            else len(discovered_roots) / len(human_root_refs)
        ),
        "simulated_expansion_root_recall": (
            1.0
            if not human_root_refs
            else len(discovered_roots) / len(human_root_refs)
        ),
        "root_groups": root_groups,
        "manifest_identity": manifest.manifest_identity,
        "candidate_set_identity": manifest.candidate_set_identity,
        "source_selection_identity": (
            manifest.source_selection_identity
        ),
    }


def main() -> int:
    args = parse_args()
    trace_path = Path(args.trace)
    graph = load_graph(
        trace_path,
        Path(args.review) if args.review else None,
    )
    starts = analysis_start_refs(graph, args.start_ref)
    labels = json.loads(
        Path(args.labels).read_text(encoding="utf-8")
    )
    human_root_refs = tuple(
        str(item.get("node_ref") or "")
        for item in labels.get("roots") or ()
        if isinstance(item, Mapping)
        and str(item.get("node_ref") or "")
    )
    state = RecursiveAnalysisState.create(
        graph=graph,
        start_refs=starts,
        objective=args.objective,
        analysis_perspective=args.analysis_perspective,
    )
    analyzer = AgenticRecursiveAnalyzer(judge=None)
    passes = []
    seen_passes = set()
    for item in state.frontier.lifecycle_items():
        _, _, funnel = analyzer._global_candidate_pool(
            state, graph, item
        )
        selection_identity = str(
            funnel.get("selection_identity") or ""
        )
        pass_key = (
            item.seed_binding_identity,
            selection_identity,
        )
        if pass_key in seen_passes:
            continue
        seen_passes.add(pass_key)
        event = next(
            entry
            for entry in reversed(state.investigation_journal)
            if entry.get("kind")
            == "candidate_cluster_manifest_shadow"
            and entry.get("seed_binding_identity")
            == item.seed_binding_identity
            and entry.get("source_selection_identity")
            == selection_identity
        )
        manifest = CandidateClusterManifest.from_dict(
            event["manifest"],
            graph=graph,
        )
        passes.append(
            {
                "frontier_ref": item.node_ref,
                "seed_binding_identity": (
                    item.seed_binding_identity
                ),
                "defect_fingerprint": (
                    item.defect_state.fingerprint
                ),
                "candidate_funnel": funnel,
                "manifest": manifest.to_dict(),
                "metrics": manifest_metrics(
                    manifest,
                    candidate_audit=funnel["candidate_audit"],
                    human_root_refs=human_root_refs,
                ),
            }
        )

    payload = {
        "schema": "candidate-cluster-shadow-evaluation/v1",
        "case_id": graph.case_id,
        "trace_path": str(trace_path),
        "review_path": str(args.review or ""),
        "labels_path": str(args.labels),
        "provider_request_count": 0,
        "start_refs": list(starts),
        "human_root_refs": list(human_root_refs),
        "passes": passes,
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
