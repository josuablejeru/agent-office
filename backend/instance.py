"""Ensures only one backend manages a data directory at a time.

Two backends on the same directory would both drive the same VMs, and one
quitting would power off VMs the other is using.
"""

from __future__ import annotations

import fcntl
import os
from typing import IO

from backend.config import Settings

LOCK_FILENAME = "backend.lock"


class AlreadyRunning(Exception):
    pass


def acquire_instance_lock(settings: Settings) -> IO[str]:
    """Take the lock; keep the returned file open for as long as it should be held."""
    settings.home.mkdir(parents=True, exist_ok=True)
    handle = open(settings.home / LOCK_FILENAME, "a+")  # noqa: SIM115 - held for the process lifetime
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AlreadyRunning(
            f"Another Agent Office is already using {settings.home}. Quit it first."
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def is_running(settings: Settings) -> bool:
    """True if some backend currently holds the lock. Only looks; changes nothing."""
    path = settings.home / LOCK_FILENAME
    if not path.exists():
        return False
    with open(path) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False
