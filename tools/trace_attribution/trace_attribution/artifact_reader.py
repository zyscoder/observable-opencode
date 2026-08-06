from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .models import JsonDict


@dataclass(frozen=True)
class VerifiedArtifact:
    artifact_id: str
    content: Optional[str]
    content_bytes: Optional[bytes]
    source: str
    truncated: bool
    byte_ranges: Tuple[Tuple[int, int], ...]
    file_hash_status: str
    slice_hash_status: str
    semantic_slice_count: int
    rejected_semantic_slice_count: int
    failures: Tuple[str, ...]
    hash_mismatch: bool
    file_available: bool
    file_identity: Optional[Tuple[int, int, int, str]]


class VerifiedArtifactReader:
    """Resolve artifact content once, exposing only hash-verified valid UTF-8."""

    def __init__(self, artifact_index: Dict[str, JsonDict], artifact_root: Optional[Path]):
        self.artifact_index = artifact_index
        self.root = Path(artifact_root).resolve() if artifact_root is not None else None
        self._cache: Dict[str, VerifiedArtifact] = {}

    def read(self, artifact_id: str, *, refresh: bool = False) -> VerifiedArtifact:
        if not refresh and artifact_id in self._cache:
            return self._cache[artifact_id]
        result = self._read_uncached(artifact_id)
        self._cache[artifact_id] = result
        return result

    def _read_uncached(self, artifact_id: str) -> VerifiedArtifact:
        artifact = self.artifact_index.get(artifact_id)
        if not isinstance(artifact, dict):
            return _unavailable(artifact_id, "artifact_not_indexed")

        failures = []
        hash_mismatch = False
        file_available = False
        path_value = artifact.get("path")
        file_hash_status = "missing"
        if path_value and (not isinstance(path_value, str) or not case_relative_artifact_path(path_value)):
            file_hash_status = "invalid_path"
            failures.append("bundle_file_invalid_path")
        elif self.root is not None and isinstance(path_value, str):
            candidate = (self.root / path_value).resolve()
            try:
                candidate.relative_to(self.root)
            except ValueError:
                file_hash_status = "invalid_path"
                failures.append("bundle_file_invalid_path")
            else:
                try:
                    with candidate.open("rb") as handle:
                        stat = os.fstat(handle.fileno())
                        content_bytes = handle.read()
                except OSError:
                    pass
                else:
                    file_available = True
                    manifest_hash = artifact.get("content_hash") or artifact.get("hash")
                    if not content_hash_matches(content_bytes, manifest_hash):
                        file_hash_status = "mismatch"
                        hash_mismatch = True
                        failures.append("bundle_file_hash_mismatch")
                    elif not valid_declared_byte_length(artifact, len(content_bytes)):
                        file_hash_status = "byte_length_mismatch"
                        failures.append("bundle_file_byte_length_mismatch")
                    else:
                        try:
                            content = content_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            file_hash_status = "invalid_utf8"
                            failures.append("bundle_file_invalid_utf8")
                        else:
                            manifest_identity = str(manifest_hash or "").strip()
                            return VerifiedArtifact(
                                artifact_id=artifact_id,
                                content=content,
                                content_bytes=content_bytes,
                                source="bundle_file",
                                truncated=False,
                                byte_ranges=((0, len(content_bytes)),),
                                file_hash_status="verified",
                                slice_hash_status="not_used",
                                semantic_slice_count=0,
                                rejected_semantic_slice_count=0,
                                failures=tuple(failures),
                                hash_mismatch=hash_mismatch,
                                file_available=True,
                                file_identity=(stat.st_size, stat.st_mtime_ns, stat.st_ino, manifest_identity),
                            )

        slices = artifact.get("semantic_slices")
        byte_length = artifact.get("byte_length")
        if not isinstance(slices, list) or not slices:
            return _unavailable(
                artifact_id,
                *failures,
                file_hash_status=file_hash_status,
                hash_mismatch=hash_mismatch,
                file_available=file_available,
            )
        if not _valid_byte_length(byte_length):
            failures.append("semantic_slice_byte_length_invalid")
            return _unavailable(
                artifact_id,
                *failures,
                file_hash_status=file_hash_status,
                hash_mismatch=hash_mismatch,
                file_available=file_available,
            )

        accepted = []
        rejected = 0
        for _, item in sorted(enumerate(slices), key=_semantic_slice_sort_key):
            if not isinstance(item, dict):
                rejected += 1
                failures.append("semantic_slice_range_mismatch")
                continue
            byte_range = item.get("byte_range")
            content = item.get("content")
            encoded = _valid_semantic_slice_bytes(byte_range, content)
            if encoded is None:
                rejected += 1
                failures.append("semantic_slice_range_mismatch")
                continue
            start, end = int(byte_range[0]), int(byte_range[1])
            if end > byte_length:
                rejected += 1
                failures.append("semantic_slice_out_of_bounds")
                continue
            if not content_hash_matches(encoded, item.get("hash")):
                rejected += 1
                hash_mismatch = True
                failures.append("semantic_slice_hash_mismatch")
                continue
            if accepted and start < accepted[-1][1]:
                rejected += 1
                failures.append("semantic_slice_overlap")
                continue
            if accepted and start > accepted[-1][1]:
                rejected += 1
                failures.append("semantic_slice_gap")
                continue
            accepted.append((start, end, content, encoded))

        content_bytes = b"".join(item[3] for item in accepted)
        if not accepted or not content_bytes:
            return _unavailable(
                artifact_id,
                *failures,
                file_hash_status=file_hash_status,
                slice_hash_status="mismatch" if hash_mismatch else "unavailable",
                rejected_semantic_slice_count=rejected,
                hash_mismatch=hash_mismatch,
                file_available=file_available,
            )
        return VerifiedArtifact(
            artifact_id=artifact_id,
            content="".join(item[2] for item in accepted),
            content_bytes=content_bytes,
            source="embedded_semantic_slice",
            truncated=True,
            byte_ranges=tuple((item[0], item[1]) for item in accepted),
            file_hash_status=file_hash_status,
            slice_hash_status="partial" if rejected else "verified",
            semantic_slice_count=len(accepted),
            rejected_semantic_slice_count=rejected,
            failures=tuple(dict.fromkeys(failures)),
            hash_mismatch=hash_mismatch,
            file_available=file_available,
            file_identity=None,
        )


