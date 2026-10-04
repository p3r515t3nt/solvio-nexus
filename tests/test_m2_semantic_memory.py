"""M2 — Langzeitgedaechtnis, getrennt vom Gespraechsverlauf.

DER UNTERSCHIED, UM DEN ES GEHT

`conversations.sqlite3` (M1) haelt den woertlichen juengsten Verlauf. `memory.sqlite3`
haelt ausgewaehltes dauerhaftes Wissen. Eine Nachricht im Verlauf ist NICHT automatisch
Gedaechtnis — dauerhaftes Wissen entsteht nur ueber den ausdruecklichen Schreibweg.

WAS DIESE SUITE VOR ALLEM SCHUETZT

Dass ein Modell sich kein Gedaechtnis selbst verschafft. Es darf vorschlagen; ob
geschrieben wird, entscheidet SOLVIO an der Aeusserung des lokalen Besitzers. Ohne
diesen Riegel koennte ein Werkzeugergebnis oder die eigene Antwort des Modells zu
dauerhaftem "Wissen" werden, das spaeter als Nutzeraussage zurueckkommt.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und ueberleben `-O`.

Direkt: python tests/test_m2_semantic_memory.py
"""
import asyncio
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
from solvio.conversation import ConversationStore  # noqa: E402
from solvio.memory.intent import detect  # noqa: E402
from solvio.memory.service import MemoryService, MemoryUnavailable  # noqa: E402
from solvio.realtime import core_server as CS  # noqa: E402
from solvio.tools.base import ToolResult  # noqa: E402
from solvio.tools.memory_tools import (MemoryIntentGate, MemoryRememberTool,  # noqa: E402
                                       MemorySearchTool)

PCM = base64.b64encode(b"\x01\x02" * 240).decode("ascii")
FACT = "Mein Langzeit-Testwort ist Bernstein 84"


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class _Log:
    def __init__(self):
        self.events = []

    def _rec(self, event, **kw):
        self.events.append((event, kw))

    info = warning = error = _rec

    def of(self, name):
        return [kw for event, kw in self.events if event == name]


def _service(tmp, clock=None):
    return MemoryService(os.path.join(tmp, "memory"),
                         now_fn=clock or (lambda: 1_000_000.0)).open()


async def _remember(svc, sentence, **kw):
    intent = detect(sentence)
    require(intent is not None, f"the fixture sentence carries no intent: {sentence!r}")
    return await svc.remember(intent, conversation_id=kw.pop("conversation_id", "c-1"), **kw)


# =====================================================================
# A, B, C — wer darf Gedaechtnis erzeugen
# =====================================================================
async def t_a_explicit_intent_creates_durable_memory():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            result = await _remember(svc, f"Merk dir dauerhaft: {FACT}.",
                                     source_message_id="m-7", source_session_id="s-1",
                                     source_turn_id="s-1-t1")
            require(result.ok, result.reason)
            require_equal(result.action, "created", result.action)
            record = await svc.semantic.get(result.memory_id)
            require(record is not None, "the memory was not stored")
            require("Bernstein 84" in record.content, record.content)
            require_equal(record.trust_level.value, "user_direct", record.trust_level)
            require_equal(record.source_type.value, "user_direct", record.source_type)
        finally:
            await svc.close()


async def t_b_an_ordinary_sentence_creates_no_memory():
    """V1 ist absichtlich eng: kein Satz wird nebenbei zu Langzeitwissen."""
    for sentence in ("Ich finde Kuerbissuppe ganz gut.",
                     "Das Wohnzimmerlicht ist aus.",
                     "Projekt Aurora startet im September.",
                     "Wie ist das Wetter heute?",
                     "Danke, das war alles."):
        require_equal(detect(sentence), None,
                      f"an ordinary sentence was read as memory intent: {sentence!r}")


