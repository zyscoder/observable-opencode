#!/usr/bin/env python3
"""Create one no-overwrite, evaluator-free root-cause forward-test workspace."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tools" / "rootcause_skill_tests" / "fixtures"
CASES_PATH = Path(__file__).with_name("cases.json")
SKILL_ROOT = ROOT / ".claude" / "skills" / "rootcause-analysis"
TRACE_QUERY = SKILL_ROOT / "scripts" / "trace_query.py"
OPAQUE_CASE_PATTERN = re.compile(r"case-[0-9a-f]{32}\Z")
RESERVED_ARTIFACT_PATHS = {
    "trace.json",
    "prompt-claude.md",
    "prompt-opencode.md",
}
RESERVED_SKILL_PREFIX = ".claude/skills/rootcause-analysis"


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
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"Artifact path must be a portable relative POSIX path: {value}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Artifact path contains a control character: {value}")
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() or ".." in posix_path.parts or not posix_path.parts:
        raise ValueError(f"Artifact path must stay within the selected fixture: {value}")
    if any(part in ("", ".") for part in posix_path.parts):
        raise ValueError(f"Artifact path must be canonical: {value}")
    return Path(*posix_path.parts)


def portable_path_key(path):
    return unicodedata.normalize("NFKC", path.as_posix()).casefold()


def validate_artifact_paths(declarations):
    reserved_exact = {portable_path_key(Path(path)) for path in RESERVED_ARTIFACT_PATHS}
    reserved_prefix = portable_path_key(Path(RESERVED_SKILL_PREFIX))
    seen = {}
    validated = []
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise ValueError("Artifact declaration must be an object")
        relative = relative_artifact_path(declaration.get("path", ""))
        key = portable_path_key(relative)
        if key in reserved_exact or key == reserved_prefix or key.startswith(reserved_prefix + "/"):
            raise ValueError(f"Artifact path is reserved by the bundle: {relative.as_posix()}")
        if key in seen:
            raise ValueError(
                "Artifact path collision after portable normalization: "
                f"{seen[key].as_posix()} and {relative.as_posix()}"
            )
        for previous_key, previous in seen.items():
            if key.startswith(previous_key + "/") or previous_key.startswith(key + "/"):
                raise ValueError(
                    "Artifact file/directory path collision: "
                    f"{previous.as_posix()} and {relative.as_posix()}"
                )
        seen[key] = relative
        validated.append((declaration, relative))
    return validated


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


def authoritative_trace_validation(path):
    result = subprocess.run(
        [sys.executable, str(TRACE_QUERY), "validate", "--trace", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown validation error"
        raise ValueError(f"Authoritative finalized Trace validation failed: {detail}")
    try:
        validation = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Authoritative finalized Trace validation returned invalid JSON") from error
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        raise ValueError("Authoritative finalized Trace validation did not confirm validity")
    return validation


def validate_trace_identity_and_integrity(trace, case):
    if not isinstance(trace, dict):
        raise ValueError("Trace must be a JSON object")
    manifest = trace.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("Trace manifest is missing")
    if manifest.get("case_id") != case:
        raise ValueError(f"Trace case_id does not match selected case: {case}")
    nodes = trace.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("Bundle handoff requires canonical Trace nodes")
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("integrity"), dict):
            raise ValueError("Canonical node integrity metadata is missing")
        integrity = node["integrity"]
        declared_payload = integrity.get("payload_hash")
        if declared_payload != canonical_hash(node.get("payload")):
            raise ValueError(f"Node payload digest mismatch: {node.get('node_id')}")
        declared_source = integrity.get("source_hash")
        if not isinstance(declared_source, str) or declared_source != source_hash(node):
            raise ValueError(f"Node source digest mismatch: {node.get('node_id')}")


def normalize_sha256(value, artifact_id, field):
    if not isinstance(value, str) or not value:
        return ""
    digest = value.split(":", 1)[1] if value.startswith("sha256:") else value
    if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
        raise ValueError(f"Artifact {artifact_id} has an invalid {field} SHA-256 digest")
    return digest.casefold()


def declared_artifact_digest(declaration):
    artifact_id = declaration.get("artifact_id")
    digests = {
        field: normalize_sha256(declaration.get(field), artifact_id, field)
        for field in ("hash", "content_hash", "content_sha256")
        if field in declaration
    }
    declared = {digest for digest in digests.values() if digest}
    if not declared:
        raise ValueError(f"Artifact has no declared SHA-256 digest: {artifact_id}")
    if len(declared) != 1:
        raise ValueError(f"Artifact has conflicting SHA-256 declarations: {artifact_id}")
    return next(iter(declared))


def verify_artifact(path, declaration, expected=None, postwrite=False):
    expected = expected or declared_artifact_digest(declaration)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        phase = "content SHA-256" if postwrite else "digest"
        raise ValueError(f"Artifact {phase} mismatch: {declaration.get('artifact_id')}")
    return actual


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


def validate_output_paths(destination, provenance_output):
    destination = destination.resolve(strict=False)
    if provenance_output is None:
        raise ValueError("Evaluator provenance output is required")
    provenance_output = Path(provenance_output)
    if not provenance_output.is_absolute():
        raise ValueError("Evaluator provenance output must be an absolute path outside the bundle")
    provenance_output = provenance_output.resolve(strict=False)
    try:
        provenance_output.relative_to(destination)
    except ValueError:
        pass
    else:
        raise ValueError("Evaluator provenance output must stay outside the Agent bundle")
    if destination.exists():
        raise ValueError(f"Bundle destination already exists: {destination}")
    if provenance_output.exists():
        raise ValueError(f"Evaluator provenance output already exists: {provenance_output}")
    if not destination.parent.is_dir():
        raise ValueError(f"Bundle destination parent does not exist: {destination.parent}")
    if not provenance_output.parent.is_dir():
        raise ValueError(f"Evaluator provenance parent does not exist: {provenance_output.parent}")
    return destination, provenance_output


def prepare(
    case,
    destination,
    opaque_case_id=None,
    agent_trace_path="/workspace/trace.json",
    provenance_output=None,
):
    questions = case_questions()
    if case not in questions:
        raise ValueError(f"No pressure question is defined for case: {case}")

    opaque_case_id = opaque_case_id or f"case-{uuid.uuid4().hex}"
    if not OPAQUE_CASE_PATTERN.fullmatch(opaque_case_id):
        raise ValueError("Opaque case ID must match case-<32 lowercase hex characters>")
    agent_trace_path = validate_agent_trace_path(agent_trace_path)
    destination, provenance_output = validate_output_paths(destination, provenance_output)
    if destination.name != opaque_case_id:
        raise ValueError("Agent-visible destination basename must equal the opaque case ID")
    source = FIXTURES / case
    trace_source = fixture_file(source, Path("trace.json"))
    source_validation = authoritative_trace_validation(trace_source)
    trace = json.loads(trace_source.read_text(encoding="utf-8"))
    validate_trace_identity_and_integrity(trace, case)

    artifacts = []
    for artifact, relative in validate_artifact_paths(trace["artifacts"]):
        artifact_source = fixture_file(source, relative)
        expected_digest = declared_artifact_digest(artifact)
        verify_artifact(artifact_source, artifact, expected_digest)
        artifacts.append((artifact, relative, artifact_source, expected_digest))

    canonical_skill = skill_files()
    derived_trace = sanitize_trace_identity(trace, opaque_case_id)
    validate_trace_identity_and_integrity(derived_trace, opaque_case_id)
    derived_bytes = trace_bytes(derived_trace)
    rendered_prompts = prompts(agent_trace_path, questions[case])
    staging = Path(
        tempfile.mkdtemp(prefix=f".{opaque_case_id}.tmp-", dir=str(destination.parent))
    )
    published = False
    provenance_created = False
    try:
        (staging / "trace.json").write_bytes(derived_bytes)
        for _, relative, artifact_source, _ in artifacts:
            artifact_destination = staging / relative
            artifact_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact_source, artifact_destination)
        for relative, skill_source in canonical_skill:
            skill_destination = staging / ".claude" / "skills" / "rootcause-analysis" / relative
            skill_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(skill_source, skill_destination)
        for name, content in rendered_prompts.items():
            (staging / name).write_text(content, encoding="utf-8")

        derived_validation = authoritative_trace_validation(staging / "trace.json")
        staged_trace = json.loads((staging / "trace.json").read_text(encoding="utf-8"))
        validate_trace_identity_and_integrity(staged_trace, opaque_case_id)
        artifact_provenance = []
        for artifact, relative, _, expected_digest in artifacts:
            copied = regular_file(staging, relative, "Bundle artifact")
            content_sha256 = verify_artifact(
                copied, artifact, expected_digest, postwrite=True
            )
            artifact_provenance.append(
                {
                    "artifact_id": artifact.get("artifact_id"),
                    "path": relative.as_posix(),
                    "content_sha256": content_sha256,
                }
            )

        provenance = {
            "source_fixture": case,
            "opaque_case_id": opaque_case_id,
            "source_trace_sha256": hashlib.sha256(trace_source.read_bytes()).hexdigest(),
            "derived_trace_sha256": hashlib.sha256(derived_bytes).hexdigest(),
            "source_completeness": source_validation["recovery"],
            "lifecycle": source_validation["lifecycle"],
            "segments": source_validation["segments"],
            "diagnostics": source_validation["diagnostics"],
            "artifacts": artifact_provenance,
            "derivation": {
                "kind": "sanitized_identity_projection",
                "identity_fields": ["manifest.run_id", "manifest.case_id", "nodes[*].scope"],
                "node_integrity_recomputed": True,
                "derived_trace_validation": derived_validation,
            },
        }

        staging.rename(destination)
        published = True
        try:
            with provenance_output.open("x", encoding="utf-8") as stream:
                provenance_created = True
                json.dump(provenance, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
        except Exception:
            if provenance_created:
                provenance_output.unlink(missing_ok=True)
            shutil.rmtree(destination)
            published = False
            raise
        return provenance
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if published and destination.exists():
            shutil.rmtree(destination)
        if provenance_created and provenance_output.exists():
            provenance_output.unlink()
        raise


def main(argv=None):
    questions = case_questions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(questions), required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--opaque-case-id")
    parser.add_argument("--agent-trace-path", default="/workspace/trace.json")
    parser.add_argument("--provenance-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepare(
            args.case,
            args.destination,
            args.opaque_case_id,
            args.agent_trace_path,
            provenance_output=args.provenance_output,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
