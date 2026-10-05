from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from backend.config import MAX_UNIX_SOCKET_PATH, Settings
from backend.db.models import Agent
from backend.vm.capabilities import Capabilities
from backend.vm.errors import VMError
from backend.vm.lifecycle import PID_FILENAME, QemuVMManager, VMStatus
from backend.vm.ports import allocate_daemon_port
from backend.vm.qmp import QMPError, qmp_execute
from backend.vm.seed import GUEST_SOURCE_DIR, render_meta_data, stage_seed


def make_agent(settings: Settings, name: str = "a1") -> Agent:
    agent_dir = settings.agent_dir(name)
    agent_dir.mkdir(parents=True)
    return Agent(
        id=1, name=name, provider="p", model="m", vm_disk_path=str(agent_dir / "disk.qcow2")
    )


async def not_ready() -> Capabilities:
    return Capabilities(
        system="Darwin", machine="arm64", apple_silicon=True, hvf_supported=True,
        qemu_system_path=None, qemu_version=None, qemu_img_path=None,
        hvf_accel_available=False, firmware_path=None, ready=False,
        problems=["qemu-system-aarch64 not found."],
    )


def test_status_requires_pid_to_be_this_agents_qemu(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    agent = make_agent(settings)
    commands: dict[int, str] = {}

    async def lookup(pid: int) -> str | None:
        return commands.get(pid)

    manager = QemuVMManager(settings, command_lookup=lookup)
    pid_file = settings.agent_dir("a1") / PID_FILENAME

    assert asyncio.run(manager.status(agent)) == VMStatus.NOT_CREATED
    Path(agent.vm_disk_path).touch()
    assert asyncio.run(manager.status(agent)) == VMStatus.STOPPED

    pid_file.write_text("4242")
    assert asyncio.run(manager.status(agent)) == VMStatus.STOPPED  # process is gone
    commands[4242] = "/usr/bin/some-other-program"
    assert asyncio.run(manager.status(agent)) == VMStatus.STOPPED  # PID was reused
    commands[4242] = f"qemu-system-aarch64 -drive if=none,file={agent.vm_disk_path},format=qcow2"
    assert asyncio.run(manager.status(agent)) == VMStatus.RUNNING

    pid_file.write_text("not-a-pid")
    assert asyncio.run(manager.status(agent)) == VMStatus.STOPPED


def test_start_fails_cleanly_without_base_image_or_qemu(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    settings.ensure_layout()
    agent = make_agent(settings)
    agent.vm_daemon_port = 18000

    with pytest.raises(VMError, match="cannot run agent VMs"):
        asyncio.run(QemuVMManager(settings, detect=not_ready).start(agent))
    with pytest.raises(VMError, match="create-base-image.sh"):
        asyncio.run(QemuVMManager(settings).create_agent_vm(agent))
    assert not Path(agent.vm_disk_path).exists()


def test_start_requires_assigned_ports(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    with pytest.raises(VMError, match="port"):
        asyncio.run(QemuVMManager(settings).start(make_agent(settings)))


def test_snapshot_validates_name_and_state(tmp_path: Path) -> None:
    settings = Settings(home=tmp_path)
    agent = make_agent(settings)
    manager = QemuVMManager(settings)
    with pytest.raises(VMError, match="Snapshot names"):
        asyncio.run(manager.snapshot(agent, "bad name; rm -rf"))
    with pytest.raises(VMError, match="no disk"):
        asyncio.run(manager.snapshot(agent, "before-upgrade"))


def test_qmp_socket_path_always_fits_unix_socket_limit(tmp_path: Path) -> None:
    short = Settings(home=Path("/Users/someone/.config/agent-office"))
    assert short.qmp_socket_path(7) == Path("/Users/someone/.config/agent-office/run/7.qmp")

    long_home = Settings(home=tmp_path / ("x" * 120))
    path = long_home.qmp_socket_path(123456)
    assert len(os.fsencode(path)) <= MAX_UNIX_SOCKET_PATH
    assert path != Settings(home=tmp_path / ("y" * 120)).qmp_socket_path(123456)
    assert len(os.fsencode(long_home.vnc_socket_path(123456))) <= MAX_UNIX_SOCKET_PATH


def test_port_allocation_skips_taken_and_busy_ports() -> None:
    assert allocate_daemon_port({18000, 18001}, is_free=lambda port: port != 18002) == 18003
    with pytest.raises(VMError):
        allocate_daemon_port(set(), is_free=lambda _: False)


def test_seed_carries_identity_secret_and_daemon(tmp_path: Path) -> None:
    meta = yaml.safe_load(render_meta_data("research-agent"))
    assert meta == {"instance-id": "agent-office-research-agent", "local-hostname": "research-agent"}

    staging = tmp_path / "seed"
    stage_seed(staging, "research-agent", "s3cret", GUEST_SOURCE_DIR)
    assert (staging / "agent-secret").read_text() == "s3cret\n"
    assert (staging / "user-data").read_text().startswith("#cloud-config\n")
    shipped = {path.name for path in (staging / "guest").iterdir()}
    assert {"__init__.py", "agentd.py", "shell.py", "files.py", "validation.py"} <= shipped


@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="gl-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_qmp_execute_negotiates_then_runs_command(short_dir: Path) -> None:
    received: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'{"QMP": {"version": {}}}\n')
        while line := await reader.readline():
            command = json.loads(line)["execute"]
            received.append(command)
            if command == "bogus":
                writer.write(b'{"error": {"class": "CommandNotFound", "desc": "no such command"}}\n')
            else:
                writer.write(b'{"event": "NOISE"}\n{"return": {}}\n')
            await writer.drain()
        writer.close()

    async def scenario() -> None:
        socket_path = short_dir / "qmp.sock"
        server = await asyncio.start_unix_server(handle, str(socket_path))
        async with server:
            assert await qmp_execute(socket_path, "system_powerdown") == {"return": {}}
            with pytest.raises(QMPError, match="no such command"):
                await qmp_execute(socket_path, "bogus")
        with pytest.raises(QMPError):
            await qmp_execute(short_dir / "missing.sock", "quit")

    asyncio.run(scenario())
    assert received == ["qmp_capabilities", "system_powerdown", "qmp_capabilities", "bogus"]


def test_shutdown_stops_only_running_vms(tmp_path: Path) -> None:
    from sqlmodel import Session

    from backend.db.database import create_db_engine, init_db
    from backend.shutdown import stop_all_vms

    settings = Settings(home=tmp_path)
    settings.ensure_layout()
    engine = create_db_engine(settings.db_path)
    init_db(engine)
    with Session(engine) as session:
        for name in ("up", "down"):
            session.add(Agent(name=name, provider="p", model="m", vm_status="running",
                              vm_disk_path=str(tmp_path / f"{name}.qcow2")))
        session.commit()

    class FakeVMs:
        def __init__(self) -> None:
            self.running, self.stopped = {"up"}, []

        async def status(self, agent: Agent) -> VMStatus:
            return VMStatus.RUNNING if agent.name in self.running else VMStatus.STOPPED

        async def stop(self, agent: Agent) -> None:
            self.running.discard(agent.name)
            self.stopped.append(agent.name)

    vms = FakeVMs()
    assert asyncio.run(stop_all_vms(settings, vms)) == 1  # type: ignore[arg-type]
    assert vms.stopped == ["up"]
    with Session(engine) as session:
        from sqlmodel import select

        assert {a.vm_status for a in session.exec(select(Agent)).all()} == {"stopped"}
