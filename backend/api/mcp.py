"""The MCP server registry, for Settings › MCP servers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.keystore import KeyStoreError
from backend.net.mcp import MCPServerConfig

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class ServerForm(BaseModel):
    name: str
    url: str
    auth_header: str = "Authorization"
    description: str = ""
    # Left empty to keep the stored one (or for a server that needs none).
    token: str = Field(default="", max_length=4096)


def _config(form: ServerForm) -> MCPServerConfig:
    try:
        return MCPServerConfig(name=form.name.strip(), url=form.url, auth_header=form.auth_header.strip() or "Authorization",
                               description=form.description.strip())
    except ValueError as exc:
        first = exc.errors()[0] if hasattr(exc, "errors") else {}
        field = ".".join(str(part) for part in first.get("loc", ())) or "server"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field}: {first.get('msg', exc)}") from exc


@router.get("/servers")
async def list_servers(request: Request) -> list[dict[str, Any]]:
    registry = request.app.state.mcp
    return [{**server.model_dump(), "has_token": registry.has_token(server)} for server in registry.servers()]


@router.put("/servers/{name}")
async def save_server(name: str, form: ServerForm, request: Request) -> dict[str, Any]:
    if form.name != name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The name in the address and the form differ.")
    server = _config(form)
    try:
        request.app.state.mcp.save(server, form.token.strip() or None)
    except KeyStoreError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"token: {exc}") from exc
    return {**server.model_dump(), "has_token": request.app.state.mcp.has_token(server)}


@router.delete("/servers/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_server(name: str, request: Request) -> None:
    if not request.app.state.mcp.remove(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No MCP server named '{name}'.")


@router.post("/servers/test")
async def test_server(form: ServerForm, request: Request) -> dict[str, Any]:
    """Connect, list the tools, and say what is wrong if that fails (before or after saving)."""
    return await request.app.state.mcp.test(_config(form), form.token.strip() or None)
