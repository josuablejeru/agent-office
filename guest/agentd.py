"""Guest agent daemon: an authenticated WebSocket server exposing controlled operations.

Runs inside the agent VM as the `agent` user:  python -m guest.agentd

Protocol: the client opens a WebSocket with `Authorization: Bearer <secret>` and
sends JSON requests `{"id": ..., "op": "shell.exec", "args": {...}}`. Each gets
one reply, `{"id": ..., "ok": true, "result": {...}}` or
`{"id": ..., "ok": false, "error": "..."}`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from guest.browser import Browser
from guest.database import db_list, db_sql
from guest.files import file_list, file_read, file_write
from guest.memory import memory_context, memory_forget, memory_recall, memory_remember
from guest.network import configure_dns
from guest.shared import (
    ensure_shared_dir,
    shared_finish,
    shared_list,
    shared_read_chunk,
    shared_stat,
    shared_write_chunk,
)
from guest.shell import shell_exec
from guest.validation import OperationError

log = logging.getLogger("agentd")

DEFAULT_PORT = 8765
DEFAULT_SECRET_PATH = "/etc/agent-office/agent-secret"
# Largest request frame; file.write content dominates.
MAX_MESSAGE_BYTES = 4_500_000
MAX_REPLY_BYTES = 4_300_000

Operation = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def ping(args: dict[str, Any]) -> dict[str, Any]:
    return {"pong": True}


BROWSER = Browser()

OPERATIONS: dict[str, Operation] = {
    "ping": ping,
    "shell.exec": shell_exec,
    "file.read": file_read,
    "file.write": file_write,
    "file.list": file_list,
    "browser.search": BROWSER.search,
    "browser.goto": BROWSER.goto,
    "browser.click": BROWSER.click,
    "browser.type": BROWSER.type,
    "browser.press": BROWSER.press,
    "browser.scroll": BROWSER.scroll,
    "browser.extract_text": BROWSER.extract_text,
    "browser.screenshot": BROWSER.screenshot,
    "browser.current_url": BROWSER.current_url,
    "browser.close": BROWSER.close,
    "memory.remember": memory_remember,
    "memory.recall": memory_recall,
    "memory.forget": memory_forget,
    "memory.context": memory_context,
    "db.sql": db_sql,
    "db.list": db_list,
    "shared.list": shared_list,
    "shared.write_chunk": shared_write_chunk,
    "shared.finish": shared_finish,
    "shared.stat": shared_stat,
    "shared.read_chunk": shared_read_chunk,
}


def is_authorized(header: str | None, secret: str) -> bool:
    if not secret or header is None or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header.removeprefix("Bearer ").encode(), secret.encode())


async def handle_message(raw: str | bytes) -> dict[str, Any]:
    """Validate and run one request. Never raises: failures become error replies."""
    request_id: Any = None
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise OperationError("request must be a JSON object")
        request_id = request.get("id")
        op = request.get("op")
        args = request.get("args", {})
        if not isinstance(op, str) or op not in OPERATIONS:
            raise OperationError(f"unknown operation: {op!r}")
        if not isinstance(args, dict):
            raise OperationError("'args' must be an object")
        result = await OPERATIONS[op](args)
        log.info("op=%s ok", op)
        return {"id": request_id, "ok": True, "result": result}
    except (OperationError, json.JSONDecodeError) as exc:
        log.info("request rejected: %s", exc)
        return {"id": request_id, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the daemon must survive any operation
        log.exception("operation failed")
        return {"id": request_id, "ok": False, "error": f"internal error: {exc}"}


def encode_reply(reply: dict[str, Any]) -> str:
    """Serialise a reply, never larger than the other side accepts."""
    # ensure_ascii=False: escaped, a megabyte of non-ASCII text would be six on the wire.
    encoded = json.dumps(reply, ensure_ascii=False)
    if len(encoded.encode("utf-8", "replace")) > MAX_REPLY_BYTES:
        return json.dumps(
            {"id": reply.get("id"), "ok": False, "error": "the result is too large to return; ask for less"}
        )
    return encoded


async def serve(host: str, port: int, secret: str) -> None:
    # Imported here so the dispatch logic above stays importable without websockets.
    from websockets.asyncio.server import ServerConnection
    from websockets.asyncio.server import serve as ws_serve
    from websockets.http11 import Request, Response

    def authenticate(connection: ServerConnection, request: Request) -> Response | None:
        if is_authorized(request.headers.get("Authorization"), secret):
            return None
        log.warning("rejected unauthenticated connection")
        return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")

    async def handler(connection: ServerConnection) -> None:
        async for raw in connection:
            await connection.send(encode_reply(await handle_message(raw)))

    async with ws_serve(
        handler, host, port, process_request=authenticate, max_size=MAX_MESSAGE_BYTES
    ):
        log.info("agentd listening on %s:%d", host, port)
        # The agent's browser is part of its desktop: open it without waiting to be asked.
        await BROWSER.keep_open()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="agentd: %(message)s")
    secret_path = Path(os.environ.get("AGENTD_SECRET_FILE", DEFAULT_SECRET_PATH))
    secret = secret_path.read_text().strip()
    if not secret:
        raise SystemExit(f"{secret_path} is empty; refusing to start without a secret")
    ensure_shared_dir()
    configure_dns()
    # Reachable only through QEMU's host-loopback port forward.
    host = os.environ.get("AGENTD_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENTD_PORT", DEFAULT_PORT))
    asyncio.run(serve(host, port, secret))


if __name__ == "__main__":
    main()
