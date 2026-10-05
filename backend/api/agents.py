"""Agent CRUD endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from backend.agents.manager import AgentAlreadyExists, AgentNotFound, UnknownProvider
from backend.agents.models import AgentCreate, AgentRead, AgentUpdate
from backend.agents.runtime import RunService
from backend.api.deps import AgentManagerDep, RunServiceDep, SettingsDep, VMManagerDep
from backend.db.models import Agent
from backend.vm.errors import VMError
from backend.vm.lifecycle import VMManager, VMStatus

router = APIRouter(prefix="/api/agents", tags=["agents"])


# Custom avatar photos: accepted formats by their leading bytes.
IMAGE_SIGNATURES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"RIFF": "webp", b"GIF8": "gif"}
MAX_AVATAR_BYTES = 3 * 1024**2
CUSTOM_AVATAR = "custom"
AVATAR_STEM = "avatar"


async def to_read(agent: Agent, vms: VMManager, runs: RunService | None = None) -> AgentRead:
    # The live VM state is authoritative; the stored column is only a cache.
    read = AgentRead.model_validate(agent)
    read.vm_status = await vms.status(agent)
    if runs is not None and agent.id is not None:
        read.activity = runs.activity(agent.id)
    return read


@router.get("")
async def list_agents(
    manager: AgentManagerDep, vms: VMManagerDep, runs: RunServiceDep
) -> list[AgentRead]:
    return [await to_read(agent, vms, runs) for agent in manager.list()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate, manager: AgentManagerDep, vms: VMManagerDep
) -> AgentRead:
    try:
        agent = manager.create(payload)
    except AgentAlreadyExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except UnknownProvider as exc:
        raise HTTPException(422, str(exc)) from exc
    return await to_read(agent, vms)


@router.get("/{agent_id}")
async def get_agent(agent_id: int, manager: AgentManagerDep, vms: VMManagerDep) -> AgentRead:
    try:
        return await to_read(manager.get(agent_id), vms)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: int, payload: AgentUpdate, manager: AgentManagerDep, vms: VMManagerDep
) -> AgentRead:
    try:
        return await to_read(manager.update(agent_id, payload), vms)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except UnknownProvider as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: int,
    manager: AgentManagerDep,
    vms: VMManagerDep,
    runs: RunServiceDep,
    confirm: bool = Query(default=False),
) -> None:
    """Deletes the agent and its VM disk. Requires `?confirm=true`."""
    if not confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Deleting an agent destroys its VM disk. Repeat with ?confirm=true.",
        )
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if await vms.status(agent) == VMStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stop the VM before deleting the agent.")
    await runs.cancel(agent_id)
    try:
        await vms.delete(agent)
    except VMError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    manager.delete(agent_id)


def _avatar_file(agent: Agent, settings: SettingsDep) -> Path | None:
    matches = sorted(settings.agent_dir(agent.name).glob(f"{AVATAR_STEM}.*"))
    return matches[0] if matches else None


@router.put("/{agent_id}/avatar")
async def upload_avatar(
    agent_id: int, request: Request, manager: AgentManagerDep, settings: SettingsDep, vms: VMManagerDep
) -> AgentRead:
    """Use a photo of your own as the agent's avatar. The body is the raw image file."""
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    image = await request.body()
    if len(image) > MAX_AVATAR_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The photo must be smaller than 3 MB.")
    extension = next((ext for magic, ext in IMAGE_SIGNATURES.items() if image.startswith(magic)), None)
    if extension is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use a PNG, JPEG, WebP or GIF image.")
    while (old := _avatar_file(agent, settings)) is not None:
        old.unlink()
    (settings.agent_dir(agent.name) / f"{AVATAR_STEM}.{extension}").write_bytes(image)
    return await to_read(manager.update(agent_id, AgentUpdate(avatar=CUSTOM_AVATAR)), vms)


@router.get("/{agent_id}/avatar")
async def get_avatar(agent_id: int, manager: AgentManagerDep, settings: SettingsDep) -> Response:
    try:
        path = _avatar_file(manager.get(agent_id), settings)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this agent has no photo")
    return FileResponse(path, headers={"Cache-Control": "no-store"})
