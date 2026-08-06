from __future__ import annotations

import unittest
import tempfile
import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    FactorRoleJudgment,
    factor_role_request_identity,
)
from trace_attribution.causal_state import (
    DefectState,
    LocalStateOwner,
    RootConfirmation,
    RecursiveAttributionReport,
    confirmation_counterfactual_for,
    semantic_visit_key,
)
from trace_attribution.global_judge import GlobalCandidateJudgment
from trace_attribution.graph import TraceGraph
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tools.trace_attribution.tests.test_recursive_analyzer import (
    FusionScriptedJudge,
    observed_trace,
)


class FactorClosureJudge(FusionScriptedJudge):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.factor_requests = []

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        result = super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        assessments = []
        for assessment in result.value.assessments:
            if assessment.candidate_ref == "record:change":
                assessments.append(
                    replace(
                        assessment,
                        causal_role="outcome_evidence",
                        responsibility="none",
                        candidate_phase="intermediate",
                        obligation_status_before="unknown",
                        obligation_status_after="unknown",
                        repair_window_effect="remained_open",
                        failure_mode="none",
                        obligation_refs=(),
                        contribution_mechanism=None,
                        reason="The change materializes the downstream outcome.",
                    )
                )
                continue
            if assessment.candidate_ref == "record:timeout":
                capsule = next(
                    item
                    for item in request.capsules
                    if item.candidate_ref == assessment.candidate_ref
                )
                assessments.append(
                    replace(
                        assessment,
                        defect_status="absent",
                        input_defect_status="absent",
                        output_defect_status="absent",
                        causal_path_refs=tuple(capsule.downstream_path),
                        counterfactual={
                            "intervention_ref": assessment.candidate_ref,
                            "intervention_kind": (
                                "replace_with_semantically_correct_behavior"
                            ),
                            "predicted_defect_status": "present",
                            "causal_effect": "does_not_prevent_defect",
                        },
                        causal_role="unrelated",
                        responsibility="none",
                        candidate_phase="intermediate",
                        obligation_status_before="unknown",
                        obligation_status_after="unknown",
                        repair_window_effect="remained_open",
                        failure_mode="none",
                        obligation_refs=(),
                        contribution_mechanism=None,
                        reason=(
                            "The timeout followed the implementation failure "
                            "and appears unrelated."
                        ),
                        confidence=0.75,
                    )
                )
                continue
            if assessment.candidate_ref != "record:context":
                assessments.append(assessment)
                continue
            capsule = next(
                item
                for item in request.capsules
                if item.candidate_ref == assessment.candidate_ref
            )
            assessments.append(
                replace(
                    assessment,
                    defect_status="present",
                    input_defect_status="unknown",
                    output_defect_status="present",
                    causal_path_refs=tuple(capsule.downstream_path),
                    counterfactual={
                        "intervention_ref": assessment.candidate_ref,
                        "intervention_kind": (
                            "replace_with_semantically_correct_behavior"
                        ),
                        "predicted_defect_status": "absent",
                        "causal_effect": "prevents_defect",
                    },
                    causal_role="contributing_condition",
                    responsibility="shared",
                    candidate_phase="planning",
                    obligation_status_before="unknown",
                    obligation_status_after="unknown",
                    repair_window_effect="remained_open",
                    failure_mode="omission_enabling_condition",
                    obligation_refs=(),
                    contribution_mechanism={
                        "type": "scope_narrowing",
                        "target_ref": request.seed_ref,
                        "effect": (
                            "The context narrowed the implementation scope "
                            "reaching the active defect."
                        ),
                        "evidence_refs": (capsule.candidate_ref,),
                    },
                    reason=(
                        "The context enabled the incomplete implementation "
                        "without independently introducing it."
                    ),
                    confidence=0.8,
                )
            )
        return BoundedJudgeCallResult(
            replace(
                result.value,
                assessments=tuple(assessments),
            ),
            result.physical_requests,
        )

    def judge_factor_role(self, request):
        self.factor_requests.append(request)
        scripted = self.confirmations.get(request.candidate_ref)
        status = scripted.status if scripted is not None else "unknown"
        if status == "confirmed":
            necessity_status = "necessary"
            factor_role = "unknown"
        elif status == "rejected":
            necessity_status = "not_necessary"
            factor_role = scripted.factor_role
            if factor_role not in {
                "contributing_condition",
                "amplifying_factor",
                "downstream_materialization",
                "unrelated",
            }:
                factor_role = "unrelated"
        else:
            necessity_status = "unknown"
            factor_role = "unknown"
        mechanism = {}
        if factor_role == "contributing_condition":
            mechanism = {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": "enabling_condition",
                "source_ref": request.candidate_ref,
                "target_ref": request.recursive_path[-1],
                "effect": "increased_defect_likelihood",
            }
        elif factor_role == "amplifying_factor":
            mechanism = {
                "schema": "factor-role-mechanism/v1",
                "mechanism_type": "amplification",
                "source_ref": request.candidate_ref,
                "target_ref": request.recursive_path[-1],
                "effect": "increased_defect_severity",
            }
        predicted_effect = {
            ("necessary", "unknown"): "prevents_defect",
            ("not_necessary", "contributing_condition"): (
                "reduces_defect_likelihood"
            ),
            ("not_necessary", "amplifying_factor"): (
                "reduces_defect_severity"
            ),
            ("not_necessary", "downstream_materialization"): (
                "defect_still_present_without_materialization"
            ),
            ("not_necessary", "unrelated"): (
                "no_grounded_causal_influence_established"
            ),
            ("unknown", "unknown"): "insufficient_grounded_evidence",
        }[(necessity_status, factor_role)]
        return FactorRoleJudgment(
            candidate_ref=request.candidate_ref,
            necessity_status=necessity_status,
            factor_role=factor_role,
            reason=(
                scripted.reason
                if scripted is not None
                else "The independent factor role remains unresolved."
            ),
            confidence=(
                scripted.confidence
                if scripted is not None
                else 0.0
            ),
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
            hypothesis_semantic_hash=request.hypothesis_semantic_hash,
            defect_fingerprint=request.defect_state.fingerprint,
            seed_binding_identity=request.seed_binding_identity,
            analysis_perspective=request.analysis_perspective,
            request_identity=factor_role_request_identity(request),
        )


