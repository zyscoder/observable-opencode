from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools.trace_attribution.tests.test_recursive_benchmarks import (
    FixtureJudge,
)
from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_state import annotate_report_semantic_anchors
from trace_attribution.checkpoint import CheckpointBundle, build_checkpoint_config
from trace_attribution.graph import TraceGraph
from trace_attribution.label_contract import validate_labels
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from trace_attribution.seed_projection import score_seed_owned_report


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "recursive_cases"
    / "multi_seed_bound_v5.json"
)
OBJECTIVE = "Identify the independent cause of each observed implementation defect."
PERSPECTIVE = "Improve Harness reasoning quality without exposing evaluation labels."
FUSION_MODE = "retrieval-global"
_BUDGETS = {
    "max_frontier_items": 96,
    "max_depth": 20,
    "max_hypotheses": 24,
    "max_investigation_rounds": 12,
    "max_artifact_bytes": 1_048_576,
    "max_judge_requests": 128,
}


def _request_payloads(judge: FixtureJudge) -> dict:
    def encode(values):
        return [
            value.to_dict() if hasattr(value, "to_dict") else value
            for value in values
        ]

    return {
        "step": encode(judge.requests),
        "confirmation": encode(judge.confirmation_requests),
        "factor": encode(judge.factor_requests),
        "global": encode(judge.global_requests),
    }


def _run_fixture(checkpoint_root: Path | None = None):
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    labels = validate_labels(document.pop("human_labels"))
    script = document.pop("scripted_analysis")
    trace = document
    graph = TraceGraph.from_trace(trace)
    judge = FixtureJudge(script)
    checkpoint = None
    checkpoint_config = None
    if checkpoint_root is not None:
        checkpoint = CheckpointBundle(checkpoint_root)
        checkpoint_config = build_checkpoint_config(
            trace=trace,
            case_id=trace["case_id"],
            objective=OBJECTIVE,
            analysis_perspective=PERSPECTIVE,
            start_refs=script["start_refs"],
            budgets=_BUDGETS,
            model_identity="offline:multi-seed-bound-v5",
            cache_identity="cache:multi-seed-bound-v5",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 8192,
                "thinking_mode": "disabled",
                "base_url": "offline://fixture",
                "provider_error_threshold": 3,
                "fusion_mode": FUSION_MODE,
            },
        )
    report = AgenticRecursiveAnalyzer(
        judge=judge,
        checkpoint=checkpoint,
        checkpoint_config=checkpoint_config,
        fusion_mode=FUSION_MODE,
    ).analyze(
        graph,
        start_refs=script["start_refs"],
        objective=OBJECTIVE,
        analysis_perspective=PERSPECTIVE,
    )
    annotated = annotate_report_semantic_anchors(
        trace["case_id"], graph.nodes, report.to_dict(), graph=graph
    )
    return trace, labels, annotated, judge


def _score(report: dict, labels: dict) -> dict:
    return score_seed_owned_report(
        report,
        labels,
        bound_seed_binding_identities=[
            seed["seed_binding_identity"] for seed in labels["seeds"]
        ],
        binding_safety_passed=True,
        checkpoint_safety_passed=True,
    ).to_dict()


