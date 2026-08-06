"""Read-only validation for immutable benchmark Trace bundles."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .evaluation_facts import trace_execution_revision
from .models import JsonDict, stable_json
from .strict_json import load_json_object


BUNDLE_SCHEMA_VERSION = "benchmark-trace-bundle/v1"
COMPOSITION_CONTRACT = "benchmark-trace-composition/v1"
SOURCE_KEYS = frozenset({"trace", "review", "evaluations", "labels"})
TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "bundle_id",
        "composition_contract",
        "case_id",
        "run_id",
        "subject_revision",
        "revision_provenance_status",
        "sources",
        "effective_trace",
        "artifacts",
        "members",
        "counts",
    }
)
BINDING_KEYS = frozenset({"path", "sha256", "byte_length", "media_type"})
EVALUATION_BINDING_KEYS = BINDING_KEYS | {"source_index"}
MEMBER_KEYS = BINDING_KEYS | {"role"}
ARTIFACT_KEYS = frozenset(
    {
        "artifact_id",
        "source_relative_path",
        "declared_content_hash",
        "member_path",
        "content_sha256",
        "byte_length",
        "media_type",
    }
)
COUNT_KEYS = frozenset(
    {
        "source_count",
        "evaluation_count",
        "declared_artifact_count",
        "referenced_artifact_count",
        "artifact_member_count",
        "member_count",
    }
)
MEMBER_ROLES = frozenset(
    {
        "source_trace",
        "source_review",
        "source_evaluation",
        "source_labels",
        "effective_trace",
        "artifact",
    }
)
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
DECLARED_HASH_PATTERN = re.compile(
    r"(?:sha256:)?(?:[0-9a-f]{8}|[0-9a-f]{16}|[0-9a-f]{64})\Z"
)


@dataclass(frozen=True)
class BenchmarkBundleSource:
    path: str
    sha256: str
    byte_length: int
    media_type: str
    source_index: Optional[int] = None


@dataclass(frozen=True)
class BenchmarkBundleMember:
    role: str
    path: str
    sha256: str
    byte_length: int
    media_type: str


@dataclass(frozen=True)
class BenchmarkBundleArtifact:
    artifact_id: str
    source_relative_path: str
    declared_content_hash: str
    member_path: str
    content_sha256: str
    byte_length: int
    media_type: str


@dataclass(frozen=True)
class BenchmarkTraceBundle:
    root: Path
    artifact_root: Path
    bundle_id: str
    case_id: str
    run_id: str
    subject_revision: str
    role: str
    trace_source: BenchmarkBundleSource
    review_source: Optional[BenchmarkBundleSource]
    evaluation_sources: Tuple[BenchmarkBundleSource, ...]
    labels_source: Optional[BenchmarkBundleSource]
    effective_trace_source: BenchmarkBundleSource
    members: Tuple[BenchmarkBundleMember, ...]
    artifacts: Tuple[BenchmarkBundleArtifact, ...]
    trace_path: Path
    review_path: Optional[Path]
    evaluation_paths: Tuple[Path, ...]
    labels_path: Optional[Path]
    effective_trace_path: Path
    trace_bytes: bytes = field(repr=False)
    review_bytes: Optional[bytes] = field(repr=False)
    evaluation_bytes: Tuple[bytes, ...] = field(repr=False)
    labels_bytes: Optional[bytes] = field(repr=False)
    effective_trace_bytes: bytes = field(repr=False)
    effective_trace: Mapping[str, Any]
    _artifact_contents: Tuple[Tuple[str, bytes], ...] = field(repr=False)

    def new_graph(self) -> Any:
        """Return a fresh working graph over this bundle's verified snapshot."""

        from .artifact_reader import PinnedVerifiedArtifactReader
        from .graph import TraceGraph

        trace = _thaw_json(self.effective_trace)
        artifact_index = {
            str(item["artifact_id"]): copy.deepcopy(item)
            for item in trace.get("artifacts", [])
        }
        reader = PinnedVerifiedArtifactReader(
            artifact_index,
            {artifact_id: content for artifact_id, content in self._artifact_contents},
        )
        return TraceGraph.from_trace(
            trace,
            artifact_root=self.artifact_root,
            artifact_reader=reader,
        )

    @property
    def graph(self) -> Any:
        """Compatibility view; each access is isolated from prior mutations."""

        return self.new_graph()


@dataclass(frozen=True)
class _BuilderArtifact:
    artifact_id: str
    source_relative_path: str
    declared_content_hash: str
    member_path: str
    content_sha256: str
    byte_length: int
    media_type: str
    content: bytes


