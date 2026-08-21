"""Immutable request and result contracts for offline attribution."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .claude import default_judge_timeout_seconds
from .judge_budget import (
    DEFAULT_JUDGE_CONTEXT_SAFETY_MARGIN_TOKENS,
    DEFAULT_JUDGE_CONTEXT_WINDOW_TOKENS,
    JudgeContextBudget,
)

QUESTION_SCHEMA_VERSION = "attribution-question/v1"
MAX_QUESTION_CHARS = 16_384
DEFAULT_ATTRIBUTION_OBJECTIVE = "Find the root cause of the observed bad final result."


def normalize_question(question: str) -> str:
    """Return the canonical form used for question identity and control flow."""
    if not isinstance(question, str):
        raise TypeError("question must be a string")
    return unicodedata.normalize(
        "NFC", question.replace("\r\n", "\n").replace("\r", "\n")
    ).strip()


def _question_id(normalized: str) -> str:
    payload = "{0}\n{1}".format(QUESTION_SCHEMA_VERSION, normalized)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_path(value: Path | str, field_name: str) -> Path:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("{0} must be non-empty".format(field_name))
        path = Path(value)
    elif isinstance(value, Path):
        path = value
    else:
        raise TypeError("{0} must be a path".format(field_name))
    return path.expanduser().resolve()


def _optional_path(value: Path | str | None, field_name: str) -> Path | None:
    return None if value is None else _normalized_path(value, field_name)


def _path_tuple(values: Iterable[Path | str], field_name: str) -> tuple[Path, ...]:
    if isinstance(values, (str, Path)):
        raise TypeError("{0} must be an iterable of paths".format(field_name))
    try:
        return tuple(_normalized_path(value, field_name) for value in values)
    except TypeError as exc:
        raise TypeError("{0} must be an iterable of paths".format(field_name)) from exc


def _start_ref_tuple(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("start_refs must be an iterable of strings")
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise TypeError("start_refs must be an iterable of strings") from exc
    normalized: list[str] = []
    for value in raw_values:
        if not isinstance(value, str):
            raise TypeError("start_refs must contain strings")
        ref = value.strip()
        if not ref:
            raise ValueError("start_refs must not contain empty values")
        normalized.append(ref)
    return tuple(normalized)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class AttributionQuestion:
    original: str
    normalized: str
    question_id: str

    def __post_init__(self) -> None:
        normalized = normalize_question(self.original)
        if not normalized:
            raise ValueError("question must be non-empty")
        if len(normalized) > MAX_QUESTION_CHARS:
            raise ValueError(
                "question must not exceed {0:,} characters".format(MAX_QUESTION_CHARS)
            )
        if self.normalized != normalized:
            raise ValueError("question normalized value is invalid")
        if self.question_id != _question_id(normalized):
            raise ValueError("question_id does not match the normalized question")

    @classmethod
    def create(cls, question: str) -> "AttributionQuestion":
        normalized = normalize_question(question)
        return cls(question, normalized, _question_id(normalized))


@dataclass(frozen=True)
class AttributionOptions:
    engine: str = "legacy"
    fusion_mode: str = "retrieval-global"
    analysis_perspective: str = "Find the best-supported causal explanation."
    max_depth: int | None = None
    max_nodes: int = 48
    max_frontier_items: int = 96
    max_hypotheses: int = 24
    max_investigation_rounds: int = 12
    max_artifact_bytes: int = 1_048_576
    max_judge_requests: int = 128
    lineage_output_path: Path | None = None
    judge_cache_path: Path | None = None
    checkpoint_path: Path | None = None
    model: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str = ""
    base_url_env: str = "ANTHROPIC_BASE_URL"
    judge_timeout_sec: float = field(default_factory=default_judge_timeout_seconds)
    judge_max_tokens: int = 8192
    judge_context_window_tokens: int = DEFAULT_JUDGE_CONTEXT_WINDOW_TOKENS
    judge_context_safety_margin_tokens: int = (
        DEFAULT_JUDGE_CONTEXT_SAFETY_MARGIN_TOKENS
    )
    thinking_mode: str = "auto"
    provider_error_threshold: int = 3

    def __post_init__(self) -> None:
        if self.engine not in {"legacy", "recursive-agentic"}:
            raise ValueError("engine must be legacy or recursive-agentic")
        if self.fusion_mode not in {"off", "retrieval-global"}:
            raise ValueError("fusion_mode must be off or retrieval-global")
        if not isinstance(self.analysis_perspective, str):
            raise TypeError("analysis_perspective must be a string")
        object.__setattr__(self, "analysis_perspective", self.analysis_perspective.strip())
        for name in ("model", "api_key_env", "base_url", "base_url_env"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError("{0} must be a string".format(name))
            object.__setattr__(self, name, value.strip())
        if self.thinking_mode not in {"auto", "enabled", "disabled"}:
            raise ValueError("thinking_mode must be auto, enabled, or disabled")
        for name in ("lineage_output_path", "judge_cache_path", "checkpoint_path"):
            object.__setattr__(self, name, _optional_path(getattr(self, name), name))
        timeout = self.judge_timeout_sec
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout <= 0
        ):
            raise ValueError("judge_timeout_sec must be a positive finite number")
        object.__setattr__(self, "judge_timeout_sec", float(timeout))
        for name in (
            "judge_max_tokens",
            "judge_context_window_tokens",
            "judge_context_safety_margin_tokens",
            "provider_error_threshold",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("{0} must be a positive integer".format(name))
        JudgeContextBudget(
            context_window_tokens=self.judge_context_window_tokens,
            max_output_tokens=self.judge_max_tokens,
            safety_margin_tokens=self.judge_context_safety_margin_tokens,
        )
        for name in (
            "max_depth",
            "max_nodes",
            "max_frontier_items",
            "max_hypotheses",
            "max_investigation_rounds",
            "max_artifact_bytes",
            "max_judge_requests",
        ):
            value = getattr(self, name)
            if name == "max_depth" and value is None:
                continue
            if type(value) is not int or value < 0:
                raise ValueError("{0} must be a non-negative integer".format(name))

    @property
    def effective_max_depth(self) -> int:
        if self.max_depth is not None:
            return self.max_depth
        return 20 if self.engine == "recursive-agentic" else 8


@dataclass(frozen=True)
class AttributionRequest:
    trace_path: Path | None
    output_path: Path
    question: str = ""
    objective: str = ""
    benchmark_bundle_path: Path | None = None
    review_path: Path | None = None
    evaluation_paths: tuple[Path, ...] = ()
    start_refs: tuple[str, ...] = ()
    options: AttributionOptions = field(default_factory=AttributionOptions)

    def __post_init__(self) -> None:
        if not isinstance(self.question, str):
            raise TypeError("question must be a string")
        if not isinstance(self.objective, str):
            raise TypeError("objective must be a string")
        normalized_question = normalize_question(self.question)
        if self.question and not normalized_question:
            raise ValueError("question must be non-empty")
        objective = self.objective.strip()
        if normalized_question and objective:
            raise ValueError("question and objective are mutually exclusive")
        if normalized_question and len(normalized_question) > MAX_QUESTION_CHARS:
            raise ValueError(
                "question must not exceed {0:,} characters".format(MAX_QUESTION_CHARS)
            )
        if (self.trace_path is None) == (self.benchmark_bundle_path is None):
            raise ValueError(
                "exactly one of trace_path and benchmark_bundle_path must be provided"
            )
        object.__setattr__(self, "trace_path", _optional_path(self.trace_path, "trace_path"))
        object.__setattr__(self, "output_path", _normalized_path(self.output_path, "output_path"))
        object.__setattr__(
            self,
            "benchmark_bundle_path",
            _optional_path(self.benchmark_bundle_path, "benchmark_bundle_path"),
        )
        object.__setattr__(self, "review_path", _optional_path(self.review_path, "review_path"))
        object.__setattr__(
            self,
            "evaluation_paths",
            _path_tuple(self.evaluation_paths, "evaluation_paths"),
        )
        object.__setattr__(self, "start_refs", _start_ref_tuple(self.start_refs))
        object.__setattr__(self, "objective", objective)
        if not isinstance(self.options, AttributionOptions):
            raise TypeError("options must be AttributionOptions")

    @property
    def question_info(self) -> AttributionQuestion | None:
        return AttributionQuestion.create(self.question) if self.question else None

    @property
    def question_binding(self) -> AttributionQuestion | None:
        return self.question_info

    @property
    def normalized_question(self) -> str:
        question = self.question_info
        return question.normalized if question is not None else ""

    @property
    def effective_objective(self) -> str:
        return self.normalized_question or self.objective or DEFAULT_ATTRIBUTION_OBJECTIVE


@dataclass(frozen=True)
class AttributionResult:
    output_path: Path
    lineage_path: Path | None
    payload: Mapping[str, Any]
    question: AttributionQuestion | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", _normalized_path(self.output_path, "output_path"))
        object.__setattr__(self, "lineage_path", _optional_path(self.lineage_path, "lineage_path"))
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        if self.question is not None and not isinstance(self.question, AttributionQuestion):
            raise TypeError("question must be an AttributionQuestion or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "lineage_path": str(self.lineage_path) if self.lineage_path is not None else None,
            "payload": _thaw_json(self.payload),
            "question": (
                {
                    "original": self.question.original,
                    "normalized": self.question.normalized,
                    "question_id": self.question.question_id,
                }
                if self.question is not None
                else None
            ),
        }
