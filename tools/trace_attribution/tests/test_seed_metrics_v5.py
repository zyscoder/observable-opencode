import copy
import unittest

from trace_attribution import seed_projection
from trace_attribution.causal_state import seed_binding_identity_for
from trace_attribution.seed_projection import SeedProjectionError


def _sha(character):
    return "sha256:" + character * 64


def _role(name):
    return {
        "node_ref": "record:{0}".format(name),
        "semantic_anchor_id": "semantic_anchor:v2:{0}".format(name),
        "semantic_occurrence_id": "semantic_occurrence:v1:{0}".format(name),
    }


def _seed(name, expected_outcome="confirmed_root", *, unresolved=()):
    start_ref = "record:start-{0}".format(name)
    fingerprint = "fingerprint-{0}".format(name)
    roots = [_role("root-{0}".format(name))] if expected_outcome == "confirmed_root" else []
    return {
        "defect_id": "defect-{0}".format(name),
        "start_ref": start_ref,
        "defect_fingerprint": fingerprint,
        "seed_binding_identity": seed_binding_identity_for(start_ref, fingerprint),
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
        "expected_outcome": expected_outcome,
        "roots": roots,
        "conditions": [],
        "amplifiers": [],
        "materializations": [],
        "unrelated": [],
        "forbidden_roots": [],
        "allowed_unresolved_outcomes": list(unresolved),
    }


def _labels(*seeds, shared=()):
    return {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": "seed-metrics-v5",
        "source_binding": {
            "trace_sha256": _sha("a"),
            "review_sha256": "",
            "evaluation_sha256": [],
            "effective_trace_sha256": _sha("b"),
            "subject_revision": "git:seed-metrics-v5",
            "revision_provenance_status": "valid",
        },
        "seeds": list(seeds),
        "case_shared_factors": list(shared),
    }


def _result(seed, outcome, *, missing=(), blocking=()):
    return {
        "start_ref": seed["start_ref"],
        "defect_fingerprint": seed["defect_fingerprint"],
        "seed_binding_identity": seed["seed_binding_identity"],
        "outcome": outcome,
        "confirmed_root_refs": (
            [item["node_ref"] for item in seed["roots"]]
            if outcome == "confirmed_root"
            else []
        ),
        "missing_evidence": list(missing),
        "blocking_reasons": list(blocking),
    }


def _report(labels, results, *, analysis_outcome="confirmed_root"):
    return {
        "case_id": labels["case_id"],
        "analysis_outcome": analysis_outcome,
        "seed_results": list(results),
        "confirmed_roots": [],
        "co_roots": [],
        "contributing_conditions": [],
        "amplifying_factors": [],
        "downstream_materializations": [],
        "rejected_candidates": [],
        "step_judgments": [],
        "introduction_candidates": [],
    }


def _publication(section, owner, entry):
    roles = {
        "confirmed_roots": "necessary_cause",
        "co_roots": "necessary_cause",
        "contributing_conditions": "contributing_condition",
        "amplifying_factors": "amplifying_factor",
        "downstream_materializations": "downstream_materialization",
        "rejected_candidates": "unrelated",
    }
    nested_name = (
        "role_judgment"
        if section == "downstream_materializations"
        else "confirmation"
    )
    ref_name = (
        "candidate_ref"
        if section == "downstream_materializations"
        else "node_ref"
    )
    judgment = {
        "candidate_ref": entry["node_ref"],
        "seed_binding_identity": owner,
        "factor_role": roles[section],
    }
    if section in {"confirmed_roots", "co_roots"}:
        judgment["status"] = "confirmed"
    return {
        ref_name: entry["node_ref"],
        "semantic_occurrence_id": entry["semantic_occurrence_id"],
        nested_name: judgment,
        "provenance": {"seed_binding_identity": owner},
    }


def _publish_expected_roots(report, *seeds):
    for seed in seeds:
        for entry in seed["roots"]:
            report["confirmed_roots"].append(
                _publication(
                    "confirmed_roots", seed["seed_binding_identity"], entry
                )
            )


