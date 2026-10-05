"""What an agent remembers and the databases it keeps, for the Memory panel."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.manager import AgentNotFound
from backend.api.deps import AgentManagerDep, VMManagerDep
from backend.vm.guest import GuestClient, GuestError
from backend.vm.lifecycle import VMStatus
from backend.vm.transfer import explain

router = APIRouter(prefix="/api/agents/{agent_id}/memory", tags=["memory"])


class Fact(BaseModel):
    id: int
    subject: str
    relation: str | None = None
    object: str | None = None
    note: str | None = None
    updated: int


class Table(BaseModel):
    name: str
    columns: list[str]
    rows: int


class Database(BaseModel):
    name: str
    tables: list[Table]


class Memory(BaseModel):
    facts: list[Fact]
    total_remembered: int
    databases: list[Database]


class NewFact(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=2000)
    relation: str = Field(default="", max_length=300)
    object: str = Field(default="", max_length=300)


async def _call(
    agent_id: int, manager: AgentManagerDep, vms: VMManagerDep, op: str, args: dict[str, Any]
) -> dict[str, Any]:
    try:
        agent = manager.get(agent_id)
    except AgentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if agent.vm_daemon_port is None or await vms.status(agent) != VMStatus.RUNNING:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Turn on this agent's computer to see what it remembers."
        )
    client = GuestClient(agent.vm_daemon_port, vms.guest_secret(agent))
    try:
        return await client.call(op, args)
    except GuestError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(explain(exc))) from exc


@router.get("")
async def get_memory(
    agent_id: int, manager: AgentManagerDep, vms: VMManagerDep, query: str = ""
) -> Memory:
    recalled = await _call(agent_id, manager, vms, "memory.recall", {"query": query[:500], "limit": 200})
    databases = await _call(agent_id, manager, vms, "db.list", {})
    return Memory.model_validate({**recalled, **databases})


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_fact(
    agent_id: int, payload: NewFact, manager: AgentManagerDep, vms: VMManagerDep
) -> dict[str, Any]:
    """Teach the agent something yourself."""
    return await _call(agent_id, manager, vms, "memory.remember", payload.model_dump())


@router.delete("/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_fact(
    agent_id: int, fact_id: int, manager: AgentManagerDep, vms: VMManagerDep
) -> None:
    await _call(agent_id, manager, vms, "memory.forget", {"id": fact_id})
