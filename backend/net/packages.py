"""A company package repository (JFrog Artifactory) for agents' computers.

Company networks often block the public package indexes and offer the same
packages through Artifactory instead. With this set up, `pip` and `npm` on
every agent's computer, and the image build, fetch through it.

The token lives in the macOS Keychain. It reaches an agent's computer inside
the package settings, where the agent can read it, so a read-only token is the
right kind to use.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from backend.config import Settings
from backend.keystore import KeyStore

TOKEN_KEY = "ARTIFACTORY_TOKEN"
CONFIG_PATH = ["packages", "artifactory"]
REPOSITORY = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
# repository/path/to/image.qcow2 in Artifactory: no "..", no leading slash.
IMAGE_PATH = r"^$|^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9][A-Za-z0-9._+-]{0,200})+\.(qcow2|img|raw)$"


class Artifactory(BaseModel):
    # The address up to and including /artifactory, e.g. https://artifactory.example.com/artifactory
    url: str = Field(min_length=8, max_length=500)
    username: str = Field(min_length=1, max_length=200)
    pypi_repo: str = Field(pattern=REPOSITORY)
    npm_repo: str = Field(default="", pattern=r"^$|" + REPOSITORY)
    # The company's (hardened) base image the agent computers are built on, as repository/path.
    # Empty: Debian's public cloud image.
    base_image: str = Field(default="", pattern=IMAGE_PATH)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.query:
            raise ValueError("give the address of Artifactory, like https://artifactory.example.com/artifactory")
        return value

    def pip_index_url(self, token: str) -> str:
        """What pip's --index-url takes: the PyPI repository's simple index, with credentials."""
        parts = urlsplit(self.url)
        credentials = f"{quote(self.username, safe='')}:{quote(token, safe='')}@"
        base = f"{parts.scheme}://{credentials}{parts.netloc}{parts.path}"
        return f"{base}/api/pypi/{self.pypi_repo}/simple"

    def npm_registry(self) -> str:
        return f"{self.url}/api/npm/{self.npm_repo}/" if self.npm_repo else ""

    def guest_settings(self, token: str) -> dict[str, Any]:
        """What an agent's computer needs to use the repository."""
        settings: dict[str, Any] = {"pip_index_url": self.pip_index_url(token)}
        parts = urlsplit(self.url)
        if parts.scheme == "http":
            # pip ignores an index without HTTPS unless its host is named as trusted.
            settings["pip_trusted_host"] = parts.hostname
        if self.npm_repo:
            settings["npm_registry"] = self.npm_registry()
            settings["npm_auth"] = base64.b64encode(f"{self.username}:{token}".encode()).decode()
        return settings


class ImageInfo(BaseModel):
    """A base image in Artifactory, with the checksum Artifactory holds for it."""

    path: str
    url: str
    sha256: str
    size: int | None = None
    modified: str = ""


class ImageError(Exception):
    pass


