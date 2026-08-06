import copy
import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_attribution.benchmark_composition import compose_effective_trace
from trace_attribution.causal_state import seed_binding_identity_for
from trace_attribution.causal_state import semantic_anchor_index
from trace_attribution.causal_state import semantic_occurrence_index
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from scripts.evaluate_recursive_attribution import validate_labels as legacy_validate_labels

try:
    from trace_attribution.label_contract import (
        LabelContractError,
        validate_labels,
    )
except ModuleNotFoundError:
    LabelContractError = ValueError

    def validate_labels(value):
        return value

try:
    from trace_attribution.input_binding import (
        InputBindingError,
        LabelInputBinding,
        bind_label_inputs,
    )
except ModuleNotFoundError:
    InputBindingError = ValueError
    LabelInputBinding = None

    def bind_label_inputs(*args, **kwargs):
        return None

try:
    from trace_attribution.seed_projection import (
        SeedOwnedReportProjection,
        SeedProjectionError,
        project_seed_owned_report,
    )
except ModuleNotFoundError:
    SeedProjectionError = ValueError
    SeedOwnedReportProjection = None

    def project_seed_owned_report(*args, **kwargs):
        return None


def _digest(value):
    return "sha256:" + value * 64


def _role_entry(name):
    return {
        "node_ref": "record:{0}".format(name),
        "semantic_anchor_id": "semantic_anchor:v2:{0}".format(name),
        "semantic_occurrence_id": "semantic_occurrence:v1:{0}".format(name),
    }


def _seed_source_binding(source_kind, source_index, source_sha256):
    return {
        "source_kind": source_kind,
        "source_index": source_index,
        "source_sha256": source_sha256,
    }


def _seed(name):
    start_ref = "record:start-{0}".format(name)
    defect_fingerprint = "fingerprint-{0}".format(name)
    return {
        "defect_id": "defect-{0}".format(name),
        "start_ref": start_ref,
        "defect_fingerprint": defect_fingerprint,
        "seed_binding_identity": seed_binding_identity_for(start_ref, defect_fingerprint),
        "seed_semantic_anchor_id": "semantic_anchor:v2:seed-{0}".format(name),
        "seed_semantic_occurrence_id": "semantic_occurrence:v1:seed-{0}".format(name),
        "seed_source_bindings": [
            _seed_source_binding("raw_trace", 0, _digest("a"))
        ],
        "defect_origin_kind": "introduced_by_agent",
        "expected_outcome": "confirmed_root",
        "roots": [_role_entry("root-{0}".format(name))],
        "conditions": [],
        "amplifiers": [],
        "materializations": [],
        "unrelated": [],
        "forbidden_roots": [],
        "allowed_unresolved_outcomes": [],
    }


def _v5_labels():
    return {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": "v5-contract-case",
        "source_binding": {
            "trace_sha256": _digest("a"),
            "review_sha256": "",
            "evaluation_sha256": [_digest("b"), _digest("c")],
            "effective_trace_sha256": _digest("d"),
            "subject_revision": "revision-1",
            "revision_provenance_status": "valid",
        },
        "seeds": [_seed("one"), _seed("two")],
        "case_shared_factors": [],
    }


def _shared_factor(labels, name="shared", role="amplifying_factor"):
    return {
        "factor_role": role,
        "seed_binding_identities": sorted(
            [seed["seed_binding_identity"] for seed in labels["seeds"]]
        ),
        **_role_entry(name),
    }


def _binding_trace():
    return {
        "manifest": {
            "case_id": "binding-case",
            "run_id": "binding-run",
            "subject_revision": "git:binding-revision",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "binding-case",
                "run_id": "binding-run",
            },
        },
        "records": [
            {
                "record_id": "raw_seed",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "binding-probe"},
            }
        ],
        "dataflow_edges": [],
    }


def _binding_review():
    return {
        "case_id": "binding-case",
        "quality_review": {
            "quality_gaps": [
                {
                    "dimension": "correctness",
                    "score": 0,
                    "max_score": 1,
                    "gap_context_refs": ["record:raw_seed"],
                }
            ]
        },
    }


def _binding_evaluation(assertion, observed_at):
    return {
        "source": "benchmark",
        "scope": "binding-order",
        "subject_revision": "git:binding-revision",
        "assertion": assertion,
        "observation": "failed",
        "status": "failed",
        "observed_at": observed_at,
        "evidence_refs": ["record:raw_seed"],
        "provenance": {"method": "offline_test", "version": "1"},
    }


def _structured_binding_review():
    return {
        "case_id": "binding-case",
        "observed_defects": [
            {
                "failure_signature_schema_version": (
                    "evaluation-failure-signature/v1"
                ),
                "exception_family": "AssertionError",
                "first_business_frame": {
                    "file": "benchmark/check.py",
                    "symbol": "verify_binding",
                },
                "assertion_contract": "the bound result must replay",
                "relevant_symbol": "verify_binding",
                "subsystem": "benchmark.binding",
                "evidence_refs": ["record:raw_seed"],
            }
        ],
    }


def _structured_binding_evaluation():
    payload = _binding_evaluation(
        "the bound result must replay",
        "2026-08-03T00:00:02Z",
    )
    payload["scope"] = "structured-binding"
    payload["provenance"] = {
        "method": "offline_test",
        "version": "1",
        "failure_signature": {
            "schema_version": "evaluation-failure-signature/v1",
            "exception_family": "AssertionError",
            "first_business_frame": {
                "file": "benchmark/check.py",
                "symbol": "verify_binding",
            },
            "assertion_contract": "the bound result must replay",
            "relevant_symbol": "verify_binding",
            "subsystem": "benchmark.binding",
        },
    }
    return payload


def _json_bytes(value, *, indent=None):
    return json.dumps(value, ensure_ascii=False, indent=indent).encode("utf-8")


def _json_bytes_with_duplicate_key(value, key):
    items = [(key, value[key]), (key, value[key])]
    items.extend((name, item) for name, item in value.items() if name != key)
    return (
        "{" + ",".join(
            json.dumps(name, ensure_ascii=False)
            + ":"
            + json.dumps(item, ensure_ascii=False)
            for name, item in items
        ) + "}"
    ).encode("utf-8")


