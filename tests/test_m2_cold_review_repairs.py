"""M2 — die Blocker aus der unabhängigen Kaltprüfung.

Jeder Test hier wurde zuerst am geprüften Stand `28c5d54` als rot nachgestellt. Sie
schützen Eigenschaften, die eine schmale Fixture nicht sichtbar macht:

  M2-01  ein geteiltes Gate sperrte nach der ERSTEN Sitzung dauerhaft zu
  M2-02  13 von 14 realen Zugangsdaten-Formen kamen durch
  M2-03  Korrektur-Vokabular im Nutztext zerstörte fremde Fakten
  M2-04  ein Schreibvorgang meldete Erfolg, obwohl die Indizierung fehlschlug
  M2-06  ein gelöschter Index wurde nie neu aufgebaut
  M2-08  Vektoren alter Profile
  M2-09  umformulierte Korrekturen ließen zwei widersprüchliche Wahrheiten stehen
  M2-10  ein exakter Bezeichner wurde von der Satzschwelle verworfen
  M2-11  health meldete „gesund", ohne zu wissen, ob das Modell laden kann

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und ueberleben `-O`.

Direkt: python tests/test_m2_cold_review_repairs.py
"""
import asyncio
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
from solvio.memory.embedding import HashingEmbeddingProvider  # noqa: E402
from solvio.memory.intent import detect, looks_like_secret  # noqa: E402
from solvio.memory.service import MemoryService  # noqa: E402
from solvio.tools.memory_tools import MemoryIntentGate, MemoryRememberTool  # noqa: E402


def _service(tmp, provider=None):
    """Vorgabe: der deterministische Provider — schnell und ohne Modell-Ladezeit.

    Tests zur ABRUFQUALITAET nehmen ausdruecklich `_production_service`: der
    Hashing-Rueckfall ist gemessen deutlich schlechter (23/26 bei 8 Falsch-Positiven
    gegen 26/26 bei 1), und ihn als Massstab zu nehmen waere eine Selbsttaeuschung.
    """
    return MemoryService(os.path.join(tmp, "memory"),
                         provider=provider or HashingEmbeddingProvider()).open()


def _production_service(tmp):
    """Der Provider, der im Betrieb laeuft."""
    return MemoryService(os.path.join(tmp, "memory")).open()


async def _fill(svc, memories):
    for text in memories:
        intent = detect("Merk dir: " + text)
        require(intent is not None, f"fixture carries no intent: {text!r}")
        await svc.remember(intent, conversation_id="c-fixture")


class _FlakyProvider(HashingEmbeddingProvider):
    """Ein Provider, dessen Einbettung auf Kommando ausfaellt — ohne Zeitspiel."""

    def __init__(self):
        super().__init__()
        self.fail = False

    async def embed_documents(self, texts):
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return await super().embed_documents(texts)


# =====================================================================
# M2-01 — ein Gate, drei Sitzungen
# =====================================================================
async def t_m2_01_three_sequential_sessions_share_one_gate():
    """Reproduziert am geprueften Stand: `Session.close()` rief `gate.clear()`, das
    `_closed=True` DAUERHAFT setzte. Ab Sitzung 2 kam nur noch `session_closed` — bis zum
    naechsten Core-Neustart. Von drei Merkauftraegen wurde genau einer gespeichert.

    Das Gate haengt am Dispatcher und lebt so lange wie der Prozess; die Sitzungen kommen
    und gehen. Ein dauerhaftes Schliessen ist deshalb der falsche Zustand.
    """
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()               # EINE Instanz, wie build_dispatcher sie baut
        gate.PERMIT_WAIT_SECONDS = 0.3
        tool = MemoryRememberTool(svc, gate)
        try:
            for number in (1, 2, 3):
                session = f"s-{number}"
                gate.offer_user_turn(
                    detect(f"Merk dir: Fakt Nummer {number} betrifft Thema {number}."),
                    session_id=session, turn_id=f"{session}-t1")
                gate.expect_turn(session, f"{session}-t1")
                result = await tool.run({})
                require(result.success,
                        f"session {number} was refused ({result.error}) — the gate stayed "
                        f"closed after an earlier session")
                gate.clear()                    # genau das tut Session.close()
            require_equal(await svc.semantic.memory.count(), 3,
                          "not every session managed to write")
        finally:
            await svc.close()


