"""FastAPI dependencies. Shared objects live on `app.state`, not in module globals."""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from backend.agents.channels import ChannelService
from backend.agents.manager import AgentManager
from backend.agents.runtime import RunService
from backend.config import Settings
from backend.keystore import KeyStore
from backend.policy.approvals import ApprovalBroker
from backend.vm.base_image import BaseImageBuilder
from backend.vm.lifecycle import VMManager


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def get_vm_manager(request: Request) -> VMManager:
    return request.app.state.vm_manager


def get_run_service(request: Request) -> RunService:
    return request.app.state.run_service


def get_approval_broker(request: Request) -> ApprovalBroker:
    return request.app.state.approvals


def get_channel_service(request: Request) -> ChannelService:
    return request.app.state.channels


def get_key_store(request: Request) -> KeyStore:
    return request.app.state.key_store


def get_base_image_builder(request: Request) -> BaseImageBuilder:
    return request.app.state.base_image_builder


def require_api_token(request: Request) -> None:
    """Reject requests that do not carry the local API token."""
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {request.app.state.api_token}"
    if not hmac.compare_digest(header.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API token.")


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
VMManagerDep = Annotated[VMManager, Depends(get_vm_manager)]
RunServiceDep = Annotated[RunService, Depends(get_run_service)]
ChannelServiceDep = Annotated[ChannelService, Depends(get_channel_service)]
KeyStoreDep = Annotated[KeyStore, Depends(get_key_store)]
BaseImageBuilderDep = Annotated[BaseImageBuilder, Depends(get_base_image_builder)]
ApprovalBrokerDep = Annotated[ApprovalBroker, Depends(get_approval_broker)]


def get_agent_manager(session: SessionDep, settings: SettingsDep) -> AgentManager:
    return AgentManager(session, settings)


AgentManagerDep = Annotated[AgentManager, Depends(get_agent_manager)]
