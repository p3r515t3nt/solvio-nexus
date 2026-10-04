"""M2-Reparatur — Politik, Autorität und Abruf gegen realistisches Deutsch.

WARUM ES DIESE SUITE GIBT

Die erste M2-Fassung bestand ihre eigenen Tests und war trotzdem unsicher, weil die
Fixtures zu schmal waren: ein bis drei künstlich verschiedene Fakten und Fragen, die
entweder offensichtlich passten oder offensichtlich nicht. Eine unabhängige Prüfung
zeigte an realistischem Deutsch:

  * "Ich bemerke direkt einen Unterschied" ERZEUGTE dauerhaftes Wissen
  * "Merk dir dauerhaft: Die Heizung geht nicht mehr an" LÖSCHTE einen fremden Fakt
  * "Mein Kennwort fürs Onlinebanking ist ..." wurde GESPEICHERT
  * 25 % unbeteiligter Fragen lieferten einen Treffer
  * ein Mandat aus Turn N war in Turn N+1 einlösbar

Diese Suite prüft gegen `tests/fixtures/german_memory_corpus.py` — Dutzende Sätze, keine
drei. Jeder Test kann für den Defekt fallen, gegen den er schützt.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und ueberleben `-O`.

Direkt: python tests/test_m2_memory_policy.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from _guard import enforce_assertions  # noqa: E402
enforce_assertions()

from _guard import require, require_equal  # noqa: E402
import german_memory_corpus as CORPUS  # noqa: E402
from solvio.conversation import ConversationStore  # noqa: E402
from solvio.memory.intent import detect, looks_like_secret  # noqa: E402
from solvio.memory.service import MemoryService, bounds_for  # noqa: E402
from solvio.tools.memory_tools import (MemoryIntentGate, MemoryRememberTool,  # noqa: E402
                                       MemorySearchTool, PermitRefused)


def _service(tmp):
    return MemoryService(os.path.join(tmp, "memory")).open()


def _fast_service(tmp):
    """Fuer Tests, die den MANDATS-Lebenszyklus pruefen, nicht die Abrufqualitaet.

    Sie bekommen ausdruecklich den deterministischen Provider: das lokale Qwen-Modell
    braucht kalt 5,5 bis 18,8 Sekunden zum Laden, und unter Gate-Last liess das diese
    Tests sporadisch in eine Zeitgrenze laufen — ein flackerndes Gate ist selbst ein
    Defekt. Die Abrufqualitaet wird an anderer Stelle gegen den echten Provider geprueft.
    """
    from solvio.memory.embedding import HashingEmbeddingProvider
    return MemoryService(os.path.join(tmp, "memory"),
                         provider=HashingEmbeddingProvider()).open()


async def _fill(svc, memories=None):
    for text in (memories or CORPUS.MEMORIES):
        intent = detect("Merk dir: " + text)
        require(intent is not None, f"fixture sentence carries no intent: {text!r}")
        await svc.remember(intent, conversation_id="c-fixture")


# =====================================================================
# A, B — gewöhnliche Sprache erzeugt kein Gedächtnis
# =====================================================================
def t_ab_ordinary_german_never_authorizes_memory():
    """Der Kern des Defekts: die Erkennung suchte Teilstrings innerhalb von Wörtern."""
    wrong = [s for s in CORPUS.INTENT_NEGATIVE if detect(s) is not None]
    require_equal(wrong, [], f"ordinary sentences were read as memory intent: {wrong}")


def t_ab_the_named_regressions_specifically():
    for sentence in ("Ich bemerke direkt einen Unterschied zwischen den beiden Aufnahmen.",
                     "Ich notiere direkt alles mit.",
                     "Das Programm speichert direkt in die Datenbank.",
                     "Der Speicher dir gegenueber ist voll."):
        require_equal(detect(sentence), None, f"regression returned: {sentence!r}")


def t_ab_explicit_instructions_are_still_recognized():
    missed = [s for s in CORPUS.INTENT_POSITIVE if detect(s) is None]
    require_equal(missed, [], f"explicit instructions were not recognized: {missed}")


def t_ab_a_negated_instruction_stores_nothing():
    for sentence in ("Merk dir das nicht.", "Merk dir das bloss nicht.",
                     "Merke dir das nichts davon."):
        require_equal(detect(sentence), None, f"a negated instruction was accepted: {sentence!r}")


def t_ab_an_instruction_without_content_stores_nothing():
    for sentence in ("Merk dir das.", "Merk dir es.", "Merke dir sowas."):
        require_equal(detect(sentence), None, f"an empty instruction was accepted: {sentence!r}")


# =====================================================================
# C, D, E — Korrektur
# =====================================================================
def t_c_nicht_mehr_alone_is_not_a_correction():
    """Reproduziert: "nicht mehr" war ein Korrekturmarker und machte alltägliche Sätze zu
    zerstörenden Operationen."""
    for sentence in ("Merk dir dauerhaft: Die Heizung im Bad geht nicht mehr an.",
                     "Merk dir: Der Drucker druckt nicht mehr.",
                     "Merk dir: Anna arbeitet nicht mehr bei Siemens.",
                     "Merk dir: Ich rauche nicht mehr."):
        intent = detect(sentence)
        require(intent is not None, f"the instruction was lost: {sentence!r}")
        require_equal(intent.kind, "remember",
                      f"an everyday sentence was classified as a correction: {sentence!r}")


async def t_c_an_everyday_sentence_destroys_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc)
            before = {r.content for r in await svc.semantic.memory.active_records()}
            for sentence in ("Merk dir dauerhaft: Die Heizung im Bad geht nicht mehr an.",
                             "Merk dir: Der Drucker druckt nicht mehr.",
                             "Merk dir: Anna arbeitet nicht mehr bei Siemens."):
                await svc.remember(detect(sentence), conversation_id="c")
            after = {r.content for r in await svc.semantic.memory.active_records()}
            lost = before - after
            require_equal(lost, set(), f"everyday sentences destroyed stored facts: {lost}")
        finally:
            await svc.close()


def t_d_explicit_correction_is_recognized():
    for sentence, _target in CORPUS.CORRECTIONS:
        intent = detect(sentence)
        require(intent is not None, f"the correction was lost: {sentence!r}")
        require_equal(intent.kind, "correct",
                      f"an explicit correction was not recognized: {sentence!r}")


async def t_d_an_explicit_correction_supersedes_only_its_target():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc)
            for sentence, target_text in CORPUS.CORRECTIONS:
                before = {r.id: r.content for r in await svc.semantic.memory.active_records()}
                result = await svc.remember(detect(sentence), conversation_id="c")
                after = {r.id: r.content for r in await svc.semantic.memory.active_records()}
                gone = [c for i, c in before.items() if i not in after]
                require_equal(result.action, "superseded",
                              f"{sentence!r} did not supersede: {result.action}/{result.reason}")
                require_equal(gone, [target_text],
                              f"{sentence!r} superseded the wrong fact: {gone}")
        finally:
            await svc.close()


async def t_e_an_ambiguous_correction_destroys_nothing():
    """Zwei gleich plausible Ziele: nicht raten, sondern nachfragen."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, ["der Termin mit Anna ist am Dienstag um zehn",
                              "der Termin mit Bernd ist am Dienstag um zehn"])
            before = {r.content for r in await svc.semantic.memory.active_records()}
            result = await svc.remember(
                detect("Korrektur, merk dir: Der Termin ist am Dienstag um elf."),
                conversation_id="c")
            after = {r.content for r in await svc.semantic.memory.active_records()}
            require(result.action in ("ambiguous", "created"),
                    f"an ambiguous correction did something destructive: {result.action}")
            require_equal(before - after, set(),
                          "an ambiguous correction destroyed a fact")
        finally:
            await svc.close()


