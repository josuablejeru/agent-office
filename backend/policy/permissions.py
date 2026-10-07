"""What each agent may do: one level per kind of tool, chosen by the user.

These settings can only make an agent more restricted. The built-in safety
rules still apply on top: "allow" never skips an approval a rule asks for.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from backend.db.models import Agent
from backend.policy.actions import PolicyAction

Level = Literal["allow", "ask", "off"]
UnknownAction = Literal["default", "allow", "ask"]

# key, name shown to the user, what it covers
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("shell", "Run commands", "Run commands and programs on its computer, and install software."),
    ("files", "Files", "Read and write files on its computer, including the Shared folder."),
    ("browser", "Browser", "Open pages, click, type and take screenshots in its Chrome."),
    ("search", "Web search", "Look things up with a search engine."),
    ("databases", "Databases", "Create and change its own tables of data."),
    ("memory", "Memory", "Remember facts between conversations, and recall them."),
    ("channels", "Channels", "Read channels and post messages to colleagues."),
)
GROUP_KEYS = tuple(key for key, _, _ in GROUPS)
GROUP_NAMES = {key: name for key, name, _ in GROUPS}

# The switches that existed before levels did; still kept in step for old data.
LEGACY_SWITCHES = {"shell": "perm_shell", "files": "perm_files", "browser": "perm_browser", "search": "perm_browser"}

# In the user's words: what the built-in rules stop for, whatever is chosen here.
ALWAYS_ASKS = (
    "Deleting folders, or anything in system directories",
    "Downloading a script and running it straight away",
    "Formatting, wiping or writing directly to a disk",
    "Removing installed software",
    "Changing users, passwords or system files",
    "Shutting down or restarting the computer",
    "Dropping a database table, or deleting or overwriting every row of one",
)


def group_of(operation: str) -> str | None:
    """The permission group a guest or host operation belongs to."""
    if operation == "browser.search":
        return "search"
    prefix = operation.split(".", 1)[0].split("_", 1)[0]
    return {"shell": "shell", "file": "files", "browser": "browser", "memory": "memory",
            "channel": "channels", "web": "search", "db": "databases"}.get(prefix)


def permissions_of(agent: Agent) -> dict[str, Level]:
    """The level of every group, filling in what was never chosen."""
    stored: dict[str, Any] = {}
    if agent.permissions_json:
        try:
            loaded = json.loads(agent.permissions_json)
            stored = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            stored = {}
    levels: dict[str, Level] = {}
    for key in GROUP_KEYS:
        if stored.get(key) in ("allow", "ask", "off"):
            levels[key] = stored[key]
        else:
            # Never chosen: what the old on/off switch said, or allowed.
            switch = LEGACY_SWITCHES.get(key)
            levels[key] = "off" if switch and not getattr(agent, switch) else "allow"
    return levels


def store_permissions(agent: Agent, levels: dict[str, Level]) -> None:
    merged = {**permissions_of(agent), **levels}
    agent.permissions_json = json.dumps(merged, sort_keys=True)
    for key in ("shell", "files", "browser"):
        setattr(agent, LEGACY_SWITCHES[key], merged[key] != "off")


def unknown_action_of(agent: Agent) -> PolicyAction | None:
    """What this agent does with actions no rule covers; None means the app's default."""
    return {"allow": PolicyAction.ALLOW, "ask": PolicyAction.REQUIRE_APPROVAL}.get(agent.unknown_action)
