"""Settings from the user's login shell, for an app opened from Finder or the Dock.

macOS starts apps without running the user's shell, so variables set in
~/.config/fish/config.fish, ~/.zshrc and the like are missing, while tools
started from a terminal (gcloud, Claude Code) see them. A few that decide how
this app reaches Google Cloud and the network are read once from the login
shell at start and added, unless already set.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from backend.logging_config import get_logger

log = get_logger("app")

# Only these: they say where cloud credentials are and how to reach the internet.
IMPORTED = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDSDK_CONFIG",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
)
MARKER = "__agent_office_env__"
TIMEOUT_SECONDS = 8


def login_shell() -> str:
    shell = os.environ.get("SHELL") or ""
    return shell if shell and Path(shell).is_file() else "/bin/zsh"


def read_login_environment(shell: str | None = None, interactive: bool = False) -> dict[str, str]:
    """The environment a login shell ends up with. Empty if the shell fails or hangs.

    `interactive` also reads ~/.zshrc or ~/.bashrc, where nvm, pyenv and the
    like usually put their PATH (fish reads config.fish either way).
    """
    # The marker separates the environment from anything the shell's startup files print.
    command = f"echo {MARKER}; /usr/bin/env -0"
    shell = shell or login_shell()
    flags = ["-i", "-l"] if interactive and Path(shell).name in ("zsh", "bash") else ["-l"]
    try:
        result = subprocess.run(
            [shell, *flags, "-c", command],
            capture_output=True, timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    _, found, environment = result.stdout.partition(MARKER.encode() + b"\n")
    if not found:
        return {}
    variables = {}
    for entry in environment.split(b"\0"):
        name, equals, value = entry.decode(errors="replace").partition("=")
        if equals:
            variables[name] = value
    return variables


FISH = ("/opt/homebrew/bin/fish", "/usr/local/bin/fish", "/usr/bin/fish")


def terminal_shells() -> list[str]:
    """The account's login shell, and fish when it is set up: many people run fish in
    their terminal while the account itself stays on zsh."""
    shells = [login_shell()]
    fish = next((path for path in FISH if Path(path).is_file()), None)
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "fish"
    if fish and config.is_dir() and Path(shells[0]).name != "fish":
        shells.append(fish)
    return shells


def read_terminal_environment() -> dict[str, str]:
    """The environment of the user's terminal shells together.

    PATH joins all of them (the first shell's folders first); any other
    variable comes from the first shell that sets it.
    """
    merged: dict[str, str] = {}
    paths: list[str] = []
    for shell in terminal_shells():
        found = read_login_environment(shell, interactive=True)
        for folder in found.get("PATH", "").split(":"):
            if folder and folder not in paths:
                paths.append(folder)
        for name, value in found.items():
            merged.setdefault(name, value)
    if paths:
        merged["PATH"] = ":".join(paths)
    return merged


def import_login_environment(shell: str | None = None) -> list[str]:
    """Add the listed variables from the login shell. Returns the names added."""
    environment = read_login_environment(shell) if shell else read_terminal_environment()
    added = []
    for name in IMPORTED:
        if name in environment and environment[name] and name not in os.environ:
            os.environ[name] = environment[name]
            added.append(name)
    if added:
        log.info("settings taken from the login shell", extra={"variables": added})
    return added
