"""Builds provider instances from config.yaml entries."""

from __future__ import annotations

import os
from collections.abc import Callable

from backend.config import ProviderSettings
from backend.logging_config import get_logger
from backend.providers.anthropic import DEFAULT_MAX_TOKENS, AnthropicProvider
from backend.providers.base import (
    ChatMessage,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolSpec,
)
from backend.providers.openai import OpenAIProvider
from backend.providers.openai_compatible import OpenAICompatibleProvider
from backend.providers.vertex import (
    DEFAULT_REGION,
    VertexProvider,
    build_vertex_anthropic,
    require_project,
)

log = get_logger("providers")

NO_KEY = "none"


KeyLookup = Callable[[str], str | None]


def resolve_api_key(
    name: str, settings: ProviderSettings, lookup: KeyLookup = os.environ.get
) -> str | None:
    """Read the provider key on the host. Keys never reach a guest VM or the logs."""
    if settings.api_key_env:
        key = lookup(settings.api_key_env)
        if not key:
            raise ProviderError(
                f"Provider '{name}' has no API key. Add one in Settings, or set the "
                f"environment variable {settings.api_key_env}."
            )
        return key
    if settings.api_key and settings.api_key.lower() != NO_KEY:
        return settings.api_key
    return None


def build_provider(
    name: str, settings: ProviderSettings, model: str, lookup: KeyLookup = os.environ.get
) -> ModelProvider:
    if settings.type == "anthropic":
        # No stored key is fine: the SDK falls back to its own credential sources.
        key = lookup(settings.api_key_env) if settings.api_key_env else None
        return AnthropicProvider(
            model, key or None, settings.base_url, settings.max_tokens or DEFAULT_MAX_TOKENS
        )
    options = {"context_chars": settings.context_chars, "extra_body": settings.extra_body}
    if settings.type == "vertex-anthropic":
        return build_vertex_anthropic(
            name, settings.project, settings.region, model, settings.max_tokens or DEFAULT_MAX_TOKENS
        )
    if settings.type == "vertex":
        return VertexProvider(
            require_project(name, settings.project), settings.region or DEFAULT_REGION, model, **options
        )
    api_key = resolve_api_key(name, settings, lookup)
    if settings.type == "openai-compatible":
        if not settings.base_url:
            raise ProviderError(f"provider '{name}' has no base_url")
        return OpenAICompatibleProvider(settings.base_url, model, api_key, **options)
    if settings.type == "openai":
        return OpenAIProvider(model, api_key, settings.base_url)
    raise ProviderError(f"provider type '{settings.type}' ('{name}') is not supported")


class FallbackProvider(ModelProvider):
    """Uses a second provider when the primary one fails."""

    def __init__(self, primary: ModelProvider, fallback: ModelProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> ModelResponse:
        try:
            return await self._primary.chat(messages, tools)
        except ProviderError as exc:
            log.warning("primary model failed, using fallback", extra={"error": str(exc)})
            return await self._fallback.chat(messages, tools)
