from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_state import (
    FrontierItem,
    LocalStateOwner,
)
from trace_attribution.checkpoint import CheckpointBundle
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)
from tools.trace_attribution.tests.test_root_confirmation_fix20 import (
    final_json,
    reviewer_trace,
)
from tools.trace_attribution.tests.test_seed_attribution import (
    SharedRootFusionJudge,
    TwoHopSharedRootJudge,
    shared_root_checkpoint_config,
    shared_root_trace,
    two_hop_shared_root_trace,
)


VISIBLE_MARKER = "FIX21_CONFLICTING_TYPED_FACT"


class MappingOwnedTypedIdentityTest(unittest.TestCase):
    def assert_all_stages_hide_marker(self, graph: TraceGraph) -> None:
        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                self.assertNotIn(
                    VISIBLE_MARKER,
                    stable_json(final_json(graph, stage)),
                )

    def test_mapping_identity_keys_trigger_atomic_classification_in_all_final_json(self):
        payload = {
            "plain_business_wrapper": {
                "candidate_ref": "record:alternative",
                "resolved_ref": "record:decision",
                "summary": "{0}_plain".format(VISIBLE_MARKER),
            },
            "references": [
                {
                    "candidate_ref": "record:alternative",
                    "canonical_ref": "record:decision",
                    "summary": "{0}_references".format(VISIBLE_MARKER),
                }
            ],
            "facts": [
                [
                    {
                        "seed_ref": "record:alternative",
                        "ref": "record:decision",
                        "summary": "{0}_nested".format(VISIBLE_MARKER),
                    }
                ]
            ],
            "citations": {
                "nested": {
                    "candidate_ref": "record:missing",
                    "summary": "{0}_citations".format(VISIBLE_MARKER),
                }
            },
            "artifacts": json.dumps(
                [
                    {
                        "candidate_ref": "record:alternative",
                        "citation_ref": "record:decision",
                        "summary": "{0}_encoded".format(VISIBLE_MARKER),
                    }
                ],
                sort_keys=True,
            ),
        }

        self.assert_all_stages_hide_marker(
            TraceGraph.from_trace(reviewer_trace(payload))
        )

    def test_role_distinct_aliases_and_business_fields_remain_visible(self):
        payload = {
            "business": {
                "config_ref": "record:outside-business-domain",
                "customer_ref": "artifact:customer-42",
            },
            "relationship": {
                "source_ref": "record:decision",
                "target_ref": "record:defect",
                "summary": "FIX21_VALID_ROLE_ENDPOINTS",
            },
        }
        graph = TraceGraph.from_trace(reviewer_trace(payload))

        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                encoded = stable_json(final_json(graph, stage))
                self.assertIn("FIX21_VALID_ROLE_ENDPOINTS", encoded)
                self.assertIn("record:outside-business-domain", encoded)
                self.assertIn("artifact:customer-42", encoded)


class ContradictorySharedNodeJudge(TwoHopSharedRootJudge):
    def judge_step_offline(self, request):
        judgment = super().judge_step_offline(request)
        if request.current_node.ref != "record:shared_middle":
            return judgment
        seed_ref = request.recursive_context["downstream_path"][-1]
        return replace(
            judgment,
            current_defect_status=(
                "present" if seed_ref == "record:seed_one" else "absent"
            ),
            current_defect_reason="FIX21_{0}_JUDGMENT".format(
                seed_ref.removeprefix("record:")
            ),
        )


class InjectedCheckpoint:
    def __init__(self, state, root: Path) -> None:
        self.state = state
        self.output_commit_path = root / "not-committed"

    def initialize(self, config) -> None:
        del config

    def restore(self, *, expected_config):
        del expected_config
        return self.state