def observed_trace_with_timeout():
    trace = observed_trace(branching=True)
    trace["records"].insert(
        -1,
        {
            "record_id": "timeout",
            "component": "harness",
            "event_type": "run.interruption",
            "data": {"reason": "request deadline exceeded"},
        },
    )
    observed = next(
        item
        for item in trace["records"]
        if item["record_id"] == "observed_defect"
    )
    observed["source_refs"] = [
        "record:change",
        "record:timeout",
    ]
    trace["dataflow_edges"].append(
        {
            "from": {"type": "record", "id": "timeout"},
            "to": {"type": "record", "id": "observed_defect"},
            "relation": "interruption_amplified_evaluation",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        }
    )
    return trace


class GlobalNonRootSchedulingTest(unittest.TestCase):
    def test_non_root_quota_requires_canonical_global_authority(self):
        root_refs = (
            "record:root_a",
            "record:root_b",
            "record:root_c",
            "record:root_d",
        )
        factor_refs = (
            "record:factor_a",
            "record:factor_b",
            "record:factor_c",
            "record:factor_d",
        )
        graph = TraceGraph.from_trace(
            {
                "case_id": "dual-confirmation-quota",
                "records": [
                    {
                        "record_id": ref.removeprefix("record:"),
                        "component": "agent",
                        "event_type": "decision",
                        "data": {"rationale": "Review {0}.".format(ref)},
                    }
                    for ref in (*root_refs, *factor_refs)
                ],
                "dataflow_edges": [],
            }
        )
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:defect",),
            objective="Bound root and factor review independently.",
            analysis_perspective="Improve Agent repository reasoning.",
        )
        defect_state = DefectState.create(
            "dual quota",
            "Review three roots and three factors.",
            "Eight candidates were offered.",
            "bounded confirmation",
            "confirmation queue",
        )
        state.defect_states[defect_state.fingerprint] = defect_state

        def enqueue(ref, review_scope):
            hypothesis = state.ledger.create(
                "Review {0}.".format(ref),
                ref,
                defect_state,
                seed_binding_identity="seed-binding",
            )
            state.introduction_bindings.append(
                {
                    "candidate_ref": ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis.semantic_hash,
                    "seed_binding_identity": "seed-binding",
                }
            )
            return state.enqueue_confirmation(
                {
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "hypothesis_semantic_hash": hypothesis.semantic_hash,
                    "candidate_ref": ref,
                    "defect_fingerprint": defect_state.fingerprint,
                    "seed_binding_identity": "seed-binding",
                    "recursive_path": [ref],
                    "checked_evidence_refs": [],
                    "task_obligations": [],
                    "analysis_perspective": (
                        "Improve Agent repository reasoning."
                    ),
                    "status": "queued",
                    "review_scope": review_scope,
                    "owner": LocalStateOwner.create(
                        seed_binding_identity="seed-binding",
                        hypothesis_id=hypothesis.hypothesis_id,
                        visit_key=semantic_visit_key(
                            ref,
                            defect_state,
                            hypothesis.semantic_hash,
                            "seed-binding",
                        ),
                        occurrence_key="confirmation_queue",
                    ).to_dict(),
                }
            )

        self.assertEqual(
            [enqueue(ref, "root") for ref in root_refs],
            [True, True, True, False],
        )
        with self.assertRaisesRegex(
            ValueError,
            "canonical Global factor assessment",
        ):
            enqueue(factor_refs[0], "non_root")
        state.validate_confirmation_queue_bound()
        self.assertEqual(len(state.confirmation_queue), 3)
        missing_origin = copy.deepcopy(state.confirmation_queue[0])
        state.confirmation_queue[0].pop("origin")
        with self.assertRaisesRegex(
            ValueError,
            "pending confirmation identity schema mismatch",
        ):
            state.validate_confirmation_queue_bound()
        state.confirmation_queue[0] = missing_origin

    def test_global_condition_is_independently_judged_and_published(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.rejected(
                    "record:context",
                    "The context enabled the omission but was not necessary.",
                    evidence_refs=["record:context"],
                    factor_role="contributing_condition",
                    confidence=0.8,
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(
            [item.candidate_ref for item in judge.confirmation_requests],
            ["record:decision"],
        )
        self.assertEqual(
            [item.candidate_ref for item in judge.factor_requests],
            ["record:context"],
        )
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:decision"],
        )
        self.assertEqual(
            [item.node_ref for item in report.contributing_conditions],
            ["record:context"],
        )
        self.assertNotIn(
            "record:context",
            [item.ref for item in report.introduction_candidates],
        )
        context_request = judge.factor_requests[0].factual_dict()
        self.assertNotIn("review_scope", context_request)
        self.assertNotIn(
            "contributing_condition",
            str(context_request),
        )
        journal_scope = {
            item["candidate_ref"]: item["review_scope"]
            for item in report.metadata["confirmation_journal"]
        }
        self.assertEqual(
            journal_scope,
            {"record:decision": "root"},
        )
        self.assertEqual(
            dict(report.metadata["confirmation_scope_counts"]),
            {"root": 1, "non_root": 1},
        )
        context_projection = next(
            item
            for item in report.metadata[
                "factor_role_action_projections"
            ]
            if item["candidate_ref"] == "record:context"
        )
        self.assertEqual(
            context_projection["judgment"]["factor_role"],
            "contributing_condition",
        )
        self.assertEqual(
            context_projection["origin"],
            "global_candidate_factor_assessment",
        )
        legacy_shape = report.to_dict()
        legacy_shape["schema_version"] = "recursive-attribution-report/v20"
        with self.assertRaisesRegex(
            ValueError,
            "unsupported report schema_version",
        ):
            RecursiveAttributionReport.from_dict(legacy_shape)
        missing_audit = report.to_dict()
        del missing_audit["metadata"][
            "factor_confirmation_enqueue_gaps"
        ]
        with self.assertRaisesRegex(
            ValueError,
            "modern report metadata.*required",
        ):
            RecursiveAttributionReport.from_dict(missing_audit)

    def test_independent_factor_judgment_may_correct_global_role(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.rejected(
                    "record:context",
                    "The context enabled the omission but was not necessary.",
                    evidence_refs=["record:context"],
                    factor_role="contributing_condition",
                    confidence=0.8,
                ),
                "record:timeout": RootConfirmation.rejected(
                    "record:timeout",
                    "The timeout amplified the visible evaluation failure.",
                    evidence_refs=["record:timeout"],
                    factor_role="amplifying_factor",
                    confidence=0.85,
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace_with_timeout()),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        requested_refs = [
            item.candidate_ref for item in judge.confirmation_requests
        ]
        self.assertEqual(requested_refs, ["record:decision"])
        self.assertNotIn("record:change", requested_refs)
        self.assertEqual(
            [item.candidate_ref for item in judge.factor_requests],
            ["record:context", "record:timeout"],
        )
        for request in judge.factor_requests:
            self.assertEqual(
                [
                    item["candidate_ref"]
                    for item in request.confirmed_root_summaries
                ],
                ["record:decision"],
            )
        self.assertEqual(
            [item.node_ref for item in report.amplifying_factors],
            ["record:timeout"],
        )
        timeout_assessment = next(
            item
            for item in report.seed_results[0].global_judgment["assessments"]
            if item["candidate_ref"] == "record:timeout"
        )
        self.assertEqual(timeout_assessment["causal_role"], "unrelated")
        timeout_judgment = next(
            item
            for item in report.metadata["factor_role_judgments"]
            if item["candidate_ref"] == "record:timeout"
        )
        self.assertEqual(
            timeout_judgment["factor_role"],
            "amplifying_factor",
        )

    def test_non_root_necessity_escalates_without_direct_root_promotion(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt=(
                        "The complete compatibility contract is documented "
                        "here."
                    ),
                    reason=(
                        "The context independently constrained discovery and "
                        "was necessary for the omission."
                    ),
                    counterfactual=confirmation_counterfactual_for(
                        "record:context",
                        "confirmed",
                    ),
                    confidence=0.82,
                    evidence_refs=["record:context"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(
            {
                item.node_ref
                for item in (*report.confirmed_roots, *report.co_roots)
            },
            {"record:decision", "record:context"},
        )
        escalation = next(
            item
            for item in report.metadata["confirmation_queue"]
            if item["candidate_ref"] == "record:context"
            and item["review_scope"] == "root"
        )
        self.assertEqual(
            escalation["origin"]["kind"],
            "factor_role_escalation",
        )
        context_judgment = next(
            item
            for item in report.metadata["factor_role_judgments"]
            if item["candidate_ref"] == "record:context"
        )
        self.assertEqual(
            (
                context_judgment["necessity_status"],
                context_judgment["factor_role"],
            ),
            ("necessary", "unknown"),
        )

    def test_non_root_requires_mutual_support_from_every_published_root(self):
        seed = "seed:multi-root"
        defect = DefectState.create(
            "multi root",
            "All roots are mutually necessary.",
            "One candidate has only partial support.",
            "partial co-root graph",
            "root publication",
        )

        def confirmed(ref, hypothesis_id):
            return RootConfirmation(
                candidate_ref=ref,
                status="confirmed",
                excerpt=ref,
                reason="{0} is necessary.".format(ref),
                counterfactual=confirmation_counterfactual_for(
                    ref,
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=[ref],
                counterfactual_status="supports_causality",
                hypothesis_id=hypothesis_id,
                hypothesis_semantic_hash="hash:{0}".format(ref),
                defect_fingerprint=defect.fingerprint,
                recursive_path=[ref],
                analysis_perspective="Improve Agent reasoning.",
                seed_binding_identity=seed,
                factor_role="necessary_cause",
            )

        root_a = confirmed("record:root_a", "hyp:root_a")
        root_b = confirmed("record:root_b", "hyp:root_b")
        candidate = confirmed("record:candidate", "hyp:candidate")

        def comparison(target, status):
            return {
                "confirmation_identity": target.confirmation_identity,
                "candidate_ref": target.candidate_ref,
                "seed_binding_identity": target.seed_binding_identity,
                "status": status,
                "requires_independent_confirmation": True,
                "reason": "Independent pairwise comparison.",
            }

        root_a = replace(
            root_a,
            competitor_comparisons=(
                comparison(candidate, "co_root"),
            ),
        )
        root_b = replace(
            root_b,
            competitor_comparisons=(
                comparison(candidate, "outperformed"),
            ),
        )
        candidate = replace(
            candidate,
            competitor_comparisons=(
                comparison(root_a, "co_root"),
                comparison(root_b, "outperformed"),
            ),
        )
        state = SimpleNamespace(
            confirmed_roots=[
                SimpleNamespace(confirmation=root_a.to_dict()),
                SimpleNamespace(confirmation=root_b.to_dict()),
            ],
            co_roots=[],
        )

        self.assertFalse(
            AgenticRecursiveAnalyzer.
            _non_root_confirmation_has_mutual_co_root_support(
                state,
                candidate,
            )
        )

    def test_non_root_necessity_fact_does_not_erase_confirmed_root(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt=(
                        "The complete compatibility contract is documented "
                        "here."
                    ),
                    reason=(
                        "The context verifier independently claimed a "
                        "necessary-cause role."
                    ),
                    counterfactual=confirmation_counterfactual_for(
                        "record:context",
                        "confirmed",
                    ),
                    confidence=0.82,
                    evidence_refs=["record:context"],
                ),
            },
        )
        judge.comparison_statuses = {
            ("record:decision", "record:context"): "outperformed",
            ("record:context", "record:decision"): "outperformed",
        }

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(report.analysis_outcome, "confirmed_root")
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:decision"],
        )
        self.assertEqual(report.co_roots, ())
        self.assertEqual(
            list(report.metadata["factor_confirmation_conflicts"]),
            [],
        )
        context_judgment = next(
            item
            for item in report.metadata["factor_role_judgments"]
            if item["candidate_ref"] == "record:context"
        )
        self.assertEqual(
            context_judgment["necessity_status"],
            "necessary",
        )

    def test_non_root_necessity_without_published_root_becomes_primary(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.unknown(
                    "record:decision",
                    "The selected root could not be confirmed.",
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.confirmed(
                    "record:context",
                    excerpt=(
                        "The complete compatibility contract is documented "
                        "here."
                    ),
                    reason=(
                        "The non-root verifier claimed a necessary-cause role."
                    ),
                    counterfactual=confirmation_counterfactual_for(
                        "record:context",
                        "confirmed",
                    ),
                    confidence=0.82,
                    evidence_refs=["record:context"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:context"],
        )
        self.assertEqual(report.analysis_outcome, "confirmed_root")
        self.assertEqual(report.co_roots, ())
        self.assertEqual(
            [item.candidate_ref for item in judge.factor_requests],
            ["record:context"],
        )
        self.assertEqual(
            judge.factor_requests[0].confirmed_root_summaries,
            (),
        )
        self.assertEqual(
            next(
                item
                for item in report.metadata["factor_role_judgments"]
                if item["candidate_ref"] == "record:context"
            )["necessity_status"],
            "necessary",
        )
        self.assertEqual(
            list(report.metadata["factor_confirmation_conflicts"]),
            [],
        )
        escalation = next(
            item
            for item in report.metadata["confirmation_queue"]
            if isinstance(item.get("origin"), Mapping)
        )
        self.assertEqual(escalation["origin"]["kind"], "factor_role_escalation")

    def test_non_root_request_excludes_unconfirmed_root_candidates(self):
        for root_confirmation in (
            RootConfirmation.unknown(
                "record:decision",
                "The selected root could not be confirmed.",
                evidence_refs=["record:decision"],
            ),
            RootConfirmation.rejected(
                "record:decision",
                "The selected root was independently rejected.",
                evidence_refs=["record:decision"],
                factor_role="unrelated",
                confidence=0.8,
            ),
        ):
            with self.subTest(root_status=root_confirmation.status):
                judge = FactorClosureJudge(
                    global_outcome="candidate_roots",
                    confirmations={
                        "record:decision": root_confirmation,
                        "record:context": RootConfirmation.rejected(
                            "record:context",
                            "The context was unrelated.",
                            evidence_refs=["record:context"],
                            factor_role="unrelated",
                            confidence=0.8,
                        ),
                    },
                )

                AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                ).analyze(
                    TraceGraph.from_trace(observed_trace(branching=True)),
                    start_refs=["record:observed_defect"],
                    objective="Find why the implementation omitted the method.",
                    analysis_perspective="Improve Agent repository reasoning.",
                )

                self.assertEqual(
                    [item.candidate_ref for item in judge.factor_requests],
                    ["record:context"],
                )
                self.assertEqual(
                    judge.factor_requests[0].confirmed_root_summaries,
                    (),
                )

    def test_unknown_non_root_role_does_not_erase_confirmed_root(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.unknown(
                    "record:context",
                    "The supplied context does not decide its causal role.",
                    evidence_refs=["record:context"],
                ),
            },
        )

        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(report.analysis_outcome, "confirmed_root")
        self.assertEqual(report.seed_results[0].outcome, "confirmed_root")
        self.assertEqual(
            [item.node_ref for item in report.confirmed_roots],
            ["record:decision"],
        )
        self.assertEqual(
            [
                (item["candidate_ref"], item["factor_role"])
                for item in report.metadata["factor_confirmation_gaps"]
            ],
            [("record:context", "unknown")],
        )
        context_judgment = next(
            item
            for item in report.metadata["factor_role_judgments"]
            if item["candidate_ref"] == "record:context"
        )
        self.assertEqual(context_judgment["factor_role"], "unknown")
        self.assertEqual(
            [
                (
                    item["candidate_ref"],
                    item["judgment_identity"],
                    item["failure_classification"],
                )
                for item in report.metadata["factor_role_gaps"]
            ],
            [
                (
                    "record:context",
                    context_judgment["judgment_identity"],
                    "none",
                )
            ],
        )
        self.assertEqual(
            [item.candidate_ref for item in report.confirmations],
            ["record:decision"],
        )

    def test_selected_root_unknown_cannot_be_forged_as_factor_gap(self):
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.unknown(
                    "record:decision",
                    "The selected root could not be confirmed.",
                    evidence_refs=["record:decision"],
                ),
                "record:context": RootConfirmation.rejected(
                    "record:context",
                    "The context was unrelated.",
                    evidence_refs=["record:context"],
                    factor_role="unrelated",
                    confidence=0.8,
                ),
            },
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(observed_trace(branching=True)),
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )
        forged = copy.deepcopy(report.to_dict())
        decision_confirmation = next(
            item
            for item in forged["confirmations"]
            if item["candidate_ref"] == "record:decision"
        )
        for collection_name in (
            "confirmation_queue",
            "confirmation_journal",
            "confirmation_action_projection",
        ):
            decision_item = next(
                item
                for item in forged["metadata"][collection_name]
                if item["candidate_ref"] == "record:decision"
            )
            decision_item["review_scope"] = "non_root"
            decision_item["origin"] = (
                "global_candidate_factor_assessment"
            )
        forged["metadata"]["factor_confirmation_gaps"].append(
            {
                "candidate_ref": "record:decision",
                "reason": decision_confirmation["reason"],
                "confidence": decision_confirmation["confidence"],
                "evidence_refs": decision_confirmation["evidence_refs"],
                "recursive_path": decision_confirmation["recursive_path"],
                "necessity_status": "unknown",
                "factor_role": "unknown",
                "request_identity": decision_confirmation[
                    "confirmation_identity"
                ],
                "judgment_identity": decision_confirmation[
                    "response_identity"
                ],
                "provenance": {},
            }
        )

        with self.assertRaisesRegex(
            ValueError,
            "factor confirmation.*Global non-root assessment",
        ):
            RecursiveAttributionReport.from_dict(forged)

    def test_failed_non_root_enqueue_is_published_as_coverage_gap(self):
        trace = observed_trace(branching=True)
        objective = "Find why the implementation omitted the method."
        perspective = "Improve Agent repository reasoning."
        start_refs = ["record:observed_defect"]
        judge = FactorClosureJudge(
            global_outcome="candidate_roots",
            confirmations={
                "record:decision": RootConfirmation.confirmed(
                    "record:decision",
                    excerpt=(
                        "Implement only the methods found in the first search."
                    ),
                    reason="The decision independently introduced the defect.",
                    counterfactual=confirmation_counterfactual_for(
                        "record:decision",
                        "confirmed",
                    ),
                    confidence=0.9,
                    evidence_refs=["record:decision"],
                ),
            },
        )
        original_enqueue = RecursiveAnalysisState.enqueue_confirmation

        def reject_non_root(state, value):
            if str(value.get("review_scope") or "root") == "non_root":
                return False
            return original_enqueue(state, value)

        config = build_checkpoint_config(
            trace=trace,
            case_id=str(trace.get("case_id") or ""),
            objective=objective,
            analysis_perspective=perspective,
            start_refs=start_refs,
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:factor-enqueue-gap",
            cache_identity="cache:factor-enqueue-gap",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://factor-enqueue-gap",
                "provider_error_threshold": 3,
                "fusion_mode": "retrieval-global",
            },
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-gap.checkpoint"
            with patch.object(
                RecursiveAnalysisState,
                "enqueue_confirmation",
                new=reject_non_root,
            ):
                report = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    checkpoint=CheckpointBundle(checkpoint_root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective=objective,
                    analysis_perspective=perspective,
                )
            replay_judge = FactorClosureJudge(
                global_outcome="candidate_roots",
                confirmations={},
            )
            replay = AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=objective,
                analysis_perspective=perspective,
            )

        self.assertEqual(replay.to_dict(), report.to_dict())
        self.assertEqual(replay_judge.global_requests, [])
        self.assertEqual(replay_judge.confirmation_requests, [])
        self.assertEqual(report.analysis_outcome, "confirmed_root")
        self.assertEqual(
            list(report.metadata["factor_confirmation_enqueue_gaps"]),
            [
                {
                    "candidate_ref": "record:context",
                    "defect_fingerprint": (
                        report.seed_results[0].defect_fingerprint
                    ),
                    "seed_binding_identity": (
                        report.confirmed_roots[0].confirmation[
                            "seed_binding_identity"
                        ]
                    ),
                    "causal_role": "contributing_condition",
                    "reason": (
                        "The bounded non-root confirmation queue rejected "
                        "the candidate before independent review."
                    ),
                    "origin": "global_candidate_factor_assessment",
                }
            ],
        )

    def test_completed_factor_confirmation_replays_without_provider_calls(self):
        trace = observed_trace(branching=True)
        objective = "Find why the implementation omitted the method."
        perspective = "Improve Agent repository reasoning."
        start_refs = ["record:observed_defect"]
        confirmations = {
            "record:decision": RootConfirmation.confirmed(
                "record:decision",
                excerpt="Implement only the methods found in the first search.",
                reason="The decision independently introduced the defect.",
                counterfactual=confirmation_counterfactual_for(
                    "record:decision",
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=["record:decision"],
            ),
            "record:context": RootConfirmation.rejected(
                "record:context",
                "The context enabled the omission but was not necessary.",
                evidence_refs=["record:context"],
                factor_role="contributing_condition",
                confidence=0.8,
            ),
        }
        config = build_checkpoint_config(
            trace=trace,
            case_id=str(trace.get("case_id") or ""),
            objective=objective,
            analysis_perspective=perspective,
            start_refs=start_refs,
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:factor-closure",
            cache_identity="cache:factor-closure",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://factor-closure",
                "provider_error_threshold": 3,
                "fusion_mode": "retrieval-global",
            },
        )

        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "factor-closure.checkpoint"
            first_judge = FactorClosureJudge(
                global_outcome="candidate_roots",
                confirmations=confirmations,
            )
            first = AgenticRecursiveAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=objective,
                analysis_perspective=perspective,
            )

            replay_judge = FactorClosureJudge(
                global_outcome="candidate_roots",
                confirmations=confirmations,
            )
            replay = AgenticRecursiveAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=objective,
                analysis_perspective=perspective,
            )

        self.assertEqual(replay.to_dict(), first.to_dict())
        self.assertEqual(replay_judge.global_requests, [])
        self.assertEqual(replay_judge.confirmation_requests, [])
        self.assertEqual(replay_judge.factor_requests, [])
        self.assertEqual(
            [item.node_ref for item in replay.contributing_conditions],
            ["record:context"],
        )
        self.assertEqual(
            [
                (item["candidate_ref"], item["factor_role"])
                for item in replay.metadata["factor_role_judgments"]
            ],
            [("record:context", "contributing_condition")],
        )


if __name__ == "__main__":
    unittest.main()
