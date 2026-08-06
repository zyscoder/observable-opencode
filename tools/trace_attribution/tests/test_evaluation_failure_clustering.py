from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.cli import load_graph
from trace_attribution.evaluation_facts import (
    FailureSignature,
    ObservedDefectSeed,
    inject_external_evaluation_facts,
    merge_observed_defect_seed_record,
    observed_defect_seed_source_binding,
    project_observed_defect_seeds,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.quality_review import inject_quality_gap_records


FORBIDDEN_LABEL_KEYS = {
    "attribution_conclusion",
    "expected_root_ref",
    "expected_root_refs",
    "ground_truth_root_cause",
    "human_root_label",
    "root_cause",
    "root_ref",
}


def base_trace():
    return {
        "manifest": {
            "case_id": "pydantic-layered-evaluation",
            "run_id": "run-20260731",
        },
        "records": [
            {
                "record_id": "final_claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "text": "All requested deprecated-field behavior is implemented.",
                    "is_final_for_case": True,
                },
            }
        ],
        "dataflow_edges": [],
    }


def revision_bound_trace():
    trace = base_trace()
    trace["manifest"].update(
        {
            "subject_revision": "git:task2",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": trace["manifest"]["case_id"],
                "run_id": trace["manifest"]["run_id"],
            },
        }
    )
    return trace


def active_revision_trace(
    *,
    case_id="pydantic-layered-evaluation",
    run_id="run-20260731",
    subject_revision="git:task2",
):
    trace = revision_bound_trace()
    trace["manifest"].update(
        {
            "case_id": case_id,
            "run_id": run_id,
            "subject_revision": subject_revision,
        }
    )
    trace["manifest"]["subject_revision_provenance"].update(
        {"case_id": case_id, "run_id": run_id}
    )
    trace["records"][0]["data"].update(
        {
            "subject_revision": subject_revision,
            "revision_status": "matched",
            "revision_provenance_status": "valid",
        }
    )
    return trace


def external_payload(*, test_id: str, signature: dict):
    return {
        "source": "featurebench",
        "scope": test_id,
        "subject_revision": "git:task2",
        "assertion": signature.get(
            "assertion_contract",
            "The structured external evaluation contract must hold.",
        ),
        "observation": signature.get(
            "message",
            "The structured external evaluator observed a failure.",
        ),
        "status": "failed",
        "observed_at": "2026-07-31T12:00:00Z",
        "evidence_refs": ["record:final_claim"],
        "provenance": {
            "method": "featurebench_grader",
            "version": "1.0",
            "failure_signature": {
                "schema_version": "evaluation-failure-signature/v1",
                "test_id": test_id,
                **signature,
            },
        },
    }


def structured_signature(**overrides):
    signature = {
        "exception_family": "AttributeError",
        "first_business_frame": {
            "file": "pydantic/fields.py",
            "symbol": "ModelPrivateAttr.__set_name__",
            "line": 149,
        },
        "assertion_contract": (
            "deprecated field descriptors must bind their owner and field name"
        ),
        "relevant_symbol": "ModelPrivateAttr.__set_name__",
        "subsystem": "pydantic.descriptor_lifecycle",
        "evaluation_run_id": "featurebench-run-a",
        "evaluation_layer_id": "baseline",
    }
    signature.update(overrides)
    return signature


def descriptor_failure(index: int):
    return {
        "component": "benchmark_evaluation",
        "failure_type": "AttributeError",
        "exception_family": "AttributeError",
        "first_business_frame": {
            "file": "pydantic/fields.py",
            "symbol": "ModelPrivateAttr.__set_name__",
            "line": 149,
        },
        "assertion_contract": (
            "deprecated field descriptors must bind their owner and field name"
        ),
        "relevant_symbol": "ModelPrivateAttr.__set_name__",
        "subsystem": "pydantic.descriptor_lifecycle",
        "test_id": f"test_deprecated_field_descriptor_{index:02d}",
        "evaluation_run_id": "pydantic-run-primary",
        "evaluation_layer_id": "baseline",
        "record_refs": ["record:final_claim"],
        # These fields deliberately model evaluation-only labels. They must
        # never influence signatures, identities, records, or Judge seeds.
        "ground_truth_root_cause": {"root_ref": f"record:human_{index}"},
        "expected_root_refs": [f"record:human_{index}"],
        "attribution_conclusion": "The human reviewer selected a root.",
    }


def layered_annotation_failure():
    return {
        "component": "benchmark_evaluation",
        "failure_type": "AttributeError",
        "exception_family": "PydanticUndefinedAnnotation",
        "first_business_frame": {
            "file": "pydantic/main.py",
            "symbol": "BaseModel.model_rebuild",
            "line": 617,
        },
        "assertion_contract": (
            "a descriptor introduced in a layered run must rebuild annotations"
        ),
        "relevant_symbol": "BaseModel.model_rebuild",
        "subsystem": "pydantic.model_rebuild",
        "test_id": "test_layered_model_rebuild",
        "evaluation_run_id": "pydantic-run-layered",
        "evaluation_layer_id": "layer-2-after-descriptor-neutralization",
        "prerequisite_neutralization": {
            "signature": "descriptor-lifecycle",
            "method": "reference patch",
            "status": "neutralized",
        },
        "record_refs": ["record:final_claim"],
        "human_root_label": "model rebuild omission",
        "expected_root_ref": "record:human_model_rebuild_root",
    }


def json_schema_failure():
    return {
        "component": "benchmark_evaluation",
        "failure_type": "AttributeError",
        "exception_family": "AttributeError",
        "first_business_frame": {
            "file": "pydantic/json_schema.py",
            "symbol": "GenerateJsonSchema.model_fields_schema",
            "line": 1412,
        },
        "assertion_contract": (
            "deprecated fields must remain representable in generated JSON Schema"
        ),
        "relevant_symbol": "GenerateJsonSchema.model_fields_schema",
        "subsystem": "pydantic.json_schema",
        "test_id": "test_deprecated_field_json_schema",
        "evaluation_run_id": "pydantic-run-primary",
        "evaluation_layer_id": "baseline",
        "record_refs": ["record:final_claim"],
        "root_cause": {"root_ref": "record:human_schema_root"},
    }


def failures():
    return [
        *(descriptor_failure(index) for index in range(12)),
        layered_annotation_failure(),
        json_schema_failure(),
    ]


def structured_review(*, case_id, observed_defects):
    return {
        "case_id": case_id,
        "observed_defects": observed_defects,
    }


