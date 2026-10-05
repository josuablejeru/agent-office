"""VM lifecycle interface and the QEMU-backed implementation."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import signal
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path

from backend.config import Settings
from backend.db.models import Agent
from backend.logging_config import get_logger
from backend.vm.capabilities import Capabilities, detect_capabilities, run_command
from backend.vm.errors import VMError
from backend.vm.guest import GuestClient, GuestError
from backend.vm.images import build_overlay_command, build_snapshot_command
from backend.vm.process import run_checked
from backend.vm.qemu import VMSpec, build_qemu_args, escape_option
from backend.vm.qmp import QMPError, qmp_execute
from backend.vm.seed import build_seed_iso

log = get_logger("vm")

PID_FILENAME = "qemu.pid"
SEED_FILENAME = "seed.iso"
CONSOLE_LOG_FILENAME = "console.log"
# Below this much free space a VM is not started: a guest whose disk writes
# fail gets paused by QEMU and looks alive while doing nothing.
MIN_FREE_BYTES = 5 * 1024**3
CONSOLE_LOG_MAX_BYTES = 5 * 1024**2
CONSOLE_LOG_KEEP_BYTES = 1024**2
# QMP run states in which the guest is not executing.
STALLED_STATES = {"paused", "io-error", "internal-error", "guest-panicked"}
SNAPSHOT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

CommandLookup = Callable[[int], Awaitable[str | None]]
Detector = Callable[[], Awaitable[Capabilities]]


class VMStatus(StrEnum):
    NOT_CREATED = "not_created"
    STOPPED = "stopped"
    RUNNING = "running"


class VMManager(ABC):
    """Manages one persistent VM per agent."""

    @abstractmethod
    async def create_agent_vm(self, agent: Agent) -> None:
        """Create the agent's qcow2 overlay disk and per-agent guest secret."""

    @abstractmethod
    async def start(self, agent: Agent) -> None: ...

    @abstractmethod
    async def stop(self, agent: Agent) -> None: ...

    @abstractmethod
    async def restart(self, agent: Agent) -> None: ...

    @abstractmethod
    async def delete(self, agent: Agent) -> None: ...

    @abstractmethod
    async def status(self, agent: Agent) -> VMStatus: ...

    @abstractmethod
    async def snapshot(self, agent: Agent, name: str) -> None: ...

    @abstractmethod
    async def problem(self, agent: Agent) -> str | None:
        """A user-facing reason why a running VM is not making progress, if any."""

    @abstractmethod
    def vnc_socket(self, agent: Agent) -> Path:
        """Unix socket on which the VM's display is served over VNC."""

    @abstractmethod
    def guest_secret(self, agent: Agent) -> str:
        """Secret shared with the agent's guest daemon. It never leaves this host and VM."""


def trim_log(path: Path) -> None:
    """Keep an append-only log bounded by dropping all but its tail."""
    try:
        if path.stat().st_size <= CONSOLE_LOG_MAX_BYTES:
            return
        with path.open("rb") as handle:
            handle.seek(-CONSOLE_LOG_KEEP_BYTES, os.SEEK_END)
            tail = handle.read()
        path.write_bytes(tail)
    except OSError:
        pass


async def process_command(pid: int) -> str | None:
    """Return the command line of a running process, or None if it is gone."""
    return await run_command("ps", "-ww", "-p", str(pid), "-o", "command=")


