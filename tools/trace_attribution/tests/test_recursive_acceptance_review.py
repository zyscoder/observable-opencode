from __future__ import annotations

import copy
import hashlib
import io
import json
import random
import tempfile
import unittest
import unicodedata
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSchemaError,
    EvaluationSafetyError,
    compare_report,
    load_fixture,
    main as evaluate_main,
    validate_labels,
)
import trace_attribution.causal_state as causal_state_module
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    FactorRoleJudgment,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    semantic_anchor_id,
    semantic_anchor_index,
    semantic_occurrence_index,
)
from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    OfflineJudgeCapability,
    RootConfirmationRequest,
    factor_role_request_identity,
    root_confirmation_request_projection,
)
from trace_attribution.global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.judgment_context import (
    build_active_failure_factual_context,
)
from trace_attribution.models import TraceNode, stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer

from tests.test_recursive_benchmarks import FIXTURE_ROOT, FixtureJudge, run_fixture


def node(*, data, source_refs=(), component="planner", event_type="decision"):
    return TraceNode(
        ref="record:node",
        record_id="node",
        component=component,
        event_type=event_type,
        data=data,
        source_refs=list(source_refs),
    )


class ActiveFailureRootAcceptanceTest(unittest.TestCase):
    def test_independent_acceptance_confirmation_can_publish_false_closure_root(self):
        binding_type = getattr(
            causal_state_module,
            "ActiveFailureRoleBinding",
            None,
        )
        self.assertIsNotNone(binding_type)
        defect = DefectState.create(
            label="acceptance",
            expected="Closure follows independent acceptance verification.",
            actual="The task was closed without acceptance verification.",
            mechanism="false closure",
            scope="acceptance",
        )
        seed_identity = causal_state_module.seed_binding_identity_for(
            "record:acceptance_defect",
            defect.fingerprint,
        )
        binding = binding_type.create(
            candidate_ref="record:dec_343",
            seed_ref="record:acceptance_defect",
            failure_signature=defect.fingerprint,
            failure_kind="acceptance",
            causal_role="false_closure",
            disposition="root",
            counterfactual_prevention_signatures=(defect.fingerprint,),
        )
        confirmation = RootConfirmation(
            candidate_ref="record:dec_343",
            status="confirmed",
            excerpt="close despite missing acceptance verification",
            reason="Replacing the closure decision prevents this acceptance failure.",
            counterfactual=confirmation_counterfactual_for(
                "record:dec_343",
                "confirmed",
            ),
            confidence=0.95,
            evidence_refs=("record:dec_343", "record:acceptance_defect"),
            counterfactual_status="supports_causality",
            hypothesis_id="hypothesis:acceptance-closure",
            hypothesis_semantic_hash="semantic:acceptance-closure",
            defect_fingerprint=defect.fingerprint,
            recursive_path=("record:dec_343", "record:acceptance_defect"),
            seed_binding_identity=seed_identity,
            analysis_perspective="task quality",
            factor_role="necessary_cause",
        )
        path_references = tuple(
            {
                "raw_ref": ref,
                "resolved_ref": ref,
                "resolution_status": "resolved",
                "provenance_class": "recorded",
            }
            for ref in confirmation.recursive_path
        )
        factual_context = build_active_failure_factual_context(
            defect_state=defect,
            seed_ref="record:acceptance_defect",
            candidate_entries=(
                {
                    "candidate_ref": "record:dec_343",
                    "path_refs": confirmation.recursive_path,
                    "path_references": (
                        {
                            **path_references[0],
                            "node": {
                                "component": "agent",
                                "event_type": "decision",
                                "status": "completed",
                                "data": {
                                    "phase": "closure",
                                    "rationale": (
                                        "close despite missing acceptance "
                                        "verification"
                                    ),
                                },
                            },
                        },
                        {
                            **path_references[1],
                            "node": {
                                "component": "evaluation",
                                "event_type": "case.observed_defect",
                                "status": "failed",
                                "data": {"actual": defect.actual},
                            },
                        },
                    ),
                    "edges": (
                        {
                            "from_ref": "record:dec_343",
                            "to_ref": "record:acceptance_defect",
                            "relation": (
                                "authored_decision_observed_by_evaluation"
                            ),
                            "edge_origin": "trace.dataflow_edges",
                            "source_container": "trace.dataflow_edges",
                            "evidence_type": "confirmed",
                        },
                    ),
                },
            ),
        )
        request_projection = root_confirmation_request_projection(
            RootConfirmationRequest(
                candidate_ref=confirmation.candidate_ref,
                defect_state=defect,
                recursive_path=confirmation.recursive_path,
                candidate_reference=path_references[0],
                recursive_path_references=path_references,
                supporting_evidence=path_references,
                opposing_evidence=(),
                competing_hypotheses=(),
                task_obligations=(),
                analysis_perspective=confirmation.analysis_perspective,
                hypothesis_id=confirmation.hypothesis_id,
                hypothesis_semantic_hash=(
                    confirmation.hypothesis_semantic_hash
                ),
                seed_binding_identity=confirmation.seed_binding_identity,
                factual_context=factual_context,
            )
        )
        publication = causal_state_module.canonical_confirmed_root_publication(
            confirmation=confirmation,
            defect_state=defect,
            candidate_node=TraceNode(
                ref="record:dec_343",
                record_id="dec_343",
                component="agent",
                event_type="decision",
                data={
                    "phase": "closure",
                    "rationale": "close despite missing acceptance verification",
                },
            ),
            seed_start_ref="record:acceptance_defect",
            active_role_binding=binding,
            request_projection=request_projection,
        )

        self.assertEqual(publication.causal_role, "false_closure")
        self.assertEqual(
            publication.provenance["active_role_binding"],
            binding.to_dict(),
        )
        self.assertEqual(
            causal_state_module.ConfirmedRoot.from_dict(
                publication.to_dict()
            ).to_dict(),
            publication.to_dict(),
        )

    def test_functional_signature_cannot_publish_false_closure_as_root(self):
        binding_type = getattr(
            causal_state_module,
            "ActiveFailureRoleBinding",
            None,
        )
        self.assertIsNotNone(binding_type)
        defect = DefectState.create(
            label="functional",
            expected="The implementation preserves behavior.",
            actual="The implementation changes behavior.",
            mechanism="functional regression",
            scope="functional",
        )

        with self.assertRaisesRegex(ValueError, "closure substitution"):
            binding_type.create(
                candidate_ref="record:dec_343",
                seed_ref="record:functional_defect",
                failure_signature=defect.fingerprint,
                failure_kind="functional",
                causal_role="false_closure",
                disposition="root",
                counterfactual_prevention_signatures=(defect.fingerprint,),
            )


