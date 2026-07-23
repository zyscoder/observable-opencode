from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trace_attribution.causal_judge import (
    BoundedJudgeCallResult,
    BoundedJudgeCapability,
    OfflineJudgeCapability,
    build_causal_step_prompt,
)
from trace_attribution.causal_state import (
    CausalStepJudgment,
    FrontierItem,
    PredecessorAssessment,
    RootConfirmation,
)
from trace_attribution.errors import JudgeProviderUnavailable
from trace_attribution.graph import TraceGraph
from trace_attribution.investigation import (
    AttributionControlDirective,
    CausalInvestigationTools,
    InvestigationDirective,
    InvestigationResult,
)
from trace_attribution.recursive_analyzer import (
    AgenticRecursiveAnalyzer,
    RecursiveAnalysisState,
)


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

    def confirm_candidate(self, request):
        return RootConfirmation.unknown(
            request.candidate_ref, "The Task 6 evidence remains insufficient."
        )


class CountingAdjacency(dict):
    def __init__(self, values):
        super().__init__(values)
        self.visits = 0

    def __iter__(self):
        for ref in super().__iter__():
            self.visits += 1
            yield ref


def episode_adjacency_graph(upstream_count: int, downstream_count: int):
    upstream_ids = ["up_{0:03d}".format(index) for index in range(upstream_count)]
    downstream_ids = ["down_{0:03d}".format(index) for index in range(downstream_count)]
    episode_ids = [*upstream_ids, "anchor", *downstream_ids]
    records = [
        {
            "record_id": "{0}_member".format(record_id),
            "component": "agent",
            "event_type": "decision",
            "data": {
                "repository_revision": 1,
                "summary": "active member for {0}".format(record_id),
            },
        }
        for record_id in episode_ids
    ] + [
        {
            "record_id": record_id,
            "component": "progress",
            "event_type": "progress.episode",
            "data": {
                "summary": record_id,
                "member_refs": ["record:{0}_member".format(record_id)],
                "chronology_index": index,
            },
        }
        for index, record_id in enumerate(episode_ids, start=1)
    ]
    edges = [
        {
            "from": {"type": "record", "id": record_id},
            "to": {"type": "record", "id": "anchor"},
            "relation": "progress_episode_member",
            "evidence_type": "recorded_dataflow",
            "eligible_for_attribution": True,
        }
        for record_id in upstream_ids
    ] + [
        {
            "from": {"type": "record", "id": "anchor"},
            "to": {"type": "record", "id": record_id},
            "relation": "progress_episode_member",
            "evidence_type": "recorded_dataflow",
            "eligible_for_attribution": True,
        }
        for record_id in downstream_ids
    ]
    graph = TraceGraph.from_trace(
        {"case_id": "episode-boundary", "records": records, "dataflow_edges": edges}
    )
    upstream = CountingAdjacency(
        (ref, None) for ref in graph._upstream["record:anchor"]
    )
    downstream = CountingAdjacency(
        (ref, None) for ref in graph._downstream["record:anchor"]
    )
    graph._upstream["record:anchor"] = upstream
    graph._downstream["record:anchor"] = downstream
    return graph, upstream, downstream


def trace_with_artifact(artifact_content: str | None = None) -> dict:
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
                "hash": (
                    hashlib.sha256(artifact_content.encode("utf-8")).hexdigest()[:16]
                    if artifact_content is not None
                    else "sha256:payload"
                ),
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
                "opposing_evidence_refs": ["record:source"],
            },
            requested_by_ref="record:decision",
            reason="Recorded output contradicts the claim.",
        )
        self.assertEqual(directive.action, "reject_hypothesis")
        self.assertEqual(
            AttributionControlDirective.from_dict(directive.to_dict()), directive
        )
        with self.assertRaisesRegex(ValueError, "opposing_evidence_refs|exact keys"):
            AttributionControlDirective.create(
                "reject_hypothesis",
                {"hypothesis_id": "hyp_1", "reason": "I prefer another explanation."},
                requested_by_ref="record:decision",
            )
        with self.assertRaisesRegex(
            ValueError, "opposing_evidence_refs|independent_verifier_result|exact keys"
        ):
            AttributionControlDirective.create(
                "reject_hypothesis",
                {
                    "hypothesis_id": "hyp_1",
                    "independent_verifier_result": {"status": "rejected"},
                },
                requested_by_ref="record:decision",
                reason="A model-authored verifier object is not trusted.",
            )

    def test_persisted_values_reject_extra_keys_wrong_kind_and_tampered_hash(self):
        directive = InvestigationDirective.create(
            "inspect_node",
            {"ref": "record:decision"},
            requested_by_ref="record:decision",
            reason="Inspect the node.",
        )
        extra = {**directive.to_dict(), "nonce": "bypass"}
        with self.assertRaisesRegex(ValueError, "exact keys|unexpected"):
            InvestigationDirective.from_dict(extra)
        wrong_kind = {**directive.to_dict(), "directive_kind": "attribution_control"}
        with self.assertRaisesRegex(ValueError, "directive_kind"):
            InvestigationDirective.from_dict(wrong_kind)
        missing_identity = {**directive.to_dict(), "directive_id": ""}
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            InvestigationDirective.from_dict(missing_identity)

        control = AttributionControlDirective.create(
            "record_hypothesis",
            {"claim": "The source introduced the defect.", "candidate_ref": "record:source"},
            requested_by_ref="record:decision",
            reason="Preserve the candidate-specific claim.",
        )
        missing_control_identity = {**control.to_dict(), "directive_id": ""}
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            AttributionControlDirective.from_dict(missing_control_identity)

        result = InvestigationResult.success(
            directive,
            requested_refs=["record:decision"],
            resolved_refs=["record:decision"],
            provenance=[{"ref": "record:decision", "provenance_class": "recorded"}],
            payload={"node": {"ref": "record:decision"}},
            byte_count=11,
        )
        tampered = {**result.to_dict(), "byte_count": 12}
        with self.assertRaisesRegex(ValueError, "evidence_hash"):
            InvestigationResult.from_dict(tampered)

    def test_persisted_result_binds_complete_directive_and_request_identity(self):
        directive = InvestigationDirective.create(
            "inspect_node",
            {"ref": "record:decision"},
            requested_by_ref="record:decision",
            reason="Inspect the grounded node.",
        )
        result = InvestigationResult.success(
            directive,
            requested_refs=["record:decision"],
            resolved_refs=["record:decision"],
            provenance=[{"ref": "record:decision", "provenance_class": "recorded"}],
            payload={"node": {"ref": "record:decision"}},
            byte_count=19,
        )
        persisted = result.to_dict()
        self.assertTrue(persisted["result_identity_hash"].startswith("sha256:"))
        for field, replacement in (
            ("directive_id", "investigation:" + "0" * 24),
            ("tool_name", "run_shell"),
            ("requested_refs", ["record:secret"]),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    InvestigationResult.from_dict({**persisted, field: replacement})
        with self.assertRaisesRegex(ValueError, "requested_refs"):
            InvestigationResult.from_dict({**persisted, "requested_refs": [123]})
        with self.assertRaisesRegex(ValueError, "resolved_refs"):
            InvestigationResult.from_dict({**persisted, "resolved_refs": [None]})
        self.assertEqual(InvestigationResult.from_dict(persisted), result)

    def test_argument_schemas_reject_nonces_and_bound_paths(self):
        with self.assertRaisesRegex(ValueError, "unexpected"):
            InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:decision", "nonce": "dedupe-bypass"},
                requested_by_ref="record:decision",
                reason="Inspect the node.",
            )
        with self.assertRaisesRegex(ValueError, "path count"):
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:decision"]] * 9},
                requested_by_ref="record:decision",
                reason="Too many paths.",
            )
        with self.assertRaisesRegex(ValueError, "path length"):
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:decision"] * 33]},
                requested_by_ref="record:decision",
                reason="Path is too long.",
            )


class InvestigationToolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "artifacts").mkdir()
        artifact_content = "0123456789-compatibility-owner"
        (root / "artifacts" / "payload.txt").write_text(artifact_content, encoding="utf-8")
        self.graph = TraceGraph.from_trace(trace_with_artifact(artifact_content), artifact_root=root)

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

    def test_audit_only_external_fact_cannot_enter_judge_investigation_input(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "audit-only-investigation",
                "records": [
                    {
                        "record_id": "stale_external",
                        "component": "evaluation",
                        "event_type": "external.evaluation_fact",
                        "status": "failed",
                        "data": {
                            "status": "failed",
                            "subject_revision": "git:stale",
                            "trace_revision": "git:current",
                            "revision_status": "mismatched",
                            "revision_provenance_status": "valid",
                            "provenance": {
                                "method": "benchmark_grader",
                                "version": "1.0",
                            },
                            "eligible_for_decisive_judgment": False,
                            "offline_only": True,
                        },
                    },
                    {
                        "record_id": "normal_decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"text": "Inspect the cleanup contract."},
                    },
                    {
                        "record_id": "current",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {"text": "The cleanup contract failed."},
                    },
                ],
            }
        )

        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_node",
                {"ref": "record:stale_external"},
                requested_by_ref="record:stale_external",
                reason="Inspect the stale external fact.",
            )
        )

        self.assertIn("record:stale_external", graph.nodes)
        self.assertEqual(result.status, "rejected")
        self.assertIn("ineligible", result.rejection_reason)

    def test_semantic_investigation_excludes_audit_only_external_fact(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "audit-only-semantic-investigation",
                "records": [
                    {
                        "record_id": "stale_external",
                        "component": "evaluation",
                        "event_type": "external.evaluation_fact",
                        "status": "failed",
                        "data": {
                            "status": "failed",
                            "subject_revision": "git:stale",
                            "trace_revision": "git:current",
                            "revision_status": "mismatched",
                            "revision_provenance_status": "valid",
                            "provenance": {
                                "method": "benchmark_grader",
                                "version": "1.0",
                            },
                            "eligible_for_decisive_judgment": False,
                            "offline_only": True,
                            "assertion": "Inspect the cleanup contract.",
                        },
                    },
                    {
                        "record_id": "normal_decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"text": "Inspect the cleanup contract."},
                    },
                    {
                        "record_id": "current",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {"text": "The cleanup contract failed."},
                    },
                ],
            }
        )
        search = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "search_semantic_nodes",
                {
                    "query": "cleanup contract",
                    "before_ref": "record:current",
                },
                requested_by_ref="record:current",
                reason="Find relevant cleanup evidence.",
            )
        )
        self.assertEqual(
            [item["ref"] for item in search.payload["matches"]],
            ["record:normal_decision"],
        )

    def test_semantic_investigation_filters_stale_revisions_before_limit(self):
        def search(graph):
            return CausalInvestigationTools(graph).execute(
                InvestigationDirective.create(
                    "search_semantic_nodes",
                    {
                        "query": "cleanup contract boundary",
                        "before_ref": "record:current",
                        "limit": 1,
                    },
                    requested_by_ref="record:current",
                    reason="Find active cleanup evidence.",
                )
            )

        graph = TraceGraph.from_trace(
            {
                "case_id": "active-semantic-backfill",
                "manifest": {"subject_revision": "git:active"},
                "records": [
                    {
                        "record_id": "stale",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {
                            "text": "cleanup contract boundary",
                            "subject_revision": "git:stale",
                        },
                    },
                    {
                        "record_id": "active",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {
                            "text": "cleanup boundary",
                            "subject_revision": "git:active",
                        },
                    },
                    {
                        "record_id": "current",
                        "component": "result",
                        "event_type": "response.claim",
                        "data": {
                            "text": "cleanup failed",
                            "subject_revision": "git:active",
                        },
                    },
                ],
            }
        )

        result = search(graph)
        self.assertEqual(result.status, "success")
        self.assertEqual(
            [item["ref"] for item in result.payload["matches"]],
            ["record:active"],
        )

        stale_only = copy.deepcopy(graph.raw_trace)
        active = next(
            item for item in stale_only["records"] if item["record_id"] == "active"
        )
        active["data"]["subject_revision"] = "git:stale"
        empty = search(TraceGraph.from_trace(stale_only))
        self.assertEqual(empty.status, "success")
        self.assertEqual(list(empty.payload["matches"]), [])

    def test_context_lineage_investigation_rejects_audit_only_external_reference(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "audit-only-context-lineage",
                "records": [
                    {
                        "record_id": "forged_external",
                        "component": "evaluation",
                        "event_type": "external.evaluation_fact",
                        "status": "failed",
                        "data": {
                            "status": "failed",
                            "subject_revision": "git:forged",
                            "trace_revision": "git:forged",
                            "revision_status": "matched",
                            "revision_provenance_status": "valid",
                            "provenance": {
                                "method": "benchmark_grader",
                                "version": "1.0",
                            },
                            "eligible_for_decisive_judgment": True,
                        },
                    },
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"text": "Inspect context lineage."},
                    },
                ],
            }
        )
        graph.message_lineage["turns"] = [
            {"message_id": "msg_external", "source_ref": "record:forged_external"}
        ]

        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_context_lineage",
                {"message_id": "msg_external"},
                requested_by_ref="record:decision",
                reason="Reject the audit-only lineage reference.",
            )
        )

        self.assertEqual(result.status, "rejected")
        self.assertIn("ineligible", result.rejection_reason)

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
        self.assertGreater(first.byte_count, 4)
        self.assertEqual(first.artifact_byte_count, 4)
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

    def test_artifact_uses_ranged_read_and_rejects_eof_or_stale_identity(self):
        tools = CausalInvestigationTools(self.graph)
        ranged = InvestigationDirective.create(
            "inspect_artifact",
            {"artifact_id": "artifact_1", "offset": 3, "length": 4},
            requested_by_ref="record:decision",
            reason="Read one grounded range.",
        )
        with patch.object(Path, "read_bytes", side_effect=AssertionError("full read forbidden")):
            result = tools.execute(ranged)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.payload["content"], "3456")
        self.assertEqual(result.artifact_byte_count, 4)
        eof = tools.execute(
            InvestigationDirective.create(
                "inspect_artifact",
                {"artifact_id": "artifact_1", "offset": 10_000, "length": 4},
                requested_by_ref="record:decision",
                reason="Reject an empty out-of-range read.",
            )
        )
        self.assertEqual(eof.status, "rejected")
        self.assertIn("EOF", eof.rejection_reason)
        stale = tools.execute(
            InvestigationDirective.create(
                "inspect_artifact",
                {
                    "artifact_id": "artifact_1",
                    "offset": 0,
                    "length": 4,
                    "expected_hash": "sha256:stale",
                },
                requested_by_ref="record:decision",
                reason="Reject stale artifact identity.",
            )
        )
        self.assertEqual(stale.status, "rejected")
        self.assertIn("identity", stale.rejection_reason)
        (Path(self.temp.name) / "artifacts" / "payload.txt").write_text(
            "changed-artifact-content-with-new-size", encoding="utf-8"
        )
        changed = tools.execute(ranged)
        self.assertEqual(changed.status, "rejected")
        self.assertIn("identity", changed.rejection_reason)

    def test_artifact_investigation_rejects_unverified_unsafe_and_invalid_utf8_inputs(self):
        cases = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "case"
            (root / "artifacts").mkdir(parents=True)
            outside = base / "outside.txt"
            outside.write_text("outside secret", encoding="utf-8")
            (root / "artifacts" / "linked.txt").symlink_to(outside)
            invalid = b"valid-prefix\xffsecret"
            (root / "artifacts" / "invalid.txt").write_bytes(invalid)
            tampered = "tampered secret"
            (root / "artifacts" / "tampered.txt").write_text(tampered, encoding="utf-8")
            cases.extend([
                (str(outside), hashlib.sha256(outside.read_bytes()).hexdigest(), outside.stat().st_size),
                ("../outside.txt", hashlib.sha256(outside.read_bytes()).hexdigest(), outside.stat().st_size),
                ("artifacts/linked.txt", hashlib.sha256(outside.read_bytes()).hexdigest(), outside.stat().st_size),
                ("artifacts/invalid.txt", hashlib.sha256(invalid).hexdigest(), len(invalid)),
                ("artifacts/tampered.txt", hashlib.sha256(b"expected").hexdigest(), len(tampered)),
            ])
            for path_value, digest, byte_length in cases:
                with self.subTest(path=path_value):
                    trace = trace_with_artifact(None)
                    trace["artifacts"][0].update({
                        "path": path_value,
                        "hash": digest,
                        "byte_length": byte_length,
                    })
                    graph = TraceGraph.from_trace(trace, artifact_root=root)
                    result = CausalInvestigationTools(graph).execute(
                        InvestigationDirective.create(
                            "inspect_artifact",
                            {"artifact_id": "artifact_1", "offset": 0, "length": 64},
                            requested_by_ref="record:decision",
                            reason="Reject unverified artifact content.",
                        )
                    )

                    self.assertEqual(result.status, "rejected")
                    self.assertNotIn("secret", json.dumps(result.to_dict()))

    def test_all_allowed_tools_return_auditable_results(self):
        self.graph.message_lineage["turns"] = [
            {"message_id": "msg_1", "source_ref": "record:decision"}
        ]
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
        self.assertNotEqual(second.evidence_hash, first.evidence_hash)
        self.assertEqual(second.payload, first.payload)

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

    def test_compare_paths_requires_directed_non_temporal_grounded_hops(self):
        tools = CausalInvestigationTools(self.graph)
        valid = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:source", "record:decision"]]},
                requested_by_ref="record:decision",
                reason="Validate the recorded causal direction.",
            )
        )
        self.assertEqual(valid.status, "success")
        self.assertTrue(valid.payload["paths"][0]["valid"])

        reversed_result = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:decision", "record:source"]]},
                requested_by_ref="record:decision",
                reason="Reject the reversed direction.",
            )
        )
        self.assertEqual(reversed_result.status, "rejected")
        self.assertIn("grounded hop", reversed_result.rejection_reason)

        temporal_trace = trace_with_artifact()
        temporal_trace["records"][1]["source_refs"] = []
        temporal_trace["dataflow_edges"] = [
            {
                "from": {"type": "record", "id": "source"},
                "to": {"type": "record", "id": "decision"},
                "relation": "temporal_adjacency",
                "evidence_type": "temporal_only",
                "eligible_for_attribution": True,
            }
        ]
        temporal = CausalInvestigationTools(
            TraceGraph.from_trace(temporal_trace, artifact_root=Path(self.temp.name))
        ).execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:source", "record:decision"]]},
                requested_by_ref="record:decision",
                reason="Reject temporal-only adjacency.",
            )
        )
        self.assertEqual(temporal.status, "rejected")

    def test_compare_paths_rejects_every_path_containing_audit_only_external_fact(self):
        trace = trace_with_artifact()
        trace["records"].insert(
            1,
            {
                "record_id": "forged_external",
                "component": "evaluation",
                "event_type": "external.evaluation_fact",
                "status": "failed",
                "data": {
                    "status": "failed",
                    "subject_revision": "git:forged",
                    "trace_revision": "git:forged",
                    "revision_status": "matched",
                    "revision_provenance_status": "valid",
                    "provenance": {
                        "method": "benchmark_grader",
                        "version": "1.0",
                    },
                    "eligible_for_decisive_judgment": True,
                },
            },
        )
        trace["dataflow_edges"].extend(
            [
                {
                    "from": {"type": "record", "id": "source"},
                    "to": {"type": "external_evaluation", "id": "forged_external"},
                    "relation": "external_evaluation_observed",
                    "evidence_type": "external_grader",
                    "eligible_for_attribution": True,
                },
                {
                    "from": {"type": "external_evaluation", "id": "forged_external"},
                    "to": {"type": "record", "id": "decision"},
                    "relation": "external_evaluation_observed",
                    "evidence_type": "external_grader",
                    "eligible_for_attribution": True,
                },
            ]
        )
        tools = CausalInvestigationTools(
            TraceGraph.from_trace(trace, artifact_root=Path(self.temp.name))
        )

        one_node = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:forged_external"]]},
                requested_by_ref="record:decision",
                reason="Reject an audit-only singleton path.",
            )
        )
        multi_node = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {
                    "paths": [
                        [
                            "record:source",
                            "record:forged_external",
                            "record:decision",
                        ]
                    ]
                },
                requested_by_ref="record:decision",
                reason="Reject an audit-only multi-node path.",
            )
        )
        normal = tools.execute(
            InvestigationDirective.create(
                "compare_causal_paths",
                {"paths": [["record:source", "record:decision"]]},
                requested_by_ref="record:decision",
                reason="Keep normal recorded paths available.",
            )
        )

        self.assertEqual(one_node.status, "rejected")
        self.assertIn("ineligible", one_node.rejection_reason)
        self.assertEqual(multi_node.status, "rejected")
        self.assertIn("ineligible", multi_node.rejection_reason)
        self.assertEqual(normal.status, "success")

    def test_unresolved_context_and_obligation_selectors_are_explicit(self):
        tools = CausalInvestigationTools(self.graph)
        missing_message = tools.execute(
            InvestigationDirective.create(
                "inspect_context_lineage",
                {"message_id": "does-not-exist"},
                requested_by_ref="record:decision",
                reason="Resolve a missing context message.",
            )
        )
        self.assertEqual(missing_message.status, "rejected")
        self.assertEqual(missing_message.resolved_refs, ())

        with self.assertRaisesRegex(ValueError, "scope"):
            InvestigationDirective.create(
                "inspect_task_obligations",
                {"scope": "fabricated-scope"},
                requested_by_ref="record:decision",
                reason="Reject an ignored selector.",
            )
        case = tools.execute(
            InvestigationDirective.create(
                "inspect_task_obligations",
                {"scope": "case"},
                requested_by_ref="record:decision",
                reason="Inspect case obligations.",
            )
        )
        manifest = tools.execute(
            InvestigationDirective.create(
                "inspect_task_obligations",
                {"scope": "manifest"},
                requested_by_ref="record:decision",
                reason="Inspect manifest obligations.",
            )
        )
        self.assertEqual(case.status, "success")
        self.assertEqual(manifest.status, "success")
        self.assertEqual(case.payload["obligations"], manifest.payload["obligations"])
        self.assertEqual(case.evidence_hash, manifest.evidence_hash)

    def test_context_lineage_matches_exact_structured_message_identity(self):
        self.graph.message_lineage = {
            "turns": [
                {"message_id": "msg_10", "record_refs": ["record:source"]},
                {"message_id": "msg_1", "record_refs": ["record:decision"]},
            ],
            "edges": [
                {
                    "from_message_id": "msg_1",
                    "to_message_id": "msg_2",
                    "from_ref": "record:source",
                    "to_ref": "record:decision",
                }
            ],
        }
        result = CausalInvestigationTools(self.graph).execute(
            InvestigationDirective.create(
                "inspect_context_lineage",
                {"message_id": "msg_1"},
                requested_by_ref="record:decision",
                reason="Resolve only the exact message identity and endpoints.",
            )
        )
        self.assertEqual(result.status, "success")
        paths = [item["path"] for item in result.payload["matches"]]
        self.assertEqual(
            paths,
            ["message_lineage.turns[1]", "message_lineage.edges[0]"],
        )
        self.assertNotIn("msg_10", json.dumps(result.to_dict()["payload"]))

    def test_bounded_adjacency_visits_at_most_limit_plus_one(self):
        trace = trace_with_artifact()
        trace["records"] = [
            {
                "record_id": "source_{0:03d}".format(index),
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "source {0}".format(index)},
            }
            for index in range(200)
        ] + [
            {
                "record_id": "target",
                "component": "agent",
                "event_type": "decision",
                "source_refs": ["record:source_{0:03d}".format(index) for index in range(200)],
                "data": {"failure_type": "bounded traversal"},
            }
        ]
        trace["dataflow_edges"] = []
        graph = TraceGraph.from_trace(trace)

        adjacency = CountingAdjacency(
            (ref, None) for ref in graph._upstream["record:target"]
        )
        graph._upstream["record:target"] = adjacency
        scan = graph.bounded_upstream_refs("record:target", limit=1)
        self.assertEqual(scan.refs, ("record:source_000",))
        self.assertTrue(scan.truncated)
        self.assertEqual(scan.inspected_count, 2)
        self.assertTrue(scan.scan_truncated)
        self.assertEqual(adjacency.visits, 2)

    def test_relation_filter_cannot_scan_102_entries_to_fill_one_match(self):
        records = [
            {
                "record_id": "source_{0:03d}".format(index),
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "source {0}".format(index)},
            }
            for index in range(102)
        ] + [
            {
                "record_id": "target",
                "component": "agent",
                "event_type": "decision",
                "data": {"failure_type": "bounded relation scan"},
            }
        ]
        edges = [
            {
                "from": {"type": "record", "id": "source_{0:03d}".format(index)},
                "to": {"type": "record", "id": "target"},
                "relation": "wanted" if index == 101 else "noise",
                "evidence_type": "recorded_dataflow",
                "eligible_for_attribution": True,
            }
            for index in range(102)
        ]
        graph = TraceGraph.from_trace(
            {"case_id": "mixed-adjacency", "records": records, "dataflow_edges": edges}
        )
        adjacency = CountingAdjacency(
            (ref, None) for ref in graph._upstream["record:target"]
        )
        graph._upstream["record:target"] = adjacency
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "expand_upstream",
                {
                    "ref": "record:target",
                    "limit": 1,
                    "relation_filter": ["wanted"],
                },
                requested_by_ref="record:target",
                reason="Bound both physical scanning and matched output.",
            )
        )
        self.assertEqual(adjacency.visits, 2)
        self.assertEqual(result.payload["nodes"], ())
        self.assertEqual(result.payload["inspected_count"], 2)
        self.assertTrue(result.payload["scan_truncated"])
        self.assertTrue(result.truncated)

    def test_episode_scan_has_one_total_physical_budget_across_adjacency(self):
        records = [
            {
                "record_id": "source_{0:03d}".format(index),
                "component": "context",
                "event_type": "context.snapshot",
                "data": {"text": "not an episode"},
            }
            for index in range(165)
        ] + [
            {
                "record_id": "target",
                "component": "agent",
                "event_type": "decision",
                "source_refs": [
                    "record:source_{0:03d}".format(index) for index in range(165)
                ],
                "data": {"failure_type": "bounded episode scan"},
            }
        ]
        graph = TraceGraph.from_trace(
            {"case_id": "mixed-episode", "records": records, "dataflow_edges": []}
        )
        adjacency = CountingAdjacency(
            (ref, None) for ref in graph._upstream["record:target"]
        )
        graph._upstream["record:target"] = adjacency
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:target"},
                requested_by_ref="record:target",
                reason="Bound total episode-neighbor scanning.",
            )
        )
        self.assertEqual(adjacency.visits, 0)
        self.assertEqual(result.payload["episodes"], ())
        self.assertEqual(result.payload["adjacency_scan"]["inspected_count"], 0)
        self.assertFalse(result.payload["adjacency_scan"]["scan_truncated"])
        self.assertFalse(result.truncated)

    def test_episode_full_output_uses_last_physical_slot_to_probe_downstream(self):
        graph, upstream, downstream = episode_adjacency_graph(64, 1)
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Use the final physical slot even though output is full.",
            )
        )
        self.assertEqual((upstream.visits, downstream.visits), (0, 0))
        self.assertEqual(len(result.payload["episodes"]), 64)
        self.assertTrue(result.truncated)
        self.assertEqual(result.payload["adjacency_scan"]["inspected_count"], 65)
        self.assertFalse(result.payload["adjacency_scan"]["scan_truncated"])

    def test_episode_full_output_without_downstream_is_scan_complete(self):
        graph, upstream, downstream = episode_adjacency_graph(64, 0)
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Distinguish output truncation from complete adjacency scanning.",
            )
        )
        self.assertEqual((upstream.visits, downstream.visits), (0, 0))
        self.assertEqual(result.payload["adjacency_scan"]["inspected_count"], 64)
        self.assertFalse(result.payload["adjacency_scan"]["scan_truncated"])
        self.assertTrue(result.truncated)

    def test_episode_one_remaining_output_slot_includes_downstream(self):
        graph, upstream, downstream = episode_adjacency_graph(62, 1)
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Fill the final output slot from downstream adjacency.",
            )
        )
        self.assertEqual((upstream.visits, downstream.visits), (0, 0))
        self.assertEqual(len(result.payload["episodes"]), 64)
        self.assertIn("record:down_000", result.resolved_refs)
        self.assertFalse(result.truncated)
        self.assertFalse(result.payload["adjacency_scan"]["scan_truncated"])

    def test_episode_upstream_scan_truncation_blocks_downstream_truthfully(self):
        graph, upstream, downstream = episode_adjacency_graph(65, 1)
        result = CausalInvestigationTools(graph).execute(
            InvestigationDirective.create(
                "inspect_episode",
                {"ref": "record:anchor"},
                requested_by_ref="record:anchor",
                reason="Stop after upstream physical scan exhaustion.",
            )
        )
        self.assertEqual((upstream.visits, downstream.visits), (0, 0))
        self.assertEqual(result.payload["adjacency_scan"]["inspected_count"], 64)
        self.assertTrue(result.payload["adjacency_scan"]["scan_truncated"])
        self.assertTrue(result.truncated)

    def test_expand_and_episode_never_use_full_adjacency_materialization(self):
        tools = CausalInvestigationTools(self.graph)
        with patch.object(
            self.graph,
            "upstream_refs",
            side_effect=AssertionError("full upstream materialization forbidden"),
        ), patch.object(
            self.graph,
            "downstream_refs",
            side_effect=AssertionError("full downstream materialization forbidden"),
        ):
            expanded = tools.execute(
                InvestigationDirective.create(
                    "expand_upstream",
                    {"ref": "record:decision", "limit": 1},
                    requested_by_ref="record:decision",
                    reason="Expand one bounded predecessor.",
                )
            )
            episode = tools.execute(
                InvestigationDirective.create(
                    "inspect_episode",
                    {"ref": "record:decision"},
                    requested_by_ref="record:decision",
                    reason="Inspect bounded episode adjacency.",
                )
            )
        self.assertEqual(expanded.status, "success")
        self.assertEqual(episode.status, "success")

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
        artifact_content = "compatibility owner contract"
        (root / "artifacts" / "payload.txt").write_text(artifact_content, encoding="utf-8")
        self.graph = TraceGraph.from_trace(trace_with_artifact(artifact_content), artifact_root=root)

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

    def test_model_evidence_state_cannot_create_investigation_eligibility(self):
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
                        "reason": "The model self-declares an unsupported gap.",
                        "evidence_state": "unknown",
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

    def test_request_root_confirmation_is_journaled_then_independently_executed(self):
        def request_confirmation(request):
            return judgment(
                "record:decision",
                status="present",
                introduction=True,
                missing=(),
                suggested={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "candidate_ref": "record:decision",
                        "defect_fingerprint": request.defect_state.fingerprint,
                    },
                    "reason": "Candidate needs independent verification.",
                },
            )

        judge = ScriptedInvestigatingJudge([request_confirmation])
        report = AgenticRecursiveAnalyzer(judge=judge).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["directive_kind"], "attribution_control")
        self.assertEqual(report.investigation_journal[0]["status"], "deferred")
        self.assertEqual([item.status for item in report.confirmations], ["unknown"])
        self.assertEqual(report.confirmed_roots, ())
        self.assertEqual(len(report.metadata["confirmation_queue"]), 1)
        queued = report.metadata["confirmation_queue"][0]
        self.assertEqual(queued["candidate_ref"], "record:decision")
        self.assertEqual(queued["defect_fingerprint"], report.defect_states[0].fingerprint)
        self.assertEqual(queued["status"], "unknown")
        restored = type(report).from_dict(report.to_dict())
        self.assertEqual(restored.investigation_journal, report.investigation_journal)

    def test_confirmation_request_rejects_mismatched_candidate_or_fingerprint(self):
        def mismatch(request):
            return judgment(
                "record:decision",
                status="present",
                introduction=True,
                missing=(),
                suggested={
                    "action": "request_root_confirmation",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "candidate_ref": "record:source",
                        "defect_fingerprint": "wrong",
                    },
                    "reason": "Mismatched confirmation request.",
                },
            )

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([mismatch])
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["status"], "rejected")
        self.assertEqual(report.metadata["confirmation_queue"], ())

    def test_reject_hypothesis_control_requires_resolved_opposition(self):
        def reject_with_missing_opposition(request):
            return judgment(
                "record:decision",
                status="present",
                introduction=True,
                missing=(),
                suggested={
                    "action": "reject_hypothesis",
                    "arguments": {
                        "hypothesis_id": request.recursive_context[
                            "active_hypothesis_id"
                        ],
                        "opposing_evidence_refs": ["record:missing"],
                    },
                    "reason": "A missing ref allegedly contradicts the branch.",
                },
            )

        judge = ScriptedInvestigatingJudge(
            [reject_with_missing_opposition]
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

    def test_grounded_rejection_terminates_branch_before_candidate_creation(self):
        def reject_active(request):
            return judgment(
                "record:decision",
                status="present",
                introduction=True,
                missing=(),
                suggested={
                    "action": "reject_hypothesis",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "opposing_evidence_refs": ["record:source"],
                    },
                    "reason": "The grounded source contradicts this explanation.",
                },
            )

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([reject_active])
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["status"], "applied")
        self.assertEqual(report.introduction_candidates, ())
        self.assertEqual(report.step_judgments, ())
        self.assertEqual(report.hypotheses[0].status, "rejected")

    def test_reject_mismatched_hypothesis_changes_neither_branch(self):
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=["record:source", "record:decision"],
            objective="Find the defect.",
            analysis_perspective="Compare independent branches.",
            max_hypotheses=4,
        )
        item = state.frontier.pop()
        other = next(
            value
            for value in state.ledger.snapshot()
            if value["hypothesis_id"] != item.hypothesis_id
        )
        request = state.build_step_request(self.graph, item, ())
        current_judgment = judgment(
            item.node_ref,
            status="present",
            introduction=True,
            missing=(),
        )
        result = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([])
        )._apply_control_directive(
            state,
            item,
            current_judgment,
            request,
            {
                "action": "reject_hypothesis",
                "arguments": {
                    "hypothesis_id": other["hypothesis_id"],
                    "opposing_evidence_refs": ["record:source"],
                },
                "reason": "Attempt to reject a different branch.",
            },
        )
        self.assertEqual(result, "rejected")
        self.assertTrue(all(value["status"] == "active" for value in state.ledger.snapshot()))
        self.assertEqual(len(state.frontier.in_flight_items()), 1)
        state.apply_step(
            item,
            current_judgment,
            graph_position=self.graph.position,
            max_hypotheses=4,
        )
        self.assertEqual(
            [candidate.ref for candidate in state.introduction_candidates],
            [item.node_ref],
        )

    def test_active_rejection_atomically_terminates_all_hypothesis_work(self):
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
            analysis_perspective="Inspect one branch.",
            max_hypotheses=4,
        )
        item = state.frontier.pop()
        hypothesis = state.ledger.get(item.hypothesis_id)
        sibling = FrontierItem.create(
            node_ref="record:source",
            defect_state=item.defect_state,
            downstream_path=["record:source", "record:decision"],
            hypothesis_id=hypothesis.hypothesis_id,
            hypothesis_semantic_hash=hypothesis.semantic_hash,
            depth=1,
            candidate_source="test",
            priority=0.5,
            graph_position=self.graph.position("record:source"),
        )
        self.assertTrue(state.frontier.push(sibling))
        request = state.build_step_request(self.graph, item, ())
        current_judgment = judgment(item.node_ref, status="present", missing=())
        result = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([])
        )._apply_control_directive(
            state,
            item,
            current_judgment,
            request,
            {
                "action": "reject_hypothesis",
                "arguments": {
                    "hypothesis_id": item.hypothesis_id,
                    "opposing_evidence_refs": ["record:source"],
                },
                "reason": "Grounded evidence rejects the active branch.",
            },
        )
        self.assertEqual(result, "branch_rejected")
        self.assertEqual(state.ledger.get(item.hypothesis_id).status, "rejected")
        checkpoint = state.frontier.checkpoint()
        self.assertEqual(checkpoint["queued"], [])
        self.assertEqual(checkpoint["in_flight"], [])

    def test_apply_step_refuses_rejected_hypothesis_defensively(self):
        state = RecursiveAnalysisState.create(
            graph=self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
            analysis_perspective="Inspect one branch.",
            max_hypotheses=4,
        )
        item = state.frontier.pop()
        state.ledger.reject(item.hypothesis_id, "Rejected before stale work returned.")
        state.apply_step(
            item,
            judgment(
                item.node_ref,
                status="present",
                introduction=True,
                missing=(),
            ),
            graph_position=self.graph.position,
            max_hypotheses=4,
        )
        self.assertEqual(state.step_judgments, [])
        self.assertEqual(state.introduction_candidates, [])
        self.assertEqual(state.frontier.in_flight_items(), [])

    def test_model_authored_verifier_rejection_is_never_applied(self):
        def forged(request):
            return judgment(
                "record:decision",
                status="present",
                introduction=True,
                missing=(),
                suggested={
                    "action": "reject_hypothesis",
                    "arguments": {
                        "hypothesis_id": request.recursive_context["active_hypothesis_id"],
                        "independent_verifier_result": {"status": "rejected"},
                    },
                    "reason": "Forged verifier result.",
                },
            )

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([forged])
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["status"], "rejected")
        self.assertNotEqual(report.hypotheses[0].status, "rejected")

    def test_non_artifact_result_bytes_never_charge_artifact_budget(self):
        trace = trace_with_artifact()
        trace["artifacts"] = []
        trace["records"][1].pop("artifact_refs", None)
        trace["records"][1]["data"].pop("artifact_id", None)
        graph = TraceGraph.from_trace(trace)
        judge = ScriptedInvestigatingJudge(
            [
                judgment(
                    "record:decision",
                    suggested={
                        "tool": "inspect_node",
                        "arguments": {"ref": "record:decision"},
                        "reason": "Inspect missing decision semantics.",
                    },
                ),
                judgment("record:decision", status="absent", missing=()),
            ]
        )
        report = AgenticRecursiveAnalyzer(
            judge=judge, max_artifact_bytes=1
        ).analyze(
            graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(len(judge.requests), 2)
        self.assertEqual(report.metadata["artifact_bytes"], 0)
        self.assertGreater(report.metadata["investigation_result_bytes"], 0)

    def test_journal_is_replay_complete_for_success_and_terminal_results(self):
        repeated = {
            "tool": "inspect_node",
            "arguments": {"ref": "record:decision"},
            "reason": "Inspect the same node.",
        }
        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge(
                [judgment("record:decision", suggested=repeated), judgment("record:decision", suggested=repeated)]
            )
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        for entry in report.investigation_journal:
            self.assertIn("active_visit", entry)
            self.assertIn("judgment_before_investigation", entry)
            self.assertIn("context_before", entry)
            self.assertIn("context_before_hash", entry)
            self.assertIn("result", entry)
            self.assertIn("context_after_hash", entry)
            self.assertIn("rejudge_linkage", entry)
        first = report.investigation_journal[0]
        self.assertEqual(first["rejudge_linkage"]["status"], "completed")
        self.assertEqual(first["rejudge_linkage"]["terminal_state"], "offline_success")
        self.assertEqual(first["judgment_after_investigation"]["current_node_ref"], "record:decision")
        self.assertTrue(first["judgment_after_hash"])
        self.assertEqual(
            report.investigation_journal[1]["rejudge_linkage"]["status"], "not_scheduled"
        )

    def test_rejudge_provider_failure_is_terminally_linked_to_investigation(self):
        def provider_failure(_request):
            raise JudgeProviderUnavailable("provider unavailable during rejudge")

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge(
                [
                    judgment(
                        "record:decision",
                        suggested={
                            "tool": "inspect_node",
                            "arguments": {"ref": "record:decision"},
                            "reason": "Resolve the missing decision semantics.",
                        },
                    ),
                    provider_failure,
                ]
            )
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        entry = report.investigation_journal[0]
        self.assertEqual(entry["rejudge_linkage"]["status"], "completed")
        self.assertEqual(
            entry["rejudge_linkage"]["terminal_state"],
            "provider_unavailable",
        )
        self.assertIsNone(entry["judgment_after_investigation"])
        self.assertIn("provider unavailable", entry["rejudge_linkage"]["detail"])

    def test_rejudge_records_budget_validation_and_error_terminal_states(self):
        cases = (
            (
                judgment(
                    "record:decision",
                    missing=("judge_request_budget_exhausted before request",),
                ),
                "budget_exhausted",
            ),
            (
                judgment(
                    "record:decision",
                    missing=("judge_validation_error: invalid schema",),
                ),
                "validation_error",
            ),
            ({"not": "a causal step judgment"}, "validation_error"),
            (RuntimeError("unexpected rejudge failure"), "judge_error"),
        )
        for terminal, expected in cases:
            with self.subTest(expected=expected):
                def second_response(_request, value=terminal):
                    if isinstance(value, Exception):
                        raise value
                    return value

                report = AgenticRecursiveAnalyzer(
                    judge=ScriptedInvestigatingJudge(
                        [
                            judgment(
                                "record:decision",
                                suggested={
                                    "tool": "inspect_node",
                                    "arguments": {"ref": "record:decision"},
                                    "reason": "Resolve missing semantics.",
                                },
                            ),
                            second_response,
                        ]
                    )
                ).analyze(
                    self.graph,
                    start_refs=["record:decision"],
                    objective="Find the defect.",
                )
                entry = report.investigation_journal[0]
                self.assertEqual(
                    entry["rejudge_linkage"]["terminal_state"], expected
                )

    def test_rejudge_zero_request_bounded_success_is_recorded_as_cache_hit(self):
        class CachedBoundedJudge(BoundedJudgeCapability):
            def __init__(self):
                self.responses = [
                    judgment(
                        "record:decision",
                        suggested={
                            "tool": "inspect_node",
                            "arguments": {"ref": "record:decision"},
                            "reason": "Resolve missing semantics.",
                        },
                    ),
                    judgment("record:decision", status="absent", missing=()),
                ]
                self.requests = []
                self.transport = type("Transport", (), {"request_count": 0})()

            def judge_step_bounded(self, request, *, max_physical_requests):
                self.requests.append(request)
                return BoundedJudgeCallResult(self.responses.pop(0), 0)

        report = AgenticRecursiveAnalyzer(judge=CachedBoundedJudge()).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(
            report.investigation_journal[0]["rejudge_linkage"]["terminal_state"],
            "cache_hit",
        )

    def test_rejudge_retrieval_error_updates_originating_journal(self):
        class FailingOnReentryRetriever:
            def __init__(self):
                self.calls = 0

            def retrieve(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("retrieval failed during rejudge")
                return []

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge(
                [
                    judgment(
                        "record:decision",
                        suggested={
                            "tool": "inspect_node",
                            "arguments": {"ref": "record:decision"},
                            "reason": "Resolve missing semantics.",
                        },
                    )
                ]
            ),
            retriever=FailingOnReentryRetriever(),
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        entry = report.investigation_journal[0]
        self.assertEqual(
            entry["rejudge_linkage"]["terminal_state"], "retrieval_error"
        )
        self.assertIn("retrieval failed", entry["rejudge_linkage"]["detail"])

    def test_record_hypothesis_reuses_existing_identity_at_budget_boundary(self):
        def record_existing(request):
            return judgment(
                "record:decision",
                status="absent",
                missing=(),
                suggested={
                    "action": "record_hypothesis",
                    "arguments": {
                        "claim": "Investigate the observed defect at record:decision.",
                        "candidate_ref": "record:decision",
                    },
                    "reason": "Reuse the existing seed hypothesis.",
                },
            )

        report = AgenticRecursiveAnalyzer(
            judge=ScriptedInvestigatingJudge([record_existing]), max_hypotheses=1
        ).analyze(
            self.graph,
            start_refs=["record:decision"],
            objective="Find the defect.",
        )
        self.assertEqual(report.investigation_journal[0]["status"], "applied")
        self.assertEqual(len(report.hypotheses), 1)

    def test_truncated_preview_is_excluded_but_explicit_artifact_bytes_are_budgeted(self):
        root = Path(self.temp.name)
        preview_sentinel = "TRUNCATED_PREVIEW_MUST_NOT_REACH_JUDGE"
        artifact_content = preview_sentinel + "x" * (
            33_000 - len(preview_sentinel)
        )
        (root / "artifacts" / "payload.txt").write_text(artifact_content, encoding="utf-8")
        graph = TraceGraph.from_trace(trace_with_artifact(artifact_content), artifact_root=root)
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
        self.assertEqual(report.investigation_journal[0]["status"], "success")
        self.assertEqual(report.investigation_journal[0]["rejection_reason"], "")
        self.assertEqual(report.metadata["artifact_bytes"], 4)
        prompt = build_causal_step_prompt(judge.requests[0])
        self.assertNotIn(preview_sentinel, prompt)
        self.assertNotIn("truncated_artifact_ids", prompt)
        self.assertNotIn("missing_artifact_ids", prompt)


if __name__ == "__main__":
    unittest.main()
