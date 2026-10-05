"""VM desktop access: a WebSocket-to-VNC bridge for the in-browser viewer, and takeover."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel
from sqlmodel import Session

from backend.api.deps import RunServiceDep, SessionDep, require_api_token
from backend.db.models import Agent
from backend.logging_config import get_logger

log = get_logger("desktop")

router = APIRouter(prefix="/api/agents/{agent_id}", tags=["desktop"])

VNC_SUBPROTOCOL = "binary"
# The viewer sends the API token as a second subprotocol, "token.<value>".
TOKEN_SUBPROTOCOL_PREFIX = "token."
POLICY_VIOLATION = 1008


class ControlState(BaseModel):
    # True while the user drives the desktop and the agent's tool calls are held back.
    manual: bool


def _require_agent(agent_id: int, session: Session) -> Agent:
    agent = session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"agent {agent_id} not found")
    return agent


# These two are `async` on purpose: they touch an asyncio.Event that a run is
# waiting on, which is only safe from the event loop.
@router.get("/control", dependencies=[Depends(require_api_token)])
async def get_control(agent_id: int, session: SessionDep, runs: RunServiceDep) -> ControlState:
    _require_agent(agent_id, session)
    return ControlState(manual=runs.is_manual(agent_id))


@router.put("/control", dependencies=[Depends(require_api_token)])
async def set_control(
    agent_id: int, payload: ControlState, session: SessionDep, runs: RunServiceDep
) -> ControlState:
    agent = _require_agent(agent_id, session)
    runs.set_manual(agent_id, payload.manual)
    log.info("manual control changed", extra={"agent": agent.name, "manual": payload.manual})
    return ControlState(manual=payload.manual)


async def _pump_to_vnc(websocket: WebSocket, writer: asyncio.StreamWriter) -> None:
    while True:
        writer.write(await websocket.receive_bytes())
        await writer.drain()


async def _pump_to_browser(reader: asyncio.StreamReader, websocket: WebSocket) -> None:
    while data := await reader.read(65536):
        await websocket.send_bytes(data)


@router.websocket("/vnc")
async def vnc_bridge(websocket: WebSocket, agent_id: int) -> None:
    """Relay raw VNC between the browser and the VM's unix-socket VNC server.

    Browsers cannot set headers on WebSocket requests, so the API token arrives
    as a subprotocol. Unlike a query parameter, that stays out of access logs.
    """
    app = websocket.app
    offered: list[str] = websocket.scope.get("subprotocols") or []
    token = next(
        (
            entry.removeprefix(TOKEN_SUBPROTOCOL_PREFIX)
            for entry in offered
            if entry.startswith(TOKEN_SUBPROTOCOL_PREFIX)
        ),
        "",
    )
    if not token or not hmac.compare_digest(token.encode(), app.state.api_token.encode()):
        await websocket.close(code=POLICY_VIOLATION)
        return
    with Session(app.state.engine) as session:
        agent = session.get(Agent, agent_id)
    if agent is None:
        await websocket.close(code=POLICY_VIOLATION)
        return
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(app.state.vm_manager.vnc_socket(agent))
        )
    except OSError:
        await websocket.close(code=1011, reason="VM display unavailable")
        return

    await websocket.accept(subprotocol=VNC_SUBPROTOCOL if VNC_SUBPROTOCOL in offered else None)
    log.info("desktop viewer connected", extra={"agent": agent.name})
    pumps = [
        asyncio.create_task(_pump_to_vnc(websocket, writer)),
        asyncio.create_task(_pump_to_browser(reader, websocket)),
    ]
    try:
        done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not isinstance(task.exception(), (WebSocketDisconnect, OSError, type(None))):
                log.warning("desktop bridge error", extra={"error": repr(task.exception())})
    finally:
        for task in pumps:
            task.cancel()
        writer.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
        log.info("desktop viewer disconnected", extra={"agent": agent.name})
