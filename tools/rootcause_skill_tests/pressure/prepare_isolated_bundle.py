#!/usr/bin/env python3
"""Create one no-overwrite, evaluator-free root-cause forward-test workspace."""

import argparse
import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tools" / "rootcause_skill_tests" / "fixtures"
CASES_PATH = Path(__file__).with_name("cases.json")
SKILL_ROOT = ROOT / ".claude" / "skills" / "rootcause-analysis"
TERMINAL_STATUSES = {"success", "error", "cancelled"}
OPAQUE_CASE_PATTERN = re.compile(r"case-[0-9a-f]{32}\Z")


def canonical_hash(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def source_hash(node):
    return canonical_hash(
        {
            "inputRefs": node["input_refs"],
            "outputRefs": node["output_refs"],
            "sourceRefs": node["source_refs"],
            "sourceLocations": node["source_locations"],
            "artifactRefs": node["artifact_refs"],
        }
    )


def case_questions():
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    questions = {}
    for item in payload.get("cases", []):
        fixture = item.get("fixture")
        question = item.get("question")
        if not isinstance(fixture, str) or not fixture:
            raise ValueError("Every pressure case must have a non-empty fixture")
        if fixture in questions:
            raise ValueError(f"Duplicate pressure case: {fixture}")
        if not isinstance(question, str) or not question:
            raise ValueError(f"Pressure case has no question: {fixture}")
        questions[fixture] = question
    if not questions:
        raise ValueError("No pressure cases are defined")
    return questions


def relative_artifact_path(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Artifact path must stay within the selected fixture: {value}")
    return path


def validate_agent_trace_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Agent Trace path must be a non-empty absolute POSIX path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Agent Trace path must not contain control characters")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Agent Trace path must be an absolute contained POSIX path")
    return str(path)


def regular_file(source, relative, label):
    root = source.resolve(strict=True)
    if source.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {source}")

    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"{label} path must not traverse a symlink: {relative}")

    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} path escapes its root: {relative}") from error
    if not resolved.is_file():
        raise ValueError(f"Referenced file is missing: {candidate}")
    return resolved


def fixture_file(source, relative):
    return regular_file(source, relative, "Fixture")