async def t_m2_01_closing_still_kills_that_session_authority():
    """Die Reparatur darf die Sicherheit nicht aufweichen: das Mandat der geschlossenen
    Sitzung verfaellt, und Wartende lehnen ab."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 5.0
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")
            gate.clear()                        # Sitzung schliesst, bevor eingeloest wurde
            result = await asyncio.wait_for(tool.run({}), timeout=10)
            require(not result.success, "a closed session's permit was still cashed")
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
            # ... und ein WARTENDER Aufruf wird beim Schliessen abgewiesen
            gate.expect_turn("s-2", "s-2-t1")
            pending = asyncio.create_task(tool.run({}))
            await asyncio.sleep(0)
            gate.clear()
            second = await asyncio.wait_for(pending, timeout=10)
            require(not second.success, "a pending call survived the session close")
            require_equal(second.error, "session_closed", second.error)
        finally:
            await svc.close()


async def t_m2_01_a_permit_still_cannot_cross_turns():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 0.2
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t2")
            result = await tool.run({})
            require(not result.success, "turn 1's permit authorized turn 2")
            require_equal(await svc.semantic.memory.count(), 0, "something was stored")
        finally:
            await svc.close()


# =====================================================================
# M2-02 / M2-08 — Zugangsdaten, deutschbewusst
# =====================================================================
def t_m2_02_the_german_credential_battery_is_blocked():
    """Reproduziert: die Vorfassung verlangte den Begriff als exakt gleiches Token und
    liess 13 von 14 realen Formen durch — Komposita, Wert-zuerst, Plural, verblos."""
    battery = [
        "Mein WLAN-Passwort ist Kastanie2024",          # Bindestrich-Kompositum
        "Das WLANPasswort ist Kastanie2024",            # verschmolzen
        "WLAN Passwort Kastanie2024",                   # getrennt, verblos
        "Meine EC-Karten-PIN ist 4711",                 # Kette
        "Die Onlinebanking-PIN ist 9021",
        "Mein Bankpasswort: Loewenzahn",                # Doppelpunkt
        "Mein Recovery-Code ist 8842-1193",
        "Der Backup-Code lautet 77213",
        "Der API-Key ist abcdefghij0123456789",
        "Das Access-Token lautet xyz123abc456",
        "Das Refresh-Token ist rt_9981223",
        "Sommerregen2024 ist mein Passwort",            # Wert zuerst
        "Meine TANs sind 884213 und 991002",            # Plural
        "Die Zugangsdaten lauten admin und geheim99",
        "Meine PIN 4711",                               # ohne Verb
        "Mein Masterpasswort lautet Eichenblatt7",
        "Der Zugangscode fuer den Tresor ist 90210",
        "Der Sicherheitscode ist 8842",
        "Meine Passphrase ist Loewenzahn im Mai",
        "Mein Kennwort fuers Onlinebanking ist Sommerregen2024",
    ]
    leaked = [t for t in battery if not looks_like_secret(t)]
    require_equal(leaked, [], f"credentials were not recognized: {leaked}")


def t_m2_08_benign_lookalikes_are_not_blocked():
    """Die Gegenrichtung: ein Nutzer muss sich Vorgangsnummern, Seriennummern und den
    Namen seines Passwort-Managers merken lassen koennen."""
    benign = [
        "Mein Passwort-Manager heisst Bitwarden",
        "Der Passwortmanager laeuft auf dem Mac",
        "Die Passwortaenderung ist faellig",
        "Die Passwortstaerke war zu gering",
        "Das Ticket hat die UUID 550e8400-e29b-41d4-a716-446655440000",
        "Die Seriennummer des Druckers ist HP-9931-AB",
        "Meine Kundennummer ist 4711-2026",
        "Die Geraetekennung lautet pi-wohnzimmer",
        "Die Vorgangsnummer lautet AB-9931",
        "Mein Fahrrad hat die Rahmennummer WBK4471",
        "Die Bestellnummer ist 4711-2026",
        "Die Garage oeffnet mit dem Code 4711",
        "Ich nutze Zwei-Faktor-Authentifizierung",
        "Mein zweites Langzeit-Testwort ist Saphir 62",
    ]
    refused = [t for t in benign if looks_like_secret(t)]
    require_equal(refused, [], f"benign sentences were wrongly refused: {refused}")


async def t_m2_02_no_credential_value_reaches_the_database_or_the_log():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        values = ["Kastanie2024", "Sommerregen2024", "Eichenblatt7", "884213"]
        try:
            for sentence in ("Merk dir: Mein WLAN-Passwort ist Kastanie2024",
                             "Merk dir: Sommerregen2024 ist mein Passwort",
                             "Merk dir: Mein Masterpasswort lautet Eichenblatt7",
                             "Merk dir: Meine TANs sind 884213 und 991002"):
                result = await svc.remember(detect(sentence), conversation_id="c")
                require(not result.ok, f"a credential was stored: {sentence!r}")
                require_equal(result.reason, "looks_like_secret", result.reason)
                require(not any(v in (result.content or "") for v in values),
                        "the refusal echoed the credential value")
            await _fill(svc, ["Anna arbeitet bei Siemens"])
        finally:
            await svc.close()
        blob = b""
        for root, _dirs, files in os.walk(os.path.join(tmp, "memory")):
            for name in files:
                with open(os.path.join(root, name), "rb") as fh:
                    blob += fh.read()
        for value in values:
            require(value.encode() not in blob, f"{value!r} reached the database")
        require(b"Anna" in blob, "the fixture stored nothing at all")


# =====================================================================
# M2-03 / M2-09 — Korrektur
# =====================================================================
def t_m2_03_correction_words_in_the_payload_are_not_correction_intent():
    """Reproduziert: die Korrekturphrasen wurden im GANZEN Satz gesucht, also auch im
    Nutztext. "Merk dir: Die KORREKTUR der Physik-Klausur ist am Freitag" galt damit als
    Korrektur und konnte einen fremden Fakt loeschen."""
    for sentence in ("Merk dir: Die Korrektur der Physik-Klausur ist am Freitag.",
                     "Merk dir: Ich korrigiere morgen die Klausuren.",
                     "Merk dir: Das war ein Fehler von Bosch, nicht von uns.",
                     "Merk dir dauerhaft: Der Fehler liegt im Netzteil.",
                     "Merk dir: Die Fehlerkorrektur laeuft automatisch."):
        intent = detect(sentence)
        require(intent is not None, f"the instruction was lost: {sentence!r}")
        require_equal(intent.kind, "remember",
                      f"payload vocabulary was read as correction intent: {sentence!r}")


def t_m2_03_explicit_correction_language_is_still_recognized():
    for sentence in ("Korrektur, merk dir: Projekt Aurora startet im Oktober.",
                     "Das war falsch. Merk dir stattdessen: Anna arbeitet bei Bosch.",
                     "Zur Korrektur: merk dir, der Termin ist am Mittwoch.",
                     "Aendere die Erinnerung: Der Vertrag laeuft bis Ende April."):
        intent = detect(sentence)
        require(intent is not None, f"the correction was lost: {sentence!r}")
        require_equal(intent.kind, "correct", f"not recognized as correction: {sentence!r}")


async def t_m2_03_payload_vocabulary_destroys_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, CORPUS.MEMORIES[:8])
            before = {r.content for r in await svc.semantic.memory.active_records()}
            for sentence in ("Merk dir: Die Korrektur der Physik-Klausur ist am Freitag.",
                             "Merk dir: Ich korrigiere morgen die Klausuren.",
                             "Merk dir: Das war ein Fehler von Bosch."):
                await svc.remember(detect(sentence), conversation_id="c")
            after = {r.content for r in await svc.semantic.memory.active_records()}
            require_equal(before - after, set(),
                          f"ordinary sentences destroyed facts: {before - after}")
        finally:
            await svc.close()


async def t_m2_09_a_short_dated_correction_supersedes_its_target():
    cases = [
        (["Papa hat am 3. Mai Geburtstag", "Anna arbeitet bei Siemens in Erlangen",
          "der Zahnarzttermin ist am Dienstag um halb neun"],
         "Korrektur, merk dir: Papa hat am 4. Mai Geburtstag.",
         "Papa hat am 3. Mai Geburtstag"),
        (["der Vertrag laeuft bis Ende Maerz", "der Muell wird donnerstags abgeholt"],
         "Korrektur, merk dir: Der Vertrag laeuft bis Ende April.",
         "der Vertrag laeuft bis Ende Maerz"),
    ]
    for memories, correction, target in cases:
        with tempfile.TemporaryDirectory() as tmp:
            svc = _service(tmp)
            try:
                await _fill(svc, memories)
                result = await svc.remember(detect(correction), conversation_id="c")
                require_equal(result.action, "superseded",
                              f"{correction!r}: {result.action}/{result.reason}")
                active = [r.content for r in await svc.semantic.memory.active_records()]
                require(target not in active, f"the stale fact is still active: {target!r}")
                require_equal(len(active), len(memories),
                              f"a parallel contradictory fact was created: {active}")
            finally:
                await svc.close()


async def t_m2_09_an_ambiguous_correction_leaves_everything_alone():
    """Wenn der beste Kandidat den zweitbesten nicht deutlich schlaegt, wird nachgefragt —
    nicht geraten. Gemessen war genau hier ein FALSCHER Kandidat vorn."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            memories = ["der Termin mit Anna ist am Dienstag um zehn",
                        "der Termin mit Bernd ist am Dienstag um zehn"]
            await _fill(svc, memories)
            before = {r.content for r in await svc.semantic.memory.active_records()}
            result = await svc.remember(
                detect("Korrektur, merk dir: Der Termin ist am Mittwoch um elf."),
                conversation_id="c")
            after = {r.content for r in await svc.semantic.memory.active_records()}
            require(result.action in ("ambiguous", "created"),
                    f"an ambiguous correction acted destructively: {result.action}")
            require_equal(before - after, set(), "an ambiguous correction destroyed a fact")
        finally:
            await svc.close()


