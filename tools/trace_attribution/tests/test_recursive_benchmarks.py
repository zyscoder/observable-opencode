from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
    load_fixture,
    main as evaluate_main,
)
from trace_attribution.causal_judge import OfflineJudgeCapability
from trace_attribution.cli import attribution_output_payload, parse_args
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    PredecessorAssessment,
    RootConfirmation,
    annotate_report_semantic_anchors,
    semantic_anchor_id,
    semantic_anchor_index,
    semantic_occurrence_index,
)
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph
from trace_attribution.models import TraceNode, stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer, RecursiveAnalysisState
from scripts.characterize_trace_fact_closure import characterize_archive


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "recursive_cases"
FIXTURE_NAMES = (
    "sphinx_recursive_minimal.json",
    "prompt_wrong_agent_faithful.json",
    "context_compaction_loss.json",
    "tool_error_ignored.json",
    "subagent_warning_ignored.json",
    "multi_root_failure.json",
    "inferred_missing_edge.json",
    "timeout_amplifier.json",
    "success_negative_control.json",
)


def decision_node(
    *,
    record_id: str,
    cwd: str,
    rationale: str = "Inspect call sites before implementing the parser contract.",
    path: str = "/work/repository/sphinx/domains/c/_parser.py",
    artifact_hash: str = "sha256:parser-contract",
) -> TraceNode:
    return TraceNode(
        ref="record:{0}".format(record_id),
        record_id=record_id,
        component="processor",
        event_type="decision",
        timestamp="2026-07-20T11:22:33.123Z",
        data={
            "cwd": cwd,
            "repository_root": cwd,
            "path": path,
            "rationale": rationale,
            "chosen_action": "inspect_call_sites",
            "pid": 41852,
            "port": 17891,
            "sessionID": "ses_run_a_123456",
            "request_id": "req_provider_a_987654",
            "providerID": "deepseek-run-a",
            "hydrated_artifacts": [
                {"artifact_id": "artifact_run_a", "hash": artifact_hash}
            ],
        },
    )


class FixtureJudge(OfflineJudgeCapability):
    def __init__(self, script: dict):
        self.script = script
        self.requests = []
        self.confirmation_requests = []
        self.request_count = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

    def judge_step(self, request):
        self.requests.append(request.to_dict())
        self.request_count += 1
        value = self.script["steps"][request.current_node.ref]
        predecessors = []
        for item in value.get("predecessors", []):
            upstream = None
            if item.get("relation") == "defect_transformation":
                upstream = request.defect_state.transformed(
                    label=item["upstream_label"],
                    mechanism="The upstream semantic defect transforms into the observed defect.",
                    transformation_reason="Scripted fixture transformation.",
                )
            predecessors.append(
                PredecessorAssessment(
                    ref=item["ref"],
                    relation=item["relation"],
                    reason="Grounded scripted relation for the offline fixture.",
                    confidence=0.95,
                    recurse=True,
                    upstream_defect=upstream,
                    evidence_refs=(item["ref"],),
                )
            )
        introduction = bool(value.get("candidate_introduction"))
        suggested = None
        if introduction:
            suggested = {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently verify the fixture candidate.",
            }
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status=value["status"],
            current_defect_reason="Scripted semantic state for the offline fixture.",
            predecessors=tuple(predecessors),
            candidate_introduction=introduction,
            suggested_investigation=suggested,
            confidence=0.95 if value["status"] != "unknown" else 0.0,
        )

    def confirm_candidate(self, request):
        self.confirmation_requests.append(request)
        value = self.script["confirmations"][request.candidate_ref]
        node_content = str(request.candidate_reference.get("content") or "")
        excerpt = node_content
        if value["status"] == "confirmed":
            result = RootConfirmation.confirmed(
                request.candidate_ref,
                excerpt=excerpt,
                reason="The candidate independently introduces the scripted fixture defect.",
                counterfactual="Replacing this behavior prevents the fixture defect.",
                confidence=value["confidence"],
                evidence_refs=[request.candidate_ref],
            )
        else:
            result = RootConfirmation.rejected(
                request.candidate_ref,
                "The candidate changes exposure but does not independently introduce the defect.",
                evidence_refs=[request.candidate_ref, request.recursive_path[-1]],
                factor_role=value["factor_role"],
            )
            result = replace(
                result,
                confidence=value["confidence"],
                factor_mechanism={
                    "mechanism_type": (
                        "amplification"
                        if value["factor_role"] == "amplifying_factor"
                        else "enabling_condition"
                    ),
                    "source_ref": request.candidate_ref,
                    "target_ref": request.recursive_path[-1],
                    "effect": "The factor changes exposure without independently causing the defect.",
                },
            )
        if request.competing_hypotheses:
            result = replace(
                result,
                competitor_comparisons=tuple(
                    {
                        "hypothesis_id": item["hypothesis_id"],
                        "hypothesis_semantic_hash": item["hypothesis_semantic_hash"],
                        "candidate_ref": item["candidate_reference"]["resolved_ref"],
                        "defect_fingerprint": item["active_defect"]["fingerprint"],
                        "confirmation_identity": item["confirmation_identity"],
                        "recursive_path": list(item["recursive_path"]),
                        "requires_independent_confirmation": item[
                            "requires_independent_confirmation"
                        ],
                        "status": (
                            "co_root"
                            if value["status"] == "confirmed"
                            and self.script["confirmations"].get(
                                item["candidate_reference"]["resolved_ref"], {}
                            ).get("status")
                            == "confirmed"
                            else "outperformed"
                        ),
                        "reason": "The scripted verifier compared this competing hypothesis.",
                        "evidence_refs": [item["candidate_reference"]["resolved_ref"]],
                    }
                    for item in request.competing_hypotheses
                    if item["status"] in {"active", "supported", "unresolved"}
                ),
            )
        return result


