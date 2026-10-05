"""Provider API keys in the macOS Keychain.

Apps started from Finder do not inherit shell environment variables, so the
packaged app needs somewhere else to keep keys. They are stored as generic
passwords under one service name; the account is the provider's key name
(for example ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable

from backend.logging_config import get_logger

log = get_logger("keystore")

KEYCHAIN_SERVICE = "agent-office"
KEY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Keys are sent to `security` on stdin, where quotes and backslashes are syntax.
UNSAFE_VALUE = re.compile(r"[\s\"'\\]")

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


class KeyStoreError(Exception):
    pass


def run_security(args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/security", *args], input=stdin, capture_output=True, text=True, timeout=15
    )


class KeyStore:
    """Looks a key up in the environment first, then in the Keychain."""

    def __init__(self, run: Runner = run_security) -> None:
        self._run = run

    def get(self, name: str) -> str | None:
        if value := os.environ.get(name):
            return value
        if not KEY_NAME.match(name):
            return None
        try:
            result = self._run(
                ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"], None
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None if result.returncode == 0 else None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def set(self, name: str, value: str) -> None:
        if not KEY_NAME.match(name):
            raise KeyStoreError("invalid key name")
        if not value or UNSAFE_VALUE.search(value):
            raise KeyStoreError("an API key cannot be empty or contain spaces or quotes")
        # Interactive mode keeps the key out of the process list.
        command = f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a {name} -w {value}\n"
        try:
            result = self._run(["-i"], command)
        except (OSError, subprocess.SubprocessError) as exc:
            raise KeyStoreError("could not reach the macOS Keychain") from exc
        if result.returncode != 0 or not self.has(name):
            raise KeyStoreError("the macOS Keychain did not accept the key")
        log.info("api key stored", extra={"key_name": name})

    def delete(self, name: str) -> None:
        if KEY_NAME.match(name):
            self._run(["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name], None)
            log.info("api key removed", extra={"key_name": name})
