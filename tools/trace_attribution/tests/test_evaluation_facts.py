from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_retrieval import root_candidate_eligible
from trace_attribution.cli import load_graph, parse_args
from trace_attribution.evaluation_facts import inject_external_evaluation_facts
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json


def base_trace(revision: str | None = "git:abc123"):
    environment = {"revision": revision} if revision is not None else {}
    return {
        "manifest": {
            "case_id": "external-evaluation-case",
            "environment": environment,
        },
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
        self.assertFalse(root_candidate_eligible(node))

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
