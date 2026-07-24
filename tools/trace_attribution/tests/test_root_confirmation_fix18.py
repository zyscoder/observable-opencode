from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.cache import build_judge_cache_key
from trace_attribution.causal_judge import (
    CAUSAL_STEP_PROMPT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
    CausalStepRequest,
    OfflineJudgeCapability,
    RootConfirmationRequest,
    build_causal_step_prompt,
    build_recursive_confirmation_prompt,
)
from trace_attribution.causal_state import (
    GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
    GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
    CausalCandidate,
    CausalStepJudgment,
    DefectState,
    PredecessorAssessment,
    RootConfirmation,
)
from trace_attribution.checkpoint import (
    CheckpointBundle,
    CheckpointCompatibilityError,
    _sha256,
    build_checkpoint_config,
    validate_checkpoint_config,
)
from trace_attribution.evidence_capsule import (
    CAPSULE_SCHEMA_VERSION,
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
)
from trace_attribution.global_judge import (
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    build_global_candidate_prompt,
)
from trace_attribution.graph import (
    EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
    TraceGraph,
)
from trace_attribution.investigation import (
    CausalInvestigationTools,
    InvestigationDirective,
)
from trace_attribution.judgment_context import progress_episode_context
from trace_attribution.models import stable_json
from trace_attribution.progress import (
    active_progress_episode_data,
    active_progress_navigation_window,
)
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)


OBJECTIVE = "Find the active defect root."
PERSPECTIVE = "Improve repository reasoning."


def defect_state() -> DefectState:
    return DefectState.create(
        label="active_generation_defect",
        expected="The active generation is correct.",
        actual="The active generation contains a defect.",
        mechanism="Stale facts must not ground the active conclusion.",
        scope="task_quality",
    )


def checkpoint_config(trace: dict, start_refs: list[str]) -> dict:
    return build_checkpoint_config(
        trace=trace,
        case_id=str(trace["case_id"]),
        objective=OBJECTIVE,
        analysis_perspective=PERSPECTIVE,
        start_refs=start_refs,
        budgets={
            "max_frontier_items": 96,
            "max_depth": 20,
            "max_hypotheses": 24,
            "max_investigation_rounds": 12,
            "max_artifact_bytes": 1_048_576,
            "max_judge_requests": 128,
        },
        model_identity="offline:fix18",
        cache_identity="cache:fix18",
        runtime_identity={
            "judge_timeout_sec": 3600.0,
            "judge_max_tokens": 4096,
            "thinking_mode": "disabled",
            "base_url": "offline://fix18",
            "provider_error_threshold": 3,
        },
    )


def mixed_seed_trace() -> dict:
    records = []
    edges = []
    for suffix in ("stale", "active"):
        records.extend(
            [
                {
                    "record_id": f"decision_{suffix}",
                    "component": "agent",
                    "event_type": "decision",
                    "data": {
                        "revision_status": "matched",
                        "rationale": "The decision introduced the defect.",
                    },
                },
                {
                    "record_id": f"change_{suffix}",
                    "component": "tool",
                    "event_type": "change",
                    "source_refs": [f"record:decision_{suffix}"],
                    "data": {
                        "revision_status": "matched",
                        "summary": "The decision changed the output.",
                    },
                },
                {
                    "record_id": f"defect_{suffix}",
                    "component": "evaluation",
                    "event_type": "case.observed_defect",
                    "source_refs": [f"record:change_{suffix}"],
                    "data": {
                        "revision_status": "matched",
                        "actual": f"The {suffix} branch contains the defect.",
                    },
                },
            ]
        )
        edges.extend(
            [
                {
                    "from": {"type": "record", "id": f"decision_{suffix}"},
                    "to": {"type": "record", "id": f"change_{suffix}"},
                    "relation": "decision_guided_change",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "record", "id": f"change_{suffix}"},
                    "to": {"type": "record", "id": f"defect_{suffix}"},
                    "relation": "change_observed_by_evaluation",
                    "evidence_type": "confirmed",
                    "eligible_for_attribution": True,
                },
            ]
        )
    return {
        "case_id": "fix18-mixed-seed-publications",
        "records": records,
        "dataflow_edges": edges,
    }


