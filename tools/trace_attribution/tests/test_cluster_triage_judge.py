from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_attribution.cache import JudgmentCache, build_judge_cache_key
from trace_attribution.candidate_budget import candidate_identity
from trace_attribution.candidate_clustering import build_candidate_cluster_manifest
from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    ClaudeCausalJudge,
)
from trace_attribution.causal_state import CausalCandidate, seed_defect_state
from trace_attribution.cluster_triage import (
    build_candidate_cluster_triage_plan,
    build_candidate_cluster_triage_request,
)
from trace_attribution.cluster_triage_judge import (
    CLUSTER_TRIAGE_PAGE_SIZE,
    ClusterTriageJudgment,
    ClusterTriagePageRequest,
    build_cluster_triage_page_requests,
    build_cluster_triage_prompt,
    merge_cluster_triage_judgments,
    parse_cluster_triage_judgment,
)
from trace_attribution.errors import (
    JudgeProviderError,
    TransportCallError,
    TransportCallResult,
)
from trace_attribution.evidence_capsule import (
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json


class ScriptedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.model = "test-model"
        self.max_tokens = 2048
        self.repair_max_tokens = 512
        self.thinking_config = None
        self.request_count = 0
        self.calls = []

    def create_message_text_with_usage(self, *, system, messages, max_tokens):
        self.request_count += 1
        self.calls.append(
            {"system": system, "messages": messages, "max_tokens": max_tokens}
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise TransportCallError(response, physical_requests=1)
        return TransportCallResult(str(response), physical_requests=1)


def _fixture(count: int = 10):
    candidate_records = []
    edges = []
    for index in range(count):
        record_id = "candidate-{0:02d}".format(index)
        data = {
            "file_path": "src/module_{0:02d}.py".format(index),
            "symbol": "Module{0:02d}.execute".format(index),
            "rationale": "Validate semantic branch {0:02d}.".format(index),
            "phase": "planning" if index % 2 == 0 else "implementation",
        }
        if index == 0:
            data.update(
                {
                    "human_label": "gold-root",
                    "benchmark_score": 0.99,
                    "reference_answer": "candidate-00",
                    "reviewer_facts": {"verdict": "known-answer"},
                    "prior_attribution": {"root_verdict": "confirmed"},
                }
            )
        candidate_records.append(
            {
                "record_id": record_id,
                "component": "planner" if index % 2 == 0 else "tool",
                "event_type": "decision",
                "title": "Candidate semantic fact {0:02d}".format(index),
                "status": "completed",
                "timestamp": "2026-07-31T00:00:{0:02d}Z".format(index),
                "data": data,
            }
        )
        edges.append(
            {
                "from": {"type": "record", "id": record_id},
                "to": {"type": "record", "id": "seed"},
                "relation": "decision_exposed_by_evaluation",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            }
        )
    trace = {
        "schema_version": "causal-ir/v1",
        "case_id": "cluster-triage-stage-b",
        "records": candidate_records
        + [
            {
                "record_id": "seed",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "title": "Observed semantic defect",
                "status": "failed",
                "timestamp": "2026-07-31T00:01:00Z",
                "source_refs": [
                    "record:candidate-{0:02d}".format(index)
                    for index in range(count)
                ],
                "data": {
                    "expected": "All requested semantic branches are implemented.",
                    "actual": "One requested semantic branch is missing.",
                    "mechanism": "The implementation omitted a required branch.",
                },
            }
        ],
        "dataflow_edges": edges,
    }
    graph = TraceGraph.from_trace(trace)
    objective = "Identify which implementation branch omitted the requested behavior."
    defect = seed_defect_state(graph.nodes["record:seed"], objective)
    candidates = tuple(
        CausalCandidate(
            ref="record:candidate-{0:02d}".format(index),
            node=graph.nodes["record:candidate-{0:02d}".format(index)],
            source="confirmed_edge",
            score=1.0 - index / 100,
            evidence_refs=("record:candidate-{0:02d}".format(index),),
        )
        for index in range(count)
    )
    paths = {
        candidate.ref: (candidate.ref, "record:seed")
        for candidate in candidates
    }
    manifest = build_candidate_cluster_manifest(
        graph=graph,
        candidates=candidates,
        candidate_paths=paths,
        candidate_audit=[
            {
                "ref": candidate.ref,
                "discovered_rank": index,
                "candidate_identity": candidate_identity(candidate),
                "disposition": "offered",
                "reason": "input_order",
            }
            for index, candidate in enumerate(candidates)
        ],
        source_selection_identity="a" * 64,
        seed_ref="record:seed",
        defect_fingerprint=defect.fingerprint,
    )
    request = build_candidate_cluster_triage_request(
        manifest=manifest,
        eligible_candidates=candidates,
    )
    capsules = build_candidate_evidence_capsules(
        graph=graph,
        candidates=candidates,
        defect_state=defect,
        downstream_paths=paths,
        start_refs=("record:seed",),
    )
    return request, manifest, capsules, defect, objective


def _pages(count: int = 10):
    request, manifest, capsules, defect, objective = _fixture(count)
    pages = build_cluster_triage_page_requests(
        request=request,
        manifest=manifest,
        eligible_capsules=capsules,
        active_defect=defect,
        objective=objective,
        analysis_perspective="task quality",
    )
    return request, manifest, capsules, defect, objective, pages


def _payload(page, dispositions=None):
    dispositions = dispositions or {}
    decisions = []
    for entry in page.clusters:
        cluster_id = entry["cluster_id"]
        disposition = dispositions.get(cluster_id, "selected")
        evidence_ref = entry["evidence_refs"][0]
        decisions.append(
            {
                "cluster_id": cluster_id,
                "disposition": disposition,
                "rationale": "Trace facts match the active defect."
                if disposition != "unselected"
                else "The trace shows a different component and obligation.",
                "evidence_refs": [evidence_ref],
                "mismatch_evidence": (
                    [
                        {
                            "evidence_ref": evidence_ref,
                            "mismatch": "Different component and obligation semantics.",
                        }
                    ]
                    if disposition == "unselected"
                    else []
                ),
            }
        )
    return {
        "schema": "candidate-cluster-triage-page-response/v1",
        "page_identity": page.page_identity,
        "request_identity": page.request_identity,
        "partition_identity": page.partition_identity,
        "page_index": page.page_index,
        "page_count": page.page_count,
        "decisions": decisions,
    }


def _merge(fixture, *, pages, judgments):
    request, manifest, capsules, defect, objective, _canonical_pages = fixture
    return merge_cluster_triage_judgments(
        request=request,
        manifest=manifest,
        eligible_capsules=capsules,
        active_defect=defect,
        objective=objective,
        analysis_perspective="task quality",
        pages=pages,
        judgments=judgments,
    )


def _resign_page(payload):
    unsigned = copy.deepcopy(payload)
    unsigned.pop("page_identity", None)
    payload["page_identity"] = hashlib.sha256(
        stable_json(
            {
                "schema": "candidate-cluster-triage-page-identity/v1",
                "facts": unsigned,
            }
        ).encode("utf-8")
    ).hexdigest()
    return ClusterTriagePageRequest.from_dict(payload)


def _normalized_keys(value):
    keys = set()
    if isinstance(value, str) and value.strip().startswith(("{", "[")):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return keys
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add("".join(character for character in key.casefold() if character.isalnum()))
            keys.update(_normalized_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(_normalized_keys(child))
    return keys


class ClusterTriagePageContractTest(unittest.TestCase):
    def test_pages_are_bounded_exact_and_contain_real_semantic_capsules(self):
        request, _manifest, _capsules, _defect, _objective, pages = _pages()

        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page.clusters) <= CLUSTER_TRIAGE_PAGE_SIZE for page in pages))
        self.assertEqual(
            {entry["cluster_id"] for page in pages for entry in page.clusters},
            set(request.cluster_ids),
        )
        first = pages[0]
        entry = first.clusters[0]
        self.assertTrue(entry["semantic_summary"]["representative_facts"])
        self.assertTrue(entry["representative_capsules"])
        capsule = entry["representative_capsules"][0]
        self.assertIn("candidate", capsule)
        self.assertIn("downstream_path", capsule)
        self.assertIn("Candidate semantic fact", stable_json(capsule))
        serialized = stable_json([page.to_dict() for page in pages])
        for forbidden in (
            "human_label",
            "benchmark_score",
            "reference_answer",
            "reviewer_facts",
            "prior_attribution",
            "root_verdict",
            "known-answer",
        ):
            self.assertNotIn(forbidden, serialized)

        self.assertEqual(
            ClusterTriagePageRequest.from_dict(first.to_dict()), first
        )
        tampered = first.to_dict()
        tampered["objective"] = "tampered"
        with self.assertRaisesRegex(ValueError, "identity"):
            ClusterTriagePageRequest.from_dict(tampered)

    def test_paging_is_deterministic_under_capsule_order_permutations(self):
        request, manifest, capsules, defect, objective = _fixture()
        forward = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=capsules,
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )
        reverse = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=tuple(reversed(capsules)),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )
        self.assertEqual(
            [page.to_dict() for page in forward],
            [page.to_dict() for page in reverse],
        )

    def test_analysis_control_envelope_in_capsule_fails_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["nested"] = {
            "schema": "global-candidate-judgment/v11"
        }
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            build_cluster_triage_page_requests(
                request=request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
            )

    def test_deep_isolation_normalizes_aliases_and_json_without_erasing_facts(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        data = payload["candidate"]["node"]["data"]
        data.update(
            {
                "Human-Labels": ["gold-root"],
                "reference.answers": ["candidate-00"],
                "REVIEWER_LABELS": ["known-answer"],
                "prior/verdicts": {"root": "confirmed"},
                "encoded": json.dumps(
                    {
                        "Human_Labels": ["encoded-gold"],
                        "normal_trace_fact": "branch remained unimplemented",
                    }
                ),
                "normal_trace_fact": "preserve this observation",
            }
        )
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        pages = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(poisoned,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )

        page_dicts = [page.to_dict() for page in pages]
        keys = _normalized_keys(page_dicts)
        self.assertTrue(
            {
                "humanlabels",
                "referenceanswers",
                "reviewerlabels",
                "priorverdicts",
            }.isdisjoint(keys)
        )
        serialized = stable_json(page_dicts)
        self.assertNotIn("encoded-gold", serialized)
        self.assertIn("preserve this observation", serialized)
        self.assertIn("branch remained unimplemented", serialized)

    def test_expected_roots_in_capsule_is_isolated_from_page_facts(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"].update(
            {
                "expected_roots": ["record:gold-root"],
                "normal_trace_fact": "The observed branch remains missing.",
            }
        )
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        pages = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(poisoned,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )

        serialized = stable_json([page.to_dict() for page in pages])
        self.assertNotIn("expected_roots", serialized)
        self.assertNotIn("record:gold-root", serialized)
        self.assertIn("The observed branch remains missing.", serialized)

    def test_ground_truth_concept_is_absent_from_page_prompt_and_cache_context(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["nested_facts"] = {
            "ground_truth": {"root_ref": "record:human-answer"},
            "normal_trace_fact": (
                "The root directory remained on solid ground after the build."
            ),
        }
        poisoned = CandidateEvidenceCapsule.from_dict(payload)
        page = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(poisoned,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )[0]
        transport = ScriptedTransport([json.dumps(_payload(page))])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        with patch(
            "trace_attribution.causal_judge.build_judge_cache_key",
            wraps=build_judge_cache_key,
        ) as cache_key:
            judge.triage_candidate_clusters_bounded(
                request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
                max_physical_requests=1,
            )

        contexts = stable_json(
            {
                "page": page.to_dict(),
                "prompt": json.loads(build_cluster_triage_prompt(page)),
                "cache_messages": cache_key.call_args.kwargs["messages"],
            }
        )
        self.assertNotIn("ground_truth", contexts)
        self.assertNotIn("record:human-answer", contexts)
        self.assertIn(
            "The root directory remained on solid ground after the build.",
            contexts,
        )

    def test_real_recursive_root_confirmation_schema_fails_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["nested"] = {
            "schema": (
                "recursive-root-confirmation/v17+resolution/v2+"
                "evidence-policy/v5"
            )
        }
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            build_cluster_triage_page_requests(
                request=request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
            )

    def test_schema_version_analysis_control_in_capsule_fails_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["nested"] = {
            "schema_version": "global-candidate-judgment/v11"
        }
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            build_cluster_triage_page_requests(
                request=request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
            )

    def test_global_validation_envelope_variants_fail_closed_at_request_boundary(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        poisons = (
            {"schema": "global-candidate-validation-envelope/v11"},
            {
                "Schema-Version": (
                    "GLOBAL_CANDIDATE.VALIDATION_ENVELOPE/V11"
                )
            },
        )
        for poison in poisons:
            with self.subTest(poison=poison):
                payload = capsules[0].to_dict()
                payload["candidate"]["node"]["data"]["nested"] = poison
                poisoned = CandidateEvidenceCapsule.from_dict(payload)

                with self.assertRaisesRegex(ValueError, "analysis-control"):
                    build_cluster_triage_page_requests(
                        request=request,
                        manifest=manifest,
                        eligible_capsules=(poisoned,),
                        active_defect=defect,
                        objective=objective,
                        analysis_perspective="task quality",
                    )

    def test_ordinary_root_validation_text_survives_request_projection(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["business_record"] = {
            "schema": "business-validation-record/v11",
            "schema_version": "customer-confirmation-note/v2",
            "summary": (
                "Root validation confirmed the repository path and ordinary "
                "business requirement."
            ),
        }
        capsule = CandidateEvidenceCapsule.from_dict(payload)

        page = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(capsule,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )[0]

        serialized = stable_json(page.to_dict())
        self.assertIn("business-validation-record/v11", serialized)
        self.assertIn("customer-confirmation-note/v2", serialized)
        self.assertIn("ordinary business requirement", serialized)

    def test_doubly_json_encoded_control_object_fails_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["candidate"]["node"]["data"]["nested"] = json.dumps(
            json.dumps(
                {
                    "provenance_class": "analysis_control",
                    "prior_attribution": {"root_verdict": "confirmed"},
                }
            )
        )
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            build_cluster_triage_page_requests(
                request=request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
            )

    def test_analysis_control_schema_aliases_and_encoded_objects_fail_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        for poison in (
            {"ScHeMa": "GLOBAL_CANDIDATE-JUDGMENT/V11"},
            json.dumps({"SCHEMA": "Root_Confirmation/V10"}),
        ):
            with self.subTest(poison=poison):
                payload = capsules[0].to_dict()
                payload["candidate"]["node"]["data"]["nested"] = poison
                poisoned = CandidateEvidenceCapsule.from_dict(payload)
                with self.assertRaisesRegex(ValueError, "analysis-control"):
                    build_cluster_triage_page_requests(
                        request=request,
                        manifest=manifest,
                        eligible_capsules=(poisoned,),
                        active_defect=defect,
                        objective=objective,
                        analysis_perspective="task quality",
                    )

    def test_isolation_depth_is_bounded_and_fails_closed(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        nested = {"normal_trace_fact": "leaf"}
        for _ in range(80):
            nested = {"nested": nested}
        payload["candidate"]["node"]["data"]["too_deep"] = nested
        poisoned = CandidateEvidenceCapsule.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "depth|complex"):
            build_cluster_triage_page_requests(
                request=request,
                manifest=manifest,
                eligible_capsules=(poisoned,),
                active_defect=defect,
                objective=objective,
                analysis_perspective="task quality",
            )

    def test_missing_artifact_capsule_preserves_unknown_evidence_for_uncertain(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["artifact_hydration"] = {
            "referenced_artifact_ids": ["missing-source"],
            "missing_artifact_ids": ["missing-source"],
            "truncated_artifact_ids": [],
            "integrity_failures": [],
            "ineligible_artifact_evidence": [],
        }
        prompt_collections = {
            "retrieval_edge": payload["candidate"]["retrieval_edge"],
            "action_group": payload["action_group"],
            "evidence_references": payload["evidence_references"],
            "artifact_hydration": payload["artifact_hydration"],
            "missing_evidence_refs": payload["missing_evidence_refs"],
        }
        payload["validation_source"]["prompt_collections_sha256"] = (
            hashlib.sha256(
                stable_json(prompt_collections).encode("utf-8")
            ).hexdigest()
        )
        missing = CandidateEvidenceCapsule.from_dict(payload)

        page = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(missing,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )[0]
        capsule = page.clusters[0]["representative_capsules"][0]
        self.assertNotIn("artifact_hydration", capsule)
        self.assertEqual(
            capsule["artifact_evidence_gaps"][0]["status"], "missing"
        )
        judgment = parse_cluster_triage_judgment(
            _payload(page, {page.cluster_ids[0]: "uncertain"}),
            page=page,
        )
        self.assertEqual(judgment.decisions[0]["disposition"], "uncertain")

    def test_owner_ineligible_artifact_preserves_unknown_evidence_for_uncertain(self):
        request, manifest, capsules, defect, objective = _fixture(1)
        payload = capsules[0].to_dict()
        payload["artifact_hydration"] = {
            "referenced_artifact_ids": ["owner-bound-source"],
            "missing_artifact_ids": ["owner-bound-source"],
            "truncated_artifact_ids": [],
            "integrity_failures": [],
            "ineligible_artifact_evidence": [
                {
                    "artifact_id": "owner-bound-source",
                    "raw_ref": "artifact:owner-bound-source",
                    "owner_ref": payload["candidate_ref"],
                    "status": "owner_ineligible",
                    "reason": "artifact owner does not bind this candidate",
                }
            ],
        }
        prompt_collections = {
            "retrieval_edge": payload["candidate"]["retrieval_edge"],
            "action_group": payload["action_group"],
            "evidence_references": payload["evidence_references"],
            "artifact_hydration": payload["artifact_hydration"],
            "missing_evidence_refs": payload["missing_evidence_refs"],
        }
        payload["validation_source"]["prompt_collections_sha256"] = (
            hashlib.sha256(
                stable_json(prompt_collections).encode("utf-8")
            ).hexdigest()
        )
        owner_ineligible = CandidateEvidenceCapsule.from_dict(payload)

        page = build_cluster_triage_page_requests(
            request=request,
            manifest=manifest,
            eligible_capsules=(owner_ineligible,),
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
        )[0]

        capsule = page.clusters[0]["representative_capsules"][0]
        self.assertNotIn("artifact_hydration", capsule)
        self.assertEqual(
            capsule["artifact_evidence_gaps"][0]["status"],
            "owner_ineligible",
        )
        judgment = parse_cluster_triage_judgment(
            _payload(page, {page.cluster_ids[0]: "uncertain"}),
            page=page,
        )
        self.assertEqual(judgment.decisions[0]["disposition"], "uncertain")

    def test_prompt_contains_only_navigation_contract(self):
        page = _pages(1)[-1][0]
        prompt = json.loads(build_cluster_triage_prompt(page))

        self.assertEqual(prompt["request"], page.to_dict())
        schema = stable_json(prompt["required_json_schema"])
        for forbidden in (
            "root_cause",
            "factor_role",
            "confirmation",
            "publication",
            "counterfactual",
        ):
            self.assertNotIn(forbidden, schema)


class ClusterTriageJudgmentContractTest(unittest.TestCase):
    def test_judgment_round_trip_and_tamper_rejection(self):
        page = _pages(2)[-1][0]
        judgment = parse_cluster_triage_judgment(_payload(page), page=page)

        self.assertEqual(
            ClusterTriageJudgment.from_dict(judgment.to_dict()), judgment
        )
        tampered = judgment.to_dict()
        tampered["decisions"][0]["rationale"] = "tampered"
        with self.assertRaisesRegex(ValueError, "identity"):
            ClusterTriageJudgment.from_dict(tampered)

    def test_parser_requires_exact_cluster_coverage_and_grounded_evidence(self):
        page = _pages(2)[-1][0]
        missing = _payload(page)
        missing["decisions"].pop()
        with self.assertRaisesRegex(ValueError, "exactly once"):
            parse_cluster_triage_judgment(missing, page=page)

        duplicate = _payload(page)
        duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
        with self.assertRaisesRegex(ValueError, "exactly once|duplicate"):
            parse_cluster_triage_judgment(duplicate, page=page)

        foreign = _payload(page)
        foreign["decisions"][0]["evidence_refs"] = ["record:foreign"]
        with self.assertRaisesRegex(ValueError, "evidence"):
            parse_cluster_triage_judgment(foreign, page=page)

    def test_unselected_requires_explicit_trace_grounded_mismatch(self):
        page = _pages(1)[-1][0]
        cluster_id = page.cluster_ids[0]
        payload = _payload(page, {cluster_id: "unselected"})
        payload["decisions"][0]["mismatch_evidence"] = []
        with self.assertRaisesRegex(ValueError, "mismatch"):
            parse_cluster_triage_judgment(payload, page=page)

        payload = _payload(page, {cluster_id: "unselected"})
        payload["decisions"][0]["mismatch_evidence"][0][
            "evidence_ref"
        ] = "record:foreign"
        with self.assertRaisesRegex(ValueError, "evidence"):
            parse_cluster_triage_judgment(payload, page=page)

    def test_root_factor_global_and_analysis_control_output_fails_closed(self):
        page = _pages(1)[-1][0]
        for field_name, value in (
            ("root_causes", [page.cluster_ids[0]]),
            ("factor_role", "root"),
            ("global_verdict", "selected"),
            ("analysis_control", {"publish": True}),
        ):
            with self.subTest(field_name=field_name):
                payload = _payload(page)
                payload[field_name] = value
                with self.assertRaises(ValueError):
                    parse_cluster_triage_judgment(payload, page=page)

    def test_provider_rationale_control_json_fails_closed_but_plain_text_survives(self):
        _request, _manifest, _capsules, _defect, _objective, pages = _pages(1)
        page = pages[0]
        payload = _payload(page)
        payload["decisions"][0]["rationale"] = json.dumps(
            json.dumps(
                {"prior_attribution": {"root_verdict": "confirmed"}}
            )
        )

        with self.assertRaisesRegex(ValueError, "evaluation-only|prior-verdict"):
            parse_cluster_triage_judgment(payload, page=page)

        payload = _payload(page)
        payload["decisions"][0]["rationale"] = (
            "The trace records a different component and obligation."
        )
        judgment = parse_cluster_triage_judgment(payload, page=page)
        self.assertEqual(
            judgment.decisions[0]["rationale"],
            "The trace records a different component and obligation.",
        )

    def test_provider_mismatch_control_json_fails_closed(self):
        _request, _manifest, _capsules, _defect, _objective, pages = _pages(1)
        page = pages[0]
        payload = _payload(
            page,
            {page.clusters[0]["cluster_id"]: "unselected"},
        )
        payload["decisions"][0]["mismatch_evidence"][0]["mismatch"] = json.dumps(
            json.dumps(
                {"schema_version": "global-candidate-judgment/v11"}
            )
        )

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            parse_cluster_triage_judgment(payload, page=page)

    def test_provider_rationale_global_validation_envelope_fails_closed(self):
        page = _pages(1)[-1][0]
        payload = _payload(page)
        payload["decisions"][0]["rationale"] = json.dumps(
            json.dumps(
                {
                    "schema": (
                        "GLOBAL_CANDIDATE.VALIDATION_ENVELOPE/V11"
                    )
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            parse_cluster_triage_judgment(payload, page=page)

    def test_provider_mismatch_global_validation_envelope_fails_closed(self):
        page = _pages(1)[-1][0]
        payload = _payload(
            page,
            {page.cluster_ids[0]: "unselected"},
        )
        payload["decisions"][0]["mismatch_evidence"][0]["mismatch"] = json.dumps(
            json.dumps(
                {
                    "SCHEMA_VERSION": (
                        "Global_Candidate-Validation.Envelope/V11"
                    )
                }
            )
        )

        with self.assertRaisesRegex(ValueError, "analysis-control"):
            parse_cluster_triage_judgment(payload, page=page)

    def test_provider_business_validation_text_survives_unchanged(self):
        page = _pages(1)[-1][0]
        payload = _payload(
            page,
            {page.cluster_ids[0]: "unselected"},
        )
        payload["decisions"][0]["rationale"] = (
            "Root validation confirmed an ordinary repository path mismatch."
        )
        payload["decisions"][0]["mismatch_evidence"][0]["mismatch"] = (
            "The customer confirmation text names a different obligation."
        )

        judgment = parse_cluster_triage_judgment(payload, page=page)

        self.assertEqual(
            judgment.decisions[0]["rationale"],
            "Root validation confirmed an ordinary repository path mismatch.",
        )
        self.assertEqual(
            judgment.decisions[0]["mismatch_evidence"][0]["mismatch"],
            "The customer confirmation text names a different obligation.",
        )

    def test_uncertain_is_first_class_and_expands_in_stage_a_plan(self):
        request, _manifest, _capsules, _defect, _objective, pages = _pages(2)
        uncertain_cluster = pages[0].cluster_ids[0]
        judgments = tuple(
            parse_cluster_triage_judgment(
                _payload(
                    page,
                    {
                        cluster_id: (
                            "uncertain"
                            if cluster_id == uncertain_cluster
                            else "unselected"
                        )
                        for cluster_id in page.cluster_ids
                    },
                ),
                page=page,
            )
            for page in pages
        )
        decision = _merge(
            (request, _manifest, _capsules, _defect, _objective, pages),
            pages=pages,
            judgments=judgments,
        )
        plan = build_candidate_cluster_triage_plan(
            request=request,
            decision=decision,
        )

        self.assertIn(uncertain_cluster, plan.effective_selected_cluster_ids)
        self.assertEqual(
            dict(
                (item["cluster_id"], item["disposition"])
                for item in decision.decisions
            )[uncertain_cluster],
            "uncertain",
        )

    def test_merge_rejects_missing_duplicate_foreign_and_stale_pages(self):
        fixture = _pages()
        request, _manifest, _capsules, _defect, _objective, pages = fixture
        judgments = tuple(
            parse_cluster_triage_judgment(_payload(page), page=page)
            for page in pages
        )
        with self.assertRaisesRegex(ValueError, "complete|page"):
            _merge(
                fixture, pages=pages, judgments=judgments[:-1]
            )
        with self.assertRaisesRegex(ValueError, "duplicate|exactly once"):
            _merge(
                fixture,
                pages=pages,
                judgments=judgments + (judgments[0],),
            )
        foreign_page = _pages(1)[-1][0]
        foreign = parse_cluster_triage_judgment(
            _payload(foreign_page),
            page=foreign_page,
        )
        with self.assertRaisesRegex(ValueError, "request|stale"):
            _merge(
                fixture,
                pages=pages,
                judgments=(foreign,) + judgments[1:],
            )

    def test_merge_rejects_fully_resigned_representative_semantic_tamper(self):
        fixture = _pages()
        pages = fixture[-1]
        forged_payload = pages[0].to_dict()
        forged_cluster = forged_payload["clusters"][0]
        forged_capsule = forged_cluster["representative_capsules"][0]
        forged_capsule["candidate"]["node"]["data"]["rationale"] = (
            "Forged semantics claim this candidate is unrelated."
        )
        forged_cluster["semantic_summary"]["representative_facts"][0][
            "semantic_data"
        ]["rationale"] = "Forged semantics claim this candidate is unrelated."
        forged_page = _resign_page(forged_payload)
        forged_pages = (forged_page,) + pages[1:]
        forged_judgment = parse_cluster_triage_judgment(
            _payload(
                forged_page,
                {
                    cluster_id: "unselected"
                    for cluster_id in forged_page.cluster_ids
                },
            ),
            page=forged_page,
        )
        judgments = (forged_judgment,) + tuple(
            parse_cluster_triage_judgment(_payload(page), page=page)
            for page in pages[1:]
        )

        with self.assertRaisesRegex(ValueError, "canonical|authoritative"):
            _merge(fixture, pages=forged_pages, judgments=judgments)


class ClaudeClusterTriageBoundedTest(unittest.TestCase):
    @staticmethod
    def _call(judge, fixture, *, max_physical_requests):
        request, manifest, capsules, defect, objective, _pages_value = fixture
        return judge.triage_candidate_clusters_bounded(
            request,
            manifest=manifest,
            eligible_capsules=capsules,
            active_defect=defect,
            objective=objective,
            analysis_perspective="task quality",
            max_physical_requests=max_physical_requests,
        )

    def test_valid_pages_use_exact_physical_requests_without_other_judges(self):
        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(page)) for page in pages]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        with patch.object(
            judge, "judge_candidates_bounded", side_effect=AssertionError("global")
        ), patch.object(
            judge, "judge_step_bounded", side_effect=AssertionError("step")
        ), patch.object(
            judge, "judge_factor_role_bounded", side_effect=AssertionError("factor")
        ), patch.object(
            judge, "confirm_candidate_bounded", side_effect=AssertionError("root")
        ):
            result = self._call(
                judge, fixture, max_physical_requests=len(pages)
            )

        self.assertEqual(result.physical_requests, len(pages))
        self.assertEqual(transport.request_count, len(pages))
        self.assertEqual(
            {item["cluster_id"] for item in result.value.decisions},
            set(fixture[0].cluster_ids),
        )

    def test_single_page_api_returns_one_exact_bound_judgment(self):
        fixture = _pages(1)
        page = fixture[-1][0]
        transport = ScriptedTransport([json.dumps(_payload(page))])
        judge = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        )

        result = judge.triage_candidate_cluster_page_bounded(
            page,
            max_physical_requests=1,
        )

        self.assertEqual(result.value.page_identity, page.page_identity)
        self.assertEqual(result.value.request_identity, page.request_identity)
        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(transport.request_count, 1)

    def test_single_page_api_uses_transport_completion_budget_up_to_8192(self):
        fixture = _pages(1)
        page = fixture[-1][0]
        transport = ScriptedTransport([json.dumps(_payload(page))])
        transport.max_tokens = 8192
        judge = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        )

        result = judge.triage_candidate_cluster_page_bounded(
            page,
            max_physical_requests=1,
        )

        self.assertEqual(result.physical_requests, 1)
        self.assertEqual(
            [call["max_tokens"] for call in transport.calls],
            [8192],
        )

    def test_single_page_api_preserves_lower_budget_and_caps_higher_budget(self):
        fixture = _pages(1)
        page = fixture[-1][0]

        for configured, expected in ((1024, 1024), (16384, 8192)):
            with self.subTest(configured=configured):
                transport = ScriptedTransport([json.dumps(_payload(page))])
                transport.max_tokens = configured
                judge = ClaudeCausalJudge(
                    transport=transport,
                    cache=JudgmentCache(),
                )

                result = judge.triage_candidate_cluster_page_bounded(
                    page,
                    max_physical_requests=1,
                )

                self.assertEqual(result.physical_requests, 1)
                self.assertEqual(
                    [call["max_tokens"] for call in transport.calls],
                    [expected],
                )

    def test_cache_hit_uses_zero_physical_requests(self):
        fixture = _pages(1)
        page = fixture[-1][0]
        with tempfile.TemporaryDirectory() as tempdir:
            transport = ScriptedTransport([json.dumps(_payload(page))])
            judge = ClaudeCausalJudge(
                transport=transport,
                cache=JudgmentCache(Path(tempdir) / "cache.jsonl"),
            )
            first = self._call(judge, fixture, max_physical_requests=1)
            second = self._call(judge, fixture, max_physical_requests=0)

        self.assertEqual(first.physical_requests, 1)
        self.assertEqual(second.physical_requests, 0)
        self.assertEqual(first.value, second.value)
        self.assertEqual(transport.request_count, 1)

    def test_invalid_output_repairs_within_allowance(self):
        fixture = _pages(1)
        page = fixture[-1][0]
        transport = ScriptedTransport(
            [json.dumps({"invalid": True}), json.dumps(_payload(page))]
        )
        transport.max_tokens = 8192
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        result = self._call(judge, fixture, max_physical_requests=2)

        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        self.assertEqual(
            [call["max_tokens"] for call in transport.calls],
            [8192, 8192],
        )
        repair_call = transport.calls[1]
        repair_payload = json.loads(repair_call["messages"][0]["content"])
        self.assertNotIn("confidence", repair_call["system"])
        self.assertNotIn(
            "confidence", repair_payload["mandatory_output_contract"]
        )
        self.assertIn("cluster", stable_json(repair_payload))

    def test_exhausted_allowance_and_provider_failure_preserve_counts(self):
        fixture = _pages(1)
        judge = ClaudeCausalJudge(
            transport=ScriptedTransport([json.dumps({"invalid": True})]),
            cache=JudgmentCache(),
        )
        with self.assertRaises(BoundedJudgeCallError) as exhausted:
            self._call(judge, fixture, max_physical_requests=1)
        self.assertEqual(exhausted.exception.physical_requests, 1)

        transport = ScriptedTransport([JudgeProviderError("provider failed")])
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())
        with self.assertRaises(BoundedJudgeCallError) as provider:
            self._call(judge, fixture, max_physical_requests=1)
        self.assertEqual(provider.exception.physical_requests, 1)
        self.assertEqual(transport.request_count, 1)

    def test_late_page_cache_failure_includes_prior_page_physical_requests(self):
        class FailSecondWriteCache(JudgmentCache):
            def __init__(self):
                super().__init__()
                self.put_count = 0

            def put_payload(self, **kwargs):
                self.put_count += 1
                if self.put_count == 2:
                    raise RuntimeError("second page cache write failed")
                return super().put_payload(**kwargs)

        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(page)) for page in pages]
        )
        judge = ClaudeCausalJudge(
            transport=transport,
            cache=FailSecondWriteCache(),
        )

        with self.assertRaises(BoundedJudgeCallError) as raised:
            self._call(
                judge,
                fixture,
                max_physical_requests=len(pages),
            )

        self.assertEqual(raised.exception.physical_requests, len(pages))
        self.assertEqual(transport.request_count, len(pages))

    def test_late_page_cache_read_failure_is_typed_and_keeps_prior_count(self):
        class FailSecondReadCache(JudgmentCache):
            def __init__(self):
                super().__init__()
                self.get_count = 0

            def get_validated_payload(self, **kwargs):
                self.get_count += 1
                if self.get_count == 2:
                    raise RuntimeError("second page cache read failed")
                return super().get_validated_payload(**kwargs)

        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(page)) for page in pages]
        )
        judge = ClaudeCausalJudge(
            transport=transport,
            cache=FailSecondReadCache(),
        )

        with self.assertRaises(BoundedJudgeCallError) as raised:
            self._call(judge, fixture, max_physical_requests=len(pages))

        self.assertEqual(raised.exception.physical_requests, 1)
        self.assertEqual(transport.request_count, 1)
        self.assertEqual(raised.exception.diagnostics["failed_page_index"], 1)

    def test_late_page_validation_failure_is_typed_and_cumulative(self):
        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(pages[0])), json.dumps({"invalid": True})]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        with self.assertRaises(BoundedJudgeCallError) as raised:
            self._call(judge, fixture, max_physical_requests=len(pages))

        self.assertEqual(raised.exception.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        self.assertEqual(raised.exception.diagnostics["failed_page_index"], 1)

    def test_late_page_provider_failure_is_typed_and_cumulative(self):
        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(pages[0])), JudgeProviderError("late failure")]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())

        with self.assertRaises(BoundedJudgeCallError) as raised:
            self._call(judge, fixture, max_physical_requests=len(pages))

        self.assertEqual(raised.exception.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        self.assertEqual(raised.exception.diagnostics["failed_page_index"], 1)

    def test_late_page_post_validation_failure_is_typed_and_cumulative(self):
        fixture = _pages()
        pages = fixture[-1]
        transport = ScriptedTransport(
            [json.dumps(_payload(page)) for page in pages]
        )
        judge = ClaudeCausalJudge(transport=transport, cache=JudgmentCache())
        original_parser = parse_cluster_triage_judgment
        calls = 0

        def fail_fourth_parse(value, *, page):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise RuntimeError("late post-validation failure")
            return original_parser(value, page=page)

        with patch(
            "trace_attribution.causal_judge.parse_cluster_triage_judgment",
            side_effect=fail_fourth_parse,
        ), self.assertRaises(BoundedJudgeCallError) as raised:
            self._call(judge, fixture, max_physical_requests=len(pages))

        self.assertEqual(raised.exception.physical_requests, 2)
        self.assertEqual(transport.request_count, 2)
        self.assertEqual(raised.exception.diagnostics["failed_page_index"], 1)


if __name__ == "__main__":
    unittest.main()
