from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    compare_report,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    LocalStateOwner,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    seed_binding_identity_for,
)
from trace_attribution.causal_judge import OfflineJudgeCapability
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _confirmation_request_identity,
    _investigation_owner_bindings,
)
from tools.trace_attribution.tests.test_root_confirmation_fix17 import (
    CountingJudge,
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
    revision_authority_trace,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    TwoHopSharedRootJudge,
    shared_root_checkpoint_config,
    two_hop_shared_root_trace,
)


def formal_record(
    record_id: str,
    event_type: str,
    *,
    component: str = "result",
    **data,
) -> dict:
    return {
        "record_id": record_id,
        "component": component,
        "event_type": event_type,
        "data": data,
    }


class FormalSubjectProvenanceBindingTest(unittest.TestCase):
    def test_every_formal_default_start_requires_matching_subject_and_provenance(self):
        default_records = (
            formal_record(
                "failed",
                "case.failed",
                component="evaluation",
                actual="The case failed.",
            ),
            formal_record(
                "defect",
                "case.observed_defect",
                component="evaluation",
                actual="The result is defective.",
            ),
            formal_record(
                "output",
                "response.output",
                text="Unbound final response.",
                is_final_for_case=True,
            ),
            formal_record(
                "completed",
                "case.completed",
                component="evaluation",
            ),
        )
        for record in default_records:
            with self.subTest(event_type=record["event_type"]):
                trace = revision_authority_trace()
                trace["records"] = [trace["records"][0], record]
                graph = TraceGraph.from_trace(trace)

                self.assertFalse(
                    graph.active_revision_start_eligible(
                        "record:{0}".format(record["record_id"])
                    )
                )
                self.assertNotIn(
                    "record:{0}".format(record["record_id"]),
                    graph.default_start_refs(),
                )

    def test_formal_revision_bearing_records_use_event_independent_binding(self):
        for field in ("repository_revision", "revision_before", "revision_after"):
            with self.subTest(field=field):
                trace = revision_authority_trace()
                trace["records"].append(
                    formal_record(
                        "revision_bearing",
                        "decision",
                        component="agent",
                        **{field: 2},
                    )
                )
                graph = TraceGraph.from_trace(trace)
                self.assertFalse(
                    graph.active_revision_evidence_eligible(
                        "record:revision_bearing"
                    )
                )

                trace["records"][-1]["data"].update(
                    {
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    }
                )
                self.assertTrue(
                    TraceGraph.from_trace(
                        trace
                    ).active_revision_evidence_eligible(
                        "record:revision_bearing"
                    )
                )

    def test_explicit_invalid_provenance_is_always_ineligible_but_legacy_omission_survives(self):
        legacy = {
            "case_id": "fix25-legacy",
            "records": [
                formal_record(
                    "legacy_output",
                    "response.output",
                    text="Legacy final output.",
                )
            ],
        }
        self.assertTrue(
            TraceGraph.from_trace(legacy).active_revision_evidence_eligible(
                "record:legacy_output"
            )
        )

        for value in ("stale", "unprovenanced", "", 7):
            with self.subTest(value=value):
                trace = copy.deepcopy(legacy)
                trace["records"][0]["data"][
                    "revision_provenance_status"
                ] = value
                self.assertFalse(
                    TraceGraph.from_trace(
                        trace
                    ).active_revision_evidence_eligible(
                        "record:legacy_output"
                    )
                )


def artifact_trace() -> dict:
    return {
        "case_id": "fix25-artifact",
        "artifacts": [
            {
                "artifact_id": "proof",
                "kind": "text",
                "path": "artifacts/proof.txt",
                "hash": "",
                "byte_length": 0,
            }
        ],
        "records": [
            {
                "record_id": "candidate",
                "component": "agent",
                "event_type": "decision",
                "artifact_refs": ["artifact:proof"],
                "data": {"summary": "The candidate introduced the defect."},
            }
        ],
    }


