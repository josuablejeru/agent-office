"""A company package repository (Artifactory) for pip and npm on agents' computers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from backend.config import Settings
from backend.net.packages import Artifactory, check
from backend.vm.base_image import BaseImageBuilder
from backend.vm.seed import HOST_PACKAGES_FILENAME, stage_seed
from guest import packages as guest_packages
from tests.fake_artifactory import FakeArtifactory

REPO = {"url": "https://artifactory.example.com/artifactory/", "username": "jo.doe@example.com",
        "pypi_repo": "pypi-remote", "npm_repo": "npm-remote"}


def test_addresses_are_built_the_way_pip_and_npm_expect() -> None:
    repository = Artifactory(**REPO)
    assert repository.url == "https://artifactory.example.com/artifactory"  # trailing slash dropped
    # Characters with a meaning in a URL are escaped in the credentials.
    assert repository.pip_index_url("t@k/en:1") == (
        "https://jo.doe%40example.com:t%40k%2Fen%3A1@artifactory.example.com/artifactory/api/pypi/pypi-remote/simple")
    settings = repository.guest_settings("tok")
    assert settings["npm_registry"] == "https://artifactory.example.com/artifactory/api/npm/npm-remote/"
    assert "npm_auth" in settings
    assert "npm_registry" not in Artifactory(**{**REPO, "npm_repo": ""}).guest_settings("tok")


@pytest.mark.parametrize("bad", [
    {"url": "artifactory.example.com"}, {"url": "ftp://x/artifactory"},
    {"url": "https://user:pw@x/artifactory"}, {"pypi_repo": "../other"}, {"pypi_repo": ""}, {"username": ""},
])
def test_wrong_settings_are_refused(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Artifactory(**{**REPO, **bad})


def test_the_connection_test_says_what_is_wrong() -> None:
    fake = FakeArtifactory("jo", "right-token")
    try:
        repository = Artifactory(url=fake.url(), username="jo", pypi_repo="pypi-remote")
        assert asyncio.run(check(repository, "wrong")).detail == "Artifactory refused the username or token."
        missing = asyncio.run(check(repository.model_copy(update={"pypi_repo": "nope"}), "right-token"))
        assert not missing.ok and "no PyPI repository named 'nope'" in missing.detail
        unreachable = asyncio.run(check(repository.model_copy(update={"url": "http://127.0.0.1:9/artifactory"}),
                                        "right-token", timeout=2))
        assert not unreachable.ok and "could not be reached" in unreachable.detail
    finally:
        fake.close()


def test_the_seed_carries_package_settings_only_when_set(tmp_path: Path) -> None:
    guest_source = Path(__file__).resolve().parents[1] / "guest"
    stage_seed(tmp_path / "with", "a", "s", guest_source, packages={"pip_index_url": "https://x/simple"})
    stage_seed(tmp_path / "without", "a", "s", guest_source)
    assert json.loads((tmp_path / "with" / "guest" / HOST_PACKAGES_FILENAME).read_text()) == {
        "pip_index_url": "https://x/simple"}
    assert not (tmp_path / "without" / "guest" / HOST_PACKAGES_FILENAME).exists()


def test_the_image_build_gets_the_index_but_the_log_does_not(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    script = tmp_path / "build.sh"
    script.write_text('#!/bin/bash\necho "index given: ${PIP_INDEX_URL:+yes}"\n')

    async def index() -> str:
        return "https://jo:secret-token@artifactory.example.com/artifactory/api/pypi/pypi-remote/simple"

    async def scenario() -> None:
        builder = BaseImageBuilder(settings, script=script, pip_index_url=index)
        builder.start()
        await builder._task  # noqa: SLF001

    asyncio.run(scenario())
    log = (settings.images_dir / "build.log").read_text()
    assert "index given: yes" in log and "secret-token" not in log


# --- on the agent's computer -----------------------------------------------------------------


def test_pip_settings_combine_the_repository_and_the_extra_roots() -> None:
    index = {"pip_index_url": "https://jo:t@a.example.com/artifactory/api/pypi/r/simple"}
    both = guest_packages.render_pip_conf(index, extra_roots=True)
    assert both is not None and both.startswith(guest_packages.MARKER)
    assert "index-url = https://jo:t@a.example.com/artifactory/api/pypi/r/simple" in both
    assert "cert = /etc/ssl/certs/ca-certificates.crt" in both
    assert "index-url" not in (guest_packages.render_pip_conf({}, extra_roots=True) or "")
    assert guest_packages.render_pip_conf({}, extra_roots=False) is None


def test_npm_gets_the_registry_and_credentials_for_that_registry_only() -> None:
    npmrc = guest_packages.render_npmrc({"npm_registry": "https://a.example.com/artifactory/api/npm/n/",
                                         "npm_auth": "am86dA=="})
    assert npmrc is not None
    assert "registry=https://a.example.com/artifactory/api/npm/n/" in npmrc
    assert "//a.example.com/artifactory/api/npm/n/:_auth=am86dA==" in npmrc
    assert guest_packages.render_npmrc({"pip_index_url": "x"}) is None


def test_package_settings_are_written_and_taken_away_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    written: dict[str, str] = {}

    def sudo(*command: str, stdin: str | None = None) -> bool:
        calls.append(command)
        if command[0] == "tee" and stdin is not None:
            Path(command[1]).write_text(stdin)
            written[command[1]] = stdin
        if command[:2] == ("rm", "-f"):
            Path(command[2]).unlink(missing_ok=True)
        return True

    monkeypatch.setattr(guest_packages, "sudo", sudo)
    monkeypatch.setattr(guest_packages, "read_bundle", lambda: [])
    monkeypatch.setattr(guest_packages, "SETTINGS_FILE", tmp_path / "host_packages.json")
    monkeypatch.setattr(guest_packages, "PIP_CONF", tmp_path / "pip.conf")
    monkeypatch.setattr(guest_packages, "NPMRC", tmp_path / ".npmrc")
    (tmp_path / "host_packages.json").write_text(json.dumps(
        {"pip_index_url": "https://x/simple", "npm_registry": "https://x/npm/", "npm_auth": "YQ=="}))
    guest_packages.configure_packages()
    assert "index-url = https://x/simple" in (tmp_path / "pip.conf").read_text()
    assert (tmp_path / ".npmrc").stat().st_mode & 0o777 == 0o600

    calls.clear()
    guest_packages.configure_packages()  # nothing changed: nothing written
    assert calls == []

    (tmp_path / "host_packages.json").unlink()  # the repository was removed in Settings
    guest_packages.configure_packages()
    assert not (tmp_path / "pip.conf").exists() and not (tmp_path / ".npmrc").exists()

    (tmp_path / "pip.conf").write_text("[global]\ntimeout = 60\n")  # the agent's own pip.conf
    guest_packages.configure_packages()
    assert (tmp_path / "pip.conf").read_text() == "[global]\ntimeout = 60\n"


# --- Settings ----------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    from backend.keystore import KeyStore
    from backend.main import create_app
    from tests.test_agents_api import FakeKeychain

    settings = Settings(home=tmp_path / "home")
    settings.config_path.parent.mkdir(parents=True)
    settings.config_path.write_text("network:\n  split_dns: false\n  trust_mac_certificates: false\n")
    keychain = FakeKeychain()
    app = create_app(settings, key_store=KeyStore(run=keychain))
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        test_client.settings, test_client.keychain, test_client.app_state = settings, keychain, app.state  # type: ignore[attr-defined]
        yield test_client


def test_artifactory_is_set_up_tested_and_removed_in_settings(client: Any) -> None:
    fake = FakeArtifactory("jo", "s3cret-token")
    try:
        form = {"url": fake.url(), "username": "jo", "pypi_repo": "pypi-remote", "npm_repo": "", "token": ""}
        assert client.get("/api/system/packages").json()["url"] == ""

        assert "enter the Artifactory token" in client.put("/api/system/packages", json=form).json()["detail"]
        assert client.post("/api/system/packages/test", json={**form, "token": "nope"}).json()["ok"] is False
        assert client.post("/api/system/packages/test", json={**form, "token": "s3cret-token"}).json()["ok"] is True

        saved = client.put("/api/system/packages", json={**form, "token": "s3cret-token"}).json()
        assert saved["has_token"] and saved["pypi_repo"] == "pypi-remote"
        assert "s3cret-token" not in client.settings.config_path.read_text()  # only in the Keychain
        assert client.keychain.items["ARTIFACTORY_TOKEN"] == "s3cret-token"
        # Changing a field later keeps the stored token, and the test uses it.
        assert client.put("/api/system/packages", json={**form, "npm_repo": "npm-remote"}).json()["npm_repo"] == "npm-remote"
        assert client.post("/api/system/packages/test", json=form).json()["ok"] is True

        # What an agent's computer is given at its next start.
        given = asyncio.run(client.app_state.vm_manager.packages())
        assert given["pip_index_url"] == f"http://jo:s3cret-token@127.0.0.1:{fake.port}/artifactory/api/pypi/pypi-remote/simple"
        assert given["npm_registry"].endswith("/api/npm/npm-remote/")

        bad = client.put("/api/system/packages", json={**form, "url": "not a url"})
        assert bad.status_code == 400 and bad.json()["detail"].startswith("url:")

        assert client.delete("/api/system/packages").status_code == 204
        assert client.get("/api/system/packages").json() == {
            "url": "", "username": "", "pypi_repo": "", "npm_repo": "", "base_image": "", "has_token": False}
        assert asyncio.run(client.app_state.vm_manager.packages()) is None
    finally:
        fake.close()


def test_a_repository_without_https_is_named_as_trusted_for_pip() -> None:
    plain = Artifactory(**{**REPO, "url": "http://artifactory.corp.example:8081/artifactory"}).guest_settings("t")
    assert plain["pip_trusted_host"] == "artifactory.corp.example"
    assert "trusted-host = artifactory.corp.example" in (guest_packages.render_pip_conf(plain, False) or "")
    assert "pip_trusted_host" not in Artifactory(**REPO).guest_settings("t")


# --- a company base image from Artifactory -----------------------------------------------------


def published_image(tmp_path: Path) -> tuple[FakeArtifactory, Artifactory, Path]:
    image = tmp_path / "agent-base-arm64.qcow2"
    image.write_bytes(b"QFI\xfb" + b"\0" * 4096)
    fake = FakeArtifactory("jo", "tok")
    fake.publish("hardened/debian/13/agent-base-arm64.qcow2", image)
    repository = Artifactory(url=fake.url(), username="jo", pypi_repo="pypi-remote",
                             base_image="hardened/debian/13/agent-base-arm64.qcow2")
    return fake, repository, image


def test_the_base_image_checksum_comes_from_artifactory(tmp_path: Path) -> None:
    import hashlib

    from backend.net.packages import ImageError, image_info

    fake, repository, image = published_image(tmp_path)
    try:
        expected = hashlib.sha256(image.read_bytes()).hexdigest()
        found = asyncio.run(image_info(repository, "tok"))
        assert found.sha256 == expected and found.size == image.stat().st_size
        assert found.url == f"{fake.url()}/hardened/debian/13/agent-base-arm64.qcow2"

        fake.storage_api = False  # a remote repository that has not cached it: the header is used
        assert asyncio.run(image_info(repository, "tok")).sha256 == expected

        fake.checksum_header = False  # no checksum anywhere: refused, never used unverified
        with pytest.raises(ImageError, match="no SHA-256"):
            asyncio.run(image_info(repository, "tok"))

        fake.storage_api, fake.checksum_header = True, True
        with pytest.raises(ImageError, match="refused the username or token"):
            asyncio.run(image_info(repository, "wrong"))
        with pytest.raises(ImageError, match="There is no file"):
            asyncio.run(image_info(repository.model_copy(update={"base_image": "hardened/nope/x.qcow2"}), "tok"))
    finally:
        fake.close()


def test_the_build_gets_the_image_and_a_private_credentials_file(tmp_path: Path) -> None:
    from backend.keystore import KeyStore
    from backend.net.packages import PackageSource
    from tests.test_agents_api import FakeKeychain

    fake, repository, _ = published_image(tmp_path)
    try:
        settings = Settings(home=tmp_path / "home")
        settings.ensure_layout()
        settings.update_user_config(["packages", "artifactory"], repository.model_dump())
        keys = KeyStore(run=FakeKeychain())
        keys.set("ARTIFACTORY_TOKEN", "tok")
        script = tmp_path / "build.sh"
        script.write_text(
            "#!/bin/bash\n"
            'echo "url=$BASE_IMAGE_URL sha=${BASE_IMAGE_SHA256:0:8} name=$BASE_IMAGE_NAME"\n'
            'echo "mode=$(stat -f %Lp "$BASE_IMAGE_CURL_CONFIG")"\n'
            'grep -c "user = " "$BASE_IMAGE_CURL_CONFIG"\n'
        )
        source = PackageSource(settings, keys)

        async def scenario() -> None:
            builder = BaseImageBuilder(settings, script=script, base_image=source.base_image)
            builder.start()
            await builder._task  # noqa: SLF001

        asyncio.run(scenario())
        log = (settings.images_dir / "build.log").read_text()
        assert "name=hardened/debian/13/agent-base-arm64.qcow2" in log and "mode=600" in log
        assert "tok" not in log.replace("token", "")  # the token is in the file, never in the output
        assert not (settings.images_dir / ".artifactory-curl").exists()  # and the file is gone afterwards
    finally:
        fake.close()


def test_a_base_image_that_cannot_be_verified_stops_the_build(tmp_path: Path) -> None:
    from backend.keystore import KeyStore
    from backend.net.packages import PackageSource
    from tests.test_agents_api import FakeKeychain

    fake, repository, _ = published_image(tmp_path)
    fake.storage_api, fake.checksum_header = False, False
    try:
        settings = Settings(home=tmp_path / "home")
        settings.ensure_layout()
        settings.update_user_config(["packages", "artifactory"], repository.model_dump())
        keys = KeyStore(run=FakeKeychain())
        keys.set("ARTIFACTORY_TOKEN", "tok")
        script = tmp_path / "build.sh"
        script.write_text("#!/bin/bash\necho should-not-run\n")

        async def scenario() -> Any:
            builder = BaseImageBuilder(settings, script=script, base_image=PackageSource(settings, keys).base_image)
            builder.start()
            await builder._task  # noqa: SLF001
            return builder.status()

        status = asyncio.run(scenario())
        assert status.detail.startswith("Build failed:") and "no SHA-256" in status.detail
        assert "should-not-run" not in (settings.images_dir / "build.log").read_text()
    finally:
        fake.close()


def test_the_base_image_path_is_checked() -> None:
    for bad in ("../x.qcow2", "repo/x.iso", "/repo/x.qcow2", "repo/../x.qcow2", "x.qcow2"):
        with pytest.raises(ValueError):
            Artifactory(**{**REPO, "base_image": bad})
    assert Artifactory(**{**REPO, "base_image": "images/debian/agent-arm64.raw"}).base_image
