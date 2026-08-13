from __future__ import annotations

import types
import unittest

from trace_attribution.claude import ClaudeJudgeClient
from trace_attribution.errors import TransportCallError
from trace_attribution.judge_budget import (
    JudgeContextBudget,
    JudgeContextBudgetExceeded,
    estimate_prompt_tokens,
)


class JudgeContextBudgetTest(unittest.TestCase):
    def test_budget_rejects_non_positive_effective_input_capacity(self) -> None:
        with self.assertRaisesRegex(ValueError, "effective input budget"):
            JudgeContextBudget(
                context_window_tokens=10_000,
                max_output_tokens=8_000,
                safety_margin_tokens=2_000,
            )

    def test_measurement_is_deterministic_and_exposes_budget_inputs(self) -> None:
        budget = JudgeContextBudget(
            context_window_tokens=10_000,
            max_output_tokens=2_000,
            safety_margin_tokens=1_000,
        )
        messages = [{"role": "user", "content": "hello" * 100}]

        first = budget.measure(system="system", messages=messages)
        second = budget.measure(system="system", messages=messages)

        self.assertEqual(first, second)
        self.assertEqual(first.context_window_tokens, 10_000)
        self.assertEqual(first.max_output_tokens, 2_000)
        self.assertEqual(first.safety_margin_tokens, 1_000)
        self.assertEqual(first.max_input_tokens, 7_000)
        self.assertEqual(
            first.estimated_input_tokens,
            estimate_prompt_tokens(system="system", messages=messages),
        )
        self.assertTrue(first.fits)

    def test_transport_preflight_blocks_oversized_prompt_without_a_request(self) -> None:
        class Messages:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **_kwargs):
                self.calls += 1
                raise AssertionError("provider must not be called")

        messages = Messages()
        transport = object.__new__(ClaudeJudgeClient)
        transport.timeout_seconds = None
        transport.thinking_config = None
        transport.model = "test-model"
        transport.client = types.SimpleNamespace(messages=messages)
        transport.request_count = 0
        transport.provider_error_threshold = 3
        transport.consecutive_provider_errors = 0
        transport.provider_circuit_open = False
        transport.provider_circuit_reason = ""
        transport.provider_circuit_disposition = None
        transport.provider_circuit_first_request = 0
        transport.provider_circuit_first_failure_at = ""
        transport.context_budget = JudgeContextBudget(
            context_window_tokens=256,
            max_output_tokens=64,
            safety_margin_tokens=64,
        )

        with self.assertRaises(TransportCallError) as raised:
            transport.create_message_text_with_usage(
                system="system",
                messages=[{"role": "user", "content": "x" * 10_000}],
                max_tokens=64,
            )

        self.assertIsInstance(raised.exception.error, JudgeContextBudgetExceeded)
        self.assertEqual(raised.exception.physical_requests, 0)
        self.assertEqual(transport.request_count, 0)
        self.assertEqual(messages.calls, 0)
        self.assertFalse(transport.provider_circuit_open)
        self.assertFalse(raised.exception.error.measurement.fits)


if __name__ == "__main__":
    unittest.main()
