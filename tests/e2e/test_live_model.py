"""The same product with a real model, for a sense of how it does on everyday tasks.

    AGENT_OFFICE_E2E=1 AGENT_OFFICE_E2E_MODEL=ollama/qwen3:8b uv run pytest tests/e2e/test_live_model.py -v

The model is whatever you name (provider/model, the provider as in the default
config). Results are judged on what ended up on the agent's computer, never on
the wording of the reply. A failure here is first of all information about the
model: only fix the product if a tool misled it, hung, or left wrong state.
"""

from __future__ import annotations

import os
import re
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import Office

LIVE_MODEL = os.environ.get("AGENT_OFFICE_E2E_MODEL", "")
pytestmark = pytest.mark.skipif(not LIVE_MODEL, reason="set AGENT_OFFICE_E2E_MODEL=provider/model")
TASK_SECONDS = 420


@pytest.fixture(scope="module")
def worker(office: Office) -> int:
    provider, _, model = LIVE_MODEL.partition("/")
    # Use the stock definition of the named provider (for example the local Ollama).
    from backend.config import DEFAULT_CONFIG

    config = yaml.safe_load((office.home / "config.yaml").read_text())
    config["providers"][provider] = DEFAULT_CONFIG["providers"][provider]
    (office.home / "config.yaml").write_text(yaml.safe_dump(config))
    response = office.api("POST", "/api/agents", json={
        "name": "worker", "provider": provider, "model": model, "vm_memory_mb": 4096, "vm_cpus": 4,
        "system_prompt": "You are a capable office assistant. Use your tools and be concise."})
    assert response.status_code == 201, response.text
    office.turn_on(response.json()["id"])
    return response.json()["id"]


def ask(office: Office, agent_id: int, text: str) -> dict[str, Any]:
    run = office.finished(office.send(agent_id, text), TASK_SECONDS)
    run["reply"] = office.api("GET", f"/api/agents/{agent_id}/messages").json()[-1]["content"]
    return run


def tools_used(run: dict[str, Any]) -> list[str]:
    return [call["tool"] for call in run["tool_calls"]]


def test_looks_something_up_on_the_web(office: Office, worker: int) -> None:
    run = ask(office, worker, "Find the phone number of Parasail Maui at Kaanapali Beach and tell me where you found it.")
    assert run["status"] == "completed", run["error"]
    assert "browser.search" in tools_used(run)
    # A site may show a bot check; the search itself must not, and the agent must get past the dead end.
    assert not any((call["result"] or {}).get("blocked") for call in run["tool_calls"] if call["tool"] == "browser.search")
    assert re.search(r"\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}", run["reply"]), run["reply"]


def test_summarises_a_file_into_the_shared_folder(office: Office, worker: int) -> None:
    notes = (b"Meeting notes, 3 October\n1. Dana tests a shorter onboarding flow with five users next week.\n"
             b"2. Lukas ships the billing fix on Tuesday.\n3. Mirela adds a forgot-password link to the login page.\n")
    office.api("PUT", f"/api/agents/{worker}/files/meeting.txt", content=notes)
    run = ask(office, worker, "meeting.txt is in your Shared folder. Save a short list of the action items "
                              "with their owners as actions.md in the Shared folder.")
    assert run["status"] == "completed", run["error"]
    saved = office.api("GET", f"/api/agents/{worker}/files/actions.md")
    assert saved.status_code == 200, "the agent did not save actions.md"
    assert all(name in saved.text for name in ("Dana", "Lukas", "Mirela"))


def test_keeps_records_in_a_database(office: Office, worker: int) -> None:
    run = ask(office, worker, "Keep a table of sales leads in your database with name, company and status. Add "
                              "Ada Lovelace (Analytical Engines, new), Grace Hopper (Navy Systems, contacted) "
                              "and Alan Turing (Bletchley Ltd, new). Then tell me how many have status new.")
    assert run["status"] == "completed", run["error"]
    tables = [table for database in office.api("GET", f"/api/agents/{worker}/memory").json()["databases"]
              for table in database["tables"]]
    assert any(table["rows"] == 3 for table in tables), tables
    assert re.search(r"\b(2|two)\b", run["reply"], re.IGNORECASE), run["reply"]


