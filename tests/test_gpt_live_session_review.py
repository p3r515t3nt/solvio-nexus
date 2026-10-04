"""Independent Live lifecycle regressions through the real local HTTPS entrance.

Reuse the authenticated temporary-store world. Only upstream provider envelopes
and selector output are synthetic; no microphone, provider, or production data.
"""
import asyncio
import base64
import json
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_gpt_live_session as W

USER_CONTEXT = "Hallo, bist du noch da?"
ASSISTANT_CONTEXT = "Ja, ich höre dir zu."


def conversation_rows(w):
    return [{key: row[key] for key in ("role", "text")}
        for row in w.server.conversations.recent_context(w.sessions[0].conversation_id)]


async def ordinary_context(w):
    provider, session = w.providers[0], w.sessions[0]
    await provider.events.put(W.transcript("Hallo, ", 0, 300, "ordinary-a"))
    await provider.events.put(W.transcript("bist du noch da?", 300, 1000, "ordinary-b"))
    await provider.events.put(W.transcript(ASSISTANT_CONTEXT, 1100, 1800, "ordinary-c", role="output"))
    await W.until(lambda: len(session._seen_fragments) == 3)


def observe_learning(w):
    session = w.sessions[0]
    observer = Mock(wraps=session._offer_adaptive)
    session._offer_adaptive = observer
    return observer


async def t_ordinary_conversation_without_delegation_preserves_both_sides_on_close():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        learning = observe_learning(w)
        await ordinary_context(w)
        await W.end(w, ws)
        require_equal(conversation_rows(w), [
            {"role": "user", "text": USER_CONTEXT},
            {"role": "assistant", "text": ASSISTANT_CONTEXT}])
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        require_equal(learning.call_count, 0)


async def t_reconnect_preserves_uncommitted_context_before_loading_new_session():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        learning = observe_learning(w)
        await ordinary_context(w)
        await w.providers[0].events.put({"type": "session.usage.updated", "event_id": "lost-provider",
                                        "usage": {"seconds": "malformed"}})
        await W.until(lambda: len(w.providers) == 2 and w.sessions[0].active)
        expected = [{"role": "user", "text": USER_CONTEXT},
                    {"role": "assistant", "text": ASSISTANT_CONTEXT}]
        require_equal(conversation_rows(w), expected)
        actual_input = [{"role": message["role"], "text": message["content"][0]["text"]}
                        for message in w.providers[1].started["input"]]
        require_equal(actual_input, expected)
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        await W.end(w, ws)
        require(all(provider.closed for provider in w.providers))
        require_equal(conversation_rows(w), expected, "reconnect/close duplicated fragments")
        require_equal(learning.call_count, 0)


async def t_cancelled_selection_preserves_correction_without_duplicating_original():
    async with W.world() as w:
        w.selector.release = asyncio.Event()
        ws, _ = await W.H.started(w)
        learning = observe_learning(w)
        provider, session = w.providers[0], w.sessions[0]
        await provider.events.put(W.transcript())
        await provider.events.put(W.delegate())
        await asyncio.wait_for(w.selector.entered.wait(), 1)
        revision = session._revision
        await provider.events.put(W.transcript(" Nein, noch nicht starten.", 1000, 1700, "cancelled-correction"))
        await W.until(lambda: session._revision > revision)
        await W.end(w, ws)
        require_equal(conversation_rows(w), [
            {"role": "user", "text": W.TEXT},
            {"role": "user", "text": "Nein, noch nicht starten."}])
        require_equal(len(w.selector.calls), 1)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])
        require_equal(learning.call_count, 0)


async def t_last_transcripts_during_provider_close_are_durable_before_worker_cancel():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        learning = observe_learning(w)
        provider = w.providers[0]
        original = provider.send

        async def closing_tail(raw):
            if json.loads(raw)["type"] == "session.close":
                await provider.events.put(W.transcript(USER_CONTEXT, 0, 1000, "closing-user"))
                await provider.events.put(W.transcript(ASSISTANT_CONTEXT, 1100, 1800, "closing-assistant", role="output"))
            await original(raw)

        provider.send = closing_tail
        await W.end(w, ws)
        require_equal(conversation_rows(w), [
            {"role": "user", "text": USER_CONTEXT},
            {"role": "assistant", "text": ASSISTANT_CONTEXT}])
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        require_equal(learning.call_count, 0)


