from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import multiprocessing
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    BoundedJudgeCapability,
    OfflineJudgeCapability,
    root_confirmation_request_projection_identity,
)
from trace_attribution.global_judge import (
    GLOBAL_CANDIDATE_PROMPT_SCHEMA_VERSION,
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
    active_focus_text_sha256,
)
from trace_attribution.causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    AttributionHypothesis,
    CausalStepJudgment,
    DefectState,
    FactorRoleJudgment,
    FrontierItem,
    LocalStateOwner,
    PredecessorAssessment,
    RecursiveAttributionReport,
    RootConfirmation,
    canonical_confirmation_publication_provenance,
    confirmation_counterfactual_for,
    confirmation_identity_for,
    confirmation_response_identity_for,
    seed_binding_identity_for,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    FACTOR_ROLE_CONTRACT_IDENTITY,
    OUTPUT_SCHEMA_VERSION,
    CheckpointBundle,
    CheckpointCompatibilityError,
    CheckpointCorruptionError,
    CheckpointLockError,
    CheckpointState,
    CompletedCheckpointReplayProof,
    LegacyProjectionNotRequired,
    LegacyProjectionRequired,
    _sha256,
    build_checkpoint_config,
    publish_output_transaction,
    validate_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.hypotheses import HypothesisLedger, RecursiveFrontier
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    classify_legacy_projection_shape,
    _provider_state_payload,
    _validate_provider_state,
)


def sample_trace() -> dict:
    return {
        "case_id": "checkpoint-case",
        "records": [
            {
                "record_id": "only",
                "ref": "record:only",
                "component": "result_processing",
                "event_type": "response.output",
                "title": "Only node",
                "status": "completed",
                "timestamp": "2026-07-21T00:00:00Z",
                "data": {"content": "A complete answer."},
                "source_refs": [],
            }
        ],
    }


def confirmed_root_trace() -> dict:
    return {
        "case_id": "checkpoint-confirmed-root",
        "records": [
            {
                "record_id": "decision",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "The decision introduced the defect."},
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:decision"],
                "data": {"actual": "The result contains the defect."},
            },
        ],
    }


def multi_seed_global_trace() -> dict:
    return {
        "case_id": "global-resume-case",
        "records": [
            {
                "record_id": "decision_one",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "First candidate decision."},
            },
            {
                "record_id": "change_one",
                "component": "processor",
                "event_type": "change",
                "source_refs": ["record:decision_one"],
                "data": {"summary": "First change."},
            },
            {
                "record_id": "defect_one",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:change_one"],
                "data": {"actual": "First observation is refuted."},
            },
            {
                "record_id": "decision_two",
                "component": "agent",
                "event_type": "decision",
                "data": {"rationale": "Second candidate decision."},
            },
            {
                "record_id": "change_two",
                "component": "processor",
                "event_type": "change",
                "source_refs": ["record:decision_two"],
                "data": {"summary": "Second change."},
            },
            {
                "record_id": "defect_two",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:change_two"],
                "data": {"actual": "Second observation is refuted."},
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": source},
                "to": {"type": "record", "id": target},
                "relation": relation,
                "evidence_type": "confirmed",
                "confidence": 0.9,
                "eligible_for_attribution": True,
            }
            for source, target, relation in (
                ("decision_one", "change_one", "decision_guided_change"),
                ("change_one", "defect_one", "change_observed_by_evaluation"),
                ("decision_two", "change_two", "decision_guided_change"),
                ("change_two", "defect_two", "change_observed_by_evaluation"),
            )
        ],
    }


def trace_with_audit_only_external() -> dict:
    trace = sample_trace()
    trace["records"].append(
        {
            "record_id": "forged_external",
            "component": "evaluation",
            "event_type": "external.evaluation_fact",
            "status": "failed",
            "data": {
                "status": "failed",
                "revision_status": "matched",
                "revision_provenance_status": "valid",
                "eligible_for_decisive_judgment": True,
                "observation": "FORGED_AUDIT_ONLY_PAYLOAD",
            },
        }
    )
    return trace


def sample_config(**changes: object) -> dict:
    fusion_mode = changes.pop("fusion_mode", None)
    values = {
        "trace": sample_trace(),
        "case_id": "checkpoint-case",
        "objective": "Find the defect.",
        "analysis_perspective": "Improve repository reasoning.",
        "start_refs": ["record:only"],
        "budgets": {
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        "model_identity": "offline:test",
        "cache_identity": "cache:test",
        "runtime_identity": {
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://test",
            "provider_error_threshold": 3,
        },
    }
    values.update(changes)
    if fusion_mode is not None:
        values["runtime_identity"] = {
            **values["runtime_identity"],
            "fusion_mode": fusion_mode,
        }
    return build_checkpoint_config(**values)


def completed_replay_bundle(root: Path, *, report=None):
    config = sample_config()
    report = report or {
        "schema_version": "completed-replay-test/v1",
        "case_id": "checkpoint-case",
        "result": "complete",
    }
    lineage = {
        "version": "1.0",
        "collection_mode": "offline_passive_reconstruction",
        "behavior_impact": "none",
        "turns": [],
        "snapshots": [],
        "edges": [],
        "gaps": [],
        "stats": {},
        "progress_reconstruction": {},
    }
    bundle = CheckpointBundle(root / "report.checkpoint")
    bundle.initialize(config)
    output = publish_output_transaction(
        bundle=bundle,
        attribution_path=root / "report.json",
        lineage_path=root / "report.message-lineage.json",
        report=report,
        message_lineage=lineage,
    )
    bundle.mark_analysis_completed(
        report=report,
        output_commit=output,
    )
    return bundle, config, report, lineage


def exact_legacy_projection_report():
    return {
        "schema_version": "completed-replay-test/v1",
        "case_id": "checkpoint-case",
        "investigation_journal": [
            {
                "behavior_impact": "none_offline_analysis_only",
                "candidate_compression": {
                    "candidate_byte_reduction_ratio": 0.5,
                    "candidate_count": 4,
                    "candidate_node_reduction_ratio": 0.5,
                    "capsule_bytes": 100,
                    "global_fusion_payload": {
                        "capsule_to_trace_expansion_ratio": 0.5,
                        "dense_root_matrix": True,
                        "eligible": False,
                        "max_open_root_candidates": 3,
                        "max_payload_bytes": 65536,
                        "negative_compression": False,
                        "open_root_candidate_count": 4,
                        "oversized": True,
                        "reason": "oversized_dense_root_matrix",
                    },
                    "open_root_candidate_count": 4,
                    "trace_json_bytes": 200,
                    "trace_node_count": 8,
                },
                "defect_fingerprint": "defect-one",
                "fallback": "recursive_backward_taint",
                "kind": "global_candidate_gate",
                "reason": "oversized_dense_root_matrix",
                "seed_ref": "record:only",
                "status": "bypassed",
            }
        ],
        "metadata": {
            "analysis": "agentic_recursive_semantic_taint",
            "fusion_mode": "off",
            "global_candidate_pass_count": 0,
            "global_judge_physical_request_count": 0,
            "global_candidate_judgments": [],
            "candidate_compression": [],
            "recursive_expansion_reasons": [],
            "global_candidate_failures": [],
        },
    }


def _write_hashed_json(path: Path, value: dict, hash_key: str) -> None:
    unsigned = {key: item for key, item in value.items() if key != hash_key}
    value[hash_key] = _sha256(unsigned)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _rewrite_bundle_as_legal_v27(bundle: CheckpointBundle) -> None:
    legacy_schema = "recursive-attribution-checkpoint/v27"

    def legacy_provider_state(value):
        if isinstance(value, list):
            return [legacy_provider_state(item) for item in value]
        if not isinstance(value, dict):
            return value
        migrated = {
            key: legacy_provider_state(item) for key, item in value.items()
        }
        if str(migrated.get("schema") or "").startswith(
            "recursive-provider-state/"
        ):
            circuit = dict(migrated["circuit"])
            migrated.pop("previous_failure", None)
            for key in (
                "disposition",
                "first_request",
                "first_failure_at",
                "opened_at",
            ):
                circuit.pop(key, None)
            migrated["schema"] = "recursive-provider-state/v1"
            migrated["circuit"] = circuit
            unsigned = {
                key: item for key, item in migrated.items() if key != "identity"
            }
            migrated["identity"] = _sha256(unsigned)
        return migrated

    journal_heads = {}
    for name, path in (
        ("frontier", bundle.frontier_path),
        ("hypotheses", bundle.hypotheses_path),
        ("actions", bundle.actions_path),
    ):
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        previous_hash = ""
        for record in records:
            record["schema_version"] = legacy_schema
            record["payload"] = legacy_provider_state(record["payload"])
            record["previous_hash"] = previous_hash
            unsigned = {
                key: item for key, item in record.items() if key != "record_hash"
            }
            record["record_hash"] = _sha256(unsigned)
            previous_hash = record["record_hash"]
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        journal_heads[name] = {
            "count": len(records),
            "sequence": len(records),
            "record_hash": previous_hash,
        }

    commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
    commit["schema_version"] = legacy_schema
    commit["journal_heads"] = journal_heads
    _write_hashed_json(bundle.commit_path, commit, "commit_hash")

    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    config = dict(manifest["config"])
    config["schema_version"] = legacy_schema
    config["runtime_identity"] = dict(config["runtime_identity"])
    config["runtime_identity"].pop("fusion_mode", None)
    semantic = {
        key: item for key, item in config.items() if key != "config_fingerprint"
    }
    config["config_fingerprint"] = _sha256(semantic)
    manifest["schema_version"] = legacy_schema
    manifest["config"] = config
    _write_hashed_json(bundle.manifest_path, manifest, "manifest_hash")


def _visible_checkpoint_root(root: Path) -> Path:
    current = root / "CURRENT"
    if not current.is_file():
        return root
    relative = current.read_text(encoding="utf-8").strip()
    if not relative:
        raise AssertionError("CURRENT must name a generation")
    return root / relative


def _checkpoint_carrier_schemas(root: Path) -> set[str]:
    active = _visible_checkpoint_root(root)
    schemas = {
        json.loads((active / "manifest.json").read_text(encoding="utf-8"))[
            "schema_version"
        ],
        json.loads((active / "commit.json").read_text(encoding="utf-8"))[
            "schema_version"
        ],
    }
    for name in (
        "frontier.jsonl",
        "hypotheses.jsonl",
        "investigation-actions.jsonl",
    ):
        schemas.update(
            json.loads(line)["schema_version"]
            for line in (active / name).read_text(encoding="utf-8").splitlines()
        )
    return schemas


def _migration_pause_worker(root, config, paused, release, result):
    def pause_before_current(stage):
        if stage != "migration_generation_directory_durable":
            return
        paused.set()
        if not release.wait(10):
            raise AssertionError("stale migration release timed out")

    try:
        state = CheckpointBundle(
            Path(root),
            fault_hook=pause_before_current,
        ).restore(expected_config=config)
        result.put(("ok", state.transaction_sequence))
    except BaseException as exc:
        result.put(("error", repr(exc)))


def _migration_append_worker(root, config, started, finished, result):
    started.set()
    try:
        bundle = CheckpointBundle(Path(root))
        bundle.initialize(config)
        bundle.record_action(
            "concurrent_append",
            "concurrency:migration-writer",
            {"writer": "migration-writer"},
        )
        result.put(("ok", "migration-writer"))
    except BaseException as exc:
        result.put(("error", repr(exc)))
    finally:
        finished.set()


def _barrier_append_worker(
    root,
    config,
    writer,
    ready,
    start,
    finished,
    result,
):
    try:
        bundle = CheckpointBundle(Path(root))
        bundle.initialize(config)
        ready.put(writer)
        if not start.wait(10):
            raise AssertionError("concurrent writer start timed out")
        bundle.record_action(
            "concurrent_append",
            "concurrency:{0}".format(writer),
            {"writer": writer},
        )
        result.put(("ok", writer))
    except BaseException as exc:
        result.put(("error", writer, repr(exc)))
    finally:
        finished.set()


def _spanning_reader_worker(root, config, paused, release, result):
    bundle = CheckpointBundle(Path(root))
    read_commit = bundle._read_commit

    def pause_after_generation_resolution():
        paused.set()
        if not release.wait(10):
            raise AssertionError("spanning reader release timed out")
        return read_commit()

    bundle._read_commit = pause_after_generation_resolution
    try:
        state = bundle.restore(expected_config=config)
        result.put(("ok", state.transaction_sequence))
    except BaseException as exc:
        result.put(("error", repr(exc)))


def _checkpoint_regular_file_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.name != ".checkpoint.lock"
    )


def _resign_completed_lineage(bundle: CheckpointBundle, lineage: dict) -> None:
    output = json.loads(bundle.output_commit_path.read_text(encoding="utf-8"))
    lineage_path = Path(output["lineage_path"])
    lineage_path.write_text(
        json.dumps(lineage, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    output["lineage_hash"] = hashlib.sha256(lineage_path.read_bytes()).hexdigest()
    _write_hashed_json(bundle.output_commit_path, output, "output_commit_hash")

    actions = [
        json.loads(line)
        for line in bundle.actions_path.read_text(encoding="utf-8").splitlines()
    ]
    terminal = actions[-1]
    terminal["payload"]["lineage_hash"] = output["lineage_hash"]
    terminal["payload"]["output_commit_hash"] = output["output_commit_hash"]
    unsigned_terminal = {
        key: item for key, item in terminal.items() if key != "record_hash"
    }
    terminal["record_hash"] = _sha256(unsigned_terminal)
    bundle.actions_path.write_text(
        "".join(
            json.dumps(action, ensure_ascii=False, sort_keys=True) + "\n"
            for action in actions
        ),
        encoding="utf-8",
    )

    commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
    commit["journal_heads"]["actions"]["record_hash"] = terminal["record_hash"]
    _write_hashed_json(bundle.commit_path, commit, "commit_hash")


class CountingOfflineJudge(OfflineJudgeCapability):
    def __init__(self) -> None:
        self.step_calls = 0
        self.confirmation_calls = 0

    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="absent",
            current_defect_reason="The output satisfies the objective.",
            predecessors=(),
            candidate_introduction=False,
            confidence=1.0,
        )

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        raise AssertionError("no root confirmation is expected")


class ConfirmedSingleNodeJudge(CountingOfflineJudge):
    def judge_step_offline(self, request):
        self.step_calls += 1
        if request.current_node.ref == "record:defect":
            return CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="present",
                current_defect_reason="The observed defect is present.",
                predecessors=(
                    PredecessorAssessment(
                        ref="record:decision",
                        relation="same_defect_propagation",
                        reason="The decision propagates the active defect.",
                        confidence=0.9,
                        recurse=True,
                        evidence_refs=("record:decision",),
                    ),
                ),
                candidate_introduction=False,
                confidence=0.9,
            )
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason="The only node contains the active defect.",
            predecessors=(),
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context[
                        "active_hypothesis_id"
                    ],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently confirm the only candidate.",
            },
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The decision introduced the defect.",
            reason="The only candidate is a necessary root.",
            counterfactual=confirmation_counterfactual_for(
                request.candidate_ref,
                "confirmed",
            ),
            confidence=0.9,
            evidence_refs=[request.candidate_ref],
        )


def refresh_checkpoint_confirmation_response_identity(
    confirmation: dict,
) -> None:
    confirmation["response_identity"] = confirmation_response_identity_for(
        confirmation_identity=confirmation["confirmation_identity"],
        status=confirmation["status"],
        excerpt=confirmation["excerpt"],
        reason=confirmation["reason"],
        counterfactual=confirmation["counterfactual"],
        confidence=confirmation["confidence"],
        evidence_refs=tuple(confirmation["evidence_refs"]),
        counterfactual_status=confirmation["counterfactual_status"],
        factor_role=confirmation["factor_role"],
        competitor_comparisons=tuple(
            confirmation["competitor_comparisons"]
        ),
        factor_mechanism=confirmation["factor_mechanism"],
        analysis_perspective=confirmation.get("analysis_perspective", ""),
    )


