"""N8/C3 §2: die Chat-Routen an echtem HTTPS — Browsersitzung, Geraet, App-Attest-Beweis.

Alle Speicher und Zertifikate sind temporaer; das iPhone ist synthetisch. Der
Prozessor ist hier eine Attrappe, die nur zaehlt, WANN sie geweckt wird — kein
Modell, kein Anbieter, kein Auftrag laeuft in dieser Suite.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()

import mobile_attest_helper as H
from test_agent_task_entry import BODY, World
from solvio.agent_runtime import conversation_message_proof as CP
from solvio.agent_runtime import costs as C
from solvio.agent_runtime import task_followup_endpoint as FE, task_start_proof as T
from solvio.conversation import ConversationStore, ConversationStoreError
from solvio.conversation import endpoint as CE, message_proof as MP
from solvio.dashboard.auth import OWNER
from solvio.security.mobile_approval import app_attest as AA, browser_sessions as B

TRANSPORT_CRED = "temporary-chat-transport"


async def _forbidden_transport(payload):
    raise AssertionError("the endpoint suite must never reach a model transport")


class ChatWorld(World):
    async def open(self, folder):
        await super().open(folder)
        self.app[OWNER] = "local-owner"
        self.chat_store = ConversationStore(os.path.join(folder, "conversations.sqlite3")).open()
        self.app[CE.STORE_PROVIDER_KEY] = lambda: self.chat_store
        self.app[CE.TRANSPORT_PROVIDER_KEY] = lambda: _forbidden_transport
        # Der Endpunkt nimmt nur an, was ein Router bearbeiten kann (A2); diese Welt
        # bearbeitet nichts (Prozessor-Attrappe unten), aber ein Router IST da.
        self.cognition = SimpleNamespace(name="endpoint-world-router")
        self.app[CE.ROUTER_PROVIDER_KEY] = lambda: self.cognition
        self.real_processor = self.app[CE.PROCESSOR_KEY]
        self.real_processor.stop()
        self.wakes = []
        self.app[CE.PROCESSOR_KEY] = SimpleNamespace(wake=lambda cid: self.wakes.append(cid))
        return self

    async def close(self):
        await super().close()
        self.chat_store.close()

    async def create(self, request_id="chat-0001", *, client=None, headers=None, title=None):
        payload = {"client_request_id": request_id}
        if title is not None:
            payload["title"] = title
        return await (client or self.client).post(CE.PREFIX, json=payload,
                                                  headers=self.headers if headers is None else headers)

    async def chat(self, request_id="chat-0001"):
        res = await self.create(request_id)
        require(res.status in (200, 201), str(await res.json()))
        return (await res.json())["conversation_id"]

    async def send(self, cid, text, *, client_message_id="msg-0001", client=None, headers=None, **extra):
        message = {"conversation_id": cid, "client_message_id": client_message_id, "text": text, **extra}
        return await (client or self.client).post(f"{CE.PREFIX}/{cid}/messages", json={"message": message},
                                                  headers=self.headers if headers is None else headers)

    async def device(self, principal="local-owner", device_id="chat-device"):
        ctx = await H.enroll_attested(self.cp, device_id=device_id, principal=principal,
                                      transport_cred=TRANSPORT_CRED)
        client = await self.new_client()
        headers = {"X-Device-Id": ctx.device_id, "X-Transport-Cred": TRANSPORT_CRED}
        return ctx, client, headers

    async def app_proof(self, ctx, client, headers, message, *, counter=1,
                        hash_function=CP.client_data_hash, service=None):
        path = f"{CE.PREFIX}/{message['conversation_id']}/messages/challenge"
        res = await client.post(path, json={"message": message}, headers=headers)
        require_equal(res.status, 200, await res.text())
        challenge = await res.json()
        raw = base64.b64decode(challenge["binding_b64"])
        require_equal(json.loads(raw)["type"], CP.TYPE_CONVERSATION_MESSAGE_BINDING)
        require_equal(challenge["request_digest"], MP.request_digest(message))
        assertion = AA.fake_assertion(ctx.aakey, hash_function(raw), counter)
        return {"message": message, "proof": {"nonce": challenge["nonce"],
                                              "assertion_b64": base64.b64encode(assertion).decode()}}


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix="solvio-chat-endpoint-") as folder:
        with patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
            w = await ChatWorld().open(folder)
            try:
                yield w
            finally:
                await w.close()


async def t_creating_a_chat_is_idempotent_and_readable_by_browser_and_device_but_not_by_others():
    async with world() as w:
        first = await w.create()
        require_equal(first.status, 201, await first.text())
        require_equal(first.headers["Cache-Control"], "no-store")
        created = await first.json()
        require_equal(set(created), {"conversation_id", "title", "kind", "created_at", "last_activity_at"})
        require_equal(created["kind"], "text")
        again = await w.create()
        require_equal(again.status, 200)
        require_equal((await again.json())["conversation_id"], created["conversation_id"])
        titled = await w.create("chat-0002", title="  Reise   nach Hamburg ")
        require_equal((await titled.json())["title"], "Reise nach Hamburg")
        require_equal((await w.create("chat-0003", title="x" * 81)).status, 400)
        require_equal((await w.create("k")).status, 400)
        # Ein Geraet mit Transportkennung darf anlegen und lesen — dieselbe Linie wie /v1/agent/runs.
        ctx, app, headers = await w.device()
        res = await w.create("chat-app-0001", client=app, headers=headers)
        require_equal(res.status, 201, await res.text())
        app_chat = (await res.json())["conversation_id"]
        listing = await (await app.get(CE.PREFIX, headers=headers)).json()
        ids = {c["conversation_id"] for c in listing["conversations"]}
        require_equal(ids, {created["conversation_id"], (await titled.json())["conversation_id"], app_chat})
        for row in listing["conversations"]:
            require_equal(set(row), {"conversation_id", "title", "kind", "last_activity_at", "created_at",
                                     "message_count", "open_task_count", "open_delivery_count"})
        # Ein anderer Browser-Principal ist nicht der Owner: nichts, auch kein Anlegen.
        other = await w.new_client()
        other_headers = await w.login(other, "another-owner")
        require_equal((await w.create("chat-x", client=other, headers=other_headers)).status, 401)
        require_equal((await other.get(CE.PREFIX + "/" + app_chat)).status, 401)
        # Ein Geraet eines anderen Principals sieht seine eigenen Chats — und sonst keinen.
        _, foreign, foreign_headers = await w.device("second-owner", "second-device")
        res = await w.create("chat-f-0001", client=foreign, headers=foreign_headers)
        require_equal(res.status, 201)
        foreign_ids = {c["conversation_id"] for c in
                       (await (await foreign.get(CE.PREFIX, headers=foreign_headers)).json())["conversations"]}
        require_equal(foreign_ids, {(await res.json())["conversation_id"]})
        require_equal((await foreign.get(CE.PREFIX + "/" + app_chat, headers=foreign_headers)).status, 404)
        require_equal((await foreign.get(CE.PREFIX + "/" + created["conversation_id"], headers=foreign_headers)).status, 404)
        require_equal((await w.client.get(CE.PREFIX + "/" + app_chat)).status, 200,
                      "the owner's browser and the owner's device share one principal")
        require_equal(w.wakes, [])


async def t_browser_rules_and_no_device_fallback_after_a_cookie_failure():
    async with world() as w:
        cid = await w.chat()
        for headers in ({}, {"Origin": w.origin}, dict(w.headers, Origin="https://foreign.example"),
                        {B.CSRF_HEADER: w.headers[B.CSRF_HEADER]}):
            require_equal((await w.create("chat-9999", headers=headers)).status, 401)
            require_equal((await w.send(cid, "Hallo SOLVIO", headers=headers)).status, 401)
        # Lesen braucht keinen CSRF — aber die Sitzung.
        require_equal((await w.client.get(CE.PREFIX)).status, 200)
        fresh = await w.new_client()
        require_equal((await fresh.get(CE.PREFIX)).status, 401)
        # Ein Browser-Cookie mit fehlgeschlagener Pruefung faellt NIE auf das Geraet zurueck.
        ctx, app, device_headers = await w.device()
        require_equal((await w.client.get(CE.PREFIX, headers=device_headers)).status, 200)
        bad = dict(device_headers, **{"Origin": w.origin, B.CSRF_HEADER: "wrong-token"})
        require_equal((await w.client.post(CE.PREFIX, json={"client_request_id": "chat-fallback"}, headers=bad)).status, 401)
        require_equal((await w.client.post(f"{CE.PREFIX}/{cid}/messages/challenge",
            json={"message": {"conversation_id": cid, "client_message_id": "msg-0001", "text": "Hallo SOLVIO"}},
            headers=bad)).status, 401)
        # Selbst ein VOLLSTAENDIGER, gueltiger App-Attest-Beweis rettet eine Anfrage nicht, deren
        # Browser-Cookie durchgefallen ist: kein Geraete-Fallback, keine Nachricht.
        message = {"conversation_id": cid, "client_message_id": "msg-0002", "text": "Hallo SOLVIO"}
        payload = await w.app_proof(ctx, app, device_headers, message)
        mixed = await w.client.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=bad)
        require_equal(mixed.status, 401)
        require_equal(w.chat_store.list_conversations("local-owner")[0]["message_count"], 0)
        require_equal(w.wakes, [])
        # Ohne Cookie-Fehler ist derselbe Beweis vom Geraet aus gueltig — der Beweis war frisch.
        payload = await w.app_proof(ctx, app, device_headers, message, counter=2)
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=device_headers)).status, 202)


async def t_a_browser_message_is_accepted_once_replayed_by_digest_and_conflicts_on_a_changed_body():
    async with world() as w:
        cid = await w.chat()
        res = await w.send(cid, "Vergleiche drei Hotels in Hamburg.")
        require_equal(res.status, 202, await res.text())
        accepted = await res.json()
        require_equal(set(accepted), {"delivery_id", "status", "message_id"})
        require_equal(accepted["status"], "accepted")
        require_equal(w.wakes, [cid], "a new delivery wakes the processor exactly once")
        # Replay (gleicher Body): 202, dieselbe Zustellung, keine zweite Nachricht, kein zweites Wecken.
        again = await w.send(cid, "Vergleiche drei Hotels in Hamburg.")
        require_equal(again.status, 202)
        require_equal((await again.json())["delivery_id"], accepted["delivery_id"])
        require_equal(len(w.chat_store.messages(cid)), 1)
        require_equal(w.wakes, [cid])
        # Gleiche client_message_id, anderer Digest: 409, nichts persistiert.
        conflict = await w.send(cid, "Loesche stattdessen alles.")
        require_equal(conflict.status, 409)
        require_equal((await conflict.json())["error"], "message_conflict")
        require_equal(len(w.chat_store.messages(cid)), 1)
        # Zwei gleichzeitige POSTs derselben Kennung: genau eine Zeile.
        responses = await asyncio.gather(*(w.send(cid, "Und was kostet das?", client_message_id="msg-0002")
                                          for _ in range(2)))
        require_equal([r.status for r in responses], [202, 202])
        ids = {(await r.json())["delivery_id"] for r in responses}
        require_equal(len(ids), 1)
        require_equal(len(w.chat_store.messages(cid)), 2)
        require_equal(w.wakes, [cid, cid])
        # Pfad und Body muessen denselben Chat nennen; ein fremder oder unbekannter Chat ist 404.
        other = await w.chat("chat-0002")
        mismatch = await w.client.post(f"{CE.PREFIX}/{other}/messages", json={"message": {
            "conversation_id": cid, "client_message_id": "msg-0003", "text": "Hallo"}}, headers=w.headers)
        require_equal(mismatch.status, 400)
        require_equal((await w.send("c-00000000000000ff", "Hallo SOLVIO")).status, 404)
        require_equal((await w.send(cid, "", client_message_id="msg-0004")).status, 400)
        require_equal((await w.send(cid, "x" * 4001, client_message_id="msg-0005")).status, 400)
        # Der Titel kam aus dem ERSTEN persistierten Text.
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal(detail["conversation"]["title"], "Vergleiche drei Hotels in Hamburg.")
        require_equal([m["role"] for m in detail["messages"]], ["user", "user"])
        require_equal(detail["messages"][0]["delivery"]["delivery_id"], accepted["delivery_id"])
        require_equal(detail["messages"][0]["delivery"]["status"], "accepted")
        require_equal(detail["deliveries_open"], 2)
        require_equal(detail["auftraege"], [])
        after = await (await w.client.get(CE.PREFIX + "/" + cid + "?after_sequence=1")).json()
        require_equal([m["sequence"] for m in after["messages"]], [2])
        require_equal((await w.client.get(CE.PREFIX + "/" + cid + "?after_sequence=x")).status, 400)


async def t_app_message_transport_auth_precedes_proof_consumption():
    """Build 11: a valid assertion cannot replace X-Transport-Cred on submit."""
    async with world() as w:
        ctx, app, headers = await w.device()
        created = await w.create("chat-app-transport", client=app, headers=headers)
        require_equal(created.status, 201, await created.text())
        cid = (await created.json())["conversation_id"]
        path = f"{CE.PREFIX}/{cid}/messages"
        message = {"conversation_id": cid, "client_message_id": "msg-app-transport-0001",
                   "text": "Hallo SOLVIO, bist du da?"}
        payload = await w.app_proof(ctx, app, headers, message)

        # Both rejected submissions reuse the very same nonce and assertion.
        for rejected_headers in ({"X-Device-Id": ctx.device_id},
                                 dict(headers, **{"X-Transport-Cred": "invalid-chat-transport"})):
            refused = await app.post(path, json=payload, headers=rejected_headers)
            require_equal(refused.status, 401, await refused.text())
            require_equal(w.chat_store.messages(cid), [])
            require_equal(w.chat_store.deliveries(cid), [])
            require_equal(w.wakes, [])

        # The transport credential is necessary, but never sufficient on its own.
        bare = await app.post(path, json={"message": message}, headers=headers)
        require_equal(bare.status, 401, await bare.text())
        require_equal(w.chat_store.messages(cid), [])
        require_equal(w.chat_store.deliveries(cid), [])
        require_equal(w.wakes, [])

        # No new challenge/assertion: transport rejection must not consume the proof.
        accepted = await app.post(path, json=payload, headers=headers)
        require_equal(accepted.status, 202, await accepted.text())
        delivery_id = (await accepted.json())["delivery_id"]
        deliveries = w.chat_store.deliveries(cid)
        require_equal(len(deliveries), 1)
        require_equal(deliveries[0]["delivery_id"], delivery_id)
        require_equal(deliveries[0]["status"], "accepted")
        require_equal(len(w.chat_store.messages(cid)), 1)
        require_equal(w.wakes, [cid])


async def t_an_app_message_needs_a_fresh_exactly_bound_proof_and_no_other_nonce_counts():
    async with world() as w:
        ctx, app, headers = await w.device()
        res = await w.create("chat-app", client=app, headers=headers)
        cid = (await res.json())["conversation_id"]
        message = {"conversation_id": cid, "client_message_id": "msg-app-0001", "text": "Hallo SOLVIO, was laeuft?"}
        # Transportkennung allein: nie eine Nachricht.
        bare = await app.post(f"{CE.PREFIX}/{cid}/messages", json={"message": message}, headers=headers)
        require_equal(bare.status, 401)
        require_equal(w.wakes, [])
        # Der exakt gebundene Beweis: 202, Zeile traegt Geraet, Core, Generation, Sequenz.
        payload = await w.app_proof(ctx, app, headers, message)
        res = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
        require_equal(res.status, 202, await res.text())
        accepted = await res.json()
        row = w.chat_store.delivery(cid, accepted["delivery_id"])
        require_equal(row["source_kind"], "app")
        require_equal(row["device_id"], ctx.device_id)
        require_equal(row["core_id"], w.cp.core_instance_id)
        require_equal(row["principal"], "local-owner")
        require_equal(row["message_sequence"], 1)
        require_equal(row["digest"], MP.request_digest(message))
        require(row["source_ref"].startswith("chat:" + w.cp.core_instance_id + ":"))
        from solvio.voice_task_session import device_generation
        require_equal(row["source_generation"],
                      MP.source_fingerprint(await device_generation(w.cp, ctx.device_id)))
        require_equal(w.wakes, [cid])
        # Derselbe Beweis noch einmal: die Nonce ist verbraucht.
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)).status, 401)
        # Dieselbe Nachricht mit FRISCHEM Beweis: Replay, dieselbe Zustellung, kein zweites Wecken.
        payload2 = await w.app_proof(ctx, app, headers, message, counter=2)
        res2 = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload2, headers=headers)
        require_equal(res2.status, 202)
        require_equal((await res2.json())["delivery_id"], accepted["delivery_id"])
        require_equal(len(w.chat_store.messages(cid)), 1)
        require_equal(w.wakes, [cid])
        # Ein Beweis ueber einen ANDEREN Body: abgelehnt, nichts angenommen.
        other = dict(message, client_message_id="msg-app-0002", text="Loesche alles.")
        forged = await w.app_proof(ctx, app, headers, message, counter=3)
        forged["message"] = other
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=forged, headers=headers)).status, 401)
        require_equal(len(w.chat_store.messages(cid)), 1)
        # Task- oder Followup-Domain ueber die Nachrichtenbindung: kein Beweis.
        for hash_function in (T.client_data_hash, FE.client_data_hash):
            wrong = await w.app_proof(ctx, app, headers, dict(message, client_message_id="msg-app-0003"),
                                      counter=4, hash_function=hash_function)
            require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=wrong, headers=headers)).status, 401)
        # Eine echte Task-Start-Nonce zaehlt nicht als Nachrichten-Nonce.
        task_body = dict(BODY, client_request_id="request-chat-001", conversation_ref=cid)
        ch = await app.post("/v1/agent/tasks/challenge", json={"task": task_body}, headers=headers)
        require_equal(ch.status, 200)
        wire = await ch.json()
        assertion = AA.fake_assertion(ctx.aakey, CP.client_data_hash(base64.b64decode(wire["binding_b64"])), 5)
        stolen = {"message": dict(message, client_message_id="msg-app-0004"),
                  "proof": {"nonce": wire["nonce"], "assertion_b64": base64.b64encode(assertion).decode()}}
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=stolen, headers=headers)).status, 401)
        require_equal(len(w.chat_store.messages(cid)), 1)
        require_equal(w.wakes, [cid])
        # Eine Challenge fuer einen fremden Chat gibt es nicht.
        foreign_cid = await w.chat("chat-browser-only")
        foreign = dict(message, conversation_id=foreign_cid)
        w.chat_store.update_title(foreign_cid, "Fremd")
        w.chat_store.conn.execute("UPDATE conversations SET owner_principal='someone-else' WHERE conversation_id=?",
                                  (foreign_cid,))
        res = await app.post(f"{CE.PREFIX}/{foreign_cid}/messages/challenge", json={"message": foreign}, headers=headers)
        require_equal(res.status, 404)
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages/challenge",
                                      json={"message": foreign}, headers=headers)).status, 400)
        # Ein gesperrtes Geraet verliert die Annahme, auch mit fertigem Beweis.
        payload3 = await w.app_proof(ctx, app, headers, dict(message, client_message_id="msg-app-0005"), counter=6)
        await w.cp.revoke_device(ctx.device_id, reason="test")
        require_equal((await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload3, headers=headers)).status, 401)
        require_equal(len(w.chat_store.messages(cid)), 1)


async def t_rename_and_delete_are_browser_only_and_delete_waits_for_open_deliveries():
    async with world() as w:
        cid = await w.chat()
        ctx, app, headers = await w.device()
        require_equal((await app.patch(CE.PREFIX + "/" + cid, json={"title": "Neu"}, headers=headers)).status, 401)
        require_equal((await app.delete(CE.PREFIX + "/" + cid, headers=headers)).status, 401)
        res = await w.client.patch(CE.PREFIX + "/" + cid, json={"title": "  Hotels  in   Hamburg "}, headers=w.headers)
        require_equal(res.status, 200, await res.text())
        require_equal((await res.json())["conversation"]["title"], "Hotels in Hamburg")
        require_equal((await w.client.patch(CE.PREFIX + "/" + cid, json={"title": ""}, headers=w.headers)).status, 400)
        require_equal((await w.client.patch(CE.PREFIX + "/" + cid, json={"title": "x" * 81}, headers=w.headers)).status, 400)
        require_equal((await w.client.patch(CE.PREFIX + "/" + cid, json={"name": "x"}, headers=w.headers)).status, 400)
        require_equal((await w.client.patch(CE.PREFIX + "/c-00000000000000ff", json={"title": "x"}, headers=w.headers)).status, 404)
        require_equal((await w.client.patch(CE.PREFIX + "/" + cid, json={"title": "x"})).status, 401)
        accepted = await (await w.send(cid, "Hallo SOLVIO")).json()
        blocked = await w.client.delete(CE.PREFIX + "/" + cid, headers=w.headers)
        require_equal(blocked.status, 409)
        require_equal((await blocked.json())["error"], "deliveries_open")
        require(w.chat_store.conversation(cid) is not None)
        # Die Zustellung endet — ueber die Store-Wege des Prozessors, nicht ueber HTTP.
        row = w.chat_store.claim_next_delivery(cid, "pw-test")
        require_equal(row["delivery_id"], accepted["delivery_id"])
        w.chat_store.complete_delivery(row["delivery_id"], "pw-test", assistant_text="Hallo zurueck.")
        status = await (await w.client.get(f"{CE.PREFIX}/{cid}/deliveries/{accepted['delivery_id']}")).json()
        require_equal((status["status"], status["message_id"], status["files"]),
                      ("completed", accepted["message_id"], []))
        require("assistant_message_id" in status)
        require_equal((await w.client.get(f"{CE.PREFIX}/{cid}/deliveries/cd-00000000000000ff")).status, 404)
        require_equal((await w.client.delete(CE.PREFIX + "/" + cid)).status, 401)
        require_equal((await w.client.delete(CE.PREFIX + "/" + cid, headers=w.headers)).status, 204)
        require_equal((await w.client.get(CE.PREFIX + "/" + cid)).status, 404)
        require_equal((await w.client.delete(CE.PREFIX + "/" + cid, headers=w.headers)).status, 404)
        # Die Anlage-Idempotenz stirbt mit dem Chat: dieselbe Kennung ergibt einen NEUEN Chat,
        # nie die geloeschte Kennung mit 200.
        recreated = await w.create()
        require_equal(recreated.status, 201)
        require((await recreated.json())["conversation_id"] != cid, "a deleted chat was resurrected")


async def t_detail_delivers_task_cards_only_for_the_owners_linked_tasks():
    async with world() as w:
        cid = await w.chat()
        started = await w.start(dict(BODY, conversation_ref=cid))
        require_equal(started.status, 201, await started.text())
        task = await started.json()
        settled = w.orch.costs.reserve(task["task_id"], "chat-card-settled", 200,
            route="codex", evidence=C.CostEvidence("enforceable_upper_bound", "test:bounded-call"))
        require(settled.allowed)
        require_equal(w.orch.costs.settle(settled.reservation_id, 125,
            C.CostEvidence("actual_charge", "test:usage-receipt")).status, "settled")
        reserved = w.orch.costs.reserve(task["task_id"], "chat-card-reserved", 75,
            route="codex", evidence=C.CostEvidence("enforceable_upper_bound", "test:pending-call"))
        require(reserved.allowed)
        standalone = await (await w.client.get("/v1/agent/runs/" + task["run_id"])).json()
        require_equal(standalone["kosten"]["configured"], True)
        require_equal(standalone["kosten"]["ai_tool"]["spent_cents"], 125)
        require_equal(standalone["kosten"]["ai_tool"]["reserved_cents"], 75)
        require_equal(standalone["kosten"]["counts"], {"settled": 1, "reserved": 1})
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal([card["id"] for card in detail["auftraege"]], [task["run_id"]])
        card = detail["auftraege"][0]
        for key in ("zustand", "dateien", "task_revision", "followup", "action_intent"):
            require(key in card, f"the task card lacks {key}")
        require_equal(card.get("kosten"), standalone["kosten"],
                      "the embedded task card must expose the same booked and reserved costs")
        listing = await (await w.client.get(CE.PREFIX)).json()
        require_equal(listing["conversations"][0]["open_task_count"], 1)
        # Ein Link auf einen FREMDEN Auftrag wird nicht ausgeliefert.
        foreign_task = w.ledger.create_task(objective="Fremder Auftrag", scope="research",
                                            created_origin="trusted_dashboard", created_principal="someone-else")
        foreign_run = w.ledger.create_run(task_id=foreign_task.task_id)
        w.chat_store.add_task_link(cid, foreign_task.task_id, foreign_run.run_id, source="task:foreign")
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal([card["id"] for card in detail["auftraege"]], [task["run_id"]])
        listing = await (await w.client.get(CE.PREFIX)).json()
        require_equal(listing["conversations"][0]["open_task_count"], 1)
        # Eine fehlende Kostenkonfiguration bleibt unbekannt, nicht erfundene null Euro.
        unconfigured = w.ledger.create_task(objective="Noch ohne Kostenkonfiguration", scope="research",
            created_origin="trusted_dashboard", created_principal="local-owner")
        unconfigured_run = w.ledger.create_run(task_id=unconfigured.task_id)
        w.chat_store.add_task_link(cid, unconfigured.task_id, unconfigured_run.run_id, source="task:unconfigured")
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        cards = {card["id"]: card for card in detail["auftraege"]}
        require_equal(set(cards), {task["run_id"], unconfigured_run.run_id})
        standalone = await (await w.client.get("/v1/agent/runs/" + unconfigured_run.run_id)).json()
        require_equal(cards[unconfigured_run.run_id].get("kosten"), standalone["kosten"])
        require_equal(standalone["kosten"],
                      {"configured": False, "currency": "EUR", "task_id": unconfigured.task_id})
        require_equal(w.wakes, [], "reading never wakes the processor")


async def t_every_delivery_view_names_the_client_message_id_so_a_lost_202_can_be_reconciled():
    """§5.2: nach einem Neuladen kennt der Client oft nur SEINE Kennung (die 202 ging
    verloren, `delivery_id` unbekannt). Detail und Einzelroute muessen sie nennen —
    sonst bleibt „moeglicherweise nicht angekommen" stehen, bis der Nutzer denselben
    Text erneut sendet. Browser- und App-Weg liefern dieselbe Sicht."""
    async with world() as w:
        cid = await w.chat()
        client_message_id = "9b8c2d1e-3f4a-4b5c-8d6e-7f8091a2b3c4"
        res = await w.send(cid, "Was steht heute an?", client_message_id=client_message_id)
        require_equal(res.status, 202, await res.text())
        delivery_id = (await res.json())["delivery_id"]
        detail = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        require_equal(detail["messages"][0]["delivery"]["client_message_id"], client_message_id)
        single = await (await w.client.get(f"{CE.PREFIX}/{cid}/deliveries/{delivery_id}")).json()
        require_equal(single["client_message_id"], client_message_id)
        require_equal(single["delivery_id"], delivery_id)
        # Dieselbe Sicht fuer die App — je Zustellung IHRE Kennung, nie die einer anderen.
        ctx, app, headers = await w.device()
        app_cid = (await (await w.create("chat-app-0001", client=app, headers=headers)).json())["conversation_id"]
        message = {"conversation_id": app_cid, "client_message_id": "app-7d2f0c11", "text": "Und morgen?"}
        res = await app.post(f"{CE.PREFIX}/{app_cid}/messages",
                             json=await w.app_proof(ctx, app, headers, message), headers=headers)
        require_equal(res.status, 202, await res.text())
        detail = await (await app.get(CE.PREFIX + "/" + app_cid, headers=headers)).json()
        require_equal([m["delivery"]["client_message_id"] for m in detail["messages"]], ["app-7d2f0c11"])


async def t_a_completion_that_lands_between_two_detail_reads_never_yields_a_torn_view():
    """§9.1 Fall 16 fuer den ABSCHLUSS: die Detailroute setzt fuenf Leser zu einer
    Sicht zusammen. Commitet ein `complete_delivery` zwischen `messages()` und
    `deliveries()`, sagt die Sicht `completed` mit `assistant_message_id`, aber der
    Verlauf zeigt die Antwort nicht und `deliveries_open` ist 0 — das Dashboard
    faellt auf den 12-s-Takt zurueck, die Reconcile raeumt den Eintrag. Verlangt ist
    „Stand davor oder vollstaendiger Stand danach"."""
    import threading
    async with world() as w:
        cid = await w.chat()
        res = await w.send(cid, "Hallo, bist du da?", client_message_id="msg-torn-0001")
        require_equal(res.status, 202, await res.text())
        delivery_id = (await res.json())["delivery_id"]
        store = w.chat_store
        require(store.claim_next_delivery(cid, "pw-torn") is not None)
        loop = asyncio.get_running_loop()
        actual_messages = store.messages
        entered, hold = asyncio.Event(), threading.Event()

        def slow_messages(*args, **kwargs):
            rows = actual_messages(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            hold.wait(5)                                    # zwischen messages() und deliveries()
            return rows
        with patch.object(store, "messages", slow_messages):
            reading = asyncio.ensure_future(w.client.get(CE.PREFIX + "/" + cid))
            await asyncio.wait_for(entered.wait(), 5)
            completing = asyncio.ensure_future(asyncio.to_thread(
                store.complete_delivery, delivery_id, "pw-torn", assistant_text="Ja, ich bin da."))
            await asyncio.sleep(0.25)
            require(not completing.done(), "the completion committed into an open detail read")
            hold.set()
            detail = await (await reading).json()
            await completing
        delivery = detail["messages"][0]["delivery"]
        roles = [m["role"] for m in detail["messages"]]
        # Der Stand DAVOR — vollstaendig: laufend, ohne Antwort, eine offene Zustellung.
        require_equal((delivery["status"], roles, detail["deliveries_open"]), ("running", ["user"], 1))
        require("assistant_message_id" not in delivery)
        # Und danach der vollstaendige Stand DANACH.
        after = await (await w.client.get(CE.PREFIX + "/" + cid)).json()
        delivery = after["messages"][0]["delivery"]
        require_equal(delivery["status"], "completed")
        require_equal([m["role"] for m in after["messages"]], ["user", "assistant"])
        require_equal(after["messages"][1]["message_id"], delivery["assistant_message_id"])
        require_equal(after["deliveries_open"], 0)
        require_equal(w.wakes, [cid], "reading never wakes the processor")


async def t_a_message_is_refused_with_503_while_no_cognitive_router_can_be_resolved():
    """Review-Hinweis A2 (18.09.2026): ohne kognitiven Router loest der Prozessor keine
    Laufzeit auf und eine angenommene Zustellung laege unbearbeitet. Ein 202 verspricht
    Bearbeitung — also wird ohne Router nichts angenommen, nichts persistiert, nichts geweckt."""
    async with world() as w:
        cid = await w.chat()
        provider = w.app[CE.ROUTER_PROVIDER_KEY]
        w.app[CE.ROUTER_PROVIDER_KEY] = lambda: None
        try:
            res = await w.send(cid, "Vergleiche drei Hotels in Hamburg.")
            require_equal(res.status, 503, await res.text())
            require_equal((await res.json())["error"], "cognitive_router_unavailable")
            require_equal(len(w.chat_store.messages(cid)), 0, "nothing is persisted without a router")
            require_equal(w.wakes, [], "nothing wakes the processor")
        finally:
            w.app[CE.ROUTER_PROVIDER_KEY] = provider
        res = await w.send(cid, "Vergleiche drei Hotels in Hamburg.")
        require_equal(res.status, 202, await res.text())
        require_equal(len(w.chat_store.messages(cid)), 1)


async def t_the_default_router_provider_serves_the_chat_only_in_active_mode():
    """Review Runde 3, A3-H4: im Schatten- oder Aus-Modus misst der Router und wirkt nicht —
    der Textweg darf dann keine Auftraege starten. Der Standard-Provider liefert nur einen
    aktiven Router; alles andere ist `cognitive_router_unavailable`."""
    from types import SimpleNamespace as NS
    app = {"voice_core_server": NS(dispatcher=NS(cognition=NS(mode="shadow")))}
    provide = CE._default_router_provider(app)
    require_equal(provide(), None, "a shadow router served the chat")
    app["voice_core_server"].dispatcher.cognition = NS(mode="off")
    require_equal(provide(), None, "an off router served the chat")
    active = NS(mode="active")
    app["voice_core_server"].dispatcher.cognition = active
    require(provide() is active)
    app["voice_core_server"] = None
    require_equal(provide(), None)


async def t_a_503_never_consumes_the_apps_proof_nonce():
    """Review Runde 2, F-4: die Verfuegbarkeitspruefungen stehen VOR dem Beweis — nach
    einer 503 ist derselbe Beweis noch gueltig und traegt die Nachricht beim naechsten Mal."""
    async with world() as w:
        ctx, app, headers = await w.device()
        cid = (await (await w.create("chat-app", client=app, headers=headers)).json())["conversation_id"]
        message = {"conversation_id": cid, "client_message_id": "msg-app-0001", "text": "Hallo SOLVIO, was laeuft?"}
        payload = await w.app_proof(ctx, app, headers, message)
        provider = w.app[CE.ROUTER_PROVIDER_KEY]
        w.app[CE.ROUTER_PROVIDER_KEY] = lambda: None
        try:
            res = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
            require_equal(res.status, 503, await res.text())
        finally:
            w.app[CE.ROUTER_PROVIDER_KEY] = provider
        res = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=headers)
        require_equal(res.status, 202, "the nonce was consumed by a refused send: " + await res.text())
        # Ohne bekannte Geraeteidentitaet sagt der Weg nichts — auch keine 503 (Review Runde 3, F3-2).
        w.app[CE.ROUTER_PROVIDER_KEY] = lambda: None
        try:
            stranger = {"X-Device-Id": "device-unknown", "X-Transport-Cred": headers["X-Transport-Cred"]}
            res = await app.post(f"{CE.PREFIX}/{cid}/messages", json=payload, headers=stranger)
            require_equal(res.status, 401, await res.text())
        finally:
            w.app[CE.ROUTER_PROVIDER_KEY] = provider


