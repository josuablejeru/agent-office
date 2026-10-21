"""A registry of MCP servers, and the bridge that lets agents use their tools.

Two kinds of server, as in Claude Desktop:
- **command** servers: a program the app starts on this Mac (`npx …`, `uvx …`,
  a local script) and talks to over stdin/stdout. They run with the user's
  rights, like in Claude Desktop, not inside an agent's computer;
- **remote** servers: MCP over Streamable HTTP, with an optional token.

The app itself is the client: tokens and environment values stay in the macOS
Keychain and never reach an agent's computer. Each agent is given only the
servers the user ticked for it, and its MCP tool calls go through the usual
permissions (the "MCP tools" group, which asks first unless the user chose
otherwise).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

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
START_TIMEOUT_SECONDS = 120
FAILURE_RETRY_SECONDS = 120
MAX_RESULT_CHARS = 20_000
# Keys some JSON schemas carry that models' tool definitions do not accept.
SCHEMA_KEYS_DROPPED = {"$schema", "$id", "title", "additionalProperties", "$defs", "definitions", "examples"}


class MCPServerConfig(BaseModel):
    name: str = Field(pattern=SERVER_NAME)
    # "http": a remote server at `url`; "stdio": a program started on this Mac.
    transport: Literal["http", "stdio"] = "http"
    url: str = Field(default="", max_length=500)
    command: str = Field(default="", max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    cwd: str = Field(default="", max_length=500)
    # The header the token goes in; "Authorization" sends "Bearer <token>".
    auth_header: str = Field(default="Authorization", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    description: str = Field(default="", max_length=300)
    # Its tools ask every time, whatever the agent's MCP tools level says. On by
    # default for command servers: they run on this Mac with the user's rights.
    always_ask: bool = False

    @model_validator(mode="after")
    def _complete(self) -> MCPServerConfig:
        if self.transport == "http":
            self.url = self.url.strip()
            parts = urlsplit(self.url)
            if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
                raise ValueError("give the server's address, like https://mcp.example.com/mcp")
        else:
            self.command = self.command.strip()
            if not self.command or "\n" in self.command:
                raise ValueError("give the command that starts the server, like npx or uvx")
            if any(len(arg) > 2000 or "\x00" in arg for arg in self.args):
                raise ValueError("an argument is too long or not text")
        return self

    @property
    def env_key(self) -> str:
        return "MCP_" + re.sub(r"[^A-Za-z0-9]", "_", self.name).upper() + "_ENV"

    def describe(self) -> str:
        return self.url if self.transport == "http" else " ".join([self.command, *self.args])

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
    if isinstance(error, FileNotFoundError):
        missing = error.filename or "the command"
        return f"{missing} was not found (is it installed? the app looks where your login shell does)"
    if isinstance(error, (EOFError, BrokenPipeError)) or "ClosedResource" in type(error).__name__:
        return "the server stopped; its log is in ~/.config/agent-office/logs"
    name = type(error).__name__
    if "Connect" in name or "Timeout" in name or isinstance(error, (OSError, TimeoutError)):
        return f"the server could not be reached ({name})"
    return f"{name}: {' '.join(str(error).split())[:200]}"


# What command servers get from the user's login shell besides PATH: how to get
# through a company proxy, which certificates to trust, and where npm, pip and uv
# download from (npx -y … and uvx … fetch the server on first use).
FORWARDED = re.compile(
    r"^(https?_proxy|all_proxy|no_proxy|NODE_EXTRA_CA_CERTS|SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE"
    r"|CURL_CA_BUNDLE|NODE_OPTIONS|npm_config_\w+|PIP_\w+|UV_\w+|VOLTA_HOME|NVM_DIR|PNPM_HOME|BUN_INSTALL)$",
    re.IGNORECASE,
)
_LOGIN_ENV: dict[str, str] | None = None


def login_environment() -> dict[str, str]:
    """PATH and the variables above, as the user's login shell has them.

    An app opened from Finder only gets /usr/bin:/bin:… and none of the user's
    proxy settings otherwise, so npx and uvx would be missing or blocked.
    """
    global _LOGIN_ENV
    if _LOGIN_ENV is None:
        from backend.shell_env import read_terminal_environment

        found = read_terminal_environment()
        home = Path.home()
        # Where installers put npx, uv and friends, in case no shell file says so.
        extra = ":".join(["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin"),
                          str(home / ".cargo" / "bin"), str(home / ".volta" / "bin"), str(home / ".bun" / "bin")])
        path = ":".join(part for part in (found.get("PATH", ""), extra, os.environ.get("PATH", "")) if part)
        _LOGIN_ENV = {name: value for name, value in found.items() if FORWARDED.match(name) and value}
        _LOGIN_ENV["PATH"] = path
    return _LOGIN_ENV


class StdioConnection:
    """One running command server, kept open: started on first use, restarted after it dies.

    The SDK's stdio client must be entered and left in one task, so a task owns
    the session and works through requests from its queue, one at a time. A
    connection that broke is set aside at once: the next request starts a fresh
    server rather than waiting on one that is still shutting down.
    """

    def __init__(
        self, server: MCPServerConfig, env: dict[str, str], log_path: Path, base_env: dict[str, str] | None = None
    ) -> None:
        self.server = server
        self._env = env
        self._base_env = base_env or {}
        self._log_path = log_path
        self._queue: asyncio.Queue[tuple[Any, asyncio.Future[Any]] | None] | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._queue is not None and self._task is not None and not self._task.done()

    async def request(self, work: Any, timeout: float) -> Any:
        if not self.running:
            queue: asyncio.Queue[tuple[Any, asyncio.Future[Any]] | None] = asyncio.Queue()
            self._queue = queue
            self._task = asyncio.create_task(self._serve(queue))
            self._task.add_done_callback(lambda task: self._fail_waiting(task, queue))
        assert self._queue is not None
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((work, future))
        return await asyncio.wait_for(future, timeout)

    @staticmethod
    def _fail_waiting(task: asyncio.Task[None], queue: asyncio.Queue[Any]) -> None:
        """The server is gone: whoever is still waiting learns why, instead of waiting forever."""
        error = task.exception() if not task.cancelled() else None
        while not queue.empty():
            item = queue.get_nowait()
            if item is not None and not item[1].done():
                item[1].set_exception(error or MCPError("the server stopped"))

    async def _serve(self, queue: asyncio.Queue[Any]) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

        # Later wins: the safe basics, the app's defaults (company certificates),
        # the user's own login-shell settings, and what the user set for this server.
        env = {**get_default_environment(), **self._base_env, **login_environment(), **self._env}
        params = StdioServerParameters(
            command=self.server.command, args=self.server.args, env=env,
            cwd=os.path.expanduser(self.server.cwd) if self.server.cwd else None,
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a") as errlog:
            # The command only: arguments may hold tokens.
            errlog.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} starting {self.server.command}\n")
            errlog.flush()
            async with stdio_client(params, errlog=errlog) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    log.info("mcp server started", extra={"server": self.server.name})
                    while True:
                        item = await queue.get()
                        if item is None:
                            return
                        work, future = item
                        try:
                            result = await work(session)
                        except Exception as exc:  # noqa: BLE001 - goes to the caller
                            if not future.done():
                                future.set_exception(exc)
                            if not self._healthy(exc):
                                if self._queue is queue:
                                    self._queue = None  # set aside: the next request starts afresh
                                raise
                            continue
                        if not future.done():
                            future.set_result(result)

    @staticmethod
    def _healthy(error: Exception) -> bool:
        """A request the server turned down is fine; a lost connection means starting it again.

        (A tool that fails is not an exception at all: it comes back as a result marked as an error.)
        """
        if "closed" in str(error).lower():
            return False
        return type(error).__name__ in ("MCPError", "McpError")

    async def close(self) -> None:
        if self._queue is not None and self._task is not None and not self._task.done():
            await self._queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 10)
            except (TimeoutError, Exception):  # noqa: BLE001 - stopping must not fail
                self._task.cancel()


def _close_later(connection: StdioConnection) -> None:
    """Stop a command server without waiting for it (it runs on the app's event loop, if any)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop running: the loop it ran on, and so the server, is already gone
    asyncio.ensure_future(connection.close())


class MCPRegistry:
    def __init__(self, settings: Settings, keys: KeyStore) -> None:
        self._settings = settings
        self._keys = keys
        self._tools: dict[str, tuple[float, list[MCPTool]]] = {}
        self._connections: dict[str, StdioConnection] = {}
        # Servers that could not be reached, and when: not tried again at every task's start.
        self._failed: dict[str, float] = {}
        # Extra environment for command servers, from the app (company certificates).
        self.base_env: Any = None

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

    def env(self, server: MCPServerConfig) -> dict[str, str]:
        """A command server's environment values, kept in the Keychain (they are often tokens)."""
        stored = self._keys.get(server.env_key)
        if not stored:
            return {}
        try:
            values = json.loads(base64.b64decode(stored))
        except (ValueError, TypeError):
            return {}
        return {str(k): str(v) for k, v in values.items()} if isinstance(values, dict) else {}

    def merged_env(self, server: MCPServerConfig, given: dict[str, str] | None) -> dict[str, str]:
        """The form shows names only: an empty value keeps what is stored, a missing name is removed."""
        stored = self.env(server)
        if given is None:
            return stored
        merged = {name.strip(): (value if value != "" else stored.get(name.strip(), "")) for name, value in given.items()}
        return {name: value for name, value in merged.items() if name}

    def save(self, server: MCPServerConfig, token: str | None, env: dict[str, str] | None = None) -> None:
        others = [s.model_dump() for s in self.servers() if s.name != server.name]
        if token:
            self._keys.set(server.token_key, token)
        if env is not None:
            env = self.merged_env(server, env)
            if env:
                self._keys.set(server.env_key, base64.b64encode(json.dumps(env).encode()).decode())
            else:
                self._keys.delete(server.env_key)
        self._settings.update_user_config(CONFIG_PATH, [*others, server.model_dump()])
        self._forget(server.name)

    def remove(self, name: str) -> bool:
        server = self.get(name)
        if server is None:
            return False
        self._settings.update_user_config(CONFIG_PATH, [s.model_dump() for s in self.servers() if s.name != name])
        self._keys.delete(server.token_key)
        self._keys.delete(server.env_key)
        self._forget(name)
        return True

    def _forget(self, name: str) -> None:
        """Changed or removed: its cached tools go, and a running command server is stopped."""
        self._tools.pop(name, None)
        self._failed.pop(name, None)
        connection = self._connections.pop(name, None)
        if connection is not None:
            _close_later(connection)

    def log_path(self, name: str) -> Path:
        return self._settings.home / "logs" / f"mcp-{name}.log"

    async def close(self) -> None:
        """Stop every command server (the app is quitting)."""
        connections, self._connections = list(self._connections.values()), {}
        await asyncio.gather(*(connection.close() for connection in connections), return_exceptions=True)

    # --- talking to servers --------------------------------------------------------------------

    async def _session(
        self, server: MCPServerConfig, token: str | None, work: Any,
        env: dict[str, str] | None = None, timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> Any:
        if server.transport == "stdio":
            connection = self._connections.get(server.name)
            if env is not None or connection is None or connection.server != server:
                if env is None and connection is not None:
                    _close_later(connection)  # replaced: the old process must not linger
                # A test of unsaved settings gets a connection of its own.
                base = await self.base_env() if self.base_env is not None else {}
                connection = StdioConnection(server, env if env is not None else self.env(server),
                                             self.log_path(server.name), base)
                if env is None:
                    self._connections[server.name] = connection
            try:
                return await connection.request(work, timeout)
            finally:
                if env is not None:
                    await connection.close()

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

    async def list_tools(
        self, server: MCPServerConfig, token: str | None, env: dict[str, str] | None = None
    ) -> list[MCPTool]:
        async def work(session: Any) -> list[MCPTool]:
            listed = await session.list_tools()
            return [
                MCPTool(server=server.name, name=tool.name, description=(tool.description or "")[:1000],
                        input_schema=clean_schema(tool.input_schema or {"type": "object", "properties": {}}))
                for tool in listed.tools[:MAX_TOOLS_PER_SERVER]
            ]

        # A command server may first have to be downloaded (npx, uvx): give it longer.
        wait = START_TIMEOUT_SECONDS if server.transport == "stdio" else CONNECT_TIMEOUT_SECONDS
        try:
            return await asyncio.wait_for(self._session(server, token, work, env=env, timeout=wait), wait + 5)
        except BaseException as exc:  # noqa: BLE001 - the SDK raises exception groups
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise MCPError(_reason(exc)) from None

    async def test(
        self, server: MCPServerConfig, token: str | None, env: dict[str, str] | None = None
    ) -> dict[str, Any]:
        token = token or self._keys.get(server.token_key)
        if server.transport == "stdio":
            env = self.merged_env(server, env)
        try:
            tools = await self.list_tools(server, token, env)
        except MCPError as exc:
            what = server.url if server.transport == "http" else server.command  # arguments may hold tokens
            return {"ok": False, "detail": f"Could not use {what}: {exc}.", "tools": []}
        return {"ok": True, "detail": f"Connected: {len(tools)} tool(s).", "tools": [tool.name for tool in tools]}

    async def tools(self, name: str) -> list[MCPTool]:
        """The tools of a registered server, cached for a few minutes; [] if it cannot be reached."""
        cached = self._tools.get(name)
        if cached and time.monotonic() - cached[0] < TOOLS_CACHE_SECONDS:
            return cached[1]
        server = self.get(name)
        if server is None:
            return []
        if time.monotonic() - self._failed.get(name, -FAILURE_RETRY_SECONDS) < FAILURE_RETRY_SECONDS:
            return []
        try:
            tools = await self.list_tools(server, self._keys.get(server.token_key))
        except MCPError as exc:
            log.warning("mcp server unavailable", extra={"server": name, "reason": str(exc)})
            self._failed[name] = time.monotonic()
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


# --- Claude Desktop ---------------------------------------------------------------------------

CLAUDE_DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def server_name(raw: str) -> str:
    """A name this app accepts, from one Claude Desktop uses ("GitHub Tools" -> "github-tools")."""
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    if not name or not name[0].isalpha():
        name = f"mcp-{name}".strip("-")
    return name[:32].rstrip("-") or "mcp"


def read_claude_desktop(path: Path = CLAUDE_DESKTOP_CONFIG) -> list[tuple[MCPServerConfig, dict[str, str], str]]:
    """The servers in Claude Desktop's config: (settings, environment, token) for each usable one."""
    try:
        entries = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return []
    found = []
    for raw_name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        token = ""
        try:
            url = entry.get("url") or entry.get("serverUrl")
            if url:
                headers = entry.get("headers") or {}
                auth = str(headers.get("Authorization") or headers.get("authorization") or "")
                token = auth.removeprefix("Bearer ").strip()
                server = MCPServerConfig(name=server_name(raw_name), transport="http", url=str(url))
            else:
                server = MCPServerConfig(
                    name=server_name(raw_name), transport="stdio", command=str(entry.get("command", "")),
                    args=[str(arg) for arg in entry.get("args") or []], cwd=str(entry.get("cwd") or ""),
                    always_ask=True,
                )
        except ValueError:
            continue
        found.append((server, env, token))
    return found