async def t_m2_09_a_superseded_fact_stays_auditable():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            await _fill(svc, ["Projekt Aurora startet im September"])
            old = (await svc.semantic.memory.active_records())[0]
            result = await svc.remember(
                detect("Korrektur, merk dir: Projekt Aurora startet im Oktober."),
                conversation_id="c")
            require_equal(result.action, "superseded", result.action)
            stale = await svc.semantic.get(old.id)
            require(stale is not None, "the superseded fact was deleted, not superseded")
            require_equal(stale.superseded_by, result.memory_id, stale.superseded_by)
            hits = await svc.search("Wann startet Projekt Aurora?")
            require(hits, "the corrected fact cannot be retrieved")
            require("Oktober" in hits[0].content,
                    f"stale history ranks ahead of current truth: {hits[0].content!r}")
            require(all(h.memory_id != old.id for h in hits),
                    "the superseded fact was returned as active")
        finally:
            await svc.close()


# =====================================================================
# M2-04 / 05 / 06 / 07 — Index-Konsistenz
# =====================================================================
async def t_m2_04_a_write_whose_indexing_failed_is_not_reported_as_complete():
    """Reproduziert: der Aufruf bekam `ok=True, action=created`, waehrend im Index NULL
    Vektoren lagen. Der Eintrag war dauerhaft gespeichert, aber semantisch unsichtbar —
    und nichts machte das sichtbar."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = _FlakyProvider()
        svc = _service(tmp, provider)
        try:
            await svc.ensure_index()
            provider.fail = True
            result = await svc.remember(detect("Merk dir: Anna arbeitet bei Siemens."),
                                        conversation_id="c")
            require(result.ok, "the durable write itself should still have succeeded")
            require_equal(result.indexed, False,
                          "the failed indexing was reported as complete")
            require_equal(await svc.semantic.memory.count(), 1, "the record was lost")
            report = await svc.check_index_consistency()
            require(not report["consistent"], f"the gap is invisible: {report}")
            require_equal(report["missing"], 1, report)
            health = await svc.health()
            require(health["index_incomplete"], f"health hides the gap: {health}")
            # ... und der naechste Zugriff stellt es her
            provider.fail = False
            await svc.search("Wo arbeitet Anna?")
            recovered = await svc.check_index_consistency()
            require(recovered["consistent"], f"the index was never repaired: {recovered}")
        finally:
            await svc.close()


async def t_m2_06_a_deleted_index_is_rebuilt_from_canonical_memory():
    """Der abgeleitete Index darf geloescht werden; der kanonische Speicher ist die
    Wahrheit und baut ihn deterministisch wieder auf."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        await _fill(svc, ["Anna arbeitet bei Siemens", "der Vertrag laeuft bis Ende Maerz"])
        base = svc.base_dir
        await svc.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(base, "semantic_index.sqlite3" + suffix))
            except OSError:
                pass
        svc = _service(tmp)
        try:
            hits = await svc.search("Wo arbeitet Anna?")
            require(hits, "the memory is unreachable after the index was deleted")
            report = await svc.check_index_consistency()
            require(report["consistent"], f"the rebuild left a gap: {report}")
            require_equal(report["indexed"], 2, report)
        finally:
            await svc.close()