async def t_a_foreign_principal_learns_nothing_from_a_replay():
    """Review Runde 2, C2-5: der Speicher prueft Eigentum VOR dem Replay. Der Endpunkt haelt
    einen fremden Principal schon an der Challenge (404) bzw. am Browser-Owner (401) auf —
    die Speicherreihenfolge ist die zweite Schranke und wird direkt gemessen."""
    async with world() as w:
        cid = await w.chat()
        res = await w.send(cid, "Vergleiche drei Hotels in Hamburg.", client_message_id="msg-0001")
        require_equal(res.status, 202, await res.text())
        row = w.chat_store.deliveries(cid)[0]
        kw = dict(conversation_id=cid, client_message_id="msg-0001", text="Vergleiche drei Hotels in Hamburg.",
                  digest=row["digest"], source_kind=row["source_kind"], source_ref=row["source_ref"],
                  core_id=row["core_id"], source_generation=row["source_generation"], device_id="")
        again, created = w.chat_store.accept_delivery(principal="local-owner", **kw)
        require_equal((again["delivery_id"], created), (row["delivery_id"], False), "the owner's replay changed")
        try:
            w.chat_store.accept_delivery(principal="someone-else", **kw)
        except ConversationStoreError as exc:
            require_equal(str(exc), "unknown_conversation")
        else:
            raise AssertionError("a foreign principal received the delivery row on replay")
        require_equal(len(w.chat_store.deliveries(cid)), 1)

if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
