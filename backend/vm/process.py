"""Subprocess helper for QEMU tooling."""

from __future__ import annotations

import asyncio

from backend.vm.errors import VMError


async def run_checked(*args: str, timeout: float = 60.0) -> str:
    """Run a command and return stdout; raise VMError with stderr if it fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise VMError(f"could not run {args[0]}: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise VMError(f"{args[0]} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise VMError(f"{args[0]} failed: {detail}")
    return stdout.decode(errors="replace")
