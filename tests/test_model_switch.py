"""Switching a task to another model, and the list of models to switch between."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from backend.agents.runtime import RunError
from backend.db.models import Agent, Run
from backend.providers.base import ModelResponse
from backend.providers.registry import FallbackProvider
from tests.test_agents_api import client, settings  # noqa: F401 - fixtures
from tests.test_runs import ScriptedProvider, make_service


def test_a_task_runs_on_the_chosen_model_and_the_next_on_the_agents_own(tmp_path: Path) -> None:
    built: list[tuple[str, str]] = []
    service, engine, agent_id = make_service(tmp_path, ScriptedProvider())

    def factory(name: str, config: Any, model: str) -> Any:
        built.append((name, model))
        return ScriptedProvider(ModelResponse(text=f"answered by {model}"))

    service._provider_factory = factory  # noqa: SLF001

    async def scenario() -> tuple[int, int]:
        switched = service.start(agent_id, "hard question", provider="ollama", model="big-model")
        await service.wait(agent_id)
        default = service.start(agent_id, "easy question")
        await service.wait(agent_id)
        return switched, default

    switched, default = asyncio.run(scenario())
    assert built == [("ollama", "big-model"), ("ollama", "m")]
    with Session(engine) as session:
        first, second = session.get(Run, switched), session.get(Run, default)
        assert first is not None and second is not None
        assert (first.provider, first.model, first.status) == ("ollama", "big-model", "completed")
        assert (second.provider, second.model) == (None, None)  # back on the agent's own model
        from backend.providers.catalog import known

        assert set(known(session, "ollama")) >= {"big-model", "m"}  # both worked: remembered


def test_the_agents_fallback_stays_behind_a_switched_model(tmp_path: Path) -> None:
    service, engine, agent_id = make_service(tmp_path, ScriptedProvider())
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        agent.fallback_provider, agent.fallback_model = "ollama", "small-backup"
        built = service._provider(agent, Run(agent_id=agent_id, provider="ollama", model="big-model"))  # noqa: SLF001
    assert isinstance(built, FallbackProvider)


def test_a_switch_needs_a_known_provider_and_both_halves(tmp_path: Path) -> None:
    service, _, agent_id = make_service(tmp_path, ScriptedProvider())
    with pytest.raises(RunError, match="no provider 'nope'"):
        service.start(agent_id, "x", provider="nope", model="m")
    with pytest.raises(RunError, match="both"):
        service.start(agent_id, "x", provider="ollama")
    assert not service.is_busy(agent_id)


def test_the_list_of_models_to_switch_between(client: TestClient) -> None:  # noqa: F811
    assert client.get("/api/models").json() == []
    chosen = [
        {"label": "Haiku", "provider": "ollama", "model": "claude-haiku-4-5"},
        {"label": "Opus", "provider": "ollama", "model": " claude-opus-5-5 "},
    ]
    saved = client.put("/api/models", json=chosen)
    assert saved.status_code == 200, saved.text
    assert client.get("/api/models").json()[1] == {"label": "Opus", "provider": "ollama", "model": "claude-opus-5-5"}

    unknown = client.put("/api/models", json=[{"label": "X", "provider": "nope", "model": "m"}])
    assert unknown.status_code == 400 and "no provider 'nope'" in unknown.text
    twice = client.put("/api/models", json=[chosen[0], {**chosen[1], "label": "haiku"}])
    assert twice.status_code == 400 and "own name" in twice.text
    assert len(client.get("/api/models").json()) == 2  # a refused save changes nothing
