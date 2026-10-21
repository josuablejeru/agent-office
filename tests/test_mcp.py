"""MCP servers: a central registry, and agents using the tools of the servers allowed for them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select

from backend.config import Settings
from backend.db.models import Agent, Approval, ToolCall
from backend.keystore import KeyStore
from backend.net.mcp import MCPRegistry, MCPServerConfig, clean_schema, tool_name
from backend.providers.base import ModelResponse, ToolCallRequest
from tests.fake_mcp import FakeMCPServer
from tests.test_agents_api import FakeKeychain
from tests.test_runs import ScriptedProvider, make_service


@pytest.fixture(scope="module")
def tickets() -> Any:
    server = FakeMCPServer()
    yield server
    server.close()


def registry(tmp_path: Path) -> tuple[MCPRegistry, FakeKeychain, Settings]:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    keychain = FakeKeychain()
    return MCPRegistry(settings, KeyStore(run=keychain)), keychain, settings


def test_tool_names_suit_every_model_provider() -> None:
    assert tool_name("tickets", "find_ticket") == "mcp_tickets_find_ticket"
    assert tool_name("jira-cloud", "search.issues") == "mcp_jira_cloud_search_issues"
    long = tool_name("a" * 30, "b" * 60)
    assert len(long) <= 64 and long != tool_name("a" * 30, "b" * 59 + "c")


def test_schemas_lose_keys_models_reject() -> None:
    schema = {"$schema": "x", "title": "T", "type": "object", "additionalProperties": False,
              "properties": {"n": {"type": "integer", "title": "N"}}}
    assert clean_schema(schema) == {"type": "object", "properties": {"n": {"type": "integer"}}}


def test_servers_are_registered_with_their_token_in_the_keychain(tmp_path: Path) -> None:
    mcp, keychain, settings = registry(tmp_path)
    server = MCPServerConfig(name="tickets", url="https://mcp.example.com/mcp")
    mcp.save(server, "mcp-secret")
    assert [s.name for s in mcp.servers()] == ["tickets"] and mcp.has_token(server)
    assert "mcp-secret" not in settings.config_path.read_text()  # only in the Keychain
    assert keychain.items["MCP_TICKETS_TOKEN"] == "mcp-secret"
    assert server.headers("mcp-secret") == {"Authorization": "Bearer mcp-secret"}
    assert MCPServerConfig(name="x", url="https://y/mcp", auth_header="X-Api-Key").headers("k") == {"X-Api-Key": "k"}
    assert mcp.remove("tickets") and mcp.servers() == [] and "MCP_TICKETS_TOKEN" not in keychain.items
    for bad in ({"name": "Bad Name", "url": "https://y/mcp"}, {"name": "ok", "url": "file:///etc/passwd"}):
        with pytest.raises(ValueError):
            MCPServerConfig(**bad)


def test_testing_a_server_lists_its_tools_or_says_what_is_wrong(tmp_path: Path, tickets: FakeMCPServer) -> None:
    mcp, _, _ = registry(tmp_path)
    server = MCPServerConfig(name="tickets", url=tickets.url)
    ok = asyncio.run(mcp.test(server, "mcp-secret"))
    assert ok["ok"] and sorted(ok["tools"]) == ["close_ticket", "find_ticket"]
    refused = asyncio.run(mcp.test(server, "wrong"))
    assert not refused["ok"] and "refused the token" in refused["detail"]
    nowhere = asyncio.run(mcp.test(MCPServerConfig(name="x", url="http://127.0.0.1:9/mcp"), "t"))
    assert not nowhere["ok"] and "could not be reached" in nowhere["detail"]


def test_tool_calls_come_back_as_results_never_as_crashes(tmp_path: Path, tickets: FakeMCPServer) -> None:
    mcp, _, _ = registry(tmp_path)
    mcp.save(MCPServerConfig(name="tickets", url=tickets.url), "mcp-secret")
    found = asyncio.run(mcp.call("tickets", "find_ticket", {"number": 42}))
    assert found["result"] == "Ticket 42: the printer on floor 2 jams. Assigned to Ben." and "error" not in found
    failed = asyncio.run(mcp.call("tickets", "close_ticket", {"number": 0}))
    assert "close_ticket" in failed["error"]  # the server keeps its internal error to itself
    gone = asyncio.run(mcp.call("nope", "find_ticket", {}))
    assert "no longer set up" in gone["error"]


def agent_run(tmp_path: Path, tickets: FakeMCPServer, level: str | None, answer: bool | None) -> tuple[Any, Any, int]:
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCallRequest(id="c1", name="mcp_tickets_find_ticket", arguments={"number": 7})]),
        ModelResponse(text="Ticket 7 is about the printer."),
    )
    service, engine, agent_id = make_service(tmp_path, provider)
    mcp = MCPRegistry(service._settings, KeyStore(run=FakeKeychain()))  # noqa: SLF001
    mcp.save(MCPServerConfig(name="tickets", url=tickets.url), "mcp-secret")
    service.mcp = mcp
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
        assert agent is not None
        agent.mcp_servers_json = json.dumps(["tickets"])
        if level:
            from backend.policy.permissions import store_permissions

            store_permissions(agent, {"mcp": level})
        session.add(agent)
        session.commit()

    async def scenario() -> None:
        service.start(agent_id, "what is ticket 7 about?")
        if answer is not None:
            for _ in range(200):
                with Session(engine) as session:
                    pending = session.exec(select(Approval).where(Approval.status == "pending")).first()
                if pending is not None:
                    assert pending.id is not None and service._approvals.resolve(pending.id, answer)  # noqa: SLF001
                    break
                await asyncio.sleep(0.02)
        await service.wait(agent_id)

    asyncio.run(scenario())
    return provider, engine, agent_id


def test_by_default_an_agent_asks_before_using_an_mcp_tool(tmp_path: Path, tickets: FakeMCPServer) -> None:
    before = len(tickets.calls)
    provider, engine, _ = agent_run(tmp_path, tickets, level=None, answer=True)
    offered = {tool.name for tool in provider.tools_seen[0]}
    assert {"mcp_tickets_find_ticket", "mcp_tickets_close_ticket"} <= offered
    with Session(engine) as session:
        call = session.exec(select(ToolCall)).one()
        approval = session.exec(select(Approval)).one()
    assert call.decision == "approved" and call.tool == "mcp.tickets.find_ticket"
    assert "Assigned to Ben" in (call.result_json or "") and len(tickets.calls) == before + 1
    assert approval.status == "approved"
    # The token never reaches the database or the model.
    assert "mcp-secret" not in (call.result_json or "") + (call.arguments_json or "")
    assert "mcp-secret" not in json.dumps([[m.model_dump() for m in r] for r in provider.requests])


def test_allowed_runs_at_once_and_rejected_never_reaches_the_server(tmp_path: Path, tickets: FakeMCPServer) -> None:
    before = len(tickets.calls)
    agent_run(tmp_path / "allowed", tickets, level="allow", answer=None)
    assert len(tickets.calls) == before + 1
    _, engine, _ = agent_run(tmp_path / "rejected", tickets, level=None, answer=False)
    with Session(engine) as session:
        assert session.exec(select(ToolCall)).one().decision == "rejected"
    assert len(tickets.calls) == before + 1  # the rejected call never went out


def test_not_allowed_means_the_tools_are_not_offered(tmp_path: Path, tickets: FakeMCPServer) -> None:
    provider, engine, _ = agent_run(tmp_path, tickets, level="off", answer=None)
    assert not [tool for tool in provider.tools_seen[0] if tool.name.startswith("mcp_")]
    with Session(engine) as session:
        assert session.exec(select(ToolCall)).one().decision == "blocked"


def test_the_registry_api(tmp_path: Path, tickets: FakeMCPServer) -> None:
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app(Settings(home=tmp_path / "home"), key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        form = {"name": "tickets", "url": tickets.url, "token": "mcp-secret"}
        assert client.post("/api/mcp/servers/test", json=form).json()["ok"]
        saved = client.put("/api/mcp/servers/tickets", json=form).json()
        assert saved["has_token"] and "token" not in saved
        assert [s["name"] for s in client.get("/api/mcp/servers").json()] == ["tickets"]
        assert client.post("/api/mcp/servers/test", json={**form, "token": ""}).json()["ok"]  # uses the stored token
        assert client.put("/api/mcp/servers/x", json={**form, "name": "x", "url": "nope"}).status_code == 400
        agent = client.post("/api/agents", json={"name": "ben", "provider": "ollama", "model": "m",
                                                 "mcp_servers": ["tickets"]}).json()
        assert agent["mcp_servers"] == ["tickets"] and agent["permissions"]["mcp"] == "ask"
        assert client.delete("/api/mcp/servers/tickets").status_code == 204
        assert client.get("/api/mcp/servers").json() == []
