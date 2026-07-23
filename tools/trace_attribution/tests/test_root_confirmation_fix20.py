from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from trace_attribution.causal_judge import (
    RootConfirmationRequest,
    build_causal_step_prompt,
    build_recursive_confirmation_prompt,
    validate_recursive_confirmation,
)
from trace_attribution.causal_state import (
    CausalCandidate,
    DefectState,
)
from trace_attribution.evidence_capsule import build_candidate_evidence_capsules
from trace_attribution.global_judge import (
    GlobalCandidateJudgeRequest,
    active_focus_text_sha256,
    build_global_candidate_prompt,
)
from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import RecursiveAnalysisState
from tools.trace_attribution.tests.test_causal_judge import (
    sample_confirmation_request,
    valid_confirmation_payload,
)


OBJECTIVE = "Confirm each active defect seed independently."
VISIBLE_MARKER = "FIX20_VISIBLE_FACT"


def active_defect() -> DefectState:
    return DefectState.create(
        label="active_defect",
        expected="The active decision is correct.",
        actual="The active decision contains a defect.",
        mechanism="Only active, graph-grounded evidence may decide the defect.",
        scope="task_quality",
    )


def reference_envelope(
    ref: str,
    *,
    raw_ref: str | None = None,
    **aliases,
) -> dict:
    return {
        "raw_ref": raw_ref or ref,
        "resolved_ref": ref,
        "resolution_status": "resolved",
        "provenance_class": "recorded",
        **aliases,
    }


