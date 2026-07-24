from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    COMPARISON_SCHEMA_VERSION,
    EvaluationSafetyError,
    EvaluationSchemaError,
    REPORT_SCHEMA_VERSION,
    compare_report,
)
from trace_attribution.cache import JUDGMENT_CACHE_VERSION
from trace_attribution.causal_state import (
    GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
    MODERN_REPORT_SCHEMA_VERSION,
    ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
    LocalStateOwner,
    RecursiveAttributionReport,
    annotate_report_semantic_anchors,
)
from trace_attribution.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    OUTPUT_SCHEMA_VERSION,
    CheckpointBundle,
)
from trace_attribution.graph import (
    EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
    TraceGraph,
)
from trace_attribution.recursive_analyzer import (
    ACTION_STATE_SCHEMA,
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tools.trace_attribution.tests.test_root_confirmation_fix21 import (
    InjectedCheckpoint,
)
from tools.trace_attribution.tests.test_root_confirmation_fix22 import (
    OBJECTIVE,
    STARTS,
    completed_checkpoint,
    latest_snapshot,
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    shared_root_trace,
)


COMPLETION_OPERATIONS = {"confirmation_completed", "confirmation_failed"}


class DurableConfirmationActionProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace, cls.config, cls.root, resources = completed_checkpoint(
            SharedRootFusionJudge()
        )
        cls.tempdir, cls.checkpoint = resources
        cls.graph = TraceGraph.from_trace(cls.trace)
        cls.report = next(
            item["payload"]["report"]
            for item in reversed(cls.checkpoint.actions)
            if item["operation"] == "analysis_ready"
        )
        cls.labels = {
            "schema_version": "recursive-attribution-labels/v3",
            "case_id": cls.graph.case_id,
            "roots": [],
            "conditions": [],
            "amplifiers": [],
            "forbidden_roots": [],
            "allowed_unresolved_outcomes": [
                "inconclusive",
                "partial_root_found",
            ],
        }

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def restored_report(self, actions):
        return AgenticRecursiveAnalyzer(
            judge=SharedRootFusionJudge(),
            fusion_mode="retrieval-global",
            checkpoint=InjectedCheckpoint(
                replace(self.checkpoint, actions=tuple(actions)),
                self.root,
            ),
            checkpoint_config=self.config,
        ).analyze(
            self.graph,
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )

    def test_current_report_persists_complete_owner_bound_action_projection(self):
        projection = self.report["metadata"]["confirmation_action_projection"]

        self.assertEqual(len(projection), len(self.report["confirmations"]))
        self.assertEqual(
            {item["response_identity"] for item in projection},
            {
                item["response_identity"]
                for item in self.report["confirmations"]
            },
        )
        for item in projection:
            self.assertEqual(
                set(item),
                {
                    "operation",
                    "semantic_key",
                    "request_identity",
                    "response_identity",
                    "owner",
                    "status",
                    "candidate_ref",
                    "hypothesis_id",
                    "hypothesis_semantic_hash",
                    "defect_fingerprint",
                    "seed_binding_identity",
                    "seed_key",
                    "recursive_path",
                    "evidence_refs",
                    "artifact_evidence_envelopes",
                    "evidence_disposition",
                    "factual_request_projection",
                    "physical_requests_reserved",
                    "physical_request_delta",
                    "physical_request_exact",
                    "confirmation",
                },
            )
            owner = LocalStateOwner.from_dict(item["owner"])
            self.assertEqual(owner.seed_binding_identity, item["seed_binding_identity"])
            self.assertEqual(owner.hypothesis_id, item["hypothesis_id"])
            self.assertEqual(
                item["response_identity"],
                item["confirmation"]["response_identity"],
            )
            self.assertEqual(item["status"], item["confirmation"]["status"])
            self.assertEqual(
                item["evidence_refs"],
                item["confirmation"]["evidence_refs"],
            )

    def test_current_report_requires_explicit_action_projection_field(self):
        report = copy.deepcopy(self.report)
        report["metadata"].pop("confirmation_action_projection")

        with self.assertRaisesRegex(ValueError, "action projection"):
            RecursiveAttributionReport.from_dict(report)

    def test_partial_restore_rejects_empty_action_side(self):
        actions = [
            item
            for item in copy.deepcopy(list(self.checkpoint.actions))
            if item["operation"] not in COMPLETION_OPERATIONS
        ]

        with self.assertRaisesRegex(ValueError, "confirmation.*action|action.*confirmation"):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(self.checkpoint, actions),
            )

    def test_pending_and_completed_restore_reject_empty_action_side(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = [
                    item
                    for item in copy.deepcopy(list(self.checkpoint.actions))
                    if item["operation"] not in COMPLETION_OPERATIONS
                ]
                report_action = next(
                    item
                    for item in reversed(actions)
                    if item["operation"] == "analysis_ready"
                )
                if completed:
                    report_action["operation"] = "analysis_completed"

                with self.assertRaisesRegex(
                    ValueError, "confirmation.*action|action.*confirmation"
                ):
                    self.restored_report(actions)

    def test_checkpoint_restore_rejects_action_projection_mutations(self):
        for mutation in (
            "missing",
            "duplicate",
            "extra",
            "status",
            "confirmation",
            "owner",
            "request_identity",
            "response_identity",
            "occurrence",
        ):
            with self.subTest(mutation=mutation):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                snapshot = latest_snapshot(actions)
                projection = snapshot["confirmation_action_projection"]
                if mutation == "missing":
                    projection.pop()
                elif mutation == "duplicate":
                    projection[1] = copy.deepcopy(projection[0])
                elif mutation == "extra":
                    projection.append(copy.deepcopy(projection[0]))
                elif mutation == "status":
                    projection[0]["status"] = "rejected"
                elif mutation == "confirmation":
                    projection[0]["confirmation"]["reason"] = (
                        "FIX23_SUBSTITUTED_CONFIRMATION"
                    )
                elif mutation == "owner":
                    projection[0]["owner"] = copy.deepcopy(
                        projection[1]["owner"]
                    )
                elif mutation == "request_identity":
                    projection[0]["request_identity"] = "FIX23_REQUEST"
                elif mutation == "response_identity":
                    projection[0]["response_identity"] = "FIX23_RESPONSE"
                else:
                    projection[0]["owner"]["occurrence_identity"] = (
                        "local_state_occurrence:v1:fix23"
                    )

                with self.assertRaisesRegex(
                    ValueError, "confirmation|action|owner|occurrence|identity"
                ):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=self.graph,
                        checkpoint=partial_checkpoint(self.checkpoint, actions),
                    )

    def test_evaluator_rejects_report_action_projection_mutations(self):
        for mutation in (
            "missing",
            "duplicate",
            "status",
            "confirmation",
            "owner",
            "request_identity",
            "response_identity",
            "occurrence",
        ):
            with self.subTest(mutation=mutation):
                report = copy.deepcopy(self.report)
                projection = report["metadata"][
                    "confirmation_action_projection"
                ]
                if mutation == "missing":
                    projection.pop()
                elif mutation == "duplicate":
                    projection[1] = copy.deepcopy(projection[0])
                elif mutation == "status":
                    projection[0]["status"] = "rejected"
                elif mutation == "confirmation":
                    projection[0]["confirmation"]["reason"] = (
                        "FIX23_SUBSTITUTED_CONFIRMATION"
                    )
                elif mutation == "owner":
                    projection[0]["owner"] = copy.deepcopy(
                        projection[1]["owner"]
                    )
                elif mutation == "request_identity":
                    projection[0]["request_identity"] = "FIX23_REQUEST"
                elif mutation == "response_identity":
                    projection[0]["response_identity"] = "FIX23_RESPONSE"
                else:
                    projection[0]["owner"]["occurrence_identity"] = (
                        "local_state_occurrence:v1:fix23"
                    )
                report = annotate_report_semantic_anchors(
                    self.graph.case_id,
                    self.graph.nodes,
                    report,
                    graph=self.graph,
                )

                with self.assertRaisesRegex(
                    (EvaluationSafetyError, EvaluationSchemaError),
                    "confirmation|action|owner|occurrence|identity",
                ):
                    compare_report(
                        report,
                        self.labels,
                        None,
                        graph=self.graph,
                    )


