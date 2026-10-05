"""Points the VM's name resolution at the host's DNS forwarder (stdlib only).

The host writes `host_dns.json` next to this file on every start. With it, the
VM asks the host for every name, and the host answers the way the Mac itself
would, including names that only resolve through a VPN. Without it, the VM is
put back on its ordinary DNS.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("agentd")

SETTINGS_FILE = Path(__file__).with_name("host_dns.json")
RESOLVED_DROP_IN = Path("/etc/systemd/resolved.conf.d/agent-office.conf")
# The host as seen from the VM.
HOST_ADDRESS = "10.0.2.2"
DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")


def render_drop_in(port: int, search_domains: list[str]) -> str:
    """systemd-resolved settings that send every lookup to the host's forwarder."""
    domains = ["~."] + [domain for domain in search_domains if DOMAIN.match(domain)]
    return (
        "# Written by Agent Office on every start. Do not edit.\n"
        "[Resolve]\n"
        # The forwarder is the only server on purpose. With a second one listed, a
        # single failed lookup makes the resolver switch to it and stay there, and
        # internal names stop resolving until the next restart.
        f"DNS={HOST_ADDRESS}:{port}\n"
        f"Domains={' '.join(domains)}\n"
    )


def wanted_drop_in() -> str | None:
    try:
        settings = json.loads(SETTINGS_FILE.read_text())
        port = int(settings["port"])
        search = [str(domain) for domain in settings.get("search_domains", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 1024 <= port <= 65535:
        return None
    return render_drop_in(port, search)


def sudo(*command: str, stdin: str | None = None) -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", *command], input=stdin, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def configure_dns() -> None:
    """Apply (or remove) the DNS settings. Never raises: DNS setup must not stop the daemon."""
    wanted = wanted_drop_in()
    try:
        current = RESOLVED_DROP_IN.read_text()
    except OSError:
        current = None
    if wanted == current:
        return
    if wanted is None:
        changed = sudo("rm", "-f", str(RESOLVED_DROP_IN))
    else:
        changed = sudo("mkdir", "-p", str(RESOLVED_DROP_IN.parent)) and sudo(
            "tee", str(RESOLVED_DROP_IN), stdin=wanted
        )
    if changed and sudo("systemctl", "restart", "systemd-resolved"):
        log.info("dns %s", "set to use the host's view" if wanted else "reset to default")
    else:
        log.warning("dns settings could not be applied")
