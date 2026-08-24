import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


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

    def trace_query_module(self):
        module_name = "rootcause_trace_query_test_module"
        spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def write_trace(self, trace, payload):
        trace.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def edge(edge_id, source, target, **fields):
        edge = {
            "edge_id": edge_id,
            "from": {"ref_type": "node", "ref_id": source},
            "to": {"ref_type": "node", "ref_id": target},
            "eligible_for_attribution": True,
        }
        edge.update(fields)
        return edge

    def test_validate_accepts_finalized_causal_ir(self):
        result = self.run_query("validate", "--trace", str(KNOWN_ROOT))
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["trace_version"], "6.0")
        self.assertEqual(payload["causal_ir_version"], "1.0")
        self.assertEqual(payload["lifecycle"]["status"], "success")
        self.assertTrue(payload["lifecycle"]["terminal"])
        self.assertTrue(payload["recovery"]["source_complete"])
        self.assertEqual(payload["diagnostics"]["recorded_count"], 0)

    def test_validate_rejects_running_snapshot_with_finalize_guidance(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["manifest"]["status"] = "running"
            self.write_trace(trace, payload)
            result = self.run_query("validate", "--trace", str(trace))

        self.assertEqual(result.returncode, 2)
        self.assertIn("nonterminal status 'running'", result.stderr)
        self.assertIn("observable-trace finalize", result.stderr)

    def test_validate_rejects_unsupported_trace_versions_with_guidance(self):
        for field, unsupported, supported in (
            ("trace_version", "7.0", "6.0"),
            ("causal_ir_version", "2.0", "1.0"),
        ):
            with self.subTest(field=field):
                with self.copied_known_root() as (trace, _):
                    payload = json.loads(trace.read_text(encoding="utf-8"))
                    payload[field] = unsupported
                    self.write_trace(trace, payload)
                    result = self.run_query("validate", "--trace", str(trace))

                self.assertEqual(result.returncode, 2)
                self.assertIn("unsupported {0} '{1}'".format(field, unsupported), result.stderr)
                self.assertIn("supported: {0}".format(supported), result.stderr)
                self.assertIn("observable-trace finalize", result.stderr)

    def test_validate_accepts_cancelled_sigterm_as_finalized(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["manifest"].update(
                {
                    "status": "cancelled",
                    "case_status": "cancelled",
                    "server_status": "cancelled",
                    "process_status": "cancelled",
                    "shutdown_signal": "SIGTERM",
                    "shutdown_disposition": "interrupted_before_case_completion",
                }
            )
            self.write_trace(trace, payload)
            response = self.query_json("validate", "--trace", str(trace))

        self.assertEqual(
            response["lifecycle"],
            {
                "status": "cancelled",
                "terminal": True,
                "case_status": "cancelled",
                "server_status": "cancelled",
                "process_status": "cancelled",
                "shutdown_signal": "SIGTERM",
                "shutdown_disposition": "interrupted_before_case_completion",
            },
        )
        self.assertTrue(response["recovery"]["source_complete"])

    def test_validate_and_summary_surface_recovery_and_diagnostic_completeness(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["manifest"].update(
                {
                    "status": "error",
                    "recovery_status": "incomplete_journal_replay",
                    "historical_interruptions": True,
                    "recovery": {"dropped_lines": 2, "segments": [{"status": "interrupted_unfinalized"}]},
                    "segment_summary": {
                        "count": 3,
                        "completed": 1,
                        "failed": 1,
                        "cancelled": 0,
                        "interrupted_unfinalized": 1,
                        "running": 0,
                    },
                }
            )
            payload["journal"]["poisoned"] = True
            payload["diagnostics"] = [{"code": "journal_replay_incomplete"}]
            payload["metrics"]["trace_health"] = {"issues": [{"code": "missing_terminal"}]}
            self.write_trace(trace, payload)
            validated = self.query_json("validate", "--trace", str(trace))
            summary = self.query_json("summary", "--trace", str(trace))

        expected_recovery = {
            "status": "incomplete_journal_replay",
            "source_complete": False,
            "historical_interruptions": True,
            "dropped_lines": 2,
            "journal_poisoned": True,
        }
        expected_segments = {
            "count": 3,
            "completed": 1,
            "failed": 1,
            "cancelled": 0,
            "interrupted_unfinalized": 1,
            "running": 0,
        }
        expected_diagnostics = {
            "recorded_count": 1,
            "complete_for_available_data": True,
            "source_data_complete": False,
            "trace_health_available": True,
            "trace_health_issue_count": 1,
        }
        for response in (validated, summary):
            self.assertEqual(response["recovery"], expected_recovery)
            self.assertEqual(response["segments"], expected_segments)
            self.assertEqual(response["diagnostics"], expected_diagnostics)

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
        self.assertEqual(payload["matched_count"], 3)
        self.assertEqual(payload["returned_count"], 2)
        self.assertEqual(payload["limit"], 2)
        self.assertEqual(payload["offset"], 0)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["next_offset"], 2)

    def test_search_offset_continues_without_gaps_or_duplicates(self):
        refs = []
        offset = 0
        while True:
            payload = self.query_json(
                "search",
                "--trace",
                str(KNOWN_ROOT),
                "--query",
                "yocto",
                "--limit",
                "1",
                "--offset",
                str(offset),
            )
            refs.extend(node["ref"] for node in payload["nodes"])
            if not payload["truncated"]:
                self.assertIsNone(payload["next_offset"])
                break
            self.assertGreater(payload["next_offset"], offset)
            offset = payload["next_offset"]

        self.assertEqual(refs, ["node:req_1", "node:ctx_1", "node:dec_1"])
        self.assertEqual(payload["matched_count"], 3)

    def test_search_rejects_negative_offset(self):
        result = self.run_query(
            "search", "--trace", str(KNOWN_ROOT), "--offset", "-1"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("offset must be at least 0", result.stderr)

    def test_canonical_node_exposes_aliases_and_dataflow_refs(self):
        payload = self.query_json("node", "--trace", str(KNOWN_ROOT), "--ref", "node:dec_1")
        self.assertEqual(payload["aliases"], ["record:decision_legacy"])
        self.assertEqual(payload["input_refs"], ["node:ctx_1"])
        self.assertEqual(payload["output_refs"], ["node:tool_1"])
        self.assertEqual(payload["source_refs"], ["node:ctx_1"])
        self.assertEqual(payload["artifact_refs"], [])

    def test_compatibility_node_exposes_empty_unprojected_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            self.write_trace(trace, self.compatibility_only_payload())
            payload = self.query_json("node", "--trace", str(trace), "--ref", "record:dec_1")

        self.assertEqual(payload["aliases"], [])
        self.assertEqual(payload["input_refs"], [])
        self.assertEqual(payload["output_refs"], [])
        self.assertEqual(payload["source_refs"], ["node:ctx_1"])
        self.assertEqual(payload["artifact_refs"], [])

    def test_node_hydrates_all_recorded_edges_with_eligibility(self):
        with self.copied_known_root() as (trace, _):
            trace_payload = json.loads(trace.read_text(encoding="utf-8"))
            trace_payload["edges"].append(
                self.edge(
                    "edge_final_unrelated",
                    "final_1",
                    "unrelated_1",
                    eligible_for_attribution=False,
                    normalized_relation="recorded_for_diagnostics",
                    evidence_tier="diagnostic",
                )
            )
            self.write_trace(trace, trace_payload)
            payload = self.query_json("node", "--trace", str(trace), "--ref", "node:final_1")

        self.assertEqual(payload["ref"], "node:final_1")
        self.assertEqual(
            [edge["ref"] for edge in payload["incoming_edges"]],
            ["edge:edge_tool_final", "edge:edge_unrelated_final"],
        )
        self.assertTrue(payload["incoming_edges"][0]["eligible_for_attribution"])
        self.assertFalse(payload["incoming_edges"][1]["eligible_for_attribution"])
        self.assertEqual(payload["incoming_edges"][1]["evidence_tier"], "temporal_advisory")
        self.assertEqual([edge["ref"] for edge in payload["outgoing_edges"]], ["edge:edge_final_unrelated"])
        self.assertFalse(payload["outgoing_edges"][0]["eligible_for_attribution"])
        self.assertEqual(payload["outgoing_edges"][0]["evidence_tier"], "diagnostic")
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
        self.assertEqual(len(payload["paths"]), 1)
        self.assertEqual(
            payload["paths"][0]["node_refs"],
            [
                "node:final_1",
                "node:tool_1",
                "node:dec_1",
                "node:ctx_1",
                "node:req_1",
            ],
        )
        self.assertEqual(
            payload["paths"][0]["edge_refs"],
            [
                "edge:edge_tool_final",
                "edge:edge_dec_tool",
                "edge:edge_ctx_dec",
                "edge:edge_req_ctx",
            ],
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
        self.assertEqual(len(payload["edges"]), 1)
        self.assertEqual(payload["edges"][0]["relation"], "context_influences_decision")
        self.assertEqual(payload["edges"][0]["evidence_refs"], ["node:ctx_1"])
        self.assertNotEqual(payload["edges"][0]["relation"], "record_source")

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

    def test_artifact_accepts_content_hash_prefixed_or_bare(self):
        for prefix in ("sha256:", ""):
            with self.subTest(prefix=prefix), self.copied_known_root() as (trace, _):
                payload = json.loads(trace.read_text(encoding="utf-8"))
                digest = payload["artifacts"][0].pop("hash").split(":", 1)[1]
                payload["artifacts"][0]["content_hash"] = "{0}{1}".format(prefix, digest)
                self.write_trace(trace, payload)
                result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["integrity"], "verified")

    def test_artifact_without_declared_digest_fails_closed(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["artifacts"][0].pop("hash")
            self.write_trace(trace, payload)
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("valid sha-256", result.stderr.lower())

    def test_artifact_rejects_conflicting_hash_declarations(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["artifacts"][0]["content_hash"] = "0" * 64
            self.write_trace(trace, payload)
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 3)
        self.assertIn("conflicting", result.stderr.lower())

    def test_artifact_accepts_matching_hash_and_content_hash_declarations(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["artifacts"][0]["content_hash"] = payload["artifacts"][0]["hash"].split(":", 1)[1]
            self.write_trace(trace, payload)
            result = self.run_query("artifact", "--trace", str(trace), "--id", ARTIFACT_ID)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["content_sha256"], payload["artifacts"][0]["content_hash"])

    def test_artifact_max_chars_truncates_verified_content(self):
        payload = self.query_json(
            "artifact", "--trace", str(KNOWN_ROOT), "--id", ARTIFACT_ID, "--max-chars", "12"
        )
        self.assertEqual(payload["content"], "Use the buil")
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["integrity"], "verified")

    def test_artifact_rejects_replacement_with_symlink_after_index_load(self):
        with self.copied_known_root() as (trace, case_directory):
            module = self.trace_query_module()
            index = module.TraceIndex.load(trace)
            artifact = case_directory / "artifacts" / "sha256" / (
                "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
            )
            outside = case_directory.parent / "replacement.txt"
            outside.write_bytes(artifact.read_bytes())
            original_open = module.os.open
            replaced = False

            def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal replaced
                if path == artifact.name and dir_fd is not None and artifact.exists():
                    artifact.unlink()
                    artifact.symlink_to(outside)
                    replaced = True
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(module.os, "open", side_effect=replace_before_open):
                with self.assertRaises(module.ArtifactIntegrityError):
                    index.artifact(ARTIFACT_ID, 20000)
            self.assertTrue(replaced)

    def test_artifact_closes_new_directory_descriptor_after_identity_failure(self):
        with self.copied_known_root() as (trace, _):
            module = self.trace_query_module()
            index = module.TraceIndex.load(trace)
            original_open = module.os.open
            original_stat = module.os.stat
            original_close = module.os.close
            opened = []
            closed = []

            def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                opened.append((path, descriptor, dir_fd))
                return descriptor

            def fail_intermediate_identity(path, *arguments, **keywords):
                if path == "artifacts" and keywords.get("dir_fd") is not None:
                    raise OSError("forced intermediate identity failure")
                return original_stat(path, *arguments, **keywords)

            def tracking_close(descriptor):
                closed.append(descriptor)
                return original_close(descriptor)

            with mock.patch.object(module.os, "open", side_effect=tracking_open):
                with mock.patch.object(module.os, "stat", side_effect=fail_intermediate_identity):
                    with mock.patch.object(module.os, "close", side_effect=tracking_close):
                        with self.assertRaises(module.ArtifactIntegrityError):
                            index.artifact(ARTIFACT_ID, 20000)

        intermediate_descriptor = next(
            descriptor for path, descriptor, _ in opened if path == "artifacts"
        )
        self.assertEqual(closed.count(intermediate_descriptor), 1)

    def test_artifact_max_chars_at_content_length_is_not_truncated(self):
        expected = (KNOWN_ROOT.parent / "artifacts" / "sha256" / (
            "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
        )).read_text(encoding="utf-8")
        payload = self.query_json(
            "artifact",
            "--trace",
            str(KNOWN_ROOT),
            "--id",
            ARTIFACT_ID,
            "--max-chars",
            str(len(expected)),
        )
        self.assertEqual(payload["content"], expected)
        self.assertFalse(payload["truncated"])

    def test_artifact_max_chars_one_below_content_length_truncates(self):
        expected = (KNOWN_ROOT.parent / "artifacts" / "sha256" / (
            "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
        )).read_text(encoding="utf-8")
        payload = self.query_json(
            "artifact",
            "--trace",
            str(KNOWN_ROOT),
            "--id",
            ARTIFACT_ID,
            "--max-chars",
            str(len(expected) - 1),
        )
        self.assertEqual(payload["content"], expected[:-1])
        self.assertTrue(payload["truncated"])

    def test_artifact_rejects_non_positive_max_chars(self):
        for max_chars in ("0", "-1"):
            with self.subTest(max_chars=max_chars):
                result = self.run_query(
                    "artifact",
                    "--trace",
                    str(KNOWN_ROOT),
                    "--id",
                    ARTIFACT_ID,
                    "--max-chars",
                    max_chars,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("max_chars must be at least 1", result.stderr)

    def test_neighbors_traverse_downstream_eligible_edges(self):
        payload = self.query_json(
            "neighbors",
            "--trace",
            str(KNOWN_ROOT),
            "--ref",
            "node:ctx_1",
            "--direction",
            "downstream",
        )
        self.assertEqual(payload["ref"], "node:ctx_1")
        self.assertEqual(payload["edges"][0]["source"], "node:ctx_1")
        self.assertEqual(payload["edges"][0]["target"], "node:dec_1")

    def test_compatibility_paths_traverse_declared_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            self.write_trace(trace, self.compatibility_only_payload())
            payload = self.query_json("paths", "--trace", str(trace), "--start", "record:final_1")

        self.assertIn(
            ["record:final_1", "record:tool_1", "record:dec_1", "record:ctx_1", "record:req_1"],
            [item["node_refs"] for item in payload["paths"]],
        )

    def test_traversal_resolves_legacy_aliases(self):
        payload = self.query_json(
            "neighbors",
            "--trace",
            str(KNOWN_ROOT),
            "--ref",
            "record:decision_legacy",
            "--direction",
            "upstream",
        )
        self.assertEqual(payload["ref"], "node:dec_1")
        self.assertEqual(payload["edges"][0]["source"], "node:ctx_1")

    def test_traversal_excludes_eligible_temporal_advisory_edges(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            edge = next(item for item in payload["edges"] if item["edge_id"] == "edge_unrelated_final")
            edge["eligible_for_attribution"] = True
            self.write_trace(trace, payload)
            response = self.query_json("paths", "--trace", str(trace), "--start", "node:final_1")

        self.assertNotIn("node:unrelated_1", json.dumps(response))

    def test_traversal_excludes_unresolved_and_self_edges(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["edges"].extend(
                [
                    self.edge("edge_self", "final_1", "final_1", normalized_relation="loop"),
                    self.edge("edge_missing", "missing_1", "final_1", normalized_relation="unknown"),
                ]
            )
            self.write_trace(trace, payload)
            response = self.query_json("paths", "--trace", str(trace), "--start", "node:final_1")

        serialized = json.dumps(response)
        self.assertNotIn("edge:edge_self", serialized)
        self.assertNotIn("edge:edge_missing", serialized)
        self.assertNotIn("node:missing_1", serialized)

    def test_neighbors_use_branching_breadth_first_order_and_frontier(self):
        with self.copied_known_root() as (trace, _):
            payload = json.loads(trace.read_text(encoding="utf-8"))
            payload["nodes"].extend(
                [
                    {"node_id": "branch_a", "source_refs": ["node:branch_a_root"]},
                    {"node_id": "branch_b", "source_refs": ["node:branch_b_root"]},
                    {"node_id": "branch_a_root"},
                    {"node_id": "branch_b_root"},
                ]
            )
            payload["edges"].extend(
                [
                    self.edge("edge_branch_a_final", "branch_a", "final_1", normalized_relation="branch_a"),
                    self.edge("edge_branch_b_final", "branch_b", "final_1", normalized_relation="branch_b"),
                    self.edge("edge_branch_a_root", "branch_a_root", "branch_a", normalized_relation="parent"),
                    self.edge("edge_branch_b_root", "branch_b_root", "branch_b", normalized_relation="parent"),
                ]
            )
            self.write_trace(trace, payload)
            response = self.query_json(
                "neighbors",
                "--trace",
                str(trace),
                "--ref",
                "node:final_1",
                "--direction",
                "upstream",
                "--depth",
                "2",
                "--limit",
                "4",
            )

        self.assertEqual(
            [edge["ref"] for edge in response["edges"]],
            ["edge:edge_tool_final", "edge:edge_branch_a_final", "edge:edge_branch_b_final", "edge:edge_dec_tool"],
        )
        self.assertTrue(response["truncated"])
        self.assertEqual(
            response["remaining_frontier_refs"],
            ["node:dec_1", "node:branch_a_root", "node:branch_a", "node:branch_b"],
        )


if __name__ == "__main__":
    unittest.main()
