"""Moving a real agent's computer onto a rebuilt system image, with its files.

Building an image takes 20 minutes, so the "rebuilt" image here is an APFS clone
of the base image under a new version name: what is checked is the update, not
the build.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

from backend.vm import generations
from backend.vm.guest import GuestClient
from tests.e2e.conftest import BASE_IMAGE, Office

SETUP = r"""
set -e
mkdir -p ~/Shared/reports ~/Databases ~/.agent-office ~/Downloads
echo numbers > ~/Shared/reports/q3.txt
head -c 5000000 /dev/urandom > ~/Shared/big.bin
sha256sum ~/Shared/big.bin | cut -d' ' -f1 > ~/Shared/big.sha
echo not-carried > ~/Downloads/setup.txt
python3 - <<'PY'
import sqlite3, os
home = os.path.expanduser("~")
db = sqlite3.connect(f"{home}/Databases/leads.sqlite")
db.execute("PRAGMA journal_mode=WAL"); db.execute("CREATE TABLE lead (name TEXT)")
db.execute("INSERT INTO lead VALUES ('Dana')"); db.commit()
m = sqlite3.connect(f"{home}/.agent-office/memory.sqlite")
m.execute("CREATE TABLE IF NOT EXISTS e2e_fact (note TEXT)")
m.execute("INSERT INTO e2e_fact VALUES ('accountant is Dana')"); m.commit()
PY
"""

CHECK = r"""
set -e
cat ~/Shared/reports/q3.txt
[ "$(sha256sum ~/Shared/big.bin | cut -d' ' -f1)" = "$(cat ~/Shared/big.sha)" ] && echo big-ok
python3 -c "
import sqlite3, os
h = os.path.expanduser('~')
print(sqlite3.connect(h + '/Databases/leads.sqlite').execute('SELECT name FROM lead').fetchall())
print(sqlite3.connect(h + '/.agent-office/memory.sqlite').execute('SELECT note FROM e2e_fact').fetchall())
"
[ -e ~/Downloads/setup.txt ] && echo downloads-still-there || echo downloads-gone
"""


def shell(office: Office, agent_id: int, name: str, command: str) -> str:
    port = office.api("GET", f"/api/agents/{agent_id}").json()["vm_daemon_port"]
    secret = (office.home / "secrets" / f"{name}.guest-secret").read_text().strip()
    result = asyncio.run(GuestClient(port, secret).call("shell.exec", {"command": command, "timeout": 60}, timeout=90))
    assert result["exit_code"] == 0, result
    return result["stdout"]


def test_an_agents_computer_moves_to_a_new_image_with_its_files(office: Office) -> None:
    real_mode = BASE_IMAGE.resolve().stat().st_mode
    agent_id = office.hire("mover")
    office.turn_on(agent_id)
    shell(office, agent_id, "mover", SETUP)

    images = office.home / "images"
    newer = "debian-desktop-20990101T000000Z"
    # A copy-on-write clone: instant, and takes no space.
    subprocess.run(["cp", "-c", str(BASE_IMAGE.resolve()), str(images / f"{newer}.qcow2")], check=True)
    (images / "debian-desktop.current").write_text(newer + "\n")

    computers = {c["name"]: c for c in office.api("GET", "/api/system/computers").json()}
    assert computers["mover"]["outdated"] is True

    response = office.api("POST", "/api/system/computers/update", json={"agent_ids": [agent_id]})
    assert response.status_code == 200, response.text
    # Nothing else may touch the computer meanwhile.
    assert office.api("POST", f"/api/agents/{agent_id}/vm/stop").status_code == 409

    state: dict = {}
    deadline = time.time() + 600
    while time.time() < deadline:
        state = {c["name"]: c for c in office.api("GET", "/api/system/computers").json()}["mover"]
        if state["update"]["state"] in ("done", "failed"):
            break
        time.sleep(2)
    assert state["update"]["state"] == "done", state
    assert state["outdated"] is False

    disk = office.home / "agents" / "mover" / "disk.qcow2"
    assert generations.backing_file(disk) == images / f"{newer}.qcow2"
    assert not disk.with_name("disk.previous.qcow2").exists()
    assert not list((office.home / "agents" / "mover").glob("carry-over*"))

    output = shell(office, agent_id, "mover", CHECK)
    assert "numbers" in output and "big-ok" in output
    assert "[('Dana',)]" in output and "[('accountant is Dana',)]" in output
    assert "downloads-gone" in output  # a fresh computer: only the agent's own files came along

    # The shared, read-only base image is untouched (the test home only had a link to it).
    assert BASE_IMAGE.resolve().exists() and BASE_IMAGE.resolve().stat().st_mode == real_mode
    assert os.access(BASE_IMAGE.resolve(), os.R_OK)
    office.api("POST", f"/api/agents/{agent_id}/vm/stop")