class SemanticAnchorV2ReviewTest(unittest.TestCase):
    def test_set_like_file_order_and_nfkc_casefold_are_stable(self):
        left = node(
            data={
                "files": ["src/B.py", "src/a.py"],
                "rationale": unicodedata.normalize("NFC", "Café CONTRACT"),
            }
        )
        right = node(
            data={
                "files": ["src/a.py", "src/B.py"],
                "rationale": unicodedata.normalize("NFD", "café contract"),
            }
        )

        self.assertEqual(semantic_anchor_id("case", left), semantic_anchor_id("case", right))
        self.assertTrue(semantic_anchor_id("case", left).startswith("semantic_anchor:v2:"))

    def test_order_sensitive_paths_remain_distinct(self):
        left = node(data={"recursive_path": ["record:a", "record:b", "record:c"]})
        right = node(data={"recursive_path": ["record:a", "record:c", "record:b"]})

        self.assertNotEqual(semantic_anchor_id("case", left), semantic_anchor_id("case", right))

    def test_unverified_absolute_paths_do_not_collapse_to_last_segments(self):
        left = node(data={"path": "/alpha/shared/a/b/c.py"})
        right = node(data={"path": "/beta/shared/a/b/c.py"})

        self.assertNotEqual(semantic_anchor_id("case", left), semantic_anchor_id("case", right))

    def test_verified_repo_roots_produce_stable_relative_paths(self):
        left = node(data={"repository_root": "/run/a/repo", "path": "/run/a/repo/src/a.py"})
        right = node(data={"repository_root": "/opt/b/repo", "path": "/opt/b/repo/src/a.py"})

        self.assertEqual(semantic_anchor_id("case", left), semantic_anchor_id("case", right))

    def test_posix_paths_and_code_identifiers_preserve_case(self):
        upper_path = node(data={"repository_root": "/repo", "path": "/repo/src/Foo.py"})
        lower_path = node(data={"repository_root": "/repo", "path": "/repo/src/foo.py"})
        upper_identifier = node(data={"chosen_action": "ParseNamespaceObject"})
        lower_identifier = node(data={"chosen_action": "parseNamespaceObject"})

        self.assertNotEqual(
            semantic_anchor_id("case", upper_path), semantic_anchor_id("case", lower_path)
        )
        self.assertNotEqual(
            semantic_anchor_id("case", upper_identifier),
            semantic_anchor_id("case", lower_identifier),
        )

    def test_path_normpath_equivalence_and_repo_root_escape_marker(self):
        direct = node(data={"repository_root": "/repo", "path": "/repo/src/a.py"})
        dotted = node(data={"repository_root": "/repo", "path": "/repo/src/../src/./a.py"})
        escaped_a = node(data={"repository_root": "/repo", "path": "/repo/../secret.txt"})
        escaped_b = node(
            data={"repository_root": "/different/repo", "path": "/different/repo/../secret.txt"}
        )
        in_root_secret = node(data={"repository_root": "/repo", "path": "/repo/secret.txt"})
        nfc_path = node(
            data={
                "repository_root": "/repo",
                "path": unicodedata.normalize("NFC", "/repo/src/Café.py"),
            }
        )
        nfd_path = node(
            data={
                "repository_root": "/repo",
                "path": unicodedata.normalize("NFD", "/repo/src/Café.py"),
            }
        )
        windows_upper = node(
            data={"repository_root": "C:\\Repo", "path": "C:\\Repo\\src\\Foo.py"}
        )
        windows_lower = node(
            data={"repository_root": "c:\\repo", "path": "c:\\repo\\src\\foo.py"}
        )

        self.assertEqual(semantic_anchor_id("case", direct), semantic_anchor_id("case", dotted))
        self.assertEqual(
            semantic_anchor_id("case", escaped_a), semantic_anchor_id("case", escaped_b)
        )
        self.assertNotEqual(
            semantic_anchor_id("case", escaped_a), semantic_anchor_id("case", in_root_secret)
        )
        self.assertEqual(
            semantic_anchor_id("case", nfc_path), semantic_anchor_id("case", nfd_path)
        )
        self.assertEqual(
            semantic_anchor_id("case", windows_upper),
            semantic_anchor_id("case", windows_lower),
        )

    def test_artifact_hydration_envelope_does_not_change_node_identity(self):
        plain = node(data={"rationale": "inspect the contract", "artifact_hash": "sha256:" + "a" * 64})
        hydrated = node(
            data={
                "rationale": "inspect the contract",
                "artifact_hash": "sha256:" + "a" * 64,
                "hydrated_artifacts": [
                    {
                        "artifact_id": "run-local-proof",
                        "hash": "sha256:" + "a" * 64,
                        "content": "large runtime payload",
                    }
                ],
            }
        )

        self.assertEqual(semantic_anchor_id("case", plain), semantic_anchor_id("case", hydrated))

    def test_semantic_anchor_is_content_only_and_occurrence_uses_causal_neighborhood(self):
        trace = {
            "case_id": "neighborhood-case",
            "records": [
                {"record_id": "prompt-a", "component": "input", "event_type": "prompt.assembly", "data": {"text": "alpha"}},
                {"record_id": "prompt-b", "component": "input", "event_type": "prompt.assembly", "data": {"text": "beta"}},
                {"record_id": "decision-a", "component": "planner-a", "event_type": "decision", "source_refs": ["record:prompt-a"], "data": {"rationale": "apply contract"}},
                {"record_id": "decision-b", "component": "planner-b", "event_type": "decision", "source_refs": ["record:prompt-b"], "data": {"rationale": "apply contract"}},
            ],
            "dataflow_edges": [],
        }
        graph = TraceGraph.from_trace(trace)
        anchors = semantic_anchor_index(graph.case_id, graph)
        occurrences = semantic_occurrence_index(graph.case_id, graph)
        self.assertEqual(anchors["record:decision-a"], anchors["record:decision-b"])
        self.assertNotEqual(
            occurrences["record:decision-a"], occurrences["record:decision-b"]
        )

        report = {"case_id": graph.case_id, "root_causes": [{"node_ref": "record:decision-a"}], "metadata": {}}
        projected = annotate_report_semantic_anchors(graph.case_id, graph.nodes, report, graph=graph)
        self.assertEqual(set(projected["metadata"]["semantic_anchor_index"]), set(graph.nodes))
        self.assertEqual(
            set(projected["metadata"]["semantic_occurrence_index"]), set(graph.nodes)
        )

    def test_existing_semantic_anchor_does_not_change_when_duplicate_is_added(self):
        base = {
            "case_id": "stable-case",
            "records": [
                {"record_id": "prompt-a", "component": "input", "event_type": "prompt.assembly", "data": {"text": "alpha"}},
                {"record_id": "decision-a", "component": "planner", "event_type": "decision", "source_refs": ["record:prompt-a"], "data": {"rationale": "apply contract"}},
            ],
            "dataflow_edges": [],
        }
        expanded = copy.deepcopy(base)
        expanded["records"].extend(
            [
                {"record_id": "prompt-b", "component": "input", "event_type": "prompt.assembly", "data": {"text": "beta"}},
                {"record_id": "decision-b", "component": "planner-alias", "event_type": "decision", "source_refs": ["record:prompt-b"], "data": {"rationale": "apply contract"}},
            ]
        )
        base_graph = TraceGraph.from_trace(base)
        expanded_graph = TraceGraph.from_trace(expanded)

        before = semantic_anchor_index(base_graph.case_id, base_graph)["record:decision-a"]
        after = semantic_anchor_index(expanded_graph.case_id, expanded_graph)["record:decision-a"]
        occurrences = semantic_occurrence_index(expanded_graph.case_id, expanded_graph)

        self.assertEqual(before, after)
        self.assertNotEqual(
            occurrences["record:decision-a"], occurrences["record:decision-b"]
        )

    def test_semantic_and_occurrence_identities_are_stable_across_run_local_ids(self):
        def graph(prompt_id, decision_id, root):
            return TraceGraph.from_trace(
                {
                    "case_id": "cross-run-case",
                    "records": [
                        {
                            "record_id": prompt_id,
                            "component": "input-run-local",
                            "event_type": "prompt.assembly",
                            "data": {
                                "text": "Apply the repository contract",
                                "repository_root": root,
                                "path": root + "/src/Foo.py",
                            },
                        },
                        {
                            "record_id": decision_id,
                            "component": "planner-run-local",
                            "event_type": "decision",
                            "source_refs": ["record:" + prompt_id],
                            "data": {"rationale": "apply contract"},
                        },
                    ],
                    "dataflow_edges": [],
                }
            )

        left = graph("prompt-1042", "decision-1043", "/tmp/run-1042/repo")
        right = graph("prompt-9911", "decision-9912", "/private/tmp/run-9911/repo")

        self.assertEqual(
            semantic_anchor_index(left.case_id, left)["record:decision-1043"],
            semantic_anchor_index(right.case_id, right)["record:decision-9912"],
        )
        self.assertEqual(
            semantic_occurrence_index(left.case_id, left)["record:decision-1043"],
            semantic_occurrence_index(right.case_id, right)["record:decision-9912"],
        )

    def test_full_graph_collision_is_reported_even_for_unpublished_node(self):
        trace = {
            "case_id": "collision-case",
            "records": [
                {"record_id": "same-a", "component": "planner", "event_type": "decision", "data": {"rationale": "same"}},
                {"record_id": "same-b", "component": "planner-alias", "event_type": "decision", "data": {"rationale": "same"}},
            ],
            "dataflow_edges": [],
        }
        graph = TraceGraph.from_trace(trace)
        report = {"case_id": graph.case_id, "root_causes": [{"node_ref": "record:same-a"}], "metadata": {}}

        projected = annotate_report_semantic_anchors(graph.case_id, graph.nodes, report, graph=graph)

        self.assertEqual(len(projected["metadata"]["semantic_anchor_collisions"]), 1)
        self.assertEqual(
            projected["metadata"]["semantic_anchor_collisions"][0]["node_refs"],
            ["record:same-a", "record:same-b"],
        )
        self.assertEqual(len(projected["metadata"]["semantic_occurrence_collisions"]), 1)


