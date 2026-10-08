"""Shared config validation helpers.

Both the node config and the rules config are strict in the same way: unknown keys are
errors rather than warnings, and every message carries a dotted path to the exact key at
fault. A silently-ignored typo is the kind of bug that costs an afternoon.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import ConfigError


def _fail(path: str, message: str) -> None:
    raise ConfigError(f"{path or '<root>'}: {message}")


def _as_mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail(path, f"expected a mapping, got {type(value).__name__}")
    keys = [k for k in value if not isinstance(k, str)]
    if keys:
        _fail(path, f"keys must be strings, got {keys!r}")
    return dict(value)


def _reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        _fail(path, f"unknown key(s) {unknown}; allowed keys are {sorted(allowed)}")


def _require(data: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in data or data[key] is None:
        _fail(path, f"missing required key {key!r}")
    return data[key]


def _number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, f"expected a number, got {value!r}")
    if integer and not float(value).is_integer():
        _fail(path, f"expected an integer, got {value!r}")
    if minimum is not None and value < minimum:
        _fail(path, f"must be >= {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        _fail(path, f"must be <= {maximum}, got {value!r}")
    return int(value) if integer else float(value)


def _string(value: Any, path: str, *, choices: Sequence[str] | None = None) -> str:
    if not isinstance(value, str):
        _fail(path, f"expected a string, got {value!r}")
    if choices is not None and value not in choices:
        _fail(path, f"expected one of {list(choices)}, got {value!r}")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail(path, f"expected true or false, got {value!r}")
    return value


def _pair(value: Any, path: str, *, integer: bool = False, minimum: float | None = None) -> tuple:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        _fail(path, f"expected a 2-element list, got {value!r}")
    return tuple(
        _number(item, f"{path}[{i}]", integer=integer, minimum=minimum)
        for i, item in enumerate(value)
    )
