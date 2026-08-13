from __future__ import annotations

import hashlib
import copy
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_judge import (
    BoundedJudgeCallError,
    BoundedJudgeCallResult,
    GLOBAL_CANDIDATE_SYSTEM_PROMPT,
)
from trace_attribution.causal_state import (
    CausalCandidate,
    RecursiveAttributionReport,
)
from trace_attribution.candidate_paging import (
    GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP,
)
from trace_attribution.candidate_clustering import (
    build_candidate_cluster_manifest,
    build_candidate_cluster_shadow_event,
)
from trace_attribution.cluster_triage_judge import (
    ClusterTriageCapability,
    ClusterTriagePageRequest,
    parse_cluster_triage_judgment,
)
from trace_attribution.global_judge import (
    GlobalCandidateAssessment,
    GlobalCandidateJudgment,
    GlobalJudgeCapability,
    build_global_candidate_prompt,
    global_candidate_request_from_validation_envelope,
)
from trace_attribution.judge_budget import JudgeContextBudget
from trace_attribution.graph import TraceGraph
from trace_attribution.checkpoint import (
    CheckpointBundle,
    CheckpointCompatibilityError,
    build_checkpoint_config,
    validate_checkpoint_config,
)
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    validate_recursive_report_against_graph,
)


def paginated_trace(candidate_count: int = 25) -> dict:
    records = [
        {
            "record_id": "decision-{0:03d}".format(index),
            "component": "agent",
            "event_type": "decision",
            "data": {
                "decision_type": "reasoning_block",
                "rationale": "Candidate authored decision {0}.".format(index),
            },
        }
        for index in range(candidate_count)
    ]
    records.append(
        {
            "record_id": "observed-defect",
            "component": "evaluation",
            "event_type": "case.observed_defect",
            "data": {
                "failure_type": "semantic_gap",
                "expected": "All required behavior is implemented.",
                "actual": "A required behavior is missing.",
                "mechanism": "An authored decision omitted required work.",
                "scope": "implementation",
                "summary": "Required behavior is missing.",
            },
        }
    )
    edges = [
        {
            "from": {
                "type": "record",
                "id": "decision-{0:03d}".format(index),
            },
            "to": {"type": "record", "id": "observed-defect"},
            "relation": "decision_guided_change",
            "evidence_type": "confirmed",
            "confidence": 1.0,
            "eligible_for_attribution": True,
        }
        for index in range(candidate_count)
    ]
    return {
        "case_id": "global-pagination-integration",
        "records": records,
        "dataflow_edges": edges,
    }


def clustered_paginated_trace(candidate_count: int = 25) -> dict:
    trace = paginated_trace(candidate_count)
    for index, record in enumerate(trace["records"][:-1]):
        record["data"]["action_group_id"] = "cluster-{0:02d}".format(
            index // 5
        )
    return trace


class _NoDefectPagingJudge(GlobalJudgeCapability):
    def __init__(self) -> None:
        self.global_requests = []
        self.request_count = 0
        self.provider_circuit_open = False
        self.provider_circuit_reason = ""

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        self.request_count += 1
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status="absent",
                input_defect_status="absent",
                output_defect_status="absent",
                causal_path_refs=tuple(capsule.downstream_path),
                counterfactual={
                    "intervention_ref": capsule.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                compared_candidate_refs=(
                    request.open_authored_root_candidate_refs
                ),
                causal_role="exculpatory_evidence",
                responsibility="none",
                candidate_phase="intermediate",
                obligation_status_before="unknown",
                obligation_status_after="unknown",
                repair_window_effect="remained_open",
                failure_mode="none",
                obligation_refs=(),
                contribution_mechanism=None,
                reason="The supplied facts rule out this page-local candidate.",
                evidence_refs=(capsule.candidate_ref,),
                confidence=0.95,
            )
            for capsule in request.capsules
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="no_defect",
                reason="Every candidate in this page is ruled out.",
                assessments=assessments,
                selected_candidate_refs=(),
                expansion_requests=(),
                decisive_evidence_refs=(
                    request.capsules[0].candidate_ref,
                ),
                missing_evidence=(),
                confidence=0.95,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": (
                        request.active_defect.fingerprint
                    ),
                    "active_focus_text_hash": (
                        request.active_focus_text_hash
                    ),
                },
            ),
            1,
        )


class _BudgetedNoDefectPagingJudge(_NoDefectPagingJudge):
    def __init__(self) -> None:
        super().__init__()
        self.max_tokens = 4096
        self.context_budget = JudgeContextBudget(
            context_window_tokens=60_288,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )


class _TinyBudgetNoDefectPagingJudge(_NoDefectPagingJudge):
    def __init__(self) -> None:
        super().__init__()
        self.max_tokens = 4096
        self.context_budget = JudgeContextBudget(
            context_window_tokens=13_000,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )


class _NoRootCandidatesPagingJudge(_NoDefectPagingJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        self.request_count += 1
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status="present",
                input_defect_status="present",
                output_defect_status="present",
                causal_path_refs=tuple(capsule.downstream_path),
                counterfactual={
                    "intervention_ref": capsule.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": "present",
                    "causal_effect": "does_not_prevent_defect",
                },
                compared_candidate_refs=(
                    request.open_authored_root_candidate_refs
                ),
                causal_role="unrelated",
                responsibility="none",
                candidate_phase="intermediate",
                obligation_status_before="pending",
                obligation_status_after="pending",
                repair_window_effect="remained_open",
                failure_mode="ordinary_non_repair",
                obligation_refs=(),
                contribution_mechanism=None,
                reason=(
                    "The active defect persists, but this page-local "
                    "candidate does not introduce it."
                ),
                evidence_refs=(capsule.candidate_ref,),
                confidence=0.95,
            )
            for capsule in request.capsules
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="no_root_candidates",
                reason=(
                    "The active defect remains present, but this page has "
                    "no supported root candidate."
                ),
                assessments=assessments,
                selected_candidate_refs=(),
                expansion_requests=(),
                decisive_evidence_refs=tuple(
                    capsule.candidate_ref
                    for capsule in request.capsules
                ),
                missing_evidence=(),
                confidence=0.95,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": (
                        request.active_defect.fingerprint
                    ),
                    "active_focus_text_hash": (
                        request.active_focus_text_hash
                    ),
                },
            ),
            1,
        )


class _LateRootPagingJudge(_NoDefectPagingJudge):
    target_ref = "record:decision-024"

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if self.target_ref not in request.offered_candidate_refs:
            return super().judge_candidates_bounded(
                request,
                max_physical_requests=max_physical_requests,
            )
        self.global_requests.append(request)
        self.request_count += 1
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status=(
                    "present"
                    if capsule.candidate_ref == self.target_ref
                    else "absent"
                ),
                input_defect_status="absent",
                output_defect_status=(
                    "present"
                    if capsule.candidate_ref == self.target_ref
                    else "absent"
                ),
                causal_path_refs=tuple(capsule.downstream_path),
                counterfactual={
                    "intervention_ref": capsule.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": (
                        "absent"
                        if capsule.candidate_ref == self.target_ref
                        else "present"
                    ),
                    "causal_effect": (
                        "prevents_defect"
                        if capsule.candidate_ref == self.target_ref
                        else "does_not_prevent_defect"
                    ),
                },
                compared_candidate_refs=(
                    request.open_authored_root_candidate_refs
                ),
                causal_role=(
                    "root_candidate"
                    if capsule.candidate_ref == self.target_ref
                    else "exculpatory_evidence"
                ),
                responsibility=(
                    "primary"
                    if capsule.candidate_ref == self.target_ref
                    else "none"
                ),
                candidate_phase=(
                    "implementation"
                    if capsule.candidate_ref == self.target_ref
                    else "intermediate"
                ),
                obligation_status_before="unknown",
                obligation_status_after="unknown",
                repair_window_effect="remained_open",
                failure_mode=(
                    "positive_introduction"
                    if capsule.candidate_ref == self.target_ref
                    else "none"
                ),
                obligation_refs=(),
                contribution_mechanism=None,
                reason="Structured page-local root comparison.",
                evidence_refs=(capsule.candidate_ref,),
                confidence=0.95,
            )
            for capsule in request.capsules
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="candidate_roots",
                reason="The late-page authored decision introduces the defect.",
                assessments=assessments,
                selected_candidate_refs=(self.target_ref,),
                expansion_requests=(),
                decisive_evidence_refs=(self.target_ref,),
                missing_evidence=(),
                confidence=0.95,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": (
                        request.active_defect.fingerprint
                    ),
                    "active_focus_text_hash": (
                        request.active_focus_text_hash
                    ),
                },
            ),
            1,
        )


