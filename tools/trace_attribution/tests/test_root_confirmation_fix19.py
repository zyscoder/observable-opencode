from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_judge import (
    CausalStepRequest,
    RootConfirmationRequest,
    build_causal_step_prompt,
    build_recursive_confirmation_prompt,
)
from trace_attribution.causal_state import (
    MODERN_REPORT_SCHEMA_VERSION,
    PREVIOUS_REPORT_SCHEMA_VERSION,
    CausalCandidate,
    DefectState,
    LocalStateOwner,
    RecursiveAttributionReport,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointBundle,
)
from trace_attribution.evidence_capsule import build_candidate_evidence_capsules
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
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
)


OBJECTIVE = "Confirm each active defect seed independently."


def sanitizer_trace() -> dict:
    embedded = "SELF_HASHED_UNINDEXED_ARTIFACT"
    return {
        "case_id": "fix19-typed-sanitizer",
        "records": [
            {
                "record_id": "decision",
                "span_id": "decision-alias",
                "component": "agent",
                "event_type": "decision",
                "data": {
                    "rationale": "Active decision.",
                    "business": {
                        "config_ref": "record:stale",
                        "customer_ref": "artifact:customer-42",
                    },
                    "evidence": {
                        "evidence_refs": ["record:decision", "record:stale"],
                        "content": "STALE_LIST_SIBLING_CONTENT",
                        "label": "mixed evidence must be atomic",
                    },
                    "citation": {
                        "mystery_pointer": "record:decision",
                        "content": "UNKNOWN_KEY_CITATION_CONTENT",
                    },
                    "valid_alias_citation": {
                        "evidence_type": "trace_record",
                        "ref": "decision",
                        "content": "ACTIVE_ALIAS_CONTENT",
                    },
                    "encoded_evidence": json.dumps(
                        {
                            "supporting_evidence": [
                                {
                                    "evidence_refs": ["record:stale"],
                                    "summary": "ENCODED_STALE_CONTENT",
                                }
                            ]
                        },
                        sort_keys=True,
                    ),
                    "nested": [
                        {
                            "opposing_evidence": {
                                "source_refs": ["record:stale"],
                                "excerpt": "NESTED_STALE_CONTENT",
                            }
                        }
                    ],
                    "artifact": {
                        "artifact_id": "self-declared",
                        "content": embedded,
                        "content_hash": "sha256:{0}".format(
                            hashlib.sha256(embedded.encode("utf-8")).hexdigest()
                        ),
                    },
                },
            },
            {
                "record_id": "stale",
                "component": "tool",
                "event_type": "change",
                "data": {
                    "revision_status": "stale",
                    "summary": "Stale generation.",
                },
            },
            {
                "record_id": "defect",
                "component": "evaluation",
                "event_type": "case.observed_defect",
                "source_refs": ["record:decision"],
                "data": {"actual": "The active decision contains a defect."},
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


def active_defect() -> DefectState:
    return DefectState.create(
        label="active_defect",
        expected="The active decision is correct.",
        actual="The active decision contains a defect.",
        mechanism="Only active, graph-grounded evidence may decide the defect.",
        scope="task_quality",
    )


class TypedEvidenceSanitizerTest(unittest.TestCase):
    def setUp(self):
        self.graph = TraceGraph.from_trace(sanitizer_trace())

    def test_atomic_mapping_validation_preserves_only_valid_typed_and_business_data(self):
        sanitized = self.graph.sanitize_judge_visible_payload(
            self.graph.nodes["record:decision"].data
        )
        encoded = stable_json(sanitized)

        self.assertEqual(
            sanitized["business"],
            {
                "config_ref": "record:stale",
                "customer_ref": "artifact:customer-42",
            },
        )
        self.assertEqual(
            sanitized["valid_alias_citation"]["ref"],
            "record:decision",
        )
        self.assertIn("ACTIVE_ALIAS_CONTENT", encoded)
        for forbidden in (
            "STALE_LIST_SIBLING_CONTENT",
            "UNKNOWN_KEY_CITATION_CONTENT",
            "ENCODED_STALE_CONTENT",
            "NESTED_STALE_CONTENT",
            "SELF_HASHED_UNINDEXED_ARTIFACT",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_stale_identity_in_list_removes_entire_fact_mapping(self):
        payload = {
            "supporting_evidence": [
                {
                    "evidence_refs": ["record:decision", "record:stale"],
                    "content": "must disappear with stale identity",
                    "metadata": {"label": "atomic"},
                },
                {
                    "evidence_refs": ["record:decision"],
                    "content": "active fact",
                },
            ]
        }

        self.assertEqual(
            self.graph.sanitize_judge_visible_payload(payload),
            {
                "supporting_evidence": [
                    {
                        "evidence_refs": ["record:decision"],
                        "content": "active fact",
                    }
                ]
            },
        )

    def test_truncated_semantic_slice_is_not_available_or_judge_visible(self):
        content = "partial semantic slice"
        content_bytes = content.encode("utf-8")
        trace = {
            "case_id": "fix19-truncated-artifact",
            "artifacts": [
                {
                    "artifact_id": "partial",
                    "kind": "text",
                    "path": "artifacts/sha256/missing.txt",
                    "hash": hashlib.sha256(b"complete artifact").hexdigest()[:16],
                    "availability": "bundled",
                    "byte_length": len(content_bytes) + 20,
                    "semantic_slices": [
                        {
                            "byte_range": [0, len(content_bytes)],
                            "content": content,
                            "hash": hashlib.sha256(content_bytes).hexdigest()[:16],
                            "truncated": False,
                        }
                    ],
                }
            ],
            "records": [
                {
                    "record_id": "decision",
                    "component": "agent",
                    "event_type": "decision",
                    "artifact_refs": ["artifact:partial"],
                    "data": {
                        "artifact": {
                            "artifact_id": "partial",
                            "raw_ref": "artifact:partial",
                            "content": content,
                        }
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            graph = TraceGraph.from_trace(trace, artifact_root=Path(directory))

        self.assertEqual(
            graph.artifact_reference_status("artifact:partial")["availability"],
            "truncated",
        )
        self.assertEqual(graph.filter_evidence_refs(("artifact:partial",)), [])
        self.assertEqual(
            graph.sanitize_judge_visible_payload(
                graph.nodes["record:decision"].data
            ),
            {},
        )


class JudgeFinalPayloadSanitizerTest(unittest.TestCase):
    def setUp(self):
        self.graph = TraceGraph.from_trace(sanitizer_trace())
        self.defect = active_defect()
        self.candidate = CausalCandidate(
            ref="record:decision",
            node=self.graph.nodes["record:decision"],
            source="confirmed_edge",
            edge=self.graph.edge_context("record:decision", "record:defect")[0],
            evidence_refs=("record:decision", "record:defect"),
        )

    def assert_final_payload_is_safe(self, payload):
        def contains_pair(value, key, expected):
            if isinstance(value, dict):
                return value.get(key) == expected or any(
                    contains_pair(child, key, expected) for child in value.values()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_pair(child, key, expected) for child in value)
            if isinstance(value, str) and value.strip().startswith(("{", "[")):
                try:
                    return contains_pair(json.loads(value), key, expected)
                except json.JSONDecodeError:
                    return False
            return False

        encoded = stable_json(payload)
        self.assertTrue(contains_pair(payload, "config_ref", "record:stale"))
        self.assertTrue(
            contains_pair(payload, "customer_ref", "artifact:customer-42")
        )
        for forbidden in (
            "STALE_LIST_SIBLING_CONTENT",
            "UNKNOWN_KEY_CITATION_CONTENT",
            "ENCODED_STALE_CONTENT",
            "NESTED_STALE_CONTENT",
            "SELF_HASHED_UNINDEXED_ARTIFACT",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_global_final_json_uses_atomic_graph_projection(self):
        capsules = build_candidate_evidence_capsules(
            graph=self.graph,
            candidates=(self.candidate,),
            defect_state=self.defect,
            downstream_paths={
                "record:decision": ("record:decision", "record:defect")
            },
            start_refs=("record:defect",),
        )
        request = GlobalCandidateJudgeRequest(
            case_id=self.graph.case_id,
            objective=OBJECTIVE,
            analysis_perspective="",
            seed_ref="record:defect",
            active_defect=self.defect,
            active_focus_text=self.defect.actual,
            active_focus_text_hash=active_focus_text_sha256(self.defect.actual),
            start_refs=("record:defect",),
            capsules=capsules,
        )

        self.assert_final_payload_is_safe(
            json.loads(build_global_candidate_prompt(request))
        )

    def test_recursive_step_final_json_uses_atomic_graph_projection(self):
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=("record:defect",),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        item = state.frontier.pop()
        request = state.build_step_request(
            self.graph,
            item,
            (self.candidate,),
        )

        self.assert_final_payload_is_safe(
            json.loads(build_causal_step_prompt(request))
        )

    def test_confirmation_final_json_uses_atomic_graph_projection(self):
        candidate_reference = {
            "raw_ref": "record:decision",
            "resolved_ref": "record:decision",
            "canonical_ref": "record:decision",
            "resolution_status": "resolved",
            "provenance_class": "recorded",
            "content": stable_json(
                self.graph.nodes["record:decision"].data
            ),
        }
        request = RootConfirmationRequest(
            candidate_ref="record:decision",
            defect_state=self.defect,
            recursive_path=("record:decision", "record:defect"),
            candidate_reference=candidate_reference,
            recursive_path_references=(
                candidate_reference,
                {
                    "raw_ref": "record:defect",
                    "resolved_ref": "record:defect",
                    "canonical_ref": "record:defect",
                    "resolution_status": "resolved",
                    "provenance_class": "recorded",
                },
            ),
            supporting_evidence=(candidate_reference,),
            opposing_evidence=(),
            competing_hypotheses=(),
            task_obligations=(),
            analysis_perspective="",
        )
        sanitized = self.graph.sanitize_judge_visible_payload(
            request.factual_dict()
        )
        request = replace(
            request,
            candidate_reference=sanitized["candidate_reference"],
            recursive_path_references=tuple(
                sanitized["recursive_path_references"]
            ),
            supporting_evidence=tuple(sanitized["supporting_evidence"]),
        )

        self.assert_final_payload_is_safe(
            json.loads(build_recursive_confirmation_prompt(request))
        )


def stale_episode_budget_trace() -> dict:
    stale_episode_ids = [
        "stale_episode_{0:03d}".format(index) for index in range(64)
    ]
    records = [
        {
            "record_id": "active_member",
            "component": "agent",
            "event_type": "decision",
            "data": {"summary": "active episode member"},
        },
        *(
            {
                "record_id": episode_id,
                "component": "progress",
                "event_type": "progress.episode",
                "data": {
                    "member_refs": [],
                    "chronology_index": index,
                },
            }
            for index, episode_id in enumerate(stale_episode_ids)
        ),
        {
            "record_id": "active_episode",
            "component": "progress",
            "event_type": "progress.episode",
            "data": {
                "member_refs": ["record:active_member"],
                "chronology_index": 65,
            },
        },
        {
            "record_id": "anchor",
            "component": "agent",
            "event_type": "decision",
            "data": {"summary": "investigation anchor"},
        },
    ]
    return {
        "case_id": "fix19-stale-episode-budget",
        "records": records,
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": episode_id},
                "to": {"type": "record", "id": "anchor"},
                "relation": "progress_episode_member",
                "evidence_type": "offline_reconstruction",
                "eligible_for_attribution": True,
            }
            for episode_id in (*stale_episode_ids, "active_episode")
        ],
    }


class EpisodeProjectionBudgetTest(unittest.TestCase):
    def test_all_stale_episodes_consume_no_scan_or_output_budget(self):
        result = CausalInvestigationTools(
            TraceGraph.from_trace(stale_episode_budget_trace())
        ).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Inspect authoritative active episodes only.",
            )
        )

        self.assertEqual(
            [item["ref"] for item in result.payload["episodes"]],
            ["record:active_episode"],
        )
        self.assertEqual(
            result.payload["adjacency_scan"]["inspected_count"],
            1,
        )
        self.assertFalse(result.payload["adjacency_scan"]["scan_truncated"])
        self.assertFalse(result.truncated)


class LocalStateOwnerRestoreTest(unittest.TestCase):
    def test_owner_envelope_rejects_non_string_missing_and_extra_fields(self):
        owner = LocalStateOwner.create(
            seed_binding_identity="seed-binding",
            hypothesis_id="hypothesis",
            visit_key="visit",
            occurrence_key="occurrence",
        ).to_dict()

        invalid_payloads = []
        for field in ("seed_binding_identity", "hypothesis_id", "visit_key"):
            payload = dict(owner)
            payload[field] = 7
            invalid_payloads.append(payload)
        non_string_occurrence = dict(owner)
        non_string_occurrence["occurrence_identity"] = type(
            "StringCoercibleOccurrence",
            (),
            {"__str__": lambda self: owner["occurrence_identity"]},
        )()
        invalid_payloads.append(non_string_occurrence)
        invalid_payloads.append(
            {
                key: value
                for key, value in owner.items()
                if key != "visit_key"
            }
        )
        invalid_payloads.append({**owner, "extra": "not allowed"})

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    LocalStateOwner.from_dict(payload)

    def test_shared_node_occurrences_keep_active_owner_on_partial_and_completed_restore(self):
        trace = shared_root_trace()
        start_refs = ("record:seed_one", "record:seed_two")
        config = shared_root_checkpoint_config(trace, OBJECTIVE, start_refs)
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "shared-owner.checkpoint"
            report = AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=start_refs,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
            self.assertEqual(len(report.step_judgments), 0)
            self.assertEqual(
                {
                    item["owner"]["seed_binding_identity"]
                    for item in report.metadata["global_candidate_judgments"]
                },
                {
                    item["seed_binding_identity"]
                    for item in report.to_dict()["seed_results"]
                },
            )
            self.assertTrue(
                all(
                    item.get("owner")
                    for item in report.metadata["recursive_expansion_reasons"]
                )
                or not report.metadata["recursive_expansion_reasons"]
            )
            self.assertTrue(
                all(
                    item.get("owner")
                    for seed in report.to_dict()["seed_results"]
                    for item in seed["decisive_evidence"]
                )
            )
            self.assertTrue(
                all(item.get("owner") for item in report.metadata["confirmation_journal"])
            )

            checkpoint = CheckpointBundle(root).restore(expected_config=config)
            active_trace = copy.deepcopy(trace)
            next(
                item
                for item in active_trace["records"]
                if item["record_id"] == "seed_one"
            )["data"]["revision_status"] = "stale"
            graph = TraceGraph.from_trace(active_trace)
            partial_actions = tuple(
                item
                for item in checkpoint.actions
                if item.get("operation") not in {"analysis_ready", "analysis_completed"}
            )
            restored = RecursiveAnalysisState.from_checkpoint(
                graph=graph,
                checkpoint=replace(checkpoint, actions=partial_actions),
            )
            active_seed = next(
                item
                for item in restored.seed_results()
                if item.start_ref == "record:seed_two"
            )
            self.assertEqual(active_seed.outcome, "confirmed_root")
            self.assertEqual(
                {
                    item["owner"]["seed_binding_identity"]
                    for item in restored.investigation_journal
                    if item.get("kind") == "global_candidate_pass"
                },
                {
                    item["seed_binding_identity"]
                    for item in restored.action_checkpoint_payload()["seed_ledger"]
                    if item["start_ref"] == "record:seed_two"
                },
            )

            completed = AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                graph,
                start_refs=start_refs,
                objective=OBJECTIVE,
                analysis_perspective="",
            )
            self.assertEqual(
                len(completed.metadata["global_candidate_judgments"]),
                1,
            )
            self.assertEqual(
                completed.metadata["global_candidate_judgments"][0]["owner"][
                    "seed_binding_identity"
                ],
                next(
                    item["seed_binding_identity"]
                    for item in completed.to_dict()["seed_results"]
                    if item["start_ref"] == "record:seed_two"
                ),
            )

    def test_persisted_local_owner_contract_versions_are_current(self):
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v11",
        )
        self.assertEqual(ACTION_STATE_SCHEMA, "recursive-analysis-actions/v9")
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v12",
        )


class LegacyReportPolicyMigrationTest(unittest.TestCase):
    def test_v2_migration_records_policy_identities_and_explicit_blocker(self):
        report = AgenticRecursiveAnalyzer(
            judge=SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
        ).analyze(
            TraceGraph.from_trace(shared_root_trace()),
            start_refs=("record:seed_one", "record:seed_two"),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        payload = report.to_dict()
        payload["schema_version"] = PREVIOUS_REPORT_SCHEMA_VERSION

        migrated = RecursiveAttributionReport.from_dict(payload)
        migration = migrated.metadata["report_migration"]

        self.assertEqual(
            migration["source_evidence_policy_identity"],
            "legacy-report-evidence-policy/unversioned",
        )
        self.assertEqual(
            migration["target_evidence_policy_identity"],
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
        )
        self.assertEqual(
            migration["blocking_reason"],
            "evidence_policy_migration_required",
        )
        self.assertTrue(
            all(
                "evidence_policy_migration_required" in item.blocking_reasons
                for item in migrated.seed_results
            )
        )
        self.assertNotIn(
            "v2_seed_binding_unavailable",
            stable_json(migrated.to_dict()),
        )
        self.assertTrue(
            all(
                item.blocking_reasons
                == ("evidence_policy_migration_required",)
                for item in migrated.seed_results
            )
        )
        for branch in migrated.metadata["unresolved_branches"]:
            self.assertEqual(
                branch["reason"],
                "evidence_policy_migration_required",
            )
            self.assertEqual(
                branch["source_evidence_policy_identity"],
                "legacy-report-evidence-policy/unversioned",
            )
            self.assertEqual(
                branch["target_evidence_policy_identity"],
                EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
            )


class OwnerScopedCapsuleTest(unittest.TestCase):
    def test_no_path_capsule_exposes_only_active_owner_outgoing_edges(self):
        graph = TraceGraph.from_trace(shared_root_trace())
        candidate = CausalCandidate(
            ref="record:shared_root",
            node=graph.nodes["record:shared_root"],
            source="progress_window",
            score=0.9,
        )

        capsule = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=active_defect(),
            downstream_paths={},
            start_refs=("record:seed_one",),
        )[0]

        self.assertEqual(capsule.downstream_path, ("record:shared_root",))
        self.assertEqual(
            [edge["to_ref"] for edge in capsule.outgoing_edges],
            ["record:seed_one"],
        )


if __name__ == "__main__":
    unittest.main()
