"""Public browser entrance -> real LiveSession -> actual Core task authority.

Only provider audio/events and the selector reply are synthetic. No provider
connection, human microphone or permanent store is opened by this suite.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
import test_browser_voice_endpoint as H
from solvio.conversation import ConversationStore
from solvio.realtime import core_server as CS, live_session as L, live_protocol as P
from solvio.agent_runtime.voice_delegate import LiveSelection
from solvio.realtime.live_status_tools import CoreStatusTool
from solvio.realtime.control import CoreControl
from solvio.agent_runtime.store import AgentRunLedger
from solvio.agent_runtime.orchestrator import Orchestrator
from solvio.capabilities.agent import AgentCapabilities, SPECS
from solvio.capabilities.router import CapabilityRouter
from solvio.capabilities.invocation import CapabilityInvocationGate
from solvio.tools.dispatcher import ToolDispatcher
from solvio.tools.agent_capability_tools import AgentCapabilityTool

TEXT = "Vergleiche drei Hotels in Hamburg mit belastbaren Quellen."
PCM = b"\x11\x00" * 320


def transcript(text=TEXT, start=0, end=1000, event="input-1", role="input"):
    return {"type": f"session.{role}_transcript.delta", "delta": text,
            "start_ms": start, "end_ms": end, "event_id": event}


def delegate(key="delegate-1", offset=1000):
    return {"type": "session.delegation.created", "offset_ms": offset,
            "event_id": "notice-" + key,
            "delegation": {"type": "delegation", "target": "client", "id": key}}


class Provider:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.release = None
        self.closed = False
        self.final = True
        self.started = None

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.start":
            self.started = dict(event["session"], id="live-test", status="active")
            await self.events.put({"type": "session.started", "session": self.started, "event_id": "ready"})
        elif event["type"] == "session.close" and self.final:
            await self.events.put({"type": "session.closed", "event_id": "closed", "session": self.started,
                                   "reason": "close_requested", "usage": {"seconds": 1.25}})

    async def recv(self):
        return json.dumps(await self.events.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def close(self):
        self.closed = True


class Selector:
    def __init__(self):
        self.calls = []
        self.release = None
        self.entered = asyncio.Event()
        self.selection = LiveSelection(calls=({"name": "agent_task_research", "arguments": {"objective": TEXT}},))

    def bind(self, **snapshot):
        return snapshot

    async def choose(self, snapshot):
        self.calls.append(snapshot)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return self.selection


@asynccontextmanager
async def world():
    with tempfile.TemporaryDirectory(prefix="solvio-gpt-live-public-") as folder, patch.dict(os.environ, {"SOLVIO_STATE_DIR": folder}):
        async with H.world() as w:
            w.server.model = "gpt-live-1"
            w.server.conversations = ConversationStore(str(Path(folder) / "conversations.sqlite3")).open()
            w.ledger = AgentRunLedger(str(Path(folder) / "agents.sqlite3"))
            w.router = CapabilityRouter()
            w.orch = Orchestrator(ledger=w.ledger, router=w.router, require_task_authority=True)
            w.router.register(SPECS["agent_task_research"], AgentCapabilities(w.orch).research)
            w.gate = CapabilityInvocationGate()
            dispatcher = ToolDispatcher()
            dispatcher.capability_gate = w.gate
            dispatcher.agent_runtime = w.orch
            dispatcher.register(AgentCapabilityTool("agent_task_research", w.router, w.gate, w.ledger))
            w.selector = Selector()
            dispatcher.live_backend = w.selector
            w.server.dispatcher = dispatcher
            real_session = L.LiveSession
            def create(server, socket):
                value = real_session(server, socket)
                w.sessions.append(value)
                return value
            async def connect(url, **kwargs):
                require_equal(url, P.LIVE_URL)
                provider = Provider()
                w.providers.append(provider)
                return provider
            with patch.object(CS, "create_session", create), patch.object(CS, "ws_connect", connect), \
                    patch.object(L, "CONTEXT_SETTLE_SECONDS", 0.025), patch.object(L, "CONTEXT_WAIT_SECONDS", 0.15), \
                    patch.object(L, "CLOSE_WAIT_SECONDS", 0.1):
                yield w


async def until(predicate, seconds=2):
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def end(w, ws, expected="closed_confirmed"):
    await ws.send_json({"type": "session_end"})
    closed = await H.receive(ws, "session_closed")
    require_equal(closed["provider"], expected)
    await H.closed(ws)
    require(w.providers[0].closed)


async def t_start_input_output_and_end_use_only_live_protocol_through_https():
    async with world() as w:
        ws, auth = await H.started(w)
        provider = w.providers[0]
        require_equal(provider.started["delegation"], {"type": "client"})
        require_equal(provider.started["audio"]["format"], {"type": "audio/pcm", "rate": 16000})
        require_equal(provider.started["store"], False)
        await ws.send_bytes(PCM)
        await until(lambda: any(e["type"] == "session.input_audio.append" for e in provider.sent))
        sent = next(e for e in provider.sent if e["type"] == "session.input_audio.append")
        require_equal(base64.b64decode(sent["audio"]), PCM)
        await provider.events.put({"type": "session.output_audio.delta", "delta": base64.b64encode(PCM).decode()})
        require_equal((await asyncio.wait_for(ws.receive(), 1)).data, PCM)
        # Input during output remains real PCM, not the old utterance guard's silence.
        await ws.send_bytes(PCM)
        await until(lambda: len([e for e in provider.sent if e["type"] == "session.input_audio.append"]) == 2)
        require_equal(base64.b64decode(provider.sent[-1]["audio"]), PCM)
        await end(w, ws)
        require(not any(e["type"].startswith(("response.", "conversation.", "input_audio_buffer.")) for e in provider.sent))
        require_equal(w.sessions[0]._live_usage.seconds, 1.25)


async def t_delegation_before_transcript_one_real_task_survives_browser_close():
    async with world() as w:
        ws, auth = await H.started(w)
        provider = w.providers[0]
        await provider.events.put(delegate())
        await provider.events.put(transcript())
        await provider.events.put(delegate())
        await until(lambda: len(w.ledger.recent_runs()) == 1)
        await until(lambda: any(e["type"] == "session.commentary.append" for e in provider.sent))
        require_equal(len(w.selector.calls), 1)
        run = w.ledger.recent_runs()[0]
        grant = w.orch.task_authority.for_run(run.run_id)
        require_equal(grant.receipt_method, "dashboard_session")
        require_equal(await w.store.list_pending(), [])
        require_equal(w.selector.calls[0]["source"].source_kind, "dashboard")
        require_equal(w.selector.calls[0]["user_text"], TEXT)
        await end(w, ws)
        require(w.orch.task_authority.active(grant.reference, task_id=run.task_id, run_id=run.run_id).allowed)
        require_equal(len(w.ledger.recent_runs()), 1)


async def t_delegation_without_transcript_does_not_invoke_selector_or_create_task():
    async with world() as w:
        ws, _ = await H.started(w)
        provider = w.providers[0]
        await provider.events.put(delegate())
        await until(lambda: any(e["type"] == "session.commentary.append" for e in provider.sent))
        require_equal(w.selector.calls, [])
        require_equal(w.ledger.recent_runs(), [])
        await end(w, ws)


async def t_correction_during_selection_invalidates_old_tool_request():
    async with world() as w:
        w.selector.release = asyncio.Event()
        ws, _ = await H.started(w)
        provider = w.providers[0]
        await provider.events.put(transcript())
        await provider.events.put(delegate())
        await asyncio.wait_for(w.selector.entered.wait(), 1)
        await provider.events.put(transcript(" Nein, noch nicht starten.", 1000, 1700, "correction"))
        await until(lambda: w.sessions[0]._revision > w.selector.calls[0]["revision"])
        w.selector.release.set()
        await asyncio.wait_for(w.sessions[0]._tool_queue.join(), 1)
        require_equal(w.ledger.recent_runs(), [])
        require_equal(await w.store.list_pending(), [])
        await end(w, ws)


async def t_close_while_selector_runs_never_admits_task_or_late_commentary():
    async with world() as w:
        w.selector.release = asyncio.Event()
        ws, _ = await H.started(w)
        provider = w.providers[0]
        await provider.events.put(transcript())
        await provider.events.put(delegate())
        await asyncio.wait_for(w.selector.entered.wait(), 1)
        await end(w, ws)
        w.selector.release.set()
        await asyncio.sleep(0)
        require_equal(w.ledger.recent_runs(), [])
        close_index = next(i for i,e in enumerate(provider.sent) if e["type"] == "session.close")
        require(not any(e["type"] == "session.commentary.append" for e in provider.sent[close_index:]))


async def t_socket_close_without_final_event_remains_unknown():
    async with world() as w:
        ws, _ = await H.started(w)
        w.providers[0].final = False
        await end(w, ws, "unknown")
        require_equal(w.sessions[0]._live_usage, None)


async def t_no_response_done_needed_for_inactivity_end():
    async with world() as w:
        w.server.idle_timeout = 0.08
        ws, _ = await H.started(w)
        await w.providers[0].events.put({"type": "session.output_audio.delta", "delta": base64.b64encode(PCM).decode()})
        closed = await H.receive(ws, "session_closed")
        require_equal(closed["provider"], "closed_confirmed")
        await H.closed(ws)
        require(w.providers[0].closed)


async def t_unknown_status_text_never_creates_a_note_or_task():
    async with world() as w:
        control = CoreControl(w.server.dispatcher, socket_path="unused")
        w.server.dispatcher.register(CoreStatusTool(control, "note_status"))
        w.selector.selection = LiveSelection(calls=({"name": "note_status", "arguments": {"text": "unknown"}},))
        ws, _ = await H.started(w)
        provider = w.providers[0]
        await provider.events.put(transcript("Hast du die Notiz geschrieben?"))
        await provider.events.put(delegate())
        await until(lambda: any(e["type"] == "session.commentary.append" for e in provider.sent))
        require_equal(w.ledger.recent_runs(), [])
        require_equal(w.ledger.starts_for_capability("note_write"), [])
        require_equal(await w.store.list_pending(), [])
        result = w.sessions[0]._tool_results[0]
        require_equal(result["data"]["found"], False)
        await end(w, ws)


async def t_muted_input_uses_local_silence_without_claiming_microphone_forwarding():
    async with world() as w:
        ws, _ = await H.started(w)
        provider, sess = w.providers[0], w.sessions[0]
        await until(lambda: any(e["type"] == "session.input_audio.append" for e in provider.sent))
        for event in provider.sent:
            if event["type"] == "session.input_audio.append":
                require_equal(base64.b64decode(event["audio"]), bytes(640))
        require_equal(sess._frames_in, 0)
        snapshot = w.server.audio_observations.snapshot()
        require(all(row.forwarded_at is None for row in w.server.audio_observations._rows.values()))
        # Progress continues, but inactivity still ends a muted conversation.
        w.server.idle_timeout = 0.05
        closed = await H.receive(ws, "session_closed")
        require_equal(closed["provider"], "closed_confirmed")
        await H.closed(ws)


async def t_verified_personal_observation_uses_existing_learning_entry_not_task_start():
    async with world() as w:
        w.selector.selection = LiveSelection(observation=True)
        offered = []
        def offer(session, text, **kwargs):
            offered.append((session.channel, text, kwargs))
        with patch.object(L.LiveSession, "_offer_adaptive", offer):
            ws, _ = await H.started(w)
            provider = w.providers[0]
            await provider.events.put(transcript("Ich mag Cafébesuche."))
            await provider.events.put(delegate())
            await until(lambda: len(offered) == 1)
            require_equal(offered[0][0:2], ("voice_browser", "Ich mag Cafébesuche."))
            require(offered[0][2]["message_id"])
            require_equal(w.ledger.recent_runs(), [])
            await end(w, ws)


async def t_c3_a_bound_chat_feeds_the_live_session_and_a_vanished_chat_is_refused_not_degraded():
    """N8/C3 §7 fuer den Live-Weg: `_load_history` bindet den Chat des Tickets mit dem
    Principal des Sitzungsbeweises; ein verlangter, aber verschwundener Chat endet 4401."""
    async with world() as w:
        store = w.server.conversations
        chat, _ = store.create_conversation(owner_principal="local-owner", kind="text", client_request_id="chat-live-0001")
        cid = chat["conversation_id"]
        store.add_message(cid, "user", "Merk dir das Testwort Zaunkoenig.")
        store.add_message(cid, "assistant", "Zaunkoenig, gemerkt.")
        before = store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        response = await w.client.post(H.V.SESSION_PATH, json={"conversation_id": cid}, headers=w.headers)
        require_equal(response.status, 201, await response.text())
        ws, _ = await H.authenticated(w, (await response.json())["nonce"])
        await ws.send_json({"type": "session_start"})
        await H.receive(ws, "session_ready")
        sess = w.sessions[0]
        require_equal((sess.bound_conversation_id, sess.conversation_id, sess.conversation_mode), (cid, cid, "active"))
        require_equal(sess.conversation_principal(), "local-owner")
        start = next(e for e in w.providers[0].sent if e["type"] == "session.start")
        texts = [item["content"][0]["text"] for item in start["session"]["input"]]
        require_equal(texts, ["Merk dir das Testwort Zaunkoenig.", "Zaunkoenig, gemerkt."])
        require_equal(store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], before)
        await end(w, ws)
        # Zwischen Socket und session_start geloescht: verweigert, nie degradiert, 4401 statt 1000.
        other, _ = store.create_conversation(owner_principal="local-owner", kind="text", client_request_id="chat-live-0002")
        nonce = (await (await w.client.post(H.V.SESSION_PATH, json={"conversation_id": other["conversation_id"]},
                                            headers=w.headers)).json())["nonce"]
        ws = await w.client.ws_connect(H.V.PATH, headers={"Origin": w.origin})
        await ws.send_json({"type": "session_auth", "nonce": nonce})
        await H.receive(ws, "session_authenticated")
        store.delete_conversation(other["conversation_id"])
        await ws.send_json({"type": "session_start"})
        await H.closed_with(ws, 4401)
        await asyncio.wait_for(asyncio.gather(*tuple(w.endpoint.handlers), return_exceptions=True), 2)
        sess = w.sessions[-1]
        require_equal((sess.conversation_mode, sess.conversation_id, sess.active), ("refused", None, False))
        require_equal(store.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], before)
        require(not w.server._busy)


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
