import copy
import json
import unittest
from pathlib import Path

from trace_attribution.causal_state import seed_binding_identity_for
from trace_attribution.seed_projection import (
    SeedProjectionError,
    project_seed_owned_report,
)


def _sha(character):
    return "sha256:" + character * 64


def _seed(name="one", *, start_ref=None, fingerprint=None, binding=None):
    start_ref = start_ref or "record:start-{0}".format(name)
    fingerprint = fingerprint or "fingerprint-{0}".format(name)
    binding = binding or seed_binding_identity_for(start_ref, fingerprint)
    return {
        "defect_id": "defect-{0}".format(name),
        "start_ref": start_ref,
        "defect_fingerprint": fingerprint,
        "seed_binding_identity": binding,
        "seed_semantic_anchor_id": "semantic_anchor:v2:seed-{0}".format(name),
        "seed_semantic_occurrence_id": (
            "semantic_occurrence:v1:seed-{0}".format(name)
        ),
        "seed_source_bindings": [
            {
                "source_kind": "raw_trace",
                "source_index": 0,
                "source_sha256": _sha("a"),
            }
        ],
        "defect_origin_kind": "introduced_by_agent",
        "expected_outcome": "confirmed_root",
        "roots": [
            {
                "node_ref": "record:expected-root-{0}".format(name),
                "semantic_anchor_id": (
                    "semantic_anchor:v2:expected-root-{0}".format(name)
                ),
                "semantic_occurrence_id": (
                    "semantic_occurrence:v1:expected-root-{0}".format(name)
                ),
            }
        ],
        "conditions": [],
        "amplifiers": [],
        "materializations": [],
        "unrelated": [],
        "forbidden_roots": [],
        "allowed_unresolved_outcomes": [],
    }


def _labels(*seeds, case_id="projection-v5"):
    seeds = list(seeds or (_seed(),))
    return {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": case_id,
        "source_binding": {
            "trace_sha256": _sha("a"),
            "review_sha256": "",
            "evaluation_sha256": [],
            "effective_trace_sha256": _sha("b"),
            "subject_revision": "git:projection-v5",
            "revision_provenance_status": "valid",
        },
        "seeds": seeds,
        "case_shared_factors": [],
    }


def _report(labels):
    return {
        "seed_results": [
            {
                "start_ref": seed["start_ref"],
                "defect_fingerprint": seed["defect_fingerprint"],
                "seed_binding_identity": seed["seed_binding_identity"],
            }
            for seed in labels["seeds"]
        ],
        "confirmed_roots": [],
        "co_roots": [],
        "contributing_conditions": [],
        "amplifying_factors": [],
        "downstream_materializations": [],
        "rejected_candidates": [],
        "step_judgments": [],
        "introduction_candidates": [],
    }


def _publication(section, owner, node_ref, occurrence, *, role=None):
    roles = {
        "confirmed_roots": "necessary_cause",
        "co_roots": "necessary_cause",
        "contributing_conditions": "contributing_condition",
        "amplifying_factors": "amplifying_factor",
        "downstream_materializations": "downstream_materialization",
        "rejected_candidates": "unrelated",
    }
    nested_key = (
        "role_judgment"
        if section == "downstream_materializations"
        else "confirmation"
    )
    ref_key = (
        "candidate_ref"
        if section == "downstream_materializations"
        else "node_ref"
    )
    factor_role = role or roles[section]
    judgment = {
        "candidate_ref": node_ref,
        "seed_binding_identity": owner,
        "factor_role": factor_role,
        "reason": "runtime-compatible judgment",
    }
    if factor_role == "necessary_cause":
        judgment["status"] = "confirmed"
    return {
        ref_key: node_ref,
        "semantic_occurrence_id": occurrence,
        nested_key: judgment,
        "provenance": {"seed_binding_identity": owner},
    }


def _introduction_step(owner, node_ref, occurrence, suffix):
    return {
        "current_node_ref": node_ref,
        "semantic_occurrence_id": occurrence,
        "candidate_introduction": True,
        "owner": {
            "seed_binding_identity": owner,
            "hypothesis_id": "hyp:{0}".format(suffix),
            "visit_key": "visit:{0}".format(suffix),
        },
        "reason": "evidence path {0}".format(suffix),
        "confidence": 0.7 if suffix == "one" else 0.9,
    }


