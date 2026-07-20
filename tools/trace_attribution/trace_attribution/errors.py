from __future__ import annotations


class JudgeProviderError(RuntimeError):
    """A retryable connection or timeout failure from the model provider."""


class JudgeProviderUnavailable(JudgeProviderError):
    """The provider circuit is open after repeated request failures."""


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
