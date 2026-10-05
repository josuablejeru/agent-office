"""Keeps element numbers usable when a page redraws itself (stdlib only).

A page snapshot numbers the interactive elements. Many pages rebuild their
content afterwards, which loses the numbering. Each number therefore also keeps
a description of its element, used to find the same element again.
"""

from __future__ import annotations

from typing import Any


def same_page(url_a: str, url_b: str) -> bool:
    """Whether two addresses are the same page, ignoring the #fragment."""
    return url_a.split("#", 1)[0] == url_b.split("#", 1)[0]


def find_again(wanted: dict[str, Any], candidates: list[dict[str, Any]]) -> int | None:
    """Return the `ref` of the one candidate that is the element `wanted` described.

    Tries the strictest description first and loosens it step by step; a step
    only counts if exactly one candidate fits, so a guess is never clicked.
    """
    tag, href, label = wanted.get("tag"), wanted.get("href"), wanted.get("label")
    rules = []
    if href and label:
        rules.append(lambda c: c.get("tag") == tag and c.get("href") == href and c.get("label") == label)
    if href:
        rules.append(lambda c: c.get("tag") == tag and c.get("href") == href)
    if label:
        rules.append(
            lambda c: c.get("tag") == tag and c.get("label") == label and c.get("type") == wanted.get("type")
        )
    for rule in rules:
        matches = [candidate for candidate in candidates if rule(candidate)]
        if len(matches) == 1:
            return int(matches[0]["ref"])
    return None
