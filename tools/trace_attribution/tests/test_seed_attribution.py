from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    GlobalJudgeCapability,
    OfflineJudgeCapability,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    DefectState,
    FrontierItem,
    FrozenMapping,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RootConfirmation,
    SeedAttributionResult,
    annotate_report_semantic_anchors,
    semantic_anchor_index,
    semantic_occurrence_index,
)
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
)
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from trace_attribution.recursive_analyzer import (
    RecursiveAnalysisState,
    SeedAttributionBuilder,
    _provider_state_payload,
)
from scripts.evaluate_recursive_attribution import (
    EvaluationSchemaError,
    _validate_report_shape,
    compare_report,
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


class TransformingRootJudge(OfflineJudgeCapability):
    def __init__(self) -> None:
        self.confirmation_requests = []

    def judge_step_offline(self, request):
        if request.current_node.ref == "record:seed":
            upstream = request.defect_state.transformed(
                label="incomplete_repository_discovery",
                mechanism="the decision stopped before inspecting every implementation",
                transformation_reason="the incomplete discovery decision produced the downstream omission",
            )
            return CausalStepJudgment(
                current_node_ref="record:seed",
                current_defect_status="present",
                current_defect_reason="The observed omission propagates from an earlier discovery decision.",
                predecessors=(
                    PredecessorAssessment(
                        ref="record:root",
                        relation="defect_transformation",
                        reason="The decision transformed into the downstream omitted implementation.",
                        confidence=0.9,
                        recurse=True,
                        upstream_defect=upstream,
                        evidence_refs=("record:root", "record:seed"),
                    ),
                ),
                candidate_introduction=False,
                confidence=0.9,
            )
        if request.current_node.ref == "record:root":
            return CausalStepJudgment(
                current_node_ref="record:root",
                current_defect_status="present",
                current_defect_reason="The recorded decision stopped discovery before every implementation was inspected.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "candidate_ref": "record:root",
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Independently confirm the transformed defect root.",
                },
                confidence=0.9,
            )
        raise AssertionError("unexpected node: {0}".format(request.current_node.ref))

    def confirm_candidate_offline(self, request):
        self.confirmation_requests.append(request)
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="Search only the first matching implementation.",
            reason="The decision independently introduced the incomplete discovery defect.",
            counterfactual="Inspecting every implementation prevents the downstream omission.",
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )


class TwoHopSharedRootJudge(OfflineJudgeCapability):
    def __init__(self) -> None:
        self.step_requests = []
        self.confirmation_requests = []

    def judge_step_offline(self, request):
        self.step_requests.append(request)
        ref = request.current_node.ref
        if ref in {"record:seed_one", "record:seed_two"}:
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="present",
                current_defect_reason="The observed claim propagates the shared unsupported decision.",
                predecessors=(
                    PredecessorAssessment(
                        ref="record:shared_middle",
                        relation="same_defect_propagation",
                        reason="The shared intermediate decision propagated the same unsupported claim.",
                        confidence=0.9,
                        recurse=True,
                        evidence_refs=("record:shared_middle", ref),
                    ),
                ),
                candidate_introduction=False,
                confidence=0.9,
            )
        if ref == "record:shared_middle":
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="present",
                current_defect_reason="The shared intermediate decision propagates the root defect.",
                predecessors=(
                    PredecessorAssessment(
                        ref="record:shared_root",
                        relation="same_defect_propagation",
                        reason="The shared root decision introduced the unsupported claim.",
                        confidence=0.9,
                        recurse=True,
                        evidence_refs=("record:shared_root", ref),
                    ),
                ),
                candidate_introduction=False,
                confidence=0.9,
            )
        if ref == "record:shared_root":
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="present",
                current_defect_reason="The shared root decision introduced the unsupported claim.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "candidate_ref": ref,
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Independently confirm the shared root for this seed.",
                },
                confidence=0.9,
            )
        raise AssertionError("unexpected node: {0}".format(ref))

    def confirm_candidate_offline(self, request):
        self.confirmation_requests.append(request)
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The root decision introduced an unsupported claim.",
            reason="The shared root independently introduced this seed's defect.",
            counterfactual="Grounding the root decision prevents this unsupported claim.",
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )


