"""Model IDs remembered per provider, and Vertex AI's Model Garden listing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from backend.db.database import create_db_engine, init_db
from backend.providers import vertex
from backend.providers.catalog import known, remember
from tests.test_agents_api import client, settings  # noqa: F401 - fixtures


def test_models_that_worked_come_first(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "db.sqlite")
    init_db(engine)
    with Session(engine) as session:
        for model in ("zeta", "alpha", "beta"):
            remember(session, "vertex", model, "listed")
        remember(session, "vertex", "beta", "tested", worked=True)
        remember(session, "vertex", "  ", "listed")  # nothing to remember
        remember(session, "other", "elsewhere", "listed")
        session.commit()
        assert known(session, "vertex") == ["beta", "alpha", "zeta"]
        remember(session, "vertex", "zeta", "used", worked=True)
        session.commit()
        assert known(session, "vertex")[0] == "zeta"


def test_tests_agents_and_listings_are_remembered(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    from backend.api import vm as vm_api
    from backend.providers.base import ProviderError

    async def fails(*_: Any, **__: Any) -> list[str]:
        raise ProviderError("offline")

    monkeypatch.setattr(vm_api, "list_publisher_models", fails)
    monkeypatch.setattr("backend.providers.openai_compatible.OpenAICompatibleProvider.list_models", fails)
    assert client.get("/api/providers/ollama/models").json() == []  # a failed listing is no error

    created = client.post("/api/agents", json={"name": "sam", "provider": "ollama", "model": "qwen3:8b"})
    assert created.status_code == 201, created.text
    client.patch(f"/api/agents/{created.json()['id']}", json={"model": "qwen3:32b"})
    assert set(client.get("/api/providers/ollama/models").json()) == {"qwen3:8b", "qwen3:32b"}

    async def listed(*_: Any, **__: Any) -> list[str]:
        return ["llama4", "qwen3:8b"]

    monkeypatch.setattr("backend.providers.openai_compatible.OpenAICompatibleProvider.list_models", listed)
    assert sorted(client.get("/api/providers/ollama/models").json()) == ["llama4", "qwen3:32b", "qwen3:8b"]


def test_model_garden_lists_a_publishers_chat_models(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "": {"publisherModels": [{"name": "publishers/google/models/gemini-2.5-pro"},
                                 {"name": "publishers/google/models/text-embedding-005"}],
             "nextPageToken": "p2"},
        "p2": {"publisherModels": [{"name": "publishers/google/models/gemini-2.5-flash"},
                                   {"name": "publishers/google/models/imagen-4.0"}]},
    }
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("pageToken", "")])

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))

    async def token() -> str:
        return "tok"

    models = asyncio.run(vertex.list_publisher_models("google", "my-project", token))
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert seen[0].url.path == "/v1beta1/publishers/google/models"
    assert seen[0].headers["authorization"] == "Bearer tok" and seen[0].headers["x-goog-user-project"] == "my-project"

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(lambda r: httpx.Response(403)), **kw))
    with pytest.raises(vertex.ProviderError, match="403"):
        asyncio.run(vertex.list_publisher_models("anthropic", None, token))