def forge_checkpoint_root_identity(report_payload: dict, ghost_ref: str) -> None:
    confirmation = report_payload["confirmations"][0]
    seed = report_payload["seed_results"][0]
    confirmation["candidate_ref"] = ghost_ref
    confirmation["counterfactual"] = confirmation_counterfactual_for(
        ghost_ref,
        confirmation["status"],
        counterfactual_status=confirmation["counterfactual_status"],
    )
    confirmation["recursive_path"] = [ghost_ref, seed["start_ref"]]
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=ghost_ref,
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=confirmation["recursive_path"],
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    refresh_checkpoint_confirmation_response_identity(confirmation)
    root = report_payload["confirmed_roots"][0]
    role_binding = copy.deepcopy(
        root.get("provenance", {}).get("active_role_binding")
    )
    if role_binding is not None:
        role_binding["candidate_ref"] = ghost_ref
    root["node_ref"] = ghost_ref
    root["recursive_path"] = list(confirmation["recursive_path"])
    root["counterfactual"] = confirmation["counterfactual"]
    root["confirmation"] = copy.deepcopy(confirmation)
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    if role_binding is not None:
        root["provenance"]["active_role_binding"] = role_binding
    report_payload["root_causes"][0]["node_ref"] = ghost_ref
    seed["confirmed_root_refs"] = [ghost_ref]
    seed["confirmation_identities"] = [confirmation["confirmation_identity"]]


def forge_checkpoint_root_path(report_payload: dict, recursive_path: list[str]) -> None:
    confirmation = report_payload["confirmations"][0]
    old_path = list(confirmation["recursive_path"])
    old_confirmation_identity = confirmation["confirmation_identity"]
    old_response_identity = confirmation["response_identity"]
    confirmation["recursive_path"] = list(recursive_path)
    confirmation["confirmation_identity"] = confirmation_identity_for(
        hypothesis_id=confirmation["hypothesis_id"],
        hypothesis_semantic_hash=confirmation["hypothesis_semantic_hash"],
        candidate_ref=confirmation["candidate_ref"],
        defect_fingerprint=confirmation["defect_fingerprint"],
        recursive_path=confirmation["recursive_path"],
        seed_binding_identity=confirmation["seed_binding_identity"],
    )
    refresh_checkpoint_confirmation_response_identity(confirmation)
    new_confirmation_identity = confirmation["confirmation_identity"]
    new_response_identity = confirmation["response_identity"]

    metadata = report_payload["metadata"]
    request_surfaces = [
        metadata["confirmation_queue"][0],
        metadata["confirmation_journal"][0],
        metadata["confirmation_action_projection"][0],
    ]
    canonical_request = copy.deepcopy(
        request_surfaces[0]["factual_request_projection"]
    )
    request_facts = canonical_request["facts"]
    request_facts["recursive_path"] = list(recursive_path)
    references_by_ref = {
        item["resolved_ref"]: item
        for item in request_facts["recursive_path_references"]
    }
    request_facts["recursive_path_references"] = [
        copy.deepcopy(references_by_ref[ref]) for ref in recursive_path
    ]
    old_request_identity = request_surfaces[0]["semantic_identity"]
    new_request_identity = root_confirmation_request_projection_identity(
        canonical_request
    )

    def rewrite(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "recursive_path" and item == old_path:
                    value[key] = list(recursive_path)
                elif item == old_confirmation_identity:
                    value[key] = new_confirmation_identity
                elif item == old_response_identity:
                    value[key] = new_response_identity
                elif isinstance(item, str) and old_request_identity in item:
                    value[key] = item.replace(
                        old_request_identity,
                        new_request_identity,
                    )
                else:
                    rewrite(item)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if item == old_confirmation_identity:
                    value[index] = new_confirmation_identity
                elif item == old_response_identity:
                    value[index] = new_response_identity
                elif isinstance(item, str) and old_request_identity in item:
                    value[index] = item.replace(
                        old_request_identity,
                        new_request_identity,
                    )
                else:
                    rewrite(item)

    rewrite(report_payload)
    for surface in request_surfaces:
        surface["factual_request_projection"] = copy.deepcopy(
            canonical_request
        )
        surface["confirmation"] = copy.deepcopy(confirmation)
        surface["response_identity"] = new_response_identity
    root = report_payload["confirmed_roots"][0]
    role_binding = copy.deepcopy(
        root.get("provenance", {}).get("active_role_binding")
    )
    root["recursive_path"] = list(recursive_path)
    root["confirmation"] = copy.deepcopy(confirmation)
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    if role_binding is not None:
        root["provenance"]["active_role_binding"] = role_binding
    report_payload["seed_results"][0]["confirmation_identities"] = [
        confirmation["confirmation_identity"]
    ]


def forge_checkpoint_factor_path(payload: dict, recursive_path: list[str]) -> None:
    container = payload.get("metadata", payload)
    action_key = (
        "factor_role_action_projections"
        if "factor_role_action_projections" in container
        else "factor_role_action_projection"
    )
    action = container[action_key][0]
    original = FactorRoleJudgment.from_dict(action["judgment"])
    rewritten = replace(
        original,
        recursive_path=tuple(recursive_path),
    )
    action["judgment"] = rewritten.to_dict()
    action["judgment_identity"] = rewritten.judgment_identity
    container["factor_role_judgments"] = [rewritten.to_dict()]
    container["factor_role_journal"] = [
        {**copy.deepcopy(action), "status": "completed"}
    ]
    queue = next(
        item
        for item in container["confirmation_queue"]
        if item["review_scope"] == "non_root"
        and item["candidate_ref"] == rewritten.candidate_ref
    )
    queue["factor_role_judgment"] = rewritten.to_dict()
    queue["response_identity"] = rewritten.judgment_identity
    for collection, embedded_key in (
        ("contributing_conditions", "confirmation"),
        ("amplifying_factors", "confirmation"),
        ("downstream_materializations", "role_judgment"),
        ("rejected_candidates", "confirmation"),
    ):
        for publication in payload.get(collection) or ():
            candidate_ref = str(
                publication.get("node_ref")
                or publication.get("candidate_ref")
                or ""
            )
            if candidate_ref != rewritten.candidate_ref:
                continue
            publication["recursive_path"] = list(recursive_path)
            publication[embedded_key] = rewritten.to_dict()
            publication["provenance"]["response_identity"] = (
                rewritten.judgment_identity
            )
            publication["provenance"]["judgment_identity"] = (
                rewritten.judgment_identity
            )


class InterruptingGlobalNoDefectJudge(CountingOfflineJudge, GlobalJudgeCapability):
    def __init__(self, *, interrupt_on_call=0):
        super().__init__()
        self.interrupt_on_call = interrupt_on_call
        self.global_calls = []

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_calls.append(request.start_refs)
        if len(self.global_calls) == self.interrupt_on_call:
            raise KeyboardInterrupt("interrupt during the second global seed pass")
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status="absent",
                input_defect_status=(
                    "absent"
                    if capsule.candidate_ref
                    in request.open_authored_root_candidate_refs
                    else "unknown"
                ),
                output_defect_status="absent",
                causal_path_refs=(
                    tuple(capsule.downstream_path)
                    if capsule.candidate_ref
                    in request.open_authored_root_candidate_refs
                    else ()
                ),
                counterfactual={
                    "intervention_ref": capsule.candidate_ref,
                    "intervention_kind": "replace_with_semantically_correct_behavior",
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                compared_candidate_refs=request.open_authored_root_candidate_refs,
                causal_role="exculpatory_evidence",
                responsibility="none",
                candidate_phase="intermediate",
                obligation_status_before="unknown",
                obligation_status_after="unknown",
                repair_window_effect="remained_open",
                failure_mode="none",
                obligation_refs=(),
                contribution_mechanism=None,
                reason="The scripted evidence refutes this observed defect.",
                evidence_refs=(capsule.candidate_ref,),
                confidence=1.0,
            )
            for capsule in request.capsules
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="no_defect",
                reason="The complete scripted candidate set refutes the defect.",
                assessments=assessments,
                selected_candidate_refs=(),
                expansion_requests=(),
                decisive_evidence_refs=(request.capsules[0].candidate_ref,),
                missing_evidence=(),
                confidence=1.0,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": request.active_defect.fingerprint,
                    "active_focus_text_hash": request.active_focus_text_hash,
                },
            ),
            0,
        )


class InterruptingOfflineJudge(CountingOfflineJudge):
    def judge_step_offline(self, request):
        self.step_calls += 1
        raise KeyboardInterrupt("simulated SIGINT inside Judge boundary")


class InterruptingBoundedJudge(BoundedJudgeCapability):
    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.step_calls = 0
        self.allowances = []

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        self.allowances.append(max_physical_requests)
        if self.interrupt:
            raise KeyboardInterrupt("provider may have received the request")
        raise AssertionError("an in-flight bounded request must not be repeated")

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class ExactFailureJudge(BoundedJudgeCapability):
    def __init__(self) -> None:
        self.step_calls = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.consecutive_provider_errors = 0
        self.provider_error_threshold = 3

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        raise BoundedJudgeCallError("exact step failure", physical_requests=1)

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class ExactRejudgeFailureJudge(ExactFailureJudge):
    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        if self.step_calls > 1:
            raise BoundedJudgeCallError("exact rejudge failure", physical_requests=1)
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="unknown",
                current_defect_reason="Inspect the local node before deciding.",
                predecessors=(),
                candidate_introduction=False,
                missing_evidence=("inspect current node",),
                suggested_investigation={
                    "tool": "inspect_node",
                    "arguments": {"ref": request.current_node.ref},
                    "reason": "Resolve current node semantics.",
                },
                confidence=0.2,
            ),
            1,
        )


class ExactConfirmationFailureJudge(ExactFailureJudge):
    def __init__(self) -> None:
        super().__init__()
        self.confirmation_calls = 0

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="present",
                current_defect_reason="The answer is incomplete.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context[
                            "active_hypothesis_id"
                        ],
                        "candidate_ref": request.current_node.ref,
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Confirm the introduction candidate.",
                },
                confidence=0.9,
            ),
            1,
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        self.confirmation_calls += 1
        raise BoundedJudgeCallError(
            "exact confirmation failure", physical_requests=1
        )


class CrashAfterDurableAction(CheckpointBundle):
    def __init__(self, root, *, operation):
        super().__init__(root)
        self.operation = operation

    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == self.operation:
            raise KeyboardInterrupt("crash after durable action")
        return record


class ResettingSuccessJudge(BoundedJudgeCapability):
    def __init__(self, *, errors=2) -> None:
        self.step_calls = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""
        self.consecutive_provider_errors = errors
        self.provider_error_threshold = 3

    def judge_step_bounded(self, request, *, max_physical_requests):
        self.step_calls += 1
        self.consecutive_provider_errors = 0
        return BoundedJudgeCallResult(
            CausalStepJudgment(
                current_node_ref=request.current_node.ref,
                current_defect_status="absent",
                current_defect_reason="No defect.",
                predecessors=(),
                candidate_introduction=False,
                confidence=1.0,
            ),
            1,
        )

    def confirm_candidate_bounded(self, request, *, max_physical_requests):
        raise AssertionError("confirmation is not expected")


class CrashAfterFrontierCompleteSnapshot(CheckpointBundle):
    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        if kwargs["semantic_key"] == "analysis:frontier_complete":
            raise KeyboardInterrupt("crash immediately after the global snapshot")
        return commit


class InvestigationJudge(CountingOfflineJudge):
    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="unknown",
            current_defect_reason="More local evidence is required.",
            predecessors=(),
            candidate_introduction=False,
            missing_evidence=("inspect the current node",),
            suggested_investigation={
                "tool": "inspect_node",
                "arguments": {"ref": request.current_node.ref},
                "reason": "Resolve the current node semantics.",
            },
            confidence=0.2,
        )


class InterruptingConfirmationJudge(CountingOfflineJudge):
    def __init__(self, *, interrupt: bool) -> None:
        super().__init__()
        self.interrupt = interrupt

    def judge_step_offline(self, request):
        self.step_calls += 1
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason="The answer is semantically incomplete.",
            predecessors=(),
            candidate_introduction=True,
            suggested_investigation={
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                    "candidate_ref": request.current_node.ref,
                    "defect_fingerprint": request.defect_state.fingerprint,
                },
                "reason": "Independently confirm the introduction candidate.",
            },
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        if self.interrupt:
            raise KeyboardInterrupt("simulated interruption inside confirmation")
        raise AssertionError("an interrupted confirmation must not be repeated")


class UnknownConfirmationJudge(InterruptingConfirmationJudge):
    def __init__(self) -> None:
        super().__init__(interrupt=False)

    def confirm_candidate_offline(self, request):
        self.confirmation_calls += 1
        return RootConfirmation.unknown(
            request.candidate_ref, "The trace lacks candidate-local confirmation evidence."
        )


class StopDuringConfirmationJudge(UnknownConfirmationJudge):
    def __init__(self, stop_flag) -> None:
        super().__init__()
        self.stop_flag = stop_flag

    def confirm_candidate_offline(self, request):
        result = super().confirm_candidate_offline(request)
        self.stop_flag[0] = True
        return result


class InterruptingTools:
    artifact_bytes_used = 0
    max_artifact_bytes = 1_048_576

    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.calls = 0

    def for_graph(self, graph, *, max_artifact_bytes):
        self.max_artifact_bytes = max_artifact_bytes
        return self

    def execute(self, directive):
        self.calls += 1
        if self.interrupt:
            raise KeyboardInterrupt("simulated interruption inside investigation")
        raise AssertionError("an interrupted investigation must not be repeated")


class InjectedRestoreCheckpoint:
    def __init__(self, state, root: Path) -> None:
        self.state = state
        self.output_commit_path = root / "output-commit.json"

    def initialize(self, config) -> None:
        return None

    def restore(self, *, expected_config=None):
        return self.state


