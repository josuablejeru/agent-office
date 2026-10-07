"""Pydantic schemas for the agent API boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.policy.permissions import Level, UnknownAction

Group = Literal["shell", "files", "browser", "search", "databases", "memory", "channels"]

# The name doubles as the agent's directory name, so it must be a safe slug.
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


AVATAR_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"


class AgentCreate(BaseModel):
    name: str = Field(pattern=AGENT_NAME_PATTERN)
    system_prompt: str = ""
    avatar: str = Field(default="manager", pattern=AVATAR_PATTERN)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    fallback_provider: str | None = None
    fallback_model: str | None = None
    jev_enabled: bool = False
    perm_browser: bool = True
    perm_shell: bool = True
    perm_files: bool = True
    # Any group left out is allowed (or follows the switches above).
    permissions: dict[Group, Level] | None = None
    unknown_action: UnknownAction = "default"
    vm_image: str = Field(default="debian-desktop", pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    vm_memory_mb: int = Field(default=4096, ge=512, le=65536)
    vm_cpus: int = Field(default=4, ge=1, le=32)


class AgentUpdate(BaseModel):
    """Editable fields. The name is immutable because it names the agent directory."""

    system_prompt: str | None = None
    avatar: str | None = Field(default=None, pattern=AVATAR_PATTERN)
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    fallback_provider: str | None = None
    fallback_model: str | None = None
    jev_enabled: bool | None = None
    perm_browser: bool | None = None
    perm_shell: bool | None = None
    perm_files: bool | None = None
    permissions: dict[Group, Level] | None = None
    unknown_action: UnknownAction | None = None
    vm_memory_mb: int | None = Field(default=None, ge=512, le=65536)
    vm_cpus: int | None = Field(default=None, ge=1, le=32)


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    system_prompt: str
    avatar: str
    provider: str
    model: str
    fallback_provider: str | None
    fallback_model: str | None
    jev_enabled: bool
    perm_browser: bool
    perm_shell: bool
    perm_files: bool
    # Every group with its level.
    permissions: dict[str, str] = {}
    unknown_action: str = "default"
    vm_image: str
    vm_memory_mb: int
    vm_cpus: int
    vm_disk_path: str
    vm_status: str
    vm_daemon_port: int | None
    created_at: datetime
    # What the agent is doing right now: "idle", "working" or "needs_approval".
    activity: str = "idle"