class TraceBackedAcceptanceReviewTest(unittest.TestCase):
    def setUp(self):
        self.report, self.labels, _ = run_fixture(FIXTURE_ROOT / "sphinx_recursive_minimal.json")
        trace_document = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
        trace_document.pop("human_labels")
        trace_document.pop("scripted_analysis")
        self.graph = TraceGraph.from_trace(trace_document)

    def compare(self, report=None, labels=None):
        return compare_report(
            report or self.report,
            labels or self.labels,
            None,
            graph=self.graph,
        )

    def test_acceptance_requires_independent_trace(self):
        with self.assertRaises(EvaluationSafetyError):
            compare_report(self.report, self.labels, None, graph=None)

    def test_self_consistent_fabricated_root_cannot_ground_itself(self):
        report = copy.deepcopy(self.report)
        root = report["confirmed_roots"][0]
        old_identity = root["confirmation"]["confirmation_identity"]
        confirmation = copy.deepcopy(root["confirmation"])
        confirmation["candidate_ref"] = "record:invented"
        confirmation["recursive_path"][0] = "record:invented"
        confirmation["confirmation_identity"] = confirmation_identity_for(
            hypothesis_id=confirmation["hypothesis_id"],
            hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
            candidate_ref=confirmation["candidate_ref"],
            defect_fingerprint=confirmation["defect_fingerprint"],
            recursive_path=tuple(confirmation["recursive_path"]),
        )
        root["node_ref"] = "record:invented"
        root["recursive_path"] = list(confirmation["recursive_path"])
        root["confirmation"] = copy.deepcopy(confirmation)
        report["confirmations"] = [
            confirmation if item["confirmation_identity"] == old_identity else item
            for item in report["confirmations"]
        ]
        report["metadata"]["semantic_anchor_index"]["record:invented"] = root["semantic_anchor_id"]

        with self.assertRaises(EvaluationSafetyError):
            self.compare(report)

    def test_evaluator_rejects_forged_confirmed_seed_binding(self):
        report = copy.deepcopy(self.report)
        seed = report["seed_results"][0]
        seed["outcome"] = "confirmed_root"
        seed["confirmed_root_refs"] = ["record:invented"]
        seed["confirmation_identities"] = ["confirmation:invented"]

        with self.assertRaises((EvaluationSchemaError, EvaluationSafetyError)):
            self.compare(report)

    def test_evaluator_rejects_same_start_ref_seed_with_fresh_defect_identity(self):
        report = copy.deepcopy(self.report)
        seed = report["seed_results"][0]
        fresh_defect = DefectState.create(
            label="fresh_but_internally_valid_defect",
            expected="A distinct defect state is retained.",
            actual="The prior root is incorrectly reused.",
            mechanism="Adversarial seed identity substitution.",
            scope="same_start_ref_regression",
        )
        seed["defect_state"] = fresh_defect.to_dict()
        seed["defect_fingerprint"] = fresh_defect.fingerprint
        report["defect_states"].append(fresh_defect.to_dict())

        with self.assertRaises(ValueError):
            RecursiveAttributionReport.from_dict(report)
        with self.assertRaises((EvaluationSchemaError, EvaluationSafetyError)):
            self.compare(report)

    def test_ambiguous_source_trace_duplicate_record_is_rejected(self):
        trace = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
        trace.pop("human_labels")
        trace.pop("scripted_analysis")
        duplicate = copy.deepcopy(
            next(item for item in trace["records"] if item["record_id"] == "decision")
        )
        trace["records"].append(duplicate)
        ambiguous_graph = TraceGraph.from_trace(trace)

        with self.assertRaises(EvaluationSafetyError):
            compare_report(self.report, self.labels, None, graph=ambiguous_graph)

    def test_disconnected_recursive_path_and_anchor_index_mismatch_fail(self):
        disconnected = copy.deepcopy(self.report)
        root = disconnected["confirmed_roots"][0]
        identity = root["confirmation"]["confirmation_identity"]
        confirmation = copy.deepcopy(root["confirmation"])
        confirmation["recursive_path"] = ["record:decision", "record:timeout", "record:observed"]
        confirmation["confirmation_identity"] = confirmation_identity_for(
            hypothesis_id=confirmation["hypothesis_id"],
            hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
            candidate_ref=confirmation["candidate_ref"],
            defect_fingerprint=confirmation["defect_fingerprint"],
            recursive_path=tuple(confirmation["recursive_path"]),
        )
        root["recursive_path"] = list(confirmation["recursive_path"])
        root["confirmation"] = copy.deepcopy(confirmation)
        disconnected["confirmations"] = [
            confirmation if item["confirmation_identity"] == identity else item
            for item in disconnected["confirmations"]
        ]
        with self.assertRaises(EvaluationSafetyError):
            self.compare(disconnected)

        mismatch = copy.deepcopy(self.report)
        mismatch["metadata"]["semantic_anchor_index"]["record:decision"] = "semantic_anchor:v2:wrong"
        with self.assertRaises(EvaluationSafetyError):
            self.compare(mismatch)

        label_mismatch = copy.deepcopy(self.labels)
        label_mismatch["roots"][0]["semantic_anchor_id"] = "semantic_anchor:v2:" + "0" * 24
        with self.assertRaises(EvaluationSafetyError):
            self.compare(labels=label_mismatch)

        occurrence_mismatch = copy.deepcopy(self.labels)
        occurrence_mismatch["roots"][0]["semantic_occurrence_id"] = (
            "semantic_occurrence:v1:" + "0" * 24
        )
        with self.assertRaises(EvaluationSafetyError):
            self.compare(labels=occurrence_mismatch)

        ungrounded_candidate_edge = copy.deepcopy(self.report)
        candidate = next(
            item
            for item in ungrounded_candidate_edge["causal_candidates"]
            if item.get("edge", {}).get("to_ref")
        )
        candidate["edge"]["eligible_for_attribution"] = False
        candidate["edge"]["retrieval_candidate"] = True
        candidate["edge"]["to_ref"] = "record:invented"
        with self.assertRaises(EvaluationSafetyError):
            self.compare(ungrounded_candidate_edge)

    def test_valid_semantic_collision_uses_occurrences_without_invalidating_existing_label(self):
        trace = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
        trace.pop("human_labels")
        trace.pop("scripted_analysis")
        duplicate = copy.deepcopy(
            next(item for item in trace["records"] if item["record_id"] == "decision")
        )
        duplicate["record_id"] = "decision-repeat"
        duplicate["source_refs"] = []
        trace["records"].append(duplicate)
        graph = TraceGraph.from_trace(trace)
        report = annotate_report_semantic_anchors(
            graph.case_id, graph.nodes, self.report, graph=graph
        )

        result = compare_report(report, self.labels, None, graph=graph)

        self.assertTrue(result["safety"]["passed"])
        self.assertEqual(len(report["metadata"]["semantic_anchor_collisions"]), 1)
        self.assertEqual(report["metadata"]["semantic_occurrence_collisions"], [])

    def test_same_anchor_distinct_occurrence_roots_score_independently(self):
        trace = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
        trace.pop("human_labels")
        trace.pop("scripted_analysis")
        duplicate = copy.deepcopy(
            next(item for item in trace["records"] if item["record_id"] == "decision")
        )
        duplicate.update(
            {
                "record_id": "decision-repeat",
                "component": "planner-alias",
                "source_refs": ["record:independent-prompt"],
            }
        )
        trace["records"].extend(
            [
                {
                    "record_id": "independent-prompt",
                    "component": "input-alias",
                    "event_type": "prompt.assembly",
                    "data": {"text": "independent causal context"},
                },
                duplicate,
            ]
        )
        graph = TraceGraph.from_trace(trace)
        anchors = semantic_anchor_index(graph.case_id, graph)
        occurrences = semantic_occurrence_index(graph.case_id, graph)
        self.assertEqual(anchors["record:decision"], anchors["record:decision-repeat"])
        self.assertNotEqual(
            occurrences["record:decision"], occurrences["record:decision-repeat"]
        )

        report = annotate_report_semantic_anchors(
            graph.case_id, graph.nodes, self.report, graph=graph
        )
        labels = copy.deepcopy(self.labels)
        labels["schema_version"] = "recursive-attribution-labels/v3"
        labels.pop("materializations")
        labels.pop("unrelated")
        for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
            for item in labels[role]:
                item["semantic_occurrence_id"] = occurrences[item["node_ref"]]
        labels["roots"] = [
            {
                "semantic_anchor_id": anchors["record:decision-repeat"],
                "semantic_occurrence_id": occurrences["record:decision-repeat"],
            },
            labels["roots"][0],
        ]

        result = compare_report(report, labels, None, graph=graph)

        self.assertEqual(result["schema_version"], "recursive-attribution-comparison/v7")
        self.assertEqual(result["metrics"]["confirmed_root_recall"], 0.5)
        self.assertEqual(result["metrics"]["confirmed_root_precision"], 1.0)
        self.assertTrue(result["metrics"]["top1_match"])
        self.assertEqual(result["metrics"]["semantic_confirmed_root_recall"], 1.0)
        self.assertEqual(result["counts"]["expected_root_count"], 2)
        self.assertEqual(result["counts"]["predicted_root_count"], 1)

        top1_labels = copy.deepcopy(labels)
        top1_labels["roots"] = [labels["roots"][0]]
        top1 = compare_report(report, top1_labels, None, graph=graph)
        self.assertFalse(top1["metrics"]["top1_match"])
        self.assertEqual(top1["metrics"]["confirmed_root_recall"], 0.0)
        self.assertEqual(top1["metrics"]["semantic_confirmed_root_recall"], 1.0)

    def test_labels_reject_duplicate_occurrence_and_require_dual_identity(self):
        labels = copy.deepcopy(self.labels)
        labels["schema_version"] = "recursive-attribution-labels/v3"
        occurrences = semantic_occurrence_index(self.graph.case_id, self.graph)
        for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
            for item in labels[role]:
                item["semantic_occurrence_id"] = occurrences[item["node_ref"]]

        missing_occurrence = copy.deepcopy(labels)
        missing_occurrence["roots"][0].pop("semantic_occurrence_id")
        with self.assertRaises(EvaluationSchemaError):
            validate_labels(missing_occurrence)

        duplicate_occurrence = copy.deepcopy(labels)
        duplicate_occurrence["roots"].append(
            {
                "semantic_anchor_id": labels["roots"][0]["semantic_anchor_id"],
                "semantic_occurrence_id": labels["roots"][0]["semantic_occurrence_id"],
            }
        )
        with self.assertRaises(EvaluationSchemaError):
            validate_labels(duplicate_occurrence)

    def test_outcome_state_machine_unresolved_policy_and_ratio_bounds_are_enforced(self):
        contradictory = copy.deepcopy(self.report)
        contradictory["analysis_outcome"] = "no_defect"
        with self.assertRaises(EvaluationSafetyError):
            self.compare(contradictory)

        disallowed = copy.deepcopy(self.report)
        disallowed["confirmed_roots"] = []
        disallowed["co_roots"] = []
        disallowed["analysis_outcome"] = "inconclusive"
        labels = copy.deepcopy(self.labels)
        labels["allowed_unresolved_outcomes"] = []
        with self.assertRaises(EvaluationSafetyError):
            self.compare(disallowed, labels)

        impossible_reuse = copy.deepcopy(self.report)
        impossible_reuse["metadata"]["logical_judge_call_count"] = 1
        impossible_reuse["metadata"]["checkpoint_reused_judgment_count"] = 2
        with self.assertRaises(EvaluationSafetyError):
            self.compare(impossible_reuse)

        duplicate_judgment = copy.deepcopy(self.report)
        duplicate_judgment["metadata"]["factor_role_judgments"].append(
            copy.deepcopy(
                duplicate_judgment["metadata"][
                    "factor_role_judgments"
                ][0]
            )
        )
        with self.assertRaises(EvaluationSafetyError):
            self.compare(duplicate_judgment)

        fabricated_factor = copy.deepcopy(self.report)
        fabricated_factor["contributing_conditions"][0]["mechanism"]["source_ref"] = "record:invented"
        with self.assertRaises(EvaluationSafetyError):
            self.compare(fabricated_factor)

    def test_strict_report_schema_and_semantic_duplicate_judgment_are_rejected(self):
        unknown_field = copy.deepcopy(self.report)
        unknown_field["self_declared_ground_truth"] = {"root": "record:decision"}
        with self.assertRaises((EvaluationSchemaError, EvaluationSafetyError)):
            self.compare(unknown_field)

        semantic_duplicate = copy.deepcopy(self.report)
        duplicate = copy.deepcopy(
            semantic_duplicate["contributing_conditions"][0]
        )
        duplicate["reason"] = (
            "Different prose cannot create a new factor publication."
        )
        duplicate["confidence"] = max(0.0, duplicate["confidence"] - 0.01)
        semantic_duplicate["contributing_conditions"].append(duplicate)
        with self.assertRaises(EvaluationSafetyError):
            self.compare(semantic_duplicate)

    def test_confirmation_evidence_is_resolved_independently_from_root_evidence(self):
        report = copy.deepcopy(self.report)
        root = report["confirmed_roots"][0]
        identity = root["confirmation"]["confirmation_identity"]
        confirmation = copy.deepcopy(root["confirmation"])
        confirmation["evidence_refs"].append("record:invented")
        root["confirmation"] = copy.deepcopy(confirmation)
        report["confirmations"] = [
            confirmation if item["confirmation_identity"] == identity else item
            for item in report["confirmations"]
        ]

        with self.assertRaises(EvaluationSafetyError):
            self.compare(report)

    def test_metrics_separate_introduction_and_confirmed_roots_and_negative_control_top1(self):
        result = self.compare()
        self.assertEqual(result["metrics"]["confirmed_root_recall"], 1.0)
        self.assertEqual(result["metrics"]["confirmed_root_precision"], 1.0)
        self.assertIn("introduction_candidate_recall", result["metrics"])
        self.assertNotIn("candidate_recall", result["metrics"])

        partial_labels = copy.deepcopy(self.labels)
        partial_labels["roots"].append(
            {
                "node_ref": "record:observed",
                "semantic_anchor_id": self.report["metadata"]["semantic_anchor_index"]["record:observed"],
                "semantic_occurrence_id": self.report["metadata"]["semantic_occurrence_index"]["record:observed"],
            }
        )
        partial = self.compare(labels=partial_labels)
        self.assertEqual(partial["metrics"]["confirmed_root_recall"], 0.5)
        self.assertEqual(partial["metrics"]["confirmed_root_precision"], 1.0)

        report, labels, _ = run_fixture(FIXTURE_ROOT / "success_negative_control.json")
        trace = json.loads((FIXTURE_ROOT / "success_negative_control.json").read_text())
        trace.pop("human_labels")
        trace.pop("scripted_analysis")
        negative = compare_report(report, labels, None, graph=TraceGraph.from_trace(trace))
        self.assertTrue(negative["metrics"]["negative_control_correct"])
        self.assertIsNone(negative["metrics"]["top1_match"])

    def test_cli_without_trace_is_nonzero(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            report_path = root / "report.json"
            labels_path = root / "labels.json"
            out_path = root / "comparison.json"
            report_path.write_text(json.dumps(self.report), encoding="utf-8")
            labels_path.write_text(json.dumps(self.labels), encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                evaluate_main(
                    [
                        "--report", str(report_path),
                        "--labels", str(labels_path),
                        "--out", str(out_path),
                    ]
                )
            self.assertFalse(out_path.exists())

    def test_artifact_evidence_is_recomputed_from_trace_content_owner_and_range(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()
            content = b"grounded contract evidence\n"
            (artifact_dir / "proof.txt").write_bytes(content)
            trace = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
            labels = trace.pop("human_labels")
            script = trace.pop("scripted_analysis")
            trace["artifacts"] = [
                {
                    "artifact_id": "proof",
                    "path": "artifacts/proof.txt",
                    "hash": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
            ]
            for record in trace["records"]:
                if record["record_id"] == "decision":
                    record["artifact_refs"] = ["artifact:proof"]
            trace_path = root / "trace.json"
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            graph = TraceGraph.from_file(trace_path)

            class ArtifactFixtureJudge(FixtureJudge):
                def confirm_candidate(self, request):
                    confirmation = super().confirm_candidate(request)
                    if request.candidate_ref == "record:decision":
                        confirmation = replace(
                            confirmation,
                            evidence_refs=("artifact:proof",),
                        )
                    return confirmation

            report = AgenticRecursiveAnalyzer(
                judge=ArtifactFixtureJudge(script)
            ).analyze(
                graph,
                start_refs=[script["start_ref"]],
                objective="Find the fixture's semantic defect introduction.",
                analysis_perspective="Improve Harness reasoning quality.",
            )
            report = annotate_report_semantic_anchors(
                graph.case_id, graph.nodes, report.to_dict(), graph=graph
            )

            graph.hydrate_node("record:decision")
            report = annotate_report_semantic_anchors(
                graph.case_id, graph.nodes, report, graph=graph
            )
            anchors = semantic_anchor_index(graph.case_id, graph)
            occurrences = semantic_occurrence_index(graph.case_id, graph)
            for role in (
                "roots",
                "conditions",
                "amplifiers",
                "materializations",
                "unrelated",
                "forbidden_roots",
            ):
                for label in labels[role]:
                    ref = str(label.get("node_ref") or "")
                    label["semantic_anchor_id"] = anchors[ref]
                    label["semantic_occurrence_id"] = occurrences[ref]

            self.assertTrue(compare_report(report, labels, None, graph=graph)["safety"]["passed"])

            invalid_hash_trace = copy.deepcopy(trace)
            invalid_hash_trace["artifacts"][0]["hash"] = "sha256:x"
            invalid_path = root / "invalid-trace.json"
            invalid_path.write_text(json.dumps(invalid_hash_trace), encoding="utf-8")
            invalid_graph = TraceGraph.from_file(invalid_path)
            invalid_report = annotate_report_semantic_anchors(
                invalid_graph.case_id, invalid_graph.nodes, report, graph=invalid_graph
            )
            with self.assertRaises(EvaluationSafetyError):
                compare_report(invalid_report, labels, None, graph=invalid_graph)

    def test_uncited_source_artifact_with_noncanonical_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()
            (artifact_dir / "unused.txt").write_text("unused evidence", encoding="utf-8")
            trace = json.loads((FIXTURE_ROOT / "sphinx_recursive_minimal.json").read_text())
            trace.pop("human_labels")
            trace.pop("scripted_analysis")
            trace["artifacts"] = [
                {
                    "artifact_id": "unused",
                    "path": "artifacts/unused.txt",
                    "hash": "truncated-hash",
                }
            ]
            trace_path = root / "trace.json"
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            graph = TraceGraph.from_file(trace_path)
            report = annotate_report_semantic_anchors(
                graph.case_id, graph.nodes, self.report, graph=graph
            )

            with self.assertRaises(EvaluationSafetyError):
                compare_report(report, self.labels, None, graph=graph)


def _semantic_payload(node_value):
    return {
        "event_type": str(node_value.get("event_type") or "").casefold(),
        "data": dict(node_value.get("data") or {}),
    }


def _fixture_rule_class(node_value):
    payload = _semantic_payload(node_value)
    event_type = payload["event_type"]
    text = stable_json(payload["data"]).casefold()
    if event_type == "case.observed_success":
        return "absent"
    if event_type == "verification":
        return "propagation" if any(token in text for token in ("failed", "missing", "absent")) else "absent"
    if event_type == "change":
        return "propagation" if any(
            token in text
            for token in ("removed", "omitted", "without", "despite", "constant-iv")
        ) else "absent"
    if "compaction" in event_type and "dropped_constraint" in text:
        return "root"
    if event_type == "decision":
        if "follow the explicit requirement exactly" in text:
            return "propagation"
        if "compacted context" in text:
            return "propagation"
        if any(
            token in text
            for token in (
                "only the new",
                "remove_compatibility",
                "constant initialization",
                "skip audit",
                "omit_",
                "despite the warning",
                "without investigating",
                "stop_search_early",
                "only methods returned",
            )
        ):
            return "root"
        return "absent"
    if "prompt" in event_type:
        return "root" if "remove authorization checks" in text else "absent"
    if event_type in {"tool.result", "tool.error", "subagent.result"}:
        return "condition" if any(token in text for token in ("failed", "warning", "error")) else "absent"
    if "interruption" in event_type or "timeout" in event_type:
        return "amplifier"
    return "absent"


def _reference_node(reference):
    content = reference.get("content") if isinstance(reference, dict) else reference.get("content")
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return {"event_type": "", "data": {"text": content}}
        if isinstance(value, dict):
            return value
    return dict(content) if isinstance(content, dict) else {}


class DeterministicRuleSemanticJudge(
    OfflineJudgeCapability,
    GlobalJudgeCapability,
):
    """Fixture-phrase rule smoke probe; never attribution-quality evidence."""

    def __init__(self):
        self.request_count = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.semantic_inputs = []
        self.global_requests = []

    def judge_step(self, request):
        current = {
            "event_type": request.current_node.event_type,
            "data": dict(request.current_node.data),
        }
        self.semantic_inputs.append(_semantic_payload(current))
        current_class = _fixture_rule_class(current)
        predecessors = []
        for candidate in request.candidates:
            candidate_value = {
                "event_type": candidate.node.event_type,
                "data": dict(candidate.node.data),
            }
            predecessor_class = _fixture_rule_class(candidate_value)
            propagates = (
                current_class != "absent"
                and predecessor_class != "absent"
                and candidate.edge.get("eligible_for_attribution") is True
            )
            predecessors.append(
                PredecessorAssessment(
                    ref=candidate.ref,
                    relation="same_defect_propagation" if propagates else "unrelated",
                    reason="The semantic facts either preserve or do not preserve the observed mismatch.",
                    confidence=0.85,
                    recurse=propagates,
                    evidence_refs=(candidate.ref,),
                )
            )
        introduction = current_class in {"root", "condition", "amplifier"}
        suggested = None
        if introduction:
            suggested = {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently check the candidate's semantic action and counterfactual.",
            }
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="absent" if current_class == "absent" else "present",
            current_defect_reason="Classified from event and data semantics only.",
            predecessors=tuple(predecessors),
            candidate_introduction=introduction,
            suggested_investigation=suggested,
            confidence=0.85,
        )

    def confirm_candidate(self, request):
        candidate_class = _fixture_rule_class(_reference_node(request.candidate_reference))
        if candidate_class == "root":
            result = RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt=str(request.candidate_reference.get("content") or ""),
                reason="The candidate semantic action independently introduces the mismatch.",
                counterfactual=confirmation_counterfactual_for(
                    request.candidate_ref,
                    "confirmed",
                ),
                confidence=0.85,
                evidence_refs=[request.candidate_ref],
            )
        else:
            factor_role = {
                "condition": "contributing_condition",
                "amplifier": "amplifying_factor",
            }.get(candidate_class, "unrelated")
            result = RootConfirmation.rejected(
                request.candidate_ref,
                "The candidate affects exposure but is not an independent semantic introduction.",
                evidence_refs=[request.candidate_ref, request.recursive_path[-1]],
                factor_role=factor_role,
            )
            if factor_role in {"contributing_condition", "amplifying_factor"}:
                result = replace(
                    result,
                    confidence=0.8,
                    factor_mechanism={
                        "mechanism_type": "amplification" if factor_role == "amplifying_factor" else "enabling_condition",
                        "source_ref": request.candidate_ref,
                        "target_ref": request.recursive_path[-1],
                        "effect": "The semantic fact changes exposure without independently introducing the mismatch.",
                    },
                )
        comparisons = []
        for hypothesis in request.competing_hypotheses:
            if hypothesis.get("status") not in {"active", "supported", "unresolved"}:
                continue
            competitor_class = _fixture_rule_class(
                _reference_node(hypothesis["candidate_reference"])
            )
            comparisons.append(
                {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                    "hypothesis_semantic_hash": hypothesis["hypothesis_semantic_hash"],
                    "candidate_ref": hypothesis["candidate_reference"]["resolved_ref"],
                    "defect_fingerprint": hypothesis["active_defect"]["fingerprint"],
                    "confirmation_identity": hypothesis["confirmation_identity"],
                    "recursive_path": list(hypothesis["recursive_path"]),
                    "requires_independent_confirmation": hypothesis["requires_independent_confirmation"],
                    "status": "co_root" if candidate_class == competitor_class == "root" else "outperformed",
                    "reason": "Compared from candidate semantic content.",
                    "evidence_refs": [hypothesis["candidate_reference"]["resolved_ref"]],
                }
            )
        return replace(result, competitor_comparisons=tuple(comparisons))

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        capsules = []
        selected = []
        for capsule in request.capsules:
            node = dict(capsule.candidate["node"])
            self.semantic_inputs.append(_semantic_payload(node))
            candidate_class = _fixture_rule_class(node)
            event_type = str(node.get("event_type") or "")
            role = {
                "condition": "contributing_condition",
                "amplifier": "amplifying_factor",
            }.get(candidate_class)
            if (
                role is not None
                and event_type
                in {
                    "case.failed",
                    "subagent.result",
                    "tool.error",
                    "tool.result",
                }
            ):
                role = "outcome_evidence"
            is_root = (
                candidate_class == "root"
                and capsule.candidate_ref
                in request.open_authored_root_candidate_refs
            )
            if is_root:
                selected.append(capsule.candidate_ref)
            capsules.append((capsule, role, is_root))

        assessments = []
        page_has_factor = any(
            role is not None for _, role, _ in capsules
        )
        outcome = (
            "candidate_roots"
            if selected
            else (
                "inconclusive"
                if page_has_factor
                else "no_defect"
            )
        )
        for capsule, role, is_root in capsules:
            is_factor = role is not None
            is_open = (
                capsule.candidate_ref
                in request.open_authored_root_candidate_refs
            )
            prevents = is_root or role in {
                "contributing_condition",
                "amplifying_factor",
            }
            assessments.append(
                GlobalCandidateAssessment(
                    candidate_ref=capsule.candidate_ref,
                    defect_status=(
                        "present"
                        if is_root or is_factor
                        else "absent"
                    ),
                    input_defect_status=(
                        "present"
                        if role == "outcome_evidence"
                        else (
                            "absent"
                            if is_root
                            or is_factor
                            or outcome == "no_defect"
                            else "unknown"
                        )
                    ),
                    output_defect_status=(
                        "present"
                        if is_root or is_factor
                        else "absent"
                    ),
                    causal_path_refs=(
                        tuple(capsule.downstream_path)
                        if is_root or is_factor or is_open
                        else ()
                    ),
                    counterfactual={
                        "intervention_ref": capsule.candidate_ref,
                        "intervention_kind": (
                            "replace_with_semantically_correct_behavior"
                        ),
                        "predicted_defect_status": (
                            "absent" if prevents else "present"
                        ),
                        "causal_effect": (
                            "prevents_defect"
                            if prevents
                            else "does_not_prevent_defect"
                        ),
                    },
                    compared_candidate_refs=(
                        request.open_authored_root_candidate_refs
                    ),
                    causal_role=(
                        "root_candidate"
                        if is_root
                        else (
                            role
                            if role is not None
                            else "exculpatory_evidence"
                        )
                    ),
                    responsibility=(
                        "primary"
                        if is_root
                        else (
                            "shared"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "none"
                        )
                    ),
                    candidate_phase=(
                        "implementation"
                        if is_root
                        else (
                            "planning"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "intermediate"
                        )
                    ),
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode=(
                        "positive_introduction"
                        if is_root
                        else (
                            "omission_enabling_condition"
                            if role
                            in {
                                "contributing_condition",
                                "amplifying_factor",
                            }
                            else "none"
                        )
                    ),
                    obligation_refs=(),
                    contribution_mechanism=(
                        {
                            "type": "scope_narrowing",
                            "target_ref": request.seed_ref,
                            "effect": (
                                "The factor constrained the behavior reaching "
                                "the observed defect."
                            ),
                            "evidence_refs": (capsule.candidate_ref,),
                        }
                        if role
                        in {
                            "contributing_condition",
                            "amplifying_factor",
                        }
                        else None
                    ),
                    reason=(
                        "Classified from candidate event and data "
                        "semantics only."
                    ),
                    evidence_refs=(capsule.candidate_ref,),
                    confidence=0.85,
                )
            )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome=outcome,
                reason=(
                    "Compared all candidates from event and data "
                    "semantics only."
                ),
                assessments=tuple(assessments),
                selected_candidate_refs=tuple(selected),
                expansion_requests=(),
                decisive_evidence_refs=(
                    tuple(selected)
                    if selected
                    else (
                        (request.capsules[0].candidate_ref,)
                        if outcome == "no_defect"
                        and request.capsules
                        else ()
                    )
                ),
                missing_evidence=(
                    (
                        "This page contains a non-root factor; compare it "
                        "with surviving root candidates."
                    ),
                )
                if outcome == "inconclusive"
                else (),
                confidence=0.85,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": (
                        request.active_defect.fingerprint
                    ),
                    "active_focus_text_hash": (
                        request.active_focus_text_hash
                    ),
                },
            ),
            0,
        )

    def judge_factor_role(self, request):
        node = _reference_node(request.candidate_reference)
        candidate_class = _fixture_rule_class(node)
        role = {
            "condition": "contributing_condition",
            "amplifier": "amplifying_factor",
        }.get(candidate_class, "unrelated")
        if node.get("event_type") in {
            "case.failed",
            "subagent.result",
            "tool.error",
            "tool.result",
        }:
            role = "downstream_materialization"
        mechanism_type = {
            "contributing_condition": "enabling_condition",
            "amplifying_factor": "amplification",
            "downstream_materialization": (
                "downstream_materialization"
            ),
        }.get(role)
        mechanism = (
            {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": mechanism_type,
                "source_ref": request.candidate_ref,
                "target_ref": request.recursive_path[-1],
                "effect": (
                    "The semantic fact changes defect exposure or "
                    "carries the defect downstream."
                ),
            }
            if mechanism_type is not None
            else {}
        )
        predicted_effect = {
            "contributing_condition": "reduces_defect_likelihood",
            "amplifying_factor": "reduces_defect_severity",
            "downstream_materialization": (
                "defect_still_present_without_materialization"
            ),
            "unrelated": (
                "no_grounded_causal_influence_established"
            ),
        }[role]
        return FactorRoleJudgment(
            candidate_ref=request.candidate_ref,
            necessity_status="not_necessary",
            factor_role=role,
            reason=(
                "Independent semantic review classified the "
                "candidate's non-root role."
            ),
            confidence=0.8,
            evidence_refs=(request.candidate_ref,),
            recursive_path=request.recursive_path,
            factor_mechanism=mechanism,
            counterfactual={
                "schema": "factor-role-counterfactual/v1",
                "intervention_ref": request.candidate_ref,
                "intervention_kind": (
                    "replace_with_semantically_correct_behavior"
                ),
                "predicted_effect": predicted_effect,
            },
            hypothesis_id=request.hypothesis_id,
            hypothesis_semantic_hash=(
                request.hypothesis_semantic_hash
            ),
            defect_fingerprint=request.defect_state.fingerprint,
            seed_binding_identity=request.seed_binding_identity,
            analysis_perspective=request.analysis_perspective,
            request_identity=factor_role_request_identity(request),
        )


