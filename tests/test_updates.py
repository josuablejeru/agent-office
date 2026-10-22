"""Updates: finding a new release, and refusing anything that is not genuinely ours."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from backend.config import Settings
from backend.updates import Updater, bundle_of, newer, parse_version
from tests.fake_github import FakeGitHub

# A real, notarized release archive, for the tests that check signatures (not in CI).
RELEASE_ZIP = os.environ.get("AGENT_OFFICE_RELEASE_ZIP", "")
needs_release = pytest.mark.skipif(not RELEASE_ZIP or not Path(RELEASE_ZIP).is_file(),
                                   reason="set AGENT_OFFICE_RELEASE_ZIP to a downloaded release zip")


def test_versions_compare_as_numbers_and_never_go_back() -> None:
    assert parse_version("v0.10.0") > parse_version("v0.9.9")  # type: ignore[operator]
    assert parse_version("v1.2.3-beta") is None and parse_version("v1.2.3-4-gabc") is None
    assert newer("v0.10.0", "v0.9.9") and newer("v1.0.0", "v0.4.0-3-gabc1234-dirty")
    assert not newer("v0.4.0", "v0.4.0") and not newer("v0.3.9", "v0.4.0") and not newer("nonsense", "v0.4.0")
    # Release candidates come before their release, in order.
    assert newer("v0.5.0-rc1", "v0.4.0") and newer("v0.5.0-rc2", "v0.5.0-rc1") and newer("v0.5.0", "v0.5.0-rc2")
    assert not newer("v0.5.0-rc3", "v0.5.0") and newer("v0.5.0-rc10", "v0.5.0-rc9")


def test_the_app_bundle_is_found_from_its_python() -> None:
    app = bundle_of("/Applications/Agent Office.app/Contents/Resources/python/bin/python3.12")
    assert app is not None and app.name == "Agent Office.app"
    assert bundle_of("/usr/local/bin/python3") is None


def updater(tmp_path: Path, github: FakeGitHub, version: str = "v0.4.0", **kwargs: Any) -> Updater:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    return Updater(settings, version=version, releases_url=github.latest_url, **kwargs)


def test_a_newer_release_is_found_and_the_answer_is_cached(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        github.publish("v0.5.0", notes="## What's new\n- Auto update")
        found = asyncio.run(updater(tmp_path, github).check())
        assert found.available and found.latest == "v0.5.0" and "Auto update" in found.notes
        again = updater(tmp_path, github)  # the next start: knows it already, asks with the ETag
        assert again.status().available
        asyncio.run(again.check())
        assert github.requests[-1]["if-none-match"]

        older = FakeGitHub()
        older.publish("v0.3.0")  # older than what runs: nothing to offer
        assert not asyncio.run(updater(tmp_path / "other", older).check()).available
        older.close()
    finally:
        github.close()


def test_a_failed_check_is_quiet_unless_the_user_asked(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        github.publish("v0.5.0")
        github.limited = True
        assert asyncio.run(updater(tmp_path, github).check(quiet=True)).state == "idle"
        loud = asyncio.run(updater(tmp_path, github).check(quiet=False))
        assert loud.state == "error" and "limiting requests" in loud.detail
    finally:
        github.close()


def test_later_hides_that_release_only(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        github.publish("v0.5.0")
        first = updater(tmp_path, github)
        asyncio.run(first.check())
        assert first.dismiss().dismissed == "v0.5.0"
        assert updater(tmp_path, github).status().dismissed == "v0.5.0"  # remembered across starts
    finally:
        github.close()


def test_only_an_installed_release_build_replaces_itself(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        local = updater(tmp_path, github, version="v0.4.0-3-gabc1234-dirty")
        assert "local build" in (local.install_problem() or "")
        from_source = updater(tmp_path, github, version="v0.4.0", executable="/usr/bin/python3")
        assert "installed app only" in (from_source.install_problem() or "")
        translocated = updater(tmp_path, github, executable=(
            "/private/var/folders/x/AppTranslocation/ABC/d/Agent Office.app/Contents/Resources/python/bin/python3.12"))
        translocated.quit_app = lambda: None
        assert "Applications folder" in (translocated.install_problem() or "")
    finally:
        github.close()


# --- with a real release archive --------------------------------------------------------------


def installed_app(tmp_path: Path) -> Path:
    """A stand-in for /Applications/Agent Office.app, in a folder of the test's own."""
    app = tmp_path / "Applications" / "Agent Office.app"
    (app / "Contents" / "Resources" / "python" / "bin").mkdir(parents=True)
    (app / "Contents" / "old-version").write_text("v0.3.9")
    return app


