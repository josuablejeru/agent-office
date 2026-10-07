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
