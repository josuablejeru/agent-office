"""Shared SQLite plumbing for the agent's memory and databases (stdlib only)."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from guest.validation import OperationError

QUERY_TIMEOUT_SECONDS = 10.0
MAX_ROWS = 200
MAX_CELL_CHARS = 2000


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    # The agent may open the same file from its own scripts at the same time.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def with_deadline(connection: sqlite3.Connection) -> None:
    """Abort any statement on this connection that runs past the deadline."""
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    connection.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)


def json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    return value


async def in_thread[T](path: Path, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run `work` on its own connection off the event loop, so one query cannot stall the daemon."""

    def run() -> T:
        connection = connect(path)
        try:
            with_deadline(connection)
            result = work(connection)
            connection.commit()
            return result
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "interrupted" in str(exc):
                raise OperationError(
                    f"the query took longer than {QUERY_TIMEOUT_SECONDS:.0f} seconds and was stopped"
                ) from exc
            raise OperationError(f"SQLite: {exc}") from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise OperationError(f"SQLite: {exc}") from exc
        finally:
            connection.close()

    return await asyncio.to_thread(run)
