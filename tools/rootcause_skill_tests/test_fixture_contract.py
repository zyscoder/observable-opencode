import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tools" / "rootcause_skill_tests" / "fixtures"
PRESSURE = ROOT / "tools" / "rootcause_skill_tests" / "pressure"
RUNNER = PRESSURE / "prepare_isolated_bundle.py"
FORWARD_RUNNER = PRESSURE / "run_isolated_opencode.py"
CASES_PATH = PRESSURE / "cases.json"
SKILL_ROOT = ROOT / ".claude" / "skills" / "rootcause-analysis"


def payload_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def source_hash(node):
    return payload_hash(
        {
            "inputRefs": node["input_refs"],
            "outputRefs": node["output_refs"],
            "sourceRefs": node["source_refs"],
            "sourceLocations": node["source_locations"],
            "artifactRefs": node["artifact_refs"],
        }
    )


def assert_node_integrity(test, node):
    test.assertEqual(node["integrity"]["payload_hash"], payload_hash(node["payload"]))
    if "source_hash" in node["integrity"]:
        test.assertEqual(node["integrity"]["source_hash"], source_hash(node))


def assert_typed_refs(test, refs, label):
    test.assertIsInstance(refs, list, label)
    for ref in refs:
        test.assertIsInstance(ref, dict, label)
        test.assertIn(ref.get("ref_type"), {"node", "artifact", "raw_event", "external"}, label)
        test.assertIsInstance(ref.get("ref_id"), str, label)
        test.assertTrue(ref["ref_id"], label)


