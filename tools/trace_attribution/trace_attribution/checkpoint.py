"""Durable transactional checkpoints for passive recursive attribution."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .causal_state import (
    FACTOR_ROLE_CONTRACT,
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
)
from .candidate_paging import global_candidate_paging_policy
from .graph import EVIDENCE_ELIGIBILITY_POLICY_IDENTITY
from .models import JsonDict, stable_json


CHECKPOINT_SCHEMA_VERSION = "recursive-attribution-checkpoint/v29"
LEGACY_CHECKPOINT_SCHEMA_VERSION = "recursive-attribution-checkpoint/v27"
SUPPORTED_CHECKPOINT_SCHEMA_VERSIONS = frozenset(
    {CHECKPOINT_SCHEMA_VERSION, LEGACY_CHECKPOINT_SCHEMA_VERSION}
)
OUTPUT_SCHEMA_VERSION = "recursive-attribution-output/v16"
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
        "global_pagination_policy",
        "root_confirmation_contract",
        "factor_role_contract",
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
        "fusion_mode",
    }
)
LEGACY_CHECKPOINT_RUNTIME_KEYS = CHECKPOINT_RUNTIME_KEYS - {"fusion_mode"}
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
ANALYSIS_COMPLETION_PAYLOAD_KEYS = frozenset(
    {
        "report",
        "interrupted",
        "output_transaction_id",
        "output_commit_hash",
        "attribution_hash",
        "lineage_hash",
        "lineage_path",
    }
)
JOURNAL_NAMES = ("frontier", "hypotheses", "actions")
CHECKPOINT_BLOB_DIRECTORY_NAME = "checkpoint-blobs"
CHECKPOINT_BLOB_ALGORITHM_DIRECTORY = "sha256"
CHECKPOINT_BLOB_SCHEMA_VERSION = "checkpoint-merkle-blob-ref/v1"
CHECKPOINT_PAYLOAD_SCHEMA_VERSION = "checkpoint-merkle-payload/v1"
CHECKPOINT_BLOB_THRESHOLD_BYTES = 16 * 1024
CHECKPOINT_BLOB_REF_KEYS = frozenset(
    {"schema_version", "digest", "byte_length"}
)
CHECKPOINT_PAYLOAD_ENVELOPE_KEY = "__checkpoint_merkle_payload__"
CHECKPOINT_PAYLOAD_ENVELOPE_KEYS = frozenset(
    {"schema_version", "root"}
)
MIGRATION_TRANSACTION_SCHEMA_VERSION = "checkpoint-migration-transaction/v1"
MIGRATION_MARKER_NAME = "migration-transaction.json"
MIGRATION_STAGING_PREFIX = ".migration-v28-"
CURRENT_POINTER_NAME = "CURRENT"
GENERATION_DIRECTORY_NAME = "generations"
GENERATION_PREFIX = "v28-"
GENERATION_WRITE_STAGING_PREFIX = ".checkpoint-v28-"
MIGRATION_TRANSACTION_DIRECTORY_NAME = ".migration-transactions"
CHECKPOINT_LOCK_NAME = ".checkpoint.lock"
MIGRATION_CARRIERS = (
    ("frontier", "frontier.jsonl"),
    ("hypotheses", "hypotheses.jsonl"),
    ("actions", "investigation-actions.jsonl"),
    ("commit", "commit.json"),
    ("manifest", "manifest.json"),
)
MIGRATION_TRANSACTION_KEYS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "source_schema_version",
        "target_schema_version",
        "run_id",
        "source_manifest_hash",
        "target_config_fingerprint",
        "staging_directory",
        "carrier_hashes",
        "transaction_hash",
    }
)
COMPLETED_CHECKPOINT_REPLAY_PROOF_SCHEMA_VERSION = (
    "completed-checkpoint-replay-proof/v1"
)
COMPLETED_CHECKPOINT_MIGRATION_SCHEMA_VERSION = (
    "completed-checkpoint-migration-decision/v2"
)
LEGACY_BYPASSED_GLOBAL_GATE_PROJECTION = (
    "legacy-bypassed-global-gate-projection/v1"
)
LEGACY_PROJECTION_CLASSIFIER_IDENTITY = (
    "exact-legacy-bypassed-global-gate-shape-classifier/v1"
)
LEGACY_PROJECTION_REQUIRED_REASON = (
    "legacy_bypassed_global_gate_projection_required"
)
MIGRATION_REPORT_PROJECTION_KEYS = frozenset(
    {"semantic_anchor_id", "semantic_occurrence_id"}
)


class CheckpointError(ValueError):
    """Base class for deterministic checkpoint failures."""


class CheckpointCompatibilityError(CheckpointError):
    """Raised when resume inputs differ from the checkpoint manifest."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when durable checkpoint identity or hash validation fails."""


class CheckpointLockError(CheckpointError):
    """Raised when the checkpoint interprocess lock cannot be used safely."""


def _sha256(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


FACTOR_ROLE_CONTRACT_IDENTITY = (
    "factor-role-contract/v1:sha256:{0}".format(
        _sha256(
            [
                {
                    str(key): (
                        list(item)
                        if isinstance(item, tuple)
                        else item
                    )
                    for key, item in row.items()
                }
                for row in FACTOR_ROLE_CONTRACT
            ]
        )
    )
)


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


def _validated_runtime(
    value: Any,
    *,
    legacy: bool = False,
    legacy_fusion_mode: Optional[str] = None,
) -> JsonDict:
    expected_keys = (
        LEGACY_CHECKPOINT_RUNTIME_KEYS
        if legacy and isinstance(value, Mapping) and "fusion_mode" not in value
        else CHECKPOINT_RUNTIME_KEYS
    )
    runtime = _exact_mapping(value, expected_keys, "checkpoint runtime identity")
    timeout = runtime["judge_timeout_sec"]
    max_tokens = runtime["judge_max_tokens"]
    threshold = runtime["provider_error_threshold"]
    if type(timeout) not in (int, float) or timeout <= 0:
        raise CheckpointCompatibilityError("judge_timeout_sec must be positive")
    if type(max_tokens) is not int or max_tokens < 1:
        raise CheckpointCompatibilityError("judge_max_tokens must be positive")
    if type(threshold) is not int or threshold < 1:
        raise CheckpointCompatibilityError("provider_error_threshold must be positive")
    if "fusion_mode" in runtime:
        fusion_mode = runtime["fusion_mode"]
        if (
            legacy_fusion_mode is not None
            and fusion_mode != legacy_fusion_mode
        ):
            raise CheckpointCompatibilityError(
                "legacy fusion_mode contradicts migration evidence"
            )
    else:
        if legacy_fusion_mode is None:
            raise CheckpointCompatibilityError(
                "legacy checkpoint fusion_mode cannot be inferred"
            )
        fusion_mode = legacy_fusion_mode
    for key, item in (
        ("thinking_mode", runtime["thinking_mode"]),
        ("base_url", runtime["base_url"]),
        ("fusion_mode", fusion_mode),
    ):
        if not isinstance(item, str):
            raise CheckpointCompatibilityError("{0} must be a string".format(key))
    if fusion_mode not in {"off", "retrieval-global"}:
        raise CheckpointCompatibilityError("unsupported fusion_mode")
    return {
        "judge_timeout_sec": float(timeout),
        "judge_max_tokens": max_tokens,
        "thinking_mode": runtime["thinking_mode"],
        "base_url": runtime["base_url"],
        "provider_error_threshold": threshold,
        "fusion_mode": fusion_mode,
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
        "runtime_identity": _validated_runtime(
            {**dict(runtime_identity), "fusion_mode": runtime_identity.get("fusion_mode", "off")}
        ),
        "evidence_eligibility_policy": EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        "global_judgment_contract": GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        "global_pagination_policy": global_candidate_paging_policy(),
        "root_confirmation_contract": ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
        "factor_role_contract": FACTOR_ROLE_CONTRACT_IDENTITY,
    }
    return {**semantic, "config_fingerprint": _sha256(semantic)}


def validate_checkpoint_config(
    value: Mapping[str, Any],
    *,
    legacy_fusion_mode: Optional[str] = None,
) -> JsonDict:
    if not isinstance(value, Mapping):
        raise CheckpointCorruptionError("checkpoint config must be an object")
    source_schema = value.get("schema_version")
    if source_schema not in SUPPORTED_CHECKPOINT_SCHEMA_VERSIONS:
        raise CheckpointCompatibilityError("unsupported checkpoint schema version")
    config = _exact_mapping(value, CHECKPOINT_CONFIG_KEYS, "checkpoint config")
    source_semantic = {
        key: config[key] for key in config if key != "config_fingerprint"
    }
    if config["config_fingerprint"] != _sha256(source_semantic):
        raise CheckpointCorruptionError(
            "checkpoint config fingerprint does not match contents"
        )
    start_refs = config["start_refs"]
    if not isinstance(start_refs, list) or any(not isinstance(item, str) for item in start_refs):
        raise CheckpointCompatibilityError("start_refs must be a list of strings")
    config["budgets"] = _validated_budgets(config["budgets"])
    config["runtime_identity"] = _validated_runtime(
        config["runtime_identity"],
        legacy=source_schema == LEGACY_CHECKPOINT_SCHEMA_VERSION,
        legacy_fusion_mode=legacy_fusion_mode,
    )
    if config["global_pagination_policy"] != global_candidate_paging_policy():
        raise CheckpointCompatibilityError(
            "unsupported global candidate pagination policy"
        )
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
        "factor_role_contract",
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
    if config["factor_role_contract"] != FACTOR_ROLE_CONTRACT_IDENTITY:
        raise CheckpointCompatibilityError(
            "unsupported factor role contract"
        )
    if source_schema == LEGACY_CHECKPOINT_SCHEMA_VERSION:
        config["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        semantic = {
            key: config[key] for key in config if key != "config_fingerprint"
        }
        config["config_fingerprint"] = _sha256(semantic)
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
            if not isinstance(report, Mapping):
                raise CheckpointCorruptionError(
                    "analysis_ready report must be an object"
                )
            return dict(report)
        return None

    @property
    def final_report(self) -> Optional[JsonDict]:
        for record in reversed(self.actions):
            if record["operation"] == "analysis_completed":
                report = record["payload"].get("report")
                if not isinstance(report, Mapping):
                    raise CheckpointCorruptionError(
                        "analysis_completed report must be an object"
                    )
                return dict(report)
        return None

    @property
    def latest_actions(self) -> Dict[str, JsonDict]:
        latest: Dict[str, JsonDict] = {}
        for record in self.actions:
            latest[str(record["semantic_key"])] = record
        return latest


def _migration_report_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _migration_report_projection(item)
            for key, item in value.items()
            if str(key) not in MIGRATION_REPORT_PROJECTION_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_migration_report_projection(item) for item in value]
    return value


@dataclass(frozen=True)
class LegacyProjectionNotRequired:
    classifier_identity: str
    reason: str


