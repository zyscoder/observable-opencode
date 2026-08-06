from __future__ import annotations

import copy
import unittest

from trace_attribution.candidate_clustering import (
    CandidateClusterManifest,
    _structured_values,
    build_candidate_cluster_manifest,
)
from trace_attribution.candidate_budget import candidate_identity
from trace_attribution.causal_state import CausalCandidate
from trace_attribution.graph import TraceGraph
from trace_attribution.models import TraceNode


class SpoofSchemaKey:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        return str(other) == self.value


def record(
    record_id,
    event_type,
    *,
    component="agent",
    source_refs=(),
    data=None,
):
    return {
        "record_id": record_id,
        "component": component,
        "event_type": event_type,
        "title": record_id,
        "status": "completed",
        "timestamp": "2026-07-31T00:00:00Z",
        "data": dict(data or {}),
        "source_refs": list(source_refs),
    }


def candidate(graph, record_id, *, source="attribution_edge", edge=None):
    ref = "record:{0}".format(record_id)
    return CausalCandidate(
        ref=ref,
        node=graph.hydrate_node(ref),
        source=source,
        edge=edge or {},
        evidence_refs=(),
    )


def selection_audit(candidates):
    return [
        {
            "ref": value.ref,
            "discovered_rank": rank,
            "candidate_identity": candidate_identity(value),
            "disposition": "offered" if rank < 6 else "dropped",
            "reason": "input_order" if rank < 6 else "total_limit",
        }
        for rank, value in enumerate(candidates)
    ]