class _BudgetedOnePerPageRootJudge(_LateRootPagingJudge):
    def __init__(self) -> None:
        super().__init__()
        self.max_tokens = 4096
        self.context_budget = JudgeContextBudget(
            context_window_tokens=34_588,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.target_ref = request.capsules[0].candidate_ref
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class _FirstPageInvalidJudge(_NoDefectPagingJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if not self.global_requests:
            self.global_requests.append(request)
            self.request_count += 1
            return BoundedJudgeCallResult({"invalid": True}, 1)
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class _PaymentFailureAfterFirstPageJudge(_NoDefectPagingJudge):
    def __init__(self) -> None:
        super().__init__()
        self.provider_error_threshold = 3
        self.consecutive_provider_errors = 0
        self.provider_circuit_disposition = None
        self.provider_circuit_first_request = 0
        self.provider_circuit_first_failure_at = ""

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if len(self.global_requests) == 1:
            self.global_requests.append(request)
            self.request_count += 1
            self.consecutive_provider_errors = 1
            self.provider_circuit_open = True
            self.provider_circuit_reason = "HTTP 402: insufficient balance"
            self.provider_circuit_disposition = {
                "retryable": False,
                "category": "http_non_retryable",
                "status_code": 402,
                "error_code": "insufficient_balance",
                "reason": "HTTP 402: insufficient balance",
            }
            self.provider_circuit_first_request = self.request_count
            self.provider_circuit_first_failure_at = "2026-07-31T00:00:00Z"
            raise BoundedJudgeCallError(
                "HTTP 402: insufficient balance",
                physical_requests=1,
            )
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class _ImmediatePaymentFailureJudge(_PaymentFailureAfterFirstPageJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if not self.global_requests:
            self.global_requests.append(request)
            self.request_count += 1
            self.consecutive_provider_errors = 1
            self.provider_circuit_open = True
            self.provider_circuit_reason = "HTTP 402: insufficient balance"
            self.provider_circuit_disposition = {
                "retryable": False,
                "category": "http_non_retryable",
                "status_code": 402,
                "error_code": "insufficient_balance",
                "reason": "HTTP 402: insufficient balance",
            }
            self.provider_circuit_first_request = self.request_count
            self.provider_circuit_first_failure_at = "2026-07-31T00:00:01Z"
            raise BoundedJudgeCallError(
                "HTTP 402: insufficient balance",
                physical_requests=1,
            )
        raise AssertionError("an open 402 circuit must stop later pages")


class _InterruptingFirstPageJudge(_NoDefectPagingJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if not self.global_requests:
            self.global_requests.append(request)
            self.request_count += 1
            raise KeyboardInterrupt("interrupt after durable page start")
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class _AllCandidatesRemainPlausibleJudge(_NoDefectPagingJudge):
    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.global_requests.append(request)
        self.request_count += 1
        assessments = tuple(
            GlobalCandidateAssessment(
                candidate_ref=capsule.candidate_ref,
                defect_status="present",
                input_defect_status="absent",
                output_defect_status="present",
                causal_path_refs=tuple(capsule.downstream_path),
                counterfactual={
                    "intervention_ref": capsule.candidate_ref,
                    "intervention_kind": (
                        "replace_with_semantically_correct_behavior"
                    ),
                    "predicted_defect_status": "absent",
                    "causal_effect": "prevents_defect",
                },
                compared_candidate_refs=(
                    request.open_authored_root_candidate_refs
                ),
                causal_role="root_candidate",
                responsibility="primary",
                candidate_phase="implementation",
                obligation_status_before="unknown",
                obligation_status_after="unknown",
                repair_window_effect="remained_open",
                failure_mode="positive_introduction",
                obligation_refs=(),
                contribution_mechanism=None,
                reason="This candidate remains structurally plausible.",
                evidence_refs=(capsule.candidate_ref,),
                confidence=0.8,
            )
            for capsule in request.capsules
        )
        selected = tuple(
            capsule.candidate_ref for capsule in request.capsules[:3]
        )
        return BoundedJudgeCallResult(
            GlobalCandidateJudgment(
                outcome="candidate_roots",
                reason="The current page cannot distinguish all plausible roots.",
                assessments=assessments,
                selected_candidate_refs=selected,
                expansion_requests=(),
                decisive_evidence_refs=selected,
                missing_evidence=(),
                confidence=0.8,
                active_focus_binding={
                    "seed_ref": request.seed_ref,
                    "defect_fingerprint": (
                        request.active_defect.fingerprint
                    ),
                    "active_focus_text_hash": (
                        request.active_focus_text_hash
                    ),
                },
            ),
            1,
        )


class _LargeBudgetAllCandidatesRemainPlausibleJudge(
    _AllCandidatesRemainPlausibleJudge
):
    def __init__(self) -> None:
        super().__init__()
        self.max_tokens = 4096
        self.context_budget = JudgeContextBudget(
            context_window_tokens=1_000_000,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )


class _BudgetedAllCandidatesRemainPlausibleJudge(
    _AllCandidatesRemainPlausibleJudge
):
    def __init__(self) -> None:
        super().__init__()
        self.max_tokens = 4096
        self.context_budget = JudgeContextBudget(
            context_window_tokens=34_288,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )


class _BudgetedEightFinalistsJudge(
    _BudgetedAllCandidatesRemainPlausibleJudge
):
    excluded_ref = "record:decision-008"

    def __init__(self) -> None:
        super().__init__()
        self.context_budget = JudgeContextBudget(
            context_window_tokens=34_588,
            max_output_tokens=self.max_tokens,
            safety_margin_tokens=8_192,
        )

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        if request.offered_candidate_refs == (self.excluded_ref,):
            return _NoDefectPagingJudge.judge_candidates_bounded(
                self,
                request,
                max_physical_requests=max_physical_requests,
            )
        return super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )


class _SelectiveClusterTriageJudge(
    _NoDefectPagingJudge,
    ClusterTriageCapability,
):
    selected_refs = frozenset(
        "record:decision-{0:03d}".format(index)
        for index in range(15, 25)
    )
    uncertain_refs = frozenset()

    def __init__(self) -> None:
        super().__init__()
        self.directory_requests = []

    def triage_candidate_cluster_page_bounded(
        self,
        page,
        *,
        max_physical_requests,
    ):
        self.directory_requests.append(page)
        decisions = []
        for cluster in page.clusters:
            selected = bool(
                self.selected_refs.intersection(
                    cluster["eligible_candidate_refs"]
                )
            )
            uncertain = bool(
                self.uncertain_refs.intersection(
                    cluster["eligible_candidate_refs"]
                )
            )
            disposition = (
                "uncertain"
                if uncertain
                else "selected"
                if selected
                else "unselected"
            )
            evidence_ref = cluster["evidence_refs"][0]
            decisions.append(
                {
                    "cluster_id": cluster["cluster_id"],
                    "disposition": disposition,
                    "rationale": (
                        "The cluster evidence remains semantically incomplete."
                        if uncertain
                        else "The cluster remains causally plausible."
                        if selected
                        else "The cluster is outside the active failure window."
                    ),
                    "evidence_refs": [evidence_ref],
                    "mismatch_evidence": (
                        []
                        if disposition in {"selected", "uncertain"}
                        else [
                            {
                                "evidence_ref": evidence_ref,
                                "mismatch": (
                                    "The trace-grounded action predates the "
                                    "active failure window."
                                ),
                            }
                        ]
                    ),
                }
            )
        judgment = parse_cluster_triage_judgment(
            {
                "schema": "candidate-cluster-triage-page-response/v1",
                "page_identity": page.page_identity,
                "request_identity": page.request_identity,
                "partition_identity": page.partition_identity,
                "page_index": page.page_index,
                "page_count": page.page_count,
                "decisions": decisions,
            },
            page=page,
        )
        return BoundedJudgeCallResult(judgment, 1)


class _TwoPhysicalStrictClusterTriageJudge(_SelectiveClusterTriageJudge):
    def __init__(self) -> None:
        super().__init__()
        self.strict_allowances = []

    def judge_candidates_bounded(self, request, *, max_physical_requests):
        self.strict_allowances.append(max_physical_requests)
        result = super().judge_candidates_bounded(
            request,
            max_physical_requests=max_physical_requests,
        )
        return BoundedJudgeCallResult(
            result.value,
            2,
            diagnostics=result.diagnostics,
        )


class _UncertainClusterTriageJudge(_SelectiveClusterTriageJudge):
    selected_refs = frozenset()
    uncertain_refs = frozenset(
        "record:decision-{0:03d}".format(index)
        for index in range(20, 25)
    )


class _AllUnselectedClusterTriageJudge(_SelectiveClusterTriageJudge):
    selected_refs = frozenset()
    uncertain_refs = frozenset()


class _LargeSyntheticClusterTriageJudge(_SelectiveClusterTriageJudge):
    selected_refs = frozenset(
        "record:decision-{0:03d}".format(index)
        for index in (0, 20, 40, 60, 80, 100)
    )
    uncertain_refs = frozenset()


class _FailedClusterTriageJudge(
    _NoDefectPagingJudge,
    ClusterTriageCapability,
):
    def __init__(self, *, physical_requests: int, untyped: bool = False):
        super().__init__()
        self.directory_requests = []
        self.directory_physical_requests = physical_requests
        self.untyped = untyped

    def triage_candidate_cluster_page_bounded(
        self,
        page,
        *,
        max_physical_requests,
    ):
        self.directory_requests.append(page)
        if self.untyped:
            raise RuntimeError("directory transport interrupted")
        raise BoundedJudgeCallError(
            "directory Provider unavailable",
            physical_requests=self.directory_physical_requests,
            diagnostics={"failure": "provider"},
        )


class _FixedPoolAnalyzer(AgenticRecursiveAnalyzer):
    def _global_candidate_pool(self, state, graph, item):
        candidates = tuple(
            CausalCandidate(
                ref=ref,
                node=graph.nodes[ref],
                source="confirmed_edge",
            )
            for ref in sorted(
                graph.nodes
            )
            if ref.startswith("record:decision-")
        )
        paths = {
            candidate.ref: (
                candidate.ref,
                "record:observed-defect",
            )
            for candidate in candidates
        }
        return candidates, paths, None


class _ManifestAuthorityMutationAnalyzer(AgenticRecursiveAnalyzer):
    def __init__(self, *args, manifest_mutation: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifest_mutation = manifest_mutation

    def _global_candidate_pool(self, state, graph, item):
        result = super()._global_candidate_pool(state, graph, item)
        shadows = [
            event
            for event in state.investigation_journal
            if event.get("kind") == "candidate_cluster_manifest_shadow"
        ]
        shadow = shadows[-1]
        if self.manifest_mutation == "missing":
            state.investigation_journal.remove(shadow)
        elif self.manifest_mutation == "duplicate":
            state.investigation_journal.append(copy.deepcopy(shadow))
        elif self.manifest_mutation == "conflicting":
            shadow["source_selection_identity"] = "f" * 64
        elif self.manifest_mutation in {"stale_seed", "stale_defect"}:
            candidates, paths, funnel = result
            manifest = build_candidate_cluster_manifest(
                graph=graph,
                candidates=candidates,
                candidate_paths=paths,
                candidate_audit=funnel["candidate_audit"],
                source_selection_identity=funnel["selection_identity"],
                seed_ref=(
                    "record:decision-000"
                    if self.manifest_mutation == "stale_seed"
                    else "record:observed-defect"
                ),
                defect_fingerprint=(
                    "stale-defect-fingerprint"
                    if self.manifest_mutation == "stale_defect"
                    else shadow["manifest"]["defect_fingerprint"]
                ),
            )
            replacement = build_candidate_cluster_shadow_event(
                manifest=manifest,
                seed_binding_identity=shadow["seed_binding_identity"],
            )
            state.investigation_journal[
                state.investigation_journal.index(shadow)
            ] = replacement
        else:
            raise AssertionError("unsupported manifest mutation")
        return result


class _PrePoolStaleManifestAnalyzer(AgenticRecursiveAnalyzer):
    def __init__(
        self,
        *args,
        manifest_mutation: str,
        source_selection_drift: bool = False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.manifest_mutation = manifest_mutation
        self.source_selection_drift = source_selection_drift
        self.stale_manifest_injected = False

    def _global_candidate_pool(self, state, graph, item):
        if self.stale_manifest_injected:
            return super()._global_candidate_pool(state, graph, item)
        result = super()._global_candidate_pool(state, graph, item)
        shadow = next(
            event
            for event in reversed(state.investigation_journal)
            if event.get("kind") == "candidate_cluster_manifest_shadow"
        )
        candidates, paths, funnel = result
        manifest = build_candidate_cluster_manifest(
            graph=graph,
            candidates=candidates,
            candidate_paths=paths,
            candidate_audit=funnel["candidate_audit"],
            source_selection_identity=(
                "f" * 64
                if self.source_selection_drift
                else funnel["selection_identity"]
            ),
            seed_ref=(
                "record:decision-000"
                if self.manifest_mutation == "stale_seed"
                else "record:observed-defect"
            ),
            defect_fingerprint=(
                "stale-defect-fingerprint"
                if self.manifest_mutation == "stale_defect"
                else shadow["manifest"]["defect_fingerprint"]
            ),
        )
        state.investigation_journal[
            state.investigation_journal.index(shadow)
        ] = build_candidate_cluster_shadow_event(
            manifest=manifest,
            seed_binding_identity=shadow["seed_binding_identity"],
        )
        self.stale_manifest_injected = True
        return super()._global_candidate_pool(state, graph, item)


def _resign_stale_manifest_shadow(
    event: dict,
    mutation: str,
    *,
    source_selection_drift: bool = False,
) -> None:
    manifest = copy.deepcopy(event["manifest"])
    if mutation == "stale_seed":
        manifest["seed_ref"] = "record:decision-000"
    elif mutation == "stale_defect":
        manifest["defect_fingerprint"] = "stale-defect-fingerprint"
    else:
        raise AssertionError("unsupported stale manifest mutation")
    if source_selection_drift:
        manifest["source_selection_identity"] = "f" * 64
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key != "manifest_identity"
    }
    manifest_identity = hashlib.sha256(
        stable_json(
            {
                "schema": "candidate-cluster-manifest-identity/v1",
                "facts": unsigned,
            }
        ).encode("utf-8")
    ).hexdigest()
    manifest["manifest_identity"] = manifest_identity
    event["manifest"] = manifest
    event["manifest_identity"] = manifest_identity
    event["source_selection_identity"] = manifest[
        "source_selection_identity"
    ]


class _StaleManifestAtPlanCheckpoint(CheckpointBundle):
    def __init__(
        self,
        root: Path,
        mutation: str,
        *,
        source_selection_drift: bool = False,
    ) -> None:
        super().__init__(root)
        self.mutation = mutation
        self.source_selection_drift = source_selection_drift

    def commit_snapshot(self, **kwargs):
        semantic_key = str(kwargs.get("semantic_key") or "")
        if semantic_key.startswith("cluster-triage:plan:"):
            action_payload = copy.deepcopy(kwargs["action_payload"])
            shadow = next(
                event
                for event in action_payload["investigation_journal"]
                if event.get("kind")
                == "candidate_cluster_manifest_shadow"
            )
            _resign_stale_manifest_shadow(
                shadow,
                self.mutation,
                source_selection_drift=self.source_selection_drift,
            )
            commit = super().commit_snapshot(
                **{
                    **kwargs,
                    "action_payload": action_payload,
                }
            )
            raise _DirectoryPageCheckpointStop(
                "stop after persisting a pre-pool stale manifest"
            )
        return super().commit_snapshot(**kwargs)


class _StopAfterPagePlanCheckpoint(CheckpointBundle):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.page_plan_persisted = False

    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        if str(kwargs.get("semantic_key") or "").startswith(
            "global:page-plan:"
        ):
            self.page_plan_persisted = True
        return commit


class _StopAfterFinalPagePlanCheckpoint(CheckpointBundle):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.final_plan_persisted = False

    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        action_payload = kwargs.get("action_payload")
        journal = (
            action_payload.get("investigation_journal", ())
            if isinstance(action_payload, dict)
            else ()
        )
        self.final_plan_persisted = any(
            isinstance(event, dict)
            and event.get("kind") == "global_candidate_page_plan"
            and event.get("page_phase") == "final"
            for event in journal
        )
        return commit


class _StopAfterClusterExpansionCheckpoint(CheckpointBundle):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.cluster_expansion_persisted = False

    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        if str(kwargs.get("semantic_key") or "").startswith(
            "cluster-triage:expansion:"
        ):
            self.cluster_expansion_persisted = True
        return commit


class _DirectoryPageCheckpointStop(RuntimeError):
    pass


class _StopAfterFirstDirectoryPageCheckpoint(CheckpointBundle):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.directory_page_snapshots = 0

    def commit_snapshot(self, **kwargs):
        commit = super().commit_snapshot(**kwargs)
        if str(kwargs.get("semantic_key") or "").startswith(
            "cluster-triage:page:"
        ):
            self.directory_page_snapshots += 1
            if self.directory_page_snapshots == 1:
                raise _DirectoryPageCheckpointStop(
                    "stop after first directory page snapshot"
                )
        return commit


class _StopAfterDirectoryPageStartedCheckpoint(CheckpointBundle):
    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == "candidate_cluster_triage_page_started":
            raise _DirectoryPageCheckpointStop(
                "stop after directory page started action"
            )
        return record


class _ForeignDirectoryPageCheckpoint(CheckpointBundle):
    def commit_snapshot(self, **kwargs):
        semantic_key = str(kwargs.get("semantic_key") or "")
        if semantic_key.startswith("cluster-triage:page:"):
            action_payload = copy.deepcopy(kwargs["action_payload"])
            page = next(
                event
                for event in action_payload["investigation_journal"]
                if event.get("kind") == "candidate_cluster_triage_page"
            )
            page["page_identity"] = "f" * 64
            commit = super().commit_snapshot(
                **{
                    **kwargs,
                    "action_payload": action_payload,
                }
            )
            raise _DirectoryPageCheckpointStop(
                "stop after persisting a foreign directory page"
            )
        return super().commit_snapshot(**kwargs)


def _resign_triage_event(event: dict) -> None:
    unsigned = {
        key: copy.deepcopy(value)
        for key, value in event.items()
        if key != "content_identity"
    }
    event["content_identity"] = hashlib.sha256(
        stable_json(
            {
                "schema": "{0}-content-identity/v1".format(
                    event["event_schema"]
                ),
                "facts": unsigned,
            }
        ).encode("utf-8")
    ).hexdigest()


def _all_unselected_triage_judgment(
    page: ClusterTriagePageRequest,
):
    return parse_cluster_triage_judgment(
        {
            "schema": "candidate-cluster-triage-page-response/v1",
            "page_identity": page.page_identity,
            "request_identity": page.request_identity,
            "partition_identity": page.partition_identity,
            "page_index": page.page_index,
            "page_count": page.page_count,
            "decisions": [
                {
                    "cluster_id": cluster["cluster_id"],
                    "disposition": "unselected",
                    "rationale": (
                        "The cluster predates the active failure window."
                    ),
                    "evidence_refs": [cluster["evidence_refs"][0]],
                    "mismatch_evidence": [
                        {
                            "evidence_ref": cluster["evidence_refs"][0],
                            "mismatch": (
                                "The trace-grounded action predates the "
                                "active failure window."
                            ),
                        }
                    ],
                }
                for cluster in page.clusters
            ],
        },
        page=page,
    )


class _ResignedDirectoryPageConflictCheckpoint(CheckpointBundle):
    def __init__(self, root: Path, mutation: str) -> None:
        super().__init__(root)
        self.mutation = mutation

    def commit_snapshot(self, **kwargs):
        semantic_key = str(kwargs.get("semantic_key") or "")
        if semantic_key.startswith("cluster-triage:page:"):
            action_payload = copy.deepcopy(kwargs["action_payload"])
            event = next(
                item
                for item in action_payload["investigation_journal"]
                if item.get("kind") == "candidate_cluster_triage_page"
            )
            if self.mutation == "judgment":
                page = ClusterTriagePageRequest.from_dict(
                    event["page_request"]
                )
                judgment = _all_unselected_triage_judgment(page)
                event["judgment"] = judgment.to_dict()
                event["judgment_identity"] = judgment.judgment_identity
            elif self.mutation == "physical_count":
                event["physical_request_delta"] = 0
            else:
                raise AssertionError("unsupported page conflict mutation")
            _resign_triage_event(event)
            commit = super().commit_snapshot(
                **{
                    **kwargs,
                    "action_payload": action_payload,
                }
            )
            raise _DirectoryPageCheckpointStop(
                "stop after re-signing a conflicting directory page"
            )
        return super().commit_snapshot(**kwargs)


class _StopAfterDirectoryPageTerminalCheckpoint(CheckpointBundle):
    def record_action(self, operation, semantic_key, payload):
        record = super().record_action(operation, semantic_key, payload)
        if operation == "candidate_cluster_triage_page_completed":
            raise _DirectoryPageCheckpointStop(
                "stop after directory page terminal action"
            )
        return record


class _StopAfterMalformedDirectoryPageTerminalCheckpoint(CheckpointBundle):
    def __init__(self, root: Path, mutation: str) -> None:
        super().__init__(root)
        self.mutation = mutation

    def record_action(self, operation, semantic_key, payload):
        if operation == "candidate_cluster_triage_page_completed":
            payload = copy.deepcopy(payload)
            if self.mutation == "missing_judgment":
                payload.pop("judgment")
            elif self.mutation == "conflicting_judgment_identity":
                payload["judgment_identity"] = "f" * 64
            else:
                raise AssertionError("unsupported terminal mutation")
            record = super().record_action(operation, semantic_key, payload)
            raise _DirectoryPageCheckpointStop(
                "stop after malformed directory page terminal action"
            )
        return super().record_action(operation, semantic_key, payload)


class GlobalPaginationIntegrationTest(unittest.TestCase):
    @staticmethod
    def _resign_planned_request_diagnostic(
        diagnostic: dict,
        *,
        context_budget: dict,
    ) -> None:
        request = global_candidate_request_from_validation_envelope(
            diagnostic["validation_envelope"]
        )
        diagnostic["request_identity"] = "global_request:v1:{0}".format(
            hashlib.sha256(
                stable_json(request.validation_envelope()).encode("utf-8")
            ).hexdigest()
        )
        diagnostic["projection"] = request.judge_prompt_projection()[
            "prompt_projection"
        ]
        diagnostic["measurement"] = JudgeContextBudget(
            context_window_tokens=context_budget["context_window_tokens"],
            max_output_tokens=context_budget["max_output_tokens"],
            safety_margin_tokens=context_budget["safety_margin_tokens"],
        ).measure(
            system=GLOBAL_CANDIDATE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": build_global_candidate_prompt(request),
                }
            ],
        ).to_dict()

    @staticmethod
    def _checkpoint_config(
        trace: dict,
        *,
        fusion_mode: str = "retrieval-global",
        max_judge_requests: int = 16,
        analysis_perspective: str = "",
    ) -> dict:
        return build_checkpoint_config(
            trace=trace,
            case_id=trace["case_id"],
            objective=(
                "Find the authored decision that introduced the defect."
            ),
            analysis_perspective=analysis_perspective,
            start_refs=["record:observed-defect"],
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 64,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": max_judge_requests,
            },
            model_identity="offline:paging-test",
            cache_identity="cache:paging-test",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://paging-test",
                "provider_error_threshold": 3,
                "fusion_mode": fusion_mode,
            },
        )

    def test_twenty_five_candidates_use_bounded_pages_and_union_no_defect(self):
        judge = _NoDefectPagingJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [8, 8, 8, 1],
        )
        self.assertTrue(
            all(len(request.capsules) <= 8 for request in judge.global_requests)
        )
        self.assertEqual(report.analysis_outcome, "no_defect")
        self.assertEqual(
            report.metadata["global_judge_physical_request_count"],
            4,
        )
        plan_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
        ]
        page_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(
            [event["status"] for event in page_events],
            [
                "completed",
                "completed",
                "completed",
                "completed",
            ],
        )
        self.assertEqual(
            [event["candidate_count"] for event in page_events],
            [8, 8, 8, 1],
        )

    def test_global_pages_split_before_transport_when_prompt_budget_is_tighter(self):
        judge = _BudgetedNoDefectPagingJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        page_sizes = [len(request.capsules) for request in judge.global_requests]
        self.assertEqual(page_sizes, [4, 4, 4, 4, 4, 4, 1])
        self.assertEqual(sum(page_sizes), 25)
        self.assertTrue(
            all(
                judge.context_budget.measure(
                    system=GLOBAL_CANDIDATE_SYSTEM_PROMPT,
                    messages=[
                        {
                            "role": "user",
                            "content": build_global_candidate_prompt(request),
                        }
                    ],
                ).fits
                for request in judge.global_requests
            )
        )
        plan_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
        ]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(
            [len(page["candidate_refs"]) for page in plan_events[0]["plan"]["pages"]],
            page_sizes,
        )
        planning = plan_events[0]["planning_diagnostics"]
        self.assertTrue(planning["split_history"])
        self.assertTrue(
            all(page["measurement"]["fits"] for page in planning["pages"])
        )

    def test_single_candidate_overflow_fails_locally_without_provider_request(self):
        judge = _TinyBudgetNoDefectPagingJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace(candidate_count=9)),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        self.assertEqual(judge.request_count, 0)
        self.assertEqual(judge.global_requests, [])
        page_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        self.assertEqual(len(page_events), 1)
        self.assertEqual(page_events[0]["status"], "failed")
        self.assertEqual(
            page_events[0]["blocker"],
            "global_judge_context_budget_exceeded",
        )
        self.assertEqual(page_events[0]["physical_request_delta"], 0)
        self.assertIs(page_events[0]["physical_request_exact"], True)
        plan_event = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
        )
        measurement = plan_event["planning_diagnostics"]["pages"][0][
            "measurement"
        ]
        self.assertIs(measurement["fits"], False)
        self.assertGreater(
            measurement["estimated_input_tokens"],
            measurement["max_input_tokens"],
        )
        self.assertEqual(report.analysis_outcome, "execution_failed")
        self.assertEqual(report.seed_results[0].outcome, "execution_failed")
        self.assertEqual(report.seed_results[0].missing_evidence, ())
        self.assertEqual(
            report.seed_results[0].execution_failures[0]["reason"],
            "context_window_exceeded",
        )
        self.assertEqual(
            report.metadata["termination_reason"],
            "analysis_execution_failed",
        )
        self.assertEqual(
            report.metadata["analysis_execution_failures"][0][
                "physical_requests"
            ],
            0,
        )

    def test_cluster_triage_expands_originals_before_strict_pagination(self):
        judge = _SelectiveClusterTriageJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=32,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(clustered_paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        self.assertEqual(len(judge.directory_requests), 1)
        strict_refs = tuple(
            capsule.candidate_ref
            for request in judge.global_requests
            for capsule in request.capsules
        )
        self.assertEqual(set(strict_refs), judge.selected_refs)
        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [8, 2],
        )
        self.assertIn("record:decision-024", strict_refs)
        self.assertTrue(
            {
                "record:decision-{0:03d}".format(index)
                for index in range(15)
            }.isdisjoint(strict_refs)
        )
        triage_events = {
            event["kind"]
            for event in report.investigation_journal
            if str(event.get("kind") or "").startswith(
                "candidate_cluster_triage"
            )
            or event.get("kind") == "candidate_cluster_expansion"
        }
        self.assertEqual(
            triage_events,
            {
                "candidate_cluster_triage_plan",
                "candidate_cluster_triage_page",
                "candidate_cluster_triage_result",
                "candidate_cluster_expansion",
            },
        )
        metrics = report.metadata["candidate_cluster_triage_metrics"]
        self.assertEqual(metrics["offered_original_candidate_count"], 25)
        self.assertEqual(metrics["cluster_count"], 5)
        self.assertEqual(metrics["directory_logical_page_count"], 1)
        self.assertEqual(metrics["directory_physical_request_count"], 1)
        self.assertEqual(metrics["expanded_original_candidate_count"], 10)
        self.assertEqual(metrics["strict_initial_logical_page_count"], 2)
        self.assertEqual(metrics["strict_physical_request_count"], 2)
        self.assertEqual(metrics["total_physical_request_count"], 3)
        self.assertEqual(
            report.metadata["global_judge_physical_request_count"],
            3,
        )
        self.assertEqual(report.metadata["physical_judge_request_count"], 3)

    def test_public_report_validator_uses_directory_plus_strict_global_count(self):
        trace = clustered_paginated_trace()
        graph = TraceGraph.from_trace(trace)
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "count-validation.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=_SelectiveClusterTriageJudge(),
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )
            actions = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            ).actions

        self.assertEqual(
            report.metadata["global_judge_physical_request_count"],
            3,
        )
        validate_recursive_report_against_graph(
            graph,
            report,
            label="Stage C directory-plus-strict report",
            action_records=actions,
        )

    def test_public_validator_rejects_completed_directory_facts_without_actions(self):
        graph = TraceGraph.from_trace(clustered_paginated_trace())
        report = AgenticRecursiveAnalyzer(
            judge=_SelectiveClusterTriageJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=32,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="task quality",
        )

        with self.assertRaisesRegex(
            ValueError,
            "completed directory facts are unverifiable without action records",
        ):
            validate_recursive_report_against_graph(
                graph,
                report,
                label="directory report without external actions",
            )

    def test_public_validator_rejects_resigned_directory_page_action_conflict(self):
        trace = clustered_paginated_trace()
        graph = TraceGraph.from_trace(trace)
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "public-validation.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=_SelectiveClusterTriageJudge(),
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )
            actions = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            ).actions

        validate_recursive_report_against_graph(
            graph,
            report,
            label="completed directory page report",
            action_records=actions,
        )
        payload = report.to_dict()
        page_event = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "candidate_cluster_triage_page"
        )
        page = ClusterTriagePageRequest.from_dict(page_event["page_request"])
        judgment = _all_unselected_triage_judgment(page)
        page_event["judgment"] = judgment.to_dict()
        page_event["judgment_identity"] = judgment.judgment_identity
        payload = RecursiveAttributionReport.from_dict(payload).to_dict()
        page_event = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "candidate_cluster_triage_page"
        )
        _resign_triage_event(page_event)
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "directory page journal contradicts durable terminal action",
        ):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="re-signed directory page report",
                action_records=actions,
            )

    def test_complete_cluster_triage_checkpoint_replays_without_directory_calls(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "cluster-complete.checkpoint"
            checkpoint = _StopAfterClusterExpansionCheckpoint(
                checkpoint_root
            )
            first_judge = _SelectiveClusterTriageJudge()
            first = AgenticRecursiveAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=checkpoint,
                checkpoint_config=config,
                stop_requested=lambda: (
                    checkpoint.cluster_expansion_persisted
                ),
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )
            resumed_judge = _SelectiveClusterTriageJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )

        self.assertEqual(len(first_judge.directory_requests), 1)
        self.assertEqual(first_judge.global_requests, [])
        self.assertEqual(resumed_judge.directory_requests, [])
        self.assertEqual(
            [len(request.capsules) for request in resumed_judge.global_requests],
            [8, 2],
        )
        self.assertEqual(first.metadata["global_judge_physical_request_count"], 1)
        self.assertEqual(resumed.metadata["global_judge_physical_request_count"], 3)

    def test_incomplete_cluster_triage_checkpoint_resumes_only_unfinished_page(self):
        trace = clustered_paginated_trace(45)
        config = self._checkpoint_config(
            trace,
            max_judge_requests=48,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "cluster-incomplete.checkpoint"
            first_judge = _SelectiveClusterTriageJudge()
            with self.assertRaises(_DirectoryPageCheckpointStop):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=48,
                    max_hypotheses=64,
                    checkpoint=_StopAfterFirstDirectoryPageCheckpoint(
                        checkpoint_root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )
            resumed_judge = _SelectiveClusterTriageJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=48,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )

        self.assertEqual(
            [page.page_index for page in first_judge.directory_requests],
            [0],
        )
        self.assertEqual(
            [page.page_index for page in resumed_judge.directory_requests],
            [1],
        )
        self.assertEqual(
            [len(request.capsules) for request in resumed_judge.global_requests],
            [8, 2],
        )
        self.assertEqual(
            resumed.metadata["candidate_cluster_triage_metrics"][
                "directory_physical_request_count"
            ],
            2,
        )

    def test_started_directory_page_is_not_replayed_without_exact_terminal(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "cluster-started.checkpoint"
            first_judge = _SelectiveClusterTriageJudge()
            with self.assertRaises(_DirectoryPageCheckpointStop):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    checkpoint=_StopAfterDirectoryPageStartedCheckpoint(
                        checkpoint_root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )
            resumed_judge = _SelectiveClusterTriageJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )

        self.assertEqual(first_judge.directory_requests, [])
        self.assertEqual(resumed_judge.directory_requests, [])
        self.assertEqual(
            [len(request.capsules) for request in resumed_judge.global_requests],
            [8, 8, 8, 1],
        )
        page = next(
            event
            for event in resumed.investigation_journal
            if event.get("kind") == "candidate_cluster_triage_page"
        )
        self.assertEqual(page["status"], "failed")
        self.assertFalse(page["physical_request_exact"])
        self.assertEqual(page["physical_request_delta"], 6)
        self.assertEqual(
            page["blocker"],
            "candidate_cluster_triage_interrupted",
        )

    def test_foreign_directory_page_checkpoint_falls_back_without_replay(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "cluster-foreign.checkpoint"
            first_judge = _SelectiveClusterTriageJudge()
            with self.assertRaises(_DirectoryPageCheckpointStop):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    checkpoint=_ForeignDirectoryPageCheckpoint(
                        checkpoint_root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )
            resumed_judge = _SelectiveClusterTriageJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )

        self.assertEqual(len(first_judge.directory_requests), 1)
        self.assertEqual(resumed_judge.directory_requests, [])
        self.assertEqual(
            [len(request.capsules) for request in resumed_judge.global_requests],
            [8, 8, 8, 1],
        )
        self.assertIn(
            "directory_page_set_invalid",
            resumed.metadata["candidate_cluster_triage_metrics"][
                "fallback_reason"
            ],
        )

    def test_resigned_page_cannot_override_completed_terminal_judgment_or_count(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        for mutation in ("judgment", "physical_count"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tempdir:
                checkpoint_root = Path(tempdir) / "cluster-conflict.checkpoint"
                with self.assertRaises(_DirectoryPageCheckpointStop):
                    AgenticRecursiveAnalyzer(
                        judge=_SelectiveClusterTriageJudge(),
                        fusion_mode="retrieval-global",
                        max_judge_requests=32,
                        max_hypotheses=64,
                        checkpoint=_ResignedDirectoryPageConflictCheckpoint(
                            checkpoint_root,
                            mutation,
                        ),
                        checkpoint_config=config,
                    ).analyze(
                        TraceGraph.from_trace(trace),
                        start_refs=["record:observed-defect"],
                        objective=(
                            "Find the authored decision that introduced the defect."
                        ),
                        analysis_perspective="task quality",
                    )
                judge = _SelectiveClusterTriageJudge()
                report = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    checkpoint=CheckpointBundle(checkpoint_root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )

                self.assertEqual(judge.directory_requests, [])
                self.assertEqual(
                    [len(request.capsules) for request in judge.global_requests],
                    [8, 8, 8, 1],
                )
                metrics = report.metadata[
                    "candidate_cluster_triage_metrics"
                ]
                self.assertEqual(
                    metrics["fallback_reason"],
                    "directory_page_terminal_conflict",
                )
                self.assertEqual(
                    metrics["directory_physical_request_count"],
                    1,
                )
                self.assertEqual(metrics["total_physical_request_count"], 5)

    def test_uncertain_cluster_expands_into_single_strict_page_metrics(self):
        judge = _UncertainClusterTriageJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=32,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(clustered_paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="task quality",
        )

        strict_refs = tuple(
            capsule.candidate_ref
            for capsule in judge.global_requests[0].capsules
        )
        self.assertEqual(set(strict_refs), judge.uncertain_refs)
        metrics = report.metadata["candidate_cluster_triage_metrics"]
        self.assertEqual(metrics["expanded_original_candidate_count"], 5)
        self.assertEqual(metrics["strict_initial_logical_page_count"], 1)
        self.assertEqual(metrics["strict_physical_request_count"], 1)
        self.assertEqual(metrics["total_physical_request_count"], 2)
        self.assertEqual(report.metadata["global_judge_physical_request_count"], 2)

    def test_complete_unselected_coverage_skips_strict_judgment_without_seed_failure(self):
        judge = _AllUnselectedClusterTriageJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=32,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(clustered_paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="task quality",
        )

        self.assertEqual(len(judge.directory_requests), 1)
        self.assertEqual(judge.global_requests, [])
        self.assertFalse(
            any(
                event.get("kind") == "global_candidate_pass"
                and event.get("status") == "failed"
                for event in report.investigation_journal
            )
        )
        gate = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_gate"
            and event.get("reason") == "cluster_triage_no_expanded_candidates"
        )
        self.assertEqual(gate["fallback"], "recursive_backward_taint")
        metrics = report.metadata["candidate_cluster_triage_metrics"]
        self.assertEqual(metrics["expanded_original_candidate_count"], 0)
        self.assertEqual(metrics["strict_initial_logical_page_count"], 0)
        self.assertEqual(metrics["strict_physical_request_count"], 0)
        self.assertEqual(metrics["total_physical_request_count"], 1)

    def test_invalid_manifest_authority_disables_directory_and_restores_full_paging(self):
        for mutation, expected_reason in (
            ("missing", "manifest_missing"),
            ("duplicate", "manifest_duplicate_or_conflicting"),
            ("conflicting", "manifest_or_request_validation_failed:ValueError"),
            ("stale_seed", "manifest_or_request_validation_failed:ValueError"),
            ("stale_defect", "manifest_or_request_validation_failed:ValueError"),
        ):
            with self.subTest(mutation=mutation):
                judge = _SelectiveClusterTriageJudge()
                report = _ManifestAuthorityMutationAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    manifest_mutation=mutation,
                ).analyze(
                    TraceGraph.from_trace(clustered_paginated_trace()),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )

                self.assertEqual(judge.directory_requests, [])
                self.assertEqual(
                    [
                        len(request.capsules)
                        for request in judge.global_requests
                    ],
                    [8, 8, 8, 1],
                )
                metrics = report.metadata[
                    "candidate_cluster_triage_metrics"
                ]
                self.assertEqual(metrics["fallback_reason"], expected_reason)
                self.assertEqual(
                    metrics["offered_original_candidate_count"],
                    25,
                )
                self.assertEqual(metrics["total_physical_request_count"], 4)
                if mutation in {"stale_seed", "stale_defect"}:
                    self.assertFalse(
                        any(
                            event.get("kind")
                            == "candidate_cluster_manifest_shadow"
                            for event in report.investigation_journal
                        )
                    )

    def test_stale_manifest_is_quarantined_before_pool_determinism_check(self):
        for mutation, expected_reason in (
            ("stale_seed", "stale_seed_ref"),
            ("stale_defect", "stale_defect_fingerprint"),
        ):
            for checkpoint_enabled in (False, True):
                with self.subTest(
                    mutation=mutation,
                    checkpoint_enabled=checkpoint_enabled,
                ):
                    trace = clustered_paginated_trace()
                    graph = TraceGraph.from_trace(trace)
                    config = self._checkpoint_config(
                        trace,
                        max_judge_requests=32,
                        analysis_perspective="task quality",
                    )
                    with tempfile.TemporaryDirectory() as tempdir:
                        checkpoint_root = Path(tempdir) / "stale.checkpoint"
                        if checkpoint_enabled:
                            with self.assertRaises(
                                _DirectoryPageCheckpointStop
                            ):
                                AgenticRecursiveAnalyzer(
                                    judge=_SelectiveClusterTriageJudge(),
                                    fusion_mode="retrieval-global",
                                    max_judge_requests=32,
                                    max_hypotheses=64,
                                    checkpoint=(
                                        _StaleManifestAtPlanCheckpoint(
                                            checkpoint_root,
                                            mutation,
                                        )
                                    ),
                                    checkpoint_config=config,
                                ).analyze(
                                    graph,
                                    start_refs=["record:observed-defect"],
                                    objective=(
                                        "Find the authored decision that "
                                        "introduced the defect."
                                    ),
                                    analysis_perspective="task quality",
                                )
                            judge = _SelectiveClusterTriageJudge()
                            analyzer = AgenticRecursiveAnalyzer(
                                judge=judge,
                                fusion_mode="retrieval-global",
                                max_judge_requests=32,
                                max_hypotheses=64,
                                checkpoint=CheckpointBundle(checkpoint_root),
                                checkpoint_config=config,
                            )
                        else:
                            judge = _SelectiveClusterTriageJudge()
                            analyzer = _PrePoolStaleManifestAnalyzer(
                                judge=judge,
                                fusion_mode="retrieval-global",
                                max_judge_requests=32,
                                max_hypotheses=64,
                                manifest_mutation=mutation,
                            )
                        try:
                            report = analyzer.analyze(
                                graph,
                                start_refs=["record:observed-defect"],
                                objective=(
                                    "Find the authored decision that introduced "
                                    "the defect."
                                ),
                                analysis_perspective="task quality",
                            )
                        except ValueError as error:
                            self.fail(
                                "stale manifest escaped pre-pool quarantine: "
                                "{0}".format(error)
                            )

                        self.assertEqual(judge.directory_requests, [])
                        self.assertEqual(
                            [
                                len(request.capsules)
                                for request in judge.global_requests
                            ],
                            [8, 8, 8, 1],
                        )
                        self.assertEqual(report.analysis_outcome, "no_defect")
                        self.assertFalse(
                            any(
                                event.get("kind")
                                == "candidate_cluster_manifest_shadow"
                                for event in report.investigation_journal
                            )
                        )
                        rejections = [
                            event
                            for event in report.investigation_journal
                            if event.get("kind")
                            == "candidate_cluster_manifest_rejection"
                        ]
                        self.assertEqual(len(rejections), 1)
                        self.assertEqual(
                            rejections[0]["reason"],
                            expected_reason,
                        )
                        self.assertEqual(
                            report.metadata[
                                "candidate_cluster_triage_metrics"
                            ]["fallback_reason"],
                            "manifest_missing",
                        )
                        if checkpoint_enabled:
                            restored = CheckpointBundle(
                                checkpoint_root
                            ).restore(expected_config=config)
                            validate_recursive_report_against_graph(
                                graph,
                                report,
                                label="checkpoint stale-manifest fallback",
                                action_records=restored.actions,
                            )
                        else:
                            validate_recursive_report_against_graph(
                                graph,
                                report,
                                label="stale-manifest fallback",
                            )

    def test_stale_source_drift_suppression_is_seed_run_scoped(self):
        for checkpoint_enabled in (False, True):
            with self.subTest(checkpoint_enabled=checkpoint_enabled):
                trace = clustered_paginated_trace()
                graph = TraceGraph.from_trace(trace)
                config = self._checkpoint_config(
                    trace,
                    max_judge_requests=32,
                    analysis_perspective="task quality",
                )
                with tempfile.TemporaryDirectory() as tempdir:
                    checkpoint_root = Path(tempdir) / "stale-source.checkpoint"
                    if checkpoint_enabled:
                        with self.assertRaises(_DirectoryPageCheckpointStop):
                            AgenticRecursiveAnalyzer(
                                judge=_SelectiveClusterTriageJudge(),
                                fusion_mode="retrieval-global",
                                max_judge_requests=32,
                                max_hypotheses=64,
                                checkpoint=_StaleManifestAtPlanCheckpoint(
                                    checkpoint_root,
                                    "stale_defect",
                                    source_selection_drift=True,
                                ),
                                checkpoint_config=config,
                            ).analyze(
                                graph,
                                start_refs=["record:observed-defect"],
                                objective=(
                                    "Find the authored decision that introduced "
                                    "the defect."
                                ),
                                analysis_perspective="task quality",
                            )
                        judge = _SelectiveClusterTriageJudge()
                        analyzer = AgenticRecursiveAnalyzer(
                            judge=judge,
                            fusion_mode="retrieval-global",
                            max_judge_requests=32,
                            max_hypotheses=64,
                            checkpoint=CheckpointBundle(checkpoint_root),
                            checkpoint_config=config,
                        )
                    else:
                        judge = _SelectiveClusterTriageJudge()
                        analyzer = _PrePoolStaleManifestAnalyzer(
                            judge=judge,
                            fusion_mode="retrieval-global",
                            max_judge_requests=32,
                            max_hypotheses=64,
                            manifest_mutation="stale_defect",
                            source_selection_drift=True,
                        )
                    report = analyzer.analyze(
                        graph,
                        start_refs=["record:observed-defect"],
                        objective=(
                            "Find the authored decision that introduced the "
                            "defect."
                        ),
                        analysis_perspective="task quality",
                    )

                    self.assertEqual(judge.directory_requests, [])
                    self.assertEqual(
                        [
                            len(request.capsules)
                            for request in judge.global_requests
                        ],
                        [8, 8, 8, 1],
                    )
                    self.assertFalse(
                        any(
                            event.get("kind")
                            == "candidate_cluster_manifest_shadow"
                            for event in report.investigation_journal
                        )
                    )
                    rejection = next(
                        event
                        for event in report.investigation_journal
                        if event.get("kind")
                        == "candidate_cluster_manifest_rejection"
                    )
                    self.assertEqual(
                        rejection["source_selection_identity"],
                        "f" * 64,
                    )
                    self.assertEqual(
                        rejection["seed_binding_identity"],
                        report.seed_results[0].seed_binding_identity,
                    )
                    if checkpoint_enabled:
                        actions = CheckpointBundle(checkpoint_root).restore(
                            expected_config=config
                        ).actions
                        validate_recursive_report_against_graph(
                            graph,
                            report,
                            label="checkpoint source-drift stale fallback",
                            action_records=actions,
                        )
                    else:
                        validate_recursive_report_against_graph(
                            graph,
                            report,
                            label="source-drift stale fallback",
                        )

    def test_large_synthetic_cluster_fixture_has_bounded_auditable_pages(self):
        trace = clustered_paginated_trace(125)
        trace["case_id"] = "large-synthetic-stage-c-fixture"
        judge = _LargeSyntheticClusterTriageJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=128,
            max_hypotheses=128,
        ).analyze(
            TraceGraph.from_trace(trace),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="task quality",
        )

        strict_refs = {
            capsule.candidate_ref
            for request in judge.global_requests
            for capsule in request.capsules
        }
        self.assertTrue(judge.selected_refs.issubset(strict_refs))
        metrics = report.metadata["candidate_cluster_triage_metrics"]
        self.assertLessEqual(metrics["strict_initial_logical_page_count"], 16)
        self.assertEqual(
            metrics["directory_physical_request_count"],
            len(judge.directory_requests),
        )
        self.assertEqual(
            metrics["total_physical_request_count"],
            metrics["directory_physical_request_count"]
            + metrics["strict_physical_request_count"],
        )

    def test_directory_budget_and_provider_failures_restore_full_strict_paging(self):
        cases = (
            ("budget", 0, False, 4, 0, 4),
            ("provider", 1, False, 32, 1, 5),
            ("interrupted", 0, True, 32, 1, 10),
        )
        for (
            label,
            physical,
            untyped,
            budget,
            expected_directory_requests,
            expected_total,
        ) in cases:
            with self.subTest(label=label):
                judge = _FailedClusterTriageJudge(
                    physical_requests=physical,
                    untyped=untyped,
                )
                report = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=budget,
                    max_hypotheses=64,
                ).analyze(
                    TraceGraph.from_trace(clustered_paginated_trace()),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )

                self.assertEqual(
                    len(judge.directory_requests),
                    expected_directory_requests,
                )
                self.assertEqual(
                    [
                        len(request.capsules)
                        for request in judge.global_requests
                    ],
                    [8, 8, 8, 1],
                )
                self.assertEqual(report.analysis_outcome, "no_defect")
                metrics = report.metadata[
                    "candidate_cluster_triage_metrics"
                ]
                self.assertTrue(metrics["fallback_reason"])
                self.assertEqual(
                    metrics["expanded_original_candidate_count"],
                    25,
                )
                self.assertEqual(
                    metrics["total_physical_request_count"],
                    expected_total,
                )
                self.assertEqual(
                    report.metadata["global_judge_physical_request_count"],
                    expected_total,
                )
                self.assertEqual(
                    report.metadata["physical_judge_request_count"],
                    expected_total,
                )

    def test_directory_budget_is_optional_to_full_original_strict_paging(self):
        for budget, expected_outcome, expected_strict_requests in (
            (4, "no_defect", 4),
            (3, "partial", 3),
        ):
            with self.subTest(budget=budget):
                judge = _FailedClusterTriageJudge(physical_requests=1)
                report = AgenticRecursiveAnalyzer(
                    judge=judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=budget,
                    max_hypotheses=64,
                ).analyze(
                    TraceGraph.from_trace(clustered_paginated_trace()),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )

                self.assertEqual(judge.directory_requests, [])
                self.assertEqual(
                    len(judge.global_requests),
                    expected_strict_requests,
                )
                self.assertEqual(report.analysis_outcome, expected_outcome)
                metrics = report.metadata[
                    "candidate_cluster_triage_metrics"
                ]
                self.assertEqual(
                    metrics["fallback_reason"],
                    "directory_optional_budget_unavailable",
                )
                self.assertEqual(
                    metrics["directory_physical_request_count"],
                    0,
                )
                self.assertEqual(
                    metrics["strict_physical_request_count"],
                    expected_strict_requests,
                )
                if budget == 3:
                    failed_page = next(
                        event
                        for event in report.investigation_journal
                        if event.get("kind") == "global_candidate_page"
                        and event.get("status") == "failed"
                    )
                    self.assertEqual(
                        failed_page["blocker"],
                        "judge_request_budget_exhausted",
                    )

    def test_directory_reserves_strict_page_maximum_physical_allowance(self):
        judge = _TwoPhysicalStrictClusterTriageJudge()
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=8,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(clustered_paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="task quality",
        )

        self.assertEqual(judge.directory_requests, [])
        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [8, 8, 8, 1],
        )
        self.assertEqual(judge.strict_allowances, [6, 6, 4, 2])
        self.assertEqual(report.analysis_outcome, "no_defect")
        metrics = report.metadata["candidate_cluster_triage_metrics"]
        self.assertEqual(metrics["directory_physical_request_count"], 0)
        self.assertEqual(metrics["strict_physical_request_count"], 8)
        self.assertEqual(metrics["total_physical_request_count"], 8)
        self.assertEqual(
            metrics["fallback_reason"],
            "directory_optional_budget_unavailable",
        )

    def test_completed_directory_action_replays_without_provider_or_page_snapshot(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "cluster-terminal.checkpoint"
            first_judge = _SelectiveClusterTriageJudge()
            with self.assertRaises(_DirectoryPageCheckpointStop):
                AgenticRecursiveAnalyzer(
                    judge=first_judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    checkpoint=_StopAfterDirectoryPageTerminalCheckpoint(
                        checkpoint_root
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )
            resumed_judge = _SelectiveClusterTriageJudge()
            resumed = AgenticRecursiveAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=32,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="task quality",
            )

        self.assertEqual(len(first_judge.directory_requests), 1)
        self.assertEqual(resumed_judge.directory_requests, [])
        self.assertEqual(
            [len(request.capsules) for request in resumed_judge.global_requests],
            [8, 2],
        )
        self.assertEqual(
            resumed.metadata["global_judge_physical_request_count"],
            3,
        )

    def test_partial_directory_terminal_fails_closed_without_provider_replay(self):
        trace = clustered_paginated_trace()
        config = self._checkpoint_config(
            trace,
            max_judge_requests=32,
            analysis_perspective="task quality",
        )
        for mutation in (
            "missing_judgment",
            "conflicting_judgment_identity",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tempdir:
                checkpoint_root = Path(tempdir) / "cluster-partial.checkpoint"
                with self.assertRaises(_DirectoryPageCheckpointStop):
                    AgenticRecursiveAnalyzer(
                        judge=_SelectiveClusterTriageJudge(),
                        fusion_mode="retrieval-global",
                        max_judge_requests=32,
                        max_hypotheses=64,
                        checkpoint=_StopAfterMalformedDirectoryPageTerminalCheckpoint(
                            checkpoint_root,
                            mutation,
                        ),
                        checkpoint_config=config,
                    ).analyze(
                        TraceGraph.from_trace(trace),
                        start_refs=["record:observed-defect"],
                        objective=(
                            "Find the authored decision that introduced the defect."
                        ),
                        analysis_perspective="task quality",
                    )
                resumed_judge = _SelectiveClusterTriageJudge()
                resumed = AgenticRecursiveAnalyzer(
                    judge=resumed_judge,
                    fusion_mode="retrieval-global",
                    max_judge_requests=32,
                    max_hypotheses=64,
                    checkpoint=CheckpointBundle(checkpoint_root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="task quality",
                )

                self.assertEqual(resumed_judge.directory_requests, [])
                self.assertEqual(
                    [
                        len(request.capsules)
                        for request in resumed_judge.global_requests
                    ],
                    [8, 8, 8, 1],
                )
                metrics = resumed.metadata[
                    "candidate_cluster_triage_metrics"
                ]
                self.assertEqual(
                    metrics["fallback_reason"],
                    "directory_terminal_replay_invalid",
                )
                self.assertEqual(
                    metrics["directory_physical_request_count"],
                    1,
                )
                self.assertEqual(
                    resumed.metadata["judge_request_uncertainty_count"],
                    0,
                )

    def test_successful_pages_remain_published_after_a_402_stops_later_pages(self):
        judge = _PaymentFailureAfterFirstPageJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        page_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        page_one_judgment = page_events[0]["judgment"]
        self.assertEqual([event["status"] for event in page_events], ["completed", "failed"])
        self.assertEqual(len(judge.global_requests), 2)
        self.assertEqual(report.metadata["global_judge_physical_request_count"], 2)
        self.assertEqual(len(report.metadata["global_candidate_judgments"]), 1)
        published_page_one = dict(report.metadata["global_candidate_judgments"][0])
        self.assertIn("owner", published_page_one)
        published_page_one.pop("owner")
        self.assertEqual(published_page_one, page_one_judgment)
        self.assertEqual(report.analysis_outcome, "partial")
        self.assertTrue(report.metadata["provider_circuit"]["open"])
        self.assertEqual(
            report.metadata["unresolved_page_refs"],
            tuple(
                page["identity"]
                for plan in report.investigation_journal
                if plan.get("kind") == "global_candidate_page_plan"
                for page in plan["plan"]["pages"]
                if page["identity"] != page_events[0]["page_identity"]
            ),
        )

    def test_resume_retries_only_the_failed_page_after_balance_recovers(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "balance.checkpoint"
            first_judge = _PaymentFailureAfterFirstPageJudge()
            first = _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            recovered_judge = _NoDefectPagingJudge()
            resumed = _FixedPoolAnalyzer(
                judge=recovered_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            restored = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )

        self.assertEqual(first.analysis_outcome, "partial")
        self.assertEqual(len(first_judge.global_requests), 2)
        self.assertEqual(
            [len(request.capsules) for request in recovered_judge.global_requests],
            [8, 8, 1],
        )
        self.assertEqual(resumed.analysis_outcome, "no_defect")
        self.assertEqual(resumed.metadata["unresolved_page_refs"], ())
        page_events = [
            event
            for event in resumed.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        self.assertEqual(len(page_events), 5)
        self.assertEqual(
            [event["status"] for event in page_events],
            ["completed", "failed", "completed", "completed", "completed"],
        )
        self.assertTrue(all("owner" in event for event in page_events))
        terminal_actions = [
            record
            for record in restored.actions
            if record["operation"]
            in {"global_judge_page_completed", "global_judge_page_failed"}
        ]
        self.assertEqual(len(terminal_actions), len(page_events))
        self.assertEqual(
            resumed.metadata["global_judge_physical_request_count"],
            sum(
                record["payload"]["physical_request_delta"]
                for record in terminal_actions
            ),
        )
        self.assertEqual(
            resumed.metadata["global_judge_physical_request_count"],
            5,
        )

    def test_zero_budget_page_has_one_durable_terminal_and_replays_idempotently(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace, max_judge_requests=0)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "zero-budget.checkpoint"
            first_judge = _NoDefectPagingJudge()
            first = _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=0,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            first_restored = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )
            resumed_judge = _NoDefectPagingJudge()
            resumed = _FixedPoolAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=0,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            resumed_restored = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )

        first_page_actions = [
            record
            for record in first_restored.actions
            if record["operation"].startswith("global_judge_page_")
        ]
        resumed_page_actions = [
            record
            for record in resumed_restored.actions
            if record["operation"].startswith("global_judge_page_")
        ]
        self.assertEqual(
            [record["operation"] for record in first_page_actions],
            ["global_judge_page_started", "global_judge_page_failed"],
        )
        self.assertEqual(resumed_page_actions, first_page_actions)
        terminal = first_page_actions[-1]["payload"]
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["physical_requests_reserved"], 0)
        self.assertEqual(terminal["physical_request_delta"], 0)
        self.assertTrue(terminal["physical_request_exact"])
        self.assertEqual(terminal["blocker"], "judge_request_budget_exhausted")
        page_events = [
            event
            for event in resumed.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        self.assertEqual(len(page_events), 1)
        self.assertEqual(page_events[0]["owner"], terminal["owner"])
        self.assertEqual(page_events[0]["physical_request_delta"], 0)
        self.assertEqual(first_judge.global_requests, [])
        self.assertEqual(resumed_judge.global_requests, [])
        self.assertEqual(first.metadata["global_judge_physical_request_count"], 0)
        self.assertEqual(resumed.metadata["global_judge_physical_request_count"], 0)

    def test_signal_after_final_plan_persists_zero_request_interrupted_terminal(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "final-signal.checkpoint"
            checkpoint = _StopAfterFinalPagePlanCheckpoint(checkpoint_root)
            judge = _LateRootPagingJudge()
            report = _FixedPoolAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=checkpoint,
                checkpoint_config=config,
                stop_requested=lambda: checkpoint.final_plan_persisted,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            restored = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )

        self.assertEqual(len(judge.global_requests), 4)
        final_plan = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
            and event.get("page_phase") == "final"
        )
        final_page = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page"
            and event.get("page_phase") == "final"
        )
        self.assertEqual(final_page["status"], "failed")
        self.assertEqual(final_page["blocker"], "global_judge_page_interrupted")
        self.assertEqual(final_page["physical_request_delta"], 0)
        self.assertTrue(final_page["physical_request_exact"])
        self.assertIn(
            final_page["page_identity"],
            report.metadata["unresolved_page_refs"],
        )
        convergence = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertEqual(convergence["status"], "interrupted")
        self.assertEqual(
            convergence["active_plan_identity"],
            final_plan["plan_identity"],
        )
        final_terminals = [
            record["payload"]
            for record in restored.actions
            if record["operation"] == "global_judge_page_failed"
            and record["payload"]["page_phase"] == "final"
        ]
        self.assertEqual(len(final_terminals), 1)
        self.assertEqual(final_terminals[0]["owner"], final_page["owner"])
        self.assertEqual(final_terminals[0]["physical_request_delta"], 0)
        self.assertTrue(final_terminals[0]["physical_request_exact"])

    def test_each_resume_gets_at_most_one_new_probe_when_402_persists(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "balance.checkpoint"
            first_judge = _PaymentFailureAfterFirstPageJudge()
            _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            still_unfunded = _ImmediatePaymentFailureJudge()
            resumed = _FixedPoolAnalyzer(
                judge=still_unfunded,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            restored = CheckpointBundle(checkpoint_root).restore(
                expected_config=config
            )

        self.assertEqual(len(first_judge.global_requests), 2)
        self.assertEqual(len(still_unfunded.global_requests), 1)
        self.assertEqual(resumed.analysis_outcome, "partial")
        self.assertTrue(resumed.metadata["provider_circuit"]["open"])
        self.assertEqual(
            sum(
                record["operation"] == "global_judge_page_failed"
                for record in restored.actions
            ),
            2,
        )

    def test_signal_after_plan_marks_only_active_plan_pages_unresolved(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint = _StopAfterPagePlanCheckpoint(
                Path(tempdir) / "signal.checkpoint"
            )
            judge = _NoDefectPagingJudge()
            report = _FixedPoolAnalyzer(
                judge=judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=checkpoint,
                checkpoint_config=config,
                stop_requested=lambda: checkpoint.page_plan_persisted,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )

        plan = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
        )
        convergence = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertEqual(judge.global_requests, [])
        self.assertEqual(report.metadata["termination_reason"], "signal_interrupted")
        self.assertEqual(convergence["status"], "interrupted")
        self.assertEqual(convergence["active_plan_identity"], plan["plan_identity"])
        self.assertEqual(
            report.metadata["unresolved_page_refs"],
            tuple(page["identity"] for page in plan["plan"]["pages"]),
        )
        self.assertIn(
            "global_candidate_pagination_interrupted",
            report.seed_results[0].blocking_reasons,
        )

    def test_checkpoint_resume_preserves_fusion_mode_and_rejects_mismatch_before_requests(self):
        trace = paginated_trace()
        retrieval_config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "fusion.checkpoint"
            first = _FixedPoolAnalyzer(
                judge=_NoDefectPagingJudge(),
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=retrieval_config,
                stop_requested=lambda: True,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective="Find the authored decision that introduced the defect.",
            )
            mismatch_judge = _NoDefectPagingJudge()
            with self.assertRaises(CheckpointCompatibilityError):
                _FixedPoolAnalyzer(
                    judge=mismatch_judge,
                    fusion_mode="off",
                    max_judge_requests=16,
                    max_hypotheses=64,
                    checkpoint=CheckpointBundle(checkpoint_root),
                    checkpoint_config=self._checkpoint_config(trace, fusion_mode="off"),
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective="Find the authored decision that introduced the defect.",
                )

        self.assertEqual(first.metadata["fusion_mode"], "retrieval-global")
        self.assertEqual(mismatch_judge.request_count, 0)

    def test_constructor_rejects_checkpoint_fusion_mismatch_before_analyze(self):
        trace = paginated_trace()
        judge = _NoDefectPagingJudge()

        with self.assertRaises(CheckpointCompatibilityError):
            _FixedPoolAnalyzer(
                judge=judge,
                fusion_mode="off",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint_config=self._checkpoint_config(trace),
            )

        self.assertEqual(judge.request_count, 0)

    def test_small_complete_chain_uses_one_global_request_without_paging(self):
        judge = _NoRootCandidatesPagingJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace(candidate_count=5)),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [5],
        )
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertFalse(
            any(
                event.get("kind") == "global_candidate_convergence"
                for event in report.investigation_journal
            )
        )

    def test_late_page_root_must_survive_into_a_separate_final_page(self):
        graph = TraceGraph.from_trace(paginated_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="",
            max_hypotheses=64,
        )
        judge = _LateRootPagingJudge()
        analyzer = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        )

        analyzer._run_global_candidate_prepass(state, graph)

        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [8, 8, 8, 1, 1],
        )
        self.assertEqual(
            [
                event["page_phase"]
                for event in state.investigation_journal
                if event.get("kind") == "global_candidate_page"
            ],
            [
                "initial",
                "initial",
                "initial",
                "initial",
                "final",
            ],
        )
        self.assertEqual(len(state.confirmation_queue), 1)
        self.assertEqual(
            state.confirmation_queue[0]["candidate_ref"],
            _LateRootPagingJudge.target_ref,
        )
        terminal_pass = next(
            event
            for event in state.investigation_journal
            if event.get("kind") == "global_candidate_pass"
        )
        self.assertEqual(
            terminal_pass["candidate_compression"][
                "candidate_pagination"
            ]["final_page_identity"],
            next(
                event["page_identity"]
                for event in state.investigation_journal
                if event.get("kind") == "global_candidate_page"
                and event.get("page_phase") == "final"
            ),
        )

    def test_budget_split_finalists_are_not_judged_as_one_partial_final_page(self):
        judge = _BudgetedEightFinalistsJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace(candidate_count=9)),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        final_plans = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page_plan"
            and event.get("planning_diagnostics", {}).get(
                "planning_intent"
            ) == "final_comparison"
        ]
        self.assertTrue(final_plans)
        self.assertTrue(
            any(len(event["plan"]["pages"]) > 1 for event in final_plans)
        )
        self.assertTrue(
            all(event["page_phase"] == "final" for event in final_plans)
        )
        convergence = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertIn(
            convergence["active_plan_identity"],
            {event["plan_identity"] for event in final_plans},
        )
        self.assertFalse(
            any(
                event.get("kind") == "global_candidate_page"
                and event.get("page_phase") == "final"
                and event.get("candidate_count")
                < len(
                    next(
                        plan["plan"]["candidate_refs"]
                        for plan in final_plans
                        if plan["plan_identity"] == event["plan_identity"]
                    )
                )
                for event in report.investigation_journal
            )
        )
        self.assertEqual(report.analysis_outcome, "execution_failed")
        seed = report.seed_results[0]
        self.assertEqual(seed.outcome, "execution_failed")
        self.assertEqual(seed.missing_evidence, ())
        self.assertEqual(seed.blocking_reasons, ())
        self.assertEqual(
            seed.execution_failures[0]["reason"],
            "context_window_exceeded",
        )
        self.assertIn(
            "final comparison",
            seed.execution_failures[0]["detail"].lower(),
        )

    def test_report_validation_rejects_tampered_page_planning_projection(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedNoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        plan = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
        )
        plan["planning_diagnostics"]["pages"][0]["projection"][
            "projection_identity"
        ] = "f" * 64
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "planning diagnostics",
        ):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered planning projection",
            )

    def test_report_validation_requires_convergence_active_plan_to_exist(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_NoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        convergence = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_convergence"
        )
        convergence["active_plan_identity"] = "f" * 64
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "active plan",
        ):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered convergence plan",
            )

    def test_report_validation_requires_convergence_to_reference_latest_plan(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedEightFinalistsJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        plans = [
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
        ]
        self.assertGreaterEqual(len(plans), 2)
        convergence = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_convergence"
        )
        convergence["active_plan_identity"] = plans[0]["plan_identity"]
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "active plan"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="stale convergence plan",
            )

    def test_unexecuted_final_plan_projection_is_bound_to_canonical_request(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedEightFinalistsJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        final_plan = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
            and event.get("planning_diagnostics", {}).get("planning_intent")
            == "final_comparison"
        )
        projection = final_plan["planning_diagnostics"]["pages"][0][
            "projection"
        ]
        projection["canonical_request_sha256"] = "e" * 64
        projection["projection_identity"] = hashlib.sha256(
            stable_json(
                {
                    key: projection[key]
                    for key in (
                        "schema",
                        "canonical_request_sha256",
                        "projected_facts_sha256",
                        "omission_manifest_sha256",
                        "projection_policy_sha256",
                    )
                }
            ).encode("utf-8")
        ).hexdigest()
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "planned request"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="resigned unexecuted final plan",
            )

    def test_resigned_planned_request_cannot_drift_from_run_analysis_perspective(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedEightFinalistsJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="Honor the requested build workflow.",
        )
        payload = report.to_dict()
        final_plan = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
            and event.get("planning_diagnostics", {}).get("planning_intent")
            == "final_comparison"
        )
        diagnostics = final_plan["planning_diagnostics"]
        page = diagnostics["pages"][0]
        page["validation_envelope"]["analysis_perspective"] = (
            "Ignore the requested build workflow."
        )
        self._resign_planned_request_diagnostic(
            page,
            context_budget=diagnostics["context_budget"],
        )
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "analysis perspective"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="resigned perspective drift",
            )

    def test_resigned_planning_request_cannot_remove_authoritative_evidence_context(self):
        trace = paginated_trace(candidate_count=9)
        trace["records"].insert(
            -1,
            {
                "record_id": "test-result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {
                    "tool_name": "bash",
                    "args": {"command": "run focused verification"},
                    "metadata": {"exit": 0, "output": "9 passing"},
                },
            },
        )
        graph = TraceGraph.from_trace(trace)
        report = AgenticRecursiveAnalyzer(
            judge=_BudgetedNoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Determine whether required behavior was verified.",
        )
        payload = report.to_dict()
        manifest = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "candidate_cluster_manifest_shadow"
        )
        self.assertIn(
            "record:test-result",
            [
                fact["ref"]
                for fact in manifest["manifest"]["candidate_facts"]
                if any(
                    disposition == "evidence_context"
                    for disposition in (
                        cluster["member_dispositions"].get(fact["ref"])
                        for cluster in manifest["manifest"]["clusters"]
                        if fact["ref"] in cluster["member_refs"]
                    )
                )
            ],
        )

        def remove_context(envelope: dict) -> None:
            envelope["evidence_context_capsules"] = []
            funnel = envelope["trace_health"]["candidate_compression"][
                "candidate_funnel"
            ]
            funnel["evidence_context_refs"] = []
            funnel["evidence_context_count"] = 0

        for event in payload["investigation_journal"]:
            if event.get("kind") == "global_candidate_page_plan":
                diagnostics = event["planning_diagnostics"]
                for diagnostic in (
                    *diagnostics["pages"],
                    *diagnostics["split_history"],
                ):
                    remove_context(diagnostic["validation_envelope"])
                    self._resign_planned_request_diagnostic(
                        diagnostic,
                        context_budget=diagnostics["context_budget"],
                    )
            if event.get("kind") == "global_candidate_page":
                remove_context(event["validation_envelope"])
                request = global_candidate_request_from_validation_envelope(
                    event["validation_envelope"]
                )
                event["request_identity"] = "global_request:v1:{0}".format(
                    hashlib.sha256(
                        stable_json(request.validation_envelope()).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                )
                event["candidate_compression"] = copy.deepcopy(
                    dict(request.trace_health["candidate_compression"])
                )
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "evidence context"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="resigned evidence-context removal",
            )

    def test_resigned_manifest_cannot_forge_a_causal_path_to_remove_evidence_context(self):
        trace = paginated_trace(candidate_count=9)
        trace["records"].insert(
            -1,
            {
                "record_id": "test-result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {
                    "tool_name": "bash",
                    "args": {"command": "run focused verification"},
                    "metadata": {"exit": 0, "output": "9 passing"},
                },
            },
        )
        graph = TraceGraph.from_trace(trace)
        report = AgenticRecursiveAnalyzer(
            judge=_BudgetedNoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Determine whether required behavior was verified.",
        )
        payload = report.to_dict()
        shadow = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "candidate_cluster_manifest_shadow"
        )
        planning = next(
            event["planning_diagnostics"]
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
        )
        funnel = planning["pages"][0]["validation_envelope"][
            "trace_health"
        ]["candidate_compression"]["candidate_funnel"]
        forged_audit = copy.deepcopy(funnel["candidate_audit"])
        test_result_audit = next(
            item
            for item in forged_audit
            if item["ref"] == "record:test-result"
        )
        test_result_audit["disposition"] = "offered"
        test_result_audit["reason"] = "input_order"
        test_result_audit["context_rank"] = None
        test_result_audit["offered_rank"] = 9
        candidate_paths = {
            fact["ref"]: tuple(fact["path_refs"])
            for fact in shadow["manifest"]["candidate_facts"]
        }
        candidate_paths["record:test-result"] = (
            "record:test-result",
            "record:observed-defect",
        )
        manifest_candidates = []
        seen_candidate_identities = set()
        for diagnostic in planning["pages"]:
            envelope = diagnostic["validation_envelope"]
            for capsule in (
                *envelope["candidate_evidence_capsules"],
                *envelope["evidence_context_capsules"],
            ):
                source = capsule["validation_source"]
                candidate = CausalCandidate(
                    ref=capsule["candidate_ref"],
                    node=graph.nodes[capsule["candidate_ref"]],
                    source=source["candidate_source"],
                    edge=source["candidate_edge"],
                    evidence_refs=tuple(
                        source["candidate_evidence_refs"]
                    ),
                )
                audit_identity = next(
                    item["candidate_identity"]
                    for item in forged_audit
                    if item["ref"] == candidate.ref
                )
                if audit_identity in seen_candidate_identities:
                    continue
                seen_candidate_identities.add(audit_identity)
                manifest_candidates.append(candidate)
        forged_manifest = build_candidate_cluster_manifest(
            graph=graph,
            candidates=manifest_candidates,
            candidate_paths=candidate_paths,
            candidate_audit=forged_audit,
            source_selection_identity=shadow["source_selection_identity"],
            seed_ref="record:observed-defect",
            defect_fingerprint=shadow["manifest"]["defect_fingerprint"],
        )
        payload["investigation_journal"][
            payload["investigation_journal"].index(shadow)
        ] = build_candidate_cluster_shadow_event(
            manifest=forged_manifest,
            seed_binding_identity=shadow["seed_binding_identity"],
        )

        def remove_context(envelope: dict) -> None:
            envelope["evidence_context_capsules"] = []
            local_funnel = envelope["trace_health"][
                "candidate_compression"
            ]["candidate_funnel"]
            local_funnel["evidence_context_refs"] = []
            local_funnel["evidence_context_count"] = 0

        for event in payload["investigation_journal"]:
            if event.get("kind") == "global_candidate_page_plan":
                diagnostics = event["planning_diagnostics"]
                for diagnostic in (
                    *diagnostics["pages"],
                    *diagnostics["split_history"],
                ):
                    remove_context(diagnostic["validation_envelope"])
                    self._resign_planned_request_diagnostic(
                        diagnostic,
                        context_budget=diagnostics["context_budget"],
                    )
            if event.get("kind") == "global_candidate_page":
                remove_context(event["validation_envelope"])
                request = global_candidate_request_from_validation_envelope(
                    event["validation_envelope"]
                )
                event["request_identity"] = "global_request:v1:{0}".format(
                    hashlib.sha256(
                        stable_json(request.validation_envelope()).encode(
                            "utf-8"
                        )
                    ).hexdigest()
                )
                event["candidate_compression"] = copy.deepcopy(
                    dict(request.trace_health["candidate_compression"])
                )
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "graph-bound path"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="resigned manifest path forgery",
            )

    def test_convergence_finalists_must_match_latest_round_summary(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedEightFinalistsJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        convergence = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_convergence"
        )
        convergence["supported_finalist_refs"] = convergence[
            "supported_finalist_refs"
        ][:1]
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "latest round"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered convergence finalists",
            )

    def test_convergence_status_must_match_terminal_page_facts(self):
        trace = paginated_trace(candidate_count=9)
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedEightFinalistsJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        convergence = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_convergence"
        )
        convergence["status"] = "final_judgment_completed"
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "terminal facts"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered convergence status",
            )

    def test_large_final_comparison_persists_replayable_budget_preflight(self):
        trace = paginated_trace()
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedOnePerPageRootJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        convergence = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertEqual(
            convergence["status"],
            "final_comparison_context_budget_exceeded",
        )
        preflight = convergence["final_comparison_preflight"]
        self.assertEqual(
            preflight["schema"],
            "global-candidate-final-comparison-preflight/v1",
        )
        self.assertFalse(preflight["measurement"]["fits"])
        preflight["measurement"]["estimated_input_tokens"] = 1
        preflight["measurement"]["fits"] = True
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "final comparison preflight"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered final comparison preflight",
            )

    def test_final_preflight_measurement_must_match_execution_failure_projection(self):
        trace = paginated_trace()
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedOnePerPageRootJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=64,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        for failure in (
            payload["seed_results"][0]["execution_failures"][0],
            payload["metadata"]["analysis_execution_failures"][0],
        ):
            measurement = failure["budget"][
                "final_comparison_measurement"
            ]
            measurement["estimated_input_tokens"] = 1
            measurement["fits"] = True
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "execution failure"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered execution failure budget",
            )

    def test_report_validation_replays_split_parent_lineage(self):
        trace = paginated_trace()
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_BudgetedNoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )
        payload = report.to_dict()
        plan = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page_plan"
            and event.get("planning_diagnostics", {}).get("split_history")
        )
        plan["planning_diagnostics"]["split_history"][0][
            "parent_page_identity"
        ] = "f" * 64
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(ValueError, "split history"):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered split parent",
            )

    def test_stalled_pages_are_not_mislabeled_when_full_comparison_fits_budget(self):
        judge = _LargeBudgetAllCandidatesRemainPlausibleJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            TraceGraph.from_trace(paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
        )

        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.seed_results[0].execution_failures, ())
        convergence = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertEqual(convergence["status"], "stalled")

    def test_final_comparison_budget_failure_replays_without_provider_calls(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace, max_judge_requests=64)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "final-budget.checkpoint"
            first_judge = _BudgetedOnePerPageRootJudge()
            first = _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=64,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
            )
            resumed_judge = _BudgetedOnePerPageRootJudge()
            resumed = _FixedPoolAnalyzer(
                judge=resumed_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=64,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
            )

        self.assertEqual(first.analysis_outcome, "execution_failed")
        self.assertGreater(len(first_judge.global_requests), 0)
        self.assertEqual(resumed.analysis_outcome, "execution_failed")
        self.assertEqual(resumed_judge.global_requests, [])
        self.assertEqual(
            resumed.seed_results[0].execution_failures,
            first.seed_results[0].execution_failures,
        )

    def test_final_page_uses_the_same_owner_bound_schema_through_build_report(self):
        judge = _LateRootPagingJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
            stop_requested=lambda: judge.request_count >= 5,
        ).analyze(
            TraceGraph.from_trace(paginated_trace()),
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="",
        )

        page_events = [
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        ordinary = next(
            event for event in page_events if event["page_phase"] == "initial"
        )
        final = next(
            event for event in page_events if event["page_phase"] == "final"
        )
        self.assertEqual(set(final), set(ordinary))
        self.assertEqual(final["owner"], ordinary["owner"])
        self.assertEqual(
            len(report.metadata["global_candidate_judgments"]),
            len(page_events),
        )

    def test_invalid_first_page_does_not_prevent_second_page_execution(self):
        graph = TraceGraph.from_trace(paginated_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=["record:observed-defect"],
            objective="Find the authored decision that introduced the defect.",
            analysis_perspective="",
            max_hypotheses=64,
        )
        judge = _FirstPageInvalidJudge()
        analyzer = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        )

        analyzer._run_global_candidate_prepass(state, graph)

        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            [8, 8, 8, 1],
        )
        self.assertEqual(
            [
                event["status"]
                for event in state.investigation_journal
                if event.get("kind") == "global_candidate_page"
            ],
            [
                "failed",
                "completed",
                "completed",
                "completed",
            ],
        )
        self.assertFalse(
            any(
                event.get("kind") == "global_candidate_pass"
                for event in state.investigation_journal
            )
        )
        seed = state.seed_results()[0]
        self.assertEqual(seed.outcome, "execution_failed")
        self.assertEqual(seed.blocking_reasons, ())
        self.assertEqual(seed.missing_evidence, ())
        self.assertEqual(
            seed.execution_failures[0]["reason"],
            "analysis_adapter_invalid",
        )

    def test_completed_paginated_checkpoint_replays_with_zero_provider_calls(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "paging.checkpoint"
            first_judge = _NoDefectPagingJudge()
            first = _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            replay_judge = _NoDefectPagingJudge()
            replayed = _FixedPoolAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )

        self.assertEqual(first.analysis_outcome, "no_defect")
        self.assertEqual(replayed.to_dict(), first.to_dict())
        self.assertEqual(
            [len(request.capsules) for request in first_judge.global_requests],
            [8, 8, 8, 1],
        )
        self.assertEqual(replay_judge.global_requests, [])

    def test_resume_skips_completed_first_page_and_runs_remaining_pages(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "paging.checkpoint"
            first_judge = _NoDefectPagingJudge()
            interrupted = _FixedPoolAnalyzer(
                judge=first_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
                stop_requested=lambda: first_judge.request_count >= 1,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )
            replay_judge = _NoDefectPagingJudge()
            resumed = _FixedPoolAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )

        self.assertEqual(
            interrupted.metadata["termination_reason"],
            "signal_interrupted",
        )
        self.assertEqual(
            [len(request.capsules) for request in first_judge.global_requests],
            [8],
        )
        self.assertEqual(
            [len(request.capsules) for request in replay_judge.global_requests],
            [8, 8, 1],
        )
        self.assertEqual(resumed.analysis_outcome, "no_defect")

    def test_resume_never_retries_started_page_and_continues_later_page(self):
        trace = paginated_trace()
        config = self._checkpoint_config(trace)
        with tempfile.TemporaryDirectory() as tempdir:
            checkpoint_root = Path(tempdir) / "paging.checkpoint"
            with self.assertRaises(KeyboardInterrupt):
                _FixedPoolAnalyzer(
                    judge=_InterruptingFirstPageJudge(),
                    fusion_mode="retrieval-global",
                    max_judge_requests=16,
                    max_hypotheses=64,
                    checkpoint=CheckpointBundle(checkpoint_root),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=["record:observed-defect"],
                    objective=(
                        "Find the authored decision that introduced the defect."
                    ),
                    analysis_perspective="",
                )
            replay_judge = _NoDefectPagingJudge()
            resumed = _FixedPoolAnalyzer(
                judge=replay_judge,
                fusion_mode="retrieval-global",
                max_judge_requests=16,
                max_hypotheses=64,
                checkpoint=CheckpointBundle(checkpoint_root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=["record:observed-defect"],
                objective=(
                    "Find the authored decision that introduced the defect."
                ),
                analysis_perspective="",
            )

        self.assertEqual(
            [len(request.capsules) for request in replay_judge.global_requests],
            [8, 8, 1],
        )
        page_events = [
            event
            for event in resumed.investigation_journal
            if event.get("kind") == "global_candidate_page"
        ]
        self.assertEqual(
            [event["status"] for event in page_events],
            [
                "failed",
                "completed",
                "completed",
                "completed",
            ],
        )
        self.assertEqual(
            page_events[0]["blocker"],
            "global_judge_page_interrupted",
        )
        self.assertEqual(
            page_events[0]["physical_request_delta"],
            GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP,
        )
        self.assertEqual(
            resumed.metadata["physical_judge_request_count"],
            GLOBAL_CANDIDATE_PAGE_PHYSICAL_REQUEST_CAP + 3,
        )

    def test_report_validation_rejects_tampered_page_request_identity(self):
        trace = paginated_trace()
        graph = TraceGraph.from_trace(trace)
        report = _FixedPoolAnalyzer(
            judge=_NoDefectPagingJudge(),
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective=(
                "Find the authored decision that introduced the defect."
            ),
            analysis_perspective="",
        )
        payload = report.to_dict()
        page = next(
            event
            for event in payload["investigation_journal"]
            if event.get("kind") == "global_candidate_page"
        )
        page["request_identity"] = "global_request:v1:" + ("0" * 64)
        tampered = RecursiveAttributionReport.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "contradicts its plan or request",
        ):
            validate_recursive_report_against_graph(
                graph,
                tampered,
                label="tampered paginated report",
            )

    def test_stalled_comparison_preserves_every_plausible_candidate(self):
        graph = TraceGraph.from_trace(paginated_trace())
        judge = _AllCandidatesRemainPlausibleJudge()
        report = _FixedPoolAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
            max_judge_requests=16,
            max_hypotheses=64,
        ).analyze(
            graph,
            start_refs=["record:observed-defect"],
            objective=(
                "Find the authored decision that introduced the defect."
            ),
            analysis_perspective="",
        )

        self.assertEqual(
            [len(request.capsules) for request in judge.global_requests],
            ([8, 8, 8, 1] * 2),
        )
        convergence = next(
            event
            for event in report.investigation_journal
            if event.get("kind") == "global_candidate_convergence"
        )
        self.assertEqual(convergence["status"], "stalled")
        self.assertEqual(
            len(convergence["supported_finalist_refs"]),
            25,
        )
        self.assertEqual(
            set(convergence["supported_finalist_refs"]),
            {
                "record:decision-{0:03d}".format(index)
                for index in range(25)
            },
        )
        self.assertFalse(
            any(
                event.get("kind") == "global_candidate_pass"
                for event in report.investigation_journal
            )
        )

    def test_checkpoint_rejects_resigned_pagination_policy_drift(self):
        config = self._checkpoint_config(paginated_trace())
        config["global_pagination_policy"]["page_size"] = 32
        semantic = {
            key: value
            for key, value in config.items()
            if key != "config_fingerprint"
        }
        config["config_fingerprint"] = hashlib.sha256(
            stable_json(semantic).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(
            CheckpointCompatibilityError,
            "pagination policy",
        ):
            validate_checkpoint_config(config)


if __name__ == "__main__":
    unittest.main()