async def t_m2_07_an_interrupted_rebuild_is_picked_up_on_the_next_open():
    """Ein Abbruch mitten im Neuaufbau darf keinen dauerhaft halben Index hinterlassen.
    Der Vergleich gegen den kanonischen Speicher findet die Luecke beim naechsten Oeffnen
    wieder, und `rebuild()` ueberspringt, was schon stimmt."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = _FlakyProvider()
        svc = _service(tmp, provider)
        await _fill(svc, ["Anna arbeitet bei Siemens", "der Vertrag laeuft bis Ende Maerz",
                          "der Drucker steht im Arbeitszimmer"])
        base = svc.base_dir
        await svc.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(base, "semantic_index.sqlite3" + suffix))
            except OSError:
                pass
        broken = _FlakyProvider()
        broken.fail = True
        svc = _service(tmp, broken)                 # Neuaufbau scheitert
        try:
            try:
                await svc.ensure_index()
            except Exception:                        # noqa: BLE001 - erwartet
                pass
            require(svc.reindex_pending, "the failed rebuild was not kept pending")
        finally:
            await svc.close()
        svc = _service(tmp)                          # naechster Start, Provider gesund
        try:
            await svc.ensure_index()
            report = await svc.check_index_consistency()
            require(report["consistent"], f"the interrupted rebuild never completed: {report}")
            require_equal(report["indexed"], 3, report)
        finally:
            await svc.close()


async def t_m2_05_a_profile_switch_survives_a_restart():
    """Der Wechsel des Einbettungsprofils muss den Prozess ueberleben: der Index traegt
    das neue Profil, und die alten Vektoren geben sich nicht als aktuelle aus."""
    with tempfile.TemporaryDirectory() as tmp:
        first = _service(tmp, HashingEmbeddingProvider(dimension=256, profile_version="1"))
        await _fill(first, ["Anna arbeitet bei Siemens"])
        old_profile = first.provider.profile.key
        memory_id = (await first.semantic.memory.active_records())[0].id
        await first.close()

        second = _service(tmp, HashingEmbeddingProvider(dimension=512, profile_version="2"))
        try:
            health = await second.health()
            require(health["reindex_pending"] or health["index_incomplete"],
                    f"the profile switch was not noticed: {health}")
            hits = await second.search("Wo arbeitet Anna?")
            require(hits, "the memory is unreachable under the new profile")
            report = await second.check_index_consistency()
            require(report["consistent"], f"the new profile is incomplete: {report}")
            index = second.semantic.index
            require_equal(index.active_profile(), second.provider.profile.key,
                          "the active profile did not follow the provider")
            require(index.get_entry(memory_id, old_profile) is not None,
                    "the old vector vanished — history should stay auditable")
            require(index.get_entry(memory_id, second.provider.profile.key) is not None,
                    "no vector under the current profile")
        finally:
            await second.close()

        third = _service(tmp, HashingEmbeddingProvider(dimension=512, profile_version="2"))
        try:
            health = await third.health()
            require_equal(health["reindex_pending"], False,
                          "the migration state did not survive the restart")
            require(await third.search("Wo arbeitet Anna?"), "unreachable after restart")
        finally:
            await third.close()


# =====================================================================
# M2-10 — der lexikalische Arm
# =====================================================================
async def t_m2_10_an_exact_identifier_is_not_discarded():
    """Reproduziert: die Anfrage "WBK4471" stand als Kandidat auf Position 0 und wurde
    von der Relevanzschranke verworfen — ihre Wortueberlappung mit dem vollen Satz betrug
    nur 0.333, und die Schwelle ist auf SAETZE kalibriert."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _production_service(tmp)
        try:
            await _fill(svc, CORPUS.MEMORIES)
            for query, needle in (("WBK4471", "WBK4471"),
                                  ("Rahmennummer", "WBK4471"),
                                  ("Solgaarden", "Solgaarden"),
                                  ("Kastanie", "Kastanie")):
                hits = await svc.search(query, top_k=3)
                require(any(needle.lower() in h.content.lower() for h in hits),
                        f"{query!r} found nothing: {[h.content for h in hits]}")
        finally:
            await svc.close()


