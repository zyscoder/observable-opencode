from __future__ import annotations

import copy
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
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import TraceNode, stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer


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


def labels(
    *,
    roots: tuple[str, ...] = ("semantic_anchor:v1:root",),
    conditions: tuple[str, ...] = (),
    amplifiers: tuple[str, ...] = (),
) -> dict:
    def entries(values, role):
        return [
            {
                "node_ref": "record:{0}_{1}".format(role, index),
                "semantic_anchor_id": value,
            }
            for index, value in enumerate(values)
        ]

    return {
        "schema_version": "recursive-attribution-labels/v1",
        "case_id": "metric-case",
        "roots": entries(roots, "root"),
        "conditions": entries(conditions, "condition"),
        "amplifiers": entries(amplifiers, "amplifier"),
        "forbidden_roots": [],
        "allowed_unresolved_outcomes": ["inconclusive", "partial_root_found"],
    }


def root_item(
    anchor: str = "semantic_anchor:v1:root",
    *,
    node_ref: str = "record:root",
    path: tuple[str, ...] = ("record:root", "record:observed"),
    evidence: tuple[str, ...] = ("record:root",),
    confirmation_identity: str = "confirmation:root",
) -> dict:
    confirmation = {
        "confirmation_identity": confirmation_identity,
        "candidate_ref": node_ref,
        "status": "confirmed",
        "evidence_refs": list(evidence),
        "recursive_path": list(path),
    }
    return {
        "node_ref": node_ref,
        "semantic_anchor_id": anchor,
        "recursive_path": list(path),
        "evidence_refs": list(evidence),
        "confirmation_status": "confirmed",
        "confirmation": confirmation,
    }


