from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution import evidence_capsule
from trace_attribution.evidence_capsule import (
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
    candidate_compression_metrics,
)
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph


def sample_graph(
    *,
    decision_event_type: str = "decision",
    decision_revision: str = "git:abc123",
) -> TraceGraph:
    return TraceGraph.from_trace(
        {
            "case_id": "evidence-capsule-case",
            "artifacts": [
                {
                    "artifact_id": "decision-rationale",
                    "kind": "text",
                    "path": "artifacts/missing-rationale.txt",
                }
            ],
            "records": [
                {
                    "record_id": "prompt",
                    "component": "prompt",
                    "event_type": "message.input",
                    "data": {"text": "Preserve cleanup after process SIGINT."},
                },
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": decision_event_type,
                    "source_refs": ["record:prompt"],
                    "artifact_refs": ["artifact:decision-rationale"],
                    "data": {
                        "decision_id": "dec_1",
                        "repository_revision": decision_revision,
                        "rationale": "Direct parent cancellation is sufficient to model process SIGINT.",
                        "metadata": {"callID": "call_1"},
                    },
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "data": {"call_id": "call_1", "tool_name": "write"},
                },
                {
                    "record_id": "tool_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "call_id": "call_1",
                        "status": "success",
                        "output": {"preview": "Wrote cancellation test."},
                    },
                },
                {
                    "record_id": "observed_defect",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:decision"],
                    "data": {
                        "failure_type": "sigint_cleanup_interrupted",
                        "expected": "Started cleanup completes after SIGINT.",
                        "actual": "Started cleanup is interrupted.",
                    },
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "prompt"},
                    "to": {"type": "record", "id": "decision"},
                    "relation": "prompt_informed_decision",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": "decision"},
                    "to": {"type": "record", "id": "observed_defect"},
                    "relation": "decision_exposed_by_evaluation",
                    "evidence_type": "confirmed",
                    "confidence": 1.0,
                    "eligible_for_attribution": True,
                },
            ],
        }
    )


