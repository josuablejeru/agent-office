"""Moving an agent's computer onto the current system image, keeping the agent's own files.

An agent's disk is a thin layer over the system image it was made on, so moving
it to a rebuilt image means a fresh disk. The update carries over the agent's
Shared folder, databases and memory (see guest/carry.py), in an order where
nothing is lost until the new computer has them:

1. no runs for the agent; its computer (re)started, so it runs the current
   guest software that can pack;
2. its files packed on the old computer and fetched to the Mac, checked;
3. the old computer stopped and its disk set aside (disk.previous.qcow2);
4. the new computer started on a fresh disk over the current image;
5. the files sent to it, checked and unpacked;
6. only then the old disk and the archive deleted, and system images no disk
   uses any more removed.

If anything fails before step 6, the new disk is discarded and the old one put
back, so the agent is where it was. The archive comes from the agent's own
computer, so the host never opens it: it is only sized, checksummed and passed on.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from backend.agents.manager import AgentManager
from backend.config import Settings
from backend.db.models import Agent
from backend.logging_config import get_logger
from backend.vm import generations
from backend.vm.guest import GuestClient, GuestError
from backend.vm.lifecycle import VMError, VMManager, VMStatus

log = get_logger("vm")

DEFAULT_IMAGE = "debian-desktop"
ARCHIVE = "carry-over.tar.gz"
PREVIOUS_DISK = "disk.previous.qcow2"
MAX_ARCHIVE_BYTES = 4 * 1024**3
CHUNK_BYTES = 2 * 1024**2
READY_TIMEOUT = 240.0
HELD = "This agent's computer is being updated. Try again when it is done."

State = Literal["idle", "queued", "running", "done", "failed"]


class UpdateStatus(BaseModel):
    state: State = "idle"
    # What is happening now, or what went wrong.
    detail: str = ""


class UpdateError(Exception):
    pass


class ComputerUpdater:
    """Updates agents' computers one at a time, in the background."""

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        vms: VMManager,
        runs: Any,
        building: Callable[[], bool] = lambda: False,
        client_factory: Callable[[int, str], GuestClient] = GuestClient,
        qemu_img: str = "qemu-img",
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._vms = vms
        self._runs = runs
        self._building = building
        self._client_factory = client_factory
        self._qemu_img = qemu_img
        self._status: dict[int, UpdateStatus] = {}
        self._queue: list[int] = []
        self._worker: asyncio.Task[None] | None = None

    # --- what the UI asks -------------------------------------------------------------

    def current_image(self) -> Path:
        return self._settings.base_image_path(DEFAULT_IMAGE)

    def is_outdated(self, agent: Agent) -> bool:
        """True if the agent's disk is a layer over an image other than the current one."""
        disk = Path(agent.vm_disk_path)
        if not disk.exists():
            return False  # made on the current image when it first starts
        current = self.current_image()
        if not current.exists():
            return False
        base = generations.backing_file(disk, self._qemu_img)
        return base is not None and base.resolve() != current.resolve()

    def status(self, agent_id: int) -> UpdateStatus:
        return self._status.get(agent_id, UpdateStatus())

    def is_updating(self, agent_id: int) -> bool:
        return self.status(agent_id).state in ("queued", "running")

    def any_updating(self) -> bool:
        return any(self.is_updating(agent_id) for agent_id in self._status)

    def request(self, agent_ids: list[int]) -> None:
        """Queue updates; they run one after another (each boots two computers)."""
        if self._building():
            raise UpdateError("The system image is being built. Update when it is finished.")
        for agent_id in agent_ids:
            if not self.is_updating(agent_id):
                self._status[agent_id] = UpdateStatus(state="queued", detail="Waiting for its turn…")
                self._queue.append(agent_id)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    async def wait(self) -> None:
        if self._worker is not None:
            await self._worker

    async def recover_interrupted(self) -> list[str]:
        """Undo updates the app quit or crashed in the middle of (call once, at startup).

        An old disk still set aside means its agent's files may not have reached
        the new computer. The old disk is the complete state from before the
        update, so it is always safe to put it back; the update can be run again.
        """
        recovered = []
        with Session(self._engine) as session:
            agents = list(session.exec(select(Agent)).all())
        for agent in agents:
            disk = Path(agent.vm_disk_path)
            previous = disk.with_name(PREVIOUS_DISK)
            for leftover in self._settings.agent_dir(agent.name).glob(f"{ARCHIVE}*"):
                leftover.unlink(missing_ok=True)
            if not previous.exists():
                continue
            if await self._vms.status(agent) == VMStatus.RUNNING:
                await self._vms.stop(agent)
            disk.unlink(missing_ok=True)
            os.replace(previous, disk)
            log.warning("interrupted computer update undone", extra={"agent": agent.name})
            if agent.id is not None:
                self._status[agent.id] = UpdateStatus(
                    state="failed",
                    detail="The app quit during the update. The computer was put back as it was; update it again.",
                )
            recovered.append(agent.name)
        return recovered

    # --- the update ---------------------------------------------------------------------

    async def _work(self) -> None:
        while self._queue:
            agent_id = self._queue.pop(0)
            self._status[agent_id] = UpdateStatus(state="running", detail="Starting…")
            try:
                await self._update(agent_id)
            except Exception as exc:  # noqa: BLE001 - the status must say what happened
                if not isinstance(exc, (UpdateError, VMError, GuestError)):
                    log.exception("computer update crashed", extra={"agent_id": agent_id})
                self._status[agent_id] = UpdateStatus(state="failed", detail=str(exc)[:500])
            finally:
                self._runs.release(agent_id)
        if not self._building():
            await asyncio.to_thread(generations.remove_unused, self._settings, DEFAULT_IMAGE, self._qemu_img)

    def _step(self, agent_id: int, detail: str) -> None:
        self._status[agent_id] = UpdateStatus(state="running", detail=detail)

    def _agent(self, session: Session, agent_id: int) -> Agent:
        agent = session.get(Agent, agent_id)
        if agent is None:
            raise UpdateError("The agent no longer exists.")
        return agent

    async def _update(self, agent_id: int) -> None:
        with Session(self._engine) as session:
            manager = AgentManager(session, self._settings)
            agent = self._agent(session, agent_id)
            agent_dir = self._settings.agent_dir(agent.name)
            disk = Path(agent.vm_disk_path)
            previous = disk.with_name(PREVIOUS_DISK)
            archive = agent_dir / ARCHIVE
            if previous.exists():
                raise UpdateError(
                    f"An earlier update of {agent.name} did not finish: {previous} is still there. "
                    "Nothing was changed."
                )
            if not await asyncio.to_thread(self.is_outdated, agent):
                self._status[agent_id] = UpdateStatus(state="done", detail="Already on the current system image.")
                return

            was_running = await self._vms.status(agent) == VMStatus.RUNNING
            self._runs.hold(agent_id, HELD)
            await self._runs.cancel(agent_id)

            # 1-2: the old computer, running current guest software, packs the agent's files.
            try:
                self._step(agent_id, "Restarting the computer to pack the agent's files…")
                await self._boot(manager, agent, restart=was_running)
                client = self._client(agent)
                self._step(agent_id, "Packing the Shared folder, databases and memory…")
                packed = await client.call("carry.pack", timeout=1800)
                size, digest = self._checked_pack(packed)
                self._check_space(size)
                self._step(agent_id, f"Copying {size / 1024**2:.0f} MB of the agent's files to the Mac…")
                await self._fetch(client, archive, size, digest)
            except BaseException:
                if not was_running:
                    await self._vms.stop(agent)
                manager.set_vm_status(agent, await self._vms.status(agent))
                raise

            # 3-4: old disk aside, a fresh one on the current image.
            self._step(agent_id, "Starting the computer on the new system image…")
            await self._vms.stop(agent)
            os.replace(disk, previous)
            try:
                await self._boot(manager, agent, restart=False)
                client = self._client(agent)
                # 5: the files go onto the new computer.
                self._step(agent_id, "Putting the agent's files back…")
                await self._send(client, archive)
                await client.call("carry.unpack", {"sha256": digest}, timeout=1800)
            except BaseException:
                await self._roll_back(agent, disk, previous)
                archive.unlink(missing_ok=True)
                if was_running:
                    try:
                        await self._boot(manager, agent, restart=False)
                    except Exception:  # noqa: BLE001 - the update's own error is the one to report
                        log.exception("could not restart the old computer", extra={"agent": agent.name})
                manager.set_vm_status(agent, await self._vms.status(agent))
                raise

            # 6: done; the old disk and the archive can go.
            previous.unlink(missing_ok=True)
            archive.unlink(missing_ok=True)
            if not was_running:
                await self._vms.stop(agent)
            manager.set_vm_status(agent, await self._vms.status(agent))
            log.info("computer updated", extra={"agent": agent.name, "carried_bytes": size})
            what = "Shared folder, databases and memory"
            self._status[agent_id] = UpdateStatus(state="done", detail=f"Updated. Its {what} came along.")

    async def _boot(self, manager: AgentManager, agent: Agent, restart: bool) -> None:
        if restart:
            await self._vms.stop(agent)
        manager.assign_vm_endpoints(agent)
        await self._vms.start(agent)
        manager.set_vm_status(agent, await self._vms.status(agent))
        try:
            await self._client(agent).wait_ready(timeout=READY_TIMEOUT)
        except GuestError as exc:
            raise UpdateError(f"{agent.name}'s computer did not come up in time: {exc}") from exc

    def _client(self, agent: Agent) -> GuestClient:
        assert agent.vm_daemon_port is not None
        return self._client_factory(agent.vm_daemon_port, self._vms.guest_secret(agent))

    @staticmethod
    def _checked_pack(packed: dict[str, Any]) -> tuple[int, str]:
        # The agent controls its computer: its answer is checked, not trusted.
        size, digest = packed.get("size"), packed.get("sha256")
        if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise UpdateError("The agent's computer reported an archive of an impossible size.")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise UpdateError("The agent's computer sent an unexpected reply.")
        return size, digest

    def _check_space(self, size: int) -> None:
        free = shutil.disk_usage(self._settings.home).free
        if free < size + 2 * 1024**3:
            raise UpdateError(
                f"Not enough space on the Mac to carry {size / 1024**3:.1f} GB of the agent's files "
                f"({free / 1024**3:.1f} GB free). Nothing was changed."
            )

    async def _fetch(self, client: GuestClient, archive: Path, size: int, digest: str) -> None:
        partial = archive.with_suffix(".partial")
        hasher = hashlib.sha256()
        offset = 0
        try:
            with partial.open("wb") as handle:
                while True:
                    piece = await client.call("carry.read_chunk", {"offset": offset}, timeout=60)
                    data = base64.b64decode(piece["data"], validate=True)
                    if len(data) > CHUNK_BYTES or offset + len(data) > size:
                        raise UpdateError("The agent's computer sent more than it announced.")
                    handle.write(data)
                    hasher.update(data)
                    offset += len(data)
                    if piece.get("eof") is True or not data:
                        break
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError("The agent's computer sent an unexpected reply.") from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        if offset != size or hasher.hexdigest() != digest:
            partial.unlink(missing_ok=True)
            raise UpdateError("The agent's files changed or were damaged while being copied. Nothing was changed.")
        os.replace(partial, archive)
        await client.call("carry.clear", timeout=30)

    async def _send(self, client: GuestClient, archive: Path) -> None:
        with archive.open("rb") as handle:
            offset = 0
            while True:
                data = handle.read(CHUNK_BYTES)
                if not data and offset:
                    break
                await client.call(
                    "carry.write_chunk", {"offset": offset, "data": base64.b64encode(data).decode()}, timeout=60
                )
                offset += len(data)
                if not data:
                    break

    async def _roll_back(self, agent: Agent, disk: Path, previous: Path) -> None:
        """Put the old disk back; the agent is as it was before the update."""
        try:
            await self._vms.stop(agent)
        except Exception:  # noqa: BLE001 - restoring the disk matters more
            log.exception("could not stop the new computer during roll-back", extra={"agent": agent.name})
        disk.unlink(missing_ok=True)
        os.replace(previous, disk)
        log.warning("computer update rolled back", extra={"agent": agent.name})
