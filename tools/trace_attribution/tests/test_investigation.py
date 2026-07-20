from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.causal_judge import OfflineJudgeCapability
from trace_attribution.causal_state import CausalStepJudgment, PredecessorAssessment
from trace_attribution.graph import TraceGraph
from trace_attribution.investigation import (
    AttributionControlDirective,
    CausalInvestigationTools,
    InvestigationDirective,
    InvestigationResult,
)
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer


def relation(ref: str, relation_name: str = "unknown") -> PredecessorAssessment:
    return PredecessorAssessment(
        ref=ref,
        relation=relation_name,
        reason="More grounded evidence is required.",
        confidence=0.2,
        recurse=False,
        evidence_refs=(),
        missing_evidence=("artifact payload",),
    )


def judgment(
    ref: str,
    *,
    status: str = "unknown",
    suggested=None,
    introduction: bool = False,
    missing=("artifact payload",),
) -> CausalStepJudgment:
    return CausalStepJudgment(
        current_node_ref=ref,
        current_defect_status=status,
        current_defect_reason="The available evidence is incomplete.",
        predecessors=(),
        candidate_introduction=introduction,
        missing_evidence=missing,
        suggested_investigation=suggested,
        confidence=0.3 if status == "unknown" else 0.9,
    )


class ScriptedInvestigatingJudge(OfflineJudgeCapability):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def judge_step(self, request):
        self.requests.append(request)
        value = self.responses.pop(0)
        return value(request) if callable(value) else value

    def confirm_candidate(self, request):  # pragma: no cover - forbidden in Task 6
        raise AssertionError("Task 6 must not confirm roots")


def trace_with_artifact() -> dict:
    return {
        "case_id": "investigation-case",
        "manifest": {
            "case_id": "investigation-case",
            "task_obligations": ["Preserve the compatibility contract."],
        },
        "artifacts": [
            {
                "artifact_id": "artifact_1",
                "kind": "tool-output",
                "path": "artifacts/payload.txt",
                "hash": "sha256:payload",
            }
        ],
        "records": [
            {
                "record_id": "source",
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "compatibility owner contract"},
            },
            {
                "record_id": "decision",
                "component": "agent",
                "event_type": "decision",
                "source_refs": ["record:source"],
                "artifact_refs": ["artifact_1"],
                "data": {
                    "failure_type": "incomplete_decision",
                    "expected": "Preserve the contract.",
                    "actual": "The contract was omitted.",
                    "mechanism": "premature closure",
                    "scope": "repository_reasoning",
                    "message_id": "msg_1",
                    "artifact_id": "artifact_1",
                },
            },
        ],
        "dataflow_edges": [
            {
                "edge_id": "edge_1",
                "from": {"type": "record", "id": "source"},
                "to": {"type": "record", "id": "decision"},
                "relation": "context_selected_for_decision",
                "evidence_type": "recorded_dataflow",
                "evidence_refs": ["record:source"],
            }
        ],
    }


class InvestigationValueTest(unittest.TestCase):
    def test_directive_and_result_are_immutable_serializable_values(self):
        directive = InvestigationDirective.create(
            "inspect_node",
            {"ref": "record:decision"},
            requested_by_ref="record:decision",
            hypothesis_id="hyp_1",
            reason="Resolve missing node semantics.",
        )
        restored = InvestigationDirective.from_dict(directive.to_dict())
        self.assertEqual(restored, directive)
        self.assertEqual(json.loads(json.dumps(directive.to_dict())), directive.to_dict())
        with self.assertRaises(TypeError):
            directive.arguments["ref"] = "record:source"

        result = InvestigationResult.success(
            directive,
            requested_refs=["record:decision"],
            resolved_refs=["record:decision"],
            provenance=[{"ref": "record:decision", "provenance_class": "recorded"}],
            payload={"node": {"ref": "record:decision"}},
            byte_count=19,
        )
        self.assertEqual(InvestigationResult.from_dict(result.to_dict()), result)
        self.assertTrue(result.evidence_hash.startswith("sha256:"))

    def test_control_directives_are_separately_typed_and_validated(self):
        directive = AttributionControlDirective.create(
            "reject_hypothesis",
            {
                "hypothesis_id": "hyp_1",
                "reason": "Recorded output contradicts the claim.",
                "opposing_evidence_refs": ["record:source"],
            },
            requested_by_ref="record:decision",
        )
        self.assertEqual(directive.action, "reject_hypothesis")
        self.assertEqual(
            AttributionControlDirective.from_dict(directive.to_dict()), directive
        )
        with self.assertRaisesRegex(ValueError, "opposing evidence|verifier"):
            AttributionControlDirective.create(
                "reject_hypothesis",
                {"hypothesis_id": "hyp_1", "reason": "I prefer another explanation."},
                requested_by_ref="record:decision",
            )


class InvestigationToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "artifacts").mkdir()
        (root / "artifacts" / "payload.txt").write_text(
            "0123456789-compatibility-owner", encoding="utf-8"
        )
        self.graph = TraceGraph.from_trace(trace_with_artifact(), artifact_root=root)

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_node_is_grounded_and_does_not_mutate_graph(self):
        before = copy.deepcopy(self.graph.raw_trace)
        tools = CausalInvestigationTools(self.graph)
        directive = InvestigationDirective.create(
            "inspect_node",
            {"ref": "record:decision"},
            requested_by_ref="record:decision",
            reason="Inspect the active node.",
        )
        result = tools.execute(directive)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.requested_refs, ("record:decision",))
        self.assertEqual(result.resolved_refs, ("record:decision",))
        self.assertEqual(result.provenance[0]["provenance_class"], "recorded")
        self.assertEqual(self.graph.raw_trace, before)
        self.assertEqual(self.graph._hydrated_refs, set())

    def test_artifact_range_uses_grounded_resolver_budget_and_dedup(self):
        tools = CausalInvestigationTools(self.graph, max_artifact_bytes=8)
        directive = InvestigationDirective.create(
            "inspect_artifact",
            {"artifact_id": "artifact_1", "offset": 2, "length": 4},
            requested_by_ref="record:decision",
            reason="Inspect the omitted contract.",
        )
        first = tools.execute(directive)
        second = tools.execute(directive)
        self.assertEqual(first.payload["content"], "2345")
        self.assertEqual(first.byte_count, 4)
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.byte_count, 0)
        self.assertEqual(tools.artifact_bytes_used, 4)
        exhausted = tools.execute(
            InvestigationDirective.create(
                "inspect_artifact",
                {"artifact_id": "artifact_1", "offset": 10, "length": 5},
                requested_by_ref="record:decision",
                reason="Inspect another grounded range.",
            )
        )
        self.assertEqual(exhausted.status, "rejected")
        self.assertEqual(exhausted.rejection_reason, "artifact_byte_budget_exhausted")

    def test_all_allowed_tools_return_auditable_results(self):
        tools = CausalInvestigationTools(self.graph)
        cases = {
            "expand_upstream": {"ref": "record:decision"},
            "expand_downstream": {"ref": "record:source"},
            "inspect_episode": {"ref": "record:decision"},
            "inspect_context_lineage": {"message_id": "msg_1"},
            "inspect_task_obligations": {"scope": "case"},
            "compare_causal_paths": {
                "paths": [["record:source", "record:decision"], ["record:decision"]]
            },
            "search_semantic_nodes": {
                "query": "compatibility owner",
                "before_ref": "record:decision",
                "limit": 5,
            },
        }
        for tool_name, arguments in cases.items():
            with self.subTest(tool=tool_name):
                result = tools.execute(
                    InvestigationDirective.create(
                        tool_name,
                        arguments,
                        requested_by_ref="record:decision",
                        reason="Resolve a bounded evidence gap.",
                    )
                )
                self.assertEqual(result.status, "success")
                self.assertTrue(result.evidence_hash)
                self.assertTrue(result.provenance)

    def test_invalid_tool_arguments_are_rejected_without_side_effects(self):
        tools = CausalInvestigationTools(self.graph)
        unsupported = InvestigationDirective.create(
            "run_shell",
            {"command": "cat secret"},
            requested_by_ref="record:decision",
            reason="Not allowed.",
            validate_tool=False,
        )
        result = tools.execute(unsupported)
        self.assertEqual(result.status, "rejected")
        self.assertIn("unsupported", result.rejection_reason)
        malformed = tools.execute(
            InvestigationDirective.create(
                "inspect_artifact",
                {"artifact_id": "artifact_1", "offset": -1, "length": 3},
                requested_by_ref="record:decision",
                reason="Invalid range.",
                validate_arguments=False,
            )
        )
        self.assertEqual(malformed.status, "rejected")

    def test_same_tool_call_is_deduplicated_even_when_reason_wording_changes(self):
        tools = CausalInvestigationTools(self.graph)
        first = tools.execute(
            InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:decision"},
                requested_by_ref="record:decision",
                reason="Inspect the missing semantics.",
            )
        )
        second = tools.execute(
            InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:decision"},
                requested_by_ref="record:decision",
                reason="Look at semantics that are absent.",
            )
        )
        self.assertEqual(first.status, "success")
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.evidence_hash, first.evidence_hash)

    def test_compare_paths_rejects_unresolved_refs(self):
        tools = CausalInvestigationTools(self.graph)
        result = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:missing", "record:decision"]]},
                requested_by_ref="record:decision",
                reason="Compare a proposed path.",
            )
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("unresolved", result.rejection_reason)

    def test_unexpected_tool_failure_becomes_auditable_error(self):
        class BrokenTools(CausalInvestigationTools):
            def _inspect_node(self, directive):
                raise RuntimeError("unexpected local failure")

        result = BrokenTools(self.graph).execute(
            InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:decision"},
                requested_by_ref="record:decision",
                reason="Inspect the active node.",
            )
        )
        self.assertEqual(result.status, "error")
        self.assertIn("RuntimeError", result.error)
        self.assertTrue(result.evidence_hash)


class AnalyzerInvestigationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "artifacts").mkdir()
        (root / "artifacts" / "payload.txt").write_text(
            "compatibility owner contract", encoding="utf-8"
        )
        self.graph = TraceGraph.from_trace(trace_with_artifact(), artifact_root=root)

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_relation_inspects_artifact_then_rejudges(self):
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    suggested={
                        "tool": "inspect_artifact",
                        "arguments": {"artifact_id": "artifact_1", "offset": 0, "length": 28},
                        "reason": "The contract is only available in the artifact.",
                    },
                ),
                judgment(
                    "record:decision",
                    status="present",
                    introduction=True,
                    missing=(),
                ),
            ]
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            max_investigation_rounds=2,
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(
            [item["tool_name"] for item in report.investigation_journal],
            ["inspect_artifact"],
        )
        self.assertEqual(len(judge.requests), 2)
        self.assertEqual(len(report.step_judgments), 1)
        self.assertEqual(report.step_judgments[0].current_defect_status, "present")
        self.assertEqual(
            report.investigation_journal[0]["judgment_before_investigation"][
                "current_defect_status"
            ],
            "unknown",
        )
        self.assertIn("investigation_evidence", judge.requests[1].recursive_context)
        self.assertEqual(
            judge.requests[1].recursive_context["investigation_evidence"][0]["status"],
            "success",
        )
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertEqual(report.confirmed_roots, ())

    def test_identical_directive_executes_once_and_terminates_unresolved(self):
        repeated = {
            "tool": "inspect_node",
            "arguments": {"ref": "record:decision"},
            "reason": "Inspect the same node again.",
        }
        judge = ScriptedInvestigatingJudge(
            [judgment("record:decision", suggested=repeated), judgment("record:decision", suggested=repeated)]
        )
        report = AgenticRecursiveAnalyzer(judge=judge, max_investigation_rounds=3).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(len(report.investigation_journal), 2)
        self.assertEqual(report.investigation_journal[-1]["status"], "unchanged")
        self.assertIn("investigation_unchanged", [x["reason"] for x in report.metadata["unresolved_branches"]])

    def test_investigation_budget_exhaustion_is_explicitly_inconclusive(self):
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    suggested={
                        "tool": "inspect_node",
                        "arguments": {"ref": "record:decision"},
                        "reason": "Inspect missing semantics.",
                    },
                )
            ]
        )
        report = AgenticRecursiveAnalyzer(judge=judge, max_investigation_rounds=0).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.analysis_outcome, "inconclusive")
        self.assertTrue(report.metadata["investigation_budget_exhausted"])
        self.assertEqual(report.metadata["exhausted_budgets"]["investigation_rounds"], 1)

    def test_ineligible_evidence_directive_is_rejected_and_not_reentered(self):
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    status="present",
                    introduction=True,
                    missing=(),
                    suggested={
                        "tool": "inspect_node",
                        "arguments": {"ref": "record:decision"},
                        "reason": "Unnecessary extra exploration.",
                    },
                )
            ]
        )
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(len(judge.requests), 1)
        self.assertEqual(report.investigation_journal[0]["status"], "rejected")
        self.assertEqual(
            report.investigation_journal[0]["rejection_reason"],
            "investigation_not_evidence_eligible",
        )

    def test_request_root_confirmation_is_journaled_but_not_executed(self):
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    status="present",
                    introduction=True,
                    missing=(),
                    suggested={
                        "action": "request_root_confirmation",
                        "arguments": {"hypothesis_id": "active", "candidate_ref": "record:decision"},
                        "reason": "Candidate needs independent verification.",
                    },
                )
            ]
        )
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["directive_kind"], "attribution_control")
        self.assertEqual(report.investigation_journal[0]["status"], "deferred")
        self.assertEqual(report.confirmations, ())
        self.assertEqual(report.confirmed_roots, ())
        restored = type(report).from_dict(report.to_dict())
        self.assertEqual(restored.investigation_journal, report.investigation_journal)

    def test_reject_hypothesis_control_requires_resolved_opposition(self):
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    status="present",
                    introduction=True,
                    missing=(),
                    suggested={
                        "action": "reject_hypothesis",
                        "arguments": {
                            "hypothesis_id": "active",
                            "reason": "A missing ref allegedly contradicts the branch.",
                            "opposing_evidence_refs": ["record:missing"],
                        },
                        "reason": "A missing ref allegedly contradicts the branch.",
                    },
                )
            ]
        )
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        entry = report.investigation_journal[0]
        self.assertEqual(entry["status"], "rejected")
        self.assertIn("unresolved", entry["rejection_reason"])
        self.assertEqual(entry["ledger_before"], entry["ledger_after"])

    def test_artifact_investigation_shares_the_analysis_byte_budget(self):
        root = Path(self.temp.name)
        (root / "artifacts" / "payload.txt").write_text("x" * 33_000, encoding="utf-8")
        graph = TraceGraph.from_trace(trace_with_artifact(), artifact_root=root)
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    suggested={
                        "tool": "inspect_artifact",
                        "arguments": {
                            "artifact_id": "artifact_1",
                            "offset": 32_000,
                            "length": 4,
                        },
                        "reason": "Inspect bytes beyond the hydrated truncation boundary.",
                    },
                )
            ]
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge,
            max_artifact_bytes=32_002,
        ).analyze(
            graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["status"], "rejected")
        self.assertEqual(
            report.investigation_journal[0]["rejection_reason"],
            "artifact_byte_budget_exhausted",
        )
        self.assertEqual(report.metadata["artifact_bytes"], 32_000)


if __name__ == "__main__":
    unittest.main()
