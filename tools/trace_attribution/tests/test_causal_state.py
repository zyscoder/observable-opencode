import math
import unittest
from dataclasses import replace

from trace_attribution.candidate_budget import select_global_candidates
from trace_attribution.causal_judge import (
    factor_role_request_projection_identity,
)
from trace_attribution.causal_state import (
    ActiveFailureRoleBinding,
    AttributionHypothesis,
    CausalCandidate,
    CausalFactor,
    CausalStepJudgment,
    ConfirmedRoot,
    DefectState,
    FactorRoleJudgment,
    FrontierItem,
    HypothesisEvidence,
    LocalStateOwner,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RejectedCandidate,
    RootConfirmation,
    SeedAttributionResult,
    active_failure_signature_for,
    canonical_confirmed_root_publication,
    canonical_factor_role_publication,
    canonical_ranked_root_publications,
    confirmation_counterfactual_for,
    seed_binding_identity_for,
    semantic_visit_key,
)
from trace_attribution.models import TraceNode


def sample_defect_state():
    return DefectState.create(
        label="missing_parse_namespace_object_contract",
        expected="compatibility method exists",
        actual="method is absent",
        mechanism="implementation omission",
        scope="parser_contract_recovery",
    )


def sample_frontier_item():
    defect_state = sample_defect_state()
    return FrontierItem.create(
        node_ref="record:dec_85",
        defect_state=defect_state,
        downstream_path=["record:observed", "record:change", "record:dec_85"],
        hypothesis_id="hyp_a",
        hypothesis_semantic_hash="hyp_semantic_a",
        depth=2,
        candidate_source="confirmed_edge",
        priority=0.85,
        checked_evidence_refs=["record:change"],
        graph_position=17,
    )


def report_seed(
    outcome,
    *,
    start_ref="record:observed",
    defect_state=None,
    root_refs=(),
    confirmation_identities=(),
):
    defect_state = defect_state or sample_defect_state()
    return SeedAttributionResult(
        start_ref=start_ref,
        defect_fingerprint=defect_state.fingerprint,
        defect_state=defect_state,
        outcome=outcome,
        confirmed_root_refs=root_refs,
        confirmation_identities=confirmation_identities,
        missing_evidence=("seed-local evidence is incomplete",)
        if outcome == "evidence_gap"
        else (),
    )


def root_candidate(root):
    return CausalCandidate(
        ref=root.node_ref,
        node=TraceNode(
            ref=root.node_ref,
            record_id=root.node_ref.removeprefix("record:"),
            component=root.component,
            event_type=root.event_type,
        ),
        source="canonical-test-fixture",
        evidence_refs=root.evidence_refs,
    )


def canonical_test_root(root, confirmation):
    seed_ref = confirmation.recursive_path[-1]
    return canonical_confirmed_root_publication(
        confirmation=confirmation,
        defect_state=root.defect_state,
        candidate_node=root_candidate(root).node,
        seed_start_ref=seed_ref,
    )


def modern_root(root, *, hypothesis_id="hyp:test-root", semantic_hash="semantic:test-root"):
    seed_ref = (
        root.observed_defect_refs[0]
        if root.observed_defect_refs
        else "record:observed"
    )
    path = root.recursive_path or (root.node_ref, seed_ref)
    seed_ref = path[-1]
    confirmation = replace(
        RootConfirmation.confirmed(
            root.node_ref,
            excerpt="The decision ended discovery.",
            reason=root.reason,
            counterfactual=confirmation_counterfactual_for(
                root.node_ref,
                "confirmed",
            ),
            confidence=root.confidence,
            evidence_refs=[root.node_ref],
        ),
        hypothesis_id=hypothesis_id,
        hypothesis_semantic_hash=semantic_hash,
        defect_fingerprint=root.defect_state.fingerprint,
        recursive_path=path,
        seed_binding_identity=seed_binding_identity_for(
            seed_ref, root.defect_state.fingerprint
        ),
    )
    return canonical_test_root(root, confirmation)


def confirmation_for(root):
    if not root.confirmation:
        bound = modern_root(root)
        for name in root.__dataclass_fields__:
            object.__setattr__(root, name, getattr(bound, name))
    return RootConfirmation.from_dict(dict(root.confirmation))


def root_seed_fields(*roots):
    grouped = {}
    candidates = {}
    for root in roots:
        confirmation = confirmation_for(root)
        candidate = root_candidate(root)
        candidates.setdefault(candidate.ref, candidate)
        seed_ref = root.recursive_path[-1]
        key = (seed_ref, root.defect_state.fingerprint)
        entry = grouped.setdefault(
            key,
            {
                "defect_state": root.defect_state,
                "root_refs": [],
                "confirmation_identities": [],
            },
        )
        entry["root_refs"].append(root.node_ref)
        entry["confirmation_identities"].append(
            confirmation.confirmation_identity
        )
    return {
        "start_refs": tuple(sorted({key[0] for key in grouped})),
        "causal_candidates": tuple(
            candidates[ref] for ref in sorted(candidates)
        ),
        "seed_results": tuple(
            report_seed(
                "confirmed_root",
                start_ref=seed_ref,
                defect_state=entry["defect_state"],
                root_refs=entry["root_refs"],
                confirmation_identities=entry["confirmation_identities"],
            )
            for (seed_ref, _), entry in sorted(grouped.items())
        ),
    }


def confirmation_seed_fields(*confirmations, outcome="inconclusive"):
    defect_state = sample_defect_state()
    grouped = {}
    for confirmation in confirmations:
        seed_ref = confirmation.recursive_path[-1]
        grouped.setdefault(seed_ref, []).append(confirmation.confirmation_identity)
    return {
        "start_refs": tuple(sorted(grouped)),
        "seed_results": tuple(
            report_seed(
                outcome,
                start_ref=seed_ref,
                defect_state=defect_state,
                confirmation_identities=tuple(identities),
            )
            for seed_ref, identities in sorted(grouped.items())
        ),
    }