@dataclass(frozen=True)
class LegacyProjectionRequired:
    classifier_identity: str
    reason: str
    gate_count: int
    shape_identity: str
    classification_identity: str

    @classmethod
    def create(
        cls,
        *,
        gate_count: int,
        shape_payload: Mapping[str, Any],
    ) -> "LegacyProjectionRequired":
        values = {
            "classifier_identity": LEGACY_PROJECTION_CLASSIFIER_IDENTITY,
            "reason": LEGACY_PROJECTION_REQUIRED_REASON,
            "gate_count": int(gate_count),
            "shape_identity": _sha256(shape_payload),
            "classification_identity": "",
        }
        classification = cls(**values)
        return cls(
            **{
                **values,
                "classification_identity": (
                    classification.recomputed_identity()
                ),
            }
        )

    def recomputed_identity(self) -> str:
        return _sha256(
            {
                "classifier_identity": self.classifier_identity,
                "reason": self.reason,
                "gate_count": self.gate_count,
                "shape_identity": self.shape_identity,
            }
        )

    def assert_valid(self) -> None:
        if (
            self.classifier_identity
            != LEGACY_PROJECTION_CLASSIFIER_IDENTITY
            or self.reason != LEGACY_PROJECTION_REQUIRED_REASON
            or type(self.gate_count) is not int
            or self.gate_count < 1
            or not self.shape_identity
            or self.classification_identity != self.recomputed_identity()
        ):
            raise CheckpointCorruptionError(
                "legacy projection classification identity is invalid"
            )

    def to_dict(self) -> JsonDict:
        return {
            "classifier_identity": self.classifier_identity,
            "reason": self.reason,
            "gate_count": self.gate_count,
            "shape_identity": self.shape_identity,
            "classification_identity": self.classification_identity,
        }


@dataclass(frozen=True)
class CompletedCheckpointReplayProof:
    """Immutable integrity proof for one completed checkpoint replay."""

    schema_version: str
    run_id: str
    config_fingerprint: str
    checkpoint_transaction_sequence: int
    checkpoint_commit_hash: str
    journal_heads: Tuple[Tuple[str, int, int, str], ...]
    terminal_action_record_hash: str
    output_transaction_id: str
    output_commit_hash: str
    attribution_path: str
    attribution_hash: str
    report_projection_hash: str
    lineage_path: str
    lineage_hash: str
    proof_identity: str

    def _identity_payload(self) -> JsonDict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "config_fingerprint": self.config_fingerprint,
            "checkpoint_transaction_sequence": self.checkpoint_transaction_sequence,
            "checkpoint_commit_hash": self.checkpoint_commit_hash,
            "journal_heads": [
                {
                    "journal": name,
                    "count": count,
                    "sequence": sequence,
                    "record_hash": record_hash,
                }
                for name, count, sequence, record_hash in self.journal_heads
            ],
            "terminal_action_record_hash": self.terminal_action_record_hash,
            "output_transaction_id": self.output_transaction_id,
            "output_commit_hash": self.output_commit_hash,
            "attribution_path": self.attribution_path,
            "attribution_hash": self.attribution_hash,
            "report_projection_hash": self.report_projection_hash,
            "lineage_path": self.lineage_path,
            "lineage_hash": self.lineage_hash,
        }

    def recomputed_identity(self) -> str:
        return _sha256(self._identity_payload())

    def to_dict(self) -> JsonDict:
        return {
            **self._identity_payload(),
            "proof_identity": self.proof_identity,
        }

    @classmethod
    def create(
        cls,
        *,
        config_fingerprint: str,
        commit: Mapping[str, Any],
        terminal_action: Mapping[str, Any],
        output_commit: Mapping[str, Any],
    ) -> "CompletedCheckpointReplayProof":
        heads = tuple(
            (
                name,
                int(commit["journal_heads"][name]["count"]),
                int(commit["journal_heads"][name]["sequence"]),
                str(commit["journal_heads"][name]["record_hash"]),
            )
            for name in JOURNAL_NAMES
        )
        values = {
            "schema_version": (
                COMPLETED_CHECKPOINT_REPLAY_PROOF_SCHEMA_VERSION
            ),
            "run_id": str(commit["run_id"]),
            "config_fingerprint": str(config_fingerprint),
            "checkpoint_transaction_sequence": int(
                commit["transaction_sequence"]
            ),
            "checkpoint_commit_hash": str(commit["commit_hash"]),
            "journal_heads": heads,
            "terminal_action_record_hash": str(terminal_action["record_hash"]),
            "output_transaction_id": str(output_commit["transaction_id"]),
            "output_commit_hash": str(output_commit["output_commit_hash"]),
            "attribution_path": str(output_commit["attribution_path"]),
            "attribution_hash": str(output_commit["attribution_hash"]),
            "report_projection_hash": _sha256(
                _migration_report_projection(terminal_action["payload"]["report"])
            ),
            "lineage_path": str(output_commit["lineage_path"]),
            "lineage_hash": str(output_commit["lineage_hash"]),
            "proof_identity": "",
        }
        proof = cls(**values)
        return cls(
            **{
                **values,
                "proof_identity": proof.recomputed_identity(),
            }
        )

    def assert_binds(
        self,
        *,
        report: Mapping[str, Any],
        action_records: Sequence[Mapping[str, Any]],
    ) -> None:
        if (
            self.schema_version
            != COMPLETED_CHECKPOINT_REPLAY_PROOF_SCHEMA_VERSION
            or self.proof_identity != self.recomputed_identity()
        ):
            raise CheckpointCorruptionError(
                "completed checkpoint replay proof identity is invalid"
            )
        if (
            _sha256(_migration_report_projection(report))
            != self.report_projection_hash
        ):
            raise CheckpointCorruptionError(
                "completed checkpoint replay proof does not bind the report"
            )
        if not action_records:
            raise CheckpointCorruptionError(
                "completed checkpoint replay proof has no terminal action"
            )
        terminal = action_records[-1]
        payload = terminal.get("payload")
        if (
            terminal.get("schema_version")
            not in SUPPORTED_CHECKPOINT_SCHEMA_VERSIONS
            or terminal.get("run_id") != self.run_id
            or terminal.get("operation") != "analysis_completed"
            or terminal.get("semantic_key") != "analysis:result"
            or terminal.get("record_hash") != self.terminal_action_record_hash
            or terminal.get("transaction_sequence")
            != self.checkpoint_transaction_sequence
            or not isinstance(payload, Mapping)
            or payload.get("interrupted") is not False
            or _migration_report_projection(payload.get("report"))
            != _migration_report_projection(report)
            or payload.get("output_transaction_id") != self.output_transaction_id
            or payload.get("output_commit_hash") != self.output_commit_hash
            or payload.get("attribution_hash") != self.attribution_hash
            or payload.get("lineage_hash") != self.lineage_hash
            or payload.get("lineage_path") != self.lineage_path
        ):
            raise CheckpointCorruptionError(
                "completed checkpoint replay proof is not terminal-action bound"
            )

    def derive_migration_decision(
        self,
        classification: LegacyProjectionRequired,
    ) -> "CompletedCheckpointMigrationDecision":
        if not isinstance(classification, LegacyProjectionRequired):
            raise CheckpointCorruptionError(
                "legacy migration requires an exact typed classification"
            )
        classification.assert_valid()
        return CompletedCheckpointMigrationDecision.create(
            replay_proof=self,
            classification=classification,
        )


@dataclass(frozen=True)
class CompletedCheckpointMigrationDecision:
    """Authorization derived only from a replay proof and exact legacy shape."""

    schema_version: str
    projection: str
    replay_proof: CompletedCheckpointReplayProof
    classifier_identity: str
    classifier_reason: str
    classification_identity: str
    decision_identity: str

    def _identity_payload(self) -> JsonDict:
        return {
            "schema_version": self.schema_version,
            "projection": self.projection,
            "replay_proof_identity": self.replay_proof.proof_identity,
            "classifier_identity": self.classifier_identity,
            "classifier_reason": self.classifier_reason,
            "classification_identity": self.classification_identity,
        }

    def recomputed_identity(self) -> str:
        return _sha256(self._identity_payload())

    @classmethod
    def create(
        cls,
        *,
        replay_proof: CompletedCheckpointReplayProof,
        classification: LegacyProjectionRequired,
    ) -> "CompletedCheckpointMigrationDecision":
        replay_proof_identity = replay_proof.recomputed_identity()
        if replay_proof.proof_identity != replay_proof_identity:
            raise CheckpointCorruptionError(
                "cannot authorize migration from a forged replay proof"
            )
        classification.assert_valid()
        values = {
            "schema_version": COMPLETED_CHECKPOINT_MIGRATION_SCHEMA_VERSION,
            "projection": LEGACY_BYPASSED_GLOBAL_GATE_PROJECTION,
            "replay_proof": replay_proof,
            "classifier_identity": classification.classifier_identity,
            "classifier_reason": classification.reason,
            "classification_identity": (
                classification.classification_identity
            ),
            "decision_identity": "",
        }
        decision = cls(**values)
        return cls(
            **{
                **values,
                "decision_identity": decision.recomputed_identity(),
            }
        )

    def to_dict(self) -> JsonDict:
        return {
            **self._identity_payload(),
            "replay_proof": self.replay_proof.to_dict(),
            "authorization": {
                "classifier_identity": self.classifier_identity,
                "reason": self.classifier_reason,
                "classification_identity": self.classification_identity,
            },
            "decision_identity": self.decision_identity,
        }

    def assert_authorizes(
        self,
        *,
        report: Mapping[str, Any],
        action_records: Sequence[Mapping[str, Any]],
        classification: LegacyProjectionRequired,
    ) -> None:
        if (
            self.schema_version
            != COMPLETED_CHECKPOINT_MIGRATION_SCHEMA_VERSION
            or self.projection != LEGACY_BYPASSED_GLOBAL_GATE_PROJECTION
            or self.decision_identity != self.recomputed_identity()
        ):
            raise CheckpointCorruptionError(
                "completed checkpoint migration decision identity is invalid"
            )
        classification.assert_valid()
        if (
            self.classifier_identity
            != classification.classifier_identity
            or self.classifier_reason != classification.reason
            or self.classification_identity
            != classification.classification_identity
        ):
            raise CheckpointCorruptionError(
                "migration decision does not bind the exact classifier result"
            )
        self.replay_proof.assert_binds(
            report=report,
            action_records=action_records,
        )


@dataclass(frozen=True)
class CheckpointReplay:
    state: CheckpointState
    replay_proof: Optional[CompletedCheckpointReplayProof]


