from __future__ import annotations

import copy
import hashlib
import json
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
from trace_attribution.models import stable_json


def sample_graph(
    *,
    decision_event_type: str = "decision",
    decision_revision: int = 0,
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
    def _synthetic_prompt_capsule(self, graph: TraceGraph | None = None):
        graph = graph or sample_graph()
        candidate = CausalCandidate(
            ref="record:prompt",
            node=graph.nodes["record:prompt"],
            source="semantic_fallback",
            edge={
                "from_ref": "record:prompt",
                "to_ref": "record:observed_defect",
                "relation": "semantic_predecessor_match",
                "evidence_type": "semantic_inferred",
                "evidence_refs": ["record:prompt"],
                "eligible_for_attribution": False,
                "retrieval_candidate": True,
                "inference_method": "token_overlap_retrieval",
                "edge_origin": "offline.semantic_retrieval",
            },
            score=0.6,
            evidence_refs=("record:prompt",),
        )
        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate],
            defect_state=DefectState.create(
                label="sigint_cleanup_interrupted",
                expected="cleanup completes",
                actual="cleanup interrupted",
                mechanism="cancellation mismatch",
                scope="task_quality",
            ),
            downstream_paths={
                "record:prompt": (
                    "record:prompt",
                    "record:decision",
                    "record:observed_defect",
                )
            },
            start_refs=("record:observed_defect",),
        )[0]
        return graph, candidate, capsule

    def _decision_candidate(self, graph: TraceGraph) -> CausalCandidate:
        return CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=graph.edge_context(
                "record:decision", "record:observed_defect"
            )[0],
            score=0.9,
            evidence_refs=("record:decision",),
        )

    def _decision_capsule(
        self, graph: TraceGraph | None = None
    ) -> CandidateEvidenceCapsule:
        graph = graph or sample_graph()
        return build_candidate_evidence_capsules(
            graph=graph,
            candidates=[self._decision_candidate(graph)],
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

    def test_capsule_v7_round_trip_rejects_stale_v6_identity(self):
        payload = self._decision_capsule().to_dict()

        self.assertEqual(payload["schema_version"], "candidate-evidence-capsule/v7")
        self.assertEqual(CandidateEvidenceCapsule.from_dict(payload).to_dict(), payload)

        payload["schema_version"] = "candidate-evidence-capsule/v6"
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            CandidateEvidenceCapsule.from_dict(payload)

    def test_synthetic_route_requires_exact_navigation_only_fact(self):
        _, _, capsule = self._synthetic_prompt_capsule()
        payload = capsule.to_dict()

        for malformed in (False, "true", 1, None):
            with self.subTest(value=malformed):
                mutated = copy.deepcopy(payload)
                mutated["candidate"]["retrieval_is_not_causal_verdict"] = malformed
                with self.assertRaisesRegex(
                    ValueError, "retrieval_is_not_causal_verdict"
                ):
                    CandidateEvidenceCapsule.from_dict(mutated)

    def test_synthetic_route_cannot_self_authorize_unrelated_active_evidence(self):
        graph, authoritative, capsule = self._synthetic_prompt_capsule()
        payload = capsule.to_dict()
        unrelated_ref = "record:tool_result"
        forged_edge = copy.deepcopy(payload["candidate"]["retrieval_edge"])
        forged_edge["evidence_refs"] = [
            "record:prompt",
            unrelated_ref,
        ]
        forged_candidate = CausalCandidate(
            ref="record:prompt",
            node=graph.nodes["record:prompt"],
            source="sibling_context",
            edge=forged_edge,
            score=0.6,
            evidence_refs=("record:prompt", unrelated_ref),
        )
        collections = evidence_capsule._build_prompt_collections(
            graph=graph,
            candidate=forged_candidate,
            path=tuple(payload["downstream_path"]),
        )
        payload["candidate"]["source"] = forged_candidate.source
        payload["candidate"]["retrieval_edge"] = collections["retrieval_edge"]
        payload["action_group"] = collections["action_group"]
        payload["evidence_references"] = collections["evidence_references"]
        payload["artifact_hydration"] = collections["artifact_hydration"]
        payload["missing_evidence_refs"] = collections["missing_evidence_refs"]
        payload["validation_source"] = evidence_capsule._validation_source(
            graph=graph,
            candidate=forged_candidate,
            path=tuple(payload["downstream_path"]),
            start_refs=tuple(payload["start_refs"]),
            prompt_collections=collections,
        )
        restored = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "authoritative retrieval route"):
            evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                graph,
                restored,
                authoritative_candidates=(authoritative,),
            )

    def test_valid_synthetic_route_round_trips_with_authoritative_source(self):
        graph, authoritative, capsule = self._synthetic_prompt_capsule()
        restored = CandidateEvidenceCapsule.from_dict(capsule.to_dict())

        evidence_capsule.validate_candidate_evidence_capsule_against_graph(
            graph,
            restored,
            authoritative_candidates=(authoritative,),
        )
        self.assertEqual(restored.to_dict(), capsule.to_dict())

    def test_recorded_route_membership_is_rebuilt_from_active_authoritative_sources(self):
        graph = sample_graph()
        edge = graph.edge_context(
            "record:decision", "record:observed_defect"
        )[1]
        authoritative = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=edge,
            score=0.9,
            evidence_refs=("record:decision", "record:prompt"),
        )
        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(authoritative,),
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
        original = capsule.to_dict()
        expected_refs = tuple(
            original["validation_source"]["candidate_evidence_refs"]
        )

        def forged_capsule(*, source=None, evidence_refs=None, mutate_edge=None):
            forged = copy.deepcopy(original)
            candidate_source = source or forged["candidate"]["source"]
            candidate_edge = copy.deepcopy(
                forged["validation_source"]["candidate_edge"]
            )
            if mutate_edge is not None:
                mutate_edge(candidate_edge)
            refs = tuple(
                evidence_refs
                if evidence_refs is not None
                else forged["validation_source"]["candidate_evidence_refs"]
            )
            forged_candidate = CausalCandidate(
                ref="record:decision",
                node=graph.nodes["record:decision"],
                source=candidate_source,
                edge=candidate_edge,
                score=0.9,
                evidence_refs=refs,
            )
            collections = evidence_capsule._build_prompt_collections(
                graph=graph,
                candidate=forged_candidate,
                path=tuple(forged["downstream_path"]),
            )
            forged["candidate"]["source"] = candidate_source
            forged["candidate"]["retrieval_edge"] = collections[
                "retrieval_edge"
            ]
            forged["action_group"] = collections["action_group"]
            forged["evidence_references"] = collections[
                "evidence_references"
            ]
            forged["artifact_hydration"] = collections[
                "artifact_hydration"
            ]
            forged["missing_evidence_refs"] = collections[
                "missing_evidence_refs"
            ]
            forged["validation_source"] = evidence_capsule._validation_source(
                graph=graph,
                candidate=forged_candidate,
                path=tuple(forged["downstream_path"]),
                start_refs=tuple(forged["start_refs"]),
                prompt_collections=collections,
            )
            return CandidateEvidenceCapsule.from_dict(forged)

        duplicated = copy.deepcopy(original)
        duplicated["validation_source"]["candidate_evidence_refs"].append(
            expected_refs[0]
        )
        mutations = {
            "unrelated active ref": forged_capsule(
                evidence_refs=(*expected_refs, "record:tool_result")
            ),
            "removed ref": forged_capsule(
                evidence_refs=expected_refs[:-1]
            ),
            "reordered membership": forged_capsule(
                evidence_refs=tuple(reversed(expected_refs))
            ),
            "duplicated membership": CandidateEvidenceCapsule.from_dict(
                duplicated
            ),
            "source-family change": forged_capsule(
                source="attribution_edge"
            ),
            "source-edge metadata change": forged_capsule(
                mutate_edge=lambda value: value.update(
                    {"metadata": {"revision": "forged"}}
                )
            ),
        }
        for label, restored in mutations.items():
            with self.subTest(case=label):
                with self.assertRaisesRegex(
                    ValueError, "authoritative recorded route"
                ):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        graph,
                        restored,
                        authoritative_candidates=(authoritative,),
                    )

        restored = CandidateEvidenceCapsule.from_dict(original)
        evidence_capsule.validate_candidate_evidence_capsule_against_graph(
            graph,
            restored,
            authoritative_candidates=(authoritative,),
        )
        self.assertEqual(restored.to_dict(), original)

    def test_action_group_excludes_ineligible_member_with_shared_call_identity(self):
        trace = copy.deepcopy(sample_graph().raw_trace)
        trace["manifest"] = {"subject_revision": "git:active"}
        next(
            item for item in trace["records"] if item["record_id"] == "decision"
        )["data"]["subject_revision"] = "git:active"
        trace["records"].append(
            {
                "record_id": "audit_only_external",
                "component": "evaluation",
                "event_type": "external.evaluation_fact",
                "data": {
                    "call_id": "call_1",
                    "status": "failed",
                    "subject_revision": "git:stale",
                    "revision_status": "mismatched",
                    "observation": "AUDIT_ONLY_SHARED_CALL_MEMBER",
                },
            }
        )
        trace["records"].append(
            {
                "record_id": "stale_shared_member",
                "component": "tool",
                "event_type": "tool.result",
                "data": {
                    "call_id": "call_1",
                    "subject_revision": "git:stale",
                    "output": "STALE_SHARED_CALL_MEMBER",
                },
            }
        )
        graph = TraceGraph.from_trace(trace)

        capsule = self._decision_capsule(graph).to_dict()

        self.assertFalse(graph.evidence_eligible("record:audit_only_external"))
        self.assertTrue(graph.evidence_eligible("record:stale_shared_member"))
        self.assertEqual(
            [
                "record:decision",
                "record:tool_call",
                "record:tool_result",
            ],
            [
                member["ref"]
                for member in capsule["action_group"]["members"]
            ],
        )
        self.assertNotIn("record:audit_only_external", str(capsule))
        self.assertNotIn("AUDIT_ONLY_SHARED_CALL_MEMBER", str(capsule))
        self.assertNotIn("record:stale_shared_member", str(capsule))
        self.assertNotIn("STALE_SHARED_CALL_MEMBER", str(capsule))

    def test_candidate_revision_policy_applies_to_recorded_and_synthetic_routes(self):
        defect = DefectState.create(
            label="sigint_cleanup_interrupted",
            expected="cleanup completes",
            actual="cleanup interrupted",
            mechanism="cancellation mismatch",
            scope="task_quality",
        )

        def graph_for(revision):
            trace = copy.deepcopy(sample_graph().raw_trace)
            trace["manifest"] = {"subject_revision": "git:active"}
            decision = next(
                item
                for item in trace["records"]
                if item["record_id"] == "decision"
            )
            if revision is None:
                decision["data"].pop("subject_revision", None)
            else:
                decision["data"]["subject_revision"] = revision
            return TraceGraph.from_trace(trace)

        def capsules_for(graph, *, source, ref="record:decision"):
            edge = (
                graph.edge_context("record:decision", "record:observed_defect")[
                    -1
                ]
                if source == "confirmed_edge"
                else {
                    "from_ref": ref,
                    "to_ref": "record:observed_defect",
                    "relation": "semantic_predecessor_match",
                    "evidence_type": "semantic_inferred",
                    "evidence_refs": [ref],
                    "eligible_for_attribution": False,
                    "retrieval_candidate": True,
                    "inference_method": "token_overlap_retrieval",
                    "edge_origin": "offline.semantic_retrieval",
                }
            )
            return build_candidate_evidence_capsules(
                graph=graph,
                candidates=(
                    CausalCandidate(
                        ref=ref,
                        node=graph.nodes["record:decision"],
                        source=source,
                        edge=edge,
                        score=0.9,
                        evidence_refs=(ref,),
                    ),
                ),
                defect_state=defect,
                downstream_paths={
                    "record:decision": (
                        "record:decision",
                        "record:observed_defect",
                    )
                },
                start_refs=("record:observed_defect",),
            )

        stale_graph = graph_for("git:stale")
        for source in ("confirmed_edge", "semantic_fallback"):
            with self.subTest(route=source):
                self.assertEqual(capsules_for(stale_graph, source=source), ())

        active_graph = graph_for("git:active")
        self.assertEqual(
            [
                capsule.candidate_ref
                for capsule in capsules_for(
                    active_graph,
                    source="confirmed_edge",
                    ref="decision:dec_1",
                )
            ],
            ["record:decision"],
        )
        self.assertEqual(
            len(capsules_for(graph_for(None), source="confirmed_edge")),
            1,
        )
        explicit_missing_graph = graph_for(None)
        explicit_missing_graph.nodes["record:decision"].data[
            "revision_status"
        ] = "missing"
        self.assertEqual(
            capsules_for(explicit_missing_graph, source="confirmed_edge"),
            (),
        )

    def test_capsule_preserves_zero_and_filters_stale_generation_action_and_evidence_refs(self):
        trace = copy.deepcopy(sample_graph(decision_revision=0).raw_trace)
        trace["manifest"] = {
            "case_id": trace["case_id"],
            "run_id": "capsule-generation-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": trace["case_id"],
                "run_id": "capsule-generation-run",
            },
        }
        decision = next(
            item for item in trace["records"] if item["record_id"] == "decision"
        )
        decision["data"].update(
            {
                "subject_revision": "git:active",
                "revision_provenance_status": "valid",
            }
        )
        trace["records"].extend(
            [
                {
                    "record_id": "stale_shared_member",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "call_id": "call_1",
                        "repository_revision": 1,
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                        "output": "STALE_GENERATION_ACTION",
                    },
                },
                {
                    "record_id": "active_shared_member",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "call_id": "call_1",
                        "repository_revision": 0,
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                        "output": "ACTIVE_GENERATION_ACTION",
                    },
                },
                {
                    "record_id": "stale_evidence",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "repository_revision": 1,
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                        "rationale": "STALE_GENERATION_EVIDENCE",
                    },
                },
                {
                    "record_id": "active_evidence",
                    "component": "processor",
                    "event_type": "decision",
                    "data": {
                        "repository_revision": 0,
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                        "rationale": "ACTIVE_GENERATION_EVIDENCE",
                    },
                },
                {
                    "record_id": "current_claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "temporal_scope": "current_revision",
                        "repository_revision": 0,
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    },
                },
            ]
        )
        graph = TraceGraph.from_trace(trace)
        candidate = self._decision_candidate(graph)
        candidate = CausalCandidate(
            ref=candidate.ref,
            node=candidate.node,
            source=candidate.source,
            edge={
                **candidate.edge,
                "evidence_refs": [
                    "record:stale_evidence",
                    "record:active_evidence",
                ],
            },
            score=candidate.score,
            evidence_refs=(
                "record:stale_evidence",
                "record:active_evidence",
            ),
        )

        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=[candidate],
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
        )[0].to_dict()

        self.assertEqual(
            capsule["candidate"]["active_graph_facts"]["repository_revision"],
            "0",
        )
        self.assertIn("record:active_shared_member", str(capsule["action_group"]))
        self.assertNotIn("record:stale_shared_member", str(capsule["action_group"]))
        evidence_refs = {
            item["raw_ref"]
            for item in capsule["evidence_references"]
        }
        self.assertIn("record:active_evidence", evidence_refs)
        self.assertNotIn("record:stale_evidence", evidence_refs)
        self.assertNotIn(
            "record:stale_evidence",
            capsule["candidate"]["retrieval_edge"]["evidence_refs"],
        )
        self.assertNotIn("STALE_GENERATION_EVIDENCE", str(capsule))

    def test_synthetic_route_cannot_launder_itself_as_empty_recorded_source(self):
        graph, authoritative, capsule = self._synthetic_prompt_capsule()
        payload = capsule.to_dict()
        forged_candidate = CausalCandidate(
            ref="record:prompt",
            node=graph.nodes["record:prompt"],
            source="confirmed_edge",
            edge={},
            score=0.6,
            evidence_refs=("record:prompt",),
        )
        collections = evidence_capsule._build_prompt_collections(
            graph=graph,
            candidate=forged_candidate,
            path=tuple(payload["downstream_path"]),
        )
        payload["candidate"]["source"] = forged_candidate.source
        payload["candidate"]["retrieval_edge"] = collections["retrieval_edge"]
        payload["action_group"] = collections["action_group"]
        payload["evidence_references"] = collections["evidence_references"]
        payload["artifact_hydration"] = collections["artifact_hydration"]
        payload["missing_evidence_refs"] = collections["missing_evidence_refs"]
        payload["validation_source"] = evidence_capsule._validation_source(
            graph=graph,
            candidate=forged_candidate,
            path=tuple(payload["downstream_path"]),
            start_refs=tuple(payload["start_refs"]),
            prompt_collections=collections,
        )
        restored = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "authoritative retrieval route"):
            evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                graph,
                restored,
                authoritative_candidates=(authoritative,),
            )

    def test_current_capsule_round_trip_keeps_validation_source_bounded(self):
        graph = sample_graph()
        payload = self._decision_capsule(graph).to_dict()

        self.assertEqual(payload["schema_version"], "candidate-evidence-capsule/v7")
        self.assertEqual(
            set(payload["validation_source"]),
            {
                "candidate_ref",
                "candidate_source",
                "candidate_edge",
                "candidate_evidence_refs",
                "downstream_path",
                "start_refs",
                "prompt_collections_sha256",
            },
        )
        self.assertLessEqual(len(str(payload["validation_source"])), 12000)

        restored = CandidateEvidenceCapsule.from_dict(payload)
        evidence_capsule.validate_candidate_evidence_capsule_against_graph(
            graph,
            restored,
            authoritative_candidates=(self._decision_candidate(graph),),
        )
        self.assertEqual(restored.to_dict(), payload)

    def test_capsule_rejects_oversized_validation_source(self):
        payload = self._decision_capsule().to_dict()
        payload["validation_source"]["candidate_evidence_refs"] = [
            "record:oversized-{0}-{1}".format(index, "x" * 256)
            for index in range(80)
        ]

        with self.assertRaisesRegex(ValueError, "validation source.*bounded"):
            CandidateEvidenceCapsule.from_dict(payload)

    def test_active_validation_rejects_paired_unresolved_synthetic_route_substitution(self):
        graph = sample_graph()
        payload = self._decision_capsule(graph).to_dict()
        forged_edge = {
            "from_ref": "record:forged-route-source",
            "to_ref": "record:observed_defect",
            "relation": "semantic_predecessor_match",
            "evidence_type": "semantic_inferred",
            "eligible_for_attribution": False,
            "retrieval_candidate": True,
        }
        payload["candidate"]["source"] = "semantic_fallback"
        payload["candidate"]["retrieval_edge"] = copy.deepcopy(forged_edge)
        payload["validation_source"]["candidate_source"] = "semantic_fallback"
        payload["validation_source"]["candidate_edge"] = copy.deepcopy(forged_edge)
        collections = {
            "retrieval_edge": payload["candidate"]["retrieval_edge"],
            "action_group": payload["action_group"],
            "evidence_references": payload["evidence_references"],
            "artifact_hydration": payload["artifact_hydration"],
            "missing_evidence_refs": payload["missing_evidence_refs"],
        }
        payload["validation_source"]["prompt_collections_sha256"] = hashlib.sha256(
            stable_json(collections).encode("utf-8")
        ).hexdigest()
        restored = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "source edge starts"):
            evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                graph, restored
            )

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
            ("revision", sample_graph(decision_revision=1)),
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

    def test_every_persisted_edge_collection_must_match_an_active_normalized_edge(self):
        graph = sample_graph()
        original = self._decision_capsule(graph).to_dict()

        for collection in ("causal_path_edges", "incoming_edges", "outgoing_edges"):
            with self.subTest(collection=collection):
                payload = copy.deepcopy(original)
                payload[collection][0]["relation"] = "forged_persisted_relation"
                restored = CandidateEvidenceCapsule.from_dict(payload)
                with self.assertRaisesRegex(ValueError, "edge.*active graph"):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        graph,
                        restored,
                        authoritative_candidates=(self._decision_candidate(graph),),
                    )

    def test_every_prompt_bearing_collection_must_match_active_construction(self):
        graph = sample_graph()
        original = self._decision_capsule(graph).to_dict()

        mutations = {}
        retrieval_edge = copy.deepcopy(original)
        retrieval_edge["candidate"]["retrieval_edge"]["relation"] = (
            "forged_retrieval_relation"
        )
        mutations["retrieval edge"] = retrieval_edge

        action_group = copy.deepcopy(original)
        action_group["action_group"]["members"][0] = copy.deepcopy(
            original["action_group"]["members"][1]
        )
        mutations["substituted action-group member"] = action_group

        evidence_reference = copy.deepcopy(original)
        evidence_reference["evidence_references"][0] = {
            "raw_ref": "record:prompt",
            "resolved_ref": "record:prompt",
            "canonical_ref": "record:prompt",
            "resolution_status": "resolved",
            "provenance_class": "recorded_or_reconstructed_trace_fact",
            "node": graph.hydrate_node("record:prompt").compact(max_chars=1800),
        }
        mutations["forged resolved evidence reference"] = evidence_reference

        artifact_hydration = copy.deepcopy(original)
        artifact_hydration["artifact_hydration"]["missing_artifact_ids"] = []
        mutations["stale artifact hydration"] = artifact_hydration

        removed_missing = copy.deepcopy(original)
        removed_missing["missing_evidence_refs"] = []
        mutations["removed missing-evidence ref"] = removed_missing

        extra_missing = copy.deepcopy(original)
        extra_missing["missing_evidence_refs"].append("record:prompt")
        mutations["extra missing-evidence ref"] = extra_missing

        for label, payload in mutations.items():
            with self.subTest(case=label):
                collections = {
                    "retrieval_edge": payload["candidate"]["retrieval_edge"],
                    "action_group": payload["action_group"],
                    "evidence_references": payload["evidence_references"],
                    "artifact_hydration": payload["artifact_hydration"],
                    "missing_evidence_refs": payload["missing_evidence_refs"],
                }
                payload["validation_source"]["prompt_collections_sha256"] = (
                    hashlib.sha256(
                        stable_json(collections).encode("utf-8")
                    ).hexdigest()
                )
                restored = CandidateEvidenceCapsule.from_dict(payload)
                with self.assertRaisesRegex(ValueError, "prompt-bearing.*active"):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        graph,
                        restored,
                        authoritative_candidates=(self._decision_candidate(graph),),
                    )

    def test_restored_capsule_rejects_removed_changed_temporal_and_revision_drifted_edges(self):
        base_trace = copy.deepcopy(sample_graph().raw_trace)
        for edge in base_trace["dataflow_edges"]:
            edge["edge_id"] = "edge:{0}".format(edge["relation"])
            edge["metadata"] = {"revision": 1}
        base_graph = TraceGraph.from_trace(base_trace)
        authoritative = self._decision_candidate(base_graph)
        capsule = self._decision_capsule(base_graph)

        def mutate_removed(trace):
            trace["dataflow_edges"] = trace["dataflow_edges"][:1]

        def mutate_changed(trace):
            trace["dataflow_edges"][1]["evidence_type"] = "content_matched"
            trace["dataflow_edges"][1]["evidence_refs"] = ["record:prompt"]
            trace["dataflow_edges"][1]["edge_origin"] = "changed.origin"
            trace["dataflow_edges"][1]["inference_method"] = "changed_method"

        def mutate_temporal(trace):
            trace["dataflow_edges"][1]["relation"] = "temporal_adjacency"

        def mutate_revision(trace):
            trace["dataflow_edges"][1]["metadata"]["revision"] = 2

        for label, mutate in (
            ("removed", mutate_removed),
            ("changed", mutate_changed),
            ("temporalized", mutate_temporal),
            ("revision", mutate_revision),
        ):
            with self.subTest(case=label):
                active_trace = copy.deepcopy(base_trace)
                mutate(active_trace)
                with self.assertRaisesRegex(
                    ValueError, "edge.*active graph|authoritative recorded route"
                ):
                    evidence_capsule.validate_candidate_evidence_capsule_against_graph(
                        TraceGraph.from_trace(active_trace),
                        capsule,
                        authoritative_candidates=(authoritative,),
                    )

        restored = CandidateEvidenceCapsule.from_dict(capsule.to_dict())
        evidence_capsule.validate_candidate_evidence_capsule_against_graph(
            TraceGraph.from_trace(base_trace),
            restored,
            authoritative_candidates=(authoritative,),
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
            source="semantic_fallback",
            edge={
                "from_ref": "record:decision",
                "to_ref": "record:decision",
                "relation": "recorded_support",
                "evidence_refs": list(all_refs),
                "eligible_for_attribution": False,
                "retrieval_candidate": True,
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

        expected_refs = {passed_ref, "record:decision"}
        self.assertEqual(
            {item["raw_ref"] for item in capsule["evidence_references"]},
            expected_refs,
        )
        self.assertEqual(
            set(capsule["candidate"]["retrieval_edge"]["evidence_refs"]),
            expected_refs,
        )
        self.assertEqual(
            set(capsule["missing_evidence_refs"]),
            {
                "record:forged_external",
                "artifact:raw-proof",
                "raw:unresolved-proof",
            },
        )
        judge_payload = CandidateEvidenceCapsule.from_dict(capsule).judge_dict()
        self.assertNotIn("record:forged_external", json.dumps(judge_payload))
        self.assertNotIn("artifact:raw-proof", json.dumps(judge_payload))
        self.assertNotIn("raw:unresolved-proof", json.dumps(judge_payload))
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

        self.assertEqual(
            capsule["artifact_hydration"]["hydrated_artifacts"],
            [],
        )
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