def all_mapping_keys(value):
    keys = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(all_mapping_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(all_mapping_keys(child))
    return keys


def source_state_seed_record(member: str, state_mask: int):
    source_ref = "record:shared_source"
    semantic_ref = f"record:semantic_{member}_{state_mask}"
    signature_seed = project_observed_defect_seeds([descriptor_failure(1)])[0]
    binding = observed_defect_seed_source_binding(
        external_refs=[source_ref] if state_mask & 4 else [],
        declared_review_refs=[source_ref] if state_mask & 1 else [],
        declared_external_refs=[source_ref] if state_mask & 6 else [],
        rejected_external_refs=[source_ref] if state_mask & 2 else [],
        unresolved_review_refs=[source_ref] if state_mask & 1 else [],
    )
    data = signature_seed.to_dict()
    data.update(
        {
            "case_id": "pydantic-layered-evaluation",
            "subject_revision": "git:task2",
            "revision_status": "matched",
            "revision_provenance_status": "valid",
            "evaluation_fact_ids": [f"fact-{member}-{state_mask}"],
            "test_ids": [f"test-{member}-{state_mask}"],
            "evaluation_run_ids": [f"run-{member}-{state_mask}"],
            "evaluation_layer_ids": ["property-test"],
            "prerequisite_neutralizations": [],
            "evidence_refs": [semantic_ref],
            "cluster_size": 1,
            "source_binding": binding,
            "source_ref_count_total": binding["declared_total"],
            "source_ref_count_included": binding["included"],
            "source_refs_truncated": binding["truncated"],
        }
    )
    return {
        "record_id": f"observed_defect_seed_{signature_seed.seed_id}",
        "event_type": "case.observed_defect",
        "component": "evaluation",
        "status": "warning",
        "source_refs": list(binding["all_refs"]),
        "data": data,
    }


def merge_seed_sequence(records):
    merged = json.loads(json.dumps(records[0]))
    for record in records[1:]:
        merge_observed_defect_seed_record(merged, record)
    return merged


class EvaluationFailureClusteringTest(unittest.TestCase):
    def test_review_only_real_load_graph_requires_trusted_revision_binding(self):
        review_failure = descriptor_failure(0)
        review_failure["record_refs"] = ["record:final_claim"]
        review = structured_review(
            case_id="pydantic-layered-evaluation",
            observed_defects=[review_failure],
        )

        def load(trace, candidate_review=review):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trace_path = root / "trace.json"
                review_path = root / "review.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                review_path.write_text(
                    json.dumps(candidate_review), encoding="utf-8"
                )
                return load_graph(trace_path, review_path=review_path)

        trusted_graph = load(active_revision_trace())
        trusted_seed_refs = [
            ref
            for ref, node in trusted_graph.nodes.items()
            if node.event_type == "case.observed_defect"
            and node.data.get("failure_signature")
        ]
        self.assertEqual(len(trusted_seed_refs), 1)
        trusted_seed = trusted_graph.nodes[trusted_seed_refs[0]]
        self.assertIn("subject_revision", trusted_seed.data)
        self.assertEqual(trusted_seed.data["subject_revision"], "git:task2")
        self.assertEqual(trusted_seed.data["revision_status"], "matched")
        self.assertEqual(
            trusted_seed.data["revision_provenance_status"], "valid"
        )
        self.assertTrue(
            trusted_graph.active_revision_start_eligible(trusted_seed.ref)
        )
        self.assertEqual(trusted_graph.default_start_refs(), [trusted_seed.ref])

        untrusted_graph = load(base_trace())
        untrusted_seed_refs = [
            ref
            for ref, node in untrusted_graph.nodes.items()
            if node.event_type == "case.observed_defect"
            and node.data.get("failure_signature")
        ]
        self.assertEqual(len(untrusted_seed_refs), 1)
        self.assertFalse(
            untrusted_graph.active_revision_start_eligible(
                untrusted_seed_refs[0]
            )
        )
        self.assertNotIn(
            untrusted_seed_refs[0],
            untrusted_graph.default_start_refs(),
        )

        mismatched_review = {
            **review,
            "case_id": "different-case",
        }
        with self.assertRaisesRegex(ValueError, "case_id"):
            load(active_revision_trace(), mismatched_review)

    def test_external_seed_requires_matched_provenanced_subject_revision(self):
        payload = external_payload(
            test_id="test_revision_eligibility",
            signature=structured_signature(),
        )
        mismatched_payload = json.loads(json.dumps(payload))
        mismatched_payload["subject_revision"] = "git:different-revision"
        cases = (
            ("matched", "matched", revision_bound_trace(), payload, True),
            (
                "mismatched",
                "mismatched",
                revision_bound_trace(),
                mismatched_payload,
                False,
            ),
            ("unknown", "missing", base_trace(), payload, False),
        )

        for state, expected_status, trace, candidate, expect_seed in cases:
            with self.subTest(revision_state=state):
                enriched = inject_external_evaluation_facts(trace, [candidate])
                facts = [
                    record
                    for record in enriched["records"]
                    if record.get("event_type") == "external.evaluation_fact"
                ]
                seeds = [
                    record
                    for record in enriched["records"]
                    if record.get("event_type") == "case.observed_defect"
                    and record.get("data", {}).get("failure_signature")
                ]

                self.assertEqual(len(facts), 1)
                self.assertEqual(facts[0]["data"]["revision_status"], expected_status)
                self.assertEqual(len(seeds), int(expect_seed))
                self.assertEqual(
                    facts[0]["data"]["subject_revision"],
                    candidate["subject_revision"],
                )
                if expect_seed:
                    seed = seeds[0]
                    self.assertEqual(
                        seed["data"]["subject_revision"],
                        candidate["subject_revision"],
                    )
                    self.assertEqual(
                        seed["source_refs"],
                        [f"record:{facts[0]['record_id']}"],
                    )
                    graph = TraceGraph.from_trace(enriched)
                    seed_ref = f"record:{seed['record_id']}"
                    self.assertEqual(graph.default_start_refs(), [seed_ref])
                    self.assertEqual(
                        graph.upstream_refs(seed_ref),
                        seed["source_refs"],
                    )
                else:
                    self.assertIsNone(
                        facts[0]["data"]["observed_defect_seed_id"]
                    )

    def test_unprovenanced_trace_revision_cannot_be_promoted_to_seed_revision(self):
        trace = revision_bound_trace()
        trace["manifest"].pop("subject_revision_provenance")
        payload = external_payload(
            test_id="test_unprovenanced_revision",
            signature=structured_signature(),
        )

        enriched = inject_external_evaluation_facts(trace, [payload])

        facts = [
            record
            for record in enriched["records"]
            if record.get("event_type") == "external.evaluation_fact"
        ]
        seeds = [
            record
            for record in enriched["records"]
            if record.get("event_type") == "case.observed_defect"
            and record.get("data", {}).get("failure_signature")
        ]
        self.assertEqual(facts[0]["data"]["revision_status"], "unprovenanced")
        self.assertEqual(seeds, [])
        self.assertIsNone(facts[0]["data"]["observed_defect_seed_id"])

    def test_real_load_graph_projects_structured_external_failures_to_seed_starts(self):
        descriptor_one = external_payload(
            test_id="test_descriptor_one",
            signature=structured_signature(),
        )
        descriptor_two = external_payload(
            test_id="test_descriptor_two",
            signature=structured_signature(),
        )
        schema_failure = external_payload(
            test_id="test_json_schema",
            signature=structured_signature(
                first_business_frame={
                    "file": "pydantic/json_schema.py",
                    "symbol": "GenerateJsonSchema.model_fields_schema",
                    "line": 1412,
                },
                assertion_contract=(
                    "deprecated fields must remain representable in JSON Schema"
                ),
                relevant_symbol="GenerateJsonSchema.model_fields_schema",
                subsystem="pydantic.json_schema",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_path = root / "trace.json"
            trace_path.write_text(json.dumps(revision_bound_trace()), encoding="utf-8")
            evaluation_paths = []
            for index, payload in enumerate(
                (descriptor_one, descriptor_two, schema_failure)
            ):
                path = root / f"evaluation-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                evaluation_paths.append(path)
            graph = load_graph(trace_path, evaluation_paths=evaluation_paths)

        external_refs = [
            ref
            for ref, node in graph.nodes.items()
            if node.event_type == "external.evaluation_fact"
        ]
        seed_refs = [
            ref
            for ref, node in graph.nodes.items()
            if node.event_type == "case.observed_defect"
            and node.data.get("failure_signature")
        ]
        self.assertEqual(len(external_refs), 3)
        self.assertEqual(len(seed_refs), 2)
        self.assertEqual(graph.default_start_refs(), seed_refs)
        for external_ref in external_refs:
            self.assertTrue(graph.evidence_eligible(external_ref))
            self.assertFalse(graph.analysis_start_eligible(external_ref))
        self.assertEqual(
            sorted(len(graph.upstream_refs(seed_ref)) for seed_ref in seed_refs),
            [1, 2],
        )
        self.assertEqual(
            {
                upstream
                for seed_ref in seed_refs
                for upstream in graph.upstream_refs(seed_ref)
            },
            set(external_refs),
        )

    def test_direct_external_injection_projects_one_seed_per_signature(self):
        payloads = [
            external_payload(
                test_id="test_descriptor_one",
                signature=structured_signature(),
            ),
            external_payload(
                test_id="test_descriptor_two",
                signature=structured_signature(),
            ),
        ]

        enriched = inject_external_evaluation_facts(
            revision_bound_trace(),
            payloads,
        )

        seeds = [
            record
            for record in enriched["records"]
            if record.get("event_type") == "case.observed_defect"
            and record.get("data", {}).get("failure_signature")
        ]
        facts = [
            record
            for record in enriched["records"]
            if record.get("event_type") == "external.evaluation_fact"
        ]
        self.assertEqual(len(seeds), 1)
        self.assertEqual(len(facts), 2)
        self.assertEqual(seeds[0]["data"]["cluster_size"], 2)
        self.assertEqual(
            set(seeds[0]["source_refs"]),
            {f"record:{record['record_id']}" for record in facts},
        )

    def test_clusters_three_semantic_failures_independent_of_input_order(self):
        forward = project_observed_defect_seeds(failures())
        reverse = project_observed_defect_seeds(reversed(failures()))

        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward), 3)
        self.assertEqual(
            {seed.signature.exception_family for seed in forward},
            {"AttributeError", "PydanticUndefinedAnnotation"},
        )
        self.assertEqual(
            sorted(len(seed.evaluation_fact_ids) for seed in forward),
            [1, 1, 12],
        )
        self.assertEqual(
            len({seed.signature.signature_id for seed in forward}),
            3,
        )
        for seed in forward:
            self.assertEqual(len(seed.signature.signature_id), 64)
            self.assertEqual(len(seed.seed_id), 64)
            int(seed.signature.signature_id, 16)
            int(seed.seed_id, 16)

    def test_each_semantic_dimension_separates_a_superficially_equal_failure(self):
        baseline = descriptor_failure(0)
        mutations = {
            "first_business_frame": {
                "file": "pydantic/fields.py",
                "symbol": "ModelPrivateAttr.__get__",
                "line": 149,
            },
            "assertion_contract": "the descriptor must return a bound private value",
            "relevant_symbol": "ModelPrivateAttr.__get__",
            "subsystem": "pydantic.private_attribute_access",
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = {
                    **descriptor_failure(0),
                    field: value,
                    "test_id": f"changed-{field}",
                }
                self.assertEqual(
                    len(project_observed_defect_seeds([baseline, changed])),
                    2,
                )

    def test_human_labels_cannot_change_signature_or_seed_identity(self):
        labeled = failures()
        unlabeled = [
            {
                key: value
                for key, value in failure.items()
                if key not in FORBIDDEN_LABEL_KEYS
            }
            for failure in labeled
        ]

        self.assertEqual(
            project_observed_defect_seeds(labeled),
            project_observed_defect_seeds(unlabeled),
        )

    def test_nested_neutralization_uses_a_strict_factual_allowlist(self):
        baseline = layered_annotation_failure()
        baseline["failure_signature_schema_version"] = (
            "evaluation-failure-signature/v1"
        )
        baseline["prerequisite_neutralization"].update(
            {
                "expected_root_node_id": "record:secret-root-a",
                "manual_root_symbol": "secret.module.bad_root",
                "arbitrary_unknown": {
                    "another_unknown": "must-not-survive",
                },
            }
        )
        mutated = json.loads(json.dumps(baseline))
        mutated["prerequisite_neutralization"].update(
            {
                "expected_root_node_id": "record:secret-root-b",
                "manual_root_symbol": "different.secret.root",
                "arbitrary_unknown": {"changed": True},
            }
        )

        first = project_observed_defect_seeds([baseline])[0]
        second = project_observed_defect_seeds([mutated])[0]

        self.assertEqual(first.seed_id, second.seed_id)
        self.assertEqual(first.signature.signature_id, second.signature.signature_id)
        self.assertEqual(
            first.to_dict()["prerequisite_neutralizations"],
            [
                {
                    "method": "reference patch",
                    "signature": "descriptor-lifecycle",
                    "status": "neutralized",
                }
            ],
        )
        projected_keys = all_mapping_keys(first.to_dict())
        self.assertNotIn("expected_root_node_id", projected_keys)
        self.assertNotIn("manual_root_symbol", projected_keys)
        self.assertNotIn("arbitrary_unknown", projected_keys)
        self.assertNotIn("another_unknown", projected_keys)

    def test_partial_legacy_semantics_keep_legacy_record_identity_and_description(self):
        review = {
            "case_id": "pydantic-layered-evaluation",
            "can_offline_module_identify_root_cause": True,
            "observed_defects": [
                {
                    "component": "verification",
                    "failure_type": "false_completion",
                    "exception_type": "AttributeError",
                    "symbol": "BaseModel.model_rebuild",
                    "description": "Legacy externally reviewed defect.",
                    "record_refs": ["record:final_claim"],
                }
            ],
        }

        enriched = inject_quality_gap_records(base_trace(), review)
        graph = TraceGraph.from_trace(enriched)
        legacy_ref = "record:observed_defect_verification_false_completion"

        self.assertEqual(graph.default_start_refs(), [legacy_ref])
        self.assertEqual(
            graph.nodes[legacy_ref].data["description"],
            "Legacy externally reviewed defect.",
        )
        self.assertNotIn("failure_signature", graph.nodes[legacy_ref].data)
        self.assertNotIn(
            "can_offline_module_identify_root_cause",
            graph.nodes[legacy_ref].data,
        )

    def test_dynamic_assertion_values_and_string_tracebacks_normalize_stably(self):
        traceback_a = """Traceback (most recent call last):
  File "/tmp/featurebench-run-17/repo/pydantic/main.py", line 617, in model_rebuild
    raise AttributeError("field 17")
AttributeError: field 17 at 0x7ffee12a in <Model id=17>"""
        traceback_b = """Traceback (most recent call last):
  File "/tmp/featurebench-run-99/repo/pydantic/main.py", line 912, in model_rebuild
    raise AttributeError("field 99")
AttributeError: field 99 at 0xabc123 in <Model id=99>"""
        failures_with_dynamic_values = [
            {
                "failure_signature": {
                    "schema_version": "evaluation-failure-signature/v1",
                    "message": traceback,
                    "traceback": traceback,
                    "assertion_contract": (
                        f"expected field {index} at 0x{address} from "
                        f"/tmp/run-{index}/result.json; "
                        f"got <Model name='{repr_name}'>"
                    ),
                    "evaluation_run_id": f"run-{index}",
                    "test_id": f"test_parametrized[{index}]",
                }
            }
            for index, address, repr_name, traceback in (
                (17, "7ffee12a", "alpha", traceback_a),
                (99, "abc123", "beta", traceback_b),
            )
        ]

        seeds = project_observed_defect_seeds(failures_with_dynamic_values)

        self.assertEqual(len(seeds), 1)
        signature = seeds[0].signature
        self.assertEqual(signature.exception_family, "AttributeError")
        self.assertNotEqual(signature.first_business_frame, "unknown")
        self.assertNotEqual(signature.assertion_contract, "unknown")
        self.assertEqual(signature.relevant_symbol, "model_rebuild")
        self.assertIn("pydantic.main", signature.subsystem)

        different_contract = json.loads(
            json.dumps(failures_with_dynamic_values[0])
        )
        different_contract["failure_signature"]["assertion_contract"] = (
            "generated JSON Schema must retain deprecated fields"
        )
        self.assertEqual(
            len(
                project_observed_defect_seeds(
                    [failures_with_dynamic_values[0], different_contract]
                )
            ),
            2,
        )

        one_argument = json.loads(json.dumps(failures_with_dynamic_values[0]))
        one_argument["failure_signature"]["assertion_contract"] = (
            "the positional argument count must be exactly 1"
        )
        two_arguments = json.loads(json.dumps(failures_with_dynamic_values[0]))
        two_arguments["failure_signature"]["assertion_contract"] = (
            "the positional argument count must be exactly 2"
        )
        self.assertEqual(
            len(project_observed_defect_seeds([one_argument, two_arguments])),
            2,
        )

    def test_dynamic_diagnostic_numbers_collapse_but_contract_constants_do_not(self):
        def failure(assertion_contract, *, expected=None, actual=None):
            signature = structured_signature(
                assertion_contract=assertion_contract,
                test_id="test_numeric_contract",
            )
            if expected is not None:
                signature["expected"] = expected
            if actual is not None:
                signature["actual"] = actual
            return {
                "failure_signature": {
                    "schema_version": "evaluation-failure-signature/v1",
                    **signature,
                }
            }

        diagnostic_a = failure(
            "expected field 17, got field 99 at 0x7ffee12a from /tmp/run-17/out"
        )
        diagnostic_b = failure(
            "expected field 42, got field 100 at 0xabc123 from /tmp/run-42/out"
        )
        self.assertEqual(
            len(project_observed_defect_seeds([diagnostic_a, diagnostic_b])),
            1,
        )

        expected_actual_a = failure(
            None,
            expected={"request_id": 17},
            actual={"request_id": 18},
        )
        expected_actual_b = failure(
            None,
            expected={"request_id": 99},
            actual={"request_id": 100},
        )
        self.assertEqual(
            len(
                project_observed_defect_seeds(
                    [expected_actual_a, expected_actual_b]
                )
            ),
            1,
        )

        distinct_contract_pairs = (
            (
                "the endpoint must return HTTP status 200",
                "the endpoint must return HTTP status 404",
            ),
            (
                "output must conform to JSON Schema draft 2020-12",
                "output must conform to JSON Schema draft 2020-09",
            ),
            (
                "the command must exit with code 0",
                "the command must exit with code 1",
            ),
            (
                "the protocol version must be version 1",
                "the protocol version must be version 2",
            ),
            (
                "the result must contain exactly 1 item",
                "the result must contain exactly 2 items",
            ),
        )
        for first_contract, second_contract in distinct_contract_pairs:
            with self.subTest(
                first=first_contract,
                second=second_contract,
            ):
                self.assertEqual(
                    len(
                        project_observed_defect_seeds(
                            [failure(first_contract), failure(second_contract)]
                        )
                    ),
                    2,
                )

    def test_contract_template_preserves_expected_constants_only(self):
        def failure(contract):
            return {
                "failure_signature": {
                    "schema_version": "evaluation-failure-signature/v1",
                    **structured_signature(
                        assertion_contract=contract,
                        test_id="test_contract_projection",
                    ),
                }
            }

        adversarial_contracts = (
            (
                "expected HTTP status 200, got 500",
                "expected HTTP status 200, got 503",
                "expected HTTP status 404, got 503",
            ),
            (
                "expected timeout threshold 30 seconds, actual 31 seconds",
                "expected timeout threshold 30 seconds, actual 45 seconds",
                "expected timeout threshold 60 seconds, actual 45 seconds",
            ),
            (
                "expected exit code 0, observed 1",
                "expected exit code 0, observed 137",
                "expected exit code 1, observed 137",
            ),
            (
                "expected JSON Schema draft 2020-12, got draft 2019-09",
                "expected JSON Schema draft 2020-12, got draft 2023-01",
                "expected JSON Schema draft 2020-09, got draft 2023-01",
            ),
            (
                "expected protocol version 1, got version 7",
                "expected protocol version 1, got version 9",
                "expected protocol version 2, got version 9",
            ),
            (
                "expected exactly 1 item, got 7 items",
                "expected exactly 1 item, got 9 items",
                "expected exactly 2 items, got 9 items",
            ),
        )
        for first, same_contract, different_contract in adversarial_contracts:
            with self.subTest(contract=first):
                same_seeds = project_observed_defect_seeds(
                    [failure(first), failure(same_contract)]
                )
                different_seeds = project_observed_defect_seeds(
                    [failure(first), failure(different_contract)]
                )
                self.assertEqual(len(same_seeds), 1)
                self.assertEqual(len(different_seeds), 2)

        signature = project_observed_defect_seeds(
            [failure("expected HTTP status 200, got 503")]
        )[0].signature.to_dict()
        self.assertIn("contract_template", signature)
        self.assertIn("observation_values", signature)
        self.assertIn("HTTP status 200", signature["contract_template"])
        self.assertNotIn("503", signature["contract_template"])
        self.assertTrue(signature["observation_values"])

        short_id_failures = [
            failure(
                "request_id={0} expected HTTP status 200, "
                "got 503 for instance_id={1}".format(request_id, instance_id)
            )
            for request_id, instance_id in (("abc", "x1"), ("xyz", "y2"))
        ]
        self.assertEqual(
            len(project_observed_defect_seeds(short_id_failures)),
            1,
        )

    def test_review_and_external_seed_merge_is_commutative_and_idempotent(self):
        trace = active_revision_trace()
        review_evidence_refs = [
            f"record:review_evidence_{index:02d}" for index in range(26)
        ]
        trace["records"].extend(
            [
                *(
                    {
                        "record_id": ref.removeprefix("record:"),
                        "component": "verification",
                        "event_type": "verification.result",
                        "data": {
                            "status": "failed",
                            "subject_revision": "git:task2",
                            "revision_status": "matched",
                            "revision_provenance_status": "valid",
                        },
                    }
                    for ref in review_evidence_refs
                ),
                {
                    "record_id": "external_evidence",
                    "component": "evaluation",
                    "event_type": "evaluation.result",
                    "data": {
                        "status": "failed",
                        "subject_revision": "git:task2",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                    },
                },
            ]
        )
        signature = structured_signature(
            test_id="external-test",
            evaluation_run_id="external-run",
            evaluation_layer_id="external-layer",
            prerequisite_neutralization={
                "signature": "external-prerequisite",
                "status": "neutralized",
            },
        )
        payload = external_payload(test_id="external-test", signature=signature)
        payload["evidence_refs"] = ["record:external_evidence"]
        payload["provenance"]["failure_signature"]["evaluation_fact_id"] = (
            "external-fact"
        )
        review_failure = {
            **structured_signature(
                test_id="review-test",
                evaluation_run_id="review-run",
                evaluation_layer_id="review-layer",
            ),
            "evaluation_fact_id": "review-fact",
            "prerequisite_neutralization": {
                "signature": "review-prerequisite",
                "status": "neutralized",
            },
            "record_refs": review_evidence_refs,
        }
        review = {
            "case_id": trace["manifest"]["case_id"],
            "observed_defects": [review_failure],
        }

        review_then_external = inject_external_evaluation_facts(
            inject_quality_gap_records(trace, review),
            [payload],
        )
        external_then_review = inject_quality_gap_records(
            inject_external_evaluation_facts(trace, [payload]),
            review,
        )
        repeated = inject_external_evaluation_facts(
            inject_quality_gap_records(review_then_external, review),
            [payload],
        )

        def canonical_seed(enriched):
            seeds = [
                record
                for record in enriched["records"]
                if record.get("event_type") == "case.observed_defect"
                and record.get("data", {}).get("failure_signature")
            ]
            self.assertEqual(len(seeds), 1)
            return seeds[0]

        forward_seed = canonical_seed(review_then_external)
        reverse_seed = canonical_seed(external_then_review)
        repeated_seed = canonical_seed(repeated)
        self.assertEqual(forward_seed, reverse_seed)
        self.assertEqual(forward_seed, repeated_seed)
        external_fact_refs = [
            f"record:{record['record_id']}"
            for record in review_then_external["records"]
            if record.get("event_type") == "external.evaluation_fact"
        ]
        self.assertEqual(
            forward_seed["data"]["evaluation_fact_ids"],
            sorted(
                [external_fact_refs[0].removeprefix("record:"), "review-fact"]
            ),
        )
        self.assertEqual(
            forward_seed["data"]["test_ids"],
            ["external-test", "review-test"],
        )
        self.assertEqual(
            forward_seed["data"]["evaluation_run_ids"],
            ["external-run", "review-run"],
        )
        self.assertEqual(
            forward_seed["data"]["evaluation_layer_ids"],
            ["external-layer", "review-layer"],
        )
        binding = forward_seed["data"]["source_binding"]
        self.assertEqual(binding["review_refs"], review_evidence_refs)
        self.assertEqual(binding["external_refs"], external_fact_refs)
        self.assertEqual(
            binding["all_refs"],
            sorted([*review_evidence_refs, *external_fact_refs]),
        )
        self.assertEqual(binding["review_ref_count"], 26)
        self.assertEqual(binding["external_ref_count"], 1)
        self.assertEqual(binding["all_ref_count"], 27)
        self.assertEqual(binding["source_ref_count_included"], 27)
        self.assertEqual(binding["source_refs_truncated"], False)
        self.assertEqual(forward_seed["source_refs"], binding["all_refs"])
        graph = TraceGraph.from_trace(review_then_external)
        seed_ref = f"record:{forward_seed['record_id']}"
        self.assertEqual(graph.upstream_refs(seed_ref), binding["all_refs"])
        self.assertEqual(
            forward_seed["data"]["evidence_refs"],
            sorted(
                [
                    "record:external_evidence",
                    *review_evidence_refs,
                    *external_fact_refs,
                ]
            ),
        )
        self.assertEqual(
            forward_seed["data"]["prerequisite_neutralizations"],
            [
                {
                    "signature": "external-prerequisite",
                    "status": "neutralized",
                },
                {
                    "signature": "review-prerequisite",
                    "status": "neutralized",
                },
            ],
        )

    def test_real_cli_promotes_late_source_and_matches_reverse_injection(self):
        trace = active_revision_trace()
        signature = structured_signature(
            test_id="external-late-source",
            evaluation_run_id="external-run",
        )
        payload = external_payload(
            test_id="external-late-source",
            signature=signature,
        )
        external_only = inject_external_evaluation_facts(trace, [payload])
        future_external_ref = next(
            f"record:{record['record_id']}"
            for record in external_only["records"]
            if record.get("event_type") == "external.evaluation_fact"
        )
        review_failure = {
            **structured_signature(
                test_id="review-late-source",
                evaluation_run_id="review-run",
            ),
            "evaluation_fact_id": "review-late-source-fact",
            "record_refs": [future_external_ref],
        }
        review = structured_review(
            case_id=trace["manifest"]["case_id"],
            observed_defects=[review_failure],
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_path = root / "trace.json"
            review_path = root / "review.json"
            evaluation_path = root / "evaluation.json"
            trace_path.write_text(json.dumps(trace), encoding="utf-8")
            review_path.write_text(json.dumps(review), encoding="utf-8")
            evaluation_path.write_text(json.dumps(payload), encoding="utf-8")
            cli_graph = load_graph(
                trace_path,
                review_path=review_path,
                evaluation_paths=[evaluation_path],
            )

        reverse_trace = inject_quality_gap_records(external_only, review)

        def canonical_seed(enriched):
            return next(
                record
                for record in enriched["records"]
                if record.get("event_type") == "case.observed_defect"
                and record.get("data", {}).get("failure_signature")
            )

        cli_seed = canonical_seed(cli_graph.raw_trace)
        reverse_seed = canonical_seed(reverse_trace)
        self.assertEqual(cli_seed, reverse_seed)
        binding = cli_seed["data"]["source_binding"]
        self.assertEqual(binding["all_refs"], [future_external_ref])
        self.assertEqual(binding["rejected_refs"], [])
        self.assertEqual(binding["unresolved_refs"], [])
        self.assertEqual(binding["declared_total"], 1)
        self.assertEqual(binding["included"], 1)
        self.assertEqual(binding["rejected"], 0)
        self.assertEqual(binding["unresolved"], 0)
        self.assertFalse(binding["truncated"])

    def test_explicit_v1_signature_ignores_outer_and_provenance_pollution(self):
        trace = active_revision_trace()
        baseline = external_payload(
            test_id="test_explicit_scope",
            signature=structured_signature(),
        )
        polluted = json.loads(json.dumps(baseline))
        pollution = {
            "exception_family": "HumanRootLabelError",
            "first_business_frame": {
                "file": "human/root_label.py",
                "symbol": "HumanRootLabel.choose",
            },
            "assertion_contract": "HumanRootLabel must select the expected root",
            "relevant_symbol": "HumanRootLabel.choose",
            "subsystem": "human.root_label",
            "HumanRootLabel": "record:forbidden-human-root",
            "expected_root_node_id": "record:forbidden-root",
        }
        polluted["provenance"].update(pollution)
        polluted["provenance"]["failure_signature"].update(
            {
                "HumanRootLabel": "record:nested-forbidden-human-root",
                "unknown_signature_field": {
                    "exception_family": "NestedUnknownOverrideError"
                },
            }
        )

        def graph_seed(payload):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trace_path = root / "trace.json"
                evaluation_path = root / "evaluation.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                evaluation_path.write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                graph = load_graph(
                    trace_path,
                    evaluation_paths=[evaluation_path],
                )
            return next(
                node
                for node in graph.nodes.values()
                if node.event_type == "case.observed_defect"
                and node.data.get("failure_signature")
            )

        baseline_seed = graph_seed(baseline)
        polluted_seed = graph_seed(polluted)
        self.assertEqual(baseline_seed.data["seed_id"], polluted_seed.data["seed_id"])
        self.assertEqual(
            baseline_seed.data["failure_signature"],
            polluted_seed.data["failure_signature"],
        )
        self.assertTrue(
            FORBIDDEN_LABEL_KEYS.isdisjoint(
                all_mapping_keys(polluted_seed.data["failure_signature"])
            )
        )
        self.assertNotIn(
            "HumanRootLabel",
            all_mapping_keys(polluted_seed.data["failure_signature"]),
        )

        declared_signature = json.loads(
            json.dumps(baseline["provenance"]["failure_signature"])
        )
        clean_projection = project_observed_defect_seeds(
            [{"failure_signature": declared_signature}]
        )[0]
        polluted_projection = project_observed_defect_seeds(
            [
                {
                    **pollution,
                    "failure_signature": declared_signature,
                }
            ]
        )[0]
        self.assertEqual(
            clean_projection.signature,
            polluted_projection.signature,
        )
        self.assertEqual(clean_projection.seed_id, polluted_projection.seed_id)

    def test_quality_review_declared_signature_priority_ignores_legacy_pollution(self):
        trace = active_revision_trace()
        canonical_signature = {
            "schema_version": "evaluation-failure-signature/v1",
            **structured_signature(test_id="quality-priority"),
        }
        provenance_declared = {
            "provenance": {
                "failure_signature": canonical_signature,
            },
            "record_refs": ["record:final_claim"],
        }
        legacy_pollution = {
            **provenance_declared,
            "failure_signature_schema_version": (
                "evaluation-failure-signature/v1"
            ),
            "exception_family": "HumanRootLabelError",
            "first_business_frame": {
                "file": "human/root.py",
                "symbol": "HumanRootLabel.choose",
            },
            "assertion_contract": "HumanRootLabel must choose this root",
            "relevant_symbol": "HumanRootLabel.choose",
            "subsystem": "human.root_label",
            "HumanRootLabel": "record:forbidden-root",
        }
        direct_declared = json.loads(json.dumps(legacy_pollution))
        direct_declared["failure_signature"] = canonical_signature
        direct_declared["provenance"]["failure_signature"] = {
            "schema_version": "evaluation-failure-signature/v1",
            **structured_signature(
                exception_family="WrongProvenanceError",
                assertion_contract="wrong provenance contract",
                relevant_symbol="wrong.provenance",
                subsystem="wrong.provenance",
            ),
        }

        def quality_seed(observed_defect):
            review = structured_review(
                case_id=trace["manifest"]["case_id"],
                observed_defects=[observed_defect],
            )
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                trace_path = root / "trace.json"
                review_path = root / "review.json"
                trace_path.write_text(json.dumps(trace), encoding="utf-8")
                review_path.write_text(json.dumps(review), encoding="utf-8")
                graph = load_graph(trace_path, review_path=review_path)
            return next(
                node
                for node in graph.nodes.values()
                if node.event_type == "case.observed_defect"
                and node.data.get("failure_signature")
            )

        baseline_seed = quality_seed(provenance_declared)
        legacy_polluted_seed = quality_seed(legacy_pollution)
        direct_seed = quality_seed(direct_declared)
        self.assertEqual(
            baseline_seed.data["failure_signature"],
            legacy_polluted_seed.data["failure_signature"],
        )
        self.assertEqual(
            baseline_seed.data["seed_id"],
            legacy_polluted_seed.data["seed_id"],
        )
        self.assertEqual(
            baseline_seed.data["failure_signature"],
            direct_seed.data["failure_signature"],
        )
        self.assertNotIn(
            "HumanRootLabel",
            all_mapping_keys(legacy_polluted_seed.data["failure_signature"]),
        )

    def test_seed_merge_fails_closed_across_case_or_revision_boundaries(self):
        def projected_seed(trace, failure):
            review = structured_review(
                case_id=trace["manifest"]["case_id"],
                observed_defects=[failure],
            )
            enriched = inject_quality_gap_records(trace, review)
            return next(
                record
                for record in enriched["records"]
                if record.get("event_type") == "case.observed_defect"
                and record.get("data", {}).get("failure_signature")
            )

        first_failure = descriptor_failure(1)
        first_failure["record_refs"] = ["record:final_claim"]
        first = projected_seed(
            active_revision_trace(
                case_id="case-a",
                run_id="trace-run-a",
                subject_revision="git:revision-a",
            ),
            first_failure,
        )

        other_case_failure = descriptor_failure(2)
        other_case_failure["record_refs"] = ["record:final_claim"]
        other_case = projected_seed(
            active_revision_trace(
                case_id="case-b",
                run_id="trace-run-b",
                subject_revision="git:revision-a",
            ),
            other_case_failure,
        )
        with self.assertRaisesRegex(ValueError, "case_id"):
            merge_observed_defect_seed_record(
                json.loads(json.dumps(first)),
                other_case,
            )

        other_revision_failure = descriptor_failure(3)
        other_revision_failure["record_refs"] = ["record:final_claim"]
        other_revision = projected_seed(
            active_revision_trace(
                case_id="case-a",
                run_id="trace-run-c",
                subject_revision="git:revision-b",
            ),
            other_revision_failure,
        )
        with self.assertRaisesRegex(ValueError, "subject_revision"):
            merge_observed_defect_seed_record(
                json.loads(json.dumps(first)),
                other_revision,
            )

        same_boundary_failure = descriptor_failure(4)
        same_boundary_failure.update(
            {
                "evaluation_run_id": "evaluation-run-second",
                "record_refs": ["record:final_claim"],
            }
        )
        same_boundary = projected_seed(
            active_revision_trace(
                case_id="case-a",
                run_id="trace-run-d",
                subject_revision="git:revision-a",
            ),
            same_boundary_failure,
        )
        merged = json.loads(json.dumps(first))
        merge_observed_defect_seed_record(merged, same_boundary)
        self.assertEqual(merged["data"]["case_id"], "case-a")
        self.assertEqual(
            merged["data"]["subject_revision"], "git:revision-a"
        )
        self.assertEqual(
            merged["data"]["evaluation_run_ids"],
            ["evaluation-run-second", "pydantic-run-primary"],
        )

    def test_structured_source_binding_is_active_complete_and_conservative(self):
        trace = active_revision_trace()
        trace["records"].extend(
            [
                {
                    "record_id": "active_review_source",
                    "component": "verification",
                    "event_type": "verification.result",
                    "data": {
                        "subject_revision": "git:task2",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                    },
                },
                {
                    "record_id": "stale_review_source",
                    "component": "verification",
                    "event_type": "verification.result",
                    "data": {
                        "subject_revision": "git:older",
                        "revision_status": "mismatched",
                        "revision_provenance_status": "valid",
                    },
                },
                {
                    "record_id": "second_active_source",
                    "component": "verification",
                    "event_type": "verification.result",
                    "data": {
                        "subject_revision": "git:task2",
                        "revision_status": "matched",
                        "revision_provenance_status": "valid",
                    },
                },
            ]
        )
        failure = descriptor_failure(5)
        failure["record_refs"] = [
            "record:active_review_source",
            "record:stale_review_source",
            "record:missing_review_source",
        ]
        review = structured_review(
            case_id=trace["manifest"]["case_id"],
            observed_defects=[failure],
        )

        enriched = inject_quality_gap_records(trace, review)
        seed = next(
            record
            for record in enriched["records"]
            if record.get("event_type") == "case.observed_defect"
            and record.get("data", {}).get("failure_signature")
        )
        binding = seed["data"]["source_binding"]
        self.assertEqual(seed["source_refs"], ["record:active_review_source"])
        self.assertEqual(binding["review_refs"], seed["source_refs"])
        self.assertEqual(
            binding["rejected_refs"], ["record:stale_review_source"]
        )
        self.assertEqual(
            binding["unresolved_refs"], ["record:missing_review_source"]
        )
        self.assertEqual(binding["declared_total"], 3)
        self.assertEqual(binding["included"], 1)
        self.assertEqual(binding["unresolved"], 1)
        self.assertEqual(binding["rejected"], 1)
        self.assertFalse(binding["truncated"])
        graph = TraceGraph.from_trace(enriched)
        seed_ref = f"record:{seed['record_id']}"
        self.assertEqual(
            graph.upstream_refs(seed_ref), ["record:active_review_source"]
        )

        historical = json.loads(json.dumps(seed))
        historical_unresolved = [
            f"record:historical_missing_{index:02d}" for index in range(99)
        ]
        historical_binding = historical["data"]["source_binding"]
        historical_binding.update(
            {
                "schema": "observed-defect-seed-source-binding/v3",
                "declared_review_refs": [
                    "record:active_review_source",
                    *historical_unresolved,
                ],
                "declared_refs": [
                    "record:active_review_source",
                    *historical_unresolved,
                ],
                "rejected_review_refs": [],
                "rejected_refs": [],
                "declared_total": 100,
                "included": 1,
                "unresolved": 99,
                "unresolved_review_refs": historical_unresolved,
                "unresolved_refs": historical_unresolved,
                "truncated": True,
                "source_refs_truncated": True,
            }
        )
        historical["data"].update(
            {
                "source_ref_count_total": 100,
                "source_ref_count_included": 1,
                "source_refs_truncated": True,
            }
        )
        incoming_failure = descriptor_failure(6)
        incoming_failure.update(
            {
                "evaluation_run_id": "evaluation-run-new-source",
                "record_refs": ["record:second_active_source"],
            }
        )
        incoming_trace = inject_quality_gap_records(
            trace,
            structured_review(
                case_id=trace["manifest"]["case_id"],
                observed_defects=[incoming_failure],
            ),
        )
        incoming = next(
            record
            for record in incoming_trace["records"]
            if record.get("event_type") == "case.observed_defect"
            and record.get("data", {}).get("failure_signature")
        )

        merge_observed_defect_seed_record(historical, incoming)

        merged_binding = historical["data"]["source_binding"]
        self.assertEqual(merged_binding["declared_total"], 2)
        self.assertEqual(merged_binding["included"], 2)
        self.assertEqual(merged_binding["unresolved"], 0)
        self.assertEqual(merged_binding["unresolved_refs"], [])
        self.assertEqual(merged_binding["rejected"], 0)
        self.assertFalse(merged_binding["truncated"])
        history = merged_binding["historical_incompleteness"]
        self.assertEqual(history["max_declared_total"], 100)
        self.assertTrue(history["ever_truncated"])
        self.assertEqual(history["prior_unresolved_refs"], historical_unresolved)
        self.assertEqual(
            historical["data"]["source_ref_count_total"], 2
        )
        self.assertEqual(
            historical["data"]["source_ref_count_included"], 2
        )
        self.assertFalse(historical["data"]["source_refs_truncated"])

    def test_declared_source_ownership_survives_cross_source_status_migration(self):
        shared_ref = "record:shared_source"
        review_only_ref = "record:review_only"
        external_only_ref = "record:external_only"
        direct_binding = observed_defect_seed_source_binding(
            declared_review_refs=[shared_ref, review_only_ref],
            declared_external_refs=[shared_ref, external_only_ref],
            unresolved_review_refs=[shared_ref, review_only_ref],
            rejected_external_refs=[shared_ref, external_only_ref],
        )
        self.assertEqual(
            direct_binding["declared_review_refs"],
            [review_only_ref, shared_ref],
        )
        self.assertEqual(
            direct_binding["declared_external_refs"],
            [external_only_ref, shared_ref],
        )
        self.assertEqual(direct_binding["declared_review_count"], 2)
        self.assertEqual(direct_binding["declared_external_count"], 2)
        self.assertEqual(
            direct_binding["declared_refs"],
            [external_only_ref, review_only_ref, shared_ref],
        )
        self.assertEqual(direct_binding["declared_total"], 3)
        self.assertEqual(
            direct_binding["rejected_refs"],
            [external_only_ref, shared_ref],
        )
        self.assertEqual(
            direct_binding["unresolved_refs"], [review_only_ref]
        )
        self.assertEqual(direct_binding["included"], 0)
        self.assertEqual(direct_binding["rejected"], 2)
        self.assertEqual(direct_binding["unresolved"], 1)

        trace = active_revision_trace()
        future_ref = "record:future_external_source"
        review_failure = descriptor_failure(7)
        review_failure["record_refs"] = [future_ref]
        review_seed_trace = inject_quality_gap_records(
            trace,
            structured_review(
                case_id=trace["manifest"]["case_id"],
                observed_defects=[review_failure],
            ),
        )
        review_seed = next(
            record
            for record in review_seed_trace["records"]
            if record.get("event_type") == "case.observed_defect"
            and record.get("data", {}).get("failure_signature")
        )
        external_rejection = json.loads(json.dumps(review_seed))
        external_rejection["source_refs"] = []
        external_rejection["data"]["evaluation_fact_ids"] = [
            "external-rejection-fact"
        ]
        external_rejection["data"]["test_ids"] = ["external-rejection"]
        external_rejection["data"]["evidence_refs"] = []
        external_rejection["data"]["source_binding"] = (
            observed_defect_seed_source_binding(
                declared_external_refs=[future_ref],
                rejected_external_refs=[future_ref],
            )
        )

        merge_observed_defect_seed_record(review_seed, external_rejection)

        migrated = review_seed["data"]["source_binding"]
        self.assertEqual(migrated["declared_review_refs"], [future_ref])
        self.assertEqual(migrated["declared_external_refs"], [future_ref])
        self.assertEqual(migrated["declared_review_count"], 1)
        self.assertEqual(migrated["declared_external_count"], 1)
        self.assertEqual(migrated["declared_total"], 1)
        self.assertEqual(migrated["all_refs"], [])
        self.assertEqual(migrated["rejected_refs"], [future_ref])
        self.assertEqual(migrated["unresolved_refs"], [])
        self.assertEqual(migrated["included"], 0)
        self.assertEqual(migrated["rejected"], 1)
        self.assertEqual(migrated["unresolved"], 0)
        self.assertFalse(migrated["truncated"])

    def test_seed_identity_is_stable_across_run_and_member_metadata(self):
        first = descriptor_failure(1)
        second = descriptor_failure(2)
        second.update(
            {
                "evaluation_run_id": "another-run",
                "evaluation_layer_id": "rerun-after-infrastructure-recovery",
                "test_id": "renamed_parameterized_test",
            }
        )

        first_seed = project_observed_defect_seeds([first])[0]
        second_seed = project_observed_defect_seeds([second])[0]

        self.assertEqual(first_seed.seed_id, second_seed.seed_id)
        self.assertEqual(first_seed.seed_id, first_seed.signature.signature_id)
        self.assertNotEqual(
            first_seed.to_dict()["evaluation_run_ids"],
            second_seed.to_dict()["evaluation_run_ids"],
        )

    def test_failure_signature_alias_collision_is_rejected(self):
        signature = project_observed_defect_seeds([descriptor_failure(0)])[0]
        trace = base_trace()
        for suffix in ("one", "two"):
            trace["records"].append(
                {
                    "record_id": f"conflicting_seed_{suffix}",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "data": {
                        "seed_id": f"seed-{suffix}",
                        "failure_signature": signature.signature.to_dict(),
                    },
                }
            )

        with self.assertRaisesRegex(ValueError, "failure_signature.*ambiguous"):
            TraceGraph.from_trace(trace)

    def test_signature_and_seed_are_deeply_factual_and_immutable(self):
        seeds = project_observed_defect_seeds(failures())
        layered = next(
            seed
            for seed in seeds
            if seed.signature.exception_family == "PydanticUndefinedAnnotation"
        )

        self.assertIsInstance(layered, ObservedDefectSeed)
        self.assertIsInstance(layered.signature, FailureSignature)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            layered.seed_id = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            layered.signature.subsystem = "changed"

        payload = layered.to_dict()
        self.assertIn("contract_template", payload["failure_signature"])
        self.assertIn("observation_values", payload["failure_signature"])
        self.assertEqual(
            payload["evaluation_layer_ids"],
            ["layer-2-after-descriptor-neutralization"],
        )
        self.assertEqual(
            payload["prerequisite_neutralizations"],
            [
                {
                    "method": "reference patch",
                    "signature": "descriptor-lifecycle",
                    "status": "neutralized",
                }
            ],
        )
        self.assertTrue(
            FORBIDDEN_LABEL_KEYS.isdisjoint(all_mapping_keys(payload))
        )

        canonical_signature = {
            "contract_template": layered.signature.contract_template,
            "exception_family": layered.signature.exception_family,
            "first_business_frame": layered.signature.first_business_frame,
            "observation_values": list(layered.signature.observation_values),
            "relevant_symbol": layered.signature.relevant_symbol,
            "subsystem": layered.signature.subsystem,
        }
        self.assertEqual(
            layered.signature.signature_id,
            hashlib.sha256(
                json.dumps(
                    canonical_signature,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_quality_projection_emits_one_graph_seed_per_signature(self):
        review = {
            "case_id": "pydantic-layered-evaluation",
            "observed_defects": failures(),
            "ground_truth_root_cause": {
                "component": "forbidden",
                "failure_type": "forbidden",
            },
        }

        forward = inject_quality_gap_records(active_revision_trace(), review)
        reverse = inject_quality_gap_records(
            active_revision_trace(),
            {**review, "observed_defects": list(reversed(failures()))},
        )

        forward_seeds = [
            record
            for record in forward["records"]
            if record.get("event_type") == "case.observed_defect"
        ]
        reverse_seeds = [
            record
            for record in reverse["records"]
            if record.get("event_type") == "case.observed_defect"
        ]
        self.assertEqual(forward_seeds, reverse_seeds)
        self.assertEqual(len(forward_seeds), 3)
        self.assertEqual(
            sorted(
                record["data"]["cluster_size"] for record in forward_seeds
            ),
            [1, 1, 12],
        )
        self.assertTrue(
            FORBIDDEN_LABEL_KEYS.isdisjoint(
                all_mapping_keys(forward_seeds)
            )
        )

        graph = TraceGraph.from_trace(forward)
        self.assertEqual(
            graph.default_start_refs(),
            [
                f"record:{record['record_id']}"
                for record in forward_seeds
            ],
        )
        for start_ref in graph.default_start_refs():
            seed = graph.nodes[start_ref]
            self.assertFalse(seed.data["root_candidate_eligible"])
            self.assertTrue(seed.data["offline_only"])
            self.assertEqual(seed.data["behavior_impact"], "none")
            self.assertEqual(
                graph.aliases[
                    f"observed_defect_seed:{seed.data['seed_id']}"
                ],
                start_ref,
            )
            self.assertEqual(
                graph.aliases[
                    "failure_signature:{0}".format(
                        seed.data["failure_signature"]["signature_id"]
                    )
                ],
                start_ref,
            )

    def test_three_source_states_merge_complete_seed_associatively(self):
        records = [
            source_state_seed_record("unresolved", 1),
            source_state_seed_record("rejected", 2),
            source_state_seed_record("eligible", 4),
        ]
        canonical = merge_seed_sequence(records)
        for permutation in itertools.permutations(records):
            self.assertEqual(merge_seed_sequence(permutation), canonical)

        left = merge_seed_sequence(
            [merge_seed_sequence(records[:2]), records[2]]
        )
        right = merge_seed_sequence(
            [records[0], merge_seed_sequence(records[1:])]
        )
        self.assertEqual(left, right)
        self.assertEqual(
            canonical["data"]["evidence_refs"],
            [
                "record:semantic_eligible_4",
                "record:semantic_rejected_2",
                "record:semantic_unresolved_1",
                "record:shared_source",
            ],
        )
        self.assertEqual(
            canonical["data"]["source_binding"]["all_refs"],
            ["record:shared_source"],
        )
        repeated = json.loads(json.dumps(canonical))
        merge_observed_defect_seed_record(repeated, canonical)
        self.assertEqual(repeated, canonical)

    def test_source_evidence_merge_laws_hold_for_all_512_state_combinations(self):
        source_ref = "record:shared_source"
        for masks in itertools.product(range(8), repeat=3):
            records = [
                source_state_seed_record(member, mask)
                for member, mask in zip(("a", "b", "c"), masks)
            ]
            left = merge_seed_sequence(
                [merge_seed_sequence(records[:2]), records[2]]
            )
            right = merge_seed_sequence(
                [records[0], merge_seed_sequence(records[1:])]
            )
            self.assertEqual(left, right, f"association failed for {masks}")

            for permutation in itertools.permutations(records):
                self.assertEqual(
                    merge_seed_sequence(permutation),
                    left,
                    f"commutation failed for {masks}",
                )

            repeated = json.loads(json.dumps(left))
            merge_observed_defect_seed_record(repeated, left)
            self.assertEqual(
                repeated,
                left,
                f"idempotence failed for {masks}",
            )

            expected_semantic_refs = {
                f"record:semantic_{member}_{mask}"
                for member, mask in zip(("a", "b", "c"), masks)
            }
            expected_evidence_refs = set(expected_semantic_refs)
            if any(mask & 4 for mask in masks):
                expected_evidence_refs.add(source_ref)
            self.assertEqual(
                set(left["data"]["evidence_refs"]),
                expected_evidence_refs,
                f"evidence projection failed for {masks}",
            )
            self.assertEqual(
                source_ref in left["data"]["evidence_refs"],
                source_ref in left["data"]["source_binding"]["all_refs"],
                f"source eligibility mismatch for {masks}",
            )


if __name__ == "__main__":
    unittest.main()