def base_report() -> dict:
    root = root_item()
    return {
        "schema_version": "recursive-attribution-report/v2",
        "case_id": "metric-case",
        "analysis_outcome": "root_found",
        "start_refs": ["record:observed"],
        "causal_candidates": [
            {
                "ref": "record:root",
                "node": {"ref": "record:root"},
                "semantic_anchor_id": "semantic_anchor:v1:root",
            },
            {
                "ref": "record:observed",
                "node": {"ref": "record:observed"},
                "semantic_anchor_id": "semantic_anchor:v1:observed",
            },
        ],
        "step_judgments": [
            {
                "current_node_ref": "record:root",
                "current_defect_status": "present",
                "predecessors": [],
            },
            {
                "current_node_ref": "record:observed",
                "current_defect_status": "present",
                "predecessors": [],
            },
        ],
        "introduction_candidates": [],
        "confirmed_roots": [root],
        "co_roots": [],
        "contributing_conditions": [],
        "amplifying_factors": [],
        "confirmations": [copy.deepcopy(root["confirmation"])],
        "unresolved_hypotheses": [],
        "unresolved_refs": [],
        "investigation_journal": [],
        "metadata": {
            "judge_request_count": 4,
            "logical_judge_call_count": 5,
            "checkpoint_reused_judgment_count": 1,
            "fabricated_refs": [],
            "unresolved_branches": [],
            "exhausted_budgets": {},
        },
    }


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
        trace["case_id"], graph.nodes, report.to_dict()
    )
    return annotated, human_labels, judge


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
        self.assertTrue(semantic_anchor_id("sphinx-case", left).startswith("semantic_anchor:v1:"))

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
    def test_metrics_have_exact_schema_and_expected_values(self):
        report = base_report()
        report["contributing_conditions"] = [
            {
                "node_ref": "record:condition",
                "semantic_anchor_id": "semantic_anchor:v1:condition",
            }
        ]
        report["amplifying_factors"] = [
            {
                "node_ref": "record:amplifier",
                "semantic_anchor_id": "semantic_anchor:v1:amplifier",
            }
        ]
        report["causal_candidates"].extend(
            [
                {
                    "ref": "record:condition",
                    "node": {"ref": "record:condition"},
                    "semantic_anchor_id": "semantic_anchor:v1:condition",
                },
                {
                    "ref": "record:amplifier",
                    "node": {"ref": "record:amplifier"},
                    "semantic_anchor_id": "semantic_anchor:v1:amplifier",
                },
            ]
        )
        report["confirmations"].append(
            {
                "confirmation_identity": "confirmation:rejected",
                "candidate_ref": "record:condition",
                "status": "rejected",
                "evidence_refs": ["record:condition"],
                "recursive_path": ["record:condition", "record:observed"],
            }
        )
        report["step_judgments"].append(
            {
                "current_node_ref": "record:condition",
                "current_defect_status": "unknown",
                "predecessors": [],
            }
        )
        report["investigation_journal"] = [
            {
                "status": "success",
                "result": {"status": "success", "evidence_hash": "sha256:new"},
                "context_before_hash": "sha256:before",
                "context_after_hash": "sha256:after",
                "rejudge_linkage": {"status": "completed"},
            }
        ]
        legacy = {"metadata": {"judge_request_count": 10}}

        result = compare_report(
            report,
            labels(
                conditions=("semantic_anchor:v1:condition",),
                amplifiers=("semantic_anchor:v1:amplifier",),
            ),
            legacy,
        )

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
                "candidate_recall",
                "top1_match",
                "judge_request_reduction",
                "mean_causal_path_length",
                "factor_role_precision",
                "unknown_rate",
                "confirmation_rejection_rate",
                "investigation_yield",
                "checkpoint_reuse_rate",
                "human_llm_disagreement_rate",
            },
        )
        self.assertEqual(result["metrics"]["candidate_recall"], 1.0)
        self.assertTrue(result["metrics"]["top1_match"])
        self.assertEqual(result["metrics"]["judge_request_reduction"], 0.6)
        self.assertEqual(result["metrics"]["mean_causal_path_length"], 2.0)
        self.assertEqual(result["metrics"]["factor_role_precision"], 1.0)
        self.assertEqual(result["metrics"]["unknown_rate"], 0.2)
        self.assertEqual(result["metrics"]["confirmation_rejection_rate"], 0.5)
        self.assertEqual(result["metrics"]["investigation_yield"], 1.0)
        self.assertEqual(result["metrics"]["checkpoint_reuse_rate"], 0.2)
        self.assertEqual(result["metrics"]["human_llm_disagreement_rate"], 0.0)

    def test_empty_denominators_are_explicit_and_success_control_scores_cleanly(self):
        report = base_report()
        report.update(
            {
                "analysis_outcome": "no_defect",
                "confirmed_roots": [],
                "confirmations": [],
                "step_judgments": [],
            }
        )
        report["metadata"].update(
            {
                "judge_request_count": 0,
                "logical_judge_call_count": 0,
                "checkpoint_reused_judgment_count": 0,
            }
        )
        result = compare_report(report, labels(roots=()), None)
        self.assertEqual(result["metrics"]["candidate_recall"], 1.0)
        self.assertTrue(result["metrics"]["top1_match"])
        self.assertIsNone(result["metrics"]["judge_request_reduction"])
        self.assertEqual(result["metrics"]["mean_causal_path_length"], 0.0)
        self.assertEqual(result["metrics"]["factor_role_precision"], 1.0)
        self.assertEqual(result["metrics"]["unknown_rate"], 0.0)
        self.assertEqual(result["metrics"]["confirmation_rejection_rate"], 0.0)
        self.assertEqual(result["metrics"]["investigation_yield"], 0.0)
        self.assertEqual(result["metrics"]["checkpoint_reuse_rate"], 0.0)

    def test_missing_current_request_measurement_does_not_invent_reduction(self):
        report = base_report()
        report["metadata"].pop("judge_request_count")

        result = compare_report(
            report,
            labels(),
            {"metadata": {"judge_request_count": 10}},
        )

        self.assertIsNone(result["counts"]["judge_request_count"])
        self.assertEqual(result["counts"]["legacy_judge_request_count"], 10)
        self.assertIsNone(result["metrics"]["judge_request_reduction"])

    def test_fabricated_or_unresolved_confirmed_evidence_fails_closed(self):
        cases = []
        fabricated = base_report()
        fabricated["metadata"]["fabricated_refs"] = ["record:invented"]
        cases.append(fabricated)

        unresolved = base_report()
        unresolved["unresolved_refs"] = ["record:root"]
        cases.append(unresolved)

        missing_evidence = base_report()
        missing_evidence["confirmed_roots"][0]["evidence_refs"] = ["record:missing"]
        missing_evidence["confirmations"][0]["evidence_refs"] = ["record:missing"]
        cases.append(missing_evidence)

        missing_confirmation = base_report()
        missing_confirmation["confirmations"] = []
        cases.append(missing_confirmation)

        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(EvaluationSafetyError):
                    compare_report(report, labels(), None)

    def test_grounded_artifact_evidence_resolves_without_weakening_missing_ref_checks(self):
        report = base_report()
        report["confirmed_roots"][0]["evidence_refs"] = ["artifact:decision-proof"]
        report["confirmed_roots"][0]["confirmation"]["evidence_refs"] = [
            "artifact:decision-proof"
        ]
        report["confirmations"][0]["evidence_refs"] = ["artifact:decision-proof"]
        report["causal_candidates"][0]["node"]["data"] = {
            "hydrated_artifacts": [
                {
                    "artifact_id": "decision-proof",
                    "content_hash": "sha256:" + "a" * 64,
                    "missing": False,
                }
            ]
        }

        result = compare_report(report, labels(), None)
        self.assertTrue(result["safety"]["passed"])

    def test_duplicate_semantic_identity_and_budget_promoted_root_fail_closed(self):
        duplicate = base_report()
        duplicate_root = root_item(
            node_ref="record:other-root",
            confirmation_identity="confirmation:other",
        )
        duplicate["co_roots"] = [duplicate_root]
        duplicate["confirmations"].append(copy.deepcopy(duplicate_root["confirmation"]))
        duplicate["causal_candidates"].append(
            {
                "ref": "record:other-root",
                "node": {"ref": "record:other-root"},
                "semantic_anchor_id": "semantic_anchor:v1:root",
            }
        )
        with self.assertRaises(EvaluationSafetyError):
            compare_report(duplicate, labels(), None)

        exhausted = base_report()
        exhausted["metadata"]["exhausted_budgets"] = {"judge_requests": 1}
        exhausted["metadata"]["unresolved_branches"] = [
            {"node_ref": "record:root", "reason": "judge_request_limit"}
        ]
        with self.assertRaises(EvaluationSafetyError):
            compare_report(exhausted, labels(), None)

    def test_projection_collision_and_cli_failure_exit_are_hard_failures(self):
        collision = base_report()
        collision["metadata"]["semantic_anchor_collisions"] = [
            {
                "semantic_anchor_id": "semantic_anchor:v1:collision",
                "node_refs": ["record:a", "record:b"],
            }
        ]
        with self.assertRaises(EvaluationSafetyError):
            compare_report(collision, labels(), None)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            report_path = root / "report.json"
            labels_path = root / "labels.json"
            output_path = root / "comparison.json"
            unsafe = base_report()
            unsafe["metadata"]["fabricated_refs"] = ["record:invented"]
            report_path.write_text(json.dumps(unsafe), encoding="utf-8")
            labels_path.write_text(json.dumps(labels()), encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                status = evaluate_main(
                    [
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

    def test_fixture_labels_use_computed_versioned_semantic_anchors(self):
        for name in FIXTURE_NAMES:
            trace, human_labels, _ = load_fixture(FIXTURE_ROOT / name)
            nodes = {
                "record:{0}".format(item["record_id"]): TraceNode(
                    ref="record:{0}".format(item["record_id"]),
                    record_id=item["record_id"],
                    component=item.get("component", ""),
                    event_type=item.get("event_type", ""),
                    title=item.get("title", ""),
                    status=item.get("status", ""),
                    timestamp=item.get("timestamp", ""),
                    data=item.get("data", {}),
                    source_refs=item.get("source_refs", []),
                )
                for item in trace["records"]
            }
            for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
                for item in human_labels[role]:
                    self.assertEqual(
                        item["semantic_anchor_id"],
                        semantic_anchor_id(trace["case_id"], nodes[item["node_ref"]]),
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
                        "human_labels": labels(),
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
        original = base_report()
        original["case_id"] = trace["case_id"]
        original["confirmed_roots"][0]["node_ref"] = "record:decision"
        original["confirmed_roots"][0]["confirmation"]["candidate_ref"] = "record:decision"
        original["confirmations"][0]["candidate_ref"] = "record:decision"
        original["causal_candidates"][0]["ref"] = "record:decision"
        original["causal_candidates"][0]["node"]["ref"] = "record:decision"
        before = copy.deepcopy(original)
        nodes_before = copy.deepcopy(graph.nodes)

        projected = annotate_report_semantic_anchors(
            trace["case_id"], graph.nodes, original
        )

        self.assertEqual(original, before)
        self.assertEqual(graph.nodes, nodes_before)
        self.assertEqual(
            projected["confirmed_roots"][0]["semantic_anchor_id"],
            semantic_anchor_id(trace["case_id"], graph.nodes["record:decision"]),
        )
        self.assertEqual(
            projected["metadata"]["semantic_anchor_schema_version"],
            "semantic-anchor/v1",
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

    def test_scripted_fixture_matrix_preserves_distinct_causal_roles(self):
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
                comparison = compare_report(report, human_labels, None)
                self.assertEqual(comparison["metrics"]["candidate_recall"], 1.0)
                self.assertTrue(comparison["metrics"]["top1_match"])
                self.assertEqual(comparison["metrics"]["factor_role_precision"], 1.0)
                request_text = stable_json(judge.requests)
                for role in ("roots", "conditions", "amplifiers", "forbidden_roots"):
                    for item in human_labels[role]:
                        self.assertNotIn(item["semantic_anchor_id"], request_text)
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