def artifact_state(root: Path, *, content: str = "verified proof"):
    trace = artifact_trace()
    encoded = content.encode("utf-8")
    trace["artifacts"][0]["hash"] = "sha256:{0}".format(
        hashlib.sha256(encoded).hexdigest()
    )
    trace["artifacts"][0]["byte_length"] = len(encoded)
    target = root / "artifacts" / "proof.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(encoded)
    graph = TraceGraph.from_trace(trace, artifact_root=root)
    state = RecursiveAnalysisState.create(
        graph=graph,
        start_refs=("record:candidate",),
        objective="Confirm the candidate.",
        analysis_perspective="",
    )
    item = state.frontier.pop()
    hypothesis = state.ledger.get(item.hypothesis_id)
    state.introduction_bindings.append(
        {
            "candidate_ref": item.node_ref,
            "defect_state_id": item.defect_state.defect_state_id,
            "defect_fingerprint": item.defect_state.fingerprint,
            "hypothesis_id": item.hypothesis_id,
            "hypothesis_semantic_hash": hypothesis.semantic_hash,
            "seed_binding_identity": item.seed_binding_identity,
            "seed_key": item.seed_binding_identity,
        }
    )
    queue_key = (
        item.hypothesis_id,
        item.node_ref,
        item.defect_state.fingerprint,
        item.seed_binding_identity,
    )
    queued = {
        "hypothesis_id": item.hypothesis_id,
        "hypothesis_semantic_hash": hypothesis.semantic_hash,
        "candidate_ref": item.node_ref,
        "defect_fingerprint": item.defect_state.fingerprint,
        "seed_binding_identity": item.seed_binding_identity,
        "seed_key": item.seed_binding_identity,
        "requested_by_ref": item.node_ref,
        "recursive_path": [item.node_ref],
        "checked_evidence_refs": ["artifact:proof"],
        "task_obligations": [],
        "analysis_perspective": "",
        "semantic_identity": _confirmation_request_identity(
            hypothesis_id=item.hypothesis_id,
            candidate_ref=item.node_ref,
            defect_fingerprint=item.defect_state.fingerprint,
            seed_binding_identity=item.seed_binding_identity,
        ),
        "status": "queued",
        "owner": LocalStateOwner.create(
            seed_binding_identity=item.seed_binding_identity,
            hypothesis_id=item.hypothesis_id,
            visit_key=item.visit_key,
            occurrence_key="confirmation_queue",
        ).to_dict(),
    }
    return graph, state, item, queued


class VerifiedArtifactConfirmationEvidenceTest(unittest.TestCase):
    def test_queue_and_confirmation_request_accept_complete_verified_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, state, _, queued = artifact_state(Path(directory))

            self.assertTrue(state.enqueue_confirmation(queued))
            state.validate_confirmation_queue_bound()
            request = AgenticRecursiveAnalyzer(
                judge=CountingJudge()
            )._build_confirmation_request(state, queued)

        artifact = next(
            item
            for item in request.supporting_evidence
            if item.get("resolved_ref") == "artifact:proof"
        )
        self.assertEqual(artifact["content"], "verified proof")
        self.assertEqual(
            set(artifact),
            {
                "artifact_id",
                "raw_ref",
                "resolved_ref",
                "canonical_ref",
                "resolution_status",
                "provenance_class",
                "content",
                "content_hash",
                "byte_count",
                "byte_range",
                "owner_reference",
                "owner_binding_identity",
                "missing",
                "truncated",
                "fact_kind",
            },
        )
        self.assertFalse(artifact["missing"])
        self.assertFalse(artifact["truncated"])

    def test_queue_rejects_missing_truncated_and_unresolved_artifacts(self):
        for mode in ("missing", "truncated", "unresolved"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                graph, state, _, queued = artifact_state(root)
                if mode == "missing":
                    (root / "artifacts" / "proof.txt").unlink()
                    graph._artifact_reader.read("proof", refresh=True)
                elif mode == "truncated":
                    (root / "artifacts" / "proof.txt").unlink()
                    graph.raw_trace["artifacts"][0]["semantic_slices"] = [
                        {
                            "byte_range": [0, 8],
                            "content": "verified",
                            "hash": hashlib.sha256(
                                b"verified"
                            ).hexdigest()[:16],
                        }
                    ]
                    graph = TraceGraph.from_trace(
                        graph.raw_trace, artifact_root=root
                    )
                    state.graph = graph
                else:
                    queued["checked_evidence_refs"] = ["artifact:absent"]

                self.assertFalse(state.enqueue_confirmation(queued))

    def test_duplicate_enqueue_creates_explicit_seed_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            graph, state, item, queued = artifact_state(Path(directory))
            self.assertTrue(state.enqueue_confirmation(queued))
            judgment = CausalStepJudgment(
                current_node_ref=item.node_ref,
                current_defect_status="present",
                current_defect_reason="The candidate requires confirmation.",
                candidate_introduction=True,
                confidence=0.9,
            )
            request = state.build_step_request(graph, item, ())
            suggestion = {
                "action": "request_root_confirmation",
                "arguments": {
                    "hypothesis_id": item.hypothesis_id,
                    "candidate_ref": item.node_ref,
                    "defect_fingerprint": item.defect_state.fingerprint,
                },
                "reason": "Confirm the active candidate.",
            }

            AgenticRecursiveAnalyzer(
                judge=CountingJudge()
            )._apply_control_directive(
                state, item, judgment, request, suggestion
            )

        result = state.seed_ledger[item.seed_binding_identity].to_result()
        self.assertIn("confirmation_enqueue_failed", result.blocking_reasons)
        self.assertTrue(
            any(
                branch.get("reason") == "confirmation_enqueue_failed"
                for branch in state.unresolved_branches
            )
        )


class ArtifactConfirmationJudge(OfflineJudgeCapability):
    def judge_step_offline(self, request):
        return CausalStepJudgment(
            current_node_ref=request.current_node.ref,
            current_defect_status="present",
            current_defect_reason="The artifact proves the candidate defect.",
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
                "reason": "Confirm against the verified artifact.",
            },
            confidence=0.9,
        )

    def confirm_candidate_offline(self, request):
        return RootConfirmation.confirmed(
            request.candidate_ref,
            excerpt="verified proof",
            reason="The complete verified artifact confirms the defect.",
            counterfactual="Removing the defective decision prevents it.",
            confidence=0.95,
            evidence_refs=["artifact:proof"],
        )


class ArtifactConfirmationRestoreEvaluatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name)
        trace = artifact_trace()
        content = b"verified proof"
        trace["artifacts"][0].update(
            {
                "hash": "sha256:{0}".format(
                    hashlib.sha256(content).hexdigest()
                ),
                "byte_length": len(content),
            }
        )
        target = cls.root / "artifacts" / "proof.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        cls.trace = trace
        cls.graph = TraceGraph.from_trace(
            trace, artifact_root=cls.root
        )
        cls.config = build_checkpoint_config(
            trace=trace,
            case_id=cls.graph.case_id,
            objective="Confirm the candidate.",
            analysis_perspective="",
            start_refs=["record:candidate"],
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:fix25-artifact",
            cache_identity="cache:fix25-artifact",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://fix25-artifact",
                "provider_error_threshold": 3,
            },
        )
        cls.checkpoint_root = cls.root / "artifact.checkpoint"
        cls.report = AgenticRecursiveAnalyzer(
            judge=ArtifactConfirmationJudge(),
            checkpoint=CheckpointBundle(cls.checkpoint_root),
            checkpoint_config=cls.config,
        ).analyze(
            cls.graph,
            start_refs=["record:candidate"],
            objective="Confirm the candidate.",
            analysis_perspective="",
        )
        cls.checkpoint = CheckpointBundle(
            cls.checkpoint_root
        ).restore(expected_config=cls.config)
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

    def restore(self, actions):
        return AgenticRecursiveAnalyzer(
            judge=ArtifactConfirmationJudge(),
            checkpoint=InjectedCheckpoint(
                replace(self.checkpoint, actions=tuple(actions)),
                self.checkpoint_root,
            ),
            checkpoint_config=self.config,
        ).analyze(
            self.graph,
            start_refs=["record:candidate"],
            objective="Confirm the candidate.",
            analysis_perspective="",
        )

    def test_live_partial_pending_completed_and_evaluator_accept_verified_artifact(self):
        self.assertEqual(self.report.analysis_outcome, "confirmed_root")
        self.assertEqual(
            self.report.confirmations[0].evidence_refs,
            ("artifact:proof",),
        )
        partial = RecursiveAnalysisState.from_checkpoint(
            graph=self.graph,
            checkpoint=partial_checkpoint(
                self.checkpoint,
                list(self.checkpoint.actions),
            ),
        )
        self.assertEqual(
            partial.confirmations[0].evidence_refs,
            ("artifact:proof",),
        )
        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                if completed:
                    next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "analysis_ready"
                    )["operation"] = "analysis_completed"
                self.assertEqual(
                    self.restore(actions).confirmations[0].evidence_refs,
                    ("artifact:proof",),
                )
        comparison = compare_report(
            annotate_report_semantic_anchors(
                self.graph.case_id,
                self.graph.nodes,
                self.report.to_dict(),
                graph=self.graph,
            ),
            self.labels,
            None,
            graph=self.graph,
        )
        self.assertTrue(comparison["safety"]["passed"])

    def test_restore_and_evaluator_reject_missing_artifact(self):
        (self.root / "artifacts" / "proof.txt").unlink()
        missing_graph = TraceGraph.from_trace(
            self.trace, artifact_root=self.root
        )
        try:
            with self.assertRaisesRegex(
                ValueError, "artifact|evidence|unresolved"
            ):
                RecursiveAnalysisState.from_checkpoint(
                    graph=missing_graph,
                    checkpoint=partial_checkpoint(
                        self.checkpoint,
                        list(self.checkpoint.actions),
                    ),
                )
            with self.assertRaisesRegex(
                EvaluationSafetyError, "artifact|evidence|unresolved"
            ):
                compare_report(
                    annotate_report_semantic_anchors(
                        missing_graph.case_id,
                        missing_graph.nodes,
                        self.report.to_dict(),
                        graph=missing_graph,
                    ),
                    self.labels,
                    None,
                    graph=missing_graph,
                )
        finally:
            (self.root / "artifacts" / "proof.txt").write_bytes(
                b"verified proof"
            )


