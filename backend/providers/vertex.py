"""Google Cloud Vertex AI providers.

Two kinds of models are served through Vertex:

- Gemini and other "model garden" models, through Vertex's OpenAI-compatible
  endpoint (`VertexProvider`). Model ids look like `google/gemini-2.5-pro`.
- Claude, through Anthropic's Vertex client (`build_vertex_anthropic`).

Neither uses an API key. Both authenticate with Google Application Default
Credentials: run `gcloud auth application-default login` once on this Mac.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anthropic

from backend.providers.anthropic import AnthropicProvider
from backend.providers.base import ProviderError, ToolSpec
from backend.providers.openai_compatible import OpenAICompatibleProvider

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_REGION = "global"
ADC_ENV_VAR = "GOOGLE_APPLICATION_CREDENTIALS"
ADC_USER_FILE = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
LOGIN_HINT = "Run `gcloud auth application-default login` on this Mac."

TokenSource = Callable[[], Awaitable[str]]


def credentials_available() -> bool:
    """Whether Application Default Credentials appear to be set up (no network call)."""
    explicit = os.environ.get(ADC_ENV_VAR)
    return bool(explicit and Path(explicit).is_file()) or ADC_USER_FILE.is_file()


def openai_base_url(project: str, region: str) -> str:
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{region}/endpoints/openapi"


class GoogleTokenSource:
    """Short-lived access tokens from Application Default Credentials, refreshed as needed."""

    def __init__(self) -> None:
        self._credentials: Any = None
        self._lock = asyncio.Lock()

    def _refresh(self) -> str:
        import google.auth
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request

        try:
            if self._credentials is None:
                self._credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            if not self._credentials.valid:
                self._credentials.refresh(Request())
        except GoogleAuthError as exc:
            raise ProviderError(f"Google Cloud credentials are not usable. {LOGIN_HINT}") from exc
        return str(self._credentials.token)

    async def __call__(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._refresh)


class VertexProvider(OpenAICompatibleProvider):
    """Gemini (and other Vertex-hosted models) via Vertex's OpenAI-compatible endpoint."""

    def __init__(
        self,
        project: str,
        region: str,
        model: str,
        token_source: TokenSource | None = None,
        **options: Any,
    ) -> None:
        super().__init__(openai_base_url(project, region), model, **options)
        self._token_source = token_source or GoogleTokenSource()

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token_source()}"}

    def _wire_tool(self, tool: ToolSpec) -> dict[str, Any]:
        wire = super()._wire_tool(tool)
        # Gemini has rejected object schemas with no properties; a tool that
        # takes no arguments is declared without a schema instead.
        if not tool.parameters.get("properties"):
            del wire["function"]["parameters"]
        return wire

    async def list_models(self) -> list[str]:
        # The OpenAI-compatible endpoint has no model listing.
        return []


def require_project(name: str, project: str | None) -> str:
    if not project:
        raise ProviderError(
            f"Provider '{name}' needs a Google Cloud project. Set it under Settings."
        )
    return project


def build_vertex_anthropic(
    name: str, project: str | None, region: str | None, model: str, max_tokens: int
) -> AnthropicProvider:
    try:
        client = anthropic.AsyncAnthropicVertex(
            project_id=require_project(name, project), region=region or DEFAULT_REGION
        )
    except Exception as exc:  # noqa: BLE001 - credential discovery can fail in several ways
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError(f"Google Cloud credentials are not usable. {LOGIN_HINT}") from exc
    return AnthropicProvider(model, max_tokens=max_tokens, client=client, label="vertex")
