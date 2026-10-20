"""Letting the model see the screen: screenshots to the model, never into storage."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from backend.agents.tools import ALL_TOOLS, VISION_TOOLS, GuestToolExecutor, tools_for
from backend.db.models import Agent
from backend.policy.permissions import group_of, store_permissions
from backend.providers.anthropic import to_request
from backend.providers.base import OMITTED_IMAGE_NOTE, ChatMessage, ToolCallRequest
from backend.providers.openai_compatible import fit_context, has_images, to_wire_messages

IMAGE = base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()


def conversation() -> list[ChatMessage]:
    """Three browser steps with screenshots; the last turn made two calls."""
    call = lambda n: ToolCallRequest(id=f"c{n}", name="browser_goto", arguments={"url": f"https://x/{n}"})  # noqa: E731
    return [
        ChatMessage(role="system", content="You are an agent."),
        ChatMessage(role="user", content="look at the dashboard"),
        ChatMessage(role="assistant", tool_calls=[call(1)]),
        ChatMessage(role="tool", tool_call_id="c1", content=json.dumps({"title": "one"}), images=[IMAGE + "1"]),
        ChatMessage(role="assistant", tool_calls=[call(2), call(3)]),
        ChatMessage(role="tool", tool_call_id="c2", content=json.dumps({"title": "two"}), images=[IMAGE + "2"]),
        ChatMessage(role="tool", tool_call_id="c3", content=json.dumps({"error": "timeout"}), images=[IMAGE + "3"]),
    ]


def test_claude_gets_the_latest_screenshots_inside_their_tool_results() -> None:
    _, wire = to_request(conversation())
    results = [block for message in wire if isinstance(message["content"], list)
               for block in message["content"] if block.get("type") == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2", "c3"]
    assert isinstance(results[0]["content"], str) and OMITTED_IMAGE_NOTE in results[0]["content"]  # oldest
    for result, image in ((results[1], IMAGE + "2"), (results[2], IMAGE + "3")):
        text, picture = result["content"]
        assert text["type"] == "text" and picture == {
            "type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}}
    assert results[2].get("is_error") is True and "is_error" not in results[1]
    # Both results of the two-call turn are in one user message, as Anthropic requires.
    assert len(wire[-1]["content"]) == 2


def test_openai_style_models_get_images_after_the_tool_results_of_a_turn() -> None:
    wire = to_wire_messages(conversation())
    roles = [message["role"] for message in wire]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "tool", "user"]
    assert OMITTED_IMAGE_NOTE in wire[3]["content"]  # nothing may come between a turn's tool results
    images = [part["image_url"]["url"] for part in wire[-1]["content"] if part["type"] == "image_url"]
    assert images == [f"data:image/jpeg;base64,{IMAGE}2", f"data:image/jpeg;base64,{IMAGE}3"]
    assert has_images(wire) and not has_images(to_wire_messages(conversation()[:2]))


def test_images_do_not_count_against_a_small_models_text_budget() -> None:
    wire = to_wire_messages(conversation())
    wire[-1]["content"][1]["image_url"]["url"] += "A" * 500_000
    shortened = fit_context([dict(message) for message in wire], budget_chars=2_000)
    assert shortened == wire  # well under the budget in text, so nothing was cut


def test_the_two_new_tools_only_come_with_sight_and_with_the_browser() -> None:
    agent = Agent(name="a", provider="p", model="m", vm_disk_path="/d")
    names = lambda: {tool.spec.name for tool in tools_for(agent)}  # noqa: E731
    assert not names() & {"browser_click_at", "browser_type_text"}
    agent.vision_enabled = True
    assert {"browser_click_at", "browser_type_text"} <= names()
    screenshot = next(tool for tool in tools_for(agent) if tool.spec.name == "browser_screenshot")
    assert "You do not receive the image" not in screenshot.spec.description
    store_permissions(agent, {"browser": "off"})
    assert not names() & {"browser_click_at", "browser_type_text", "browser_screenshot"}
    for tool in VISION_TOOLS:
        assert group_of(tool.operation) == "browser" and tool not in ALL_TOOLS


class FakeGuest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        self.calls.append((op, args or {}))
        if op == "browser.screenshot":
            return {"url": "https://x", "width": 1280, "height": 690, "image_format": "jpeg", "image_base64": IMAGE}
        return {"url": "https://x", "title": "Dashboard", "elements": []}


def executor(tmp_path: Path, vision: bool) -> tuple[GuestToolExecutor, FakeGuest]:
    guest = FakeGuest()
    agent = Agent(name="a", provider="p", model="m", vm_disk_path="/d", vision_enabled=vision)
    return GuestToolExecutor(guest, tools_for(agent), tmp_path / "shots", vision=vision), guest  # type: ignore[arg-type]


def test_with_sight_the_model_looks_when_it_chooses_to(tmp_path: Path) -> None:
    run, guest = executor(tmp_path, vision=True)
    asyncio.run(run.execute(ToolCallRequest(id="1", name="browser_goto", arguments={"url": "https://x"})))
    assert [op for op, _ in guest.calls] == ["browser.goto"] and run.take_images() == []  # no look it did not ask for

    result = asyncio.run(run.execute(ToolCallRequest(id="2", name="browser_screenshot", arguments={})))
    assert guest.calls[-1] == ("browser.screenshot", {"marks": True})  # numbers drawn on what it sees
    assert run.take_images() == [IMAGE] and run.take_images() == []  # handed over once
    assert IMAGE not in json.dumps(result)  # the stored result never holds the image
    assert result["view"] == {"width": 1280, "height": 690} and (tmp_path / "shots" / result["screenshot"]).exists()

    asyncio.run(run.execute(ToolCallRequest(id="3", name="browser_click_at", arguments={"x": 10, "y": 20})))
    assert guest.calls[-1] == ("browser.click_at", {"x": 10, "y": 20}) and run.take_images() == []


def test_without_sight_nothing_changes(tmp_path: Path) -> None:
    run, guest = executor(tmp_path, vision=False)
    asyncio.run(run.execute(ToolCallRequest(id="1", name="browser_goto", arguments={"url": "https://x"})))
    assert [op for op, _ in guest.calls] == ["browser.goto"] and run.take_images() == []
    shot = asyncio.run(run.execute(ToolCallRequest(id="2", name="browser_screenshot", arguments={})))
    assert guest.calls[-1] == ("browser.screenshot", {}) and run.take_images() == [] and "screenshot" in shot


def test_a_run_gives_the_model_the_image_and_stores_none(tmp_path: Path) -> None:
    from backend.providers.base import ModelResponse
    from tests.test_runs import ScriptedProvider, make_service

    provider = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCallRequest(id="c1", name="browser_screenshot", arguments={})]),
        ModelResponse(text="The chart shows growth."),
    )
    service, engine, agent_id = make_service(tmp_path, provider)
    service._client_factory = lambda port, secret: FakeGuest()  # noqa: SLF001
    from sqlmodel import Session, select

    from backend.db.models import ToolCall

    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        agent.vision_enabled = True
        session.add(agent)
        session.commit()

    async def scenario() -> None:
        service.start(agent_id, "what does the chart show?")
        await service.wait(agent_id)

    asyncio.run(scenario())
    second = provider.requests[1]
    assert second[-1].role == "tool" and second[-1].images == [IMAGE]
    assert "You can see the screen when you choose to" in second[0].content
    with Session(engine) as session:
        stored = session.exec(select(ToolCall)).one().result_json or ""
    assert IMAGE not in stored and "screenshot" in stored