# =====================================================================
# F, G, H, I — Zugangsdaten
# =====================================================================
def t_fg_credentials_are_refused():
    leaked = [s for s in CORPUS.SECRETS_BLOCK if not looks_like_secret(detect(s).payload)]
    require_equal(leaked, [], f"credentials were not recognized: {leaked}")


def t_hi_benign_metadata_is_allowed():
    refused = [s for s in CORPUS.SECRETS_ALLOW if looks_like_secret(detect(s).payload)]
    require_equal(refused, [], f"benign sentences were wrongly refused: {refused}")


async def t_fg_the_named_credential_regressions_reach_no_database():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        marks = ["Sommerregen2024", "4711", "hunter2", "884213"]
        try:
            for sentence in CORPUS.SECRETS_BLOCK:
                result = await svc.remember(detect(sentence), conversation_id="c")
                require(not result.ok, f"a credential was stored: {sentence!r}")
                require_equal(result.reason, "looks_like_secret", result.reason)
            await svc.remember(detect("Merk dir: Anna arbeitet bei Siemens."),
                               conversation_id="c")
        finally:
            await svc.close()
        blob = b""
        for root, _dirs, files in os.walk(os.path.join(tmp, "memory")):
            for name in files:
                with open(os.path.join(root, name), "rb") as fh:
                    blob += fh.read()
        require(b"Sommerregen2024" not in blob, "a banking password reached the database")
        require(b"hunter2" not in blob, "a password reached the database")
        require(b"884213" not in blob, "a TAN reached the database")
        require(b"Anna" in blob, "the fixture stored nothing at all")


async def t_hi_benign_metadata_is_actually_stored_and_found():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            for sentence in CORPUS.SECRETS_ALLOW:
                result = await svc.remember(detect(sentence), conversation_id="c")
                require(result.ok, f"a benign sentence was refused: {sentence!r} "
                                   f"({result.reason})")
            hits = await svc.search("Wie heisst mein Passwort-Manager?")
            require(any("Bitwarden" in h.content for h in hits),
                    f"the benign fact is not retrievable: {[h.content for h in hits]}")
        finally:
            await svc.close()


