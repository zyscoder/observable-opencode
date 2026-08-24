import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KNOWN_ROOT = ROOT / "tools" / "rootcause_skill_tests" / "fixtures" / "known-root" / "trace.json"
SCRIPT = ROOT / ".claude" / "skills" / "rootcause-analysis" / "scripts" / "trace_query.py"
ARTIFACT_ID = "build-requirement"


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

    def query_json(self, *arguments):
        result = self.run_query(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @contextmanager
    def copied_known_root(self):
        with tempfile.TemporaryDirectory() as directory:
            case_directory = Path(directory) / "known-root"
            shutil.copytree(KNOWN_ROOT.parent, case_directory)
            yield case_directory / "trace.json", case_directory

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

    def test_backward_paths_follow_only_eligible_recorded_edges(self):
        payload = self.query_json("paths", "--trace", str(KNOWN_ROOT), "--start", "node:final_1")
        paths = [item["node_refs"] for item in payload["paths"]]
        self.assertIn(
            ["node:final_1", "node:tool_1", "node:dec_1", "node:ctx_1", "node:req_1"],
            paths,
        )
        self.assertNotIn("node:unrelated_1", json.dumps(payload))

    def test_neighbors_preserve_relation_and_evidence_refs(self):
        payload = self.query_json(
            "neighbors",
            "--trace",
            str(KNOWN_ROOT),
            "--ref",
            "node:dec_1",
            "--direction",
            "upstream",
        )
        self.assertEqual(payload["edges"][0]["relation"], "context_influences_decision")
        self.assertEqual(payload["edges"][0]["evidence_refs"], ["node:ctx_1"])

    def test_query_limit_marks_output_truncated_without_hiding_frontier(self):
        payload = self.query_json(
            "neighbors",
            "--trace",
            str(KNOWN_ROOT),
            "--ref",
            "node:final_1",
            "--direction",
            "upstream",
            "--limit",
            "1",
        )
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["remaining_frontier_refs"])

    def test_neighbor_depth_bound_exposes_remaining_frontier(self):
        payload = self.query_json(
            "neighbors",
            "--trace",
            str(KNOWN_ROOT),
            "--ref",
            "node:final_1",
            "--direction",
            "upstream",
            "--depth",
            "1",
            "--limit",
            "20",
        )
        self.assertTrue(payload["truncated"])
        self.assertIn("node:tool_1", payload["remaining_frontier_refs"])

    def test_path_depth_bound_returns_partial_path_and_frontier(self):
        payload = self.query_json(
            "paths",
            "--trace",
            str(KNOWN_ROOT),
            "--start",
            "node:final_1",
            "--max-depth",
            "1",
            "--limit",
            "20",
        )
        self.assertIn(["node:final_1", "node:tool_1"], [item["node_refs"] for item in payload["paths"]])
        self.assertTrue(payload["truncated"])
        self.assertIn("node:tool_1", payload["remaining_frontier_refs"])

    def test_artifact_verifies_sha256_before_returning_content(self):
        payload = self.query_json("artifact", "--trace", str(KNOWN_ROOT), "--id", ARTIFACT_ID)
        self.assertEqual(payload["integrity"], "verified")
        self.assertIn("Yocto", payload["content"])

    def test_artifact_accepts_unprefixed_sha256_digest(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["artifacts"][0]["hash"] = payload["artifacts"][0]["hash"].split(":", 1)[1]
            trace.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["integrity"], "verified")

    def test_artifact_hash_mismatch_returns_integrity_error(self):
        with self.copied_known_root() as (trace, case_directory):
            artifact = case_directory / "artifacts" / "sha256" / (
                "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
            )
            artifact.write_text("corrupted", encoding="utf-8")
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("hash mismatch", result.stderr.lower())

    def test_artifact_rejects_path_escape(self):
        with self.copied_known_root() as (trace, case_directory):
            outside = case_directory.parent / "outside.txt"
            outside.write_text("not part of the trace", encoding="utf-8")
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["artifacts"][0]["path"] = "../outside.txt"
            trace.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("path", result.stderr.lower())

    def test_artifact_rejects_symlink_and_missing_files(self):
        with self.copied_known_root() as (trace, case_directory):
            outside = case_directory.parent / "outside.txt"
            outside.write_text("not part of the trace", encoding="utf-8")
            artifact = case_directory / "artifacts" / "sha256" / (
                "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
            )
            artifact.unlink()
            artifact.symlink_to(outside)
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("symlink", result.stderr.lower())

        with self.copied_known_root() as (trace, case_directory):
            artifact = case_directory / "artifacts" / "sha256" / (
                "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
            )
            artifact.unlink()
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("missing", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