async def t_regressing_final_usage_keeps_measured_consumption_and_unknown_close():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        provider, session = w.providers[0], w.sessions[0]
        await provider.events.put({"type": "session.usage.updated", "event_id": "usage-30",
                                   "usage": {"seconds": 30}})
        await W.until(lambda: session._live_usage is not None)
        require_equal(session._live_usage.seconds, 30)
        # The fixture's close event has 1.25 s for the same provider session.
        await W.end(w, ws, "unknown")
        require_equal(session._live_usage.seconds, 30)
        require(not session._live_usage.final)
        require(not session._live_closed.is_set())
        require_equal(session._live_final_provider, None)


async def t_other_provider_session_cannot_confirm_this_close():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        provider, session = w.providers[0], w.sessions[0]
        expected = session._live_provider_id
        provider.started = dict(provider.started, id="another-provider-session")
        await W.end(w, ws, "unknown")
        require_equal(session._live_provider_id, expected)
        require_equal(session._live_usage, None)
        require(not session._live_closed.is_set())
        require_equal(session._live_final_provider, None)


async def t_continuous_quiet_pcm_does_not_extend_idle_deadline():
    async with W.world() as w:
        w.server.idle_timeout = 0.08
        ws, _ = await W.H.started(w)
        provider, session = w.providers[0], w.sessions[0]
        quiet = (b"\x00\x00" + b"\x3f\x00" + b"\xc1\xff") * 100
        before = session.last_activity
        sent = []
        async def stream():
            while True:
                await provider.events.put({"type": "session.output_audio.delta",
                    "delta": base64.b64encode(quiet).decode()})
                sent.append(True)
                await asyncio.sleep(0.04)
        sender = asyncio.create_task(stream())
        try:
            # Real timer is used; no manual timer event/clock mutation.
            closed = await asyncio.wait_for(W.H.receive(ws, "session_closed"), 0.9)
            require_equal(closed["provider"], "closed_confirmed")
            await W.H.closed(ws)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        require(len(sent) >= 2, "probe did not supply a continuous silent stream")
        require_equal(session.last_activity, before)
        require(provider.closed)
        require_equal(w.ledger.recent_runs(), [])


async def t_stale_delegation_cannot_acquire_later_input_but_fresh_one_can():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        provider, session = w.providers[0], w.sessions[0]
        await provider.events.put(W.delegate("old", offset=1000))
        await provider.events.put(W.transcript(start=20000, end=23000, event="later-request"))
        await W.until(lambda: any(e["type"] == "session.commentary.append" for e in provider.sent))
        await asyncio.wait_for(session._tool_queue.join(), 1)
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])
        # The rejected notice must not consume the actual current request.
        await provider.events.put(W.delegate("fresh", offset=23000))
        await W.until(lambda: len(w.ledger.recent_runs()) == 1)
        await asyncio.wait_for(session._tool_queue.join(), 1)
        require_equal(len(w.selector.calls), 1)
        require_equal(w.selector.calls[0]["delegation_id"], "fresh")
        require_equal(w.selector.calls[0]["user_text"], W.TEXT)
        require_equal(await w.store.list_pending(), [])
        await W.end(w, ws)


async def t_malformed_pcm_is_dropped_without_losing_next_good_frame():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        provider, session = w.providers[0], w.sessions[0]
        await provider.events.put({"type": "session.output_audio.delta", "delta": "not!base64"})
        await provider.events.put({"type": "session.output_audio.delta",
                                   "delta": base64.b64encode(W.PCM).decode()})
        msg = await asyncio.wait_for(ws.receive(), 0.6)
        require_equal(msg.data, W.PCM)
        require_equal(session.dropped_audio_frames, 1)
        require(session.active and not session.reader.done())
        require_equal(len(w.providers), 1, "bad frame caused a provider reconnect")
        require_equal(w.ledger.recent_runs(), [])
        await W.end(w, ws)


async def bound_start(w, conversation_id):
    response = await w.client.post(W.H.V.SESSION_PATH,
        json={"conversation_id": conversation_id}, headers=w.headers)
    require_equal(response.status, 201)
    ws, auth = await W.H.authenticated(w, (await response.json())["nonce"])
    await ws.send_json({"type": "session_start"})
    ready = await W.H.receive(ws, "session_ready")
    require_equal(ready["session_id"], auth["session_id"])
    require_equal(ready["conversation_id"], conversation_id)
    require_equal(ready["handoff_protocol"], 1)
    return ws, ready