async def t_c_assistant_text_cannot_become_memory_by_itself():
    """Das Modell darf vorschlagen. Ohne Aeusserung des Besitzers passiert nichts."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            # Der Assistent hat gerade selbst gesagt "merk dir das" — der Riegel steht auf
            # dem NUTZERTURN, nicht auf dem, was das Modell produziert.
            gate.offer_user_turn(detect("Wie spaet ist es?"), session_id="s-1",
                                 turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")
            result = await tool.run({})
            require(not result.success, "assistant output created a durable memory")
            require_equal(result.error, "no_explicit_intent", result.error)
            require_equal(await svc.semantic.semantic_count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_c_an_intent_is_spent_once():
    """Ein Satz ist ein Auftrag, nicht beliebig viele: ein zweiter Werkzeugaufruf im
    selben Turn bekommt nichts."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect(f"Merk dir: {FACT}."), session_id="s-1",
                                 turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")     # M2-Reparatur: der Turn wird angemeldet
            first = await tool.run({})
            second = await tool.run({})
            require(first.success, "the legitimate write was refused")
            require(not second.success, "the same utterance was spent twice")
            require_equal(second.error, "no_explicit_intent", second.error)
        finally:
            await svc.close()


async def t_c_the_gate_does_not_survive_into_the_next_turn():
    gate = MemoryIntentGate()
    gate.offer_user_turn(detect(f"Merk dir: {FACT}."), session_id="s-1", turn_id="s-1-t1")
    gate.offer_user_turn(detect("Wie ist das Wetter?"), session_id="s-1", turn_id="s-1-t2")
    gate.expect_turn("s-1", "s-1-t2")
    require_equal(gate.take(), None, "a previous turn's permission leaked forward")


# =====================================================================
# D, E, F — Dauerhaftigkeit
# =====================================================================
async def t_d_e_f_memory_outlives_sessions_conversations_and_processes():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        conversations = ConversationStore(os.path.join(tmp, "conversations.sqlite3"),
                                          now_fn=clock, linger_seconds=900.0).open()
        svc = _service(tmp, clock)
        conversation_id, _ = conversations.begin_session("s-1")
        await _remember(svc, f"Merk dir dauerhaft: {FACT}.", conversation_id=conversation_id)
        conversations.end_session("s-1", "inactivity_timeout")
        await svc.close()
        conversations.close()

        # Der Provider ist zu, das Gespraech laeuft ab, und der Dienst wird NEU gebaut —
        # nichts davon lebt im Speicher weiter.
        clock.advance(900.0 + 60)
        conversations = ConversationStore(os.path.join(tmp, "conversations.sqlite3"),
                                          now_fn=clock, linger_seconds=900.0).open()
        new_conversation, resumed = conversations.begin_session("s-2")
        svc = _service(tmp, clock)
        try:
            require(not resumed, "the conversation did not actually expire")
            require(new_conversation != conversation_id, "a new conversation was expected")
            hits = await svc.search("Was ist mein Langzeit-Testwort?")
            require(hits, "the durable memory did not survive")
            require("Bernstein 84" in hits[0].content, hits[0].content)
        finally:
            await svc.close()
            conversations.close()


# =====================================================================
# G, H — Abruf
# =====================================================================
async def t_g_search_finds_a_differently_worded_memory():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _remember(svc, "Merk dir: Ich trinke morgens keinen Kaffee.")
            hits = await svc.search("Trinke ich Kaffee am Morgen?")
            require(hits, "a differently worded question found nothing")
            require("Kaffee" in hits[0].content, hits[0].content)
        finally:
            await svc.close()


async def t_g_transcription_variants_still_find_the_memory():
    """Sprache wird jedes Mal anders verschriftet.

    Live beobachtet: die Aeusserung kam als "mein Langzeittestwort ist Bernstein84" an
    — zusammengeschrieben. Die spaetere Frage lautete "Was ist mein Langzeit-Testwort?".
    Auf Wortebene ist das kein einziger gemeinsamer Token, und die Suche lieferte NULL
    Treffer. Ein Gedaechtnis, das nur bei identischer Verschriftung erinnert, ist keins.
    """
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _remember(svc, "Merk dir dauerhaft: mein Langzeittestwort ist Bernstein84")
            for question in ("Was ist mein Langzeit-Testwort?",
                             "Was ist mein Langzeittestwort?",
                             "Was war mein Langzeit Testwort?",
                             "Kennst du mein Langzeit-Testwort noch?",
                             "Was ist mein Testwort?"):
                hits = await svc.search(question)
                require(hits, f"no hit for {question!r} — the wording changed, not the fact")
                require("Bernstein84" in hits[0].content, hits[0].content)
            # und die Aufweichung darf die Trennschaerfe nicht kosten
            for unrelated in ("Wie ist das Wetter heute?", "Wie hoch ist der Eiffelturm?",
                              "Schalte das Licht an."):
                require_equal(await svc.search(unrelated), [],
                              f"{unrelated!r} matched through the n-gram path")
        finally:
            await svc.close()


