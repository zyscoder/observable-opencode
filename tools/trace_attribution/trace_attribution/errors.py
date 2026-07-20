from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class JudgeProviderError(RuntimeError):
    """A retryable connection or timeout failure from the model provider."""


class JudgeProviderUnavailable(JudgeProviderError):
    """The provider circuit is open after repeated request failures."""


def _physical_request_delta(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1}:
        raise ValueError("transport physical_requests must be exactly 0 or 1")
    return value


@dataclass(frozen=True)
class TransportCallResult:
    """Result returned at the physical transport boundary."""

    text: str
    physical_requests: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "physical_requests", _physical_request_delta(self.physical_requests)
        )


class TransportCallError(RuntimeError):
    """Transport failure carrying whether a physical request was attempted."""

    def __init__(self, error: BaseException, *, physical_requests: int) -> None:
        self.error = error
        self.physical_requests = _physical_request_delta(physical_requests)
        super().__init__("{0}: {1}".format(type(error).__name__, error))


def is_provider_request_error(error: BaseException) -> bool:
    if isinstance(error, (JudgeProviderError, ConnectionError, TimeoutError)):
        return True
    name = type(error).__name__.lower()
    text = f"{type(error).__name__}: {error}".lower()
    markers = (
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connectionerror",
        "connection error",
        "readtimeout",
        "connecttimeout",
        "timeout",
        "network error",
    )
    return any(marker in name or marker in text for marker in markers)
