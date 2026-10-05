"""Detects whether this Mac can run QEMU ARM64 guests with HVF acceleration.

Run directly to print a report:  python -m backend.vm.capabilities
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel

QEMU_SYSTEM_BINARY = "qemu-system-aarch64"
QEMU_IMG_BINARY = "qemu-img"
FIRMWARE_FILENAME = "edk2-aarch64-code.fd"

Which = Callable[[str], str | None]
Runner = Callable[..., Awaitable[str | None]]


class Capabilities(BaseModel):
    system: str
    machine: str
    apple_silicon: bool
    hvf_supported: bool
    qemu_system_path: str | None
    qemu_version: str | None
    qemu_img_path: str | None
    hvf_accel_available: bool
    firmware_path: str | None
    ready: bool
    problems: list[str]


async def run_command(*args: str, timeout: float = 5.0) -> str | None:
    """Run a command and return its stdout, or None if it fails for any reason."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip()


def find_firmware(qemu_system_path: str | None) -> str | None:
    """Locate the EDK2 UEFI firmware shipped alongside QEMU."""
    candidates: list[Path] = []
    if qemu_system_path:
        prefix = Path(qemu_system_path).resolve().parent.parent
        candidates.append(prefix / "share" / "qemu" / FIRMWARE_FILENAME)
        # Homebrew symlinks the binary; the unresolved prefix also has share/qemu.
        candidates.append(Path(qemu_system_path).parent.parent / "share" / "qemu" / FIRMWARE_FILENAME)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


async def detect_capabilities(
    which: Which = shutil.which,
    run: Runner = run_command,
    system: str | None = None,
    machine: str | None = None,
) -> Capabilities:
    """Probe the host. Never raises: anything missing is reported in `problems`."""
    system = system or platform.system()
    machine = machine or platform.machine()
    problems: list[str] = []

    apple_silicon = system == "Darwin" and machine == "arm64"
    if not apple_silicon:
        problems.append(f"Host is {system}/{machine}; Apple Silicon macOS is required.")

    hvf_supported = apple_silicon and (await run("sysctl", "-n", "kern.hv_support")) == "1"
    if apple_silicon and not hvf_supported:
        problems.append("Hypervisor.framework is unavailable (sysctl kern.hv_support != 1).")

    qemu_system_path = which(QEMU_SYSTEM_BINARY)
    qemu_img_path = which(QEMU_IMG_BINARY)
    qemu_version: str | None = None
    hvf_accel_available = False
    if qemu_system_path is None:
        problems.append(f"{QEMU_SYSTEM_BINARY} not found. Install it with: brew install qemu")
    else:
        version_output = await run(qemu_system_path, "--version")
        qemu_version = version_output.splitlines()[0] if version_output else None
        accel_output = await run(qemu_system_path, "-accel", "help") or ""
        hvf_accel_available = "hvf" in accel_output.split()
        if not hvf_accel_available:
            problems.append(f"{QEMU_SYSTEM_BINARY} was built without the hvf accelerator.")
    if qemu_img_path is None:
        problems.append(f"{QEMU_IMG_BINARY} not found. Install it with: brew install qemu")

    firmware_path = find_firmware(qemu_system_path)
    if qemu_system_path is not None and firmware_path is None:
        problems.append(f"UEFI firmware {FIRMWARE_FILENAME} not found next to QEMU.")

    return Capabilities(
        system=system,
        machine=machine,
        apple_silicon=apple_silicon,
        hvf_supported=hvf_supported,
        qemu_system_path=qemu_system_path,
        qemu_version=qemu_version,
        qemu_img_path=qemu_img_path,
        hvf_accel_available=hvf_accel_available,
        firmware_path=firmware_path,
        ready=not problems,
        problems=problems,
    )


def main() -> int:
    caps = asyncio.run(detect_capabilities())
    print(caps.model_dump_json(indent=2))
    return 0 if caps.ready else 1


if __name__ == "__main__":
    sys.exit(main())