class MixedPublicationJudge(OfflineJudgeCapability):
    def judge_step_offline(self, request):
        ref = request.current_node.ref
        if "defect_" in ref:
            predecessor = ref.replace("defect_", "change_")
        elif "change_" in ref:
            predecessor = ref.replace("change_", "decision_")
        else:
            return CausalStepJudgment(
                current_node_ref=ref,
                current_defect_status="present",
                current_defect_reason="The decision contains the active defect.",
                predecessors=(),
                candidate_introduction=True,
                suggested_investigation={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context[
                            "active_hypothesis_id"
                        ],
                        "candidate_ref": ref,
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Independently confirm the introduction candidate.",
                },
                confidence=0.9,
            )
        return CausalStepJudgment(
            current_node_ref=ref,
            current_defect_status="present",
            current_defect_reason="The predecessor propagates the active defect.",
            predecessors=(
                PredecessorAssessment(
                    ref=predecessor,
                    relation="same_defect_propagation",
                    reason="The predecessor carries the same defect.",
                    confidence=0.9,
                    recurse=True,
                    evidence_refs=(predecessor,),
                ),
            ),
            candidate_introduction=False,
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        if request.candidate_ref == "record:decision_stale":
            return RootConfirmation(
                candidate_ref=request.candidate_ref,
                status="rejected",
                reason="The stale decision is a condition rather than a necessary root.",
                counterfactual_status="rejects_causality",
                evidence_refs=(
                    request.candidate_ref,
                    "record:change_stale",
                ),
                factor_role="contributing_condition",
                factor_mechanism={
                    "mechanism_type": "enabling_condition",
                    "source_ref": request.candidate_ref,
                    "target_ref": "record:change_stale",
                    "effect": "The decision enabled the defective change.",
                },
            )
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="The decision introduced the defect.",
            reason="The active decision is the necessary root.",
            counterfactual="Correcting the decision prevents the defect.",
            confidence=0.9,
            evidence_refs=[request.candidate_ref],
        )


