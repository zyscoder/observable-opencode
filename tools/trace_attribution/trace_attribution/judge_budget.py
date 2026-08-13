"""Deterministic context budgeting at the physical Judge boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence


DEFAULT_JUDGE_CONTEXT_WINDOW_TOKENS = 200_000
DEFAULT_JUDGE_CONTEXT_SAFETY_MARGIN_TOKENS = 8_192
TOKEN_ESTIMATOR_ID = "utf8-byte-upper-bound-plus-message-overhead/v2"


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("{0} must be a positive integer".format(name))
    return value


def _prompt_bytes(
    *, system: str, messages: Sequence[Mapping[str, Any]]
) -> tuple[int, int]:
    if not isinstance(system, str):
        raise TypeError("Judge system prompt must be a string")
    if not isinstance(messages, (list, tuple)):
        raise TypeError("Judge messages must be an array")
    character_count = len(system)
    byte_count = len(system.encode("utf-8"))
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("Judge message must be an object")
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            raise TypeError("Judge message role and content must be strings")
        character_count += len(role) + len(content)
        byte_count += len(role.encode("utf-8")) + len(content.encode("utf-8"))
    return character_count, byte_count


def estimate_prompt_tokens(
    *, system: str, messages: Sequence[Mapping[str, Any]]
) -> int:
    """Conservatively estimate input tokens without a provider tokenizer."""
    _, byte_count = _prompt_bytes(system=system, messages=messages)
    framing_tokens = 16 + (8 * len(messages))
    return max(1, byte_count + framing_tokens)


@dataclass(frozen=True)
class PromptBudgetMeasurement:
    estimator: str
    character_count: int
    utf8_byte_count: int
    estimated_input_tokens: int
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    max_input_tokens: int
    fits: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "judge-prompt-budget-measurement/v1",
            "estimator": self.estimator,
            "character_count": self.character_count,
            "utf8_byte_count": self.utf8_byte_count,
            "estimated_input_tokens": self.estimated_input_tokens,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "max_input_tokens": self.max_input_tokens,
            "fits": self.fits,
        }


@dataclass(frozen=True)
class JudgeContextBudget:
    context_window_tokens: int = DEFAULT_JUDGE_CONTEXT_WINDOW_TOKENS
    max_output_tokens: int = 4096
    safety_margin_tokens: int = DEFAULT_JUDGE_CONTEXT_SAFETY_MARGIN_TOKENS

    def __post_init__(self) -> None:
        for name in (
            "context_window_tokens",
            "max_output_tokens",
            "safety_margin_tokens",
        ):
            _positive_integer(getattr(self, name), name)
        if self.max_input_tokens <= 0:
            raise ValueError(
                "Judge effective input budget must be positive after output and safety reserves"
            )

    @property
    def max_input_tokens(self) -> int:
        return (
            self.context_window_tokens
            - self.max_output_tokens
            - self.safety_margin_tokens
        )

    def with_max_output_tokens(self, max_output_tokens: int) -> "JudgeContextBudget":
        return replace(
            self,
            max_output_tokens=_positive_integer(
                max_output_tokens, "max_output_tokens"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "judge-context-budget/v1",
            "estimator": TOKEN_ESTIMATOR_ID,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "max_input_tokens": self.max_input_tokens,
        }

    def measure(
        self,
        *,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        max_output_tokens: int | None = None,
    ) -> PromptBudgetMeasurement:
        budget = (
            self
            if max_output_tokens is None
            else self.with_max_output_tokens(max_output_tokens)
        )
        character_count, byte_count = _prompt_bytes(
            system=system, messages=messages
        )
        estimated = estimate_prompt_tokens(system=system, messages=messages)
        return PromptBudgetMeasurement(
            estimator=TOKEN_ESTIMATOR_ID,
            character_count=character_count,
            utf8_byte_count=byte_count,
            estimated_input_tokens=estimated,
            context_window_tokens=budget.context_window_tokens,
            max_output_tokens=budget.max_output_tokens,
            safety_margin_tokens=budget.safety_margin_tokens,
            max_input_tokens=budget.max_input_tokens,
            fits=estimated <= budget.max_input_tokens,
        )


class JudgeContextBudgetExceeded(RuntimeError):
    """A local preflight failure that consumes no physical request."""

    def __init__(self, measurement: PromptBudgetMeasurement) -> None:
        self.measurement = measurement
        super().__init__(
            "Judge prompt exceeds local context budget: estimated {0} input "
            "tokens, maximum {1}".format(
                measurement.estimated_input_tokens,
                measurement.max_input_tokens,
            )
        )
