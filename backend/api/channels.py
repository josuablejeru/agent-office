"""Shared channel endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from backend.agents.channels import HUMAN_AUTHOR, MAX_MESSAGE_CHARS, ChannelError
from backend.api.deps import ChannelServiceDep, RunServiceDep

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelRead(BaseModel):
    id: int
    name: str


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class ChannelMessageRead(BaseModel):
    id: int
    author: str
    content: str
    created_at: datetime


class ChannelPost(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


@router.get("")
def list_channels(channels: ChannelServiceDep) -> list[ChannelRead]:
    return [ChannelRead.model_validate(c, from_attributes=True) for c in channels.list()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_channel(payload: ChannelCreate, channels: ChannelServiceDep) -> ChannelRead:
    try:
        channel = channels.create(payload.name.strip().lstrip("#").lower())
    except ChannelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return ChannelRead.model_validate(channel, from_attributes=True)


@router.get("/{channel_id}/messages")
def list_messages(channel_id: int, channels: ChannelServiceDep, after: int = 0) -> list[ChannelMessageRead]:
    if channels.get(channel_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
    return [
        ChannelMessageRead.model_validate(m, from_attributes=True)
        for m in channels.messages(channel_id, after_id=after)
    ]


@router.post("/{channel_id}/messages", status_code=status.HTTP_201_CREATED)
async def post_message(
    channel_id: int, payload: ChannelPost, channels: ChannelServiceDep, runs: RunServiceDep
) -> ChannelMessageRead:
    """Post as the user. Agents mentioned with @name are woken to answer."""
    if channels.get(channel_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
    try:
        message = channels.post(channel_id, HUMAN_AUTHOR, payload.content)
    except ChannelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await runs.dispatch_mentions(message)
    return ChannelMessageRead.model_validate(message, from_attributes=True)
