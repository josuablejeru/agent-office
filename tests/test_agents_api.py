from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.keystore import KeyStore
from backend.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()))
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        yield test_client


class FakeKeychain:
    """Stands in for /usr/bin/security so tests never touch the real Keychain."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def __call__(self, args: list[str], stdin: str | None) -> Any:
        import subprocess

        if args == ["-i"] and stdin:
            words = stdin.split()
            self.items[words[words.index("-a") + 1]] = words[words.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        account = args[args.index("-a") + 1]
        if args[0] == "find-generic-password" and account in self.items:
            return subprocess.CompletedProcess(args, 0, self.items[account] + "\n", "")
        return subprocess.CompletedProcess(args, 44, "", "not found")


def payload(**overrides: object) -> dict[str, object]:
    return {"name": "research-agent", "provider": "ollama", "model": "some-model", **overrides}


def test_layout_and_default_config_created(client: TestClient, settings: Settings) -> None:
    assert settings.db_path.exists()
    assert settings.images_dir.is_dir() and settings.secrets_dir.is_dir()
    assert set(settings.load_providers()) == {"openai", "anthropic", "grok", "ollama", "vertex", "vertex-claude"}


def test_create_list_get(client: TestClient, settings: Settings) -> None:
    created = client.post("/api/agents", json=payload(system_prompt="hi"))
    assert created.status_code == 201
    agent = created.json()
    assert agent["vm_status"] == "not_created"
    assert agent["vm_disk_path"] == str(settings.agent_dir("research-agent") / "disk.qcow2")

    agent_dir = settings.agent_dir("research-agent")
    assert (agent_dir / "logs").is_dir()
    config = yaml.safe_load((agent_dir / "agent.yaml").read_text())
    assert config["model"] == {"provider": "ollama", "model": "some-model"}
    assert config["vm"] == {"image": "debian-desktop", "memory_mb": 4096, "cpus": 4}

    assert [a["name"] for a in client.get("/api/agents").json()] == ["research-agent"]
    assert client.get(f"/api/agents/{agent['id']}").json()["system_prompt"] == "hi"


def test_duplicate_name_conflicts(client: TestClient) -> None:
    assert client.post("/api/agents", json=payload()).status_code == 201
    assert client.post("/api/agents", json=payload()).status_code == 409


@pytest.mark.parametrize("name", ["../evil", "Has Space", "UPPER", "", "-leading", "a/b"])
def test_unsafe_names_rejected(client: TestClient, name: str) -> None:
    assert client.post("/api/agents", json=payload(name=name)).status_code == 422


def test_unknown_provider_rejected(client: TestClient, settings: Settings) -> None:
    response = client.post("/api/agents", json=payload(provider="nope"))
    assert response.status_code == 422
    assert not settings.agent_dir("research-agent").exists()


def test_update_rewrites_yaml(client: TestClient, settings: Settings) -> None:
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    updated = client.patch(f"/api/agents/{agent_id}", json={"model": "other", "vm_cpus": 2})
    assert updated.status_code == 200
    assert updated.json()["model"] == "other"
    config = yaml.safe_load((settings.agent_dir("research-agent") / "agent.yaml").read_text())
    assert config["model"]["model"] == "other"
    assert config["vm"]["cpus"] == 2


def test_delete_requires_confirmation(client: TestClient, settings: Settings) -> None:
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    assert client.delete(f"/api/agents/{agent_id}").status_code == 400
    assert settings.agent_dir("research-agent").exists()

    assert client.delete(f"/api/agents/{agent_id}?confirm=true").status_code == 204
    assert not settings.agent_dir("research-agent").exists()
    assert client.get(f"/api/agents/{agent_id}").status_code == 404


def test_providers_endpoint_exposes_no_credentials(client: TestClient) -> None:
    providers = client.get("/api/providers").json()
    assert {p["name"] for p in providers} == {"openai", "anthropic", "grok", "ollama", "vertex", "vertex-claude"}
    assert all("key" not in p and "api_key" not in p for p in providers)
    by_name = {p["name"]: p for p in providers}
    assert by_name["ollama"]["key_name"] is None and not by_name["ollama"]["has_key"]


def test_provider_keys_are_stored_but_never_returned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def anthropic() -> dict[str, Any]:
        return next(p for p in client.get("/api/providers").json() if p["name"] == "anthropic")

    assert anthropic()["has_key"] is False
    assert client.put("/api/providers/anthropic/key", json={"key": "sk-test-123"}).status_code == 204
    assert anthropic()["has_key"] is True
    assert "sk-test-123" not in client.get("/api/providers").text
    assert client.put("/api/providers/anthropic/key", json={"key": "has space"}).status_code == 400
    assert client.put("/api/providers/ollama/key", json={"key": "x"}).status_code == 404


def test_base_image_status_and_ui_serving(settings: Settings, tmp_path: Path) -> None:
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<title>agent-office</title>")
    app = create_app(settings, key_store=KeyStore(run=FakeKeychain()), ui_dir=ui)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        assert "agent-office" in test_client.get("/").text
        assert test_client.get("/api/agents").status_code == 401  # the API still needs the token
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        status_body = test_client.get("/api/system/base-image", headers=headers).json()
        assert status_body == {"ready": False, "building": False, "detail": ""}


def test_vm_start_without_base_image_reports_conflict(client: TestClient) -> None:
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    response = client.post(f"/api/agents/{agent_id}/vm/start")
    assert response.status_code == 409
    agent = client.get(f"/api/agents/{agent_id}").json()
    assert agent["vm_status"] == "not_created"
    # The port is reserved for the agent even though the VM could not start.
    assert agent["vm_daemon_port"] is not None


def test_vm_endpoints_404_for_unknown_agent(client: TestClient) -> None:
    for action in ("start", "stop", "restart"):
        assert client.post(f"/api/agents/999/vm/{action}").status_code == 404


def test_agents_get_distinct_vm_ports(client: TestClient) -> None:
    ports = []
    for name in ("one", "two"):
        agent_id = client.post("/api/agents", json=payload(name=name)).json()["id"]
        client.post(f"/api/agents/{agent_id}/vm/start")
        agent = client.get(f"/api/agents/{agent_id}").json()
        ports.append(agent["vm_daemon_port"])
    assert ports[0] != ports[1]


def test_api_requires_token(client: TestClient, settings: Settings) -> None:
    assert settings.api_token_path.stat().st_mode & 0o777 == 0o600
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}):
        assert client.get("/api/agents", headers=headers).status_code == 401
        assert client.get("/api/providers", headers=headers).status_code == 401
        assert client.post("/api/agents/1/chat", json={"content": "hi"}, headers=headers).status_code == 401


def test_chat_requires_running_vm(client: TestClient) -> None:
    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    response = client.post(f"/api/agents/{agent_id}/chat", json={"content": "hi"})
    assert response.status_code == 409
    assert client.get(f"/api/agents/{agent_id}/messages").json() == []
    assert client.get("/api/runs/1").status_code == 404


def test_deleting_an_agent_removes_its_conversation(client: TestClient, settings: Settings) -> None:
    from sqlmodel import Session, select

    from backend.db.database import create_db_engine
    from backend.db.models import Approval, Message, Run, ToolCall

    agent_id = client.post("/api/agents", json=payload()).json()["id"]
    engine = create_db_engine(settings.db_path)
    with Session(engine) as session:
        run = Run(agent_id=agent_id)
        session.add(run)
        session.commit()
        call = ToolCall(run_id=run.id, tool="shell.exec")
        session.add(call)
        session.add(Message(agent_id=agent_id, run_id=run.id, role="user", content="hi"))
        session.commit()
        session.add(Approval(tool_call_id=call.id))
        session.commit()

    assert client.delete(f"/api/agents/{agent_id}?confirm=true").status_code == 204
    reborn = client.post("/api/agents", json=payload()).json()["id"]
    assert client.get(f"/api/agents/{reborn}/messages").json() == []
    with Session(engine) as session:
        for table in (Message, Run, ToolCall, Approval):
            assert session.exec(select(table)).all() == []