class FixtureContractTests(unittest.TestCase):
    def traces(self):
        return sorted(FIXTURES.glob("*/trace.json"))

    def test_traces_use_full_canonical_node_and_edge_envelopes(self):
        for path in self.traces():
            with self.subTest(path=path):
                trace = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(trace["causal_ir_version"], "1.0")
                for index, node in enumerate(trace["nodes"], start=1):
                    with self.subTest(node=node.get("node_id")):
                        for key in (
                            "node_id",
                            "kind",
                            "schema_version",
                            "origin",
                            "component",
                            "order",
                            "scope",
                            "payload",
                            "input_refs",
                            "output_refs",
                            "source_refs",
                            "source_locations",
                            "artifact_refs",
                            "aliases",
                            "derivation",
                            "integrity",
                            "timestamp",
                            "time_ms",
                            "data",
                        ):
                            self.assertIn(key, node)
                        self.assertEqual(node["schema_version"], "1.0")
                        self.assertIn(node["origin"], {"observed", "deterministic_derived", "offline_derived"})
                        self.assertEqual(node["order"]["sequence"], index)
                        self.assertEqual(node["order"]["timestamp"], node["timestamp"])
                        self.assertEqual(node["order"]["time_ms"], node["time_ms"])
                        self.assertEqual(node["scope"]["run_id"], trace["manifest"]["run_id"])
                        self.assertEqual(node["scope"]["case_id"], trace["manifest"]["case_id"])
                        self.assertEqual(node["payload"], node["data"])
                        assert_node_integrity(self, node)
                        for field in ("input_refs", "output_refs", "source_refs"):
                            assert_typed_refs(self, node[field], f"{node['node_id']}.{field}")
                        self.assertIsInstance(node["source_locations"], list)
                        self.assertIsInstance(node["artifact_refs"], list)
                        self.assertIsInstance(node["aliases"], list)
                        self.assertIsNone(node["derivation"])
                for edge in trace["edges"]:
                    with self.subTest(edge=edge.get("edge_id")):
                        for key in (
                            "edge_id",
                            "from",
                            "to",
                            "original_relation",
                            "normalized_relation",
                            "evidence_tier",
                            "eligible_for_attribution",
                            "derivation_method",
                            "evidence_refs",
                        ):
                            self.assertIn(key, edge)
                        assert_typed_refs(self, [edge["from"], edge["to"]], f"{edge['edge_id']}.endpoints")
                        assert_typed_refs(self, edge["evidence_refs"], f"{edge['edge_id']}.evidence_refs")
                        self.assertIsInstance(edge["eligible_for_attribution"], bool)

    def test_source_hash_validation_rejects_a_mutated_canonical_node(self):
        trace = json.loads((FIXTURES / "known-root" / "trace.json").read_text(encoding="utf-8"))
        node = trace["nodes"][0]
        node["integrity"]["source_hash"] = "not-the-deterministic-source-hash"
        with self.assertRaises(AssertionError):
            assert_node_integrity(self, node)

    def test_temporal_advisory_edges_are_recorded_in_time_order_and_ineligible(self):
        trace = json.loads((FIXTURES / "known-root" / "trace.json").read_text(encoding="utf-8"))
        times = {node["node_id"]: node["time_ms"] for node in trace["nodes"]}
        advisory = [edge for edge in trace["edges"] if edge["evidence_tier"] == "temporal_advisory"]
        self.assertEqual(len(advisory), 1)
        edge = advisory[0]
        self.assertFalse(edge["eligible_for_attribution"])
        self.assertEqual(edge["derivation_method"], "recorded_time_order")
        self.assertLess(times[edge["from"]["ref_id"]], times[edge["to"]["ref_id"]])

    def test_isolated_bundles_cover_all_cases_without_evaluator_data(self):
        self.assertTrue(RUNNER.is_file(), "pressure bundle preparation script is required")
        case_specs = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
        self.assertEqual(len(case_specs), 7)
        canonical_skill_files = {
            Path(".claude/skills/rootcause-analysis") / path.relative_to(SKILL_ROOT)
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
        }
        with tempfile.TemporaryDirectory() as directory:
            for case_spec in case_specs:
                case = case_spec["fixture"]
                with self.subTest(case=case):
                    opaque_id = f"case-{uuid.uuid4().hex}"
                    destination = Path(directory) / opaque_id
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(RUNNER),
                            "--case",
                            case,
                            "--destination",
                            str(destination),
                            "--opaque-case-id",
                            opaque_id,
                            "--agent-trace-path",
                            "/workspace/trace.json",
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

                    provenance = json.loads(result.stdout)
                    self.assertEqual(provenance["source_fixture"], case)
                    self.assertEqual(provenance["opaque_case_id"], opaque_id)
                    self.assertEqual(provenance["derivation"]["kind"], "sanitized_identity_projection")

                    source_trace = json.loads((FIXTURES / case / "trace.json").read_text(encoding="utf-8"))
                    trace = json.loads((destination / "trace.json").read_text(encoding="utf-8"))
                    artifact_files = {Path(item["path"]) for item in trace["artifacts"]}
                    expected_files = {
                        Path("trace.json"),
                        Path("prompt-claude.md"),
                        Path("prompt-opencode.md"),
                    } | canonical_skill_files | artifact_files
                    actual_files = {
                        path.relative_to(destination)
                        for path in destination.rglob("*")
                        if path.is_file()
                    }
                    self.assertEqual(actual_files, expected_files)

                    for path in destination.rglob("*"):
                        self.assertFalse(path.is_symlink(), path)
                        path.resolve(strict=True).relative_to(destination.resolve(strict=True))

                    self.assertEqual(trace["manifest"]["case_id"], opaque_id)
                    self.assertEqual(trace["manifest"]["run_id"], opaque_id)
                    for node in trace["nodes"]:
                        self.assertEqual(node["scope"]["case_id"], opaque_id)
                        self.assertEqual(node["scope"]["run_id"], opaque_id)
                        assert_node_integrity(self, node)
                    self.assertEqual(
                        hashlib.sha256((FIXTURES / case / "trace.json").read_bytes()).hexdigest(),
                        provenance["source_trace_sha256"],
                    )
                    self.assertEqual(
                        hashlib.sha256((destination / "trace.json").read_bytes()).hexdigest(),
                        provenance["derived_trace_sha256"],
                    )
                    self.assertNotEqual(trace["manifest"], source_trace["manifest"])
                    for relative in canonical_skill_files:
                        source = ROOT / relative
                        self.assertEqual((destination / relative).read_bytes(), source.read_bytes())

                    for prompt_name in ("prompt-claude.md", "prompt-opencode.md"):
                        prompt = (destination / prompt_name).read_text(encoding="utf-8")
                        self.assertIn(case_spec["question"], prompt)
                        self.assertIn("/workspace/trace.json", prompt)
                        self.assertNotIn(str(destination), prompt)
                        self.assertNotIn(str(ROOT), prompt)
                        for hidden_key in case_spec["expected"]:
                            self.assertNotIn(hidden_key, prompt)
                        self.assertNotIn(json.dumps(case_spec["expected"], ensure_ascii=False), prompt)

                    names = {path.name for path in destination.rglob("*")}
                    self.assertNotIn("cases.json", names)
                    self.assertNotIn("rubric.json", names)
                    self.assertNotIn(".git", names)
                    self.assertNotIn("reports", names)

                    visible_paths = [str(path.relative_to(destination)) for path in destination.rglob("*")]
                    visible_bytes = "\n".join(
                        path.read_text(encoding="utf-8", errors="replace")
                        for path in destination.rglob("*")
                        if path.is_file()
                    )
                    questionless = visible_bytes.replace(case_spec["question"], "")
                    for semantic_slug in {item["fixture"] for item in case_specs}:
                        self.assertNotIn(semantic_slug, "\n".join(visible_paths))
                        self.assertNotIn(f"rootcause-{semantic_slug}", questionless)
                        if semantic_slug != "ambiguous":
                            self.assertNotIn(semantic_slug, questionless)
                    identity_values = []
                    def collect_strings(value):
                        if isinstance(value, dict):
                            for child in value.values():
                                collect_strings(child)
                        elif isinstance(value, list):
                            for child in value:
                                collect_strings(child)
                        elif isinstance(value, str):
                            identity_values.append(value)
                    collect_strings(trace)
                    for semantic_slug in {item["fixture"] for item in case_specs}:
                        self.assertNotIn(semantic_slug, identity_values)
                        self.assertNotIn(f"rootcause-{semantic_slug}", identity_values)
                    self.assertNotIn('"expected"', questionless)
                    self.assertNotIn(json.dumps(case_spec["expected"], ensure_ascii=False), questionless)
                    self.assertNotIn("source_fixture", visible_bytes)
                    self.assertNotIn("derived_trace_sha256", visible_bytes)

    def test_isolated_bundle_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "case-0123456789abcdef0123456789abcdef"
            command = [
                sys.executable,
                str(RUNNER),
                "--case",
                "known-root",
                "--destination",
                str(destination),
                "--opaque-case-id",
                "case-0123456789abcdef0123456789abcdef",
            ]
            first = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            second = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(second.returncode, 0)
            after = {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_isolated_bundle_rejects_artifact_hash_mismatch(self):
        spec = importlib.util.spec_from_file_location("prepare_isolated_bundle_hash", RUNNER)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root = root / "fixtures"
            source = fixture_root / "known-root"
            shutil.copytree(FIXTURES / "known-root", source)
            artifact = next(path for path in (source / "artifacts").rglob("*") if path.is_file())
            artifact.write_text("tampered", encoding="utf-8")
            module.FIXTURES = fixture_root

            with self.assertRaisesRegex(ValueError, "digest"):
                module.prepare(
                    "known-root",
                    root / "case-0123456789abcdef0123456789abcdef",
                    "case-0123456789abcdef0123456789abcdef",
                    "/workspace/trace.json",
                )

    def test_forward_runner_smoke_is_explicitly_unscored(self):
        executable = shutil.which("echo")
        self.assertIsNotNone(executable)
        with tempfile.TemporaryDirectory() as directory:
            batch_root = Path(directory) / "fresh-batch"
            result = subprocess.run(
                [
                    sys.executable,
                    str(FORWARD_RUNNER),
                    "--mode",
                    "smoke",
                    "--batch-root",
                    str(batch_root),
                    "--case",
                    "known-root",
                    "--opencode-bin",
                    executable,
                    "--timeout-seconds",
                    "10",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["run_kind"], "smoke_local")
            self.assertEqual(record["evaluation_status"], "unscored_smoke")
            self.assertNotIn("scored", record["run_kind"])
            workspace = Path(record["workspace"])
            self.assertRegex(workspace.name, r"^case-[0-9a-f]{32}$")
            self.assertEqual(record["cwd"], str(workspace))
            self.assertEqual(record["trace_path"], str(workspace / "trace.json"))
            self.assertEqual(record["command_argv"][0], executable)
            self.assertEqual(record["raw_returncode"], 0)
            self.assertFalse(record["timed_out"])
            self.assertIsNone(record["wrapper_error"])
            self.assertNotIn(str(ROOT), record["prompt"])

    def test_forward_runner_redacts_provider_secrets_from_host_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "echo-provider-secret"
            executable.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$APIKEY\" \"$OPENCODE_CONFIG_CONTENT\"\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            secret = "audit-must-not-contain-this-api-key"
            config = '{"apiKey":"audit-must-not-contain-config-secret"}'
            environment = dict(os.environ, APIKEY=secret, OPENCODE_CONFIG_CONTENT=config)
            result = subprocess.run(
                [
                    sys.executable,
                    str(FORWARD_RUNNER),
                    "--mode",
                    "smoke",
                    "--batch-root",
                    str(root / "batch"),
                    "--case",
                    "known-root",
                    "--opencode-bin",
                    str(executable),
                    "--timeout-seconds",
                    "2",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            audit_bytes = (root / "batch" / "results.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(secret, audit_bytes)
            self.assertNotIn(config, audit_bytes)
            self.assertIn("<redacted:APIKEY>", audit_bytes)
            self.assertIn("<redacted:OPENCODE_CONFIG_CONTENT>", audit_bytes)

    def test_scored_container_command_mounts_only_one_workspace_and_linux_binary(self):
        spec = importlib.util.spec_from_file_location("scored_forward_runner", FORWARD_RUNNER)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "case-0123456789abcdef0123456789abcdef"
            workspace.mkdir()
            binary = root / "opencode-linux-x64"
            binary.write_bytes(b"\x7fELF" + b"\0" * 32)
            binary.chmod(0o755)
            command = module.build_scored_container_command(
                "docker",
                "debian:bookworm-slim",
                workspace,
                binary,
                "Trace: /workspace/trace.json; prompt without a secret",
                {"MODEL": "m", "URL": "https://provider.invalid/v1", "APIKEY": "top-secret"},
            )
            mounts = module.container_mounts(command)
            self.assertEqual(
                mounts,
                {
                    str(workspace.resolve()): "/workspace",
                    str(binary.resolve()): "/opt/opencode",
                },
            )
            rendered = json.dumps(command, ensure_ascii=False)
            self.assertNotIn("top-secret", rendered)
            self.assertNotIn(str(root / "evaluator"), rendered)
            self.assertNotIn(str(root / "audit.jsonl"), rendered)
            self.assertIn("/workspace/trace.json", rendered)
            self.assertIn("APIKEY", command)

    def test_scored_mode_refuses_without_container_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "opencode-linux-x64"
            binary.write_bytes(b"\x7fELF" + b"\0" * 32)
            binary.chmod(0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(FORWARD_RUNNER),
                    "--mode",
                    "scored",
                    "--container-runtime",
                    "podman",
                    "--opencode-bin",
                    str(binary),
                    "--batch-root",
                    str(Path(directory) / "batch"),
                    "--case",
                    "known-root",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("filesystem sandbox", result.stderr)
            self.assertFalse((Path(directory) / "batch").exists())

    def test_scored_adversarial_probe_has_the_same_two_mount_boundary(self):
        spec = importlib.util.spec_from_file_location("scored_probe_runner", FORWARD_RUNNER)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "case-0123456789abcdef0123456789abcdef"
            workspace.mkdir()
            (workspace / "trace.json").write_text("{}", encoding="utf-8")
            binary = root / "opencode-linux-x64"
            binary.write_bytes(b"\x7fELF" + b"\0" * 32)
            canary = root / "evaluator-canary"
            canary.write_text("hidden", encoding="utf-8")
            command = module.build_scored_probe_command(
                "docker", "debian:bookworm-slim", workspace, binary, canary
            )
            self.assertEqual(
                module.container_mounts(command),
                {
                    str(workspace.resolve()): "/workspace",
                    str(binary.resolve()): "/opt/opencode",
                },
            )
            rendered = shlex.join(command)
            for unavailable in (
                "/workspace/../evaluator",
                "/workspace/../audit",
                "/workspace/../batch",
                str(canary),
            ):
                self.assertIn(unavailable, rendered)
            self.assertNotIn(str(root / "cases.json"), rendered)
            self.assertNotIn(str(root / "rubric.json"), rendered)

    def test_scored_adversarial_probe_integration_or_explicit_block(self):
        docker = shutil.which("docker")
        if docker is None:
            self.skipTest("BLOCKED: Docker absent; Podman integration is not available")
        info = subprocess.run(
            [docker, "info"], text=True, capture_output=True, check=False, timeout=15
        )
        if info.returncode != 0:
            blocker = info.stderr.strip() or info.stdout.strip()
            self.skipTest(f"BLOCKED: Docker daemon unavailable: {blocker}")
        spec = importlib.util.spec_from_file_location("scored_probe_integration", FORWARD_RUNNER)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "case-0123456789abcdef0123456789abcdef"
            workspace.mkdir()
            (workspace / "trace.json").write_text("{}", encoding="utf-8")
            binary = root / "opencode-linux-x64"
            binary.write_bytes(b"\x7fELF" + b"\0" * 32)
            canary = root / "evaluator-canary"
            canary.write_text("hidden", encoding="utf-8")
            command = module.build_scored_probe_command(
                docker, "debian:bookworm-slim", workspace, binary, canary
            )
            probe = subprocess.run(
                command, text=True, capture_output=True, check=False, timeout=120
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_isolated_bundle_rejects_a_fixture_local_artifact_symlink_escape(self):
        spec = importlib.util.spec_from_file_location("prepare_isolated_bundle", RUNNER)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_root = root / "fixtures"
            source = fixture_root / "known-root"
            shutil.copytree(FIXTURES / "known-root", source)
            evaluator_data = root / "evaluator" / "cases.json"
            evaluator_data.parent.mkdir()
            evaluator_data.write_text('{"expected":"must not escape"}', encoding="utf-8")
            artifact = source / "artifacts" / "sha256" / "3ae017fde4b7a5c634d92ade034c43241b383de7f30b328dea292a91d7a21fb1"
            artifact.unlink()
            artifact.symlink_to(evaluator_data)
            module.FIXTURES = fixture_root

            with self.assertRaises(ValueError):
                module.prepare(
                    "known-root",
                    root / "case-0123456789abcdef0123456789abcdef",
                    "case-0123456789abcdef0123456789abcdef",
                    "/workspace/trace.json",
                )


if __name__ == "__main__":
    unittest.main()
