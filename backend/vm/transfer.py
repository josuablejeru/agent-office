"""Moves files between the host and an agent's Shared folder, in verified pieces."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from backend.vm.guest import GuestClient, GuestError, GuestOperationError

CHUNK_BYTES = 1024**2
MAX_FILE_BYTES = 25 * 1024**2
OUTDATED_DAEMON = (
    "This agent's computer is running an older version of its software. "
    "Turn the computer off and on again to update it."
)


class TransferError(GuestError):
    pass


def explain(exc: GuestError) -> TransferError:
    """Turn a daemon error into something a user can act on."""
    if "unknown operation" in str(exc):
        return TransferError(OUTDATED_DAEMON)
    return TransferError(str(exc))


async def list_files(client: GuestClient) -> list[dict[str, object]]:
    try:
        return (await client.call("shared.list"))["files"]
    except GuestError as exc:
        raise explain(exc) from exc


async def upload(client: GuestClient, name: str, data: bytes) -> dict[str, object]:
    if len(data) > MAX_FILE_BYTES:
        raise TransferError("Files can be at most 25 MB.")
    try:
        # An empty file still needs one (empty) chunk so the partial file exists.
        for offset in range(0, max(len(data), 1), CHUNK_BYTES):
            chunk = base64.b64encode(data[offset : offset + CHUNK_BYTES]).decode()
            await client.call("shared.write_chunk", {"name": name, "offset": offset, "data": chunk})
        return await client.call(
            "shared.finish", {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
        )
    except GuestError as exc:
        raise explain(exc) from exc


async def download(client: GuestClient, path: str, destination: Path) -> int:
    """Fetch a file into `destination`, verifying it. Returns its size."""
    try:
        expected = await client.call("shared.stat", {"path": path}, timeout=60)
        digest = hashlib.sha256()
        offset = 0
        with destination.open("wb") as handle:
            while True:
                piece = await client.call("shared.read_chunk", {"path": path, "offset": offset})
                data = base64.b64decode(piece["data"])
                handle.write(data)
                digest.update(data)
                offset += len(data)
                if piece["eof"] or not data:
                    break
                if offset > MAX_FILE_BYTES:
                    raise TransferError("Files can be at most 25 MB.")
    except GuestError as exc:
        destination.unlink(missing_ok=True)
        raise explain(exc) from exc
    if offset != expected["size"] or digest.hexdigest() != expected["sha256"]:
        destination.unlink(missing_ok=True)
        raise TransferError("The file changed or was damaged while it was being fetched. Try again.")
    return offset


__all__ = ["GuestOperationError", "TransferError", "download", "explain", "list_files", "upload"]