class SeedProjectionV5RuntimeCorrectionTest(unittest.TestCase):
    def test_rejects_conflicting_final_roles_by_occurrence_or_node(self):
        labels = _labels()
        owner = labels["seeds"][0]["seed_binding_identity"]

        for same_field in ("occurrence", "node_ref"):
            with self.subTest(same_field=same_field):
                report = _report(labels)
                first_ref = "record:shared" if same_field == "node_ref" else "record:a"
                second_ref = "record:shared" if same_field == "node_ref" else "record:b"
                first_occurrence = (
                    "semantic_occurrence:v1:shared"
                    if same_field == "occurrence"
                    else "semantic_occurrence:v1:a"
                )
                second_occurrence = (
                    "semantic_occurrence:v1:shared"
                    if same_field == "occurrence"
                    else "semantic_occurrence:v1:b"
                )
                report["confirmed_roots"] = [
                    _publication(
                        "confirmed_roots",
                        owner,
                        first_ref,
                        first_occurrence,
                    )
                ]
                report["contributing_conditions"] = [
                    _publication(
                        "contributing_conditions",
                        owner,
                        second_ref,
                        second_occurrence,
                    )
                ]

                with self.assertRaisesRegex(
                    SeedProjectionError, "causal role|contradict"
                ):
                    project_seed_owned_report(report, labels)

    def test_unowned_aggregate_introduction_is_ignored(self):
        labels = _labels()
        report = _report(labels)
        report["introduction_candidates"] = [
            {
                "ref": "record:aggregate-only",
                "semantic_occurrence_id": (
                    "semantic_occurrence:v1:aggregate-only"
                ),
                "score": 1.0,
            }
        ]

        projection = project_seed_owned_report(report, labels)

        self.assertEqual(projection.records, ())

    def test_duplicate_owned_introduction_paths_collapse_semantically(self):
        labels = _labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        report = _report(labels)
        node_ref = "record:introduction"
        occurrence = "semantic_occurrence:v1:introduction"
        report["step_judgments"] = [
            _introduction_step(owner, node_ref, occurrence, "one"),
            _introduction_step(owner, node_ref, occurrence, "two"),
        ]

        projection = project_seed_owned_report(report, labels)

        introductions = [
            item
            for item in projection.records
            if item.role == "introduction_candidate"
        ]
        self.assertEqual(len(introductions), 1)
        self.assertEqual(introductions[0].node_ref, node_ref)

    def test_introduction_discovery_can_coexist_with_one_final_role(self):
        labels = _labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        report = _report(labels)
        node_ref = "record:discovered-root"
        occurrence = "semantic_occurrence:v1:discovered-root"
        report["confirmed_roots"] = [
            _publication("confirmed_roots", owner, node_ref, occurrence)
        ]
        report["step_judgments"] = [
            _introduction_step(owner, node_ref, occurrence, "root-path")
        ]

        projection = project_seed_owned_report(report, labels)

        self.assertEqual(
            {
                item.role
                for item in projection.records
                if item.semantic_occurrence_id == occurrence
            },
            {"root", "introduction_candidate"},
        )

    def test_unknown_rejected_candidate_is_unresolved_and_not_scored(self):
        labels = _labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        report = _report(labels)
        report["rejected_candidates"] = [
            _publication(
                "rejected_candidates",
                owner,
                "record:unresolved",
                "semantic_occurrence:v1:unresolved",
                role="unknown",
            )
        ]

        projection = project_seed_owned_report(report, labels)

        self.assertFalse(
            any(item.role == "unrelated" for item in projection.records)
        )

    def test_malformed_present_ownership_fields_fail_closed(self):
        labels = _labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        mutations = (
            ("scalar", {"seed_binding_identity": 7}),
            ("plural", {"seed_binding_identities": owner}),
            ("nested_plural", {"provenance": {"seed_binding_identities": {owner}}}),
        )
        for name, mutation in mutations:
            with self.subTest(name=name):
                report = _report(labels)
                publication = _publication(
                    "confirmed_roots",
                    owner,
                    "record:root",
                    "semantic_occurrence:v1:root",
                )
                for key, value in mutation.items():
                    if key == "provenance":
                        publication[key].update(value)
                    else:
                        publication[key] = value
                report["confirmed_roots"] = [publication]

                with self.assertRaisesRegex(
                    SeedProjectionError, "ownership|seed_binding"
                ):
                    project_seed_owned_report(report, labels)

    def test_current_v22_flash_report_and_real_root_shape_are_evaluable(self):
        report_path = Path(
            ".benchmark-runs/attribution-convergence-vnext-20260803/"
            "sphinx-flash-probe/recursive.attribution.json"
        )
        if not report_path.exists():
            self.skipTest("read-only Sphinx Flash runtime report is unavailable")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        runtime_seed = report["seed_results"][0]
        labels = _labels(
            _seed(
                "runtime",
                start_ref=runtime_seed["start_ref"],
                fingerprint=runtime_seed["defect_fingerprint"],
                binding=runtime_seed["seed_binding_identity"],
            ),
            case_id=report["case_id"],
        )

        projection = project_seed_owned_report(report, labels)

        self.assertEqual(
            {item.role for item in projection.records},
            {"root", "amplifying_factor", "downstream_materialization"},
        )
        self.assertNotIn(
            "introduction_candidate", {item.role for item in projection.records}
        )

        co_root_report = copy.deepcopy(report)
        co_root_report["co_roots"] = [co_root_report["confirmed_roots"].pop()]
        co_root_projection = project_seed_owned_report(co_root_report, labels)
        self.assertEqual(
            [item.role for item in co_root_projection.records].count("root"),
            1,
        )


if __name__ == "__main__":
    unittest.main()