# =====================================================================
# J, K, L — Turn-gebundene Autorität
# =====================================================================
async def t_j_a_permit_from_the_previous_turn_cannot_be_cashed():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.PERMIT_WAIT_SECONDS = 0.2             # keine 4 Sekunden im Test warten
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t2")          # das Werkzeug arbeitet an Turn 2
            result = await tool.run({})
            require(not result.success, "a permit from the previous turn was cashed")
            # Turn 2 hat keine eigene Entscheidung: der Aufruf wartet begrenzt und lehnt
            # dann ab. Welcher der beiden Ablehnungsgruende greift, ist zweitrangig —
            # entscheidend ist, dass NICHTS geliehen und NICHTS geschrieben wird.
            require(result.error in ("no_explicit_intent", "transcript_timeout"),
                    f"unexpected refusal reason: {result.error}")
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_l_a_tool_call_before_the_transcript_cannot_use_a_stale_permit():
    """Der Betriebsfall: Werkzeugaufrufe treffen frueher ein als das finalisierte
    Transkript. In der Live-Abnahme lag TOOL_CALL vor der Transkriptverarbeitung
    desselben Turns."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect("Merk dir: Der Drucker steht im Arbeitszimmer."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.PERMIT_WAIT_SECONDS = 0.2
            gate.expect_turn("s-1", "s-1-t2")          # Turn 2 laeuft, Transkript fehlt noch
            require(not (await tool.run({})).success,
                    "a tool call ahead of the transcript borrowed turn 1's authority")
            gate.offer_user_turn(None, session_id="s-1", turn_id="s-1-t2")
            require(not (await tool.run({})).success,
                    "a turn without intent still authorized a write")
        finally:
            await svc.close()


def t_k_a_failed_transcription_closes_the_permit():
    gate = MemoryIntentGate()
    gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                         session_id="s-1", turn_id="s-1-t1")
    gate.offer_user_turn(None, session_id="s-1", turn_id="s-1-t2")   # transcription.failed
    gate.expect_turn("s-1", "s-1-t2")
    require_equal(gate.take(), None, "a failed transcription left the permit armed")


def t_k_the_reader_closes_the_permit_on_a_failed_transcription():
    from solvio.realtime import core_server as CS
    import inspect
    reader = inspect.getsource(CS.Session._oa_reader)
    require("input_audio_transcription.failed" in reader,
            "a failed transcription is not handled at all — the permit stays armed")
    require("_offer_memory_intent(\"\")" in reader,
            "the failed-transcription branch does not clear the permit")


def t_j_the_permit_carries_its_turn():
    gate = MemoryIntentGate()
    gate.offer_user_turn(detect("Merk dir: X ist Y und Z."), session_id="s-9",
                         turn_id="s-9-t3")
    require_equal(gate.take(), None, "a permit was cashed with no turn announced")
    gate.expect_turn("s-9", "s-9-t3")
    require(gate.take() is not None, "the legitimate write was refused")


# =====================================================================
# Same-Turn-Verzögerung — der Live-Defekt
# =====================================================================
async def t_same_turn_1_a_tool_call_before_the_transcript_still_persists():
    """DER Produktdefekt aus der Live-Abnahme.

    Gesagt: "Merk dir dauerhaft: Mein zweites Langzeit-Testwort ist Saphir 62."
    SOLVIO bestaetigte muendlich, dass "merk dir" gefallen war, weigerte sich aber zu
    speichern und verlangte vom Nutzer ausgerechnet dieses "merk dir".

    Der Provider ruft das Werkzeug regelmaessig VOR dem finalisierten Transkript
    desselben Turns auf. Der Aufruf wartet jetzt begrenzt auf die Entscheidung SEINES
    Turns, statt die normale Reihenfolge als Ablehnung durchzureichen.
    """
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")          # Werkzeugrunde beginnt
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)                      # Aufruf wartet, Transkript fehlt
            require(not pending.done(), "the tool refused before the transcript arrived")
            gate.offer_user_turn(                       # jetzt trifft das Transkript ein
                detect("Merk dir dauerhaft: Mein zweites Langzeit-Testwort ist Saphir 62."),
                session_id="s-1", turn_id="s-1-t1")
            try:
                result = await asyncio.wait_for(pending, timeout=30)
            except asyncio.TimeoutError:
                require(False, "the pending write never resolved after the transcript "
                               "arrived — the same-turn deferral is stuck")
            require(result.success, f"the write was refused: {result.error}")
            records = await svc.semantic.memory.active_records()
            require_equal(len(records), 1, records)
            require("Saphir 62" in records[0].content, records[0].content)
        finally:
            await svc.close()


async def t_same_turn_2_an_ordinary_statement_is_still_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)
            gate.offer_user_turn(detect("Ich bemerke direkt einen Unterschied."),
                                 session_id="s-1", turn_id="s-1-t1")
            result = await asyncio.wait_for(pending, timeout=5)
            require(not result.success, "an ordinary statement created a memory")
            require_equal(result.error, "no_explicit_intent", result.error)
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_same_turn_3_a_failed_transcription_rejects_the_pending_call():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)
            gate.offer_user_turn(None, session_id="s-1", turn_id="s-1-t1")   # failed
            result = await asyncio.wait_for(pending, timeout=5)
            require(not result.success, "a failed transcription authorized a write")
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_same_turn_4_a_new_turn_rejects_the_pending_call():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)
            gate.expect_turn("s-1", "s-1-t2")          # der naechste Turn beginnt
            result = await asyncio.wait_for(pending, timeout=5)
            require(not result.success, "a pending call survived into the next turn")
            require_equal(result.error, "turn_changed", result.error)
            # und ein spaeter eintreffendes Mandat aus t1 darf t2 nicht autorisieren
            gate.offer_user_turn(detect(f"Merk dir: {'Anna arbeitet bei Siemens'}."),
                                 session_id="s-1", turn_id="s-1-t1")
            require_equal(gate.take(), None, "turn 1's permit authorized turn 2")
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_same_turn_5_a_closing_session_rejects_the_pending_call():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)
            gate.clear()                                # Sitzung schliesst
            result = await asyncio.wait_for(pending, timeout=5)
            require(not result.success, "a closing session authorized a write")
            require_equal(result.error, "session_closed", result.error)
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


async def t_same_turn_6_the_wait_is_bounded():
    """Kein unbegrenztes Warten: bleibt das Transkript aus, wird abgelehnt."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 0.2
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.expect_turn("s-1", "s-1-t1")
            result = await asyncio.wait_for(tool.run({}), timeout=5)
            require(not result.success, "an absent transcript authorized a write")
            require_equal(result.error, "transcript_timeout", result.error)
            require(result.human_message, "the user would hear nothing about the failure")
        finally:
            await svc.close()


