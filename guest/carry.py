"""Carrying an agent's own files to a new computer when its system image is updated.

An update gives the agent a fresh disk on the new system image. What the agent
keeps across that is packed here into one archive on the old computer, fetched
by the host in pieces, sent to the new computer and unpacked there:

- the Shared folder (~/Shared),
- its databases (~/Databases),
- its memory (~/.agent-office/memory.sqlite).

SQLite files are copied with SQLite's backup, so a file being written at the
time is still copied whole. The host never opens the archive; it only checks
that what arrives is what was sent. Unpacking refuses anything outside these
three places, links that point out of them, and special files.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from guest.validation import OperationError, optional_number, require_str

HOME = Path.home()
SHARED = "Shared"
DATABASES = "Databases"
MEMORY = "memory.sqlite"
STATE_DIR = HOME / ".agent-office"
OUTGOING = STATE_DIR / "carry-out.tar.gz"
INCOMING = STATE_DIR / "carry-in.tar.gz"
MAX_CARRY_BYTES = 4 * 1024**3
CHUNK_BYTES = 2 * 1024**2
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
# Left behind by SQLite next to a database; the backup copy already holds their contents.
SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _backup(source: Path, target: Path) -> None:
    """Copy a SQLite database consistently, even while something has it open."""
    target.parent.mkdir(parents=True, exist_ok=True)
    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=10)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


def _copy_tree(source: Path, target: Path) -> None:
    """Copy a folder: databases through SQLite, other files as they are, links as links."""
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(path), destination)
        elif path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            if path.name.endswith(SQLITE_SIDECARS) and path.with_name(
                path.name.rsplit("-", 1)[0]
            ).exists():
                continue
            if path.suffix in SQLITE_SUFFIXES and _is_sqlite(path):
                _backup(path, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)


async def carry_pack(args: dict[str, Any]) -> dict[str, Any]:
    """Pack the agent's files into one archive; returns its size and SHA-256."""
    return await asyncio.to_thread(_pack)


def _pack() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTGOING.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as staging_name:
        staging = Path(staging_name)
        counts = {"shared_files": 0, "databases": 0, "memory": False}
        for name in (SHARED, DATABASES):
            if (HOME / name).is_dir():
                _copy_tree(HOME / name, staging / name)
        if (STATE_DIR / MEMORY).is_file():
            _backup(STATE_DIR / MEMORY, staging / MEMORY)
            counts["memory"] = True
        counts["shared_files"] = sum(1 for p in (staging / SHARED).rglob("*") if p.is_file()) \
            if (staging / SHARED).is_dir() else 0
        counts["databases"] = sum(1 for p in (staging / DATABASES).glob("*.sqlite")) \
            if (staging / DATABASES).is_dir() else 0
        with tarfile.open(OUTGOING, "w:gz", compresslevel=1) as archive:
            for entry in sorted(staging.iterdir()):
                archive.add(entry, arcname=entry.name)
    size = OUTGOING.stat().st_size
    if size > MAX_CARRY_BYTES:
        OUTGOING.unlink()
        raise OperationError(
            f"the files to carry over come to {size / 1024**3:.1f} GB packed; "
            f"at most {MAX_CARRY_BYTES // 1024**3} GB can be carried"
        )
    return {"size": size, "sha256": _sha256(OUTGOING), **counts}


async def carry_read_chunk(args: dict[str, Any]) -> dict[str, Any]:
    if not OUTGOING.is_file():
        raise OperationError("nothing has been packed")
    offset = int(optional_number(args, "offset", 0, 0, MAX_CARRY_BYTES))
    with OUTGOING.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(CHUNK_BYTES)
    return {"data": base64.b64encode(data).decode(), "eof": offset + len(data) >= OUTGOING.stat().st_size}


async def carry_write_chunk(args: dict[str, Any]) -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    offset = int(optional_number(args, "offset", 0, 0, MAX_CARRY_BYTES))
    encoded = args.get("data")
    if not isinstance(encoded, str) or len(encoded) > CHUNK_BYTES * 2:
        raise OperationError("'data' must be a base64 string of at most 2 MB of content")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationError("the chunk was not valid base64") from exc
    if offset + len(data) > MAX_CARRY_BYTES:
        raise OperationError("the archive is larger than the limit")
    with INCOMING.open("wb" if offset == 0 else "r+b") as handle:
        handle.seek(offset)
        handle.write(data)
    return {"received": offset + len(data)}


def _allowed(member: tarfile.TarInfo) -> bool:
    parts = Path(member.name).parts
    if not parts:
        return False
    if parts[0] in (SHARED, DATABASES):
        return True
    return member.name == MEMORY and member.isfile()


def _move_into(source: Path, target: Path) -> None:
    """Merge a carried folder into place; carried files win over ones already there."""
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir() and not path.is_symlink():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            os.replace(path, destination)


async def carry_unpack(args: dict[str, Any]) -> dict[str, Any]:
    """Check the received archive and put its files in place."""
    expected = require_str(args, "sha256", 64)
    return await asyncio.to_thread(_unpack, expected)


def _unpack(expected: str) -> dict[str, Any]:
    if not INCOMING.is_file():
        raise OperationError("no archive has been received")
    if _sha256(INCOMING) != expected:
        INCOMING.unlink()
        raise OperationError("the archive arrived damaged and was discarded")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as staging_name:
        staging = Path(staging_name)
        with tarfile.open(INCOMING, "r:gz") as archive:
            members = [m for m in archive.getmembers() if _allowed(m)]
            # "data": no absolute paths, no "..", no links leading out, no devices.
            archive.extractall(staging, members=members, filter="data")
        for name in (SHARED, DATABASES):
            if (staging / name).is_dir():
                _move_into(staging / name, HOME / name)
        if (staging / MEMORY).is_file():
            for suffix in SQLITE_SIDECARS:
                (STATE_DIR / f"{MEMORY}{suffix}").unlink(missing_ok=True)
            os.replace(staging / MEMORY, STATE_DIR / MEMORY)
    INCOMING.unlink(missing_ok=True)
    return {"unpacked": True}


async def carry_clear(args: dict[str, Any]) -> dict[str, Any]:
    OUTGOING.unlink(missing_ok=True)
    INCOMING.unlink(missing_ok=True)
    return {"cleared": True}
