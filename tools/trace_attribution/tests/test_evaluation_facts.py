from __future__ import annotations

import copy
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_retrieval import root_candidate_eligible
from trace_attribution.cli import load_graph, parse_args
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json


def base_trace(revision: str | None = "git:abc123", *, provenanced: bool = True):
    manifest = {
        "case_id": "external-evaluation-case",
        "run_id": "external-evaluation-run",
        "started_at": "2026-07-21T11:59:00Z",
        "environment": {"revision": "git:untrusted-legacy"},
    }
    if revision is not None:
        manifest["subject_revision"] = revision
        if provenanced:
            manifest["subject_revision_provenance"] = {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": manifest["case_id"],
                "run_id": manifest["run_id"],
            }
    return {
        "manifest": manifest,
        "records": [
            {
                "record_id": "tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "grader_probe"},
            }
        ],
        "dataflow_edges": [],
    }


def evaluation_payload(**overrides):
    payload = {
        "source": "terminalbench",
        "scope": "process_sigint_behavior",
        "subject_revision": "git:abc123",
        "assertion": "cleanup completes",
        "observation": "cleanup was interrupted",
        "status": "failed",
        "observed_at": "2026-07-21T12:00:00Z",
        "evidence_refs": ["record:tool_result"],
        "provenance": {"method": "benchmark_grader", "version": "1.0"},
    }
    payload.update(overrides)
    return payload


