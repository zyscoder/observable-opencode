from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass

from trace_attribution.candidate_paging import (
    CANDIDATE_PAGE_SIZE,
    CandidatePagePlan,
    build_candidate_page_outcome,
    build_candidate_page_plan,
    summarize_candidate_round,
)
from trace_attribution.global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
)


@dataclass(frozen=True)
class _Capsule:
    candidate_ref: str
    root_candidate_eligible: bool = True
    revision: int = 1

    def to_dict(self) -> dict:
        return {
            "candidate_ref": self.candidate_ref,
            "candidate": {
                "root_candidate_eligible": self.root_candidate_eligible,
                "revision": self.revision,
            },
        }


def _capsules(count: int) -> tuple[_Capsule, ...]:
    return tuple(_Capsule("record:candidate-{0:03d}".format(index)) for index in range(count))


def _counterfactual(ref: str, *, prevents_defect: bool) -> dict:
    return {
        "intervention_ref": ref,
        "intervention_kind": "replace_with_semantically_correct_behavior",
        "predicted_defect_status": "absent" if prevents_defect else "present",
        "causal_effect": (
            "prevents_defect" if prevents_defect else "does_not_prevent_defect"
        ),
    }


def _assessment(
    ref: str,
    *,
    input_status: str = "absent",
    output_status: str = "absent",
    causal_role: str = "unrelated",
    path: tuple[str, ...] | None = None,
    prevents_defect: bool = False,
    reason: str = "Canonical structured assessment.",
) -> GlobalCandidateAssessment:
    is_root = causal_role == "root_candidate"
    is_factor = causal_role in {
        "contributing_condition",
        "amplifying_factor",
    }
    return GlobalCandidateAssessment(
        candidate_ref=ref,
        defect_status=output_status,
        input_defect_status=input_status,
        output_defect_status=output_status,
        causal_path_refs=path if path is not None else (ref, "record:seed"),
        counterfactual=_counterfactual(ref, prevents_defect=prevents_defect),
        compared_candidate_refs=(),
        causal_role=causal_role,
        responsibility=(
            "primary" if is_root else ("shared" if is_factor else "none")
        ),
        candidate_phase=(
            "implementation"
            if is_root
            else ("planning" if is_factor else "intermediate")
        ),
        obligation_status_before="unknown",
        obligation_status_after="unknown",
        repair_window_effect="remained_open",
        failure_mode=(
            "positive_introduction"
            if is_root
            else ("omission_enabling_condition" if is_factor else "none")
        ),
        obligation_refs=(),
        contribution_mechanism=(
            {
                "type": "scope_narrowing",
                "target_ref": "record:seed",
                "effect": "Narrowed the implementation scope reaching the defect.",
                "evidence_refs": (ref,),
            }
            if is_factor
            else None
        ),
        reason=reason,
        evidence_refs=(ref,),
        confidence=0.9,
    )


def _judgment(
    assessments: tuple[GlobalCandidateAssessment, ...],
    *,
    selected: tuple[str, ...] = (),
    outcome: str = "inconclusive",
) -> GlobalCandidateJudgment:
    return GlobalCandidateJudgment(
        outcome=outcome,
        reason="Canonical judgment reason.",
        assessments=assessments,
        selected_candidate_refs=selected,
        expansion_requests=(),
        decisive_evidence_refs=(),
        missing_evidence=("Further comparison is required.",),
        confidence=0.8,
        active_focus_binding={
            "seed_ref": "record:seed",
            "defect_fingerprint": "defect-1",
            "active_focus_text_hash": "f" * 64,
        },
    )


