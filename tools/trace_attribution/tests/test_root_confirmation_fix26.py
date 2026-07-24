from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    OfflineJudgeCapability,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    LocalStateOwner,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    confirmation_counterfactual_for,
    seed_binding_identity_for,
)
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.hypotheses import RecursiveFrontier
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    FrontierItem,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_root_confirmation_fix21 import (
    InjectedCheckpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    STARTS,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    ArtifactConfirmationJudge,
    artifact_state,
    artifact_trace,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    TwoHopSharedRootJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
    two_hop_shared_root_trace,
)


def formal_manifest(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "run_id": "fix26-run",
        "subject_revision": "git:active",
        "subject_revision_provenance": {
            "method": "case_trace_config",
            "source": "CaseTraceConfig.subjectRevision",
            "bound_at": "case_start",
            "case_id": case_id,
            "run_id": "fix26-run",
        },
    }


def formally_unbound_shared_trace() -> dict:
    trace = shared_root_trace()
    trace["manifest"] = formal_manifest(trace["case_id"])
    return trace


def formally_bound_shared_trace() -> dict:
    trace = formally_unbound_shared_trace()
    for record in trace["records"]:
        record.setdefault("data", {}).update(
            {
                "subject_revision": "git:active",
                "revision_provenance_status": "valid",
            }
        )
    return trace


def checkpoint_for(
    trace: dict,
    judge,
    *,
    starts=STARTS,
    fusion_mode: str = "off",
    name: str = "fix26",
):
    config = shared_root_checkpoint_config(trace, OBJECTIVE, starts)
    tempdir = tempfile.TemporaryDirectory()
    root = Path(tempdir.name) / "{0}.checkpoint".format(name)
    report = AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode=fusion_mode,
        checkpoint=CheckpointBundle(root),
        checkpoint_config=config,
    ).analyze(
        TraceGraph.from_trace(trace),
        start_refs=starts,
        objective=OBJECTIVE,
        analysis_perspective="",
    )
    checkpoint = CheckpointBundle(root).restore(expected_config=config)
    return tempdir, root, config, checkpoint, report


def report_payload(checkpoint) -> dict:
    return copy.deepcopy(
        next(
            item["payload"]["report"]
            for item in reversed(checkpoint.actions)
            if item["operation"] == "analysis_ready"
        )
    )


def permissive_labels(case_id: str) -> dict:
    return {
        "schema_version": "recursive-attribution-labels/v3",
        "case_id": case_id,
        "roots": [],
        "conditions": [],
        "amplifiers": [],
        "forbidden_roots": [],
        "allowed_unresolved_outcomes": [
            "inconclusive",
            "partial_root_found",
        ],
    }


class StrictActiveStartBoundaryTest(unittest.TestCase):
    def test_formal_unbound_explicit_start_never_enters_state(self):
        graph = TraceGraph.from_trace(formally_unbound_shared_trace())
        self.assertTrue(
            graph.active_revision_evidence_eligible("record:seed_one")
        )
        self.assertFalse(
            graph.active_revision_start_eligible("record:seed_one")
        )

        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=("record:seed_one",),
            objective=OBJECTIVE,
            analysis_perspective="",
        )

        self.assertEqual(state.start_refs, ())
        self.assertEqual(state.seed_results(), ())
        self.assertFalse(state.frontier)

    def test_bound_formal_and_nonformal_legacy_explicit_starts_remain_valid(self):
        for trace in (formally_bound_shared_trace(), shared_root_trace()):
            with self.subTest(formal="manifest" in trace):
                graph = TraceGraph.from_trace(trace)
                state = RecursiveAnalysisState.create(
                    graph=graph,
                    start_refs=("record:seed_one",),
                    objective=OBJECTIVE,
                    analysis_perspective="",
                )
                self.assertEqual(state.start_refs, ("record:seed_one",))
                self.assertEqual(
                    {item.start_ref for item in state.seed_results()},
                    {"record:seed_one"},
                )

    def test_formal_unbound_seed_cannot_build_or_restore_global_request(self):
        judge = SharedRootFusionJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(formally_unbound_shared_trace()),
            start_refs=("record:seed_one",),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        self.assertEqual(judge.global_requests, [])
        self.assertEqual(report.start_refs, ())

    def test_formal_unbound_seed_is_rejected_on_checkpoint_and_report_restore(self):
        tempdir, _, _, checkpoint, _ = checkpoint_for(
            formally_bound_shared_trace(),
            RecursiveFallbackRootJudge(),
            starts=("record:seed_one",),
            fusion_mode="off",
            name="strict-start",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(formally_unbound_shared_trace())

        with self.assertRaisesRegex(ValueError, "start|seed|binding|revision"):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(
                    checkpoint,
                    list(checkpoint.actions),
                ),
            )

        restored = RecursiveAttributionReport.from_dict(
            report_payload(checkpoint)
        )
        with self.assertRaisesRegex(ValueError, "start|seed|binding|revision"):
            validate_recursive_report_against_graph(
                graph,
                restored,
                label="fix26 formal report restore",
                action_records=checkpoint.actions,
            )