async def t_h_irrelevant_memories_are_not_returned():
    """Ohne Relevanzschranke lieferte hybrid_recall bei kleinem Bestand ALLES."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _remember(svc, f"Merk dir dauerhaft: {FACT}.")
            await _remember(svc, "Merk dir: Projekt Aurora startet im September.")
            for query in ("Wie ist das Wetter heute?", "Wie hoch ist der Eiffelturm?",
                          "Schalte bitte das Licht an."):
                hits = await svc.search(query)
                require_equal(hits, [], f"{query!r} returned {[h.content for h in hits]}")
        finally:
            await svc.close()


async def t_h_results_stay_within_the_budget():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            for i in range(20):
                await _remember(svc, f"Merk dir: Bernstein Notiz {i} " + "x" * 200)
            hits = await svc.search("Bernstein Notiz", top_k=3, max_chars=600)
            require(len(hits) <= 3, f"{len(hits)} hits for top_k=3")
            total = sum(len(h.content) for h in hits)
            require(total <= 600, f"{total} characters returned for a 600 budget")
        finally:
            await svc.close()


# =====================================================================
# I, J, K, L — Entdopplung, Korrektur, Loeschen
# =====================================================================
async def t_i_the_same_fact_twice_does_not_create_two_active_memories():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            first = await _remember(svc, "Merk dir: Projekt Aurora startet im September.")
            again = await _remember(svc, "merk dir projekt aurora startet im september")
            require_equal(again.action, "duplicate", again.action)
            require_equal(again.memory_id, first.memory_id, "a second copy was created")
        finally:
            await svc.close()


async def t_i_distinct_facts_are_never_merged():
    """Reproduziert: die fruehere Entdopplung verglich SORTIERTE Inhaltswoerter und warf
    einstellige Zahlen weg. Von 50 verschiedenen Fakten kamen nur 41 an — neun wurden
    stillschweigend verschluckt. Ein falsch erkanntes Duplikat verliert Wissen."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            actions = []
            for i in range(12):
                result = await _remember(svc, f"Merk dir: Notiz Nummer {i} betrifft Thema {i % 5}")
                actions.append(result.action)
            require_equal(actions, ["created"] * 12,
                          f"distinct facts were merged: {actions}")
        finally:
            await svc.close()


async def t_j_k_a_correction_supersedes_and_retrieval_prefers_the_new_truth():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            old = await _remember(svc, "Merk dir: Projekt Aurora startet im September.")
            new = await _remember(
                svc, "Korrektur, merk dir: Projekt Aurora startet im Oktober, "
                     "nicht im September.")
            require_equal(new.action, "superseded", new.action)
            require_equal(new.superseded_id, old.memory_id, new.superseded_id)
            stale = await svc.semantic.get(old.memory_id)
            require(stale.superseded_by == new.memory_id,
                    "the old fact is still active — two contradictory truths")
            hits = await svc.search("Wann startet Projekt Aurora?")
            require(hits, "the corrected fact cannot be retrieved")
            require("Oktober" in hits[0].content, f"retrieval prefers the stale fact: {hits[0].content}")
            for hit in hits:
                require(hit.memory_id != old.memory_id,
                        "the superseded fact was returned as active")
        finally:
            await svc.close()


