"""Points pip and npm at the host's package settings (stdlib only).

Writes /etc/pip.conf (all users) and the agent's ~/.npmrc from what the host
puts into `host_packages.json` next to this file on every start: a company
repository such as Artifactory, with credentials. pip.conf also carries the
system's root certificates when the host passed extra ones (see
guest/certificates.py), because pip otherwise trusts only its own list.
Files this module wrote are removed again when the host stops sending them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from guest.certificates import SYSTEM_BUNDLE, read_bundle
from guest.network import sudo

log = logging.getLogger("agentd")

SETTINGS_FILE = Path(__file__).with_name("host_packages.json")
PIP_CONF = Path("/etc/pip.conf")
NPMRC = Path.home() / ".npmrc"
MARKER = "# Written by Agent Office on every start. Do not edit."


def read_settings(path: Path | None = None) -> dict[str, str]:
    try:
        settings = json.loads((path or SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {key: str(value) for key, value in settings.items() if isinstance(value, str) and value}


def render_pip_conf(settings: dict[str, str], extra_roots: bool) -> str | None:
    lines = []
    if "pip_index_url" in settings:
        lines.append(f"index-url = {settings['pip_index_url']}")
    if "pip_trusted_host" in settings:
        lines.append(f"trusted-host = {settings['pip_trusted_host']}")
    if extra_roots:
        lines.append(f"cert = {SYSTEM_BUNDLE}")
    return f"{MARKER}\n[global]\n" + "\n".join(lines) + "\n" if lines else None


def render_npmrc(settings: dict[str, str]) -> str | None:
    registry = settings.get("npm_registry")
    if not registry:
        return None
    # npm scopes credentials to the registry address, without the scheme.
    scope = "//" + registry.split("://", 1)[-1]
    lines = [MARKER, f"registry={registry}"]
    if settings.get("npm_auth"):
        lines += [f"{scope}:_auth={settings['npm_auth']}", f"{scope}:always-auth=true"]
    return "\n".join(lines) + "\n"


def _ours(path: Path) -> bool:
    try:
        return path.read_text().startswith(MARKER)
    except OSError:
        return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def configure_packages() -> None:
    """Apply (or remove) the package settings. Never raises: this must not stop the daemon."""
    settings = read_settings()
    ok = True
    pip_conf = render_pip_conf(settings, extra_roots=bool(read_bundle()))
    if pip_conf != _read(PIP_CONF):
        if pip_conf:
            # Readable by the agent, who runs pip; the credentials are its own to use.
            ok = sudo("tee", str(PIP_CONF), stdin=pip_conf) and sudo("chmod", "644", str(PIP_CONF))
        elif _ours(PIP_CONF):
            ok = sudo("rm", "-f", str(PIP_CONF))
    npmrc = render_npmrc(settings)
    try:
        if npmrc and npmrc != _read(NPMRC):
            NPMRC.write_text(npmrc)
            NPMRC.chmod(0o600)
        elif not npmrc and _ours(NPMRC):
            NPMRC.unlink()
    except OSError:
        ok = False
    if not ok:
        log.warning("package settings could not be applied")
    elif settings:
        log.info("package settings: using the host's package repository")