class ExactLocalStateOwnerTest(unittest.TestCase):
    starts = ("record:seed_one", "record:seed_two")
    objective = "Confirm each active defect seed independently."

    def test_live_shared_node_context_isolates_contradictory_seed_judgments(self):
        judge = ContradictorySharedNodeJudge()
        AgenticRecursiveAnalyzer(judge=judge).analyze(
            TraceGraph.from_trace(two_hop_shared_root_trace()),
            start_refs=self.starts,
            objective=self.objective,
            analysis_perspective="",
        )

        shared_requests = [
            request
            for request in judge.step_requests
            if request.current_node.ref == "record:shared_middle"
        ]
        self.assertEqual(len(shared_requests), 2)
        for request in shared_requests:
            active_seed = request.recursive_context["hypothesis"][
                "seed_binding_identity"
            ]
            downstream = request.recursive_context["downstream_judgments"]
            owner_seeds = {
                item["owner"]["seed_binding_identity"] for item in downstream
            }
            with self.subTest(active_seed=active_seed):
                self.assertEqual(owner_seeds, {active_seed})
                self.assertTrue(downstream)
                self.assertTrue(
                    all(
                        item["owner"]["hypothesis_id"]
                        and item["owner"]["visit_key"]
                        and item["owner"]["occurrence_identity"]
                        for item in downstream
                    )
                )

    def test_local_evidence_requires_the_current_exact_owner(self):
        graph = TraceGraph.from_trace(shared_root_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=self.starts,
            objective=self.objective,
            analysis_perspective="",
        )
        item = state.frontier.pop()
        foreign_item = FrontierItem.from_dict(state.frontier.snapshot()[0])
        owned = LocalStateOwner.create(
            seed_binding_identity=item.seed_binding_identity,
            hypothesis_id=item.hypothesis_id,
            visit_key=item.visit_key,
            occurrence_key="investigation_evidence:owned",
        )
        foreign = LocalStateOwner.create(
            seed_binding_identity=foreign_item.seed_binding_identity,
            hypothesis_id=foreign_item.hypothesis_id,
            visit_key=foreign_item.visit_key,
            occurrence_key="investigation_evidence:foreign",
        )
        state.investigation_evidence[item.visit_key] = [
            {
                "evidence_hash": "owned",
                "resolved_refs": [item.node_ref],
                "summary": "FIX21_OWNED_LOCAL_EVIDENCE",
                "owner": owned.to_dict(),
            },
            {
                "evidence_hash": "foreign",
                "resolved_refs": [foreign_item.node_ref],
                "summary": "FIX21_FOREIGN_LOCAL_EVIDENCE",
                "owner": foreign.to_dict(),
            },
            {
                "evidence_hash": "unowned",
                "resolved_refs": [item.node_ref],
                "summary": "FIX21_UNOWNED_LOCAL_EVIDENCE",
            },
        ]

        request = state.build_step_request(graph, item, ())
        encoded = stable_json(request.recursive_context)

        self.assertIn("FIX21_OWNED_LOCAL_EVIDENCE", encoded)
        self.assertNotIn("FIX21_FOREIGN_LOCAL_EVIDENCE", encoded)
        self.assertNotIn("FIX21_UNOWNED_LOCAL_EVIDENCE", encoded)

    def test_checkpoint_rejects_owned_local_evidence_without_action_entry(self):
        graph = TraceGraph.from_trace(shared_root_trace())
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=self.starts,
            objective=self.objective,
            analysis_perspective="",
        )
        item = state.frontier.pop()
        owner = LocalStateOwner.create(
            seed_binding_identity=item.seed_binding_identity,
            hypothesis_id=item.hypothesis_id,
            visit_key=item.visit_key,
            occurrence_key="investigation_evidence:orphan",
        )
        state.investigation_evidence[item.visit_key] = [
            {
                "evidence_hash": "orphan",
                "resolved_refs": [item.node_ref],
                "owner": owner.to_dict(),
            }
        ]

        with self.assertRaisesRegex(ValueError, "action"):
            state.action_checkpoint_payload()

    def test_partial_restore_cross_checks_owned_state_with_ledger_and_frontier(self):
        trace = two_hop_shared_root_trace()
        config = shared_root_checkpoint_config(
            trace,
            self.objective,
            self.starts,
            fusion_mode="off",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "recursive-owner.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=TwoHopSharedRootJudge(),
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=self.starts,
                objective=self.objective,
                analysis_perspective="",
            )
            checkpoint = CheckpointBundle(root).restore(expected_config=config)

            for field in (
                "step_judgments",
                "causal_relations",
                "visited_entries",
                "confirmation_journal",
            ):
                with self.subTest(field=field):
                    actions = copy.deepcopy(list(checkpoint.actions))
                    snapshot = next(
                        item
                        for item in reversed(actions)
                        if item["operation"] == "state_snapshot"
                    )
                    entries = snapshot["payload"][field]
                    target = entries[0]
                    replacement = next(
                        item["owner"]
                        for item in entries[1:]
                        if item["owner"]["seed_binding_identity"]
                        != target["owner"]["seed_binding_identity"]
                    )
                    target["owner"] = copy.deepcopy(replacement)
                    partial = replace(
                        checkpoint,
                        actions=tuple(
                            item
                            for item in actions
                            if item["operation"] != "analysis_ready"
                        ),
                    )

                    with self.assertRaisesRegex(ValueError, "owner"):
                        RecursiveAnalysisState.from_checkpoint(
                            graph=TraceGraph.from_trace(trace),
                            checkpoint=partial,
                        )

    def test_restore_rejects_cross_seed_global_pass_and_pending_report_owner(self):
        trace = shared_root_trace()
        config = shared_root_checkpoint_config(
            trace, self.objective, self.starts
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "global-owner.checkpoint"
            AgenticRecursiveAnalyzer(
                judge=SharedRootFusionJudge(),
                fusion_mode="retrieval-global",
                checkpoint=CheckpointBundle(root),
                checkpoint_config=config,
            ).analyze(
                TraceGraph.from_trace(trace),
                start_refs=self.starts,
                objective=self.objective,
                analysis_perspective="",
            )
            checkpoint = CheckpointBundle(root).restore(expected_config=config)

            partial_actions = copy.deepcopy(list(checkpoint.actions))
            snapshot = next(
                item
                for item in reversed(partial_actions)
                if item["operation"] == "state_snapshot"
            )
            passes = [
                item
                for item in snapshot["payload"]["investigation_journal"]
                if item.get("kind") == "global_candidate_pass"
                and item.get("status") == "completed"
            ]
            passes[0]["owner"] = copy.deepcopy(passes[1]["owner"])
            with self.assertRaisesRegex(ValueError, "owner"):
                RecursiveAnalysisState.from_checkpoint(
                    graph=TraceGraph.from_trace(trace),
                    checkpoint=replace(
                        checkpoint,
                        actions=tuple(
                            item
                            for item in partial_actions
                            if item["operation"] != "analysis_ready"
                        ),
                    ),
                )

            pending_actions = copy.deepcopy(list(checkpoint.actions))
            report_action = next(
                item
                for item in reversed(pending_actions)
                if item["operation"] == "analysis_ready"
            )
            judgments = report_action["payload"]["report"]["metadata"][
                "global_candidate_judgments"
            ]
            judgments[0]["owner"] = copy.deepcopy(judgments[1]["owner"])
            with self.assertRaisesRegex(ValueError, "owner"):
                AgenticRecursiveAnalyzer(
                    judge=SharedRootFusionJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedCheckpoint(
                        replace(checkpoint, actions=tuple(pending_actions)),
                        root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=self.starts,
                    objective=self.objective,
                    analysis_perspective="",
                )

            completed_actions = copy.deepcopy(pending_actions)
            next(
                item
                for item in reversed(completed_actions)
                if item["operation"] == "analysis_ready"
            )["operation"] = "analysis_completed"
            with self.assertRaisesRegex(ValueError, "owner"):
                AgenticRecursiveAnalyzer(
                    judge=SharedRootFusionJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedCheckpoint(
                        replace(checkpoint, actions=tuple(completed_actions)),
                        root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=self.starts,
                    objective=self.objective,
                    analysis_perspective="",
                )

            missing_actions = copy.deepcopy(list(checkpoint.actions))
            missing_report = next(
                item
                for item in reversed(missing_actions)
                if item["operation"] == "analysis_ready"
            )["payload"]["report"]
            missing_report["metadata"]["global_candidate_judgments"].pop()
            with self.assertRaisesRegex(ValueError, "global candidate judgment"):
                AgenticRecursiveAnalyzer(
                    judge=SharedRootFusionJudge(),
                    fusion_mode="retrieval-global",
                    checkpoint=InjectedCheckpoint(
                        replace(checkpoint, actions=tuple(missing_actions)),
                        root,
                    ),
                    checkpoint_config=config,
                ).analyze(
                    TraceGraph.from_trace(trace),
                    start_refs=self.starts,
                    objective=self.objective,
                    analysis_perspective="",
                )


if __name__ == "__main__":
    unittest.main()