async def download(update: Updater) -> Any:
    await update.check()
    update.start_download()
    if update._task is not None:  # noqa: SLF001
        await update._task  # noqa: SLF001
    return update.status()


@needs_release
def test_a_genuine_release_is_downloaded_verified_and_swapped_in(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = installed_app(tmp_path)
    launcher = subprocess.Popen(["sleep", "300"])  # plays the app's launcher
    try:
        github.publish("v0.4.0", Path(RELEASE_ZIP))
        update = updater(tmp_path, github, version="v0.3.9",
                         executable=str(app / "Contents" / "Resources" / "python" / "bin" / "python3.12"),
                         launcher_pid=launcher.pid)
        def quit_app() -> None:  # quitting the app ends the launcher (macOS then reaps it)
            launcher.terminate()
            launcher.wait()

        update.quit_app = quit_app
        update.reopen = False
        ready = asyncio.run(download(update))
        assert ready.state == "ready", ready.detail
        assert update.install().state == "installing"
        for _ in range(60):
            if (app / "Contents" / "Resources" / "app" / "VERSION").exists():
                break
            time.sleep(0.5)
        assert (app / "Contents" / "Resources" / "app" / "VERSION").read_text().strip() == "v0.4.0"
        previous = update.directory / "previous" / "Agent Office.app"
        assert (previous / "Contents" / "old-version").exists()  # the old one is kept aside
        assert "installed" in (update.directory / "update.log").read_text()
    finally:
        launcher.kill()
        github.close()


@needs_release
def test_an_update_is_not_installed_while_an_agent_is_working(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = installed_app(tmp_path)
    try:
        github.publish("v0.4.0", Path(RELEASE_ZIP))
        update = updater(tmp_path, github, version="v0.3.9", launcher_pid=1,
                         executable=str(app / "Contents" / "Resources" / "python" / "bin" / "python3.12"),
                         busy=lambda: "maya is working")
        quits: list[bool] = []
        update.quit_app = lambda: quits.append(True)
        assert asyncio.run(download(update)).state == "ready"
        held = update.install()
        assert held.state == "ready" and "maya is working" in held.detail and quits == []
    finally:
        github.close()


def tampered_zip(tmp_path: Path, change: str) -> Path:
    """The real release, unpacked, changed, and packed again."""
    work = tmp_path / "tamper"
    subprocess.run(["ditto", "-x", "-k", RELEASE_ZIP, str(work)], check=True)
    app = work / "Agent Office.app"
    if change == "file":
        (app / "Contents" / "Resources" / "app" / "backend" / "main.py").write_text("print('changed')\n")
    archive = tmp_path / "tampered.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    shutil.rmtree(work)
    return archive


@needs_release
@pytest.mark.parametrize(("case", "expected"), [
    ("checksum", "damaged"),
    ("file", "changed after signing"),
    ("version", "not v0.4.1"),
])
def test_anything_not_genuinely_ours_is_refused(tmp_path: Path, case: str, expected: str) -> None:
    github = FakeGitHub()
    app = installed_app(tmp_path)
    try:
        if case == "checksum":
            github.publish("v0.4.0", Path(RELEASE_ZIP), checksum="0" * 64)
        elif case == "file":
            github.publish("v0.4.0", tampered_zip(tmp_path, "file"))
        else:  # a real v0.4.0 offered as if it were v0.4.1
            github.publish("v0.4.1", Path(RELEASE_ZIP))
        update = updater(tmp_path, github, version="v0.3.9", launcher_pid=1,
                         executable=str(app / "Contents" / "Resources" / "python" / "bin" / "python3.12"))
        update.quit_app = lambda: None
        failed = asyncio.run(download(update))
        assert failed.state == "error" and expected in failed.detail, failed.detail
        assert not list(update.directory.glob("v0.4.*/unpacked"))  # nothing kept
        assert (app / "Contents" / "old-version").exists()  # the installed app untouched
    finally:
        github.close()


def test_the_beta_channel_also_offers_release_candidates(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        github.publish("v0.5.0")
        github.publish("v0.6.0-rc1", prerelease=True, notes="first candidate")
        github.publish("v0.6.0-rc2", prerelease=True, notes="second candidate")
        stable = updater(tmp_path / "stable", github)
        assert asyncio.run(stable.check()).latest == "v0.5.0"  # candidates are not for the stable channel

        beta = updater(tmp_path / "beta", github)
        beta._settings.update_user_config(["app", "update_channel"], "beta")  # noqa: SLF001
        found = asyncio.run(beta.check())
        assert found.channel == "beta" and found.latest == "v0.6.0-rc2" and found.notes == "second candidate"

        github.publish("v0.6.0", notes="final")  # the release itself follows its candidates
        assert asyncio.run(updater(tmp_path / "beta", github).check()).latest == "v0.6.0"

        # Back on stable after running a candidate: nothing older is offered.
        on_candidate = updater(tmp_path / "back", github, version="v0.7.0-rc1")
        assert not asyncio.run(on_candidate.check()).available
    finally:
        github.close()


def test_switching_the_channel_takes_effect_without_asking_github_again(tmp_path: Path) -> None:
    github = FakeGitHub()
    try:
        github.publish("v0.5.0")
        github.publish("v0.6.0-rc1", prerelease=True)
        update = updater(tmp_path, github)
        asyncio.run(update.check())
        asked = len(github.requests)
        assert update.status().latest == "v0.5.0"
        update._settings.update_user_config(["app", "update_channel"], "beta")  # noqa: SLF001
        assert update.status().latest == "v0.6.0-rc1" and len(github.requests) == asked
    finally:
        github.close()


def test_a_local_build_compares_as_the_release_it_was_built_on() -> None:
    # Built after rc2: rc3 and the final release are newer, rc2 itself is not.
    assert newer("v0.5.0-rc3", "v0.5.0-rc2-1-g7105c17") and newer("v0.5.0", "v0.5.0-rc2-4-gabc1234-dirty")
    assert not newer("v0.5.0-rc2", "v0.5.0-rc2-1-g7105c17")
    assert newer("v0.5.1", "v0.5.0-3-gabc1234") and not newer("v0.5.0", "v0.5.0-3-gabc1234")


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        ("proxy page", "did not send a checksum"),
        ("zip refused", "the download failed: 127.0.0.1 answered with HTTP 404"),
        ("host blocked", "the download failed: 127.0.0.1 could not be reached (ConnectError"),
    ],
)
def test_a_failed_download_says_which_host_and_why(tmp_path: Path, break_it: str, expected: str) -> None:
    github = FakeGitHub()
    app = installed_app(tmp_path)
    try:
        github.publish("v0.4.1", b"not really a zip")
        assets = github.releases[-1]["assets"]
        if break_it == "proxy page":
            github.files["Agent-Office-0.4.1-macos-arm64.zip.sha256"] = b"<html><body>Blocked by policy</body></html>"
        elif break_it == "zip refused":
            assets[0]["browser_download_url"] += "-gone"
        else:
            with socket.socket() as probe:  # a port nothing listens on
                probe.bind(("127.0.0.1", 0))
                closed = probe.getsockname()[1]
            assets[0]["browser_download_url"] = f"http://127.0.0.1:{closed}/asset.zip"
        update = updater(tmp_path, github, version="v0.4.0", launcher_pid=1,
                         executable=str(app / "Contents" / "Resources" / "python" / "bin" / "python3.12"))
        update.quit_app = lambda: None
        failed = asyncio.run(download(update))
        assert failed.state == "error" and expected in failed.detail, failed.detail
        assert not list(update.directory.glob("v0.4.1/*"))  # nothing half-downloaded is kept
    finally:
        github.close()
