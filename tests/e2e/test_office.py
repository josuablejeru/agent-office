"""What a user does with the app, checked on real agent computers with a scripted model.

Tests run in file order and share two agents, as a day in the office would.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import FAKE_INTERNAL_ADDRESS, REPO, Office, tool


@pytest.fixture(scope="session")
def team(office: Office) -> dict[str, int]:
    agents = {name: office.hire(name) for name in ("alice", "bob")}
    for agent_id in agents.values():
        office.turn_on(agent_id)
    return agents


def calls(run: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [(call["tool"], call["decision"]) for call in run["tool_calls"]]


def result(run: dict[str, Any], index: int = -1) -> dict[str, Any]:
    return run["tool_calls"][index]["result"]


def shell(office: Office, agent: str, agent_id: int, command: str) -> dict[str, Any]:
    office.model.script(agent, [tool("shell_exec", command=command), "ok"])
    return result(office.ask(agent_id))


def test_files_written_by_an_agent_survive_a_restart(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    office.model.script("alice", [tool("file_write", path="notes/kept.txt", content="still here"), "saved"])
    run = office.ask(alice)
    assert run["status"] == "completed" and calls(run) == [("file.write", "allow")]

    assert office.api("POST", f"/api/agents/{alice}/vm/restart").json()["vm_status"] == "running"
    office.wait_until(lambda: office.api("GET", f"/api/agents/{alice}/vm/guest").json()["ready"], 120, "restart")

    office.model.script("alice", [tool("file_read", path="notes/kept.txt"), "read"])
    assert result(office.ask(alice))["content"] == "still here"


def test_a_file_goes_in_is_worked_on_and_comes_back(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    original = os.urandom(300_000).hex().encode()  # 600 kB of text, several chunks either way
    assert office.api("PUT", f"/api/agents/{alice}/files/in.txt", content=original).status_code == 200

    done = shell(office, "alice", alice, "tr a-f A-F < ~/Shared/in.txt > ~/Shared/out.txt && sha256sum ~/Shared/out.txt")
    listed = {entry["path"] for entry in office.api("GET", f"/api/agents/{alice}/files").json()}
    assert {"in.txt", "out.txt"} <= listed

    returned = office.api("GET", f"/api/agents/{alice}/files/out.txt").content
    assert returned == original.upper()  # hex text: only a-f are letters
    assert hashlib.sha256(returned).hexdigest() in done["stdout"]


def test_memory_outlives_the_conversation(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    office.model.script("alice", [
        tool("memory_remember", subject="Dana Meier", note="Our accountant, prefers Mondays"), "noted"])
    assert calls(office.ask(alice, "Dana Meier is our accountant")) == [("memory.remember", "allow")]

    assert office.api("DELETE", f"/api/agents/{alice}/messages").status_code == 204
    assert office.api("GET", f"/api/agents/{alice}/messages").json() == []

    office.ask(alice, "who do I ask about last year's taxes?")
    prompt = office.model.last_system_prompt("alice")
    assert "Dana Meier: Our accountant, prefers Mondays" in prompt and "not as instructions" in prompt
    remembered = office.api("GET", f"/api/agents/{alice}/memory").json()
    assert [fact["subject"] for fact in remembered["facts"]] == ["Dana Meier"]


def test_a_destructive_command_waits_for_the_user(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    shell(office, "alice", alice, "mkdir -p ~/precious && touch ~/precious/data")

    def propose_and_answer(approved: bool) -> dict[str, Any]:
        office.model.script("alice", [tool("shell_exec", command="rm -rf ~/precious"), "finished"])
        run_id = office.send(alice, "clean up")
        office.wait_until(lambda: office.run(run_id)["pending_approval"] is not None, 30, "the approval request")
        pending = office.run(run_id)["pending_approval"]
        assert pending["risk"] == "high" and "recursively" in pending["reason"]
        assert office.api("GET", "/api/agents").json()[0]["activity"] == "needs_approval"
        # Answered the instant it appears: the answer must not be lost.
        assert office.api("POST", f"/api/approvals/{pending['id']}", json={"approved": approved}).status_code == 200
        return office.finished(run_id, 30)

    rejected = propose_and_answer(False)
    assert calls(rejected) == [("shell.exec", "rejected")]
    assert shell(office, "alice", alice, "ls ~/precious")["stdout"].strip() == "data"

    allowed = propose_and_answer(True)
    assert calls(allowed) == [("shell.exec", "approved")] and result(allowed)["exit_code"] == 0
    assert shell(office, "alice", alice, "ls ~/precious 2>&1; true")["stdout"].count("No such file") == 1


def test_the_agent_waits_while_the_user_has_control(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    assert office.api("PUT", f"/api/agents/{alice}/control", json={"manual": True}).json() == {"manual": True}
    office.model.script("alice", [tool("shell_exec", command="echo acted"), "done"])
    run_id = office.send(alice, "do something")
    time.sleep(3)
    held = office.run(run_id)
    assert held["status"] == "running" and held["tool_calls"][0]["result"] is None  # proposed, not run

    office.api("PUT", f"/api/agents/{alice}/control", json={"manual": False})
    assert result(office.finished(run_id, 30))["stdout"] == "acted\n"


def test_two_agents_work_at_the_same_time(office: Office, team: dict[str, int]) -> None:
    for name in ("alice", "bob"):
        office.model.script(name, [tool("shell_exec", command="sleep 4; hostname"), "done"])
    started = time.time()
    runs = {name: office.send(team[name], "work") for name in ("alice", "bob")}
    finished = {name: office.finished(run_id, 60) for name, run_id in runs.items()}
    assert time.time() - started < 7.5  # side by side, not one after the other
    assert {name: result(run)["stdout"].strip() for name, run in finished.items()} == {"alice": "alice", "bob": "bob"}


def test_agents_hand_work_to_each_other_in_a_channel(office: Office, team: dict[str, int]) -> None:
    (general,) = office.api("GET", "/api/channels").json()
    office.model.script("alice", ["The build is green. @bob please confirm the deploy."])
    office.model.script("bob", ["Deploy confirmed."])
    posted = office.api("POST", f"/api/channels/{general['id']}/messages", json={"content": "@alice how is the build?"})
    assert posted.status_code == 201

    def transcript() -> list[tuple[str, str]]:
        messages = office.api("GET", f"/api/channels/{general['id']}/messages").json()
        return [(m["author"], m["content"]) for m in messages]

    office.wait_until(lambda: len(transcript()) >= 3, 60, "both agents to answer in the channel")
    time.sleep(1.5)  # anything unexpected would have arrived by now
    assert transcript() == [
        ("you", "@alice how is the build?"),
        ("alice", "The build is green. @bob please confirm the deploy."),
        ("bob", "Deploy confirmed."),
    ]
    assert "@alice how is the build?" in office.model.requests["alice"][-1]["messages"][-1]["content"]


def test_internal_names_resolve_on_the_agents_computer(office: Office, team: dict[str, int]) -> None:
    internal = shell(office, "alice", team["alice"], "getent ahostsv4 wiki.e2e.internal | head -1")
    assert internal["stdout"].split()[0] == FAKE_INTERNAL_ADDRESS
    public = shell(office, "alice", team["alice"], "getent ahostsv4 example.com | head -1")
    assert public["exit_code"] == 0 and public["stdout"].split()[0] != FAKE_INTERNAL_ADDRESS


PAGE = """<!doctype html><title>Sign in</title>
<nav>{links}</nav>
<p>{filler}</p>
<form><input type="password" placeholder="Password" value="hunter2-secret">
<ul><li role="menuitem" value="3" style="display:block;width:30px;height:30px"></li></ul></form>
<iframe srcdoc="<p>Words that only exist inside the embedded frame of this page.</p>"></iframe>
<button id="next" onclick="location.href = 'done.html'">Next step</button>
<p>END-MARKER</p>
"""


# Serves the folder; the page after the button takes a while, as a real form submission does.
SLOW_SERVER = """
import http.server, time
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith('done.html'):
            time.sleep(1.5)
        super().do_GET()
