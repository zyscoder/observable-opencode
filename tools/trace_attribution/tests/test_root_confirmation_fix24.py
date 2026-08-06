from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from scripts.evaluate_recursive_attribution import (
    EvaluationSafetyError,
    EvaluationSchemaError,
    compare_report,
)
from trace_attribution.causal_state import (
    LocalStateOwner,
    RecursiveAttributionReport,
    RootConfirmation,
    annotate_report_semantic_anchors,
    seed_binding_identity_for,
)
from trace_attribution.causal_judge import (
    root_confirmation_request_projection_identity,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
    _confirmation_action_projection,
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
    partial_checkpoint,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
)


COMPLETION_OPERATIONS = {"confirmation_completed", "confirmation_failed"}


def unknown_seed_terminal_action(action):
    output = copy.deepcopy(action)
    confirmation = RootConfirmation.from_dict(
        output["payload"]["confirmation"]
    )
    unknown_seed = seed_binding_identity_for(
        "record:unknown_cross_seed",
        confirmation.defect_fingerprint,
    )
    confirmation = replace(
        confirmation,
        seed_binding_identity=unknown_seed,
    )
    owner = LocalStateOwner.create(
        seed_binding_identity=unknown_seed,
        hypothesis_id=confirmation.hypothesis_id,
        visit_key=output["payload"]["action_projection"]["owner"][
            "visit_key"
        ],
        occurrence_key="global_candidate_pass",
    )
    factual_request_projection = copy.deepcopy(
        output["payload"]["action_projection"][
            "factual_request_projection"
        ]
    )
    factual_request_projection["facts"][
        "seed_binding_identity"
    ] = unknown_seed
    request_identity = root_confirmation_request_projection_identity(
        factual_request_projection
    )
    semantic_key = "confirmation:{0}".format(request_identity)
    projection = _confirmation_action_projection(
        operation=output["operation"],
        semantic_key=semantic_key,
        request_identity=request_identity,
        owner=owner.to_dict(),
        seed_key=unknown_seed,
        review_scope=output["payload"]["action_projection"][
            "review_scope"
        ],
        origin=output["payload"]["action_projection"]["origin"],
        confirmation=confirmation,
        physical_requests_reserved=output["payload"][
            "physical_requests_reserved"
        ],
        physical_request_delta=output["payload"][
            "physical_request_delta"
        ],
        physical_request_exact=output["payload"]["physical_request_exact"],
        factual_request_projection=factual_request_projection,
        artifact_evidence_envelopes=output["payload"][
            "action_projection"
        ]["artifact_evidence_envelopes"],
        evidence_disposition=output["payload"]["action_projection"][
            "evidence_disposition"
        ],
    )
    output["semantic_key"] = semantic_key
    output["payload"]["confirmation"] = confirmation.to_dict()
    output["payload"]["action_projection"] = projection
    return output


