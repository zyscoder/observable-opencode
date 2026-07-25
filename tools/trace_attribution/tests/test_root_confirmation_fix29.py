from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    REPORT_SCHEMA_VERSION as EVALUATOR_REPORT_SCHEMA_VERSION,
    compare_report,
)
from trace_attribution.cache import JudgmentCache
from trace_attribution.causal_judge import ClaudeCausalJudge
from trace_attribution.causal_state import (
    GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
    LocalStateOwner,
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    global_candidate_request_from_validation_envelope,
    validate_global_candidate_payload,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    ACTION_STATE_SCHEMA,
    GLOBAL_FAILURE_PROJECTION_SCHEMA,
    RecursiveAnalysisState,
    _classify_global_failure_episodes,
    _classify_global_pass_records,
    _global_pass_identity,
    _quarantine_stale_seed_report_payload,
    validate_recursive_report_against_graph,
)
from tools.trace_attribution.tests.test_global_judge import (
    payload as global_payload,
    sample_request,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    STARTS,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix25 import (
    ArtifactConfirmationJudge,
    artifact_trace,
    stale_seed_trace,
)
from tools.trace_attribution.tests.test_root_confirmation_fix26 import (
    SelectiveGlobalJudge,
    checkpoint_for,
    permissive_labels,
)
from tools.trace_attribution.tests.test_root_confirmation_fix27 import (
    StaticTransport,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_trace,
)
from tools.trace_attribution.tests import (
    test_recursive_analyzer as recursive_test_support,
)


def closed_no_defect_payload():
    request = sample_request()
    value = global_payload(outcome="no_defect", request=request)
    open_refs = set(request.open_authored_root_candidate_refs)
    for item in value["assessments"]:
        if item["candidate_ref"] not in open_refs:
            continue
        item.update(
            {
                "defect_status": "absent",
                "input_defect_status": "absent",
                "output_defect_status": "absent",
                "causal_role": "unrelated",
            }
        )
    return request, value


class NoDefectOpenCandidateClosureTest(unittest.TestCase):
    def open_assessment(self, request, value):
        open_refs = set(request.open_authored_root_candidate_refs)
        return next(
            item
            for item in value["assessments"]
            if item["candidate_ref"] in open_refs
        )

    def assert_rejected(self, value, request):
        with self.assertRaisesRegex(
            ValueError,
            (
                "no_defect|open authored|assessment|unknown|causal|"
                "counterfactual|every offered"
            ),
        ):
            validate_global_candidate_payload(value, request=request)

    def test_rejects_unknown_output_for_open_authored_candidate(self):
        request, value = closed_no_defect_payload()
        assessment = self.open_assessment(request, value)
        assessment["defect_status"] = "unknown"
        assessment["output_defect_status"] = "unknown"

        self.assert_rejected(value, request)

    def test_rejects_unknown_input_for_open_authored_candidate(self):
        request, value = closed_no_defect_payload()
        self.open_assessment(request, value)["input_defect_status"] = "unknown"

        self.assert_rejected(value, request)

    def test_rejects_unknown_role_for_open_authored_candidate(self):
        request, value = closed_no_defect_payload()
        self.open_assessment(request, value)["causal_role"] = "unknown"

        self.assert_rejected(value, request)

    def test_rejects_missing_assessment_for_one_open_authored_candidate(self):
        request, value = closed_no_defect_payload()
        open_refs = set(request.open_authored_root_candidate_refs)
        value["assessments"] = [
            item
            for item in value["assessments"]
            if item["candidate_ref"] not in open_refs
        ]

        self.assert_rejected(value, request)

    def test_rejects_present_open_candidate_hidden_as_outcome_evidence(self):
        request, value = closed_no_defect_payload()
        assessment = self.open_assessment(request, value)
        assessment.update(
            {
                "defect_status": "present",
                "output_defect_status": "present",
                "causal_role": "outcome_evidence",
            }
        )

        self.assert_rejected(value, request)

    def test_rejects_unresolved_open_candidate_causal_path(self):
        request, value = closed_no_defect_payload()
        self.open_assessment(request, value)["causal_path_refs"] = []

        self.assert_rejected(value, request)

    def test_rejects_missing_open_candidate_counterfactual(self):
        request, value = closed_no_defect_payload()
        self.open_assessment(request, value).pop("counterfactual")

        self.assert_rejected(value, request)

    def test_accepts_fully_closed_no_defect_matrix(self):
        request, value = closed_no_defect_payload()

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "no_defect")
        by_ref = {
            item.candidate_ref: item
            for item in judgment.assessments
        }
        for ref in request.open_authored_root_candidate_refs:
            self.assertEqual(by_ref[ref].input_defect_status, "absent")
            self.assertEqual(by_ref[ref].output_defect_status, "absent")
            self.assertNotEqual(by_ref[ref].causal_role, "unknown")
            self.assertTrue(by_ref[ref].causal_path_refs)

    def test_real_adapter_repairs_insufficient_no_defect_to_inconclusive(self):
        request, invalid = closed_no_defect_payload()
        self.open_assessment(request, invalid)[
            "input_defect_status"
        ] = "unknown"
        repaired = copy.deepcopy(invalid)
        repaired.update(
            {
                "outcome": "inconclusive",
                "reason": (
                    "The open authored candidate input status remains "
                    "unresolved."
                ),
                "decisive_evidence_refs": [],
                "missing_evidence": [
                    "Determine the open authored candidate input defect status."
                ],
            }
        )
        transport = StaticTransport(
            [json.dumps(invalid), json.dumps(repaired)]
        )

        result = ClaudeCausalJudge(
            transport=transport,
            cache=JudgmentCache(),
        ).judge_candidates_bounded(
            request,
            max_physical_requests=2,
        )

        self.assertEqual(result.physical_requests, 2)
        self.assertEqual(result.value.outcome, "inconclusive")
        self.assertEqual(
            result.value.missing_evidence,
            (
                "Determine the open authored candidate input defect status.",
            ),
        )

    def test_analyzer_turns_insufficient_no_defect_into_seed_gap(self):
        judge = recursive_test_support.FusionScriptedJudge(
            global_outcome="no_defect"
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(recursive_test_support.observed_trace()),
            start_refs=("record:observed_defect",),
            objective="Determine whether the observed defect is supported.",
        )

        self.assertEqual(report.seed_results[0].outcome, "evidence_gap")
        self.assertIn(
            "global_judge_output_invalid",
            report.seed_results[0].blocking_reasons,
        )
        self.assertFalse(report.seed_results[0].global_judgment)