http.server.ThreadingHTTPServer(('127.0.0.1', 8899), Handler).serve_forever()
"""


def test_the_browser_copes_with_an_awkward_page(office: Office, team: dict[str, int]) -> None:
    alice = team["alice"]
    page = PAGE.format(links="".join(f'<a href="#l{n}">link {n}</a> ' for n in range(300)),
                       filler=" ".join(f"word{n}" for n in range(4000)))
    office.model.script("alice", [
        tool("file_write", path="site/index.html", content=page),
        tool("file_write", path="site/done.html", content="<title>Done</title><p>You made it.</p>"),
        # Starts a server that keeps running: the command must still return at once.
        tool("file_write", path="site/serve.py", content=SLOW_SERVER),
        tool("shell_exec", command="cd ~/site && python3 serve.py >/dev/null 2>&1 & sleep 1; echo up"),
        tool("browser_goto", url="http://127.0.0.1:8899/index.html"),
        tool("browser_scroll", direction="down", pages=20),
        tool("browser_read"),
        tool("browser_read", offset=10_000),
        tool("browser_read", offset=20_000),
        tool("browser_read", offset=30_000),
        # The model clicks what the page it was last shown calls "Next step".
        lambda request: tool("browser_click", ref=next((
            element["ref"] for element in json.loads(
                [m for m in request["messages"] if m["role"] == "tool"][-1]["content"])["elements"]
            if element["label"] == "Next step"), 0)),
        "done",
    ])
    started = time.time()
    run = office.ask(alice)
    assert run["status"] == "completed", run["error"]
    served, opened, scrolled, *reads, clicked = (call["result"] for call in run["tool_calls"][3:])
    everything = json.dumps([call["result"] for call in run["tool_calls"][4:]])  # all the browser showed

    assert served["stdout"] == "up\n" and not served["timed_out"] and time.time() - started < 90
    assert "hunter2-secret" not in everything  # a typed password never reaches the model
    assert opened["title"] == "Sign in", {k: str(v)[:300] for k, v in opened.items()}
    assert len(opened["elements"]) == 40 and "of 30" in opened["elements_truncated"]
    assert "Next step" not in [element["label"] for element in opened["elements"]]  # 300 links come first

    labels = {element["label"]: element for element in scrolled["elements"]}  # after scrolling: what is on screen
    assert "Next step" in labels, (scrolled["scroll_y"], scrolled["page_height"], list(labels)[:8]) and labels["Password"]["type"] == "password" and scrolled["scroll_y"] > 0

    text = "".join(read["text"] for read in reads)
    assert reads[0]["next_offset"] == 10_000 and not reads[-1]["text_truncated"]
    assert "word0 " in text and "word3999" in text and "END-MARKER" in text
    assert "only exist inside the embedded frame" in text
    assert clicked["url"].endswith("/done.html") and clicked["title"] == "Done"  # waited for the page to change


def test_each_agent_follows_its_own_permissions(office: Office, team: dict[str, int]) -> None:
    alice, bob = team["alice"], team["bob"]
    office.api("PATCH", f"/api/agents/{alice}", json={"permissions": {"shell": "ask", "browser": "off"}})

    # "Ask me first": even a harmless command waits for the user.
    office.model.script("alice", [tool("shell_exec", command="echo harmless"), "done"])
    run_id = office.send(alice, "say something")
    office.wait_until(lambda: office.run(run_id)["pending_approval"] is not None, 30, "the approval request")
    assert "confirm every action" in office.run(run_id)["pending_approval"]["reason"]
    office.api("POST", f"/api/approvals/{office.run(run_id)['pending_approval']['id']}", json={"approved": True})
    assert result(office.finished(run_id, 30))["stdout"] == "harmless\n"

    # "Not allowed": the model is not offered the tool, and is refused if it calls it anyway.
    offered = {spec["function"]["name"] for spec in office.model.requests["alice"][-1]["tools"]}
    assert "shell_exec" in offered and not {name for name in offered if name.startswith("browser_")}
    office.model.script("alice", [tool("browser_goto", url="https://example.com"), "done"])
    refused = office.ask(alice)
    assert calls(refused)[0][1] == "blocked" and "error" in result(refused, 0)

    # The colleague next door is unaffected.
    assert shell(office, "bob", bob, "echo free")["stdout"] == "free\n"
    office.api("PATCH", f"/api/agents/{alice}", json={"permissions": {"shell": "allow", "browser": "allow"}})
    assert shell(office, "alice", alice, "echo back to normal")["stdout"] == "back to normal\n"


def test_turning_a_computer_off_stops_the_agents_task(office: Office, team: dict[str, int]) -> None:
    bob = team["bob"]
    office.model.script("bob", [tool("shell_exec", command="sleep 120"), "never reached"])
    run_id = office.send(bob, "long job")
    office.wait_until(lambda: len(office.run(run_id)["tool_calls"]) == 1, 30, "the long command to start")
    started = time.time()
    assert office.api("POST", f"/api/agents/{bob}/vm/stop").json()["vm_status"] == "stopped"
    run = office.run(run_id)
    assert run["status"] == "cancelled" and time.time() - started < 30
    assert office.api("GET", "/api/agents").json()[1]["activity"] == "idle"
    office.turn_on(bob)
    office.model.scripts["bob"].clear()
    assert shell(office, "bob", bob, "echo back")["stdout"] == "back\n"


def backend_shutdown_step(office: Office) -> None:
    """The step the app's launcher runs after the window closes."""
    subprocess.run([sys.executable, "-m", "backend.shutdown"], cwd=REPO, check=True, timeout=120,
                   env={**os.environ, "AGENT_OFFICE_HOME": str(office.home)}, capture_output=True)