class CandidatePagePlanTest(unittest.TestCase):
    def _plan(self, count: int) -> CandidatePagePlan:
        return build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=_capsules(count),
            round_index=0,
        )

    def test_boundary_counts_keep_small_complete_chains_on_one_page(self):
        expected_page_sizes = {
            0: [],
            1: [1],
            4: [4],
            5: [5],
            8: [8],
            9: [8, 1],
            24: [8] * 3,
            25: ([8] * 3) + [1],
            48: [8] * 6,
            256: [8] * 32,
        }
        for count, expected in expected_page_sizes.items():
            with self.subTest(count=count):
                plan = self._plan(count)
                self.assertEqual(plan.page_size, CANDIDATE_PAGE_SIZE)
                self.assertEqual([len(page.candidate_refs) for page in plan.pages], expected)
                self.assertTrue(
                    all(len(page.candidate_refs) <= 8 for page in plan.pages)
                )

    def test_every_candidate_appears_exactly_once_without_reordering(self):
        capsules = _capsules(256)
        plan = build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=capsules,
            round_index=3,
        )
        flattened_refs = tuple(
            ref for page in plan.pages for ref in page.candidate_refs
        )
        flattened_identities = tuple(
            identity
            for page in plan.pages
            for identity in page.candidate_identities
        )
        self.assertEqual(flattened_refs, tuple(item.candidate_ref for item in capsules))
        self.assertEqual(flattened_identities, plan.candidate_identities)
        self.assertEqual(len(flattened_refs), len(set(flattened_refs)))
        self.assertEqual(len(flattened_identities), len(set(flattened_identities)))

    def test_identity_is_stable_and_input_order_sensitive(self):
        capsules = _capsules(25)
        first = build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=capsules,
            round_index=2,
        )
        second = build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=capsules,
            round_index=2,
        )
        reordered = build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=tuple(reversed(capsules)),
            round_index=2,
        )
        changed_capsule = build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=(_Capsule(capsules[0].candidate_ref, revision=2),) + capsules[1:],
            round_index=2,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.identity, second.identity)
        self.assertNotEqual(first.identity, reordered.identity)
        self.assertNotEqual(first.identity, changed_capsule.identity)
        self.assertNotEqual(first.pages[0].identity, reordered.pages[0].identity)

    def test_duplicate_candidate_ref_or_capsule_identity_is_rejected(self):
        duplicated = (_Capsule("record:duplicate"), _Capsule("record:duplicate"))
        with self.assertRaisesRegex(ValueError, "duplicate candidate_ref"):
            build_candidate_page_plan(
                seed_ref="record:seed",
                defect_fingerprint="defect-1",
                capsules=duplicated,
                round_index=0,
            )

    def test_strict_round_trip_rejects_schema_identity_and_membership_drift(self):
        plan = self._plan(25)
        payload = plan.to_dict()
        self.assertEqual(CandidatePagePlan.from_dict(payload), plan)

        mutations = {}
        extra = copy.deepcopy(payload)
        extra["unexpected"] = True
        mutations["schema"] = extra

        wrong_identity = copy.deepcopy(payload)
        wrong_identity["identity"] = "0" * 64
        mutations["plan identity"] = wrong_identity

        wrong_page_identity = copy.deepcopy(payload)
        wrong_page_identity["pages"][0]["identity"] = "0" * 64
        mutations["page identity"] = wrong_page_identity

        omitted = copy.deepcopy(payload)
        omitted["pages"][0]["candidate_refs"].pop()
        omitted["pages"][0]["candidate_identities"].pop()
        mutations["omitted membership"] = omitted

        duplicated = copy.deepcopy(payload)
        duplicated["pages"][1]["candidate_refs"][0] = duplicated["pages"][0][
            "candidate_refs"
        ][0]
        duplicated["pages"][1]["candidate_identities"][0] = duplicated["pages"][0][
            "candidate_identities"
        ][0]
        mutations["duplicated membership"] = duplicated

        oversized = copy.deepcopy(self._plan(48).to_dict())
        oversized["pages"][0]["candidate_refs"].append(
            oversized["pages"][1]["candidate_refs"][0]
        )
        oversized["pages"][0]["candidate_identities"].append(
            oversized["pages"][1]["candidate_identities"][0]
        )
        mutations["oversized page"] = oversized

        non_json_array = copy.deepcopy(payload)
        non_json_array["candidate_refs"] = tuple(
            non_json_array["candidate_refs"]
        )
        mutations["non-JSON array"] = non_json_array

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises((TypeError, ValueError)):
                    CandidatePagePlan.from_dict(mutated)


