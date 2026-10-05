"""qcow2 image helpers: per-agent overlays backed by a shared base image."""

from __future__ import annotations

from pathlib import Path

from backend.vm.capabilities import QEMU_IMG_BINARY


def build_overlay_command(
    base_image: Path, overlay: Path, qemu_img_binary: str = QEMU_IMG_BINARY
) -> list[str]:
    """Return the argv creating `overlay` as a copy-on-write layer over `base_image`."""
    return [
        qemu_img_binary,
        "create",
        "-f", "qcow2",
        "-b", str(base_image),
        "-F", "qcow2",
        str(overlay),
    ]


def build_snapshot_command(
    disk: Path, name: str, qemu_img_binary: str = QEMU_IMG_BINARY
) -> list[str]:
    """Return the argv creating an internal snapshot of a stopped VM's disk."""
    return [qemu_img_binary, "snapshot", "-c", name, str(disk)]