async def t_j_an_unrelated_correction_does_not_overwrite_a_foreign_fact():
    """Falsch-negativ ist reparierbar, falsch-positiv zerstoert."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            keep = await _remember(svc, f"Merk dir dauerhaft: {FACT}.")
            other = await _remember(
                svc, "Korrektur, merk dir: Der Zahnarzttermin ist am Dienstag.")
            require_equal(other.action, "created",
                          "an unrelated correction overwrote an existing fact")
            record = await svc.semantic.get(keep.memory_id)
            require(record.superseded_by is None, "an unrelated fact was superseded")
        finally:
            await svc.close()


async def t_l_a_deleted_memory_disappears_from_search():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            result = await _remember(svc, f"Merk dir dauerhaft: {FACT}.")
            require(await svc.search("Was ist mein Langzeit-Testwort?"), "not findable before")
            await svc.delete(result.memory_id, reason="test")
            after = await svc.search("Was ist mein Langzeit-Testwort?")
            require_equal(after, [], f"the deleted memory is still searchable: {after}")
            require_equal(await svc.semantic.get(result.memory_id), None,
                          "the deleted memory is still readable")
        finally:
            await svc.close()


# =====================================================================
# M, N — Autoritaet und Inhalt
# =====================================================================
async def t_m_retrieved_memory_never_becomes_a_system_instruction():
    """Abgerufenes Gedaechtnis ist Information. Es geht als Werkzeugergebnis zurueck ins
    Gespraech — nie in die Anweisung, aus der das Modell seine Autoritaet zieht."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _remember(svc, f"Merk dir dauerhaft: {FACT}.")
            tool = MemorySearchTool(svc)
            result = await tool.run({"query": "Was ist mein Langzeit-Testwort?"})
            require(result.success, result.error)
            blob = json.dumps(result.as_dict(), ensure_ascii=False)
            require("Bernstein 84" in blob, "the memory did not come back at all")
        finally:
            await svc.close()
        # und der Instruktionsblock der Sprachschicht kennt kein Gedaechtnis
        import inspect
        configure = inspect.getsource(CS.Session._configure)
        require("memory" not in configure.lower(),
                "the session instructions reference memory — retrieval must stay a tool result")
        require("instructions" in configure, "the fixture no longer inspects the right place")


def t_m_the_search_tool_returns_no_internal_identifiers():
    """Interne Bezeichner gehoeren nicht in eine gesprochene Antwort."""
    import inspect
    source = inspect.getsource(MemorySearchTool.run)
    require("memory_id" not in source.split("return ToolResult(True, data={\"memories\"")[-1],
            "the tool result exposes internal memory identifiers")


async def t_n_secrets_and_audio_never_enter_the_memory_store():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        secret = "sk-proj-GEHEIM0123456789abcdefXYZ"
        try:
            refused = await _remember(svc, f"Merk dir: mein Passwort ist {secret}")
            require(not refused.ok, "a credential was stored in memory")
            require_equal(refused.reason, "looks_like_secret", refused.reason)
            await _remember(svc, f"Merk dir dauerhaft: {FACT}.")
        finally:
            await svc.close()
        blob = b""
        for root, _dirs, files in os.walk(os.path.join(tmp, "memory")):
            for name in files:                     # Unterverzeichnisse wie backups/ auslassen
                with open(os.path.join(root, name), "rb") as fh:
                    blob += fh.read()
        require(secret.encode() not in blob, "the credential reached the database")
        require(PCM[:32].encode() not in blob, "audio payload reached the database")
        require(b"Bernstein 84" in blob, "the fixture stored nothing at all")


# =====================================================================
# O, P — Ausfall und Hot Path
# =====================================================================
class _BrokenMemory:
    """Ein Gedaechtnis, das bei jedem Zugriff versagt."""

    base_dir = "<broken>"

    def __init__(self):
        self.provider = None

    async def remember(self, *a, **kw):
        raise MemoryUnavailable("embedding model unavailable")

    async def search(self, *a, **kw):
        raise MemoryUnavailable("index unreadable")

    async def health(self):
        return {"available": False}