def run_fixture(path: Path):
    trace, human_labels, script = load_fixture(path)
    graph = TraceGraph.from_trace(trace)
    judge = FixtureJudge(script)
    report = AgenticRecursiveAnalyzer(judge=judge).analyze(
        graph,
        start_refs=[script["start_ref"]],
        objective="Find the fixture's semantic defect introduction.",
        analysis_perspective="Improve Harness reasoning quality.",
    )
    annotated = annotate_report_semantic_anchors(
        trace["case_id"], graph.nodes, report.to_dict(), graph=graph
    )
    return annotated, human_labels, judge


def task5_broken_claim_fragments(trace: dict) -> list[str]:
    return [
        text
        for record in trace.get("records") or []
        if record.get("event_type") == "response.claim"
        for text in [record.get("data", {}).get("text")]
        if isinstance(text, str) and text.lstrip().startswith((",", "，", ";", "；", ")", "）"))
    ]


class TraceFactClosureBenchmarkTest(unittest.TestCase):
    def test_archive_characterizer_is_deterministic_and_hermetic(self):
        trace = {
            "manifest": {"case_id": "characterizer-fixture", "run_id": "run"},
            "nodes": [],
            "edges": [],
            "artifacts": [],
            "records": [
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": ", broken historical fragment"},
                }
            ],
            "dataflow_edges": [],
        }
        source_bytes = json.dumps(trace, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "case" / "partial" / "latest.json"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(source_bytes)
            first = characterize_archive(partial)
            second = characterize_archive(partial)

        self.assertEqual(first, second)
        self.assertEqual(first["path"], str(partial.resolve()))
        self.assertEqual(first["sha256"], hashlib.sha256(source_bytes).hexdigest())
        self.assertEqual(first["byte_length"], len(source_bytes))
        self.assertEqual(first["broken_claim_fragments"], 1)
        self.assertEqual(first["artifact_files"], 0)
        self.assertIsNone(first["subject_revision"])

    def test_legacy_archive_fixture_fails_closed_without_rewriting_historical_gaps(self):
        trace = {
            "manifest": {"case_id": "legacy-astropy", "run_id": "legacy-run"},
            "artifacts": [
                {
                    "artifact_id": "legacy_missing_artifact",
                    "kind": "text",
                    "path": "artifacts/sha256/legacy.txt",
                    "availability": "bundled",
                    "hash": "0123456789abcdef",
                    "content_hash": "0123456789abcdef",
                    "byte_length": 128,
                }
            ],
            "records": [
                {
                    "record_id": "legacy_claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "text": ", a pre-computed separability matrix from a nested compound model), it used the wrong values.",
                        "artifact_id": "legacy_missing_artifact",
                    },
                }
            ],
            "dataflow_edges": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(trace, artifact_root=Path(directory))
            for ref in graph.nodes:
                graph.hydrate_node(ref)

        self.assertEqual(len(task5_broken_claim_fragments(trace)), 1)
        self.assertNotIn("subject_revision", trace["manifest"])
        self.assertEqual(
            {
                key: graph.artifact_hydration[key]
                for key in ("loaded", "missing", "truncated", "slice_fallbacks", "hash_mismatches")
            },
            {"loaded": 0, "missing": 1, "truncated": 0, "slice_fallbacks": 0, "hash_mismatches": 0},
        )
        self.assertEqual(
            [
                ref
                for ref in graph.default_start_refs()
                if graph.nodes[ref].event_type == "external.evaluation_fact"
            ],
            [],
        )
        self.assertEqual(graph.nodes["record:legacy_claim"].data["text"], trace["records"][0]["data"]["text"])

    def test_current_terminalbench_fixture_is_a_revision_matched_failed_seed(self):
        slice_content = "SIGINT leaves async workers running."
        full_content = slice_content + " Detailed shutdown diagnostics follow."
        trace = {
            "manifest": {
                "case_id": "current-terminalbench",
                "run_id": "current-terminalbench-run",
                "subject_revision": "git:task5-terminalbench",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": "current-terminalbench",
                    "run_id": "current-terminalbench-run",
                },
            },
            "artifacts": [
                {
                    "artifact_id": "terminalbench_output",
                    "kind": "text",
                    "path": "artifacts/sha256/terminalbench.txt",
                    "availability": "bundled",
                    "hash": hashlib.sha256(full_content.encode("utf-8")).hexdigest()[:16],
                    "content_hash": hashlib.sha256(full_content.encode("utf-8")).hexdigest()[:16],
                    "byte_length": len(full_content.encode("utf-8")),
                    "semantic_slices": [
                        {
                            "byte_range": [0, len(slice_content.encode("utf-8"))],
                            "content": slice_content,
                            "hash": hashlib.sha256(slice_content.encode("utf-8")).hexdigest()[:16],
                            "truncated": True,
                        }
                    ],
                }
            ],
            "records": [
                {
                    "record_id": "terminalbench_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {"artifact_id": "terminalbench_output"},
                }
            ],
            "dataflow_edges": [],
        }
        trace = inject_external_evaluation_facts(
            trace,
            [
                {
                    "source": "terminalbench",
                    "scope": "cancel_async_tasks_after_sigint",
                    "subject_revision": "git:task5-terminalbench",
                    "assertion": "All async tasks are cancelled after SIGINT.",
                    "observation": "The external evaluator found async workers still running.",
                    "status": "failed",
                    "observed_at": "2026-07-21T12:00:00Z",
                    "evidence_refs": ["record:terminalbench_result"],
                    "provenance": {"method": "terminalbench_grader", "version": "1.0"},
                }
            ],
        )

        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(trace, artifact_root=Path(directory))
            for ref in graph.nodes:
                graph.hydrate_node(ref)
            starts = graph.default_start_refs()
            state = RecursiveAnalysisState.create(
                graph=graph,
                start_refs=starts,
                objective="Find why async task cancellation failed.",
                analysis_perspective="Use only reconstructed formal facts.",
            )

        self.assertEqual(len(task5_broken_claim_fragments(trace)), 0)
        self.assertEqual(
            {
                key: graph.artifact_hydration[key]
                for key in ("loaded", "missing", "truncated", "slice_fallbacks", "hash_mismatches")
            },
            {"loaded": 1, "missing": 0, "truncated": 1, "slice_fallbacks": 1, "hash_mismatches": 0},
        )
        hydrated = graph.nodes["record:terminalbench_result"].data["hydrated_artifacts"][0]
        self.assertEqual(hydrated["source"], "embedded_semantic_slice")
        self.assertTrue(hydrated["truncated"])
        self.assertEqual(hydrated["hash_status"], "verified")
        self.assertEqual(len(starts), 1)
        external = graph.nodes[starts[0]]
        self.assertEqual(external.event_type, "external.evaluation_fact")
        self.assertEqual(external.status, "failed")
        self.assertEqual(external.data["revision_status"], "matched")
        self.assertEqual(external.data["revision_provenance_status"], "valid")
        self.assertTrue(external.data["eligible_for_decisive_judgment"])
        self.assertFalse(external.data["root_candidate_eligible"])
        self.assertEqual(state.seed_count, 1)
        self.assertEqual(next(iter(state.defect_states.values())).label, "external_evaluation_failed")


