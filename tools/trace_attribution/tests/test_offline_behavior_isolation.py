from __future__ import annotations

import copy
import hashlib
import unittest

from trace_attribution.graph import TraceGraph
from trace_attribution.models import stable_json
from trace_attribution.recursive_analyzer import AgenticRecursiveAnalyzer
from tools.trace_attribution.tests.test_recursive_analyzer import (
    FusionScriptedJudge,
    observed_trace,
)


AGENT_VISIBLE_EVENT_TYPES = frozenset(
    {
        "message.input",
        "provider.request",
        "tool.call",
        "tool.result",
        "message.output",
    }
)


def _hash(value) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _agent_visible_projection(trace: dict) -> dict:
    records = [
        copy.deepcopy(record)
        for record in trace.get("records") or []
        if record.get("event_type") in AGENT_VISIBLE_EVENT_TYPES
    ]
    return {
        "messages": [
            record
            for record in records
            if record.get("event_type") in {"message.input", "message.output"}
        ],
        "provider_requests": [
            record
            for record in records
            if record.get("event_type") == "provider.request"
        ],
        "tool_io": [
            record
            for record in records
            if record.get("event_type") in {"tool.call", "tool.result"}
        ],
    }


class OfflineBehaviorIsolationTest(unittest.TestCase):
    def test_attribution_does_not_mutate_agent_visible_trace_surfaces(self):
        trace = observed_trace(branching=True)
        trace["records"].extend(
            [
                {
                    "record_id": "provider_request",
                    "component": "llm",
                    "event_type": "provider.request",
                    "title": "Provider request",
                    "status": "completed",
                    "timestamp": "2026-07-30T00:00:01Z",
                    "data": {"messages": [{"role": "user", "content": "request"}]},
                    "source_refs": ["record:message"],
                },
                {
                    "record_id": "tool_call",
                    "component": "tool",
                    "event_type": "tool.call",
                    "title": "Read source",
                    "status": "completed",
                    "timestamp": "2026-07-30T00:00:02Z",
                    "data": {"name": "read", "input": {"path": "src/model.py"}},
                    "source_refs": ["record:provider_request"],
                },
                {
                    "record_id": "tool_result",
                    "component": "tool",
                    "event_type": "tool.result",
                    "title": "Read source result",
                    "status": "completed",
                    "timestamp": "2026-07-30T00:00:03Z",
                    "data": {"output": "class Model: pass"},
                    "source_refs": ["record:tool_call"],
                },
                {
                    "record_id": "message_output",
                    "component": "agent",
                    "event_type": "message.output",
                    "title": "Final response",
                    "status": "completed",
                    "timestamp": "2026-07-30T00:00:04Z",
                    "data": {"content": "Implemented the requested change."},
                    "source_refs": ["record:tool_result"],
                },
            ]
        )
        original_trace = copy.deepcopy(trace)
        original_trace_hash = _hash(trace)
        original_visible_hashes = {
            key: _hash(value)
            for key, value in _agent_visible_projection(trace).items()
        }
        graph = TraceGraph.from_trace(trace)
        judge = FusionScriptedJudge(global_outcome="no_defect")

        AgenticRecursiveAnalyzer(
            judge=judge,
            fusion_mode="retrieval-global",
        ).analyze(
            graph,
            start_refs=["record:observed_defect"],
            objective="Find why the implementation omitted the method.",
            analysis_perspective="Improve Agent repository reasoning.",
        )

        self.assertEqual(trace, original_trace)
        self.assertEqual(_hash(trace), original_trace_hash)
        self.assertEqual(_hash(graph.raw_trace), original_trace_hash)
        self.assertEqual(
            {
                key: _hash(value)
                for key, value in _agent_visible_projection(trace).items()
            },
            original_visible_hashes,
        )
        self.assertTrue(judge.global_requests)
        obligations = judge.global_requests[0].restoration_obligations
        self.assertTrue(obligations)
        self.assertTrue(
            all(
                obligation.visibility == "offline_judge_only"
                for obligation in obligations
            )
        )
        self.assertNotIn("restoration-obligation", stable_json(trace))


if __name__ == "__main__":
    unittest.main()
