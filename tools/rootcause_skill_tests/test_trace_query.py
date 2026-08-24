import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KNOWN_ROOT = ROOT / "tools" / "rootcause_skill_tests" / "fixtures" / "known-root" / "trace.json"
SCRIPT = ROOT / ".claude" / "skills" / "rootcause-analysis" / "scripts" / "trace_query.py"


class TraceQueryTests(unittest.TestCase):
    def run_query(self, *arguments):
        trace = Path(arguments[arguments.index("--trace") + 1])
        before = hashlib.sha256(trace.read_bytes()).hexdigest()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        after = hashlib.sha256(trace.read_bytes()).hexdigest()
        self.assertEqual(after, before, "trace query must not modify its input")
        return result

    def compatibility_only_payload(self):
        payload = json.loads(KNOWN_ROOT.read_text(encoding="utf-8"))
        payload.pop("nodes")
        payload.pop("edges")
        for edge in payload["dataflow_edges"]:
            metadata = dict(edge.get("metadata", {}))
            metadata.update(
                {
                    "original_relation": metadata.get("original_relation", edge["relation"]),
                    "normalized_relation": edge["relation"],
                    "evidence_tier": metadata.get("evidence_tier", "confirmed"),
                    "eligible_for_attribution": edge["eligible_for_attribution"],
                    "derivation_method": metadata.get("derivation_method", "explicit_relation"),
                }
            )
            edge["metadata"] = metadata
        return payload

    def test_validate_accepts_finalized_causal_ir(self):
        result = self.run_query("validate", "--trace", str(KNOWN_ROOT))
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["causal_ir_version"], "1.0")

    def test_validate_rejects_html_and_segment_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            html = Path(directory) / "trace.html"
            html.write_text("<html></html>", encoding="utf-8")
            journal = Path(directory) / "records.jsonl"
            journal.write_text('{"event":"node.created"}\n', encoding="utf-8")
            for invalid in (html, journal):
                result = self.run_query("validate", "--trace", str(invalid))
                self.assertEqual(result.returncode, 2)
                self.assertIn("observable-trace finalize", result.stderr)

    def test_validate_rejects_incomplete_causal_ir_envelopes(self):
        with tempfile.TemporaryDirectory() as directory:
            incomplete = [
                {"nodes": [], "edges": []},
                {
                    key: value
                    for key, value in json.loads(KNOWN_ROOT.read_text(encoding="utf-8")).items()
                    if key != "journal"
                },
            ]
            for index, payload in enumerate(incomplete):
                trace = Path(directory) / "incomplete-{0}.json".format(index)
                trace.write_text(json.dumps(payload), encoding="utf-8")
                result = self.run_query("validate", "--trace", str(trace))
                self.assertEqual(result.returncode, 2)
                self.assertIn("observable-trace finalize", result.stderr)

    def test_summary_reports_lifecycle_components_and_counts(self):
        result = self.run_query("summary", "--trace", str(KNOWN_ROOT))
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["node_count"], 6)
        self.assertEqual(payload["eligible_edge_count"], 4)
        self.assertEqual(
            payload["components"],
            {"context": 1, "llm": 1, "prompt": 1, "result": 1, "runtime": 1, "tool": 1},
        )

    def test_search_matches_normalized_nodes_in_recorded_order(self):
        result = self.run_query(
            "search", "--trace", str(KNOWN_ROOT), "--query", "yocto", "--limit", "2"
        )
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual([node["ref"] for node in payload["nodes"]], ["node:req_1", "node:ctx_1"])
        self.assertEqual(payload["limit"], 2)

    def test_node_hydrates_only_recorded_edges(self):
        result = self.run_query("node", "--trace", str(KNOWN_ROOT), "--ref", "node:dec_1")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ref"], "node:dec_1")
        self.assertEqual([edge["ref"] for edge in payload["incoming_edges"]], ["edge:edge_ctx_dec"])
        self.assertEqual([edge["ref"] for edge in payload["outgoing_edges"]], ["edge:edge_dec_tool"])
        self.assertFalse(
            {"root_score", "root_ranking", "defect_label"}.intersection(payload),
            "node output must remain factual rather than making attribution judgments",
        )

    def test_compatibility_records_read_formal_edge_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            payload = self.compatibility_only_payload()
            edge = payload["dataflow_edges"][2]
            edge.pop("relation")
            edge.pop("eligible_for_attribution")
            edge["metadata"] = {
                "original_relation": "selected_by",
                "normalized_relation": "metadata_selected_by",
                "evidence_tier": "confirmed",
                "eligible_for_attribution": False,
                "derivation_method": "explicit_relation",
            }
            trace.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_query("node", "--trace", str(trace), "--ref", "record:dec_1")

        self.assertEqual(result.returncode, 0)
        node = json.loads(result.stdout)
        self.assertEqual(node["ref"], "record:dec_1")
        self.assertEqual(node["source_refs"], ["node:ctx_1"])
        self.assertEqual(node["outgoing_edges"][0]["target"], "record:tool_1")
        self.assertEqual(node["outgoing_edges"][0]["relation"], "metadata_selected_by")
        self.assertFalse(node["outgoing_edges"][0]["eligible_for_attribution"])

    def test_compatibility_edge_top_level_fields_override_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            payload = self.compatibility_only_payload()
            edge = payload["dataflow_edges"][2]
            edge["relation"] = "top_level_selected_by"
            edge["eligible_for_attribution"] = True
            edge["metadata"] = {
                "original_relation": "selected_by",
                "normalized_relation": "metadata_selected_by",
                "evidence_tier": "confirmed",
                "eligible_for_attribution": False,
                "derivation_method": "explicit_relation",
            }
            trace.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_query("node", "--trace", str(trace), "--ref", "record:dec_1")

        self.assertEqual(result.returncode, 0)
        edge = json.loads(result.stdout)["outgoing_edges"][0]
        self.assertEqual(edge["relation"], "top_level_selected_by")
        self.assertTrue(edge["eligible_for_attribution"])

    def test_node_resolves_unique_legacy_alias(self):
        result = self.run_query("node", "--trace", str(KNOWN_ROOT), "--ref", "record:decision_legacy")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["ref"], "node:dec_1")

    def test_alias_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            payload = json.loads(KNOWN_ROOT.read_text(encoding="utf-8"))
            payload["nodes"][0]["aliases"] = ["record:decision_legacy"]
            trace.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_query("node", "--trace", str(trace), "--ref", "record:decision_legacy")

        self.assertEqual(result.returncode, 2)
        self.assertIn("ambiguous alias", result.stderr)

    def test_canonical_projection_wins_over_divergent_compatibility_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            payload = json.loads(KNOWN_ROOT.read_text(encoding="utf-8"))
            payload["records"][2]["component"] = "compatibility"
            payload["records"][2]["data"] = {"decision": "compatibility-only value"}
            payload["dataflow_edges"][2]["relation"] = "compatibility_relation"
            payload["dataflow_edges"][2]["eligible_for_attribution"] = False
            trace.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_query("node", "--trace", str(trace), "--ref", "node:dec_1")

        self.assertEqual(result.returncode, 0)
        node = json.loads(result.stdout)
        self.assertEqual(node["component"], "llm")
        self.assertIn("rationale", node["payload"])
        self.assertEqual(node["outgoing_edges"][0]["relation"], "selected_by")
        self.assertTrue(node["outgoing_edges"][0]["eligible_for_attribution"])

    def test_invalid_node_reference_is_a_usage_error(self):
        result = self.run_query("node", "--trace", str(KNOWN_ROOT), "--ref", "node:missing")
        self.assertEqual(result.returncode, 2)
        self.assertIn("node:missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