class SemanticAnchorTest(unittest.TestCase):
    def test_anchor_survives_run_local_ids_cwd_and_transport_metadata(self):
        left = decision_node(
            record_id="decisionnode_dec_85_aaa111",
            cwd="/private/tmp/run-a/repository",
            path="/private/tmp/run-a/repository/sphinx/domains/c/_parser.py",
        )
        right = decision_node(
            record_id="decisionnode_dec_85_bbb222",
            cwd="/opt/runner/run-b/repository",
            path="/opt/runner/run-b/repository/sphinx/domains/c/_parser.py",
        )
        right = TraceNode(
            **{
                **right.__dict__,
                "timestamp": "2027-01-01T00:00:00Z",
                "data": {
                    **right.data,
                    "pid": 7,
                    "port": 49152,
                    "sessionID": "ses_run_b_999999",
                    "request_id": "req_provider_b_000000",
                    "providerID": "anthropic-run-b",
                    "hydrated_artifacts": [
                        {
                            "artifact_id": "artifact_run_b",
                            "hash": "sha256:parser-contract",
                        }
                    ],
                },
            }
        )

        self.assertEqual(
            semantic_anchor_id("sphinx-case", left),
            semantic_anchor_id("sphinx-case", right),
        )
        self.assertTrue(semantic_anchor_id("sphinx-case", left).startswith("semantic_anchor:v2:"))

    def test_anchor_retains_meaningful_semantics_without_collisions(self):
        baseline = semantic_anchor_id(
            "sphinx-case", decision_node(record_id="dec_a", cwd="/repo")
        )
        variants = {
            semantic_anchor_id(
                "sphinx-case",
                decision_node(
                    record_id="dec_b",
                    cwd="/repo",
                    rationale="Implement immediately without inspecting call sites.",
                ),
            ),
            semantic_anchor_id(
                "sphinx-case",
                decision_node(
                    record_id="dec_c",
                    cwd="/repo",
                    path="/repo/sphinx/domains/python/_parser.py",
                ),
            ),
            semantic_anchor_id(
                "sphinx-case",
                decision_node(
                    record_id="dec_d",
                    cwd="/repo",
                    artifact_hash="sha256:different-contract",
                ),
            ),
            semantic_anchor_id(
                "different-case", decision_node(record_id="dec_e", cwd="/repo")
            ),
        }
        self.assertNotIn(baseline, variants)
        self.assertEqual(len(variants), 4)


