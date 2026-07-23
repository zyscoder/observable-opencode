"""Durable transactional checkpoints for passive recursive attribution."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
)
from .graph import EVIDENCE_ELIGIBILITY_POLICY_IDENTITY
from .models import JsonDict, stable_json


CHECKPOINT_SCHEMA_VERSION = "recursive-attribution-checkpoint/v8"
OUTPUT_SCHEMA_VERSION = "recursive-attribution-output/v2"
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
        "runtime_identity",
        "evidence_eligibility_policy",
        "global_judgment_contract",
        "root_confirmation_contract",
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
CHECKPOINT_RUNTIME_KEYS = frozenset(
    {
        "judge_timeout_sec",
        "judge_max_tokens",
        "thinking_mode",
        "base_url",
        "provider_error_threshold",
    }
)
MANIFEST_KEYS = frozenset(
    {"schema_version", "run_id", "config", "manifest_hash"}
)
JOURNAL_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "journal",
        "sequence",
        "transaction_sequence",
        "timestamp",
        "operation",
        "semantic_key",
        "payload",
        "previous_hash",
        "record_hash",
    }
)
JOURNAL_HEAD_KEYS = frozenset({"count", "sequence", "record_hash"})
TAIL_REPAIR_EVENT_KEYS = frozenset(
    {"journal", "operation", "removed_bytes", "timestamp"}
)
COMMIT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "transaction_sequence",
        "timestamp",
        "semantic_key",
        "journal_heads",
        "previous_commit_hash",
        "tail_repair_count",
        "tail_repair_events",
        "commit_hash",
    }
)
OUTPUT_COMMIT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "transaction_id",
        "status",
        "attribution_path",
        "attribution_hash",
        "attribution_temp_path",
        "lineage_path",
        "lineage_hash",
        "lineage_temp_path",
        "output_commit_hash",
    }
)
JOURNAL_NAMES = ("frontier", "hypotheses", "actions")


class CheckpointError(ValueError):
    """Base class for deterministic checkpoint failures."""


class CheckpointCompatibilityError(CheckpointError):
    """Raised when resume inputs differ from the checkpoint manifest."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when durable checkpoint identity or hash validation fails."""


def _sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
            raise CheckpointCompatibilityError(
                "{0} must be a non-negative integer".format(key)
            )
        output[key] = amount
    return output


def _validated_runtime(value: Any) -> JsonDict:
    runtime = _exact_mapping(value, CHECKPOINT_RUNTIME_KEYS, "checkpoint runtime identity")
    timeout = runtime["judge_timeout_sec"]
    max_tokens = runtime["judge_max_tokens"]
    threshold = runtime["provider_error_threshold"]
    if type(timeout) not in (int, float) or timeout <= 0:
        raise CheckpointCompatibilityError("judge_timeout_sec must be positive")
    if type(max_tokens) is not int or max_tokens < 1:
        raise CheckpointCompatibilityError("judge_max_tokens must be positive")
    if type(threshold) is not int or threshold < 1:
        raise CheckpointCompatibilityError("provider_error_threshold must be positive")
    for key in ("thinking_mode", "base_url"):
        if not isinstance(runtime[key], str):
            raise CheckpointCompatibilityError("{0} must be a string".format(key))
    return {
        "judge_timeout_sec": float(timeout),
        "judge_max_tokens": max_tokens,
        "thinking_mode": runtime["thinking_mode"],
        "base_url": runtime["base_url"],
        "provider_error_threshold": threshold,
    }


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
    runtime_identity: Mapping[str, Any],
) -> JsonDict:
    """Build the exact behavior compatibility envelope for a resumable run."""

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
        "runtime_identity": _validated_runtime(runtime_identity),
        "evidence_eligibility_policy": EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        "global_judgment_contract": GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        "root_confirmation_contract": ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    }
    return {**semantic, "config_fingerprint": _sha256(semantic)}


