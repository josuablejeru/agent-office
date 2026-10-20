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
import json
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


def credentials_file() -> tuple[Path, str]:
    """Where Google's libraries look for Application Default Credentials, and why there."""
    explicit = os.environ.get(ADC_ENV_VAR)
    if explicit:
        return Path(explicit).expanduser(), f"{ADC_ENV_VAR}"
    config_dir = os.environ.get("CLOUDSDK_CONFIG")
    if config_dir:
        return Path(config_dir).expanduser() / ADC_USER_FILE.name, "CLOUDSDK_CONFIG"
    return ADC_USER_FILE, "gcloud's default folder"


def credentials_available() -> bool:
    """Whether Application Default Credentials appear to be set up (no network call)."""
    return credentials_file()[0].is_file()


def credentials_summary() -> str:
    """Where the credentials are and what kind, in a sentence (never their contents)."""
    path, why = credentials_file()
    if not path.is_file():
        return f"No credentials file at {path} (looked there because of {why})."
    try:
        kind = str(json.loads(path.read_text()).get("type", "unknown"))
    except (OSError, ValueError):
        kind = "unreadable"
    return f"Credentials: {path} (type {kind}, found through {why})."


def describe_google_error(exc: Exception) -> str:
    """Why Google sign-in failed, in words someone can act on."""
    from google.auth import exceptions

    message = " ".join(str(exc).split())[:300]
    if isinstance(exc, exceptions.DefaultCredentialsError):
        return f"No usable Google Cloud credentials. {credentials_summary()} {LOGIN_HINT}"
    if isinstance(exc, exceptions.RefreshError) and any(
        word in message for word in ("invalid_grant", "reauth", "Reauthentication", "expired")
    ):
        return f"Your Google Cloud sign-in has expired ({message}). {LOGIN_HINT} {credentials_summary()}"
    if isinstance(exc, exceptions.TransportError):
        return (
            f"Google's sign-in service could not be reached ({message}). On a company network this is "
            "often a proxy or certificate setting. " + credentials_summary()
        )
    return f"Google Cloud sign-in failed: {type(exc).__name__}: {message}. {credentials_summary()}"


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
            self._credentials = None  # look again next time: the user may have signed in meanwhile
            raise ProviderError(describe_google_error(exc)) from exc
        return str(self._credentials.token)

    async def __call__(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._refresh)


def vertex_model_id(model: str) -> str:
    """The model as Vertex's OpenAI-compatible endpoint names it: publisher/model.

    Gemini models are often written without the publisher; Claude models do not
    work through this endpoint at all, and Google's own error does not say so.
    """
    model = model.strip()
    if "/" in model:
        return model
    if model.startswith("claude"):
        raise ProviderError(
            f"'{model}' is a Claude model: on Vertex AI, Claude models go through the "
            "vertex-claude provider. Choose that provider for this model."
        )
    if model.startswith(("gemini", "gemma")):
        return f"google/{model}"
    raise ProviderError(
        f"Vertex AI needs the model as publisher/model, for example google/gemini-2.5-pro (not '{model}')."
    )


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
        super().__init__(openai_base_url(project, region), vertex_model_id(model), **options)
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
        raise ProviderError(describe_google_error(exc)) from exc
    return AnthropicProvider(model, max_tokens=max_tokens, client=client, label="vertex")