class CausalCheckpointTest(unittest.TestCase):
    def test_completed_replay_derives_proof_without_pre_authorizing_migration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle, config, report, lineage = completed_replay_bundle(Path(tempdir))

            replay = CheckpointBundle(bundle.root).restore_for_replay(
                expected_config=config,
                expected_lineage=lineage,
            )

            self.assertEqual(replay.state.final_report, report)
            proof = replay.replay_proof
            self.assertIsInstance(proof, CompletedCheckpointReplayProof)
            self.assertEqual(
                proof.schema_version,
                "completed-checkpoint-replay-proof/v1",
            )
            self.assertEqual(
                proof.config_fingerprint,
                config["config_fingerprint"],
            )
            self.assertEqual(
                {name for name, _, _, _ in proof.journal_heads},
                {"frontier", "hypotheses", "actions"},
            )
            self.assertEqual(
                proof.proof_identity,
                proof.recomputed_identity(),
            )
            self.assertFalse(hasattr(replay, "migration_decision"))

    def test_completed_replay_reuses_published_output_without_appending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle, config, report, lineage = completed_replay_bundle(
                Path(tempdir)
            )
            replay_bundle = CheckpointBundle(bundle.root)
            before_actions = bundle.actions_path.read_bytes()
            replay_bundle.restore_for_replay(
                expected_config=config,
                expected_lineage=lineage,
            )

            output = replay_bundle.completed_replay_output_commit(
                attribution_path=Path(tempdir) / "report.json",
                lineage_path=Path(tempdir) / "report.message-lineage.json",
                report=report,
                message_lineage=lineage,
            )

            self.assertEqual(output["status"], "published")
            self.assertEqual(
                output["attribution_hash"],
                hashlib.sha256(
                    (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(bundle.actions_path.read_bytes(), before_actions)

    def test_exact_legacy_shape_is_required_before_proof_can_derive_decision(self):
        with tempfile.TemporaryDirectory() as tempdir:
            report = exact_legacy_projection_report()
            bundle, config, _, lineage = completed_replay_bundle(
                Path(tempdir),
                report=report,
            )
            replay = CheckpointBundle(bundle.root).restore_for_replay(
                expected_config=config,
                expected_lineage=lineage,
            )

            classification = classify_legacy_projection_shape(
                report["investigation_journal"],
                report["metadata"],
            )
            self.assertIsInstance(classification, LegacyProjectionRequired)
            decision = replay.replay_proof.derive_migration_decision(
                classification
            )

            self.assertEqual(
                decision.classifier_identity,
                classification.classifier_identity,
            )
            self.assertEqual(
                decision.classifier_reason,
                classification.reason,
            )
            self.assertEqual(
                decision.to_dict()["authorization"][
                    "classifier_identity"
                ],
                classification.classifier_identity,
            )
            self.assertEqual(
                decision.decision_identity,
                decision.recomputed_identity(),
            )

    def test_forged_classifier_and_decision_fail_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            report = exact_legacy_projection_report()
            bundle, config, _, lineage = completed_replay_bundle(
                Path(tempdir),
                report=report,
            )
            replay = CheckpointBundle(bundle.root).restore_for_replay(
                expected_config=config,
                expected_lineage=lineage,
            )
            classification = classify_legacy_projection_shape(
                report["investigation_journal"],
                report["metadata"],
            )
            forged_classifier = replace(
                classification,
                classifier_identity="forged-classifier",
            )
            with self.assertRaises(CheckpointCorruptionError):
                replay.replay_proof.derive_migration_decision(
                    forged_classifier
                )

            decision = replay.replay_proof.derive_migration_decision(
                classification
            )
            forged_decision = replace(
                decision,
                classifier_reason="forged-reason",
            )
            with self.assertRaises(CheckpointCorruptionError):
                forged_decision.assert_authorizes(
                    report=report,
                    action_records=replay.state.actions,
                    classification=classification,
                )

    def test_near_legacy_shape_cannot_authorize_migration(self):
        report = exact_legacy_projection_report()
        report["investigation_journal"][0][
            "candidate_compression"
        ]["unexpected_field"] = "forged"

        classification = classify_legacy_projection_shape(
            report["investigation_journal"],
            report["metadata"],
        )

        self.assertIsInstance(
            classification,
            LegacyProjectionNotRequired,
        )
        self.assertEqual(
            classification.reason,
            "legacy_shape_not_exact",
        )

    def test_completed_replay_rejects_wrong_or_incomplete_config_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            bundle, config, _, lineage = completed_replay_bundle(root)
            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=sample_config(objective="Different objective."),
                    expected_lineage=lineage,
                )

            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            del manifest["config"]["cache_identity"]
            _write_hashed_json(bundle.manifest_path, manifest, "manifest_hash")
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=config,
                    expected_lineage=lineage,
                )

    def test_completed_replay_requires_every_journal_file(self):
        for journal_name in ("frontier", "hypotheses", "actions"):
            with self.subTest(journal=journal_name):
                with tempfile.TemporaryDirectory() as tempdir:
                    bundle, config, _, lineage = completed_replay_bundle(
                        Path(tempdir)
                    )
                    bundle._paths[journal_name].unlink()

                    with self.assertRaises(CheckpointCorruptionError):
                        CheckpointBundle(bundle.root).restore_for_replay(
                            expected_config=config,
                            expected_lineage=lineage,
                        )

    def test_completed_replay_rejects_missing_or_wrong_schema_output_and_lineage(self):
        mutations = ("missing_lineage", "wrong_output_schema", "wrong_lineage")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tempdir:
                    bundle, config, _, lineage = completed_replay_bundle(
                        Path(tempdir)
                    )
                    output = json.loads(
                        bundle.output_commit_path.read_text(encoding="utf-8")
                    )
                    lineage_path = Path(output["lineage_path"])
                    if mutation == "missing_lineage":
                        lineage_path.unlink()
                    elif mutation == "wrong_output_schema":
                        output["schema_version"] = "recursive-attribution-output/v0"
                        _write_hashed_json(
                            bundle.output_commit_path,
                            output,
                            "output_commit_hash",
                        )
                    else:
                        lineage_path.write_text("{}\n", encoding="utf-8")

                    with self.assertRaises(
                        (CheckpointCompatibilityError, CheckpointCorruptionError)
                    ):
                        CheckpointBundle(bundle.root).restore_for_replay(
                            expected_config=config,
                            expected_lineage=lineage,
                        )

    def test_completed_replay_rejects_a_self_consistent_resigned_lineage_forgery(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle, config, _, lineage = completed_replay_bundle(Path(tempdir))
            forged_lineage = {**lineage, "behavior_impact": "forged"}
            _resign_completed_lineage(bundle, forged_lineage)

            with self.assertRaisesRegex(
                CheckpointCorruptionError,
                "lineage.*current trace",
            ):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=config,
                    expected_lineage=lineage,
                )

    def test_completed_replay_rejects_incomplete_journal_and_nonterminal_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle, config, _, lineage = completed_replay_bundle(Path(tempdir))
            raw_actions = bundle.actions_path.read_bytes()
            bundle.actions_path.write_bytes(raw_actions[:-1])
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=config,
                    expected_lineage=lineage,
                )

        with tempfile.TemporaryDirectory() as tempdir:
            bundle, config, _, lineage = completed_replay_bundle(Path(tempdir))
            bundle.record_action(
                "audit_recorded",
                "audit:after-completion",
                {"status": "later"},
            )
            with self.assertRaisesRegex(
                CheckpointCorruptionError,
                "terminal",
            ):
                CheckpointBundle(bundle.root).restore_for_replay(
                    expected_config=config,
                    expected_lineage=lineage,
                )

    def test_v1_visit_key_migration_rewrites_all_occurrences_and_converges_with_v2(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "checkpoints"
            / "frontier-v1-pre-seed-binding.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        legacy_item = copy.deepcopy(fixture["frontier"]["queued"][0])
        defect_state = DefectState.from_dict(legacy_item["defect_state"])
        hypothesis = AttributionHypothesis.create(
            fixture["hypothesis"]["claim"],
            fixture["hypothesis"]["candidate_root_ref"],
            defect_state,
            seed_binding_identity=seed_binding_identity_for(
                "record:seed_one", defect_state.fingerprint
            ),
        )
        legacy_item["hypothesis_id"] = hypothesis.hypothesis_id
        legacy_item["hypothesis_semantic_hash"] = hypothesis.semantic_hash
        legacy_item["item_id"] = "frontier:{0}".format(
            hashlib.sha256(
                stable_json(
                    {
                        "node_ref": legacy_item["node_ref"],
                        "defect_fingerprint": defect_state.fingerprint,
                        "downstream_path": legacy_item["downstream_path"],
                        "hypothesis_id": hypothesis.hypothesis_id,
                        "hypothesis_semantic_hash": hypothesis.semantic_hash,
                        "depth": legacy_item["depth"],
                    }
                ).encode("utf-8")
            ).hexdigest()[:20]
        )
        legacy_item["visit_key"] = hashlib.sha256(
            stable_json(
                {
                    "node_ref": legacy_item["node_ref"],
                    "defect_fingerprint": defect_state.fingerprint,
                    "hypothesis_semantic_hash": hypothesis.semantic_hash,
                }
            ).encode("utf-8")
        ).hexdigest()
        item = FrontierItem.from_legacy_dict(
            legacy_item,
            seed_binding_identity=hypothesis.seed_binding_identity,
        )
        old_visit_key = legacy_item["visit_key"]
        new_visit_key = item.visit_key
        trace = {
            "case_id": "legacy-visit-migration",
            "records": [
                {
                    "record_id": "shared_anchor",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {"summary": "Shared expansion anchor."},
                },
                {
                    "record_id": "seed_one",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:shared_anchor"],
                    "data": {"actual": "The seed remains unresolved."},
                },
            ],
        }
        graph = TraceGraph.from_trace(trace)
        ledger = HypothesisLedger.from_snapshot([hypothesis.to_dict()])
        frontier = RecursiveFrontier()
        frontier.push(item)
        state = RecursiveAnalysisState(
            graph=graph,
            start_refs=("record:seed_one",),
            objective="Find the defect.",
            analysis_perspective="Improve repository reasoning.",
            ledger=ledger,
            frontier=frontier,
        )
        state.defect_states[item.defect_state.fingerprint] = item.defect_state
        state.transformation_chains[item.defect_state.fingerprint] = (item.defect_state,)
        seed = state._ensure_seed("record:seed_one", item.defect_state)
        state._bind_hypothesis_to_seed(hypothesis.hypothesis_id, seed)
        state.visit_evidence[new_visit_key] = {"record:shared_anchor"}
        context = {
            "active_visit_key": new_visit_key,
            "checked_evidence_refs": ["record:shared_anchor"],
        }
        context["evidence_hash"] = hashlib.sha256(
            stable_json(context).encode("utf-8")
        ).hexdigest()
        context_hash = hashlib.sha256(stable_json(context).encode("utf-8")).hexdigest()
        investigation_result = {
            "active_visit_key": new_visit_key,
            "journal_key": "investigation:{0}".format(new_visit_key),
        }
        state.investigation_journal = [
            {
                "active_visit": state._active_visit_snapshot(item),
                "result": copy.deepcopy(investigation_result),
                "context_before": copy.deepcopy(context),
                "context_before_hash": context_hash,
                "context_after": copy.deepcopy(context),
                "context_after_hash": context_hash,
                "rejudge_linkage": {
                    "source_visit_key": new_visit_key,
                    "source_context_hash": context_hash,
                    "rejudge_visit_key": new_visit_key,
                    "context_after_hash": context_hash,
                },
            }
        ]
        state.investigation_evidence = {
            new_visit_key: [investigation_result]
        }
        state.investigation_evidence_hashes = {new_visit_key: {"evidence:one"}}
        state.pending_rejudge_journal = {new_visit_key: [0]}
        confirmation_owner = LocalStateOwner.create(
            seed_binding_identity=seed.key,
            hypothesis_id=hypothesis.hypothesis_id,
            visit_key=new_visit_key,
            occurrence_key="confirmation_queue",
        )
        state.confirmation_journal = [
            {
                "nested": {"active_visit_key": new_visit_key},
                "hypothesis_id": hypothesis.hypothesis_id,
                "seed_binding_identity": seed.key,
                "owner": confirmation_owner.to_dict(),
            }
        ]
        state.provider_state = _provider_state_payload(
            CountingOfflineJudge(), state, cache_identity="cache:test"
        )

        native_frontier = state.frontier_checkpoint_payload()
        native_hypotheses = state.hypothesis_checkpoint_payload()
        native_action = state.action_checkpoint_payload()

        def replace_visit_key(value, source, target):
            if isinstance(value, dict):
                return {
                    str(key).replace(source, target): replace_visit_key(
                        child, source, target
                    )
                    for key, child in value.items()
                }
            if isinstance(value, list):
                return [replace_visit_key(child, source, target) for child in value]
            if isinstance(value, str):
                return value.replace(source, target)
            return copy.deepcopy(value)

        legacy_action = replace_visit_key(native_action, new_visit_key, old_visit_key)
        legacy_context = legacy_action["investigation_journal"][0]
        for context_key, hash_key in (
            ("context_before", "context_before_hash"),
            ("context_after", "context_after_hash"),
        ):
            migrated_context = legacy_context[context_key]
            semantic_context = dict(migrated_context)
            semantic_context.pop("evidence_hash")
            migrated_context["evidence_hash"] = hashlib.sha256(
                stable_json(semantic_context).encode("utf-8")
            ).hexdigest()
            legacy_context[hash_key] = hashlib.sha256(
                stable_json(migrated_context).encode("utf-8")
            ).hexdigest()
        legacy_context["rejudge_linkage"]["source_context_hash"] = legacy_context[
            "context_before_hash"
        ]
        legacy_context["rejudge_linkage"]["context_after_hash"] = legacy_context[
            "context_after_hash"
        ]
        legacy_frontier = {
            "schema": "recursive-analysis-frontier/v1",
            "frontier": {
                **copy.deepcopy(fixture["frontier"]),
                "queued": [copy.deepcopy(legacy_item)],
            },
            "visit_evidence": {old_visit_key: ["record:shared_anchor"]},
        }

        def checkpoint_record(
            journal,
            sequence,
            transaction_sequence,
            operation,
            semantic_key,
            payload,
            previous_hash="",
        ):
            unsigned = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "run_id": "migration-test",
                "journal": journal,
                "sequence": sequence,
                "transaction_sequence": transaction_sequence,
                "timestamp": "2026-07-22T00:00:0{0}Z".format(sequence),
                "operation": operation,
                "semantic_key": semantic_key,
                "payload": copy.deepcopy(payload),
                "previous_hash": previous_hash,
            }
            return {**unsigned, "record_hash": _sha256(unsigned)}

        def checkpoint_state(frontier_payload, action_payload, semantic_key, visit_key):
            frontier_record = checkpoint_record(
                "frontier", 1, 2, "snapshot", semantic_key, frontier_payload
            )
            hypothesis_record = checkpoint_record(
                "hypotheses", 1, 2, "snapshot", semantic_key, native_hypotheses
            )
            provider_record = checkpoint_record(
                "actions",
                1,
                1,
                "visit_migration_marker",
                "provider:{0}".format(visit_key),
                {"visit_key": visit_key, "status": "completed"},
            )
            snapshot_record = checkpoint_record(
                "actions",
                2,
                2,
                "state_snapshot",
                semantic_key,
                action_payload,
                provider_record["record_hash"],
            )
            return CheckpointState(
                config={"cache_identity": "cache:test"},
                run_id="migration-test",
                transaction_sequence=2,
                frontier_records=(frontier_record,),
                hypothesis_records=(hypothesis_record,),
                actions=(provider_record, snapshot_record),
            )

        native = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=checkpoint_state(
                native_frontier,
                native_action,
                "state:{0}".format(new_visit_key),
                new_visit_key,
            ),
        )
        migrated = RecursiveAnalysisState.from_checkpoint(
            graph=graph,
            checkpoint=checkpoint_state(
                legacy_frontier,
                legacy_action,
                "state:{0}".format(old_visit_key),
                old_visit_key,
            ),
        )

        self.assertEqual(
            migrated.frontier_checkpoint_payload(), native.frontier_checkpoint_payload()
        )
        self.assertEqual(
            migrated.action_checkpoint_payload(), native.action_checkpoint_payload()
        )
        self.assertEqual(migrated.replay_actions, native.replay_actions)
        for replay_actions in (migrated.replay_actions, native.replay_actions):
            previous_hash = ""
            for record in sorted(
                replay_actions.values(), key=lambda item: item["sequence"]
            ):
                self.assertEqual(record["previous_hash"], previous_hash)
                self.assertEqual(
                    record["record_hash"],
                    _sha256(
                        {
                            key: value
                            for key, value in record.items()
                            if key != "record_hash"
                        }
                    ),
                )
                previous_hash = record["record_hash"]
        migrated_output = stable_json(
            {
                "frontier": migrated.frontier_checkpoint_payload(),
                "actions": migrated.action_checkpoint_payload(),
                "replay": migrated.replay_actions,
            }
        )
        self.assertNotIn(old_visit_key, migrated_output)

    def test_checkpoint_config_fingerprints_graph_evidence_eligibility_policy(self):
        config = sample_config()

        self.assertEqual(
            config["evidence_eligibility_policy"],
            "graph-external-evidence-eligibility/v5",
        )
        semantic = {
            key: value for key, value in config.items() if key != "config_fingerprint"
        }
        self.assertEqual(config["config_fingerprint"], _sha256(semantic))

    def test_restore_rejects_checkpoint_from_before_evidence_eligibility_policy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            manifest["config"].pop("evidence_eligibility_policy", None)
            manifest["config"]["schema_version"] = "recursive-attribution-checkpoint/v2"
            semantic = {
                key: value
                for key, value in manifest["config"].items()
                if key != "config_fingerprint"
            }
            manifest["config"]["config_fingerprint"] = _sha256(semantic)
            unsigned = {
                key: value for key, value in manifest.items() if key != "manifest_hash"
            }
            manifest["manifest_hash"] = _sha256(unsigned)
            bundle.manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )

            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_older_evidence_eligibility_policy_identity(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
            manifest["config"]["evidence_eligibility_policy"] = (
                "graph-external-evidence-eligibility/v0"
            )
            semantic = {
                key: value
                for key, value in manifest["config"].items()
                if key != "config_fingerprint"
            }
            manifest["config"]["config_fingerprint"] = _sha256(semantic)
            unsigned = {
                key: value for key, value in manifest.items() if key != "manifest_hash"
            }
            manifest["manifest_hash"] = _sha256(unsigned)
            bundle.manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
            )

            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_recursive_restore_rejects_audit_only_refs_across_all_state_surfaces(self):
        with tempfile.TemporaryDirectory() as tempdir:
            trace = trace_with_audit_only_external()
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config(trace=trace)
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            for surface in (
                "hypothesis",
                "visit",
                "investigation",
                "judgment",
            ):
                with self.subTest(surface=surface):
                    frontier_records = json.loads(json.dumps(restored.frontier_records))
                    hypothesis_records = json.loads(json.dumps(restored.hypothesis_records))
                    actions = json.loads(json.dumps(restored.actions))
                    action_snapshot = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "state_snapshot"
                    )
                    if surface == "hypothesis":
                        hypothesis_records[-1]["payload"]["hypotheses"][0][
                            "supporting_evidence"
                        ].append(
                            {
                                "ref": "record:forged_external",
                                "reason": "Restored audit-only evidence.",
                                "confidence": 1.0,
                            }
                        )
                    elif surface == "visit":
                        frontier_records[-1]["payload"]["visit_evidence"][
                            "restored-visit"
                        ] = ["record:forged_external"]
                    elif surface == "investigation":
                        action_snapshot["payload"]["investigation_evidence"] = {
                            "restored-visit": [
                                {"resolved_refs": ["record:forged_external"]}
                            ]
                        }
                    else:
                        action_snapshot["payload"]["step_judgments"].append(
                            CausalStepJudgment(
                                current_node_ref="record:only",
                                current_defect_status="unknown",
                                current_defect_reason="Restored judgment.",
                                predecessors=(),
                                missing_evidence=("record:forged_external",),
                                confidence=0.0,
                            ).to_dict()
                        )

                    with self.assertRaisesRegex(ValueError, "evidence eligibility"):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(trace),
                            checkpoint=replace(
                                restored,
                                frontier_records=tuple(frontier_records),
                                hypothesis_records=tuple(hypothesis_records),
                                actions=tuple(actions),
                            ),
                        )

    def test_completed_and_pending_reports_reject_restored_audit_only_refs(self):
        with tempfile.TemporaryDirectory() as tempdir:
            trace = trace_with_audit_only_external()
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config(trace=trace)
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            for report_state in ("completed", "pending"):
                with self.subTest(report_state=report_state):
                    actions = json.loads(json.dumps(restored.actions))
                    report_action = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "analysis_ready"
                    )
                    if report_state == "completed":
                        report_action["operation"] = "analysis_completed"
                    report_action["payload"]["report"]["metadata"][
                        "restored_evidence_refs"
                    ] = ["record:forged_external"]
                    injected = InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    )

                    with self.assertRaisesRegex(ValueError, "evidence eligibility"):
                        AgenticRecursiveAnalyzer(
                            judge=CountingOfflineJudge(),
                            checkpoint=injected,
                            checkpoint_config=config,
                        ).analyze(
                            TraceGraph.from_trace(trace),
                            start_refs=["record:only"],
                            objective="Find the defect.",
                            analysis_perspective="Improve repository reasoning.",
                        )

    def test_checkpoint_report_restoration_rejects_schema_less_report(self):
        with tempfile.TemporaryDirectory() as tempdir:
            trace = sample_trace()
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config(trace=trace)
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            report_action["payload"]["report"].pop("schema_version")

            with self.assertRaisesRegex(ValueError, "schema_version"):
                AgenticRecursiveAnalyzer(
                    judge=CountingOfflineJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_checkpoint_report_restoration_rejects_unversioned_global_judgment(self):
        with tempfile.TemporaryDirectory() as tempdir:
            trace = sample_trace()
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config(trace=trace)
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            report_action["payload"]["report"]["seed_results"][0][
                "global_judgment"
            ] = {
                "outcome": "candidate_roots",
                "assessments": [{"candidate_ref": "record:only"}],
            }

            with self.assertRaisesRegex(ValueError, "current migration"):
                AgenticRecursiveAnalyzer(
                    judge=CountingOfflineJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_checkpoint_identity_includes_global_judgment_contract(self):
        config = sample_config()

        self.assertEqual(
            config["global_judgment_contract"],
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
        )
        self.assertEqual(
            config["global_judgment_contract"],
            "global-candidate-judgment/v11+validation-envelope/v11+capsule/v8"
            "+evidence-policy/v5+local-state-owner/v1"
            "+global-pass-identity/v1+failure-action/v4"
            "+failure-projection/v5+terminal-record-schema/v3"
            "+prompt-projection/v2+planning-diagnostics/v3"
            "+run-semantic-authority/v2+convergence-lineage/v1"
            "+final-comparison-preflight/v1"
            "+judge-lifecycle/v1+graph-seed-authority/v1"
            "+objective-authority/v1"
            "+candidate-set-closure/v1+comparison-matrix-closure/v1"
            "+bounded-evidence-expansion/v1",
        )
        self.assertEqual(
            config["root_confirmation_contract"],
            "recursive-root-confirmation/v17+resolution/v2+evidence-policy/v5"
            "+artifact-owner/v1+terminal-evidence/v2+local-state-owner/v1"
            "+action-projection/v8+response-identity/v1+counterfactual/v1"
            "+queue-response-identity/v1+published-root-projection/v3"
            "+causal-publication/v3"
            "+perspective-binding/v1"
            "+step-action-projection/v1+confirmation-request-identity/v3"
            "+confirmation-request-projection/v2",
        )
        self.assertEqual(
            config["factor_role_contract"],
            FACTOR_ROLE_CONTRACT_IDENTITY,
        )
        self.assertTrue(
            config["factor_role_contract"].startswith(
                "factor-role-contract/v1:sha256:"
            )
        )
        self.assertEqual(
            ACTION_STATE_SCHEMA,
            "recursive-analysis-actions/v26",
        )
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v29",
        )
        self.assertEqual(
            OUTPUT_SCHEMA_VERSION,
            "recursive-attribution-output/v16",
        )

    def test_legal_v27_checkpoint_migrates_runtime_and_provider_defaults(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            judge = CountingOfflineJudge()
            judge.provider_circuit_open = True
            judge.provider_circuit_reason = "historical provider failure"
            judge.consecutive_provider_errors = 3
            judge.provider_error_threshold = 3
            bundle = CheckpointBundle(root)
            AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=bundle,
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            _rewrite_bundle_as_legal_v27(bundle)

            restored = CheckpointBundle(root).restore(expected_config=config)
            state = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(sample_trace()),
                checkpoint=restored,
            )

        self.assertEqual(
            restored.config["schema_version"],
            "recursive-attribution-checkpoint/v29",
        )
        self.assertEqual(restored.config["runtime_identity"]["fusion_mode"], "off")
        self.assertEqual(state.provider_state["schema"], "recursive-provider-state/v3")
        self.assertFalse(state.provider_state["circuit"]["open"])
        self.assertEqual(state.provider_state["circuit"]["reason"], "")
        self.assertEqual(
            state.provider_state["circuit"]["consecutive_provider_errors"],
            0,
        )
        self.assertEqual(state.provider_state["circuit"]["disposition"], None)
        self.assertEqual(state.provider_state["circuit"]["first_request"], 0)
        self.assertEqual(state.provider_state["circuit"]["first_failure_at"], "")
        self.assertEqual(
            state.provider_state["previous_failure"]["reason"],
            "historical provider failure",
        )

    def test_typed_v2_provider_state_migrates_read_only_to_canonical_v3(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "typed-v2-provider.checkpoint"
            config = sample_config()
            judge = CountingOfflineJudge()
            judge.provider_circuit_open = True
            judge.provider_circuit_reason = "v2 provider unavailable"
            judge.consecutive_provider_errors = 2
            judge.provider_error_threshold = 7
            judge.provider_circuit_disposition = {
                "retryable": True,
                "category": "http_retryable",
                "status_code": 503,
                "error_code": "service_unavailable",
                "reason": "v2 provider unavailable",
            }
            judge.provider_circuit_first_request = 4
            judge.provider_circuit_first_failure_at = "2026-07-31T00:00:00Z"
            AgenticRecursiveAnalyzer(
                judge=judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            state = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(sample_trace()),
                checkpoint=restored,
            )

        v2 = copy.deepcopy(state.provider_state)
        v2["schema"] = "recursive-provider-state/v2"
        v2.pop("previous_failure")
        unsigned_v2 = {key: value for key, value in v2.items() if key != "identity"}
        v2["identity"] = hashlib.sha256(
            stable_json(unsigned_v2).encode("utf-8")
        ).hexdigest()
        original_v2 = copy.deepcopy(v2)

        migrated = _validate_provider_state(
            v2,
            state,
            cache_identity=config["cache_identity"],
        )

        self.assertEqual(v2, original_v2)
        self.assertEqual(migrated["schema"], "recursive-provider-state/v3")
        self.assertEqual(
            migrated["previous_failure"],
            {
                "open": True,
                "reason": "v2 provider unavailable",
                "consecutive_provider_errors": 2,
                "provider_error_threshold": 7,
                "disposition": {
                    "retryable": True,
                    "category": "http_retryable",
                    "status_code": 503,
                    "error_code": "service_unavailable",
                    "reason": "v2 provider unavailable",
                },
                "first_request": 4,
                "first_failure_at": "2026-07-31T00:00:00Z",
            },
        )
        self.assertEqual(
            migrated["circuit"],
            {
                "open": False,
                "reason": "",
                "consecutive_provider_errors": 0,
                "provider_error_threshold": 7,
                "disposition": None,
                "first_request": 0,
                "first_failure_at": "",
            },
        )
        self.assertEqual(migrated["accounting"], original_v2["accounting"])
        self.assertEqual(migrated["cache_identity"], original_v2["cache_identity"])
        self.assertEqual(migrated["cache_stats"], original_v2["cache_stats"])
        unsigned_v3 = {
            key: value for key, value in migrated.items() if key != "identity"
        }
        self.assertEqual(
            migrated["identity"],
            hashlib.sha256(stable_json(unsigned_v3).encode("utf-8")).hexdigest(),
        )

    def test_malformed_typed_v2_provider_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "malformed-v2-provider.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            state = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(sample_trace()),
                checkpoint=CheckpointBundle(root).restore(expected_config=config),
            )

        malformed = copy.deepcopy(state.provider_state)
        malformed["schema"] = "recursive-provider-state/v2"
        malformed.pop("previous_failure")
        malformed["circuit"].pop("first_failure_at")
        unsigned_v2 = {
            key: value for key, value in malformed.items() if key != "identity"
        }
        malformed["identity"] = hashlib.sha256(
            stable_json(unsigned_v2).encode("utf-8")
        ).hexdigest()
        original_malformed = copy.deepcopy(malformed)

        with self.assertRaisesRegex(ValueError, "provider circuit"):
            _validate_provider_state(
                malformed,
                state,
                cache_identity=config["cache_identity"],
            )

        self.assertEqual(malformed, original_malformed)

    def test_v27_retrieval_global_checkpoint_uses_expected_mode_and_unifies_carriers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-global-v27.checkpoint"
            runtime = dict(sample_config()["runtime_identity"])
            runtime["fusion_mode"] = "retrieval-global"
            config = sample_config(runtime_identity=runtime)
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="global:evidence",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={
                    "investigation_journal": [
                        {
                            "kind": "global_candidate_page",
                            "status": "failed",
                        }
                    ]
                },
            )
            _rewrite_bundle_as_legal_v27(bundle)

            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(root).restore(expected_config=sample_config())
            restored = CheckpointBundle(root).restore(expected_config=config)

            active = _visible_checkpoint_root(root)
            manifest = json.loads(
                (active / "manifest.json").read_text(encoding="utf-8")
            )
            commit = json.loads(
                (active / "commit.json").read_text(encoding="utf-8")
            )
            journal_records = [
                json.loads(line)
                for path in (
                    active / "frontier.jsonl",
                    active / "hypotheses.jsonl",
                    active / "investigation-actions.jsonl",
                )
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            restored.config["runtime_identity"]["fusion_mode"],
            "retrieval-global",
        )
        self.assertEqual(manifest["schema_version"], CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(commit["schema_version"], CHECKPOINT_SCHEMA_VERSION)
        self.assertTrue(journal_records)
        self.assertTrue(
            all(
                record["schema_version"] == CHECKPOINT_SCHEMA_VERSION
                for record in journal_records
            )
        )
        self.assertTrue(
            all(
                record["schema_version"] == CHECKPOINT_SCHEMA_VERSION
                for record in (
                    *restored.frontier_records,
                    *restored.hypothesis_records,
                    *restored.actions,
                )
            )
        )

    def test_v27_migration_recovers_from_every_generation_transaction_fault(self):
        phases = (
            "migration_staging_created",
            "migration_staged_frontier",
            "migration_staged_hypotheses",
            "migration_staged_actions",
            "migration_staged_commit",
            "migration_staged_manifest",
            "migration_staging_durable",
            "migration_staging_validated",
            "migration_transaction_published",
            "migration_generation_published",
            "migration_generation_directory_durable",
            "migration_current_prepared",
            "migration_current_replaced",
            "migration_current_directory_durable",
            "migration_active_validated",
            "migration_transaction_removed",
            "migration_cleanup_complete",
        )

        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "legacy-v27.checkpoint"
                config = sample_config()
                bundle = CheckpointBundle(root)
                bundle.initialize(config)
                bundle.commit_snapshot(
                    semantic_key="migration:fault-fixture",
                    frontier_payload={"snapshot": "frontier"},
                    hypothesis_payload={"snapshot": "hypotheses"},
                    action_payload={"snapshot": "actions"},
                )
                expected_state = bundle.restore(expected_config=config)
                _rewrite_bundle_as_legal_v27(bundle)
                carrier_paths = (
                    bundle.frontier_path,
                    bundle.hypotheses_path,
                    bundle.actions_path,
                    bundle.commit_path,
                    bundle.manifest_path,
                )
                original_v27 = {
                    path.name: path.read_bytes() for path in carrier_paths
                }
                observed = []

                def interrupt(stage):
                    observed.append(stage)
                    if stage == phase:
                        raise KeyboardInterrupt("migration fault at {0}".format(stage))

                with self.assertRaisesRegex(KeyboardInterrupt, phase):
                    CheckpointBundle(root, fault_hook=interrupt).restore(
                        expected_config=config
                    )

                self.assertIn(phase, observed)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in carrier_paths
                    },
                    original_v27,
                )
                self.assertEqual(len(_checkpoint_carrier_schemas(root)), 1)

                restored = CheckpointBundle(root).restore(expected_config=config)
                self.assertEqual(restored, expected_state)
                active = _visible_checkpoint_root(root)
                manifest = json.loads(
                    (active / "manifest.json").read_text(encoding="utf-8")
                )
                commit = json.loads(
                    (active / "commit.json").read_text(encoding="utf-8")
                )
                journal_records = [
                    json.loads(line)
                    for path in (
                        active / "frontier.jsonl",
                        active / "hypotheses.jsonl",
                        active / "investigation-actions.jsonl",
                    )
                    for line in path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(manifest["schema_version"], CHECKPOINT_SCHEMA_VERSION)
                self.assertEqual(commit["schema_version"], CHECKPOINT_SCHEMA_VERSION)
                self.assertTrue(
                    all(
                        record["schema_version"] == CHECKPOINT_SCHEMA_VERSION
                        for record in journal_records
                    )
                )
                self.assertTrue((root / "CURRENT").is_file())
                self.assertEqual(
                    list((root / ".migration-transactions").glob("*.json")),
                    [],
                )
                orphan_staging = list(
                    (root / "generations").glob(".migration-v28-*")
                )
                self.assertEqual(
                    len(orphan_staging),
                    1 if phase in frozenset(phases[:8]) else 0,
                )

                before_initialize = {
                    path.name: path.read_bytes()
                    for path in active.iterdir()
                    if path.is_file()
                }
                reopened = CheckpointBundle(root)
                reopened.initialize(config)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in active.iterdir()
                        if path.is_file()
                    },
                    before_initialize,
                )
                self.assertEqual(
                    CheckpointBundle(root).restore(expected_config=config),
                    expected_state,
                )

    def test_v27_migration_never_exposes_mixed_active_carrier_schemas(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:mixed-schema-fixture",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)

            paused = threading.Event()
            release = threading.Event()
            worker_errors = []

            def pause_after_generation_write(stage):
                if stage not in {
                    "migration_after_replace_frontier",
                    "migration_generation_directory_durable",
                } or paused.is_set():
                    return
                paused.set()
                if not release.wait(5):
                    raise AssertionError("migration publication test timed out")

            def migrate():
                try:
                    CheckpointBundle(
                        root,
                        fault_hook=pause_after_generation_write,
                    ).restore(expected_config=config)
                except BaseException as exc:
                    worker_errors.append(exc)

            worker = threading.Thread(target=migrate)
            worker.start()
            self.assertTrue(paused.wait(5), "migration did not reach publication gate")
            try:
                schemas = _checkpoint_carrier_schemas(root)
                self.assertEqual(len(schemas), 1, schemas)
            finally:
                release.set()
                worker.join(5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(worker_errors, [])

    def test_concurrent_v27_restores_survive_stale_transaction_snapshot(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:concurrent-fixture",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)

            def stop_after_transaction_publication(stage):
                if stage == "migration_marker_published":
                    raise KeyboardInterrupt("leave a recoverable transaction")

            with self.assertRaisesRegex(
                KeyboardInterrupt,
                "leave a recoverable transaction",
            ):
                CheckpointBundle(
                    root,
                    fault_hook=stop_after_transaction_publication,
                ).restore(expected_config=config)

            restore_b = CheckpointBundle(root)
            validate_generation = restore_b._validate_migration_generation
            transaction_read = threading.Event()
            resume_b = threading.Event()
            states = []
            worker_errors = []

            def validate_after_transaction_read(
                *,
                marker,
                expected_config,
                generation_path=None,
            ):
                staging = root / marker["staging_directory"]
                if generation_path == staging and not transaction_read.is_set():
                    transaction_read.set()
                    if not resume_b.wait(5):
                        raise AssertionError("concurrent restore test timed out")
                return validate_generation(
                    marker=marker,
                    expected_config=expected_config,
                    generation_path=generation_path,
                )

            restore_b._validate_migration_generation = (
                validate_after_transaction_read
            )

            def run_restore_b():
                try:
                    states.append(restore_b.restore(expected_config=config))
                except BaseException as exc:
                    worker_errors.append(exc)

            worker = threading.Thread(target=run_restore_b)
            worker.start()
            self.assertTrue(
                transaction_read.wait(5),
                "restore B did not read the migration transaction",
            )
            state_a = []
            worker_a_errors = []

            def run_restore_a():
                try:
                    state_a.append(
                        CheckpointBundle(root).restore(expected_config=config)
                    )
                except BaseException as exc:
                    worker_a_errors.append(exc)

            worker_a = threading.Thread(target=run_restore_a)
            worker_a.start()
            worker_a.join(0.1)
            try:
                self.assertTrue(
                    worker_a.is_alive(),
                    "second restorer did not wait for the migration lock",
                )
            finally:
                resume_b.set()
                worker.join(5)
                worker_a.join(5)

            self.assertFalse(worker.is_alive())
            self.assertFalse(worker_a.is_alive())
            self.assertEqual(worker_errors, [])
            self.assertEqual(worker_a_errors, [])
            self.assertEqual(states, state_a)
            active_generation = _visible_checkpoint_root(root)
            self.assertTrue(active_generation.is_dir())

    def test_resume_append_keeps_published_generation_immutable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:immutable-fixture",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)
            migrated = CheckpointBundle(root).restore(expected_config=config)
            published = _visible_checkpoint_root(root)

            resumed = CheckpointBundle(root)
            resumed.initialize(config)
            resumed.record_action(
                "resume_probe",
                "migration:immutable-resume",
                {"status": "recorded"},
            )

            replacement = _visible_checkpoint_root(root)
            restored = CheckpointBundle(root).restore(expected_config=config)

            self.assertNotEqual(replacement, published)
            self.assertFalse(published.exists())
            self.assertEqual(
                restored.transaction_sequence,
                migrated.transaction_sequence + 1,
            )
            self.assertEqual(restored.actions[-1]["operation"], "resume_probe")

    def test_stale_migration_cannot_overwrite_a_newer_process_append(self):
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:stale-source",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)

            migration_paused = context.Event()
            release_migration = context.Event()
            migration_result = context.Queue()
            migrator = context.Process(
                target=_migration_pause_worker,
                args=(
                    root,
                    config,
                    migration_paused,
                    release_migration,
                    migration_result,
                ),
            )
            migrator.start()
            self.assertTrue(
                migration_paused.wait(10),
                "stale migrator did not reach its publication gate",
            )

            writer_started = context.Event()
            writer_finished = context.Event()
            writer_result = context.Queue()
            writer = context.Process(
                target=_migration_append_worker,
                args=(
                    root,
                    config,
                    writer_started,
                    writer_finished,
                    writer_result,
                ),
            )
            writer.start()
            self.assertTrue(writer_started.wait(10))
            writer_finished_while_migration_paused = writer_finished.wait(1)
            release_migration.set()
            migrator.join(10)
            writer.join(10)

            self.assertFalse(migrator.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(migrator.exitcode, 0)
            self.assertEqual(writer.exitcode, 0)
            self.assertFalse(writer_finished_while_migration_paused)
            self.assertEqual(migration_result.get(timeout=5)[0], "ok")
            self.assertEqual(writer_result.get(timeout=5), ("ok", "migration-writer"))

            restored = CheckpointBundle(root).restore(expected_config=config)
            appended = [
                action
                for action in restored.actions
                if action["semantic_key"] == "concurrency:migration-writer"
            ]
            self.assertEqual(len(appended), 1)
            self.assertEqual(appended[0]["payload"], {"writer": "migration-writer"})

    def test_two_process_writers_append_exactly_once_in_root_and_generation_layouts(self):
        context = multiprocessing.get_context("fork")
        for layout in ("root-v28", "generation-v28"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "case.checkpoint"
                config = sample_config()
                bundle = CheckpointBundle(root)
                bundle.initialize(config)
                if layout == "generation-v28":
                    bundle.commit_snapshot(
                        semantic_key="migration:writer-source",
                        frontier_payload={"snapshot": "frontier"},
                        hypothesis_payload={"snapshot": "hypotheses"},
                        action_payload={"snapshot": "actions"},
                    )
                    _rewrite_bundle_as_legal_v27(bundle)
                    CheckpointBundle(root).restore(expected_config=config)

                ready = context.Queue()
                start = context.Event()
                result = context.Queue()
                finished = [context.Event(), context.Event()]
                writers = [
                    context.Process(
                        target=_barrier_append_worker,
                        args=(
                            root,
                            config,
                            writer,
                            ready,
                            start,
                            finished[index],
                            result,
                        ),
                    )
                    for index, writer in enumerate(("writer-a", "writer-b"))
                ]
                for process in writers:
                    process.start()
                self.assertEqual(
                    {ready.get(timeout=10), ready.get(timeout=10)},
                    {"writer-a", "writer-b"},
                )
                start.set()
                for event in finished:
                    self.assertTrue(event.wait(10))
                for process in writers:
                    process.join(10)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)

                results = [result.get(timeout=5), result.get(timeout=5)]
                self.assertEqual(
                    {entry[:2] for entry in results},
                    {("ok", "writer-a"), ("ok", "writer-b")},
                )
                restored = CheckpointBundle(root).restore(expected_config=config)
                appended = [
                    action
                    for action in restored.actions
                    if action["operation"] == "concurrent_append"
                ]
                self.assertCountEqual(
                    [action["payload"]["writer"] for action in appended],
                    ["writer-a", "writer-b"],
                )
                self.assertEqual(
                    len({action["record_hash"] for action in appended}),
                    2,
                )

    def test_shared_reader_blocks_publication_until_old_generation_can_be_reclaimed(self):
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:reader-source",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)
            CheckpointBundle(root).restore(expected_config=config)
            old_generation = _visible_checkpoint_root(root)

            reader_paused = context.Event()
            release_reader = context.Event()
            reader_result = context.Queue()
            reader = context.Process(
                target=_spanning_reader_worker,
                args=(root, config, reader_paused, release_reader, reader_result),
            )
            reader.start()
            self.assertTrue(reader_paused.wait(10))

            writer_started = context.Event()
            writer_finished = context.Event()
            writer_result = context.Queue()
            writer = context.Process(
                target=_migration_append_worker,
                args=(
                    root,
                    config,
                    writer_started,
                    writer_finished,
                    writer_result,
                ),
            )
            writer.start()
            self.assertTrue(writer_started.wait(10))
            writer_finished_while_reader_paused = writer_finished.wait(1)
            release_reader.set()
            reader.join(10)
            writer.join(10)

            self.assertFalse(reader.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(writer.exitcode, 0)
            self.assertFalse(writer_finished_while_reader_paused)
            self.assertEqual(reader_result.get(timeout=5)[0], "ok")
            self.assertEqual(writer_result.get(timeout=5)[0], "ok")
            self.assertFalse(old_generation.exists())

            restored = CheckpointBundle(root).restore(expected_config=config)
            self.assertEqual(
                sum(
                    action["semantic_key"] == "concurrency:migration-writer"
                    for action in restored.actions
                ),
                1,
            )

    def test_restore_rejects_symlink_substitution_for_every_active_carrier(self):
        carrier_names = (
            "manifest.json",
            "commit.json",
            "frontier.jsonl",
            "hypotheses.jsonl",
            "investigation-actions.jsonl",
        )
        for carrier_name in carrier_names:
            with self.subTest(carrier=carrier_name), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "legacy-v27.checkpoint"
                config = sample_config()
                bundle = CheckpointBundle(root)
                bundle.initialize(config)
                bundle.commit_snapshot(
                    semantic_key="migration:symlink-source",
                    frontier_payload={"snapshot": "frontier"},
                    hypothesis_payload={"snapshot": "hypotheses"},
                    action_payload={"snapshot": "actions"},
                )
                _rewrite_bundle_as_legal_v27(bundle)
                CheckpointBundle(root).restore(expected_config=config)
                active = _visible_checkpoint_root(root)
                carrier = active / carrier_name
                escaped = Path(tempdir) / "escaped-{0}".format(carrier_name)
                shutil.copyfile(carrier, escaped)
                carrier.unlink()
                carrier.symlink_to(escaped)

                with self.assertRaisesRegex(
                    CheckpointCorruptionError,
                    "regular|symlink|carrier|contained",
                ):
                    CheckpointBundle(root).restore(expected_config=config)

    def test_cleanup_rejects_an_inactive_generation_symlink_without_following_it(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:cleanup-source",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)
            CheckpointBundle(root).restore(expected_config=config)
            current_before = (root / "CURRENT").read_bytes()
            outside = Path(tempdir) / "outside-generation"
            outside.mkdir()
            sentinel = outside / "must-survive.txt"
            sentinel.write_text("outside checkpoint\n", encoding="utf-8")
            (root / "generations" / "v28-escaped").symlink_to(
                outside,
                target_is_directory=True,
            )

            with self.assertRaises(CheckpointCorruptionError):
                resumed = CheckpointBundle(root)
                resumed.initialize(config)
                resumed.record_action(
                    "unsafe_cleanup_probe",
                    "concurrency:unsafe-cleanup",
                    {"status": "must-not-commit"},
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside checkpoint\n")
            self.assertEqual((root / "CURRENT").read_bytes(), current_before)

    def test_lock_artifact_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            CheckpointBundle(root).initialize(config)
            outside = Path(tempdir) / "outside-lock"
            outside.write_text("not a checkpoint lock\n", encoding="utf-8")
            lock_path = root / ".checkpoint.lock"
            lock_path.unlink(missing_ok=True)
            lock_path.symlink_to(outside)

            with self.assertRaisesRegex(
                CheckpointCorruptionError,
                "lock|regular|symlink",
            ):
                CheckpointBundle(root).restore(expected_config=config)

    def test_carrier_identity_change_during_read_fails_closed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            CheckpointBundle(root).initialize(config)
            manifest = root / "manifest.json"
            manifest_inode = manifest.stat().st_ino
            manifest_bytes = manifest.read_bytes()
            real_read = os.read
            replaced = False

            def replace_manifest_after_read(descriptor, size):
                nonlocal replaced
                content = real_read(descriptor, size)
                if (
                    content
                    and not replaced
                    and os.fstat(descriptor).st_ino == manifest_inode
                ):
                    temporary = root / ".manifest-identity-swap"
                    temporary.write_bytes(manifest_bytes)
                    os.replace(temporary, manifest)
                    replaced = True
                return content

            with mock.patch(
                "trace_attribution.checkpoint.os.read",
                side_effect=replace_manifest_after_read,
            ):
                with self.assertRaisesRegex(
                    CheckpointCorruptionError,
                    "changed identity",
                ):
                    CheckpointBundle(root).restore(expected_config=config)
            self.assertTrue(replaced)

    def test_lock_acquisition_and_release_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            CheckpointBundle(root).initialize(config)

            with mock.patch(
                "trace_attribution.checkpoint.fcntl.flock",
                side_effect=OSError("acquisition failed"),
            ):
                with self.assertRaisesRegex(
                    CheckpointLockError,
                    "acquisition failed",
                ):
                    CheckpointBundle(root).restore(expected_config=config)

            real_flock = fcntl.flock

            def fail_release(descriptor, operation):
                if operation == fcntl.LOCK_UN:
                    raise OSError("release failed")
                return real_flock(descriptor, operation)

            with mock.patch(
                "trace_attribution.checkpoint.fcntl.flock",
                side_effect=fail_release,
            ):
                with self.assertRaisesRegex(
                    CheckpointLockError,
                    "release failed",
                ):
                    CheckpointBundle(root).restore(expected_config=config)

    def test_one_hundred_generation_appends_have_bounded_retention_and_disk(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "legacy-v27.checkpoint"
            config = sample_config()
            bundle = CheckpointBundle(root)
            bundle.initialize(config)
            bundle.commit_snapshot(
                semantic_key="migration:bounded-source",
                frontier_payload={"snapshot": "frontier"},
                hypothesis_payload={"snapshot": "hypotheses"},
                action_payload={"snapshot": "actions"},
            )
            _rewrite_bundle_as_legal_v27(bundle)
            CheckpointBundle(root).restore(expected_config=config)

            writer = CheckpointBundle(root)
            writer.initialize(config)
            for index in range(100):
                writer.record_action(
                    "bounded_append",
                    "concurrency:bounded:{0}".format(index),
                    {"index": index},
                )

            active = _visible_checkpoint_root(root)
            generations = [
                path
                for path in (root / "generations").iterdir()
                if path.name.startswith("v28-")
                and path.is_dir()
                and not path.is_symlink()
            ]
            active_bytes = sum(
                (active / name).stat().st_size
                for name in (
                    "manifest.json",
                    "commit.json",
                    "frontier.jsonl",
                    "hypotheses.jsonl",
                    "investigation-actions.jsonl",
                )
            )
            retained_bytes = _checkpoint_regular_file_bytes(root)
            restored = CheckpointBundle(root).restore(expected_config=config)

            self.assertEqual(
                [
                    action["payload"]["index"]
                    for action in restored.actions
                    if action["operation"] == "bounded_append"
                ],
                list(range(100)),
            )
            self.assertLessEqual(len(generations), 2)
            self.assertLessEqual(retained_bytes, active_bytes * 3)

    def test_v27_without_fusion_evidence_or_expected_mode_is_not_migratable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "ambiguous-v27.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            _rewrite_bundle_as_legal_v27(bundle)

            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(root).restore()

    def test_checkpoint_rejects_mixed_manifest_commit_and_terminal_schemas(self):
        for carrier in ("commit", "terminal"):
            with self.subTest(carrier=carrier), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "mixed.checkpoint"
                config = sample_config()
                bundle = CheckpointBundle(root)
                bundle.initialize(config)
                bundle.commit_snapshot(
                    semantic_key="mixed:source",
                    frontier_payload={"snapshot": "frontier"},
                    hypothesis_payload={"snapshot": "hypotheses"},
                    action_payload={"snapshot": "actions"},
                )
                _rewrite_bundle_as_legal_v27(bundle)
                commit = json.loads(
                    bundle.commit_path.read_text(encoding="utf-8")
                )
                if carrier == "commit":
                    commit["schema_version"] = CHECKPOINT_SCHEMA_VERSION
                else:
                    actions = [
                        json.loads(line)
                        for line in bundle.actions_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    actions[-1]["schema_version"] = CHECKPOINT_SCHEMA_VERSION
                    unsigned = {
                        key: value
                        for key, value in actions[-1].items()
                        if key != "record_hash"
                    }
                    actions[-1]["record_hash"] = _sha256(unsigned)
                    bundle.actions_path.write_text(
                        "".join(
                            json.dumps(record, sort_keys=True) + "\n"
                            for record in actions
                        ),
                        encoding="utf-8",
                    )
                    commit["journal_heads"]["actions"]["record_hash"] = (
                        actions[-1]["record_hash"]
                    )
                _write_hashed_json(bundle.commit_path, commit, "commit_hash")

                with self.assertRaises(CheckpointCorruptionError):
                    CheckpointBundle(root).restore(expected_config=config)

    def test_checkpoint_rejects_a_different_factor_role_truth_contract(self):
        config = sample_config()
        config["factor_role_contract"] = (
            "factor-role-contract/v1:sha256:forged"
        )
        semantic = {
            key: value
            for key, value in config.items()
            if key != "config_fingerprint"
        }
        config["config_fingerprint"] = _sha256(semantic)

        with self.assertRaisesRegex(
            CheckpointCompatibilityError,
            "factor role contract",
        ):
            validate_checkpoint_config(config)

    def test_completed_report_rejects_v4_global_judgment_with_unresolved_evidence(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
            fusion_mode="retrieval-global",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=InterruptingGlobalNoDefectJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective="Determine whether either observation is supported.",
                analysis_perspective="",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            seed = report_action["payload"]["report"]["seed_results"][0]
            seed["decisive_evidence_refs"] = ["record:ghost"]
            seed["decisive_evidence"] = [
                {
                    "ref": "record:ghost",
                    "owner": seed["global_judgment"]["owner"],
                }
            ]
            seed["global_judgment"]["decisive_evidence_refs"] = ["record:ghost"]
            seed["global_judgment"]["assessments"][0]["evidence_refs"] = [
                "record:ghost"
            ]

            with self.assertRaisesRegex(
                ValueError, "persisted global judgment.*semantic.*grounded refs"
            ):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

    def test_partial_checkpoint_rejects_semantically_invalid_v4_matrix(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
            fusion_mode="retrieval-global",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "partial-global.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(interrupt_on_call=2),
                    fusion_mode="retrieval-global",
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            snapshot = next(
                item
                for item in reversed(actions)
                if item["operation"] == "state_snapshot"
                and any(
                    seed.get("global_judgment")
                    for seed in item["payload"].get("seed_ledger", [])
                )
            )
            judgment = next(
                seed["global_judgment"]
                for seed in snapshot["payload"]["seed_ledger"]
                if seed.get("global_judgment")
            )
            judgment["assessments"][0]["defect_status"] = "malformed"

            with self.assertRaisesRegex(
                ValueError, "persisted global judgment.*semantic"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(restored, actions=tuple(actions)),
                )

    def test_completed_checkpoint_rejects_incomplete_v4_competitor_coverage(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
            fusion_mode="retrieval-global",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "completed-global.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=InterruptingGlobalNoDefectJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective="Determine whether either observation is supported.",
                analysis_perspective="",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            judgment = report_action["payload"]["report"]["seed_results"][0][
                "global_judgment"
            ]
            judgment["assessments"][0]["compared_candidate_refs"] = []

            with self.assertRaisesRegex(
                ValueError, "persisted global judgment.*semantic"
            ):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

    def test_partial_and_completed_restore_reject_stale_capsule_prompt_collections(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
            fusion_mode="retrieval-global",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            partial_root = Path(tempdir) / "partial-stale-capsule.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(interrupt_on_call=2),
                    fusion_mode="retrieval-global",
                    checkpoint=CheckpointBundle(partial_root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )
            partial = CheckpointBundle(partial_root).restore(expected_config=config)
            partial_actions = json.loads(json.dumps(partial.actions))
            snapshot = next(
                item
                for item in reversed(partial_actions)
                if item["operation"] == "state_snapshot"
                and any(
                    seed.get("global_judgment")
                    for seed in item["payload"].get("seed_ledger", [])
                )
            )
            partial_judgment = next(
                seed["global_judgment"]
                for seed in snapshot["payload"]["seed_ledger"]
                if seed.get("global_judgment")
            )
            partial_judgment["validation_envelope"][
                "candidate_evidence_capsules"
            ][0]["action_group"]["members"][0]["ref"] = "record:defect_two"

            with self.assertRaisesRegex(ValueError, "validation source|active"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(partial, actions=tuple(partial_actions)),
                )

            route_actions = json.loads(json.dumps(partial.actions))
            route_snapshot = next(
                item
                for item in reversed(route_actions)
                if item["operation"] == "state_snapshot"
                and any(
                    seed.get("global_judgment")
                    for seed in item["payload"].get("seed_ledger", [])
                )
            )
            route_judgment = next(
                seed["global_judgment"]
                for seed in route_snapshot["payload"]["seed_ledger"]
                if seed.get("global_judgment")
            )
            route_capsule = route_judgment["validation_envelope"][
                "candidate_evidence_capsules"
            ][0]
            route_capsule["candidate"]["source"] = "semantic_fallback"
            route_capsule["validation_source"][
                "candidate_source"
            ] = "semantic_fallback"
            with self.assertRaisesRegex(
                ValueError,
                "authoritative retrieval route|completed pass",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(partial, actions=tuple(route_actions)),
                )

            membership_actions = json.loads(json.dumps(partial.actions))
            membership_snapshot = next(
                item
                for item in reversed(membership_actions)
                if item["operation"] == "state_snapshot"
                and any(
                    seed.get("global_judgment")
                    for seed in item["payload"].get("seed_ledger", [])
                )
            )
            membership_judgment = next(
                seed["global_judgment"]
                for seed in membership_snapshot["payload"]["seed_ledger"]
                if seed.get("global_judgment")
            )
            membership_capsule = next(
                capsule
                for capsule in membership_judgment["validation_envelope"][
                    "candidate_evidence_capsules"
                ]
                if capsule["candidate"]["source"]
                in {"confirmed_edge", "attribution_edge"}
            )
            membership_source = membership_capsule["validation_source"]
            membership_source["candidate_evidence_refs"].append(
                membership_source["candidate_evidence_refs"][0]
            )
            with self.assertRaisesRegex(
                ValueError,
                "authoritative recorded route|completed pass",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(
                        partial, actions=tuple(membership_actions)
                    ),
                )

            completed_root = Path(tempdir) / "completed-stale-capsule.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=InterruptingGlobalNoDefectJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(completed_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective="Determine whether either observation is supported.",
                analysis_perspective="",
            )
            completed = CheckpointBundle(completed_root).restore(
                expected_config=config
            )
            completed_actions = json.loads(json.dumps(completed.actions))
            report_action = next(
                item
                for item in reversed(completed_actions)
                if item["operation"] == "analysis_ready"
            )
            completed_capsule = report_action["payload"]["report"][
                "seed_results"
            ][0]["global_judgment"]["validation_envelope"][
                "candidate_evidence_capsules"
            ][0]
            completed_capsule["artifact_hydration"][
                "missing_artifact_ids"
            ] = ["stale-artifact"]

            with self.assertRaisesRegex(ValueError, "validation source|active"):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(completed, actions=tuple(completed_actions)),
                        completed_root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

            route_completed_actions = json.loads(json.dumps(completed.actions))
            route_report_action = next(
                item
                for item in reversed(route_completed_actions)
                if item["operation"] == "analysis_ready"
            )
            route_capsule = route_report_action["payload"]["report"][
                "seed_results"
            ][0]["global_judgment"]["validation_envelope"][
                "candidate_evidence_capsules"
            ][0]
            route_capsule["candidate"]["source"] = "semantic_fallback"
            route_capsule["validation_source"][
                "candidate_source"
            ] = "semantic_fallback"
            with self.assertRaisesRegex(
                ValueError,
                "authoritative retrieval route|completed pass",
            ):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(completed, actions=tuple(route_completed_actions)),
                        completed_root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

            membership_completed_actions = json.loads(
                json.dumps(completed.actions)
            )
            membership_report_action = next(
                item
                for item in reversed(membership_completed_actions)
                if item["operation"] == "analysis_ready"
            )
            membership_capsule = next(
                capsule
                for capsule in membership_report_action["payload"]["report"][
                    "seed_results"
                ][0]["global_judgment"]["validation_envelope"][
                    "candidate_evidence_capsules"
                ]
                if capsule["candidate"]["source"]
                in {"confirmed_edge", "attribution_edge"}
            )
            membership_source = membership_capsule["validation_source"]
            membership_source["candidate_evidence_refs"].append(
                membership_source["candidate_evidence_refs"][0]
            )
            with self.assertRaisesRegex(
                ValueError,
                "authoritative recorded route|completed pass",
            ):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(
                            completed,
                            actions=tuple(membership_completed_actions),
                        ),
                        completed_root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

    def test_completed_checkpoint_rejects_unresolved_published_root_identity(self):
        trace = confirmed_root_trace()
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:defect"],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "completed-root.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=ConfirmedSingleNodeJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(
                [item.node_ref for item in report.confirmed_roots],
                ["record:decision"],
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            forge_checkpoint_root_identity(
                report_action["payload"]["report"], "record:ghost"
            )

            with self.assertRaisesRegex(
                ValueError,
                "publication identity.*record:ghost|canonical candidate node",
            ):
                AgenticRecursiveAnalyzer(
                    judge=ConfirmedSingleNodeJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_completed_checkpoint_rejects_disconnected_confirmation_path(self):
        trace = confirmed_root_trace()
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:defect"],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "completed-path.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=ConfirmedSingleNodeJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(actions)
                if item["operation"] == "analysis_ready"
            )
            report_action["operation"] = "analysis_completed"
            forge_checkpoint_root_path(
                report_action["payload"]["report"],
                ["record:decision", "record:decision", "record:defect"],
            )

            with self.assertRaisesRegex(
                ValueError, "confirmation path lacks a grounded causal edge"
            ):
                AgenticRecursiveAnalyzer(
                    judge=ConfirmedSingleNodeJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_partial_and_completed_restore_reject_stale_intermediate_root_path(self):
        trace = confirmed_root_trace()
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:defect"],
        )

        def forge_stale_path(value):
            replacements = {}

            def rewrite(item):
                if isinstance(item, dict):
                    if (
                        item.get("candidate_ref") == "record:decision"
                        and isinstance(item.get("recursive_path"), (list, tuple))
                    ):
                        old_identity = str(
                            item.get("confirmation_identity") or ""
                        )
                        item["recursive_path"] = [
                            "record:decision",
                            "record:stale_fact",
                            "record:defect",
                        ]
                        identity_fields = (
                            "hypothesis_id",
                            "hypothesis_semantic_hash",
                            "defect_fingerprint",
                            "seed_binding_identity",
                        )
                        if all(field in item for field in identity_fields):
                            new_identity = confirmation_identity_for(
                                hypothesis_id=item["hypothesis_id"],
                                hypothesis_semantic_hash=item[
                                    "hypothesis_semantic_hash"
                                ],
                                candidate_ref=item["candidate_ref"],
                                defect_fingerprint=item["defect_fingerprint"],
                                recursive_path=item["recursive_path"],
                                seed_binding_identity=item[
                                    "seed_binding_identity"
                                ],
                            )
                            item["confirmation_identity"] = new_identity
                            if old_identity:
                                replacements[old_identity] = new_identity
                    for child in item.values():
                        rewrite(child)
                elif isinstance(item, list):
                    for child in item:
                        rewrite(child)

            def replace_identities(item):
                if isinstance(item, dict):
                    for key, child in list(item.items()):
                        if isinstance(child, str) and child in replacements:
                            item[key] = replacements[child]
                        else:
                            replace_identities(child)
                elif isinstance(item, list):
                    for index, child in enumerate(item):
                        if isinstance(child, str) and child in replacements:
                            item[index] = replacements[child]
                        else:
                            replace_identities(child)

            rewrite(value)
            replace_identities(value)

        stale_trace = copy.deepcopy(trace)
        stale_trace["manifest"] = {
            "case_id": stale_trace["case_id"],
            "run_id": "stale-intermediate-root-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": stale_trace["case_id"],
                "run_id": "stale-intermediate-root-run",
            },
        }
        for record in stale_trace["records"]:
            record["data"].update(
                {
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                }
            )
        stale_trace["records"].insert(
            1,
            {
                "record_id": "stale_fact",
                "component": "processor",
                "event_type": "evidence.fact",
                "data": {
                    "text": "This fact belongs to an older subject revision.",
                    "subject_revision": "git:stale",
                    "revision_provenance_status": "valid",
                },
            },
        )
        stale_trace["dataflow_edges"] = [
            {
                "from": {"type": "record", "id": "decision"},
                "to": {"type": "record", "id": "stale_fact"},
                "relation": "decision_produced_fact",
                "evidence_type": "confirmed",
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "stale_fact"},
                "to": {"type": "record", "id": "defect"},
                "relation": "fact_exposed_by_evaluation",
                "evidence_type": "confirmed",
                "eligible_for_attribution": True,
            },
        ]

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "stale-intermediate-root.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=ConfirmedSingleNodeJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            actions = json.loads(json.dumps(restored.actions))
            forge_stale_path(actions)
            forged = replace(restored, actions=tuple(actions))

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(stale_trace),
                    checkpoint=forged,
                )

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                AgenticRecursiveAnalyzer(
                    judge=ConfirmedSingleNodeJudge(),
                    checkpoint=InjectedRestoreCheckpoint(forged, root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(stale_trace),
                    start_refs=["record:defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_partial_and_completed_restore_reject_stale_repository_generation_root_candidate(self):
        trace = confirmed_root_trace()
        trace["manifest"] = {
            "case_id": trace["case_id"],
            "run_id": "stale-revision-root-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": trace["case_id"],
                "run_id": "stale-revision-root-run",
            },
        }
        trace["records"][0]["data"]["repository_revision"] = 0
        trace["records"].append(
            {
                "record_id": "current_claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "temporal_scope": "current_revision",
                    "repository_revision": 0,
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                },
            }
        )
        for record in trace["records"]:
            record["data"].update(
                {
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                }
            )
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:defect"],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "stale-revision-root.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=ConfirmedSingleNodeJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)
            stale_trace = copy.deepcopy(trace)
            stale_trace["records"][-1]["data"]["repository_revision"] = 1

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(stale_trace),
                    checkpoint=restored,
                )

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                AgenticRecursiveAnalyzer(
                    judge=ConfirmedSingleNodeJudge(),
                    checkpoint=InjectedRestoreCheckpoint(restored, root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(stale_trace),
                    start_refs=["record:defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_partial_and_completed_restore_reject_stale_intermediate_factor_path(self):
        from tests.test_recursive_analyzer import (
            FusionScriptedJudge,
            observed_trace,
        )

        trace = observed_trace(branching=True)
        trace["manifest"] = {
            "case_id": trace["case_id"],
            "run_id": "stale-intermediate-factor-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": trace["case_id"],
                "run_id": "stale-intermediate-factor-run",
            },
        }
        for record in trace["records"]:
            record.setdefault("data", {}).update(
                {
                    "subject_revision": "git:active",
                    "revision_provenance_status": "valid",
                }
            )
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:observed_defect"],
            fusion_mode="retrieval-global",
        )

        def judge():
            return FusionScriptedJudge(
                global_outcome="candidate_roots",
                selected_candidate_refs=("record:decision",),
                global_non_root_roles={
                    "record:context": "contributing_condition",
                },
                factor_roles={
                    "record:context": "contributing_condition",
                },
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt=(
                            "Implement only the methods found in the "
                            "first search."
                        ),
                        reason=(
                            "The decision stopped repository discovery."
                        ),
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision",
                            "confirmed",
                        ),
                        confidence=0.91,
                        evidence_refs=["record:decision"],
                    )
                },
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "stale-intermediate-factor.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=judge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(len(report.contributing_conditions), 1)
            restored = CheckpointBundle(root).restore(expected_config=config)
            stale_trace = copy.deepcopy(trace)
            change = next(
                item
                for item in stale_trace["records"]
                if item["record_id"] == "change"
            )
            change["data"]["subject_revision"] = "git:stale"

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(stale_trace),
                    checkpoint=restored,
                )

            with self.assertRaisesRegex(
                ValueError, "active revision|evidence eligibility"
            ):
                AgenticRecursiveAnalyzer(
                    judge=judge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(restored, root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(stale_trace),
                    start_refs=["record:observed_defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_partial_and_completed_checkpoint_reject_disconnected_factor_path(self):
        from tests.test_recursive_analyzer import (
            FusionScriptedJudge,
            observed_trace,
        )

        trace = observed_trace(branching=True)
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:observed_defect"],
            fusion_mode="retrieval-global",
        )

        def judge():
            return FusionScriptedJudge(
                global_outcome="candidate_roots",
                selected_candidate_refs=("record:decision",),
                global_non_root_roles={
                    "record:context": "contributing_condition",
                },
                factor_roles={
                    "record:context": "contributing_condition",
                },
                confirmations={
                    "record:decision": RootConfirmation.confirmed(
                        "record:decision",
                        excerpt=(
                            "Implement only the methods found in the "
                            "first search."
                        ),
                        reason=(
                            "The decision stopped repository discovery."
                        ),
                        counterfactual=confirmation_counterfactual_for(
                            "record:decision",
                            "confirmed",
                        ),
                        confidence=0.91,
                        evidence_refs=["record:decision"],
                    )
                },
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "factor-path.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=judge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed_defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            partial_actions = json.loads(json.dumps(restored.actions))
            snapshot = next(
                item
                for item in reversed(partial_actions)
                if item["operation"] == "state_snapshot"
                and item["payload"]["factor_role_action_projection"]
            )
            forge_checkpoint_factor_path(
                snapshot["payload"],
                ["record:context", "record:decision", "record:observed_defect"],
            )
            with self.assertRaisesRegex(
                ValueError,
                "factor role|FactorRole|path|request",
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(restored, actions=tuple(partial_actions)),
                )

            completed_actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(completed_actions)
                if item["operation"] == "analysis_ready"
            )
            forge_checkpoint_factor_path(
                report_action["payload"]["report"],
                ["record:context", "record:decision", "record:observed_defect"],
            )
            with self.assertRaisesRegex(
                ValueError,
                "factor role|FactorRole|path|request",
            ):
                AgenticRecursiveAnalyzer(
                    judge=judge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(completed_actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed_defect"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_snapshot_commit_has_one_run_id_and_global_transaction_sequence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            commit = bundle.commit_snapshot(
                semantic_key="snapshot:a",
                frontier_payload={"frontier": ["a"]},
                hypothesis_payload={"hypotheses": ["a"]},
                action_payload={"state": "a"},
            )
            records = [
                json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
                for path in (
                    bundle.frontier_path,
                    bundle.hypotheses_path,
                    bundle.actions_path,
                )
            ]
            self.assertEqual({item["run_id"] for item in records}, {commit["run_id"]})
            self.assertEqual(
                {item["transaction_sequence"] for item in records},
                {commit["transaction_sequence"]},
            )
            self.assertEqual(
                commit["journal_heads"]["frontier"]["record_hash"],
                records[0]["record_hash"],
            )

    def test_snapshot_content_addresses_repeated_large_nested_values(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            shared = {
                "candidate_ref": "record:decisionnode_revision_bound",
                "rationale": "semantic evidence " * 12_000,
            }

            for sequence in (1, 2):
                bundle.commit_snapshot(
                    semantic_key="snapshot:{0}".format(sequence),
                    frontier_payload={"frontier": []},
                    hypothesis_payload={"hypotheses": []},
                    action_payload={"sequence": sequence, "shared": shared},
                )

            restored = CheckpointBundle(root).restore(
                expected_config=sample_config()
            )
            self.assertEqual(
                restored.actions[-1]["payload"],
                {"sequence": 2, "shared": shared},
            )
            blobs = list(
                (root / "checkpoint-blobs" / "sha256").glob("*.json")
            )
            self.assertEqual(len(blobs), 1)
            self.assertLess(bundle.actions_path.stat().st_size, 8_000)

    def test_restore_rejects_missing_content_addressed_checkpoint_blob(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.commit_snapshot(
                semantic_key="snapshot:missing",
                frontier_payload={"frontier": []},
                hypothesis_payload={"hypotheses": []},
                action_payload={"shared": {"text": "evidence " * 12_000}},
            )
            blob = next(
                (root / "checkpoint-blobs" / "sha256").glob("*.json")
            )
            blob.unlink()

            with self.assertRaisesRegex(
                CheckpointCorruptionError, "checkpoint blob is missing"
            ):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_modified_content_addressed_checkpoint_blob(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.commit_snapshot(
                semantic_key="snapshot:modified",
                frontier_payload={"frontier": []},
                hypothesis_payload={"hypotheses": []},
                action_payload={"shared": {"text": "evidence " * 12_000}},
            )
            blob = next(
                (root / "checkpoint-blobs" / "sha256").glob("*.json")
            )
            blob.write_text('{"forged":true}', encoding="utf-8")

            with self.assertRaisesRegex(
                CheckpointCorruptionError, "checkpoint blob hash mismatch"
            ):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_recursive_restore_rejects_state_members_from_different_transactions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=bundle,
                checkpoint_config=sample_config(),
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            stable = bundle.restore(expected_config=sample_config())
            bundle.record_frontier(
                "snapshot", "forged:mixed", stable.frontier_payload
            )
            mixed = bundle.restore(expected_config=sample_config())
            with self.assertRaises(ValueError):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(sample_trace()), checkpoint=mixed
                )

    def test_restore_rejects_cross_run_journal_mix(self):
        with tempfile.TemporaryDirectory() as tempdir:
            left = CheckpointBundle(Path(tempdir) / "left.checkpoint")
            right = CheckpointBundle(Path(tempdir) / "right.checkpoint")
            left.initialize(sample_config())
            right.initialize(sample_config())
            left.commit_snapshot(
                semantic_key="snapshot:left",
                frontier_payload={"run": "left"},
                hypothesis_payload={"run": "left"},
                action_payload={"run": "left"},
            )
            right.commit_snapshot(
                semantic_key="snapshot:right",
                frontier_payload={"run": "right"},
                hypothesis_payload={"run": "right"},
                action_payload={"run": "right"},
            )
            shutil.copyfile(right.hypotheses_path, left.hypotheses_path)
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(left.root).restore(expected_config=sample_config())

    def test_restore_rejects_deletion_of_a_committed_valid_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.record_action("completed", "call:a", {"status": "completed"})
            lines = bundle.actions_path.read_text(encoding="utf-8").splitlines()
            bundle.actions_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(bundle.root).restore(expected_config=sample_config())

    def test_parseable_record_without_newline_is_repaired_before_append(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.actions_path.write_bytes(bundle.actions_path.read_bytes().rstrip(b"\n"))

            reopened = CheckpointBundle(bundle.root)
            reopened.initialize(sample_config())
            reopened.record_action("completed", "call:a", {"status": "completed"})
            restored = CheckpointBundle(bundle.root).restore(expected_config=sample_config())
            self.assertEqual(len(restored.actions), 2)
            self.assertGreaterEqual(restored.tail_repair_count, 1)
            self.assertTrue(reopened.actions_path.read_bytes().endswith(b"\n"))

    def test_uncommitted_cross_journal_crash_tail_is_not_mixed_into_restore(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            stable = CheckpointBundle(root)
            stable.initialize(sample_config())
            stable.commit_snapshot(
                semantic_key="stable",
                frontier_payload={"version": "stable"},
                hypothesis_payload={"version": "stable"},
                action_payload={"version": "stable"},
            )

            def crash(stage):
                if stage == "snapshot_after_hypotheses":
                    raise KeyboardInterrupt("crash before action member and commit publish")

            crashing = CheckpointBundle(root, fault_hook=crash)
            crashing.initialize(sample_config())
            with self.assertRaises(KeyboardInterrupt):
                crashing.commit_snapshot(
                    semantic_key="unstable",
                    frontier_payload={"version": "unstable"},
                    hypothesis_payload={"version": "unstable"},
                    action_payload={"version": "unstable"},
                )

            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            restored = reopened.restore(expected_config=sample_config())
            self.assertEqual(restored.frontier_payload, {"version": "stable"})
            self.assertEqual(restored.hypothesis_payload, {"version": "stable"})
            self.assertEqual(restored.actions[-1]["payload"], {"version": "stable"})
            self.assertGreaterEqual(restored.tail_repair_count, 2)

    def test_initialize_creates_and_directory_fsyncs_all_journals(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            with mock.patch("trace_attribution.checkpoint.os.fsync") as fsync:
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
            self.assertTrue(bundle.frontier_path.is_file())
            self.assertTrue(bundle.hypotheses_path.is_file())
            self.assertTrue(bundle.actions_path.is_file())
            self.assertTrue(bundle.commit_path.is_file())
            self.assertGreaterEqual(fsync.call_count, 6)

    def test_directory_durability_boundary_is_reached_during_initialize(self):
        with tempfile.TemporaryDirectory() as tempdir:
            stages = []
            bundle = CheckpointBundle(
                Path(tempdir) / "case.checkpoint", fault_hook=stages.append
            )
            bundle.initialize(sample_config())
            self.assertIn("checkpoint_directory_durable", stages)

    def test_timeout_drift_is_checkpoint_incompatible(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            changed_runtime = dict(sample_config()["runtime_identity"])
            changed_runtime["judge_timeout_sec"] = 120.0
            with self.assertRaises(CheckpointCompatibilityError):
                CheckpointBundle(root).restore(
                    expected_config=sample_config(runtime_identity=changed_runtime)
                )

    def test_three_journals_are_fsynced_and_restore_after_corrupt_tail(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            with mock.patch("trace_attribution.checkpoint.os.fsync") as fsync:
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
                bundle.record_frontier("snapshot", "visit:a", {"frontier": ["a"]})
                bundle.record_hypothesis("snapshot", "hyp:a", {"hypotheses": ["a"]})
                bundle.record_action("investigation_completed", "action:a", {"status": "success"})
                self.assertGreaterEqual(fsync.call_count, 4)

            with bundle.frontier_path.open("a", encoding="utf-8") as handle:
                handle.write('{"truncated"')

            state = CheckpointBundle(root).restore(expected_config=sample_config())
            self.assertEqual(state.frontier_payload, {"frontier": ["a"]})
            self.assertEqual(state.hypothesis_payload, {"hypotheses": ["a"]})
            self.assertEqual(state.actions[-1]["payload"], {"status": "success"})
            self.assertEqual(state.corrupt_entries, 1)

            raw = json.loads(bundle.actions_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(
                set(raw),
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
                },
            )
            self.assertEqual(raw["schema_version"], CHECKPOINT_SCHEMA_VERSION)

    def test_reopen_repairs_only_a_truncated_tail_before_the_next_append(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')

            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            reopened.record_action("completed", "call:a", {"status": "completed"})
            state = CheckpointBundle(root).restore(expected_config=sample_config())
            self.assertEqual([item["sequence"] for item in state.actions], [1, 2])
            self.assertEqual(state.corrupt_entries, 0)

    def test_restore_rejects_interior_corruption_reorder_and_hash_break(self):
        mutations = ("interior", "reorder", "hash")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir) / "case.checkpoint"
                bundle = CheckpointBundle(root)
                bundle.initialize(sample_config())
                bundle.record_action("queued", "call:a", {"status": "queued"})
                bundle.record_action("completed", "call:a", {"status": "completed"})
                lines = bundle.actions_path.read_text(encoding="utf-8").splitlines()
                if mutation == "interior":
                    lines[0] = "{broken"
                elif mutation == "reorder":
                    lines.reverse()
                else:
                    record = json.loads(lines[0])
                    record["payload"] = {"status": "forged"}
                    lines[0] = json.dumps(record, sort_keys=True)
                bundle.actions_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaises(CheckpointCorruptionError):
                    CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_nonmonotonic_global_transaction_sequence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            bundle.record_action("queued", "call:a", {"status": "queued"})
            bundle.record_action("completed", "call:a", {"status": "completed"})

            records = [
                json.loads(line)
                for line in bundle.actions_path.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["transaction_sequence"] = records[0]["transaction_sequence"]
            records[1]["record_hash"] = _sha256(
                {key: value for key, value in records[1].items() if key != "record_hash"}
            )
            bundle.actions_path.write_text(
                "\n".join(json.dumps(item, sort_keys=True) for item in records) + "\n",
                encoding="utf-8",
            )
            commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
            commit["journal_heads"]["actions"]["record_hash"] = records[1]["record_hash"]
            commit["commit_hash"] = _sha256(
                {key: value for key, value in commit.items() if key != "commit_hash"}
            )
            bundle.commit_path.write_text(
                json.dumps(commit, sort_keys=True) + "\n", encoding="utf-8"
            )

            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(root).restore(expected_config=sample_config())

    def test_restore_rejects_stale_trace_or_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            changed_trace = sample_trace()
            changed_trace["records"][0]["data"]["content"] = "changed"
            for changed in (
                sample_config(trace=changed_trace),
                sample_config(objective="Different objective."),
                sample_config(model_identity="provider:different"),
                sample_config(cache_identity="cache:different"),
                sample_config(budgets={**sample_config()["budgets"], "max_depth": 21}),
            ):
                with self.subTest(config=changed):
                    with self.assertRaises(CheckpointCompatibilityError):
                        CheckpointBundle(root).restore(expected_config=changed)

    def test_completed_recursive_run_restores_exact_report_without_repeating_calls(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            config = sample_config()
            first_judge = CountingOfflineJudge()
            analyzer = AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=bundle,
                checkpoint_config=config,
            )
            first = analyzer.analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(first_judge.step_calls, 1)

            resumed_judge = CountingOfflineJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(bundle.root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertIsInstance(resumed, RecursiveAttributionReport)
            self.assertEqual(resumed.to_dict(), first.to_dict())
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)

    def test_inflight_provider_call_is_never_replayed_or_fabricated(self):
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "case.checkpoint")
            config = sample_config()
            bundle.initialize(config)
            bundle.record_frontier(
                "snapshot",
                "analysis",
                {"frontier": {"schema": "recursive-frontier-checkpoint", "version": 2, "queued": [], "in_flight": [], "completed": []}},
            )
            bundle.record_hypothesis("snapshot", "analysis", {"hypotheses": []})
            bundle.record_action(
                "provider_call_started",
                "step:visit:a",
                {"call_kind": "step", "status": "in_flight", "physical_requests_reserved": 1},
            )
            restored = bundle.restore(expected_config=config)
            self.assertEqual(restored.inflight_actions[0]["semantic_key"], "step:visit:a")
            self.assertFalse(restored.inflight_actions[0]["payload"].get("success", False))

    def test_resume_conservatively_closes_an_interrupted_judge_call_without_replay(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            interrupted_judge = InterruptingOfflineJudge()
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=interrupted_judge,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(interrupted_judge.step_calls, 1)

            resumed_judge = CountingOfflineJudge()
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertIn("record:only", report.unresolved_refs)
            self.assertTrue(
                any(
                    item.get("reason") == "interrupted_judge_call"
                    for item in report.metadata.get("unresolved_branches", [])
                )
            )

    def test_resume_quarantines_a_stale_start_without_invoking_judge(self):
        trace = {
            "case_id": "checkpoint-stale-start",
            "records": [
                {
                    "record_id": "claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "repository_revision": 0,
                        "is_final_for_case": True,
                        "claim": "Generation zero claim.",
                    },
                }
            ],
        }
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:claim"],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "stale-start.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingOfflineJudge(),
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:claim"],
                    objective="Assess the claim.",
                    analysis_perspective="Improve repository reasoning.",
                )
            checkpoint = CheckpointBundle(root).restore(expected_config=config)
            active_trace = copy.deepcopy(trace)
            active_trace["records"].append(
                {
                    "record_id": "active_claim",
                    "component": "result",
                    "event_type": "response.claim",
                    "data": {
                        "repository_revision": 1,
                        "is_final_for_case": True,
                        "claim": "Generation one claim.",
                    },
                }
            )
            graph = TraceGraph.from_trace(active_trace)

            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=checkpoint,
            )
            self.assertFalse(restored.frontier)
            result = restored.seed_results()[0]
            self.assertEqual(result.outcome, "evidence_gap")
            self.assertIn(
                "start_ref_active_revision_ineligible",
                result.blocking_reasons,
            )

            resumed_judge = CountingOfflineJudge()
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:claim"],
                objective="Assess the claim.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(report.seed_results[0].outcome, "evidence_gap")

    def test_completed_restore_removes_publication_owned_by_a_stale_start(self):
        trace = confirmed_root_trace()
        trace["records"][1]["data"]["repository_revision"] = 0
        trace["records"].append(
            {
                "record_id": "claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 0,
                    "is_final_for_case": True,
                    "claim": "Generation zero is authoritative.",
                },
            }
        )
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:defect"],
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "completed-stale-start.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=ConfirmedSingleNodeJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:defect"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(report.seed_results[0].outcome, "confirmed_root")
            checkpoint = CheckpointBundle(root).restore(expected_config=config)
            stale_trace = copy.deepcopy(trace)
            stale_trace["records"][1]["data"]["repository_revision"] = 1

            restored = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(stale_trace),
                checkpoint=checkpoint,
            )

            self.assertEqual(restored.seed_results()[0].outcome, "evidence_gap")
            self.assertEqual(restored.confirmations, [])
            self.assertEqual(restored.confirmed_roots, [])
            self.assertEqual(restored.co_roots, [])

    def test_inflight_bounded_call_conservatively_keeps_max_one_budget_debited(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 1
            config = sample_config(budgets=budgets)
            first = InterruptingBoundedJudge(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=first,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                    max_judge_requests=1,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first.allowances, [1])

            resumed_judge = InterruptingBoundedJudge(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                max_judge_requests=1,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(report.metadata["physical_judge_request_count"], 1)
            self.assertEqual(report.metadata["judge_request_uncertainty_count"], 1)
            self.assertGreaterEqual(report.metadata["exhausted_budgets"]["judge_requests"], 1)

    def test_exact_failed_step_replays_terminal_semantics_and_exact_debit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="provider_call_failed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

            resumed_judge = ExactFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 1)
            self.assertEqual(
                resumed.metadata["unresolved_branches"][0]["reason"], "judge_error"
            )

    def test_exact_failed_rejudge_replays_without_becoming_interrupted(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactRejudgeFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactRejudgeFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="provider_call_failed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            resumed_judge = ExactRejudgeFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 2)

    def test_exact_failed_confirmation_replays_exact_unknown_result(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ExactConfirmationFailureJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ExactConfirmationFailureJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterDurableAction(
                        root, operation="confirmation_completed"
                    ),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            resumed_judge = ExactConfirmationFailureJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())
            self.assertEqual(resumed.metadata["physical_judge_request_count"], 2)

    def test_resume_never_repeats_an_inflight_investigation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_tools = InterruptingTools(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InvestigationJudge(),
                    tools=first_tools,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first_tools.calls, 1)

            resumed_tools = InterruptingTools(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=InvestigationJudge(),
                tools=resumed_tools,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_tools.calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertTrue(
                any(
                    item.get("rejection_reason") == "interrupted_investigation_call"
                    for item in report.investigation_journal
                )
            )

    def test_resume_never_repeats_an_inflight_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = InterruptingConfirmationJudge(interrupt=True)
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            self.assertEqual(first_judge.step_calls, 1)
            self.assertEqual(first_judge.confirmation_calls, 1)
            started_reservation = next(
                item["payload"]["physical_requests_reserved"]
                for item in reversed(
                    CheckpointBundle(root).restore(
                        expected_config=config
                    ).actions
                )
                if item["operation"] == "confirmation_started"
            )

            resumed_judge = InterruptingConfirmationJudge(interrupt=False)
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertTrue(
                any(
                    item.reason.startswith("confirmation_interrupted")
                    for item in report.confirmations
                )
            )
            projection = report.metadata["confirmation_action_projection"]
            self.assertEqual(len(projection), 1)
            self.assertEqual(projection[0]["operation"], "confirmation_failed")
            self.assertFalse(projection[0]["physical_request_exact"])
            self.assertEqual(
                projection[0]["physical_requests_reserved"],
                started_reservation,
            )

    def test_resume_preserves_provider_failure_history_but_allows_one_probe(self):
        class FreshProbeJudge(CountingOfflineJudge):
            def __init__(self):
                super().__init__()
                self.active_state_at_probe = None

            def judge_step_offline(self, request):
                self.active_state_at_probe = {
                    "open": self.provider_circuit_open,
                    "reason": self.provider_circuit_reason,
                    "errors": self.consecutive_provider_errors,
                    "disposition": self.provider_circuit_disposition,
                    "first_request": self.provider_circuit_first_request,
                    "first_failure_at": self.provider_circuit_first_failure_at,
                }
                return super().judge_step_offline(request)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = CountingOfflineJudge()
            first_judge.provider_circuit_open = True
            first_judge.provider_circuit_reason = "provider unavailable"
            first_judge.consecutive_provider_errors = 3
            first_judge.provider_error_threshold = 3
            first_judge.provider_circuit_disposition = {
                "retryable": True,
                "category": "http_retryable",
                "status_code": 503,
                "error_code": "service_unavailable",
                "reason": "historical provider failure",
            }
            first_judge.provider_circuit_first_request = 1
            first_judge.provider_circuit_first_failure_at = (
                "2026-07-31T00:00:00Z"
            )
            AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )

            resumed_judge = FreshProbeJudge()
            resumed_judge.provider_circuit_open = False
            resumed_judge.provider_circuit_reason = ""
            resumed_judge.consecutive_provider_errors = 0
            resumed_judge.provider_error_threshold = 3
            resumed_judge.provider_circuit_disposition = None
            resumed_judge.provider_circuit_first_request = 0
            resumed_judge.provider_circuit_first_failure_at = ""
            report = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 1)
            self.assertEqual(
                resumed_judge.active_state_at_probe,
                {
                    "open": False,
                    "reason": "",
                    "errors": 0,
                    "disposition": None,
                    "first_request": 0,
                    "first_failure_at": "",
                },
            )
            self.assertFalse(report.metadata["provider_circuit"]["open"])
            self.assertEqual(
                report.metadata["provider_circuit"]["previous_failure"][
                    "disposition"
                ]["status_code"],
                503,
            )
            self.assertEqual(resumed_judge.consecutive_provider_errors, 0)
            restored = CheckpointBundle(root).restore(expected_config=config)
            restored_state = RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(sample_trace()),
                checkpoint=restored,
            )
            circuit = restored_state.provider_state["circuit"]
            self.assertFalse(circuit["open"])
            self.assertEqual(circuit["reason"], "")
            self.assertEqual(circuit["consecutive_provider_errors"], 0)
            self.assertIsNone(circuit["disposition"])
            self.assertEqual(circuit["first_request"], 0)
            self.assertEqual(circuit["first_failure_at"], "")
            previous = restored_state.provider_state["previous_failure"]
            self.assertEqual(previous["disposition"]["status_code"], 503)
            self.assertEqual(previous["first_request"], 1)
            self.assertEqual(
                previous["first_failure_at"],
                "2026-07-31T00:00:00Z",
            )

    def test_provider_state_is_atomic_with_recursive_snapshot(self):
        with tempfile.TemporaryDirectory() as tempdir:
            budgets = dict(sample_config()["budgets"])
            budgets["max_judge_requests"] = 5
            config = sample_config(budgets=budgets)
            graph = TraceGraph.from_trace(sample_trace())
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=ResettingSuccessJudge(), max_judge_requests=5
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            root = Path(tempdir) / "case.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=ResettingSuccessJudge(),
                    max_judge_requests=5,
                    checkpoint=CrashAfterFrontierCompleteSnapshot(root),
                    checkpoint_config=config,
                ).analyze(
                    graph,
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )
            restored = CheckpointBundle(root).restore(expected_config=config)
            snapshot = next(
                item
                for item in reversed(restored.actions)
                if item["operation"] == "state_snapshot"
            )
            self.assertIn("provider_state", snapshot["payload"])
            self.assertFalse(
                any(item["operation"] == "provider_state" for item in restored.actions)
            )

            resumed_judge = ResettingSuccessJudge(errors=99)
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                max_judge_requests=5,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.consecutive_provider_errors, 0)
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())

    def test_recursive_restore_rejects_missing_or_mismatched_provider_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            for mutation in ("missing", "identity"):
                with self.subTest(mutation=mutation):
                    actions = json.loads(json.dumps(restored.actions))
                    snapshot = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "state_snapshot"
                    )
                    if mutation == "missing":
                        snapshot["payload"].pop("provider_state")
                    else:
                        snapshot["payload"]["provider_state"]["identity"] = "0" * 64
                    with self.assertRaises(ValueError):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(sample_trace()),
                            checkpoint=replace(restored, actions=tuple(actions)),
                        )

    def test_final_state_resume_does_not_repeat_an_already_recorded_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = UnknownConfirmationJudge()
            first = AgenticRecursiveAnalyzer(
                judge=first_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            actions_path = root / "investigation-actions.jsonl"
            lines = actions_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[-1])["operation"], "analysis_ready")

            resumed_judge = UnknownConfirmationJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed_judge.step_calls, 0)
            self.assertEqual(resumed_judge.confirmation_calls, 0)
            self.assertEqual(resumed.to_dict(), first.to_dict())

    def test_partial_and_completed_restore_reject_orphan_unknown_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "ownership.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=UnknownConfirmationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            partial_actions = json.loads(json.dumps(restored.actions))
            snapshot = next(
                item
                for item in reversed(partial_actions)
                if item["operation"] == "state_snapshot"
                and item["payload"]["confirmations"]
            )
            snapshot["payload"]["seed_ledger"][0][
                "confirmation_identities"
            ] = []
            with self.assertRaisesRegex(ValueError, "confirmation ownership"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(sample_trace()),
                    checkpoint=replace(restored, actions=tuple(partial_actions)),
                )

            completed_actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(completed_actions)
                if item["operation"] == "analysis_ready"
            )
            report_action["payload"]["report"]["seed_results"][0][
                "confirmation_identities"
            ] = []
            with self.assertRaisesRegex(ValueError, "confirmation ownership"):
                AgenticRecursiveAnalyzer(
                    judge=UnknownConfirmationJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(completed_actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_partial_and_completed_restore_reject_unblocked_unknown_confirmation_owner(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "unknown-owner.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=UnknownConfirmationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            for outcome in ("no_defect", "inconclusive"):
                with self.subTest(entry_point="partial", outcome=outcome):
                    partial_actions = json.loads(json.dumps(restored.actions))
                    snapshot = next(
                        item
                        for item in reversed(partial_actions)
                        if item["operation"] == "state_snapshot"
                        and item["payload"]["confirmations"]
                    )
                    owner = snapshot["payload"]["seed_ledger"][0]
                    owner.update(
                        {
                            "outcome": outcome,
                            "missing_evidence": [],
                            "blocking_reasons": [],
                            "no_defect": outcome == "no_defect",
                        }
                    )
                    with self.assertRaisesRegex(ValueError, "unresolved confirmation"):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(sample_trace()),
                            checkpoint=replace(
                                restored, actions=tuple(partial_actions)
                            ),
                        )

                with self.subTest(entry_point="completed", outcome=outcome):
                    completed_actions = json.loads(json.dumps(restored.actions))
                    report_action = next(
                        item
                        for item in reversed(completed_actions)
                        if item["operation"] == "analysis_ready"
                    )
                    report_action["payload"]["report"]["seed_results"][0].update(
                        {
                            "outcome": outcome,
                            "missing_evidence": [],
                            "blocking_reasons": [],
                        }
                    )
                    with self.assertRaisesRegex(ValueError, "unresolved confirmation"):
                        AgenticRecursiveAnalyzer(
                            judge=UnknownConfirmationJudge(),
                            checkpoint=InjectedRestoreCheckpoint(
                                replace(restored, actions=tuple(completed_actions)),
                                root,
                            ),
                            checkpoint_config=config,
                        ).analyze(
                            TraceGraph.from_trace(sample_trace()),
                            start_refs=["record:only"],
                            objective="Find the defect.",
                            analysis_perspective="Improve repository reasoning.",
                        )

    def test_partial_and_completed_restore_reject_unblocked_unresolved_rejection(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "unresolved-rejection-owner.checkpoint"
            config = sample_config()
            AgenticRecursiveAnalyzer(
                judge=UnknownConfirmationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            restored = CheckpointBundle(root).restore(expected_config=config)

            partial_actions = json.loads(json.dumps(restored.actions))
            snapshot = next(
                item
                for item in reversed(partial_actions)
                if item["operation"] == "state_snapshot"
                and item["payload"]["confirmations"]
            )
            snapshot["payload"]["confirmations"][0].update(
                {
                    "status": "rejected",
                    "confidence": 0.5,
                    "counterfactual_status": "unknown",
                    "factor_role": "unknown",
                }
            )
            refresh_checkpoint_confirmation_response_identity(
                snapshot["payload"]["confirmations"][0]
            )
            snapshot["payload"]["seed_ledger"][0].update(
                {
                    "outcome": "no_defect",
                    "missing_evidence": [],
                    "blocking_reasons": [],
                    "no_defect": True,
                }
            )
            with self.assertRaisesRegex(ValueError, "unresolved confirmation"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(sample_trace()),
                    checkpoint=replace(restored, actions=tuple(partial_actions)),
                )

            completed_actions = json.loads(json.dumps(restored.actions))
            report_action = next(
                item
                for item in reversed(completed_actions)
                if item["operation"] == "analysis_ready"
            )
            report_action["payload"]["report"]["confirmations"][0].update(
                {
                    "status": "rejected",
                    "confidence": 0.5,
                    "counterfactual_status": "unknown",
                    "factor_role": "unknown",
                }
            )
            refresh_checkpoint_confirmation_response_identity(
                report_action["payload"]["report"]["confirmations"][0]
            )
            report_action["payload"]["report"]["seed_results"][0].update(
                {
                    "outcome": "no_defect",
                    "missing_evidence": [],
                    "blocking_reasons": [],
                }
            )
            with self.assertRaisesRegex(ValueError, "unresolved confirmation"):
                AgenticRecursiveAnalyzer(
                    judge=UnknownConfirmationJudge(),
                    checkpoint=InjectedRestoreCheckpoint(
                        replace(restored, actions=tuple(completed_actions)), root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(sample_trace()),
                    start_refs=["record:only"],
                    objective="Find the defect.",
                    analysis_perspective="Improve repository reasoning.",
                )

    def test_signal_arriving_during_confirmation_writes_partial_resume_marker(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            stop_flag = [False]
            report = AgenticRecursiveAnalyzer(
                judge=StopDuringConfirmationJudge(stop_flag),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=sample_config(),
                stop_requested=lambda: stop_flag[0],
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(report.analysis_outcome, "inconclusive")
            self.assertEqual(report.metadata["termination_reason"], "signal_interrupted")
            actions = (root / "investigation-actions.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(json.loads(actions[-1])["operation"], "analysis_ready")

    def test_resumed_signal_checkpoint_converges_to_uninterrupted_report(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            partial = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(partial.analysis_outcome, "inconclusive")

            resumed = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            uninterrupted = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge()
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertEqual(resumed.to_dict(), uninterrupted.to_dict())

    def test_resumed_global_prepass_never_retries_started_second_seed(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
            fusion_mode="retrieval-global",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "global.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                AgenticRecursiveAnalyzer(
                    judge=InterruptingGlobalNoDefectJudge(interrupt_on_call=2),
                    fusion_mode="retrieval-global",
                    checkpoint=CheckpointBundle(root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=start_refs,
                    objective="Determine whether either observation is supported.",
                    analysis_perspective="",
                )

            resumed_judge = InterruptingGlobalNoDefectJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective="Determine whether either observation is supported.",
                analysis_perspective="",
            )
        self.assertEqual(resumed_judge.global_calls, [])
        by_ref = {item.start_ref: item for item in resumed.seed_results}
        self.assertEqual(by_ref["record:defect_one"].outcome, "no_defect")
        self.assertEqual(
            by_ref["record:defect_two"].outcome,
            "execution_failed",
        )
        self.assertEqual(
            by_ref["record:defect_two"].execution_failures[0]["reason"],
            "analysis_interrupted",
        )

    def test_tail_repair_audit_is_persisted_in_report_metadata(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')
            reopened = CheckpointBundle(root)
            reopened.initialize(sample_config())
            report = AgenticRecursiveAnalyzer(
                judge=CountingOfflineJudge(),
                checkpoint=reopened,
                checkpoint_config=sample_config(),
            ).analyze(
                TraceGraph.from_trace(sample_trace()),
                start_refs=["record:only"],
                objective="Find the defect.",
                analysis_perspective="Improve repository reasoning.",
            )
            self.assertGreaterEqual(
                report.metadata["checkpoint_audit"]["tail_repair_count"], 1
            )

    def test_restore_rejects_nonexact_tail_repair_event_schema(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            bundle = CheckpointBundle(root)
            bundle.initialize(sample_config())
            with bundle.actions_path.open("ab") as handle:
                handle.write(b'{"partial"')
            bundle.initialize(sample_config())

            commit = json.loads(bundle.commit_path.read_text(encoding="utf-8"))
            commit["tail_repair_events"][0]["unexpected"] = True
            commit["commit_hash"] = _sha256(
                {key: value for key, value in commit.items() if key != "commit_hash"}
            )
            bundle.commit_path.write_text(
                json.dumps(commit, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaises(CheckpointCorruptionError):
                CheckpointBundle(root).restore(expected_config=sample_config())


if __name__ == "__main__":
    unittest.main()
