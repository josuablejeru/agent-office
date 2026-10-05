"""Powers off every running agent VM:  python -m backend.shutdown

The app launcher runs this after the app process ends, however it ended
(window closed, Cmd-Q, crash), so no VM keeps running unseen in the background.
VM disks are untouched; agents continue where they left off on the next start.
"""

from __future__ import annotations

import asyncio
import sys

from sqlmodel import Session

from backend.agents.manager import AgentManager
from backend.config import Settings
from backend.db.database import create_db_engine, init_db
from backend.instance import is_running
from backend.logging_config import configure_logging, get_logger
from backend.vm.lifecycle import QemuVMManager, VMManager, VMStatus

log = get_logger("app")


async def stop_all_vms(settings: Settings, vm_manager: VMManager | None = None) -> int:
    """Stop all running VMs and return how many were stopped."""
    vm_manager = vm_manager or QemuVMManager(settings)
    engine = create_db_engine(settings.db_path)
    init_db(engine)
    stopped = 0
    with Session(engine) as session:
        manager = AgentManager(session, settings)
        for agent in manager.list():
            if await vm_manager.status(agent) == VMStatus.RUNNING:
                await vm_manager.stop(agent)
                stopped += 1
            manager.set_vm_status(agent, await vm_manager.status(agent))
    return stopped


def main() -> int:
    configure_logging()
    settings = Settings.from_env()
    if not settings.db_path.exists():
        return 0
    if is_running(settings):
        # Another backend (for example a dev server) is using these VMs.
        log.info("vms left running: another backend is active")
        return 0
    if (settings.load_config().get("app") or {}).get("keep_vms_running_on_quit"):
        log.info("vms left running: keep_vms_running_on_quit is set")
        return 0
    stopped = asyncio.run(stop_all_vms(settings))
    log.info("vms stopped at exit", extra={"count": stopped})
    return 0


if __name__ == "__main__":
    sys.exit(main())
