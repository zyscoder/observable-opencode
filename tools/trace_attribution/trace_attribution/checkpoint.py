"""Durable append-only checkpoints for passive recursive attribution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import JsonDict, stable_json


CHECKPOINT_SCHEMA_VERSION = "recursive-attribution-checkpoint/v1"
CHECKPOINT_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "case_id",
        "trace_fingerprint",
        "objective",
        "analysis_perspective",
        "start_refs",
        "budgets",
        "model_identity",
        "cache_identity",
        "config_fingerprint",
    }
)
CHECKPOINT_BUDGET_KEYS = frozenset(
    {
        "max_frontier_items",
        "max_depth",
        "max_hypotheses",
        "max_investigation_rounds",
        "max_artifact_bytes",
        "max_judge_requests",
    }
)
JOURNAL_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "journal",
        "sequence",
        "timestamp",
        "operation",
        "semantic_key",
        "payload",
        "previous_hash",
        "record_hash",
    }
)
JOURNAL_NAMES = ("frontier", "hypotheses", "actions")


class CheckpointError(ValueError):
    """Base class for deterministic checkpoint failures."""


class CheckpointCompatibilityError(CheckpointError):
    """Raised when resume inputs differ from the checkpoint manifest."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when a journal is not a valid append-only hash chain."""


def _sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _exact_mapping(value: Any, keys: frozenset[str], label: str) -> JsonDict:
    if not isinstance(value, Mapping):
        raise CheckpointCorruptionError("{0} must be an object".format(label))
    actual = frozenset(str(key) for key in value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise CheckpointCorruptionError(
            "{0} schema mismatch (missing={1}, extra={2})".format(label, missing, extra)
        )
    return dict(value)


def _validated_budgets(value: Any) -> JsonDict:
    budgets = _exact_mapping(value, CHECKPOINT_BUDGET_KEYS, "checkpoint budgets")
    output: JsonDict = {}
    for key in sorted(CHECKPOINT_BUDGET_KEYS):
        amount = budgets[key]
        if type(amount) is not int or amount < 0:
            raise CheckpointCompatibilityError("{0} must be a non-negative integer".format(key))
        output[key] = amount
    return output


def build_checkpoint_config(
    *,
    trace: Mapping[str, Any],
    case_id: str,
    objective: str,
    analysis_perspective: str,
    start_refs: Sequence[str],
    budgets: Mapping[str, Any],
    model_identity: str,
    cache_identity: str,
) -> JsonDict:
    """Build the exact semantic compatibility envelope for a resumable run."""

    semantic = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "case_id": str(case_id),
        "trace_fingerprint": _sha256(trace),
        "objective": str(objective),
        "analysis_perspective": str(analysis_perspective),
        "start_refs": [str(item) for item in start_refs],
        "budgets": _validated_budgets(budgets),
        "model_identity": str(model_identity),
        "cache_identity": str(cache_identity),
    }
    return {**semantic, "config_fingerprint": _sha256(semantic)}


