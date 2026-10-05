"""OpenAI provider: the OpenAI-compatible adapter pointed at api.openai.com."""

from __future__ import annotations

from backend.providers.openai_compatible import OpenAICompatibleProvider

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, model: str, api_key: str | None, base_url: str | None = None) -> None:
        super().__init__(base_url or OPENAI_BASE_URL, model, api_key)
