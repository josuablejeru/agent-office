"""Builds the base image from inside the app, so first-time setup needs no terminal."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backend.config import Settings
from backend.logging_config import get_logger

log = get_logger("vm")

BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create-base-image.sh"
DEFAULT_IMAGE = "debian-desktop"
# Download, working copy and finished image exist side by side during a build.
BUILD_FREE_BYTES = 12 * 1024**3


class BaseImageStatus(BaseModel):
    ready: bool
    building: bool
    # Last line the build printed, or the reason it failed.
    detail: str = ""
    # Where the image agents boot from came from, for tracing it back.
    source: str = ""


class BaseImageBuilder:
    def __init__(
        self,
        settings: Settings,
        script: Path = BUILD_SCRIPT,
        certificates: Callable[[], Awaitable[str]] | None = None,
        pip_index_url: Callable[[], Awaitable[str]] | None = None,
        base_image: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._settings = settings
        self._script = script
        self._certificates = certificates
        self._pip_index_url = pip_index_url
        # Returns (ImageInfo, curl credentials) for a company base image, or None.
        self._base_image = base_image
        self._task: asyncio.Task[None] | None = None
        self._detail = ""

    @property
    def log_path(self) -> Path:
        return self._settings.images_dir / "build.log"

    @property
    def source_path(self) -> Path:
        return self._settings.images_dir / f"{DEFAULT_IMAGE}.source.json"

    def source(self) -> str:
        """Where the current image was built from, in words."""
        try:
            record = json.loads(self.source_path.read_text())
        except (OSError, ValueError):
            return ""
        built = f", built {record['built_at']}" if record.get("built_at") else ""
        return f"{record.get('from', '?')} ({record.get('checksum', '')[:19]}…{built})"

    def status(self) -> BaseImageStatus:
        ready = self._settings.base_image_path(DEFAULT_IMAGE).exists()
        return BaseImageStatus(
            ready=ready,
            building=self._task is not None and not self._task.done(),
            detail=self._detail,
            source=self.source() if ready else "",
        )

    def log_tail(self, lines: int = 80) -> list[str]:
        """The end of the last build's output, for the setup banner."""
        try:
            # Bytes, not read_text(): that would turn each carriage return into a line.
            text = self.log_path.read_bytes().decode(errors="replace")
        except OSError:
            return []
        # curl redraws its progress line with carriage returns: keep only the final state.
        cleaned = [line.rsplit("\r", 1)[-1].rstrip() for line in text.split("\n")]
        return [line for line in cleaned if line.strip()][-lines:]

    def start(self) -> BaseImageStatus:
        current = self.status()
        if not current.ready and not current.building:
            free = shutil.disk_usage(self._settings.home).free
            if free < BUILD_FREE_BYTES:
                self._detail = (
                    f"Not enough disk space: {free / 1024**3:.1f} GB free, "
                    f"{BUILD_FREE_BYTES // 1024**3} GB needed."
                )
                return self.status()
            self._detail = "Starting…"
            self._task = asyncio.create_task(self._build())
        return self.status()

    def start_import(self, source: str) -> BaseImageStatus:
        """Use an image built elsewhere (another Mac), instead of building one here.

        Problems with the file are reported at once, in the status; the copy itself
        runs in the background like a build.
        """
        current = self.status()
        if current.ready or current.building:
            return current
        problem = self._check_import(Path(source).expanduser())
        if problem:
            self._detail = problem
            return self.status()
        self._detail = "Copying the image…"
        self._task = asyncio.create_task(self._import(Path(source).expanduser()))
        return self.status()

    def _check_import(self, source: Path) -> str | None:
        if not source.is_file():
            return f"Import failed: {source} is not a file."
        if any(self._settings.home.glob("agents/*/disk.qcow2")):
            return "Import failed: agents' disks already depend on an image here."
        try:
            info = json.loads(subprocess.run(
                ["qemu-img", "info", "--output=json", str(source)],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return f"Import failed: {source.name} is not a disk image QEMU can read."
        if info.get("format") != "qcow2":
            return f"Import failed: {source.name} is a {info.get('format', 'unknown')} file, not a qcow2 image."
        if info.get("backing-filename"):
            # An agent's own disk: only the differences from a base image, useless alone.
            return f"Import failed: {source.name} is an agent's disk, not a system image."
        free = shutil.disk_usage(self._settings.home).free
        if free < source.stat().st_size + 2 * 1024**3:
            return f"Import failed: not enough disk space ({free / 1024**3:.1f} GB free)."
        return None

    async def _import(self, source: Path) -> None:
        target = self._settings.base_image_path(DEFAULT_IMAGE)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".importing")
        try:
            partial.unlink(missing_ok=True)
            # A clone on APFS: instant, and no extra space until either copy changes.
            cloned = await asyncio.to_thread(
                subprocess.run, ["cp", "-c", str(source), str(partial)], capture_output=True
            )
            if cloned.returncode != 0:
                await asyncio.to_thread(shutil.copyfile, source, partial)
            partial.chmod(0o444)
            os.replace(partial, target)
            self._detail = ""
            log.info("base image imported", extra={"source": source.name})
        except Exception as exc:  # noqa: BLE001 - the status must end up saying what happened
            partial.unlink(missing_ok=True)
            self._detail = f"Import failed: {exc}"
            log.exception("base image import failed")

    async def _build(self) -> None:
        try:
            await self._run_script()
        except Exception as exc:  # noqa: BLE001 - the status must end up saying what happened
            self._detail = f"Build failed: {exc}"
            log.exception("base image build crashed")

    async def _run_script(self) -> None:
        log.info("base image build started")
        env = {**os.environ, "AGENT_OFFICE_HOME": str(self._settings.home), "IMAGE_NAME": DEFAULT_IMAGE}
        # Behind a company's HTTPS inspection, the downloads need the company's root too.
        bundle = await self._certificates() if self._certificates else ""
        if bundle:
            extra = self._settings.images_dir / "extra-ca.pem"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text(bundle)
            env["EXTRA_CA_FILE"] = str(extra)
        # Python packages for the agent software come through the company repository, if any.
        index = await self._pip_index_url() if self._pip_index_url else ""
        if index:
            env["PIP_INDEX_URL"] = index
        # A company base image from Artifactory instead of Debian's public one.
        credentials_file: Path | None = None
        if self._base_image:
            from backend.net.packages import ImageError

            try:
                chosen = await self._base_image()
            except ImageError as exc:
                self._detail = f"Build failed: {exc}"
                self.log_path.write_text(f"error: {exc}\n")
                return
            if chosen:
                image, credentials = chosen
                credentials_file = self._settings.images_dir / ".artifactory-curl"
                fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(credentials)
                env.update({
                    "BASE_IMAGE_URL": image.url, "BASE_IMAGE_SHA256": image.sha256,
                    "BASE_IMAGE_NAME": image.path, "BASE_IMAGE_CURL_CONFIG": str(credentials_file),
                })
        try:
            await self._run_build(env)
        finally:
            if credentials_file is not None:
                credentials_file.unlink(missing_ok=True)

    async def _run_build(self, env: dict[str, str]) -> None:
        with self.log_path.open("w") as build_log:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash", str(self._script),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdout is not None
            # curl redraws its progress with carriage returns; treat those as line ends.
            buffer = b""
            while chunk := await process.stdout.read(4096):
                build_log.write(chunk.decode(errors="replace"))
                buffer = (buffer + chunk).replace(b"\r", b"\n")
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if text := line.decode(errors="replace").strip():
                        self._detail = text[:200]
            code = await process.wait()
        if code != 0:
            # The script's own explanation, rather than whatever printed last while it cleaned up.
            errors = [line for line in self.log_tail(lines=400) if line.startswith("error: ")]
            reason = errors[-1].removeprefix("error: ") if errors else self._detail
            self._detail = f"Build failed: {reason}"
        log.info("base image build finished", extra={"exit_code": code})
