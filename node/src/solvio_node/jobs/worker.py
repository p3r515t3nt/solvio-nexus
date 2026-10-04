"""Bounded background worker pool.

At most `workers` concurrent executions (default 2); the store enforces the queue
bound. A worker runs ONLY a registered capability whose `supports_background` is
True — there is no eval/exec/subprocess/shell/dynamic-import and no caller-supplied
code. Read-only capabilities (research.fetch_public_url) are safe under
at-least-once semantics; retries are bounded and classified.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random

from pydantic import ValidationError

from solvio_node.jobs.store import JobStore
from solvio_node.registry import CapabilityRegistry

# Exception type-names treated as transient (bounded retry). Everything else is a
# permanent failure — no retry loop.
_TRANSIENT = {
    "TimeoutError", "DnsResolutionError", "ConnectionError", "ConnectionResetError",
    "OSError", "ClientConnectorError", "ServerDisconnectedError",
    "ClientOSError", "ClientPayloadError",
}


class _Cancelled(Exception):
    pass


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.TimeoutError) or type(exc).__name__ in _TRANSIENT


class JobWorkerPool:
    def __init__(self, store: JobStore, registry: CapabilityRegistry, logger, *,
                 workers: int = 2, poll_interval: float = 1.0,
                 base_backoff: float = 0.5, max_backoff: float = 5.0,
                 cleanup_interval: float = 3600.0,
                 rng: random.Random | None = None) -> None:
        self.store = store
        self.registry = registry
        self.log = logger
        self.workers = workers
        self.poll_interval = poll_interval
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.cleanup_interval = cleanup_interval
        self._rng = rng or random.Random()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        recovered = await self.store.recover()
        await self.store.cleanup_expired()
        if self.log is not None and any(recovered.values()):
            self.log.event("job_recovery", count=sum(recovered.values()))
        self._running = True
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker_loop(i)))
        self._tasks.append(asyncio.create_task(self._reaper_loop()))

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except BaseException:  # noqa: BLE001
                pass
        self._tasks.clear()

    async def _idle_wait(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            pass

    async def _worker_loop(self, idx: int) -> None:
        while self._running:
            try:
                claimed = await self.store.claim_next()
            except Exception:  # noqa: BLE001 - store hiccup, back off briefly
                await self._idle_wait()
                continue
            if claimed is None:
                await self._idle_wait()
                continue
            await self._execute(claimed)

    async def _reaper_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self.store.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                pass

    async def _execute(self, claimed: dict) -> None:
        job_id = claimed["job_id"]
        cap_id = claimed["capability_id"]
        attempt = claimed["attempt_count"]
        max_attempts = claimed["max_attempts"]

        cap = self.registry.get(cap_id)
        if cap is None or not getattr(cap, "supports_background", False):
            await self.store.finish_failed(job_id, "UNSUPPORTED_CAPABILITY")
            self._log_done(cap_id, "FAILED", attempt)
            return

        if await self.store.is_cancel_requested(job_id):
            await self.store.finish_cancelled(job_id)
            self._log_done(cap_id, "CANCELLED", attempt)
            return

        try:
            data = cap.input_model.model_validate(claimed["payload"])
        except ValidationError:
            await self.store.finish_failed(job_id, "VALIDATION_ERROR")
            self._log_done(cap_id, "FAILED", attempt)
            return

        try:
            out = await self._run_with_cancel(job_id, cap, data)
            result = cap.output_model.model_validate(out).model_dump(mode="json")
        except _Cancelled:
            await self.store.finish_cancelled(job_id)
            self._log_done(cap_id, "CANCELLED", attempt)
            return
        except asyncio.TimeoutError:
            await self._fail_or_retry(job_id, cap_id, attempt, max_attempts, "TIMEOUT", True)
            return
        except Exception as exc:  # noqa: BLE001 - payload-free classification
            await self._fail_or_retry(job_id, cap_id, attempt, max_attempts,
                                      type(exc).__name__, _is_transient(exc))
            return

        raw = json.dumps(result, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        await self.store.finish_success(job_id, result, digest)
        self._log_done(cap_id, "SUCCEEDED", attempt, result_size=len(raw))

    async def _run_with_cancel(self, job_id: str, cap, data):
        """Await the capability, polling for cooperative cancellation (best-effort)."""
        task = asyncio.ensure_future(
            asyncio.wait_for(cap.invoke(data), timeout=cap.timeout_s))
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if task in done:
                return task.result()
            if await self.store.is_cancel_requested(job_id):
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001
                    pass
                raise _Cancelled()

    async def _fail_or_retry(self, job_id, cap_id, attempt, max_attempts, code, transient):
        if transient and attempt < max_attempts:
            raw = min(self.max_backoff, self.base_backoff * (2 ** max(0, attempt - 1)))
            delay = raw * (0.5 + 0.5 * self._rng.random())  # bounded jitter
            await asyncio.sleep(delay)
            await self.store.requeue(job_id)
            self.notify()
            self._log_done(cap_id, "RETRY", attempt)
        else:
            await self.store.finish_failed(job_id, code)
            self._log_done(cap_id, "FAILED", attempt)

    def _log_done(self, cap_id, status, attempt, result_size=0):
        if self.log is None:
            return
        try:
            self.log.event("job", detail=status, capability=cap_id, count=attempt)
        except Exception:  # noqa: BLE001
            pass