async def t_m2_10_common_words_still_return_nothing():
    """Die Aufweichung darf nicht zum Scheunentor werden."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _production_service(tmp)
        try:
            await _fill(svc, CORPUS.MEMORIES)
            for query in ("hat", "ist", "mein", "heisst", "steht", "die"):
                hits = await svc.search(query, top_k=3)
                require_equal(hits, [],
                              f"the common word {query!r} returned {[h.content for h in hits]}")
        finally:
            await svc.close()


async def t_m2_10_the_corpus_precision_is_unchanged():
    """Nach allen Reparaturen dieselbe Messung wie vorher: 26 relevante Fragen finden
    ihren Fakt, hoechstens jede zehnte unbeteiligte liefert etwas."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _production_service(tmp)
        try:
            await _fill(svc, CORPUS.MEMORIES)
            missed = [q for q, needle in CORPUS.RELEVANT
                      if not any(needle.lower() in h.content.lower()
                                 for h in await svc.search(q))]
            require(len(missed) <= len(CORPUS.RELEVANT) // 10,
                    f"{len(missed)} relevant questions missed: {missed}")
            wrong = [q for q in CORPUS.UNRELATED if await svc.search(q)]
            require(len(wrong) <= max(2, len(CORPUS.UNRELATED) // 10),
                    f"{len(wrong)} unrelated questions returned a memory: {wrong}")
        finally:
            await svc.close()


# =====================================================================
# M2-11 — health sagt die Wahrheit
# =====================================================================
async def t_m2_11_health_distinguishes_its_states():
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            health = await svc.health()
            for field in ("state", "provider_id", "degraded", "model_loaded",
                          "index_incomplete", "reindex_pending", "retrieval_quality"):
                require(field in health, f"health does not report {field}: {health}")
            require_equal(health["degraded"], True,
                          "the deterministic fallback claimed to be the production path")
            require_equal(health["state"], "degraded_fallback", health)
        finally:
            await svc.close()


async def t_m2_11_a_model_that_cannot_load_is_not_reported_as_healthy():
    """Reproduziert: die Verfuegbarkeitspruefung sieht nur das PAKET. Konnte das Modell
    nicht laden, meldete health trotzdem `degraded=false` — also "gesund"."""
    class _Unloadable(HashingEmbeddingProvider):
        @property
        def profile(self):
            from solvio.memory.embedding import EmbeddingProfile
            return EmbeddingProfile(provider_id="qwen-local", model_id="Qwen3-Embedding-0.6B",
                                    dimension=256, profile_version="1")

        async def embed_queries(self, texts):
            raise RuntimeError("model could not be loaded")

    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp, _Unloadable())
        try:
            await _fill(svc, ["Anna arbeitet bei Siemens"])
            try:
                await svc.search("Wo arbeitet Anna?")
            except Exception:                       # noqa: BLE001 - erwartet
                pass
            health = await svc.health()
            require_equal(health["state"], "embedding_unavailable",
                          f"a model that cannot load was reported as {health['state']!r}")
            require("model_load_error" in health, health)
            require(health["model_load_error"], "the failure has no diagnostic")
        finally:
            await svc.close()


