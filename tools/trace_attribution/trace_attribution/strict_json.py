"""Strict JSON parsing for immutable factual attribution inputs."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


class StrictJsonError(ValueError):
    """Raised when factual JSON is ambiguous or non-standard."""


def load_json_object(data: bytes, label: str) -> Dict[str, Any]:
    if not isinstance(data, bytes):
        raise StrictJsonError("{0} must be exact bytes".format(label))
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_number,
        )
    except UnicodeError as error:
        raise StrictJsonError(
            "{0} is not valid UTF-8 JSON: {1}".format(label, error)
        ) from error
    except (json.JSONDecodeError, StrictJsonError) as error:
        raise StrictJsonError(
            "{0} is not valid strict JSON: {1}".format(label, error)
        ) from error
    if not isinstance(value, dict):
        raise StrictJsonError("{0} must contain a JSON object".format(label))
    return value


def _object_without_duplicate_keys(
    pairs: List[Tuple[str, Any]],
) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise StrictJsonError(
                "JSON object contains duplicate key: {0}".format(key)
            )
        value[key] = item
    return value


def _reject_nonfinite_number(constant: str) -> Any:
    raise StrictJsonError(
        "JSON contains non-finite number: {0}".format(constant)
    )


__all__ = ["StrictJsonError", "load_json_object"]
