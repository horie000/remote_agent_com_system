from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalTicket:
    code: str
    request_id: str
    method: str
    item_id: str
    thread_id: str
    turn_id: str
    guild_id: int
    channel_id: int
    user_id: int
    title: str
    detail: str
    created_at: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    _answer: asyncio.Future[bool] | None = field(default=None, repr=False)


class ApprovalManager:
    """One-use, origin-bound Discord approvals with a deny-by-default timeout."""

    def __init__(self) -> None:
        self._pending: dict[str, ApprovalTicket] = {}

    def create(
        self,
        request_id: Any,
        method: str,
        item_id: str,
        thread_id: str,
        turn_id: str,
        guild_id: int,
        channel_id: int,
        user_id: int,
        title: str,
        detail: str,
        timeout: int,
    ) -> ApprovalTicket:
        loop = asyncio.get_running_loop()
        code = secrets.token_hex(3).upper()
        while code in self._pending:
            code = secrets.token_hex(3).upper()
        ticket = ApprovalTicket(
            code=code,
            request_id=str(request_id),
            method=method,
            item_id=item_id,
            thread_id=thread_id,
            turn_id=turn_id,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            title=title,
            detail=detail,
            deadline=time.monotonic() + timeout,
            _answer=loop.create_future(),
        )
        self._pending[code] = ticket
        return ticket

    async def wait(self, ticket: ApprovalTicket, timeout: int) -> bool:
        try:
            assert ticket._answer is not None
            remaining = min(timeout, max(0.0, ticket.deadline - time.monotonic()))
            if remaining <= 0:
                return False
            return await asyncio.wait_for(ticket._answer, timeout=remaining)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending.pop(ticket.code, None)

    def resolve(
        self,
        code: str,
        approve: bool,
        guild_id: int,
        channel_id: int,
        user_id: int,
    ) -> bool:
        ticket = self._pending.get(code.upper())
        if ticket is None:
            return False
        if time.monotonic() >= ticket.deadline:
            self._pending.pop(ticket.code, None)
            if ticket._answer is not None and not ticket._answer.done():
                ticket._answer.set_result(False)
            return False
        if (ticket.guild_id, ticket.channel_id, ticket.user_id) != (guild_id, channel_id, user_id):
            return False
        self._pending.pop(ticket.code, None)
        assert ticket._answer is not None
        if not ticket._answer.done():
            ticket._answer.set_result(bool(approve))
            return True
        return False

    def deny_all(self) -> None:
        for ticket in list(self._pending.values()):
            self._pending.pop(ticket.code, None)
            if ticket._answer is not None and not ticket._answer.done():
                ticket._answer.set_result(False)

    def get(self, code: str) -> ApprovalTicket | None:
        return self._pending.get(code.upper())
