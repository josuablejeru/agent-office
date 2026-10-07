"""Agent CRUD: database rows plus the per-agent directory and agent.yaml."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session, delete, select

from backend.agents.models import AgentCreate, AgentUpdate
from backend.config import Settings
from backend.db.models import Agent, Approval, Message, Run, ToolCall
from backend.logging_config import get_logger
from backend.policy.permissions import permissions_of, store_permissions
from backend.vm.ports import allocate_daemon_port, is_port_free

log = get_logger("agents")


class AgentError(Exception):
    """Base class for agent management errors."""


class AgentNotFound(AgentError):
    pass


class AgentAlreadyExists(AgentError):
    pass


class UnknownProvider(AgentError):
    pass


def agent_to_yaml_dict(agent: Agent) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": agent.name,
        "system_prompt": agent.system_prompt,
        "vm": {
            "image": agent.vm_image,
            "memory_mb": agent.vm_memory_mb,
            "cpus": agent.vm_cpus,
        },
        "model": {"provider": agent.provider, "model": agent.model},
    }
    if agent.fallback_provider and agent.fallback_model:
        data["fallback_model"] = {
            "provider": agent.fallback_provider,
            "model": agent.fallback_model,
        }
    data["jev"] = {"enabled": agent.jev_enabled, "provider": "jev"}
    data["permissions"] = {
        **permissions_of(agent),
        "unknown_actions": agent.unknown_action,
    }
    return data


class AgentManager:
    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def list(self) -> list[Agent]:
        return list(self._session.exec(select(Agent).order_by(Agent.created_at)).all())

    def get(self, agent_id: int) -> Agent:
        agent = self._session.get(Agent, agent_id)
        if agent is None:
            raise AgentNotFound(f"agent {agent_id} not found")
        return agent

    def create(self, payload: AgentCreate) -> Agent:
        self._check_providers(payload.provider, payload.fallback_provider)
        existing = self._session.exec(select(Agent).where(Agent.name == payload.name)).first()
        agent_dir = self._settings.agent_dir(payload.name)
        if existing is not None or agent_dir.exists():
            raise AgentAlreadyExists(f"agent '{payload.name}' already exists")

        fields = payload.model_dump()
        levels = fields.pop("permissions") or {}
        agent = Agent(**fields, vm_disk_path=str(agent_dir / "disk.qcow2"))
        store_permissions(agent, levels)
        (agent_dir / "logs").mkdir(parents=True)
        self._session.add(agent)
        self._session.commit()
        self._session.refresh(agent)
        self._write_yaml(agent, agent_dir)
        log.info("agent created", extra={"agent": agent.name, "provider": agent.provider})
        return agent

    def update(self, agent_id: int, payload: AgentUpdate) -> Agent:
        agent = self.get(agent_id)
        changes = payload.model_dump(exclude_unset=True)
        levels = changes.pop("permissions", None) or {}
        if changes.get("unknown_action", "") is None:
            del changes["unknown_action"]
        # The older on/off switches, when sent alone, still mean what they say.
        for group, switch in (("shell", "perm_shell"), ("files", "perm_files"), ("browser", "perm_browser")):
            if switch in changes and group not in levels and changes[switch] is not None:
                if changes[switch] != (permissions_of(agent)[group] != "off"):
                    levels[group] = "allow" if changes[switch] else "off"
        self._check_providers(changes.get("provider"), changes.get("fallback_provider"))
        for key, value in changes.items():
            setattr(agent, key, value)
        if levels:
            store_permissions(agent, levels)
        self._session.add(agent)
        self._session.commit()
        self._session.refresh(agent)
        self._write_yaml(agent, self._settings.agent_dir(agent.name))
        log.info("agent updated", extra={"agent": agent.name, "fields": sorted(changes)})
        return agent

    def delete(self, agent_id: int) -> None:
        """Remove the agent row and its directory, including the VM disk."""
        agent = self.get(agent_id)
        agent_dir = self._settings.agent_dir(agent.name)
        if agent_dir.parent.resolve() != self._settings.agents_dir.resolve():
            raise AgentError(f"refusing to delete unexpected path {agent_dir}")
        self._delete_history(agent_id)
        self._session.delete(agent)
        self._session.commit()
        shutil.rmtree(agent_dir, ignore_errors=True)
        log.info("agent deleted", extra={"agent": agent.name})

    def clear_history(self, agent_id: int) -> None:
        self._delete_history(agent_id)
        self._session.commit()

    def _delete_history(self, agent_id: int) -> None:
        """Remove the agent's conversation. SQLite reuses ids, so leftovers would
        otherwise show up in the next agent that gets this id."""
        run_ids = select(Run.id).where(Run.agent_id == agent_id)
        call_ids = select(ToolCall.id).where(ToolCall.run_id.in_(run_ids))  # type: ignore[attr-defined]
        self._session.exec(delete(Approval).where(Approval.tool_call_id.in_(call_ids)))  # type: ignore[attr-defined]
        self._session.exec(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))  # type: ignore[attr-defined]
        self._session.exec(delete(Message).where(Message.agent_id == agent_id))  # type: ignore[arg-type]
        self._session.exec(delete(Run).where(Run.agent_id == agent_id))  # type: ignore[arg-type]

    def assign_vm_endpoints(self, agent: Agent) -> None:
        """Give a stopped agent's VM its guest daemon port, keeping it stable if possible."""
        others = [other for other in self.list() if other.id != agent.id]
        daemon_ports = {o.vm_daemon_port for o in others if o.vm_daemon_port is not None}

        port = agent.vm_daemon_port
        if port is None or port in daemon_ports or not is_port_free(port):
            agent.vm_daemon_port = allocate_daemon_port(daemon_ports)
        self._session.add(agent)
        self._session.commit()
        self._session.refresh(agent)

    def set_vm_status(self, agent: Agent, vm_status: str) -> None:
        """Update the cached VM state; the live state always comes from the VM manager."""
        if agent.vm_status != vm_status:
            agent.vm_status = vm_status
            self._session.add(agent)
            self._session.commit()
            self._session.refresh(agent)

    def _check_providers(self, *names: str | None) -> None:
        known = self._settings.load_providers()
        for name in names:
            if name is not None and name not in known:
                raise UnknownProvider(
                    f"provider '{name}' is not defined in {self._settings.config_path}"
                )

    def _write_yaml(self, agent: Agent, agent_dir: Path) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.yaml").write_text(
            yaml.safe_dump(agent_to_yaml_dict(agent), sort_keys=False)
        )
