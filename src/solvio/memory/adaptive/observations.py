"""Verified inputs into the existing adaptive queue and cost ledger.

Only the server constructs sources. A channel label or a model argument is not
identity. Queue entries carry immutable message bindings, not an ambient task
context. The transcript stays in its original store and the bounded queue;
the cost ledger retains only its digest and references.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import TYPE_CHECKING

from solvio.capabilities.policy import OriginClass
from solvio.memory.adaptive.policy import OwnerTurn
from solvio.memory.intent import looks_like_secret

if TYPE_CHECKING:
    from solvio.agent_runtime.cost_subjects import VerifiedInteractionSource


def observation_digest(turn: OwnerTurn, context: str = "") -> str:
    body = {**asdict(turn), "context": context}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(b"SOLVIO_ADAPTIVE_OBSERVATION_V1\0" + encoded).hexdigest()


@dataclass(frozen=True)
class ObservationBinding:
    activity_id: str
    source: VerifiedInteractionSource
    validate: object = None  # optional async check against the original authority
    generation: int = 0


class AdaptiveObservations:
    def __init__(self, adaptive, ledger, *, owner_principal="", quote_adapter=None, settlement_adapter=None):
        # Keep the optional runtime seam lazy: ordinary memory still loads when
        # the agent runtime is disabled or unavailable.
        from solvio.agent_runtime.cost_subjects import ActivityLedger
        from solvio.agent_runtime.task_authority import TaskAuthority
        self.adaptive = adaptive
        self.ledger = ledger
        # The shared personal memory belongs to the configured Core owner, not
        # to every principal that can legitimately authorize its own task.
        self.owner_principal = owner_principal
        self.activities = ActivityLedger(ledger)
        self.activities.hold_interrupted_observations()
        self.authority = TaskAuthority(ledger)
        self.quote_adapter = quote_adapter
        self.settlement_adapter = settlement_adapter
        self._queued: set[str] = set()  # bounded by AdaptiveMemory's existing queue
        adaptive.observation_scope = self._scope

    def offer(self, turn: OwnerTurn, source: VerifiedInteractionSource, *,
              context="", task_id=None, run_id=None, validate=None) -> bool:
        from solvio.agent_runtime.cost_subjects import VerifiedInteractionSource
        if (not self.adaptive.enabled or not turn.is_eligible()[0] or
                looks_like_secret(turn.text) or looks_like_secret(context)):
            return False
        channels = {"app": {"voice_iphone", "chat_iphone"},
                    "dashboard": {"voice_browser", "chat_dashboard"},
                    "task": {"task_iphone", "task_dashboard"}}
        if (type(source) is not VerifiedInteractionSource or
                (source.source_kind == 'task' and
                 (not self.owner_principal or source.principal != self.owner_principal)) or
                turn.channel not in channels.get(source.source_kind, set()) or
                (turn.channel in {'chat_iphone', 'chat_dashboard'} and (context or not callable(validate))) or
                not turn.message_id or not turn.conversation_id or
                (source.conversation_id, source.message_id) != (turn.conversation_id, turn.message_id)):
            return False
        digest = observation_digest(turn, context)
        try:
            bound = self.activities.admit(source, content_digest=digest, task_id=task_id, run_id=run_id)
            self.activities.binding(bound.activity_id, content_digest=digest, source=source)
        except ValueError:
            return False
        if bound.activity_id in self._queued:
            return False
        self._queued.add(bound.activity_id)
        try:
            accepted = self.adaptive.observe_turn(turn, context=context,
                cost_binding=ObservationBinding(bound.activity_id, source, validate,
                                                self.activities.generation(bound.activity_id)))
        except BaseException:
            self._queued.discard(bound.activity_id)
            raise
        if not accepted:
            self._queued.discard(bound.activity_id)
        return accepted

    def resume_chat(self, activity_id, principal, turn, source, *, validate):
        """The chat processor has re-read and checked the original persisted source."""
        if (not self.adaptive.enabled or activity_id in self._queued or
                turn.channel not in {"chat_iphone", "chat_dashboard"} or validate is None):
            raise ValueError('observation_resume_unavailable')
        self.activities.resume_bound_chat(activity_id, principal, source, observation_digest(turn))
        try:
            accepted = self.offer(turn, source, validate=validate)
        except Exception:
            accepted = False
        if not accepted:
            self.activities.hold(activity_id, 'observation_queue_unavailable')
            raise ValueError('observation_queue_unavailable')
        return True

    def offer_task(self, task_id: str, run_id: str) -> bool:
        prepared = self._task_observation(task_id, run_id)
        if prepared is None:
            return False
        turn, source = prepared
        return self.offer(turn, source, task_id=task_id, run_id=run_id)

    def _task_observation(self, task_id, run_id):
        from solvio.agent_runtime.cost_subjects import _verified_source
        task = self.ledger.get_task(task_id)
        grant = self.authority.for_run(run_id)
        if (not task or not self.owner_principal or task.created_principal != self.owner_principal
                or not grant or grant.task_id != task_id or grant.authorizer != task.created_principal):
            return None
        origins = {
            "app_session": (OriginClass.TRUSTED_INTERACTIVE_APP.value, "task_iphone"),
            "dashboard_session": (OriginClass.TRUSTED_DASHBOARD.value, "task_dashboard"),
        }
        expected = origins.get(grant.receipt_method)
        if not expected or task.created_origin != expected[0]:
            return None
        # A spoken app task is already observed as its original voice message.
        # Giving it a synthetic second conversation would duplicate evidence.
        if grant.receipt_reference.startswith(("app-voice:", "browser-voice:")):
            return None
        source = _verified_source(principal=task.created_principal, source_kind="task",
            source_ref=grant.reference, conversation_id="task:" + task_id, message_id=task_id)
        turn = OwnerTurn(text=task.objective, channel=expected[1],
            conversation_id=source.conversation_id, session_id=grant.reference,
            turn_id=task_id, message_id=task_id)
        return turn, source

    def resume_task(self, activity_id, principal):
        if not self.adaptive.enabled or activity_id in self._queued:
            raise ValueError('observation_resume_unavailable')
        row = self.activities.held_task(activity_id, principal)
        prepared = self._task_observation(row['task_id'], row['run_id']) if row else None
        if prepared is None:
            # Voice observations require their original live device proof;
            # there is no invented browser replacement after a restart.
            raise ValueError('observation_resume_unavailable')
        turn, source = prepared
        bound = self.activities.resume_bound_task(activity_id, principal, source, observation_digest(turn))
        try:
            accepted = self.offer(turn, source, task_id=bound.task_id, run_id=bound.run_id)
        except BaseException as exc:
            self.activities.hold(activity_id, 'observation_queue_unavailable')
            if isinstance(exc, Exception):
                raise ValueError('observation_queue_unavailable') from exc
            raise
        if not accepted:
            self.activities.hold(activity_id, 'observation_queue_unavailable')
            raise ValueError('observation_queue_unavailable')
        return True

    def offer_voice(self, turn: OwnerTurn, proof, *, persisted) -> bool:
        from solvio.agent_runtime.cost_subjects import _verified_source
        from solvio.voice_task_session import VerifiedAppTaskSession, device_generation
        from solvio.browser_voice_session import VerifiedBrowserTaskSession
        if type(proof) is VerifiedBrowserTaskSession:
            if (not proof.live() or turn.channel != "voice_browser"
                    or turn.session_id != proof.session_id):
                return False
            source = _verified_source(principal=proof.principal, source_kind="dashboard",
                source_ref=proof.observation_reference,
                conversation_id=turn.conversation_id, message_id=turn.message_id)

            async def valid_browser():
                # Conversation close only stops future admission. Logout or
                # expiry invalidate already queued learning at each N4 boundary.
                if not await proof.authority_current() or not await persisted():
                    return False
                return await proof.authority_current()

            return self.offer(turn, source, validate=valid_browser)
        if (type(proof) is not VerifiedAppTaskSession or not proof.alive() or
                turn.channel != "voice_iphone" or turn.session_id != proof.session_id):
            return False
        source = _verified_source(principal=proof.principal, source_kind="app",
            source_ref="voice:" + proof.core_instance_id + ":" + proof.session_nonce,
            conversation_id=turn.conversation_id, message_id=turn.message_id)

        async def valid():
            # Normal conversation end permits the already admitted, bounded
            # observation. Revocation/re-enrollment does not.
            actual = await device_generation(proof.control_plane, proof.device_id)
            return (actual is not None and actual == proof.device_generation
                and actual[0] == proof.principal
                and proof.control_plane.core_instance_id == proof.core_instance_id
                and await persisted())

        return self.offer(turn, source, validate=valid)

    @asynccontextmanager
    async def _scope(self, binding, turn, context):
        from solvio.agent_runtime.cost_dispatch import interaction_cost_scope
        if type(binding) is not ObservationBinding:
            raise ValueError("observation_binding_required")
        digest = observation_digest(turn, context)

        async def check():
            if not self.adaptive.enabled:
                self.activities.hold(binding.activity_id, "memory_disabled")
                raise ValueError("memory_disabled")
            if (binding.source.source_kind == 'task' and
                    (not self.owner_principal or binding.source.principal != self.owner_principal)):
                self.activities.finish(binding.activity_id, cancelled=True)
                raise ValueError("observation_source_inactive")
            if binding.validate is not None and not await binding.validate():
                self.activities.finish(binding.activity_id, cancelled=True)
                raise ValueError("observation_source_inactive")
            self.activities.binding(binding.activity_id, content_digest=digest, source=binding.source)
            if self.activities.generation(binding.activity_id) != binding.generation:
                raise ValueError("observation_generation_changed")

        try:
            await check()
            with interaction_cost_scope(self.ledger, activity_id=binding.activity_id, content_digest=digest,
                    quote_adapter=self.quote_adapter, settlement_adapter=self.settlement_adapter) as scope:
                scope.source_check = check
                try:
                    # The pipeline calls this again after the provider await
                    # and before each canonical mutation.
                    yield check
                except (Exception, asyncio.CancelledError):
                    self.activities.hold(binding.activity_id, "extractor_interrupted")
                    raise
            status = getattr(self.adaptive.extractor, "last_status", {})
            if getattr(check, 'processing_completed', False) or status.get("state") == "completed":
                self.activities.finish(binding.activity_id)
            elif status.get("state") == "unavailable":
                self.activities.hold(binding.activity_id, status.get("reason") or "extractor_failed")
        finally:
            self._queued.discard(binding.activity_id)