async def t_o_a_broken_memory_degrades_instead_of_breaking_the_voice_path():
    broken = _BrokenMemory()
    gate = MemoryIntentGate()
    gate.offer_user_turn(detect(f"Merk dir dauerhaft: {FACT}."), session_id="s-1",
                         turn_id="s-1-t1")
    gate.expect_turn("s-1", "s-1-t1")
    remember_result = await MemoryRememberTool(broken, gate).run({})
    search_result = await MemorySearchTool(broken).run({"query": "Testwort"})
    require(not remember_result.success, "a failed write reported success")
    require_equal(remember_result.error, "memory_unavailable", remember_result.error)
    require(remember_result.human_message, "the failure would be silent to the user")
    require(not search_result.success, "a failed search reported success")
    require_equal(search_result.error, "memory_unavailable", search_result.error)
    # Keine erfundene Erinnerung.
    require(search_result.data is None or not (search_result.data or {}).get("memories"),
            "a failing search fabricated recall")


async def t_o_a_failing_memory_does_not_kill_the_provider_reader():
    class _Dispatcher:
        approvals = None

        def __init__(self):
            self.memory_gate = MemoryIntentGate()

        def parse_args(self, raw):
            return json.loads(raw) if raw else {}

        async def dispatch(self, name, args):
            raise MemoryUnavailable("index unreadable")

    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = _Dispatcher()
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "m"

    class _Provider:
        def __init__(self, frames):
            self._f = frames
            self.exhausted = asyncio.Event()
            self.sent = []

        def __aiter__(self):
            async def gen():
                for f in self._f:
                    yield f
                self.exhausted.set()
                await asyncio.sleep(3600)
            return gen()

        async def send(self, d):
            self.sent.append(d)

        async def close(self, *a, **kw):
            pass

    class _Satellite:
        def __init__(self):
            self.audio = []

        async def send(self, d):
            if isinstance(d, (bytes, bytearray)):
                self.audio.append(d)

        async def close(self, *a, **kw):
            pass

        remote_address = ("x", 1)

    sess = CS.Session(srv, _Satellite())
    sess.active = True
    sess.oa = _Provider([
        json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "memory_search", "call_id": "c1",
             "arguments": '{"query": "x"}'}]}}),
        json.dumps({"type": "response.output_audio.delta", "delta": PCM}),
        json.dumps({"type": "response.done", "response": {"output": []}}),
    ])
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=10)
        await asyncio.wait_for(sess._tool_queue.join(), timeout=10)
        require(not reader.done(), "a memory failure ended the provider reader")
        require(sess.ws.audio, "audio stopped flowing because memory failed")
    finally:
        reader.cancel()
        worker.cancel()


async def t_p_slow_memory_work_does_not_block_the_provider_reader():
    """Die Verzoegerung liegt im DIENST, nicht im Test-Dispatcher: nur so faellt auf,
    wenn das Werkzeug seine Arbeit auf dem Event-Loop erledigt statt daneben.

    Gemessen an dieser Stelle: 0 ms, wenn der Dienst im Thread wartet; 2011 ms, sobald
    dieselbe Wartezeit auf dem Loop stattfindet. Geprueft wird die VERSTRICHENE ZEIT —
    ein blockierter Loop laesst auch `wait_for` nicht ausloesen und meldete faelschlich
    Erfolg.
    """
    import time as _time
    entered = asyncio.Event()

    class _SlowSearchService:
        """Sucht langsam, aber korrekt: die Wartezeit liegt in einem Thread."""

        base_dir = "<slow>"

        async def search(self, query, *, top_k=3, max_chars=600):
            entered.set()
            await asyncio.to_thread(_time.sleep, 2.0)
            return []

    class _Dispatcher:
        approvals = None

        def __init__(self, service):
            self.memory_gate = MemoryIntentGate()
            self.tool = MemorySearchTool(service)

        def parse_args(self, raw):
            return json.loads(raw) if raw else {}

        async def dispatch(self, name, args):
            return (await self.tool.run(args)).as_dict()

    class _Provider:
        def __init__(self, frames):
            self._f = frames

        def __aiter__(self):
            async def gen():
                for f in self._f:
                    yield f
                await asyncio.sleep(3600)
            return gen()

        async def send(self, d):
            pass

        async def close(self, *a, **kw):
            pass

    class _Satellite:
        def __init__(self):
            self.got = asyncio.Event()

        async def send(self, d):
            if isinstance(d, (bytes, bytearray)):
                self.got.set()

        async def close(self, *a, **kw):
            pass

        remote_address = ("x", 1)

    srv = CS.CoreServer.__new__(CS.CoreServer)
    srv.dispatcher = _Dispatcher(_SlowSearchService())
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "m"
    sess = CS.Session(srv, _Satellite())
    sess.active = True
    sess.oa = _Provider([
        json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "memory_search", "call_id": "c1",
             "arguments": '{"query": "x"}'}]}}),
        json.dumps({"type": "response.output_audio.delta", "delta": PCM}),
    ])
    started = asyncio.get_event_loop().time()
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await sess.ws.got.wait()
        elapsed = asyncio.get_event_loop().time() - started
        require(entered.is_set(), "the memory search never started — this proved nothing")
        require(elapsed < 1.0,
                f"the audio delta reached the satellite only after {elapsed:.1f}s of memory "
                f"work — memory is running on the event loop, not beside it")
    finally:
        reader.cancel()
        worker.cancel()


