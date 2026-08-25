#!/usr/bin/env python3
"""Create one no-overwrite, evaluator-free root-cause forward-test workspace."""

import argparse
import hashlib
import json
import os
import re
import stat
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
OWNER_MARKER = ".handoff-owner"
READY_SCHEMA_VERSION = "rootcause-handoff-ready/v1"
RESERVED_ARTIFACT_PATHS = {
    OWNER_MARKER,
    "trace.json",
    "prompt-claude.md",
    "prompt-opencode.md",
}
RESERVED_SKILL_PREFIX = ".claude/skills/rootcause-analysis"
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
PORTABLE_FORBIDDEN_CHARACTERS = set('<>:"\\|?*')


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
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
    ):
        raise ValueError(f"Artifact path must be a portable relative POSIX path: {value}")
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in PORTABLE_FORBIDDEN_CHARACTERS
        for character in value
    ):
        raise ValueError(f"Artifact path contains a non-portable character: {value}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"Artifact path has an ambiguous component: {value}")
    posix_path = PurePosixPath(value)
    if posix_path.is_absolute() or ".." in posix_path.parts or not posix_path.parts:
        raise ValueError(f"Artifact path must stay within the selected fixture: {value}")
    for part in posix_path.parts:
        normalized = unicodedata.normalize("NFKC", part)
        if part in ("", ".", "..") or normalized in ("", ".", ".."):
            raise ValueError(f"Artifact path has an ambiguous component: {value}")
        if part.endswith((".", " ")) or normalized.endswith((".", " ")):
            raise ValueError(f"Artifact path has a non-portable trailing character: {value}")
        device_stem = normalized.split(".", 1)[0].upper()
        if device_stem in WINDOWS_DEVICE_NAMES:
            raise ValueError(f"Artifact path uses a reserved portable device name: {value}")
    return Path(*posix_path.parts)


def portable_path_key(path):
    return tuple(unicodedata.normalize("NFKC", part).casefold() for part in path.parts)


def path_is_prefix(left, right):
    return len(left) <= len(right) and right[: len(left)] == left


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
        if any(
            path_is_prefix(key, reserved) or path_is_prefix(reserved, key)
            for reserved in (*reserved_exact, reserved_prefix)
        ):
            raise ValueError(f"Artifact path is reserved by the bundle: {relative.as_posix()}")
        if key in seen:
            raise ValueError(
                "Artifact path collision after portable normalization: "
                f"{seen[key].as_posix()} and {relative.as_posix()}"
            )
        for previous_key, previous in seen.items():
            if path_is_prefix(key, previous_key) or path_is_prefix(previous_key, key):
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


def directory_open_flags():
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def open_directory_fd(path, label):
    try:
        descriptor = os.open(path, directory_open_flags())
    except OSError as error:
        raise ValueError(
            f"{label} must be a real directory without symlink traversal: {path}"
        ) from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} must be a directory: {path}")
    return descriptor


def validate_relative_components(relative, label):
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(
        part in ("", ".", "..") for part in relative.parts
    ):
        raise ValueError(f"{label} must be a contained relative path: {relative}")
    return relative