def owner_occurrence(value: dict) -> str:
    return str(value.get("owner", {}).get("occurrence_identity") or "")


def relation_for_nested_predecessor(payload: dict) -> tuple[dict, dict]:
    relations = payload["causal_relations"]
    for judgment in payload["step_judgments"]:
        for predecessor in judgment.get("predecessors") or ():
            occurrence = owner_occurrence(predecessor)
            relation = next(
                (
                    item
                    for item in relations
                    if owner_occurrence(item) == occurrence
                ),
                None,
            )
            if relation is not None:
                return judgment, relation
    raise AssertionError("fixture has no nested predecessor relation")


class LocalOccurrenceRelationBijectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace = two_hop_shared_root_trace()
        cls.config = shared_root_checkpoint_config(
            cls.trace, OBJECTIVE, STARTS
        )
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name) / "fix25-relations.checkpoint"
        AgenticRecursiveAnalyzer(
            judge=TwoHopSharedRootJudge(),
            checkpoint=CheckpointBundle(cls.root),
            checkpoint_config=cls.config,
        ).analyze(
            TraceGraph.from_trace(cls.trace),
            start_refs=STARTS,
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        cls.checkpoint = CheckpointBundle(cls.root).restore(
            expected_config=cls.config
        )
        cls.graph = TraceGraph.from_trace(cls.trace)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def restored_report(self, actions):
        return AgenticRecursiveAnalyzer(
            judge=TwoHopSharedRootJudge(),
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

    def test_partial_restore_rejects_duplicate_step_occurrence_and_relation(self):
        for mutation in ("step", "relation"):
            with self.subTest(mutation=mutation):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                snapshot = latest_snapshot(actions)
                judgment, relation = relation_for_nested_predecessor(snapshot)
                if mutation == "step":
                    snapshot["step_judgments"].append(copy.deepcopy(judgment))
                else:
                    snapshot["causal_relations"].append(
                        copy.deepcopy(relation)
                    )

                with self.assertRaisesRegex(
                    ValueError, "duplicate|occurrence|bijection|predecessor"
                ):
                    RecursiveAnalysisState.from_checkpoint(
                        graph=self.graph,
                        checkpoint=partial_checkpoint(
                            self.checkpoint, actions
                        ),
                    )

    def test_partial_pending_completed_and_evaluator_reject_missing_relation(self):
        partial_actions = copy.deepcopy(list(self.checkpoint.actions))
        snapshot = latest_snapshot(partial_actions)
        _, relation = relation_for_nested_predecessor(snapshot)
        occurrence = owner_occurrence(relation)
        snapshot["causal_relations"] = [
            item
            for item in snapshot["causal_relations"]
            if owner_occurrence(item) != occurrence
        ]
        with self.assertRaisesRegex(
            ValueError, "bijection|predecessor|causal relation"
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(
                    self.checkpoint, partial_actions
                ),
            )

        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                report_action = next(
                    item
                    for item in reversed(actions)
                    if item["operation"] == "analysis_ready"
                )
                report = report_action["payload"]["report"]
                _, relation = relation_for_nested_predecessor(report)
                occurrence = owner_occurrence(relation)
                report["causal_relations"] = [
                    item
                    for item in report["causal_relations"]
                    if owner_occurrence(item) != occurrence
                ]
                if completed:
                    report_action["operation"] = "analysis_completed"
                with self.assertRaisesRegex(
                    ValueError, "bijection|predecessor|causal relation"
                ):
                    self.restored_report(actions)

        report = copy.deepcopy(
            next(
                item["payload"]["report"]
                for item in reversed(self.checkpoint.actions)
                if item["operation"] == "analysis_ready"
            )
        )
        _, relation = relation_for_nested_predecessor(report)
        occurrence = owner_occurrence(relation)
        report["causal_relations"] = [
            item
            for item in report["causal_relations"]
            if owner_occurrence(item) != occurrence
        ]
        labels = {
            "schema_version": "recursive-attribution-labels/v3",
            "case_id": self.graph.case_id,
            "roots": [],
            "conditions": [],
            "amplifiers": [],
            "forbidden_roots": [],
            "allowed_unresolved_outcomes": [
                "inconclusive",
                "partial_root_found",
            ],
        }
        with self.assertRaisesRegex(
            EvaluationSafetyError,
            "bijection|predecessor|causal relation",
        ):
            compare_report(report, labels, None, graph=self.graph)


def stale_seed_trace(trace: dict) -> dict:
    output = copy.deepcopy(trace)
    record = next(
        item
        for item in output["records"]
        if item["record_id"] == "seed_one"
    )
    record.setdefault("data", {})[
        "revision_provenance_status"
    ] = "stale"
    return output


def stale_report_entry(report: dict) -> tuple[dict, str]:
    seed = next(
        item
        for item in report["seed_results"]
        if item["start_ref"] == "record:seed_one"
    )
    seed_key = seed_binding_identity_for(
        seed["start_ref"], seed["defect_fingerprint"]
    )
    hypothesis = next(
        item
        for item in report["hypotheses"]
        if item["seed_binding_identity"] == seed_key
    )
    return (
        {
            "directive_kind": "evidence_investigation",
            "directive": {
                "arguments": {
                    "hypothesis_id": hypothesis["hypothesis_id"],
                }
            },
            "referenced_state": {
                "seed_binding": {
                    "seed_binding_identity": seed_key,
                }
            },
            "result": {
                "status": "success",
                "byte_count": 19,
            },
            "rejudge_linkage": {"status": "completed"},
        },
        seed_key,
    )


def stale_visit_only_entry(report: dict) -> dict:
    seed = next(
        item
        for item in report["seed_results"]
        if item["start_ref"] == "record:seed_one"
    )
    seed_key = seed_binding_identity_for(
        seed["start_ref"], seed["defect_fingerprint"]
    )
    stale_hypothesis_ids = {
        item["hypothesis_id"]
        for item in report["hypotheses"]
        if item["seed_binding_identity"] == seed_key
    }
    frontier = report["metadata"]["frontier_checkpoint"]
    lifecycle_items = [
        *frontier["queued"],
        *frontier["in_flight"],
        *[
            item["item"]
            for item in frontier["completed"]
        ],
    ]
    stale_visit_key = next(
        item["visit_key"]
        for item in lifecycle_items
        if item["hypothesis_id"] in stale_hypothesis_ids
    )
    return {
        "directive_kind": "evidence_investigation",
        "referenced_state": {
            "active_visit_key": stale_visit_key,
        },
        "result": {
            "status": "success",
            "byte_count": 23,
        },
        "rejudge_linkage": {"status": "completed"},
    }


class NestedInvestigationOwnerQuarantineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace, cls.config, cls.root, resources = completed_checkpoint(
            SharedRootFusionJudge()
        )
        cls.tempdir, cls.checkpoint = resources
        cls.stale_graph = TraceGraph.from_trace(stale_seed_trace(cls.trace))

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_shared_extractor_reads_every_nested_owner_location(self):
        bindings = _investigation_owner_bindings(
            {
                "owner": {
                    "seed_binding_identity": "seed:owner",
                    "hypothesis_id": "hyp:owner",
                    "visit_key": "visit:owner",
                },
                "active_visit": {
                    "hypothesis_id": "hyp:visit",
                    "visit_key": "visit:active",
                },
                "directive": {
                    "arguments": {"hypothesis_id": "hyp:directive"}
                },
                "hypothesis": {
                    "hypothesis_id": "hyp:nested",
                    "seed_binding_identity": "seed:hypothesis",
                },
                "seed_binding": {
                    "seed_binding_identity": "seed:nested"
                },
                "referenced_state": {
                    "active_visit_key": "visit:referenced",
                    "start_ref": "record:seed",
                },
            }
        )
        self.assertEqual(
            bindings["seed_binding_identities"],
            {"seed:owner", "seed:hypothesis", "seed:nested"},
        )
        self.assertEqual(
            bindings["hypothesis_ids"],
            {
                "hyp:owner",
                "hyp:visit",
                "hyp:directive",
                "hyp:nested",
            },
        )
        self.assertEqual(
            bindings["visit_keys"],
            {"visit:owner", "visit:active", "visit:referenced"},
        )
        self.assertEqual(bindings["start_refs"], {"record:seed"})

    def test_partial_restore_quarantines_nested_owner_and_rebuilds_counters(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        snapshot = latest_snapshot(actions)
        report = next(
            item["payload"]["report"]
            for item in reversed(actions)
            if item["operation"] == "analysis_ready"
        )
        entry, _ = stale_report_entry(report)
        snapshot["investigation_journal"].append(entry)
        snapshot["investigation_rounds"] += 1
        snapshot["investigation_result_bytes"] += 19

        restored = RecursiveAnalysisState.from_checkpoint(
            graph=self.stale_graph,
            checkpoint=partial_checkpoint(self.checkpoint, actions),
        )

        self.assertNotIn(entry, restored.investigation_journal)
        self.assertEqual(
            restored.investigation_rounds,
            sum(
                1
                for item in restored.investigation_journal
                if item.get("directive_kind") == "evidence_investigation"
            ),
        )
        self.assertEqual(
            restored.investigation_result_bytes,
            sum(
                int(item.get("result", {}).get("byte_count") or 0)
                for item in restored.investigation_journal
                if item.get("directive_kind") == "evidence_investigation"
            ),
        )

    def test_pending_and_completed_restore_share_nested_quarantine(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = copy.deepcopy(list(self.checkpoint.actions))
                report_action = next(
                    item
                    for item in reversed(actions)
                    if item["operation"] == "analysis_ready"
                )
                report = report_action["payload"]["report"]
                entry, _ = stale_report_entry(report)
                visit_entry = stale_visit_only_entry(report)
                report["investigation_journal"].extend(
                    [entry, visit_entry]
                )
                report["metadata"]["investigation_rounds"] += 2
                report["metadata"]["investigation_result_bytes"] += 42
                if completed:
                    report_action["operation"] = "analysis_completed"

                restored = AgenticRecursiveAnalyzer(
                    judge=SharedRootFusionJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedCheckpoint(
                        replace(
                            self.checkpoint, actions=tuple(actions)
                        ),
                        self.root,
                    ),
                    checkpoint_config=self.config,
                ).analyze(
                    self.stale_graph,
                    start_refs=STARTS,
                    objective=OBJECTIVE,
                    analysis_perspective="",
                )

                self.assertNotIn(
                    entry,
                    [
                        dict(item)
                        for item in restored.investigation_journal
                    ],
                )
                self.assertNotIn(
                    visit_entry,
                    [
                        dict(item)
                        for item in restored.investigation_journal
                    ],
                )
                retained = [
                    item
                    for item in restored.investigation_journal
                    if item.get("directive_kind")
                    == "evidence_investigation"
                ]
                self.assertEqual(
                    restored.metadata["investigation_rounds"],
                    len(retained),
                )
                self.assertEqual(
                    restored.metadata["investigation_result_bytes"],
                    sum(
                        int(item.get("result", {}).get("byte_count") or 0)
                        for item in retained
                    ),
                )


if __name__ == "__main__":
    unittest.main()
