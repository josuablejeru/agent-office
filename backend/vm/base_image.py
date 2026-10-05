"""Builds the base image from inside the app, so first-time setup needs no terminal."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

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


class BaseImageBuilder:
    def __init__(self, settings: Settings, script: Path = BUILD_SCRIPT) -> None:
        self._settings = settings
        self._script = script
        self._task: asyncio.Task[None] | None = None
        self._detail = ""

    @property
    def log_path(self) -> Path:
        return self._settings.images_dir / "build.log"

    def status(self) -> BaseImageStatus:
        return BaseImageStatus(
            ready=self._settings.base_image_path(DEFAULT_IMAGE).exists(),
            building=self._task is not None and not self._task.done(),
            detail=self._detail,
        )

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

    async def _build(self) -> None:
        try:
            await self._run_script()
        except Exception as exc:  # noqa: BLE001 - the status must end up saying what happened
            self._detail = f"Build failed: {exc}"
            log.exception("base image build crashed")

    async def _run_script(self) -> None:
        log.info("base image build started")
        env = {**os.environ, "AGENT_OFFICE_HOME": str(self._settings.home), "IMAGE_NAME": DEFAULT_IMAGE}
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
            self._detail = f"Build failed: {self._detail}"
        log.info("base image build finished", extra={"exit_code": code})