class StaleSeedPublicationQuarantineTest(unittest.TestCase):
    def test_partial_and_completed_restore_quarantine_stale_mixed_publications(self):
        trace = mixed_seed_trace()
        start_refs = ["record:defect_stale", "record:defect_active"]
        config = checkpoint_config(trace, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "mixed.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=MixedPublicationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=OBJECTIVE,
                analysis_perspective=PERSPECTIVE,
            )
            self.assertEqual(len(report.contributing_conditions), 1)
            self.assertEqual(len(report.confirmed_roots), 1)
            checkpoint = CheckpointBundle(root).restore(expected_config=config)

            active_trace = copy.deepcopy(trace)
            stale_seed = next(
                item
                for item in active_trace["records"]
                if item["record_id"] == "defect_stale"
            )
            stale_seed["data"]["revision_status"] = "stale"
            graph = TraceGraph.from_trace(active_trace)

            partial_actions = tuple(
                item
                for item in checkpoint.actions
                if item.get("operation") != "analysis_ready"
            )
            for label, candidate_checkpoint in (
                ("partial", replace(checkpoint, actions=partial_actions)),
                ("completed", checkpoint),
            ):
                with self.subTest(label=label):
                    restored = RecursiveAnalysisState.from_checkpoint(
                        graph=graph,
                        checkpoint=candidate_checkpoint,
                    )
                    by_ref = {
                        item.start_ref: item for item in restored.seed_results()
                    }
                    self.assertEqual(
                        by_ref["record:defect_stale"].outcome, "evidence_gap"
                    )
                    self.assertEqual(
                        by_ref["record:defect_active"].outcome, "confirmed_root"
                    )
                    self.assertEqual(restored.contributing_conditions, [])
                    self.assertEqual(restored.rejected_candidates, [])
                    self.assertEqual(
                        [item.node_ref for item in restored.confirmed_roots],
                        ["record:decision_active"],
                    )
                    self.assertEqual(
                        [
                            item.current_node_ref
                            for item in restored.step_judgments
                        ],
                        [
                            "record:change_active",
                            "record:decision_active",
                        ],
                    )
                    self.assertEqual(
                        [item.ref for item in restored.causal_relations],
                        [
                            "record:change_active",
                            "record:decision_active",
                        ],
                    )
                    self.assertEqual(
                        [
                            item["candidate_ref"]
                            for item in restored.introduction_bindings
                        ],
                        ["record:decision_active"],
                    )
                    self.assertEqual(
                        [
                            item["active_visit"]["node_ref"]
                            for item in restored.investigation_journal
                        ],
                        ["record:decision_active"],
                    )
                    self.assertEqual(
                        restored.taint_paths,
                        [
                            (
                                "record:change_active",
                                "record:defect_active",
                            ),
                            (
                                "record:decision_active",
                                "record:change_active",
                                "record:defect_active",
                            ),
                        ],
                    )

    def test_completed_report_restore_quarantines_stale_seed_without_blocking_active(self):
        trace = mixed_seed_trace()
        start_refs = ["record:defect_stale", "record:defect_active"]
        config = checkpoint_config(trace, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "completed.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=MixedPublicationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=OBJECTIVE,
                analysis_perspective=PERSPECTIVE,
            )
            active_trace = copy.deepcopy(trace)
            next(
                item
                for item in active_trace["records"]
                if item["record_id"] == "defect_stale"
            )["data"]["revision_status"] = "stale"

            restored = AgenticRecursiveAnalyzer(
                judge=MixedPublicationJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(active_trace),
                start_refs=start_refs,
                objective=OBJECTIVE,
                analysis_perspective=PERSPECTIVE,
            )

            by_ref = {item.start_ref: item for item in restored.seed_results}
            self.assertEqual(by_ref["record:defect_stale"].outcome, "evidence_gap")
            self.assertEqual(by_ref["record:defect_active"].outcome, "confirmed_root")
            self.assertEqual(restored.contributing_conditions, ())
            self.assertEqual(
                [item.node_ref for item in restored.confirmed_roots],
                ["record:decision_active"],
            )


def sanitizer_trace() -> dict:
    leak = {
        "evidence": {
            "ref": "stale_change",
            "content": "STALE_NESTED_FACT",
        },
        "artifact": {
            "artifact_id": "missing-artifact",
            "content": "MISSING_ARTIFACT_EXCERPT",
        },
    }
    return {
        "case_id": "fix18-deep-sanitizer",
        "records": [
            {
                "record_id": "decision",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "repository_revision": 1,
                    "rationale": "Active decision.",
                    "nested": leak,
                    "serialized_diagnostics": json.dumps(leak, sort_keys=True),
                },
            },
            {
                "record_id": "stale_change",
                "component": "tool",
                "event_type": "change",
                "data": {
                    "revision_after": 0,
                    "summary": "Stale generation diagnostic.",
                },
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:decision"],
                "data": {
                    "repository_revision": 1,
                    "actual": "The active generation contains a defect.",
                },
            },
            {
                "record_id": "claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 1,
                    "is_final_for_case": True,
                    "claim": "Generation one is active.",
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "decision"},
                "to": {"type": "record", "id": "defect"},
                "relation": "decision_observed_by_evaluation",
                "evidence_type": "confirmed",
                "eligible_for_attribution": True,
            }
        ],
    }


