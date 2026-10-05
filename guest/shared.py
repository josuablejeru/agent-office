"""The Shared folder: how files travel between the user and the agent's computer.

Files move in pieces, because one WebSocket message cannot hold a large file.
An upload is written to a hidden partial file and only takes its real name once
its checksum has been confirmed, so a broken transfer never leaves a damaged
file where the agent would find it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
from typing import Any

from guest.validation import OperationError, optional_number, require_str

SHARED_DIR = Path.home() / "Shared"
MAX_FILE_BYTES = 25 * 1024**2
MAX_CHUNK_BYTES = 1024**2
MAX_LISTED = 200
MAX_NAME_LENGTH = 200
PARTIAL_SUFFIX = ".partial"


def ensure_shared_dir() -> None:
    SHARED_DIR.mkdir(exist_ok=True)


def _inside(path: Path) -> Path:
    """Resolve a path and insist it stays in the Shared folder (no .., no symlink escapes)."""
    root = SHARED_DIR.resolve()
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise OperationError("that path is outside the Shared folder")
    return resolved


def _upload_target(args: dict[str, Any]) -> Path:
    name = require_str(args, "name", MAX_NAME_LENGTH)
    if name != os.path.basename(name) or name.startswith(".") or name in ("", ".", ".."):
        raise OperationError("a file name may not contain folders or start with a dot")
    return _inside(SHARED_DIR / name)


def _existing(args: dict[str, Any]) -> Path:
    path = _inside(SHARED_DIR / require_str(args, "path", 1000))
    if not path.is_file():
        raise OperationError("there is no such file in the Shared folder")
    return path


def _partial(target: Path) -> Path:
    return target.with_name(f".{target.name}{PARTIAL_SUFFIX}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


async def shared_list(args: dict[str, Any]) -> dict[str, Any]:
    ensure_shared_dir()
    files = []
    for path in sorted(SHARED_DIR.rglob("*")):
        relative = path.relative_to(SHARED_DIR)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        try:
            _inside(path)  # a link pointing out of the folder is not offered
        except OperationError:
            continue
        stat = path.stat()
        files.append({"path": str(relative), "size": stat.st_size, "modified": int(stat.st_mtime)})
    files.sort(key=lambda entry: entry["modified"], reverse=True)
    return {"files": files[:MAX_LISTED], "truncated": len(files) > MAX_LISTED}


async def shared_write_chunk(args: dict[str, Any]) -> dict[str, Any]:
    ensure_shared_dir()
    partial = _partial(_upload_target(args))
    offset = int(optional_number(args, "offset", 0, 0, MAX_FILE_BYTES))
    encoded = args.get("data")
    if not isinstance(encoded, str) or len(encoded) > MAX_CHUNK_BYTES * 2:
        raise OperationError("'data' must be a base64 string of at most 1 MB of content")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationError("the chunk was not valid base64") from exc
    if len(data) > MAX_CHUNK_BYTES or offset + len(data) > MAX_FILE_BYTES:
        raise OperationError("the file is larger than the 25 MB limit")
    with partial.open("wb" if offset == 0 else "r+b") as handle:
        handle.seek(offset)
        handle.write(data)
    return {"received": offset + len(data)}


async def shared_finish(args: dict[str, Any]) -> dict[str, Any]:
    target = _upload_target(args)
    partial = _partial(target)
    expected = require_str(args, "sha256", 64)
    if not partial.is_file():
        raise OperationError("no upload is in progress for that file")
    if _sha256(partial) != expected:
        partial.unlink()
        raise OperationError("the file arrived damaged and was discarded; send it again")
    partial.replace(target)
    return {"path": target.name, "size": target.stat().st_size}


async def shared_stat(args: dict[str, Any]) -> dict[str, Any]:
    path = _existing(args)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise OperationError("the file is larger than the 25 MB limit")
    return {"size": size, "sha256": _sha256(path)}


async def shared_read_chunk(args: dict[str, Any]) -> dict[str, Any]:
    path = _existing(args)
    offset = int(optional_number(args, "offset", 0, 0, MAX_FILE_BYTES))
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(MAX_CHUNK_BYTES)
    return {"data": base64.b64encode(data).decode(), "eof": offset + len(data) >= path.stat().st_size}
