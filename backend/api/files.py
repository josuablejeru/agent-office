"""Files in an agent's Shared folder: list, upload, download, save to Downloads."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from backend.agents.manager import AgentNotFound
from backend.api.deps import AgentManagerDep, VMManagerDep
from backend.logging_config import get_logger
from backend.vm.guest import GuestClient
from backend.vm.lifecycle import VMStatus
from backend.vm.transfer import MAX_FILE_BYTES, TransferError, download, list_files, upload

log = get_logger("files")

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["files"])

# Uploaded names: one path component, nothing hidden, no control characters.
SAFE_NAME = re.compile(r"^[^/\\\x00-\x1f.][^/\\\x00-\x1f]{0,199}$")
DOWNLOADS_DIR = Path.home() / "Downloads"


class SharedFile(BaseModel):
    path: str
    size: int
    modified: int


class SaveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class Saved(BaseModel):
    saved_to: str


async def _client(agent_id: int, manager: AgentManagerDep, vms: VMManagerDep) -> GuestClient:
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if agent.vm_daemon_port is None or await vms.status(agent) != VMStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Turn on this agent's computer to use its files.")
    return GuestClient(agent.vm_daemon_port, vms.guest_secret(agent))


def free_path(directory: Path, name: str) -> Path:
    """A path in `directory` for `name` that does not overwrite anything."""
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


@router.get("/files")
async def get_files(agent_id: int, manager: AgentManagerDep, vms: VMManagerDep) -> list[SharedFile]:
    client = await _client(agent_id, manager, vms)
    try:
        return [SharedFile.model_validate(entry) for entry in await list_files(client)]
    except TransferError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.put("/files/{name}")
async def put_file(
    agent_id: int, name: str, request: Request, manager: AgentManagerDep, vms: VMManagerDep
) -> SharedFile:
    """Upload a file into the agent's Shared folder. The body is the raw file."""
    if not SAFE_NAME.match(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file name cannot be used.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Files can be at most 25 MB.")
    data = await request.body()
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Files can be at most 25 MB.")
    client = await _client(agent_id, manager, vms)
    try:
        result = await upload(client, name, data)
    except TransferError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # Names and sizes only: file contents never go to the log.
    log.info("file uploaded", extra={"agent": agent_id, "bytes": len(data)})
    return SharedFile(path=str(result["path"]), size=len(data), modified=0)


async def _fetch(agent_id: int, path: str, manager: AgentManagerDep, vms: VMManagerDep) -> Path:
    client = await _client(agent_id, manager, vms)
    handle, temporary = tempfile.mkstemp(prefix="agent-office-")
    os.close(handle)
    try:
        await download(client, path, Path(temporary))
    except TransferError as exc:
        Path(temporary).unlink(missing_ok=True)
        code = status.HTTP_404_NOT_FOUND if "no such file" in str(exc) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc
    return Path(temporary)


@router.get("/files/{path:path}")
async def get_file(
    agent_id: int, path: str, manager: AgentManagerDep, vms: VMManagerDep
) -> FileResponse:
    """Download a file from the Shared folder. It is fetched and verified before sending."""
    temporary = await _fetch(agent_id, path, manager, vms)
    name = Path(path).name
    return FileResponse(
        temporary,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
        background=BackgroundTask(temporary.unlink, missing_ok=True),
    )


@router.post("/saved-files")
async def save_file(
    agent_id: int, payload: SaveRequest, manager: AgentManagerDep, vms: VMManagerDep
) -> Saved:
    """Copy a file from the Shared folder into this Mac's Downloads folder."""
    temporary = await _fetch(agent_id, payload.path, manager, vms)
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    destination = free_path(DOWNLOADS_DIR, Path(payload.path).name)
    shutil.move(str(temporary), destination)
    destination.chmod(0o644)  # temp files are private; a download should be an ordinary file
    log.info("file saved to downloads", extra={"agent": agent_id})
    return Saved(saved_to=str(destination))