def factor_bundle(
    *,
    role="contributing_condition",
    analysis_perspective="State publication test.",
):
    node_ref = "record:context"
    target_ref = "record:decision"
    path = (node_ref, target_ref)
    defect_state = sample_defect_state()
    active_failure_signature = active_failure_signature_for(
        defect_state,
        seed_ref=path[-1],
    )
    seed_binding = seed_binding_identity_for(
        path[-1], defect_state.fingerprint
    )
    request_projection = {
        "schema": "factor-role-request-projection/v1",
        "facts": {
            "candidate_ref": node_ref,
            "defect_state": defect_state.to_dict(),
            "recursive_path": list(path),
            "candidate_reference": {"ref": node_ref},
            "recursive_path_references": [
                {"ref": ref} for ref in path
            ],
            "supporting_evidence": [{"ref": node_ref}],
            "opposing_evidence": [],
            "task_obligations": [],
            "confirmed_root_summaries": [],
            "hypothesis_id": "hyp:factor",
            "hypothesis_semantic_hash": "semantic:factor",
            "seed_binding_identity": seed_binding,
            "analysis_perspective": analysis_perspective,
        },
    }
    request_identity = factor_role_request_projection_identity(
        request_projection
    )
    mechanism_type = {
        "contributing_condition": "enabling_condition",
        "amplifying_factor": "amplification",
    }.get(role)
    mechanism = (
        {
            "schema": "factor-role-mechanism/v1",
            "mechanism_type": mechanism_type,
            "source_ref": node_ref,
            "target_ref": target_ref,
            "effect": "The candidate changes downstream defect conditions.",
        }
        if mechanism_type is not None
        else {}
    )
    judgment = FactorRoleJudgment(
        candidate_ref=node_ref,
        necessity_status="not_necessary",
        factor_role=role,
        reason="The candidate is a factor but not a necessary root.",
        confidence=0.7,
        evidence_refs=(node_ref, target_ref),
        recursive_path=path,
        factor_mechanism=mechanism,
        counterfactual={
            "schema": "factor-role-counterfactual/v1",
            "intervention_ref": node_ref,
            "intervention_kind": (
                "replace_with_semantically_correct_behavior"
            ),
            "predicted_effect": {
                "contributing_condition": "reduces_defect_likelihood",
                "amplifying_factor": "reduces_defect_severity",
                "unrelated": "no_grounded_causal_influence_established",
            }[role],
        },
        hypothesis_id="hyp:factor",
        hypothesis_semantic_hash="semantic:factor",
        defect_fingerprint=defect_state.fingerprint,
        seed_binding_identity=seed_binding,
        analysis_perspective=analysis_perspective,
        request_identity=request_identity,
    )
    owner = LocalStateOwner.create(
        seed_binding_identity=seed_binding,
        hypothesis_id=judgment.hypothesis_id,
        visit_key="visit:factor-publication",
        occurrence_key="confirmation_queue",
    ).to_dict()
    action = {
        "operation": "factor_role_completed",
        "semantic_key": "factor_role:{0}".format(request_identity),
        "owner": owner,
        "origin": "global_candidate_factor_assessment",
        "candidate_ref": judgment.candidate_ref,
        "hypothesis_id": judgment.hypothesis_id,
        "defect_fingerprint": judgment.defect_fingerprint,
        "seed_binding_identity": judgment.seed_binding_identity,
        "request_projection": request_projection,
        "request_identity": request_identity,
        "physical_requests_reserved": 1,
        "physical_request_delta": 1,
        "physical_request_exact": True,
        "judgment": judgment.to_dict(),
        "judgment_identity": judgment.judgment_identity,
        "failure_classification": "none",
        "active_role_binding": (
            None
            if role == "unrelated"
            else ActiveFailureRoleBinding.create(
                candidate_ref=judgment.candidate_ref,
                seed_ref=judgment.recursive_path[-1],
                failure_signature=active_failure_signature[
                    "signature_id"
                ],
                defect_fingerprint=active_failure_signature[
                    "fingerprint"
                ],
                failure_identity_source=active_failure_signature[
                    "identity_source"
                ],
                failure_kind=active_failure_signature["kind"],
                causal_role="amplifying_condition",
                disposition="factor",
            ).to_dict()
        ),
        "queue_binding": {
            "seed_key": judgment.seed_binding_identity,
            "requested_by_ref": judgment.candidate_ref,
            "recursive_path": list(judgment.recursive_path),
            "checked_evidence_refs": [judgment.candidate_ref],
            "task_obligations": [],
            "artifact_evidence_envelopes": [],
        },
    }
    publication = canonical_factor_role_publication(
        judgment=judgment,
        request_projection=request_projection,
        action_projection=action,
    )
    metadata = {
        "confirmation_queue": [
            {
                "hypothesis_id": judgment.hypothesis_id,
                "hypothesis_semantic_hash": (
                    judgment.hypothesis_semantic_hash
                ),
                "candidate_ref": judgment.candidate_ref,
                "defect_fingerprint": judgment.defect_fingerprint,
                "seed_binding_identity": judgment.seed_binding_identity,
                "semantic_identity": request_identity,
                "status": "completed",
                "owner": owner,
                "analysis_perspective": judgment.analysis_perspective,
                "artifact_evidence_envelopes": [],
                "factual_request_projection": request_projection,
                "review_scope": "non_root",
                "origin": "global_candidate_factor_assessment",
                "seed_key": judgment.seed_binding_identity,
                "requested_by_ref": judgment.candidate_ref,
                "recursive_path": list(judgment.recursive_path),
                "checked_evidence_refs": [judgment.candidate_ref],
                "task_obligations": [],
                "factor_role_judgment": judgment.to_dict(),
                "response_identity": judgment.judgment_identity,
                "failure_classification": "none",
            }
        ],
        "confirmation_queue_keys": [
            [
                judgment.hypothesis_id,
                judgment.candidate_ref,
                judgment.defect_fingerprint,
                judgment.seed_binding_identity,
            ]
        ],
        "factor_role_judgments": [judgment.to_dict()],
        "factor_role_journal": [{**action, "status": "completed"}],
        "factor_role_action_projections": [action],
        "factor_role_gaps": [],
        "factor_confirmation_gaps": [],
        "factor_confirmation_conflicts": [],
        "factor_confirmation_enqueue_gaps": [],
    }
    return judgment, publication, metadata


def reciprocal_root_pair(*, identical_legacy_projection):
    defect = sample_defect_state()
    first_ref = "record:decision"
    second_ref = first_ref if identical_legacy_projection else "record:context"
    path = ("record:decision", "record:change")

    def make_root(node_ref, hypothesis_id, semantic_hash):
        return modern_root(
            ConfirmedRoot(
                node_ref=node_ref,
                defect_state=defect,
                reason="The decision stopped discovery.",
                counterfactual="Continuing discovery prevents the defect.",
                confidence=0.9,
                component="agent",
                event_type="decision",
                defect_type=defect.label,
                causal_role="defect_introduction",
                episode_id="episode:decision",
            ),
            hypothesis_id=hypothesis_id,
            semantic_hash=semantic_hash,
        )

    first = make_root(first_ref, "hyp:first", "semantic:first")
    second = make_root(second_ref, "hyp:second", "semantic:second")
    first_confirmation = confirmation_for(first)
    second_confirmation = confirmation_for(second)

    def comparison(target):
        return {
            "hypothesis_id": target.hypothesis_id,
            "hypothesis_semantic_hash": target.hypothesis_semantic_hash,
            "candidate_ref": target.candidate_ref,
            "defect_fingerprint": target.defect_fingerprint,
            "confirmation_identity": target.confirmation_identity,
            "recursive_path": list(target.recursive_path),
            "requires_independent_confirmation": True,
            "status": "co_root",
            "reason": "The independently confirmed causes are jointly necessary.",
            "evidence_refs": [target.candidate_ref],
        }

    first_confirmation = replace(
        first_confirmation,
        competitor_comparisons=(comparison(second_confirmation),),
    )
    second_confirmation = replace(
        second_confirmation,
        competitor_comparisons=(comparison(first_confirmation),),
    )
    first = canonical_test_root(first, first_confirmation)
    second = canonical_test_root(second, second_confirmation)
    return first, second, first_confirmation, second_confirmation


def ranked_root_pair(first, second):
    return canonical_ranked_root_publications(
        (first, second),
    )


