"""Reading the user's terminal shells, for an app opened from Finder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def test_fish_in_the_terminal_counts_too_when_the_account_shell_is_zsh(monkeypatch: Any) -> None:
    from backend import shell_env

    shells = {"/bin/zsh": {"PATH": "/usr/bin:/opt/homebrew/bin", "HTTPS_PROXY": "http://zsh-proxy"},
              "/opt/homebrew/bin/fish": {"PATH": "/Users/x/.cargo/bin:/usr/bin", "HTTPS_PROXY": "http://fish-proxy",
                                         "CLOUDSDK_CONFIG": "/Users/x/gcloud"}}
    monkeypatch.setattr(shell_env, "terminal_shells", lambda: list(shells))
    monkeypatch.setattr(shell_env, "read_login_environment", lambda shell=None, interactive=False: shells[shell])
    merged = shell_env.read_terminal_environment()
    assert merged["PATH"] == "/usr/bin:/opt/homebrew/bin:/Users/x/.cargo/bin"  # uvx from fish's PATH is found
    assert merged["HTTPS_PROXY"] == "http://zsh-proxy" and merged["CLOUDSDK_CONFIG"] == "/Users/x/gcloud"


FISH = next((path for path in ("/opt/homebrew/bin/fish", "/usr/local/bin/fish", "/usr/bin/fish") if Path(path).is_file()), None)


@pytest.mark.skipif(FISH is None, reason="fish is not installed")
def test_fish_reads_its_conf_d_like_the_uv_installer_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.shell_env import read_login_environment, read_terminal_environment

    conf = tmp_path / "fish" / "conf.d"
    conf.mkdir(parents=True)
    cargo = tmp_path / "cargo" / "bin"  # fish_add_path skips folders that do not exist
    cargo.mkdir(parents=True)
    (conf / "uv.env.fish").write_text(
        f"fish_add_path --global --prepend {cargo}\nset -gx HTTPS_PROXY http://fish-proxy:8080\n"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    found = read_login_environment(FISH, interactive=True)
    assert str(cargo) in found["PATH"].split(":") and found["HTTPS_PROXY"] == "http://fish-proxy:8080"

    # The account shell is zsh, the terminal runs fish: both count.
    monkeypatch.setenv("SHELL", "/bin/zsh")
    merged = read_terminal_environment()
    assert str(cargo) in merged["PATH"].split(":") and "/bin" in merged["PATH"].split(":")

    # The account shell is fish itself: read once, as fish.
    monkeypatch.setenv("SHELL", FISH)
    assert str(cargo) in read_terminal_environment()["PATH"].split(":")


def test_fish_is_skipped_when_it_is_not_set_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.shell_env import terminal_shells

    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # no fish folder here
    assert terminal_shells() == ["/bin/zsh"]