class SharedRootFusionJudge(OfflineJudgeCapability, GlobalJudgeCapability):
    def __init__(self) -> None:
        self.global_requests = []
        self.confirmation_requests = []
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.consecutive_provider_errors = 0
        self.provider_error_threshold = 3

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status=(
                    "present" if capsule.candidate_ref == "record:shared_root" else "absent"
                ),
                causal_role=(
                    "root_candidate"
                    if capsule.candidate_ref == "record:shared_root"
                    else "exculpatory_evidence"
                ),
                reason="The shared decision is the trace-visible defect source.",
                evidence_refs=(capsule.candidate_ref,),
                confidence=0.9,
            )
            for capsule in request.capsules
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="candidate_roots",
                reason="Both seeds independently select the shared root.",
                assessments=assessments,
                selected_candidate_refs=("record:shared_root",),
                expansion_requests=(),
                decisive_evidence_refs=("record:shared_root",),
                missing_evidence=(),
                confidence=0.9,
            ),
            0,
        )

    def judge_step_offline(self, request):
        raise AssertionError("global selection should supersede recursive traversal")

    def confirm_candidate_offline(self, request):
        self.confirmation_requests.append(request)
        result = RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The shared root decision omitted required evidence.",
            reason="The shared decision independently caused this seed's defect.",
            counterfactual="A grounded decision prevents the unsupported claim.",
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )
        return replace(
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
                    "status": "co_root",
                    "reason": "The same shared root independently binds both seeds.",
                    "evidence_refs": [request.candidate_ref],
                }
                for item in request.competing_hypotheses
            ),
        )


class NeedsExpansionSharedRootFusionJudge(SharedRootFusionJudge):
    def __init__(self) -> None:
        super().__init__()
        self.step_requests = []

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="needs_expansion",
                reason="The shared anchor needs an independent recursive check.",
                assessments=tuple(
                    GlobalCandidateAssessment(
                        candidate_ref=capsule.candidate_ref,
                        defect_status="unknown",
                        causal_role="unknown",
                        reason="Expansion is required before selecting a root.",
                        evidence_refs=(capsule.candidate_ref,),
                        confidence=0.5,
                    )
                    for capsule in request.capsules
                ),
                selected_candidate_refs=(),
                expansion_requests=(
                    {
                        "anchor_ref": "record:shared_root",
                        "context_kind": "upstream",
                        "reason": "Independently inspect the shared root.",
                    },
                ),
                decisive_evidence_refs=("record:shared_root",),
                missing_evidence=("Independent recursive inspection.",),
                confidence=0.5,
            ),
            0,
        )

    def judge_step_offline(self, request):
        self.step_requests.append(request)
        if request.current_node.ref != "record:shared_root":
            raise AssertionError("unexpected node: {0}".format(request.current_node.ref))
        return CausalStepJudgment(
            current_node_ref="record:shared_root",
            current_defect_status="present",
            current_defect_reason="The shared root introduced the unsupported claim.",
            predecessors=(),
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": "record:shared_root",
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Confirm the shared root for this seed.",
            },
            confidence=0.9,
        )


class DivergentSharedRootFusionJudge(SharedRootFusionJudge):
    def confirm_candidate_offline(self, request):
        self.confirmation_requests.append(request)
        if request.recursive_path[-1] == "record:seed_two":
            return RootConfirmation.unknown(
                request.candidate_ref,
                "The second seed lacks independent confirmation evidence.",
                evidence_refs=(request.candidate_ref,),
            )
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The shared root decision omitted required evidence.",
            reason="The shared decision independently caused the first seed's defect.",
            counterfactual="A grounded decision prevents the unsupported claim.",
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )


class CrashAfterFirstCompletedConfirmation(CheckpointBundle):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.crashed = False

    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == "confirmation_completed" and not self.crashed:
            self.crashed = True
            raise KeyboardInterrupt("crash after first durable confirmation")
        return record


def shared_root_trace():
    return {
        "case_id": "shared-root-seeds",
        "records": [
            {
                "record_id": "shared_root",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "rationale": "The shared root decision omitted required evidence."
                },
            },
            *(
                {
                    "record_id": seed,
                    "component": "assistant",
                    "event_type": "response.claim",
                    "data": {"text": "The same unsupported claim."},
                }
                for seed in ("seed_one", "seed_two")
            ),
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "shared_root"},
                "to": {"type": "record", "id": seed},
                "relation": "decision_guided_claim",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            }
            for seed in ("seed_one", "seed_two")
        ],
    }


def two_hop_shared_root_trace():
    return {
        "case_id": "two-hop-shared-root-seeds",
        "records": [
            {
                "record_id": "shared_root",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "The root decision introduced an unsupported claim."},
            },
            {
                "record_id": "shared_middle",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "The middle decision propagated the unsupported claim."},
            },
            *(
                {
                    "record_id": seed,
                    "component": "assistant",
                    "event_type": "response.claim",
                    "data": {"text": "The same unsupported claim."},
                }
                for seed in ("seed_one", "seed_two")
            ),
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "shared_root"},
                "to": {"type": "record", "id": "shared_middle"},
                "relation": "decision_guided_claim",
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            },
            *(
                {
                    "from": {"type": "record", "id": "shared_middle"},
                    "to": {"type": "record", "id": seed},
                    "relation": "decision_guided_claim",
                    "evidence_type": "confirmed",
                    "confidence": 0.9,
                    "eligible_for_attribution": True,
                }
                for seed in ("seed_one", "seed_two")
            ),
        ],
    }