class UnknownSeedTerminalActionTest(unittest.TestCase):
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
        cls.terminal_action = next(
            item
            for item in cls.checkpoint.actions
            if item["operation"] in COMPLETION_OPERATIONS
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

    def actions_with_unknown_seed(self):
        actions = copy.deepcopy(list(self.checkpoint.actions))
        actions.append(unknown_seed_terminal_action(self.terminal_action))
        return actions

    def test_partial_restore_rejects_canonical_unknown_seed_terminal_action(self):
        with self.assertRaisesRegex(
            ValueError,
            "seed ledger owner|unknown seed|confirmation action",
        ):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(
                    self.checkpoint,
                    self.actions_with_unknown_seed(),
                ),
            )

    def test_pending_and_completed_restore_reject_unknown_seed_terminal_action(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                actions = self.actions_with_unknown_seed()
                if completed:
                    next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "analysis_ready"
                    )["operation"] = "analysis_completed"

                with self.assertRaisesRegex(
                    ValueError,
                    "seed ledger owner|unknown seed|confirmation action",
                ):
                    self.restored_report(actions)

    def test_evaluator_rejects_unknown_seed_action_projection(self):
        report = copy.deepcopy(self.report)
        report["metadata"]["confirmation_action_projection"].append(
            unknown_seed_terminal_action(self.terminal_action)["payload"][
                "action_projection"
            ]
        )
        report = annotate_report_semantic_anchors(
            self.graph.case_id,
            self.graph.nodes,
            report,
            graph=self.graph,
        )

        with self.assertRaisesRegex(
            (EvaluationSafetyError, EvaluationSchemaError),
            "seed owner|action|confirmation",
        ):
            compare_report(
                report,
                self.labels,
                None,
                graph=self.graph,
            )

    def test_duplicate_active_action_fails_and_active_multiseed_control_passes(self):
        restored = RecursiveAnalysisState.from_checkpoint(
            graph=self.graph,
            checkpoint=partial_checkpoint(
                self.checkpoint,
                list(self.checkpoint.actions),
            ),
        )
        self.assertEqual(
            {item.start_ref for item in restored.seed_results()},
            set(STARTS),
        )

        actions = copy.deepcopy(list(self.checkpoint.actions))
        actions.append(copy.deepcopy(self.terminal_action))
        with self.assertRaisesRegex(ValueError, "completed confirmation actions"):
            RecursiveAnalysisState.from_checkpoint(
                graph=self.graph,
                checkpoint=partial_checkpoint(self.checkpoint, actions),
            )


def start_record(
    record_id,
    event_type,
    *,
    component="fix24",
    active=True,
    **data,
):
    return {
        "record_id": record_id,
        "component": component,
        "event_type": event_type,
        "data": {
            "revision_status": "matched" if active else "stale",
            **data,
        },
    }


def start_trace(*records, manifest=None):
    trace = {
        "case_id": "fix24-default-start",
        "records": list(records),
    }
    if manifest is not None:
        trace["manifest"] = manifest
    return trace


