from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from trace_attribution import service
from trace_attribution import cli
from trace_attribution import defect_explanation
from trace_attribution.graph import TraceGraph
from trace_attribution.request import AttributionResult


def behavioral_deviation_graph() -> TraceGraph:
    return TraceGraph.from_trace(
        {
            "case_id": "behavioral-deviation",
            "records": [
                {
                    "record_id": "plan",
                    "component": "tool",
                    "event_type": "tool.result",
                    "data": {
                        "tool_name": "build_plan",
                        "observed_by_model": True,
                        "output": {
                            "preview": '{"next_tool":"build_execute"}'
                        },
                    },
                },
                {
                    "record_id": "root",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:plan"],
                    "data": {
                        "decision_type": "reasoning_block",
                        "rationale": (
                            "The plan requires build_execute next. "
                            "But actually, the edit should come first."
                        ),
                    },
                },
                {
                    "record_id": "edit",
                    "component": "processor",
                    "event_type": "decision",
                    "source_refs": ["record:root"],
                    "data": {
                        "decision_type": "llm_tool_call",
                        "chosen_action": "edit",
                        "rationale": {
                            "tool": "edit",
                            "recent_reasoning": (
                                "The plan requires build_execute next. "
                                "But actually, the edit should come first."
                            ),
                        },
                    },
                },
                {
                    "record_id": "execute",
                    "component": "tool",
                    "event_type": "tool.call",
                    "source_refs": ["record:edit"],
                    "data": {"tool_name": "build_execute"},
                },
                {
                    "record_id": "question",
                    "component": "case",
                    "event_type": "case.observed_defect",
                    "source_refs": ["record:edit", "record:execute"],
                    "data": {
                        "expected": "Call build_execute before editing.",
                        "actual": "The edit happened before build_execute.",
                    },
                },
            ],
        }
    )


def behavioral_deviation_report() -> dict:
    root = "record:root"
    edit = "record:edit"
    question = "record:question"
    return {
        "case_id": "behavioral-deviation",
        "analysis_outcome": "confirmed_root",
        "analysis_question": {
            "question": "Why was the required tool order ignored?",
            "premise_assessment": {
                "status": "supported",
                "expected_behavior": "Call build_execute before editing.",
                "alleged_actual_behavior": "The edit happened before build_execute.",
                "deviation_type": "action_order",
                "contract_source_refs": ["record:plan"],
                "actual_sequence_refs": [
                    "record:plan",
                    root,
                    edit,
                    "record:execute",
                    question,
                ],
                "first_deviation_ref": edit,
                "localization_reason": "The edit occurred before the required tool.",
                "evidence_refs": ["record:plan", root, edit, "record:execute"],
                "missing_evidence": [],
                "confidence": 0.95,
            },
        },
        "confirmed_roots": [
            {
                "node_ref": root,
                "component": "processor",
                "event_type": "decision",
                "defect_type": "localized_action_order_deviation",
                "reason": "The reasoning overrode the required action order.",
                "confidence": 0.9,
                "excerpt": "But actually, the edit should come first.",
                "recursive_path": [root, edit, question],
                "evidence_refs": [root, edit],
                "defect_state": {
                    "expected": "Call build_execute before editing.",
                    "actual": "The edit happened before build_execute.",
                    "label": "localized_action_order_deviation",
                    "mechanism": "The decision replaced the explicit order.",
                },
            }
        ],
        "co_roots": [],
        "contributing_conditions": [],
        "causal_chain": [[root, edit, question]],
        "step_judgments": [
            {
                "current_node_ref": edit,
                "current_defect_status": "present",
                "candidate_introduction": False,
                "current_defect_reason": "The edit materialized the wrong order.",
                "predecessors": [
                    {
                        "ref": root,
                        "relation": "same_defect_propagation",
                        "recurse": True,
                        "reason": "The action repeats the earlier reasoning.",
                    }
                ],
            },
            {
                "current_node_ref": root,
                "current_defect_status": "present",
                "candidate_introduction": True,
                "current_defect_reason": "This is the first wrong commitment.",
                "predecessors": [
                    {
                        "ref": "record:plan",
                        "relation": "motivated_by_evidence",
                        "recurse": False,
                        "reason": "The plan was correct evidence, not a defective source.",
                    }
                ],
            },
        ],
        "supporting_evidence_refs": [
            "record:plan",
            root,
            edit,
            "record:execute",
            question,
        ],
        "unresolved_gaps": [],
        "conclusion": "The reasoning decision introduced the wrong order.",
    }