def validate_checkpoint_config(value: Mapping[str, Any]) -> JsonDict:
    if not isinstance(value, Mapping):
        raise CheckpointCorruptionError("checkpoint config must be an object")
    if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointCompatibilityError("unsupported checkpoint schema version")
    config = _exact_mapping(value, CHECKPOINT_CONFIG_KEYS, "checkpoint config")
    start_refs = config["start_refs"]
    if not isinstance(start_refs, list) or any(not isinstance(item, str) for item in start_refs):
        raise CheckpointCompatibilityError("start_refs must be a list of strings")
    config["budgets"] = _validated_budgets(config["budgets"])
    config["runtime_identity"] = _validated_runtime(config["runtime_identity"])
    for key in (
        "case_id",
        "trace_fingerprint",
        "objective",
        "analysis_perspective",
        "model_identity",
        "cache_identity",
        "evidence_eligibility_policy",
        "global_judgment_contract",
        "root_confirmation_contract",
        "config_fingerprint",
    ):
        if not isinstance(config[key], str):
            raise CheckpointCompatibilityError("{0} must be a string".format(key))
    if config["evidence_eligibility_policy"] != EVIDENCE_ELIGIBILITY_POLICY_IDENTITY:
        raise CheckpointCompatibilityError(
            "unsupported graph evidence eligibility policy"
        )
    if config["global_judgment_contract"] != GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION:
        raise CheckpointCompatibilityError(
            "unsupported global judgment contract"
        )
    if config["root_confirmation_contract"] != ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION:
        raise CheckpointCompatibilityError(
            "unsupported root confirmation contract"
        )
    semantic = {key: config[key] for key in config if key != "config_fingerprint"}
    if config["config_fingerprint"] != _sha256(semantic):
        raise CheckpointCorruptionError("checkpoint config fingerprint does not match contents")
    return config


