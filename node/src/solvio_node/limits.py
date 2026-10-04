"""Resource limits: bounded concurrency, bounded wait-queue, coarse rate limit,
request-size cap. On overload the node returns RESOURCE_BUSY instead of trying to
serve unbounded work and taking the host down.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from solvio_node.config import Limits


class BusyError(Exception):
    """Overload — map to ErrorCode.RESOURCE_BUSY."""


class RequestTooLarge(Exception):
    """Body exceeds max_request_bytes — map to ErrorCode.PAYLOAD_TOO_LARGE."""


class LimitGuard:
    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self._sem = asyncio.Semaphore(limits.max_concurrency)
        self._waiting = 0
        self._win_start = 0.0
        self._win_count = 0
        self._lock = asyncio.Lock()

    def check_size(self, nbytes: int) -> None:
        if nbytes > self.limits.max_request_bytes:
            raise RequestTooLarge()

    async def _rate_ok(self) -> bool:
        now = time.monotonic()
        async with self._lock:
            if now - self._win_start >= 60.0:
                self._win_start = now
                self._win_count = 0
            if self._win_count >= self.limits.rate_limit_per_min:
                return False
            self._win_count += 1
            return True

    @asynccontextmanager
    async def slot(self):
        """Acquire one execution slot or raise BusyError (rate/queue/concurrency)."""
        if not await self._rate_ok():
            raise BusyError("rate_limited")
        # Nur ablehnen, wenn KEIN Slot frei ist (Semaphor voll) UND die Warteschlange
        # bereits voll. Ein freier Slot wird immer sofort bedient (kein Queueing).
        if self._sem.locked() and self._waiting >= self.limits.max_queue:
            raise BusyError("queue_full")
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._sem.release()
