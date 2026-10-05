"""file.read / file.write / file.list for the guest daemon."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

from guest.validation import (
    OperationError,
    optional_bool,
    optional_number,
    require_str,
)

MAX_PATH_LENGTH = 4096
DEFAULT_READ_BYTES = 64_000
MAX_READ_BYTES = 1_000_000
MAX_WRITE_CHARS = 1_000_000
MAX_LIST_ENTRIES = 500


def _resolve(args: dict[str, Any]) -> Path:
    """Paths are relative to the agent user's home unless absolute."""
    path = Path(require_str(args, "path", MAX_PATH_LENGTH)).expanduser()
    return path if path.is_absolute() else Path.home() / path


def _regular(path: Path, must_exist: bool) -> None:
    """Refuse pipes, devices and the like: opening one can block forever."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        if must_exist:
            raise OperationError(f"cannot read {path}: No such file or directory") from None
        return
    except OSError as exc:
        raise OperationError(f"cannot use {path}: {exc.strerror}") from exc
    if stat.S_ISDIR(mode):
        raise OperationError(f"{path} is a directory")
    if not stat.S_ISREG(mode):
        raise OperationError(f"{path} is not a regular file")


async def file_read(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)
    limit = int(optional_number(args, "max_bytes", DEFAULT_READ_BYTES, 1, MAX_READ_BYTES))
    offset = int(optional_number(args, "offset", 0, 0, 10**12))

    def work() -> dict[str, Any]:
        _regular(path, must_exist=True)
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
        except OSError as exc:
            raise OperationError(f"cannot read {path}: {exc.strerror}") from exc
        end = offset + len(data)
        result: dict[str, Any] = {
            "path": str(path),
            "content": data.decode(errors="replace"),
            "size": size,
            "truncated": end < size,
        }
        if end < size:
            result["next_offset"] = end  # pass as 'offset' to read on
        return result

    return await asyncio.to_thread(work)


async def file_write(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise OperationError("'content' must be a string")
    if len(content) > MAX_WRITE_CHARS:
        raise OperationError(f"'content' is longer than {MAX_WRITE_CHARS} characters")
    append = optional_bool(args, "append", False)

    def work() -> dict[str, Any]:
        _regular(path, must_exist=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if append else "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            raise OperationError(f"cannot write {path}: {exc.strerror}") from exc
        return {"path": str(path), "bytes_written": len(content.encode())}

    return await asyncio.to_thread(work)


async def file_list(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)

    def work() -> dict[str, Any]:
        try:
            children = sorted(path.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            raise OperationError(f"cannot list {path}: {exc.strerror}") from exc
        entries = []
        for child in children[:MAX_LIST_ENTRIES]:
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if is_dir else "file",
                    "size": None if is_dir else child.lstat().st_size,
                }
            )
        return {"path": str(path), "entries": entries, "truncated": len(children) > MAX_LIST_ENTRIES}

    return await asyncio.to_thread(work)
