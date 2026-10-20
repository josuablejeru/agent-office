"""Company root certificates: which the Mac passes on, and how agents' computers install them."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from backend.config import Settings
from backend.net.certificates import CertificateSource, beyond_public, certificate_authorities, file_roots
from backend.vm.base_image import BaseImageBuilder
from backend.vm.seed import HOST_CA_FILENAME, stage_seed
from guest import certificates as guest_certificates
from tests.certs import make_ca, make_v1_root, pem


def test_only_unexpired_certificate_authorities_are_taken() -> None:
    root, _ = make_ca("Example Corp Inspection Root")
    leaf, _ = make_ca("someone@example.com", ca=False)  # a personal certificate
    expired, _ = make_ca("Old Root", days=-1)
    found = certificate_authorities((pem(root) + pem(leaf) + pem(expired) + "junk").encode())
    assert [c.subject.rfc4514_string() for c in found] == ["CN=Example Corp Inspection Root"]


def test_public_authorities_and_what_they_issued_are_left_out() -> None:
    public_root, _ = make_ca("Public Root")
    company_root, _ = make_ca("Example Corp Root")
    public = {public_root.subject.public_bytes()}
    assert not beyond_public(public_root, public)
    assert beyond_public(company_root, public)


def test_roots_from_files_are_described_and_deduplicated(tmp_path: Path) -> None:
    root, _ = make_ca("Example Corp Root")
    bundle = tmp_path / "corp.pem"
    bundle.write_text(pem(root) + pem(root))
    (described,) = file_roots([str(bundle), str(tmp_path / "missing.pem")])[:1]
    assert described.subject == "Example Corp Root" and described.source == "corp.pem"
    source = CertificateSource(from_mac=False, extra_files=[str(bundle)])
    assert len(asyncio.run(source.roots())) == 1
    assert asyncio.run(source.bundle()).count("BEGIN CERTIFICATE") == 1
    assert asyncio.run(CertificateSource(from_mac=False).bundle()) == ""


def test_the_seed_carries_the_roots_only_when_there_are_some(tmp_path: Path) -> None:
    root, _ = make_ca("Example Corp Root")
    guest_source = Path(__file__).resolve().parents[1] / "guest"
    stage_seed(tmp_path / "with", "a", "secret", guest_source, certificates=pem(root))
    stage_seed(tmp_path / "without", "a", "secret", guest_source)
    assert (tmp_path / "with" / "guest" / HOST_CA_FILENAME).read_text() == pem(root)
    assert not (tmp_path / "without" / "guest" / HOST_CA_FILENAME).exists()


# --- on the agent's computer -----------------------------------------------------------------


def test_chrome_gets_each_root_as_base64_der(tmp_path: Path) -> None:
    root, _ = make_ca("Example Corp Root")
    bundle = tmp_path / "host_ca.pem"
    bundle.write_text("noise\n" + pem(root))
    (block,) = guest_certificates.read_bundle(bundle)
    policy = json.loads(guest_certificates.chrome_policy([block]))
    assert base64.b64decode(policy["CACertificates"][0]) == root.public_bytes(Encoding.DER)


def test_environment_lines_are_added_once_and_removed_cleanly() -> None:
    original = 'PATH="/usr/bin"\nLANG=C.UTF-8\n'
    added = guest_certificates.environment_with_ours(original)
    assert guest_certificates.environment_with_ours(added) == added  # applying twice changes nothing
    assert "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt" in added
    assert guest_certificates.environment_without_ours(added) == original


class FakeSudo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *command: str, stdin: str | None = None) -> bool:
        self.calls.append(command)
        return True


@pytest.fixture
def guest_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeSudo, Path]:
    sudo = FakeSudo()
    monkeypatch.setattr(guest_certificates, "sudo", sudo)
    monkeypatch.setattr(guest_certificates, "BUNDLE_FILE", tmp_path / "host_ca.pem")
    monkeypatch.setattr(guest_certificates, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(guest_certificates, "PIP_CONF", tmp_path / "pip.conf")
    monkeypatch.setattr(guest_certificates, "ENVIRONMENT", tmp_path / "environment")
    return sudo, tmp_path


def test_roots_from_the_host_are_installed_everywhere(guest_files: tuple[FakeSudo, Path]) -> None:
    sudo, home = guest_files
    root, _ = make_ca("Example Corp Root")
    (home / "host_ca.pem").write_text(pem(root))
    guest_certificates.configure_certificates()
    commands = [" ".join(call[:2]) for call in sudo.calls]
    assert "update-ca-certificates --fresh" in commands
    targets = [call[1] for call in sudo.calls if call[0] == "tee"]
    assert ("python3", "-c", guest_certificates.WRITE_FILES, str(home / "store")) in sudo.calls
    assert str(guest_certificates.CHROME_POLICY) in targets
    assert str(home / "pip.conf") in targets and str(home / "environment") in targets


def test_nothing_happens_when_nothing_changed(guest_files: tuple[FakeSudo, Path]) -> None:
    sudo, home = guest_files
    guest_certificates.configure_certificates()  # no roots from the host, none installed
    assert sudo.calls == []
    root, _ = make_ca("Example Corp Root")
    (home / "host_ca.pem").write_text(pem(root))
    (home / "store").mkdir()
    (home / "store" / "host-01.crt").write_text(guest_certificates.read_bundle(home / "host_ca.pem")[0])
    guest_certificates.configure_certificates()  # already installed
    assert sudo.calls == []


def test_roots_the_host_no_longer_trusts_are_removed(guest_files: tuple[FakeSudo, Path]) -> None:
    sudo, home = guest_files
    (home / "store").mkdir()
    (home / "store" / "host-01.crt").write_text("old")
    (home / "pip.conf").write_text(guest_certificates.MARKER + "\n[global]\n")
    guest_certificates.configure_certificates()
    assert ("rm", "-rf", str(home / "store")) in sudo.calls
    assert ("rm", "-f", str(guest_certificates.CHROME_POLICY)) in sudo.calls
    assert ("rm", "-f", str(home / "pip.conf")) in sudo.calls
    assert ("update-ca-certificates", "--fresh") in sudo.calls


# --- using an image built on another Mac ------------------------------------------------------

needs_qemu_img = pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")


def image(path: Path, *options: str) -> Path:
    subprocess.run(["qemu-img", "create", *options, str(path), "64M"], check=True, capture_output=True)
    return path


async def finished(builder: BaseImageBuilder) -> Any:
    for _ in range(200):
        if not builder.status().building:
            return builder.status()
        await asyncio.sleep(0.02)
    raise AssertionError("the import did not finish")


@needs_qemu_img
def test_an_image_from_another_mac_can_be_used(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    source = image(tmp_path / "debian-desktop.qcow2", "-f", "qcow2")

    async def scenario() -> Any:
        builder = BaseImageBuilder(settings)
        assert builder.start_import(str(source)).building
        return await finished(builder)

    status = asyncio.run(scenario())
    target = settings.base_image_path("debian-desktop")
    assert status.ready and status.detail == "" and target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o444 and source.exists()  # the original is left alone


@needs_qemu_img
def test_files_that_are_not_a_system_image_are_refused(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    raw = image(tmp_path / "disk.img", "-f", "raw")
    base = image(tmp_path / "base.qcow2", "-f", "qcow2")
    overlay = image(tmp_path / "overlay.qcow2", "-f", "qcow2", "-b", str(base), "-F", "qcow2")

    async def attempt(path: str) -> str:
        return BaseImageBuilder(settings).start_import(path).detail

    assert "not a file" in asyncio.run(attempt(str(tmp_path / "missing.qcow2")))
    assert "not a qcow2 image" in asyncio.run(attempt(str(raw)))
    assert "an agent's disk" in asyncio.run(attempt(str(overlay)))
    (tmp_path / "notes.txt").write_text("hello")
    assert "Import failed" in asyncio.run(attempt(str(tmp_path / "notes.txt")))
    assert not settings.base_image_path("debian-desktop").exists()


@needs_qemu_img
def test_an_image_is_not_replaced_under_existing_agents(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    (settings.home / "agents" / "pam").mkdir(parents=True)
    (settings.home / "agents" / "pam" / "disk.qcow2").write_text("x")
    source = image(tmp_path / "debian-desktop.qcow2", "-f", "qcow2")
    status = BaseImageBuilder(settings).start_import(str(source))
    assert "already depend" in status.detail and not status.ready


# --- added in the app ------------------------------------------------------------------------


def test_a_certificate_can_be_added_and_removed_in_settings(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.serialization import Encoding as Enc
    from fastapi.testclient import TestClient

    from backend.keystore import KeyStore
    from backend.main import create_app
    from tests.test_agents_api import FakeKeychain

    settings = Settings(home=tmp_path / "home")
    settings.config_path.parent.mkdir(parents=True)
    settings.config_path.write_text("network:\n  split_dns: false\n  trust_mac_certificates: false\n")
    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()))
    root, _ = make_ca("Example Corp Root")
    leaf, _ = make_ca("server.example.com", ca=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        assert client.get("/api/system/dns").json()["certificates"] == []

        refused = client.post("/api/system/certificates", content=pem(leaf).encode())
        assert refused.status_code == 400 and "certificate authority" in refused.json()["detail"]
        assert client.post("/api/system/certificates", content=b"x" * 11_000_000).status_code == 413

        # A .cer file is often DER rather than PEM: both are accepted.
        (added,) = client.post("/api/system/certificates", content=root.public_bytes(Enc.DER)).json()
        listed = client.get("/api/system/dns").json()["certificates"]
        assert [(c["subject"], c["source"]) for c in listed] == [("Example Corp Root", "added in the app")]
        assert "Example Corp Root" not in "".join(settings.load_user_config().keys())  # kept out of config.yaml
        assert asyncio.run(app.state.certificates.bundle()) == pem(root)  # what agents' computers get

        assert client.delete(f"/api/system/certificates/{added['fingerprint']}").status_code == 204
        assert client.get("/api/system/dns").json()["certificates"] == []
        assert client.delete(f"/api/system/certificates/{added['fingerprint']}").status_code == 404
        assert client.delete("/api/system/certificates/..%2Fsecrets").status_code == 404


def test_an_old_root_without_extensions_is_still_a_root() -> None:
    old_root = make_v1_root("Example Corp Legacy Root")
    found = certificate_authorities(pem(old_root).encode())
    assert [c.subject.rfc4514_string() for c in found] == ["CN=Example Corp Legacy Root"]


def test_a_company_bundle_of_hundreds_of_certificates_is_accepted(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.serialization import pkcs7 as p7

    from backend.net.certificates import read_any

    roots = [make_ca(f"Example Corp CA {n}")[0] for n in range(700)]
    leaf, _ = make_ca("server.example.com", ca=False)
    bundle = ("".join(pem(root) for root in roots) + pem(leaf)).encode()
    assert len(bundle) > 256 * 1024  # bigger than the old limit, like the file that was refused
    assert len(read_any(bundle)) == 700  # the server certificate is left out

    for encoding in (Encoding.PEM, Encoding.DER):  # PKCS#7, as IT sometimes ships it under .crt
        packed = p7.serialize_certificates(roots[:3], encoding)
        # A PKCS#7 set has no fixed order: which OpenSSL wrote it decides.
        assert {c.subject for c in read_any(packed)} == {r.subject for r in roots[:3]}
    assert read_any(b"\x30\x82garbage") == []


def test_the_root_side_script_writes_one_file_per_certificate(tmp_path: Path) -> None:
    import sys

    roots = [pem(make_ca(f"Example Corp CA {n}")[0]) for n in range(3)]
    subprocess.run([sys.executable, "-c", guest_certificates.WRITE_FILES, str(tmp_path / "store")],
                   input=json.dumps(roots), text=True, check=True)
    files = sorted((tmp_path / "store").iterdir())
    assert [f.name for f in files] == ["host-0001.crt", "host-0002.crt", "host-0003.crt"]
    assert [f.read_text() for f in files] == roots
