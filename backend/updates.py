"""Updates from GitHub Releases: check, download, verify, and swap the app after it quits.

Only a release build offers to install: a local build says a release exists,
nothing more. Nothing is installed unless the download is an Apple-notarized
app signed with this project's Developer ID, for this app, of a newer version.
The swap happens after the app has quit and its agents' computers are off;
if anything goes wrong, the old app is put back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from backend.config import Settings
from backend.logging_config import get_logger
from backend.version import is_release, running_version

log = get_logger("updates")

REPOSITORY = "josuablejeru/agent-office"
RELEASES_URL = os.environ.get("AGENT_OFFICE_UPDATE_URL", f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
BUNDLE_ID = "com.josuablejeru.agent-office"
TEAM_ID = "X82QNBM4C4"
# Apple-anchored, a Developer ID Application certificate (not a development one), this team, this app.
REQUIREMENT = (
    f'anchor apple generic and identifier "{BUNDLE_ID}" and certificate leaf[subject.OU] = "{TEAM_ID}" '
    "and certificate 1[field.1.2.840.113635.100.6.2.6] exists "
    "and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
)
CHECK_EVERY_SECONDS = 24 * 3600
FREE_BYTES_NEEDED = 1024**3


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for a plain release tag like v1.2.3; None for anything else."""
    found = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return (int(found[1]), int(found[2]), int(found[3])) if found else None


def newer(candidate: str, current: str) -> bool:
    new, old = parse_version(candidate), parse_version(current.split("-")[0])
    return new is not None and old is not None and new > old