class CandidateClusteringTest(unittest.TestCase):
    def setUp(self):
        self.trace = {
            "schema_version": "causal-ir/v1",
            "case_id": "shadow-clustering",
            "records": [
                record(
                    "action-plan",
                    "decision",
                    data={
                        "decision_type": "reasoning_block",
                        "action_group_id": "action-a",
                        "phase": "planning",
                    },
                ),
                record(
                    "action-result",
                    "tool.result",
                    component="tool",
                    data={
                        "action_group_id": "action-a",
                        "phase": "implementation",
                    },
                ),
                record(
                    "episode-plan",
                    "decision",
                    data={
                        "decision_type": "reasoning_block",
                        "phase": "planning",
                    },
                ),
                record(
                    "episode-change",
                    "change",
                    source_refs=("record:episode-plan",),
                    data={"phase": "implementation"},
                ),
                record(
                    "path-plan",
                    "decision",
                    data={"decision_type": "reasoning_block"},
                ),
                record(
                    "path-verification",
                    "verification",
                    component="verification",
                ),
                record(
                    "shared-materialization",
                    "change",
                    component="tool",
                    data={"phase": "implementation"},
                ),
                record(
                    "fallback-a",
                    "decision",
                    component="planner",
                    data={
                        "decision_type": "reasoning_block",
                        "tool_name": "edit",
                        "file_path": "src/model.py",
                        "symbol": "Model.validate",
                    },
                ),
                record(
                    "fallback-b",
                    "decision",
                    component="planner",
                    data={
                        "decision_type": "reasoning_block",
                        "tool_name": "edit",
                        "file_path": "src/model.py",
                        "symbol": "Model.validate",
                    },
                ),
                record(
                    "seed",
                    "case.observed_defect",
                    component="evaluation",
                ),
            ],
        }
        self.graph = TraceGraph.from_trace(self.trace)
        self.candidates = tuple(
            candidate(self.graph, record_id)
            for record_id in (
                "action-plan",
                "action-result",
                "episode-plan",
                "episode-change",
                "path-plan",
                "path-verification",
                "fallback-a",
                "fallback-b",
            )
        )
        self.paths = {
            value.ref: (value.ref, "record:seed")
            for value in self.candidates
        }
        self.paths["record:path-plan"] = (
            "record:path-plan",
            "record:shared-materialization",
            "record:seed",
        )
        self.paths["record:path-verification"] = (
            "record:path-verification",
            "record:shared-materialization",
            "record:seed",
        )

    def build(self, candidates=None):
        values = tuple(candidates or self.candidates)
        return build_candidate_cluster_manifest(
            graph=self.graph,
            candidates=values,
            candidate_paths=self.paths,
            candidate_audit=selection_audit(values),
            source_selection_identity="a" * 64,
            seed_ref="record:seed",
            defect_fingerprint="defect:omitted-validation",
        )

    def test_every_discovered_candidate_belongs_to_exactly_one_leaf_cluster(self):
        manifest = self.build()
        member_refs = [
            ref
            for cluster in manifest.clusters
            for ref in cluster.member_refs
        ]

        self.assertEqual(manifest.discovered_count, len(self.candidates))
        self.assertCountEqual(
            member_refs,
            [value.ref for value in self.candidates],
        )
        self.assertEqual(len(member_refs), len(set(member_refs)))
        self.assertEqual(
            {fact.ref for fact in manifest.candidate_facts},
            set(member_refs),
        )

    def test_grouping_uses_action_then_confirmed_episode_then_materialization_then_fallback(self):
        manifest = self.build()
        facts = {value.ref: value for value in manifest.candidate_facts}

        self.assertEqual(
            facts["record:action-plan"].cluster_id,
            facts["record:action-result"].cluster_id,
        )
        self.assertEqual(
            facts["record:action-plan"].grouping_level,
            "l0_exact_action",
        )
        self.assertEqual(
            facts["record:episode-plan"].cluster_id,
            facts["record:episode-change"].cluster_id,
        )
        self.assertEqual(
            facts["record:episode-plan"].grouping_level,
            "l1_confirmed_causal_episode",
        )
        self.assertEqual(
            facts["record:path-plan"].cluster_id,
            facts["record:path-verification"].cluster_id,
        )
        self.assertEqual(
            facts["record:path-plan"].grouping_level,
            "l2_materialization_episode",
        )
        self.assertEqual(
            facts["record:fallback-a"].cluster_id,
            facts["record:fallback-b"].cluster_id,
        )
        self.assertEqual(
            facts["record:fallback-a"].grouping_level,
            "l3_structural_fallback",
        )

    def test_cluster_preserves_complete_members_representatives_and_dispositions(self):
        manifest = self.build()
        cluster = next(
            value
            for value in manifest.clusters
            if set(value.member_refs)
            == {"record:action-plan", "record:action-result"}
        )

        self.assertEqual(
            cluster.member_refs,
            ("record:action-plan", "record:action-result"),
        )
        self.assertEqual(
            cluster.representatives["earliest_authored_plan"],
            "record:action-plan",
        )
        self.assertEqual(
            cluster.representatives["latest_execution"],
            "record:action-result",
        )
        self.assertEqual(
            dict(cluster.member_dispositions),
            {
                "record:action-plan": "offered",
                "record:action-result": "offered",
            },
        )
        self.assertEqual(
            cluster.root_eligible_refs,
            ("record:action-plan",),
        )
        self.assertEqual(cluster.expansion_status, "shadow_only")
        self.assertEqual(
            cluster.expansion_reason,
            "shadow_manifest_does_not_control_candidate_scheduling",
        )

    def test_manifest_round_trip_is_identity_stable_and_tamper_evident(self):
        manifest = self.build()
        payload = manifest.to_dict()
        restored = CandidateClusterManifest.from_dict(
            copy.deepcopy(payload),
            graph=self.graph,
        )

        self.assertEqual(restored, manifest)
        self.assertEqual(
            restored.manifest_identity,
            manifest.manifest_identity,
        )

        payload["clusters"][0]["member_refs"].append("record:fallback-a")
        with self.assertRaisesRegex(ValueError, "identity|membership"):
            CandidateClusterManifest.from_dict(payload, graph=self.graph)

    def test_human_labels_and_candidate_scores_do_not_change_semantic_manifest_identity(self):
        baseline = self.build()
        changed = []
        for index, value in enumerate(self.candidates):
            data = dict(value.node.data)
            data["human_labels"] = {
                "root": index == len(self.candidates) - 1,
            }
            changed.append(
                CausalCandidate(
                    ref=value.ref,
                    node=TraceNode(
                        ref=value.node.ref,
                        record_id=value.node.record_id,
                        component=value.node.component,
                        event_type=value.node.event_type,
                        title=value.node.title,
                        status=value.node.status,
                        timestamp=value.node.timestamp,
                        data=data,
                        source_refs=list(value.node.source_refs),
                    ),
                    source=value.source,
                    edge=dict(value.edge),
                    score=0.99 - (index / 100),
                    evidence_refs=value.evidence_refs,
                )
            )

        relabeled = self.build(changed)

        self.assertEqual(
            relabeled.candidate_set_identity,
            baseline.candidate_set_identity,
        )
        self.assertEqual(
            relabeled.manifest_identity,
            baseline.manifest_identity,
        )

    def test_foreign_candidate_path_reference_is_rejected(self):
        paths = dict(self.paths)
        paths["record:fallback-a"] = (
            "record:fallback-a",
            "record:not-in-active-graph",
            "record:seed",
        )

        with self.assertRaisesRegex(ValueError, "active graph"):
            build_candidate_cluster_manifest(
                graph=self.graph,
                candidates=self.candidates,
                candidate_paths=paths,
                candidate_audit=selection_audit(self.candidates),
                source_selection_identity="a" * 64,
                seed_ref="record:seed",
                defect_fingerprint="defect:omitted-validation",
            )

    def test_duplicate_candidate_audit_entry_fails_closed(self):
        audit = selection_audit(self.candidates)
        audit.append(
            {
                **audit[0],
                "disposition": "dropped",
                "reason": "total_limit",
            }
        )

        with self.assertRaisesRegex(ValueError, "duplicate|exactly one"):
            build_candidate_cluster_manifest(
                graph=self.graph,
                candidates=self.candidates,
                candidate_paths=self.paths,
                candidate_audit=audit,
                source_selection_identity="a" * 64,
                seed_ref="record:seed",
                defect_fingerprint="defect:omitted-validation",
            )

    def test_recursive_candidate_metadata_fails_closed_in_bounded_time(self):
        cyclic = {}
        cyclic["metadata"] = cyclic

        with self.assertRaisesRegex(ValueError, "recursive|bounded"):
            _structured_values(cyclic, ("tool_name",))

    def test_manifest_exact_schema_rejects_string_equivalent_non_string_keys(self):
        payload = self.build().to_dict()
        schema = payload.pop("schema")
        payload[SpoofSchemaKey("schema")] = schema

        with self.assertRaisesRegex(ValueError, "non-string|schema mismatch"):
            CandidateClusterManifest.from_dict(payload, graph=self.graph)


if __name__ == "__main__":
    unittest.main()
