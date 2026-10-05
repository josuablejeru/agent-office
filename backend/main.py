"""FastAPI application factory.

Run with:  uvicorn --factory backend.main:create_app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.agents.channels import ChannelService
from backend.agents.manager import AgentManager
from backend.agents.runtime import RunService
from backend.api import agents as agents_api
from backend.api import channels as channels_api
from backend.api import chat as chat_api
from backend.api import desktop as desktop_api
from backend.api import files as files_api
from backend.api import memory as memory_api
from backend.api import vm as vm_api
from backend.api.deps import require_api_token
from backend.config import ProviderSettings, Settings
from backend.db.database import create_db_engine, init_db
from backend.instance import acquire_instance_lock
from backend.keystore import KeyStore
from backend.logging_config import configure_logging, get_logger
from backend.net.dns import DnsForwarder
from backend.policy.approvals import ApprovalBroker
from backend.policy.setup import build_policy_engine
from backend.providers.base import ModelProvider
from backend.providers.registry import build_provider
from backend.vm.base_image import BaseImageBuilder
from backend.vm.lifecycle import QemuVMManager, VMStatus

log = get_logger("app")

LOCAL_HOSTS = ["127.0.0.1", "localhost"]


async def recover_vm_states(app: FastAPI) -> None:
    """Reconcile cached VM states with the QEMU processes that outlived the last run."""
    with Session(app.state.engine) as session:
        manager = AgentManager(session, app.state.settings)
        for agent in manager.list():
            vm_status = await app.state.vm_manager.status(agent)
            if vm_status == VMStatus.RUNNING:
                log.info("vm recovered", extra={"agent": agent.name})
            manager.set_vm_status(agent, vm_status)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Held until the process exits: a second backend on this directory must not start.
    app.state.instance_lock = acquire_instance_lock(app.state.settings)
    if app.state.dns is not None:
        await app.state.dns.start()
        app.state.vm_manager.dns_settings = app.state.dns.guest_settings
    await recover_vm_states(app)
    app.state.run_service.mark_interrupted()
    app.state.channels.ensure_default()
    yield
    if app.state.dns is not None:
        app.state.dns.stop()
    app.state.instance_lock.close()
    # VMs are deliberately left running here: they are persistent computers and
    # the backend finds them again on its next start. The desktop app powers
    # them off separately when it quits (backend/shutdown.py).


def create_app(
    settings: Settings | None = None,
    key_store: KeyStore | None = None,
    ui_dir: Path | None = None,
) -> FastAPI:
    """Build the API. With `ui_dir`, the built frontend is served from the same origin."""
    settings = settings or Settings.from_env()
    key_store = key_store or KeyStore()
    configure_logging()
    settings.ensure_layout()

    engine = create_db_engine(settings.db_path)
    init_db(engine)

    app = FastAPI(title="Agent Office", version="0.1.0", lifespan=lifespan)
    # Only answer requests addressed to this machine: a web page that points its
    # own hostname at 127.0.0.1 (DNS rebinding) is turned away.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)
    app.state.settings = settings
    app.state.engine = engine
    app.state.api_token = settings.load_api_token()
    app.state.vm_manager = QemuVMManager(settings)
    network = settings.load_config().get("network") or {}
    app.state.dns = (
        DnsForwarder(int(network.get("dns_port", 47653)), network.get("dns_rules") or {})
        if network.get("split_dns", True)
        else None
    )
    app.state.key_store = key_store
    app.state.base_image_builder = BaseImageBuilder(settings)
    app.state.approvals = ApprovalBroker()
    app.state.channels = ChannelService(engine)

    def provider_factory(name: str, config: ProviderSettings, model: str) -> ModelProvider:
        return build_provider(name, config, model, key_store.get)

    app.state.run_service = RunService(
        engine,
        settings,
        app.state.vm_manager,
        build_policy_engine(settings),
        app.state.approvals,
        provider_factory=provider_factory,
        channels=app.state.channels,
    )

    protected = [Depends(require_api_token)]
    app.include_router(agents_api.router, dependencies=protected)
    app.include_router(vm_api.router, dependencies=protected)
    app.include_router(chat_api.router, dependencies=protected)
    app.include_router(channels_api.router, dependencies=protected)
    app.include_router(files_api.router, dependencies=protected)
    app.include_router(memory_api.router, dependencies=protected)
    # Checks the token itself: its WebSocket route cannot use header auth.
    app.include_router(desktop_api.router)

    if ui_dir is not None:
        # Mounted last so it only receives paths no API route claimed.
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")

    log.info("backend ready", extra={"home": str(settings.home)})
    return app
