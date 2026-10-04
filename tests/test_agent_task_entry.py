"""N2: echte HTTPS-Eingaenge, Router, Freigabe- und Agentenbuecher.

Alle Speicher und Zertifikate sind temporaer. Nur das iPhone ist synthetisch;
kein Anbieter, kein produktiver Core und kein echtes Ziel werden aufgerufen.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import os
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

from aiohttp import CookieJar, TCPConnector
from aiohttp.test_utils import TestClient, TestServer, unused_port
import mobile_attest_helper as H
from test_browser_sessions import _tls
from test_dashboard_task_approval import _wire
from solvio.agent_runtime import endpoint, store as S
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.agent_runtime.task_authority import CapabilityGrant, VerifiedTaskReceipt
from solvio.agent_runtime.task_start_service import TaskStepAuthority
from solvio.agent_runtime import task_start_proof as T
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.approval_gateway import CapabilityApprovals
from solvio.capabilities.contract import CapabilitySpec, ExecutionClass, ArgumentSource
from solvio.capabilities.envelope import CapabilityOutcome as OUT
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.security.mobile_approval import app_attest as AA, browser_sessions as B, gateway
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE
from solvio.tools.base import RiskLevel

BODY = {"scope": "research", "objective": "Vergleiche drei Hotels in Hamburg mit Quellen.",
        "target_repo": "", "client_request_id": "request-001"}
OWNER = TrustContext(TrustLevel.USER_DIRECT, user_authorized=True)


class World:
    async def open(self, folder):
        self.store, self.cp, self.co, self.sessions = await _wire(folder)
        self.ledger = S.AgentRunLedger(os.path.join(folder, "agent.sqlite3"))
        self.router = CapabilityRouter(mobile=CapabilityApprovals(self.co, owner_principal="local-owner"))
        self.orch = Orchestrator(ledger=self.ledger, router=self.router, control_plane=self.cp,
                                 memory_owner_principal="local-owner")
        caps = AgentCapabilities(self.orch)
        self.router.register(SPECS["agent_task_research"], caps.research)
        self.router.register(SPECS["agent_task_build"], caps.build)
        self.app = gateway.build_app(control_plane=self.cp, coordinator=self.co)
        port = unused_port()
        self.origin = f"https://127.0.0.1:{port}"
        B.attach(self.app, self.sessions, {self.origin})
        endpoint.attach(self.app, self.orch)
        server_tls, self.client_tls = _tls(folder)
        self.server = TestServer(self.app, port=port, scheme="https")
        await self.server.start_server(ssl=server_tls)
        self.clients = []
        self.client = await self.new_client()
        self.headers = await self.login(self.client)
        return self

    async def new_client(self):
        client = TestClient(self.server, cookie_jar=CookieJar(unsafe=True),
                            connector=TCPConnector(ssl=self.client_tls))
        await client.start_server()
        self.clients.append(client)
        return client

    async def login(self, client, principal="local-owner"):
        token = await self.sessions.issue_enrollment(principal=principal)
        res = await client.post(B.SESSION_PATH + "/login", json={"token": token.token},
                                headers={"Origin": self.origin})
        require_equal(res.status, 200)
        session = await res.json()
        return {"Origin": self.origin, "X-CSRF-Token": session["csrf_token"]}

    async def start(self, task=None):
        return await self.client.post("/v1/agent/tasks", json={"task": task or BODY}, headers=self.headers)

    async def close(self):
        for client in self.clients:
            await client.close()
        await self.server.close()
        await self.store.close()


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix="solvio-task-entry-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            w = await World().open(folder)
            try:
                yield w
            finally:
                await w.close()


async def t_browser_parallel_start_is_one_authorized_task_without_second_approval():
    async with world() as w:
        responses = await asyncio.gather(w.start(), w.start())
        require_equal([r.status for r in responses], [201, 201])
        data = [await r.json() for r in responses]
        require_equal(data[0]["task_id"], data[1]["task_id"])
        require_equal(len(w.ledger.recent_runs()), 1)
        task = w.ledger.get_task(data[0]["task_id"])
        require_equal(task.created_principal, "local-owner")
        require_equal(task.created_origin, "trusted_dashboard")
        grant = w.orch.task_authority.for_run(data[0]["run_id"])
        require_equal(grant.receipt_method, "dashboard_session")
        require(w.orch.task_authority.active(grant.reference, task_id=task.task_id, run_id=grant.run_id).allowed)
        require_equal(await w.store.list_pending(), [])
        require_equal(w.orch.costs.view(task.task_id)["ask_threshold_cents"], 1000)


async def t_repeated_request_cannot_change_objective_or_claim_origin():
    async with world() as w:
        require_equal((await w.start()).status, 201)
        changed = dict(BODY, objective="Loesche stattdessen den gesamten lokalen Bestand.")
        require_equal((await w.start(changed)).status, 409)
        forged = dict(BODY, origin="trusted_interactive_app", principal="local-owner")
        require_equal((await w.start(forged)).status, 400)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_two_distinct_direct_tasks_queue_without_blocking_each_other():
    async with world() as w:
        first = await (await w.start()).json()
        second = await (await w.start(dict(BODY, client_request_id="request-002"))).json()
        await w.orch.tick()
        require_equal(w.ledger.get_run(first["run_id"]).state, S.PLANNING)
        require_equal(w.ledger.get_run(second["run_id"]).state, S.CREATED)
        # Der belegte Slot bleibt einer; nach dem Owner-Abbruch startet der
        # naechste Auftrag beim echten Takt. Kein Anbieter fuer diesen Nachweis.
        await w.orch.cancel(first["run_id"])
        await w.orch.tick()
        require_equal(w.ledger.get_run(second["run_id"]).state, S.PLANNING)


async def t_browser_csrf_and_origin_fail_before_any_task_or_approval():
    async with world() as w:
        for headers in ({}, {"Origin": w.origin},
                        dict(w.headers, Origin="https://foreign.example")):
            res = await w.client.post("/v1/agent/tasks", json={"task": BODY}, headers=headers)
            require_equal(res.status, 401)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])


async def t_app_transport_alone_does_not_start_but_fresh_bound_app_attest_does():
    async with world() as w:
        device = await H.enroll_attested(w.cp, transport_cred="temporary-entry-transport")
        app = await w.new_client()
        headers = {"X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-entry-transport"}
        require_equal((await app.post("/v1/agent/tasks", json={"task": BODY}, headers=headers)).status, 401)
        ch = await app.post("/v1/agent/tasks/challenge", json={"task": BODY}, headers=headers)
        require_equal(ch.status, 200)
        wire = await ch.json()
        assertion = AA.fake_assertion(device.aakey,
            T.client_data_hash(base64.b64decode(wire["binding_b64"])), 1)
        payload = {"task": BODY, "proof": {"nonce": wire["nonce"],
            "assertion_b64": base64.b64encode(assertion).decode()}}
        res = await app.post("/v1/agent/tasks", json=payload, headers=headers)
        require_equal(res.status, 201, str(await res.json()))
        data = await res.json()
        require_equal(w.ledger.get_task(data["task_id"]).created_origin, "trusted_interactive_app")
        require_equal(w.orch.task_authority.for_run(data["run_id"]).receipt_method, "app_session")
        require_equal((await app.post("/v1/agent/tasks", json=payload, headers=headers)).status, 401)
        require_equal(await w.store.list_pending(), [])


async def t_other_principal_cannot_read_cancel_resume_or_raise_task_costs():
    async with world() as w:
        data = await (await w.start()).json()
        other = await w.new_client()
        headers = await w.login(other, "another-owner")
        require_equal((await (await other.get("/v1/agent/runs")).json())["laeufe"], [])
        require_equal((await other.get("/v1/agent/runs/" + data["run_id"])).status, 404)
        for op in ("cancel", "resume"):
            require_equal((await other.post("/v1/agent/runs/" + data["run_id"] + "/" + op,
                                          json={}, headers=headers)).status, 404)
        res = await other.post("/v1/agent/tasks/" + data["task_id"] + "/cost-approval",
            json={"max_total_cents": 2000, "client_request_id": "approve-0001"}, headers=headers)
        require_equal(res.status, 404)
        res = await other.put("/v1/agent/cost-policy", headers=headers,
            json={"ask_threshold_cents": 999999, "client_request_id": "policy-foreign"})
        require_equal(res.status, 403)
        require_equal(w.orch.costs.settings()["ask_threshold_cents"], 1000)
        require_equal(w.ledger.get_run(data["run_id"]).state, S.CREATED)


async def t_cost_policy_changes_new_tasks_and_explicit_cap_is_bound_to_one_request():
    async with world() as w:
        first = await (await w.start()).json()
        res = await w.client.put("/v1/agent/cost-policy", headers=w.headers,
            json={"ask_threshold_cents": 1500, "client_request_id": "policy-0001"})
        require_equal(res.status, 200)
        second = await (await w.start(dict(BODY, client_request_id="request-002"))).json()
        require_equal(w.orch.costs.view(first["task_id"])["ask_threshold_cents"], 1000)
        require_equal(w.orch.costs.view(second["task_id"])["ask_threshold_cents"], 1500)
        path = "/v1/agent/tasks/" + first["task_id"] + "/cost-approval"
        approval = {"max_total_cents": 2000, "client_request_id": "approve-0001"}
        require_equal((await w.client.post(path, json=approval, headers=w.headers)).status, 200)
        require_equal((await w.client.post(path, json=approval, headers=w.headers)).status, 200)
        require_equal((await w.client.post(path, json=dict(approval, max_total_cents=3000), headers=w.headers)).status, 409)


async def t_crash_between_task_creation_and_grant_never_runs_or_duplicates_task():
    async with world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("temporary interrupted acceptance")):
            response = await w.start()
            require_equal(response.status, 202)
            require_equal((await response.json())["annahme"], "preparing")
        run = w.ledger.recent_runs()[0]
        require(not w.orch.task_starts.ready(run.run_id))
        # Frische Runtime liest dieselbe DB; erster Takt komplettiert nur die
        # Annahme, ohne Planer/Spezialisten. Der zweite HTTP-Start findet sie.
        fresh = Orchestrator(ledger=w.ledger, router=w.router, control_plane=w.cp)
        await fresh._advance(run)
        require(fresh.task_starts.ready(run.run_id))
        require_equal(w.ledger.get_run(run.run_id).planner_calls, 0)
        require_equal(w.ledger.get_run(run.run_id).state, S.CREATED)
        again = await (await w.start()).json()
        require_equal(again["run_id"], run.run_id)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_grant_alone_is_not_a_price_receipt_for_an_unreviewed_handler():
    async with world() as w:
        effects = []
        from solvio.secret_vault.context import current
        async def write(arguments):
            effects.append((dict(arguments), current().origin))
            await asyncio.sleep(0)
            return {"written": True}
        spec = CapabilitySpec(name="note_write", version=1, execution_class=ExecutionClass.CONTROLLED,
            base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            executor="inline")
        w.router.register(spec, write)
        task, run = w.orch.create_task(objective="Schreibe genau die beauftragte Testnotiz.",
            scope="research", origin="trusted_dashboard", principal="local-owner")
        grant = w.orch.task_authority.issue(task.task_id, run.run_id,
            receipt=VerifiedTaskReceipt("dashboard_session", "browser:test-write", "local-owner"),
            capabilities=(CapabilityGrant("note_write", 1, {"text": "Beauftragte Testnotiz"}),))
        w.ledger.transition(run.run_id, S.PLANNING)
        w.ledger.transition(run.run_id, S.RUNNING)
        step = w.ledger.create_step(run_id=run.run_id, seq=1, kind="capability", capability="note_write")
        w.ledger.update_step(step.step_id, state="running", started=True)
        kwargs = dict(trust=OWNER, origin=OriginClass.BACKGROUND_AUTOMATION,
            principal="agent:temporary", task_step=TaskStepAuthority(grant.reference, task.task_id, run.run_id, step.step_id))
        results = await asyncio.gather(*(w.router.execute("note_write", {"text": "Beauftragte Testnotiz"}, **kwargs) for _ in range(2)))
        require_equal([(r.outcome, r.reason) for r in results],
                      [(OUT.REJECTED_BY_POLICY, "cost_unbounded")] * 2)
        require_equal(effects, [], "even a granted name must not dispatch an unpriced implementation")
        stored = w.ledger.get_step(step.step_id)
        require_equal(stored.dispatch_binding_digest, "")
        require_equal(stored.dispatch_claimed_at, None)
        from solvio.agent_runtime.cost_dispatch import invocations
        require_equal(invocations(w.ledger, task.task_id), [])
        require_equal(await w.store.list_pending(), [])
        require_equal((await w.router.execute("note_write", {"text": "Anderes Ziel"}, **kwargs)).outcome, OUT.REJECTED_BY_POLICY)
        # Actual dispatch uniqueness and BACKGROUND_AUTOMATION at the physical
        # access boundary are proved by test_agent_portal_cost_dispatch using
        # the original, reviewed local handler and canonical temporary vault.


async def _room_request(w):
    from solvio.tools.agent_capability_tools import remember_pending_start
    arguments = {"objective": BODY["objective"]}
    context = SimpleNamespace(principal="pi-wohnzimmer", origin=OriginClass.ROOM_VOICE, commanded=True)
    result = await w.router.execute("agent_task_research", arguments,
        trust=OWNER, principal=context.principal, origin=context.origin)
    require_equal(result.outcome, OUT.APPROVAL_REQUIRED)
    remember_pending_start(w.ledger, capability="agent_task_research", result=result,
                           arguments=arguments, context=context)
    require_equal(w.ledger.recent_runs(), [])
    return result.data["request_id"]


async def t_dashboard_ok_after_room_turn_ends_creates_one_task_with_claim_receipt():
    async with world() as w:
        key = await _room_request(w)
        pending = await (await w.client.get("/v1/agent/approvals")).json()
        require_equal(len(pending["approvals"]), 1)
        response = await w.client.post("/v1/agent/approvals/" + key + "/decision",
            json={"action_digest": pending["approvals"][0]["action_digest"], "decision": "APPROVE"}, headers=w.headers)
        require_equal(response.status, 200, str(await response.json()))
        # Keine Gespraechsinstanz mehr erforderlich: bestehender Core-Poller.
        await w.orch._poll_pending_starts()
        runs = w.ledger.recent_runs()
        require_equal(len(runs), 1)
        task = w.ledger.get_task(runs[0].task_id)
        require_equal(task.created_origin, "room_voice")
        require_equal(task.created_principal, "local-owner")
        grant = w.orch.task_authority.for_run(runs[0].run_id)
        require_equal(grant.receipt_method, "dashboard_ok")
        require_equal(grant.receipt_reference, "approval:" + key)
        receipt, status = await w.cp.task_authorization_receipt(key)
        require_equal(status, "ok")
        require_equal(receipt["tool"], "agent_task_research")
        await w.orch._poll_pending_starts()
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_face_id_public_decision_reuses_same_start_path_and_denial_is_final():
    from solvio.security.mobile_approval import protocol as P
    for approve in (True, False):
        async with world() as w:
            device = await H.enroll_attested(w.cp, transport_cred="temporary-room-transport")
            app = await w.new_client()
            headers = {"X-Device-Id": device.device_id, "X-Transport-Cred": "temporary-room-transport"}
            key = await _room_request(w)
            ch = await app.post("/v1/approvals/" + key + "/challenge", headers=headers)
            require_equal(ch.status, 200)
            signed = H.sign_decision(device, P.b64d((await ch.json())["payload_b64"]),
                                     decision="APPROVE" if approve else "DENY")
            result = await app.post("/v1/approvals/" + key + "/decision", json=signed)
            require_equal(result.status, 200)
            await w.orch._poll_pending_starts()
            require_equal(len(w.ledger.recent_runs()), 1 if approve else 0)
            if approve:
                grant = w.orch.task_authority.for_run(w.ledger.recent_runs()[0].run_id)
                require_equal(grant.receipt_method, "face_id")
                require_equal(grant.authorizer, "local-owner")
            else:
                row = await w.store.get_request(key)
                retry = await w.client.post("/v1/agent/approvals/" + key + "/decision",
                    json={"action_digest": row["action_digest"], "decision": "APPROVE"}, headers=w.headers)
                require_equal(retry.status, 409)
            await w.orch._poll_pending_starts()
            require_equal(len(w.ledger.recent_runs()), 1 if approve else 0)


async def t_preparing_failure_does_not_block_another_accepted_task():
    async with world() as w:
        with patch.object(w.orch.task_authority, "issue", side_effect=RuntimeError("temporary grant store failure")):
            first = await (await w.start()).json()
        second = await (await w.start(dict(BODY, client_request_id="request-002"))).json()
        with patch.object(w.orch.task_starts, "finish", side_effect=RuntimeError("still unavailable")):
            await w.orch.tick()
        require_equal(w.ledger.get_run(first["run_id"]).state, S.CREATED)
        require_equal(w.ledger.get_run(second["run_id"]).state, S.PLANNING)


async def t_browser_enrollment_and_revocation_use_the_real_owner_unix_socket():
    from solvio.realtime.control import CoreControl, ControlClient
    async with world() as w:
        runtime = SimpleNamespace(browser_sessions=w.sessions,
            approvals=SimpleNamespace(owner_principal="local-owner"))
        with tempfile.TemporaryDirectory(prefix="solvio-browser-local-") as folder:
            socket = os.path.join(folder, "control.sock")
            control = CoreControl(SimpleNamespace(approver_runtime=runtime), socket_path=socket)
            await control.start()
            try:
                client = ControlClient(socket_path=socket)
                denied = await client.call({"op": "browser_session_enroll", "principal": "someone-else"})
                require(not denied["ok"])
                issued = await client.call({"op": "browser_session_enroll"})
                require(issued["ok"])
                session = await w.sessions.redeem(issued["token"])
                require_equal(session.actor.principal, "local-owner")
                revoked = await client.call({"op": "browser_session_revoke", "session_id": session.actor.session_id})
                require(revoked["revoked"])
                require(await w.sessions.authenticate(session.token) is None)
            finally:
                await control.stop()


# ---------------------------------------------------------------- N8/C3: conversation_ref im Task-Body

#: Gemessen mit den Modulen von c91a745 (vor der Aenderung): der Digest eines
#: Bodys OHNE `conversation_ref` und der `arguments_digest` eines Belegs ohne
#: Chatverweis. Beide muessen bytegleich bleiben — sonst braechen die auf dem
#: iPhone gespeicherten Retry-Bindungen und jede Replay-Erkennung alter Auftraege.
GOLDEN_REQUEST_DIGEST = "82a90d7a03a16a90311ab3a769ffac91d9186d996e02bd475b6f2334b3d40f63"
GOLDEN_BIND_DIGEST = "9436a856e6ad3970b7091e10d075f72e63057a2ee4341866722839bb2522a2c2"


def _chat_store(w, folder):
    from solvio.conversation import ConversationStore
    store = ConversationStore(os.path.join(folder, "conversations.sqlite3")).open()
    w.app["conversation_store_provider"] = lambda: store
    return store


def t_a_body_without_conversation_ref_keeps_its_golden_digests():
    from solvio.agent_runtime.task_start_service import AuthorizedTaskStart
    require_equal(T.request_digest(BODY), GOLDEN_REQUEST_DIGEST, "the task body digest moved")
    receipt = VerifiedTaskReceipt("dashboard_session", "browser:s-1:request-001", "local-owner")
    start = AuthorizedTaskStart.bind(receipt=receipt, request_id="request-001",
                                     capability="agent_task_research",
                                     arguments={"objective": BODY["objective"]})
    require_equal(start.arguments_digest, GOLDEN_BIND_DIGEST, "the bind digest moved")
    require_equal(start.conversation_ref, "")
    require(start.matches("agent_task_research", {"objective": BODY["objective"]}, "local-owner",
                          "trusted_dashboard"))
    # Mit Verweis: gueltig, anderer Digest, `matches` spiegelt ihn.
    bound = AuthorizedTaskStart.bind(receipt=receipt, request_id="request-001",
                                     capability="agent_task_research",
                                     arguments={"objective": BODY["objective"]},
                                     conversation_ref="c-0123456789abcdef")
    require(bound.arguments_digest != GOLDEN_BIND_DIGEST, "a chat-bound start has the unbound digest")
    require_equal(bound.conversation_ref, "c-0123456789abcdef")
    require(bound.matches("agent_task_research", {"objective": BODY["objective"]}, "local-owner",
                          "trusted_dashboard"))
    with_ref = dict(BODY, conversation_ref="c-0123456789abcdef")
    require(T.request_digest(with_ref) != GOLDEN_REQUEST_DIGEST)
    require_equal(T.canonical_task_body(with_ref), with_ref)
    for bad in ("", "c-0123", "at-0123456789abcdef", "C-0123456789ABCDEF", 7, None, "c-0123456789abcdeg"):
        try:
            T.canonical_task_body(dict(BODY, conversation_ref=bad))
        except (ValueError, TypeError):
            pass
        else:
            raise AssertionError(f"conversation_ref {bad!r} was accepted")
        if isinstance(bad, str) and bad:
            try:
                AuthorizedTaskStart.bind(receipt=receipt, request_id="request-001",
                                         capability="agent_task_research",
                                         arguments={"objective": BODY["objective"]}, conversation_ref=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"bind accepted conversation_ref {bad!r}")


async def t_conversation_ref_binds_a_started_task_to_an_owned_chat():
    async with world() as w:
        with tempfile.TemporaryDirectory(prefix="solvio-task-chat-") as folder:
            store = _chat_store(w, folder)
            try:
                chat, _ = store.create_conversation(owner_principal="local-owner", kind="text",
                                                    client_request_id="chat-0001")
                cid = chat["conversation_id"]
                body = dict(BODY, conversation_ref=cid)
                first = await w.start(body)
                require_equal(first.status, 201, str(await first.json()))
                data = await first.json()
                task = w.ledger.get_task(data["task_id"])
                require_equal(task.conversation_ref, cid, "the task does not carry the chat")
                links = store.task_links(cid)
                require_equal([(l["task_id"], l["run_id"], l["revision"], l["source"]) for l in links],
                              [(data["task_id"], data["run_id"], 1, "task:request-001")])
                # Replay: derselbe Body ergibt denselben Auftrag, keinen zweiten Link.
                again = await w.start(body)
                require_equal(again.status, 201)
                require_equal((await again.json())["task_id"], data["task_id"])
                require_equal(len(store.task_links(cid)), 1)
                require_equal(len(w.ledger.recent_runs()), 1)
                # Gleiche client_request_id, anderer (oder kein) Chatverweis → 409, kein zweiter Auftrag.
                other, _ = store.create_conversation(owner_principal="local-owner", kind="text",
                                                     client_request_id="chat-0002")
                require_equal((await w.start(dict(BODY, conversation_ref=other["conversation_id"]))).status, 409)
                require_equal((await w.start(BODY)).status, 409)
                require_equal(len(w.ledger.recent_runs()), 1)
                require_equal(store.task_links(other["conversation_id"]), [])
            finally:
                store.close()


async def t_a_foreign_or_unknown_chat_refuses_the_task_before_it_exists():
    async with world() as w:
        with tempfile.TemporaryDirectory(prefix="solvio-task-chat-") as folder:
            store = _chat_store(w, folder)
            try:
                foreign, _ = store.create_conversation(owner_principal="another-owner", kind="text",
                                                       client_request_id="chat-0001")
                voice, _ = store.begin_session("s-voice")
                for ref in (foreign["conversation_id"], voice, "c-00000000000000ff"):
                    res = await w.start(dict(BODY, conversation_ref=ref))
                    require_equal(res.status, 404, str(await res.json()))
                    require_equal((await res.json())["error"], "unknown_conversation")
                require_equal(w.ledger.recent_runs(), [], "a refused chat binding created a task")
                require_equal(store.task_links(foreign["conversation_id"]), [])
                # Ohne Speicher gibt es keinen Chat, an den gebunden werden koennte.
                w.app["conversation_store_provider"] = lambda: None
                res = await w.start(dict(BODY, conversation_ref=foreign["conversation_id"]))
                require_equal(res.status, 404)
                require_equal(w.ledger.recent_runs(), [])
                # Ein Body ohne Verweis laeuft wie bisher.
                require_equal((await w.start()).status, 201)
            finally:
                store.close()


async def t_the_receipts_chat_reaches_the_task_and_a_conflicting_argument_is_refused():
    """§4.1: `_create` nimmt den Chat aus dem Beleg; zwei verschiedene Verweise sind ein
    Verdrahtungsfehler, keine stille Wahl."""
    from solvio.agent_runtime.task_start_service import AuthorizedTaskStart
    from solvio.capabilities.contract import CapabilityRefused
    from solvio.secret_vault import context as SC
    async with world() as w:
        with tempfile.TemporaryDirectory(prefix="solvio-task-chat-") as folder:
            store = _chat_store(w, folder)
            try:
                chat, _ = store.create_conversation(owner_principal="local-owner", kind="text",
                                                    client_request_id="chat-0001")
                cid = chat["conversation_id"]
                caps = AgentCapabilities(w.orch)
                receipt = VerifiedTaskReceipt("dashboard_session", "browser:s-1:request-777", "local-owner")
                start = AuthorizedTaskStart.bind(receipt=receipt, request_id="request-777",
                                                 capability="agent_task_research",
                                                 arguments={"objective": BODY["objective"]},
                                                 conversation_ref=cid)
                context = SC.UseContext(origin=OriginClass.TRUSTED_DASHBOARD, capability="agent_task_research",
                                        principal="local-owner", task_start_receipt=start)
                with SC.bound(context):
                    try:
                        await caps._create(BODY["objective"], "research", "",
                                           conversation_ref="c-fedcba9876543210")
                    except CapabilityRefused as exc:
                        require_equal(exc.reason, "task_start_conversation_mismatch")
                    else:
                        raise AssertionError("a conflicting conversation_ref started a task")
                    require_equal(w.ledger.recent_runs(), [], "the refusal created a task")
                    created = await caps._create(BODY["objective"], "research", "")
                require_equal(w.ledger.get_task(created["task_id"]).conversation_ref, cid,
                              "the chat of the receipt did not reach the task")
            finally:
                store.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
