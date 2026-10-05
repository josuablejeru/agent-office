"""Argument validation helpers for guest operations (stdlib only)."""

from __future__ import annotations

from typing import Any


class OperationError(Exception):
    """An operation was rejected or failed in a way the caller should see."""


def require_str(args: dict[str, Any], key: str, max_length: int) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise OperationError(f"'{key}' must be a non-empty string")
    if len(value) > max_length:
        raise OperationError(f"'{key}' is longer than {max_length} characters")
    return value


def optional_str(args: dict[str, Any], key: str, max_length: int) -> str | None:
    if args.get(key) is None:
        return None
    return require_str(args, key, max_length)


def optional_number(
    args: dict[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OperationError(f"'{key}' must be a number")
    if not minimum <= value <= maximum:
        raise OperationError(f"'{key}' must be between {minimum:g} and {maximum:g}")
    return float(value)


def optional_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise OperationError(f"'{key}' must be true or false")
    return value
