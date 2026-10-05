"""Pending approvals: lets a paused run wait for the user's answer."""

from __future__ import annotations

import asyncio


class ApprovalBroker:
    """In-memory rendezvous between a waiting run and the approval endpoint.

    The approval itself is stored in the database; this only carries the answer
    to the coroutine that is waiting for it.
    """

    def __init__(self) -> None:
        self._waiting: dict[int, asyncio.Future[bool]] = {}

    async def wait(self, approval_id: int) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiting[approval_id] = future
        try:
            return await future
        finally:
            self._waiting.pop(approval_id, None)

    def resolve(self, approval_id: int, approved: bool) -> bool:
        """Deliver the answer. Returns False if nothing is waiting for it any more."""
        future = self._waiting.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True
