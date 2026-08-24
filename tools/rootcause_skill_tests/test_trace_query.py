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

    def test_summary_reports_lifecycle_components_and_counts(self):
        result = self.run_query("summary", "--trace", str(KNOWN_ROOT))
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["node_count"], 6)
        self.assertEqual(payload["eligible_edge_count"], 4)
        self.assertIn("agent", payload["components"])

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

    def test_compatibility_records_are_normalized_without_canonical_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            payload = json.loads(KNOWN_ROOT.read_text(encoding="utf-8"))
            payload.pop("nodes")
            payload.pop("edges")
            trace.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_query("node", "--trace", str(trace), "--ref", "record:dec_1")

        self.assertEqual(result.returncode, 0)
        node = json.loads(result.stdout)
        self.assertEqual(node["ref"], "record:dec_1")
        self.assertEqual(node["source_refs"], ["node:ctx_1"])
        self.assertEqual(node["outgoing_edges"][0]["target"], "record:tool_1")

    def test_invalid_node_reference_is_a_usage_error(self):
        result = self.run_query("node", "--trace", str(KNOWN_ROOT), "--ref", "node:missing")
        self.assertEqual(result.returncode, 2)
        self.assertIn("node:missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