class DeepJudgeSanitizerTest(unittest.TestCase):
    def setUp(self):
        self.graph = TraceGraph.from_trace(sanitizer_trace())
        self.defect = defect_state()
        self.candidate = CausalCandidate(
            ref="record:decision",
            node=self.graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=self.graph.edge_context("record:decision", "record:defect")[0],
            evidence_refs=("record:decision", "record:defect"),
        )

    def assert_no_leaks(self, payload, *, allow_missing_artifact_gap=False):
        encoded = json.dumps(payload, sort_keys=True)
        self.assertNotIn("STALE_NESTED_FACT", encoded)
        self.assertNotIn("MISSING_ARTIFACT_EXCERPT", encoded)
        self.assertNotIn("stale_change", encoded)
        if allow_missing_artifact_gap:
            self.assertIn("missing-artifact", encoded)
        else:
            self.assertNotIn("missing-artifact", encoded)

    def test_global_final_json_is_deep_sanitized(self):
        capsules = build_candidate_evidence_capsules(
            graph=self.graph,
            candidates=(self.candidate,),
            defect_state=self.defect,
            downstream_paths={
                "record:decision": ("record:decision", "record:defect")
            },
            start_refs=("record:defect",),
        )
        global_request = GlobalCandidateJudgeRequest(
            case_id=self.graph.case_id,
            objective=OBJECTIVE,
            analysis_perspective=PERSPECTIVE,
            seed_ref="record:defect",
            active_defect=self.defect,
            active_focus_text=self.defect.actual,
            active_focus_text_hash=active_focus_text_sha256(self.defect.actual),
            start_refs=("record:defect",),
            capsules=capsules,
        )
        self.assert_no_leaks(
            json.loads(build_global_candidate_prompt(global_request)),
            allow_missing_artifact_gap=True,
        )

    def test_recursive_step_final_json_is_deep_sanitized(self):
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=["record:defect"],
            objective=OBJECTIVE,
            analysis_perspective=PERSPECTIVE,
        )
        item = state.frontier.pop()
        step_request = state.build_step_request(
            self.graph, item, [self.candidate]
        )
        self.assert_no_leaks(json.loads(build_causal_step_prompt(step_request)))

    def test_confirmation_final_json_is_deep_sanitized(self):
        resolved_candidate = {
            "raw_ref": "record:decision",
            "resolved_ref": "record:decision",
            "canonical_ref": "record:decision",
            "resolution_status": "resolved",
            "provenance_class": "recorded",
            "node": {
                "ref": "record:decision",
                "data": self.graph.nodes["record:decision"].data,
            },
        }
        confirmation_request = RootConfirmationRequest(
            candidate_ref="record:decision",
            defect_state=self.defect,
            recursive_path=("record:decision", "record:defect"),
            candidate_reference=resolved_candidate,
            recursive_path_references=(
                resolved_candidate,
                {
                    "raw_ref": "record:defect",
                    "resolved_ref": "record:defect",
                    "canonical_ref": "record:defect",
                    "resolution_status": "resolved",
                    "provenance_class": "recorded",
                },
            ),
            supporting_evidence=(resolved_candidate,),
            opposing_evidence=(),
            competing_hypotheses=(),
            task_obligations=(),
            analysis_perspective=PERSPECTIVE,
        )
        sanitized_confirmation = self.graph.sanitize_judge_visible_payload(
            confirmation_request.factual_dict()
        )
        sanitized_request = replace(
            confirmation_request,
            candidate_reference=sanitized_confirmation["candidate_reference"],
            recursive_path_references=tuple(
                sanitized_confirmation["recursive_path_references"]
            ),
            supporting_evidence=tuple(
                sanitized_confirmation["supporting_evidence"]
            ),
        )
        self.assert_no_leaks(
            json.loads(build_recursive_confirmation_prompt(sanitized_request))
        )

    def test_unverified_artifact_and_unresolved_namespaced_ref_are_removed(self):
        payload = {
            "evidence": {
                "ref": "external:unresolved",
                "content": "UNRESOLVED_NAMESPACED_FACT",
            },
            "artifact": {
                "artifact_id": "unindexed-artifact",
                "content": "UNVERIFIED_ARTIFACT_FACT",
                "content_hash": "sha256:" + "0" * 64,
            },
        }

        sanitized = self.graph.sanitize_judge_visible_payload(payload)
        encoded = stable_json(sanitized)

        self.assertNotIn("UNRESOLVED_NAMESPACED_FACT", encoded)
        self.assertNotIn("UNVERIFIED_ARTIFACT_FACT", encoded)


