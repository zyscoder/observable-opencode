from __future__ import annotations

import unittest
import json
import hashlib
import tempfile
from pathlib import Path

from trace_attribution import service
from trace_attribution.expected_action import build_expected_action_projection
from trace_attribution.graph import TraceGraph
from trace_attribution.request import AttributionQuestion
from trace_attribution.service import (
    bind_question_hypothesis,
    question_premise_inconclusive_report,
)


def _record(record_id: str, event_type: str, data: dict, *, component: str) -> dict:
    return {
        "record_id": record_id,
        "event_type": event_type,
        "component": component,
        "data": data,
    }


def _graph(*records: dict) -> TraceGraph:
    return TraceGraph.from_trace(
        {
            "trace_version": "6.0",
            "manifest": {"case_id": "skill-omission-case"},
            "records": list(records),
            "dataflow_edges": [],
        }
    )


def _request() -> dict:
    return _record(
        "user_request",
        "message.input",
        {"text": "这是一次特性迁移，请完成设计和开发。", "session_id": "ses_1"},
        component="user",
    )


def _catalog(*, exposed: bool = True, permission: str = "allow") -> dict:
    selected = {
        "name": "migration-consistency",
        "description": "当进行特性迁移时必须调用，执行一致性校验并输出结果。",
        "location": "/repo/.opencode/skills/migration-consistency/SKILL.md",
    }
    return _record(
        "skill_catalog",
        "skill.catalog.exposed",
        {
            "session_id": "ses_1",
            "message_id": "msg_1",
            "agent": "build",
            "skill_tool_available": True,
            "selected": [
                {"name": selected["name"], "location": selected["location"]}
            ],
            "exposed": [selected] if exposed else [],
            "permission_evaluations": [
                {"name": selected["name"], "action": permission}
            ],
            "candidates": [
                {
                    **selected,
                    "status": "loaded",
                    "source_family": "opencode",
                    "source_scope": "project",
                }
            ],
            "conflicts": [],
            "parse_failures": [],
        },
        component="skill",
    )


def _catalog_with_unrelated_exposed_skill() -> dict:
    catalog = _catalog()
    unrelated = {
        "name": "repository-search",
        "description": "Use this skill to locate repository symbols and files.",
        "location": "/repo/.opencode/skills/repository-search/SKILL.md",
        "status": "loaded",
        "source_family": "opencode",
        "source_scope": "project",
    }
    catalog["data"]["candidates"].append(unrelated)
    catalog["data"]["selected"].append(
        {"name": unrelated["name"], "location": unrelated["location"]}
    )
    catalog["data"]["exposed"].append(unrelated)
    catalog["data"]["permission_evaluations"].append(
        {"name": unrelated["name"], "action": "allow"}
    )
    return catalog


