from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    BoundedJudgeCapability,
    OfflineJudgeCapability,
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
    CheckpointBundle,
    CheckpointCompatibilityError,
    CheckpointCorruptionError,
    CheckpointState,
    _sha256,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.hypotheses import HypothesisLedger, RecursiveFrontier
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _provider_state_payload,
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
    return build_checkpoint_config(**values)


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
    root["node_ref"] = ghost_ref
    root["recursive_path"] = list(confirmation["recursive_path"])
    root["counterfactual"] = confirmation["counterfactual"]
    root["confirmation"] = copy.deepcopy(confirmation)
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    report_payload["root_causes"][0]["node_ref"] = ghost_ref
    seed["confirmed_root_refs"] = [ghost_ref]
    seed["confirmation_identities"] = [confirmation["confirmation_identity"]]


def forge_checkpoint_root_path(report_payload: dict, recursive_path: list[str]) -> None:
    confirmation = report_payload["confirmations"][0]
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
    root = report_payload["confirmed_roots"][0]
    root["recursive_path"] = list(recursive_path)
    root["confirmation"] = copy.deepcopy(confirmation)
    root["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    report_payload["seed_results"][0]["confirmation_identities"] = [
        confirmation["confirmation_identity"]
    ]


def forge_checkpoint_factor_path(payload: dict, recursive_path: list[str]) -> None:
    factor = payload["contributing_conditions"][0]
    old_identity = factor["confirmation"]["confirmation_identity"]
    confirmation = next(
        item
        for item in payload["confirmations"]
        if item["confirmation_identity"] == old_identity
    )
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
    factor["recursive_path"] = list(recursive_path)
    factor["confirmation"] = copy.deepcopy(confirmation)
    factor["provenance"] = canonical_confirmation_publication_provenance(
        RootConfirmation.from_dict(confirmation)
    )
    for seed in payload["seed_ledger"] if "seed_ledger" in payload else payload["seed_results"]:
        seed["confirmation_identities"] = [
            confirmation["confirmation_identity"]
            if identity == old_identity
            else identity
            for identity in seed["confirmation_identities"]
        ]


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
                "provider_call_completed",
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

            with self.assertRaises(CheckpointCompatibilityError):
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
            "global-candidate-judgment/v9+validation-envelope/v9+capsule/v7"
            "+evidence-policy/v5+local-state-owner/v1"
            "+global-pass-identity/v1+failure-action/v3"
            "+failure-projection/v4+terminal-record-schema/v3"
            "+judge-lifecycle/v1+graph-seed-authority/v1"
            "+objective-authority/v1"
            "+candidate-set-closure/v1+comparison-matrix-closure/v1"
            "+bounded-evidence-expansion/v1",
        )
        self.assertEqual(
            config["root_confirmation_contract"],
            "recursive-root-confirmation/v17+resolution/v2+evidence-policy/v5"
            "+artifact-owner/v1+terminal-evidence/v2+local-state-owner/v1"
            "+action-projection/v7+response-identity/v1+counterfactual/v1"
            "+queue-response-identity/v1+published-root-projection/v3"
            "+causal-publication/v3"
            "+perspective-binding/v1"
            "+step-action-projection/v1+confirmation-request-identity/v3"
            "+confirmation-request-projection/v2",
        )
        self.assertEqual(CHECKPOINT_SCHEMA_VERSION, "recursive-attribution-checkpoint/v20")

    def test_completed_report_rejects_v4_global_judgment_with_unresolved_evidence(self):
        trace = multi_seed_global_trace()
        start_refs = ["record:defect_one", "record:defect_two"]
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=start_refs,
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
            ConfirmingScriptedJudge,
            RecursiveRootRankingTest,
            observed_trace,
            relation,
            step,
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
        )

        def judge():
            return ConfirmingScriptedJudge(
                {
                    "record:change": step(
                        "record:change",
                        predecessors=(
                            relation("record:context", "same_defect_propagation"),
                        ),
                    ),
                    "record:context": RecursiveRootRankingTest()._confirmation_step,
                },
                {
                    "record:context": RootConfirmation.rejected(
                        "record:context",
                        "The context is a condition, not a necessary root.",
                        evidence_refs=[
                            "record:context",
                            "record:change",
                            "record:observed_defect",
                        ],
                        factor_role="contributing_condition",
                    )
                },
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "stale-intermediate-factor.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=judge(),
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
            ConfirmingScriptedJudge,
            RecursiveRootRankingTest,
            observed_trace,
            relation,
            step,
        )

        trace = observed_trace(branching=True)
        config = sample_config(
            trace=trace,
            case_id=trace["case_id"],
            start_refs=["record:observed_defect"],
        )

        def judge():
            return ConfirmingScriptedJudge(
                {
                    "record:change": step(
                        "record:change",
                        predecessors=(
                            relation("record:context", "same_defect_propagation"),
                        ),
                    ),
                    "record:context": RecursiveRootRankingTest()._confirmation_step,
                },
                {
                    "record:context": RootConfirmation.rejected(
                        "record:context",
                        "The context is a condition, not a necessary root.",
                        evidence_refs=[
                            "record:context",
                            "record:change",
                            "record:observed_defect",
                        ],
                        factor_role="contributing_condition",
                    )
                },
            )

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "factor-path.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=judge(),
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
                and item["payload"]["contributing_conditions"]
            )
            forge_checkpoint_factor_path(
                snapshot["payload"],
                ["record:context", "record:decision", "record:observed_defect"],
            )
            with self.assertRaisesRegex(ValueError, "non-root.*causal edge"):
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
            with self.assertRaisesRegex(ValueError, "non-root.*causal edge"):
                AgenticRecursiveAnalyzer(
                    judge=judge(),
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

    def test_resume_restores_provider_circuit_state_before_traversal(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "case.checkpoint"
            config = sample_config()
            first_judge = CountingOfflineJudge()
            first_judge.provider_circuit_open = True
            first_judge.provider_circuit_reason = "provider unavailable"
            first_judge.consecutive_provider_errors = 3
            first_judge.provider_error_threshold = 3
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

            resumed_judge = CountingOfflineJudge()
            resumed_judge.provider_circuit_open = False
            resumed_judge.provider_circuit_reason = ""
            resumed_judge.consecutive_provider_errors = 0
            resumed_judge.provider_error_threshold = 3
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
            self.assertTrue(report.metadata["provider_circuit"]["open"])
            self.assertEqual(resumed_judge.consecutive_provider_errors, 3)

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
        self.assertEqual(by_ref["record:defect_two"].outcome, "evidence_gap")
        self.assertIn(
            "global_judge_interrupted",
            by_ref["record:defect_two"].blocking_reasons,
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