class CheckpointBundle:
    """Three fsync journals governed by one atomically published commit head."""

    def __init__(
        self,
        root: Path,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
        blob_store_root: Optional[Path] = None,
    ) -> None:
        self.root = Path(root)
        self._blob_store_root = Path(blob_store_root or root)
        self._blob_directory = (
            self._blob_store_root
            / CHECKPOINT_BLOB_DIRECTORY_NAME
            / CHECKPOINT_BLOB_ALGORITHM_DIRECTORY
        )
        self.current_path = self.root / CURRENT_POINTER_NAME
        self.generations_path = self.root / GENERATION_DIRECTORY_NAME
        self.migration_transactions_path = (
            self.root / MIGRATION_TRANSACTION_DIRECTORY_NAME
        )
        self.lock_path = self.root / CHECKPOINT_LOCK_NAME
        self.output_commit_path = self.root / "output-commit.json"
        self.migration_marker_path = self.root / MIGRATION_MARKER_NAME
        self._carrier_root = self.root
        self._bind_carrier_root(self.root)
        self._migration_transaction_path: Optional[Path] = None
        self._pending_generation_staging: Optional[Path] = None
        self._pending_generation_target: Optional[Path] = None
        self._fault_hook = fault_hook or (lambda _stage: None)
        self._run_id = ""
        self._next_sequence: Dict[str, int] = {}
        self._last_hash: Dict[str, str] = {}
        self._transaction_sequence = 0
        self._commit: JsonDict = {}
        self._loaded_manifest_schema_version = CHECKPOINT_SCHEMA_VERSION
        self._expected_config: Optional[JsonDict] = None
        self._lock_descriptor: Optional[int] = None
        self._lock_exclusive = False
        self._carrier_root_identity: Optional[Tuple[int, int, int]] = None
        self._generation_directory_identity: Optional[Tuple[int, int, int]] = None
        self._completed_replay: Optional[CheckpointReplay] = None

    def _bind_carrier_root(self, carrier_root: Path) -> None:
        self._carrier_root = Path(carrier_root)
        self._carrier_root_identity = None
        self.manifest_path = self._carrier_root / "manifest.json"
        self.commit_path = self._carrier_root / "commit.json"
        self.frontier_path = self._carrier_root / "frontier.jsonl"
        self.hypotheses_path = self._carrier_root / "hypotheses.jsonl"
        self.actions_path = self._carrier_root / "investigation-actions.jsonl"
        self._paths = {
            "frontier": self.frontier_path,
            "hypotheses": self.hypotheses_path,
            "actions": self.actions_path,
        }

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        return path.is_symlink() or path.exists()

    @staticmethod
    def _directory_identity(value: os.stat_result) -> Tuple[int, int, int]:
        return (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))

    @staticmethod
    def _file_identity(value: os.stat_result) -> Tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            stat.S_IFMT(value.st_mode),
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def _validate_checkpoint_root(self, *, create: bool) -> None:
        if not self._entry_exists(self.root):
            if not create:
                raise CheckpointCorruptionError(
                    "checkpoint root directory is missing"
                )
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise CheckpointLockError(
                    "checkpoint root could not be created for locking"
                ) from exc
        try:
            root_stat = os.lstat(self.root)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint root directory is unreadable"
            ) from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise CheckpointCorruptionError(
                "checkpoint root is not a regular directory"
            )

    def _open_checkpoint_lock(self) -> int:
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.lock_path), flags, 0o600)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint lock is not a usable regular file"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            linked = os.lstat(self.lock_path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(linked.st_mode)
                or not stat.S_ISREG(linked.st_mode)
                or self._directory_identity(opened)
                != self._directory_identity(linked)
            ):
                raise CheckpointCorruptionError(
                    "checkpoint lock is not a stable regular file"
                )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _assert_lock_path_identity(self, descriptor: int) -> None:
        try:
            opened = os.fstat(descriptor)
            linked = os.lstat(self.lock_path)
        except OSError as exc:
            raise CheckpointLockError(
                "checkpoint lock identity could not be verified"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or self._directory_identity(opened) != self._directory_identity(linked)
        ):
            raise CheckpointLockError(
                "checkpoint lock identity changed during acquisition"
            )

    @contextmanager
    def _checkpoint_lock(
        self,
        *,
        exclusive: bool,
        create_root: bool,
    ) -> Iterator[None]:
        if self._lock_descriptor is not None:
            raise CheckpointLockError(
                "nested checkpoint lock acquisition is forbidden"
            )
        self._validate_checkpoint_root(create=create_root)
        descriptor = self._open_checkpoint_lock()
        acquired = False
        body_error: Optional[BaseException] = None
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
            except OSError as exc:
                raise CheckpointLockError(
                    "checkpoint lock acquisition failed"
                ) from exc
            acquired = True
            self._assert_lock_path_identity(descriptor)
            self._lock_descriptor = descriptor
            self._lock_exclusive = exclusive
            yield
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            release_error: Optional[BaseException] = None
            if acquired:
                try:
                    self._assert_lock_path_identity(descriptor)
                except BaseException as exc:
                    release_error = exc
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError as exc:
                    release_error = release_error or exc
            self._lock_descriptor = None
            self._lock_exclusive = False
            try:
                os.close(descriptor)
            except OSError as exc:
                release_error = release_error or exc
            if release_error is not None:
                error = CheckpointLockError("checkpoint lock release failed")
                if body_error is not None:
                    raise error from body_error
                raise error from release_error

    def _require_lock(self, *, exclusive: bool) -> None:
        if self._lock_descriptor is None:
            raise CheckpointLockError("checkpoint operation requires an active lock")
        if exclusive and not self._lock_exclusive:
            raise CheckpointLockError(
                "checkpoint mutation requires an exclusive lock"
            )

    def _capture_carrier_root_identity(self) -> None:
        try:
            carrier_stat = os.lstat(self._carrier_root)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint carrier directory is missing"
            ) from exc
        if stat.S_ISLNK(carrier_stat.st_mode) or not stat.S_ISDIR(
            carrier_stat.st_mode
        ):
            raise CheckpointCorruptionError(
                "checkpoint carrier directory is not a regular directory"
            )
        root_real = self.root.resolve(strict=True)
        carrier_real = self._carrier_root.resolve(strict=True)
        try:
            carrier_real.relative_to(root_real)
        except ValueError as exc:
            raise CheckpointCorruptionError(
                "checkpoint carrier directory escaped checkpoint root"
            ) from exc
        if self._carrier_root != self.root:
            if self._carrier_root.parent != self.generations_path:
                raise CheckpointCorruptionError(
                    "checkpoint generation escaped generation directory"
                )
            try:
                generations = os.lstat(self.generations_path)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint generation directory is missing"
                ) from exc
            if stat.S_ISLNK(generations.st_mode) or not stat.S_ISDIR(
                generations.st_mode
            ):
                raise CheckpointCorruptionError(
                    "checkpoint generation directory is invalid"
                )
            generations_identity = self._directory_identity(generations)
            if (
                self._generation_directory_identity is not None
                and generations_identity != self._generation_directory_identity
            ):
                raise CheckpointCorruptionError(
                    "checkpoint generation directory changed identity"
                )
            self._generation_directory_identity = generations_identity
        self._carrier_root_identity = self._directory_identity(carrier_stat)

    def _assert_carrier_root_identity(self) -> None:
        if self._carrier_root_identity is None:
            self._capture_carrier_root_identity()
            return
        try:
            current = os.lstat(self._carrier_root)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint carrier directory changed identity"
            ) from exc
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or self._directory_identity(current) != self._carrier_root_identity
        ):
            raise CheckpointCorruptionError(
                "checkpoint carrier directory changed identity"
            )
        if self._carrier_root != self.root:
            try:
                generations = os.lstat(self.generations_path)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint generation directory changed identity"
                ) from exc
            if (
                self._generation_directory_identity is None
                or stat.S_ISLNK(generations.st_mode)
                or not stat.S_ISDIR(generations.st_mode)
                or self._directory_identity(generations)
                != self._generation_directory_identity
            ):
                raise CheckpointCorruptionError(
                    "checkpoint generation directory changed identity"
                )

    def _assert_contained_regular_file(self, path: Path, label: str) -> os.stat_result:
        self._assert_carrier_root_identity()
        root_absolute = Path(os.path.abspath(self.root))
        path_absolute = Path(os.path.abspath(path))
        try:
            path_absolute.relative_to(root_absolute)
        except ValueError as exc:
            raise CheckpointCorruptionError(
                "{0} is not contained by the checkpoint root".format(label)
            ) from exc
        try:
            value = os.lstat(path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "{0} is missing or unreadable".format(label)
            ) from exc
        if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
            raise CheckpointCorruptionError(
                "{0} is not a regular non-symlink file".format(label)
            )
        return value

    def _read_regular_bytes(self, path: Path, label: str) -> bytes:
        linked_before = self._assert_contained_regular_file(path, label)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "{0} could not be opened safely".format(label)
            ) from exc
        try:
            opened_before = os.fstat(descriptor)
            if self._directory_identity(opened_before) != self._directory_identity(
                linked_before
            ):
                raise CheckpointCorruptionError(
                    "{0} changed identity before read".format(label)
                )
            chunks: List[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(descriptor)
            linked_after = os.lstat(path)
            if (
                self._file_identity(opened_before)
                != self._file_identity(opened_after)
                or self._file_identity(opened_after)
                != self._file_identity(linked_after)
            ):
                raise CheckpointCorruptionError(
                    "{0} changed identity during read".format(label)
                )
            return b"".join(chunks)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "{0} changed during read".format(label)
            ) from exc
        finally:
            os.close(descriptor)

    def _read_regular_json(self, path: Path, label: str) -> JsonDict:
        try:
            value = json.loads(self._read_regular_bytes(path, label).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointCorruptionError("invalid {0}".format(label)) from exc
        if not isinstance(value, Mapping):
            raise CheckpointCorruptionError("invalid {0}".format(label))
        return dict(value)

    def _validate_active_carriers(self) -> None:
        self._assert_carrier_root_identity()
        expected = {name for _, name in MIGRATION_CARRIERS}
        if self._carrier_root != self.root:
            try:
                with os.scandir(self._carrier_root) as entries:
                    actual = {entry.name for entry in entries}
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint generation directory is unreadable"
                ) from exc
            if actual != expected:
                raise CheckpointCorruptionError(
                    "checkpoint generation carriers are not self-contained"
                )
        for _, name in MIGRATION_CARRIERS:
            self._assert_contained_regular_file(
                self._carrier_root / name,
                "checkpoint carrier {0}".format(name),
            )

    def _resolve_visible_generation(self) -> Path:
        self._bind_carrier_root(self.root)
        self._generation_directory_identity = None
        self._capture_carrier_root_identity()
        if not self._entry_exists(self.current_path):
            self._bind_carrier_root(self.root)
            self._capture_carrier_root_identity()
            return self.root
        try:
            raw = self._read_regular_bytes(
                self.current_path,
                "checkpoint CURRENT pointer",
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointCorruptionError(
                "checkpoint CURRENT pointer is unreadable"
            ) from exc
        relative = raw.strip()
        candidate = Path(relative)
        if (
            raw != "{0}\n".format(relative)
            or candidate.parts
            != (
                GENERATION_DIRECTORY_NAME,
                candidate.name,
            )
            or not candidate.name.startswith(GENERATION_PREFIX)
            or candidate.name == GENERATION_PREFIX
        ):
            raise CheckpointCorruptionError(
                "checkpoint CURRENT pointer is invalid"
            )
        try:
            generations_stat = os.lstat(self.generations_path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation directory is invalid"
            ) from exc
        if stat.S_ISLNK(generations_stat.st_mode) or not stat.S_ISDIR(
            generations_stat.st_mode
        ):
            raise CheckpointCorruptionError(
                "checkpoint generation directory is invalid"
            )
        generation = self.root / candidate
        try:
            generation_stat = os.lstat(generation)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint CURRENT generation is missing"
            ) from exc
        if stat.S_ISLNK(generation_stat.st_mode) or not stat.S_ISDIR(
            generation_stat.st_mode
        ):
            raise CheckpointCorruptionError(
                "checkpoint CURRENT generation is missing"
            )
        self._bind_carrier_root(generation)
        self._generation_directory_identity = self._directory_identity(
            generations_stat
        )
        self._capture_carrier_root_identity()
        self._assert_carrier_root_identity()
        return generation

    def _prepare_generation_write(self) -> None:
        self._require_lock(exclusive=True)
        if self._pending_generation_staging is not None:
            return
        source = self._carrier_root
        if source == self.root:
            return
        self._preflight_generation_reclamation()
        if (
            source.parent != self.generations_path
            or not source.name.startswith(GENERATION_PREFIX)
        ):
            raise CheckpointCorruptionError(
                "checkpoint write source is not a published generation"
            )
        transaction_id = str(uuid.uuid4())
        staging = self.generations_path / "{0}{1}".format(
            GENERATION_WRITE_STAGING_PREFIX,
            transaction_id,
        )
        target = self.generations_path / "{0}{1}".format(
            GENERATION_PREFIX,
            transaction_id,
        )
        staging.mkdir()
        self._fsync_directory(self.generations_path)
        for _, name in MIGRATION_CARRIERS:
            source_path = source / name
            _atomic_write_bytes(
                staging / name,
                self._read_regular_bytes(
                    source_path,
                    "published checkpoint generation carrier {0}".format(name),
                )
            )
        self._fsync_directory(staging)
        self._pending_generation_staging = staging
        self._pending_generation_target = target
        self._bind_carrier_root(staging)

    def _finish_generation_write(self) -> None:
        self._require_lock(exclusive=True)
        staging = self._pending_generation_staging
        target = self._pending_generation_target
        if staging is None or target is None:
            return
        state = CheckpointBundle(
            staging,
            blob_store_root=self._blob_store_root,
        )._restore_current_generation(
            validated_expected=None,
        )
        if (
            state.run_id != self._run_id
            or state.transaction_sequence != self._transaction_sequence
        ):
            raise CheckpointCorruptionError(
                "checkpoint generation write does not match committed state"
            )
        os.replace(staging, target)
        self._fsync_directory(self.generations_path)
        self._publish_current_generation(
            target,
            emit_migration_faults=False,
        )
        self._bind_carrier_root(target)
        self._capture_carrier_root_identity()
        self._validate_active_carriers()
        self._pending_generation_staging = None
        self._pending_generation_target = None
        self._reclaim_inactive_generations(active=target)

    def initialize(self, config: Mapping[str, Any]) -> None:
        validated = validate_checkpoint_config(config)
        with self._checkpoint_lock(exclusive=True, create_root=True):
            self._initialize_locked(validated)

    def _initialize_locked(self, validated: Mapping[str, Any]) -> None:
        self._require_lock(exclusive=True)
        self._resolve_visible_generation()
        self._recover_migration_transaction(expected_config=validated)
        self._resolve_visible_generation()
        if self._entry_exists(self.manifest_path):
            self._validate_active_carriers()
            manifest = self._read_manifest_envelope(
                expected_config=validated
            )
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
            if self._entry_exists(path):
                self._assert_contained_regular_file(
                    path,
                    "checkpoint carrier {0}".format(path.name),
                )
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(str(path), flags, 0o600)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint journal could not be opened safely"
                ) from exc
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise CheckpointCorruptionError(
                        "checkpoint journal is not a regular file"
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._fsync_directory(self._carrier_root)
        if self._carrier_root != self.root:
            self._fsync_directory(self.root)
        self._fault_hook("checkpoint_directory_durable")

        if self._entry_exists(self.commit_path):
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

        self._validate_active_carriers()
        self._validate_commit_identity(self._commit)
        if self._carrier_root == self.root:
            repairs: List[JsonDict] = []
            for journal, path in self._paths.items():
                repairs.extend(self._repair_to_committed_head(journal, path))
            if repairs:
                previous = str(self._commit["commit_hash"])
                self._commit = self._new_commit(
                    transaction_sequence=int(self._commit["transaction_sequence"])
                    + 1,
                    semantic_key="checkpoint:tail_repair",
                    journal_heads=dict(self._commit["journal_heads"]),
                    previous_commit_hash=previous,
                    tail_repair_count=int(self._commit["tail_repair_count"])
                    + len(repairs),
                    tail_repair_events=list(self._commit["tail_repair_events"])
                    + repairs,
                )
                self._atomic_write_json(self.commit_path, self._commit)
        else:
            for journal, path in self._paths.items():
                records, corrupt, complete = self._read_journal(journal, path)
                committed = self._committed_prefix(journal, records, self._commit)
                if corrupt or not complete or len(records) != len(committed):
                    raise CheckpointCorruptionError(
                        "published checkpoint generation is not append-safe"
                    )
        self._load_append_heads()
        self._expected_config = dict(validated)
        self._preflight_generation_reclamation()
        if self._carrier_root != self.root:
            self._reclaim_inactive_generations(active=self._carrier_root)

    def record_frontier(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._locked_commit_one(
            "frontier", operation, semantic_key, payload
        )

    def record_hypothesis(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._locked_commit_one(
            "hypotheses", operation, semantic_key, payload
        )

    def record_action(
        self, operation: str, semantic_key: str, payload: Mapping[str, Any]
    ) -> JsonDict:
        return self._locked_commit_one(
            "actions", operation, semantic_key, payload
        )

    def _locked_commit_one(
        self,
        journal: str,
        operation: str,
        semantic_key: str,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        with self._checkpoint_lock(exclusive=True, create_root=False):
            self._reload_latest_for_write_locked()
            return self._commit_one(journal, operation, semantic_key, payload)

    def commit_snapshot(
        self,
        *,
        semantic_key: str,
        frontier_payload: Mapping[str, Any],
        hypothesis_payload: Mapping[str, Any],
        action_payload: Mapping[str, Any],
    ) -> JsonDict:
        with self._checkpoint_lock(exclusive=True, create_root=False):
            self._reload_latest_for_write_locked()
            return self._commit_snapshot_locked(
                semantic_key=semantic_key,
                frontier_payload=frontier_payload,
                hypothesis_payload=hypothesis_payload,
                action_payload=action_payload,
            )

    def _commit_snapshot_locked(
        self,
        *,
        semantic_key: str,
        frontier_payload: Mapping[str, Any],
        hypothesis_payload: Mapping[str, Any],
        action_payload: Mapping[str, Any],
    ) -> JsonDict:
        self._require_lock(exclusive=True)
        self._require_initialized()
        self._prepare_generation_write()
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
        validated_expected = (
            validate_checkpoint_config(expected_config)
            if expected_config is not None
            else None
        )
        with self._restored_state_transaction(
            validated_expected=validated_expected
        ) as state:
            return state

    @contextmanager
    def _restored_state_transaction(
        self,
        *,
        validated_expected: Optional[Mapping[str, Any]],
    ) -> Iterator[CheckpointState]:
        requires_exclusive = False
        with self._checkpoint_lock(exclusive=False, create_root=False):
            self._resolve_visible_generation()
            self._validate_active_carriers()
            requires_exclusive = self._restore_requires_exclusive_locked()
            if not requires_exclusive:
                yield self._restore_current_generation(
                    validated_expected=validated_expected
                )
                return
        with self._checkpoint_lock(exclusive=True, create_root=False):
            self._resolve_visible_generation()
            self._recover_migration_transaction(
                expected_config=validated_expected
            )
            self._resolve_visible_generation()
            self._validate_active_carriers()
            yield self._restore_current_generation(
                validated_expected=validated_expected
            )

    def _restore_requires_exclusive_locked(self) -> bool:
        self._require_lock(exclusive=False)
        if self._migration_transaction_candidates():
            return True
        manifest = self._read_regular_json(
            self.manifest_path,
            "checkpoint manifest",
        )
        return manifest.get("schema_version") == LEGACY_CHECKPOINT_SCHEMA_VERSION

    def _restore_current_generation(
        self,
        *,
        validated_expected: Optional[Mapping[str, Any]],
    ) -> CheckpointState:
        self._validate_active_carriers()
        manifest = self._read_manifest_envelope(
            expected_config=validated_expected
        )
        self._run_id = str(manifest["run_id"])
        config = manifest["config"]
        if validated_expected is not None:
            self._assert_compatible(config, validated_expected)
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
        blob_cache: Dict[str, Any] = {}
        logical_committed = {
            journal: [
                self._hydrate_journal_record(record, blob_cache=blob_cache)
                for record in records
            ]
            for journal, records in committed.items()
        }
        return CheckpointState(
            config=dict(config),
            run_id=str(commit["run_id"]),
            transaction_sequence=int(commit["transaction_sequence"]),
            frontier_records=tuple(logical_committed["frontier"]),
            hypothesis_records=tuple(logical_committed["hypotheses"]),
            actions=tuple(logical_committed["actions"]),
            corrupt_entries=incomplete,
            tail_repair_count=int(commit["tail_repair_count"]),
            tail_repair_events=tuple(dict(item) for item in commit["tail_repair_events"]),
        )

    def restore_for_replay(
        self,
        *,
        expected_config: Mapping[str, Any],
        expected_lineage: Mapping[str, Any],
    ) -> CheckpointReplay:
        """Restore state and prove completed replay integrity without authorizing migration."""

        validated_expected = validate_checkpoint_config(expected_config)
        with self._restored_state_transaction(
            validated_expected=validated_expected
        ) as state:
            replay = self._restore_for_replay_locked(
                state=state,
                validated_expected=validated_expected,
                expected_lineage=expected_lineage,
            )
        self._completed_replay = replay
        return replay

    def completed_replay_output_commit(
        self,
        *,
        attribution_path: Path,
        lineage_path: Path,
        report: Mapping[str, Any],
        message_lineage: Mapping[str, Any],
    ) -> Optional[JsonDict]:
        """Return the already-published output for an exact completed replay."""

        replay = self._completed_replay
        if replay is None or replay.replay_proof is None:
            return None
        proof = replay.replay_proof
        if replay.state.final_report != dict(report):
            raise CheckpointCompatibilityError(
                "completed replay report differs from the published report"
            )
        proof.assert_binds(report=report, action_records=replay.state.actions)
        resolved_attribution = Path(attribution_path).resolve()
        resolved_lineage = Path(lineage_path).resolve()
        if (
            str(resolved_attribution) != proof.attribution_path
            or str(resolved_lineage) != proof.lineage_path
        ):
            raise CheckpointCompatibilityError(
                "completed replay output paths differ from the published transaction"
            )
        if (
            hashlib.sha256(_json_bytes(report)).hexdigest()
            != proof.attribution_hash
            or hashlib.sha256(_json_bytes(message_lineage)).hexdigest()
            != proof.lineage_hash
        ):
            raise CheckpointCorruptionError(
                "completed replay output content differs from the published transaction"
            )
        with self._checkpoint_lock(exclusive=False, create_root=False):
            output = _validate_output_commit(
                self._read_regular_json(
                    self.output_commit_path,
                    "output commit",
                ),
                run_id=proof.run_id,
            )
        if (
            output["status"] != "published"
            or output["transaction_id"] != proof.output_transaction_id
            or output["output_commit_hash"] != proof.output_commit_hash
            or output["attribution_hash"] != proof.attribution_hash
            or output["lineage_hash"] != proof.lineage_hash
        ):
            raise CheckpointCorruptionError(
                "completed replay output commit differs from its integrity proof"
            )
        return output

    def _restore_for_replay_locked(
        self,
        *,
        state: CheckpointState,
        validated_expected: Mapping[str, Any],
        expected_lineage: Mapping[str, Any],
    ) -> CheckpointReplay:
        if state.config != validated_expected:
            raise CheckpointCompatibilityError(
                "restored checkpoint config does not exactly match the invocation"
            )
        report = state.final_report
        if report is None:
            return CheckpointReplay(state=state, replay_proof=None)
        if state.corrupt_entries:
            raise CheckpointCorruptionError(
                "completed checkpoint contains an incomplete journal tail"
            )

        commit = self._read_commit()
        if (
            commit["run_id"] != state.run_id
            or int(commit["transaction_sequence"]) != state.transaction_sequence
            or commit["semantic_key"] != "analysis:result"
        ):
            raise CheckpointCorruptionError(
                "completed checkpoint commit is not terminal"
            )
        records_by_journal = {
            "frontier": state.frontier_records,
            "hypotheses": state.hypothesis_records,
            "actions": state.actions,
        }
        for name in JOURNAL_NAMES:
            records = records_by_journal[name]
            actual_head = {
                "count": len(records),
                "sequence": len(records),
                "record_hash": (
                    str(records[-1]["record_hash"]) if records else ""
                ),
            }
            if actual_head != commit["journal_heads"][name]:
                raise CheckpointCorruptionError(
                    "{0} journal is not bound to the completed commit".format(name)
                )
        if not state.actions:
            raise CheckpointCorruptionError(
                "completed checkpoint has no terminal action"
            )
        terminal = state.actions[-1]
        payload = _exact_mapping(
            terminal.get("payload"),
            ANALYSIS_COMPLETION_PAYLOAD_KEYS,
            "analysis completion payload",
        )
        if (
            terminal["operation"] != "analysis_completed"
            or terminal["semantic_key"] != "analysis:result"
            or int(terminal["transaction_sequence"])
            != state.transaction_sequence
            or payload["interrupted"] is not False
            or payload["report"] != report
            or report.get("case_id") != state.config["case_id"]
        ):
            raise CheckpointCorruptionError(
                "analysis completion is not the checkpoint terminal action"
            )

        output = _validate_output_commit(
            self._read_regular_json(
                self.output_commit_path,
                "output commit",
            ),
            run_id=state.run_id,
        )
        if output["status"] != "published":
            raise CheckpointCorruptionError(
                "completed checkpoint output is not published"
            )
        attribution_path = Path(str(output["attribution_path"]))
        report_bytes = _json_bytes(report)
        if (
            not attribution_path.is_file()
            or _file_sha256(attribution_path) != output["attribution_hash"]
            or attribution_path.read_bytes() != report_bytes
            or hashlib.sha256(report_bytes).hexdigest()
            != output["attribution_hash"]
        ):
            raise CheckpointCorruptionError(
                "published attribution does not match the completed report"
            )

        lineage_path = Path(str(output["lineage_path"]))
        expected_lineage_bytes = _json_bytes(expected_lineage)
        if (
            not lineage_path.is_file()
            or _file_sha256(lineage_path) != output["lineage_hash"]
            or lineage_path.read_bytes() != expected_lineage_bytes
            or hashlib.sha256(expected_lineage_bytes).hexdigest()
            != output["lineage_hash"]
        ):
            raise CheckpointCorruptionError(
                "published lineage does not match the current trace reconstruction"
            )
        if (
            payload["output_transaction_id"] != output["transaction_id"]
            or payload["output_commit_hash"] != output["output_commit_hash"]
            or payload["attribution_hash"] != output["attribution_hash"]
            or payload["lineage_hash"] != output["lineage_hash"]
            or payload["lineage_path"] != output["lineage_path"]
        ):
            raise CheckpointCorruptionError(
                "analysis completion does not match the output transaction"
            )

        proof = CompletedCheckpointReplayProof.create(
            config_fingerprint=str(state.config["config_fingerprint"]),
            commit=commit,
            terminal_action=terminal,
            output_commit=output,
        )
        proof.assert_binds(
            report=report,
            action_records=state.actions,
        )
        return CheckpointReplay(state=state, replay_proof=proof)

    def mark_analysis_completed(
        self, *, report: Mapping[str, Any], output_commit: Mapping[str, Any]
    ) -> JsonDict:
        with self._checkpoint_lock(exclusive=True, create_root=False):
            self._reload_latest_for_write_locked()
            return self._mark_analysis_completed_locked(
                report=report,
                output_commit=output_commit,
            )

    def _mark_analysis_completed_locked(
        self, *, report: Mapping[str, Any], output_commit: Mapping[str, Any]
    ) -> JsonDict:
        self._require_lock(exclusive=True)
        persisted = self._read_regular_json(
            self.output_commit_path,
            "output commit",
        )
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
        return self._commit_one(
            "actions",
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
        with self._checkpoint_lock(exclusive=False, create_root=False):
            self._resolve_visible_generation()
            self._validate_active_carriers()
            for path in (*self._paths.values(), self.manifest_path, self.commit_path):
                descriptor = os.open(
                    str(path),
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._fsync_directory(self._carrier_root)
            if self._carrier_root != self.root:
                self._fsync_directory(self.root)

    def _reload_latest_for_write_locked(self) -> None:
        self._require_lock(exclusive=True)
        if self._expected_config is None or not self._run_id or not self._commit:
            raise CheckpointCompatibilityError(
                "checkpoint must be initialized before append"
            )
        self._pending_generation_staging = None
        self._pending_generation_target = None
        self._resolve_visible_generation()
        self._recover_migration_transaction(
            expected_config=self._expected_config,
        )
        self._resolve_visible_generation()
        self._validate_active_carriers()
        manifest = self._read_manifest_envelope(
            expected_config=self._expected_config,
        )
        self._assert_compatible(manifest["config"], self._expected_config)
        self._run_id = str(manifest["run_id"])
        self._commit = self._read_commit()
        self._validate_commit_identity(self._commit)
        if self._carrier_root == self.root:
            repairs: List[JsonDict] = []
            for journal, path in self._paths.items():
                repairs.extend(self._repair_to_committed_head(journal, path))
            if repairs:
                previous = str(self._commit["commit_hash"])
                self._commit = self._new_commit(
                    transaction_sequence=int(
                        self._commit["transaction_sequence"]
                    )
                    + 1,
                    semantic_key="checkpoint:tail_repair",
                    journal_heads=dict(self._commit["journal_heads"]),
                    previous_commit_hash=previous,
                    tail_repair_count=int(self._commit["tail_repair_count"])
                    + len(repairs),
                    tail_repair_events=list(self._commit["tail_repair_events"])
                    + repairs,
                )
                self._atomic_write_json(self.commit_path, self._commit)
        else:
            for journal, path in self._paths.items():
                records, corrupt, complete = self._read_journal(journal, path)
                committed = self._committed_prefix(journal, records, self._commit)
                if corrupt or not complete or len(records) != len(committed):
                    raise CheckpointCorruptionError(
                        "published checkpoint generation is not append-safe"
                    )
        self._load_append_heads()
        self._preflight_generation_reclamation()

    def _commit_one(
        self,
        journal: str,
        operation: str,
        semantic_key: str,
        payload: Mapping[str, Any],
    ) -> JsonDict:
        self._require_lock(exclusive=True)
        self._require_initialized()
        self._prepare_generation_write()
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
        normalized_payload = json.loads(stable_json(payload))
        persisted_payload = self._persist_checkpoint_payload(
            normalized_payload
        )
        unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": self._run_id,
            "journal": journal,
            "sequence": self._next_sequence[journal],
            "transaction_sequence": transaction_sequence,
            "timestamp": _timestamp(),
            "operation": operation,
            "semantic_key": semantic_key,
            "payload": persisted_payload,
            "previous_hash": self._last_hash[journal],
        }
        record = {**unsigned, "record_hash": _sha256(unsigned)}
        path = self._paths[journal]
        linked = self._assert_contained_regular_file(
            path,
            "checkpoint carrier {0}".format(path.name),
        )
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal could not be opened safely for append"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if self._directory_identity(opened) != self._directory_identity(linked):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity before append"
                )
            content = (
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
            linked_after = os.lstat(path)
            if self._directory_identity(os.fstat(descriptor)) != self._directory_identity(
                linked_after
            ):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity during append"
                )
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal append failed"
            ) from exc
        finally:
            os.close(descriptor)
        self._next_sequence[journal] += 1
        self._last_hash[journal] = str(record["record_hash"])
        return record

    def _persist_checkpoint_payload(self, payload: Mapping[str, Any]) -> JsonDict:
        encoded, externalized = self._encode_checkpoint_value(
            dict(payload),
            allow_externalize=False,
        )
        if not isinstance(encoded, Mapping):
            raise TypeError("journal payload root must remain an object")
        if not externalized:
            return dict(encoded)
        return {
            CHECKPOINT_PAYLOAD_ENVELOPE_KEY: {
                "schema_version": CHECKPOINT_PAYLOAD_SCHEMA_VERSION,
                "root": encoded,
            }
        }

    def _encode_checkpoint_value(
        self,
        value: Any,
        *,
        allow_externalize: bool = True,
    ) -> Tuple[Any, bool]:
        child_externalized = False
        if isinstance(value, Mapping):
            encoded: Any = {}
            for key, item in value.items():
                encoded_item, item_externalized = self._encode_checkpoint_value(
                    item
                )
                encoded[str(key)] = encoded_item
                child_externalized = child_externalized or item_externalized
        elif isinstance(value, (list, tuple)):
            encoded = []
            for item in value:
                encoded_item, item_externalized = self._encode_checkpoint_value(
                    item
                )
                encoded.append(encoded_item)
                child_externalized = child_externalized or item_externalized
        else:
            return value, False

        content = stable_json(encoded).encode("utf-8")
        if allow_externalize and len(content) >= CHECKPOINT_BLOB_THRESHOLD_BYTES:
            return self._write_checkpoint_blob(content), True
        return encoded, child_externalized

    def _ensure_checkpoint_blob_directory(self, *, create: bool) -> None:
        current = self._blob_store_root
        for name in (
            CHECKPOINT_BLOB_DIRECTORY_NAME,
            CHECKPOINT_BLOB_ALGORITHM_DIRECTORY,
        ):
            current = current / name
            if not current.exists() and not current.is_symlink():
                if not create:
                    raise CheckpointCorruptionError(
                        "checkpoint blob is missing"
                    )
                current.mkdir()
                self._fsync_directory(current.parent)
            try:
                linked = os.lstat(current)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint blob directory is missing or unreadable"
                ) from exc
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(
                linked.st_mode
            ):
                raise CheckpointCorruptionError(
                    "checkpoint blob directory is not a regular directory"
                )

    def _checkpoint_blob_path(self, digest: str) -> Path:
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CheckpointCorruptionError(
                "checkpoint blob digest is invalid"
            )
        return self._blob_directory / "{0}.json".format(digest)

    def _write_checkpoint_blob(self, content: bytes) -> JsonDict:
        digest = hashlib.sha256(content).hexdigest()
        self._ensure_checkpoint_blob_directory(create=True)
        path = self._checkpoint_blob_path(digest)
        if path.exists() or path.is_symlink():
            existing = self._read_checkpoint_blob_bytes(path)
            if existing != content:
                raise CheckpointCorruptionError(
                    "checkpoint blob hash collision or modification"
                )
        else:
            _atomic_write_bytes(path, content)
        return {
            "schema_version": CHECKPOINT_BLOB_SCHEMA_VERSION,
            "digest": "sha256:{0}".format(digest),
            "byte_length": len(content),
        }

    def _read_checkpoint_blob_bytes(self, path: Path) -> bytes:
        self._ensure_checkpoint_blob_directory(create=False)
        try:
            linked_before = os.lstat(path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint blob is missing"
            ) from exc
        if stat.S_ISLNK(linked_before.st_mode) or not stat.S_ISREG(
            linked_before.st_mode
        ):
            raise CheckpointCorruptionError(
                "checkpoint blob is not a regular non-symlink file"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint blob could not be opened safely"
            ) from exc
        try:
            opened_before = os.fstat(descriptor)
            if self._file_identity(opened_before) != self._file_identity(
                linked_before
            ):
                raise CheckpointCorruptionError(
                    "checkpoint blob changed identity before read"
                )
            chunks: List[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(descriptor)
            linked_after = os.lstat(path)
            if (
                self._file_identity(opened_before)
                != self._file_identity(opened_after)
                or self._file_identity(opened_after)
                != self._file_identity(linked_after)
            ):
                raise CheckpointCorruptionError(
                    "checkpoint blob changed identity during read"
                )
            return b"".join(chunks)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint blob changed during read"
            ) from exc
        finally:
            os.close(descriptor)

    def _hydrate_journal_record(
        self,
        record: Mapping[str, Any],
        *,
        blob_cache: Dict[str, Any],
    ) -> JsonDict:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise CheckpointCorruptionError(
                "journal payload must be an object"
            )
        if set(payload) != {CHECKPOINT_PAYLOAD_ENVELOPE_KEY}:
            return dict(record)
        envelope = _exact_mapping(
            payload[CHECKPOINT_PAYLOAD_ENVELOPE_KEY],
            CHECKPOINT_PAYLOAD_ENVELOPE_KEYS,
            "checkpoint Merkle payload",
        )
        if envelope["schema_version"] != CHECKPOINT_PAYLOAD_SCHEMA_VERSION:
            raise CheckpointCorruptionError(
                "checkpoint Merkle payload schema is unsupported"
            )
        hydrated = self._hydrate_checkpoint_value(
            envelope["root"],
            blob_cache=blob_cache,
            visiting=frozenset(),
        )
        if not isinstance(hydrated, Mapping):
            raise CheckpointCorruptionError(
                "hydrated journal payload must be an object"
            )
        return {**dict(record), "payload": dict(hydrated)}

    def _hydrate_checkpoint_value(
        self,
        value: Any,
        *,
        blob_cache: Dict[str, Any],
        visiting: frozenset[str],
    ) -> Any:
        if isinstance(value, Mapping) and set(value) == CHECKPOINT_BLOB_REF_KEYS:
            reference = _exact_mapping(
                value,
                CHECKPOINT_BLOB_REF_KEYS,
                "checkpoint blob reference",
            )
            if reference["schema_version"] != CHECKPOINT_BLOB_SCHEMA_VERSION:
                raise CheckpointCorruptionError(
                    "checkpoint blob reference schema is unsupported"
                )
            declared_digest = reference["digest"]
            declared_length = reference["byte_length"]
            if (
                not isinstance(declared_digest, str)
                or not declared_digest.startswith("sha256:")
                or type(declared_length) is not int
                or declared_length < 0
            ):
                raise CheckpointCorruptionError(
                    "checkpoint blob reference is invalid"
                )
            digest = declared_digest.removeprefix("sha256:")
            path = self._checkpoint_blob_path(digest)
            if digest in visiting:
                raise CheckpointCorruptionError(
                    "checkpoint blob reference cycle detected"
                )
            if digest not in blob_cache:
                content = self._read_checkpoint_blob_bytes(path)
                actual_digest = hashlib.sha256(content).hexdigest()
                if actual_digest != digest:
                    raise CheckpointCorruptionError(
                        "checkpoint blob hash mismatch"
                    )
                if len(content) != declared_length:
                    raise CheckpointCorruptionError(
                        "checkpoint blob byte length mismatch"
                    )
                try:
                    decoded = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CheckpointCorruptionError(
                        "checkpoint blob content is invalid JSON"
                    ) from exc
                if not isinstance(decoded, (dict, list)):
                    raise CheckpointCorruptionError(
                        "checkpoint blob content must be an object or array"
                    )
                blob_cache[digest] = self._hydrate_checkpoint_value(
                    decoded,
                    blob_cache=blob_cache,
                    visiting=visiting | {digest},
                )
            return copy.deepcopy(blob_cache[digest])
        if isinstance(value, Mapping):
            return {
                str(key): self._hydrate_checkpoint_value(
                    item,
                    blob_cache=blob_cache,
                    visiting=visiting,
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                self._hydrate_checkpoint_value(
                    item,
                    blob_cache=blob_cache,
                    visiting=visiting,
                )
                for item in value
            ]
        return value

    def _publish_commit(
        self, transaction_sequence: int, semantic_key: str
    ) -> JsonDict:
        self._require_lock(exclusive=True)
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
        self._finish_generation_write()
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

    @staticmethod
    def _has_global_fusion_evidence(value: Any) -> bool:
        if isinstance(value, Mapping):
            if value.get("kind") in {
                "global_candidate_gate",
                "global_candidate_page",
                "global_candidate_page_plan",
                "global_candidate_pass",
            }:
                return True
            return any(
                CheckpointBundle._has_global_fusion_evidence(item)
                for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(
                CheckpointBundle._has_global_fusion_evidence(item)
                for item in value
            )
        return False

    def _read_source_checkpoint_records(
        self,
    ) -> Tuple[JsonDict, Dict[str, List[JsonDict]], bool]:
        commit = self._read_commit()
        if commit["run_id"] != self._run_id:
            raise CheckpointCorruptionError(
                "checkpoint manifest and commit run IDs differ"
            )
        records_by_journal: Dict[str, List[JsonDict]] = {}
        global_evidence = False
        for journal, path in self._paths.items():
            records, corrupt_tail, complete = self._read_journal(journal, path)
            if corrupt_tail or not complete:
                raise CheckpointCorruptionError(
                    "legacy checkpoint cannot migrate an incomplete journal tail"
                )
            committed = self._committed_prefix(journal, records, commit)
            records_by_journal[journal] = records
            global_evidence = global_evidence or any(
                str(record.get("operation") or "").startswith("global_judge")
                or self._has_global_fusion_evidence(record.get("payload"))
                for record in committed
            )
        return commit, records_by_journal, global_evidence

    def _recover_migration_transaction(
        self,
        *,
        expected_config: Optional[Mapping[str, Any]],
    ) -> None:
        if not self.root.exists():
            return
        if not self._migration_transaction_candidates():
            return
        active_published = self.current_path.exists()
        try:
            marker = self._read_migration_transaction()
            self._roll_forward_migration_transaction(
                marker,
                marker_path=self._migration_transaction_path,
                expected_config=expected_config,
            )
        except (CheckpointCompatibilityError, CheckpointCorruptionError):
            if not active_published:
                raise
            self._resolve_visible_generation()

    def _migration_transaction_candidates(self) -> List[Path]:
        candidates: List[Path] = []
        if self._entry_exists(self.migration_marker_path):
            candidates.append(self.migration_marker_path)
        if self._entry_exists(self.migration_transactions_path):
            if (
                self.migration_transactions_path.is_symlink()
                or not self.migration_transactions_path.is_dir()
            ):
                raise CheckpointCorruptionError(
                    "checkpoint migration transaction directory is invalid"
                )
            candidates.extend(
                sorted(self.migration_transactions_path.glob("*.json"))
            )
        return candidates

    def _read_migration_transaction(self) -> JsonDict:
        candidates = self._migration_transaction_candidates()
        if not candidates:
            raise CheckpointCorruptionError(
                "checkpoint migration transaction is missing"
            )
        marker_path = candidates[0]
        if marker_path.is_symlink() or not marker_path.is_file():
            raise CheckpointCorruptionError(
                "checkpoint migration transaction entry is invalid"
            )
        marker = _exact_mapping(
            self._read_regular_json(
                marker_path,
                "checkpoint migration transaction",
            ),
            MIGRATION_TRANSACTION_KEYS,
            "checkpoint migration transaction",
        )
        unsigned = {
            key: value
            for key, value in marker.items()
            if key != "transaction_hash"
        }
        if marker["transaction_hash"] != _sha256(unsigned):
            raise CheckpointCorruptionError(
                "checkpoint migration transaction hash does not match contents"
            )
        if marker["schema_version"] != MIGRATION_TRANSACTION_SCHEMA_VERSION:
            raise CheckpointCompatibilityError(
                "unsupported checkpoint migration transaction version"
            )
        if (
            marker["source_schema_version"]
            != LEGACY_CHECKPOINT_SCHEMA_VERSION
            or marker["target_schema_version"] != CHECKPOINT_SCHEMA_VERSION
        ):
            raise CheckpointCorruptionError(
                "checkpoint migration transaction schema transition is invalid"
            )
        for key in (
            "transaction_id",
            "run_id",
            "source_manifest_hash",
            "target_config_fingerprint",
            "staging_directory",
        ):
            if not isinstance(marker[key], str) or not marker[key]:
                raise CheckpointCorruptionError(
                    "checkpoint migration transaction field is invalid: {0}".format(
                        key
                    )
                )
        staging_name = str(marker["staging_directory"])
        legacy_staging_name = "{0}{1}".format(
            MIGRATION_STAGING_PREFIX, marker["transaction_id"]
        )
        generation_staging_name = "{0}/{1}".format(
            GENERATION_DIRECTORY_NAME,
            legacy_staging_name,
        )
        if staging_name not in {
            legacy_staging_name,
            generation_staging_name,
        }:
            raise CheckpointCorruptionError(
                "checkpoint migration staging directory is invalid"
            )
        carrier_hashes = _exact_mapping(
            marker["carrier_hashes"],
            frozenset(name for _, name in MIGRATION_CARRIERS),
            "checkpoint migration carrier hashes",
        )
        for digest in carrier_hashes.values():
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise CheckpointCorruptionError(
                    "checkpoint migration carrier hash is invalid"
                )
        if marker_path != self.migration_marker_path and marker_path != (
            self.migration_transactions_path
            / "{0}.json".format(marker["transaction_id"])
        ):
            raise CheckpointCorruptionError(
                "checkpoint migration transaction path is invalid"
            )
        marker["carrier_hashes"] = carrier_hashes
        self._migration_transaction_path = marker_path
        return marker

    def _migration_staging_path(self, marker: Mapping[str, Any]) -> Path:
        return self.root / str(marker["staging_directory"])

    def _migration_generation_path(self, marker: Mapping[str, Any]) -> Path:
        return self.generations_path / "{0}{1}".format(
            GENERATION_PREFIX,
            marker["transaction_id"],
        )

    def _validate_migration_generation(
        self,
        *,
        marker: Mapping[str, Any],
        expected_config: Optional[Mapping[str, Any]],
        generation_path: Optional[Path] = None,
    ) -> CheckpointState:
        generation = generation_path or self._migration_staging_path(marker)
        if not generation.is_dir() or generation.is_symlink():
            raise CheckpointCorruptionError(
                "checkpoint migration staging generation is missing"
            )
        carrier_hashes = marker["carrier_hashes"]
        try:
            for _, name in MIGRATION_CARRIERS:
                path = generation / name
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or _file_sha256(path) != carrier_hashes[name]
                ):
                    raise CheckpointCorruptionError(
                        "checkpoint migration staged carrier is missing or corrupt: {0}".format(
                            name
                        )
                    )
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint migration generation changed during validation"
            ) from exc
        staged_manifest = _read_exact_json(
            generation / "manifest.json",
            "checkpoint migration staged manifest",
        )
        if staged_manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointCorruptionError(
                "checkpoint migration staging is not a v28 generation"
            )
        probe = CheckpointBundle(generation)
        state = probe._restore_current_generation(
            validated_expected=expected_config,
        )
        if state.run_id != marker["run_id"]:
            raise CheckpointCorruptionError(
                "checkpoint migration run IDs differ"
            )
        if (
            state.config.get("config_fingerprint")
            != marker["target_config_fingerprint"]
        ):
            raise CheckpointCorruptionError(
                "checkpoint migration target configuration is not bound"
            )
        return state

    def _roll_forward_migration_transaction(
        self,
        marker: Mapping[str, Any],
        *,
        marker_path: Optional[Path],
        expected_config: Optional[Mapping[str, Any]],
    ) -> None:
        staging = self._migration_staging_path(marker)
        generation = self._migration_generation_path(marker)

        if self.current_path.exists():
            return self._finish_migration_from_current(
                marker=marker,
                marker_path=marker_path,
                expected_config=expected_config,
            )

        source = generation if generation.exists() else staging
        try:
            staged_state = self._validate_migration_generation(
                marker=marker,
                expected_config=expected_config,
                generation_path=source,
            )
        except CheckpointCorruptionError:
            if self.current_path.exists():
                return self._finish_migration_from_current(
                    marker=marker,
                    marker_path=marker_path,
                    expected_config=expected_config,
                )
            if source != generation and generation.exists():
                staged_state = self._validate_migration_generation(
                    marker=marker,
                    expected_config=expected_config,
                    generation_path=generation,
                )
            else:
                raise

        self._ensure_generation_directory()
        if not generation.exists():
            source_parent = staging.parent
            try:
                os.replace(staging, generation)
            except FileNotFoundError:
                if self.current_path.exists():
                    return self._finish_migration_from_current(
                        marker=marker,
                        marker_path=marker_path,
                        expected_config=expected_config,
                    )
                if not generation.exists():
                    raise CheckpointCorruptionError(
                        "checkpoint migration generation disappeared during publication"
                    )
            else:
                self._fault_hook("migration_generation_published")
                self._fsync_directory(self.generations_path)
                if source_parent != self.generations_path:
                    self._fsync_directory(source_parent)
                self._fault_hook("migration_generation_directory_durable")
        generation_state = self._validate_migration_generation(
            marker=marker,
            expected_config=expected_config,
            generation_path=generation,
        )
        if generation_state != staged_state:
            raise CheckpointCorruptionError(
                "checkpoint migration published generation changed"
            )

        self._publish_current_generation(generation)
        self._bind_carrier_root(generation)

        active_state = self._restore_current_generation(
            validated_expected=staged_state.config,
        )
        if active_state != staged_state:
            raise CheckpointCorruptionError(
                "checkpoint migration active generation does not match staging"
            )
        self._loaded_manifest_schema_version = CHECKPOINT_SCHEMA_VERSION
        self._fault_hook("migration_active_validated")

        self._remove_migration_transaction(marker_path, marker)
        self._fault_hook("migration_transaction_removed")
        self._remove_migration_staging(staging, marker)
        self._reclaim_inactive_generations(active=generation)
        self._fault_hook("migration_cleanup_complete")

    def _finish_migration_from_current(
        self,
        *,
        marker: Mapping[str, Any],
        marker_path: Optional[Path],
        expected_config: Optional[Mapping[str, Any]],
    ) -> None:
        active = self._resolve_visible_generation()
        self._validate_migration_generation(
            marker=marker,
            expected_config=expected_config,
            generation_path=active,
        )
        self._loaded_manifest_schema_version = CHECKPOINT_SCHEMA_VERSION
        self._remove_migration_transaction(marker_path, marker)
        self._fault_hook("migration_transaction_removed")
        self._remove_migration_staging(
            self._migration_staging_path(marker),
            marker,
        )
        self._reclaim_inactive_generations(active=active)
        self._fault_hook("migration_cleanup_complete")

    def _ensure_generation_directory(self) -> None:
        self._require_lock(exclusive=True)
        if self._entry_exists(self.generations_path):
            try:
                value = os.lstat(self.generations_path)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint generation directory is invalid"
                ) from exc
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
                raise CheckpointCorruptionError(
                    "checkpoint generation directory is invalid"
                )
        else:
            self.generations_path.mkdir()
        self._fsync_directory(self.root)

    def _generation_entries_for_reclamation(self) -> List[Path]:
        if not self._entry_exists(self.generations_path):
            return []
        try:
            directory_stat = os.lstat(self.generations_path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation directory is unreadable"
            ) from exc
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise CheckpointCorruptionError(
                "checkpoint generation directory is invalid"
            )
        entries: List[Path] = []
        try:
            names = os.listdir(self.generations_path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation directory is unreadable"
            ) from exc
        for name in sorted(names):
            if not (
                name.startswith(GENERATION_PREFIX)
                or name.startswith(GENERATION_WRITE_STAGING_PREFIX)
            ):
                continue
            path = self.generations_path / name
            try:
                value = os.lstat(path)
            except OSError as exc:
                raise CheckpointCorruptionError(
                    "checkpoint generation entry changed during validation"
                ) from exc
            if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
                raise CheckpointCorruptionError(
                    "checkpoint generation entry is not a regular directory"
                )
            entries.append(path)
        return entries

    def _preflight_generation_reclamation(self) -> None:
        self._require_lock(exclusive=True)
        for path in self._generation_entries_for_reclamation():
            self._validate_generation_cleanup_members(
                path,
                allow_partial=path.name.startswith(
                    GENERATION_WRITE_STAGING_PREFIX
                ),
            )

    def _validate_generation_cleanup_members(
        self,
        path: Path,
        *,
        allow_partial: bool,
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup target is invalid"
            ) from exc
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise CheckpointCorruptionError(
                    "checkpoint generation cleanup target is not a directory"
                )
            names = set(os.listdir(descriptor))
            expected = {name for _, name in MIGRATION_CARRIERS}
            if (not allow_partial and names != expected) or not names <= expected:
                raise CheckpointCorruptionError(
                    "checkpoint generation cleanup found unsafe members"
                )
            for name in sorted(names):
                value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
                    raise CheckpointCorruptionError(
                        "checkpoint generation cleanup found a non-regular carrier"
                    )
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup validation failed"
            ) from exc
        finally:
            os.close(descriptor)

    def _secure_remove_generation_directory(
        self,
        path: Path,
        *,
        allow_partial: bool,
    ) -> None:
        valid_parent_and_prefix = (
            path.parent == self.generations_path
            and (
                path.name.startswith(GENERATION_PREFIX)
                or path.name.startswith(GENERATION_WRITE_STAGING_PREFIX)
                or path.name.startswith(MIGRATION_STAGING_PREFIX)
            )
        ) or (
            path.parent == self.root
            and path.name.startswith(MIGRATION_STAGING_PREFIX)
        )
        if not valid_parent_and_prefix:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup escaped checkpoint root"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup target is invalid"
            ) from exc
        directory_identity: Optional[Tuple[int, int, int]] = None
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise CheckpointCorruptionError(
                    "checkpoint generation cleanup target is not a directory"
                )
            directory_identity = self._directory_identity(opened)
            names = set(os.listdir(descriptor))
            expected = {name for _, name in MIGRATION_CARRIERS}
            if (not allow_partial and names != expected) or not names <= expected:
                raise CheckpointCorruptionError(
                    "checkpoint generation cleanup found unsafe members"
                )
            for name in sorted(names):
                value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
                    raise CheckpointCorruptionError(
                        "checkpoint generation cleanup found a non-regular carrier"
                    )
            for name in sorted(names):
                os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup failed safely"
            ) from exc
        finally:
            os.close(descriptor)
        try:
            linked = os.lstat(path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation changed during cleanup"
            ) from exc
        if (
            directory_identity is None
            or stat.S_ISLNK(linked.st_mode)
            or self._directory_identity(linked) != directory_identity
        ):
            raise CheckpointCorruptionError(
                "checkpoint generation changed identity during cleanup"
            )
        try:
            os.rmdir(path)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint generation cleanup could not remove directory"
            ) from exc
        self._fsync_directory(path.parent)

    def _reclaim_inactive_generations(self, *, active: Path) -> None:
        self._require_lock(exclusive=True)
        for path in self._generation_entries_for_reclamation():
            if path == active:
                continue
            self._secure_remove_generation_directory(
                path,
                allow_partial=path.name.startswith(
                    GENERATION_WRITE_STAGING_PREFIX
                ),
            )

    def _publish_current_generation(
        self,
        generation: Path,
        *,
        emit_migration_faults: bool = True,
    ) -> None:
        self._require_lock(exclusive=True)
        relative = generation.relative_to(self.root)
        content = "{0}\n".format(relative.as_posix()).encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".{0}.".format(CURRENT_POINTER_NAME),
            suffix=".tmp",
            dir=str(self.root),
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if emit_migration_faults:
                self._fault_hook("migration_current_prepared")
            os.replace(temporary, self.current_path)
            if emit_migration_faults:
                self._fault_hook("migration_current_replaced")
            self._fsync_directory(self.root)
            if emit_migration_faults:
                self._fault_hook("migration_current_directory_durable")
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _remove_migration_transaction(
        self,
        marker_path: Optional[Path],
        marker: Mapping[str, Any],
    ) -> None:
        if marker_path is None or not self._entry_exists(marker_path):
            return
        expected_paths = {
            self.migration_marker_path,
            self.migration_transactions_path
            / "{0}.json".format(marker["transaction_id"]),
        }
        if marker_path not in expected_paths:
            raise CheckpointCorruptionError(
                "checkpoint migration transaction cleanup escaped checkpoint root"
            )
        persisted = self._read_regular_json(
            marker_path,
            "checkpoint migration transaction",
        )
        if persisted.get("transaction_hash") != marker["transaction_hash"]:
            return
        self._durable_unlink(marker_path)

    def _remove_migration_staging(
        self,
        path: Path,
        marker: Mapping[str, Any],
    ) -> None:
        if path != self._migration_staging_path(marker):
            raise CheckpointCorruptionError(
                "checkpoint migration staging cleanup escaped checkpoint root"
            )
        if not self._entry_exists(path):
            return
        self._secure_remove_generation_directory(
            path,
            allow_partial=False,
        )

    @classmethod
    def _durable_unlink(cls, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        cls._fsync_directory(path.parent)

    def _migrate_v27_carriers(
        self,
        *,
        source_manifest: Mapping[str, Any],
        validated_config: Mapping[str, Any],
        source_commit: Mapping[str, Any],
        records_by_journal: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        migrated_records: Dict[str, List[JsonDict]] = {}
        migrated_heads: JsonDict = {}
        for journal in JOURNAL_NAMES:
            previous_hash = ""
            output: List[JsonDict] = []
            committed_count = int(
                source_commit["journal_heads"][journal]["count"]
            )
            committed_hash = ""
            for index, raw_record in enumerate(records_by_journal[journal], 1):
                record = copy.deepcopy(dict(raw_record))
                record["schema_version"] = CHECKPOINT_SCHEMA_VERSION
                record["previous_hash"] = previous_hash
                unsigned = {
                    key: record[key]
                    for key in record
                    if key != "record_hash"
                }
                record["record_hash"] = _sha256(unsigned)
                previous_hash = str(record["record_hash"])
                if index == committed_count:
                    committed_hash = previous_hash
                output.append(record)
            if committed_count == 0:
                committed_hash = ""
            migrated_records[journal] = output
            migrated_heads[journal] = {
                "count": committed_count,
                "sequence": committed_count,
                "record_hash": committed_hash,
            }

        migrated_commit = copy.deepcopy(dict(source_commit))
        migrated_commit["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        migrated_commit["journal_heads"] = migrated_heads
        commit_unsigned = {
            key: migrated_commit[key]
            for key in migrated_commit
            if key != "commit_hash"
        }
        migrated_commit["commit_hash"] = _sha256(commit_unsigned)
        manifest_unsigned = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": str(source_manifest["run_id"]),
            "config": copy.deepcopy(dict(validated_config)),
        }
        migrated_manifest = {
            **manifest_unsigned,
            "manifest_hash": _sha256(manifest_unsigned),
        }

        transaction_id = str(uuid.uuid4())
        self._ensure_generation_directory()
        staging = self.generations_path / "{0}{1}".format(
            MIGRATION_STAGING_PREFIX,
            transaction_id,
        )
        staging.mkdir()
        self._fsync_directory(self.generations_path)
        self._fault_hook("migration_staging_created")
        staged_bundle = CheckpointBundle(staging)
        for journal in JOURNAL_NAMES:
            _atomic_write_json_lines(
                staged_bundle._paths[journal], migrated_records[journal]
            )
            self._fault_hook("migration_staged_{0}".format(journal))
        _atomic_write_json(staged_bundle.commit_path, migrated_commit)
        self._fault_hook("migration_staged_commit")
        _atomic_write_json(staged_bundle.manifest_path, migrated_manifest)
        self._fault_hook("migration_staged_manifest")
        self._fsync_directory(staging)
        self._fault_hook("migration_staging_durable")

        carrier_hashes = {
            name: _file_sha256(staging / name)
            for _, name in MIGRATION_CARRIERS
        }
        marker_unsigned = {
            "schema_version": MIGRATION_TRANSACTION_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "source_schema_version": LEGACY_CHECKPOINT_SCHEMA_VERSION,
            "target_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": str(source_manifest["run_id"]),
            "source_manifest_hash": str(source_manifest["manifest_hash"]),
            "target_config_fingerprint": str(
                validated_config["config_fingerprint"]
            ),
            "staging_directory": staging.relative_to(self.root).as_posix(),
            "carrier_hashes": carrier_hashes,
        }
        marker = {
            **marker_unsigned,
            "transaction_hash": _sha256(marker_unsigned),
        }
        self._validate_migration_generation(
            marker=marker,
            expected_config=validated_config,
        )
        self._fault_hook("migration_staging_validated")
        if self.migration_transactions_path.exists() and (
            self.migration_transactions_path.is_symlink()
            or not self.migration_transactions_path.is_dir()
        ):
            raise CheckpointCorruptionError(
                "checkpoint migration transaction directory is invalid"
            )
        self.migration_transactions_path.mkdir(exist_ok=True)
        self._fsync_directory(self.root)
        marker_path = self.migration_transactions_path / "{0}.json".format(
            transaction_id
        )
        self._atomic_write_json(marker_path, marker)
        self._migration_transaction_path = marker_path
        self._fault_hook("migration_transaction_published")
        self._fault_hook("migration_marker_published")
        self._roll_forward_migration_transaction(
            marker,
            marker_path=marker_path,
            expected_config=validated_config,
        )

    def _read_manifest_envelope(
        self,
        *,
        expected_config: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        value = _exact_mapping(
            self._read_regular_json(
                self.manifest_path,
                "checkpoint manifest",
            ),
            MANIFEST_KEYS,
            "checkpoint manifest",
        )
        unsigned = {key: value[key] for key in value if key != "manifest_hash"}
        if value["manifest_hash"] != _sha256(unsigned):
            raise CheckpointCorruptionError("checkpoint manifest hash does not match contents")
        source_schema = value["schema_version"]
        if source_schema not in SUPPORTED_CHECKPOINT_SCHEMA_VERSIONS:
            raise CheckpointCompatibilityError("unsupported checkpoint manifest version")
        if not isinstance(value["run_id"], str) or not value["run_id"]:
            raise CheckpointCorruptionError("checkpoint run ID is invalid")
        self._run_id = str(value["run_id"])
        self._loaded_manifest_schema_version = str(source_schema)
        if not isinstance(value["config"], Mapping):
            raise CheckpointCorruptionError(
                "checkpoint manifest and config schema versions differ"
            )
        config_source_schema = value["config"].get("schema_version")
        if config_source_schema != source_schema:
            raise CheckpointCorruptionError(
                "checkpoint manifest and config schema versions differ"
            )
        if source_schema == LEGACY_CHECKPOINT_SCHEMA_VERSION:
            source_commit, records_by_journal, global_evidence = (
                self._read_source_checkpoint_records()
            )
            runtime = value["config"].get("runtime_identity")
            persisted_mode = (
                runtime.get("fusion_mode")
                if isinstance(runtime, Mapping)
                and "fusion_mode" in runtime
                else None
            )
            expected_runtime = (
                expected_config.get("runtime_identity")
                if isinstance(expected_config, Mapping)
                else None
            )
            expected_mode = (
                expected_runtime.get("fusion_mode")
                if isinstance(expected_runtime, Mapping)
                else None
            )
            migration_mode = persisted_mode or expected_mode
            if migration_mode is None:
                if global_evidence:
                    migration_mode = "retrieval-global"
                else:
                    raise CheckpointCompatibilityError(
                        "legacy checkpoint has no evidence for fusion_mode migration"
                    )
            if global_evidence and migration_mode != "retrieval-global":
                raise CheckpointCompatibilityError(
                    "legacy checkpoint global history contradicts fusion_mode"
                )
            validated_config = validate_checkpoint_config(
                value["config"],
                legacy_fusion_mode=str(migration_mode),
            )
            self._migrate_v27_carriers(
                source_manifest=value,
                validated_config=validated_config,
                source_commit=source_commit,
                records_by_journal=records_by_journal,
            )
            return self._read_manifest_envelope(
                expected_config=expected_config
            )
        validated_config = validate_checkpoint_config(value["config"])
        value["config"] = validated_config
        value["schema_version"] = CHECKPOINT_SCHEMA_VERSION
        return value

    def _read_commit(self) -> JsonDict:
        value = _exact_mapping(
            self._read_regular_json(
                self.commit_path,
                "checkpoint commit",
            ),
            COMMIT_KEYS,
            "checkpoint commit",
        )
        unsigned = {key: value[key] for key in value if key != "commit_hash"}
        if value["commit_hash"] != _sha256(unsigned):
            raise CheckpointCorruptionError("checkpoint commit hash does not match contents")
        if value["schema_version"] not in SUPPORTED_CHECKPOINT_SCHEMA_VERSIONS:
            raise CheckpointCompatibilityError("unsupported checkpoint commit version")
        if value["schema_version"] != self._loaded_manifest_schema_version:
            raise CheckpointCorruptionError(
                "checkpoint manifest and commit schema versions differ"
            )
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
        raw = self._read_regular_bytes(
            path,
            "{0} journal".format(journal),
        )
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
            if (
                record["schema_version"]
                != self._loaded_manifest_schema_version
            ):
                raise CheckpointCorruptionError(
                    "journal and checkpoint source schema versions differ"
                )
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
        raw = self._read_regular_bytes(
            path,
            "{0} journal".format(journal),
        )
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
                    self._durable_append_bytes(path, b"\n")
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

    def _byte_end_for_record_count(self, path: Path, count: int) -> int:
        if count == 0:
            return 0
        raw = self._read_regular_bytes(path, "checkpoint journal")
        position = 0
        for _ in range(count):
            newline = raw.find(b"\n", position)
            if newline < 0:
                raise CheckpointCorruptionError("committed journal delimiter is missing")
            position = newline + 1
        return position

    def _durable_append_bytes(self, path: Path, content: bytes) -> None:
        linked = self._assert_contained_regular_file(path, "checkpoint journal")
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal could not be opened for repair"
            ) from exc
        try:
            if self._directory_identity(os.fstat(descriptor)) != self._directory_identity(
                linked
            ):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity before repair"
                )
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
            if self._directory_identity(os.fstat(descriptor)) != self._directory_identity(
                os.lstat(path)
            ):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity during repair"
                )
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal repair append failed"
            ) from exc
        finally:
            os.close(descriptor)

    def _durable_truncate(self, path: Path, size: int) -> None:
        linked = self._assert_contained_regular_file(path, "checkpoint journal")
        flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(path), flags)
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal could not be opened for truncate"
            ) from exc
        try:
            if self._directory_identity(os.fstat(descriptor)) != self._directory_identity(
                linked
            ):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity before truncate"
                )
            os.ftruncate(descriptor, size)
            os.fsync(descriptor)
            if self._directory_identity(os.fstat(descriptor)) != self._directory_identity(
                os.lstat(path)
            ):
                raise CheckpointCorruptionError(
                    "checkpoint journal changed identity during truncate"
                )
        except OSError as exc:
            raise CheckpointCorruptionError(
                "checkpoint journal truncate failed"
            ) from exc
        finally:
            os.close(descriptor)

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
    with bundle._checkpoint_lock(exclusive=True, create_root=False):
        bundle._reload_latest_for_write_locked()
        return _publish_output_transaction_locked(
            bundle=bundle,
            attribution_path=attribution_path,
            lineage_path=lineage_path,
            report=report,
            message_lineage=message_lineage,
            stop_requested=stop_requested,
            fault_hook=fault_hook,
        )


def _publish_output_transaction_locked(
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
    bundle._require_lock(exclusive=True)
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
    if bundle._entry_exists(bundle.output_commit_path):
        existing = _validate_output_commit(
            bundle._read_regular_json(
                bundle.output_commit_path,
                "output commit",
            ),
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


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name), suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
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


def _atomic_write_json_lines(
    path: Path, records: Sequence[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".{0}.".format(path.name),
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )
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
