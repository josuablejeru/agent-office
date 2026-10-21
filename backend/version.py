"""Which build of Agent Office is running: a release tag, or the git commit of a local build."""

from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
# Written into the app bundle by scripts/build-app.sh.
VERSION_FILE = APP_ROOT / "VERSION"


@cache
def running_version() -> str:
    """e.g. "v0.4.0" for a release, "v0.3.0-7-g1a2b3c4-dirty" for a local build, "unknown" otherwise."""
    try:
        recorded = VERSION_FILE.read_text().strip()
        if recorded:
            return recorded
    except OSError:
        pass
    try:
        # From source: the checkout itself says which commit this is.
        described = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=APP_ROOT, capture_output=True, text=True, timeout=5,
        )
        if described.returncode == 0 and described.stdout.strip():
            return described.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def is_release(version: str) -> bool:
    """A plain tag (v1.2.3, or v1.2.3-rc1), not a commit after one or a changed checkout."""
    parts = version.split("-")
    return version.startswith("v") and not version.endswith("-dirty") and not (
        len(parts) >= 3 and parts[-1].startswith("g") and parts[-2].isdigit()
    )
