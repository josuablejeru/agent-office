"""A registry of MCP servers, and the bridge that lets agents use their tools.

Servers are remote (MCP over Streamable HTTP). The app itself is the client:
servers' tokens stay in the macOS Keychain and never reach an agent's
computer. Each agent is given only the servers the user ticked for it, and
its MCP tool calls go through the usual permissions (the "MCP tools" group,
which asks first unless the user chose otherwise).

stdio servers are not supported: started on the Mac they would run with the
user's full rights, outside the agent's own computer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from backend.config import Settings
from backend.keystore import KeyStore
from backend.logging_config import get_logger

log = get_logger("mcp")

SERVER_NAME = r"^[a-z][a-z0-9-]{0,31}$"
CONFIG_PATH = ["mcp", "servers"]
MAX_TOOLS_PER_SERVER = 40
TOOLS_CACHE_SECONDS = 300
CALL_TIMEOUT_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 15
MAX_RESULT_CHARS = 20_000
# Keys some JSON schemas carry that models' tool definitions do not accept.
SCHEMA_KEYS_DROPPED = {"$schema", "$id", "title", "additionalProperties", "$defs", "definitions", "examples"}


class MCPServerConfig(BaseModel):
    name: str = Field(pattern=SERVER_NAME)
    url: str = Field(min_length=8, max_length=500)
    # The header the token goes in; "Authorization" sends "Bearer <token>".
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    description: str = Field(default="", max_length=300)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
            raise ValueError("give the server's address, like https://mcp.example.com/mcp")
        return value

    @property
    def token_key(self) -> str:
        return "MCP_" + re.sub(r"[^A-Za-z0-9]", "_", self.name).upper() + "_TOKEN"

    def headers(self, token: str | None) -> dict[str, str]:
        if not token:
            return {}
        if self.auth_header.lower() == "authorization" and " " not in token:
            return {"Authorization": f"Bearer {token}"}
        return {self.auth_header: token}


class MCPTool(BaseModel):
    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPError(Exception):
    pass


def clean_schema(schema: Any) -> Any:
    """A tool's input schema without the keys models' tool definitions reject."""
    if isinstance(schema, dict):
        return {key: clean_schema(value) for key, value in schema.items() if key not in SCHEMA_KEYS_DROPPED}
    if isinstance(schema, list):
        return [clean_schema(item) for item in schema]
    return schema


def tool_name(server: str, tool: str) -> str:
    """mcp_<server>_<tool>, limited to what every model provider accepts in a tool name."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", f"mcp_{server}_{tool}")
    if len(name) > 64:
        digest = hashlib.sha256(name.encode()).hexdigest()[:8]
        name = f"{name[:55]}_{digest}"
    return name


class HTTPFailure(Exception):
    """An MCP exchange that failed at the HTTP level; the SDK's own error does not say how."""

    def __init__(self, status: int, cause: BaseException) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.__cause__ = cause