async def t_same_turn_7_a_call_after_the_transcript_still_works():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")
            result = await asyncio.wait_for(tool.run({}), timeout=5)
            require(result.success, f"the normal ordering was refused: {result.error}")
            require_equal(await svc.semantic.memory.count(), 1, "nothing was stored")
        finally:
            await svc.close()


async def t_same_turn_8_waiting_does_not_block_the_provider_reader():
    """Der wartende Aufruf laeuft im Tool-Worker, nicht im Reader."""
    from solvio.realtime import core_server as CS
    import base64
    pcm = base64.b64encode(b"\x01\x02" * 240).decode("ascii")
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)

        class _Dispatcher:
            approvals = None

            def __init__(self):
                self.memory_gate = MemoryIntentGate()
                self.memory_gate.PERMIT_WAIT_SECONDS = 3.0
                self.tool = MemoryRememberTool(svc, self.memory_gate)

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
        srv.dispatcher = _Dispatcher()
        srv.idle_timeout = 999
        srv.credentials = None
        srv.model = "m"
        srv.conversations = None
        sess = CS.Session(srv, _Satellite())
        sess.active = True
        sess.oa = _Provider([
            json.dumps({"type": "response.done", "response": {"output": [
                {"type": "function_call", "name": "memory_remember", "call_id": "c1",
                 "arguments": "{}"}]}}),
            json.dumps({"type": "response.output_audio.delta", "delta": pcm}),
        ])
        started = asyncio.get_event_loop().time()
        reader = asyncio.create_task(sess._oa_reader())
        worker = asyncio.create_task(sess._tool_loop())
        try:
            await sess.ws.got.wait()
            elapsed = asyncio.get_event_loop().time() - started
            require(elapsed < 1.0,
                    f"audio reached the satellite only after {elapsed:.1f}s while a memory "
                    f"permit was pending — the wait is on the reader's path")
        finally:
            reader.cancel()
            worker.cancel()
            await svc.close()


def t_same_turn_the_asr_short_form_is_recognized():
    """Live beobachtet: die ASR verschluckte das "dir". Gesagt wurde "Merk dir
    dauerhaft ...", transkribiert kam "Merke dauerhaft, ..." an."""
    intent = detect("Merke dauerhaft, mein zweites Langzeittestwort ist Saphir 62.")
    require(intent is not None, "the transcribed short form is still not recognized")
    require_equal(intent.kind, "remember", intent.kind)
    require("Saphir 62" in intent.payload, intent.payload)
    for ordinary in ("Ich merke dauerhaft einen Unterschied.",
                     "Er merkt dauerhaft nichts.",
                     "Das merke dauerhaft niemand."):
        require_equal(detect(ordinary), None,
                      f"the short form leaked into ordinary language: {ordinary!r}")