class SkillOmissionProjectionTest(unittest.TestCase):
    question = (
        "为什么特性迁移一致性校验 Skill 没有被调用？请判断是 Skill 配置、"
        "上下文、权限还是 Agent 决策问题。"
    )

    def test_exposed_skill_without_invocation_is_projected_as_absent(self) -> None:
        projection = build_expected_action_projection(
            _graph(_request(), _catalog()), self.question
        )

        self.assertIsNotNone(projection)
        assert projection is not None
        self.assertEqual(projection["schema"], "expected-action-projection/v1")
        self.assertEqual(projection["action_kind"], "skill.invoke")
        self.assertEqual(projection["observed_action"], "absent")
        self.assertEqual(projection["candidate_skill_names"], ["migration-consistency"])
        self.assertEqual(projection["exposed_skill_names"], ["migration-consistency"])
        self.assertEqual(projection["invoked_skill_names"], [])
        self.assertEqual(
            projection["expected_skill_lifecycle"],
            [
                {
                    "name": "migration-consistency",
                    "location": "/repo/.opencode/skills/migration-consistency/SKILL.md",
                    "source_family": "opencode",
                    "source_scope": "project",
                    "discovered": True,
                    "selected": True,
                    "exposed": True,
                    "permission_action": "allow",
                    "skill_tool_available": True,
                    "invoked": False,
                    "first_absent_stage": "invocation",
                    "trigger_match_basis": "request_description_overlap",
                }
            ],
        )
        self.assertEqual(
            projection["evidence_refs"],
            ["record:user_request", "record:skill_catalog"],
        )

    def test_matching_invocation_is_projected_as_present(self) -> None:
        invocation = _record(
            "skill_load",
            "skill.load",
            {
                "session_id": "ses_1",
                "skill_name": "migration-consistency",
                "request_status": "completed",
            },
            component="skill",
        )
        projection = build_expected_action_projection(
            _graph(_request(), _catalog(), invocation), self.question
        )

        assert projection is not None
        self.assertEqual(projection["observed_action"], "present")
        self.assertEqual(projection["invoked_skill_names"], ["migration-consistency"])
        self.assertIn("record:skill_load", projection["evidence_refs"])

    def test_unrelated_skill_invocation_does_not_satisfy_exposed_candidate(self) -> None:
        invocation = _record(
            "other_skill_load",
            "skill.load",
            {
                "session_id": "ses_1",
                "skill_name": "repository-search",
                "request_status": "completed",
            },
            component="skill",
        )
        projection = build_expected_action_projection(
            _graph(_request(), _catalog(), invocation), self.question
        )

        assert projection is not None
        self.assertEqual(projection["observed_action"], "absent")
        self.assertEqual(projection["invoked_skill_names"], ["repository-search"])

    def test_invoking_another_exposed_skill_does_not_satisfy_expected_skill(self) -> None:
        invocation = _record(
            "other_exposed_skill_load",
            "skill.load",
            {
                "session_id": "ses_1",
                "skill_name": "repository-search",
                "request_status": "completed",
            },
            component="skill",
        )

        projection = build_expected_action_projection(
            _graph(_request(), _catalog_with_unrelated_exposed_skill(), invocation),
            self.question,
        )

        assert projection is not None
        self.assertEqual(
            projection["expected_skill_names"], ["migration-consistency"]
        )
        self.assertEqual(projection["matching_invoked_skill_names"], [])
        self.assertEqual(projection["observed_action"], "absent")

    def test_permission_filtered_skill_preserves_filter_evidence(self) -> None:
        projection = build_expected_action_projection(
            _graph(_request(), _catalog(exposed=False, permission="deny")),
            self.question,
        )

        assert projection is not None
        self.assertEqual(projection["observed_action"], "absent")
        self.assertEqual(projection["exposed_skill_names"], [])
        self.assertEqual(
            projection["permission_evaluations"],
            [{"name": "migration-consistency", "action": "deny"}],
        )
        self.assertEqual(
            projection["expected_skill_lifecycle"][0]["first_absent_stage"],
            "exposure",
        )

    def test_missing_catalog_candidate_is_distinguished_from_agent_omission(self) -> None:
        empty_catalog = _record(
            "empty_catalog",
            "skill.catalog.exposed",
            {
                "session_id": "ses_1",
                "skill_tool_available": True,
                "selected": [],
                "exposed": [],
                "permission_evaluations": [],
                "candidates": [],
                "conflicts": [],
                "parse_failures": [
                    {
                        "location": "/repo/.opencode/skills/migration/SKILL.md",
                        "error": "invalid frontmatter",
                    }
                ],
            },
            component="skill",
        )
        projection = build_expected_action_projection(
            _graph(_request(), empty_catalog), self.question
        )

        assert projection is not None
        self.assertEqual(projection["candidate_skill_names"], [])
        self.assertEqual(projection["observed_action"], "unknown")
        self.assertEqual(
            projection["parse_failures"][0]["error"], "invalid frontmatter"
        )

    def test_externalized_catalog_candidates_are_hydrated_for_matching(self) -> None:
        candidate = {
            "name": "migration-consistency",
            "description": "当进行特性迁移时必须调用，执行一致性校验并输出结果。",
            "location": "/repo/.opencode/skills/migration-consistency/SKILL.md",
            "status": "loaded",
            "source_family": "opencode",
            "source_scope": "project",
        }
        content = json.dumps([candidate], ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            artifact_path = Path(tmp) / "catalog-candidates.json"
            artifact_path.write_bytes(content)
            graph = TraceGraph.from_trace(
                {
                    "case_id": "externalized-skill-catalog",
                    "records": [
                        _request(),
                        _record(
                            "skill_catalog",
                            "skill.catalog.exposed",
                            {
                                "candidates": {
                                    "type": "array",
                                    "length": len(content),
                                    "hash": digest,
                                    "preview": "[...externalized...]",
                                    "artifact_id": "artifact_catalog_candidates",
                                    "payload_ref": "artifact_catalog_candidates",
                                },
                                "selected": [
                                    {
                                        "name": candidate["name"],
                                        "location": candidate["location"],
                                    }
                                ],
                                "exposed": [candidate],
                                "permission_evaluations": [
                                    {"name": candidate["name"], "action": "allow"}
                                ],
                                "skill_tool_available": True,
                                "parse_failures": [],
                                "conflicts": [],
                            },
                            component="skill",
                        ),
                    ],
                    "artifacts": [
                        {
                            "artifact_id": "artifact_catalog_candidates",
                            "kind": "json",
                            "label": "skill.catalog.exposed.candidates",
                            "path": artifact_path.name,
                            "hash": digest,
                            "content_hash": digest,
                            "byte_length": len(content),
                        }
                    ],
                },
                artifact_root=Path(tmp),
            )

            projection = build_expected_action_projection(graph, self.question)

        assert projection is not None
        self.assertEqual(
            projection["expected_skill_names"], ["migration-consistency"]
        )
        self.assertEqual(projection["observed_action"], "absent")

    def test_unrelated_question_does_not_create_skill_obligation(self) -> None:
        projection = build_expected_action_projection(
            _graph(_request(), _catalog()), "为什么这次代码编译失败？"
        )
        self.assertIsNone(projection)

    def test_question_seed_binds_expected_action_and_skill_evidence(self) -> None:
        llm_turn = _record(
            "llm_turn",
            "llm.turn",
            {"finish_reason": "tool-calls", "model_id": "test-model"},
            component="llm",
        )
        gcc_call = _record(
            "gcc_call",
            "tool.call",
            {"tool": "bash", "input": {"command": "gcc -c migrated.c"}},
            component="tool",
        )
        response = _record(
            "response",
            "response.output",
            {"text": "迁移代码已经完成。"},
            component="result",
        )
        graph = bind_question_hypothesis(
            _graph(_request(), _catalog(), llm_turn, gcc_call, response),
            AttributionQuestion.create(self.question),
        )
        projection = graph.raw_trace["offline_question_projection"]
        seed = graph.nodes[projection["seed_ref"]]

        self.assertEqual(
            seed.data["expected_action_projection"]["observed_action"], "absent"
        )
        self.assertEqual(
            seed.data["expected_action_projection"]["exposed_skill_names"],
            ["migration-consistency"],
        )
        self.assertIn("record:skill_catalog", seed.source_refs)
        self.assertEqual(
            seed.data["expected_action_projection"]["execution_window_refs"],
            ["record:llm_turn", "record:gcc_call", "record:response"],
        )
        self.assertTrue(
            {
                "record:llm_turn",
                "record:gcc_call",
                "record:response",
            }.issubset(set(seed.source_refs))
        )

    def test_many_unrelated_skill_calls_do_not_evict_final_execution_evidence(self) -> None:
        unrelated_calls = [
            _record(
                "other_skill_{0}".format(index),
                "skill.load",
                {"skill_name": "other-{0}".format(index)},
                component="skill",
            )
            for index in range(40)
        ]
        response = _record(
            "final_response",
            "response.output",
            {"text": "任务结束，但未执行迁移一致性校验。"},
            component="result",
        )

        graph = bind_question_hypothesis(
            _graph(_request(), _catalog(), *unrelated_calls, response),
            AttributionQuestion.create(self.question),
        )
        seed = graph.nodes[graph.raw_trace["offline_question_projection"]["seed_ref"]]

        self.assertIn("record:final_response", seed.source_refs)
        self.assertLessEqual(len(seed.source_refs), 32)

    def test_premise_judge_receives_expected_skill_lifecycle_and_grounded_refs(self) -> None:
        question = AttributionQuestion.create(self.question)
        graph = bind_question_hypothesis(
            _graph(_request(), _catalog_with_unrelated_exposed_skill()),
            question,
        )

        class Transport:
            prompt = ""

            def create_message_text(self, *, system, messages, max_tokens):
                self.prompt = messages[0]["content"]
                return json.dumps(
                    {
                        "status": "supported",
                        "expected_behavior": "Invoke migration-consistency.",
                        "alleged_actual_behavior": "No matching Skill invocation occurred.",
                        "reason": "The Skill was exposed but never invoked.",
                        "evidence_refs": [
                            "record:user_request",
                            "record:skill_catalog",
                        ],
                        "missing_evidence": [],
                        "confidence": 0.9,
                        "deviation_type": "action_omission",
                        "contract_source_refs": ["record:skill_catalog"],
                        "actual_sequence_refs": ["record:user_request"],
                        "first_deviation_ref": "record:skill_catalog",
                        "upstream_influence_refs": ["record:user_request"],
                        "localization_reason": "Invocation is absent after exposure.",
                    }
                )

        transport = Transport()
        assessment = service.assess_question_premise(
            transport,
            graph,
            question,
            seed_ref=graph.default_start_refs()[0],
        )

        self.assertEqual(assessment["status"], "supported")
        prompt = json.loads(transport.prompt)
        lifecycle = prompt["reported_deviation"]["expected_action_projection"][
            "expected_skill_lifecycle"
        ]
        self.assertEqual(lifecycle[0]["name"], "migration-consistency")
        self.assertEqual(lifecycle[0]["first_absent_stage"], "invocation")
        self.assertEqual(
            set(assessment["evidence_refs"]),
            {"record:user_request", "record:skill_catalog"},
        )
        instructions = " ".join(prompt["instructions"])
        self.assertIn("first_absent_stage", instructions)
        self.assertIn("successful exposure", instructions)

    def test_unknown_skill_premise_terminates_without_generic_candidates(self) -> None:
        report = question_premise_inconclusive_report(
            case_id="skill-omission-case",
            objective=self.question,
            seed_ref="record:offline_question",
            assessment={
                "status": "unknown",
                "reason": "The catalog exposure fact is missing.",
                "evidence_refs": ["record:user_request"],
                "missing_evidence": ["skill_catalog_exposure_missing"],
                "confidence": 0.0,
            },
        )

        self.assertEqual(report["analysis_outcome"], "inconclusive")
        self.assertEqual(report["taint_paths"], [])
        self.assertEqual(report["confirmed_roots"], [])
        self.assertEqual(
            report["seed_results"][0]["blocking_reasons"],
            ["question_premise_unknown"],
        )
        self.assertEqual(
            report["metadata"]["termination_reason"],
            "question_premise_unknown",
        )

    def test_final_question_report_exposes_the_expected_action_projection(self) -> None:
        binding = AttributionQuestion.create(self.question)
        graph = bind_question_hypothesis(
            _graph(_request(), _catalog()),
            binding,
        )
        seed_ref = graph.default_start_refs()[0]
        report = question_premise_inconclusive_report(
            case_id=graph.case_id,
            objective=self.question,
            seed_ref=seed_ref,
            assessment={
                "status": "unknown",
                "reason": "Synthetic test.",
                "evidence_refs": [],
                "missing_evidence": ["judge_unavailable"],
                "confidence": 0.0,
            },
        )

        payload = service.question_bound_output_payload(
            report,
            graph,
            binding=binding,
            starts=(seed_ref,),
        )

        projection = payload["analysis_question"]["expected_action_projection"]
        self.assertEqual(projection["action_kind"], "skill.invoke")
        self.assertEqual(
            projection["expected_skill_names"], ["migration-consistency"]
        )


if __name__ == "__main__":
    unittest.main()
