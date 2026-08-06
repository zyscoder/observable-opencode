from __future__ import annotations

import copy
import itertools
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from trace_attribution.candidate_budget import (
    candidate_identity,
    select_global_candidates,
)
from trace_attribution.candidate_clustering import (
    CandidateClusterManifest,
    build_candidate_cluster_manifest,
)
from trace_attribution.candidate_paging import (
    CandidatePagePlan,
    build_candidate_page_plan,
)
from trace_attribution.causal_state import CausalCandidate, DefectState
from trace_attribution.checkpoint import (
    CheckpointBundle,
    build_checkpoint_config,
)
from trace_attribution.causal_retrieval import (
    canonical_candidate_route,
    canonicalize_ranked_candidates,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.evidence_capsule import (
    CandidateEvidenceCapsule,
    build_candidate_evidence_capsules,
)
from trace_attribution.models import TraceNode, stable_json


CASE_ID = "vnext-omission-case"
SUBJECT_REVISION = "git:task-3-revision"


def _reconstruct(graph: TraceGraph):
    from trace_attribution.reconstruction import (
        reconstruct_obligation_gap_candidates,
    )

    return reconstruct_obligation_gap_candidates(graph)


def _causal_candidates(graph: TraceGraph):
    from trace_attribution.causal_retrieval import (
        obligation_gap_causal_candidates,
    )

    return obligation_gap_causal_candidates(graph)


def _candidate_audit(candidates):
    from trace_attribution.reconstruction import obligation_gap_candidate_audit

    return obligation_gap_candidate_audit(candidates)


def _record(
    record_id: str,
    event_type: str,
    timestamp: str,
    data: dict,
    *,
    component: str,
    status: str = "",
    source_refs: tuple[str, ...] = (),
) -> dict:
    return {
        "record_id": record_id,
        "component": component,
        "event_type": event_type,
        "timestamp": timestamp,
        "status": status,
        "source_refs": list(source_refs),
        "data": {
            "case_id": CASE_ID,
            "repository_revision": 7,
            "subject_revision": SUBJECT_REVISION,
            "revision_status": "matched",
            "revision_provenance_status": "valid",
            **data,
        },
    }


def _typed_failure(
    obligation_id: str,
    signature_id: str,
    *,
    subsystem: str,
) -> dict:
    return {
        "failure_signature": {
            "schema": "obligation-failure-signature/v1",
            "signature_id": signature_id,
            "subsystem": subsystem,
            "status": "failed",
        },
        "failure_binding": {
            "binding_type": "obligation",
            "obligation_id": obligation_id,
        },
    }


def _pydantic_records() -> list[dict]:
    return [
        _record(
            "descriptor_contract",
            "message.input",
            "2026-07-31T08:00:00Z",
            {
                "text": "Restore the complete deprecated-field descriptor protocol.",
                "task_obligations": [
                    {
                        "obligation_id": "descriptor-protocol",
                        "obligation_text": (
                            "Restore the deprecated-field descriptor protocol."
                        ),
                        "required_capabilities": ["__set_name__", "__get__"],
                    }
                ],
            },
            component="user",
        ),
        _record(
            "decisionnode_dec_293",
            "decision",
            "2026-07-31T08:01:00Z",
            {
                "decision_id": "dec_293",
                "decision_type": "reasoning_block",
                "rationale": (
                    "Scope descriptor restoration to implementing __get__ only; "
                    "this narrow descriptor work is complete."
                ),
            },
            component="agent",
        ),
        _record(
            "change_get",
            "change",
            "2026-07-31T08:02:00Z",
            {"summary": "Restored descriptor __get__ behavior."},
            component="tool",
            status="success",
            source_refs=("record:decisionnode_dec_293",),
        ),
        _record(
            "verification_get",
            "verification",
            "2026-07-31T08:03:00Z",
            {"exit_code": 0, "output": "descriptor __get__ tests passed"},
            component="tool",
            status="success",
            source_refs=("record:change_get",),
        ),
        _record(
            "verification_missing_set_name",
            "verification",
            "2026-07-31T08:04:00Z",
            {
                "exit_code": 1,
                "output": (
                    "AttributeError: deprecated descriptor is missing __set_name__"
                ),
                **_typed_failure(
                    "descriptor-protocol",
                    "descriptor-set-name-failure",
                    subsystem="pydantic.descriptor_lifecycle",
                ),
                "failure_binding": {
                    "binding_type": "decision",
                    "decision_ref": "record:decisionnode_dec_293",
                },
            },
            component="tool",
            status="failure",
        ),
    ]


def _seaborn_records() -> list[dict]:
    return [
        _record(
            "dependency_contract",
            "message.input",
            "2026-07-31T09:00:00Z",
            {
                "text": "Restore the runtime dependency contract.",
                "task_obligations": [
                    {
                        "obligation_id": "runtime-packaging-dependency",
                        "obligation_text": (
                            "Preserve the required packaging runtime dependency."
                        ),
                        "required_capabilities": ["packaging"],
                    }
                ],
            },
            component="user",
        ),
        _record(
            "decisionnode_dec_147",
            "decision",
            "2026-07-31T09:01:00Z",
            {
                "decision_id": "dec_147",
                "decision_type": "reasoning_block",
                "rationale": (
                    "The implementation depends on packaging, but tests probably "
                    "omit it, so exclude packaging from this restoration."
                ),
            },
            component="agent",
        ),
        _record(
            "verification_missing_packaging",
            "verification",
            "2026-07-31T09:02:00Z",
            {
                "exit_code": 1,
                "output": "ImportError: packaging is required at runtime",
                **_typed_failure(
                    "runtime-packaging-dependency",
                    "runtime-packaging-import-failure",
                    subsystem="seaborn.runtime_dependencies",
                ),
                "failure_binding": {
                    "binding_type": "decision",
                    "decision_ref": "record:decisionnode_dec_147",
                },
            },
            component="tool",
            status="failure",
        ),
    ]


def omission_trace(records: list[dict] | None = None) -> dict:
    return {
        "trace_version": "6.0",
        "manifest": {
            "case_id": CASE_ID,
            "subject_revision": SUBJECT_REVISION,
            "run_id": "vnext-task-3-run",
            "subject_revision_provenance": {
                "method": "case_trace_config",
                "source": "CaseTraceConfig.subjectRevision",
                "bound_at": "case_start",
                "case_id": CASE_ID,
                "run_id": "vnext-task-3-run",
            },
        },
        "records": copy.deepcopy(
            records if records is not None else _pydantic_records()
        ),
        "dataflow_edges": [
            {
                "from": {"type": "record", "id": "decisionnode_dec_293"},
                "to": {"type": "record", "id": "change_get"},
                "relation": "decision_guided_change",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
            {
                "from": {"type": "record", "id": "change_get"},
                "to": {"type": "record", "id": "verification_get"},
                "relation": "change_verified_by",
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            },
        ],
        "human_labels": {
            "root_cause_ref": "record:decisionnode_dec_293",
            "reviewer_conclusion": "This text is evaluation-only.",
        },
    }


def _ordinary_candidate(index: int) -> CausalCandidate:
    ref = "record:ordinary-{0:03d}".format(index)
    return CausalCandidate(
        ref=ref,
        node=TraceNode(
            ref=ref,
            record_id=ref.removeprefix("record:"),
            component="test",
            event_type="message.input",
            data={"text": "ordinary candidate {0}".format(index)},
        ),
        source="semantic_fallback",
        edge={
            "from_ref": ref,
            "to_ref": "record:verification_missing_set_name",
            "relation": "semantic_predecessor_match",
            "evidence_type": "semantic_inferred",
            "evidence_refs": [ref],
            "eligible_for_attribution": False,
            "retrieval_candidate": True,
            "edge_origin": "offline.test",
        },
        evidence_refs=(ref,),
    )


class ObligationGapCandidateReconstructionTest(unittest.TestCase):
    def test_broad_decision_presence_is_upgraded_to_gap_provenance_before_budget(self) -> None:
        graph = TraceGraph.from_trace(omission_trace())
        decision = graph.nodes["record:decisionnode_dec_293"]
        broad_candidate = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="semantic_fallback",
            edge={
                "from_ref": decision.ref,
                "to_ref": "record:verification_missing_set_name",
                "relation": "semantic_predecessor_match",
                "evidence_type": "semantic_inferred",
                "evidence_refs": [decision.ref],
                "eligible_for_attribution": False,
                "retrieval_candidate": True,
                "edge_origin": "offline.semantic_retrieval",
            },
            evidence_refs=(decision.ref,),
        )

        selection = select_global_candidates(graph, (broad_candidate,))

        self.assertEqual(len(selection.discovered), 1)
        discovered = selection.discovered[0]
        self.assertEqual(discovered.ref, "record:decisionnode_dec_293")
        self.assertEqual(discovered.source, "obligation_gap_reconstruction")
        self.assertEqual(discovered.edge["evidence_type"], "offline_reconstruction")
        gap = discovered.edge["obligation_gap"]
        self.assertEqual(gap["gap_coverage"], ("__set_name__",))
        self.assertEqual(
            selection.to_dict()["candidate_audit"][0]["candidate_identity"],
            gap["identity"],
        )

    def test_pydantic_scoped_descriptor_work_exposes_the_full_gap(self) -> None:
        graph = TraceGraph.from_trace(omission_trace())

        candidates = _reconstruct(graph)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        from trace_attribution.restoration_obligation import ObligationGapCandidate

        self.assertIsInstance(candidate, ObligationGapCandidate)
        self.assertEqual(candidate.case_id, CASE_ID)
        self.assertEqual(candidate.subject_revision, SUBJECT_REVISION)
        self.assertEqual(candidate.obligation_id, "descriptor-protocol")
        self.assertEqual(
            candidate.normalized_obligation_text,
            "restore the deprecated-field descriptor protocol.",
        )
        self.assertEqual(candidate.decision_ref, "record:decisionnode_dec_293")
        self.assertEqual(
            candidate.recognized_evidence_refs,
            ("record:decisionnode_dec_293",),
        )
        self.assertEqual(candidate.required_coverage, ("__get__", "__set_name__"))
        self.assertEqual(candidate.recognized_coverage, ("__get__",))
        self.assertEqual(candidate.planned_coverage, ("__get__",))
        self.assertEqual(candidate.actual_action_coverage, ("__get__",))
        self.assertEqual(candidate.verification_coverage, ("__get__",))
        self.assertEqual(candidate.excluded_coverage, ("__set_name__",))
        self.assertEqual(candidate.gap_coverage, ("__set_name__",))
        self.assertIn("narrow descriptor work is complete", candidate.exclusion_or_closure_rationale)
        self.assertEqual(
            candidate.downstream_failure_signature_refs,
            ("record:verification_missing_set_name",),
        )
        self.assertEqual(len(candidate.failure_signature), 64)
        self.assertEqual(
            candidate.confirmed_path_provenance,
            (
                (
                    "record:decisionnode_dec_293",
                    "record:change_get",
                ),
            ),
        )
        self.assertEqual(
            candidate.offline_path_provenance[0]["evidence_type"],
            "offline_reconstruction",
        )
        self.assertFalse(candidate.offline_path_provenance[0]["confirmed_fact"])
        self.assertEqual(len(candidate.identity), 64)
        with self.assertRaises(FrozenInstanceError):
            candidate.obligation_id = "mutated"  # type: ignore[misc]

    def test_seaborn_exclusion_preserves_the_authored_rationale(self) -> None:
        graph = TraceGraph.from_trace(omission_trace(_seaborn_records()))

        candidates = _reconstruct(graph)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.decision_ref, "record:decisionnode_dec_147")
        self.assertEqual(candidate.recognized_coverage, ("packaging",))
        self.assertEqual(candidate.planned_coverage, ())
        self.assertEqual(candidate.actual_action_coverage, ())
        self.assertEqual(candidate.verification_coverage, ())
        self.assertEqual(candidate.excluded_coverage, ("packaging",))
        self.assertEqual(candidate.gap_coverage, ("packaging",))
        self.assertEqual(
            candidate.exclusion_or_closure_rationale,
            (
                "the implementation depends on packaging, but tests probably "
                "omit it, so exclude packaging from this restoration."
            ),
        )
        self.assertEqual(
            candidate.downstream_failure_signature_refs,
            ("record:verification_missing_packaging",),
        )

    def test_unstructured_primary_contract_text_can_ground_the_obligation(self) -> None:
        cases = []
        pydantic = _pydantic_records()
        pydantic[0]["data"].pop("task_obligations")
        pydantic[0]["data"]["text"] = (
            "This request requires the descriptor protocol contract to preserve "
            "__get__ and __set_name__."
        )
        cases.append((pydantic, "record:decisionnode_dec_293", ("__set_name__",)))
        seaborn = _seaborn_records()
        seaborn[0]["data"].pop("task_obligations")
        seaborn[0]["data"]["text"] = (
            "For the current task, the runtime dependency contract requires "
            "packaging."
        )
        cases.append((seaborn, "record:decisionnode_dec_147", ("packaging",)))

        for records, decision_ref, gap in cases:
            with self.subTest(decision_ref=decision_ref):
                candidates = _reconstruct(
                    TraceGraph.from_trace(omission_trace(records))
                )
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0].decision_ref, decision_ref)
                self.assertEqual(candidates[0].gap_coverage, gap)

    def test_unmentioned_missing_function_creates_no_candidate(self) -> None:
        records = _pydantic_records()
        records[1]["data"]["rationale"] = (
            "Tighten unrelated warning formatting and leave descriptors untouched."
        )

        candidates = _reconstruct(
            TraceGraph.from_trace(omission_trace(records))
        )

        self.assertEqual(candidates, ())

    def test_fully_acted_on_and_verified_obligation_creates_no_gap(self) -> None:
        records = _pydantic_records()
        records[1]["data"]["rationale"] = (
            "Implement and verify descriptor __get__ and __set_name__."
        )
        records[2]["data"]["summary"] = (
            "Restored descriptor __get__ and __set_name__."
        )
        records[3]["data"]["output"] = (
            "descriptor __get__ and __set_name__ tests passed"
        )

        candidates = _reconstruct(
            TraceGraph.from_trace(omission_trace(records))
        )

        self.assertEqual(candidates, ())

    def test_unlinked_later_work_cannot_supply_this_decisions_coverage(self) -> None:
        records = _pydantic_records()
        records[2]["data"]["summary"] = (
            "Restored descriptor __get__ and __set_name__ in unrelated work."
        )
        records[3]["data"]["output"] = (
            "unrelated descriptor __get__ and __set_name__ tests passed"
        )
        records[2]["source_refs"] = []
        records[3]["source_refs"] = []
        trace = omission_trace(records)
        trace["dataflow_edges"] = []

        candidates = _reconstruct(TraceGraph.from_trace(trace))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].actual_action_coverage, ())
        self.assertEqual(candidates[0].verification_coverage, ())
        self.assertEqual(
            candidates[0].gap_coverage,
            ("__get__", "__set_name__"),
        )

    def test_label_only_or_reviewer_only_evidence_creates_no_candidate(self) -> None:
        records = [
            _record(
                "decisionnode_dec_label",
                "decision",
                "2026-07-31T10:00:00Z",
                {
                    "rationale": (
                        "Implement __set_name__ for the descriptor contract."
                    )
                },
                component="agent",
            ),
            _record(
                "reviewer_label",
                "case.observed_defect",
                "2026-07-31T10:01:00Z",
                {
                    "task_obligations": [
                        {
                            "obligation_id": "descriptor-protocol",
                            "obligation_text": "Restore the descriptor protocol.",
                            "required_capabilities": ["__set_name__"],
                        }
                    ],
                    "failure_type": "missing___set_name__",
                    "reviewer_conclusion": "The Agent omitted __set_name__.",
                },
                component="evaluation",
                status="failure",
            ),
        ]

        candidates = _reconstruct(
            TraceGraph.from_trace(omission_trace(records))
        )

        self.assertEqual(candidates, ())

    def test_unresolved_or_cross_case_evidence_refs_fail_closed(self) -> None:
        unresolved = _pydantic_records()
        unresolved[0]["data"]["task_obligations"][0]["evidence_refs"] = [
            "record:missing_contract"
        ]
        cross_case = _pydantic_records()
        cross_case[-1]["data"]["case_id"] = "another-case"

        for records in (unresolved, cross_case):
            with self.subTest(records=records[-1]["record_id"]):
                candidates = _reconstruct(
                    TraceGraph.from_trace(omission_trace(records))
                )
                self.assertEqual(candidates, ())

    def test_offline_candidate_is_not_projected_as_a_confirmed_fact(self) -> None:
        graph = TraceGraph.from_trace(omission_trace())

        candidate = _causal_candidates(graph)[0]

        self.assertEqual(candidate.source, "obligation_gap_reconstruction")
        self.assertEqual(candidate.edge["evidence_type"], "offline_reconstruction")
        self.assertFalse(candidate.edge["confirmed_fact"])
        self.assertEqual(
            graph.edge_context(
                "record:decisionnode_dec_293",
                "record:verification_missing_set_name",
            ),
            [],
        )
        self.assertFalse(
            any(
                edge.get("evidence_type") == "offline_reconstruction"
                for edge in graph.message_lineage["edges"]
            )
        )
        self.assertEqual(graph.message_lineage["stats"]["confirmed_edge_count"], 0)

    def test_permutation_preserves_identity_order_budget_and_failure_binding(self) -> None:
        records = [*_pydantic_records(), *_seaborn_records()]
        forward_graph = TraceGraph.from_trace(omission_trace(records))
        permuted_records = copy.deepcopy(list(reversed(records)))
        for record in permuted_records:
            record["source_refs"] = list(reversed(record.get("source_refs") or []))
            for obligation in record["data"].get("task_obligations") or []:
                obligation["required_capabilities"] = list(
                    reversed(obligation["required_capabilities"])
                )
                obligation["evidence_refs"] = list(
                    reversed(obligation.get("evidence_refs") or [])
                )
        reverse_graph = TraceGraph.from_trace(omission_trace(permuted_records))

        forward_gaps = _reconstruct(forward_graph)
        reverse_gaps = _reconstruct(reverse_graph)
        forward_candidates = _causal_candidates(forward_graph)
        reverse_candidates = _causal_candidates(reverse_graph)
        forward_selection = select_global_candidates(
            forward_graph,
            (*(_ordinary_candidate(index) for index in range(300)), *forward_candidates),
        )
        reverse_selection = select_global_candidates(
            reverse_graph,
            (*(_ordinary_candidate(index) for index in range(300)), *reverse_candidates),
        )

        self.assertEqual(
            [candidate.to_dict() for candidate in forward_gaps],
            [candidate.to_dict() for candidate in reverse_gaps],
        )
        self.assertEqual(
            [candidate.ref for candidate in forward_candidates],
            [candidate.ref for candidate in reverse_candidates],
        )
        self.assertEqual(
            [candidate.ref for candidate in forward_selection.offered],
            [candidate.ref for candidate in reverse_selection.offered],
        )
        self.assertEqual(
            forward_selection.selection_identity,
            reverse_selection.selection_identity,
        )
        self.assertEqual(
            [candidate.downstream_failure_signature_refs for candidate in forward_gaps],
            [candidate.downstream_failure_signature_refs for candidate in reverse_gaps],
        )

    def test_decisions_survive_budget_and_publish_zero_fabricated_refs(self) -> None:
        records = [*_pydantic_records(), *_seaborn_records()]
        graph = TraceGraph.from_trace(omission_trace(records))
        gaps = _reconstruct(graph)
        candidates = _causal_candidates(graph)

        selection = select_global_candidates(
            graph,
            (*(_ordinary_candidate(index) for index in range(300)), *candidates),
        )
        payload = selection.to_dict()
        audit_by_ref = {
            entry["ref"]: entry for entry in payload["candidate_audit"]
        }
        gap_by_ref = {gap.decision_ref: gap for gap in gaps}
        real_refs = set(graph.nodes)

        self.assertEqual(
            set(gap_by_ref),
            {
                "record:decisionnode_dec_293",
                "record:decisionnode_dec_147",
            },
        )
        for ref, gap in gap_by_ref.items():
            self.assertIn(ref, [item.ref for item in selection.discovered])
            self.assertIn(ref, [item.ref for item in selection.offered])
            self.assertEqual(audit_by_ref[ref]["candidate_identity"], gap.identity)
            self.assertTrue(set(gap.evidence_refs) <= real_refs)
            for edge in gap.offline_path_provenance:
                self.assertIn(edge["from_ref"], real_refs)
                self.assertIn(edge["to_ref"], real_refs)
                self.assertTrue(set(edge["evidence_refs"]) <= real_refs)

        report_audit = _candidate_audit(gaps)
        self.assertEqual(report_audit["behavior_impact"], "none_offline_analysis_only")
        self.assertEqual(report_audit["candidate_count"], 2)
        self.assertEqual(
            report_audit["candidates"],
            [gap.to_dict() for gap in gaps],
        )
        serialized = stable_json(report_audit)
        self.assertNotIn("root_cause_ref", serialized)
        self.assertNotIn("reviewer_conclusion", serialized)
        self.assertNotIn("This text is evaluation-only", serialized)


