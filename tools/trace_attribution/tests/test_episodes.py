import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from trace_attribution.episodes import CausalEpisodeIndex
from trace_attribution.graph import TraceGraph
from trace_attribution.models import NodeJudgment


def episode_trace():
    return {
        "manifest": {"case_id": "episode-case"},
        "records": [
            {
                "record_id": "reasoning",
                "component": "processor",
                "event_type": "decision",
                "data": {
                    "decision_type": "reasoning_block",
                    "rationale": "Implement the parser with a repeated keyword.",
                    "metadata": {"sessionID": "ses_1", "messageID": "msg_1"},
                },
            },
            {
                "record_id": "tool_execute",
                "component": "processor",
                "event_type": "decision",
                "span_id": "span_edit",
                "data": {
                    "decision_type": "tool_execute",
                    "metadata": {
                        "sessionID": "ses_1",
                        "messageID": "msg_1",
                        "callID": "call_edit",
                    },
                },
            },
            {
                "record_id": "llm_tool_call",
                "component": "processor",
                "event_type": "decision",
                "data": {
                    "decision_type": "llm_tool_call",
                    "metadata": {
                        "sessionID": "ses_1",
                        "messageID": "msg_1",
                        "callID": "call_edit",
                    },
                },
            },
            {
                "record_id": "tool_call",
                "component": "tool",
                "event_type": "tool.call",
                "data": {"call_id": "call_edit"},
            },
            {
                "record_id": "change",
                "component": "tool",
                "event_type": "change",
                "span_id": "span_edit",
                "data": {"change_id": "chg_1", "files": ["src/parser.py"]},
            },
            {
                "record_id": "tool_result",
                "component": "tool",
                "event_type": "tool.result",
                "source_refs": ["tool_call:call_edit"],
                "data": {"call_id": "call_edit"},
            },
            {
                "record_id": "temporal_neighbor",
                "component": "processor",
                "event_type": "decision",
                "timestamp": "2026-07-14T00:00:00.001Z",
                "data": {"decision_type": "reasoning_block", "rationale": "Unrelated thought."},
            },
        ],
    }


def trace_with_aggregated_observation_sources():
    trace = episode_trace()
    trace["records"].extend(
        [
            {
                "record_id": "second_action",
                "component": "processor",
                "event_type": "decision",
                "data": {
                    "decision_type": "llm_tool_call",
                    "metadata": {"callID": "call_second"},
                },
            },
            {
                "record_id": "second_result",
                "component": "tool",
                "event_type": "tool.result",
                "data": {"call_id": "call_second"},
            },
            {
                "record_id": "aggregate_observation",
                "component": "tool",
                "event_type": "execution.observation",
                "source_refs": [
                    "tool_result:call_edit",
                    "tool_result:call_second",
                ],
                "data": {"summary": "Results available to a later turn."},
            },
        ]
    )
    return trace


class CausalEpisodeIndexTest(unittest.TestCase):
    def test_groups_only_confirmed_action_identity_edges(self):
        graph = TraceGraph.from_trace(episode_trace())

        episodes = CausalEpisodeIndex.from_graph(graph)

        expected_members = {
            "record:reasoning",
            "record:tool_execute",
            "record:llm_tool_call",
            "record:tool_call",
            "record:change",
            "record:tool_result",
        }
        self.assertEqual(set(episodes.episode_for("record:reasoning").member_refs), expected_members)
        self.assertNotEqual(
            episodes.episode_for("record:reasoning").episode_id,
            episodes.episode_for("record:temporal_neighbor").episode_id,
        )

    def test_prefers_concrete_authored_action_over_generic_reasoning(self):
        graph = TraceGraph.from_trace(episode_trace())
        episodes = CausalEpisodeIndex.from_graph(graph)
        judgments = {
            "record:reasoning": NodeJudgment(
                node_ref="record:reasoning",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="parser_contract",
                defect_reason="The reasoning first chooses the wrong parser contract.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
            ),
            "record:llm_tool_call": NodeJudgment(
                node_ref="record:llm_tool_call",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="parser_contract",
                defect_reason="The action materializes the wrong parser contract.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
            ),
            "record:change": NodeJudgment(
                node_ref="record:change",
                component="tool",
                event_type="change",
                has_defect=True,
                defect_status="present",
                defect_type="parser_contract",
                defect_reason="The change contains the wrong parser contract.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
            ),
        }

        representative = episodes.representative(
            ["record:llm_tool_call", "record:change", "record:reasoning"],
            judgments,
        )

        self.assertEqual(representative, "record:llm_tool_call")

    def test_aggregated_observation_provenance_does_not_bridge_tool_episodes(self):
        graph = TraceGraph.from_trace(trace_with_aggregated_observation_sources())

        episodes = CausalEpisodeIndex.from_graph(graph)

        self.assertNotEqual(
            episodes.episode_for("record:tool_result").episode_id,
            episodes.episode_for("record:second_result").episode_id,
        )

    def test_hydrates_externalized_call_identity_before_grouping(self):
        trace = episode_trace()
        artifact_content = json.dumps({"callID": "call_edit", "messageID": "msg_1"})
        tool_execute = next(item for item in trace["records"] if item["record_id"] == "tool_execute")
        tool_execute["data"]["metadata"] = {
            "artifact_id": "artifact_tool_metadata",
            "payload_ref": "artifact_tool_metadata",
        }
        trace["artifacts"] = [
            {
                "artifact_id": "artifact_tool_metadata",
                "kind": "json",
                "label": "decision.metadata",
                "path": "artifacts/tool-metadata.json",
                "hash": hashlib.sha256(artifact_content.encode("utf-8")).hexdigest()[:16],
            }
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            (root / "artifacts" / "tool-metadata.json").write_text(artifact_content, encoding="utf-8")
            trace_file = root / "trace.json"
            trace_file.write_text(json.dumps(trace), encoding="utf-8")
            graph = TraceGraph.from_file(trace_file)

            episodes = CausalEpisodeIndex.from_graph(graph)

        self.assertEqual(
            episodes.episode_for("record:tool_execute").episode_id,
            episodes.episode_for("record:llm_tool_call").episode_id,
        )

    def test_skips_earlier_nondefective_member_when_selecting_representative(self):
        graph = TraceGraph.from_trace(episode_trace())
        episodes = CausalEpisodeIndex.from_graph(graph)
        judgments = {
            "record:reasoning": NodeJudgment(
                node_ref="record:reasoning",
                component="processor",
                event_type="decision",
                has_defect=False,
                defect_status="absent",
                defect_reason="Planning to test is not defective.",
                causal_role="non_defective",
            ),
            "record:llm_tool_call": NodeJudgment(
                node_ref="record:llm_tool_call",
                component="processor",
                event_type="decision",
                has_defect=True,
                defect_status="present",
                defect_type="bad_test_contract",
                defect_reason="The authored test encodes the wrong input contract.",
                causal_role="defect_introduction",
                branch_relation="same_defect",
                is_root_cause=True,
            ),
        }

        representative = episodes.representative(
            ["record:reasoning", "record:llm_tool_call"],
            judgments,
        )

        self.assertEqual(representative, "record:llm_tool_call")


if __name__ == "__main__":
    unittest.main()