class RecursiveFallbackRootJudge(OfflineJudgeCapability):
    def judge_step_offline(self, request):
        if request.current_node.ref == "record:seed_one":
            from trace_attribution.causal_state import PredecessorAssessment

            return CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="present",
                current_defect_reason="The shared root propagated this defect.",
                predecessors=(
                    PredecessorAssessment(
                        ref="record:shared_root",
                        relation="same_defect_propagation",
                        reason="The authored decision propagated the defect.",
                        confidence=0.9,
                        recurse=True,
                        evidence_refs=(
                            "record:shared_root",
                            "record:seed_one",
                        ),
                    ),
                ),
                candidate_introduction=False,
                confidence=0.9,
            )
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason="The authored decision introduced the defect.",
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context[
                        "active_hypothesis_id"
                    ],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Confirm the authored root.",
            },
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The authored decision introduced the defect.",
            reason="The recursive fallback confirms the root.",
            counterfactual=confirmation_counterfactual_for(
                request.candidate_ref,
                "confirmed",
            ),
            confidence=0.95,
            evidence_refs=(request.candidate_ref,),
        )


class FailingGlobalJudge(SharedRootFusionJudge):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if self.mode == "bounded":
            raise BoundedJudgeCallError(
                "fix26 bounded global failure",
                physical_requests=1,
            )
        if self.mode == "invalid":
            return BoundedJudgeCallResult("invalid-global-output", 0)
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class SelectiveGlobalJudge(SharedRootFusionJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if request.seed_ref == "record:seed_one":
            raise BoundedJudgeCallError(
                "seed one global failure",
                physical_requests=0,
            )
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class EnabledGlobalFailureTest(unittest.TestCase):
    def run_report(self, judge, *, starts=("record:seed_one",)):
        return AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=starts,
            objective=OBJECTIVE,
            analysis_perspective="",
        )

    def assert_failed_seed(self, report, blocker: str):
        seed = next(
            item
            for item in report.seed_results
            if item.start_ref == "record:seed_one"
        )
        self.assertEqual(seed.outcome, "evidence_gap")
        self.assertIn(blocker, seed.blocking_reasons)
        self.assertTrue(seed.missing_evidence)
        self.assertFalse(seed.confirmed_root_refs)
        self.assertFalse(report.confirmed_roots)
        failures = [
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("seed_ref") == "record:seed_one"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["status"], "failed")
        self.assertEqual(failures[0]["blocker"], blocker)

    def test_missing_global_capability_fails_closed_in_enabled_mode(self):
        report = self.run_report(RecursiveFallbackRootJudge())
        self.assert_failed_seed(
            report,
            "global_judge_capability_missing",
        )

    def test_candidate_capsule_failure_fails_closed(self):
        with patch(
            "trace_attribution.recursive_analyzer.build_candidate_evidence_capsules",
            side_effect=ValueError("fix26 capsule failure"),
        ):
            report = self.run_report(SharedRootFusionJudge())
        self.assert_failed_seed(
            report,
            "global_candidate_capsule_failure",
        )

    def test_request_validation_failure_fails_closed(self):
        with patch(
            "trace_attribution.recursive_analyzer.validate_global_candidate_request_against_graph",
            side_effect=ValueError("fix26 request validation failure"),
        ):
            report = self.run_report(SharedRootFusionJudge())
        self.assert_failed_seed(
            report,
            "global_request_validation_failure",
        )

    def test_bounded_global_judge_failure_fails_closed(self):
        report = self.run_report(FailingGlobalJudge("bounded"))
        self.assert_failed_seed(
            report,
            "global_judge_bounded_failure",
        )

    def test_invalid_global_judge_output_fails_closed(self):
        report = self.run_report(FailingGlobalJudge("invalid"))
        self.assert_failed_seed(
            report,
            "global_judge_output_invalid",
        )

    def test_one_failed_seed_does_not_block_an_independent_seed(self):
        report = self.run_report(
            SelectiveGlobalJudge(),
            starts=STARTS,
        )
        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(by_ref["record:seed_one"].outcome, "evidence_gap")
        self.assertEqual(
            by_ref["record:seed_two"].outcome,
            "confirmed_root",
        )
        self.assertEqual(
            {
                root.observed_defect_refs[-1]
                for root in report.confirmed_roots
            },
            {"record:seed_two"},
        )

    def test_global_failure_action_and_episode_round_trip_bijectively(self):
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
            name="global-failure",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())
        restored = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=partial_checkpoint(
                checkpoint,
                list(checkpoint.actions),
            ),
        )
        failures = [
            item
            for item in restored.investigation_journal
            if item.get("kind") == "global_candidate_pass"
            and item.get("status") == "failed"
        ]
        episodes = [
            item
            for item in restored.unresolved_branches
            if item.get("reason") == "global_judge_bounded_failure"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(
            failures[0]["pass_identity"],
            episodes[0]["global_pass_identity"],
        )
        validate_recursive_report_against_graph(
            graph,
            report,
            label="fix26 global failure report",
            action_records=checkpoint.actions,
        )


def artifact_graph(root: Path, owners: tuple[tuple[str, str], ...]) -> TraceGraph:
    content = b"fix26 verified artifact"
    trace = {
        "case_id": "fix26-artifact-owner",
        "artifacts": [
            {
                "artifact_id": "proof",
                "kind": "text",
                "path": "artifacts/proof.txt",
                "hash": "sha256:{0}".format(
                    hashlib.sha256(content).hexdigest()
                ),
                "byte_length": len(content),
            }
        ],
        "records": [
            {
                "record_id": record_id,
                "component": "agent",
                "event_type": "decision",
                "artifact_refs": (
                    ["artifact:proof"]
                    if ownership in {"owner", "stale"}
                    else []
                ),
                "data": {
                    "summary": "{0} artifact owner".format(ownership),
                    **(
                        {"revision_status": "stale"}
                        if ownership == "stale"
                        else {}
                    ),
                },
            }
            for record_id, ownership in owners
        ],
    }
    target = root / "artifacts" / "proof.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return TraceGraph.from_trace(trace, artifact_root=root)


class VerifiedArtifactOwnerBindingTest(unittest.TestCase):
    def test_orphan_and_stale_only_artifacts_are_ineligible(self):
        for owners in (
            (("orphan", "not-owner"),),
            (("stale", "stale"),),
        ):
            with self.subTest(owners=owners), tempfile.TemporaryDirectory() as directory:
                graph = artifact_graph(Path(directory), owners)
                self.assertFalse(
                    graph.active_revision_evidence_reference_eligible(
                        "artifact:proof"
                    )
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "owner|active|artifact",
                ):
                    graph.artifact_evidence_envelope(
                        "artifact:proof",
                        fact_kind="supporting_evidence",
                    )

    def test_mixed_stale_and_active_owner_selects_only_active_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("a_stale", "stale"), ("z_active", "owner")),
            )
            envelope = graph.artifact_evidence_envelope(
                "artifact:proof",
                fact_kind="supporting_evidence",
            )
        self.assertEqual(
            envelope["owner_reference"]["resolved_ref"],
            "record:z_active",
        )
        self.assertTrue(
            envelope["owner_reference"]["active_revision_eligible"]
        )

    def test_ambiguous_active_owners_fail_closed_without_explicit_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("owner_one", "owner"), ("owner_two", "owner")),
            )
            with self.assertRaisesRegex(ValueError, "ambiguous|owner"):
                graph.artifact_evidence_envelope(
                    "artifact:proof",
                    fact_kind="supporting_evidence",
                )

    def test_explicit_owner_selection_and_owner_substitution_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            graph = artifact_graph(
                Path(directory),
                (("owner_one", "owner"), ("owner_two", "owner")),
            )
            envelope = graph.artifact_evidence_envelope(
                "artifact:proof",
                fact_kind="supporting_evidence",
                expected_owner_ref="record:owner_two",
            )
            graph.validate_artifact_evidence_envelope(
                envelope,
                expected_owner_ref="record:owner_two",
            )
            substituted = copy.deepcopy(envelope)
            substituted["owner_reference"]["resolved_ref"] = (
                "record:owner_one"
            )
            with self.assertRaisesRegex(
                ValueError,
                "owner|envelope|binding",
            ):
                graph.validate_artifact_evidence_envelope(
                    substituted,
                    expected_owner_ref="record:owner_two",
                )

    def test_valid_owner_envelope_persists_through_actions_report_and_restore(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        trace = artifact_trace()
        content = b"verified proof"
        trace["artifacts"][0].update(
            {
                "hash": "sha256:{0}".format(
                    hashlib.sha256(content).hexdigest()
                ),
                "byte_length": len(content),
            }
        )
        target = root / "artifacts" / "proof.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        graph = TraceGraph.from_trace(trace, artifact_root=root)
        config = build_checkpoint_config(
            trace=trace,
            case_id=graph.case_id,
            objective="Confirm the artifact owner.",
            analysis_perspective="",
            start_refs=["record:candidate"],
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:fix26-artifact",
            cache_identity="cache:fix26-artifact",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://fix26-artifact",
                "provider_error_threshold": 3,
            },
        )
        checkpoint_root = root / "artifact.checkpoint"
        report = AgenticRecursiveAnalyzer(
            judge=ArtifactConfirmationJudge(),
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        ).analyze(
            graph,
            start_refs=["record:candidate"],
            objective="Confirm the artifact owner.",
            analysis_perspective="",
        )
        checkpoint = CheckpointBundle(checkpoint_root).restore(
            expected_config=config
        )
        payload = report.to_dict()
        queue_envelopes = payload["metadata"]["confirmation_queue"][0][
            "artifact_evidence_envelopes"
        ]
        action_envelopes = payload["metadata"][
            "confirmation_action_projection"
        ][0]["artifact_evidence_envelopes"]
        self.assertEqual(queue_envelopes, action_envelopes)
        graph.validate_artifact_evidence_envelope(
            queue_envelopes[0],
            expected_owner_ref="record:candidate",
        )

        restored = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=partial_checkpoint(
                checkpoint,
                list(checkpoint.actions),
            ),
        )
        self.assertEqual(
            restored.confirmation_queue[0][
                "artifact_evidence_envelopes"
            ],
            queue_envelopes,
        )
        validate_recursive_report_against_graph(
            graph,
            RecursiveAttributionReport.from_dict(payload),
            label="fix26 artifact report",
            action_records=checkpoint.actions,
        )

    def test_report_rejects_substituted_artifact_owner(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        graph, state, _, queued = artifact_state(root)
        self.assertTrue(state.enqueue_confirmation(queued))
        state.validate_confirmation_queue_bound()
        envelope = state.confirmation_queue[0][
            "artifact_evidence_envelopes"
        ][0]
        envelope["owner_reference"]["resolved_ref"] = "record:substitute"
        with self.assertRaisesRegex(ValueError, "owner|artifact|envelope"):
            state.validate_confirmation_queue_bound()


class CanonicalGlobalPassIdentityTest(unittest.TestCase):
    def test_multiple_frontier_occurrences_for_one_seed_run_one_pass(self):
        graph = TraceGraph.from_trace(shared_root_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=("record:seed_one",),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        original = FrontierItem.from_dict(state.frontier.snapshot()[0])
        hypothesis = state.ledger.create(
            "A second frontier occurrence for the same seed.",
            original.node_ref,
            original.defect_state,
            seed_binding_identity=original.seed_binding_identity,
        )
        state._bind_hypothesis_to_seed(
            hypothesis.hypothesis_id,
            state.seed_ledger[original.seed_binding_identity],
        )
        state.frontier.push(
            FrontierItem.create(
                node_ref=original.node_ref,
                defect_state=original.defect_state,
                downstream_path=original.downstream_path,
                hypothesis_id=hypothesis.hypothesis_id,
                hypothesis_semantic_hash=hypothesis.semantic_hash,
                seed_binding_identity=original.seed_binding_identity,
                depth=original.depth,
                candidate_source="fix26_duplicate_seed_frontier",
                priority=original.priority + 0.1,
                checked_evidence_refs=original.checked_evidence_refs,
                graph_position=graph.position(original.node_ref),
            )
        )
        judge = SharedRootFusionJudge()
        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        )._run_global_candidate_prepass(state, graph)

        passes = [
            item
            for item in state.investigation_journal
            if item.get("kind") == "global_candidate_pass"
        ]
        self.assertEqual(len(judge.global_requests), 1)
        self.assertEqual(len(passes), 1)
        self.assertEqual(
            passes[0]["seed_binding_identity"],
            original.seed_binding_identity,
        )
        self.assertTrue(passes[0]["pass_identity"])

    def test_two_independent_seeds_have_one_distinct_canonical_pass_each(self):
        report = AgenticRecursiveAnalyzer(
            judge=SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        passes = [
            item
            for item in report.investigation_journal
            if item.get("kind") == "global_candidate_pass"
        ]
        self.assertEqual(len(passes), 2)
        self.assertEqual(
            {item["seed_binding_identity"] for item in passes},
            {
                seed_binding_identity_for(
                    seed.start_ref,
                    seed.defect_fingerprint,
                )
                for seed in report.seed_results
            },
        )
        self.assertEqual(
            len({item["pass_identity"] for item in passes}),
            2,
        )

    def test_duplicate_completed_pass_identity_and_substituted_owner_are_rejected(self):
        tempdir, _, _, checkpoint, _ = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
            name="global-pass-identity",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())
        for mutation in ("duplicate", "substitute"):
            with self.subTest(mutation=mutation):
                actions = copy.deepcopy(list(checkpoint.actions))
                snapshot = latest_snapshot(actions)
                passes = [
                    item
                    for item in snapshot["investigation_journal"]
                    if item.get("kind") == "global_candidate_pass"
                    and item.get("status") == "completed"
                ]
                if mutation == "duplicate":
                    snapshot["investigation_journal"].append(
                        copy.deepcopy(passes[0])
                    )
                else:
                    passes[0]["owner"] = copy.deepcopy(
                        passes[1]["owner"]
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "global|pass|owner|identity|duplicate",
                ):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=partial_checkpoint(
                            checkpoint,
                            actions,
                        ),
                    )


