"""The MCP server registry, for Settings › MCP servers."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.keystore import KeyStoreError
from backend.net.mcp import MCPServerConfig, read_claude_desktop

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class ServerForm(BaseModel):
    name: str
    transport: Literal["http", "stdio"] = "http"
    url: str = ""
    auth_header: str = "Authorization"
    command: str = ""
    args: list[str] = Field(default_factory=list, max_length=50)
    cwd: str = ""
    description: str = ""
    always_ask: bool = False
    # Left empty to keep the stored one (or for a server that needs none).
    token: str = Field(default="", max_length=4096)
    # None keeps the stored environment; {} clears it.
    env: dict[str, str] | None = None


def _config(form: ServerForm) -> MCPServerConfig:
    try:
        return MCPServerConfig(
            name=form.name.strip(), transport=form.transport, url=form.url,
            auth_header=form.auth_header.strip() or "Authorization", command=form.command,
            args=[arg for arg in form.args if arg != ""], cwd=form.cwd.strip(), description=form.description.strip(),
            always_ask=form.always_ask,
        )
    except ValueError as exc:
        first = exc.errors()[0] if hasattr(exc, "errors") else {}
        field = ".".join(str(part) for part in first.get("loc", ())) or "server"
        message = str(first.get("msg", exc)).removeprefix("Value error, ")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field}: {message}" if field != "server" else message) from exc


def _shown(registry: Any, server: MCPServerConfig) -> dict[str, Any]:
    # Environment names only: their values are often tokens.
    return {**server.model_dump(), "has_token": registry.has_token(server), "env_keys": sorted(registry.env(server))}


@router.get("/servers")
async def list_servers(request: Request) -> list[dict[str, Any]]:
    registry = request.app.state.mcp
    return [_shown(registry, server) for server in registry.servers()]


@router.put("/servers/{name}")
async def save_server(name: str, form: ServerForm, request: Request) -> dict[str, Any]:
    if form.name != name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The name in the address and the form differ.")
    server = _config(form)
    try:
        request.app.state.mcp.save(server, form.token.strip() or None, form.env)
    except KeyStoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"token: {exc}") from exc
    return _shown(request.app.state.mcp, server)


@router.delete("/servers/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_server(name: str, request: Request) -> None:
    if not request.app.state.mcp.remove(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No MCP server named '{name}'.")


@router.post("/servers/test")
async def test_server(form: ServerForm, request: Request) -> dict[str, Any]:
    """Connect, list the tools, and say what is wrong if that fails (before or after saving)."""
    return await request.app.state.mcp.test(_config(form), form.token.strip() or None, form.env)


@router.get("/claude-desktop")
async def claude_desktop_servers(request: Request) -> dict[str, Any]:
    """What Claude Desktop has set up, to import (environment names only, never values)."""
    existing = {server.name for server in request.app.state.mcp.servers()}
    servers = [
        {"name": server.name, "transport": server.transport, "describe": server.describe(),
         "env_keys": sorted(env), "has_token": bool(token), "exists": server.name in existing}
        for server, env, token in read_claude_desktop()
    ]
    return {"servers": servers}


class ImportRequest(BaseModel):
    names: list[str] = Field(max_length=50)


@router.post("/claude-desktop/import")
async def import_from_claude_desktop(payload: ImportRequest, request: Request) -> list[dict[str, Any]]:
    registry = request.app.state.mcp
    imported = []
    for server, env, token in read_claude_desktop():
        if server.name in payload.names:
            try:
                registry.save(server, token or None, env)
            except KeyStoreError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{server.name}: {exc}") from exc
            imported.append(_shown(registry, server))
    return imported