def test_remembers_what_it_was_told(office: Office, worker: int) -> None:
    ask(office, worker, "For future reference: our office wifi network is called Loft-5G and the guest password "
                        "is changed every Monday by Mirela.")
    facts = office.api("GET", f"/api/agents/{worker}/memory").json()["facts"]
    assert any("Loft-5G" in str(fact) for fact in facts), facts
    office.api("DELETE", f"/api/agents/{worker}/messages")
    run = ask(office, worker, "Who should I ask for the guest wifi password? Answer in one sentence.")
    assert "Mirela" in run["reply"], run["reply"]


def test_finds_and_clicks_something_only_visible_on_screen(office: Office) -> None:
    """A red square drawn on a canvas, with no text or element: only a model that sees can find it."""
    import random
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from tests.e2e.conftest import free_port

    left, top = random.randint(200, 900), random.randint(150, 450)
    page = f"""<!doctype html><title>Find it</title><body style="margin:0">
<canvas id="c" width="1200" height="620"></canvas>
<script>
  const c = document.getElementById('c'), g = c.getContext('2d');
  g.fillStyle = '#d00'; g.fillRect({left}, {top}, 60, 60);
  c.addEventListener('click', (e) => fetch('/hit?x=' + e.offsetX + '&y=' + e.offsetY));
</script>""".encode()
    hits: list[tuple[int, int]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/hit"):
                query = dict(part.split("=") for part in self.path.split("?", 1)[1].split("&"))
                hits.append((int(float(query["x"])), int(float(query["y"]))))
            body = page if self.path == "/" else b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        provider, _, model = (os.environ.get("AGENT_OFFICE_E2E_VISION_MODEL") or LIVE_MODEL).partition("/")
        response = office.api("POST", "/api/agents", json={
            "name": "looker", "provider": provider, "model": model, "vision_enabled": True,
            "vm_memory_mb": 3072, "vm_cpus": 2, "system_prompt": "You are a careful assistant. Use your tools."})
        assert response.status_code == 201, response.text
        looker = response.json()["id"]
        office.turn_on(looker)
        run = ask(office, looker, f"Open http://10.0.2.2:{port}/ and click the red square on the page. "
                                  "Then tell me where it was.")
        assert run["status"] == "completed", run["error"]
        assert "browser.screenshot" in tools_used(run)  # it decided to look
        assert "browser.click_at" in tools_used(run)
        assert any(left <= x <= left + 60 and top <= y <= top + 60 for x, y in hits), (hits, (left, top))
    finally:
        server.shutdown()


def test_a_channel_message_to_nobody_is_answered_once(office: Office) -> None:
    """Two agents around, a message without a name: one answers, the other stays quiet."""
    provider, _, model = LIVE_MODEL.partition("/")
    ids = []
    for name, job in (("ana", "You are the office's accountant: numbers, invoices, budgets."),
                      ("ben", "You are the office's IT support: computers, printers, networks.")):
        response = office.api("POST", "/api/agents", json={
            "name": name, "provider": provider, "model": model, "system_prompt": job,
            "vm_memory_mb": 3072, "vm_cpus": 2})
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])
    for agent_id in ids:
        office.turn_on(agent_id)
    channel = office.api("GET", "/api/channels").json()[0]["id"]
    before = len(office.api("GET", f"/api/channels/{channel}/messages").json())
    office.api("POST", f"/api/channels/{channel}/messages",
               json={"content": "The printer on the second floor keeps jamming. Can someone look at it?"})

    def replies() -> list[dict[str, Any]]:
        return office.api("GET", f"/api/channels/{channel}/messages").json()[before + 1:]

    office.wait_until(lambda: len(replies()) >= 1, TASK_SECONDS, "an answer in the channel")
    import time

    time.sleep(60)  # anyone else would have spoken by now
    authors = [message["author"] for message in replies()]
    assert authors == ["ben"], [(m["author"], m["content"][:80]) for m in replies()]