class RecursiveMetricTest(unittest.TestCase):
    def fixture(self, name="sphinx_recursive_minimal.json"):
        report, labels, judge = run_fixture(FIXTURE_ROOT / name)
        trace, _, _ = load_fixture(FIXTURE_ROOT / name)
        return report, labels, judge, TraceGraph.from_trace(trace)

    def test_metrics_have_exact_schema_and_bounded_values(self):
        report, labels, _, graph = self.fixture()
        result = compare_report(
            report,
            labels,
            {"metadata": {"physical_judge_request_count": 10}},
            graph=graph,
        )

        self.assertEqual(result["schema_version"], "recursive-attribution-comparison/v4")
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "case_id",
                "metrics",
                "counts",
                "disagreements",
                "safety",
            },
        )
        self.assertEqual(
            set(result["metrics"]),
            {
                "confirmed_root_recall",
                "confirmed_root_precision",
                "semantic_confirmed_root_recall",
                "semantic_confirmed_root_precision",
                "introduction_candidate_recall",
                "introduction_candidate_precision",
                "semantic_introduction_candidate_recall",
                "semantic_introduction_candidate_precision",
                "top1_match",
                "negative_control_correct",
                "judge_request_reduction",
                "request_ratio",
                "mean_causal_path_length",
                "factor_role_precision",
                "unknown_rate",
                "confirmation_rejection_rate",
                "investigation_yield",
                "checkpoint_reuse_rate",
                "human_llm_disagreement_rate",
            },
        )
        self.assertEqual(result["metrics"]["confirmed_root_recall"], 1.0)
        self.assertEqual(result["metrics"]["confirmed_root_precision"], 1.0)
        self.assertTrue(result["metrics"]["top1_match"])
        self.assertEqual(result["metrics"]["judge_request_reduction"], 1.0)
        self.assertEqual(result["metrics"]["request_ratio"], 0.0)
        self.assertEqual(result["counts"]["request_delta"], 10)
        for key, value in result["metrics"].items():
            if key in {"top1_match", "negative_control_correct", "judge_request_reduction", "request_ratio", "mean_causal_path_length"}:
                continue
            self.assertGreaterEqual(value, 0.0, key)
            self.assertLessEqual(value, 1.0, key)

    def test_empty_denominators_are_explicit_and_success_control_scores_cleanly(self):
        report, labels, _, graph = self.fixture("success_negative_control.json")
        result = compare_report(report, labels, None, graph=graph)

        self.assertEqual(result["metrics"]["confirmed_root_recall"], 1.0)
        self.assertEqual(result["metrics"]["confirmed_root_precision"], 1.0)
        self.assertIsNone(result["metrics"]["top1_match"])
        self.assertTrue(result["metrics"]["negative_control_correct"])
        self.assertIsNone(result["metrics"]["judge_request_reduction"])
        self.assertIsNone(result["metrics"]["request_ratio"])
        self.assertIsNone(result["counts"]["request_delta"])
        self.assertEqual(result["metrics"]["mean_causal_path_length"], 0.0)
        self.assertEqual(result["metrics"]["unknown_rate"], 0.0)
        self.assertEqual(result["metrics"]["confirmation_rejection_rate"], 0.0)
        self.assertEqual(result["metrics"]["investigation_yield"], 0.0)
        self.assertEqual(result["metrics"]["checkpoint_reuse_rate"], 0.0)

    def test_missing_current_request_measurement_does_not_invent_reduction(self):
        report, labels, _, graph = self.fixture()
        for key in (
            "physical_judge_request_count",
            "judge_request_count",
            "provider_request_count",
        ):
            report["metadata"].pop(key, None)

        result = compare_report(
            report,
            labels,
            {"metadata": {"judge_request_count": 10}},
            graph=graph,
        )

        self.assertIsNone(result["counts"]["judge_request_count"])
        self.assertEqual(result["counts"]["legacy_judge_request_count"], 10)
        self.assertIsNone(result["metrics"]["judge_request_reduction"])
        self.assertIsNone(result["metrics"]["request_ratio"])
        self.assertIsNone(result["counts"]["request_delta"])

    def test_request_performance_reports_signed_regression(self):
        report, labels, _, graph = self.fixture()
        report["metadata"]["physical_judge_request_count"] = 12

        result = compare_report(
            report,
            labels,
            {"metadata": {"physical_judge_request_count": 10}},
            graph=graph,
        )

        self.assertEqual(result["metrics"]["judge_request_reduction"], -0.2)
        self.assertEqual(result["metrics"]["request_ratio"], 1.2)
        self.assertEqual(result["counts"]["request_delta"], -2)

    def test_fabricated_unresolved_duplicate_and_budget_states_fail_closed(self):
        base, labels, _, graph = self.fixture()
        cases = []

        fabricated = copy.deepcopy(base)
        fabricated["metadata"]["fabricated_refs"] = ["record:invented"]
        cases.append(fabricated)

        unresolved = copy.deepcopy(base)
        unresolved["unresolved_refs"] = [unresolved["confirmed_roots"][0]["node_ref"]]
        cases.append(unresolved)

        duplicate = copy.deepcopy(base)
        duplicate["step_judgments"].append(copy.deepcopy(duplicate["step_judgments"][0]))
        cases.append(duplicate)

        exhausted = copy.deepcopy(base)
        exhausted["metadata"]["unresolved_branches"] = [
            {
                "node_ref": exhausted["confirmed_roots"][0]["node_ref"],
                "reason": "judge_request_limit",
            }
        ]
        cases.append(exhausted)

        collision = copy.deepcopy(base)
        collision["metadata"]["semantic_anchor_collisions"] = [
            {
                "semantic_anchor_id": collision["confirmed_roots"][0]["semantic_anchor_id"],
                "node_refs": ["record:a", "record:b"],
            }
        ]
        cases.append(collision)

        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(EvaluationSafetyError):
                    compare_report(report, labels, None, graph=graph)

    def test_cli_failure_is_nonzero_and_writes_no_output(self):
        report, labels, _, _ = self.fixture()
        trace, _, _ = load_fixture(FIXTURE_ROOT / "sphinx_recursive_minimal.json")
        report["metadata"]["fabricated_refs"] = ["record:invented"]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            trace_path = root / "trace.json"
            report_path = root / "report.json"
            labels_path = root / "labels.json"
            output_path = root / "comparison.json"
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            labels_path.write_text(json.dumps(labels), encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                status = evaluate_main(
                    [
                        "--trace",
                        str(trace_path),
                        "--report",
                        str(report_path),
                        "--labels",
                        str(labels_path),
                        "--out",
                        str(output_path),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertFalse(output_path.exists())

class RecursiveFixtureTest(unittest.TestCase):
    def test_all_fixture_documents_exist_and_keep_labels_out_of_trace(self):
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                trace, human_labels, script = load_fixture(FIXTURE_ROOT / name)
                self.assertEqual(trace["case_id"], human_labels["case_id"])
                self.assertNotIn("human_labels", trace)
                self.assertNotIn("scripted_analysis", trace)
                self.assertTrue(script)
                label_text = stable_json(human_labels)
                self.assertNotIn(label_text, stable_json(trace))
                self.assertNotIn("expected_roots", stable_json(trace))

    def test_fixture_labels_bind_computed_semantic_and_occurrence_identities(self):
        for name in FIXTURE_NAMES:
            trace, human_labels, _ = load_fixture(FIXTURE_ROOT / name)
            graph = TraceGraph.from_trace(trace)
            anchors = semantic_anchor_index(trace["case_id"], graph)
            occurrences = semantic_occurrence_index(trace["case_id"], graph)
            self.assertEqual(
                human_labels["schema_version"], "recursive-attribution-labels/v3"
            )
            for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
                for item in human_labels[role]:
                    self.assertEqual(
                        item["semantic_anchor_id"],
                        anchors[item["node_ref"]],
                    )
                    self.assertEqual(
                        item["semantic_occurrence_id"],
                        occurrences[item["node_ref"]],
                    )

    def test_load_fixture_rejects_label_or_script_fields_nested_in_trace_facts(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "leaky.json"
            path.write_text(
                json.dumps(
                    {
                        "case_id": "leaky",
                        "records": [
                            {
                                "record_id": "prompt",
                                "component": "prompt",
                                "event_type": "message.input",
                                "data": {"human_labels": {"roots": ["record:prompt"]}},
                            }
                        ],
                        "human_labels": {
                            "schema_version": "recursive-attribution-labels/v3",
                            "case_id": "leaky",
                            "roots": [],
                            "conditions": [],
                            "amplifiers": [],
                            "forbidden_roots": [],
                            "allowed_unresolved_outcomes": [],
                        },
                        "scripted_analysis": {"steps": []},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_fixture(path)

    def test_report_projection_adds_anchors_without_mutating_report_or_nodes(self):
        trace, _, _ = load_fixture(FIXTURE_ROOT / "sphinx_recursive_minimal.json")
        graph = TraceGraph.from_trace(trace)
        original = {
            "case_id": trace["case_id"],
            "root_causes": [{"node_ref": "record:decision"}],
            "metadata": {},
        }
        before = copy.deepcopy(original)
        nodes_before = copy.deepcopy(graph.nodes)

        projected = annotate_report_semantic_anchors(
            trace["case_id"], graph.nodes, original, graph=graph
        )

        self.assertEqual(original, before)
        self.assertEqual(graph.nodes, nodes_before)
        self.assertEqual(
            projected["root_causes"][0]["semantic_anchor_id"],
            semantic_anchor_id(trace["case_id"], graph.nodes["record:decision"]),
        )
        self.assertEqual(
            projected["metadata"]["semantic_anchor_schema_version"],
            "semantic-anchor/v2",
        )

    def test_cli_output_projection_keeps_legacy_default_and_adds_anchors(self):
        trace, _, _ = load_fixture(FIXTURE_ROOT / "sphinx_recursive_minimal.json")
        graph = TraceGraph.from_trace(trace)

        class Report:
            def to_dict(self):
                return {
                    "case_id": trace["case_id"],
                    "root_causes": [{"node_ref": "record:decision"}],
                    "metadata": {},
                }

        payload = attribution_output_payload(Report(), graph)
        args = parse_args(["--trace", "/tmp/trace.json", "--out", "/tmp/out.json"])
        self.assertEqual(args.engine, "legacy")
        self.assertEqual(
            payload["root_causes"][0]["semantic_anchor_id"],
            semantic_anchor_id(trace["case_id"], graph.nodes["record:decision"]),
        )

    def test_deterministic_plumbing_matrix_preserves_distinct_causal_roles(self):
        expected_primary = {
            "sphinx_recursive_minimal.json": "record:decision",
            "prompt_wrong_agent_faithful.json": "record:prompt",
            "context_compaction_loss.json": "record:compaction",
            "tool_error_ignored.json": "record:decision",
            "subagent_warning_ignored.json": "record:main_agent_decision",
            "inferred_missing_edge.json": "record:decision",
            "timeout_amplifier.json": "record:decision",
        }
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                report, human_labels, judge = run_fixture(FIXTURE_ROOT / name)
                trace, _, _ = load_fixture(FIXTURE_ROOT / name)
                comparison = compare_report(
                    report,
                    human_labels,
                    None,
                    graph=TraceGraph.from_trace(trace),
                )
                self.assertEqual(comparison["metrics"]["confirmed_root_recall"], 1.0)
                if human_labels["roots"]:
                    self.assertTrue(comparison["metrics"]["top1_match"])
                else:
                    self.assertIsNone(comparison["metrics"]["top1_match"])
                self.assertEqual(comparison["metrics"]["factor_role_precision"], 1.0)
                request_text = stable_json(judge.requests)
                for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
                    for item in human_labels[role]:
                        self.assertNotIn(item["semantic_anchor_id"], request_text)
                        self.assertNotIn(item["semantic_occurrence_id"], request_text)
                if name == "success_negative_control.json":
                    self.assertEqual(report["analysis_outcome"], "no_defect")
                    self.assertEqual(report["confirmed_roots"], [])
                elif name == "multi_root_failure.json":
                    self.assertEqual(len(report["confirmed_roots"]), 1)
                    self.assertEqual(len(report["co_roots"]), 1)
                else:
                    self.assertEqual(
                        report["confirmed_roots"][0]["node_ref"],
                        expected_primary[name],
                    )
        sphinx, _, _ = run_fixture(FIXTURE_ROOT / "sphinx_recursive_minimal.json")
        self.assertEqual(
            [item["node_ref"] for item in sphinx["contributing_conditions"]],
            ["record:prompt"],
        )
        self.assertEqual(
            [item["node_ref"] for item in sphinx["amplifying_factors"]],
            ["record:timeout"],
        )


if __name__ == "__main__":
    unittest.main()