def _reason(error: BaseException) -> str:
    """The innermost cause of an MCP failure, in words."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    status = getattr(error, "status", None) or getattr(getattr(error, "response", None), "status_code", None)
    if status in (401, 403):
        return "the server refused the token"
    if status == 404:
        return "there is no MCP server at that address"
    if status:
        return f"the server answered with HTTP {status}"
    name = type(error).__name__
    if "Connect" in name or "Timeout" in name or isinstance(error, (OSError, TimeoutError)):
        return f"the server could not be reached ({name})"
    return f"{name}: {' '.join(str(error).split())[:200]}"


class MCPRegistry:
    def __init__(self, settings: Settings, keys: KeyStore) -> None:
        self._settings = settings
        self._keys = keys
        self._tools: dict[str, tuple[float, list[MCPTool]]] = {}

    # --- the registry ------------------------------------------------------------------------

    def servers(self) -> list[MCPServerConfig]:
        listed = ((self._settings.load_config().get("mcp") or {}).get("servers")) or []
        found = []
        for entry in listed:
            try:
                found.append(MCPServerConfig.model_validate(entry))
            except ValueError:
                log.warning("mcp server in config.yaml is not valid", extra={"entry": str(entry)[:100]})
        return found

    def get(self, name: str) -> MCPServerConfig | None:
        return next((server for server in self.servers() if server.name == name), None)

    def has_token(self, server: MCPServerConfig) -> bool:
        return self._keys.has(server.token_key)

    def save(self, server: MCPServerConfig, token: str | None) -> None:
        others = [s.model_dump() for s in self.servers() if s.name != server.name]
        if token:
            self._keys.set(server.token_key, token)
        self._settings.update_user_config(CONFIG_PATH, [*others, server.model_dump()])
        self._tools.pop(server.name, None)

    def remove(self, name: str) -> bool:
        server = self.get(name)
        if server is None:
            return False
        self._settings.update_user_config(CONFIG_PATH, [s.model_dump() for s in self.servers() if s.name != name])
        self._keys.delete(server.token_key)
        self._tools.pop(name, None)
        return True

    # --- talking to servers --------------------------------------------------------------------

    async def _session(self, server: MCPServerConfig, token: str | None, work: Any) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        failures: list[int] = []

        async def note_status(response: Any) -> None:
            if response.status_code >= 400:
                failures.append(response.status_code)

        async def run() -> Any:
            http = create_mcp_http_client(headers=server.headers(token))
            http.event_hooks["response"].append(note_status)
            async with http, streamable_http_client(server.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await work(session)

        try:
            return await run()
        except BaseException as exc:
            if failures and not isinstance(exc, asyncio.CancelledError):
                raise HTTPFailure(failures[0], exc) from exc
            raise

    async def list_tools(self, server: MCPServerConfig, token: str | None) -> list[MCPTool]:
        async def work(session: Any) -> list[MCPTool]:
            listed = await session.list_tools()
            return [
                MCPTool(server=server.name, name=tool.name, description=(tool.description or "")[:1000],
                        input_schema=clean_schema(tool.input_schema or {"type": "object", "properties": {}}))
                for tool in listed.tools[:MAX_TOOLS_PER_SERVER]
            ]

        try:
            return await asyncio.wait_for(self._session(server, token, work), CONNECT_TIMEOUT_SECONDS)
        except BaseException as exc:  # noqa: BLE001 - the SDK raises exception groups
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise MCPError(_reason(exc)) from None

    async def test(self, server: MCPServerConfig, token: str | None) -> dict[str, Any]:
        token = token or self._keys.get(server.token_key)
        try:
            tools = await self.list_tools(server, token)
        except MCPError as exc:
            return {"ok": False, "detail": f"Could not use {server.url}: {exc}.", "tools": []}
        return {"ok": True, "detail": f"Connected: {len(tools)} tool(s).", "tools": [tool.name for tool in tools]}

    async def tools(self, name: str) -> list[MCPTool]:
        """The tools of a registered server, cached for a few minutes; [] if it cannot be reached."""
        cached = self._tools.get(name)
        if cached and time.monotonic() - cached[0] < TOOLS_CACHE_SECONDS:
            return cached[1]
        server = self.get(name)
        if server is None:
            return []
        try:
            tools = await self.list_tools(server, self._keys.get(server.token_key))
        except MCPError as exc:
            log.warning("mcp server unavailable", extra={"server": name, "reason": str(exc)})
            return []
        self._tools[name] = (time.monotonic(), tools)
        return tools

    async def call(self, name: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool; the result (or what went wrong) comes back as a tool result, never an exception."""
        server = self.get(name)
        if server is None:
            return {"error": f"The MCP server '{name}' is no longer set up."}

        async def work(session: Any) -> Any:
            return await session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT_SECONDS)

        try:
            result = await asyncio.wait_for(
                self._session(server, self._keys.get(server.token_key), work), CALL_TIMEOUT_SECONDS + 10)
        except BaseException as exc:  # noqa: BLE001 - the SDK raises exception groups
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"error": f"The MCP server '{name}' failed: {_reason(exc)}."}
        texts, omitted = [], 0
        for block in getattr(result, "content", None) or []:
            if getattr(block, "type", "") == "text":
                texts.append(block.text)
            else:
                omitted += 1
        structured = getattr(result, "structured_content", None)
        text = "\n".join(texts) or (json.dumps(structured)[:MAX_RESULT_CHARS] if structured else "")
        answer: dict[str, Any] = {"server": name, "tool": tool, "result": text[:MAX_RESULT_CHARS],
                                  "truncated": len(text) > MAX_RESULT_CHARS}
        if omitted:
            answer["note"] = f"{omitted} non-text part(s) (images, files) were left out."
        if getattr(result, "is_error", False):
            answer["error"] = text[:2000] or "the tool reported an error"
        return answer
