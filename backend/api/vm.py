"""Host capability, provider listing and VM lifecycle endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.manager import AgentManager, AgentNotFound
from backend.agents.models import AgentRead
from backend.api.deps import (
    AgentManagerDep,
    BaseImageBuilderDep,
    KeyStoreDep,
    RunServiceDep,
    SettingsDep,
    VMManagerDep,
)
from backend.db.models import Agent
from backend.keystore import KeyStoreError
from backend.logging_config import get_logger
from backend.providers.base import ProviderError
from backend.providers.check import ModelCheck, check_model
from backend.providers.registry import build_provider
from backend.providers.vertex import LOGIN_HINT, credentials_available
from backend.vm.base_image import BaseImageStatus
from backend.vm.capabilities import Capabilities, detect_capabilities
from backend.vm.errors import VMError
from backend.vm.guest import GuestClient
from backend.vm.lifecycle import VMManager, VMStatus

router = APIRouter(prefix="/api", tags=["system"])
log = get_logger("providers")

VERTEX_TYPES = ("vertex", "vertex-anthropic")


class ProviderInfo(BaseModel):
    """A configured provider, without any credential material."""

    name: str
    type: str
    base_url: str | None
    # Name under which the provider's key is stored, if it needs one.
    key_name: str | None
    has_key: bool
    # Vertex AI providers use a Google Cloud project and local credentials instead.
    uses_google_cloud: bool
    project: str | None
    region: str | None
    credentials_found: bool


class ProviderLocation(BaseModel):
    project: str = Field(max_length=100, pattern=r"^[a-z0-9][a-z0-9:.-]*$|^$")
    region: str = Field(default="global", max_length=40, pattern=r"^[a-z0-9-]+$")


class ProviderCreate(BaseModel):
    """A model server that speaks the OpenAI chat API, on this Mac or another machine."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    base_url: str = Field(pattern=r"^https?://[^\s]+$", max_length=300)
    needs_key: bool = False


class ModelTestRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class AppSettings(BaseModel):
    keep_vms_running_on_quit: bool


class ModelStatus(BaseModel):
    ok: bool
    detail: str = ""


class ProviderKey(BaseModel):
    key: str = Field(min_length=1, max_length=500)


class SnapshotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=63)


@router.get("/system/capabilities")
async def get_capabilities() -> Capabilities:
    return await detect_capabilities()


@router.get("/providers")
async def list_providers(settings: SettingsDep, keys: KeyStoreDep) -> list[ProviderInfo]:
    return [
        ProviderInfo(
            name=name,
            type=provider.type,
            base_url=provider.base_url,
            key_name=provider.api_key_env,
            has_key=bool(provider.api_key_env) and keys.has(provider.api_key_env or ""),
            uses_google_cloud=provider.type in VERTEX_TYPES,
            project=provider.project,
            region=provider.region,
            credentials_found=provider.type in VERTEX_TYPES and credentials_available(),
        )
        for name, provider in settings.load_providers().items()
    ]


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def add_provider(payload: ProviderCreate, settings: SettingsDep) -> dict[str, str]:
    """Register an OpenAI-compatible model server (Ollama, LM Studio, vLLM, a gateway...)."""
    if payload.name in settings.load_providers():
        raise HTTPException(status.HTTP_409_CONFLICT, f"A provider named '{payload.name}' already exists.")
    entry: dict[str, str] = {"type": "openai-compatible", "base_url": payload.base_url.rstrip("/")}
    if payload.needs_key:
        entry["api_key_env"] = payload.name.upper().replace("-", "_") + "_API_KEY"
    else:
        entry["api_key"] = "none"
    settings.update_user_config(["providers", payload.name], entry)
    return {"name": payload.name}


@router.put("/providers/{name}/key", status_code=status.HTTP_204_NO_CONTENT)
async def set_provider_key(
    name: str, payload: ProviderKey, settings: SettingsDep, keys: KeyStoreDep
) -> None:
    """Store a provider's API key in the macOS Keychain."""
    provider = settings.load_providers().get(name)
    if provider is None or not provider.api_key_env:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"provider '{name}' takes no API key")
    try:
        keys.set(provider.api_key_env, payload.key.strip())
    except KeyStoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/providers/{name}/test")
async def test_provider(
    name: str, payload: ModelTestRequest, settings: SettingsDep, keys: KeyStoreDep
) -> ModelCheck:
    """Send one small request through the same path agents use and report what happened."""
    provider = settings.load_providers().get(name)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"provider '{name}' not found")
    try:
        instance = build_provider(name, provider, payload.model.strip(), keys.get)
    except ProviderError as exc:
        return ModelCheck(ok=False, detail=str(exc))
    result = await check_model(instance)
    log.info(
        "provider tested",
        extra={"provider": name, "model": payload.model, "ok": result.ok, "tools": result.tools},
    )
    return result


@router.put("/providers/{name}/location", status_code=status.HTTP_204_NO_CONTENT)
async def set_provider_location(name: str, payload: ProviderLocation, settings: SettingsDep) -> None:
    """Set the Google Cloud project and region of a Vertex AI provider."""
    provider = settings.load_providers().get(name)
    if provider is None or provider.type not in VERTEX_TYPES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"provider '{name}' is not a Vertex AI provider")
    settings.update_user_config(["providers", name, "type"], provider.type)
    settings.update_user_config(["providers", name, "project"], payload.project)
    settings.update_user_config(["providers", name, "region"], payload.region)


@router.get("/settings")
async def get_app_settings(settings: SettingsDep) -> AppSettings:
    return AppSettings.model_validate(settings.load_config().get("app") or {})


