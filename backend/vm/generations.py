"""Versions of the system image, so it can be rebuilt while agents' computers use it.

Every agent's disk is a thin qcow2 layer over a system image, which it names by
its full path. Replacing that file would corrupt every agent. So a rebuild
writes a new file beside it (debian-desktop-<UTC time>.qcow2), and a small
pointer file (debian-desktop.current) says which one new computers start from.
Existing disks stay on the image they were made on until their agent's computer
is updated; an image no disk uses any more is removed.

Without a pointer file, the current image is debian-desktop.qcow2, as before.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from backend.config import Settings
from backend.logging_config import get_logger

log = get_logger("vm")

# debian-desktop, or debian-desktop-20261126T101500Z for a rebuilt one.
VERSION = re.compile(r"^(?P<image>[a-z0-9][a-z0-9._-]{0,40}?)(-(?P<stamp>\d{8}T\d{6}Z))?$")


def pointer_path(settings: Settings, image: str) -> Path:
    return settings.images_dir / f"{image}.current"


def current_version(settings: Settings, image: str) -> str:
    """The file name (without .qcow2) of the image new computers start from."""
    try:
        name = pointer_path(settings, image).read_text().strip()
    except OSError:
        return image
    match = VERSION.match(name)
    if not match or match.group("image") != image:
        log.warning("ignoring a damaged image pointer", extra={"image": image})
        return image
    return name


def new_version(image: str, now: datetime | None = None) -> str:
    return f"{image}-{(now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}"


def set_current(settings: Settings, image: str, version: str) -> None:
    """Point new computers at `version`, atomically."""
    pointer = pointer_path(settings, image)
    partial = pointer.with_suffix(".current.partial")
    partial.write_text(version + "\n")
    os.replace(partial, pointer)


def backing_file(disk: Path, qemu_img: str = "qemu-img") -> Path | None:
    """The image an agent's disk is a layer over, read from the disk itself.

    -U (force share): QEMU locks the disk of a running computer, and only the
    header is read here.
    """
    try:
        result = subprocess.run(
            [qemu_img, "info", "-U", "--output=json", str(disk)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        info = json.loads(result.stdout)
        name = info.get("full-backing-filename") or info.get("backing-filename")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return Path(name) if name else None


def agent_disks(settings: Settings) -> list[Path]:
    """Every agent disk, including one kept aside while its agent is being updated."""
    return sorted(settings.agents_dir.glob("*/disk*.qcow2"))


def versions(settings: Settings, image: str) -> list[Path]:
    """The image files of every version of `image`, oldest first."""
    found = []
    for path in settings.images_dir.glob(f"{image}*.qcow2"):
        match = VERSION.match(path.stem)
        if match and match.group("image") == image:
            found.append(path)
    return sorted(found, key=lambda p: (p.stem != image, p.stem))


def remove_unused(settings: Settings, image: str, qemu_img: str = "qemu-img") -> list[str]:
    """Delete versions that are neither current nor under any agent's disk.

    Only call this while no build is running: a build's finished file exists
    for a moment before it becomes current.
    """
    current = settings.images_dir / f"{current_version(settings, image)}.qcow2"
    in_use: set[Path] = set()
    for disk in agent_disks(settings):
        base = backing_file(disk, qemu_img)
        if base is None:
            # Unreadable: keep everything rather than guess.
            log.warning("cannot tell which image a disk uses; keeping all images", extra={"disk": str(disk)})
            return []
        in_use.add(base.resolve())
    removed = []
    for path in versions(settings, image):
        if path.resolve() == current.resolve() or path.resolve() in in_use:
            continue
        # Only the directory entry: a read-only image (or a link to one) needs no chmod to go.
        path.unlink(missing_ok=True)
        (settings.images_dir / f"{path.stem}.source.json").unlink(missing_ok=True)
        (settings.secrets_dir / f"{path.stem}.login-password").unlink(missing_ok=True)
        removed.append(path.stem)
        log.info("unused system image removed", extra={"image": path.stem})
    return removed
