"""Telling the user that an agent needs them. The default does nothing (tests, dev server)."""

from __future__ import annotations


class Notifier:
    def attention_changed(self, waiting: int) -> None:
        """`waiting` agents are now paused for the user's approval."""

    def run_ended(self, agent_name: str, succeeded: bool) -> None:
        """An agent finished (or failed) the task it was given."""
