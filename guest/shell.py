"""shell.exec: run a command as the agent user with a timeout and capped output."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

from guest.validation import OperationError, optional_number, optional_str, require_str

MAX_COMMAND_LENGTH = 32_000
MAX_OUTPUT_BYTES = 16_000
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 600.0


async def _read_capped(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    """Read a stream to its end, keeping only the first `limit` bytes."""
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(65536):
        room = limit - len(kept)
        kept += chunk[:room]
        truncated = truncated or len(chunk) > room
    return kept.decode(errors="replace"), truncated


async def shell_exec(args: dict[str, Any]) -> dict[str, Any]:
    command = require_str(args, "command", MAX_COMMAND_LENGTH)
    timeout = optional_number(args, "timeout", DEFAULT_TIMEOUT_SECONDS, 1, MAX_TIMEOUT_SECONDS)
    cwd = Path(optional_str(args, "cwd", 4096) or "~").expanduser()
    if not cwd.is_dir():
        raise OperationError(f"cwd '{cwd}' is not a directory")

    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", "-lc", command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own session, so a timeout can kill everything the command spawned.
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    readers = asyncio.gather(
        _read_capped(proc.stdout, MAX_OUTPUT_BYTES), _read_capped(proc.stderr, MAX_OUTPUT_BYTES)
    )
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(readers), timeout=timeout)
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None and timed_out:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    (stdout, stdout_truncated), (stderr, stderr_truncated) = await readers
    await proc.wait()
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }
