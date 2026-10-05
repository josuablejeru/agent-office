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
# How long output is collected after the command itself has finished.
OUTPUT_GRACE_SECONDS = 0.5


class _Capture:
    """Collects a stream, keeping only the first `limit` bytes. Readable at any time."""

    def __init__(self, stream: asyncio.StreamReader, limit: int) -> None:
        self._stream, self._limit = stream, limit
        self._kept = bytearray()
        self.truncated = False
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        while chunk := await self._stream.read(65536):
            room = self._limit - len(self._kept)
            self._kept += chunk[:room]
            self.truncated = self.truncated or len(chunk) > room

    @property
    def text(self) -> str:
        return self._kept.decode(errors="replace")


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
    out, err = _Capture(proc.stdout, MAX_OUTPUT_BYTES), _Capture(proc.stderr, MAX_OUTPUT_BYTES)
    # Wait for the command itself, not for its output to end: something it left
    # running in the background (a server started with `&`) keeps the pipes open,
    # and asyncio's own wait() would then wait for that too.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while proc.returncode is None and loop.time() < deadline:  # noqa: ASYNC110 - see above
        await asyncio.sleep(0.02)
    timed_out = proc.returncode is None
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # Collect what was printed, then stop listening even if something still holds the pipes.
    _, listening = await asyncio.wait({out.task, err.task}, timeout=OUTPUT_GRACE_SECONDS)
    for reader in listening:
        reader.cancel()
    await asyncio.gather(*listening, return_exceptions=True)
    if listening or timed_out:
        # Let go of our ends of the pipes; otherwise each such command leaks two descriptors.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
    stdout, stdout_truncated = out.text, out.truncated
    stderr, stderr_truncated = err.text, err.truncated
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
    }
