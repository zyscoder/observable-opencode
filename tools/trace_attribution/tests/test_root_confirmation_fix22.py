from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_state import (
    LocalStateOwner,
    annotate_report_semantic_anchors,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tools.trace_attribution.tests.test_root_confirmation_fix21 import (
    InjectedCheckpoint,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    FIXTURE_ROOT,
    NeedsExpansionSharedRootFusionJudge,
    SharedRootFusionJudge,
    run_fixture,
    shared_root_checkpoint_config,
    shared_root_trace,
)


STARTS = ("record:seed_one", "record:seed_two")
OBJECTIVE = "Confirm each active defect seed independently."


def completed_checkpoint(judge) -> tuple[dict, dict, CheckpointBundle, object]:
    trace = shared_root_trace()
    config = shared_root_checkpoint_config(trace, OBJECTIVE, STARTS)
    tempdir = tempfile.TemporaryDirectory()
    root = Path(tempdir.name) / "fix22.checkpoint"
    AgenticRecursiveAnalyzer(
        judge=judge,
        fusion_mode="retrieval-global",
        checkpoint=CheckpointBundle(root),
        checkpoint_config=config,
    ).analyze(
        TraceGraph.from_trace(trace),
        start_refs=STARTS,
        objective=OBJECTIVE,
        analysis_perspective="",
    )
    return trace, config, root, (
        tempdir,
        CheckpointBundle(root).restore(expected_config=config),
    )


def partial_checkpoint(checkpoint, actions):
    return replace(
        checkpoint,
        actions=tuple(
            item for item in actions if item["operation"] != "analysis_ready"
        ),
    )


def latest_snapshot(actions):
    return next(
        item
        for item in reversed(actions)
        if item["operation"] == "state_snapshot"
    )["payload"]


def completed_passes(payload):
    return [
        item
        for item in payload["investigation_journal"]
        if item.get("kind") == "global_candidate_pass"
        and item.get("status") == "completed"
    ]


class GlobalPassBijectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace, cls.config, cls.root, resources = completed_checkpoint(
            SharedRootFusionJudge()
        )
        cls.tempdir, cls.checkpoint = resources

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
            TraceGraph.from_trace(self.trace),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )

    def test_pending_and_completed_restore_reject_duplicate_derived_source(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                report_action = next(
                    item
                    for item in reversed(actions)
                    if item["operation"] == "analysis_ready"
                )
                report = report_action["payload"]["report"]
                judgments = report["metadata"]["global_candidate_judgments"]
                judgments[1] = copy.deepcopy(judgments[0])
                if completed:
                    report_action["operation"] = "analysis_completed"

                with self.assertRaisesRegex(ValueError, "global|owner|action"):
                    self.restored_report(actions)

    def test_report_restore_rejects_duplicate_global_pass_owner_with_distinct_payloads(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        report_action = next(
            item
            for item in reversed(actions)
            if item["operation"] == "analysis_ready"
        )
        report = report_action["payload"]["report"]
        passes = completed_passes(report)
        source = passes[0]
        target = passes[1]
        target["owner"] = copy.deepcopy(source["owner"])
        target["seed_ref"] = source["seed_ref"]
        target["hypothesis_id"] = source["hypothesis_id"]
        target["visit_key"] = source["visit_key"]
        report["metadata"]["global_candidate_judgments"][1]["owner"] = (
            copy.deepcopy(source["owner"])
        )
        report["metadata"]["candidate_compression"][1]["owner"] = (
            copy.deepcopy(source["owner"])
        )

        with self.assertRaisesRegex(ValueError, "duplicate|owner|global"):
            self.restored_report(actions)


class PartialActionPayloadBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace, cls.config, _, resources = completed_checkpoint(
            SharedRootFusionJudge()
        )
        cls.tempdir, cls.checkpoint = resources
        (
            cls.expansion_trace,
            cls.expansion_config,
            _,
            expansion_resources,
        ) = completed_checkpoint(NeedsExpansionSharedRootFusionJudge())
        cls.expansion_tempdir, cls.expansion_checkpoint = expansion_resources

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()
        cls.expansion_tempdir.cleanup()

    def assert_partial_rejected(self, checkpoint, trace, actions):
        with self.assertRaisesRegex(
            ValueError, "action|payload|global|confirmation|evidence|expansion"
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=TraceGraph.from_trace(trace),
                checkpoint=partial_checkpoint(checkpoint, actions),
            )

    def test_partial_restore_binds_full_seed_global_judgment_to_pass_action(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        ledger = latest_snapshot(actions)["seed_ledger"]
        ledger[0]["global_judgment"]["reason"] = (
            "FIX22_SAME_OWNER_REWRITTEN_GLOBAL_REASON"
        )

        self.assert_partial_rejected(
            self.checkpoint,
            self.trace,
            actions,
        )

    def test_partial_restore_binds_expansion_history_and_decisive_evidence(self):
        for field in (
            "expansion_history",
            "decisive_evidence",
            "candidate_refs",
        ):
            with self.subTest(field=field):
                actions = copy.deepcopy(list(self.expansion_checkpoint.actions))
                seed = latest_snapshot(actions)["seed_ledger"][0]
                if field == "expansion_history":
                    seed[field][0]["reason"] = (
                        "FIX22_SAME_OWNER_REWRITTEN_EXPANSION_REASON"
                    )
                else:
                    if field == "decisive_evidence":
                        extra = copy.deepcopy(seed[field][0])
                        extra["ref"] = seed["start_ref"]
                        seed[field].append(extra)
                        seed["decisive_evidence_refs"].append(
                            seed["start_ref"]
                        )
                    else:
                        seed[field].append(seed["start_ref"])

                self.assert_partial_rejected(
                    self.expansion_checkpoint,
                    self.expansion_trace,
                    actions,
                )

    def test_partial_restore_binds_confirmation_queue_and_journal_to_completed_action(self):
        for field in ("confirmation_queue", "confirmation_journal"):
            with self.subTest(field=field):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                entry = latest_snapshot(actions)[field][0]
                entry["confirmation"]["reason"] = (
                    "FIX22_SAME_OWNER_REWRITTEN_CONFIRMATION_REASON"
                )

                self.assert_partial_rejected(
                    self.checkpoint,
                    self.trace,
                    actions,
                )


def revision_authority_trace() -> dict:
    provenance = {
        "method": "case_trace_config",
        "source": "CaseTraceConfig.subjectRevision",
        "bound_at": "case_start",
        "case_id": "fix22-revision-authority",
        "run_id": "fix22-run",
    }
    return {
        "case_id": "fix22-revision-authority",
        "manifest": {
            "case_id": "fix22-revision-authority",
            "run_id": "fix22-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": provenance,
        },
        "records": [
            {
                "record_id": "current_change",
                "component": "tool",
                "event_type": "change",
                "data": {
                    "revision_after": 2,
                    "subject_revision": "git:active",
                    "revision_status": "matched",
                },
            },
            {
                "record_id": "stale_claim",
                "component": "result",
                "event_type": "response.claim",
                "data": {
                    "repository_revision": 99,
                    "subject_revision": "git:stale",
                    "revision_status": "stale",
                },
            },
        ],
    }


class EligibleRevisionAuthorityTest(unittest.TestCase):
    def test_stale_high_revision_cannot_displace_current_lower_revision(self):
        graph = TraceGraph.from_trace(revision_authority_trace())

        self.assertEqual(graph.active_repository_revision(), 2)
        self.assertTrue(
            graph.active_revision_evidence_eligible("record:current_change")
        )
        self.assertFalse(
            graph.active_revision_evidence_eligible("record:stale_claim")
        )

    def test_authority_filters_malformed_missing_audit_and_external_producers(self):
        for label, mutate in (
            (
                "boolean",
                lambda record: record["data"].update({"repository_revision": True}),
            ),
            (
                "negative",
                lambda record: record["data"].update({"repository_revision": -1}),
            ),
            (
                "missing",
                lambda record: record["data"].pop("repository_revision"),
            ),
            (
                "stale_status",
                lambda record: record["data"].update({"revision_status": "stale"}),
            ),
            (
                "stale_subject",
                lambda record: record["data"].update(
                    {"subject_revision": "git:stale"}
                ),
            ),
            (
                "invalid_provenance_status",
                lambda record: record["data"].update(
                    {"revision_provenance_status": "unprovenanced"}
                ),
            ),
            (
                "audit_only",
                lambda record: record["data"].update({"audit_only": True}),
            ),
            (
                "offline_only",
                lambda record: record["data"].update(
                    {"offline_only": True, "behavior_impact": "none"}
                ),
            ),
            (
                "external_ineligible",
                lambda record: record["data"].update(
                    {"eligible_for_attribution": False}
                ),
            ),
        ):
            with self.subTest(label=label):
                trace = revision_authority_trace()
                trace["records"] = [trace["records"][1]]
                trace["records"][0]["data"].update(
                    {
                        "subject_revision": "git:active",
                        "revision_status": "matched",
                    }
                )
                mutate(trace["records"][0])
                self.assertIsNone(
                    TraceGraph.from_trace(trace).active_repository_revision()
                )

    def test_unprovenanced_active_manifest_cannot_authorize_subject_revision(self):
        trace = revision_authority_trace()
        trace["records"] = [trace["records"][0]]
        trace["manifest"]["subject_revision_provenance"]["run_id"] = (
            "another-run"
        )

        self.assertIsNone(
            TraceGraph.from_trace(trace).active_repository_revision()
        )

    def test_valid_ties_and_zero_remain_authoritative(self):
        trace = revision_authority_trace()
        trace["records"] = [
            {
                "record_id": "zero_change",
                "component": "tool",
                "event_type": "change",
                "data": {
                    "revision_after": 0,
                    "subject_revision": "git:active",
                    "revision_status": "matched",
                },
            },
            {
                "record_id": "zero_verification",
                "component": "tool",
                "event_type": "verification",
                "data": {
                    "repository_revision": 0,
                    "effective_for_final_state": True,
                    "subject_revision": "git:active",
                    "revision_status": "matched",
                },
            },
        ]

        self.assertEqual(
            TraceGraph.from_trace(trace).active_repository_revision(),
            0,
        )


class EvaluatorAnalyzerParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        document = json.loads(
            (FIXTURE_ROOT / "multi_seed_claims.json").read_text(
                encoding="utf-8"
            )
        )
        document.pop("scripted_analysis")
        cls.graph = TraceGraph.from_trace(document)
        cls.report = annotate_report_semantic_anchors(
            cls.graph.case_id,
            cls.graph.nodes,
            run_fixture("multi_seed_claims.json").to_dict(),
            graph=cls.graph,
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
        global_graph = TraceGraph.from_trace(shared_root_trace())
        cls.global_graph = global_graph
        cls.global_report = annotate_report_semantic_anchors(
            global_graph.case_id,
            global_graph.nodes,
            AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
            ).analyze(
                global_graph,
                start_refs=("record:seed_one",),
                objective=OBJECTIVE,
                analysis_perspective="",
            ).to_dict(),
            graph=global_graph,
        )
        cls.global_labels = {
            **cls.labels,
            "case_id": global_graph.case_id,
        }

    def test_valid_report_reaches_scoring(self):
        result = compare_report(
            copy.deepcopy(self.report),
            self.labels,
            None,
            graph=self.graph,
        )

        self.assertIn("metrics", result)

    def test_evaluator_rejects_cross_seed_owner_transplant(self):
        report = copy.deepcopy(self.report)
        entries = report["visited_entries"]
        source = LocalStateOwner.from_dict(entries[0]["owner"])
        target = next(
            item
            for item in entries[1:]
            if item["owner"]["seed_binding_identity"]
            != source.seed_binding_identity
        )
        target_owner = LocalStateOwner.from_dict(target["owner"])
        target["owner"] = LocalStateOwner.create(
            seed_binding_identity=source.seed_binding_identity,
            hypothesis_id=target_owner.hypothesis_id,
            visit_key=target_owner.visit_key,
            occurrence_key="visited_node",
        ).to_dict()

        with self.assertRaisesRegex(
            EvaluationSafetyError, "owner|frontier|action|ledger"
        ):
            compare_report(report, self.labels, None, graph=self.graph)

    def test_evaluator_rejects_forged_owner_identity_components(self):
        for field in (
            "hypothesis_id",
            "visit_key",
            "occurrence_identity",
        ):
            with self.subTest(field=field):
                report = copy.deepcopy(self.report)
                original = LocalStateOwner.from_dict(
                    report["visited_entries"][0]["owner"]
                )
                report["visited_entries"][0]["owner"] = (
                    LocalStateOwner.create(
                        seed_binding_identity=original.seed_binding_identity,
                        hypothesis_id=(
                            "FIX22_FORGED_HYPOTHESIS"
                            if field == "hypothesis_id"
                            else original.hypothesis_id
                        ),
                        visit_key=(
                            "FIX22_FORGED_VISIT"
                            if field == "visit_key"
                            else original.visit_key
                        ),
                        occurrence_key=(
                            "FIX22_FORGED_OCCURRENCE"
                            if field == "occurrence_identity"
                            else "visited_node"
                        ),
                    ).to_dict()
                )

                with self.assertRaisesRegex(
                    EvaluationSafetyError, "owner|frontier|action|ledger"
                ):
                    compare_report(
                        report,
                        self.labels,
                        None,
                        graph=self.graph,
                    )

    def test_evaluator_rejects_missing_and_duplicate_global_derivations(self):
        for mutation in ("missing", "duplicate"):
            with self.subTest(mutation=mutation):
                report = copy.deepcopy(self.global_report)
                judgments = report["metadata"][
                    "global_candidate_judgments"
                ]
                if mutation == "missing":
                    judgments.pop()
                else:
                    judgments.append(copy.deepcopy(judgments[0]))

                with self.assertRaisesRegex(
                    EvaluationSafetyError,
                    "global|owner|payload|action",
                ):
                    compare_report(
                        report,
                        self.global_labels,
                        None,
                        graph=self.global_graph,
                    )

    def test_evaluator_rejects_same_owner_action_payload_rewrites(self):
        for field in (
            "global_judgment",
            "confirmation_journal",
            "candidate_refs",
        ):
            with self.subTest(field=field):
                report = copy.deepcopy(self.global_report)
                if field == "global_judgment":
                    report["seed_results"][0]["global_judgment"][
                        "reason"
                    ] = "FIX22_EVALUATOR_REWRITTEN_GLOBAL_REASON"
                else:
                    if field == "confirmation_journal":
                        report["metadata"]["confirmation_journal"][0][
                            "confirmation"
                        ]["reason"] = (
                            "FIX22_EVALUATOR_REWRITTEN_CONFIRMATION_REASON"
                        )
                    else:
                        report["seed_results"][0][field].append(
                            "record:seed_two"
                        )

                with self.assertRaisesRegex(
                    EvaluationSafetyError,
                    "global|confirmation|owner|payload|action",
                ):
                    compare_report(
                        report,
                        self.global_labels,
                        None,
                        graph=self.global_graph,
                    )


if __name__ == "__main__":
    unittest.main()