def shared_root_checkpoint_config(trace, objective, start_refs):
    return build_checkpoint_config(
        trace=trace,
        case_id=trace["case_id"],
        objective=objective,
        analysis_perspective="",
        start_refs=start_refs,
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


def run_shared_root_report(trace=None):
    document = trace or shared_root_trace()
    return AgenticRecursiveAnalyzer(
        judge=SharedRootFusionJudge(),
        fusion_mode="retrieval-global",
    ).analyze(
        TraceGraph.from_trace(document),
        start_refs=("record:seed_one", "record:seed_two"),
        objective="Confirm each identical claim independently.",
        analysis_perspective="",
    )


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


def same_start_ref_sibling_mutation(
    report: RecursiveAttributionReport,
    *,
    sibling_outcome: str,
    sibling_matches_root_lineage: bool,
) -> dict:
    """Return a root-owner mutation with a distinct seed fingerprint at one ref."""
    root = report.confirmed_roots[0]
    owner = next(
        item for item in report.seed_results if item.outcome == "confirmed_root"
    )
    root_defect = owner.defect_state.transformed(
        label="published_root_lineage",
        mechanism="The published root carries a derived defect state.",
        transformation_reason="Exercise composite root ownership validation.",
    )
    confirmation = RootConfirmation.from_dict(dict(root.confirmation))
    root_confirmation = replace(
        confirmation,
        defect_fingerprint=root_defect.fingerprint,
    )
    updated_root = replace(
        root,
        defect_state=root_defect,
        confirmation=root_confirmation.to_dict(),
    )
    updated_owner = replace(
        owner,
        confirmation_identities=(root_confirmation.confirmation_identity,),
    )
    sibling_defect = (
        root_defect
        if sibling_matches_root_lineage
        else defect("same-start-ref-independent-sibling")
    )
    sibling = SeedAttributionResult(
        start_ref=owner.start_ref,
        defect_fingerprint=sibling_defect.fingerprint,
        defect_state=sibling_defect,
        outcome=sibling_outcome,
        missing_evidence=("sibling evidence is incomplete",)
        if sibling_outcome == "evidence_gap"
        else (),
    )
    return {
        "seed_results": tuple(
            updated_owner if item == owner else item
            for item in report.seed_results
        )
        + (sibling,),
        "confirmations": tuple(
            root_confirmation if item == confirmation else item
            for item in report.confirmations
        ),
        "confirmed_roots": tuple(
            updated_root if item == root else item for item in report.confirmed_roots
        ),
    }


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

    def test_two_hop_shared_root_preserves_seed_visits_and_resume_confirmations(self):
        trace = two_hop_shared_root_trace()
        graph = TraceGraph.from_trace(trace)
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Confirm each identical claim through the shared causal path."

        def analyze(judge, checkpoint=None, checkpoint_config=None):
            return AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=checkpoint,
                checkpoint_config=checkpoint_config,
            ).analyze(
                graph,
                start_refs=start_refs,
                objective=objective,
                analysis_perspective="",
            )

        uninterrupted_judge = TwoHopSharedRootJudge()
        uninterrupted = analyze(uninterrupted_judge)
        root_steps = [
            request
            for request in uninterrupted_judge.step_requests
            if request.current_node.ref == "record:shared_root"
        ]

        self.assertEqual(
            {item.defect_fingerprint for item in uninterrupted.seed_results},
            {uninterrupted.seed_results[0].defect_fingerprint},
        )
        self.assertEqual(len(root_steps), 2)
        self.assertEqual(len(uninterrupted.confirmations), 2)
        self.assertEqual(len(uninterrupted_judge.confirmation_requests), 2)
        self.assertEqual(
            len(
                {
                    request.seed_binding_identity
                    for request in uninterrupted_judge.confirmation_requests
                }
            ),
            2,
        )
        self.assertEqual(
            {item.outcome for item in uninterrupted.seed_results},
            {"confirmed_root"},
        )

        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "two-hop-shared-root.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                analyze(
                    TwoHopSharedRootJudge(),
                    CrashAfterFirstCompletedConfirmation(root),
                    config,
                )
            checkpoint = CheckpointBundle(root).restore(expected_config=config)
            restored_state = RecursiveAnalysisState.from_checkpoint(
                graph=graph, checkpoint=checkpoint
            )
            restored_items = [
                FrontierItem.from_dict(item)
                for item in restored_state.frontier.snapshot()
            ]
            for item in restored_items:
                hypothesis = restored_state.ledger.get(item.hypothesis_id)
                self.assertEqual(
                    restored_state.hypothesis_seed_keys[item.hypothesis_id],
                    hypothesis.seed_binding_identity,
                )
                self.assertEqual(
                    restored_state._seed_builder_for_item(item).key,
                    hypothesis.seed_binding_identity,
                )
            queued_hypotheses = [
                str(item["hypothesis_id"])
                for item in restored_state.confirmation_queue
            ]
            self.assertTrue(restored_items or queued_hypotheses)
            for hypothesis_id in queued_hypotheses:
                hypothesis = restored_state.ledger.get(hypothesis_id)
                self.assertEqual(
                    restored_state.hypothesis_seed_keys[hypothesis_id],
                    hypothesis.seed_binding_identity,
                )
            resumed_judge = TwoHopSharedRootJudge()
            resumed = analyze(resumed_judge, CheckpointBundle(root), config)

        self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
        self.assertEqual(len(resumed_judge.confirmation_requests), 1)
        self.assertEqual(
            [
                request.current_node.ref
                for request in resumed_judge.step_requests
                if request.current_node.ref == "record:shared_root"
            ],
            [],
        )

    def test_global_expansion_preserves_same_fingerprint_seed_frontiers_and_resume(self):
        trace = shared_root_trace()
        graph = TraceGraph.from_trace(trace)
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Independently confirm each seed through the shared expansion anchor."

        def analyze(judge, checkpoint=None, checkpoint_config=None):
            return AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=checkpoint,
                checkpoint_config=checkpoint_config,
                fusion_mode="retrieval-global",
            ).analyze(
                graph,
                start_refs=start_refs,
                objective=objective,
                analysis_perspective="",
            )

        uninterrupted_judge = NeedsExpansionSharedRootFusionJudge()
        uninterrupted = analyze(uninterrupted_judge)
        root_steps = [
            request
            for request in uninterrupted_judge.step_requests
            if request.current_node.ref == "record:shared_root"
        ]

        self.assertEqual(len(root_steps), 2)
        self.assertEqual(len(uninterrupted.confirmations), 2)
        self.assertEqual(
            len(
                {
                    request.seed_binding_identity
                    for request in uninterrupted_judge.confirmation_requests
                }
            ),
            2,
        )
        self.assertEqual(
            {item.outcome for item in uninterrupted.seed_results},
            {"confirmed_root"},
        )

        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "global-expansion.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                analyze(
                    NeedsExpansionSharedRootFusionJudge(),
                    CrashAfterFirstCompletedConfirmation(root),
                    config,
                )
            resumed_judge = NeedsExpansionSharedRootFusionJudge()
            resumed = analyze(resumed_judge, CheckpointBundle(root), config)

        self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
        self.assertEqual(len(resumed_judge.confirmation_requests), 1)

    def test_shared_root_same_fingerprint_keeps_seed_confirmations_independent_after_resume(self):
        trace = shared_root_trace()
        graph = TraceGraph.from_trace(trace)
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Confirm each identical claim independently."

        def analyze(judge, checkpoint=None, checkpoint_config=None):
            return AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=checkpoint,
                checkpoint_config=checkpoint_config,
                fusion_mode="retrieval-global",
            ).analyze(
                graph,
                start_refs=start_refs,
                objective=objective,
                analysis_perspective="",
            )

        uninterrupted_judge = SharedRootFusionJudge()
        uninterrupted = analyze(uninterrupted_judge)
        self.assertEqual(
            {item.defect_fingerprint for item in uninterrupted.seed_results},
            {uninterrupted.seed_results[0].defect_fingerprint},
        )
        self.assertEqual(len(uninterrupted.confirmations), 2)
        self.assertEqual(
            len({item.confirmation_identity for item in uninterrupted.confirmations}),
            2,
        )
        self.assertEqual(
            len(
                {
                    item.hypothesis_id
                    for item in uninterrupted.hypotheses
                    if item.candidate_root_ref == "record:shared_root"
                }
            ),
            2,
        )
        for key in (
            "introduction_bindings",
            "confirmation_queue",
            "confirmation_journal",
        ):
            values = uninterrupted.metadata[key]
            self.assertEqual(len(values), 2, key)
            self.assertEqual(len({item["seed_key"] for item in values}), 2, key)
        self.assertEqual(
            {item.outcome for item in uninterrupted.seed_results},
            {"confirmed_root"},
        )
        self.assertTrue(
            all(len(item.confirmation_identities) == 1 for item in uninterrupted.seed_results)
        )

        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "shared-root.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                analyze(
                    SharedRootFusionJudge(),
                    CrashAfterFirstCompletedConfirmation(root),
                    config,
                )
            resumed_judge = SharedRootFusionJudge()
            resumed = analyze(resumed_judge, CheckpointBundle(root), config)

        self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
        self.assertEqual(len(resumed_judge.confirmation_requests), 1)

    def test_same_fingerprint_different_seed_unknown_does_not_erase_confirmed_seed_after_resume(self):
        trace = shared_root_trace()
        graph = TraceGraph.from_trace(trace)
        start_refs = ("record:seed_one", "record:seed_two")
        objective = "Confirm each identical claim independently."

        def analyze(judge, checkpoint=None, checkpoint_config=None):
            return AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=checkpoint,
                checkpoint_config=checkpoint_config,
                fusion_mode="retrieval-global",
            ).analyze(
                graph,
                start_refs=start_refs,
                objective=objective,
                analysis_perspective="",
            )

        uninterrupted_judge = DivergentSharedRootFusionJudge()
        uninterrupted = analyze(uninterrupted_judge)
        by_ref = {item.start_ref: item for item in uninterrupted.seed_results}

        self.assertEqual(by_ref["record:seed_one"].outcome, "confirmed_root")
        self.assertEqual(by_ref["record:seed_two"].outcome, "evidence_gap")
        self.assertEqual(uninterrupted.analysis_outcome, "partial")
        self.assertEqual(
            [item.node_ref for item in uninterrupted.confirmed_roots],
            ["record:shared_root"],
        )
        self.assertTrue(
            all(
                request.competing_hypotheses == ()
                for request in uninterrupted_judge.confirmation_requests
            )
        )

        config = shared_root_checkpoint_config(trace, objective, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "divergent-shared-root.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                analyze(
                    DivergentSharedRootFusionJudge(),
                    CrashAfterFirstCompletedConfirmation(root),
                    config,
                )
            resumed_judge = DivergentSharedRootFusionJudge()
            resumed = analyze(resumed_judge, CheckpointBundle(root), config)

        self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
        self.assertEqual(len(resumed_judge.confirmation_requests), 1)