# =====================================================================
# M, N — Abrufqualität am Korpus
# =====================================================================
async def t_m_unrelated_questions_return_nothing():
    """Gemessen vor der Reparatur: 25 % der unbeteiligten Fragen lieferten einen Treffer,
    etwa "Wie viele Bundeslaender hat Oesterreich?" -> "mein Fahrrad hat die
    Rahmennummer WBK4471", allein ueber das geteilte Wort "hat"."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc)
            wrong = []
            for question in CORPUS.UNRELATED:
                hits = await svc.search(question)
                if hits:
                    wrong.append((question, hits[0].content))
            limit = max(2, len(CORPUS.UNRELATED) // 10)      # <= 10 %
            require(len(wrong) <= limit,
                    f"{len(wrong)} of {len(CORPUS.UNRELATED)} unrelated questions returned "
                    f"a memory (limit {limit}): {wrong}")
        finally:
            await svc.close()


async def t_n_relevant_questions_find_their_fact():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc)
            missed = []
            for question, needle in CORPUS.RELEVANT:
                hits = await svc.search(question)
                if not any(needle.lower() in h.content.lower() for h in hits):
                    missed.append((question, needle))
            allowed = len(CORPUS.RELEVANT) // 10             # <= 10 %
            require(len(missed) <= allowed,
                    f"{len(missed)} of {len(CORPUS.RELEVANT)} relevant questions missed "
                    f"(limit {allowed}): {missed}")
        finally:
            await svc.close()


async def t_n_german_compound_and_asr_variants_all_hit():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, ["mein zweites Langzeittestwort ist Saphir 62"])
            for question in ("Was ist mein zweites Langzeit-Testwort?",
                             "Was ist mein zweites Langzeittestwort?",
                             "Was war mein zweites Langzeit Testwort?",
                             "Wie lautet mein zweites Langzeittestwort?"):
                hits = await svc.search(question)
                require(any("Saphir" in h.content for h in hits),
                        f"a transcription variant found nothing: {question!r}")
        finally:
            await svc.close()


async def t_m_results_stay_within_top_k_and_budget():
    """Die frühere Fassung dieses Tests konnte nicht fallen: sie legte kurze Einträge an
    und prüfte eine Grenze, die ohnehin nie erreicht wurde."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            long_ones = [f"Notiz {i} ueber das Gartenhaus: " + "Kastanie " * 60
                         for i in range(12)]
            await _fill(svc, long_ones)
            hits = await svc.search("Was steht in den Notizen ueber das Gartenhaus?",
                                    top_k=3, max_chars=600)
            require(len(hits) <= 3, f"{len(hits)} hits for top_k=3")
            total = sum(len(h.content) for h in hits)
            require(total <= 600, f"{total} characters returned for a 600 budget")
            require(all(len(h.content) > 400 for h in hits) or not hits,
                    "the fixture did not actually produce oversized entries")
        finally:
            await svc.close()


# =====================================================================
# Lesen ist nicht Schreiben — der Live-Fehlschlag vom 21. August
# =====================================================================
RECALL_QUESTIONS = [
    "Was ist mein zweites Langzeit-Testwort?",
    "Was war mein Langzeit-Testwort?",
    "Weisst du noch mein Langzeit-Testwort?",
    "Was hattest du dir ueber Projekt Aurora gemerkt?",
    "Was weisst du noch ueber meinen Zahnarzttermin?",
    "Kennst du meine Rahmennummer noch?",
    "Erinnerst du dich an das WLAN im Gartenhaus?",
]


def t_recall_2_a_retrieval_question_creates_no_write_permit():
    """DIE Invariante aus dem Live-Fehlschlag: eine Frage nach etwas Gemerktem darf
    niemals Schreib-Autoritaet erzeugen, und der Nutzer darf nie "Merk dir" sagen
    muessen, um etwas zu LESEN."""
    wrong = [q for q in RECALL_QUESTIONS if detect(q) is not None]
    require_equal(wrong, [], f"retrieval questions created remember intent: {wrong}")


def t_recall_6_the_truncated_live_fragment_creates_no_memory():
    """Was die ASR tatsaechlich lieferte, nachdem der Satzanfang beim Verbindungsaufbau
    verloren ging: ein Fragment ohne Frage."""
    require_equal(detect("ein zweites Langzeittestwort."), None,
                  "the truncated fragment created remember intent")
    require_equal(detect("zweites Langzeittestwort"), None, "a bare noun phrase authorized")


async def t_recall_1_3_the_search_tool_returns_the_stored_memory():
    """Der Abrufweg liefert den Eintrag als Werkzeugergebnis — genau das, was das Modell
    zum Antworten braucht."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, ["mein zweites Langzeit-Testwort ist Saphir62"])
            tool = MemorySearchTool(svc)
            for question in ("Was ist mein zweites Langzeit-Testwort?",
                             "Was war mein zweites Langzeittestwort?",
                             "Weisst du noch mein zweites Langzeit Testwort?"):
                result = await tool.run({"query": question})
                require(result.success, f"{question!r}: {result.error}")
                memories = (result.data or {}).get("memories", [])
                require(any("Saphir62" in m["content"] for m in memories),
                        f"{question!r} did not surface the memory: {memories}")
        finally:
            await svc.close()


async def t_recall_1_the_fragment_would_also_have_found_it():
    """Selbst das abgeschnittene Fragment findet den Eintrag — gemessen cos 0.660.
    Die Suche war nicht das Problem; sie wurde nie aufgerufen."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, ["mein zweites Langzeit-Testwort ist Saphir62"])
            hits = await svc.search("ein zweites Langzeittestwort.")
            require(any("Saphir62" in h.content for h in hits),
                    f"the fragment found nothing: {[h.content for h in hits]}")
        finally:
            await svc.close()


