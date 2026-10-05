"""Host-side client for the guest agent daemon."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from backend.logging_config import get_logger
from backend.vm.errors import VMError
from backend.vm.ports import LOOPBACK

log = get_logger("guest")

MAX_REPLY_BYTES = 4_500_000
# Added to an operation's own timeout to cover connection and transfer.
TRANSPORT_MARGIN_SECONDS = 15.0


class GuestError(VMError):
    """The guest daemon could not be reached or refused the request."""


class GuestOperationError(GuestError):
    """The daemon ran the request and reported a failure."""


class GuestClient:
    """Talks to one agent's daemon through its loopback port forward.

    Each call uses its own connection, so nothing breaks across VM restarts.
    """

    def __init__(self, port: int, secret: str, host: str = LOOPBACK) -> None:
        self._url = f"ws://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {secret}"}

    async def call(
        self, op: str, args: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout + TRANSPORT_MARGIN_SECONDS):
                async with connect(
                    self._url,
                    additional_headers=self._headers,
                    max_size=MAX_REPLY_BYTES,
                    open_timeout=10,
                    proxy=None,
                ) as connection:
                    await connection.send(json.dumps({"id": 1, "op": op, "args": args or {}}))
                    reply = json.loads(await connection.recv())
        except InvalidStatus as exc:
            raise GuestError(f"guest daemon rejected the connection: {exc}") from exc
        except TimeoutError as exc:
            raise GuestError(f"guest daemon did not answer {op} in time") from exc
        except (OSError, WebSocketException, json.JSONDecodeError) as exc:
            raise GuestError(f"guest daemon unavailable: {exc}") from exc
        if not reply.get("ok"):
            raise GuestOperationError(str(reply.get("error", "operation failed")))
        return reply["result"]

    async def is_ready(self) -> bool:
        """True once the daemon answers an authenticated ping.

        The forwarded port accepts TCP connections before the guest has booted,
        so only a completed round trip counts.
        """
        try:
            await self.call("ping", timeout=3.0)
        except GuestError:
            return False
        return True

    async def wait_ready(self, timeout: float = 90.0, interval: float = 1.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not await self.is_ready():
            if asyncio.get_running_loop().time() >= deadline:
                raise GuestError(f"guest daemon did not become ready within {timeout:.0f}s")
            await asyncio.sleep(interval)
        log.info("guest connected", extra={"url": self._url})
