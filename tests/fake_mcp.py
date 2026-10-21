"""A small MCP server over Streamable HTTP, behind a bearer token, for tests."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer


class FakeMCPServer:
    def __init__(self, token: str = "mcp-secret") -> None:
        server = MCPServer("tickets")
        self.calls: list[dict[str, Any]] = []
        outer = self

        @server.tool()
        def find_ticket(number: int) -> str:
            """Look up a support ticket by its number."""
            outer.calls.append({"tool": "find_ticket", "number": number})
            return f"Ticket {number}: the printer on floor 2 jams. Assigned to Ben."

        @server.tool()
        def close_ticket(number: int, note: str = "") -> str:
            """Close a support ticket."""
            outer.calls.append({"tool": "close_ticket", "number": number})
            if number == 0:
                raise ValueError("there is no ticket 0")
            return f"Ticket {number} closed."

        inner = server.streamable_http_app()
        expected = f"Bearer {token}".encode()

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != expected:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            await inner(scope, receive, send)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        while not self._server.started:
            time.sleep(0.05)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self._server.should_exit = True