async def t_handoff_waits_for_final_transcript_then_next_voice_reads_typed_reply():
    async with W.world() as w:
        store = w.server.conversations
        chat, _ = store.create_conversation(owner_principal="local-owner", kind="text",
                                            client_request_id="voice-text-transition")
        cid = chat["conversation_id"]
        store.add_message(cid, "user", "Wir planen ein vegetarisches Essen.")
        ws, ready = await bound_start(w, cid)
        provider = w.providers[0]
        require_equal(provider.started["input"][0]["content"][0]["text"],
                      "Wir planen ein vegetarisches Essen.")
        original = provider.send

        async def closing_tail(raw):
            if json.loads(raw)["type"] == "session.close":
                await provider.events.put(W.transcript(USER_CONTEXT, 0, 1000, "handoff-user"))
                await provider.events.put(W.transcript(ASSISTANT_CONTEXT, 1100, 1800,
                                                       "handoff-assistant", role="output"))
            await original(raw)

        provider.send = closing_tail
        await ws.send_json({"type": "session_end"})
        flushed = await W.H.receive(ws, "conversation_flushed")
        require_equal(flushed, {"type": "conversation_flushed", "session_id": ready["session_id"],
            "conversation_id": cid, "status": "complete", "provider_closed": True})
        require_equal([r["text"] for r in store.recent_context(cid)],
                      ["Wir planen ein vegetarisches Essen.", USER_CONTEXT, ASSISTANT_CONTEXT])
        closed = await W.H.receive(ws, "session_closed")
        require_equal(closed["provider"], "closed_confirmed")
        await W.H.closed(ws)
        await W.until(lambda: not w.server._busy)
        # This is the existing canonical text store, not another voice ledger.
        store.add_message(cid, "user", "Dann nehmen wir die Gemüsepfanne.")
        store.add_message(cid, "assistant", "Die Gemüsepfanne ist ausgewählt.")
        old_providers = w.providers[:]
        w.providers.clear()  # H.authenticated asserts that authentication opens no provider.
        ws2, ready2 = await bound_start(w, cid)
        require(ready2["session_id"] != ready["session_id"])
        texts = [m["content"][0]["text"] for m in w.providers[0].started["input"]]
        require_equal(texts, ["Wir planen ein vegetarisches Essen.", USER_CONTEXT,
            ASSISTANT_CONTEXT, "Dann nehmen wir die Gemüsepfanne.", "Die Gemüsepfanne ist ausgewählt."])
        await W.end(w, ws2)
        require(all(p.closed for p in old_providers + w.providers))
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])


async def t_handoff_does_not_confirm_missing_provider_final():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        await ordinary_context(w)
        w.providers[0].final = False
        await ws.send_json({"type": "session_end"})
        flushed = await W.H.receive(ws, "conversation_flushed")
        require_equal((flushed["status"], flushed["provider_closed"]), ("unknown", False))
        require_equal(conversation_rows(w), [
            {"role": "user", "text": USER_CONTEXT},
            {"role": "assistant", "text": ASSISTANT_CONTEXT}])
        await W.H.closed(ws)


async def t_new_provider_final_cannot_erase_an_unconfirmed_previous_generation():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        await ordinary_context(w)
        w.providers[0].final = False
        await w.providers[0].events.put({"type": "session.usage.updated", "event_id": "lost-provider",
                                        "usage": {"seconds": "malformed"}})
        await W.until(lambda: len(w.providers) == 2 and w.sessions[0].active)
        require(w.sessions[0]._handoff_prior_unconfirmed)
        await ws.send_json({"type": "session_end"})
        flushed = await W.H.receive(ws, "conversation_flushed")
        require_equal((flushed["status"], flushed["provider_closed"]), ("unknown", True))
        await W.H.closed(ws)


async def t_handoff_does_not_confirm_failed_canonical_write():
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        with patch.object(w.server.conversations, "add_message", side_effect=OSError("synthetic storage failure")):
            await ordinary_context(w)
            await ws.send_json({"type": "session_end"})
            flushed = await W.H.receive(ws, "conversation_flushed")
            require_equal(flushed["status"], "unknown")
            require(flushed["provider_closed"])
            require(w.sessions[0].persist_failures > 0)
            await W.H.closed(ws)
        require_equal(conversation_rows(w), [])