async def image_info(repository: Artifactory, token: str, timeout: float = 20.0) -> ImageInfo:
    """Look the base image up in Artifactory, and take its SHA-256 from Artifactory.

    The storage API knows every file a repository holds; a remote repository may
    not have fetched the file yet, and then the checksum header of the file
    itself is used. Without a SHA-256 from either, the image is refused: the
    point of a vetted image is that what arrives is what was published.
    """
    if not repository.base_image:
        raise ImageError("No base image is set.")
    url = f"{repository.url}/{repository.base_image}"
    auth = (repository.username, token)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            storage = await client.get(f"{repository.url}/api/storage/{repository.base_image}", auth=auth)
            if storage.status_code in (401, 403):
                raise ImageError("Artifactory refused the username or token for the base image.")
            if storage.status_code == 200:
                data = storage.json()
                sha256 = str((data.get("checksums") or {}).get("sha256") or "")
                size = int(data["size"]) if str(data.get("size", "")).isdigit() else None
                if re.fullmatch(r"[0-9a-f]{64}", sha256):
                    return ImageInfo(path=repository.base_image, url=url, sha256=sha256, size=size,
                                     modified=str(data.get("lastModified", "")))
            head = await client.head(url, auth=auth)
    except httpx.HTTPError as exc:
        raise ImageError(f"Artifactory could not be reached: {type(exc).__name__}: {exc}"[:300]) from exc
    if head.status_code == 404:
        raise ImageError(f"There is no file {repository.base_image} in Artifactory.")
    if head.status_code in (401, 403):
        raise ImageError("Artifactory refused the username or token for the base image.")
    if head.status_code != 200:
        raise ImageError(f"Artifactory answered with HTTP {head.status_code} for the base image.")
    sha256 = head.headers.get("X-Checksum-Sha256", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ImageError(
            f"Artifactory gives no SHA-256 for {repository.base_image}, so it cannot be verified "
            "and is not used. Ask for the checksum to be recorded, or let the repository cache it once."
        )
    length = head.headers.get("Content-Length", "")
    return ImageInfo(path=repository.base_image, url=url, sha256=sha256,
                     size=int(length) if length.isdigit() else None,
                     modified=head.headers.get("Last-Modified", ""))


def curl_config(repository: Artifactory, token: str) -> str:
    """Credentials for curl, read from a file: on its command line they would show in `ps`."""
    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"user = {quoted(f'{repository.username}:{token}')}\n"


def load(settings: Settings) -> Artifactory | None:
    section = ((settings.load_config().get("packages") or {}).get("artifactory")) or None
    if not section:
        return None
    try:
        return Artifactory.model_validate(section)
    except ValueError:
        return None


class PackageSource:
    """The configured repository and its token, as the VMs and the build need them."""

    def __init__(self, settings: Settings, keys: KeyStore) -> None:
        self._settings = settings
        self._keys = keys

    def current(self) -> tuple[Artifactory, str] | None:
        repository = load(self._settings)
        token = self._keys.get(TOKEN_KEY) if repository else None
        return (repository, token) if repository and token else None

    async def guest_settings(self) -> dict[str, Any] | None:
        current = self.current()
        return current[0].guest_settings(current[1]) if current else None

    async def base_image(self) -> tuple[ImageInfo, str] | None:
        """The company base image to build on, with curl's credentials; None for Debian's.

        Raises ImageError when one is set but cannot be used: the build must not
        quietly fall back to the public image.
        """
        current = self.current()
        if current is None:
            repository = load(self._settings)
            if repository and repository.base_image:
                raise ImageError("The Artifactory token is missing: enter it under Settings › Network.")
            return None
        repository, token = current
        if not repository.base_image:
            return None
        return await image_info(repository, token), curl_config(repository, token)

    async def pip_index_url(self) -> str:
        current = self.current()
        return current[0].pip_index_url(current[1]) if current else ""


class CheckResult(BaseModel):
    ok: bool
    detail: str


async def check(repository: Artifactory, token: str, timeout: float = 15.0) -> CheckResult:
    """Ask the PyPI repository for a well-known package, as pip would."""
    url = f"{repository.url}/api/pypi/{repository.pypi_repo}/simple/pip/"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, auth=(repository.username, token))
    except httpx.HTTPError as exc:
        return CheckResult(ok=False, detail=f"Artifactory could not be reached: {type(exc).__name__}: {exc}"[:300])
    if response.status_code == 200 and "pip-" in response.text:
        detail = f"Connected: '{repository.pypi_repo}' serves Python packages."
        if repository.base_image:
            try:
                image = await image_info(repository, token, timeout)
            except ImageError as exc:
                return CheckResult(ok=False, detail=f"{detail} But the base image: {exc}")
            size = f", {image.size / 1024**3:.1f} GB" if image.size else ""
            detail += f" Base image found{size}, SHA-256 {image.sha256[:12]}…"
        return CheckResult(ok=True, detail=detail)
    if response.status_code in (401, 403):
        return CheckResult(ok=False, detail="Artifactory refused the username or token.")
    if response.status_code == 404:
        return CheckResult(ok=False, detail=f"There is no PyPI repository named '{repository.pypi_repo}' there.")
    return CheckResult(ok=False, detail=f"Artifactory answered with HTTP {response.status_code}.")
