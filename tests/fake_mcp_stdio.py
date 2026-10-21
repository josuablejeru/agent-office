"""A small MCP server run as a command, over stdin and stdout, for tests."""

from __future__ import annotations

import os

import anyio
from mcp.server.mcpserver import MCPServer

server = MCPServer("notes")


@server.tool()
def whoami() -> str:
    """Says which process answers, and what it was given."""
    local_bin = "/.local/bin" in os.environ.get("PATH", "")
    return (f"pid={os.getpid()} token={os.environ.get('NOTES_TOKEN', 'none')} local-bin-on-path={local_bin}"
            f" proxy={os.environ.get('HTTPS_PROXY', 'none')} node-ca={os.environ.get('NODE_EXTRA_CA_CERTS', 'none')}"
            f" other={os.environ.get('NOTES_OTHER', 'none')}")


@server.tool()
def crash() -> str:
    """Ends the server abruptly."""
    os._exit(3)


if __name__ == "__main__":
    anyio.run(server.run_stdio_async)
