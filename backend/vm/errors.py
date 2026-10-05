"""VM error types."""

from __future__ import annotations


class VMError(Exception):
    """A VM operation failed for a reason worth showing to the user."""
