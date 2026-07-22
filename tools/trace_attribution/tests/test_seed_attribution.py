from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

from trace_attribution.causal_judge import OfflineJudgeCapability
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    RecursiveAttributionReport,
    RootConfirmation,
    SeedAttributionResult,
)
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from trace_attribution.recursive_analyzer import (
    RecursiveAnalysisState,
    SeedAttributionBuilder,
    _provider_state_payload,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "recursive_cases"


class MultiSeedJudge(OfflineJudgeCapability):
    def judge_step_offline(self, request):
        ref = request.current_node.ref
        if ref == "record:claim_tests":
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="present",
                current_defect_reason="The test claim requires independent confirmation.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "candidate_ref": ref,
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Confirm the unsupported test claim.",
                },
                confidence=0.9,
            )
        if ref == "record:claim_change":
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="absent",
                current_defect_reason="The trace grounds the completion claim.",
                predecessors=(),
                candidate_introduction=False,
                confidence=1.0,
            )
        return CausalStepJudgment(
            current_node_ref=ref,
            current_defect_status="unknown",
            current_defect_reason="The fragment is truncated.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=("complete verification output",),
            confidence=0.0,
        )

    def confirm_candidate_offline(self, request):
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="All focused tests passed.",
            reason="The unsupported test claim is the confirmed local root.",
            counterfactual="Reporting the observed test result avoids the unsupported claim.",
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )


class FailingFragmentJudge(MultiSeedJudge):
    def judge_step_offline(self, request):
        if request.current_node.ref == "record:claim_fragment":
            raise RuntimeError("seed-local scripted failure")
        return super().judge_step_offline(request)


def run_fixture(name: str):
    document = json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    script = document.pop("scripted_analysis")
    return AgenticRecursiveAnalyzer(judge=MultiSeedJudge()).analyze(
        TraceGraph.from_trace(document),
        start_refs=script["start_refs"],
        objective="Confirm each response claim independently.",
    )


def defect(label: str) -> DefectState:
    return DefectState.create(
        label=label,
        expected="Expected {0}.".format(label),
        actual="Observed {0}.".format(label),
        mechanism="Seed-local defect state.",
        scope="seed-attribution-test",
    )


def seed_result(ref: str, outcome: str) -> SeedAttributionResult:
    state = defect(ref)
    return SeedAttributionResult(
        start_ref=ref,
        defect_fingerprint=state.fingerprint,
        defect_state=state,
        outcome=outcome,
        confirmed_root_refs=("record:root-{0}".format(ref),)
        if outcome == "confirmed_root"
        else (),
        missing_evidence=("missing {0}".format(ref),)
        if outcome == "evidence_gap"
        else (),
    )


class SeedAttributionIntegrationTests(unittest.TestCase):
    def test_report_preserves_independent_seed_outcomes(self):
        report = run_fixture("multi_seed_claims.json")
        by_ref = {item.start_ref: item for item in report.seed_results}

        self.assertEqual(by_ref["record:claim_tests"].outcome, "confirmed_root")
        self.assertEqual(by_ref["record:claim_change"].outcome, "no_defect")
        self.assertEqual(by_ref["record:claim_fragment"].outcome, "evidence_gap")
        self.assertEqual(report.analysis_outcome, "partial")

    def test_one_seed_failure_does_not_erase_completed_seeds(self):
        document = json.loads(
            (FIXTURE_ROOT / "multi_seed_claims.json").read_text(encoding="utf-8")
        )
        script = document.pop("scripted_analysis")
        report = AgenticRecursiveAnalyzer(judge=FailingFragmentJudge()).analyze(
            TraceGraph.from_trace(document),
            start_refs=script["start_refs"],
            objective="Confirm each response claim independently.",
        )
        by_ref = {item.start_ref: item for item in report.seed_results}

        self.assertEqual(by_ref["record:claim_tests"].outcome, "confirmed_root")
        self.assertEqual(by_ref["record:claim_change"].outcome, "no_defect")
        self.assertEqual(by_ref["record:claim_fragment"].outcome, "evidence_gap")
        self.assertIn(
            "judge_error",
            by_ref["record:claim_fragment"].blocking_reasons,
        )