class ActiveRevisionDefaultStartTest(unittest.TestCase):
    def assert_only_active(self, records, expected):
        graph = TraceGraph.from_trace(start_trace(*records))
        self.assertEqual(graph.default_start_refs(), ["record:{0}".format(expected)])

    def test_every_default_start_branch_prefilters_active_revision(self):
        branches = (
            (
                "final_claim",
                start_record(
                    "active",
                    "response.claim",
                    component="result",
                    is_final_for_case=True,
                ),
                start_record(
                    "stale",
                    "response.claim",
                    component="result",
                    active=False,
                    is_final_for_case=True,
                ),
            ),
            (
                "explicit_output",
                start_record(
                    "active",
                    "response.output",
                    component="result",
                    is_final_for_case=True,
                ),
                start_record(
                    "stale",
                    "response.output",
                    component="result",
                    active=False,
                    is_final_for_case=True,
                ),
            ),
            (
                "final_output",
                start_record(
                    "active",
                    "response.output",
                    component="result",
                    response_role="final",
                ),
                start_record(
                    "stale",
                    "response.output",
                    component="result",
                    active=False,
                    response_role="final",
                ),
            ),
            (
                "quality_flags",
                start_record(
                    "active",
                    "response.claim",
                    quality_flags=["unsupported"],
                ),
                start_record(
                    "stale",
                    "response.claim",
                    active=False,
                    quality_flags=["unsupported"],
                ),
            ),
            (
                "case_outcome",
                start_record("active", "case.completed"),
                start_record("stale", "case.completed", active=False),
            ),
            (
                "generic_fallback",
                start_record("active", "decision"),
                start_record("stale", "decision", active=False),
            ),
        )
        for branch, active, stale in branches:
            with self.subTest(branch=branch):
                self.assert_only_active((active, stale), "active")

    def test_stale_preferred_candidates_backfill_active_lower_branches(self):
        cases = (
            (
                (
                    start_record(
                        "active_final_output",
                        "response.output",
                        component="result",
                        response_role="final",
                    ),
                    start_record(
                        "stale_explicit_output",
                        "response.output",
                        component="result",
                        active=False,
                        is_final_for_case=True,
                    ),
                ),
                "active_final_output",
            ),
            (
                (
                    start_record(
                        "active_quality",
                        "response.claim",
                        quality_flags=["unsupported"],
                    ),
                    start_record(
                        "stale_output",
                        "response.output",
                        component="result",
                        active=False,
                        is_final_for_case=True,
                    ),
                ),
                "active_quality",
            ),
            (
                (
                    start_record("active_case", "case.completed"),
                    start_record(
                        "stale_quality",
                        "response.claim",
                        active=False,
                        quality_flags=["unsupported"],
                    ),
                ),
                "active_case",
            ),
            (
                (
                    start_record("active_fallback", "decision"),
                    start_record(
                        "stale_case",
                        "case.completed",
                        active=False,
                    ),
                ),
                "active_fallback",
            ),
            (
                (
                    start_record("active_after_failed", "decision"),
                    start_record(
                        "stale_failed",
                        "case.failed",
                        active=False,
                    ),
                ),
                "active_after_failed",
            ),
        )
        for records, expected in cases:
            with self.subTest(expected=expected):
                self.assert_only_active(records, expected)

    def test_no_active_candidate_returns_empty_and_analyzer_remains_conservative(self):
        branches = (
            start_record(
                "stale_claim",
                "response.claim",
                component="result",
                active=False,
                is_final_for_case=True,
            ),
            start_record(
                "stale_output",
                "response.output",
                component="result",
                active=False,
                is_final_for_case=True,
            ),
            start_record(
                "stale_quality",
                "response.claim",
                active=False,
                quality_flags=["unsupported"],
            ),
            start_record(
                "stale_case",
                "case.completed",
                active=False,
            ),
            start_record("stale_fallback", "decision", active=False),
        )
        for record in branches:
            with self.subTest(record=record["record_id"]):
                graph = TraceGraph.from_trace(start_trace(record))
                self.assertEqual(graph.default_start_refs(), [])

        judge = CountingJudge()
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(start_trace(branches[1])),
            objective="Assess the active final response.",
        )
        restored = RecursiveAttributionReport.from_dict(report.to_dict())
        self.assertEqual(report.start_refs, ())
        self.assertEqual(restored.start_refs, ())
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(judge.step_requests, [])
        self.assertEqual(judge.confirmation_requests, [])

    def test_formal_legacy_and_alias_revision_controls_select_active_output(self):
        formal_manifest = {
            "case_id": "fix24-default-start",
            "run_id": "fix24-run",
            "subject_revision": "git:active",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": "fix24-default-start",
                "run_id": "fix24-run",
            },
        }
        for formal in (False, True):
            with self.subTest(formal=formal):
                binding = (
                    {
                        "subject_revision": "git:active",
                        "revision_provenance_status": "valid",
                    }
                    if formal
                    else {}
                )
                trace = start_trace(
                    start_record(
                        "authority",
                        "change",
                        revision_after=2,
                        **binding,
                    ),
                    start_record(
                        "active_output",
                        "response.output",
                        component="result",
                        repository_revision=2,
                        is_final_for_case=True,
                        **binding,
                    ),
                    start_record(
                        "stale_output",
                        "response.output",
                        component="result",
                        repository_revision=1,
                        is_final_for_case=True,
                        **binding,
                    ),
                    manifest=formal_manifest if formal else None,
                )
                graph = TraceGraph.from_trace(trace)

                self.assertEqual(
                    graph.default_start_refs(),
                    ["record:active_output"],
                )
                self.assertTrue(
                    graph.active_revision_evidence_eligible("active_output")
                )
                self.assertTrue(
                    graph.active_revision_evidence_eligible(
                        "record:active_output"
                    )
                )
                self.assertFalse(
                    graph.active_revision_evidence_eligible("stale_output")
                )
                self.assertEqual(
                    TraceGraph.from_trace(copy.deepcopy(trace)).default_start_refs(),
                    graph.default_start_refs(),
                )


if __name__ == "__main__":
    unittest.main()