def bundle_of(executable: str) -> Path | None:
    """The .app the running Python belongs to (…/X.app/Contents/Resources/python/bin/python3.12)."""
    for parent in Path(executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


class UpdateStatus(BaseModel):
    current: str
    latest: str = ""
    available: bool = False
    notes: str = ""
    published: str = ""
    # idle, checking, downloading, ready (verified, waiting for a restart), installing, error
    state: str = "idle"
    progress: float = 0.0
    detail: str = ""
    # Whether this app can replace itself; when not, why.
    can_install: bool = False
    install_note: str = ""
    checked_at: float = 0.0
    dismissed: str = ""


def run(*command: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, timeout=timeout)


def verify_app(app: Path, expected_version: str) -> str | None:
    """None if `app` may replace this one; otherwise what is wrong with it."""
    if not (app / "Contents" / "Info.plist").is_file():
        return "the download does not contain an app"
    signed = run("codesign", "--verify", "--deep", "--strict", f"-R={REQUIREMENT}", str(app))
    if signed.returncode != 0:
        return "it is not signed with this project's Developer ID, or was changed after signing"
    if run("spctl", "--assess", "--type", "execute", str(app)).returncode != 0:
        return "Apple has not notarized it"
    try:
        version = (app / "Contents" / "Resources" / "app" / "VERSION").read_text().strip()
    except OSError:
        version = ""
    if version != expected_version:
        return f"it says it is {version or 'an unknown version'}, not {expected_version}"
    return None


HELPER = """#!/bin/bash
# Replaces Agent Office once it has quit, then opens the new one. Written by the app.
launcher="$1"; current="$2"; new="$3"; previous="$4"; reopen="$5"
echo "$(date) waiting for the app (pid $launcher) to quit"
for _ in $(seq 240); do kill -0 "$launcher" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$launcher" 2>/dev/null; then echo "the app did not quit; nothing was changed"; exit 1; fi
rm -rf "$previous"; mkdir -p "$(dirname "$previous")"
mv "$current" "$previous" || { echo "could not move the old app aside; nothing was changed"; exit 1; }
if ! mv "$new" "$current"; then
  echo "could not put the new app in place; restoring the old one"
  mv "$previous" "$current"; [ "$reopen" = yes ] && open "$current"; exit 1
fi
echo "$(date) installed"
if [ "$reopen" = yes ] && ! open "$current"; then
  echo "the new app did not open; restoring the old one"
  rm -rf "$current"; mv "$previous" "$current"; open "$current"; exit 1
fi
"""


class Updater:
    def __init__(
        self,
        settings: Settings,
        version: str | None = None,
        executable: str = sys.executable,
        launcher_pid: int | None = None,
        busy: Callable[[], str | None] = lambda: None,
        releases_url: str = RELEASES_URL,
    ) -> None:
        self._settings = settings
        self._releases_url = releases_url
        self.version = version or running_version()
        self.bundle = bundle_of(executable)
        # The app's launcher (a shell script): it powers the VMs off after the window closes.
        self.launcher_pid = launcher_pid
        # Says why updating now would interrupt something (an agent at work), or None.
        self._busy = busy
        # Set by the app window: closes it, which quits the app.
        self.quit_app: Callable[[], None] | None = None
        # Open the new version once it is in place (tests switch this off).
        self.reopen = os.environ.get("AGENT_OFFICE_UPDATE_REOPEN", "1") != "0"
        self._status = UpdateStatus(current=self.version)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._load_state()

    # --- state kept between starts -------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._settings.home / "updates"

    def _state_file(self) -> Path:
        return self.directory / "state.json"

    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_file().read_text())
        except (OSError, ValueError):
            state = {}
        self._etag = str(state.get("etag", ""))
        self._release: dict[str, Any] = state.get("release") or {}
        self._status.checked_at = float(state.get("checked_at", 0))
        self._status.dismissed = str(state.get("dismissed", ""))
        self._apply_release()

    def _save_state(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._state_file().write_text(json.dumps({
            "etag": self._etag, "release": self._release,
            "checked_at": self._status.checked_at, "dismissed": self._status.dismissed}))

    def _apply_release(self) -> None:
        tag = str(self._release.get("tag_name", ""))
        self._status.latest = tag
        self._status.available = newer(tag, self.version) if tag else False
        self._status.notes = str(self._release.get("body", ""))[:20_000]
        self._status.published = str(self._release.get("published_at", ""))

    # --- what the user sees ------------------------------------------------------------------

    def install_problem(self) -> str | None:
        """Why this copy of the app cannot replace itself, or None."""
        if not is_release(self.version):
            return "This is a local build: it does not update itself. Download releases from GitHub."
        if self.bundle is None or self.quit_app is None:
            return "Updating works in the installed app only."
        if "/AppTranslocation/" in str(self.bundle):
            return "Move Agent Office to the Applications folder first, then update."
        if not os.access(self.bundle.parent, os.W_OK) or not os.access(self.bundle, os.W_OK):
            return f"This Mac does not allow changing {self.bundle.parent}: install the new version by hand."
        return None

    def status(self) -> UpdateStatus:
        problem = self.install_problem()
        self._status.can_install = problem is None
        self._status.install_note = problem or ""
        return self._status.model_copy()

    def dismiss(self) -> UpdateStatus:
        self._status.dismissed = self._status.latest
        self._save_state()
        return self.status()

    def check_due(self) -> bool:
        return time.time() - self._status.checked_at > CHECK_EVERY_SECONDS

    # --- checking ------------------------------------------------------------------------

    async def check(self, quiet: bool = True) -> UpdateStatus:
        """Ask GitHub for the latest release. Failures stay quiet unless the user asked."""
        async with self._lock:
            if self._status.state in ("downloading", "installing"):
                return self.status()
            headers = {"Accept": "application/vnd.github+json", "User-Agent": f"AgentOffice/{self.version}"}
            if self._etag:
                headers["If-None-Match"] = self._etag
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    response = await client.get(self._releases_url, headers=headers)
            except httpx.HTTPError as exc:
                return self._check_failed(f"GitHub could not be reached ({type(exc).__name__})", quiet)
            if response.status_code in (403, 429):
                return self._check_failed("GitHub is limiting requests from this network; try again later", quiet)
            if response.status_code == 304:
                pass  # nothing new since the last check
            elif response.status_code == 200:
                release = response.json()
                if release.get("draft") or release.get("prerelease"):
                    return self._check_failed("the latest release is not final", quiet)
                self._release = {key: release.get(key) for key in ("tag_name", "body", "published_at", "assets")}
                self._etag = response.headers.get("ETag", "")
            else:
                return self._check_failed(f"GitHub answered with HTTP {response.status_code}", quiet)
            self._status.checked_at = time.time()
            if self._status.state == "error":
                self._status.state, self._status.detail = "idle", ""
            self._apply_release()
            self._save_state()
            log.info("update check", extra={"latest": self._status.latest, "available": self._status.available})
            return self.status()

    def _check_failed(self, reason: str, quiet: bool) -> UpdateStatus:
        log.info("update check failed", extra={"reason": reason})
        if not quiet:
            self._status.state, self._status.detail = "error", f"Could not check for updates: {reason}."
        return self.status()

    # --- downloading and verifying -------------------------------------------------------

    def _asset(self, name: str) -> str | None:
        for asset in self._release.get("assets") or []:
            if asset.get("name") == name:
                return str(asset.get("browser_download_url"))
        return None

    def start_download(self) -> UpdateStatus:
        status = self.status()
        if not status.available or not status.can_install or status.state in ("downloading", "ready", "installing"):
            return status
        self._status.state, self._status.progress, self._status.detail = "downloading", 0.0, ""
        self._task = asyncio.create_task(self._download())
        return self.status()

    async def _download(self) -> None:
        tag = self._status.latest
        version = tag.removeprefix("v")
        zip_name = f"Agent-Office-{version}-macos-arm64.zip"
        zip_url, sum_url = self._asset(zip_name), self._asset(zip_name + ".sha256")
        target = self.directory / tag
        try:
            if not zip_url or not sum_url:
                raise RuntimeError(f"the release has no {zip_name} with its checksum")
            if shutil.disk_usage(self._settings.home).free < FREE_BYTES_NEEDED:
                raise RuntimeError("not enough free disk space (1 GB needed)")
            shutil.rmtree(target, ignore_errors=True)
            target.mkdir(parents=True)
            archive = target / zip_name
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                expected = (await client.get(sum_url)).text.split()[0].lower()
                async with client.stream("GET", zip_url) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("Content-Length") or 0)
                    digest, received = hashlib.sha256(), 0
                    with archive.open("wb") as file:
                        async for chunk in response.aiter_bytes(1 << 20):
                            file.write(chunk)
                            digest.update(chunk)
                            received += len(chunk)
                            self._status.progress = received / total if total else 0.0
            if digest.hexdigest() != expected:
                raise RuntimeError("the download is damaged (its checksum does not match)")
            self._status.detail = "Checking the download…"
            unpack = await asyncio.to_thread(run, "ditto", "-x", "-k", str(archive), str(target / "unpacked"))
            archive.unlink(missing_ok=True)
            if unpack.returncode != 0:
                raise RuntimeError("the download could not be unpacked")
            app = target / "unpacked" / "Agent Office.app"
            problem = await asyncio.to_thread(verify_app, app, tag)
            if problem:
                raise RuntimeError(f"the new version was not installed: {problem}")
            self._status.state, self._status.progress = "ready", 1.0
            self._status.detail = f"{tag} is downloaded and verified. Restart to update."
            log.info("update ready", extra={"version": tag})
        except Exception as exc:  # noqa: BLE001 - the status must say what happened
            shutil.rmtree(target, ignore_errors=True)
            self._status.state, self._status.detail = "error", f"Update failed: {exc}"
            log.warning("update failed", extra={"version": tag, "error": str(exc)[:300]})

    # --- installing ----------------------------------------------------------------------

    def install(self) -> UpdateStatus:
        """Start the helper that swaps the app, then quit. The helper waits for the quit."""
        status = self.status()
        if status.state != "ready":
            return status
        busy = self._busy()
        if busy:
            self._status.detail = f"Not now: {busy}. Update when that is done."
            return self.status()
        problem = self.install_problem()
        if problem or self.bundle is None or self.launcher_pid is None or self.quit_app is None:
            self._status.state, self._status.detail = "error", problem or "Updating works in the installed app only."
            return self.status()
        new_app = self.directory / self._status.latest / "unpacked" / "Agent Office.app"
        helper = self.directory / "install-update.sh"
        helper.write_text(HELPER)
        helper.chmod(0o700)
        with (self.directory / "update.log").open("a") as output:
            subprocess.Popen(
                ["/bin/bash", str(helper), str(self.launcher_pid), str(self.bundle), str(new_app),
                 str(self.directory / "previous" / self.bundle.name), "yes" if self.reopen else "no"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            )
        self._status.state, self._status.detail = "installing", "Restarting into the new version…"
        log.info("update installing", extra={"version": self._status.latest})
        self.quit_app()
        return self.status()
