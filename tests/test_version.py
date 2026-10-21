"""Which build is running, as shown under Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import version


@pytest.mark.parametrize(("label", "release"), [
    ("v0.4.0", True), ("v0.4.0-rc1", True),
    ("v0.3.0-7-g1a2b3c4", False), ("v0.3.0-7-g1a2b3c4-dirty", False), ("v0.4.0-dirty", False),
    ("1a2b3c4", False), ("unknown", False),
])
def test_releases_are_told_apart_from_local_builds(label: str, release: bool) -> None:
    assert version.is_release(label) is release


def test_a_bundle_says_what_it_was_built_as(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "VERSION").write_text("v0.4.0\n")
    monkeypatch.setattr(version, "VERSION_FILE", tmp_path / "VERSION")
    version.running_version.cache_clear()
    try:
        assert version.running_version() == "v0.4.0"
    finally:
        version.running_version.cache_clear()


def test_from_source_the_checkout_says_which_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(version, "VERSION_FILE", tmp_path / "missing")
    version.running_version.cache_clear()
    try:
        assert version.running_version() != "unknown"  # this repository is a git checkout
    finally:
        version.running_version.cache_clear()