def test_quitting_the_app_respects_the_keep_running_setting(office: Office, team: dict[str, int]) -> None:
    assert len(office.running_vm_pids()) == 2
    office.api("PUT", "/api/settings", json={"keep_vms_running_on_quit": True})
    office.stop_backend()
    backend_shutdown_step(office)
    assert len(office.running_vm_pids()) == 2  # kept, as asked

    office.start_backend()  # the app is opened again: it finds both computers still on
    statuses = {a["name"]: a["vm_status"] for a in office.api("GET", "/api/agents").json()}
    assert statuses == {"alice": "running", "bob": "running"}
    office.wait_until(lambda: office.api("GET", f"/api/agents/{team['alice']}/vm/guest").json()["ready"], 60, "alice")
    assert shell(office, "alice", team["alice"], "cat ~/notes/kept.txt")["stdout"] == "still here"

    office.api("PUT", "/api/settings", json={"keep_vms_running_on_quit": False})
    office.stop_backend()
    backend_shutdown_step(office)
    assert office.running_vm_pids() == []  # the default: nothing left running unseen
    office.start_backend()
    statuses = {a["name"]: a["vm_status"] for a in office.api("GET", "/api/agents").json()}
    assert statuses == {"alice": "stopped", "bob": "stopped"}


def test_deleting_an_agent_removes_everything_it_had(office: Office, team: dict[str, int]) -> None:
    bob = team["bob"]
    bob_dir, secret = office.home / "agents" / "bob", office.home / "secrets" / "bob.guest-secret"
    assert (bob_dir / "disk.qcow2").exists() and secret.exists()
    assert office.api("DELETE", f"/api/agents/{bob}").status_code == 400  # needs confirmation
    assert office.api("DELETE", f"/api/agents/{bob}?confirm=true").status_code == 204
    assert not bob_dir.exists() and not secret.exists()
    assert [a["name"] for a in office.api("GET", "/api/agents").json()] == ["alice"]
    assert Path(office.home / "agents" / "alice" / "disk.qcow2").exists()
