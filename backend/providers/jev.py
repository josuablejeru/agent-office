"""Jev decision-provider adapter.

Jev is a decision provider, not a chat model: it is asked to judge a proposed
agent action. This adapter speaks a small JSON-over-HTTP contract:

    POST {base_url}/decide
    {"state": {...}, "questions": {...}}   ->   {"answers": {...}}

The contract is this project's assumption and has not been verified against a
real Jev deployment; adjust `decide` here if the service differs.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel

from backend.providers.base import DecisionProvider, DecisionResult, ProviderError

DEFAULT_TIMEOUT_SECONDS = 20.0


class JevSettings(BaseModel):
    base_url: str
    api_key_env: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


class JevProvider(DecisionProvider):
    def __init__(
        self, settings: JevSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        if not self._settings.api_key_env:
            return {}
        key = os.environ.get(self._settings.api_key_env)
        if not key:
            raise ProviderError(f"Jev needs the environment variable {self._settings.api_key_env}")
        return {"Authorization": f"Bearer {key}"}

    async def decide(self, state: dict[str, Any], questions: dict[str, Any]) -> DecisionResult:
        url = self._settings.base_url.rstrip("/") + "/decide"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    url, json={"state": state, "questions": questions}, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Jev request failed: {exc or type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"Jev returned HTTP {response.status_code}")
        try:
            answers = response.json()["answers"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("Jev returned a response without answers") from exc
        if not isinstance(answers, dict):
            raise ProviderError("Jev returned answers that are not an object")
        return DecisionResult(answers=answers)