class PinnedVerifiedArtifactReader(VerifiedArtifactReader):
    """Expose only artifact bytes retained by a completed bundle validation."""

    def __init__(
        self,
        artifact_index: Dict[str, JsonDict],
        pinned_content: Dict[str, bytes],
    ) -> None:
        private_index = {
            str(artifact_id): dict(artifact)
            for artifact_id, artifact in artifact_index.items()
        }
        super().__init__(
            {
                artifact_id: dict(artifact)
                for artifact_id, artifact in private_index.items()
            },
            None,
        )
        self._pinned_artifact_index = private_index
        self._pinned_content = {
            str(artifact_id): bytes(content)
            for artifact_id, content in pinned_content.items()
        }

    def _read_uncached(self, artifact_id: str) -> VerifiedArtifact:
        artifact = self._pinned_artifact_index.get(artifact_id)
        if not isinstance(artifact, dict):
            return _unavailable(artifact_id, "artifact_not_indexed")
        content_bytes = self._pinned_content.get(artifact_id)
        if content_bytes is None:
            return _unavailable(artifact_id, "pinned_artifact_missing")
        manifest_hash = artifact.get("content_hash") or artifact.get("hash")
        if not content_hash_matches(content_bytes, manifest_hash):
            return _unavailable(
                artifact_id,
                "pinned_artifact_hash_mismatch",
                file_hash_status="mismatch",
                hash_mismatch=True,
                file_available=True,
            )
        if not valid_declared_byte_length(artifact, len(content_bytes)):
            return _unavailable(
                artifact_id,
                "pinned_artifact_byte_length_mismatch",
                file_hash_status="byte_length_mismatch",
                file_available=True,
            )
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return _unavailable(
                artifact_id,
                "pinned_artifact_invalid_utf8",
                file_hash_status="invalid_utf8",
                file_available=True,
            )
        manifest_identity = str(manifest_hash or "").strip()
        return VerifiedArtifact(
            artifact_id=artifact_id,
            content=content,
            content_bytes=content_bytes,
            source="bundle_file",
            truncated=False,
            byte_ranges=((0, len(content_bytes)),),
            file_hash_status="verified",
            slice_hash_status="not_used",
            semantic_slice_count=0,
            rejected_semantic_slice_count=0,
            failures=(),
            hash_mismatch=False,
            file_available=True,
            file_identity=(len(content_bytes), 0, 0, manifest_identity),
        )


def case_relative_artifact_path(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def content_hash_matches(content: bytes, expected: Any) -> bool:
    match = re.fullmatch(r"(?:sha256:)?([0-9a-fA-F]{16}|[0-9a-fA-F]{64})", str(expected or ""))
    if not match:
        return False
    actual = hashlib.sha256(content).hexdigest()
    digest = match.group(1).lower()
    return actual[: len(digest)] == digest


def valid_declared_byte_length(artifact: JsonDict, actual: int) -> bool:
    if "byte_length" not in artifact:
        return True
    value = artifact.get("byte_length")
    return _valid_byte_length(value) and value == actual


def _valid_byte_length(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _semantic_slice_sort_key(entry: Tuple[int, Any]) -> Tuple[int, int, int]:
    index, item = entry
    byte_range = item.get("byte_range") if isinstance(item, dict) else None
    if (
        isinstance(byte_range, (list, tuple))
        and len(byte_range) == 2
        and isinstance(byte_range[0], int)
        and not isinstance(byte_range[0], bool)
        and isinstance(byte_range[1], int)
        and not isinstance(byte_range[1], bool)
    ):
        return byte_range[0], byte_range[1], index
    return (2**63 - 1, 2**63 - 1, index)


def _valid_semantic_slice_bytes(byte_range: Any, content: Any) -> Optional[bytes]:
    if not isinstance(content, str) or not isinstance(byte_range, (list, tuple)) or len(byte_range) != 2:
        return None
    start, end = byte_range
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end < start
    ):
        return None
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return encoded if len(encoded) == end - start else None


def _unavailable(
    artifact_id: str,
    *failures: str,
    file_hash_status: str = "missing",
    slice_hash_status: str = "unavailable",
    rejected_semantic_slice_count: int = 0,
    hash_mismatch: bool = False,
    file_available: bool = False,
) -> VerifiedArtifact:
    return VerifiedArtifact(
        artifact_id=artifact_id,
        content=None,
        content_bytes=None,
        source="",
        truncated=False,
        byte_ranges=(),
        file_hash_status=file_hash_status,
        slice_hash_status=slice_hash_status,
        semantic_slice_count=0,
        rejected_semantic_slice_count=rejected_semantic_slice_count,
        failures=tuple(dict.fromkeys(failures)),
        hash_mismatch=hash_mismatch,
        file_available=file_available,
        file_identity=None,
    )