def _sha256(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _replace_source_digest(labels, source_kind, source_index, digest):
    if source_kind == "raw_trace":
        labels["source_binding"]["trace_sha256"] = digest
    elif source_kind == "quality_review":
        labels["source_binding"]["review_sha256"] = digest
    else:
        labels["source_binding"]["evaluation_sha256"][source_index] = digest
    for seed in labels["seeds"]:
        for binding in seed["seed_source_bindings"]:
            if (
                binding["source_kind"] == source_kind
                and binding["source_index"] == source_index
            ):
                binding["source_sha256"] = digest


def _binding_case(*, trace=None, review=None, evaluations=None):
    trace = copy.deepcopy(trace if trace is not None else _binding_trace())
    review = copy.deepcopy(review if review is not None else _binding_review())
    evaluations = copy.deepcopy(
        evaluations
        if evaluations is not None
        else [
            _binding_evaluation("first", "2026-08-03T00:00:00Z"),
            _binding_evaluation("second", "2026-08-03T00:00:01Z"),
        ]
    )
    trace_bytes = _json_bytes(trace)
    review_bytes = _json_bytes(review)
    evaluation_bytes = tuple(_json_bytes(value) for value in evaluations)
    effective_trace = compose_effective_trace(
        trace,
        review=review,
        evaluations=evaluations,
    )
    graph = TraceGraph.from_trace(effective_trace)
    anchors = semantic_anchor_index(graph.case_id, graph)
    occurrences = semantic_occurrence_index(graph.case_id, graph)
    evaluation_refs = [
        "record:{0}".format(record["record_id"])
        for record in effective_trace["records"]
        if record.get("event_type") == "external.evaluation_fact"
    ]
    trace_digest = _sha256(trace_bytes)
    review_digest = _sha256(review_bytes)
    evaluation_digests = tuple(_sha256(value) for value in evaluation_bytes)
    seed_specs = (
        (
            "raw",
            "record:raw_seed",
            [_seed_source_binding("raw_trace", 0, trace_digest)],
        ),
        (
            "review",
            "record:quality_gap_correctness",
            [_seed_source_binding("quality_review", 0, review_digest)],
        ),
        (
            "evaluation",
            evaluation_refs[0],
            [
                _seed_source_binding(
                    "external_evaluation", 0, evaluation_digests[0]
                )
            ],
        ),
    )
    seeds = []
    for name, start_ref, source_bindings in seed_specs:
        fingerprint = "fingerprint-{0}".format(name)
        identity = {
            "node_ref": start_ref,
            "semantic_anchor_id": anchors[start_ref],
            "semantic_occurrence_id": occurrences[start_ref],
        }
        seeds.append(
            {
                "defect_id": "defect-{0}".format(name),
                "start_ref": start_ref,
                "defect_fingerprint": fingerprint,
                "seed_binding_identity": seed_binding_identity_for(
                    start_ref, fingerprint
                ),
                "seed_semantic_anchor_id": anchors[start_ref],
                "seed_semantic_occurrence_id": occurrences[start_ref],
                "seed_source_bindings": source_bindings,
                "defect_origin_kind": "introduced_by_agent",
                "expected_outcome": "confirmed_root",
                "roots": [identity],
                "conditions": [],
                "amplifiers": [],
                "materializations": [],
                "unrelated": [],
                "forbidden_roots": [],
                "allowed_unresolved_outcomes": [],
            }
        )
    labels = {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": graph.case_id,
        "source_binding": {
            "trace_sha256": trace_digest,
            "review_sha256": review_digest,
            "evaluation_sha256": list(evaluation_digests),
            "effective_trace_sha256": _sha256(
                stable_json(effective_trace).encode("utf-8")
            ),
            "subject_revision": "git:binding-revision",
            "revision_provenance_status": "valid",
        },
        "seeds": seeds,
        "case_shared_factors": [],
    }
    return labels, trace_bytes, review_bytes, evaluation_bytes


def _structured_binding_case(*, trace=None, evaluations=None):
    trace = copy.deepcopy(trace if trace is not None else _binding_trace())
    review = _structured_binding_review()
    evaluations = copy.deepcopy(
        evaluations
        if evaluations is not None
        else [_structured_binding_evaluation()]
    )
    trace_bytes = _json_bytes(trace)
    review_bytes = _json_bytes(review)
    evaluation_bytes = tuple(_json_bytes(value) for value in evaluations)
    effective_trace = compose_effective_trace(
        trace,
        review=review,
        evaluations=evaluations,
    )
    graph = TraceGraph.from_trace(effective_trace)
    anchors = semantic_anchor_index(graph.case_id, graph)
    occurrences = semantic_occurrence_index(graph.case_id, graph)
    seed_record = next(
        record
        for record in effective_trace["records"]
        if record.get("event_type") == "case.observed_defect"
    )
    start_ref = "record:{0}".format(seed_record["record_id"])
    fingerprint = "fingerprint-structured"
    review_digest = _sha256(review_bytes)
    evaluation_digests = tuple(_sha256(value) for value in evaluation_bytes)
    source_bindings = [
        _seed_source_binding("quality_review", 0, review_digest)
    ] + [
        _seed_source_binding("external_evaluation", index, digest)
        for index, digest in enumerate(evaluation_digests)
    ]
    identity = {
        "node_ref": start_ref,
        "semantic_anchor_id": anchors[start_ref],
        "semantic_occurrence_id": occurrences[start_ref],
    }
    labels = {
        "schema_version": "recursive-attribution-labels/v5",
        "case_id": graph.case_id,
        "source_binding": {
            "trace_sha256": _sha256(trace_bytes),
            "review_sha256": review_digest,
            "evaluation_sha256": list(evaluation_digests),
            "effective_trace_sha256": _sha256(
                stable_json(effective_trace).encode("utf-8")
            ),
            "subject_revision": "git:binding-revision",
            "revision_provenance_status": "valid",
        },
        "seeds": [
            {
                "defect_id": "defect-structured",
                "start_ref": start_ref,
                "defect_fingerprint": fingerprint,
                "seed_binding_identity": seed_binding_identity_for(
                    start_ref, fingerprint
                ),
                "seed_semantic_anchor_id": anchors[start_ref],
                "seed_semantic_occurrence_id": occurrences[start_ref],
                "seed_source_bindings": source_bindings,
                "defect_origin_kind": "introduced_by_agent",
                "expected_outcome": "confirmed_root",
                "roots": [identity],
                "conditions": [],
                "amplifiers": [],
                "materializations": [],
                "unrelated": [],
                "forbidden_roots": [],
                "allowed_unresolved_outcomes": [],
            }
        ],
        "case_shared_factors": [],
    }
    return labels, trace_bytes, review_bytes, evaluation_bytes, effective_trace


def _authoritative_artifact_case(*, first_byte_length=17):
    trace = _binding_trace()
    trace["artifacts"] = [
        {
            "artifact_id": "proof-a",
            "path": "source/proof-a.txt",
            "hash": "a" * 16,
            "content_hash": "a" * 16,
            "byte_length": first_byte_length,
            "media_type": "text/plain",
        },
        {
            "artifact_id": "proof-b",
            "path": "source/proof-b.json",
            "content_hash": "c" * 16,
            "byte_length": 23,
            "media_type": "application/json",
        },
    ]
    labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case(
        trace=trace
    )
    effective_trace = compose_effective_trace(
        json.loads(trace_bytes),
        review=json.loads(review_bytes),
        evaluations=[json.loads(value) for value in evaluation_bytes],
    )
    normalized = copy.deepcopy(effective_trace)
    normalized_digests = ("b" * 64, "d" * 64)
    for artifact, digest in zip(normalized["artifacts"], normalized_digests):
        artifact["path"] = "artifacts/sha256/{0}/{1}".format(
            digest[:2], digest
        )
        if "hash" in artifact:
            artifact["hash"] = "sha256:{0}".format(digest)
        if "content_hash" in artifact:
            artifact["content_hash"] = "sha256:{0}".format(digest)
    labels["source_binding"]["effective_trace_sha256"] = _sha256(
        stable_json(normalized).encode("utf-8")
    )
    return labels, trace_bytes, review_bytes, evaluation_bytes, normalized


def _projection_report(labels=None):
    labels = labels if labels is not None else _v5_labels()
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


def _projection_judgment(owner, node_ref, factor_role):
    judgment = {
        "candidate_ref": node_ref,
        "seed_binding_identity": owner,
        "factor_role": factor_role,
        "reason": "owned {0}".format(factor_role),
    }
    if factor_role == "necessary_cause":
        judgment["status"] = "confirmed"
    return judgment


def _projection_publication(section, owner, node_ref, occurrence):
    role_by_section = {
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
    return {
        ref_key: node_ref,
        "semantic_occurrence_id": occurrence,
        nested_key: _projection_judgment(
            owner, node_ref, role_by_section[section]
        ),
        "provenance": {"seed_binding_identity": owner},
    }


def _projection_introduction(owner, node_ref, occurrence):
    return {
        "current_node_ref": node_ref,
        "semantic_occurrence_id": occurrence,
        "candidate_introduction": True,
        "owner": {"seed_binding_identity": owner},
        "current_defect_status": "present",
    }


class EvaluationLabelsV5ContractTest(unittest.TestCase):
    def valid_labels(self, labels):
        try:
            return validate_labels(labels)
        except LabelContractError as error:
            self.fail("valid labels were rejected: {0}".format(error))

    def test_valid_v5_labels_are_deep_copied_with_evaluation_digest_order_preserved(self):
        labels = _v5_labels()

        parsed = self.valid_labels(labels)

        self.assertEqual(
            parsed["source_binding"]["evaluation_sha256"],
            [_digest("b"), _digest("c")],
        )
        parsed["seeds"][0]["roots"][0]["node_ref"] = "record:changed"
        self.assertEqual(labels["seeds"][0]["roots"][0]["node_ref"], "record:root-one")

    def test_v5_rejects_extra_or_missing_exact_keys(self):
        labels = _v5_labels()
        labels["extra"] = True
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["source_binding"].pop("subject_revision")
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0].pop("defect_id")
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0].pop("expected_outcome")
        with self.assertRaisesRegex(LabelContractError, "missing=.*expected_outcome"):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["roots"][0]["unexpected"] = "value"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

    def test_v5_rejects_invalid_digests_enums_and_identity_formats(self):
        labels = _v5_labels()
        labels["source_binding"]["trace_sha256"] = _digest("A")
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["source_binding"]["revision_provenance_status"] = "unknown"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["seed_source_bindings"][0]["source_kind"] = "agent"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["defect_origin_kind"] = "unknown"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["expected_outcome"] = "partial"
        with self.assertRaisesRegex(LabelContractError, "expected_outcome"):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["seed_binding_identity"] = "seed_binding:v1:one"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["seed_binding_identity"] = seed_binding_identity_for(
            "record:other-start", labels["seeds"][0]["defect_fingerprint"]
        )
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["roots"][0]["semantic_occurrence_id"] = "occurrence:root"
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

    def test_v5_requires_ordered_duplicate_free_seed_source_bindings(self):
        mutations = (
            [],
            [
                _seed_source_binding("raw_trace", 1, _digest("a")),
            ],
            [
                _seed_source_binding("external_evaluation", -1, _digest("b")),
            ],
            [
                _seed_source_binding("external_evaluation", 0, _digest("B")),
            ],
            [
                _seed_source_binding("quality_review", 0, _digest("a")),
                _seed_source_binding("raw_trace", 0, _digest("a")),
            ],
            [
                _seed_source_binding("raw_trace", 0, _digest("a")),
                _seed_source_binding("raw_trace", 0, _digest("b")),
            ],
        )
        for source_bindings in mutations:
            with self.subTest(source_bindings=source_bindings):
                labels = _v5_labels()
                labels["seeds"][0]["seed_source_bindings"] = source_bindings
                with self.assertRaisesRegex(
                    LabelContractError, "seed_source_bindings"
                ):
                    validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["seed_source_bindings"][0]["extra"] = True
        with self.assertRaisesRegex(LabelContractError, "keys differ"):
            validate_labels(labels)

    def test_v5_rejects_duplicate_seed_and_role_identities(self):
        labels = _v5_labels()
        labels["seeds"][1]["defect_id"] = labels["seeds"][0]["defect_id"]
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][1]["start_ref"] = labels["seeds"][0]["start_ref"]
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][1]["seed_binding_identity"] = labels["seeds"][0]["seed_binding_identity"]
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["conditions"] = [_role_entry("condition"), _role_entry("condition")]
        with self.assertRaises(LabelContractError):
            validate_labels(labels)

    def test_v5_rejects_conflicting_roles_for_a_seed(self):
        labels = _v5_labels()
        labels["seeds"][0]["conditions"] = [_role_entry("shared")]
        labels["seeds"][0]["amplifiers"] = [_role_entry("shared")]

        with self.assertRaises(LabelContractError):
            validate_labels(labels)

    def test_v5_rejects_same_node_ref_with_conflicting_roles_across_occurrences(self):
        labels = _v5_labels()
        condition = _role_entry("node-role-condition")
        amplifier = _role_entry("node-role-amplifier")
        amplifier["node_ref"] = condition["node_ref"]
        labels["seeds"][0]["conditions"] = [condition]
        labels["seeds"][0]["amplifiers"] = [amplifier]

        with self.assertRaisesRegex(LabelContractError, "role conflict.*node_ref"):
            validate_labels(labels)

    def test_v5_enforces_expected_outcome_consistency(self):
        labels = _v5_labels()
        labels["seeds"][0]["roots"] = []
        with self.assertRaisesRegex(LabelContractError, "confirmed_root.*root"):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["expected_outcome"] = "no_defect"
        with self.assertRaisesRegex(LabelContractError, "no_defect.*positive"):
            validate_labels(labels)

        labels = _v5_labels()
        seed = labels["seeds"][0]
        seed["expected_outcome"] = "no_defect"
        seed["roots"] = []
        seed["allowed_unresolved_outcomes"] = ["partial"]
        with self.assertRaisesRegex(LabelContractError, "no_defect.*unresolved"):
            validate_labels(labels)

        labels = _v5_labels()
        labels["seeds"][0]["expected_outcome"] = "unresolved"
        labels["seeds"][0]["allowed_unresolved_outcomes"] = ["partial"]
        with self.assertRaisesRegex(LabelContractError, "unresolved.*positive"):
            validate_labels(labels)

        labels = _v5_labels()
        seed = labels["seeds"][0]
        seed["expected_outcome"] = "unresolved"
        seed["roots"] = []
        with self.assertRaisesRegex(LabelContractError, "unresolved.*allowed"):
            validate_labels(labels)

    def test_v5_accepts_explicit_no_defect_and_unresolved_negative_labels(self):
        labels = _v5_labels()
        no_defect = labels["seeds"][0]
        no_defect["expected_outcome"] = "no_defect"
        no_defect["roots"] = []
        no_defect["unrelated"] = [_role_entry("no-defect-unrelated")]
        no_defect["forbidden_roots"] = [_role_entry("no-defect-forbidden")]
        unresolved = labels["seeds"][1]
        unresolved["expected_outcome"] = "unresolved"
        unresolved["roots"] = []
        unresolved["unrelated"] = [_role_entry("unresolved-unrelated")]
        unresolved["forbidden_roots"] = [_role_entry("unresolved-forbidden")]
        unresolved["allowed_unresolved_outcomes"] = ["partial_root_found"]

        self.assertEqual(self.valid_labels(labels), labels)

    def test_v5_rejects_positive_shared_factors_for_no_defect_seeds(self):
        labels = _v5_labels()
        for seed in labels["seeds"]:
            seed["expected_outcome"] = "no_defect"
            seed["roots"] = []
        labels["case_shared_factors"] = [_shared_factor(labels)]

        with self.assertRaisesRegex(LabelContractError, "no_defect.*positive"):
            validate_labels(labels)

    def test_v5_recomputes_identity_after_each_seed_input_mutation(self):
        mutations = (
            ("start_ref", "record:changed-start"),
            ("defect_fingerprint", "changed-fingerprint"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                labels = _v5_labels()
                labels["seeds"][0][field] = value
                with self.assertRaisesRegex(LabelContractError, "does not match its seed"):
                    validate_labels(labels)

    def test_v5_validates_shared_factor_ownership_and_role_conflicts(self):
        labels = _v5_labels()
        labels["case_shared_factors"] = [_shared_factor(labels)]
        self.assertEqual(
            self.valid_labels(labels)["case_shared_factors"], labels["case_shared_factors"]
        )

        invalid = copy.deepcopy(labels)
        invalid["case_shared_factors"][0]["seed_binding_identities"].reverse()
        with self.assertRaises(LabelContractError):
            validate_labels(invalid)

        invalid = copy.deepcopy(labels)
        invalid["case_shared_factors"][0]["seed_binding_identities"] = [
            labels["seeds"][0]["seed_binding_identity"]
        ]
        with self.assertRaises(LabelContractError):
            validate_labels(invalid)

        invalid = copy.deepcopy(labels)
        invalid["case_shared_factors"][0]["seed_binding_identities"] = [
            labels["seeds"][0]["seed_binding_identity"],
            labels["seeds"][0]["seed_binding_identity"],
        ]
        with self.assertRaises(LabelContractError):
            validate_labels(invalid)

        invalid = copy.deepcopy(labels)
        invalid["case_shared_factors"][0]["seed_binding_identities"] = [
            labels["seeds"][0]["seed_binding_identity"],
            seed_binding_identity_for("record:unknown", "fingerprint-unknown"),
        ]
        with self.assertRaises(LabelContractError):
            validate_labels(invalid)

        invalid = copy.deepcopy(labels)
        invalid["seeds"][0]["conditions"] = [_role_entry("shared")]
        with self.assertRaisesRegex(LabelContractError, "conflicts.*occurrence"):
            validate_labels(invalid)

        invalid = copy.deepcopy(labels)
        direct = _role_entry("direct-node-conflict")
        direct["node_ref"] = invalid["case_shared_factors"][0]["node_ref"]
        invalid["seeds"][0]["conditions"] = [direct]
        with self.assertRaisesRegex(LabelContractError, "conflicts.*node_ref"):
            validate_labels(invalid)

    def test_v5_rejects_malformed_or_duplicate_shared_factors(self):
        labels = _v5_labels()
        factor = _shared_factor(labels)
        factor["unexpected"] = True
        labels["case_shared_factors"] = [factor]
        with self.assertRaisesRegex(LabelContractError, "keys differ"):
            validate_labels(labels)

        labels = _v5_labels()
        factor = _shared_factor(labels, role="root")
        labels["case_shared_factors"] = [factor]
        with self.assertRaisesRegex(LabelContractError, "factor_role"):
            validate_labels(labels)

        labels = _v5_labels()
        factor = _shared_factor(labels)
        labels["case_shared_factors"] = [factor, copy.deepcopy(factor)]
        with self.assertRaisesRegex(LabelContractError, "duplicate shared factor"):
            validate_labels(labels)

    def test_v3_and_v4_labels_remain_supported_without_mutating_input(self):
        v3 = {
            "schema_version": "recursive-attribution-labels/v3",
            "case_id": "legacy",
            "roots": [{"semantic_anchor_id": "semantic_anchor:v2:root", "semantic_occurrence_id": "semantic_occurrence:v1:root"}],
            "conditions": [],
            "amplifiers": [],
            "forbidden_roots": [],
            "allowed_unresolved_outcomes": ["partial_root_found"],
        }
        v4 = {**copy.deepcopy(v3), "schema_version": "recursive-attribution-labels/v4", "materializations": [], "unrelated": []}

        parsed_v3 = validate_labels(v3)
        parsed_v4 = validate_labels(v4)

        self.assertEqual(parsed_v3, v3)
        self.assertEqual(parsed_v4, v4)
        parsed_v3["roots"][0]["semantic_anchor_id"] = "semantic_anchor:v2:changed"
        self.assertEqual(v3["roots"][0]["semantic_anchor_id"], "semantic_anchor:v2:root")

    def test_v3_and_v4_match_legacy_whitespace_and_prefix_only_behavior(self):
        v3 = {
            "schema_version": "recursive-attribution-labels/v3",
            "case_id": "   ",
            "roots": [
                {
                    "node_ref": "record:",
                    "semantic_anchor_id": "semantic_anchor:v2:",
                    "semantic_occurrence_id": "semantic_occurrence:v1:",
                }
            ],
            "conditions": [],
            "amplifiers": [],
            "forbidden_roots": [],
            "allowed_unresolved_outcomes": [],
        }
        v4 = {
            **copy.deepcopy(v3),
            "schema_version": "recursive-attribution-labels/v4",
            "materializations": [],
            "unrelated": [],
        }

        self.assertEqual(legacy_validate_labels(v3), v3)
        self.assertEqual(legacy_validate_labels(v4), v4)
        self.assertEqual(self.valid_labels(v3), v3)
        self.assertEqual(self.valid_labels(v4), v4)


class EvaluationLabelsV5InputBindingTest(unittest.TestCase):
    def bind(
        self,
        labels,
        trace_bytes,
        review_bytes,
        evaluation_bytes,
        *,
        effective_trace=None,
        graph=None,
        bundle=None,
    ):
        kwargs = {
            "effective_trace": effective_trace,
            "graph": graph,
        }
        if bundle is not None:
            kwargs["bundle"] = bundle
        result = bind_label_inputs(
            labels,
            trace_bytes=trace_bytes,
            review_bytes=review_bytes,
            evaluation_bytes=evaluation_bytes,
            **kwargs,
        )
        self.assertIsNotNone(result, "input binding implementation is missing")
        return result

    def test_binds_exact_sources_revision_and_seed_identities_immutably(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        before = copy.deepcopy(labels)

        result = self.bind(labels, trace_bytes, review_bytes, evaluation_bytes)

        self.assertIsInstance(result, LabelInputBinding)
        self.assertEqual(result.case_id, "binding-case")
        self.assertEqual(result.trace_sha256, _sha256(trace_bytes))
        self.assertEqual(result.review_sha256, _sha256(review_bytes))
        self.assertEqual(
            result.evaluation_sha256,
            tuple(_sha256(value) for value in evaluation_bytes),
        )
        self.assertEqual(result.subject_revision, "git:binding-revision")
        self.assertEqual(result.revision_provenance_status, "valid")
        self.assertEqual(
            tuple(
                tuple(
                    (
                        binding.source_kind,
                        binding.source_index,
                        binding.source_sha256,
                    )
                    for binding in seed.seed_source_bindings
                )
                for seed in result.seeds
            ),
            (
                (("raw_trace", 0, _sha256(trace_bytes)),),
                (("quality_review", 0, _sha256(review_bytes)),),
                (("external_evaluation", 0, _sha256(evaluation_bytes[0])),),
            ),
        )
        self.assertEqual(result.seeds[0].start_ref, "record:raw_seed")
        self.assertEqual(labels, before)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.case_id = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.seeds[0].start_ref = "record:changed"

    def test_semantically_equal_byte_different_sources_fail_exact_hash_binding(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        mutations = (
            (
                "trace_sha256",
                _json_bytes(json.loads(trace_bytes), indent=2),
                review_bytes,
                evaluation_bytes,
            ),
            (
                "review_sha256",
                trace_bytes,
                _json_bytes(json.loads(review_bytes), indent=2),
                evaluation_bytes,
            ),
            (
                "evaluation_sha256",
                trace_bytes,
                review_bytes,
                (
                    _json_bytes(json.loads(evaluation_bytes[0]), indent=2),
                    evaluation_bytes[1],
                ),
            ),
        )
        for field, changed_trace, changed_review, changed_evaluations in mutations:
            with self.subTest(field=field):
                with self.assertRaisesRegex(InputBindingError, field):
                    bind_label_inputs(
                        labels,
                        trace_bytes=changed_trace,
                        review_bytes=changed_review,
                        evaluation_bytes=changed_evaluations,
                    )

    def test_rejects_each_source_and_effective_hash_mutation(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        mutations = (
            ("trace_sha256", None),
            ("review_sha256", None),
            ("effective_trace_sha256", None),
            ("evaluation_sha256", 0),
        )
        for field, index in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(labels)
                if index is None:
                    changed["source_binding"][field] = _digest("f")
                else:
                    changed["source_binding"][field][index] = _digest("f")
                with self.assertRaisesRegex(InputBindingError, field):
                    bind_label_inputs(
                        changed,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=evaluation_bytes,
                    )

    def test_rejects_evaluation_membership_and_order_mutations(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        mutations = (
            evaluation_bytes[:1],
            tuple(reversed(evaluation_bytes)),
        )
        for changed_evaluations in mutations:
            with self.subTest(count=len(changed_evaluations)):
                with self.assertRaisesRegex(InputBindingError, "evaluation_sha256"):
                    bind_label_inputs(
                        labels,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=changed_evaluations,
                    )

    def test_rejects_case_revision_and_provenance_mutations(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        changed = copy.deepcopy(labels)
        changed["case_id"] = "changed-case"
        with self.assertRaisesRegex(InputBindingError, "case_id"):
            bind_label_inputs(
                changed,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
            )

        changed = copy.deepcopy(labels)
        changed["source_binding"]["subject_revision"] = "git:changed"
        with self.assertRaisesRegex(InputBindingError, "subject_revision"):
            bind_label_inputs(
                changed,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
            )

        invalid_trace = _binding_trace()
        invalid_trace["manifest"]["subject_revision_provenance"]["bound_at"] = (
            "case_end"
        )
        invalid = _binding_case(trace=invalid_trace)
        with self.assertRaisesRegex(InputBindingError, "revision_provenance_status"):
            bind_label_inputs(
                invalid[0],
                trace_bytes=invalid[1],
                review_bytes=invalid[2],
                evaluation_bytes=invalid[3],
            )

    def test_rejects_every_seed_identity_field_mutation(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        mutations = (
            ("start_ref", "record:changed"),
            ("defect_fingerprint", "changed-fingerprint"),
            ("seed_binding_identity", "seed:" + "0" * 24),
            ("seed_semantic_anchor_id", "semantic_anchor:v2:" + "0" * 24),
            (
                "seed_semantic_occurrence_id",
                "semantic_occurrence:v1:" + "0" * 24,
            ),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                changed = copy.deepcopy(labels)
                changed["seeds"][0][field] = value
                with self.assertRaisesRegex(InputBindingError, field):
                    bind_label_inputs(
                        changed,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=evaluation_bytes,
                    )

    def test_structured_seed_binds_review_and_every_merging_evaluation(self):
        (
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            _,
        ) = _structured_binding_case()

        result = self.bind(labels, trace_bytes, review_bytes, evaluation_bytes)

        bindings = result.seeds[0].seed_source_bindings
        self.assertEqual(
            tuple(
                (item.source_kind, item.source_index, item.source_sha256)
                for item in bindings
            ),
            (
                ("quality_review", 0, _sha256(review_bytes)),
                ("external_evaluation", 0, _sha256(evaluation_bytes[0])),
            ),
        )

    def test_duplicate_identical_evaluations_keep_separate_source_bindings(self):
        evaluation = _structured_binding_evaluation()
        (
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            _,
        ) = _structured_binding_case(evaluations=[evaluation, evaluation])

        result = self.bind(labels, trace_bytes, review_bytes, evaluation_bytes)

        self.assertEqual(
            tuple(
                (item.source_kind, item.source_index, item.source_sha256)
                for item in result.seeds[0].seed_source_bindings
            ),
            (
                ("quality_review", 0, _sha256(review_bytes)),
                ("external_evaluation", 0, _sha256(evaluation_bytes[0])),
                ("external_evaluation", 1, _sha256(evaluation_bytes[1])),
            ),
        )

    def test_duplicate_evaluation_probe_preserves_referenced_review_seed(self):
        review_only = compose_effective_trace(
            _binding_trace(),
            review=_structured_binding_review(),
        )
        review_seed_ref = next(
            "record:{0}".format(record["record_id"])
            for record in review_only["records"]
            if record.get("event_type") == "case.observed_defect"
        )
        evaluation = _structured_binding_evaluation()
        evaluation["evidence_refs"] = [review_seed_ref]
        (
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            _,
        ) = _structured_binding_case(evaluations=[evaluation, evaluation])

        result = self.bind(labels, trace_bytes, review_bytes, evaluation_bytes)

        self.assertEqual(
            tuple(
                (item.source_kind, item.source_index, item.source_sha256)
                for item in result.seeds[0].seed_source_bindings
            ),
            (
                ("quality_review", 0, _sha256(review_bytes)),
                ("external_evaluation", 0, _sha256(evaluation_bytes[0])),
                ("external_evaluation", 1, _sha256(evaluation_bytes[1])),
            ),
        )

    def test_rejects_seed_source_binding_kind_index_digest_order_and_membership_mutations(self):
        (
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            _,
        ) = _structured_binding_case()
        mutations = []

        changed = copy.deepcopy(labels)
        changed["seeds"][0]["seed_source_bindings"][0]["source_kind"] = (
            "raw_trace"
        )
        mutations.append(("kind", changed))

        changed = copy.deepcopy(labels)
        changed["seeds"][0]["seed_source_bindings"][1]["source_index"] = 1
        mutations.append(("index", changed))

        changed = copy.deepcopy(labels)
        changed["seeds"][0]["seed_source_bindings"][1]["source_sha256"] = (
            _digest("f")
        )
        mutations.append(("digest", changed))

        changed = copy.deepcopy(labels)
        changed["seeds"][0]["seed_source_bindings"].reverse()
        mutations.append(("order", changed))

        changed = copy.deepcopy(labels)
        changed["seeds"][0]["seed_source_bindings"].pop()
        mutations.append(("membership", changed))

        for label, changed in mutations:
            with self.subTest(mutation=label):
                with self.assertRaisesRegex(
                    InputBindingError, "seed_source_bindings"
                ):
                    bind_label_inputs(
                        changed,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=evaluation_bytes,
                    )

    def test_probe_composition_errors_fail_closed(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        real_compose = compose_effective_trace
        call_count = 0

        def fail_one_probe(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise ValueError("probe composition failed")
            return real_compose(*args, **kwargs)

        with patch(
            "trace_attribution.input_binding.compose_effective_trace",
            side_effect=fail_one_probe,
        ):
            with self.assertRaisesRegex(
                InputBindingError, "probe composition failed"
            ):
                bind_label_inputs(
                    labels,
                    trace_bytes=trace_bytes,
                    review_bytes=review_bytes,
                    evaluation_bytes=evaluation_bytes,
                )

    def test_alias_start_ref_fails_with_direct_canonical_ref_error(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        seed = labels["seeds"][0]
        seed["start_ref"] = "node:raw_seed"
        seed["seed_binding_identity"] = seed_binding_identity_for(
            seed["start_ref"], seed["defect_fingerprint"]
        )

        with self.assertRaisesRegex(InputBindingError, "canonical record"):
            bind_label_inputs(
                labels,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
            )

    def test_accepts_matching_authoritative_effective_trace_and_graph(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        effective_trace = compose_effective_trace(
            json.loads(trace_bytes),
            review=json.loads(review_bytes),
            evaluations=[json.loads(value) for value in evaluation_bytes],
        )
        graph = TraceGraph.from_trace(effective_trace)

        result = self.bind(
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            effective_trace=effective_trace,
            graph=graph,
        )

        self.assertEqual(result.effective_trace_sha256, _sha256(
            stable_json(effective_trace).encode("utf-8")
        ))

    def test_rebuilds_authoritative_graph_instead_of_consuming_mutated_derived_state(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        effective_trace = compose_effective_trace(
            json.loads(trace_bytes),
            review=json.loads(review_bytes),
            evaluations=[json.loads(value) for value in evaluation_bytes],
        )
        graph = TraceGraph.from_trace(effective_trace)
        graph.case_id = "forged-case"
        graph.nodes.clear()
        graph.aliases.clear()
        graph.aliases["record:raw_seed"] = "record:forged"
        graph._upstream.clear()
        graph._downstream.clear()

        result = self.bind(
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            effective_trace=effective_trace,
            graph=graph,
        )

        self.assertEqual(result.case_id, "binding-case")
        self.assertEqual(result.seeds[0].start_ref, "record:raw_seed")

    def test_authoritative_graph_must_match_its_effective_trace(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        effective_trace = compose_effective_trace(
            json.loads(trace_bytes),
            review=json.loads(review_bytes),
            evaluations=[json.loads(value) for value in evaluation_bytes],
        )
        mismatched_trace = copy.deepcopy(effective_trace)
        mismatched_trace["loader_note"] = "different"
        graph = TraceGraph.from_trace(mismatched_trace)

        with self.assertRaisesRegex(InputBindingError, "graph/raw-trace"):
            bind_label_inputs(
                labels,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
                effective_trace=effective_trace,
                graph=graph,
            )

    def test_rejects_artifact_normalized_effective_trace_without_verified_bundle(self):
        (
            labels,
            trace_bytes,
            review_bytes,
            evaluation_bytes,
            normalized,
        ) = _authoritative_artifact_case()
        graph = TraceGraph.from_trace(
            normalized,
            artifact_root=Path("/verified/bundle"),
        )

        with self.assertRaisesRegex(InputBindingError, "verified.*bundle"):
            self.bind(
                labels,
                trace_bytes,
                review_bytes,
                evaluation_bytes,
                effective_trace=normalized,
                graph=graph,
            )

    def test_accepts_artifact_normalization_from_verified_bundle_snapshot(self):
        from tools.trace_attribution.tests.test_benchmark_bundle import (
            create_manual_bundle,
            open_manual_bundle,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            root = Path(directory) / "bundle"
            create_manual_bundle(root, artifact=True, review=True, evaluations=1)
            bundle = open_manual_bundle(root)
            trace_bytes = bundle.trace_path.read_bytes()
            review_bytes = bundle.review_path.read_bytes()
            evaluation_bytes = tuple(
                path.read_bytes() for path in bundle.evaluation_paths
            )
            effective_trace = json.loads(bundle.effective_trace_path.read_bytes())
            graph = TraceGraph.from_trace(effective_trace)
            start_ref = "record:tool_result"
            anchors = semantic_anchor_index(graph.case_id, graph)
            occurrences = semantic_occurrence_index(graph.case_id, graph)
            fingerprint = "fingerprint-bundle"
            trace_digest = _sha256(trace_bytes)
            labels = {
                "schema_version": "recursive-attribution-labels/v5",
                "case_id": graph.case_id,
                "source_binding": {
                    "trace_sha256": trace_digest,
                    "review_sha256": _sha256(review_bytes),
                    "evaluation_sha256": [
                        _sha256(value) for value in evaluation_bytes
                    ],
                    "effective_trace_sha256": _sha256(
                        stable_json(effective_trace).encode("utf-8")
                    ),
                    "subject_revision": "git:abc123",
                    "revision_provenance_status": "valid",
                },
                "seeds": [
                    {
                        "defect_id": "defect-bundle",
                        "start_ref": start_ref,
                        "defect_fingerprint": fingerprint,
                        "seed_binding_identity": seed_binding_identity_for(
                            start_ref, fingerprint
                        ),
                        "seed_semantic_anchor_id": anchors[start_ref],
                        "seed_semantic_occurrence_id": occurrences[start_ref],
                        "seed_source_bindings": [
                            _seed_source_binding("raw_trace", 0, trace_digest)
                        ],
                        "defect_origin_kind": "introduced_by_agent",
                        "expected_outcome": "confirmed_root",
                        "roots": [
                            {
                                "node_ref": start_ref,
                                "semantic_anchor_id": anchors[start_ref],
                                "semantic_occurrence_id": occurrences[start_ref],
                            }
                        ],
                        "conditions": [],
                        "amplifiers": [],
                        "materializations": [],
                        "unrelated": [],
                        "forbidden_roots": [],
                        "allowed_unresolved_outcomes": [],
                    }
                ],
                "case_shared_factors": [],
            }

            result = self.bind(
                labels,
                trace_bytes,
                review_bytes,
                evaluation_bytes,
                bundle=bundle,
            )

        self.assertEqual(result.case_id, "bundle-case")
        self.assertEqual(result.effective_trace_sha256, labels["source_binding"]["effective_trace_sha256"])

    def test_rejects_float_and_boolean_artifact_byte_lengths(self):
        for invalid in (17.0, True):
            with self.subTest(byte_length=invalid):
                (
                    labels,
                    trace_bytes,
                    review_bytes,
                    evaluation_bytes,
                    normalized,
                ) = _authoritative_artifact_case(
                    first_byte_length=1 if invalid is True else 17
                )
                normalized["artifacts"][0]["byte_length"] = invalid
                labels["source_binding"]["effective_trace_sha256"] = _sha256(
                    stable_json(normalized).encode("utf-8")
                )
                graph = TraceGraph.from_trace(
                    normalized,
                    artifact_root=Path("/caller-claimed/bundle"),
                )

                with self.assertRaisesRegex(InputBindingError, "byte_length"):
                    bind_label_inputs(
                        labels,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=evaluation_bytes,
                        effective_trace=normalized,
                        graph=graph,
                    )

    def test_factual_sources_reject_duplicate_keys_and_nonfinite_numbers(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        trace = json.loads(trace_bytes)
        review = json.loads(review_bytes)
        evaluation = json.loads(evaluation_bytes[0])
        malformed_cases = []
        malformed_cases.append(
            (
                "raw_trace",
                0,
                _json_bytes_with_duplicate_key(trace, "manifest"),
            )
        )
        malformed_cases.append(
            (
                "quality_review",
                0,
                _json_bytes_with_duplicate_key(review, "case_id"),
            )
        )
        malformed_cases.append(
            (
                "external_evaluation",
                0,
                _json_bytes_with_duplicate_key(evaluation, "source"),
            )
        )
        for source_kind, source_index, malformed in malformed_cases:
            with self.subTest(source=source_kind, malformed="duplicate_key"):
                changed = copy.deepcopy(labels)
                _replace_source_digest(
                    changed,
                    source_kind,
                    source_index,
                    _sha256(malformed),
                )
                sources = [trace_bytes, review_bytes, *evaluation_bytes]
                position = {
                    "raw_trace": 0,
                    "quality_review": 1,
                    "external_evaluation": 2 + source_index,
                }[source_kind]
                sources[position] = malformed
                with self.assertRaisesRegex(InputBindingError, "duplicate key"):
                    bind_label_inputs(
                        changed,
                        trace_bytes=sources[0],
                        review_bytes=sources[1],
                        evaluation_bytes=tuple(sources[2:]),
                    )

        for source_kind, source_index, position in (
            ("raw_trace", 0, 0),
            ("quality_review", 0, 1),
            ("external_evaluation", 0, 2),
        ):
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(source=source_kind, constant=constant):
                    changed = copy.deepcopy(labels)
                    malformed = (
                        '{"invalid_number":' + constant + "}"
                    ).encode("utf-8")
                    _replace_source_digest(
                        changed,
                        source_kind,
                        source_index,
                        _sha256(malformed),
                    )
                    sources = [trace_bytes, review_bytes, *evaluation_bytes]
                    sources[position] = malformed
                    with self.assertRaisesRegex(InputBindingError, "non-finite"):
                        bind_label_inputs(
                            changed,
                            trace_bytes=sources[0],
                            review_bytes=sources[1],
                            evaluation_bytes=tuple(sources[2:]),
                        )

    def test_duplicate_evaluation_resolves_unique_evidence_alias(self):
        review_only = compose_effective_trace(
            _binding_trace(),
            review=_structured_binding_review(),
        )
        review_seed_id = next(
            record["record_id"]
            for record in review_only["records"]
            if record.get("event_type") == "case.observed_defect"
        )
        evaluation = _structured_binding_evaluation()
        evaluation["evidence_refs"] = ["node:{0}".format(review_seed_id)]
        case = _structured_binding_case(evaluations=[evaluation, evaluation])

        result = self.bind(case[0], case[1], case[2], case[3])

        self.assertEqual(
            [item.source_index for item in result.seeds[0].seed_source_bindings],
            [0, 0, 1],
        )

    def test_evidence_aliases_fail_closed_when_unresolved_or_ambiguous(self):
        for alias in ("node:missing", "verification:ambiguous"):
            with self.subTest(alias=alias):
                trace = _binding_trace()
                if alias.startswith("verification:"):
                    trace["records"].extend(
                        {
                            "record_id": "verification-{0}".format(index),
                            "component": "verification",
                            "event_type": "verification",
                            "data": {"verification_id": "ambiguous"},
                        }
                        for index in range(2)
                    )
                evaluation = _structured_binding_evaluation()
                evaluation["evidence_refs"] = [alias]
                case = _structured_binding_case(
                    trace=trace,
                    evaluations=[evaluation, evaluation],
                )

                with self.assertRaisesRegex(
                    InputBindingError, "evidence alias.*(unresolved|ambiguous)"
                ):
                    bind_label_inputs(
                        case[0],
                        trace_bytes=case[1],
                        review_bytes=case[2],
                        evaluation_bytes=case[3],
                    )

    def test_rejects_nonstructural_authoritative_artifact_normalization(self):
        mutations = (
            (
                "artifact_id",
                lambda artifacts: artifacts[0].__setitem__(
                    "artifact_id", "changed"
                ),
            ),
            (
                "added",
                lambda artifacts: artifacts.append(copy.deepcopy(artifacts[0])),
            ),
            ("removed", lambda artifacts: artifacts.pop()),
            ("reordered", lambda artifacts: artifacts.reverse()),
            (
                "extra key",
                lambda artifacts: artifacts[0].__setitem__("extra", True),
            ),
            ("missing key", lambda artifacts: artifacts[0].pop("hash")),
            (
                "byte_length",
                lambda artifacts: artifacts[0].__setitem__("byte_length", 18),
            ),
            (
                "media_type",
                lambda artifacts: artifacts[0].__setitem__(
                    "media_type", "application/octet-stream"
                ),
            ),
            (
                "malformed member path",
                lambda artifacts: artifacts[0].__setitem__(
                    "path", "artifacts/sha256/{0}".format("b" * 64)
                ),
            ),
            (
                "truncated normalized hash",
                lambda artifacts: artifacts[0].__setitem__("hash", "b" * 16),
            ),
            (
                "path hash mismatch",
                lambda artifacts: artifacts[0].__setitem__(
                    "content_hash", "sha256:{0}".format("e" * 64)
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(mutation=label):
                (
                    labels,
                    trace_bytes,
                    review_bytes,
                    evaluation_bytes,
                    normalized,
                ) = _authoritative_artifact_case()
                mutate(normalized["artifacts"])
                labels["source_binding"]["effective_trace_sha256"] = _sha256(
                    stable_json(normalized).encode("utf-8")
                )
                graph = TraceGraph.from_trace(
                    normalized,
                    artifact_root=Path("/verified/bundle"),
                )

                with self.assertRaisesRegex(
                    InputBindingError, "artifact normalization"
                ):
                    bind_label_inputs(
                        labels,
                        trace_bytes=trace_bytes,
                        review_bytes=review_bytes,
                        evaluation_bytes=evaluation_bytes,
                        effective_trace=normalized,
                        graph=graph,
                    )

    def test_authoritative_effective_trace_rejects_arbitrary_record_mutation(self):
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case()
        effective_trace = compose_effective_trace(
            json.loads(trace_bytes),
            review=json.loads(review_bytes),
            evaluations=[json.loads(value) for value in evaluation_bytes],
        )
        effective_trace["records"][0]["data"]["call_id"] = "mutated"
        labels["source_binding"]["effective_trace_sha256"] = _sha256(
            stable_json(effective_trace).encode("utf-8")
        )
        graph = TraceGraph.from_trace(
            effective_trace,
            artifact_root=Path("/verified/bundle"),
        )

        with self.assertRaisesRegex(InputBindingError, "factual"):
            bind_label_inputs(
                labels,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
                effective_trace=effective_trace,
                graph=graph,
            )

    def test_rejects_preexisting_source_id_collision_without_partial_result(self):
        trace = _binding_trace()
        trace["records"].append(
            {
                "record_id": "quality_gap_correctness",
                "component": "preexisting",
                "event_type": "raw.collision",
                "data": {"origin": "raw_trace"},
            }
        )
        labels, trace_bytes, review_bytes, evaluation_bytes = _binding_case(
            trace=trace
        )
        result = None

        with self.assertRaisesRegex(InputBindingError, "record ID collision"):
            result = bind_label_inputs(
                labels,
                trace_bytes=trace_bytes,
                review_bytes=review_bytes,
                evaluation_bytes=evaluation_bytes,
            )

        self.assertIsNone(result)


class EvaluationLabelsV5SeedProjectionTest(unittest.TestCase):
    def project(self, report, labels=None):
        result = project_seed_owned_report(
            report,
            labels if labels is not None else _v5_labels(),
        )
        self.assertIsNotNone(result, "seed projection implementation is missing")
        return result

    def test_swapped_roots_remain_distinct_per_seed_despite_aggregate_match(self):
        labels = _v5_labels()
        first, second = labels["seeds"]
        report = _projection_report(labels)
        report["confirmed_roots"] = [
            _projection_publication(
                "confirmed_roots",
                second["seed_binding_identity"],
                first["roots"][0]["node_ref"],
                first["roots"][0]["semantic_occurrence_id"],
            ),
            _projection_publication(
                "confirmed_roots",
                first["seed_binding_identity"],
                second["roots"][0]["node_ref"],
                second["roots"][0]["semantic_occurrence_id"],
            ),
        ]

        projection = self.project(report, labels)

        roots_by_seed = {
            seed.seed_binding_identity: {
                item.semantic_occurrence_id
                for item in seed.records
                if item.role == "root"
            }
            for seed in projection.seeds
        }
        expected_aggregate = {
            seed["roots"][0]["semantic_occurrence_id"]
            for seed in labels["seeds"]
        }
        self.assertEqual(
            {
                item.semantic_occurrence_id
                for item in projection.records
                if item.role == "root"
            },
            expected_aggregate,
        )
        self.assertEqual(
            roots_by_seed[first["seed_binding_identity"]],
            {second["roots"][0]["semantic_occurrence_id"]},
        )
        self.assertEqual(
            roots_by_seed[second["seed_binding_identity"]],
            {first["roots"][0]["semantic_occurrence_id"]},
        )

    def test_projects_every_authoritative_ownership_source_immutably(self):
        labels = _v5_labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        report = _projection_report(labels)
        sections = (
            ("confirmed_roots", "root"),
            ("contributing_conditions", "contributing_condition"),
            ("amplifying_factors", "amplifying_factor"),
            ("downstream_materializations", "downstream_materialization"),
            ("rejected_candidates", "unrelated"),
        )
        for index, (section, _) in enumerate(sections):
            node_ref = "record:owned-{0}".format(index)
            occurrence = "semantic_occurrence:v1:owned-{0}".format(index)
            report[section] = [
                _projection_publication(section, owner, node_ref, occurrence)
            ]
        intro_ref = "record:owned-introduction"
        intro_occurrence = "semantic_occurrence:v1:owned-introduction"
        report["step_judgments"] = [
            _projection_introduction(owner, intro_ref, intro_occurrence)
        ]
        report["introduction_candidates"] = [
            {
                "ref": intro_ref,
                "semantic_occurrence_id": intro_occurrence,
            }
        ]

        projection = self.project(report, labels)

        self.assertIsInstance(projection, SeedOwnedReportProjection)
        self.assertEqual(
            {item.role for item in projection.records},
            {role for _, role in sections} | {"introduction_candidate"},
        )
        self.assertEqual(
            {item.seed_binding_identity for item in projection.records},
            {owner},
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            projection.records[0].role = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            projection.seeds[0].seed_binding_identity = "changed"

    def test_each_ownership_source_rejects_missing_and_unknown_owner(self):
        labels = _v5_labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        cases = (
            ("confirmed_roots", "confirmation"),
            ("contributing_conditions", "confirmation"),
            ("amplifying_factors", "confirmation"),
            ("downstream_materializations", "role_judgment"),
            ("rejected_candidates", "confirmation"),
        )
        for section, nested_key in cases:
            for mutation in ("missing", "unknown"):
                with self.subTest(section=section, mutation=mutation):
                    report = _projection_report(labels)
                    publication = _projection_publication(
                        section,
                        owner,
                        "record:{0}".format(section),
                        "semantic_occurrence:v1:{0}".format(section),
                    )
                    if mutation == "missing":
                        publication[nested_key].pop("seed_binding_identity")
                        publication["provenance"].pop("seed_binding_identity")
                    else:
                        publication[nested_key]["seed_binding_identity"] = (
                            "seed:" + "f" * 24
                        )
                        publication["provenance"]["seed_binding_identity"] = (
                            "seed:" + "f" * 24
                        )
                    report[section] = [publication]
                    with self.assertRaisesRegex(
                        SeedProjectionError, "owner|ownership|seed"
                    ):
                        project_seed_owned_report(report, labels)

        for mutation in ("missing", "unknown"):
            with self.subTest(section="introduction", mutation=mutation):
                report = _projection_report(labels)
                step = _projection_introduction(
                    owner,
                    "record:introduction",
                    "semantic_occurrence:v1:introduction",
                )
                if mutation == "missing":
                    step.pop("owner")
                else:
                    step["owner"]["seed_binding_identity"] = (
                        "seed:" + "f" * 24
                    )
                report["step_judgments"] = [step]
                report["introduction_candidates"] = [
                    {
                        "ref": step["current_node_ref"],
                        "semantic_occurrence_id": step[
                            "semantic_occurrence_id"
                        ],
                    }
                ]
                with self.assertRaisesRegex(
                    SeedProjectionError, "owner|ownership|seed"
                ):
                    project_seed_owned_report(report, labels)

    def test_rejects_contradictory_and_multiply_asserted_ownership(self):
        labels = _v5_labels()
        first_owner = labels["seeds"][0]["seed_binding_identity"]
        second_owner = labels["seeds"][1]["seed_binding_identity"]

        contradictory = _projection_report(labels)
        publication = _projection_publication(
            "confirmed_roots",
            first_owner,
            "record:contradictory",
            "semantic_occurrence:v1:contradictory",
        )
        publication["provenance"]["seed_binding_identity"] = second_owner
        contradictory["confirmed_roots"] = [publication]
        with self.assertRaisesRegex(
            SeedProjectionError, "conflicting|multiple|ownership"
        ):
            project_seed_owned_report(contradictory, labels)

        multiply_asserted = _projection_report(labels)
        publication = _projection_publication(
            "confirmed_roots",
            first_owner,
            "record:multiply-owned",
            "semantic_occurrence:v1:multiply-owned",
        )
        publication["confirmation"]["owner"] = {
            "seed_binding_identity": second_owner
        }
        multiply_asserted["confirmed_roots"] = [publication]
        with self.assertRaisesRegex(
            SeedProjectionError, "conflicting|multiple|ownership"
        ):
            project_seed_owned_report(multiply_asserted, labels)

    def test_same_occurrence_can_be_projected_once_for_each_seed(self):
        labels = _v5_labels()
        report = _projection_report(labels)
        occurrence = "semantic_occurrence:v1:shared"
        report["contributing_conditions"] = [
            _projection_publication(
                "contributing_conditions",
                seed["seed_binding_identity"],
                "record:shared",
                occurrence,
            )
            for seed in labels["seeds"]
        ]

        projection = self.project(report, labels)

        shared = [
            item
            for item in projection.records
            if item.semantic_occurrence_id == occurrence
        ]
        self.assertEqual(len(shared), 2)
        self.assertEqual(
            {item.seed_binding_identity for item in shared},
            {
                seed["seed_binding_identity"] for seed in labels["seeds"]
            },
        )

    def test_report_item_and_seed_order_do_not_change_projection(self):
        labels = _v5_labels()
        first_owner = labels["seeds"][0]["seed_binding_identity"]
        second_owner = labels["seeds"][1]["seed_binding_identity"]
        report = _projection_report(labels)
        report["confirmed_roots"] = [
            _projection_publication(
                "confirmed_roots",
                first_owner,
                "record:root-a",
                "semantic_occurrence:v1:root-a",
            ),
            _projection_publication(
                "confirmed_roots",
                second_owner,
                "record:root-b",
                "semantic_occurrence:v1:root-b",
            ),
        ]
        report["amplifying_factors"] = [
            _projection_publication(
                "amplifying_factors",
                first_owner,
                "record:amp-a",
                "semantic_occurrence:v1:amp-a",
            ),
            _projection_publication(
                "amplifying_factors",
                second_owner,
                "record:amp-b",
                "semantic_occurrence:v1:amp-b",
            ),
        ]
        permuted = copy.deepcopy(report)
        for value in permuted.values():
            if isinstance(value, list):
                value.reverse()

        self.assertEqual(
            self.project(report, labels),
            self.project(permuted, labels),
        )

    def test_duplicate_projection_collapses_by_normalized_semantic_fact(self):
        labels = _v5_labels()
        owner = labels["seeds"][0]["seed_binding_identity"]
        publication = _projection_publication(
            "confirmed_roots",
            owner,
            "record:duplicate",
            "semantic_occurrence:v1:duplicate",
        )
        equivalent = _projection_report(labels)
        equivalent["confirmed_roots"] = [
            publication,
            copy.deepcopy(publication),
        ]

        projection = self.project(equivalent, labels)
        self.assertEqual(
            len([item for item in projection.records if item.role == "root"]),
            1,
        )

        distinct_provenance = copy.deepcopy(equivalent)
        distinct_provenance["confirmed_roots"][1]["confirmation"]["reason"] = (
            "different publication"
        )
        projection = project_seed_owned_report(distinct_provenance, labels)
        self.assertEqual(
            len([item for item in projection.records if item.role == "root"]),
            1,
        )

    def test_requires_exact_report_seed_coverage(self):
        labels = _v5_labels()
        missing = _projection_report(labels)
        missing["seed_results"].pop()
        with self.assertRaisesRegex(SeedProjectionError, "report seed"):
            project_seed_owned_report(missing, labels)

        extra = _projection_report(labels)
        extra_ref = "record:extra"
        extra_fingerprint = "extra-fingerprint"
        extra["seed_results"].append(
            {
                "start_ref": extra_ref,
                "defect_fingerprint": extra_fingerprint,
                "seed_binding_identity": seed_binding_identity_for(
                    extra_ref, extra_fingerprint
                ),
            }
        )
        with self.assertRaisesRegex(SeedProjectionError, "report seed"):
            project_seed_owned_report(extra, labels)

    def test_top_level_introduction_candidate_never_supplies_ownership(self):
        labels = _v5_labels()
        report = _projection_report(labels)
        report["introduction_candidates"] = [
            {
                "ref": "record:aggregate-introduction",
                "semantic_occurrence_id": (
                    "semantic_occurrence:v1:aggregate-introduction"
                ),
                "seed_binding_identity": labels["seeds"][0][
                    "seed_binding_identity"
                ],
            }
        ]

        projection = project_seed_owned_report(report, labels)
        self.assertFalse(
            any(item.role == "introduction_candidate" for item in projection.records)
        )


if __name__ == "__main__":
    unittest.main()