def metamorphic_trace(name, seed):
    trace, _, _ = load_fixture(FIXTURE_ROOT / name)
    transformed = copy.deepcopy(trace)
    rng = random.Random(seed)
    old_ids = [item["record_id"] for item in transformed["records"]]
    mapping = {
        old: "rule_{0:08x}".format(rng.getrandbits(32))
        for old in old_ids
    }
    component_aliases = {}
    for index, record in enumerate(transformed["records"]):
        old = record["record_id"]
        component = record.get("component", "")
        component_aliases.setdefault(component, "layer_{0}_{1}".format(seed, len(component_aliases)))
        record["record_id"] = mapping[old]
        record["component"] = component_aliases[component]
        record["source_refs"] = [
            "record:" + mapping[item.removeprefix("record:")]
            for item in record.get("source_refs", [])
        ]
    for edge in transformed.get("dataflow_edges", []):
        for endpoint in ("from", "to"):
            value = edge.get(endpoint)
            if isinstance(value, dict) and value.get("id") in mapping:
                value["id"] = mapping[value["id"]]
        edge["evidence_refs"] = [
            "record:" + mapping[item.removeprefix("record:")]
            for item in edge.get("evidence_refs", [])
        ]
    rng.shuffle(transformed["records"])
    return transformed


def run_rule_smoke(trace):
    graph = TraceGraph.from_trace(trace)
    judge = DeterministicRuleSemanticJudge()
    start = next(
        ref
        for ref, node in graph.nodes.items()
        if node.event_type in {"case.observed_defect", "case.observed_success"}
    )
    report = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
    ).analyze(
        graph,
        start_refs=[start],
        objective="Find the semantic mismatch introduction.",
        analysis_perspective="Improve Harness reasoning quality.",
    )
    return annotate_report_semantic_anchors(
        graph.case_id, graph.nodes, report.to_dict(), graph=graph
    ), judge