class DefectExplanationTests(unittest.TestCase):
    def test_deterministic_human_fields_do_not_invent_an_edit_order_scenario(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "wrong-algorithm",
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {
                            "rationale": "Use exhaustive search for every request."
                        },
                    }
                ],
            }
        )
        report = {
            "analysis_question": {
                "premise_assessment": {
                    "deviation_type": "semantic_change",
                    "expected_behavior": "Preserve the bounded lookup algorithm.",
                    "alleged_actual_behavior": "The decision selected exhaustive search.",
                    "first_deviation_ref": "record:decision",
                    "actual_sequence_refs": ["record:decision"],
                }
            }
        }

        fields = defect_explanation._human_step_fields(
            graph,
            report,
            ref="record:decision",
            stage="defect_introduction",
            data=graph.nodes["record:decision"].data,
            excerpt="Use exhaustive search for every request.",
        )
        rendered = json.dumps(fields, ensure_ascii=False)

        self.assertIn("bounded lookup algorithm", rendered)
        self.assertIn("exhaustive search", rendered)
        self.assertNotIn("编辑", rendered)
        self.assertNotIn("构建规划", rendered)
        self.assertNotIn("迟到", rendered)

    def test_action_order_human_fields_use_the_recorded_actions_not_edit_template(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "deploy-before-test",
                "records": [
                    {
                        "record_id": "decision",
                        "component": "processor",
                        "event_type": "decision",
                        "data": {"rationale": "Deploy now and test afterward."},
                    }
                ],
            }
        )
        report = {
            "analysis_question": {
                "premise_assessment": {
                    "deviation_type": "action_order",
                    "expected_behavior": "Run integration tests before deployment.",
                    "alleged_actual_behavior": "Deployment happened before integration tests.",
                    "first_deviation_ref": "record:decision",
                    "actual_sequence_refs": ["record:decision"],
                }
            }
        }

        fields = defect_explanation._human_step_fields(
            graph,
            report,
            ref="record:decision",
            stage="defect_introduction",
            data=graph.nodes["record:decision"].data,
            excerpt="Deploy now and test afterward.",
        )
        rendered = json.dumps(fields, ensure_ascii=False)

        self.assertIn("integration tests", rendered)
        self.assertIn("Deployment", rendered)
        self.assertNotIn("编辑", rendered)
        self.assertNotIn("构建规划", rendered)
        self.assertNotIn("指定工具", rendered)

    def test_builds_a_stepwise_defect_evolution_from_grounded_nodes(self):
        builder = getattr(service, "build_defect_evolution", None)
        self.assertIsNotNone(
            builder,
            "production must expose build_defect_evolution",
        )

        evolution = builder(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )

        self.assertEqual(evolution["schema_version"], "defect-evolution/v2")
        self.assertEqual(
            evolution["defect_subject"],
            "本应先调用 build_execute 再编辑，但实际先编辑、后调用 build_execute。",
        )
        self.assertEqual(
            evolution["expected_sequence"],
            ["调用 build_execute", "修改代码"],
        )
        self.assertEqual(
            evolution["actual_sequence"],
            ["修改代码", "调用 build_execute"],
        )
        self.assertEqual(
            evolution["first_deviation"]["node_ref"],
            "record:root",
        )
        self.assertEqual(
            [step["stage"] for step in evolution["steps"]],
            [
                "contract_established",
                "defect_introduction",
                "defect_materialization",
                "late_recovery",
                "user_observation",
            ],
        )
        self.assertEqual(
            [step["node_ref"] for step in evolution["steps"]],
            [
                "record:plan",
                "record:root",
                "record:edit",
                "record:execute",
                "record:question",
            ],
        )
        self.assertEqual(evolution["steps"][0]["defect_after"], "absent")
        self.assertEqual(evolution["steps"][1]["defect_before"], "absent")
        self.assertEqual(evolution["steps"][1]["defect_after"], "present")
        self.assertEqual(evolution["steps"][2]["defect_before"], "present")
        self.assertEqual(evolution["steps"][2]["defect_after"], "propagated")
        self.assertEqual(
            evolution["steps"][1]["input_refs"],
            ["record:plan"],
        )
        self.assertIn(
            "But actually, the edit should come first.",
            evolution["steps"][1]["evidence_excerpt"],
        )
        self.assertEqual(
            evolution["primary_cause"]["node_ref"],
            "record:root",
        )

    def test_renders_the_process_as_a_readable_markdown_report(self):
        builder = getattr(service, "build_defect_evolution", None)
        renderer = getattr(service, "render_defect_explanation_markdown", None)
        self.assertIsNotNone(builder)
        self.assertIsNotNone(
            renderer,
            "production must expose render_defect_explanation_markdown",
        )

        evolution = builder(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )
        evolution["steps"][1]["explanation"] = (
            "该节点首次用自生成假设覆盖了显式契约。"
        )
        evolution["steps"][1]["problem_explanation"] = (
            "该节点首次用自生成假设覆盖了显式契约。"
        )
        evolution["contributing_conditions"] = [
            {
                "title": "契约仅为声明性文本",
                "explanation": "Harness 没有把 next_tool 转换为强制 obligation。",
                "evidence_refs": ["record:plan"],
            }
        ]
        evolution["ruled_out"] = [
            {
                "component": "tool_execution",
                "explanation": "工具保持可用并在后续成功执行。",
                "evidence_refs": ["record:execute"],
            }
        ]
        evolution["recommendations"] = [
            "把 required next action 提升为可跟踪 obligation。"
        ]
        markdown = renderer(
            behavioral_deviation_report(),
            evolution,
        )

        self.assertIn("# 缺陷根因分析", markdown)
        self.assertIn("## 一句话结论", markdown)
        self.assertIn("## 期望动作与实际动作", markdown)
        self.assertIn("本次追踪的偏差", markdown)
        self.assertIn("期望顺序：先调用 `build_execute`，再修改代码", markdown)
        self.assertIn("实际顺序：先修改代码，再调用 `build_execute`", markdown)
        self.assertIn("## 缺陷是怎样一步步产生的", markdown)
        self.assertIn("### 第 2 步：Agent 首次改变了执行顺序", markdown)
        self.assertIn("谁在做什么", markdown)
        self.assertIn("当时掌握的信息", markdown)
        self.assertIn("为什么这一步有问题", markdown)
        self.assertIn("对下一步的影响", markdown)
        self.assertIn("该节点首次用自生成假设覆盖了显式契约。", markdown)
        narrative, appendix = markdown.split("## 技术证据附录", maxsplit=1)
        self.assertIn("Agent 决策层决定先修改代码", narrative)
        self.assertIn("如果 Agent 在首次决策时遵循期望顺序", narrative)
        self.assertNotIn("The reasoning overrode", narrative)
        self.assertNotIn("record:", narrative)
        self.assertNotIn("tool.result", narrative)
        self.assertNotIn("`absent`", narrative)
        self.assertNotIn("`present`", narrative)
        self.assertIn("`record:root`", appendix)
        self.assertIn("`tool.result`", appendix)
        self.assertIn("`absent` -> `present`", appendix)
        self.assertIn("## 反事实", markdown)
        self.assertIn("## 系统性诱因", markdown)
        self.assertIn("Harness 没有把 next_tool 转换为强制 obligation。", markdown)
        self.assertIn("工具保持可用并在后续成功执行。", markdown)
        self.assertIn("## 改进建议", markdown)

    def test_each_step_explains_the_actor_action_deviation_and_next_effect(self):
        evolution = service.build_defect_evolution(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )

        root_step = next(
            step
            for step in evolution["steps"]
            if step["node_ref"] == "record:root"
        )

        self.assertEqual(root_step["actor"], "Agent 决策层")
        self.assertIn("先修改代码", root_step["action_description"])
        self.assertIn("build_execute", root_step["knowledge_at_time"])
        self.assertIn("首次", root_step["problem_explanation"])
        self.assertIn("edit", root_step["effect_on_next"])
        self.assertEqual(
            root_step["human_defect_state"],
            "执行要求原本正确，但在本步骤首次产生了顺序偏差。",
        )

    def test_order_comparison_excludes_actions_outside_the_contract_deviation(self):
        graph = TraceGraph.from_trace(
            {
                "case_id": "wide-action-window",
                "records": [
                    {
                        "record_id": "plan-call",
                        "component": "tool",
                        "event_type": "tool.call",
                        "data": {"tool_name": "syntheticBuildChain_yocto_build_plan"},
                    },
                    {
                        "record_id": "plan",
                        "component": "tool",
                        "event_type": "tool.result",
                        "data": {
                            "tool_name": "syntheticBuildChain_yocto_build_plan",
                            "output": {"preview": '{"next_tool":"yocto_build_execute"}'},
                        },
                    },
                    {
                        "record_id": "read",
                        "component": "tool",
                        "event_type": "tool.call",
                        "data": {"tool_name": "read"},
                    },
                    {
                        "record_id": "root",
                        "component": "processor",
                        "event_type": "decision",
                        "source_refs": ["record:plan"],
                        "data": {"rationale": "Let me edit first."},
                    },
                    {
                        "record_id": "edit",
                        "component": "processor",
                        "event_type": "decision",
                        "source_refs": ["record:root"],
                        "data": {
                            "decision_type": "llm_tool_call",
                            "chosen_action": "edit",
                        },
                    },
                    {
                        "record_id": "execute",
                        "component": "tool",
                        "event_type": "tool.call",
                        "source_refs": ["record:edit"],
                        "data": {
                            "tool_name": "syntheticBuildChain_yocto_build_execute"
                        },
                    },
                    {
                        "record_id": "question",
                        "component": "case",
                        "event_type": "case.observed_defect",
                        "source_refs": ["record:execute"],
                        "data": {"actual": "Editing happened before Yocto execution."},
                    },
                ],
            }
        )
        report = behavioral_deviation_report()
        premise = report["analysis_question"]["premise_assessment"]
        premise["contract_source_refs"] = ["record:plan"]
        premise["first_deviation_ref"] = "record:edit"
        premise["actual_sequence_refs"] = [
            "record:plan-call",
            "record:plan",
            "record:read",
            "record:root",
            "record:edit",
            "record:execute",
            "record:question",
        ]
        report["confirmed_roots"][0]["recursive_path"] = [
            "record:root",
            "record:edit",
            "record:question",
        ]
        report["causal_chain"] = [
            ["record:root", "record:edit", "record:question"]
        ]

        evolution = service.build_defect_evolution(report, graph)

        self.assertEqual(
            evolution["expected_sequence"],
            ["调用 yocto_build_execute", "修改代码"],
        )
        self.assertEqual(
            evolution["actual_sequence"],
            ["修改代码", "调用 yocto_build_execute"],
        )
        self.assertIn(
            "先修改代码",
            evolution["first_deviation"]["action"],
        )
        self.assertNotIn("read", evolution["defect_subject"])
        self.assertNotIn("build_plan", evolution["defect_subject"])

    def test_derives_a_stable_explanation_path_from_the_json_report_path(self):
        path_builder = getattr(service, "explanation_output_path", None)
        self.assertIsNotNone(
            path_builder,
            "production must expose explanation_output_path",
        )

        self.assertEqual(
            path_builder(Path("/tmp/case.attribution.json")),
            Path("/tmp/case.attribution.explanation.md"),
        )

    def test_llm_enriches_prose_without_changing_the_grounded_process(self):
        synthesizer = getattr(service, "synthesize_defect_evolution", None)
        self.assertIsNotNone(
            synthesizer,
            "production must expose synthesize_defect_evolution",
        )
        evolution = service.build_defect_evolution(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )
        evolution["ruled_out"].append(
            {
                "component": "context_management",
                "reason": "The context envelope preserved the contract.",
                "evidence_refs": ["record:context-not-on-main-path"],
            }
        )
        refs = [step["node_ref"] for step in evolution["steps"]]
        transport = RecordingTransport(
            {
                "summary": "The Agent replaced an explicit tool-order contract with its own workflow assumption.",
                "primary_cause_explanation": "The first defective semantic transformation occurred in record:root.",
                "steps": [
                    {
                        "node_ref": ref,
                        "explanation": "Grounded explanation for {0}.".format(ref),
                        "input_semantics": "Grounded input for {0}.".format(ref),
                        "transformation": "Grounded transformation for {0}.".format(ref),
                        "output_semantics": "Grounded output for {0}.".format(ref),
                        "causal_reason": "Grounded causal role for {0}.".format(ref),
                        "human_title": "Human title for {0}.".format(ref),
                        "actor": "Human actor for {0}.".format(ref),
                        "action_description": "Human action for {0}.".format(ref),
                        "knowledge_at_time": "Human knowledge for {0}.".format(ref),
                        "problem_explanation": "Human problem for {0}.".format(ref),
                        "effect_on_next": "Human effect for {0}.".format(ref),
                    }
                    for ref in refs
                ],
                "contributing_conditions": [
                    {
                        "title": "Declarative contract",
                        "explanation": "The next action was declared but not enforced.",
                        "evidence_refs": ["record:plan"],
                    }
                ],
                "ruled_out": [
                    {
                        "component": "tool_execution",
                        "explanation": "The required tool remained available and was later called.",
                        "evidence_refs": ["record:context-not-on-main-path"],
                    }
                ],
                "counterfactual": "Calling build_execute at record:root would avoid the order deviation.",
                "recommendations": [
                    "Promote required next actions to tracked obligations."
                ],
            }
        )

        enriched = synthesizer(
            evolution,
            transport=transport,
        )

        self.assertEqual(
            [step["node_ref"] for step in enriched["steps"]],
            refs,
        )
        self.assertEqual(
            [step["stage"] for step in enriched["steps"]],
            [step["stage"] for step in evolution["steps"]],
        )
        self.assertEqual(
            [step["defect_after"] for step in enriched["steps"]],
            [step["defect_after"] for step in evolution["steps"]],
        )
        self.assertEqual(
            enriched["steps"][1]["explanation"],
            "Grounded explanation for record:root.",
        )
        self.assertEqual(
            enriched["steps"][1]["actor"],
            "Human actor for record:root.",
        )
        self.assertEqual(
            enriched["steps"][1]["problem_explanation"],
            "Human problem for record:root.",
        )
        self.assertEqual(enriched["generation"]["mode"], "llm_grounded_synthesis")
        self.assertIn("record:root", transport.last_prompt)

    def test_llm_cannot_insert_an_unsupported_process_node(self):
        synthesizer = getattr(service, "synthesize_defect_evolution", None)
        self.assertIsNotNone(synthesizer)
        evolution = service.build_defect_evolution(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )
        transport = RecordingTransport(
            {
                "summary": "Invented narrative.",
                "primary_cause_explanation": "Invented root.",
                "steps": [
                    {
                        "node_ref": "record:not-in-trace",
                        "explanation": "Invented.",
                        "input_semantics": "Invented.",
                        "transformation": "Invented.",
                        "output_semantics": "Invented.",
                        "causal_reason": "Invented.",
                    }
                ],
                "contributing_conditions": [],
                "ruled_out": [],
                "counterfactual": "Invented.",
                "recommendations": [],
            }
        )

        enriched = synthesizer(evolution, transport=transport)

        self.assertEqual(enriched["generation"]["mode"], "deterministic_fallback")
        self.assertEqual(
            [step["node_ref"] for step in enriched["steps"]],
            [step["node_ref"] for step in evolution["steps"]],
        )
        self.assertIn("unsupported", enriched["generation"]["fallback_reason"])

    def test_writes_a_markdown_explanation_next_to_the_json_report(self):
        publisher = getattr(service, "publish_defect_explanation", None)
        self.assertIsNotNone(
            publisher,
            "production must expose publish_defect_explanation",
        )
        evolution = service.build_defect_evolution(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            report_path = Path(tempdir) / "case.attribution.json"
            explanation_path = publisher(
                report_path,
                behavioral_deviation_report(),
                evolution,
            )

            self.assertEqual(
                explanation_path,
                Path(tempdir) / "case.attribution.explanation.md",
            )
            text = explanation_path.read_text(encoding="utf-8")
            self.assertIn("## 缺陷是怎样一步步产生的", text)
            self.assertIn("`record:root`", text)

    def test_enriches_the_published_json_with_the_stepwise_evolution(self):
        enricher = getattr(service, "enrich_attribution_explanation", None)
        self.assertIsNotNone(
            enricher,
            "production must expose enrich_attribution_explanation",
        )

        payload = enricher(
            behavioral_deviation_report(),
            behavioral_deviation_graph(),
            transport=None,
        )

        self.assertIn("defect_evolution", payload)
        self.assertEqual(
            payload["defect_evolution"]["primary_cause"]["node_ref"],
            "record:root",
        )
        self.assertEqual(
            payload["defect_evolution"]["generation"]["mode"],
            "deterministic_causal_ir_projection",
        )

    def test_cli_prints_both_the_json_and_markdown_output_paths(self):
        output = Path("/tmp/case.attribution.json")
        result = AttributionResult(
            output_path=output,
            lineage_path=Path("/tmp/case.message-lineage.json"),
            payload={"analysis_outcome": "confirmed_root"},
        )
        stdout = io.StringIO()
        with mock.patch(
            "trace_attribution.cli.parse_args",
            return_value=mock.Mock(
                trace="/tmp/trace.json",
                benchmark_bundle="",
                out=str(output),
                review="",
                evaluation=[],
                start_ref=[],
                question="Why?",
                objective="",
                engine="recursive-agentic",
                fusion_mode="retrieval-global",
                analysis_perspective="Find the best-supported causal explanation.",
                max_depth=20,
                max_nodes=48,
                max_frontier_items=96,
                max_hypotheses=24,
                max_investigation_rounds=12,
                max_artifact_bytes=1_048_576,
                max_judge_requests=128,
                lineage_out="",
                judge_cache="",
                checkpoint_dir="",
                model="test",
                api_key_env="ANTHROPIC_API_KEY",
                base_url="",
                base_url_env="ANTHROPIC_BASE_URL",
                judge_timeout_sec=3600.0,
                judge_max_tokens=8192,
                thinking_mode="disabled",
                provider_error_threshold=3,
            ),
        ), mock.patch("trace_attribution.cli.analyze", return_value=result), redirect_stdout(stdout):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "/tmp/case.attribution.json",
                "/tmp/case.attribution.explanation.md",
            ],
        )

    def test_preserves_the_offline_question_as_the_final_observation_step(self):
        report = behavioral_deviation_report()
        offline_ref = "record:offline_question_123"
        report["confirmed_roots"][0]["recursive_path"] = [
            "record:root",
            "record:edit",
            offline_ref,
        ]
        report["causal_chain"] = [["record:root", "record:edit", offline_ref]]
        report["analysis_question"]["premise_assessment"]["actual_sequence_refs"] = [
            "record:plan",
            "record:edit",
            "record:root",
            "record:execute",
            offline_ref,
        ]

        evolution = service.build_defect_evolution(
            report,
            behavioral_deviation_graph(),
        )

        self.assertEqual(evolution["steps"][-1]["stage"], "user_observation")
        self.assertEqual(evolution["steps"][-1]["node_ref"], offline_ref)
        self.assertEqual(
            evolution["steps"][-2]["node_ref"],
            "record:execute",
        )

    def test_expands_recorded_message_transforms_before_the_root_decision(self):
        graph = behavioral_deviation_graph()
        trace = dict(graph.raw_trace)
        records = list(trace["records"])
        records.insert(
            1,
            {
                "record_id": "request-ready",
                "component": "context",
                "event_type": "context.transform",
                "source_refs": ["record:plan"],
                "data": {
                    "stage": "llm_request_ready",
                    "transforms": [
                        {"name": "resolveTools", "tool_count": 14}
                    ],
                },
            },
        )
        for record in records:
            if record.get("record_id") == "root":
                record["data"] = dict(record["data"])
                record["data"]["message_transforms"] = [
                    {
                        "node_ref": "record:request-ready",
                        "event_type": "context.transform",
                        "stage": "llm_request_ready",
                        "transforms": [
                            {"name": "resolveTools", "tool_count": 14}
                        ],
                    }
                ]
        trace["records"] = records

        evolution = service.build_defect_evolution(
            behavioral_deviation_report(),
            TraceGraph.from_trace(trace),
        )

        root_index = next(
            index
            for index, step in enumerate(evolution["steps"])
            if step["node_ref"] == "record:root"
        )
        transform = evolution["steps"][root_index - 1]
        self.assertEqual(transform["stage"], "context_delivery")
        self.assertEqual(transform["node_ref"], "record:request-ready")
        self.assertEqual(transform["defect_after"], "absent")
        self.assertIn("resolveTools", transform["evidence_excerpt"])


class RecordingTransport:
    def __init__(self, payload: dict):
        self.payload = payload
        self.last_prompt = ""
        self.model = "test-model"
        self.max_tokens = 4096
        self.thinking_config = None
        self.cache = None

    def create_message_text(self, *, system, messages, max_tokens):
        del system, max_tokens
        self.last_prompt = messages[0]["content"]
        return json.dumps(self.payload)


if __name__ == "__main__":
    unittest.main()