def substituted_global_owner(record):
    return LocalStateOwner.create(
        seed_binding_identity=str(
            record.get("seed_binding_identity")
            or record.get("failure_projection", {}).get(
                "seed_binding_identity"
            )
            or ""
        ),
        hypothesis_id="hypothesis:fix29-owner-substitution",
        visit_key="visit:fix29-owner-substitution",
        occurrence_key="global_candidate_pass",
    ).to_dict()


class CanonicalGlobalTerminalClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (
            cls.tempdir,
            _,
            _,
            cls.checkpoint,
            cls.report,
        ) = checkpoint_for(
            shared_root_trace(),
            SelectiveGlobalJudge(),
            fusion_mode="retrieval-global",
            name="fix29-global-terminal-classifier",
        )
        cls.graph = TraceGraph.from_trace(shared_root_trace())

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def completed_event(self):
        return copy.deepcopy(
            next(
                item
                for item in self.report.to_dict()["investigation_journal"]
                if item.get("status") == "completed"
            )
        )

    def failed_event(self, snapshot):
        return next(
            item
            for item in snapshot["investigation_journal"]
            if item.get("status") == "failed"
        )

    def failed_episode(self, snapshot, pass_identity):
        return next(
            item
            for item in snapshot["unresolved_branches"]
            if item.get("global_pass_identity") == pass_identity
        )

    def test_classifier_reconstructs_seed_binding_and_full_canonical_owner(self):
        owner_substitution = self.completed_event()
        owner_substitution["owner"] = substituted_global_owner(
            owner_substitution
        )
        seed_substitution = self.completed_event()
        seed_substitution["seed_binding_identity"] = (
            "seed_binding:fix29-substitution"
        )
        seed_substitution["pass_identity"] = _global_pass_identity(
            seed_substitution["seed_binding_identity"]
        )
        seed_substitution["owner"] = substituted_global_owner(
            seed_substitution
        )

        for record in (owner_substitution, seed_substitution):
            with self.subTest(record=record["seed_binding_identity"]):
                with self.assertRaisesRegex(
                    ValueError,
                    "global|pass|owner|seed|canonical|identity",
                ):
                    _classify_global_pass_records((record,))

    def test_classifier_recognizes_kind_only_failure_episode(self):
        episode = {
            "kind": "global_candidate_pass",
            "status": "failed",
        }

        with self.assertRaisesRegex(
            ValueError,
            "global|episode|marker|schema",
        ):
            _classify_global_failure_episodes((episode,))

    def test_checkpoint_rejects_kind_only_episode_before_restore_filtering(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        latest_snapshot(actions)["unresolved_branches"].append(
            {
                "kind": "global_candidate_pass",
                "status": "failed",
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "global|episode|marker|schema",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(self.checkpoint, actions),
            )

    def test_report_and_evaluator_reject_kind_only_episode(self):
        payload = self.report.to_dict()
        payload["metadata"]["unresolved_branches"].append(
            {
                "kind": "global_candidate_pass",
                "status": "failed",
            }
        )
        with self.assertRaisesRegex(
            ValueError,
            "global|episode|marker|schema",
        ):
            RecursiveAttributionReport.from_dict(payload)
        with self.assertRaises(EvaluationSafetyError):
            compare_report(
                payload,
                permissive_labels(self.graph.case_id),
                None,
                graph=self.graph,
            )

    def test_live_terminal_set_rejects_substituted_owner_before_skip(self):
        event = self.completed_event()
        event["owner"] = substituted_global_owner(event)
        seed_ref = str(event["seed_ref"])
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=(seed_ref,),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        state.investigation_journal.append(event)

        with self.assertRaisesRegex(
            ValueError,
            "global|pass|owner|canonical",
        ):
            AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
            )._run_global_candidate_prepass(state, self.graph)

    def test_stale_quarantine_rejects_coordinated_owner_substitution(self):
        payload = self.report.to_dict()
        failed = next(
            item
            for item in payload["investigation_journal"]
            if item.get("status") == "failed"
        )
        substituted = substituted_global_owner(failed)
        failed["owner"] = copy.deepcopy(substituted)
        failed["failure_projection"]["owner"] = copy.deepcopy(
            substituted
        )
        episode = next(
            item
            for item in payload["metadata"]["unresolved_branches"]
            if item.get("global_pass_identity") == failed["pass_identity"]
        )
        episode["owner"] = copy.deepcopy(substituted)
        episode["failure_projection"]["owner"] = copy.deepcopy(
            substituted
        )
        failure = next(
            item
            for item in payload["metadata"]["global_candidate_failures"]
            if item.get("pass_identity") == failed["pass_identity"]
        )
        failure["owner"] = copy.deepcopy(substituted)

        with self.assertRaisesRegex(
            ValueError,
            "global|pass|owner|canonical",
        ):
            _quarantine_stale_seed_report_payload(
                TraceGraph.from_trace(
                    stale_seed_trace(shared_root_trace())
                ),
                payload,
            )


def multi_owner_artifact_graph(
    root: Path,
    *,
    record_ids=("candidate", "other_owner"),
):
    trace = artifact_trace()
    trace["case_id"] = "fix29-terminal-artifact-owner"
    trace["records"] = [
        {
            "record_id": record_id,
            "component": "agent",
            "event_type": "decision",
            "artifact_refs": ["artifact:proof"],
            "data": {
                "summary": (
                    "The {0} decision is explicitly supported by the "
                    "verified proof."
                ).format(record_id)
            },
        }
        for record_id in record_ids
    ]
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return trace, TraceGraph.from_trace(trace, artifact_root=root)


def artifact_checkpoint_config(trace, graph, starts):
    return build_checkpoint_config(
        trace=trace,
        case_id=graph.case_id,
        objective="Confirm each explicitly owned artifact candidate.",
        analysis_perspective="",
        start_refs=list(starts),
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        model_identity="offline:fix29-terminal-artifact",
        cache_identity="cache:fix29-terminal-artifact",
        runtime_identity={
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://fix29-terminal-artifact",
            "provider_error_threshold": 3,
        },
    )


class QueueTamperingArtifactJudge(ArtifactConfirmationJudge):
    def __init__(self, mode, target_ref="record:candidate"):
        self.mode = mode
        self.target_ref = target_ref
        self.state = None

    def confirm_candidate_offline(self, request):
        if request.candidate_ref == self.target_ref:
            queued = next(
                item
                for item in self.state.confirmation_queue
                if item.get("candidate_ref") == request.candidate_ref
                and item.get("status") == "queued"
            )
            envelopes = queued["artifact_evidence_envelopes"]
            if self.mode == "missing":
                envelopes.clear()
            elif self.mode == "duplicate":
                envelopes.append(copy.deepcopy(envelopes[0]))
            elif self.mode == "substituted":
                envelopes[0]["owner_reference"]["resolved_ref"] = (
                    "record:other_owner"
                )
            elif self.mode == "stale":
                envelopes[0]["owner_reference"][
                    "active_revision_eligible"
                ] = False
            elif self.mode == "contradictory":
                envelopes[0]["content_hash"] = "sha256:{0}".format(
                    "0" * 64
                )
        return super().confirm_candidate_offline(request)


class StateCapturingArtifactAnalyzer(AgenticRecursiveAnalyzer):
    def _build_confirmation_request(self, state, queued):
        self.judge.state = state
        return AgenticRecursiveAnalyzer._build_confirmation_request(
            state, queued
        )


class ExplicitArtifactOwnerTerminalValidationTest(unittest.TestCase):
    def run_analysis(
        self,
        *,
        record_ids=("candidate", "other_owner"),
        starts=("record:candidate",),
        judge=None,
        name="artifact",
    ):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        trace, graph = multi_owner_artifact_graph(
            root,
            record_ids=record_ids,
        )
        config = artifact_checkpoint_config(trace, graph, starts)
        checkpoint_root = root / "{0}.checkpoint".format(name)
        analyzer_class = (
            StateCapturingArtifactAnalyzer
            if isinstance(judge, QueueTamperingArtifactJudge)
            else AgenticRecursiveAnalyzer
        )
        report = analyzer_class(
            judge=judge or ArtifactConfirmationJudge(),
            checkpoint=CheckpointBundle(checkpoint_root),
            checkpoint_config=config,
        ).analyze(
            graph,
            start_refs=starts,
            objective="Confirm each explicitly owned artifact candidate.",
            analysis_perspective="",
        )
        checkpoint = CheckpointBundle(checkpoint_root).restore(
            expected_config=config
        )
        return graph, checkpoint, report

    def test_explicit_candidate_owner_completes_and_round_trips(self):
        graph, checkpoint, report = self.run_analysis(
            name="valid-explicit-owner"
        )

        self.assertEqual(report.analysis_outcome, "confirmed_root")
        envelope = report.to_dict()["metadata"]["confirmation_queue"][0][
            "artifact_evidence_envelopes"
        ][0]
        self.assertEqual(
            envelope["owner_reference"]["resolved_ref"],
            "record:candidate",
        )
        graph.validate_artifact_evidence_envelope(
            envelope,
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
            restored.confirmations[0].evidence_refs,
            ("artifact:proof",),
        )
        validate_recursive_report_against_graph(
            graph,
            RecursiveAttributionReport.from_dict(report.to_dict()),
            label="fix29 explicit artifact owner report",
            action_records=checkpoint.actions,
        )
        comparison = compare_report(
            annotate_report_semantic_anchors(
                graph.case_id,
                graph.nodes,
                report.to_dict(),
                graph=graph,
            ),
            permissive_labels(graph.case_id),
            None,
            graph=graph,
        )
        self.assertTrue(comparison["safety"]["passed"])

    def test_invalid_terminal_artifact_envelopes_fail_only_owning_seed(self):
        for mode in (
            "missing",
            "duplicate",
            "substituted",
            "stale",
            "contradictory",
        ):
            with self.subTest(mode=mode):
                _, checkpoint, report = self.run_analysis(
                    judge=QueueTamperingArtifactJudge(mode),
                    name="invalid-{0}".format(mode),
                )
                seed = report.seed_results[0]
                self.assertEqual(seed.outcome, "evidence_gap")
                self.assertIn(
                    "root_confirmation_unknown",
                    seed.blocking_reasons,
                )
                self.assertTrue(
                    any(
                        "terminal_artifact_preflight_rejected"
                        in item
                        for item in seed.missing_evidence
                    )
                )
                terminal_actions = [
                    item
                    for item in checkpoint.actions
                    if item.get("operation")
                    in {
                        "confirmation_completed",
                        "confirmation_failed",
                    }
                ]
                self.assertEqual(
                    [item["operation"] for item in terminal_actions],
                    ["confirmation_failed"],
                )

    def test_terminal_artifact_failure_does_not_block_other_seed(self):
        graph, checkpoint, report = self.run_analysis(
            record_ids=("candidate", "candidate_two"),
            starts=("record:candidate", "record:candidate_two"),
            judge=QueueTamperingArtifactJudge(
                "substituted",
                target_ref="record:candidate",
            ),
            name="independent-seeds",
        )

        by_ref = {item.start_ref: item for item in report.seed_results}
        self.assertEqual(
            by_ref["record:candidate"].outcome,
            "evidence_gap",
        )
        self.assertEqual(
            by_ref["record:candidate_two"].outcome,
            "confirmed_root",
        )
        self.assertEqual(
            {root.node_ref for root in report.confirmed_roots},
            {"record:candidate_two"},
        )
        terminal_by_candidate = {
            item["payload"]["action_projection"]["candidate_ref"]: item[
                "operation"
            ]
            for item in checkpoint.actions
            if item.get("operation")
            in {"confirmation_completed", "confirmation_failed"}
        }
        self.assertEqual(
            terminal_by_candidate,
            {
                "record:candidate": "confirmation_failed",
                "record:candidate_two": "confirmation_completed",
            },
        )
        validate_recursive_report_against_graph(
            graph,
            RecursiveAttributionReport.from_dict(report.to_dict()),
            label="fix29 independent terminal artifact seeds",
            action_records=checkpoint.actions,
        )


class Fix29PersistenceVersionTest(unittest.TestCase):
    def test_versions_name_every_changed_judge_and_persistence_contract(self):
        self.assertEqual(
            GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
            "global-candidate-judgment/v7",
        )
        self.assertEqual(
            GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
            "global-candidate-judgment/v7",
        )
        self.assertEqual(
            GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
            "global-candidate-validation-envelope/v7",
        )
        self.assertIn(
            "failure-projection/v4",
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "terminal-record-schema/v3",
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertIn(
            "terminal-evidence/v2",
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(
            GLOBAL_FAILURE_PROJECTION_SCHEMA,
            "global-candidate-failure-projection/v4",
        )
        self.assertEqual(ACTION_STATE_SCHEMA, "recursive-analysis-actions/v15")
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v19",
        )
        self.assertEqual(
            EVALUATOR_REPORT_SCHEMA_VERSION,
            MODERN_REPORT_SCHEMA_VERSION,
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v18",
        )

    def test_old_global_envelope_and_report_are_rejected(self):
        request = sample_request()
        old_envelope = request.validation_envelope()
        old_envelope["schema_version"] = (
            "global-candidate-validation-envelope/v6"
        )
        with self.assertRaisesRegex(ValueError, "schema"):
            global_candidate_request_from_validation_envelope(
                old_envelope
            )

        old_report = RecursiveAttributionReport(
            case_id="fix29-old-report",
            objective="Reject old terminal semantics.",
        ).to_dict()
        old_report["schema_version"] = "recursive-attribution-report/v11"
        with self.assertRaisesRegex(ValueError, "schema"):
            RecursiveAttributionReport.from_dict(old_report)


if __name__ == "__main__":
    unittest.main()
