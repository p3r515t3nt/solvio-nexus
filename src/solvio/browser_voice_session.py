"""Core-only authority from the consumed, Owner-bound browser voice handshake.

No cookie or CSRF secret is retained. The existing approval store owns browser
session identity and revocation; this value only binds one live voice connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Callable

from solvio.security.mobile_approval.browser_sessions import BrowserActor, BrowserSessionService

_SEAL = object()


async def browser_generation(service, session_id: str, principal: str) -> str | None:
    """Read the same principal/core/expiry/revocation predicate as dashboard claims."""
    if type(service) is not BrowserSessionService:
        return None
    core_id = service.core_instance_id

    def read():
        service._connection()
        row, reason = service.store._browser_session(session_id, principal, core_id)
        return service.store._browser_binding(row) if reason is None else None

    try:
        result = await service.store._run(read)
        return result if service.core_instance_id == core_id else None
    except Exception:
        return None


@dataclass(frozen=True)
class VerifiedBrowserTaskSession:
    principal: str
    browser_session_id: str
    core_instance_id: str
    session_id: str
    session_nonce: str = field(repr=False)
    generation: str = field(repr=False)
    service: BrowserSessionService = field(repr=False, compare=False)
    alive: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False, default=None)

    def live(self) -> bool:
        return (self._seal is _SEAL and self.alive()
                and self.service.core_instance_id == self.core_instance_id)

    async def authority_current(self) -> bool:
        # Normal voice close does not cancel already admitted learning. Logout
        # and browser expiry still cancel its original source before any work.
        if self._seal is not _SEAL or self.service.core_instance_id != self.core_instance_id:
            return False
        actual = await browser_generation(self.service, self.browser_session_id, self.principal)
        return (actual is not None and actual == self.generation
                and self.service.core_instance_id == self.core_instance_id)

    async def current(self) -> bool:
        return self.live() and await self.authority_current() and self.live()

    @property
    def observation_reference(self) -> str:
        # The cost ledger retains a source identity, never the consumed socket
        # nonce or browser credential. Domain separation keeps task IDs distinct.
        raw = json.dumps([self.core_instance_id, self.browser_session_id, self.principal,
                          self.session_id, self.session_nonce, self.generation],
                         separators=(",", ":")).encode("utf-8")
        return "browser-voice:" + hashlib.sha256(b"SOLVIO_BROWSER_VOICE_OBSERVATION_V1\0" + raw).hexdigest()

    async def authorize(self, context, capability: str, arguments: dict, *, turn_current,
                        private_data=False, conversation_current=None):
        from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
        from solvio.agent_runtime.task_start_service import AuthorizedTaskStart, TASK_STARTS
        from solvio.capabilities.policy import OriginClass
        from solvio.capabilities.invocation import task_is_commissioned
        if (capability not in TASK_STARTS or context.origin is not OriginClass.TRUSTED_DASHBOARD
                or context.principal != self.principal or context.session_id != self.session_id
                or not context.turn_id or not task_is_commissioned(context, capability, arguments)
                or not turn_current()):
            return None
        if type(private_data) is not bool or (private_data and (capability != "agent_task_task"
                or not context.conversation_id or not callable(conversation_current)
                or not conversation_current())):
            return None
        private = {"conversation_ref": context.conversation_id, "private_data": True} if private_data else {}
        if capability == "agent_task_research" and context.conversation_id:
            private = {"conversation_ref": context.conversation_id}
        try:
            raw = json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False)
            if (not await self.current() or not turn_current()
                    or private_data and not conversation_current()):
                return None
            if raw != json.dumps(arguments, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False):
                return None
            binding = json.dumps({"core": self.core_instance_id,
                "browser": self.browser_session_id, "principal": self.principal,
                "nonce": self.session_nonce, "session": self.session_id,
                "turn": context.turn_id, "capability": capability, "arguments": json.loads(raw), **private},
                sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest = hashlib.sha256(b"SOLVIO_BROWSER_VOICE_TASK_V1\0" + binding).hexdigest()
            receipt = VerifiedTaskReceipt("dashboard_session", "browser-voice:" + digest, self.principal)
            start = AuthorizedTaskStart.bind(receipt=receipt, request_id="voice-" + digest,
                capability=capability, arguments=json.loads(raw), **private)
            from dataclasses import replace
            guard = BrowserVoiceTaskAuthorization(self, start.request_id, start.capability,
                start.arguments_digest, start.receipt.reference, turn_current, _SEAL,
                conversation_current if private_data else None)
            return replace(start, browser_voice_authorization=guard)
        except (TypeError, ValueError, UnicodeError):
            return None


@dataclass(frozen=True)
class BrowserVoiceTaskAuthorization:
    """Exact pending start; checked again after routing awaits, before persistence."""
    session: VerifiedBrowserTaskSession = field(repr=False)
    request_id: str
    capability: str
    arguments_digest: str
    receipt_reference: str
    turn_current: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False, default=None)
    conversation_current: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    async def current(self, start) -> bool:
        if (self._seal is not _SEAL or type(self.session) is not VerifiedBrowserTaskSession
                or start.request_id != self.request_id or start.capability != self.capability
                or start.arguments_digest != self.arguments_digest
                or start.receipt.reference != self.receipt_reference
                or start.receipt.method != "dashboard_session"
                or start.receipt.authorizer != self.session.principal or not self.turn_current()):
            return False
        return (await self.session.current() and self.turn_current()
                and (not start.private_data or callable(self.conversation_current)
                     and self.conversation_current()))


async def verified_browser_task_session(*, actor, service, session_id, session_nonce, alive):
    """Called only after the endpoint consumed its exact authenticated nonce."""
    if (type(actor) is not BrowserActor or type(service) is not BrowserSessionService
            or not isinstance(session_id, str) or not session_id or len(session_id) > 200
            or not isinstance(session_nonce, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_nonce) or not callable(alive)
            or not alive()):
        return None
    core_id = service.core_instance_id
    generation = await browser_generation(service, actor.session_id, actor.principal)
    if not generation or not alive() or service.core_instance_id != core_id:
        return None
    return VerifiedBrowserTaskSession(actor.principal, actor.session_id, core_id,
        session_id, session_nonce, generation, service, alive, _SEAL)
