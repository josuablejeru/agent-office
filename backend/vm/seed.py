"""Per-agent seed disk: cloud-init identity plus the guest daemon and its secret.

The guest mounts this disk on every boot (see scripts/create-base-image.sh) and
installs the daemon code and secret from it, so the host stays the source of
truth for both.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from backend.vm.process import run_checked

GUEST_USER = "agent"
SEED_VOLUME_LABEL = "cidata"
SEED_SECRET_FILENAME = "agent-secret"
SEED_GUEST_DIRNAME = "guest"
GUEST_SOURCE_DIR = Path(__file__).resolve().parents[2] / "guest"


def render_meta_data(agent_name: str) -> str:
    return yaml.safe_dump(
        {"instance-id": f"agent-office-{agent_name}", "local-hostname": agent_name},
        sort_keys=False,
    )


HOST_DNS_FILENAME = "host_dns.json"


def stage_seed(
    staging: Path,
    agent_name: str,
    secret: str,
    guest_source: Path,
    dns: dict[str, Any] | None = None,
) -> None:
    """Lay out the seed disk contents in `staging`.

    `dns` tells the guest how to reach the host's DNS forwarder; without it the
    guest keeps (or returns to) its ordinary DNS.
    """
    staging.mkdir(parents=True)
    (staging / "meta-data").write_text(render_meta_data(agent_name))
    (staging / "user-data").write_text("#cloud-config\n{}\n")
    (staging / SEED_SECRET_FILENAME).write_text(secret + "\n")
    guest_dir = staging / SEED_GUEST_DIRNAME
    guest_dir.mkdir()
    for source in sorted(guest_source.glob("*.py")):
        shutil.copyfile(source, guest_dir / source.name)
    if dns is not None:
        (guest_dir / HOST_DNS_FILENAME).write_text(json.dumps(dns))


async def build_seed_iso(
    iso_path: Path,
    agent_name: str,
    secret: str,
    guest_source: Path = GUEST_SOURCE_DIR,
    dns: dict[str, Any] | None = None,
) -> None:
    """Write the seed ISO. macOS ships hdiutil, so no extra tooling is required."""
    staging = iso_path.parent / "seed"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        stage_seed(staging, agent_name, secret, guest_source, dns)
        iso_path.unlink(missing_ok=True)
        await run_checked(
            "hdiutil", "makehybrid", "-iso", "-joliet",
            "-default-volume-name", SEED_VOLUME_LABEL,
            "-o", str(iso_path), str(staging),
        )
        iso_path.chmod(0o600)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
