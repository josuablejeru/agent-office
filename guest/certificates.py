"""Installs the root certificates the host trusts beyond the public ones (stdlib only).

Behind a company's HTTPS inspection every secure connection is signed by the
company's own root. The host puts the roots it trusts into `host_ca.pem` next
to this file on every start. They are installed for the command line (the
system store, plus the variables pip, requests and Node read) and for Chrome,
which on Linux takes them only through its `CACertificates` policy. Without
the file, everything this module added is removed again.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from guest.network import sudo

log = logging.getLogger("agentd")

BUNDLE_FILE = Path(__file__).with_name("host_ca.pem")
STORE_DIR = Path("/usr/local/share/ca-certificates/agent-office")
SYSTEM_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
CHROME_POLICY = Path("/etc/opt/chrome/policies/managed/agent-office-certificates.json")
PIP_CONF = Path("/etc/pip.conf")
ENVIRONMENT = Path("/etc/environment")
MARKER = "# agent-office: trust the host's extra root certificates"
ENV_VARIABLES = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")
PEM = re.compile(r"-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END CERTIFICATE-----")


def read_bundle(path: Path | None = None) -> list[str]:
    """The certificates in the host's bundle, as PEM blocks."""
    try:
        text = (path or BUNDLE_FILE).read_text()
    except OSError:
        return []
    return [f"-----BEGIN CERTIFICATE-----\n{''.join(body.split())}\n-----END CERTIFICATE-----\n"
            for body in PEM.findall(text)]


def chrome_policy(certificates: list[str]) -> str:
    """Chrome's policy wants each certificate as base64 DER: the body of the PEM block."""
    bodies = ["".join(PEM.search(pem).group(1).split()) for pem in certificates]  # type: ignore[union-attr]
    return json.dumps({"CACertificates": bodies}, indent=2) + "\n"


def environment_without_ours(text: str) -> str:
    lines = text.splitlines()
    kept = [line for line in lines if line != MARKER and not any(
        line.startswith(f"{name}=") for name in ENV_VARIABLES)]
    return "\n".join(kept) + ("\n" if kept else "")


def environment_with_ours(text: str) -> str:
    ours = [MARKER] + [f"{name}={SYSTEM_BUNDLE}" for name in ENV_VARIABLES]
    return environment_without_ours(text) + "\n".join(ours) + "\n"


def _installed() -> list[str]:
    try:
        return [path.read_text() for path in sorted(STORE_DIR.glob("*.crt"))]
    except OSError:
        return []


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def configure_certificates() -> None:
    """Apply (or remove) the host's extra roots. Never raises: this must not stop the daemon."""
    wanted = read_bundle()
    if wanted == _installed():
        return
    ok = sudo("rm", "-rf", str(STORE_DIR))
    if wanted:
        ok = ok and sudo("mkdir", "-p", str(STORE_DIR))
        for number, pem in enumerate(wanted, 1):
            ok = ok and sudo("tee", str(STORE_DIR / f"host-{number:02d}.crt"), stdin=pem)
    ok = ok and sudo("update-ca-certificates", "--fresh")
    if wanted:
        ok = ok and sudo("mkdir", "-p", str(CHROME_POLICY.parent))
        ok = ok and sudo("tee", str(CHROME_POLICY), stdin=chrome_policy(wanted))
        ok = ok and sudo("tee", str(PIP_CONF), stdin=f"{MARKER}\n[global]\ncert = {SYSTEM_BUNDLE}\n")
        ok = ok and sudo("tee", str(ENVIRONMENT), stdin=environment_with_ours(_read(ENVIRONMENT)))
    else:
        ok = ok and sudo("rm", "-f", str(CHROME_POLICY))
        if _read(PIP_CONF).startswith(MARKER):
            ok = ok and sudo("rm", "-f", str(PIP_CONF))
        ok = ok and sudo("tee", str(ENVIRONMENT), stdin=environment_without_ours(_read(ENVIRONMENT)))
    if ok:
        log.info("extra root certificates: %d installed", len(wanted))
    else:
        log.warning("extra root certificates could not be installed")