@dataclass
class _PinnedBundleRoot:
    descriptors: Tuple[int, ...]
    links: Tuple[Tuple[int, str, int], ...]
    identities: Tuple[Tuple[int, int], ...]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def verify(self) -> None:
        for descriptor, expected in zip(self.descriptors, self.identities):
            descriptor_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(descriptor_stat.st_mode)
                or _file_identity(descriptor_stat) != expected
            ):
                raise ValueError("bundle root directory changed during validation")
        for parent, component, child in self.links:
            try:
                linked = os.stat(
                    component,
                    dir_fd=parent,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise ValueError("bundle root ancestry changed") from error
            child_stat = os.fstat(child)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or (linked.st_dev, linked.st_ino)
                != (child_stat.st_dev, child_stat.st_ino)
            ):
                raise ValueError("bundle root ancestry changed")

    def close(self) -> None:
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def build_benchmark_trace_bundle(
    *,
    trace_path: Path,
    store_dir: Path,
    artifact_root: Path,
    review_path: Optional[Path] = None,
    evaluation_paths: Sequence[Path] = (),
    labels_path: Optional[Path] = None,
) -> BenchmarkTraceBundle:
    """Build and atomically publish one complete content-addressed bundle."""

    trace_path = Path(trace_path)
    store_dir = Path(store_dir)
    artifact_root = Path(artifact_root)
    review_path = Path(review_path) if review_path is not None else None
    labels_path = Path(labels_path) if labels_path is not None else None
    evaluation_paths = tuple(Path(path) for path in evaluation_paths)

    source_files: List[Tuple[Path, bytes]] = []
    trace_bytes = _read_source_file(trace_path, "source Trace")
    source_files.append((trace_path, trace_bytes))
    trace = _parse_json_object(trace_bytes, "source Trace")

    review_bytes = None
    review = None
    if review_path is not None:
        review_bytes = _read_source_file(review_path, "quality review")
        source_files.append((review_path, review_bytes))
        review = _parse_json_object(review_bytes, "quality review")

    evaluation_bytes = tuple(
        _read_source_file(path, "evaluation {0}".format(index))
        for index, path in enumerate(evaluation_paths)
    )
    source_files.extend(zip(evaluation_paths, evaluation_bytes))
    evaluations = tuple(
        _parse_json_object(data, "evaluation {0}".format(index))
        for index, data in enumerate(evaluation_bytes)
    )

    labels_bytes = None
    if labels_path is not None:
        labels_bytes = _read_source_file(labels_path, "labels")
        source_files.append((labels_path, labels_bytes))
        _parse_json_object(labels_bytes, "labels")

    from .benchmark_composition import compose_effective_trace
    from .graph import collect_artifact_ids

    effective_trace = compose_effective_trace(
        trace,
        review=review,
        evaluations=evaluations,
    )
    manifest = effective_trace.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("effective Trace manifest is missing")
    case_id = _require_nonempty_string(manifest.get("case_id"), "Trace case_id")
    run_id = _require_nonempty_string(manifest.get("run_id"), "Trace run_id")
    subject_revision, provenance_status = trace_execution_revision(effective_trace)
    _require_nonempty_string(subject_revision, "Trace subject_revision")
    if provenance_status != "valid":
        raise ValueError("Trace subject revision provenance must be valid")

    referenced_artifact_ids: Set[str] = set()
    records = effective_trace.get("records", [])
    if not isinstance(records, list):
        raise ValueError("effective Trace records must be a list")
    for record in records:
        if not isinstance(record, dict):
            continue
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        referenced_artifact_ids.update(collect_artifact_ids(record, data))

    declarations = effective_trace.get("artifacts", [])
    if not isinstance(declarations, list):
        raise ValueError("effective Trace artifacts must be a list")
    builder_artifacts: List[_BuilderArtifact] = []
    normalized_declarations: List[JsonDict] = []
    artifact_ids: Set[str] = set()
    source_relative_paths: Set[str] = set()
    artifact_handle = _open_bundle_root(artifact_root)
    try:
        for index, declaration_value in enumerate(declarations):
            label = "effective Trace artifact {0}".format(index)
            if not isinstance(declaration_value, dict):
                raise ValueError("{0} must be an object".format(label))
            declaration = copy.deepcopy(declaration_value)
            artifact_id = _require_nonempty_string(
                declaration.get("artifact_id"),
                label + ".artifact_id",
            )
            if artifact_id in artifact_ids:
                raise ValueError("effective Trace artifact IDs must be unique")
            artifact_ids.add(artifact_id)
            source_relative_path = _validate_relative_path(
                declaration.get("path"),
                label + ".path",
            )
            if source_relative_path in source_relative_paths:
                raise ValueError("effective Trace artifact source paths must be unique")
            source_relative_paths.add(source_relative_path)
            if artifact_id not in referenced_artifact_ids:
                continue
            declared_content_hash = declaration.get("content_hash")
            if (
                not isinstance(declared_content_hash, str)
                or not declared_content_hash
                or DECLARED_HASH_PATTERN.fullmatch(declared_content_hash) is None
            ):
                raise ValueError("{0}.content_hash is invalid".format(label))
            declared_byte_length = _require_nonnegative_integer(
                declaration.get("byte_length"),
                label + ".byte_length",
            )
            media_type = _artifact_media_type(declaration, source_relative_path, label)
            declaration["media_type"] = media_type
            content = _read_source_artifact(artifact_handle, source_relative_path)
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("{0} is not valid UTF-8".format(label)) from error
            content_sha256 = _sha256(content)
            normalized_declared_hash = declared_content_hash.removeprefix("sha256:")
            if not content_sha256.removeprefix("sha256:").startswith(
                normalized_declared_hash
            ):
                raise ValueError("{0} content hash mismatch".format(label))
            if len(content) != declared_byte_length:
                raise ValueError("{0} byte length mismatch".format(label))
            digest_hex = content_sha256.removeprefix("sha256:")
            member_path = "artifacts/sha256/{0}/{1}".format(
                digest_hex[:2],
                digest_hex,
            )
            declaration["path"] = member_path
            declaration["content_hash"] = content_sha256
            normalized_declarations.append(declaration)
            builder_artifacts.append(
                _BuilderArtifact(
                    artifact_id=artifact_id,
                    source_relative_path=source_relative_path,
                    declared_content_hash=declared_content_hash,
                    member_path=member_path,
                    content_sha256=content_sha256,
                    byte_length=len(content),
                    media_type=media_type,
                    content=content,
                )
            )

        if referenced_artifact_ids != {
            artifact.artifact_id for artifact in builder_artifacts
        }:
            raise ValueError("artifact references do not close over declarations")
        artifact_handle.verify()
        for artifact in builder_artifacts:
            if (
                _read_source_artifact(
                    artifact_handle,
                    artifact.source_relative_path,
                )
                != artifact.content
            ):
                raise ValueError("source artifact changed before bundle publication")
        artifact_handle.verify()
    finally:
        artifact_handle.close()

    effective_trace = copy.deepcopy(effective_trace)
    if normalized_declarations or "artifacts" in effective_trace:
        effective_trace["artifacts"] = normalized_declarations
    effective_bytes = stable_json(effective_trace).encode("utf-8")

    member_payloads: Dict[str, Tuple[str, str, bytes]] = {}

    def add_member(path: str, role: str, media_type: str, data: bytes) -> None:
        prior = member_payloads.get(path)
        value = (role, media_type, bytes(data))
        if prior is not None and prior != value:
            raise ValueError("bundle member path collision: {0}".format(path))
        member_payloads[path] = value

    add_member("inputs/trace.json", "source_trace", "application/json", trace_bytes)
    if review_bytes is not None:
        add_member(
            "inputs/review.json",
            "source_review",
            "application/json",
            review_bytes,
        )
    evaluation_bindings = []
    for index, data in enumerate(evaluation_bytes):
        digest = _sha256(data)
        path = "inputs/evaluations/{0:04d}-{1}.json".format(
            index,
            digest.removeprefix("sha256:"),
        )
        add_member(path, "source_evaluation", "application/json", data)
        evaluation_bindings.append(
            _manifest_binding(path, data, source_index=index)
        )
    if labels_bytes is not None:
        add_member(
            "inputs/labels.json",
            "source_labels",
            "application/json",
            labels_bytes,
        )
    add_member(
        "effective/trace.json",
        "effective_trace",
        "application/json",
        effective_bytes,
    )
    for artifact in builder_artifacts:
        add_member(
            artifact.member_path,
            "artifact",
            artifact.media_type,
            artifact.content,
        )

    members = [
        {
            "role": role,
            **_manifest_binding(path, data, media_type=media_type),
        }
        for path, (role, media_type, data) in sorted(member_payloads.items())
    ]
    artifact_manifest = [
        {
            "artifact_id": artifact.artifact_id,
            "source_relative_path": artifact.source_relative_path,
            "declared_content_hash": artifact.declared_content_hash,
            "member_path": artifact.member_path,
            "content_sha256": artifact.content_sha256,
            "byte_length": artifact.byte_length,
            "media_type": artifact.media_type,
        }
        for artifact in builder_artifacts
    ]
    manifest_value: JsonDict = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": "",
        "composition_contract": COMPOSITION_CONTRACT,
        "case_id": case_id,
        "run_id": run_id,
        "subject_revision": subject_revision,
        "revision_provenance_status": provenance_status,
        "sources": {
            "trace": _manifest_binding("inputs/trace.json", trace_bytes),
            "review": (
                _manifest_binding("inputs/review.json", review_bytes)
                if review_bytes is not None
                else None
            ),
            "evaluations": evaluation_bindings,
            "labels": (
                _manifest_binding("inputs/labels.json", labels_bytes)
                if labels_bytes is not None
                else None
            ),
        },
        "effective_trace": _manifest_binding(
            "effective/trace.json",
            effective_bytes,
        ),
        "artifacts": artifact_manifest,
        "members": members,
        "counts": {
            "source_count": (
                1
                + int(review_bytes is not None)
                + len(evaluation_bytes)
                + int(labels_bytes is not None)
            ),
            "evaluation_count": len(evaluation_bytes),
            "declared_artifact_count": len(builder_artifacts),
            "referenced_artifact_count": len(referenced_artifact_ids),
            "artifact_member_count": len(
                {artifact.member_path for artifact in builder_artifacts}
            ),
            "member_count": len(members),
        },
    }
    identity_manifest = copy.deepcopy(manifest_value)
    del identity_manifest["bundle_id"]
    bundle_id = _sha256(stable_json(identity_manifest).encode("utf-8"))
    manifest_value["bundle_id"] = bundle_id
    manifest_bytes = stable_json(manifest_value).encode("utf-8")

    for source_path, expected in source_files:
        if _read_source_file(source_path, "source input") != expected:
            raise ValueError("source input changed before bundle publication")

    namespace = Path(os.path.abspath(os.fspath(store_dir / "sha256")))
    namespace_handle = _open_or_create_bundle_root(namespace)
    staging_name = ".staging-{0}".format(secrets.token_hex(16))
    target_name = bundle_id.removeprefix("sha256:")
    staging_created = False
    staging_identity: Optional[Tuple[int, int]] = None
    try:
        try:
            os.mkdir(staging_name, 0o700, dir_fd=namespace_handle.descriptor)
        except OSError as error:
            raise ValueError("benchmark bundle staging creation failed") from error
        staging_created = True
        staging_stat = os.stat(
            staging_name,
            dir_fd=namespace_handle.descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISDIR(staging_stat.st_mode):
            raise ValueError("benchmark bundle staging directory is unsafe")
        staging_identity = _file_identity(staging_stat)
        for relative_path, (_role, _media_type, data) in member_payloads.items():
            _write_bundle_member(
                namespace_handle.descriptor,
                staging_name,
                relative_path,
                data,
            )
        _write_bundle_member(
            namespace_handle.descriptor,
            staging_name,
            "bundle.json",
            manifest_bytes,
        )
        staging = namespace / staging_name
        open_benchmark_trace_bundle(
            staging,
            role="evaluation" if labels_bytes is not None else "attribution",
        )
        namespace_handle.verify()
        linked_staging = os.stat(
            staging_name,
            dir_fd=namespace_handle.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(linked_staging.st_mode)
            or _file_identity(linked_staging) != _file_identity(staging_stat)
        ):
            raise ValueError("benchmark bundle staging changed before publication")
        try:
            os.rename(
                staging_name,
                target_name,
                src_dir_fd=namespace_handle.descriptor,
                dst_dir_fd=namespace_handle.descriptor,
            )
        except OSError as error:
            try:
                existing = open_benchmark_trace_bundle(
                    namespace / target_name,
                    role=(
                        "evaluation"
                        if labels_bytes is not None
                        else "attribution"
                    ),
                )
            except ValueError:
                raise ValueError("benchmark bundle publication failed") from error
            if existing.bundle_id != bundle_id:
                raise ValueError("benchmark bundle identity collision") from error
            return existing
        staging_created = False
        target = namespace / target_name
        try:
            published_stat = os.stat(
                target_name,
                dir_fd=namespace_handle.descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(published_stat.st_mode)
                or _file_identity(published_stat) != _file_identity(staging_stat)
            ):
                raise ValueError("published benchmark bundle identity changed")
            return open_benchmark_trace_bundle(
                target,
                role="evaluation" if labels_bytes is not None else "attribution",
            )
        except (OSError, ValueError) as error:
            _rollback_published_bundle(
                namespace_handle.descriptor,
                target_name,
                _file_identity(staging_stat),
            )
            raise ValueError(
                "published benchmark bundle failed verification"
            ) from error
    finally:
        if staging_created:
            if staging_identity is not None:
                _remove_tree_entry(
                    namespace_handle.descriptor,
                    staging_name,
                    staging_identity,
                )
        namespace_handle.close()


def open_benchmark_trace_bundle(
    path: Path,
    *,
    verify: str = "full",
    role: str = "attribution",
) -> BenchmarkTraceBundle:
    """Open a fully verified bundle without mutating it or inferring roots."""

    if verify != "full":
        raise ValueError("benchmark bundles support only verify='full'")
    if not isinstance(role, str) or role not in {"attribution", "evaluation"}:
        raise ValueError("benchmark bundle role must be attribution or evaluation")
    root = Path(path)
    pinned_root = _open_bundle_root(root)
    try:
        return _open_pinned_benchmark_trace_bundle(
            root,
            pinned_root,
            role=role,
        )
    finally:
        pinned_root.close()


def _open_pinned_benchmark_trace_bundle(
    root: Path,
    pinned_root: _PinnedBundleRoot,
    *,
    role: str,
) -> BenchmarkTraceBundle:
    bundle_files, file_identities = _read_bundle_tree(pinned_root.descriptor)
    manifest_bytes = bundle_files.get("bundle.json")
    if manifest_bytes is None:
        raise ValueError("bundle.json is missing")
    manifest_identity = file_identities["bundle.json"]
    manifest = _parse_json_object(manifest_bytes, "bundle.json")
    _require_exact_keys(manifest, TOP_LEVEL_KEYS, "bundle.json")
    if manifest["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise ValueError("bundle.json schema_version is invalid")
    if manifest["composition_contract"] != COMPOSITION_CONTRACT:
        raise ValueError("bundle.json composition_contract is invalid")
    for field in ("case_id", "run_id", "subject_revision"):
        _require_nonempty_string(manifest[field], "bundle.json.{0}".format(field))
    if manifest["revision_provenance_status"] != "valid":
        raise ValueError("bundle.json revision_provenance_status must be valid")
    _require_sha256(manifest["bundle_id"], "bundle.json.bundle_id")
    identity_manifest = dict(manifest)
    del identity_manifest["bundle_id"]
    expected_bundle_id = _sha256(stable_json(identity_manifest).encode("utf-8"))
    if manifest["bundle_id"] != expected_bundle_id:
        raise ValueError("bundle.json bundle_id does not match canonical manifest")

    sources = manifest["sources"]
    if not isinstance(sources, dict):
        raise ValueError("bundle.json.sources must be an object")
    _require_exact_keys(sources, SOURCE_KEYS, "bundle.json.sources")
    trace_binding = _validate_source_binding(
        sources["trace"],
        "bundle.json.sources.trace",
        expected_path="inputs/trace.json",
    )
    review_binding = _validate_optional_source_binding(
        sources["review"],
        "bundle.json.sources.review",
        expected_path="inputs/review.json",
    )
    labels_binding = _validate_optional_source_binding(
        sources["labels"],
        "bundle.json.sources.labels",
        expected_path="inputs/labels.json",
    )
    if not isinstance(sources["evaluations"], list):
        raise ValueError("bundle.json.sources.evaluations must be a list")
    evaluation_bindings = tuple(
        _validate_evaluation_binding(value, index)
        for index, value in enumerate(sources["evaluations"])
    )
    effective_binding = _validate_source_binding(
        manifest["effective_trace"],
        "bundle.json.effective_trace",
        expected_path="effective/trace.json",
    )

    if not isinstance(manifest["members"], list):
        raise ValueError("bundle.json.members must be a list")
    member_values = tuple(
        _validate_member(value, index)
        for index, value in enumerate(manifest["members"])
    )
    member_paths = [value.path for value in member_values]
    if member_paths != sorted(member_paths):
        raise ValueError("bundle members must be path sorted")
    if len(member_paths) != len(set(member_paths)):
        raise ValueError("bundle member paths must be unique")
    member_by_path = {value.path: value for value in member_values}
    expected_non_artifact = [
        (trace_binding, "source_trace"),
        (effective_binding, "effective_trace"),
    ]
    if review_binding:
        expected_non_artifact.append((review_binding, "source_review"))
    if labels_binding:
        expected_non_artifact.append((labels_binding, "source_labels"))
    expected_non_artifact.extend(
        (value, "source_evaluation") for value in evaluation_bindings
    )
    for binding, expected_role in expected_non_artifact:
        _require_binding_member(binding, expected_role, member_by_path)
    expected_non_artifact_paths = {
        binding.path for binding, _role in expected_non_artifact
    }
    actual_non_artifact_paths = {
        value.path for value in member_values if value.role != "artifact"
    }
    if actual_non_artifact_paths != expected_non_artifact_paths:
        raise ValueError("source/effective members do not match their bindings")

    if not isinstance(manifest["artifacts"], list):
        raise ValueError("bundle.json.artifacts must be a list")
    artifact_values = tuple(
        _validate_artifact(value, index, member_by_path)
        for index, value in enumerate(manifest["artifacts"])
    )
    artifact_ids = [value.artifact_id for value in artifact_values]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("bundle artifact IDs must be unique")
    source_relative_paths = [value.source_relative_path for value in artifact_values]
    if len(source_relative_paths) != len(set(source_relative_paths)):
        raise ValueError("bundle artifact source paths must be unique")
    artifact_member_paths = {
        value.path for value in member_values if value.role == "artifact"
    }
    if artifact_member_paths != {value.member_path for value in artifact_values}:
        raise ValueError("artifact members do not match artifact bindings")

    counts = manifest["counts"]
    if not isinstance(counts, dict):
        raise ValueError("bundle.json.counts must be an object")
    _require_exact_keys(counts, COUNT_KEYS, "bundle.json.counts")
    for name in COUNT_KEYS:
        _require_nonnegative_integer(
            counts[name],
            "bundle.json.counts.{0}".format(name),
        )
    source_count = (
        1
        + int(review_binding is not None)
        + len(evaluation_bindings)
        + int(labels_binding is not None)
    )
    preliminary_counts = {
        "source_count": source_count,
        "evaluation_count": len(evaluation_bindings),
        "artifact_member_count": len(artifact_member_paths),
        "member_count": len(member_values),
    }
    for name, expected in preliminary_counts.items():
        if counts[name] != expected:
            raise ValueError("bundle count equation failed for {0}".format(name))

    expected_files = {"bundle.json", *member_paths}
    actual_files = set(bundle_files)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise ValueError(
            "bundle file closure mismatch; missing={0}, extra={1}".format(
                missing,
                extra,
            )
        )
    if len(file_identities) != len(set(file_identities.values())):
        raise ValueError("bundle files contain duplicate real identities")
    if file_identities.get("bundle.json") != manifest_identity:
        raise ValueError("bundle.json identity changed during validation")

    member_bytes: Dict[str, bytes] = {}
    for member in member_values:
        data = bundle_files[member.path]
        if len(data) != member.byte_length:
            raise ValueError("bundle member byte length mismatch: {0}".format(member.path))
        if _sha256(data) != member.sha256:
            raise ValueError("bundle member SHA-256 mismatch: {0}".format(member.path))
        member_bytes[member.path] = data

    effective_trace = _parse_json_object(
        member_bytes[effective_binding.path],
        effective_binding.path,
    )
    if stable_json(effective_trace).encode("utf-8") != member_bytes[effective_binding.path]:
        raise ValueError("effective Trace JSON is not canonical")
    source_trace = _parse_json_object(
        member_bytes[trace_binding.path],
        trace_binding.path,
    )
    source_manifest = source_trace.get("manifest")
    if not isinstance(source_manifest, dict):
        raise ValueError("source Trace manifest is missing")
    for field in ("case_id", "run_id", "subject_revision"):
        if source_manifest.get(field) != manifest[field]:
            raise ValueError(
                "source Trace {0} does not match bundle manifest".format(field)
            )
    source_revision, source_provenance_status = trace_execution_revision(source_trace)
    if (
        source_revision != manifest["subject_revision"]
        or source_provenance_status != manifest["revision_provenance_status"]
    ):
        raise ValueError("source Trace revision provenance does not match bundle")
    review_source = None
    if review_binding:
        review_source = _parse_json_object(
            member_bytes[review_binding.path],
            review_binding.path,
        )
        review_case_id = review_source.get("case_id")
        if review_case_id not in (None, manifest["case_id"]):
            raise ValueError("review source case_id does not match bundle manifest")
    evaluation_sources = tuple(
        _parse_json_object(
            member_bytes[evaluation_binding.path],
            evaluation_binding.path,
        )
        for evaluation_binding in evaluation_bindings
    )
    effective_manifest = effective_trace.get("manifest")
    if not isinstance(effective_manifest, dict):
        raise ValueError("effective Trace manifest is missing")
    for field in ("case_id", "run_id", "subject_revision"):
        if effective_manifest.get(field) != manifest[field]:
            raise ValueError(
                "effective Trace {0} does not match bundle manifest".format(field)
            )
    revision, provenance_status = trace_execution_revision(effective_trace)
    if (
        revision != manifest["subject_revision"]
        or provenance_status != manifest["revision_provenance_status"]
    ):
        raise ValueError("effective Trace revision provenance does not match bundle")

    declarations = effective_trace.get("artifacts", [])
    if not isinstance(declarations, list):
        raise ValueError("effective Trace artifacts must be a list")
    declarations_by_id: Dict[str, JsonDict] = {}
    for index, declaration in enumerate(declarations):
        if not isinstance(declaration, dict):
            raise ValueError(
                "effective Trace artifact {0} must be an object".format(index)
            )
        artifact_id = _require_nonempty_string(
            declaration.get("artifact_id"),
            "effective Trace artifact {0}.artifact_id".format(index),
        )
        declaration_path = _require_nonempty_string(
            declaration.get("path"),
            "effective Trace artifact {0}.path".format(index),
        )
        declaration_hash = _require_sha256(
            declaration.get("content_hash"),
            "effective Trace artifact {0}.content_hash".format(index),
        )
        declaration_length = _require_nonnegative_integer(
            declaration.get("byte_length"),
            "effective Trace artifact {0}.byte_length".format(index),
        )
        declaration_media_type = _require_nonempty_string(
            declaration.get("media_type"),
            "effective Trace artifact {0}.media_type".format(index),
        )
        if artifact_id in declarations_by_id:
            raise ValueError("effective Trace artifact IDs must be unique")
        declarations_by_id[artifact_id] = declaration
    if set(declarations_by_id) != set(artifact_ids):
        raise ValueError("artifact declarations do not match bundle bindings")
    artifacts_by_id = {value.artifact_id: value for value in artifact_values}
    for artifact_id, declaration in declarations_by_id.items():
        binding = artifacts_by_id[artifact_id]
        expected = {
            "path": binding.member_path,
            "content_hash": binding.content_sha256,
            "byte_length": binding.byte_length,
            "media_type": binding.media_type,
        }
        for field, expected_value in expected.items():
            if declaration.get(field) != expected_value:
                raise ValueError(
                    "effective Trace artifact {0} {1} mismatch".format(
                        artifact_id,
                        field,
                    )
                )

    from .benchmark_composition import compose_effective_trace

    recomposed_trace = compose_effective_trace(
        source_trace,
        review=review_source,
        evaluations=evaluation_sources,
    )
    recomposed_declarations = recomposed_trace.get("artifacts", [])
    if not isinstance(recomposed_declarations, list):
        raise ValueError("recomposed Trace artifacts must be a list")
    from .graph import collect_artifact_ids

    recomposed_referenced_artifact_ids: Set[str] = set()
    recomposed_records = recomposed_trace.get("records", [])
    if not isinstance(recomposed_records, list):
        raise ValueError("recomposed Trace records must be a list")
    for record in recomposed_records:
        if not isinstance(record, dict):
            continue
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        recomposed_referenced_artifact_ids.update(collect_artifact_ids(record, data))
    recomposed_by_id: Dict[str, JsonDict] = {}
    for index, declaration in enumerate(recomposed_declarations):
        if not isinstance(declaration, dict):
            raise ValueError(
                "recomposed Trace artifact {0} must be an object".format(index)
            )
        artifact_id = _require_nonempty_string(
            declaration.get("artifact_id"),
            "recomposed Trace artifact {0}.artifact_id".format(index),
        )
        if artifact_id in recomposed_by_id:
            raise ValueError("recomposed Trace artifact IDs must be unique")
        if artifact_id in recomposed_referenced_artifact_ids:
            recomposed_by_id[artifact_id] = declaration
    if set(recomposed_by_id) != set(artifacts_by_id):
        raise ValueError("recomposed artifact declarations do not match bundle")
    for artifact_id, declaration in recomposed_by_id.items():
        binding = artifacts_by_id[artifact_id]
        source_relative_path = _validate_relative_path(
            declaration.get("path"),
            "recomposed Trace artifact {0}.path".format(artifact_id),
        )
        declaration["media_type"] = _artifact_media_type(
            declaration,
            source_relative_path,
            "recomposed Trace artifact {0}".format(artifact_id),
        )
        declaration["path"] = binding.member_path
        declaration["content_hash"] = binding.content_sha256
    if recomposed_declarations or "artifacts" in recomposed_trace:
        recomposed_trace["artifacts"] = [
            declaration
            for declaration in recomposed_declarations
            if declaration.get("artifact_id") in recomposed_by_id
        ]
    if stable_json(recomposed_trace).encode("utf-8") != member_bytes[
        effective_binding.path
    ]:
        raise ValueError("effective Trace does not match its bound source composition")

    from .artifact_reader import PinnedVerifiedArtifactReader
    from .graph import TraceGraph, collect_artifact_ids

    referenced_artifact_ids: Set[str] = set()
    records = effective_trace.get("records", [])
    if not isinstance(records, list):
        raise ValueError("effective Trace records must be a list")
    record_ids: Set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError("effective Trace record {0} must be an object".format(index))
        record_id = _require_nonempty_string(
            record.get("record_id"),
            "effective Trace record {0}.record_id".format(index),
        )
        if record_id in record_ids:
            raise ValueError("effective Trace record IDs must be unique")
        record_ids.add(record_id)
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        referenced_artifact_ids.update(collect_artifact_ids(record, data))
    if referenced_artifact_ids != set(artifact_ids):
        raise ValueError("artifact references do not close over declarations")
    final_counts = {
        "declared_artifact_count": len(declarations_by_id),
        "referenced_artifact_count": len(referenced_artifact_ids),
    }
    for name, expected in final_counts.items():
        if counts[name] != expected:
            raise ValueError("bundle count equation failed for {0}".format(name))

    graph_trace = copy.deepcopy(effective_trace)
    pinned_reader = PinnedVerifiedArtifactReader(
        {
            artifact_id: copy.deepcopy(declaration)
            for artifact_id, declaration in declarations_by_id.items()
        },
        {
            artifact.artifact_id: member_bytes[artifact.member_path]
            for artifact in artifact_values
        },
    )
    TraceGraph.from_trace(
        graph_trace,
        artifact_root=root,
        artifact_reader=pinned_reader,
    )
    public_effective_trace = _freeze_json(copy.deepcopy(effective_trace))
    pinned_root.verify()
    visible_members = tuple(
        value
        for value in member_values
        if role == "evaluation" or value.role != "source_labels"
    )
    visible_labels = labels_binding if role == "evaluation" else None
    return BenchmarkTraceBundle(
        root=root,
        artifact_root=root,
        bundle_id=manifest["bundle_id"],
        case_id=manifest["case_id"],
        run_id=manifest["run_id"],
        subject_revision=manifest["subject_revision"],
        role=role,
        trace_source=trace_binding,
        review_source=review_binding,
        evaluation_sources=evaluation_bindings,
        labels_source=visible_labels,
        effective_trace_source=effective_binding,
        members=visible_members,
        artifacts=artifact_values,
        trace_path=root / trace_binding.path,
        review_path=root / review_binding.path if review_binding else None,
        evaluation_paths=tuple(root / value.path for value in evaluation_bindings),
        labels_path=root / visible_labels.path if visible_labels else None,
        effective_trace_path=root / effective_binding.path,
        trace_bytes=bytes(member_bytes[trace_binding.path]),
        review_bytes=(
            bytes(member_bytes[review_binding.path]) if review_binding else None
        ),
        evaluation_bytes=tuple(
            bytes(member_bytes[value.path]) for value in evaluation_bindings
        ),
        labels_bytes=(
            bytes(member_bytes[labels_binding.path])
            if role == "evaluation" and labels_binding
            else None
        ),
        effective_trace_bytes=bytes(member_bytes[effective_binding.path]),
        effective_trace=public_effective_trace,
        _artifact_contents=tuple(
            (artifact.artifact_id, bytes(member_bytes[artifact.member_path]))
            for artifact in artifact_values
        ),
    )


def _validate_source_binding(
    value: Any,
    label: str,
    *,
    expected_path: str,
) -> BenchmarkBundleSource:
    if not isinstance(value, dict):
        raise ValueError("{0} must be an object".format(label))
    _require_exact_keys(value, BINDING_KEYS, label)
    source = _source_from_binding(value, label)
    if source.path != expected_path:
        raise ValueError("{0}.path must be {1}".format(label, expected_path))
    if source.media_type != "application/json":
        raise ValueError("{0}.media_type must be application/json".format(label))
    return source


def _validate_optional_source_binding(
    value: Any,
    label: str,
    *,
    expected_path: str,
) -> Optional[BenchmarkBundleSource]:
    if value is None:
        return None
    return _validate_source_binding(value, label, expected_path=expected_path)


def _validate_evaluation_binding(
    value: Any,
    index: int,
) -> BenchmarkBundleSource:
    label = "bundle.json.sources.evaluations[{0}]".format(index)
    if not isinstance(value, dict):
        raise ValueError("{0} must be an object".format(label))
    _require_exact_keys(value, EVALUATION_BINDING_KEYS, label)
    _require_nonnegative_integer(value["source_index"], label + ".source_index")
    if value["source_index"] != index:
        raise ValueError("evaluation source indices must be ordered and contiguous")
    source = _source_from_binding(value, label, source_index=index)
    expected_path = "inputs/evaluations/{0:04d}-{1}.json".format(
        index,
        source.sha256.removeprefix("sha256:"),
    )
    if source.path != expected_path:
        raise ValueError("{0}.path does not match its index and digest".format(label))
    if source.media_type != "application/json":
        raise ValueError("{0}.media_type must be application/json".format(label))
    return source


def _source_from_binding(
    value: JsonDict,
    label: str,
    *,
    source_index: Optional[int] = None,
) -> BenchmarkBundleSource:
    path = _validate_relative_path(value["path"], label + ".path")
    sha256 = _require_sha256(value["sha256"], label + ".sha256")
    byte_length = _require_nonnegative_integer(
        value["byte_length"],
        label + ".byte_length",
    )
    media_type = _require_nonempty_string(value["media_type"], label + ".media_type")
    return BenchmarkBundleSource(
        path=path,
        sha256=sha256,
        byte_length=byte_length,
        media_type=media_type,
        source_index=source_index,
    )


def _validate_member(value: Any, index: int) -> BenchmarkBundleMember:
    label = "bundle.json.members[{0}]".format(index)
    if not isinstance(value, dict):
        raise ValueError("{0} must be an object".format(label))
    _require_exact_keys(value, MEMBER_KEYS, label)
    role = value["role"]
    if not isinstance(role, str) or role not in MEMBER_ROLES:
        raise ValueError("{0}.role is invalid".format(label))
    return BenchmarkBundleMember(
        role=role,
        path=_validate_relative_path(value["path"], label + ".path"),
        sha256=_require_sha256(value["sha256"], label + ".sha256"),
        byte_length=_require_nonnegative_integer(
            value["byte_length"],
            label + ".byte_length",
        ),
        media_type=_require_nonempty_string(
            value["media_type"],
            label + ".media_type",
        ),
    )


def _validate_artifact(
    value: Any,
    index: int,
    member_by_path: Dict[str, BenchmarkBundleMember],
) -> BenchmarkBundleArtifact:
    label = "bundle.json.artifacts[{0}]".format(index)
    if not isinstance(value, dict):
        raise ValueError("{0} must be an object".format(label))
    _require_exact_keys(value, ARTIFACT_KEYS, label)
    artifact_id = _require_nonempty_string(value["artifact_id"], label + ".artifact_id")
    source_relative_path = _validate_relative_path(
        value["source_relative_path"],
        label + ".source_relative_path",
    )
    declared_content_hash = value["declared_content_hash"]
    if not isinstance(declared_content_hash, str) or (
        declared_content_hash
        and DECLARED_HASH_PATTERN.fullmatch(declared_content_hash) is None
    ):
        raise ValueError("{0}.declared_content_hash is invalid".format(label))
    member_path = _validate_relative_path(
        value["member_path"],
        label + ".member_path",
    )
    content_sha256 = _require_sha256(
        value["content_sha256"],
        label + ".content_sha256",
    )
    normalized_declared_hash = declared_content_hash.removeprefix("sha256:")
    if (
        normalized_declared_hash
        and not content_sha256.removeprefix("sha256:").startswith(
            normalized_declared_hash
        )
    ):
        raise ValueError(
            "{0}.declared_content_hash conflicts with content hash".format(label)
        )
    byte_length = _require_nonnegative_integer(
        value["byte_length"],
        label + ".byte_length",
    )
    media_type = _require_nonempty_string(value["media_type"], label + ".media_type")
    digest_hex = content_sha256.removeprefix("sha256:")
    expected_member_path = "artifacts/sha256/{0}/{1}".format(
        digest_hex[:2],
        digest_hex,
    )
    if member_path != expected_member_path:
        raise ValueError("{0}.member_path is not content addressed".format(label))
    member = member_by_path.get(member_path)
    if member is None or member.role != "artifact":
        raise ValueError("{0} has no artifact member".format(label))
    if (
        member.sha256 != content_sha256
        or member.byte_length != byte_length
        or member.media_type != media_type
    ):
        raise ValueError("{0} does not match its artifact member".format(label))
    return BenchmarkBundleArtifact(
        artifact_id=artifact_id,
        source_relative_path=source_relative_path,
        declared_content_hash=declared_content_hash,
        member_path=member_path,
        content_sha256=content_sha256,
        byte_length=byte_length,
        media_type=media_type,
    )


def _require_binding_member(
    binding: BenchmarkBundleSource,
    expected_role: str,
    member_by_path: Dict[str, BenchmarkBundleMember],
) -> None:
    member = member_by_path.get(binding.path)
    if member is None or member.role != expected_role:
        raise ValueError("binding {0} has no {1} member".format(binding.path, expected_role))
    if (
        member.sha256 != binding.sha256
        or member.byte_length != binding.byte_length
        or member.media_type != binding.media_type
    ):
        raise ValueError("binding {0} does not match its member".format(binding.path))


def _manifest_binding(
    path: str,
    data: bytes,
    *,
    media_type: str = "application/json",
    source_index: Optional[int] = None,
) -> JsonDict:
    binding: JsonDict = {
        "path": path,
        "sha256": _sha256(data),
        "byte_length": len(data),
        "media_type": media_type,
    }
    if source_index is not None:
        binding["source_index"] = source_index
    return binding


def _read_source_file(path: Path, label: str) -> bytes:
    path = Path(path)
    if not path.name or path.name in {".", ".."}:
        raise ValueError("{0} path is invalid".format(label))
    root = _open_bundle_root(path.parent)
    try:
        try:
            source_stat = os.stat(
                path.name,
                dir_fd=root.descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError("{0} is unavailable".format(label)) from error
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("{0} is not a regular file".format(label))
        data, _identity = _read_bundle_file(
            root.descriptor,
            path.name,
            label,
            source_stat,
        )
        root.verify()
        return data
    finally:
        root.close()


def _read_source_artifact(
    root: _PinnedBundleRoot,
    relative_path: str,
) -> bytes:
    normalized = _validate_relative_path(relative_path, "source artifact path")
    components = normalized.split("/")
    current = root.descriptor
    opened: List[int] = []
    try:
        for index, component in enumerate(components[:-1]):
            relative = "/".join(components[: index + 1])
            try:
                expected = os.stat(
                    component,
                    dir_fd=current,
                    follow_symlinks=False,
                )
                descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | os.O_NOFOLLOW
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=current,
                )
            except OSError as error:
                raise ValueError(
                    "source artifact directory is unavailable: {0}".format(relative)
                ) from error
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(expected.st_mode)
                or not stat.S_ISDIR(opened_stat.st_mode)
                or _file_identity(expected) != _file_identity(opened_stat)
            ):
                os.close(descriptor)
                raise ValueError(
                    "source artifact directory changed: {0}".format(relative)
                )
            opened.append(descriptor)
            current = descriptor
        leaf = components[-1]
        try:
            expected_leaf = os.stat(
                leaf,
                dir_fd=current,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError(
                "source artifact is unavailable: {0}".format(normalized)
            ) from error
        if not stat.S_ISREG(expected_leaf.st_mode):
            raise ValueError(
                "source artifact is not a regular file: {0}".format(normalized)
            )
        data, _identity = _read_bundle_file(
            current,
            leaf,
            normalized,
            expected_leaf,
        )
        return data
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _open_bundle_root(root: Path) -> _PinnedBundleRoot:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_only is None:
        raise ValueError("secure benchmark bundle traversal is unavailable")
    root_parts = root.parts
    if ".." in root_parts:
        raise ValueError("benchmark bundle root contains an unsafe path component")
    absolute_root = root if root.is_absolute() else Path.cwd() / root
    components = absolute_root.parts[1:]
    flags = os.O_RDONLY | nofollow | directory_only | getattr(os, "O_CLOEXEC", 0)
    descriptors: List[int] = []
    links: List[Tuple[int, str, int]] = []
    identities: List[Tuple[int, int]] = []
    try:
        current = os.open(os.path.sep, flags)
        descriptors.append(current)
        identities.append(_checked_directory_identity(current, "/"))
        for component in components:
            child = os.open(component, flags, dir_fd=current)
            descriptors.append(child)
            links.append((current, component, child))
            identities.append(_checked_directory_identity(child, component))
            current = child
    except (OSError, ValueError) as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ValueError("benchmark bundle root is unavailable or unsafe") from error
    return _PinnedBundleRoot(
        descriptors=tuple(descriptors),
        links=tuple(links),
        identities=tuple(identities),
    )


def _open_or_create_bundle_root(root: Path) -> _PinnedBundleRoot:
    """Create a directory path without following an existing symlink component."""

    absolute_root = Path(os.path.abspath(os.fspath(root)))
    missing: List[str] = []
    existing = absolute_root
    while True:
        try:
            os.lstat(existing)
            break
        except FileNotFoundError:
            if existing.parent == existing:
                raise ValueError("benchmark bundle store root is unavailable")
            missing.append(existing.name)
            existing = existing.parent
        except OSError as error:
            raise ValueError("benchmark bundle store root is unavailable") from error

    handle = _open_bundle_root(existing)
    current = existing
    try:
        for component in reversed(missing):
            try:
                os.mkdir(component, 0o755, dir_fd=handle.descriptor)
            except FileExistsError:
                pass
            except OSError as error:
                raise ValueError(
                    "benchmark bundle store directory creation failed"
                ) from error
            handle.verify()
            current = current / component
            next_handle = _open_bundle_root(current)
            handle.close()
            handle = next_handle
        return handle
    except Exception:
        handle.close()
        raise


def _open_child_directory(parent_descriptor: int, name: str) -> int:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        expected = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError("benchmark bundle output directory is unsafe") from error
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(expected.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or _file_identity(expected) != _file_identity(opened)
    ):
        os.close(descriptor)
        raise ValueError("benchmark bundle output directory changed")
    return descriptor


def _write_bundle_member(
    namespace_descriptor: int,
    staging_name: str,
    relative_path: str,
    data: bytes,
) -> None:
    normalized = _validate_relative_path(
        relative_path,
        "bundle output member",
        allow_reserved=True,
    )
    opened: List[int] = []
    try:
        current = _open_child_directory(namespace_descriptor, staging_name)
        opened.append(current)
        components = normalized.split("/")
        for component in components[:-1]:
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            except OSError as error:
                raise ValueError(
                    "benchmark bundle output directory creation failed"
                ) from error
            child = _open_child_directory(current, component)
            opened.append(child)
            current = child
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(components[-1], flags, 0o600, dir_fd=current)
        except OSError as error:
            raise ValueError("benchmark bundle output member is unsafe") from error
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ValueError("benchmark bundle output member write failed")
                view = view[written:]
            final_stat = os.fstat(descriptor)
            if not stat.S_ISREG(final_stat.st_mode) or final_stat.st_nlink != 1:
                raise ValueError("benchmark bundle output member is unsafe")
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _remove_tree_entry(
    parent_descriptor: int,
    name: str,
    expected_identity: Tuple[int, int],
) -> None:
    """Remove an owned unpublished tree without resolving path components."""

    try:
        entry_stat = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError("benchmark bundle cleanup failed") from error
    if _file_identity(entry_stat) != expected_identity:
        raise ValueError("benchmark bundle cleanup target identity changed")
    if not stat.S_ISDIR(entry_stat.st_mode):
        try:
            os.unlink(name, dir_fd=parent_descriptor)
            return
        except OSError as error:
            raise ValueError("benchmark bundle cleanup failed") from error

    descriptor = _open_child_directory(parent_descriptor, name)
    try:
        for child in os.listdir(descriptor):
            child_stat = os.stat(
                child,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            _remove_tree_entry(
                descriptor,
                child,
                _file_identity(child_stat),
            )
        linked = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(linked.st_mode)
            or _file_identity(linked) != _file_identity(entry_stat)
        ):
            raise ValueError("benchmark bundle cleanup target changed")
    finally:
        os.close(descriptor)
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError("benchmark bundle cleanup failed") from error


def _rollback_published_bundle(
    namespace_descriptor: int,
    target_name: str,
    expected_identity: Tuple[int, int],
) -> None:
    try:
        target_stat = os.stat(
            target_name,
            dir_fd=namespace_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError("benchmark bundle rollback failed") from error
    if (
        not stat.S_ISDIR(target_stat.st_mode)
        or _file_identity(target_stat) != expected_identity
    ):
        raise ValueError("benchmark bundle rollback target changed")
    rollback_name = ".rollback-{0}".format(secrets.token_hex(16))
    try:
        os.rename(
            target_name,
            rollback_name,
            src_dir_fd=namespace_descriptor,
            dst_dir_fd=namespace_descriptor,
        )
    except OSError as error:
        raise ValueError("benchmark bundle rollback failed") from error
    rollback_stat = os.stat(
        rollback_name,
        dir_fd=namespace_descriptor,
        follow_symlinks=False,
    )
    if _file_identity(rollback_stat) != expected_identity:
        raise ValueError("benchmark bundle rollback target identity changed")
    _remove_tree_entry(
        namespace_descriptor,
        rollback_name,
        expected_identity,
    )


def _read_bundle_tree(
    root_descriptor: int,
) -> Tuple[Dict[str, bytes], Dict[str, Tuple[int, int]]]:
    files: Dict[str, bytes] = {}
    identities: Dict[str, Tuple[int, int]] = {}
    _read_bundle_directory(root_descriptor, "", files, identities)
    return files, identities


def _read_bundle_directory(
    descriptor: int,
    prefix: str,
    files: Dict[str, bytes],
    identities: Dict[str, Tuple[int, int]],
) -> None:
    before = _directory_signature(os.fstat(descriptor))
    try:
        entries = sorted(os.scandir(descriptor), key=lambda entry: entry.name)
    except OSError as error:
        raise ValueError("bundle directory cannot be scanned") from error
    for entry in entries:
        name = entry.name
        relative = "{0}/{1}".format(prefix, name) if prefix else name
        try:
            entry_stat = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ValueError("bundle entry is unavailable: {0}".format(relative)) from error
        if stat.S_ISLNK(entry_stat.st_mode):
            raise ValueError("bundle contains a symlink: {0}".format(relative))
        if stat.S_ISDIR(entry_stat.st_mode):
            _read_bundle_subdirectory(
                descriptor,
                name,
                relative,
                entry_stat,
                files,
                identities,
            )
            continue
        if not stat.S_ISREG(entry_stat.st_mode):
            raise ValueError("bundle contains a special file: {0}".format(relative))
        data, identity = _read_bundle_file(
            descriptor,
            name,
            relative,
            entry_stat,
        )
        files[relative] = data
        identities[relative] = identity
    after = _directory_signature(os.fstat(descriptor))
    if after != before:
        raise ValueError("bundle directory changed during validation")


def _read_bundle_subdirectory(
    parent_descriptor: int,
    name: str,
    relative: str,
    expected_stat: os.stat_result,
    files: Dict[str, bytes],
    identities: Dict[str, Tuple[int, int]],
) -> None:
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError("bundle directory is unavailable: {0}".format(relative)) from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened_stat.st_mode)
            or _file_identity(opened_stat) != _file_identity(expected_stat)
        ):
            raise ValueError("bundle directory changed before read: {0}".format(relative))
        _read_bundle_directory(descriptor, relative, files, identities)
        linked_stat = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(linked_stat.st_mode)
            or _file_identity(linked_stat) != _file_identity(opened_stat)
        ):
            raise ValueError("bundle directory changed during read: {0}".format(relative))
    except OSError as error:
        raise ValueError("bundle directory changed during read: {0}".format(relative)) from error
    finally:
        os.close(descriptor)