def progress_projection_trace() -> dict:
    return {
        "case_id": "fix18-progress-projection",
        "records": [
            {
                "record_id": "active_change",
                "component": "tool",
                "event_type": "change",
                "data": {"revision_after": 1, "summary": "One active change."},
            },
            {
                "record_id": "stale_change",
                "component": "tool",
                "event_type": "change",
                "data": {"revision_after": 0, "summary": "Stale change."},
            },
            {
                "record_id": "active_decision",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "repository_revision": 1,
                    "rationale": "No-delivery reasoning.",
                },
            },
            {
                "record_id": "delivery",
                "component": "progress",
                "event_type": "progress.episode",
                "data": {
                    "member_refs": ["record:active_change"],
                    "mutation_count": 2,
                    "cumulative_change_count": 2,
                    "cumulative_verification_count": 4,
                    "consecutive_no_delivery_episodes": 7,
                    "chronology_index": 1,
                },
            },
            {
                "record_id": "all_stale",
                "component": "progress",
                "event_type": "progress.episode",
                "data": {
                    "member_refs": ["record:stale_change"],
                    "previous_episode_ref": "record:delivery",
                    "mutation_count": 1,
                    "cumulative_change_count": 3,
                    "consecutive_no_delivery_episodes": 0,
                    "chronology_index": 2,
                },
            },
            {
                "record_id": "current",
                "component": "progress",
                "event_type": "progress.episode",
                "data": {
                    "member_refs": ["record:active_decision"],
                    "previous_episode_ref": "record:all_stale",
                    "cumulative_change_count": 99,
                    "cumulative_verification_count": 99,
                    "consecutive_no_delivery_episodes": 99,
                    "chronology_index": 3,
                },
            },
            {
                "record_id": "claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 1,
                    "is_final_for_case": True,
                },
            },
        ],
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": source},
                "to": {"type": "record", "id": target},
                "relation": "progress_episode_member",
                "evidence_type": "offline_reconstruction",
                "eligible_for_attribution": True,
            }
            for source, target in (
                ("delivery", "all_stale"),
                ("all_stale", "current"),
            )
        ],
    }


class ActiveProgressProjectionTest(unittest.TestCase):
    def test_projection_drops_all_stale_episode_and_recomputes_chain_aggregates(self):
        graph = TraceGraph.from_trace(progress_projection_trace())

        delivery = active_progress_episode_data(graph, "record:delivery")
        current = active_progress_episode_data(graph, "record:current")
        window = active_progress_navigation_window(graph, "record:current")
        context = progress_episode_context(graph, "record:active_change")

        self.assertEqual(delivery["mutation_count"], 1)
        self.assertEqual(delivery["cumulative_change_count"], 1)
        self.assertEqual(delivery["cumulative_verification_count"], 0)
        self.assertEqual(delivery["consecutive_no_delivery_episodes"], 0)
        self.assertEqual(current["cumulative_change_count"], 1)
        self.assertEqual(current["consecutive_no_delivery_episodes"], 1)
        self.assertEqual(current["previous_episode_ref"], "record:delivery")
        self.assertNotIn("record:all_stale", window["member_episode_refs"])
        self.assertEqual(
            window["previous_delivery_episode_ref"], "record:delivery"
        )
        self.assertEqual(window["episode_count"], 1)
        self.assertEqual(context["cumulative_change_count"], 1)

    def test_investigation_limit_excludes_all_stale_episode(self):
        graph = TraceGraph.from_trace(progress_projection_trace())
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:current"},
                requested_by_ref="record:current",
                reason="Inspect the active progress chain.",
            )
        )

        encoded = stable_json(result.payload)
        self.assertNotIn("record:all_stale", encoded)
        self.assertNotIn('"cumulative_change_count": 99', encoded)

    def test_investigation_returns_zero_for_all_stale_adjacent_episode(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "fix18-all-stale-investigation",
                "records": [
                    {
                        "record_id": "stale_member",
                        "component": "tool",
                        "event_type": "change",
                        "data": {"revision_after": 0},
                    },
                    {
                        "record_id": "all_stale",
                        "component": "progress",
                        "event_type": "progress.episode",
                        "data": {
                            "member_refs": ["record:stale_member"],
                            "chronology_index": 1,
                        },
                    },
                    {
                        "record_id": "anchor",
                        "component": "agent",
                        "event_type": "decision",
                        "data": {"repository_revision": 1},
                    },
                    {
                        "record_id": "claim",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {
                            "repository_revision": 1,
                            "is_final_for_case": True,
                        },
                    },
                ],
                "dataflow_edges": [
                    {
                        "from": {"type": "record", "id": "all_stale"},
                        "to": {"type": "record", "id": "anchor"},
                        "relation": "progress_episode_member",
                        "evidence_type": "offline_reconstruction",
                        "eligible_for_attribution": True,
                    }
                ],
            }
        )
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Ignore the adjacent all-stale episode.",
            )
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.payload["episodes"], ())