class CandidateEvidenceCapsuleTest(unittest.TestCase):
    def _decision_capsule(self) -> CandidateEvidenceCapsule:
        graph = sample_graph()
        return build_candidate_evidence_capsules(
            graph=graph,
            candidates=[
                CausalCandidate(
                    ref="record:decision",
                    node=graph.nodes["record:decision"],
                    source="confirmed_edge",
                    score=0.9,
                    evidence_refs=("record:decision",),
                )
            ],
            defect_state=DefectState.create(
                label="sigint_cleanup_interrupted",
                expected="cleanup completes",
                actual="cleanup interrupted",
                mechanism="cancellation mismatch",
                scope="task_quality",
            ),
            downstream_paths={
                "record:decision": (
                    "record:decision",
                    "record:observed_defect",
                )
            },
            start_refs=("record:observed_defect",),
        )[0]

    def test_capsule_restore_rejects_cross_candidate_identity_substitution(self):
        original = self._decision_capsule().to_dict()

        mutations = {}
        candidate_ref = copy.deepcopy(original)
        candidate_ref["candidate_ref"] = "record:prompt"
        mutations["capsule ref"] = candidate_ref
        candidate_fact = copy.deepcopy(original)
        candidate_fact["candidate"]["ref"] = "record:prompt"
        mutations["candidate fact"] = candidate_fact
        embedded_node = copy.deepcopy(original)
        embedded_node["candidate"]["node"]["ref"] = "record:prompt"
        mutations["embedded node"] = embedded_node
        path_start = copy.deepcopy(original)
        path_start["downstream_path"][0] = "record:prompt"
        mutations["path start"] = path_start
        resolved_start = copy.deepcopy(original)
        resolved_start["downstream_path_references"][0]["resolved_ref"] = (
            "record:prompt"
        )
        mutations["resolved path start"] = resolved_start

        for label, payload in mutations.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, "candidate identity"):
                    CandidateEvidenceCapsule.from_dict(payload)

    def test_capsule_requires_boolean_candidate_eligibility_on_restore(self):
        original = self._decision_capsule().to_dict()

        for malformed in ("true", "false", 1, 0, None):
            with self.subTest(value=malformed):
                payload = copy.deepcopy(original)
                payload["candidate"]["root_candidate_eligible"] = malformed
                with self.assertRaisesRegex(ValueError, "root_candidate_eligible"):
                    CandidateEvidenceCapsule.from_dict(payload)

    def test_capsule_restore_validates_every_downstream_path_reference(self):
        original = self._decision_capsule().to_dict()

        for field in ("raw_ref", "resolved_ref", "canonical_ref"):
            with self.subTest(field=field):
                payload = copy.deepcopy(original)
                payload["downstream_path_references"][1][field] = "record:prompt"
                with self.assertRaisesRegex(ValueError, "downstream path reference"):
                    CandidateEvidenceCapsule.from_dict(payload)

    def test_restored_capsule_must_match_active_graph_facts_and_revision(self):
        capsule = self._decision_capsule()

        for label, active_graph in (
            ("event type", sample_graph(decision_event_type="tool.result")),
            ("revision", sample_graph(decision_revision="git:stale")),
        ):
            with self.subTest(case=label):
                with self.assertRaisesRegex(ValueError, "active graph"):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        active_graph, capsule
                    )

        for field in ("root_candidate_eligible", "evidence_eligible"):
            with self.subTest(fabricated=field):
                payload = capsule.to_dict()
                payload["candidate"][field] = False
                payload["candidate"]["active_graph_facts"][field] = False
                fabricated = CandidateEvidenceCapsule.from_dict(payload)
                with self.assertRaisesRegex(ValueError, "active graph"):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        sample_graph(), fabricated
                    )

    def test_duplicate_routes_preserve_recorded_provenance_independent_of_score(self):
        graph = sample_graph()
        node = graph.nodes["record:decision"]
        recorded_edge = next(
            edge
            for edge in graph.semantic_predecessor_edges(
                "record:observed_defect"
            )
            if edge.get("ref") == node.ref
        )

        def build(recorded_score, inferred_score, reverse):
            candidates = [
                CausalCandidate(
                    ref=node.ref,
                    node=node,
                    source="confirmed_edge",
                    edge=recorded_edge,
                    score=recorded_score,
                    evidence_refs=(node.ref,),
                ),
                CausalCandidate(
                    ref=node.ref,
                    node=node,
                    source="semantic_fallback",
                    edge={
                        "from_ref": node.ref,
                        "to_ref": "record:observed_defect",
                        "relation": "semantic_predecessor_match",
                        "evidence_type": "semantic_inferred",
                        "confidence": inferred_score,
                        "eligible_for_attribution": False,
                        "retrieval_candidate": True,
                    },
                    score=inferred_score,
                    evidence_refs=(node.ref,),
                ),
            ]
            if reverse:
                candidates.reverse()
            return build_candidate_evidence_capsules(
                graph=graph,
                candidates=candidates,
                defect_state=DefectState.create(
                    label="sigint_cleanup_interrupted",
                    expected="cleanup completes",
                    actual="cleanup interrupted",
                    mechanism="cancellation mismatch",
                    scope="task_quality",
                ),
                downstream_paths={
                    node.ref: (node.ref, "record:observed_defect")
                },
                start_refs=("record:observed_defect",),
            )[0].to_dict()["candidate"]

        recorded_high = build(0.99, 0.01, False)
        inferred_high = build(0.01, 0.99, True)

        self.assertEqual(recorded_high, inferred_high)
        self.assertEqual(recorded_high["source"], "confirmed_edge")
        self.assertEqual(
            recorded_high["retrieval_edge"]["relation"],
            "decision_exposed_by_evaluation",
        )
        self.assertEqual(recorded_high["retrieval_edge"]["confidence"], 1.0)

    def _external_candidate_capsules(self, *, status: str, subject_revision: str):
        trace = inject_external_evaluation_facts(
            {
                "manifest": {
                    "case_id": "capsule-candidate-case",
                    "run_id": "capsule-candidate-run",
                    "subject_revision": "git:abc123",
                    "subject_revision_provenance": {
                        "method": "case_trace_config",
                        "source": "CaseTraceConfig.subjectRevision",
                        "bound_at": "case_start",
                        "case_id": "capsule-candidate-case",
                        "run_id": "capsule-candidate-run",
                    },
                },
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "Preserve cleanup."},
                    }
                ],
                "dataflow_edges": [],
            },
            [
                {
                    "source": "terminalbench",
                    "scope": "cleanup",
                    "subject_revision": subject_revision,
                    "assertion": "Cleanup completes.",
                    "observation": "Cleanup {0}.".format(status),
                    "status": status,
                    "observed_at": "2026-07-21T12:00:00Z",
                    "evidence_refs": ["record:decision"],
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                }
            ],
        )
        ref = "record:{0}".format(trace["records"][-1]["record_id"])
        graph = TraceGraph.from_trace(trace)
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[
                CausalCandidate(
                    ref=ref,
                    node=graph.nodes[ref],
                    source="global_evidence",
                    score=1.0,
                )
            ],
            defect_state=DefectState.create(
                label="cleanup_failed",
                expected="Cleanup completes.",
                actual="Cleanup stopped.",
                mechanism="The implementation omitted cleanup preservation.",
                scope="cleanup",
            ),
            downstream_paths={},
            start_refs=(ref,),
        )
        return graph, ref, capsules

    def test_capsule_rejects_audit_only_external_candidate(self):
        graph, ref, capsules = self._external_candidate_capsules(
            status="failed", subject_revision="git:stale"
        )

        self.assertFalse(graph.evidence_eligible(ref))
        self.assertEqual(capsules, ())

    def test_capsule_supports_matched_passed_external_candidate(self):
        graph, ref, capsules = self._external_candidate_capsules(
            status="passed", subject_revision="git:abc123"
        )

        self.assertTrue(graph.evidence_eligible(ref))
        self.assertFalse(graph.analysis_start_eligible(ref))
        self.assertEqual([item.candidate_ref for item in capsules], [ref])
        self.assertFalse(capsules[0].to_dict()["candidate"]["root_candidate_eligible"])

    def test_capsule_filters_audit_only_external_refs_but_preserves_other_ref_classes(self):
        trace = {
            "manifest": {
                "case_id": "capsule-external-case",
                "run_id": "capsule-external-run",
                "subject_revision": "git:abc123",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": "capsule-external-case",
                    "run_id": "capsule-external-run",
                },
            },
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {"rationale": "Use the recorded implementation plan."},
                }
            ],
            "dataflow_edges": [],
        }
        trace = inject_external_evaluation_facts(
            trace,
            [
                {
                    "source": "terminalbench",
                    "scope": "cleanup",
                    "subject_revision": "git:abc123",
                    "assertion": "Cleanup completes.",
                    "observation": "Cleanup passed.",
                    "status": "passed",
                    "observed_at": "2026-07-21T12:00:00Z",
                    "evidence_refs": ["record:decision"],
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                }
            ],
        )
        passed_ref = "record:{0}".format(trace["records"][-1]["record_id"])
        trace["records"].append(
            {
                "record_id": "forged_external",
                "component": "evaluation",
                "event_type": "external.evaluation_fact",
                "status": "failed",
                "data": {
                    "status": "failed",
                    "subject_revision": "git:abc123",
                    "trace_revision": "git:abc123",
                    "revision_status": "matched",
                    "revision_provenance_status": "valid",
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                    "eligible_for_decisive_judgment": True,
                    "observation": "FORGED_AUDIT_ONLY_PAYLOAD",
                },
            }
        )
        graph = TraceGraph.from_trace(trace)
        all_refs = (
            "record:forged_external",
            passed_ref,
            "record:decision",
            "artifact:raw-proof",
            "raw:unresolved-proof",
        )
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge={
                "from_ref": "record:decision",
                "to_ref": "record:decision",
                "relation": "recorded_support",
                "evidence_refs": list(all_refs),
            },
            score=1.0,
            evidence_refs=all_refs,
        )

        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate],
            defect_state=DefectState.create(
                label="cleanup_failed",
                expected="Cleanup completes.",
                actual="Cleanup stopped.",
                mechanism="The implementation omitted cleanup preservation.",
                scope="cleanup",
            ),
            downstream_paths={},
            start_refs=("record:decision",),
        )[0].to_dict()

        expected_refs = {
            passed_ref,
            "record:decision",
            "artifact:raw-proof",
            "raw:unresolved-proof",
        }
        self.assertEqual(
            {item["raw_ref"] for item in capsule["evidence_references"]},
            expected_refs,
        )
        self.assertEqual(
            set(capsule["candidate"]["retrieval_edge"]["evidence_refs"]),
            expected_refs,
        )
        self.assertNotIn("record:forged_external", str(capsule))
        self.assertNotIn("FORGED_AUDIT_ONLY_PAYLOAD", str(capsule))

    def test_capsule_closes_candidate_path_action_group_and_missing_artifacts(self):
        graph = sample_graph()
        defect = DefectState.create(
            label="sigint_cleanup_interrupted",
            expected="Started cleanup completes after SIGINT.",
            actual="Started cleanup is interrupted.",
            mechanism="The authored cancellation model may not preserve cleanup.",
            scope="task_quality",
        )
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="progress_window",
            edge={
                "from_ref": "record:decision",
                "to_ref": "record:observed_defect",
                "relation": "progress_window_candidate",
                "evidence_type": "offline_reconstruction",
                "evidence_refs": ["record:decision"],
            },
            score=0.91,
            evidence_refs=("record:decision",),
        )

        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate],
            defect_state=defect,
            downstream_paths={
                "record:decision": (
                    "record:decision",
                    "record:observed_defect",
                )
            },
            start_refs=("record:observed_defect",),
        )

        self.assertEqual(len(capsules), 1)
        capsule = capsules[0].to_dict()
        self.assertEqual(capsule["candidate_ref"], "record:decision")
        self.assertEqual(
            capsule["downstream_path"],
            ["record:decision", "record:observed_defect"],
        )
        self.assertEqual(capsule["action_group"]["identity"], "call_id:call_1")
        self.assertEqual(
            {item["ref"] for item in capsule["action_group"]["members"]},
            {"record:decision", "record:tool_call", "record:tool_result"},
        )
        self.assertEqual(
            capsule["artifact_hydration"]["missing_artifact_ids"],
            ["decision-rationale"],
        )
        self.assertIn("artifact:decision-rationale", capsule["missing_evidence_refs"])
        self.assertTrue(
            any(
                edge["relation"] == "prompt_informed_decision"
                for edge in capsule["incoming_edges"]
            )
        )
        self.assertTrue(
            any(
                edge["relation"] == "decision_exposed_by_evaluation"
                for edge in capsule["outgoing_edges"]
            )
        )

    def test_capsule_reports_embedded_slice_fallback_as_truncated(self):
        content = "redacted decisive rationale"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        trace = {
            "case_id": "slice-fallback-capsule",
            "artifacts": [
                {
                    "artifact_id": "decision-rationale",
                    "kind": "text",
                    "path": "artifacts/sha256/missing.txt",
                    "hash": hashlib.sha256(b"full artifact").hexdigest()[:16],
                    "availability": "bundled",
                    "byte_length": len(content.encode("utf-8")) + 10,
                    "semantic_slices": [
                        {
                            "byte_range": [0, len(content.encode("utf-8"))],
                            "content": content,
                            "hash": digest,
                            "truncated": False,
                        }
                    ],
                }
            ],
            "records": [
                {
                    "record_id": "decision",
                    "component": "processor",
                    "event_type": "decision",
                    "artifact_refs": ["artifact:decision-rationale"],
                    "data": {"decision_id": "dec_1"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(trace, artifact_root=Path(directory))
            candidate = CausalCandidate(
                ref="record:decision",
                node=graph.nodes["record:decision"],
                source="progress_window",
                score=0.9,
            )
            capsule = build_candidate_evidence_capsules(
                graph=graph,
                candidates=[candidate],
                defect_state=DefectState.create(
                    label="missing_cleanup",
                    expected="cleanup completes",
                    actual="cleanup stopped",
                    mechanism="decision omitted the cleanup boundary",
                    scope="task_quality",
                ),
                downstream_paths={},
                start_refs=("record:decision",),
            )[0].to_dict()

        hydrated = capsule["artifact_hydration"]["hydrated_artifacts"][0]
        self.assertEqual(hydrated["source"], "embedded_semantic_slice")
        self.assertEqual(hydrated["content"], content)
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(
            capsule["artifact_hydration"]["truncated_artifact_ids"],
            ["decision-rationale"],
        )
        self.assertNotIn("artifact:decision-rationale", capsule["missing_evidence_refs"])
        self.assertEqual(graph.artifact_hydration["slice_fallbacks"], 1)

    def test_compression_metrics_compare_unique_capsules_with_trace_nodes(self):
        graph = sample_graph()
        defect = DefectState.create(
            label="sigint_cleanup_interrupted",
            expected="cleanup completes",
            actual="cleanup interrupted",
            mechanism="cancellation mismatch",
            scope="task_quality",
        )
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="semantic_fallback",
            score=0.8,
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate, candidate],
            defect_state=defect,
            downstream_paths={},
            start_refs=("record:observed_defect",),
        )

        metrics = candidate_compression_metrics(graph, capsules)

        self.assertEqual(metrics["trace_node_count"], 5)
        self.assertEqual(metrics["candidate_count"], 1)
        self.assertEqual(metrics["candidate_node_reduction_ratio"], 0.8)
        self.assertGreater(metrics["capsule_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