class QemuVMManager(VMManager):
    """Runs each agent VM as a daemonized QEMU process that outlives this app.

    Nothing about a running VM is held in memory: state is derived from the
    agent directory (disk, PID file), so it is recovered after an app restart.
    """

    def __init__(
        self,
        settings: Settings,
        command_lookup: CommandLookup = process_command,
        detect: Detector = detect_capabilities,
        shutdown_timeout: float = 60.0,
    ) -> None:
        self._settings = settings
        self._command_lookup = command_lookup
        self._detect = detect
        self._shutdown_timeout = shutdown_timeout
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def pid_file(self, agent: Agent) -> Path:
        return self._settings.agent_dir(agent.name) / PID_FILENAME

    def seed_iso(self, agent: Agent) -> Path:
        return self._settings.agent_dir(agent.name) / SEED_FILENAME

    def console_log(self, agent: Agent) -> Path:
        return self._settings.agent_dir(agent.name) / "logs" / CONSOLE_LOG_FILENAME

    def qmp_socket(self, agent: Agent) -> Path:
        if agent.id is None:
            raise VMError("agent has not been saved yet")
        return self._settings.qmp_socket_path(agent.id)

    def vnc_socket(self, agent: Agent) -> Path:
        if agent.id is None:
            raise VMError("agent has not been saved yet")
        return self._settings.vnc_socket_path(agent.id)

    async def status(self, agent: Agent) -> VMStatus:
        if not Path(agent.vm_disk_path).exists():
            return VMStatus.NOT_CREATED
        if await self._live_pid(agent) is not None:
            return VMStatus.RUNNING
        return VMStatus.STOPPED

    async def problem(self, agent: Agent) -> str | None:
        try:
            reply = await qmp_execute(self.qmp_socket(agent), "query-status", timeout=2.0)
        except (QMPError, VMError):
            return None
        state = str(reply.get("return", {}).get("status", ""))
        if state not in STALLED_STATES:
            return None
        if shutil.disk_usage(self._settings.home).free < MIN_FREE_BYTES:
            return "This computer is paused because the Mac's disk is almost full. Free up space, then restart it."
        return f"This computer has stopped responding ({state}). Restart it."

    async def create_agent_vm(self, agent: Agent) -> None:
        disk = Path(agent.vm_disk_path)
        if disk.exists():
            return
        base = self._settings.base_image_path(agent.vm_image)
        if not base.exists():
            raise VMError(
                f"Base image {base} does not exist. Build it with scripts/create-base-image.sh."
            )
        caps = await self._require_capabilities()
        assert caps.qemu_img_path is not None

        self.guest_secret(agent)
        await run_checked(*build_overlay_command(base, disk, caps.qemu_img_path))
        log.info("vm created", extra={"agent": agent.name, "disk": str(disk), "base": str(base)})

    async def start(self, agent: Agent) -> None:
        async with self._locks[agent.name]:
            await self._start(agent)

    async def stop(self, agent: Agent) -> None:
        async with self._locks[agent.name]:
            await self._stop(agent)

    async def restart(self, agent: Agent) -> None:
        async with self._locks[agent.name]:
            await self._stop(agent)
            await self._start(agent)

    async def delete(self, agent: Agent) -> None:
        """Remove VM artefacts stored outside the agent directory."""
        async with self._locks[agent.name]:
            if await self._live_pid(agent) is not None:
                raise VMError("Stop the VM before deleting it.")
            self._settings.guest_secret_path(agent.name).unlink(missing_ok=True)
            self.qmp_socket(agent).unlink(missing_ok=True)
            self.vnc_socket(agent).unlink(missing_ok=True)
            for path in (Path(agent.vm_disk_path), self.seed_iso(agent), self.pid_file(agent)):
                path.unlink(missing_ok=True)
            log.info("vm deleted", extra={"agent": agent.name})

    async def snapshot(self, agent: Agent, name: str) -> None:
        if not SNAPSHOT_NAME_PATTERN.match(name):
            raise VMError("Snapshot names may contain letters, digits, '.', '_' and '-'.")
        async with self._locks[agent.name]:
            status = await self.status(agent)
            if status == VMStatus.NOT_CREATED:
                raise VMError("The VM has no disk yet.")
            if status == VMStatus.RUNNING:
                raise VMError("Stop the VM before taking a snapshot.")
            caps = await self._require_capabilities()
            assert caps.qemu_img_path is not None
            await run_checked(
                *build_snapshot_command(Path(agent.vm_disk_path), name, caps.qemu_img_path)
            )
            log.info("vm snapshot created", extra={"agent": agent.name, "snapshot": name})

    async def _start(self, agent: Agent) -> None:
        if await self._live_pid(agent) is not None:
            return
        if agent.vm_daemon_port is None:
            raise VMError("The VM's guest daemon port has not been assigned.")
        free = shutil.disk_usage(self._settings.home).free
        if free < MIN_FREE_BYTES:
            raise VMError(
                f"Only {free / 1024**3:.1f} GB of disk space is free. Free up at least "
                f"{MIN_FREE_BYTES // 1024**3} GB before starting an agent's computer."
            )
        caps = await self._require_capabilities()
        assert caps.qemu_system_path is not None and caps.firmware_path is not None
        await self.create_agent_vm(agent)

        socket_path = self.qmp_socket(agent)
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        self.vnc_socket(agent).unlink(missing_ok=True)
        self.pid_file(agent).unlink(missing_ok=True)
        self.console_log(agent).parent.mkdir(parents=True, exist_ok=True)
        trim_log(self.console_log(agent))
        # Rebuilt on every start so the guest always runs the current daemon code.
        seed = self.seed_iso(agent)
        await build_seed_iso(seed, agent.name, self.guest_secret(agent))

        spec = VMSpec(
            name=agent.name,
            disk_path=Path(agent.vm_disk_path),
            firmware_path=Path(caps.firmware_path),
            memory_mb=agent.vm_memory_mb,
            cpus=agent.vm_cpus,
            daemon_host_port=agent.vm_daemon_port,
            vnc_socket_path=self.vnc_socket(agent),
            qmp_socket_path=socket_path,
            pid_file_path=self.pid_file(agent),
            console_log_path=self.console_log(agent),
            seed_iso_path=seed,
        )
        # QEMU daemonizes itself: this returns once the VM is up or has failed.
        await run_checked(*build_qemu_args(spec, caps.qemu_system_path))
        pid = await self._live_pid(agent)
        if pid is None:
            raise VMError("QEMU exited immediately after starting.")
        log.info(
            "vm started",
            extra={
                "agent": agent.name,
                "pid": pid,
                "daemon_port": spec.daemon_host_port,
            },
        )

    async def _stop(self, agent: Agent) -> None:
        pid = await self._live_pid(agent)
        if pid is not None:
            method = await self._shut_down(agent, pid)
            log.info("vm stopped", extra={"agent": agent.name, "pid": pid, "method": method})
        self.pid_file(agent).unlink(missing_ok=True)
        if agent.id is not None:
            self.qmp_socket(agent).unlink(missing_ok=True)
            self.vnc_socket(agent).unlink(missing_ok=True)

    async def _shut_down(self, agent: Agent, pid: int) -> str:
        """Stop QEMU, escalating from guest powerdown to quit to SIGKILL."""
        await self._prepare_guest_for_shutdown(agent)
        socket_path = self.qmp_socket(agent)
        for command, wait in (("system_powerdown", self._shutdown_timeout), ("quit", 10.0)):
            try:
                await qmp_execute(socket_path, command)
            except QMPError as exc:
                log.warning(
                    "qmp command failed",
                    extra={"agent": agent.name, "command": command, "error": str(exc)},
                )
                continue
            if await self._wait_for_exit(agent, wait):
                return command
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await self._wait_for_exit(agent, 5.0)
        return "kill"

    async def _prepare_guest_for_shutdown(self, agent: Agent) -> None:
        """Let the guest's browser quit cleanly so its profile is fully written to disk."""
        if agent.vm_daemon_port is None:
            return
        client = GuestClient(agent.vm_daemon_port, self.guest_secret(agent))
        try:
            await client.call("browser.close", timeout=15.0)
        except GuestError as exc:
            # Not fatal: the guest may still be booting or have no browser.
            log.info("guest not prepared for shutdown", extra={"agent": agent.name, "reason": str(exc)})

    async def _wait_for_exit(self, agent: Agent, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while await self._live_pid(agent) is not None:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return True

    async def _live_pid(self, agent: Agent) -> int | None:
        """PID of this agent's QEMU process, guarding against stale or reused PIDs."""
        try:
            pid = int(self.pid_file(agent).read_text().strip())
        except (OSError, ValueError):
            return None
        command = await self._command_lookup(pid)
        if command is None or escape_option(agent.vm_disk_path) not in command:
            return None
        return pid

    async def _require_capabilities(self) -> Capabilities:
        caps = await self._detect()
        if not caps.ready:
            raise VMError("This Mac cannot run agent VMs: " + " ".join(caps.problems))
        return caps

    def guest_secret(self, agent: Agent) -> str:
        """Return the per-agent guest daemon secret, generating it on first use."""
        path = self._settings.guest_secret_path(agent.name)
        if path.exists():
            return path.read_text().strip()
        secret = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(secret + "\n")
        return secret
