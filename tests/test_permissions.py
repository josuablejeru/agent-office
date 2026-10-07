"""Per-agent permissions: what the user allows, asks to confirm, or switches off."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from backend.agents.tools import ALL_TOOLS, tools_for
from backend.db.database import create_db_engine, init_db
from backend.db.models import Agent, Approval, ToolCall
from backend.policy.engine import PolicyAction, PolicyDecision, PolicyEngine
from backend.policy.permissions import GROUP_KEYS, group_of, permissions_of, store_permissions
from backend.providers.base import DecisionProvider, DecisionResult, ModelResponse
from tests.test_runs import ScriptedProvider, call, make_service

ALLOW, ASK, REJECT = PolicyAction.ALLOW, PolicyAction.REQUIRE_APPROVAL, PolicyAction.REJECT

SAFE = ("shell.exec", {"command": "ls -la"})                 # a rule says: read-only, allow
UNKNOWN = ("shell.exec", {"command": "python3 build.py"})    # no rule covers it
DANGEROUS = ("shell.exec", {"command": "rm -rf ~/project"})  # a rule asks for approval


def decide(engine: PolicyEngine, action: tuple[str, dict[str, Any]], **settings: Any) -> PolicyDecision:
    return asyncio.run(engine.evaluate(*action, **settings))


def test_every_tool_belongs_to_a_group_the_user_can_set() -> None:
    for tool in ALL_TOOLS:
        assert tool.permission in GROUP_KEYS
        assert group_of(tool.operation) == tool.permission, tool.operation
        assert group_of(tool.spec.name) == tool.permission, tool.spec.name  # as the model names it


@pytest.mark.parametrize(("action", "level", "expected"), [
    (SAFE, "allow", ALLOW), (UNKNOWN, "allow", ALLOW), (DANGEROUS, "allow", ASK),
    (SAFE, "ask", ASK), (UNKNOWN, "ask", ASK), (DANGEROUS, "ask", ASK),
    (SAFE, "off", REJECT), (UNKNOWN, "off", REJECT), (DANGEROUS, "off", REJECT),
])
def test_levels_only_ever_tighten(action: tuple[str, dict[str, Any]], level: str, expected: PolicyAction) -> None:
    assert decide(PolicyEngine(), action, level=level).action == expected


def test_the_reason_tells_the_user_why_they_are_asked() -> None:
    asked = decide(PolicyEngine(), SAFE, level="ask")
    assert asked.source == "agent" and "Run commands" in asked.reason
    kept = decide(PolicyEngine(), DANGEROUS, level="ask")
    assert kept.source == "rule" and "recursively" in kept.reason and kept.risk == "high"
    refused = decide(PolicyEngine(), ("browser.goto", {"url": "https://x"}), level="off")
    assert refused.reason == "Browser is switched off for this agent."


def test_the_agents_setting_for_uncovered_actions_overrides_the_apps() -> None:
    lenient, strict = PolicyEngine(ALLOW), PolicyEngine(ASK)
    assert decide(lenient, UNKNOWN, unknown_action=ASK).action == ASK
    assert decide(strict, UNKNOWN, unknown_action=ALLOW).action == ALLOW
    assert decide(strict, UNKNOWN).action == ASK  # nothing chosen: the app's default
    # It is only about actions no rule covers: rules keep their verdicts.
    assert decide(lenient, SAFE, unknown_action=ASK).action == ALLOW
    assert decide(strict, DANGEROUS, unknown_action=ALLOW).action == ASK


class Jev(DecisionProvider):
    def __init__(self, **answers: Any) -> None:
        self.answers = answers

    async def decide(self, state: dict[str, Any], questions: dict[str, str]) -> DecisionResult:
        return DecisionResult(answers=self.answers)


def test_jev_cannot_loosen_what_the_user_set() -> None:
    permissive = PolicyEngine(decision_provider=Jev(allow_execution=True, action_risk="low"))
    assert decide(permissive, UNKNOWN, use_decision_provider=True, level="allow").action == ALLOW
    assert decide(permissive, UNKNOWN, use_decision_provider=True, level="ask").action == ASK
    assert decide(permissive, UNKNOWN, use_decision_provider=True, level="off").action == REJECT
    strict = PolicyEngine(decision_provider=Jev(allow_execution=False))
    assert decide(strict, UNKNOWN, use_decision_provider=True, level="ask").action == REJECT


def test_levels_decide_which_tools_the_model_is_offered() -> None:
    agent = Agent(name="a", provider="p", model="m", vm_disk_path="/d")
    assert len(tools_for(agent)) == len(ALL_TOOLS)
    store_permissions(agent, {"shell": "off", "search": "off", "databases": "ask", "channels": "off"})
    names = {tool.spec.name for tool in tools_for(agent)}
    assert not names & {"shell_exec", "web_search", "channel_read", "channel_post"}
    assert {"db_sql", "browser_goto", "file_read", "memory_recall"} <= names  # "ask" is still offered
    assert agent.perm_shell is False and agent.perm_browser is True


def test_agents_from_before_levels_keep_exactly_what_they_had(tmp_path: Path) -> None:
    """A database written by the previous version gains the columns and loses nothing."""
    path = tmp_path / "old.db"
    engine = create_db_engine(path)
    init_db(engine)
    with Session(engine) as session:
        session.add(Agent(name="everything", provider="p", model="m", vm_disk_path="/a"))
        session.add(Agent(name="no-shell", provider="p", model="m", vm_disk_path="/b", perm_shell=False))
        session.add(Agent(name="no-browser", provider="p", model="m", vm_disk_path="/c", perm_browser=False))
        session.commit()
    engine.dispose()
    with sqlite3.connect(path) as raw:  # take the new columns away again: now it is an old database
        raw.execute("ALTER TABLE agents DROP COLUMN permissions_json")
        raw.execute("ALTER TABLE agents DROP COLUMN unknown_action")

    engine = create_db_engine(path)
    init_db(engine)
    with Session(engine) as session:
        agents = {agent.name: agent for agent in session.exec(select(Agent)).all()}
        assert set(permissions_of(agents["everything"]).values()) == {"allow"}
        assert agents["everything"].unknown_action == "default"
        assert permissions_of(agents["no-shell"]) == {**dict.fromkeys(GROUP_KEYS, "allow"), "shell": "off"}
        assert permissions_of(agents["no-browser"]) == {
            **dict.fromkeys(GROUP_KEYS, "allow"), "browser": "off", "search": "off"}


def set_levels(engine: Any, agent_id: int, **levels: Any) -> None:
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        store_permissions(agent, levels)
        session.add(agent)
        session.commit()


async def wait_for_approval(engine: Any) -> Approval:
    for _ in range(150):
        with Session(engine) as session:
            approval = session.exec(select(Approval).where(Approval.status == "pending")).first()
        if approval is not None:
            return approval
        await asyncio.sleep(0.02)
    raise AssertionError("no approval was requested")


def test_ask_me_first_stops_a_harmless_command_for_approval(tmp_path: Path) -> None:
    provider = ScriptedProvider(ModelResponse(tool_calls=[call(command="echo hi")]), ModelResponse(text="ok"))
    service, engine, agent_id = make_service(tmp_path, provider)
    set_levels(engine, agent_id, shell="ask")

    async def scenario() -> None:
        service.start(agent_id, "say hi")
        approval = await wait_for_approval(engine)
        assert "confirm every action" in (approval.reason or "") and approval.id is not None
        service._approvals.resolve(approval.id, True)  # noqa: SLF001
        await service.wait(agent_id)

    asyncio.run(scenario())
    with Session(engine) as session:
        assert session.exec(select(ToolCall)).one().decision == "approved"


def test_a_change_made_while_the_agent_works_applies_to_its_next_action(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[call(command="echo one")]),
        ModelResponse(tool_calls=[call(command="echo two")]),
        ModelResponse(tool_calls=[call(command="echo three")]),
        ModelResponse(text="done"),
    )
    service, engine, agent_id = make_service(tmp_path, provider)
    set_levels(engine, agent_id, shell="ask")

    async def scenario() -> None:
        service.start(agent_id, "count")
        first = await wait_for_approval(engine)
        # While the first action waits, the user loosens and then removes the permission.
        set_levels(engine, agent_id, shell="off")
        assert first.id is not None and service._approvals.resolve(first.id, True)  # noqa: SLF001
        await service.wait(agent_id)

    asyncio.run(scenario())
    with Session(engine) as session:
        decisions = [row.decision for row in session.exec(select(ToolCall).order_by(ToolCall.id)).all()]  # type: ignore[arg-type]
        results = [row.result_json or "" for row in session.exec(select(ToolCall).order_by(ToolCall.id)).all()]  # type: ignore[arg-type]
    assert decisions == ["approved", "blocked", "blocked"]
    assert "Run commands is switched off for this agent" in results[1]


def test_a_tool_that_is_off_is_refused_even_if_the_model_calls_it(tmp_path: Path) -> None:
    provider = ScriptedProvider(ModelResponse(tool_calls=[call(command="echo sneaky")]), ModelResponse(text="ok"))
    service, engine, agent_id = make_service(tmp_path, provider)
    set_levels(engine, agent_id, shell="off")

    async def scenario() -> None:
        service.start(agent_id, "try anyway")
        await service.wait(agent_id)

    asyncio.run(scenario())
    assert "shell_exec" not in [tool.name for tool in provider.tools_seen[0]]
    with Session(engine) as session:
        refused = session.exec(select(ToolCall)).one()
        assert refused.decision == "blocked" and "switched off" in (refused.result_json or "")


# --- through the API, as the settings page uses it -------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Any:
    from fastapi.testclient import TestClient

    from backend.config import Settings
    from backend.keystore import KeyStore
    from backend.main import create_app
    from tests.test_agents_api import FakeKeychain

    app = create_app(Settings(home=tmp_path / "home"), key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        yield test_client


def test_the_settings_page_can_read_and_change_an_agents_permissions(client: Any) -> None:
    described = client.get("/api/agents/permissions").json()
    assert [group["key"] for group in described["groups"]] == list(GROUP_KEYS)
    assert described["app_default"] == "allow" and len(described["always_asks"]) >= 5

    created = client.post("/api/agents", json={"name": "pam", "provider": "ollama", "model": "m"}).json()
    assert set(created["permissions"].values()) == {"allow"} and created["unknown_action"] == "default"

    changed = client.patch(f"/api/agents/{created['id']}", json={
        "permissions": {"shell": "ask", "channels": "off"}, "unknown_action": "ask"}).json()
    assert changed["permissions"] == {**dict.fromkeys(GROUP_KEYS, "allow"), "shell": "ask", "channels": "off"}
    assert changed["unknown_action"] == "ask" and changed["perm_shell"] is True

    # Changing something else leaves them alone; an old-style switch still works.
    again = client.patch(f"/api/agents/{created['id']}", json={"system_prompt": "x", "perm_files": False}).json()
    assert again["permissions"]["shell"] == "ask" and again["permissions"]["files"] == "off"

    # Every place that returns an agent shows the same levels (the list, and VM actions too).
    assert client.get("/api/agents").json()[0]["permissions"] == again["permissions"]
    from backend.agents.models import AgentRead
    from backend.db.models import Agent as AgentRow

    row = AgentRow(name="x", provider="p", model="m", vm_disk_path="/d", perm_shell=False)
    assert AgentRead.model_validate({**row.model_dump(), "id": 1, "permissions": row.permissions,
                                     "created_at": row.created_at}).permissions["shell"] == "off"
    assert AgentRead.model_validate(row.model_copy(update={"id": 1})).permissions["shell"] == "off"

    for bad in ({"permissions": {"shell": "sometimes"}}, {"permissions": {"root": "allow"}}, {"unknown_action": "off"}):
        assert client.patch(f"/api/agents/{created['id']}", json=bad).status_code == 422


def test_a_new_agent_can_start_out_restricted(client: Any) -> None:
    created = client.post("/api/agents", json={
        "name": "intern", "provider": "ollama", "model": "m",
        "permissions": {"shell": "ask", "files": "ask"}, "unknown_action": "ask"}).json()
    assert created["permissions"]["shell"] == "ask" and created["permissions"]["browser"] == "allow"
    assert created["unknown_action"] == "ask"


# --- the two things an agent does without a tool call ----------------------------------------


@pytest.mark.parametrize(("level", "recalled"), [("allow", True), ("ask", False), ("off", False)])
def test_memory_is_only_recalled_for_an_agent_that_may_use_it(tmp_path: Path, level: str, recalled: bool) -> None:
    from tests.test_runs import FakeGuest

    guests: list[Any] = []

    class RememberingGuest(FakeGuest):
        def __init__(self, port: int, secret: str) -> None:
            super().__init__(port, secret)
            guests.append(self)

        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
            if op == "memory.context":
                self.calls.append(op)
                return {"facts": [{"id": 1, "subject": "Dana", "note": "Our accountant"}], "databases": []}
            return await super().call(op, args, timeout)

    provider = ScriptedProvider(ModelResponse(text="ok"))
    service, engine, agent_id = make_service(tmp_path, provider)
    service._client_factory = RememberingGuest  # noqa: SLF001
    set_levels(engine, agent_id, memory=level)

    async def scenario() -> None:
        service.start(agent_id, "who is Dana?")
        await service.wait(agent_id)

    asyncio.run(scenario())
    assert ("Dana: Our accountant" in provider.requests[0][0].content) is recalled
    assert ("memory.context" in [op for guest in guests for op in guest.calls]) is recalled


def test_a_mention_does_not_reach_an_agent_whose_channels_are_off(tmp_path: Path) -> None:
    from backend.db.models import Run
    from tests.test_office import ChannelWorld

    world = ChannelWorld(tmp_path)
    set_levels(world.engine, 1, channels="off")  # alice
    asyncio.run(world.say("@alice and @bob, status please?"))
    assert world.transcript() == [
        ("you", "@alice and @bob, status please?"),
        ("system", "@alice does not take part in channels (switched off in its permissions)."),
        ("bob", "On it."),
    ]
    with Session(world.engine) as session:
        assert [run.agent_id for run in session.exec(select(Run)).all()] == [2]