async def t_handoff_ack_cannot_overtake_an_inflight_store_write():
    import threading
    async with W.world() as w:
        ws, _ = await W.H.started(w)
        await ordinary_context(w)
        entered, release = threading.Event(), threading.Event()
        original = w.server.conversations.add_message

        def delayed_write(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("synthetic delayed write")
            return original(*args, **kwargs)

        with patch.object(w.server.conversations, "add_message", side_effect=delayed_write):
            await ws.send_json({"type": "session_end"})
            ack = asyncio.create_task(W.H.receive(ws, "conversation_flushed"))
            try:
                await W.until(entered.is_set)
                require(not ack.done(), "handoff acknowledged while the canonical write is blocked")
            finally:
                release.set()
            flushed = await ack
            require_equal(flushed["status"], "complete")
            require_equal(len(conversation_rows(w)), 2)
            await W.H.closed(ws)


@asynccontextmanager
async def native_world():
    import tempfile
    from aiohttp import TCPConnector, web
    from aiohttp.test_utils import TestClient, TestServer
    import mobile_attest_helper as mobile
    from test_dashboard_task_approval import _wire
    from solvio import voice_endpoint as endpoint, voice_session_proof as proof
    from solvio.security.mobile_approval import app_attest

    async with W.world() as w:
        with tempfile.TemporaryDirectory(prefix="solvio-iphone-handoff-") as folder:
            approvals, cp, _, _ = await _wire(folder)
            device = await mobile.enroll_attested(cp, transport_cred="synthetic-handoff-credential")
            app = web.Application()
            app["control_plane"] = cp
            endpoint.attach(app, w.server)
            server_tls, client_tls = W.H._tls(folder)
            server = TestServer(app, scheme="https")
            await server.start_server(ssl=server_tls)
            client = TestClient(server, connector=TCPConnector(ssl=client_tls))
            await client.start_server()
            try:
                chat, _ = w.server.conversations.create_conversation(owner_principal="local-owner",
                    kind="text", client_request_id="iphone-handoff-chat")
                cid = chat["conversation_id"]
                ws = await client.ws_connect(endpoint.PATH, headers={
                    "X-Device-Id": device.device_id, "X-Transport-Cred": "synthetic-handoff-credential"})
                challenge = await W.H.receive(ws, "session_challenge")
                raw = proof.canonical_bytes(proof.build_binding(core_instance_id=cp.core_instance_id,
                    device_id=device.device_id, session_nonce=challenge["session_nonce"], conversation_id=cid))
                assertion = app_attest.fake_assertion(device.aakey, proof.client_data_hash(raw), 1)
                await ws.send_json({"type": "session_assertion", "session_nonce": challenge["session_nonce"],
                    "conversation_id": cid, "assertion": base64.b64encode(assertion).decode()})
                await ws.send_json({"type": "session_start"})
                ready = await W.H.receive(ws, "session_ready")
                require_equal((ready["conversation_id"], ready["handoff_protocol"]), (cid, 1))
                yield w, ws, ready
            finally:
                await client.close()
                await server.close()
                await approvals.close()


async def t_iphone_signed_chat_receives_flush_after_stop_before_socket_closes():
    async with native_world() as (w, ws, ready):
        await ordinary_context(w)
        await ws.send_json({"type": "session_end"})
        await W.H.receive(ws, "session_end")
        flushed = await W.H.receive(ws, "conversation_flushed")
        require_equal(flushed, {"type": "conversation_flushed", "session_id": ready["session_id"],
            "conversation_id": ready["conversation_id"], "status": "complete", "provider_closed": True})
        require_equal(conversation_rows(w), [
            {"role": "user", "text": USER_CONTEXT}, {"role": "assistant", "text": ASSISTANT_CONTEXT}])
        require_equal(w.sessions[0].channel, "voice_iphone")
        require_equal(w.sessions[0].conversation_principal(), "local-owner")
        await ws.close()
        await W.until(lambda: not w.server._busy)
        require_equal(w.sessions[0]._draining, [])


async def t_iphone_disconnect_cannot_release_busy_before_an_existing_close_finishes():
    async with native_world() as (w, ws, _):
        await ordinary_context(w)
        provider, session = w.providers[0], w.sessions[0]
        entered, release = asyncio.Event(), asyncio.Event()
        original = provider.send

        async def delayed_close(raw):
            if json.loads(raw)["type"] == "session.close":
                entered.set()
                await release.wait()
            await original(raw)

        provider.send = delayed_close
        close = asyncio.create_task(session.close(reason="timeout"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await ws.close()
            await asyncio.sleep(0.02)
            require(w.server._busy, "a new conversation could start while the previous close is pending")
            require(not close.done())
        finally:
            release.set()
            await asyncio.gather(close, return_exceptions=True)
        await W.until(lambda: not w.server._busy)
        require(provider.closed)
        require_equal(session._draining, [])
        require_equal(len(conversation_rows(w)), 2)


async def t_legacy_socket_close_is_not_a_final_transcript_handoff():
    async with W.world() as w:
        session = W.CS.Session(w.server, type("Socket", (), {"send": Mock()})())
        session.channel = "voice_iphone"
        session.conversation_id = "c-0123456789abcdef"
        session.conversation_mode = "active"
        messages = []
        async def send(raw):
            messages.append(json.loads(raw))
        session.ws.send = send
        require("handoff_protocol" not in session._ready_message())
        await session._confirm_conversation_flush(provider_closed=True)
        require_equal(messages[0]["status"], "unknown")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