def _read_bundle_file(
    parent_descriptor: int,
    name: str,
    relative: str,
    expected_stat: os.stat_result,
) -> Tuple[bytes, Tuple[int, int]]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as error:
        raise ValueError("bundle member is unavailable: {0}".format(relative)) from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or _file_identity(opened_stat) != _file_identity(expected_stat)
        ):
            raise ValueError("bundle member is unsafe: {0}".format(relative))
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final_stat = os.fstat(descriptor)
        linked_stat = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            _regular_file_signature(final_stat)
            != _regular_file_signature(opened_stat)
            or not stat.S_ISREG(linked_stat.st_mode)
            or linked_stat.st_nlink != 1
            or _file_identity(linked_stat) != _file_identity(opened_stat)
        ):
            raise ValueError("bundle member changed during read: {0}".format(relative))
    except OSError as error:
        raise ValueError("bundle member changed during read: {0}".format(relative)) from error
    finally:
        os.close(descriptor)
    return b"".join(chunks), _file_identity(opened_stat)


def _checked_directory_identity(descriptor: int, label: str) -> Tuple[int, int]:
    descriptor_stat = os.fstat(descriptor)
    if not stat.S_ISDIR(descriptor_stat.st_mode):
        raise ValueError("bundle root component is not a directory: {0}".format(label))
    return _file_identity(descriptor_stat)