def _comparison_bytes(report: dict, labels: dict) -> bytes:
    return json.dumps(
        _score(report, labels),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _swap_root_ownership(report: dict, labels: dict) -> dict:
    swapped = copy.deepcopy(report)
    identities = {
        seed["defect_id"]: seed["seed_binding_identity"]
        for seed in labels["seeds"]
    }
    owner_by_ref = {
        "record:agent_scope_decision": identities["agent-introduced-scope-loss"],
        "record:skip_baseline_repair": identities["baseline-existing-unrepaired"],
    }
    opposite = {
        identities["agent-introduced-scope-loss"]: identities[
            "baseline-existing-unrepaired"
        ],
        identities["baseline-existing-unrepaired"]: identities[
            "agent-introduced-scope-loss"
        ],
    }
    for publication in [*swapped["confirmed_roots"], *swapped["co_roots"]]:
        owner = owner_by_ref.get(publication["node_ref"])
        if owner is None:
            continue
        publication["confirmation"]["seed_binding_identity"] = opposite[owner]
        if "provenance" in publication:
            publication["provenance"]["seed_binding_identity"] = opposite[owner]
    return swapped


def _regular_file_text(root: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )


class MultiSeedBoundV5AcceptanceTest(unittest.TestCase):
    def test_two_origin_kinds_shared_amplifier_and_correct_ownership_score(self):
        _trace, labels, report, _judge = _run_fixture()

        self.assertEqual(
            {seed["defect_origin_kind"] for seed in labels["seeds"]},
            {"introduced_by_agent", "baseline_existing_unrepaired"},
        )
        self.assertEqual(len(labels["case_shared_factors"]), 1)
        shared = labels["case_shared_factors"][0]
        self.assertEqual(shared["factor_role"], "amplifying_factor")
        self.assertEqual(len(shared["seed_binding_identities"]), 2)

        comparison = _score(report, labels)

        self.assertTrue(comparison["all_seed_coverage_passed"])
        self.assertEqual(comparison["micro"]["root_metrics"]["f1"], 1.0)
        self.assertEqual(comparison["micro"]["role_pair_metrics"]["f1"], 1.0)
        self.assertTrue(
            all(item["top1_match"] is True for item in comparison["per_seed"])
        )
        self.assertTrue(
            all(item["outcome_agreement"] for item in comparison["per_seed"])
        )

    def test_swapping_root_ownership_fails_each_seed(self):
        _trace, labels, report, _judge = _run_fixture()

        comparison = _score(_swap_root_ownership(report, labels), labels)

        self.assertFalse(comparison["all_seed_coverage_passed"])
        for seed in comparison["per_seed"]:
            self.assertEqual(seed["root_metrics"]["f1"], 0.0)
            self.assertFalse(seed["top1_match"])

    def test_repeated_execution_has_byte_stable_comparison(self):
        _trace, first_labels, first_report, _first_judge = _run_fixture()
        _trace, second_labels, second_report, _second_judge = _run_fixture()

        self.assertEqual(
            _comparison_bytes(first_report, first_labels),
            _comparison_bytes(second_report, second_labels),
        )

    def test_labels_never_enter_report_judge_cache_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            checkpoint_root = root / "checkpoint"
            _trace, labels, report, judge = _run_fixture(checkpoint_root)
            requests = _request_payloads(judge)
            cache_path = root / "judge-cache.jsonl"
            cache = JudgmentCache(cache_path)
            for index, judgment in enumerate(report["confirmations"]):
                cache.put_payload(
                    key="fixture-confirmation-{0}".format(index),
                    stage="root-confirmation",
                    model="offline:fixture",
                    node_ref=str(judgment.get("candidate_ref") or ""),
                    payload=judgment,
                )
            runtime_documents = {
                "report": stable_json(report),
                "judge": stable_json(requests),
                "cache": cache_path.read_text(encoding="utf-8"),
                "checkpoint": _regular_file_text(checkpoint_root),
            }

        label_only_keys = {
            "human_labels",
            "source_binding",
            "seed_source_bindings",
            "defect_origin_kind",
            "case_shared_factors",
        }
        label_only_values = {
            seed["defect_id"] for seed in labels["seeds"]
        } | {seed["defect_origin_kind"] for seed in labels["seeds"]}
        complete_labels = stable_json(labels)
        for name, text in runtime_documents.items():
            with self.subTest(runtime_document=name):
                self.assertNotIn(complete_labels, text)
                for key in label_only_keys:
                    self.assertNotIn('"{0}"'.format(key), text)
                for value in label_only_values:
                    self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