def revision_trace(*, formal_manifest: bool = True) -> dict:
    manifest = {
        "case_id": "fix23-revision-authority",
        "run_id": "fix23-run",
    }
    if formal_manifest:
        manifest.update(
            {
                "subject_revision": "git:active",
                "subject_revision_provenance": {
                    "method": "case_trace_config",
                    "source": "CaseTraceConfig.subjectRevision",
                    "bound_at": "case_start",
                    "case_id": "fix23-revision-authority",
                    "run_id": "fix23-run",
                },
            }
        )
    return {
        "case_id": "fix23-revision-authority",
        "manifest": manifest,
        "records": [],
    }


def authority_record(event_type: str, revision: object, **overrides) -> dict:
    data = {
        "subject_revision": "git:active",
        "revision_provenance_status": "valid",
        "revision_status": "matched",
    }
    if event_type == "change":
        data["revision_after"] = revision
    else:
        data["repository_revision"] = revision
    if event_type == "verification":
        data["effective_for_final_state"] = True
    data.update(overrides)
    return {
        "record_id": "{0}_{1}".format(event_type.replace(".", "_"), revision),
        "component": "fix23",
        "event_type": event_type,
        "data": data,
    }


class FormalManifestRevisionAuthorityTest(unittest.TestCase):
    def test_formal_manifest_requires_matching_subject_and_valid_provenance(self):
        for event_type in ("response.claim", "verification", "change"):
            for mutation in (
                "missing_subject",
                "mismatched_subject",
                "missing_provenance",
                "malformed_provenance",
            ):
                with self.subTest(event_type=event_type, mutation=mutation):
                    trace = revision_trace()
                    record = authority_record(event_type, 9)
                    if mutation == "missing_subject":
                        record["data"].pop("subject_revision")
                    elif mutation == "mismatched_subject":
                        record["data"]["subject_revision"] = "git:stale"
                    elif mutation == "missing_provenance":
                        record["data"].pop("revision_provenance_status")
                    else:
                        record["data"]["revision_provenance_status"] = (
                            "unprovenanced"
                        )
                    trace["records"] = [record]

                    graph = TraceGraph.from_trace(trace)
                    self.assertIsNone(graph.active_repository_revision())
                    self.assertFalse(
                        graph.active_revision_evidence_eligible(
                            "record:{0}_{1}".format(
                                event_type.replace(".", "_"),
                                9,
                            )
                        )
                    )

    def test_bound_lower_revision_wins_over_unbound_stale_high_revision(self):
        trace = revision_trace()
        stale = authority_record("response.claim", 99)
        stale["data"].pop("subject_revision")
        stale["data"].pop("revision_provenance_status")
        trace["records"] = [
            authority_record("change", 2),
            stale,
        ]

        self.assertEqual(
            TraceGraph.from_trace(trace).active_repository_revision(),
            2,
        )

    def test_malformed_formal_manifest_does_not_enable_legacy_omission(self):
        trace = revision_trace()
        trace["manifest"]["subject_revision_provenance"]["run_id"] = (
            "fix23-other-run"
        )
        record = authority_record("change", 5)
        record["data"].pop("subject_revision")
        record["data"].pop("revision_provenance_status")
        trace["records"] = [record]

        self.assertIsNone(
            TraceGraph.from_trace(trace).active_repository_revision()
        )

    def test_manifest_without_formal_revision_allows_legacy_omission(self):
        for event_type in ("response.claim", "verification", "change"):
            with self.subTest(event_type=event_type):
                trace = revision_trace(formal_manifest=False)
                record = authority_record(event_type, 0)
                record["data"].pop("subject_revision")
                record["data"].pop("revision_provenance_status")
                trace["records"] = [record]

                self.assertEqual(
                    TraceGraph.from_trace(trace).active_repository_revision(),
                    0,
                )

    def test_bound_zero_ties_and_non_boolean_nonnegative_checks_are_preserved(self):
        trace = revision_trace()
        trace["records"] = [
            authority_record("change", 0),
            authority_record("verification", 0),
            authority_record("response.claim", 0),
        ]
        self.assertEqual(
            TraceGraph.from_trace(trace).active_repository_revision(),
            0,
        )
        for invalid in (True, -1, 1.5, "2"):
            with self.subTest(invalid=invalid):
                invalid_trace = revision_trace()
                invalid_trace["records"] = [
                    authority_record("change", invalid)
                ]
                self.assertIsNone(
                    TraceGraph.from_trace(
                        invalid_trace
                    ).active_repository_revision()
                )