async def t_recall_5_an_empty_memory_does_not_demand_write_authority():
    """Ohne passenden Eintrag sagt der Abruf schlicht, dass nichts da ist — er verlangt
    NICHT, dass der Nutzer erst "Merk dir" sagt."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            tool = MemorySearchTool(svc)
            result = await tool.run({"query": "Was ist mein zweites Langzeit-Testwort?"})
            require(result.success, f"an empty store made the search fail: {result.error}")
            require_equal((result.data or {}).get("memories"), [], result.data)
            spoken = (result.human_message or "").lower()
            require("merk dir" not in spoken,
                    f"reading demanded write authority: {result.human_message!r}")
        finally:
            await svc.close()


async def t_recall_the_two_tools_stay_separate():
    """Lesen und Schreiben sind getrennte Wege: das Suchwerkzeug braucht kein Mandat,
    das Schreibwerkzeug immer eines."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 0.2
        try:
            await _fill(svc, ["mein zweites Langzeit-Testwort ist Saphir62"])
            # Suche: ohne jedes Mandat erfolgreich.
            search = await MemorySearchTool(svc).run(
                {"query": "Was ist mein zweites Langzeit-Testwort?"})
            require(search.success, "the read path required authority")
            # Schreiben: ohne Mandat abgelehnt.
            gate.expect_turn("s-1", "s-1-t1")
            write = await MemoryRememberTool(svc, gate).run({})
            require(not write.success, "the write path ran without a permit")
            require_equal(len(await svc.semantic.memory.active_records()), 1,
                          "the read path created or changed a memory")
        finally:
            await svc.close()


async def t_recall_7_a_plain_restart_needs_no_reindex():
    """Nach einem gewoehnlichen Neustart — gleicher Provider — darf kein Neuaufbau
    anstehen; sonst waere jede erste Suche unnoetig langsam."""
    with tempfile.TemporaryDirectory() as tmp:
        first = _service(tmp)
        try:
            await _fill(first, ["mein zweites Langzeit-Testwort ist Saphir62"])
        finally:
            await first.close()
        second = _service(tmp)                      # wie ein Core-Neustart
        try:
            health = await second.health()
            require_equal(health["reindex_pending"], False,
                          "a plain restart scheduled an unnecessary reindex")
            require_equal(second.semantic.index.active_profile(),
                          second.provider.profile.key, "the active profile drifted")
            hits = await second.search("Was ist mein zweites Langzeit-Testwort?")
            require(any("Saphir62" in h.content for h in hits),
                    "the memory is unreachable after a restart")
        finally:
            await second.close()


# =====================================================================
# Provenienz: die Kennung der Quellnachricht
# =====================================================================
async def t_provenance_the_source_message_is_linked():
    """Beobachtet an beiden live erzeugten Eintraegen: `source_message_id` war leer.

    Die Kennung entsteht erst beim Schreiben in den Gespraechsspeicher, das in einer
    Warteschlange laeuft — zum Zeitpunkt der Absichtserkennung war sie also noch nicht
    bekannt. Jetzt vergibt die Sprachschicht sie vorab und reicht sie an beide Seiten
    weiter, sodass sich ein Gedaechtniseintrag auf die Nachricht zurueckfuehren laesst,
    aus der er stammt.
    """
    import base64
    from solvio.realtime import core_server as CS
    from solvio.conversation import ConversationStore
    pcm = base64.b64encode(b"\x01\x02" * 240).decode("ascii")

    with tempfile.TemporaryDirectory() as tmp:
        svc = _fast_service(tmp)
        conversations = ConversationStore(os.path.join(tmp, "conversations.sqlite3")).open()

        class _Dispatcher:
            approvals = None

            def __init__(self):
                self.memory_gate = MemoryIntentGate()
                self.tool = MemoryRememberTool(svc, self.memory_gate)

            def parse_args(self, raw):
                return json.loads(raw) if raw else {}

            async def dispatch(self, name, args):
                return (await self.tool.run(args)).as_dict()

        class _Provider:
            def __init__(self, frames):
                self._f = frames
                self.exhausted = asyncio.Event()

            def __aiter__(self):
                async def gen():
                    for f in self._f:
                        yield f
                        await asyncio.sleep(0)
                    self.exhausted.set()
                    await asyncio.sleep(3600)
                return gen()

            async def send(self, d):
                pass

            async def close(self, *a, **kw):
                pass

        class _Satellite:
            async def send(self, d):
                pass

            async def close(self, *a, **kw):
                pass

            remote_address = ("x", 1)

        srv = CS.CoreServer.__new__(CS.CoreServer)
        srv.dispatcher = _Dispatcher()
        srv.idle_timeout = 999
        srv.credentials = None
        srv.model = "m"
        srv.conversations = conversations
        sess = CS.Session(srv, _Satellite())
        sess.active = True
        sess.conversation_id, _ = conversations.begin_session(sess.session_id)
        sess.conversation_mode = "active"
        sess.oa = _Provider([
            json.dumps({"type": "input_audio_buffer.speech_started"}),
            json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Merk dir dauerhaft: Anna arbeitet bei Siemens."}),
            json.dumps({"type": "response.done", "response": {"output": [
                {"type": "function_call", "name": "memory_remember", "call_id": "c1",
                 "arguments": "{}"}]}}),
            json.dumps({"type": "response.output_audio.delta", "delta": pcm}),
        ])
        reader = asyncio.create_task(sess._oa_reader())
        worker = asyncio.create_task(sess._tool_loop())
        persister = asyncio.create_task(sess._persist_loop())
        try:
            await asyncio.wait_for(sess.oa.exhausted.wait(), timeout=15)
            await asyncio.wait_for(sess._tool_queue.join(), timeout=15)
            await asyncio.wait_for(sess._persist_queue.join(), timeout=15)
            records = await svc.semantic.memory.active_records()
            require_equal(len(records), 1, f"expected exactly one memory: {records}")
            linked = records[0].metadata.get("source_message_id")
            require(linked, "source_message_id is still empty — provenance is incomplete")
            messages = conversations.messages(sess.conversation_id)
            ids = [m["message_id"] for m in messages]
            require(linked in ids,
                    f"the memory points at {linked!r}, which is not a stored message: {ids}")
            source = next(m for m in messages if m["message_id"] == linked)
            require_equal(source["role"], "user", source)
            require("Anna arbeitet bei Siemens" in source["text"],
                    f"the memory points at the wrong message: {source['text']!r}")
            require_equal(records[0].metadata.get("source_session_id"), sess.session_id,
                          records[0].metadata)
        finally:
            for task in (reader, worker, persister):
                task.cancel()
            await svc.close()
            conversations.close()