async def t_m2_11_a_pending_rebuild_is_visible_in_health():
    with tempfile.TemporaryDirectory() as tmp:
        first = _service(tmp, HashingEmbeddingProvider(dimension=256, profile_version="1"))
        await _fill(first, ["Anna arbeitet bei Siemens"])
        await first.close()
        second = _service(tmp, HashingEmbeddingProvider(dimension=512, profile_version="2"))
        try:
            health = await second.health()
            require(health["reindex_pending"],
                    f"a pending rebuild is invisible in health: {health}")
        finally:
            await second.close()


# =====================================================================
# Zielgerichtete Nachbesserung nach der Validierung
# =====================================================================
# Ein gegnerischer Korpus fuer ASR-getrennte Zusammensetzungen. Bewusst NICHT aus der
# Begriffsliste erzeugt, sondern so geschrieben, wie eine Spracherkennung es liefert:
# klein, ohne Bindestriche, ohne Satzzeichen, Komposita auseinandergerissen.
ASR_SPACED_CREDENTIALS = [
    "mein recovery code ist X7712",
    "mein backup code ist 4471",
    "der zugangs code ist 9021",
    "der sicherheits code ist 8842",
    "mein wiederherstellungs code ist 1193",
    "meine geheim zahl ist 4711",
    "meine online banking pin ist 9021",
    "mein wlan passwort ist Kastanie2024",
    "meine ec karten pin ist 4711",
    "das access token ist abc12345",
    "das refresh token ist rt99812",
    "der api key ist k9912abc",
    "meine seed phrase ist blau rot gruen",
    "MEIN RECOVERY CODE IST X7712",
    "mein recovery code X7712",
    "mein master passwort lautet Eichenblatt7",
    "die zugangs daten lauten admin und geheim99",
    "meine wieder herstellungs codes sind 8842 und 1193",
]

# Harmlos — dieselben Woerter, andere Bedeutung.
ASR_SPACED_BENIGN = [
    "Passwort Manager",
    "Mein Passwort Manager heisst Bitwarden",
    "PIN Eingabe war zu langsam",
    "Die PIN Eingabe ist gesperrt",
    "Herr Tan kommt am Dienstag",
    "Der Token ist abgelaufen",
    "Das Passwort war vergessen",
    "Die Passwort Aenderung ist faellig",
    "Das Ticket hat die UUID 550e8400-e29b-41d4-a716-446655440000",
    "Die Seriennummer des Druckers ist HP-9931-AB",
    "Meine Kundennummer ist 4711-2026",
    "Die Geraetekennung lautet pi-wohnzimmer",
    "Die Garage oeffnet mit dem Code 4711",
    "Mein zweites Langzeit-Testwort ist Saphir 62",
]


def t_m2_02r_asr_spaced_credentials_are_blocked():
    """Reproduziert end-to-end: "Merk dir: mein recovery code ist X7712" wurde
    angenommen, gespeichert, war abrufbar und wurde woertlich zurueckgesprochen.

    Die Erkennung sah nur EINZELNE Tokens. Die Spracherkennung trennt Komposita aber
    regelmaessig — sechs von dreizehn Formen kamen durch.
    """
    leaked = [t for t in ASR_SPACED_CREDENTIALS if not looks_like_secret(t)]
    require_equal(leaked, [], f"ASR-spaced credentials leaked: {leaked}")


def t_m2_02r_the_same_words_stay_benign_when_they_mean_something_else():
    """Die Zusammenziehung gilt in beide Richtungen: "PIN Eingabe" ergibt "pineingabe"
    und faellt am harmlosen Kopf durch."""
    refused = [t for t in ASR_SPACED_BENIGN if looks_like_secret(t)]
    require_equal(refused, [], f"benign phrases were refused: {refused}")


