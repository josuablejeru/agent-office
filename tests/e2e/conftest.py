"""End-to-end tests: a real backend, real agent VMs, and a scripted model.

These need QEMU, the base image and a few minutes, so they only run on request:

    AGENT_OFFICE_E2E=1 uv run pytest tests/e2e -v

Everything happens in a throwaway data directory. The installed app and its
agents are not touched; only the read-only base image is shared.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from backend.config import Settings

REPO = Path(__file__).resolve().parents[2]
BASE_IMAGE = Settings.from_env().base_image_path("debian-desktop")
FAKE_INTERNAL_ADDRESS = "10.9.8.7"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("AGENT_OFFICE_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="end-to-end tests run only with AGENT_OFFICE_E2E=1")
    for item in items:
        if "tests/e2e" in str(item.fspath):
            item.add_marker(skip)


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def tool(name: str, **arguments: Any) -> dict[str, Any]:
    """A scripted model turn that calls one tool."""
    return {"tool": name, "arguments": arguments}


class ScriptedModel:
    """A stand-in model server that speaks the OpenAI chat API.

    Each agent uses its own model name, so each has its own script: a queue of
    turns (a tool call, a final text, or a function of the request that returns
    one of those). Every request is recorded, which lets
    a test check what the product actually sent to the model.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, deque[Any]] = defaultdict(deque)
        self.requests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.port = free_port()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send({"data": [{"id": name} for name in outer.scripts]})

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = request["model"]
                outer.requests[model].append(request)
                turn = outer.scripts[model].popleft() if outer.scripts[model] else "Done."
                if callable(turn):  # decided from what the model was shown
                    turn = turn(request)
                if isinstance(turn, dict):
                    call_id = f"call_{len(outer.requests[model])}"
                    message = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": turn["tool"], "arguments": json.dumps(turn["arguments"])},
                            }
                        ],
                    }
                else:
                    message = {"role": "assistant", "content": turn}
                self._send({"choices": [{"message": message}]})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def script(self, agent: str, turns: list[Any]) -> None:
        self.scripts[agent].extend(turns)

    def last_system_prompt(self, agent: str) -> str:
        return self.requests[agent][-1]["messages"][0]["content"]

    def close(self) -> None:
        self._server.shutdown()


class FakeInternalDns:
    """Plays a company DNS server: every A question is answered with one fixed address."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind(("127.0.0.1", 0))
        self.port = self._socket.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                data, address = self._socket.recvfrom(4096)
            except OSError:
                return
            is_a = struct.unpack("!H", data[-4:-2])[0] == 1
            header = data[:2] + struct.pack("!HHHHH", 0x8180, 1, 1 if is_a else 0, 0, 0)
            record = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(FAKE_INTERNAL_ADDRESS)
            self._socket.sendto(header + data[12:] + (record if is_a else b""), address)

    def close(self) -> None:
        self._socket.close()


class Office:
    """A running backend on a throwaway data directory, driven over HTTP like the app does."""

    def __init__(self, home: Path, model: ScriptedModel) -> None:
        self.home = home
        self.model = model
        self.port = free_port()
        self._process: subprocess.Popen[bytes] | None = None
        self.http: httpx.Client | None = None

    def start_backend(self) -> None:
        self.port = free_port()
        log = (self.home / "backend.log").open("ab")
        self._process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--factory", "backend.main:create_app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=REPO, env={**os.environ, "AGENT_OFFICE_HOME": str(self.home)}, stdout=log, stderr=log,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}/docs", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("the backend did not start; see backend.log in the test home")
        token = (self.home / "secrets" / "api-token").read_text().strip()
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", headers={"Authorization": f"Bearer {token}"}, timeout=120
        )

    def stop_backend(self) -> None:
        if self.http is not None:
            self.http.close()
            self.http = None
        if self._process is not None:
            self._process.send_signal(signal.SIGINT)
            try:
                self._process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    # --- helpers that read like what a user does -------------------------------------------

    def api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        assert self.http is not None
        return self.http.request(method, path, **kwargs)

    def hire(self, name: str) -> int:
        response = self.api("POST", "/api/agents", json={
            "name": name, "provider": "scripted", "model": name, "vm_memory_mb": 3072, "vm_cpus": 2})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def turn_on(self, agent_id: int) -> None:
        response = self.api("POST", f"/api/agents/{agent_id}/vm/start")
        assert response.status_code == 200 and response.json()["vm_status"] == "running", response.text
        self.wait_until(lambda: self.api("GET", f"/api/agents/{agent_id}/vm/guest").json()["ready"], 120,
                        "the agent's computer to answer")

    def wait_until(self, condition: Any, seconds: float, what: str) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {what}")

    def send(self, agent_id: int, text: str) -> int:
        response = self.api("POST", f"/api/agents/{agent_id}/chat", json={"content": text})
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def run(self, run_id: int) -> dict[str, Any]:
        return self.api("GET", f"/api/runs/{run_id}").json()

    def finished(self, run_id: int, seconds: float = 120) -> dict[str, Any]:
        self.wait_until(lambda: self.run(run_id)["status"] != "running", seconds, f"run {run_id} to end")
        return self.run(run_id)

    def ask(self, agent_id: int, text: str = "go") -> dict[str, Any]:
        return self.finished(self.send(agent_id, text))

    def running_vm_pids(self) -> list[int]:
        pids = []
        for pid_file in self.home.glob("agents/*/qemu.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                pids.append(pid)
            except (OSError, ValueError):
                continue
        return pids


@pytest.fixture(scope="session")
def office(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Office]:
    if not BASE_IMAGE.exists():
        pytest.skip(f"base image missing: {BASE_IMAGE} (build it with scripts/create-base-image.sh)")
    home = tmp_path_factory.mktemp("office")
    (home / "images").mkdir()
    (home / "images" / BASE_IMAGE.name).symlink_to(BASE_IMAGE)
    model, internal_dns = ScriptedModel(), FakeInternalDns()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "providers": {"scripted": {
            "type": "openai-compatible", "base_url": f"http://127.0.0.1:{model.port}/v1", "api_key": "none"}},
        # Its own DNS port: the installed app may be using the default one.
        "network": {"dns_port": free_port(socket.SOCK_DGRAM),
                    "dns_rules": {"e2e.internal": [f"127.0.0.1:{internal_dns.port}"]}},
    }))
    running = Office(home, model)
    running.start_backend()
    try:
        yield running
    finally:
        # Runs even when a test failed: never leave a VM behind.
        try:
            if running.http is not None:
                for agent in running.api("GET", "/api/agents").json():
                    running.api("POST", f"/api/agents/{agent['id']}/vm/stop")
        except Exception:  # noqa: BLE001
            pass
        running.stop_backend()
        for pid in running.running_vm_pids():
            os.kill(pid, signal.SIGKILL)
        model.close()
        internal_dns.close()
        if os.environ.get("AGENT_OFFICE_E2E_KEEP") == "1":
            print(f"\ntest data kept for inspection: {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)