# =====================================================================
# Q, R, S — Aufbewahrung
# =====================================================================
def t_q_expired_conversations_are_purged():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        path = os.path.join(tmp, "conversations.sqlite3")
        store = ConversationStore(path, now_fn=clock, linger_seconds=900.0,
                                  retention_days=90.0).open()
        old_id, _ = store.begin_session("s-old")
        store.add_message(old_id, "user", "das ist sehr alt")
        clock.advance(91 * 86400)
        fresh_id, _ = store.begin_session("s-new")
        store.add_message(fresh_id, "user", "das ist frisch")
        report = store.purge_expired()
        try:
            require_equal(report["conversations"], 1, report)
            require_equal(report["messages"], 1, report)
            require_equal(store.conversation(old_id), None, "the expired conversation survived")
            require(store.conversation(fresh_id) is not None, "a fresh conversation was purged")
            require_equal([m["text"] for m in store.messages(fresh_id)], ["das ist frisch"],
                          "the fresh conversation lost its messages")
        finally:
            store.close()


def t_q_purge_runs_when_the_store_opens():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        path = os.path.join(tmp, "conversations.sqlite3")
        store = ConversationStore(path, now_fn=clock, retention_days=90.0).open()
        old_id, _ = store.begin_session("s-old")
        store.add_message(old_id, "user", "alt")
        store.close()
        clock.advance(91 * 86400)
        store = ConversationStore(path, now_fn=clock, retention_days=90.0).open()
        try:
            require_equal(store.conversation(old_id), None,
                          "opening the store did not purge expired history")
        finally:
            store.close()


def t_r_retention_does_not_change_the_linger_semantics():
    """Zwei verschiedene Uhren: Linger entscheidet ueber AKTIVEN Kontext, Retention
    ueber Speicherung. Eine 20 Minuten alte Unterhaltung ist nicht mehr aktiv, aber
    selbstverstaendlich noch gespeichert."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        store = ConversationStore(os.path.join(tmp, "c.sqlite3"), now_fn=clock,
                                  linger_seconds=900.0, retention_days=90.0).open()
        first, _ = store.begin_session("s-1")
        store.add_message(first, "user", "vor zwanzig Minuten gesagt")
        clock.advance(20 * 60)
        second, resumed = store.begin_session("s-2")
        try:
            require(not resumed, "the linger window did not expire")
            require(second != first, "a new conversation was expected")
            require(store.conversation(first) is not None,
                    "retention deleted a conversation that only left the linger window")
            require_equal([m["text"] for m in store.messages(first)],
                          ["vor zwanzig Minuten gesagt"], "the history was destroyed early")
            report = store.purge_expired()
            require_equal(report["conversations"], 0, "a 20-minute-old conversation was purged")
        finally:
            store.close()


async def t_s_durable_memory_survives_a_conversation_purge():
    """Keine Kaskade: das Loeschen von Gespraechsverlauf beruehrt Langzeitwissen nicht."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        conversations = ConversationStore(os.path.join(tmp, "conversations.sqlite3"),
                                          now_fn=clock, retention_days=90.0).open()
        svc = _service(tmp, clock)
        try:
            conversation_id, _ = conversations.begin_session("s-1")
            conversations.add_message(conversation_id, "user", f"Merk dir dauerhaft: {FACT}.")
            await _remember(svc, f"Merk dir dauerhaft: {FACT}.",
                            conversation_id=conversation_id)
            clock.advance(91 * 86400)
            report = conversations.purge_expired()
            require_equal(report["conversations"], 1, report)
            require_equal(conversations.messages(conversation_id), [],
                          "the verbatim history was not purged")
        finally:
            await svc.close()
            conversations.close()
        # NEU OEFFNEN. Ein noch offener SQLite-Handle liest auch dann weiter, wenn die
        # Datei laengst von der Platte entfernt wurde — ohne diesen Schnitt wuerde der
        # Test eine geloeschte Datenbank nicht bemerken.
        svc = _service(tmp, clock)
        try:
            hits = await svc.search("Was ist mein Langzeit-Testwort?")
            require(hits, "the durable memory was destroyed with the conversation")
            require("Bernstein 84" in hits[0].content, hits[0].content)
        finally:
            await svc.close()


