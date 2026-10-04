"""N2: real voice proof, turn gate, task tool/router and temporary ledgers.

Only socket frames, device signing and the model transport are supplied by the
test. No live audio, provider, production store or external action is used.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import base64
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from aiohttp import web
import mobile_attest_helper as H
from test_dashboard_task_approval import _wire
from solvio import voice_endpoint as VE, voice_session_proof as VSP
from solvio.agent_runtime import store as S
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities.invocation import CapabilityInvocationGate, voice_trust
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.router import CapabilityRouter
from solvio.security.mobile_approval import app_attest as AA
from solvio.tools.agent_capability_tools import AgentCapabilityTool

TASK = {"objective": "Vergleiche drei Hotels in Hamburg mit belastbaren Quellen."}


class ProofSocket:
    def __init__(self, device, *, invalid=False):
        self.device, self.invalid = device, invalid
        self.closed = False
        self.challenge = None

    async def send_str(self, raw):
        self.challenge = json.loads(raw)

    async def receive(self):
        ch = self.challenge
        raw = VSP.canonical_bytes(VSP.build_binding(core_instance_id=ch["core_instance_id"],
            device_id=self.device.device_id, session_nonce=ch["session_nonce"]))
        assertion = AA.fake_assertion(self.device.aakey, VSP.client_data_hash(raw), 1)
        if self.invalid:
            assertion = b"not a signed assertion"
        return SimpleNamespace(type=web.WSMsgType.TEXT, data=json.dumps({
            "type": "session_assertion", "session_nonce": ch["session_nonce"],
            "assertion": base64.b64encode(assertion).decode()}))


@asynccontextmanager
async def world(*, invalid=False):
    with tempfile.TemporaryDirectory(prefix="solvio-app-voice-task-") as path:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": path}):
            store, cp, co, _ = await _wire(path)
            device = await H.enroll_attested(cp, device_id="iphone-task-device")
            ledger = S.AgentRunLedger(os.path.join(path, "agent.sqlite3"))
            router = CapabilityRouter(mobile=CapabilityApprovals(co, owner_principal="local-owner"))
            orch = Orchestrator(ledger=ledger, router=router, control_plane=cp)
            caps = AgentCapabilities(orch)
            router.register(SPECS["agent_task_research"], caps.research)
            router.register(SPECS["agent_task_build"], caps.build)
            gate = CapabilityInvocationGate()
            socket = ProofSocket(device, invalid=invalid)
            session = SimpleNamespace(session_id="voice-session-one", app_task_session=None)
            request = SimpleNamespace(app={"control_plane": cp})
            proven = await VE._session_proof(request, socket, device.device_id, session=session)
            w = SimpleNamespace(store=store, cp=cp, co=co, device=device, ledger=ledger,
                router=router, orch=orch, gate=gate, socket=socket, session=session,
                proven=proven, request=request)
            try:
                yield w
            finally:
                socket.closed = True
                session.app_task_session = None
                await store.close()


def begin(w, *, turn="turn-one", session_id=None, proof=None, origin=None, user_text=None):
    w.gate.begin_turn(session_id=session_id or w.session.session_id, turn_id=turn,
        principal=VE.principal_for(w.device.device_id), trust=voice_trust(True),
        user_text=user_text or TASK["objective"],
        origin=origin or OriginClass.TRUSTED_INTERACTIVE_APP,
        app_task_session=proof if proof is not None else w.session.app_task_session)


async def t_real_voice_proof_to_tool_creates_one_bound_task_and_replays_same_turn():
    async with world() as w:
        require(w.proven)
        begin(w)
        require_equal(w.gate.context().principal, "local-owner")
        tool = AgentCapabilityTool("agent_task_research", w.router, w.gate, w.ledger)
        results = await asyncio.gather(tool.run(TASK), tool.run(TASK))
        require(all(r.success for r in results))
        require_equal(results[0].data["task_id"], results[1].data["task_id"])
        task = w.ledger.get_task(results[0].data["task_id"])
        grant = w.orch.task_authority.for_run(results[0].data["run_id"])
        require_equal(task.created_principal, "local-owner")
        require_equal(task.created_origin, "trusted_interactive_app")
        require_equal(task.objective, TASK["objective"])
        require_equal(grant.receipt_method, "app_session")
        require(grant.receipt_reference.startswith("app-voice:"))
        require_equal(w.orch.costs.view(task.task_id)["ask_threshold_cents"], 1000)
        require_equal(await w.store.list_pending(), [])
        begin(w, turn="turn-two")
        next_result = await tool.run(TASK)
        require(next_result.success)
        require(next_result.data["task_id"] != task.task_id)


async def t_bad_assertion_and_boolean_origin_never_create_a_task_grant():
    async with world(invalid=True) as w:
        require(not w.proven)
        require_equal(w.session.app_task_session, None)
        for proof in (True, {"principal": "local-owner", "interactive_proof": True}, object()):
            begin(w, proof=proof)
            require_equal(await w.gate.authorize_task_start("agent_task_research", TASK), None)
            result = await AgentCapabilityTool("agent_task_research", w.router, w.gate, w.ledger).run(TASK)
            require(not result.success)
            require_equal(w.ledger.recent_runs(), [])


async def t_closed_stopped_revoked_reenrolled_or_foreign_core_session_cannot_start():
    for change in ("closed", "closing", "stopping", "revoked", "reenrolled", "core"):
        async with world() as w:
            begin(w)
            if change == "closed":
                w.socket.closed = True
            elif change in {"closing", "stopping"}:
                setattr(w.session, "_" + change, True)
            elif change == "revoked":
                await w.cp.revoke_device(w.device.device_id)
            elif change == "reenrolled":
                await H.enroll_attested(w.cp, device_id=w.device.device_id)
            else:
                w.cp.core_instance_id = "another-core"
            result = await AgentCapabilityTool("agent_task_research", w.router, w.gate, w.ledger).run(TASK)
            require(not result.success, change)
            require_equal(w.ledger.recent_runs(), [], change)


async def t_bound_turn_session_task_and_noncommand_are_not_interchangeable():
    async with world() as w:
        begin(w)
        first_context = w.gate.context()
        first = await w.gate.authorize_task_start("agent_task_research", TASK)
        require(first is not None)
        second = await w.gate.authorize_task_start("agent_task_research", dict(TASK, objective="Pruefe ein anderes konkretes Forschungsthema."))
        require(first.request_id != second.request_id)
        require_equal(await w.gate.authorize_task_start("note_write", {"text": "not authorized"}), None)
        begin(w, turn="turn-two")
        require_equal(await w.gate.authorize_task_start("agent_task_research", TASK, context=first_context), None)
        begin(w, session_id="other-voice-session")
        require_equal(await w.gate.authorize_task_start("agent_task_research", TASK), None)
        begin(w, user_text="Was ist aus meinem alten Auftrag geworden?")
        require_equal(await w.gate.authorize_task_start("agent_task_research", TASK), None)
        begin(w, origin=OriginClass.ROOM_VOICE)
        require_equal(await w.gate.authorize_task_start("agent_task_research", TASK), None)
        require_equal(w.gate.context().principal, VE.principal_for(w.device.device_id))


async def t_turn_replaced_while_device_lookup_waits_cannot_mint_a_receipt():
    async with world() as w:
        from solvio import voice_task_session as VTS
        begin(w)
        original = VTS.device_generation
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        with patch.object(VTS, "device_generation", delayed):
            pending = asyncio.create_task(w.gate.authorize_task_start("agent_task_research", TASK))
            await entered.wait()
            begin(w, turn="turn-two")
            release.set()
            require_equal(await pending, None)


async def t_app_voice_task_rechecks_turn_end_and_revocation_after_router_await():
    for change in ("unchanged", "correction", "end", "revocation"):
        async with world() as w:
            require(w.proven)
            begin(w)
            entered, release = asyncio.Event(), asyncio.Event()
            original = w.router._classify_and_decide

            async def delayed(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            with patch.object(w.router, "_classify_and_decide", delayed):
                pending = asyncio.create_task(AgentCapabilityTool(
                    "agent_task_research", w.router, w.gate, w.ledger).run(TASK))
                await asyncio.wait_for(entered.wait(), 1)
                if change == "correction":
                    begin(w, turn="corrected-turn", user_text="Nein, noch nicht starten.")
                elif change == "end":
                    w.socket.closed = True
                elif change == "revocation":
                    await w.cp.revoke_device(w.device.device_id)
                release.set()
                result = await asyncio.wait_for(pending, 1)
            require_equal(result.success, change == "unchanged", change)
            require_equal(len(w.ledger.recent_runs()), int(change == "unchanged"), change)
            require_equal(await w.store.list_pending(), [], change)
            if change == "unchanged":
                run = w.ledger.recent_runs()[0]
                grant = w.orch.task_authority.for_run(run.run_id)
                require_equal(grant.receipt_method, "app_session")
                w.socket.closed = True
                w.gate.clear()
                require(w.orch.task_authority.active(grant.reference,
                    task_id=run.task_id, run_id=run.run_id).allowed)


async def t_app_voice_dispatch_guard_binds_exact_start_and_cannot_be_removed():
    async with world() as w:
        begin(w)
        start = await w.gate.authorize_task_start("agent_task_research", TASK)
        require(start is not None and await start.dispatch_current())
        variants = (
            replace(start, app_voice_authorization=None),
            replace(start, app_voice_authorization=True),
            replace(start, app_voice_authorization=replace(start.app_voice_authorization, _seal=None)),
            replace(start, browser_voice_authorization=object()),
            replace(start, request_id="different-exact-request"),
            replace(start, capability="agent_task_build"),
            replace(start, arguments_digest="0" * 64),
            replace(start, receipt=replace(start.receipt, method="dashboard_session")),
            replace(start, receipt=replace(start.receipt, authorizer="foreign-owner")),
            replace(start, receipt=replace(start.receipt, reference="browser-voice:" + "0" * 64)),
        )
        for variant in variants:
            require(not await variant.dispatch_current())
        w.gate.clear()
        require(not await start.dispatch_current())
        require_equal(w.ledger.recent_runs(), [])


async def t_app_voice_arguments_cannot_change_during_authorization_lookup():
    async with world() as w:
        from solvio import voice_task_session as VTS
        begin(w)
        args = dict(TASK)
        entered, release = asyncio.Event(), asyncio.Event()
        original = VTS.device_generation

        async def delayed(*values):
            entered.set()
            await release.wait()
            return await original(*values)

        with patch.object(VTS, "device_generation", delayed):
            pending = asyncio.create_task(w.gate.authorize_task_start("agent_task_research", args))
            await asyncio.wait_for(entered.wait(), 1)
            args["objective"] = "Ein anderer Auftrag, der nicht gebunden war."
            release.set()
            require_equal(await asyncio.wait_for(pending, 1), None)
        require_equal(w.ledger.recent_runs(), [])


async def t_changed_device_generation_during_real_proof_does_not_mint_task_session():
    async with world() as w:
        fresh = SimpleNamespace(session_id="voice-session-next", app_task_session=None)
        original = VSP.verify_session_proof

        async def re_enroll(*args, **kwargs):
            result = await original(*args, **kwargs)
            await H.enroll_attested(w.cp, device_id=w.device.device_id)
            return result

        with patch.object(VSP, "verify_session_proof", re_enroll):
            await VE._session_proof(w.request, ProofSocket(w.device), w.device.device_id, session=fresh)
        require_equal(fresh.app_task_session, None)


async def t_real_session_tool_loop_delivers_proved_session_and_canonical_principal_to_gate():
    async with world() as w:
        from solvio.realtime.core_server import Session
        sent = []

        async def noop():
            pass

        async def send(raw):
            sent.append(json.loads(raw))

        # Exercise the actual Session, including shared provider hooks. The
        # authenticated proof remains the one issued by this fixture.
        original = w.session
        session = Session(SimpleNamespace(dispatcher=SimpleNamespace(capability_gate=w.gate)),
                          SimpleNamespace(send=send))
        session.__dict__.update(vars(original))
        w.session = session
        session.satellite_id = VE.principal_for(w.device.device_id)
        session.channel = "voice_iphone"
        session.interactive_proof = w.proven
        session.turn_user_text = TASK["objective"]
        session.conversation_id = ""
        session.touch = lambda: None
        session._await_turn_text = noop
        session._offer_shadow = lambda **kwargs: None
        session.oa = SimpleNamespace(send=send)
        await Session._handle_tool_calls(session, [], turn_id="actual-core-turn")
        context = w.gate.context()
        require_equal(context.principal, "local-owner")
        require_equal(context.turn_id, "actual-core-turn")
        require(context.app_task_session is w.session.app_task_session)
        require(await w.gate.authorize_task_start("agent_task_research", TASK) is not None)


async def t_cognitive_dispatch_uses_the_same_turn_receipt_and_cannot_start_after_revocation():
    async with world() as w:
        from solvio.cognition.router import CognitiveRouter
        from solvio.cognition.continuity import ContinuityView
        from solvio.cognition.ledger import CognitionLedger, new_decision_id
        from solvio.cognition.types import Route, RoutingDecision, TaskAssessment
        begin(w)
        dispatcher = SimpleNamespace(capabilities=w.router, capability_gate=w.gate,
                                     agent_runtime=w.orch)
        ledger = CognitionLedger(os.path.join(os.path.dirname(w.ledger.path), "cognition.sqlite3"))
        router = CognitiveRouter(dispatcher, mode="active", ledger=ledger)
        assessment = TaskAssessment(Route.AGENT_RESEARCH, TASK["objective"])
        row = RoutingDecision(new_decision_id(), time.time())
        result = await router._dispatch(row, time.time(), assessment, ContinuityView(), w.gate.context())
        require(result.ok)
        run_id = result.data["run_id"]
        require_equal(w.orch.task_authority.for_run(run_id).receipt_method, "app_session")
        require_equal(w.ledger.get_task(result.data["task_id"]).created_principal, "local-owner")
        await w.cp.revoke_device(w.device.device_id)
        begin(w, turn="turn-two")
        row = RoutingDecision(new_decision_id(), time.time())
        refused = await router._dispatch(row, time.time(), assessment, ContinuityView(), w.gate.context())
        require(not refused.ok)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_authenticated_voice_build_binds_the_existing_workspace_target_without_creating_it():
    async with world() as w:
        args = {"objective": "Verbessere die Testausgabe in diesem Projekt.",
                "repository": "/temporary/explicit/project"}
        begin(w, user_text=args["objective"] + " " + args["repository"])
        with patch.object(w.orch, "_build_available", return_value=True):
            result = await AgentCapabilityTool("agent_task_build", w.router, w.gate, w.ledger).run(args)
        require(result.success)
        task = w.ledger.get_task(result.data["task_id"])
        require_equal(task.scope, "build")
        require_equal(task.target_repo, args["repository"])
        require_equal(w.ledger.get_run(result.data["run_id"]).workspace_path, "")
        require_equal(w.orch.task_authority.for_run(result.data["run_id"]).receipt_method, "app_session")


async def t_room_voice_without_session_proof_still_parks_the_same_approval_path():
    async with world(invalid=True) as w:
        begin(w, origin=OriginClass.ROOM_VOICE)
        result = await AgentCapabilityTool("agent_task_research", w.router, w.gate, w.ledger).run(TASK)
        require(not result.success)
        require(result.error.startswith("approval_required"))
        require_equal(w.ledger.recent_runs(), [])
        pending = await w.store.list_pending()
        require_equal(len(pending), 1)
        require_equal(pending[0]["principal"], "local-owner")
        require_equal(pending[0]["tool"], "agent_task_research")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