class SeedAttributionModelTests(unittest.TestCase):
    def test_no_defect_does_not_clear_an_existing_seed_failure(self):
        builder = SeedAttributionBuilder(
            start_ref="record:seed",
            defect_state=defect("branch-failure"),
        )
        builder.mark_unresolved("judge_error", "One branch failed.")
        builder.mark_no_defect()

        result = builder.to_result()
        self.assertEqual(result.outcome, "evidence_gap")
        self.assertEqual(result.blocking_reasons, ("judge_error",))

    def test_seed_result_is_deeply_immutable_and_round_trips_defaults(self):
        state = defect("immutable")
        result = SeedAttributionResult(
            start_ref="record:seed",
            defect_fingerprint=state.fingerprint,
            defect_state=state,
            outcome="evidence_gap",
            candidate_refs=["record:z", "record:a"],
            global_judgment={"nested": [{"status": "unknown"}]},
            expansion_history=[{"anchor_ref": "record:a", "refs": ["record:b"]}],
        )

        with self.assertRaises(FrozenInstanceError):
            result.outcome = "no_defect"
        with self.assertRaises(TypeError):
            result.global_judgment["nested"] = ()
        self.assertIsInstance(result.global_judgment["nested"], tuple)
        self.assertIsInstance(result.global_judgment["nested"][0], Mapping)
        self.assertEqual(result.candidate_refs, ("record:a", "record:z"))

        payload = result.to_dict()
        self.assertEqual(SeedAttributionResult.from_dict(payload).to_dict(), payload)
        self.assertEqual(
            set(payload),
            {
                "start_ref",
                "defect_fingerprint",
                "defect_state",
                "outcome",
                "candidate_refs",
                "selected_candidate_refs",
                "confirmation_identities",
                "confirmed_root_refs",
                "decisive_evidence_refs",
                "missing_evidence",
                "blocking_reasons",
                "global_judgment",
                "expansion_history",
            },
        )

    def test_report_v3_round_trip_is_complete_and_v2_migration_is_conservative(self):
        report = run_fixture("multi_seed_claims.json")
        payload = report.to_dict()

        self.assertEqual(payload["schema_version"], "recursive-attribution-report/v3")
        self.assertEqual(RecursiveAttributionReport.from_dict(payload).to_dict(), payload)

        v2_payload = json.loads(json.dumps(payload))
        v2_payload["schema_version"] = "recursive-attribution-report/v2"
        v2_payload.pop("seed_results")
        migrated = RecursiveAttributionReport.from_dict(v2_payload)

        self.assertEqual(migrated.schema_version, "recursive-attribution-report/v3")
        self.assertEqual(
            [item.start_ref for item in migrated.seed_results],
            sorted(v2_payload["start_refs"]),
        )
        self.assertEqual(
            {item.outcome for item in migrated.seed_results},
            {"inconclusive"},
        )
        self.assertTrue(migrated.confirmed_roots)
        self.assertTrue(
            all(not item.confirmed_root_refs for item in migrated.seed_results)
        )
        self.assertEqual(migrated.analysis_outcome, "inconclusive")

    def test_top_level_aggregation_uses_only_seed_outcomes(self):
        cases = {
            "all_no_defect": (
                (seed_result("a", "no_defect"), seed_result("b", "no_defect")),
                "no_defect",
            ),
            "confirmed_and_no_defect": (
                (
                    seed_result("a", "confirmed_root"),
                    seed_result("b", "no_defect"),
                ),
                "confirmed_root",
            ),
            "partial": (
                (
                    seed_result("a", "confirmed_root"),
                    seed_result("b", "no_defect"),
                    seed_result("c", "evidence_gap"),
                ),
                "partial",
            ),
            "unresolved": (
                (
                    seed_result("a", "confirmed_root"),
                    seed_result("b", "inconclusive"),
                ),
                "inconclusive",
            ),
        }
        for name, (seed_results, expected) in cases.items():
            with self.subTest(name=name):
                report = RecursiveAttributionReport(
                    case_id="aggregation",
                    objective="Aggregate seed outcomes.",
                    seed_results=seed_results,
                )
                self.assertEqual(report.analysis_outcome, expected)

    def test_output_order_is_deterministic(self):
        seeds = (
            seed_result("record:z", "evidence_gap"),
            seed_result("record:a", "no_defect"),
            seed_result("record:m", "confirmed_root"),
        )
        forward = RecursiveAttributionReport(
            case_id="order",
            objective="Stable output.",
            start_refs=[item.start_ref for item in seeds],
            seed_results=seeds,
        )
        reverse = RecursiveAttributionReport(
            case_id="order",
            objective="Stable output.",
            start_refs=[item.start_ref for item in reversed(seeds)],
            seed_results=tuple(reversed(seeds)),
        )

        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertEqual(
            [item["start_ref"] for item in forward.to_dict()["seed_results"]],
            ["record:a", "record:m", "record:z"],
        )

    def test_retrieval_scores_do_not_enter_seed_confirmation_data(self):
        result = seed_result("record:seed", "confirmed_root")
        payload = result.to_dict()

        self.assertNotIn("score", stable_json(payload))


class SeedAttributionCheckpointTests(unittest.TestCase):
    def test_checkpoint_preserves_duplicate_ref_with_distinct_fingerprints(self):
        trace = {
            "case_id": "duplicate-seed-checkpoint",
            "records": [
                {
                    "record_id": "same",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "A shared start reference."},
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:same",),
            objective="Preserve both defects.",
            analysis_perspective="",
        )
        first = state._ensure_seed("record:same", defect("first"))
        second = state._ensure_seed("record:same", defect("second"))
        first.mark_no_defect()
        second.mark_unresolved("missing_second_evidence", "Second seed failed.")

        self.assertEqual(len(state.seed_ledger), 2)
        self.assertNotEqual(first.key, second.key)

        config = build_checkpoint_config(
            trace=trace,
            case_id="duplicate-seed-checkpoint",
            objective=state.objective,
            analysis_perspective="",
            start_refs=state.start_refs,
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:test",
            cache_identity="cache:test",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://test",
                "provider_error_threshold": 3,
            },
        )
        state.provider_state = _provider_state_payload(
            MultiSeedJudge(), state, cache_identity="cache:test"
        )
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="seed-ledger",
                frontier_payload=state.frontier_checkpoint_payload(),
                hypothesis_payload=state.hypothesis_checkpoint_payload(),
                action_payload=state.action_checkpoint_payload(),
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=bundle.restore(expected_config=config),
            )

        self.assertEqual(
            sorted(restored.seed_ledger),
            sorted(state.seed_ledger),
        )
        self.assertEqual(
            [item.to_dict() for item in restored.seed_results()],
            [item.to_dict() for item in state.seed_results()],
        )


if __name__ == "__main__":
    unittest.main()
