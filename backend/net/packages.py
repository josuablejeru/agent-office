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
# Either repository/path/to/image.qcow2 in Artifactory (no "..", no leading slash), or only the
# name of a repository that mirrors Debian's cloud images (a remote of cloud.debian.org).
IMAGE_PATH = (r"^$|^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
              r"|^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(/[A-Za-z0-9][A-Za-z0-9._+-]{0,200})+\.(qcow2|img|raw)$")
# The Debian cloud image the build uses; keep in step with scripts/create-base-image.sh.
DEBIAN_RELEASE = "trixie"
DEBIAN_VERSION = "13"
DEBIAN_IMAGE = f"debian-{DEBIAN_VERSION}-generic-arm64.qcow2"
# Debian's own listing of the release's builds, one dated folder each (20261123-2671/).
DEBIAN_CLOUD = "https://cloud.debian.org/images/cloud"
DEBIAN_BUILD = re.compile(r"^\d{8}-\d{1,6}$")
# Where a mirror repository keeps the release's folders, by what the remote points at:
# https://cloud.debian.org/images/cloud/, https://cloud.debian.org/, or .../cloud/trixie/ itself.
DEBIAN_LAYOUTS = (f"{DEBIAN_RELEASE}/", f"images/cloud/{DEBIAN_RELEASE}/", "")


def debian_image_file(build: str) -> str:
    """The image's file name in a dated folder: debian-13-generic-arm64-20261123-2671.qcow2."""
    return f"debian-{DEBIAN_VERSION}-generic-arm64-{build}.qcow2"


def _build_key(build: str) -> tuple[int, int]:
    date, serial = build.split("-")
    return int(date), int(serial)


def _builds_in_listing(html: str) -> set[str]:
    """Dated build folders in an HTML folder listing (Debian's or Artifactory's)."""
    found = {match.rstrip("/") for match in re.findall(r'href="(?:\./)?(\d{8}-\d{1,6})/?"', html)}
    return {build for build in found if DEBIAN_BUILD.match(build)}


class Artifactory(BaseModel):
    # The address up to and including /artifactory, e.g. https://artifactory.example.com/artifactory
    url: str = Field(min_length=8, max_length=500)
    username: str = Field(min_length=1, max_length=200)
    pypi_repo: str = Field(pattern=REPOSITORY)
    npm_repo: str = Field(default="", pattern=r"^$|" + REPOSITORY)
    # The company's (hardened) base image the agent computers are built on, as repository/path;
    # or only a repository name: Debian's cloud image through that mirror of cloud.debian.org.
    # Empty: Debian's public cloud image, straight from Debian.
    base_image: str = Field(default="", pattern=IMAGE_PATH)

    @property
    def debian_mirror_repo(self) -> str:
        """The repository mirroring Debian's cloud images, if base_image names only a repository."""
        return self.base_image if self.base_image and "/" not in self.base_image else ""

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


class DebianMirror(BaseModel):
    """Debian's cloud image through an Artifactory repository that mirrors cloud.debian.org."""

    repository: str
    # The build's dated folder, as the build script's MIRROR: SHA512SUMS and the image are in it.
    url: str
    build: str
    image: str
    # Artifactory had the image already; otherwise it fetches it from Debian on the first download.
    cached: bool = False
    # Where the newest build was learnt from: "Debian", or "Artifactory" when Debian is out of reach.
    newest_from: str = "Debian"


class ImageError(Exception):
    pass


async def debian_builds(client: httpx.AsyncClient, debian_url: str | None = None) -> set[str]:
    """The release's builds, from Debian's own listing; empty if Debian is out of reach."""
    debian_url = debian_url or DEBIAN_CLOUD
    try:
        response = await client.get(f"{debian_url}/{DEBIAN_RELEASE}/")
    except httpx.HTTPError:
        return set()
    return _builds_in_listing(response.text) if response.status_code == 200 else set()


async def _mirror_builds(
    client: httpx.AsyncClient, repository: Artifactory, auth: tuple[str, str], layout: str
) -> tuple[set[str], set[str]]:
    """(builds Artifactory has cached, builds its remote listing shows) under one layout."""
    name = repository.debian_mirror_repo
    cached: set[str] = set()
    storage = await client.get(f"{repository.url}/api/storage/{name}/{layout}".rstrip("/"), auth=auth)
    if storage.status_code in (401, 403):
        raise ImageError("Artifactory refused the username or token for the base image.")
    if storage.status_code == 200:
        try:
            children = storage.json().get("children") or []
        except ValueError:
            children = []
        cached = {str(c.get("uri", "")).strip("/") for c in children if isinstance(c, dict) and c.get("folder")}
        cached = {build for build in cached if DEBIAN_BUILD.match(build)}
    listing = await client.get(f"{repository.url}/{name}/{layout}", auth=auth)
    listed = _builds_in_listing(listing.text) if listing.status_code == 200 else set()
    return cached, listed


