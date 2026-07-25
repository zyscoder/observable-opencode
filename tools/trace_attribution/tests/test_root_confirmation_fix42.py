from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_state import DefectState
from trace_attribution.causal_state import (
    RecursiveAttributionReport,
    RootConfirmation,
    confirmation_counterfactual_for,
    seed_defect_state,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.evidence_capsule import CandidateEvidenceCapsule
from trace_attribution.global_judge import (
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    global_candidate_request_from_validation_envelope,
    validate_global_candidate_payload,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.investigation import (
    InvestigationDirective,
    InvestigationResult,
)
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from trace_attribution.recursive_analyzer import (
    _assert_report_grounded_evidence,
)
from tools.trace_attribution.tests.test_causal_checkpoint import (
    CrashAfterDurableAction,
    InvestigationJudge,
    InterruptingTools,
    sample_config,
    sample_trace,
)
from tools.trace_attribution.tests.test_global_judge import (
    counterfactual,
    multi_root_payload,
    multi_root_request,
    payload,
    sample_request,
)
from tools.trace_attribution.tests.test_root_confirmation_fix41 import (
    InjectedReplayCheckpoint,
)
from tools.trace_attribution.tests.test_recursive_analyzer import (
    FusionScriptedJudge,
    observed_trace,
)


class SuccessfulInvestigationTools:
    artifact_bytes_used = 0
    max_artifact_bytes = 1_048_576

    def __init__(self) -> None:
        self.calls = 0

    def for_graph(self, graph, *, max_artifact_bytes):
        del graph
        self.max_artifact_bytes = max_artifact_bytes
        return self

    def execute(self, directive):
        self.calls += 1
        return InvestigationResult.success(
            directive,
            requested_refs=(directive.requested_by_ref,),
            resolved_refs=(directive.requested_by_ref,),
            payload={"summary": "Bounded investigation evidence."},
            byte_count=64,
        )


class GlobalAuthorityAndReplayOwnershipTest(unittest.TestCase):
    def test_open_root_candidates_require_a_recorded_path_to_active_seed(self):
        request = sample_request()
        disconnected_payload = request.capsules[0].to_dict()
        disconnected_payload["downstream_path"] = ["record:decision"]
        disconnected_payload["downstream_path_references"] = (
            disconnected_payload["downstream_path_references"][:1]
        )
        disconnected_payload["causal_path_edges"] = []
        disconnected_payload["validation_source"]["downstream_path"] = [
            "record:decision"
        ]
        disconnected = CandidateEvidenceCapsule.from_dict(
            disconnected_payload
        )
        request = GlobalCandidateJudgeRequest(
            case_id=request.case_id,
            objective=request.objective,
            analysis_perspective=request.analysis_perspective,
            seed_ref=request.seed_ref,
            active_defect=request.active_defect,
            active_focus_text=request.active_focus_text,
            active_focus_text_hash=request.active_focus_text_hash,
            start_refs=request.start_refs,
            capsules=(disconnected, *request.capsules[1:]),
            trace_health=request.trace_health,
        )

        self.assertNotIn(
            "record:decision",
            request.open_authored_root_candidate_refs,
        )

    def test_graph_rejects_coordinated_active_focus_and_defect_drift(self):
        graph, candidates, request = sample_request(return_context=True)
        envelope = request.validation_envelope()
        drifted_defect = DefectState.create(
            label=request.active_defect.label,
            expected=request.active_defect.expected,
            actual="A neighboring cleanup claim is interrupted.",
            mechanism=request.active_defect.mechanism,
            scope=request.active_defect.scope,
        )
        envelope["active_defect"] = drifted_defect.to_dict()
        envelope["active_focus_text"] = drifted_defect.actual
        envelope["active_focus_text_hash"] = active_focus_text_sha256(
            drifted_defect.actual
        )
        for capsule in envelope["candidate_evidence_capsules"]:
            capsule["defect_state"] = drifted_defect.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "active.*focus|active.*defect|graph.*seed",
        ):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=candidates,
                authoritative_objective=request.objective,
            )

    def test_graph_rejects_coordinated_objective_and_defect_drift(self):
        graph, candidates, request = sample_request(return_context=True)
        envelope = request.validation_envelope()
        drifted_objective = "Diagnose a neighboring cleanup requirement."
        drifted_defect = seed_defect_state(
            graph.nodes[request.seed_ref],
            drifted_objective,
        )
        envelope["objective"] = drifted_objective
        envelope["active_defect"] = drifted_defect.to_dict()
        envelope["active_focus_text"] = drifted_defect.actual
        envelope["active_focus_text_hash"] = active_focus_text_sha256(
            drifted_defect.actual
        )
        for capsule in envelope["candidate_evidence_capsules"]:
            capsule["defect_state"] = drifted_defect.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "objective.*authoritative|objective.*drift|objective.*request",
        ):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=candidates,
                authoritative_objective=request.objective,
            )

    def test_graph_rejects_missing_authoritative_candidate_capsule(self):
        graph, candidates, request = sample_request(return_context=True)
        envelope = request.validation_envelope()
        envelope["candidate_evidence_capsules"] = [
            capsule
            for capsule in envelope["candidate_evidence_capsules"]
            if capsule["candidate_ref"] != "record:decision"
        ]

        with self.assertRaisesRegex(
            ValueError,
            "authoritative.*candidate|capsule.*set|candidate.*coverage",
        ):
            global_candidate_request_from_validation_envelope(
                envelope,
                graph=graph,
                authoritative_candidates=candidates,
                authoritative_objective=request.objective,
            )

    def test_candidate_roots_requires_complete_open_competitor_rows(self):
        request = multi_root_request("record:a", "record:b")
        value = multi_root_payload(request, ["record:a"])
        competitor = next(
            item
            for item in value["assessments"]
            if item["candidate_ref"] == "record:b"
        )
        competitor.update(
            {
                "defect_status": "absent",
                "input_defect_status": "unknown",
                "output_defect_status": "absent",
                "causal_path_refs": [],
                "counterfactual": counterfactual(
                    "record:b", prevents_defect=False
                ),
                "causal_role": "unrelated",
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "complete.*comparison|open.*candidate|causal_path",
        ):
            validate_global_candidate_payload(value, request=request)

    def test_inconclusive_cannot_hide_decisive_root_matrix(self):
        request = multi_root_request("record:a", "record:b")
        value = multi_root_payload(request, ["record:a"])
        value["outcome"] = "inconclusive"
        value["selected_candidate_refs"] = []
        value["missing_evidence"] = []

        with self.assertRaisesRegex(
            ValueError,
            "inconclusive.*missing|decisive.*matrix|selectable",
        ):
            validate_global_candidate_payload(value, request=request)

    def test_contributing_condition_may_prevent_active_defect(self):
        request = sample_request()
        value = payload(outcome="candidate_roots", request=request)
        value["outcome"] = "needs_expansion"
        value["selected_candidate_refs"] = []
        value["expansion_requests"] = [
            {
                "anchor_ref": "record:decision",
                "context_kind": "upstream",
                "reason": "Determine whether the condition is independently necessary.",
            }
        ]
        value["missing_evidence"] = [
            "Independent necessity of the contributing condition is unresolved."
        ]
        value["assessments"][0]["causal_role"] = "contributing_condition"

        judgment = validate_global_candidate_payload(value, request=request)

        self.assertEqual(judgment.outcome, "needs_expansion")
        self.assertEqual(
            judgment.assessments[0].counterfactual["causal_effect"],
            "prevents_defect",
        )

    def test_report_validation_rejects_capsule_deleted_from_global_envelope(self):
        graph = TraceGraph.from_trace(observed_trace())
        judge = FusionScriptedJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision introduced the omission.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=("record:decision",),
                )
            },
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            graph,
            start_refs=("record:observed_defect",),
            objective="Find the primary trace-visible root.",
        )
        payload = report.to_dict()
        judgment = payload["seed_results"][0]["global_judgment"]
        envelope = judgment["validation_envelope"]
        removed_ref = "record:change"
        envelope["candidate_evidence_capsules"] = [
            capsule
            for capsule in envelope["candidate_evidence_capsules"]
            if capsule["candidate_ref"] != removed_ref
        ]
        judgment["assessments"] = [
            assessment
            for assessment in judgment["assessments"]
            if assessment["candidate_ref"] != removed_ref
        ]
        for assessment in judgment["assessments"]:
            assessment["compared_candidate_refs"] = [
                ref
                for ref in assessment["compared_candidate_refs"]
                if ref != removed_ref
            ]
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "capsule.*pass|global.*capsule|candidate.*coverage",
        ):
            _assert_report_grounded_evidence(
                graph,
                tampered,
                label="fix42 deleted global capsule",
            )

    def test_completed_investigation_action_persists_visit_binding(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "fix42-visit.checkpoint"
            config = sample_config()
            graph = TraceGraph.from_trace(sample_trace())
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=SuccessfulInvestigationTools(),
                    checkpoint=CrashAfterDurableAction(
                        root,
                        operation="investigation_completed",
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            checkpoint = CheckpointBundle(root).restore(
                expected_config=config
            )
            started = next(
                action
                for action in checkpoint.actions
                if action["operation"] == "investigation_started"
            )
            completed = next(
                action
                for action in checkpoint.actions
                if action["operation"] == "investigation_completed"
            )

            self.assertEqual(
                completed["payload"]["visit_key"],
                started["payload"]["visit_key"],
            )

    def test_completed_investigation_replay_rejects_another_visit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "fix42-other-visit.checkpoint"
            config = sample_config()
            graph = TraceGraph.from_trace(sample_trace())
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=SuccessfulInvestigationTools(),
                    checkpoint=CrashAfterDurableAction(
                        root,
                        operation="investigation_completed",
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            checkpoint = CheckpointBundle(root).restore(
                expected_config=config
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            completed = next(
                action
                for action in actions
                if action["operation"] == "investigation_completed"
            )
            completed["payload"]["visit_key"] = "visit:another"
            tampered = replace(checkpoint, actions=tuple(actions))

            with self.assertRaisesRegex(
                ValueError,
                "investigation.*visit|replay.*ownership",
            ):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=InterruptingTools(interrupt=False),
                    checkpoint=InjectedReplayCheckpoint(tampered, root),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_completed_investigation_replay_rejects_another_directive_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "fix42.checkpoint"
            config = sample_config()
            graph = TraceGraph.from_trace(sample_trace())
            first_tools = SuccessfulInvestigationTools()
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=first_tools,
                    checkpoint=CrashAfterDurableAction(
                        root,
                        operation="investigation_completed",
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first_tools.calls, 1)
            checkpoint = CheckpointBundle(root).restore(
                expected_config=config
            )
            actions = copy.deepcopy(list(checkpoint.actions))
            completed = next(
                action
                for action in actions
                if action["operation"] == "investigation_completed"
            )
            other_directive = InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:only"},
                requested_by_ref="record:only",
                hypothesis_id="another-seed-hypothesis",
                reason="Inspect evidence for another seed.",
            )
            other_result = InvestigationResult.success(
                other_directive,
                requested_refs=("record:only",),
                resolved_refs=("record:only",),
                payload={"summary": "Evidence owned by another directive."},
                byte_count=64,
            )
            completed["payload"]["directive_id"] = (
                other_directive.directive_id
            )
            completed["payload"]["status"] = other_result.status
            completed["payload"]["result"] = other_result.to_dict()
            tampered = replace(checkpoint, actions=tuple(actions))

            with self.assertRaisesRegex(
                ValueError,
                "investigation.*directive|action.*ownership|replay.*binding",
            ):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=InterruptingTools(interrupt=False),
                    checkpoint=InjectedReplayCheckpoint(tampered, root),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )


if __name__ == "__main__":
    unittest.main()