class SeedAttributionModelTests(unittest.TestCase):
    def test_same_start_ref_sibling_with_matching_lineage_is_rejected(self):
        report = run_fixture("multi_seed_claims.json")
        for sibling_outcome in ("no_defect", "evidence_gap"):
            with self.subTest(entry_point="direct", outcome=sibling_outcome):
                mutation = same_start_ref_sibling_mutation(
                    report,
                    sibling_outcome=sibling_outcome,
                    sibling_matches_root_lineage=True,
                )
                with self.assertRaisesRegex(ValueError, "composite.*owner"):
                    replace(report, **mutation)

            with self.subTest(entry_point="from_dict", outcome=sibling_outcome):
                mutation = same_start_ref_sibling_mutation(
                    report,
                    sibling_outcome=sibling_outcome,
                    sibling_matches_root_lineage=True,
                )
                payload = report.to_dict()
                payload.update(
                    {
                        key: [item.to_dict() for item in value]
                        for key, value in mutation.items()
                    }
                )
                with self.assertRaisesRegex(ValueError, "composite.*owner"):
                    RecursiveAttributionReport.from_dict(payload)

    def test_same_start_ref_sibling_with_distinct_lineage_is_disambiguated(self):
        report = run_fixture("multi_seed_claims.json")
        mutation = same_start_ref_sibling_mutation(
            report,
            sibling_outcome="evidence_gap",
            sibling_matches_root_lineage=False,
        )
        disambiguated = replace(report, **mutation)
        same_ref_seeds = [
            item
            for item in disambiguated.seed_results
            if item.start_ref == disambiguated.confirmed_roots[0].recursive_path[-1]
        ]
        self.assertEqual(
            {item.outcome for item in same_ref_seeds},
            {"confirmed_root", "evidence_gap"},
        )
        self.assertEqual(
            len({item.defect_fingerprint for item in same_ref_seeds}),
            2,
        )
        self.assertEqual(
            RecursiveAttributionReport.from_dict(disambiguated.to_dict()).to_dict(),
            disambiguated.to_dict(),
        )

    def test_published_root_rejects_another_report_seed_in_observed_refs(self):
        report = run_fixture("multi_seed_claims.json")
        root = report.confirmed_roots[0]
        other_seed = next(
            item
            for item in report.seed_results
            if item.start_ref not in root.observed_defect_refs
        )
        mixed_root = replace(
            root,
            observed_defect_refs=(
                *root.observed_defect_refs,
                "record:distinguishable-provenance",
                other_seed.start_ref,
            ),
        )

        with self.assertRaisesRegex(ValueError, "observed_defect_refs.*owning seed"):
            replace(report, confirmed_roots=(mixed_root,))

        payload = report.to_dict()
        payload["confirmed_roots"][0]["observed_defect_refs"] = list(
            mixed_root.observed_defect_refs
        )
        with self.assertRaisesRegex(ValueError, "observed_defect_refs.*owning seed"):
            RecursiveAttributionReport.from_dict(payload)

    def test_published_root_requires_exactly_one_confirmed_seed_owner(self):
        report = run_fixture("multi_seed_claims.json")
        owning = next(
            item for item in report.seed_results if item.outcome == "confirmed_root"
        )
        non_confirmed_owner = replace(
            owning,
            outcome="no_defect",
            confirmed_root_refs=(),
        )

        with self.assertRaisesRegex(ValueError, "non-confirmed.*published root"):
            replace(
                report,
                seed_results=tuple(
                    non_confirmed_owner if item == owning else item
                    for item in report.seed_results
                ),
            )

        orphaned_owner = replace(
            non_confirmed_owner,
            confirmation_identities=(),
        )
        payload = report.to_dict()
        payload["seed_results"] = [
            orphaned_owner.to_dict() if item == owning else item.to_dict()
            for item in report.seed_results
        ]
        with self.assertRaisesRegex(ValueError, "exactly one confirmed_root seed"):
            RecursiveAttributionReport.from_dict(payload)

    def test_all_no_defect_seeds_cannot_publish_top_level_roots(self):
        report = run_fixture("multi_seed_claims.json")
        no_defect_seeds = tuple(
            replace(
                item,
                outcome="no_defect",
                confirmation_identities=(),
                confirmed_root_refs=(),
                missing_evidence=(),
                blocking_reasons=(),
            )
            for item in report.seed_results
        )

        with self.assertRaisesRegex(ValueError, "exactly one confirmed_root seed"):
            replace(report, seed_results=no_defect_seeds)

        payload = report.to_dict()
        payload["seed_results"] = [item.to_dict() for item in no_defect_seeds]
        payload["analysis_outcome"] = "no_defect"
        with self.assertRaisesRegex(ValueError, "exactly one confirmed_root seed"):
            RecursiveAttributionReport.from_dict(payload)

    def test_schema_less_report_is_not_implicitly_parsed_as_modern(self):
        seed = seed_result("record:seed", "no_defect")
        payload = RecursiveAttributionReport(
            case_id="schema-less",
            objective="Reject ambiguous report versions.",
            start_refs=(seed.start_ref,),
            seed_results=(seed,),
        ).to_dict()
        payload.pop("schema_version")

        with self.assertRaisesRegex(ValueError, "schema_version"):
            RecursiveAttributionReport.from_dict(payload)

    def test_v3_seed_results_reject_non_object_entries_across_entry_points(self):
        valid = seed_result("record:seed", "no_defect")
        payload = RecursiveAttributionReport(
            case_id="non-object-seed-results",
            objective="Reject malformed seed entries.",
            start_refs=(valid.start_ref,),
            seed_results=(valid,),
        ).to_dict()
        payload["seed_results"].append("not-an-object")

        with self.assertRaisesRegex(TypeError, "seed_results"):
            RecursiveAttributionReport(
                case_id=payload["case_id"],
                objective=payload["objective"],
                start_refs=(valid.start_ref,),
                seed_results=(valid, "not-an-object"),
            )
        with self.assertRaisesRegex(TypeError, "seed_results"):
            RecursiveAttributionReport.from_dict(payload)
        with self.assertRaisesRegex(EvaluationSchemaError, "seed_results"):
            _validate_report_shape(payload, {"case_id": payload["case_id"]})

    def test_transformed_seed_lineage_with_real_confirmation_is_accepted_everywhere(self):
        trace = {
            "case_id": "transformed-seed-confirmation",
            "records": [
                {
                    "record_id": "root",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {
                        "rationale": "Search only the first matching implementation."
                    },
                },
                {
                    "record_id": "seed",
                    "component": "assistant",
                    "event_type": "response.claim",
                    "source_refs": ["record:root"],
                    "data": {"text": "Every implementation was inspected."},
                },
            ],
            "dataflow_edges": [
                {
                    "from": {"type": "record", "id": "root"},
                    "to": {"type": "record", "id": "seed"},
                    "relation": "decision_guided_claim",
                    "evidence_type": "confirmed",
                    "confidence": 0.9,
                    "eligible_for_attribution": True,
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        judge = TransformingRootJudge()
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            graph,
            start_refs=("record:seed",),
            objective="Find the omitted implementation root.",
        )

        direct = replace(report)
        payload = report.to_dict()
        parsed = RecursiveAttributionReport.from_dict(payload)
        self.assertEqual(direct.to_dict(), payload)
        self.assertEqual(parsed.to_dict(), payload)
        self.assertEqual(len(judge.confirmation_requests), 1)
        self.assertNotEqual(
            report.seed_results[0].defect_fingerprint,
            report.confirmed_roots[0].defect_state.fingerprint,
        )

        anchors = semantic_anchor_index(graph.case_id, graph)
        occurrences = semantic_occurrence_index(graph.case_id, graph)
        labels = {
            "schema_version": "recursive-attribution-labels/v3",
            "case_id": graph.case_id,
            "roots": [
                {
                    "node_ref": "record:root",
                    "semantic_anchor_id": anchors["record:root"],
                    "semantic_occurrence_id": occurrences["record:root"],
                }
            ],
            "conditions": [],
            "amplifiers": [],
            "forbidden_roots": [],
            "allowed_unresolved_outcomes": [],
        }
        projected = annotate_report_semantic_anchors(
            graph.case_id, graph.nodes, payload, graph=graph
        )
        self.assertTrue(compare_report(projected, labels, None, graph=graph)["safety"]["passed"])

    def test_every_seed_confirmation_identity_must_bind_individually(self):
        report = run_shared_root_report()
        first, second = report.seed_results
        foreign_identity = second.confirmation_identities[0]

        for label, extra_identity in (
            ("missing", "confirmation:missing"),
            ("other-seed", foreign_identity),
        ):
            with self.subTest(entry_point="direct", identity=label):
                mutated = replace(
                    first,
                    confirmation_identities=(
                        *first.confirmation_identities,
                        extra_identity,
                    ),
                )
                with self.assertRaisesRegex(
                    ValueError, "confirmation identities.*seed"
                ):
                    replace(report, seed_results=(mutated, second))

            with self.subTest(entry_point="from_dict", identity=label):
                payload = report.to_dict()
                payload["seed_results"][0]["confirmation_identities"].append(
                    extra_identity
                )
                with self.assertRaisesRegex(
                    ValueError, "confirmation identities.*seed"
                ):
                    RecursiveAttributionReport.from_dict(payload)

    def test_v3_requires_seed_results_to_cover_every_start_ref_across_all_entry_points(self):
        complete = RecursiveAttributionReport(
            case_id="seed-coverage",
            objective="Cover every declared seed.",
            start_refs=("record:one", "record:two"),
            seed_results=(
                seed_result("record:one", "no_defect"),
                seed_result("record:two", "evidence_gap"),
            ),
        )
        incomplete_results = (complete.seed_results[0],)

        with self.assertRaisesRegex(ValueError, "cover.*start_refs"):
            RecursiveAttributionReport(
                case_id=complete.case_id,
                objective=complete.objective,
                start_refs=complete.start_refs,
                seed_results=incomplete_results,
            )

        incomplete_payload = complete.to_dict()
        incomplete_payload["seed_results"] = incomplete_payload["seed_results"][:1]
        with self.assertRaisesRegex(ValueError, "cover.*start_refs"):
            RecursiveAttributionReport.from_dict(incomplete_payload)
        with self.assertRaisesRegex(EvaluationSchemaError, "cover.*start_refs"):
            _validate_report_shape(
                incomplete_payload,
                {"case_id": complete.case_id},
            )

    def test_v3_allows_multiple_defect_fingerprints_for_one_start_ref(self):
        first = seed_result("record:one", "no_defect")
        second_state = defect("second-fingerprint")
        second = SeedAttributionResult(
            start_ref="record:one",
            defect_fingerprint=second_state.fingerprint,
            defect_state=second_state,
            outcome="evidence_gap",
            missing_evidence=("second defect evidence",),
        )

        report = RecursiveAttributionReport(
            case_id="multi-fingerprint",
            objective="Retain both defects for one seed ref.",
            start_refs=("record:one",),
            seed_results=(first, second),
        )

        self.assertEqual(len(report.seed_results), 2)
        self.assertEqual(
            {item.start_ref for item in report.seed_results},
            {"record:one"},
        )

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
        with self.assertRaises((AttributeError, TypeError)):
            result.global_judgment._entries = ()
        with self.assertRaises((AttributeError, TypeError)):
            result.expansion_history[0]._entries = ()
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

    def test_frozen_mapping_rejects_own_storage_reassignment(self):
        value = FrozenMapping({"nested": {"refs": ["record:one"]}})

        with self.assertRaises((AttributeError, TypeError)):
            value._entries = ()
        with self.assertRaises((AttributeError, TypeError)):
            value["nested"]._entries = ()

        self.assertEqual(value, {"nested": {"refs": ["record:one"]}})

    def test_confirmed_seed_cannot_fabricate_root_without_top_level_confirmation(self):
        result = seed_result("record:seed", "confirmed_root")
        with self.assertRaisesRegex(ValueError, "confirmed_root"):
            RecursiveAttributionReport(
                case_id="forged-seed",
                objective="Reject fabricated local roots.",
                start_refs=["record:seed"],
                seed_results=[result],
            )

        conservative = seed_result("record:seed", "no_defect")
        payload = RecursiveAttributionReport(
            case_id="forged-seed",
            objective="Reject fabricated local roots.",
            start_refs=["record:seed"],
            seed_results=[conservative],
        ).to_dict()
        payload["seed_results"][0].update(result.to_dict())
        with self.assertRaisesRegex(ValueError, "confirmed_root"):
            RecursiveAttributionReport.from_dict(payload)

        outside_start_refs = json.loads(json.dumps(payload))
        outside_start_refs["start_refs"] = []
        with self.assertRaisesRegex(ValueError, "start_ref"):
            RecursiveAttributionReport.from_dict(outside_start_refs)

        non_confirmed = json.loads(json.dumps(payload))
        non_confirmed["seed_results"][0]["outcome"] = "no_defect"
        with self.assertRaisesRegex(ValueError, "non-confirmed"):
            RecursiveAttributionReport.from_dict(non_confirmed)

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
        self.assertFalse(migrated.confirmed_roots)
        self.assertFalse(migrated.co_roots)
        self.assertTrue(
            all(not item.confirmed_root_refs for item in migrated.seed_results)
        )
        self.assertEqual(migrated.analysis_outcome, "inconclusive")
        self.assertTrue(
            migrated.metadata["report_migration"]["unpublished_confirmed_roots"]
        )

    def test_top_level_aggregation_requires_root_ownership_when_outcomes_change(self):
        no_defect_seeds = (
            seed_result("a", "no_defect"),
            seed_result("b", "no_defect"),
        )
        no_defect = RecursiveAttributionReport(
            case_id="aggregation",
            objective="Aggregate seed outcomes.",
            start_refs=tuple(item.start_ref for item in no_defect_seeds),
            seed_results=no_defect_seeds,
        )
        self.assertEqual(no_defect.analysis_outcome, "no_defect")

        confirmed = run_shared_root_report()
        self.assertEqual(confirmed.analysis_outcome, "confirmed_root")
        first, second = confirmed.seed_results
        for name, replacement in (
            ("confirmed_and_no_defect", replace(first, outcome="no_defect", confirmed_root_refs=(), confirmation_identities=())),
            ("partial", replace(first, outcome="evidence_gap", confirmed_root_refs=(), confirmation_identities=(), missing_evidence=("missing",))),
            ("unresolved", replace(first, outcome="inconclusive", confirmed_root_refs=(), confirmation_identities=())),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ValueError, "exactly one confirmed_root seed"
                ):
                    replace(confirmed, seed_results=(replacement, second))

    def test_output_order_is_deterministic(self):
        seeds = (
            seed_result("record:z", "evidence_gap"),
            seed_result("record:a", "no_defect"),
            seed_result("record:m", "inconclusive"),
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

    def test_checkpoint_rejects_noncanonical_hypothesis_seed_key_routing(self):
        trace = {
            "case_id": "hypothesis-seed-key-routing",
            "records": [
                {
                    "record_id": "seed",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {"text": "A seed with pending frontier work."},
                }
            ],
        }
        graph = TraceGraph.from_trace(trace)
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:seed",),
            objective="Keep every frontier hypothesis bound to its seed.",
            analysis_perspective="",
        )
        owner = state._ensure_seed("record:seed", defect("owner"))
        other = state._ensure_seed("record:seed", defect("other"))
        hypothesis = state.ledger.create(
            "The pending item belongs to the owner seed.",
            "record:seed",
            owner.defect_state,
            seed_binding_identity=owner.key,
        )
        state._bind_hypothesis_to_seed(hypothesis.hypothesis_id, owner)
        state.frontier.push(
            FrontierItem.create(
                node_ref="record:seed",
                defect_state=owner.defect_state,
                downstream_path=["record:seed"],
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=owner.key,
                candidate_source="checkpoint-test",
            )
        )
        config = build_checkpoint_config(
            trace=trace,
            case_id=trace["case_id"],
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
                semantic_key="hypothesis-seed-key-routing",
                frontier_payload=state.frontier_checkpoint_payload(),
                hypothesis_payload=state.hypothesis_checkpoint_payload(),
                action_payload=state.action_checkpoint_payload(),
            )
            checkpoint = bundle.restore(expected_config=config)
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph, checkpoint=checkpoint
            )
            restored_item = FrontierItem.from_dict(restored.frontier.snapshot()[0])
            self.assertEqual(
                restored._seed_builder_for_item(restored_item).key, owner.key
            )

        mutations = {
            "extra": lambda mapping: mapping.update({"hyp:extra": owner.key}),
            "missing_frontier": lambda mapping: mapping.pop(hypothesis.hypothesis_id),
            "cross_seed": lambda mapping: mapping.update(
                {hypothesis.hypothesis_id: other.key}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                actions = json.loads(json.dumps(checkpoint.actions))
                snapshot = next(
                    item
                    for item in reversed(actions)
                    if item["operation"] == "state_snapshot"
                )
                mutate(snapshot["payload"]["hypothesis_seed_keys"])
                with self.assertRaisesRegex(ValueError, "hypothesis_seed_keys"):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=replace(checkpoint, actions=tuple(actions)),
                    )


if __name__ == "__main__":
    unittest.main()