def validate_trace(trace, case):
    if not isinstance(trace, dict):
        raise ValueError("Trace must be a JSON object")
    if trace.get("causal_ir_version") != "1.0":
        raise ValueError("Trace must use causal_ir_version 1.0")
    manifest = trace.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("Trace manifest is missing")
    if manifest.get("case_id") != case:
        raise ValueError(f"Trace case_id does not match selected case: {case}")
    if manifest.get("status") not in TERMINAL_STATUSES:
        raise ValueError("Trace manifest status is not terminal")
    for collection in ("nodes", "edges", "artifacts", "records", "dataflow_edges"):
        if not isinstance(trace.get(collection), list):
            raise ValueError(f"Trace collection is missing: {collection}")

    for node in trace["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("integrity"), dict):
            raise ValueError("Canonical node integrity metadata is missing")
        integrity = node["integrity"]
        declared_payload = integrity.get("payload_hash")
        if declared_payload != canonical_hash(node.get("payload")):
            raise ValueError(f"Node payload digest mismatch: {node.get('node_id')}")
        declared_source = integrity.get("source_hash")
        if not isinstance(declared_source, str) or declared_source != source_hash(node):
            raise ValueError(f"Node source digest mismatch: {node.get('node_id')}")


def verify_artifact(path, declaration):
    declared = declaration.get("hash")
    if not isinstance(declared, str) or not declared:
        raise ValueError(f"Artifact has no declared digest: {declaration.get('artifact_id')}")
    expected = declared.split(":", 1)[1] if declared.startswith("sha256:") else declared
    if len(expected) != 64:
        raise ValueError(f"Artifact digest is not SHA-256: {declaration.get('artifact_id')}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"Artifact digest mismatch: {declaration.get('artifact_id')}")


def skill_files():
    root = SKILL_ROOT.resolve(strict=True)
    if SKILL_ROOT.is_symlink():
        raise ValueError(f"Canonical Skill must not be a symlink: {SKILL_ROOT}")
    files = []
    for candidate in sorted(SKILL_ROOT.rglob("*")):
        relative = candidate.relative_to(SKILL_ROOT)
        if candidate.is_symlink():
            raise ValueError(f"Canonical Skill must not contain symlinks: {relative}")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(f"Canonical Skill path escapes its root: {relative}") from error
        if candidate.is_file():
            files.append((relative, resolved))
        elif not candidate.is_dir():
            raise ValueError(f"Unsupported canonical Skill entry: {relative}")
    if not files:
        raise ValueError("Canonical rootcause-analysis Skill is empty")
    return files


def prompts(trace_path, question):
    common = (
        f"Trace: {trace_path}\n"
        f"Question: {question}\n"
        "只分析并给出改进建议，不得修改任何文件、配置、Trace 或被分析系统。\n"
    )
    return {
        "prompt-claude.md": "/rootcause-analysis\n" + common,
        "prompt-opencode.md": (
            "开始分析前，先调用 skill 工具并传入 name=rootcause-analysis。\n" + common
        ),
    }


def sanitize_trace_identity(trace, opaque_case_id):
    derived = json.loads(json.dumps(trace, ensure_ascii=False))
    derived["manifest"]["run_id"] = opaque_case_id
    derived["manifest"]["case_id"] = opaque_case_id
    for node in derived["nodes"]:
        scope = node.get("scope")
        if not isinstance(scope, dict):
            raise ValueError(f"Canonical node scope is missing: {node.get('node_id')}")
        scope["run_id"] = opaque_case_id
        scope["case_id"] = opaque_case_id
        node["integrity"]["payload_hash"] = canonical_hash(node.get("payload"))
        if "source_hash" in node["integrity"]:
            node["integrity"]["source_hash"] = source_hash(node)
    return derived


def trace_bytes(trace):
    return (json.dumps(trace, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def prepare(case, destination, opaque_case_id=None, agent_trace_path="/workspace/trace.json"):
    questions = case_questions()
    if case not in questions:
        raise ValueError(f"No pressure question is defined for case: {case}")

    opaque_case_id = opaque_case_id or f"case-{uuid.uuid4().hex}"
    if not OPAQUE_CASE_PATTERN.fullmatch(opaque_case_id):
        raise ValueError("Opaque case ID must match case-<32 lowercase hex characters>")
    agent_trace_path = validate_agent_trace_path(agent_trace_path)
    destination = destination.resolve(strict=False)
    if destination.name != opaque_case_id:
        raise ValueError("Agent-visible destination basename must equal the opaque case ID")
    source = FIXTURES / case
    trace_source = fixture_file(source, Path("trace.json"))
    trace = json.loads(trace_source.read_text(encoding="utf-8"))
    validate_trace(trace, case)

    artifacts = []
    for artifact in trace["artifacts"]:
        if not isinstance(artifact, dict):
            raise ValueError("Artifact declaration must be an object")
        relative = relative_artifact_path(artifact.get("path", ""))
        artifact_source = fixture_file(source, relative)
        verify_artifact(artifact_source, artifact)
        artifacts.append((relative, artifact_source))

    canonical_skill = skill_files()
    derived_trace = sanitize_trace_identity(trace, opaque_case_id)
    validate_trace(derived_trace, opaque_case_id)
    derived_bytes = trace_bytes(derived_trace)
    rendered_prompts = prompts(agent_trace_path, questions[case])

    destination.mkdir(parents=True, exist_ok=False)
    (destination / "trace.json").write_bytes(derived_bytes)
    for relative, artifact_source in artifacts:
        artifact_destination = destination / relative
        artifact_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact_source, artifact_destination)
    for relative, skill_source in canonical_skill:
        skill_destination = destination / ".claude" / "skills" / "rootcause-analysis" / relative
        skill_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_source, skill_destination)
    for name, content in rendered_prompts.items():
        (destination / name).write_text(content, encoding="utf-8")
    return {
        "source_fixture": case,
        "opaque_case_id": opaque_case_id,
        "source_trace_sha256": hashlib.sha256(trace_source.read_bytes()).hexdigest(),
        "derived_trace_sha256": hashlib.sha256(derived_bytes).hexdigest(),
        "derivation": {
            "kind": "sanitized_identity_projection",
            "identity_fields": ["manifest.run_id", "manifest.case_id", "nodes[*].scope"],
            "node_integrity_recomputed": True,
        },
    }


def main(argv=None):
    questions = case_questions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(questions), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--opaque-case-id")
    parser.add_argument("--agent-trace-path", default="/workspace/trace.json")
    args = parser.parse_args(argv)
    try:
        provenance = prepare(
            args.case,
            args.destination,
            args.opaque_case_id,
            args.agent_trace_path,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    print(json.dumps(provenance, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
