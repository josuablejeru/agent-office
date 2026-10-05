"""Tests for robustness features, channels, avatars and Vertex AI."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import namedtuple
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from backend.agents.channels import ChannelService, mentions
from backend.agents.runtime import MAX_MENTION_HOPS, RunService
from backend.config import ProviderSettings, Settings, merge_config
from backend.db.database import create_db_engine, init_db, migrate
from backend.db.models import Agent, ChannelMessage, Run
from backend.instance import AlreadyRunning, acquire_instance_lock, is_running
from backend.keystore import KeyStore
from backend.main import create_app
from backend.policy.approvals import ApprovalBroker
from backend.policy.engine import PolicyEngine
from backend.providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError, ToolSpec
from backend.providers.openai_compatible import RAW_TOOL_CALLS, parse_response, to_wire_message
from backend.providers.registry import build_provider
from backend.providers.vertex import VertexProvider, openai_base_url
from backend.vm.errors import VMError
from backend.vm.lifecycle import CONSOLE_LOG_MAX_BYTES, QemuVMManager, VMStatus, trim_log
from tests.test_agents_api import FakeKeychain, payload

Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def app_client(settings: Settings):  # type: ignore[no-untyped-def]
    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        yield client, app


# --- database ---------------------------------------------------------------------------------


def test_migration_adds_new_columns_to_an_old_database(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "old.db")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE agents (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, "
            "system_prompt VARCHAR NOT NULL, provider VARCHAR NOT NULL, model VARCHAR NOT NULL, "
            "vm_disk_path VARCHAR NOT NULL)"))
        connection.execute(text(
            "INSERT INTO agents VALUES (1, 'old-timer', 'p', 'ollama', 'm', '/d.qcow2')"))
    init_db(engine)
    with Session(engine) as session:
        agent = session.exec(select(Agent)).one()
        assert agent.name == "old-timer" and agent.avatar == "manager" and agent.perm_shell is True
        assert agent.vm_memory_mb == 4096 and agent.fallback_model is None
    assert migrate(engine) == []  # nothing left to do the second time
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_migration_keeps_the_real_database_intact(tmp_path: Path) -> None:
    real = Settings.from_env().db_path
    if not real.exists():
        pytest.skip("no installed database on this machine")
    copy = tmp_path / "copy.db"
    shutil.copyfile(real, copy)
    engine = create_db_engine(copy)
    with engine.connect() as connection:
        before = connection.execute(text("SELECT id, name FROM agents ORDER BY id")).all()
    init_db(engine)
    with Session(engine) as session:
        after = [(a.id, a.name) for a in session.exec(select(Agent).order_by(Agent.id)).all()]  # type: ignore[arg-type]
    assert after == [tuple(row) for row in before]


# --- config, instance lock, shutdown ----------------------------------------------------------


def test_user_config_overlays_defaults_and_edits_touch_only_the_user_file(settings: Settings) -> None:
    assert merge_config({"a": {"x": 1, "y": 2}, "b": 1}, {"a": {"y": 3}, "c": 4}) == {
        "a": {"x": 1, "y": 3}, "b": 1, "c": 4}
    settings.home.mkdir(parents=True)
    settings.config_path.write_text("providers:\n  ollama:\n    type: openai-compatible\n    base_url: http://x/v1\n")
    providers = settings.load_providers()
    assert providers["ollama"].base_url == "http://x/v1"  # the user's value wins
    assert "vertex" in providers and providers["vertex"].region == "global"  # new defaults appear
    settings.update_user_config(["app", "keep_vms_running_on_quit"], True)
    assert settings.load_config()["app"]["keep_vms_running_on_quit"] is True
    assert "vertex" not in settings.load_user_config()["providers"]


def test_only_one_backend_per_data_directory(settings: Settings) -> None:
    assert not is_running(settings)
    held = acquire_instance_lock(settings)
    assert is_running(settings)
    with pytest.raises(AlreadyRunning):
        acquire_instance_lock(settings)
    held.close()
    assert not is_running(settings)


def test_shutdown_leaves_vms_alone_when_told_to(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import shutdown

    settings.ensure_layout()
    init_db(create_db_engine(settings.db_path))
    calls: list[str] = []

    async def fake_stop_all(_settings: Settings, vm_manager: Any = None) -> int:
        calls.append("stopped")
        return 0

    monkeypatch.setattr(shutdown, "stop_all_vms", fake_stop_all)
    monkeypatch.setenv("AGENT_OFFICE_HOME", str(settings.home))

    shutdown.main()
    assert calls == ["stopped"]
    held = acquire_instance_lock(settings)  # e.g. a dev server is using the VMs
    shutdown.main()
    held.close()
    settings.update_user_config(["app", "keep_vms_running_on_quit"], True)
    shutdown.main()
    assert calls == ["stopped"]


# --- VM guards --------------------------------------------------------------------------------


def test_vm_is_not_started_when_the_disk_is_nearly_full(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.ensure_layout()
    agent = Agent(id=1, name="a1", provider="p", model="m", vm_daemon_port=18000,
                  vm_disk_path=str(settings.agent_dir("a1") / "disk.qcow2"))
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: Usage(100, 99, 2 * 1024**3))
    with pytest.raises(VMError, match="2.0 GB of disk space is free"):
        asyncio.run(QemuVMManager(settings).start(agent))


def test_console_log_is_trimmed_to_its_tail(tmp_path: Path) -> None:
    log = tmp_path / "console.log"
    log.write_bytes(b"a" * CONSOLE_LOG_MAX_BYTES + b"THE-END")
    trim_log(log)
    assert log.stat().st_size < CONSOLE_LOG_MAX_BYTES and log.read_bytes().endswith(b"THE-END")
    small = tmp_path / "small.log"
    small.write_text("keep me")
    trim_log(small)
    assert small.read_text() == "keep me"
    trim_log(tmp_path / "missing.log")


# --- API --------------------------------------------------------------------------------------


def test_requests_for_other_hostnames_are_refused(app_client: Any) -> None:
    client, _ = app_client
    assert client.get("/api/agents", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/agents", headers={"Host": "localhost:8000"}).status_code == 200


def test_custom_avatar_photo_upload(app_client: Any, settings: Settings) -> None:
    client, _ = app_client
    agent_id = client.post("/api/agents", json=payload(avatar="accountant")).json()["id"]
    assert client.get(f"/api/agents/{agent_id}").json()["avatar"] == "accountant"
    assert client.get(f"/api/agents/{agent_id}/avatar").status_code == 404

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    uploaded = client.put(f"/api/agents/{agent_id}/avatar", content=png)
    assert uploaded.status_code == 200 and uploaded.json()["avatar"] == "custom"
    assert client.get(f"/api/agents/{agent_id}/avatar").content == png
    jpeg = b"\xff\xd8\xff" + b"\x00" * 64
    client.put(f"/api/agents/{agent_id}/avatar", content=jpeg)
    assert [p.name for p in settings.agent_dir("research-agent").glob("avatar.*")] == ["avatar.jpg"]

    assert client.put(f"/api/agents/{agent_id}/avatar", content=b"<svg onload=alert(1)>").status_code == 400
    assert client.put(f"/api/agents/{agent_id}/avatar", content=png + b"0" * 4_000_000).status_code == 413
    assert client.post("/api/agents", json=payload(name="x", avatar="../../etc")).status_code == 422


def test_settings_and_vertex_location_are_saved(app_client: Any, settings: Settings) -> None:
    client, _ = app_client
    assert client.get("/api/settings").json() == {"keep_vms_running_on_quit": False}
    client.put("/api/settings", json={"keep_vms_running_on_quit": True})
    assert client.get("/api/settings").json() == {"keep_vms_running_on_quit": True}

    assert client.put("/api/providers/vertex/location",
                      json={"project": "my-project-123", "region": "europe-west4"}).status_code == 204
    vertex = next(p for p in client.get("/api/providers").json() if p["name"] == "vertex")
    assert vertex["uses_google_cloud"] and vertex["project"] == "my-project-123"
    assert vertex["region"] == "europe-west4" and vertex["key_name"] is None
    assert client.put("/api/providers/ollama/location", json={"project": "p"}).status_code == 404
    assert client.put("/api/providers/vertex/location", json={"project": "Bad Project!"}).status_code == 422


def test_model_status_explains_what_is_missing(app_client: Any) -> None:
    client, _ = app_client
    vertex_agent = client.post("/api/agents", json=payload(name="v", provider="vertex")).json()["id"]
    status_body = client.get(f"/api/agents/{vertex_agent}/model-status").json()
    assert status_body["ok"] is False and "project" in status_body["detail"]
    hosted = client.post("/api/agents", json=payload(name="h", provider="grok")).json()["id"]
    status_body = client.get(f"/api/agents/{hosted}/model-status").json()
    assert status_body["ok"] is False and "API key" in status_body["detail"]


def test_clearing_a_conversation(app_client: Any, settings: Settings) -> None:
    client, app = app_client
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    with Session(app.state.engine) as session:
        run = Run(agent_id=agent_id)
        session.add(run)
        session.commit()
    assert client.delete(f"/api/agents/{agent_id}/messages").status_code == 204
    with Session(app.state.engine) as session:
        assert session.exec(select(Run)).all() == []
    assert client.get(f"/api/agents/{agent_id}").status_code == 200  # the agent itself stays
    assert client.delete("/api/agents/999/messages").status_code == 404


def test_stopping_the_vm_cancels_the_agents_run(app_client: Any) -> None:
    client, app = app_client
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    cancelled: list[int] = []

    async def cancel(agent: int) -> bool:
        cancelled.append(agent)
        return True

    app.state.run_service.cancel = cancel
    client.post(f"/api/agents/{agent_id}/vm/stop")
    client.post(f"/api/agents/{agent_id}/vm/restart")
    client.delete(f"/api/agents/{agent_id}?confirm=true")
    assert cancelled == [agent_id, agent_id, agent_id]


def test_agent_list_reports_activity(app_client: Any) -> None:
    client, app = app_client
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    assert client.get("/api/agents").json()[0]["activity"] == "idle"
    app.state.run_service.activity = lambda _id: "needs_approval"
    assert client.get("/api/agents").json()[0]["activity"] == "needs_approval"
    assert agent_id


# --- channels ---------------------------------------------------------------------------------


def test_mentions_are_parsed() -> None:
    assert mentions("@alice please ask @bob-2, then @alice again") == ["alice", "bob-2"]
    assert mentions("mail me at someone@example.com") == []
    assert mentions("no mentions") == []


def test_channel_api(app_client: Any) -> None:
    client, _ = app_client
    (general,) = client.get("/api/channels").json()
    assert general["name"] == "general"
    created = client.post("/api/channels", json={"name": "#Research"})
    assert created.status_code == 201 and created.json()["name"] == "research"
    assert client.post("/api/channels", json={"name": "research"}).status_code == 400
    assert client.post("/api/channels", json={"name": "bad name"}).status_code == 400

    posted = client.post(f"/api/channels/{general['id']}/messages", json={"content": "hello team"})
    assert posted.status_code == 201 and posted.json()["author"] == "you"
    messages = client.get(f"/api/channels/{general['id']}/messages").json()
    assert [m["content"] for m in messages] == ["hello team"]
    assert client.get(f"/api/channels/{general['id']}/messages?after={messages[0]['id']}").json() == []
    assert client.get("/api/channels/999/messages").status_code == 404


class SilentGuest:
    """A guest that answers every call with nothing (no memory, no tool output)."""

    async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        return {}


class ChannelWorld:
    """A run service with two agents, a fake model and controllable VM states."""

    def __init__(self, tmp_path: Path, reply: str = "On it.") -> None:
        settings = Settings(home=tmp_path)
        settings.ensure_layout()
        self.engine = create_db_engine(settings.db_path)
        init_db(self.engine)
        with Session(self.engine) as session:
            for name in ("alice", "bob"):
                session.add(Agent(name=name, provider="ollama", model="m", vm_daemon_port=18000,
                                  vm_disk_path=str(tmp_path / f"{name}.qcow2")))
            session.commit()
        self.channels = ChannelService(self.engine)
        self.channels.ensure_default()
        self.channel_id = self.channels.list()[0].id or 0
        self.online = {"alice", "bob"}
        self.prompts: list[str] = []
        world = self

        class Provider(ModelProvider):
            async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
                world.prompts.append(messages[-1].content)
                return ModelResponse(text=reply)

        class VMs(QemuVMManager):
            async def status(self, agent: Agent) -> VMStatus:
                return VMStatus.RUNNING if agent.name in world.online else VMStatus.STOPPED

            def guest_secret(self, agent: Agent) -> str:
                return "s"

        self.service = RunService(self.engine, settings, VMs(settings), PolicyEngine(), ApprovalBroker(),
                                  provider_factory=lambda n, c, m: Provider(),
                                  client_factory=lambda port, secret: SilentGuest(),  # type: ignore[arg-type,return-value]
                                  channels=self.channels)

    async def say(self, content: str, author: str = "you") -> None:
        message = self.channels.post(self.channel_id, author, content)
        await self.service.dispatch_mentions(message)
        for _ in range(200):
            if not any(self.service.is_busy(i) for i in (1, 2)):
                await asyncio.sleep(0.02)
                if not any(self.service.is_busy(i) for i in (1, 2)):
                    return
            await asyncio.sleep(0.02)

    def transcript(self) -> list[tuple[str, str]]:
        return [(m.author, m.content) for m in self.channels.messages(self.channel_id)]


def test_mention_wakes_agent_and_reply_lands_in_channel(tmp_path: Path) -> None:
    world = ChannelWorld(tmp_path)
    asyncio.run(world.say("@alice what is the status?"))
    assert world.transcript() == [("you", "@alice what is the status?"), ("alice", "On it.")]
    assert "#general" in world.prompts[0] and "you: @alice what is the status?" in world.prompts[0]
    with Session(world.engine) as session:
        run = session.exec(select(Run)).one()
        assert run.channel_id == world.channel_id and run.hops == 1 and run.status == "completed"


def test_offline_agents_and_unknown_names_do_not_start_runs(tmp_path: Path) -> None:
    world = ChannelWorld(tmp_path)
    world.online = {"alice"}
    asyncio.run(world.say("@bob and @nobody, are you there?"))
    assert world.transcript()[-1] == ("system", "@bob is offline. Start its computer to bring it in.")
    with Session(world.engine) as session:
        assert session.exec(select(Run)).all() == []


def test_handoff_chains_between_agents_are_bounded(tmp_path: Path) -> None:
    # Each agent's reply mentions both agents, so without a bound this never ends.
    world = ChannelWorld(tmp_path, reply="Over to you, @alice and @bob.")
    asyncio.run(world.say("@alice start"))
    with Session(world.engine) as session:
        runs = session.exec(select(Run)).all()
        notes = session.exec(select(ChannelMessage).where(ChannelMessage.author == "system")).all()
    assert len(runs) == MAX_MENTION_HOPS and max(run.hops for run in runs) == MAX_MENTION_HOPS
    assert any("chain stopped" in note.content for note in notes)


def test_mention_of_a_busy_agent_is_handled_when_it_is_free(tmp_path: Path) -> None:
    world = ChannelWorld(tmp_path)

    async def scenario() -> None:
        first = world.channels.post(world.channel_id, "you", "@alice first task")
        await world.service.dispatch_mentions(first)
        assert world.service.is_busy(1)
        second = world.channels.post(world.channel_id, "you", "@alice second task")
        await world.service.dispatch_mentions(second)  # alice is busy: queued, not dropped
        for _ in range(200):
            await asyncio.sleep(0.02)
            if len(world.prompts) == 2 and not world.service.is_busy(1):
                break

    asyncio.run(scenario())
    assert len(world.prompts) == 2 and "second task" in world.prompts[1]
    assert [author for author, _ in world.transcript()].count("alice") == 2
    assert all(author != "system" for author, _ in world.transcript())


def test_agent_that_posts_itself_is_not_echoed_again(tmp_path: Path) -> None:
    world = ChannelWorld(tmp_path, reply="Done, see above.")
    with Session(world.engine) as session:
        alice = session.exec(select(Agent).where(Agent.name == "alice")).one()

    async def scenario() -> None:
        run_id = world.service.start(1, "task", channel_id=world.channel_id, hops=1)
        await world.service._channel_handlers(alice, 1, run_id)["channel.post"]({"message": "my own post"})  # noqa: SLF001
        await world.service.wait(1)

    asyncio.run(scenario())
    assert world.transcript() == [("alice", "my own post")]


def test_agents_use_channel_tools(tmp_path: Path) -> None:
    world = ChannelWorld(tmp_path)
    with Session(world.engine) as session:
        alice = session.exec(select(Agent).where(Agent.name == "alice")).one()
    handlers = world.service._channel_handlers(alice, hops=0)  # noqa: SLF001

    async def scenario() -> None:
        assert await handlers["channel.post"]({"message": "build is green"}) == {"posted": True}
        assert await handlers["channel.read"]({"channel": "#general"}) == {
            "messages": [{"author": "alice", "content": "build is green"}]}
        assert "no channel #nope" in (await handlers["channel.read"]({"channel": "nope"}))["error"]
        assert "empty" in (await handlers["channel.post"]({"message": "  "}))["error"]

    asyncio.run(scenario())


# --- Vertex AI --------------------------------------------------------------------------------


def test_vertex_endpoint_urls() -> None:
    assert openai_base_url("p1", "global") == (
        "https://aiplatform.googleapis.com/v1/projects/p1/locations/global/endpoints/openapi")
    assert openai_base_url("p1", "europe-west4").startswith("https://europe-west4-aiplatform.googleapis.com/")


def test_vertex_provider_uses_google_token_not_an_api_key() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    async def token() -> str:
        return "ya29.test-token"

    provider = VertexProvider("p1", "us-central1", "google/gemini-x", token_source=token,
                              transport=httpx.MockTransport(handler))
    assert asyncio.run(provider.chat([ChatMessage(role="user", content="go")])).text == "hi"
    assert seen["url"].endswith("/projects/p1/locations/us-central1/endpoints/openapi/chat/completions")
    assert seen["auth"] == "Bearer ya29.test-token" and seen["body"]["model"] == "google/gemini-x"
    assert asyncio.run(provider.list_models()) == []


def test_vertex_needs_a_project() -> None:
    for kind in ("vertex", "vertex-anthropic"):
        with pytest.raises(ProviderError, match="Google Cloud project"):
            build_provider("v", ProviderSettings(type=kind, project="", region="global"), "m")


def test_tool_calls_are_replayed_exactly_as_the_model_sent_them() -> None:
    raw = {"id": "c1", "type": "function",
           "function": {"name": "shell_exec", "arguments": '{"command":"ls"}'},
           "extra_content": {"google": {"thought_signature": "opaque-bytes"}}}
    response = parse_response({"choices": [{"message": {"content": None, "tool_calls": [raw]}}]})
    assert response.tool_calls[0].arguments == {"command": "ls"}
    echoed = to_wire_message(ChatMessage(role="assistant", tool_calls=response.tool_calls,
                                         provider_data=response.provider_data))
    assert echoed["tool_calls"] == [raw]
    assert response.provider_data == {RAW_TOOL_CALLS: [raw]}

    no_id = parse_response({"choices": [{"message": {"tool_calls": [
        {"function": {"name": "file_read", "arguments": {"path": "a"}}}]}}]})
    assert no_id.provider_data[RAW_TOOL_CALLS][0]["id"] == no_id.tool_calls[0].id  # type: ignore[index]


def test_extra_body_is_merged_into_requests() -> None:
    from backend.providers.openai_compatible import OpenAICompatibleProvider

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatibleProvider("http://x/v1", "m", transport=httpx.MockTransport(handler),
                                        extra_body={"reasoning_effort": "low", "model": "ignored"})
    asyncio.run(provider.chat([ChatMessage(role="user", content="hi")]))
    assert seen["reasoning_effort"] == "low" and seen["model"] == "m"


def test_adding_a_model_server_on_another_machine(app_client: Any, settings: Settings) -> None:
    client, _ = app_client
    created = client.post("/api/providers", json={"name": "gpu-box", "base_url": "http://192.168.1.50:8000/v1/"})
    assert created.status_code == 201
    box = next(p for p in client.get("/api/providers").json() if p["name"] == "gpu-box")
    assert box["base_url"] == "http://192.168.1.50:8000/v1" and box["key_name"] is None
    assert client.post("/api/agents", json=payload(name="remote", provider="gpu-box")).status_code == 201

    client.post("/api/providers", json={"name": "team-gateway", "base_url": "https://llm.example/v1", "needs_key": True})
    gateway = next(p for p in client.get("/api/providers").json() if p["name"] == "team-gateway")
    assert gateway["key_name"] == "TEAM_GATEWAY_API_KEY" and gateway["has_key"] is False

    assert client.post("/api/providers", json={"name": "gpu-box", "base_url": "http://x/v1"}).status_code == 409
    assert client.post("/api/providers", json={"name": "Bad Name", "base_url": "http://x/v1"}).status_code == 422
    assert client.post("/api/providers", json={"name": "ftp", "base_url": "ftp://x"}).status_code == 422


def test_null_provider_hides_it_and_config_writes_are_atomic(settings: Settings) -> None:
    settings.home.mkdir(parents=True)
    settings.config_path.write_text("providers:\n  openai: null\n  grok: null\n")
    providers = settings.load_providers()
    assert "openai" not in providers and "grok" not in providers and "ollama" in providers
    settings.update_user_config(["providers", "vertex", "project"], "p1")
    assert settings.load_providers()["vertex"].project == "p1"
    assert settings.load_user_config()["providers"]["openai"] is None  # the user's choices survive
    assert not settings.config_path.with_suffix(".yaml.tmp").exists()


def test_vertex_declares_argument_free_tools_without_a_schema() -> None:
    from backend.agents.tools import ALL_TOOLS

    async def token() -> str:
        return "t"

    provider = VertexProvider("p", "global", "google/m", token_source=token)
    by_name = {tool.spec.name: provider._wire_tool(tool.spec)["function"] for tool in ALL_TOOLS}  # noqa: SLF001
    assert "parameters" not in by_name["browser_screenshot"]
    assert by_name["shell_exec"]["parameters"]["required"] == ["command"]


# --- connection test ---------------------------------------------------------------------------


def test_model_check_distinguishes_three_outcomes() -> None:
    from backend.providers.base import ToolCallRequest
    from backend.providers.check import PROBE_TOOL, check_model

    class Scripted(ModelProvider):
        def __init__(self, outcome: Any) -> None:
            self.outcome, self.seen_tools = outcome, None

        async def chat(self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None) -> ModelResponse:
            self.seen_tools = tools
            if isinstance(self.outcome, Exception):
                raise self.outcome
            if self.outcome == "hang":
                await asyncio.sleep(5)
            return self.outcome

    calls = ModelResponse(tool_calls=[ToolCallRequest(id="1", name="report_status", arguments={"status": "ok"})])
    provider = Scripted(calls)
    result = asyncio.run(check_model(provider))
    assert result.ok and result.tools and "tool calls" in result.detail
    assert provider.seen_tools == [PROBE_TOOL]

    chat_only = asyncio.run(check_model(Scripted(ModelResponse(text="ok"))))
    assert chat_only.ok and not chat_only.tools and "unreliable as an agent" in chat_only.detail

    failed = asyncio.run(check_model(Scripted(ProviderError("The model 'nope' was not found."))))
    assert not failed.ok and "not found" in failed.detail

    slow = asyncio.run(check_model(Scripted("hang"), timeout=0.05))
    assert not slow.ok and "still be loading" in slow.detail


def test_provider_test_endpoint_reports_setup_problems(app_client: Any) -> None:
    client, _ = app_client
    no_key = client.post("/api/providers/grok/test", json={"model": "m"}).json()
    assert no_key["ok"] is False and "API key" in no_key["detail"]
    no_project = client.post("/api/providers/vertex/test", json={"model": "google/m"}).json()
    assert no_project["ok"] is False and "Google Cloud project" in no_project["detail"]
    assert client.post("/api/providers/nope/test", json={"model": "m"}).status_code == 404
    assert client.post("/api/providers/ollama/test", json={"model": ""}).status_code == 422


def test_memory_api_needs_the_computer_on(app_client: Any) -> None:
    client, _ = app_client
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    off = client.get(f"/api/agents/{agent_id}/memory")
    assert off.status_code == 409 and "Turn on" in off.json()["detail"]
    assert client.post(f"/api/agents/{agent_id}/memory", json={"subject": "X", "note": "y"}).status_code == 409
    assert client.delete(f"/api/agents/{agent_id}/memory/1").status_code == 409
    assert client.post(f"/api/agents/{agent_id}/memory", json={"subject": ""}).status_code == 422
    assert client.get("/api/agents/999/memory").status_code == 404


def test_handlers_that_wake_a_run_execute_on_the_event_loop() -> None:
    import inspect

    from backend.api import chat, desktop

    # A plain `def` route runs on a worker thread, where waking a waiting run is unsafe.
    for handler in (chat.answer_approval, desktop.set_control, desktop.get_control):
        assert inspect.iscoroutinefunction(handler), handler.__name__


# --- findings from the second review ---------------------------------------------------------


def test_an_empty_token_file_is_replaced_and_never_authenticates(settings: Settings) -> None:
    settings.ensure_layout()
    settings.api_token_path.write_text("")  # what an interrupted first start could leave behind
    token = settings.load_api_token()
    assert len(token) > 20 and settings.load_api_token() == token
    assert settings.api_token_path.stat().st_mode & 0o777 == 0o600

    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        app.state.api_token = ""  # even if the token were somehow empty
        for header in ("Bearer ", "Bearer", ""):
            assert client.get("/api/agents", headers={"Authorization": header}).status_code == 401


def test_saving_settings_keeps_a_symlinked_config_and_its_permissions(settings: Settings, tmp_path: Path) -> None:
    settings.home.mkdir(parents=True)
    real = tmp_path / "dotfiles" / "agent-office.yaml"
    real.parent.mkdir()
    real.write_text("providers:\n  mine:\n    type: openai-compatible\n    base_url: http://x/v1\n    api_key: s3cret\n")
    real.chmod(0o600)
    settings.config_path.symlink_to(real)

    settings.update_user_config(["app", "keep_vms_running_on_quit"], True)

    assert settings.config_path.is_symlink() and settings.config_path.resolve() == real.resolve()
    assert real.stat().st_mode & 0o777 == 0o600
    assert settings.load_user_config()["providers"]["mine"]["api_key"] == "s3cret"
    assert settings.load_user_config()["app"] == {"keep_vms_running_on_quit": True}
    assert [p.name for p in real.parent.iterdir()] == ["agent-office.yaml"]  # no temp files left


def test_checking_for_a_running_backend_changes_nothing(settings: Settings) -> None:
    assert not is_running(settings)  # no lock file yet, and none is created by looking
    assert not (settings.home / "backend.lock").exists()
    held = acquire_instance_lock(settings)
    recorded = (settings.home / "backend.lock").read_text()
    assert is_running(settings) and (settings.home / "backend.lock").read_text() == recorded
    held.close()


def test_shutdown_keeps_other_backends_out_until_the_vms_are_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend import shutdown

    settings.ensure_layout()
    init_db(create_db_engine(settings.db_path))
    seen: list[bool] = []

    async def stop_while_checking(_settings: Settings, vm_manager: Any = None) -> int:
        seen.append(is_running(settings))  # what a backend starting right now would find
        return 0

    monkeypatch.setattr(shutdown, "stop_all_vms", stop_while_checking)
    monkeypatch.setenv("AGENT_OFFICE_HOME", str(settings.home))
    shutdown.main()
    assert seen == [True] and not is_running(settings)