# =====================================================================
# O, P — welcher Provider läuft
# =====================================================================
async def t_o_the_production_configuration_selects_qwen():
    """Der Produktivpfad ist das lokale Modell. Fehlt es, muss das AUSDRUECKLICH als
    degradiert gemeldet werden — nicht als gesundes semantisches Gedaechtnis."""
    import importlib.util
    available = importlib.util.find_spec("sentence_transformers") is not None
    previous = os.environ.pop("SOLVIO_EMBEDDING", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp)
            try:
                health = await svc.health()
                if available:
                    require_equal(health["provider_id"], "qwen-local",
                                  f"the local model is installed but not used: {health}")
                    require_equal(health["degraded"], False, health)
                    require_equal(health["retrieval_quality"], "semantic", health)
                else:
                    require_equal(health["degraded"], True,
                                  "the fallback was reported as healthy")
            finally:
                await svc.close()
    finally:
        if previous is not None:
            os.environ["SOLVIO_EMBEDDING"] = previous


async def t_p_the_hashing_fallback_reports_itself_as_degraded():
    previous = os.environ.get("SOLVIO_EMBEDDING")
    os.environ["SOLVIO_EMBEDDING"] = "hashing"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp)
            try:
                health = await svc.health()
                require_equal(health["provider_id"], "deterministic", health)
                require_equal(health["degraded"], True,
                              "the lexical fallback claimed to be healthy")
                require_equal(health["retrieval_quality"], "degraded_lexical", health)
            finally:
                await svc.close()
    finally:
        if previous is None:
            os.environ.pop("SOLVIO_EMBEDDING", None)
        else:
            os.environ["SOLVIO_EMBEDDING"] = previous


def t_p_retrieval_bounds_are_provider_specific():
    qwen = bounds_for("qwen-local")
    hashing = bounds_for("deterministic")
    require(qwen.min_cosine != hashing.min_cosine,
            "both providers share one cosine threshold — one of them is guessed")
    require(qwen.min_lexical > hashing.min_lexical,
            "the lexical path is not tightened for the semantic provider, where it leaks")


def t_p_the_embedding_dependency_is_declared():
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    require("sentence-transformers" in text,
            "the local embedding dependency is not declared in pyproject.toml")


async def t_o_switching_the_embedding_provider_reindexes():
    """Beobachtet nach dem Wechsel von Hashing auf Qwen: der Index trug weiterhin das
    ALTE Profil als aktives, unter dem neuen waren null Eintraege, und jede semantische
    Suche lief ins Leere — still. Das Fundament wechselt das aktive Profil absichtlich
    nicht von selbst; die Entscheidung gehoert in die Integrationsschicht."""
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get("SOLVIO_EMBEDDING")
        os.environ["SOLVIO_EMBEDDING"] = "hashing"
        try:
            first = _service(tmp)
            try:
                await _fill(first, ["das WLAN im Gartenhaus heisst Kastanie"])
                hashing_profile = first.semantic.index.active_profile()
                require(await first.search("Wie heisst das WLAN im Gartenhaus?"),
                        "the fixture never became searchable under the first provider")
            finally:
                await first.close()
            os.environ.pop("SOLVIO_EMBEDDING", None)
            import importlib.util
            if importlib.util.find_spec("sentence_transformers") is None:
                return                       # ohne zweiten Provider ist nichts zu wechseln
            second = _service(tmp)
            try:
                health = await second.health()
                require(health["reindex_pending"],
                        "the provider changed but no reindex was scheduled")
                require_equal(health.get("previous_profile"), hashing_profile, health)
                hits = await second.search("Wie heisst das WLAN im Gartenhaus?")
                require(any("Kastanie" in h.content for h in hits),
                        f"the memory is unreachable after the provider switch: {hits}")
                require_equal((await second.health())["reindex_pending"], False,
                              "the reindex did not settle")
            finally:
                await second.close()
        finally:
            if previous is None:
                os.environ.pop("SOLVIO_EMBEDDING", None)
            else:
                os.environ["SOLVIO_EMBEDDING"] = previous


