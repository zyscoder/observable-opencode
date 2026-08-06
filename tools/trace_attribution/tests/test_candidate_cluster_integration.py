from __future__ import annotations

import copy
import hashlib
import unittest

from trace_attribution.candidate_clustering import (
    CandidateClusterManifest,
    validate_candidate_cluster_shadow_event,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.causal_state import RecursiveAttributionReport
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tests.test_recursive_analyzer import (
    FusionScriptedJudge,
    ScriptedCausalJudge,
    observed_trace,
)


class CandidateClusterIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.trace = observed_trace(branching=True)
        self.trace_hash = hashlib.sha256(
            stable_json(self.trace).encode("utf-8")
        ).hexdigest()
        self.graph = TraceGraph.from_trace(self.trace)
        self.state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the omitted namespace method.",
            analysis_perspective="task quality",
        )
        self.item = self.state.frontier.lifecycle_items()[0]
        self.analyzer = AgenticRecursiveAnalyzer(
            judge=ScriptedCausalJudge({})
        )

    def shadow_events(self):
        return [
            value
            for value in self.state.investigation_journal
            if value.get("kind")
            == "candidate_cluster_manifest_shadow"
        ]

    def test_global_pool_persists_complete_shadow_manifest_without_changing_funnel(self):
        candidates, paths, funnel = self.analyzer._global_candidate_pool(
            self.state,
            self.graph,
            self.item,
        )

        self.assertEqual(len(self.shadow_events()), 1)
        event = self.shadow_events()[0]
        self.assertEqual(
            set(event),
            {
                "kind",
                "event_schema",
                "seed_binding_identity",
                "source_selection_identity",
                "manifest_identity",
                "manifest",
                "behavior_impact",
            },
        )
        manifest = CandidateClusterManifest.from_dict(
            event["manifest"],
            graph=self.graph,
        )
        validate_candidate_cluster_shadow_event(
            event,
            graph=self.graph,
            expected_seed_binding_identity=(
                self.item.seed_binding_identity
            ),
            expected_source_selection_identity=(
                funnel["selection_identity"]
            ),
        )
        self.assertEqual(
            event["source_selection_identity"],
            funnel["selection_identity"],
        )
        self.assertEqual(
            manifest.discovered_count,
            funnel["discovered_count"],
        )
        self.assertEqual(
            manifest.discovered_refs,
            tuple(
                value["ref"] for value in funnel["candidate_audit"]
            ),
        )
        self.assertEqual(
            [value.ref for value in candidates],
            [
                value["ref"]
                for value in sorted(
                    (
                        value
                        for value in funnel["candidate_audit"]
                        if value["disposition"]
                        in {"offered", "evidence_context"}
                    ),
                    key=lambda value: (
                        0
                        if value["disposition"] == "offered"
                        else 1,
                        (
                            value["offered_rank"]
                            if value["disposition"] == "offered"
                            else value["context_rank"]
                        ),
                    ),
                )
            ],
        )
        self.assertEqual(set(paths), {value.ref for value in candidates})
        self.assertEqual(
            event["behavior_impact"],
            "none_offline_analysis_only",
        )
        self.assertEqual(
            hashlib.sha256(
                stable_json(self.trace).encode("utf-8")
            ).hexdigest(),
            self.trace_hash,
        )

    def test_identical_pool_replay_deduplicates_shadow_manifest_event(self):
        first = self.analyzer._global_candidate_pool(
            self.state,
            self.graph,
            self.item,
        )
        second = self.analyzer._global_candidate_pool(
            self.state,
            self.graph,
            self.item,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(self.shadow_events()), 1)

    def test_same_selection_with_a_different_manifest_is_a_determinism_error(self):
        self.analyzer._global_candidate_pool(
            self.state,
            self.graph,
            self.item,
        )
        event = self.shadow_events()[0]
        event["manifest_identity"] = "b" * 64

        with self.assertRaisesRegex(
            ValueError,
            "determinism|manifest",
        ):
            self.analyzer._global_candidate_pool(
                self.state,
                self.graph,
                self.item,
            )

    def test_action_checkpoint_rejects_tampered_shadow_manifest(self):
        self.analyzer._global_candidate_pool(
            self.state,
            self.graph,
            self.item,
        )
        event = self.shadow_events()[0]
        event["manifest"] = copy.deepcopy(event["manifest"])
        event["manifest"]["clusters"][0]["member_refs"].append(
            "record:context"
        )

        with self.assertRaisesRegex(
            ValueError,
            "identity|membership|member",
        ):
            self.state.action_checkpoint_payload()

    def test_report_round_trip_preserves_event_and_rejects_tampering(self):
        report = AgenticRecursiveAnalyzer(
            judge=FusionScriptedJudge(
                global_outcome="no_defect"
            ),
            fusion_mode="retrieval-global",
        ).analyze(
            self.graph,
            start_refs=["record:observed_defect"],
            objective="Diagnose the omitted namespace method.",
            analysis_perspective="task quality",
        )
        payload = report.to_dict()

        restored = RecursiveAttributionReport.from_dict(
            copy.deepcopy(payload)
        )

        self.assertEqual(restored.to_dict(), payload)
        event = next(
            value
            for value in payload["investigation_journal"]
            if value.get("kind")
            == "candidate_cluster_manifest_shadow"
        )
        event["manifest"]["candidate_facts"][0][
            "component"
        ] = "tampered"
        with self.assertRaisesRegex(ValueError, "identity"):
            RecursiveAttributionReport.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