async def t_m2_02r_no_asr_spaced_credential_reaches_the_store_or_the_log():
    """End-to-end ueber den echten Schreibweg, mit Byte-Pruefung der Datenbank."""
    values = ["X7712", "Kastanie2024", "rt99812", "Eichenblatt7"]
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        try:
            for sentence in ASR_SPACED_CREDENTIALS:
                intent = detect("Merk dir: " + sentence)
                require(intent is not None, f"fixture carries no intent: {sentence!r}")
                result = await svc.remember(intent, conversation_id="c")
                require(not result.ok, f"a credential was stored: {sentence!r}")
                require_equal(result.reason, "looks_like_secret", result.reason)
                require(not any(v in (result.content or "") for v in values),
                        "the refusal echoed the credential value")
            await _fill(svc, ["Anna arbeitet bei Siemens"])
        finally:
            await svc.close()
        blob = b""
        for root, _dirs, files in os.walk(os.path.join(tmp, "memory")):
            for name in files:
                with open(os.path.join(root, name), "rb") as fh:
                    blob += fh.read()
        for value in values:
            require(value.encode() not in blob, f"{value!r} reached the database")
        require(b"Anna" in blob, "the fixture stored nothing at all")


# ---------------------------------------------------------------------
# Wahrhaftigkeit gegenueber dem Nutzer
# ---------------------------------------------------------------------
async def t_m2_indexed_false_is_not_spoken_as_plain_success():
    """Reproduziert: der kanonische Eintrag entstand, die Indizierung scheiterte, und das
    Werkzeug sagte trotzdem "Gemerkt." — ein Versprechen von Abrufbereitschaft, die es
    noch nicht gab. `indexed` fehlte auch in den Werkzeugdaten."""
    with tempfile.TemporaryDirectory() as tmp:
        provider = _FlakyProvider()
        svc = _service(tmp, provider)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 0.3
        tool = MemoryRememberTool(svc, gate)
        try:
            await svc.ensure_index()
            provider.fail = True
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")
            result = await tool.run({})
            require(result.success, "the durable write should still have succeeded")
            require("indexed" in (result.data or {}),
                    f"the tool result hides the indexing state: {result.data}")
            require_equal(result.data["indexed"], False, result.data)
            spoken = result.human_message or ""
            require(spoken.strip() != "Gemerkt.",
                    "an incomplete index was acknowledged as plain success")
            require(spoken, "the user would hear nothing about the incompleteness")
            require_equal(await svc.semantic.memory.count(), 1, "the record was lost")
        finally:
            await svc.close()


