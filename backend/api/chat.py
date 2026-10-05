"""Chat endpoints: send a message to an agent, read its history and follow a run."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from backend.agents.manager import AgentNotFound
from backend.agents.runtime import RunError
from backend.api.deps import (
    AgentManagerDep,
    ApprovalBrokerDep,
    RunServiceDep,
    SessionDep,
    SettingsDep,
    VMManagerDep,
)
from backend.db.models import Approval, Message, Run, ToolCall
from backend.vm.lifecycle import VMStatus

router = APIRouter(prefix="/api", tags=["chat"])

SCREENSHOT_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png)$")


class ChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class ChatStarted(BaseModel):
    run_id: int


class MessageRead(BaseModel):
    id: int
    run_id: int | None
    role: str
    content: str
    created_at: datetime


class ToolCallRead(BaseModel):
    id: int
    tool: str
    arguments: dict[str, Any]
    decision: str | None
    result: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None


class ApprovalRead(BaseModel):
    id: int
    tool_call_id: int
    tool: str
    arguments: dict[str, Any]
    risk: str | None
    reason: str | None


class ApprovalAnswer(BaseModel):
    approved: bool


class RunRead(BaseModel):
    id: int
    agent_id: int
    status: str
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    tool_calls: list[ToolCallRead]
    # Set while the run is paused waiting for the user's decision.
    pending_approval: ApprovalRead | None


@router.get("/agents/{agent_id}/messages")
def list_messages(agent_id: int, session: SessionDep) -> list[MessageRead]:
    rows = session.exec(
        select(Message).where(Message.agent_id == agent_id).order_by(Message.id)  # type: ignore[arg-type]
    ).all()
    return [MessageRead.model_validate(row, from_attributes=True) for row in rows]


@router.post("/agents/{agent_id}/chat", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    agent_id: int,
    payload: ChatRequest,
    manager: AgentManagerDep,
    vms: VMManagerDep,
    runs: RunServiceDep,
) -> ChatStarted:
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if await vms.status(agent) != VMStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Start the agent's VM before chatting.")
    try:
        return ChatStarted(run_id=runs.start(agent_id, payload.content))
    except RunError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/runs/{run_id}")
def get_run(run_id: int, session: SessionDep) -> RunRead:
    run = session.get(Run, run_id)
    if run is None or run.id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")
    calls = session.exec(
        select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)  # type: ignore[arg-type]
    ).all()
    pending = None
    for call in calls:
        approval = session.exec(
            select(Approval).where(Approval.tool_call_id == call.id, Approval.status == "pending")
        ).first()
        if approval is not None and approval.id is not None:
            pending = ApprovalRead(
                id=approval.id,
                tool_call_id=approval.tool_call_id,
                tool=call.tool,
                arguments=json.loads(call.arguments_json),
                risk=approval.risk,
                reason=approval.reason,
            )
    return RunRead(
        id=run.id,
        agent_id=run.agent_id,
        status=run.status,
        error=run.error,
        started_at=run.started_at,
        finished_at=run.finished_at,
        pending_approval=pending,
        tool_calls=[
            ToolCallRead(
                id=call.id or 0,
                tool=call.tool,
                arguments=json.loads(call.arguments_json),
                decision=call.decision,
                result=json.loads(call.result_json) if call.result_json else None,
                started_at=call.started_at,
                finished_at=call.finished_at,
            )
            for call in calls
        ],
    )


@router.post("/agents/{agent_id}/chat/cancel")
async def cancel_run(agent_id: int, runs: RunServiceDep) -> dict[str, bool]:
    """Stop the agent's current run."""
    return {"cancelled": await runs.cancel(agent_id)}


@router.post("/approvals/{approval_id}")
async def answer_approval(
    approval_id: int, payload: ApprovalAnswer, session: SessionDep, approvals: ApprovalBrokerDep
) -> dict[str, bool]:
    """Allow once or reject the action a paused run is waiting on.

    Deliberately `async`: it wakes a coroutine, which must happen on the event
    loop and not on a worker thread.
    """
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"approval {approval_id} not found")
    if approval.status != "pending" or not approvals.resolve(approval_id, payload.approved):
        raise HTTPException(status.HTTP_409_CONFLICT, "This approval is no longer pending.")
    return {"approved": payload.approved}


@router.get("/agents/{agent_id}/screenshots/{filename}")
def get_screenshot(
    agent_id: int, filename: str, manager: AgentManagerDep, settings: SettingsDep
) -> FileResponse:
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    path = settings.screenshots_dir(agent.name) / filename
    if not SCREENSHOT_NAME.match(filename) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "screenshot not found")
    return FileResponse(path)


@router.delete("/agents/{agent_id}/messages", status_code=status.HTTP_204_NO_CONTENT)
async def clear_conversation(agent_id: int, manager: AgentManagerDep, runs: RunServiceDep) -> None:
    """Forget the conversation. The agent's computer and its files are not touched."""
    try:
        manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await runs.cancel(agent_id)
    manager.clear_history(agent_id)
