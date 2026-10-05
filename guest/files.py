"""file.read / file.write / file.list for the guest daemon."""

from __future__ import annotations

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


async def file_read(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)
    limit = int(optional_number(args, "max_bytes", DEFAULT_READ_BYTES, 1, MAX_READ_BYTES))
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(limit)
    except OSError as exc:
        raise OperationError(f"cannot read {path}: {exc.strerror}") from exc
    return {
        "path": str(path),
        "content": data.decode(errors="replace"),
        "size": size,
        "truncated": size > len(data),
    }


async def file_write(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise OperationError("'content' must be a string")
    if len(content) > MAX_WRITE_CHARS:
        raise OperationError(f"'content' is longer than {MAX_WRITE_CHARS} characters")
    append = optional_bool(args, "append", False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise OperationError(f"cannot write {path}: {exc.strerror}") from exc
    return {"path": str(path), "bytes_written": len(content.encode())}


async def file_list(args: dict[str, Any]) -> dict[str, Any]:
    path = _resolve(args)
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