class ObligationGapAdversarialReviewTest(unittest.TestCase):
    def _packaging_contract(self, record_id: str = "packaging_contract") -> dict:
        return _record(
            record_id,
            "message.input",
            "2026-07-31T11:00:00Z",
            {
                "text": "Restore the packaging runtime contract.",
                "task_obligations": [
                    {
                        "obligation_id": "packaging-runtime",
                        "obligation_text": "Preserve the packaging runtime dependency.",
                        "required_capabilities": ["packaging"],
                    }
                ],
            },
            component="user",
        )

    def _packaging_failure(
        self,
        record_id: str,
        timestamp: str,
        *,
        source_refs: tuple[str, ...] = (),
        obligation_id: str = "packaging-runtime",
        signature_id: str = "packaging-runtime-failure",
        subsystem: str = "runtime.packaging",
        chronology_index: int | None = None,
    ) -> dict:
        data = {
            "exit_code": 1,
            "output": "ImportError: packaging is required at runtime",
            **_typed_failure(
                obligation_id,
                signature_id,
                subsystem=subsystem,
            ),
        }
        if chronology_index is not None:
            data["chronology_index"] = chronology_index
        return _record(
            record_id,
            "verification",
            timestamp,
            data,
            component="tool",
            status="failure",
            source_refs=source_refs,
        )

    def test_nested_reviewer_only_contracts_are_scrubbed_before_extraction(self) -> None:
        records = [
            _record(
                "benign_request",
                "message.input",
                "2026-07-31T11:00:00Z",
                {
                    "text": "Tighten warning formatting.",
                    "request_metadata": {
                        "review_only": {
                            "expected_roots": {
                                "task_obligations": [
                                    {
                                        "obligation_id": "packaging-runtime",
                                        "obligation_text": (
                                            "Preserve the packaging runtime dependency."
                                        ),
                                        "required_capabilities": ["packaging"],
                                    }
                                ],
                                "reviewer_conclusion": (
                                    "Packaging is the expected root."
                                ),
                            },
                            "scoring_explanation": (
                                "The contract requires packaging."
                            ),
                        }
                    },
                    "expected_roots": {
                        "contract": "The runtime contract requires packaging."
                    },
                    "human_labels": {
                        "missing_symbol": "packaging"
                    },
                },
                component="user",
            ),
            _record(
                "decision_nested_review",
                "decision",
                "2026-07-31T11:01:00Z",
                {"rationale": "Exclude packaging from this change."},
                component="agent",
            ),
            self._packaging_failure(
                "nested_review_failure",
                "2026-07-31T11:02:00Z",
            ),
        ]
        records[-1]["data"]["failure_binding"] = {
            "binding_type": "decision",
            "decision_ref": "record:decision_nested_review",
        }

        candidates = _reconstruct(TraceGraph.from_trace(omission_trace(records)))

        self.assertEqual(candidates, ())

    def test_complete_plan_and_action_with_failed_verification_is_a_gap(self) -> None:
        records = [
            self._packaging_contract(),
            _record(
                "decision_plan_packaging",
                "decision",
                "2026-07-31T11:01:00Z",
                {
                    "rationale": "Implement packaging and verify the runtime dependency.",
                    "planned_capabilities": ["packaging"],
                },
                component="agent",
            ),
            _record(
                "change_packaging",
                "change",
                "2026-07-31T11:02:00Z",
                {"summary": "Implemented packaging runtime support."},
                component="tool",
                status="success",
                source_refs=("record:decision_plan_packaging",),
            ),
            self._packaging_failure(
                "failed_packaging_verification",
                "2026-07-31T11:03:00Z",
                source_refs=("record:change_packaging",),
            ),
        ]

        candidates = _reconstruct(TraceGraph.from_trace(omission_trace(records)))

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.planned_coverage, ("packaging",))
        self.assertEqual(candidate.actual_action_coverage, ("packaging",))
        self.assertEqual(candidate.verification_coverage, ())
        self.assertEqual(candidate.excluded_coverage, ())
        self.assertEqual(candidate.gap_coverage, ("packaging",))

    def test_nested_review_fields_do_not_change_candidate_identity(self) -> None:
        graph = TraceGraph.from_trace(omission_trace())
        decision = graph.nodes["record:decisionnode_dec_293"]
        edge = graph.edge_context(decision.ref, "record:change_get")[0]
        baseline = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="confirmed_edge",
            edge=edge,
            evidence_refs=(decision.ref,),
        )
        polluted = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="confirmed_edge",
            edge={
                **edge,
                "nested_metadata": {
                    "reviewer_conclusion": "Packaging is the expected root.",
                    "expected_roots": ["record:decisionnode_dec_293"],
                    "scoring_explanation": "Award full credit for this root.",
                },
            },
            evidence_refs=(decision.ref,),
        )

        self.assertEqual(
            candidate_identity(polluted),
            candidate_identity(baseline),
        )

    def test_clause_scoped_negation_does_not_create_false_coverage(self) -> None:
        records = [
            self._packaging_contract(),
            _record(
                "decision_clause_scope",
                "decision",
                "2026-07-31T11:01:00Z",
                {"rationale": "Skip changelog; implement packaging."},
                component="agent",
            ),
            _record(
                "negated_packaging_action",
                "change",
                "2026-07-31T11:02:00Z",
                {"summary": "Did not implement packaging; changed metadata only."},
                component="tool",
                status="success",
                source_refs=("record:decision_clause_scope",),
            ),
            _record(
                "negated_packaging_verification",
                "verification",
                "2026-07-31T11:03:00Z",
                {"exit_code": 0, "output": "Packaging tests skipped; not verified."},
                component="tool",
                status="success",
                source_refs=("record:negated_packaging_action",),
            ),
            self._packaging_failure(
                "clause_scope_failure",
                "2026-07-31T11:04:00Z",
                source_refs=("record:negated_packaging_action",),
            ),
        ]

        candidates = _reconstruct(TraceGraph.from_trace(omission_trace(records)))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].planned_coverage, ("packaging",))
        self.assertEqual(candidates[0].actual_action_coverage, ())
        self.assertEqual(candidates[0].verification_coverage, ())

    def test_verification_requires_ordered_action_to_verification_reachability(self) -> None:
        for suffix, action_timestamp, verification_timestamp, verification_source in (
            (
                "verification_before_action",
                "2026-07-31T11:03:00Z",
                "2026-07-31T11:02:00Z",
                "record:decision_verification_before_action",
            ),
            (
                "sibling_branches",
                "2026-07-31T11:02:00Z",
                "2026-07-31T11:03:00Z",
                "record:decision_sibling_branches",
            ),
        ):
            with self.subTest(suffix=suffix):
                decision_id = "decision_{0}".format(suffix)
                action_id = "action_{0}".format(suffix)
                records = [
                    self._packaging_contract("contract_{0}".format(suffix)),
                    _record(
                        decision_id,
                        "decision",
                        "2026-07-31T11:01:00Z",
                        {"rationale": "Implement and verify packaging."},
                        component="agent",
                    ),
                    _record(
                        action_id,
                        "change",
                        action_timestamp,
                        {"summary": "Implemented packaging."},
                        component="tool",
                        status="success",
                        source_refs=("record:{0}".format(decision_id),),
                    ),
                    _record(
                        "verification_{0}".format(suffix),
                        "verification",
                        verification_timestamp,
                        {"exit_code": 0, "output": "Packaging tests passed."},
                        component="tool",
                        status="success",
                        source_refs=(verification_source,),
                    ),
                    self._packaging_failure(
                        "failure_{0}".format(suffix),
                        "2026-07-31T11:04:00Z",
                        source_refs=("record:{0}".format(action_id),),
                    ),
                ]

                candidates = _reconstruct(
                    TraceGraph.from_trace(omission_trace(records))
                )

                self.assertEqual(len(candidates), 1)
                self.assertEqual(
                    candidates[0].actual_action_coverage,
                    ("packaging",),
                )
                self.assertEqual(candidates[0].verification_coverage, ())

    def test_chronology_index_orders_records_without_timestamps(self) -> None:
        contract = _record(
            "z_contract",
            "message.input",
            "",
            {
                "chronology_index": 1,
                "task_obligations": [
                    {
                        "obligation_id": "package-and-wheel",
                        "obligation_text": "Restore packaging and wheel support.",
                        "required_capabilities": ["packaging", "wheel"],
                    }
                ],
            },
            component="user",
        )
        records = [
            contract,
            _record(
                "z_decision",
                "decision",
                "",
                {
                    "chronology_index": 2,
                    "rationale": "Implement packaging but exclude wheel.",
                    "planned_capabilities": ["packaging"],
                },
                component="agent",
            ),
            _record(
                "a_action",
                "change",
                "",
                {
                    "chronology_index": 3,
                    "summary": "Implemented packaging.",
                },
                component="tool",
                status="success",
                source_refs=("record:z_decision",),
            ),
            _record(
                "b_verification",
                "verification",
                "",
                {
                    "chronology_index": 4,
                    "exit_code": 0,
                    "output": "Packaging tests passed.",
                },
                component="tool",
                status="success",
                source_refs=("record:a_action",),
            ),
            _record(
                "c_failure",
                "verification",
                "",
                {
                    "chronology_index": 5,
                    "exit_code": 1,
                    "output": "Wheel support is missing.",
                    **_typed_failure(
                        "package-and-wheel",
                        "wheel-support-failure",
                        subsystem="runtime.wheel",
                    ),
                },
                component="tool",
                status="failure",
                source_refs=("record:a_action",),
            ),
        ]

        candidates = _reconstruct(TraceGraph.from_trace(omission_trace(records)))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].actual_action_coverage, ("packaging",))
        self.assertEqual(candidates[0].verification_coverage, ("packaging",))
        self.assertEqual(candidates[0].gap_coverage, ("wheel",))

    def test_failure_binding_rejects_coincidence_unrelated_and_predecision(self) -> None:
        records = [
            self._packaging_failure(
                "z_predecision_failure",
                "",
                chronology_index=1,
                signature_id="predecision-packaging-failure",
            ),
            {
                **self._packaging_contract("a_contract"),
                "timestamp": "",
                "data": {
                    **self._packaging_contract("a_contract")["data"],
                    "chronology_index": 2,
                },
            },
            _record(
                "m_decision",
                "decision",
                "",
                {
                    "chronology_index": 3,
                    "rationale": "Exclude packaging from this restoration.",
                },
                component="agent",
            ),
            self._packaging_failure(
                "n_related_failure",
                "",
                chronology_index=4,
                signature_id="related-packaging-failure",
            ),
            self._packaging_failure(
                "o_unrelated_failure",
                "",
                chronology_index=5,
                obligation_id="documentation-build",
                signature_id="documentation-packaging-word-failure",
                subsystem="docs.rendering",
            ),
            _record(
                "p_text_coincidence",
                "verification",
                "",
                {
                    "chronology_index": 6,
                    "exit_code": 1,
                    "output": "A later unrelated log happened to mention packaging.",
                },
                component="tool",
                status="failure",
            ),
        ]

        candidates = _reconstruct(TraceGraph.from_trace(omission_trace(records)))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].downstream_failure_signature_refs,
            ("record:n_related_failure",),
        )

    def test_confirmed_candidate_remains_authoritative_when_offline_route_exists(self) -> None:
        graph = TraceGraph.from_trace(omission_trace())
        decision = graph.nodes["record:decisionnode_dec_293"]
        confirmed_edge = graph.edge_context(
            decision.ref,
            "record:change_get",
        )[0]
        confirmed = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="confirmed_edge",
            edge=confirmed_edge,
            score=0.25,
            evidence_refs=(decision.ref, "record:change_get"),
        )
        confirmed_identity = candidate_identity(confirmed)
        offline = _causal_candidates(graph)[0]

        for routes in ((offline, confirmed), (confirmed, offline)):
            with self.subTest(order=[route.source for route in routes]):
                merged = canonical_candidate_route(graph, decision.ref, routes)
                self.assertEqual(merged.source, "confirmed_edge")
                self.assertEqual(merged.score, confirmed.score)
                self.assertEqual(candidate_identity(merged), confirmed_identity)
                self.assertEqual(
                    [
                        item["identity"]
                        for item in merged.edge[
                            "attribution_only_obligation_gaps"
                        ]
                    ],
                    [offline.edge["obligation_gap"]["identity"]],
                )

        selection = select_global_candidates(graph, (confirmed,))
        self.assertEqual(selection.discovered[0].source, "confirmed_edge")
        self.assertEqual(
            candidate_identity(selection.discovered[0]),
            confirmed_identity,
        )

    def test_two_obligations_for_one_decision_are_zero_loss_and_merge_aci(self) -> None:
        records = [
            _record(
                "dual_contract",
                "message.input",
                "2026-07-31T12:00:00Z",
                {
                    "task_obligations": [
                        {
                            "obligation_id": "packaging-runtime",
                            "obligation_text": "Preserve packaging.",
                            "required_capabilities": ["packaging"],
                        },
                        {
                            "obligation_id": "wheel-runtime",
                            "obligation_text": "Preserve wheel.",
                            "required_capabilities": ["wheel"],
                        },
                    ]
                },
                component="user",
            ),
            _record(
                "dual_decision",
                "decision",
                "2026-07-31T12:01:00Z",
                {"rationale": "Exclude packaging and wheel from restoration."},
                component="agent",
            ),
            self._packaging_failure(
                "dual_packaging_failure",
                "2026-07-31T12:02:00Z",
                obligation_id="packaging-runtime",
                signature_id="dual-packaging-failure",
            ),
            _record(
                "dual_wheel_failure",
                "verification",
                "2026-07-31T12:03:00Z",
                {
                    "exit_code": 1,
                    "output": "Wheel is required at runtime.",
                    **_typed_failure(
                        "wheel-runtime",
                        "dual-wheel-failure",
                        subsystem="runtime.wheel",
                    ),
                },
                component="tool",
                status="failure",
            ),
        ]
        graph = TraceGraph.from_trace(omission_trace(records))
        gaps = _reconstruct(graph)
        routes = _causal_candidates(graph)
        expected_identities = tuple(sorted(gap.identity for gap in gaps))

        self.assertEqual(
            {gap.obligation_id for gap in gaps},
            {"packaging-runtime", "wheel-runtime"},
        )
        self.assertEqual(len(routes), 2)

        forward = canonicalize_ranked_candidates(graph, routes, limit=24)
        reverse = canonicalize_ranked_candidates(
            graph,
            tuple(reversed(routes)),
            limit=24,
        )
        duplicate = canonicalize_ranked_candidates(
            graph,
            (*routes, *reversed(routes)),
            limit=24,
        )
        left_associated = canonicalize_ranked_candidates(
            graph,
            (*canonicalize_ranked_candidates(graph, routes[:1], limit=24), *routes[1:]),
            limit=24,
        )
        right_associated = canonicalize_ranked_candidates(
            graph,
            (*routes[:1], *canonicalize_ranked_candidates(graph, routes[1:], limit=24)),
            limit=24,
        )
        for merged in (
            forward,
            reverse,
            duplicate,
            left_associated,
            right_associated,
        ):
            self.assertEqual(
                tuple(sorted(candidate_identity(item) for item in merged)),
                expected_identities,
            )

        selection = select_global_candidates(graph, routes)
        selection_payload = selection.to_dict()
        self.assertEqual(len(selection.discovered), 2)
        self.assertEqual(len(selection.offered), 2)
        self.assertEqual(
            {
                item["candidate_identity"]
                for item in selection_payload["candidate_audit"]
            },
            set(expected_identities),
        )
        self.assertEqual(_candidate_audit(gaps)["candidate_count"], 2)

        defect_state = DefectState.create(
            label="dual-runtime-gap",
            expected="packaging and wheel remain available",
            actual="both runtime obligations failed",
            mechanism="dependency restoration gap",
            scope="task_quality",
        )
        paths = {
            "record:dual_decision": ("record:dual_decision",),
        }
        projection_candidate = canonical_candidate_route(
            graph,
            "record:dual_decision",
            selection.discovered,
        )
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(projection_candidate,),
            defect_state=defect_state,
            downstream_paths=paths,
            start_refs=("record:dual_decision",),
        )
        self.assertEqual(len(capsules), 1)
        capsule_payload = capsules[0].to_dict()
        capsule_gap_identities = {
            item["identity"]
            for item in capsule_payload["candidate"]["retrieval_edge"][
                "attribution_only_obligation_gaps"
            ]
        }
        self.assertEqual(capsule_gap_identities, set(expected_identities))
        self.assertEqual(
            CandidateEvidenceCapsule.from_dict(capsule_payload).to_dict(),
            capsule_payload,
        )

        manifest = build_candidate_cluster_manifest(
            graph=graph,
            candidates=selection.discovered,
            candidate_paths=paths,
            candidate_audit=selection_payload["candidate_audit"],
            source_selection_identity=selection.selection_identity,
            seed_ref="record:dual_decision",
            defect_fingerprint=defect_state.fingerprint,
        )
        self.assertEqual(
            manifest.candidate_facts[0].restoration_obligation_ids,
            ("packaging-runtime", "wheel-runtime"),
        )

        page_plan = build_candidate_page_plan(
            seed_ref="record:dual_decision",
            defect_fingerprint=defect_state.fingerprint,
            capsules=capsules,
            round_index=0,
        )
        self.assertEqual(page_plan.candidate_refs, ("record:dual_decision",))
        self.assertEqual(len(page_plan.candidate_identities), 1)
        self.assertEqual(
            set(
                item["identity"]
                for item in _candidate_audit(gaps)["candidates"]
            ),
            set(expected_identities),
        )

    def test_mixed_revisions_without_one_authoritative_generation_fail_closed(self) -> None:
        records = _pydantic_records()
        records[-1]["data"]["repository_revision"] = 8
        graph = TraceGraph.from_trace(omission_trace(records))

        candidates = _reconstruct(graph)
        audit = getattr(graph, "obligation_gap_reconstruction_audit", {})

        self.assertEqual(candidates, ())
        self.assertEqual(audit.get("candidate_count"), 0)
        self.assertIn(
            "incompatible_formal_revision_binding",
            {item["reason"] for item in audit.get("rejections", ())},
        )

    def test_invalid_obligation_input_is_recorded_in_rejection_audit(self) -> None:
        records = _pydantic_records()
        records[0]["data"]["task_obligations"][0]["evidence_refs"] = (
            "record:descriptor_contract"
        )
        graph = TraceGraph.from_trace(omission_trace(records))

        candidates = _reconstruct(graph)
        audit = getattr(graph, "obligation_gap_reconstruction_audit", {})

        self.assertEqual(candidates, ())
        self.assertIn(
            {
                "reason": "invalid_obligation_evidence_refs",
                "source_ref": "record:descriptor_contract",
            },
            audit.get("rejections", ()),
        )

    def test_camel_case_review_aliases_are_zero_contribution_and_redacted(self) -> None:
        secret_label = "SECRET_REVIEW_LABEL_REQUIRES_PACKAGING"
        records = [
            _record(
                "camel_review_contract",
                "message.input",
                "2026-07-31T13:00:00Z",
                {
                    "text": "Tighten warning formatting.",
                    "reviewOnly": {
                        "task_obligations": [
                            {
                                "obligation_id": secret_label,
                                "obligation_text": (
                                    "The contract requires packaging."
                                ),
                                "required_capabilities": ["packaging"],
                                "evidence_refs": "record:camel_review_contract",
                            }
                        ]
                    },
                    "expectedRoots": {
                        "text": "The runtime contract requires packaging."
                    },
                    "humanLabels": {
                        "missingSymbol": "packaging",
                    },
                },
                component="user",
            ),
            _record(
                "camel_review_decision",
                "decision",
                "2026-07-31T13:01:00Z",
                {"rationale": "Exclude packaging from this restoration."},
                component="agent",
            ),
            self._packaging_failure(
                "camel_review_failure",
                "2026-07-31T13:02:00Z",
            ),
        ]
        records[-1]["data"]["failure_binding"] = {
            "binding_type": "decision",
            "decision_ref": "record:camel_review_decision",
        }
        graph = TraceGraph.from_trace(omission_trace(records))

        self.assertEqual(_reconstruct(graph), ())
        serialized_audit = stable_json(
            getattr(graph, "obligation_gap_reconstruction_audit", {})
        )
        for rejected_value in (
            secret_label,
            "The contract requires packaging.",
            "The runtime contract requires packaging.",
            "missingSymbol",
        ):
            self.assertNotIn(rejected_value, serialized_audit)

        identity_graph = TraceGraph.from_trace(omission_trace())
        decision = identity_graph.nodes["record:decisionnode_dec_293"]
        edge = identity_graph.edge_context(
            decision.ref, "record:change_get"
        )[0]
        baseline = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="confirmed_edge",
            edge=edge,
            evidence_refs=(decision.ref,),
        )
        polluted = CausalCandidate(
            ref=decision.ref,
            node=decision,
            source="confirmed_edge",
            edge={
                **edge,
                "reviewOnly": {"value": secret_label},
                "expectedRoots": ["record:decisionnode_dec_293"],
                "humanLabels": {"root": secret_label},
            },
            evidence_refs=(decision.ref,),
        )
        self.assertEqual(candidate_identity(polluted), candidate_identity(baseline))

    def test_conflicting_formal_failure_binding_is_authoritative(self) -> None:
        conflicts = (
            ("obligation", {"obligation_id": "another-obligation"}),
            ("subsystem", {"subsystem": "docs.rendering"}),
            ("case", {"case_id": "another-case"}),
            ("subject_revision", {"subject_revision": "git:other"}),
            ("repository_revision", {"repository_revision": 99}),
            ("seed", {"seed_binding_identity": "seed:other"}),
        )
        for label, conflict in conflicts:
            with self.subTest(binding=label):
                records = _pydantic_records()
                contract = records[0]["data"]["task_obligations"][0]
                contract["subsystem"] = "pydantic.descriptor_lifecycle"
                contract["seed_binding_identity"] = "seed:descriptor"
                failure = records[-1]
                failure["source_refs"] = ["record:change_get"]
                failure["data"]["failure_binding"] = {
                    "binding_type": "obligation",
                    "obligation_id": "descriptor-protocol",
                    "subsystem": "pydantic.descriptor_lifecycle",
                    "case_id": CASE_ID,
                    "subject_revision": SUBJECT_REVISION,
                    "repository_revision": 7,
                    "seed_binding_identity": "seed:descriptor",
                    **conflict,
                }
                if label == "subsystem":
                    failure["data"]["failure_signature"]["subsystem"] = (
                        "docs.rendering"
                    )
                trace = omission_trace(records)
                trace["dataflow_edges"].append(
                    {
                        "from": {"type": "record", "id": "change_get"},
                        "to": {
                            "type": "record",
                            "id": "verification_missing_set_name",
                        },
                        "relation": "change_observed_by_verification",
                        "evidence_type": "confirmed",
                        "confidence": 1.0,
                        "eligible_for_attribution": True,
                    }
                )

                self.assertEqual(
                    _reconstruct(TraceGraph.from_trace(trace)),
                    (),
                )

    def test_conflicting_canonical_binding_aliases_are_order_independent(self) -> None:
        alias_orders = (
            (
                ("obligation_id", "descriptor-protocol"),
                ("ObligationID", "another-obligation"),
            ),
            (
                ("ObligationID", "another-obligation"),
                ("obligation_id", "descriptor-protocol"),
            ),
        )
        for aliases in alias_orders:
            with self.subTest(aliases=aliases):
                records = _pydantic_records()
                failure = records[-1]
                failure["source_refs"] = ["record:change_get"]
                failure["data"]["failure_binding"] = {
                    "binding_type": "obligation",
                    **dict(aliases),
                }
                trace = omission_trace(records)
                trace["dataflow_edges"].append(
                    {
                        "from": {"type": "record", "id": "change_get"},
                        "to": {
                            "type": "record",
                            "id": "verification_missing_set_name",
                        },
                        "relation": "change_observed_by_verification",
                        "evidence_type": "confirmed",
                        "confidence": 1.0,
                        "eligible_for_attribution": True,
                    }
                )

                self.assertEqual(
                    _reconstruct(TraceGraph.from_trace(trace)),
                    (),
                )

    def test_conflicting_decision_ref_aliases_all_permutations_are_rejected(self) -> None:
        aliases = (
            ("decision_ref", "record:decisionnode_dec_293"),
            ("DecisionRef", "record:another_decision"),
            ("decisionRef", "record:third_decision"),
        )
        for permutation in itertools.permutations(aliases):
            with self.subTest(aliases=permutation):
                records = _pydantic_records()
                records[-1]["data"]["failure_binding"] = {
                    "binding_type": "obligation",
                    "obligation_id": "descriptor-protocol",
                    **dict(permutation),
                }

                self.assertEqual(
                    _reconstruct(TraceGraph.from_trace(omission_trace(records))),
                    (),
                )

    def test_natural_contract_rejects_non_current_non_affirmative_text(self) -> None:
        prohibited_contracts = (
            (
                "The descriptor contract does not require __get__ or "
                "__set_name__."
            ),
            (
                'Example: "The descriptor contract requires __get__ and '
                '__set_name__."'
            ),
            (
                "Historically the descriptor contract required __get__ and "
                "__set_name__."
            ),
            (
                "For the current task, the historical descriptor contract "
                "requires __get__ and __set_name__."
            ),
            (
                "A future release will require __get__ and __set_name__."
            ),
            (
                "The descriptor contract could require __get__ and "
                "__set_name__ instead."
            ),
            (
                "Version1 requires the descriptor contract to preserve "
                "__get__ and __set_name__."
            ),
            (
                "The retired descriptor contract requires __get__ and "
                "__set_name__."
            ),
            (
                "The archived descriptor contract requires __get__ and "
                "__set_name__."
            ),
            (
                "The next release requires the descriptor contract to preserve "
                "__get__ and __set_name__."
            ),
            (
                "After the planned migration, the descriptor contract requires "
                "__get__ and __set_name__."
            ),
            (
                "The descriptor contract requires __get__ and __set_name__."
            ),
        )
        for text in prohibited_contracts:
            with self.subTest(text=text):
                records = _pydantic_records()
                records[0]["data"].pop("task_obligations")
                records[0]["data"]["text"] = text
                self.assertEqual(
                    _reconstruct(TraceGraph.from_trace(omission_trace(records))),
                    (),
                )

        records = _pydantic_records()
        records[0]["data"].pop("task_obligations")
        records[0]["data"]["text"] = "Tighten warning formatting."
        records[0]["data"]["metadata"] = {
            "text": (
                "The descriptor contract requires __get__ and __set_name__."
            )
        }
        self.assertEqual(
            _reconstruct(TraceGraph.from_trace(omission_trace(records))),
            (),
        )

        current_contracts = (
            "For the current task, restore __get__ and __set_name__ in the "
            "descriptor protocol.",
            "This request requires the descriptor contract to preserve __get__ "
            "and __set_name__.",
            "Now restore __get__ and __set_name__ in the descriptor protocol.",
            "此次需求 requires the descriptor contract to preserve __get__ and "
            "__set_name__.",
        )
        for text in current_contracts:
            with self.subTest(current_scope=text):
                current = _pydantic_records()
                current[0]["data"].pop("task_obligations")
                current[0]["data"]["text"] = text
                current_candidates = _reconstruct(
                    TraceGraph.from_trace(omission_trace(current))
                )
                self.assertEqual(len(current_candidates), 1)
                self.assertEqual(
                    current_candidates[0].gap_coverage,
                    ("__set_name__",),
                )

        structured = _pydantic_records()
        structured[0]["data"]["text"] = (
            "Historically the descriptor contract was archived."
        )
        structured_candidates = _reconstruct(
            TraceGraph.from_trace(omission_trace(structured))
        )
        self.assertEqual(len(structured_candidates), 1)
        self.assertEqual(
            structured_candidates[0].gap_coverage,
            ("__set_name__",),
        )

    def test_natural_contract_rejects_negated_current_scope_cue(self) -> None:
        records = _pydantic_records()
        records[0]["data"].pop("task_obligations")
        records[0]["data"]["text"] = (
            "Not now, restore __get__ and __set_name__ in the descriptor protocol."
        )

        self.assertEqual(
            _reconstruct(TraceGraph.from_trace(omission_trace(records))),
            (),
        )

    def test_natural_contract_rejects_textual_and_next_version_markers(self) -> None:
        prohibited_contracts = (
            (
                "For the current task, version two requires the descriptor "
                "contract to preserve __get__ and __set_name__."
            ),
            (
                "For the current task, the next version requires the descriptor "
                "contract to preserve __get__ and __set_name__."
            ),
        )
        for text in prohibited_contracts:
            with self.subTest(text=text):
                records = _pydantic_records()
                records[0]["data"].pop("task_obligations")
                records[0]["data"]["text"] = text

                self.assertEqual(
                    _reconstruct(TraceGraph.from_trace(omission_trace(records))),
                    (),
                )

    def test_coordinated_local_negation_and_graph_path_define_coverage_order(self) -> None:
        records = [
            _record(
                "contract_three_capabilities",
                "message.input",
                "",
                {
                    "task_obligations": [
                        {
                            "obligation_id": "three-capabilities",
                            "obligation_text": (
                                "Restore packaging, wheel, and twine."
                            ),
                            "required_capabilities": [
                                "packaging",
                                "wheel",
                                "twine",
                            ],
                        }
                    ]
                },
                component="user",
            ),
            _record(
                "m_decision_three",
                "decision",
                "",
                {
                    "rationale": (
                        "Do not implement changelog but implement packaging "
                        "and implement wheel; exclude twine."
                    )
                },
                component="agent",
            ),
            _record(
                "z_action_three",
                "change",
                "",
                {
                    "summary": (
                        "Did not update changelog but implemented packaging "
                        "and implemented wheel."
                    )
                },
                component="tool",
                status="success",
            ),
            _record(
                "a_verification_three",
                "verification",
                "",
                {
                    "exit_code": 0,
                    "output": "Packaging and wheel tests passed.",
                },
                component="tool",
                status="success",
            ),
            _record(
                "b_failure_twine",
                "verification",
                "",
                {
                    "exit_code": 1,
                    "output": "Twine is required at runtime.",
                    **_typed_failure(
                        "three-capabilities",
                        "twine-runtime-failure",
                        subsystem="runtime.twine",
                    ),
                },
                component="tool",
                status="failure",
            ),
        ]
        trace = omission_trace(records)
        trace["dataflow_edges"] = [
            {
                "from": {"type": "record", "id": source},
                "to": {"type": "record", "id": target},
                "relation": relation,
                "evidence_type": "confirmed",
                "confidence": 1.0,
                "eligible_for_attribution": True,
            }
            for source, target, relation in (
                (
                    "m_decision_three",
                    "z_action_three",
                    "decision_guided_change",
                ),
                (
                    "z_action_three",
                    "a_verification_three",
                    "change_verified_by",
                ),
                (
                    "z_action_three",
                    "b_failure_twine",
                    "change_observed_by_verification",
                ),
            )
        ]

        candidates = _reconstruct(TraceGraph.from_trace(trace))

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].planned_coverage, ("packaging", "wheel"))
        self.assertEqual(
            candidates[0].actual_action_coverage,
            ("packaging", "wheel"),
        )
        self.assertEqual(
            candidates[0].verification_coverage,
            ("packaging", "wheel"),
        )
        self.assertEqual(candidates[0].gap_coverage, ("twine",))

    def test_three_obligation_identities_survive_every_projection_and_checkpoint(self) -> None:
        obligation_specs = (
            ("packaging-runtime", "packaging"),
            ("twine-runtime", "twine"),
            ("wheel-runtime", "wheel"),
        )
        records = [
            _record(
                "triple_contract",
                "message.input",
                "2026-07-31T14:00:00Z",
                {
                    "task_obligations": [
                        {
                            "obligation_id": obligation_id,
                            "obligation_text": "Preserve {0}.".format(capability),
                            "required_capabilities": [capability],
                        }
                        for obligation_id, capability in obligation_specs
                    ]
                },
                component="user",
            ),
            _record(
                "triple_decision",
                "decision",
                "2026-07-31T14:01:00Z",
                {
                    "rationale": (
                        "Exclude packaging, twine, and wheel from restoration."
                    )
                },
                component="agent",
            ),
            *(
                _record(
                    "triple_{0}_failure".format(capability),
                    "verification",
                    "2026-07-31T14:0{0}:00Z".format(index + 2),
                    {
                        "exit_code": 1,
                        "output": "{0} is required at runtime.".format(
                            capability.title()
                        ),
                        **_typed_failure(
                            obligation_id,
                            "triple-{0}-failure".format(capability),
                            subsystem="runtime.{0}".format(capability),
                        ),
                    },
                    component="tool",
                    status="failure",
                )
                for index, (obligation_id, capability) in enumerate(
                    obligation_specs
                )
            ),
        ]
        trace = omission_trace(records)
        trace["dataflow_edges"] = []
        graph = TraceGraph.from_trace(trace)
        gaps = _reconstruct(graph)
        routes = _causal_candidates(graph)
        expected_identities = tuple(sorted(gap.identity for gap in gaps))
        self.assertEqual(len(expected_identities), 3)

        selection = select_global_candidates(graph, tuple(reversed(routes)))
        selection_payload = selection.to_dict()
        projection_candidate = canonical_candidate_route(
            graph,
            "record:triple_decision",
            selection.discovered,
        )
        defect_state = DefectState.create(
            label="triple-runtime-gap",
            expected="all three dependencies remain available",
            actual="all three obligations failed",
            mechanism="dependency restoration gap",
            scope="task_quality",
        )
        paths = {"record:triple_decision": ("record:triple_decision",)}
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(projection_candidate,),
            defect_state=defect_state,
            downstream_paths=paths,
            start_refs=("record:triple_decision",),
        )
        capsule_identities = tuple(
            item["identity"]
            for item in capsules[0].to_dict()["candidate"]["retrieval_edge"][
                "attribution_only_obligation_gaps"
            ]
        )
        self.assertEqual(capsule_identities, expected_identities)

        manifest = build_candidate_cluster_manifest(
            graph=graph,
            candidates=selection.discovered,
            candidate_paths=paths,
            candidate_audit=selection_payload["candidate_audit"],
            source_selection_identity=selection.selection_identity,
            seed_ref="record:triple_decision",
            defect_fingerprint=defect_state.fingerprint,
        )
        self.assertEqual(
            manifest.candidate_facts[0].attribution_only_gap_identities,
            expected_identities,
        )
        self.assertEqual(
            manifest.clusters[0].attribution_only_gap_identities,
            expected_identities,
        )
        self.assertEqual(
            CandidateClusterManifest.from_dict(manifest.to_dict()).to_dict(),
            manifest.to_dict(),
        )

        page_plan = build_candidate_page_plan(
            seed_ref="record:triple_decision",
            defect_fingerprint=defect_state.fingerprint,
            capsules=capsules,
            round_index=0,
        )
        self.assertEqual(
            page_plan.attribution_only_gap_identities,
            (expected_identities,),
        )
        self.assertEqual(
            page_plan.pages[0].attribution_only_gap_identities,
            (expected_identities,),
        )
        restored_plan = CandidatePagePlan.from_dict(page_plan.to_dict())
        self.assertEqual(
            restored_plan.attribution_only_gap_identities,
            (expected_identities,),
        )

        checkpoint_config = build_checkpoint_config(
            trace=trace,
            case_id=CASE_ID,
            objective="Preserve every obligation gap identity.",
            analysis_perspective="offline attribution",
            start_refs=("record:triple_decision",),
            budgets={
                "max_frontier_items": 96,
                "max_depth": 20,
                "max_hypotheses": 24,
                "max_investigation_rounds": 12,
                "max_artifact_bytes": 1_048_576,
                "max_judge_requests": 128,
            },
            model_identity="offline:test",
            cache_identity="cache:test",
            runtime_identity={
                "judge_timeout_sec": 3600.0,
                "judge_max_tokens": 4096,
                "thinking_mode": "disabled",
                "base_url": "offline://test",
                "provider_error_threshold": 3,
                "fusion_mode": "retrieval-global",
            },
        )
        with tempfile.TemporaryDirectory() as tempdir:
            bundle = CheckpointBundle(Path(tempdir) / "triple.checkpoint")
            bundle.initialize(checkpoint_config)
            bundle.record_action(
                "global_candidate_page_plan",
                "triple:page-plan",
                {"plan": page_plan.to_dict()},
            )
            checkpoint_state = bundle.restore(
                expected_config=checkpoint_config
            )
            checkpoint_plan = CandidatePagePlan.from_dict(
                checkpoint_state.actions[-1]["payload"]["plan"]
            )
        self.assertEqual(
            checkpoint_plan.attribution_only_gap_identities,
            (expected_identities,),
        )

        report_audit = _candidate_audit(gaps)
        self.assertEqual(
            tuple(
                sorted(
                    item["identity"]
                    for item in report_audit["candidates"]
                )
            ),
            expected_identities,
        )


if __name__ == "__main__":
    unittest.main()