@dataclass(frozen=True)
class CheckpointState:
    config: JsonDict
    run_id: str
    transaction_sequence: int
    frontier_records: Tuple[JsonDict, ...]
    hypothesis_records: Tuple[JsonDict, ...]
    actions: Tuple[JsonDict, ...]
    corrupt_entries: int = 0
    tail_repair_count: int = 0
    tail_repair_events: Tuple[JsonDict, ...] = ()

    @property
    def frontier_payload(self) -> JsonDict:
        return dict(self.frontier_records[-1]["payload"]) if self.frontier_records else {}

    @property
    def hypothesis_payload(self) -> JsonDict:
        return dict(self.hypothesis_records[-1]["payload"]) if self.hypothesis_records else {}

    @property
    def inflight_actions(self) -> Tuple[JsonDict, ...]:
        latest: Dict[str, JsonDict] = {}
        for record in self.actions:
            latest[str(record["semantic_key"])] = record
        return tuple(
            record
            for _, record in sorted(latest.items())
            if str(record["operation"]).endswith("_started")
        )

    @property
    def pending_report(self) -> Optional[JsonDict]:
        record = self.latest_actions.get("analysis:result")
        if record is not None and record["operation"] == "analysis_ready":
            report = record["payload"].get("report")
            return dict(report) if isinstance(report, Mapping) else None
        return None

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
    """Three fsync journals governed by one atomically published commit head."""

    def __init__(
        self,
        root: Path,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"
        self.commit_path = self.root / "commit.json"
        self.output_commit_path = self.root / "output-commit.json"
        self.frontier_path = self.root / "frontier.jsonl"
        self.hypotheses_path = self.root / "hypotheses.jsonl"
        self.actions_path = self.root / "investigation-actions.jsonl"
        self._paths = {
            "frontier": self.frontier_path,
            "hypotheses": self.hypotheses_path,
            "actions": self.actions_path,
        }
        self._fault_hook = fault_hook or (lambda _stage: None)
        self._run_id = ""
        self._next_sequence: Dict[str, int] = {}
        self._last_hash: Dict[str, str] = {}
        self._transaction_sequence = 0
        self._commit: JsonDict = {}

    def initialize(self, config: Mapping[str, Any]) -> None:
        validated = validate_checkpoint_config(config)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.manifest_path.exists():
            manifest = self._read_manifest_envelope()
            self._assert_compatible(manifest["config"], validated)
            self._run_id = str(manifest["run_id"])
        else:
            self._run_id = str(uuid.uuid4())
            unsigned = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": self._run_id,
                "config": validated,
            }
            self._atomic_write_json(
                self.manifest_path, {**unsigned, "manifest_hash": _sha256(unsigned)}
            )

        for path in self._paths.values():
            with path.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        self._fsync_directory(self.root)
        self._fault_hook("checkpoint_directory_durable")

        if self.commit_path.exists():
            self._commit = self._read_commit()
        else:
            self._commit = self._new_commit(
                transaction_sequence=0,
                semantic_key="checkpoint:initialized",
                journal_heads={name: self._empty_head() for name in JOURNAL_NAMES},
                previous_commit_hash="",
                tail_repair_count=0,
                tail_repair_events=[],
            )
            self._atomic_write_json(self.commit_path, self._commit)

        self._validate_commit_identity(self._commit)
        repairs: List[JsonDict] = []
        for journal, path in self._paths.items():
            repairs.extend(self._repair_to_committed_head(journal, path))
        if repairs:
            previous = str(self._commit["commit_hash"])
            self._commit = self._new_commit(
                transaction_sequence=int(self._commit["transaction_sequence"]) + 1,
                semantic_key="checkpoint:tail_repair",
                journal_heads=dict(self._commit["journal_heads"]),
                previous_commit_hash=previous,
                tail_repair_count=int(self._commit["tail_repair_count"]) + len(repairs),
                tail_repair_events=list(self._commit["tail_repair_events"]) + repairs,
            )
            self._atomic_write_json(self.commit_path, self._commit)
        self._load_append_heads()

    def record_frontier(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._commit_one("frontier", operation, semantic_key, payload)

    def record_hypothesis(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._commit_one("hypotheses", operation, semantic_key, payload)

    def record_action(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._commit_one("actions", operation, semantic_key, payload)

    def commit_snapshot(
        self,
        *,
        semantic_key: str,
        frontier_payload: Mapping[str, Any],
        hypothesis_payload: Mapping[str, Any],
        action_payload: Mapping[str, Any],
    ) -> JsonDict:
        self._require_initialized()
        transaction = self._transaction_sequence + 1
        members = (
            ("frontier", "snapshot", frontier_payload),
            ("hypotheses", "snapshot", hypothesis_payload),
            ("actions", "state_snapshot", action_payload),
        )
        for journal, operation, payload in members:
            self._append_member(
                journal, operation, semantic_key, payload, transaction_sequence=transaction
            )
            self._fault_hook("snapshot_after_{0}".format(journal))
        return self._publish_commit(transaction, semantic_key)

    def restore(self, *, expected_config: Optional[Mapping[str, Any]] = None) -> CheckpointState:
        manifest = self._read_manifest_envelope()
        self._run_id = str(manifest["run_id"])
        config = manifest["config"]
        if expected_config is not None:
            self._assert_compatible(config, validate_checkpoint_config(expected_config))
        commit = self._read_commit()
        if commit["run_id"] != manifest["run_id"]:
            raise CheckpointCorruptionError("checkpoint manifest and commit run IDs differ")
        self._validate_commit_identity(commit)
        committed: Dict[str, List[JsonDict]] = {}
        incomplete = 0
        for journal, path in self._paths.items():
            records, corrupt_tail, complete = self._read_journal(journal, path)
            incomplete += corrupt_tail
            if not complete and len(records) < int(commit["journal_heads"][journal]["count"]):
                raise CheckpointCorruptionError(
                    "{0} journal committed tail is incomplete".format(journal)
                )
            committed[journal] = self._committed_prefix(journal, records, commit)
        return CheckpointState(
            config=dict(config),
            run_id=str(commit["run_id"]),
            transaction_sequence=int(commit["transaction_sequence"]),
            frontier_records=tuple(committed["frontier"]),
            hypothesis_records=tuple(committed["hypotheses"]),
            actions=tuple(committed["actions"]),
            corrupt_entries=incomplete,
            tail_repair_count=int(commit["tail_repair_count"]),
            tail_repair_events=tuple(dict(item) for item in commit["tail_repair_events"]),
        )

    def mark_analysis_completed(
        self, *, report: Mapping[str, Any], output_commit: Mapping[str, Any]
    ) -> JsonDict:
        persisted = _read_exact_json(self.output_commit_path, "output commit")
        validated = _validate_output_commit(persisted, run_id=self._run_id)
        if validated != dict(output_commit) or validated["status"] != "published":
            raise CheckpointCorruptionError("analysis output transaction is not fully published")
        for path_key, hash_key in (
            ("attribution_path", "attribution_hash"),
            ("lineage_path", "lineage_hash"),
        ):
            path = Path(str(validated[path_key]))
            if not path.is_file() or _file_sha256(path) != validated[hash_key]:
                raise CheckpointCorruptionError("published output is missing or changed")
        report_bytes = _json_bytes(report)
        report_hash = hashlib.sha256(report_bytes).hexdigest()
        if report_hash != validated["attribution_hash"]:
            raise CheckpointCorruptionError(
                "completion report does not match published attribution"
            )
        attribution_path = Path(str(validated["attribution_path"]))
        if attribution_path.read_bytes() != report_bytes:
            raise CheckpointCorruptionError(
                "completion report bytes do not match published attribution"
            )
        return self.record_action(
            "analysis_completed",
            "analysis:result",
            {
                "report": json.loads(stable_json(report)),
                "interrupted": False,
                "output_transaction_id": validated["transaction_id"],
                "output_commit_hash": validated["output_commit_hash"],
                "attribution_hash": validated["attribution_hash"],
                "lineage_hash": validated["lineage_hash"],
                "lineage_path": validated["lineage_path"],
            },
        )

    def flush_all(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (*self._paths.values(), self.manifest_path, self.commit_path):
            if not path.exists():
                continue
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._fsync_directory(self.root)

    def _commit_one(
        self,
        journal: str,
        operation: str,
        semantic_key: str,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        self._require_initialized()
        transaction = self._transaction_sequence + 1
        record = self._append_member(
            journal,
            operation,
            semantic_key,
            payload,
            transaction_sequence=transaction,
        )
        self._publish_commit(transaction, semantic_key)
        return record

    def _append_member(
        self,
        journal: str,
        operation: str,
        semantic_key: str,
        payload: Mapping[str, Any],
        *,
        transaction_sequence: int,
    ) -> JsonDict:
        if journal not in self._paths:
            raise ValueError("unsupported journal")
        if not isinstance(payload, Mapping):
            raise TypeError("journal payload must be an object")
        operation = str(operation).strip()
        semantic_key = str(semantic_key).strip()
        if not operation or not semantic_key:
            raise ValueError("journal operation and semantic key must be non-empty")
        unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": self._run_id,
            "journal": journal,
            "sequence": self._next_sequence[journal],
            "transaction_sequence": transaction_sequence,
            "timestamp": _timestamp(),
            "operation": operation,
            "semantic_key": semantic_key,
            "payload": json.loads(stable_json(payload)),
            "previous_hash": self._last_hash[journal],
        }
        record = {**unsigned, "record_hash": _sha256(unsigned)}
        with self._paths[journal].open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._next_sequence[journal] += 1
        self._last_hash[journal] = str(record["record_hash"])
        return record

    def _publish_commit(
        self, transaction_sequence: int, semantic_key: str
    ) -> JsonDict:
        heads = {
            name: {
                "count": self._next_sequence[name] - 1,
                "sequence": self._next_sequence[name] - 1,
                "record_hash": self._last_hash[name],
            }
            for name in JOURNAL_NAMES
        }
        commit = self._new_commit(
            transaction_sequence=transaction_sequence,
            semantic_key=semantic_key,
            journal_heads=heads,
            previous_commit_hash=str(self._commit.get("commit_hash") or ""),
            tail_repair_count=int(self._commit.get("tail_repair_count") or 0),
            tail_repair_events=list(self._commit.get("tail_repair_events") or []),
        )
        self._atomic_write_json(self.commit_path, commit)
        self._commit = commit
        self._transaction_sequence = transaction_sequence
        self._fault_hook("commit_published")
        return dict(commit)

    def _new_commit(
        self,
        *,
        transaction_sequence: int,
        semantic_key: str,
        journal_heads: Mapping[str, Any],
        previous_commit_hash: str,
        tail_repair_count: int,
        tail_repair_events: Sequence[Mapping[str, Any]],
    ) -> JsonDict:
        heads = {
            name: _exact_mapping(journal_heads[name], JOURNAL_HEAD_KEYS, "journal head")
            for name in JOURNAL_NAMES
        }
        unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": self._run_id,
            "transaction_sequence": transaction_sequence,
            "timestamp": _timestamp(),
            "semantic_key": str(semantic_key),
            "journal_heads": heads,
            "previous_commit_hash": str(previous_commit_hash),
            "tail_repair_count": tail_repair_count,
            "tail_repair_events": [json.loads(stable_json(item)) for item in tail_repair_events],
        }
        return {**unsigned, "commit_hash": _sha256(unsigned)}

    def _read_manifest_envelope(self) -> JsonDict:
        value = _exact_mapping(
            _read_exact_json(self.manifest_path, "checkpoint manifest"),
            MANIFEST_KEYS,
            "checkpoint manifest",
        )
        unsigned = {key: value[key] for key in value if key != "manifest_hash"}
        if value["manifest_hash"] != _sha256(unsigned):
            raise CheckpointCorruptionError("checkpoint manifest hash does not match contents")
        if value["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointCompatibilityError("unsupported checkpoint manifest version")
        if not isinstance(value["run_id"], str) or not value["run_id"]:
            raise CheckpointCorruptionError("checkpoint run ID is invalid")
        value["config"] = validate_checkpoint_config(value["config"])
        return value

    def _read_commit(self) -> JsonDict:
        value = _exact_mapping(
            _read_exact_json(self.commit_path, "checkpoint commit"),
            COMMIT_KEYS,
            "checkpoint commit",
        )
        unsigned = {key: value[key] for key in value if key != "commit_hash"}
        if value["commit_hash"] != _sha256(unsigned):
            raise CheckpointCorruptionError("checkpoint commit hash does not match contents")
        if value["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointCompatibilityError("unsupported checkpoint commit version")
        for key in ("run_id", "timestamp", "semantic_key", "previous_commit_hash"):
            if not isinstance(value[key], str):
                raise CheckpointCorruptionError(
                    "checkpoint commit field is invalid: {0}".format(key)
                )
        if not value["run_id"] or not value["timestamp"] or not value["semantic_key"]:
            raise CheckpointCorruptionError("checkpoint commit identity is incomplete")
        if type(value["transaction_sequence"]) is not int or value["transaction_sequence"] < 0:
            raise CheckpointCorruptionError("checkpoint transaction sequence is invalid")
        if type(value["tail_repair_count"]) is not int or value["tail_repair_count"] < 0:
            raise CheckpointCorruptionError("checkpoint repair count is invalid")
        if not isinstance(value["tail_repair_events"], list):
            raise CheckpointCorruptionError("checkpoint repair events are invalid")
        repairs: List[JsonDict] = []
        for raw_event in value["tail_repair_events"]:
            event = _exact_mapping(
                raw_event, TAIL_REPAIR_EVENT_KEYS, "checkpoint repair event"
            )
            if event["journal"] not in JOURNAL_NAMES:
                raise CheckpointCorruptionError("checkpoint repair journal is invalid")
            if not isinstance(event["operation"], str) or not event["operation"]:
                raise CheckpointCorruptionError("checkpoint repair operation is invalid")
            if type(event["removed_bytes"]) is not int or event["removed_bytes"] < 0:
                raise CheckpointCorruptionError("checkpoint repair byte count is invalid")
            if not isinstance(event["timestamp"], str) or not event["timestamp"]:
                raise CheckpointCorruptionError("checkpoint repair timestamp is invalid")
            repairs.append(event)
        if len(repairs) != value["tail_repair_count"]:
            raise CheckpointCorruptionError("checkpoint repair count does not match events")
        value["tail_repair_events"] = repairs
        heads = _exact_mapping(
            value["journal_heads"], frozenset(JOURNAL_NAMES), "checkpoint journal heads"
        )
        value["journal_heads"] = {
            name: self._validate_head(heads[name]) for name in JOURNAL_NAMES
        }
        return value

    def _validate_commit_identity(self, commit: Mapping[str, Any]) -> None:
        if commit["run_id"] != self._run_id:
            raise CheckpointCorruptionError("checkpoint run IDs are mixed")

    @staticmethod
    def _validate_head(value: Any) -> JsonDict:
        head = _exact_mapping(value, JOURNAL_HEAD_KEYS, "journal head")
        if type(head["count"]) is not int or head["count"] < 0:
            raise CheckpointCorruptionError("journal head count is invalid")
        if type(head["sequence"]) is not int or head["sequence"] != head["count"]:
            raise CheckpointCorruptionError("journal head sequence is invalid")
        if not isinstance(head["record_hash"], str):
            raise CheckpointCorruptionError("journal head hash is invalid")
        if head["count"] == 0 and head["record_hash"]:
            raise CheckpointCorruptionError("empty journal head has a hash")
        if head["count"] > 0 and not head["record_hash"]:
            raise CheckpointCorruptionError("non-empty journal head has no hash")
        return head

    @staticmethod
    def _empty_head() -> JsonDict:
        return {"count": 0, "sequence": 0, "record_hash": ""}

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

    def _read_journal(
        self, journal: str, path: Path
    ) -> Tuple[List[JsonDict], int, bool]:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise CheckpointCorruptionError("cannot read {0} journal".format(journal)) from exc
        if not raw:
            return [], 0, True
        complete = raw.endswith(b"\n")
        lines = raw.splitlines(keepends=True)
        records: List[JsonDict] = []
        expected_sequence = 1
        previous_transaction = 0
        previous_hash = ""
        for index, line in enumerate(lines):
            is_last = index == len(lines) - 1
            complete_line = line.endswith(b"\n")
            if is_last and not complete_line:
                return records, 1, False
            try:
                value = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CheckpointCorruptionError(
                    "{0} journal contains corrupt interior JSON at line {1}".format(
                        journal, index + 1
                    )
                ) from exc
            record = _exact_mapping(value, JOURNAL_RECORD_KEYS, "journal record")
            if record["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
                raise CheckpointCorruptionError("journal schema version mismatch")
            if record["run_id"] != self._run_id:
                raise CheckpointCorruptionError("journal run ID does not match checkpoint")
            if record["journal"] != journal:
                raise CheckpointCorruptionError("journal identity mismatch")
            if type(record["sequence"]) is not int or record["sequence"] != expected_sequence:
                raise CheckpointCorruptionError("journal sequence is reordered or non-monotonic")
            if (
                type(record["transaction_sequence"]) is not int
                or record["transaction_sequence"] <= previous_transaction
            ):
                raise CheckpointCorruptionError("journal transaction sequence is invalid")
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
            previous_transaction = int(record["transaction_sequence"])
            previous_hash = str(record["record_hash"])
        return records, 0, complete

    def _committed_prefix(
        self, journal: str, records: Sequence[JsonDict], commit: Mapping[str, Any]
    ) -> List[JsonDict]:
        head = commit["journal_heads"][journal]
        count = int(head["count"])
        if len(records) < count:
            raise CheckpointCorruptionError(
                "{0} journal is shorter than its committed head".format(journal)
            )
        prefix = list(records[:count])
        actual_hash = str(prefix[-1]["record_hash"]) if prefix else ""
        if actual_hash != head["record_hash"]:
            raise CheckpointCorruptionError(
                "{0} journal committed head hash does not match".format(journal)
            )
        if prefix and int(prefix[-1]["transaction_sequence"]) > int(commit["transaction_sequence"]):
            raise CheckpointCorruptionError("journal head exceeds committed transaction")
        return prefix

    def _repair_to_committed_head(self, journal: str, path: Path) -> List[JsonDict]:
        repairs: List[JsonDict] = []
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            valid_end = raw.rfind(b"\n") + 1
            tail = raw[valid_end:]
            repair_kind = "truncated_incomplete_tail"
            try:
                candidate = json.loads(tail.decode("utf-8"))
                record = _exact_mapping(candidate, JOURNAL_RECORD_KEYS, "journal record")
                count = int(self._commit["journal_heads"][journal]["count"])
                if (
                    record.get("run_id") == self._run_id
                    and record.get("journal") == journal
                    and record.get("sequence") == count
                    and record.get("record_hash")
                    == self._commit["journal_heads"][journal]["record_hash"]
                ):
                    with path.open("ab") as handle:
                        handle.write(b"\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    repair_kind = "repaired_missing_newline"
                else:
                    self._durable_truncate(path, valid_end)
            except (UnicodeDecodeError, json.JSONDecodeError, CheckpointCorruptionError):
                self._durable_truncate(path, valid_end)
            repairs.append(
                {
                    "journal": journal,
                    "operation": repair_kind,
                    "removed_bytes": 0 if repair_kind == "repaired_missing_newline" else len(tail),
                    "timestamp": _timestamp(),
                }
            )
        records, corrupt, complete = self._read_journal(journal, path)
        if corrupt or not complete:
            raise CheckpointCorruptionError("{0} journal tail repair failed".format(journal))
        prefix = self._committed_prefix(journal, records, self._commit)
        if len(records) > len(prefix):
            removed = len(records) - len(prefix)
            end = self._byte_end_for_record_count(path, len(prefix))
            self._durable_truncate(path, end)
            for _ in range(removed):
                repairs.append(
                    {
                        "journal": journal,
                        "operation": "truncated_uncommitted_record",
                        "removed_bytes": 0,
                        "timestamp": _timestamp(),
                    }
                )
        return repairs

    def _load_append_heads(self) -> None:
        self._transaction_sequence = int(self._commit["transaction_sequence"])
        for journal, path in self._paths.items():
            records, corrupt, complete = self._read_journal(journal, path)
            if corrupt or not complete:
                raise CheckpointCorruptionError("journal is not append-safe")
            committed = self._committed_prefix(journal, records, self._commit)
            self._next_sequence[journal] = len(committed) + 1
            self._last_hash[journal] = (
                str(committed[-1]["record_hash"]) if committed else ""
            )

    def _require_initialized(self) -> None:
        if not self._run_id or not self._commit:
            raise CheckpointCompatibilityError("checkpoint must be initialized before append")

    @staticmethod
    def _byte_end_for_record_count(path: Path, count: int) -> int:
        if count == 0:
            return 0
        raw = path.read_bytes()
        position = 0
        for _ in range(count):
            newline = raw.find(b"\n", position)
            if newline < 0:
                raise CheckpointCorruptionError("committed journal delimiter is missing")
            position = newline + 1
        return position

    @staticmethod
    def _durable_truncate(path: Path, size: int) -> None:
        with path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        _atomic_write_json(path, value)


def publish_output_transaction(
    *,
    bundle: CheckpointBundle,
    attribution_path: Path,
    lineage_path: Path,
    report: Mapping[str, Any],
    message_lineage: Mapping[str, Any],
    stop_requested: Optional[Callable[[], bool]] = None,
    fault_hook: Optional[Callable[[str], None]] = None,
) -> JsonDict:
    """Durably publish the report and lineage as one recoverable output transaction."""

    del stop_requested  # Signals request shutdown; they do not split a durable publish.
    hook = fault_hook or (lambda _stage: None)
    bundle._require_initialized()
    attribution_path = Path(attribution_path).resolve()
    lineage_path = Path(lineage_path).resolve()
    if attribution_path == lineage_path:
        raise CheckpointCompatibilityError(
            "attribution and lineage outputs must use distinct paths"
        )
    attribution_bytes = _json_bytes(report)
    lineage_bytes = _json_bytes(message_lineage)
    attribution_hash = hashlib.sha256(attribution_bytes).hexdigest()
    lineage_hash = hashlib.sha256(lineage_bytes).hexdigest()

    existing: Optional[JsonDict] = None
    if bundle.output_commit_path.exists():
        existing = _validate_output_commit(
            _read_exact_json(bundle.output_commit_path, "output commit"),
            run_id=bundle._run_id,
        )
        expected_identity = (
            str(attribution_path), attribution_hash, str(lineage_path), lineage_hash
        )
        actual_identity = (
            existing["attribution_path"],
            existing["attribution_hash"],
            existing["lineage_path"],
            existing["lineage_hash"],
        )
        if actual_identity != expected_identity and existing["status"] == "published":
            existing = None
        elif actual_identity != expected_identity:
            raise CheckpointCompatibilityError("output transaction inputs changed on resume")

    if existing is None:
        transaction_id = str(uuid.uuid4())
        attr_temp = attribution_path.parent / ".{0}.{1}.pending".format(
            attribution_path.name, transaction_id
        )
        lineage_temp = lineage_path.parent / ".{0}.{1}.pending".format(
            lineage_path.name, transaction_id
        )
        _write_staged_file(attr_temp, attribution_bytes)
        _write_staged_file(lineage_temp, lineage_bytes)
        unsigned = {
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "run_id": bundle._run_id,
            "transaction_id": transaction_id,
            "status": "staged",
            "attribution_path": str(attribution_path),
            "attribution_hash": attribution_hash,
            "attribution_temp_path": str(attr_temp),
            "lineage_path": str(lineage_path),
            "lineage_hash": lineage_hash,
            "lineage_temp_path": str(lineage_temp),
        }
        existing = {**unsigned, "output_commit_hash": _sha256(unsigned)}
        _atomic_write_json(bundle.output_commit_path, existing)

    for label, path_key, hash_key, temp_key in (
        ("attribution", "attribution_path", "attribution_hash", "attribution_temp_path"),
        ("lineage", "lineage_path", "lineage_hash", "lineage_temp_path"),
    ):
        final_path = Path(str(existing[path_key]))
        temp_path = Path(str(existing[temp_key]))
        if final_path.is_file() and _file_sha256(final_path) == existing[hash_key]:
            pass
        elif temp_path.is_file() and _file_sha256(temp_path) == existing[hash_key]:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(temp_path), str(final_path))
            CheckpointBundle._fsync_directory(final_path.parent)
        else:
            raise CheckpointCorruptionError(
                "cannot repair missing {0} output publication".format(label)
            )
        hook("output_after_{0}_publish".format(label))

    unsigned = {key: existing[key] for key in existing if key != "output_commit_hash"}
    unsigned["status"] = "published"
    published = {**unsigned, "output_commit_hash": _sha256(unsigned)}
    _atomic_write_json(bundle.output_commit_path, published)
    return published


def _validate_output_commit(value: Any, *, run_id: str) -> JsonDict:
    commit = _exact_mapping(value, OUTPUT_COMMIT_KEYS, "output commit")
    unsigned = {key: commit[key] for key in commit if key != "output_commit_hash"}
    if commit["output_commit_hash"] != _sha256(unsigned):
        raise CheckpointCorruptionError("output commit hash does not match contents")
    if commit["schema_version"] != OUTPUT_SCHEMA_VERSION:
        raise CheckpointCompatibilityError("unsupported output commit version")
    if commit["run_id"] != run_id:
        raise CheckpointCorruptionError("output and checkpoint run IDs differ")
    if commit["status"] not in {"staged", "published"}:
        raise CheckpointCorruptionError("output commit status is invalid")
    for key in (
        "transaction_id",
        "attribution_path",
        "attribution_hash",
        "attribution_temp_path",
        "lineage_path",
        "lineage_hash",
        "lineage_temp_path",
    ):
        if not isinstance(commit[key], str) or not commit[key]:
            raise CheckpointCorruptionError("output commit field is invalid: {0}".format(key))
    return commit


def _read_exact_json(path: Path, label: str) -> JsonDict:
    if not path.exists():
        raise CheckpointCompatibilityError("{0} is missing".format(label))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointCorruptionError("{0} is unreadable".format(label)) from exc
    if not isinstance(value, Mapping):
        raise CheckpointCorruptionError("{0} must be an object".format(label))
    return dict(value)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_staged_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    CheckpointBundle._fsync_directory(path.parent)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
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
        CheckpointBundle._fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
