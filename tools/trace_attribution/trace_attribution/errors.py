from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


class JudgeProviderError(RuntimeError):
    """A retryable connection or timeout failure from the model provider."""


class JudgeProviderUnavailable(JudgeProviderError):
    """The provider circuit is open after repeated request failures."""


class AttributionInputError(ValueError):
    """Invalid user-selected attribution input that is safe for concise CLI display."""


@dataclass(frozen=True)
class ProviderFailureDisposition:
    """Structured retry and circuit-breaker semantics for a Provider failure."""

    retryable: bool
    category: str
    status_code: Optional[int]
    error_code: str
    reason: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderFailureDisposition":
        expected = {
            "retryable",
            "category",
            "status_code",
            "error_code",
            "reason",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("provider failure disposition schema mismatch")
        status_code = value["status_code"]
        if type(value["retryable"]) is not bool:
            raise ValueError("provider failure retryable flag is invalid")
        if status_code is not None and type(status_code) is not int:
            raise ValueError("provider failure status code is invalid")
        for key in ("category", "error_code", "reason"):
            if not isinstance(value[key], str):
                raise ValueError("provider failure {0} is invalid".format(key))
        return cls(
            retryable=value["retryable"],
            category=value["category"],
            status_code=status_code,
            error_code=value["error_code"],
            reason=value["reason"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "retryable": self.retryable,
            "category": self.category,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "reason": self.reason,
        }


def classify_provider_failure(
    *, status: Optional[int], code: Any, reason: str = ""
) -> ProviderFailureDisposition:
    """Classify a Provider failure without relying on its human-readable text."""
    status_code = status if type(status) is int else None
    error_code = str(code or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    context_window_markers = (
        "maximum context length",
        "context length exceeded",
        "context window exceeded",
        "context_length_exceeded",
        "context_window_exceeded",
        "prompt is too long",
        "too many input tokens",
        "input tokens exceed",
    )
    if status_code == 400 and any(
        marker in value
        for marker in context_window_markers
        for value in (error_code, normalized_reason)
    ):
        return ProviderFailureDisposition(
            False,
            "context_window_exceeded",
            status_code,
            "context_window_exceeded",
            reason,
        )
    transient_markers = (
        "temporary",
        "timeout",
        "unavailable",
        "overloaded",
        "rate_limit",
        "try_again",
    )
    if status_code == 400:
        retryable = bool(error_code) and any(
            marker in error_code for marker in transient_markers
        )
        return ProviderFailureDisposition(
            retryable,
            "http_retryable" if retryable else "http_non_retryable",
            status_code,
            error_code,
            reason,
        )
    if status_code in {401, 402, 403, 404}:
        return ProviderFailureDisposition(False, "http_non_retryable", status_code, error_code, reason)
    if status_code in {408, 409, 425, 429} or (
        status_code is not None and 500 <= status_code <= 599
    ):
        return ProviderFailureDisposition(True, "http_retryable", status_code, error_code, reason)
    retryable_codes = (
        "connection", "connect_timeout", "read_timeout", "timeout", "dns",
        "network", "service_unavailable", "temporarily_unavailable",
    )
    if any(marker in error_code for marker in retryable_codes):
        return ProviderFailureDisposition(True, "network_retryable", status_code, error_code, reason)
    return ProviderFailureDisposition(False, "provider_non_retryable", status_code, error_code, reason)


def provider_failure_disposition_from_value(
    value: Any,
) -> Optional[ProviderFailureDisposition]:
    if value is None:
        return None
    if isinstance(value, ProviderFailureDisposition):
        return value
    if isinstance(value, Mapping):
        return ProviderFailureDisposition.from_dict(value)
    raise ValueError("provider failure disposition is invalid")


def provider_failure_disposition_to_dict(value: Any) -> Optional[dict[str, Any]]:
    disposition = provider_failure_disposition_from_value(value)
    return disposition.to_dict() if disposition is not None else None


def _structured_error_field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None) if value is not None else None


def _structured_error_body_values(value: Any) -> tuple[Any, Any]:
    if value is None:
        return None, None
    nested = _structured_error_field(value, "error")
    sources = (nested, value) if nested is not None else (value,)
    code = None
    message = None
    for source in sources:
        if code is None:
            code = _structured_error_field(source, "code")
        if code is None:
            code = _structured_error_field(source, "type")
        if message is None:
            message = _structured_error_field(source, "message")
    return code, message


def provider_failure_fields(error: BaseException) -> tuple[Optional[int], Any]:
    """Read structured transport fields from SDK errors and response envelopes."""
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    code = getattr(error, "code", None)
    if code is None:
        code = getattr(error, "type", None)
    own_body_code, _ = _structured_error_body_values(
        getattr(error, "body", None)
    )
    if code is None:
        code = own_body_code
    if code is None and response is not None:
        code = getattr(response, "code", None)
    if code is None and response is not None:
        code = getattr(response, "type", None)
    if code is None:
        code, _ = _structured_error_body_values(
            getattr(response, "body", None)
        )
    return (status if type(status) is int else None), code


def provider_failure_reason(error: BaseException) -> str:
    """Prefer the SDK's structured Provider message over a generic wrapper."""
    response = getattr(error, "response", None)
    _, own_message = _structured_error_body_values(
        getattr(error, "body", None)
    )
    _, response_message = _structured_error_body_values(
        getattr(response, "body", None)
    )
    message = own_message or response_message or getattr(error, "message", None)
    return "{0}: {1}".format(
        type(error).__name__,
        str(message or error),
    )


def provider_failure_disposition(
    error: BaseException,
) -> Optional[ProviderFailureDisposition]:
    """Extract structured Provider details when an exception is a Provider failure."""
    status, code = provider_failure_fields(error)
    reason = provider_failure_reason(error)
    if type(status) is int or code:
        return classify_provider_failure(status=status, code=code, reason=reason)
    if is_provider_request_error(error):
        return classify_provider_failure(status=None, code=type(error).__name__, reason=reason)
    return None


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