async def t_m2_a_fully_indexed_write_still_says_it_plainly():
    """Die Gegenrichtung: gelingt alles, soll SOLVIO nicht relativieren."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        gate = MemoryIntentGate()
        gate.PERMIT_WAIT_SECONDS = 0.3
        tool = MemoryRememberTool(svc, gate)
        try:
            gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                                 session_id="s-1", turn_id="s-1-t1")
            gate.expect_turn("s-1", "s-1-t1")
            result = await tool.run({})
            require(result.success, result.error)
            require_equal(result.data["indexed"], True, result.data)
            require_equal(result.human_message, "Gemerkt.", result.human_message)
        finally:
            await svc.close()


async def t_m2_a_failed_write_is_still_a_failure():
    class _BrokenStore:
        base_dir = "<broken>"

        async def remember(self, *a, **kw):
            from solvio.memory.service import MemoryUnavailable
            raise MemoryUnavailable("store is gone")

    gate = MemoryIntentGate()
    gate.PERMIT_WAIT_SECONDS = 0.3
    gate.offer_user_turn(detect("Merk dir: Anna arbeitet bei Siemens."),
                         session_id="s-1", turn_id="s-1-t1")
    gate.expect_turn("s-1", "s-1-t1")
    result = await MemoryRememberTool(_BrokenStore(), gate).run({})
    require(not result.success, "a failed write reported success")
    require(result.human_message, "the failure would be silent")


# ---------------------------------------------------------------------
# Abgebrochener Neuaufbau, health-Erholung, kurze Batch
# ---------------------------------------------------------------------
class _SlowProvider(HashingEmbeddingProvider):
    """Meldet, wann der Neuaufbau wirklich einbettet — dann erst wird abgebrochen.

    Ohne dieses Signal schneidet der Test womoeglich, bevor `rebuild()` ueberhaupt
    begonnen hat, und beweist dann nichts.
    """

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()

    async def embed_documents(self, texts):
        self.entered.set()
        await asyncio.sleep(5)
        return await super().embed_documents(texts)


class _ShortBatchProvider(HashingEmbeddingProvider):
    """Liefert einen Vektor zu wenig — die Fehlerform, die `rebuild()` still schluckt."""

    def __init__(self):
        super().__init__()
        self.short = True

    async def embed_documents(self, texts):
        full = await super().embed_documents(texts)
        return full[:-1] if (self.short and len(full) > 1) else full


def _wipe_index(base):
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(os.path.join(base, "semantic_index.sqlite3" + suffix))
        except OSError:
            pass


async def t_m2_a_cancelled_rebuild_keeps_its_retry_intent():
    """Reproduziert: `reindex_pending` wurde VOR dem Neuaufbau auf False gesetzt. Ein
    Abbruch lief am gewoehnlichen Exception-Zweig vorbei, und die Wiederholungsabsicht
    war fuer den Rest des Prozesses verschwunden — nur ein Neustart half."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        await _fill(svc, ["Anna arbeitet bei Siemens", "der Vertrag laeuft bis Ende Maerz"])
        base = svc.base_dir
        await svc.close()
        _wipe_index(base)

        slow = _SlowProvider()
        svc = _service(tmp, slow)
        task = asyncio.create_task(svc.ensure_index())
        await asyncio.wait_for(slow.entered.wait(), timeout=10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            require(svc.reindex_pending,
                    "the cancelled rebuild lost its retry intent — a restart would be needed")
            require(svc.index_incomplete, "the incompleteness became invisible")
            health = await svc.health()
            require(health["reindex_pending"] or health["index_incomplete"],
                    f"health hides the pending rebuild: {health}")
        finally:
            await svc.close()

        svc = _service(tmp)                      # gewoehnlicher naechster Zugriff
        try:
            hits = await svc.search("Wo arbeitet Anna?")
            require(hits, "the memory stayed unreachable after the cancelled rebuild")
            report = await svc.check_index_consistency()
            require(report["consistent"], f"the rebuild never converged: {report}")
        finally:
            await svc.close()


async def t_m2_health_recovers_after_a_transient_embedding_failure():
    """Reproduziert: `model_load_error` blieb stehen, nachdem der Provider laengst wieder
    arbeitete — health meldete dauerhaft `embedding_unavailable`. Zugleich widersprachen
    sich die alten Felder: `degraded=false` und `retrieval_quality='semantic'` neben
    `state='embedding_unavailable'`."""
    class _Recovering(HashingEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def embed_queries(self, texts):
            if self.fail:
                raise RuntimeError("model could not be loaded")
            return await super().embed_queries(texts)

    with tempfile.TemporaryDirectory() as tmp:
        provider = _Recovering()
        svc = _service(tmp, provider)
        try:
            await _fill(svc, ["Anna arbeitet bei Siemens"])
            try:
                await svc.search("Wo arbeitet Anna?")
            except Exception:                    # noqa: BLE001 - erwartet
                pass
            during = await svc.health()
            require_equal(during["state"], "embedding_unavailable", during)
            require_equal(during["degraded"], True,
                          "the legacy field contradicts the explicit state")
            require_equal(during["retrieval_quality"], "unavailable",
                          "the legacy field claims semantic retrieval while it is down")

            provider.fail = False
            hits = await svc.search("Wo arbeitet Anna?")
            require(hits, "the provider recovered but retrieval stayed empty")
            after = await svc.health()
            require(after["state"] != "embedding_unavailable",
                    f"the stale load error survived the recovery: {after}")
            require(not after.get("model_load_error"),
                    f"model_load_error was not cleared: {after.get('model_load_error')}")
            require(after["retrieval_quality"] != "unavailable", after)
        finally:
            await svc.close()


async def t_m2_a_short_embedding_batch_is_visible_and_converges():
    """Reproduziert: `rebuild()` paart nach Position. Liefert der Provider weniger
    Vektoren, faellt der Rest lautlos weg — der Bericht meldete `indexed: 4` bei
    `total_in_index: 3`. Der Vergleich gegen den kanonischen Speicher ist die Wahrheit."""
    with tempfile.TemporaryDirectory() as tmp:
        svc = _service(tmp)
        await _fill(svc, [f"Fakt Nummer {i} betrifft Thema {i}" for i in range(4)])
        base = svc.base_dir
        await svc.close()
        _wipe_index(base)

        provider = _ShortBatchProvider()
        svc = _service(tmp, provider)
        try:
            await svc.ensure_index()
            report = await svc.check_index_consistency()
            require(not report["consistent"],
                    f"the dropped vector was accepted as complete: {report}")
            require_equal(report["missing"], 1, report)
            require(svc.reindex_pending, "the gap left no retry intent")
            health = await svc.health()
            require(health["index_incomplete"], f"health hides the gap: {health}")

            provider.short = False               # Provider geheilt
            await svc.search("Was betrifft Thema 2?")
            converged = await svc.check_index_consistency()
            require(converged["consistent"], f"the rebuild never converged: {converged}")
            require_equal(converged["indexed"], 4, converged)
            require(not svc.reindex_pending, "the retry intent never settled")
            for i in range(4):
                hits = await svc.search(f"Was betrifft Thema {i}?")
                require(hits, f"memory {i} stayed unreachable")
        finally:
            await svc.close()


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