def validate_checkpoint_config(value: Mapping[str, Any]) -> JsonDict:
    config = _exact_mapping(value, CHECKPOINT_CONFIG_KEYS, "checkpoint manifest")
    if config["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointCompatibilityError("unsupported checkpoint schema version")
    start_refs = config["start_refs"]
    if not isinstance(start_refs, list) or any(not isinstance(item, str) for item in start_refs):
        raise CheckpointCompatibilityError("start_refs must be a list of strings")
    config["budgets"] = _validated_budgets(config["budgets"])
    for key in (
        "case_id",
        "trace_fingerprint",
        "objective",
        "analysis_perspective",
        "model_identity",
        "cache_identity",
        "config_fingerprint",
    ):
        if not isinstance(config[key], str):
            raise CheckpointCompatibilityError("{0} must be a string".format(key))
    semantic = {key: config[key] for key in config if key != "config_fingerprint"}
    if config["config_fingerprint"] != _sha256(semantic):
        raise CheckpointCorruptionError("checkpoint config fingerprint does not match contents")
    return config


@dataclass(frozen=True)
class CheckpointState:
    config: JsonDict
    frontier_records: Tuple[JsonDict, ...]
    hypothesis_records: Tuple[JsonDict, ...]
    actions: Tuple[JsonDict, ...]
    corrupt_entries: int = 0

    @property
    def frontier_payload(self) -> JsonDict:
        return dict(self.frontier_records[-1]["payload"]) if self.frontier_records else {}

    @property
    def hypothesis_payload(self) -> JsonDict:
        return dict(self.hypothesis_records[-1]["payload"]) if self.hypothesis_records else {}

    @property
    def inflight_actions(self) -> Tuple[JsonDict, ...]:
        terminal = {
            "provider_call_completed",
            "provider_call_failed",
            "provider_call_interrupted",
            "investigation_completed",
            "investigation_failed",
            "confirmation_completed",
            "confirmation_failed",
        }
        latest: Dict[str, JsonDict] = {}
        for record in self.actions:
            latest[str(record["semantic_key"])] = record
        return tuple(
            record
            for _, record in sorted(latest.items())
            if record["operation"].endswith("_started")
            and record["operation"] not in terminal
        )

    @property
    def final_report(self) -> Optional[JsonDict]:
        for record in reversed(self.actions):
            if record["operation"] == "analysis_completed":
                report = record["payload"].get("report")
                return dict(report) if isinstance(report, Mapping) else None
        return None

    @property
    def latest_actions(self) -> Dict[str, JsonDict]:
        latest: Dict[str, JsonDict] = {}
        for record in self.actions:
            latest[str(record["semantic_key"])] = record
        return latest


class CheckpointBundle:
    """Three fsync-backed JSONL journals plus an exact compatibility manifest."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self.frontier_path = self.root / "frontier.jsonl"
        self.hypotheses_path = self.root / "hypotheses.jsonl"
        self.actions_path = self.root / "investigation-actions.jsonl"
        self._paths = {
            "frontier": self.frontier_path,
            "hypotheses": self.hypotheses_path,
            "actions": self.actions_path,
        }
        self._next_sequence: Dict[str, int] = {}
        self._last_hash: Dict[str, str] = {}

    def initialize(self, config: Mapping[str, Any]) -> None:
        validated = validate_checkpoint_config(config)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.exists():
            persisted = self._read_manifest()
            self._assert_compatible(persisted, validated)
        else:
            self._atomic_write_json(self.manifest_path, validated)
        for journal, path in self._paths.items():
            records, corrupt_tail = self._read_journal(journal, path)
            if corrupt_tail:
                self._truncate_incomplete_tail(path)
                records, remaining_corruption = self._read_journal(journal, path)
                if remaining_corruption:
                    raise CheckpointCorruptionError(
                        "{0} journal tail could not be repaired".format(journal)
                    )
            self._next_sequence[journal] = len(records) + 1
            self._last_hash[journal] = str(records[-1]["record_hash"]) if records else ""

    def record_frontier(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._append("frontier", operation, semantic_key, payload)

    def record_hypothesis(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._append("hypotheses", operation, semantic_key, payload)

    def record_action(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._append("actions", operation, semantic_key, payload)

    def restore(self, *, expected_config: Optional[Mapping[str, Any]] = None) -> CheckpointState:
        config = self._read_manifest()
        if expected_config is not None:
            self._assert_compatible(config, validate_checkpoint_config(expected_config))
        frontier, frontier_corrupt = self._read_journal("frontier", self.frontier_path)
        hypotheses, hypothesis_corrupt = self._read_journal(
            "hypotheses", self.hypotheses_path
        )
        actions, action_corrupt = self._read_journal("actions", self.actions_path)
        return CheckpointState(
            config=config,
            frontier_records=tuple(frontier),
            hypothesis_records=tuple(hypotheses),
            actions=tuple(actions),
            corrupt_entries=frontier_corrupt + hypothesis_corrupt + action_corrupt,
        )

    def flush_all(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self._paths.values():
            if not path.exists():
                continue
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _append(
        self,
        journal: str,
        operation: str,
        semantic_key: str,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        if journal not in self._paths:
            raise ValueError("unsupported journal")
        if not self.manifest_path.exists():
            raise CheckpointCompatibilityError("checkpoint must be initialized before append")
        if not isinstance(payload, Mapping):
            raise TypeError("journal payload must be an object")
        operation = str(operation).strip()
        semantic_key = str(semantic_key).strip()
        if not operation or not semantic_key:
            raise ValueError("journal operation and semantic key must be non-empty")
        if journal not in self._next_sequence:
            records, _ = self._read_journal(journal, self._paths[journal])
            self._next_sequence[journal] = len(records) + 1
            self._last_hash[journal] = str(records[-1]["record_hash"]) if records else ""
        unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "journal": journal,
            "sequence": self._next_sequence[journal],
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operation": operation,
            "semantic_key": semantic_key,
            "payload": json.loads(stable_json(payload)),
            "previous_hash": self._last_hash[journal],
        }
        record = {**unsigned, "record_hash": _sha256(unsigned)}
        path = self._paths[journal]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._next_sequence[journal] += 1
        self._last_hash[journal] = str(record["record_hash"])
        return record

    def _read_manifest(self) -> JsonDict:
        if not self.manifest_path.exists():
            raise CheckpointCompatibilityError("checkpoint manifest is missing")
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointCorruptionError("checkpoint manifest is unreadable") from exc
        return validate_checkpoint_config(value)

    @staticmethod
    def _assert_compatible(persisted: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
        if persisted.get("config_fingerprint") != expected.get("config_fingerprint"):
            changed = [
                key
                for key in sorted(CHECKPOINT_CONFIG_KEYS - {"config_fingerprint"})
                if persisted.get(key) != expected.get(key)
            ]
            raise CheckpointCompatibilityError(
                "checkpoint configuration is stale or incompatible: {0}".format(
                    ", ".join(changed) or "fingerprint"
                )
            )

    def _read_journal(self, journal: str, path: Path) -> Tuple[List[JsonDict], int]:
        if not path.exists():
            return [], 0
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CheckpointCorruptionError("cannot read {0} journal".format(journal)) from exc
        if not raw:
            return [], 0
        lines = raw.splitlines(keepends=True)
        records: List[JsonDict] = []
        corrupt_tail = 0
        expected_sequence = 1
        previous_hash = ""
        for index, line in enumerate(lines):
            is_last = index == len(lines) - 1
            complete_line = line.endswith(b"\n")
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if is_last and not complete_line:
                    corrupt_tail += 1
                    break
                raise CheckpointCorruptionError(
                    "{0} journal contains corrupt interior JSON at line {1}".format(
                        journal, index + 1
                    )
                ) from exc
            record = _exact_mapping(value, JOURNAL_RECORD_KEYS, "journal record")
            if record["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
                raise CheckpointCorruptionError("journal schema version mismatch")
            if record["journal"] != journal:
                raise CheckpointCorruptionError("journal identity mismatch")
            if type(record["sequence"]) is not int or record["sequence"] != expected_sequence:
                raise CheckpointCorruptionError("journal sequence is reordered or non-monotonic")
            if not isinstance(record["timestamp"], str) or not record["timestamp"]:
                raise CheckpointCorruptionError("journal timestamp is invalid")
            if not isinstance(record["operation"], str) or not record["operation"]:
                raise CheckpointCorruptionError("journal operation is invalid")
            if not isinstance(record["semantic_key"], str) or not record["semantic_key"]:
                raise CheckpointCorruptionError("journal semantic key is invalid")
            if not isinstance(record["payload"], Mapping):
                raise CheckpointCorruptionError("journal payload must be an object")
            if record["previous_hash"] != previous_hash:
                raise CheckpointCorruptionError("journal hash chain is broken")
            unsigned = {key: record[key] for key in record if key != "record_hash"}
            if record["record_hash"] != _sha256(unsigned):
                raise CheckpointCorruptionError("journal record hash does not match contents")
            records.append(record)
            expected_sequence += 1
            previous_hash = str(record["record_hash"])
        return records, corrupt_tail

    def _atomic_write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".{0}.".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _truncate_incomplete_tail(path: Path) -> None:
        raw = path.read_bytes()
        valid_end = raw.rfind(b"\n") + 1
        with path.open("r+b") as handle:
            handle.truncate(valid_end)
            handle.flush()
            os.fsync(handle.fileno())