class SeedMetricsV5Test(unittest.TestCase):
    def score(self, report, labels, *, bound=None, **safety):
        scorer = getattr(seed_projection, "score_seed_owned_report", None)
        self.assertIsNotNone(scorer, "Task 4 public scorer is missing")
        if bound is None:
            bound = [seed["seed_binding_identity"] for seed in labels["seeds"]]
        return scorer(
            report,
            labels,
            bound_seed_binding_identities=bound,
            **safety
        ).to_dict()

    def test_swapped_roots_fail_each_seed_top1_and_micro_identity(self):
        first, second = _seed("first"), _seed("second")
        labels = _labels(first, second)
        report = _report(labels, [_result(first, "confirmed_root"), _result(second, "confirmed_root")])
        report["confirmed_roots"] = [
            _publication("confirmed_roots", second["seed_binding_identity"], first["roots"][0]),
            _publication("confirmed_roots", first["seed_binding_identity"], second["roots"][0]),
        ]

        scored = self.score(report, labels)

        self.assertEqual(scored["schema_version"], "recursive-attribution-comparison/v8")
        for seed in scored["per_seed"]:
            self.assertEqual(seed["root_metrics"]["precision"], 0.0)
            self.assertEqual(seed["root_metrics"]["recall"], 0.0)
            self.assertFalse(seed["top1_match"])
        self.assertEqual(scored["micro"]["root_metrics"]["tp"], 0)
        self.assertEqual(scored["micro"]["root_metrics"]["fp"], 2)
        self.assertEqual(scored["micro"]["root_metrics"]["fn"], 2)
        self.assertFalse(scored["all_seed_coverage_passed"])

    def test_shared_factor_expands_once_per_declared_seed(self):
        first, second = _seed("first"), _seed("second")
        shared = {
            "factor_role": "amplifying_factor",
            "seed_binding_identities": sorted(
                [first["seed_binding_identity"], second["seed_binding_identity"]]
            ),
            **_role("shared-timeout"),
        }
        labels = _labels(first, second, shared=[shared])
        report = _report(labels, [_result(first, "confirmed_root"), _result(second, "confirmed_root")])
        _publish_expected_roots(report, first, second)
        for seed in (first, second):
            report["amplifying_factors"].append(
                _publication("amplifying_factors", seed["seed_binding_identity"], shared)
            )

        scored = self.score(report, labels)

        self.assertEqual(scored["micro"]["role_pair_metrics"]["tp"], 4)
        self.assertEqual(scored["micro"]["role_pair_metrics"]["fp"], 0)
        self.assertEqual(scored["micro"]["role_pair_metrics"]["fn"], 0)
        self.assertEqual(scored["macro"]["role_pair_f1"], {"value": 1.0, "contributor_count": 2})
        self.assertTrue(scored["all_seed_coverage_passed"])

    def test_confirmed_no_defect_and_unresolved_outcomes_are_explicit(self):
        confirmed = _seed("confirmed")
        no_defect = _seed("clean", "no_defect")
        unresolved = _seed(
            "unresolved", "unresolved", unresolved=("partial_root_found",)
        )
        labels = _labels(confirmed, no_defect, unresolved)
        report = _report(
            labels,
            [
                _result(confirmed, "confirmed_root"),
                _result(no_defect, "no_defect"),
                _result(unresolved, "evidence_gap", missing=("missing branch",)),
            ],
            analysis_outcome="partial",
        )
        _publish_expected_roots(report, confirmed)

        scored = self.score(report, labels)
        by_id = {item["seed_binding_identity"]: item for item in scored["per_seed"]}

        self.assertTrue(by_id[confirmed["seed_binding_identity"]]["top1_match"])
        self.assertIsNone(by_id[no_defect["seed_binding_identity"]]["top1_match"])
        self.assertIsNone(by_id[unresolved["seed_binding_identity"]]["top1_match"])
        self.assertEqual(
            by_id[no_defect["seed_binding_identity"]]["root_metrics"],
            {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None, "f1": None},
        )
        unresolved_score = by_id[unresolved["seed_binding_identity"]]
        self.assertEqual(unresolved_score["terminal_outcome"], "unresolved")
        self.assertEqual(unresolved_score["matched_unresolved_outcome"], "partial_root_found")
        self.assertTrue(all(item["outcome_agreement"] for item in scored["per_seed"]))
        self.assertEqual(scored["macro"]["root_f1"], {"value": 1.0, "contributor_count": 1})
        self.assertEqual(scored["macro"]["top1_match"], {"value": 1.0, "contributor_count": 1})
        self.assertEqual(scored["macro"]["outcome_agreement"], {"value": 1.0, "contributor_count": 3})
        self.assertTrue(scored["all_seed_coverage_passed"])

    def test_forbidden_owned_root_is_a_seed_safety_failure(self):
        seed = _seed("forbidden")
        forbidden = _role("forbidden-candidate")
        seed["forbidden_roots"] = [forbidden]
        labels = _labels(seed)
        report = _report(labels, [_result(seed, "confirmed_root")])
        _publish_expected_roots(report, seed)
        report["co_roots"].append(
            _publication("co_roots", seed["seed_binding_identity"], forbidden)
        )

        scored = self.score(report, labels)

        item = scored["per_seed"][0]
        self.assertFalse(item["forbidden_root_passed"])
        self.assertEqual(item["forbidden_root_occurrences"], [forbidden["semantic_occurrence_id"]])
        self.assertFalse(scored["all_seed_coverage_passed"])

    def test_coverage_and_checkpoint_safety_are_published_separately(self):
        first, second = _seed("first"), _seed("second")
        labels = _labels(first, second)
        report = _report(labels, [_result(first, "confirmed_root"), _result(second, "confirmed_root")])
        _publish_expected_roots(report, first, second)

        scored = self.score(
            report,
            labels,
            bound=[first["seed_binding_identity"]],
            checkpoint_safety_passed=False,
        )

        self.assertEqual(scored["coverage"]["label_seed_coverage"], {"covered": 1, "declared": 2, "value": 0.5})
        self.assertEqual(scored["coverage"]["report_seed_coverage"], {"covered": 2, "declared": 2, "value": 1.0})
        self.assertEqual(scored["coverage"]["terminal_seed_coverage"], {"covered": 2, "declared": 2, "value": 1.0})
        self.assertFalse(scored["safety"]["checkpoint_safety_passed"])
        self.assertFalse(scored["all_seed_coverage_passed"])

    def test_terminal_contradiction_reduces_terminal_coverage(self):
        seed = _seed("contradiction")
        labels = _labels(seed)
        report = _report(labels, [_result(seed, "no_defect")], analysis_outcome="no_defect")
        _publish_expected_roots(report, seed)

        scored = self.score(report, labels)

        item = scored["per_seed"][0]
        self.assertFalse(item["terminal_valid"])
        self.assertFalse(item["outcome_agreement"])
        self.assertEqual(scored["coverage"]["terminal_seed_coverage"]["value"], 0.0)
        self.assertFalse(scored["all_seed_coverage_passed"])

    def test_missing_or_extra_report_seed_fails_closed(self):
        first, second = _seed("first"), _seed("second")
        labels = _labels(first, second)
        report = _report(labels, [_result(first, "confirmed_root")])

        with self.assertRaisesRegex(SeedProjectionError, "report seed"):
            self.score(report, labels)

        extra = copy.deepcopy(report)
        third = _seed("third")
        extra["seed_results"].extend([_result(second, "confirmed_root"), _result(third, "confirmed_root")])
        with self.assertRaisesRegex(SeedProjectionError, "report seed"):
            self.score(extra, labels)

    def test_non_root_permutations_do_not_change_metrics(self):
        first, second = _seed("first"), _seed("second")
        first["conditions"] = [_role("condition-first")]
        second["conditions"] = [_role("condition-second")]
        labels = _labels(first, second)
        report = _report(labels, [_result(first, "confirmed_root"), _result(second, "confirmed_root")])
        _publish_expected_roots(report, first, second)
        for seed in (first, second):
            report["contributing_conditions"].append(
                _publication("contributing_conditions", seed["seed_binding_identity"], seed["conditions"][0])
            )
        permuted = copy.deepcopy(report)
        permuted["seed_results"].reverse()
        permuted["contributing_conditions"].reverse()

        self.assertEqual(self.score(report, labels), self.score(permuted, labels))


if __name__ == "__main__":
    unittest.main()