class CandidatePageOutcomeTest(unittest.TestCase):
    def _page(self, refs: tuple[str, ...]):
        capsules = tuple(_Capsule(ref) for ref in refs)
        return build_candidate_page_plan(
            seed_ref="record:seed",
            defect_fingerprint="defect-1",
            capsules=capsules,
            round_index=0,
        ).pages[0]

    def test_projects_five_mutually_exclusive_conserved_categories(self):
        refs = (
            "record:selected",
            "record:supported",
            "record:unresolved",
            "record:factor",
        )
        judgment = _judgment(
            (
                _assessment(
                    refs[0],
                    output_status="present",
                    causal_role="root_candidate",
                    prevents_defect=True,
                ),
                _assessment(
                    refs[1],
                    output_status="present",
                    causal_role="root_candidate",
                    prevents_defect=True,
                ),
                _assessment(
                    refs[2],
                    input_status="unknown",
                    output_status="unknown",
                    causal_role="unknown",
                    path=(),
                ),
                _assessment(
                    refs[3],
                    output_status="present",
                    causal_role="contributing_condition",
                ),
            ),
            selected=(refs[0],),
            outcome="candidate_roots",
        )
        outcome = build_candidate_page_outcome(
            page=self._page(refs),
            judgment=judgment,
            root_eligible_candidate_refs=refs,
        )

        self.assertEqual(outcome.selected_root_hypothesis_refs, (refs[0],))
        self.assertEqual(outcome.supported_root_hypothesis_refs, (refs[1],))
        self.assertEqual(outcome.unresolved_root_hypothesis_refs, (refs[2],))
        self.assertEqual(outcome.non_root_factor_refs, (refs[3],))
        self.assertEqual(outcome.excluded_refs, ())
        self.assertEqual(outcome.classified_candidate_refs, refs)
        self.assertEqual(type(outcome).from_dict(outcome.to_dict()), outcome)
        excluded_ref = "record:excluded"
        excluded = build_candidate_page_outcome(
            page=self._page((excluded_ref,)),
            judgment=_judgment(
                (_assessment(excluded_ref, causal_role="unrelated"),)
            ),
            root_eligible_candidate_refs=(excluded_ref,),
        )
        self.assertEqual(excluded.excluded_refs, (excluded_ref,))

    def test_reason_text_cannot_change_survivor_classification(self):
        ref = "record:reason-must-not-matter"
        structural = dict(
            input_status="unknown",
            output_status="unknown",
            causal_role="unknown",
            path=(),
        )
        first = build_candidate_page_outcome(
            page=self._page((ref,)),
            judgment=_judgment(
                (
                    _assessment(
                        ref,
                        reason="Definitely the selected root cause.",
                        **structural,
                    ),
                )
            ),
            root_eligible_candidate_refs=(ref,),
        )
        second = build_candidate_page_outcome(
            page=self._page((ref,)),
            judgment=_judgment(
                (
                    _assessment(
                        ref,
                        reason="Definitely unrelated and should be excluded.",
                        **structural,
                    ),
                )
            ),
            root_eligible_candidate_refs=(ref,),
        )
        self.assertEqual(
            first.unresolved_root_hypothesis_refs,
            second.unresolved_root_hypothesis_refs,
        )
        self.assertEqual(first.classification_by_ref, second.classification_by_ref)

    def test_unresolved_candidates_are_not_limited_by_finalist_soft_limit(self):
        supported_refs = ("record:supported-1", "record:supported-2")
        unresolved_refs = tuple(
            "record:unresolved-{0}".format(index) for index in range(2)
        )
        refs = supported_refs + unresolved_refs
        assessments = tuple(
            _assessment(
                ref,
                output_status="present",
                causal_role="root_candidate",
                prevents_defect=True,
            )
            for ref in supported_refs
        ) + tuple(
            _assessment(
                ref,
                input_status="unknown",
                output_status="unknown",
                causal_role="unknown",
                path=(),
            )
            for ref in unresolved_refs
        )
        page_outcome = build_candidate_page_outcome(
            page=self._page(refs),
            judgment=_judgment(assessments),
            root_eligible_candidate_refs=refs,
        )
        summary = summarize_candidate_round(
            round_index=0,
            page_outcomes=(page_outcome,),
            finalist_soft_limit=1,
        )

        self.assertEqual(summary.finalist_candidate_refs, supported_refs)
        self.assertEqual(
            summary.unresolved_root_hypothesis_refs,
            unresolved_refs,
        )
        self.assertTrue(summary.requires_comparison_round)
        self.assertEqual(type(summary).from_dict(summary.to_dict()), summary)

    def test_projection_rejects_missing_duplicate_or_foreign_assessments(self):
        refs = ("record:a", "record:b")
        page = self._page(refs)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            build_candidate_page_outcome(
                page=page,
                judgment=_judgment((_assessment(refs[0]),)),
                root_eligible_candidate_refs=refs,
            )
        with self.assertRaisesRegex(ValueError, "exactly once"):
            build_candidate_page_outcome(
                page=page,
                judgment=_judgment(
                    (_assessment(refs[0]), _assessment(refs[0]))
                ),
                root_eligible_candidate_refs=refs,
            )
        with self.assertRaisesRegex(ValueError, "root-eligible"):
            build_candidate_page_outcome(
                page=page,
                judgment=_judgment(
                    (_assessment(refs[0]), _assessment(refs[1]))
                ),
                root_eligible_candidate_refs=refs + ("record:foreign",),
            )


if __name__ == "__main__":
    unittest.main()