async def debian_mirror(
    repository: Artifactory, token: str, timeout: float = 20.0, debian_url: str | None = None
) -> DebianMirror:
    """Choose the Debian build to use and where the mirror repository serves it.

    1. The newest build, from Debian's own listing (from the Mac directly).
    2. What Artifactory has: its cache (storage API) and its remote's listing.
    3. The newest build is asked for through the mirror; a remote repository that
       does not have it yet fetches it from Debian. When Debian is out of reach,
       the newest build Artifactory knows is used.

    A dated folder never changes, unlike "latest", which some remote repositories
    cannot follow. The checksums come through the mirror from Debian (SHA512SUMS);
    the build checks the image against them.
    """
    name = repository.debian_mirror_repo
    if not name:
        raise ImageError("No Debian mirror repository is set.")
    debian_url = debian_url or DEBIAN_CLOUD
    auth = (repository.username, token)
    tried: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            public = await debian_builds(client, debian_url)
            known: dict[str, tuple[set[str], set[str]]] = {}
            for layout in DEBIAN_LAYOUTS:
                known[layout] = await _mirror_builds(client, repository, auth, layout)
            in_mirror = set().union(*(cached | listed for cached, listed in known.values()))
            candidates = sorted(public or in_mirror, key=_build_key, reverse=True)
            # The newest first; then what Artifactory already has, should the newest not come through.
            cached_all = set().union(*(cached for cached, _ in known.values()))
            candidates += sorted(cached_all - set(candidates[:1]), key=_build_key, reverse=True)
            if not candidates:
                raise ImageError(
                    f"No Debian {DEBIAN_VERSION} builds found: Debian's list ({debian_url}/{DEBIAN_RELEASE}/) "
                    f"could not be read from this Mac, and '{name}' lists none and has none cached. "
                    "It should be a remote of https://cloud.debian.org/images/cloud/."
                )
            # Layouts where builds were seen go first.
            layouts = sorted(DEBIAN_LAYOUTS, key=lambda lay: not (known[lay][0] | known[lay][1]))
            for build in dict.fromkeys(candidates[:4]):
                image = debian_image_file(build)
                for layout in layouts:
                    folder = f"{repository.url}/{name}/{layout}{build}"
                    response = await client.get(f"{folder}/SHA512SUMS", auth=auth)
                    if response.status_code in (401, 403):
                        raise ImageError("Artifactory refused the username or token for the base image.")
                    if response.status_code == 200 and any(
                        line.split()[1:2] == [image] for line in response.text.splitlines()
                    ):
                        return DebianMirror(
                            repository=name, url=folder, build=build, image=image,
                            cached=build in known[layout][0],
                            newest_from="Debian" if public else "Artifactory",
                        )
                    tried.append(f"{name}/{layout}{build}/SHA512SUMS (HTTP {response.status_code})")
    except httpx.HTTPError as exc:
        raise ImageError(f"Artifactory could not be reached: {type(exc).__name__}: {exc}"[:300]) from exc
    raise ImageError(
        f"The repository '{name}' does not serve Debian's cloud images: tried "
        + ", ".join(tried[:6]) + ". It should be a remote of https://cloud.debian.org/images/cloud/."
    )


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

    async def base_image(self) -> tuple[ImageInfo | DebianMirror, str] | None:
        """The company base image (or Debian mirror) to build from, with curl's credentials;
        None for Debian's public server.

        Raises ImageError when one is set but cannot be used: the build must not
        quietly fall back to the public image.
        """
        current = self.current()
        if current is None:
            repository = load(self._settings)
            if repository and repository.base_image:
                raise ImageError("The Artifactory token is missing: enter it under Settings › Package repository.")
            return None
        repository, token = current
        if not repository.base_image:
            return None
        if repository.debian_mirror_repo:
            return await debian_mirror(repository, token), curl_config(repository, token)
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
        if repository.debian_mirror_repo:
            try:
                mirror = await debian_mirror(repository, token, timeout)
            except ImageError as exc:
                return CheckResult(ok=False, detail=f"{detail} But the base image: {exc}")
            where = "Artifactory has it already" if mirror.cached else "Artifactory fetches it from Debian on the next build"
            newest = "the newest at Debian" if mirror.newest_from == "Debian" else "the newest Artifactory knows"
            return CheckResult(ok=True, detail=f"{detail} Base image: Debian {DEBIAN_VERSION} build {mirror.build} "
                                               f"({newest}) through '{mirror.repository}'; {where}.")
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