def open_parent_at(root_fd, relative, label, create=False):
    relative = validate_relative_components(relative, label)
    current_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    os.fsync(current_fd)
                except FileExistsError:
                    pass
            next_fd = None
            try:
                next_fd = os.open(
                    component,
                    directory_open_flags(),
                    dir_fd=current_fd,
                )
                metadata = os.fstat(next_fd)
            except Exception:
                if next_fd is not None:
                    os.close(next_fd)
                raise
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise ValueError(f"{label} ancestor must be a directory: {relative}")
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, relative.parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def read_regular_file_at(root_fd, relative, label):
    parent_fd, name = open_parent_at(root_fd, relative, label)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        os.close(parent_fd)
        raise ValueError(
            f"{label} must be a regular file without symlink traversal: {relative}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {relative}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)
        os.close(parent_fd)


def read_regular_file_once(root, relative, label):
    root_fd = open_directory_fd(root, f"{label} root")
    try:
        return read_regular_file_at(root_fd, relative, label)
    finally:
        os.close(root_fd)


def read_named_regular_at(directory_fd, name, label, expected=None):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        raise ValueError(f"{label} must not be a symlink: {name}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {name}")
        if expected is not None and (
            metadata.st_dev,
            metadata.st_ino,
        ) != (expected.st_dev, expected.st_ino):
            raise ValueError(f"{label} changed while being snapshotted: {name}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), metadata
    finally:
        os.close(descriptor)


def snapshot_tree_at(root_fd, label):
    directories = []
    files = []

    def walk(directory_fd, prefix):
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            raise ValueError(f"Unable to enumerate {label}: {prefix}") from error
        for name in names:
            relative = prefix / name if prefix.parts else Path(name)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{label} must not contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, directory_open_flags(), dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError(
                            f"{label} directory changed while being snapshotted: {relative}"
                        )
                    directories.append(relative)
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                payload, _ = read_named_regular_at(
                    directory_fd, name, label, expected=metadata
                )
                files.append((relative, payload))
            else:
                raise ValueError(f"{label} contains an unsupported entry: {relative}")

    walk(root_fd, Path())
    return {"directories": directories, "files": files}


def fsync_directory(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_all(descriptor, payload):
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Failed to write complete handoff file")
        view = view[written:]


def write_new_file(path, payload, mode=0o600):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    try:
        write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_file_at(directory_fd, relative, payload, mode=0o600):
    relative = validate_relative_components(Path(relative), "Published file")
    parent_fd, name = open_parent_at(
        directory_fd, relative, "Published file", create=True
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, mode, dir_fd=parent_fd)
        try:
            write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def ensure_directory_at(root_fd, relative):
    relative = validate_relative_components(Path(relative), "Bundle directory")
    current_fd = os.dup(root_fd)
    try:
        for component in relative.parts:
            try:
                os.mkdir(component, 0o700, dir_fd=current_fd)
                os.fsync(current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(component, directory_open_flags(), dir_fd=current_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise ValueError(f"Bundle directory is not a directory: {relative}")
            os.close(current_fd)
            current_fd = next_fd
    finally:
        os.close(current_fd)


def publish_direct_no_replace(path, payload):
    parent_fd = open_directory_fd(path.parent, "Handoff output parent")
    try:
        write_new_file_at(parent_fd, Path(path.name), payload)
        os.fsync(parent_fd)
    except FileExistsError as error:
        raise ValueError(f"Handoff output already exists: {path}") from error
    finally:
        os.close(parent_fd)
    return hashlib.sha256(payload).hexdigest()


def reserve_destination(destination, owner_token):
    try:
        os.mkdir(destination, 0o700)
    except FileExistsError as error:
        raise ValueError(f"Bundle destination already exists; cannot reserve: {destination}") from error
    destination_fd = open_directory_fd(destination, "Reserved bundle destination")
    try:
        write_new_file_at(
            destination_fd,
            OWNER_MARKER,
            (owner_token + "\n").encode("ascii"),
        )
        os.fsync(destination_fd)
    except Exception as error:
        os.close(destination_fd)
        raise OSError(
            f"Bundle destination was reserved but owner marker write failed; "
            f"handoff remains unready: {destination}"
        ) from error
    metadata = os.fstat(destination_fd)
    return destination_fd, (metadata.st_dev, metadata.st_ino)


def assert_reserved_destination(destination, destination_fd, identity):
    opened = os.fstat(destination_fd)
    try:
        named = os.lstat(destination)
    except OSError as error:
        raise ValueError("Reserved destination identity is no longer reachable") from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (opened.st_dev, opened.st_ino) != identity
        or (named.st_dev, named.st_ino) != identity
    ):
        raise ValueError("Reserved destination identity no longer matches destination_fd")


def remove_owner_marker(destination_fd):
    try:
        os.unlink(OWNER_MARKER, dir_fd=destination_fd)
        os.fsync(destination_fd)
    except OSError as error:
        raise OSError(
            "Owner marker removal failed; handoff remains unready and no READY was published"
        ) from error


def validate_snapshot(label, payload):
    with tempfile.TemporaryDirectory(prefix=f"rootcause-{label}-") as directory:
        snapshot = Path(directory) / "trace.json"
        write_new_file(snapshot, payload)
        return authoritative_trace_validation(snapshot)


def emit_phase(hook, phase, context):
    if hook is not None:
        hook(phase, context)


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


def json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def normalized_manifest_path(relative):
    return "/".join(unicodedata.normalize("NFKC", part) for part in relative.parts)


def bundle_tree_manifest_at(destination_fd):
    entries = []
    normalized_paths = {}

    def add_entry(relative, entry):
        normalized = normalized_manifest_path(relative)
        previous = normalized_paths.get(normalized)
        if previous is not None and previous != relative.as_posix():
            raise ValueError(
                f"Bundle tree path normalization collision: {previous} and {relative}"
            )
        normalized_paths[normalized] = relative.as_posix()
        entries.append(
            {
                "type": entry["type"],
                "path": relative.as_posix(),
                "normalized_path": normalized,
                "mode": entry["mode"],
                **entry.get("content", {}),
            }
        )

    def walk(directory_fd, prefix):
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            raise ValueError(f"Unable to enumerate bundle tree: {prefix}") from error
        for name in names:
            relative = prefix / name if prefix.parts else Path(name)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Bundle tree must not contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, directory_open_flags(), dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError(
                            f"Bundle directory changed while hashing: {relative}"
                        )
                    add_entry(relative, {"type": "directory", "mode": mode})
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                content, opened = read_named_regular_at(
                    directory_fd, name, "Bundle tree file", expected=metadata
                )
                add_entry(
                    relative,
                    {
                        "type": "file",
                        "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
                        "content": {
                            "size": len(content),
                            "content_sha256": hashlib.sha256(content).hexdigest(),
                        },
                    },
                )
            else:
                raise ValueError(f"Bundle tree has an unsupported entry: {relative}")

    walk(destination_fd, Path())
    return sorted(entries, key=lambda entry: entry["path"])


def bundle_tree_manifest(destination):
    destination_fd = open_directory_fd(destination, "Bundle destination")
    try:
        return bundle_tree_manifest_at(destination_fd)
    finally:
        os.close(destination_fd)


def bundle_tree_digest(destination):
    return canonical_hash(bundle_tree_manifest(destination))


def bundle_tree_digest_at(destination_fd):
    return canonical_hash(bundle_tree_manifest_at(destination_fd))


def verify_ready_handoff(destination, provenance_output, ready_output):
    destination = Path(destination)
    provenance_output = Path(provenance_output)
    ready_output = Path(ready_output)
    try:
        ready_bytes = read_regular_file_once(
            ready_output.parent, Path(ready_output.name), "READY marker"
        )
    except (OSError, ValueError) as error:
        raise ValueError("Handoff READY marker is missing or invalid")
    try:
        ready = json.loads(ready_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("Handoff READY marker is not valid JSON") from error
    if ready.get("schema_version") != READY_SCHEMA_VERSION:
        raise ValueError("Handoff READY marker has an unsupported schema version")
    opaque_case_id = ready.get("opaque_case_id")
    if opaque_case_id != destination.name or not OPAQUE_CASE_PATTERN.fullmatch(str(opaque_case_id)):
        raise ValueError("Handoff READY marker does not match the opaque destination")
    try:
        provenance_bytes = read_regular_file_once(
            provenance_output.parent,
            Path(provenance_output.name),
            "Evaluator provenance",
        )
    except (OSError, ValueError) as error:
        raise ValueError("Handoff provenance is missing or invalid")
    provenance_digest = hashlib.sha256(provenance_bytes).hexdigest()
    if provenance_digest != ready.get("provenance_sha256"):
        raise ValueError("Handoff provenance digest does not match READY")
    try:
        provenance = json.loads(provenance_bytes)
    except json.JSONDecodeError as error:
        raise ValueError("Handoff provenance is not valid JSON") from error
    if provenance.get("opaque_case_id") != opaque_case_id:
        raise ValueError("Handoff provenance identity does not match READY")
    tree_digest = bundle_tree_digest(destination)
    if tree_digest != ready.get("bundle_tree_sha256"):
        raise ValueError("Handoff bundle tree digest does not match READY")
    if provenance.get("bundle_tree_sha256") != tree_digest:
        raise ValueError("Handoff provenance bundle tree digest does not match READY")
    return {
        "valid": True,
        "opaque_case_id": opaque_case_id,
        "provenance_sha256": provenance_digest,
        "bundle_tree_sha256": tree_digest,
    }


def validate_output_paths(destination, provenance_output, ready_output):
    destination = destination.resolve(strict=False)
    if provenance_output is None:
        raise ValueError("Evaluator provenance output is required")
    if ready_output is None:
        raise ValueError("Evaluator READY output is required")
    provenance_output = Path(provenance_output)
    ready_output = Path(ready_output)
    if not provenance_output.is_absolute():
        raise ValueError("Evaluator provenance output must be an absolute path outside the bundle")
    if not ready_output.is_absolute():
        raise ValueError("Evaluator READY output must be an absolute path outside the bundle")
    provenance_output = provenance_output.resolve(strict=False)
    ready_output = ready_output.resolve(strict=False)
    for label, output in (("provenance", provenance_output), ("READY", ready_output)):
        try:
            output.relative_to(destination)
        except ValueError:
            pass
        else:
            raise ValueError(f"Evaluator {label} output must stay outside the Agent bundle")
    if provenance_output == ready_output:
        raise ValueError("Evaluator provenance and READY outputs must be distinct")
    if provenance_output.exists():
        raise ValueError(f"Evaluator provenance output already exists: {provenance_output}")
    if ready_output.exists():
        raise ValueError(f"Evaluator READY output already exists: {ready_output}")
    if not destination.parent.is_dir():
        raise ValueError(f"Bundle destination parent does not exist: {destination.parent}")
    if not provenance_output.parent.is_dir():
        raise ValueError(f"Evaluator provenance parent does not exist: {provenance_output.parent}")
    if not ready_output.parent.is_dir():
        raise ValueError(f"Evaluator READY parent does not exist: {ready_output.parent}")
    return destination, provenance_output, ready_output


def fsync_bundle_directories_at(destination_fd):
    def walk(directory_fd):
        for name in sorted(os.listdir(directory_fd)):
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"Bundle tree must not contain symlinks: {name}")
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, directory_open_flags(), dir_fd=directory_fd)
                try:
                    walk(child_fd)
                finally:
                    os.close(child_fd)
            elif not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"Bundle tree has an unsupported entry: {name}")
        os.fsync(directory_fd)

    walk(destination_fd)


def verify_artifact_bytes(payload, declaration, expected=None):
    expected = expected or declared_artifact_digest(declaration)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"Artifact digest mismatch: {declaration.get('artifact_id')}")
    return actual


def prepare(
    case,
    destination,
    opaque_case_id=None,
    agent_trace_path="/workspace/trace.json",
    provenance_output=None,
    ready_output=None,
    _phase_hook=None,
):
    questions = case_questions()
    if case not in questions:
        raise ValueError(f"No pressure question is defined for case: {case}")

    opaque_case_id = opaque_case_id or f"case-{uuid.uuid4().hex}"
    if not OPAQUE_CASE_PATTERN.fullmatch(opaque_case_id):
        raise ValueError("Opaque case ID must match case-<32 lowercase hex characters>")
    agent_trace_path = validate_agent_trace_path(agent_trace_path)
    destination, provenance_output, ready_output = validate_output_paths(
        destination, provenance_output, ready_output
    )
    if destination.name != opaque_case_id:
        raise ValueError("Agent-visible destination basename must equal the opaque case ID")
    owner_token = uuid.uuid4().hex
    destination_fd, destination_identity = reserve_destination(destination, owner_token)
    context = {
        "opaque_case_id": opaque_case_id,
        "destination": str(destination),
        "provenance_output": str(provenance_output),
        "ready_output": str(ready_output),
    }
    try:
        emit_phase(_phase_hook, "destination_reserved", context)

        source = FIXTURES / case
        source_fd = open_directory_fd(source, "Selected fixture root")
        try:
            source_trace_bytes = read_regular_file_at(
                source_fd, Path("trace.json"), "Source Trace"
            )
            source_validation = validate_snapshot("source-trace", source_trace_bytes)
            trace = json.loads(source_trace_bytes)
            validate_trace_identity_and_integrity(trace, case)
            emit_phase(_phase_hook, "source_validated", context)

            artifacts = []
            for artifact, relative in validate_artifact_paths(trace["artifacts"]):
                artifact_bytes = read_regular_file_at(
                    source_fd, relative, "Artifact source"
                )
                expected_digest = declared_artifact_digest(artifact)
                verify_artifact_bytes(artifact_bytes, artifact, expected_digest)
                artifacts.append((artifact, relative, artifact_bytes, expected_digest))
        finally:
            os.close(source_fd)

        skill_root_fd = open_directory_fd(SKILL_ROOT, "Canonical Skill root")
        try:
            canonical_skill = snapshot_tree_at(skill_root_fd, "Canonical Skill")
        finally:
            os.close(skill_root_fd)
        if not canonical_skill["files"]:
            raise ValueError("Canonical rootcause-analysis Skill is empty")
        emit_phase(_phase_hook, "skill_snapshotted", context)
        derived_trace = sanitize_trace_identity(trace, opaque_case_id)
        validate_trace_identity_and_integrity(derived_trace, opaque_case_id)
        derived_bytes = trace_bytes(derived_trace)
        rendered_prompts = prompts(agent_trace_path, questions[case])

        write_new_file_at(destination_fd, Path("trace.json"), derived_bytes)
        for _, relative, artifact_bytes, _ in artifacts:
            write_new_file_at(destination_fd, relative, artifact_bytes)
        skill_prefix = Path(".claude/skills/rootcause-analysis")
        ensure_directory_at(destination_fd, skill_prefix)
        for relative in canonical_skill["directories"]:
            ensure_directory_at(destination_fd, skill_prefix / relative)
        for relative, skill_bytes in canonical_skill["files"]:
            write_new_file_at(destination_fd, skill_prefix / relative, skill_bytes)
        for name, content in rendered_prompts.items():
            write_new_file_at(destination_fd, Path(name), content.encode("utf-8"))

        final_trace_bytes = read_regular_file_at(
            destination_fd, Path("trace.json"), "Published derived Trace"
        )
        if final_trace_bytes != derived_bytes:
            raise ValueError("Published derived Trace bytes changed after validation")
        derived_validation = validate_snapshot("derived-trace", final_trace_bytes)
        artifact_provenance = []
        for artifact, relative, artifact_bytes, expected_digest in artifacts:
            copied_bytes = read_regular_file_at(destination_fd, relative, "Bundle artifact")
            if copied_bytes != artifact_bytes:
                raise ValueError(
                    f"Artifact content changed after write: {artifact.get('artifact_id')}"
                )
            content_sha256 = verify_artifact_bytes(
                copied_bytes, artifact, expected_digest
            )
            artifact_provenance.append(
                {
                    "artifact_id": artifact.get("artifact_id"),
                    "path": relative.as_posix(),
                    "content_sha256": content_sha256,
                }
            )
        fsync_bundle_directories_at(destination_fd)
        assert_reserved_destination(
            destination, destination_fd, destination_identity
        )
        remove_owner_marker(destination_fd)
        tree_digest = bundle_tree_digest_at(destination_fd)
        emit_phase(_phase_hook, "bundle_written", context)
        provenance = {
            "source_fixture": case,
            "opaque_case_id": opaque_case_id,
            "source_trace_sha256": hashlib.sha256(source_trace_bytes).hexdigest(),
            "derived_trace_sha256": hashlib.sha256(derived_bytes).hexdigest(),
            "bundle_tree_sha256": tree_digest,
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
        provenance_bytes = json_bytes(provenance)
        assert_reserved_destination(
            destination, destination_fd, destination_identity
        )
        provenance_digest = publish_direct_no_replace(
            provenance_output, provenance_bytes
        )
        emit_phase(_phase_hook, "provenance_published", context)

        ready = {
            "schema_version": READY_SCHEMA_VERSION,
            "opaque_case_id": opaque_case_id,
            "provenance_sha256": provenance_digest,
            "bundle_tree_sha256": tree_digest,
        }
        assert_reserved_destination(
            destination, destination_fd, destination_identity
        )
        publish_direct_no_replace(ready_output, json_bytes(ready))
    finally:
        os.close(destination_fd)
    emit_phase(_phase_hook, "ready_published", context)
    return provenance


def main(argv=None):
    questions = case_questions()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(questions))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--opaque-case-id")
    parser.add_argument("--agent-trace-path", default="/workspace/trace.json")
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--ready-output", type=Path, required=True)
    parser.add_argument("--verify-ready-handoff", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_ready_handoff:
            result = verify_ready_handoff(
                args.destination, args.provenance_output, args.ready_output
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            if args.case is None:
                parser.error("--case is required when building a handoff")
            prepare(
                args.case,
                args.destination,
                args.opaque_case_id,
                args.agent_trace_path,
                provenance_output=args.provenance_output,
                ready_output=args.ready_output,
            )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
