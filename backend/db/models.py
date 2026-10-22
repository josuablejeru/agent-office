"""SQLite tables."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Agent(SQLModel, table=True):
    __tablename__ = "agents"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    system_prompt: str = ""
    # Key of the avatar the UI draws for this agent.
    avatar: str = "manager"
    provider: str
    model: str
    fallback_provider: str | None = None
    fallback_model: str | None = None
    jev_enabled: bool = False
    perm_browser: bool = True
    perm_shell: bool = True
    perm_files: bool = True
    # Level per kind of tool as JSON ("allow", "ask", "off"); see backend/policy/permissions.py.
    # Empty until chosen: the switches above then say what applies.
    permissions_json: str = ""
    # Actions no safety rule covers: "default" (the app's setting), "allow" or "ask".
    unknown_action: str = "default"
    # The model is shown screenshots of the browser (and may click by position).
    vision_enabled: bool = False
    # Names of the MCP servers (Settings › MCP servers) this agent may use, as a JSON list.
    mcp_servers_json: str = ""
    vm_image: str = "debian-desktop"
    vm_memory_mb: int = 4096
    vm_cpus: int = 4
    vm_disk_path: str
    vm_status: str = "not_created"
    # Assigned on first VM start and kept so the agent's endpoint stays stable.
    vm_daemon_port: int | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def mcp_servers(self) -> list[str]:
        try:
            names = json.loads(self.mcp_servers_json or "[]")
        except ValueError:
            return []
        return [str(name) for name in names] if isinstance(names, list) else []

    @property
    def permissions(self) -> dict[str, str]:
        """Every kind of tool with its level, wherever an agent is shown."""
        from backend.policy.permissions import permissions_of

        return dict(permissions_of(self))


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    run_id: int | None = Field(default=None, foreign_key="runs.id")
    role: str
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: int | None = Field(default=None, primary_key=True)
    agent_id: int = Field(foreign_key="agents.id", index=True)
    status: str = "running"
    error: str | None = None
    # Set when a channel message started this run: the reply is posted back there.
    channel_id: int | None = Field(default=None, foreign_key="channels.id")
    # How many agent-to-agent hand-offs led to this run (bounds mention chains).
    hops: int = 0
    # The model this run used, when the user switched away from the agent's own.
    provider: str | None = None
    model: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class KnownModel(SQLModel, table=True):
    """A model ID seen to exist for a provider: listed by it, tested, chosen or used.

    Suggested wherever a model ID is typed, so it never has to be typed twice.
    """

    __tablename__ = "known_models"
    __table_args__ = (UniqueConstraint("provider", "model"),)

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(index=True)
    model: str
    # "listed" (the provider said so), "tested", "chosen" (an agent's model) or "used".
    source: str = "listed"
    seen_at: datetime = Field(default_factory=utcnow)
    # Last time it worked (a passing Test or a finished task): those come first.
    worked_at: datetime | None = None


class Channel(SQLModel, table=True):
    __tablename__ = "channels"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class ChannelMessage(SQLModel, table=True):
    __tablename__ = "channel_messages"

    id: int | None = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channels.id", index=True)
    # An agent's name, "you" for the user, or "system" for notices.
    author: str
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class ToolCall(SQLModel, table=True):
    __tablename__ = "tool_calls"

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="runs.id", index=True)
    tool: str
    arguments_json: str = "{}"
    decision: str | None = None
    result_json: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class Approval(SQLModel, table=True):
    __tablename__ = "approvals"

    id: int | None = Field(default=None, primary_key=True)
    tool_call_id: int = Field(foreign_key="tool_calls.id", index=True)
    status: str = "pending"
    risk: str | None = None
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class ProviderConfig(SQLModel, table=True):
    __tablename__ = "provider_configs"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    type: str
    base_url: str | None = None
    # Name of the environment variable holding the key; the key itself is never stored.
    api_key_env: str | None = None
