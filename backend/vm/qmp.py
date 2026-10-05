"""Minimal QMP (QEMU Machine Protocol) client over a unix socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from backend.vm.errors import VMError


class QMPError(VMError):
    pass


async def _read_reply(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Return the next command reply, skipping asynchronous events."""
    while True:
        line = await reader.readline()
        if not line:
            raise QMPError("QMP connection closed")
        message: dict[str, Any] = json.loads(line)
        if "error" in message:
            raise QMPError(message["error"].get("desc", "QMP command failed"))
        if "return" in message:
            return message


async def _execute(socket_path: Path, command: str) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        await reader.readline()  # greeting
        for name in ("qmp_capabilities", command):
            writer.write(json.dumps({"execute": name}).encode() + b"\n")
            await writer.drain()
            reply = await _read_reply(reader)
        return reply
    finally:
        writer.close()


async def qmp_execute(socket_path: Path, command: str, timeout: float = 5.0) -> dict[str, Any]:
    """Run one argument-less QMP command and return its reply."""
    try:
        return await asyncio.wait_for(_execute(socket_path, command), timeout=timeout)
    except TimeoutError as exc:
        raise QMPError(f"QMP command {command} timed out") from exc
    except OSError as exc:
        raise QMPError(f"QMP socket unavailable: {exc}") from exc