class DeterministicRuleSemanticSmokeTest(unittest.TestCase):
    CASES = (
        "prompt_wrong_agent_faithful.json",
        "context_compaction_loss.json",
        "tool_error_ignored.json",
        "subagent_warning_ignored.json",
        "multi_root_failure.json",
        "timeout_amplifier.json",
    )

    def test_fixture_phrase_rules_survive_ref_component_and_order_changes(self):
        for name in self.CASES:
            with self.subTest(name=name):
                original_trace, human_labels, _ = load_fixture(FIXTURE_ROOT / name)
                original, original_judge = run_rule_smoke(original_trace)
                variant, variant_judge = run_rule_smoke(metamorphic_trace(name, 73))

                def signature(report):
                    return {
                        "outcome": report["analysis_outcome"],
                        "roots": sorted(
                            item["semantic_anchor_id"]
                            for item in [*report["confirmed_roots"], *report["co_roots"]]
                        ),
                        "conditions": sorted(
                            item["semantic_anchor_id"] for item in report["contributing_conditions"]
                        ),
                        "amplifiers": sorted(
                            item["semantic_anchor_id"] for item in report["amplifying_factors"]
                        ),
                    }

                original_signature = signature(original)
                self.assertEqual(original_signature, signature(variant))
                self.assertEqual(
                    original_signature["roots"],
                    sorted(item["semantic_anchor_id"] for item in human_labels["roots"]),
                )
                self.assertEqual(
                    original_signature["conditions"],
                    sorted(item["semantic_anchor_id"] for item in human_labels["conditions"]),
                )
                self.assertEqual(
                    original_signature["amplifiers"],
                    sorted(item["semantic_anchor_id"] for item in human_labels["amplifiers"]),
                )
                self.assertTrue(original_judge.semantic_inputs)
                self.assertTrue(variant_judge.semantic_inputs)
                self.assertNotIn("component", stable_json(original_judge.semantic_inputs))
                self.assertNotIn("record:", stable_json(original_judge.semantic_inputs))

    def test_paraphrase_probe_records_fixture_phrase_rule_limitation(self):
        trace, _, _ = load_fixture(FIXTURE_ROOT / "multi_root_failure.json")
        original, _ = run_rule_smoke(trace)
        paraphrased = copy.deepcopy(trace)
        encryption = next(
            item for item in paraphrased["records"] if item["record_id"] == "encryption_decision"
        )
        encryption["data"]["rationale"] = (
            "Reuse one fixed IV for every export to maintain compatibility."
        )
        encryption["data"]["chosen_action"] = "reuse_fixed_iv"
        changed, _ = run_rule_smoke(paraphrased)

        def root_refs(report):
            return {
                item["node_ref"]
                for item in [*report["confirmed_roots"], *report["co_roots"]]
            }

        self.assertEqual(
            root_refs(original),
            {"record:encryption_decision", "record:audit_decision"},
        )
        self.assertEqual(root_refs(changed), {"record:audit_decision"})
