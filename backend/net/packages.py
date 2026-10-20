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
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from backend.config import Settings
from backend.keystore import KeyStore

TOKEN_KEY = "ARTIFACTORY_TOKEN"
CONFIG_PATH = ["packages", "artifactory"]
REPOSITORY = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class Artifactory(BaseModel):
    # The address up to and including /artifactory, e.g. https://artifactory.example.com/artifactory
    url: str = Field(min_length=8, max_length=500)
    username: str = Field(min_length=1, max_length=200)
    pypi_repo: str = Field(pattern=REPOSITORY)
    npm_repo: str = Field(default="", pattern=r"^$|" + REPOSITORY)

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
        return CheckResult(ok=True, detail=f"Connected: '{repository.pypi_repo}' serves Python packages.")
    if response.status_code in (401, 403):
        return CheckResult(ok=False, detail="Artifactory refused the username or token.")
    if response.status_code == 404:
        return CheckResult(ok=False, detail=f"There is no PyPI repository named '{repository.pypi_repo}' there.")
    return CheckResult(ok=False, detail=f"Artifactory answered with HTTP {response.status_code}.")
