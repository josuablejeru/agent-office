"""Automatic model choice (routing) and escalation to a stronger model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from backend.agents.routing import (
    ESCALATE_TOOL,
    RoutingSettings,
    choose_model,
    failures_in_a_row,
    model_list,
    parse_route,
    router_messages,
)
from backend.db.models import Run
from backend.providers.anthropic import _assistant_content
from backend.providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError, ToolCallRequest
from backend.providers.history import flatten_tool_turns
from backend.providers.openai_compatible import to_wire_messages
from tests.test_runs import FakeGuest, ScriptedProvider, make_service

MODELS = [
    {"label": "Haiku", "provider": "ollama", "model": "haiku"},
    {"label": "Opus", "provider": "ollama", "model": "opus"},
]
ROUTING = {
    "router": "Haiku",
    "routes": [{"when": "Quick questions and lookups", "use": "Haiku"},
               {"when": "Coding and research with many steps", "use": "Opus"}],
    "escalate_to": "Opus",
}


def shell(index: int) -> ToolCallRequest:
    return ToolCallRequest(id=f"t{index}", name="shell_exec", arguments={"command": f"echo try {index}"})


def test_route_numbers_are_read_strictly() -> None:
    assert parse_route("2", 3) == 2 and parse_route("Route 2.", 3) == 2
    assert parse_route("<think>1 or 3?</think>3", 3) == 3
    assert parse_route("0", 3) == 0
    assert parse_route("4", 3) is None and parse_route("1 or 2", 3) is None and parse_route("Opus", 3) is None


def test_the_router_sees_only_the_users_words() -> None:
    routing = RoutingSettings.model_validate(ROUTING)
    sent = router_messages(routing, ["first ask", "x" * 10_000 + "latest ask"])
    assert "1. Quick questions and lookups" in sent[0].content and "2. Coding" in sent[0].content
    assert sent[1].content.count("x") <= 3000 and "latest ask" in sent[1].content


class Answers(ModelProvider):
    def __init__(self, text: str = "", delay: float = 0.0, error: bool = False) -> None:
        self.text, self.delay, self.error = text, delay, error

    async def chat(self, messages: list[ChatMessage], tools: Any = None) -> ModelResponse:
        await asyncio.sleep(self.delay)
        if self.error:
            raise ProviderError("quota")
        return ModelResponse(text=self.text)


@pytest.mark.parametrize(
    ("router", "chosen", "why"),
    [
        (Answers("2"), "Opus", "Coding"),
        (Answers("0"), None, "no route fits"),
        (Answers("I'd say Opus"), None, "not a route number"),
        (Answers("1", delay=5), None, "did not choose"),
        (Answers(error=True), None, "could not choose"),
    ],
)
def test_choosing_falls_back_to_the_agents_own_model_on_any_doubt(router: Any, chosen: str | None, why: str) -> None:
    from backend.agents.routing import ModelRef

    models = [ModelRef.model_validate(m) for m in MODELS]
    choice, note = asyncio.run(
        choose_model(RoutingSettings.model_validate(ROUTING), models, ["task"], lambda p, m: router, timeout=0.5)
    )
    assert (choice.label if choice else None) == chosen and why in note


def test_refusals_never_count_as_failures() -> None:
    def result(content: dict[str, Any]) -> ChatMessage:
        return ChatMessage(role="tool", content=json.dumps(content), tool_call_id="x")

    failed = result({"error": "connection reset"})
    exit_2 = result({"exit_code": 2, "stderr": "no such file"})
    no_match = result({"exit_code": 1, "stdout": ""})  # grep found nothing: an answer
    timed_out = result({"exit_code": -1, "timed_out": True})
    rejected = result({"error": "The user rejected this action. Do not retry it; ask how to proceed."})
    blocked = result({"error": "This action was blocked by policy: rm -rf"})
    ok = result({"exit_code": 0})
    call = ChatMessage(role="assistant", tool_calls=[shell(1)])
    assert failures_in_a_row([call, failed, call, exit_2, call, timed_out]) == 3
    assert failures_in_a_row([call, failed, call, no_match]) == 0
    assert failures_in_a_row([call, failed, call, rejected, call, blocked]) == 0
    assert failures_in_a_row([call, failed, call, ok]) == 0


def test_flattened_tool_turns_suit_both_message_formats() -> None:
    history = [
        ChatMessage(role="system", content="Be brief."),
        ChatMessage(role="user", content="list files"),
        ChatMessage(role="assistant", content="Looking.", tool_calls=[shell(1), shell(2)],
                    provider_data={"anthropic_content": [{"type": "thinking", "thinking": "…", "signature": "sig"}]}),
        ChatMessage(role="tool", content='{"exit_code": 0, "stdout": "a"}', tool_call_id="t1", images=["aW1n"]),
        ChatMessage(role="tool", content='{"exit_code": 0, "stdout": "b"}', tool_call_id="t2"),
        ChatMessage(role="assistant", tool_calls=[shell(3)],
                    provider_data={"openai_tool_calls": [{"id": "t3", "extra_content": {"google": {"thought_signature": "s"}}}]}),
        ChatMessage(role="tool", content='{"error": "boom"}', tool_call_id="t3"),
    ]
    flat = flatten_tool_turns(history)
    # The task, then one report from the user's side: no assistant turns a model could imitate.
    assert [m.role for m in flat] == ["system", "user"]
    assert not any(m.tool_calls or m.provider_data or m.tool_call_id or m.images for m in flat)
    report = flat[1].content
    assert report.startswith("list files") and "done on this task so far" in report
    assert "echo try 1" in report and "screenshot" in report and "boom" in report and "Continue the task" in report
    wire = json.dumps(to_wire_messages(flat))
    assert "tool_calls" not in wire and "thought_signature" not in wire
    claude_turns = json.dumps([_assistant_content(m) for m in flat if m.role == "assistant"])
    assert "tool_use" not in claude_turns and "thinking" not in claude_turns


def routed_service(tmp_path: Path, scripts: dict[str, ScriptedProvider], guest: Any = FakeGuest) -> tuple[Any, Any, int]:
    service, engine, agent_id = make_service(tmp_path, ScriptedProvider())
    service._settings.update_user_config(["models"], MODELS)  # noqa: SLF001
    service._settings.update_user_config(["routing"], ROUTING)  # noqa: SLF001
    service._provider_factory = lambda name, config, model: scripts[model]  # noqa: SLF001
    service._client_factory = guest  # noqa: SLF001
    return service, engine, agent_id


def run_once(service: Any, agent_id: int, **options: Any) -> int:
    async def scenario() -> int:
        run_id = service.start(agent_id, "refactor the billing module", **options)
        await service.wait(agent_id)
        return run_id

    return asyncio.run(scenario())


def test_auto_runs_the_task_on_the_routed_model(tmp_path: Path) -> None:
    scripts = {"haiku": ScriptedProvider(ModelResponse(text="2")), "opus": ScriptedProvider(ModelResponse(text="Done by Opus.")),
               "m": ScriptedProvider(ModelResponse(text="Done by the default."))}
    service, engine, agent_id = routed_service(tmp_path, scripts)
    run_id = run_once(service, agent_id, auto=True)
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and (run.model, run.status) == ("opus", "completed")
        assert run.route_note is not None and "Auto chose Opus" in run.route_note
    assert scripts["m"].requests == []
    # The router got the user's words and no tools.
    assert scripts["haiku"].tools_seen == [[]] and "refactor the billing" in scripts["haiku"].requests[0][-1].content


def test_a_model_chosen_by_hand_wins_over_auto(tmp_path: Path) -> None:
    scripts = {"haiku": ScriptedProvider(ModelResponse(text="1")), "opus": ScriptedProvider(ModelResponse(text="Opus here."))}
    service, engine, agent_id = routed_service(tmp_path, scripts)
    run_id = run_once(service, agent_id, auto=True, provider="ollama", model="opus")
    assert scripts["haiku"].requests == []  # no routing call at all
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and run.model == "opus" and not run.auto


class FailingGuest(FakeGuest):
    async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self.calls.append(op)
        return {"exit_code": 2, "stderr": "not found"}


def test_after_three_failed_actions_the_stronger_model_takes_over_once(tmp_path: Path) -> None:
    haiku = ScriptedProvider(ModelResponse(text="1"), *(ModelResponse(tool_calls=[shell(i)]) for i in range(1, 6)))
    opus = ScriptedProvider(ModelResponse(tool_calls=[shell(10)]), ModelResponse(tool_calls=[shell(11)]),
                            ModelResponse(tool_calls=[shell(12)]), ModelResponse(tool_calls=[shell(13)]),
                            ModelResponse(text="Fixed it."))
    service, engine, agent_id = routed_service(tmp_path, {"haiku": haiku, "opus": opus}, FailingGuest)
    run_id = run_once(service, agent_id, auto=True)
    # Router call + three failing actions on Haiku, then Opus to the end, despite failing too.
    assert len(haiku.requests) == 1 + 3
    assert len(opus.requests) == 5
    first = opus.requests[0]
    assert not any(m.tool_calls or m.role in ("tool", "assistant") for m in first)  # Haiku's turns as a report
    assert "echo try 3" in first[-1].content
    assert ESCALATE_TOOL not in {t.name for t in opus.tools_seen[0]}
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and run.model == "opus" and run.status == "completed"
        assert run.route_note is not None and "escalated to Opus: 3 actions in a row failed" in run.route_note


def test_the_model_can_ask_for_the_stronger_one_without_any_dialog(tmp_path: Path) -> None:
    ask = ToolCallRequest(id="e1", name=ESCALATE_TOOL, arguments={"reason": "this needs deeper analysis"})
    haiku = ScriptedProvider(ModelResponse(text="1"), ModelResponse(tool_calls=[ask]))
    opus = ScriptedProvider(ModelResponse(text="Deep answer."))
    service, engine, agent_id = routed_service(tmp_path, {"haiku": haiku, "opus": opus})
    assert ESCALATE_TOOL  # offered to the routed model:
    run_id = run_once(service, agent_id, auto=True)
    assert ESCALATE_TOOL in {t.name for t in haiku.tools_seen[1]}
    with Session(engine) as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status == "completed" and run.model == "opus"
        assert run.route_note is not None and "this needs deeper analysis" in run.route_note
        from backend.db.models import Approval

        assert session.exec(__import__("sqlmodel").select(Approval)).all() == []  # never asked the user


def test_no_escalation_when_auto_already_chose_the_strong_model(tmp_path: Path) -> None:
    opus = ScriptedProvider(ModelResponse(text="Opus answer."))
    service, _, agent_id = routed_service(tmp_path, {"haiku": ScriptedProvider(ModelResponse(text="2")), "opus": opus})
    run_once(service, agent_id, auto=True)
    assert ESCALATE_TOOL not in {t.name for t in opus.tools_seen[0]}


def test_routing_settings_must_name_models_from_the_list(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from backend.config import Settings
    from backend.keystore import KeyStore
    from backend.main import create_app
    from tests.test_agents_api import FakeKeychain

    app = create_app(Settings(home=tmp_path / "home"), key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        assert client.put("/api/models", json=MODELS).status_code == 200
        bad = client.put("/api/routing", json={**ROUTING, "escalate_to": "Gemini"})
        assert bad.status_code == 400 and "Gemini" in bad.text
        assert client.put("/api/routing", json={**ROUTING, "router": ""}).status_code == 400
        assert client.put("/api/routing", json=ROUTING).status_code == 200
        assert client.get("/api/routing").json()["escalate_to"] == "Opus"
        renamed = client.put("/api/models", json=[MODELS[0], {**MODELS[1], "label": "Opus 5"}])
        assert renamed.status_code == 400 and "used by Automatic model choice" in renamed.text
        assert [m.label for m in model_list(app.state.settings)] == ["Haiku", "Opus"]