# =====================================================================
# Q — Hot Path mit dem echten Anbieter
# =====================================================================
async def t_q_a_slow_real_provider_does_not_block_the_provider_reader():
    """Gegen den ECHTEN Werkzeugpfad, mit einem Dienst, der im Thread wartet — ein
    Modell-Kaltstart kostet gemessen 5,55 s."""
    import time as _time
    from solvio.realtime import core_server as CS
    import base64
    pcm = base64.b64encode(b"\x01\x02" * 240).decode("ascii")
    entered = asyncio.Event()

    class _SlowService:
        base_dir = "<slow>"

        async def search(self, query, *, top_k=3, max_chars=600):
            entered.set()
            await asyncio.to_thread(_time.sleep, 2.0)
            return []

    class _Dispatcher:
        approvals = None

        def __init__(self):
            self.memory_gate = MemoryIntentGate()
            self.tool = MemorySearchTool(_SlowService())

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
    srv.dispatcher = _Dispatcher()
    srv.idle_timeout = 999
    srv.credentials = None
    srv.model = "m"
    sess = CS.Session(srv, _Satellite())
    sess.active = True
    sess.oa = _Provider([
        json.dumps({"type": "response.done", "response": {"output": [
            {"type": "function_call", "name": "memory_search", "call_id": "c1",
             "arguments": '{"query": "x"}'}]}}),
        json.dumps({"type": "response.output_audio.delta", "delta": pcm}),
    ])
    started = asyncio.get_event_loop().time()
    reader = asyncio.create_task(sess._oa_reader())
    worker = asyncio.create_task(sess._tool_loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await sess.ws.got.wait()
        elapsed = asyncio.get_event_loop().time() - started
        require(elapsed < 1.0,
                f"audio reached the satellite only after {elapsed:.1f}s of memory work")
    finally:
        reader.cancel()
        worker.cancel()


def t_q_provider_selection_does_not_import_the_ml_stack():
    """Gemessen: `import sentence_transformers` nur zum Pruefen der Verfuegbarkeit kostete
    +354 MB RSS beim Core-Start und haette die Faulheit des Providers ausgehebelt."""
    import inspect
    from solvio.memory import service as module
    source = inspect.getsource(module.select_local_provider)
    require("find_spec" in source,
            "availability is checked by importing, which pulls in the whole torch stack")
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    require("import sentence_transformers" not in code,
            "the ML stack is imported at provider selection")


# =====================================================================
# R — Aufbewahrung
# =====================================================================
def t_r_a_failed_purge_is_observable():
    """Frueher wurde die Ausnahme stillschweigend geschluckt — dann sieht niemand, dass
    die Aufbewahrungsregel seit Wochen nicht mehr greift."""
    class _Clock:
        def __init__(self):
            self.t = 1_000_000.0

        def __call__(self):
            return self.t

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.sqlite3")
        store = ConversationStore(path, now_fn=_Clock()).open()
        try:
            require(hasattr(store, "last_purge"), "the purge result is not recorded")
            require(store.last_purge is not None, "the open-time purge did not run")
            require_equal(store.last_purge_error, None, store.last_purge_error)
            require_equal(store.last_purge["retention_days"], 90.0, store.last_purge)
        finally:
            store.close()


def t_r_the_purge_result_names_no_message_bodies():
    class _Clock:
        def __init__(self):
            self.t = 1_000_000.0

        def __call__(self):
            return self.t

    clock = _Clock()
    with tempfile.TemporaryDirectory() as tmp:
        store = ConversationStore(os.path.join(tmp, "c.sqlite3"), now_fn=clock,
                                  retention_days=90.0).open()
        cid, _ = store.begin_session("s-1")
        store.add_message(cid, "user", "ein sehr privater Satz ueber Kastanie")
        clock.t += 91 * 86400
        report = store.purge_expired()
        try:
            require_equal(report["conversations"], 1, report)
            blob = json.dumps(report, default=str)
            require("Kastanie" not in blob, f"the purge report carries message text: {blob}")
            require(set(report) == {"conversations", "messages", "empty_explicit",
                                    "retention_days"}, report)
        finally:
            store.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
