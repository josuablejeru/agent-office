"""Rebuilding the system image while agents use it, and moving agents onto it with their files."""

from __future__ import annotations

import asyncio
import io
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from backend.config import Settings
from backend.db.database import create_db_engine, init_db
from backend.db.models import Agent
from backend.vm import generations
from backend.vm.base_image import BaseImageBuilder
from backend.vm.lifecycle import VMStatus
from backend.vm.update import ComputerUpdater
from guest import carry
from guest.validation import OperationError

needs_qemu_img = pytest.mark.skipif(shutil.which("qemu-img") is None, reason="needs qemu-img")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def use_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point the guest's carry module at one computer's home folder."""
    monkeypatch.setattr(carry, "HOME", home)
    monkeypatch.setattr(carry, "STATE_DIR", home / ".agent-office")
    monkeypatch.setattr(carry, "OUTGOING", home / ".agent-office" / "carry-out.tar.gz")
    monkeypatch.setattr(carry, "INCOMING", home / ".agent-office" / "carry-in.tar.gz")


def fill(home: Path) -> None:
    """An agent's computer with a Shared folder, a database in use and a memory."""
    (home / "Shared" / "reports").mkdir(parents=True)
    (home / "Shared" / "reports" / "q3.txt").write_text("numbers")
    (home / "Shared" / "big.bin").write_bytes(b"x" * (3 * 1024**2))  # more than one chunk
    (home / "Databases").mkdir()
    leads = sqlite3.connect(home / "Databases" / "leads.sqlite")
    leads.execute("PRAGMA journal_mode=WAL")
    leads.execute("CREATE TABLE lead (name TEXT)")
    leads.execute("INSERT INTO lead VALUES ('Dana')")
    leads.commit()  # left open: the row is still only in the -wal file
    (home / ".agent-office").mkdir()
    memory = sqlite3.connect(home / ".agent-office" / "memory.sqlite")
    memory.execute("CREATE TABLE fact (note TEXT)")
    memory.execute("INSERT INTO fact VALUES ('accountant is Dana')")
    memory.commit()
    memory.close()
    # Not carried: anything else in the home folder.
    (home / "Downloads").mkdir()
    (home / "Downloads" / "setup.deb").write_text("x")
    fill.open_connection = leads  # type: ignore[attr-defined]


def check_carried(home: Path) -> None:
    assert (home / "Shared" / "reports" / "q3.txt").read_text() == "numbers"
    assert (home / "Shared" / "big.bin").stat().st_size == 3 * 1024**2
    rows = sqlite3.connect(home / "Databases" / "leads.sqlite").execute("SELECT name FROM lead").fetchall()
    assert rows == [("Dana",)]
    facts = sqlite3.connect(home / ".agent-office" / "memory.sqlite").execute("SELECT note FROM fact").fetchall()
    assert facts == [("accountant is Dana",)]
    assert not (home / "Downloads").exists()


def move(monkeypatch: pytest.MonkeyPatch, old: Path, new: Path) -> None:
    """Pack on one computer, unpack on another, through the guest operations."""
    use_home(monkeypatch, old)
    packed = run(carry.carry_pack({}))
    chunks, offset = [], 0
    while True:
        piece = run(carry.carry_read_chunk({"offset": offset}))
        chunks.append(piece["data"])
        offset += len(__import__("base64").b64decode(piece["data"]))
        if piece["eof"]:
            break
    use_home(monkeypatch, new)
    offset = 0
    for data in chunks:
        run(carry.carry_write_chunk({"offset": offset, "data": data}))
        offset += len(__import__("base64").b64decode(data))
    run(carry.carry_unpack({"sha256": packed["sha256"]}))
    assert packed["memory"] is True and packed["databases"] == 1


# --- the guest side --------------------------------------------------------------------------


def test_the_agents_files_move_to_a_new_computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    fill(old)
    # The new computer already has an empty memory of its own: the carried one replaces it.
    (new / ".agent-office").mkdir()
    sqlite3.connect(new / ".agent-office" / "memory.sqlite").execute("CREATE TABLE fact (note TEXT)")
    move(monkeypatch, old, new)
    check_carried(new)
    assert not list((new / ".agent-office").glob("carry-*"))


def test_a_damaged_archive_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_home(monkeypatch, tmp_path)
    run(carry.carry_write_chunk({"offset": 0, "data": "aGVsbG8="}))
    with pytest.raises(OperationError, match="damaged"):
        run(carry.carry_unpack({"sha256": "0" * 64}))