class CausalStateTest(unittest.TestCase):
    def test_report_ignores_application_owned_candidate_funnel_payloads(self):
        defect_state = sample_defect_state()
        seed = report_seed("no_defect", defect_state=defect_state)
        report = RecursiveAttributionReport(
            case_id="application-owned-candidate-funnel",
            objective="Preserve decision payloads.",
            start_refs=[seed.start_ref],
            seed_results=[seed],
            metadata={
                "decision": {
                    "data": {
                        "candidate_funnel": {"application": "owned"},
                    },
                },
            },
        )

        restored = RecursiveAttributionReport.from_dict(report.to_dict())

        self.assertEqual(
            restored.metadata["decision"]["data"]["candidate_funnel"],
            {"application": "owned"},
        )

    def test_report_rejects_invalid_candidate_funnel_in_global_gate(self):
        defect_state = sample_defect_state()
        seed = report_seed("no_defect", defect_state=defect_state)
        report = RecursiveAttributionReport(
            case_id="global-gate-candidate-funnel",
            objective="Validate global gate audit facts.",
            start_refs=[seed.start_ref],
            seed_results=[seed],
        )
        forged = report.to_dict()
        forged["investigation_journal"] = [
            {
                "kind": "global_candidate_gate",
                "status": "bypassed",
                "candidate_compression": {
                    "candidate_funnel": {"application": "forged"},
                },
            },
        ]

        with self.assertRaisesRegex(ValueError, "candidate_funnel"):
            RecursiveAttributionReport.from_dict(forged)

    def test_report_round_trip_preserves_candidate_funnel_selection_identity(self):
        class CandidateFunnelGraph:
            def resolve(self, ref):
                return ref

        candidate = CausalCandidate(
            ref="record:decision",
            node=TraceNode(
                ref="record:decision",
                record_id="decision",
                component="agent",
                event_type="decision",
            ),
            source="confirmed_edge",
        )
        funnel = select_global_candidates(
            CandidateFunnelGraph(),
            [candidate],
        ).to_dict()
        defect_state = sample_defect_state()
        seed = report_seed("no_defect", defect_state=defect_state)
        owner = LocalStateOwner.create(
            seed_binding_identity=seed_binding_identity_for(
                seed.start_ref,
                defect_state.fingerprint,
            ),
            hypothesis_id="global:candidate-funnel",
            visit_key="global:candidate-funnel",
            occurrence_key="candidate_compression",
        )
        report = RecursiveAttributionReport(
            case_id="candidate-funnel-round-trip",
            objective="Preserve candidate budget audit facts.",
            start_refs=[seed.start_ref],
            seed_results=[seed],
            metadata={
                "candidate_compression": [
                    {
                        "candidate_funnel": funnel,
                        "owner": owner.to_dict(),
                    },
                ],
            },
        )

        restored = RecursiveAttributionReport.from_dict(report.to_dict())

        self.assertEqual(
            restored.metadata["candidate_compression"][0]["candidate_funnel"][
                "selection_identity"
            ],
            funnel["selection_identity"],
        )

        forged = report.to_dict()
        forged["metadata"]["candidate_compression"][0]["candidate_funnel"][
            "selection_identity"
        ] = "0" * 64
        with self.assertRaisesRegex(
            ValueError,
            "candidate_funnel selection_identity",
        ):
            RecursiveAttributionReport.from_dict(forged)

    def test_legacy_projection_deduplicates_identical_visible_roots(self):
        first, second, first_confirmation, second_confirmation = reciprocal_root_pair(
            identical_legacy_projection=True
        )
        primary_roots, co_roots = ranked_root_pair(first, second)
        report = RecursiveAttributionReport(
            case_id="legacy-dedup",
            objective="Find roots.",
            **root_seed_fields(first, second),
            confirmations=[first_confirmation, second_confirmation],
            confirmed_roots=primary_roots,
            co_roots=co_roots,
        )

        payload = report.to_dict()
        self.assertEqual(len(payload["confirmed_roots"]), 1)
        self.assertEqual(len(payload["co_roots"]), 1)
        self.assertEqual(
            payload["root_causes"],
            [primary_roots[0].to_legacy_root_cause()],
        )

    def test_legacy_projection_preserves_distinct_objects_primary_first(self):
        first, second, first_confirmation, second_confirmation = reciprocal_root_pair(
            identical_legacy_projection=False
        )
        primary_roots, co_roots = ranked_root_pair(first, second)
        report = RecursiveAttributionReport(
            case_id="legacy-order",
            objective="Find roots.",
            **root_seed_fields(first, second),
            confirmations=[first_confirmation, second_confirmation],
            confirmed_roots=primary_roots,
            co_roots=co_roots,
        )

        self.assertEqual(
            report.to_dict()["root_causes"],
            [
                primary_roots[0].to_legacy_root_cause(),
                co_roots[0].to_legacy_root_cause(),
            ],
        )

    def test_factor_identity_cannot_also_be_a_rejected_candidate(self):
        judgment, factor, metadata = factor_bundle()

        with self.assertRaisesRegex(
            ValueError,
            "rejected candidates|role identity",
        ):
            RecursiveAttributionReport(
                case_id="factor-overlap",
                objective="Find roots.",
                start_refs=["record:decision"],
                seed_results=[
                    report_seed(
                        "inconclusive",
                        start_ref="record:decision",
                    )
                ],
                analysis_perspective=judgment.analysis_perspective,
                contributing_conditions=[factor],
                rejected_candidates=[
                    RejectedCandidate(
                        node_ref=factor.node_ref,
                        reason=factor.reason,
                        evidence_refs=factor.evidence_refs,
                        hypothesis_id=judgment.hypothesis_id,
                        recursive_path=factor.recursive_path,
                        confirmation_status="not_necessary",
                        confidence=factor.confidence,
                        confirmation=judgment.to_dict(),
                        provenance=factor.provenance,
                    )
                ],
                metadata=metadata,
            )

    def test_factor_without_confirmed_root_remains_inconclusive(self):
        for role in ("contributing_condition", "amplifying_factor"):
            with self.subTest(role=role):
                judgment, factor, metadata = factor_bundle(role=role)
                report = RecursiveAttributionReport(
                    case_id="factor-only",
                    objective="Find roots.",
                    start_refs=["record:decision"],
                    seed_results=[
                        report_seed(
                            "inconclusive",
                            start_ref="record:decision",
                        )
                    ],
                    analysis_perspective=judgment.analysis_perspective,
                    contributing_conditions=(
                        [factor] if role == "contributing_condition" else []
                    ),
                    amplifying_factors=(
                        [factor] if role == "amplifying_factor" else []
                    ),
                    metadata=metadata,
                )

                self.assertEqual(report.rejected_candidates, ())
                self.assertEqual(report.analysis_outcome, "inconclusive")

    def test_non_root_publications_must_equal_their_owning_confirmation(self):
        for role in ("contributing_condition", "amplifying_factor"):
            judgment, factor, metadata = factor_bundle(role=role)
            mutations = {
                "reason": replace(factor, reason="A different factor reason."),
                "confidence": replace(factor, confidence=0.2),
                "evidence citations": replace(
                    factor, evidence_refs=(factor.node_ref,)
                ),
                "mechanism": replace(
                    factor,
                    mechanism={
                        **dict(factor.mechanism),
                        "target_ref": "record:unrelated",
                    },
                ),
            }
            for label, mutated in mutations.items():
                with self.subTest(role=role, mutation=label):
                    with self.assertRaisesRegex(
                        ValueError, "completed FactorRole actions"
                    ):
                        RecursiveAttributionReport(
                            case_id="factor-confirmation-drift",
                            objective="Find roots.",
                            start_refs=["record:decision"],
                            seed_results=[
                                report_seed(
                                    "inconclusive",
                                    start_ref="record:decision",
                                )
                            ],
                            analysis_perspective=(
                                judgment.analysis_perspective
                            ),
                            contributing_conditions=(
                                [mutated]
                                if role == "contributing_condition"
                                else []
                            ),
                            amplifying_factors=(
                                [mutated] if role == "amplifying_factor" else []
                            ),
                            metadata=metadata,
                        )

        judgment, rejected, metadata = factor_bundle(role="unrelated")
        for label, mutated in {
            "reason": replace(rejected, reason="A different rejection reason."),
            "confidence": replace(rejected, confidence=0.1),
            "evidence citations": replace(
                rejected, evidence_refs=(rejected.node_ref,)
            ),
        }.items():
            with self.subTest(role="rejected_candidate", mutation=label):
                with self.assertRaisesRegex(
                    ValueError, "completed FactorRole actions"
                ):
                    RecursiveAttributionReport(
                        case_id="rejected-confirmation-drift",
                        objective="Find roots.",
                        start_refs=["record:decision"],
                        seed_results=[
                            report_seed(
                                "inconclusive",
                                start_ref="record:decision",
                            )
                        ],
                        analysis_perspective=(
                            judgment.analysis_perspective
                        ),
                        rejected_candidates=[mutated],
                        metadata=metadata,
                    )

    def _modern_report_bundle(self):
        root = modern_root(
            ConfirmedRoot(
                node_ref="record:decision",
                defect_state=sample_defect_state(),
                reason="The decision stopped discovery.",
                counterfactual="Searching call sites would reveal the contract.",
                confidence=0.9,
            )
        )
        confirmation = confirmation_for(root)
        return root, confirmation, RecursiveAttributionReport(
            case_id="modern",
            objective="Find roots.",
            **root_seed_fields(root),
            confirmations=[confirmation],
            confirmed_roots=[root],
        )

    def test_modern_confirmation_requires_persisted_identity(self):
        _, _, report = self._modern_report_bundle()
        payload = report.to_dict()
        del payload["confirmations"][0]["confirmation_identity"]
        del payload["confirmed_roots"][0]["confirmation"]["confirmation_identity"]

        with self.assertRaisesRegex(ValueError, "confirmation_identity.*required"):
            RecursiveAttributionReport.from_dict(payload)

    def test_modern_root_cannot_fall_back_to_candidate_only_binding(self):
        _, _, report = self._modern_report_bundle()
        payload = report.to_dict()
        root = payload["confirmed_roots"][0]
        root.pop("hypothesis_id")
        root.pop("recursive_path")
        root.pop("confirmation")

        with self.assertRaisesRegex(ValueError, "modern root.*full confirmation identity"):
            RecursiveAttributionReport.from_dict(payload)

    def test_primary_and_co_root_cannot_publish_same_confirmation_identity(self):
        root, _, report = self._modern_report_bundle()

        with self.assertRaisesRegex(ValueError, "primary.*co-root|duplicate root role"):
            RecursiveAttributionReport(
                case_id=report.case_id,
                objective=report.objective,
                confirmations=report.confirmations,
                confirmed_roots=[root],
                co_roots=[root],
            )

    def test_modern_report_rejects_duplicate_top_level_confirmation_identity(self):
        root, confirmation, _ = self._modern_report_bundle()
        with self.assertRaisesRegex(ValueError, "duplicate confirmation identity"):
            RecursiveAttributionReport(
                case_id="duplicate-confirmation",
                objective="Find roots.",
                confirmations=[confirmation, confirmation],
                confirmed_roots=[root],
            )

    def test_two_published_roots_require_reciprocal_co_root_confirmations(self):
        first = modern_root(
            ConfirmedRoot(
                "record:first",
                sample_defect_state(),
                "First cause.",
                "Correcting it prevents the defect.",
                0.9,
            ),
            hypothesis_id="hyp:first",
            semantic_hash="semantic:first",
        )
        second = modern_root(
            ConfirmedRoot(
                "record:second",
                sample_defect_state(),
                "Second cause.",
                "Correcting it prevents the defect.",
                0.8,
            ),
            hypothesis_id="hyp:second",
            semantic_hash="semantic:second",
        )

        with self.assertRaisesRegex(ValueError, "confirmation graph"):
            RecursiveAttributionReport(
                case_id="missing-reciprocal",
                objective="Find roots.",
                **root_seed_fields(first, second),
                confirmations=[confirmation_for(first), confirmation_for(second)],
                confirmed_roots=[first],
                co_roots=[second],
            )

    def test_orphan_confirmed_confirmation_requires_explicit_unresolved_state(self):
        _, confirmation, _ = self._modern_report_bundle()
        with self.assertRaisesRegex(ValueError, "orphan confirmed confirmation"):
            RecursiveAttributionReport(
                case_id="orphan",
                objective="Find roots.",
                confirmations=[confirmation],
            )

        with self.assertRaisesRegex(ValueError, "confirmation ownership"):
            RecursiveAttributionReport(
                case_id="orphan-unresolved",
                objective="Find roots.",
                confirmations=[confirmation],
                unresolved_refs=[confirmation.candidate_ref],
                metadata={"confirmation_graph_inconsistent": True},
            )

    def test_full_identity_cannot_occupy_root_and_rejected_roles(self):
        root, confirmation, _ = self._modern_report_bundle()
        rejected = RejectedCandidate(
            node_ref=root.node_ref,
            reason="Contradictory role.",
            hypothesis_id=root.hypothesis_id,
            recursive_path=root.recursive_path,
            confirmation_status="confirmed",
            confirmation=confirmation.to_dict(),
        )

        with self.assertRaisesRegex(ValueError, "role conflict|rejected candidate"):
            RecursiveAttributionReport(
                case_id="role-conflict",
                objective="Find roots.",
                **root_seed_fields(root),
                confirmations=[confirmation],
                confirmed_roots=[root],
                rejected_candidates=[rejected],
            )

    def test_explicit_legacy_schema_migrates_roots_as_unconfirmed_facts(self):
        report = RecursiveAttributionReport.from_dict({
            "schema_version": "recursive-attribution-report/v1-legacy",
            "case_id": "legacy",
            "objective": "Find root.",
            "root_causes": [{
                "node_ref": "record:decision",
                "reason": "Legacy root.",
                "confidence": 0.9,
            }],
        })

        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.unresolved_refs, ("record:decision",))
        self.assertEqual(
            report.metadata["legacy_migration_status"],
            "independent_confirmation_required",
        )

    def test_report_keeps_distinct_confirmation_identities_on_same_node(self):
        defect = sample_defect_state()
        path = ("record:decision", "record:change")
        confirmed = replace(
            RootConfirmation.confirmed(
                "record:decision",
                excerpt="The decision ended discovery.",
                reason="The first hypothesis is causal.",
                counterfactual=confirmation_counterfactual_for(
                    "record:decision",
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=["record:decision"],
            ),
            hypothesis_id="hyp:first",
            hypothesis_semantic_hash="semantic:first",
            defect_fingerprint=defect.fingerprint,
            recursive_path=path,
            seed_binding_identity=seed_binding_identity_for(
                path[-1], defect.fingerprint
            ),
        )
        rejected = replace(
            RootConfirmation.rejected("record:decision", "The second claim is weaker."),
            hypothesis_id="hyp:second",
            hypothesis_semantic_hash="semantic:second",
            defect_fingerprint=defect.fingerprint,
            recursive_path=path,
            seed_binding_identity=seed_binding_identity_for(
                path[-1], defect.fingerprint
            ),
        )
        root = canonical_test_root(ConfirmedRoot(
            node_ref="record:decision",
            defect_state=defect,
            reason=confirmed.reason,
            counterfactual=confirmed.counterfactual,
            confidence=confirmed.confidence,
            evidence_refs=confirmed.evidence_refs,
            excerpt=confirmed.excerpt,
            hypothesis_id="hyp:first",
            recursive_path=path,
            observed_defect_refs=(path[-1],),
            confirmation=confirmed.to_dict(),
        ), confirmed)

        report = RecursiveAttributionReport(
            case_id="multi-identity",
            objective="Find roots.",
            start_refs=(path[-1],),
            seed_results=[
                report_seed(
                    "confirmed_root",
                    start_ref=path[-1],
                    defect_state=defect,
                    root_refs=(root.node_ref,),
                    confirmation_identities=(
                        confirmed.confirmation_identity,
                        rejected.confirmation_identity,
                    ),
                )
            ],
            confirmations=[confirmed, rejected],
            confirmed_roots=[root],
            causal_candidates=[root_candidate(root)],
        )

        self.assertEqual(report.analysis_outcome, "confirmed_root")
        self.assertEqual(report.metadata["confirmation_node_summary"]["record:decision"]["status"], "mixed")

    def test_schema_less_legacy_root_causes_require_explicit_schema(self):
        with self.assertRaisesRegex(ValueError, "schema_version"):
            RecursiveAttributionReport.from_dict({
                "case_id": "legacy",
                "objective": "Find root.",
                "root_causes": [{
                    "node_ref": "record:decision",
                    "component": "agent",
                    "event_type": "decision",
                    "reason": "Legacy root.",
                    "confidence": 0.9,
                }],
            })

    def test_modern_confirmation_roundtrip_rejects_forged_full_identity(self):
        confirmation = replace(
            RootConfirmation.confirmed(
                "record:decision",
                excerpt="The decision is causal.",
                reason="Evidence is complete.",
                counterfactual=confirmation_counterfactual_for(
                    "record:decision",
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=["record:decision"],
            ),
            hypothesis_id="hyp:one",
            hypothesis_semantic_hash="semantic:one",
            defect_fingerprint=sample_defect_state().fingerprint,
            recursive_path=("record:decision", "record:change"),
        )
        payload = confirmation.to_dict()
        self.assertEqual(RootConfirmation.from_dict(payload), confirmation)
        payload["confirmation_identity"] = "confirmation:forged"
        with self.assertRaisesRegex(ValueError, "confirmation_identity"):
            RootConfirmation.from_dict(payload)

        root = ConfirmedRoot(
            node_ref=confirmation.candidate_ref,
            defect_state=sample_defect_state(),
            reason=confirmation.reason,
            counterfactual=confirmation.counterfactual,
            confidence=confirmation.confidence,
            evidence_refs=confirmation.evidence_refs,
            excerpt=confirmation.excerpt,
            hypothesis_id="hyp:foreign",
            recursive_path=confirmation.recursive_path,
            confirmation=confirmation.to_dict(),
        )
        with self.assertRaisesRegex(
            ValueError, "root confirmation (identity|response projection)"
        ):
            RecursiveAttributionReport(
                case_id="forged-root",
                objective="Find roots.",
                confirmations=[confirmation],
                confirmed_roots=[root],
            )
    def test_legacy_direct_confirmation_infers_structured_counterfactual_status(self):
        confirmation = RootConfirmation(
            candidate_ref="record:decision",
            status="confirmed",
            excerpt="Stop discovery.",
            reason="The decision omitted required discovery.",
            counterfactual="Searching would have prevented the omission.",
            confidence=0.8,
        )

        self.assertEqual(confirmation.counterfactual_status, "supports_causality")

    def test_numeric_fields_reject_nonfinite_and_out_of_range_values(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
        )
        valid_hypothesis = AttributionHypothesis.create("Decision is root", node.ref, defect_state)
        constructors = (
            ("candidate score", lambda value: CausalCandidate(node.ref, node, "edge", score=value)),
            ("assessment confidence", lambda value: PredecessorAssessment(node.ref, confidence=value)),
            (
                "judgment confidence",
                lambda value: CausalStepJudgment(node.ref, "present", "reason", confidence=value),
            ),
            (
                "frontier priority",
                lambda value: FrontierItem.create(
                    node_ref=node.ref,
                    defect_state=defect_state,
                    downstream_path=[node.ref],
                    hypothesis_id=valid_hypothesis.hypothesis_id,
                    hypothesis_semantic_hash=valid_hypothesis.semantic_hash,
                    priority=value,
                ),
            ),
            ("evidence confidence", lambda value: HypothesisEvidence(node.ref, "reason", value)),
            ("hypothesis confidence", lambda value: valid_hypothesis.with_updates(confidence=value)),
            (
                "confirmation confidence",
                lambda value: RootConfirmation.confirmed(
                    node.ref,
                    excerpt="excerpt",
                    reason="reason",
                    counterfactual=confirmation_counterfactual_for(
                        node.ref,
                        "confirmed",
                    ),
                    confidence=value,
                ),
            ),
            (
                "root confidence",
                lambda value: ConfirmedRoot(node.ref, defect_state, "reason", "counterfactual", value),
            ),
            ("factor confidence", lambda value: CausalFactor(node.ref, "unknown", "reason", value)),
        )

        for name, constructor in constructors:
            invalid_values = (math.nan, math.inf, -math.inf)
            if name != "frontier priority":
                invalid_values += (-0.01, 1.01)
            for value in invalid_values:
                with self.subTest(field=name, value=value):
                    with self.assertRaisesRegex(ValueError, "finite|between"):
                        constructor(value)

    def test_numeric_deserialization_rejects_invalid_values(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
        )
        hypothesis = AttributionHypothesis.create("Decision is root", node.ref, defect_state)
        values = (
            (CausalCandidate(node.ref, node, "edge").to_dict(), CausalCandidate.from_dict, "score"),
            (PredecessorAssessment(node.ref).to_dict(), PredecessorAssessment.from_dict, "confidence"),
            (
                CausalStepJudgment(node.ref, "present", "reason").to_dict(),
                CausalStepJudgment.from_dict,
                "confidence",
            ),
            (sample_frontier_item().to_dict(), FrontierItem.from_dict, "priority"),
            (HypothesisEvidence(node.ref, "reason").to_dict(), HypothesisEvidence.from_dict, "confidence"),
            (hypothesis.to_dict(), AttributionHypothesis.from_dict, "confidence"),
            (
                RootConfirmation.confirmed(
                    node.ref,
                    excerpt="excerpt",
                    reason="reason",
                    counterfactual=confirmation_counterfactual_for(
                        node.ref,
                        "confirmed",
                    ),
                    confidence=0.5,
                ).to_dict(),
                RootConfirmation.from_dict,
                "confidence",
            ),
            (
                ConfirmedRoot(node.ref, defect_state, "reason", "counterfactual", 0.5).to_dict(),
                ConfirmedRoot.from_dict,
                "confidence",
            ),
            (CausalFactor(node.ref, "unknown", "reason").to_dict(), CausalFactor.from_dict, "confidence"),
        )

        for payload, loader, field in values:
            payload[field] = math.nan
            with self.subTest(loader=loader.__qualname__, field=field):
                with self.assertRaisesRegex(ValueError, "finite"):
                    loader(payload)
    def test_transformed_defect_preserves_chain_and_changes_visit_identity(self):
        downstream = sample_defect_state()
        upstream = downstream.transformed(
            label="premature_repository_search_closure",
            mechanism="the plan stopped before searching call sites",
            transformation_reason="the incomplete search produced the incomplete plan",
        )

        self.assertEqual(upstream.derived_from_defect_state_id, downstream.defect_state_id)
        self.assertEqual(upstream.expected, downstream.expected)
        self.assertEqual(upstream.actual, downstream.actual)
        self.assertEqual(upstream.scope, downstream.scope)
        self.assertNotEqual(
            semantic_visit_key("record:dec_85", downstream, "hyp_a"),
            semantic_visit_key("record:dec_85", upstream, "hyp_a"),
        )

    def test_frontier_round_trip_preserves_semantic_identity(self):
        item = sample_frontier_item()
        self.assertEqual(FrontierItem.from_dict(item.to_dict()), item)
        self.assertEqual(
            item.visit_key,
            semantic_visit_key(item.node_ref, item.defect_state, item.hypothesis_semantic_hash),
        )

    def test_recursive_state_types_round_trip_and_report_projects_legacy_fields(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
            title="Stop discovery",
            data={"rationale": "The search is complete."},
        )
        candidate = CausalCandidate(
            ref=node.ref,
            node=node,
            source="confirmed_edge",
            edge={"relation": "reasoning_selected_action", "confidence": 1.0},
            score=0.9,
            evidence_refs=["record:change"],
        )
        evidence = HypothesisEvidence(
            "record:decision", "The plan declared completion.", 0.8
        )
        hypothesis = AttributionHypothesis.create(
            "Agent prematurely narrowed implementation.", node.ref, defect_state
        )
        hypothesis = hypothesis.with_updates(
            supporting_evidence=[evidence],
            unresolved_questions=["Were call sites searched?"],
        )
        visit_key = semantic_visit_key(
            "record:change", defect_state, hypothesis.semantic_hash
        )
        seed_binding = seed_binding_identity_for(
            "record:observed", defect_state.fingerprint
        )
        assessment = PredecessorAssessment(
            ref=node.ref,
            relation="defect_transformation",
            reason="The incomplete plan produced the incomplete change.",
            confidence=0.9,
            recurse=True,
            upstream_defect=defect_state.transformed(
                label="incomplete_plan",
                mechanism="call sites were not searched",
                transformation_reason="the plan controlled authored methods",
            ),
            evidence_refs=["record:change"],
            owner=LocalStateOwner.create(
                seed_binding_identity=seed_binding,
                hypothesis_id=hypothesis.hypothesis_id,
                visit_key=visit_key,
                occurrence_key="round_trip_predecessor",
            ),
        )
        judgment = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="present",
            current_defect_reason="The change omits the method.",
            predecessors=[assessment],
            confidence=0.9,
            owner=LocalStateOwner.create(
                seed_binding_identity=seed_binding,
                hypothesis_id=hypothesis.hypothesis_id,
                visit_key=visit_key,
                occurrence_key="round_trip_step",
            ),
        )
        root_path = (node.ref, "record:change", "record:observed")
        confirmation = replace(
            RootConfirmation.confirmed(
                node.ref,
                excerpt="Now I have a comprehensive understanding",
                reason="The decision stopped discovery before checking call sites.",
                counterfactual=confirmation_counterfactual_for(
                    node.ref,
                    "confirmed",
                ),
                confidence=0.9,
                evidence_refs=[node.ref],
                counterfactual_status="supports_causality",
            ),
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_semantic_hash=hypothesis.semantic_hash,
            defect_fingerprint=defect_state.fingerprint,
            recursive_path=root_path,
            seed_binding_identity=seed_binding_identity_for(
                root_path[-1], defect_state.fingerprint
            ),
            analysis_perspective="Improve Agent repository reasoning.",
        )
        root = canonical_confirmed_root_publication(
            confirmation=confirmation,
            defect_state=defect_state,
            candidate_node=node,
            seed_start_ref="record:observed",
        )
        factor_judgment, factor, factor_metadata = factor_bundle(
            analysis_perspective="Improve Agent repository reasoning.",
        )
        report = RecursiveAttributionReport(
            case_id="case-1",
            objective="Find the defect origin.",
            start_refs=["record:observed", "record:prompt", node.ref],
            seed_results=[
                report_seed(
                    "confirmed_root",
                    defect_state=defect_state,
                    root_refs=(root.node_ref,),
                    confirmation_identities=(confirmation.confirmation_identity,),
                ),
                report_seed(
                    "evidence_gap",
                    start_ref="record:prompt",
                    defect_state=defect_state,
                ),
                report_seed(
                    "no_defect",
                    start_ref=node.ref,
                    defect_state=defect_state,
                ),
            ],
            analysis_outcome="inconclusive",
            analysis_perspective="Improve Agent repository reasoning.",
            defect_states=[defect_state],
            causal_candidates=[candidate],
            causal_relations=[assessment],
            step_judgments=[judgment],
            hypotheses=[hypothesis],
            introduction_candidates=[candidate],
            confirmations=[confirmation],
            confirmed_roots=[root],
            contributing_conditions=[factor],
            taint_paths=[["record:observed", "record:change", node.ref]],
            visited_order=["record:observed", "record:change", node.ref],
            visited_entries=[
                {
                    "node_ref": ref,
                    "owner": LocalStateOwner.create(
                        seed_binding_identity=seed_binding_identity_for(
                            ref if ref in {"record:observed", node.ref} else "record:observed",
                            defect_state.fingerprint,
                        ),
                        hypothesis_id=hypothesis.hypothesis_id,
                        visit_key=semantic_visit_key(
                            ref, defect_state, hypothesis.semantic_hash
                        ),
                        occurrence_key="round_trip_visit:{0}".format(ref),
                    ).to_dict(),
                }
                for ref in ("record:observed", "record:change", node.ref)
            ],
            unresolved_refs=["record:prompt"],
            metadata={
                "offline_only": True,
                **factor_metadata,
            },
        )

        for value, type_ in (
            (defect_state, DefectState),
            (candidate, CausalCandidate),
            (assessment, PredecessorAssessment),
            (judgment, CausalStepJudgment),
            (sample_frontier_item(), FrontierItem),
            (evidence, HypothesisEvidence),
            (hypothesis, AttributionHypothesis),
            (confirmation, RootConfirmation),
            (root, ConfirmedRoot),
            (factor, CausalFactor),
            (report, RecursiveAttributionReport),
        ):
            self.assertEqual(type_.from_dict(value.to_dict()), value)

        payload = report.to_dict()
        self.assertEqual(payload["case_id"], "case-1")
        self.assertEqual(payload["objective"], "Find the defect origin.")
        self.assertEqual(report.analysis_outcome, "partial")
        self.assertEqual(payload["root_causes"], [root.to_legacy_root_cause()])
        self.assertEqual(payload["taint_paths"], [list(path) for path in report.taint_paths])
        self.assertEqual(payload["visited_order"], list(report.visited_order))
        self.assertEqual(payload["unresolved_refs"], list(report.unresolved_refs))
        self.assertEqual(payload["metadata"], report.metadata)
        self.assertEqual(
            factor.confirmation["judgment_identity"],
            factor_judgment.judgment_identity,
        )

    def test_relation_values_are_exact_and_reject_unknown_values(self):
        self.assertEqual(
            PredecessorAssessment(
                ref="record:prompt", relation="same_defect_propagation"
            ).relation,
            "same_defect_propagation",
        )
        with self.assertRaisesRegex(ValueError, "relation"):
            PredecessorAssessment(ref="record:prompt", relation="caused_by")

    def test_report_outcome_preserves_partial_roots_and_keeps_pure_inconclusive_root_free(self):
        root = modern_root(ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
            observed_defect_refs=["record:observed"],
        ))
        partial = RecursiveAttributionReport(
            case_id="partial",
            objective="Find the root.",
            start_refs=["record:observed", "record:prompt"],
            analysis_outcome="inconclusive",
            seed_results=[
                report_seed(
                    "confirmed_root",
                    defect_state=root.defect_state,
                    root_refs=(root.node_ref,),
                    confirmation_identities=(confirmation_for(root).confirmation_identity,),
                ),
                report_seed("evidence_gap", start_ref="record:prompt"),
            ],
            confirmed_roots=[root],
            causal_candidates=[root_candidate(root)],
            confirmations=[confirmation_for(root)],
            unresolved_refs=["record:prompt"],
        )
        self.assertEqual(partial.analysis_outcome, "partial")
        self.assertEqual(partial.confirmed_roots, (root,))
        self.assertEqual(
            RecursiveAttributionReport.from_dict(partial.to_dict()).analysis_outcome,
            "partial",
        )

        for metadata in (
            {"unresolved_reason": "provider_unavailable"},
            {"unresolved_reason": "judge_budget_exhausted"},
            {"unresolved_reason": "artifact_missing"},
        ):
            report = RecursiveAttributionReport(
                case_id="unresolved",
                objective="Find the root.",
                analysis_outcome="inconclusive",
                unresolved_refs=["record:change"],
                metadata=metadata,
            )
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertEqual(report.confirmed_roots, ())
            self.assertEqual(report.co_roots, ())

    def test_report_outcome_downgrades_root_labels_without_confirmed_roots(self):
        root_found = RecursiveAttributionReport(
            case_id="no-root",
            objective="Find the root.",
            analysis_outcome="root_found",
            start_refs=["record:observed"],
            seed_results=[report_seed("no_defect")],
        )
        self.assertEqual(root_found.analysis_outcome, "no_defect")

        partial = RecursiveAttributionReport(
            case_id="no-partial-root",
            objective="Find the root.",
            analysis_outcome="partial_root_found",
            start_refs=["record:observed"],
            seed_results=[report_seed("inconclusive")],
            unresolved_refs=["record:change"],
        )
        self.assertEqual(partial.analysis_outcome, "inconclusive")

    def test_report_allows_node_level_mixed_outcomes_without_cross_closing_identity(self):
        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        )
        unknown = replace(
            RootConfirmation.unknown(
                root.node_ref, "A separate hypothesis is unresolved."
            ),
            hypothesis_id="hyp:other",
            hypothesis_semantic_hash="semantic:other",
            defect_fingerprint=root.defect_state.fingerprint,
            recursive_path=(root.node_ref, "record:other"),
            seed_binding_identity=seed_binding_identity_for(
                "record:other", root.defect_state.fingerprint
            ),
        )
        report = RecursiveAttributionReport(
            case_id="mixed-identities",
            objective="Find the root.",
            start_refs=["record:observed", "record:other"],
            seed_results=[
                report_seed(
                    "confirmed_root",
                    defect_state=root.defect_state,
                    root_refs=(root.node_ref,),
                    confirmation_identities=(
                        confirmation_for(root).confirmation_identity,
                    ),
                ),
                report_seed(
                    "evidence_gap",
                    start_ref="record:other",
                    confirmation_identities=(unknown.confirmation_identity,),
                ),
            ],
            confirmed_roots=[root],
            causal_candidates=[root_candidate(root)],
            confirmations=[
                confirmation_for(root),
                unknown,
            ],
            unresolved_refs=[root.node_ref],
        )
        self.assertEqual(report.analysis_outcome, "partial")

    def test_report_requires_confirmed_root_confirmation(self):
        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        )
        with self.assertRaisesRegex(ValueError, "modern root.*full confirmation identity"):
            RecursiveAttributionReport(
                case_id="missing-confirmation",
                objective="Find the root.",
                confirmed_roots=[root],
            )
        with self.assertRaisesRegex(ValueError, "modern root|root confirmation identity"):
            RecursiveAttributionReport(
                case_id="unknown-root-confirmation",
                objective="Find the root.",
                confirmed_roots=[root],
                confirmations=[RootConfirmation.unknown(root.node_ref, "artifact missing")],
            )
        with self.assertRaisesRegex(ValueError, "modern root|root confirmation identity"):
            RecursiveAttributionReport(
                case_id="rejected-root-confirmation",
                objective="Find the root.",
                confirmed_roots=[root],
                confirmations=[RootConfirmation.rejected(root.node_ref, "better predecessor exists")],
            )

    def test_unknown_non_root_confirmation_is_blocking_evidence(self):
        defect = sample_defect_state()
        unknown = replace(
            RootConfirmation.unknown("record:prompt", "artifact missing"),
            hypothesis_id="hyp:unknown",
            hypothesis_semantic_hash="semantic:unknown",
            defect_fingerprint=defect.fingerprint,
            recursive_path=("record:prompt",),
            seed_binding_identity=seed_binding_identity_for(
                "record:prompt", defect.fingerprint
            ),
        )
        self.assertEqual(
            RecursiveAttributionReport(
                case_id="unknown-without-root",
                objective="Find the root.",
                start_refs=["record:prompt"],
                seed_results=[
                    report_seed(
                        "evidence_gap",
                        start_ref="record:prompt",
                        defect_state=defect,
                        confirmation_identities=(
                            unknown.confirmation_identity,
                        ),
                    )
                ],
                confirmations=[unknown],
            ).analysis_outcome,
            "inconclusive",
        )

        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        )
        self.assertEqual(
            RecursiveAttributionReport(
                case_id="unknown-with-root",
                objective="Find the root.",
                start_refs=["record:observed", "record:prompt"],
                confirmed_roots=[root],
                causal_candidates=[root_candidate(root)],
                seed_results=[
                    report_seed(
                        "confirmed_root",
                        defect_state=root.defect_state,
                        root_refs=(root.node_ref,),
                        confirmation_identities=(
                            confirmation_for(root).confirmation_identity,
                        ),
                    ),
                    report_seed(
                        "evidence_gap",
                        start_ref="record:prompt",
                        defect_state=defect,
                        confirmation_identities=(
                            unknown.confirmation_identity,
                        ),
                    ),
                ],
                confirmations=[confirmation_for(root), unknown],
            ).analysis_outcome,
            "partial",
        )

    def test_nested_judgment_uncertainty_blocks_report_outcome(self):
        unknown_status = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="unknown",
            current_defect_reason="artifact truncated",
        )
        missing_evidence = CausalStepJudgment(
            current_node_ref="record:decision",
            current_defect_status="present",
            current_defect_reason="plan incomplete",
            missing_evidence=["artifact:decision"],
        )
        unknown_predecessor = CausalStepJudgment(
            current_node_ref="record:response",
            current_defect_status="present",
            current_defect_reason="response repeats the plan",
            predecessors=[
                PredecessorAssessment(
                    ref="record:decision",
                    relation="unknown",
                    reason="the decision artifact is unavailable",
                )
            ],
        )
        direct_unknown_relation = PredecessorAssessment(
            ref="record:prompt",
            relation="unknown",
            reason="prompt evidence is missing",
        )
        for report in (
            RecursiveAttributionReport(
                case_id="unknown-status", objective="Find root.", step_judgments=[unknown_status]
            ),
            RecursiveAttributionReport(
                case_id="missing-step-evidence", objective="Find root.", step_judgments=[missing_evidence]
            ),
            RecursiveAttributionReport(
                case_id="unknown-predecessor", objective="Find root.", step_judgments=[unknown_predecessor]
            ),
            RecursiveAttributionReport(
                case_id="unknown-relation", objective="Find root.", causal_relations=[direct_unknown_relation]
            ),
        ):
            self.assertEqual(report.analysis_outcome, "inconclusive")

        root = modern_root(ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        ))
        partial = RecursiveAttributionReport(
            case_id="root-plus-unknown-step",
            objective="Find root.",
            start_refs=["record:observed", "record:change"],
            seed_results=[
                report_seed(
                    "confirmed_root",
                    defect_state=root.defect_state,
                    root_refs=(root.node_ref,),
                    confirmation_identities=(
                        confirmation_for(root).confirmation_identity,
                    ),
                ),
                report_seed("evidence_gap", start_ref="record:change"),
            ],
            confirmed_roots=[root],
            causal_candidates=[root_candidate(root)],
            confirmations=[confirmation_for(root)],
            step_judgments=[unknown_status],
        )
        self.assertEqual(partial.analysis_outcome, "partial")

    def test_latest_nested_judgment_resolves_earlier_unknown_state(self):
        earlier = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="unknown",
            current_defect_reason="artifact truncated",
            missing_evidence=["artifact:change"],
            predecessors=[
                PredecessorAssessment(
                    ref="record:decision",
                    relation="unknown",
                    reason="decision artifact unavailable",
                    missing_evidence=["artifact:decision"],
                )
            ],
        )
        later = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="present",
            current_defect_reason="the hydrated change omits the method",
            predecessors=[
                PredecessorAssessment(
                    ref="record:decision",
                    relation="defect_transformation",
                    reason="the incomplete plan produced the incomplete change",
                )
            ],
        )
        report = RecursiveAttributionReport(
            case_id="resolved-rejudgment",
            objective="Find root.",
            start_refs=["record:observed"],
            seed_results=[report_seed("no_defect")],
            step_judgments=[earlier, later],
        )
        self.assertEqual(report.analysis_outcome, "no_defect")

    def test_report_outcome_is_derived_from_roots_and_blocking_facts(self):
        root = modern_root(ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The decision stopped discovery.",
            counterfactual="Searching call sites would reveal the contract.",
            confidence=0.9,
        ))
        self.assertEqual(
            RecursiveAttributionReport(
                case_id="root-overrides-caller",
                objective="Find the root.",
                analysis_outcome="no_defect",
                start_refs=["record:observed"],
                seed_results=[
                    report_seed(
                        "confirmed_root",
                        defect_state=root.defect_state,
                        root_refs=(root.node_ref,),
                        confirmation_identities=(
                            confirmation_for(root).confirmation_identity,
                        ),
                    )
                ],
                confirmed_roots=[root],
                causal_candidates=[root_candidate(root)],
                confirmations=[confirmation_for(root)],
            ).analysis_outcome,
            "confirmed_root",
        )
        self.assertEqual(
            RecursiveAttributionReport(
                case_id="unresolved-overrides-caller",
                objective="Find the root.",
                analysis_outcome="no_defect",
                start_refs=["record:observed"],
                seed_results=[report_seed("inconclusive")],
                unresolved_refs=["record:change"],
            ).analysis_outcome,
            "inconclusive",
        )

        unresolved_hypothesis = AttributionHypothesis.create(
            "The decision may be the root.", "record:decision", sample_defect_state()
        ).with_updates(status="unresolved")
        self.assertEqual(
            RecursiveAttributionReport(
                case_id="unresolved-hypothesis",
                objective="Find the root.",
                analysis_outcome="no_defect",
                unresolved_hypotheses=[unresolved_hypothesis],
            ).analysis_outcome,
            "inconclusive",
        )

        for metadata in (
            {"provider_circuit_open": True},
            {"investigation_budget_exhausted": True},
            {"missing_evidence": ["artifact:change"]},
        ):
            self.assertEqual(
                RecursiveAttributionReport(
                    case_id="metadata-blocked",
                    objective="Find the root.",
                    analysis_outcome="no_defect",
                    metadata=metadata,
                ).analysis_outcome,
                "inconclusive",
            )

        self.assertEqual(
            RecursiveAttributionReport(
                case_id="partial-metadata",
                objective="Find the root.",
                analysis_outcome="no_defect",
                start_refs=["record:observed", "record:provider"],
                seed_results=[
                    report_seed(
                        "confirmed_root",
                        defect_state=root.defect_state,
                        root_refs=(root.node_ref,),
                        confirmation_identities=(
                            confirmation_for(root).confirmation_identity,
                        ),
                    ),
                    report_seed("evidence_gap", start_ref="record:provider"),
                ],
                confirmed_roots=[root],
                causal_candidates=[root_candidate(root)],
                confirmations=[confirmation_for(root)],
                metadata={"provider_unavailable": True},
            ).analysis_outcome,
            "partial",
        )

    def test_recursive_state_collections_are_deeply_immutable(self):
        defect_state = sample_defect_state()
        node = TraceNode(
            ref="record:decision",
            record_id="decision",
            component="agent",
            event_type="decision",
            data={"nested": {"reason": "stop"}},
            source_refs=["record:prompt"],
        )
        candidate = CausalCandidate(
            ref=node.ref,
            node=node,
            source="confirmed_edge",
            edge={"context": {"confidence": 1.0}},
            evidence_refs=["record:change"],
        )
        assessment = PredecessorAssessment(ref=node.ref, evidence_refs=["record:change"])
        judgment = CausalStepJudgment(
            current_node_ref="record:change",
            current_defect_status="unknown",
            current_defect_reason="artifact missing",
            predecessors=[assessment],
            missing_evidence=["artifact:change"],
            suggested_investigation={"tool": "inspect_artifact"},
        )
        hypothesis = AttributionHypothesis.create("Decision is the root.", node.ref, defect_state).with_updates(
            unresolved_questions=["Was the artifact complete?"],
            counterfactual={"action": "search"},
        )
        confirmation = replace(
            RootConfirmation.unknown(
                node.ref, "artifact missing", evidence_refs=["artifact:change"]
            ),
            hypothesis_id="hyp:unknown",
            hypothesis_semantic_hash="semantic:unknown",
            defect_fingerprint=defect_state.fingerprint,
            recursive_path=(node.ref, "record:observed"),
            seed_binding_identity=seed_binding_identity_for(
                "record:observed", defect_state.fingerprint
            ),
        )
        _, factor, factor_metadata = factor_bundle(
            analysis_perspective="Immutable state test.",
        )
        report = RecursiveAttributionReport(
            case_id="immutable",
            objective="Find the root.",
            analysis_perspective="Immutable state test.",
            start_refs=["record:observed", node.ref],
            seed_results=[
                report_seed(
                    "evidence_gap",
                    start_ref="record:observed",
                    defect_state=defect_state,
                    confirmation_identities=(
                        confirmation.confirmation_identity,
                    ),
                ),
                report_seed(
                    "inconclusive",
                    start_ref=node.ref,
                    defect_state=defect_state,
                ),
            ],
            causal_candidates=[candidate],
            step_judgments=[judgment],
            hypotheses=[hypothesis],
            confirmations=[confirmation],
            contributing_conditions=[factor],
            taint_paths=[["record:observed", node.ref]],
            metadata={
                "nested": {"offline_only": True},
                **factor_metadata,
            },
        )
        item = sample_frontier_item()
        before = (candidate.to_dict(), hypothesis.semantic_hash, item.visit_key, report.to_dict())

        self.assertNotIsInstance(candidate.edge, dict)
        self.assertEqual(candidate.edge, {"context": {"confidence": 1.0}})
        self.assertEqual(list(candidate.edge), ["context"])
        self.assertEqual(candidate.edge["context"]["confidence"], 1.0)

        for callback in (
            lambda: candidate.evidence_refs.append("record:forged"),
            lambda: candidate.edge.__setitem__("forged", True),
            lambda: dict.__setitem__(candidate.edge, "forged", True),
            lambda: candidate.node.data["nested"].__setitem__("forged", True),
            lambda: dict.__setitem__(candidate.node.data["nested"], "forged", True),
            lambda: item.downstream_path.append("record:forged"),
            lambda: judgment.predecessors.append(assessment),
            lambda: judgment.suggested_investigation.__setitem__("forged", True),
            lambda: hypothesis.unresolved_questions.append("forged"),
            lambda: hypothesis.counterfactual.__setitem__("forged", True),
            lambda: confirmation.evidence_refs.append("record:forged"),
            lambda: factor.evidence_refs.append("record:forged"),
            lambda: report.causal_candidates.append(candidate),
            lambda: report.taint_paths[0].append("record:forged"),
            lambda: report.metadata["nested"].__setitem__("forged", True),
            lambda: dict.__setitem__(report.metadata, "forged", True),
        ):
            with self.assertRaises((AttributeError, TypeError)):
                callback()

        self.assertEqual(before, (candidate.to_dict(), hypothesis.semantic_hash, item.visit_key, report.to_dict()))

    def test_from_dict_rejects_forged_persisted_identity(self):
        defect_payload = sample_defect_state().to_dict()
        defect_payload["fingerprint"] = "forged"
        with self.assertRaisesRegex(ValueError, "DefectState fingerprint"):
            DefectState.from_dict(defect_payload)

        hypothesis = AttributionHypothesis.create(
            "Decision is the root.", "record:decision", sample_defect_state()
        )
        hypothesis_payload = hypothesis.to_dict()
        hypothesis_payload["semantic_hash"] = "forged"
        with self.assertRaisesRegex(ValueError, "AttributionHypothesis semantic_hash"):
            AttributionHypothesis.from_dict(hypothesis_payload)

        frontier_payload = sample_frontier_item().to_dict()
        frontier_payload["item_id"] = "frontier:forged"
        with self.assertRaisesRegex(ValueError, "FrontierItem item_id"):
            FrontierItem.from_dict(frontier_payload)
        frontier_payload = sample_frontier_item().to_dict()
        frontier_payload["visit_key"] = "forged"
        with self.assertRaisesRegex(ValueError, "FrontierItem visit_key"):
            FrontierItem.from_dict(frontier_payload)

    def test_legacy_root_cause_projection_matches_existing_candidate_fields(self):
        root = ConfirmedRoot(
            node_ref="record:decision",
            defect_state=sample_defect_state(),
            reason="The plan closed before searching call sites.",
            counterfactual="Searching call sites would reveal the method.",
            confidence=0.9,
            evidence_refs=["record:decision"],
            component="agent",
            event_type="decision",
            defect_type="premature_repository_search_closure",
            causal_role="defect_introduction",
            episode_id="episode:decision",
            episode_member_refs=["record:decision", "record:change"],
            observed_defect_refs=["record:observed"],
        )
        self.assertEqual(ConfirmedRoot.from_dict(root.to_dict()), root)
        self.assertEqual(
            root.to_legacy_root_cause(),
            {
                "node_ref": "record:decision",
                "component": "agent",
                "event_type": "decision",
                "defect_type": "premature_repository_search_closure",
                "reason": "The plan closed before searching call sites.",
                "confidence": 0.9,
                "causal_role": "defect_introduction",
                "episode_id": "episode:decision",
                "episode_member_refs": ["record:decision", "record:change"],
                "observed_defect_refs": ["record:observed"],
            },
        )
        report = RecursiveAttributionReport(
            case_id="legacy",
            objective="Find root.",
            **root_seed_fields(root),
            confirmed_roots=[root],
            confirmations=[confirmation_for(root)],
        )
        self.assertEqual(report.to_dict()["confirmed_roots"], [root.to_dict()])
        self.assertEqual(report.to_dict()["root_causes"], [root.to_legacy_root_cause()])


if __name__ == "__main__":
    unittest.main()