class Fix23PersistedIdentityTest(unittest.TestCase):
    def test_all_affected_persisted_identities_are_bumped(self):
        self.assertEqual(
            MODERN_REPORT_SCHEMA_VERSION,
            "recursive-attribution-report/v15",
        )
        self.assertEqual(REPORT_SCHEMA_VERSION, MODERN_REPORT_SCHEMA_VERSION)
        self.assertEqual(
            CHECKPOINT_SCHEMA_VERSION,
            "recursive-attribution-checkpoint/v14",
        )
        self.assertEqual(
            OUTPUT_SCHEMA_VERSION,
            "recursive-attribution-output/v3",
        )
        self.assertEqual(ACTION_STATE_SCHEMA, "recursive-analysis-actions/v12")
        self.assertEqual(
            COMPARISON_SCHEMA_VERSION,
            "recursive-attribution-comparison/v5",
        )
        self.assertEqual(JUDGMENT_CACHE_VERSION, "3.0")
        self.assertEqual(
            EVIDENCE_ELIGIBILITY_POLICY_IDENTITY,
            "graph-external-evidence-eligibility/v5",
        )
        self.assertEqual(
            ROOT_CONFIRMATION_PERSISTENCE_CONTRACT_VERSION,
            "recursive-root-confirmation/v16+resolution/v2"
            "+evidence-policy/v5+artifact-owner/v1+terminal-evidence/v2"
            "+local-state-owner/v1+action-projection/v6"
            "+response-identity/v1"
            "+step-action-projection/v1+confirmation-request-identity/v3"
            "+confirmation-request-projection/v2",
        )
        self.assertEqual(
            GLOBAL_CANDIDATE_PERSISTENCE_CONTRACT_VERSION,
            "global-candidate-judgment/v7+validation-envelope/v7"
            "+capsule/v7+evidence-policy/v5+local-state-owner/v1"
            "+global-pass-identity/v1+failure-action/v3"
            "+failure-projection/v4+terminal-record-schema/v3",
        )


if __name__ == "__main__":
    unittest.main()