def test_unpacking_never_writes_outside_the_carried_places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive comes from the agent's old computer, which the agent controlled."""
    home = tmp_path / "home"
    home.mkdir()
    use_home(monkeypatch, home)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        def add(name: str, data: bytes = b"x", kind: bytes = tarfile.REGTYPE, link: str = "") -> None:
            info = tarfile.TarInfo(name)
            info.type, info.linkname, info.size = kind, link, len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        add("Shared/ok.txt")
        add(".bashrc")  # not a carried place
        add("Shared/../../escape.txt")
        add("Shared/link", kind=tarfile.SYMTYPE, link="/etc/passwd")
    import base64
    import hashlib

    data = buffer.getvalue()
    run(carry.carry_write_chunk({"offset": 0, "data": base64.b64encode(data).decode()}))
    with pytest.raises(tarfile.TarError):
        run(carry.carry_unpack({"sha256": hashlib.sha256(data).hexdigest()}))
    assert not (home / ".bashrc").exists() and not (tmp_path / "escape.txt").exists()
    assert not (home / "Shared" / "link").exists()


# --- system image versions -------------------------------------------------------------------


def make_image(path: Path) -> None:
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(path), "16M"], check=True, capture_output=True)


def make_disk(settings: Settings, name: str) -> Path:
    disk = settings.agent_dir(name) / "disk.qcow2"
    disk.parent.mkdir(parents=True, exist_ok=True)
    base = settings.base_image_path("debian-desktop")
    subprocess.run(["qemu-img", "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(disk)],
                   check=True, capture_output=True)
    return disk


def test_without_a_pointer_the_plain_image_is_current(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    settings.ensure_layout()
    assert settings.base_image_path("debian-desktop") == settings.images_dir / "debian-desktop.qcow2"
    generations.set_current(settings, "debian-desktop", "debian-desktop-20261126T101500Z")
    assert settings.base_image_path("debian-desktop").name == "debian-desktop-20261126T101500Z.qcow2"
    for damaged in ("../../etc/x", "other-20261126T101500Z", ""):
        generations.pointer_path(settings, "debian-desktop").write_text(damaged)
        assert settings.base_image_path("debian-desktop").name == "debian-desktop.qcow2"


@needs_qemu_img
def test_images_in_use_are_kept_and_unused_ones_removed(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    settings.ensure_layout()
    make_image(settings.images_dir / "debian-desktop.qcow2")
    disk = make_disk(settings, "ada")
    newer = "debian-desktop-20261126T101500Z"
    make_image(settings.images_dir / f"{newer}.qcow2")
    generations.set_current(settings, "debian-desktop", newer)
    assert generations.backing_file(disk) == settings.images_dir / "debian-desktop.qcow2"

    assert generations.remove_unused(settings, "debian-desktop") == []  # ada's disk still uses it
    disk.rename(disk.with_name("disk.previous.qcow2"))  # set aside during an update: still counts
    assert generations.remove_unused(settings, "debian-desktop") == []
    disk.with_name("disk.previous.qcow2").unlink()
    assert generations.remove_unused(settings, "debian-desktop") == ["debian-desktop"]
    assert (settings.images_dir / f"{newer}.qcow2").exists()  # the current one always stays


@needs_qemu_img
def test_a_rebuild_goes_beside_the_image_in_use(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    make_image(settings.images_dir / "debian-desktop.qcow2")
    disk = make_disk(settings, "ada")
    script = tmp_path / "build.sh"
    script.write_text(
        "#!/bin/bash\nset -e\n"
        'qemu-img create -f qcow2 "$AGENT_OFFICE_HOME/images/$IMAGE_NAME.qcow2" 16M >/dev/null\n'
        'echo "built $IMAGE_NAME"\n'
    )
    builder = BaseImageBuilder(settings, script=script)

    async def scenario() -> None:
        assert builder.start().building is False  # without rebuild=True, an existing image stays
        status = builder.start(rebuild=True)
        assert status.building and status.rebuilding
        await builder._task  # noqa: SLF001

    run(scenario())
    status = builder.status()
    assert status.ready and status.version.startswith("debian-desktop-") and not status.building
    # Ada's computer is untouched and still on the old image, which is kept for it.
    assert generations.backing_file(disk) == settings.images_dir / "debian-desktop.qcow2"
    assert (settings.images_dir / "debian-desktop.qcow2").exists()
    assert settings.base_image_path("debian-desktop").name == f"{status.version}.qcow2"


# --- the update, end to end with stand-in computers ------------------------------------------


class FakeVMs:
    """Computers whose home folder belongs to their disk file, as on a real disk."""

    def __init__(self, settings: Settings, root: Path) -> None:
        self.settings, self.root = settings, root
        self.running: set[str] = set()
        self.homes: dict[int, Path] = {}
        self.fail_unpack = False

    def home(self, agent: Agent) -> Path:
        inode = Path(agent.vm_disk_path).stat().st_ino
        if inode not in self.homes:
            self.homes[inode] = self.root / f"home-{len(self.homes)}"
            self.homes[inode].mkdir()
        return self.homes[inode]

    async def status(self, agent: Agent) -> VMStatus:
        if not Path(agent.vm_disk_path).exists():
            return VMStatus.NOT_CREATED
        return VMStatus.RUNNING if agent.name in self.running else VMStatus.STOPPED

    async def start(self, agent: Agent) -> None:
        disk = Path(agent.vm_disk_path)
        if not disk.exists():
            base = self.settings.base_image_path(agent.vm_image)
            await asyncio.to_thread(
                subprocess.run, ["qemu-img", "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(disk)],
                check=True, capture_output=True,
            )
        self.running.add(agent.name)

    async def stop(self, agent: Agent) -> None:
        self.running.discard(agent.name)

    def guest_secret(self, agent: Agent) -> str:
        return "secret"


class FakeRuns:
    def __init__(self) -> None:
        self.held: list[int] = []
        self.released: list[int] = []

    def hold(self, agent_id: int, reason: str) -> None:
        self.held.append(agent_id)

    def release(self, agent_id: int) -> None:
        self.released.append(agent_id)

    async def cancel(self, agent_id: int) -> bool:
        return False


def updater_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ComputerUpdater, FakeVMs, Any, int]:
    settings = Settings(home=tmp_path / "home")
    settings.ensure_layout()
    make_image(settings.images_dir / "debian-desktop.qcow2")
    engine = create_db_engine(settings.db_path)
    init_db(engine)
    with Session(engine) as session:
        agent = Agent(name="ada", provider="p", model="m", vm_disk_path=str(settings.agent_dir("ada") / "disk.qcow2"))
        session.add(agent)
        session.commit()
        session.refresh(agent)
        agent_id = agent.id
    settings.agent_dir("ada").mkdir(parents=True)
    vms = FakeVMs(settings, tmp_path)
    runs = FakeRuns()

    class Client:
        def __init__(self, port: int, secret: str) -> None:
            pass

        async def wait_ready(self, timeout: float = 0) -> None:
            pass

        async def call(self, op: str, args: dict[str, Any] | None = None, timeout: float = 0) -> dict[str, Any]:
            with Session(engine) as session:
                current = session.get(Agent, agent_id)
            use_home(monkeypatch, vms.home(current))
            if op == "carry.unpack" and vms.fail_unpack:
                raise OperationError("disk full")
            name = op.replace(".", "_")
            return await getattr(carry, name)(args or {})

    updater = ComputerUpdater(engine, settings, vms, runs, client_factory=Client)  # type: ignore[arg-type]
    with Session(engine) as session:
        run(vms.start(session.get(Agent, agent_id)))
        fill(vms.home(session.get(Agent, agent_id)))
    return updater, vms, engine, agent_id


@needs_qemu_img
def test_an_agent_moves_to_the_rebuilt_image_with_its_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    updater, vms, engine, agent_id = updater_for(tmp_path, monkeypatch)
    settings = updater._settings  # noqa: SLF001
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
    assert not updater.is_outdated(agent)
    newer = "debian-desktop-20261126T101500Z"
    make_image(settings.images_dir / f"{newer}.qcow2")
    generations.set_current(settings, "debian-desktop", newer)
    assert updater.is_outdated(agent)

    async def scenario() -> None:
        updater.request([agent_id])
        await updater.wait()

    run(scenario())
    assert updater.status(agent_id).state == "done", updater.status(agent_id).detail
    disk = Path(agent.vm_disk_path)
    assert generations.backing_file(disk) == settings.images_dir / f"{newer}.qcow2"
    check_carried(vms.home(agent))
    assert not updater.is_outdated(agent)
    # Nothing left behind: no old disk, no archive, and the old image is gone.
    assert not disk.with_name("disk.previous.qcow2").exists()
    assert not list(settings.agent_dir("ada").glob("carry-over*"))
    assert not (settings.images_dir / "debian-desktop.qcow2").exists()
    assert "ada" in vms.running  # it was on, so it is on again
    assert updater._runs.held == [agent_id] == updater._runs.released  # noqa: SLF001


@needs_qemu_img
def test_a_failed_update_puts_the_old_computer_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    updater, vms, engine, agent_id = updater_for(tmp_path, monkeypatch)
    settings = updater._settings  # noqa: SLF001
    newer = "debian-desktop-20261126T101500Z"
    make_image(settings.images_dir / f"{newer}.qcow2")
    generations.set_current(settings, "debian-desktop", newer)
    vms.fail_unpack = True

    async def scenario() -> None:
        updater.request([agent_id])
        await updater.wait()

    run(scenario())
    status = updater.status(agent_id)
    assert status.state == "failed" and "disk full" in status.detail
    with Session(engine) as session:
        agent = session.get(Agent, agent_id)
    disk = Path(agent.vm_disk_path)
    # The old disk, with everything on it, is back; the old image it needs is kept.
    assert generations.backing_file(disk) == settings.images_dir / "debian-desktop.qcow2"
    assert (vms.home(agent) / "Downloads" / "setup.deb").exists()
    assert not disk.with_name("disk.previous.qcow2").exists()
    assert (settings.images_dir / "debian-desktop.qcow2").exists()
    assert not list(settings.agent_dir("ada").glob("carry-over*"))
    assert updater._runs.released == [agent_id]  # noqa: SLF001