def reviewer_trace(payload: dict, *, artifact: dict | None = None) -> dict:
    trace = {
        "case_id": "fix20-reviewer",
        "records": [
            {
                "record_id": "decision",
                "span_id": "decision-alias",
                "component": "agent",
                "event_type": "decision",
                "data": {"reviewer_payload": payload},
            },
            {
                "record_id": "alternative",
                "component": "agent",
                "event_type": "decision",
                "data": {"summary": "Another active decision."},
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
    if artifact is not None:
        trace["artifacts"] = [artifact]
        trace["records"][0]["artifact_refs"] = [
            "artifact:{0}".format(artifact["artifact_id"])
        ]
    return trace


def candidate_for(graph: TraceGraph) -> CausalCandidate:
    return CausalCandidate(
        ref="record:decision",
        node=graph.nodes["record:decision"],
        source="confirmed_edge",
        edge=graph.edge_context("record:decision", "record:defect")[0],
        evidence_refs=("record:decision", "record:defect"),
    )


def final_json(graph: TraceGraph, stage: str) -> dict:
    defect = active_defect()
    candidate = candidate_for(graph)
    if stage == "global":
        capsules = build_candidate_evidence_capsules(
            graph=graph,
            candidates=(candidate,),
            defect_state=defect,
            downstream_paths={
                "record:decision": ("record:decision", "record:defect")
            },
            start_refs=("record:defect",),
        )
        request = GlobalCandidateJudgeRequest(
            case_id=graph.case_id,
            objective=OBJECTIVE,
            analysis_perspective="",
            seed_ref="record:defect",
            active_defect=defect,
            active_focus_text=defect.actual,
            active_focus_text_hash=active_focus_text_sha256(defect.actual),
            start_refs=("record:defect",),
            capsules=capsules,
        )
        return json.loads(build_global_candidate_prompt(request))
    if stage == "step":
        state = RecursiveAnalysisState.create(
            graph=graph,
            start_refs=("record:defect",),
            objective=OBJECTIVE,
            analysis_perspective="",
        )
        request = state.build_step_request(
            graph,
            state.frontier.pop(),
            (candidate,),
        )
        return json.loads(build_causal_step_prompt(request))
    if stage != "confirmation":
        raise AssertionError("unknown stage: {0}".format(stage))
    candidate_reference = {
        **reference_envelope(
            "record:decision",
            raw_ref="decision",
            canonical_ref="record:decision",
        ),
        "content": stable_json(graph.nodes["record:decision"].data),
    }
    request = RootConfirmationRequest(
        candidate_ref="record:decision",
        defect_state=defect,
        recursive_path=("record:decision", "record:defect"),
        candidate_reference=candidate_reference,
        recursive_path_references=(
            candidate_reference,
            reference_envelope("record:defect"),
        ),
        supporting_evidence=(candidate_reference,),
        opposing_evidence=(),
        competing_hypotheses=(),
        task_obligations=(),
        analysis_perspective="",
    )
    sanitized = graph.sanitize_judge_visible_payload(request.factual_dict())
    request = replace(
        request,
        candidate_reference=sanitized["candidate_reference"],
        recursive_path_references=tuple(
            sanitized["recursive_path_references"]
        ),
        supporting_evidence=tuple(sanitized["supporting_evidence"]),
    )
    return json.loads(build_recursive_confirmation_prompt(request))


def contains_pair(value, key: str, expected) -> bool:
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


def verified_artifact(content: str) -> dict:
    content_bytes = content.encode("utf-8")
    return {
        "artifact_id": "verified",
        "kind": "text",
        "path": "artifacts/verified.txt",
        "content_hash": hashlib.sha256(content_bytes).hexdigest(),
        "byte_length": len(content_bytes),
    }


def artifact_fact(content: str, *, owner: dict | None = None, **updates) -> dict:
    content_bytes = content.encode("utf-8")
    value = {
        "artifact_id": "verified",
        "raw_ref": "artifact:verified",
        "resolved_ref": "artifact:verified",
        "canonical_ref": "artifact:verified",
        "resolution_status": "resolved",
        "provenance_class": "recorded",
        "content": content,
        "content_hash": "sha256:{0}".format(
            hashlib.sha256(content_bytes).hexdigest()
        ),
        "byte_count": len(content_bytes),
        "byte_range": [0, len(content_bytes)],
        "owner_reference": owner or reference_envelope("record:decision"),
        "missing": False,
        "truncated": False,
        "summary": VISIBLE_MARKER,
    }
    value.update(updates)
    return value


class TypedIdentityEquivalenceTest(unittest.TestCase):
    def assert_all_stages_hide_marker(self, graph: TraceGraph) -> None:
        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                self.assertNotIn(VISIBLE_MARKER, stable_json(final_json(graph, stage)))

    def assert_all_stages_show_marker(self, graph: TraceGraph) -> None:
        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                self.assertIn(VISIBLE_MARKER, stable_json(final_json(graph, stage)))

    def test_conflicting_active_alias_identity_atomically_removes_fact_from_all_final_json(self):
        for alias in ("ref", "citation_ref", "node_ref"):
            payload = {
                "evidence_fact": {
                    **reference_envelope(
                        "record:decision",
                        raw_ref="decision",
                        canonical_ref="record:decision",
                    ),
                    alias: "record:alternative",
                    "summary": VISIBLE_MARKER,
                }
            }
            with self.subTest(alias=alias):
                self.assert_all_stages_hide_marker(
                    TraceGraph.from_trace(reviewer_trace(payload))
                )

    def test_shape_specific_candidate_alias_is_compared_with_envelope_identity(self):
        payload = {
            "candidate_reference": {
                **reference_envelope("record:decision"),
                "candidate_ref": "record:alternative",
                "summary": VISIBLE_MARKER,
            }
        }
        self.assert_all_stages_hide_marker(
            TraceGraph.from_trace(reviewer_trace(payload))
        )

    def test_equivalent_aliases_and_valid_single_identity_facts_survive(self):
        aliases = {
            "ref": "record:decision",
            "raw_ref": "decision",
            "resolved_ref": "record:decision",
            "canonical_ref": "record:decision",
            "citation_ref": "record:decision",
            "node_ref": "record:decision",
            "resolution_status": "resolved",
            "provenance_class": "recorded",
            "summary": VISIBLE_MARKER,
        }
        graph = TraceGraph.from_trace(
            reviewer_trace(
                {
                    "evidence_fact": aliases,
                    "single_identity_facts": [
                        {
                            key: "decision",
                            "evidence_type": "trace_record",
                            "summary": "{0}_{1}".format(VISIBLE_MARKER, key),
                        }
                        for key in (
                            "ref",
                            "raw_ref",
                            "resolved_ref",
                            "canonical_ref",
                            "citation_ref",
                            "node_ref",
                        )
                    ],
                }
            )
        )

        self.assert_all_stages_show_marker(graph)
        sanitized = graph.sanitize_judge_visible_payload(
            graph.nodes["record:decision"].data
        )
        self.assertEqual(
            len(sanitized["reviewer_payload"]["single_identity_facts"]),
            6,
        )

    def test_role_distinct_edge_endpoints_are_not_compared_as_aliases(self):
        payload = {
            "evidence_edge": {
                "from_ref": "record:decision",
                "to_ref": "record:defect",
                "evidence_type": "confirmed",
                "summary": VISIBLE_MARKER,
            }
        }
        self.assert_all_stages_show_marker(
            TraceGraph.from_trace(reviewer_trace(payload))
        )

    def test_conflicting_owner_alias_atomically_removes_artifact_from_all_final_json(self):
        content = "complete verified artifact"
        owner = reference_envelope(
            "record:decision",
            raw_ref="record:alternative",
            canonical_ref="record:decision",
        )
        payload = {"verified_artifact": artifact_fact(content, owner=owner)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "artifacts/verified.txt"
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            graph = TraceGraph.from_trace(
                reviewer_trace(payload, artifact=verified_artifact(content)),
                artifact_root=root,
            )
            self.assert_all_stages_hide_marker(graph)

    def test_owner_mismatch_propagates_through_hydration_parent(self):
        content = "complete verified artifact"
        payload = {
            "artifact_hydration": {
                "node_ref": "record:decision",
                "referenced_artifact_ids": ["verified"],
                "hydrated_artifacts": [
                    artifact_fact(
                        content,
                        owner=reference_envelope("record:alternative"),
                    )
                ],
                "missing_artifact_ids": [],
                "truncated_artifact_ids": [],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "artifacts/verified.txt"
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            graph = TraceGraph.from_trace(
                reviewer_trace(payload, artifact=verified_artifact(content)),
                artifact_root=root,
            )
            self.assert_all_stages_hide_marker(graph)

    def test_owner_mismatch_without_content_still_removes_parent_artifact_fact(self):
        content = "complete verified artifact"
        fact = artifact_fact(
            content,
            owner=reference_envelope("record:alternative"),
        )
        for key in ("content", "content_hash", "byte_count", "byte_range"):
            fact.pop(key)
        payload = {
            "artifact_hydration": {
                "node_ref": "record:decision",
                "referenced_artifact_ids": ["verified"],
                "hydrated_artifacts": [fact],
                "missing_artifact_ids": [],
                "truncated_artifact_ids": [],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "artifacts/verified.txt"
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            graph = TraceGraph.from_trace(
                reviewer_trace(payload, artifact=verified_artifact(content)),
                artifact_root=root,
            )
            self.assert_all_stages_hide_marker(graph)


class VerifiedArtifactSubrangeTest(unittest.TestCase):
    full_content = "prefix|证据-世界|suffix"

    @classmethod
    def valid_subrange(cls) -> tuple[int, int, str]:
        content_bytes = cls.full_content.encode("utf-8")
        start = content_bytes.index("证".encode("utf-8"))
        end = content_bytes.index("|suffix".encode("utf-8"))
        return start, end, content_bytes[start:end].decode("utf-8")

    def graph_for(self, root: Path, fact: dict) -> TraceGraph:
        target = root / "artifacts/verified.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.full_content, encoding="utf-8")
        return TraceGraph.from_trace(
            reviewer_trace(
                {"verified_artifact": fact},
                artifact=verified_artifact(self.full_content),
            ),
            artifact_root=root,
        )

    def valid_fact(self) -> dict:
        start, end, content = self.valid_subrange()
        return artifact_fact(
            content,
            byte_range=[start, end],
        )

    def assert_all_stages_show_fact(self, graph: TraceGraph, fact: dict) -> None:
        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                payload = final_json(graph, stage)
                self.assertIn(VISIBLE_MARKER, stable_json(payload))
                self.assertTrue(
                    contains_pair(payload, "byte_range", fact["byte_range"])
                )

    def assert_all_stages_hide_fact(self, graph: TraceGraph) -> None:
        for stage in ("global", "step", "confirmation"):
            with self.subTest(stage=stage):
                self.assertNotIn(
                    VISIBLE_MARKER,
                    stable_json(final_json(graph, stage)),
                )

    def test_valid_utf8_verified_subrange_survives_all_final_json(self):
        fact = self.valid_fact()
        with tempfile.TemporaryDirectory() as directory:
            self.assert_all_stages_show_fact(
                self.graph_for(Path(directory), fact),
                fact,
            )

    def test_valid_empty_verified_subrange_survives_all_final_json(self):
        start, _, _ = self.valid_subrange()
        fact = artifact_fact("", byte_range=[start, start])
        with tempfile.TemporaryDirectory() as directory:
            self.assert_all_stages_show_fact(
                self.graph_for(Path(directory), fact),
                fact,
            )

    def test_confirmation_grounding_accepts_nonzero_artifact_subrange(self):
        start, end, content = self.valid_subrange()
        manifest = {
            "node_ref": "record:decision",
            "referenced_artifact_ids": ["verified"],
            "hydrated_artifacts": [
                artifact_fact(content, byte_range=[start, end])
            ],
            "missing_artifact_ids": [],
            "truncated_artifact_ids": [],
            "ineligible_artifact_evidence": [],
        }
        candidate_fact = {
            **reference_envelope("record:decision"),
            "fact_kind": "candidate_fact",
            "decisive": True,
            "artifact_hydration": manifest,
        }
        payload = valid_confirmation_payload()
        payload["excerpt"] = content
        payload["evidence_refs"] = ["record:decision"]

        result = validate_recursive_confirmation(
            payload,
            request=sample_confirmation_request(
                supporting_evidence=(candidate_fact,)
            ),
        )

        self.assertEqual(result.status, "confirmed")

    def test_invalid_subranges_are_audit_only_in_all_final_json(self):
        start, end, content = self.valid_subrange()
        full_bytes = self.full_content.encode("utf-8")
        invalid_facts = {
            "boolean_start": {**self.valid_fact(), "byte_range": [False, end]},
            "reversed": {**self.valid_fact(), "byte_range": [end, start]},
            "out_of_bounds": {
                **self.valid_fact(),
                "byte_range": [
                    len(full_bytes) - len(content.encode("utf-8")) + 1,
                    len(full_bytes) + 1,
                ],
            },
            "misaligned_utf8": {
                **self.valid_fact(),
                "byte_range": [start + 1, end + 1],
            },
            "wrong_offset": {
                **self.valid_fact(),
                "byte_range": [0, end - start],
            },
            "content_mismatch": artifact_fact(
                content[:-1] + "国",
                byte_range=[start, end],
            ),
            "hash_mismatch": {
                **self.valid_fact(),
                "content_hash": "sha256:" + "0" * 64,
            },
            "byte_count_mismatch": {
                **self.valid_fact(),
                "byte_count": len(content.encode("utf-8")) + 1,
            },
            "missing": {**self.valid_fact(), "missing": True},
            "truncated": {**self.valid_fact(), "truncated": True},
            "unresolved": {
                **self.valid_fact(),
                "resolution_status": "unresolved",
            },
            "declared_unavailable": {
                **self.valid_fact(),
                "availability": "missing",
            },
            "false_availability": {
                **self.valid_fact(),
                "available": False,
            },
            "wrong_artifact": {
                **self.valid_fact(),
                "artifact_id": "other",
                "raw_ref": "artifact:other",
                "resolved_ref": "artifact:other",
                "canonical_ref": "artifact:other",
            },
        }

        for name, fact in invalid_facts.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.assert_all_stages_hide_fact(
                    self.graph_for(Path(directory), fact)
                )


if __name__ == "__main__":
    unittest.main()