class ExternalEvaluationFactsTest(unittest.TestCase):
    def test_injects_revision_matched_external_failure_as_start_seed(self):
        graph = TraceGraph.from_trace(
            inject_external_evaluation_facts(base_trace(), [evaluation_payload()])
        )

        starts = graph.default_start_refs()

        self.assertEqual(len(starts), 1)
        node = graph.nodes[starts[0]]
        self.assertEqual(node.event_type, "external.evaluation_fact")
        self.assertEqual(node.data["revision_status"], "matched")
        self.assertTrue(node.data["eligible_for_decisive_judgment"])
        self.assertTrue(graph.evidence_eligible(node.ref))
        self.assertTrue(graph.analysis_start_eligible(node.ref))
        self.assertFalse(root_candidate_eligible(node))

    def test_graph_rejects_forged_preexisting_external_fact_without_formal_proof(self):
        trace = base_trace(revision=None)
        trace["records"].append(
            {
                "record_id": "forged_external",
                "component": "evaluation",
                "event_type": "external.evaluation_fact",
                "status": "failed",
                "data": {
                    "status": "failed",
                    "subject_revision": "git:forged",
                    "trace_revision": "git:forged",
                    "revision_status": "matched",
                    "revision_provenance_status": "valid",
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                    "eligible_for_decisive_judgment": True,
                },
            }
        )
        trace["dataflow_edges"].append(
            {
                "from": {"type": "record", "id": "tool_result"},
                "to": {"type": "external_evaluation", "id": "forged_external"},
                "relation": "external_evaluation_observed",
                "evidence_type": "external_grader",
                "eligible_for_attribution": True,
            }
        )

        graph = TraceGraph.from_trace(trace)

        self.assertIn("record:forged_external", graph.nodes)
        self.assertFalse(graph.evidence_eligible("record:forged_external"))
        self.assertFalse(graph.analysis_start_eligible("record:forged_external"))
        self.assertNotIn("record:forged_external", graph.default_start_refs())
        self.assertEqual(graph.upstream_refs("record:forged_external"), [])

    def test_mismatched_revision_is_retained_but_not_decisive_or_a_default_start(self):
        enriched = inject_external_evaluation_facts(
            base_trace(revision="git:different"), [evaluation_payload()]
        )
        graph = TraceGraph.from_trace(enriched)
        fact = next(
            node
            for node in graph.nodes.values()
            if node.event_type == "external.evaluation_fact"
        )

        self.assertEqual(fact.data["revision_status"], "mismatched")
        self.assertFalse(fact.data["eligible_for_decisive_judgment"])
        self.assertNotIn(fact.ref, graph.default_start_refs())
        self.assertEqual(enriched["dataflow_edges"][0]["eligible_for_attribution"], False)
        self.assertEqual(graph.upstream_refs(fact.ref), [])

    def test_missing_trace_revision_is_not_matched_or_decisive(self):
        graph = TraceGraph.from_trace(
            inject_external_evaluation_facts(
                base_trace(revision=None), [evaluation_payload()]
            )
        )
        fact = next(
            node
            for node in graph.nodes.values()
            if node.event_type == "external.evaluation_fact"
        )

        self.assertEqual(fact.data["revision_status"], "missing")
        self.assertFalse(fact.data["eligible_for_decisive_judgment"])
        self.assertNotIn(fact.ref, graph.default_start_refs())

    def test_legacy_environment_revision_and_unbound_formal_revision_fail_closed(self):
        for trace, expected_status in (
            (base_trace(revision=None), "missing"),
            (base_trace(provenanced=False), "unprovenanced"),
        ):
            with self.subTest(expected_status=expected_status):
                graph = TraceGraph.from_trace(
                    inject_external_evaluation_facts(trace, [evaluation_payload()])
                )
                fact = next(
                    node
                    for node in graph.nodes.values()
                    if node.event_type == "external.evaluation_fact"
                )
                self.assertEqual(fact.data["revision_status"], expected_status)
                self.assertIs(fact.data["eligible_for_decisive_judgment"], False)
                self.assertNotIn(fact.ref, graph.default_start_refs())

    def test_revision_provenance_must_bind_the_revision_to_this_run_and_case_start(self):
        invalid_bindings = [
            {"case_id": "different-case"},
            {"run_id": "different-run"},
            {"bound_at": "after_case_start"},
            {"method": "repository_inspection"},
            {"source": "config.environment.revision"},
        ]
        for override in invalid_bindings:
            with self.subTest(override=override):
                trace = base_trace()
                trace["manifest"]["subject_revision_provenance"].update(override)
                enriched = inject_external_evaluation_facts(
                    trace, [evaluation_payload()]
                )
                fact = enriched["records"][-1]["data"]
                self.assertEqual(fact["revision_status"], "unprovenanced")
                self.assertIs(fact["eligible_for_decisive_judgment"], False)
                graph = TraceGraph.from_trace(enriched)
                fact_ref = "record:{0}".format(enriched["records"][-1]["record_id"])
                self.assertFalse(graph.evidence_eligible(fact_ref))
                self.assertFalse(graph.analysis_start_eligible(fact_ref))

    def test_graph_rejects_preexisting_external_fact_with_derived_field_contradiction(self):
        enriched = inject_external_evaluation_facts(
            base_trace(), [evaluation_payload()]
        )
        fact = enriched["records"][-1]
        fact["data"]["offline_only"] = False
        fact_ref = "record:{0}".format(fact["record_id"])

        graph = TraceGraph.from_trace(enriched)

        self.assertFalse(graph.evidence_eligible(fact_ref))
        self.assertFalse(graph.analysis_start_eligible(fact_ref))
        self.assertNotIn(fact_ref, graph.default_start_refs())

    def test_passed_and_unknown_facts_are_not_decisive_or_default_starts(self):
        for status in ("passed", "unknown"):
            with self.subTest(status=status):
                graph = TraceGraph.from_trace(
                    inject_external_evaluation_facts(
                        base_trace(), [evaluation_payload(status=status)]
                    )
                )
                fact = next(
                    node
                    for node in graph.nodes.values()
                    if node.event_type == "external.evaluation_fact"
                )
                self.assertEqual(fact.data["revision_status"], "matched")
                self.assertFalse(fact.data["eligible_for_decisive_judgment"])
                self.assertNotIn(fact.ref, graph.default_start_refs())

    def test_rejects_missing_wrong_typed_and_unknown_payload_fields(self):
        missing = evaluation_payload()
        missing.pop("assertion")
        invalid_payloads = [
            (missing, "fields"),
            (evaluation_payload(source=7), "source"),
            (evaluation_payload(evidence_refs="record:tool_result"), "evidence_refs"),
            (evaluation_payload(evidence_refs=[7]), "evidence_refs"),
            (evaluation_payload(provenance=[]), "provenance"),
            ({**evaluation_payload(), "extra": True}, "fields"),
        ]
        for payload, message in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    inject_external_evaluation_facts(base_trace(), [payload])

    def test_rejects_status_outside_the_exact_enum(self):
        for status in ("FAIL", "failure", "", 1):
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, "status"):
                    inject_external_evaluation_facts(
                        base_trace(), [evaluation_payload(status=status)]
                    )

    def test_rejects_empty_trimmed_strings_malformed_timestamps_and_bad_evidence_refs(self):
        invalid_payloads = [
            (evaluation_payload(source="  \t"), "source"),
            (evaluation_payload(assertion="\n"), "assertion"),
            (evaluation_payload(observed_at="2026-07-21"), "observed_at"),
            (evaluation_payload(observed_at="2026-07-21T12:00:00"), "observed_at"),
            (evaluation_payload(observed_at="not-a-timestamp"), "observed_at"),
            (evaluation_payload(evidence_refs=[]), "evidence_refs"),
            (evaluation_payload(evidence_refs=["  "]), "evidence_refs"),
            (
                evaluation_payload(
                    evidence_refs=["record:tool_result", "record:tool_result"]
                ),
                "unique",
            ),
        ]
        for payload, message in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    inject_external_evaluation_facts(base_trace(), [payload])

    def test_rejects_empty_or_non_json_safe_provenance(self):
        invalid_provenance = [
            {},
            {"method": "", "version": "1.0"},
            {"method": "grader", "version": "  "},
            {"method": "grader", "version": True},
            {"method": "grader", "version": "1.0", "score": math.nan},
            {"method": "grader", "version": "1.0", "score": math.inf},
            {"method": "grader", "version": "1.0", "nested": {1: "bad"}},
            {"method": "grader", "version": "1.0", "nested": {"bad"}},
        ]
        for provenance in invalid_provenance:
            with self.subTest(provenance=provenance):
                with self.assertRaisesRegex(ValueError, "provenance"):
                    inject_external_evaluation_facts(
                        base_trace(), [evaluation_payload(provenance=provenance)]
                    )

    def test_record_id_uses_normalized_payload_hash_and_repeated_payload_is_idempotent(self):
        payload = evaluation_payload()
        expected = "external_evaluation_{0}".format(
            hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()[:16]
        )
        original = base_trace()

        enriched = inject_external_evaluation_facts(
            original, [copy.deepcopy(payload), copy.deepcopy(payload)]
        )

        self.assertEqual(original, base_trace())
        facts = [
            record
            for record in enriched["records"]
            if record.get("event_type") == "external.evaluation_fact"
        ]
        self.assertEqual([record["record_id"] for record in facts], [expected])
        self.assertEqual(len(enriched["dataflow_edges"]), 1)
        self.assertEqual(facts[0]["data"]["evaluation_id"], expected)

    def test_batch_forward_references_are_order_independent(self):
        passed = evaluation_payload(
            scope="upstream_verification",
            observation="cleanup passed",
            status="passed",
        )
        passed_id = "external_evaluation_{0}".format(
            hashlib.sha256(stable_json(passed).encode("utf-8")).hexdigest()[:16]
        )
        failed = evaluation_payload(
            scope="downstream_failure",
            evidence_refs=["external_evaluation:{0}".format(passed_id)],
        )
        failed_id = "external_evaluation_{0}".format(
            hashlib.sha256(stable_json(failed).encode("utf-8")).hexdigest()[:16]
        )
        semantics = []

        for payloads in ([failed, passed], [passed, failed]):
            enriched = inject_external_evaluation_facts(base_trace(), payloads)
            graph = TraceGraph.from_trace(enriched)
            external_records = [
                record
                for record in enriched["records"]
                if record.get("event_type") == "external.evaluation_fact"
            ]
            edges = [
                edge
                for edge in enriched["dataflow_edges"]
                if edge.get("relation") == "external_evaluation_observed"
            ]
            failed_record = next(
                record
                for record in external_records
                if record["record_id"] == failed_id
            )

            self.assertEqual(
                [record["data"]["scope"] for record in external_records],
                [payload["scope"] for payload in payloads],
            )
            self.assertEqual(
                [edge["to"]["id"] for edge in edges],
                [
                    failed_id if payload is failed else passed_id
                    for payload in payloads
                ],
            )
            self.assertEqual(failed_record["data"]["unresolved_evidence_refs"], [])
            self.assertEqual(
                graph.upstream_refs("record:{0}".format(failed_id)),
                ["record:{0}".format(passed_id)],
            )
            semantics.append(
                {
                    "eligible_refs": {
                        ref for ref in graph.nodes if graph.evidence_eligible(ref)
                    },
                    "eligible_edges": {
                        (
                            edge["from"]["id"],
                            edge["to"]["id"],
                            edge["eligible_for_attribution"],
                        )
                        for edge in edges
                    },
                    "default_starts": set(graph.default_start_refs()),
                }
            )

        self.assertEqual(semantics[0], semantics[1])
        self.assertEqual(
            semantics[0]["default_starts"], {"record:{0}".format(failed_id)}
        )

    def test_creates_only_resolved_evidence_edges_and_keeps_unresolved_refs_explicit(self):
        payload = evaluation_payload(
            evidence_refs=["record:tool_result", "record:not_present"]
        )

        enriched = inject_external_evaluation_facts(base_trace(), [payload])
        fact = enriched["records"][-1]
        edge = enriched["dataflow_edges"][0]

        self.assertEqual(len(enriched["dataflow_edges"]), 1)
        self.assertEqual(edge["relation"], "external_evaluation_observed")
        self.assertEqual(edge["evidence_type"], "external_grader")
        self.assertTrue(edge["eligible_for_attribution"])
        self.assertEqual(fact["data"]["evidence_refs"], payload["evidence_refs"])
        self.assertEqual(
            fact["data"]["unresolved_evidence_refs"], ["record:not_present"]
        )

    def test_matched_fact_cannot_make_mismatched_external_source_edge_eligible(self):
        mismatched = inject_external_evaluation_facts(
            base_trace(),
            [
                evaluation_payload(
                    scope="stale_cleanup_result",
                    subject_revision="git:stale",
                )
            ],
        )
        source = mismatched["records"][-1]

        enriched = inject_external_evaluation_facts(
            mismatched,
            [
                evaluation_payload(
                    scope="current_cleanup_result",
                    evidence_refs=[
                        "external_evaluation:{0}".format(source["record_id"])
                    ],
                )
            ],
        )
        target = enriched["records"][-1]
        edge = next(
            item
            for item in enriched["dataflow_edges"]
            if item["from"]["id"] == source["record_id"]
            and item["to"]["id"] == target["record_id"]
        )

        self.assertEqual(source["data"]["revision_status"], "mismatched")
        self.assertEqual(target["data"]["revision_status"], "matched")
        self.assertIs(edge["eligible_for_attribution"], False)
        self.assertEqual(target["data"]["unresolved_evidence_refs"], [])
        graph = TraceGraph.from_trace(enriched)
        self.assertIn("record:{0}".format(source["record_id"]), graph.nodes)
        self.assertNotIn(
            "record:{0}".format(source["record_id"]),
            graph.upstream_refs("record:{0}".format(target["record_id"])),
        )

    def test_ambiguous_evidence_alias_stays_unresolved_without_an_edge(self):
        trace = base_trace()
        trace["records"].append(
            {
                "record_id": "other_tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "grader_probe"},
            }
        )
        payload = evaluation_payload(evidence_refs=["tool_result:grader_probe"])

        enriched = inject_external_evaluation_facts(trace, [payload])

        self.assertEqual(enriched["dataflow_edges"], [])
        self.assertEqual(
            enriched["records"][-1]["data"]["unresolved_evidence_refs"],
            ["tool_result:grader_probe"],
        )

    def test_generated_edge_id_is_idempotent_only_for_the_full_semantic_edge(self):
        payload = evaluation_payload()
        once = inject_external_evaluation_facts(base_trace(), [payload])
        edge = copy.deepcopy(once["dataflow_edges"][0])

        identical = inject_external_evaluation_facts(once, [payload])
        self.assertEqual(identical["dataflow_edges"], [edge])

        collided = copy.deepcopy(once)
        collided["dataflow_edges"][0]["relation"] = "incompatible_relation"
        with self.assertRaisesRegex(ValueError, "edge ID collision"):
            inject_external_evaluation_facts(collided, [payload])

    def test_repeatable_cli_evaluations_load_after_review_in_argument_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace_path = root / "trace.json"
            review_path = root / "review.json"
            evaluation_paths = [root / "evaluation-1.json", root / "evaluation-2.json"]
            payloads = [
                evaluation_payload(scope="first", observation="first failure"),
                evaluation_payload(scope="second", observation="second failure"),
            ]
            trace_path.write_text(json.dumps(base_trace()), encoding="utf-8")
            review_path.write_text(
                json.dumps(
                    {
                        "case_id": "external-evaluation-case",
                        "quality_review": {
                            "quality_gaps": [{"dimension": "correctness"}]
                        },
                    }
                ),
                encoding="utf-8",
            )
            for path, payload in zip(evaluation_paths, payloads):
                path.write_text(json.dumps(payload), encoding="utf-8")

            args = parse_args(
                [
                    "--trace",
                    str(trace_path),
                    "--review",
                    str(review_path),
                    "--evaluation",
                    str(evaluation_paths[0]),
                    "--evaluation",
                    str(evaluation_paths[1]),
                    "--out",
                    str(root / "out.json"),
                ]
            )
            graph = load_graph(
                trace_path,
                review_path,
                [Path(path) for path in args.evaluation],
            )

        self.assertEqual(args.evaluation, [str(path) for path in evaluation_paths])
        event_types = [record["event_type"] for record in graph.raw_trace["records"]]
        self.assertEqual(
            event_types,
            [
                "tool.result",
                "case.quality_gap",
                "external.evaluation_fact",
                "external.evaluation_fact",
            ],
        )
        self.assertEqual(
            [
                record["data"]["scope"]
                for record in graph.raw_trace["records"]
                if record["event_type"] == "external.evaluation_fact"
            ],
            ["first", "second"],
        )


if __name__ == "__main__":
    unittest.main()