@router.put("/settings")
async def set_app_settings(payload: AppSettings, settings: SettingsDep) -> AppSettings:
    settings.update_user_config(["app", "keep_vms_running_on_quit"], payload.keep_vms_running_on_quit)
    return payload


@router.get("/agents/{agent_id}/model-status", tags=["agents"])
async def model_status(
    agent_id: int, manager: AgentManagerDep, settings: SettingsDep, keys: KeyStoreDep
) -> ModelStatus:
    """Whether the agent's model can be reached, checked before the user sends anything."""
    agent = _load(agent_id, manager)
    provider = settings.load_providers().get(agent.provider)
    if provider is None:
        return ModelStatus(ok=False, detail=f"Provider '{agent.provider}' is not configured.")
    if provider.type in VERTEX_TYPES:
        if not provider.project:
            return ModelStatus(ok=False, detail="Set a Google Cloud project for this provider in Settings.")
        if not credentials_available():
            return ModelStatus(ok=False, detail=f"No Google Cloud credentials found. {LOGIN_HINT}")
        return ModelStatus(ok=True)
    try:
        instance = build_provider(agent.provider, provider, agent.model, keys.get)
        models = await asyncio.wait_for(instance.list_models(), timeout=6)
    except (ProviderError, TimeoutError) as exc:
        detail = str(exc) or f"The model server for '{agent.provider}' did not answer."
        return ModelStatus(ok=False, detail=detail)
    if models and agent.model not in models and f"{agent.model}:latest" not in models:
        return ModelStatus(
            ok=False,
            detail=f"The model '{agent.model}' is not offered by {agent.provider}. "
            f"Available: {', '.join(models[:8])}{'…' if len(models) > 8 else ''}",
        )
    return ModelStatus(ok=True)


@router.get("/system/base-image")
async def base_image_status(builder: BaseImageBuilderDep) -> BaseImageStatus:
    return builder.status()


@router.post("/system/base-image")
async def build_base_image(builder: BaseImageBuilderDep) -> BaseImageStatus:
    """Start building the base image all agent VMs boot from, if it is missing."""
    return builder.start()


@router.get("/providers/{name}/models")
async def list_provider_models(name: str, settings: SettingsDep, keys: KeyStoreDep) -> list[str]:
    """Models the provider offers, for populating the model picker."""
    providers = settings.load_providers()
    if name not in providers:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"provider '{name}' not found")
    try:
        return await build_provider(name, providers[name], "", keys.get).list_models()
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


class GuestStatus(BaseModel):
    ready: bool
    # Why a running VM is not usable (for example paused on a full disk), if known.
    problem: str | None = None


def _load(agent_id: int, manager: AgentManager) -> Agent:
    try:
        return manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


async def _run(
    agent: Agent,
    manager: AgentManager,
    vms: VMManager,
    action: Callable[[Agent], Awaitable[None]],
) -> AgentRead:
    """Run a lifecycle action, then return the agent with its refreshed VM state."""
    try:
        await action(agent)
    except VMError as exc:
        manager.set_vm_status(agent, await vms.status(agent))
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    manager.set_vm_status(agent, await vms.status(agent))
    return AgentRead.model_validate(agent)


@router.post("/agents/{agent_id}/vm/start", tags=["vm"])
async def start_vm(agent_id: int, manager: AgentManagerDep, vms: VMManagerDep) -> AgentRead:
    agent = _load(agent_id, manager)
    if await vms.status(agent) != VMStatus.RUNNING:
        try:
            manager.assign_vm_endpoints(agent)
        except VMError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _run(agent, manager, vms, vms.start)


@router.post("/agents/{agent_id}/vm/stop", tags=["vm"])
async def stop_vm(
    agent_id: int, manager: AgentManagerDep, vms: VMManagerDep, runs: RunServiceDep
) -> AgentRead:
    # A run left going would keep failing against a computer that is gone.
    await runs.cancel(agent_id)
    return await _run(_load(agent_id, manager), manager, vms, vms.stop)


@router.post("/agents/{agent_id}/vm/restart", tags=["vm"])
async def restart_vm(
    agent_id: int, manager: AgentManagerDep, vms: VMManagerDep, runs: RunServiceDep
) -> AgentRead:
    agent = _load(agent_id, manager)
    await runs.cancel(agent_id)
    if agent.vm_daemon_port is None:
        try:
            manager.assign_vm_endpoints(agent)
        except VMError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await _run(agent, manager, vms, vms.restart)


@router.post("/agents/{agent_id}/vm/snapshot", tags=["vm"])
async def snapshot_vm(
    agent_id: int, payload: SnapshotRequest, manager: AgentManagerDep, vms: VMManagerDep
) -> AgentRead:
    return await _run(
        _load(agent_id, manager), manager, vms, lambda agent: vms.snapshot(agent, payload.name)
    )


@router.get("/agents/{agent_id}/vm/guest", tags=["vm"])
async def guest_status(agent_id: int, manager: AgentManagerDep, vms: VMManagerDep) -> GuestStatus:
    """Whether the daemon inside the agent's VM is answering."""
    agent = _load(agent_id, manager)
    if agent.vm_daemon_port is None or await vms.status(agent) != VMStatus.RUNNING:
        return GuestStatus(ready=False)
    client = GuestClient(agent.vm_daemon_port, vms.guest_secret(agent))
    ready = await client.is_ready()
    return GuestStatus(ready=ready, problem=None if ready else await vms.problem(agent))