def _file_identity(value: os.stat_result) -> Tuple[int, int]:
    return value.st_dev, value.st_ino


def _directory_signature(value: os.stat_result) -> Tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


def _regular_file_signature(
    value: os.stat_result,
) -> Tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


def _parse_json_object(data: bytes, label: str) -> JsonDict:
    return load_json_object(data, label)


def _artifact_media_type(
    declaration: Mapping[str, Any],
    source_relative_path: str,
    label: str,
) -> str:
    declared = declaration.get("media_type")
    if isinstance(declared, str) and declared.strip():
        return declared
    kind = declaration.get("kind")
    if kind == "json" or source_relative_path.endswith(".json"):
        return "application/json"
    if kind == "text" or source_relative_path.endswith(".txt"):
        return "text/plain"
    if source_relative_path.endswith(".jsonl"):
        return "application/x-ndjson"
    if source_relative_path.endswith(".html"):
        return "text/html"
    if source_relative_path.endswith(".md"):
        return "text/markdown"
    raise ValueError("{0}.media_type must be declared or inferable".format(label))


def _validate_relative_path(
    value: Any,
    label: str,
    *,
    allow_reserved: bool = False,
) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("{0} must be a non-empty relative path".format(label))
    if (
        value.startswith("/")
        or re.match(r"[A-Za-z]:", value)
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("{0} is not a portable relative path".format(label))
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("{0} contains an unsafe path component".format(label))
    if not allow_reserved and value == "bundle.json":
        raise ValueError("{0} collides with reserved bundle.json".format(label))
    return value


def _require_exact_keys(value: JsonDict, expected: Iterable[str], label: str) -> None:
    expected_keys = set(expected)
    actual_keys = set(value)
    if actual_keys != expected_keys:
        raise ValueError(
            "{0} keys must be exact; missing={1}, extra={2}".format(
                label,
                sorted(expected_keys - actual_keys),
                sorted(actual_keys - expected_keys),
            )
        )


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(label))
    return value


def _require_nonnegative_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("{0} must be a non-negative integer".format(label))
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("{0} must be a full lowercase SHA-256".format(label))
    return value


def _sha256(data: bytes) -> str:
    return "sha256:{0}".format(hashlib.sha256(data).hexdigest())
