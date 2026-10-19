"""The extra root certificates this Mac trusts, handed on to agents' computers.

Company networks often inspect HTTPS with their own root certificate, which IT
installs on the Mac. A VM does not know it, so every secure connection from an
agent's computer (Chrome, apt, pip, curl) would fail. This module finds the
certificate authorities the Mac trusts beyond the public ones (those in the
System and login keychains that macOS accepts), plus any listed in
`network.extra_ca_certificates`, and the seed passes them to each VM.

Only CA certificates that macOS itself trusts are taken: personal and VPN
client certificates, and anything the user never trusted, stay on the Mac.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from pydantic import BaseModel

from backend.logging_config import get_logger
from backend.vm.capabilities import run_command

log = get_logger("certificates")

KEYCHAINS = (
    Path("/Library/Keychains/System.keychain"),
    Path.home() / "Library/Keychains/login.keychain-db",
)
# The public certificate authorities macOS ships. What chains to these is already
# trusted by Debian and Chrome, so only the rest needs passing on.
BUILT_IN_ROOTS = Path("/System/Library/Keychains/SystemRootCertificates.keychain")
PEM_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)
CACHE_SECONDS = 600


class TrustedRoot(BaseModel):
    subject: str
    expires: str
    source: str  # "this Mac", "added in the app", or the file it came from
    pem: str
    # SHA-256 of the certificate: names one added in the app, for removing it.
    fingerprint: str = ""


def certificate_authorities(pem_bytes: bytes) -> list[x509.Certificate]:
    """The unexpired CA certificates in a PEM bundle; everything else is skipped."""
    found = []
    now = datetime.now(UTC)
    for block in PEM_BLOCK.findall(pem_bytes):
        try:
            with warnings.catch_warnings():
                # Some old public roots have non-standard serial numbers; they are still readable.
                warnings.simplefilter("ignore")
                certificate = x509.load_pem_x509_certificate(block)
                try:
                    is_ca = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                except x509.ExtensionNotFound:
                    # Old (version 1) roots, as some company CAs still use, have no
                    # extensions at all: a self-signed one is a root.
                    is_ca = certificate.subject == certificate.issuer
        except ValueError:
            continue
        if is_ca and certificate.not_valid_after_utc > now:
            found.append(certificate)
    return found


def describe(certificate: x509.Certificate, source: str) -> TrustedRoot:
    names = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    subject = str(names[0].value) if names else certificate.subject.rfc4514_string()
    return TrustedRoot(
        subject=subject,
        expires=certificate.not_valid_after_utc.date().isoformat(),
        source=source,
        pem=certificate.public_bytes(Encoding.PEM).decode(),
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
    )


def read_any(data: bytes) -> list[x509.Certificate]:
    """The CA certificates in an uploaded file, whether PEM text or a single DER certificate."""
    if b"-----BEGIN CERTIFICATE-----" in data:
        return certificate_authorities(data)
    try:
        certificate = x509.load_der_x509_certificate(data)
    except ValueError:
        return []
    return certificate_authorities(certificate.public_bytes(Encoding.PEM))


async def trusted_by_macos(pem: str) -> bool:
    """Whether macOS accepts this certificate, without going to the network."""
    handle, name = tempfile.mkstemp(suffix=".pem")
    path = Path(name)
    with os.fdopen(handle, "w") as file:
        file.write(pem)
    try:
        output = await run_command("security", "verify-cert", "-c", str(path), "-p", "basic", "-L", "-q")
    finally:
        path.unlink(missing_ok=True)
    return output is not None


async def _keychain_certificates(keychain: Path) -> list[x509.Certificate]:
    if not keychain.exists():
        return []
    output = await run_command("security", "find-certificate", "-a", "-p", str(keychain), timeout=20)
    return certificate_authorities((output or "").encode())


def beyond_public(certificate: x509.Certificate, public: set[bytes]) -> bool:
    """Not one of the public CAs, nor issued by one (company roots and their intermediates are kept)."""
    return certificate.subject.public_bytes() not in public and certificate.issuer.public_bytes() not in public


async def mac_roots() -> list[TrustedRoot]:
    public = {certificate.subject.public_bytes() for certificate in await _keychain_certificates(BUILT_IN_ROOTS)}
    roots: list[TrustedRoot] = []
    for keychain in KEYCHAINS:
        for certificate in await _keychain_certificates(keychain):
            if not beyond_public(certificate, public):
                continue
            root = describe(certificate, "this Mac")
            if await trusted_by_macos(root.pem):
                roots.append(root)
    return roots


def file_roots(paths: list[str]) -> list[TrustedRoot]:
    roots = []
    for name in paths:
        path = Path(name).expanduser()
        try:
            data = path.read_bytes()
        except OSError as exc:
            log.warning("extra certificate file not readable", extra={"path": str(path), "error": str(exc)})
            continue
        roots += [describe(certificate, path.name) for certificate in certificate_authorities(data)]
    return roots


class CertificateSource:
    """Looks up the roots to pass on, at most every few minutes."""

    def __init__(
        self, from_mac: bool = True, extra_files: list[str] | None = None, added_dir: Path | None = None
    ) -> None:
        self._from_mac = from_mac
        self._extra_files = list(extra_files or [])
        # Certificates added in the app (Settings › Network), one file each.
        self._added_dir = added_dir
        self._cached: list[TrustedRoot] | None = None
        self._read_at = 0.0
        self._lock = asyncio.Lock()

    async def roots(self) -> list[TrustedRoot]:
        async with self._lock:
            if self._cached is None or time.monotonic() - self._read_at > CACHE_SECONDS:
                roots = self._added() + file_roots(self._extra_files)
                if self._from_mac:
                    roots += await mac_roots()
                unique: dict[str, TrustedRoot] = {}
                for root in roots:
                    unique.setdefault(root.pem, root)
                self._cached, self._read_at = list(unique.values()), time.monotonic()
                log.info("extra root certificates", extra={"count": len(self._cached)})
            return self._cached

    def _added(self) -> list[TrustedRoot]:
        if self._added_dir is None:
            return []
        roots = []
        for path in sorted(self._added_dir.glob("*.pem")):
            roots += [describe(c, "added in the app") for c in certificate_authorities(path.read_bytes())]
        return roots

    def add(self, data: bytes) -> list[TrustedRoot]:
        """Keep the CA certificates in an uploaded file. Raises ValueError if there are none."""
        if self._added_dir is None:
            raise ValueError("certificates cannot be added here")
        found = read_any(data)
        if not found:
            raise ValueError(
                "That file holds no certificate authority: use the company's root certificate "
                "(a .pem, .crt or .cer file), not a personal or server certificate."
            )
        self._added_dir.mkdir(parents=True, exist_ok=True)
        added = [describe(certificate, "added in the app") for certificate in found]
        for root in added:
            (self._added_dir / f"{root.fingerprint}.pem").write_text(root.pem)
        self._cached = None
        return added

    def remove(self, fingerprint: str) -> bool:
        if self._added_dir is None or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return False
        path = self._added_dir / f"{fingerprint}.pem"
        if not path.exists():
            return False
        path.unlink()
        self._cached = None
        return True

    async def bundle(self) -> str:
        """All roots as one PEM text (empty when there are none)."""
        return "".join(root.pem for root in await self.roots())
