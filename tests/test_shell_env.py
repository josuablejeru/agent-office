"""Reading the user's terminal shells, for an app opened from Finder."""

from __future__ import annotations

from typing import Any


def test_fish_in_the_terminal_counts_too_when_the_account_shell_is_zsh(monkeypatch: Any) -> None:
    from backend import shell_env

    shells = {"/bin/zsh": {"PATH": "/usr/bin:/opt/homebrew/bin", "HTTPS_PROXY": "http://zsh-proxy"},
              "/opt/homebrew/bin/fish": {"PATH": "/Users/x/.cargo/bin:/usr/bin", "HTTPS_PROXY": "http://fish-proxy",
                                         "CLOUDSDK_CONFIG": "/Users/x/gcloud"}}
    monkeypatch.setattr(shell_env, "terminal_shells", lambda: list(shells))
    monkeypatch.setattr(shell_env, "read_login_environment", lambda shell=None: shells[shell])
    merged = shell_env.read_terminal_environment()
    assert merged["PATH"] == "/usr/bin:/opt/homebrew/bin:/Users/x/.cargo/bin"  # uvx from fish's PATH is found
    assert merged["HTTPS_PROXY"] == "http://zsh-proxy" and merged["CLOUDSDK_CONFIG"] == "/Users/x/gcloud"