class EvidencePolicyCompatibilityTest(unittest.TestCase):
    def test_current_policy_versions_every_reusable_conclusion_identity(self):
        self.assertEqual(
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
            "graph-external-evidence-eligibility/v5",
        )
        self.assertEqual(CAPSULE_SCHEMA_VERSION, "candidate-evidence-capsule/v7")
        self.assertEqual(
            GLOBAL_CANDIDATE_JUDGMENT_SCHEMA_VERSION,
            "global-candidate-judgment/v7",
        )
        self.assertEqual(
            GLOBAL_CANDIDATE_VALIDATION_ENVELOPE_SCHEMA_VERSION,
            "global-candidate-validation-envelope/v7",
        )
        self.assertEqual(CAUSAL_STEP_PROMPT_SCHEMA_VERSION, "recursive-causal-step-v9")
        self.assertEqual(
            ROOT_CONFIRMATION_PROMPT_SCHEMA_VERSION,
            "recursive-root-confirmation-v9",
        )

        common = {
            "stage": "recursive_causal_step",
            "model": "offline",
            "system": "system",
            "messages": [{"role": "user", "content": "request"}],
            "max_tokens": 128,
            "thinking_config": None,
            "prompt_schema_version": CAUSAL_STEP_PROMPT_SCHEMA_VERSION,
        }
        old_key = build_judge_cache_key(
            **common,
            evidence_eligibility_policy="graph-external-evidence-eligibility/v1",
        )
        new_key = build_judge_cache_key(
            **common,
            evidence_eligibility_policy=EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        )
        self.assertNotEqual(old_key, new_key)

    def test_old_checkpoint_and_capsule_are_rejected_but_current_round_trips(self):
        trace = mixed_seed_trace()
        config = checkpoint_config(
            trace, ["record:defect_stale", "record:defect_active"]
        )
        old_config = copy.deepcopy(config)
        old_config["evidence_eligibility_policy"] = (
            "graph-external-evidence-eligibility/v1"
        )
        old_config["config_fingerprint"] = _sha256(
            {
                key: value
                for key, value in old_config.items()
                if key != "config_fingerprint"
            }
        )
        with self.assertRaises(CheckpointCompatibilityError):
            validate_checkpoint_config(old_config)
        self.assertEqual(validate_checkpoint_config(config), config)

        graph = TraceGraph.from_trace(sanitizer_trace())
        defect = defect_state()
        candidate = CausalCandidate(
            ref="record:decision",
            node=graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=graph.edge_context("record:decision", "record:defect")[0],
            evidence_refs=("record:decision", "record:defect"),
        )
        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect,
            downstream_paths={
                "record:decision": ("record:decision", "record:defect")
            },
            start_refs=("record:defect",),
        )[0]
        self.assertEqual(
            CandidateEvidenceCapsule.from_dict(capsule.to_dict()), capsule
        )
        old_capsule = capsule.to_dict()
        old_capsule["schema_version"] = "candidate-evidence-capsule/v6"
        with self.assertRaises(ValueError):
            CandidateEvidenceCapsule.from_dict(old_capsule)


if __name__ == "__main__":
    unittest.main()