def completed_step_checkpoint():
    trace = two_hop_shared_root_trace()
    return (
        trace,
        *checkpoint_for(
            trace,
            TwoHopSharedRootJudge(),
            starts=("record:seed_one",),
            fusion_mode="off",
            name="step-actions",
        ),
    )


def completed_step_actions(actions):
    return [
        item
        for item in actions
        if item.get("operation") == "provider_call_completed"
        and item.get("payload", {}).get("call_kind") == "step"
    ]


class StepProviderActionBijectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.trace,
            cls.tempdir,
            cls.root,
            cls.config,
            cls.checkpoint,
            cls.report,
        ) = completed_step_checkpoint()
        cls.graph = TraceGraph.from_trace(cls.trace)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def restore_partial(self, actions):
        return RecursiveAnalysisState.from_checkpoint(
            graph=self.graph,
            checkpoint=partial_checkpoint(self.checkpoint, actions),
        )

    def test_content_mutated_step_judgment_is_rejected(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        latest_snapshot(actions)["step_judgments"][0][
            "current_defect_reason"
        ] = "FIX26 MUTATED JUDGMENT CONTENT"
        with self.assertRaisesRegex(
            ValueError,
            "provider|step|action|judgment|bijection",
        ):
            self.restore_partial(actions)

    def test_coordinated_step_and_relation_replacement_is_rejected(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        snapshot = latest_snapshot(actions)
        judgment = next(
            item for item in snapshot["step_judgments"]
            if item.get("predecessors")
        )
        predecessor = judgment["predecessors"][0]
        occurrence = predecessor["owner"]["occurrence_identity"]
        relation = next(
            item
            for item in snapshot["causal_relations"]
            if item["owner"]["occurrence_identity"] == occurrence
        )
        predecessor["reason"] = "FIX26 COORDINATED REPLACEMENT"
        relation["reason"] = "FIX26 COORDINATED REPLACEMENT"
        with self.assertRaisesRegex(
            ValueError,
            "provider|step|action|judgment|bijection",
        ):
            self.restore_partial(actions)

    def test_duplicate_missing_and_failed_provider_actions_are_rejected(self):
        for mutation in ("duplicate", "missing", "failed"):
            with self.subTest(mutation=mutation):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                completed = completed_step_actions(actions)
                if mutation == "duplicate":
                    actions.append(copy.deepcopy(completed[0]))
                elif mutation == "missing":
                    actions.remove(completed[0])
                else:
                    completed[0]["operation"] = "provider_call_failed"
                with self.assertRaisesRegex(
                    ValueError,
                    "provider|step|action|judgment|bijection",
                ):
                    self.restore_partial(actions)

    def test_valid_action_projection_round_trips_to_report_and_evaluator(self):
        payload = self.report.to_dict()
        projection = payload["metadata"]["step_action_projection"]
        self.assertEqual(
            len(projection),
            len(self.report.step_judgments),
        )
        validate_recursive_report_against_graph(
            self.graph,
            RecursiveAttributionReport.from_dict(payload),
            label="fix26 step projection report",
            action_records=self.checkpoint.actions,
        )
        annotated = annotate_report_semantic_anchors(
            self.graph.case_id,
            self.graph.nodes,
            payload,
            graph=self.graph,
        )
        comparison = compare_report(
            annotated,
            permissive_labels(self.graph.case_id),
            None,
            graph=self.graph,
        )
        self.assertTrue(comparison["safety"]["passed"])

    def test_evaluator_rejects_coordinated_judgment_relation_tamper(self):
        payload = self.report.to_dict()
        judgment = next(
            item for item in payload["step_judgments"]
            if item.get("predecessors")
        )
        predecessor = judgment["predecessors"][0]
        occurrence = predecessor["owner"]["occurrence_identity"]
        relation = next(
            item
            for item in payload["causal_relations"]
            if item["owner"]["occurrence_identity"] == occurrence
        )
        predecessor["reason"] = "FIX26 EVALUATOR COORDINATED TAMPER"
        relation["reason"] = "FIX26 EVALUATOR COORDINATED TAMPER"
        with self.assertRaisesRegex(
            (EvaluationSafetyError, ValueError),
            "provider|step|action|judgment|bijection",
        ):
            compare_report(
                annotate_report_semantic_anchors(
                    self.graph.case_id,
                    self.graph.nodes,
                    payload,
                    graph=self.graph,
                ),
                permissive_labels(self.graph.case_id),
                None,
                graph=self.graph,
            )


def queue_key(entry: dict) -> tuple[str, str, str, str]:
    return (
        str(entry.get("hypothesis_id") or ""),
        str(entry.get("candidate_ref") or ""),
        str(entry.get("defect_fingerprint") or ""),
        str(entry.get("seed_binding_identity") or ""),
    )


class ConfirmationQueueKeyBijectionTest(unittest.TestCase):
    def queued_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        graph, state, _, queued = artifact_state(Path(tempdir.name))
        self.assertTrue(state.enqueue_confirmation(queued))
        return graph, state

    def test_live_queue_rejects_missing_extra_and_duplicate_keys(self):
        for mutation in ("missing", "extra", "duplicate-entry"):
            with self.subTest(mutation=mutation):
                _, state = self.queued_state()
                if mutation == "missing":
                    state.confirmation_queue_keys.clear()
                elif mutation == "extra":
                    state.confirmation_queue_keys.add(
                        ("hyp:extra", "record:extra", "defect:extra", "seed:extra")
                    )
                else:
                    state.confirmation_queue.append(
                        copy.deepcopy(state.confirmation_queue[0])
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "queue|key|duplicate|bijection",
                ):
                    state.validate_confirmation_queue_bound()

    def test_inconsistent_key_set_cannot_silently_suppress_enqueue(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        _, state, _, queued = artifact_state(Path(tempdir.name))
        state.confirmation_queue_keys.add(queue_key(queued))
        with self.assertRaisesRegex(
            ValueError,
            "queue|key|bijection|inconsistent",
        ):
            state.enqueue_confirmation(queued)

    def test_duplicate_semantic_identity_is_rejected(self):
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
            name="duplicate-request-identity",
        )
        self.addCleanup(tempdir.cleanup)
        payload = report.to_dict()
        queue = payload["metadata"]["confirmation_queue"]
        queue[1]["semantic_identity"] = queue[0]["semantic_identity"]
        restored = RecursiveAttributionReport.from_dict(payload)
        with self.assertRaisesRegex(
            ValueError,
            "semantic|request|identity|canonical|duplicate",
        ):
            validate_recursive_report_against_graph(
                TraceGraph.from_trace(shared_root_trace()),
                restored,
                label="duplicate full confirmation request identity",
                action_records=checkpoint.actions,
            )

    def test_checkpoint_and_report_reject_queue_key_mismatch(self):
        tempdir, _, _, checkpoint, report = checkpoint_for(
            shared_root_trace(),
            SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
            name="queue-keys",
        )
        self.addCleanup(tempdir.cleanup)
        graph = TraceGraph.from_trace(shared_root_trace())
        actions = copy.deepcopy(list(checkpoint.actions))
        latest_snapshot(actions)["confirmation_queue_keys"].append(
            ["hyp:extra", "record:extra", "defect:extra", "seed:extra"]
        )
        with self.assertRaisesRegex(
            ValueError,
            "queue|key|bijection|extra",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

        payload = report.to_dict()
        payload["metadata"]["confirmation_queue_keys"].append(
            ["hyp:extra", "record:extra", "defect:extra", "seed:extra"]
        )
        with self.assertRaisesRegex(
            ValueError,
            "queue|key|bijection|extra",
        ):
            validate_recursive_report_against_graph(
                graph,
                RecursiveAttributionReport.from_dict(payload),
                label="fix26 queue report",
                action_records=checkpoint.actions,
            )


class Fix26VersionIdentityTest(unittest.TestCase):
    def test_changed_persistence_shapes_are_explicitly_versioned(self):
        from scripts.evaluate_recursive_attribution import (
            REPORT_SCHEMA_VERSION,
        )
        from trace_attribution.causal_state import (
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            MODERN_REPORT_SCHEMA_VERSION,
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        from trace_attribution.checkpoint import CHECKPOINT_SCHEMA_VERSION
        from trace_attribution.graph import (
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        )
        from trace_attribution.recursive_analyzer import ACTION_STATE_SCHEMA

        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v17",
        )
        self.assertEqual(REPORT_SCHEMA_VERSION, MODERN_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v16",
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v14",
        )
        self.assertEqual(
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
            "graph-external-evidence-eligibility/v5",
        )
        self.assertIn(
            "global-pass-identity/v1",
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "artifact-owner/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "step-action-projection/v1",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )

    def test_old_report_shape_is_rejected_not_silently_reinterpreted(self):
        report = AgenticRecursiveAnalyzer(
            judge=TwoHopSharedRootJudge()
        ).analyze(
            TraceGraph.from_trace(two_hop_shared_root_trace()),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        ).to_dict()
        report["schema_version"] = "recursive-attribution-report/v8"
        with self.assertRaisesRegex(ValueError, "schema|unsupported"):
            RecursiveAttributionReport.from_dict(report)


if __name__ == "__main__":
    unittest.main()