async def t_s_deleting_one_conversation_does_not_touch_memory():
    with tempfile.TemporaryDirectory() as tmp:
        clock = _Clock()
        conversations = ConversationStore(os.path.join(tmp, "conversations.sqlite3"),
                                          now_fn=clock).open()
        svc = _service(tmp, clock)
        try:
            conversation_id, _ = conversations.begin_session("s-1")
            await _remember(svc, f"Merk dir dauerhaft: {FACT}.",
                            conversation_id=conversation_id)
            conversations.delete_conversation(conversation_id)
            hits = await svc.search("Was ist mein Langzeit-Testwort?")
            require(hits, "deleting a conversation cascaded into durable memory")
        finally:
            await svc.close()
            conversations.close()


# =====================================================================
# Das Fundament wurde benutzt, nicht nachgebaut
# =====================================================================
def t_the_existing_foundation_is_reused_not_duplicated():
    import inspect
    from solvio.memory.semantic import SemanticMemory
    from solvio.memory.store import SolvioMemory
    source = inspect.getsource(MemoryService)
    require("SemanticMemory" in source, "the service does not use the existing semantic layer")
    require("CREATE TABLE" not in source, "the service created a parallel schema")
    require(issubclass(SemanticMemory, object) and SolvioMemory is not None,
            "the foundation is missing")


def t_every_memory_file_is_owner_only():
    """Beobachtet: memory.sqlite3 und privacy_ledger.sqlite3 kamen mit 0600, der
    semantische Index mit 0644. Er traegt aus Gedaechtnisinhalten abgeleitete Vektoren
    und gehoert genauso geschuetzt wie seine Geschwister."""
    import stat as _stat
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            base = svc.base_dir
            mode = _stat.S_IMODE(os.stat(base).st_mode)
            require_equal(mode & 0o077, 0, f"the memory directory is open: {oct(mode)}")
            checked = 0
            for name in os.listdir(base):
                path = os.path.join(base, name)
                if not os.path.isfile(path) or ".sqlite3" not in name:
                    continue
                checked += 1
                mode = _stat.S_IMODE(os.stat(path).st_mode)
                require_equal(mode & 0o077, 0,
                              f"{name} is readable by others: {oct(mode)}")
            require(checked >= 3, f"only {checked} memory files were checked")
        finally:
            svc.semantic.memory._close_sync()


def t_embeddings_are_local_only():
    from solvio.memory.service import select_local_provider
    provider = select_local_provider()
    require(provider.profile.provider_id in ("deterministic", "qwen-local"),
            f"a non-local embedding provider was selected: {provider.profile.provider_id}")
    import inspect
    from solvio.memory import service as service_module
    require("OpenAIEmbeddingProvider" not in inspect.getsource(service_module),
            "the service can reach the hosted embedding API")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
