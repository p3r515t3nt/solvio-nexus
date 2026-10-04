"""Core-only task authority from an actually proved iPhone voice session.

The WebSocket proof owner constructs this value only after the existing App
Attest assertion succeeds for the same device generation. A channel name, a
boolean, or a model argument cannot substitute for it. It lives exactly as long
as that connection; durable task authority is issued by the existing task start
service after an authenticated turn commissions an exact task.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Callable

_SEAL = object()


async def device_generation(control_plane, device_id: str) -> tuple | None:
    """Read current device authority through its existing transaction predicate."""
    store = control_plane.store

    def read():
        conn = store._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            dev = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
            binding = None
            if dev is not None:
                reason, _, fingerprint, key_id = store._execution_authority(
                    {"principal": dev["principal"]}, dev, control_plane.allowed_environments)
                if (reason is None and dev["principal"] and
                        dev["app_id"] == control_plane.app_id and dev["app_attest_public_key"]):
                    binding = (dev["principal"], dev["device_id"], dev["current_enrollment_id"],
                               fingerprint, key_id, dev["app_attest_public_key"], dev["environment"])
            conn.execute("COMMIT")
            return binding
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    try:
        return await store._run(read)
    except Exception:
        return None


@dataclass(frozen=True)
class VerifiedAppTaskSession:
    principal: str
    device_id: str
    core_instance_id: str
    session_id: str
    session_nonce: str
    device_generation: tuple = field(repr=False)
    control_plane: Any = field(repr=False, compare=False)
    alive: Callable[[], bool] = field(repr=False, compare=False)

    async def current(self) -> bool:
        if (not self.alive() or not self.session_id or not self.session_nonce or
                self.control_plane.core_instance_id != self.core_instance_id):
            return False
        actual = await device_generation(self.control_plane, self.device_id)
        return (self.alive() and self.control_plane.core_instance_id == self.core_instance_id
                and actual is not None and actual == self.device_generation
                and actual[0] == self.principal)

    async def authorize(self, context, capability: str, arguments: dict, *, turn_current,
                        private_data=False, conversation_current=None):
        from solvio.agent_runtime.task_authority import VerifiedTaskReceipt
        from solvio.agent_runtime.task_start_service import AuthorizedTaskStart, TASK_STARTS
        from solvio.capabilities.policy import OriginClass
        from solvio.capabilities.invocation import task_is_commissioned

        if (capability not in TASK_STARTS or context.origin is not OriginClass.TRUSTED_INTERACTIVE_APP
                or context.principal != self.principal or context.session_id != self.session_id
                or not context.turn_id or not task_is_commissioned(context, capability, arguments)
                or not callable(turn_current)
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
            binding = json.dumps({"core": self.core_instance_id, "device": self.device_id,
                "nonce": self.session_nonce, "session": self.session_id, "turn": context.turn_id,
                "capability": capability, "arguments": json.loads(raw), **private}, sort_keys=True,
                ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            digest = hashlib.sha256(b"SOLVIO_APP_VOICE_TASK_V1\0" + binding).hexdigest()
            receipt = VerifiedTaskReceipt("app_session", "app-voice:" + digest, self.principal)
            start = AuthorizedTaskStart.bind(receipt=receipt, request_id="voice-" + digest,
                capability=capability, arguments=json.loads(raw), **private)
            guard = AppVoiceTaskAuthorization(self, start.request_id, start.capability,
                start.arguments_digest, start.receipt.reference, turn_current, _SEAL,
                conversation_current if private_data else None)
            return replace(start, app_voice_authorization=guard)
        except (ValueError, TypeError, UnicodeError):
            return None


@dataclass(frozen=True)
class AppVoiceTaskAuthorization:
    """One pending task; rechecked after router awaits, before durable admission.

    The final caller performs no await between this check and Task/Grant commit.
    Ending a conversation after that commit leaves the admitted task intact.
    """
    session: VerifiedAppTaskSession = field(repr=False)
    request_id: str
    capability: str
    arguments_digest: str
    receipt_reference: str
    turn_current: Callable[[], bool] = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False, default=None)
    conversation_current: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    async def current(self, start) -> bool:
        if (self._seal is not _SEAL or type(self.session) is not VerifiedAppTaskSession
                or start.request_id != self.request_id or start.capability != self.capability
                or start.arguments_digest != self.arguments_digest
                or start.receipt.reference != self.receipt_reference
                or start.receipt.method != "app_session"
                or start.receipt.authorizer != self.session.principal or not self.turn_current()):
            return False
        return (await self.session.current() and self.turn_current()
                and (not start.private_data or callable(self.conversation_current)
                     and self.conversation_current()))
